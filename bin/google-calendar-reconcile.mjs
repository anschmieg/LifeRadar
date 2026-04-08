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

const clientId = env('GOOGLE_CALENDAR_CLIENT_ID');
const clientSecret = env('GOOGLE_CALENDAR_CLIENT_SECRET');
const envRefreshToken = env('GOOGLE_CALENDAR_REFRESH_TOKEN');
const calendarId = encodeURIComponent(env('GOOGLE_CALENDAR_ID', 'primary'));
const authKey = 'google_calendar_auth';
const runtimeKey = 'google_calendar_reconcile';

if (!clientId || !clientSecret || !envRefreshToken) {
  console.log('life-radar google calendar reconcile skipped: credentials not configured');
  process.exit(0);
}

function eventForAction(action) {
  const start = new Date(action.scheduled_start);
  const end = action.scheduled_end ? new Date(action.scheduled_end) : new Date(start.getTime() + (action.effort_estimate_minutes || 60) * 60_000);
  return {
    summary: action.title,
    description: action.summary || '',
    start: { dateTime: start.toISOString() },
    end: { dateTime: end.toISOString() },
    extendedProperties: {
      private: {
        lifeRadarPlannedActionId: action.id,
        sourceEntityType: action.source_entity_type,
        sourceEntityId: action.source_entity_id || '',
      },
    },
  };
}

async function acquireToken() {
  const existing = getRuntimeMetadata(authKey) ?? {};
  const refreshToken = envRefreshToken || existing.refresh_token;
  const token = await postFormJson('https://oauth2.googleapis.com/token', {
    client_id: clientId,
    client_secret: clientSecret,
    grant_type: 'refresh_token',
    refresh_token: refreshToken,
  });
  setRuntimeMetadata(authKey, {
    provider: 'google-calendar',
    updated_at: nowIso(),
    refresh_token: token.refresh_token || refreshToken,
    expires_in: token.expires_in ?? null,
    scope: token.scope ?? null,
    token_type: token.token_type ?? null,
  });
  return token.access_token;
}

async function calendarRequest(path, accessToken, { method = 'GET', body } = {}) {
  return fetchJson(`https://www.googleapis.com/calendar/v3${path}`, {
    method,
    headers: {
      authorization: `Bearer ${accessToken}`,
      'content-type': 'application/json',
    },
    body: body ? JSON.stringify(body) : undefined,
  });
}

function fetchActions() {
  const sql = `
    select row_to_json(t)
    from (
      select id, source_entity_type, source_entity_id, title, summary, status,
             scheduled_start, scheduled_end, calendar_provider, calendar_external_id,
             effort_estimate_minutes, metadata
      from life_radar.planned_actions
      where (
        status in ('proposed', 'scheduled', 'ready')
        and scheduled_start is not null
        and (calendar_provider is null or calendar_provider = 'google-calendar')
      ) or (
        status = 'cancelled'
        and calendar_provider = 'google-calendar'
        and calendar_external_id is not null
      )
      order by scheduled_start nulls last, created_at
    ) t;
  `;
  const rows = runPsql(sql, { tuplesOnly: true }).split('\n').filter(Boolean);
  return rows.map((row) => JSON.parse(row));
}

function updateAction(actionId, fieldsSql) {
  runPsql(`update life_radar.planned_actions set ${fieldsSql}, updated_at = now() where id = ${sqlLiteral(actionId)}::uuid;`);
}

function upsertProjection(actionId, eventId, event) {
  runPsql(`
    insert into life_radar.external_projections (
      source_entity_type, source_entity_id, target_system, target_object_type,
      target_object_id, sync_state, last_synced_at, metadata
    ) values (
      'planned_action',
      ${sqlLiteral(actionId)}::uuid,
      'google-calendar',
      'event',
      ${sqlLiteral(eventId)},
      'active',
      now(),
      ${sqlJson({ htmlLink: event.htmlLink ?? null, status: event.status ?? null })}
    )
    on conflict (target_system, target_object_type, target_object_id) do update
    set sync_state = 'active',
        last_synced_at = now(),
        metadata = excluded.metadata,
        updated_at = now();
  `);
}

const accessToken = await acquireToken();
const actions = fetchActions();
let created = 0;
let updated = 0;
let cancelled = 0;

for (const action of actions) {
  if (action.status === 'cancelled' && action.calendar_external_id) {
    try {
      await calendarRequest(`/calendars/${calendarId}/events/${encodeURIComponent(action.calendar_external_id)}`, accessToken, { method: 'DELETE' });
    } catch (error) {
      if (!String(error.message).includes('HTTP 410') && !String(error.message).includes('HTTP 404')) throw error;
    }
    runPsql(`
      update life_radar.external_projections
      set sync_state = 'deleted', last_synced_at = now(), updated_at = now()
      where target_system = 'google-calendar'
        and target_object_type = 'event'
        and target_object_id = ${sqlLiteral(action.calendar_external_id)};
    `);
    cancelled += 1;
    continue;
  }

  const desired = eventForAction(action);
  if (!action.calendar_external_id) {
    const event = await calendarRequest(`/calendars/${calendarId}/events`, accessToken, { method: 'POST', body: desired });
    updateAction(action.id, `calendar_provider = 'google-calendar', calendar_external_id = ${sqlLiteral(event.id)}, status = 'scheduled', metadata = metadata || ${sqlJson({ calendar_html_link: event.htmlLink ?? null })}`);
    upsertProjection(action.id, event.id, event);
    created += 1;
    continue;
  }

  let event = null;
  try {
    event = await calendarRequest(`/calendars/${calendarId}/events/${encodeURIComponent(action.calendar_external_id)}`, accessToken);
  } catch (error) {
    if (!String(error.message).includes('HTTP 404')) throw error;
  }
  if (!event) {
    const recreated = await calendarRequest(`/calendars/${calendarId}/events`, accessToken, { method: 'POST', body: desired });
    updateAction(action.id, `calendar_provider = 'google-calendar', calendar_external_id = ${sqlLiteral(recreated.id)}, status = 'scheduled', metadata = metadata || ${sqlJson({ calendar_html_link: recreated.htmlLink ?? null, recreated: true })}`);
    upsertProjection(action.id, recreated.id, recreated);
    created += 1;
    continue;
  }

  const changed = event.summary !== desired.summary
    || event.description !== desired.description
    || event.start?.dateTime !== desired.start.dateTime
    || event.end?.dateTime !== desired.end.dateTime;
  if (changed) {
    const updatedEvent = await calendarRequest(`/calendars/${calendarId}/events/${encodeURIComponent(action.calendar_external_id)}`, accessToken, { method: 'PATCH', body: desired });
    upsertProjection(action.id, updatedEvent.id, updatedEvent);
    updated += 1;
  } else {
    upsertProjection(action.id, event.id, event);
  }
}

setRuntimeMetadata(runtimeKey, {
  provider: 'google-calendar',
  updated_at: nowIso(),
  actions_seen: actions.length,
  events_created: created,
  events_updated: updated,
  events_cancelled: cancelled,
});

console.log(`life-radar google calendar reconcile complete: created=${created} updated=${updated} cancelled=${cancelled}`);
