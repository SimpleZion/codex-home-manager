use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use super::ResolvedEnvironment;

pub mod cc_switch;
pub mod native_codex;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AdapterSupport {
    ReadWrite,
    ReadOnly,
    NotFound,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteClassification {
    Official,
    ThirdParty,
    Local,
    Conflicting,
    Unknown,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteConfidence {
    Observed,
    ConfigurationOnly,
    Conflicting,
    Unknown,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Presence {
    Present,
    Absent,
    Unknown,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RouteSummary {
    pub classification: RouteClassification,
    pub confidence: RouteConfidence,
    pub model_provider: Option<String>,
    pub provider_name: Option<String>,
    pub base_url_present: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AuthenticationSummary {
    pub official_auth_required: Presence,
    pub provider_token: Presence,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InteroperabilitySettings {
    pub preserve_official_auth: Option<bool>,
    pub unify_session_history: Option<bool>,
    pub migrate_existing_history: Option<bool>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ExternalAdapterSummary {
    pub kind: String,
    pub support: AdapterSupport,
    pub root: Option<String>,
    pub database_path: Option<String>,
    pub user_version: Option<i64>,
    pub schema_fingerprint: Option<String>,
    pub database_quick_check: Option<String>,
    pub bound_codex_home: Option<String>,
    pub current_provider_name: Option<String>,
    pub current_provider_id_hash: Option<String>,
    pub settings: InteroperabilitySettings,
    pub reason: Option<String>,
}

impl ExternalAdapterSummary {
    pub fn not_found() -> Self {
        Self {
            kind: "cc_switch".to_string(),
            support: AdapterSupport::NotFound,
            root: None,
            database_path: None,
            user_version: None,
            schema_fingerprint: None,
            database_quick_check: None,
            bound_codex_home: None,
            current_provider_name: None,
            current_provider_id_hash: None,
            settings: InteroperabilitySettings::default(),
            reason: Some("adapter_not_found".to_string()),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AdapterInspection {
    pub environment: ResolvedEnvironment,
    pub adapter: ExternalAdapterSummary,
    pub route: RouteSummary,
    pub authentication: AuthenticationSummary,
    pub history_buckets: BTreeMap<String, u64>,
}
