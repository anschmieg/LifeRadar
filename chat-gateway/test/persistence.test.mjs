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
    async getCheckpoint() { return null; },
    async getConnectorAccounts() { return []; },
  };
}

function makeWhatsAppSocket() {
  const listeners = new Map();
  const socket = { user: { id: 'me@s.whatsapp.net', name: 'Me' }, ev: { on(name, fn) { listeners.set(name, fn); } }, listeners };
  return socket;
}

function telegramClient(overrides = {}) {
  return {
    async connect() {},
    async getMe() { return { id: '42' }; },
    async getDialogs() { return []; },
    addEventHandler() {},
    ...overrides,
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

test('Telegram backfill persists per-page checkpoint and resumes after crash without refetching', async () => {
  const db = fakeDb();
  const store = {};
  db.getCheckpoint = async (_p, _a, key) => store[key] ?? null;
  db.setCheckpoint = async (_p, _a, key, value) => { store[key] = value; db.checkpoints.push([_p, _a, key, value]); };

  const page1 = Array.from({ length: 100 }, (_, i) => ({ id: 300 - i }));
  let firstCalls = 0;
  const client1 = telegramClient({
    async getDialogs() { return [{ id: '99', entity: {}, title: 'chat' }]; },
    async getMessages() {
      firstCalls += 1;
      if (firstCalls === 1) return page1;
      throw new Error('simulated crash mid-backfill');
    },
  });
  const connector1 = new TelegramConnector({
    db, logger, provider: 'telegram', sessionDir: '/unused',
    readSession: async () => 'saved',
    createAuthorizedClient: async () => client1,
    newMessageEvent: 'event',
  });

  await assert.rejects(() => connector1.start(), /simulated crash/);

  // The per-page checkpoint must have been persisted BEFORE the crash on page 2.
  assert.ok(store['dialog:99'], 'per-page checkpoint persisted before crash');
  assert.equal(store['dialog:99'].message_id, 300);
  assert.equal(db.ingested.length, 100);

  // Restart: must resume from the persisted high-water mark and NOT refetch page 1.
  const minIds = [];
  const client2 = telegramClient({
    async getDialogs() { return [{ id: '99', entity: {}, title: 'chat' }]; },
    async getMessages(_entity, options) { minIds.push(options.minId); return [{ id: 305 }, { id: 302 }]; },
  });
  const connector2 = new TelegramConnector({
    db, logger, provider: 'telegram', sessionDir: '/unused',
    readSession: async () => 'saved',
    createAuthorizedClient: async () => client2,
    newMessageEvent: 'event',
  });

  await connector2.start();
  assert.equal(minIds[0], 300, 'resumes from last persisted high-water mark');
  assert.equal(db.ingested.length, 102, 'does not refetch already-ingested messages');
  assert.equal(store['dialog:99'].message_id, 305);
});

test('Telegram backfill checkpoint never decreases across restarts even on gap/empty pages', async () => {
  const db = fakeDb();
  const store = { 'dialog:99': { message_id: 50, dialog_id: '99' } };
  db.getCheckpoint = async (_p, _a, key) => store[key] ?? null;
  db.setCheckpoint = async (_p, _a, key, value) => { store[key] = value; db.checkpoints.push([_p, _a, key, value]); };

  // Empty page: nothing ingested, cursor must not regress.
  const client1 = telegramClient({
    async getDialogs() { return [{ id: '99', entity: {}, title: 'chat' }]; },
    async getMessages() { return []; },
  });
  const connector1 = new TelegramConnector({
    db, logger, provider: 'telegram', sessionDir: '/unused',
    readSession: async () => 'saved',
    createAuthorizedClient: async () => client1,
    newMessageEvent: 'event',
  });
  await connector1.start();
  assert.equal(store['dialog:99'].message_id, 50, 'empty page does not regress checkpoint');

  // Gap page returns messages below the stored high-water mark.
  const client2 = telegramClient({
    async getDialogs() { return [{ id: '99', entity: {}, title: 'chat' }]; },
    async getMessages() { return [{ id: 45 }, { id: 40 }]; },
  });
  const connector2 = new TelegramConnector({
    db, logger, provider: 'telegram', sessionDir: '/unused',
    readSession: async () => 'saved',
    createAuthorizedClient: async () => client2,
    newMessageEvent: 'event',
  });
  await connector2.start();
  assert.equal(store['dialog:99'].message_id, 50, 'gap below checkpoint never regresses');
  assert.equal(db.ingested.length, 2);
});

test('Telegram live handler contains DB errors, keeps cursor, resolves real peer entity', async () => {
  const db = fakeDb();
  let failIngest = true;
  db.ingestTelegramMessage = async (...args) => {
    if (failIngest) throw new Error('db down');
    db.ingested.push(args);
  };
  const handlers = [];
  const client = telegramClient({
    addEventHandler(handler) { handlers.push(handler); },
    async getEntity(id) { return { id, title: 'Real Chat', username: 'real' }; },
  });
  const connector = new TelegramConnector({
    db, logger, provider: 'telegram', sessionDir: '/unused',
    readSession: async () => 'saved',
    createAuthorizedClient: async () => client,
    newMessageEvent: 'event',
  });
  await connector.start();
  assert.equal(handlers.length, 1);

  // DB throws on ingest → handler must catch, not advance the cursor, not crash.
  await handlers[0]({ message: { id: 7, peerId: { userId: 99 }, date: new Date(), message: 'hello', senderId: 99, out: false } });
  assert.equal(db.checkpoints.length, 0, 'live_cursor not advanced on failed ingest');
  assert.equal(db.ingested.length, 0);

  // A later successful delivery re-ingests and advances the cursor.
  failIngest = false;
  await handlers[0]({ message: { id: 8, peerId: { userId: 99 }, date: new Date(), message: 'hello2', senderId: 99, out: false } });
  assert.equal(db.ingested.length, 1);
  assert.equal(db.checkpoints.length, 1);
  assert.equal(db.checkpoints[0][3].message_id, 8);
  assert.equal(db.ingested[0][1].title, 'Real Chat', 'real peer entity resolved instead of synthetic dialog');
});

test('WhatsApp live handlers contain DB errors without advancing checkpoint or crashing', async () => {
  const db = fakeDb();
  let failIngest = true;
  db.ingestWhatsAppMessage = async (...args) => {
    if (failIngest) throw new Error('db down');
    db.ingested.push(args);
  };
  db.ingestWhatsAppChat = async () => {
    if (failIngest) throw new Error('db down');
  };
  const socket = makeWhatsAppSocket();
  const connector = new WhatsAppConnector({
    db, logger, provider: 'whatsapp', sessionDir: '/unused', unofficialAllowed: true,
    hasSession: async () => true,
    createSocket: async () => socket,
    schedule: () => { throw new Error('no reconnect expected'); },
    random: () => 0.5,
  });
  await connector.start();
  assert.ok(socket.listeners.has('chats.upsert'));
  assert.ok(socket.listeners.has('messaging-history.set'));
  assert.ok(socket.listeners.has('messages.upsert'));

  // messages.upsert with a throwing DB → handler catches, no checkpoint advance, no unhandled rejection.
  await socket.listeners.get('messages.upsert')({ messages: [{ key: { id: 'm1', remoteJid: 'jid' }, messageTimestamp: 10 }] });
  assert.equal(db.ingested.length, 0);
  assert.equal(db.checkpoints.length, 0);
  assert.equal(db.accounts.length, 0, 'lastSyncedAt not bumped on failed ingest');

  // messaging-history.set with a throwing DB → checkpoint must not advance.
  await socket.listeners.get('messaging-history.set')({ chats: [], messages: [{ key: { id: 'h1', remoteJid: 'jid' }, messageTimestamp: 20 }] });
  assert.equal(db.checkpoints.length, 0, 'history_sync checkpoint not advanced on failed ingest');

  // chats.upsert with a throwing DB → caught too.
  await socket.listeners.get('chats.upsert')([{ id: 'jid', name: 'Chat' }]);
  assert.equal(db.accounts.length, 0);
});

test('WhatsApp history replay after reconnect does not duplicate rows', async () => {
  const db = fakeDb();
  const store = {};
  db.getCheckpoint = async (_p, _a, key) => store[key] ?? null;
  db.setCheckpoint = async (_p, _a, key, value) => { store[key] = value; db.checkpoints.push([_p, _a, key, value]); };
  const socket = makeWhatsAppSocket();
  const connector = new WhatsAppConnector({
    db, logger, provider: 'whatsapp', sessionDir: '/unused', unofficialAllowed: true,
    hasSession: async () => true,
    createSocket: async () => socket,
    schedule: () => { throw new Error('no reconnect expected'); },
    random: () => 0.5,
  });
  await connector.start();

  const historySet = {
    chats: [{ id: 'jid', name: 'Chat' }],
    messages: [
      { key: { id: 'm1', remoteJid: 'jid' }, messageTimestamp: 10 },
      { key: { id: 'm2', remoteJid: 'jid' }, messageTimestamp: 20 },
    ],
  };
  // Initial history sync → both messages ingested, durable per-jid cursor persisted.
  await socket.listeners.get('messaging-history.set')(historySet);
  assert.equal(db.ingested.length, 2);
  assert.equal(db.checkpoints.length, 1);
  assert.ok(store.history_sync?.dialogs?.['jid'], 'durable per-remoteJid cursor persisted');
  assert.equal(store.history_sync.dialogs.jid.max_timestamp, 20);

  // Reconnect replay delivers the same messages again → no duplicate rows.
  await socket.listeners.get('messaging-history.set')(historySet);
  assert.equal(db.ingested.length, 2, 'replay does not duplicate rows');
  assert.equal(db.checkpoints.length, 2, 'checkpoint re-persisted idempotently');
  assert.equal(store.history_sync.dialogs.jid.max_timestamp, 20, 'cursor stays at high-water');
});

test('WhatsApp concurrent closes do not stack socket builds', async () => {
  const db = fakeDb();
  const sockets = [];
  const timers = [];
  let builds = 0;
  let gateResolve;
  const createSocket = async () => {
    builds += 1;
    const socket = makeWhatsAppSocket();
    sockets.push(socket);
    if (builds === 1) return socket;
    await new Promise((r) => { gateResolve = r; });
    return socket;
  };
  const connector = new WhatsAppConnector({
    db, logger, provider: 'whatsapp', sessionDir: '/unused', unofficialAllowed: true,
    hasSession: async () => true,
    createSocket,
    schedule: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
    random: () => 0.5,
    reconnectBaseMs: 100,
    reconnectMaxMs: 500,
  });

  await connector.start();
  assert.equal(sockets.length, 1);
  await sockets[0].listeners.get('connection.update')({ connection: 'close' });
  assert.equal(timers.length, 1);

  // Fire the reconnect timer; the connect is in-flight (blocked on gate).
  const reconnectPromise = timers[0].fn();
  // A second close fires while the first reconnect is still building.
  await sockets[0].listeners.get('connection.update')({ connection: 'close' });
  gateResolve();
  await reconnectPromise;

  assert.equal(sockets.length, 2, 'exactly one reconnect socket built');
  assert.equal(timers.length, 1, 'no stacked reconnect timer');
});

test('WhatsApp hard connect failure re-arms a bounded retry without stacking', async () => {
  const db = fakeDb();
  const sockets = [];
  const timers = [];
  let builds = 0;
  let failBuilds = false;
  let gateResolve;
  const createSocket = async () => {
    builds += 1;
    if (builds === 1) { const s = makeWhatsAppSocket(); sockets.push(s); return s; }
    await new Promise((r) => { gateResolve = r; });
    if (failBuilds) throw new Error('hard connect failure');
    const s = makeWhatsAppSocket();
    sockets.push(s);
    return s;
  };
  const connector = new WhatsAppConnector({
    db, logger, provider: 'whatsapp', sessionDir: '/unused', unofficialAllowed: true,
    hasSession: async () => true,
    createSocket,
    schedule: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
    random: () => 0.5,
    reconnectBaseMs: 100,
    reconnectMaxMs: 500,
  });

  await connector.start();
  assert.equal(sockets.length, 1);
  await sockets[0].listeners.get('connection.update')({ connection: 'close' });
  assert.equal(timers.length, 1);

  // Make the next connect fail hard while a concurrent close also fires.
  failBuilds = true;
  const attempt1 = timers[0].fn();
  await sockets[0].listeners.get('connection.update')({ connection: 'close' });
  gateResolve();
  await attempt1;

  // Exactly one bounded retry re-armed (no stacking), backoff doubled and capped.
  assert.equal(timers.length, 2, 'bounded retry re-armed exactly once');
  assert.equal(timers[1].delay, 200);
  assert.equal(sockets.length, 1, 'failed build created no socket');

  // Retry succeeds and creates the next socket.
  failBuilds = false;
  const attempt2 = timers[1].fn();
  gateResolve(); // release build 3's gate (armed synchronously on the retry)
  await attempt2;
  assert.equal(sockets.length, 2);
});

test('upsertConnectorAccount: explicit null clears error fields, preserves labels; undefined preserves everything', async () => {
  const run = (opts) => {
    let captured;
    GatewayDb.prototype.upsertConnectorAccount.call({ query: async (sql, params) => { captured = { sql, params }; } }, opts);
    return captured;
  };

  // Explicit null clears error fields, preserves display_label when omitted.
  const cleared = run({ provider: 'telegram', accountId: '42', authState: 'connected', enabled: true, lastError: null, lastErrorAt: null, metadata: { x: 1 } });
  assert.equal(cleared.params[10], true, 'hasLastErrorAt flag true on explicit null');
  assert.equal(cleared.params[11], true, 'hasLastError flag true on explicit null');
  assert.equal(cleared.params[6], null, 'last_error_at param null');
  assert.equal(cleared.params[7], null, 'last_error param null');
  assert.match(cleared.sql, /on conflict \(provider, account_id\)/);

  // Undefined preserves everything (flags false, params null but not applied).
  const preserved = run({ provider: 'telegram', accountId: '42', authState: 'connected', enabled: true, metadata: { x: 2 } });
  assert.equal(preserved.params[9], false, 'hasLastSyncedAt flag false on undefined');
  assert.equal(preserved.params[10], false, 'hasLastErrorAt flag false on undefined');
  assert.equal(preserved.params[11], false, 'hasLastError flag false on undefined');

  // display_label semantics: explicit null label should be preserved on conflict via coalesce.
  const labelPreserved = run({ provider: 'telegram', accountId: '42', displayLabel: null, authState: 'connected', enabled: true, metadata: {} });
  assert.equal(labelPreserved.params[2], null);
  assert.match(labelPreserved.sql, /display_label = coalesce\(excluded\.display_label, life_radar\.connector_accounts\.display_label\)/);
});

test('Telegram backfill fails loudly when db.getCheckpoint is missing', async () => {
  const db = fakeDb();
  delete db.getCheckpoint;
  const client = telegramClient({
    async getDialogs() { return [{ id: '99', entity: {}, title: 'chat' }]; },
    async getMessages() { return [{ id: 1 }]; },
  });
  const connector = new TelegramConnector({
    db, logger, provider: 'telegram', sessionDir: '/unused',
    readSession: async () => 'saved',
    createAuthorizedClient: async () => client,
    newMessageEvent: 'event',
  });

  await assert.rejects(() => connector.start(), TypeError, 'missing getCheckpoint throws, not silently swallowed');
});
