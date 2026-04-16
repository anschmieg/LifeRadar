#!/usr/bin/env node
import { spawnSync } from 'node:child_process';

export function env(name, fallback = '', { required = false } = {}) {
  const value = process.env[name] ?? fallback;
  if (required && !value) throw new Error(`Missing required env: ${name}`);
  return value;
}

export function maybeJson(text, fallback = null) {
  if (!text) return fallback;
  try { return JSON.parse(text); } catch { return fallback; }
}

export function sqlLiteral(value) {
  if (value === null || value === undefined) return 'NULL';
  return `'${String(value).replace(/'/g, "''")}'`;
}

export function sqlJson(value) {
  return `${sqlLiteral(JSON.stringify(value ?? {}))}::jsonb`;
}

export function dbConfig() {
  return {
    host: env('LIFERADAR_DB_HOST', 'liferadar-db'),
    port: env('LIFERADAR_DB_PORT', '5432'),
    name: env('LIFERADAR_DB_NAME', 'life_radar'),
    user: env('LIFERADAR_DB_USER', 'life_radar'),
    password: env('LIFERADAR_DB_PASSWORD', '', { required: true }),
  };
}

export function runPsql(sql, { tuplesOnly = false } = {}) {
  const cfg = dbConfig();
  const args = ['--host', cfg.host, '--port', cfg.port, '--username', cfg.user, '--dbname', cfg.name, '--set', 'ON_ERROR_STOP=1'];
  if (tuplesOnly) args.push('--tuples-only', '--no-align');
  const proc = spawnSync('psql', args, {
    input: sql,
    encoding: 'utf8',
    env: { ...process.env, PGPASSWORD: cfg.password },
  });
  if (proc.status !== 0) throw new Error((proc.stderr || proc.stdout || 'psql failed').trim());
  return proc.stdout.trim();
}

export function getRuntimeMetadata(key) {
  const out = runPsql(`select value::text from life_radar.runtime_metadata where key = ${sqlLiteral(key)};`, { tuplesOnly: true });
  return maybeJson(out, null);
}

export function setRuntimeMetadata(key, value) {
  runPsql(`
    insert into life_radar.runtime_metadata (key, value)
    values (${sqlLiteral(key)}, ${sqlJson(value)})
    on conflict (key) do update
    set value = excluded.value,
        updated_at = now();
  `);
}

export async function postFormJson(url, form) {
  const body = new URLSearchParams();
  for (const [k, v] of Object.entries(form)) {
    if (v !== undefined && v !== null && v !== '') body.set(k, String(v));
  }
  const response = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body });
  const text = await response.text();
  const json = maybeJson(text, null);
  if (!response.ok) throw new Error(`HTTP ${response.status} from ${url}: ${text}`);
  return json ?? {};
}

export async function fetchJson(url, { headers = {}, method = 'GET', body } = {}) {
  const response = await fetch(url, { method, headers, body });
  const text = await response.text();
  const json = maybeJson(text, null);
  if (!response.ok) throw new Error(`HTTP ${response.status} from ${url}: ${text}`);
  return json ?? {};
}

export function nowIso() {
  return new Date().toISOString();
}
