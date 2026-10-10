use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::{
    adapters::cc_switch::{current_provider_snapshots, AdapterRepairPlan},
    sqlite::{
        backup_database, migrate_provider, quick_check, read_common_config, update_common_config,
    },
    HistoryAudit, InteropError,
};

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FaultPoint {
    AfterBackup,
    AfterExternalConfig,
    AfterJsonl,
    AfterSqlite,
}

#[derive(Clone, Debug)]
pub struct TransactionPlan {
    pub operation_id: String,
    pub codex_home: PathBuf,
    pub source_bucket: String,
    pub target_bucket: String,
    pub history: HistoryAudit,
    pub apply_history: bool,
    pub apply_continuation: bool,
    pub adapter: Option<AdapterRepairPlan>,
}

impl TransactionPlan {
    pub fn history(
        operation_id: impl Into<String>,
        codex_home: impl AsRef<Path>,
        source_bucket: impl Into<String>,
        target_bucket: impl Into<String>,
        history: HistoryAudit,
    ) -> Self {
        Self {
            operation_id: operation_id.into(),
            codex_home: codex_home.as_ref().to_path_buf(),
            source_bucket: source_bucket.into(),
            target_bucket: target_bucket.into(),
            history,
            apply_history: true,
            apply_continuation: false,
            adapter: None,
        }
    }

    pub fn with_adapter(mut self, adapter: AdapterRepairPlan) -> Self {
        self.adapter = Some(adapter);
        self
    }

    pub fn with_history_enabled(mut self, enabled: bool) -> Self {
        self.apply_history = enabled;
        self
    }

    pub fn with_continuation_enabled(mut self, enabled: bool) -> Self {
        self.apply_continuation = enabled;
        self
    }

    fn apply_jsonl(&self) -> bool {
        self.apply_history || self.apply_continuation
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TransactionResult {
    pub status: String,
    pub operation_id: String,
    pub backup_directory: String,
    pub manifest_path: String,
    pub journal_path: String,
    pub jsonl_files_changed: u64,
    pub jsonl_replacements: u64,
    pub sqlite_rows_changed: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RollbackResult {
    pub status: String,
    pub operation_id: String,
    pub restored_file_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct BackupFile {
    source_path: String,
    backup_path: String,
    source_sha256: String,
    backup_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct TransactionManifest {
    schema_version: u32,
    operation_id: String,
    codex_home: String,
    source_bucket: String,
    target_bucket: String,
    state_database_path: String,
    state_database_backup_path: String,
    files: Vec<BackupFile>,
    sqlite_quick_check: String,
    committed: bool,
    #[serde(default)]
    adapter_database_path: Option<String>,
    #[serde(default)]
    adapter_database_backup_path: Option<String>,
    #[serde(default)]
    adapter_settings_path: Option<String>,
    #[serde(default)]
    adapter_settings_backup_path: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct JournalEntry {
    sequence: u64,
    stage: String,
}

pub fn apply_transaction(
    plan: &TransactionPlan,
    backup_root: &Path,
    fault: Option<FaultPoint>,
) -> Result<TransactionResult, InteropError> {
    validate_plan(plan)?;
    let backup_directory = create_backup_directory(backup_root, &plan.operation_id)?;
    let journal_path = backup_directory.join("journal.jsonl");
    let manifest_path = backup_directory.join("manifest.json");
    let database_path = plan.codex_home.join("state_5.sqlite");
    let database_backup_path = backup_directory.join("state_5.sqlite");
    let mut journal = Journal::new(&journal_path)?;
    let mut backup_files = Vec::new();
    let mut adapter_database_backup_path = None;
    let mut adapter_settings_backup_path = None;
    let mut mutation_started = false;
    let operation = (|| -> Result<TransactionResult, InteropError> {
        for (index, file) in plan
            .history
            .files
            .iter()
            .filter(|file| plan.apply_jsonl() && file.replacement_count > 0)
            .enumerate()
        {
            let source = PathBuf::from(&file.path);
            validate_file_binding(&source, file.size_bytes, &file.source_sha256)?;
            let backup = backup_directory.join(format!("rollout-{index}.jsonl"));
            copy_verified(&source, &backup, &file.source_sha256)?;
            backup_files.push(BackupFile {
                source_path: file.path.clone(),
                backup_path: backup.to_string_lossy().into_owned(),
                source_sha256: file.source_sha256.clone(),
                backup_sha256: sha256_file(&backup)?,
            });
        }
        backup_database(&database_path, &database_backup_path)?;
        if let Some(adapter) = &plan.adapter {
            let adapter_database_backup = backup_directory.join("cc-switch.db");
            backup_database(&adapter.database_path, &adapter_database_backup)?;
            adapter_database_backup_path =
                Some(adapter_database_backup.to_string_lossy().into_owned());
            let settings_backup = backup_directory.join("cc-switch-settings.json");
            copy_file_verified(&adapter.settings_path, &settings_backup)?;
            adapter_settings_backup_path = Some(settings_backup.to_string_lossy().into_owned());
        }
        let mut manifest = TransactionManifest {
            schema_version: 1,
            operation_id: plan.operation_id.clone(),
            codex_home: plan.codex_home.to_string_lossy().into_owned(),
            source_bucket: plan.source_bucket.clone(),
            target_bucket: plan.target_bucket.clone(),
            state_database_path: database_path.to_string_lossy().into_owned(),
            state_database_backup_path: database_backup_path.to_string_lossy().into_owned(),
            files: backup_files.clone(),
            sqlite_quick_check: "ok".to_string(),
            committed: false,
            adapter_database_path: plan
                .adapter
                .as_ref()
                .map(|adapter| adapter.database_path.to_string_lossy().into_owned()),
            adapter_database_backup_path: adapter_database_backup_path.clone(),
            adapter_settings_path: plan
                .adapter
                .as_ref()
                .map(|adapter| adapter.settings_path.to_string_lossy().into_owned()),
            adapter_settings_backup_path: adapter_settings_backup_path.clone(),
        };
        write_json_fsync(&manifest_path, &manifest)?;
        journal.record("backed_up")?;
        mutation_started = true;
        inject_fault(fault, FaultPoint::AfterBackup)?;
        if let Some(adapter) = &plan.adapter {
            if let Some(common) = &adapter.common_config {
                if sha256_bytes(read_common_config(&adapter.database_path)?.as_bytes())
                    != common.source_common_sha256
                {
                    return Err(InteropError::new(
                        "state_changed_since_preview",
                        "CC Switch common configuration changed after preview",
                    ));
                }
                update_common_config(&adapter.database_path, &common.rendered_common)?;
            }
            if !adapter.setting_changes.is_empty() {
                validate_file_sha256(&adapter.settings_path, &adapter.settings_source_sha256)?;
                atomic_replace_bytes(&adapter.settings_path, &adapter.settings_output)?;
                validate_file_sha256(&adapter.settings_path, &adapter.settings_output_sha256)?;
            }
            verify_adapter_snapshots(adapter)?;
        }
        journal.record("external_config_applied")?;
        inject_fault(fault, FaultPoint::AfterExternalConfig)?;
        for file in plan
            .history
            .files
            .iter()
            .filter(|file| plan.apply_jsonl() && file.replacement_count > 0)
        {
            let source = PathBuf::from(&file.path);
            validate_file_binding(&source, file.size_bytes, &file.source_sha256)?;
            let temporary = file.temporary_path.as_deref().ok_or_else(|| {
                InteropError::new(
                    "history_temporary_missing",
                    "history transaction plan has no verified temporary file",
                )
            })?;
            let temporary = PathBuf::from(temporary);
            let expected = file.output_sha256.as_deref().ok_or_else(|| {
                InteropError::new(
                    "history_temporary_missing",
                    "history transaction plan has no output hash",
                )
            })?;
            copy_verified(&temporary, &source, expected)?;
        }
        journal.record("jsonl_applied")?;
        inject_fault(fault, FaultPoint::AfterJsonl)?;

        let sqlite_rows_changed = if plan.apply_history {
            migrate_provider(&database_path, &plan.source_bucket, &plan.target_bucket)?
        } else {
            0
        };
        journal.record("sqlite_applied")?;
        inject_fault(fault, FaultPoint::AfterSqlite)?;

        verify_applied(plan, &database_path)?;
        journal.record("verified")?;
        manifest.committed = true;
        write_json_fsync(&manifest_path, &manifest)?;
        journal.record("committed")?;
        Ok(TransactionResult {
            status: "committed".to_string(),
            operation_id: plan.operation_id.clone(),
            backup_directory: backup_directory.to_string_lossy().into_owned(),
            manifest_path: manifest_path.to_string_lossy().into_owned(),
            journal_path: journal_path.to_string_lossy().into_owned(),
            jsonl_files_changed: if plan.apply_jsonl() {
                plan.history.changed_file_count
            } else {
                0
            },
            jsonl_replacements: if plan.apply_jsonl() {
                plan.history.replacement_count
            } else {
                0
            },
            sqlite_rows_changed,
        })
    })();

    match operation {
        Ok(result) => Ok(result),
        Err(error) if mutation_started => {
            journal.record("automatic_rollback_started")?;
            let rollback = restore_preimages(&manifest_path);
            match rollback {
                Ok(_) => {
                    journal.record("automatic_rollback_complete")?;
                    Err(InteropError::new(
                        "failed_rolled_back",
                        format!("transaction failed and was rolled back: {}", error.code),
                    ))
                }
                Err(rollback_error) => Err(InteropError::new(
                    "failed_manual_recovery_required",
                    format!(
                        "transaction failed with {}; rollback failed with {}",
                        error.code, rollback_error.code
                    ),
                )),
            }
        }
        Err(error) => Err(error),
    }
}

pub fn rollback_transaction(manifest_path: &Path) -> Result<RollbackResult, InteropError> {
    restore_preimages(manifest_path)
}

fn restore_preimages(manifest_path: &Path) -> Result<RollbackResult, InteropError> {
    let manifest: TransactionManifest =
        serde_json::from_slice(&fs::read(manifest_path).map_err(|_| {
            InteropError::new(
                "rollback_manifest_missing",
                "transaction manifest cannot be read",
            )
        })?)
        .map_err(|_| {
            InteropError::new(
                "rollback_manifest_invalid",
                "transaction manifest is invalid",
            )
        })?;
    let mut restored = 0u64;
    for file in &manifest.files {
        let backup = PathBuf::from(&file.backup_path);
        let source = PathBuf::from(&file.source_path);
        if sha256_file(&backup)? != file.backup_sha256 {
            return Err(InteropError::new(
                "rollback_backup_mismatch",
                "rollout backup hash does not match the manifest",
            ));
        }
        copy_verified(&backup, &source, &file.backup_sha256)?;
        restored += 1;
    }
    let database_backup = PathBuf::from(&manifest.state_database_backup_path);
    let database = PathBuf::from(&manifest.state_database_path);
    quick_check(&database_backup)?;
    fs::copy(&database_backup, &database).map_err(|_| {
        InteropError::new(
            "rollback_failed",
            "state database backup cannot be restored",
        )
    })?;
    sync_file(&database)?;
    quick_check(&database)?;
    if let (Some(adapter_database), Some(adapter_backup)) = (
        manifest.adapter_database_path.as_deref(),
        manifest.adapter_database_backup_path.as_deref(),
    ) {
        let adapter_database = Path::new(adapter_database);
        let adapter_backup = Path::new(adapter_backup);
        quick_check(adapter_backup)?;
        fs::copy(adapter_backup, adapter_database).map_err(|_| {
            InteropError::new(
                "rollback_failed",
                "CC Switch database backup cannot be restored",
            )
        })?;
        sync_file(adapter_database)?;
        quick_check(adapter_database)?;
    }
    if let (Some(adapter_settings), Some(settings_backup)) = (
        manifest.adapter_settings_path.as_deref(),
        manifest.adapter_settings_backup_path.as_deref(),
    ) {
        copy_file_verified(Path::new(settings_backup), Path::new(adapter_settings))?;
    }
    Ok(RollbackResult {
        status: "rolled_back".to_string(),
        operation_id: manifest.operation_id,
        restored_file_count: restored,
    })
}

fn validate_plan(plan: &TransactionPlan) -> Result<(), InteropError> {
    if plan.operation_id.trim().is_empty() || !plan.codex_home.is_dir() {
        return Err(InteropError::new(
            "invalid_transaction_plan",
            "transaction plan has an invalid operation or Codex Home",
        ));
    }
    if !plan.apply_jsonl() && plan.adapter.is_none() {
        return Err(InteropError::new(
            "empty_repair_scope",
            "provider interoperability repair scope is empty",
        ));
    }
    if plan.source_bucket.len() != plan.target_bucket.len() {
        return Err(InteropError::new(
            "unsupported_provider_substitution",
            "transaction provider identities must have equal length",
        ));
    }
    if plan.history.parse_error_count > 0 {
        return Err(InteropError::new(
            "jsonl_parse_error",
            "history transaction cannot run with rollout parse errors",
        ));
    }
    if plan.apply_continuation && plan.history.unsupported_foreign_item_count > 0 {
        return Err(InteropError::new(
            "unsupported_foreign_response_item",
            "continuation repair found foreign response item types that cannot be safely migrated",
        ));
    }
    if let Some(adapter) = &plan.adapter {
        if !adapter.database_path.is_file() || !adapter.settings_path.is_file() {
            return Err(InteropError::new(
                "adapter_not_found",
                "CC Switch adapter files are missing",
            ));
        }
    }
    Ok(())
}

fn verify_adapter_snapshots(adapter: &AdapterRepairPlan) -> Result<(), InteropError> {
    let current = current_provider_snapshots(&adapter.database_path)?;
    if current != adapter.provider_snapshots {
        return Err(InteropError::new(
            "provider_snapshot_changed",
            "provider-specific settings changed during the transaction",
        ));
    }
    Ok(())
}

fn validate_file_sha256(path: &Path, expected: &str) -> Result<(), InteropError> {
    if sha256_file(path)? != expected {
        return Err(InteropError::new(
            "state_changed_since_preview",
            "adapter settings changed after preview",
        ));
    }
    Ok(())
}

fn sha256_bytes(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn copy_file_verified(source: &Path, target: &Path) -> Result<(), InteropError> {
    fs::copy(source, target)
        .map_err(|_| InteropError::new("file_copy_failed", "transaction file copy failed"))?;
    sync_file(target)
}

fn atomic_replace_bytes(path: &Path, bytes: &[u8]) -> Result<(), InteropError> {
    let temporary = path.with_extension("codex-home-manager.tmp");
    {
        let mut file = File::create(&temporary).map_err(|_| {
            InteropError::new(
                "adapter_write_failed",
                "adapter settings temporary file cannot be created",
            )
        })?;
        file.write_all(bytes).map_err(|_| {
            InteropError::new("adapter_write_failed", "adapter settings cannot be written")
        })?;
        file.flush().map_err(|_| {
            InteropError::new("adapter_write_failed", "adapter settings cannot be flushed")
        })?;
        file.sync_all().map_err(|_| {
            InteropError::new(
                "adapter_write_failed",
                "adapter settings cannot be synchronized",
            )
        })?;
    }
    if fs::rename(&temporary, path).is_ok() {
        return Ok(());
    }
    fs::remove_file(path).map_err(|_| {
        let _ = fs::remove_file(&temporary);
        InteropError::new(
            "adapter_write_failed",
            "adapter settings target cannot be replaced",
        )
    })?;
    fs::rename(&temporary, path).map_err(|_| {
        let _ = fs::remove_file(&temporary);
        InteropError::new(
            "adapter_write_failed",
            "adapter settings cannot be replaced",
        )
    })
}

fn create_backup_directory(root: &Path, operation_id: &str) -> Result<PathBuf, InteropError> {
    fs::create_dir_all(root)
        .map_err(|_| InteropError::new("backup_create_failed", "backup root cannot be created"))?;
    let safe: String = operation_id
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_'))
        .take(80)
        .collect();
    let directory = root.join(format!("provider-interoperability-{safe}"));
    fs::create_dir(&directory).map_err(|_| {
        InteropError::new(
            "backup_create_failed",
            "transaction backup already exists or cannot be created",
        )
    })?;
    Ok(directory)
}

fn validate_file_binding(path: &Path, size: u64, sha256: &str) -> Result<(), InteropError> {
    let metadata = fs::metadata(path).map_err(|_| {
        InteropError::new("state_changed_since_preview", "planned rollout is missing")
    })?;
    if metadata.len() != size || sha256_file(path)? != sha256 {
        return Err(InteropError::new(
            "state_changed_since_preview",
            "planned rollout changed after audit",
        ));
    }
    Ok(())
}

fn copy_verified(source: &Path, target: &Path, expected_sha256: &str) -> Result<(), InteropError> {
    fs::copy(source, target)
        .map_err(|_| InteropError::new("file_copy_failed", "transaction file copy failed"))?;
    sync_file(target)?;
    if sha256_file(target)? != expected_sha256 {
        return Err(InteropError::new(
            "file_copy_mismatch",
            "transaction file copy hash mismatch",
        ));
    }
    Ok(())
}

fn verify_applied(plan: &TransactionPlan, database: &Path) -> Result<(), InteropError> {
    quick_check(database)?;
    for file in plan
        .history
        .files
        .iter()
        .filter(|file| plan.apply_jsonl() && file.replacement_count > 0)
    {
        let expected = file.output_sha256.as_deref().ok_or_else(|| {
            InteropError::new(
                "history_preservation_failed",
                "planned output hash is missing",
            )
        })?;
        if sha256_file(Path::new(&file.path))? != expected {
            return Err(InteropError::new(
                "history_preservation_failed",
                "applied rollout hash differs from the verified output",
            ));
        }
    }
    if let Some(adapter) = &plan.adapter {
        if let Some(common) = &adapter.common_config {
            if sha256_bytes(read_common_config(&adapter.database_path)?.as_bytes())
                != common.rendered_common_sha256
            {
                return Err(InteropError::new(
                    "common_config_verification_failed",
                    "CC Switch common configuration differs from the planned output",
                ));
            }
        }
        if !adapter.setting_changes.is_empty() {
            validate_file_sha256(&adapter.settings_path, &adapter.settings_output_sha256)?;
        }
        verify_adapter_snapshots(adapter)?;
        quick_check(&adapter.database_path)?;
    }
    Ok(())
}

fn inject_fault(configured: Option<FaultPoint>, current: FaultPoint) -> Result<(), InteropError> {
    if configured == Some(current) {
        return Err(InteropError::new(
            "injected_transaction_failure",
            "transaction stopped by test fault injection",
        ));
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String, InteropError> {
    let mut file = File::open(path)
        .map_err(|_| InteropError::new("file_hash_failed", "transaction file cannot be opened"))?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut file, &mut hasher)
        .map_err(|_| InteropError::new("file_hash_failed", "transaction file cannot be hashed"))?;
    Ok(format!("{:x}", hasher.finalize()))
}

fn sync_file(path: &Path) -> Result<(), InteropError> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .and_then(|file| file.sync_all())
        .map_err(|_| {
            InteropError::new(
                "file_sync_failed",
                "transaction file cannot be synchronized",
            )
        })
}

fn write_json_fsync(path: &Path, value: &impl Serialize) -> Result<(), InteropError> {
    let bytes = serde_json::to_vec_pretty(value).map_err(|_| {
        InteropError::new(
            "transaction_manifest_failed",
            "transaction metadata cannot serialize",
        )
    })?;
    let mut file = File::create(path).map_err(|_| {
        InteropError::new(
            "transaction_manifest_failed",
            "transaction metadata cannot be created",
        )
    })?;
    file.write_all(&bytes).map_err(|_| {
        InteropError::new(
            "transaction_manifest_failed",
            "transaction metadata cannot be written",
        )
    })?;
    file.flush().map_err(|_| {
        InteropError::new(
            "transaction_manifest_failed",
            "transaction metadata cannot be flushed",
        )
    })?;
    file.sync_all().map_err(|_| {
        InteropError::new(
            "transaction_manifest_failed",
            "transaction metadata cannot be synchronized",
        )
    })
}

struct Journal {
    file: File,
    sequence: u64,
}

impl Journal {
    fn new(path: &Path) -> Result<Self, InteropError> {
        let file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(path)
            .map_err(|_| {
                InteropError::new(
                    "transaction_journal_failed",
                    "transaction journal cannot be created",
                )
            })?;
        Ok(Self { file, sequence: 0 })
    }

    fn record(&mut self, stage: &str) -> Result<(), InteropError> {
        self.sequence += 1;
        let entry = JournalEntry {
            sequence: self.sequence,
            stage: stage.to_string(),
        };
        serde_json::to_writer(&mut self.file, &entry).map_err(|_| {
            InteropError::new(
                "transaction_journal_failed",
                "transaction journal cannot be written",
            )
        })?;
        self.file.write_all(b"\n").map_err(|_| {
            InteropError::new(
                "transaction_journal_failed",
                "transaction journal cannot be written",
            )
        })?;
        self.file.flush().map_err(|_| {
            InteropError::new(
                "transaction_journal_failed",
                "transaction journal cannot be flushed",
            )
        })?;
        self.file.sync_all().map_err(|_| {
            InteropError::new(
                "transaction_journal_failed",
                "transaction journal cannot be synchronized",
            )
        })
    }
}
