use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use toml_edit::{DocumentMut, Item, Table};

use super::InteropError;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConfigClass {
    Shared,
    ProviderScoped,
    DesktopDynamic,
    Secret,
    Unknown,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfigDecision {
    pub path: String,
    pub class: ConfigClass,
    pub reason: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfigClassification {
    pub decisions: Vec<ConfigDecision>,
}

impl ConfigClassification {
    pub fn class_for(&self, path: &str) -> Option<ConfigClass> {
        self.decisions
            .iter()
            .find(|decision| decision.path == path)
            .map(|decision| decision.class)
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProviderSnapshot {
    pub id_hash: String,
    pub config_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfigMergePlan {
    pub rendered_common: String,
    pub source_common_sha256: String,
    pub rendered_common_sha256: String,
    pub decisions: Vec<ConfigDecision>,
    pub conflicts: Vec<String>,
    pub blocked_paths: Vec<String>,
    pub excluded_paths: Vec<String>,
    pub provider_snapshots: Vec<ProviderSnapshot>,
}

pub fn classify_config(source: &str) -> Result<ConfigClassification, InteropError> {
    let document = parse_document(source)?;
    Ok(ConfigClassification {
        decisions: classify_document(&document),
    })
}

pub fn plan_common_merge(
    common: &str,
    live: &str,
    provider_snapshots: &[ProviderSnapshot],
) -> Result<ConfigMergePlan, InteropError> {
    let mut common_document = parse_document(common)?;
    let live_document = parse_document(live)?;
    let common_decisions = classify_document(&common_document);
    let live_decisions = classify_document(&live_document);
    remove_non_shared(&mut common_document, &common_decisions);

    let mut conflicts = Vec::new();
    merge_shared(
        &mut common_document,
        &live_document,
        &live_decisions,
        &mut conflicts,
    )?;
    conflicts.sort();
    conflicts.dedup();
    let rendered_common = common_document.to_string();
    let rendered_classification = classify_config(&rendered_common)?;
    if rendered_classification
        .decisions
        .iter()
        .any(|decision| decision.class != ConfigClass::Shared)
    {
        return Err(InteropError::new(
            "common_config_classification_blocked",
            "rendered common configuration contains a non-shared declaration",
        ));
    }
    let mut blocked_paths: Vec<String> = live_decisions
        .iter()
        .filter(|decision| decision.class == ConfigClass::Unknown)
        .map(|decision| decision.path.clone())
        .collect();
    blocked_paths.sort();
    let mut excluded_paths: Vec<String> = live_decisions
        .iter()
        .filter(|decision| decision.class != ConfigClass::Shared)
        .map(|decision| decision.path.clone())
        .collect();
    excluded_paths.sort();
    Ok(ConfigMergePlan {
        source_common_sha256: sha256(common.as_bytes()),
        rendered_common_sha256: sha256(rendered_common.as_bytes()),
        rendered_common,
        decisions: live_decisions,
        conflicts,
        blocked_paths,
        excluded_paths,
        provider_snapshots: provider_snapshots.to_vec(),
    })
}

fn parse_document(source: &str) -> Result<DocumentMut, InteropError> {
    source.parse::<DocumentMut>().map_err(|_| {
        InteropError::new(
            "invalid_common_config",
            "provider common configuration is not valid TOML",
        )
    })
}

fn classify_document(document: &DocumentMut) -> Vec<ConfigDecision> {
    let mut decisions = Vec::new();
    for (key, item) in document.iter() {
        match key {
            "plugins" | "plugin_marketplaces" | "features" => decisions.push(decision(
                key,
                ConfigClass::Shared,
                "provider_independent_declaration",
            )),
            "mcp_servers" => classify_mcp_servers(item, &mut decisions),
            "notify" => decisions.push(decision(
                key,
                ConfigClass::DesktopDynamic,
                "desktop_managed_notify",
            )),
            key if provider_scoped_key(key) => decisions.push(decision(
                key,
                ConfigClass::ProviderScoped,
                "provider_route_or_identity",
            )),
            _ if contains_sensitive_material(item) => decisions.push(decision(
                key,
                ConfigClass::Secret,
                "credential_like_declaration",
            )),
            _ => decisions.push(decision(
                key,
                ConfigClass::Unknown,
                "unrecognized_declaration",
            )),
        }
    }
    decisions
}

fn classify_mcp_servers(item: &Item, decisions: &mut Vec<ConfigDecision>) {
    let Some(table) = item.as_table() else {
        decisions.push(decision(
            "mcp_servers",
            ConfigClass::Unknown,
            "invalid_mcp_table",
        ));
        return;
    };
    for (name, server) in table.iter() {
        let path = format!("mcp_servers.{name}");
        if desktop_dynamic_mcp(name, server) {
            decisions.push(decision(
                &path,
                ConfigClass::DesktopDynamic,
                "desktop_dynamic_mcp",
            ));
        } else if contains_sensitive_material(server) {
            decisions.push(decision(
                &path,
                ConfigClass::Secret,
                "credential_bearing_mcp",
            ));
        } else {
            decisions.push(decision(
                &path,
                ConfigClass::Shared,
                "stable_provider_independent_mcp",
            ));
        }
    }
}

fn decision(path: &str, class: ConfigClass, reason: &str) -> ConfigDecision {
    ConfigDecision {
        path: path.to_string(),
        class,
        reason: reason.to_string(),
    }
}

fn provider_scoped_key(key: &str) -> bool {
    matches!(
        key,
        "model"
            | "model_provider"
            | "model_providers"
            | "profile"
            | "profiles"
            | "project"
            | "projects"
            | "model_catalog_json"
            | "web_search"
    )
}

fn desktop_dynamic_mcp(name: &str, item: &Item) -> bool {
    let lower = item.to_string().to_ascii_lowercase();
    name.eq_ignore_ascii_case("node_repl")
        || lower.contains("sky_cua_native_pipe")
        || lower.contains("windowsapps")
        || lower.contains("codex-computer-use")
        || lower.contains("turn-ended")
        || lower.contains("@oai/sky")
}

fn contains_sensitive_material(item: &Item) -> bool {
    let lower = item.to_string().to_ascii_lowercase();
    [
        "authorization",
        "bearer ",
        "api_key",
        "apikey",
        "experimental_bearer_token",
        "access_token",
        "refresh_token",
        "password",
        "client_secret",
        "cookie",
    ]
    .iter()
    .any(|needle| lower.contains(needle))
}

fn remove_non_shared(document: &mut DocumentMut, decisions: &[ConfigDecision]) {
    let top_level_to_remove: Vec<String> = decisions
        .iter()
        .filter(|decision| {
            decision.class != ConfigClass::Shared && !decision.path.starts_with("mcp_servers.")
        })
        .map(|decision| decision.path.clone())
        .collect();
    for key in top_level_to_remove {
        document.remove(&key);
    }
    let mcp_to_remove: Vec<String> = decisions
        .iter()
        .filter(|decision| {
            decision.path.starts_with("mcp_servers.") && decision.class != ConfigClass::Shared
        })
        .filter_map(|decision| {
            decision
                .path
                .split_once('.')
                .map(|(_, name)| name.to_string())
        })
        .collect();
    if let Some(table) = document.get_mut("mcp_servers").and_then(Item::as_table_mut) {
        for name in mcp_to_remove {
            table.remove(&name);
        }
    }
}

fn merge_shared(
    target: &mut DocumentMut,
    source: &DocumentMut,
    decisions: &[ConfigDecision],
    conflicts: &mut Vec<String>,
) -> Result<(), InteropError> {
    for decision in decisions {
        if decision.class != ConfigClass::Shared {
            continue;
        }
        if let Some((root, child)) = decision.path.split_once('.') {
            let source_table = source.get(root).and_then(Item::as_table).ok_or_else(|| {
                InteropError::new("invalid_common_config", "shared table is invalid")
            })?;
            let source_item = source_table.get(child).ok_or_else(|| {
                InteropError::new("invalid_common_config", "shared entry is missing")
            })?;
            let target_table = ensure_table(target, root);
            record_conflict(
                target_table.get(child),
                source_item,
                &decision.path,
                conflicts,
            );
            target_table.insert(child, source_item.clone());
        } else {
            let source_item = source.get(&decision.path).ok_or_else(|| {
                InteropError::new("invalid_common_config", "shared declaration is missing")
            })?;
            let source_table = source_item.as_table().ok_or_else(|| {
                InteropError::new(
                    "invalid_common_config",
                    "shared declaration must be a table",
                )
            })?;
            let target_table = ensure_table(target, &decision.path);
            for (child, item) in source_table.iter() {
                let path = format!("{}.{}", decision.path, child);
                record_conflict(target_table.get(child), item, &path, conflicts);
                target_table.insert(child, item.clone());
            }
        }
    }
    Ok(())
}

fn ensure_table<'a>(document: &'a mut DocumentMut, key: &str) -> &'a mut Table {
    if !document.get(key).is_some_and(Item::is_table) {
        document[key] = Item::Table(Table::new());
    }
    document[key].as_table_mut().expect("table inserted above")
}

fn record_conflict(
    existing: Option<&Item>,
    replacement: &Item,
    path: &str,
    conflicts: &mut Vec<String>,
) {
    if existing.is_some_and(|item| item.to_string() != replacement.to_string()) {
        conflicts.push(path.to_string());
    }
}

fn sha256(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}
