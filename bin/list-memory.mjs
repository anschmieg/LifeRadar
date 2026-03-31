#!/usr/bin/env node
import { env, runPsql, sqlLiteral } from '../lib/runtime.mjs';

const kind = process.argv[2] || env('LIFE_RADAR_MEMORY_KIND', '');
const limit = Number(process.argv[3] || env('LIFE_RADAR_MEMORY_LIMIT', '20'));

const where = [
  'active = true',
];
if (kind) where.push(`kind = ${sqlLiteral(kind)}`);

const sql = `
  select coalesce(json_agg(t), '[]'::json)
  from (
    select id, kind, subject_type, subject_key, title, summary, detail,
           sensitivity, confidence, updated_at
    from life_radar.memory_records
    where ${where.join(' and ')}
    order by updated_at desc, created_at desc
    limit ${Number.isFinite(limit) ? Math.max(1, Math.min(limit, 100)) : 20}
  ) t;
`;

const rows = JSON.parse(runPsql(sql, { tuplesOnly: true }) || '[]');
console.log(JSON.stringify(rows, null, 2));
