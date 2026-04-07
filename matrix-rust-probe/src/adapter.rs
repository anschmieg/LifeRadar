use serde::Serialize;
use serde_json::{json, Value};

#[derive(Debug, Clone, Serialize)]
pub struct IngestEvent {
    pub external_id: String,
    pub sender_id: String,
    pub occurred_at: String,
    pub content_text: String,
    pub content_json: String,
    pub is_inbound: bool,
    pub provenance: String,
    pub classification: EventClassification,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EventClassification {
    DecryptedText,
    DecryptedNonText,
    PlainText,
    Undecrypted,
    UnsupportedCustom,
}

#[derive(Debug, Clone, Serialize)]
pub struct RoomInfo {
    pub title: String,
    pub title_source: String,
    pub participants_json: String,
    pub metadata_json: String,
}

pub fn parse_timeline_event(
    raw_json: Value,
    decrypted: bool,
    self_user_id: Option<&str>,
    import_source: &str,
) -> Option<IngestEvent> {
    let event_id = raw_json.get("event_id")?.as_str()?.to_string();
    let sender = raw_json
        .get("sender")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let ts_ms = raw_json.get("origin_server_ts")?.as_u64()?;
    let event_type = raw_json.get("type").and_then(Value::as_str).unwrap_or("");

    if !should_ingest_event(event_type) {
        return None;
    }

    let content = raw_json
        .get("content")
        .cloned()
        .unwrap_or(Value::Object(Default::default()));
    let msgtype = content
        .get("msgtype")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let body = content
        .get("body")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let provider_hints = provider_hints(event_type, &raw_json, &content);
    let classification = classify_event(event_type, decrypted, &msgtype);
    let content_text = render_body(event_type, &msgtype, &body, classification);

    Some(IngestEvent {
        external_id: event_id,
        sender_id: sender.clone(),
        occurred_at: iso_from_unix_ms(ts_ms),
        content_text,
        content_json: json!({
            "event_type": event_type,
            "msgtype": msgtype,
            "decrypted": decrypted,
            "classification": classification,
            "provider_hints": provider_hints,
            "raw_event": raw_json,
        })
        .to_string(),
        is_inbound: self_user_id.map(|id| id != sender).unwrap_or(true),
        provenance: json!({
            "import_source": import_source,
            "event_type": event_type,
            "provider_hints": provider_hints,
            "unsupported_custom": classification == EventClassification::UnsupportedCustom,
        })
        .to_string(),
        classification,
    })
}

pub fn build_room_info(
    title: String,
    title_source: &str,
    participants_json: String,
    room_topic: Option<&str>,
    event_counts: EventCountSummary,
) -> RoomInfo {
    RoomInfo {
        title,
        title_source: title_source.to_string(),
        participants_json,
        metadata_json: json!({
            "room_name_resolution": title_source,
            "topic": room_topic.filter(|value| !value.is_empty()),
            "event_counts": event_counts,
            "provider": "matrix",
            "provider_hints": ["beeper-matrix-compatible"],
        })
        .to_string(),
    }
}

#[derive(Debug, Clone, Copy, Serialize, Default)]
pub struct EventCountSummary {
    pub decrypted_text: usize,
    pub decrypted_non_text: usize,
    pub plain_text: usize,
    pub undecrypted: usize,
    pub unsupported_custom: usize,
}

impl EventCountSummary {
    pub fn record(&mut self, classification: EventClassification) {
        match classification {
            EventClassification::DecryptedText => self.decrypted_text += 1,
            EventClassification::DecryptedNonText => self.decrypted_non_text += 1,
            EventClassification::PlainText => self.plain_text += 1,
            EventClassification::Undecrypted => self.undecrypted += 1,
            EventClassification::UnsupportedCustom => self.unsupported_custom += 1,
        }
    }
}

fn should_ingest_event(event_type: &str) -> bool {
    matches!(event_type, "m.room.message" | "m.room.encrypted")
        || event_type.contains("beeper")
        || (!event_type.starts_with("m.room.") && !event_type.starts_with("org.matrix"))
}

fn classify_event(event_type: &str, decrypted: bool, msgtype: &str) -> EventClassification {
    match (event_type, decrypted, msgtype) {
        ("m.room.message", true, "m.text" | "m.notice" | "m.emote") => {
            EventClassification::DecryptedText
        }
        ("m.room.message", true, _) => EventClassification::DecryptedNonText,
        ("m.room.message", false, _) => EventClassification::PlainText,
        ("m.room.encrypted", _, _) => EventClassification::Undecrypted,
        _ => EventClassification::UnsupportedCustom,
    }
}

fn render_body(
    event_type: &str,
    msgtype: &str,
    body: &str,
    classification: EventClassification,
) -> String {
    if !body.is_empty() {
        return body.to_string();
    }

    match classification {
        EventClassification::Undecrypted => "[undecrypted]".to_string(),
        EventClassification::UnsupportedCustom => format!("[custom event: {event_type}]"),
        EventClassification::DecryptedNonText | EventClassification::PlainText => {
            if msgtype.is_empty() {
                "[non-text message]".to_string()
            } else {
                format!("[non-text message: {msgtype}]")
            }
        }
        EventClassification::DecryptedText => "[message]".to_string(),
    }
}

fn provider_hints(event_type: &str, raw_json: &Value, content: &Value) -> Vec<String> {
    let mut hints = Vec::new();
    if event_type.contains("beeper") {
        hints.push("beeper-event".to_string());
    }
    for value in [raw_json, content] {
        if let Some(object) = value.as_object() {
            for (key, inner) in object {
                let lower = key.to_ascii_lowercase();
                if lower.contains("beeper") || lower.contains("bridge") || lower.contains("network")
                {
                    hints.push(format!("field:{key}"));
                }
                if inner
                    .as_str()
                    .map(|text| text.to_ascii_lowercase().contains("beeper"))
                    .unwrap_or(false)
                {
                    hints.push(format!("value:{key}"));
                }
            }
        }
    }
    hints.sort();
    hints.dedup();
    hints
}

fn iso_from_unix_ms(ms: u64) -> String {
    let secs = ms / 1000;
    let millis = ms % 1000;
    format!("{secs}.{millis:03}Z")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_standard_message_event() {
        let event = json!({
            "event_id": "$one",
            "sender": "@alice:example.com",
            "origin_server_ts": 1710000000123u64,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "Hello"}
        });

        let parsed = parse_timeline_event(event, true, Some("@me:example.com"), "matrix_rust_live")
            .expect("event should parse");

        assert_eq!(parsed.content_text, "Hello");
        assert_eq!(parsed.classification, EventClassification::DecryptedText);
        assert!(parsed.is_inbound);
    }

    #[test]
    fn parses_undecrypted_event() {
        let event = json!({
            "event_id": "$two",
            "sender": "@alice:example.com",
            "origin_server_ts": 1710000000123u64,
            "type": "m.room.encrypted",
            "content": {"algorithm": "m.megolm.v1.aes-sha2"}
        });

        let parsed =
            parse_timeline_event(event, false, Some("@me:example.com"), "matrix_rust_live")
                .expect("event should parse");

        assert_eq!(parsed.content_text, "[undecrypted]");
        assert_eq!(parsed.classification, EventClassification::Undecrypted);
    }

    #[test]
    fn preserves_beeper_custom_event() {
        let event = json!({
            "event_id": "$three",
            "sender": "@bridge:example.com",
            "origin_server_ts": 1710000000123u64,
            "type": "com.beeper.message_send_status",
            "content": {"beeper_status": "sent"}
        });

        let parsed =
            parse_timeline_event(event, false, Some("@me:example.com"), "matrix_rust_live")
                .expect("event should parse");

        assert_eq!(
            parsed.classification,
            EventClassification::UnsupportedCustom
        );
        assert!(parsed.content_text.contains("custom event"));
        assert!(parsed.content_json.contains("beeper"));
    }
}
