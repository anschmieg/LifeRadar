import { Pool } from 'pg';

function env(name, fallback = '') { return process.env[name] ?? fallback; }
function json(value) { return value == null ? {} : value; }
const NOISE_KEYS = new Set(['protocolMessage', 'senderKeyDistributionMessage']);

// Baileys control chatter (history-sync notices, key distribution) carries no
// user content and must not create message rows.
function isNoisePayload(message) {
  if (!message || typeof message !== 'object') return false;
  const keys = Object.keys(message).filter((key) => key !== 'messageContextInfo');
  if (!keys.length) return true; // context metadata only — no content at all
  return keys.every((key) => NOISE_KEYS.has(key));
}

function pickText(message) {
  if (!message) return null;
  if (typeof message === 'string') return message;
  // Unwrap Baileys containers (ephemeral / view-once / document-with-caption).
  const inner = message.ephemeralMessage?.message
    || message.viewOnceMessage?.message
    || message.viewOnceMessageV2?.message
    || message.documentWithCaptionMessage?.message
    || message;
  if (typeof inner === 'string') return inner;
  if (inner.conversation) return inner.conversation;
  if (inner.extendedTextMessage?.text) return inner.extendedTextMessage.text;
  if (inner.interactiveMessage) {
    const interactive = inner.interactiveMessage;
    const body = interactive.body?.text || interactive.header?.title || interactive.footer?.text;
    if (body) return body;
  }
  if (inner.listResponseMessage?.title) return inner.listResponseMessage.title;
  if (inner.buttonsResponseMessage?.selectedButtonId) return inner.buttonsResponseMessage.selectedButtonId;
  const kinds = [
    ['imageMessage', 'image'],
    ['videoMessage', 'video'],
    ['audioMessage', 'voice message'],
    ['pttMessage', 'voice note'],
    ['stickerMessage', 'sticker'],
    ['documentMessage', 'document'],
    ['contactMessage', 'contact'],
    ['contactsArrayMessage', 'contacts'],
    ['locationMessage', 'location'],
    ['liveLocationMessage', 'live location'],
    ['pollCreationMessage', 'poll'],
    ['pollCreationMessageV3', 'poll'],
    ['reactionMessage', 'reaction'],
  ];
  for (const [key, label] of kinds) {
    const media = inner[key];
    if (!media) continue;
    const detail = media.caption || media.fileName || media.title || (key === 'reactionMessage' ? media.text : null);
    return `[${label}${detail ? `: ${detail}` : ''}]`;
  }
  return null;
}

export class GatewayDb {
  constructor({ logger }) {
    this.logger = logger;
    this.pool = new Pool({ host: env('LIFERADAR_DB_HOST', 'liferadar-db'), port: Number.parseInt(env('LIFERADAR_DB_PORT', '5432'), 10), database: env('LIFERADAR_DB_NAME', 'life_radar'), user: env('LIFERADAR_DB_USER', 'life_radar'), password: env('LIFERADAR_DB_PASSWORD', ''), max: 6 });
  }
  async query(sql, params = []) { return this.pool.query(sql, params); }
  async getConnectorAccounts(provider) {
    const result = await this.query(`select provider, account_id, display_label, auth_state, enabled, last_synced_at, last_error_at, last_error, metadata, created_at, updated_at from life_radar.connector_accounts where provider = $1 order by updated_at desc`, [provider]);
    return result.rows.map((row) => ({ ...row, metadata: json(row.metadata) }));
  }
  async upsertConnectorAccount({ provider, accountId, displayLabel = null, authState = 'logged_out', enabled = true, lastSyncedAt, lastErrorAt, lastError, metadata = {} }) {
    const hasLastSyncedAt = lastSyncedAt !== undefined;
    const hasLastErrorAt = lastErrorAt !== undefined;
    const hasLastError = lastError !== undefined;
    await this.query(
      `insert into life_radar.connector_accounts (provider, account_id, display_label, auth_state, enabled, last_synced_at, last_error_at, last_error, metadata)
       values ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
       on conflict (provider, account_id) do update
       set display_label = coalesce(excluded.display_label, life_radar.connector_accounts.display_label),
           auth_state = excluded.auth_state,
           enabled = excluded.enabled,
           last_synced_at = case when $10 then excluded.last_synced_at else life_radar.connector_accounts.last_synced_at end,
           last_error_at = case when $11 then excluded.last_error_at else life_radar.connector_accounts.last_error_at end,
           last_error = case when $12 then excluded.last_error else life_radar.connector_accounts.last_error end,
           metadata = life_radar.connector_accounts.metadata || excluded.metadata,
           updated_at = now()`,
      [provider, accountId, displayLabel, authState, enabled, lastSyncedAt ?? null, lastErrorAt ?? null, lastError ?? null, JSON.stringify(metadata), hasLastSyncedAt, hasLastErrorAt, hasLastError]
    );
  }
  async setCheckpoint(provider, accountId, key, value) { await this.query(`insert into life_radar.connector_sync_checkpoints (provider, account_id, checkpoint_key, checkpoint_value) values ($1, $2, $3, $4::jsonb) on conflict (provider, account_id, checkpoint_key) do update set checkpoint_value = excluded.checkpoint_value, updated_at = now()`, [provider, accountId, key, JSON.stringify(value ?? {})]); }
  async getCheckpoint(provider, accountId, key) { const result = await this.query(`select checkpoint_value from life_radar.connector_sync_checkpoints where provider = $1 and account_id = $2 and checkpoint_key = $3`, [provider, accountId, key]); return result.rows[0]?.checkpoint_value ?? null; }
  async upsertConversation({ source, externalId, accountId = null, title = null, participants = [], lastEventAt = null, metadata = {} }) {
    const result = await this.query(
      `insert into life_radar.conversations (source, external_id, account_id, title, participants, last_event_at, metadata)
       values ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb)
       on conflict (source, external_id) do update
       set account_id = coalesce(excluded.account_id, life_radar.conversations.account_id),
           title = coalesce(excluded.title, life_radar.conversations.title),
           participants = case when excluded.participants::jsonb = '[]'::jsonb then life_radar.conversations.participants else excluded.participants::jsonb end,
           last_event_at = greatest(coalesce(life_radar.conversations.last_event_at, excluded.last_event_at), coalesce(excluded.last_event_at, life_radar.conversations.last_event_at)),
           metadata = life_radar.conversations.metadata || excluded.metadata,
           updated_at = now()
       returning id`,
      [source, externalId, accountId, title, JSON.stringify(participants ?? []), lastEventAt, JSON.stringify(metadata ?? {})]
    );
    return result.rows[0]?.id ?? null;
  }
  async upsertMessage({ conversationId = null, source, externalId, senderId = null, senderLabel = null, occurredAt, contentText = null, contentJson = {}, isInbound = true, provenance = {} }) {
    await this.query(
      `insert into life_radar.message_events (conversation_id, source, external_id, sender_id, sender_label, occurred_at, content_text, content_json, is_inbound, provenance)
       values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10::jsonb)
       on conflict (source, external_id) do update
       set conversation_id = coalesce(excluded.conversation_id, life_radar.message_events.conversation_id),
           sender_id = coalesce(excluded.sender_id, life_radar.message_events.sender_id),
           sender_label = coalesce(excluded.sender_label, life_radar.message_events.sender_label),
           occurred_at = excluded.occurred_at,
           content_text = coalesce(excluded.content_text, life_radar.message_events.content_text),
           content_json = life_radar.message_events.content_json || excluded.content_json,
           is_inbound = excluded.is_inbound,
           provenance = life_radar.message_events.provenance || excluded.provenance,
           updated_at = now()`,
      [conversationId, source, externalId, senderId, senderLabel, occurredAt, contentText, JSON.stringify(contentJson ?? {}), isInbound, JSON.stringify(provenance ?? {})]
    );
  }
  // One-time repair: WhatsApp media rows ingested before media labeling had
  // blank content_text. content_json keeps the raw Baileys payload, so labels
  // can be reconstructed without touching the socket.
  async repairWhatsappMediaLabels() {
    const rows = await this.query(
      `select id, content_json from life_radar.message_events
       where source = 'whatsapp' and (content_text is null or content_text = '')
         and content_json is not null and content_json::text <> '{}'
       limit 5000`
    );
    let fixed = 0;
    for (const row of rows.rows) {
      const payload = json(row.content_json);
      const text = pickText(payload);
      if (!text) {
        // Control chatter (history-sync notices etc.) has no text and no value —
        // drop the row entirely.
        if (isNoisePayload(payload)) {
          await this.query(`delete from life_radar.message_events where id = $1`, [row.id]);
          fixed++;
        }
        continue;
      }
      const update = await this.query(
        `update life_radar.message_events set content_text = $1 where id = $2 and (content_text is null or content_text = '')`,
        [text, row.id]
      );
      fixed += update.rowCount ?? 0;
    }
    return fixed;
  }
  async purgeExcludedConversations(provider, externalIds) {
    const ids = (externalIds || []).map(String).filter(Boolean);
    if (!ids.length) return 0;
    // messages cascade with their conversation (ON DELETE CASCADE)
    const removed = await this.query(
      `delete from life_radar.conversations where source = $1 and external_id = any($2::text[])`,
      [provider, ids]
    );
    return removed.rowCount ?? 0;
  }
  async ingestTelegramMessage(accountId, dialog, message, meId = null) {
    if (!message?.id) return;
    if (message.action) return; // service messages (joins, pins, calls) are noise
    const externalId = String(dialog.id);
    // gramjs `message.date` is unix epoch SECONDS; timestamptz needs a Date.
    const occurredAt = message.date instanceof Date ? message.date : new Date(Number(message.date) * 1000);
    const conversationId = await this.upsertConversation({ source: 'telegram', externalId, accountId, title: dialog.title || dialog.name || dialog.entity?.title || dialog.entity?.username || externalId, participants: [], lastEventAt: occurredAt, metadata: { provider: 'telegram', telegram_dialog_id: externalId, entity_type: dialog.entity?.className || null } });
    await this.upsertMessage({ conversationId, source: 'telegram', externalId: `${externalId}:${message.id}`, senderId: message.senderId ? String(message.senderId) : null, senderLabel: message.sender?.username || message.sender?.title || [message.sender?.firstName, message.sender?.lastName].filter(Boolean).join(' ') || null, occurredAt, contentText: message.message || null, contentJson: { raw_text: message.message || null, media: message.media ? message.media.className || 'media' : null }, isInbound: meId ? String(message.senderId ?? '') !== String(meId) : !message.out, provenance: { provider: 'telegram', account_id: accountId, message_id: String(message.id) } });
  }
  async ingestWhatsAppChat(accountId, chat) { if (!chat?.id) return null; return this.upsertConversation({ source: 'whatsapp', externalId: String(chat.id), accountId, title: chat.name || chat.pushName || String(chat.id), participants: [], lastEventAt: chat.conversationTimestamp ? new Date(chat.conversationTimestamp * 1000) : null, metadata: { provider: 'whatsapp', jid: String(chat.id), archived: !!chat.archived, unread_count: chat.unreadCount ?? 0 } }); }
  async ingestWhatsAppMessage(accountId, message, { conversationTitle = null } = {}) { const key = message?.key; if (!key?.id || !key?.remoteJid) return; if (isNoisePayload(message.message)) return; const occurredAt = message.messageTimestamp ? new Date(Number(message.messageTimestamp) * 1000) : new Date(); const conversationId = await this.upsertConversation({ source: 'whatsapp', externalId: String(key.remoteJid), accountId, title: conversationTitle || String(key.remoteJid), participants: [], lastEventAt: occurredAt, metadata: { provider: 'whatsapp', jid: String(key.remoteJid) } }); await this.upsertMessage({ conversationId, source: 'whatsapp', externalId: `${key.remoteJid}:${key.id}`, senderId: key.participant || key.remoteJid, occurredAt, contentText: pickText(message.message), contentJson: json(message.message), isInbound: !key.fromMe, provenance: { provider: 'whatsapp', account_id: accountId, remote_jid: key.remoteJid } }); }
  async ingestSignalMessage(accountId, event) {
    if (!event?.conversationId || !event?.messageId) return null;
    const externalMessageId = `${event.conversationId}:${event.messageId}`;
    const parsedAt = event.occurredAt == null ? NaN : Number(event.occurredAt);
    const occurredAt = Number.isFinite(parsedAt) ? new Date(parsedAt) : new Date();
    const conversationId = await this.upsertConversation({
      source: 'signal',
      externalId: event.conversationId,
      accountId,
      title: event.conversationTitle || event.conversationId,
      participants: [],
      lastEventAt: occurredAt,
      metadata: {
        provider: 'signal',
        signal_account: accountId,
        conversation_kind: String(event.conversationId).startsWith('group:') ? 'group' : 'direct',
      },
    });
    await this.upsertMessage({
      conversationId,
      source: 'signal',
      externalId: externalMessageId,
      senderId: event.senderId ? String(event.senderId) : null,
      senderLabel: event.senderLabel || null,
      occurredAt,
      contentText: event.text || null,
      contentJson: { text: event.text || null, has_attachment: !!event.hasAttachment, expires_in_seconds: event.expiresInSeconds ?? 0, is_own: !!event.isOwn },
      isInbound: event.isInbound !== false,
      provenance: { provider: 'signal', account_id: accountId, message_id: String(event.messageId) },
    });
    return { conversationId, externalId: externalMessageId };
  }
}
