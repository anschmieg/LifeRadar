import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import QRCode from 'qrcode';

import { BaseConnector } from './base.mjs';
import { rejectOutboundMessage } from '../read-only.mjs';
import { SignalSidecarClient } from '../signal-client.mjs';

// Real Signal LINKED-DEVICE connector (signal-cli-rest-api sidecar).
//
// Security model (details in docs/signal-connector.md):
//   * Pairing: GET /v1/qrcodelink/raw -> in-memory attempt only. The device
//     link URI / QR is never written to logs or Postgres; attempts live in
//     this process's memory and the completed attempt clears qr_text/qr_svg.
//   * Session: only the paired account id + pairing timestamp are persisted,
//     in signal.session inside the connector session dir (the gateway view of
//     the nested /home/.local/share/signal-cli bind mount). Keys live in the
//     sidecar's own store inside that same directory tree.
//   * Receive: json-rpc websocket stream through the allowlist proxy. Only
//     inbound events are handled. Receipt policy: signal-cli's DEFAULT
//     delivery receipts are accepted; read receipts are NEVER requested
//     (no send_read_receipts, no /v1/receipts, no receipt methods here).
//   * Outbound: sendMessage() throws read-only; this class deliberately has
//     no send/reaction/typing/presence/receipt method at all.

const QR_REFRESH_MS = 60_000;
const SESSION_FILE = 'signal.session';
const LOGIN_PROMPT = 'Scan this QR code with Signal on your phone: Settings → Linked devices → Link a device.';

function toSafeDate(value) {
  const date = value == null ? new Date() : new Date(Number(value));
  return Number.isNaN(date.getTime()) ? new Date() : date;
}

export class SignalConnector extends BaseConnector {
  constructor({
    db,
    logger,
    provider = 'signal',
    sessionDir,
    hasSession = null,
    createSignalClient = null,
    createSignalApi = null,
    sidecarUrl = null,
    deviceName = 'liferadar',
    fetchImpl = null,
    schedule = null,
    random = Math.random,
    reconnectBaseMs = 1000,
    reconnectMaxMs = 30000,
  } = {}) {
    super({ db, logger, provider, sessionDir });
    this.hasSessionFn = hasSession;
    this.createSignalClient = createSignalClient;
    this.createSignalApi = createSignalApi;
    this.sidecarUrl = sidecarUrl;
    this.deviceName = deviceName;
    this.fetchImpl = fetchImpl;
    this.schedule = schedule ?? ((fn, ms) => { const timer = setTimeout(fn, ms); timer.unref?.(); return timer; });
    this.random = random;
    this.reconnectBaseMs = reconnectBaseMs;
    this.reconnectMaxMs = reconnectMaxMs;

    this.client = null;
    this.apiInstance = null;
    this.liveAccount = null;
    this.stopped = false;
    this.reconnectAttempts = 0;
    this.reconnectTimer = null;
  }

  // ---------------------------------------------------------------- plumbing

  #api() {
    if (this.apiInstance) return this.apiInstance;
    if (this.createSignalApi) {
      this.apiInstance = this.createSignalApi();
      return this.apiInstance;
    }
    if (!this.sidecarUrl) throw new Error('signal sidecar url is not configured');
    this.apiInstance = new SignalSidecarClient({ baseUrl: this.sidecarUrl, fetchImpl: this.fetchImpl ?? undefined, logger: this.logger });
    return this.apiInstance;
  }

  #sessionPath() {
    return path.join(this.sessionDir, SESSION_FILE);
  }

  async #readSession() {
    try {
      const raw = await readFile(this.#sessionPath(), 'utf8');
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed.account === 'string' && parsed.account) return parsed;
      return null;
    } catch (error) {
      if (error.code === 'ENOENT') return null;
      this.logger.warn({ err: { name: error.name, message: 'unreadable signal session file' } }, 'signal session unreadable');
      return null;
    }
  }

  async #writeSession(account) {
    await writeFile(this.#sessionPath(), `${JSON.stringify({ account, paired_at: new Date().toISOString() })}\n`, { mode: 0o600 });
  }

  async #upsertAccount(accountId, { authState, lastSyncedAt = undefined, lastError = undefined, lastErrorAt = undefined, metadata = {} } = {}) {
    await this.db.upsertConnectorAccount({
      provider: this.provider,
      accountId,
      authState,
      enabled: true,
      lastSyncedAt,
      lastError,
      lastErrorAt,
      metadata: {
        read_only: true,
        receipt_policy: { delivery: 'accepted', read: 'disabled' },
        ...metadata,
      },
    });
  }

  // ----------------------------------------------------------------- start

  async start() {
    await this.ensureDirectories();
    this.stopped = false;
    if (this.hasSessionFn && !(await this.hasSessionFn())) {
      await this.#upsertAccount(this.defaultAccountId, { authState: 'no_session', metadata: { status: 'no_session' } });
      return { status: 'no_session', provider: this.provider };
    }
    const session = await this.#readSession();
    if (!session) {
      await this.#upsertAccount(this.defaultAccountId, { authState: 'no_session', metadata: { status: 'no_session' } });
      return { status: 'no_session', provider: this.provider };
    }
    this.liveAccount = session.account;
    await this.#upsertAccount(session.account, {
      authState: 'connected',
      lastSyncedAt: new Date(),
      metadata: { status: 'connected', paired_at: session.paired_at ?? null },
    });
    try {
      await this.#connectLive(session.account);
      return { status: 'connected', provider: this.provider, account_id: session.account };
    } catch (error) {
      this.logger.warn({ err: error }, 'signal receive attach failed; scheduling reconnect');
      this.#scheduleReconnect(session.account);
      return { status: 'degraded', provider: this.provider, account_id: session.account };
    }
  }

  // -------------------------------------------------------- live connection

  #buildLiveClient(account) {
    const api = this.#api();
    const stream = api.openReceiveStream(account);
    stream.events.on('error', (error) => this.logger.warn({ err: error }, 'signal receive stream error'));
    return {
      ev: {
        on(name, listener) {
          if (name === 'message') stream.events.on('event', listener);
          else if (name === 'connection.close') stream.events.on('close', listener);
        },
      },
      end: async () => { stream.close(); },
    };
  }

  async #connectLive(account) {
    if (this.client) return this.client;
    const client = this.createSignalClient ? await this.createSignalClient(account) : this.#buildLiveClient(account);
    client.ev.on('message', (event) => { this.#handleLiveEvent(account, event); });
    client.ev.on('connection.close', () => { this.#handleDisconnect(account); });
    this.client = client;
    return client;
  }

  async #stopLive() {
    if (!this.client) return;
    const client = this.client;
    this.client = null;
    try {
      await client.end?.();
    } catch (error) {
      this.logger.warn({ err: { name: error.name } }, 'signal stream close failed');
    }
  }

  #handleDisconnect(account) {
    if (this.client) {
      const client = this.client;
      this.client = null;
      Promise.resolve(client.end?.()).catch(() => {});
    }
    if (this.stopped) return;
    this.logger.warn('signal receive stream closed; scheduling reconnect');
    this.#scheduleReconnect(account);
  }

  #scheduleReconnect(account) {
    if (this.stopped || this.reconnectTimer) return; // single-flight
    const attempt = ++this.reconnectAttempts;
    const ceiling = Math.min(this.reconnectMaxMs, this.reconnectBaseMs * 2 ** Math.min(attempt - 1, 16));
    const delay = Math.max(1, Math.round(ceiling * (0.5 + this.random())));
    this.reconnectTimer = this.schedule(async () => {
      this.reconnectTimer = null;
      if (this.stopped) return;
      try {
        const session = await this.#readSession();
        if (!session) return; // logged out while waiting
        await this.#connectLive(session.account);
        this.reconnectAttempts = 0;
        this.logger.info({ attempt }, 'signal receive stream reconnected');
      } catch (error) {
        this.logger.warn({ err: error, attempt }, 'signal receive reconnect failed');
        this.#scheduleReconnect(account);
      }
    }, delay);
  }

  async #handleLiveEvent(account, event) {
    if (!event || typeof event !== 'object') return;
    if (event.kind === 'receipt') {
      // Delivery receipts (and any inbound read receipt) are accepted per
      // operator policy: acknowledged locally, never answered, never persisted.
      this.logger.debug({ receipt_type: event.receiptType ?? null }, 'signal receipt accepted without response');
      return;
    }
    if (event.kind !== 'message') {
      this.logger.debug({ kind: event.kind }, 'signal event ignored');
      return;
    }
    try {
      const row = await this.db.ingestSignalMessage(account, event);
      if (row?.conversationId) {
        await this.db.setCheckpoint('signal', account, `dialog:${event.conversationId}`, {
          message_id: row.externalId,
          updated_at: toSafeDate(event.occurredAt).toISOString(),
        });
      }
      await this.#upsertAccount(account, {
        authState: 'connected',
        lastSyncedAt: new Date(),
        metadata: { status: 'connected', last_live_at: new Date().toISOString() },
      });
    } catch (error) {
      // No checkpoint on failure: the cursor only advances after a successful write.
      this.logger.error({ err: error }, 'signal live ingestion failed');
    }
  }

  // --------------------------------------------------------------- pairing

  async #issueQr(attemptId) {
    const uri = await this.#api().getDeviceLinkUri(this.deviceName);
    const qrSvg = await QRCode.toString(uri, { type: 'svg', margin: 1 });
    return this.updateAttempt(attemptId, {
      state: 'awaiting_qr_scan',
      prompt: LOGIN_PROMPT,
      fields: [],
      qr_text: uri,
      qr_svg: qrSvg,
      error: null,
      metadata: {
        ...(this.attempts.get(attemptId)?.metadata || {}),
        mode: 'qr',
        qr_supported: true,
        qr_generated_at: Date.now(),
      },
    });
  }

  async beginLogin(fields = {}) {
    const attempt = this.createAttempt({
      state: 'initializing',
      prompt: LOGIN_PROMPT,
      metadata: { mode: 'qr', read_only: true },
    });
    try {
      await this.ensureDirectories();
      await this.#issueQr(attempt.attempt_id);
    } catch (error) {
      this.logger.warn({ err: { name: error.name, message: error.message, status: error.statusCode ?? null } }, 'signal pairing QR creation failed');
      return this.updateAttempt(attempt.attempt_id, {
        state: 'error',
        error: 'Signal pairing service is unavailable. It may not be running yet.',
      });
    }
    return this.attempts.get(attempt.attempt_id);
  }

  async submitLoginStep(attemptId) {
    const attempt = await this.getLoginAttempt(attemptId);
    if (attempt.state === 'completed') return attempt;

    let accounts;
    try {
      accounts = await this.#api().getAccounts();
    } catch (error) {
      this.logger.warn({ err: { name: error.name, status: error.statusCode ?? null }, attempt_id: attemptId }, 'signal pairing poll failed');
      if (attempt.state === 'error') {
        try {
          return await this.#issueQr(attemptId);
        } catch {
          return attempt;
        }
      }
      return attempt;
    }

    if (accounts.length > 0) {
      const account = accounts[0];
      const pairedAt = new Date().toISOString();
      await this.#writeSession(account);
      this.liveAccount = account;
      await this.#upsertAccount(account, {
        authState: 'connected',
        lastSyncedAt: new Date(),
        metadata: { status: 'connected', paired_at: pairedAt },
      });
      try {
        await this.#connectLive(account);
      } catch (error) {
        this.logger.warn({ err: error }, 'signal receive attach after pairing failed; scheduling reconnect');
        this.#scheduleReconnect(account);
      }
      return this.updateAttempt(attemptId, {
        state: 'completed',
        prompt: null,
        fields: [],
        qr_text: null,
        qr_svg: null,
        account_id: account,
        error: null,
        metadata: { ...(attempt.metadata || {}), paired: true, paired_at: pairedAt },
      });
    }

    if (attempt.state === 'error') {
      try {
        return await this.#issueQr(attemptId);
      } catch {
        return attempt;
      }
    }

    const generatedAt = attempt.metadata?.qr_generated_at || 0;
    if (!attempt.qr_text || Date.now() - generatedAt > QR_REFRESH_MS) {
      try {
        return await this.#issueQr(attemptId);
      } catch (error) {
        this.logger.warn({ err: { name: error.name, status: error.statusCode ?? null }, attempt_id: attemptId }, 'signal QR refresh failed');
      }
    }
    return attempt;
  }

  // -------------------------------------------------------------- outbound

  async sendMessage(..._args) {
    throw rejectOutboundMessage();
  }

  async logout(payload = {}) {
    this.stopped = true;
    await this.#stopLive();
    const result = await super.logout({
      ...payload,
      account_id: payload.account_id || this.liveAccount || this.defaultAccountId,
    });
    this.liveAccount = null;
    return result;
  }
}
