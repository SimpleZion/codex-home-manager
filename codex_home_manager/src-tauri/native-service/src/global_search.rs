use std::{
    collections::{HashMap, HashSet},
    fs::{self, File},
    io::{BufRead, BufReader, Read},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};

use axum::{
    extract::{Path as AxumPath, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::{
    prompts::prompt_index::{
        complete_global_timeline_index, global_timeline_cursor_is_direct,
        read_completed_global_timeline_page, read_direct_global_timeline_page,
        read_global_timeline_page, GlobalTimelineSearchQuery, GlobalTimelineSegment,
    },
    AppState,
};

const INVENTORY_TTL: Duration = Duration::from_secs(30);
const MAX_SESSION_META_BYTES: u64 = 8 * 1024 * 1024;
const MAX_REQUEST_ID_BYTES: usize = 128;
const SEARCH_CANCELLED: &str = "prompt request was cancelled";

#[derive(Debug, Deserialize)]
pub struct GlobalSearchQuery {
    codex_home: Option<String>,
    cursor: Option<String>,
    limit: Option<usize>,
    kind: Option<String>,
    search: Option<String>,
    #[serde(rename = "sourceFilter", alias = "source_filter")]
    source_filter: Option<String>,
    #[serde(rename = "threadKind", alias = "thread_kind")]
    thread_kind: Option<String>,
    #[serde(rename = "filterMode", alias = "filter_mode")]
    filter_mode: Option<String>,
    #[serde(rename = "projectPath", alias = "project_path")]
    project_path: Option<String>,
    #[serde(rename = "sidebarLimit", alias = "sidebar_limit")]
    sidebar_limit: Option<usize>,
    #[serde(rename = "catalogVersion", alias = "catalog_version")]
    catalog_version: Option<String>,
    #[serde(rename = "scanBudgetMs", alias = "scan_budget_ms")]
    scan_budget_ms: Option<u64>,
    #[serde(rename = "requestId", alias = "request_id")]
    request_id: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ThreadTimelineSearchQuery {
    codex_home: Option<String>,
    cursor: Option<String>,
    limit: Option<usize>,
    kind: Option<String>,
    search: Option<String>,
    #[serde(rename = "sourceFilter", alias = "source_filter")]
    source_filter: Option<String>,
    #[serde(rename = "scanBudgetMs", alias = "scan_budget_ms")]
    scan_budget_ms: Option<u64>,
    #[serde(rename = "requestId", alias = "request_id")]
    request_id: Option<String>,
}

#[derive(Clone)]
struct InventorySnapshot {
    built_at: Instant,
    paths_by_thread: Arc<HashMap<String, Vec<PathBuf>>>,
}

struct RequestRegistration {
    id: String,
    cancelled: Arc<AtomicBool>,
}

#[derive(Default)]
pub struct GlobalSearchStore {
    inventories: Mutex<HashMap<String, InventorySnapshot>>,
    requests: Mutex<HashMap<String, Arc<AtomicBool>>>,
    indexing: Mutex<HashSet<String>>,
}

impl GlobalSearchStore {
    fn begin_request(&self, requested: Option<&str>) -> Result<RequestRegistration, String> {
        let id = requested
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| Uuid::new_v4().to_string());
        if id.len() > MAX_REQUEST_ID_BYTES
            || !id
                .bytes()
                .all(|value| value.is_ascii_alphanumeric() || matches!(value, b'-' | b'_' | b'.'))
        {
            return Err("invalid global search request id".to_string());
        }
        let cancelled = Arc::new(AtomicBool::new(false));
        let mut requests = self
            .requests
            .lock()
            .map_err(|_| "global search request registry is unavailable".to_string())?;
        if requests.contains_key(&id) {
            return Err("global search request id is already active".to_string());
        }
        requests.insert(id.clone(), cancelled.clone());
        Ok(RequestRegistration { id, cancelled })
    }

    fn finish_request(&self, registration: &RequestRegistration) {
        if let Ok(mut requests) = self.requests.lock() {
            if requests
                .get(&registration.id)
                .is_some_and(|value| Arc::ptr_eq(value, &registration.cancelled))
            {
                requests.remove(&registration.id);
            }
        }
    }

    fn cancel_request(&self, request_id: &str) -> bool {
        let request = self
            .requests
            .lock()
            .ok()
            .and_then(|requests| requests.get(request_id).cloned());
        if let Some(request) = request {
            request.store(true, Ordering::Release);
            true
        } else {
            false
        }
    }

    fn begin_indexing(&self, codex_home: &Path) -> bool {
        self.indexing
            .lock()
            .map(|mut indexing| indexing.insert(comparable_path(codex_home)))
            .unwrap_or(false)
    }

    fn finish_indexing(&self, codex_home: &Path) {
        if let Ok(mut indexing) = self.indexing.lock() {
            indexing.remove(&comparable_path(codex_home));
        }
    }

    fn inventory(
        &self,
        codex_home: &Path,
        cancelled: &AtomicBool,
    ) -> Result<Arc<HashMap<String, Vec<PathBuf>>>, String> {
        let key = comparable_path(codex_home);
        if let Some(snapshot) = self
            .inventories
            .lock()
            .map_err(|_| "global search inventory cache is unavailable".to_string())?
            .get(&key)
            .filter(|snapshot| snapshot.built_at.elapsed() < INVENTORY_TTL)
            .cloned()
        {
            return Ok(snapshot.paths_by_thread);
        }
        let paths_by_thread = Arc::new(build_inventory(codex_home, cancelled)?);
        self.inventories
            .lock()
            .map_err(|_| "global search inventory cache is unavailable".to_string())?
            .insert(
                key,
                InventorySnapshot {
                    built_at: Instant::now(),
                    paths_by_thread: paths_by_thread.clone(),
                },
            );
        Ok(paths_by_thread)
    }
}

pub async fn global_thread_content_search_page(
    State(state): State<AppState>,
    Query(query): Query<GlobalSearchQuery>,
) -> Response {
    let registration = match state
        .global_search
        .begin_request(query.request_id.as_deref())
    {
        Ok(registration) => registration,
        Err(error) => return global_error_response(&error),
    };
    let request_id = registration.id.clone();
    let cancelled = registration.cancelled.clone();
    let catalog_store = state.catalog.clone();
    let search_store = state.global_search.clone();
    let result = tokio::task::spawn_blocking(move || {
        validate_query(&query)?;
        let sidebar_limit = query.sidebar_limit.unwrap_or(50);
        let catalog_version = query
            .catalog_version
            .as_deref()
            .filter(|value| !value.trim().is_empty());
        let catalog = catalog_store.global_search_catalog(
            query.codex_home.as_deref(),
            sidebar_limit,
            catalog_version,
            query.cursor.is_some(),
        )?;
        check_cancelled(&cancelled)?;
        let inventory = search_store.inventory(&catalog.codex_home, &cancelled)?;
        let thread_kind = normalized_option(query.thread_kind.as_deref(), "all");
        let filter_mode = normalized_option(query.filter_mode.as_deref(), "all");
        let project_path = query.project_path.as_deref().unwrap_or("all");
        let mut segments = Vec::new();
        let mut seen_threads = HashSet::new();
        let mut seen_paths = HashSet::new();
        let mut scoped_threads = 0usize;
        for thread in &catalog.threads {
            check_cancelled(&cancelled)?;
            let thread_id = text(thread, "id");
            if thread_id.is_empty() || !seen_threads.insert(thread_id.clone()) {
                continue;
            }
            let actual_kind = text(thread, "threadKind");
            let archived = boolean(thread, "archived");
            if (thread_kind != "all" && actual_kind != thread_kind)
                || (filter_mode == "archived" && !archived)
                || (project_path != "all" && text(thread, "projectPath") != project_path)
            {
                continue;
            }
            scoped_threads += 1;
            let mut paths = inventory.get(&thread_id).cloned().unwrap_or_default();
            let active_path = text(thread, "rolloutPath");
            if !active_path.is_empty() {
                paths.push(PathBuf::from(active_path));
            }
            paths.sort_by_key(|path| comparable_path(path));
            paths.dedup_by(|left, right| comparable_path(left) == comparable_path(right));
            for path in paths {
                if !path.is_file() || !seen_paths.insert(comparable_path(&path)) {
                    continue;
                }
                segments.push(GlobalTimelineSegment {
                    path,
                    thread_id: thread_id.clone(),
                    thread_title: text(thread, "title"),
                    project_path: text(thread, "projectPath"),
                    project_label: text(thread, "projectLabel"),
                    thread_kind: actual_kind.clone(),
                    archived,
                    updated_at_ms: integer(thread, "updatedAtMs"),
                });
            }
        }
        segments.sort_by(|left, right| {
            right
                .updated_at_ms
                .cmp(&left.updated_at_ms)
                .then_with(|| comparable_path(&left.path).cmp(&comparable_path(&right.path)))
        });
        let index_query = GlobalTimelineSearchQuery {
            cursor: query.cursor.clone(),
            limit: query.limit.unwrap_or(60),
            kind: normalized_option(query.kind.as_deref(), "conversation"),
            search: query.search.clone().unwrap_or_default(),
            source_filter: normalized_option(query.source_filter.as_deref(), "all"),
            scan_budget_ms: query.scan_budget_ms.unwrap_or(450),
        };
        let cursor_is_direct = global_timeline_cursor_is_direct(index_query.cursor.as_deref());
        let payload = if cursor_is_direct {
            read_direct_global_timeline_page(&segments, &index_query, cancelled.clone())?
        } else if index_query.cursor.is_some() {
            read_global_timeline_page(
                &catalog.codex_home,
                &segments,
                &index_query,
                cancelled.clone(),
            )?
        } else if let Some(payload) = read_completed_global_timeline_page(
            &catalog.codex_home,
            &segments,
            &index_query,
            cancelled.clone(),
        )? {
            payload
        } else {
            let payload =
                read_direct_global_timeline_page(&segments, &index_query, cancelled.clone())?;
            if search_store.begin_indexing(&catalog.codex_home) {
                let background_store = search_store.clone();
                let background_home = catalog.codex_home.clone();
                let background_segments = segments.clone();
                let background_query = index_query.clone();
                std::thread::spawn(move || {
                    let _ = complete_global_timeline_index(
                        &background_home,
                        &background_segments,
                        &background_query,
                    );
                    background_store.finish_indexing(&background_home);
                });
            }
            payload
        };
        let mut result = payload.as_object().cloned().unwrap_or_default();
        result.insert("catalogVersion".to_string(), json!(catalog.catalog_version));
        result.insert("requestId".to_string(), json!(request_id));
        result.insert(
            "scope".to_string(),
            json!({
                "threadCount": scoped_threads,
                "segmentCount": segments.len(),
                "threadKind": thread_kind,
                "filterMode": filter_mode,
                "projectPath": project_path,
            }),
        );
        Ok::<Value, String>(Value::Object(result))
    })
    .await;
    state.global_search.finish_request(&registration);
    match result {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => global_error_response(&error),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"detail": format!("global search worker failed: {error}")})),
        )
            .into_response(),
    }
}

pub async fn cancel_global_thread_content_search_request(
    State(state): State<AppState>,
    AxumPath(request_id): AxumPath<String>,
) -> Response {
    let cancelled = state.global_search.cancel_request(&request_id);
    Json(json!({
        "requestId": request_id,
        "cancelled": cancelled,
        "runtime": "rust",
    }))
    .into_response()
}

pub async fn thread_timeline_search_page(
    State(state): State<AppState>,
    AxumPath(thread_id): AxumPath<String>,
    Query(query): Query<ThreadTimelineSearchQuery>,
) -> Response {
    let registration = match state
        .global_search
        .begin_request(query.request_id.as_deref())
    {
        Ok(registration) => registration,
        Err(error) => return global_error_response(&error),
    };
    let request_id = registration.id.clone();
    let cancelled = registration.cancelled.clone();
    let catalog_store = state.catalog.clone();
    let search_store = state.global_search.clone();
    let result = tokio::task::spawn_blocking(move || {
        if !(1..=200).contains(&query.limit.unwrap_or(80)) {
            return Err("timeline search limit must be between 1 and 200".to_string());
        }
        if !(1..=5_000).contains(&query.scan_budget_ms.unwrap_or(450)) {
            return Err("scanBudgetMs must be between 1 and 5000".to_string());
        }
        validate_option(
            query.kind.as_deref(),
            "conversation",
            &["conversation", "user", "commentary", "assistant", "assistant_unclassified"],
        )?;
        validate_option(
            query.source_filter.as_deref(),
            "all",
            &[
                "all",
                "pure",
                "focused",
                "with_agents",
                "automation",
                "delegation",
            ],
        )?;
        let search = query.search.as_deref().unwrap_or("").trim();
        if search.chars().count() < 1 || search.chars().count() > 20_000 {
            return Err("timeline search requires 1 to 20000 characters".to_string());
        }
        let catalog = catalog_store.global_search_catalog(
            query.codex_home.as_deref(),
            50,
            None,
            // This cursor belongs to timeline content, not the thread catalog.
            false,
        )?;
        let thread = catalog
            .threads
            .iter()
            .find(|item| text(item, "id") == thread_id)
            .ok_or_else(|| format!("thread not found: {thread_id}"))?;
        let inventory = search_store.inventory(&catalog.codex_home, &cancelled)?;
        let mut paths = inventory.get(&thread_id).cloned().unwrap_or_default();
        let active_path = text(thread, "rolloutPath");
        if !active_path.is_empty() {
            paths.push(PathBuf::from(active_path));
        }
        paths.sort_by_key(|path| comparable_path(path));
        paths.dedup_by(|left, right| comparable_path(left) == comparable_path(right));
        let segments = paths
            .into_iter()
            .filter(|path| path.is_file())
            .map(|path| GlobalTimelineSegment {
                path,
                thread_id: thread_id.clone(),
                thread_title: text(thread, "title"),
                project_path: text(thread, "projectPath"),
                project_label: text(thread, "projectLabel"),
                thread_kind: text(thread, "threadKind"),
                archived: boolean(thread, "archived"),
                updated_at_ms: integer(thread, "updatedAtMs"),
            })
            .collect::<Vec<_>>();
        if segments.is_empty() {
            return Err(format!("rollout file is missing for thread: {thread_id}"));
        }
        let index_query = GlobalTimelineSearchQuery {
            cursor: query.cursor,
            limit: query.limit.unwrap_or(80),
            kind: normalized_option(query.kind.as_deref(), "conversation"),
            search: query.search.unwrap_or_default(),
            source_filter: normalized_option(query.source_filter.as_deref(), "all"),
            scan_budget_ms: query.scan_budget_ms.unwrap_or(450),
        };
        let payload = read_global_timeline_page(&catalog.codex_home, &segments, &index_query, cancelled)?;
        let mut result = payload.as_object().cloned().unwrap_or_default();
        result.insert("threadId".to_string(), json!(thread_id));
        result.insert("requestId".to_string(), json!(request_id));
        if let Some(index) = result.get_mut("index").and_then(Value::as_object_mut) {
            index.insert("fileSize".to_string(), index.get("sourceBytes").cloned().unwrap_or(json!(0)));
            index.insert("scannedBytes".to_string(), index.get("indexedBytes").cloned().unwrap_or(json!(0)));
            index.insert("scannedLines".to_string(), json!(0));
        }
        Ok::<Value, String>(Value::Object(result))
    })
    .await;
    state.global_search.finish_request(&registration);
    match result {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => global_error_response(&error),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"detail": format!("timeline search worker failed: {error}")})),
        )
            .into_response(),
    }
}

pub async fn cancel_thread_timeline_search_request(
    State(state): State<AppState>,
    AxumPath((thread_id, request_id)): AxumPath<(String, String)>,
) -> Response {
    Json(json!({
        "threadId": thread_id,
        "requestId": request_id,
        "cancelled": state.global_search.cancel_request(&request_id),
        "runtime": "rust",
    }))
    .into_response()
}

fn validate_query(query: &GlobalSearchQuery) -> Result<(), String> {
    if !(1..=200).contains(&query.limit.unwrap_or(60)) {
        return Err("global search limit must be between 1 and 200".to_string());
    }
    if !(1..=1000).contains(&query.sidebar_limit.unwrap_or(50)) {
        return Err("sidebarLimit must be between 1 and 1000".to_string());
    }
    if !(1..=5_000).contains(&query.scan_budget_ms.unwrap_or(450)) {
        return Err("scanBudgetMs must be between 1 and 5000".to_string());
    }
    let search = query.search.as_deref().unwrap_or("").trim();
    if search.chars().count() < 3 || search.chars().count() > 2_000 {
        return Err("global content search requires 3 to 2000 characters".to_string());
    }
    validate_option(
        query.kind.as_deref(),
        "conversation",
        &["conversation", "user", "commentary", "assistant", "assistant_unclassified"],
    )?;
    validate_option(
        query.source_filter.as_deref(),
        "all",
        &[
            "all",
            "pure",
            "focused",
            "with_agents",
            "automation",
            "delegation",
        ],
    )?;
    validate_option(
        query.thread_kind.as_deref(),
        "all",
        &["all", "main", "subagent"],
    )?;
    validate_option(query.filter_mode.as_deref(), "all", &["all", "archived"])?;
    Ok(())
}

fn validate_option(value: Option<&str>, default: &str, accepted: &[&str]) -> Result<(), String> {
    let normalized = normalized_option(value, default);
    if accepted.contains(&normalized.as_str()) {
        Ok(())
    } else {
        Err(format!("unsupported global search option: {normalized}"))
    }
}

fn normalized_option(value: Option<&str>, default: &str) -> String {
    value.unwrap_or(default).trim().to_ascii_lowercase()
}

fn global_error_response(error: &str) -> Response {
    let status = if error == SEARCH_CANCELLED {
        StatusCode::from_u16(499).unwrap_or(StatusCode::BAD_REQUEST)
    } else if error.contains("already active")
        || error.contains("catalog")
        || error.contains("cursor is stale")
    {
        StatusCode::CONFLICT
    } else {
        StatusCode::BAD_REQUEST
    };
    (status, Json(json!({"detail": error, "runtime": "rust"}))).into_response()
}

fn build_inventory(
    codex_home: &Path,
    cancelled: &AtomicBool,
) -> Result<HashMap<String, Vec<PathBuf>>, String> {
    let mut pending = vec![
        codex_home.join("sessions"),
        codex_home.join("archived_sessions"),
    ];
    let mut inventory: HashMap<String, Vec<PathBuf>> = HashMap::new();
    while let Some(directory) = pending.pop() {
        check_cancelled(cancelled)?;
        let Ok(entries) = fs::read_dir(&directory) else {
            continue;
        };
        for entry in entries.flatten() {
            check_cancelled(cancelled)?;
            let path = entry.path();
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if file_type.is_dir() {
                pending.push(path);
            } else if path.extension().and_then(|value| value.to_str()) == Some("jsonl") {
                if let Some(thread_id) = session_root_thread_id(&path) {
                    inventory.entry(thread_id).or_default().push(path);
                }
            }
        }
    }
    for paths in inventory.values_mut() {
        paths.sort_by_key(|path| comparable_path(path));
        paths.dedup_by(|left, right| comparable_path(left) == comparable_path(right));
    }
    Ok(inventory)
}

fn session_root_thread_id(path: &Path) -> Option<String> {
    let file = File::open(path).ok()?;
    let mut first_line = Vec::new();
    BufReader::new(file)
        .take(MAX_SESSION_META_BYTES)
        .read_until(b'\n', &mut first_line)
        .ok()?;
    let value = serde_json::from_slice::<Value>(&first_line).ok()?;
    if value.get("type").and_then(Value::as_str) != Some("session_meta") {
        return None;
    }
    let payload = value.get("payload")?;
    payload
        .get("history_base")
        .and_then(|value| value.get("thread_id"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            payload
                .get("id")
                .or_else(|| payload.get("session_id"))
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
        })
        .map(str::to_string)
}

fn text(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn boolean(value: &Value, key: &str) -> bool {
    value.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn integer(value: &Value, key: &str) -> i64 {
    value.get(key).and_then(Value::as_i64).unwrap_or_default()
}

fn comparable_path(path: &Path) -> String {
    path.to_string_lossy()
        .replace('/', "\\")
        .trim_end_matches('\\')
        .to_lowercase()
}

fn check_cancelled(cancelled: &AtomicBool) -> Result<(), String> {
    if cancelled.load(Ordering::Acquire) {
        Err(SEARCH_CANCELLED.to_string())
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::tempdir;

    fn write_session(path: &Path, metadata: Value) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        let mut file = File::create(path).unwrap();
        writeln!(file, "{}", metadata).unwrap();
    }

    #[test]
    fn inventory_assigns_continuations_to_the_history_root() {
        let directory = tempdir().unwrap();
        let root = "019-root";
        let direct = directory.path().join("sessions/2026/09/direct.jsonl");
        let continuation = directory.path().join("archived_sessions/continued.jsonl");
        write_session(
            &direct,
            json!({"type":"session_meta","payload":{"id":root}}),
        );
        write_session(
            &continuation,
            json!({"type":"session_meta","payload":{"id":"019-child","history_base":{"thread_id":root}}}),
        );
        let inventory = build_inventory(directory.path(), &AtomicBool::new(false)).unwrap();
        assert_eq!(inventory[root].len(), 2);
        assert!(!inventory.contains_key("019-child"));
    }

    #[test]
    fn request_cancellation_is_native_and_scoped_by_request_id() {
        let store = GlobalSearchStore::default();
        let registration = store.begin_request(Some("request-1")).unwrap();
        assert!(!store.cancel_request("request-2"));
        assert!(store.cancel_request("request-1"));
        assert_eq!(
            check_cancelled(&registration.cancelled).unwrap_err(),
            SEARCH_CANCELLED
        );
        store.finish_request(&registration);
        assert!(!store.cancel_request("request-1"));
    }
}
