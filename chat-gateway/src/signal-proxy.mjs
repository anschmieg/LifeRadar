import http from 'node:http';

// Deny-by-default reverse proxy in front of signal-cli-rest-api.
//
// The upstream sidecar has NO authentication and NO read-only mode (see
// docs/signal-connector.md). This proxy is the only process that talks to it.
// Policy:
//   * GET-only, and only on a tiny allowlist of routes.
//   * WebSocket upgrades only for /v1/receive/{number} (json-rpc mode).
//   * Query parameters are rebuilt from scratch; nothing is forwarded blindly.
//   * `send_read_receipts=true` is rejected outright (403) and the receive
//     route is always forwarded with `send_read_receipts=false` explicitly —
//     read receipts are never requested, while signal-cli's default delivery
//     receipts on receive remain untouched (accepted by operator policy).
//   * POST /v1/receipts/{number} (read/viewed receipts) can never match the
//     allowlist: every non-GET method is denied before routing.
//   * Logs never include query strings or bodies, so QR/session material
//     (device_link_uri, pairing secrets) can never reach the logs.
//
// The proxy binds loopback only; it is never published on any network.

const RECEIVE_ACCOUNT_PATTERN = /^\/v1\/receive\/[A-Za-z0-9+._@-]{1,64}$/;

const ALLOWED_GET_ROUTES = [
  { name: 'health', pattern: /^\/v1\/health$/, allowQuery: [] },
  { name: 'about', pattern: /^\/v1\/about$/, allowQuery: [] },
  { name: 'accounts', pattern: /^\/v1\/accounts$/, allowQuery: [] },
  { name: 'qrcodelink', pattern: /^\/v1\/qrcodelink$/, allowQuery: ['device_name'], requireQuery: ['device_name'] },
  { name: 'qrcodelink_raw', pattern: /^\/v1\/qrcodelink\/raw$/, allowQuery: ['device_name'], requireQuery: ['device_name'] },
  { name: 'receive', pattern: RECEIVE_ACCOUNT_PATTERN, allowQuery: [], requireQuery: [], forceQuery: { send_read_receipts: 'false' } },
];

/**
 * Evaluate a request against the allowlist. Pure function so tests can pin
 * the full allow/deny matrix without opening sockets.
 *
 * @param {string} method HTTP method
 * @param {string} rawUrl request target (path + query, or absolute URL)
 * @param {{isUpgrade?: boolean}} [options]
 * @returns {{allowed: boolean, reason?: string, route?: string, path?: string, search?: string}}
 */
export function evaluateSignalRequest(method, rawUrl, options = {}) {
  const isUpgrade = options.isUpgrade === true;
  if (String(method || '').toUpperCase() !== 'GET') {
    return { allowed: false, reason: 'method_not_allowed' };
  }
  let url;
  try {
    url = new URL(rawUrl, 'http://signal-proxy.invalid');
  } catch {
    return { allowed: false, reason: 'malformed_url' };
  }
  const pathname = url.pathname;
  const route = ALLOWED_GET_ROUTES.find((candidate) => candidate.pattern.test(pathname));
  if (!route) {
    return { allowed: false, reason: 'route_not_allowlisted' };
  }
  if (isUpgrade && route.name !== 'receive') {
    return { allowed: false, reason: 'upgrade_not_allowed_for_route' };
  }

  const forwarded = new URLSearchParams();
  for (const [key, value] of url.searchParams.entries()) {
    if (key === 'send_read_receipts') {
      // Hard rule: any attempt to request read receipts is refused, not
      // silently downgraded, so misuse is visible.
      if (value !== 'false') {
        return { allowed: false, reason: 'read_receipts_forbidden' };
      }
      continue; // re-added below via forceQuery
    }
    if (!route.allowQuery.includes(key)) {
      return { allowed: false, reason: `query_param_not_allowed:${key}` };
    }
    forwarded.set(key, value);
  }
  for (const key of route.requireQuery ?? []) {
    if (!forwarded.has(key)) {
      return { allowed: false, reason: `query_param_required:${key}` };
    }
  }
  for (const [key, value] of Object.entries(route.forceQuery ?? {})) {
    forwarded.set(key, value);
  }

  const search = forwarded.toString();
  return { allowed: true, route: route.name, path: pathname, search: search ? `?${search}` : '' };
}

function deny(res, statusCode, error) {
  res.writeHead(statusCode, { 'content-type': 'application/json', 'cache-control': 'no-store' });
  res.end(JSON.stringify({ error }));
}

function isLoopback(hostname) {
  return hostname === '127.0.0.1' || hostname === '::1' || hostname === 'localhost';
}

/**
 * Create (but do not listen on) the Signal sidecar proxy.
 *
 * @param {{upstreamUrl: string, host?: string, port?: number, logger?: object}} options
 */
export function createSignalProxy({ upstreamUrl, host = '127.0.0.1', port = 8099, logger = console }) {
  const upstream = new URL(upstreamUrl);
  if (!isLoopback(host)) {
    throw new Error('signal proxy may only bind a loopback address');
  }
  if (upstream.protocol !== 'http:' && upstream.protocol !== 'https:') {
    throw new Error('signal proxy upstream must be http(s)');
  }
  const upstreamPort = Number.parseInt(upstream.port || (upstream.protocol === 'https:' ? '443' : '80'), 10);

  function forwardUpgrade(req, socket, head, verdict) {
    const headers = { ...req.headers, host: upstream.host };
    const request = http.request({
      protocol: upstream.protocol,
      hostname: upstream.hostname,
      port: upstreamPort,
      method: 'GET',
      path: `${verdict.path}${verdict.search}`,
      headers,
    });
    let settled = false;
    const fail = (statusLine) => {
      if (settled) return;
      settled = true;
      socket.write(`${statusLine}\r\nconnection: close\r\n\r\n`);
      socket.destroy();
      request.destroy();
    };
    request.on('upgrade', (res, upstreamSocket, upstreamHead) => {
      settled = true;
      request.setTimeout(0); // receive streams may legitimately idle between frames
      socket.write(`HTTP/1.1 101 Switching Protocols\r\n${Object.entries(res.headers)
        .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(', ') : value}`)
        .join('\r\n')}\r\n\r\n`);
      if (upstreamHead?.length) socket.unshift(upstreamHead);
      if (head?.length) upstreamSocket.unshift(head);
      upstreamSocket.pipe(socket);
      socket.pipe(upstreamSocket);
      const teardown = () => { upstreamSocket.destroy(); socket.destroy(); };
      upstreamSocket.on('error', teardown);
      socket.on('error', teardown);
    });
    request.on('response', (res) => { res.resume(); fail(`HTTP/1.1 ${res.statusCode || 502} Bad Gateway`); });
    request.on('error', () => fail('HTTP/1.1 502 Bad Gateway'));
    request.setTimeout(15000, () => fail('HTTP/1.1 504 Gateway Timeout'));
    if (head?.length) request.write(head);
    request.end();
  }

  const server = http.createServer((req, res) => {
    const verdict = evaluateSignalRequest(req.method, req.url || '/');
    if (!verdict.allowed) {
      logger.warn({ method: req.method || 'UNKNOWN', reason: verdict.reason }, 'signal proxy denied request');
      deny(res, 403, 'forbidden');
      req.resume();
      return;
    }
    logger.debug({ method: 'GET', route: verdict.route }, 'signal proxy forwarding request');
    const request = http.request({
      protocol: upstream.protocol,
      hostname: upstream.hostname,
      port: upstreamPort,
      method: 'GET',
      path: `${verdict.path}${verdict.search}`,
      headers: { host: upstream.host, accept: req.headers.accept || '*/*', 'accept-encoding': 'identity' },
    }, (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
      upstreamRes.pipe(res);
    });
    request.on('error', (error) => {
      logger.warn({ err: error, route: verdict.route }, 'signal proxy upstream request failed');
      if (!res.headersSent) deny(res, 502, 'upstream_unavailable');
      else res.destroy();
    });
    request.setTimeout(30000, () => {
      request.destroy();
      if (!res.headersSent) deny(res, 504, 'upstream_timeout');
    });
    req.resume();
    request.end();
  });

  // Upgraded sockets are invisible to closeAllConnections(), so track every
  // socket ourselves; otherwise close() would hang forever on shutdown while a
  // receive websocket is open.
  const trackedSockets = new Set();
  server.on('connection', (socket) => {
    trackedSockets.add(socket);
    socket.on('close', () => trackedSockets.delete(socket));
  });

  server.on('upgrade', (req, socket, head) => {
    const method = req.method || 'GET';
    const isUpgrade = String(req.headers.upgrade || '').toLowerCase() === 'websocket';
    const verdict = evaluateSignalRequest(method, req.url || '/', { isUpgrade });
    if (!verdict.allowed || !isUpgrade) {
      logger.warn({ method, reason: verdict.reason || 'not_websocket' }, 'signal proxy denied upgrade');
      socket.write('HTTP/1.1 403 Forbidden\r\nconnection: close\r\n\r\n');
      socket.destroy();
      return;
    }
    logger.debug({ route: verdict.route }, 'signal proxy forwarding websocket upgrade');
    forwardUpgrade(req, socket, head, verdict);
  });

  return {
    server,
    listen: () => new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, host, () => {
        server.removeListener('error', reject);
        const address = server.address();
        resolve({ host: address.address, port: address.port });
      });
    }),
    async close() {
      for (const socket of trackedSockets) socket.destroy();
      trackedSockets.clear();
      await new Promise((resolve) => {
        // Guard against a socket that ignores destroy(): shutdown must never hang.
        const deadline = setTimeout(resolve, 2000);
        deadline.unref?.();
        server.close(() => { clearTimeout(deadline); resolve(); });
        server.closeAllConnections?.();
      });
    },
    get port() { const address = server.address(); return address && typeof address === 'object' ? address.port : port; },
    get redactedTarget() { return `${upstream.protocol}//${upstream.host}`; },
  };
}
