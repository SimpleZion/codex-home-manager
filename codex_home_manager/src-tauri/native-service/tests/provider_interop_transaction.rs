use std::{fs, path::Path};

use codex_home_manager_service::provider_interop::{
    adapters::cc_switch, apply_transaction, audit_history, rollback_transaction, CancellationFlag,
    HistoryAuditRequest, TransactionPlan,
};
use rusqlite::Connection;
use serde_json::Value;

fn fixture() -> (tempfile::TempDir, tempfile::TempDir, std::path::PathBuf) {
    let home = tempfile::tempdir().unwrap();
    let backup = tempfile::tempdir().unwrap();
    let sessions = home.path().join("sessions");
    fs::create_dir_all(&sessions).unwrap();
    let rollout = sessions.join("rollout.jsonl");
    fs::write(
        &rollout,
        b"{\"type\":\"session_meta\",\"payload\":{\"id\":\"t\",\"model_provider\":\"openai\"}}\n",
    )
    .unwrap();
    let connection = Connection::open(home.path().join("state_5.sqlite")).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL);\n\
             INSERT INTO threads VALUES ('t', 'openai');",
        )
        .unwrap();
    (home, backup, rollout)
}

fn provider_count(database: &Path, provider: &str) -> i64 {
    Connection::open(database)
        .unwrap()
        .query_row(
            "SELECT COUNT(*) FROM threads WHERE model_provider = ?1",
            [provider],
            |row| row.get(0),
        )
        .unwrap()
}

#[test]
fn apply_and_explicit_rollback_restore_exact_history_and_database() {
    let (home, backup, rollout) = fixture();
    let original = fs::read(&rollout).unwrap();
    let audit = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", "transaction-1")
            .with_temporary_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();
    let plan = TransactionPlan::history("transaction-1", home.path(), "openai", "custom", audit);

    let result = apply_transaction(&plan, backup.path(), None).unwrap();
    assert_eq!(result.status, "committed");
    assert!(String::from_utf8(fs::read(&rollout).unwrap())
        .unwrap()
        .contains("\"custom\""));
    assert_eq!(
        provider_count(&home.path().join("state_5.sqlite"), "custom"),
        1
    );
    assert_eq!(result.sqlite_rows_changed, 1);

    let rollback = rollback_transaction(Path::new(&result.manifest_path)).unwrap();
    assert_eq!(rollback.status, "rolled_back");
    assert_eq!(fs::read(&rollout).unwrap(), original);
    assert_eq!(
        provider_count(&home.path().join("state_5.sqlite"), "openai"),
        1
    );
}

#[test]
fn repair_and_rollback_include_shared_config_settings_and_provider_isolation() {
    let (home, backup, _rollout) = fixture();
    fs::write(
        home.path().join("config.toml"),
        "model_provider = \"custom\"\n[model_providers.custom]\nbase_url = \"https://proxy.invalid\"\nexperimental_bearer_token = \"sk-private-fixture\"\n[plugins.chrome]\nenabled = true\n[plugins.sites]\nenabled = true\n",
    )
    .unwrap();
    let adapter = tempfile::tempdir().unwrap();
    let adapter_database = adapter.path().join("cc-switch.db");
    let connection = Connection::open(&adapter_database).unwrap();
    connection
        .execute_batch(include_str!(
            "../../../tests/fixtures/provider_interop/cc_switch_schema_16.sql"
        ))
        .unwrap();
    connection
        .execute(
            "UPDATE settings SET value = ?1 WHERE key = 'common_config_codex'",
            ["[plugins.chrome]\nenabled = true\n"],
        )
        .unwrap();
    drop(connection);
    let original_settings = serde_json::to_vec_pretty(&serde_json::json!({
        "codexConfigDir": home.path(),
        "currentProviderCodex": "provider-proxy",
        "preserveCodexOfficialAuthOnSwitch": false,
        "unifyCodexSessionHistory": false,
        "unifyCodexMigrateExisting": false
    }))
    .unwrap();
    fs::write(adapter.path().join("settings.json"), &original_settings).unwrap();
    let provider_before: String = Connection::open(&adapter_database)
        .unwrap()
        .query_row(
            "SELECT settings_config FROM providers WHERE id = 'provider-proxy'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let external = cc_switch::plan_repair(adapter.path(), home.path(), true, true).unwrap();
    let operation = "transaction-with-adapter";
    let audit = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", operation)
            .with_temporary_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();
    let plan = TransactionPlan::history(operation, home.path(), "openai", "custom", audit)
        .with_adapter(external);

    let result = apply_transaction(&plan, backup.path(), None).unwrap();
    let applied_database = Connection::open(&adapter_database).unwrap();
    let common: String = applied_database
        .query_row(
            "SELECT value FROM settings WHERE key = 'common_config_codex'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let provider_after: String = applied_database
        .query_row(
            "SELECT settings_config FROM providers WHERE id = 'provider-proxy'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    drop(applied_database);
    assert!(common.contains("[plugins.chrome]"));
    assert!(common.contains("[plugins.sites]"));
    assert!(!common.contains("experimental_bearer_token"));
    assert_eq!(provider_after, provider_before);
    let applied_settings: Value =
        serde_json::from_slice(&fs::read(adapter.path().join("settings.json")).unwrap()).unwrap();
    assert_eq!(applied_settings["preserveCodexOfficialAuthOnSwitch"], true);
    assert_eq!(applied_settings["unifyCodexSessionHistory"], true);
    assert_eq!(applied_settings["unifyCodexMigrateExisting"], true);

    rollback_transaction(Path::new(&result.manifest_path)).unwrap();
    assert_eq!(
        fs::read(adapter.path().join("settings.json")).unwrap(),
        original_settings
    );
    let restored_database = Connection::open(&adapter_database).unwrap();
    let restored_common: String = restored_database
        .query_row(
            "SELECT value FROM settings WHERE key = 'common_config_codex'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let provider_restored: String = restored_database
        .query_row(
            "SELECT settings_config FROM providers WHERE id = 'provider-proxy'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(restored_common, "[plugins.chrome]\nenabled = true\n");
    assert_eq!(provider_restored, provider_before);
}

#[test]
fn continuation_only_transaction_rewrites_jsonl_without_migrating_provider_bucket() {
    let (home, backup, rollout) = fixture();
    fs::write(
        &rollout,
        concat!(
            "{\"type\":\"session_meta\",\"payload\":{\"id\":\"t\",\"model_provider\":\"openai\"}}\n",
            "{\"type\":\"response_item\",\"payload\":{\"type\":\"reasoning\",\"id\":\"item_proxy\",\"encrypted_content\":\"opaque\",\"summary\":[]}}\n"
        ),
    )
    .unwrap();
    let audit = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", "continuation-only")
            .with_provider_rewrites(false)
            .with_continuation_rewrites(true)
            .with_temporary_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();
    let plan =
        TransactionPlan::history("continuation-only", home.path(), "openai", "custom", audit)
            .with_history_enabled(false)
            .with_continuation_enabled(true);

    let result = apply_transaction(&plan, backup.path(), None).unwrap();

    assert_eq!(result.jsonl_files_changed, 1);
    assert_eq!(result.sqlite_rows_changed, 0);
    assert_eq!(
        provider_count(&home.path().join("state_5.sqlite"), "openai"),
        1
    );
    assert!(!String::from_utf8(fs::read(&rollout).unwrap())
        .unwrap()
        .contains("item_proxy"));
}

#[test]
fn continuation_transaction_fails_closed_for_unknown_foreign_item_types() {
    let (home, backup, rollout) = fixture();
    fs::write(
        &rollout,
        concat!(
            "{\"type\":\"session_meta\",\"payload\":{\"id\":\"t\",\"model_provider\":\"openai\"}}\n",
            "{\"type\":\"response_item\",\"payload\":{\"type\":\"future_tool_call\",\"id\":\"item_unknown\"}}\n"
        ),
    )
    .unwrap();
    let audit = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", "continuation-unknown")
            .with_provider_rewrites(false)
            .with_continuation_rewrites(true)
            .with_temporary_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();
    let plan = TransactionPlan::history(
        "continuation-unknown",
        home.path(),
        "openai",
        "custom",
        audit,
    )
    .with_history_enabled(false)
    .with_continuation_enabled(true);

    let error = apply_transaction(&plan, backup.path(), None).unwrap_err();

    assert_eq!(error.code, "unsupported_foreign_response_item");
    assert!(String::from_utf8(fs::read(&rollout).unwrap())
        .unwrap()
        .contains("item_unknown"));
}
