import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

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

export class TelegramConnector extends BaseConnector {
  constructor(opts) {
    super(opts);
    this.client = null;
    this.sessionFile = path.join(this.sessionDir, 'gramjs.session');
    this.passwordHints = new Map();
  }

  async beginLogin() {
    await this.ensureDirectories();
    const attempt = this.createAttempt({
      state: 'awaiting_phone',
      prompt: 'Enter the phone number for the Telegram personal account.',
      fields: ['phone_number'],
      metadata: { qr_supported: false },
    });
    return attempt;
  }

  async submitLoginStep(attemptId, body) {
    const attempt = await this.getLoginAttempt(attemptId);
    const phoneNumber = body.phone_number || attempt.metadata.phone_number || null;
    const code = body.code || null;
    const password = body.password || null;

    const { TelegramClient } = await import('telegram');
    const { StringSession } = await import('telegram/sessions/index.js');

    const apiId = Number.parseInt(requireEnv('TELEGRAM_API_ID'), 10);
    const apiHash = requireEnv('TELEGRAM_API_HASH');
    const existingSession = await this.#readSession();
    const session = new StringSession(existingSession);
    this.client = new TelegramClient(session, apiId, apiHash, { connectionRetries: 5 });
    await this.client.connect();

    if (attempt.state === 'awaiting_phone') {
      if (!phoneNumber) {
        const error = new Error('phone_number is required');
        error.statusCode = 400;
        throw error;
      }
      const sent = await this.client.sendCode(
        { apiId, apiHash },
        phoneNumber
      );
      this.passwordHints.set(attemptId, {
        phone_number: phoneNumber,
        phone_code_hash: sent.phoneCodeHash,
      });
      return this.updateAttempt(attemptId, {
        state: 'awaiting_code',
        prompt: 'Enter the Telegram login code.',
        fields: ['code'],
        metadata: { ...attempt.metadata, phone_number: phoneNumber },
      });
    }

    if (attempt.state === 'awaiting_code') {
      if (!code) {
        const error = new Error('code is required');
        error.statusCode = 400;
        throw error;
      }
      const auth = this.passwordHints.get(attemptId);
      try {
        const result = await this.client.invoke(
          new (await import('telegram/tl/functions/auth/index.js')).SignIn({
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
        });
      } catch (error) {
        if (String(error?.errorMessage || error?.message || '').includes('SESSION_PASSWORD_NEEDED')) {
          return this.updateAttempt(attemptId, {
            state: 'awaiting_password',
            prompt: 'Telegram requires the account password.',
            fields: ['password'],
            error: null,
          });
        }
        await this.db.upsertConnectorAccount({
          provider: this.provider,
          accountId: this.defaultAccountId,
          authState: 'error',
          lastError: error.message,
          lastErrorAt: new Date(),
          metadata: { phase: 'code' },
        });
        throw error;
      }
    }

    if (attempt.state === 'awaiting_password') {
      if (!password) {
        const error = new Error('password is required');
        error.statusCode = 400;
        throw error;
      }
      const { CheckPassword } = await import('telegram/tl/functions/auth/index.js');
      const { computeCheck } = await import('telegram/Password.js');
      const passwordInfo = await this.client.getPassword();
      const result = await this.client.invoke(
        new CheckPassword({ password: await computeCheck(passwordInfo, password) })
      );
      await this.#finishAuthorization(result.user ?? null, attemptId);
      return this.updateAttempt(attemptId, {
        state: 'completed',
        prompt: null,
        fields: [],
        account_id: String(result.user?.id ?? this.defaultAccountId),
      });
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
    return super.logout({ account_id: accountId || this.defaultAccountId });
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
    this.passwordHints.delete(attemptId);
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
