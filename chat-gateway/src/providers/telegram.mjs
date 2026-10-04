import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

import QRCode from 'qrcode';

import { BaseConnector } from './base.mjs';
import { rejectOutboundMessage } from '../read-only.mjs';

function requireEnv(name) {
  const value = process.env[name];
  if (!value) {
    const error = new Error(`Missing required env: ${name}`);
    error.statusCode = 400;
    throw error;
  }
  return value;
}

function toBase64Url(buffer) {
  return Buffer.from(buffer)
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/g, '');
}

export class TelegramConnector extends BaseConnector {
  constructor({ readSession, createAuthorizedClient, newMessageEvent, ...opts }) {
    super(opts);
    this.client = null;
    this.sessionFile = path.join(this.sessionDir, 'gramjs.session');
    this.codeHints = new Map();
    this.qrClients = new Map();
    this.readSession = readSession || (() => this.#readSession());
    this.createAuthorizedClient = createAuthorizedClient || ((session) => this.#newAuthorizedClient(session));
    this.newMessageEvent = newMessageEvent;
  }

  async start() {
    const sessionValue = await this.readSession();
    if (!sessionValue) return { provider: this.provider, status: 'no_session' };
    const client = await this.#ensureAuthorizedClient();
    const me = await client.getMe();
    const accountId = String(me?.id ?? this.defaultAccountId);
    await this.db.upsertConnectorAccount({ provider: this.provider, accountId, authState: 'connected', enabled: true, metadata: { restored_at: new Date().toISOString() } });
    await this.#backfill(accountId, me);
    await this.#startLiveUpdates(accountId, me);
    return { provider: this.provider, status: 'connected', accountId };
  }

  async beginLogin(body = {}) {
    await this.ensureDirectories();
    const mode = body.mode === 'code' ? 'code' : 'qr';
    if (mode === 'qr') {
      const attempt = this.createAttempt({
        state: 'initializing',
        prompt: 'Generating Telegram QR login…',
        fields: [],
        metadata: { qr_supported: true, mode },
      });
      await this.#ensureQrAttempt(attempt.attempt_id);
      return this.getLoginAttempt(attempt.attempt_id);
    }

    return this.createAttempt({
      state: 'awaiting_phone',
      prompt: 'Enter the phone number for the Telegram account.',
      fields: ['phone_number'],
      metadata: { qr_supported: true, mode },
    });
  }

  async getLoginAttempt(attemptId) {
    const attempt = await super.getLoginAttempt(attemptId);
    if (attempt.metadata?.mode === 'qr' && !['completed', 'failed', 'error'].includes(attempt.state)) {
      return this.#pollQrAttempt(attemptId);
    }
    return attempt;
  }

  async submitLoginStep(attemptId, body) {
    const attempt = await super.getLoginAttempt(attemptId);
    if ((body.mode || attempt.metadata?.mode || 'qr') === 'qr') {
      return this.getLoginAttempt(attemptId);
    }

    const phoneNumber = body.phone_number || attempt.metadata.phone_number || null;
    const code = body.code || null;
    const client = await this.#createCodeClient();
    // gramjs 2.26.x has no `tl/functions/auth` module; auth classes hang off
    // the top-level `Api` namespace (verified: `Api.auth.SignIn` is a function).
    const { Api } = await import('telegram');

    if (attempt.state === 'awaiting_phone') {
      if (!phoneNumber) {
        const error = new Error('phone_number is required');
        error.statusCode = 400;
        throw error;
      }
      const apiId = Number.parseInt(requireEnv('TELEGRAM_API_ID'), 10);
      const apiHash = requireEnv('TELEGRAM_API_HASH');
      const sent = await client.sendCode({ apiId, apiHash }, phoneNumber);
      this.codeHints.set(attemptId, {
        phone_number: phoneNumber,
        phone_code_hash: sent.phoneCodeHash,
      });
      return this.updateAttempt(attemptId, {
        state: 'awaiting_code',
        prompt: 'Enter the Telegram confirmation code.',
        fields: ['code'],
        metadata: { ...attempt.metadata, phone_number: phoneNumber, mode: 'code' },
        error: null,
      });
    }

    if (attempt.state === 'awaiting_code') {
      if (!code) {
        const error = new Error('code is required');
        error.statusCode = 400;
        throw error;
      }
      const auth = this.codeHints.get(attemptId);
      if (!auth) {
        const error = new Error('Login code session expired. Start again.');
        error.statusCode = 409;
        throw error;
      }
      try {
        const result = await client.invoke(
          new Api.auth.SignIn({
            phoneNumber: auth.phone_number,
            phoneCodeHash: auth.phone_code_hash,
            phoneCode: code,
          })
        );
        await this.#finishAuthorization(result.user ?? null, attemptId);
        return this.updateAttempt(attemptId, {
          state: 'completed',
          prompt: null,
          fields: [],
          account_id: String(result.user?.id ?? this.defaultAccountId),
          error: null,
        });
      } catch (error) {
        const message = String(error?.errorMessage || error?.message || error);
        if (message.includes('SESSION_PASSWORD_NEEDED')) {
          return this.updateAttempt(attemptId, {
            state: 'error',
            prompt: null,
            fields: [],
            error: 'This Telegram account requires 2FA for phone-code login. Use QR login instead.',
          });
        }
        throw error;
      }
    }

    return attempt;
  }

  async sendMessage() {
    throw rejectOutboundMessage();
  }

  async logout({ account_id: accountId } = {}) {
    if (this.client) {
      await this.client.disconnect();
      this.client = null;
    }
    for (const qrClient of this.qrClients.values()) {
      try {
        await qrClient.disconnect();
      } catch {
        // ignore cleanup errors
      }
    }
    this.qrClients.clear();
    this.codeHints.clear();
    return super.logout({ account_id: accountId || this.defaultAccountId });
  }

  async #createCodeClient() {
    const { TelegramClient } = await import('telegram');
    const { StringSession } = await import('telegram/sessions/index.js');
    const apiId = Number.parseInt(requireEnv('TELEGRAM_API_ID'), 10);
    const apiHash = requireEnv('TELEGRAM_API_HASH');
    const client = new TelegramClient(new StringSession(''), apiId, apiHash, {
      connectionRetries: 5,
    });
    await client.connect();
    this.client = client;
    return client;
  }

  async #ensureQrAttempt(attemptId) {
    if (this.qrClients.has(attemptId)) return this.qrClients.get(attemptId);
    const { TelegramClient, Api } = await import('telegram');
    const { StringSession } = await import('telegram/sessions/index.js');
    const apiId = Number.parseInt(requireEnv('TELEGRAM_API_ID'), 10);
    const apiHash = requireEnv('TELEGRAM_API_HASH');
    const client = new TelegramClient(new StringSession(''), apiId, apiHash, {
      connectionRetries: 5,
    });
    await client.connect();
    this.qrClients.set(attemptId, client);
    await this.#refreshQrToken(attemptId, client, Api);
    return client;
  }

  async #pollQrAttempt(attemptId) {
    const attempt = await super.getLoginAttempt(attemptId);
    const client = await this.#ensureQrAttempt(attemptId);
    const { Api } = await import('telegram');
    return this.#refreshQrToken(attemptId, client, Api);
  }

  async #refreshQrToken(attemptId, client, Api) {
    const apiId = Number.parseInt(requireEnv('TELEGRAM_API_ID'), 10);
    const apiHash = requireEnv('TELEGRAM_API_HASH');

    let result = await client.invoke(
      new Api.auth.ExportLoginToken({
        apiId,
        apiHash,
        exceptIds: [],
      })
    );

    // gramjs 2.26 names classNames in PascalCase (`auth.LoginToken`), while the
    // TL schema uses snake_case (`auth.loginToken`). Compare case-insensitively.
    const typeOf = (r) => String(r.className || '').toLowerCase();

    // An api_id registered on another DC returns LoginTokenMigrateTo; after
    // switching, ImportLoginToken can itself hand back another hop. Follow the
    // chain (bounded) instead of only handling a single hop.
    for (let hop = 0; hop < 4 && typeOf(result) === 'auth.logintokenmigrateto'; hop++) {
      if (typeof client._switchDC !== 'function') break;
      await client._switchDC(result.dcId);
      result = await client.invoke(new Api.auth.ImportLoginToken({ token: result.token }));
    }

    if (typeOf(result) === 'auth.logintokensuccess') {
      this.client = client;
      await this.#finishAuthorization(result.authorization?.user ?? null, attemptId);
      this.qrClients.delete(attemptId);
      return this.updateAttempt(attemptId, {
        state: 'completed',
        prompt: null,
        fields: [],
        qr_text: null,
        qr_svg: null,
        account_id: String(result.authorization?.user?.id ?? this.defaultAccountId),
        error: null,
      });
    }

    if (typeOf(result) === 'auth.logintoken') {
      const token = toBase64Url(result.token);
      const qrText = `tg://login?token=${token}`;
      const qrSvg = await QRCode.toString(qrText, { type: 'svg', margin: 1 });
      return this.updateAttempt(attemptId, {
        state: 'awaiting_qr_scan',
        prompt: 'Scan this QR code in Telegram: Settings → Devices → Link Desktop Device.',
        fields: [],
        qr_text: qrText,
        qr_svg: qrSvg,
        metadata: {
          ...(this.attempts.get(attemptId)?.metadata || {}),
          mode: 'qr',
          qr_supported: true,
        },
        error: null,
      });
    }

    return this.updateAttempt(attemptId, {
      state: 'error',
      error: `Could not generate Telegram QR login token (unexpected response: ${result.className}). Try again or use phone + code.`,
    });
  }

  async #ensureAuthorizedClient() {
    if (this.client) return this.client;
    const sessionValue = await this.readSession();
    if (!sessionValue) {
      const error = new Error('Telegram is not logged in');
      error.statusCode = 409;
      throw error;
    }

    this.client = await this.createAuthorizedClient(sessionValue);
    if (typeof this.client.connect === 'function') await this.client.connect();
    return this.client;
  }

  async #newAuthorizedClient(sessionValue) {
    const { TelegramClient } = await import('telegram');
    const { StringSession } = await import('telegram/sessions/index.js');
    return new TelegramClient(new StringSession(sessionValue), Number.parseInt(requireEnv('TELEGRAM_API_ID'), 10), requireEnv('TELEGRAM_API_HASH'), { connectionRetries: 5 });
  }

  async #startLiveUpdates(accountId, me) {
    const client = await this.#ensureAuthorizedClient();
    let NewMessage = this.newMessageEvent;
    if (!NewMessage) {
      const events = await import('telegram/events/index.js');
      NewMessage = new events.NewMessage({});
    }
    client.addEventHandler(async (event) => {
      const message = event.message;
      const peerId = message?.peerId?.channelId || message?.peerId?.chatId || message?.peerId?.userId;
      if (!message?.id || peerId == null) return;
      let entity = null;
      if (typeof client.getEntity === 'function') {
        try {
          entity = await client.getEntity(peerId);
        } catch {
          entity = null;
        }
      }
      const dialog = entity
        ? { id: String(entity.id ?? peerId), entity, title: entity.title || entity.username || String(peerId) }
        : { id: String(peerId), entity: {}, title: String(peerId) };
      try {
        await this.db.ingestTelegramMessage(accountId, dialog, message, me?.id);
        await this.db.setCheckpoint(this.provider, accountId, 'live_cursor', { message_id: message.id, peer_id: String(peerId) });
        await this.db.upsertConnectorAccount({ provider: this.provider, accountId, authState: 'connected', enabled: true, lastSyncedAt: new Date(), metadata: { live_update_at: new Date().toISOString() } });
      } catch (error) {
        this.logger.error({ err: error }, 'Telegram live ingest failed; checkpoint unchanged');
      }
    }, NewMessage);
  }

  async #finishAuthorization(user, attemptId) {
    const sessionValue = this.client.session.save();
    await mkdir(this.sessionDir, { recursive: true });
    await writeFile(this.sessionFile, String(sessionValue), 'utf8');
    const accountId = String(user?.id ?? this.defaultAccountId);
    await this.db.upsertConnectorAccount({
      provider: this.provider,
      accountId,
      displayLabel: user?.username || [user?.firstName, user?.lastName].filter(Boolean).join(' ') || null,
      authState: 'connected',
      enabled: true,
      lastSyncedAt: new Date(),
      lastError: null,
      lastErrorAt: null,
      metadata: {
        username: user?.username || null,
        phone: user?.phone || null,
      },
    });
    await this.#backfill(accountId);
    this.codeHints.delete(attemptId);
  }

  async #backfill(accountId, me = null) {
    const client = await this.#ensureAuthorizedClient();
    me ||= await client.getMe();
    const limitPerChat = Number.parseInt(process.env.LIFERADAR_CONNECTOR_BACKFILL_LIMIT_PER_CHAT || '2000', 10);
    const dialogs = await client.getDialogs({ limit: 100 });

    for (const dialog of dialogs) {
      const checkpoint = await this.db.getCheckpoint(this.provider, accountId, `dialog:${dialog.id}`);
      const minId = Number(checkpoint?.message_id || 0);
      let remaining = limitPerChat;
      let offsetId = 0;
      let newestId = minId;
      while (remaining > 0) {
        const pageSize = Math.min(remaining, 100);
        const messages = await client.getMessages(dialog.entity, { limit: pageSize, offsetId, minId });
        if (!messages?.length) break;
        for (const message of messages.reverse()) {
          await this.db.ingestTelegramMessage(accountId, dialog, message, me?.id);
          newestId = Math.max(newestId, message.id);
        }
        remaining -= messages.length;
        offsetId = Math.min(...messages.map((message) => message.id));
        // Persist the per-dialog high-water mark after EVERY page so a crash
        // mid-loop resumes from the last persisted cursor and never regresses.
        const persisted = Math.max(minId, newestId);
        await this.db.setCheckpoint(this.provider, accountId, `dialog:${dialog.id}`, {
          message_id: persisted,
          dialog_id: String(dialog.id),
          updated_at: new Date().toISOString(),
        });
        if (messages.length < pageSize) break;
      }
    }

    await this.db.upsertConnectorAccount({
      provider: this.provider,
      accountId,
      authState: 'connected',
      enabled: true,
      lastSyncedAt: new Date(),
      metadata: { backfill_completed_at: new Date().toISOString() },
    });
  }

  async #readSession() {
    try {
      return (await readFile(this.sessionFile, 'utf8')).trim();
    } catch {
      return '';
    }
  }
}
