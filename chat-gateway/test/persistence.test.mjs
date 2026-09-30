import assert from 'node:assert/strict';
import test from 'node:test';

import { GatewayDb } from '../src/db.mjs';
import { TelegramConnector } from '../src/providers/telegram.mjs';
import { WhatsAppConnector } from '../src/providers/whatsapp.mjs';

const logger = { child: () => logger, warn() {}, error() {}, info() {} };

function fakeDb() {
  return {
    accounts: [],
    ingested: [],
    checkpoints: [],
    async upsertConnectorAccount(account) { this.accounts.push(account); },
    async ingestTelegramMessage(...args) { this.ingested.push(args); },
    async ingestWhatsAppMessage(...args) { this.ingested.push(args); },
    async ingestWhatsAppChat() {},
    async setCheckpoint(...args) { this.checkpoints.push(args); },
    async getConnectorAccounts() { return []; },
  };
}

test('account updates preserve prior sync and error fields when omitted', async () => {
  let captured;
  await GatewayDb.prototype.upsertConnectorAccount.call({
    query: async (sql, params) => { captured = { sql, params }; },
  }, {
    provider: 'telegram', accountId: '42', authState: 'connected', metadata: {},
  });

  assert.match(captured.sql, /last_synced_at = case when \$10 then excluded\.last_synced_at else life_radar\.connector_accounts\.last_synced_at end/);
  assert.match(captured.sql, /last_error = case when \$12 then excluded\.last_error else life_radar\.connector_accounts\.last_error end/);
  assert.deepEqual(captured.params.slice(9), [false, false, false]);
});

test('Telegram startup restores a saved session, backfills, and listens for live messages without login', async () => {
  const db = fakeDb();
  const handlers = [];
  const client = {
    async connect() {},
    async getMe() { return { id: '42' }; },
    async getDialogs() { return []; },
    addEventHandler(handler) { handlers.push(handler); },
  };
  const connector = new TelegramConnector({
    db, logger, provider: 'telegram', sessionDir: '/unused',
    readSession: async () => 'saved-session',
    createAuthorizedClient: async (session) => { assert.equal(session, 'saved-session'); return client; },
    newMessageEvent: 'new-message-filter',
  });

  const result = await connector.start();
  assert.equal(result.status, 'connected');
  assert.equal(handlers.length, 1);
  await handlers[0]({ message: { id: 7, peerId: { userId: 99 }, date: new Date(), message: 'hello', senderId: 99, out: false } });
  assert.equal(db.ingested.length, 1);
  assert.equal(db.ingested[0][0], '42');
  assert.equal(db.checkpoints[0][2], 'live_cursor');
  assert.deepEqual(db.checkpoints[0][3], { message_id: 7, peer_id: '99' });
});

test('Telegram backfill resumes each dialog from its durable checkpoint', async () => {
  const db = fakeDb();
  db.getCheckpoint = async () => ({ message_id: 12 });
  const requests = [];
  const client = {
    async connect() {}, async getMe() { return { id: '42' }; },
    async getDialogs() { return [{ id: '99', entity: {}, title: 'chat' }]; },
    async getMessages(_entity, options) { requests.push(options); return []; },
    addEventHandler() {},
  };
  const connector = new TelegramConnector({ db, logger, provider: 'telegram', sessionDir: '/unused', readSession: async () => 'saved', createAuthorizedClient: async () => client, newMessageEvent: 'event' });

  await connector.start();
  assert.equal(requests[0].minId, 12);
});

test('WhatsApp startup restores paired credentials and reconnects after close with bounded jittered delay', async () => {
  const db = fakeDb();
  const sockets = [];
  const timers = [];
  const makeSocket = () => {
    const listeners = new Map();
    const socket = { user: { id: 'me@s.whatsapp.net', name: 'Me' }, ev: { on(name, fn) { listeners.set(name, fn); } }, listeners };
    sockets.push(socket);
    return socket;
  };
  const connector = new WhatsAppConnector({
    db, logger, provider: 'whatsapp', sessionDir: '/unused', unofficialAllowed: true,
    hasSession: async () => true,
    createSocket: async () => makeSocket(),
    schedule: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
    random: () => 0.5,
    reconnectBaseMs: 100,
    reconnectMaxMs: 500,
  });

  await connector.start();
  assert.equal(sockets.length, 1);
  await sockets[0].listeners.get('connection.update')({ connection: 'close', lastDisconnect: { error: new Error('closed') } });
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 100);
  await timers[0].fn();
  assert.equal(sockets.length, 2);
});
