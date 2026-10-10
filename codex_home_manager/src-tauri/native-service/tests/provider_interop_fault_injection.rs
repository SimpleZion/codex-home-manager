use std::fs;

use codex_home_manager_service::provider_interop::{
    apply_transaction, audit_history, CancellationFlag, FaultPoint, HistoryAuditRequest,
    TransactionPlan,
};
use rusqlite::Connection;

#[test]
fn every_injected_mutation_failure_restores_preimages() {
    for (index, fault) in [
        FaultPoint::AfterBackup,
        FaultPoint::AfterJsonl,
        FaultPoint::AfterSqlite,
    ]
    .into_iter()
    .enumerate()
    {
        let home = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let sessions = home.path().join("sessions");
        fs::create_dir_all(&sessions).unwrap();
        let rollout = sessions.join("rollout.jsonl");
        let original = b"{\"type\":\"session_meta\",\"payload\":{\"id\":\"t\",\"model_provider\":\"openai\"}}\n";
        fs::write(&rollout, original).unwrap();
        let database = home.path().join("state_5.sqlite");
        let connection = Connection::open(&database).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL);\n\
                 INSERT INTO threads VALUES ('t', 'openai');",
            )
            .unwrap();
        drop(connection);
        let operation = format!("fault-{index}");
        let audit = audit_history(
            HistoryAuditRequest::new(home.path(), "openai", "custom", &operation)
                .with_temporary_rewrites(true),
            CancellationFlag::new(),
        )
        .unwrap();
        let plan = TransactionPlan::history(&operation, home.path(), "openai", "custom", audit);

        let error = apply_transaction(&plan, backup.path(), Some(fault)).unwrap_err();
        assert_eq!(error.code, "failed_rolled_back");
        assert_eq!(fs::read(&rollout).unwrap(), original);
        let restored = Connection::open(&database).unwrap();
        let count: i64 = restored
            .query_row(
                "SELECT COUNT(*) FROM threads WHERE model_provider = 'openai'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
        let quick: String = restored
            .query_row("PRAGMA quick_check", [], |row| row.get(0))
            .unwrap();
        assert_eq!(quick, "ok");
    }
}
