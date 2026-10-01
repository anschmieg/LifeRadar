import { EventEmitter } from 'node:events';

// Typed client for signal-cli-rest-api, always pointed at the local
// deny-by-default proxy (src/signal-proxy.mjs) — never at the raw sidecar.
//
// Deliberate surface: this class exposes ONLY
//   * GET /v1/health, /v1/about, /v1/accounts, /v1/qrcodelink[/raw]
//   * websocket GET /v1/receive/{number}  (json-rpc mode)
// There is no send / receipt / reaction / typing / presence method here on
// purpose: the connector has no code path that could emit outbound Signal
// traffic, and the proxy rejects those routes anyway (defense in depth).
//
// Read receipts: the receive stream never passes send_read_receipts (the
// proxy additionally forces `send_read_receipts=false`), and upstream
// signal-cli spawns its json-rpc daemon WITHOUT --send-read-receipts, so only
// signal-cli's default delivery receipts happen. See docs/signal-connector.md.

const REQUEST_TIMEOUT_MS = 20000;

function messageIdFor(params, envelope) {
  const dm = envelope?.dataMessage ?? params?.dataMessage ?? null;
  if (dm?.timestamp != null) return String(dm.timestamp);
  if (params?.timestamp != null) return String(params.timestamp);
  if (envelope?.timestamp != null) return String(envelope.timestamp);
  return null;
}

/**
 * Normalize a raw json-rpc `receiveMessage` notification (params payload)
 * into a stable, minimal event. Anything not classified is `ignore`, which
 * includes typing indicators, stories, sticker packs, profile updates —
 * LifeRadar neither acts on nor stores them.
 */
export function normalizeSignalEvent(raw, { account = null } = {}) {
  if (!raw || typeof raw !== 'object') return { kind: 'ignore' };
  if (raw.method && raw.method !== 'receiveMessage') return { kind: 'ignore', reason: `method:${raw.method}` };
  const params = raw.params && typeof raw.params === 'object' ? raw.params : raw;
  const envelope = params.envelope && typeof params.envelope === 'object' ? params.envelope : params;

  const dm = envelope.dataMessage ?? params.dataMessage ?? null;
  const rm = envelope.receiptMessage ?? params.receiptMessage ?? null;
  const tm = envelope.typingMessage ?? params.typingMessage ?? null;

  if (dm && (dm.message != null || (Array.isArray(dm.attachments) && dm.attachments.length > 0))) {
    const sourceId = envelope.sourceNumber || envelope.source || params.source || null;
    const groupId = dm.groupV2Id || dm.groupId || null;
    const messageId = messageIdFor(params, envelope);
    if (!messageId) return { kind: 'ignore', reason: 'missing_timestamp' };
    const isOwn = !!(account && sourceId && String(sourceId) === String(account));
    return {
      kind: 'message',
      conversationId: groupId ? `group:${groupId}` : `direct:${sourceId || 'unknown'}`,
      conversationTitle: envelope.sourceName || (groupId ? null : sourceId),
      messageId,
      senderId: sourceId,
      senderLabel: envelope.sourceName || sourceId || null,
      occurredAt: envelope.timestamp ?? params.timestamp ?? Date.now(),
      text: typeof dm.message === 'string' ? dm.message : null,
      hasAttachment: Array.isArray(dm.attachments) ? dm.attachments.length > 0 : false,
      expiresInSeconds: dm.expiresInSeconds ?? 0,
      isOwn,
      isInbound: !isOwn,
    };
  }

  if (rm) {
    // Delivery receipts (default on signal-cli receive) and any inbound
    // read/viewed receipt. Accepted per operator policy: we record nothing
    // and — critically — never respond with a receipt of our own.
    return { kind: 'receipt', receiptType: String(rm.type || 'DELIVERY').toLowerCase(), timestamp: rm.timestamp ?? null, text: null };
  }

  if (tm) return { kind: 'typing' };
  return { kind: 'ignore' };
}

/**
 * Open a json-rpc receive stream. Returns a handle with `close()`; events are
 * delivered through the `events` EventEmitter ('event', 'error', 'close').
 */
export function openReceiveStream({ WebSocketImpl, wsUrl, account, logger }) {
  const events = new EventEmitter();
  let closed = false;
  let socket = null;

  // An 'error' event with no listener throws and would take the gateway down;
  // degrade to a log line instead, since the connector always listens for
  // 'close' and drives reconnection from there.
  const emitError = (error) => {
    const normalized = error instanceof Error ? error : new Error('signal websocket error');
    if (events.listenerCount('error') > 0) events.emit('error', normalized);
    else logger.warn({ err: normalized }, 'signal receive stream error');
  };

  const connect = () => {
    if (closed) return;
    try {
      socket = new WebSocketImpl(wsUrl);
    } catch (error) {
      emitError(error);
      events.emit('close');
      return;
    }
    socket.addEventListener('message', (message) => {
      let parsed;
      try {
        parsed = JSON.parse(typeof message.data === 'string' ? message.data : String(message.data));
      } catch {
        logger.debug('signal receive stream dropped malformed frame');
        return;
      }
      const normalized = normalizeSignalEvent(parsed, { account });
      if (normalized.kind === 'ignore') {
        logger.debug({ reason: normalized.reason ?? 'unclassified' }, 'signal event ignored');
        return;
      }
      events.emit('event', normalized);
    });
    socket.addEventListener('error', (error) => emitError(error instanceof Error ? error : new Error('signal websocket error')));
    socket.addEventListener('close', () => {
      if (!closed) events.emit('close');
    });
  };

  connect();
  return {
    events,
    close() {
      closed = true;
      try { socket?.close(); } catch { /* already closed */ }
    },
    get closed() { return closed; },
  };
}

export class SignalSidecarClient {
  /**
   * @param {{baseUrl: string, WebSocketImpl?: typeof WebSocket, fetchImpl?: typeof fetch, logger?: object}} options
   */
  constructor({ baseUrl, WebSocketImpl = globalThis.WebSocket, fetchImpl = globalThis.fetch, logger = console }) {
    if (!baseUrl) throw new Error('signal client requires baseUrl');
    let parsed;
    try {
      parsed = new URL(baseUrl);
    } catch {
      throw new Error('signal client baseUrl must be an absolute http(s) URL');
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      throw new Error('signal client baseUrl must be an absolute http(s) URL');
    }
    if (!['127.0.0.1', '::1', 'localhost'].includes(parsed.hostname)) {
      throw new Error('signal client may only talk to the local loopback proxy, never to a remote sidecar');
    }
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.WebSocketImpl = WebSocketImpl;
    this.fetchImpl = fetchImpl;
    this.logger = logger;
  }

  async #get(path) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await this.fetchImpl(`${this.baseUrl}${path}`, { signal: controller.signal });
      if (!response.ok) {
        const error = new Error(`signal sidecar responded ${response.status} for ${path.split('?')[0]}`);
        error.statusCode = response.status;
        throw error;
      }
      return response;
    } finally {
      clearTimeout(timer);
    }
  }

  async getHealth() {
    const response = await this.#get('/v1/health');
    const payload = await response.json();
    if (!payload || typeof payload !== 'object') throw new Error('unexpected health payload from signal sidecar');
    return { status: payload.status ?? 'unknown' };
  }

  async getAccounts() {
    const response = await this.#get('/v1/accounts');
    const accounts = await response.json();
    if (!Array.isArray(accounts)) throw new Error('unexpected accounts payload from signal sidecar');
    return accounts.map(String);
  }

  /** Raw device-link URI for QR pairing. Must never be logged or persisted. */
  async getDeviceLinkUri(deviceName) {
    if (!deviceName || !/^[A-Za-z0-9._-]{1,64}$/.test(String(deviceName))) {
      throw new Error('signal device_name must be a short identifier');
    }
    const response = await this.#get(`/v1/qrcodelink/raw?device_name=${encodeURIComponent(deviceName)}`);
    const payload = await response.json();
    const uri = payload?.device_link_uri;
    if (typeof uri !== 'string' || !uri) throw new Error('signal sidecar returned no device_link_uri');
    return uri;
  }

  openReceiveStream(account) {
    if (!account) throw new Error('signal receive requires a linked account');
    if (!/^[A-Za-z0-9+._@-]{1,64}$/.test(String(account))) throw new Error('signal receive account has invalid format');
    const wsUrl = `${this.baseUrl.replace(/^http/, 'ws')}/v1/receive/${account}`;
    return openReceiveStream({ WebSocketImpl: this.WebSocketImpl, wsUrl, account, logger: this.logger });
  }
}
