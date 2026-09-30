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
        await this.socket.logout();
      } catch {
        // ignore logout errors; session cleanup below is authoritative
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
    if (this.stopped || this.reconnectTimer) return;
    const cap = Math.min(this.reconnectMaxMs, this.reconnectBaseMs * (2 ** this.reconnectAttempt++));
    const delay = Math.round(cap * (0.5 + this.random()));
    this.reconnectTimer = this.schedule(async () => {
      this.reconnectTimer = null;
      if (!this.stopped) {
        try { await this.#connectSocket(); } catch (error) { this.logger.warn({ err: error }, 'WhatsApp reconnect failed'); this.#scheduleReconnect(); }
      }
    }, delay);
  }

  async #connectSocket(attemptId = null) {
    if (this.socket) return this.socket;
    let socket;
    let saveCreds = () => {};
    if (this.createSocket) {
      socket = await this.createSocket();
    } else {
      const baileys = await import('@whiskeysockets/baileys');
      const authDir = path.join(this.sessionDir, 'auth');
      const auth = await baileys.useMultiFileAuthState(authDir);
      saveCreds = auth.saveCreds;
      socket = baileys.default({ auth: auth.state, printQRInTerminal: false, browser: ['LifeRadar', 'Chrome', '1.0'], syncFullHistory: true, markOnlineOnConnect: false });
    }

    socket.ev.on('creds.update', saveCreds);
    socket.ev.on('connection.update', async (update) => {
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

      if (update.connection === 'close') {
        const disconnectError = update.lastDisconnect?.error;
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
        this.socket = null;
        this.#scheduleReconnect();
      }
    });

    socket.ev.on('chats.upsert', async (chats) => {
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
    });

    socket.ev.on('messaging-history.set', async ({ chats = [], messages = [] }) => {
      for (const chat of chats) {
        await this.db.ingestWhatsAppChat(this.accountId, chat);
      }
      for (const message of messages) {
        await this.db.ingestWhatsAppMessage(this.accountId, message);
      }
      const newest = messages.reduce((latest, message) => !latest || Number(message.messageTimestamp || 0) > Number(latest.messageTimestamp || 0) ? message : latest, null);
      await this.db.setCheckpoint(this.provider, this.accountId, 'history_sync', {
        latest_message_id: newest?.key?.id || null,
        latest_remote_jid: newest?.key?.remoteJid || null,
        latest_timestamp: newest?.messageTimestamp ? Number(newest.messageTimestamp) : null,
        updated_at: new Date().toISOString(),
      });
    });

    socket.ev.on('messages.upsert', async ({ messages = [] }) => {
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
    });

    this.socket = socket;
    return socket;
  }
}
