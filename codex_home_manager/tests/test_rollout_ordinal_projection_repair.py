from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


def load_module() -> object:
    path = Path(__file__).resolve().parents[1] / "scripts" / "repair-rollout-ordinals-and-history-projection.py"
    specification = importlib.util.spec_from_file_location("rollout_ordinal_projection_repair", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


repair = load_module()


def create_database(path: Path, thread_id: str, duplicate_offset: int) -> None:
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
    connection.execute("insert into thread_history_projection_state values (?, ?, ?)", (thread_id, duplicate_offset, 3))
    connection.execute("insert into thread_turns values (?, 'turn-a', 1)", (thread_id,))
    connection.execute("insert into thread_items values (?, 'turn-a', 'item-a', 2)", (thread_id,))
    connection.commit()
    connection.close()


def create_rollout(path: Path, thread_id: str, *, valid_restart_duplicate: bool = True) -> int:
    records = [
        {"timestamp": "2026-01-01T00:00:00Z", "ordinal": 0, "type": "session_meta", "payload": {"id": thread_id}},
        {"timestamp": "2026-01-01T00:00:01Z", "ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-a"}},
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "keep exactly"}]},
        },
        {
            "timestamp": "2026-01-01T00:01:00Z",
            "ordinal": 2,
            "type": "event_msg",
            "payload": {"type": "thread_settings_applied" if valid_restart_duplicate else "token_count"},
        },
        {"timestamp": "2026-01-01T00:01:01Z", "ordinal": 3, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-b"}},
        {"timestamp": "2026-01-01T00:01:02Z", "ordinal": 4, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-b"}},
    ]
    encoded_lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for record in records]
    path.write_bytes(b"".join(encoded_lines))
    return sum(len(line) for line in encoded_lines[:3])


def test_repairs_only_restart_duplicate_ordinals_and_resets_projection(tmp_path: Path) -> None:
    thread_id = "thread-a"
    rollout = tmp_path / "rollout.jsonl"
    duplicate_offset = create_rollout(rollout, thread_id)
    database = tmp_path / "thread_history_1.sqlite"
    create_database(database, thread_id, duplicate_offset)
    before = repair.analyze_rollout(rollout, thread_id)

    result = repair.repair(rollout, database, thread_id, tmp_path / "backup", require_offline=False)

    after = repair.analyze_rollout(rollout, thread_id)
    assert before["normalizationAllowed"] is True
    assert after["ordinalAnomalies"] == []
    assert after["lastOrdinal"] == after["lineCount"] - 1
    assert after["userPromptSha256"] == before["userPromptSha256"]
    assert after["nonOrdinalSemanticSha256"] == before["nonOrdinalSemanticSha256"]
    connection = sqlite3.connect(database)
    for table in repair.PROJECTION_TABLES:
        assert connection.execute(f"select count(*) from {table} where thread_id=?", (thread_id,)).fetchone()[0] == 0
    connection.close()
    assert Path(result["backupRolloutPath"]).exists()
    assert Path(result["backupDatabasePath"]).exists()


def test_refuses_duplicate_ordinal_from_any_other_event(tmp_path: Path) -> None:
    thread_id = "thread-a"
    rollout = tmp_path / "rollout.jsonl"
    duplicate_offset = create_rollout(rollout, thread_id, valid_restart_duplicate=False)
    database = tmp_path / "thread_history_1.sqlite"
    create_database(database, thread_id, duplicate_offset)

    try:
        repair.repair(rollout, database, thread_id, tmp_path / "backup", require_offline=False)
    except RuntimeError as error:
        assert "not limited to duplicate thread_settings_applied" in str(error)
    else:
        raise AssertionError("repair must reject non-restart ordinal anomalies")
