import { readFile, access } from 'node:fs/promises';
import path from 'node:path';

import { BaseConnector } from './base.mjs';
import { rejectOutboundMessage } from '../read-only.mjs';

// LifeRadar Signal connector - STRICTLY READ-ONLY.
//
// This connector drives a THIN LOCAL-ONLY socket wrapper exposed via the
// `createSignalClient` seam. The wrapper has read + teardown surfaces and
// nothing else: no outbound send, no presence/typing, no read receipts, no
// reactions, and no remote logout/unpair. logout() closes the local socket
// (end()-equivalent) only; it never instructs the remote device to drop the
// pairing. There is deliberately no real signal-cli REST send path wired in.

const DEFAULT_ACCOUNT_ID = 'default';

export class SignalConnector extends BaseConnector {
  constructor({
    db,
    logger,
    provider,
    sessionDir,
    hasSession = null,
    createSignalClient = null,
    schedule = null,
    random = Math.random,
    reconnectBaseMs = 1000,
    reconnectMaxMs = 30000,
    ...opts
  }) {
    super({ db, logger, provider, sessionDir, ...opts });
    this.sessionFile = path.join(sessionDir, 'signal.session');
    this.hasSession = hasSession || (() => this.#hasSession());
    this.createSignalClient = createSignalClient || (() => this.#buildLocalWrapper());
    this.schedule = schedule || ((fn, delay) => setTimeout(fn, delay));
    this.random = random;
    this.reconnectBaseMs = reconnectBaseMs;
    this.reconnectMaxMs = reconnectMaxMs;
    this.accountId = DEFAULT_ACCOUNT_ID;
    this.client = null;
    this.stopped = false;
    this.reconnectAttempt = 0;
    this.reconnectTimer = null;
    this.connecting = null;
  }

  async start() {
    await this.ensureDirectories();
    if (!(await this.hasSession())) {
      await this.db.upsertConnectorAccount({
        provider: this.provider, accountId: this.accountId, authState: 'logged_out',
        enabled: false, lastSyncedAt: null, metadata: { status: 'no_session' },
      });
      return { provider: this.provider, status: 'no_session' };
    }
    this.stopped = false;
    await this.db.upsertConnectorAccount({
      provider: this.provider, accountId: this.accountId, authState: 'connected',
      enabled: true, lastSyncedAt: new Date(),
      metadata: { paired_at: new Date().toISOString() },
    });
    await this.#connectLocal();
    await this.#backfill();
    return { provider: this.provider, status: 'connected', accountId: this.accountId };
  }

  async beginLogin() {
    await this.ensureDirectories();
    const attempt = this.createAttempt({
      state: 'initializing',
      prompt: 'Pairing a new Signal device. This connector is read-only: it registers a local session only.',
      fields: [],
      metadata: { read_only: true },
    });
    return this.getLoginAttempt(attempt.attempt_id);
  }

  async submitLoginStep(attemptId) {
    return this.getLoginAttempt(attemptId);
  }

  async sendMessage() {
    throw rejectOutboundMessage();
  }

  // Local-only teardown. We close the local socket wrapper (end()-equivalent)
  // and never instruct the remote device to drop its pairing. There is no
  // signal-cli unregister / unpair / remote logout call anywhere in this
  // connector: the read-only policy forbids it and the local wrapper has no
  // such surface.
  async logout({ account_id: accountId } = {}) {
    if (this.client) {
      try {
        await this.client.end();
      } catch {
        // ignore teardown errors; local state below is authoritative
      }
      this.client = null;
    }
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    return super.logout({ account_id: accountId || this.accountId });
  }

  async #connectLocal() {
    if (this.client) return this.client;
    if (this.connecting) return this.connecting;
    this.connecting = this.#buildLocal();
    try {
      return await this.connecting;
    } finally {
      this.connecting = null;
    }
  }

  async #buildLocal() {
    const client = await this.createSignalClient();
    // Attach the live inbound event surface (read-only ingestion only).
    client.ev?.on?.('message', (payload) => {
      this.#handleLiveMessage(payload).catch((error) => {
        this.logger.warn({ err: error }, 'Signal live handler contained error');
      });
    });
    client.ev?.on?.('connection.close', () => {
      this.client = null;
      if (!this.stopped) this.#scheduleReconnect();
    });
    this.client = client;
    return client;
  }

  #scheduleReconnect() {
    if (this.stopped || this.reconnectTimer || this.connecting) return;
    const cap = Math.min(this.reconnectMaxMs, this.reconnectBaseMs * (2 ** this.reconnectAttempt++));
    const delay = Math.round(cap * (0.5 + this.random()));
    this.reconnectTimer = this.schedule(async () => {
      this.reconnectTimer = null;
      if (this.stopped) return;
      try {
        await this.#connectLocal();
      } catch (error) {
        this.logger.warn({ err: error }, 'Signal reconnect failed');
      } finally {
        if (!this.stopped && !this.client) this.#scheduleReconnect();
      }
    }, delay);
  }

  async #handleLiveMessage(payload) {
    try {
      const message = payload?.message || payload || {};
      if (!message.message_id && !message.id) return;
      await this.db.ingestSignalMessage(this.accountId, message);
      const conversation = message.remoteConversation || 'inbox';
      await this.db.setCheckpoint(this.provider, this.accountId, "dialog:" + conversation, {
        message_id: Number(message.message_id || message.id || 0),
        updated_at: new Date().toISOString(),
      });
      await this.db.upsertConnectorAccount({
        provider: this.provider, accountId: this.accountId, authState: 'connected',
        enabled: true, lastSyncedAt: new Date(),
        metadata: { last_live_at: new Date().toISOString() },
      });
    } catch (error) {
      // The checkpoint only advances after the ingest succeeded.
      this.logger.error({ err: error }, 'Signal live ingest failed; checkpoint unchanged');
    }
  }

  async #hasSession() {
    try {
      await access(this.sessionFile);
      return true;
    } catch {
      return false;
    }
  }

  // The default local-only wrapper: an in-process sandbox sink that can only
  // be closed. It has no network identity and no outbound capability. If a
  // real signal-cli-backed client is injected via the seam it is still used
  // strictly for the read/listen/close surfaces above.
  #buildLocalWrapper() {
    const listeners = new Map();
    return {
      ev: { on(name, fn) { listeners.set(name, fn); } },
      listeners,
      on(name, fn) { listeners.set(name, fn); },
      async listConversations() { return []; },
      async listMessages() { return []; },
      user: null,
      async end() { listeners.clear(); },
    };
  }

  // Durable per-page backfill: iterate every conversation, page through its
  // messages, ingest each page, then persist a per-dialog high-water
  // checkpoint AFTER the page ingested. A crash mid-loop therefore resumes
  // from the last persisted page and never replays the start of the dialog.
  async #backfill() {
    const conversations = (await this.client?.listConversations?.()) || [];
    for (const conversation of conversations) {
      const externalId = String(conversation.id ?? conversation.externalId ?? '');
      if (!externalId) continue;
      const key = 'dialog:' + externalId;
      const prev = await this.db.getCheckpoint(this.provider, this.accountId, key);
      const minMessageId = Number(prev?.message_id || 0);
      let pageMinId = minMessageId;
      let highWater = minMessageId;
      let stop = false;
      while (!stop) {
        const messages = (await this.client?.listMessages?.(conversation, { minTimestamp: pageMinId })) || [];
        for (const message of messages) {
          const messageId = Number(message.id ?? message.message_id ?? message.timestamp ?? 0);
          if (messageId > highWater) highWater = messageId;
          await this.db.ingestSignalMessage(this.accountId, conversation, message);
        }
        await this.db.setCheckpoint(this.provider, this.accountId, key, {
          message_id: highWater,
          dialog_id: externalId,
          updated_at: new Date().toISOString(),
        });
        if (messages.length === 0) stop = true;
      }
    }
  }
}
