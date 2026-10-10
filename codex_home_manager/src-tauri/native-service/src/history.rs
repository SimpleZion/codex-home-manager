use std::{
    collections::{HashMap, VecDeque},
    fs::File,
    path::{Path, PathBuf},
};

use axum::{
    extract::{Path as AxumPath, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use chrono::DateTime;
use memchr::{memchr, memrchr};
use memmap2::MmapOptions;
use serde::Deserialize;
use serde_json::{json, Map, Value};

use crate::{catalog::ThreadLocator, prompts::classify_prompt, AppState};

const MAX_RECORD_BYTES: usize = 64 * 1024 * 1024;
const DEFAULT_SCAN_RECORDS: usize = 20_000;
const DEFAULT_SCAN_BYTES: usize = 64 * 1024 * 1024;

#[derive(Debug, Deserialize)]
pub struct TimelineQuery {
    codex_home: Option<String>,
    before_byte: Option<usize>,
    kind: Option<String>,
    #[serde(rename = "sourceFilter", alias = "source_filter")]
    source_filter: Option<String>,
    search: Option<String>,
    limit: Option<usize>,
    content_limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
pub struct TimelineItemQuery {
    codex_home: Option<String>,
    byte_offset: usize,
    content_limit: Option<usize>,
}

pub async fn thread_timeline(
    State(state): State<AppState>,
    AxumPath(thread_id): AxumPath<String>,
    Query(query): Query<TimelineQuery>,
) -> Response {
    let store = state.catalog.clone();
    let result = tokio::task::spawn_blocking(move || {
        let locator = store.thread_locator(query.codex_home.as_deref(), &thread_id)?;
        read_timeline(&locator, &query)
    })
    .await;
    history_response(result, "timeline worker")
}

pub async fn thread_timeline_item(
    State(state): State<AppState>,
    AxumPath(thread_id): AxumPath<String>,
    Query(query): Query<TimelineItemQuery>,
) -> Response {
    let store = state.catalog.clone();
    let result = tokio::task::spawn_blocking(move || {
        let locator = store.thread_locator(query.codex_home.as_deref(), &thread_id)?;
        read_timeline_item(
            &locator,
            query.byte_offset,
            query.content_limit.unwrap_or(2_000_000),
        )
    })
    .await;
    history_response(result, "timeline item worker")
}

fn history_response(
    result: Result<Result<Value, String>, tokio::task::JoinError>,
    worker: &str,
) -> Response {
    match result {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => {
            let status = if error.starts_with("thread not found") || error.contains("missing") {
                StatusCode::NOT_FOUND
            } else {
                StatusCode::BAD_REQUEST
            };
            (status, Json(json!({"detail": error}))).into_response()
        }
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"detail": format!("{worker} failed: {error}")})),
        )
            .into_response(),
    }
}

fn map_file(path: &Path) -> Result<memmap2::Mmap, String> {
    let file =
        File::open(path).map_err(|error| format!("cannot open {}: {error}", path.display()))?;
    // The mapping is read-only and its length is fixed at creation, so a growing active rollout
    // remains a stable snapshot for this request without allocating its complete contents.
    unsafe { MmapOptions::new().map(&file) }
        .map_err(|error| format!("cannot map {}: {error}", path.display()))
}

#[derive(Clone, Debug)]
struct RolloutSegment {
    path: PathBuf,
    base: usize,
    size: usize,
}

fn rollout_segments(locator: &ThreadLocator) -> Result<Vec<RolloutSegment>, String> {
    let paths = if locator.rollout_paths.is_empty() {
        vec![locator.rollout_path.clone()]
    } else {
        locator.rollout_paths.clone()
    };
    let mut base = 0usize;
    let mut segments = Vec::with_capacity(paths.len());
    for path in paths {
        let size = std::fs::metadata(&path)
            .map_err(|error| format!("cannot stat {}: {error}", path.display()))?
            .len() as usize;
        segments.push(RolloutSegment { path, base, size });
        base = base
            .checked_add(size)
            .ok_or_else(|| "combined rollout size exceeds addressable range".to_string())?;
    }
    Ok(segments)
}

fn set_global_offset(event: &mut TimelineEvent, base: usize) {
    event.byte_offset += base;
    event.id = format!("byte-{}", event.byte_offset);
}

fn read_timeline(locator: &ThreadLocator, query: &TimelineQuery) -> Result<Value, String> {
    let kind = query
        .kind
        .as_deref()
        .unwrap_or("conversation")
        .trim()
        .to_lowercase();
    if !matches!(
        kind.as_str(),
        "conversation"
            | "user"
            | "commentary"
            | "assistant"
            | "assistant_unclassified"
            | "reasoning"
            | "tool_call"
            | "tool_output"
            | "developer"
            | "system"
            | "context"
            | "status"
            | "tool"
            | "all"
    ) {
        return Err(format!("unsupported timeline kind: {kind}"));
    }
    let search = query.search.as_deref().unwrap_or("").trim().to_string();
    let normalized_search = search.to_lowercase();
    let source_filter = query
        .source_filter
        .as_deref()
        .unwrap_or("all")
        .trim()
        .to_lowercase();
    if !matches!(
        source_filter.as_str(),
        "all" | "pure" | "focused" | "with_agents" | "automation" | "delegation"
    ) {
        return Err(format!(
            "unsupported timeline source filter: {source_filter}"
        ));
    }
    let limit = query.limit.unwrap_or(80).clamp(1, 200);
    let content_limit = query.content_limit.unwrap_or(120_000).clamp(2_000, 500_000);
    let segments = rollout_segments(locator)?;
    let rollout_paths = segments
        .iter()
        .map(|segment| segment.path.to_string_lossy().to_string())
        .collect::<Vec<_>>();
    let file_size = segments
        .last()
        .map_or(0, |segment| segment.base + segment.size);
    if file_size == 0 {
        return Ok(json!({
            "threadId": locator.id,
            "title": locator.title,
            "rolloutPath": locator.rollout_path.to_string_lossy(),
            "rolloutPaths": rollout_paths,
            "segmentCount": segments.len(),
            "fileSize": 0,
            "beforeByte": query.before_byte,
            "nextBeforeByte": Value::Null,
            "limit": limit,
            "kind": kind,
            "sourceFilter": source_filter,
            "search": search,
            "hasMore": false,
            "scannedRecords": 0,
            "scannedBytes": 0,
            "scanLimited": false,
            "skippedOversizedRecords": 0,
            "recoveredOversizedRecords": 0,
            "items": [],
            "pageCounts": {},
            "runtime": "rust",
        }));
    }
    let requested_before = query.before_byte.map(|value| value.min(file_size));
    let global_origin = requested_before.unwrap_or(file_size);
    let mut events = Vec::with_capacity(limit);
    let mut seen: HashMap<String, VecDeque<(usize, String, String, i64)>> = HashMap::new();
    let mut scanned_records = 0usize;
    let mut scanned_bytes = 0usize;
    let mut skipped_oversized = 0usize;
    let mut next_before = None;
    let mut has_more = false;
    let mut scan_limited = false;

    'segments: for segment in segments.iter().rev() {
        if global_origin <= segment.base || segment.size == 0 {
            continue;
        }
        let mapped = map_file(&segment.path)?;
        let mut origin = global_origin.saturating_sub(segment.base).min(mapped.len());
        if origin > 0 && mapped[origin - 1] == b'\n' {
            origin -= 1;
        }
        let mut line_end = origin;
        while line_end > 0 {
            let line_start = memrchr(b'\n', &mapped[..line_end]).map_or(0, |index| index + 1);
            let raw = &mapped[line_start..line_end];
            let mut previous_end = line_start.saturating_sub(1);
            scanned_records += 1;
            scanned_bytes = scanned_bytes.saturating_add(line_end.saturating_sub(line_start));
            if scanned_records > DEFAULT_SCAN_RECORDS
                || (scanned_records > 1 && scanned_bytes > DEFAULT_SCAN_BYTES)
            {
                has_more = true;
                scan_limited = true;
                next_before = Some(segment.base + line_end);
                break 'segments;
            }
            if raw.len() > MAX_RECORD_BYTES {
                skipped_oversized += 1;
                line_end = previous_end;
                continue;
            }
            if !raw.is_empty() {
                if let Ok(item) = serde_json::from_slice::<Value>(raw) {
                    if let Some(mut event) = event_from_item(&item, line_start) {
                        if let Some((canonical_event, next_line_end)) =
                            canonical_agent_message_mirror(&mapped, line_start, &event)
                        {
                            event = canonical_event;
                            previous_end = next_line_end;
                            scanned_records += 1;
                        }
                        set_global_offset(&mut event, segment.base);
                        if event_matches(&event, &kind, &normalized_search, &source_filter)
                            && (!event.text.is_empty() || event.kind == "reasoning")
                            && !is_duplicate(&event, &mut seen)
                        {
                            apply_source_filter(&mut event, &source_filter);
                            truncate_event(&mut event, content_limit);
                            if events.len() >= limit {
                                has_more = true;
                                next_before = Some(segment.base + line_end);
                                break 'segments;
                            }
                            events.push(event);
                        }
                    }
                }
            }
            line_end = previous_end;
        }
    }

    if !events.is_empty() && next_before.is_none() {
        next_before = events.last().map(|event| event.byte_offset);
    }
    events.reverse();
    let mut page_counts = Map::new();
    for event in &events {
        let current = page_counts
            .get(&event.kind)
            .and_then(Value::as_u64)
            .unwrap_or(0);
        page_counts.insert(event.kind.clone(), json!(current + 1));
    }
    Ok(json!({
        "threadId": locator.id,
        "title": locator.title,
        "rolloutPath": locator.rollout_path.to_string_lossy(),
        "rolloutPaths": rollout_paths,
        "segmentCount": segments.len(),
        "fileSize": file_size,
        "beforeByte": requested_before,
        "nextBeforeByte": if has_more { next_before } else { None },
        "limit": limit,
        "kind": kind,
        "sourceFilter": source_filter,
        "search": search,
        "hasMore": has_more,
        "scannedRecords": scanned_records,
        "scannedBytes": scanned_bytes,
        "scanLimited": scan_limited,
        "skippedOversizedRecords": skipped_oversized,
        "recoveredOversizedRecords": 0,
        "items": events.iter().map(TimelineEvent::to_json).collect::<Vec<_>>(),
        "pageCounts": page_counts,
        "runtime": "rust",
    }))
}

fn read_timeline_item(
    locator: &ThreadLocator,
    byte_offset: usize,
    content_limit: usize,
) -> Result<Value, String> {
    let segments = rollout_segments(locator)?;
    let Some(segment) = segments
        .iter()
        .find(|segment| byte_offset >= segment.base && byte_offset < segment.base + segment.size)
    else {
        return Err("timeline byte offset is outside the rollout file".to_string());
    };
    let mapped = map_file(&segment.path)?;
    let local_offset = byte_offset - segment.base;
    let tail = &mapped[local_offset..];
    let line_length = memchr(b'\n', tail).unwrap_or(tail.len());
    if line_length > MAX_RECORD_BYTES {
        return Err("timeline JSONL record exceeds the 64 MB safety limit".to_string());
    }
    let item: Value = serde_json::from_slice(&tail[..line_length])
        .map_err(|_| "timeline byte offset does not point to a valid JSONL record".to_string())?;
    let mut event = event_from_item(&item, local_offset)
        .ok_or_else(|| "JSONL record is not a displayable timeline event".to_string())?;
    set_global_offset(&mut event, segment.base);
    truncate_event(&mut event, content_limit.clamp(2_000, 5_000_000));
    Ok(event.to_json())
}

#[derive(Clone, Debug)]
pub(crate) struct TimelineEvent {
    pub(crate) id: String,
    pub(crate) byte_offset: usize,
    pub(crate) timestamp: Value,
    pub(crate) timestamp_ms: i64,
    pub(crate) kind: String,
    pub(crate) label: String,
    pub(crate) text: String,
    pub(crate) source_type: String,
    pub(crate) payload_type: String,
    pub(crate) phase: String,
    pub(crate) call_id: String,
    pub(crate) readable: bool,
    pub(crate) encrypted: bool,
    pub(crate) has_encrypted_content: bool,
    pub(crate) prompt_source_type: String,
    pub(crate) pure_text: String,
    pub(crate) pure_character_count: usize,
    pub(crate) has_pure_text: bool,
    pub(crate) character_count: usize,
    pub(crate) text_truncated: bool,
}

impl TimelineEvent {
    fn to_json(&self) -> Value {
        json!({
            "id": self.id,
            "byteOffset": self.byte_offset,
            "timestamp": self.timestamp,
            "timestampMs": self.timestamp_ms,
            "kind": self.kind,
            "label": self.label,
            "text": self.text,
            "sourceType": self.source_type,
            "payloadType": self.payload_type,
            "phase": self.phase,
            "callId": self.call_id,
            "readable": self.readable,
            "encrypted": self.encrypted,
            "hasEncryptedContent": self.has_encrypted_content,
            "promptSourceType": self.prompt_source_type,
            "pureText": self.pure_text,
            "pureCharacterCount": self.pure_character_count,
            "hasPureText": self.has_pure_text,
            "characterCount": self.character_count,
            "textTruncated": self.text_truncated,
        })
    }
}

pub(crate) fn event_from_item(item: &Value, byte_offset: usize) -> Option<TimelineEvent> {
    let item_type = item.get("type")?.as_str().unwrap_or_default();
    let raw_payload = item.get("payload").unwrap_or(&Value::Null);
    let payload = raw_payload.as_object();
    let payload_type = payload
        .and_then(|value| value.get("type"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let role = payload
        .and_then(|value| value.get("role"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let phase = payload
        .and_then(|value| value.get("phase"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let timestamp = item.get("timestamp").cloned().unwrap_or(Value::Null);
    let mut event = if let Some(text) = crate::prompts::incoming_delegation_text(item) {
        user_event(&Value::String(text.to_string()))
    } else {
        match item_type {
            "user_message" => user_event(raw_payload),
            "turn_context" => Some(base_event(
                "context",
                "轮次上下文",
                text_from_value(raw_payload),
            )),
            "compacted" => Some(base_event(
                "context",
                "上下文压缩",
                text_from_value(payload?.get("message").unwrap_or(&Value::Null)),
            )),
            "event_msg" => event_message(payload_type, payload?),
            "response_item" => response_item(payload_type, role, phase, payload?),
            _ => None,
        }
    }?;
    event.id = format!("byte-{byte_offset}");
    event.byte_offset = byte_offset;
    event.timestamp = timestamp;
    event.timestamp_ms = timestamp_to_epoch_ms(&event.timestamp);
    event.source_type = item_type.to_string();
    event.payload_type = payload_type.to_string();
    if event.kind != "user" && event.kind != "context" {
        event.pure_text = event.text.clone();
        event.pure_character_count = event.text.chars().count();
        event.has_pure_text = !event.text.is_empty();
    }
    Some(event)
}

pub(crate) fn assistant_stage(phase: &str) -> (&'static str, &'static str) {
    match phase {
        "commentary" => ("commentary", "进度说明"),
        "final" | "final_answer" => ("assistant", "最终回复"),
        _ => ("assistant_unclassified", "助手消息（阶段未标注）"),
    }
}

fn event_message(payload_type: &str, payload: &Map<String, Value>) -> Option<TimelineEvent> {
    match payload_type {
        "user_message" => user_event(payload.get("message").unwrap_or(&Value::Null)),
        "agent_message" => {
            let phase = payload
                .get("phase")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let (kind, label) = assistant_stage(phase);
            let mut event = base_event(
                kind,
                label,
                text_from_value(payload.get("message").unwrap_or(&Value::Null)),
            );
            event.phase = phase.to_string();
            Some(event)
        }
        "agent_reasoning" => Some(base_event(
            "reasoning",
            "推理记录",
            text_from_value(payload.get("text").unwrap_or(&Value::Null)),
        )),
        "task_started"
        | "task_complete"
        | "turn_aborted"
        | "context_compacted"
        | "thread_settings_applied" => Some(base_event(
            "status",
            payload_type,
            text_from_value(&Value::Object(payload.clone())),
        )),
        value
            if value.contains("tool_call")
                || value.contains("exec_command")
                || value.contains("command_execution")
                || value.contains("patch_apply") =>
        {
            let output = value.ends_with("_end")
                || value.ends_with("_output")
                || value.ends_with("_complete")
                || value.ends_with("_completed");
            Some(base_event(
                if output { "tool_output" } else { "tool_call" },
                payload
                    .get("name")
                    .and_then(Value::as_str)
                    .or_else(|| payload.get("call_id").and_then(Value::as_str))
                    .unwrap_or(value),
                text_from_value(&Value::Object(payload.clone())),
            ))
        }
        _ => None,
    }
}

fn response_item(
    payload_type: &str,
    role: &str,
    phase: &str,
    payload: &Map<String, Value>,
) -> Option<TimelineEvent> {
    match payload_type {
        "message" if matches!(role, "user" | "assistant" | "developer" | "system") => {
            let text = text_from_value(payload.get("content").unwrap_or(&Value::Null));
            if role == "user" {
                return user_event(&Value::String(text));
            }
            let (kind, label) = if role == "assistant" {
                assistant_stage(phase)
            } else if role == "developer" {
                ("developer", "开发者上下文")
            } else {
                ("system", "系统上下文")
            };
            let mut event = base_event(kind, label, text);
            event.phase = phase.to_string();
            Some(event)
        }
        "reasoning" => {
            let summary = text_from_value(payload.get("summary").unwrap_or(&Value::Null));
            let text = if summary.is_empty() {
                text_from_value(payload.get("content").unwrap_or(&Value::Null))
            } else { summary };
            let encrypted = payload
                .get("encrypted_content")
                .is_some_and(|value| !value.is_null());
            let mut event = base_event("reasoning", "推理记录", text);
            event.readable = !event.text.is_empty();
            event.encrypted = encrypted && event.text.is_empty();
            event.has_encrypted_content = encrypted;
            Some(event)
        }
        "function_call" | "custom_tool_call" | "tool_search_call" => {
            let text = text_from_value(
                payload
                    .get("arguments")
                    .or_else(|| payload.get("input"))
                    .or_else(|| payload.get("query"))
                    .unwrap_or(&Value::Null),
            );
            let mut event = base_event(
                "tool_call",
                payload
                    .get("name")
                    .and_then(Value::as_str)
                    .or_else(|| payload.get("call_id").and_then(Value::as_str))
                    .unwrap_or("工具调用"),
                if text.is_empty() {
                    "[无参数]".to_string()
                } else {
                    text
                },
            );
            event.call_id = payload
                .get("call_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            Some(event)
        }
        "function_call_output" | "custom_tool_call_output" | "tool_search_output" => {
            let text = text_from_value(
                payload
                    .get("output")
                    .or_else(|| payload.get("content"))
                    .or_else(|| payload.get("tools"))
                    .unwrap_or(&Value::Null),
            );
            let mut event = base_event(
                "tool_output",
                payload
                    .get("call_id")
                    .and_then(Value::as_str)
                    .unwrap_or("工具结果"),
                if text.is_empty() {
                    "[无文本结果]".to_string()
                } else {
                    text
                },
            );
            event.call_id = payload
                .get("call_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            Some(event)
        }
        _ => None,
    }
}

fn user_event(value: &Value) -> Option<TimelineEvent> {
    let text = text_from_value(value);
    let classification = classify_prompt(&text);
    let mut event = base_event(if classification.visible_by_default { "user" } else { "context" }, &classification.source_label, text);
    event.prompt_source_type = classification.source_type;
    event.pure_text = classification.pure_text;
    event.pure_character_count = event.pure_text.chars().count();
    event.has_pure_text = !event.pure_text.is_empty();
    Some(event)
}

fn canonical_agent_message_mirror(
    mapped: &[u8],
    line_start: usize,
    event: &TimelineEvent,
) -> Option<(TimelineEvent, usize)> {
    if event.source_type != "event_msg"
        || event.payload_type != "agent_message"
        || !event.phase.is_empty()
        || line_start == 0
    {
        return None;
    }
    let previous_line_end = line_start - 1;
    let previous_line_start =
        memrchr(b'\n', &mapped[..previous_line_end]).map_or(0, |index| index + 1);
    let previous_item =
        serde_json::from_slice::<Value>(&mapped[previous_line_start..previous_line_end]).ok()?;
    let canonical = event_from_item(&previous_item, previous_line_start)?;
    if canonical.source_type != "response_item"
        || canonical.payload_type != "message"
        || !matches!(canonical.kind.as_str(), "commentary" | "assistant" | "assistant_unclassified")
        || canonical.text != event.text
    {
        return None;
    }
    Some((canonical, previous_line_start.saturating_sub(1)))
}

fn base_event(kind: &str, label: &str, text: String) -> TimelineEvent {
    TimelineEvent {
        id: String::new(),
        byte_offset: 0,
        timestamp: Value::Null,
        timestamp_ms: 0,
        kind: kind.to_string(),
        label: label.to_string(),
        text,
        source_type: String::new(),
        payload_type: String::new(),
        phase: String::new(),
        call_id: String::new(),
        readable: true,
        encrypted: false,
        has_encrypted_content: false,
        prompt_source_type: String::new(),
        pure_text: String::new(),
        pure_character_count: 0,
        has_pure_text: false,
        character_count: 0,
        text_truncated: false,
    }
}

pub(crate) fn text_from_value(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(text) => redact_data_urls(text).trim().to_string(),
        Value::Array(items) => items
            .iter()
            .map(text_from_value)
            .filter(|text| !text.is_empty())
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Object(map) => {
            if let Some(placeholder) = attachment_placeholder(map) {
                return placeholder;
            }
            if matches!(map.get("type").and_then(Value::as_str), Some("summary_text" | "reasoning_text")) {
                return text_from_value(map.get("text").unwrap_or(&Value::Null));
            }
            for key in [
                "text",
                "message",
                "output",
                "content",
                "summary",
                "arguments",
                "input",
                "result",
            ] {
                if let Some(value) = map.get(key) {
                    let text = text_from_value(value);
                    if !text.is_empty() {
                        return text;
                    }
                }
            }
            serde_json::to_string_pretty(&safe_json(value)).unwrap_or_default()
        }
        _ => value.to_string(),
    }
}

fn attachment_placeholder(map: &Map<String, Value>) -> Option<String> {
    let kind = map
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_lowercase();
    if kind.contains("image") || map.contains_key("image_url") {
        Some("[图片附件]".to_string())
    } else if kind.contains("audio") || map.contains_key("audio_url") {
        Some("[音频附件]".to_string())
    } else if kind.contains("file") || map.contains_key("file_url") {
        let name = map
            .get("filename")
            .or_else(|| map.get("name"))
            .and_then(Value::as_str)
            .unwrap_or_default();
        Some(if name.is_empty() {
            "[文件附件]".to_string()
        } else {
            format!("[文件附件：{name}]")
        })
    } else {
        None
    }
}

fn safe_json(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            if let Some(placeholder) = attachment_placeholder(map) {
                return Value::String(placeholder);
            }
            Value::Object(
                map.iter()
                    .map(|(key, value)| {
                        let safe = if matches!(
                            key.to_lowercase().as_str(),
                            "image_url" | "audio_url" | "video_url" | "file_url"
                        ) {
                            Value::String("[附件内容已隐藏]".to_string())
                        } else {
                            safe_json(value)
                        };
                        (key.clone(), safe)
                    })
                    .collect(),
            )
        }
        Value::Array(items) => Value::Array(items.iter().map(safe_json).collect()),
        Value::String(text) => Value::String(redact_data_urls(text)),
        _ => value.clone(),
    }
}

fn redact_data_urls(text: &str) -> String {
    let lower = text.to_ascii_lowercase();
    if !lower.contains("data:") {
        return text.to_string();
    }
    let mut result = String::with_capacity(text.len().min(4096));
    let bytes = text.as_bytes();
    let mut cursor = 0usize;
    while let Some(relative_start) = lower[cursor..].find("data:") {
        let start = cursor + relative_start;
        result.push_str(&text[cursor..start]);
        result.push_str("[附件内容已隐藏]");
        let mut end = start + 5;
        while end < bytes.len()
            && !bytes[end].is_ascii_whitespace()
            && !matches!(bytes[end], b'"' | b'\'' | b'<' | b'>')
        {
            end += 1;
        }
        cursor = end;
    }
    result.push_str(&text[cursor..]);
    result
}

fn event_matches(event: &TimelineEvent, kind: &str, search: &str, source_filter: &str) -> bool {
    let source_matches = match source_filter {
        "all" => true,
        "automation" | "delegation" => event.prompt_source_type == source_filter,
        "pure" | "focused" if matches!(event.kind.as_str(), "user" | "context") => {
            matches!(
                event.prompt_source_type.as_str(),
                "user" | "attachment" | "browser"
            ) && event.has_pure_text
        }
        "with_agents" if matches!(event.kind.as_str(), "user" | "context") => matches!(
            event.prompt_source_type.as_str(),
            "user" | "attachment" | "browser" | "subagent"
        ),
        _ => true,
    };
    if !source_matches {
        return false;
    }
    let kind_matches = match kind {
        "all" => true,
        "conversation" => {
            matches!(event.kind.as_str(), "user" | "commentary" | "assistant" | "assistant_unclassified")
                || (event.kind == "context" && !event.prompt_source_type.is_empty())
        }
        "user" => {
            matches!(event.prompt_source_type.as_str(), "user" | "attachment" | "browser" | "subagent" | "automation" | "delegation" | "goal")
        }
        "tool" => matches!(event.kind.as_str(), "tool_call" | "tool_output"),
        other => event.kind == other,
    };
    let searchable_text = if matches!(source_filter, "pure" | "focused")
        && matches!(event.kind.as_str(), "user" | "context")
    {
        &event.pure_text
    } else {
        &event.text
    };
    kind_matches && (search.is_empty() || searchable_text.to_lowercase().contains(search))
}

fn apply_source_filter(event: &mut TimelineEvent, source_filter: &str) {
    if matches!(source_filter, "pure" | "focused") && event.kind == "user" {
        event.text = event.pure_text.clone();
        event.character_count = event.pure_character_count;
        event.text_truncated = false;
    }
}

fn is_duplicate(
    event: &TimelineEvent,
    seen: &mut HashMap<String, VecDeque<(usize, String, String, i64)>>,
) -> bool {
    let records = seen.entry(event.kind.clone()).or_default();
    let text = event.text.trim().to_string();
    let duplicate = records
        .iter()
        .any(|(offset, prior_text, source, prior_timestamp_ms)| {
            let near_in_time = event.timestamp_ms > 0
                && *prior_timestamp_ms > 0
                && event.timestamp_ms.abs_diff(*prior_timestamp_ms) <= 2_000;
            let near_in_file = event.timestamp_ms == 0
                && *prior_timestamp_ms == 0
                && event.byte_offset.abs_diff(*offset) < 1_000_000;
            source != &event.source_type
                && (near_in_time || near_in_file)
                && (text == *prior_text || (!text.is_empty() && prior_text.starts_with(&text)))
        });
    if !duplicate {
        records.push_back((
            event.byte_offset,
            text,
            event.source_type.clone(),
            event.timestamp_ms,
        ));
        while records.len() > 256 {
            records.pop_front();
        }
    }
    duplicate
}

fn timestamp_to_epoch_ms(value: &Value) -> i64 {
    if let Some(milliseconds) = value.as_i64() {
        return milliseconds;
    }
    value
        .as_str()
        .and_then(|text| DateTime::parse_from_rfc3339(text).ok())
        .map_or(0, |timestamp| timestamp.timestamp_millis())
}

fn truncate_event(event: &mut TimelineEvent, content_limit: usize) {
    event.character_count = event.text.chars().count();
    if event.character_count > content_limit {
        event.text = event.text.chars().take(content_limit).collect();
        event.text_truncated = true;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;

    fn assert_user_classification(text: &str, expected: (&str, &str, bool)) {
        let value = classify_prompt(text);
        assert_eq!((value.source_type.as_str(), value.source_label.as_str(), value.visible_by_default), expected);
    }

    #[test]
    fn shared_timeline_classification_contract() {
        let fixtures: Vec<Value> = serde_json::from_str(include_str!("../../../tests/fixtures/timeline_classification_contract.json")).unwrap();
        for fixture in fixtures {
            let event = event_from_item(&fixture["record"], 0).unwrap().to_json();
            for (key, expected) in fixture["expected"].as_object().unwrap() {
                assert_eq!(&event[key], expected, "{}: {}", fixture["name"], key);
            }
        }
    }

    fn fixture_locator(lines: &[Value]) -> (NamedTempFile, ThreadLocator) {
        let mut file = NamedTempFile::new().expect("fixture");
        for line in lines {
            writeln!(file, "{}", serde_json::to_string(line).unwrap()).unwrap();
        }
        let locator = ThreadLocator {
            id: "thread-1".to_string(),
            title: "Fixture".to_string(),
            rollout_path: file.path().to_path_buf(),
            rollout_paths: vec![file.path().to_path_buf()],
        };
        (file, locator)
    }

    #[test]
    fn official_incoming_delegation_is_visible_in_source_filter() {
        let record = json!({"type":"response_item","payload":{"type":"function_call_output","namespace":"codex_app","name":"send_message_to_thread","output":"<codex_delegation>\n<input>handover</input>\n</codex_delegation>"}});
        let mirror = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"FunctionCallOutput","namespace":"codex_app","name":"send_message_to_thread","output":record["payload"]["output"]}}});
        let (_file, locator) = fixture_locator(&[record.clone(), mirror, record]);
        let query = TimelineQuery {
            codex_home: None,
            before_byte: None,
            kind: Some("conversation".into()),
            source_filter: Some("delegation".into()),
            search: None,
            limit: Some(80),
            content_limit: None,
        };
        let page = read_timeline(&locator, &query).unwrap();
        assert_eq!(page["items"].as_array().unwrap().len(), 2);
        assert_eq!(page["items"][0]["promptSourceType"], "delegation");
        assert_eq!(page["items"][0]["kind"], "context");
        for kind in ["conversation", "user", "all"] {
            for source in [
                "all",
                "delegation",
                "focused",
                "pure",
                "with_agents",
                "automation",
            ] {
                let query = TimelineQuery {
                    codex_home: None,
                    before_byte: None,
                    kind: Some(kind.into()),
                    source_filter: Some(source.into()),
                    search: Some("handover".into()),
                    limit: Some(80),
                    content_limit: None,
                };
                let page = read_timeline(&locator, &query).unwrap();
                assert_eq!(
                    page["items"].as_array().unwrap().len(),
                    if matches!(source, "all" | "delegation") {
                        2
                    } else {
                        0
                    },
                    "{kind} + {source}"
                );
            }
        }
    }

    #[test]
    fn native_timeline_reads_latest_conversation_without_tools() {
        let (_file, locator) = fixture_locator(&[
            json!({"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}}),
            json!({"timestamp":"2026-01-01T00:00:01Z","type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"secret"}}),
            json!({"timestamp":"2026-01-01T00:00:02Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"commentary","content":[{"type":"output_text","text":"working"}]}}),
            json!({"timestamp":"2026-01-01T00:00:03Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"done"}]}}),
        ]);
        let payload = read_timeline(
            &locator,
            &TimelineQuery {
                codex_home: None,
                before_byte: None,
                kind: Some("conversation".to_string()),
                source_filter: None,
                search: None,
                limit: Some(80),
                content_limit: Some(120_000),
            },
        )
        .unwrap();
        let items = payload["items"].as_array().unwrap();
        assert_eq!(items.len(), 3);
        assert_eq!(items[0]["text"], "hello");
        assert_eq!(items[1]["kind"], "commentary");
        assert_eq!(items[2]["kind"], "assistant");
        assert_eq!(payload["runtime"], "rust");
    }

    #[test]
    fn native_timeline_accepts_an_empty_rollout() {
        let (_file, locator) = fixture_locator(&[]);
        let payload = read_timeline(
            &locator,
            &TimelineQuery {
                codex_home: None,
                before_byte: None,
                kind: Some("conversation".to_string()),
                source_filter: None,
                search: None,
                limit: Some(80),
                content_limit: Some(120_000),
            },
        )
        .unwrap();

        assert_eq!(payload["fileSize"], 0);
        assert_eq!(payload["hasMore"], false);
        assert_eq!(payload["items"], json!([]));
        assert_eq!(payload["runtime"], "rust");
    }

    #[test]
    fn native_timeline_redacts_embedded_data_urls() {
        assert_eq!(
            redact_data_urls(
                "before data:image/png;base64,AAAA and data:audio/wav;base64,BBBB after"
            ),
            "before [附件内容已隐藏] and [附件内容已隐藏] after"
        );
    }

    #[test]
    fn subagent_notifications_stay_out_of_the_default_conversation() {
        assert_user_classification("<subagent_notification>{}</subagent_notification>", ("subagent", "子 agent", false));
    }

    #[test]
    fn runtime_context_markers_are_not_classified_as_user_input() {
        assert_user_classification("<recommended_plugins>\n- Sentry\n</recommended_plugins>", ("internal", "推荐插件上下文", false));
        assert_user_classification("# AGENTS.md instructions for C:/workspace", ("internal", "内部上下文", false));
        assert_user_classification("请解释正文中的 <recommended_plugins> 标签", ("user", "用户输入", true));
    }

    #[test]
    fn automation_markers_are_classified_as_automation_inputs() {
        for text in [
            "<heartbeat>check now</heartbeat>",
            "<automation>daily review</automation>",
            "<scheduled_task>weekly review</scheduled_task>",
            "prefix <automation_id>task-1</automation_id>",
            "<current_time_iso>2026-08-17T00:00:00Z</current_time_iso><instructions>run</instructions>",
        ] {
            assert_user_classification(text, ("automation", "自动化任务", false));
        }

        let (_file, locator) = fixture_locator(&[
            json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<heartbeat><instructions>巡检插件</instructions></heartbeat>"}]}}),
            json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"普通用户输入"}]}}),
        ]);
        let payload = read_timeline(
            &locator,
            &TimelineQuery {
                codex_home: None,
                before_byte: None,
                kind: Some("conversation".to_string()),
                source_filter: Some("automation".to_string()),
                search: None,
                limit: Some(80),
                content_limit: Some(120_000),
            },
        )
        .unwrap();
        assert_eq!(payload["items"].as_array().unwrap().len(), 1);
        assert_eq!(payload["items"][0]["promptSourceType"], "automation");
        assert_eq!(payload["items"][0]["kind"], "context");
    }

    #[test]
    fn protocol_mirror_prefers_explicit_commentary_phase() {
        let (_file, locator) = fixture_locator(&[
            json!({"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"正在检查页面"}],"phase":"commentary"}}),
            json!({"timestamp":"2026-01-01T00:00:00Z","type":"event_msg","payload":{"type":"agent_message","message":"正在检查页面"}}),
            json!({"timestamp":"2026-01-01T00:00:01Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"检查完成"}],"phase":"final_answer"}}),
            json!({"timestamp":"2026-01-01T00:00:01Z","type":"event_msg","payload":{"type":"agent_message","message":"检查完成"}}),
        ]);
        let query = |kind: &str| TimelineQuery {
            codex_home: None,
            before_byte: None,
            kind: Some(kind.to_string()),
            source_filter: Some("all".to_string()),
            search: None,
            limit: Some(80),
            content_limit: Some(120_000),
        };

        let commentary = read_timeline(&locator, &query("commentary")).unwrap();
        let assistant = read_timeline(&locator, &query("assistant")).unwrap();
        let conversation = read_timeline(&locator, &query("conversation")).unwrap();

        assert_eq!(commentary["items"].as_array().unwrap().len(), 1);
        assert_eq!(commentary["items"][0]["text"], "正在检查页面");
        assert_eq!(assistant["items"].as_array().unwrap().len(), 1);
        assert_eq!(assistant["items"][0]["text"], "检查完成");
        assert_eq!(conversation["items"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn cross_protocol_repeats_far_apart_in_time_are_preserved() {
        let (_file, locator) = fixture_locator(&[
            json!({"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"相同回复"}]}}),
            json!({"timestamp":"2026-01-01T00:30:00Z","type":"event_msg","payload":{"type":"task_started"}}),
            json!({"timestamp":"2026-01-01T01:00:00Z","type":"event_msg","payload":{"type":"agent_message","phase":"final_answer","message":"相同回复"}}),
        ]);
        let payload = read_timeline(
            &locator,
            &TimelineQuery {
                codex_home: None,
                before_byte: None,
                kind: Some("assistant".to_string()),
                source_filter: Some("all".to_string()),
                search: None,
                limit: Some(80),
                content_limit: Some(120_000),
            },
        )
        .unwrap();

        let items = payload["items"].as_array().unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0]["timestampMs"], 1_767_225_600_000_i64);
        assert_eq!(items[1]["timestampMs"], 1_767_229_200_000_i64);
    }

    #[test]
    fn protocol_mirror_does_not_use_an_adjacent_user_message() {
        let (_file, locator) = fixture_locator(&[
            json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"相同文本"}]}}),
            json!({"type":"event_msg","payload":{"type":"agent_message","message":"相同文本"}}),
        ]);
        let payload = read_timeline(
            &locator,
            &TimelineQuery {
                codex_home: None,
                before_byte: None,
                kind: Some("conversation".to_string()),
                source_filter: Some("all".to_string()),
                search: None,
                limit: Some(80),
                content_limit: Some(120_000),
            },
        )
        .unwrap();

        assert_eq!(
            payload["items"]
                .as_array()
                .unwrap()
                .iter()
                .map(|item| item["kind"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec!["user", "assistant_unclassified"]
        );
    }

    #[test]
    fn pure_source_filter_extracts_attachment_request_from_older_pages() {
        let (_file, locator) = fixture_locator(&[
            json!({"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"# Files mentioned by the user:\n\n## screenshot.png: C:/Temp/screenshot.png\n\n## My request:\nfix this"}]}}),
            json!({"timestamp":"2026-01-01T00:00:01Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"older answer"}]}}),
            json!({"timestamp":"2026-01-01T00:00:02Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"plain request"}]}}),
            json!({"timestamp":"2026-01-01T00:00:03Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"commentary","content":[{"type":"output_text","text":"working"}]}}),
        ]);
        let first_page = read_timeline(
            &locator,
            &TimelineQuery {
                codex_home: None,
                before_byte: None,
                kind: Some("conversation".to_string()),
                source_filter: Some("pure".to_string()),
                search: None,
                limit: Some(2),
                content_limit: Some(120_000),
            },
        )
        .unwrap();
        let second_page = read_timeline(
            &locator,
            &TimelineQuery {
                codex_home: None,
                before_byte: first_page["nextBeforeByte"]
                    .as_u64()
                    .map(|value| value as usize),
                kind: Some("conversation".to_string()),
                source_filter: Some("pure".to_string()),
                search: None,
                limit: Some(2),
                content_limit: Some(120_000),
            },
        )
        .unwrap();

        let all_items = first_page["items"]
            .as_array()
            .unwrap()
            .iter()
            .chain(second_page["items"].as_array().unwrap())
            .collect::<Vec<_>>();
        assert!(all_items.iter().any(|item| item["text"] == "plain request"));
        assert!(all_items.iter().any(|item| item["text"] == "fix this"));
        assert!(all_items.iter().any(|item| item["text"] == "older answer"));
        assert!(all_items
            .iter()
            .all(|item| item["text"] != "screenshot.png"));
        assert_eq!(first_page["sourceFilter"], "pure");
        assert_eq!(second_page["sourceFilter"], "pure");
    }

    #[test]
    fn continuation_segments_page_across_boundary_and_load_items_by_global_offset() {
        let mut old_file = NamedTempFile::new().expect("old fixture");
        let mut current_file = NamedTempFile::new().expect("current fixture");
        for line in [
            json!({"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"old question"}]}}),
            json!({"timestamp":"2026-01-01T00:00:01Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"old answer"}]}}),
        ] {
            writeln!(old_file, "{}", serde_json::to_string(&line).unwrap()).unwrap();
        }
        for line in [
            json!({"timestamp":"2026-01-02T00:00:00Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"new question"}]}}),
            json!({"timestamp":"2026-01-02T00:00:01Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"new answer"}]}}),
        ] {
            writeln!(current_file, "{}", serde_json::to_string(&line).unwrap()).unwrap();
        }
        let old_size = old_file.as_file().metadata().unwrap().len() as usize;
        let locator = ThreadLocator {
            id: "continued-thread".to_string(),
            title: "Continued fixture".to_string(),
            rollout_path: current_file.path().to_path_buf(),
            rollout_paths: vec![
                old_file.path().to_path_buf(),
                current_file.path().to_path_buf(),
            ],
        };
        let query = |before_byte, limit| TimelineQuery {
            codex_home: None,
            before_byte,
            kind: Some("conversation".to_string()),
            source_filter: Some("all".to_string()),
            search: None,
            limit: Some(limit),
            content_limit: Some(120_000),
        };

        let first_page = read_timeline(&locator, &query(None, 3)).unwrap();
        let first_items = first_page["items"].as_array().unwrap();
        assert_eq!(
            first_items
                .iter()
                .map(|item| item["text"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec!["old answer", "new question", "new answer"]
        );
        assert_eq!(first_page["segmentCount"], 2);
        assert_eq!(first_page["rolloutPaths"].as_array().unwrap().len(), 2);
        assert_eq!(first_page["hasMore"], true);
        assert!(first_items[0]["byteOffset"].as_u64().unwrap() < old_size as u64);
        assert!(first_items[1]["byteOffset"].as_u64().unwrap() >= old_size as u64);

        let second_page = read_timeline(
            &locator,
            &query(
                first_page["nextBeforeByte"]
                    .as_u64()
                    .map(|value| value as usize),
                3,
            ),
        )
        .unwrap();
        assert_eq!(second_page["items"].as_array().unwrap().len(), 1);
        assert_eq!(second_page["items"][0]["text"], "old question");
        assert_eq!(second_page["hasMore"], false);

        for item in first_items
            .iter()
            .chain(second_page["items"].as_array().unwrap())
        {
            let offset = item["byteOffset"].as_u64().unwrap() as usize;
            let loaded = read_timeline_item(&locator, offset, 120_000).unwrap();
            assert_eq!(loaded["id"], item["id"]);
            assert_eq!(loaded["text"], item["text"]);
        }
    }
}
