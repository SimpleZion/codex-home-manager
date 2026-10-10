from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


def load_module() -> object:
    path = Path(__file__).resolve().parents[1] / "scripts" / "repair-thread-history-projection.py"
    spec = importlib.util.spec_from_file_location("thread_history_projection_repair", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repair = load_module()


def create_database(path: Path, thread_id: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        create table thread_history_projection_state (
            thread_id text primary key,
            next_rollout_byte_offset integer not null,
            next_rollout_ordinal integer not null
        );
        create table thread_turns (
            thread_id text not null,
            turn_id text not null,
            rollout_ordinal integer not null,
            primary key (thread_id, turn_id)
        );
        create table thread_items (
            thread_id text not null,
            turn_id text not null,
            item_id text not null,
            rollout_ordinal integer not null,
            primary key (thread_id, turn_id, item_id)
        );
        create table thread_realtime_items (
            thread_id text not null,
            item_id text not null,
            rollout_ordinal integer not null,
            primary key (thread_id, item_id)
        );
        """
    )
    for current_thread in (thread_id, "other-thread"):
        connection.execute(
            "insert into thread_history_projection_state values (?, ?, ?)",
            (current_thread, 7 if current_thread == thread_id else 0, 1),
        )
        connection.execute("insert into thread_turns values (?, ?, ?)", (current_thread, f"turn-{current_thread}", 1))
        connection.execute(
            "insert into thread_items values (?, ?, ?, ?)",
            (current_thread, f"turn-{current_thread}", f"item-{current_thread}", 1),
        )
    connection.commit()
    connection.close()


def create_rollout(path: Path, thread_id: str) -> None:
    records = [
        {"ordinal": 0, "type": "session_meta", "payload": {"id": thread_id}},
        {"ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-a"}},
        {
            "ordinal": 2,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "keep this exact prompt"}],
            },
        },
        {"ordinal": 3, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-a"}},
        {"ordinal": 4, "type": "user_message", "payload": {"content": "legacy prompt"}},
        {"ordinal": 5, "type": "event_msg", "payload": {"type": "user_message", "message": "event prompt"}},
    ]
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")


def test_repair_projection_removes_only_target_derived_rows_and_preserves_rollout(tmp_path: Path) -> None:
    thread_id = "thread-a"
    database = tmp_path / "thread_history_1.sqlite"
    rollout = tmp_path / "rollout.jsonl"
    backup_root = tmp_path / "backup"
    create_database(database, thread_id)
    create_rollout(rollout, thread_id)
    rollout_before = repair.scan_rollout(rollout, thread_id)
    assert rollout_before["userPromptCount"] == 3

    result = repair.repair_projection(
        database,
        rollout,
        thread_id,
        backup_root,
        require_offline=False,
    )

    connection = sqlite3.connect(database)
    for table in repair.PROJECTION_TABLES:
        assert connection.execute(f"select count(*) from {table} where thread_id=?", (thread_id,)).fetchone()[0] == 0
        assert connection.execute(f"select count(*) from {table} where thread_id='other-thread'").fetchone()[0] == 1
    assert connection.execute("pragma quick_check").fetchone()[0] == "ok"
    connection.close()
    assert repair.scan_rollout(rollout, thread_id) == rollout_before
    assert Path(result["backupDatabasePath"]).exists()
    assert Path(result["manifestPath"]).exists()


def test_repair_projection_refuses_line_aligned_consistent_cursor(tmp_path: Path) -> None:
    thread_id = "thread-a"
    database = tmp_path / "thread_history_1.sqlite"
    rollout = tmp_path / "rollout.jsonl"
    create_database(database, thread_id)
    create_rollout(rollout, thread_id)
    first_line_length = len(rollout.read_bytes().splitlines(keepends=True)[0])
    connection = sqlite3.connect(database)
    connection.execute(
        "update thread_history_projection_state set next_rollout_byte_offset=?, next_rollout_ordinal=1 where thread_id=?",
        (first_line_length, thread_id),
    )
    connection.commit()
    connection.close()

    try:
        repair.repair_projection(database, rollout, thread_id, tmp_path / "backup", require_offline=False)
    except RuntimeError as error:
        assert "not proven corrupt" in str(error)
    else:
        raise AssertionError("repair should refuse a consistent cursor")


def test_offline_process_probe_does_not_match_its_own_powershell_command() -> None:
    processes = repair.running_codex_processes()

    assert all(process["Name"].casefold() != "powershell.exe" for process in processes)
