use std::{fs, path::Path};

use codex_home_manager_service::provider_interop::{
    audit_continuation_candidates, audit_history, CancellationFlag, HistoryAuditRequest,
};

fn write_rollout(home: &Path, name: &str, bytes: &[u8]) -> std::path::PathBuf {
    let directory = home.join("sessions/2026/08/16");
    fs::create_dir_all(&directory).unwrap();
    let path = directory.join(name);
    fs::write(&path, bytes).unwrap();
    path
}

#[test]
fn audit_prepares_equal_length_rewrite_and_preserves_every_other_byte() {
    let home = tempfile::tempdir().unwrap();
    let source = concat!(
        "{\"type\":\"session_meta\",\"payload\":{\"id\":\"thread-1\",\"model_provider\":\"openai\"}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"keep openai in my prompt\"}]}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"reasoning\",\"encrypted_content\":\"ciphertext-openai\"}}\n"
    );
    let rollout = write_rollout(home.path(), "rollout-thread-1.jsonl", source.as_bytes());
    let request = HistoryAuditRequest::new(home.path(), "openai", "custom", "operation-1")
        .with_temporary_rewrites(true);

    let audit = audit_history(request, CancellationFlag::new()).unwrap();

    assert_eq!(audit.changed_file_count, 1);
    assert_eq!(audit.replacement_count, 1);
    assert_eq!(audit.prompt_fingerprint.count, 1);
    let plan = &audit.files[0];
    let temporary = plan.temporary_path.as_ref().unwrap();
    let output = fs::read(temporary).unwrap();
    let expected = source.replacen(
        "\"model_provider\":\"openai\"",
        "\"model_provider\":\"custom\"",
        1,
    );
    assert_eq!(output, expected.as_bytes());
    assert_eq!(fs::read(rollout).unwrap(), source.as_bytes());
    assert_eq!(
        plan.normalized_sha256,
        plan.output_sha256.as_deref().unwrap()
    );
    assert!(String::from_utf8(output)
        .unwrap()
        .contains("ciphertext-openai"));
}

#[test]
fn audit_supports_crlf_whitespace_and_field_order() {
    let home = tempfile::tempdir().unwrap();
    let source = b"{\"payload\" : { \"model_provider\" : \"openai\", \"id\":\"t\" }, \"type\" : \"session_meta\"}\r\n";
    write_rollout(home.path(), "rollout-t.jsonl", source);
    let audit = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", "operation-2")
            .with_temporary_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();

    let output = fs::read(audit.files[0].temporary_path.as_ref().unwrap()).unwrap();
    assert_eq!(output.len(), source.len());
    assert!(output.ends_with(b"\r\n"));
    assert!(String::from_utf8(output).unwrap().contains("\"custom\""));
}

#[test]
fn unknown_provider_bucket_is_reported_but_not_rewritten() {
    let home = tempfile::tempdir().unwrap();
    write_rollout(
        home.path(),
        "rollout-unknown.jsonl",
        b"{\"type\":\"session_meta\",\"payload\":{\"id\":\"t\",\"model_provider\":\"other\"}}\n",
    );
    let audit = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", "operation-3")
            .with_temporary_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();

    assert_eq!(audit.changed_file_count, 0);
    assert_eq!(audit.provider_buckets.get("other"), Some(&1));
    assert!(audit.files[0].temporary_path.is_none());
}

#[test]
fn unequal_length_provider_ids_are_rejected() {
    let home = tempfile::tempdir().unwrap();
    let error = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom-long", "operation-4"),
        CancellationFlag::new(),
    )
    .unwrap_err();
    assert_eq!(error.code, "unsupported_provider_substitution");
}

#[test]
fn cancellation_stops_before_rewrite() {
    let home = tempfile::tempdir().unwrap();
    write_rollout(
        home.path(),
        "rollout-cancel.jsonl",
        b"{\"type\":\"session_meta\",\"payload\":{\"model_provider\":\"openai\"}}\n",
    );
    let cancellation = CancellationFlag::new();
    cancellation.cancel();
    let error = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", "operation-5")
            .with_temporary_rewrites(true),
        cancellation,
    )
    .unwrap_err();
    assert_eq!(error.code, "operation_cancelled");
}

#[test]
fn continuation_rewrite_removes_foreign_reasoning_and_strips_type_invalid_ids() {
    let home = tempfile::tempdir().unwrap();
    let source = concat!(
        "{\"type\":\"session_meta\",\"payload\":{\"id\":\"thread-1\",\"model_provider\":\"custom\"}}\n",
        "{\"type\":\"event_msg\",\"payload\":{\"type\":\"user_message\",\"message\":\"keep my prompt\"}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"reasoning\",\"id\":\"item_proxy-reasoning\",\"encrypted_content\":\"opaque-ciphertext\",\"summary\":[]}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"assistant\",\"id\":\"item_proxy-message\",\"content\":[{\"type\":\"output_text\",\"text\":\"answer\"}]}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"function_call\",\"id\":\"item_proxy-call\",\"call_id\":\"call-stable\",\"name\":\"wait\",\"arguments\":\"{}\"}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"call-stable\",\"output\":\"done\"}}\n"
    );
    let rollout = write_rollout(home.path(), "rollout-thread-1.jsonl", source.as_bytes());
    let audit = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", "continuation-1")
            .with_continuation_rewrites(true)
            .with_temporary_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();

    assert_eq!(audit.continuation_changed_file_count, 1);
    assert_eq!(audit.nonportable_reasoning_count, 1);
    assert_eq!(audit.incompatible_item_id_count, 2);
    assert_eq!(audit.prompt_fingerprint.count, 1);
    let output = fs::read(audit.files[0].temporary_path.as_ref().unwrap()).unwrap();
    let output_text = String::from_utf8(output).unwrap();
    assert!(!output_text.contains("item_proxy"));
    assert!(!output_text.contains("opaque-ciphertext"));
    assert!(output_text.contains("\"call_id\":\"call-stable\""));
    assert!(output_text.contains("\"output\":\"done\""));
    assert!(String::from_utf8(fs::read(rollout).unwrap())
        .unwrap()
        .contains("opaque-ciphertext"));
}

#[test]
fn continuation_audit_reports_unknown_foreign_items_without_mutating_them() {
    let home = tempfile::tempdir().unwrap();
    let source = concat!(
        "{\"type\":\"session_meta\",\"payload\":{\"id\":\"thread-1\",\"model_provider\":\"custom\"}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"future_tool_call\",\"id\":\"item_unknown\"}}\n"
    );
    write_rollout(home.path(), "rollout-thread-1.jsonl", source.as_bytes());
    let audit = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", "continuation-unknown")
            .with_continuation_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();

    assert_eq!(audit.unsupported_foreign_item_count, 1);
    assert_eq!(audit.changed_file_count, 0);
    assert_eq!(audit.replacement_count, 0);
}

#[test]
fn continuation_rewrite_only_changes_latest_active_replay_context() {
    let home = tempfile::tempdir().unwrap();
    let source = concat!(
        "{\"type\":\"session_meta\",\"payload\":{\"id\":\"thread-1\",\"model_provider\":\"custom\"}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"reasoning\",\"id\":\"item_inactive-reasoning\",\"encrypted_content\":\"keep-inactive-ciphertext\",\"summary\":[]}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"future_tool_call\",\"id\":\"item_inactive-unknown\"}}\n",
        "{\"type\":\"compacted\",\"payload\":{\"replacement_history\":[{\"type\":\"message\",\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"keep compacted prompt\"}]},{\"type\":\"reasoning\",\"id\":\"item_active-reasoning\",\"encrypted_content\":\"drop-active-ciphertext\",\"summary\":[]},{\"type\":\"message\",\"role\":\"assistant\",\"id\":\"item_active-message\",\"content\":[{\"type\":\"output_text\",\"text\":\"keep compacted answer\"}]}]}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"function_call\",\"id\":\"item_active-call\",\"call_id\":\"call-active\",\"name\":\"wait\",\"arguments\":\"{}\"}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"call-active\",\"output\":\"done\"}}\n"
    );
    let rollout = write_rollout(home.path(), "rollout-thread-1.jsonl", source.as_bytes());
    let audit = audit_history(
        HistoryAuditRequest::new(home.path(), "openai", "custom", "continuation-active")
            .with_continuation_rewrites(true)
            .with_temporary_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();

    assert_eq!(audit.nonportable_reasoning_count, 1);
    assert_eq!(audit.incompatible_item_id_count, 2);
    assert_eq!(audit.unsupported_foreign_item_count, 0);
    let output = fs::read_to_string(audit.files[0].temporary_path.as_ref().unwrap()).unwrap();
    assert!(output.contains("item_inactive-reasoning"));
    assert!(output.contains("keep-inactive-ciphertext"));
    assert!(output.contains("item_inactive-unknown"));
    assert!(!output.contains("item_active-reasoning"));
    assert!(!output.contains("drop-active-ciphertext"));
    assert!(!output.contains("item_active-message"));
    assert!(!output.contains("item_active-call"));
    assert!(output.contains("\"call_id\":\"call-active\""));
    assert!(fs::read_to_string(rollout)
        .unwrap()
        .contains("drop-active-ciphertext"));
}

#[test]
fn continuation_audit_blocks_unknown_items_only_when_they_are_active() {
    let home = tempfile::tempdir().unwrap();
    let source = concat!(
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"future_tool_call\",\"id\":\"item_inactive\"}}\n",
        "{\"type\":\"compacted\",\"payload\":{\"replacement_history\":[{\"type\":\"message\",\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"summary\"}]}]}}\n",
        "{\"type\":\"response_item\",\"payload\":{\"type\":\"future_tool_call\",\"id\":\"item_active\"}}\n"
    );
    write_rollout(home.path(), "rollout-thread-1.jsonl", source.as_bytes());
    let audit = audit_history(
        HistoryAuditRequest::new(
            home.path(),
            "openai",
            "custom",
            "continuation-active-unknown",
        )
        .with_continuation_rewrites(true),
        CancellationFlag::new(),
    )
    .unwrap();

    assert_eq!(audit.unsupported_foreign_item_count, 1);
}

#[test]
fn continuation_inventory_reads_only_the_active_tail_and_returns_candidates() {
    let home = tempfile::tempdir().unwrap();
    let inactive_padding = "x".repeat(2 * 1024 * 1024);
    let source = format!(
        "{{\"type\":\"event_msg\",\"payload\":{{\"type\":\"agent_message\",\"message\":{inactive_padding:?}}}}}\n\
         {{\"type\":\"response_item\",\"payload\":{{\"type\":\"reasoning\",\"id\":\"item_inactive\",\"encrypted_content\":\"keep\",\"summary\":[]}}}}\n\
         {{\"type\":\"compacted\",\"payload\":{{\"replacement_history\":[{{\"type\":\"message\",\"role\":\"user\",\"content\":[{{\"type\":\"input_text\",\"text\":\"summary\"}}]}}]}}}}\n\
         {{\"type\":\"response_item\",\"payload\":{{\"type\":\"message\",\"role\":\"assistant\",\"id\":\"item_active\",\"content\":[{{\"type\":\"output_text\",\"text\":\"answer\"}}]}}}}\n"
    );
    write_rollout(home.path(), "rollout-thread-1.jsonl", source.as_bytes());
    let inventory = audit_continuation_candidates(home.path(), CancellationFlag::new()).unwrap();

    assert_eq!(inventory.scanned_file_count, 1);
    assert_eq!(inventory.candidate_file_count, 1);
    assert_eq!(inventory.incompatible_item_id_count, 1);
    assert_eq!(inventory.nonportable_reasoning_count, 0);
    assert!(inventory.active_tail_scanned_bytes < 2048);
    assert!(inventory.candidates[0].active_tail_offset > 2 * 1024 * 1024);
}
