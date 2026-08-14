from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .offline_repair_policy import assert_backup_path, assert_codex_offline


@dataclass(frozen=True)
class StaleOpenEdgesPreview:
    parent_thread_id: str
    inactive_since: int
    open_child_ids: tuple[str, ...]
    active_child_ids: tuple[str, ...]
    stale_child_ids: tuple[str, ...]
    open_edges_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "parentThreadId": self.parent_thread_id,
            "inactiveSince": self.inactive_since,
            "openChildIds": list(self.open_child_ids),
            "activeChildIds": list(self.active_child_ids),
            "staleChildIds": list(self.stale_child_ids),
            "openCount": len(self.open_child_ids),
            "activeCount": len(self.active_child_ids),
            "staleCount": len(self.stale_child_ids),
            "openEdgesSha256": self.open_edges_sha256,
        }


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _edge_fingerprint(child_ids: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for child_id in child_ids:
        encoded = child_id.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _open_child_ids(connection: sqlite3.Connection, parent_thread_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT child_thread_id
        FROM thread_spawn_edges
        WHERE parent_thread_id = ? AND status = 'open'
        ORDER BY child_thread_id
        """,
        (parent_thread_id,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _active_child_ids(
    logs_database_path: Path,
    child_ids: tuple[str, ...],
    inactive_since: int,
) -> tuple[str, ...]:
    if not child_ids:
        return ()
    placeholders = ",".join("?" for _ in child_ids)
    connection = _readonly_connection(logs_database_path)
    try:
        rows = connection.execute(
            f"""
            SELECT DISTINCT thread_id
            FROM logs
            WHERE thread_id IN ({placeholders}) AND ts >= ?
            ORDER BY thread_id
            """,
            (*child_ids, inactive_since),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)
    finally:
        connection.close()


def preview_stale_open_edges(
    state_database_path: Path,
    logs_database_path: Path,
    parent_thread_id: str,
    *,
    inactive_since: int,
) -> StaleOpenEdgesPreview:
    if not parent_thread_id.strip():
        raise ValueError("parent_thread_id must not be empty")
    if inactive_since < 0:
        raise ValueError("inactive_since must be non-negative")
    connection = _readonly_connection(state_database_path)
    try:
        parent_exists = connection.execute(
            "SELECT 1 FROM threads WHERE id = ?",
            (parent_thread_id,),
        ).fetchone()
        if parent_exists is None:
            raise RuntimeError(f"parent thread is not registered: {parent_thread_id}")
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_spawn_edges'"
        ).fetchone()
        if table_exists is None:
            raise RuntimeError("state database has no thread_spawn_edges table")
        open_child_ids = _open_child_ids(connection, parent_thread_id)
    finally:
        connection.close()
    active_child_ids = _active_child_ids(logs_database_path, open_child_ids, inactive_since)
    active_set = set(active_child_ids)
    stale_child_ids = tuple(child_id for child_id in open_child_ids if child_id not in active_set)
    return StaleOpenEdgesPreview(
        parent_thread_id=parent_thread_id,
        inactive_since=inactive_since,
        open_child_ids=open_child_ids,
        active_child_ids=active_child_ids,
        stale_child_ids=stale_child_ids,
        open_edges_sha256=_edge_fingerprint(open_child_ids),
    )


def _backup_state_database(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        quick_check = destination.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise RuntimeError(f"state database backup failed quick_check: {quick_check}")
    finally:
        destination.close()
        source.close()


def close_stale_open_edges(
    state_database_path: Path,
    logs_database_path: Path,
    backup_directory: Path,
    parent_thread_id: str,
    *,
    inactive_since: int,
    expected_open_count: int,
    expected_open_edges_sha256: str,
) -> dict[str, Any]:
    assert_codex_offline()
    assert_backup_path(backup_directory)
    expected_sha256 = expected_open_edges_sha256.casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected_open_edges_sha256 must be a SHA-256 hex digest")
    preview = preview_stale_open_edges(
        state_database_path,
        logs_database_path,
        parent_thread_id,
        inactive_since=inactive_since,
    )
    if preview.active_child_ids:
        raise RuntimeError(
            "refusing to close open subagent edges with recent activity: "
            + ", ".join(preview.active_child_ids)
        )
    if len(preview.open_child_ids) != expected_open_count:
        raise RuntimeError(
            f"open edge count changed: expected {expected_open_count}, got {len(preview.open_child_ids)}"
        )
    if preview.open_edges_sha256 != expected_sha256:
        raise RuntimeError("open edge identity changed after preview")
    if expected_open_count == 0:
        raise RuntimeError("there are no stale open edges to close")

    backup_directory.mkdir(parents=True, exist_ok=False)
    database_backup_path = backup_directory / "state_5.sqlite.before"
    _backup_state_database(state_database_path, database_backup_path)

    connection = sqlite3.connect(state_database_path)
    quick_check = ""
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_child_ids = _open_child_ids(connection, parent_thread_id)
        if len(current_child_ids) != expected_open_count:
            raise RuntimeError("open edge count changed before commit")
        if _edge_fingerprint(current_child_ids) != expected_sha256:
            raise RuntimeError("open edge identity changed before commit")
        active_child_ids = _active_child_ids(
            logs_database_path,
            current_child_ids,
            inactive_since,
        )
        if active_child_ids:
            raise RuntimeError(
                "refusing to close open subagent edges with recent activity: "
                + ", ".join(active_child_ids)
            )
        cursor = connection.execute(
            """
            UPDATE thread_spawn_edges
            SET status = 'closed'
            WHERE parent_thread_id = ? AND status = 'open'
            """,
            (parent_thread_id,),
        )
        if cursor.rowcount != expected_open_count:
            raise RuntimeError(
                f"closed edge count mismatch: expected {expected_open_count}, got {cursor.rowcount}"
            )
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise RuntimeError(f"state database quick_check failed after repair: {quick_check}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    postcheck = preview_stale_open_edges(
        state_database_path,
        logs_database_path,
        parent_thread_id,
        inactive_since=inactive_since,
    )
    if postcheck.open_child_ids:
        raise RuntimeError(f"open edges remain after repair: {postcheck.open_child_ids}")

    result = {
        "state": "complete",
        "parentThreadId": parent_thread_id,
        "closedCount": expected_open_count,
        "closedChildIds": list(preview.open_child_ids),
        "openEdgesSha256": expected_sha256,
        "inactiveSince": inactive_since,
        "quickCheck": quick_check,
        "databaseBackupPath": str(database_backup_path),
        "completedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
    }
    manifest_path = backup_directory / "repair-result.json"
    manifest_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    result["manifestPath"] = str(manifest_path)
    return result
