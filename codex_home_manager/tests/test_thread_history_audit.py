from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

import audit_codex_thread_histories
from audit_codex_thread_histories import audit_threads


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths are platform-specific")
def test_audit_deduplicates_extended_length_and_normal_rollout_paths(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "sessions" / "rollout-thread-a.jsonl"
    rollout_path.parent.mkdir()
    rollout_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "thread-a"}}),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_image", "image_url": {"invalid": True}}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    extended_path = "\\\\?\\" + str(rollout_path)
    database = sqlite3.connect(state_path)
    try:
        database.execute(
            """
            create table threads(
                id text, rollout_path text, created_at integer, updated_at integer,
                source text, cwd text, title text, archived integer, cli_version text,
                agent_nickname text, agent_role text, thread_source text, history_mode text
            )
            """
        )
        database.executemany(
            "insert into threads values (?, ?, 0, 0, '', '', '', 0, '', '', '', '', '')",
            [("thread-a", str(rollout_path)), ("thread-b", extended_path)],
        )
        database.commit()
    finally:
        database.close()

    report = audit_threads(state_path, tmp_path / "audit.json")

    assert report["schema_version"] == 5
    assert report["policy"]["version"] == 2
    assert report["state"]["path"] == str(state_path.resolve())
    assert report["state"]["sha256"]
    assert set(report["state"]["files"]) == {"main", "wal", "shm", "journal"}
    assert report["state"]["read_snapshot"]["quick_check"] == "ok"
    assert Path(report["state"]["read_snapshot"]["path"]).is_file()
    assert report["policy"]["thresholds"] == {
        "max_active_bytes": 100 * 1024 * 1024,
        "max_active_lines": 50_000,
        "max_tail_lines": 20_000,
    }
    assert report["summary"]["unique_rollout_count"] == 1
    assert report["summary"]["shared_rollout_mapping_count"] == 1
    assert report["summary"]["parser_error_breakdown"]["invalid_image_url"] == 2
    assert report["summary"]["estimated_current_parser_errors"] == 2
    assert report["summary"]["json_parse_error_count"] == 0
    assert report["threads"][0]["audited_sha256"]
    assert report["threads"][1]["audited_sha256"] == report["threads"][0]["audited_sha256"]


def test_audit_snapshot_includes_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    rollout_path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-a"}}) + "\n")
    database = sqlite3.connect(state_path)
    try:
        database.execute("pragma journal_mode=wal")
        database.execute("pragma wal_autocheckpoint=0")
        database.execute(
            """
            create table threads(
                id text, rollout_path text, created_at integer, updated_at integer,
                source text, cwd text, title text, archived integer, cli_version text,
                agent_nickname text, agent_role text, thread_source text, history_mode text
            )
            """
        )
        database.execute(
            "insert into threads values (?, ?, 0, 0, '', '', '', 0, '', '', '', '', '')",
            ("thread-a", str(rollout_path)),
        )
        database.commit()
        assert Path(str(state_path) + "-wal").is_file()

        report = audit_threads(state_path, tmp_path / "audit.json")
    finally:
        database.close()

    assert report["summary"]["database_thread_count"] == 1
    assert report["threads"][0]["id"] == "thread-a"


def test_audit_snapshot_allows_transient_shm_contract_change() -> None:
    before = {
        "main": {"exists": True, "sha256": "main"},
        "wal": {"exists": True, "sha256": "wal"},
        "shm": {"exists": True, "sha256": "before"},
        "journal": {"exists": False},
    }
    after = {**before, "shm": {"exists": True, "sha256": "after"}}

    audit_codex_thread_histories.assert_stable_source_database_contracts(before, after)


def test_audit_snapshot_rejects_durable_sidecar_change() -> None:
    before = {
        "main": {"exists": True, "sha256": "main"},
        "wal": {"exists": True, "sha256": "before"},
        "shm": {"exists": True, "sha256": "shm"},
        "journal": {"exists": False},
    }
    after = {**before, "wal": {"exists": True, "sha256": "after"}}

    with pytest.raises(RuntimeError, match="durable sidecar changed"):
        audit_codex_thread_histories.assert_stable_source_database_contracts(before, after)


def test_audit_rejects_state_database_change_during_full_scan(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    rollout_path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-a"}}) + "\n")
    database = sqlite3.connect(state_path)
    try:
        database.execute(
            """
            create table threads(
                id text, rollout_path text, created_at integer, updated_at integer,
                source text, cwd text, title text, archived integer, cli_version text,
                agent_nickname text, agent_role text, thread_source text, history_mode text
            )
            """
        )
        database.execute(
            "insert into threads values (?, ?, 0, 0, '', '', '', 0, '', '', '', '', '')",
            ("thread-a", str(rollout_path)),
        )
        database.commit()
    finally:
        database.close()

    original_scan = audit_codex_thread_histories.scan_rollout

    def mutate_state_after_scan(path: Path):
        result = original_scan(path)
        with state_path.open("ab") as destination:
            destination.write(b"changed-during-audit")
        return result

    monkeypatch.setattr(audit_codex_thread_histories, "scan_rollout", mutate_state_after_scan)

    with pytest.raises(RuntimeError, match="changed during the full audit"):
        audit_threads(state_path, tmp_path / "audit.json")


def test_audit_rejects_sidecar_appearance_during_full_scan(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    rollout_path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-a"}}) + "\n")
    database = sqlite3.connect(state_path)
    try:
        database.execute(
            """
            create table threads(
                id text, rollout_path text, created_at integer, updated_at integer,
                source text, cwd text, title text, archived integer, cli_version text,
                agent_nickname text, agent_role text, thread_source text, history_mode text
            )
            """
        )
        database.execute(
            "insert into threads values (?, ?, 0, 0, '', '', '', 0, '', '', '', '', '')",
            ("thread-a", str(rollout_path)),
        )
        database.commit()
    finally:
        database.close()

    original_scan = audit_codex_thread_histories.scan_rollout

    def create_sidecar_after_scan(path: Path):
        result = original_scan(path)
        Path(str(state_path) + "-journal").write_bytes(b"appeared-during-audit")
        return result

    monkeypatch.setattr(audit_codex_thread_histories, "scan_rollout", create_sidecar_after_scan)

    with pytest.raises(RuntimeError, match="sidecar changed during the full audit"):
        audit_threads(state_path, tmp_path / "audit.json")
