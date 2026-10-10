use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

use rusqlite::{Connection, OpenFlags};
use toml_edit::{DocumentMut, Item};

use super::{AuthenticationSummary, Presence, RouteClassification, RouteConfidence, RouteSummary};
use crate::provider_interop::InteropError;

pub struct NativeInspection {
    pub config_path: PathBuf,
    pub database_path: PathBuf,
    pub route: RouteSummary,
    pub authentication: AuthenticationSummary,
    pub history_buckets: BTreeMap<String, u64>,
}

pub fn inspect(codex_home: &Path) -> Result<NativeInspection, InteropError> {
    let config_path = codex_home.join("config.toml");
    let database_path = codex_home.join("state_5.sqlite");
    let config_text = fs::read_to_string(&config_path).map_err(|_| {
        InteropError::new("codex_config_not_found", "Codex config.toml cannot be read")
    })?;
    let document = config_text.parse::<DocumentMut>().map_err(|_| {
        InteropError::new(
            "invalid_codex_config",
            "Codex config.toml is not valid TOML",
        )
    })?;
    let model_provider = document
        .get("model_provider")
        .and_then(Item::as_str)
        .map(ToOwned::to_owned);
    let provider_table = model_provider.as_deref().and_then(|provider| {
        document
            .get("model_providers")?
            .as_table()?
            .get(provider)?
            .as_table()
    });
    let provider_name = provider_table
        .and_then(|table| table.get("name"))
        .and_then(Item::as_str)
        .map(ToOwned::to_owned);
    let base_url = provider_table
        .and_then(|table| table.get("base_url"))
        .and_then(Item::as_str)
        .filter(|value| !value.trim().is_empty());
    let official_auth_required = provider_table
        .and_then(|table| table.get("requires_openai_auth"))
        .and_then(Item::as_bool)
        .unwrap_or(false);
    let provider_token = provider_table
        .and_then(|table| table.get("experimental_bearer_token"))
        .and_then(Item::as_str)
        .is_some_and(|value| !value.trim().is_empty());
    let classification = if base_url.is_some() && official_auth_required {
        RouteClassification::Conflicting
    } else if let Some(url) = base_url {
        if is_local_url(url) {
            RouteClassification::Local
        } else {
            RouteClassification::ThirdParty
        }
    } else if official_auth_required || model_provider.as_deref() == Some("openai") {
        RouteClassification::Official
    } else {
        RouteClassification::Unknown
    };
    let confidence = match classification {
        RouteClassification::Conflicting => RouteConfidence::Conflicting,
        RouteClassification::Unknown => RouteConfidence::Unknown,
        _ => RouteConfidence::ConfigurationOnly,
    };
    Ok(NativeInspection {
        config_path,
        database_path: database_path.clone(),
        route: RouteSummary {
            classification,
            confidence,
            model_provider,
            provider_name,
            base_url_present: base_url.is_some(),
        },
        authentication: AuthenticationSummary {
            official_auth_required: presence(official_auth_required),
            provider_token: presence(provider_token),
        },
        history_buckets: read_history_buckets(&database_path)?,
    })
}

fn presence(value: bool) -> Presence {
    if value {
        Presence::Present
    } else {
        Presence::Absent
    }
}

fn is_local_url(url: &str) -> bool {
    let lower = url.trim().to_ascii_lowercase();
    lower.starts_with("http://127.0.0.1")
        || lower.starts_with("https://127.0.0.1")
        || lower.starts_with("http://localhost")
        || lower.starts_with("https://localhost")
        || lower.starts_with("http://[::1]")
        || lower.starts_with("https://[::1]")
}

fn read_history_buckets(database_path: &Path) -> Result<BTreeMap<String, u64>, InteropError> {
    if !database_path.is_file() {
        return Ok(BTreeMap::new());
    }
    let connection = Connection::open_with_flags(
        database_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| {
        InteropError::new(
            "codex_state_unreadable",
            "Codex state database cannot be opened",
        )
    })?;
    let mut statement = connection
        .prepare("SELECT model_provider, COUNT(*) FROM threads GROUP BY model_provider ORDER BY model_provider")
        .map_err(|_| InteropError::new("codex_state_schema_invalid", "Codex threads.model_provider cannot be queried"))?;
    let rows = statement
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(|_| {
            InteropError::new(
                "codex_state_query_failed",
                "Codex history buckets cannot be queried",
            )
        })?;
    let mut buckets = BTreeMap::new();
    for row in rows {
        let (provider, count) = row.map_err(|_| {
            InteropError::new(
                "codex_state_query_failed",
                "Codex history bucket row is invalid",
            )
        })?;
        buckets.insert(provider, count.max(0) as u64);
    }
    Ok(buckets)
}
