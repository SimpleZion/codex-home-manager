use std::path::Path;

use rusqlite::{backup::Backup, params, Connection, OpenFlags, TransactionBehavior};

use super::InteropError;

pub fn backup_database(source: &Path, target: &Path) -> Result<(), InteropError> {
    let source_connection = Connection::open_with_flags(
        source,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| InteropError::new("sqlite_backup_failed", "source database cannot be opened"))?;
    let mut target_connection = Connection::open(target).map_err(|_| {
        InteropError::new("sqlite_backup_failed", "backup database cannot be created")
    })?;
    let backup = Backup::new(&source_connection, &mut target_connection).map_err(|_| {
        InteropError::new(
            "sqlite_backup_failed",
            "SQLite backup cannot be initialized",
        )
    })?;
    backup
        .run_to_completion(128, std::time::Duration::from_millis(5), None)
        .map_err(|_| {
            InteropError::new("sqlite_backup_failed", "SQLite backup cannot be completed")
        })?;
    drop(backup);
    quick_check_connection(&target_connection)?;
    drop(target_connection);
    drop(source_connection);
    Ok(())
}

pub fn migrate_provider(database: &Path, source: &str, target: &str) -> Result<u64, InteropError> {
    let mut connection = Connection::open(database).map_err(|_| {
        InteropError::new(
            "sqlite_write_failed",
            "Codex state database cannot be opened",
        )
    })?;
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| {
            InteropError::new(
                "sqlite_write_failed",
                "Codex state transaction cannot start",
            )
        })?;
    let count: i64 = transaction
        .query_row(
            "SELECT COUNT(*) FROM threads WHERE model_provider = ?1",
            [source],
            |row| row.get(0),
        )
        .map_err(|_| {
            InteropError::new(
                "sqlite_write_failed",
                "eligible Codex rows cannot be counted",
            )
        })?;
    let changed = transaction
        .execute(
            "UPDATE threads SET model_provider = ?1 WHERE model_provider = ?2",
            params![target, source],
        )
        .map_err(|_| {
            InteropError::new(
                "sqlite_write_failed",
                "Codex provider rows cannot be updated",
            )
        })? as i64;
    if changed != count {
        return Err(InteropError::new(
            "sqlite_row_count_mismatch",
            "eligible and changed Codex row counts differ",
        ));
    }
    transaction.commit().map_err(|_| {
        InteropError::new(
            "sqlite_write_failed",
            "Codex state transaction cannot commit",
        )
    })?;
    quick_check_connection(&connection)?;
    drop(connection);
    Ok(changed.max(0) as u64)
}

pub fn count_provider_rows(database: &Path, provider: &str) -> Result<u64, InteropError> {
    let connection = Connection::open_with_flags(
        database,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| {
        InteropError::new(
            "sqlite_read_failed",
            "Codex state database cannot be opened read-only",
        )
    })?;
    let count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM threads WHERE model_provider = ?1",
            [provider],
            |row| row.get(0),
        )
        .map_err(|_| {
            InteropError::new(
                "sqlite_read_failed",
                "Codex provider rows cannot be counted",
            )
        })?;
    Ok(count.max(0) as u64)
}

pub fn update_common_config(database: &Path, rendered_common: &str) -> Result<bool, InteropError> {
    let mut connection = Connection::open(database).map_err(|_| {
        InteropError::new(
            "adapter_write_failed",
            "CC Switch database cannot be opened",
        )
    })?;
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| {
            InteropError::new("adapter_write_failed", "CC Switch transaction cannot start")
        })?;
    let existing: Option<String> = transaction
        .query_row(
            "SELECT value FROM settings WHERE key = 'common_config_codex'",
            [],
            |row| row.get(0),
        )
        .ok();
    let changed = existing.as_deref() != Some(rendered_common);
    if changed {
        transaction
            .execute(
                "INSERT INTO settings (key, value) VALUES ('common_config_codex', ?1) \
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [rendered_common],
            )
            .map_err(|_| {
                InteropError::new(
                    "adapter_write_failed",
                    "CC Switch common configuration cannot be updated",
                )
            })?;
    }
    transaction.commit().map_err(|_| {
        InteropError::new(
            "adapter_write_failed",
            "CC Switch transaction cannot commit",
        )
    })?;
    quick_check_connection(&connection)?;
    Ok(changed)
}

pub fn read_common_config(database: &Path) -> Result<String, InteropError> {
    let connection = Connection::open_with_flags(
        database,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| {
        InteropError::new(
            "adapter_database_unreadable",
            "CC Switch database cannot be opened",
        )
    })?;
    Ok(connection
        .query_row(
            "SELECT value FROM settings WHERE key = 'common_config_codex'",
            [],
            |row| row.get(0),
        )
        .unwrap_or_default())
}

pub fn quick_check(path: &Path) -> Result<(), InteropError> {
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| {
        InteropError::new(
            "sqlite_integrity_failed",
            "database cannot be opened for quick check",
        )
    })?;
    quick_check_connection(&connection)
}

fn quick_check_connection(connection: &Connection) -> Result<(), InteropError> {
    let result: String = connection
        .query_row("PRAGMA quick_check", [], |row| row.get(0))
        .map_err(|_| {
            InteropError::new("sqlite_integrity_failed", "database quick check cannot run")
        })?;
    if result != "ok" {
        return Err(InteropError::new(
            "sqlite_integrity_failed",
            "database quick check did not return ok",
        ));
    }
    Ok(())
}
