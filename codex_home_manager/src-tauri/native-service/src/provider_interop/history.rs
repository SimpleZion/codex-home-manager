use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, BufWriter, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::UNIX_EPOCH,
};

use memchr::memmem;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::InteropError;

const MAX_JSONL_LINE_BYTES: usize = 64 * 1024 * 1024;

#[derive(Clone, Debug)]
pub struct HistoryAuditRequest {
    pub codex_home: PathBuf,
    pub source_bucket: String,
    pub target_bucket: String,
    pub operation_id: String,
    pub prepare_temporary_rewrites: bool,
    pub provider_rewrites: bool,
    pub continuation_rewrites: bool,
    pub selected_paths: Option<Vec<PathBuf>>,
}

impl HistoryAuditRequest {
    pub fn new(
        codex_home: impl AsRef<Path>,
        source_bucket: impl Into<String>,
        target_bucket: impl Into<String>,
        operation_id: impl Into<String>,
    ) -> Self {
        Self {
            codex_home: codex_home.as_ref().to_path_buf(),
            source_bucket: source_bucket.into(),
            target_bucket: target_bucket.into(),
            operation_id: operation_id.into(),
            prepare_temporary_rewrites: false,
            provider_rewrites: true,
            continuation_rewrites: false,
            selected_paths: None,
        }
    }

    pub fn with_temporary_rewrites(mut self, enabled: bool) -> Self {
        self.prepare_temporary_rewrites = enabled;
        self
    }

    pub fn with_continuation_rewrites(mut self, enabled: bool) -> Self {
        self.continuation_rewrites = enabled;
        self
    }

    pub fn with_provider_rewrites(mut self, enabled: bool) -> Self {
        self.provider_rewrites = enabled;
        self
    }

    pub fn with_selected_paths(mut self, paths: Vec<PathBuf>) -> Self {
        self.selected_paths = Some(paths);
        self
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ContinuationCandidate {
    pub path: String,
    pub size_bytes: u64,
    pub modified_ns: u128,
    pub active_tail_offset: u64,
    pub active_tail_bytes: u64,
    pub nonportable_reasoning_count: u64,
    pub incompatible_item_id_count: u64,
    pub unsupported_foreign_item_count: u64,
    pub parse_error_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ContinuationInventory {
    pub candidates: Vec<ContinuationCandidate>,
    pub scanned_file_count: u64,
    pub candidate_file_count: u64,
    pub active_tail_scanned_bytes: u64,
    pub nonportable_reasoning_count: u64,
    pub incompatible_item_id_count: u64,
    pub unsupported_foreign_item_count: u64,
    pub parse_error_count: u64,
}

#[derive(Clone, Default)]
pub struct CancellationFlag(Arc<AtomicBool>);

impl CancellationFlag {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }

    fn check(&self) -> Result<(), InteropError> {
        if self.0.load(Ordering::Acquire) {
            Err(InteropError::new(
                "operation_cancelled",
                "provider interoperability history audit was cancelled",
            ))
        } else {
            Ok(())
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PromptFingerprint {
    pub count: u64,
    pub sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HistoryFilePlan {
    pub path: String,
    pub size_bytes: u64,
    pub modified_ns: u128,
    pub line_count: u64,
    pub parse_error_count: u64,
    pub replacement_count: u64,
    pub nonportable_reasoning_count: u64,
    pub incompatible_item_id_count: u64,
    pub unsupported_foreign_item_count: u64,
    pub source_sha256: String,
    pub normalized_sha256: String,
    pub output_sha256: Option<String>,
    pub temporary_path: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HistoryAudit {
    pub files: Vec<HistoryFilePlan>,
    pub scanned_file_count: u64,
    pub changed_file_count: u64,
    pub replacement_count: u64,
    pub continuation_changed_file_count: u64,
    pub nonportable_reasoning_count: u64,
    pub incompatible_item_id_count: u64,
    pub unsupported_foreign_item_count: u64,
    pub parse_error_count: u64,
    pub required_backup_bytes: u64,
    pub provider_buckets: BTreeMap<String, u64>,
    pub prompt_fingerprint: PromptFingerprint,
    pub protected_content_sha256: String,
}

struct ScanContext<'a> {
    request: &'a HistoryAuditRequest,
    cancellation: &'a CancellationFlag,
    provider_buckets: &'a mut BTreeMap<String, u64>,
    prompt_hasher: &'a mut Sha256,
    prompt_count: &'a mut u64,
    protected_hasher: &'a mut Sha256,
}

pub fn audit_history(
    request: HistoryAuditRequest,
    cancellation: CancellationFlag,
) -> Result<HistoryAudit, InteropError> {
    validate_request(&request)?;
    cancellation.check()?;
    let codex_home = request.codex_home.canonicalize().map_err(|_| {
        InteropError::new(
            "codex_home_not_found",
            "selected Codex Home cannot be resolved",
        )
    })?;
    let mut paths = if let Some(selected) = request.selected_paths.as_ref() {
        validate_selected_paths(&codex_home, selected)?
    } else {
        let mut discovered = Vec::new();
        collect_jsonl_files(&codex_home.join("sessions"), &mut discovered)?;
        collect_jsonl_files(&codex_home.join("archived_sessions"), &mut discovered)?;
        discovered
    };
    paths.sort_by_cached_key(|path| path_sort_key(path));

    let mut provider_buckets = BTreeMap::new();
    let mut prompt_hasher = Sha256::new();
    let mut protected_hasher = Sha256::new();
    let mut prompt_count = 0u64;
    let mut files = Vec::with_capacity(paths.len());
    for path in paths {
        cancellation.check()?;
        protected_hasher.update(path_sort_key(&path).as_bytes());
        protected_hasher.update([0]);
        files.push(scan_file(
            &path,
            &mut ScanContext {
                request: &request,
                cancellation: &cancellation,
                provider_buckets: &mut provider_buckets,
                prompt_hasher: &mut prompt_hasher,
                prompt_count: &mut prompt_count,
                protected_hasher: &mut protected_hasher,
            },
        )?);
    }
    let changed_file_count = files
        .iter()
        .filter(|file| file.replacement_count > 0)
        .count() as u64;
    let continuation_changed_file_count = files
        .iter()
        .filter(|file| file.nonportable_reasoning_count > 0 || file.incompatible_item_id_count > 0)
        .count() as u64;
    let replacement_count = files.iter().map(|file| file.replacement_count).sum();
    let nonportable_reasoning_count = files
        .iter()
        .map(|file| file.nonportable_reasoning_count)
        .sum();
    let incompatible_item_id_count = files
        .iter()
        .map(|file| file.incompatible_item_id_count)
        .sum();
    let unsupported_foreign_item_count = files
        .iter()
        .map(|file| file.unsupported_foreign_item_count)
        .sum();
    let parse_error_count = files.iter().map(|file| file.parse_error_count).sum();
    let required_backup_bytes = files
        .iter()
        .filter(|file| file.replacement_count > 0)
        .map(|file| file.size_bytes)
        .sum();
    Ok(HistoryAudit {
        scanned_file_count: files.len() as u64,
        changed_file_count,
        replacement_count,
        continuation_changed_file_count,
        nonportable_reasoning_count,
        incompatible_item_id_count,
        unsupported_foreign_item_count,
        parse_error_count,
        required_backup_bytes,
        provider_buckets,
        prompt_fingerprint: PromptFingerprint {
            count: prompt_count,
            sha256: hex_digest(prompt_hasher.finalize()),
        },
        protected_content_sha256: hex_digest(protected_hasher.finalize()),
        files,
    })
}

pub fn audit_continuation_candidates(
    codex_home: impl AsRef<Path>,
    cancellation: CancellationFlag,
) -> Result<ContinuationInventory, InteropError> {
    audit_continuation_paths(codex_home, None, cancellation)
}

pub fn audit_continuation_paths(
    codex_home: impl AsRef<Path>,
    selected_paths: Option<Vec<PathBuf>>,
    cancellation: CancellationFlag,
) -> Result<ContinuationInventory, InteropError> {
    cancellation.check()?;
    let codex_home = codex_home.as_ref().canonicalize().map_err(|_| {
        InteropError::new(
            "codex_home_not_found",
            "selected Codex Home cannot be resolved",
        )
    })?;
    let mut paths = if let Some(selected) = selected_paths.as_ref() {
        validate_selected_paths(&codex_home, selected)?
    } else {
        let mut discovered = Vec::new();
        collect_jsonl_files(&codex_home.join("sessions"), &mut discovered)?;
        collect_jsonl_files(&codex_home.join("archived_sessions"), &mut discovered)?;
        discovered
    };
    paths.sort_by_cached_key(|path| path_sort_key(path));
    let mut candidates = Vec::new();
    let mut active_tail_scanned_bytes = 0u64;
    let mut nonportable_reasoning_count = 0u64;
    let mut incompatible_item_id_count = 0u64;
    let mut unsupported_foreign_item_count = 0u64;
    let mut parse_error_count = 0u64;
    for path in &paths {
        cancellation.check()?;
        let candidate = scan_active_tail(path, &cancellation)?;
        active_tail_scanned_bytes += candidate.active_tail_bytes;
        nonportable_reasoning_count += candidate.nonportable_reasoning_count;
        incompatible_item_id_count += candidate.incompatible_item_id_count;
        unsupported_foreign_item_count += candidate.unsupported_foreign_item_count;
        parse_error_count += candidate.parse_error_count;
        if candidate.nonportable_reasoning_count > 0
            || candidate.incompatible_item_id_count > 0
            || candidate.unsupported_foreign_item_count > 0
            || candidate.parse_error_count > 0
        {
            candidates.push(candidate);
        }
    }
    Ok(ContinuationInventory {
        candidate_file_count: candidates.len() as u64,
        scanned_file_count: paths.len() as u64,
        candidates,
        active_tail_scanned_bytes,
        nonportable_reasoning_count,
        incompatible_item_id_count,
        unsupported_foreign_item_count,
        parse_error_count,
    })
}

fn validate_request(request: &HistoryAuditRequest) -> Result<(), InteropError> {
    if request.provider_rewrites && request.source_bucket.len() != request.target_bucket.len() {
        return Err(InteropError::new(
            "unsupported_provider_substitution",
            "source and target provider identities must have equal UTF-8 byte length",
        ));
    }
    if request.source_bucket.is_empty()
        || request.target_bucket.is_empty()
        || request.operation_id.trim().is_empty()
    {
        return Err(InteropError::new(
            "invalid_history_audit_request",
            "provider identities and operation ID must be non-empty",
        ));
    }
    Ok(())
}

fn collect_jsonl_files(root: &Path, output: &mut Vec<PathBuf>) -> Result<(), InteropError> {
    if !root.exists() {
        return Ok(());
    }
    let entries = fs::read_dir(root).map_err(|_| {
        InteropError::new(
            "history_scan_failed",
            "Codex history directory cannot be read",
        )
    })?;
    for entry in entries {
        let entry = entry.map_err(|_| {
            InteropError::new("history_scan_failed", "Codex history entry cannot be read")
        })?;
        let file_type = entry.file_type().map_err(|_| {
            InteropError::new(
                "history_scan_failed",
                "Codex history entry type cannot be read",
            )
        })?;
        if file_type.is_symlink() {
            continue;
        }
        if file_type.is_dir() {
            collect_jsonl_files(&entry.path(), output)?;
        } else if file_type.is_file()
            && entry
                .path()
                .extension()
                .is_some_and(|extension| extension == "jsonl")
        {
            output.push(entry.path());
        }
    }
    Ok(())
}

fn validate_selected_paths(
    codex_home: &Path,
    selected: &[PathBuf],
) -> Result<Vec<PathBuf>, InteropError> {
    let mut paths = Vec::with_capacity(selected.len());
    for path in selected {
        let resolved = path.canonicalize().map_err(|_| {
            InteropError::new(
                "history_selected_path_invalid",
                "selected rollout path cannot be resolved",
            )
        })?;
        if !resolved.starts_with(codex_home)
            || resolved
                .extension()
                .is_none_or(|extension| extension != "jsonl")
            || !resolved.is_file()
        {
            return Err(InteropError::new(
                "history_selected_path_invalid",
                "selected rollout path is outside Codex Home or is not JSONL",
            ));
        }
        paths.push(resolved);
    }
    paths.sort_by_cached_key(|path| path_sort_key(path));
    paths.dedup();
    Ok(paths)
}

fn scan_active_tail(
    path: &Path,
    cancellation: &CancellationFlag,
) -> Result<ContinuationCandidate, InteropError> {
    let before = FileVersion::read(path)?;
    let active_tail_offset = find_latest_compacted_offset(path, cancellation)?.unwrap_or(0);
    let source = File::open(path)
        .map_err(|_| InteropError::new("history_scan_failed", "rollout file cannot be opened"))?;
    let mut reader = BufReader::with_capacity(1024 * 1024, source);
    reader
        .seek(SeekFrom::Start(active_tail_offset))
        .map_err(|_| {
            InteropError::new(
                "history_scan_failed",
                "rollout active tail cannot be positioned",
            )
        })?;
    let mut line = Vec::new();
    let mut nonportable_reasoning_count = 0u64;
    let mut incompatible_item_id_count = 0u64;
    let mut unsupported_foreign_item_count = 0u64;
    let mut parse_error_count = 0u64;
    loop {
        cancellation.check()?;
        line.clear();
        let bytes_read = reader
            .read_until(b'\n', &mut line)
            .map_err(|_| InteropError::new("history_scan_failed", "rollout line cannot be read"))?;
        if bytes_read == 0 {
            break;
        }
        if line.len() > MAX_JSONL_LINE_BYTES {
            return Err(InteropError::new(
                "history_record_too_large",
                "rollout record exceeds 64 MiB",
            ));
        }
        let content = trim_line_ending(&line);
        if content.iter().all(u8::is_ascii_whitespace) {
            continue;
        }
        let mut value = match serde_json::from_slice::<Value>(content) {
            Ok(value) => value,
            Err(_) => {
                parse_error_count += 1;
                continue;
            }
        };
        let outcome = rewrite_continuation_record(&mut value)?;
        nonportable_reasoning_count += outcome.nonportable_reasoning_count;
        incompatible_item_id_count += outcome.incompatible_item_id_count;
        unsupported_foreign_item_count += outcome.unsupported_foreign_item_count;
    }
    let after = FileVersion::read(path)?;
    if before != after {
        return Err(InteropError::new(
            "history_source_changed",
            "rollout changed during provider interoperability audit",
        ));
    }
    Ok(ContinuationCandidate {
        path: path.to_string_lossy().into_owned(),
        size_bytes: before.size_bytes,
        modified_ns: before.modified_ns,
        active_tail_offset,
        active_tail_bytes: before.size_bytes.saturating_sub(active_tail_offset),
        nonportable_reasoning_count,
        incompatible_item_id_count,
        unsupported_foreign_item_count,
        parse_error_count,
    })
}

fn find_latest_compacted_offset(
    path: &Path,
    cancellation: &CancellationFlag,
) -> Result<Option<u64>, InteropError> {
    const REVERSE_CHUNK_BYTES: usize = 1024 * 1024;
    let mut source = File::open(path)
        .map_err(|_| InteropError::new("history_scan_failed", "rollout file cannot be opened"))?;
    let mut position = source
        .seek(SeekFrom::End(0))
        .map_err(|_| InteropError::new("history_scan_failed", "rollout size cannot be read"))?;
    let mut carry = Vec::new();
    while position > 0 {
        cancellation.check()?;
        let start = position.saturating_sub(REVERSE_CHUNK_BYTES as u64);
        let chunk_len = (position - start) as usize;
        let mut chunk = vec![0u8; chunk_len];
        source.seek(SeekFrom::Start(start)).map_err(|_| {
            InteropError::new("history_scan_failed", "rollout tail cannot be positioned")
        })?;
        source
            .read_exact(&mut chunk)
            .map_err(|_| InteropError::new("history_scan_failed", "rollout tail cannot be read"))?;
        chunk.extend_from_slice(&carry);
        let complete_start = if start == 0 {
            0
        } else if let Some(newline) = chunk.iter().position(|byte| *byte == b'\n') {
            newline + 1
        } else {
            if chunk.len() > MAX_JSONL_LINE_BYTES {
                return Err(InteropError::new(
                    "history_record_too_large",
                    "rollout record exceeds 64 MiB",
                ));
            }
            carry = chunk;
            position = start;
            continue;
        };
        let mut line_starts = vec![complete_start];
        for (index, byte) in chunk[complete_start..].iter().enumerate() {
            if *byte == b'\n' && complete_start + index + 1 < chunk.len() {
                line_starts.push(complete_start + index + 1);
            }
        }
        for line_start in line_starts.into_iter().rev() {
            let line_end = chunk[line_start..]
                .iter()
                .position(|byte| *byte == b'\n')
                .map_or(chunk.len(), |relative| line_start + relative + 1);
            let content = trim_line_ending(&chunk[line_start..line_end]);
            if content.is_empty() || memmem::find(content, b"\"compacted\"").is_none() {
                continue;
            }
            let valid = serde_json::from_slice::<Value>(content)
                .ok()
                .is_some_and(|value| {
                    value.get("type").and_then(Value::as_str) == Some("compacted")
                        && value
                            .get("payload")
                            .and_then(|payload| payload.get("replacement_history"))
                            .is_some_and(Value::is_array)
                });
            if valid {
                return Ok(Some(start + line_start as u64));
            }
        }
        carry = chunk[..complete_start].to_vec();
        if carry.len() > MAX_JSONL_LINE_BYTES {
            return Err(InteropError::new(
                "history_record_too_large",
                "rollout record exceeds 64 MiB",
            ));
        }
        position = start;
    }
    Ok(None)
}

fn scan_file(path: &Path, context: &mut ScanContext<'_>) -> Result<HistoryFilePlan, InteropError> {
    let before = FileVersion::read(path)?;
    let latest_compacted_line = if context.request.continuation_rewrites {
        find_latest_compacted_line(path, context.cancellation)?
    } else {
        None
    };
    let source = File::open(path)
        .map_err(|_| InteropError::new("history_scan_failed", "rollout file cannot be opened"))?;
    let temporary_path = temporary_path(path, &context.request.operation_id);
    let temporary = if context.request.prepare_temporary_rewrites {
        Some(
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary_path)
                .map_err(|_| {
                    InteropError::new(
                        "history_temporary_file_exists",
                        "history rewrite temporary file cannot be created",
                    )
                })?,
        )
    } else {
        None
    };
    let mut output = temporary.map(BufWriter::new);
    let mut reader = BufReader::with_capacity(1024 * 1024, source);
    let mut source_hasher = Sha256::new();
    let mut normalized_hasher = Sha256::new();
    let mut output_hasher = Sha256::new();
    let mut source_prompt_hasher = Sha256::new();
    let mut rewritten_prompt_hasher = Sha256::new();
    let mut source_prompt_count = 0u64;
    let mut rewritten_prompt_count = 0u64;
    let mut line = Vec::new();
    let mut line_count = 0u64;
    let mut parse_error_count = 0u64;
    let mut replacement_count = 0u64;
    let mut nonportable_reasoning_count = 0u64;
    let mut incompatible_item_id_count = 0u64;
    let mut unsupported_foreign_item_count = 0u64;
    loop {
        context.cancellation.check()?;
        line.clear();
        let bytes_read = reader
            .read_until(b'\n', &mut line)
            .map_err(|_| InteropError::new("history_scan_failed", "rollout line cannot be read"))?;
        if bytes_read == 0 {
            break;
        }
        if line.len() > MAX_JSONL_LINE_BYTES {
            return cleanup_error(
                output,
                &temporary_path,
                InteropError::new("history_record_too_large", "rollout record exceeds 64 MiB"),
            );
        }
        line_count += 1;
        source_hasher.update(&line);
        let content = trim_line_ending(&line);
        let parsed = if content.iter().all(u8::is_ascii_whitespace) {
            None
        } else {
            match serde_json::from_slice::<Value>(content) {
                Ok(value) => Some(value),
                Err(_) => {
                    parse_error_count += 1;
                    None
                }
            }
        };
        let parsed_successfully = parsed.is_some();
        if let Some(value) = parsed.as_ref() {
            add_prompts(value, context.prompt_hasher, context.prompt_count);
            add_prompts(value, &mut source_prompt_hasher, &mut source_prompt_count);
        }
        let replacement = parsed
            .as_ref()
            .and_then(session_provider)
            .map(|provider| {
                *context
                    .provider_buckets
                    .entry(provider.to_string())
                    .or_insert(0) += 1;
                context.request.provider_rewrites && provider == context.request.source_bucket
            })
            .unwrap_or(false);
        let mut rewritten = if replacement {
            let range = locate_provider_value(content, &context.request.source_bucket)?;
            let mut rewritten = line.clone();
            rewritten[range.clone()].copy_from_slice(context.request.target_bucket.as_bytes());
            replacement_count += 1;
            rewritten
        } else {
            line.clone()
        };
        let in_active_replay_context = latest_compacted_line.is_none_or(|line| line_count >= line);
        if context.request.continuation_rewrites && in_active_replay_context {
            if let Some(mut value) = parsed {
                let outcome = rewrite_continuation_record(&mut value)?;
                nonportable_reasoning_count += outcome.nonportable_reasoning_count;
                incompatible_item_id_count += outcome.incompatible_item_id_count;
                unsupported_foreign_item_count += outcome.unsupported_foreign_item_count;
                if outcome.drop_record {
                    rewritten.clear();
                    replacement_count += 1;
                } else if outcome.changed {
                    rewritten = serde_json::to_vec(&value).map_err(|_| {
                        InteropError::new(
                            "history_rewrite_failed",
                            "compatible rollout record cannot be serialized",
                        )
                    })?;
                    append_original_line_ending(&mut rewritten, &line);
                    replacement_count += 1;
                }
            }
        }
        normalized_hasher.update(&rewritten);
        context.protected_hasher.update(&rewritten);
        let rewritten_content = trim_line_ending(&rewritten);
        if parsed_successfully && !rewritten_content.is_empty() {
            let rewritten_value =
                serde_json::from_slice::<Value>(rewritten_content).map_err(|_| {
                    InteropError::new(
                        "history_preservation_failed",
                        "rewritten rollout record cannot be parsed for prompt verification",
                    )
                })?;
            add_prompts(
                &rewritten_value,
                &mut rewritten_prompt_hasher,
                &mut rewritten_prompt_count,
            );
        }
        if let Some(writer) = output.as_mut() {
            writer.write_all(&rewritten).map_err(|_| {
                InteropError::new(
                    "history_rewrite_failed",
                    "temporary rollout cannot be written",
                )
            })?;
            output_hasher.update(&rewritten);
        }
    }
    if let Some(mut writer) = output {
        writer.flush().map_err(|_| {
            InteropError::new(
                "history_rewrite_failed",
                "temporary rollout cannot be flushed",
            )
        })?;
        writer.get_ref().sync_all().map_err(|_| {
            InteropError::new(
                "history_rewrite_failed",
                "temporary rollout cannot be synchronized",
            )
        })?;
    }
    if source_prompt_count != rewritten_prompt_count
        || hex_digest(source_prompt_hasher.finalize())
            != hex_digest(rewritten_prompt_hasher.finalize())
    {
        let _ = fs::remove_file(&temporary_path);
        return Err(InteropError::new(
            "history_prompt_preservation_failed",
            "user prompt count, order, or content changed during compatibility rewrite",
        ));
    }
    let after = FileVersion::read(path)?;
    if before != after {
        let _ = fs::remove_file(&temporary_path);
        return Err(InteropError::new(
            "history_source_changed",
            "rollout changed during provider interoperability audit",
        ));
    }
    let normalized_sha256 = hex_digest(normalized_hasher.finalize());
    let output_sha256 = if context.request.prepare_temporary_rewrites {
        let digest = hex_digest(output_hasher.finalize());
        if replacement_count == 0 {
            let _ = fs::remove_file(&temporary_path);
            None
        } else if digest != normalized_sha256 {
            let _ = fs::remove_file(&temporary_path);
            return Err(InteropError::new(
                "history_preservation_failed",
                "temporary rollout differs outside the allowed provider field",
            ));
        } else {
            Some(digest)
        }
    } else {
        None
    };
    Ok(HistoryFilePlan {
        path: path.to_string_lossy().into_owned(),
        size_bytes: before.size_bytes,
        modified_ns: before.modified_ns,
        line_count,
        parse_error_count,
        replacement_count,
        nonportable_reasoning_count,
        incompatible_item_id_count,
        unsupported_foreign_item_count,
        source_sha256: hex_digest(source_hasher.finalize()),
        normalized_sha256,
        output_sha256,
        temporary_path: (replacement_count > 0 && context.request.prepare_temporary_rewrites)
            .then(|| temporary_path.to_string_lossy().into_owned()),
    })
}

enum ContinuationItemRewrite {
    Unchanged,
    DropReasoning,
    StripItemId,
    Unsupported,
}

#[derive(Default)]
struct ContinuationRecordRewrite {
    changed: bool,
    drop_record: bool,
    nonportable_reasoning_count: u64,
    incompatible_item_id_count: u64,
    unsupported_foreign_item_count: u64,
}

fn rewrite_continuation_record(
    value: &mut Value,
) -> Result<ContinuationRecordRewrite, InteropError> {
    if value.get("type").and_then(Value::as_str) == Some("response_item") {
        let Some(payload) = value.get_mut("payload") else {
            return Ok(ContinuationRecordRewrite::default());
        };
        return Ok(outcome_from_item_rewrite(rewrite_continuation_payload(
            payload,
        )?));
    }
    if value.get("type").and_then(Value::as_str) != Some("compacted") {
        return Ok(ContinuationRecordRewrite::default());
    }
    let Some(history) = value
        .get_mut("payload")
        .and_then(|payload| payload.get_mut("replacement_history"))
        .and_then(Value::as_array_mut)
    else {
        return Ok(ContinuationRecordRewrite::default());
    };
    let mut outcome = ContinuationRecordRewrite::default();
    let mut index = 0usize;
    while index < history.len() {
        match rewrite_continuation_payload(&mut history[index])? {
            ContinuationItemRewrite::Unchanged => index += 1,
            ContinuationItemRewrite::DropReasoning => {
                history.remove(index);
                outcome.changed = true;
                outcome.nonportable_reasoning_count += 1;
            }
            ContinuationItemRewrite::StripItemId => {
                outcome.changed = true;
                outcome.incompatible_item_id_count += 1;
                index += 1;
            }
            ContinuationItemRewrite::Unsupported => {
                outcome.unsupported_foreign_item_count += 1;
                index += 1;
            }
        }
    }
    Ok(outcome)
}

fn outcome_from_item_rewrite(rewrite: ContinuationItemRewrite) -> ContinuationRecordRewrite {
    match rewrite {
        ContinuationItemRewrite::Unchanged => ContinuationRecordRewrite::default(),
        ContinuationItemRewrite::DropReasoning => ContinuationRecordRewrite {
            changed: true,
            drop_record: true,
            nonportable_reasoning_count: 1,
            ..ContinuationRecordRewrite::default()
        },
        ContinuationItemRewrite::StripItemId => ContinuationRecordRewrite {
            changed: true,
            incompatible_item_id_count: 1,
            ..ContinuationRecordRewrite::default()
        },
        ContinuationItemRewrite::Unsupported => ContinuationRecordRewrite {
            unsupported_foreign_item_count: 1,
            ..ContinuationRecordRewrite::default()
        },
    }
}

fn rewrite_continuation_payload(
    payload: &mut Value,
) -> Result<ContinuationItemRewrite, InteropError> {
    let Some(payload) = payload.as_object_mut() else {
        return Ok(ContinuationItemRewrite::Unchanged);
    };
    let item_id = payload.get("id").and_then(Value::as_str);
    if !item_id.is_some_and(|item_id| item_id.starts_with("item_")) {
        return Ok(ContinuationItemRewrite::Unchanged);
    }
    let item_type = payload
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if item_type == "reasoning" {
        return Ok(ContinuationItemRewrite::DropReasoning);
    }
    let migratable = match item_type {
        "message" => {
            payload.get("role").and_then(Value::as_str) == Some("assistant")
                && payload
                    .get("content")
                    .and_then(Value::as_array)
                    .is_some_and(|content| {
                        content.iter().all(|item| {
                            item.get("type")
                                .and_then(Value::as_str)
                                .is_some_and(|kind| matches!(kind, "output_text" | "refusal"))
                        })
                    })
        }
        "function_call" | "custom_tool_call" => payload
            .get("call_id")
            .and_then(Value::as_str)
            .is_some_and(|call_id| !call_id.is_empty()),
        _ => false,
    };
    if !migratable {
        return Ok(ContinuationItemRewrite::Unsupported);
    }
    payload.remove("id");
    Ok(ContinuationItemRewrite::StripItemId)
}

fn find_latest_compacted_line(
    path: &Path,
    cancellation: &CancellationFlag,
) -> Result<Option<u64>, InteropError> {
    let source = File::open(path)
        .map_err(|_| InteropError::new("history_scan_failed", "rollout file cannot be opened"))?;
    let mut reader = BufReader::with_capacity(1024 * 1024, source);
    let mut line = Vec::new();
    let mut line_number = 0u64;
    let mut latest = None;
    loop {
        cancellation.check()?;
        line.clear();
        let bytes_read = reader
            .read_until(b'\n', &mut line)
            .map_err(|_| InteropError::new("history_scan_failed", "rollout line cannot be read"))?;
        if bytes_read == 0 {
            break;
        }
        if line.len() > MAX_JSONL_LINE_BYTES {
            return Err(InteropError::new(
                "history_record_too_large",
                "rollout record exceeds 64 MiB",
            ));
        }
        line_number += 1;
        let content = trim_line_ending(&line);
        let is_valid_compaction =
            serde_json::from_slice::<Value>(content)
                .ok()
                .is_some_and(|value| {
                    value.get("type").and_then(Value::as_str) == Some("compacted")
                        && value
                            .get("payload")
                            .and_then(|payload| payload.get("replacement_history"))
                            .is_some_and(Value::is_array)
                });
        if is_valid_compaction {
            latest = Some(line_number);
        }
    }
    Ok(latest)
}

fn append_original_line_ending(output: &mut Vec<u8>, original: &[u8]) {
    if original.ends_with(b"\r\n") {
        output.extend_from_slice(b"\r\n");
    } else if original.ends_with(b"\n") {
        output.push(b'\n');
    }
}

fn cleanup_error<T>(
    output: Option<BufWriter<File>>,
    temporary_path: &Path,
    error: InteropError,
) -> Result<T, InteropError> {
    drop(output);
    let _ = fs::remove_file(temporary_path);
    Err(error)
}

fn session_provider(value: &Value) -> Option<&str> {
    (value.get("type")?.as_str()? == "session_meta")
        .then_some(())
        .and_then(|_| value.get("payload"))?
        .get("model_provider")?
        .as_str()
}

fn locate_provider_value(
    line: &[u8],
    source: &str,
) -> Result<std::ops::Range<usize>, InteropError> {
    let needle = b"\"model_provider\"";
    let mut ranges = Vec::new();
    for key_start in memmem::find_iter(line, needle) {
        let mut cursor = key_start + needle.len();
        skip_ascii_whitespace(line, &mut cursor);
        if line.get(cursor) != Some(&b':') {
            continue;
        }
        cursor += 1;
        skip_ascii_whitespace(line, &mut cursor);
        if line.get(cursor) != Some(&b'\"') {
            continue;
        }
        cursor += 1;
        let end = cursor.saturating_add(source.len());
        if line.get(cursor..end) == Some(source.as_bytes()) && line.get(end) == Some(&b'\"') {
            ranges.push(cursor..end);
        }
    }
    if ranges.len() != 1 {
        return Err(InteropError::new(
            "provider_field_not_rewritable",
            "eligible session metadata does not contain one unambiguous provider field",
        ));
    }
    Ok(ranges.remove(0))
}

fn skip_ascii_whitespace(line: &[u8], cursor: &mut usize) {
    while line.get(*cursor).is_some_and(u8::is_ascii_whitespace) {
        *cursor += 1;
    }
}

fn add_prompts(value: &Value, hasher: &mut Sha256, count: &mut u64) {
    for prompt in user_prompt_texts(value) {
        let bytes = prompt.as_bytes();
        hasher.update((bytes.len() as u64).to_le_bytes());
        hasher.update(bytes);
        *count += 1;
    }
}

fn user_prompt_texts(value: &Value) -> Vec<String> {
    let item_type = value
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let payload = value.get("payload").unwrap_or(&Value::Null);
    if item_type == "response_item" && payload.get("role").and_then(Value::as_str) == Some("user") {
        return content_texts(payload.get("content"));
    }
    if item_type == "user_message" {
        return message_texts(payload);
    }
    if item_type == "event_msg"
        && payload.get("type").and_then(Value::as_str) == Some("user_message")
    {
        return message_texts(payload);
    }
    Vec::new()
}

fn content_texts(content: Option<&Value>) -> Vec<String> {
    content
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| {
            item.get("text")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned)
        })
        .collect()
}

fn message_texts(payload: &Value) -> Vec<String> {
    if let Some(message) = payload.get("message").and_then(Value::as_str) {
        return vec![message.to_string()];
    }
    if let Some(text) = payload.get("text").and_then(Value::as_str) {
        return vec![text.to_string()];
    }
    content_texts(payload.get("content"))
}

fn trim_line_ending(line: &[u8]) -> &[u8] {
    line.strip_suffix(b"\n")
        .unwrap_or(line)
        .strip_suffix(b"\r")
        .unwrap_or_else(|| line.strip_suffix(b"\n").unwrap_or(line))
}

fn temporary_path(path: &Path, operation_id: &str) -> PathBuf {
    let safe_operation: String = operation_id
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_'))
        .take(64)
        .collect();
    path.with_extension(format!("jsonl.provider-interop-{safe_operation}.tmp"))
}

fn path_sort_key(path: &Path) -> String {
    path.to_string_lossy()
        .replace('\\', "/")
        .to_ascii_lowercase()
}

fn hex_digest(digest: impl AsRef<[u8]>) -> String {
    digest
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[derive(Clone, Copy, Eq, PartialEq)]
struct FileVersion {
    size_bytes: u64,
    modified_ns: u128,
}

impl FileVersion {
    fn read(path: &Path) -> Result<Self, InteropError> {
        let metadata = fs::metadata(path).map_err(|_| {
            InteropError::new("history_scan_failed", "rollout metadata cannot be read")
        })?;
        let modified_ns = metadata
            .modified()
            .ok()
            .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
            .map(|value| value.as_nanos())
            .unwrap_or(0);
        Ok(Self {
            size_bytes: metadata.len(),
            modified_ns,
        })
    }
}
