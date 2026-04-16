import { mkdir, rm } from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';

export class BaseConnector {
  constructor({ db, logger, provider, sessionDir }) {
    this.db = db;
    this.logger = logger.child({ provider });
    this.provider = provider;
    this.sessionDir = sessionDir;
    this.attempts = new Map();
    this.defaultAccountId = 'default';
  }

  async ensureDirectories() {
    await mkdir(this.sessionDir, { recursive: true });
  }

  createAttempt(payload = {}) {
    const attemptId = crypto.randomUUID();
    const attempt = {
      attempt_id: attemptId,
      provider: this.provider,
      state: payload.state || 'pending',
      prompt: payload.prompt || null,
      auth_url: payload.auth_url || null,
      qr_text: payload.qr_text || null,
      qr_svg: payload.qr_svg || null,
      fields: payload.fields || [],
      account_id: payload.account_id || this.defaultAccountId,
      error: payload.error || null,
      metadata: payload.metadata || {},
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    this.attempts.set(attemptId, attempt);
    return attempt;
  }

  updateAttempt(attemptId, patch) {
    const attempt = this.attempts.get(attemptId);
    if (!attempt) {
      const error = new Error(`Unknown login attempt '${attemptId}'`);
      error.statusCode = 404;
      throw error;
    }
    Object.assign(attempt, patch, { updated_at: new Date().toISOString() });
    return attempt;
  }

  async getLoginAttempt(attemptId) {
    return this.updateAttempt(attemptId, {});
  }

  async clearSessions() {
    await rm(this.sessionDir, { recursive: true, force: true });
    await mkdir(this.sessionDir, { recursive: true });
  }

  async getStatus() {
    const accounts = await this.db.getConnectorAccounts(this.provider);
    return {
      provider: this.provider,
      enabled: true,
      accounts,
    };
  }

  async beginLogin() {
    throw new Error('beginLogin not implemented');
  }

  async submitLoginStep() {
    throw new Error('submitLoginStep not implemented');
  }

  async logout({ account_id: accountId } = {}) {
    await this.clearSessions();
    await this.db.upsertConnectorAccount({
      provider: this.provider,
      accountId: accountId || this.defaultAccountId,
      authState: 'logged_out',
      enabled: true,
      lastError: null,
      lastErrorAt: null,
      metadata: {},
    });
    return { provider: this.provider, status: 'logged_out' };
  }
}
