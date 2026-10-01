import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeSignalEvent, SignalSidecarClient } from '../src/signal-client.mjs';

test('normalizeSignalEvent: direct message carries stable external id and sender labels', () => {
  const event = normalizeSignalEvent({
    method: 'receiveMessage',
    params: {
      account: '+4915112345678',
      envelope: {
        source: '+491234567890',
        sourceNumber: '+491234567890',
        sourceName: 'Alice',
        timestamp: 17123456789012,
        dataMessage: { timestamp: 17123456789012, message: 'hello there', expiresInSeconds: 60, attachments: [] },
      },
    },
  }, '+4915112345678');
  assert.equal(event.kind, 'message');
  assert.equal(event.conversationId, 'direct:+491234567890');
  assert.equal(event.messageId, '17123456789012');
  assert.equal(event.senderId, '+491234567890');
  assert.equal(event.senderLabel, 'Alice');
  assert.equal(event.text, 'hello there');
  assert.equal(event.isInbound, true);
  assert.equal(event.expiresInSeconds, 60);
});

test('normalizeSignalEvent: group message maps to a group conversation', () => {
  const event = normalizeSignalEvent({
    method: 'receiveMessage',
    params: {
      envelope: {
        sourceNumber: '+491234567890',
        sourceName: 'Bob',
        timestamp: 5,
        dataMessage: { timestamp: 5, message: 'hi team', groupV2Id: 'group.abc123' },
      },
    },
  }, '+4915112345678');
  assert.equal(event.kind, 'message');
  assert.equal(event.conversationId, 'group:group.abc123');
  assert.equal(event.conversationTitle, 'Bob');
});

test('normalizeSignalEvent: own messages are flagged outbound and not marked inbound', () => {
  const event = normalizeSignalEvent({
    method: 'receiveMessage',
    params: {
      envelope: { sourceNumber: '+4915112345678', timestamp: 7, dataMessage: { timestamp: 7, message: 'sent from phone' } },
    },
  }, { account: '+4915112345678' });
  assert.equal(event.kind, 'message');
  assert.equal(event.isOwn, true);
  assert.equal(event.isInbound, false);
});

test('normalizeSignalEvent: attachment-only message is kept without text', () => {
  const event = normalizeSignalEvent({
    method: 'receiveMessage',
    params: {
      envelope: { sourceNumber: '+49123', timestamp: 9, dataMessage: { timestamp: 9, attachments: [{ contentType: 'image/jpeg' }] } },
    },
  }, '+4915112345678');
  assert.equal(event.kind, 'message');
  assert.equal(event.text, null);
  assert.equal(event.hasAttachment, true);
});

test('normalizeSignalEvent: receipts are recognized and typed, never answered', () => {
  for (const type of ['DELIVERY', 'READ', 'VIEWED']) {
    const event = normalizeSignalEvent({
      method: 'receiveMessage',
      params: { envelope: { sourceNumber: '+49123', timestamp: 10, receiptMessage: { timestamp: 10, type, isReceipt: true } } },
    }, '+4915112345678');
    assert.equal(event.kind, 'receipt');
    assert.equal(event.receiptType, type.toLowerCase());
    assert.equal(event.text, null);
  }
});

test('normalizeSignalEvent: typing, stories, stickers and unknown methods are dropped', () => {
  assert.equal(normalizeSignalEvent({ method: 'receiveMessage', params: { envelope: { typingMessage: { timestamp: 1 } } } }).kind, 'typing');
  assert.equal(normalizeSignalEvent({ method: 'receiveMessage', params: { envelope: { storyMessage: {} } } }).kind, 'ignore');
  assert.equal(normalizeSignalEvent({ method: 'receiveMessage', params: { envelope: { stickerMessage: {} } } }).kind, 'ignore');
  assert.equal(normalizeSignalEvent({ method: 'sendResult', params: {} }).kind, 'ignore');
  assert.equal(normalizeSignalEvent({ method: 'link', params: {} }).kind, 'ignore');
  assert.equal(normalizeSignalEvent({}).kind, 'ignore');
  assert.equal(normalizeSignalEvent(null).kind, 'ignore');
});

test('normalizeSignalEvent: envelopes without timestamps or from unknown sources are dropped', () => {
  assert.equal(normalizeSignalEvent({ method: 'receiveMessage', params: { envelope: { dataMessage: { message: 'no stamps' } } } }).kind, 'ignore');
  assert.equal(normalizeSignalEvent({ method: 'receiveMessage', params: { envelope: { sourceNumber: '+4999', timestamp: 1 } } }).kind, 'ignore');
});

// ------------------------------------------------------------ client surface

function makeFetch(handler) {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, options });
    return handler(url, options);
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

class FakeWebSocket {
  static instances = [];
  constructor(url) { this.url = url; this.listeners = new Map(); FakeWebSocket.instances.push(this); }
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(fn);
  }
  emit(name, event) { for (const fn of this.listeners.get(name) ?? []) fn(event); }
  close() { this.emit('close', { code: 1000 }); }
}

test('SignalSidecarClient: accounts, pairing URI and receive URL are built strictly', async () => {
  const fetchImpl = makeFetch((url) => {
    if (url.includes('/v1/accounts')) return jsonResponse(['+4915112345678']);
    if (url.includes('/v1/qrcodelink/raw')) return jsonResponse({ device_link_uri: 'sgnl://linkdevice?uuid=SECRET' });
    if (url.includes('/v1/health')) return jsonResponse({ status: 'ok' });
    return new Response('not found', { status: 404 });
  });
  const client = new SignalSidecarClient({ baseUrl: 'http://127.0.0.1:8099', fetchImpl });

  assert.deepEqual(await client.getAccounts(), ['+4915112345678']);
  const uri = await client.getDeviceLinkUri('liferadar');
  assert.equal(uri, 'sgnl://linkdevice?uuid=SECRET');
  assert.ok(fetchImpl.calls.some((c) => c.url === 'http://127.0.0.1:8099/v1/qrcodelink/raw?device_name=liferadar'));
  assert.deepEqual(await client.getHealth(), { status: 'ok' });

  await assert.rejects(() => client.getDeviceLinkUri(''), /device_name/);
  assert.throws(() => new SignalSidecarClient({ baseUrl: 'not-a-url', fetchImpl }), /absolute http\(s\)/);
  assert.throws(() => new SignalSidecarClient({ baseUrl: 'ws://127.0.0.1:8099', fetchImpl }), /absolute http\(s\)/);
  assert.throws(() => new SignalSidecarClient({ baseUrl: 'http://signal.example.com:8080', fetchImpl }), /loopback/);
  await assert.rejects(async () => {
    const failing = new SignalSidecarClient({ baseUrl: 'http://127.0.0.1:8099', fetchImpl: async () => new Response('boom', { status: 500 }) });
    await failing.getAccounts();
  }, (error) => error.statusCode === 500);

  const badJson = new SignalSidecarClient({ baseUrl: 'http://127.0.0.1:8099', fetchImpl: async () => jsonResponse({ nope: true }) });
  await assert.rejects(() => badJson.getAccounts(), /unexpected accounts payload/);

  // Receive URL: account validated, raw '+' kept (proxy allowlist matches raw charset).
  FakeWebSocket.instances.length = 0;
  const receiveClient = new SignalSidecarClient({ baseUrl: 'http://127.0.0.1:8099', WebSocketImpl: FakeWebSocket });
  const stream = receiveClient.openReceiveStream('+4915112345678');
  assert.equal(FakeWebSocket.instances[0].url, 'ws://127.0.0.1:8099/v1/receive/+4915112345678');
  assert.throws(() => receiveClient.openReceiveStream('../evil'), /invalid format/);
  assert.throws(() => receiveClient.openReceiveStream(''), /linked account/);
  stream.close();
});

test('SignalSidecarClient receive stream: normalizes frames, ignores garbage, reports close', () => {
  const fakeLogger = { debug() {}, warn() {}, info() {}, error() {} };
  const client = new SignalSidecarClient({ baseUrl: 'http://127.0.0.1:8099', WebSocketImpl: FakeWebSocket, logger: fakeLogger });
  FakeWebSocket.instances.length = 0;
  const stream = client.openReceiveStream('+4915112345678');
  const socket = FakeWebSocket.instances.at(-1);

  const events = [];
  const closes = [];
  stream.events.on('event', (event) => events.push(event));
  stream.events.on('close', (event) => closes.push(event));

  socket.emit('message', { data: JSON.stringify({ method: 'receiveMessage', params: { envelope: { sourceNumber: '+49123', timestamp: 42, dataMessage: { timestamp: 42, message: 'yo' } } } }) });
  socket.emit('message', { data: 'not json at all' });
  socket.emit('message', { data: JSON.stringify({ method: 'receiveMessage', params: { envelope: { receiptMessage: { timestamp: 43, type: 'DELIVERY' } } } }) });
  assert.equal(events.length, 2);
  assert.equal(events[0].kind, 'message');
  assert.equal(events[1].kind, 'receipt');

  socket.emit('error', { type: 'connect_error' });
  socket.emit('close', { code: 1006 });
  assert.equal(closes.length, 1, 'error+close collapse into one close notification');
  stream.close();
});
