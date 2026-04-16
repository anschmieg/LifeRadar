import { Pool } from 'pg';

function env(name, fallback = '') {
  return process.env[name] ?? fallback;
}

function json(value) {
  return value == null ? {} : value;
}

function pickText(message) {
  if (!message) return null;
  if (typeof message === 'string') return message;
  if (message.conversation) return message.conversation;
  if (message.extendedTextMessage?.text) return message.extendedTextMessage.text;
  if (message.imageMessage?.caption) return message.imageMessage.caption;
  if (message.videoMessage?.caption) return message.videoMessage.caption;
  return null;
}

export class GatewayDb {
  constructor({ logger }) {
    this.logger = logger;
    this.pool = new Pool({
      host: env('LIFERADAR_DB_HOST', 'liferadar-db'),
      port: Number.parseInt(env('LIFERADAR_DB_PORT', '5432'), 10),
      database: env('LIFERADAR_DB_NAME', 'life_radar'),
      user: env('LIFERADAR_DB_USER', 'life_radar'),
      password: env('LIFERADAR_DB_PASSWORD', ''),
      max: 6,
    });
  }

  async query(sql, params = []) {
    return this.pool.query(sql, params);
  }

  async getConnectorAccounts(provider) {
    const result = await this.query(
      `select provider, account_id, display_label, auth_state, enabled, last_synced_at,
              last_error_at, last_error, metadata, created_at, updated_at
         from life_radar.connector_accounts
        where provider = $1
        order by updated_at desc`,
      [provider]
    );
    return result.rows.map((row) => ({
      ...row,
      metadata: json(row.metadata),
    }));
  }

  async upsertConnectorAccount({
    provider,
    accountId,
    displayLabel = null,
    authState = 'logged_out',
    enabled = true,
    lastSyncedAt = null,
    lastErrorAt = null,
    lastError = null,
    metadata = {},
  }) {
    await this.query(
      `insert into life_radar.connector_accounts
         (provider, account_id, display_label, auth_state, enabled, last_synced_at,
          last_error_at, last_error, metadata)
       values ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
       on conflict (provider, account_id) do update
       set display_label = excluded.display_label,
           auth_state = excluded.auth_state,
           enabled = excluded.enabled,
           last_synced_at = excluded.last_synced_at,
           last_error_at = excluded.last_error_at,
           last_error = excluded.last_error,
           metadata = life_radar.connector_accounts.metadata || excluded.metadata,
           updated_at = now()`,
      [provider, accountId, displayLabel, authState, enabled, lastSyncedAt, lastErrorAt, lastError, JSON.stringify(metadata)]
    );
  }

  async setCheckpoint(provider, accountId, key, value) {
    await this.query(
      `insert into life_radar.connector_sync_checkpoints
         (provider, account_id, checkpoint_key, checkpoint_value)
       values ($1, $2, $3, $4::jsonb)
       on conflict (provider, account_id, checkpoint_key) do update
       set checkpoint_value = excluded.checkpoint_value,
           updated_at = now()`,
      [provider, accountId, key, JSON.stringify(value ?? {})]
    );
  }

  async upsertConversation({
    source,
    externalId,
    accountId = null,
    title = null,
    participants = [],
    lastEventAt = null,
    metadata = {},
  }) {
    const result = await this.query(
      `insert into life_radar.conversations
         (source, external_id, account_id, title, participants, last_event_at, metadata)
       values ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb)
       on conflict (source, external_id) do update
       set account_id = coalesce(excluded.account_id, life_radar.conversations.account_id),
           title = coalesce(excluded.title, life_radar.conversations.title),
           participants = case
             when excluded.participants::jsonb = '[]'::jsonb
             then life_radar.conversations.participants
             else excluded.participants::jsonb
           end,
           last_event_at = greatest(
             coalesce(life_radar.conversations.last_event_at, excluded.last_event_at),
             coalesce(excluded.last_event_at, life_radar.conversations.last_event_at)
           ),
           metadata = life_radar.conversations.metadata || excluded.metadata,
           updated_at = now()
       returning id`,
      [
        source,
        externalId,
        accountId,
        title,
        JSON.stringify(participants ?? []),
        lastEventAt,
        JSON.stringify(metadata ?? {}),
      ]
    );
    return result.rows[0]?.id ?? null;
  }

  async upsertMessage({
    conversationId = null,
    source,
    externalId,
    senderId = null,
    senderLabel = null,
    occurredAt,
    contentText = null,
    contentJson = {},
    isInbound = true,
    provenance = {},
  }) {
    await this.query(
      `insert into life_radar.message_events
         (conversation_id, source, external_id, sender_id, sender_label, occurred_at,
          content_text, content_json, is_inbound, provenance)
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
      [
        conversationId,
        source,
        externalId,
        senderId,
        senderLabel,
        occurredAt,
        contentText,
        JSON.stringify(contentJson ?? {}),
        isInbound,
        JSON.stringify(provenance ?? {}),
      ]
    );
  }

  async ingestTelegramMessage(accountId, dialog, message, meId = null) {
    if (!message?.id) return;
    const externalId = String(dialog.id);
    const participants = [];
    if (dialog.entity?.username || dialog.entity?.title || dialog.entity?.firstName) {
      participants.push({
        id: String(dialog.entity?.id ?? dialog.id),
        label: dialog.entity?.title || [dialog.entity?.firstName, dialog.entity?.lastName].filter(Boolean).join(' ') || dialog.entity?.username || externalId,
        username: dialog.entity?.username || null,
      });
    }
    const conversationId = await this.upsertConversation({
      source: 'telegram',
      externalId,
      accountId,
      title: dialog.title || dialog.name || dialog.entity?.title || dialog.entity?.username || externalId,
      participants,
      lastEventAt: message.date,
      metadata: {
        provider: 'telegram',
        telegram_dialog_id: externalId,
        entity_type: dialog.entity?.className || null,
      },
    });

    await this.upsertMessage({
      conversationId,
      source: 'telegram',
      externalId: `${externalId}:${message.id}`,
      senderId: message.senderId ? String(message.senderId) : null,
      senderLabel: message.sender?.username || message.sender?.title || [message.sender?.firstName, message.sender?.lastName].filter(Boolean).join(' ') || null,
      occurredAt: message.date,
      contentText: message.message || null,
      contentJson: {
        raw_text: message.message || null,
        media: message.media ? message.media.className || 'media' : null,
      },
      isInbound: meId ? String(message.senderId ?? '') !== String(meId) : !message.out,
      provenance: {
        provider: 'telegram',
        account_id: accountId,
        message_id: String(message.id),
      },
    });
  }

  async ingestWhatsAppChat(accountId, chat) {
    if (!chat?.id) return null;
    return this.upsertConversation({
      source: 'whatsapp',
      externalId: String(chat.id),
      accountId,
      title: chat.name || chat.pushName || String(chat.id),
      participants: [],
      lastEventAt: chat.conversationTimestamp ? new Date(chat.conversationTimestamp * 1000) : null,
      metadata: {
        provider: 'whatsapp',
        jid: String(chat.id),
        archived: !!chat.archived,
        unread_count: chat.unreadCount ?? 0,
      },
    });
  }

  async ingestWhatsAppMessage(accountId, message, { conversationTitle = null } = {}) {
    const key = message?.key;
    if (!key?.id || !key?.remoteJid) return;
    const conversationId = await this.upsertConversation({
      source: 'whatsapp',
      externalId: String(key.remoteJid),
      accountId,
      title: conversationTitle || String(key.remoteJid),
      participants: [],
      lastEventAt: message.messageTimestamp ? new Date(Number(message.messageTimestamp) * 1000) : new Date(),
      metadata: {
        provider: 'whatsapp',
        jid: String(key.remoteJid),
      },
    });

    await this.upsertMessage({
      conversationId,
      source: 'whatsapp',
      externalId: `${key.remoteJid}:${key.id}`,
      senderId: key.participant || key.remoteJid,
      senderLabel: null,
      occurredAt: message.messageTimestamp ? new Date(Number(message.messageTimestamp) * 1000) : new Date(),
      contentText: pickText(message.message),
      contentJson: json(message.message),
      isInbound: !key.fromMe,
      provenance: {
        provider: 'whatsapp',
        account_id: accountId,
        remote_jid: key.remoteJid,
      },
    });
  }
}
