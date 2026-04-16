import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

import QRCode from 'qrcode';

import { BaseConnector } from './base.mjs';

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
  constructor(opts) {
    super(opts);
    this.client = null;
    this.sessionFile = path.join(this.sessionDir, 'gramjs.session');
    this.codeHints = new Map();
    this.qrClients = new Map();
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
    const { SignIn } = await import('telegram/tl/functions/auth/index.js');

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
          new SignIn({
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

  async sendMessage({ externalId, contentText }) {
    const client = await this.#ensureAuthorizedClient();
    const entity = await client.getInputEntity(externalId);
    const message = await client.sendMessage(entity, { message: contentText });
    return {
      status: 'sent',
      message_id: `${externalId}:${message.id}`,
    };
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

    if (result.className === 'auth.loginTokenMigrateTo') {
      if (typeof client._switchDC === 'function') {
        await client._switchDC(result.dcId);
      }
      result = await client.invoke(new Api.auth.ImportLoginToken({ token: result.token }));
    }

    if (result.className === 'auth.loginTokenSuccess') {
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

    if (result.className === 'auth.loginToken') {
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
      error: 'Could not generate Telegram QR login token.',
    });
  }

  async #ensureAuthorizedClient() {
    if (this.client) return this.client;
    const sessionValue = await this.#readSession();
    if (!sessionValue) {
      const error = new Error('Telegram is not logged in');
      error.statusCode = 409;
      throw error;
    }

    const { TelegramClient } = await import('telegram');
    const { StringSession } = await import('telegram/sessions/index.js');
    this.client = new TelegramClient(
      new StringSession(sessionValue),
      Number.parseInt(requireEnv('TELEGRAM_API_ID'), 10),
      requireEnv('TELEGRAM_API_HASH'),
      { connectionRetries: 5 }
    );
    await this.client.connect();
    return this.client;
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

  async #backfill(accountId) {
    const client = await this.#ensureAuthorizedClient();
    const me = await client.getMe();
    const limitPerChat = Number.parseInt(process.env.LIFE_RADAR_CONNECTOR_BACKFILL_LIMIT_PER_CHAT || '2000', 10);
    const dialogs = await client.getDialogs({ limit: 100 });

    for (const dialog of dialogs) {
      let remaining = limitPerChat;
      let offsetId = 0;
      while (remaining > 0) {
        const pageSize = Math.min(remaining, 100);
        const messages = await client.getMessages(dialog.entity, { limit: pageSize, offsetId });
        if (!messages?.length) break;
        for (const message of messages.reverse()) {
          await this.db.ingestTelegramMessage(accountId, dialog, message, me?.id);
          offsetId = Math.max(offsetId, message.id);
        }
        remaining -= messages.length;
        if (messages.length < pageSize) break;
      }
      await this.db.setCheckpoint(this.provider, accountId, `dialog:${dialog.id}`, {
        imported_message_limit: limitPerChat - remaining,
        updated_at: new Date().toISOString(),
      });
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
