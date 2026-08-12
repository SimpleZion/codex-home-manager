from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

import apply_codex_offline_repair
from backend import offline_repair_policy


@pytest.fixture(autouse=True)
def provide_test_disk_capacity(monkeypatch) -> None:
    usage = type("usage", (), {"total": 20 * 1024**3, "used": 1 * 1024**3, "free": 19 * 1024**3})
    monkeypatch.setattr(apply_codex_offline_repair.shutil, "disk_usage", lambda _path: usage)
    monkeypatch.setattr(apply_codex_offline_repair, "assert_backup_path", lambda _path, _root=None: None)
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def valid_compacted() -> dict[str, object]:
    return {
        "type": "compacted",
        "payload": {
            "replacement_history": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "checkpoint summary"}],
                },
                {
                    "type": "compaction",
                    "id": "compaction-a",
                    "encrypted_content": "encrypted-checkpoint",
                },
            ]
        },
    }


def create_sqlite(path: Path) -> None:
    database = sqlite3.connect(path)
    try:
        database.execute("create table marker(value text)")
        database.execute("insert into marker values ('ok')")
        database.commit()
    finally:
        database.close()


def create_state_sqlite(path: Path, thread_id: str, rollout_path: Path) -> None:
    create_state_sqlite_rows(path, [(thread_id, rollout_path)])


def create_state_sqlite_rows(path: Path, rows: list[tuple[str, Path]]) -> None:
    database = sqlite3.connect(path)
    try:
        database.execute("create table threads(id text primary key, rollout_path text not null)")
        database.executemany("insert into threads values (?, ?)", [(thread_id, str(path)) for thread_id, path in rows])
        database.commit()
    finally:
        database.close()


def bind_audit(audit: dict[str, object], codex_home: Path) -> dict[str, object]:
    state_path = codex_home / "state_5.sqlite"
    state_stat = state_path.stat()
    audit["schema_version"] = apply_codex_offline_repair.audit_schema_version
    audit["generated_at_epoch"] = int(time.time())
    audit["state"] = {
        "path": str(state_path.resolve()),
        "size": state_stat.st_size,
        "mtime_ns": state_stat.st_mtime_ns,
        "sha256": apply_codex_offline_repair.file_sha256(state_path),
        "files": apply_codex_offline_repair.state_file_contracts(state_path),
    }
    audit["policy"] = {
        "name": "codex-thread-history-repair",
        "version": apply_codex_offline_repair.audit_policy_version,
        "thresholds": apply_codex_offline_repair.expected_threshold_policy(),
    }
    if "threads" not in audit:
        rows: list[dict[str, object]] = []
        for key in (
            "active_performance_repair_candidates",
            "active_compatibility_repair_candidates",
            "archived_performance_repair_candidates",
            "archived_compatibility_repair_candidates",
        ):
            rows.extend(audit.get(key, []))
        audit["threads"] = rows
    return audit


def rollback_from_manifest(manifest_path: Path, codex_home: Path):
    manifest, manifest_sha256 = apply_codex_offline_repair.load_manifest_pair(manifest_path)
    return apply_codex_offline_repair.rollback_completed_repair(
        manifest_path,
        expected_run_id=str(manifest["runner_run_id"]),
        expected_run_root=Path(str(manifest["run_root"])),
        expected_codex_home=codex_home,
        expected_manifest_sha256=manifest_sha256,
    )


def test_archived_global_state_cleanup_preserves_archive_index_and_rolls_back_exactly(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    create_state_sqlite(codex_home / "state_5.sqlite", "archived-thread", codex_home / "sessions" / "rollout.jsonl")
    session_index_path = codex_home / "session_index.jsonl"
    session_index_path.write_text(
        json.dumps({"id": "archived-thread", "thread_name": "Archived"}) + "\n",
        encoding="utf-8",
    )
    state_data = {
        "pinned-thread-ids": ["archived-thread", "active-thread"],
        "projectless-thread-ids": ["archived-thread", "active-thread"],
        "thread-workspace-root-hints": {
            "archived-thread": "C:/archived",
            "active-thread": "C:/active",
        },
        "heartbeat-thread-permissions-by-id": {
            "archived-thread": {"allowed": True},
            "active-thread": {"allowed": True},
        },
        "electron-persisted-atom-state": {
            "thread-workspace-root-hints": {
                "archived-thread": "C:/nested-archived",
                "active-thread": "C:/nested-active",
            }
        },
    }
    original_state_bytes: dict[str, bytes] = {}
    for file_name in (".codex-global-state.json", ".codex-global-state.json.bak"):
        path = codex_home / file_name
        path.write_text(json.dumps(state_data, ensure_ascii=False), encoding="utf-8")
        original_state_bytes[file_name] = path.read_bytes()
    original_index_bytes = session_index_path.read_bytes()
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)
    state_backups = apply_codex_offline_repair.backup_codex_state(codex_home, backup_root)

    result = apply_codex_offline_repair.cleanup_archived_global_state_references(
        codex_home,
        {"archived-thread"},
    )

    assert result["removed_reference_count"] == 10
    assert result["removed_thread_ids"] == ["archived-thread"]
    assert session_index_path.read_bytes() == original_index_bytes
    for file_name in (".codex-global-state.json", ".codex-global-state.json.bak"):
        cleaned = json.loads((codex_home / file_name).read_text(encoding="utf-8"))
        assert "archived-thread" not in json.dumps(cleaned)
        assert "active-thread" in json.dumps(cleaned)

    rollback = apply_codex_offline_repair.restore_state_database_snapshot(
        state_backups=state_backups,
        state_absent_paths=[],
        backup_root=backup_root,
        precommit_guard=lambda: None,
    )

    assert rollback["errors"] == []
    assert session_index_path.read_bytes() == original_index_bytes
    for file_name, original_bytes in original_state_bytes.items():
        assert (codex_home / file_name).read_bytes() == original_bytes


def test_rollback_ignores_unbound_repair_journal(tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    journal_root = backup_root / "rollouts" / "rogue"
    journal_root.mkdir(parents=True)
    outside_source = tmp_path / "outside.txt"
    outside_backup = tmp_path / "outside-backup.txt"
    outside_source.write_text("must stay", encoding="utf-8")
    outside_backup.write_text("attacker replacement", encoding="utf-8")
    (journal_root / "rogue.repair-journal.json").write_text(
        json.dumps(
            {
                "thread_id": "rogue",
                "source_path": str(outside_source),
                "backup_path": str(outside_backup),
                "original_sha256": apply_codex_offline_repair.file_sha256(outside_backup),
            }
        ),
        encoding="utf-8",
    )

    result = apply_codex_offline_repair.rollback_repair_changes(
        thread_repairs=[],
        log_plan=[],
        backup_root=backup_root,
        precommit_guard=lambda: None,
    )

    assert outside_source.read_text(encoding="utf-8") == "must stay"
    assert result["thread_restores"] == []
    assert result["errors"] == []
    assert result["ignored_unbound_journals"] == [
        str(journal_root / "rogue.repair-journal.json")
    ]


def test_apply_repairs_closes_backup_replace_log_archive_and_postcheck(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    create_sqlite(codex_home / "logs_2.sqlite")
    (codex_home / "config.toml").write_text("model = 'test'\n", encoding="utf-8")

    thread_id = "thread-a"
    rollout_path = codex_home / "sessions" / "rollout-thread-a.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "invalid"}]}},
            {"type": "world_state", "payload": {"full": True, "state": {"goal": "keep"}}},
            valid_compacted(),
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )
    original_rollout = rollout_path.read_bytes()
    create_state_sqlite(codex_home / "state_5.sqlite", thread_id, rollout_path)
    file_stat = rollout_path.stat()
    scan = apply_codex_offline_repair.scan_rollout(rollout_path).to_dict()
    row = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "archived": 0,
        "audited_size": file_stat.st_size,
        "audited_mtime_ns": file_stat.st_mtime_ns,
        "audited_sha256": scan["source_sha256"],
        "scan": scan,
    }
    audit = {
        "schema_version": 2,
        "thresholds": {"max_active_bytes": 10_000, "max_active_lines": 100, "max_tail_lines": 100},
        "active_performance_repair_candidates": [row],
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
        "threads": [row],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    manifest = apply_codex_offline_repair.apply_repairs(
        audit_path=audit_path,
        backup_root=backup_root,
        codex_home=codex_home,
        include_archived=True,
        expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
    )

    assert manifest["status"] == "applied_pending_runner_diagnostics"
    assert manifest["postcheck"] == {
        "state_quick_check_after": "ok",
        "database_thread_count": 1,
        "checked_rollouts": 1,
        "missing_rollouts": 0,
        "json_parse_errors": 0,
        "estimated_current_parser_errors": 0,
        "remaining_performance_candidates": 0,
        "remaining_compatibility_candidates": 0,
        "remaining_blocked_candidates": 0,
        "prompt_contract_checked_threads": 1,
        "prompt_contract_baseline_threads": 1,
        "prompt_contract_mode": "exact",
    }
    assert (backup_root / "state" / "state_5.sqlite").is_file()
    assert not (codex_home / "logs_2.sqlite").exists()
    assert (backup_root / "logs" / "logs_2.sqlite").is_file()
    archived_rollout = backup_root / "rollouts" / thread_id / rollout_path.name
    assert archived_rollout.read_bytes() == original_rollout
    assert rollout_path.stat().st_size < len(original_rollout)


def test_apply_repairs_does_not_checkpoint_performance_only_threads_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    create_sqlite(codex_home / "logs_2.sqlite")
    (codex_home / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
    thread_id = "performance-only"
    rollout_path = codex_home / "sessions" / "rollout-performance-only.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "must remain exactly"}],
                },
            },
            valid_compacted(),
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )
    original_rollout = rollout_path.read_bytes()
    create_state_sqlite(codex_home / "state_5.sqlite", thread_id, rollout_path)
    original_thresholds = apply_codex_offline_repair.RepairThresholds
    monkeypatch.setattr(
        apply_codex_offline_repair,
        "RepairThresholds",
        lambda: original_thresholds(max_active_bytes=1, max_active_lines=1, max_tail_lines=100),
    )
    rollout_stat = rollout_path.stat()
    rollout_scan = apply_codex_offline_repair.scan_rollout(rollout_path).to_dict()
    row = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "archived": 0,
        "audited_size": rollout_stat.st_size,
        "audited_mtime_ns": rollout_stat.st_mtime_ns,
        "audited_sha256": rollout_scan["source_sha256"],
        "scan": rollout_scan,
    }
    audit = {
        "active_performance_repair_candidates": [row],
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
        "threads": [row],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    monkeypatch.setattr(
        apply_codex_offline_repair,
        "repair_rollout_in_place",
        lambda **_arguments: (_ for _ in ()).throw(AssertionError("default repair must not slim performance-only threads")),
    )
    manifest = apply_codex_offline_repair.apply_repairs(
        audit_path=audit_path,
        backup_root=backup_root,
        codex_home=codex_home,
        include_archived=True,
        expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
    )

    assert manifest["status"] == "applied_pending_runner_diagnostics"
    assert manifest["thread_repairs"] == []
    assert manifest["postcheck"]["remaining_performance_candidates"] == 1
    assert rollout_path.read_bytes() == original_rollout


def test_apply_repairs_slims_only_explicit_thread_and_preserves_exact_prompts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    create_sqlite(codex_home / "logs_2.sqlite")
    thread_id = "explicit-performance-thread"
    rollout_path = codex_home / "sessions" / "rollout-explicit-performance.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "checkpoint summary and this exact prompt must remain unchanged",
                        }
                    ],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "discardable history " * 4_000}],
                },
            },
            valid_compacted(),
            {"type": "event_msg", "payload": {"type": "user_message", "message": "tail prompt stays byte-exact"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )
    original_rollout = rollout_path.read_bytes()
    source_scan = apply_codex_offline_repair.scan_rollout(rollout_path)
    assert source_scan.latest_compacted_checkpoint_valid is True
    create_state_sqlite(codex_home / "state_5.sqlite", thread_id, rollout_path)
    rollout_stat = rollout_path.stat()
    row = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "archived": 0,
        "audited_size": rollout_stat.st_size,
        "audited_mtime_ns": rollout_stat.st_mtime_ns,
        "audited_sha256": source_scan.source_sha256,
        "scan": source_scan.to_dict(),
    }
    audit = {
        "active_performance_repair_candidates": [row],
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
        "threads": [row],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    manifest = apply_codex_offline_repair.apply_repairs(
        audit_path=audit_path,
        backup_root=backup_root,
        codex_home=codex_home,
        include_archived=True,
        expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
        slim_thread_ids=[thread_id],
    )

    active_scan = apply_codex_offline_repair.scan_rollout(rollout_path)
    assert manifest["checkpoint_history_reduction_enabled"] is True
    assert manifest["prompt_preserving_slim_thread_ids"] == [thread_id]
    assert manifest["thread_repairs"][0]["strategy"] == "prompt_preserving_checkpoint_slim_view"
    assert manifest["postcheck"]["targeted_slim"] == {
        "requested_thread_ids": [thread_id],
        "committed_thread_ids": [thread_id],
        "all_reduced": True,
    }
    assert active_scan.total_bytes < source_scan.total_bytes
    assert active_scan.user_prompt_count == source_scan.user_prompt_count
    assert active_scan.user_prompt_sha256 == source_scan.user_prompt_sha256
    assert (backup_root / "rollouts" / thread_id / rollout_path.name).read_bytes() == original_rollout


def test_apply_repairs_migrates_large_compatibility_thread_without_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    create_sqlite(codex_home / "logs_2.sqlite")
    thread_id = "large-compatibility"
    rollout_path = codex_home / "sessions" / "rollout-large-compatibility.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "keep exact prompt"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "tool_search_output",
                    "tools_summary": {"omitted_tool_count": 2},
                },
            },
        ],
    )
    create_state_sqlite(codex_home / "state_5.sqlite", thread_id, rollout_path)
    original_thresholds = apply_codex_offline_repair.RepairThresholds
    monkeypatch.setattr(
        apply_codex_offline_repair,
        "RepairThresholds",
        lambda: original_thresholds(max_active_bytes=1, max_active_lines=1, max_tail_lines=1),
    )
    source_stat = rollout_path.stat()
    source_scan = apply_codex_offline_repair.scan_rollout(rollout_path)
    row = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "archived": 0,
        "audited_size": source_stat.st_size,
        "audited_mtime_ns": source_stat.st_mtime_ns,
        "audited_sha256": source_scan.source_sha256,
        "scan": source_scan.to_dict(),
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            bind_audit(
                {
                    "missing_rollouts": [],
                    "shared_rollouts": [],
                    "active_blocked": [],
                    "archived_blocked": [],
                    "threads": [row],
                },
                codex_home,
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    manifest = apply_codex_offline_repair.apply_repairs(
        audit_path=audit_path,
        backup_root=tmp_path / "backup",
        codex_home=codex_home,
        include_archived=True,
        expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
    )
    final_scan = apply_codex_offline_repair.scan_rollout(rollout_path)

    assert [item["strategy"] for item in manifest["thread_repairs"]] == ["compatibility_migration"]
    assert final_scan.estimated_current_parser_errors == 0
    assert final_scan.user_prompt_count == source_scan.user_prompt_count
    assert final_scan.user_prompt_sha256 == source_scan.user_prompt_sha256
    assert manifest["postcheck"]["remaining_performance_candidates"] == 1
    assert manifest["postcheck"]["prompt_contract_checked_threads"] == 1
    assert manifest["postcheck"]["prompt_contract_baseline_threads"] == 1
    assert manifest["prompt_contract"]["postcheck_mode"] == "exact"


def test_apply_repairs_rolls_back_when_final_prompt_contract_differs_from_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    create_sqlite(codex_home / "logs_2.sqlite")
    thread_id = "prompt-contract-thread"
    rollout_path = codex_home / "sessions" / "rollout-prompt-contract.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "keep exact prompt"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "tool_search_output",
                    "tools_summary": {"omitted_tool_count": 1},
                },
            },
        ],
    )
    original_rollout = rollout_path.read_bytes()
    create_state_sqlite(codex_home / "state_5.sqlite", thread_id, rollout_path)
    source_stat = rollout_path.stat()
    source_scan = apply_codex_offline_repair.scan_rollout(rollout_path)
    row = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "archived": 0,
        "audited_size": source_stat.st_size,
        "audited_mtime_ns": source_stat.st_mtime_ns,
        "audited_sha256": source_scan.source_sha256,
        "scan": source_scan.to_dict(),
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            bind_audit(
                {
                    "missing_rollouts": [],
                    "shared_rollouts": [],
                    "active_blocked": [],
                    "archived_blocked": [],
                    "threads": [row],
                },
                codex_home,
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)
    real_repair = apply_codex_offline_repair.repair_rollout_compatibility_in_place

    def corrupt_after_replace(**arguments):
        result = real_repair(**arguments)
        active_path = Path(arguments["source_path"])
        active_path.write_text(
            active_path.read_text(encoding="utf-8").replace("keep exact prompt", "changed prompt"),
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(
        apply_codex_offline_repair,
        "repair_rollout_compatibility_in_place",
        corrupt_after_replace,
    )

    with pytest.raises(RuntimeError, match="differ from the baseline audit"):
        apply_codex_offline_repair.apply_repairs(
            audit_path=audit_path,
            backup_root=tmp_path / "backup",
            codex_home=codex_home,
            include_archived=True,
            expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
        )

    assert rollout_path.read_bytes() == original_rollout


def test_apply_repairs_refuses_missing_rollout_before_creating_backup(tmp_path: Path, monkeypatch) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps({"schema_version": 2, "missing_rollouts": [{"id": "missing"}]}),
        encoding="utf-8",
    )
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    try:
        apply_codex_offline_repair.apply_repairs(
            audit_path=audit_path,
            backup_root=backup_root,
            codex_home=tmp_path / "codex_home",
            include_archived=True,
            expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
        )
    except RuntimeError as error:
        assert "missing rollouts" in str(error)
    else:
        raise AssertionError("missing rollout audit must be rejected")

    assert not backup_root.exists()


def test_apply_repairs_refuses_blocked_candidate_before_creating_backup(tmp_path: Path, monkeypatch) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "missing_rollouts": [],
                "shared_rollouts": [],
                "active_blocked": [{"id": "blocked-thread", "repair_block_reason": "source_parse_errors"}],
                "archived_blocked": [],
            }
        ),
        encoding="utf-8",
    )
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    with pytest.raises(RuntimeError, match="blocked repair candidates"):
        apply_codex_offline_repair.apply_repairs(
            audit_path=audit_path,
            backup_root=backup_root,
            codex_home=tmp_path / "codex_home",
            include_archived=True,
            expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
        )

    assert not backup_root.exists()


def test_archive_logs_restores_every_source_when_detach_fails(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex_home"
    backup_root = tmp_path / "backup"
    codex_home.mkdir()
    expected = {
        "logs_2.sqlite": b"main-log",
        "logs_2.sqlite-wal": b"wal-log",
        "logs_2.sqlite-shm": b"shm-log",
        "logs_2.sqlite-journal": b"journal-log",
    }
    for name, content in expected.items():
        (codex_home / name).write_bytes(content)

    real_replace = os.replace

    def fail_second_detach(source, destination) -> None:
        if Path(source).name == "logs_2.sqlite-wal":
            raise OSError("simulated detach failure")
        real_replace(source, destination)

    monkeypatch.setattr(apply_codex_offline_repair.os, "replace", fail_second_detach)

    with pytest.raises(OSError, match="simulated detach failure"):
        apply_codex_offline_repair.archive_logs(
            codex_home,
            backup_root,
            precommit_guard=lambda: None,
            before_detach=lambda _plan: None,
        )

    for name, content in expected.items():
        assert (codex_home / name).read_bytes() == content
        assert (backup_root / "logs" / name).read_bytes() == content


def test_apply_repairs_restores_prior_thread_when_later_repair_fails(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    create_sqlite(codex_home / "logs_2.sqlite")

    rows: list[dict[str, object]] = []
    originals: dict[Path, bytes] = {}
    for thread_id in ("thread-a", "thread-b"):
        rollout_path = codex_home / "sessions" / f"rollout-{thread_id}.jsonl"
        write_jsonl(
            rollout_path,
            [
                {"type": "session_meta", "payload": {"id": thread_id}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "invalid"}]}},
                {"type": "world_state", "payload": {"full": True, "state": {"goal": thread_id}}},
                valid_compacted(),
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
            ],
        )
        originals[rollout_path] = rollout_path.read_bytes()
        file_stat = rollout_path.stat()
        rollout_scan = apply_codex_offline_repair.scan_rollout(rollout_path).to_dict()
        rows.append(
            {
                "id": thread_id,
                "rollout_path": str(rollout_path),
                "archived": 0,
                "audited_size": file_stat.st_size,
                "audited_mtime_ns": file_stat.st_mtime_ns,
                "audited_sha256": rollout_scan["source_sha256"],
                "scan": rollout_scan,
            }
        )

    create_state_sqlite_rows(
        codex_home / "state_5.sqlite",
        [(str(row["id"]), Path(str(row["rollout_path"]))) for row in rows],
    )
    audit = {
        "schema_version": 2,
        "active_performance_repair_candidates": rows,
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    real_repair = apply_codex_offline_repair.repair_rollout_compatibility_in_place
    repair_calls = 0

    def fail_second_repair(**arguments):
        nonlocal repair_calls
        repair_calls += 1
        if repair_calls == 2:
            raise RuntimeError("simulated later repair failure")
        return real_repair(**arguments)

    monkeypatch.setattr(apply_codex_offline_repair, "repair_rollout_compatibility_in_place", fail_second_repair)

    with pytest.raises(RuntimeError, match="simulated later repair failure"):
        apply_codex_offline_repair.apply_repairs(
            audit_path=audit_path,
            backup_root=tmp_path / "backup",
            codex_home=codex_home,
            include_archived=True,
            expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
        )

    for rollout_path, content in originals.items():
        assert rollout_path.read_bytes() == content


def test_apply_repairs_restores_threads_and_logs_when_postcheck_fails(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    create_sqlite(codex_home / "logs_2.sqlite")
    original_log = (codex_home / "logs_2.sqlite").read_bytes()

    repair_thread_id = "thread-repair"
    repair_rollout = codex_home / "sessions" / "rollout-repair.jsonl"
    write_jsonl(
        repair_rollout,
        [
            {"type": "session_meta", "payload": {"id": repair_thread_id}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "invalid"}]}},
            {"type": "world_state", "payload": {"full": True, "state": {"goal": "keep"}}},
            valid_compacted(),
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )
    original_rollout = repair_rollout.read_bytes()
    repair_stat = repair_rollout.stat()
    repair_row = {
        "id": repair_thread_id,
        "rollout_path": str(repair_rollout),
        "archived": 0,
        "audited_size": repair_stat.st_size,
        "audited_mtime_ns": repair_stat.st_mtime_ns,
        "audited_sha256": apply_codex_offline_repair.scan_rollout(repair_rollout).source_sha256,
        "scan": apply_codex_offline_repair.scan_rollout(repair_rollout).to_dict(),
    }

    bad_thread_id = "thread-bad"
    bad_rollout = codex_home / "sessions" / "rollout-bad.jsonl"
    write_jsonl(
        bad_rollout,
        [
            {"type": "session_meta", "payload": {"id": bad_thread_id}},
            {
                "type": "response_item",
                "payload": {"type": "tool_search_output", "tools_summary": [{"name": "legacy"}]},
            },
        ],
    )
    create_state_sqlite_rows(
        codex_home / "state_5.sqlite",
        [(repair_thread_id, repair_rollout), (bad_thread_id, bad_rollout)],
    )
    audit = {
        "schema_version": 2,
        "active_performance_repair_candidates": [repair_row],
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    with pytest.raises(RuntimeError, match="absent from the prompt baseline"):
        apply_codex_offline_repair.apply_repairs(
            audit_path=audit_path,
            backup_root=tmp_path / "backup",
            codex_home=codex_home,
            include_archived=True,
            expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
        )

    assert repair_rollout.read_bytes() == original_rollout
    assert (codex_home / "logs_2.sqlite").read_bytes() == original_log


def test_codex_process_guard_detects_orphan_plugin_processes(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self, pid: int, name: str, command_line: list[str]):
            self.pid = pid
            self.info = {"pid": pid, "name": name}
            self._command_line = command_line

        def exe(self) -> str:
            return f"C:/fake/{self.info['name']}"

        def cmdline(self) -> list[str]:
            return self._command_line

    processes = [
        FakeProcess(10, "node.exe", ["node.exe", "ordinary-project.js"]),
        FakeProcess(11, "cmd.exe", ["cmd.exe", "/c", "npx", "xcodebuildmcp@latest", "mcp"]),
        FakeProcess(12, "node_repl.exe", ["node_repl.exe"]),
        FakeProcess(13, "node.exe", ["node.exe", "./mcp/server.mjs", "--stdio"]),
        FakeProcess(14, "node.exe", ["node.exe", "./mcp/server.bundle.mjs"]),
        FakeProcess(15, "node.exe", ["node.exe", "./mcp/server.cjs", "--stdio"]),
        FakeProcess(16, "node.exe", ["node.exe", "C:/npm/xcodebuildmcp/build/cli.js", "mcp"]),
    ]
    monkeypatch.setattr(offline_repair_policy.psutil, "process_iter", lambda _attributes: processes)

    found = apply_codex_offline_repair.codex_processes()

    assert {item["pid"] for item in found} == {11, 12, 13, 14, 15, 16}


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths are platform-specific")
def test_extended_length_rollout_path_is_inside_codex_home_and_matches_sqlite(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    rollout_path = codex_home / "sessions" / "rollout-thread-a.jsonl"
    write_jsonl(rollout_path, [{"type": "session_meta", "payload": {"id": "thread-a"}}])
    extended_rollout_path = Path("\\\\?\\" + str(rollout_path))
    state_path = codex_home / "state_5.sqlite"
    create_state_sqlite(state_path, "thread-a", extended_rollout_path)
    database = sqlite3.connect(state_path)
    try:
        assert apply_codex_offline_repair.path_is_within(extended_rollout_path, codex_home)
        apply_codex_offline_repair.validate_thread_mapping(
            database,
            "thread-a",
            rollout_path,
            codex_home,
        )
    finally:
        database.close()


def test_disk_preflight_refuses_insufficient_space(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    backup_root = tmp_path / "backup" / "repair"
    audit = {
        "active_performance_repair_candidates": [
            {"rollout_path": str(codex_home / "sessions" / "a.jsonl"), "audited_size": 100}
        ],
        "active_compatibility_repair_candidates": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
    }
    disk_usage = type("usage", (), {"total": 1000, "used": 999, "free": 1})
    monkeypatch.setattr(apply_codex_offline_repair.shutil, "disk_usage", lambda _path: disk_usage)

    with pytest.raises(RuntimeError, match="insufficient backup-volume space"):
        apply_codex_offline_repair.estimate_required_space(
            audit,
            codex_home,
            backup_root,
            include_archived=True,
        )


def test_completed_repair_can_be_rolled_back_from_manifest(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    create_sqlite(codex_home / "logs_2.sqlite")
    original_log = (codex_home / "logs_2.sqlite").read_bytes()
    thread_id = "thread-a"
    rollout_path = codex_home / "sessions" / "rollout-thread-a.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "invalid"}]}},
            {"type": "world_state", "payload": {"full": True, "state": {"goal": "keep"}}},
            valid_compacted(),
        ],
    )
    original_rollout = rollout_path.read_bytes()
    create_state_sqlite(codex_home / "state_5.sqlite", thread_id, rollout_path)
    rollout_stat = rollout_path.stat()
    rollout_scan = apply_codex_offline_repair.scan_rollout(rollout_path).to_dict()
    row = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "archived": 0,
        "audited_size": rollout_stat.st_size,
        "audited_mtime_ns": rollout_stat.st_mtime_ns,
        "audited_sha256": rollout_scan["source_sha256"],
        "scan": rollout_scan,
    }
    audit = {
        "schema_version": 2,
        "active_performance_repair_candidates": [row],
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    run_root = tmp_path / "run"
    backup_root = run_root / "repair_data"
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)
    apply_codex_offline_repair.apply_repairs(
        audit_path,
        backup_root,
        codex_home,
        True,
        apply_codex_offline_repair.file_sha256(audit_path),
        runner_run_id="test-run",
        run_root=run_root,
    )

    result = rollback_from_manifest(backup_root / "repair_manifest.json", codex_home)

    assert result["errors"] == []
    assert rollout_path.read_bytes() == original_rollout
    assert (codex_home / "logs_2.sqlite").read_bytes() == original_log
    manifest = json.loads((backup_root / "repair_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "rolled_back"


def test_write_ahead_journal_recovers_replace_when_process_exits_before_result_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    thread_id = "thread-a"
    rollout_path = codex_home / "sessions" / "rollout-thread-a.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "invalid"}]}},
            {"type": "world_state", "payload": {"full": True, "state": {"goal": "keep"}}},
            valid_compacted(),
        ],
    )
    original_rollout = rollout_path.read_bytes()
    create_state_sqlite(codex_home / "state_5.sqlite", thread_id, rollout_path)
    rollout_stat = rollout_path.stat()
    rollout_scan = apply_codex_offline_repair.scan_rollout(rollout_path).to_dict()
    row = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "archived": 0,
        "audited_size": rollout_stat.st_size,
        "audited_mtime_ns": rollout_stat.st_mtime_ns,
        "audited_sha256": rollout_scan["source_sha256"],
        "scan": rollout_scan,
    }
    audit = {
        "schema_version": 2,
        "thresholds": {"max_active_bytes": 10_000, "max_active_lines": 100, "max_tail_lines": 100},
        "active_performance_repair_candidates": [row],
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    run_root = tmp_path / "run"
    backup_root = run_root / "repair_data"
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)
    real_repair = apply_codex_offline_repair.repair_rollout_compatibility_in_place

    def crash_after_replace(**arguments):
        def terminate_process() -> None:
            raise SystemExit(91)

        return real_repair(**arguments, post_replace_hook=terminate_process)

    monkeypatch.setattr(apply_codex_offline_repair, "repair_rollout_compatibility_in_place", crash_after_replace)
    with pytest.raises(SystemExit, match="91"):
        apply_codex_offline_repair.apply_repairs(
            audit_path,
            backup_root,
            codex_home,
            True,
            apply_codex_offline_repair.file_sha256(audit_path),
            runner_run_id="test-run",
            run_root=run_root,
        )

    assert rollout_path.read_bytes() != original_rollout
    manifest = json.loads((backup_root / "repair_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert len(manifest["thread_repair_journal"]) == 1
    assert manifest["thread_repair_journal"][0]["status"] == "prepared"

    rollback_from_manifest(backup_root / "repair_manifest.json", codex_home)
    assert rollout_path.read_bytes() == original_rollout


def test_process_crash_after_replace_is_recovered_from_write_ahead_journal(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    thread_id = "thread-a"
    rollout_path = codex_home / "sessions" / "rollout-thread-a.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "invalid"}]}},
            {"type": "world_state", "payload": {"full": True, "state": {"goal": "keep"}}},
            valid_compacted(),
        ],
    )
    original_rollout = rollout_path.read_bytes()
    create_state_sqlite(codex_home / "state_5.sqlite", thread_id, rollout_path)
    rollout_stat = rollout_path.stat()
    rollout_scan = apply_codex_offline_repair.scan_rollout(rollout_path).to_dict()
    row = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "archived": 0,
        "audited_size": rollout_stat.st_size,
        "audited_mtime_ns": rollout_stat.st_mtime_ns,
        "audited_sha256": rollout_scan["source_sha256"],
        "scan": rollout_scan,
    }
    audit = {
        "schema_version": 2,
        "thresholds": {"max_active_bytes": 10_000, "max_active_lines": 100, "max_tail_lines": 100},
        "active_performance_repair_candidates": [row],
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    run_root = tmp_path / "run"
    backup_root = run_root / "repair_data"
    child_script = tmp_path / "crash_worker.py"
    scripts_path_text = str(scripts_path)
    child_script.write_text(
        f"""
import os
import sys
from pathlib import Path
sys.path.insert(0, {scripts_path_text!r})
import apply_codex_offline_repair as repair
repair.assert_codex_offline = lambda: None
repair.assert_backup_path = lambda *_arguments: None
real_repair = repair.repair_rollout_compatibility_in_place
def crash_after_replace(**arguments):
    return real_repair(**arguments, post_replace_hook=lambda: os._exit(91))
repair.repair_rollout_compatibility_in_place = crash_after_replace
repair.apply_repairs(Path({str(audit_path)!r}), Path({str(backup_root)!r}), Path({str(codex_home)!r}), True, repair.file_sha256(Path({str(audit_path)!r})), runner_run_id="test-run", run_root=Path({str(run_root)!r}))
""".lstrip(),
        encoding="utf-8",
    )

    completed = subprocess.run([sys.executable, str(child_script)], timeout=30)

    assert completed.returncode == 91
    assert rollout_path.read_bytes() != original_rollout
    manifest_path = backup_root / "repair_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["thread_repair_journal"][0]["status"] == "prepared"
    manifest["thread_repair_journal"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert list((backup_root / "rollouts").rglob("*.repair-journal.json"))
    manifest_path.write_text("{broken-primary", encoding="utf-8")
    original_assert_codex_offline = apply_codex_offline_repair.assert_codex_offline
    apply_codex_offline_repair.assert_codex_offline = lambda: None
    try:
        rollback_from_manifest(manifest_path, codex_home)
    finally:
        apply_codex_offline_repair.assert_codex_offline = original_assert_codex_offline
    assert rollout_path.read_bytes() == original_rollout


def test_title_sync_uses_session_index_and_state_backup_rolls_it_back(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    thread_id = "thread-a"
    rollout_path = codex_home / "sessions" / "rollout-thread-a.jsonl"
    write_jsonl(rollout_path, [{"type": "session_meta", "payload": {"id": thread_id}}])
    state_path = codex_home / "state_5.sqlite"
    database = sqlite3.connect(state_path)
    try:
        database.execute("create table threads(id text primary key, rollout_path text, title text, archived integer)")
        database.execute(
            "insert into threads values (?, ?, ?, 0)",
            (thread_id, str(rollout_path), "旧首条 prompt"),
        )
        database.commit()
    finally:
        database.close()
    session_index_path = codex_home / "session_index.jsonl"
    session_index_path.write_text(
        "\n".join(
            [
                json.dumps({"id": thread_id, "thread_name": "用户可见标题", "updated_at": "2026-07-11T02:00:00Z"}, ensure_ascii=False),
                json.dumps({"id": thread_id, "thread_name": "过期标题", "updated_at": "2026-07-10T02:00:00Z"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    original_session_index = session_index_path.read_bytes()
    audit = {
        "active_performance_repair_candidates": [],
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
        "threads": [
            {
                "id": thread_id,
                "rollout_path": str(rollout_path),
                "archived": 0,
                "audited_size": rollout_path.stat().st_size,
                "audited_mtime_ns": rollout_path.stat().st_mtime_ns,
                "audited_sha256": apply_codex_offline_repair.scan_rollout(rollout_path).source_sha256,
                "scan": apply_codex_offline_repair.scan_rollout(rollout_path).to_dict(),
            }
        ],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    run_root = tmp_path / "run"
    backup_root = run_root / "repair_data"
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    manifest = apply_codex_offline_repair.apply_repairs(
        audit_path,
        backup_root,
        codex_home,
        True,
        apply_codex_offline_repair.file_sha256(audit_path),
        runner_run_id="test-run",
        run_root=run_root,
        mutation_allowlist=[apply_codex_offline_repair.mutation_title_sync],
    )
    database = sqlite3.connect(state_path)
    try:
        assert database.execute("select title from threads where id = ?", (thread_id,)).fetchone()[0] == "用户可见标题"
    finally:
        database.close()
    assert manifest["title_sync"]["status"] == "committed"
    assert manifest["title_sync"]["changed_count"] == 1
    assert manifest["mutation_preview"]["title_sync"]["allowed"] is True
    assert manifest["mutation_preview"]["title_sync"]["sqlite_updates"] == [
        {"thread_id": thread_id, "before": "旧首条 prompt", "after": "用户可见标题"}
    ]
    assert len(session_index_path.read_text(encoding="utf-8").splitlines()) == 1

    rollback_from_manifest(backup_root / "repair_manifest.json", codex_home)
    database = sqlite3.connect(state_path)
    try:
        assert database.execute("select title from threads where id = ?", (thread_id,)).fetchone()[0] == "旧首条 prompt"
    finally:
        database.close()
    assert session_index_path.read_bytes() == original_session_index


def test_default_compatibility_migration_previews_but_does_not_mutate_titles_or_global_state(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    thread_id = "archived-thread"
    rollout_path = codex_home / "sessions" / "rollout-archived-thread.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "prompt hash must remain exact"}],
                },
            },
        ],
    )
    state_path = codex_home / "state_5.sqlite"
    database = sqlite3.connect(state_path)
    try:
        database.execute("create table threads(id text primary key, rollout_path text, title text, archived integer)")
        database.execute(
            "insert into threads values (?, ?, ?, 1)",
            (thread_id, str(rollout_path), "sqlite title"),
        )
        database.commit()
    finally:
        database.close()
    session_index_path = codex_home / "session_index.jsonl"
    session_index_path.write_text(
        "\n".join(
            [
                json.dumps({"id": thread_id, "thread_name": "visible title", "updated_at": "2026-07-11T02:00:00Z"}),
                json.dumps({"id": thread_id, "thread_name": "stale title", "updated_at": "2026-07-10T02:00:00Z"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    global_state_path = codex_home / ".codex-global-state.json"
    global_state_path.write_text(
        json.dumps(
            {
                "pinned-thread-ids": [thread_id],
                "thread-workspace-root-hints": {thread_id: "C:/archived"},
            }
        ),
        encoding="utf-8",
    )
    original_session_index = session_index_path.read_bytes()
    original_global_state = global_state_path.read_bytes()
    rollout_scan = apply_codex_offline_repair.scan_rollout(rollout_path)
    row = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "archived": 1,
        "audited_size": rollout_path.stat().st_size,
        "audited_mtime_ns": rollout_path.stat().st_mtime_ns,
        "audited_sha256": rollout_scan.source_sha256,
        "scan": rollout_scan.to_dict(),
    }
    audit = {
        "active_performance_repair_candidates": [],
        "active_compatibility_repair_candidates": [],
        "active_blocked": [],
        "archived_performance_repair_candidates": [],
        "archived_compatibility_repair_candidates": [],
        "archived_blocked": [],
        "missing_rollouts": [],
        "shared_rollouts": [],
        "threads": [row],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    run_root = tmp_path / "run"

    manifest = apply_codex_offline_repair.apply_repairs(
        audit_path=audit_path,
        backup_root=run_root / "repair_data",
        codex_home=codex_home,
        include_archived=True,
        expected_audit_sha256=apply_codex_offline_repair.file_sha256(audit_path),
        runner_run_id="default-mutation-test",
        run_root=run_root,
    )

    database = sqlite3.connect(state_path)
    try:
        assert database.execute("select title from threads where id = ?", (thread_id,)).fetchone()[0] == "sqlite title"
    finally:
        database.close()
    assert session_index_path.read_bytes() == original_session_index
    assert global_state_path.read_bytes() == original_global_state
    assert manifest["mutation_policy"]["allowlist"] == []
    assert manifest["title_sync"]["status"] == "previewed_not_allowed"
    assert manifest["archived_global_state_cleanup"]["status"] == "previewed_not_allowed"
    assert manifest["mutation_preview"]["title_sync"]["mutation_required"] is True
    assert manifest["mutation_preview"]["title_sync"]["allowed"] is False
    assert manifest["mutation_preview"]["archived_global_state_cleanup"]["mutation_required"] is True
    assert manifest["mutation_preview"]["archived_global_state_cleanup"]["allowed"] is False
    assert apply_codex_offline_repair.scan_rollout(rollout_path).user_prompt_sha256 == rollout_scan.user_prompt_sha256

    explicit_audit_path = tmp_path / "explicit-audit.json"
    explicit_audit_path.write_text(json.dumps(bind_audit(audit, codex_home)), encoding="utf-8")
    explicit_run_root = tmp_path / "explicit-run"
    explicit_manifest = apply_codex_offline_repair.apply_repairs(
        audit_path=explicit_audit_path,
        backup_root=explicit_run_root / "repair_data",
        codex_home=codex_home,
        include_archived=True,
        expected_audit_sha256=apply_codex_offline_repair.file_sha256(explicit_audit_path),
        runner_run_id="explicit-mutation-test",
        run_root=explicit_run_root,
        mutation_allowlist=[
            apply_codex_offline_repair.mutation_title_sync,
            apply_codex_offline_repair.mutation_archived_global_state_cleanup,
        ],
    )

    database = sqlite3.connect(state_path)
    try:
        assert database.execute("select title from threads where id = ?", (thread_id,)).fetchone()[0] == "visible title"
    finally:
        database.close()
    assert len(session_index_path.read_text(encoding="utf-8").splitlines()) == 1
    assert thread_id not in json.dumps(json.loads(global_state_path.read_text(encoding="utf-8")))
    assert explicit_manifest["title_sync"]["status"] == "committed"
    assert explicit_manifest["archived_global_state_cleanup"]["status"] == "applied"
    assert explicit_manifest["mutation_preview"]["title_sync"]["allowed"] is True
    assert explicit_manifest["mutation_preview"]["archived_global_state_cleanup"]["allowed"] is True
    assert apply_codex_offline_repair.scan_rollout(rollout_path).user_prompt_sha256 == rollout_scan.user_prompt_sha256


def test_read_session_index_titles_uses_newest_timestamp_and_reports_duplicates(tmp_path: Path) -> None:
    index_path = tmp_path / "session_index.jsonl"
    index_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "thread-a", "thread_name": "newer", "updated_at": "2026-07-11T02:00:00Z"}),
                json.dumps({"id": "thread-a", "thread_name": "older", "updated_at": "2026-07-10T02:00:00Z"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = apply_codex_offline_repair.read_session_index_titles(index_path)

    assert result["titles"] == {"thread-a": "newer"}
    assert result["duplicate_thread_ids"] == ["thread-a"]


def test_state_backup_preserves_exact_wal_shm_and_journal_sidecars(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    backup_root = tmp_path / "backup"
    payloads = {
        "state_5.sqlite": b"main-db",
        "state_5.sqlite-wal": b"wal",
        "state_5.sqlite-shm": b"shm",
        "state_5.sqlite-journal": b"journal",
    }
    for name, payload in payloads.items():
        (codex_home / name).write_bytes(payload)

    copied = apply_codex_offline_repair.backup_codex_state(
        codex_home,
        backup_root,
        verify_sqlite=False,
        precommit_guard=lambda: None,
    )

    assert {Path(item["source"]).name for item in copied} == set(payloads)
    for name, payload in payloads.items():
        assert (backup_root / "state" / name).read_bytes() == payload


def test_validate_audit_contract_rejects_wrong_state_and_threshold_policy(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    state_path = codex_home / "state_5.sqlite"
    create_sqlite(state_path)
    audit = {
        "schema_version": apply_codex_offline_repair.audit_schema_version,
        "generated_at_epoch": 1,
        "state": {
            "path": str(tmp_path / "wrong.sqlite"),
            "size": state_path.stat().st_size,
            "mtime_ns": state_path.stat().st_mtime_ns,
            "sha256": apply_codex_offline_repair.file_sha256(state_path),
            "files": apply_codex_offline_repair.state_file_contracts(state_path),
        },
        "policy": {
            "name": "codex-thread-history-repair",
            "version": apply_codex_offline_repair.audit_policy_version,
            "thresholds": {"max_active_bytes": 999999999999, "max_active_lines": 999999999, "max_tail_lines": 999999999},
        },
        "threads": [],
    }

    with pytest.raises(RuntimeError, match="state path"):
        apply_codex_offline_repair.validate_audit_contract(audit, codex_home, now_epoch=1)

    audit["state"]["path"] = str(state_path)
    with pytest.raises(RuntimeError, match="threshold policy"):
        apply_codex_offline_repair.validate_audit_contract(audit, codex_home, now_epoch=1)


def test_validate_audit_contract_rejects_sidecar_appearance_after_audit(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    state_path = codex_home / "state_5.sqlite"
    create_sqlite(state_path)
    audit = bind_audit({"threads": []}, codex_home)

    Path(str(state_path) + "-journal").write_bytes(b"appeared-after-audit")

    with pytest.raises(RuntimeError, match="sidecar changed since audit: journal"):
        apply_codex_offline_repair.validate_audit_contract(audit, codex_home)


def test_validate_audit_file_hash_rejects_changed_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text('{"schema_version":3}', encoding="utf-8")
    original_hash = apply_codex_offline_repair.file_sha256(audit_path)
    audit_path.write_text('{"schema_version":3,"tampered":true}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="audit file hash"):
        apply_codex_offline_repair.validate_audit_file_hash(audit_path, original_hash)


def test_load_bound_audit_hashes_and_parses_the_same_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_path = tmp_path / "audit.json"
    original_bytes = b'{"schema_version":4,"marker":"bound"}'
    audit_path.write_bytes(original_bytes)
    expected_hash = apply_codex_offline_repair.file_sha256(audit_path)
    original_read_bytes = Path.read_bytes
    read_count = 0

    def read_bytes_once(path: Path) -> bytes:
        nonlocal read_count
        if path == audit_path:
            read_count += 1
            result = original_read_bytes(path)
            audit_path.write_bytes(b'{"schema_version":4,"marker":"replaced"}')
            return result
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes_once)

    audit, actual_hash = apply_codex_offline_repair.load_bound_audit(audit_path, expected_hash)

    assert read_count == 1
    assert actual_hash == expected_hash
    assert audit["marker"] == "bound"


def test_title_sync_rewrites_duplicate_session_index_to_latest_record(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    database = sqlite3.connect(state_path)
    database.execute("create table threads(id text primary key, title text)")
    database.execute("insert into threads values ('thread-a', 'sqlite title')")
    database.commit()
    index_path = tmp_path / "session_index.jsonl"
    index_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "thread-a", "thread_name": "new", "updated_at": "2026-07-11T02:00:00Z"}),
                json.dumps({"id": "thread-a", "thread_name": "old", "updated_at": "2026-07-10T02:00:00Z"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = apply_codex_offline_repair.synchronize_sqlite_titles(database, index_path, precommit_guard=lambda: None)
    database.commit()
    database.close()

    records = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()]
    assert result["deduplicated_count"] == 1
    assert records == [{"id": "thread-a", "thread_name": "new", "updated_at": "2026-07-11T02:00:00Z"}]


def test_title_sync_adds_sqlite_threads_missing_from_session_index_and_preserves_other_records(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    database = sqlite3.connect(state_path)
    database.execute("create table threads(id text primary key, title text, updated_at integer)")
    database.execute("insert into threads values ('thread-a', 'indexed title', 100)")
    database.execute("insert into threads values ('thread-b', 'sqlite only title', 200)")
    database.commit()
    index_path = tmp_path / "session_index.jsonl"
    passthrough = {"type": "metadata", "value": "keep-me"}
    index_path.write_text(
        "\n".join(
            [
                json.dumps(passthrough),
                json.dumps({"id": "thread-a", "thread_name": "indexed title", "updated_at": "1970-01-01T00:01:40Z"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = apply_codex_offline_repair.synchronize_sqlite_titles(database, index_path, precommit_guard=lambda: None)
    database.commit()
    database.close()

    records = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()]
    assert result["added_index_thread_ids"] == ["thread-b"]
    assert records[0] == passthrough
    assert records[-1]["id"] == "thread-b"
    assert records[-1]["thread_name"] == "sqlite only title"


def test_completed_repair_rollback_restores_plugin_snapshot_in_same_transaction(tmp_path: Path, monkeypatch) -> None:
    run_root = tmp_path / "run"
    backup_root = run_root / "repair_data"
    backup_root.mkdir(parents=True)
    plugin_manifest = run_root / "plugin_state_snapshot" / "plugin_state_snapshot.json"
    plugin_manifest.parent.mkdir()
    plugin_manifest.write_text("{}", encoding="utf-8")
    repair_manifest = backup_root / "repair_manifest.json"
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    payload = {
        "status": "pending_restart_validation",
        "runner_run_id": "test-run",
        "run_root": str(run_root),
        "codex_home": str(codex_home),
        "state_backups": [],
        "state_absent_paths": [],
        "thread_repair_journal": [],
        "log_archive_plan": [],
        "plugin_snapshot_manifest": str(plugin_manifest),
        "plugin_snapshot_manifest_sha256": apply_codex_offline_repair.file_sha256(plugin_manifest),
    }
    manifest_sha256 = apply_codex_offline_repair.write_manifest_pair(repair_manifest, payload)
    calls: list[Path] = []
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)
    monkeypatch.setattr(apply_codex_offline_repair, "assert_backup_path", lambda _path, _root=None: None)
    monkeypatch.setattr(
        apply_codex_offline_repair,
        "restore_plugin_state",
        lambda path, _root, **_kwargs: calls.append(Path(path)) or {"status": "complete", "errors": []},
    )

    result = apply_codex_offline_repair.rollback_completed_repair(
        repair_manifest,
        expected_run_id="test-run",
        expected_run_root=run_root,
        expected_codex_home=codex_home,
        expected_manifest_sha256=manifest_sha256,
    )

    assert result["plugins"]["status"] == "complete"
    assert calls == [plugin_manifest]


def test_abandoned_rollback_preserves_runtime_state_during_plugin_restore(tmp_path: Path, monkeypatch) -> None:
    run_root = tmp_path / "run"
    backup_root = run_root / "repair_data"
    backup_root.mkdir(parents=True)
    plugin_manifest = run_root / "plugin_state_snapshot" / "plugin_state_snapshot.json"
    plugin_manifest.parent.mkdir()
    plugin_manifest.write_text("{}", encoding="utf-8")
    repair_manifest = backup_root / "repair_manifest.json"
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    payload = {
        "status": "failed",
        "runner_run_id": "test-run",
        "run_root": str(run_root),
        "codex_home": str(codex_home),
        "state_backups": [],
        "state_absent_paths": [],
        "thread_repair_journal": [],
        "log_archive_plan": [],
        "plugin_snapshot_manifest": str(plugin_manifest),
        "plugin_snapshot_manifest_sha256": apply_codex_offline_repair.file_sha256(plugin_manifest),
    }
    manifest_sha256 = apply_codex_offline_repair.write_manifest_pair(repair_manifest, payload)
    captured: dict[str, object] = {}
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    def capture_restore(_path, _root, **keyword_arguments):
        captured.update(keyword_arguments)
        return {"status": "complete", "errors": []}

    monkeypatch.setattr(apply_codex_offline_repair, "restore_plugin_state", capture_restore)

    apply_codex_offline_repair.rollback_completed_repair(
        repair_manifest,
        expected_run_id="test-run",
        expected_run_root=run_root,
        expected_codex_home=codex_home,
        expected_manifest_sha256=manifest_sha256,
        preserve_runtime_state=True,
    )

    assert captured["skip_relative_paths"] == apply_codex_offline_repair.runtime_state_snapshot_relative_paths


def test_rollback_validation_accepts_every_file_captured_by_state_backup(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    repair_data = run_root / "repair_data"
    repair_data.mkdir(parents=True)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    state_names = [
        "state_5.sqlite",
        "state_5.sqlite-wal",
        "state_5.sqlite-shm",
        "state_5.sqlite-journal",
        ".codex-global-state.json",
        ".codex-global-state.json.bak",
        "session_index.jsonl",
        "config.toml",
        "managed_config.toml",
        "AGENTS.md",
    ]
    payload = {
        "runner_run_id": "test-run",
        "run_root": str(run_root),
        "codex_home": str(codex_home),
        "state_backups": [
            {"source": str(codex_home / name), "backup": str(repair_data / "state" / name)}
            for name in state_names
        ],
        "state_absent_paths": [str(codex_home / "state_5.sqlite-journal")],
    }

    apply_codex_offline_repair.validate_rollback_manifest_paths(
        repair_data / "repair_manifest.json",
        payload,
        "test-run",
        run_root,
        codex_home,
    )


def test_state_rollback_resume_does_not_overwrite_post_restart_state(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    backup_root = tmp_path / "repair_data"
    backup_path = backup_root / "state" / "state_5.sqlite"
    backup_path.parent.mkdir(parents=True)
    backup_path.write_bytes(b"pre-repair-state")
    source_path = codex_home / "state_5.sqlite"
    source_path.write_bytes(b"post-restart-state-with-new-writes")
    backup_sha256 = apply_codex_offline_repair.file_sha256(backup_path)
    previous_restore = {
        "source": str(source_path),
        "backup": str(backup_path),
        "restored_sha256": backup_sha256,
    }

    result = apply_codex_offline_repair.restore_state_database_snapshot(
        state_backups=[
            {
                "source": str(source_path),
                "backup": str(backup_path),
                "backup_sha256": backup_sha256,
            }
        ],
        state_absent_paths=[],
        backup_root=backup_root,
        precommit_guard=lambda: None,
        existing_restores=[previous_restore],
    )

    assert source_path.read_bytes() == b"post-restart-state-with-new-writes"
    assert result["restores"] == []
    assert result["skipped_previous_restores"] == [previous_restore]
    assert result["errors"] == []


def test_completed_rollback_resume_preserves_state_written_after_restart(tmp_path: Path, monkeypatch) -> None:
    run_root = tmp_path / "run"
    repair_data = run_root / "repair_data"
    state_backup_root = repair_data / "state"
    state_backup_root.mkdir(parents=True)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    backup_path = state_backup_root / "state_5.sqlite"
    backup_path.write_bytes(b"pre-repair-state")
    source_path = codex_home / "state_5.sqlite"
    source_path.write_bytes(b"post-restart-state-with-new-writes")
    backup_sha256 = apply_codex_offline_repair.file_sha256(backup_path)
    previous_restore = {
        "source": str(source_path),
        "backup": str(backup_path),
        "restored_sha256": backup_sha256,
    }
    payload = {
        "schema_version": 1,
        "status": "failed",
        "runner_run_id": "test-run",
        "run_root": str(run_root),
        "codex_home": str(codex_home),
        "state_backups": [
            {
                "source": str(source_path),
                "backup": str(backup_path),
                "backup_sha256": backup_sha256,
            }
        ],
        "state_absent_paths": [],
        "thread_repair_journal": [],
        "log_archive_plan": [],
        "rollback": {"state": {"restores": [previous_restore]}},
    }
    manifest_path = repair_data / "repair_manifest.json"
    manifest_sha256 = apply_codex_offline_repair.write_manifest_pair(manifest_path, payload)
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    result = apply_codex_offline_repair.rollback_completed_repair(
        manifest_path,
        expected_run_id="test-run",
        expected_run_root=run_root,
        expected_codex_home=codex_home,
        expected_manifest_sha256=manifest_sha256,
    )

    assert source_path.read_bytes() == b"post-restart-state-with-new-writes"
    assert result["state"]["skipped_previous_restores"] == [previous_restore]
    assert result["state"]["restores"] == [previous_restore]
    committed_manifest, _manifest_sha256 = apply_codex_offline_repair.load_manifest_pair(manifest_path)
    assert committed_manifest["status"] == "rolled_back"


def test_rollback_rejects_manifest_target_outside_bound_codex_home(tmp_path: Path, monkeypatch) -> None:
    run_root = tmp_path / "run"
    repair_data = run_root / "repair_data"
    repair_data.mkdir(parents=True)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    outside_source = tmp_path / "outside.jsonl"
    outside_source.write_text("outside", encoding="utf-8")
    backup_path = repair_data / "rollouts" / "thread-a" / "outside.jsonl"
    backup_path.parent.mkdir(parents=True)
    backup_path.write_text("outside", encoding="utf-8")
    payload = {
        "schema_version": 1,
        "status": "pending_restart_validation",
        "runner_run_id": "test-run",
        "run_root": str(run_root),
        "codex_home": str(codex_home),
        "state_backups": [],
        "state_absent_paths": [],
        "log_archive_plan": [],
        "thread_repair_journal": [
            {
                "thread_id": "thread-a",
                "source_path": str(outside_source),
                "backup_path": str(backup_path),
                "original_sha256": apply_codex_offline_repair.file_sha256(backup_path),
            }
        ],
    }
    manifest_path = repair_data / "repair_manifest.json"
    manifest_sha256 = apply_codex_offline_repair.write_manifest_pair(manifest_path, payload)
    monkeypatch.setattr(apply_codex_offline_repair, "assert_codex_offline", lambda: None)

    with pytest.raises(RuntimeError, match="rollout source escapes"):
        apply_codex_offline_repair.rollback_completed_repair(
            manifest_path,
            expected_run_id="test-run",
            expected_run_root=run_root,
            expected_codex_home=codex_home,
            expected_manifest_sha256=manifest_sha256,
        )

    assert outside_source.read_text(encoding="utf-8") == "outside"
