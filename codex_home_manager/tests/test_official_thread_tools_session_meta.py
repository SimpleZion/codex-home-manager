from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from backend import official_thread_tools_session_meta as session_meta_module
from backend.official_thread_tools_session_meta import (
    codex_app_tool_names,
    dynamic_tools_protocol,
    initial_session_meta_records,
    latest_session_meta,
    official_tool_candidates,
    partition_candidates_by_rollout_identity,
    repair_official_thread_tool_session_meta,
    required_thread_tool_names,
    write_json_atomic,
)
from backend.thread_history_repair import scan_rollout


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def test_atomic_status_write_retries_windows_sharing_violation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    status_path = tmp_path / "status.json"
    original_replace = session_meta_module.os.replace
    attempts = 0

    def flaky_replace(source_path, target_path):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError(5, "sharing violation")
            error.winerror = 5
            raise error
        return original_replace(source_path, target_path)

    monkeypatch.setattr(session_meta_module.os, "replace", flaky_replace)

    write_json_atomic(status_path, {"state": "complete"})

    assert attempts == 3
    assert json.loads(status_path.read_text(encoding="utf-8")) == {"state": "complete"}


def create_codex_home(tmp_path: Path, *, complete_initial_meta: bool = False) -> tuple[Path, Path, str]:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    thread_id = "thread-a"
    rollout_path = codex_home / "sessions" / "rollout-thread-a.jsonl"
    official_namespace = {
        "type": "namespace",
        "name": "codex_app",
        "description": "",
        "tools": [
            {"type": "function", "name": name, "description": "", "inputSchema": {}}
            for name in sorted(required_thread_tool_names)
        ],
    }
    dynamic_tools = [
        {
            "type": "namespace",
            "name": "custom_namespace",
            "description": "must survive the repair",
            "tools": [],
        }
    ]
    if complete_initial_meta:
        dynamic_tools.append(official_namespace)
    write_jsonl(
        rollout_path,
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": thread_id, "dynamic_tools": dynamic_tools},
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "保留这个 prompt 原文"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:00:02Z",
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "cwd": str(tmp_path),
                    "dynamic_tools": [*dynamic_tools, official_namespace]
                    if not complete_initial_meta
                    else dynamic_tools,
                },
            },
        ],
    )

    connection = sqlite3.connect(codex_home / "state_5.sqlite")
    connection.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            title TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE thread_dynamic_tools (
            thread_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            input_schema TEXT NOT NULL,
            defer_loading INTEGER NOT NULL DEFAULT 0,
            namespace TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, 0)",
        (thread_id, str(rollout_path), "test thread"),
    )
    for position, name in enumerate(sorted(required_thread_tool_names)):
        connection.execute(
            "INSERT INTO thread_dynamic_tools VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread_id, position, name, f"description for {name}", "{}", int(position > 0), "codex_app"),
        )
    connection.commit()
    connection.close()
    return codex_home, rollout_path, thread_id


def test_repair_rewrites_initial_session_meta_and_preserves_prompt_fingerprint(tmp_path: Path) -> None:
    codex_home, rollout_path, thread_id = create_codex_home(tmp_path)
    baseline_scan = scan_rollout(rollout_path)
    status_path = tmp_path / "status.json"

    result = repair_official_thread_tool_session_meta(
        codex_home,
        tmp_path / "backups",
        status_path,
        require_codex_stopped=False,
    )

    assert result["state"] == "complete"
    assert result["candidateCount"] == 1
    assert result["completedCount"] == 1
    current_scan = scan_rollout(rollout_path)
    assert current_scan.line_count == baseline_scan.line_count
    assert current_scan.user_prompt_count == baseline_scan.user_prompt_count
    assert current_scan.user_prompt_sha256 == baseline_scan.user_prompt_sha256
    initial_records = initial_session_meta_records(rollout_path)
    assert len(initial_records) == 1
    _, initial_record = initial_records[0]
    assert required_thread_tool_names.issubset(codex_app_tool_names(initial_record))
    assert initial_record["payload"]["id"] == thread_id
    assert any(
        item.get("name") == "custom_namespace"
        for item in initial_record["payload"]["dynamic_tools"]
    )
    assert "codex_home_manager_repair" not in initial_record["payload"]
    backup_path = Path(result["threads"][0]["backupPath"])
    assert scan_rollout(backup_path).source_sha256 == baseline_scan.source_sha256
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "complete"


def test_complete_initial_session_meta_is_not_modified(tmp_path: Path) -> None:
    codex_home, rollout_path, _ = create_codex_home(tmp_path, complete_initial_meta=True)
    baseline_scan = scan_rollout(rollout_path)

    assert official_tool_candidates(codex_home) == []
    result = repair_official_thread_tool_session_meta(
        codex_home,
        tmp_path / "backups",
        tmp_path / "status.json",
        require_codex_stopped=False,
    )

    assert result["state"] == "complete"
    assert result["candidateCount"] == 0
    assert scan_rollout(rollout_path).source_sha256 == baseline_scan.source_sha256


def test_shared_rollout_alias_is_reported_and_never_rewrites_thread_identity(
    tmp_path: Path,
) -> None:
    codex_home, rollout_path, canonical_thread_id = create_codex_home(tmp_path)
    alias_thread_id = "thread-alias"
    connection = sqlite3.connect(codex_home / "state_5.sqlite")
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, 0)",
        (alias_thread_id, str(rollout_path), "shared rollout alias"),
    )
    tool_rows = connection.execute(
        """
        SELECT position, name, description, input_schema, defer_loading, namespace
        FROM thread_dynamic_tools
        WHERE thread_id = ?
        ORDER BY position
        """,
        (canonical_thread_id,),
    ).fetchall()
    for row in tool_rows:
        connection.execute(
            "INSERT INTO thread_dynamic_tools VALUES (?, ?, ?, ?, ?, ?, ?)",
            (alias_thread_id, *row),
        )
    connection.commit()
    connection.close()

    candidates = official_tool_candidates(codex_home)
    safe_candidates, blocked_candidates = partition_candidates_by_rollout_identity(
        codex_home,
        candidates,
    )
    assert [candidate["threadId"] for candidate in safe_candidates] == [canonical_thread_id]
    assert [candidate["threadId"] for candidate in blocked_candidates] == [alias_thread_id]
    assert blocked_candidates[0]["reason"] == "shared_rollout_alias"

    result = repair_official_thread_tool_session_meta(
        codex_home,
        tmp_path / "backups",
        tmp_path / "status.json",
        require_codex_stopped=False,
    )

    assert result["candidateCount"] == 2
    assert result["safeCandidateCount"] == 1
    assert result["blockedCount"] == 1
    assert result["completedCount"] == 1
    _, initial_record = initial_session_meta_records(rollout_path)[0]
    assert initial_record["payload"]["id"] == canonical_thread_id
    remaining_candidates = official_tool_candidates(codex_home)
    assert [candidate["threadId"] for candidate in remaining_candidates] == [alias_thread_id]


def test_growing_rollout_is_blocked_without_aborting_other_repairs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home, rollout_path, thread_id = create_codex_home(tmp_path)
    original_scan_rollout = session_meta_module.scan_rollout
    source_scan_count = 0

    def scan_and_grow(path: Path):
        nonlocal source_scan_count
        result = original_scan_rollout(path)
        if Path(path) == rollout_path and source_scan_count == 0:
            source_scan_count += 1
            with rollout_path.open("a", encoding="utf-8", newline="\n") as destination:
                destination.write(
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:03Z",
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "运行中新增 prompt"}],
                            },
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        return result

    monkeypatch.setattr(session_meta_module, "scan_rollout", scan_and_grow)

    result = repair_official_thread_tool_session_meta(
        codex_home,
        tmp_path / "backups",
        tmp_path / "status.json",
        require_codex_stopped=False,
    )

    assert result["state"] == "complete"
    assert result["completedCount"] == 0
    assert result["safeCandidateCount"] == 0
    assert result["blockedCount"] == 1
    assert result["blockedThreads"][0]["threadId"] == thread_id
    assert result["blockedThreads"][0]["reason"] == "rollout_changed_during_baseline_scan"
    _, initial_record = initial_session_meta_records(rollout_path)[0]
    assert not required_thread_tool_names.issubset(codex_app_tool_names(initial_record))


def test_valid_concurrent_append_after_replacement_is_preserved(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home, rollout_path, _ = create_codex_home(tmp_path)
    original_guard = session_meta_module.rollout_write_guard

    @contextmanager
    def guard_and_append(active_path: Path):
        with original_guard(active_path) as guarded_handle:
            yield guarded_handle
        with active_path.open("a", encoding="utf-8", newline="\n") as destination:
            destination.write(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:04Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "替换后追加的 prompt"}],
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    monkeypatch.setattr(session_meta_module, "rollout_write_guard", guard_and_append)

    result = repair_official_thread_tool_session_meta(
        codex_home,
        tmp_path / "backups",
        tmp_path / "status.json",
        require_codex_stopped=False,
    )

    assert result["state"] == "complete"
    assert result["completedCount"] == 1
    assert result["threads"][0]["concurrentAppendPreserved"] is True
    current_scan = scan_rollout(rollout_path)
    assert current_scan.user_prompt_count == 2
    assert "替换后追加的 prompt" in rollout_path.read_text(encoding="utf-8")


def test_append_attempt_inside_guard_cannot_overwrite_a_new_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home, rollout_path, thread_id = create_codex_home(tmp_path)
    original_install = session_meta_module.install_validated_rollout

    def append_before_install(
        temporary_path: Path,
        active_path: Path,
        guarded_handle: int | None = None,
    ) -> str:
        with active_path.open("a", encoding="utf-8", newline="\n") as destination:
            destination.write(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:04Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "不得丢失的并发 prompt"}],
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
        return original_install(temporary_path, active_path, guarded_handle)

    monkeypatch.setattr(session_meta_module, "install_validated_rollout", append_before_install)

    result = repair_official_thread_tool_session_meta(
        codex_home,
        tmp_path / "backups",
        tmp_path / "status.json",
        require_codex_stopped=False,
    )

    assert result["state"] == "complete"
    assert result["completedCount"] == 0
    assert result["blockedCount"] == 1
    assert result["blockedThreads"][0]["threadId"] == thread_id
    assert result["blockedThreads"][0]["reason"] == "rollout_atomic_replace_conflict"
    current_scan = scan_rollout(rollout_path)
    assert current_scan.user_prompt_count == 1
    assert "不得丢失的并发 prompt" not in rollout_path.read_text(encoding="utf-8")


def test_post_install_verification_failure_rolls_back_and_reports_zero_completed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home, rollout_path, _ = create_codex_home(tmp_path)
    baseline_scan = scan_rollout(rollout_path)
    original_initial_records = session_meta_module.initial_session_meta_records

    def fail_after_repaired_install(path: Path):
        records = original_initial_records(path)
        if records and required_thread_tool_names.issubset(codex_app_tool_names(records[0][1])):
            raise RuntimeError("injected post-install verification failure")
        return records

    monkeypatch.setattr(
        session_meta_module,
        "initial_session_meta_records",
        fail_after_repaired_install,
    )
    status_path = tmp_path / "status.json"

    with pytest.raises(RuntimeError, match="injected post-install verification failure"):
        repair_official_thread_tool_session_meta(
            codex_home,
            tmp_path / "backups",
            status_path,
            require_codex_stopped=False,
        )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["completedCount"] == 0
    assert status["installedCount"] == 1
    assert status["rolledBackCount"] == 1
    assert status["threads"][0]["state"] == "rolled_back"
    restored_scan = scan_rollout(rollout_path)
    assert restored_scan.source_sha256 == baseline_scan.source_sha256
    assert restored_scan.user_prompt_sha256 == baseline_scan.user_prompt_sha256


def test_legacy_flat_dynamic_tools_are_migrated_without_mixing_protocols(tmp_path: Path) -> None:
    codex_home, rollout_path, _ = create_codex_home(tmp_path, complete_initial_meta=True)
    records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
    records[0]["payload"]["dynamic_tools"] = [
        {
            "name": "automation_update",
            "namespace": "codex_app",
            "description": "legacy automation",
            "inputSchema": {},
            "deferLoading": False,
        },
        {
            "name": "install_workspace_dependencies",
            "description": "legacy extra tool",
            "inputSchema": {"type": "object"},
            "deferLoading": True,
        },
    ]
    write_jsonl(rollout_path, records)

    result = repair_official_thread_tool_session_meta(
        codex_home,
        tmp_path / "backups",
        tmp_path / "status.json",
        require_codex_stopped=False,
    )

    assert result["completedCount"] == 1
    _, initial_record = initial_session_meta_records(rollout_path)[0]
    assert dynamic_tools_protocol(initial_record) == {
        "namespaceCount": 1,
        "legacyFlatCount": 0,
        "mixed": False,
    }
    namespace = initial_record["payload"]["dynamic_tools"][0]
    nested_names = {tool["name"] for tool in namespace["tools"]}
    assert required_thread_tool_names.issubset(nested_names)
    assert "automation_update" in nested_names
    assert "install_workspace_dependencies" in nested_names


def test_complete_latest_meta_does_not_hide_incomplete_initial_meta(tmp_path: Path) -> None:
    codex_home, rollout_path, _ = create_codex_home(tmp_path)

    candidates = official_tool_candidates(codex_home)

    assert len(candidates) == 1
    assert candidates[0]["initialSessionMetaLines"] == [1]
    result = repair_official_thread_tool_session_meta(
        codex_home,
        tmp_path / "backups",
        tmp_path / "status.json",
        require_codex_stopped=False,
    )
    assert result["completedCount"] == 1
    assert all(
        required_thread_tool_names.issubset(codex_app_tool_names(record))
        for _, record in initial_session_meta_records(rollout_path)
    )


def test_non_contiguous_registry_positions_are_rejected(tmp_path: Path) -> None:
    codex_home, _, _ = create_codex_home(tmp_path)
    connection = sqlite3.connect(codex_home / "state_5.sqlite")
    connection.execute(
        "UPDATE thread_dynamic_tools SET position = 9 WHERE name = 'send_message_to_thread'"
    )
    connection.commit()
    connection.close()

    try:
        official_tool_candidates(codex_home)
    except RuntimeError as error:
        assert "positions are not contiguous" in str(error)
    else:
        raise AssertionError("expected non-contiguous positions to be rejected")

    candidates = official_tool_candidates(codex_home, require_contiguous_positions=False)
    assert len(candidates) == 1
    assert required_thread_tool_names.issubset(
        {tool["name"] for tool in candidates[0]["namespace"]["tools"]}
    )
    result = repair_official_thread_tool_session_meta(
        codex_home,
        tmp_path / "backups",
        tmp_path / "status.json",
        require_codex_stopped=False,
    )
    assert result["state"] == "complete"


def test_cli_runs_end_to_end_with_external_guard_contract(tmp_path: Path) -> None:
    codex_home, rollout_path, _ = create_codex_home(tmp_path)
    baseline_scan = scan_rollout(rollout_path)
    status_path = tmp_path / "cli-status.json"
    script_path = Path(__file__).parents[1] / "scripts" / "repair-official-thread-tool-session-meta.py"

    completed_process = subprocess.run(
        [
            sys.executable,
            "-B",
            str(script_path),
            "--codex-home",
            str(codex_home),
            "--backup-root",
            str(tmp_path / "cli-backups"),
            "--status-path",
            str(status_path),
            "--external-process-guard-active",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed_process.returncode == 0, completed_process.stderr
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["state"] == "complete"
    current_scan = scan_rollout(rollout_path)
    assert current_scan.user_prompt_count == baseline_scan.user_prompt_count
    assert current_scan.user_prompt_sha256 == baseline_scan.user_prompt_sha256
