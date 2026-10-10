use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::{AdapterSupport, ExternalAdapterSummary, InteroperabilitySettings};
use crate::provider_interop::{plan_common_merge, ConfigMergePlan, InteropError, ProviderSnapshot};

const SUPPORTED_USER_VERSIONS: &[i64] = &[16, 18];
const REQUIRED_PROVIDER_COLUMNS: &[&str] = &[
    "id",
    "app_type",
    "name",
    "settings_config",
    "meta",
    "is_current",
    "provider_type",
];
const REQUIRED_SETTINGS_COLUMNS: &[&str] = &["key", "value"];

#[derive(Clone, Debug)]
pub struct AdapterRepairPlan {
    pub database_path: PathBuf,
    pub settings_path: PathBuf,
    pub common_config: Option<ConfigMergePlan>,
    pub settings_source_sha256: String,
    pub settings_output_sha256: String,
    pub settings_output: Vec<u8>,
    pub setting_changes: Vec<String>,
    pub provider_snapshots: Vec<ProviderSnapshot>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AdapterRepairSummary {
    pub common_config_changed: bool,
    pub common_config_additions: u64,
    pub common_config_conflicts: Vec<String>,
    pub common_config_exclusions: Vec<String>,
    pub setting_changes: Vec<String>,
    pub provider_snapshot_count: u64,
    pub provider_snapshots_preserved: bool,
    pub common_config_source_sha256: Option<String>,
    pub common_config_output_sha256: Option<String>,
    pub settings_source_sha256: String,
    pub settings_output_sha256: String,
    pub provider_snapshots_sha256: String,
}

impl AdapterRepairPlan {
    pub fn summary(&self) -> AdapterRepairSummary {
        let common_config_changed = self
            .common_config
            .as_ref()
            .is_some_and(|plan| plan.source_common_sha256 != plan.rendered_common_sha256);
        let common_config_additions = self.common_config.as_ref().map_or(0, |plan| {
            plan.decisions
                .iter()
                .filter(|decision| decision.class == crate::provider_interop::ConfigClass::Shared)
                .count() as u64
        });
        AdapterRepairSummary {
            common_config_changed,
            common_config_additions,
            common_config_conflicts: self
                .common_config
                .as_ref()
                .map(|plan| plan.conflicts.clone())
                .unwrap_or_default(),
            common_config_exclusions: self
                .common_config
                .as_ref()
                .map(|plan| plan.excluded_paths.clone())
                .unwrap_or_default(),
            setting_changes: self.setting_changes.clone(),
            provider_snapshot_count: self.provider_snapshots.len() as u64,
            provider_snapshots_preserved: true,
            common_config_source_sha256: self
                .common_config
                .as_ref()
                .map(|plan| plan.source_common_sha256.clone()),
            common_config_output_sha256: self
                .common_config
                .as_ref()
                .map(|plan| plan.rendered_common_sha256.clone()),
            settings_source_sha256: self.settings_source_sha256.clone(),
            settings_output_sha256: self.settings_output_sha256.clone(),
            provider_snapshots_sha256: provider_snapshots_digest(&self.provider_snapshots),
        }
    }
}

pub fn inspect(
    adapter_root: Option<&Path>,
    selected_codex_home: &Path,
) -> Result<ExternalAdapterSummary, InteropError> {
    let Some(root) = adapter_root else {
        return Ok(ExternalAdapterSummary::not_found());
    };
    if !root.is_dir() {
        return Ok(ExternalAdapterSummary::not_found());
    }
    let root = root.canonicalize().map_err(|_| {
        InteropError::new(
            "adapter_not_found",
            "CC Switch state root cannot be resolved",
        )
    })?;
    let database_path = root.join("cc-switch.db");
    let settings_path = root.join("settings.json");
    if !database_path.is_file() || !settings_path.is_file() {
        return Ok(ExternalAdapterSummary::not_found());
    }
    let settings: Value = serde_json::from_slice(&fs::read(&settings_path).map_err(|_| {
        InteropError::new(
            "adapter_settings_unreadable",
            "CC Switch settings cannot be read",
        )
    })?)
    .map_err(|_| {
        InteropError::new(
            "adapter_settings_invalid",
            "CC Switch settings are not valid JSON",
        )
    })?;
    let connection = Connection::open_with_flags(
        &database_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| {
        InteropError::new(
            "adapter_database_unreadable",
            "CC Switch database cannot be opened",
        )
    })?;
    let quick_check: String = connection
        .query_row("PRAGMA quick_check", [], |row| row.get(0))
        .map_err(|_| {
            InteropError::new(
                "adapter_database_unreadable",
                "CC Switch quick check failed",
            )
        })?;
    let user_version: i64 = connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .map_err(|_| {
            InteropError::new(
                "adapter_database_unreadable",
                "CC Switch user version cannot be read",
            )
        })?;
    let provider_columns = table_columns(&connection, "providers")?;
    let settings_columns = table_columns(&connection, "settings")?;
    let schema_fingerprint = schema_fingerprint(user_version, &provider_columns, &settings_columns);
    let schema_supported = required_columns_present(&provider_columns, REQUIRED_PROVIDER_COLUMNS)
        && required_columns_present(&settings_columns, REQUIRED_SETTINGS_COLUMNS);
    let bound_codex_home = settings
        .get("codexConfigDir")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let home_matches = bound_codex_home
        .as_deref()
        .is_some_and(|bound| paths_equal(Path::new(bound), selected_codex_home));
    let (current_provider_name, current_provider_id_hash) =
        current_provider(&connection, &settings)?;
    let (support, reason) = if quick_check != "ok" {
        (
            AdapterSupport::ReadOnly,
            Some("sqlite_integrity_failed".to_string()),
        )
    } else if !SUPPORTED_USER_VERSIONS.contains(&user_version) {
        (
            AdapterSupport::ReadOnly,
            Some("unsupported_user_version".to_string()),
        )
    } else if !schema_supported {
        (
            AdapterSupport::ReadOnly,
            Some("unsupported_schema_fingerprint".to_string()),
        )
    } else if !home_matches {
        (
            AdapterSupport::ReadOnly,
            Some("codex_home_mismatch".to_string()),
        )
    } else {
        (AdapterSupport::ReadWrite, None)
    };
    Ok(ExternalAdapterSummary {
        kind: "cc_switch".to_string(),
        support,
        root: Some(root.to_string_lossy().into_owned()),
        database_path: Some(database_path.to_string_lossy().into_owned()),
        user_version: Some(user_version),
        schema_fingerprint: Some(schema_fingerprint),
        database_quick_check: Some(quick_check),
        bound_codex_home,
        current_provider_name,
        current_provider_id_hash,
        settings: InteroperabilitySettings {
            preserve_official_auth: setting_bool(&settings, "preserveCodexOfficialAuthOnSwitch"),
            unify_session_history: setting_bool(&settings, "unifyCodexSessionHistory"),
            migrate_existing_history: setting_bool(&settings, "unifyCodexMigrateExisting"),
        },
        reason,
    })
}

pub fn plan_repair(
    adapter_root: &Path,
    selected_codex_home: &Path,
    share_common_config: bool,
    enable_interoperability_settings: bool,
) -> Result<AdapterRepairPlan, InteropError> {
    let inspection = inspect(Some(adapter_root), selected_codex_home)?;
    if inspection.support != AdapterSupport::ReadWrite {
        return Err(InteropError::new(
            inspection
                .reason
                .as_deref()
                .unwrap_or("unsupported_adapter_schema"),
            "CC Switch adapter is not writable for the selected Codex Home",
        ));
    }
    let database_path = inspection
        .database_path
        .as_deref()
        .map(PathBuf::from)
        .ok_or_else(|| {
            InteropError::new(
                "adapter_database_unreadable",
                "CC Switch database path is unavailable",
            )
        })?;
    let root = inspection
        .root
        .as_deref()
        .map(PathBuf::from)
        .ok_or_else(|| {
            InteropError::new("adapter_not_found", "CC Switch state root is unavailable")
        })?;
    let settings_path = root.join("settings.json");
    let settings_source = fs::read(&settings_path).map_err(|_| {
        InteropError::new(
            "adapter_settings_unreadable",
            "CC Switch settings cannot be read",
        )
    })?;
    let mut settings: Value = serde_json::from_slice(&settings_source).map_err(|_| {
        InteropError::new(
            "adapter_settings_invalid",
            "CC Switch settings are not valid JSON",
        )
    })?;
    let object = settings.as_object_mut().ok_or_else(|| {
        InteropError::new(
            "adapter_settings_invalid",
            "CC Switch settings root must be an object",
        )
    })?;
    let mut setting_changes = Vec::new();
    if enable_interoperability_settings {
        for key in [
            "preserveCodexOfficialAuthOnSwitch",
            "unifyCodexSessionHistory",
            "unifyCodexMigrateExisting",
        ] {
            if object.get(key).and_then(Value::as_bool) != Some(true) {
                object.insert(key.to_string(), Value::Bool(true));
                setting_changes.push(key.to_string());
            }
        }
    }
    let settings_output = if setting_changes.is_empty() {
        settings_source.clone()
    } else {
        serialize_settings(&settings)?
    };

    let connection = Connection::open_with_flags(
        &database_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| {
        InteropError::new(
            "adapter_database_unreadable",
            "CC Switch database cannot be opened",
        )
    })?;
    let provider_snapshots = provider_snapshots(&connection)?;
    let common_config = if share_common_config {
        let common: String = connection
            .query_row(
                "SELECT value FROM settings WHERE key = 'common_config_codex'",
                [],
                |row| row.get(0),
            )
            .unwrap_or_default();
        let live = fs::read_to_string(selected_codex_home.join("config.toml")).map_err(|_| {
            InteropError::new("codex_config_not_found", "Codex config.toml cannot be read")
        })?;
        Some(plan_common_merge(&common, &live, &provider_snapshots)?)
    } else {
        None
    };
    Ok(AdapterRepairPlan {
        database_path,
        settings_path,
        common_config,
        settings_source_sha256: sha256_bytes(&settings_source),
        settings_output_sha256: sha256_bytes(&settings_output),
        settings_output,
        setting_changes,
        provider_snapshots,
    })
}

fn serialize_settings(settings: &Value) -> Result<Vec<u8>, InteropError> {
    let mut bytes = serde_json::to_vec_pretty(settings).map_err(|_| {
        InteropError::new(
            "adapter_settings_invalid",
            "CC Switch settings cannot be serialized",
        )
    })?;
    bytes.push(b'\n');
    Ok(bytes)
}

fn provider_snapshots(connection: &Connection) -> Result<Vec<ProviderSnapshot>, InteropError> {
    let mut statement = connection
        .prepare("SELECT id, settings_config FROM providers WHERE app_type = 'codex' ORDER BY id")
        .map_err(|_| {
            InteropError::new(
                "adapter_schema_invalid",
                "CC Switch providers cannot be queried",
            )
        })?;
    let rows = statement
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|_| {
            InteropError::new(
                "adapter_database_unreadable",
                "CC Switch providers cannot be read",
            )
        })?;
    let mut snapshots = Vec::new();
    for row in rows {
        let (id, config) = row.map_err(|_| {
            InteropError::new(
                "adapter_database_unreadable",
                "CC Switch provider row is invalid",
            )
        })?;
        snapshots.push(ProviderSnapshot {
            id_hash: sha256_bytes(id.as_bytes())[..16].to_string(),
            config_sha256: sha256_bytes(config.as_bytes()),
        });
    }
    Ok(snapshots)
}

pub fn current_provider_snapshots(
    database_path: &Path,
) -> Result<Vec<ProviderSnapshot>, InteropError> {
    let connection = Connection::open_with_flags(
        database_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| {
        InteropError::new(
            "adapter_database_unreadable",
            "CC Switch database cannot be opened",
        )
    })?;
    provider_snapshots(&connection)
}

pub fn sha256_bytes(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn provider_snapshots_digest(snapshots: &[ProviderSnapshot]) -> String {
    let mut hasher = Sha256::new();
    for snapshot in snapshots {
        hasher.update(snapshot.id_hash.as_bytes());
        hasher.update([0]);
        hasher.update(snapshot.config_sha256.as_bytes());
        hasher.update([0xff]);
    }
    format!("{:x}", hasher.finalize())
}

fn table_columns(connection: &Connection, table: &str) -> Result<BTreeSet<String>, InteropError> {
    let sql = match table {
        "providers" => "PRAGMA table_info(providers)",
        "settings" => "PRAGMA table_info(settings)",
        _ => {
            return Err(InteropError::new(
                "adapter_schema_invalid",
                "unsupported schema table",
            ))
        }
    };
    let mut statement = connection.prepare(sql).map_err(|_| {
        InteropError::new(
            "adapter_schema_invalid",
            "CC Switch table schema cannot be read",
        )
    })?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|_| {
            InteropError::new(
                "adapter_schema_invalid",
                "CC Switch column schema cannot be read",
            )
        })?;
    let mut columns = BTreeSet::new();
    for row in rows {
        columns.insert(row.map_err(|_| {
            InteropError::new("adapter_schema_invalid", "CC Switch column name is invalid")
        })?);
    }
    Ok(columns)
}

fn required_columns_present(columns: &BTreeSet<String>, required: &[&str]) -> bool {
    required.iter().all(|column| columns.contains(*column))
}

fn schema_fingerprint(
    user_version: i64,
    providers: &BTreeSet<String>,
    settings: &BTreeSet<String>,
) -> String {
    let mut hasher = Sha256::new();
    hasher.update(user_version.to_le_bytes());
    for (table, columns) in [("providers", providers), ("settings", settings)] {
        hasher.update(table.as_bytes());
        for column in columns {
            hasher.update([0]);
            hasher.update(column.as_bytes());
        }
    }
    format!("{:x}", hasher.finalize())
}

fn current_provider(
    connection: &Connection,
    settings: &Value,
) -> Result<(Option<String>, Option<String>), InteropError> {
    let Some(provider_id) = settings.get("currentProviderCodex").and_then(Value::as_str) else {
        return Ok((None, None));
    };
    let provider_name = connection
        .query_row(
            "SELECT name FROM providers WHERE id = ?1 AND app_type = 'codex' LIMIT 1",
            [provider_id],
            |row| row.get::<_, String>(0),
        )
        .ok();
    let mut hasher = Sha256::new();
    hasher.update(provider_id.as_bytes());
    let digest = format!("{:x}", hasher.finalize());
    Ok((provider_name, Some(digest[..16].to_string())))
}

fn setting_bool(settings: &Value, key: &str) -> Option<bool> {
    settings.get(key).and_then(Value::as_bool)
}

fn paths_equal(left: &Path, right: &Path) -> bool {
    let left = left.canonicalize().unwrap_or_else(|_| left.to_path_buf());
    let right = right.canonicalize().unwrap_or_else(|_| right.to_path_buf());
    if cfg!(windows) {
        left.to_string_lossy()
            .eq_ignore_ascii_case(&right.to_string_lossy())
    } else {
        left == right
    }
}
