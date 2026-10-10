use codex_home_manager_service::provider_interop::{
    contains_secret, redact_json, run_worker, WorkerRequest, PROTOCOL_VERSION,
};
use serde_json::json;

fn fixture_request(stage: &str) -> WorkerRequest {
    WorkerRequest {
        protocol_version: PROTOCOL_VERSION,
        operation_id: "operation-1".to_string(),
        nonce: "nonce-1".to_string(),
        stage: stage.to_string(),
        codex_home: r"C:\Users\fixture\.codex".to_string(),
        adapter_root: None,
        state_digest: "digest-1".to_string(),
        payload: json!({"probe": true}),
    }
}

#[test]
fn worker_result_echoes_every_binding() {
    let request = fixture_request("status");
    let result = run_worker(request.clone()).expect("status request should succeed");

    assert_eq!(result.protocol_version, request.protocol_version);
    assert_eq!(result.operation_id, request.operation_id);
    assert_eq!(result.nonce, request.nonce);
    assert_eq!(result.state_digest, request.state_digest);
    assert_eq!(result.status, "ok");
    assert!(result.errors.is_empty());
}

#[test]
fn redaction_removes_nested_tokens_and_authorization_headers() {
    let value = json!({
        "auth": {"experimental_bearer_token": "fixture-secret-token"},
        "headers": {"Authorization": "Bearer fixture-secret-header"},
        "safe": "visible"
    });

    assert!(contains_secret(&value));
    let redacted = redact_json(&value);
    let serialized = serde_json::to_string(&redacted).expect("redacted JSON should serialize");
    assert!(!serialized.contains("fixture-secret"));
    assert!(serialized.contains("visible"));
    assert!(serialized.contains("[REDACTED]"));
    assert!(!contains_secret(&redacted));
}

#[test]
fn unsupported_protocol_version_fails_closed() {
    let mut request = fixture_request("status");
    request.protocol_version += 1;

    let error = run_worker(request).expect_err("unsupported protocol must fail");
    assert_eq!(error.code, "unsupported_worker_protocol");
}

#[test]
fn unknown_stage_fails_closed() {
    let error = run_worker(fixture_request("unknown")).expect_err("unknown stages must fail");
    assert_eq!(error.code, "unsupported_worker_stage");
}

#[test]
fn worker_history_audit_enables_cross_provider_continuation_analysis() {
    let home = tempfile::tempdir().unwrap();
    let sessions = home.path().join("sessions");
    std::fs::create_dir_all(&sessions).unwrap();
    std::fs::write(
        sessions.join("rollout.jsonl"),
        concat!(
            "{\"type\":\"session_meta\",\"payload\":{\"id\":\"t\",\"model_provider\":\"custom\"}}\n",
            "{\"type\":\"response_item\",\"payload\":{\"type\":\"reasoning\",\"id\":\"item_proxy\",\"encrypted_content\":\"opaque\",\"summary\":[]}}\n"
        ),
    )
    .unwrap();
    let mut request = fixture_request("audit_history");
    request.codex_home = home.path().to_string_lossy().into_owned();
    request.payload = json!({
        "sourceBucket": "openai",
        "targetBucket": "custom",
        "continuationCompatibility": true
    });

    let result = run_worker(request).unwrap();

    assert_eq!(result.payload["nonportableReasoningCount"], 1);
    assert_eq!(result.payload["continuationChangedFileCount"], 1);
}
