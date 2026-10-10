use std::io::{self, Read, Write};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::adapters::cc_switch;
use super::redact_json;
use super::sqlite::count_provider_rows;
use super::{
    apply_transaction, audit_continuation_candidates, audit_continuation_paths, audit_history,
    inspect_environment, rollback_transaction, CancellationFlag, HistoryAuditRequest,
    InspectRequest, TransactionPlan,
};

pub const PROTOCOL_VERSION: u32 = 1;
const MAX_WORKER_REQUEST_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WorkerRequest {
    pub protocol_version: u32,
    pub operation_id: String,
    pub nonce: String,
    pub stage: String,
    pub codex_home: String,
    pub adapter_root: Option<String>,
    pub state_digest: String,
    #[serde(default)]
    pub payload: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerResult {
    pub protocol_version: u32,
    pub operation_id: String,
    pub nonce: String,
    pub state_digest: String,
    pub status: String,
    pub payload: Value,
    pub errors: Vec<InteropError>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InteropError {
    pub code: String,
    pub message: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RepairScopes {
    #[serde(default = "enabled")]
    history: bool,
    #[serde(default = "enabled")]
    continuation_compatibility: bool,
    #[serde(default = "enabled")]
    common_config: bool,
    #[serde(default = "enabled")]
    interoperability_settings: bool,
}

impl Default for RepairScopes {
    fn default() -> Self {
        Self {
            history: true,
            continuation_compatibility: true,
            common_config: true,
            interoperability_settings: true,
        }
    }
}

fn enabled() -> bool {
    true
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RepairPayload {
    source_bucket: String,
    target_bucket: String,
    #[serde(default)]
    scopes: RepairScopes,
    backup_root: Option<String>,
    paths: Option<Vec<std::path::PathBuf>>,
}

impl InteropError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }
}

impl std::fmt::Display for InteropError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for InteropError {}

pub fn run_worker(request: WorkerRequest) -> Result<WorkerResult, InteropError> {
    validate_request(&request)?;
    let payload = match request.stage.as_str() {
        "status" => json!({
            "runtime": "rust",
            "worker": "provider_interoperability",
            "protocolVersion": PROTOCOL_VERSION,
        }),
        "inspect" => serde_json::to_value(inspect_environment(InspectRequest::new(
            &request.codex_home,
            request.adapter_root.as_deref().map(std::path::Path::new),
        ))?)
        .map_err(|_| {
            InteropError::new(
                "worker_serialization_failed",
                "provider inspection result cannot be serialized",
            )
        })?,
        "audit_history" => {
            #[derive(Deserialize)]
            #[serde(rename_all = "camelCase")]
            struct AuditPayload {
                source_bucket: String,
                target_bucket: String,
                #[serde(default)]
                prepare_temporary_rewrites: bool,
                #[serde(default = "enabled")]
                continuation_compatibility: bool,
                #[serde(default = "enabled")]
                provider_rewrites: bool,
                paths: Option<Vec<std::path::PathBuf>>,
            }
            let payload: AuditPayload =
                serde_json::from_value(request.payload.clone()).map_err(|_| {
                    InteropError::new(
                        "invalid_history_audit_request",
                        "history audit payload is invalid",
                    )
                })?;
            let mut audit_request = HistoryAuditRequest::new(
                &request.codex_home,
                payload.source_bucket,
                payload.target_bucket,
                &request.operation_id,
            )
            .with_provider_rewrites(payload.provider_rewrites)
            .with_continuation_rewrites(payload.continuation_compatibility)
            .with_temporary_rewrites(payload.prepare_temporary_rewrites);
            if let Some(paths) = payload.paths {
                audit_request = audit_request.with_selected_paths(paths);
            }
            serde_json::to_value(audit_history(audit_request, CancellationFlag::new())?).map_err(
                |_| {
                    InteropError::new(
                        "worker_serialization_failed",
                        "history audit result cannot be serialized",
                    )
                },
            )?
        }
        "audit_continuation" => {
            #[derive(Default, Deserialize)]
            #[serde(rename_all = "camelCase")]
            struct ContinuationPayload {
                paths: Option<Vec<std::path::PathBuf>>,
            }
            let payload: ContinuationPayload = serde_json::from_value(request.payload.clone())
                .map_err(|_| {
                    InteropError::new(
                        "invalid_continuation_audit_request",
                        "continuation audit payload is invalid",
                    )
                })?;
            serde_json::to_value(audit_continuation_paths(
                &request.codex_home,
                payload.paths,
                CancellationFlag::new(),
            )?)
            .map_err(|_| {
                InteropError::new(
                    "worker_serialization_failed",
                    "continuation inventory cannot be serialized",
                )
            })?
        }
        "plan_repair" => {
            let payload: RepairPayload =
                serde_json::from_value(request.payload.clone()).map_err(|_| {
                    InteropError::new("invalid_transaction_plan", "repair plan payload is invalid")
                })?;
            validate_scopes(payload.scopes)?;
            let status = inspect_environment(InspectRequest::new(
                &request.codex_home,
                request.adapter_root.as_deref().map(std::path::Path::new),
            ))?;
            let (history, effective_history_rewrite, inventory) =
                audit_for_repair(&request, &payload, false)?;
            let adapter = plan_adapter(
                &request,
                payload.scopes.common_config,
                payload.scopes.interoperability_settings,
            )?;
            json!({
                "status": status,
                "history": history,
                "adapterRepair": adapter.as_ref().map(|plan| plan.summary()),
                "scopes": payload.scopes,
                "effectiveHistoryRewrite": effective_history_rewrite,
                "continuationInventory": inventory,
            })
        }
        "apply_history" => {
            let payload: RepairPayload =
                serde_json::from_value(request.payload.clone()).map_err(|_| {
                    InteropError::new(
                        "invalid_transaction_plan",
                        "history apply payload is invalid",
                    )
                })?;
            validate_scopes(payload.scopes)?;
            let backup_root = payload.backup_root.as_deref().ok_or_else(|| {
                InteropError::new("invalid_transaction_plan", "repair backup root is required")
            })?;
            let (audit, effective_history_rewrite, _) = audit_for_repair(&request, &payload, true)?;
            let plan = TransactionPlan::history(
                &request.operation_id,
                &request.codex_home,
                payload.source_bucket,
                payload.target_bucket,
                audit,
            )
            .with_history_enabled(effective_history_rewrite)
            .with_continuation_enabled(payload.scopes.continuation_compatibility);
            let plan = if let Some(adapter) = plan_adapter(
                &request,
                payload.scopes.common_config,
                payload.scopes.interoperability_settings,
            )? {
                plan.with_adapter(adapter)
            } else {
                plan
            };
            serde_json::to_value(apply_transaction(
                &plan,
                std::path::Path::new(backup_root),
                None,
            )?)
            .map_err(|_| {
                InteropError::new(
                    "worker_serialization_failed",
                    "history transaction result cannot be serialized",
                )
            })?
        }
        "rollback_history" => {
            #[derive(Deserialize)]
            #[serde(rename_all = "camelCase")]
            struct RollbackPayload {
                manifest_path: String,
            }
            let payload: RollbackPayload = serde_json::from_value(request.payload.clone())
                .map_err(|_| {
                    InteropError::new(
                        "rollback_manifest_invalid",
                        "history rollback payload is invalid",
                    )
                })?;
            serde_json::to_value(rollback_transaction(std::path::Path::new(
                &payload.manifest_path,
            ))?)
            .map_err(|_| {
                InteropError::new(
                    "worker_serialization_failed",
                    "history rollback result cannot be serialized",
                )
            })?
        }
        _ => {
            return Err(InteropError::new(
                "unsupported_worker_stage",
                "the requested provider interoperability worker stage is not supported",
            ));
        }
    };
    Ok(WorkerResult {
        protocol_version: PROTOCOL_VERSION,
        operation_id: request.operation_id,
        nonce: request.nonce,
        state_digest: request.state_digest,
        status: "ok".to_string(),
        payload: redact_json(&payload),
        errors: Vec::new(),
    })
}

fn audit_for_repair(
    request: &WorkerRequest,
    payload: &RepairPayload,
    prepare_temporary_rewrites: bool,
) -> Result<
    (
        super::HistoryAudit,
        bool,
        Option<super::ContinuationInventory>,
    ),
    InteropError,
> {
    let source_rows = if payload.scopes.history {
        count_provider_rows(
            std::path::Path::new(&request.codex_home)
                .join("state_5.sqlite")
                .as_path(),
            &payload.source_bucket,
        )?
    } else {
        0
    };
    let effective_history_rewrite = payload.scopes.history && source_rows > 0;
    let inventory = if payload.paths.is_none()
        && payload.scopes.continuation_compatibility
        && !effective_history_rewrite
    {
        Some(audit_continuation_candidates(
            &request.codex_home,
            CancellationFlag::new(),
        )?)
    } else {
        None
    };
    let selected_paths = payload.paths.clone().or_else(|| {
        inventory.as_ref().map(|inventory| {
            inventory
                .candidates
                .iter()
                .map(|candidate| std::path::PathBuf::from(&candidate.path))
                .collect::<Vec<_>>()
        })
    });
    let mut audit_request = HistoryAuditRequest::new(
        &request.codex_home,
        &payload.source_bucket,
        &payload.target_bucket,
        &request.operation_id,
    )
    .with_provider_rewrites(effective_history_rewrite)
    .with_continuation_rewrites(payload.scopes.continuation_compatibility)
    .with_temporary_rewrites(prepare_temporary_rewrites);
    if let Some(paths) = selected_paths {
        audit_request = audit_request.with_selected_paths(paths);
    }
    let audit = audit_history(audit_request, CancellationFlag::new())?;
    Ok((audit, effective_history_rewrite, inventory))
}

fn validate_scopes(scopes: RepairScopes) -> Result<(), InteropError> {
    if !scopes.history
        && !scopes.continuation_compatibility
        && !scopes.common_config
        && !scopes.interoperability_settings
    {
        return Err(InteropError::new(
            "empty_repair_scope",
            "at least one provider interoperability repair scope is required",
        ));
    }
    Ok(())
}

fn plan_adapter(
    request: &WorkerRequest,
    common_config: bool,
    interoperability_settings: bool,
) -> Result<Option<cc_switch::AdapterRepairPlan>, InteropError> {
    if !common_config && !interoperability_settings {
        return Ok(None);
    }
    let root = request.adapter_root.as_deref().ok_or_else(|| {
        InteropError::new(
            "adapter_not_found",
            "CC Switch state root is required for shared configuration repair",
        )
    })?;
    cc_switch::plan_repair(
        std::path::Path::new(root),
        std::path::Path::new(&request.codex_home),
        common_config,
        interoperability_settings,
    )
    .map(Some)
}

fn validate_request(request: &WorkerRequest) -> Result<(), InteropError> {
    if request.protocol_version != PROTOCOL_VERSION {
        return Err(InteropError::new(
            "unsupported_worker_protocol",
            "the provider interoperability worker protocol version is not supported",
        ));
    }
    for (name, value) in [
        ("operationId", request.operation_id.as_str()),
        ("nonce", request.nonce.as_str()),
        ("codexHome", request.codex_home.as_str()),
        ("stateDigest", request.state_digest.as_str()),
    ] {
        if value.trim().is_empty() {
            return Err(InteropError::new(
                "invalid_worker_request",
                format!("{name} must be a non-empty string"),
            ));
        }
    }
    Ok(())
}

pub fn run_worker_stdio() -> i32 {
    match read_worker_request().and_then(run_worker) {
        Ok(result) => write_json_result(&result, 0),
        Err(error) => {
            let public_error = json!({
                "protocolVersion": PROTOCOL_VERSION,
                "status": "error",
                "errors": [{
                    "code": error.code,
                    "message": error.message,
                }],
            });
            write_json_result(&public_error, 2)
        }
    }
}

fn read_worker_request() -> Result<WorkerRequest, InteropError> {
    let mut bytes = Vec::new();
    io::stdin()
        .take(MAX_WORKER_REQUEST_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| InteropError::new("invalid_worker_request", "cannot read worker request"))?;
    if bytes.len() as u64 > MAX_WORKER_REQUEST_BYTES {
        return Err(InteropError::new(
            "worker_request_too_large",
            "worker request exceeds the maximum size",
        ));
    }
    serde_json::from_slice(&bytes).map_err(|_| {
        InteropError::new("invalid_worker_request", "worker request is not valid JSON")
    })
}

fn write_json_result(value: &impl Serialize, exit_code: i32) -> i32 {
    let output = match serde_json::to_vec(&redact_serializable(value)) {
        Ok(output) => output,
        Err(_) => return 3,
    };
    let mut stdout = io::stdout().lock();
    if stdout.write_all(&output).is_err()
        || stdout.write_all(b"\n").is_err()
        || stdout.flush().is_err()
    {
        return 3;
    }
    exit_code
}

fn redact_serializable(value: &impl Serialize) -> Value {
    serde_json::to_value(value)
        .map(|value| redact_json(&value))
        .unwrap_or_else(
            |_| json!({"status": "error", "errors": [{"code": "worker_serialization_failed"}]}),
        )
}
