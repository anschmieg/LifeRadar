import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { mkdtempSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import test from 'node:test';

import { SignalConnector } from '../src/providers/signal.mjs';
import { SignalSidecarClient } from '../src/signal-client.mjs';

function makeLogger() {
  const entries = [];
  const logger = {
    entries,
    child: () => logger,
    warn: (...args) => entries.push(['warn', ...args]),
    error: (...args) => entries.push(['error', ...args]),
    info: (...args) => entries.push(['info', ...args]),
    debug: (...args) => entries.push(['debug', ...args]),
    allText: () => JSON.stringify(entries),
  };
  return logger;
}

function makeDb() {
  const store = new Map();
  return {
    accounts: [],
    ingested: [],
    checkpoints: [],
    async getConnectorAccounts() { return this.accounts; },
    async upsertConnectorAccount(account) { this.accounts.push(account); },
    async ingestSignalMessage(...args) { this.ingested.push(args); return { conversationId: 7, externalId: `${args[1]?.conversationId}:${args[1]?.messageId}` }; },
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
    ...overrides,
  };
}

const scheduled = [];

function makeConnector({ db, client, api, hasSession, schedule, random = () => 0.5, ...extra } = {}) {
  const sessionDir = mkdtempSync(path.join(os.tmpdir(), 'signal-test-'));
  const logger = extra.logger ?? makeLogger();
  const connector = new SignalConnector({
    db: db ?? makeDb(),
    logger,
    provider: 'signal',
    sessionDir,
    hasSession,
    createSignalClient: client !== undefined || api === undefined ? () => (client || makeClient()) : undefined,
    createSignalApi: api !== undefined ? () => api : undefined,
    schedule: schedule || ((fn, delay) => { scheduled.push({ fn, delay }); return scheduled.length; }),
    random,
    reconnectBaseMs: 100,
    reconnectMaxMs: 400,
    ...extra,
  });
  return { connector, sessionDir, logger };
}

function seedSession(sessionDir, account = '+4915112345678') {
  writeFileSync(path.join(sessionDir, 'signal.session'), JSON.stringify({ account, paired_at: '2026-10-01T00:00:00.000Z' }));
  return account;
}

function makeApi({ accounts = [], links = [] } = {}) {
  const state = { accounts, linkCalls: 0, links, received: [], closed: 0 };
  const api = {
    state,
    async getAccounts() { return state.accounts; },
    async getDeviceLinkUri(deviceName) {
      state.deviceNames = state.deviceNames || [];
      state.deviceNames.push(deviceName);
      return state.links[state.linkCalls++ % Math.max(1, state.links.length)] ?? 'sgnl://linkdevice?uuid=fallback';
    },
    openReceiveStream(account) {
      const listeners = new Map();
      state.received.push(account);
      return {
        events: {
          on(name, fn) { listeners.set(name, fn); },
          emit(name, payload) { listeners.get(name)?.(payload); },
        },
        close() { state.closed += 1; },
        listeners,
      };
    },
  };
  return api;
}

test('Signal connector is read-only by construction - no outbound/receipt surface exists', async () => {
  const { connector } = makeConnector({ db: makeDb(), schedule: () => 0 });
  let threw = false;
  try { await connector.sendMessage('+49123', 'hi'); } catch (error) { threw = true; assert.equal(error.statusCode, 403); }
  assert.equal(threw, true, 'sendMessage must reject outbound loudly');

  // Receipt/typing/presence/reaction mutations must not exist at all.
  for (const name of ['sendPresence', 'readReceipt', 'reaction', 'typing', 'markRead', 'reactTo', 'sendReceipt', 'sendReadReceipt', 'sendReaction', 'setTyping', 'updateProfile', 'unpair']) {
    assert.equal(typeof connector[name], 'undefined', name + ' must not exist on the connector');
  }

  // The sidecar client surface itself must be receive/pair-only.
  for (const name of ['sendMessage', 'sendReceipt', 'sendReadReceipt', 'sendTypingIndicator', 'sendReaction', 'setPresence', 'registerAccount', 'unregisterAccount', 'logout', 'unpair']) {
    assert.equal(typeof SignalSidecarClient.prototype[name], 'undefined', name + ' must not exist on SignalSidecarClient');
  }
});

test('Signal logout() is local-only teardown and clears the session marker', async () => {
  const db = makeDb();
  let endCalls = 0;
  const client = makeClient({ async end() { endCalls += 1; } });
  const { connector, sessionDir, logger } = makeConnector({ db, client, schedule: () => 0 });
  seedSession(sessionDir);
  await connector.start();
  assert.equal(endCalls, 0);
  await connector.logout({ account_id: 'sig-1' });
  assert.equal(endCalls, 1, 'local stream closed exactly once');
  assert.equal(db.accounts.at(-1)?.authState, 'logged_out');
  assert.equal(db.checkpoints.length, 0, 'no cursor churn from teardown');
  assert.equal(readdirSync(sessionDir).length, 0, 'signal.session removed on logout');
  assert.ok(!logger.allText().includes('signal.session'), 'logout never logs session file content');
});

test('Signal start() is session-driven: no_session without marker, connected with marker', async () => {
  const db = makeDb();
  const without = makeConnector({ db, schedule: () => 0 });
  const resultA = await without.connector.start();
  assert.equal(resultA.status, 'no_session');
  assert.equal(db.accounts.at(-1)?.authState, 'no_session');
  assert.equal(typeof resultA.account_id, 'undefined');

  const withSession = makeConnector({ db, schedule: () => 0 });
  const account = seedSession(withSession.sessionDir);
  const resultB = await withSession.connector.start();
  assert.equal(resultB.status, 'connected');
  assert.equal(resultB.account_id, account);
  const row = db.accounts.at(-1);
  assert.equal(row.authState, 'connected');
  assert.deepEqual(row.metadata.receipt_policy, { delivery: 'accepted', read: 'disabled' });
  const serialized = JSON.stringify(row);
  assert.ok(!serialized.includes('sgnl://'), 'no pairing material in account metadata');
});

test('Signal pairing: QR stays in-memory, refreshes only when stale, completes into a session', async () => {
  const db = makeDb();
  const api = makeApi({ links: ['sgnl://linkdevice?uuid=QRPOLLSECRETA', 'sgnl://linkdevice?uuid=QRPOLLSECRETB'] });
  const { connector, sessionDir, logger } = makeConnector({ db, api, schedule: () => 0 });

  const attempt = await connector.beginLogin({});
  assert.equal(attempt.state, 'awaiting_qr_scan');
  assert.equal(attempt.qr_text, 'sgnl://linkdevice?uuid=QRPOLLSECRETA');
  assert.match(attempt.qr_svg, /^<svg/);
  assert.equal(attempt.metadata.mode, 'qr');
  assert.equal(api.state.deviceNames.at(-1), connector.deviceName);

  // Not paired yet: fresh QR is returned unchanged (no regeneration churn).
  const still = await connector.submitLoginStep(attempt.attempt_id);
  assert.equal(still.state, 'awaiting_qr_scan');
  assert.equal(still.qr_text, 'sgnl://linkdevice?uuid=QRPOLLSECRETA');

  // Stale QR (older than refresh window) is regenerated.
  connector.attempts.get(attempt.attempt_id).metadata.qr_generated_at = Date.now() - 61_000;
  const refreshed = await connector.submitLoginStep(attempt.attempt_id);
  assert.equal(refreshed.qr_text, 'sgnl://linkdevice?uuid=QRPOLLSECRETB');
  assert.ok(refreshed.metadata.qr_generated_at >= Date.now() - 5000);

  // Sidecar now reports a linked account -> pairing completes.
  api.state.accounts = ['+4915112345678'];
  const completed = await connector.submitLoginStep(attempt.attempt_id);
  assert.equal(completed.state, 'completed');
  assert.equal(completed.account_id, '+4915112345678');
  assert.equal(completed.qr_text, null, 'QR cleared from attempt after pairing');
  assert.equal(completed.qr_svg, null);
  const session = JSON.parse(readFileSync(path.join(sessionDir, 'signal.session'), 'utf8'));
  assert.equal(session.account, '+4915112345678');
  assert.equal(db.accounts.at(-1)?.authState, 'connected');
  assert.equal(db.accounts.at(-1)?.metadata.read_only, true);
  assert.equal(api.state.received.at(-1), '+4915112345678', 'receive stream attached after pairing');

  // Nothing sensitive ever reached the logger or the DB rows.
  const dbText = JSON.stringify(db.accounts) + JSON.stringify(db.checkpoints);
  assert.ok(!logger.allText().includes('QRPOLLSECRET'), 'no QR material in logs');
  assert.ok(!logger.allText().includes('sgnl://'), 'no link URI in logs');
  assert.ok(!dbText.includes('QRPOLLSECRET'), 'no QR material in Postgres rows');
  assert.ok(!dbText.includes('sgnl://'), 'no link URI in Postgres rows');
  const mode = statSync(path.join(sessionDir, 'signal.session')).mode & 0o777;
  assert.equal(mode & 0o077, 0, 'session file not group/world readable');
});

test('Signal live ingest: message -> DB row + per-dialog cursor, failures never advance cursor', async () => {
  const db = makeDb();
  const client = makeClient();
  let failIngest = true;
  db.ingestSignalMessage = async (...args) => {
    if (failIngest) throw new Error('db down');
    db.ingested.push(args);
    return { conversationId: 7, externalId: `${args[1].conversationId}:${args[1].messageId}` };
  };
  const { connector, sessionDir } = makeConnector({ db, client, schedule: () => 0 });
  seedSession(sessionDir);
  await connector.start();
  const handler = client.listeners.get('message');
  assert.equal(typeof handler, 'function', 'live event surface attached');

  const event = {
    kind: 'message',
    conversationId: 'direct:+491234567890',
    messageId: '17123456789012',
    senderId: '+491234567890',
    senderLabel: 'Alice',
    occurredAt: 17123456789012,
    text: 'hello',
    isInbound: true,
  };
  await handler(event);
  assert.equal(db.ingested.length, 0);
  assert.equal(db.checkpoints.length, 0, 'cursor not advanced on failed ingest');
  failIngest = false;
  await handler(event);
  assert.equal(db.ingested.length, 1);
  assert.equal(db.ingested[0][0], '+4915112345678');
  assert.equal(db.ingested[0][1], event);
  assert.equal(db.checkpoints.length, 1, 'cursor advanced after successful ingest');
  assert.equal(db.checkpoints[0][2], 'dialog:direct:+491234567890');
  assert.equal(db.checkpoints[0][3].message_id, 'direct:+491234567890:17123456789012');
});

test('Signal delivery receipts are accepted silently; typing and unknown events are dropped', async () => {
  const db = makeDb();
  const client = makeClient();
  const { connector, sessionDir, logger } = makeConnector({ db, client, schedule: () => 0 });
  seedSession(sessionDir);
  await connector.start();
  const handler = client.listeners.get('message');

  // Delivery receipt: accepted, no DB write, no response of any kind.
  await handler({ kind: 'receipt', receiptType: 'delivery', timestamp: 1 });
  assert.equal(db.ingested.length, 0, 'delivery receipt not persisted as message');
  assert.equal(db.checkpoints.length, 0, 'delivery receipt does not move the cursor');

  // Inbound read receipt and typing: ignored entirely.
  await handler({ kind: 'receipt', receiptType: 'read' });
  await handler({ kind: 'typing' });
  await handler({ kind: 'ignore' });
  await handler(null);
  assert.equal(db.ingested.length, 0);
  assert.ok(logger.allText().includes('receipt accepted without response'));
  assert.ok(!logger.allText().includes('sgnl://'));
});

test('Signal connector is receive-only: no history/backfill surface exists', async () => {
  const { connector } = makeConnector({ db: makeDb(), schedule: () => 0 });
  for (const name of ['listConversations', 'listMessages', 'backfill', 'fetchHistory']) {
    assert.equal(typeof connector[name], 'undefined', name + ' must not exist (upside exposes no history GET route; history is not backfilled)');
  }
});

test('Signal connector is single-flight on reconnect with bounded jittered backoff', async () => {
  const db = makeDb();
  const sockets = [];
  const localTimers = [];
  let failBuild = false;
  const createSignalClient = async () => {
    const client = makeClient();
    sockets.push(client);
    if (failBuild) throw new Error('hard connect failure');
    return client;
  };
  const sessionDir = mkdtempSync(path.join(os.tmpdir(), 'signal-test-'));
  seedSession(sessionDir);
  const connector = new SignalConnector({
    db,
    logger: makeLogger(),
    provider: 'signal',
    sessionDir,
    hasSession: undefined,
    createSignalClient,
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

  const cap1 = Math.min(400, 100 * (2 ** 0));
  const cap2 = Math.min(400, 100 * (2 ** 1));
  assert.equal(firstTimerDelay, Math.round(cap1 * (0.5 + 0.5)));
  assert.equal(secondTimerDelay, Math.round(cap2 * (0.5 + 0.5)));

  // Logout mid-backoff: the armed timer must not resurrect the stream.
  failBuild = false;
  await connector.logout({});
  await localTimers[1].fn();
  assert.equal(sockets.length, 2, 'reconnect timer is inert after logout');
});

test('Signal ingestSignalMessage maps a normalized event into conversation + message rows', async () => {
  const { GatewayDb } = await import('../src/db.mjs');
  const calls = {};
  const fake = {
    async upsertConversation(payload) { calls.conversation = payload; return 42; },
    async upsertMessage(payload) { calls.message = payload; },
  };
  const event = {
    conversationId: 'group:AbCdEf',
    conversationTitle: 'Team',
    messageId: '17123456789012',
    senderId: '+49111',
    senderLabel: 'Bob',
    occurredAt: 17123456789012,
    text: 'yo',
    hasAttachment: false,
    expiresInSeconds: 0,
    isOwn: false,
    isInbound: true,
  };
  const row = await GatewayDb.prototype.ingestSignalMessage.call(fake, '+49999', event);
  assert.equal(calls.conversation.source, 'signal');
  assert.equal(calls.conversation.externalId, 'group:AbCdEf');
  assert.equal(calls.conversation.accountId, '+49999');
  assert.equal(calls.conversation.metadata.conversation_kind, 'group');
  assert.equal(calls.message.source, 'signal');
  assert.equal(calls.message.externalId, 'group:AbCdEf:17123456789012');
  assert.equal(calls.message.isInbound, true);
  assert.equal(calls.message.contentText, 'yo');
  assert.deepEqual(calls.message.provenance, { provider: 'signal', account_id: '+49999', message_id: '17123456789012' });
  assert.equal(row.conversationId, 42);
  assert.equal(row.externalId, 'group:AbCdEf:17123456789012');

  const noRow = await GatewayDb.prototype.ingestSignalMessage.call(fake, '+49999', { kind: 'receipt' });
  assert.equal(noRow, null, 'non-message events never reach storage');
});
