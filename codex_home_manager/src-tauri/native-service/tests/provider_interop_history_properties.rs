use std::fs;

use codex_home_manager_service::provider_interop::{
    audit_history, CancellationFlag, HistoryAuditRequest,
};

#[test]
fn formatting_variants_change_only_the_provider_value() {
    let separators = [(":", ""), (" : ", " "), ("\t:\t", "")];
    let endings = ["\n", "\r\n"];
    for (case_index, ((separator, after_comma), ending)) in separators
        .into_iter()
        .flat_map(|separator| endings.into_iter().map(move |ending| (separator, ending)))
        .enumerate()
    {
        let home = tempfile::tempdir().unwrap();
        let directory = home.path().join("sessions");
        fs::create_dir_all(&directory).unwrap();
        let source = format!(
            "{{\"payload\"{separator}{{\"id\"{separator}\"t\",{after_comma}\"model_provider\"{separator}\"openai\"}},{after_comma}\"type\"{separator}\"session_meta\"}}{ending}"
        );
        fs::write(directory.join("rollout.jsonl"), source.as_bytes()).unwrap();
        let audit = audit_history(
            HistoryAuditRequest::new(
                home.path(),
                "openai",
                "custom",
                format!("property-{case_index}"),
            )
            .with_temporary_rewrites(true),
            CancellationFlag::new(),
        )
        .unwrap();
        let output = fs::read(audit.files[0].temporary_path.as_ref().unwrap()).unwrap();
        assert_eq!(source.len(), output.len());
        let restored = String::from_utf8(output)
            .unwrap()
            .replacen("\"custom\"", "\"openai\"", 1);
        assert_eq!(restored.as_bytes(), source.as_bytes());
    }
}
