use std::{
    collections::{BTreeMap, HashMap},
    env, fs,
    path::{Path, PathBuf},
    sync::Mutex,
};

use axum::{
    extract::{Path as AxumPath, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use chrono::{DateTime, Local, NaiveDate, TimeZone};
use memchr::memmem;
use memmap2::Mmap;
use rusqlite::{params, Connection};
use serde::Deserialize;
use serde_json::{json, Map, Value};

use crate::AppState;

const TOKEN_MARKER: &[u8] = b"token_count";

#[derive(Debug, Deserialize)]
pub struct DailyTokenQuery {
    codex_home: Option<String>,
    #[allow(dead_code)]
    sidebar_limit: Option<usize>,
}

#[derive(Default)]
pub struct DailyTokenStore {
    memory: Mutex<HashMap<String, (u64, i64, RolloutUsage)>>,
}

impl DailyTokenStore {
    fn build_thread_tree_usage(&self, threads: &[Value]) -> Result<Value, String> {
        let connection = open_cache().ok();
        build_thread_tree_usage(threads, self, connection.as_ref())
    }
}

#[derive(Clone, Default)]
struct RolloutUsage {
    days: BTreeMap<String, DayUsage>,
    tokens: u64,
    token_events: u64,
    counted_token_events: u64,
    zero_delta_token_events: u64,
    fallback_token_events: u64,
    parse_errors: u64,
    missing_file: bool,
    unknown_token_threads: u64,
}

#[derive(Clone, Default)]
struct DayUsage {
    tokens: u64,
    token_events: u64,
    unknown_token_threads: u64,
}

pub async fn thread_daily_tokens(
    State(state): State<AppState>,
    AxumPath(thread_id): AxumPath<String>,
    Query(query): Query<DailyTokenQuery>,
) -> Response {
    let store = state.catalog.clone();
    let daily_tokens = state.daily_tokens.clone();
    let result = tokio::task::spawn_blocking(move || {
        let threads = store.thread_tree(query.codex_home.as_deref(), &thread_id)?;
        daily_tokens.build_thread_tree_usage(&threads)
    })
    .await;
    match result {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) if error.starts_with("thread not found:") => {
            (StatusCode::NOT_FOUND, Json(json!({"detail": error}))).into_response()
        }
        Ok(Err(error)) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"detail": error})),
        )
            .into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"detail": format!("daily token worker failed: {error}")})),
        )
            .into_response(),
    }
}

fn build_thread_tree_usage(
    threads: &[Value],
    store: &DailyTokenStore,
    connection: Option<&Connection>,
) -> Result<Value, String> {
    let mut usages = Vec::with_capacity(threads.len());
    for thread in threads {
        usages.push(usage_for_thread(thread, store, connection)?);
    }
    let own = usages.first().cloned().unwrap_or_default();
    let children = &usages[usages.len().min(1)..];
    let mut records: BTreeMap<String, Value> = BTreeMap::new();

    merge_usage_days(&mut records, &own, true);
    for child in children {
        merge_usage_days(&mut records, child, false);
    }

    let dated_dates: Vec<NaiveDate> = records
        .keys()
        .filter_map(|date| NaiveDate::parse_from_str(date, "%Y-%m-%d").ok())
        .collect();
    let mut days = Vec::new();
    if let (Some(first), Some(last)) = (dated_dates.first(), dated_dates.last()) {
        let mut current = *first;
        while current <= *last {
            let date_key = current.format("%Y-%m-%d").to_string();
            days.push(
                records
                    .remove(&date_key)
                    .unwrap_or_else(|| zero_day(&date_key)),
            );
            let Some(next) = current.succ_opt() else {
                break;
            };
            current = next;
        }
    } else {
        days.extend(records.into_values());
    }

    let active_days: Vec<&Value> = days
        .iter()
        .filter(|day| value_u64(day, "totalTokens") > 0)
        .collect();
    let peak = active_days
        .iter()
        .copied()
        .max_by_key(|day| value_u64(day, "totalTokens"));
    let child_tokens = children.iter().map(|usage| usage.tokens).sum::<u64>();
    let child_token_events = children.iter().map(|usage| usage.token_events).sum::<u64>();
    let child_counted_events = children
        .iter()
        .map(|usage| usage.counted_token_events)
        .sum::<u64>();
    let child_zero_events = children
        .iter()
        .map(|usage| usage.zero_delta_token_events)
        .sum::<u64>();
    let child_fallback_events = children
        .iter()
        .map(|usage| usage.fallback_token_events)
        .sum::<u64>();
    let child_unknown_threads = children
        .iter()
        .map(|usage| usage.unknown_token_threads)
        .sum::<u64>();
    let unknown_days = days
        .iter()
        .filter(|day| value_u64(day, "unknownTokenThreads") > 0)
        .count();

    Ok(json!({
        "summary": {
            "ownTokens": own.tokens,
            "childTokens": child_tokens,
            "totalTokens": own.tokens + child_tokens,
            "days": active_days.len(),
            "activeDays": active_days.len(),
            "rangeDays": days.len(),
            "zeroDays": days.len().saturating_sub(active_days.len()),
            "unknownDays": unknown_days,
            "firstDate": days.first().and_then(|day| day.get("date")).cloned().unwrap_or(Value::Null),
            "lastDate": days.last().and_then(|day| day.get("date")).cloned().unwrap_or(Value::Null),
            "peakDate": peak.and_then(|day| day.get("date")).cloned().unwrap_or(Value::Null),
            "peakTokens": peak.map(|day| value_u64(day, "totalTokens")).unwrap_or(0),
            "ownTokenEvents": own.token_events,
            "childTokenEvents": child_token_events,
            "ownCountedTokenEvents": own.counted_token_events,
            "childCountedTokenEvents": child_counted_events,
            "zeroDeltaTokenEvents": own.zero_delta_token_events + child_zero_events,
            "fallbackTokenEvents": own.fallback_token_events + child_fallback_events,
            "ownUnknownTokenThreads": own.unknown_token_threads,
            "childUnknownTokenThreads": child_unknown_threads,
            "unknownTokenThreads": own.unknown_token_threads + child_unknown_threads,
            "childThreadCount": children.len(),
            "missingChildRolloutFiles": children.iter().filter(|usage| usage.missing_file).count(),
        },
        "days": days,
    }))
}

fn usage_for_thread(
    thread: &Value,
    store: &DailyTokenStore,
    connection: Option<&Connection>,
) -> Result<RolloutUsage, String> {
    let rollout_path = thread
        .get("rolloutPath")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let mut usage = parse_rollout_usage(Path::new(rollout_path), store, connection)?;
    if usage.tokens > 0 {
        return Ok(usage);
    }
    let sqlite_tokens = value_u64(thread, "tokensUsed");
    if sqlite_tokens == 0 {
        return Ok(usage);
    }
    let timestamp_ms = ["updatedAtMs", "fileModifiedAtMs", "createdAtMs"]
        .iter()
        .find_map(|key| thread.get(*key).and_then(Value::as_i64));
    let Some(date_key) = timestamp_ms.and_then(local_date_from_millis) else {
        return Ok(usage);
    };
    usage.days.insert(
        date_key,
        DayUsage {
            unknown_token_threads: 1,
            ..DayUsage::default()
        },
    );
    usage.unknown_token_threads = 1;
    Ok(usage)
}

fn parse_rollout_usage(
    path: &Path,
    store: &DailyTokenStore,
    connection: Option<&Connection>,
) -> Result<RolloutUsage, String> {
    if !path.is_file() {
        return Ok(RolloutUsage {
            missing_file: true,
            ..RolloutUsage::default()
        });
    }
    let metadata =
        fs::metadata(path).map_err(|error| format!("cannot stat {}: {error}", path.display()))?;
    let file_size = metadata.len();
    let file_mtime_ns = metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|value| value.as_nanos().min(i64::MAX as u128) as i64)
        .unwrap_or_default();
    let cache_key = path.to_string_lossy().to_string();
    if let Some((_, _, cached)) = store
        .memory
        .lock()
        .map_err(|_| "daily token memory cache is unavailable".to_string())?
        .get(&cache_key)
        .filter(|(size, modified, _)| *size == file_size && *modified == file_mtime_ns)
    {
        return Ok(cached.clone());
    }
    if let Some(cached) = read_persistent_cache(connection, path, file_size, file_mtime_ns) {
        let usage = usage_from_cached_json(&cached)?;
        store
            .memory
            .lock()
            .map_err(|_| "daily token memory cache is unavailable".to_string())?
            .insert(cache_key, (file_size, file_mtime_ns, usage.clone()));
        return Ok(usage);
    }

    let file =
        fs::File::open(path).map_err(|error| format!("cannot open {}: {error}", path.display()))?;
    let mmap = unsafe { Mmap::map(&file) }
        .map_err(|error| format!("cannot map {}: {error}", path.display()))?;
    let mut usage = RolloutUsage::default();
    let mut previous_cumulative_tokens: Option<u64> = None;
    let mut start = 0usize;
    while start < mmap.len() {
        let end = memchr::memchr(b'\n', &mmap[start..])
            .map(|offset| start + offset)
            .unwrap_or(mmap.len());
        let line = &mmap[start..end];
        start = end.saturating_add(1);
        if memmem::find(line, TOKEN_MARKER).is_none() {
            continue;
        }
        let item: Value = match serde_json::from_slice(line) {
            Ok(value) => value,
            Err(_) => {
                usage.parse_errors += 1;
                continue;
            }
        };
        let Some(payload) = token_count_payload(&item) else {
            continue;
        };
        usage.token_events += 1;
        let Some(date_key) = item
            .get("timestamp")
            .and_then(Value::as_str)
            .and_then(local_date_from_timestamp)
        else {
            usage.zero_delta_token_events += 1;
            continue;
        };
        let info = payload.get("info").and_then(Value::as_object);
        let cumulative = info.and_then(|value| {
            token_usage_total(
                value
                    .get("total_token_usage")
                    .or_else(|| value.get("totalTokenUsage")),
            )
        });
        let last = info.and_then(|value| {
            token_usage_total(
                value
                    .get("last_token_usage")
                    .or_else(|| value.get("lastTokenUsage")),
            )
        });
        let delta = if let Some(cumulative) = cumulative {
            let delta = match previous_cumulative_tokens {
                None => Some(cumulative),
                Some(previous) if cumulative >= previous => Some(cumulative - previous),
                Some(_) if last.is_some() => {
                    usage.fallback_token_events += 1;
                    last
                }
                Some(_) => {
                    usage.fallback_token_events += 1;
                    Some(cumulative)
                }
            };
            previous_cumulative_tokens = Some(cumulative);
            delta
        } else if last.is_some() {
            usage.fallback_token_events += 1;
            last
        } else {
            None
        };
        let Some(delta) = delta.filter(|value| *value > 0) else {
            usage.zero_delta_token_events += 1;
            continue;
        };
        let day = usage.days.entry(date_key).or_default();
        day.tokens += delta;
        day.token_events += 1;
        usage.tokens += delta;
        usage.counted_token_events += 1;
    }
    let cached = usage_to_cache_json(&usage);
    let _ = write_persistent_cache(connection, path, file_size, file_mtime_ns, &cached);
    store
        .memory
        .lock()
        .map_err(|_| "daily token memory cache is unavailable".to_string())?
        .insert(cache_key, (file_size, file_mtime_ns, usage.clone()));
    Ok(usage)
}

fn token_count_payload(item: &Value) -> Option<&Value> {
    (item.get("type").and_then(Value::as_str) == Some("event_msg"))
        .then(|| item.get("payload"))
        .flatten()
        .filter(|payload| payload.get("type").and_then(Value::as_str) == Some("token_count"))
}

fn token_usage_total(value: Option<&Value>) -> Option<u64> {
    let usage = value?.as_object()?;
    usage
        .get("total_tokens")
        .or_else(|| usage.get("totalTokens"))
        .and_then(Value::as_u64)
        .or_else(|| {
            let input = usage
                .get("input_tokens")
                .or_else(|| usage.get("inputTokens"))
                .and_then(Value::as_u64)?;
            let output = usage
                .get("output_tokens")
                .or_else(|| usage.get("outputTokens"))
                .and_then(Value::as_u64)?;
            Some(input + output)
        })
}

fn merge_usage_days(records: &mut BTreeMap<String, Value>, usage: &RolloutUsage, own: bool) {
    for (date, day) in &usage.days {
        let record = records
            .entry(date.clone())
            .or_insert_with(|| zero_day(date));
        let object = record
            .as_object_mut()
            .expect("daily token record is an object");
        add_number(
            object,
            if own { "ownTokens" } else { "childTokens" },
            day.tokens,
        );
        add_number(
            object,
            if own {
                "ownTokenEvents"
            } else {
                "childTokenEvents"
            },
            day.token_events,
        );
        add_number(
            object,
            if own {
                "ownUnknownTokenThreads"
            } else {
                "childUnknownTokenThreads"
            },
            day.unknown_token_threads,
        );
        let own_tokens = object.get("ownTokens").and_then(Value::as_u64).unwrap_or(0);
        let child_tokens = object
            .get("childTokens")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let own_unknown = object
            .get("ownUnknownTokenThreads")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let child_unknown = object
            .get("childUnknownTokenThreads")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        object.insert("totalTokens".to_string(), json!(own_tokens + child_tokens));
        object.insert(
            "unknownTokenThreads".to_string(),
            json!(own_unknown + child_unknown),
        );
        object.insert("hasData".to_string(), json!(own_tokens + child_tokens > 0));
        object.insert(
            "hasUnknownTokens".to_string(),
            json!(own_unknown + child_unknown > 0),
        );
    }
}

fn add_number(object: &mut Map<String, Value>, key: &str, value: u64) {
    let current = object.get(key).and_then(Value::as_u64).unwrap_or(0);
    object.insert(key.to_string(), json!(current + value));
}

fn zero_day(date: &str) -> Value {
    json!({
        "date": date,
        "ownTokens": 0,
        "childTokens": 0,
        "totalTokens": 0,
        "ownTokenEvents": 0,
        "childTokenEvents": 0,
        "ownUnknownTokenThreads": 0,
        "childUnknownTokenThreads": 0,
        "unknownTokenThreads": 0,
        "hasData": false,
        "hasUnknownTokens": false,
    })
}

fn local_date_from_timestamp(timestamp: &str) -> Option<String> {
    DateTime::parse_from_rfc3339(timestamp).ok().map(|value| {
        value
            .with_timezone(&Local)
            .date_naive()
            .format("%Y-%m-%d")
            .to_string()
    })
}

fn local_date_from_millis(timestamp_ms: i64) -> Option<String> {
    Local
        .timestamp_millis_opt(timestamp_ms)
        .single()
        .map(|value| value.date_naive().format("%Y-%m-%d").to_string())
}

fn value_u64(value: &Value, key: &str) -> u64 {
    value.get(key).and_then(Value::as_u64).unwrap_or(0)
}

fn cache_path() -> PathBuf {
    env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env::var_os("USERPROFILE").unwrap_or_default()).join("AppData/Local")
        })
        .join("CodexHomeManager/daily-token-cache.sqlite3")
}

fn open_cache() -> rusqlite::Result<Connection> {
    let path = cache_path();
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let connection = Connection::open(path)?;
    connection.execute_batch(
        "CREATE TABLE IF NOT EXISTS rollout_daily_tokens (
            rollout_path TEXT PRIMARY KEY,
            file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );",
    )?;
    Ok(connection)
}

fn read_persistent_cache(
    connection: Option<&Connection>,
    path: &Path,
    file_size: u64,
    file_mtime_ns: i64,
) -> Option<Value> {
    let connection = connection?;
    let mut statement = connection
        .prepare("SELECT result_json FROM rollout_daily_tokens WHERE rollout_path = ? AND file_size = ? AND file_mtime_ns = ?")
        .ok()?;
    let text: String = statement
        .query_row(
            params![path.to_string_lossy(), file_size as i64, file_mtime_ns],
            |row| row.get(0),
        )
        .ok()?;
    serde_json::from_str(&text).ok()
}

fn write_persistent_cache(
    connection: Option<&Connection>,
    path: &Path,
    file_size: u64,
    file_mtime_ns: i64,
    value: &Value,
) -> rusqlite::Result<()> {
    let Some(connection) = connection else {
        return Ok(());
    };
    connection.execute(
        "INSERT INTO rollout_daily_tokens (rollout_path, file_size, file_mtime_ns, result_json, updated_at)
         VALUES (?, ?, ?, ?, unixepoch('subsec'))
         ON CONFLICT(rollout_path) DO UPDATE SET file_size=excluded.file_size, file_mtime_ns=excluded.file_mtime_ns,
             result_json=excluded.result_json, updated_at=excluded.updated_at",
        params![path.to_string_lossy(), file_size as i64, file_mtime_ns, value.to_string()],
    )?;
    Ok(())
}

fn usage_to_cache_json(usage: &RolloutUsage) -> Value {
    let days: Vec<Value> = usage
        .days
        .iter()
        .map(|(date, day)| json!({"date": date, "tokens": day.tokens, "tokenEvents": day.token_events}))
        .collect();
    let peak = usage.days.iter().max_by_key(|(_, day)| day.tokens);
    json!({
        "summary": {
            "tokens": usage.tokens,
            "days": usage.days.len(),
            "firstDate": usage.days.keys().next(),
            "lastDate": usage.days.keys().next_back(),
            "peakDate": peak.map(|(date, _)| date),
            "peakTokens": peak.map(|(_, day)| day.tokens).unwrap_or(0),
            "tokenEvents": usage.token_events,
            "countedTokenEvents": usage.counted_token_events,
            "zeroDeltaTokenEvents": usage.zero_delta_token_events,
            "fallbackTokenEvents": usage.fallback_token_events,
            "parseErrors": usage.parse_errors,
            "missingFile": usage.missing_file,
        },
        "days": days,
    })
}

fn usage_from_cached_json(value: &Value) -> Result<RolloutUsage, String> {
    let summary = value
        .get("summary")
        .and_then(Value::as_object)
        .ok_or("daily token cache summary is invalid")?;
    let mut usage = RolloutUsage {
        tokens: summary.get("tokens").and_then(Value::as_u64).unwrap_or(0),
        token_events: summary
            .get("tokenEvents")
            .and_then(Value::as_u64)
            .unwrap_or(0),
        counted_token_events: summary
            .get("countedTokenEvents")
            .and_then(Value::as_u64)
            .unwrap_or(0),
        zero_delta_token_events: summary
            .get("zeroDeltaTokenEvents")
            .and_then(Value::as_u64)
            .unwrap_or(0),
        fallback_token_events: summary
            .get("fallbackTokenEvents")
            .and_then(Value::as_u64)
            .unwrap_or(0),
        parse_errors: summary
            .get("parseErrors")
            .and_then(Value::as_u64)
            .unwrap_or(0),
        missing_file: summary
            .get("missingFile")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        ..RolloutUsage::default()
    };
    for day in value
        .get("days")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some(date) = day.get("date").and_then(Value::as_str) else {
            continue;
        };
        usage.days.insert(
            date.to_string(),
            DayUsage {
                tokens: day.get("tokens").and_then(Value::as_u64).unwrap_or(0),
                token_events: day.get("tokenEvents").and_then(Value::as_u64).unwrap_or(0),
                unknown_token_threads: 0,
            },
        );
    }
    Ok(usage)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;

    fn event(timestamp: &str, total: u64, last: u64) -> String {
        json!({
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": total},
                "last_token_usage": {"total_tokens": last}
            }}
        })
        .to_string()
    }

    #[test]
    fn cumulative_events_are_converted_to_daily_deltas() {
        let mut file = NamedTempFile::new().unwrap();
        writeln!(file, "{}", event("2026-06-03T12:00:00Z", 100, 100)).unwrap();
        writeln!(file, "{}", event("2026-06-03T13:00:00Z", 160, 60)).unwrap();
        writeln!(file, "{}", event("2026-06-05T12:00:00Z", 200, 40)).unwrap();
        let usage = parse_rollout_usage(file.path(), &DailyTokenStore::default(), None).unwrap();
        assert_eq!(usage.tokens, 200);
        assert_eq!(usage.counted_token_events, 3);
        assert_eq!(usage.days.values().map(|day| day.tokens).sum::<u64>(), 200);
    }

    #[test]
    fn thread_tree_includes_zero_days_and_child_totals() {
        let mut own_file = NamedTempFile::new().unwrap();
        let mut child_file = NamedTempFile::new().unwrap();
        writeln!(own_file, "{}", event("2026-06-03T12:00:00Z", 100, 100)).unwrap();
        writeln!(own_file, "{}", event("2026-06-05T12:00:00Z", 125, 25)).unwrap();
        writeln!(child_file, "{}", event("2026-06-03T12:30:00Z", 30, 30)).unwrap();
        let threads = vec![
            json!({"rolloutPath": own_file.path(), "tokensUsed": 125}),
            json!({"rolloutPath": child_file.path(), "tokensUsed": 30}),
        ];
        let result = build_thread_tree_usage(&threads, &DailyTokenStore::default(), None).unwrap();
        assert_eq!(result["summary"]["totalTokens"], 155);
        assert_eq!(result["summary"]["rangeDays"], 3);
        assert_eq!(result["days"][1]["totalTokens"], 0);
    }
}
