import path from 'node:path';

import QRCode from 'qrcode';

import { BaseConnector } from './base.mjs';
import { rejectOutboundMessage } from '../read-only.mjs';

export class WhatsAppConnector extends BaseConnector {
  constructor({ unofficialAllowed, hasSession, createSocket, schedule, random = Math.random, reconnectBaseMs = 1_000, reconnectMaxMs = 30_000, ...opts }) {
    super(opts);
    this.unofficialAllowed = unofficialAllowed;
    this.socket = null;
    this.accountId = this.defaultAccountId;
    this.qrState = null;
    this.hasSession = hasSession || (() => this.#hasSession());
    this.createSocket = createSocket;
    this.schedule = schedule || ((fn, delay) => setTimeout(fn, delay));
    this.random = random;
    this.reconnectBaseMs = reconnectBaseMs;
    this.reconnectMaxMs = reconnectMaxMs;
    this.reconnectAttempt = 0;
    this.reconnectTimer = null;
    this.stopped = false;
  }

  async start() {
    if (!this.unofficialAllowed || !(await this.hasSession())) return { provider: this.provider, status: 'no_session' };
    this.stopped = false;
    await this.#connectSocket();
    if (typeof this.db.repairWhatsappMediaLabels === 'function') {
      try {
        const fixed = await this.db.repairWhatsappMediaLabels();
        if (fixed) this.logger.info({ fixed }, 'repaired blank WhatsApp media labels');
      } catch (error) {
        this.logger.warn({ err: error }, 'WhatsApp media label repair failed; continuing');
      }
    }
    return { provider: this.provider, status: 'connecting' };
  }

  async beginLogin() {
    if (!this.unofficialAllowed) {
      const error = new Error('WhatsApp consumer sessions are disabled by configuration');
      error.statusCode = 403;
      throw error;
    }
    await this.ensureDirectories();
    const attempt = this.createAttempt({
      state: 'initializing',
      prompt: 'Scan the QR code with WhatsApp on your phone.',
      fields: [],
      metadata: { qr_supported: true },
    });
    await this.#connectSocket(attempt.attempt_id);
    return await this.getLoginAttempt(attempt.attempt_id);
  }

  async submitLoginStep(attemptId) {
    return this.getLoginAttempt(attemptId);
  }

  async sendMessage() {
    throw rejectOutboundMessage();
  }

  async logout({ account_id: accountId } = {}) {
    if (this.socket) {
      try {
        // Local-only teardown. `socket.logout()` on Baileys performs a REMOTE
        // unpair (remove-companion-device) — a provider mutation that our
        // read-only policy forbids. `end()` simply closes the socket and
        // keeps the persisted (pairing) session intact for the next start.
        await this.socket.end();
      } catch {
        // ignore end errors; session cleanup below is authoritative
      }
      this.socket = null;
    }
    this.qrState = null;
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    return super.logout({ account_id: accountId || this.accountId });
  }

  async #requireSocket() {
    if (this.socket?.user) return this.socket;
    await this.#connectSocket();
    if (!this.socket?.user) {
      const error = new Error('WhatsApp is not yet paired');
      error.statusCode = 409;
      throw error;
    }
    return this.socket;
  }

  async #hasSession() {
    try {
      const { access } = await import('node:fs/promises');
      await access(path.join(this.sessionDir, 'auth', 'creds.json'));
      return true;
    } catch { return false; }
  }

  #scheduleReconnect() {
    if (this.stopped || this.reconnectTimer || this.connecting) return;
    const cap = Math.min(this.reconnectMaxMs, this.reconnectBaseMs * (2 ** this.reconnectAttempt++));
    const delay = Math.round(cap * (0.5 + this.random()));
    this.reconnectTimer = this.schedule(async () => {
      try {
        if (this.stopped) return;
        await this.#connectSocket();
      } catch (error) {
        this.logger.warn({ err: error }, 'WhatsApp reconnect failed');
      } finally {
        this.reconnectTimer = null;
        // Re-arm a bounded retry after a hard failure even when no further
        // close event fires (Baileys removes the connection.update listener on
        // close), and only when we did not end up with a live socket.
        if (!this.stopped && !this.socket) this.#scheduleReconnect();
      }
    }, delay);
  }

  async #connectSocket(attemptId = null) {
    if (this.socket) return this.socket;
    // Single-flight: concurrent close events must not stack socket builds.
    if (this.connecting) return this.connecting;
    this.connecting = this.#buildSocket(attemptId);
    try {
      return await this.connecting;
    } finally {
      this.connecting = null;
    }
  }

  async #buildSocket(attemptId = null) {
    let socket;
    let saveCreds = () => {};
    if (this.createSocket) {
      socket = await this.createSocket();
    } else {
      const baileys = await import('@whiskeysockets/baileys');
      const authDir = path.join(this.sessionDir, 'auth');
      const auth = await baileys.useMultiFileAuthState(authDir);
      saveCreds = auth.saveCreds;
      socket = baileys.default({
        auth: auth.state,
        printQRInTerminal: false,
        browser: ['LifeRadar', 'Chrome', '1.0'],
        // History sync is server-initiated; we do NOT force a full bootstrap
        // re-download on every reconnect. The durable per-remoteJid cursor
        // below makes any re-delivered history incremental (deduped), so
        // reconnects only ingest messages newer than the persisted cursor.
        shouldSyncHistoryMessage: () => true,
        markOnlineOnConnect: false,
      });
    }

    socket.ev.on('creds.update', saveCreds);
    socket.ev.on('connection.update', async (update) => {
      try {
        if (update.qr) {
          const qrSvg = await QRCode.toString(update.qr, { type: 'svg', margin: 1 });
          this.qrState = { qr_text: update.qr, qr_svg: qrSvg };
          if (attemptId && this.attempts.has(attemptId)) {
            this.updateAttempt(attemptId, {
              state: 'awaiting_qr_scan',
              qr_text: update.qr,
              qr_svg: qrSvg,
              prompt: 'Scan the QR code with WhatsApp on your phone.',
            });
          }
        }

        if (update.connection === 'open') {
          this.reconnectAttempt = 0;
          this.accountId = socket.user?.id || this.defaultAccountId;
          await this.db.upsertConnectorAccount({
            provider: this.provider,
            accountId: this.accountId,
            displayLabel: socket.user?.name || 'WhatsApp',
            authState: 'connected',
            enabled: true,
            lastSyncedAt: new Date(),
            // Deliberate clear of prior errors on successful connect.
            lastError: null,
            lastErrorAt: null,
            metadata: {
              jid: socket.user?.id || null,
              paired_at: new Date().toISOString(),
            },
          });
          if (attemptId && this.attempts.has(attemptId)) {
            this.updateAttempt(attemptId, {
              state: 'completed',
              qr_text: null,
              qr_svg: null,
              prompt: null,
              account_id: this.accountId,
            });
          }
        }
      } catch (error) {
        this.logger.error({ err: error }, 'WhatsApp connection.update handling failed');
      }

      if (update.connection === 'close') {
        const disconnectError = update.lastDisconnect?.error;
        try {
          await this.db.upsertConnectorAccount({
            provider: this.provider,
            accountId: this.accountId,
            authState: 'error',
            enabled: true,
            lastError: disconnectError?.message || 'connection closed',
            lastErrorAt: new Date(),
            metadata: {
              disconnect_reason: disconnectError?.output?.statusCode ?? null,
            },
          });
        } catch (error) {
          this.logger.error({ err: error }, 'WhatsApp close persistence failed');
        }
        this.socket = null;
        this.#scheduleReconnect();
      }
    });

    socket.ev.on('chats.upsert', async (chats) => {
      try {
        for (const chat of chats || []) {
          await this.db.ingestWhatsAppChat(this.accountId, chat);
        }
        await this.db.upsertConnectorAccount({
          provider: this.provider,
          accountId: this.accountId,
          authState: 'connected',
          enabled: true,
          lastSyncedAt: new Date(),
          metadata: { last_chats_upsert_at: new Date().toISOString() },
        });
      } catch (error) {
        this.logger.error({ err: error }, 'WhatsApp chats.upsert ingest failed');
      }
    });

    socket.ev.on('messaging-history.set', async ({ chats = [], messages = [] } = {}) => {
      try {
        const prev = await this.db.getCheckpoint(this.provider, this.accountId, 'history_sync');
        const dialogs = { ...(prev?.dialogs || {}) };

        // Incremental: only ingest messages newer than the durable per-jid
        // high-water cursor, so re-delivered history after a reconnect does
        // not duplicate rows.
        const toIngest = [];
        for (const message of messages) {
          const jid = message?.key?.remoteJid;
          const ts = Number(message.messageTimestamp || 0);
          if (!jid || ts <= (dialogs[jid]?.max_timestamp || 0)) continue;
          toIngest.push(message);
        }

        for (const chat of chats) {
          await this.db.ingestWhatsAppChat(this.accountId, chat);
        }
        for (const message of toIngest) {
          await this.db.ingestWhatsAppMessage(this.accountId, message);
        }

        // The history_sync checkpoint must NOT advance unless persistence
        // succeeded — this only runs after every ingest above resolved.
        for (const message of messages) {
          const jid = message?.key?.remoteJid;
          const ts = Number(message.messageTimestamp || 0);
          if (!jid) continue;
          const previous = dialogs[jid]?.max_timestamp || 0;
          dialogs[jid] = { max_timestamp: Math.max(previous, ts), updated_at: new Date().toISOString() };
        }
        await this.db.setCheckpoint(this.provider, this.accountId, 'history_sync', {
          dialogs,
          updated_at: new Date().toISOString(),
        });
      } catch (error) {
        this.logger.error({ err: error }, 'WhatsApp history sync failed; checkpoint unchanged');
      }
    });

    socket.ev.on('messages.upsert', async ({ messages = [] } = {}) => {
      try {
        for (const message of messages) {
          await this.db.ingestWhatsAppMessage(this.accountId, message);
        }
        await this.db.upsertConnectorAccount({
          provider: this.provider,
          accountId: this.accountId,
          authState: 'connected',
          enabled: true,
          lastSyncedAt: new Date(),
          metadata: { last_message_upsert_at: new Date().toISOString() },
        });
      } catch (error) {
        this.logger.error({ err: error }, 'WhatsApp messages.upsert ingest failed');
      }
    });

    this.socket = socket;
    return socket;
  }
}
