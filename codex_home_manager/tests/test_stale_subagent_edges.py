from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend import stale_subagent_edges


def create_state_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO threads (id, updated_at) VALUES (?, ?)",
            [
                ("parent-target", 10),
                ("child-stale-a", 20),
                ("child-stale-b", 30),
                ("child-active", 40),
                ("parent-other", 50),
                ("child-other", 60),
            ],
        )
        connection.executemany(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES (?, ?, ?)",
            [
                ("parent-target", "child-stale-a", "open"),
                ("parent-target", "child-stale-b", "open"),
                ("parent-target", "child-active", "open"),
                ("parent-target", "child-already-closed", "closed"),
                ("parent-other", "child-other", "open"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def create_logs_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, thread_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO logs (ts, thread_id) VALUES (?, ?)",
            [
                (99, "child-stale-a"),
                (100, "child-active"),
                (150, "child-active"),
                (200, "unrelated-thread"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_preview_separates_stale_open_edges_from_children_active_since_cutoff(tmp_path: Path) -> None:
    state_path = tmp_path / "state_5.sqlite"
    logs_path = tmp_path / "logs_2.sqlite"
    create_state_database(state_path)
    create_logs_database(logs_path)

    preview = stale_subagent_edges.preview_stale_open_edges(
        state_path,
        logs_path,
        "parent-target",
        inactive_since=100,
    )

    assert preview.open_child_ids == (
        "child-active",
        "child-stale-a",
        "child-stale-b",
    )
    assert preview.active_child_ids == ("child-active",)
    assert preview.stale_child_ids == ("child-stale-a", "child-stale-b")
    assert len(preview.open_edges_sha256) == 64


def test_close_stale_open_edges_blocks_when_any_open_child_has_recent_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state_5.sqlite"
    logs_path = tmp_path / "logs_2.sqlite"
    create_state_database(state_path)
    create_logs_database(logs_path)
    monkeypatch.setattr(stale_subagent_edges, "assert_codex_offline", lambda: None)
    monkeypatch.setattr(stale_subagent_edges, "assert_backup_path", lambda _path: None)
    preview = stale_subagent_edges.preview_stale_open_edges(
        state_path,
        logs_path,
        "parent-target",
        inactive_since=100,
    )

    with pytest.raises(RuntimeError, match="recent activity"):
        stale_subagent_edges.close_stale_open_edges(
            state_path,
            logs_path,
            tmp_path / "backup",
            "parent-target",
            inactive_since=100,
            expected_open_count=len(preview.open_child_ids),
            expected_open_edges_sha256=preview.open_edges_sha256,
        )


def test_close_stale_open_edges_preserves_threads_and_other_parent_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state_5.sqlite"
    logs_path = tmp_path / "logs_2.sqlite"
    create_state_database(state_path)
    create_logs_database(logs_path)
    monkeypatch.setattr(stale_subagent_edges, "assert_codex_offline", lambda: None)
    monkeypatch.setattr(stale_subagent_edges, "assert_backup_path", lambda _path: None)
    preview = stale_subagent_edges.preview_stale_open_edges(
        state_path,
        logs_path,
        "parent-target",
        inactive_since=201,
    )

    result = stale_subagent_edges.close_stale_open_edges(
        state_path,
        logs_path,
        tmp_path / "backup",
        "parent-target",
        inactive_since=201,
        expected_open_count=3,
        expected_open_edges_sha256=preview.open_edges_sha256,
    )

    assert result["closedCount"] == 3
    assert result["quickCheck"] == "ok"
    assert Path(result["databaseBackupPath"]).is_file()
    connection = sqlite3.connect(state_path)
    backup_connection = sqlite3.connect(result["databaseBackupPath"])
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM threads"
        ).fetchone() == (6,)
        assert connection.execute(
            "SELECT status FROM thread_spawn_edges WHERE child_thread_id='child-stale-a'"
        ).fetchone() == ("closed",)
        assert connection.execute(
            "SELECT status FROM thread_spawn_edges WHERE child_thread_id='child-already-closed'"
        ).fetchone() == ("closed",)
        assert connection.execute(
            "SELECT status FROM thread_spawn_edges WHERE child_thread_id='child-other'"
        ).fetchone() == ("open",)
        assert backup_connection.execute(
            "SELECT status FROM thread_spawn_edges WHERE child_thread_id='child-stale-a'"
        ).fetchone() == ("open",)
    finally:
        connection.close()
        backup_connection.close()
