use std::{fs, path::Path};

use codex_home_manager_service::provider_interop::{
    classify_config, plan_common_merge, ConfigClass, ProviderSnapshot,
};

fn fixture(name: &str) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("tests/fixtures/provider_interop")
        .join(name);
    fs::read_to_string(path).unwrap()
}

#[test]
fn classifier_separates_shared_scoped_dynamic_secret_and_unknown_fields() {
    let classification = classify_config(&fixture("desktop_dynamic_config.toml")).unwrap();
    assert_eq!(
        classification.class_for("plugins"),
        Some(ConfigClass::Shared)
    );
    assert_eq!(
        classification.class_for("model_provider"),
        Some(ConfigClass::ProviderScoped)
    );
    assert_eq!(
        classification.class_for("mcp_servers.node_repl"),
        Some(ConfigClass::DesktopDynamic)
    );
    assert_eq!(
        classification.class_for("mcp_servers.credential_tool"),
        Some(ConfigClass::Secret)
    );
    assert_eq!(
        classification.class_for("mcp_servers.stable_tool"),
        Some(ConfigClass::Shared)
    );
    assert_eq!(
        classification.class_for("unrecognized_section"),
        Some(ConfigClass::Unknown)
    );
}

#[test]
fn merge_copies_only_safe_shared_declarations_and_preserves_snapshots() {
    let common = fixture("common_config.toml");
    let live = fixture("desktop_dynamic_config.toml");
    let snapshots = vec![ProviderSnapshot {
        id_hash: "provider-hash".to_string(),
        config_sha256: "snapshot-sha".to_string(),
    }];
    let plan = plan_common_merge(&common, &live, &snapshots).unwrap();

    assert!(plan.rendered_common.contains("[plugins.chrome]"));
    assert!(plan.rendered_common.contains("[plugins.sites]"));
    assert!(plan.rendered_common.contains("[mcp_servers.stable_tool]"));
    assert!(plan.rendered_common.contains("--new"));
    assert!(!plan.rendered_common.contains("model_provider"));
    assert!(!plan.rendered_common.contains("base_url"));
    assert!(!plan.rendered_common.contains("fixture-secret"));
    assert!(!plan.rendered_common.contains("node_repl"));
    assert!(!plan.rendered_common.contains("unrecognized_section"));
    assert_eq!(plan.provider_snapshots, snapshots);
    assert!(plan
        .conflicts
        .iter()
        .any(|path| path == "mcp_servers.stable_tool"));
    assert!(plan
        .blocked_paths
        .iter()
        .any(|path| path == "unrecognized_section"));
}

#[test]
fn malformed_toml_is_rejected_without_partial_output() {
    let error = classify_config("[plugins\ninvalid").unwrap_err();
    assert_eq!(error.code, "invalid_common_config");
}
