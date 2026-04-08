mod adapter;

use std::{
    env, fs,
    path::PathBuf,
    time::{Duration, Instant},
};

use adapter::{
    build_room_info, parse_timeline_event as adapt_parse_timeline_event, EventClassification,
    EventCountSummary, IngestEvent,
};
use anyhow::{Context, Result};
use matrix_sdk::{
    authentication::matrix::MatrixSession,
    config::SyncSettings,
    room::MessagesOptions,
    ruma::{
        events::room::message::RoomMessageEventContent, uint, OwnedDeviceId, OwnedRoomId,
        OwnedUserId,
    },
    store::RoomLoadSettings,
    Client, RoomMemberships, SessionMeta, SessionTokens,
};
use reqwest::Client as HttpClient;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio_postgres::{Client as PgClient, NoTls};

#[derive(Debug, Deserialize)]
struct SessionFile {
    access_token: String,
    user_id: String,
    device_id: String,
    homeserver: String,
    next_batch: Option<String>,
}

struct ProbeConfig {
    db_host: String,
    db_port: u16,
    db_name: String,
    db_user: String,
    db_password: String,
    candidate_id: String,
    candidate_type: String,
    session_path: PathBuf,
    store_path: PathBuf,
    key_export_path: PathBuf,
    key_passphrase_path: PathBuf,
    key_import_marker_path: PathBuf,
    report_dir: PathBuf,
    timeout_seconds: u64,
    mode: String,
    inspect_room_id: Option<String>,
    recent_room_limit: usize,
    recent_message_limit: u32,
}

impl ProbeConfig {
    fn from_env() -> Result<Self> {
        Ok(Self {
            db_host: env_var("LIFE_RADAR_DB_HOST", "life-radar-db"),
            db_port: env_var("LIFE_RADAR_DB_PORT", "5432")
                .parse()
                .context("invalid LIFE_RADAR_DB_PORT")?,
            db_name: env_var("LIFE_RADAR_DB_NAME", "life_radar"),
            db_user: env_var("LIFE_RADAR_DB_USER", "life_radar"),
            db_password: env_var("LIFE_RADAR_DB_PASSWORD", "change-me-in-env"),
            candidate_id: env_var("LIFE_RADAR_MATRIX_RUST_CANDIDATE_ID", "matrix-rust-sdk"),
            candidate_type: env_var("LIFE_RADAR_MATRIX_RUST_CANDIDATE_TYPE", "matrix-native"),
            session_path: PathBuf::from(env_var(
                "MATRIX_RUST_SESSION_PATH",
                "/app/identity/matrix-session.json",
            )),
            store_path: PathBuf::from(env_var(
                "MATRIX_RUST_STORE",
                "/app/identity/matrix-rust-sdk-store",
            )),
            key_export_path: PathBuf::from(env_var(
                "MATRIX_E2EE_EXPORT_PATH",
                "/app/identity/beeper-e2e-keys.txt",
            )),
            key_passphrase_path: PathBuf::from(env_var(
                "MATRIX_E2EE_EXPORT_PASSPHRASE_PATH",
                "/app/identity/.e2ee-export-passphrase",
            )),
            key_import_marker_path: PathBuf::from(env_var(
                "MATRIX_RUST_KEY_IMPORT_MARKER",
                "/app/identity/matrix-rust-sdk-store/room-key-import-marker.json",
            )),
            report_dir: PathBuf::from(env_var(
                "LIFE_RADAR_REPORT_DIR",
                "/app/workspace/life-radar/reports",
            )),
            timeout_seconds: env_var("LIFE_RADAR_MATRIX_RUST_TIMEOUT_SEC", "20")
                .parse()
                .context("invalid LIFE_RADAR_MATRIX_RUST_TIMEOUT_SEC")?,
            mode: env_var("LIFE_RADAR_MATRIX_RUST_MODE", "probe"),
            inspect_room_id: env::var("LIFE_RADAR_MATRIX_RUST_ROOM_ID").ok(),
            recent_room_limit: env_var("LIFE_RADAR_MATRIX_RUST_RECENT_ROOM_LIMIT", "5")
                .parse()
                .context("invalid LIFE_RADAR_MATRIX_RUST_RECENT_ROOM_LIMIT")?,
            recent_message_limit: env_var("LIFE_RADAR_MATRIX_RUST_RECENT_MESSAGE_LIMIT", "10")
                .parse()
                .context("invalid LIFE_RADAR_MATRIX_RUST_RECENT_MESSAGE_LIMIT")?,
        })
    }
}

struct ProbeResult {
    status: &'static str,
    notes: String,
    latency_ms: i32,
    freshness_seconds: i32,
    total_events: i64,
    decrypt_failures: i64,
    encrypted_non_text: i64,
    running_processes: i32,
    metadata_json: String,
    report_body: String,
}

struct DecryptionSample {
    sampled_rooms: usize,
    sampled_events: i64,
    decrypted_text_events: i64,
    decrypted_non_text_events: i64,
    undecrypted_events: i64,
    plain_text_events: i64,
    unsupported_custom_events: i64,
    freshest_timestamp_ms: Option<u64>,
    room_summaries: Vec<String>,
    room_errors: Vec<String>,
}

#[derive(Debug)]
struct ImportOutcome {
    status: &'static str,
    detail: String,
    imported_count: usize,
    total_count: usize,
}

#[derive(Debug, Deserialize, Serialize)]
struct ImportMarker {
    source_size: u64,
    source_mtime_epoch: u64,
    passphrase_mtime_epoch: u64,
    imported_count: usize,
    total_count: usize,
    imported_at: String,
}

#[derive(Debug, Serialize)]
struct RecentRoomsReport {
    rooms: Vec<RecentRoom>,
}

#[derive(Debug, Serialize)]
struct RecentRoom {
    room_id: String,
    room_name: String,
    latest_timestamp_ms: u64,
    messages: Vec<RecentMessage>,
}

#[derive(Debug, Serialize)]
struct RecentMessage {
    event_id: String,
    sender: String,
    occurred_at: String,
    kind: String,
    decrypted: bool,
    body: String,
}

#[derive(Debug, Serialize)]
struct InspectRoomReport {
    requested_room_id: String,
    sync_source: String,
    room_found: bool,
    joined_room_count: usize,
    known_room_count: usize,
    room: Option<RecentRoom>,
}

#[derive(Debug)]
struct PersistedRoomState {
    last_event_ts_ms: Option<u64>,
    backfill_complete: bool,
    metadata: Value,
}

#[derive(Debug, Serialize)]
struct SendMessageResult {
    status: &'static str,
    event_id: String,
}

const MATRIX_SYNC_CHECKPOINT_KEY: &str = "matrix_sync_checkpoint";

#[tokio::main]
async fn main() -> Result<()> {
    let cfg = ProbeConfig::from_env()?;
    let observed_at = iso_now();
    if cfg.mode == "inspect_recent" {
        let report = inspect_recent_rooms(&cfg).await?;
        println!(
            "{}",
            serde_json::to_string_pretty(&report)
                .context("failed to serialize recent room report")?
        );
        return Ok(());
    }
    if cfg.mode == "inspect_room" {
        let report = inspect_room(&cfg).await?;
        println!(
            "{}",
            serde_json::to_string_pretty(&report)
                .context("failed to serialize inspect room report")?
        );
        return Ok(());
    }
    if cfg.mode == "ingest_live_history" {
        match ingest_live_history(&cfg).await {
            Ok(result) => println!("{result}"),
            Err(err) => {
                let failure = failure_result(&cfg, &observed_at, err);
                let _ = persist_probe(&cfg, &observed_at, &failure).await;
                return Err(anyhow::anyhow!(failure.notes));
            }
        }
        return Ok(());
    }
    if cfg.mode == "recover_http" {
        match ingest_via_http(&cfg).await {
            Ok(result) => println!("{result}"),
            Err(err) => {
                let failure = failure_result(&cfg, &observed_at, err);
                let _ = persist_probe(&cfg, &observed_at, &failure).await;
                return Err(anyhow::anyhow!(failure.notes));
            }
        }
        return Ok(());
    }
    if cfg.mode == "send_message" {
        let result = send_message_via_sdk(&cfg).await?;
        println!(
            "{}",
            serde_json::to_string(&result).context("failed to serialize send result")?
        );
        return Ok(());
    }

    let result = match run_probe(&cfg).await {
        Ok(result) => result,
        Err(err) => failure_result(&cfg, &observed_at, err),
    };

    persist_probe(&cfg, &observed_at, &result).await?;
    write_report(&cfg, &result.report_body)?;

    println!("{}\t{}", cfg.candidate_id, result.status);
    Ok(())
}

async fn run_probe(cfg: &ProbeConfig) -> Result<ProbeResult> {
    let (client, session_file) = build_client(cfg).await?;

    let import_outcome = maybe_import_room_keys(cfg, &client).await?;

    let mut settings = SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds));
    if let Some(token) = session_file.next_batch.clone() {
        settings = settings.token(token);
    }

    let start = Instant::now();
    let response = client
        .sync_once(settings)
        .await
        .context("matrix rust sync_once failed")?;
    let latency_ms = i32::try_from(start.elapsed().as_millis()).unwrap_or(i32::MAX);

    let joined_room_count = client.joined_rooms().len() as i64;
    let invited_room_count = response.rooms.invited.len() as i64;
    let knocked_room_count = response.rooms.knocked.len() as i64;
    let presence_count = response.presence.len() as i64;
    let to_device_count = response.to_device.len() as i64;
    let next_batch_known = !response.next_batch.is_empty();
    let decrypt_sample = sample_recent_messages(&client).await;
    let freshest_timestamp_ms = decrypt_sample.freshest_timestamp_ms.unwrap_or_default();
    let freshness_seconds = if freshest_timestamp_ms > 0 {
        now_unix_ms()
            .saturating_sub(freshest_timestamp_ms)
            .checked_div(1000)
            .unwrap_or_default()
            .min(i32::MAX as u64) as i32
    } else {
        0
    };

    let status = if decrypt_sample.decrypted_text_events > 0 {
        "ok"
    } else if joined_room_count > 0 || presence_count > 0 || to_device_count > 0 {
        "warn"
    } else {
        "warn"
    };

    let notes = if decrypt_sample.decrypted_text_events > 0 {
        format!(
            "sync_once succeeded; joined_rooms={joined_room_count}; decrypted_text_events={}; sampled_rooms={}; undecrypted_events={}; unsupported_custom_events={}; room_key_import={}",
            decrypt_sample.decrypted_text_events,
            decrypt_sample.sampled_rooms,
            decrypt_sample.undecrypted_events,
            decrypt_sample.unsupported_custom_events,
            import_outcome.detail
        )
    } else if joined_room_count > 0 || presence_count > 0 || to_device_count > 0 {
        format!(
            "sync_once succeeded; joined_rooms={joined_room_count}; sampled_rooms={}; decrypted_text_events=0; undecrypted_events={}; unsupported_custom_events={}; room_key_import={}",
            decrypt_sample.sampled_rooms,
            decrypt_sample.undecrypted_events,
            decrypt_sample.unsupported_custom_events,
            import_outcome.detail
        )
    } else {
        format!(
            "sync_once succeeded but no active rooms/events were surfaced; room_key_import={}",
            import_outcome.detail
        )
    };

    let metadata_json = serde_json::json!({
        "homeserver": session_file.homeserver,
        "session_path": cfg.session_path,
        "store_path": cfg.store_path,
        "joined_rooms": joined_room_count,
        "invite_rooms": invited_room_count,
        "knocked_rooms": knocked_room_count,
        "presence_events": presence_count,
        "to_device_events": to_device_count,
        "room_key_import_status": import_outcome.status,
        "room_key_import_detail": import_outcome.detail,
        "room_key_imported_count": import_outcome.imported_count,
        "room_key_total_count": import_outcome.total_count,
        "next_batch_known": next_batch_known,
        "sampled_rooms": decrypt_sample.sampled_rooms,
        "sampled_events": decrypt_sample.sampled_events,
        "decrypted_text_events": decrypt_sample.decrypted_text_events,
        "decrypted_non_text_events": decrypt_sample.decrypted_non_text_events,
        "undecrypted_events": decrypt_sample.undecrypted_events,
        "plain_text_events": decrypt_sample.plain_text_events,
        "unsupported_custom_events": decrypt_sample.unsupported_custom_events,
        "freshest_timestamp_ms": decrypt_sample.freshest_timestamp_ms,
        "room_summaries": &decrypt_sample.room_summaries,
        "room_errors": &decrypt_sample.room_errors,
    })
    .to_string();

    let report_body = format!(
        "# Matrix Candidate Report\n\n- observed_at: {}\n- candidate_id: {}\n- candidate_type: {}\n- status: {}\n- notes: {}\n- latency_ms: {}\n- freshness_seconds: {}\n- joined_rooms: {}\n- invite_rooms: {}\n- knocked_rooms: {}\n- presence_events: {}\n- to_device_events: {}\n- room_key_import_status: {}\n- room_key_import_detail: {}\n- room_key_imported_count: {}\n- room_key_total_count: {}\n- sampled_rooms: {}\n- sampled_events: {}\n- decrypted_text_events: {}\n- decrypted_non_text_events: {}\n- undecrypted_events: {}\n- plain_text_events: {}\n- unsupported_custom_events: {}\n- session_path: {}\n- store_path: {}\n- homeserver: {}\n{}\n{}\n",
        iso_now(),
        cfg.candidate_id,
        cfg.candidate_type,
        status,
        notes,
        latency_ms,
        freshness_seconds,
        joined_room_count,
        invited_room_count,
        knocked_room_count,
        presence_count,
        to_device_count,
        import_outcome.status,
        import_outcome.detail,
        import_outcome.imported_count,
        import_outcome.total_count,
        decrypt_sample.sampled_rooms,
        decrypt_sample.sampled_events,
        decrypt_sample.decrypted_text_events,
        decrypt_sample.decrypted_non_text_events,
        decrypt_sample.undecrypted_events,
        decrypt_sample.plain_text_events,
        decrypt_sample.unsupported_custom_events,
        cfg.session_path.display(),
        cfg.store_path.display(),
        client.homeserver(),
        format_report_list("room_summaries", &decrypt_sample.room_summaries),
        format_report_list("room_errors", &decrypt_sample.room_errors),
    );

    Ok(ProbeResult {
        status,
        notes,
        latency_ms,
        freshness_seconds,
        total_events: decrypt_sample.sampled_events,
        decrypt_failures: decrypt_sample.undecrypted_events,
        encrypted_non_text: decrypt_sample.decrypted_non_text_events,
        running_processes: 1,
        metadata_json,
        report_body,
    })
}

async fn inspect_recent_rooms(cfg: &ProbeConfig) -> Result<RecentRoomsReport> {
    let (client, session_file) = build_client(cfg).await?;
    maybe_import_room_keys(cfg, &client).await?;

    let sync_attempts = build_sync_attempts(None, session_file.next_batch.clone());
    let mut last_sync_error = None;
    let mut synced = false;

    for (source, token) in sync_attempts {
        let mut settings =
            SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds));
        if let Some(token) = token {
            settings = settings.token(token);
        }

        match client.sync_once(settings).await {
            Ok(_) => {
                eprintln!("inspect_recent sync_once succeeded via {source}");
                synced = true;
                break;
            }
            Err(err) => {
                eprintln!("inspect_recent sync_once attempt {source} failed: {err:#}");
                last_sync_error = Some(format!("{err:#}"));
            }
        }
    }

    if !synced {
        return Err(anyhow::anyhow!(
            "matrix rust sync_once failed in inspect_recent mode after retries: {}",
            last_sync_error.unwrap_or_else(|| "no sync attempt executed".to_string())
        ));
    }

    let mut rooms = Vec::new();

    for room in client.joined_rooms() {
        let mut options = MessagesOptions::backward();
        options.limit = uint!(20);

        let messages = match room.messages(options).await {
            Ok(messages) => messages,
            Err(_) => continue,
        };

        let mut recent_messages = Vec::new();
        let mut latest_timestamp_ms = 0_u64;

        for event in messages.chunk.into_iter() {
            let raw_json =
                serde_json::from_str::<Value>(event.raw().json().get()).unwrap_or(Value::Null);
            let event_type = raw_json.get("type").and_then(Value::as_str).unwrap_or("");
            let ts_ms = raw_json
                .get("origin_server_ts")
                .and_then(Value::as_u64)
                .unwrap_or_default();
            latest_timestamp_ms = latest_timestamp_ms.max(ts_ms);

            let body = raw_json
                .get("content")
                .and_then(|content| content.get("body"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            let msgtype = raw_json
                .get("content")
                .and_then(|content| content.get("msgtype"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let event_id = raw_json
                .get("event_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let sender = raw_json
                .get("sender")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let decrypted = event.encryption_info().is_some();
            let rendered_body = adapt_parse_timeline_event(
                raw_json.clone(),
                decrypted,
                client.user_id().as_deref().map(|id| id.as_str()),
                "matrix_rust_probe",
            )
            .map(|parsed| parsed.content_text)
            .unwrap_or_else(|| {
                if body.is_empty() {
                    "[non-text event]".to_string()
                } else {
                    body
                }
            });
            let kind = if msgtype.is_empty() {
                event_type.to_string()
            } else {
                format!("{event_type}/{msgtype}")
            };

            recent_messages.push(RecentMessage {
                event_id,
                sender,
                occurred_at: iso_from_unix_ms(ts_ms),
                kind,
                decrypted,
                body: rendered_body,
            });
        }

        if recent_messages.is_empty() {
            continue;
        }

        recent_messages.sort_by(|left, right| left.occurred_at.cmp(&right.occurred_at));
        recent_messages.truncate(cfg.recent_message_limit as usize);

        let room_name = resolve_room_name(&room, client.user_id()).await;

        rooms.push(RecentRoom {
            room_id: room.room_id().to_string(),
            room_name,
            latest_timestamp_ms,
            messages: recent_messages,
        });
    }

    rooms.sort_by(|left, right| right.latest_timestamp_ms.cmp(&left.latest_timestamp_ms));
    rooms.truncate(cfg.recent_room_limit);

    Ok(RecentRoomsReport { rooms })
}

async fn inspect_room(cfg: &ProbeConfig) -> Result<InspectRoomReport> {
    let requested_room_id = cfg
        .inspect_room_id
        .clone()
        .context("missing LIFE_RADAR_MATRIX_RUST_ROOM_ID for inspect_room mode")?;
    let room_id: OwnedRoomId = requested_room_id
        .parse()
        .context("invalid LIFE_RADAR_MATRIX_RUST_ROOM_ID")?;

    let (client, session_file) = build_client(cfg).await?;
    maybe_import_room_keys(cfg, &client).await?;

    let sync_attempts = build_sync_attempts(None, session_file.next_batch.clone());
    let mut last_sync_error = None;
    let mut sync_source = "none".to_string();

    for (source, token) in sync_attempts {
        let mut settings =
            SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds));
        if let Some(token) = token {
            settings = settings.token(token);
        }

        match client.sync_once(settings).await {
            Ok(_) => {
                sync_source = source.to_string();
                break;
            }
            Err(err) => {
                eprintln!("inspect_room sync_once attempt {source} failed: {err:#}");
                last_sync_error = Some(format!("{err:#}"));
            }
        }
    }

    if sync_source == "none" {
        return Err(anyhow::anyhow!(
            "matrix rust sync_once failed in inspect_room mode after retries: {}",
            last_sync_error.unwrap_or_else(|| "no sync attempt executed".to_string())
        ));
    }

    let joined_room_count = client.joined_rooms().len();
    let known_room_count = client.rooms().len();
    let self_user_id = client.user_id();

    let room = match client.get_room(&room_id) {
        Some(room) => room,
        None => {
            return Ok(InspectRoomReport {
                requested_room_id,
                sync_source,
                room_found: false,
                joined_room_count,
                known_room_count,
                room: None,
            });
        }
    };

    let mut options = MessagesOptions::backward();
    options.limit = uint!(50);

    let messages = room
        .messages(options)
        .await
        .context("matrix SDK room.messages failed in inspect_room mode")?;

    let mut recent_messages = Vec::new();
    let mut latest_timestamp_ms = 0_u64;

    for event in messages.chunk.into_iter() {
        let raw_json =
            serde_json::from_str::<Value>(event.raw().json().get()).unwrap_or(Value::Null);
        let event_type = raw_json.get("type").and_then(Value::as_str).unwrap_or("");
        let ts_ms = raw_json
            .get("origin_server_ts")
            .and_then(Value::as_u64)
            .unwrap_or_default();
        latest_timestamp_ms = latest_timestamp_ms.max(ts_ms);

        let body = raw_json
            .get("content")
            .and_then(|content| content.get("body"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let msgtype = raw_json
            .get("content")
            .and_then(|content| content.get("msgtype"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let event_id = raw_json
            .get("event_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let sender = raw_json
            .get("sender")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let decrypted = event.encryption_info().is_some();
        let rendered_body = adapt_parse_timeline_event(
            raw_json.clone(),
            decrypted,
            client.user_id().as_deref().map(|id| id.as_str()),
            "matrix_rust_probe",
        )
        .map(|parsed| parsed.content_text)
        .unwrap_or_else(|| {
            if body.is_empty() {
                "[non-text event]".to_string()
            } else {
                body
            }
        });
        let kind = if msgtype.is_empty() {
            event_type.to_string()
        } else {
            format!("{event_type}/{msgtype}")
        };

        recent_messages.push(RecentMessage {
            event_id,
            sender,
            occurred_at: iso_from_unix_ms(ts_ms),
            kind,
            decrypted,
            body: rendered_body,
        });
    }

    recent_messages.sort_by(|left, right| left.occurred_at.cmp(&right.occurred_at));
    let room_name = resolve_room_name(&room, self_user_id).await;

    Ok(InspectRoomReport {
        requested_room_id,
        sync_source,
        room_found: true,
        joined_room_count,
        known_room_count,
        room: Some(RecentRoom {
            room_id: room.room_id().to_string(),
            room_name,
            latest_timestamp_ms,
            messages: recent_messages,
        }),
    })
}

async fn ingest_live_history(cfg: &ProbeConfig) -> Result<String> {
    try_sdk_ingest(cfg).await
}

async fn ingest_via_http(cfg: &ProbeConfig) -> Result<String> {
    let session_json = fs::read_to_string(&cfg.session_path)
        .with_context(|| format!("failed to read session at {}", cfg.session_path.display()))?;
    let session_file: SessionFile =
        serde_json::from_str(&session_json).context("invalid matrix session json")?;

    let http_client = HttpClient::builder()
        .user_agent("life-radar-matrix-probe/0.1.0")
        .timeout(Duration::from_secs(cfg.timeout_seconds))
        .build()
        .context("failed to build HTTP client")?;
    let mut total_events = 0usize;
    let mut latest_seen = String::new();
    let self_user_id = session_file.user_id.clone();

    // Connect to DB
    let conn_str = format!(
        "host={} port={} user={} password={} dbname={}",
        cfg.db_host, cfg.db_port, cfg.db_user, cfg.db_password, cfg.db_name
    );
    let (db, connection) = tokio_postgres::connect(&conn_str, NoTls)
        .await
        .context("failed to connect to life-radar postgres for HTTP ingest")?;
    tokio::spawn(async move {
        if let Err(err) = connection.await {
            eprintln!("postgres connection error: {err}");
        }
    });

    let upsert_conversation = db
        .prepare(
            "insert into life_radar.conversations (
                source, external_id, account_id, title, participants, last_event_at, metadata
             ) values ($1,$2,$3,$4,$5::text::jsonb,$6::text::timestamptz,$7::text::jsonb)
             on conflict (source, external_id) do update set
                account_id = coalesce(excluded.account_id, life_radar.conversations.account_id),
                title = excluded.title,
                participants = excluded.participants,
                last_event_at = greatest(
                    coalesce(life_radar.conversations.last_event_at, to_timestamp(0)),
                    coalesce(excluded.last_event_at, to_timestamp(0))
                ),
                metadata = life_radar.conversations.metadata || excluded.metadata,
                updated_at = now()
             returning id::text",
        )
        .await?;
    let upsert_event = db
        .prepare(
            "insert into life_radar.message_events (
                conversation_id, source, external_id, sender_id, sender_label, occurred_at,
                content_text, content_json, is_inbound, provenance
             ) values (
                (select id from life_radar.conversations where source = 'matrix' and external_id = $1::text),
                $2,$3,$4,$5,$6::text::timestamptz,$7,$8::text::jsonb,$9,$10::text::jsonb
             )
             on conflict (source, external_id) do update set
                conversation_id = excluded.conversation_id,
                sender_id = excluded.sender_id,
                sender_label = excluded.sender_label,
                occurred_at = excluded.occurred_at,
                content_text = excluded.content_text,
                content_json = excluded.content_json,
                is_inbound = excluded.is_inbound,
                provenance = life_radar.message_events.provenance || excluded.provenance,
                updated_at = now()",
        )
        .await?;
    let upsert_runtime_metadata = db
        .prepare(
            "insert into life_radar.runtime_metadata (key, value)
             values ($1, $2::text::jsonb)
             on conflict (key) do update set value = excluded.value, updated_at = now()",
        )
        .await?;

    let base_url = session_file.homeserver.trim_end_matches('/');
    let persisted_sync_token = load_sync_checkpoint(&db).await?;
    let sync_attempts = build_sync_attempts(persisted_sync_token, session_file.next_batch.clone());
    let mut selected_sync_source = "full_sync";
    let mut last_sync_error = None;
    let mut sync_body = None;

    for (source, token) in sync_attempts {
        match http_fetch_sync_json(
            &http_client,
            &session_file.access_token,
            base_url,
            token.as_deref(),
            cfg.timeout_seconds,
        )
        .await
        {
            Ok(body) => {
                selected_sync_source = source;
                sync_body = Some(body);
                break;
            }
            Err(err) => {
                eprintln!("matrix http sync attempt {source} failed: {err:#}");
                last_sync_error = Some(format!("{err:#}"));
            }
        }
    }

    let sync_body = match sync_body {
        Some(body) => body,
        None => {
            return Err(anyhow::anyhow!(
                "matrix HTTP sync failed after retries: {}",
                last_sync_error.unwrap_or_else(|| "no sync attempt executed".to_string())
            ));
        }
    };

    if let Some(next_batch) = sync_body.get("next_batch").and_then(Value::as_str) {
        save_sync_checkpoint(
            &db,
            next_batch,
            &format!("matrix-http-normalized:{selected_sync_source}"),
        )
        .await?;
    }

    let mut joined_room_ids: Vec<String> = Vec::new();

    // Extract joined rooms from sync response
    if let Some(rooms) = sync_body.get("rooms") {
        if let Some(join) = rooms.get("join") {
            if let Some(rooms_obj) = join.as_object() {
                for (room_id, room_data) in rooms_obj {
                    joined_room_ids.push(room_id.clone());

                    // Extract timeline events from this room's initial sync
                    if let Some(timeline) = room_data.get("timeline") {
                        if let Some(events) = timeline.get("events") {
                            if let Some(arr) = events.as_array() {
                                for event_val in arr {
                                    // Process event — extract message events
                                    if let Some(processed) =
                                        http_parse_event(event_val, &self_user_id)
                                    {
                                        if processed.occurred_at > latest_seen {
                                            latest_seen = processed.occurred_at.clone();
                                        }
                                        total_events += 1;
                                        // Ingest event directly to DB (room must already be upserted)
                                        // We'll upsert the room first, then events
                                        let room_name = http_resolve_room_name(
                                            &http_client,
                                            &session_file.access_token,
                                            &base_url,
                                            room_id,
                                        )
                                        .await;
                                        let participants_json = "[]".to_string();

                                        let metadata_json = serde_json::json!({
                                            "direct_runtime": "matrix-http-fallback",
                                            "live_history_ingest": true,
                                            "source": "sync_timeline"
                                        })
                                        .to_string();

                                        db.query_one(
                                            &upsert_conversation,
                                            &[
                                                &"matrix",
                                                room_id,
                                                &Some(session_file.user_id.clone()),
                                                &room_name,
                                                &participants_json,
                                                &processed.occurred_at,
                                                &metadata_json,
                                            ],
                                        )
                                        .await
                                        .ok();

                                        db.execute(
                                            &upsert_event,
                                            &[
                                                room_id,
                                                &"matrix",
                                                &processed.external_id,
                                                &Some(processed.sender_id.clone()),
                                                &Some(processed.sender_id.clone()),
                                                &processed.occurred_at,
                                                &processed.content_text,
                                                &processed.content_json,
                                                &processed.is_inbound,
                                                &processed.provenance,
                                            ],
                                        )
                                        .await
                                        .ok();
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    let total_rooms = joined_room_ids.len();

    // For each joined room, fetch backfill messages via HTTP
    for room_id in &joined_room_ids {
        let room_name =
            http_resolve_room_name(&http_client, &session_file.access_token, &base_url, room_id)
                .await;
        let participants_json = "[]".to_string();

        // Fetch messages via HTTP
        let room_events = http_fetch_room_history(
            &http_client,
            &session_file.access_token,
            &base_url,
            room_id,
            cfg.timeout_seconds,
            &self_user_id,
        )
        .await
        .unwrap_or_default();

        if room_events.is_empty() {
            continue;
        }

        let latest_event_at = room_events
            .last()
            .map(|e| e.occurred_at.clone())
            .unwrap_or_default();

        let metadata_json = serde_json::json!({
            "direct_runtime": "matrix-http-fallback",
            "live_history_ingest": true,
            "message_count": room_events.len(),
            "room_name_resolution": "http-state-fallback"
        })
        .to_string();

        db.query_one(
            &upsert_conversation,
            &[
                &"matrix",
                room_id,
                &Some(session_file.user_id.clone()),
                &room_name,
                &participants_json,
                &latest_event_at,
                &metadata_json,
            ],
        )
        .await
        .ok();

        for event in &room_events {
            db.execute(
                &upsert_event,
                &[
                    room_id,
                    &"matrix",
                    &event.external_id,
                    &Some(event.sender_id.clone()),
                    &Some(event.sender_id.clone()),
                    &event.occurred_at,
                    &event.content_text,
                    &event.content_json,
                    &event.is_inbound,
                    &event.provenance,
                ],
            )
            .await
            .ok();
        }

        total_events += room_events.len();
        if latest_event_at > latest_seen {
            latest_seen = latest_event_at;
        }
    }

    let metadata_value = serde_json::json!({
        "ingested_rooms": total_rooms,
        "ingested_events": total_events,
        "latest_seen": latest_seen,
        "candidate": cfg.candidate_id,
        "completed_at": iso_now(),
        "runtime": "matrix-http-normalized"
    })
    .to_string();
    db.execute(
        &upsert_runtime_metadata,
        &[&"matrix_live_history_ingest", &metadata_value],
    )
    .await?;

    Ok(format!(
        "matrix HTTP ingest complete: rooms={} events={} latest_seen={} sync_source={}",
        total_rooms, total_events, latest_seen, selected_sync_source
    ))
}

async fn http_fetch_sync_json(
    client: &HttpClient,
    access_token: &str,
    base_url: &str,
    since: Option<&str>,
    timeout_secs: u64,
) -> Result<Value> {
    let mut url = format!("{}/_matrix/client/v3/sync?timeout=0", base_url);
    if let Some(token) = since {
        url.push_str("&since=");
        url.push_str(token);
    }

    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", access_token))
        .timeout(Duration::from_secs(timeout_secs))
        .send()
        .await
        .context("http sync request failed")?;

    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        anyhow::bail!("HTTP sync returned {}: {}", status, body);
    }

    let mut sync_body: Value = resp
        .json()
        .await
        .context("failed to parse sync response as JSON")?;
    normalize_sync_payload(&mut sync_body);
    Ok(sync_body)
}

fn normalize_sync_payload(sync_body: &mut Value) {
    ensure_events_container(sync_body.get_mut("account_data"));

    if let Some(rooms) = sync_body.get_mut("rooms").and_then(Value::as_object_mut) {
        for section_name in ["join", "leave", "invite", "knock"] {
            let Some(section) = rooms.get_mut(section_name).and_then(Value::as_object_mut) else {
                continue;
            };

            for room in section.values_mut() {
                let Some(room_obj) = room.as_object_mut() else {
                    continue;
                };

                for key in [
                    "timeline",
                    "state",
                    "ephemeral",
                    "account_data",
                    "invite_state",
                    "knock_state",
                ] {
                    ensure_events_container(room_obj.get_mut(key));
                }
            }
        }
    }
}

fn ensure_events_container(target: Option<&mut Value>) {
    let Some(value) = target else {
        return;
    };
    let Some(obj) = value.as_object_mut() else {
        return;
    };
    if !obj.contains_key("events") {
        obj.insert("events".to_string(), Value::Array(Vec::new()));
    }
}

fn http_parse_event(event_val: &Value, self_user_id: &str) -> Option<IngestEvent> {
    adapt_parse_timeline_event(
        event_val.clone(),
        false,
        Some(self_user_id),
        "matrix_http_fallback",
    )
}

async fn http_resolve_room_name(
    client: &HttpClient,
    access_token: &str,
    base_url: &str,
    room_id: &str,
) -> String {
    let url = format!("{}/_matrix/client/v3/rooms/{}/state", base_url, room_id);
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", access_token))
        .send()
        .await
        .ok();

    if let Some(resp) = resp {
        if resp.status().is_success() {
            let states: Vec<Value> = resp.json().await.unwrap_or_default();
            for state_event in states {
                let event_type = state_event.get("type").and_then(Value::as_str);
                let content = state_event.get("content");
                if event_type == Some("m.room.name") {
                    if let Some(name) = content.and_then(|c| c.get("name")).and_then(Value::as_str)
                    {
                        return name.to_string();
                    }
                }
                if event_type == Some("m.room.canonical_alias") {
                    if let Some(alias) =
                        content.and_then(|c| c.get("alias")).and_then(Value::as_str)
                    {
                        return alias.to_string();
                    }
                }
            }
        }
    }
    room_id.to_string()
}

async fn http_fetch_room_history(
    client: &HttpClient,
    access_token: &str,
    base_url: &str,
    room_id: &str,
    timeout_secs: u64,
    self_user_id: &str,
) -> Result<Vec<IngestEvent>> {
    let mut events = Vec::new();
    let mut from_token: Option<String> = None;

    loop {
        let url = if let Some(ref from) = from_token {
            format!(
                "{}/_matrix/client/v3/rooms/{}/messages?dir=b&limit=100&from={}",
                base_url, room_id, from
            )
        } else {
            format!(
                "{}/_matrix/client/v3/rooms/{}/messages?dir=b&limit=100",
                base_url, room_id
            )
        };

        let resp = client
            .get(&url)
            .header("Authorization", format!("Bearer {}", access_token))
            .timeout(Duration::from_secs(timeout_secs))
            .send()
            .await
            .context("http room messages request failed")?;

        if !resp.status().is_success() {
            break;
        }

        let msgs: Value = match resp.json().await {
            Ok(v) => v,
            Err(_) => break,
        };

        let chunk = msgs.get("chunk").and_then(|v| v.as_array());
        let Some(arr) = chunk else {
            break;
        };

        if arr.is_empty() {
            break;
        }

        for event_val in arr {
            if let Some(parsed) = http_parse_event(event_val, self_user_id) {
                events.push(parsed);
            }
        }

        from_token = msgs.get("end").and_then(|v| v.as_str()).map(String::from);

        if from_token.is_none() {
            break;
        }
    }

    events.sort_by(|a, b| a.occurred_at.cmp(&b.occurred_at));
    events.dedup_by(|a, b| a.external_id == b.external_id);
    Ok(events)
}

async fn try_sdk_ingest(cfg: &ProbeConfig) -> Result<String> {
    let (client, session_file) = build_client(cfg).await?;
    let import_outcome = maybe_import_room_keys(cfg, &client).await?;

    let db = connect_postgres(
        cfg,
        "failed to connect to life-radar postgres for live ingest",
    )
    .await?;
    let persisted_sync_token = load_sync_checkpoint(&db).await?;

    let sync_attempts = build_sync_attempts(persisted_sync_token, session_file.next_batch.clone());

    let mut selected_sync_source = "full_sync";
    let mut last_sync_error = None;
    let mut response = None;
    let mut next_batch_override: Option<String> = None;

    for (source, token) in sync_attempts {
        let mut settings =
            SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds));
        if let Some(token) = token {
            settings = settings.token(token);
        }

        match client.sync_once(settings).await {
            Ok(sync_response) => {
                selected_sync_source = source;
                response = Some(sync_response);
                break;
            }
            Err(err) => {
                eprintln!("matrix sync_once attempt {source} failed: {err:#}");
                last_sync_error = Some(format!("{err:#}"));
            }
        }
    }

    if response.is_none() {
        let error_text = last_sync_error.expect("sync attempt loop must record an error");
        if is_malformed_sync_shape_error(&error_text) {
            eprintln!(
                "SDK ingest failed ({error_text}), using normalized HTTP sync bootstrap but keeping SDK room history ingest"
            );
            let bootstrap_attempts =
                build_sync_attempts(load_sync_checkpoint(&db).await?, session_file.next_batch.clone());
            for (source, token) in bootstrap_attempts {
                match fetch_normalized_sync_next_batch(cfg, &session_file, token.as_deref()).await {
                    Ok(next_batch) => {
                        selected_sync_source = source;
                        next_batch_override = Some(next_batch);
                        break;
                    }
                    Err(err) => {
                        eprintln!("normalized HTTP sync bootstrap {source} failed: {err:#}");
                    }
                }
            }
            if next_batch_override.is_none() {
                return Err(anyhow::anyhow!(
                    "matrix rust sync_once failed and normalized HTTP bootstrap could not recover: {}",
                    error_text
                ));
            }
        } else {
            return Err(anyhow::anyhow!(
                "matrix rust sync_once failed in ingest_live_history mode after retries: {}",
                error_text
            ));
        }
    }

    let next_batch_to_save = response
        .as_ref()
        .map(|sync_response| sync_response.next_batch.clone())
        .or(next_batch_override)
        .context("missing next_batch for matrix sync checkpoint")?;

    save_sync_checkpoint(
        &db,
        &next_batch_to_save,
        &format!("matrix-sdk-native:{selected_sync_source}"),
    )
    .await?;

    let upsert_conversation = prepare_upsert_conversation(&db).await?;
    let upsert_event = prepare_upsert_event(&db).await?;
    let upsert_runtime_metadata = prepare_upsert_runtime_metadata(&db).await?;

    let mut total_rooms = 0;
    let mut total_events = 0;
    let mut latest_seen = String::new();
    let self_user_id = client.user_id();
    let self_user_id_str = self_user_id.as_deref().map(|id| id.as_str());
    let should_refresh_undecrypted = import_outcome.status == "imported";

    for room in client.joined_rooms() {
        let room_id = room.room_id().to_string();
        let room_name = resolve_room_name(&room, self_user_id).await;
        let room_state = fetch_persisted_room_state(&db, &room_id).await?;
        let participants_json = room_participants_json(&room, self_user_id).await;
        let should_reprocess_room = should_refresh_undecrypted
            && room_has_undecrypted_events(&db, &room_id).await.unwrap_or(false);

        let room_events = fetch_room_history(
            &room,
            self_user_id_str,
            if should_reprocess_room {
                None
            } else {
                room_state.last_event_ts_ms
            },
        )
        .await
        .unwrap_or_default();

        if room_events.is_empty() && room_state.backfill_complete && !should_reprocess_room {
            continue;
        }

        if room_events.is_empty() {
            continue;
        }

        let latest_event_at = room_events
            .last()
            .map(|e| e.occurred_at.clone())
            .unwrap_or_default();

        let mut event_counts = EventCountSummary::default();
        for event in &room_events {
            event_counts.record(event.classification);
        }

        let room_info = build_room_info(
            room_name,
            "sdk-derived",
            participants_json,
            room.topic().as_deref(),
            event_counts,
        );
        let latest_event_id = room_events.last().map(|event| event.external_id.clone());
        let metadata_json = matrix_room_metadata(
            &room_info.metadata_json,
            room_events.len(),
            room_state.metadata,
            latest_event_id.as_deref(),
            &latest_event_at,
            "matrix-sdk-native",
            true,
        )?;

        db.query_one(
            &upsert_conversation,
            &[
                &"matrix",
                &room_id,
                &Some(session_file.user_id.clone()),
                &room_info.title,
                &room_info.participants_json,
                &latest_event_at,
                &metadata_json,
            ],
        )
        .await
        .ok();

        for event in &room_events {
            db.execute(
                &upsert_event,
                &[
                    &room_id,
                    &"matrix",
                    &event.external_id,
                    &Some(event.sender_id.clone()),
                    &Some(event.sender_id.clone()),
                    &event.occurred_at,
                    &event.content_text,
                    &event.content_json,
                    &event.is_inbound,
                    &event.provenance,
                ],
            )
            .await
            .ok();
        }

        total_rooms += 1;
        total_events += room_events.len();
        if latest_event_at > latest_seen {
            latest_seen = latest_event_at;
        }
    }

    let metadata_value = serde_json::json!({
        "ingested_rooms": total_rooms,
        "ingested_events": total_events,
        "latest_seen": latest_seen,
        "candidate": cfg.candidate_id,
        "completed_at": iso_now(),
        "runtime": "matrix-sdk-native"
    })
    .to_string();
    db.execute(
        &upsert_runtime_metadata,
        &[&"matrix_live_history_ingest", &metadata_value],
    )
    .await
    .ok();

    Ok(format!(
        "matrix SDK ingest complete: rooms={} events={} latest_seen={} room_key_import={}",
        total_rooms, total_events, latest_seen, import_outcome.detail
    ))
}

async fn fetch_normalized_sync_next_batch(
    cfg: &ProbeConfig,
    session_file: &SessionFile,
    since: Option<&str>,
) -> Result<String> {
    let http_client = HttpClient::builder()
        .user_agent("life-radar-matrix-probe/0.1.0")
        .timeout(Duration::from_secs(cfg.timeout_seconds))
        .build()
        .context("failed to build HTTP client for normalized sync bootstrap")?;
    let sync_body = http_fetch_sync_json(
        &http_client,
        &session_file.access_token,
        &session_file.homeserver,
        since,
        cfg.timeout_seconds,
    )
    .await?;
    sync_body
        .get("next_batch")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .context("normalized sync bootstrap did not return next_batch")
}

fn is_malformed_sync_shape_error(error_text: &str) -> bool {
    error_text.contains("missing field `events`") || error_text.contains("missing field 'events'")
}

fn build_sync_attempts(
    persisted_sync_token: Option<String>,
    session_file_token: Option<String>,
) -> Vec<(&'static str, Option<String>)> {
    let mut sync_attempts: Vec<(&'static str, Option<String>)> = Vec::new();
    if let Some(token) = persisted_sync_token {
        sync_attempts.push(("persisted_checkpoint", Some(token)));
    }
    if let Some(token) = session_file_token {
        let duplicate = sync_attempts
            .iter()
            .any(|(_, existing)| existing.as_deref() == Some(token.as_str()));
        if !duplicate {
            sync_attempts.push(("session_file", Some(token)));
        }
    }
    sync_attempts.push(("full_sync", None));
    sync_attempts
}

async fn fetch_room_history(
    room: &matrix_sdk::Room,
    self_user_id: Option<&str>,
    existing_last_event_at_ms: Option<u64>,
) -> Result<Vec<IngestEvent>> {
    let mut token: Option<String> = None;
    let mut events = Vec::new();

    loop {
        let mut options = MessagesOptions::backward();
        options.limit = uint!(100);
        if let Some(ref from) = token {
            options = options.from(Some(from.as_str()));
        }

        let response = room
            .messages(options)
            .await
            .with_context(|| format!("failed to paginate room {}", room.room_id()))?;

        if response.chunk.is_empty() {
            break;
        }

        let next_token = response.end.clone();
        let mut oldest_seen_ms: Option<u64> = None;
        for event in response.chunk {
            let raw_json = match serde_json::from_str::<Value>(event.raw().json().get()) {
                Ok(value) => value,
                Err(_) => continue,
            };
            let ts_ms = raw_json
                .get("origin_server_ts")
                .and_then(Value::as_u64)
                .unwrap_or_default();
            oldest_seen_ms = Some(oldest_seen_ms.map_or(ts_ms, |current| current.min(ts_ms)));
            if let Some(cutoff) = existing_last_event_at_ms {
                if ts_ms <= cutoff {
                    continue;
                }
            }
            if let Some(parsed) = adapt_parse_timeline_event(
                raw_json,
                event.encryption_info().is_some(),
                self_user_id,
                "matrix_rust_live",
            ) {
                events.push(parsed);
            }
        }

        if let (Some(cutoff), Some(oldest_seen)) = (existing_last_event_at_ms, oldest_seen_ms) {
            if oldest_seen <= cutoff {
                break;
            }
        }

        match next_token {
            Some(end) if token.as_deref() != Some(end.as_str()) => token = Some(end),
            _ => break,
        }
    }

    events.sort_by(|left, right| left.occurred_at.cmp(&right.occurred_at));
    events.dedup_by(|left, right| left.external_id == right.external_id);
    Ok(events)
}

async fn room_participants_json(
    room: &matrix_sdk::Room,
    self_user_id: Option<&matrix_sdk::ruma::UserId>,
) -> String {
    let _ = room.sync_members().await;
    let members = room
        .members(RoomMemberships::ACTIVE)
        .await
        .unwrap_or_default();
    let participants = members
        .into_iter()
        .filter(|member| Some(member.user_id()) != self_user_id)
        .map(|member| {
            serde_json::json!({
                "sender_id": member.user_id().to_string(),
                "sender_label": member.display_name().unwrap_or(member.user_id().localpart()).to_string()
            })
        })
        .collect::<Vec<_>>();
    serde_json::to_string(&participants).unwrap_or_else(|_| "[]".to_string())
}

async fn resolve_room_name(
    room: &matrix_sdk::Room,
    self_user_id: Option<&matrix_sdk::ruma::UserId>,
) -> String {
    let cached = room
        .cached_display_name()
        .map(|name| name.to_string())
        .unwrap_or_default();
    if !cached.is_empty() && cached != "Empty Room" {
        return cached;
    }
    if let Ok(name) = room.display_name().await {
        let value = name.to_string();
        if !value.is_empty() && value != "Empty Room" {
            return value;
        }
    }

    let _ = room.sync_members().await;
    if let Ok(name) = room.display_name().await {
        let value = name.to_string();
        if !value.is_empty() && value != "Empty Room" {
            return value;
        }
    }

    if let Ok(members) = room.members(RoomMemberships::ACTIVE).await {
        let names = members
            .into_iter()
            .filter(|member| Some(member.user_id()) != self_user_id)
            .map(|member| {
                member
                    .display_name()
                    .unwrap_or(member.user_id().localpart())
                    .to_string()
            })
            .filter(|name| !name.is_empty())
            .take(6)
            .collect::<Vec<_>>();
        if !names.is_empty() {
            return names.join(", ");
        }
    }

    if let Some(topic) = room.topic() {
        if !topic.is_empty() {
            return topic;
        }
    }

    room.room_id().to_string()
}

async fn build_client(cfg: &ProbeConfig) -> Result<(Client, SessionFile)> {
    let session_file: SessionFile = serde_json::from_str(
        &fs::read_to_string(&cfg.session_path)
            .with_context(|| format!("failed to read {}", cfg.session_path.display()))?,
    )
    .context("invalid matrix session json")?;

    fs::create_dir_all(&cfg.store_path)
        .with_context(|| format!("failed to create {}", cfg.store_path.display()))?;

    let client = Client::builder()
        .homeserver_url(session_file.homeserver.clone())
        .sqlite_store(cfg.store_path.clone(), None)
        .build()
        .await
        .context("failed to build matrix rust client")?;

    let user_id: OwnedUserId = session_file
        .user_id
        .parse()
        .context("invalid matrix user_id")?;
    let device_id: OwnedDeviceId = session_file.device_id.as_str().into();
    let session = MatrixSession {
        meta: SessionMeta { user_id, device_id },
        tokens: SessionTokens {
            access_token: session_file.access_token.clone(),
            refresh_token: None,
        },
    };

    client
        .matrix_auth()
        .restore_session(session, RoomLoadSettings::default())
        .await
        .context("failed to restore matrix session")?;

    Ok((client, session_file))
}

async fn send_message_via_sdk(cfg: &ProbeConfig) -> Result<SendMessageResult> {
    let room_id_value =
        env::var("LIFE_RADAR_SEND_ROOM_ID").context("missing LIFE_RADAR_SEND_ROOM_ID")?;
    let content_text = env::var("LIFE_RADAR_SEND_TEXT").context("missing LIFE_RADAR_SEND_TEXT")?;
    let room_id: OwnedRoomId = room_id_value
        .parse()
        .context("invalid LIFE_RADAR_SEND_ROOM_ID")?;

    let (client, _) = build_client(cfg).await?;
    maybe_import_room_keys(cfg, &client).await?;
    client
        .sync_once(SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds)))
        .await
        .context("matrix rust sync_once failed before send")?;

    let room = client
        .get_room(&room_id)
        .context("matrix room not found in restored session")?;
    let response = room
        .send(RoomMessageEventContent::text_plain(content_text))
        .await
        .context("matrix SDK send failed")?;

    Ok(SendMessageResult {
        status: "sent",
        event_id: response.event_id.to_string(),
    })
}

async fn connect_postgres(cfg: &ProbeConfig, context_msg: &'static str) -> Result<PgClient> {
    let conn_str = format!(
        "host={} port={} user={} password={} dbname={}",
        cfg.db_host, cfg.db_port, cfg.db_user, cfg.db_password, cfg.db_name
    );
    let (db, connection) = tokio_postgres::connect(&conn_str, NoTls)
        .await
        .context(context_msg)?;
    tokio::spawn(async move {
        if let Err(err) = connection.await {
            eprintln!("postgres connection error: {err}");
        }
    });
    Ok(db)
}

async fn prepare_upsert_conversation(db: &PgClient) -> Result<tokio_postgres::Statement> {
    db.prepare(
        "insert into life_radar.conversations (
            source, external_id, account_id, title, participants, last_event_at, metadata
         ) values ($1,$2,$3,$4,$5::text::jsonb,$6::text::timestamptz,$7::text::jsonb)
         on conflict (source, external_id) do update set
            account_id = coalesce(excluded.account_id, life_radar.conversations.account_id),
            title = excluded.title,
            participants = excluded.participants,
            last_event_at = greatest(
                coalesce(life_radar.conversations.last_event_at, to_timestamp(0)),
                coalesce(excluded.last_event_at, to_timestamp(0))
            ),
            metadata = life_radar.conversations.metadata || excluded.metadata,
            updated_at = now()
         returning id::text",
    )
    .await
    .context("failed to prepare conversation upsert")
}

async fn prepare_upsert_event(db: &PgClient) -> Result<tokio_postgres::Statement> {
    db.prepare(
        "insert into life_radar.message_events (
            conversation_id, source, external_id, sender_id, sender_label, occurred_at,
            content_text, content_json, is_inbound, provenance
         ) values (
            (select id from life_radar.conversations where source = 'matrix' and external_id = $1::text),
            $2,$3,$4,$5,$6::text::timestamptz,$7,$8::text::jsonb,$9,$10::text::jsonb
         )
         on conflict (source, external_id) do update set
            conversation_id = excluded.conversation_id,
            sender_id = excluded.sender_id,
            sender_label = excluded.sender_label,
            occurred_at = excluded.occurred_at,
            content_text = excluded.content_text,
            content_json = excluded.content_json,
            is_inbound = excluded.is_inbound,
            provenance = life_radar.message_events.provenance || excluded.provenance,
            updated_at = now()",
    )
    .await
    .context("failed to prepare message event upsert")
}

async fn prepare_upsert_runtime_metadata(db: &PgClient) -> Result<tokio_postgres::Statement> {
    db.prepare(
        "insert into life_radar.runtime_metadata (key, value)
         values ($1, $2::text::jsonb)
         on conflict (key) do update set value = excluded.value, updated_at = now()",
    )
    .await
    .context("failed to prepare runtime metadata upsert")
}

async fn load_sync_checkpoint(db: &PgClient) -> Result<Option<String>> {
    let row = db
        .query_opt(
            "select value::text from life_radar.runtime_metadata where key = $1",
            &[&MATRIX_SYNC_CHECKPOINT_KEY],
        )
        .await
        .context("failed to load matrix sync checkpoint")?;
    let Some(row) = row else {
        return Ok(None);
    };
    let value_text: String = row.get(0);
    let value: Value = serde_json::from_str(&value_text).unwrap_or(Value::Null);
    Ok(value
        .get("next_batch")
        .and_then(Value::as_str)
        .map(|value| value.to_string()))
}

async fn save_sync_checkpoint(db: &PgClient, next_batch: &str, runtime: &str) -> Result<()> {
    db.execute(
        "insert into life_radar.runtime_metadata (key, value)
         values ($1, $2::text::jsonb)
         on conflict (key) do update set value = excluded.value, updated_at = now()",
        &[
            &MATRIX_SYNC_CHECKPOINT_KEY,
            &serde_json::json!({
                "next_batch": next_batch,
                "runtime": runtime,
                "updated_at": iso_now(),
            })
            .to_string(),
        ],
    )
    .await
    .context("failed to save matrix sync checkpoint")?;
    Ok(())
}

async fn fetch_persisted_room_state(db: &PgClient, room_id: &str) -> Result<PersistedRoomState> {
    let row = db
        .query_opt(
            "select
                (extract(epoch from last_event_at) * 1000)::bigint as last_event_ts_ms,
                metadata::text
             from life_radar.conversations
             where source = 'matrix' and external_id = $1",
            &[&room_id],
        )
        .await
        .context("failed to load matrix conversation checkpoint")?;

    let Some(row) = row else {
        return Ok(PersistedRoomState {
            last_event_ts_ms: None,
            backfill_complete: false,
            metadata: Value::Object(Default::default()),
        });
    };

    let metadata_text = row.get::<usize, String>(1);
    let metadata: Value =
        serde_json::from_str(&metadata_text).unwrap_or(Value::Object(Default::default()));
    let checkpoint = metadata
        .get("matrix_room_checkpoint")
        .cloned()
        .unwrap_or(Value::Object(Default::default()));
    let last_event_ts_ms = checkpoint
        .get("latest_event_ts_ms")
        .and_then(Value::as_u64)
        .or_else(|| {
            row.get::<usize, Option<i64>>(0)
                .map(|value| value.max(0) as u64)
        });
    let backfill_complete = checkpoint
        .get("backfill_complete")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    Ok(PersistedRoomState {
        last_event_ts_ms,
        backfill_complete,
        metadata,
    })
}

async fn room_has_undecrypted_events(db: &PgClient, room_id: &str) -> Result<bool> {
    let row = db
        .query_one(
            "select exists(
                select 1
                from life_radar.message_events me
                join life_radar.conversations c on c.id = me.conversation_id
                where c.source = 'matrix'
                  and c.external_id = $1
                  and me.content_text = '[undecrypted]'
            )",
            &[&room_id],
        )
        .await
        .with_context(|| format!("failed to check undecrypted events for room {room_id}"))?;

    Ok(row.get::<usize, bool>(0))
}

fn matrix_room_metadata(
    base_metadata_json: &str,
    message_count: usize,
    existing_metadata: Value,
    latest_event_id: Option<&str>,
    latest_event_at: &str,
    runtime: &str,
    backfill_complete: bool,
) -> Result<String> {
    let mut metadata = serde_json::from_str::<Value>(base_metadata_json)
        .unwrap_or(Value::Object(Default::default()));
    let Some(metadata_obj) = metadata.as_object_mut() else {
        return Ok(base_metadata_json.to_string());
    };

    if let Some(existing) = existing_metadata.as_object() {
        for (key, value) in existing {
            if key != "matrix_room_checkpoint" && !metadata_obj.contains_key(key) {
                metadata_obj.insert(key.clone(), value.clone());
            }
        }
    }

    metadata_obj.insert(
        "direct_runtime".to_string(),
        Value::String(runtime.to_string()),
    );
    metadata_obj.insert("live_history_ingest".to_string(), Value::Bool(true));
    metadata_obj.insert(
        "message_count".to_string(),
        Value::Number(serde_json::Number::from(message_count as u64)),
    );
    metadata_obj.insert(
        "matrix_room_checkpoint".to_string(),
        serde_json::json!({
            "latest_event_ts_ms": iso_to_unix_ms(latest_event_at),
            "latest_event_at": latest_event_at,
            "latest_event_id": latest_event_id,
            "backfill_complete": backfill_complete,
            "updated_at": iso_now(),
        }),
    );

    Ok(metadata.to_string())
}

async fn maybe_import_room_keys(cfg: &ProbeConfig, client: &Client) -> Result<ImportOutcome> {
    if !cfg.key_export_path.is_file() {
        return Ok(ImportOutcome {
            status: "missing",
            detail: format!("missing export {}", cfg.key_export_path.display()),
            imported_count: 0,
            total_count: 0,
        });
    }

    if !cfg.key_passphrase_path.is_file() {
        return Ok(ImportOutcome {
            status: "missing",
            detail: format!("missing passphrase {}", cfg.key_passphrase_path.display()),
            imported_count: 0,
            total_count: 0,
        });
    }

    let source_metadata = fs::metadata(&cfg.key_export_path)
        .with_context(|| format!("failed to stat {}", cfg.key_export_path.display()))?;
    let passphrase_metadata = fs::metadata(&cfg.key_passphrase_path)
        .with_context(|| format!("failed to stat {}", cfg.key_passphrase_path.display()))?;
    let source_size = source_metadata.len();
    let source_mtime_epoch = modified_epoch_secs(&source_metadata)?;
    let passphrase_mtime_epoch = modified_epoch_secs(&passphrase_metadata)?;

    if let Ok(marker_json) = fs::read_to_string(&cfg.key_import_marker_path) {
        if let Ok(marker) = serde_json::from_str::<ImportMarker>(&marker_json) {
            if marker.source_size == source_size
                && marker.source_mtime_epoch == source_mtime_epoch
                && marker.passphrase_mtime_epoch == passphrase_mtime_epoch
            {
                return Ok(ImportOutcome {
                    status: "cached",
                    detail: format!(
                        "up-to-date (imported {}/{} at {})",
                        marker.imported_count, marker.total_count, marker.imported_at
                    ),
                    imported_count: marker.imported_count,
                    total_count: marker.total_count,
                });
            }
        }
    }

    let passphrase = fs::read_to_string(&cfg.key_passphrase_path)
        .with_context(|| format!("failed to read {}", cfg.key_passphrase_path.display()))?;
    let passphrase = passphrase.trim().to_owned();
    if passphrase.is_empty() {
        return Ok(ImportOutcome {
            status: "missing",
            detail: format!("empty passphrase {}", cfg.key_passphrase_path.display()),
            imported_count: 0,
            total_count: 0,
        });
    }

    let result = client
        .encryption()
        .import_room_keys(cfg.key_export_path.clone(), &passphrase)
        .await
        .context("failed to import Matrix room keys into rust crypto store")?;

    let marker = ImportMarker {
        source_size,
        source_mtime_epoch,
        passphrase_mtime_epoch,
        imported_count: result.imported_count,
        total_count: result.total_count,
        imported_at: iso_now(),
    };
    let marker_json =
        serde_json::to_string_pretty(&marker).context("failed to serialize key import marker")?;
    fs::write(&cfg.key_import_marker_path, marker_json)
        .with_context(|| format!("failed to write {}", cfg.key_import_marker_path.display()))?;

    Ok(ImportOutcome {
        status: "imported",
        detail: format!(
            "imported {}/{} from {}",
            result.imported_count,
            result.total_count,
            cfg.key_export_path.display()
        ),
        imported_count: result.imported_count,
        total_count: result.total_count,
    })
}

async fn sample_recent_messages(client: &Client) -> DecryptionSample {
    const SAMPLE_ROOM_LIMIT: usize = 5;

    let mut sorted_rooms = client.joined_rooms();
    sorted_rooms.sort_by(|left, right| left.room_id().as_str().cmp(right.room_id().as_str()));

    let mut sample = DecryptionSample {
        sampled_rooms: 0,
        sampled_events: 0,
        decrypted_text_events: 0,
        decrypted_non_text_events: 0,
        undecrypted_events: 0,
        plain_text_events: 0,
        unsupported_custom_events: 0,
        freshest_timestamp_ms: None,
        room_summaries: Vec::new(),
        room_errors: Vec::new(),
    };

    for room in sorted_rooms.into_iter().take(SAMPLE_ROOM_LIMIT) {
        let room_id = room.room_id().to_owned();
        let mut options = MessagesOptions::backward();
        options.limit = uint!(20);

        match room.messages(options).await {
            Ok(messages) => {
                sample.sampled_rooms += 1;

                let mut room_decrypted_text = 0_i64;
                let mut room_undecrypted = 0_i64;
                let mut room_plain_text = 0_i64;
                let mut room_decrypted_non_text = 0_i64;
                let mut room_unsupported_custom = 0_i64;

                for event in messages.chunk {
                    sample.sampled_events += 1;
                    let decrypted = event.encryption_info().is_some();
                    let raw_json = serde_json::from_str::<Value>(event.raw().json().get())
                        .unwrap_or(Value::Null);
                    if let Some(ts_ms) = raw_json.get("origin_server_ts").and_then(Value::as_u64) {
                        sample.freshest_timestamp_ms = Some(
                            sample
                                .freshest_timestamp_ms
                                .map_or(ts_ms, |current| current.max(ts_ms)),
                        );
                    }
                    let Some(parsed) = adapt_parse_timeline_event(
                        raw_json,
                        decrypted,
                        client.user_id().as_deref().map(|id| id.as_str()),
                        "matrix_rust_probe",
                    ) else {
                        continue;
                    };

                    match parsed.classification {
                        EventClassification::DecryptedText => {
                            sample.decrypted_text_events += 1;
                            room_decrypted_text += 1;
                        }
                        EventClassification::DecryptedNonText => {
                            sample.decrypted_non_text_events += 1;
                            room_decrypted_non_text += 1;
                        }
                        EventClassification::PlainText => {
                            sample.plain_text_events += 1;
                            room_plain_text += 1;
                        }
                        EventClassification::Undecrypted => {
                            sample.undecrypted_events += 1;
                            room_undecrypted += 1;
                        }
                        EventClassification::UnsupportedCustom => {
                            sample.unsupported_custom_events += 1;
                            room_unsupported_custom += 1;
                        }
                    }
                }

                sample.room_summaries.push(format!(
                    "{}: decrypted_text={}, decrypted_non_text={}, undecrypted={}, plain_text={}, unsupported_custom={}",
                    room_id.as_str(),
                    room_decrypted_text,
                    room_decrypted_non_text,
                    room_undecrypted,
                    room_plain_text,
                    room_unsupported_custom,
                ));
            }
            Err(err) => {
                sample
                    .room_errors
                    .push(format!("{}: {}", room_id.as_str(), err));
            }
        }
    }

    sample
}

fn failure_result(cfg: &ProbeConfig, observed_at: &str, err: anyhow::Error) -> ProbeResult {
    let notes = format!("{}", err.root_cause());
    let metadata_json = serde_json::json!({
        "session_path": cfg.session_path,
        "store_path": cfg.store_path,
        "error_chain": format!("{err:#}")
    })
    .to_string();

    let report_body = format!(
        "# Matrix Candidate Report\n\n- observed_at: {}\n- candidate_id: {}\n- candidate_type: {}\n- status: fail\n- notes: {}\n- session_path: {}\n- store_path: {}\n",
        observed_at,
        cfg.candidate_id,
        cfg.candidate_type,
        notes,
        cfg.session_path.display(),
        cfg.store_path.display(),
    );

    ProbeResult {
        status: "fail",
        notes,
        latency_ms: 0,
        freshness_seconds: 0,
        total_events: 0,
        decrypt_failures: 0,
        encrypted_non_text: 0,
        running_processes: 0,
        metadata_json,
        report_body,
    }
}

async fn persist_probe(cfg: &ProbeConfig, observed_at: &str, result: &ProbeResult) -> Result<()> {
    let conn_str = format!(
        "host={} port={} user={} password={} dbname={}",
        cfg.db_host, cfg.db_port, cfg.db_user, cfg.db_password, cfg.db_name
    );
    let (client, connection) = tokio_postgres::connect(&conn_str, NoTls)
        .await
        .context("failed to connect to life-radar postgres")?;
    tokio::spawn(async move {
        if let Err(err) = connection.await {
            eprintln!("postgres connection error: {err}");
        }
    });

    client
        .execute(
            "insert into life_radar.runtime_probes (
                candidate_id, candidate_type, status, observed_at, latency_ms,
                freshness_seconds, total_events, decrypt_failures,
                encrypted_non_text, running_processes, metadata, notes
             ) values ($1,$2,$3,$4::text::timestamptz,$5,$6,$7,$8,$9,$10,$11::text::jsonb,$12)",
            &[
                &cfg.candidate_id,
                &cfg.candidate_type,
                &result.status,
                &observed_at,
                &result.latency_ms,
                &result.freshness_seconds,
                &result.total_events,
                &result.decrypt_failures,
                &result.encrypted_non_text,
                &result.running_processes,
                &result.metadata_json,
                &Some(result.notes.clone()),
            ],
        )
        .await
        .context("failed to insert rust matrix runtime probe")?;

    client
        .execute(
            "insert into life_radar.messaging_candidates (
                candidate_id, candidate_type, last_status, last_probe_at,
                latest_freshness_seconds, latest_total_events, latest_decrypt_failures,
                latest_encrypted_non_text, latest_running_processes, latest_notes,
                metadata, updated_at
            ) values ($1,$2,$3,$4::text::timestamptz,$5,$6,$7,$8,$9,$10,$11::text::jsonb, now())
            on conflict (candidate_id) do update set
                candidate_type = excluded.candidate_type,
                last_status = excluded.last_status,
                last_probe_at = excluded.last_probe_at,
                latest_freshness_seconds = excluded.latest_freshness_seconds,
                latest_total_events = excluded.latest_total_events,
                latest_decrypt_failures = excluded.latest_decrypt_failures,
                latest_encrypted_non_text = excluded.latest_encrypted_non_text,
                latest_running_processes = excluded.latest_running_processes,
                latest_notes = excluded.latest_notes,
                metadata = excluded.metadata,
                updated_at = now()",
            &[
                &cfg.candidate_id,
                &cfg.candidate_type,
                &result.status,
                &observed_at,
                &result.freshness_seconds,
                &result.total_events,
                &result.decrypt_failures,
                &result.encrypted_non_text,
                &result.running_processes,
                &Some(result.notes.clone()),
                &result.metadata_json,
            ],
        )
        .await
        .context("failed to upsert rust matrix messaging candidate")?;

    Ok(())
}

fn write_report(cfg: &ProbeConfig, content: &str) -> Result<()> {
    fs::create_dir_all(&cfg.report_dir)
        .with_context(|| format!("failed to create {}", cfg.report_dir.display()))?;
    let tmp_path = cfg.report_dir.join(".matrix-rust-sdk-latest.tmp");
    let final_path = cfg.report_dir.join("matrix-rust-sdk-latest.md");
    fs::write(&tmp_path, content)
        .with_context(|| format!("failed to write {}", tmp_path.display()))?;
    fs::rename(&tmp_path, &final_path)
        .with_context(|| format!("failed to rename {}", final_path.display()))?;
    Ok(())
}

fn format_report_list(label: &str, values: &[String]) -> String {
    if values.is_empty() {
        format!("- {}: none", label)
    } else {
        let rendered = values
            .iter()
            .map(|value| format!("  - {}", value))
            .collect::<Vec<_>>()
            .join("\n");
        format!("- {}:\n{}", label, rendered)
    }
}

fn env_var(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

fn iso_now() -> String {
    let output = std::process::Command::new("date")
        .args(["-u", "+%FT%TZ"])
        .output()
        .expect("date command must exist");
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn iso_from_unix_ms(ts_ms: u64) -> String {
    let seconds = (ts_ms / 1000).to_string();
    let output = std::process::Command::new("date")
        .args(["-u", "-d", &format!("@{seconds}"), "+%FT%TZ"])
        .output();
    match output {
        Ok(result) if result.status.success() => {
            String::from_utf8_lossy(&result.stdout).trim().to_string()
        }
        _ => String::new(),
    }
}

fn iso_to_unix_ms(value: &str) -> u64 {
    if value.is_empty() {
        return 0;
    }

    let output = std::process::Command::new("date")
        .args(["-u", "-d", value, "+%s"])
        .output();
    match output {
        Ok(result) if result.status.success() => String::from_utf8_lossy(&result.stdout)
            .trim()
            .parse::<u64>()
            .map(|seconds| seconds.saturating_mul(1000))
            .unwrap_or_default(),
        _ => 0,
    }
}

fn now_unix_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};

    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis().min(u64::MAX as u128) as u64)
        .unwrap_or_default()
}

fn modified_epoch_secs(metadata: &fs::Metadata) -> Result<u64> {
    use std::time::UNIX_EPOCH;

    Ok(metadata
        .modified()
        .context("failed to read mtime")?
        .duration_since(UNIX_EPOCH)
        .context("mtime predates unix epoch")?
        .as_secs())
}

#[cfg(test)]
mod tests {
    use super::build_sync_attempts;

    #[test]
    fn sync_attempts_include_all_fallbacks_in_order() {
        let attempts = build_sync_attempts(
            Some("persisted-token".to_string()),
            Some("session-token".to_string()),
        );

        assert_eq!(
            attempts,
            vec![
                ("persisted_checkpoint", Some("persisted-token".to_string())),
                ("session_file", Some("session-token".to_string())),
                ("full_sync", None),
            ]
        );
    }

    #[test]
    fn sync_attempts_deduplicate_matching_tokens() {
        let attempts = build_sync_attempts(
            Some("same-token".to_string()),
            Some("same-token".to_string()),
        );

        assert_eq!(
            attempts,
            vec![
                ("persisted_checkpoint", Some("same-token".to_string())),
                ("full_sync", None),
            ]
        );
    }

    #[test]
    fn sync_attempts_still_full_sync_without_tokens() {
        let attempts = build_sync_attempts(None, None);

        assert_eq!(attempts, vec![("full_sync", None)]);
    }
}
