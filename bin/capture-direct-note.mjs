#!/usr/bin/env node
import { randomUUID } from 'node:crypto';
import { env, nowIso, runPsql, sqlJson, sqlLiteral } from '../lib/runtime.mjs';

const noteText = process.argv.slice(2).join(' ').trim() || env('LIFE_RADAR_NOTE_TEXT');
if (!noteText) {
  console.error('usage: capture-direct-note.mjs "<note text>"');
  process.exit(1);
}

const inboxId = env('LIFE_RADAR_DIRECT_NOTE_INBOX', 'direct-notes');
const userId = env('LIFE_RADAR_USER_ID', 'adrian');
const now = new Date();

function parseTimeParts(rawHour, rawMinute, meridiem) {
  let hour = Number(rawHour);
  const minute = rawMinute ? Number(rawMinute) : 0;
  if (Number.isNaN(hour) || Number.isNaN(minute)) return null;
  const suffix = (meridiem || '').toLowerCase();
  if (suffix === 'pm' && hour < 12) hour += 12;
  if (suffix === 'am' && hour === 12) hour = 0;
  return { hour, minute };
}

function parseScheduledStart(text) {
  const lower = text.toLowerCase();
  const dayOffset = lower.includes('tomorrow') ? 1 : (lower.includes('today') ? 0 : null);
  const timeMatch = lower.match(/\b(?:around\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b/);
  if (dayOffset === null && !timeMatch) return null;

  const scheduled = new Date(now);
  if (dayOffset !== null) scheduled.setDate(scheduled.getDate() + dayOffset);
  if (timeMatch) {
    const parsed = parseTimeParts(timeMatch[1], timeMatch[2], timeMatch[3]);
    if (parsed) {
      scheduled.setHours(parsed.hour, parsed.minute, 0, 0);
    }
  } else {
    scheduled.setHours(18, 0, 0, 0);
  }
  return scheduled.toISOString();
}

function classifyNote(text) {
  const lower = text.toLowerCase();
  const scheduledStart = parseScheduledStart(text);
  const isCall = /\b(call|phone|ring)\b/.test(lower);
  const isBuy = /\b(buy|get|purchase|order)\b/.test(lower);
  const title = text.replace(/\s+/g, ' ').trim().slice(0, 180);

  if (scheduledStart && isCall) {
    return {
      type: 'planned_action',
      title,
      summary: 'Created from direct note',
      scheduledStart,
      scheduledEnd: new Date(new Date(scheduledStart).getTime() + 30 * 60_000).toISOString(),
      effortMinutes: 30,
      rewardValue: 0.35,
      energyFit: 0.55,
      reminderAt: new Date(new Date(scheduledStart).getTime() - 30 * 60_000).toISOString(),
      reminderReason: 'Time-bound personal follow-up',
      decisionContext: null,
    };
  }

  if (isBuy) {
    const remindAt = new Date(now);
    remindAt.setDate(remindAt.getDate() + 2);
    remindAt.setHours(18, 0, 0, 0);
    return {
      type: 'reminder',
      title,
      summary: 'Created from direct note',
      remindAt: remindAt.toISOString(),
      cadenceProfile: 'adaptive',
      effortMinutes: 20,
      rewardValue: 0.45,
      energyFit: 0.4,
      decisionContext: {
        type: 'candidate_prep',
        title: `Prepare options for: ${title}`,
        summary: 'Collect short list of candidate options before reminder fires.',
      },
    };
  }

  const remindAt = new Date(now);
  remindAt.setDate(remindAt.getDate() + 1);
  remindAt.setHours(18, 0, 0, 0);
  return {
    type: 'reminder',
    title,
    summary: 'Created from direct note',
    remindAt: remindAt.toISOString(),
    cadenceProfile: 'adaptive',
    effortMinutes: 15,
    rewardValue: 0.3,
    energyFit: 0.5,
    decisionContext: null,
  };
}

const note = classifyNote(noteText);
const directConversationTitle = 'Direct notes';
const actionId = randomUUID();
const reminderId = randomUUID();
const contextId = randomUUID();

runPsql(`
  insert into life_radar.conversations (
    source, external_id, account_id, title, participants, last_event_at, metadata
  ) values (
    'direct_note',
    ${sqlLiteral(inboxId)},
    ${sqlLiteral(userId)},
    ${sqlLiteral(directConversationTitle)},
    ${sqlJson([{ role: 'user', id: userId, label: userId }])},
    ${sqlLiteral(nowIso())}::timestamptz,
    ${sqlJson({ source: 'direct_note', managed: true })}
  )
  on conflict (source, external_id) do update
  set last_event_at = excluded.last_event_at,
      updated_at = now();

  insert into life_radar.message_events (
    conversation_id, source, external_id, sender_id, sender_label, occurred_at,
    content_text, content_json, is_inbound, provenance
  )
  select c.id,
         'direct_note',
         ${sqlLiteral(randomUUID())},
         ${sqlLiteral(userId)},
         ${sqlLiteral('Direct note')},
         ${sqlLiteral(nowIso())}::timestamptz,
         ${sqlLiteral(noteText)},
         ${sqlJson({ event_type: 'direct.note' })},
         true,
         ${sqlJson({ source: 'direct_note', lifecycle: 'captured' })}
  from life_radar.conversations c
  where c.source = 'direct_note' and c.external_id = ${sqlLiteral(inboxId)};
`);

if (note.type === 'planned_action') {
  runPsql(`
    insert into life_radar.planned_actions (
      id, source_entity_type, title, summary, status, scheduled_start, scheduled_end,
      effort_estimate_minutes, reward_value, energy_fit, metadata
    ) values (
      ${sqlLiteral(actionId)}::uuid,
      'direct_note',
      ${sqlLiteral(note.title)},
      ${sqlLiteral(note.summary)},
      'ready',
      ${sqlLiteral(note.scheduledStart)}::timestamptz,
      ${sqlLiteral(note.scheduledEnd)}::timestamptz,
      ${note.effortMinutes},
      ${note.rewardValue},
      ${note.energyFit},
      ${sqlJson({ source: 'direct_note', note_text: noteText })}
    );

    insert into life_radar.reminders (
      id, source_entity_type, source_entity_id, title, summary, status, remind_at,
      remind_channel, timing_reason, cadence_profile, effort_estimate_minutes, metadata
    ) values (
      ${sqlLiteral(reminderId)}::uuid,
      'planned_action',
      ${sqlLiteral(actionId)}::uuid,
      ${sqlLiteral(note.title)},
      ${sqlLiteral('Reminder for planned action created from direct note')},
      'scheduled',
      ${sqlLiteral(note.reminderAt)}::timestamptz,
      'telegram',
      ${sqlLiteral(note.reminderReason)},
      'time-bound',
      ${note.effortMinutes},
      ${sqlJson({ source: 'direct_note' })}
    );
  `);
} else {
  runPsql(`
    insert into life_radar.reminders (
      id, source_entity_type, title, summary, status, remind_at, remind_channel,
      timing_reason, cadence_profile, effort_estimate_minutes, metadata
    ) values (
      ${sqlLiteral(reminderId)}::uuid,
      'direct_note',
      ${sqlLiteral(note.title)},
      ${sqlLiteral(note.summary)},
      'scheduled',
      ${sqlLiteral(note.remindAt)}::timestamptz,
      'telegram',
      'Captured from direct note',
      ${sqlLiteral(note.cadenceProfile)},
      ${note.effortMinutes},
      ${sqlJson({ source: 'direct_note', note_text: noteText })}
    );
  `);
}

if (note.decisionContext) {
  runPsql(`
    insert into life_radar.decision_contexts (
      id, source_entity_type, title, summary, context_json, prepared_at
    ) values (
      ${sqlLiteral(contextId)}::uuid,
      'direct_note',
      ${sqlLiteral(note.decisionContext.title)},
      ${sqlLiteral(note.decisionContext.summary)},
      ${sqlJson({ source: 'direct_note', note_text: noteText, kind: note.decisionContext.type })},
      null
    );
  `);
}

console.log(JSON.stringify({
  captured: true,
  type: note.type,
  title: note.title,
  planned_action_id: note.type === 'planned_action' ? actionId : null,
  reminder_id: reminderId,
  decision_context: Boolean(note.decisionContext),
}, null, 2));
