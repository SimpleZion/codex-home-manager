use std::{fs, path::Path};

use codex_home_manager_service::provider_interop::{
    inspect_environment, AdapterSupport, InspectRequest, Presence, RouteClassification,
    RouteConfidence,
};
use rusqlite::Connection;
use serde_json::json;
use tempfile::TempDir;

fn fixture_root() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("tests/fixtures/provider_interop")
}

fn create_codex_home(config_name: &str) -> TempDir {
    let directory = tempfile::tempdir().unwrap();
    fs::copy(
        fixture_root().join(config_name),
        directory.path().join("config.toml"),
    )
    .unwrap();
    let state = Connection::open(directory.path().join("state_5.sqlite")).unwrap();
    state
        .execute_batch(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL);\n\
             INSERT INTO threads VALUES ('official', 'openai'), ('proxy', 'custom');",
        )
        .unwrap();
    directory
}

fn create_cc_switch(codex_home: &Path, user_version: i64, remove_provider_type: bool) -> TempDir {
    let directory = tempfile::tempdir().unwrap();
    let database_path = directory.path().join("cc-switch.db");
    let connection = Connection::open(&database_path).unwrap();
    connection
        .execute_batch(&fs::read_to_string(fixture_root().join("cc_switch_schema_16.sql")).unwrap())
        .unwrap();
    if remove_provider_type {
        connection
            .execute_batch(
                "ALTER TABLE providers RENAME TO providers_old;\n\
                 CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, is_current INTEGER);\n\
                 INSERT INTO providers SELECT id, app_type, name, settings_config, is_current FROM providers_old;\n\
                 DROP TABLE providers_old;",
            )
            .unwrap();
    }
    connection
        .pragma_update(None, "user_version", user_version)
        .unwrap();
    drop(connection);
    fs::write(
        directory.path().join("settings.json"),
        serde_json::to_vec_pretty(&json!({
            "codexConfigDir": codex_home,
            "currentProviderCodex": "provider-proxy",
            "preserveCodexOfficialAuthOnSwitch": true,
            "unifyCodexSessionHistory": true,
            "unifyCodexMigrateExisting": true
        }))
        .unwrap(),
    )
    .unwrap();
    directory
}

#[test]
fn schema_16_proxy_is_supported_and_redacted() {
    let codex_home = create_codex_home("codex_config_proxy.toml");
    let cc_switch = create_cc_switch(codex_home.path(), 16, false);

    let inspection = inspect_environment(InspectRequest::new(
        codex_home.path(),
        Some(cc_switch.path()),
    ))
    .unwrap();

    assert_eq!(inspection.adapter.support, AdapterSupport::ReadWrite);
    assert_eq!(
        inspection.route.classification,
        RouteClassification::ThirdParty
    );
    assert_eq!(
        inspection.route.confidence,
        RouteConfidence::ConfigurationOnly
    );
    assert_eq!(inspection.authentication.provider_token, Presence::Present);
    assert_eq!(inspection.history_buckets.get("custom"), Some(&1));
    assert_eq!(inspection.history_buckets.get("openai"), Some(&1));
    let public = serde_json::to_string(&inspection).unwrap();
    assert!(!public.contains("fixture-secret"));
    assert!(!public.contains("provider-proxy"));
    assert!(public.contains("Fixture Proxy"));
}

#[test]
fn schema_18_proxy_is_supported_when_required_contract_columns_are_present() {
    let codex_home = create_codex_home("codex_config_proxy.toml");
    let cc_switch = create_cc_switch(codex_home.path(), 18, false);

    let inspection = inspect_environment(InspectRequest::new(
        codex_home.path(),
        Some(cc_switch.path()),
    ))
    .unwrap();

    assert_eq!(inspection.adapter.support, AdapterSupport::ReadWrite);
    assert_eq!(inspection.adapter.reason, None);
    assert_eq!(inspection.adapter.user_version, Some(18));
}

#[test]
fn official_route_does_not_require_an_external_manager() {
    let codex_home = create_codex_home("codex_config_official.toml");
    let inspection = inspect_environment(InspectRequest::new(codex_home.path(), None)).unwrap();

    assert_eq!(inspection.adapter.support, AdapterSupport::NotFound);
    assert_eq!(
        inspection.route.classification,
        RouteClassification::Official
    );
    assert_eq!(
        inspection.authentication.official_auth_required,
        Presence::Present
    );
    assert_eq!(inspection.authentication.provider_token, Presence::Absent);
}

#[test]
fn unknown_user_version_is_read_only() {
    let codex_home = create_codex_home("codex_config_proxy.toml");
    let cc_switch = create_cc_switch(codex_home.path(), 99, false);
    let inspection = inspect_environment(InspectRequest::new(
        codex_home.path(),
        Some(cc_switch.path()),
    ))
    .unwrap();

    assert_eq!(inspection.adapter.support, AdapterSupport::ReadOnly);
    assert_eq!(
        inspection.adapter.reason.as_deref(),
        Some("unsupported_user_version")
    );
}

#[test]
fn missing_required_column_is_read_only() {
    let codex_home = create_codex_home("codex_config_proxy.toml");
    let cc_switch = create_cc_switch(codex_home.path(), 16, true);
    let inspection = inspect_environment(InspectRequest::new(
        codex_home.path(),
        Some(cc_switch.path()),
    ))
    .unwrap();

    assert_eq!(inspection.adapter.support, AdapterSupport::ReadOnly);
    assert_eq!(
        inspection.adapter.reason.as_deref(),
        Some("unsupported_schema_fingerprint")
    );
}

#[test]
fn codex_home_mismatch_is_read_only() {
    let codex_home = create_codex_home("codex_config_proxy.toml");
    let other_home = tempfile::tempdir().unwrap();
    let cc_switch = create_cc_switch(other_home.path(), 16, false);
    let inspection = inspect_environment(InspectRequest::new(
        codex_home.path(),
        Some(cc_switch.path()),
    ))
    .unwrap();

    assert_eq!(inspection.adapter.support, AdapterSupport::ReadOnly);
    assert_eq!(
        inspection.adapter.reason.as_deref(),
        Some("codex_home_mismatch")
    );
}
