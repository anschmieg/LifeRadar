mod adapter;

use std::{
    collections::{HashMap, HashSet},
    env, fs, io,
    path::PathBuf,
    time::{Duration, Instant},
};

use adapter::{
    build_room_info, parse_timeline_event as adapt_parse_timeline_event, EventClassification,
    EventCountSummary, IngestEvent,
};
use anyhow::{Context, Result};
use futures_util::StreamExt;
use matrix_sdk::{
    authentication::matrix::MatrixSession,
    config::SyncSettings,
    encryption::{
        verification::{SasState, Verification, VerificationRequestState},
        BackupDownloadStrategy, EncryptionSettings,
    },
    room::MessagesOptions,
    ruma::{
        events::{
            key::verification::VerificationMethod,
            room::message::RoomMessageEventContent,
        },
        uint, OwnedDeviceId, OwnedRoomId, OwnedUserId,
    },
    store::RoomLoadSettings,
    Client, RoomMemberships, SessionMeta, SessionTokens,
};
use matrix_sdk_crypto::{
    OlmMachine,
    types::SecretsBundle,
};
use reqwest::Client as HttpClient;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    sync::mpsc,
    task::JoinSet,
};
use tokio_postgres::{Client as PgClient, NoTls};

#[derive(Clone, Debug, Deserialize, Serialize)]
struct SessionFile {
    access_token: String,
    refresh_token: Option<String>,
    user_id: String,
    device_id: String,
    homeserver: String,
    next_batch: Option<String>,
    expires_at: Option<String>,
    expires_in: Option<u64>,
    saved_at: Option<String>,
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
    key_import_enabled: bool,
    secrets_bundle_path: Option<PathBuf>,
    recovery_key_path: Option<PathBuf>,
    recovery_passphrase_path: Option<PathBuf>,
    report_dir: PathBuf,
    timeout_seconds: u64,
    mode: String,
    inspect_room_id: Option<String>,
    recent_room_limit: usize,
    recent_message_limit: u32,
    undecrypted_heal_room_limit: usize,
    undecrypted_heal_cooldown_hours: u64,
}

impl ProbeConfig {
    fn from_env() -> Result<Self> {
        Ok(Self {
            db_host: env_var("LIFERADAR_DB_HOST", "liferadar-db"),
            db_port: env_var("LIFERADAR_DB_PORT", "5432")
                .parse()
                .context("invalid LIFERADAR_DB_PORT")?,
            db_name: env_var("LIFERADAR_DB_NAME", "life_radar"),
            db_user: env_var("LIFERADAR_DB_USER", "life_radar"),
            db_password: env_var("LIFERADAR_DB_PASSWORD", "change-me-in-env"),
            candidate_id: env_var("LIFERADAR_MATRIX_RUST_CANDIDATE_ID", "matrix-rust-sdk"),
            candidate_type: env_var("LIFERADAR_MATRIX_RUST_CANDIDATE_TYPE", "matrix-native"),
            session_path: PathBuf::from(env_var(
                "MATRIX_RUST_SESSION_PATH",
                "/app/identity/matrix-session.json",
            )),
            store_path: PathBuf::from(env_var(
                "MATRIX_RUST_STORE",
                "/app/identity/matrix-rust-sdk-store",
            )),
            key_export_path: PathBuf::from(env_var(
                "MATRIX_ROOM_KEYS_PATH",
                "/app/identity/matrix-e2e-keys.txt",
            )),
            key_passphrase_path: PathBuf::from(env_var(
                "MATRIX_ROOM_KEYS_PASSPHRASE_PATH",
                "/app/identity/.e2e-keys-passphrase",
            )),
            key_import_marker_path: PathBuf::from(env_var(
                "MATRIX_RUST_KEY_IMPORT_MARKER",
                "/app/identity/matrix-rust-sdk-store/room-key-import-marker.json",
            )),
            key_import_enabled: env_flag("LIFERADAR_MATRIX_KEY_IMPORT_ENABLED", true),
            secrets_bundle_path: env::var("MATRIX_SECRETS_BUNDLE_PATH").ok().map(PathBuf::from),
            recovery_key_path: env::var("MATRIX_RECOVERY_KEY_PATH").ok().map(PathBuf::from),
            recovery_passphrase_path: env::var("MATRIX_RECOVERY_PASSPHRASE_PATH")
                .ok()
                .map(PathBuf::from),
            report_dir: PathBuf::from(env_var(
                "LIFERADAR_REPORT_DIR",
                "/app/workspace/liferadar/reports",
            )),
            timeout_seconds: env_var("LIFERADAR_MATRIX_RUST_TIMEOUT_SEC", "20")
                .parse()
                .context("invalid LIFERADAR_MATRIX_RUST_TIMEOUT_SEC")?,
            mode: env_var("LIFERADAR_MATRIX_RUST_MODE", "probe"),
            inspect_room_id: env::var("LIFERADAR_MATRIX_RUST_ROOM_ID").ok(),
            recent_room_limit: env_var("LIFERADAR_MATRIX_RUST_RECENT_ROOM_LIMIT", "5")
                .parse()
                .context("invalid LIFERADAR_MATRIX_RUST_RECENT_ROOM_LIMIT")?,
            recent_message_limit: env_var("LIFERADAR_MATRIX_RUST_RECENT_MESSAGE_LIMIT", "10")
                .parse()
                .context("invalid LIFERADAR_MATRIX_RUST_RECENT_MESSAGE_LIMIT")?,
            undecrypted_heal_room_limit: env_var(
                "LIFERADAR_MATRIX_RUST_UNDECRYPTED_HEAL_ROOM_LIMIT",
                "3",
            )
            .parse()
            .context("invalid LIFERADAR_MATRIX_RUST_UNDECRYPTED_HEAL_ROOM_LIMIT")?,
            undecrypted_heal_cooldown_hours: env_var(
                "LIFERADAR_MATRIX_RUST_UNDECRYPTED_HEAL_COOLDOWN_HOURS",
                "24",
            )
            .parse()
            .context("invalid LIFERADAR_MATRIX_RUST_UNDECRYPTED_HEAL_COOLDOWN_HOURS")?,
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

#[allow(dead_code)]
#[derive(Debug)]
struct E2eBackupOutcome {
    status: &'static str,
    detail: String,
}

#[derive(Debug)]
struct SecretRecoveryOutcome {
    status: &'static str,
    detail: String,
}

#[derive(Debug)]
struct OwnDeviceVerificationOutcome {
    status: &'static str,
    detail: String,
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

#[derive(Debug, Clone)]
struct BridgeTarget {
    state_key: String,
    protocol_id: Option<String>,
    channel_id: Option<String>,
    display_name: Option<String>,
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

#[derive(Debug, Clone)]
struct PersistedRoomState {
    last_event_ts_ms: Option<u64>,
    backfill_complete: bool,
    metadata: Value,
}

#[derive(Debug, Default, Clone, Copy)]
struct UndecryptedStats {
    count: i64,
    latest_event_ts_ms: Option<u64>,
}

#[derive(Debug, Serialize)]
struct SendMessageResult {
    status: &'static str,
    event_id: String,
}

#[derive(Debug, Clone, Copy)]
enum VerificationCommand {
    Confirm,
    Reject,
    Cancel,
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
    if cfg.mode == "verify_device_interactive" {
        run_interactive_device_verification(&cfg).await?;
        return Ok(());
    }
    if cfg.mode == "key_import" {
        let (client, _session) = build_client(&cfg).await?;
        let recovery_outcome = maybe_restore_secret_storage(&cfg, &client).await?;
        let import_outcome = maybe_import_room_keys(&cfg, &client).await?;
        println!("secret_recovery_status={}", recovery_outcome.status);
        println!("secret_recovery_detail={}", recovery_outcome.detail);
        println!("key_import_status={}", import_outcome.status);
        println!("key_import_detail={}", import_outcome.detail);
        println!("imported_count={}", import_outcome.imported_count);
        println!("total_count={}", import_outcome.total_count);

        println!("\nsyncing to apply imported keys...");
        client
            .sync_once(SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds)))
            .await
            .context("sync_one failed after key import")?;

        let sample = sample_recent_messages(&client).await;
        println!("\n=== Decryption Verification ===");
        println!("sampled_rooms={}", sample.sampled_rooms);
        println!("sampled_events={}", sample.sampled_events);
        println!("decrypted_text_events={}", sample.decrypted_text_events);
        println!(
            "decrypted_non_text_events={}",
            sample.decrypted_non_text_events
        );
        println!("undecrypted_events={}", sample.undecrypted_events);
        println!("plain_text_events={}", sample.plain_text_events);

        if sample.decrypted_text_events > 0 {
            println!(
                "\n✅ SUCCESS: {} messages decrypted successfully",
                sample.decrypted_text_events
            );
        } else if sample.undecrypted_events > 0 {
            println!(
                "\n❌ FAIL: {} events remain undecrypted after key import",
                sample.undecrypted_events
            );
        } else {
            println!("\n⚠️  WARN: no encrypted messages found in sampled rooms");
        }
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

    let recovery_outcome = maybe_restore_secret_storage(cfg, &client).await?;
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
    persist_client_session(
        &cfg.session_path,
        &client,
        Some(response.next_batch.clone()),
    )?;
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
        "secret_recovery_status": recovery_outcome.status,
        "secret_recovery_detail": recovery_outcome.detail,
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
        "# Matrix Candidate Report\n\n- observed_at: {}\n- candidate_id: {}\n- candidate_type: {}\n- status: {}\n- notes: {}\n- latency_ms: {}\n- freshness_seconds: {}\n- joined_rooms: {}\n- invite_rooms: {}\n- knocked_rooms: {}\n- presence_events: {}\n- to_device_events: {}\n- room_key_import_status: {}\n- room_key_import_detail: {}\n- secret_recovery_status: {}\n- secret_recovery_detail: {}\n- room_key_imported_count: {}\n- room_key_total_count: {}\n- sampled_rooms: {}\n- sampled_events: {}\n- decrypted_text_events: {}\n- decrypted_non_text_events: {}\n- undecrypted_events: {}\n- plain_text_events: {}\n- unsupported_custom_events: {}\n- session_path: {}\n- store_path: {}\n- homeserver: {}\n{}\n{}\n",
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
        recovery_outcome.status,
        recovery_outcome.detail,
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
    let _ = maybe_restore_secret_storage(cfg, &client).await?;
    maybe_import_room_keys(cfg, &client).await?;

    let sync_attempts = build_sync_attempts(None, session_file.next_batch.clone());
    let mut last_sync_error = None;
    let mut synced = false;
    let mut latest_next_batch = None;

    for (source, token) in sync_attempts {
        let mut settings =
            SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds));
        if let Some(token) = token {
            settings = settings.token(token);
        }

        match client.sync_once(settings).await {
            Ok(sync_response) => {
                eprintln!("inspect_recent sync_once succeeded via {source}");
                latest_next_batch = Some(sync_response.next_batch.clone());
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
    persist_client_session(&cfg.session_path, &client, latest_next_batch)?;

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
        .context("missing LIFERADAR_MATRIX_RUST_ROOM_ID for inspect_room mode")?;
    let room_id: OwnedRoomId = requested_room_id
        .parse()
        .context("invalid LIFERADAR_MATRIX_RUST_ROOM_ID")?;

    let (client, session_file) = build_client(cfg).await?;
    let _ = maybe_restore_secret_storage(cfg, &client).await?;
    maybe_import_room_keys(cfg, &client).await?;

    let sync_attempts = build_sync_attempts(None, session_file.next_batch.clone());
    let mut last_sync_error = None;
    let mut sync_source = "none".to_string();
    let mut latest_next_batch = None;

    for (source, token) in sync_attempts {
        let mut settings =
            SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds));
        if let Some(token) = token {
            settings = settings.token(token);
        }

        match client.sync_once(settings).await {
            Ok(sync_response) => {
                sync_source = source.to_string();
                latest_next_batch = Some(sync_response.next_batch.clone());
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
    persist_client_session(&cfg.session_path, &client, latest_next_batch)?;

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
        .user_agent("liferadar-matrix-probe/0.1.0")
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
        .context("failed to connect to liferadar postgres for HTTP ingest")?;
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
        persist_session_checkpoint(&cfg.session_path, next_batch)?;
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
    let url = format!(
        "{}/_matrix/client/v3/rooms/{}/state",
        base_url,
        urlencoding::encode(room_id)
    );
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

async fn http_fetch_room_state(
    client: &HttpClient,
    access_token: &str,
    base_url: &str,
    room_id: &str,
    timeout_secs: u64,
) -> Result<Vec<Value>> {
    let base_url = base_url.trim_end_matches('/');
    let url = format!(
        "{}/_matrix/client/v3/rooms/{}/state",
        base_url,
        urlencoding::encode(room_id)
    );
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", access_token))
        .timeout(Duration::from_secs(timeout_secs))
        .send()
        .await
        .context("http room state request failed")?;

    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        anyhow::bail!("HTTP room state returned {}: {}", status, body);
    }

    resp.json()
        .await
        .context("failed to parse room state response as JSON")
}

async fn http_fetch_room_state_event(
    client: &HttpClient,
    access_token: &str,
    base_url: &str,
    room_id: &str,
    event_type: &str,
    state_key: &str,
    timeout_secs: u64,
) -> Result<Option<Value>> {
    let base_url = base_url.trim_end_matches('/');
    let url = format!(
        "{}/_matrix/client/v3/rooms/{}/state/{}/{}",
        base_url,
        urlencoding::encode(room_id),
        urlencoding::encode(event_type),
        urlencoding::encode(state_key)
    );
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", access_token))
        .timeout(Duration::from_secs(timeout_secs))
        .send()
        .await
        .context("http room state event request failed")?;

    if resp.status() == reqwest::StatusCode::NOT_FOUND {
        return Ok(None);
    }

    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        anyhow::bail!("HTTP room state event returned {}: {}", status, body);
    }

    let content: Value = resp
        .json()
        .await
        .context("failed to parse room state event response as JSON")?;

    Ok(Some(serde_json::json!({
        "type": event_type,
        "state_key": state_key,
        "content": content
    })))
}

async fn http_fetch_joined_rooms(
    client: &HttpClient,
    access_token: &str,
    base_url: &str,
    timeout_secs: u64,
) -> Result<Vec<String>> {
    let base_url = base_url.trim_end_matches('/');
    let url = format!("{}/_matrix/client/v3/joined_rooms", base_url);
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", access_token))
        .timeout(Duration::from_secs(timeout_secs))
        .send()
        .await
        .context("http joined_rooms request failed")?;

    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        anyhow::bail!("HTTP joined_rooms returned {}: {}", status, body);
    }

    let payload: Value = resp
        .json()
        .await
        .context("failed to parse joined_rooms response as JSON")?;

    Ok(payload
        .get("joined_rooms")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(ToOwned::to_owned)
        .collect())
}

fn extract_bridge_targets(states: &[Value]) -> Vec<BridgeTarget> {
    let mut targets = Vec::new();
    for state_event in states {
        let Some(event_type) = state_event.get("type").and_then(Value::as_str) else {
            continue;
        };
        if event_type != "m.bridge" && event_type != "uk.half-shot.bridge" {
            continue;
        }

        let Some(state_key) = state_event.get("state_key").and_then(Value::as_str) else {
            continue;
        };
        let content = state_event.get("content").unwrap_or(&Value::Null);
        let protocol_id = content
            .get("protocol")
            .and_then(|value| value.get("id"))
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
        let channel_id = content
            .get("channel")
            .and_then(|value| value.get("id"))
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
        let display_name = content
            .get("channel")
            .and_then(|value| value.get("displayname"))
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);

        targets.push(BridgeTarget {
            state_key: state_key.to_string(),
            protocol_id,
            channel_id,
            display_name,
        });
    }
    targets
}

fn bridge_targets_match(left: &BridgeTarget, right: &BridgeTarget) -> bool {
    (!left.state_key.is_empty() && !right.state_key.is_empty() && left.state_key == right.state_key)
        || (left.protocol_id.is_some()
            && left.protocol_id == right.protocol_id
            && left.channel_id.is_some()
            && left.channel_id == right.channel_id)
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
    let _ = maybe_restore_secret_storage(cfg, &client).await?;
    let import_outcome = maybe_import_room_keys(cfg, &client).await?;

    let db = connect_postgres(
        cfg,
        "failed to connect to liferadar postgres for live ingest",
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
            let bootstrap_attempts = build_sync_attempts(
                load_sync_checkpoint(&db).await?,
                session_file.next_batch.clone(),
            );
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
    persist_client_session(&cfg.session_path, &client, Some(next_batch_to_save.clone()))?;

    let upsert_conversation = prepare_upsert_conversation(&db).await?;
    let upsert_event = prepare_upsert_event(&db).await?;
    let upsert_runtime_metadata = prepare_upsert_runtime_metadata(&db).await?;

    let mut total_rooms = 0;
    let mut total_events = 0;
    let mut latest_seen = String::new();
    let self_user_id = client.user_id();
    let self_user_id_str = self_user_id.as_deref().map(|id| id.as_str());
    let joined_rooms = client.joined_rooms();
    let mut room_states = HashMap::new();
    let mut undecrypted_stats_by_room = HashMap::new();
    let mut heal_candidates = Vec::new();

    for room in &joined_rooms {
        let room_id = room.room_id().to_string();
        let room_state = fetch_persisted_room_state(&db, &room_id).await?;
        let undecrypted_stats = room_undecrypted_stats(&db, &room_id)
            .await
            .unwrap_or_default();
        if room_should_heal_undecrypted(cfg, &import_outcome, &room_state, undecrypted_stats) {
            heal_candidates.push((room_id.clone(), undecrypted_stats));
        }
        room_states.insert(room_id.clone(), room_state);
        undecrypted_stats_by_room.insert(room_id, undecrypted_stats);
    }

    heal_candidates.sort_by(|left, right| {
        right
            .1
            .latest_event_ts_ms
            .cmp(&left.1.latest_event_ts_ms)
            .then(right.1.count.cmp(&left.1.count))
    });
    let heal_room_ids: HashSet<String> = if import_outcome.status == "imported" {
        heal_candidates
            .into_iter()
            .map(|(room_id, _)| room_id)
            .collect()
    } else {
        heal_candidates
            .into_iter()
            .take(cfg.undecrypted_heal_room_limit)
            .map(|(room_id, _)| room_id)
            .collect()
    };

    for room in joined_rooms {
        let room_id = room.room_id().to_string();
        let room_name = resolve_room_name(&room, self_user_id).await;
        let room_state = room_states
            .get(&room_id)
            .cloned()
            .unwrap_or(PersistedRoomState {
                last_event_ts_ms: None,
                backfill_complete: false,
                metadata: Value::Object(Default::default()),
            });
        let participants_json = room_participants_json(&room, self_user_id).await;
        let undecrypted_stats = undecrypted_stats_by_room
            .get(&room_id)
            .copied()
            .unwrap_or_default();
        let should_reprocess_room = heal_room_ids.contains(&room_id);

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
            should_reprocess_room,
            undecrypted_stats.count,
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
        .user_agent("liferadar-matrix-probe/0.1.0")
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

fn persist_client_session(
    session_path: &PathBuf,
    client: &Client,
    next_batch: Option<String>,
) -> Result<()> {
    let matrix_session = client
        .matrix_auth()
        .session()
        .context("matrix session is unavailable for persistence")?;
    let existing = read_optional_session_file(session_path);
    let session = SessionFile {
        access_token: matrix_session.tokens.access_token,
        refresh_token: matrix_session.tokens.refresh_token,
        user_id: matrix_session.meta.user_id.to_string(),
        device_id: matrix_session.meta.device_id.to_string(),
        homeserver: client.homeserver().to_string(),
        next_batch: next_batch.or_else(|| {
            existing
                .as_ref()
                .and_then(|session| session.next_batch.clone())
        }),
        expires_at: existing
            .as_ref()
            .and_then(|session| session.expires_at.clone()),
        expires_in: existing.as_ref().and_then(|session| session.expires_in),
        saved_at: Some(iso_now()),
    };
    write_session_file_atomic(session_path, &session)
}

fn persist_session_checkpoint(session_path: &PathBuf, next_batch: &str) -> Result<()> {
    let Some(existing) = read_optional_session_file(session_path) else {
        return Ok(());
    };

    let session = SessionFile {
        next_batch: Some(next_batch.to_string()),
        saved_at: Some(iso_now()),
        ..existing
    };
    write_session_file_atomic(session_path, &session)
}

fn read_optional_session_file(session_path: &PathBuf) -> Option<SessionFile> {
    let content = fs::read_to_string(session_path).ok()?;
    serde_json::from_str(&content).ok()
}

fn write_session_file_atomic(session_path: &PathBuf, session: &SessionFile) -> Result<()> {
    let Some(parent) = session_path.parent() else {
        return Err(anyhow::anyhow!(
            "matrix session path {} has no parent directory",
            session_path.display()
        ));
    };

    fs::create_dir_all(parent).with_context(|| format!("failed to create {}", parent.display()))?;
    let tmp_path = session_path.with_extension("json.tmp");
    let payload =
        serde_json::to_string_pretty(session).context("failed to serialize matrix session")?;
    fs::write(&tmp_path, payload)
        .with_context(|| format!("failed to write {}", tmp_path.display()))?;
    fs::rename(&tmp_path, session_path)
        .with_context(|| format!("failed to replace {}", session_path.display()))?;
    Ok(())
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
    let _ = maybe_import_local_secrets_bundle(cfg, &session_file).await?;

    let encryption_settings = EncryptionSettings {
        backup_download_strategy: BackupDownloadStrategy::OneShot,
        ..Default::default()
    };

    let client = Client::builder()
        .homeserver_url(session_file.homeserver.clone())
        .handle_refresh_tokens()
        .with_encryption_settings(encryption_settings)
        .sqlite_store(cfg.store_path.clone(), None)
        .build()
        .await
        .context("failed to build matrix rust client")?;

    let user_id: OwnedUserId = session_file
        .user_id
        .parse()
        .context("invalid matrix user_id")?;
    let device_id: OwnedDeviceId = session_file.device_id.as_str().into();
    // Use refresh_token from session file if available
    let refresh_token = session_file
        .refresh_token
        .as_ref()
        .filter(|t| !t.is_empty())
        .cloned();

    let session = MatrixSession {
        meta: SessionMeta { user_id, device_id },
        tokens: SessionTokens {
            access_token: session_file.access_token.clone(),
            refresh_token,
        },
    };

    client
        .matrix_auth()
        .restore_session(session, RoomLoadSettings::default())
        .await
        .context("failed to restore matrix session")?;
    let session_path = cfg.session_path.clone();
    client
        .set_session_callbacks(
            Box::new(|client| {
                client.session_tokens().ok_or_else(|| {
                    Box::new(io::Error::new(
                        io::ErrorKind::NotFound,
                        "missing session tokens",
                    )) as Box<dyn std::error::Error + Send + Sync>
                })
            }),
            Box::new(move |client| {
                persist_client_session(&session_path, &client, None).map_err(|err| {
                    Box::new(io::Error::other(err.to_string()))
                        as Box<dyn std::error::Error + Send + Sync>
                })
            }),
        )
        .context("failed to configure matrix session persistence callbacks")?;

    Ok((client, session_file))
}

async fn send_message_via_sdk(cfg: &ProbeConfig) -> Result<SendMessageResult> {
    let room_id_value =
        env::var("LIFERADAR_SEND_ROOM_ID").context("missing LIFERADAR_SEND_ROOM_ID")?;
    let content_text = env::var("LIFERADAR_SEND_TEXT").context("missing LIFERADAR_SEND_TEXT")?;
    let room_id: OwnedRoomId = room_id_value
        .parse()
        .context("invalid LIFERADAR_SEND_ROOM_ID")?;

    send_debug(format!("start send_message room_id={room_id_value}"));
    let (client, session_file) = build_client(cfg).await?;
    send_debug("build_client complete");
    let recovery_outcome = maybe_restore_secret_storage(cfg, &client).await?;
    send_debug(format!(
        "secret recovery status={} detail={}",
        recovery_outcome.status, recovery_outcome.detail
    ));
    maybe_import_room_keys(cfg, &client).await?;
    send_debug("room-key import phase complete");
    let sync_response = client
        .sync_once(SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds)))
        .await
        .context("matrix rust sync_once failed before send")?;
    send_debug(format!(
        "initial sync complete next_batch={}",
        sync_response.next_batch
    ));
    persist_client_session(
        &cfg.session_path,
        &client,
        Some(sync_response.next_batch.clone()),
    )?;
    send_debug("persisted session after initial sync");
    let verification_outcome = maybe_verify_own_device(&client).await?;
    send_debug(format!(
        "own device verification status={} detail={}",
        verification_outcome.status, verification_outcome.detail
    ));

    if let Some(room) = client.get_room(&room_id) {
        send_debug("room found in SDK after initial sync; sending via SDK");
        let response = send_via_sdk_room(&room, &content_text).await.context("matrix SDK send failed")?;

        return Ok(SendMessageResult {
            status: "sent",
            event_id: response.event_id.to_string(),
        });
    }

    send_debug("room missing in SDK after initial sync; trying SDK room hydration via join");
    if let Some(room) = hydrate_joined_room_via_sdk(&client, &room_id).await? {
        send_debug("SDK room hydration succeeded for requested room; sending via SDK");
        let response = send_via_sdk_room(&room, &content_text)
            .await
            .context("matrix SDK send failed after SDK room hydration")?;

        return Ok(SendMessageResult {
            status: "sent",
            event_id: response.event_id.to_string(),
        });
    }
    send_debug("SDK room hydration did not materialize requested room");

    let http_client = HttpClient::builder()
        .user_agent("liferadar-matrix-probe/0.1.0")
        .timeout(Duration::from_secs(cfg.timeout_seconds))
        .build()
        .context("failed to build HTTP client for Matrix send fallback")?;
    send_debug("SDK room missing; trying direct HTTP send to requested room");

    if let Ok(result) = send_message_via_http(
        &http_client,
        &session_file.access_token,
        &session_file.homeserver,
        &room_id_value,
        &content_text,
        cfg.timeout_seconds,
    )
    .await
    {
        send_debug("direct HTTP send to requested room succeeded");
        return Ok(result);
    }
    send_debug("direct HTTP send to requested room failed; resolving replacement room");

    let Some((replacement_room_id, replacement_title)) = resolve_replacement_room_id(
        &http_client,
        &session_file.access_token,
        &session_file.homeserver,
        &room_id_value,
        cfg.timeout_seconds,
    )
    .await?
    else {
        send_debug("replacement room lookup returned none");
        anyhow::bail!("matrix room not found in restored session");
    };
    send_debug(format!(
        "replacement room resolved replacement_room_id={replacement_room_id} title={}",
        replacement_title.as_deref().unwrap_or("")
    ));

    let replacement_owned_room_id: OwnedRoomId = replacement_room_id
        .parse()
        .with_context(|| format!("invalid replacement room id {}", replacement_room_id))?;

    send_debug("running full sync before remapped send");
    let full_sync_response = client
        .sync_once(SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds)))
        .await
        .context("matrix full sync failed before remapped send")?;
    send_debug(format!(
        "full sync before remapped send complete next_batch={}",
        full_sync_response.next_batch
    ));
    persist_client_session(
        &cfg.session_path,
        &client,
        Some(full_sync_response.next_batch.clone()),
    )?;
    send_debug("persisted session after full sync");

    let result = if let Some(room) = client.get_room(&replacement_owned_room_id) {
        send_debug("replacement room found in SDK; sending via SDK");
        let response = send_via_sdk_room(&room, &content_text)
            .await
            .with_context(|| format!("matrix SDK send failed after remap to {}", replacement_room_id))?;
        SendMessageResult {
            status: "sent",
            event_id: response.event_id.to_string(),
        }
    } else if let Some(room) = hydrate_joined_room_via_sdk(&client, &replacement_owned_room_id).await? {
        send_debug("replacement room hydrated via SDK join; sending via SDK");
        let response = send_via_sdk_room(&room, &content_text)
            .await
            .with_context(|| format!("matrix SDK send failed after hydrating remap to {}", replacement_room_id))?;
        SendMessageResult {
            status: "sent",
            event_id: response.event_id.to_string(),
        }
    } else {
        send_debug("replacement room still missing in SDK; trying HTTP send to replacement room");
        send_message_via_http(
            &http_client,
            &session_file.access_token,
            &session_file.homeserver,
            &replacement_room_id,
            &content_text,
            cfg.timeout_seconds,
        )
        .await
        .with_context(|| format!("matrix HTTP send failed after remap to {}", replacement_room_id))?
    };
    send_debug("remapped send completed; reconciling conversation room id");

    let db = connect_postgres(cfg, "failed to connect to liferadar postgres for room remap").await?;
    reconcile_conversation_room_id(
        &db,
        &room_id_value,
        &replacement_room_id,
        replacement_title.as_deref(),
    )
    .await?;
    send_debug("conversation room id reconciliation complete");

    Ok(result)
}

async fn run_interactive_device_verification(cfg: &ProbeConfig) -> Result<()> {
    let target_device_value = env::var("LIFERADAR_VERIFY_TARGET_DEVICE_ID")
        .context("missing LIFERADAR_VERIFY_TARGET_DEVICE_ID")?;
    let target_device_id: OwnedDeviceId = target_device_value
        .as_str()
        .into();

    let (client, session_file) = build_client(cfg).await?;
    let recovery_outcome = maybe_restore_secret_storage(cfg, &client).await?;
    emit_verification_event(json!({
        "event": "secret_recovery",
        "status": recovery_outcome.status,
        "detail": recovery_outcome.detail,
    }))?;
    let import_outcome = maybe_import_room_keys(cfg, &client).await?;
    emit_verification_event(json!({
        "event": "room_key_import",
        "status": import_outcome.status,
        "detail": import_outcome.detail,
        "imported_count": import_outcome.imported_count,
        "total_count": import_outcome.total_count,
    }))?;

    let sync_response = client
        .sync_once(SyncSettings::default().timeout(Duration::from_secs(cfg.timeout_seconds)))
        .await
        .context("matrix rust sync_once failed before verification")?;
    persist_client_session(
        &cfg.session_path,
        &client,
        Some(sync_response.next_batch.clone()),
    )?;

    let own_user_id = session_file.user_id.parse::<OwnedUserId>()
        .context("invalid user id in matrix session file")?;
    let Some(device) = client
        .encryption()
        .get_device(&own_user_id, &target_device_id)
        .await
        .context("failed to look up target matrix device")?
    else {
        anyhow::bail!("target Matrix device was not found in the current device list");
    };
    let Some(own_identity) = client
        .encryption()
        .get_user_identity(&own_user_id)
        .await
        .context("failed to load own Matrix user identity")?
    else {
        anyhow::bail!("own Matrix user identity is not available yet");
    };

    emit_verification_event(json!({
        "event": "device_found",
        "user_id": own_user_id,
        "device_id": target_device_value,
        "verified": device.is_verified(),
        "locally_trusted": device.is_locally_trusted(),
        "display_name": device.display_name(),
    }))?;

    if device.is_verified() {
        emit_verification_event(json!({
            "event": "verification_complete",
            "status": "already_verified",
            "device_id": target_device_value,
        }))?;
        return Ok(());
    }

    let verification = own_identity
        .request_verification_with_methods(vec![VerificationMethod::SasV1])
        .await
        .context("failed to request self-verification with existing devices")?;
    let flow_id = verification.flow_id().to_string();
    emit_verification_event(json!({
        "event": "request_created",
        "status": "waiting_for_accept",
        "flow_id": flow_id,
        "device_id": target_device_value,
        "detail": format!(
            "Verification request sent to your other Matrix devices. Accept it on {}.",
            target_device_value
        ),
    }))?;

    let sync_client = client.clone();
    let sync_session_path = cfg.session_path.clone();
    let sync_timeout = cfg.timeout_seconds;
    let sync_task = tokio::spawn(async move {
        let mut since = None::<String>;
        loop {
            let settings = match &since {
                Some(token) => SyncSettings::new().token(token.clone()),
                None => SyncSettings::new(),
            }
            .timeout(Duration::from_secs(sync_timeout));

            match sync_client.sync_once(settings).await {
                Ok(response) => {
                    since = Some(response.next_batch.clone());
                    let _ = persist_client_session(
                        &sync_session_path,
                        &sync_client,
                        Some(response.next_batch),
                    );
                }
                Err(err) => {
                    eprintln!("[matrix-verify-sync] {err:#}");
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }
        }
    });

    let (command_tx, mut command_rx) = mpsc::unbounded_channel::<VerificationCommand>();
    tokio::spawn(async move {
        let mut lines = BufReader::new(tokio::io::stdin()).lines();
        loop {
            match lines.next_line().await {
                Ok(Some(line)) => {
                    let command = match line.trim().to_ascii_lowercase().as_str() {
                        "yes" | "confirm" => Some(VerificationCommand::Confirm),
                        "no" | "reject" | "mismatch" => Some(VerificationCommand::Reject),
                        "cancel" => Some(VerificationCommand::Cancel),
                        _ => None,
                    };
                    if let Some(command) = command {
                        let _ = command_tx.send(command);
                    }
                }
                Ok(None) | Err(_) => break,
            }
        }
    });

    let verification_result =
        wait_for_verification_request(&verification, &mut command_rx, &target_device_value).await;
    sync_task.abort();
    let _ = sync_task.await;
    verification_result
}

async fn wait_for_verification_request(
    verification: &matrix_sdk::encryption::verification::VerificationRequest,
    command_rx: &mut mpsc::UnboundedReceiver<VerificationCommand>,
    target_device_id: &str,
) -> Result<()> {
    let mut changes = verification.changes();
    let mut handled_initial_state = false;

    loop {
        if !handled_initial_state {
            handled_initial_state = true;
            match verification.state() {
                VerificationRequestState::Created { .. } => {
                    emit_verification_event(json!({
                        "event": "request_pending",
                        "status": "waiting_for_accept",
                        "device_id": target_device_id,
                    }))?;
                }
                VerificationRequestState::Requested { their_methods: _, other_device_data } => {
                    emit_verification_event(json!({
                        "event": "request_received",
                        "status": "requested",
                        "device_id": target_device_id,
                        "other_device_id": other_device_data.device_id().to_string(),
                        "detail": format!(
                            "Verification request acknowledged by {}.",
                            other_device_data.device_id()
                        ),
                    }))?;
                }
                VerificationRequestState::Ready { their_methods: _, our_methods: _, other_device_data } => {
                    let accepted_device_id = other_device_data.device_id().to_string();
                    emit_verification_event(json!({
                        "event": "request_ready",
                        "status": "ready_for_sas",
                        "device_id": target_device_id,
                        "other_device_id": accepted_device_id,
                        "detail": if accepted_device_id == target_device_id {
                            format!("{} accepted the request. Starting emoji verification…", target_device_id)
                        } else {
                            format!(
                                "{} accepted the request instead of {}. Continuing with the accepting device.",
                                accepted_device_id,
                                target_device_id
                            )
                        },
                    }))?;
                    if let Some(sas) = verification
                        .start_sas()
                        .await
                        .context("failed to start SAS verification")?
                    {
                        return wait_for_sas_verification(&sas, command_rx, target_device_id).await;
                    }
                }
                VerificationRequestState::Transitioned { verification } => {
                    if let Verification::SasV1(sas) = verification {
                        emit_verification_event(json!({
                            "event": "sas_started",
                            "status": "waiting_for_emoji",
                            "device_id": target_device_id,
                        }))?;
                        return wait_for_sas_verification(&sas, command_rx, target_device_id).await;
                    }
                }
                VerificationRequestState::Done => {
                    emit_verification_event(json!({
                        "event": "verification_complete",
                        "status": "done",
                        "device_id": target_device_id,
                    }))?;
                    return Ok(());
                }
                VerificationRequestState::Cancelled(cancel_info) => {
                    emit_verification_event(json!({
                        "event": "verification_cancelled",
                        "status": "cancelled",
                        "reason": cancel_info.reason(),
                        "device_id": target_device_id,
                    }))?;
                    anyhow::bail!("verification cancelled: {}", cancel_info.reason());
                }
            }
        }

        tokio::select! {
            maybe_command = command_rx.recv() => {
                match maybe_command {
                    Some(VerificationCommand::Cancel) | Some(VerificationCommand::Reject) => {
                        verification.cancel().await.context("failed to cancel pending verification request")?;
                        emit_verification_event(json!({
                            "event": "request_cancelled_locally",
                            "status": "cancelled",
                            "device_id": target_device_id,
                        }))?;
                    }
                    Some(VerificationCommand::Confirm) | None => {}
                }
            }
            maybe_state = changes.next() => {
                let Some(state) = maybe_state else {
                    anyhow::bail!("verification request stream ended unexpectedly");
                };

                match state {
                    VerificationRequestState::Created { .. } => {
                        emit_verification_event(json!({
                            "event": "request_pending",
                            "status": "waiting_for_accept",
                            "device_id": target_device_id,
                        }))?;
                    }
                    VerificationRequestState::Requested { their_methods: _, other_device_data } => {
                        emit_verification_event(json!({
                            "event": "request_received",
                            "status": "requested",
                            "device_id": target_device_id,
                            "other_device_id": other_device_data.device_id().to_string(),
                            "detail": format!(
                                "Verification request acknowledged by {}.",
                                other_device_data.device_id()
                            ),
                        }))?;
                    }
                    VerificationRequestState::Ready { their_methods: _, our_methods: _, other_device_data } => {
                        let accepted_device_id = other_device_data.device_id().to_string();
                        emit_verification_event(json!({
                            "event": "request_ready",
                            "status": "ready_for_sas",
                            "device_id": target_device_id,
                            "other_device_id": accepted_device_id,
                            "detail": if accepted_device_id == target_device_id {
                                format!("{} accepted the request. Starting emoji verification…", target_device_id)
                            } else {
                                format!(
                                    "{} accepted the request instead of {}. Continuing with the accepting device.",
                                    accepted_device_id,
                                    target_device_id
                                )
                            },
                        }))?;
                        if let Some(sas) = verification
                            .start_sas()
                            .await
                            .context("failed to start SAS verification")?
                        {
                            return wait_for_sas_verification(&sas, command_rx, target_device_id).await;
                        }
                    }
                    VerificationRequestState::Transitioned { verification } => {
                        if let Verification::SasV1(sas) = verification {
                            emit_verification_event(json!({
                                "event": "sas_started",
                                "status": "waiting_for_emoji",
                                "device_id": target_device_id,
                            }))?;
                            return wait_for_sas_verification(&sas, command_rx, target_device_id).await;
                        }
                    }
                    VerificationRequestState::Done => {
                        emit_verification_event(json!({
                            "event": "verification_complete",
                            "status": "done",
                            "device_id": target_device_id,
                        }))?;
                        return Ok(());
                    }
                    VerificationRequestState::Cancelled(cancel_info) => {
                        emit_verification_event(json!({
                            "event": "verification_cancelled",
                            "status": "cancelled",
                            "reason": cancel_info.reason(),
                            "device_id": target_device_id,
                        }))?;
                        anyhow::bail!("verification cancelled: {}", cancel_info.reason());
                    }
                }
            }
        }
    }
}

async fn wait_for_sas_verification(
    sas: &matrix_sdk::encryption::verification::SasVerification,
    command_rx: &mut mpsc::UnboundedReceiver<VerificationCommand>,
    target_device_id: &str,
) -> Result<()> {
    let mut changes = sas.changes();
    let mut awaiting_confirmation = false;
    let mut handled_initial_state = false;

    loop {
        if !handled_initial_state {
            handled_initial_state = true;
            match sas.state() {
                SasState::Created { .. } => {
                    emit_verification_event(json!({
                        "event": "sas_created",
                        "status": "waiting_for_emoji",
                        "device_id": target_device_id,
                    }))?;
                }
                SasState::Started { .. } => {
                    emit_verification_event(json!({
                        "event": "sas_started",
                        "status": "waiting_for_emoji",
                        "device_id": target_device_id,
                    }))?;
                }
                SasState::Accepted { .. } => {
                    emit_verification_event(json!({
                        "event": "sas_accepted",
                        "status": "waiting_for_emoji",
                        "device_id": target_device_id,
                    }))?;
                }
                SasState::KeysExchanged { emojis, decimals } => {
                    awaiting_confirmation = true;
                    let emoji_payload = emojis
                        .map(|items| {
                            items
                                .emojis
                                .into_iter()
                                .map(|emoji| json!({
                                    "symbol": emoji.symbol,
                                    "description": emoji.description,
                                }))
                                .collect::<Vec<_>>()
                        })
                        .unwrap_or_default();
                    emit_verification_event(json!({
                        "event": "emoji_ready",
                        "status": "waiting_for_confirm",
                        "device_id": target_device_id,
                        "emojis": emoji_payload,
                        "decimals": [decimals.0, decimals.1, decimals.2],
                    }))?;
                }
                SasState::Confirmed => {
                    emit_verification_event(json!({
                        "event": "sas_confirmed",
                        "status": "confirming",
                        "device_id": target_device_id,
                    }))?;
                }
                SasState::Done { .. } => {
                    let verified_device = sas.other_device();
                    emit_verification_event(json!({
                        "event": "verification_complete",
                        "status": "done",
                        "device_id": target_device_id,
                        "verified_device_id": verified_device.device_id().to_string(),
                        "verified_user_id": verified_device.user_id().to_string(),
                        "locally_trusted": verified_device.is_locally_trusted(),
                    }))?;
                    return Ok(());
                }
                SasState::Cancelled(cancel_info) => {
                    emit_verification_event(json!({
                        "event": "verification_cancelled",
                        "status": "cancelled",
                        "reason": cancel_info.reason(),
                        "device_id": target_device_id,
                    }))?;
                    anyhow::bail!("SAS verification cancelled: {}", cancel_info.reason());
                }
            }
        }

        tokio::select! {
            maybe_command = command_rx.recv() => {
                let Some(command) = maybe_command else {
                    continue;
                };

                if !awaiting_confirmation {
                    if matches!(command, VerificationCommand::Cancel | VerificationCommand::Reject) {
                        sas.cancel().await.context("failed to cancel SAS verification")?;
                        emit_verification_event(json!({
                            "event": "verification_cancelled_locally",
                            "status": "cancelled",
                            "device_id": target_device_id,
                        }))?;
                    }
                    continue;
                }

                match command {
                    VerificationCommand::Confirm => {
                        sas.confirm().await.context("failed to confirm SAS verification")?;
                        awaiting_confirmation = false;
                        emit_verification_event(json!({
                            "event": "verification_confirmed",
                            "status": "confirming",
                            "device_id": target_device_id,
                        }))?;
                    }
                    VerificationCommand::Reject => {
                        sas.mismatch().await.context("failed to reject SAS verification")?;
                        awaiting_confirmation = false;
                        emit_verification_event(json!({
                            "event": "verification_rejected",
                            "status": "rejected",
                            "device_id": target_device_id,
                        }))?;
                    }
                    VerificationCommand::Cancel => {
                        sas.cancel().await.context("failed to cancel SAS verification")?;
                        awaiting_confirmation = false;
                        emit_verification_event(json!({
                            "event": "verification_cancelled_locally",
                            "status": "cancelled",
                            "device_id": target_device_id,
                        }))?;
                    }
                }
            }
            maybe_state = changes.next() => {
                let Some(state) = maybe_state else {
                    anyhow::bail!("SAS verification stream ended unexpectedly");
                };

                match state {
                    SasState::Created { .. } => {
                        emit_verification_event(json!({
                            "event": "sas_created",
                            "status": "waiting_for_emoji",
                            "device_id": target_device_id,
                        }))?;
                    }
                    SasState::Started { .. } => {
                        emit_verification_event(json!({
                            "event": "sas_started",
                            "status": "waiting_for_emoji",
                            "device_id": target_device_id,
                        }))?;
                    }
                    SasState::Accepted { .. } => {
                        emit_verification_event(json!({
                            "event": "sas_accepted",
                            "status": "waiting_for_emoji",
                            "device_id": target_device_id,
                        }))?;
                    }
                    SasState::KeysExchanged { emojis, decimals } => {
                        awaiting_confirmation = true;
                        let emoji_payload = emojis
                            .map(|items| {
                                items
                                    .emojis
                                    .into_iter()
                                    .map(|emoji| json!({
                                        "symbol": emoji.symbol,
                                        "description": emoji.description,
                                    }))
                                    .collect::<Vec<_>>()
                            })
                            .unwrap_or_default();
                        emit_verification_event(json!({
                            "event": "emoji_ready",
                            "status": "waiting_for_confirm",
                            "device_id": target_device_id,
                            "emojis": emoji_payload,
                            "decimals": [decimals.0, decimals.1, decimals.2],
                        }))?;
                    }
                    SasState::Confirmed => {
                        emit_verification_event(json!({
                            "event": "sas_confirmed",
                            "status": "confirming",
                            "device_id": target_device_id,
                        }))?;
                    }
                    SasState::Done { .. } => {
                        let verified_device = sas.other_device();
                        emit_verification_event(json!({
                            "event": "verification_complete",
                            "status": "done",
                            "device_id": target_device_id,
                            "verified_device_id": verified_device.device_id().to_string(),
                            "verified_user_id": verified_device.user_id().to_string(),
                            "locally_trusted": verified_device.is_locally_trusted(),
                        }))?;
                        return Ok(());
                    }
                    SasState::Cancelled(cancel_info) => {
                        emit_verification_event(json!({
                            "event": "verification_cancelled",
                            "status": "cancelled",
                            "reason": cancel_info.reason(),
                            "device_id": target_device_id,
                        }))?;
                        anyhow::bail!("SAS verification cancelled: {}", cancel_info.reason());
                    }
                }
            }
        }
    }
}

fn emit_verification_event(payload: serde_json::Value) -> Result<()> {
    use std::io::Write;

    let mut stdout = io::stdout();
    serde_json::to_writer(&mut stdout, &payload).context("failed to serialize verification event")?;
    stdout.write_all(b"\n").context("failed to terminate verification event line")?;
    stdout.flush().context("failed to flush verification event line")?;
    Ok(())
}

async fn send_via_sdk_room(
    room: &matrix_sdk::Room,
    content_text: &str,
) -> Result<matrix_sdk::ruma::api::client::message::send_message_event::v3::Response> {
    room.send(RoomMessageEventContent::text_plain(content_text))
        .await
        .context("matrix SDK room send failed")
}

async fn hydrate_joined_room_via_sdk(
    client: &Client,
    room_id: &matrix_sdk::ruma::RoomId,
) -> Result<Option<matrix_sdk::Room>> {
    match client.join_room_by_id(room_id).await {
        Ok(room) => Ok(Some(room)),
        Err(err) => {
            send_debug(format!("SDK join_room_by_id failed for {room_id}: {err:#}"));
            Ok(client.get_room(room_id))
        }
    }
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

async fn send_message_via_http(
    client: &HttpClient,
    access_token: &str,
    base_url: &str,
    room_id: &str,
    content_text: &str,
    timeout_secs: u64,
) -> Result<SendMessageResult> {
    let base_url = base_url.trim_end_matches('/');
    let txn_id = format!("liferadar-{}", now_unix_ms());
    let url = format!(
        "{}/_matrix/client/v3/rooms/{}/send/m.room.message/{}",
        base_url,
        urlencoding::encode(room_id),
        txn_id
    );
    let resp = client
        .put(&url)
        .header("Authorization", format!("Bearer {}", access_token))
        .timeout(Duration::from_secs(timeout_secs))
        .json(&serde_json::json!({
            "msgtype": "m.text",
            "body": content_text
        }))
        .send()
        .await
        .context("matrix HTTP send request failed")?;

    let status = resp.status();
    let body = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        anyhow::bail!("Matrix HTTP send returned {}: {}", status, body);
    }

    let payload: Value =
        serde_json::from_str(&body).context("failed to parse Matrix HTTP send response")?;
    let event_id = payload
        .get("event_id")
        .and_then(Value::as_str)
        .context("Matrix HTTP send response missing event_id")?;

    Ok(SendMessageResult {
        status: "sent",
        event_id: event_id.to_string(),
    })
}

async fn resolve_replacement_room_id(
    client: &HttpClient,
    access_token: &str,
    base_url: &str,
    stale_room_id: &str,
    timeout_secs: u64,
) -> Result<Option<(String, Option<String>)>> {
    let old_state = http_fetch_room_state(client, access_token, base_url, stale_room_id, timeout_secs)
        .await
        .with_context(|| format!("failed to fetch state for stale room {}", stale_room_id))?;
    let bridge_targets = extract_bridge_targets(&old_state);
    if bridge_targets.is_empty() {
        return Ok(None);
    }

    let joined_rooms = http_fetch_joined_rooms(client, access_token, base_url, timeout_secs).await?;
    if let Some(found) = lookup_joined_room_by_bridge_state(
        client,
        access_token,
        base_url,
        &joined_rooms,
        &bridge_targets,
        stale_room_id,
        timeout_secs,
    )
    .await?
    {
        return Ok(Some(found));
    }

    for room_id in joined_rooms {
        if room_id == stale_room_id {
            continue;
        }
        for target in &bridge_targets {
            if let Some(state_event) = http_fetch_room_state_event(
                client,
                access_token,
                base_url,
                &room_id,
                "m.bridge",
                &target.state_key,
                timeout_secs,
            )
            .await?
            {
                let matched = extract_bridge_targets(&[state_event]);
                if matched.iter().any(|candidate| bridge_targets_match(candidate, target)) {
                    return Ok(Some((room_id.clone(), target.display_name.clone())));
                }
            }
            if let Some(state_event) = http_fetch_room_state_event(
                client,
                access_token,
                base_url,
                &room_id,
                "uk.half-shot.bridge",
                &target.state_key,
                timeout_secs,
            )
            .await?
            {
                let matched = extract_bridge_targets(&[state_event]);
                if matched.iter().any(|candidate| bridge_targets_match(candidate, target)) {
                    return Ok(Some((room_id.clone(), target.display_name.clone())));
                }
            }
        }
    }

    Ok(None)
}

async fn lookup_joined_room_by_bridge_state(
    client: &HttpClient,
    access_token: &str,
    base_url: &str,
    joined_rooms: &[String],
    bridge_targets: &[BridgeTarget],
    stale_room_id: &str,
    timeout_secs: u64,
) -> Result<Option<(String, Option<String>)>> {
    let exact_targets: Vec<BridgeTarget> = bridge_targets
        .iter()
        .filter(|target| !target.state_key.is_empty())
        .cloned()
        .collect();
    if exact_targets.is_empty() {
        return Ok(None);
    }

    let mut join_set = JoinSet::new();
    let mut next_idx = 0usize;
    let concurrency = 32usize;

    while next_idx < joined_rooms.len() || !join_set.is_empty() {
        while next_idx < joined_rooms.len() && join_set.len() < concurrency {
            let room_id = joined_rooms[next_idx].clone();
            next_idx += 1;
            if room_id == stale_room_id {
                continue;
            }
            let client = client.clone();
            let access_token = access_token.to_string();
            let base_url = base_url.to_string();
            let targets = exact_targets.clone();
            join_set.spawn(async move {
                lookup_room_by_exact_bridge_state(
                    client,
                    access_token,
                    base_url,
                    room_id,
                    targets,
                    timeout_secs,
                )
                .await
            });
        }

        let Some(joined) = join_set.join_next().await else {
            break;
        };

        match joined {
            Ok(Ok(Some(found))) => return Ok(Some(found)),
            Ok(Ok(None)) => {}
            Ok(Err(err)) => return Err(err),
            Err(err) => return Err(anyhow::anyhow!("bridge-state lookup task failed: {err}")),
        }
    }

    Ok(None)
}

async fn lookup_room_by_exact_bridge_state(
    client: HttpClient,
    access_token: String,
    base_url: String,
    room_id: String,
    targets: Vec<BridgeTarget>,
    timeout_secs: u64,
) -> Result<Option<(String, Option<String>)>> {
    for target in &targets {
        for event_type in ["m.bridge", "uk.half-shot.bridge"] {
            if let Some(state_event) = http_fetch_room_state_event(
                &client,
                &access_token,
                &base_url,
                &room_id,
                event_type,
                &target.state_key,
                timeout_secs,
            )
            .await?
            {
                let matched = extract_bridge_targets(&[state_event]);
                if matched.iter().any(|candidate| bridge_targets_match(candidate, target)) {
                    return Ok(Some((room_id.clone(), target.display_name.clone())));
                }
            }
        }
    }

    Ok(None)
}

async fn reconcile_conversation_room_id(
    db: &PgClient,
    stale_room_id: &str,
    replacement_room_id: &str,
    replacement_title: Option<&str>,
) -> Result<()> {
    if stale_room_id == replacement_room_id {
        return Ok(());
    }

    let stale_row = db
        .query_opt(
            "select id::text from life_radar.conversations where source = 'matrix' and external_id = $1",
            &[&stale_room_id],
        )
        .await
        .context("failed to load stale matrix conversation")?;
    let Some(stale_row) = stale_row else {
        return Ok(());
    };
    let stale_id: String = stale_row.get(0);

    let replacement_row = db
        .query_opt(
            "select id::text from life_radar.conversations where source = 'matrix' and external_id = $1",
            &[&replacement_room_id],
        )
        .await
        .context("failed to load replacement matrix conversation")?;

    let remap_meta = serde_json::json!({
        "room_id_remapped_from": stale_room_id,
        "room_id_remapped_to": replacement_room_id,
        "room_id_remapped_at": iso_now(),
        "room_id_remap_reason": "bridge-room-replacement-detected"
    })
    .to_string();

    if let Some(replacement_row) = replacement_row {
        let replacement_id: String = replacement_row.get(0);
        db.execute(
            "update life_radar.message_events set conversation_id = $1::uuid where conversation_id = $2::uuid",
            &[&replacement_id, &stale_id],
        )
        .await
        .context("failed to move message events to replacement conversation")?;
        db.execute(
            "update life_radar.conversations
             set metadata = metadata || $2::text::jsonb,
                 title = coalesce(title, $3),
                 updated_at = now()
             where id = $1::uuid",
            &[&replacement_id, &remap_meta, &replacement_title],
        )
        .await
        .context("failed to annotate replacement conversation")?;
        db.execute(
            "update life_radar.conversations
             set state = 'archived',
                 metadata = metadata || $2::text::jsonb,
                 updated_at = now()
             where id = $1::uuid",
            &[&stale_id, &remap_meta],
        )
        .await
        .context("failed to archive stale matrix conversation")?;
    } else {
        db.execute(
            "update life_radar.conversations
             set external_id = $2,
                 title = coalesce($3, title),
                 metadata = metadata || $4::text::jsonb,
                 updated_at = now()
             where source = 'matrix' and external_id = $1",
            &[&stale_room_id, &replacement_room_id, &replacement_title, &remap_meta],
        )
        .await
        .context("failed to remap stale matrix conversation room id")?;
    }

    Ok(())
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

async fn room_undecrypted_stats(db: &PgClient, room_id: &str) -> Result<UndecryptedStats> {
    let row = db
        .query_one(
            "select
                count(*)::bigint as undecrypted_count,
                max((extract(epoch from me.occurred_at) * 1000)::bigint) as latest_undecrypted_ts_ms
             from life_radar.message_events me
             join life_radar.conversations c on c.id = me.conversation_id
             where c.source = 'matrix'
               and c.external_id = $1
               and me.content_text = '[undecrypted]'",
            &[&room_id],
        )
        .await
        .with_context(|| format!("failed to check undecrypted events for room {room_id}"))?;

    Ok(UndecryptedStats {
        count: row.get::<usize, i64>(0),
        latest_event_ts_ms: row
            .get::<usize, Option<i64>>(1)
            .map(|value| value.max(0) as u64),
    })
}

fn room_should_heal_undecrypted(
    cfg: &ProbeConfig,
    import_outcome: &ImportOutcome,
    room_state: &PersistedRoomState,
    stats: UndecryptedStats,
) -> bool {
    if stats.count <= 0 {
        return false;
    }

    if import_outcome.status == "imported" {
        return true;
    }

    let Some(heal_meta) = room_state
        .metadata
        .get("undecrypted_heal")
        .and_then(Value::as_object)
    else {
        return true;
    };

    let last_attempt_ms = heal_meta
        .get("attempted_at")
        .and_then(Value::as_str)
        .map(iso_to_unix_ms)
        .unwrap_or_default();
    if last_attempt_ms == 0 {
        return true;
    }

    let cooldown_ms = cfg
        .undecrypted_heal_cooldown_hours
        .saturating_mul(60)
        .saturating_mul(60)
        .saturating_mul(1000);
    let due_by_time = now_unix_ms().saturating_sub(last_attempt_ms) >= cooldown_ms;
    let newer_undecrypted_exists = stats
        .latest_event_ts_ms
        .map(|latest| latest > last_attempt_ms)
        .unwrap_or(false);

    due_by_time || newer_undecrypted_exists
}

fn matrix_room_metadata(
    base_metadata_json: &str,
    message_count: usize,
    existing_metadata: Value,
    latest_event_id: Option<&str>,
    latest_event_at: &str,
    runtime: &str,
    backfill_complete: bool,
    undecrypted_heal_attempted: bool,
    undecrypted_count_before: i64,
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

    if undecrypted_heal_attempted {
        metadata_obj.insert(
            "undecrypted_heal".to_string(),
            serde_json::json!({
                "attempted_at": iso_now(),
                "undecrypted_count_before": undecrypted_count_before,
                "runtime": runtime,
            }),
        );
    }

    Ok(metadata.to_string())
}

async fn maybe_import_room_keys(cfg: &ProbeConfig, client: &Client) -> Result<ImportOutcome> {
    if !cfg.key_import_enabled {
        return Ok(ImportOutcome {
            status: "disabled",
            detail: "room-key import disabled by LIFERADAR_MATRIX_KEY_IMPORT_ENABLED".to_string(),
            imported_count: 0,
            total_count: 0,
        });
    }

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

    let passphrase = read_key_passphrase(&cfg.key_passphrase_path)?;
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

async fn maybe_restore_secret_storage(cfg: &ProbeConfig, client: &Client) -> Result<SecretRecoveryOutcome> {
    let secret_storage = client.encryption().secret_storage();
    let Some(default_key_id) = secret_storage.fetch_default_key_id().await? else {
        return Ok(SecretRecoveryOutcome {
            status: "not_configured",
            detail: "secret storage is not configured on this account".to_string(),
        });
    };

    let Some(cross_signing_status) = client.encryption().cross_signing_status().await else {
        return Ok(SecretRecoveryOutcome {
            status: "unavailable",
            detail: "cross-signing status is not available yet".to_string(),
        });
    };
    if cross_signing_status.has_master && cross_signing_status.has_self_signing {
        return Ok(SecretRecoveryOutcome {
            status: "already_present",
            detail: "cross-signing secrets already available locally".to_string(),
        });
    }

    let candidate_paths = recovery_candidate_paths(cfg);
    if candidate_paths.is_empty() {
        return Ok(SecretRecoveryOutcome {
            status: "missing",
            detail: format!(
                "secret storage default key exists ({:?}) but no recovery material path was configured",
                default_key_id
            ),
        });
    }

    let mut attempted_paths = Vec::new();
    let mut errors = Vec::new();

    for path in candidate_paths {
        let Some(candidate) = read_optional_trimmed_secret(&path)? else {
            continue;
        };

        attempted_paths.push(path.display().to_string());
        match client.encryption().recovery().recover(&candidate).await {
            Ok(()) => {
                return Ok(SecretRecoveryOutcome {
                    status: "recovered",
                    detail: format!("recovered E2EE secrets from {}", path.display()),
                });
            }
            Err(err) => {
                errors.push(format!("{}: {err}", path.display()));
            }
        }
    }

    Ok(SecretRecoveryOutcome {
        status: "failed",
        detail: format!(
            "failed to recover E2EE secrets using {} candidate(s): {}",
            attempted_paths.len(),
            if errors.is_empty() {
                "no readable recovery material found".to_string()
            } else {
                errors.join(" | ")
            }
        ),
    })
}

async fn maybe_import_local_secrets_bundle(
    cfg: &ProbeConfig,
    session_file: &SessionFile,
) -> Result<()> {
    let Some(path) = cfg.secrets_bundle_path.as_ref() else {
        return Ok(());
    };

    if !path.is_file() {
        return Ok(());
    }

    let bundle: SecretsBundle = serde_json::from_str(
        &fs::read_to_string(path)
            .with_context(|| format!("failed to read {}", path.display()))?,
    )
    .with_context(|| format!("invalid secrets bundle {}", path.display()))?;

    let store = matrix_sdk::SqliteCryptoStore::open(&cfg.store_path, None)
        .await
        .context("failed to open Matrix crypto store for secrets bundle import")?;
    let user_id: OwnedUserId = session_file
        .user_id
        .parse()
        .context("invalid matrix user_id while importing local secrets bundle")?;
    let device_id: OwnedDeviceId = session_file.device_id.as_str().into();
    let machine = OlmMachine::with_store(&user_id, &device_id, store, None)
        .await
        .context("failed to create OlmMachine for local secrets bundle import")?;
    machine
        .store()
        .import_secrets_bundle(&bundle)
        .await
        .context("failed to import secrets bundle into Matrix crypto store")?;

    Ok(())
}

async fn maybe_verify_own_device(client: &Client) -> Result<OwnDeviceVerificationOutcome> {
    let Some(cross_signing_status) = client.encryption().cross_signing_status().await else {
        return Ok(OwnDeviceVerificationOutcome {
            status: "unavailable",
            detail: "cross-signing status is not available yet".to_string(),
        });
    };

    if !cross_signing_status.has_self_signing {
        return Ok(OwnDeviceVerificationOutcome {
            status: "missing_self_signing",
            detail: "self-signing key is not available locally".to_string(),
        });
    }

    let Some(own_device) = client.encryption().get_own_device().await? else {
        return Ok(OwnDeviceVerificationOutcome {
            status: "missing_device",
            detail: "current Matrix device was not found in the local store".to_string(),
        });
    };

    if own_device.is_verified() || own_device.is_verified_with_cross_signing() {
        return Ok(OwnDeviceVerificationOutcome {
            status: "already_verified",
            detail: format!("device {} is already verified", own_device.device_id()),
        });
    }

    own_device.verify().await?;

    Ok(OwnDeviceVerificationOutcome {
        status: "verified",
        detail: format!("verified own device {}", own_device.device_id()),
    })
}

/// Attempts to restore E2E encryption keys from the homeserver's E2E Backup.
/// This is called after restoring a session, before syncing.
/// If successful, we can decrypt messages without needing a Beeper key export.
#[allow(dead_code)]
async fn maybe_restore_e2e_backup(client: &Client) -> E2eBackupOutcome {
    let backups = client.encryption().backups();

    // Check if backups are enabled
    if !backups.are_enabled().await {
        return E2eBackupOutcome {
            status: "not_enabled",
            detail: "E2E Backup is not enabled for this client".to_string(),
        };
    }

    // Check if a backup exists on the server
    match backups.fetch_exists_on_server().await {
        Ok(exists) if !exists => {
            return E2eBackupOutcome {
                status: "not_found",
                detail: "No E2E Backup found on server".to_string(),
            };
        }
        Err(err) => {
            eprintln!("E2E Backup check failed (non-fatal): {}", err);
            return E2eBackupOutcome {
                status: "error",
                detail: format!("E2E Backup check failed: {}", err),
            };
        }
        _ => {}
    }

    // Download room keys from the backup for each joined room
    let joined_rooms = client.joined_rooms();
    let mut rooms_restored = 0;
    let mut rooms_failed = 0;

    for room in joined_rooms {
        match backups.download_room_keys_for_room(room.room_id()).await {
            Ok(()) => {
                rooms_restored += 1;
            }
            Err(err) => {
                eprintln!(
                    "E2E Backup restore failed for room {}: {}",
                    room.room_id(),
                    err
                );
                rooms_failed += 1;
            }
        }
    }

    if rooms_restored > 0 {
        eprintln!(
            "E2E Backup restored: {} rooms succeeded, {} failed",
            rooms_restored, rooms_failed
        );
        E2eBackupOutcome {
            status: "restored",
            detail: format!(
                "Restored E2E Backup for {} rooms ({} failed)",
                rooms_restored, rooms_failed
            ),
        }
    } else if rooms_failed > 0 {
        E2eBackupOutcome {
            status: "restore_failed",
            detail: format!("E2E Backup restore failed for all {} rooms", rooms_failed),
        }
    } else {
        E2eBackupOutcome {
            status: "no_rooms",
            detail: "No joined rooms to restore keys for".to_string(),
        }
    }
}

/// Ensures this device's E2E encryption keys are backed up to the homeserver.
/// This is called after syncing, so we have the latest session keys to upload.
#[allow(dead_code)]
async fn ensure_e2e_backup(client: &Client) -> E2eBackupOutcome {
    let backups = client.encryption().backups();

    // Check if backups are enabled
    if !backups.are_enabled().await {
        // Try to create a backup if none exists
        match backups.create().await {
            Ok(()) => {
                eprintln!("E2E Backup created successfully");
                E2eBackupOutcome {
                    status: "created",
                    detail: "Created E2E Backup successfully".to_string(),
                }
            }
            Err(err) => {
                // Non-fatal — keys are still in the local store
                eprintln!("E2E Backup creation failed (non-fatal): {}", err);
                E2eBackupOutcome {
                    status: "create_failed",
                    detail: format!("E2E Backup creation failed: {}", err),
                }
            }
        }
    } else {
        // Backup exists, wait for any pending uploads to complete
        match backups.wait_for_steady_state().await {
            Ok(()) => {
                eprintln!("E2E Backup sync completed");
                E2eBackupOutcome {
                    status: "synced",
                    detail: "E2E Backup synced successfully".to_string(),
                }
            }
            Err(err) => {
                eprintln!("E2E Backup sync wait failed (non-fatal): {}", err);
                E2eBackupOutcome {
                    status: "sync_failed",
                    detail: format!("E2E Backup sync wait failed: {}", err),
                }
            }
        }
    }
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
        .context("failed to connect to liferadar postgres")?;
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

fn read_key_passphrase(path: &PathBuf) -> Result<String> {
    Ok(fs::read_to_string(path)
        .with_context(|| format!("failed to read {}", path.display()))?
        .trim()
        .to_owned())
}

fn read_optional_trimmed_secret(path: &PathBuf) -> Result<Option<String>> {
    if !path.is_file() {
        return Ok(None);
    }

    let value = fs::read_to_string(path)
        .with_context(|| format!("failed to read {}", path.display()))?
        .trim()
        .to_owned();

    if value.is_empty() {
        Ok(None)
    } else {
        Ok(Some(value))
    }
}

fn recovery_candidate_paths(cfg: &ProbeConfig) -> Vec<PathBuf> {
    let mut paths = Vec::new();

    if let Some(path) = cfg.recovery_key_path.clone() {
        paths.push(path);
    }
    if let Some(path) = cfg.recovery_passphrase_path.clone() {
        paths.push(path);
    }

    if paths.is_empty() {
        paths.push(cfg.key_passphrase_path.clone());

        if let Some(parent) = cfg.key_passphrase_path.parent() {
            let legacy_path = parent.join(".e2ee-export-passphrase");
            if legacy_path != cfg.key_passphrase_path {
                paths.push(legacy_path);
            }
        }
    }

    let mut seen = HashSet::new();
    paths
        .into_iter()
        .filter(|path| seen.insert(path.clone()))
        .collect()
}

fn env_var(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

fn env_flag(key: &str, default: bool) -> bool {
    match env::var(key) {
        Ok(value) => !matches!(value.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off"),
        Err(_) => default,
    }
}

fn send_debug_enabled() -> bool {
    env_flag("LIFERADAR_MATRIX_DEBUG_SEND", false)
}

fn send_debug(message: impl AsRef<str>) {
    if send_debug_enabled() {
        eprintln!("[matrix-send-debug] {}", message.as_ref());
    }
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
    use super::{
        bridge_targets_match, build_sync_attempts, persist_session_checkpoint, read_key_passphrase,
        read_optional_session_file, SessionFile,
    };
    use std::{
        fs,
        path::PathBuf,
        time::{SystemTime, UNIX_EPOCH},
    };

    fn temp_path(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("liferadar-{name}-{unique}.txt"))
    }

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

    #[test]
    fn passphrase_reader_trims_trailing_whitespace() {
        let path = temp_path("passphrase");
        fs::write(&path, "secret-passphrase\n").unwrap();

        let value = read_key_passphrase(&path).unwrap();
        assert_eq!(value, "secret-passphrase");

        let _ = fs::remove_file(path);
    }

    #[test]
    fn persist_session_checkpoint_updates_next_batch_without_dropping_tokens() {
        let path = temp_path("session");
        let original = SessionFile {
            access_token: "access-1".to_string(),
            refresh_token: Some("refresh-1".to_string()),
            user_id: "@user:example.com".to_string(),
            device_id: "DEVICE1".to_string(),
            homeserver: "https://example.com".to_string(),
            next_batch: Some("old-batch".to_string()),
            expires_at: Some("2026-04-17T12:00:00Z".to_string()),
            expires_in: Some(3600),
            saved_at: Some("2026-04-17T11:00:00Z".to_string()),
        };
        fs::write(&path, serde_json::to_string(&original).unwrap()).unwrap();

        persist_session_checkpoint(&path, "new-batch").unwrap();

        let updated = read_optional_session_file(&path).unwrap();
        assert_eq!(updated.access_token, "access-1");
        assert_eq!(updated.refresh_token.as_deref(), Some("refresh-1"));
        assert_eq!(updated.next_batch.as_deref(), Some("new-batch"));
        assert_eq!(updated.expires_in, Some(3600));
        assert!(updated.saved_at.is_some());

        let _ = fs::remove_file(path);
    }

    #[test]
    fn bridge_target_match_ignores_empty_state_keys() {
        let left = super::BridgeTarget {
            state_key: String::new(),
            protocol_id: Some("telegram".to_string()),
            channel_id: Some("user:8591267".to_string()),
            display_name: Some("Telegram Saved Messages".to_string()),
        };
        let unrelated = super::BridgeTarget {
            state_key: String::new(),
            protocol_id: Some("whatsapp".to_string()),
            channel_id: Some("491791389081@s.whatsapp.net".to_string()),
            display_name: Some("Martin Bayerl".to_string()),
        };

        assert!(!bridge_targets_match(&left, &unrelated));
    }
}
