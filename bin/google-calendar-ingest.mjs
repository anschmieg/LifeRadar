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
const authKey = 'google_calendar_auth';
const runtimeKey = 'google_calendar_ingest';
const lookbackDays = Number.parseInt(env('GOOGLE_CALENDAR_LOOKBACK_DAYS', '30'), 10);
const lookaheadDays = Number.parseInt(env('GOOGLE_CALENDAR_LOOKAHEAD_DAYS', '60'), 10);

if (!clientId || !clientSecret || !envRefreshToken) {
  console.log('life-radar google calendar ingest skipped: credentials not configured');
  process.exit(0);
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

async function calendarRequest(path, accessToken) {
  return fetchJson(`https://www.googleapis.com/calendar/v3${path}`, {
    headers: {
      authorization: `Bearer ${accessToken}`,
      'content-type': 'application/json',
    },
  });
}

function isoAtUtcMidnight(dateString) {
  return `${dateString}T00:00:00Z`;
}

function normalizeStart(event) {
  if (event.start?.dateTime) return event.start.dateTime;
  if (event.start?.date) return isoAtUtcMidnight(event.start.date);
  return null;
}

function normalizeEnd(event, fallbackStart) {
  if (event.end?.dateTime) return event.end.dateTime;
  if (event.end?.date) return isoAtUtcMidnight(event.end.date);
  return fallbackStart;
}

function eventStatus(event) {
  if (event.status === 'cancelled') return 'cancelled';
  if (normalizeStart(event)) return 'scheduled';
  return 'proposed';
}

function shouldSkip(event) {
  return Boolean(event.extendedProperties?.private?.lifeRadarPlannedActionId);
}

function externalId(calendar, event) {
  return `${calendar.id}:${event.id}`;
}

const accessToken = await acquireToken();
const now = new Date();
const timeMin = new Date(now.getTime() - lookbackDays * 24 * 60 * 60 * 1000).toISOString();
const timeMax = new Date(now.getTime() + lookaheadDays * 24 * 60 * 60 * 1000).toISOString();
const calendars = (await calendarRequest('/users/me/calendarList', accessToken)).items ?? [];

let imported = 0;
let skipped = 0;

for (const calendar of calendars) {
  const params = new URLSearchParams({
    singleEvents: 'true',
    orderBy: 'startTime',
    timeMin,
    timeMax,
    maxResults: '250',
  });
  const calendarId = encodeURIComponent(calendar.id);
  const events = (await calendarRequest(`/calendars/${calendarId}/events?${params.toString()}`, accessToken)).items ?? [];

  for (const event of events) {
    if (!event.id || shouldSkip(event)) {
      skipped += 1;
      continue;
    }

    const scheduledStart = normalizeStart(event);
    const scheduledEnd = normalizeEnd(event, scheduledStart);
    const metadata = {
      source: 'google-calendar-import',
      calendar_id: calendar.id,
      calendar_summary: calendar.summary ?? null,
      html_link: event.htmlLink ?? null,
      status: event.status ?? null,
      organizer: event.organizer?.email ?? null,
      creator: event.creator?.email ?? null,
      attendees: event.attendees ?? [],
      event_type: event.eventType ?? null,
      visibility: event.visibility ?? null,
      all_day: Boolean(event.start?.date && !event.start?.dateTime),
    };

    runPsql(`
      insert into life_radar.planned_actions (
        source_entity_type,
        source_entity_id,
        title,
        summary,
        status,
        scheduled_start,
        scheduled_end,
        calendar_provider,
        calendar_external_id,
        metadata
      ) values (
        'calendar',
        ${sqlLiteral(calendar.id)},
        ${sqlLiteral(event.summary || '(Untitled event)')},
        ${sqlLiteral(event.description || null)},
        ${sqlLiteral(eventStatus(event))},
        ${sqlLiteral(scheduledStart)}::timestamptz,
        ${sqlLiteral(scheduledEnd)}::timestamptz,
        'google-calendar',
        ${sqlLiteral(externalId(calendar, event))},
        ${sqlJson(metadata)}
      )
      on conflict (calendar_external_id) do update
      set
        source_entity_type = 'calendar',
        source_entity_id = excluded.source_entity_id,
        title = excluded.title,
        summary = excluded.summary,
        status = excluded.status,
        scheduled_start = excluded.scheduled_start,
        scheduled_end = excluded.scheduled_end,
        calendar_provider = excluded.calendar_provider,
        metadata = coalesce(life_radar.planned_actions.metadata, '{}'::jsonb) || excluded.metadata,
        updated_at = now();
    `);
    imported += 1;
  }
}

setRuntimeMetadata(runtimeKey, {
  provider: 'google-calendar',
  updated_at: nowIso(),
  calendars_seen: calendars.length,
  events_imported: imported,
  events_skipped: skipped,
  time_min: timeMin,
  time_max: timeMax,
});

console.log(`life-radar google calendar ingest complete: imported=${imported} skipped=${skipped}`);
