mod config;
mod environment;
mod history;
mod protocol;
mod redaction;
mod sqlite;
mod transaction;

pub mod adapters;

pub use adapters::{
    AdapterInspection, AdapterSupport, AuthenticationSummary, ExternalAdapterSummary,
    InteroperabilitySettings, Presence, RouteClassification, RouteConfidence, RouteSummary,
};
pub use config::{
    classify_config, plan_common_merge, ConfigClass, ConfigClassification, ConfigDecision,
    ConfigMergePlan, ProviderSnapshot,
};
pub use environment::{inspect_environment, InspectRequest, ResolvedEnvironment};
pub use history::{
    audit_continuation_candidates, audit_continuation_paths, audit_history, CancellationFlag,
    ContinuationCandidate, ContinuationInventory, HistoryAudit, HistoryAuditRequest,
    HistoryFilePlan, PromptFingerprint,
};
pub use protocol::{
    run_worker, run_worker_stdio, InteropError, WorkerRequest, WorkerResult, PROTOCOL_VERSION,
};
pub use redaction::{contains_secret, redact_json};
pub use transaction::{
    apply_transaction, rollback_transaction, FaultPoint, RollbackResult, TransactionPlan,
    TransactionResult,
};
