import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import http from 'node:http';
import test from 'node:test';
import { once } from 'node:events';

import { evaluateSignalRequest, createSignalProxy } from '../src/signal-proxy.mjs';

function allow(method, url, options) {
  return evaluateSignalRequest(method, url, options);
}

test('signal proxy allowlist: GET-only, exact routes, nothing else', () => {
  const allowed = [
    'GET /v1/health',
    'GET /v1/about',
    'GET /v1/accounts',
    'GET /v1/qrcodelink?device_name=liferadar',
    'GET /v1/qrcodelink/raw?device_name=liferadar',
    'GET /v1/receive/+4915112345678',
  ];
  for (const entry of allowed) {
    const [method, url] = entry.split(' ');
    const verdict = allow(method, url);
    assert.equal(verdict.allowed, true, `${entry} must be allowed (got: ${verdict.reason})`);
  }

  const denied = [
    // every non-GET method, including the read/viewed receipt endpoint
    'POST /v1/receipts/+4915112345678',
    'PUT /v1/receipts/+4915112345678',
    'GET /v1/receipts/+4915112345678',
    'POST /v2/send',
    'POST /v1/send',
    'POST /v1/typing-indicator/+4915112345678',
    'POST /v1/reactions/+4915112345678',
    'POST /v1/register/+4915112345678',
    'POST /v1/unregister/+4915112345678',
    'DELETE /v1/devices/12345/1',
    'PUT /v1/devices/12345/1',
    'PATCH /v1/devices/12345/1',
    // read-only-looking GETs that are still not on the allowlist
    'GET /',
    'GET /v1',
    'GET /v1/contacts/+4915112345678',
    'GET /v1/conversations/+4915112345678',
    'GET /v1/groups',
    'GET /v1/settings/+4915112345678',
    'GET /v1/identities/+4915112345678',
    'GET /v1/attachments',
    'GET /v1/evil',
    'GET /v1/qrcodelink/raw/extra?device_name=x',
    // missing required params / smuggled params
    'GET /v1/qrcodelink',
    'GET /v1/qrcodelink/raw',
    'GET /v1/qrcodelink?device_name=x&qr_code_version=999',
    'GET /v1/accounts?limit=10',
    // traversal / obfuscation
    'GET /v1/%61ccounts',
    'GET /v1/qrcodelink/../send?device_name=x',
    'GET http://evil.internal/v1/send',
    'GET //v1/accounts',
  ];
  for (const entry of denied) {
    const [method, ...rest] = entry.split(' ');
    const verdict = allow(method, rest.join(' '));
    assert.equal(verdict.allowed, false, `${entry} must be denied (got: ${JSON.stringify(verdict)})`);
  }
});

test('signal proxy forbids read receipts explicitly and forces send_read_receipts=false', () => {
  for (const value of ['true', '1', 'TRUE', 'yes']) {
    const verdict = allow('GET', `/v1/receive/+4915112345678?send_read_receipts=${value}`);
    assert.equal(verdict.allowed, false, `send_read_receipts=${value} must be denied`);
    assert.equal(verdict.reason, 'read_receipts_forbidden');
  }
  const explicitFalse = allow('GET', '/v1/receive/+4915112345678?send_read_receipts=false');
  assert.equal(explicitFalse.allowed, true);
  assert.equal(explicitFalse.search, '?send_read_receipts=false');

  const bare = allow('GET', '/v1/receive/+4915112345678');
  assert.equal(bare.allowed, true);
  assert.equal(bare.search, '?send_read_receipts=false', 'explicit false is always attached upstream');

  const withTimeout = allow('GET', '/v1/receive/+4915112345678?timeout=20');
  assert.equal(withTimeout.allowed, false, 'unexpected query params are denied, never silently forwarded');
  assert.equal(withTimeout.reason, 'query_param_not_allowed:timeout');
});

test('signal proxy upgrades websockets for receive only', () => {
  const receive = allow('GET', '/v1/receive/+4915112345678', { isUpgrade: true });
  assert.equal(receive.allowed, true);
  const qr = allow('GET', '/v1/qrcodelink/raw?device_name=x', { isUpgrade: true });
  assert.equal(qr.allowed, false);
  assert.equal(qr.reason, 'upgrade_not_allowed_for_route');
  const send = allow('POST', '/v1/receive/+4915112345678', { isUpgrade: true });
  assert.equal(send.allowed, false);
});

// ---------------------------------------------------------------- live proxy

function makeStubUpstream() {
  const requests = [];
  const server = http.createServer((req, res) => {
    requests.push({ method: req.method, url: req.url });
    if (req.url === '/v1/accounts') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('["+4915112345678"]');
    } else if (req.url?.startsWith('/v1/qrcodelink')) {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ device_link_uri: 'sgnl://linkdevice?uuid=UPSTREAMONLY' }));
    } else {
      res.writeHead(200, { 'content-type': 'text/plain' });
      res.end('ok');
    }
  });
  const sockets = new Set();
  server.on('upgrade', (req, socket, head) => {
    requests.push({ method: 'UPGRADE', url: req.url });
    const key = req.headers['sec-websocket-key'];
    const accept = crypto.createHash('sha1').update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64');
    socket.write([
      'HTTP/1.1 101 Switching Protocols',
      'Upgrade: websocket',
      'Connection: Upgrade',
      `Sec-WebSocket-Accept: ${accept}`,
      '',
      '',
    ].join('\r\n'));
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    // echo-ish: forward raw frames back is unnecessary; the handshake is the contract.
    if (head?.length) socket.write(head);
  });
  return {
    server,
    requests,
    listen: async () => { server.listen(0, '127.0.0.1'); await once(server, 'listening'); return server.address().port; },
    close: async () => {
      for (const socket of sockets) socket.destroy();
      server.closeAllConnections?.();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

function captureLogger() {
  const entries = [];
  const logger = { warn: (...a) => entries.push(a), info: (...a) => entries.push(a), debug: (...a) => entries.push(a), error: (...a) => entries.push(a) };
  logger.entries = entries;
  logger.text = () => JSON.stringify(entries);
  return logger;
}

test('signal proxy live: allowlisted GETs forward, denied requests never reach upstream', async () => {
  const upstream = makeStubUpstream();
  const upstreamPort = await upstream.listen();
  const logger = captureLogger();
  const proxy = createSignalProxy({ upstreamUrl: `http://127.0.0.1:${upstreamPort}`, host: '127.0.0.1', port: 0, logger });
  const { port } = await proxy.listen();
  const base = `http://127.0.0.1:${port}`;

  try {
    const accounts = await fetch(`${base}/v1/accounts`);
    assert.equal(accounts.status, 200);
    assert.deepEqual(await accounts.json(), ['+4915112345678']);

    const link = await fetch(`${base}/v1/qrcodelink/raw?device_name=SECRETNAME`);
    assert.equal(link.status, 200);
    assert.equal((await link.json()).device_link_uri, 'sgnl://linkdevice?uuid=UPSTREAMONLY');

    const seenBeforeDenied = upstream.requests.length;
    const deniedCases = [
      ['POST', `${base}/v1/receipts/+4915112345678`],
      ['POST', `${base}/v2/send`],
      ['POST', `${base}/v1/typing-indicator/+4915112345678`],
      ['DELETE', `${base}/v1/devices/1/2`],
      ['GET', `${base}/v1/contacts/+4915112345678`],
      ['GET', `${base}/v1/evil`],
      ['GET', `${base}/v1/receive/+4915112345678?send_read_receipts=true`],
      ['GET', `${base}/v1/qrcodelink`],
      ['PUT', `${base}/v1/accounts`],
    ];
    for (const [method, url] of deniedCases) {
      const response = await fetch(url, { method });
      assert.equal(response.status, 403, `${method} ${url} must be 403`);
      assert.equal((await response.json()).error, 'forbidden');
    }
    assert.equal(upstream.requests.length, seenBeforeDenied, 'denied requests must not reach the sidecar');

    // Allowed receive GET is forced to explicit send_read_receipts=false upstream.
    const before = upstream.requests.length;
    const receive = await fetch(`${base}/v1/receive/+4915112345678`);
    assert.equal(receive.status, 200);
    assert.equal(upstream.requests.length, before + 1);
    assert.equal(upstream.requests.at(-1).url, '/v1/receive/+4915112345678?send_read_receipts=false');

    // Logs: reasons only, never query strings or pairing material.
    const logged = logger.text();
    assert.ok(!logged.includes('SECRETNAME'), 'query strings must never be logged');
    assert.ok(!logged.includes('send_read_receipts'), 'raw query strings must never be logged');
    assert.ok(logged.includes('route_not_allowlisted'), 'denials are still observable');
  } finally {
    await proxy.close();
    await upstream.close();
  }
});

test('signal proxy live: websocket passthrough for receive, 403 for everything else', async () => {
  const upstream = makeStubUpstream();
  const upstreamPort = await upstream.listen();
  const proxy = createSignalProxy({ upstreamUrl: `http://127.0.0.1:${upstreamPort}`, host: '127.0.0.1', port: 0, logger: captureLogger() });
  const { port } = await proxy.listen();

  const wsHandshake = (path) => new Promise((resolve) => {
    const request = http.request({
      host: '127.0.0.1',
      port,
      path,
      headers: {
        Connection: 'Upgrade',
        Upgrade: 'websocket',
        'Sec-WebSocket-Key': Buffer.from('liferadar-test-key-0123456789ab').toString('base64'),
        'Sec-WebSocket-Version': '13',
      },
    });
    request.on('upgrade', (res, socket, head) => { resolve({ status: 101, socket, head }); });
    request.on('response', (res) => { res.resume(); resolve({ status: res.statusCode, socket: null }); });
    request.on('error', (error) => resolve({ status: 0, error }));
    request.end();
  });

  try {
    const allowed = await wsHandshake('/v1/receive/+4915112345678');
    assert.equal(allowed.status, 101, 'receive websocket must pass through');
    allowed.socket?.destroy();

    for (const path of ['/v1/qrcodelink/raw?device_name=x', '/v1/accounts', '/v1/receipts/+4915112345678', '/v1/evil']) {
      const denied = await wsHandshake(path);
      assert.equal(denied.status, 403, `upgrade to ${path} must be denied`);
      denied.socket?.destroy();
    }
  } finally {
    await proxy.close();
    await upstream.close();
  }
});

test('signal proxy live: unreachable upstream answers 502 without crashing', async () => {
  const logger = captureLogger();
  const proxy = createSignalProxy({ upstreamUrl: 'http://127.0.0.1:1', host: '127.0.0.1', port: 0, logger });
  const { port } = await proxy.listen();
  try {
    const response = await fetch(`http://127.0.0.1:${port}/v1/accounts`);
    assert.equal(response.status, 502);
    assert.equal((await response.json()).error, 'upstream_unavailable');
  } finally {
    await proxy.close();
  }
});

test('signal proxy refuses non-loopback binds', () => {
  assert.throws(() => createSignalProxy({ upstreamUrl: 'http://127.0.0.1:8080', host: '0.0.0.0', port: 0 }), /loopback/);
  assert.throws(() => createSignalProxy({ upstreamUrl: 'file:///etc/passwd', host: '127.0.0.1', port: 0 }), /http/);
});
