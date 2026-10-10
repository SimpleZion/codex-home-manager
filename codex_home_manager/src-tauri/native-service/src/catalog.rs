use std::{
    collections::{HashMap, HashSet, VecDeque},
    env, fs,
    io::{BufRead, BufReader, Read},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

use axum::{
    extract::{Path as AxumPath, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use rusqlite::{types::ValueRef, Connection, OpenFlags, Row};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::AppState;

const STATUS_VALUES: [&str; 7] = [
    "visible",
    "hidden_by_initial_limit",
    "hidden",
    "needs_user_event_repair",
    "missing_file",
    "archived",
    "subagent",
];

#[derive(Clone)]
struct Catalog {
    codex_home: String,
    database_path: String,
    global_state_path: String,
    session_index_path: String,
    sidebar_limit: usize,
    version: Value,
    summary: Value,
    threads: Vec<Value>,
    projects: Vec<Value>,
    sqlite_rows: HashMap<String, Value>,
    generated_at_ms: u64,
    catalog_version: String,
    fingerprint: String,
}

#[derive(Default)]
pub struct ThreadCatalogStore {
    catalogs: Mutex<HashMap<String, VecDeque<Arc<Catalog>>>>,
    rollout_paths: Mutex<HashMap<String, Vec<PathBuf>>>,
}

#[derive(Clone, Debug)]
pub struct ThreadLocator {
    pub id: String,
    pub title: String,
    pub rollout_path: PathBuf,
    pub rollout_paths: Vec<PathBuf>,
}

#[derive(Clone)]
pub struct GlobalSearchCatalog {
    pub codex_home: PathBuf,
    pub catalog_version: String,
    pub threads: Vec<Value>,
}

const RETAINED_CATALOG_VERSIONS: usize = 4;

#[derive(Debug, Deserialize)]
pub struct CatalogQuery {
    codex_home: Option<String>,
    sidebar_limit: Option<usize>,
    refresh: Option<bool>,
}

#[derive(Debug, Deserialize)]
pub struct ThreadPageQuery {
    codex_home: Option<String>,
    sidebar_limit: Option<usize>,
    thread_kind: Option<String>,
    filter_mode: Option<String>,
    project_path: Option<String>,
    search: Option<String>,
    sort_mode: Option<String>,
    sort_direction: Option<String>,
    limit: Option<usize>,
    cursor: Option<String>,
    catalog_version: Option<String>,
    refresh: Option<bool>,
}

#[derive(Debug)]
pub(super) struct CatalogError {
    status: StatusCode,
    detail: String,
}

impl CatalogError {
    fn bad_request(detail: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            detail: detail.into(),
        }
    }

    fn conflict(detail: impl Into<String>) -> Self {
        Self {
            status: StatusCode::CONFLICT,
            detail: detail.into(),
        }
    }

    fn internal(detail: impl Into<String>) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            detail: detail.into(),
        }
    }
}

impl IntoResponse for CatalogError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({"detail": self.detail}))).into_response()
    }
}

impl ThreadCatalogStore {
    pub fn global_search_catalog(
        &self,
        codex_home: Option<&str>,
        sidebar_limit: usize,
        catalog_version: Option<&str>,
        has_cursor: bool,
    ) -> Result<GlobalSearchCatalog, String> {
        let catalog = self
            .catalog_for_page(
                codex_home,
                sidebar_limit,
                false,
                catalog_version,
                has_cursor,
            )
            .map_err(|error| error.detail)?;
        Ok(GlobalSearchCatalog {
            codex_home: PathBuf::from(&catalog.codex_home),
            catalog_version: catalog.catalog_version.clone(),
            threads: catalog.threads.clone(),
        })
    }

    pub fn thread_locator(
        &self,
        codex_home: Option<&str>,
        thread_id: &str,
    ) -> Result<ThreadLocator, String> {
        let catalog = self
            .catalog(codex_home, 50, false)
            .map_err(|error| error.detail)?;
        let thread = catalog
            .threads
            .iter()
            .find(|thread| text(thread, "id") == thread_id)
            .ok_or_else(|| format!("thread not found: {thread_id}"))?;
        let rollout_path = PathBuf::from(text(thread, "rolloutPath"));
        if !rollout_path.is_file() {
            return Err(format!(
                "rollout file is missing: {}",
                rollout_path.display()
            ));
        }
        let rollout_cache_key = format!(
            "{}|{}|{}",
            comparable_path(&catalog.codex_home),
            thread_id,
            comparable_path(&rollout_path.to_string_lossy())
        );
        let rollout_paths = self
            .rollout_paths
            .lock()
            .map_err(|_| "thread rollout cache is unavailable".to_string())?
            .get(&rollout_cache_key)
            .cloned()
            .unwrap_or_else(|| {
                thread_rollout_paths(Path::new(&catalog.codex_home), thread_id, &rollout_path)
            });
        self.rollout_paths
            .lock()
            .map_err(|_| "thread rollout cache is unavailable".to_string())?
            .insert(rollout_cache_key, rollout_paths.clone());
        Ok(ThreadLocator {
            id: thread_id.to_string(),
            title: text(thread, "title"),
            rollout_path,
            rollout_paths,
        })
    }

    pub fn thread_tree(
        &self,
        codex_home: Option<&str>,
        thread_id: &str,
    ) -> Result<Vec<Value>, String> {
        let catalog = self
            .catalog(codex_home, 50, false)
            .map_err(|error| error.detail)?;
        if !catalog
            .threads
            .iter()
            .any(|thread| text(thread, "id") == thread_id)
        {
            return Err(format!("thread not found: {thread_id}"));
        }

        let mut pending = VecDeque::from([thread_id.to_string()]);
        let mut visited = HashSet::new();
        let mut result = Vec::new();
        while let Some(parent_id) = pending.pop_front() {
            if !visited.insert(parent_id.clone()) {
                continue;
            }
            if let Some(thread) = catalog
                .threads
                .iter()
                .find(|thread| text(thread, "id") == parent_id)
            {
                result.push(thread.clone());
            }
            for child in catalog
                .threads
                .iter()
                .filter(|thread| text(thread, "parentThreadId") == parent_id)
            {
                let child_id = text(child, "id");
                if !child_id.is_empty() && !visited.contains(&child_id) {
                    pending.push_back(child_id);
                }
            }
        }
        Ok(result)
    }
}

fn thread_rollout_paths(codex_home: &Path, thread_id: &str, active_path: &Path) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    let mut pending = vec![codex_home.join("sessions")];
    while let Some(directory) = pending.pop() {
        let Ok(entries) = fs::read_dir(directory) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if file_type.is_dir() {
                pending.push(path);
                continue;
            }
            let matches_name = path
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|name| name.ends_with(".jsonl") && name.contains(thread_id));
            if matches_name && rollout_session_matches(&path, thread_id) {
                paths.push(path);
            }
        }
    }
    if !paths.iter().any(|path| same_path(path, active_path)) {
        paths.push(active_path.to_path_buf());
    }
    paths.sort_by(|left, right| {
        comparable_path(&left.to_string_lossy()).cmp(&comparable_path(&right.to_string_lossy()))
    });
    paths.dedup_by(|left, right| same_path(left, right));
    paths
}

fn rollout_session_matches(path: &Path, thread_id: &str) -> bool {
    let Ok(file) = fs::File::open(path) else {
        return false;
    };
    let mut first_line = Vec::new();
    if BufReader::new(file)
        .take(8 * 1024 * 1024)
        .read_until(b'\n', &mut first_line)
        .is_err()
    {
        return false;
    }
    let Ok(value) = serde_json::from_slice::<Value>(&first_line) else {
        return false;
    };
    if value.get("type").and_then(Value::as_str) != Some("session_meta") {
        return false;
    }
    let payload = value.get("payload");
    payload
        .and_then(|value| value.get("id").or_else(|| value.get("session_id")))
        .and_then(Value::as_str)
        == Some(thread_id)
        || payload
            .and_then(|value| value.get("history_base"))
            .and_then(|value| value.get("thread_id"))
            .and_then(Value::as_str)
            == Some(thread_id)
}

fn same_path(left: &Path, right: &Path) -> bool {
    comparable_path(&left.to_string_lossy()) == comparable_path(&right.to_string_lossy())
}

pub async fn snapshot_summary(
    State(state): State<AppState>,
    Query(query): Query<CatalogQuery>,
) -> Response {
    let sidebar_limit = query.sidebar_limit.unwrap_or(50);
    if !(1..=1000).contains(&sidebar_limit) {
        return CatalogError::bad_request("sidebar_limit must be between 1 and 1000")
            .into_response();
    }
    let store = state.catalog.clone();
    let result = tokio::task::spawn_blocking(move || {
        store.catalog(
            query.codex_home.as_deref(),
            sidebar_limit,
            query.refresh.unwrap_or(false),
        )
    })
    .await;
    let catalog = match result {
        Ok(Ok(catalog)) => catalog,
        Ok(Err(error)) => return error.into_response(),
        Err(error) => {
            return CatalogError::internal(format!("catalog worker failed: {error}"))
                .into_response()
        }
    };

    let mut status_counts = Map::new();
    for status in STATUS_VALUES {
        status_counts.insert(
            status.to_string(),
            json!(catalog
                .threads
                .iter()
                .filter(|thread| text(thread, "visibility") == status)
                .count()),
        );
    }
    let mut highlights = catalog.threads.clone();
    highlights.sort_by(|left, right| {
        (
            integer(right, "fileSizeBytes"),
            integer(right, "tokensUsed"),
        )
            .cmp(&(integer(left, "fileSizeBytes"), integer(left, "tokensUsed")))
    });
    highlights.truncate(4);

    Json(json!({
        "codexHome": catalog.codex_home,
        "databasePath": catalog.database_path,
        "globalStatePath": catalog.global_state_path,
        "sessionIndexPath": catalog.session_index_path,
        "sidebarLimit": catalog.sidebar_limit,
        "version": catalog.version,
        "summary": catalog.summary,
        "projects": catalog.projects,
        "statusCounts": status_counts,
        "threadHighlights": highlights,
        "catalogVersion": catalog.catalog_version,
        "generatedAtMs": catalog.generated_at_ms,
    }))
    .into_response()
}

pub async fn health(State(state): State<AppState>, Query(query): Query<CatalogQuery>) -> Response {
    let store = state.catalog.clone();
    let result = tokio::task::spawn_blocking(move || {
        let catalog = store.catalog(query.codex_home.as_deref(), 50, false)?;
        let processes = running_codex_processes();
        let desktop_install = processes
            .iter()
            .find(|process| {
                let name = text(process, "name").to_lowercase();
                name == "chatgpt.exe" || name == "codex.exe"
            })
            .map(|process| {
                let path = text(process, "path");
                json!({
                    "version": desktop_version_from_path(&path),
                    "path": path,
                    "modifiedAtMs": file_modified_ms(&text(process, "path")),
                })
            });
        let warnings = if processes.is_empty() {
            Vec::new()
        } else {
            vec![format!(
                "{} Codex-related processes are running; write operations require explicit acknowledgement.",
                processes.len()
            )]
        };
        let pagination_patch = crate::pagination_patch::status(desktop_install.as_ref());
        Ok::<Value, CatalogError>(json!({
            "paths": {
                "codexHome": catalog.codex_home,
                "database": catalog.database_path,
                "globalState": catalog.global_state_path,
                "sessionIndex": catalog.session_index_path,
                "config": Path::new(&catalog.codex_home).join("config.toml").to_string_lossy(),
            },
            "checks": {
                "codexHomeExists": Path::new(&catalog.codex_home).is_dir(),
                "databaseExists": Path::new(&catalog.database_path).is_file(),
                "globalStateExists": Path::new(&catalog.global_state_path).is_file(),
                "sessionIndexExists": Path::new(&catalog.session_index_path).is_file(),
                "configExists": Path::new(&catalog.codex_home).join("config.toml").is_file(),
                "backupRoot": "D:\\Backup\\codex_home_manager",
            },
            "threadCount": catalog.summary.get("totalThreads").cloned().unwrap_or(Value::Null),
            "version": catalog.version,
            "currentVersions": {
                "configuredCli": Value::Null,
                "runtimeConfiguredCli": Value::Null,
                "pathCli": Value::Null,
                "desktopInstall": desktop_install,
                "versionCache": catalog.version,
            },
            "paginationPatch": pagination_patch,
            "codexProcesses": processes,
            "writeWarnings": warnings,
        }))
    })
    .await;
    match result {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => error.into_response(),
        Err(error) => {
            CatalogError::internal(format!("health worker failed: {error}")).into_response()
        }
    }
}

pub async fn thread_page(
    State(state): State<AppState>,
    Query(query): Query<ThreadPageQuery>,
) -> Response {
    match thread_page_result(state.catalog.clone(), query).await {
        Ok(payload) => Json(payload).into_response(),
        Err(error) => error.into_response(),
    }
}

pub async fn native_thread_detail(
    State(state): State<AppState>,
    AxumPath(thread_id): AxumPath<String>,
    Query(query): Query<CatalogQuery>,
) -> Response {
    let sidebar_limit = query.sidebar_limit.unwrap_or(50);
    if !(1..=1000).contains(&sidebar_limit) {
        return CatalogError::bad_request("sidebar_limit must be between 1 and 1000")
            .into_response();
    }
    let store = state.catalog.clone();
    let result = tokio::task::spawn_blocking(move || {
        let catalog = store.catalog(query.codex_home.as_deref(), sidebar_limit, false)?;
        let Some(thread) = catalog
            .threads
            .iter()
            .find(|thread| text(thread, "id") == thread_id)
            .cloned()
        else {
            return Err(CatalogError {
                status: StatusCode::NOT_FOUND,
                detail: format!("thread not found: {thread_id}"),
            });
        };
        let mut descendants = Vec::new();
        let mut visited = HashSet::from([thread_id.clone()]);
        collect_descendants(&thread_id, &catalog.threads, &mut visited, &mut descendants);
        Ok::<Value, CatalogError>(json!({
            "thread": thread,
            "descendants": descendants,
            "sqliteRow": catalog.sqlite_rows.get(&thread_id).cloned().unwrap_or_else(|| json!({})),
            "rolloutStats": Value::Null,
            "analysisStatus": "pending",
            "backups": [],
        }))
    })
    .await;
    match result {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => error.into_response(),
        Err(error) => {
            CatalogError::internal(format!("thread detail worker failed: {error}")).into_response()
        }
    }
}

async fn thread_page_result(
    store: Arc<ThreadCatalogStore>,
    query: ThreadPageQuery,
) -> Result<Value, CatalogError> {
    let sidebar_limit = query.sidebar_limit.unwrap_or(50);
    let limit = query.limit.unwrap_or(100);
    if !(1..=1000).contains(&sidebar_limit) || !(1..=200).contains(&limit) {
        return Err(CatalogError::bad_request("invalid paging limit"));
    }
    let thread_kind = validated_option(
        query.thread_kind.as_deref(),
        "main",
        &["main", "all", "subagent"],
    )?;
    let filter_mode = validated_option(
        query.filter_mode.as_deref(),
        "all",
        &["all", "visible", "manual_hidden", "repair", "archived"],
    )?;
    let sort_mode = validated_option(
        query.sort_mode.as_deref(),
        "rank",
        &[
            "rank", "updated", "size", "tokens", "project", "title", "status",
        ],
    )?;
    let sort_direction =
        validated_option(query.sort_direction.as_deref(), "asc", &["asc", "desc"])?;
    let codex_home = query.codex_home.clone();
    let refresh = query.refresh.unwrap_or(false);
    let expected_catalog = query
        .catalog_version
        .as_deref()
        .filter(|value| !value.is_empty())
        .map(str::to_owned);
    let has_cursor = query.cursor.is_some();
    let catalog = tokio::task::spawn_blocking(move || {
        store.catalog_for_page(
            codex_home.as_deref(),
            sidebar_limit,
            refresh,
            expected_catalog.as_deref(),
            has_cursor,
        )
    })
    .await
    .map_err(|error| CatalogError::internal(format!("catalog worker failed: {error}")))??;
    let project_path = query.project_path.as_deref().unwrap_or("all");
    let normalized_search = query.search.as_deref().unwrap_or("").trim().to_lowercase();
    if normalized_search.chars().count() > 2000 {
        return Err(CatalogError::bad_request("search is too long"));
    }
    let query_hash = hash_json(&json!({
        "threadKind": thread_kind,
        "filterMode": filter_mode,
        "projectPath": project_path,
        "search": normalized_search,
        "sortMode": sort_mode,
        "sortDirection": sort_direction,
        "limit": limit,
    }));
    let offset = match query.cursor.as_deref() {
        Some(cursor) => decode_cursor(cursor, &query_hash, &catalog.catalog_version)?,
        None => 0,
    };

    let scoped: Vec<&Value> = catalog
        .threads
        .iter()
        .filter(|thread| thread_kind == "all" || text(thread, "threadKind") == thread_kind)
        .filter(|thread| matches_status(thread, &filter_mode, &thread_kind))
        .collect();
    let mut project_counts = Map::new();
    for thread in &scoped {
        let path = text(thread, "projectPath");
        let next = project_counts
            .get(&path)
            .and_then(Value::as_u64)
            .unwrap_or(0)
            + 1;
        project_counts.insert(path, json!(next));
    }
    let mut matched: Vec<Value> = scoped
        .into_iter()
        .filter(|thread| project_path == "all" || text(thread, "projectPath") == project_path)
        .filter(|thread| {
            normalized_search.is_empty() || search_text(thread).contains(&normalized_search)
        })
        .cloned()
        .collect();
    matched.sort_by(|left, right| compare_threads(left, right, &sort_mode));
    if sort_direction == "desc" {
        matched.reverse();
    }
    if offset > matched.len() {
        return Err(CatalogError::conflict(
            "thread page cursor is outside the current result set",
        ));
    }
    let end = (offset + limit).min(matched.len());
    let page = matched[offset..end].to_vec();
    let has_more = end < matched.len();
    Ok(json!({
        "codexHome": catalog.codex_home,
        "catalogVersion": catalog.catalog_version,
        "generatedAtMs": catalog.generated_at_ms,
        "matchedThreads": matched.len(),
        "loadedThreads": page.len(),
        "threads": page,
        "projectCounts": project_counts,
        "hasMore": has_more,
        "nextCursor": if has_more { Some(encode_cursor(end, &query_hash, &catalog.catalog_version)) } else { None },
    }))
}

impl ThreadCatalogStore {
    fn cache_key(codex_home: &Path, sidebar_limit: usize) -> String {
        format!(
            "{}|{sidebar_limit}",
            comparable_path(&codex_home.to_string_lossy())
        )
    }

    fn cached_version(
        &self,
        key: &str,
        catalog_version: &str,
    ) -> Result<Option<Arc<Catalog>>, CatalogError> {
        let catalogs = self
            .catalogs
            .lock()
            .map_err(|_| CatalogError::internal("thread catalog lock is poisoned"))?;
        Ok(catalogs
            .get(key)
            .and_then(|versions| {
                versions
                    .iter()
                    .find(|catalog| catalog.catalog_version == catalog_version)
            })
            .cloned())
    }

    fn catalog_for_page(
        &self,
        codex_home_text: Option<&str>,
        sidebar_limit: usize,
        refresh: bool,
        expected_catalog: Option<&str>,
        has_cursor: bool,
    ) -> Result<Arc<Catalog>, CatalogError> {
        let codex_home = resolve_codex_home(codex_home_text)?;
        let key = Self::cache_key(&codex_home, sidebar_limit);
        if !refresh {
            if let Some(expected) = expected_catalog {
                if let Some(catalog) = self.cached_version(&key, expected)? {
                    return Ok(catalog);
                }
            }
        }
        let catalog = self.catalog(codex_home_text, sidebar_limit, refresh)?;
        if has_cursor && expected_catalog != Some(catalog.catalog_version.as_str()) {
            return Err(CatalogError::conflict(
                "thread page cursor catalog is no longer retained; reload the first page",
            ));
        }
        Ok(catalog)
    }

    fn catalog(
        &self,
        codex_home_text: Option<&str>,
        sidebar_limit: usize,
        refresh: bool,
    ) -> Result<Arc<Catalog>, CatalogError> {
        let codex_home = resolve_codex_home(codex_home_text)?;
        let key = Self::cache_key(&codex_home, sidebar_limit);
        let fingerprint = source_fingerprint(&codex_home);
        if !refresh {
            let catalogs = self
                .catalogs
                .lock()
                .map_err(|_| CatalogError::internal("thread catalog lock is poisoned"))?;
            if let Some(catalog) = catalogs.get(&key).and_then(VecDeque::front) {
                if catalog.fingerprint == fingerprint {
                    return Ok(Arc::clone(catalog));
                }
            }
        }
        let catalog = Arc::new(build_catalog(&codex_home, sidebar_limit)?);
        self.remember_catalog(key, catalog)
    }

    fn remember_catalog(
        &self,
        key: String,
        catalog: Arc<Catalog>,
    ) -> Result<Arc<Catalog>, CatalogError> {
        let mut catalogs = self
            .catalogs
            .lock()
            .map_err(|_| CatalogError::internal("thread catalog lock is poisoned"))?;
        let versions = catalogs.entry(key).or_default();
        if let Some(existing) = versions
            .iter()
            .find(|existing| existing.catalog_version == catalog.catalog_version)
        {
            return Ok(Arc::clone(existing));
        }
        versions.push_front(Arc::clone(&catalog));
        versions.truncate(RETAINED_CATALOG_VERSIONS);
        Ok(catalog)
    }
}

fn build_catalog(codex_home: &Path, sidebar_limit: usize) -> Result<Catalog, CatalogError> {
    let database_path = codex_home.join("state_5.sqlite");
    if !database_path.is_file() {
        return Err(CatalogError::bad_request(format!(
            "state database not found: {}",
            database_path.display()
        )));
    }
    let global_state_path = codex_home.join(".codex-global-state.json");
    let session_index_path = codex_home.join("session_index.jsonl");
    let global_state = read_json(&global_state_path);
    let version = read_json(&codex_home.join("version.json"));
    let rows = load_thread_rows(&database_path)?;
    let spawn_edges = load_spawn_edges(&database_path)?;
    let spawn_parents: HashSet<String> = spawn_edges
        .values()
        .map(|edge| edge.parent_thread_id.clone())
        .collect();
    let kind_metadata: HashMap<String, KindMetadata> = rows
        .iter()
        .map(|row| {
            let id = row_text(row, "id");
            let metadata = kind_for(row, spawn_edges.get(&id), spawn_parents.contains(&id));
            (id, metadata)
        })
        .collect();
    let session_records = read_session_index(&session_index_path);
    let session_entries: HashMap<String, SessionRecord> = session_records
        .iter()
        .cloned()
        .map(|record| (record.thread_id.clone(), record))
        .collect();
    let session_ranks = session_ranks(&session_records, &rows, &kind_metadata);
    let all_thread_ranks = thread_ranks(&rows, &kind_metadata, false);
    let main_thread_ranks = thread_ranks(&rows, &kind_metadata, true);

    let pinned = state_string_set(&global_state, "pinned-thread-ids");
    let hidden = state_string_set(&global_state, "codex_home_manager-hidden-thread-ids")
        .into_iter()
        .chain(state_string_set(
            &global_state,
            "codex-thread-manager-hidden-thread-ids",
        ))
        .collect::<HashSet<_>>();
    let workspace_hints = state_string_map(&global_state, "thread-workspace-root-hints");
    let mut explicit = pinned.clone();
    explicit.extend(workspace_hints.keys().cloned());
    let projectless = state_string_set(&global_state, "projectless-thread-ids");
    let saved_projects = saved_project_paths(&global_state);
    let saved_comparable: HashSet<String> = saved_projects
        .iter()
        .map(|path| comparable_path(path))
        .collect();
    let project_labels = project_labels_from_state(&global_state);
    let project_path_aliases = project_path_aliases(&global_state, &project_labels);
    let assignments = inherit_project_assignments(project_assignments(&global_state), &spawn_edges);
    let conversation_roots: HashSet<String> = workspace_hints
        .values()
        .filter(|path| comparable_path(path).ends_with("documents\\codex"))
        .cloned()
        .collect();

    let mut projects: HashMap<String, ProjectAggregate> = HashMap::new();
    for path in &saved_projects {
        let label = project_label(path, "workspace_project", &project_labels);
        projects.insert(
            comparable_path(path),
            ProjectAggregate::new(path, &label, "workspace_project"),
        );
    }
    let mut threads = Vec::with_capacity(rows.len());
    let mut total_storage = 0_i64;
    let archived_root = comparable_path(&codex_home.join("archived_sessions").to_string_lossy());

    for row in &rows {
        let id = row_text(row, "id");
        let kind = &kind_metadata[&id];
        let working_directory = normalize_path(&row_text(row, "cwd"));
        let assignment = assignments.get(&id);
        let project_path = assignment
            .map(|value| value.path.clone())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| working_directory.clone());
        let project_path = project_path_aliases
            .get(&comparable_path(&project_path))
            .cloned()
            .unwrap_or(project_path);
        let project_kind = classify_project_kind(
            &project_path,
            &saved_comparable,
            &conversation_roots,
            projectless.contains(&id),
        );
        let project_label = assignment
            .map(|value| value.label.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| project_label(&project_path, project_kind, &project_labels));
        let rollout_path = normalize_path(&row_text(row, "rollout_path"));
        let rollout = file_stat(&rollout_path);
        total_storage += rollout.size_bytes;
        let archived = row_bool(row, "archived");
        let has_user_event = row_bool(row, "has_user_event");
        let sidebar = session_entries.get(&id);
        let sqlite_title = nonempty(&row_text(row, "title"), "(untitled)");
        let sqlite_name_supported = row.contains_key("name");
        let sqlite_name = row_text(row, "name").trim().to_string();
        let session_title = sidebar.map(|entry| entry.title.clone()).unwrap_or_default();
        let display_title = nonempty(&sqlite_name, &sqlite_title);
        let has_user_signal = has_user_event
            || row_text(row, "thread_source").eq_ignore_ascii_case("user")
            || !row_text(row, "first_user_message").trim().is_empty()
            || !row_text(row, "preview").trim().is_empty()
            || sidebar.is_some();
        let list_rank = all_thread_ranks.get(&id).copied();
        let main_list_rank = main_thread_ranks.get(&id).copied();
        let session_rank = session_ranks.get(&id).copied();
        let recent_rank = [list_rank, session_rank].into_iter().flatten().min();
        let in_archived_store = {
            let path = comparable_path(&rollout.path);
            !path.is_empty()
                && (path == archived_root || path.starts_with(&(archived_root.clone() + "\\")))
        };
        let (visibility, reasons, codex_visible) = classify_visibility(ClassifyInput {
            archived,
            has_user_event,
            has_native_first_user_message: !row_text(row, "first_user_message").trim().is_empty(),
            has_user_signal,
            file_exists: rollout.exists,
            rollout_in_archived_store: in_archived_store,
            thread_kind: &kind.thread_kind,
            project_kind,
            thread_list_rank: list_rank,
            main_thread_list_rank: main_list_rank,
            session_index_rank: session_rank,
            sidebar_limit,
            pinned: pinned.contains(&id),
            explicit: explicit.contains(&id),
            manager_hidden: hidden.contains(&id),
        });
        let tokens_used = row_integer(row, "tokens_used");
        let project_id = assignment
            .map(|value| value.project_id.clone())
            .unwrap_or_default();
        let preview = nonempty(
            &row_text(row, "preview"),
            &row_text(row, "first_user_message"),
        );
        let thread = json!({
            "id": id,
            "title": truncate_text(&display_title, 500),
            "sqliteTitle": truncate_text(&sqlite_title, 500),
            "sqliteName": truncate_text(&sqlite_name, 500),
            "sqliteNameSupported": sqlite_name_supported,
            "sidebarTitle": truncate_text(&sqlite_name, 500),
            "sessionIndexTitle": truncate_text(&session_title, 500),
            "sessionIndexUpdatedAt": sidebar.map(|entry| entry.updated_at.clone()).unwrap_or_default(),
            "rolloutTitle": "",
            "rolloutTitleTimestamp": "",
            "rolloutTitleLine": Value::Null,
            "preview": truncate_text(&preview, 1200),
            "projectPath": project_path,
            "projectLabel": project_label,
            "projectKind": project_kind,
            "projectId": project_id,
            "workingDirectory": working_directory,
            "rolloutPath": rollout.path,
            "source": truncate_text(&row_text(row, "source"), 500),
            "threadKind": kind.thread_kind,
            "threadSource": kind.thread_source,
            "parentThreadId": kind.parent_thread_id,
            "subagentStatus": kind.subagent_status,
            "agentNickname": kind.agent_nickname,
            "agentRole": kind.agent_role,
            "model": nonempty(&row_text(row, "model"), &row_text(row, "model_provider")),
            "createdAtMs": timestamp_ms(row, "created"),
            "updatedAtMs": timestamp_ms(row, "updated"),
            "archived": archived,
            "archivedAtMs": row_optional_integer(row, "archived_at").map(|value| value * 1000),
            "hasUserEvent": has_user_event,
            "hasUserSignal": has_user_signal,
            "tokensUsed": tokens_used,
            "fileExists": rollout.exists,
            "fileSizeBytes": rollout.size_bytes,
            "childThreadCount": 0,
            "childFileSizeBytes": 0,
            "totalFileSizeBytes": rollout.size_bytes,
            "childTokensUsed": 0,
            "totalTokensUsed": tokens_used,
            "fileModifiedAtMs": rollout.modified_at_ms,
            "rolloutInArchivedStore": in_archived_store,
            "recentRank": recent_rank,
            "threadListRank": list_rank,
            "mainThreadListRank": main_list_rank,
            "sessionIndexRank": session_rank,
            "isPinned": pinned.contains(&id),
            "explicitSidebarReference": explicit.contains(&id),
            "managerHidden": hidden.contains(&id),
            "presentInThreadList": list_rank.is_some(),
            "presentInSessionIndex": session_rank.is_some(),
            "initialThreadListVisible": list_rank.is_some_and(|rank| rank <= sidebar_limit),
            "initialSessionIndexVisible": session_rank.is_some_and(|rank| rank <= sidebar_limit),
            "inInitialSidebarPage": codex_visible,
            "outsideInitialLimit": false,
            "codexVisible": codex_visible,
            "visibility": visibility,
            "hiddenReasons": reasons,
            "rolloutDisplayStatus": "not_scanned",
            "rolloutDisplayResponseUserMessages": 0,
            "rolloutDisplayResponseAssistantMessages": 0,
            "rolloutDisplayVisibleUserMessages": 0,
            "rolloutDisplayVisibleAgentMessages": 0,
            "rolloutDisplayEventUserMessages": 0,
            "rolloutDisplayEventAgentMessages": 0,
            "gitBranch": row_text(row, "git_branch"),
            "cliVersion": row_text(row, "cli_version"),
        });
        let project_key = comparable_path(&text(&thread, "projectPath"));
        projects
            .entry(project_key)
            .or_insert_with(|| {
                ProjectAggregate::new(
                    &text(&thread, "projectPath"),
                    &text(&thread, "projectLabel"),
                    project_kind,
                )
            })
            .add_thread(&thread);
        threads.push(thread);
    }

    aggregate_children(&mut threads);
    let mut project_values: Vec<Value> = projects
        .into_values()
        .map(ProjectAggregate::finish)
        .collect();
    let single_thread_labels: HashMap<String, String> = threads
        .iter()
        .map(|thread| {
            (
                comparable_path(&text(thread, "projectPath")),
                text(thread, "title"),
            )
        })
        .collect();
    for project in &mut project_values {
        if text(project, "projectKind") == "conversation" && integer(project, "total") == 1 {
            if let Some(label) = single_thread_labels.get(&comparable_path(&text(project, "path")))
            {
                if !label.trim().is_empty() {
                    project["label"] = json!(label);
                    for thread in &mut threads {
                        if comparable_path(&text(thread, "projectPath"))
                            == comparable_path(&text(project, "path"))
                        {
                            thread["projectLabel"] = json!(label);
                        }
                    }
                }
            }
        }
    }
    project_values.sort_by(|left, right| {
        project_kind_order(&text(left, "projectKind"))
            .cmp(&project_kind_order(&text(right, "projectKind")))
            .then_with(|| {
                text(left, "label")
                    .to_lowercase()
                    .cmp(&text(right, "label").to_lowercase())
            })
            .then_with(|| {
                text(left, "path")
                    .to_lowercase()
                    .cmp(&text(right, "path").to_lowercase())
            })
    });

    let main_count = threads
        .iter()
        .filter(|thread| text(thread, "threadKind") == "main")
        .count();
    let visible_count = threads
        .iter()
        .filter(|thread| boolean(thread, "codexVisible"))
        .count();
    let archived_count = threads
        .iter()
        .filter(|thread| boolean(thread, "archived"))
        .count();
    let repair_count = threads
        .iter()
        .filter(|thread| {
            matches!(
                text(thread, "visibility").as_str(),
                "needs_user_event_repair" | "missing_file"
            )
        })
        .count();
    let eligible = rows
        .iter()
        .filter(|row| {
            let id = row_text(row, "id");
            session_ranks.contains_key(&id) || all_thread_ranks.contains_key(&id)
        })
        .count();
    let summary = json!({
        "totalThreads": threads.len(),
        "mainThreads": main_count,
        "subagentThreads": threads.len() - main_count,
        "eligibleThreads": eligible,
        "codexVisibleThreads": visible_count,
        "hiddenByInitialLimit": 0,
        "archivedThreads": archived_count,
        "needsRepairThreads": repair_count,
        "savedProjects": saved_projects.len(),
        "workspaceProjects": project_values.iter().filter(|project| text(project, "projectKind") == "workspace_project").count(),
        "conversationProjects": project_values.iter().filter(|project| text(project, "projectKind") == "conversation").count(),
        "otherProjects": project_values.iter().filter(|project| text(project, "projectKind") == "other").count(),
        "emptyProjectsWithHiddenThreads": project_values.iter().filter(|project| boolean(project, "emptyButHasHiddenThreads")).count(),
        "totalStorageBytes": total_storage,
    });
    let sqlite_rows = rows
        .iter()
        .map(|row| {
            let id = row_text(row, "id");
            let value = Value::Object(
                row.iter()
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect(),
            );
            (id, value)
        })
        .collect();
    let fingerprint = source_fingerprint(codex_home);
    Ok(Catalog {
        codex_home: codex_home.to_string_lossy().into_owned(),
        database_path: database_path.to_string_lossy().into_owned(),
        global_state_path: global_state_path.to_string_lossy().into_owned(),
        session_index_path: session_index_path.to_string_lossy().into_owned(),
        sidebar_limit,
        version,
        summary,
        threads,
        projects: project_values,
        sqlite_rows,
        generated_at_ms: now_ms(),
        catalog_version: fingerprint.chars().take(24).collect(),
        fingerprint,
    })
}

fn collect_descendants(
    parent_id: &str,
    threads: &[Value],
    visited: &mut HashSet<String>,
    descendants: &mut Vec<Value>,
) {
    for thread in threads {
        if text(thread, "parentThreadId") != parent_id {
            continue;
        }
        let id = text(thread, "id");
        if !visited.insert(id.clone()) {
            continue;
        }
        descendants.push(thread.clone());
        collect_descendants(&id, threads, visited, descendants);
    }
}

type DbRow = HashMap<String, Value>;

fn load_thread_rows(database_path: &Path) -> Result<Vec<DbRow>, CatalogError> {
    let connection = open_database(database_path)?;
    let mut statement = connection
        .prepare("SELECT * FROM threads ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC, id DESC")
        .map_err(|error| CatalogError::internal(format!("cannot query threads: {error}")))?;
    let names: Vec<String> = statement
        .column_names()
        .iter()
        .map(|name| (*name).to_string())
        .collect();
    let rows = statement
        .query_map([], |row| db_row(row, &names))
        .map_err(|error| CatalogError::internal(format!("cannot read threads: {error}")))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| CatalogError::internal(format!("cannot decode threads: {error}")))
}

#[derive(Clone)]
struct SpawnEdge {
    parent_thread_id: String,
    status: String,
}

fn load_spawn_edges(database_path: &Path) -> Result<HashMap<String, SpawnEdge>, CatalogError> {
    let connection = open_database(database_path)?;
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_spawn_edges')",
            [],
            |row| row.get(0),
        )
        .unwrap_or(false);
    if !exists {
        return Ok(HashMap::new());
    }
    let mut statement = connection
        .prepare("SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges")
        .map_err(|error| CatalogError::internal(format!("cannot query thread edges: {error}")))?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(1)?,
                SpawnEdge {
                    parent_thread_id: row.get(0)?,
                    status: row.get(2).unwrap_or_default(),
                },
            ))
        })
        .map_err(|error| CatalogError::internal(format!("cannot read thread edges: {error}")))?;
    rows.collect::<Result<HashMap<_, _>, _>>()
        .map_err(|error| CatalogError::internal(format!("cannot decode thread edges: {error}")))
}

fn open_database(path: &Path) -> Result<Connection, CatalogError> {
    Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| CatalogError::internal(format!("cannot open {}: {error}", path.display())))
}

fn db_row(row: &Row<'_>, names: &[String]) -> rusqlite::Result<DbRow> {
    let mut result = HashMap::with_capacity(names.len());
    for (index, name) in names.iter().enumerate() {
        let value = match row.get_ref(index)? {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(value) => json!(value),
            ValueRef::Real(value) => json!(value),
            ValueRef::Text(value) => json!(String::from_utf8_lossy(value)),
            ValueRef::Blob(_) => Value::Null,
        };
        result.insert(name.clone(), value);
    }
    Ok(result)
}

#[derive(Clone)]
struct SessionRecord {
    thread_id: String,
    title: String,
    updated_at: String,
    line: usize,
}

fn read_session_index(path: &Path) -> Vec<SessionRecord> {
    let Ok(content) = fs::read_to_string(path) else {
        return Vec::new();
    };
    content
        .lines()
        .enumerate()
        .filter_map(|(index, line)| {
            serde_json::from_str::<Value>(line)
                .ok()
                .map(|value| (index, value))
        })
        .filter_map(|(index, value)| {
            let id = value.get("id")?.as_str()?.to_string();
            Some(SessionRecord {
                thread_id: id,
                title: value
                    .get("thread_name")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .trim()
                    .to_string(),
                updated_at: value
                    .get("updated_at")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
                line: index + 1,
            })
        })
        .collect()
}

#[derive(Clone)]
struct KindMetadata {
    thread_kind: String,
    thread_source: String,
    parent_thread_id: String,
    subagent_status: String,
    agent_nickname: String,
    agent_role: String,
}

fn kind_for(row: &DbRow, edge: Option<&SpawnEdge>, is_spawn_parent: bool) -> KindMetadata {
    let source = row_text(row, "source");
    let source_json = serde_json::from_str::<Value>(&source).unwrap_or(Value::Null);
    let subagent = source_json.get("subagent");
    let spawn = subagent.and_then(|value| value.get("thread_spawn"));
    let thread_source = row_text(row, "thread_source");
    let has_parent_evidence = edge.is_some() || subagent.is_some_and(json_truthy);
    let is_subagent = has_parent_evidence || (thread_source == "subagent" && !is_spawn_parent);
    KindMetadata {
        thread_kind: if is_subagent { "subagent" } else { "main" }.to_string(),
        thread_source: nonempty(&thread_source, &source),
        parent_thread_id: edge
            .map(|value| value.parent_thread_id.clone())
            .filter(|value| !value.is_empty())
            .or_else(|| {
                spawn
                    .and_then(|value| value.get("parent_thread_id"))
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .unwrap_or_default(),
        subagent_status: edge.map(|value| value.status.clone()).unwrap_or_default(),
        agent_nickname: nonempty(
            &row_text(row, "agent_nickname"),
            spawn
                .and_then(|value| value.get("agent_nickname"))
                .and_then(Value::as_str)
                .unwrap_or_default(),
        ),
        agent_role: nonempty(
            &row_text(row, "agent_role"),
            spawn
                .and_then(|value| value.get("agent_role"))
                .and_then(Value::as_str)
                .unwrap_or_default(),
        ),
    }
}

fn json_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().unwrap_or_default() != 0.0,
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn session_ranks(
    records: &[SessionRecord],
    rows: &[DbRow],
    kinds: &HashMap<String, KindMetadata>,
) -> HashMap<String, usize> {
    let rows_by_id: HashMap<String, &DbRow> =
        rows.iter().map(|row| (row_text(row, "id"), row)).collect();
    let mut records = records.to_vec();
    records.sort_by_key(|record| std::cmp::Reverse(record.line));
    let mut rank = 0;
    let mut result = HashMap::new();
    for record in records {
        let Some(row) = rows_by_id.get(&record.thread_id) else {
            continue;
        };
        if row_bool(row, "archived")
            || kinds
                .get(&record.thread_id)
                .is_none_or(|kind| kind.thread_kind != "main")
        {
            continue;
        }
        rank += 1;
        result.entry(record.thread_id).or_insert(rank);
    }
    result
}

fn thread_ranks(
    rows: &[DbRow],
    kinds: &HashMap<String, KindMetadata>,
    main_only: bool,
) -> HashMap<String, usize> {
    let mut rank = 0;
    let mut result = HashMap::new();
    for row in rows {
        if row_bool(row, "archived") {
            continue;
        }
        let id = row_text(row, "id");
        if main_only && kinds.get(&id).is_none_or(|kind| kind.thread_kind != "main") {
            continue;
        }
        rank += 1;
        result.insert(id, rank);
    }
    result
}

#[derive(Clone)]
struct Assignment {
    path: String,
    project_id: String,
    label: String,
}

fn project_assignments(state: &Value) -> HashMap<String, Assignment> {
    let assignments = state_value(state, "thread-project-assignments");
    let local_projects = state_value(state, "local-projects");
    let Some(assignments) = assignments.as_object() else {
        return HashMap::new();
    };
    let local_projects = local_projects.as_object();
    assignments
        .iter()
        .filter_map(|(thread_id, assignment)| {
            let assignment = assignment.as_object()?;
            let project_id = assignment
                .get("projectId")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let project = local_projects
                .and_then(|projects| projects.get(project_id))
                .and_then(Value::as_object);
            let canonical_root = project
                .and_then(|value| value.get("rootPaths"))
                .and_then(Value::as_array)
                .and_then(|paths| {
                    paths
                        .iter()
                        .filter_map(Value::as_str)
                        .map(normalize_path)
                        .find(|path| !path.is_empty())
                })
                .unwrap_or_default();
            let fallback_path = ["path", "cwd"].iter().find_map(|key| {
                assignment
                    .get(*key)
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
            });
            let path = if canonical_root.is_empty() {
                normalize_path(fallback_path.unwrap_or_default())
            } else {
                canonical_root
            };
            if path.is_empty() {
                return None;
            }
            Some((
                thread_id.clone(),
                Assignment {
                    path,
                    project_id: project_id.to_string(),
                    label: project
                        .and_then(|value| value.get("name"))
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .trim()
                        .to_string(),
                },
            ))
        })
        .collect()
}

fn inherit_project_assignments(
    mut assignments: HashMap<String, Assignment>,
    spawn_edges: &HashMap<String, SpawnEdge>,
) -> HashMap<String, Assignment> {
    fn resolve(
        thread_id: &str,
        assignments: &mut HashMap<String, Assignment>,
        spawn_edges: &HashMap<String, SpawnEdge>,
        visiting: &mut HashSet<String>,
    ) -> Option<Assignment> {
        if let Some(assignment) = assignments.get(thread_id) {
            return Some(assignment.clone());
        }
        if !visiting.insert(thread_id.to_string()) {
            return None;
        }
        let parent_thread_id = spawn_edges.get(thread_id)?.parent_thread_id.clone();
        let inherited = resolve(&parent_thread_id, assignments, spawn_edges, visiting);
        visiting.remove(thread_id);
        if let Some(assignment) = inherited.clone() {
            assignments.insert(thread_id.to_string(), assignment);
        }
        inherited
    }

    for child_thread_id in spawn_edges.keys() {
        resolve(
            child_thread_id,
            &mut assignments,
            spawn_edges,
            &mut HashSet::new(),
        );
    }
    assignments
}

fn classify_project_kind<'a>(
    project_path: &str,
    saved_projects: &HashSet<String>,
    conversation_roots: &HashSet<String>,
    projectless: bool,
) -> &'a str {
    let comparable = comparable_path(project_path);
    if saved_projects
        .iter()
        .any(|saved| comparable == *saved || comparable.starts_with(&(saved.clone() + "\\")))
    {
        return "workspace_project";
    }
    if projectless
        || conversation_roots
            .iter()
            .map(|root| comparable_path(root))
            .any(|root| comparable == root || comparable.starts_with(&(root + "\\")))
    {
        return "conversation";
    }
    "other"
}

fn project_label(path: &str, kind: &str, labels: &HashMap<String, String>) -> String {
    if let Some(label) = labels.get(&comparable_path(path)) {
        return label.clone();
    }
    if kind == "conversation" {
        let parts: Vec<&str> = path
            .split(['\\', '/'])
            .filter(|part| !part.is_empty())
            .collect();
        if let Some(index) = parts
            .iter()
            .position(|part| part.eq_ignore_ascii_case("codex"))
        {
            if index + 1 < parts.len() {
                return parts[index + 1..].join("\\");
            }
        }
    }
    Path::new(path)
        .file_name()
        .map(|value| value.to_string_lossy().into_owned())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| {
            if path.is_empty() {
                "No project".to_string()
            } else {
                path.to_string()
            }
        })
}

struct ClassifyInput<'a> {
    archived: bool,
    has_user_event: bool,
    has_native_first_user_message: bool,
    has_user_signal: bool,
    file_exists: bool,
    rollout_in_archived_store: bool,
    thread_kind: &'a str,
    project_kind: &'a str,
    thread_list_rank: Option<usize>,
    main_thread_list_rank: Option<usize>,
    session_index_rank: Option<usize>,
    sidebar_limit: usize,
    pinned: bool,
    explicit: bool,
    manager_hidden: bool,
}

fn classify_visibility(input: ClassifyInput<'_>) -> (String, Vec<String>, bool) {
    let mut reasons = Vec::new();
    if input.archived {
        reasons.push("archived".to_string());
    }
    if !input.has_user_signal {
        reasons.push("missing_user_signal".to_string());
    }
    if input.has_user_signal && !input.has_user_event {
        reasons.push("metadata_has_user_event_false".to_string());
    }
    if input.has_user_signal && !input.has_native_first_user_message {
        reasons.push("missing_first_user_message".to_string());
    }
    if input.session_index_rank.is_none() {
        reasons.push("missing_session_index_entry".to_string());
    }
    if !input.file_exists {
        reasons.push("missing_rollout_file".to_string());
    }
    if input.rollout_in_archived_store {
        reasons.push("rollout_in_archived_sessions".to_string());
    }
    if input.thread_kind == "subagent" {
        reasons.push("subagent_child_thread".to_string());
    }
    if input
        .thread_list_rank
        .is_some_and(|rank| rank > input.sidebar_limit)
    {
        reasons.push("outside_thread_list_initial_page".to_string());
    }
    if input.project_kind == "conversation"
        && input
            .main_thread_list_rank
            .is_some_and(|rank| rank > input.sidebar_limit)
    {
        reasons.push("outside_conversation_initial_page".to_string());
    }
    if input
        .session_index_rank
        .is_some_and(|rank| rank > input.sidebar_limit)
    {
        reasons.push("outside_session_index_repair_window".to_string());
    }
    if input.manager_hidden {
        reasons.push("manually_hidden_by_manager".to_string());
    }

    let active_main = input.thread_kind == "main" && !input.archived && input.file_exists;
    let sidebar_visible = active_main
        && !input.manager_hidden
        && if input.project_kind == "conversation" {
            input.main_thread_list_rank.is_some()
                || input.thread_list_rank.is_some()
                || input.session_index_rank.is_some()
                || input.explicit
                || input.pinned
        } else {
            input.thread_list_rank.is_some()
                || input.session_index_rank.is_some()
                || input.pinned
                || input.explicit
        };
    if input.thread_kind == "subagent" && !input.archived && input.file_exists {
        return ("subagent".to_string(), reasons, false);
    }
    if input.archived {
        return ("archived".to_string(), reasons, false);
    }
    if !input.file_exists {
        return ("missing_file".to_string(), reasons, false);
    }
    if active_main
        && (input.rollout_in_archived_store
            || (input.has_user_signal && !input.has_native_first_user_message))
    {
        return ("needs_user_event_repair".to_string(), reasons, false);
    }
    if active_main && input.manager_hidden {
        return ("hidden".to_string(), reasons, false);
    }
    if sidebar_visible {
        return ("visible".to_string(), reasons, true);
    }
    if !input.has_user_signal {
        return ("hidden".to_string(), reasons, false);
    }
    if !input.has_user_event || input.session_index_rank.is_none() {
        return ("needs_user_event_repair".to_string(), reasons, false);
    }
    ("hidden".to_string(), reasons, false)
}

#[derive(Default)]
struct ProjectAggregate {
    path: String,
    label: String,
    kind: String,
    total: i64,
    main_threads: i64,
    subagent_threads: i64,
    active: i64,
    visible: i64,
    archived: i64,
    needs_repair: i64,
    storage_bytes: i64,
}

impl ProjectAggregate {
    fn new(path: &str, label: &str, kind: &str) -> Self {
        Self {
            path: path.to_string(),
            label: label.to_string(),
            kind: kind.to_string(),
            ..Self::default()
        }
    }

    fn add_thread(&mut self, thread: &Value) {
        self.total += 1;
        if text(thread, "threadKind") == "subagent" {
            self.subagent_threads += 1;
        } else {
            self.main_threads += 1;
        }
        if text(thread, "threadKind") == "main"
            && !boolean(thread, "archived")
            && boolean(thread, "fileExists")
        {
            self.active += 1;
        }
        if boolean(thread, "codexVisible") {
            self.visible += 1;
        }
        if boolean(thread, "archived") {
            self.archived += 1;
        }
        if matches!(
            text(thread, "visibility").as_str(),
            "needs_user_event_repair" | "missing_file"
        ) {
            self.needs_repair += 1;
        }
        self.storage_bytes += integer(thread, "fileSizeBytes");
    }

    fn finish(self) -> Value {
        let empty_hidden = self.active > 0 && self.visible == 0 && self.needs_repair > 0;
        json!({
            "path": self.path, "label": self.label, "projectKind": self.kind,
            "total": self.total, "mainThreads": self.main_threads, "subagentThreads": self.subagent_threads,
            "active": self.active, "visible": self.visible, "hiddenByInitialLimit": 0,
            "archived": self.archived, "needsRepair": self.needs_repair,
            "storageBytes": self.storage_bytes, "emptyButHasHiddenThreads": empty_hidden,
        })
    }
}

fn aggregate_children(threads: &mut [Value]) {
    let index_by_id: HashMap<String, usize> = threads
        .iter()
        .enumerate()
        .map(|(index, thread)| (text(thread, "id"), index))
        .collect();
    let mut children: HashMap<String, Vec<String>> = HashMap::new();
    for thread in threads.iter() {
        let parent = text(thread, "parentThreadId");
        if !parent.is_empty() && index_by_id.contains_key(&parent) {
            children.entry(parent).or_default().push(text(thread, "id"));
        }
    }
    fn visit(
        id: &str,
        threads: &[Value],
        index: &HashMap<String, usize>,
        children: &HashMap<String, Vec<String>>,
        cache: &mut HashMap<String, (i64, i64, i64)>,
        visiting: &mut HashSet<String>,
    ) -> (i64, i64, i64) {
        if let Some(value) = cache.get(id) {
            return *value;
        }
        if !visiting.insert(id.to_string()) {
            return (0, 0, 0);
        }
        let mut result = (0, 0, 0);
        for child in children.get(id).into_iter().flatten() {
            let descendant = visit(child, threads, index, children, cache, visiting);
            if let Some(thread) = index.get(child).map(|position| &threads[*position]) {
                result.0 += 1 + descendant.0;
                result.1 += integer(thread, "fileSizeBytes") + descendant.1;
                result.2 += integer(thread, "tokensUsed") + descendant.2;
            }
        }
        visiting.remove(id);
        cache.insert(id.to_string(), result);
        result
    }
    let mut cache = HashMap::new();
    for position in 0..threads.len() {
        let id = text(&threads[position], "id");
        let aggregate = visit(
            &id,
            threads,
            &index_by_id,
            &children,
            &mut cache,
            &mut HashSet::new(),
        );
        threads[position]["childThreadCount"] = json!(aggregate.0);
        threads[position]["childFileSizeBytes"] = json!(aggregate.1);
        threads[position]["totalFileSizeBytes"] =
            json!(integer(&threads[position], "fileSizeBytes") + aggregate.1);
        threads[position]["childTokensUsed"] = json!(aggregate.2);
        threads[position]["totalTokensUsed"] =
            json!(integer(&threads[position], "tokensUsed") + aggregate.2);
    }
}

fn matches_status(thread: &Value, filter: &str, kind: &str) -> bool {
    match filter {
        "visible" if kind == "subagent" => {
            text(thread, "threadKind") == "subagent"
                && !boolean(thread, "archived")
                && boolean(thread, "fileExists")
        }
        "visible" if kind == "all" => {
            boolean(thread, "codexVisible")
                || (text(thread, "threadKind") == "subagent"
                    && !boolean(thread, "archived")
                    && boolean(thread, "fileExists"))
        }
        "visible" => boolean(thread, "codexVisible"),
        "manual_hidden" => text(thread, "visibility") == "hidden",
        "repair" => matches!(
            text(thread, "visibility").as_str(),
            "needs_user_event_repair" | "missing_file"
        ),
        "archived" => boolean(thread, "archived"),
        _ => true,
    }
}

fn search_text(thread: &Value) -> String {
    [
        "title",
        "sqliteTitle",
        "sidebarTitle",
        "sessionIndexTitle",
        "rolloutTitle",
        "id",
        "projectPath",
        "rolloutPath",
        "preview",
        "parentThreadId",
        "agentNickname",
    ]
    .iter()
    .map(|key| text(thread, key))
    .collect::<Vec<_>>()
    .join(" ")
    .to_lowercase()
}

fn compare_threads(left: &Value, right: &Value, mode: &str) -> std::cmp::Ordering {
    let rank = |thread: &Value| {
        let value = integer(thread, "recentRank");
        if value <= 0 {
            999_999
        } else {
            value
        }
    };
    let status = |thread: &Value| match text(thread, "visibility").as_str() {
        "visible" | "hidden_by_initial_limit" => 0,
        "hidden" => 2,
        "needs_user_event_repair" => 3,
        "missing_file" => 4,
        "archived" => 5,
        "subagent" => 6,
        _ => 99,
    };
    let primary = match mode {
        "status" => status(left).cmp(&status(right)),
        "title" => text(left, "title")
            .to_lowercase()
            .cmp(&text(right, "title").to_lowercase()),
        "project" => (
            text(left, "projectLabel").to_lowercase(),
            text(left, "title").to_lowercase(),
        )
            .cmp(&(
                text(right, "projectLabel").to_lowercase(),
                text(right, "title").to_lowercase(),
            )),
        "size" => integer(left, "totalFileSizeBytes").cmp(&integer(right, "totalFileSizeBytes")),
        "tokens" => integer(left, "totalTokensUsed").cmp(&integer(right, "totalTokensUsed")),
        "updated" => integer(left, "updatedAtMs").cmp(&integer(right, "updatedAtMs")),
        _ => rank(left).cmp(&rank(right)),
    };
    primary
        .then_with(|| rank(left).cmp(&rank(right)))
        .then_with(|| {
            text(left, "title")
                .to_lowercase()
                .cmp(&text(right, "title").to_lowercase())
        })
        .then_with(|| text(left, "id").cmp(&text(right, "id")))
}

pub(super) fn resolve_codex_home(input: Option<&str>) -> Result<PathBuf, CatalogError> {
    if let Some(input) = input.filter(|value| !value.trim().is_empty()) {
        let path = PathBuf::from(input.trim());
        if !path.join("state_5.sqlite").is_file() {
            return Err(CatalogError::bad_request(format!(
                "codex_home is not valid: {}",
                path.display()
            )));
        }
        return Ok(path);
    }
    if let Ok(configured) = env::var("CODEX_HOME") {
        let path = PathBuf::from(configured);
        if path.join("state_5.sqlite").is_file() {
            return Ok(path);
        }
    }
    let home = env::var_os("USERPROFILE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    for candidate in [
        home.join(".codex"),
        PathBuf::from(r"D:\.codex"),
        PathBuf::from(r"C:\.codex"),
    ] {
        if candidate.join("state_5.sqlite").is_file() {
            return Ok(candidate);
        }
    }
    Err(CatalogError::bad_request(
        "no Codex Home with state_5.sqlite was found",
    ))
}

fn state_value<'a>(state: &'a Value, key: &str) -> &'a Value {
    state
        .get(key)
        .or_else(|| {
            state
                .get("electron-persisted-atom-state")
                .and_then(|value| value.get(key))
        })
        .unwrap_or(&Value::Null)
}

fn state_string_array(state: &Value, key: &str) -> Vec<String> {
    state_value(state, key)
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(normalize_path)
        .collect()
}

fn state_string_set(state: &Value, key: &str) -> HashSet<String> {
    state_value(state, key)
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_string)
        .collect()
}

fn state_string_map(state: &Value, key: &str) -> HashMap<String, String> {
    state_value(state, key)
        .as_object()
        .into_iter()
        .flat_map(|value| value.iter())
        .filter_map(|(key, value)| {
            value
                .as_str()
                .map(|value| (key.clone(), normalize_path(value)))
        })
        .collect()
}

fn legacy_saved_project_paths(state: &Value) -> Vec<String> {
    state_string_array(state, "electron-saved-workspace-roots")
}

#[derive(Clone)]
struct LocalProjectEntry {
    canonical_path: String,
    label: String,
    root_paths: Vec<String>,
}

fn local_project_entries(state: &Value) -> Vec<LocalProjectEntry> {
    let Some(projects) = state_value(state, "local-projects").as_object() else {
        return Vec::new();
    };
    let mut entries = Vec::new();
    for project in projects.values().filter_map(Value::as_object) {
        let label = project
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_string();
        let Some(root_paths) = project.get("rootPaths").and_then(Value::as_array) else {
            continue;
        };
        let mut seen = HashSet::new();
        let mut normalized_roots = Vec::new();
        for path in root_paths.iter().filter_map(Value::as_str) {
            let normalized = normalize_path(path);
            let comparable = comparable_path(&normalized);
            if !normalized.is_empty() && seen.insert(comparable) {
                normalized_roots.push(normalized);
            }
        }
        if let Some(canonical_path) = normalized_roots.first().cloned() {
            entries.push(LocalProjectEntry {
                canonical_path,
                label,
                root_paths: normalized_roots,
            });
        }
    }
    entries
}

fn saved_project_paths(state: &Value) -> Vec<String> {
    let current: Vec<String> = local_project_entries(state)
        .into_iter()
        .map(|project| project.canonical_path)
        .collect();
    if current.is_empty() {
        legacy_saved_project_paths(state)
    } else {
        current
    }
}

fn project_labels_from_state(state: &Value) -> HashMap<String, String> {
    let mut labels = state_value(state, "electron-workspace-root-labels")
        .as_object()
        .into_iter()
        .flat_map(|value| value.iter())
        .filter_map(|(path, label)| {
            label
                .as_str()
                .map(|label| (comparable_path(path), label.to_string()))
        })
        .collect::<HashMap<_, _>>();
    for project in local_project_entries(state) {
        if !project.label.is_empty() {
            labels.insert(comparable_path(&project.canonical_path), project.label);
        }
    }
    labels
}

fn path_identity(path: &str) -> String {
    normalize_path(path)
        .rsplit(['\\', '/'])
        .next()
        .unwrap_or_default()
        .trim()
        .to_lowercase()
}

fn project_path_aliases(
    state: &Value,
    project_labels: &HashMap<String, String>,
) -> HashMap<String, String> {
    let current_entries = local_project_entries(state);
    if current_entries.is_empty() {
        return HashMap::new();
    }
    let current_paths: Vec<String> = current_entries
        .iter()
        .map(|project| project.canonical_path.clone())
        .collect();
    let current_comparable: HashSet<String> = current_paths
        .iter()
        .map(|path| comparable_path(path))
        .collect();
    let mut current_by_identity: HashMap<String, HashSet<String>> = HashMap::new();
    for path in &current_paths {
        let comparable = comparable_path(path);
        for identity in [
            path_identity(path),
            project_labels
                .get(&comparable)
                .map(|label| label.trim().to_lowercase())
                .unwrap_or_default(),
        ] {
            if !identity.is_empty() {
                current_by_identity
                    .entry(identity)
                    .or_default()
                    .insert(path.clone());
            }
        }
    }

    let mut aliases = HashMap::new();
    let mut secondary_candidates: HashMap<String, HashSet<String>> = HashMap::new();
    for project in &current_entries {
        for secondary_path in project.root_paths.iter().skip(1) {
            let secondary_comparable = comparable_path(secondary_path);
            if !current_comparable.contains(&secondary_comparable) {
                secondary_candidates
                    .entry(secondary_comparable)
                    .or_default()
                    .insert(project.canonical_path.clone());
            }
        }
    }
    for (secondary_comparable, candidates) in secondary_candidates {
        if candidates.len() == 1 {
            if let Some(canonical_path) = candidates.into_iter().next() {
                aliases.insert(secondary_comparable, canonical_path);
            }
        }
    }

    for legacy_path in legacy_saved_project_paths(state) {
        let legacy_comparable = comparable_path(&legacy_path);
        if legacy_path.is_empty() || current_comparable.contains(&legacy_comparable) {
            continue;
        }
        let mut candidates = HashSet::new();
        for identity in [
            path_identity(&legacy_path),
            project_labels
                .get(&legacy_comparable)
                .map(|label| label.trim().to_lowercase())
                .unwrap_or_default(),
        ] {
            if let Some(paths) = current_by_identity.get(&identity) {
                candidates.extend(paths.iter().cloned());
            }
        }
        if candidates.len() == 1 {
            if let Some(current_path) = candidates.into_iter().next() {
                aliases.insert(legacy_comparable, current_path);
            }
        }
    }
    aliases
}

fn read_json(path: &Path) -> Value {
    fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_else(|| json!({}))
}

fn source_fingerprint(home: &Path) -> String {
    let mut hasher = Sha256::new();
    for name in [
        "state_5.sqlite",
        "state_5.sqlite-wal",
        "state_5.sqlite-shm",
        ".codex-global-state.json",
        ".codex-global-state.json.bak",
        "session_index.jsonl",
        "version.json",
    ] {
        let path = home.join(name);
        hasher.update(path.to_string_lossy().as_bytes());
        if let Ok(metadata) = fs::metadata(&path) {
            hasher.update(metadata.len().to_le_bytes());
            let modified = metadata
                .modified()
                .ok()
                .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
                .map(|value| value.as_nanos())
                .unwrap_or_default();
            hasher.update(modified.to_le_bytes());
        } else {
            hasher.update(u64::MAX.to_le_bytes());
        }
    }
    format!("{:x}", hasher.finalize())
}

struct FileStat {
    exists: bool,
    size_bytes: i64,
    modified_at_ms: Option<u64>,
    path: String,
}

fn file_stat(path: &str) -> FileStat {
    let normalized = normalize_path(path);
    let metadata = fs::metadata(&normalized).ok();
    FileStat {
        exists: metadata.is_some(),
        size_bytes: metadata
            .as_ref()
            .map(|value| value.len() as i64)
            .unwrap_or_default(),
        modified_at_ms: metadata
            .and_then(|value| value.modified().ok())
            .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
            .map(|value| value.as_millis() as u64),
        path: normalized,
    }
}

fn file_modified_ms(path: &str) -> Option<u64> {
    fs::metadata(path)
        .ok()?
        .modified()
        .ok()?
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|value| value.as_millis() as u64)
}

fn desktop_version_from_path(path: &str) -> String {
    Path::new(path)
        .components()
        .filter_map(|component| component.as_os_str().to_str())
        .find_map(|component| {
            component
                .strip_prefix("OpenAI.Codex_")
                .and_then(|suffix| suffix.split('_').next())
                .filter(|version| {
                    !version.is_empty()
                        && version
                            .chars()
                            .all(|character| character.is_ascii_digit() || character == '.')
                })
                .map(str::to_string)
        })
        .unwrap_or_default()
}

#[cfg(target_os = "windows")]
fn running_codex_processes() -> Vec<Value> {
    use std::{mem::size_of, os::windows::ffi::OsStringExt};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, INVALID_HANDLE_VALUE},
        System::{
            Diagnostics::ToolHelp::{
                CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
                TH32CS_SNAPPROCESS,
            },
            Threading::{
                OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
                PROCESS_QUERY_LIMITED_INFORMATION,
            },
        },
    };

    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot == INVALID_HANDLE_VALUE {
            return Vec::new();
        }
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = size_of::<PROCESSENTRY32W>() as u32;
        let mut result = Vec::new();
        let mut available = Process32FirstW(snapshot, &mut entry) != 0;
        while available {
            let name_length = entry
                .szExeFile
                .iter()
                .position(|character| *character == 0)
                .unwrap_or(entry.szExeFile.len());
            let name = std::ffi::OsString::from_wide(&entry.szExeFile[..name_length])
                .to_string_lossy()
                .into_owned();
            if matches!(
                name.to_ascii_lowercase().as_str(),
                "codex.exe" | "chatgpt.exe"
            ) {
                let process =
                    OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, entry.th32ProcessID);
                let path = if process.is_null() {
                    String::new()
                } else {
                    let mut buffer = vec![0_u16; 32_768];
                    let mut length = buffer.len() as u32;
                    let resolved = if QueryFullProcessImageNameW(
                        process,
                        PROCESS_NAME_WIN32,
                        buffer.as_mut_ptr(),
                        &mut length,
                    ) != 0
                    {
                        std::ffi::OsString::from_wide(&buffer[..length as usize])
                            .to_string_lossy()
                            .into_owned()
                    } else {
                        String::new()
                    };
                    CloseHandle(process);
                    resolved
                };
                result.push(json!({
                    "pid": entry.th32ProcessID,
                    "name": name,
                    "path": path,
                }));
            }
            available = Process32NextW(snapshot, &mut entry) != 0;
        }
        CloseHandle(snapshot);
        result
    }
}

#[cfg(not(target_os = "windows"))]
fn running_codex_processes() -> Vec<Value> {
    Vec::new()
}

fn normalize_path(path: &str) -> String {
    let path = path
        .strip_prefix(r"\\?\")
        .unwrap_or(path)
        .replace('/', r"\");
    path.trim_end_matches(['\\', '/']).to_string()
}

fn comparable_path(path: &str) -> String {
    normalize_path(path).to_lowercase()
}

fn row_text(row: &DbRow, key: &str) -> String {
    row.get(key)
        .and_then(|value| match value {
            Value::String(value) => Some(value.clone()),
            Value::Number(value) => Some(value.to_string()),
            _ => None,
        })
        .unwrap_or_default()
}

fn row_integer(row: &DbRow, key: &str) -> i64 {
    row.get(key).and_then(Value::as_i64).unwrap_or_default()
}

fn row_optional_integer(row: &DbRow, key: &str) -> Option<i64> {
    row.get(key)
        .and_then(Value::as_i64)
        .filter(|value| *value != 0)
}

fn row_bool(row: &DbRow, key: &str) -> bool {
    row_integer(row, key) != 0 || row.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn timestamp_ms(row: &DbRow, prefix: &str) -> i64 {
    let milliseconds = row_integer(row, &format!("{prefix}_at_ms"));
    if milliseconds != 0 {
        milliseconds
    } else {
        row_integer(row, &format!("{prefix}_at")) * 1000
    }
}

fn text(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn integer(value: &Value, key: &str) -> i64 {
    value.get(key).and_then(Value::as_i64).unwrap_or_default()
}

fn boolean(value: &Value, key: &str) -> bool {
    value.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn nonempty(value: &str, fallback: &str) -> String {
    if value.is_empty() {
        fallback.to_string()
    } else {
        value.to_string()
    }
}

fn truncate_text(value: &str, limit: usize) -> String {
    if value.chars().count() <= limit {
        return value.to_string();
    }
    value
        .chars()
        .take(limit.saturating_sub(1))
        .collect::<String>()
        + "…"
}

fn project_kind_order(value: &str) -> i32 {
    match value {
        "workspace_project" => 0,
        "conversation" => 1,
        "other" => 2,
        _ => 99,
    }
}

fn validated_option(
    value: Option<&str>,
    fallback: &'static str,
    allowed: &[&str],
) -> Result<String, CatalogError> {
    let value = value.unwrap_or(fallback);
    if allowed.contains(&value) {
        Ok(value.to_string())
    } else {
        Err(CatalogError::bad_request(format!(
            "invalid option: {value}"
        )))
    }
}

fn hash_json(value: &Value) -> String {
    let bytes = serde_json::to_vec(value).unwrap_or_default();
    let digest = Sha256::digest(bytes);
    format!("{digest:x}").chars().take(24).collect()
}

fn encode_cursor(offset: usize, query: &str, catalog: &str) -> String {
    URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(&json!({"offset": offset, "query": query, "catalog": catalog}))
            .unwrap_or_default(),
    )
}

fn decode_cursor(cursor: &str, query: &str, catalog: &str) -> Result<usize, CatalogError> {
    let bytes = URL_SAFE_NO_PAD
        .decode(cursor)
        .map_err(|_| CatalogError::conflict("invalid thread page cursor"))?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| CatalogError::conflict("invalid thread page cursor"))?;
    let offset = value
        .get("offset")
        .and_then(Value::as_u64)
        .ok_or_else(|| CatalogError::conflict("invalid thread page cursor"))?
        as usize;
    if value.get("query").and_then(Value::as_str) != Some(query)
        || value.get("catalog").and_then(Value::as_str) != Some(catalog)
    {
        return Err(CatalogError::conflict(
            "thread page cursor does not match the current query or catalog",
        ));
    }
    Ok(offset)
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    fn catalog_stub(version: &str) -> Arc<Catalog> {
        Arc::new(Catalog {
            codex_home: "D:\\.codex".to_string(),
            database_path: String::new(),
            global_state_path: String::new(),
            session_index_path: String::new(),
            sidebar_limit: 50,
            version: json!({}),
            summary: json!({}),
            threads: Vec::new(),
            projects: Vec::new(),
            sqlite_rows: HashMap::new(),
            generated_at_ms: 0,
            catalog_version: version.to_string(),
            fingerprint: version.to_string(),
        })
    }

    #[test]
    fn catalog_cache_retains_recent_immutable_versions_for_cursor_paging() {
        let store = ThreadCatalogStore::default();
        let key = "test|50".to_string();
        for version in ["v1", "v2", "v3", "v4", "v5"] {
            store
                .remember_catalog(key.clone(), catalog_stub(version))
                .expect("cache insert should succeed");
        }
        assert!(store.cached_version(&key, "v1").unwrap().is_none());
        assert_eq!(
            store
                .cached_version(&key, "v2")
                .unwrap()
                .unwrap()
                .catalog_version,
            "v2"
        );
        assert_eq!(
            store
                .cached_version(&key, "v5")
                .unwrap()
                .unwrap()
                .catalog_version,
            "v5"
        );
    }

    #[test]
    fn official_project_assignment_overrides_working_directory() {
        let state = json!({
            "thread-project-assignments": {"thread-1": {"projectId": "project-1", "path": "C:/Work/Assigned"}},
            "local-projects": {"project-1": {"name": "Assigned project", "rootPaths": ["C:/Work/Assigned"]}}
        });
        let assignments = project_assignments(&state);
        assert_eq!(assignments["thread-1"].path, r"C:\Work\Assigned");
        assert_eq!(assignments["thread-1"].label, "Assigned project");
    }

    #[test]
    fn current_local_projects_replace_legacy_roots_and_alias_unique_migrations() {
        let legacy_path = r"C:\ZionCloudDrive\CodexProject\电脑问题";
        let current_path = r"D:\CXProject\电脑问题";
        let state = json!({
            "electron-saved-workspace-roots": [legacy_path],
            "electron-workspace-root-labels": {legacy_path: "电脑问题"},
            "local-projects": {
                "project-1": {"name": "电脑问题", "rootPaths": [current_path]}
            }
        });

        let saved = saved_project_paths(&state);
        let labels = project_labels_from_state(&state);
        let aliases = project_path_aliases(&state, &labels);
        let saved_comparable = saved.iter().map(|path| comparable_path(path)).collect();

        assert_eq!(saved, vec![current_path.to_string()]);
        assert_eq!(labels[&comparable_path(current_path)], "电脑问题");
        assert_eq!(aliases[&comparable_path(legacy_path)], current_path);
        assert_eq!(
            classify_project_kind(current_path, &saved_comparable, &HashSet::new(), false),
            "workspace_project"
        );
    }

    #[test]
    fn local_projects_use_only_canonical_roots_and_preserve_canonical_labels() {
        let cloud_path = r"D:\CXProject\云计算";
        let system_cockpit_path = r"D:\CXProject\电脑问题\SystemCockpit";
        let computer_path = r"D:\CXProject\电脑问题";
        let codex_path = r"D:\CXProject\Codex";
        let vscode_path = r"D:\CXProject\VSCode";
        let state = json!({
            "local-projects": {
                "cloud": {"name": "云计算", "rootPaths": [cloud_path, system_cockpit_path]},
                "computer": {"name": "电脑问题", "rootPaths": [computer_path, codex_path]},
                "codex": {"name": "Codex", "rootPaths": [codex_path]},
                "vscode": {"name": "VSCode", "rootPaths": [vscode_path, computer_path, codex_path]}
            }
        });

        let saved = saved_project_paths(&state);
        let labels = project_labels_from_state(&state);
        let aliases = project_path_aliases(&state, &labels);

        assert_eq!(
            saved.into_iter().collect::<HashSet<_>>(),
            [cloud_path, computer_path, codex_path, vscode_path]
                .into_iter()
                .map(str::to_string)
                .collect()
        );
        assert_eq!(labels[&comparable_path(cloud_path)], "云计算");
        assert_eq!(labels[&comparable_path(computer_path)], "电脑问题");
        assert_eq!(labels[&comparable_path(codex_path)], "Codex");
        assert_eq!(labels[&comparable_path(vscode_path)], "VSCode");
        assert_eq!(aliases[&comparable_path(system_cockpit_path)], cloud_path);
        assert!(!aliases.contains_key(&comparable_path(computer_path)));
        assert!(!aliases.contains_key(&comparable_path(codex_path)));
    }

    #[test]
    fn subagent_inherits_parent_project_assignment_without_changing_working_directory() {
        let assignments = HashMap::from([(
            "parent".to_string(),
            Assignment {
                path: r"D:\CXProject\VSCode".to_string(),
                project_id: "vscode".to_string(),
                label: "VSCode".to_string(),
            },
        )]);
        let spawn_edges = HashMap::from([(
            "child".to_string(),
            SpawnEdge {
                parent_thread_id: "parent".to_string(),
                status: "closed".to_string(),
            },
        )]);

        let inherited = inherit_project_assignments(assignments, &spawn_edges);

        assert_eq!(inherited["child"].path, r"D:\CXProject\VSCode");
        assert_eq!(inherited["child"].project_id, "vscode");
        assert_eq!(inherited["child"].label, "VSCode");
    }

    #[test]
    fn ambiguous_legacy_project_names_are_not_aliased() {
        let legacy_path = r"C:\Legacy\Shared";
        let state = json!({
            "electron-saved-workspace-roots": [legacy_path],
            "local-projects": {
                "project-1": {"name": "Shared", "rootPaths": [r"D:\One"]},
                "project-2": {"name": "Shared", "rootPaths": [r"D:\Two"]}
            }
        });
        let labels = project_labels_from_state(&state);

        assert!(!project_path_aliases(&state, &labels).contains_key(&comparable_path(legacy_path)));
    }

    #[test]
    fn bare_subagent_marker_does_not_hide_a_spawn_parent() {
        let row = HashMap::from([
            ("thread_source".to_string(), json!("subagent")),
            ("source".to_string(), json!("")),
        ]);
        assert_eq!(kind_for(&row, None, true).thread_kind, "main");
        assert_eq!(kind_for(&row, None, false).thread_kind, "subagent");
    }

    #[test]
    fn cursor_rejects_a_different_query() {
        let cursor = encode_cursor(100, "query-a", "catalog-a");
        assert_eq!(decode_cursor(&cursor, "query-a", "catalog-a").unwrap(), 100);
        assert!(decode_cursor(&cursor, "query-b", "catalog-a").is_err());
    }

    #[test]
    fn rollout_discovery_collects_verified_continuation_segments_only() {
        let directory = tempfile::tempdir().unwrap();
        let thread_id = "019e6365-4d4c-7af1-a907-a79d3a28afdc";
        let older_directory = directory.path().join("sessions/2026/05/26");
        let current_directory = directory.path().join("sessions/2026/09/08");
        fs::create_dir_all(&older_directory).unwrap();
        fs::create_dir_all(&current_directory).unwrap();
        let older = older_directory.join(format!("rollout-old-{thread_id}.jsonl"));
        let current = current_directory.join(format!("rollout-current-{thread_id}.jsonl"));
        let impostor = current_directory.join(format!("rollout-impostor-{thread_id}.jsonl"));
        fs::write(
            &older,
            format!(
                "{}\n",
                json!({"type":"session_meta","payload":{"id":thread_id}})
            ),
        )
        .unwrap();
        fs::write(
            &current,
            format!(
                "{}\n",
                json!({"type":"session_meta","payload":{"history_base":{"thread_id":thread_id}}})
            ),
        )
        .unwrap();
        fs::write(
            &impostor,
            format!(
                "{}\n",
                json!({"type":"session_meta","payload":{"id":"different-thread"}})
            ),
        )
        .unwrap();

        let paths = thread_rollout_paths(directory.path(), thread_id, &current);

        assert_eq!(paths, vec![older, current]);
    }

    #[test]
    fn desktop_version_is_read_from_the_active_appx_path() {
        assert_eq!(
            desktop_version_from_path(
                r"C:\Program Files\WindowsApps\OpenAI.Codex_26.803.10989.0_x64__8wekyb3d8bbwe\app\ChatGPT.exe"
            ),
            "26.803.10989.0"
        );
    }
}
