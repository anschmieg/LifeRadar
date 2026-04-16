import path from 'node:path';

import QRCode from 'qrcode';

import { BaseConnector } from './base.mjs';

export class WhatsAppConnector extends BaseConnector {
  constructor({ unofficialAllowed, ...opts }) {
    super(opts);
    this.unofficialAllowed = unofficialAllowed;
    this.socket = null;
    this.accountId = this.defaultAccountId;
    this.qrState = null;
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

  async sendMessage({ externalId, contentText }) {
    const socket = await this.#requireSocket();
    const response = await socket.sendMessage(externalId, { text: contentText });
    return {
      status: 'sent',
      message_id: `${externalId}:${response?.key?.id ?? Date.now()}`,
    };
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

  async #connectSocket(attemptId = null) {
    if (this.socket) return this.socket;
    const baileys = await import('@whiskeysockets/baileys');
    const authDir = path.join(this.sessionDir, 'auth');
    const { state, saveCreds } = await baileys.useMultiFileAuthState(authDir);
    const socket = baileys.default({
      auth: state,
      printQRInTerminal: false,
      browser: ['LifeRadar', 'Chrome', '1.0'],
      syncFullHistory: true,
      markOnlineOnConnect: false,
    });

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

      if (update.connection === 'close' && update.lastDisconnect?.error) {
        await this.db.upsertConnectorAccount({
          provider: this.provider,
          accountId: this.accountId,
          authState: 'error',
          enabled: true,
          lastError: update.lastDisconnect.error.message || 'connection closed',
          lastErrorAt: new Date(),
          metadata: {
            disconnect_reason: update.lastDisconnect.error.output?.statusCode ?? null,
          },
        });
        this.socket = null;
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
      await this.db.setCheckpoint(this.provider, this.accountId, 'history_sync', {
        chats: chats.length,
        messages: messages.length,
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
