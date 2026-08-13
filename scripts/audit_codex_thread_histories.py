from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "codex_home_manager"))

from backend.thread_history_repair import RepairThresholds, build_repair_plan, scan_rollout
from backend.windows_paths import windows_path_key


audit_schema_version = 5
audit_policy_name = "codex-thread-history-repair"
audit_policy_version = 2
state_file_suffixes = {"main": "", "wal": "-wal", "shm": "-shm", "journal": "-journal"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def state_file_contracts(state_path: Path) -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    for name, suffix in state_file_suffixes.items():
        path = Path(str(state_path) + suffix)
        item: dict[str, Any] = {"path": str(path.resolve()), "exists": path.is_file()}
        if path.is_file():
            file_stat = path.stat()
            item.update(
                {
                    "size": file_stat.st_size,
                    "mtime_ns": file_stat.st_mtime_ns,
                    "sha256": file_sha256(path),
                }
            )
        contracts[name] = item
    return contracts


def read_threads_from_database(database: sqlite3.Connection) -> list[dict[str, Any]]:
    database.row_factory = sqlite3.Row
    rows = database.execute(
        """
        select id, rollout_path, created_at, updated_at, source, cwd, title,
               archived, cli_version, agent_nickname, agent_role, thread_source,
               history_mode
        from threads
        order by archived asc, updated_at desc
        """
    ).fetchall()
    return [dict(row) for row in rows]


def assert_stable_source_database_contracts(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> None:
    for name in ("main", "wal", "journal"):
        if before[name] != after[name]:
            raise RuntimeError(f"state database or durable sidecar changed while creating audit snapshot: {name}")


def snapshot_thread_rows(
    state_path: Path,
    snapshot_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    if snapshot_path.exists():
        raise RuntimeError(f"audit state snapshot already exists: {snapshot_path}")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    before = state_file_contracts(state_path)
    source_uri = f"file:{state_path.as_posix()}?mode=ro"
    source_database = sqlite3.connect(source_uri, uri=True)
    snapshot_database = sqlite3.connect(snapshot_path)
    try:
        source_database.backup(snapshot_database)
        snapshot_database.execute("pragma journal_mode=delete").fetchone()
        quick_check = snapshot_database.execute("pragma quick_check").fetchone()
        if quick_check is None or str(quick_check[0]).casefold() != "ok":
            raise RuntimeError(f"audit state snapshot quick_check failed: {quick_check}")
        rows = read_threads_from_database(snapshot_database)
        snapshot_database.commit()
    finally:
        snapshot_database.close()
        source_database.close()
    after = state_file_contracts(state_path)
    assert_stable_source_database_contracts(before, after)
    snapshot_contract = {
        "path": str(snapshot_path.resolve()),
        "size": snapshot_path.stat().st_size,
        "sha256": file_sha256(snapshot_path),
        "quick_check": "ok",
    }
    return rows, after, snapshot_contract


def audit_threads(state_path: Path, report_path: Path) -> dict[str, Any]:
    state_path = state_path.resolve()
    started_at = time.time()
    snapshot_path = report_path.with_name(f"{report_path.stem}.state-snapshot.sqlite")
    thread_rows, initial_state_files, snapshot_contract = snapshot_thread_rows(state_path, snapshot_path)
    audited_rows: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    seen_paths: dict[str, dict[str, Any]] = {}
    total = len(thread_rows)

    for index, thread_row in enumerate(thread_rows, 1):
        rollout_path = Path(thread_row["rollout_path"])
        normalized_path = windows_path_key(rollout_path)
        if normalized_path in seen_paths:
            shared = dict(thread_row)
            shared["shared_rollout_with"] = seen_paths[normalized_path]["id"]
            shared["scan"] = seen_paths[normalized_path]["scan"]
            shared["audited_size"] = seen_paths[normalized_path]["audited_size"]
            shared["audited_mtime_ns"] = seen_paths[normalized_path]["audited_mtime_ns"]
            shared["audited_sha256"] = seen_paths[normalized_path]["audited_sha256"]
            audited_rows.append(shared)
            continue
        if not rollout_path.is_file():
            missing_rows.append(dict(thread_row))
            continue

        file_stat = rollout_path.stat()
        scan = scan_rollout(rollout_path)
        audited_row = dict(thread_row)
        audited_row["scan"] = scan.to_dict()
        audited_row["audited_size"] = file_stat.st_size
        audited_row["audited_mtime_ns"] = file_stat.st_mtime_ns
        audited_row["audited_sha256"] = scan.source_sha256
        audited_rows.append(audited_row)
        seen_paths[normalized_path] = audited_row
        if index == 1 or index % 10 == 0 or index == total:
            elapsed = time.time() - started_at
            print(f"AUDIT {index}/{total} unique={len(seen_paths)} elapsed={elapsed:.1f}s", flush=True)

    thresholds = RepairThresholds()
    performance, compatibility, blocked = build_repair_plan(
        audited_rows,
        thresholds=thresholds,
        include_archived=False,
    )
    performance_all, compatibility_all, blocked_all = build_repair_plan(
        audited_rows,
        thresholds=thresholds,
        include_archived=True,
    )
    performance_archived = [row for row in performance_all if bool(row.get("archived"))]
    compatibility_archived = [row for row in compatibility_all if bool(row.get("archived"))]
    blocked_archived = [row for row in blocked_all if bool(row.get("archived"))]
    hard_block_reasons = {
        "shared_rollout_path",
        "source_parse_errors",
        "missing_session_meta_id",
        "session_meta_id_mismatch",
    }
    active_hard_blocked = [
        row for row in blocked if str(row.get("repair_block_reason") or "") in hard_block_reasons
    ]
    archived_hard_blocked = [
        row for row in blocked_archived if str(row.get("repair_block_reason") or "") in hard_block_reasons
    ]
    shared_rollouts = [row for row in audited_rows if row.get("shared_rollout_with")]
    parser_error_counts = Counter()
    total_bytes = 0
    json_parse_errors = 0
    for row in audited_rows:
        scan = row["scan"]
        total_bytes += int(scan["total_bytes"])
        json_parse_errors += int(scan["parse_errors"])
        parser_error_counts["legacy_tools_summary"] += int(scan["tools_summary_output_count"])
        parser_error_counts["unknown_thread_name_updated"] += int(scan["thread_name_updated_count"])
        parser_error_counts["invalid_image_url"] += int(scan["invalid_image_url_count"])

    if state_file_contracts(state_path) != initial_state_files:
        raise RuntimeError("state database or sidecar changed during the full audit")

    main_state = initial_state_files["main"]

    report = {
        "schema_version": audit_schema_version,
        "generated_at_epoch": int(time.time()),
        "state_path": str(state_path),
        "duration_seconds": round(time.time() - started_at, 3),
        "thresholds": {
            "max_active_bytes": thresholds.max_active_bytes,
            "max_active_lines": thresholds.max_active_lines,
            "max_tail_lines": thresholds.max_tail_lines,
        },
        "policy": {
            "name": audit_policy_name,
            "version": audit_policy_version,
            "thresholds": {
                "max_active_bytes": thresholds.max_active_bytes,
                "max_active_lines": thresholds.max_active_lines,
                "max_tail_lines": thresholds.max_tail_lines,
            },
        },
        "state": {
            "path": str(state_path),
            "size": main_state["size"],
            "mtime_ns": main_state["mtime_ns"],
            "sha256": main_state["sha256"],
            "files": initial_state_files,
            "read_snapshot": snapshot_contract,
        },
        "summary": {
            "database_thread_count": len(thread_rows),
            "audited_thread_count": len(audited_rows),
            "unique_rollout_count": len(seen_paths),
            "missing_rollout_count": len(missing_rows),
            "active_performance_repair_candidate_count": len(performance),
            "active_compatibility_repair_candidate_count": len(compatibility),
            "active_blocked_count": len(blocked),
            "active_hard_blocked_count": len(active_hard_blocked),
            "archived_performance_repair_candidate_count": len(performance_archived),
            "archived_compatibility_repair_candidate_count": len(compatibility_archived),
            "archived_blocked_count": len(blocked_archived),
            "archived_hard_blocked_count": len(archived_hard_blocked),
            "shared_rollout_mapping_count": len(shared_rollouts),
            "total_rollout_bytes": total_bytes,
            "json_parse_error_count": json_parse_errors,
            "estimated_current_parser_errors": sum(parser_error_counts.values()),
            "parser_error_breakdown": dict(parser_error_counts),
        },
        "active_performance_repair_candidates": performance,
        "active_compatibility_repair_candidates": compatibility,
        "active_blocked": blocked,
        "archived_performance_repair_candidates": performance_archived,
        "archived_compatibility_repair_candidates": compatibility_archived,
        "archived_blocked": blocked_archived,
        "shared_rollouts": shared_rollouts,
        "missing_rollouts": missing_rows,
        "largest_rollouts": sorted(
            audited_rows,
            key=lambda row: int(row["scan"]["total_bytes"]),
            reverse=True,
        )[:30],
        "threads": audited_rows,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Codex rollout histories without modifying them.")
    parser.add_argument("--state", type=Path, default=Path(r"D:\.codex\state_5.sqlite"))
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    report = audit_threads(arguments.state, arguments.report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    print(f"REPORT {arguments.report.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
