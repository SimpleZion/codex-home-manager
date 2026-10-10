use std::{
    env, fs,
    io::Read,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use serde_json::{json, Value};

const MAXIMUM_STATE_BYTES: u64 = 1024 * 1024;
const DEFAULT_ARTIFACT_ROOT: &str = r"D:\ZFiles\CXProject\Codex";
const REQUIRED_FEATURE_CONTRACT_VERSION: u64 = 5;

fn text(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn hash(value: Option<&Value>) -> String {
    text(value).to_uppercase()
}

fn package_full_name_from_path(path_text: &str) -> String {
    Path::new(path_text)
        .ancestors()
        .filter_map(Path::file_name)
        .filter_map(|name| name.to_str())
        .find(|name| name.to_ascii_lowercase().starts_with("openai.codex_"))
        .unwrap_or_default()
        .to_string()
}

fn candidate_paths(environment_name: &str, filename: &str) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(value) = env::var(environment_name) {
        if !value.trim().is_empty() {
            return vec![PathBuf::from(value)];
        }
    }
    if let Ok(local_app_data) = env::var("LOCALAPPDATA") {
        candidates.push(
            PathBuf::from(local_app_data)
                .join("CodexHomeManager")
                .join("pagination")
                .join(filename),
        );
    }
    candidates
        .push(PathBuf::from(DEFAULT_ARTIFACT_ROOT).join(format!("codex-pagination-{filename}")));
    candidates
}

fn read_first_state(paths: &[PathBuf]) -> (Option<Value>, String, String) {
    for path in paths {
        if !path.is_file() {
            continue;
        }
        match fs::metadata(path) {
            Ok(metadata) if metadata.len() > MAXIMUM_STATE_BYTES => {
                return (
                    None,
                    path.to_string_lossy().into_owned(),
                    "state file exceeds 1 MiB".to_string(),
                );
            }
            Err(error) => return (None, path.to_string_lossy().into_owned(), error.to_string()),
            _ => {}
        }
        let source = fs::File::open(path).and_then(|file| {
            let mut bytes = Vec::new();
            file.take(MAXIMUM_STATE_BYTES + 1).read_to_end(&mut bytes)?;
            Ok(bytes)
        });
        match source
            .ok()
            .filter(|bytes| bytes.len() as u64 <= MAXIMUM_STATE_BYTES)
            .and_then(|source| {
                serde_json::from_slice::<Value>(
                    source.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(&source),
                )
                .ok()
            }) {
            Some(value @ Value::Object(_)) => {
                return (
                    Some(value),
                    path.to_string_lossy().into_owned(),
                    String::new(),
                )
            }
            _ => {
                return (
                    None,
                    path.to_string_lossy().into_owned(),
                    "state file is not valid JSON object".to_string(),
                )
            }
        }
    }
    (None, String::new(), String::new())
}

fn positive_integer(value: Option<&Value>, fallback: u64) -> u64 {
    value
        .and_then(Value::as_u64)
        .filter(|number| *number > 0)
        .unwrap_or(fallback)
}

fn version_parts(value: Option<&Value>) -> Option<Vec<u64>> {
    let value = text(value);
    if value.is_empty() {
        return None;
    }
    value
        .split('.')
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()
        .ok()
}

fn state_view(state: Option<&Value>, path: &str) -> Option<Value> {
    let object = state?.as_object()?;
    Some(json!({
        "path": path,
        "status": text(object.get("status")),
        "targetPackageFullName": text(object.get("mainPackageFullName")),
        "mainVersion": text(object.get("mainVersion")),
        "packageVersion": text(object.get("packageVersion")),
        "packageStatus": text(object.get("packageStatus")),
        "featureContractVersion": positive_integer(object.get("featureContractVersion"), 0),
        "freshTurnReadItemBoundary": object.get("freshTurnReadItemBoundary").and_then(Value::as_bool).unwrap_or(false),
        "bundleRuntimeAuditPassed": object.get("bundleRuntimeAuditPassed").and_then(Value::as_bool).unwrap_or(false),
        "initialTurnItemsPaginated": object.get("initialTurnItemsPaginated").and_then(Value::as_bool).unwrap_or(false),
        "initialTurnItemPageSize": positive_integer(object.get("initialTurnItemPageSize"), 0),
        "promptRailLoadedTurnsOnly": object.get("promptRailLoadedTurnsOnly").and_then(Value::as_bool).unwrap_or(false),
        "periodicStatusMetadataOnly": object.get("periodicStatusMetadataOnly").and_then(Value::as_bool).unwrap_or(false),
        "priorityFeedMetadataOnly": object.get("priorityFeedMetadataOnly").and_then(Value::as_bool).unwrap_or(false),
        "passiveTabRestorationLoadedTurnsOnly": object.get("passiveTabRestorationLoadedTurnsOnly").and_then(Value::as_bool).unwrap_or(false),
        "runtimeAcceptanceRequired": object.get("runtimeAcceptanceRequired").and_then(Value::as_bool).unwrap_or(false),
        "runtimeStatus": text(object.get("runtimeStatus")),
        "runtimeValidatedAt": text(object.get("runtimeValidatedAt")),
        "runtimeObservationSeconds": positive_integer(object.get("runtimeObservationSeconds"), 0),
        "runtimeBackgroundTurnListRequests": object.get("runtimeBackgroundTurnListRequests").cloned().unwrap_or(Value::Null),
        "runtimeMemoryGrowthMiB": object.get("runtimeMemoryGrowthMiB").cloned().unwrap_or(Value::Null),
        "runtimeTargetThreadId": text(object.get("runtimeTargetThreadId")),
        "runtimeReportPath": text(object.get("runtimeReportPath")),
        "patchedAsarSha256": hash(object.get("patchedAsarSha256")),
        "effectiveAsarHash": hash(object.get("effectiveAsarHash")),
        "initialTurns": positive_integer(object.get("initialTurns"), 1),
        "userPageTurns": positive_integer(object.get("userPageTurns"), 3),
        "preparedAt": text(object.get("preparedAt")),
        "finishedAt": text(object.get("finishedAt")),
        "error": text(object.get("error")),
    }))
}

fn field<'a>(value: Option<&'a Value>, name: &str) -> Option<&'a Value> {
    value
        .and_then(Value::as_object)
        .and_then(|object| object.get(name))
}

pub fn status(desktop_install: Option<&Value>) -> Value {
    status_with_reader(desktop_install, |environment_name, filename| {
        read_first_state(&candidate_paths(environment_name, filename))
    })
}

fn status_with_reader(
    desktop_install: Option<&Value>,
    read_state: impl Fn(&str, &str) -> (Option<Value>, String, String),
) -> Value {
    let current_path = text(field(desktop_install, "path"));
    let current_package = {
        let explicit = text(field(desktop_install, "packageFullName"));
        if explicit.is_empty() {
            package_full_name_from_path(&current_path)
        } else {
            explicit
        }
    };
    let current_version = text(field(desktop_install, "version"));
    let modern_contract = version_parts(field(desktop_install, "version"))
        .map(|version| version >= vec![26, 901, 6511, 0])
        .unwrap_or(true);
    let contract_version = if modern_contract {
        7
    } else {
        REQUIRED_FEATURE_CONTRACT_VERSION
    };
    let valid_features = |value: Option<&Value>| {
        field(value, "initialTurnItemsPaginated").and_then(Value::as_bool) == Some(true)
            && field(value, "initialTurnItemPageSize").and_then(Value::as_u64) == Some(100)
            && (!modern_contract
                || (field(value, "freshTurnReadItemBoundary").and_then(Value::as_bool)
                    == Some(true)
                    && field(value, "bundleRuntimeAuditPassed").and_then(Value::as_bool)
                        == Some(true)))
    };
    let (ready_state, ready_path, ready_error) =
        read_state("CODEX_PAGINATION_READY_STATE", "ready.json");
    let (installed_state, installed_path, installed_error) =
        read_state("CODEX_PAGINATION_INSTALLED_STATE", "installed.json");
    let ready = state_view(ready_state.as_ref(), &ready_path);
    let installed = state_view(installed_state.as_ref(), &installed_path);
    let receipt_observed_at = text(field(installed.as_ref(), "runtimeValidatedAt"));

    let mut state = "missing";
    let mut action = "prepare";
    let mut update_available = false;
    let mut reason = "No prepared or installed pagination patch evidence was found.".to_string();
    if !ready_error.is_empty() || !installed_error.is_empty() {
        state = "invalid";
        action = "verify";
        reason = format!(
            "Pagination state evidence cannot be parsed: {}",
            if ready_error.is_empty() {
                installed_error
            } else {
                ready_error
            }
        );
    } else if current_package.is_empty() {
        state = "unknown";
        action = "verify";
        reason = "The active Codex package identity is unavailable.".to_string();
    } else {
        let installed_target = text(field(installed.as_ref(), "targetPackageFullName"));
        let ready_target = text(field(ready.as_ref(), "targetPackageFullName"));
        let installed_hash = hash(field(installed.as_ref(), "effectiveAsarHash"));
        let expected_hash = hash(field(installed.as_ref(), "patchedAsarSha256"));
        let installed_receipt_matches = installed.is_some()
            && installed_target == current_package
            && text(field(installed.as_ref(), "status")) == "success"
            && text(field(installed.as_ref(), "packageStatus")) == "Ok"
            && !installed_hash.is_empty()
            && installed_hash == expected_hash
            && field(installed.as_ref(), "featureContractVersion")
                .and_then(Value::as_u64)
                .unwrap_or_default()
                >= contract_version
            && valid_features(installed.as_ref())
            && field(installed.as_ref(), "promptRailLoadedTurnsOnly").and_then(Value::as_bool)
                == Some(true)
            && field(installed.as_ref(), "periodicStatusMetadataOnly").and_then(Value::as_bool)
                == Some(true)
            && field(installed.as_ref(), "priorityFeedMetadataOnly").and_then(Value::as_bool)
                == Some(true)
            && field(installed.as_ref(), "passiveTabRestorationLoadedTurnsOnly")
                .and_then(Value::as_bool)
                == Some(true)
            && field(installed.as_ref(), "runtimeAcceptanceRequired").and_then(Value::as_bool)
                == Some(true);
        let runtime_status = text(field(installed.as_ref(), "runtimeStatus")).to_ascii_lowercase();
        let receipt_runtime_passed = runtime_status == "passed"
            && field(installed.as_ref(), "runtimeObservationSeconds")
                .and_then(Value::as_u64)
                .unwrap_or_default()
                >= 180
            && field(installed.as_ref(), "runtimeBackgroundTurnListRequests")
                .and_then(Value::as_u64)
                == Some(0);
        let ready_compatible = ready.is_some()
            && ready_target == current_package
            && text(field(ready.as_ref(), "status")) == "ready"
            && field(ready.as_ref(), "featureContractVersion")
                .and_then(Value::as_u64)
                .unwrap_or_default()
                >= contract_version
            && valid_features(ready.as_ref())
            && field(ready.as_ref(), "promptRailLoadedTurnsOnly").and_then(Value::as_bool)
                == Some(true)
            && field(ready.as_ref(), "periodicStatusMetadataOnly").and_then(Value::as_bool)
                == Some(true)
            && field(ready.as_ref(), "priorityFeedMetadataOnly").and_then(Value::as_bool)
                == Some(true)
            && field(ready.as_ref(), "passiveTabRestorationLoadedTurnsOnly")
                .and_then(Value::as_bool)
                == Some(true)
            && field(ready.as_ref(), "runtimeAcceptanceRequired").and_then(Value::as_bool)
                == Some(true);
        let ready_version = version_parts(field(ready.as_ref(), "packageVersion"));
        let installed_version = version_parts(field(installed.as_ref(), "packageVersion"));
        update_available = ready_compatible
            && matches!((&ready_version, &installed_version), (Some(ready), Some(installed)) if ready > installed);
        if installed_receipt_matches && update_available {
            state = "ready";
            action = "install";
            reason = "A newer patch receipt satisfies the package contract; current runtime state has not been verified.".to_string();
        } else if installed_receipt_matches && receipt_runtime_passed {
            // Receipt contents cannot attest a live process or its currently loaded bytes.
            state = "unverified";
            action = "verify";
            let observed_at = if receipt_observed_at.is_empty() {
                "an unknown time"
            } else {
                &receipt_observed_at
            };
            reason = format!("The installation receipt reports passed runtime acceptance at {observed_at}; current PID/creation time, Optional registration, and loaded ASAR bytes have not been verified.");
        } else if installed_receipt_matches && runtime_status == "failed" {
            state = "invalid";
            action = "verify";
            reason = "The installation receipt reports failed long-thread runtime acceptance; current runtime state has not been verified.".to_string();
        } else if installed_receipt_matches {
            state = "unverified";
            action = "verify";
            reason = "The installation receipt matches the package contract, but does not report complete runtime acceptance; current runtime state has not been verified.".to_string();
        } else if ready_compatible {
            state = "ready";
            action = "install";
            reason = "A prepared patch receipt satisfies the package contract; current runtime state has not been verified.".to_string();
        } else if (!installed_target.is_empty() && installed_target != current_package)
            || (!ready_target.is_empty() && ready_target != current_package)
        {
            state = "stale";
            action = "rebuild";
            reason = "Codex was updated after the pagination patch was prepared or installed."
                .to_string();
        } else if installed.is_some() || ready.is_some() {
            state = "invalid";
            action = "verify";
            reason = "Pagination evidence exists but its status or effective ASAR hash does not satisfy the patch contract.".to_string();
        }
    }

    let checked_at_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    json!({
        "schemaVersion": 3,
        "state": state,
        "action": action,
        "reason": reason,
        "checkedAtMs": checked_at_ms,
        "evidenceScope": "receipt_only",
        "receiptObservedAt": receipt_observed_at,
        "currentRuntimeVerified": false,
        "current": {"packageFullName": current_package, "version": current_version, "path": current_path},
        "policy": {
            "requiredFeatureContractVersion": contract_version,
            "initialTurnItemsPaginated": true,
            "initialTurnItemPageSize": 100,
            "initialTurns": 1,
            "userPageTurns": 3,
            "oneRequestPerTopGesture": true,
            "promptRailLoadedTurnsOnly": true,
            "periodicStatusMetadataOnly": true,
            "priorityFeedMetadataOnly": true,
            "passiveTabRestorationLoadedTurnsOnly": true,
            "runtimeObservationSeconds": 180,
        },
        "updateAvailable": update_available,
        "prepared": ready,
        "installed": installed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_fields(base: &Value, overrides: Option<&Value>) -> Value {
        let mut fields = base.as_object().unwrap().clone();
        if let Some(overrides) = overrides.and_then(Value::as_object) {
            fields.extend(overrides.clone());
        }
        Value::Object(fields)
    }

    #[test]
    fn receipt_only_status_matches_shared_contract() {
        let fixtures: Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/pagination_receipt_status.json"
        ))
        .unwrap();
        for case in fixtures["cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let installed = if case.get("installed") == Some(&Value::Null) {
                None
            } else {
                Some(fixture_fields(
                    &fixtures["installed"],
                    case.get("installed"),
                ))
            };
            let ready = case
                .get("ready")
                .map(|overrides| fixture_fields(&fixtures["installed"], Some(overrides)));
            let desktop = fixture_fields(&fixtures["desktop"], case.get("desktop"));
            let result =
                status_with_reader(Some(&desktop), |_environment, filename| match filename {
                    "ready.json" => (ready.clone(), filename.to_string(), String::new()),
                    "installed.json" => (
                        installed.clone(),
                        filename.to_string(),
                        text(case.get("installedError")),
                    ),
                    _ => panic!("unexpected state read"),
                });
            assert_eq!(result["state"], case["state"], "{name}");
            assert_eq!(result["action"], case["action"], "{name}");
            assert_ne!(result["state"], "effective", "{name}");
            assert_eq!(result["currentRuntimeVerified"], false, "{name}");
            assert_eq!(result["evidenceScope"], "receipt_only", "{name}");
            assert_eq!(result["schemaVersion"], 3, "{name}");
            assert!(result["checkedAtMs"].as_u64().is_some(), "{name}");
            assert_eq!(
                result["receiptObservedAt"],
                text(field(installed.as_ref(), "runtimeValidatedAt")),
                "{name}"
            );
            assert_eq!(
                result["updateAvailable"].as_bool(),
                Some(
                    case.get("updateAvailable")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                ),
                "{name}"
            );
            assert!(
                result["reason"]
                    .as_str()
                    .unwrap()
                    .contains(&text(case.get("reasonContains"))),
                "{name}"
            );
            if let Some(installed) = installed.as_ref() {
                for key in [
                    "preparedAt",
                    "finishedAt",
                    "runtimeStatus",
                    "runtimeReportPath",
                    "runtimeMemoryGrowthMiB",
                ] {
                    assert_eq!(result["installed"][key], installed[key], "{name}: {key}");
                }
            }
        }
    }

    #[test]
    fn extracts_package_identity_from_executable_path() {
        assert_eq!(
            package_full_name_from_path(
                r"C:\Program Files\WindowsApps\OpenAI.Codex_26.831.1445.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
            ),
            "OpenAI.Codex_26.831.1445.0_x64__2p2nqsd0c76g0"
        );
    }
}
