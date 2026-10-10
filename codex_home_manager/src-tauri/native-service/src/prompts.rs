use std::{
    collections::{HashMap, VecDeque},
    fs::File,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, MutexGuard, OnceLock, TryLockError,
    },
    time::{Duration, Instant, UNIX_EPOCH},
};

use axum::{
    extract::{Path as AxumPath, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use memchr::{memchr, memmem};
use memmap2::MmapOptions;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::{
    catalog::{resolve_codex_home, ThreadCatalogStore, ThreadLocator},
    history::text_from_value,
    AppState,
};

#[path = "prompt_index.rs"]
pub(crate) mod prompt_index;

const MAX_RECORD_BYTES: usize = 64 * 1024 * 1024;
const MAX_REQUEST_ID_BYTES: usize = 128;
const PROMPT_REQUEST_CANCELLED: &str = "prompt request was cancelled";
#[cfg(test)]
static NEVER_CANCELLED: AtomicBool = AtomicBool::new(false);
type RecentPrompt = (String, Option<String>, String, usize);
type RecentPromptFingerprint = (String, Option<String>, String, usize);

#[derive(Clone, Debug, Deserialize)]
pub struct PromptPageQuery {
    codex_home: Option<String>,
    cursor: Option<String>,
    limit: Option<usize>,
    search: Option<String>,
    scope: Option<String>,
    #[serde(rename = "sourceType")]
    source_type: Option<String>,
    order: Option<String>,
    strategy: Option<String>,
    #[serde(rename = "requestId")]
    request_id: Option<String>,
    #[serde(rename = "scanBudgetMs")]
    scan_budget_ms: Option<u64>,
}

#[derive(Debug, Deserialize)]
pub struct PromptContextQuery {
    codex_home: Option<String>,
    #[serde(rename = "anchorIndex")]
    anchor_index: usize,
    #[serde(rename = "anchorByteOffset")]
    anchor_byte_offset: Option<i64>,
    #[serde(rename = "anchorRecordId")]
    anchor_record_id: Option<String>,
    generation: Option<String>,
    search: Option<String>,
    scope: Option<String>,
    #[serde(rename = "sourceType")]
    source_type: Option<String>,
    before: Option<usize>,
    after: Option<usize>,
    #[serde(rename = "scanBudgetMs")]
    scan_budget_ms: Option<u64>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct PromptRecord {
    index: usize,
    line_number: usize,
    byte_offset: usize,
    ordinal_exact: bool,
    timestamp: Option<String>,
    text: String,
    character_count: usize,
    protocol: String,
    source_type: String,
    source_label: String,
    visible_by_default: bool,
    pure_text: String,
    pure_character_count: usize,
    has_pure_text: bool,
}

#[derive(Clone)]
struct PromptSnapshot {
    file_size: u64,
    modified_ns: u128,
    prefix_len: usize,
    prefix_hash: [u8; 32],
    tail_hash: [u8; 32],
    prompts: Arc<Vec<PromptRecord>>,
    source_counts: Arc<HashMap<String, usize>>,
    pure_prompt_count: usize,
    visible_prompt_count: usize,
    scanned_lines: usize,
    skipped_oversized_records: usize,
    recent: Arc<VecDeque<RecentPrompt>>,
    version: String,
    elapsed_ms: u128,
    incremental: bool,
}

#[derive(Default)]
pub struct PromptStore {
    snapshots: Mutex<HashMap<PathBuf, Arc<PromptSnapshot>>>,
    locators: Mutex<HashMap<String, ThreadLocator>>,
    build_locks: Mutex<HashMap<PathBuf, Arc<Mutex<()>>>>,
    requests: Mutex<HashMap<String, ActivePromptRequest>>,
}

struct ActivePromptRequest {
    thread_id: String,
    cancelled: Arc<AtomicBool>,
}

struct PromptRequestRegistration {
    request_id: Option<String>,
    cancelled: Arc<AtomicBool>,
}

struct PromptRequestGuard {
    store: Arc<PromptStore>,
    registration: PromptRequestRegistration,
}

impl Drop for PromptRequestGuard {
    fn drop(&mut self) {
        self.store.finish_request(&self.registration);
    }
}

impl PromptStore {
    fn locator(
        &self,
        catalog: &ThreadCatalogStore,
        codex_home: Option<&str>,
        thread_id: &str,
    ) -> Result<ThreadLocator, String> {
        let key = format!(
            "{}\0{thread_id}",
            codex_home.unwrap_or_default().to_lowercase()
        );
        if let Some(locator) = self
            .locators
            .lock()
            .map_err(|_| "prompt locator cache is poisoned".to_string())?
            .get(&key)
            .filter(|locator| locator.rollout_path.is_file())
            .cloned()
        {
            return Ok(locator);
        }
        let locator = catalog.thread_locator(codex_home, thread_id)?;
        self.locators
            .lock()
            .map_err(|_| "prompt locator cache is poisoned".to_string())?
            .insert(key, locator.clone());
        Ok(locator)
    }

    #[cfg(test)]
    fn snapshot(&self, locator: &ThreadLocator) -> Result<Arc<PromptSnapshot>, String> {
        self.snapshot_cancelable(locator, &NEVER_CANCELLED)
    }

    fn snapshot_cancelable(
        &self,
        locator: &ThreadLocator,
        cancelled: &AtomicBool,
    ) -> Result<Arc<PromptSnapshot>, String> {
        check_cancelled(cancelled)?;
        let (file_size, modified_ns) = file_version(&locator.rollout_path)?;
        let cached = self
            .snapshots
            .lock()
            .map_err(|_| "prompt snapshot cache is poisoned".to_string())?
            .get(&locator.rollout_path)
            .cloned();
        if let Some(snapshot) = cached.as_ref() {
            if snapshot.file_size == file_size && snapshot.modified_ns == modified_ns {
                check_cancelled(cancelled)?;
                return Ok(snapshot.clone());
            }
        }

        let build_lock = self
            .build_locks
            .lock()
            .map_err(|_| "prompt snapshot lock registry is poisoned".to_string())?
            .entry(locator.rollout_path.clone())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone();
        let _build_guard = lock_build_cancelable(&build_lock, cancelled)?;
        check_cancelled(cancelled)?;
        let (file_size, modified_ns) = file_version(&locator.rollout_path)?;
        let cached = self
            .snapshots
            .lock()
            .map_err(|_| "prompt snapshot cache is poisoned".to_string())?
            .get(&locator.rollout_path)
            .cloned();
        if let Some(snapshot) = cached.as_ref() {
            if snapshot.file_size == file_size && snapshot.modified_ns == modified_ns {
                check_cancelled(cancelled)?;
                return Ok(snapshot.clone());
            }
        }

        let rebuilt = if let Some(prior) = cached
            .as_deref()
            .filter(|prior| file_size > prior.file_size)
        {
            extend_snapshot(
                &locator.rollout_path,
                prior,
                file_size,
                modified_ns,
                cancelled,
            )?
            .map(Ok)
            .unwrap_or_else(|| {
                build_snapshot(&locator.rollout_path, file_size, modified_ns, cancelled)
            })?
        } else {
            build_snapshot(&locator.rollout_path, file_size, modified_ns, cancelled)?
        };
        check_cancelled(cancelled)?;
        let snapshot = Arc::new(rebuilt);
        self.snapshots
            .lock()
            .map_err(|_| "prompt snapshot cache is poisoned".to_string())?
            .insert(locator.rollout_path.clone(), snapshot.clone());
        Ok(snapshot)
    }

    fn begin_request(
        &self,
        thread_id: &str,
        request_id: Option<&str>,
    ) -> Result<PromptRequestRegistration, String> {
        let Some(request_id) = request_id.map(str::trim).filter(|value| !value.is_empty()) else {
            return Ok(PromptRequestRegistration {
                request_id: None,
                cancelled: Arc::new(AtomicBool::new(false)),
            });
        };
        if request_id.len() > MAX_REQUEST_ID_BYTES {
            return Err(format!(
                "prompt request id exceeds {MAX_REQUEST_ID_BYTES} bytes"
            ));
        }
        let mut requests = self
            .requests
            .lock()
            .map_err(|_| "prompt request registry is poisoned".to_string())?;
        if requests.contains_key(request_id) {
            return Err("prompt request id is already active".to_string());
        }
        let cancelled = Arc::new(AtomicBool::new(false));
        requests.insert(
            request_id.to_string(),
            ActivePromptRequest {
                thread_id: thread_id.to_string(),
                cancelled: cancelled.clone(),
            },
        );
        Ok(PromptRequestRegistration {
            request_id: Some(request_id.to_string()),
            cancelled,
        })
    }

    fn finish_request(&self, registration: &PromptRequestRegistration) {
        let Some(request_id) = registration.request_id.as_deref() else {
            return;
        };
        if let Ok(mut requests) = self.requests.lock() {
            if requests
                .get(request_id)
                .is_some_and(|request| Arc::ptr_eq(&request.cancelled, &registration.cancelled))
            {
                requests.remove(request_id);
            }
        }
    }

    fn cancel_request(&self, thread_id: &str, request_id: &str) -> bool {
        self.requests
            .lock()
            .ok()
            .and_then(|requests| {
                requests
                    .get(request_id)
                    .filter(|request| request.thread_id == thread_id)
                    .map(|request| request.cancelled.clone())
            })
            .is_some_and(|cancelled| {
                cancelled.store(true, Ordering::Release);
                true
            })
    }
}

pub async fn thread_prompt_page(
    State(state): State<AppState>,
    AxumPath(thread_id): AxumPath<String>,
    Query(mut query): Query<PromptPageQuery>,
) -> Response {
    // Resolve before dispatch: a blank Home must not select a different index/ordinal space.
    let codex_home = match resolve_codex_home(query.codex_home.as_deref()) {
        Ok(home) => home,
        Err(error) => return error.into_response(),
    };
    query.codex_home = Some(codex_home.to_string_lossy().into_owned());
    let locator =
        match state
            .prompts
            .locator(&state.catalog, query.codex_home.as_deref(), &thread_id)
        {
            Ok(locator) => locator,
            Err(error) => {
                return (StatusCode::NOT_FOUND, Json(json!({"detail": error}))).into_response()
            }
        };
    let store = state.prompts.clone();
    let registration = match store.begin_request(&thread_id, query.request_id.as_deref()) {
        Ok(registration) => registration,
        Err(error) => {
            return (
                StatusCode::CONFLICT,
                Json(json!({"code": "prompt_request_conflict", "detail": error})),
            )
                .into_response()
        }
    };
    let cancelled = registration.cancelled.clone();
    let request_guard = PromptRequestGuard {
        store: store.clone(),
        registration,
    };
    let worker_store = store.clone();
    let result = tokio::task::spawn_blocking(move || {
        let _request_guard = request_guard;
        read_prompt_page_cancelable(&worker_store, &locator, &query, cancelled)
    })
    .await;
    match result {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => {
            let status = if error == PROMPT_REQUEST_CANCELLED {
                StatusCode::from_u16(499).expect("499 is a valid HTTP status")
            } else {
                StatusCode::BAD_REQUEST
            };
            (
                status,
                Json(json!({
                    "code": prompt_request_error_code(&error),
                    "detail": error,
                })),
            )
                .into_response()
        }
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"detail": format!("prompt worker failed: {error}")})),
        )
            .into_response(),
    }
}

pub async fn thread_prompt_context(
    State(state): State<AppState>,
    AxumPath(thread_id): AxumPath<String>,
    Query(mut query): Query<PromptContextQuery>,
) -> Response {
    let codex_home = match resolve_codex_home(query.codex_home.as_deref()) {
        Ok(home) => home,
        Err(error) => return error.into_response(),
    };
    query.codex_home = Some(codex_home.to_string_lossy().into_owned());
    let locator =
        match state
            .prompts
            .locator(&state.catalog, query.codex_home.as_deref(), &thread_id)
        {
            Ok(locator) => locator,
            Err(error) => {
                return (StatusCode::NOT_FOUND, Json(json!({"detail": error}))).into_response()
            }
        };
    match tokio::task::spawn_blocking(move || {
        prompt_index::read_persistent_prompt_context(&locator, &codex_home, &query)
    })
    .await
    {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"code": if error.starts_with("prompt context anchor is stale") { "prompt_context_stale" } else { "prompt_context_invalid" }, "detail": error})),
        )
            .into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"detail": format!("prompt context worker failed: {error}")})),
        )
            .into_response(),
    }
}

pub async fn cancel_thread_prompt_request(
    State(state): State<AppState>,
    AxumPath((thread_id, request_id)): AxumPath<(String, String)>,
) -> Response {
    Json(json!({
        "threadId": thread_id,
        "requestId": request_id,
        "cancelled": state.prompts.cancel_request(&thread_id, &request_id),
    }))
    .into_response()
}

pub async fn native_prompt_index_status(Query(query): Query<PromptPageQuery>) -> Response {
    let codex_home = query
        .codex_home
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"D:\.codex"));
    match tokio::task::spawn_blocking(move || prompt_index::prompt_index_status(&codex_home)).await
    {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => (StatusCode::BAD_REQUEST, Json(json!({"detail": error}))).into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"detail": format!("prompt index status worker failed: {error}")})),
        )
            .into_response(),
    }
}

fn prompt_request_error_code(error: &str) -> &'static str {
    if error == PROMPT_REQUEST_CANCELLED {
        "prompt_request_cancelled"
    } else if error.to_ascii_lowercase().contains("prompt cursor") {
        "prompt_cursor_invalid"
    } else {
        "prompt_request_invalid"
    }
}

#[cfg(test)]
fn read_prompt_page(
    store: &PromptStore,
    locator: &ThreadLocator,
    query: &PromptPageQuery,
) -> Result<Value, String> {
    read_prompt_page_cancelable(
        store,
        locator,
        query,
        Arc::new(AtomicBool::new(NEVER_CANCELLED.load(Ordering::Relaxed))),
    )
}

fn read_prompt_page_cancelable(
    store: &PromptStore,
    locator: &ThreadLocator,
    query: &PromptPageQuery,
    cancelled: Arc<AtomicBool>,
) -> Result<Value, String> {
    check_cancelled(&cancelled)?;
    let started = Instant::now();
    let scope = query
        .scope
        .as_deref()
        .unwrap_or("visible")
        .trim()
        .to_lowercase();
    if !matches!(
        scope.as_str(),
        "pure" | "visible" | "all" | "with_agents" | "automation" | "heartbeat" | "delegation"
    ) {
        return Err(format!("unsupported prompt scope: {scope}"));
    }
    let order = query
        .order
        .as_deref()
        .unwrap_or("asc")
        .trim()
        .to_lowercase();
    if !matches!(order.as_str(), "asc" | "desc") {
        return Err(format!("unsupported prompt order: {order}"));
    }
    let search = query.search.as_deref().unwrap_or("").trim().to_string();
    let normalized_search = search.to_lowercase();
    let source_type = query
        .source_type
        .as_deref()
        .unwrap_or("")
        .trim()
        .to_string();
    let limit = query.limit.unwrap_or(100).clamp(1, 500);
    let strategy = query
        .strategy
        .as_deref()
        .unwrap_or("index")
        .trim()
        .to_lowercase();
    if strategy == "recent" {
        if order != "desc" || !search.is_empty() {
            return Err(
                "recent prompt strategy requires descending order without search".to_string(),
            );
        }
        if let Some(codex_home) = query.codex_home.as_deref().map(str::trim).filter(|home| !home.is_empty()) {
            if let Some(page) = prompt_index::read_indexed_recent_prompt_page(
                locator, Path::new(codex_home), query, &scope, &source_type, limit.min(200), cancelled.clone(),
            )? {
                return Ok(page);
            }
        }
        if locator.rollout_paths.len() > 1
            && source_type.is_empty()
            && matches!(scope.as_str(), "pure" | "visible" | "with_agents" | "all")
        {
            return read_multi_recent_prompt_page(
                locator,
                query,
                &scope,
                &source_type,
                limit.min(200),
                &cancelled,
            );
        }
        if locator.rollout_paths.len() <= 1 {
            return read_recent_prompt_page(
                locator,
                query,
                &scope,
                &source_type,
                limit.min(200),
                &cancelled,
            );
        }
    }
    if !matches!(strategy.as_str(), "index" | "native" | "recent") {
        return Err(format!("unsupported prompt strategy: {strategy}"));
    }

    if let Some(codex_home) = query
        .codex_home
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        return prompt_index::read_persistent_prompt_page(
            locator,
            Path::new(codex_home),
            query,
            &scope,
            &source_type,
            &order,
            &search,
            limit,
            cancelled,
        );
    }

    let snapshot = store.snapshot_cancelable(locator, &cancelled)?;
    let signature = query_signature(&scope, &normalized_search, &source_type, &order, "index-v2");
    let cursor = decode_cursor(query.cursor.as_deref(), &signature, snapshot.file_size)?;

    let mut matching = Vec::new();
    for (index, prompt) in snapshot.prompts.iter().enumerate() {
        if index % 256 == 0 {
            check_cancelled(&cancelled)?;
        }
        if prompt_matches(prompt, &scope, &source_type, &normalized_search) {
            matching.push(prompt);
        }
    }
    check_cancelled(&cancelled)?;
    let match_count = matching.len();
    if let Some(cursor) = cursor.as_ref() {
        let mut anchor = None;
        for (index, prompt) in snapshot.prompts.iter().enumerate() {
            if index % 256 == 0 {
                check_cancelled(&cancelled)?;
            }
            if prompt.byte_offset == cursor.anchor_byte {
                anchor = Some(prompt);
                break;
            }
        }
        let anchor =
            anchor.ok_or_else(|| "native prompt cursor anchor no longer exists".to_string())?;
        if prompt_cursor_fingerprint(anchor) != cursor.anchor_fingerprint {
            return Err("native prompt cursor anchor changed after the page was read".to_string());
        }
        let mut remaining = Vec::with_capacity(matching.len());
        for (index, prompt) in matching.into_iter().enumerate() {
            if index % 256 == 0 {
                check_cancelled(&cancelled)?;
            }
            let follows_cursor = if order == "desc" {
                prompt.byte_offset < cursor.anchor_byte
            } else {
                prompt.byte_offset > cursor.anchor_byte
            };
            if follows_cursor {
                remaining.push(prompt);
            }
        }
        matching = remaining;
        check_cancelled(&cancelled)?;
    }
    if order == "desc" {
        matching.reverse();
    }
    let end = limit.min(matching.len());
    let prompts = matching[..end]
        .iter()
        .map(|prompt| (*prompt).clone())
        .collect::<Vec<_>>();
    let has_more = end < matching.len();
    let next_cursor = has_more.then(|| {
        let anchor = prompts
            .last()
            .expect("a page with more results has an anchor");
        encode_cursor(anchor, &signature, snapshot.file_size)
    });
    let prompt_count = snapshot.prompts.len();
    check_cancelled(&cancelled)?;

    Ok(json!({
        "threadId": locator.id,
        "title": locator.title,
        "rolloutPath": locator.rollout_path.to_string_lossy(),
        "requestId": query.request_id.clone().unwrap_or_default(),
        "scope": scope,
        "search": search,
        "sourceType": if source_type.is_empty() { Value::Null } else { Value::String(source_type) },
        "order": order,
        "promptCount": prompt_count,
        "purePromptCount": snapshot.pure_prompt_count,
        "visiblePromptCount": snapshot.visible_prompt_count,
        "hiddenPromptCount": prompt_count.saturating_sub(snapshot.visible_prompt_count),
        "sourceCounts": &*snapshot.source_counts,
        "matchCount": match_count,
        "matchCountComplete": true,
        "prompts": prompts,
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "index": {
            "complete": true,
            "strategy": "native",
            "runtime": "rust",
            "version": snapshot.version,
            "scannedBytes": snapshot.file_size,
            "scannedLines": snapshot.scanned_lines,
            "fileSize": snapshot.file_size,
            "skippedOversizedRecords": snapshot.skipped_oversized_records,
            "buildElapsedMs": snapshot.elapsed_ms,
            "incremental": snapshot.incremental,
            "elapsedMs": started.elapsed().as_millis(),
        }
    }))
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RecentCursor {
    query: String,
    source_size: usize,
    prefix_len: usize,
    prefix_hash: String,
    before_byte: usize,
    prompt_count: usize,
    pure_prompt_count: usize,
    visible_prompt_count: usize,
    match_count: usize,
    source_counts: HashMap<String, usize>,
    recent: Vec<RecentPromptFingerprint>,
    scanned_lines: usize,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct MultiRecentCursor {
    query: String,
    version: String,
    segment_index: usize,
    segment_cursor: Option<String>,
    prompt_count: usize,
    pure_prompt_count: usize,
    visible_prompt_count: usize,
    match_count: usize,
    source_counts: HashMap<String, usize>,
    scanned_lines: usize,
    scanned_bytes: usize,
}

fn read_multi_recent_prompt_page(
    locator: &ThreadLocator,
    query: &PromptPageQuery,
    scope: &str,
    source_type: &str,
    limit: usize,
    cancelled: &AtomicBool,
) -> Result<Value, String> {
    check_cancelled(cancelled)?;
    let started = Instant::now();
    let paths = &locator.rollout_paths;
    let mut file_sizes = Vec::with_capacity(paths.len());
    let mut version_digest = Sha256::new();
    version_digest.update(b"recent-multi-v2\0");
    for path in paths {
        let (size, _modified_ns) = file_version(path)?;
        let size = usize::try_from(size)
            .map_err(|_| "rollout size does not fit this platform".to_string())?;
        file_sizes.push(size);
        version_digest.update(path.to_string_lossy().as_bytes());
        version_digest.update(b"\0");
    }
    let version = format!("{:x}", version_digest.finalize());
    let signature = query_signature(scope, "", source_type, "desc", "recent-multi-v1");
    let mut state = if let Some(cursor) = query.cursor.as_deref().filter(|value| !value.is_empty())
    {
        let bytes = URL_SAFE_NO_PAD
            .decode(cursor)
            .map_err(|_| "invalid recent prompt cursor".to_string())?;
        let state: MultiRecentCursor = serde_json::from_slice(&bytes)
            .map_err(|_| "invalid recent prompt cursor".to_string())?;
        if state.query != signature
            || state.version != version
            || state.segment_index >= paths.len()
        {
            return Err(
                "recent prompt cursor is stale or does not match the current filters".to_string(),
            );
        }
        state
    } else {
        MultiRecentCursor {
            query: signature,
            version: version.clone(),
            segment_index: paths.len() - 1,
            segment_cursor: None,
            prompt_count: 0,
            pure_prompt_count: 0,
            visible_prompt_count: 0,
            match_count: 0,
            source_counts: HashMap::new(),
            scanned_lines: 0,
            scanned_bytes: 0,
        }
    };
    let mut prompts = Vec::with_capacity(limit);
    let mut current_totals = None;
    let mut has_more = false;
    loop {
        check_cancelled(cancelled)?;
        let segment_path = paths[state.segment_index].clone();
        let segment_locator = ThreadLocator {
            id: locator.id.clone(),
            title: locator.title.clone(),
            rollout_path: segment_path,
            rollout_paths: Vec::new(),
        };
        let segment_query = PromptPageQuery {
            cursor: state.segment_cursor.clone(),
            limit: Some(limit.saturating_sub(prompts.len()).max(1)),
            strategy: Some("recent".to_string()),
            ..query.clone()
        };
        let page = read_recent_prompt_page(
            &segment_locator,
            &segment_query,
            scope,
            source_type,
            limit.saturating_sub(prompts.len()).max(1),
            cancelled,
        )?;
        let segment_base = file_sizes[..state.segment_index].iter().sum::<usize>();
        for mut prompt in page["prompts"].as_array().cloned().unwrap_or_default() {
            let local_offset = prompt["byteOffset"].as_u64().unwrap_or_default() as usize;
            let global_offset = segment_base.saturating_add(local_offset);
            prompt["index"] = json!(global_offset.saturating_add(1));
            prompt["byteOffset"] = json!(global_offset);
            prompts.push(prompt);
        }
        let totals = (
            page["promptCount"].as_u64().unwrap_or_default() as usize,
            page["purePromptCount"].as_u64().unwrap_or_default() as usize,
            page["visiblePromptCount"].as_u64().unwrap_or_default() as usize,
            page["matchCount"].as_u64().unwrap_or_default() as usize,
            page["sourceCounts"]
                .as_object()
                .cloned()
                .unwrap_or_default(),
            page["index"]["scannedLines"].as_u64().unwrap_or_default() as usize,
            page["index"]["scannedBytes"].as_u64().unwrap_or_default() as usize,
        );
        if page["hasMore"].as_bool().unwrap_or(false) {
            state.segment_cursor = page["nextCursor"].as_str().map(str::to_string);
            current_totals = Some(totals);
            has_more = true;
            break;
        }
        state.prompt_count = state.prompt_count.saturating_add(totals.0);
        state.pure_prompt_count = state.pure_prompt_count.saturating_add(totals.1);
        state.visible_prompt_count = state.visible_prompt_count.saturating_add(totals.2);
        state.match_count = state.match_count.saturating_add(totals.3);
        merge_source_counts(&mut state.source_counts, &totals.4);
        state.scanned_lines = state.scanned_lines.saturating_add(totals.5);
        state.scanned_bytes = state.scanned_bytes.saturating_add(totals.6);
        if state.segment_index == 0 {
            break;
        }
        state.segment_index -= 1;
        state.segment_cursor = None;
        if prompts.len() >= limit {
            has_more = true;
            break;
        }
    }
    let mut prompt_count = state.prompt_count;
    let mut pure_prompt_count = state.pure_prompt_count;
    let mut visible_prompt_count = state.visible_prompt_count;
    let mut match_count = state.match_count;
    let mut source_counts = state.source_counts.clone();
    let mut scanned_lines = state.scanned_lines;
    let mut scanned_bytes = state.scanned_bytes;
    if let Some(totals) = current_totals {
        prompt_count = prompt_count.saturating_add(totals.0);
        pure_prompt_count = pure_prompt_count.saturating_add(totals.1);
        visible_prompt_count = visible_prompt_count.saturating_add(totals.2);
        match_count = match_count.saturating_add(totals.3);
        merge_source_counts(&mut source_counts, &totals.4);
        scanned_lines = scanned_lines.saturating_add(totals.5);
        scanned_bytes = scanned_bytes.saturating_add(totals.6);
    }
    let next_cursor = has_more.then(|| {
        URL_SAFE_NO_PAD.encode(
            serde_json::to_vec(&state).expect("multi-segment recent cursor is serializable"),
        )
    });
    Ok(json!({
        "threadId": locator.id,
        "title": locator.title,
        "rolloutPath": locator.rollout_path.to_string_lossy(),
        "rolloutPaths": paths.iter().map(|path| path.to_string_lossy().into_owned()).collect::<Vec<_>>(),
        "requestId": query.request_id.clone().unwrap_or_default(),
        "scope": scope,
        "search": "",
        "sourceType": if source_type.is_empty() { Value::Null } else { Value::String(source_type.to_string()) },
        "order": "desc",
        "promptCount": prompt_count,
        "purePromptCount": pure_prompt_count,
        "visiblePromptCount": visible_prompt_count,
        "hiddenPromptCount": prompt_count.saturating_sub(visible_prompt_count),
        "sourceCounts": source_counts,
        "matchCount": match_count,
        "matchCountComplete": !has_more,
        "prompts": prompts,
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "index": {
            "complete": !has_more,
            "strategy": "recent",
            "runtime": "rust",
            "version": version,
            "segmentCount": paths.len(),
            "scannedBytes": scanned_bytes,
            "scannedLines": scanned_lines,
            "fileSize": file_sizes.iter().sum::<usize>(),
            "elapsedMs": started.elapsed().as_millis(),
        }
    }))
}

fn merge_source_counts(
    target: &mut HashMap<String, usize>,
    source: &serde_json::Map<String, Value>,
) {
    for (name, value) in source {
        *target.entry(name.clone()).or_default() += value.as_u64().unwrap_or_default() as usize;
    }
}

fn read_recent_prompt_page(
    locator: &ThreadLocator,
    query: &PromptPageQuery,
    scope: &str,
    source_type: &str,
    limit: usize,
    cancelled: &AtomicBool,
) -> Result<Value, String> {
    check_cancelled(cancelled)?;
    let started = Instant::now();
    let (file_size_u64, modified_ns) = file_version(&locator.rollout_path)?;
    let file_size = usize::try_from(file_size_u64)
        .map_err(|_| "rollout size does not fit this platform".to_string())?;
    let version = format!("{file_size_u64:x}-{modified_ns:x}");
    let mapped = if file_size == 0 {
        None
    } else {
        let file = File::open(&locator.rollout_path)
            .map_err(|error| format!("cannot open {}: {error}", locator.rollout_path.display()))?;
        Some(
            unsafe { MmapOptions::new().map(&file) }.map_err(|error| {
                format!("cannot map {}: {error}", locator.rollout_path.display())
            })?,
        )
    };
    let signature = query_signature(scope, "", source_type, "desc", "recent-v2");
    let current_bytes = mapped.as_deref().unwrap_or(&[]);
    let mut state = decode_recent_cursor(
        query.cursor.as_deref(),
        &signature,
        file_size,
        current_bytes,
    )?;
    let mut prompts = Vec::with_capacity(limit);
    let mut line_end = state.before_byte.min(file_size);
    let scan_start = line_end;
    let scan_budget = std::time::Duration::from_millis(query.scan_budget_ms.unwrap_or(100).clamp(1, 5_000));
    const RECENT_SCAN_BYTES: usize = 8 * 1024 * 1024;
    if let Some(mapped) = mapped.as_ref() {
        if line_end > 0 && mapped[line_end - 1] == b'\n' {
            line_end -= 1;
        }
        while line_end > 0 {
            check_cancelled(cancelled)?;
            let line_start =
                memchr::memrchr(b'\n', &mapped[..line_end]).map_or(0, |index| index + 1);
            let raw = &mapped[line_start..line_end];
            state.scanned_lines += 1;
            if raw.len() <= MAX_RECORD_BYTES && prompt_candidate(raw) {
                if let Ok(item) = serde_json::from_slice::<Value>(raw) {
                    if let Some((text, protocol)) = prompt_text_from_item(&item) {
                        let normalized_text = text.trim().to_string();
                        if !normalized_text.is_empty() {
                            let timestamp = item
                                .get("timestamp")
                                .and_then(Value::as_str)
                                .map(str::to_string);
                            let classification = classify_prompt(&normalized_text);
                            let canonical_text = if classification.has_pure_text {
                                classification.pure_text.clone()
                            } else {
                                normalized_text.clone()
                            };
                            let canonical_fingerprint = prompt_fingerprint(&canonical_text);
                            if !is_cross_protocol_fingerprint_duplicate(
                                &canonical_fingerprint,
                                timestamp.as_deref(),
                                &protocol,
                                state.scanned_lines,
                                &state.recent,
                            ) {
                                state.prompt_count += 1;
                                if classification.has_pure_text {
                                    state.pure_prompt_count += 1;
                                }
                                if classification.visible_by_default {
                                    state.visible_prompt_count += 1;
                                }
                                *state
                                    .source_counts
                                    .entry(classification.source_type.clone())
                                    .or_insert(0) += 1;
                                let prompt = PromptRecord {
                                    index: line_start.saturating_add(1),
                                    line_number: 0,
                                    byte_offset: line_start,
                                    ordinal_exact: false,
                                    timestamp: timestamp.clone(),
                                    text: normalized_text.clone(),
                                    character_count: normalized_text.chars().count(),
                                    protocol: protocol.clone(),
                                    source_type: classification.source_type,
                                    source_label: classification.source_label,
                                    visible_by_default: classification.visible_by_default,
                                    pure_character_count: classification.pure_text.chars().count(),
                                    has_pure_text: classification.has_pure_text,
                                    pure_text: classification.pure_text,
                                };
                                if prompt_matches(&prompt, scope, source_type, "") {
                                    state.match_count += 1;
                                    prompts.push(prompt);
                                }
                                state.recent.push((
                                    canonical_fingerprint,
                                    timestamp,
                                    protocol,
                                    state.scanned_lines,
                                ));
                                if state.recent.len() > 8 {
                                    state.recent.remove(0);
                                }
                            }
                        }
                    }
                }
            }
            line_end = line_start.saturating_sub(1);
            if prompts.len() >= limit {
                break;
            }
            if scan_start.saturating_sub(line_end) >= RECENT_SCAN_BYTES || started.elapsed() >= scan_budget {
                break;
            }
        }
    }
    check_cancelled(cancelled)?;
    state.before_byte = line_end;
    let has_more = line_end > 0;
    let next_cursor = has_more.then(|| encode_recent_cursor(&state));
    let scanned_bytes = file_size.saturating_sub(line_end);
    Ok(json!({
        "threadId": locator.id,
        "title": locator.title,
        "rolloutPath": locator.rollout_path.to_string_lossy(),
        "requestId": query.request_id.clone().unwrap_or_default(),
        "scope": scope,
        "search": "",
        "sourceType": if source_type.is_empty() { Value::Null } else { Value::String(source_type.to_string()) },
        "order": "desc",
        "promptCount": state.prompt_count,
        "purePromptCount": state.pure_prompt_count,
        "visiblePromptCount": state.visible_prompt_count,
        "hiddenPromptCount": state.prompt_count.saturating_sub(state.visible_prompt_count),
        "sourceCounts": state.source_counts,
        "matchCount": state.match_count,
        "matchCountComplete": !has_more,
        "prompts": prompts,
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "index": {
            "complete": false,
            "strategy": "recent",
            "runtime": "rust",
            "version": version,
            "scannedBytes": scanned_bytes,
            "scannedLines": state.scanned_lines,
            "fileSize": file_size,
            "elapsedMs": started.elapsed().as_millis(),
        }
    }))
}

fn encode_recent_cursor(state: &RecentCursor) -> String {
    URL_SAFE_NO_PAD.encode(serde_json::to_vec(state).expect("recent prompt cursor is serializable"))
}

fn decode_recent_cursor(
    cursor: Option<&str>,
    signature: &str,
    file_size: usize,
    current_bytes: &[u8],
) -> Result<RecentCursor, String> {
    let Some(cursor) = cursor.filter(|value| !value.is_empty()) else {
        let prefix_len = current_bytes.len().min(64 * 1024);
        return Ok(RecentCursor {
            query: signature.to_string(),
            source_size: file_size,
            prefix_len,
            prefix_hash: URL_SAFE_NO_PAD
                .encode(hash_prefix_from_bytes(&current_bytes[..prefix_len])),
            before_byte: file_size,
            prompt_count: 0,
            pure_prompt_count: 0,
            visible_prompt_count: 0,
            match_count: 0,
            source_counts: HashMap::new(),
            recent: Vec::new(),
            scanned_lines: 0,
        });
    };
    let bytes = URL_SAFE_NO_PAD
        .decode(cursor)
        .map_err(|_| "invalid recent prompt cursor".to_string())?;
    let state: RecentCursor =
        serde_json::from_slice(&bytes).map_err(|_| "invalid recent prompt cursor".to_string())?;
    if state.query != signature
        || state.before_byte > state.source_size
        || file_size < state.source_size
        || state.prefix_len > current_bytes.len()
        || URL_SAFE_NO_PAD.encode(hash_prefix_from_bytes(&current_bytes[..state.prefix_len]))
            != state.prefix_hash
    {
        return Err(
            "recent prompt cursor is stale or does not match the current filters".to_string(),
        );
    }
    Ok(state)
}

fn build_snapshot(
    path: &Path,
    file_size: u64,
    modified_ns: u128,
    cancelled: &AtomicBool,
) -> Result<PromptSnapshot, String> {
    check_cancelled(cancelled)?;
    let started = Instant::now();
    let mut prompts = Vec::new();
    let mut source_counts = HashMap::new();
    let mut pure_prompt_count = 0usize;
    let mut visible_prompt_count = 0usize;
    let mut scanned_lines = 0usize;
    let mut skipped_oversized_records = 0usize;
    let mut recent = VecDeque::<RecentPrompt>::new();
    let mut prefix_len = 0usize;
    let mut prefix_hash = hash_prefix_from_bytes(&[]);
    let mut tail_hash = hash_prefix_from_bytes(&[]);

    if file_size > 0 {
        let file =
            File::open(path).map_err(|error| format!("cannot open {}: {error}", path.display()))?;
        let mapped = unsafe { MmapOptions::new().map(&file) }
            .map_err(|error| format!("cannot map {}: {error}", path.display()))?;
        prefix_len = mapped.len().min(64 * 1024);
        prefix_hash = hash_prefix_from_bytes(&mapped[..prefix_len]);
        tail_hash = hash_prefix_from_bytes(&mapped[mapped.len().saturating_sub(64 * 1024)..]);
        let mut line_start = 0usize;
        while line_start < mapped.len() {
            check_cancelled(cancelled)?;
            let line_end = memchr(b'\n', &mapped[line_start..])
                .map(|offset| line_start + offset)
                .unwrap_or(mapped.len());
            scanned_lines += 1;
            let raw = &mapped[line_start..line_end];
            if raw.len() > MAX_RECORD_BYTES {
                skipped_oversized_records += 1;
            } else if prompt_candidate(raw) {
                if let Ok(item) = serde_json::from_slice::<Value>(raw) {
                    if let Some((text, protocol)) = prompt_text_from_item(&item) {
                        let normalized_text = text.trim().to_string();
                        if !normalized_text.is_empty() {
                            let timestamp = item
                                .get("timestamp")
                                .and_then(Value::as_str)
                                .map(str::to_string);
                            let classification = classify_prompt(&normalized_text);
                            let canonical_text = if classification.has_pure_text {
                                classification.pure_text.clone()
                            } else {
                                normalized_text.clone()
                            };
                            if !is_cross_protocol_duplicate(
                                &canonical_text,
                                timestamp.as_deref(),
                                &protocol,
                                scanned_lines,
                                &recent,
                            ) {
                                let ordinal = prompts.len() + 1;
                                if classification.has_pure_text {
                                    pure_prompt_count += 1;
                                }
                                if classification.visible_by_default {
                                    visible_prompt_count += 1;
                                }
                                *source_counts
                                    .entry(classification.source_type.clone())
                                    .or_insert(0) += 1;
                                prompts.push(PromptRecord {
                                    index: ordinal,
                                    line_number: scanned_lines,
                                    byte_offset: line_start,
                                    ordinal_exact: true,
                                    timestamp: timestamp.clone(),
                                    text: normalized_text.clone(),
                                    character_count: normalized_text.chars().count(),
                                    protocol: protocol.clone(),
                                    source_type: classification.source_type,
                                    source_label: classification.source_label,
                                    visible_by_default: classification.visible_by_default,
                                    pure_character_count: classification.pure_text.chars().count(),
                                    has_pure_text: classification.has_pure_text,
                                    pure_text: classification.pure_text,
                                });
                                recent.push_back((
                                    canonical_text,
                                    timestamp,
                                    protocol,
                                    scanned_lines,
                                ));
                                while recent.len() > 8 {
                                    recent.pop_front();
                                }
                            }
                        }
                    }
                }
            }
            line_start = if line_end < mapped.len() {
                line_end + 1
            } else {
                mapped.len()
            };
        }
    }

    let version = format!("{file_size:x}-{modified_ns:x}");
    Ok(PromptSnapshot {
        file_size,
        modified_ns,
        prefix_len,
        prefix_hash,
        tail_hash,
        prompts: Arc::new(prompts),
        source_counts: Arc::new(source_counts),
        pure_prompt_count,
        visible_prompt_count,
        scanned_lines,
        skipped_oversized_records,
        recent: Arc::new(recent),
        version,
        elapsed_ms: started.elapsed().as_millis(),
        incremental: false,
    })
}

fn extend_snapshot(
    path: &Path,
    prior: &PromptSnapshot,
    file_size: u64,
    modified_ns: u128,
    cancelled: &AtomicBool,
) -> Result<Option<PromptSnapshot>, String> {
    check_cancelled(cancelled)?;
    let started = Instant::now();
    let file = File::open(path).map_err(|error| {
        format!(
            "cannot open {} for prompt extension: {error}",
            path.display()
        )
    })?;
    let mapped = unsafe { MmapOptions::new().map(&file) }.map_err(|error| {
        format!(
            "cannot map {} for prompt extension: {error}",
            path.display()
        )
    })?;
    let prior_size = usize::try_from(prior.file_size)
        .map_err(|_| "cached prompt snapshot size does not fit this platform".to_string())?;
    if mapped.len() != usize::try_from(file_size).unwrap_or(usize::MAX)
        || prior_size > mapped.len()
        || (prior_size > 0 && mapped[prior_size - 1] != b'\n')
    {
        return Ok(None);
    }
    if prior.prefix_len > mapped.len()
        || hash_prefix_from_bytes(&mapped[..prior.prefix_len]) != prior.prefix_hash
        || hash_prefix_from_bytes(&mapped[prior_size.saturating_sub(64 * 1024)..prior_size])
            != prior.tail_hash
    {
        return Ok(None);
    }

    let mut prompts = (*prior.prompts).clone();
    let mut source_counts = (*prior.source_counts).clone();
    let mut pure_prompt_count = prior.pure_prompt_count;
    let mut visible_prompt_count = prior.visible_prompt_count;
    let mut scanned_lines = prior.scanned_lines;
    let mut skipped_oversized_records = prior.skipped_oversized_records;
    let mut recent = (*prior.recent).clone();
    let mut line_start = prior_size;
    while line_start < mapped.len() {
        check_cancelled(cancelled)?;
        let line_end = memchr(b'\n', &mapped[line_start..])
            .map(|offset| line_start + offset)
            .unwrap_or(mapped.len());
        scanned_lines += 1;
        let raw = &mapped[line_start..line_end];
        if raw.len() > MAX_RECORD_BYTES {
            skipped_oversized_records += 1;
        } else if prompt_candidate(raw) {
            if let Ok(item) = serde_json::from_slice::<Value>(raw) {
                if let Some((text, protocol)) = prompt_text_from_item(&item) {
                    let normalized_text = text.trim().to_string();
                    if !normalized_text.is_empty() {
                        let timestamp = item
                            .get("timestamp")
                            .and_then(Value::as_str)
                            .map(str::to_string);
                        let classification = classify_prompt(&normalized_text);
                        let canonical_text = if classification.has_pure_text {
                            classification.pure_text.clone()
                        } else {
                            normalized_text.clone()
                        };
                        if !is_cross_protocol_duplicate(
                            &canonical_text,
                            timestamp.as_deref(),
                            &protocol,
                            scanned_lines,
                            &recent,
                        ) {
                            let ordinal = prompts.len() + 1;
                            if classification.has_pure_text {
                                pure_prompt_count += 1;
                            }
                            if classification.visible_by_default {
                                visible_prompt_count += 1;
                            }
                            *source_counts
                                .entry(classification.source_type.clone())
                                .or_insert(0) += 1;
                            prompts.push(PromptRecord {
                                index: ordinal,
                                line_number: scanned_lines,
                                byte_offset: line_start,
                                ordinal_exact: true,
                                timestamp: timestamp.clone(),
                                text: normalized_text.clone(),
                                character_count: normalized_text.chars().count(),
                                protocol: protocol.clone(),
                                source_type: classification.source_type,
                                source_label: classification.source_label,
                                visible_by_default: classification.visible_by_default,
                                pure_character_count: classification.pure_text.chars().count(),
                                has_pure_text: classification.has_pure_text,
                                pure_text: classification.pure_text,
                            });
                            recent.push_back((canonical_text, timestamp, protocol, scanned_lines));
                            while recent.len() > 8 {
                                recent.pop_front();
                            }
                        }
                    }
                }
            }
        }
        line_start = if line_end < mapped.len() {
            line_end + 1
        } else {
            mapped.len()
        };
    }

    let prefix_len = if prior.prefix_len == 0 {
        mapped.len().min(64 * 1024)
    } else {
        prior.prefix_len
    };
    Ok(Some(PromptSnapshot {
        file_size,
        modified_ns,
        prefix_len,
        prefix_hash: hash_prefix_from_bytes(&mapped[..prefix_len]),
        tail_hash: hash_prefix_from_bytes(&mapped[mapped.len().saturating_sub(64 * 1024)..]),
        prompts: Arc::new(prompts),
        source_counts: Arc::new(source_counts),
        pure_prompt_count,
        visible_prompt_count,
        scanned_lines,
        skipped_oversized_records,
        recent: Arc::new(recent),
        version: format!("{file_size:x}-{modified_ns:x}"),
        elapsed_ms: started.elapsed().as_millis(),
        incremental: true,
    }))
}

fn check_cancelled(cancelled: &AtomicBool) -> Result<(), String> {
    if cancelled.load(Ordering::Acquire) {
        Err(PROMPT_REQUEST_CANCELLED.to_string())
    } else {
        Ok(())
    }
}

fn lock_build_cancelable<'a>(
    build_lock: &'a Mutex<()>,
    cancelled: &AtomicBool,
) -> Result<MutexGuard<'a, ()>, String> {
    loop {
        check_cancelled(cancelled)?;
        match build_lock.try_lock() {
            Ok(guard) => return Ok(guard),
            Err(TryLockError::WouldBlock) => std::thread::sleep(Duration::from_millis(10)),
            Err(TryLockError::Poisoned(_)) => {
                return Err("prompt snapshot build lock is poisoned".to_string())
            }
        }
    }
}

fn hash_prefix_from_bytes(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

fn prompt_fingerprint(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

fn file_version(path: &Path) -> Result<(u64, u128), String> {
    let metadata = std::fs::metadata(path)
        .map_err(|error| format!("cannot stat {}: {error}", path.display()))?;
    let modified_ns = metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_nanos())
        .unwrap_or_default();
    Ok((metadata.len(), modified_ns))
}

fn prompt_candidate(raw: &[u8]) -> bool {
    let head = &raw[..raw.len().min(4096)];
    let tail = &raw[raw.len().saturating_sub(4096)..];
    let contains = |needle: &[u8]| {
        memmem::find(head, needle).is_some()
            || (tail.as_ptr() != head.as_ptr() && memmem::find(tail, needle).is_some())
    };
    contains(b"send_message_to_thread")
        || contains(b"user_message")
        || (contains(b"response_item") && contains(b"\"role\"") && contains(b"\"user\""))
}

pub(crate) fn incoming_delegation_text(item: &Value) -> Option<&str> {
    let payload = item.get("payload")?;
    if item.get("type")?.as_str()? != "response_item"
        || payload.get("type")?.as_str()? != "function_call_output"
        || payload.get("namespace")?.as_str()? != "codex_app"
        || payload.get("name")?.as_str()? != "send_message_to_thread"
    {
        return None;
    }
    let output = payload.get("output")?.as_str()?;
    let prefix = output.trim_start().as_bytes();
    let marker = b"<codex_delegation";
    if prefix.get(..marker.len())?.eq_ignore_ascii_case(marker)
        && (prefix.get(marker.len())? == &b'>' || prefix.get(marker.len())?.is_ascii_whitespace())
    {
        Some(output)
    } else {
        None
    }
}

fn prompt_text_from_item(item: &Value) -> Option<(String, String)> {
    if let Some(text) = incoming_delegation_text(item) {
        return Some((text.to_string(), "response_item".to_string()));
    }
    let item_type = item.get("type")?.as_str()?;
    let raw_payload = item.get("payload").unwrap_or(&Value::Null);
    let payload = raw_payload.as_object();
    match item_type {
        "user_message" => Some((text_from_value(raw_payload), "user_message".to_string())),
        "event_msg" if payload?.get("type").and_then(Value::as_str) == Some("user_message") => {
            let value = payload?
                .get("message")
                .or_else(|| payload?.get("text"))
                .or_else(|| payload?.get("content"))
                .unwrap_or(&Value::Null);
            Some((text_from_value(value), "event_msg".to_string()))
        }
        "response_item"
            if payload?.get("type").and_then(Value::as_str) == Some("message")
                && payload?.get("role").and_then(Value::as_str) == Some("user") =>
        {
            Some((
                text_from_value(payload?.get("content").unwrap_or(&Value::Null)),
                "response_item".to_string(),
            ))
        }
        _ => None,
    }
}

pub(crate) struct PromptClassification {
    pub(crate) source_type: String,
    pub(crate) source_label: String,
    pub(crate) visible_by_default: bool,
    pub(crate) pure_text: String,
    pub(crate) has_pure_text: bool,
}

pub(crate) fn classify_prompt(text: &str) -> PromptClassification {
    let prefix = text.trim_start().chars().take(5000).collect::<String>();
    let lower = prefix.to_lowercase();
    let (source_type, source_label, visible) = if is_subagent(&prefix, &lower) {
        ("subagent", "子 agent", false)
    } else if is_automation(&lower) {
        ("automation", "自动化任务", false)
    } else if lower.starts_with("<codex_delegation") {
        ("delegation", "线程转发", false)
    } else if lower.starts_with("<codex_internal_context") || lower.starts_with("<goal_context") {
        ("goal", "续跑目标上下文", false)
    } else if is_internal(&prefix, &lower) {
        (
            "internal",
            if lower.starts_with("<recommended_plugins>") {
                "推荐插件上下文"
            } else if lower.starts_with("<skills_instructions>")
                || lower.starts_with("<apps_instructions>")
                || lower.starts_with("<plugins_instructions>")
                || lower.starts_with("<collaboration_mode>")
            {
                "运行时上下文"
            } else {
                "内部上下文"
            },
            false,
        )
    } else if prefix.starts_with("# In app browser:")
        || lower.starts_with("<in-app-browser-context>")
        || lower.starts_with("<in-app-browser-context ") {
        ("browser", "浏览器上下文", true)
    } else if prefix.starts_with("# Files mentioned by the user:") {
        ("attachment", "附件上下文", true)
    } else {
        ("user", "用户输入", true)
    };
    let pure_text = if visible {
        pure_user_text(text)
    } else {
        String::new()
    };
    PromptClassification {
        source_type: source_type.to_string(),
        source_label: source_label.to_string(),
        visible_by_default: visible,
        has_pure_text: !pure_text.is_empty(),
        pure_text,
    }
}

fn is_subagent(prefix: &str, lower: &str) -> bool {
    prefix.starts_with("<subagent_notification>")
        || (prefix.contains("\"agent_path\"")
            && prefix.contains("\"status\"")
            && lower.contains("subagent"))
}

fn is_automation(lower: &str) -> bool {
    lower.starts_with("<heartbeat>")
        || lower.starts_with("<automation>")
        || lower.starts_with("<scheduled_task>")
        || lower.contains("<automation_id>")
        || (lower.contains("<current_time_iso>") && lower.contains("<instructions>"))
}

fn is_internal(prefix: &str, lower: &str) -> bool {
    lower.starts_with("<recommended_plugins>")
        || lower.starts_with("<skills_instructions>")
        || lower.starts_with("<apps_instructions>")
        || lower.starts_with("<plugins_instructions>")
        || lower.starts_with("<collaboration_mode>")
        || prefix.starts_with("# AGENTS.md instructions")
        || prefix.starts_with("<environment_context>")
        || prefix.starts_with("<turn_aborted>")
        || prefix.starts_with("<user_interruption>")
        || prefix.starts_with("<permissions instructions>")
}

fn pure_user_text(text: &str) -> String {
    static IMAGE_BLOCKS: OnceLock<Regex> = OnceLock::new();
    static MARKDOWN_IMAGES: OnceLock<Regex> = OnceLock::new();
    static REQUEST_MARKER: OnceLock<Regex> = OnceLock::new();
    static BROWSER_CONTEXT: OnceLock<Regex> = OnceLock::new();
    let image_blocks = IMAGE_BLOCKS
        .get_or_init(|| Regex::new(r"(?is)\n?<image\b.*?</image>\s*").expect("valid image regex"));
    let markdown_images = MARKDOWN_IMAGES.get_or_init(|| {
        Regex::new(r"(?m)\n?!\[[^\]]*\]\([^)]*\)\s*").expect("valid markdown image regex")
    });
    let request_marker = REQUEST_MARKER.get_or_init(|| {
        Regex::new(r"(?im)^##\s*My request(?:\s+for\s+Codex)?:\s*$").expect("valid request regex")
    });
    let cleaned = markdown_images
        .replace_all(&image_blocks.replace_all(text, "\n"), "\n")
        .trim()
        .to_string();
    let cleaned = BROWSER_CONTEXT.get_or_init(|| {
        Regex::new(r"(?is)^<in-app-browser-context(?:\s[^>]*)?>.*?</in-app-browser-context>\s*").expect("valid browser context regex")
    }).replace(&cleaned, "").to_string();
    if let Some(marker) = request_marker.find(&cleaned) {
        return markdown_images
            .replace_all(
                &image_blocks.replace_all(&cleaned[marker.end()..], "\n"),
                "\n",
            )
            .trim()
            .to_string();
    }
    let prefix = cleaned.trim_start();
    if prefix.starts_with("# In app browser:")
        || prefix.starts_with("# Files mentioned by the user:")
    {
        String::new()
    } else {
        cleaned
    }
}

fn is_cross_protocol_duplicate(
    text: &str,
    timestamp: Option<&str>,
    protocol: &str,
    line_number: usize,
    recent: &VecDeque<RecentPrompt>,
) -> bool {
    recent.iter().rev().any(
        |(prior_text, prior_timestamp, prior_protocol, prior_line)| {
            prior_protocol != protocol
                && prior_text == text
                && match (timestamp, prior_timestamp.as_deref()) {
                    (Some(current), Some(previous)) => {
                        timestamps_within_two_seconds(current, previous)
                    }
                    _ => line_number.abs_diff(*prior_line) <= 2,
                }
        },
    )
}

fn is_cross_protocol_fingerprint_duplicate(
    fingerprint: &str,
    timestamp: Option<&str>,
    protocol: &str,
    line_number: usize,
    recent: &[RecentPromptFingerprint],
) -> bool {
    recent.iter().rev().any(
        |(prior_fingerprint, prior_timestamp, prior_protocol, prior_line)| {
            prior_protocol != protocol
                && prior_fingerprint == fingerprint
                && match (timestamp, prior_timestamp.as_deref()) {
                    (Some(current), Some(previous)) => {
                        timestamps_within_two_seconds(current, previous)
                    }
                    _ => line_number.abs_diff(*prior_line) <= 2,
                }
        },
    )
}

fn timestamps_within_two_seconds(left: &str, right: &str) -> bool {
    match (timestamp_millis(left), timestamp_millis(right)) {
        (Some(left), Some(right)) => left.abs_diff(right) <= 2_000,
        _ => left == right,
    }
}

fn timestamp_millis(value: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|timestamp| timestamp.timestamp_millis())
}

fn prompt_matches(prompt: &PromptRecord, scope: &str, source_type: &str, search: &str) -> bool {
    let scope_matches = match scope {
        "pure" => prompt.has_pure_text,
        "visible" => prompt.visible_by_default,
        "with_agents" => prompt.visible_by_default || prompt.source_type == "subagent",
        "automation" | "heartbeat" => prompt.source_type == "automation",
        "delegation" => prompt.source_type == "delegation",
        "all" => true,
        _ => false,
    };
    let source_matches = source_type.is_empty() || prompt.source_type == source_type;
    let searchable = if scope == "pure" {
        &prompt.pure_text
    } else {
        &prompt.text
    };
    scope_matches
        && source_matches
        && (search.is_empty() || searchable.to_lowercase().contains(search))
}

fn query_signature(
    scope: &str,
    search: &str,
    source_type: &str,
    order: &str,
    version: &str,
) -> String {
    let mut hasher = Sha256::new();
    for value in [scope, search, source_type, order, version] {
        hasher.update((value.len() as u64).to_le_bytes());
        hasher.update(value.as_bytes());
    }
    format!("{:x}", hasher.finalize())
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct IndexCursor {
    query: String,
    anchor_byte: usize,
    anchor_fingerprint: String,
    source_size: u64,
}

fn prompt_cursor_fingerprint(prompt: &PromptRecord) -> String {
    let mut hasher = Sha256::new();
    hasher.update(prompt.byte_offset.to_le_bytes());
    hasher.update(prompt.protocol.as_bytes());
    hasher.update(prompt.source_type.as_bytes());
    hasher.update(prompt.text.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn encode_cursor(prompt: &PromptRecord, signature: &str, source_size: u64) -> String {
    URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(&IndexCursor {
            query: signature.to_string(),
            anchor_byte: prompt.byte_offset,
            anchor_fingerprint: prompt_cursor_fingerprint(prompt),
            source_size,
        })
        .expect("native prompt cursor is serializable"),
    )
}

fn decode_cursor(
    cursor: Option<&str>,
    signature: &str,
    current_size: u64,
) -> Result<Option<IndexCursor>, String> {
    let Some(cursor) = cursor.filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let bytes = URL_SAFE_NO_PAD
        .decode(cursor)
        .map_err(|_| "invalid native prompt cursor".to_string())?;
    let payload: IndexCursor =
        serde_json::from_slice(&bytes).map_err(|_| "invalid native prompt cursor".to_string())?;
    if payload.query != signature {
        return Err("native prompt cursor does not match the current filters".to_string());
    }
    if current_size < payload.source_size {
        return Err("native prompt cursor is stale because the rollout was truncated".to_string());
    }
    Ok(Some(payload))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn official_incoming_delegation_is_a_prompt_not_an_outgoing_receipt() {
        let text = "<codex_delegation>\n<input>handover complete</input>\n</codex_delegation>";
        let mut record = json!({"type":"response_item","payload":{"type":"function_call_output","namespace":"codex_app","name":"send_message_to_thread","output":text}});
        assert!(prompt_candidate(
            serde_json::to_string(&record).unwrap().as_bytes()
        ));
        assert_eq!(prompt_text_from_item(&record).unwrap().0, text);
        assert_eq!(classify_prompt(text).source_type, "delegation");
        assert!(!classify_prompt(text).has_pure_text);
        for output in [
            "receipt",
            "quoted <codex_delegation>",
            "中文中文中文中文中文中文",
        ] {
            record["payload"]["output"] = json!(output);
            assert!(incoming_delegation_text(&record).is_none());
        }
        record["payload"]["output"] = json!(text);
        record["payload"]["namespace"] = json!("other");
        assert!(incoming_delegation_text(&record).is_none());
    }

    #[test]
    fn prompt_cursor_errors_have_a_stable_machine_code() {
        assert_eq!(
            prompt_request_error_code("native prompt cursor does not match the current filters"),
            "prompt_cursor_invalid"
        );
        assert_eq!(
            prompt_request_error_code(
                "recent prompt cursor is stale or does not match the current filters"
            ),
            "prompt_cursor_invalid"
        );
        assert_eq!(
            prompt_request_error_code("unsupported prompt scope"),
            "prompt_request_invalid"
        );
        assert_eq!(
            prompt_request_error_code(PROMPT_REQUEST_CANCELLED),
            "prompt_request_cancelled"
        );
    }

    #[test]
    fn prompt_request_cancellation_is_scoped_and_releases_the_registry_entry() {
        let store = PromptStore::default();
        let registration = store
            .begin_request("thread-a", Some("request-a"))
            .expect("register request");
        assert!(!store.cancel_request("thread-b", "request-a"));
        assert!(store.cancel_request("thread-a", "request-a"));
        assert_eq!(
            check_cancelled(&registration.cancelled).unwrap_err(),
            PROMPT_REQUEST_CANCELLED
        );
        store.finish_request(&registration);
        assert!(!store.cancel_request("thread-a", "request-a"));
    }

    #[test]
    fn cancelled_prompt_scan_does_not_cache_a_partial_snapshot() {
        let (_file, locator) = fixture(&[
            json!({"type":"user_message","payload":{"text":"first"}}),
            json!({"type":"user_message","payload":{"text":"second"}}),
        ]);
        let store = PromptStore::default();
        let cancelled = AtomicBool::new(true);
        let error = match store.snapshot_cancelable(&locator, &cancelled) {
            Ok(_) => panic!("cancelled scan must stop"),
            Err(error) => error,
        };
        assert_eq!(error, PROMPT_REQUEST_CANCELLED);
        assert!(store.snapshots.lock().unwrap().is_empty());
    }
    use std::io::{Seek, SeekFrom, Write};
    use tempfile::NamedTempFile;

    fn fixture(records: &[Value]) -> (NamedTempFile, ThreadLocator) {
        let mut file = NamedTempFile::new().expect("fixture");
        for record in records {
            writeln!(file, "{}", serde_json::to_string(record).unwrap()).unwrap();
        }
        let locator = ThreadLocator {
            id: "thread-prompts".to_string(),
            title: "Prompts".to_string(),
            rollout_path: file.path().to_path_buf(),
            rollout_paths: vec![file.path().to_path_buf()],
        };
        (file, locator)
    }

    fn query(order: &str, scope: &str, limit: usize) -> PromptPageQuery {
        PromptPageQuery {
            codex_home: None,
            cursor: None,
            limit: Some(limit),
            search: None,
            scope: Some(scope.to_string()),
            source_type: None,
            order: Some(order.to_string()),
            strategy: Some("index".to_string()),
            request_id: Some("request-1".to_string()),
            scan_budget_ms: Some(1_000),
        }
    }

    #[test]
    fn native_prompt_snapshot_deduplicates_protocol_mirrors_but_keeps_real_repeats() {
        let (_file, locator) = fixture(&[
            json!({"type":"user_message","timestamp":"2026-08-13T05:00:00.000Z","payload":{"text":"# Files mentioned by the user:\ncontext-a\n## My request:\nmirrored"}}),
            json!({"type":"response_item","timestamp":"2026-08-13T05:00:01.000Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"# Files mentioned by the user:\ncontext-b\n## My request:\nmirrored"}]}}),
            json!({"type":"response_item","timestamp":"2026-08-13T05:01:00.000Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"repeat"}]}}),
            json!({"type":"response_item","timestamp":"2026-08-13T05:02:00.000Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"repeat"}]}}),
        ]);
        let payload =
            read_prompt_page(&PromptStore::default(), &locator, &query("asc", "pure", 20)).unwrap();
        assert_eq!(payload["promptCount"], 3);
        assert_eq!(payload["matchCount"], 3);
        assert_eq!(payload["index"]["runtime"], "rust");
    }

    #[test]
    fn native_prompt_snapshot_extends_an_append_only_rollout() {
        let (mut file, locator) = fixture(&[
            json!({"type":"user_message","timestamp":"2026-08-13T05:00:00.000Z","payload":{"text":"first"}}),
        ]);
        let store = PromptStore::default();
        let first = store.snapshot(&locator).unwrap();
        assert_eq!(first.prompts.len(), 1);
        assert!(!first.incremental);

        writeln!(
            file,
            "{}",
            json!({"type":"user_message","timestamp":"2026-08-13T05:01:00.000Z","payload":{"text":"second"}})
        )
        .unwrap();
        file.flush().unwrap();

        let second = store.snapshot(&locator).unwrap();
        assert_eq!(second.prompts.len(), 2);
        assert!(second.incremental);
        let bytes = std::fs::read(file.path()).unwrap();
        assert_eq!(
            second.tail_hash,
            hash_prefix_from_bytes(&bytes[bytes.len().saturating_sub(64 * 1024)..])
        );
    }

    #[test]
    fn native_prompt_snapshot_pages_a_stable_newest_first_result() {
        let (mut file, locator) = fixture(&[
            json!({"type":"user_message","timestamp":"2026-08-13T05:00:00.000Z","payload":{"text":"oldest"}}),
            json!({"type":"user_message","timestamp":"2026-08-13T05:01:00.000Z","payload":{"text":"middle"}}),
            json!({"type":"user_message","timestamp":"2026-08-13T05:02:00.000Z","payload":{"text":"newest"}}),
        ]);
        let store = PromptStore::default();
        let first = read_prompt_page(&store, &locator, &query("desc", "pure", 2)).unwrap();
        assert_eq!(first["prompts"][0]["pureText"], "newest");
        assert_eq!(first["prompts"][1]["pureText"], "middle");
        assert_eq!(first["hasMore"], true);
        writeln!(
            file,
            "{}",
            json!({"type":"user_message","timestamp":"2026-08-13T05:03:00.000Z","payload":{"text":"appended while paging"}})
        )
        .unwrap();
        file.flush().unwrap();
        let mut second_query = query("desc", "pure", 2);
        second_query.cursor = first["nextCursor"].as_str().map(str::to_string);
        let second = read_prompt_page(&store, &locator, &second_query).unwrap();
        assert_eq!(second["prompts"][0]["pureText"], "oldest");
        assert_eq!(second["matchCountComplete"], true);
    }

    #[test]
    fn recent_prompt_budget_returns_partial_rows_and_resumes_without_losing_inputs() {
        let (mut file, locator) = fixture(&[
            json!({"type":"user_message","payload":{"text":"oldest"}}),
        ]);
        let filler = json!({"type":"response_item","payload":{"type":"function_call_output","output":"x".repeat(256 * 1024)}});
        for _ in 0..40 {
            writeln!(file, "{filler}").unwrap();
        }
        writeln!(file, "{}", json!({"type":"user_message","payload":{"text":"newest"}})).unwrap();
        file.flush().unwrap();
        let mut request = query("desc", "pure", 60);
        request.strategy = Some("recent".into());
        request.scan_budget_ms = Some(5_000);
        let store = PromptStore::default();
        let first = read_prompt_page(&store, &locator, &request).unwrap();
        assert_eq!(first["prompts"].as_array().unwrap().len(), 1);
        assert_eq!(first["prompts"][0]["pureText"], "newest");
        assert_eq!(first["hasMore"], true);
        assert!(first["index"]["scannedBytes"].as_u64().unwrap() < 9 * 1024 * 1024);
        request.cursor = first["nextCursor"].as_str().map(str::to_string);
        let second = read_prompt_page(&store, &locator, &request).unwrap();
        assert_eq!(second["prompts"].as_array().unwrap().len(), 1);
        assert_eq!(second["prompts"][0]["pureText"], "oldest");
        assert_eq!(second["hasMore"], false);
        assert_eq!(second["matchCount"], 2);
    }

    #[test]
    fn recent_prompt_strategy_reads_the_true_tail_without_building_a_snapshot() {
        let (mut file, locator) = fixture(&[
            json!({"type":"user_message","timestamp":"2026-08-13T05:00:00.000Z","payload":{"text":"oldest"}}),
            json!({"type":"user_message","timestamp":"2026-08-13T05:01:00.000Z","payload":{"text":"middle"}}),
            json!({"type":"user_message","timestamp":"2026-08-13T05:02:00.000Z","payload":{"text":"newest"}}),
        ]);
        let store = PromptStore::default();
        let mut first_query = query("desc", "pure", 2);
        first_query.strategy = Some("recent".to_string());
        let first = read_prompt_page(&store, &locator, &first_query).unwrap();
        assert_eq!(first["prompts"][0]["pureText"], "newest");
        assert_eq!(first["prompts"][1]["pureText"], "middle");
        assert_eq!(first["index"]["strategy"], "recent");
        assert_eq!(first["prompts"][0]["ordinalExact"], false);
        assert!(store.snapshots.lock().unwrap().is_empty());

        writeln!(
            file,
            "{}",
            json!({"type":"user_message","timestamp":"2026-08-13T05:03:00.000Z","payload":{"text":"appended while paging"}})
        )
        .unwrap();
        file.flush().unwrap();
        let mut second_query = first_query;
        second_query.cursor = first["nextCursor"].as_str().map(str::to_string);
        let second = read_prompt_page(&store, &locator, &second_query).unwrap();
        assert_eq!(second["prompts"][0]["pureText"], "oldest");
        assert_eq!(second["hasMore"], false);
        assert_eq!(second["matchCountComplete"], true);
    }

    #[test]
    fn multi_segment_recent_cursor_survives_append_to_active_segment() {
        let (older_file, _) = fixture(&[
            json!({"type":"user_message","timestamp":"2026-08-13T04:00:00.000Z","payload":{"text":"older segment"}}),
        ]);
        let (mut active_file, _) = fixture(&[
            json!({"type":"user_message","timestamp":"2026-08-13T05:00:00.000Z","payload":{"text":"active older"}}),
            json!({"type":"user_message","timestamp":"2026-08-13T05:01:00.000Z","payload":{"text":"active newest"}}),
        ]);
        let locator = ThreadLocator {
            id: "thread-multi-prompts".to_string(),
            title: "Multi prompts".to_string(),
            rollout_path: active_file.path().to_path_buf(),
            rollout_paths: vec![
                older_file.path().to_path_buf(),
                active_file.path().to_path_buf(),
            ],
        };
        let mut first_query = query("desc", "pure", 1);
        first_query.strategy = Some("recent".to_string());
        let first = read_prompt_page(&PromptStore::default(), &locator, &first_query).unwrap();
        assert_eq!(first["prompts"][0]["pureText"], "active newest");
        assert_eq!(first["hasMore"], true);

        writeln!(
            active_file,
            "{}",
            json!({"type":"response_item","timestamp":"2026-08-13T05:02:00.000Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"append while paging"}]}})
        )
        .unwrap();
        active_file.flush().unwrap();

        let mut second_query = first_query;
        second_query.cursor = first["nextCursor"].as_str().map(str::to_string);
        let second = read_prompt_page(&PromptStore::default(), &locator, &second_query).unwrap();
        assert_eq!(second["prompts"][0]["pureText"], "active older");
        assert_eq!(second["hasMore"], true);
    }

    #[test]
    fn protocol_mirror_timestamps_use_real_rfc3339_calendar_math() {
        assert!(timestamps_within_two_seconds(
            "2026-01-31T23:59:59.500Z",
            "2026-02-01T00:00:00.500Z"
        ));
        assert!(timestamps_within_two_seconds(
            "2026-08-13T08:00:00+08:00",
            "2026-08-13T00:00:01Z"
        ));
    }

    #[test]
    fn recent_prompt_strategy_reads_large_rollouts_without_index_fallback() {
        let mut file = NamedTempFile::new().expect("large sparse fixture");
        file.as_file_mut()
            .set_len(64 * 1024 * 1024 + 1)
            .unwrap();
        file.as_file_mut().seek(SeekFrom::End(0)).unwrap();
        writeln!(file).unwrap();
        writeln!(
            file,
            "{}",
            json!({"type":"user_message","timestamp":"2026-08-13T05:02:00.000Z","payload":{"text":"large rollout tail"}})
        )
        .unwrap();
        file.flush().unwrap();
        let locator = ThreadLocator {
            id: "thread-large-prompts".to_string(),
            title: "Large prompts".to_string(),
            rollout_path: file.path().to_path_buf(),
            rollout_paths: vec![file.path().to_path_buf()],
        };
        let mut recent_query = query("desc", "pure", 1);
        recent_query.strategy = Some("recent".to_string());
        let page = read_prompt_page(&PromptStore::default(), &locator, &recent_query).unwrap();
        assert_eq!(page["index"]["strategy"], "recent");
        assert_eq!(page["prompts"][0]["pureText"], "large rollout tail");
        assert_eq!(page["index"]["scannedBytes"].as_u64().unwrap() < 4096, true);
    }

    #[test]
    fn recent_prompt_cursor_keeps_only_bounded_prompt_fingerprints() {
        let large_prompt = "x".repeat(1024 * 1024);
        let large_record = json!({"type":"user_message","timestamp":"2026-08-13T05:01:00.000Z","payload":{"text":large_prompt}});
        let serialized = serde_json::to_vec(&large_record).unwrap();
        assert!(prompt_candidate(&serialized));
        let (parsed_text, _) = prompt_text_from_item(&large_record).expect("large prompt text");
        assert_eq!(parsed_text.len(), 1024 * 1024);
        assert!(classify_prompt(&parsed_text).has_pure_text);
        let (_file, locator) = fixture(&[
            json!({"type":"user_message","timestamp":"2026-08-13T05:00:00.000Z","payload":{"text":"older"}}),
            large_record,
        ]);
        let mut recent_query = query("desc", "pure", 1);
        recent_query.strategy = Some("recent".to_string());
        let page = read_prompt_page(&PromptStore::default(), &locator, &recent_query).unwrap();
        let cursor = page["nextCursor"]
            .as_str()
            .unwrap_or_else(|| panic!("next cursor missing: {page}"));
        assert!(cursor.len() < 2_048);
    }
}
