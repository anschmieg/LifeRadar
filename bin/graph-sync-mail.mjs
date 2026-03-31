#!/usr/bin/env node
import {
  env,
  fetchJson,
  getRuntimeMetadata,
  nowIso,
  postFormJson,
  runPsql,
  setRuntimeMetadata,
  sqlJson,
  sqlLiteral,
} from '../lib/runtime.mjs';

const clientId = env('MSGRAPH_CLIENT_ID');
const clientSecret = env('MSGRAPH_CLIENT_SECRET', '');
const tenantId = env('MSGRAPH_TENANT_ID');
const envRefreshToken = env('MSGRAPH_REFRESH_TOKEN');
const metadataKey = 'msgraph_mail_sync';
const authKey = 'msgraph_auth';
const fixtureDir = env('LIFE_RADAR_MSGRAPH_FIXTURE_DIR');

if (!fixtureDir && (!clientId || !tenantId || !envRefreshToken)) {
  console.log('life-radar msgraph sync skipped: credentials not configured');
  process.exit(0);
}

function escapeText(value) {
  return String(value ?? '').replace(/\u0000/g, '').slice(0, 120000);
}

function participantList(message) {
  const participants = [];
  const add = (entry, role) => {
    const email = entry?.emailAddress?.address?.trim();
    if (!email) return;
    participants.push({ role, email, name: entry.emailAddress?.name?.trim() || email });
  };
  add(message.from, 'from');
  for (const entry of message.toRecipients ?? []) add(entry, 'to');
  for (const entry of message.ccRecipients ?? []) add(entry, 'cc');
  const seen = new Set();
  return participants.filter((item) => {
    const key = `${item.role}:${item.email}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function conversationMetadata(message) {
  return {
    subject: message.subject ?? null,
    webLink: message.webLink ?? null,
    internetMessageId: message.internetMessageId ?? null,
    inferenceClassification: message.inferenceClassification ?? null,
    isRead: Boolean(message.isRead),
    categories: message.categories ?? [],
    folderId: message.parentFolderId ?? null,
    lastModifiedDateTime: message.lastModifiedDateTime ?? null,
    source: 'msgraph',
  };
}

function messageMetadata(message) {
  return {
    event_type: 'mail.message',
    is_read: Boolean(message.isRead),
    importance: message.importance ?? null,
    inferenceClassification: message.inferenceClassification ?? null,
    categories: message.categories ?? [],
    webLink: message.webLink ?? null,
    internetMessageId: message.internetMessageId ?? null,
  };
}

function mailIsHuman(senderEmail) {
  const value = (senderEmail || '').toLowerCase();
  return value && !/(^|[^a-z])(no-?reply|donotreply|do-not-reply|notification|notifications|mailer-daemon)@/.test(value);
}

async function loadFixture(name) {
  const { readFile } = await import('node:fs/promises');
  return JSON.parse(await readFile(`${fixtureDir}/${name}`, 'utf8'));
}

async function acquireToken() {
  const existing = getRuntimeMetadata(authKey) ?? {};
  const refreshToken = existing.refresh_token || envRefreshToken;
  if (fixtureDir) return loadFixture('token.json');
  const tokenRequest = {
    client_id: clientId,
    grant_type: 'refresh_token',
    refresh_token: refreshToken,
    scope: 'offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read',
  };
  if (clientSecret) tokenRequest.client_secret = clientSecret;
  const token = await postFormJson(`https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/token`, tokenRequest);
  setRuntimeMetadata(authKey, {
    provider: 'microsoft-graph',
    updated_at: nowIso(),
    refresh_token: token.refresh_token || refreshToken,
    expires_in: token.expires_in ?? null,
    scope: token.scope ?? null,
    token_type: token.token_type ?? null,
  });
  return token;
}

async function graphGet(url, accessToken) {
  if (fixtureDir) {
    if (url === 'me') return loadFixture('me.json');
    if (url === 'delta') return loadFixture('delta-page-1.json');
    throw new Error(`missing fixture for ${url}`);
  }
  return fetchJson(url, {
    headers: {
      authorization: `Bearer ${accessToken}`,
      prefer: 'outlook.body-content-type="text"',
    },
  });
}

function initialDeltaUrl() {
  const encoded = new URLSearchParams({
    '$select': 'id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,lastModifiedDateTime,bodyPreview,isRead,webLink,internetMessageId,inferenceClassification,categories,parentFolderId',
    '$orderby': 'receivedDateTime DESC',
    '$top': '50',
  });
  return `https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages/delta?${encoded.toString()}`;
}

function upsertMessage(accountId, meAddress, message) {
  if (!message.id || !message.conversationId) return;
  const senderEmail = message.from?.emailAddress?.address ?? '';
  const isInbound = !meAddress || senderEmail.toLowerCase() !== meAddress.toLowerCase();
  const contentText = escapeText(message.bodyPreview || message.subject || '');
  const participants = participantList(message);
  const convMeta = conversationMetadata(message);
  const msgMeta = messageMetadata(message);
  const title = escapeText(message.subject || participants.map((p) => p.name).join(', ') || 'Outlook thread').slice(0, 1000);
  runPsql(`
    insert into life_radar.conversations (
      source, external_id, account_id, title, participants, last_event_at, metadata
    ) values (
      'outlook',
      ${sqlLiteral(message.conversationId)},
      ${sqlLiteral(accountId)},
      ${sqlLiteral(title)},
      ${sqlJson(participants)},
      ${sqlLiteral(message.receivedDateTime || message.lastModifiedDateTime || nowIso())}::timestamptz,
      ${sqlJson(convMeta)}
    )
    on conflict (source, external_id) do update
    set account_id = excluded.account_id,
        title = excluded.title,
        participants = excluded.participants,
        last_event_at = greatest(coalesce(life_radar.conversations.last_event_at, to_timestamp(0)), excluded.last_event_at),
        metadata = life_radar.conversations.metadata || excluded.metadata,
        updated_at = now();

    insert into life_radar.message_events (
      conversation_id, source, external_id, sender_id, sender_label, occurred_at,
      content_text, content_json, is_inbound, provenance
    )
    select c.id,
           'outlook',
           ${sqlLiteral(message.id)},
           ${sqlLiteral(senderEmail)},
           ${sqlLiteral(message.from?.emailAddress?.name || senderEmail || 'Unknown sender')},
           ${sqlLiteral(message.receivedDateTime || message.lastModifiedDateTime || nowIso())}::timestamptz,
           ${sqlLiteral(contentText)},
           ${sqlJson(msgMeta)},
           ${isInbound ? 'true' : 'false'},
           ${sqlJson({ source: 'msgraph', mail_is_human: mailIsHuman(senderEmail) })}
    from life_radar.conversations c
    where c.source = 'outlook' and c.external_id = ${sqlLiteral(message.conversationId)}
    on conflict (source, external_id) do update
    set sender_id = excluded.sender_id,
        sender_label = excluded.sender_label,
        occurred_at = excluded.occurred_at,
        content_text = excluded.content_text,
        content_json = excluded.content_json,
        is_inbound = excluded.is_inbound,
        provenance = life_radar.message_events.provenance || excluded.provenance,
        updated_at = now();
  `);
}

function markRemoved(messageId) {
  if (!messageId) return;
  runPsql(`
    update life_radar.message_events
    set content_json = content_json || jsonb_build_object('removed', true),
        provenance = provenance || jsonb_build_object('removed_at', now()),
        updated_at = now()
    where source = 'outlook' and external_id = ${sqlLiteral(messageId)};
  `);
}

const token = await acquireToken();
const accessToken = token.access_token || 'fixture';
const me = await graphGet(fixtureDir ? 'me' : 'https://graph.microsoft.com/v1.0/me?$select=id,displayName,mail,userPrincipalName', accessToken);
const accountId = me.mail || me.userPrincipalName || me.id;
const meAddress = me.mail || me.userPrincipalName || '';
const syncState = getRuntimeMetadata(metadataKey) ?? {};
let pageUrl = fixtureDir ? 'delta' : (syncState.delta_link || initialDeltaUrl());
let pageCount = 0;
let messageCount = 0;
let removedCount = 0;
let latestAt = syncState.latest_occurred_at ?? null;
let deltaLink = syncState.delta_link ?? null;

while (pageUrl) {
  const page = await graphGet(pageUrl, accessToken);
  pageCount += 1;
  for (const item of page.value ?? []) {
    if (item['@removed']) {
      removedCount += 1;
      markRemoved(item.id);
      continue;
    }
    upsertMessage(accountId, meAddress, item);
    messageCount += 1;
    if (!latestAt || (item.receivedDateTime && item.receivedDateTime > latestAt)) latestAt = item.receivedDateTime;
  }
  pageUrl = page['@odata.nextLink'] ?? null;
  deltaLink = page['@odata.deltaLink'] ?? deltaLink;
  if (fixtureDir) break;
}

runPsql(`
  with latest_sender as (
    select distinct on (me.conversation_id)
      me.conversation_id,
      me.sender_label
    from life_radar.message_events me
    where me.source = 'outlook'
      and coalesce((me.content_json->>'removed')::boolean, false) = false
    order by me.conversation_id, me.occurred_at desc, me.id desc
  ),
  agg as (
    select me.conversation_id,
           max(me.occurred_at) as last_event_at,
           count(*) filter (where coalesce((me.content_json->>'removed')::boolean, false) = false) as message_count
    from life_radar.message_events me
    where me.source = 'outlook'
    group by me.conversation_id
  )
  update life_radar.conversations c
  set last_event_at = agg.last_event_at,
      metadata = c.metadata || jsonb_build_object(
        'message_count', agg.message_count,
        'latest_sender', ls.sender_label
      ),
      updated_at = now()
  from agg
  left join latest_sender ls on ls.conversation_id = agg.conversation_id
  where c.id = agg.conversation_id
    and c.source = 'outlook';
`);

setRuntimeMetadata(metadataKey, {
  provider: 'microsoft-graph',
  account_id: accountId,
  pages_processed: pageCount,
  messages_upserted: messageCount,
  removed_messages: removedCount,
  latest_occurred_at: latestAt,
  delta_link: deltaLink,
  updated_at: nowIso(),
});

console.log(`life-radar msgraph sync complete: upserted=${messageCount} removed=${removedCount}`);
