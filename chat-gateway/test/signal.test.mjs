import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { mkdtempSync } from 'node:fs';
import test from 'node:test';

import { SignalConnector } from '../src/providers/signal.mjs';

const logger = { child: () => logger, warn() {}, error() {}, info() {}, debug() {} };

function makeDb() {
  const store = new Map();
  return {
    accounts: [],
    ingested: [],
    checkpoints: [],
    async getConnectorAccounts() { return this.accounts; },
    async upsertConnectorAccount(account) { this.accounts.push(account); },
    async ingestSignalMessage(...args) { this.ingested.push(args); },
    async getCheckpoint(_p, _a, key) { return store.get(key) ?? null; },
    async setCheckpoint(_p, _a, key, value) { store.set(key, value); this.checkpoints.push([_p, _a, key, value]); },
  };
}

function makeClient(overrides = {}) {
  const listeners = new Map();
  return {
    listeners,
    ev: { on(name, fn) { listeners.set(name, fn); } },
    async end() {},
    async listConversations() { return []; },
    async listMessages() { return []; },
    ...overrides,
  };
}

function makeConnector({ db, client, hasSession = () => true, schedule, random = () => 0.5, ...extra }) {
  return new SignalConnector({
    db,
    logger,
    provider: 'signal',
    sessionDir: mkdtempSync(path.join(os.tmpdir(), 'signal-test-')),
    hasSession,
    createSignalClient: () => (client || makeClient()),
    schedule: schedule || ((fn, delay) => { timers.push({ fn, delay }); return timers.length; }),
    random,
    reconnectBaseMs: 100,
    reconnectMaxMs: 1000,
    ...extra,
  });
}

const timers = [];

test('Signal connector is read-only by construction - outbound methods reject loudly', async () => {
  const connector = makeConnector({ db: makeDb(), schedule: () => 0 });
  // The connector must have NO outbound method that performs a real remote
  // mutation. Any send-like method is allowed to exist only as a loud
  // rejector that throws rejectOutboundMessage.
  for (const name of ['sendMessage', 'sendPresence', 'readReceipt', 'reaction']) {
    if (name in connector) {
      let threw = false;
      try { await connector[name](); } catch { threw = true; }
      assert.equal(threw, true, name + ' must reject outbound loudly');
    } else {
      assert.ok(true, name + ' absent (equally read-only)');
    }
  }
  // Presence/typing/receipt/reaction must not exist as real mutation paths.
  const absent = ['sendPresence', 'readReceipt', 'reaction', 'typing', 'markRead', 'reactTo'];
  for (const name of absent) {
    assert.equal(typeof connector[name], 'undefined', name + ' must not exist');
  }
  // The thin local wrapper exposes only read/listen/teardown - never remote
  // logout/unpair/send.
  const client = makeClient();
  assert.equal(typeof client.sendMessage, 'undefined');
  assert.equal(typeof client.logout, 'undefined');
  assert.equal(typeof client.unpair, 'undefined');
  assert.equal(typeof client.end, 'function');
});

test('Signal logout() is local-only teardown - never touches the remote pair', async () => {
  const db = makeDb();
  let endCalls = 0;
  const client = makeClient({ async end() { endCalls += 1; } });
  const connector = makeConnector({ db, client, schedule: () => 0 });
  await connector.start();
  await connector.logout({ account_id: 'sig-1' });
  assert.equal(endCalls, 1, 'local socket closed exactly once');
  assert.equal(db.accounts.at(-1)?.authState, 'logged_out');
  assert.equal(db.checkpoints.length, 0, 'no cursor churn from teardown');
});

test('Signal backfill persists a per-page checkpoint and resumes mid-dialog without replay', async () => {
  const db = makeDb();
  const pagesSeen = [];
  const firstClient = makeClient({
    async listConversations() { return [{ id: '99', title: 'chat' }]; },
    async listMessages(_conv, opts) {
      pagesSeen.push(opts);
      if (pagesSeen.length === 1) return [{ id: 25, timestamp: 101 }];
      return [];
    },
  });
  const connector = makeConnector({ db, client: firstClient, createSignalClient: () => firstClient });
  await connector.start();
  assert.equal(db.ingested.length, 1);
  assert.ok(db.checkpoints.length >= 1, 'per-page checkpoint persisted');
  assert.equal(db.checkpoints.at(-1)[2], 'dialog:99');
  assert.equal(db.checkpoints.at(-1)[3].message_id, 25);
  // Fresh connector, same durable store: resume from cursor, no replay.
  const connector2 = makeConnector({ db, createSignalClient: () => makeClient() });
  await connector2.start();
  assert.equal(db.ingested.length, 1, 'no replay of persisted page');
});

test('Signal live handler contains DB errors and never advances the cursor', async () => {
  const db = makeDb();
  let failIngest = true;
  db.ingestSignalMessage = async (...args) => {
    if (failIngest) throw new Error('db down');
    db.ingested.push(args);
  };
  const client = makeClient();
  const connector = makeConnector({ db, client, schedule: () => 0 });
  await connector.start();
  const handler = client.listeners.get('message');
  assert.equal(typeof handler, 'function', 'live event surface attached');
  await handler({ message: { id: 42, timestamp: 100 } });
  assert.equal(db.ingested.length, 0);
  assert.equal(db.checkpoints.length, 0, 'cursor not advanced on failed ingest');
  failIngest = false;
  await handler({ message: { id: 44, timestamp: 200 } });
  assert.equal(db.ingested.length, 1);
  assert.ok(db.checkpoints.length >= 1, 'cursor advanced after successful ingest');
});

test('Signal connector is single-flight on reconnect with bounded jittered backoff', async () => {
  const db = makeDb();
  const sockets = [];
  const localTimers = [];
  let failBuild = false;
  const createClient = async () => {
    const client = makeClient();
    sockets.push(client);
    if (sockets.length === 1) return client;
    if (failBuild) throw new Error('hard connect failure');
    return client;
  };
  const connector = new SignalConnector({
    db, logger, provider: 'signal', sessionDir: mkdtempSync(path.join(os.tmpdir(), 'signal-test-')),
    hasSession: () => true,
    createSignalClient: createClient,
    schedule: (fn, delay) => { localTimers.push({ fn, delay }); return localTimers.length; },
    random: () => 0.5,
    reconnectBaseMs: 100,
    reconnectMaxMs: 400,
  });
  await connector.start();
  assert.equal(sockets.length, 1);
  // Single-flight: concurrent close events must not stack socket builds.
  await sockets[0].listeners.get('connection.close')();
  await sockets[0].listeners.get('connection.close')();
  assert.equal(localTimers.length, 1, 'only one reconnect timer scheduled (single-flight)');

  // Failed reconnect re-arms a bounded retry with jittered backoff.
  failBuild = true;
  const firstTimerDelay = localTimers[0].delay;
  await localTimers[0].fn();
  assert.ok(sockets.length >= 2, 'reconnect attempt ran');
  assert.equal(localTimers.length, 2, 'failed reconnect re-armed a bounded retry');
  const secondTimerDelay = localTimers[1].delay;
  assert.ok(secondTimerDelay >= 50 && secondTimerDelay <= 400, 'backoff within bounds: ' + secondTimerDelay);

  // Bounded backoff grows (attempt 0 -> cap 100, attempt 1 -> cap 200, with
  // jitter 0.5+0.5 = 1.0 => delay equals the cap, capped at reconnectMaxMs).
  const cap1 = Math.min(400, 100 * (2 ** 0));
  const cap2 = Math.min(400, 100 * (2 ** 1));
  assert.equal(firstTimerDelay, Math.round(cap1 * (0.5 + 0.5)));
  assert.equal(secondTimerDelay, Math.round(cap2 * (0.5 + 0.5)));
});
