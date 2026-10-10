use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    env,
    fs::{File, OpenOptions},
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::{atomic::AtomicBool, Arc, Mutex, OnceLock},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use fs2::FileExt;
use memchr::{memchr, memmem};
use memmap2::MmapOptions;
use rusqlite::{
    params, params_from_iter, types::Value as SqlValue, Connection, OpenFlags, OptionalExtension,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use unicode_casefold::UnicodeCaseFold;
use unicode_general_category::{get_general_category, GeneralCategory};
use unicode_normalization::UnicodeNormalization;
use uuid::Uuid;

use super::{
    check_cancelled, classify_prompt, incoming_delegation_text, is_cross_protocol_duplicate,
    prompt_candidate, prompt_text_from_item, timestamp_millis, PromptContextQuery, PromptPageQuery,
    PromptRecord, RecentPrompt, ThreadLocator, MAX_RECORD_BYTES, PROMPT_REQUEST_CANCELLED,
};
use crate::history::{assistant_stage, event_from_item, TimelineEvent};

const SCHEMA_VERSION: i64 = 23;
const BOUNDARY_BYTES: usize = 64 * 1024;
const SCAN_CHUNK_BYTES: usize = 1024 * 1024;
const COMMIT_RECORDS: usize = 256;
const COMMIT_BYTES: usize = 8 * 1024 * 1024;
const TIMELINE_COMMIT_RECORDS: usize = 512;
const TIMELINE_COMMIT_BYTES: usize = 8 * 1024 * 1024;
const RESULT_ITEM_BYTES: usize = 64 * 1024;
const RESULT_PAGE_BYTES: usize = 2 * 1024 * 1024;
const DIRECT_SEARCH_CACHE_TTL: Duration = Duration::from_secs(30);
const DIRECT_SEARCH_CACHE_LIMIT: usize = 2;

#[derive(Clone)]
struct DirectSearchCacheEntry {
    created_at: Instant,
    matches: Arc<Vec<Value>>,
    source_bytes: u64,
    source_file_count: usize,
    elapsed_ms: u128,
}

#[derive(Debug, Serialize, Deserialize)]
struct DirectSearchCursor {
    snapshot: String,
    query: String,
    position: usize,
}

pub(crate) fn global_timeline_cursor_is_direct(cursor: Option<&str>) -> bool {
    cursor
        .filter(|value| !value.is_empty())
        .and_then(|value| URL_SAFE_NO_PAD.decode(value).ok())
        .and_then(|bytes| serde_json::from_slice::<DirectSearchCursor>(&bytes).ok())
        .is_some()
}

#[derive(Debug)]
struct DirectMatch {
    timestamp_ms: i64,
    rollout_path: String,
    byte_offset: usize,
    value: Value,
}

static DIRECT_SEARCH_CACHE: OnceLock<Mutex<HashMap<String, DirectSearchCacheEntry>>> =
    OnceLock::new();

struct DatabaseLock {
    file: File,
}

impl DatabaseLock {
    fn exclusive(database_path: &Path) -> Result<Self, String> {
        Self::exclusive_cancelled(database_path, &AtomicBool::new(false))
    }

    fn exclusive_cancelled(database_path: &Path, cancelled: &AtomicBool) -> Result<Self, String> {
        check_cancelled(cancelled)?;
        let file = Self::open_lock_file(database_path)?;
        loop {
            check_cancelled(cancelled)?;
            match FileExt::try_lock_exclusive(&file) {
                Ok(()) => {
                    let lock = Self::from_locked_file(file)?;
                    check_cancelled(cancelled)?;
                    return Ok(lock);
                }
                Err(error)
                    if error.raw_os_error() == fs2::lock_contended_error().raw_os_error() =>
                {
                    thread::sleep(Duration::from_millis(10));
                }
                Err(error) => return Err(format!("cannot lock prompt index: {error}")),
            }
        }
    }

    fn open_lock_file(database_path: &Path) -> Result<File, String> {
        let lock_path = database_path.with_file_name(format!(
            "{}.lock",
            database_path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("prompt-index.sqlite")
        ));
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&lock_path)
            .map_err(|error| {
                format!(
                    "cannot open prompt index lock {}: {error}",
                    lock_path.display()
                )
            })?;
        Ok(file)
    }

    fn from_locked_file(file: File) -> Result<Self, String> {
        let lock = Self { file };
        if lock
            .file
            .metadata()
            .map_err(|error| format!("cannot inspect prompt index lock: {error}"))?
            .len()
            == 0
        {
            lock.file
                .set_len(1)
                .map_err(|error| format!("cannot initialize prompt index lock: {error}"))?;
        }
        Ok(lock)
    }

    fn try_exclusive(database_path: &Path) -> Result<Option<Self>, String> {
        let file = Self::open_lock_file(database_path)?;
        match FileExt::try_lock_exclusive(&file) {
            Ok(()) => Self::from_locked_file(file).map(Some),
            Err(error) if error.raw_os_error() == fs2::lock_contended_error().raw_os_error() => {
                Ok(None)
            }
            Err(error) => Err(format!("cannot inspect prompt index lock: {error}")),
        }
    }
}

impl Drop for DatabaseLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.file);
    }
}

#[derive(Clone, Debug)]
struct FileState {
    id: i64,
    device: String,
    inode: String,
    created_ns: i64,
    generation: String,
    observed_size: u64,
    observed_mtime_ns: i64,
    scanned_offset: u64,
    scanned_line_count: u64,
    boundary_hash: String,
    partial_offset: u64,
    partial_candidate: bool,
    complete: bool,
    timeline_scanned_offset: u64,
    timeline_scanned_line_count: u64,
    timeline_boundary_hash: String,
    timeline_partial_offset: u64,
    timeline_partial_candidate: bool,
    timeline_complete: bool,
    timeline_lineage_line_number: u64,
    timeline_lineage_text_hash: String,
    timeline_lineage_source_type: String,
    timeline_lineage_payload_type: String,
    timeline_lineage_kind: String,
}

#[derive(Clone, Debug)]
pub(crate) struct GlobalTimelineSegment {
    pub path: PathBuf,
    pub thread_id: String,
    pub thread_title: String,
    pub project_path: String,
    pub project_label: String,
    pub thread_kind: String,
    pub archived: bool,
    pub updated_at_ms: i64,
}

#[derive(Clone, Debug)]
pub(crate) struct GlobalTimelineSearchQuery {
    pub cursor: Option<String>,
    pub limit: usize,
    pub kind: String,
    pub search: String,
    pub source_filter: String,
    pub scan_budget_ms: u64,
}

#[derive(Debug)]
struct PendingTimelineEvent {
    event_index: i64,
    line_number: u64,
    event: TimelineEvent,
    search_text: String,
    pure_search_text: String,
}

#[derive(Clone, Debug)]
struct SourceState {
    size: u64,
    modified_ns: i64,
    created_ns: i64,
    device: String,
    inode: String,
}

#[derive(Debug)]
struct PendingPrompt {
    record: PromptRecord,
    timestamp_ms: Option<i64>,
    search_text: String,
    pure_search_text: String,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct IndexState {
    kind: &'static str,
    generation: String,
    cursor_generation: String,
    file_identity: FileIdentity,
    file_size: u64,
    file_mtime_ns: i64,
    scanned_bytes: u64,
    scanned_lines: u64,
    scan_start_offset: u64,
    scan_added_prompts: usize,
    complete: bool,
    reset: bool,
    elapsed_ms: u128,
    strategy: &'static str,
    runtime: &'static str,
    query_mode: &'static str,
    segment_count: usize,
}

struct IndexedSegment {
    file_id: i64,
    base: i64,
    prompt_count: i64,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct FileIdentity {
    device: String,
    inode: String,
    created_ns: i64,
}

#[derive(Debug, Serialize, Deserialize)]
struct PersistentCursor {
    generation: String,
    query: String,
    position: i64,
    #[serde(default)]
    segment_file_id: Option<i64>,
    #[serde(default)]
    complete_snapshot: bool,
}

#[allow(clippy::too_many_arguments)]
pub(super) fn read_persistent_prompt_page(
    locator: &ThreadLocator,
    codex_home: &Path,
    query: &PromptPageQuery,
    scope: &str,
    source_type: &str,
    order: &str,
    search: &str,
    limit: usize,
    cancelled: Arc<AtomicBool>,
) -> Result<Value, String> {
    let result = read_persistent_prompt_page_inner(
        locator,
        codex_home,
        query,
        scope,
        source_type,
        order,
        search,
        limit,
        cancelled.clone(),
        false,
    );
    if cancelled.load(std::sync::atomic::Ordering::Acquire) {
        Err(PROMPT_REQUEST_CANCELLED.to_string())
    } else {
        result.and_then(|page| page.ok_or_else(|| "prompt index page is unavailable".to_string()))
    }
}

pub(super) fn read_indexed_recent_prompt_page(
    locator: &ThreadLocator,
    codex_home: &Path,
    query: &PromptPageQuery,
    scope: &str,
    source_type: &str,
    limit: usize,
    cancelled: Arc<AtomicBool>,
) -> Result<Option<Value>, String> {
    if let Some(cursor) = query.cursor.as_deref().filter(|cursor| !cursor.is_empty()) {
        let indexed_cursor = URL_SAFE_NO_PAD.decode(cursor).ok()
            .and_then(|bytes| serde_json::from_slice::<PersistentCursor>(&bytes).ok()).is_some();
        if !indexed_cursor {
            return Ok(None);
        }
        // Keep the cursor's reader for its entire paging transaction.
        return read_persistent_prompt_page(locator, codex_home, query, scope, source_type,
            "desc", "", limit, cancelled).map(Some);
    }
    read_persistent_prompt_page_inner(locator, codex_home, query, scope, source_type,
        "desc", "", limit, cancelled, true)
}

pub(super) fn read_persistent_prompt_context(
    locator: &ThreadLocator,
    codex_home: &Path,
    query: &PromptContextQuery,
) -> Result<Value, String> {
    let scope = query.scope.as_deref().unwrap_or("visible").trim();
    let source_type = query.source_type.as_deref().unwrap_or("").trim();
    let search = query.search.as_deref().unwrap_or("").trim();
    let mut anchor_index = query.anchor_index.max(1) as i64;
    let before = query.before.unwrap_or(10).min(50);
    let after = query.after.unwrap_or(10).min(50);
    let database_path = prompt_index_database_path(codex_home)?;
    if let Some(parent) = database_path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| {
            format!(
                "cannot create prompt index directory {}: {error}",
                parent.display()
            )
        })?;
    }
    let _database_lock = DatabaseLock::exclusive(&database_path)?;
    let mut connection = connect(&database_path)?;
    let cancelled = AtomicBool::new(false);
    let (segments, index_state) = advance_thread_index(
        &mut connection,
        locator,
        if query.generation.is_some() {
            0
        } else {
            query.scan_budget_ms.unwrap_or(250).clamp(1, 5_000)
        },
        &cancelled,
    )?;
    if query.generation.as_ref().is_some_and(|generation| {
        generation
            != if query.anchor_record_id.is_some() {
                &index_state.cursor_generation
            } else {
                &index_state.generation
            }
    }) {
        return Err("prompt context anchor is stale: index generation changed".to_string());
    }
    if let Some(record_id) = query.anchor_record_id.as_deref() {
        let (file_id, local_index) = parse_record_id(record_id)?;
        let segment = segments
            .iter()
            .find(|segment| segment.file_id == file_id)
            .ok_or_else(|| "prompt context anchor is stale: unknown segment".to_string())?;
        if local_index < 1 || local_index > segment.prompt_count {
            return Err("prompt context anchor is stale: invalid local position".to_string());
        }
        anchor_index = segment.base + local_index;
    }
    let (segments_sql, mut values) = segment_cte(&segments);
    let (scope_sql, scope_values) = scope_clause(scope)?;
    let mut clauses = vec![scope_sql];
    values.extend(scope_values);
    if !source_type.is_empty() {
        clauses.push("p.source_type=?".to_string());
        values.push(SqlValue::Text(source_type.to_string()));
    }
    let where_sql = clauses
        .iter()
        .map(|value| format!("({value})"))
        .collect::<Vec<_>>()
        .join(" AND ");
    let columns =
        "s.base+p.prompt_index,p.line_number,p.byte_offset,p.timestamp,p.protocol,p.text,\
        p.character_count,p.source_type,p.source_label,p.visible_by_default,p.pure_text,\
        p.pure_character_count,p.has_pure_text,p.file_id,p.prompt_index,s.segment_order";
    let mut anchor_values = values.clone();
    anchor_values.push(SqlValue::Integer(anchor_index));
    let mut anchor_rows = query_indexed_prompts(
        &connection,
        &format!("{segments_sql} SELECT {columns} FROM segments AS s JOIN prompts AS p ON p.file_id=s.file_id WHERE {where_sql} AND s.base+p.prompt_index=?"),
        &anchor_values,
    )?;
    let anchor = anchor_rows
        .pop()
        .ok_or_else(|| "prompt context anchor does not match the current filters".to_string())?;
    if query
        .anchor_byte_offset
        .is_some_and(|offset| offset != anchor.byte_offset)
    {
        return Err("prompt context anchor is stale: record position changed".to_string());
    }

    let mut earlier_values = values.clone();
    earlier_values.push(SqlValue::Integer(anchor_index));
    earlier_values.push(SqlValue::Integer(before as i64));
    let mut earlier = query_indexed_prompts(
        &connection,
        &format!(
            "{segments_sql} SELECT {columns} FROM segments AS s JOIN prompts AS p ON p.file_id=s.file_id WHERE {where_sql} AND s.base+p.prompt_index<? \
             ORDER BY s.base+p.prompt_index DESC LIMIT ?"
        ),
        &earlier_values,
    )?;
    earlier.reverse();
    let mut later_values = values.clone();
    later_values.push(SqlValue::Integer(anchor_index));
    later_values.push(SqlValue::Integer(after as i64));
    let later = query_indexed_prompts(
        &connection,
        &format!(
            "{segments_sql} SELECT {columns} FROM segments AS s JOIN prompts AS p ON p.file_id=s.file_id WHERE {where_sql} AND s.base+p.prompt_index>? \
             ORDER BY s.base+p.prompt_index ASC LIMIT ?"
        ),
        &later_values,
    )?;
    let anchor_offset = earlier.len();
    let mut rows = earlier;
    rows.push(anchor);
    rows.extend(later);
    let first_index = rows.first().map(|row| row.index).unwrap_or(anchor_index);
    let last_index = rows.last().map(|row| row.index).unwrap_or(anchor_index);
    let has_earlier = context_neighbor_exists(
        &connection,
        &segments_sql,
        &where_sql,
        &values,
        "<",
        first_index,
    )?;
    let has_later = context_neighbor_exists(
        &connection,
        &segments_sql,
        &where_sql,
        &values,
        ">",
        last_index,
    )?;
    let normalized_search = normalize_search_text(search);
    let mut remaining_bytes = RESULT_PAGE_BYTES;
    let prompts = rows
        .iter()
        .map(|row| {
            let budget = RESULT_ITEM_BYTES.min(remaining_bytes.max(1));
            let (payload, consumed) = indexed_prompt_value(row, &normalized_search, budget);
            remaining_bytes = remaining_bytes.saturating_sub(consumed);
            payload
        })
        .collect::<Vec<_>>();
    Ok(json!({
        "threadId": locator.id,
        "title": locator.title,
        "rolloutPath": locator.rollout_path.to_string_lossy(),
        "rolloutPaths": locator.rollout_paths.iter().map(|path| path.to_string_lossy().into_owned()).collect::<Vec<_>>(),
        "scope": scope,
        "search": search,
        "sourceType": if source_type.is_empty() { Value::Null } else { Value::String(source_type.to_string()) },
        "anchorIndex": query.anchor_index.max(1),
        "anchorOffset": anchor_offset,
        "hasEarlier": has_earlier,
        "hasLater": has_later,
        "prompts": prompts,
        "index": index_state,
    }))
}

struct IndexedPrompt {
    index: i64,
    line_number: i64,
    byte_offset: i64,
    timestamp: Option<String>,
    protocol: String,
    text: String,
    character_count: i64,
    source_type: String,
    source_label: String,
    visible_by_default: bool,
    pure_text: String,
    pure_character_count: i64,
    has_pure_text: bool,
    record_id: String,
    segment_order: i64,
}

fn query_indexed_prompts(
    connection: &Connection,
    sql: &str,
    values: &[SqlValue],
) -> Result<Vec<IndexedPrompt>, String> {
    let mut statement = connection.prepare(sql).map_err(sql_error)?;
    let rows = statement
        .query_map(params_from_iter(values.iter()), |row| {
            Ok(IndexedPrompt {
                index: row.get(0)?,
                line_number: row.get(1)?,
                byte_offset: row.get(2)?,
                timestamp: row.get(3)?,
                protocol: row.get(4)?,
                text: row.get(5)?,
                character_count: row.get(6)?,
                source_type: row.get(7)?,
                source_label: row.get(8)?,
                visible_by_default: row.get::<_, i64>(9)? != 0,
                pure_text: row.get(10)?,
                pure_character_count: row.get(11)?,
                has_pure_text: row.get::<_, i64>(12)? != 0,
                record_id: format!("{}:{}", row.get::<_, i64>(13)?, row.get::<_, i64>(14)?),
                segment_order: row.get(15)?,
            })
        })
        .map_err(sql_error)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(sql_error)
}

fn indexed_prompt_value(
    row: &IndexedPrompt,
    normalized_search: &str,
    budget: usize,
) -> (Value, usize) {
    let (text, text_truncated, pure_text, pure_text_truncated) = if row.pure_text == row.text {
        let (excerpt, truncated) = excerpt(&row.text, normalized_search, budget);
        (excerpt.clone(), truncated, excerpt, truncated)
    } else {
        let (text, text_truncated) = excerpt(&row.text, normalized_search, budget / 2);
        let (pure_text, pure_text_truncated) = excerpt(
            &row.pure_text,
            normalized_search,
            budget.saturating_sub(budget / 2),
        );
        (text, text_truncated, pure_text, pure_text_truncated)
    };
    let consumed = text.len()
        + if pure_text == text {
            0
        } else {
            pure_text.len()
        };
    (
        json!({
            "index": row.index,
            "lineNumber": row.line_number,
            "byteOffset": row.byte_offset,
            "recordId": row.record_id,
            "segmentOrder": row.segment_order,
            "ordinalExact": true,
            "timestamp": row.timestamp,
            "protocol": row.protocol,
            "text": text,
            "characterCount": row.character_count,
            "sourceType": row.source_type,
            "sourceLabel": row.source_label,
            "visibleByDefault": row.visible_by_default,
            "pureText": pure_text,
            "pureCharacterCount": row.pure_character_count,
            "hasPureText": row.has_pure_text,
            "textTruncated": text_truncated,
            "pureTextTruncated": pure_text_truncated,
        }),
        consumed,
    )
}

fn context_neighbor_exists(
    connection: &Connection,
    segments_sql: &str,
    where_sql: &str,
    values: &[SqlValue],
    operator: &str,
    index: i64,
) -> Result<bool, String> {
    let mut query_values = values.to_vec();
    query_values.push(SqlValue::Integer(index));
    let sql = format!(
        "{segments_sql} SELECT 1 FROM segments AS s JOIN prompts AS p ON p.file_id=s.file_id WHERE {where_sql} AND s.base+p.prompt_index {operator} ? LIMIT 1"
    );
    connection
        .query_row(&sql, params_from_iter(query_values.iter()), |_| Ok(()))
        .optional()
        .map(|value| value.is_some())
        .map_err(sql_error)
}

#[allow(clippy::too_many_arguments)]
fn read_persistent_prompt_page_inner(
    locator: &ThreadLocator,
    codex_home: &Path,
    query: &PromptPageQuery,
    scope: &str,
    source_type: &str,
    order: &str,
    search: &str,
    limit: usize,
    cancelled: Arc<AtomicBool>,
    warm_only: bool,
) -> Result<Option<Value>, String> {
    check_cancelled(&cancelled)?;
    let started = Instant::now();
    let database_path = prompt_index_database_path(codex_home)?;
    if warm_only && !database_path.is_file() {
        return Ok(None);
    }
    if let Some(parent) = database_path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| {
            format!(
                "cannot create prompt index directory {}: {error}",
                parent.display()
            )
        })?;
    }
    let lock_started = Instant::now();
    let _database_lock = if warm_only {
        let Some(lock) = DatabaseLock::try_exclusive(&database_path)? else { return Ok(None) };
        lock
    } else {
        DatabaseLock::exclusive_cancelled(&database_path, &cancelled)?
    };
    let lock_wait_ms = lock_started.elapsed().as_millis();
    check_cancelled(&cancelled)?;
    let open_started = Instant::now();
    let mut connection = connect(&database_path)?;
    install_cancellation(&connection, cancelled.clone());
    let database_open_ms = open_started.elapsed().as_millis();
    if warm_only {
        let paths = if locator.rollout_paths.is_empty() {
            std::slice::from_ref(&locator.rollout_path)
        } else { locator.rollout_paths.as_slice() };
        for path in paths {
            check_cancelled(&cancelled)?;
            let path = absolute_path(path)?;
            let Some(state) = read_file_state(&connection, &path.to_string_lossy())? else {
                return Ok(None);
            };
            let source = source_state(&path)?;
            if !state.complete && (state.scanned_offset == 0
                || source.size.saturating_sub(state.scanned_offset) > 8 * 1024 * 1024) {
                return Ok(None);
            }
        }
    }
    let index_started = Instant::now();
    let (segments, index_state) = advance_thread_index(
        &mut connection,
        locator,
        if query
            .cursor
            .as_deref()
            .is_some_and(|cursor| !cursor.is_empty())
        {
            0
        } else {
            query.scan_budget_ms.unwrap_or(100).min(5_000)
        },
        &cancelled,
    )?;
    let index_prepare_ms = index_started.elapsed().as_millis();
    check_cancelled(&cancelled)?;
    if warm_only && !index_state.complete {
        return Ok(None);
    }
    let query_started = Instant::now();
    let mut payload = read_page(
        &connection,
        &segments,
        scope,
        source_type,
        order,
        search,
        query.cursor.as_deref(),
        limit,
        &index_state,
        &cancelled,
    )?;
    let query_elapsed_ms = query_started.elapsed().as_millis();
    payload["threadId"] = Value::String(locator.id.clone());
    payload["title"] = Value::String(locator.title.clone());
    payload["rolloutPath"] = Value::String(locator.rollout_path.to_string_lossy().into_owned());
    payload["rolloutPaths"] = json!(locator
        .rollout_paths
        .iter()
        .map(|path| path.to_string_lossy().into_owned())
        .collect::<Vec<_>>());
    payload["requestId"] = Value::String(query.request_id.clone().unwrap_or_default());
    payload["scope"] = Value::String(scope.to_string());
    payload["search"] = Value::String(search.to_string());
    payload["sourceType"] = if source_type.is_empty() {
        Value::Null
    } else {
        Value::String(source_type.to_string())
    };
    payload["order"] = Value::String(order.to_string());
    payload["index"]["requestElapsedMs"] = json!(started.elapsed().as_millis());
    payload["index"]["lockWaitMs"] = json!(lock_wait_ms);
    payload["index"]["databaseOpenMs"] = json!(database_open_ms);
    payload["index"]["indexPrepareMs"] = json!(index_prepare_ms);
    payload["index"]["queryElapsedMs"] = json!(query_elapsed_ms);
    payload["index"]["warmReuse"] = json!(warm_only);
    Ok(Some(payload))
}

pub(super) fn prompt_index_status(codex_home: &Path) -> Result<Value, String> {
    let database_path = prompt_index_database_path(codex_home)?;
    let root = database_path
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| "prompt index root is unavailable".to_string())?;
    let databases = prompt_index_databases(&root)?;
    let max_total_bytes = configured_positive_u64(
        "CODEX_HOME_MANAGER_PROMPT_INDEX_MAX_TOTAL_BYTES",
        1024 * 1024 * 1024,
        1024 * 1024,
    );
    let max_idle_seconds = configured_positive_u64(
        "CODEX_HOME_MANAGER_PROMPT_INDEX_MAX_IDLE_SECONDS",
        30 * 24 * 60 * 60,
        60,
    );
    let total_size_bytes = databases
        .iter()
        .map(|path| database_size(path))
        .sum::<u64>();
    let mut active_database_count = 0usize;
    for path in &databases {
        if DatabaseLock::try_exclusive(path)?.is_none() {
            active_database_count += 1;
        }
    }
    let database = if database_path.is_file() {
        let size_bytes = database_size(&database_path);
        match DatabaseLock::try_exclusive(&database_path)? {
            None => Some(json!({
                "sizeBytes": size_bytes, "inUse": true, "activeOperations": 1,
                "readable": Value::Null, "inspectionState": "in_use",
                "lastAccessedAtMs": Value::Null, "schemaVersion": Value::Null,
                "sourceRolloutCount": Value::Null, "missingSourceRolloutCount": Value::Null,
                "promptCount": Value::Null, "timelineEventCount": Value::Null,
            })),
            Some(_lock) => Some(inspect_database(&database_path, size_bytes)?),
        }
    } else {
        None
    };
    Ok(json!({
        "databaseExists": database.is_some(),
        "database": database,
        "storage": {
            "rootPath": root.to_string_lossy(),
            "databaseCount": databases.len(),
            "activeDatabaseCount": active_database_count,
            "totalSizeBytes": total_size_bytes,
            "maxTotalBytes": max_total_bytes,
            "maxIdleSeconds": max_idle_seconds,
            "overCapacity": total_size_bytes > max_total_bytes,
        }
    }))
}

fn prompt_index_databases(root: &Path) -> Result<Vec<PathBuf>, String> {
    let entries = match std::fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(format!("cannot inspect prompt index storage: {error}")),
    };
    Ok(entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
                return false;
            };
            name.len() == 71
                && name.ends_with(".sqlite")
                && name[..64].bytes().all(|byte| byte.is_ascii_hexdigit())
        })
        .collect())
}

fn database_size(database_path: &Path) -> u64 {
    ["", "-wal", "-shm"]
        .iter()
        .map(|suffix| PathBuf::from(format!("{}{suffix}", database_path.to_string_lossy())))
        .filter_map(|path| path.metadata().ok().map(|metadata| metadata.len()))
        .sum()
}

fn inspect_database(database_path: &Path, size_bytes: u64) -> Result<Value, String> {
    let connection =
        match Connection::open_with_flags(database_path, OpenFlags::SQLITE_OPEN_READ_ONLY) {
            Ok(connection) => connection,
            Err(_) => {
                return Ok(json!({
                    "sizeBytes": size_bytes, "inUse": false, "activeOperations": 0,
                    "readable": false, "inspectionState": "unreadable",
                    "lastAccessedAtMs": Value::Null, "schemaVersion": Value::Null,
                    "sourceRolloutCount": Value::Null, "missingSourceRolloutCount": Value::Null,
                    "promptCount": Value::Null, "timelineEventCount": Value::Null,
                }))
            }
        };
    let schema_version: i64 = connection
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .map_err(sql_error)?;
    let source_rollout_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM prompt_files", [], |row| row.get(0))
        .map_err(sql_error)?;
    let missing_source_rollout_count: usize = {
        let mut statement = connection
            .prepare("SELECT path FROM prompt_files")
            .map_err(sql_error)?;
        let count = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(sql_error)?
            .filter_map(Result::ok)
            .filter(|path| !Path::new(path).is_file())
            .count();
        count
    };
    let prompt_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM prompts", [], |row| row.get(0))
        .map_err(sql_error)?;
    let timeline_event_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM timeline_events", [], |row| row.get(0))
        .map_err(sql_error)?;
    let last_accessed_ns: Option<i64> = connection
        .query_row(
            "SELECT last_accessed_ns FROM prompt_index_metadata WHERE singleton=1",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(sql_error)?;
    Ok(json!({
        "sizeBytes": size_bytes, "inUse": false, "activeOperations": 0,
        "readable": true, "inspectionState": "available",
        "lastAccessedAtMs": last_accessed_ns.map(|value| value / 1_000_000),
        "schemaVersion": schema_version, "sourceRolloutCount": source_rollout_count,
        "missingSourceRolloutCount": missing_source_rollout_count,
        "promptCount": prompt_count, "timelineEventCount": timeline_event_count,
    }))
}

fn configured_positive_u64(name: &str, default: u64, minimum: u64) -> u64 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(default)
        .max(minimum)
}

fn prompt_index_database_path(codex_home: &Path) -> Result<PathBuf, String> {
    let home = absolute_path(codex_home)?;
    let mut normalized = home.to_string_lossy().into_owned();
    if cfg!(windows) {
        normalized = normalized.replace('/', "\\").to_lowercase();
    }
    let database_name = format!("{:x}.sqlite", Sha256::digest(normalized.as_bytes()));
    let root = env::var_os("CODEX_HOME_MANAGER_PROMPT_INDEX_ROOT")
        .map(PathBuf::from)
        .or_else(|| {
            env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .map(|path| path.join("CodexHomeManager").join("prompt-indexes"))
        })
        .unwrap_or_else(|| PathBuf::from(".").join("prompt-indexes"));
    Ok(root.join(database_name))
}

fn absolute_path(path: &Path) -> Result<PathBuf, String> {
    if path.is_absolute() {
        Ok(path.to_path_buf())
    } else {
        env::current_dir()
            .map(|root| root.join(path))
            .map_err(|error| format!("cannot resolve {}: {error}", path.display()))
    }
}

fn connect(database_path: &Path) -> Result<Connection, String> {
    let connection = Connection::open(database_path).map_err(|error| {
        format!(
            "cannot open prompt index {}: {error}",
            database_path.display()
        )
    })?;
    connection
        .busy_timeout(std::time::Duration::from_secs(30))
        .map_err(sql_error)?;
    connection
        .execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=NORMAL;
             PRAGMA foreign_keys=ON;
             PRAGMA secure_delete=ON;",
        )
        .map_err(sql_error)?;
    ensure_schema(&connection)?;
    Ok(connection)
}

fn ensure_schema(connection: &Connection) -> Result<(), String> {
    let version: i64 = connection
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .map_err(sql_error)?;
    if version == 22 {
        migrate_classification(connection)?;
    }
    if version == SCHEMA_VERSION || version == 22 {
        connection
            .execute(
                "INSERT INTO prompt_index_metadata(singleton, last_accessed_ns) VALUES (1, ?1)
                 ON CONFLICT(singleton) DO UPDATE SET last_accessed_ns=excluded.last_accessed_ns",
                [now_ns()],
            )
            .map_err(sql_error)?;
        return Ok(());
    }
    if version != SCHEMA_VERSION {
        connection
            .execute_batch(
                "DROP TABLE IF EXISTS timeline_search_fts;
                 DROP TABLE IF EXISTS prompt_search_fts;
                 DROP TABLE IF EXISTS timeline_events;
                 DROP TABLE IF EXISTS prompts;
                 DROP TABLE IF EXISTS prompt_files;",
            )
            .map_err(sql_error)?;
    }
    connection
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS prompt_files (
                id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, device TEXT NOT NULL,
                inode TEXT NOT NULL, created_ns INTEGER NOT NULL, generation TEXT NOT NULL,
                observed_size INTEGER NOT NULL, observed_mtime_ns INTEGER NOT NULL,
                scanned_offset INTEGER NOT NULL, scanned_line_count INTEGER NOT NULL,
                boundary_hash TEXT NOT NULL, partial_offset INTEGER NOT NULL,
                partial_candidate INTEGER NOT NULL, complete INTEGER NOT NULL,
                timeline_scanned_offset INTEGER NOT NULL, timeline_scanned_line_count INTEGER NOT NULL,
                timeline_boundary_hash TEXT NOT NULL, timeline_partial_offset INTEGER NOT NULL,
                timeline_partial_candidate INTEGER NOT NULL, timeline_complete INTEGER NOT NULL,
                timeline_lineage_line_number INTEGER NOT NULL, timeline_lineage_text_hash TEXT NOT NULL,
                timeline_lineage_source_type TEXT NOT NULL, timeline_lineage_payload_type TEXT NOT NULL,
                timeline_lineage_kind TEXT NOT NULL, updated_at_ns INTEGER NOT NULL
             );
             CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES prompt_files(id) ON DELETE CASCADE,
                prompt_index INTEGER NOT NULL, line_number INTEGER NOT NULL, byte_offset INTEGER NOT NULL,
                timestamp TEXT, timestamp_ms INTEGER, protocol TEXT NOT NULL, text TEXT NOT NULL,
                search_text TEXT NOT NULL, character_count INTEGER NOT NULL, source_type TEXT NOT NULL,
                source_label TEXT NOT NULL, visible_by_default INTEGER NOT NULL, pure_text TEXT NOT NULL,
                pure_search_text TEXT NOT NULL, pure_character_count INTEGER NOT NULL,
                has_pure_text INTEGER NOT NULL, UNIQUE(file_id, prompt_index)
             );
             CREATE INDEX IF NOT EXISTS prompts_file_order_idx ON prompts(file_id, prompt_index);
             CREATE INDEX IF NOT EXISTS prompts_file_source_idx ON prompts(file_id, source_type, prompt_index);
             CREATE TABLE IF NOT EXISTS timeline_events (
                id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES prompt_files(id) ON DELETE CASCADE,
                event_index INTEGER NOT NULL, line_number INTEGER NOT NULL, byte_offset INTEGER NOT NULL,
                timestamp TEXT, timestamp_ms INTEGER, kind TEXT NOT NULL, label TEXT NOT NULL,
                text TEXT NOT NULL, search_text TEXT NOT NULL, character_count INTEGER NOT NULL,
                source_type TEXT NOT NULL, payload_type TEXT NOT NULL, phase TEXT NOT NULL,
                call_id TEXT NOT NULL, readable INTEGER NOT NULL, encrypted INTEGER NOT NULL,
                has_encrypted_content INTEGER NOT NULL, prompt_source_type TEXT NOT NULL,
                pure_text TEXT NOT NULL, pure_search_text TEXT NOT NULL,
                pure_character_count INTEGER NOT NULL, has_pure_text INTEGER NOT NULL,
                UNIQUE(file_id, event_index)
             );
             CREATE INDEX IF NOT EXISTS timeline_events_file_order_idx ON timeline_events(file_id, event_index);
             CREATE INDEX IF NOT EXISTS timeline_events_file_kind_idx ON timeline_events(file_id, kind, event_index);
             CREATE VIRTUAL TABLE IF NOT EXISTS prompt_search_fts USING fts5(
                file_id UNINDEXED, prompt_index UNINDEXED, search_text, pure_search_text, tokenize='trigram'
             );
             CREATE VIRTUAL TABLE IF NOT EXISTS timeline_search_fts USING fts5(
                file_id UNINDEXED, event_index UNINDEXED, search_text, pure_search_text, tokenize='trigram'
             );
             CREATE TABLE IF NOT EXISTS prompt_index_metadata (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1), last_accessed_ns INTEGER NOT NULL
             );",
        )
        .map_err(sql_error)?;
    if version != SCHEMA_VERSION {
        connection
            .pragma_update(None, "user_version", SCHEMA_VERSION)
            .map_err(sql_error)?;
    }
    connection
        .execute(
            "INSERT INTO prompt_index_metadata(singleton, last_accessed_ns) VALUES (1, ?1)
             ON CONFLICT(singleton) DO UPDATE SET last_accessed_ns=excluded.last_accessed_ns",
            [now_ns()],
        )
        .map_err(sql_error)?;
    Ok(())
}

fn migrate_classification(connection: &Connection) -> Result<(), String> {
    // Only derived classifications change. Retain source identity, generation,
    // scan progress and search coordinates instead of rereading the rollout.
    connection.execute_batch("SAVEPOINT classification_revision").map_err(sql_error)?;
    let result = (|| {
        for (table, order_column, source_column, label_column, search_table) in [
            ("prompts", "prompt_index", "source_type", "source_label", "prompt_search_fts"),
            ("timeline_events", "event_index", "prompt_source_type", "label", "timeline_search_fts"),
        ] {
            let mut pure_changed = false;
            let mut statement = connection.prepare(&format!(
                "SELECT id,file_id,{order_column},text,{source_column},{label_column},pure_text,pure_character_count,character_count FROM {table}
                 WHERE {source_column} IN ('user','attachment','browser','internal','goal') ORDER BY id"
            )).map_err(sql_error)?;
            let mut rows = statement.query([]).map_err(sql_error)?;
            let mut update = connection.prepare(&format!(
                "UPDATE {table} SET {source_column}=?1,{label_column}=?2,pure_text=?3,pure_search_text=?4,pure_character_count=?5,has_pure_text=?6,{}=?7 WHERE id=?8",
                if table == "prompts" { "visible_by_default" } else { "kind" }
            )).map_err(sql_error)?;
            while let Some(row) = rows.next().map_err(sql_error)? {
                let text: String = row.get(3).map_err(sql_error)?;
                let old_source: String = row.get(4).map_err(sql_error)?;
                let old_label: String = row.get(5).map_err(sql_error)?;
                let old_pure: String = row.get(6).map_err(sql_error)?;
                let classification = classify_prompt(&text);
                if classification.source_type == old_source && classification.source_label == old_label && classification.pure_text == old_pure { continue; }
                let pure_count: i64 = if classification.pure_text == old_pure { row.get(7).map_err(sql_error)? }
                    else if classification.pure_text == text { row.get(8).map_err(sql_error)? }
                    else { classification.pure_text.chars().count() as i64 };
                let pure_search = normalize_search_text(&classification.pure_text);
                let display = if table == "prompts" { SqlValue::Integer(classification.visible_by_default as i64) }
                    else { SqlValue::Text(if classification.visible_by_default { "user" } else { "context" }.to_string()) };
                update.execute(params![classification.source_type, classification.source_label, classification.pure_text, pure_search,
                    pure_count, classification.has_pure_text as i64, display, row.get::<_, i64>(0).map_err(sql_error)?]).map_err(sql_error)?;
                if classification.pure_text != old_pure {
                    pure_changed = true;
                }
            }
            drop(rows);
            drop(statement);
            if pure_changed {
                let coordinate = format!("p.file_id=CAST({search_table}.file_id AS INTEGER) AND p.{order_column}=CAST({search_table}.{order_column} AS INTEGER)");
                connection.execute(&format!(
                    "UPDATE {search_table} SET pure_search_text=(SELECT p.pure_search_text FROM {table} p WHERE {coordinate})
                     WHERE EXISTS(SELECT 1 FROM {table} p WHERE {coordinate} AND p.pure_search_text<>{search_table}.pure_search_text)"
                ), []).map_err(sql_error)?;
            }
        }
        let mut statement = connection.prepare("SELECT id,phase FROM timeline_events WHERE kind IN ('commentary','assistant') ORDER BY id").map_err(sql_error)?;
        let mut rows = statement.query([]).map_err(sql_error)?;
        let mut update = connection.prepare("UPDATE timeline_events SET kind=?1,label=?2 WHERE id=?3").map_err(sql_error)?;
        while let Some(row) = rows.next().map_err(sql_error)? {
            let phase: String = row.get(1).map_err(sql_error)?;
            let (kind, label) = assistant_stage(&phase);
            update.execute(params![kind, label, row.get::<_, i64>(0).map_err(sql_error)?]).map_err(sql_error)?;
        }
        connection.pragma_update(None, "user_version", SCHEMA_VERSION).map_err(sql_error)?;
        Ok::<(), String>(())
    })();
    match result {
        Ok(()) => connection.execute_batch("RELEASE classification_revision").map_err(sql_error),
        Err(error) => {
            connection.execute_batch("ROLLBACK TO classification_revision; RELEASE classification_revision").map_err(sql_error)?;
            Err(error)
        }
    }
}

fn install_cancellation(connection: &Connection, cancelled: Arc<AtomicBool>) {
    let _ = connection.progress_handler(
        1_000,
        Some(move || cancelled.load(std::sync::atomic::Ordering::Acquire)),
    );
}

fn advance_index(
    connection: &mut Connection,
    rollout_path: &Path,
    scan_budget_ms: u64,
    cancelled: &AtomicBool,
) -> Result<IndexState, String> {
    let started = Instant::now();
    let path = absolute_path(rollout_path)?;
    let path_text = path.to_string_lossy().into_owned();
    let source = source_state(&path)?;
    let (mut state, reset) = prepare_file_state(connection, &path, &path_text, &source)?;
    let scan_start_offset = state.scanned_offset.max(state.partial_offset);
    if !reset
        && state.complete
        && state.scanned_offset >= source.size
        && state.observed_size == source.size
        && state.observed_mtime_ns == source.modified_ns
    {
        return Ok(index_state(
            &state,
            &source,
            false,
            scan_start_offset,
            0,
            started.elapsed().as_millis(),
        ));
    }
    let mut prompt_index: i64 = connection
        .query_row(
            "SELECT COALESCE(MAX(prompt_index), 0) FROM prompts WHERE file_id=?1",
            [state.id],
            |row| row.get(0),
        )
        .map_err(sql_error)?;
    let mut recent = load_recent(connection, state.id)?;
    let file = File::open(&path)
        .map_err(|error| format!("cannot open rollout {}: {error}", path.display()))?;
    let mapped = if source.size == 0 {
        None
    } else {
        Some(
            unsafe { MmapOptions::new().map(&file) }
                .map_err(|error| format!("cannot map rollout {}: {error}", path.display()))?,
        )
    };
    let bytes = mapped.as_deref().unwrap_or(&[]);
    let mut cursor = state.partial_offset.max(state.scanned_offset) as usize;
    let mut line_start = state.scanned_offset as usize;
    let mut partial_candidate = state.partial_candidate;
    let mut pending = Vec::new();
    let mut pending_bytes = 0usize;
    let mut added = 0usize;
    let mut reached_budget = false;

    while cursor < bytes.len() {
        check_cancelled(cancelled)?;
        let chunk_end = (cursor + SCAN_CHUNK_BYTES).min(bytes.len());
        let mut search_at = cursor;
        while search_at < chunk_end {
            if let Some(relative) = memchr(b'\n', &bytes[search_at..chunk_end]) {
                let line_end = search_at + relative;
                let raw = &bytes[line_start..line_end];
                state.scanned_line_count += 1;
                if raw.len() <= MAX_RECORD_BYTES && prompt_candidate(raw) {
                    if let Some(pending_prompt) = parse_prompt(
                        raw,
                        prompt_index + 1,
                        state.scanned_line_count as usize,
                        line_start,
                        &recent,
                    ) {
                        let canonical_text = if pending_prompt.record.has_pure_text {
                            pending_prompt.record.pure_text.clone()
                        } else {
                            pending_prompt.record.text.clone()
                        };
                        recent.push_back((
                            canonical_text,
                            pending_prompt.record.timestamp.clone(),
                            pending_prompt.record.protocol.clone(),
                            pending_prompt.record.line_number,
                        ));
                        while recent.len() > 8 {
                            recent.pop_front();
                        }
                        prompt_index += 1;
                        pending_bytes += pending_prompt.record.text.len()
                            + pending_prompt.record.pure_text.len();
                        pending.push(pending_prompt);
                        added += 1;
                    }
                }
                state.scanned_offset = (line_end + 1) as u64;
                state.partial_offset = 0;
                state.partial_candidate = false;
                line_start = line_end + 1;
                search_at = line_end + 1;
                partial_candidate = false;
                if pending.len() >= COMMIT_RECORDS || pending_bytes >= COMMIT_BYTES {
                    commit_progress(connection, &mut state, &source, &path, &mut pending)?;
                    pending_bytes = 0;
                }
            } else {
                let segment = &bytes[search_at..chunk_end];
                partial_candidate |= prompt_candidate(segment);
                search_at = chunk_end;
            }
        }
        cursor = chunk_end;
        if line_start < cursor {
            state.partial_offset = cursor as u64;
            state.partial_candidate = partial_candidate;
        }
        if started.elapsed().as_millis() >= scan_budget_ms as u128 {
            reached_budget = true;
            break;
        }
    }

    if !reached_budget && cursor == bytes.len() && line_start < bytes.len() {
        let raw = &bytes[line_start..];
        state.scanned_line_count += 1;
        if raw.len() <= MAX_RECORD_BYTES && prompt_candidate(raw) {
            if let Some(pending_prompt) = parse_prompt(
                raw,
                prompt_index + 1,
                state.scanned_line_count as usize,
                line_start,
                &recent,
            ) {
                pending.push(pending_prompt);
                added += 1;
            }
        }
        state.scanned_offset = bytes.len() as u64;
        state.partial_offset = 0;
        state.partial_candidate = false;
    }
    state.complete =
        !reached_budget && state.partial_offset == 0 && state.scanned_offset >= source.size;
    commit_progress(connection, &mut state, &source, &path, &mut pending)?;
    Ok(index_state(
        &state,
        &source,
        reset,
        scan_start_offset,
        added,
        started.elapsed().as_millis(),
    ))
}

fn advance_thread_index(
    connection: &mut Connection,
    locator: &ThreadLocator,
    scan_budget_ms: u64,
    cancelled: &AtomicBool,
) -> Result<(Vec<IndexedSegment>, IndexState), String> {
    let paths = if locator.rollout_paths.is_empty() {
        vec![locator.rollout_path.clone()]
    } else {
        locator.rollout_paths.clone()
    };
    let per_segment_budget = if scan_budget_ms == 0 {
        0
    } else {
        (scan_budget_ms / paths.len().max(1) as u64).max(1)
    };
    let mut indexed = Vec::with_capacity(paths.len());
    for path in paths {
        check_cancelled(cancelled)?;
        let absolute = absolute_path(&path)?;
        let path_text = absolute.to_string_lossy().into_owned();
        let state = if per_segment_budget == 0 {
            let source = source_state(&absolute)?;
            let (file_state, reset) =
                prepare_file_state(connection, &absolute, &path_text, &source)?;
            index_state(&file_state, &source, reset, file_state.scanned_offset, 0, 0)
        } else {
            advance_index(connection, &path, per_segment_budget, cancelled)?
        };
        let file_id: i64 = connection
            .query_row(
                "SELECT id FROM prompt_files WHERE path=?1",
                [&path_text],
                |row| row.get(0),
            )
            .map_err(sql_error)?;
        let prompt_count: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM prompts WHERE file_id=?1",
                [file_id],
                |row| row.get(0),
            )
            .map_err(sql_error)?;
        indexed.push((path, file_id, prompt_count, state));
    }

    let active_position = indexed
        .iter()
        .position(|(path, _, _, _)| same_rollout_path(path, &locator.rollout_path))
        .unwrap_or(indexed.len().saturating_sub(1));
    let mut aggregate = indexed[active_position].3.clone();
    let mut generation = Sha256::new();
    let mut cursor_generation = Sha256::new();
    let mut base = 0i64;
    let mut segments = Vec::with_capacity(indexed.len());
    aggregate.file_size = 0;
    aggregate.file_mtime_ns = 0;
    aggregate.scanned_bytes = 0;
    aggregate.scanned_lines = 0;
    aggregate.scan_start_offset = 0;
    aggregate.scan_added_prompts = 0;
    aggregate.complete = true;
    aggregate.reset = false;
    aggregate.elapsed_ms = 0;
    aggregate.segment_count = indexed.len();
    for (position, (path, file_id, prompt_count, state)) in indexed.into_iter().enumerate() {
        generation.update(path.to_string_lossy().as_bytes());
        generation.update(state.generation.as_bytes());
        cursor_generation.update(path.to_string_lossy().as_bytes());
        cursor_generation.update(state.generation.as_bytes());
        // Appending to the active rollout only extends its ordinal space; it does not
        // invalidate an existing cursor. Older segment growth shifts later bases and
        // therefore still changes the aggregate generation.
        if position != active_position {
            generation.update(prompt_count.to_le_bytes());
        }
        segments.push(IndexedSegment {
            file_id,
            base,
            prompt_count,
        });
        base += prompt_count;
        aggregate.file_size = aggregate.file_size.saturating_add(state.file_size);
        aggregate.file_mtime_ns = aggregate.file_mtime_ns.max(state.file_mtime_ns);
        aggregate.scanned_bytes = aggregate.scanned_bytes.saturating_add(state.scanned_bytes);
        aggregate.scanned_lines = aggregate.scanned_lines.saturating_add(state.scanned_lines);
        aggregate.scan_start_offset = aggregate
            .scan_start_offset
            .saturating_add(state.scan_start_offset);
        aggregate.scan_added_prompts = aggregate
            .scan_added_prompts
            .saturating_add(state.scan_added_prompts);
        aggregate.complete &= state.complete;
        aggregate.reset |= state.reset;
        aggregate.elapsed_ms = aggregate.elapsed_ms.saturating_add(state.elapsed_ms);
    }
    aggregate.generation = format!("{:x}", generation.finalize());
    aggregate.cursor_generation = format!("{:x}", cursor_generation.finalize());
    Ok((segments, aggregate))
}

fn same_rollout_path(left: &Path, right: &Path) -> bool {
    let left = left.to_string_lossy();
    let right = right.to_string_lossy();
    if cfg!(windows) {
        left.replace('/', "\\")
            .eq_ignore_ascii_case(&right.replace('/', "\\"))
    } else {
        left == right
    }
}

fn parse_prompt(
    raw: &[u8],
    prompt_index: i64,
    line_number: usize,
    byte_offset: usize,
    recent: &VecDeque<RecentPrompt>,
) -> Option<PendingPrompt> {
    let item = serde_json::from_slice::<Value>(raw).ok()?;
    let (text, protocol) = prompt_text_from_item(&item)?;
    let text = text.trim().to_string();
    if text.is_empty() {
        return None;
    }
    let timestamp = item
        .get("timestamp")
        .and_then(Value::as_str)
        .map(str::to_string);
    let classification = classify_prompt(&text);
    let canonical_text = if classification.has_pure_text {
        &classification.pure_text
    } else {
        &text
    };
    if is_cross_protocol_duplicate(
        canonical_text,
        timestamp.as_deref(),
        &protocol,
        line_number,
        recent,
    ) {
        return None;
    }
    let search_text = normalize_search_text(&text);
    let pure_search_text = normalize_search_text(&classification.pure_text);
    Some(PendingPrompt {
        timestamp_ms: timestamp.as_deref().and_then(timestamp_millis),
        search_text,
        pure_search_text,
        record: PromptRecord {
            index: prompt_index as usize,
            line_number,
            byte_offset,
            ordinal_exact: true,
            timestamp,
            character_count: text.chars().count(),
            text,
            protocol,
            source_type: classification.source_type,
            source_label: classification.source_label,
            visible_by_default: classification.visible_by_default,
            pure_character_count: classification.pure_text.chars().count(),
            has_pure_text: classification.has_pure_text,
            pure_text: classification.pure_text,
        },
    })
}

fn prepare_file_state(
    connection: &mut Connection,
    path: &Path,
    path_text: &str,
    source: &SourceState,
) -> Result<(FileState, bool), String> {
    let existing = read_file_state(connection, path_text)?;
    let mut reset = existing.is_none();
    if let Some(state) = existing.as_ref() {
        let legacy_identity = state.device == "dev:0" && state.inode == "ino:0";
        let identity_changed = if legacy_identity {
            state.created_ns != source.created_ns
        } else {
            state.device != source.device || state.inode != source.inode
        };
        reset |= identity_changed
            || source.size < state.scanned_offset
            || source.size < state.timeline_scanned_offset
            || source.size < state.partial_offset
            || source.size < state.timeline_partial_offset
            || (source.size == state.observed_size
                && source.modified_ns != state.observed_mtime_ns);
        if !reset && state.scanned_offset > 0 {
            reset |= boundary_hash(path, state.scanned_offset)? != state.boundary_hash;
        }
        if !reset && state.timeline_scanned_offset > 0 {
            reset |=
                boundary_hash(path, state.timeline_scanned_offset)? != state.timeline_boundary_hash;
        }
        if !reset && legacy_identity {
            // Bootstrap legacy zero identities only after their existing metadata
            // and boundary checks pass; subsequent replacements use handle identity.
            connection
                .execute(
                    "UPDATE prompt_files SET device=?1,inode=?2 WHERE id=?3",
                    params![source.device, source.inode, state.id],
                )
                .map_err(sql_error)?;
        }
    }
    if reset {
        reset_file_state(connection, path_text, source, existing.as_ref())?;
    }
    read_file_state(connection, path_text)?
        .map(|state| (state, reset))
        .ok_or_else(|| "failed to initialize prompt index metadata".to_string())
}

fn reset_file_state(
    connection: &mut Connection,
    path_text: &str,
    source: &SourceState,
    existing: Option<&FileState>,
) -> Result<(), String> {
    let generation = Uuid::new_v4().simple().to_string();
    let empty_hash = format!("{:x}", Sha256::digest([]));
    let transaction = connection.transaction().map_err(sql_error)?;
    if let Some(state) = existing {
        transaction
            .execute("DELETE FROM prompt_search_fts WHERE file_id=?1", [state.id])
            .map_err(sql_error)?;
        transaction
            .execute(
                "DELETE FROM timeline_search_fts WHERE file_id=?1",
                [state.id],
            )
            .map_err(sql_error)?;
        transaction
            .execute("DELETE FROM prompts WHERE file_id=?1", [state.id])
            .map_err(sql_error)?;
        transaction
            .execute("DELETE FROM timeline_events WHERE file_id=?1", [state.id])
            .map_err(sql_error)?;
        transaction
            .execute(
                "UPDATE prompt_files SET device=?8, inode=?9, created_ns=?1,
             generation=?2, observed_size=?3, observed_mtime_ns=?4,
             scanned_offset=0, scanned_line_count=0, boundary_hash=?5,
             partial_offset=0, partial_candidate=0, complete=0,
             timeline_scanned_offset=0, timeline_scanned_line_count=0,
             timeline_boundary_hash=?5, timeline_partial_offset=0,
             timeline_partial_candidate=0, timeline_complete=0,
             timeline_lineage_line_number=0, timeline_lineage_text_hash='',
             timeline_lineage_source_type='', timeline_lineage_payload_type='',
             timeline_lineage_kind='', updated_at_ns=?6 WHERE id=?7",
                params![
                    source.created_ns,
                    generation,
                    source.size as i64,
                    source.modified_ns,
                    empty_hash,
                    now_ns(),
                    state.id,
                    source.device,
                    source.inode
                ],
            )
            .map_err(sql_error)?;
    } else {
        transaction.execute(
            "INSERT INTO prompt_files(
               path,device,inode,created_ns,generation,observed_size,observed_mtime_ns,
               scanned_offset,scanned_line_count,boundary_hash,partial_offset,partial_candidate,complete,
               timeline_scanned_offset,timeline_scanned_line_count,timeline_boundary_hash,
               timeline_partial_offset,timeline_partial_candidate,timeline_complete,
               timeline_lineage_line_number,timeline_lineage_text_hash,timeline_lineage_source_type,
               timeline_lineage_payload_type,timeline_lineage_kind,updated_at_ns
             ) VALUES(?1,?8,?9,?2,?3,?4,?5,0,0,?6,0,0,0,0,0,?6,0,0,0,0,'','','','',?7)",
            params![path_text, source.created_ns, generation, source.size as i64, source.modified_ns, empty_hash, now_ns(), source.device, source.inode],
        ).map_err(sql_error)?;
    }
    transaction.commit().map_err(sql_error)
}

fn read_file_state(connection: &Connection, path_text: &str) -> Result<Option<FileState>, String> {
    connection
        .query_row(
            "SELECT id,device,inode,created_ns,generation,observed_size,observed_mtime_ns,
                    scanned_offset,scanned_line_count,boundary_hash,partial_offset,
                    partial_candidate,complete,timeline_scanned_offset,
                    timeline_scanned_line_count,timeline_boundary_hash,timeline_partial_offset,
                    timeline_partial_candidate,timeline_complete,timeline_lineage_line_number,
                    timeline_lineage_text_hash,timeline_lineage_source_type,
                    timeline_lineage_payload_type,timeline_lineage_kind
             FROM prompt_files WHERE path=?1",
            [path_text],
            |row| {
                Ok(FileState {
                    id: row.get(0)?,
                    device: row.get(1)?,
                    inode: row.get(2)?,
                    created_ns: row.get(3)?,
                    generation: row.get(4)?,
                    observed_size: row.get::<_, i64>(5)? as u64,
                    observed_mtime_ns: row.get(6)?,
                    scanned_offset: row.get::<_, i64>(7)? as u64,
                    scanned_line_count: row.get::<_, i64>(8)? as u64,
                    boundary_hash: row.get(9)?,
                    partial_offset: row.get::<_, i64>(10)? as u64,
                    partial_candidate: row.get::<_, i64>(11)? != 0,
                    complete: row.get::<_, i64>(12)? != 0,
                    timeline_scanned_offset: row.get::<_, i64>(13)? as u64,
                    timeline_scanned_line_count: row.get::<_, i64>(14)? as u64,
                    timeline_boundary_hash: row.get(15)?,
                    timeline_partial_offset: row.get::<_, i64>(16)? as u64,
                    timeline_partial_candidate: row.get::<_, i64>(17)? != 0,
                    timeline_complete: row.get::<_, i64>(18)? != 0,
                    timeline_lineage_line_number: row.get::<_, i64>(19)? as u64,
                    timeline_lineage_text_hash: row.get(20)?,
                    timeline_lineage_source_type: row.get(21)?,
                    timeline_lineage_payload_type: row.get(22)?,
                    timeline_lineage_kind: row.get(23)?,
                })
            },
        )
        .optional()
        .map_err(sql_error)
}

fn commit_progress(
    connection: &mut Connection,
    state: &mut FileState,
    source: &SourceState,
    path: &Path,
    pending: &mut Vec<PendingPrompt>,
) -> Result<(), String> {
    state.boundary_hash = boundary_hash(path, state.scanned_offset)?;
    let transaction = connection.transaction().map_err(sql_error)?;
    for item in pending.drain(..) {
        let record = item.record;
        transaction.execute(
            "INSERT INTO prompts(file_id,prompt_index,line_number,byte_offset,timestamp,timestamp_ms,
              protocol,text,search_text,character_count,source_type,source_label,visible_by_default,
              pure_text,pure_search_text,pure_character_count,has_pure_text)
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17)",
            params![state.id, record.index as i64, record.line_number as i64, record.byte_offset as i64,
                record.timestamp, item.timestamp_ms, record.protocol, record.text, item.search_text,
                record.character_count as i64, record.source_type, record.source_label,
                record.visible_by_default as i64, record.pure_text, item.pure_search_text,
                record.pure_character_count as i64, record.has_pure_text as i64],
        ).map_err(sql_error)?;
        transaction
            .execute(
                "INSERT INTO prompt_search_fts(file_id,prompt_index,search_text,pure_search_text)
             VALUES(?1,?2,?3,?4)",
                params![
                    state.id,
                    record.index as i64,
                    item.search_text,
                    item.pure_search_text
                ],
            )
            .map_err(sql_error)?;
    }
    transaction
        .execute(
            "UPDATE prompt_files SET observed_size=?1, observed_mtime_ns=?2, scanned_offset=?3,
         scanned_line_count=?4, boundary_hash=?5, partial_offset=?6, partial_candidate=?7,
         complete=?8, updated_at_ns=?9 WHERE id=?10",
            params![
                source.size as i64,
                source.modified_ns,
                state.scanned_offset as i64,
                state.scanned_line_count as i64,
                state.boundary_hash,
                state.partial_offset as i64,
                state.partial_candidate as i64,
                state.complete as i64,
                now_ns(),
                state.id
            ],
        )
        .map_err(sql_error)?;
    transaction.commit().map_err(sql_error)
}

fn load_recent(connection: &Connection, file_id: i64) -> Result<VecDeque<RecentPrompt>, String> {
    let mut statement = connection
        .prepare(
            "SELECT text,pure_text,has_pure_text,timestamp,protocol,line_number
             FROM prompts WHERE file_id=?1 ORDER BY prompt_index DESC LIMIT 8",
        )
        .map_err(sql_error)?;
    let rows = statement
        .query_map([file_id], |row| {
            let text: String = row.get(0)?;
            let pure_text: String = row.get(1)?;
            let has_pure_text: i64 = row.get(2)?;
            Ok((
                if has_pure_text != 0 { pure_text } else { text },
                row.get(3)?,
                row.get(4)?,
                row.get::<_, i64>(5)? as usize,
            ))
        })
        .map_err(sql_error)?;
    let mut values = rows.collect::<Result<Vec<_>, _>>().map_err(sql_error)?;
    values.reverse();
    Ok(values.into())
}

#[allow(clippy::too_many_arguments)]
fn read_page(
    connection: &Connection,
    segments: &[IndexedSegment],
    scope: &str,
    source_type: &str,
    order: &str,
    search: &str,
    cursor: Option<&str>,
    limit: usize,
    index_state: &IndexState,
    cancelled: &AtomicBool,
) -> Result<Value, String> {
    let normalized_search = normalize_search_text(search.trim());
    let signature = query_signature(scope, &normalized_search, source_type, order);
    let cursor_state =
        decode_cursor(cursor, &index_state.cursor_generation, &signature, segments)?;
    let cursor_position = cursor_state.map(|(position, _)| position);
    let complete_snapshot = order == "desc" && cursor_state.map_or(index_state.complete, |(_, complete)| complete);
    let (segments_sql, mut values) = segment_cte(segments);
    let (scope_sql, scope_values) = scope_clause(scope)?;
    let search_column = if scope == "pure" {
        "pure_search_text"
    } else {
        "search_text"
    };
    let use_fts = !normalized_search.is_empty() && normalized_search.chars().count() >= 3;
    let mut clauses = vec![scope_sql];
    values.extend(scope_values);
    if let Some(position) = cursor_position {
        clauses.push(
            if order == "asc" {
                "s.base+p.prompt_index > ?"
            } else {
                "s.base+p.prompt_index < ?"
            }
            .to_string(),
        );
        values.push(SqlValue::Integer(position));
    }
    if !source_type.is_empty() {
        clauses.push("p.source_type=?".to_string());
        values.push(SqlValue::Text(source_type.to_string()));
    }
    if !normalized_search.is_empty() {
        if use_fts {
            clauses.push("prompt_search_fts MATCH ?".to_string());
            values.push(SqlValue::Text(fts_literal(
                &normalized_search,
                search_column,
            )));
        } else {
            clauses.push(format!("instr(p.{search_column}, ?) > 0"));
            values.push(SqlValue::Text(normalized_search.clone()));
        }
    }
    let from_sql = if use_fts {
        // Keep MATCH outside the ordered message lookup, as in the Python reader.
        "segments AS s JOIN prompts AS p ON p.file_id=s.file_id JOIN prompt_search_fts AS f ON CAST(f.file_id AS INTEGER)=p.file_id AND CAST(f.prompt_index AS INTEGER)=p.prompt_index"
    } else {
        "segments AS s JOIN prompts AS p ON p.file_id=s.file_id"
    };
    let where_sql = clauses
        .iter()
        .map(|value| format!("({value})"))
        .collect::<Vec<_>>()
        .join(" AND ");
    let safe_limit = limit.clamp(1, 500);
    let sql = format!(
        "{segments_sql} SELECT s.base+p.prompt_index,p.line_number,p.byte_offset,p.timestamp,p.protocol,p.text,
         p.character_count,p.source_type,p.source_label,p.visible_by_default,p.pure_text,
         p.pure_character_count,p.has_pure_text,p.file_id,p.prompt_index,s.segment_order FROM {from_sql} WHERE {where_sql}
         ORDER BY s.base+p.prompt_index {} LIMIT ?",
        order.to_uppercase()
    );
    values.push(SqlValue::Integer((safe_limit + 1) as i64));
    let mut statement = connection
        .prepare(&sql)
        .map_err(cancel_sql_error(cancelled))?;
    let mut rows = statement
        .query(params_from_iter(values.iter()))
        .map_err(cancel_sql_error(cancelled))?;
    let mut prompts = Vec::new();
    let mut remaining_bytes = RESULT_PAGE_BYTES;
    let mut has_indexed_more = false;
    let mut last_index = cursor_position.unwrap_or(0);
    let mut last_segment = None;
    while let Some(row) = rows.next().map_err(cancel_sql_error(cancelled))? {
        check_cancelled(cancelled)?;
        if prompts.len() >= safe_limit || remaining_bytes == 0 {
            has_indexed_more = true;
            break;
        }
        let item_budget = RESULT_ITEM_BYTES.min(remaining_bytes);
        let prompt_index: i64 = row.get(0).map_err(sql_error)?;
        let text: String = row.get(5).map_err(sql_error)?;
        let pure_text: String = row.get(10).map_err(sql_error)?;
        let (text_excerpt, text_truncated, pure_excerpt, pure_truncated) = if pure_text == text {
            let (excerpt, truncated) = excerpt(&text, &normalized_search, item_budget);
            (excerpt.clone(), truncated, excerpt, truncated)
        } else {
            let (text_excerpt, text_truncated) =
                excerpt(&text, &normalized_search, item_budget / 2);
            let (pure_excerpt, pure_truncated) = excerpt(
                &pure_text,
                &normalized_search,
                item_budget - item_budget / 2,
            );
            (text_excerpt, text_truncated, pure_excerpt, pure_truncated)
        };
        remaining_bytes = remaining_bytes.saturating_sub(text_excerpt.len());
        if pure_excerpt != text_excerpt {
            remaining_bytes = remaining_bytes.saturating_sub(pure_excerpt.len());
        }
        prompts.push(json!({
            "index": prompt_index, "lineNumber": row.get::<_, i64>(1).map_err(sql_error)?,
            "byteOffset": row.get::<_, i64>(2).map_err(sql_error)?, "ordinalExact": true,
            "recordId": format!("{}:{}", row.get::<_, i64>(13).map_err(sql_error)?, row.get::<_, i64>(14).map_err(sql_error)?),
            "segmentOrder": row.get::<_, i64>(15).map_err(sql_error)?,
            "timestamp": row.get::<_, Option<String>>(3).map_err(sql_error)?,
            "protocol": row.get::<_, String>(4).map_err(sql_error)?, "text": text_excerpt,
            "characterCount": row.get::<_, i64>(6).map_err(sql_error)?,
            "sourceType": row.get::<_, String>(7).map_err(sql_error)?,
            "sourceLabel": row.get::<_, String>(8).map_err(sql_error)?,
            "visibleByDefault": row.get::<_, i64>(9).map_err(sql_error)? != 0,
            "pureText": pure_excerpt, "pureCharacterCount": row.get::<_, i64>(11).map_err(sql_error)?,
            "hasPureText": row.get::<_, i64>(12).map_err(sql_error)? != 0,
            "textTruncated": text_truncated, "pureTextTruncated": pure_truncated,
        }));
        last_index = row.get(14).map_err(sql_error)?;
        last_segment = Some(row.get::<_, i64>(13).map_err(sql_error)?);
    }
    drop(rows);
    drop(statement);
    let match_count = count_matches(
        connection,
        from_sql,
        scope,
        source_type,
        &normalized_search,
        search_column,
        use_fts,
        segments,
        cancelled,
    )?;
    let summary = summary(connection, segments)?;
    // A newer append is not an older page in an already complete descending snapshot.
    let has_more = has_indexed_more || (!index_state.complete && !complete_snapshot);
    let next_cursor = has_more.then(|| {
        if last_segment.is_none() {
            if let Some(cursor) = cursor {
                return cursor.to_string();
            }
        }
        encode_cursor(
            &index_state.cursor_generation,
            &signature,
            last_index,
            last_segment,
            complete_snapshot,
        )
    });
    Ok(json!({
        "promptCount": summary.prompt_count,
        "purePromptCount": summary.pure_prompt_count,
        "visiblePromptCount": summary.visible_prompt_count,
        "hiddenPromptCount": summary.prompt_count.saturating_sub(summary.visible_prompt_count),
        "sourceCounts": summary.source_counts,
        "matchCount": match_count,
        "matchCountComplete": index_state.complete,
        "prompts": prompts,
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "index": index_state,
    }))
}

#[allow(clippy::too_many_arguments)]
fn count_matches(
    connection: &Connection,
    from_sql: &str,
    scope: &str,
    source_type: &str,
    normalized_search: &str,
    search_column: &str,
    use_fts: bool,
    segments: &[IndexedSegment],
    cancelled: &AtomicBool,
) -> Result<i64, String> {
    let (segments_sql, mut values) = segment_cte(segments);
    let (scope_sql, scope_values) = scope_clause(scope)?;
    let mut clauses = vec![scope_sql];
    values.extend(scope_values);
    if !source_type.is_empty() {
        clauses.push("p.source_type=?".to_string());
        values.push(SqlValue::Text(source_type.to_string()));
    }
    if !normalized_search.is_empty() {
        if use_fts {
            clauses.push("prompt_search_fts MATCH ?".to_string());
            values.push(SqlValue::Text(fts_literal(
                normalized_search,
                search_column,
            )));
        } else {
            clauses.push(format!("instr(p.{search_column}, ?) > 0"));
            values.push(SqlValue::Text(normalized_search.to_string()));
        }
    }
    let sql = format!(
        "{segments_sql} SELECT COUNT(*) FROM {from_sql} WHERE {}",
        clauses
            .iter()
            .map(|value| format!("({value})"))
            .collect::<Vec<_>>()
            .join(" AND ")
    );
    connection
        .query_row(&sql, params_from_iter(values.iter()), |row| row.get(0))
        .map_err(cancel_sql_error(cancelled))
}

struct Summary {
    prompt_count: usize,
    pure_prompt_count: usize,
    visible_prompt_count: usize,
    source_counts: HashMap<String, usize>,
}

fn summary(connection: &Connection, segments: &[IndexedSegment]) -> Result<Summary, String> {
    let (segments_sql, values) = segment_cte(segments);
    let (prompt_count, pure_prompt_count, visible_prompt_count): (i64, i64, i64) = connection
        .query_row(
            &format!("{segments_sql} SELECT COUNT(*),COALESCE(SUM(p.has_pure_text),0),COALESCE(SUM(p.visible_by_default),0)
             FROM segments AS s JOIN prompts AS p ON p.file_id=s.file_id"),
            params_from_iter(values.iter()),
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .map_err(sql_error)?;
    let (segments_sql, values) = segment_cte(segments);
    let mut statement = connection
        .prepare(&format!("{segments_sql} SELECT p.source_type,COUNT(*) FROM segments AS s JOIN prompts AS p ON p.file_id=s.file_id GROUP BY p.source_type"))
        .map_err(sql_error)?;
    let counts = statement
        .query_map(params_from_iter(values.iter()), |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)? as usize))
        })
        .map_err(sql_error)?
        .collect::<Result<HashMap<_, _>, _>>()
        .map_err(sql_error)?;
    Ok(Summary {
        prompt_count: prompt_count as usize,
        pure_prompt_count: pure_prompt_count as usize,
        visible_prompt_count: visible_prompt_count as usize,
        source_counts: counts,
    })
}

fn segment_cte(segments: &[IndexedSegment]) -> (String, Vec<SqlValue>) {
    let placeholders = std::iter::repeat_n("(?,?,?)", segments.len())
        .collect::<Vec<_>>()
        .join(",");
    let mut values = Vec::with_capacity(segments.len() * 3);
    for (order, segment) in segments.iter().enumerate() {
        values.push(SqlValue::Integer(segment.file_id));
        values.push(SqlValue::Integer(segment.base));
        values.push(SqlValue::Integer(order as i64));
    }
    (
        format!("WITH segments(file_id,base,segment_order) AS (VALUES {placeholders})"),
        values,
    )
}

fn scope_clause(scope: &str) -> Result<(String, Vec<SqlValue>), String> {
    match scope {
        "pure" => Ok(("p.has_pure_text=1".to_string(), vec![])),
        "all" => Ok(("1=1".to_string(), vec![])),
        "automation" | "heartbeat" => Ok((
            "p.source_type=?".to_string(),
            vec![SqlValue::Text("automation".to_string())],
        )),
        "delegation" => Ok((
            "p.source_type=?".to_string(),
            vec![SqlValue::Text("delegation".to_string())],
        )),
        "with_agents" => Ok((
            "(p.visible_by_default=1 OR p.source_type='subagent')".to_string(),
            vec![],
        )),
        "visible" => Ok(("p.visible_by_default=1".to_string(), vec![])),
        _ => Err(format!("unsupported prompt scope: {scope}")),
    }
}

fn query_signature(scope: &str, normalized_search: &str, source_type: &str, order: &str) -> String {
    let payload = BTreeMap::from([
        ("scope", scope),
        ("search", normalized_search),
        ("sourceType", source_type),
        ("order", order),
    ]);
    let encoded = serde_json::to_vec(&payload).expect("query signature is serializable");
    format!("{:x}", Sha256::digest(encoded))[..20].to_string()
}

fn encode_cursor(
    generation: &str,
    query: &str,
    position: i64,
    segment_file_id: Option<i64>,
    complete_snapshot: bool,
) -> String {
    URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(&PersistentCursor {
            generation: generation.to_string(),
            query: query.to_string(),
            position,
            segment_file_id,
            complete_snapshot,
        })
        .expect("persistent prompt cursor is serializable"),
    )
}

fn decode_cursor(
    cursor: Option<&str>,
    generation: &str,
    query: &str,
    segments: &[IndexedSegment],
) -> Result<Option<(i64, bool)>, String> {
    let Some(cursor) = cursor.filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let bytes = URL_SAFE_NO_PAD
        .decode(cursor)
        .map_err(|_| "invalid prompt cursor".to_string())?;
    let payload: PersistentCursor =
        serde_json::from_slice(&bytes).map_err(|_| "invalid prompt cursor".to_string())?;
    if payload.generation != generation {
        return Err("prompt cursor is stale because the rollout index was rebuilt".to_string());
    }
    if payload.query != query {
        return Err("prompt cursor does not match the current filters".to_string());
    }
    if payload.position < 0 {
        return Err("invalid prompt cursor position".to_string());
    }
    if payload.segment_file_id.is_none() && payload.position == 0 {
        return Ok(None);
    }
    // Segment-local ordinals survive incremental discovery in earlier segments.
    // The generation still rejects replaced, truncated or reordered source files.
    let base = match payload.segment_file_id {
        Some(file_id) => {
            let segment = segments
                .iter()
                .find(|segment| segment.file_id == file_id)
                .ok_or_else(|| "prompt cursor references an unknown rollout segment".to_string())?;
            if payload.position == 0 || payload.position > segment.prompt_count {
                return Err("invalid prompt cursor position".to_string());
            }
            segment.base
        }
        None => 0,
    };
    base.checked_add(payload.position)
        .map(|position| Some((position, payload.complete_snapshot)))
        .ok_or_else(|| "invalid prompt cursor position".to_string())
}

fn parse_record_id(value: &str) -> Result<(i64, i64), String> {
    let (file, position) = value
        .split_once(':')
        .ok_or_else(|| "prompt context anchor is stale: invalid record identity".to_string())?;
    let invalid = || "prompt context anchor is stale: invalid record identity".to_string();
    Ok((
        file.parse().map_err(|_| invalid())?,
        position.parse().map_err(|_| invalid())?,
    ))
}

fn normalize_search_text(value: &str) -> String {
    if value.is_ascii() {
        return value.to_ascii_lowercase();
    }
    value
        .nfkd()
        .case_fold()
        .filter(|character| {
            !matches!(
                get_general_category(*character),
                GeneralCategory::NonspacingMark
                    | GeneralCategory::SpacingMark
                    | GeneralCategory::EnclosingMark
            )
        })
        .collect()
}

fn fts_literal(normalized_query: &str, column: &str) -> String {
    format!("{column} : \"{}\"", normalized_query.replace('"', "\"\""))
}

fn excerpt(text: &str, normalized_query: &str, byte_limit: usize) -> (String, bool) {
    if text.len() <= byte_limit {
        return (text.to_string(), false);
    }
    let limit = byte_limit.max(1);
    let match_span = normalized_original_span(text, normalized_query);
    let center = match_span
        .map(|(start, end)| start + (end - start) / 2)
        .unwrap_or(0);
    let mut start = center.saturating_sub(limit / 2).min(text.len());
    let mut end = (start + limit).min(text.len());
    while start > 0 && !text.is_char_boundary(start) {
        start -= 1;
    }
    while end > start && !text.is_char_boundary(end) {
        end -= 1;
    }
    if end - start < limit && end == text.len() {
        start = end.saturating_sub(limit);
        while start > 0 && !text.is_char_boundary(start) {
            start -= 1;
        }
    }
    let prefix = if start > 0 { "..." } else { "" };
    let suffix = if end < text.len() { "..." } else { "" };
    (format!("{prefix}{}{suffix}", &text[start..end]), true)
}

fn normalized_original_span(value: &str, normalized_query: &str) -> Option<(usize, usize)> {
    if normalized_query.is_empty() {
        return None;
    }
    if normalized_query.is_ascii() {
        if let Some(start) = memmem::find(value.as_bytes(), normalized_query.as_bytes()) {
            return Some((start, start + normalized_query.len()));
        }
        if let Some(start) = value
            .as_bytes()
            .windows(normalized_query.len())
            .position(|window| window.eq_ignore_ascii_case(normalized_query.as_bytes()))
        {
            return Some((start, start + normalized_query.len()));
        }
    } else if let Some(start) = value.find(normalized_query) {
        return Some((start, start + normalized_query.len()));
    }
    let mut normalized = Vec::new();
    let mut spans = Vec::new();
    for (start, character) in value.char_indices() {
        let end = start + character.len_utf8();
        for folded in normalize_search_text(&character.to_string()).chars() {
            let mut bytes = [0u8; 4];
            let encoded = folded.encode_utf8(&mut bytes).as_bytes();
            normalized.extend_from_slice(encoded);
            spans.extend(std::iter::repeat_n((start, end), encoded.len()));
        }
    }
    let query = normalized_query.as_bytes();
    normalized
        .windows(query.len())
        .position(|window| window == query)
        .map(|start| (spans[start].0, spans[start + query.len() - 1].1))
}

fn source_state(path: &Path) -> Result<SourceState, String> {
    let file = File::open(path)
        .map_err(|error| format!("cannot open rollout {}: {error}", path.display()))?;
    let metadata = file
        .metadata()
        .map_err(|error| format!("cannot stat rollout {}: {error}", path.display()))?;
    #[cfg(windows)]
    let (device, inode) = {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::{
            FileIdInfo, GetFileInformationByHandleEx, FILE_ID_INFO,
        };
        let mut information: FILE_ID_INFO = unsafe { std::mem::zeroed() };
        if unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle(),
                FileIdInfo,
                (&mut information as *mut FILE_ID_INFO).cast(),
                std::mem::size_of::<FILE_ID_INFO>() as u32,
            )
        } == 0
        {
            return Err(format!(
                "cannot identify rollout {}: {}",
                path.display(),
                std::io::Error::last_os_error()
            ));
        }
        (
            information.VolumeSerialNumber,
            u128::from_le_bytes(information.FileId.Identifier),
        )
    };
    #[cfg(unix)]
    let (device, inode) = {
        use std::os::unix::fs::MetadataExt;
        (metadata.dev(), metadata.ino())
    };
    #[cfg(not(any(windows, unix)))]
    return Err("rollout file identity is unsupported on this platform".to_string());
    Ok(SourceState {
        size: metadata.len(),
        modified_ns: system_time_ns(metadata.modified().ok()),
        created_ns: system_time_ns(metadata.created().ok()),
        device: format!("dev:{device}"),
        inode: format!("ino:{inode}"),
    })
}

fn system_time_ns(value: Option<SystemTime>) -> i64 {
    value
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos().min(i64::MAX as u128) as i64)
        .unwrap_or_default()
}

fn boundary_hash(path: &Path, offset: u64) -> Result<String, String> {
    if offset == 0 {
        return Ok(format!("{:x}", Sha256::digest([])));
    }
    let start = offset.saturating_sub(BOUNDARY_BYTES as u64);
    let mut file = File::open(path)
        .map_err(|error| format!("cannot open rollout boundary {}: {error}", path.display()))?;
    file.seek(SeekFrom::Start(start))
        .map_err(|error| format!("cannot seek rollout boundary: {error}"))?;
    let mut bytes = vec![0u8; (offset - start) as usize];
    file.read_exact(&mut bytes)
        .map_err(|error| format!("cannot read rollout boundary: {error}"))?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

fn index_state(
    state: &FileState,
    source: &SourceState,
    reset: bool,
    scan_start_offset: u64,
    added: usize,
    elapsed_ms: u128,
) -> IndexState {
    IndexState {
        kind: "prompts",
        generation: state.generation.clone(),
        cursor_generation: state.generation.clone(),
        file_identity: FileIdentity {
            device: state.device.clone(),
            inode: state.inode.clone(),
            created_ns: state.created_ns,
        },
        file_size: source.size,
        file_mtime_ns: source.modified_ns,
        scanned_bytes: state.scanned_offset.max(state.partial_offset),
        scanned_lines: state.scanned_line_count,
        scan_start_offset,
        scan_added_prompts: added,
        complete: state.complete && state.scanned_offset >= source.size,
        reset,
        elapsed_ms,
        strategy: "persistent",
        runtime: "rust",
        query_mode: "indexed",
        segment_count: 1,
    }
}

fn now_ns() -> i64 {
    system_time_ns(Some(SystemTime::now()))
}

fn cancel_sql_error(cancelled: &AtomicBool) -> impl FnOnce(rusqlite::Error) -> String + '_ {
    move |error| {
        if cancelled.load(std::sync::atomic::Ordering::Acquire)
            || error.to_string().to_lowercase().contains("interrupted")
        {
            PROMPT_REQUEST_CANCELLED.to_string()
        } else {
            sql_error(error)
        }
    }
}

fn sql_error(error: rusqlite::Error) -> String {
    format!("prompt index database error: {error}")
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct GlobalTimelineCursor {
    revision: String,
    query: String,
    timestamp_ms: i64,
    file_id: i64,
    event_index: i64,
}

fn has_json_type(raw: &[u8], expected: &[u8]) -> bool {
    let mut cursor = 0usize;
    while let Some(relative) = memmem::find(&raw[cursor..], b"\"type\"") {
        let mut position = cursor + relative + b"\"type\"".len();
        while position < raw.len() && raw[position].is_ascii_whitespace() {
            position += 1;
        }
        if raw.get(position) != Some(&b':') {
            cursor = position.min(raw.len());
            continue;
        }
        position += 1;
        while position < raw.len() && raw[position].is_ascii_whitespace() {
            position += 1;
        }
        if raw.get(position) == Some(&b'\"')
            && raw.get(position + 1..position + 1 + expected.len()) == Some(expected)
            && raw.get(position + 1 + expected.len()) == Some(&b'\"')
        {
            return true;
        }
        cursor = position.min(raw.len());
    }
    false
}

fn timeline_candidate(raw: &[u8]) -> bool {
    let envelope = &raw[..raw.len().min(512)];
    let head = &raw[..raw.len().min(4_096)];
    if has_json_type(envelope, b"user_message") {
        return true;
    }
    if has_json_type(envelope, b"event_msg") {
        return has_json_type(head, b"user_message") || has_json_type(head, b"agent_message");
    }
    has_json_type(envelope, b"response_item")
        && (has_json_type(head, b"message") || memmem::find(raw, b"codex_delegation").is_some())
}

fn conversation_event(raw: &[u8], byte_offset: usize) -> Option<TimelineEvent> {
    if raw.len() > MAX_RECORD_BYTES || !timeline_candidate(raw) {
        return None;
    }
    let item = serde_json::from_slice::<Value>(raw).ok()?;
    let item_type = item.get("type").and_then(Value::as_str).unwrap_or_default();
    let payload = item.get("payload").and_then(Value::as_object);
    let payload_type = payload
        .and_then(|value| value.get("type"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let role = payload
        .and_then(|value| value.get("role"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let supported = item_type == "user_message"
        || (item_type == "event_msg" && matches!(payload_type, "user_message" | "agent_message"))
        || (item_type == "response_item"
            && payload_type == "message"
            && matches!(role, "user" | "assistant"))
        || incoming_delegation_text(&item).is_some();
    if !supported {
        return None;
    }
    let mut event = event_from_item(&item, byte_offset)?;
    let natural_language = matches!(event.kind.as_str(), "user" | "commentary" | "assistant" | "assistant_unclassified")
        || (event.kind == "context" && !event.prompt_source_type.is_empty());
    if !natural_language {
        return None;
    }
    event.character_count = event.text.chars().count();
    Some(event)
}

type RecentTimelineEvent = (usize, String, String, i64, i64);

fn load_recent_timeline(
    connection: &Connection,
    file_id: i64,
) -> Result<HashMap<String, VecDeque<RecentTimelineEvent>>, String> {
    let mut statement = connection
        .prepare(
            "SELECT kind,byte_offset,text,source_type,timestamp_ms,event_index
             FROM timeline_events WHERE file_id=?1 ORDER BY event_index DESC LIMIT 512",
        )
        .map_err(sql_error)?;
    let rows = statement
        .query_map([file_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                (
                    row.get::<_, i64>(1)? as usize,
                    row.get::<_, String>(2)?.trim().to_string(),
                    row.get::<_, String>(3)?,
                    row.get::<_, i64>(4)?,
                    row.get::<_, i64>(5)?,
                ),
            ))
        })
        .map_err(sql_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql_error)?;
    let mut recent: HashMap<String, VecDeque<RecentTimelineEvent>> = HashMap::new();
    for (kind, record) in rows.into_iter().rev() {
        recent.entry(kind).or_default().push_back(record);
    }
    Ok(recent)
}

fn timeline_duplicate_and_superseded(
    event: &TimelineEvent,
    recent: &VecDeque<RecentTimelineEvent>,
) -> (bool, Vec<i64>) {
    let text = event.text.trim();
    let mut superseded = Vec::new();
    for (offset, prior_text, prior_source, prior_timestamp_ms, event_index) in recent {
        if prior_source == &event.source_type {
            continue;
        }
        let near_in_time = event.timestamp_ms > 0
            && *prior_timestamp_ms > 0
            && event.timestamp_ms.abs_diff(*prior_timestamp_ms) <= 2_000;
        let near_in_file = event.timestamp_ms == 0
            && *prior_timestamp_ms == 0
            && event.byte_offset.abs_diff(*offset) < 1_000_000;
        if !near_in_time && !near_in_file {
            continue;
        }
        if text == prior_text || (!text.is_empty() && prior_text.starts_with(text)) {
            return (true, Vec::new());
        }
        if !prior_text.is_empty() && text.len() > prior_text.len() && text.starts_with(prior_text) {
            superseded.push(*event_index);
        }
    }
    (false, superseded)
}

fn commit_timeline_progress(
    connection: &mut Connection,
    state: &mut FileState,
    source: &SourceState,
    path: &Path,
    pending: &mut Vec<PendingTimelineEvent>,
    deleted_event_indices: &mut Vec<i64>,
) -> Result<(), String> {
    state.timeline_boundary_hash = boundary_hash(path, state.timeline_scanned_offset)?;
    let transaction = connection.transaction().map_err(sql_error)?;
    {
        let mut delete_fts = transaction
            .prepare_cached(
                "DELETE FROM timeline_search_fts WHERE file_id=?1 AND CAST(event_index AS INTEGER)=?2",
            )
            .map_err(sql_error)?;
        let mut delete_event = transaction
            .prepare_cached("DELETE FROM timeline_events WHERE file_id=?1 AND event_index=?2")
            .map_err(sql_error)?;
        for event_index in deleted_event_indices.drain(..) {
            delete_fts
                .execute(params![state.id, event_index])
                .map_err(sql_error)?;
            delete_event
                .execute(params![state.id, event_index])
                .map_err(sql_error)?;
        }
    }
    {
        let mut insert_event = transaction
            .prepare_cached(
                "INSERT INTO timeline_events(file_id,event_index,line_number,byte_offset,timestamp,timestamp_ms,
                 kind,label,text,search_text,character_count,source_type,payload_type,phase,call_id,readable,
                 encrypted,has_encrypted_content,prompt_source_type,pure_text,pure_search_text,
                 pure_character_count,has_pure_text)
                 VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23)",
            )
            .map_err(sql_error)?;
        let mut insert_fts = transaction
            .prepare_cached(
                "INSERT INTO timeline_search_fts(file_id,event_index,search_text,pure_search_text)
                 VALUES(?1,?2,?3,?4)",
            )
            .map_err(sql_error)?;
        for item in pending.drain(..) {
            let event = item.event;
            let timestamp = event.timestamp.as_str().map(str::to_string);
            insert_event
                .execute(params![
                    state.id,
                    item.event_index,
                    item.line_number as i64,
                    event.byte_offset as i64,
                    timestamp,
                    event.timestamp_ms,
                    event.kind,
                    event.label,
                    event.text,
                    item.search_text,
                    event.character_count as i64,
                    event.source_type,
                    event.payload_type,
                    event.phase,
                    event.call_id,
                    event.readable as i64,
                    event.encrypted as i64,
                    event.has_encrypted_content as i64,
                    event.prompt_source_type,
                    event.pure_text,
                    item.pure_search_text,
                    event.pure_character_count as i64,
                    event.has_pure_text as i64,
                ])
                .map_err(sql_error)?;
            insert_fts
                .execute(params![
                    state.id,
                    item.event_index,
                    item.search_text,
                    item.pure_search_text
                ])
                .map_err(sql_error)?;
        }
    }
    transaction
        .execute(
            "UPDATE prompt_files SET observed_size=?1,observed_mtime_ns=?2,
             timeline_scanned_offset=?3,timeline_scanned_line_count=?4,timeline_boundary_hash=?5,
             timeline_partial_offset=?6,timeline_partial_candidate=?7,timeline_complete=?8,
             timeline_lineage_line_number=?9,timeline_lineage_text_hash=?10,
             timeline_lineage_source_type=?11,timeline_lineage_payload_type=?12,
             timeline_lineage_kind=?13,updated_at_ns=?14 WHERE id=?15",
            params![
                source.size as i64,
                source.modified_ns,
                state.timeline_scanned_offset as i64,
                state.timeline_scanned_line_count as i64,
                state.timeline_boundary_hash,
                state.timeline_partial_offset as i64,
                state.timeline_partial_candidate as i64,
                state.timeline_complete as i64,
                state.timeline_lineage_line_number as i64,
                state.timeline_lineage_text_hash,
                state.timeline_lineage_source_type,
                state.timeline_lineage_payload_type,
                state.timeline_lineage_kind,
                now_ns(),
                state.id,
            ],
        )
        .map_err(sql_error)?;
    transaction.commit().map_err(sql_error)
}

fn advance_timeline_index(
    connection: &mut Connection,
    rollout_path: &Path,
    scan_budget_ms: u64,
    cancelled: &AtomicBool,
) -> Result<(FileState, SourceState, usize, u128), String> {
    let started = Instant::now();
    let path = absolute_path(rollout_path)?;
    let path_text = path.to_string_lossy().into_owned();
    let source = source_state(&path)?;
    let (mut state, _) = prepare_file_state(connection, &path, &path_text, &source)?;
    if state.timeline_complete
        && state.timeline_scanned_offset >= source.size
        && state.observed_size == source.size
        && state.observed_mtime_ns == source.modified_ns
    {
        return Ok((state, source, 0, started.elapsed().as_millis()));
    }
    let mut event_index: i64 = connection
        .query_row(
            "SELECT COALESCE(MAX(event_index),0) FROM timeline_events WHERE file_id=?1",
            [state.id],
            |row| row.get(0),
        )
        .map_err(sql_error)?;
    let mut recent = load_recent_timeline(connection, state.id)?;
    let file = File::open(&path)
        .map_err(|error| format!("cannot open rollout {}: {error}", path.display()))?;
    let mapped = if source.size == 0 {
        None
    } else {
        Some(
            unsafe { MmapOptions::new().map(&file) }
                .map_err(|error| format!("cannot map rollout {}: {error}", path.display()))?,
        )
    };
    let bytes = mapped.as_deref().unwrap_or(&[]);
    let mut cursor = state
        .timeline_partial_offset
        .max(state.timeline_scanned_offset) as usize;
    let mut line_start = state.timeline_scanned_offset as usize;
    let mut pending = Vec::new();
    let mut deleted_event_indices = Vec::new();
    let mut pending_bytes = 0usize;
    let mut added = 0usize;
    let mut reached_budget = false;

    'scan: while cursor < bytes.len() {
        check_cancelled(cancelled)?;
        let chunk_end = (cursor + SCAN_CHUNK_BYTES).min(bytes.len());
        let mut search_at = cursor;
        while search_at < chunk_end {
            let Some(relative) = memchr(b'\n', &bytes[search_at..chunk_end]) else {
                search_at = chunk_end;
                continue;
            };
            let line_end = search_at + relative;
            let raw = &bytes[line_start..line_end];
            state.timeline_scanned_line_count += 1;
            if let Some(event) = conversation_event(raw, line_start) {
                let event_kind = event.kind.clone();
                let event_text = event.text.trim().to_string();
                let event_text_hash = format!("{:x}", Sha256::digest(event_text.as_bytes()));
                let adjacent_mirror = event.source_type == "event_msg"
                    && event.payload_type == "agent_message"
                    && event.phase.is_empty()
                    && state.timeline_lineage_line_number + 1 == state.timeline_scanned_line_count
                    && state.timeline_lineage_source_type == "response_item"
                    && state.timeline_lineage_payload_type == "message"
                    && matches!(
                        state.timeline_lineage_kind.as_str(),
                        "commentary" | "assistant" | "assistant_unclassified"
                    )
                    && state.timeline_lineage_text_hash == event_text_hash;
                let event_history = recent.entry(event_kind.clone()).or_default();
                let (duplicate, superseded) = if adjacent_mirror {
                    (true, Vec::new())
                } else {
                    timeline_duplicate_and_superseded(&event, event_history)
                };
                if !superseded.is_empty() {
                    pending.retain(|item: &PendingTimelineEvent| {
                        !superseded.contains(&item.event_index)
                    });
                    deleted_event_indices.extend(superseded.iter().copied());
                    event_history.retain(|record| !superseded.contains(&record.4));
                }
                if !duplicate {
                    event_index += 1;
                    pending_bytes =
                        pending_bytes.saturating_add(event.text.len() + event.pure_text.len());
                    pending.push(PendingTimelineEvent {
                        event_index,
                        line_number: state.timeline_scanned_line_count,
                        search_text: normalize_search_text(&event.text),
                        pure_search_text: normalize_search_text(&event.pure_text),
                        event: event.clone(),
                    });
                    event_history.push_back((
                        event.byte_offset,
                        event_text.clone(),
                        event.source_type.clone(),
                        event.timestamp_ms,
                        event_index,
                    ));
                    while event_history.len() > 256 {
                        event_history.pop_front();
                    }
                    added += 1;
                }
                state.timeline_lineage_line_number = state.timeline_scanned_line_count;
                state.timeline_lineage_text_hash = event_text_hash;
                state.timeline_lineage_source_type = event.source_type;
                state.timeline_lineage_payload_type = event.payload_type;
                state.timeline_lineage_kind = event_kind;
            }
            state.timeline_scanned_offset = (line_end + 1) as u64;
            state.timeline_partial_offset = 0;
            state.timeline_partial_candidate = false;
            line_start = line_end + 1;
            search_at = line_end + 1;
            if pending.len() >= TIMELINE_COMMIT_RECORDS || pending_bytes >= TIMELINE_COMMIT_BYTES {
                commit_timeline_progress(
                    connection,
                    &mut state,
                    &source,
                    &path,
                    &mut pending,
                    &mut deleted_event_indices,
                )?;
                pending_bytes = 0;
            }
            if started.elapsed().as_millis() >= scan_budget_ms as u128 {
                reached_budget = true;
                break 'scan;
            }
        }
        cursor = chunk_end;
        if line_start < cursor {
            state.timeline_partial_offset = cursor as u64;
            state.timeline_partial_candidate = timeline_candidate(&bytes[line_start..cursor]);
        }
        if started.elapsed().as_millis() >= scan_budget_ms as u128 {
            reached_budget = true;
            break;
        }
    }
    if !reached_budget && cursor == bytes.len() && line_start < bytes.len() {
        let raw = &bytes[line_start..];
        state.timeline_scanned_line_count += 1;
        if let Some(event) = conversation_event(raw, line_start) {
            event_index += 1;
            pending.push(PendingTimelineEvent {
                event_index,
                line_number: state.timeline_scanned_line_count,
                search_text: normalize_search_text(&event.text),
                pure_search_text: normalize_search_text(&event.pure_text),
                event,
            });
            added += 1;
        }
        state.timeline_scanned_offset = bytes.len() as u64;
        state.timeline_partial_offset = 0;
        state.timeline_partial_candidate = false;
    }
    state.timeline_complete = !reached_budget
        && state.timeline_partial_offset == 0
        && state.timeline_scanned_offset >= source.size;
    commit_timeline_progress(
        connection,
        &mut state,
        &source,
        &path,
        &mut pending,
        &mut deleted_event_indices,
    )?;
    Ok((state, source, added, started.elapsed().as_millis()))
}

fn timeline_kind_clause(kind: &str) -> Result<&'static str, String> {
    match kind {
        "conversation" => Ok("(e.kind IN ('user','commentary','assistant','assistant_unclassified') OR (e.kind='context' AND e.prompt_source_type<>''))"),
        "user" => Ok("e.prompt_source_type IN ('user','attachment','browser','subagent','automation','delegation','goal')"),
        "commentary" => Ok("e.kind='commentary'"),
        "assistant" => Ok("e.kind='assistant'"),
        "assistant_unclassified" => Ok("e.kind='assistant_unclassified'"),
        _ => Err(format!("unsupported global timeline kind: {kind}")),
    }
}

fn timeline_source_clause(source_filter: &str) -> Result<&'static str, String> {
    match source_filter {
        "all" => Ok("1=1"),
        "pure" | "focused" => Ok("(e.kind NOT IN ('user','context') OR (e.prompt_source_type IN ('user','attachment','browser') AND e.has_pure_text=1))"),
        "with_agents" => Ok("(e.kind NOT IN ('user','context') OR e.prompt_source_type IN ('user','attachment','browser','subagent'))"),
        "automation" => Ok("e.prompt_source_type='automation'"),
        "delegation" => Ok("e.prompt_source_type='delegation'"),
        _ => Err(format!("unsupported global timeline source filter: {source_filter}")),
    }
}

fn direct_event_in_scope(
    event: &TimelineEvent,
    kind: &str,
    source_filter: &str,
) -> Result<bool, String> {
    let kind_matches = match kind {
        "conversation" => {
            matches!(event.kind.as_str(), "user" | "commentary" | "assistant" | "assistant_unclassified")
                || (event.kind == "context" && !event.prompt_source_type.is_empty())
        }
        "user" => {
            matches!(event.prompt_source_type.as_str(), "user" | "attachment" | "browser" | "subagent" | "automation" | "delegation" | "goal")
        }
        "commentary" => event.kind == "commentary",
        "assistant" => event.kind == "assistant",
        "assistant_unclassified" => event.kind == "assistant_unclassified",
        _ => return Err(format!("unsupported global timeline kind: {kind}")),
    };
    if !kind_matches {
        return Ok(false);
    }
    let source_matches = match source_filter {
        "all" => true,
        "pure" | "focused" => {
            !matches!(event.kind.as_str(), "user" | "context")
                || (matches!(
                    event.prompt_source_type.as_str(),
                    "user" | "attachment" | "browser"
                ) && event.has_pure_text)
        }
        "with_agents" => {
            !matches!(event.kind.as_str(), "user" | "context")
                || matches!(
                    event.prompt_source_type.as_str(),
                    "user" | "attachment" | "browser" | "subagent"
                )
        }
        "automation" => event.prompt_source_type == "automation",
        "delegation" => event.prompt_source_type == "delegation",
        _ => {
            return Err(format!(
                "unsupported global timeline source filter: {source_filter}"
            ))
        }
    };
    Ok(source_matches)
}

fn scan_direct_segment(
    segment: &GlobalTimelineSegment,
    normalized_search: &str,
    query: &GlobalTimelineSearchQuery,
    cancelled: &AtomicBool,
) -> Result<Vec<DirectMatch>, String> {
    check_cancelled(cancelled)?;
    let file = File::open(&segment.path)
        .map_err(|error| format!("cannot open rollout {}: {error}", segment.path.display()))?;
    if file.metadata().map_err(|error| error.to_string())?.len() == 0 {
        return Ok(Vec::new());
    }
    let mapped = unsafe { MmapOptions::new().map(&file) }
        .map_err(|error| format!("cannot map rollout {}: {error}", segment.path.display()))?;
    let bytes = mapped.as_ref();
    let mut line_start = 0usize;
    let mut line_number = 0u64;
    let mut event_index = 0i64;
    let mut recent: HashMap<String, VecDeque<RecentTimelineEvent>> = HashMap::new();
    let mut lineage_line_number = 0u64;
    let mut lineage_text_hash = String::new();
    let mut lineage_source_type = String::new();
    let mut lineage_payload_type = String::new();
    let mut lineage_kind = String::new();
    let mut found: Vec<(i64, DirectMatch)> = Vec::new();
    let path_text = segment.path.to_string_lossy().into_owned();
    let path_hash = format!("{:x}", Sha256::digest(path_text.as_bytes()));

    while line_start < bytes.len() {
        if line_start % (16 * 1024 * 1024) < 4096 {
            check_cancelled(cancelled)?;
        }
        let line_end = memchr(b'\n', &bytes[line_start..])
            .map(|relative| line_start + relative)
            .unwrap_or(bytes.len());
        line_number += 1;
        let raw = &bytes[line_start..line_end];
        if let Some(event) = conversation_event(raw, line_start) {
            let event_kind = event.kind.clone();
            let event_text = event.text.trim().to_string();
            let event_text_hash = format!("{:x}", Sha256::digest(event_text.as_bytes()));
            let adjacent_mirror = event.source_type == "event_msg"
                && event.payload_type == "agent_message"
                && event.phase.is_empty()
                && lineage_line_number + 1 == line_number
                && lineage_source_type == "response_item"
                && lineage_payload_type == "message"
                && matches!(lineage_kind.as_str(), "commentary" | "assistant" | "assistant_unclassified")
                && lineage_text_hash == event_text_hash;
            let event_history = recent.entry(event_kind.clone()).or_default();
            let (duplicate, superseded) = if adjacent_mirror {
                (true, Vec::new())
            } else {
                timeline_duplicate_and_superseded(&event, event_history)
            };
            if !superseded.is_empty() {
                found.retain(|(index, _)| !superseded.contains(index));
                event_history.retain(|record| !superseded.contains(&record.4));
            }
            if !duplicate {
                event_index += 1;
                if direct_event_in_scope(&event, &query.kind, &query.source_filter)? {
                    let selected_text =
                        if matches!(query.source_filter.as_str(), "pure" | "focused")
                            && !event.pure_text.is_empty()
                        {
                            &event.pure_text
                        } else {
                            &event.text
                        };
                    if normalize_search_text(selected_text).contains(normalized_search) {
                        let (excerpt, truncated) =
                            excerpt(selected_text, normalized_search, RESULT_ITEM_BYTES);
                        let value = json!({
                            "id": format!("{}-{}-{}", segment.thread_id, &path_hash[..16], event_index),
                            "threadId": segment.thread_id,
                            "threadTitle": segment.thread_title,
                            "projectPath": segment.project_path,
                            "projectLabel": segment.project_label,
                            "threadKind": segment.thread_kind,
                            "archived": segment.archived,
                            "rolloutPath": path_text,
                            "eventIndex": event_index,
                            "lineNumber": line_number,
                            "byteOffset": event.byte_offset,
                            "timestamp": event.timestamp,
                            "timestampMs": event.timestamp_ms,
                            "kind": event.kind,
                            "label": event.label,
                            "text": excerpt,
                            "characterCount": event.character_count,
                            "sourceType": event.source_type,
                            "payloadType": event.payload_type,
                            "phase": event.phase,
                            "callId": event.call_id,
                            "readable": event.readable,
                            "encrypted": event.encrypted,
                            "hasEncryptedContent": event.has_encrypted_content,
                            "promptSourceType": event.prompt_source_type,
                            "pureText": event.pure_text,
                            "pureCharacterCount": event.pure_character_count,
                            "hasPureText": event.has_pure_text,
                            "textTruncated": truncated || event.text_truncated,
                        });
                        found.push((
                            event_index,
                            DirectMatch {
                                timestamp_ms: event.timestamp_ms,
                                rollout_path: path_text.clone(),
                                byte_offset: event.byte_offset,
                                value,
                            },
                        ));
                    }
                }
                event_history.push_back((
                    event.byte_offset,
                    event_text.clone(),
                    event.source_type.clone(),
                    event.timestamp_ms,
                    event_index,
                ));
                while event_history.len() > 256 {
                    event_history.pop_front();
                }
            }
            lineage_line_number = line_number;
            lineage_text_hash = event_text_hash;
            lineage_source_type = event.source_type;
            lineage_payload_type = event.payload_type;
            lineage_kind = event_kind;
        }
        if line_end == bytes.len() {
            break;
        }
        line_start = line_end + 1;
    }
    Ok(found.into_iter().map(|(_, item)| item).collect())
}

fn direct_search_cache() -> &'static Mutex<HashMap<String, DirectSearchCacheEntry>> {
    DIRECT_SEARCH_CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

fn validate_timeline_search_scope(segments: &[GlobalTimelineSegment], search: &str) -> Result<(), String> {
    if search.is_empty() {
        return Err("timeline search requires at least one normalized character".to_string());
    }
    if search.chars().count() < 3 && !segments.first().is_some_and(|first| {
        segments.iter().all(|segment| segment.thread_id == first.thread_id)
    }) {
        return Err("global content search requires at least 3 normalized characters".to_string());
    }
    Ok(())
}

pub(crate) fn read_direct_global_timeline_page(
    segments: &[GlobalTimelineSegment],
    query: &GlobalTimelineSearchQuery,
    cancelled: Arc<AtomicBool>,
) -> Result<Value, String> {
    let normalized_search = normalize_search_text(query.search.trim());
    validate_timeline_search_scope(segments, &normalized_search)?;
    let query_signature = global_query_signature(query);
    let cursor = query
        .cursor
        .as_deref()
        .filter(|value| !value.is_empty())
        .map(|value| {
            URL_SAFE_NO_PAD
                .decode(value)
                .map_err(|_| "invalid direct timeline cursor".to_string())
                .and_then(|bytes| {
                    serde_json::from_slice::<DirectSearchCursor>(&bytes)
                        .map_err(|_| "invalid direct timeline cursor".to_string())
                })
        })
        .transpose()?;
    if let Some(cursor) = &cursor {
        if cursor.query != query_signature {
            return Err("direct timeline cursor does not match the current filters".to_string());
        }
    }

    let mut revision_hash = Sha256::new();
    let mut source_bytes = 0u64;
    let mut weighted = Vec::with_capacity(segments.len());
    for segment in segments {
        check_cancelled(&cancelled)?;
        let state = source_state(&segment.path)?;
        source_bytes = source_bytes.saturating_add(state.size);
        revision_hash.update(segment.path.to_string_lossy().as_bytes());
        weighted.push((state.size, segment.clone()));
    }
    revision_hash.update(query_signature.as_bytes());
    let snapshot_id = format!("{:x}", revision_hash.finalize());
    if cursor
        .as_ref()
        .is_some_and(|value| value.snapshot != snapshot_id)
    {
        return Err("direct timeline cursor is stale".to_string());
    }

    let cached = {
        let mut cache = direct_search_cache()
            .lock()
            .map_err(|_| "direct search cache is unavailable".to_string())?;
        cache.retain(|_, entry| entry.created_at.elapsed() < DIRECT_SEARCH_CACHE_TTL);
        cache.get(&snapshot_id).cloned()
    };
    let entry = if let Some(entry) = cached {
        entry
    } else {
        let started = Instant::now();
        weighted.sort_by(|left, right| right.0.cmp(&left.0));
        let worker_count = thread::available_parallelism()
            .map_or(4, usize::from)
            .clamp(1, 8)
            .min(weighted.len().max(1));
        let mut buckets = vec![Vec::<GlobalTimelineSegment>::new(); worker_count];
        let mut bucket_bytes = vec![0u64; worker_count];
        for (size, segment) in weighted {
            let target = bucket_bytes
                .iter()
                .enumerate()
                .min_by_key(|(_, value)| **value)
                .map(|(index, _)| index)
                .unwrap_or(0);
            bucket_bytes[target] = bucket_bytes[target].saturating_add(size);
            buckets[target].push(segment);
        }
        let results = thread::scope(|scope| {
            let mut handles = Vec::with_capacity(worker_count);
            for bucket in buckets {
                let cancelled = cancelled.clone();
                let normalized_search = normalized_search.clone();
                handles.push(scope.spawn(move || {
                    let mut matches = Vec::new();
                    for segment in bucket {
                        matches.extend(scan_direct_segment(
                            &segment,
                            &normalized_search,
                            query,
                            &cancelled,
                        )?);
                    }
                    Ok::<Vec<DirectMatch>, String>(matches)
                }));
            }
            let mut matches = Vec::new();
            for handle in handles {
                matches.extend(
                    handle
                        .join()
                        .map_err(|_| "direct search worker panicked".to_string())??,
                );
            }
            Ok::<Vec<DirectMatch>, String>(matches)
        })?;
        let mut results = results;
        results.sort_by(|left, right| {
            right
                .timestamp_ms
                .cmp(&left.timestamp_ms)
                .then_with(|| right.rollout_path.cmp(&left.rollout_path))
                .then_with(|| right.byte_offset.cmp(&left.byte_offset))
        });
        let entry = DirectSearchCacheEntry {
            created_at: Instant::now(),
            matches: Arc::new(results.into_iter().map(|item| item.value).collect()),
            source_bytes,
            source_file_count: segments.len(),
            elapsed_ms: started.elapsed().as_millis(),
        };
        let mut cache = direct_search_cache()
            .lock()
            .map_err(|_| "direct search cache is unavailable".to_string())?;
        if cache.len() >= DIRECT_SEARCH_CACHE_LIMIT {
            if let Some(oldest) = cache
                .iter()
                .min_by_key(|(_, value)| value.created_at)
                .map(|(key, _)| key.clone())
            {
                cache.remove(&oldest);
            }
        }
        cache.insert(snapshot_id.clone(), entry.clone());
        entry
    };

    let position = cursor
        .as_ref()
        .map_or(0, |value| value.position)
        .min(entry.matches.len());
    let limit = query.limit.clamp(1, 200);
    let end = (position + limit).min(entry.matches.len());
    let page = entry.matches[position..end].to_vec();
    let next_cursor = (end < entry.matches.len()).then(|| {
        URL_SAFE_NO_PAD.encode(
            serde_json::to_vec(&DirectSearchCursor {
                snapshot: snapshot_id.clone(),
                query: query_signature.clone(),
                position: end,
            })
            .expect("direct cursor serializes"),
        )
    });
    Ok(json!({
        "kind": query.kind,
        "sourceFilter": query.source_filter,
        "search": query.search,
        "matchCount": entry.matches.len(),
        "matchCountComplete": true,
        "matches": page,
        "nextCursor": next_cursor,
        "hasMore": end < entry.matches.len(),
        "index": {
            "revision": snapshot_id,
            "sourceFileCount": entry.source_file_count,
            "indexedFileCount": entry.source_file_count,
            "completeFileCount": entry.source_file_count,
            "sourceBytes": entry.source_bytes,
            "indexedBytes": entry.source_bytes,
            "progress": 1.0,
            "complete": true,
            "runtime": "rust",
            "strategy": "parallel-direct-cache",
            "scanElapsedMs": entry.elapsed_ms,
            "requestElapsedMs": entry.elapsed_ms,
            "scanAddedEvents": 0,
            "scannedFiles": entry.source_file_count,
        }
    }))
}

pub(crate) fn read_completed_global_timeline_page(
    codex_home: &Path,
    segments: &[GlobalTimelineSegment],
    query: &GlobalTimelineSearchQuery,
    cancelled: Arc<AtomicBool>,
) -> Result<Option<Value>, String> {
    let payload = read_global_timeline_page(codex_home, segments, query, cancelled)?;
    Ok(payload["index"]["complete"]
        .as_bool()
        .unwrap_or(false)
        .then_some(payload))
}

pub(crate) fn complete_global_timeline_index(
    codex_home: &Path,
    segments: &[GlobalTimelineSegment],
    query: &GlobalTimelineSearchQuery,
) -> Result<(), String> {
    let mut build_query = query.clone();
    build_query.cursor = None;
    build_query.limit = 1;
    build_query.scan_budget_ms = 5_000;
    loop {
        let payload = read_global_timeline_page(
            codex_home,
            segments,
            &build_query,
            Arc::new(AtomicBool::new(false)),
        )?;
        if payload["index"]["complete"].as_bool().unwrap_or(false) {
            return Ok(());
        }
    }
}

fn global_query_signature(query: &GlobalTimelineSearchQuery) -> String {
    let payload = format!(
        "global-timeline-v1\0{}\0{}\0{}",
        query.kind,
        query.source_filter,
        normalize_search_text(&query.search)
    );
    format!("{:x}", Sha256::digest(payload.as_bytes()))
}

pub(crate) fn read_global_timeline_page(
    codex_home: &Path,
    segments: &[GlobalTimelineSegment],
    query: &GlobalTimelineSearchQuery,
    cancelled: Arc<AtomicBool>,
) -> Result<Value, String> {
    check_cancelled(&cancelled)?;
    let normalized_search = normalize_search_text(query.search.trim());
    validate_timeline_search_scope(segments, &normalized_search)?;
    let database_path = prompt_index_database_path(codex_home)?;
    if let Some(parent) = database_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|error| format!("cannot create prompt index directory: {error}"))?;
    }
    let _database_lock = DatabaseLock::exclusive_cancelled(&database_path, &cancelled)?;
    let mut connection = connect(&database_path)?;
    install_cancellation(&connection, cancelled.clone());
    let request_started = Instant::now();
    let mut sources = Vec::with_capacity(segments.len());
    let mut states: HashMap<String, FileState> = HashMap::new();
    for segment in segments {
        check_cancelled(&cancelled)?;
        let path = absolute_path(&segment.path)?;
        let source = source_state(&path)?;
        let path_text = path.to_string_lossy().into_owned();
        if let Some(state) = read_file_state(&connection, &path_text)? {
            states.insert(path_text.clone(), state);
        }
        sources.push((segment, path, path_text, source));
    }

    let scan_started = Instant::now();
    let mut scan_added_events = 0usize;
    let mut scanned_files = 0usize;
    loop {
        check_cancelled(&cancelled)?;
        let elapsed = scan_started.elapsed().as_millis() as u64;
        if elapsed >= query.scan_budget_ms.max(1) {
            break;
        }
        let target = sources
            .iter()
            .filter(|(_, _, path_text, source)| {
                states.get(path_text).is_none_or(|state| {
                    !state.timeline_complete
                        || state.timeline_scanned_offset < source.size
                        || state.observed_size != source.size
                        || state.observed_mtime_ns != source.modified_ns
                })
            })
            .max_by_key(|(_, _, path_text, source)| {
                let scanned = states.get(path_text).map_or(0, |state| {
                    state
                        .timeline_scanned_offset
                        .max(state.timeline_partial_offset)
                });
                (source.size.saturating_sub(scanned), source.modified_ns)
            });
        let Some((_, path, path_text, _)) = target else {
            break;
        };
        let remaining = query.scan_budget_ms.saturating_sub(elapsed).max(1);
        let (state, _, added, _) =
            advance_timeline_index(&mut connection, path, remaining, &cancelled)?;
        let complete = state.timeline_complete;
        states.insert(path_text.clone(), state);
        scan_added_events = scan_added_events.saturating_add(added);
        scanned_files += 1;
        if !complete {
            break;
        }
    }

    let mut indexed = Vec::new();
    let mut source_bytes = 0u64;
    let mut indexed_bytes = 0u64;
    let mut complete_files = 0usize;
    let mut revision_hash = Sha256::new();
    for (segment, _, path_text, source) in &sources {
        source_bytes = source_bytes.saturating_add(source.size);
        if let Some(state) = states.get(path_text) {
            let scanned = state
                .timeline_scanned_offset
                .max(state.timeline_partial_offset)
                .min(source.size);
            indexed_bytes = indexed_bytes.saturating_add(scanned);
            if state.timeline_complete
                && state.timeline_scanned_offset >= source.size
                && state.observed_size == source.size
                && state.observed_mtime_ns == source.modified_ns
            {
                complete_files += 1;
            }
            if scanned > 0 {
                indexed.push((state.id, *segment));
                revision_hash.update(path_text.as_bytes());
                revision_hash.update(state.generation.as_bytes());
            }
        }
    }
    let complete = complete_files == segments.len();
    let revision = format!("{:x}", revision_hash.finalize());
    let query_signature = global_query_signature(query);
    let cursor = if let Some(cursor) = query.cursor.as_deref().filter(|value| !value.is_empty()) {
        let bytes = URL_SAFE_NO_PAD
            .decode(cursor)
            .map_err(|_| "invalid global timeline cursor".to_string())?;
        let cursor: GlobalTimelineCursor = serde_json::from_slice(&bytes)
            .map_err(|_| "invalid global timeline cursor".to_string())?;
        if cursor.revision != revision || cursor.query != query_signature {
            return Err(
                "global timeline cursor is stale or does not match the current filters".to_string(),
            );
        }
        Some(cursor)
    } else {
        None
    };

    let kind_clause = timeline_kind_clause(&query.kind)?;
    let source_clause = timeline_source_clause(&query.source_filter)?;
    let search_column = if matches!(query.source_filter.as_str(), "pure" | "focused") {
        "pure_search_text"
    } else {
        "search_text"
    };
    let mut matches = Vec::new();
    let mut match_count = 0i64;
    let mut has_indexed_more = false;
    let mut last_cursor_tuple: Option<(i64, i64, i64)> = None;
    let safe_limit = query.limit.clamp(1, 200);
    let use_fts = normalized_search.chars().count() >= 3;
    let search_from = if use_fts {
        "timeline_search_fts AS f CROSS JOIN timeline_events AS e ON CAST(f.file_id AS INTEGER)=e.file_id AND CAST(f.event_index AS INTEGER)=e.event_index"
    } else {
        "timeline_events AS e"
    };
    let search_clause = if use_fts {
        "timeline_search_fts MATCH ?".to_string()
    } else {
        format!("instr(e.{search_column}, ?) > 0")
    };
    let search_value = if use_fts {
        fts_literal(&normalized_search, search_column)
    } else { normalized_search.clone() };
    if !indexed.is_empty() {
        let placeholders = std::iter::repeat_n("(?)", indexed.len())
            .collect::<Vec<_>>()
            .join(",");
        let mut base_values = indexed
            .iter()
            .map(|(file_id, _)| SqlValue::Integer(*file_id))
            .collect::<Vec<_>>();
        base_values.push(SqlValue::Text(search_value.clone()));
        let cursor_clause = if cursor.is_some() {
            "AND (e.timestamp_ms<? OR (e.timestamp_ms=? AND e.file_id<?) OR (e.timestamp_ms=? AND e.file_id=? AND e.event_index<?))"
        } else {
            ""
        };
        if let Some(cursor) = cursor.as_ref() {
            base_values.extend([
                SqlValue::Integer(cursor.timestamp_ms),
                SqlValue::Integer(cursor.timestamp_ms),
                SqlValue::Integer(cursor.file_id),
                SqlValue::Integer(cursor.timestamp_ms),
                SqlValue::Integer(cursor.file_id),
                SqlValue::Integer(cursor.event_index),
            ]);
        }
        let sql = format!(
            "WITH scoped(file_id) AS (VALUES {placeholders})
             SELECT e.file_id,e.event_index,e.line_number,e.byte_offset,e.timestamp,e.timestamp_ms,
                    e.kind,e.label,e.text,e.character_count,e.source_type,e.payload_type,e.phase,
                    e.call_id,e.readable,e.encrypted,e.has_encrypted_content,e.prompt_source_type,
                    e.pure_text,e.pure_character_count,e.has_pure_text
             FROM {search_from}
             JOIN scoped AS s ON s.file_id=e.file_id
             WHERE {search_clause} AND ({kind_clause}) AND ({source_clause}) {cursor_clause}
             ORDER BY e.timestamp_ms DESC,e.file_id DESC,e.event_index DESC LIMIT ?"
        );
        let mut page_values = base_values.clone();
        page_values.push(SqlValue::Integer((safe_limit + 1) as i64));
        let segment_by_file = indexed
            .iter()
            .map(|(file_id, segment)| (*file_id, *segment))
            .collect::<HashMap<_, _>>();
        let mut statement = connection
            .prepare(&sql)
            .map_err(cancel_sql_error(&cancelled))?;
        let mut rows = statement
            .query(params_from_iter(page_values.iter()))
            .map_err(cancel_sql_error(&cancelled))?;
        let mut remaining_bytes = RESULT_PAGE_BYTES;
        while let Some(row) = rows.next().map_err(cancel_sql_error(&cancelled))? {
            check_cancelled(&cancelled)?;
            if matches.len() >= safe_limit || remaining_bytes == 0 {
                has_indexed_more = true;
                break;
            }
            let file_id: i64 = row.get(0).map_err(sql_error)?;
            let Some(segment) = segment_by_file.get(&file_id) else {
                continue;
            };
            let text: String = row.get(8).map_err(sql_error)?;
            let pure_text: String = row.get(18).map_err(sql_error)?;
            let selected_text = if matches!(query.source_filter.as_str(), "pure" | "focused")
                && !pure_text.is_empty()
            {
                &pure_text
            } else {
                &text
            };
            let (excerpt, truncated) = excerpt(
                selected_text,
                &normalized_search,
                RESULT_ITEM_BYTES.min(remaining_bytes),
            );
            remaining_bytes = remaining_bytes.saturating_sub(excerpt.len());
            matches.push(json!({
                "id": format!("{}-{}-{}", segment.thread_id, file_id, row.get::<_, i64>(1).map_err(sql_error)?),
                "threadId": segment.thread_id,
                "threadTitle": segment.thread_title,
                "projectPath": segment.project_path,
                "projectLabel": segment.project_label,
                "threadKind": segment.thread_kind,
                "archived": segment.archived,
                "rolloutPath": segment.path.to_string_lossy(),
                "eventIndex": row.get::<_, i64>(1).map_err(sql_error)?,
                "lineNumber": row.get::<_, i64>(2).map_err(sql_error)?,
                "byteOffset": row.get::<_, i64>(3).map_err(sql_error)?,
                "timestamp": row.get::<_, Option<String>>(4).map_err(sql_error)?,
                "timestampMs": row.get::<_, i64>(5).map_err(sql_error)?,
                "kind": row.get::<_, String>(6).map_err(sql_error)?,
                "label": row.get::<_, String>(7).map_err(sql_error)?,
                "text": excerpt,
                "characterCount": row.get::<_, i64>(9).map_err(sql_error)?,
                "sourceType": row.get::<_, String>(10).map_err(sql_error)?,
                "payloadType": row.get::<_, String>(11).map_err(sql_error)?,
                "phase": row.get::<_, String>(12).map_err(sql_error)?,
                "callId": row.get::<_, String>(13).map_err(sql_error)?,
                "readable": row.get::<_, i64>(14).map_err(sql_error)? != 0,
                "encrypted": row.get::<_, i64>(15).map_err(sql_error)? != 0,
                "hasEncryptedContent": row.get::<_, i64>(16).map_err(sql_error)? != 0,
                "promptSourceType": row.get::<_, String>(17).map_err(sql_error)?,
                "pureText": pure_text,
                "pureCharacterCount": row.get::<_, i64>(19).map_err(sql_error)?,
                "hasPureText": row.get::<_, i64>(20).map_err(sql_error)? != 0,
                "textTruncated": truncated,
            }));
            last_cursor_tuple = Some((
                row.get::<_, i64>(5).map_err(sql_error)?,
                file_id,
                row.get::<_, i64>(1).map_err(sql_error)?,
            ));
        }
        drop(rows);
        drop(statement);
        let count_sql = format!(
            "WITH scoped(file_id) AS (VALUES {placeholders})
             SELECT COUNT(*) FROM {search_from}
             JOIN scoped AS s ON s.file_id=e.file_id
             WHERE {search_clause} AND ({kind_clause}) AND ({source_clause})"
        );
        let count_values = indexed
            .iter()
            .map(|(file_id, _)| SqlValue::Integer(*file_id))
            .chain(std::iter::once(SqlValue::Text(search_value)))
            .collect::<Vec<_>>();
        match_count = connection
            .query_row(&count_sql, params_from_iter(count_values.iter()), |row| {
                row.get(0)
            })
            .map_err(cancel_sql_error(&cancelled))?;
    }
    let next_cursor = if (complete || cursor.is_some()) && has_indexed_more {
        last_cursor_tuple.map(|(timestamp_ms, file_id, event_index)| {
            let cursor = GlobalTimelineCursor {
                revision: revision.clone(),
                query: query_signature,
                timestamp_ms,
                file_id,
                event_index,
            };
            URL_SAFE_NO_PAD.encode(serde_json::to_vec(&cursor).expect("global cursor serializes"))
        })
    } else {
        None
    };
    Ok(json!({
        "kind": query.kind,
        "sourceFilter": query.source_filter,
        "search": query.search,
        "matchCount": match_count,
        "matchCountComplete": complete,
        "matches": matches,
        "nextCursor": next_cursor,
        "hasMore": has_indexed_more || !complete,
        "index": {
            "revision": revision,
            "sourceFileCount": segments.len(),
            "indexedFileCount": indexed.len(),
            "completeFileCount": complete_files,
            "sourceBytes": source_bytes,
            "indexedBytes": indexed_bytes,
            "progress": if source_bytes == 0 { 1.0 } else { indexed_bytes as f64 / source_bytes as f64 },
            "complete": complete,
            "runtime": "rust",
            "scanElapsedMs": scan_started.elapsed().as_millis(),
            "requestElapsedMs": request_started.elapsed().as_millis(),
            "scanAddedEvents": scan_added_events,
            "scannedFiles": scanned_files,
        }
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::process::Command;
    use tempfile::tempdir;

    fn write_record(file: &mut File, text: &str, seconds: usize) {
        writeln!(
            file,
            "{}",
            json!({
                "type": "user_message",
                "timestamp": format!("2026-08-22T00:00:{seconds:02}Z"),
                "payload": {"text": text}
            })
        )
        .unwrap();
        file.flush().unwrap();
    }

    fn single_segment(connection: &Connection, rollout: &Path) -> Vec<IndexedSegment> {
        let path = absolute_path(rollout)
            .unwrap()
            .to_string_lossy()
            .into_owned();
        let file_id = connection
            .query_row("SELECT id FROM prompt_files WHERE path=?1", [path], |row| {
                row.get(0)
            })
            .unwrap();
        let prompt_count = connection
            .query_row(
                "SELECT COUNT(*) FROM prompts WHERE file_id=?1",
                [file_id],
                |row| row.get(0),
            )
            .unwrap();
        vec![IndexedSegment {
            file_id,
            base: 0,
            prompt_count,
        }]
    }

    #[test]
    fn unicode_normalization_matches_browser_contract() {
        assert_eq!(
            normalize_search_text("ﬀ ﬃ ﭏ Straße Σς Cafe\u{301} İZMİR"),
            "ff ffi אל strasse σσ cafe izmir"
        );
    }

    #[test]
    fn classification_migration_preserves_progress_and_search() {
        let directory = tempdir().unwrap();
        let rollout = directory.path().join("classification.jsonl");
        let fixtures: Vec<Value> = serde_json::from_str(include_str!("../../../tests/fixtures/timeline_classification_contract.json")).unwrap();
        let mut file = File::create(&rollout).unwrap();
        for (index, fixture) in fixtures.iter().enumerate() {
            let mut record = fixture["record"].clone();
            record["timestamp"] = json!(format!("2026-01-01T00:{index:02}:00Z"));
            writeln!(file, "{record}").unwrap();
        }
        drop(file);
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_schema(&connection).unwrap();
        let cancelled = AtomicBool::new(false);
        advance_index(&mut connection, &rollout, 5000, &cancelled).unwrap();
        advance_timeline_index(&mut connection, &rollout, 5000, &cancelled).unwrap();
        let progress = read_file_state(&connection, &absolute_path(&rollout).unwrap().to_string_lossy()).unwrap().unwrap();
        let coordinates = |connection: &Connection| {
            connection.prepare("SELECT id,event_index,byte_offset,text FROM timeline_events ORDER BY id").unwrap()
                .query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?, row.get::<_, i64>(2)?, row.get::<_, String>(3)?)))
                .unwrap().collect::<Result<Vec<_>, _>>().unwrap()
        };
        let before = coordinates(&connection);
        connection.execute_batch("UPDATE timeline_events SET kind='user',label='用户输入',prompt_source_type='user',pure_text=text WHERE prompt_source_type='goal';
            UPDATE timeline_events SET kind='assistant',label='最终回复' WHERE kind='assistant_unclassified';
            UPDATE timeline_events SET label='思考过程' WHERE kind='commentary';
            UPDATE prompts SET source_type='internal',source_label='内部上下文',visible_by_default=0,pure_text='',has_pure_text=0 WHERE text LIKE 'Explain %<environment_context>%' OR text LIKE 'Explain %<permissions instructions>%';
            PRAGMA user_version=22;").unwrap();
        ensure_schema(&connection).unwrap();
        let after = read_file_state(&connection, &absolute_path(&rollout).unwrap().to_string_lossy()).unwrap().unwrap();
        assert_eq!(after.generation, progress.generation);
        assert_eq!(after.scanned_offset, progress.scanned_offset);
        assert_eq!(after.timeline_scanned_offset, progress.timeline_scanned_offset);
        assert_eq!(coordinates(&connection), before);
        for (sql, expected) in [
            ("SELECT COUNT(*) FROM timeline_events WHERE kind='assistant_unclassified'", 2),
            ("SELECT COUNT(*) FROM timeline_events WHERE label='思考过程'", 0),
            ("SELECT COUNT(*) FROM timeline_events WHERE prompt_source_type='goal' AND kind='context' AND pure_text=''", 1),
            ("SELECT COUNT(*) FROM prompts WHERE text LIKE 'Explain %' AND source_type='user'", 4),
            ("SELECT COUNT(*) FROM prompt_search_fts WHERE prompt_search_fts MATCH 'explain'", 4),
        ] { assert_eq!(connection.query_row(sql, [], |row| row.get::<_, i64>(0)).unwrap(), expected, "{sql}"); }
        assert_eq!(connection.query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0)).unwrap(), 23);
        ensure_schema(&connection).unwrap();
        assert_eq!(coordinates(&connection), before);
    }

    #[test]
    fn timeline_prefilter_skips_tool_payloads_before_json_parsing() {
        let tool = br#"{"type":"response_item","payload":{"type":"custom_tool_call_output","output":"user_message only inside tool text"}}"#;
        let message = br#"{"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}}"#;
        let event = br#"{"type":"event_msg","payload":{"type":"agent_message","message":"done"}}"#;
        assert!(!timeline_candidate(tool));
        assert!(timeline_candidate(message));
        assert!(timeline_candidate(event));
    }

    #[test]
    fn recent_browsing_reuses_warm_index_and_preserves_cold_cursor_and_latest_tail() {
        let directory = tempdir().unwrap();
        let old_path = directory.path().join("old.jsonl");
        let current_path = directory.path().join("current.jsonl");
        let mut old = File::create(&old_path).unwrap();
        let mut current = File::create(&current_path).unwrap();
        for position in 0..100 {
            write_record(&mut old, &format!("old {position}"), position);
            write_record(&mut current, &format!("current {position}"), position);
        }
        let locator = ThreadLocator {
            id: "warm-paging".into(), title: "Warm paging".into(),
            rollout_path: current_path.clone(), rollout_paths: vec![old_path, current_path.clone()],
        };
        let mut query = PromptPageQuery {
            codex_home: Some(directory.path().to_string_lossy().into_owned()), cursor: None,
            limit: Some(60), search: None, scope: Some("pure".into()), source_type: None,
            order: Some("desc".into()), strategy: Some("recent".into()), request_id: None,
            scan_budget_ms: Some(1_000),
        };
        let cancelled = Arc::new(AtomicBool::new(false));
        assert!(read_indexed_recent_prompt_page(&locator, directory.path(), &query, "pure", "", 60, cancelled.clone()).unwrap().is_none());
        let cold = super::super::read_prompt_page(&super::super::PromptStore::default(), &locator, &query).unwrap();
        assert_eq!(cold["index"]["strategy"], "recent");
        let database = prompt_index_database_path(directory.path()).unwrap();
        std::fs::create_dir_all(database.parent().unwrap()).unwrap();
        let mut connection = connect(&database).unwrap();
        let (_, state) = advance_thread_index(&mut connection, &locator, 1000, &cancelled).unwrap();
        assert!(state.complete);
        drop(connection);
        query.cursor = cold["nextCursor"].as_str().map(str::to_string);
        let cold_next = super::super::read_prompt_page(&super::super::PromptStore::default(), &locator, &query).unwrap();
        assert_eq!(cold_next["index"]["strategy"], "recent", "a cold cursor must not switch coordinate systems mid-scroll");
        write_record(&mut current, "latest appended", 101);
        query.cursor = None;
        let mut identities = std::collections::HashSet::new();
        let mut pages = 0;
        loop {
            let page = super::super::read_prompt_page(&super::super::PromptStore::default(), &locator, &query).unwrap();
            assert_eq!(page["index"]["strategy"], "persistent");
            assert_eq!(page["index"]["complete"], pages == 0);
            let rows = page["prompts"].as_array().unwrap();
            if pages == 0 {
                assert_eq!(rows[0]["text"], "latest appended");
                assert_eq!(page["index"]["warmReuse"], true);
                write_record(&mut current, "arrived while browsing", 102);
            } else {
                assert_eq!(page["index"]["scanAddedPrompts"], 0);
            }
            for row in rows { assert!(identities.insert(row["recordId"].as_str().unwrap().to_string())); }
            pages += 1;
            query.cursor = page["nextCursor"].as_str().map(str::to_string);
            if query.cursor.is_none() { break; }
            assert!(pages < 5);
        }
        assert_eq!(pages, 4);
        assert_eq!(identities.len(), 201);
        query.cursor = None;
        let refreshed = super::super::read_prompt_page(&super::super::PromptStore::default(), &locator, &query).unwrap();
        assert_eq!(refreshed["prompts"][0]["text"], "arrived while browsing");
        drop(current);
        std::fs::write(&current_path, format!("{}\n", json!({"type":"user_message","payload":{"text":"replacement live"}}))).unwrap();
        query.cursor = None;
        let replacement = super::super::read_prompt_page(&super::super::PromptStore::default(), &locator, &query).unwrap();
        assert_eq!(replacement["prompts"][0]["text"], "replacement live");
        assert!(!replacement["prompts"].as_array().unwrap().iter().any(|row| row["text"] == "latest appended"));
    }

    #[test]
    fn windows_database_hash_matches_python_normcase_contract() {
        if cfg!(windows) {
            let path = prompt_index_database_path(Path::new(r"D:\.codex")).unwrap();
            assert_eq!(
                path.file_name().unwrap().to_string_lossy(),
                "f62773fbc97d945cb72d176b302b0fdef6d32b17f6a7f26a905b8effdf70252b.sqlite"
            );
        }
    }

    #[test]
    fn global_timeline_search_is_rust_native_and_pages_continuations() {
        let directory = tempdir().unwrap();
        let home = directory.path().join("home");
        std::fs::create_dir_all(&home).unwrap();
        let first = home.join("first.jsonl");
        let second = home.join("second.jsonl");
        let mut first_file = File::create(&first).unwrap();
        let mut second_file = File::create(&second).unwrap();
        write_record(&mut first_file, "old needle", 1);
        write_record(&mut first_file, "old background", 2);
        write_record(&mut second_file, "new needle", 3);
        write_record(&mut second_file, "newest needle", 4);
        drop(first_file);
        drop(second_file);
        let status = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "prompts::prompt_index::tests::global_timeline_search_child",
                "--nocapture",
            ])
            .env("GLOBAL_TIMELINE_TEST_HOME", &home)
            .env(
                "CODEX_HOME_MANAGER_PROMPT_INDEX_ROOT",
                directory.path().join("index"),
            )
            .status()
            .unwrap();
        assert!(status.success());
    }

    #[test]
    fn global_timeline_search_child() {
        let Ok(home) = std::env::var("GLOBAL_TIMELINE_TEST_HOME") else {
            return;
        };
        let home = PathBuf::from(home);
        let segments = ["first.jsonl", "second.jsonl"]
            .into_iter()
            .map(|name| GlobalTimelineSegment {
                path: home.join(name),
                thread_id: "root-thread-with-dashes".to_string(),
                thread_title: "Continued task".to_string(),
                project_path: "project".to_string(),
                project_label: "Project".to_string(),
                thread_kind: "main".to_string(),
                archived: false,
                updated_at_ms: 1,
            })
            .collect::<Vec<_>>();
        let mut query = GlobalTimelineSearchQuery {
            cursor: None,
            limit: 2,
            kind: "conversation".to_string(),
            search: "needle".to_string(),
            source_filter: "all".to_string(),
            scan_budget_ms: 1_000,
        };
        let first =
            read_direct_global_timeline_page(&segments, &query, Arc::new(AtomicBool::new(false)))
                .unwrap();
        assert_eq!(first["index"]["runtime"], "rust");
        assert_eq!(first["index"]["complete"], true);
        assert_eq!(first["matchCount"], 3);
        assert_eq!(first["matches"].as_array().unwrap().len(), 2);
        assert_eq!(first["matches"][0]["text"], "newest needle");
        assert_eq!(first["matches"][0]["threadId"], "root-thread-with-dashes");
        query.cursor = first["nextCursor"].as_str().map(str::to_string);
        let second =
            read_direct_global_timeline_page(&segments, &query, Arc::new(AtomicBool::new(false)))
                .unwrap();
        assert_eq!(second["matches"].as_array().unwrap().len(), 1);
        assert_eq!(second["matches"][0]["text"], "old needle");
        assert_eq!(second["hasMore"], false);
        assert!(second["nextCursor"].is_null());

        let mut persistent_query = GlobalTimelineSearchQuery {
            cursor: None,
            limit: 2,
            kind: "conversation".to_string(),
            search: "needle".to_string(),
            source_filter: "all".to_string(),
            scan_budget_ms: 1_000,
        };
        let persistent_first = read_global_timeline_page(
            &home,
            &segments,
            &persistent_query,
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        assert_eq!(persistent_first["index"]["complete"], true);
        persistent_query.cursor = persistent_first["nextCursor"].as_str().map(str::to_string);
        assert!(!global_timeline_cursor_is_direct(
            persistent_query.cursor.as_deref()
        ));
        let mut appended = std::fs::OpenOptions::new()
            .append(true)
            .open(home.join("second.jsonl"))
            .unwrap();
        write_record(&mut appended, "appended needle", 5);
        let persistent_second = read_global_timeline_page(
            &home,
            &segments,
            &persistent_query,
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        assert_eq!(persistent_second["matches"].as_array().unwrap().len(), 1);
        assert_eq!(persistent_second["matches"][0]["text"], "old needle");
        assert!(global_timeline_cursor_is_direct(
            first["nextCursor"].as_str()
        ));

        let mut old_file = std::fs::OpenOptions::new().append(true).open(home.join("first.jsonl")).unwrap();
        write_record(&mut old_file, "old 批量", 6);
        write_record(&mut appended, "new 批量", 7);
        for (phase, text, seconds) in [("commentary", "progress 批量", 8), ("final", "reply 批量", 9)] {
            writeln!(appended, "{}", json!({"type":"response_item","timestamp":format!("2026-09-07T00:00:{seconds:02}Z"),
                "payload":{"type":"message","role":"assistant","phase":phase,"content":[{"type":"output_text","text":text}]}})).unwrap();
        }
        let mut short_query = GlobalTimelineSearchQuery {
            cursor: None, limit: 1, kind: "conversation".into(), search: "批量".into(),
            source_filter: "focused".into(), scan_budget_ms: 1000,
        };
        let short_first = read_global_timeline_page(&home, &segments, &short_query, Arc::new(AtomicBool::new(false))).unwrap();
        assert_eq!(short_first["matchCountComplete"], true);
        assert_eq!(short_first["matchCount"], 4);
        assert_eq!(short_first["matches"][0]["text"], "reply 批量");
        short_query.cursor = short_first["nextCursor"].as_str().map(str::to_string);
        for expected in ["progress 批量", "new 批量", "old 批量"] {
            let page = read_global_timeline_page(&home, &segments, &short_query, Arc::new(AtomicBool::new(false))).unwrap();
            assert_eq!(page["matches"][0]["text"], expected);
            short_query.cursor = page["nextCursor"].as_str().map(str::to_string);
            assert_eq!(page["hasMore"], expected != "old 批量");
        }
        short_query.cursor = None;
        short_query.search = "量".into();
        assert_eq!(read_global_timeline_page(&home, &segments, &short_query, Arc::new(AtomicBool::new(false))).unwrap()["matchCount"], 4);
        let mut mixed_segments = segments.clone();
        mixed_segments[1].thread_id = "another-thread".into();
        assert!(read_global_timeline_page(&home, &mixed_segments, &short_query, Arc::new(AtomicBool::new(false))).unwrap_err().contains("at least 3"));
        assert_eq!(read_global_timeline_page(&home, &segments, &short_query, Arc::new(AtomicBool::new(true))).unwrap_err(), PROMPT_REQUEST_CANCELLED);
        short_query.search = "\u{301}".into();
        assert!(read_global_timeline_page(&home, &segments, &short_query, Arc::new(AtomicBool::new(false))).unwrap_err().contains("at least one"));
    }

    #[test]
    fn persistent_index_searches_unicode_and_extends_append_only_rollout() {
        let directory = tempdir().unwrap();
        let rollout = directory.path().join("rollout.jsonl");
        let database = directory.path().join("index.sqlite");
        let mut source = File::create(&rollout).unwrap();
        write_record(&mut source, "Cafe\u{301} 顶刊研究", 1);
        write_record(&mut source, "Straße baseline", 2);
        let cancelled = AtomicBool::new(false);
        let mut connection = connect(&database).unwrap();
        let first_state = advance_index(&mut connection, &rollout, 1_000, &cancelled).unwrap();
        assert!(first_state.complete);
        assert_eq!(first_state.scan_added_prompts, 2);

        let cafe = read_page(
            &connection,
            &single_segment(&connection, &rollout),
            "all",
            "",
            "asc",
            "café",
            None,
            20,
            &first_state,
            &cancelled,
        )
        .unwrap();
        assert_eq!(cafe["matchCount"], 1);
        assert_eq!(cafe["prompts"][0]["text"], "Cafe\u{301} 顶刊研究");
        drop(connection);

        write_record(&mut source, "latest appended prompt", 3);
        let mut connection = connect(&database).unwrap();
        let second_state = advance_index(&mut connection, &rollout, 1_000, &cancelled).unwrap();
        assert!(second_state.complete);
        assert_eq!(second_state.scan_added_prompts, 1);
        assert!(second_state.scan_start_offset > 0);
        let latest = read_page(
            &connection,
            &single_segment(&connection, &rollout),
            "pure",
            "",
            "desc",
            "latest appended",
            None,
            20,
            &second_state,
            &cancelled,
        )
        .unwrap();
        assert_eq!(latest["matchCount"], 1);
        assert_eq!(latest["prompts"][0]["pureText"], "latest appended prompt");
    }

    #[test]
    fn search_hits_and_context_share_filtered_record_identity() {
        let directory = tempdir().unwrap();
        let rollout = directory.path().join("rollout.jsonl");
        let mut source = File::create(&rollout).unwrap();
        for (index, text) in [
            "earlier neighbor",
            "older needle",
            "middle neighbor",
            "latest needle",
            "later neighbor",
        ]
        .iter()
        .enumerate()
        {
            write_record(&mut source, text, index);
        }
        let locator = ThreadLocator {
            id: "context-regression".into(),
            title: "Context".into(),
            rollout_path: rollout.clone(),
            rollout_paths: vec![rollout],
        };
        for scope in ["pure", "visible", "with_agents", "all"] {
            let query = PromptPageQuery {
                codex_home: Some(directory.path().to_string_lossy().into_owned()),
                cursor: None,
                limit: Some(20),
                search: Some("needle".into()),
                scope: Some(scope.into()),
                source_type: None,
                order: Some("desc".into()),
                strategy: Some("index".into()),
                request_id: None,
                scan_budget_ms: Some(1000),
            };
            let page = super::super::read_prompt_page(
                &super::super::PromptStore::default(),
                &locator,
                &query,
            )
            .unwrap();
            assert_eq!(page["index"]["strategy"], "persistent");
            assert_eq!(page["matchCount"], 2);
            assert_eq!(page["prompts"][0]["text"], "latest needle");
            for hit in page["prompts"].as_array().unwrap() {
                let context_query = PromptContextQuery {
                    codex_home: query.codex_home.clone(),
                    anchor_index: hit["index"].as_u64().unwrap() as usize,
                    anchor_byte_offset: hit["byteOffset"].as_i64(),
                    anchor_record_id: None,
                    generation: page["index"]["generation"].as_str().map(str::to_string),
                    scope: Some(scope.into()),
                    search: query.search.clone(),
                    source_type: None,
                    before: Some(1),
                    after: Some(1),
                    scan_budget_ms: Some(1000),
                };
                let context =
                    read_persistent_prompt_context(&locator, directory.path(), &context_query)
                        .unwrap();
                let anchor =
                    &context["prompts"][context["anchorOffset"].as_u64().unwrap() as usize];
                assert_eq!(anchor["byteOffset"], hit["byteOffset"]);
                assert_eq!(anchor["text"], hit["text"]);
                assert_eq!(context["prompts"].as_array().unwrap().len(), 3);
                assert_eq!(context["index"]["generation"], page["index"]["generation"]);
                let wrong_position = PromptContextQuery {
                    anchor_byte_offset: Some(-1),
                    ..context_query
                };
                assert!(read_persistent_prompt_context(
                    &locator,
                    directory.path(),
                    &wrong_position
                )
                .unwrap_err()
                .contains("record position changed"));
                let wrong_generation = PromptContextQuery {
                    generation: Some("outdated".into()),
                    ..wrong_position
                };
                assert!(read_persistent_prompt_context(
                    &locator,
                    directory.path(),
                    &wrong_generation
                )
                .unwrap_err()
                .contains("generation changed"));
            }
        }
    }

    #[test]
    fn continuation_segments_share_counts_paging_search_and_context() {
        let directory = tempdir().unwrap();
        let old_rollout = directory.path().join("rollout-2026-05-old.jsonl");
        let current_rollout = directory.path().join("rollout-2026-09-current.jsonl");
        let mut old_source = File::create(&old_rollout).unwrap();
        let mut current_source = File::create(&current_rollout).unwrap();
        for (index, text) in ["old one", "old two", "old three"].iter().enumerate() {
            write_record(&mut old_source, text, index + 1);
        }
        for (index, text) in ["current one", "current two"].iter().enumerate() {
            write_record(&mut current_source, text, index + 10);
        }
        let locator = ThreadLocator {
            id: "continued-thread".into(),
            title: "Continued".into(),
            rollout_path: current_rollout.clone(),
            rollout_paths: vec![old_rollout, current_rollout],
        };
        let page_query = PromptPageQuery {
            codex_home: Some(directory.path().to_string_lossy().into_owned()),
            cursor: None,
            limit: Some(3),
            search: None,
            scope: Some("all".into()),
            source_type: None,
            order: Some("desc".into()),
            strategy: Some("recent".into()),
            request_id: None,
            scan_budget_ms: Some(1_000),
        };

        let page = super::super::read_prompt_page(
            &super::super::PromptStore::default(),
            &locator,
            &page_query,
        )
        .unwrap();
        assert_eq!(page["promptCount"], 3);
        assert_eq!(page["index"]["segmentCount"], 2);
        assert_eq!(page["rolloutPaths"].as_array().unwrap().len(), 2);
        assert_eq!(page["prompts"][0]["text"], "current two");
        assert_eq!(page["prompts"][0]["ordinalExact"], false);
        assert_eq!(page["prompts"][2]["text"], "old three");

        let search_query = PromptPageQuery {
            search: Some("old".into()),
            limit: Some(20),
            strategy: Some("index".into()),
            ..page_query
        };
        let search_page = super::super::read_prompt_page(
            &super::super::PromptStore::default(),
            &locator,
            &search_query,
        )
        .unwrap();
        assert_eq!(search_page["matchCount"], 3);
        let context_query = PromptContextQuery {
            codex_home: search_query.codex_home.clone(),
            anchor_index: 4,
            anchor_byte_offset: None,
            anchor_record_id: None,
            generation: search_page["index"]["generation"]
                .as_str()
                .map(str::to_string),
            scope: Some("all".into()),
            search: None,
            source_type: None,
            before: Some(1),
            after: Some(1),
            scan_budget_ms: Some(1_000),
        };
        let context =
            read_persistent_prompt_context(&locator, directory.path(), &context_query).unwrap();
        assert_eq!(context["prompts"][0]["text"], "old three");
        assert_eq!(context["prompts"][1]["text"], "current one");
        assert_eq!(context["prompts"][2]["text"], "current two");
    }

    #[test]
    fn selective_search_preserves_paging_with_bounded_sql_work() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };

        let directory = tempdir().unwrap();
        let rollout = directory.path().join("rollout.jsonl");
        let mut source = File::create(&rollout).unwrap();
        for index in 0..2001 {
            let text = if index % 1000 == 0 {
                format!("selective needle {index}")
            } else {
                "ordinary background".to_string()
            };
            write_record(&mut source, &text, index % 60);
        }
        let cancelled = AtomicBool::new(false);
        let mut connection = connect(&directory.path().join("index.sqlite")).unwrap();
        let mut state = advance_index(&mut connection, &rollout, 1000, &cancelled).unwrap();
        while !state.complete {
            state = advance_index(&mut connection, &rollout, 1000, &cancelled).unwrap();
        }
        for scope in ["all", "pure"] {
            for order in ["asc", "desc"] {
                let mut cursor: Option<String> = None;
                let mut texts = Vec::new();
                for _ in 0..3 {
                    let steps = Arc::new(AtomicUsize::new(0));
                    let measured = Arc::clone(&steps);
                    connection
                        .progress_handler(
                            1000,
                            Some(move || measured.fetch_add(1, Ordering::Relaxed) >= 500),
                        )
                        .unwrap();
                    let page = read_page(
                        &connection,
                        &single_segment(&connection, &rollout),
                        scope,
                        "",
                        order,
                        "selective needle",
                        cursor.as_deref(),
                        1,
                        &state,
                        &cancelled,
                    )
                    .unwrap();
                    assert!(steps.load(Ordering::Relaxed) < 500);
                    assert_eq!(page["matchCount"], 3);
                    texts.push(page["prompts"][0]["text"].as_str().unwrap().to_string());
                    cursor = page["nextCursor"].as_str().map(str::to_string);
                }
                assert!(cursor.is_none());
                let mut expected = vec![
                    "selective needle 0",
                    "selective needle 1000",
                    "selective needle 2000",
                ];
                if order == "desc" {
                    expected.reverse();
                }
                assert_eq!(texts, expected);
                connection
                    .progress_handler(0, None::<fn() -> bool>)
                    .unwrap();
                let absent = read_page(
                    &connection,
                    &single_segment(&connection, &rollout),
                    scope,
                    "",
                    order,
                    "absent needle",
                    None,
                    200,
                    &state,
                    &cancelled,
                )
                .unwrap();
                assert_eq!(absent["matchCount"], 0);
                assert!(absent["prompts"].as_array().unwrap().is_empty());
            }
        }
    }

    #[test]
    fn persistent_cursor_is_bound_to_generation_and_filters() {
        let segments = [IndexedSegment {
            file_id: 7,
            base: 100,
            prompt_count: 50,
        }];
        let cursor = encode_cursor("generation-a", "query-a", 42, Some(7), false);
        assert_eq!(
            decode_cursor(Some(&cursor), "generation-a", "query-a", &segments).unwrap(),
            Some((142, false))
        );
        assert!(
            decode_cursor(Some(&cursor), "generation-b", "query-a", &segments)
                .unwrap_err()
                .contains("stale")
        );
        assert!(
            decode_cursor(Some(&cursor), "generation-a", "query-b", &segments)
                .unwrap_err()
                .contains("filters")
        );
        assert!(decode_cursor(Some(&cursor), "generation-a", "query-a", &[]).is_err());
        let outside = encode_cursor("generation-a", "query-a", 51, Some(7), false);
        assert!(decode_cursor(Some(&outside), "generation-a", "query-a", &segments).is_err());
        let empty = encode_cursor("generation-a", "query-a", 0, None, false);
        assert_eq!(
            decode_cursor(Some(&empty), "generation-a", "query-a", &segments).unwrap(),
            None
        );
    }

    #[test]
    fn continued_prompt_cursor_survives_predecessor_index_growth() {
        let directory = tempdir().unwrap();
        let old_path = directory.path().join("old.jsonl");
        let current_path = directory.path().join("current.jsonl");
        let mut old = File::create(&old_path).unwrap();
        let mut current = File::create(&current_path).unwrap();
        write_record(&mut old, "needle old", 1);
        for index in 1..=3 {
            write_record(&mut current, &format!("needle current {index}"), index);
        }
        let locator = ThreadLocator {
            id: "continued".into(),
            title: "Continued".into(),
            rollout_path: current_path.clone(),
            rollout_paths: vec![old_path.clone(), current_path],
        };
        let cancelled = AtomicBool::new(false);
        let mut connection = connect(&directory.path().join("index.sqlite")).unwrap();
        let (segments, state) =
            advance_thread_index(&mut connection, &locator, 1_000, &cancelled).unwrap();
        let mut cursors = Vec::new();
        for order in ["asc", "desc"] {
            let page = read_page(
                &connection,
                &segments,
                "pure",
                "",
                order,
                "needle",
                None,
                2,
                &state,
                &cancelled,
            )
            .unwrap();
            cursors.push((order, page["nextCursor"].as_str().unwrap().to_string()));
        }
        write_record(&mut old, "needle discovered later", 9);
        let (grown_segments, grown_state) =
            advance_thread_index(&mut connection, &locator, 1_000, &cancelled).unwrap();
        assert_ne!(state.generation, grown_state.generation);
        assert_eq!(state.cursor_generation, grown_state.cursor_generation);
        for (order, cursor) in cursors {
            let page = read_page(
                &connection,
                &grown_segments,
                "pure",
                "",
                order,
                "needle",
                Some(&cursor),
                20,
                &grown_state,
                &cancelled,
            )
            .unwrap();
            let texts = page["prompts"]
                .as_array()
                .unwrap()
                .iter()
                .map(|row| row["text"].as_str().unwrap())
                .collect::<Vec<_>>();
            assert_eq!(
                texts,
                if order == "asc" {
                    vec!["needle current 2", "needle current 3"]
                } else {
                    vec!["needle current 1", "needle discovered later", "needle old"]
                }
            );
        }
        let cursor = encode_cursor(
            &grown_state.cursor_generation,
            "query",
            1,
            Some(grown_segments[1].file_id),
            false,
        );
        std::fs::write(&old_path, b"").unwrap();
        let (replaced_segments, replaced_state) =
            advance_thread_index(&mut connection, &locator, 1_000, &cancelled).unwrap();
        assert!(decode_cursor(
            Some(&cursor),
            &replaced_state.cursor_generation,
            "query",
            &replaced_segments
        )
        .unwrap_err()
        .contains("stale"));
    }

    #[test]
    fn fixed_continuation_sources_page_without_advancing_scan_and_keep_stable_context() {
        let directory = tempdir().unwrap();
        let old_path = directory.path().join("old.jsonl");
        let current_path = directory.path().join("current.jsonl");
        let mut old = File::create(&old_path).unwrap();
        write_record(&mut old, "needle old 1", 1);
        let first_end = old.stream_position().unwrap();
        write_record(&mut old, "needle old 2", 2);
        write_record(&mut old, "needle old 3", 3);
        let mut current = File::create(&current_path).unwrap();
        write_record(&mut current, "needle current 1", 4);
        write_record(&mut current, "needle current 2", 5);
        let locator = ThreadLocator {
            id: "fixed-continuation".into(),
            title: "Fixed continuation".into(),
            rollout_path: current_path.clone(),
            rollout_paths: vec![old_path.clone(), current_path],
        };
        let database = prompt_index_database_path(directory.path()).unwrap();
        std::fs::create_dir_all(database.parent().unwrap()).unwrap();
        let mut connection = connect(&database).unwrap();
        let cancelled = AtomicBool::new(false);
        let (segments, _) =
            advance_thread_index(&mut connection, &locator, 1000, &cancelled).unwrap();
        let file_id = segments[0].file_id;
        // Model a persisted partial scan of unchanged, fully present source files.
        connection
            .execute(
                "DELETE FROM prompt_search_fts WHERE file_id=?1 AND prompt_index>1",
                [file_id],
            )
            .unwrap();
        connection
            .execute(
                "DELETE FROM prompts WHERE file_id=?1 AND prompt_index>1",
                [file_id],
            )
            .unwrap();
        connection.execute(
            "UPDATE prompt_files SET scanned_offset=?1,scanned_line_count=1,boundary_hash=?2,complete=0 WHERE id=?3",
            params![first_end as i64, boundary_hash(&old_path, first_end).unwrap(), file_id],
        ).unwrap();
        drop(connection);
        let mut query = PromptPageQuery {
            codex_home: None,
            cursor: None,
            limit: Some(2),
            search: Some("needle".into()),
            scope: Some("pure".into()),
            source_type: None,
            order: Some("desc".into()),
            strategy: Some("index".into()),
            request_id: Some("fixed-source".into()),
            scan_budget_ms: Some(0),
        };
        let read = |query: &PromptPageQuery| {
            read_persistent_prompt_page_inner(
                &locator,
                directory.path(),
                query,
                "pure",
                "",
                "desc",
                "needle",
                2,
                Arc::new(AtomicBool::new(false)),
                false,
            )
            .unwrap()
            .unwrap()
        };
        let first = read(&query);
        assert_eq!(first["prompts"][0]["text"], "needle current 2");
        query.cursor = first["nextCursor"].as_str().map(str::to_string);
        query.scan_budget_ms = Some(1000);
        let older = read(&query);
        assert_eq!(older["prompts"][0]["text"], "needle old 1");
        let connection = connect(&database).unwrap();
        let scanned: i64 = connection
            .query_row(
                "SELECT scanned_offset FROM prompt_files WHERE id=?1",
                [file_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            scanned, first_end as i64,
            "foreground cursor paging must not advance the scan"
        );
        drop(connection);
        query.cursor = None;
        let scan = read(&query);
        assert_eq!(scan["matchCount"], 5);
        assert_eq!(
            scan["index"]["cursorGeneration"],
            first["index"]["cursorGeneration"]
        );
        assert_ne!(scan["index"]["generation"], first["index"]["generation"]);
        let hit = &first["prompts"][0];
        let context = read_persistent_prompt_context(
            &locator,
            directory.path(),
            &PromptContextQuery {
                codex_home: None,
                anchor_index: hit["index"].as_u64().unwrap() as usize,
                anchor_byte_offset: hit["byteOffset"].as_i64(),
                anchor_record_id: hit["recordId"].as_str().map(str::to_string),
                generation: scan["index"]["cursorGeneration"]
                    .as_str()
                    .map(str::to_string),
                search: Some("needle".into()),
                scope: Some("pure".into()),
                source_type: None,
                before: Some(1),
                after: Some(1),
                scan_budget_ms: Some(0),
            },
        )
        .unwrap();
        let anchor = &context["prompts"][context["anchorOffset"].as_u64().unwrap() as usize];
        assert_eq!(anchor["recordId"], hit["recordId"]);
        assert_eq!(anchor["text"], hit["text"]);
        assert_ne!(anchor["index"], hit["index"]);
    }

    #[test]
    fn legacy_zero_identity_bootstraps_without_rebuilding_and_replacement_is_detected() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("source.jsonl");
        let mut file = File::create(&path).unwrap();
        write_record(&mut file, "original needle", 1);
        let cancelled = AtomicBool::new(false);
        let mut connection = connect(&directory.path().join("index.sqlite")).unwrap();
        let original = advance_index(&mut connection, &path, 1000, &cancelled).unwrap();
        connection
            .execute("UPDATE prompt_files SET device='dev:0',inode='ino:0'", [])
            .unwrap();
        let bootstrapped = advance_index(&mut connection, &path, 0, &cancelled).unwrap();
        assert!(!bootstrapped.reset);
        assert_eq!(original.generation, bootstrapped.generation);
        assert_ne!(bootstrapped.file_identity.inode, "ino:0");
        drop(file);
        std::fs::rename(&path, directory.path().join("previous.jsonl")).unwrap();
        std::fs::copy(directory.path().join("previous.jsonl"), &path).unwrap();
        let mut replacement = source_state(&path).unwrap();
        replacement.created_ns = bootstrapped.file_identity.created_ns;
        let (_, reset) = prepare_file_state(
            &mut connection,
            &path,
            &absolute_path(&path).unwrap().to_string_lossy(),
            &replacement,
        )
        .unwrap();
        assert!(reset, "a replaced handle identity must not be accepted just because creation time and boundary bytes agree");
    }

    #[test]
    fn rust_index_is_readable_by_python_schema_20() {
        let directory = tempdir().unwrap();
        let codex_home = directory.path().join("home");
        let index_root = directory.path().join("indexes");
        let rollout = directory.path().join("rollout.jsonl");
        std::fs::create_dir_all(&codex_home).unwrap();
        let mut source = File::create(&rollout).unwrap();
        write_record(&mut source, "Rust to Python interoperability", 1);
        let database = index_root.join(
            prompt_index_database_path(&codex_home)
                .unwrap()
                .file_name()
                .unwrap(),
        );
        std::fs::create_dir_all(&index_root).unwrap();
        let mut connection = connect(&database).unwrap();
        let cancelled = AtomicBool::new(false);
        let state = advance_index(&mut connection, &rollout, 1_000, &cancelled).unwrap();
        assert!(state.complete);
        drop(connection);

        let repository_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .unwrap();
        let script = r#"
import json, sys
from pathlib import Path
from backend.prompt_index import read_prompt_index_state, read_prompt_page
database, rollout = map(Path, sys.argv[1:3])
state = read_prompt_index_state(database, rollout)
assert state is not None, 'Python rejected the Rust index state'
page = read_prompt_page(database, rollout, scope='all', search='interoperability', source_type=None, cursor=None, limit=20, index_state=state)
print(json.dumps({'count': page['matchCount'], 'text': page['prompts'][0]['text']}))
"#;
        let output = Command::new("python")
            .arg("-c")
            .arg(script)
            .arg(&database)
            .arg(&rollout)
            .env("PYTHONPATH", repository_root)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let payload: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(payload["count"], 1);
        assert_eq!(payload["text"], "Rust to Python interoperability");
    }

    #[test]
    fn python_index_is_extended_without_rebuild_by_rust() {
        let directory = tempdir().unwrap();
        let codex_home = directory.path().join("home");
        let index_root = directory.path().join("indexes");
        let rollout = directory.path().join("rollout.jsonl");
        std::fs::create_dir_all(&codex_home).unwrap();
        let mut source = File::create(&rollout).unwrap();
        write_record(&mut source, "Python created row", 1);
        let repository_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .unwrap();
        let script = r#"
import sys
from pathlib import Path
from backend.codex_data import ensure_rollout_prompt_index
home, rollout = map(Path, sys.argv[1:3])
_, state = ensure_rollout_prompt_index(home, rollout, max_scan_ms=1000)
assert state['complete']
"#;
        let output = Command::new("python")
            .arg("-c")
            .arg(script)
            .arg(&codex_home)
            .arg(&rollout)
            .env("PYTHONPATH", repository_root)
            .env("CODEX_HOME_MANAGER_PROMPT_INDEX_ROOT", &index_root)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        write_record(&mut source, "Rust appended row", 2);
        let database = index_root.join(
            prompt_index_database_path(&codex_home)
                .unwrap()
                .file_name()
                .unwrap(),
        );
        let mut connection = connect(&database).unwrap();
        let cancelled = AtomicBool::new(false);
        let state = advance_index(&mut connection, &rollout, 1_000, &cancelled).unwrap();
        assert!(!state.reset);
        assert_eq!(state.scan_added_prompts, 1);
        let page = read_page(
            &connection,
            &single_segment(&connection, &rollout),
            "all",
            "",
            "asc",
            "row",
            None,
            20,
            &state,
            &cancelled,
        )
        .unwrap();
        assert_eq!(page["matchCount"], 2);
    }

    #[test]
    fn sparse_multi_gib_rollout_makes_bounded_forward_progress() {
        let directory = tempdir().unwrap();
        let rollout = directory.path().join("sparse-rollout.jsonl");
        let database = directory.path().join("index.sqlite");
        let mut source = File::create(&rollout).unwrap();
        write_record(&mut source, "prompt before sparse range", 1);
        source.set_len(2 * 1024 * 1024 * 1024).unwrap();
        let cancelled = AtomicBool::new(false);
        let mut connection = connect(&database).unwrap();
        let started = Instant::now();
        let state = advance_index(&mut connection, &rollout, 5, &cancelled).unwrap();
        assert!(started.elapsed() < std::time::Duration::from_secs(2));
        assert!(!state.complete);
        assert!(state.scanned_bytes > 0);
        assert!(state.scanned_bytes < state.file_size);
        let page = read_page(
            &connection,
            &single_segment(&connection, &rollout),
            "all",
            "",
            "asc",
            "",
            None,
            20,
            &state,
            &cancelled,
        )
        .unwrap();
        assert_eq!(page["prompts"][0]["text"], "prompt before sparse range");
    }

    #[test]
    fn persistent_request_maps_every_cancelled_stage_to_stable_error() {
        let directory = tempdir().unwrap();
        let rollout = directory.path().join("rollout.jsonl");
        File::create(&rollout).unwrap();
        let locator = ThreadLocator {
            id: "cancelled-thread".to_string(),
            title: "Cancelled".to_string(),
            rollout_path: rollout.clone(),
            rollout_paths: vec![rollout],
        };
        let query = PromptPageQuery {
            codex_home: Some(directory.path().to_string_lossy().into_owned()),
            cursor: None,
            limit: Some(20),
            search: None,
            scope: Some("all".to_string()),
            source_type: None,
            order: Some("asc".to_string()),
            strategy: Some("index".to_string()),
            request_id: Some("cancelled-request".to_string()),
            scan_budget_ms: Some(1_000),
        };
        let cancelled = Arc::new(AtomicBool::new(true));
        let error = read_persistent_prompt_page(
            &locator,
            directory.path(),
            &query,
            "all",
            "",
            "asc",
            "",
            20,
            cancelled,
        )
        .unwrap_err();
        assert_eq!(error, PROMPT_REQUEST_CANCELLED);
    }

    #[test]
    fn prompt_index_lock_wait_can_be_cancelled_without_releasing_the_owner() {
        let directory = tempdir().unwrap();
        let database = directory.path().join("index.sqlite");
        let owner = DatabaseLock::exclusive(&database).unwrap();
        let cancelled = Arc::new(AtomicBool::new(false));
        let worker_cancelled = cancelled.clone();
        let worker_database = database.clone();
        let (started_sender, started_receiver) = std::sync::mpsc::channel();
        let (result_sender, result_receiver) = std::sync::mpsc::channel();
        let worker = thread::spawn(move || {
            started_sender.send(()).unwrap();
            let result = DatabaseLock::exclusive_cancelled(&worker_database, &worker_cancelled)
                .map(|_| "unexpected acquisition".to_string())
                .unwrap_or_else(|error| error);
            result_sender.send(result).unwrap();
        });
        started_receiver
            .recv_timeout(Duration::from_secs(1))
            .unwrap();
        thread::sleep(Duration::from_millis(30));
        cancelled.store(true, std::sync::atomic::Ordering::Release);
        let result = result_receiver.recv_timeout(Duration::from_secs(1));
        let owner_still_held = DatabaseLock::try_exclusive(&database).unwrap().is_none();
        drop(owner);
        worker.join().unwrap();
        assert_eq!(result.unwrap(), PROMPT_REQUEST_CANCELLED);
        assert!(owner_still_held);
        assert!(DatabaseLock::try_exclusive(&database).unwrap().is_some());
    }

    #[cfg(windows)]
    #[test]
    fn rust_lock_blocks_python_cleanup_lock() {
        let directory = tempdir().unwrap();
        let database = directory.path().join("index.sqlite");
        let _lock = DatabaseLock::exclusive(&database).unwrap();
        let script = r#"
import msvcrt, sys
path = sys.argv[1] + '.lock'
with open(path, 'a+b') as handle:
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise SystemExit(0)
raise SystemExit(2)
"#;
        let status = Command::new("python")
            .arg("-c")
            .arg(script)
            .arg(&database)
            .status()
            .unwrap();
        assert!(status.success());
    }
}
