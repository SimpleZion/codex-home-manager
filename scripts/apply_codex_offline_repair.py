from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "codex_home_manager"))

from backend.thread_history_repair import (
    RepairThresholds,
    repair_rollout_in_place,
    repair_rollout_compatibility_in_place,
    scan_rollout,
    validate_user_prompt_contract,
)
from backend.offline_repair_policy import (
    assert_backup_path,
    assert_codex_offline,
    default_backup_root,
    related_codex_processes,
)
from backend.windows_paths import (
    canonical_path,
    strip_windows_extended_prefix,
    windows_extended_path,
    windows_path_is_within,
    windows_path_key,
)
from codex_plugin_state_snapshot import restore_plugin_state
from repair_manifest_chain import load_manifest_pair, write_manifest_pair


audit_schema_version = 5
audit_policy_name = "codex-thread-history-repair"
audit_policy_version = 2
max_audit_age_seconds = 15 * 60
state_contract_file_suffixes = {"main": "", "wal": "-wal", "shm": "-shm", "journal": "-journal"}
runtime_state_snapshot_relative_paths = {
    "state_5.sqlite",
    "state_5.sqlite-wal",
    "state_5.sqlite-shm",
    "state_5.sqlite-journal",
    ".codex-global-state.json",
    ".codex-global-state.json.bak",
    "session_index.jsonl",
}
mutation_title_sync = "title_sync"
mutation_archived_global_state_cleanup = "archived_global_state_cleanup"
supported_state_mutations = frozenset(
    {
        mutation_title_sync,
        mutation_archived_global_state_cleanup,
    }
)


def normalize_mutation_allowlist(mutations: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_mutation in mutations or []:
        mutation = str(raw_mutation or "").strip()
        if mutation not in supported_state_mutations:
            raise RuntimeError(f"unsupported offline state mutation: {mutation or '<empty>'}")
        if mutation in seen:
            continue
        seen.add(mutation)
        normalized.append(mutation)
    return normalized


def atomic_replace(source: Path, destination: Path) -> None:
    last_error: Exception | None = None
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            last_error = error
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError(f"could not atomically replace path after retries: {source} -> {destination}") from last_error


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    if path.name == "repair_manifest.json" and manifest.get("runner_run_id") and manifest.get("run_root"):
        write_manifest_pair(path, manifest)
        return
    durable_path = windows_extended_path(path)
    durable_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = durable_path.with_suffix(durable_path.suffix + ".writing")
    with temporary_path.open("w", encoding="utf-8", newline="") as target:
        json.dump(manifest, target, ensure_ascii=False, indent=2)
        target.flush()
        os.fsync(target.fileno())
    atomic_replace(temporary_path, durable_path)
    if path.name == "repair_manifest.json":
        mirror_path = path.with_name("repair_manifest.mirror.json")
        mirror_temporary_path = mirror_path.with_suffix(mirror_path.suffix + ".writing")
        with mirror_temporary_path.open("w", encoding="utf-8", newline="") as target:
            json.dump(manifest, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        atomic_replace(mirror_temporary_path, mirror_path)


codex_processes = related_codex_processes


def current_offline_guard() -> None:
    assert_codex_offline()


def sqlite_quick_check(path: Path) -> str:
    uri = f"file:{path.as_posix()}?mode=ro"
    database = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        rows = database.execute("pragma quick_check").fetchall()
    finally:
        database.close()
    return "; ".join(str(row[0]) for row in rows)


def backup_codex_state(
    codex_home: Path,
    backup_root: Path,
    verify_sqlite: bool = True,
    precommit_guard: Callable[[], None] = current_offline_guard,
) -> list[dict[str, Any]]:
    state_directory = backup_root / "state"
    state_directory.mkdir(parents=True, exist_ok=True)
    names = [
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
    copied: list[dict[str, Any]] = []
    for name in names:
        source_path = codex_home / name
        if not source_path.is_file():
            continue
        precommit_guard()
        destination_path = state_directory / name
        source_stat = source_path.stat()
        source_sha256 = file_sha256(source_path)
        backup_method = "exact_copy2"
        shutil.copy2(source_path, destination_path)
        backup_sha256 = file_sha256(destination_path)
        if source_sha256 != backup_sha256:
            raise RuntimeError(f"state backup hash mismatch: {source_path}")
        precommit_guard()
        final_source_stat = source_path.stat()
        if (
            final_source_stat.st_size != source_stat.st_size
            or final_source_stat.st_mtime_ns != source_stat.st_mtime_ns
            or file_sha256(source_path) != source_sha256
        ):
            raise RuntimeError(f"state source changed during backup: {source_path}")
        copied.append(
            {
                "source": str(source_path),
                "backup": str(destination_path),
                "bytes": destination_path.stat().st_size,
                "source_sha256": source_sha256,
                "backup_sha256": backup_sha256,
                "backup_method": backup_method,
            }
        )
    precommit_guard()
    for item in copied:
        source_path = Path(item["source"])
        if not source_path.is_file() or file_sha256(source_path) != item["source_sha256"]:
            raise RuntimeError(f"state backup set became inconsistent: {source_path}")
    if verify_sqlite and (state_directory / "state_5.sqlite").is_file():
        quick_check = sqlite_quick_check(state_directory / "state_5.sqlite")
        if quick_check != "ok":
            raise RuntimeError(f"state exact backup quick_check failed: {quick_check}")
    return copied


def read_session_index_titles(path: Path) -> dict[str, Any]:
    selected: dict[str, tuple[str, str, int, dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    all_records: list[tuple[int, dict[str, Any], str]] = []
    if not path.is_file():
        return {"titles": {}, "records": [], "duplicate_thread_ids": []}
    with path.open("r", encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"session_index JSON error at line {line_number}: {error}") from error
            thread_id = str(item.get("id") or "") if isinstance(item, dict) else ""
            title = str(item.get("thread_name") or "") if isinstance(item, dict) else ""
            updated_at = str(item.get("updated_at") or "") if isinstance(item, dict) else ""
            all_records.append((line_number, item, thread_id if thread_id and title else ""))
            if thread_id and title:
                counts[thread_id] = counts.get(thread_id, 0) + 1
                candidate = (updated_at, title, line_number, item)
                current = selected.get(thread_id)
                if current is None or (candidate[0], candidate[2]) > (current[0], current[2]):
                    selected[thread_id] = candidate
    selected_lines = {thread_id: value[2] for thread_id, value in selected.items()}
    ordered_records = [
        item
        for line_number, item, thread_id in all_records
        if not thread_id or selected_lines.get(thread_id) == line_number
    ]
    return {
        "titles": {thread_id: value[1] for thread_id, value in selected.items()},
        "records": ordered_records,
        "duplicate_thread_ids": sorted(thread_id for thread_id, count in counts.items() if count > 1),
    }


def build_sqlite_title_sync_plan(database: sqlite3.Connection, session_index_path: Path) -> dict[str, Any]:
    columns = {str(row[1]) for row in database.execute("pragma table_info(threads)").fetchall()}
    if "title" not in columns:
        return {
            "source": str(session_index_path),
            "title_column_present": False,
            "sqlite_updates": [],
            "index_records": [],
            "added_index_records": [],
            "duplicate_thread_ids": [],
            "index_rewrite_required": False,
        }
    title_index = read_session_index_titles(session_index_path)
    titles = title_index["titles"]
    sqlite_columns = ["id", "title"] + (["updated_at"] if "updated_at" in columns else [])
    sqlite_rows = database.execute(f"select {', '.join(sqlite_columns)} from threads order by id").fetchall()
    sqlite_updates: list[dict[str, str]] = []
    effective_rows: list[tuple[Any, ...]] = []
    for row in sqlite_rows:
        thread_id = str(row[0] or "")
        current_title = str(row[1] or "")
        intended_title = str(titles.get(thread_id, current_title))
        if thread_id in titles and current_title != intended_title:
            sqlite_updates.append(
                {
                    "thread_id": thread_id,
                    "before": current_title,
                    "after": intended_title,
                }
            )
        effective_rows.append((thread_id, intended_title, *row[2:]))

    added_index_records: list[dict[str, Any]] = []
    for row in effective_rows:
        thread_id = str(row[0] or "")
        effective_title = str(row[1] or "")
        if not thread_id or not effective_title or thread_id in titles:
            continue
        record: dict[str, Any] = {"id": thread_id, "thread_name": effective_title}
        if "updated_at" in columns and row[2] not in (None, ""):
            updated_at = row[2]
            if isinstance(updated_at, (int, float)):
                record["updated_at"] = datetime.fromtimestamp(float(updated_at), timezone.utc).isoformat().replace("+00:00", "Z")
            else:
                record["updated_at"] = str(updated_at)
        added_index_records.append(record)

    duplicate_thread_ids = title_index["duplicate_thread_ids"]
    return {
        "source": str(session_index_path),
        "title_column_present": True,
        "sqlite_updates": sqlite_updates,
        "effective_title_rows": effective_rows,
        "index_records": title_index["records"],
        "added_index_records": added_index_records,
        "duplicate_thread_ids": duplicate_thread_ids,
        "index_rewrite_required": bool(duplicate_thread_ids or added_index_records),
    }


def preview_sqlite_title_sync(
    database: sqlite3.Connection,
    session_index_path: Path,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = plan or build_sqlite_title_sync_plan(database, session_index_path)
    sqlite_updates = list(plan["sqlite_updates"])
    added_index_records = list(plan["added_index_records"])
    duplicate_thread_ids = list(plan["duplicate_thread_ids"])
    return {
        "status": "previewed",
        "mutation": mutation_title_sync,
        "source": str(session_index_path),
        "title_column_present": bool(plan["title_column_present"]),
        "mutation_required": bool(sqlite_updates or plan["index_rewrite_required"]),
        "sqlite_update_count": len(sqlite_updates),
        "sqlite_updates": sqlite_updates,
        "index_rewrite_required": bool(plan["index_rewrite_required"]),
        "duplicate_thread_ids": duplicate_thread_ids,
        "added_index_thread_ids": [str(record["id"]) for record in added_index_records],
    }


def synchronize_sqlite_titles(
    database: sqlite3.Connection,
    session_index_path: Path,
    precommit_guard: Callable[[], None],
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = plan or build_sqlite_title_sync_plan(database, session_index_path)
    if not plan["title_column_present"]:
        return {
            "status": "prepared",
            "source": str(session_index_path),
            "changed_count": 0,
            "changed_thread_ids": [],
        }
    sqlite_updates = list(plan["sqlite_updates"])
    for update in sqlite_updates:
        database.execute(
            "update threads set title = ? where id = ?",
            (update["after"], update["thread_id"]),
        )
    added_index_records = list(plan["added_index_records"])
    duplicate_thread_ids = list(plan["duplicate_thread_ids"])
    if plan["index_rewrite_required"]:
        precommit_guard()
        temporary_path = session_index_path.with_suffix(session_index_path.suffix + f".{uuid4().hex}.deduplicating")
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("w", encoding="utf-8", newline="") as destination:
            for record in [*plan["index_records"], *added_index_records]:
                destination.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        precommit_guard()
        os.replace(temporary_path, session_index_path)
    final_titles = read_session_index_titles(session_index_path)["titles"]
    for row in plan["effective_title_rows"]:
        thread_id = str(row[0] or "")
        sqlite_title = str(row[1] or "")
        if not sqlite_title:
            continue
        if final_titles.get(thread_id) != sqlite_title:
            raise RuntimeError(f"title synchronization verification failed: {thread_id}")
    return {
        "status": "prepared",
        "source": str(session_index_path),
        "changed_count": len(sqlite_updates),
        "changed_thread_ids": [str(update["thread_id"]) for update in sqlite_updates],
        "duplicate_thread_ids": duplicate_thread_ids,
        "deduplicated_count": sum(1 for _thread_id in duplicate_thread_ids),
        "added_index_thread_ids": [str(record["id"]) for record in added_index_records],
    }


archived_global_state_list_keys = (
    "pinned-thread-ids",
    "projectless-thread-ids",
)
archived_global_state_map_keys = (
    "thread-workspace-root-hints",
    "heartbeat-thread-permissions-by-id",
)


def archived_global_state_references(data: dict[str, Any], archived_thread_ids: set[str]) -> dict[str, list[str]]:
    references: dict[str, list[str]] = {}
    containers: list[tuple[str, dict[str, Any]]] = [("root", data)]
    nested = data.get("electron-persisted-atom-state")
    if isinstance(nested, dict):
        containers.append(("electron-persisted-atom-state", nested))
    for container_name, container in containers:
        for key in archived_global_state_list_keys:
            values = container.get(key)
            if not isinstance(values, list):
                continue
            matched = sorted({str(value) for value in values if str(value) in archived_thread_ids})
            if matched:
                references[f"{container_name}.{key}"] = matched
        for key in archived_global_state_map_keys:
            values = container.get(key)
            if not isinstance(values, dict):
                continue
            matched = sorted(str(thread_id) for thread_id in values if str(thread_id) in archived_thread_ids)
            if matched:
                references[f"{container_name}.{key}"] = matched
    return references


def preview_archived_global_state_cleanup(codex_home: Path, archived_thread_ids: set[str]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    referenced_thread_ids: set[str] = set()
    reference_count = 0
    for file_name in (".codex-global-state.json", ".codex-global-state.json.bak"):
        state_path = codex_home / file_name
        if not state_path.is_file():
            files.append({"path": str(state_path), "status": "missing", "references": {}})
            continue
        try:
            data = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"cannot parse global state preview: {state_path}: {error}") from error
        if not isinstance(data, dict):
            raise RuntimeError(f"global state root is not an object: {state_path}")
        references = archived_global_state_references(data, archived_thread_ids)
        for thread_ids in references.values():
            referenced_thread_ids.update(thread_ids)
            reference_count += len(thread_ids)
        files.append(
            {
                "path": str(state_path),
                "status": "previewed",
                "sha256_before": file_sha256(state_path),
                "references": references,
            }
        )
    return {
        "status": "previewed",
        "mutation": mutation_archived_global_state_cleanup,
        "archived_thread_count": len(archived_thread_ids),
        "mutation_required": reference_count > 0,
        "reference_count": reference_count,
        "referenced_thread_ids": sorted(referenced_thread_ids),
        "files": files,
    }


def cleanup_archived_global_state_references(
    codex_home: Path,
    archived_thread_ids: set[str],
    precommit_guard: Callable[[], None] = current_offline_guard,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "applied",
        "archived_thread_count": len(archived_thread_ids),
        "files": [],
        "removed_reference_count": 0,
        "removed_thread_ids": [],
    }
    removed_thread_ids: set[str] = set()
    for file_name in (".codex-global-state.json", ".codex-global-state.json.bak"):
        state_path = codex_home / file_name
        if not state_path.is_file():
            result["files"].append({"path": str(state_path), "status": "missing", "removed": {}})
            continue
        precommit_guard()
        try:
            data = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"cannot parse global state before archived reference cleanup: {state_path}: {error}") from error
        if not isinstance(data, dict):
            raise RuntimeError(f"global state root is not an object: {state_path}")
        before_references = archived_global_state_references(data, archived_thread_ids)
        containers = [data]
        nested = data.get("electron-persisted-atom-state")
        if isinstance(nested, dict):
            containers.append(nested)
        removed_by_key: dict[str, int] = {}
        for container in containers:
            for key in archived_global_state_list_keys:
                values = container.get(key)
                if not isinstance(values, list):
                    continue
                next_values = [value for value in values if str(value) not in archived_thread_ids]
                removed_count = len(values) - len(next_values)
                if removed_count:
                    container[key] = next_values
                    removed_by_key[key] = removed_by_key.get(key, 0) + removed_count
            for key in archived_global_state_map_keys:
                values = container.get(key)
                if not isinstance(values, dict):
                    continue
                matched_keys = [thread_id for thread_id in values if str(thread_id) in archived_thread_ids]
                for thread_id in matched_keys:
                    values.pop(thread_id, None)
                if matched_keys:
                    container[key] = values
                    removed_by_key[key] = removed_by_key.get(key, 0) + len(matched_keys)
        removed_count = sum(removed_by_key.values())
        if removed_count:
            temporary_path = state_path.with_suffix(state_path.suffix + f".{uuid4().hex}.archived-cleanup")
            with temporary_path.open("w", encoding="utf-8", newline="") as destination:
                destination.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
                destination.flush()
                os.fsync(destination.fileno())
            precommit_guard()
            atomic_replace(temporary_path, state_path)
        final_data = json.loads(state_path.read_text(encoding="utf-8-sig"))
        remaining_references = archived_global_state_references(final_data, archived_thread_ids)
        if remaining_references:
            raise RuntimeError(f"archived global state references remain after cleanup: {state_path}: {remaining_references}")
        for thread_ids in before_references.values():
            removed_thread_ids.update(thread_ids)
        result["removed_reference_count"] += removed_count
        result["files"].append(
            {
                "path": str(state_path),
                "status": "updated" if removed_count else "unchanged",
                "removed": removed_by_key,
                "remaining": remaining_references,
                "sha256_after": file_sha256(state_path),
            }
        )
    result["removed_thread_ids"] = sorted(removed_thread_ids)
    return result


def expected_threshold_policy() -> dict[str, int]:
    thresholds = RepairThresholds()
    return {
        "max_active_bytes": thresholds.max_active_bytes,
        "max_active_lines": thresholds.max_active_lines,
        "max_tail_lines": thresholds.max_tail_lines,
    }


def validate_audit_contract(audit: dict[str, Any], codex_home: Path, now_epoch: int | None = None) -> None:
    if int(audit.get("schema_version") or 0) != audit_schema_version:
        raise RuntimeError(f"audit schema must be {audit_schema_version}")
    state_path = (codex_home / "state_5.sqlite").resolve()
    state = audit.get("state") if isinstance(audit.get("state"), dict) else {}
    if windows_path_key(state.get("path") or "") != windows_path_key(state_path):
        raise RuntimeError("audit state path does not match CODEX_HOME state database")
    policy = audit.get("policy") if isinstance(audit.get("policy"), dict) else {}
    if policy.get("name") != audit_policy_name or int(policy.get("version") or 0) != audit_policy_version:
        raise RuntimeError("audit policy identity does not match the repair implementation")
    if policy.get("thresholds") != expected_threshold_policy():
        raise RuntimeError("audit threshold policy does not match the fixed repair policy")
    generated_at = int(audit.get("generated_at_epoch") or 0)
    current_epoch = int(time.time()) if now_epoch is None else now_epoch
    if generated_at <= 0 or current_epoch - generated_at > max_audit_age_seconds or generated_at > current_epoch + 60:
        raise RuntimeError("audit is stale or has an invalid generation time")
    state_stat = state_path.stat()
    if int(state.get("size") or -1) != state_stat.st_size or int(state.get("mtime_ns") or -1) != state_stat.st_mtime_ns:
        raise RuntimeError("state database changed since audit")
    if str(state.get("sha256") or "") != file_sha256(state_path):
        raise RuntimeError("state database hash changed since audit")
    audited_state_files = state.get("files") if isinstance(state.get("files"), dict) else None
    if audited_state_files is None:
        raise RuntimeError("audit state sidecar contract is missing")
    current_state_files = state_file_contracts(state_path)
    if set(audited_state_files) != set(current_state_files):
        raise RuntimeError("audit state sidecar contract is incomplete")
    for name, current_item in current_state_files.items():
        audited_item = audited_state_files.get(name)
        if not isinstance(audited_item, dict):
            raise RuntimeError(f"audit state sidecar contract is invalid: {name}")
        if windows_path_key(audited_item.get("path") or "") != windows_path_key(current_item["path"]):
            raise RuntimeError(f"audit state sidecar path changed: {name}")
        comparable_audited = {key: value for key, value in audited_item.items() if key != "path"}
        comparable_current = {key: value for key, value in current_item.items() if key != "path"}
        if comparable_audited != comparable_current:
            raise RuntimeError(f"state database sidecar changed since audit: {name}")


def load_bound_audit(audit_path: Path, expected_sha256: str | None) -> tuple[dict[str, Any], str]:
    normalized = str(expected_sha256 or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise RuntimeError("expected audit file hash is missing or invalid")
    audit_bytes = audit_path.read_bytes()
    actual = hashlib.sha256(audit_bytes).hexdigest()
    if actual.casefold() != normalized:
        raise RuntimeError("audit file hash does not match the runner-bound SHA-256")
    try:
        audit = json.loads(audit_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"audit file is not valid UTF-8 JSON: {error}") from error
    if not isinstance(audit, dict):
        raise RuntimeError("audit root must be a JSON object")
    return audit, actual


def validate_audit_file_hash(audit_path: Path, expected_sha256: str | None) -> str:
    _, actual = load_bound_audit(audit_path, expected_sha256)
    return actual


def archive_logs(
    codex_home: Path,
    backup_root: Path,
    precommit_guard: Callable[[], None],
    before_detach: Callable[[list[dict[str, Any]]], None],
) -> list[dict[str, Any]]:
    log_directory = backup_root / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    staged: list[dict[str, Any]] = []
    for name in ("logs_2.sqlite", "logs_2.sqlite-wal", "logs_2.sqlite-shm", "logs_2.sqlite-journal"):
        source_path = codex_home / name
        if not source_path.is_file():
            continue
        destination_path = log_directory / name
        if destination_path.exists():
            raise RuntimeError(f"log archive already exists: {destination_path}")
        precommit_guard()
        source_stat = source_path.stat()
        size = source_path.stat().st_size
        shutil.copy2(source_path, destination_path)
        source_hash = file_sha256(source_path)
        backup_hash = file_sha256(destination_path)
        if source_hash != backup_hash:
            raise RuntimeError(f"log backup hash mismatch: {source_path}")
        precommit_guard()
        final_source_stat = source_path.stat()
        if (
            final_source_stat.st_size != source_stat.st_size
            or final_source_stat.st_mtime_ns != source_stat.st_mtime_ns
            or file_sha256(source_path) != source_hash
        ):
            raise RuntimeError(f"log source changed during backup: {source_path}")
        staged.append(
            {
                "source": str(source_path),
                "backup": str(destination_path),
                "bytes": size,
                "source_sha256": source_hash,
                "backup_sha256": backup_hash,
            }
        )

    precommit_guard()
    for item in staged:
        source_path = Path(item["source"])
        if not source_path.is_file() or file_sha256(source_path) != item["source_sha256"]:
            raise RuntimeError(f"log backup set became inconsistent: {source_path}")
    before_detach(staged)
    precommit_guard()
    detached: list[dict[str, Any]] = []
    try:
        for item in staged:
            source_path = Path(item["source"])
            backup_path = Path(item["backup"])
            precommit_guard()
            atomic_replace(source_path, backup_path)
            detached.append(item)
    except Exception:
        restore_errors: list[str] = []
        for item in reversed(detached):
            source_path = Path(item["source"])
            backup_path = Path(item["backup"])
            try:
                restore_file_from_backup(
                    source_path=source_path,
                    backup_path=backup_path,
                    expected_sha256=str(item["source_sha256"]),
                    rollback_root=backup_root / "rollback_artifacts" / "log_detach_failure",
                    precommit_guard=precommit_guard,
                )
            except Exception as restore_error:
                restore_errors.append(f"{source_path}: {restore_error}")
        if restore_errors:
            raise RuntimeError(f"log detach failed and rollback was incomplete: {restore_errors}")
        raise
    return staged


def file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def state_file_contracts(state_path: Path) -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    for name, suffix in state_contract_file_suffixes.items():
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


def restore_file_from_backup(
    source_path: Path,
    backup_path: Path,
    expected_sha256: str,
    rollback_root: Path,
    precommit_guard: Callable[[], None] = current_offline_guard,
) -> dict[str, Any]:
    if not backup_path.is_file():
        raise RuntimeError(f"rollback backup is missing: {backup_path}")
    backup_sha256 = file_sha256(backup_path)
    if backup_sha256 != expected_sha256:
        raise RuntimeError(f"rollback backup hash mismatch: {backup_path}")

    source_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_root.mkdir(parents=True, exist_ok=True)
    temporary_restore = source_path.with_name(f".{source_path.name}.{uuid4().hex}.restoring")
    archived_current_path: Path | None = None
    try:
        precommit_guard()
        shutil.copy2(backup_path, temporary_restore)
        if file_sha256(temporary_restore) != expected_sha256:
            raise RuntimeError(f"temporary rollback copy hash mismatch: {source_path}")
        if source_path.is_file():
            archived_current_path = rollback_root / f"{source_path.name}.{uuid4().hex}.replaced"
            shutil.copy2(source_path, archived_current_path)
        precommit_guard()
        atomic_replace(temporary_restore, source_path)
        restored_sha256 = file_sha256(source_path)
        if restored_sha256 != expected_sha256:
            raise RuntimeError(f"restored source hash mismatch: {source_path}")
    except Exception:
        if temporary_restore.is_file():
            failed_copy_path = rollback_root / f"{temporary_restore.name}.failed"
            precommit_guard()
            atomic_replace(temporary_restore, failed_copy_path)
        raise
    return {
        "source": str(source_path),
        "backup": str(backup_path),
        "restored_sha256": expected_sha256,
        "archived_replaced_file": str(archived_current_path) if archived_current_path else None,
    }


def discover_rollout_repair_journals(backup_root: Path) -> list[str]:
    discovered: list[str] = []
    rollout_root = windows_extended_path(backup_root / "rollouts")
    if not rollout_root.is_dir():
        return discovered
    for journal_path in rollout_root.rglob("*.repair-journal.json"):
        discovered.append(strip_windows_extended_prefix(str(journal_path)))
    return discovered


def rollback_repair_changes(
    thread_repairs: list[dict[str, Any]],
    log_plan: list[dict[str, Any]],
    backup_root: Path,
    precommit_guard: Callable[[], None] = current_offline_guard,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "thread_restores": [],
        "log_restores": [],
        "errors": [],
        "ignored_unbound_journals": [],
    }
    combined_repairs: dict[str, dict[str, Any]] = {}
    bound_journal_keys = {
        windows_path_key(item["journal_path"])
        for item in thread_repairs
        if str(item.get("journal_path") or "")
    }
    result["ignored_unbound_journals"] = [
        path
        for path in discover_rollout_repair_journals(backup_root)
        if windows_path_key(path) not in bound_journal_keys
    ]
    for item in thread_repairs:
        combined_repairs[windows_path_key(item["source_path"])] = item
    for item in reversed(list(combined_repairs.values())):
        try:
            restored = restore_file_from_backup(
                source_path=Path(item["source_path"]),
                backup_path=Path(item["backup_path"]),
                expected_sha256=str(item["original_sha256"]),
                rollback_root=backup_root / "rollback_artifacts" / "threads",
                precommit_guard=precommit_guard,
            )
            result["thread_restores"].append(restored)
            journal_path_text = str(item.get("journal_path") or "")
            durable_journal_path = windows_extended_path(journal_path_text)
            if journal_path_text and durable_journal_path.is_file():
                journal_payload = json.loads(durable_journal_path.read_text(encoding="utf-8"))
                journal_payload["status"] = "rolled_back"
                journal_payload["rolled_back_at_epoch"] = int(time.time())
                write_manifest(Path(journal_path_text), journal_payload)
        except Exception as error:
            result["errors"].append(f"thread {item.get('thread_id')}: {error}")

    for item in reversed(log_plan):
        source_path = Path(item["source"])
        expected_sha256 = str(item["source_sha256"])
        try:
            if source_path.is_file() and file_sha256(source_path) == expected_sha256:
                continue
            restored = restore_file_from_backup(
                source_path=source_path,
                backup_path=Path(item["backup"]),
                expected_sha256=expected_sha256,
                rollback_root=backup_root / "rollback_artifacts" / "logs",
                precommit_guard=precommit_guard,
            )
            result["log_restores"].append(restored)
        except Exception as error:
            result["errors"].append(f"log {source_path}: {error}")
    return result


def restore_state_database_snapshot(
    state_backups: list[dict[str, Any]],
    state_absent_paths: list[str],
    backup_root: Path,
    precommit_guard: Callable[[], None] = current_offline_guard,
    existing_restores: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "restores": [],
        "skipped_previous_restores": [],
        "archived_new_files": [],
        "errors": [],
    }
    previous_restore_by_source = {
        windows_path_key(item["source"]): item
        for item in existing_restores or []
        if str(item.get("source") or "")
    }
    state_names = {
        "state_5.sqlite",
        "state_5.sqlite-wal",
        "state_5.sqlite-shm",
        "state_5.sqlite-journal",
        ".codex-global-state.json",
        ".codex-global-state.json.bak",
        "session_index.jsonl",
    }
    for item in state_backups:
        source_path = Path(item["source"])
        if source_path.name not in state_names:
            continue
        previous_restore = previous_restore_by_source.get(windows_path_key(source_path))
        if previous_restore is not None:
            if str(previous_restore.get("restored_sha256") or "") != str(item["backup_sha256"]):
                result["errors"].append(f"state {source_path}: previous restore hash does not match backup contract")
            else:
                result["skipped_previous_restores"].append(previous_restore)
            continue
        try:
            restored = restore_file_from_backup(
                source_path=source_path,
                backup_path=Path(item["backup"]),
                expected_sha256=str(item["backup_sha256"]),
                rollback_root=backup_root / "rollback_artifacts" / "state",
                precommit_guard=precommit_guard,
            )
            result["restores"].append(restored)
        except Exception as error:
            result["errors"].append(f"state {source_path}: {error}")
    for source_text in state_absent_paths:
        source_path = Path(source_text)
        if source_path.name not in state_names or not source_path.is_file():
            continue
        try:
            archive_path = backup_root / "rollback_artifacts" / "state_absent" / f"{source_path.name}.{uuid4().hex}"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            precommit_guard()
            atomic_replace(source_path, archive_path)
            result["archived_new_files"].append(str(archive_path))
        except Exception as error:
            result["errors"].append(f"state absent {source_path}: {error}")
    return result


compatibility_block_reasons = {
    "shared_rollout_path",
    "source_parse_errors",
    "missing_session_meta_id",
    "session_meta_id_mismatch",
}


def build_content_preserving_repair_plan(
    rows: list[dict[str, Any]],
    include_archived: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    compatibility: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in rows:
        if bool(row.get("archived")) and not include_archived:
            continue
        scan = row.get("scan") or {}
        if int(scan.get("estimated_current_parser_errors") or 0) <= 0:
            continue
        blocked_reason = ""
        if row.get("shared_rollout_with"):
            blocked_reason = "shared_rollout_path"
        elif int(scan.get("parse_errors") or 0):
            blocked_reason = "source_parse_errors"
        elif not scan.get("session_meta_id"):
            blocked_reason = "missing_session_meta_id"
        elif str(scan.get("session_meta_id")) != str(row.get("id")):
            blocked_reason = "session_meta_id_mismatch"
        enriched_row = dict(row)
        if blocked_reason:
            enriched_row["repair_block_reason"] = blocked_reason
            blocked.append(enriched_row)
        else:
            compatibility.append(enriched_row)
    return compatibility, blocked


def performance_findings(
    rows: list[dict[str, Any]],
    include_archived: bool,
) -> list[dict[str, Any]]:
    thresholds = RepairThresholds()
    return [
        row
        for row in rows
        if (include_archived or not bool(row.get("archived")))
        and (
            int((row.get("scan") or {}).get("total_bytes") or 0) > thresholds.max_active_bytes
            or int((row.get("scan") or {}).get("line_count") or 0) > thresholds.max_active_lines
        )
    ]


def prompt_baselines(audit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    baselines: dict[str, dict[str, Any]] = {}
    for row in audit.get("threads") or []:
        thread_id = str(row.get("id") or "")
        scan = row.get("scan") if isinstance(row.get("scan"), dict) else {}
        if not thread_id:
            raise RuntimeError("audit thread is missing an id for the prompt preservation contract")
        if "user_prompt_count" not in scan or "user_prompt_sha256" not in scan:
            raise RuntimeError(f"audit thread is missing a prompt fingerprint: {thread_id}")
        baselines[thread_id] = {
            "count": int(scan["user_prompt_count"]),
            "sha256": str(scan["user_prompt_sha256"]),
        }
    return baselines


def collect_repair_rows(audit: dict[str, Any], include_archived: bool) -> list[tuple[dict[str, Any], str]]:
    return collect_repair_rows_with_targeted_slimming(audit, include_archived, [])


def normalize_slim_thread_ids(slim_thread_ids: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_thread_id in slim_thread_ids or []:
        thread_id = str(raw_thread_id or "").strip()
        if not thread_id:
            raise RuntimeError("targeted slim thread id cannot be empty")
        if thread_id in seen:
            continue
        seen.add(thread_id)
        normalized.append(thread_id)
    return normalized


def collect_repair_rows_with_targeted_slimming(
    audit: dict[str, Any],
    include_archived: bool,
    slim_thread_ids: list[str] | tuple[str, ...] | None,
) -> list[tuple[dict[str, Any], str]]:
    compatibility, blocked = build_content_preserving_repair_plan(
        list(audit.get("threads") or []),
        include_archived=include_archived,
    )
    if blocked:
        raise RuntimeError(f"audit contains blocked repair candidates: {len(blocked)}")
    rows = [(row, "compatibility_migration") for row in compatibility]
    unique: dict[str, tuple[dict[str, Any], str]] = {}
    for row, strategy in rows:
        unique[windows_path_key(row["rollout_path"])] = (row, strategy)

    audit_rows = {
        str(row.get("id") or ""): row
        for row in audit.get("threads") or []
        if str(row.get("id") or "")
    }
    for thread_id in normalize_slim_thread_ids(slim_thread_ids):
        row = audit_rows.get(thread_id)
        if row is None:
            raise RuntimeError(f"targeted slim thread is absent from the audit: {thread_id}")
        if bool(row.get("archived")) and not include_archived:
            raise RuntimeError(f"targeted slim thread is archived but archived threads are excluded: {thread_id}")
        scan = row.get("scan") if isinstance(row.get("scan"), dict) else {}
        block_reason = ""
        if row.get("shared_rollout_with"):
            block_reason = "shared_rollout_path"
        elif int(scan.get("parse_errors") or 0):
            block_reason = "source_parse_errors"
        elif not scan.get("session_meta_id"):
            block_reason = "missing_session_meta_id"
        elif str(scan.get("session_meta_id")) != thread_id:
            block_reason = "session_meta_id_mismatch"
        elif not int(scan.get("latest_compacted_line") or 0):
            block_reason = "missing_compacted_checkpoint"
        elif scan.get("latest_compacted_checkpoint_valid") is not True:
            block_reason = str(scan.get("latest_compacted_checkpoint_reason") or "invalid_compacted_checkpoint")
        if block_reason:
            raise RuntimeError(f"targeted slim thread is not safely replayable: {thread_id}: {block_reason}")
        unique[windows_path_key(row["rollout_path"])] = (
            row,
            "prompt_preserving_checkpoint_slim_view",
        )
    return list(unique.values())


def existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        raise RuntimeError(f"could not find an existing parent for disk-space check: {path}")
    return candidate


def estimate_required_space(
    audit: dict[str, Any],
    codex_home: Path,
    backup_root: Path,
    include_archived: bool,
    slim_thread_ids: list[str] | tuple[str, ...] | None = None,
) -> dict[str, int | str | bool]:
    rows = collect_repair_rows_with_targeted_slimming(audit, include_archived, slim_thread_ids)
    rollout_backup_bytes = sum(int(row["audited_size"]) for row, _strategy in rows)
    largest_temporary_view_bytes = max(
        (int(row["audited_size"]) for row, _strategy in rows),
        default=0,
    )
    state_backup_bytes = sum(
        (codex_home / name).stat().st_size
        for name in (
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
        )
        if (codex_home / name).is_file()
    )
    log_bytes = sum(
        (codex_home / name).stat().st_size
        for name in ("logs_2.sqlite", "logs_2.sqlite-wal", "logs_2.sqlite-shm", "logs_2.sqlite-journal")
        if (codex_home / name).is_file()
    )
    reserve_bytes = 512 * 1024 * 1024
    backup_required = rollout_backup_bytes + state_backup_bytes + log_bytes + reserve_bytes
    source_required = largest_temporary_view_bytes + 128 * 1024 * 1024
    backup_parent = existing_parent(backup_root)
    source_parent = existing_parent(codex_home)
    same_volume = canonical_path(backup_parent).drive.casefold() == canonical_path(source_parent).drive.casefold()
    backup_free = shutil.disk_usage(backup_parent).free
    source_free = shutil.disk_usage(source_parent).free
    required_on_backup_volume = backup_required + source_required if same_volume else backup_required
    if backup_free < required_on_backup_volume:
        raise RuntimeError(
            f"insufficient backup-volume space: free={backup_free} required={required_on_backup_volume}"
        )
    if not same_volume and source_free < source_required:
        raise RuntimeError(f"insufficient CODEX_HOME volume space: free={source_free} required={source_required}")
    return {
        "rollout_backup_bytes": rollout_backup_bytes,
        "largest_temporary_view_bytes": largest_temporary_view_bytes,
        "state_backup_bytes": state_backup_bytes,
        "log_bytes": log_bytes,
        "reserve_bytes": reserve_bytes,
        "backup_required_bytes": backup_required,
        "source_required_bytes": source_required,
        "required_on_backup_volume_bytes": required_on_backup_volume,
        "backup_free_bytes": backup_free,
        "source_free_bytes": source_free,
        "same_volume": same_volume,
        "backup_probe_path": str(backup_parent),
        "source_probe_path": str(source_parent),
    }


def open_state_guard(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(path, timeout=2, isolation_level=None)
    try:
        database.execute("pragma locking_mode=exclusive")
        database.execute("begin exclusive")
    except Exception:
        database.close()
        raise
    return database


def path_is_within(path: Path, root: Path) -> bool:
    return windows_path_is_within(path, root)


def validate_thread_mapping(
    database: sqlite3.Connection,
    thread_id: str,
    expected_rollout_path: Path,
    codex_home: Path,
) -> None:
    if not path_is_within(expected_rollout_path, codex_home):
        raise RuntimeError(f"rollout path is outside CODEX_HOME: {expected_rollout_path}")
    row = database.execute("select rollout_path from threads where id = ?", (thread_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"thread disappeared from state database: {thread_id}")
    current_path = canonical_path(str(row[0]))
    if windows_path_key(current_path) != windows_path_key(expected_rollout_path):
        raise RuntimeError(f"thread rollout mapping changed since audit: {thread_id}")


def apply_repairs(
    audit_path: Path,
    backup_root: Path,
    codex_home: Path,
    include_archived: bool,
    expected_audit_sha256: str | None = None,
    plugin_snapshot_manifest: Path | None = None,
    expected_plugin_snapshot_sha256: str | None = None,
    runner_run_id: str | None = None,
    run_root: Path | None = None,
    slim_thread_ids: list[str] | tuple[str, ...] | None = None,
    mutation_allowlist: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    assert_codex_offline()
    assert_backup_path(backup_root, default_backup_root)
    effective_run_root = (run_root or backup_root.parent).resolve()
    effective_run_id = str(runner_run_id or f"standalone-{uuid4().hex}")
    if run_root is not None and backup_root.resolve() != effective_run_root / "repair_data":
        raise RuntimeError("repair backup root must be the repair_data directory of the bound run")
    plugin_snapshot_path: Path | None = None
    plugin_snapshot_sha256: str | None = None
    if plugin_snapshot_manifest is not None:
        plugin_snapshot_path = plugin_snapshot_manifest.resolve()
        expected_plugin_path = effective_run_root / "plugin_state_snapshot" / "plugin_state_snapshot.json"
        if run_root is not None and windows_path_key(plugin_snapshot_path) != windows_path_key(expected_plugin_path):
            raise RuntimeError("plugin snapshot manifest is outside the bound repair run")
        plugin_snapshot_sha256 = file_sha256(plugin_snapshot_path)
        normalized_plugin_hash = str(expected_plugin_snapshot_sha256 or "").strip().casefold()
        if run_root is not None and plugin_snapshot_sha256 != normalized_plugin_hash:
            raise RuntimeError("plugin snapshot manifest does not match the runner-bound SHA-256")
    audit, audit_sha256 = load_bound_audit(audit_path, expected_audit_sha256)
    normalized_slim_thread_ids = normalize_slim_thread_ids(slim_thread_ids)
    normalized_mutation_allowlist = normalize_mutation_allowlist(mutation_allowlist)
    allowed_mutations = set(normalized_mutation_allowlist)
    prompt_baseline_by_thread = prompt_baselines(audit)
    if audit.get("missing_rollouts"):
        raise RuntimeError(f"audit contains missing rollouts: {len(audit['missing_rollouts'])}")
    if audit.get("shared_rollouts"):
        raise RuntimeError(f"audit contains shared rollout mappings: {len(audit['shared_rollouts'])}")
    blocked = [
        row
        for row in list(audit.get("active_blocked") or [])
        if str(row.get("repair_block_reason") or "") in compatibility_block_reasons
    ]
    if include_archived:
        blocked.extend(
            row
            for row in list(audit.get("archived_blocked") or [])
            if str(row.get("repair_block_reason") or "") in compatibility_block_reasons
        )
    if blocked:
        raise RuntimeError(f"audit contains blocked repair candidates: {len(blocked)}")
    validate_audit_contract(audit, codex_home)
    disk_preflight = estimate_required_space(
        audit,
        codex_home,
        backup_root,
        include_archived,
        normalized_slim_thread_ids,
    )
    backup_root.mkdir(parents=True, exist_ok=False)
    manifest_path = backup_root / "repair_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "started_at_epoch": int(time.time()),
        "status": "running",
        "runner_run_id": effective_run_id,
        "run_root": str(effective_run_root),
        "codex_home": str(codex_home),
        "audit_path": str(audit_path),
        "audit_sha256": audit_sha256,
        "plugin_snapshot_manifest": str(plugin_snapshot_path) if plugin_snapshot_path else None,
        "plugin_snapshot_manifest_sha256": plugin_snapshot_sha256,
        "include_archived": include_archived,
        "checkpoint_history_reduction_enabled": bool(normalized_slim_thread_ids),
        "prompt_preserving_slim_thread_ids": normalized_slim_thread_ids,
        "mutation_policy": {
            "supported": sorted(supported_state_mutations),
            "allowlist": normalized_mutation_allowlist,
            "default_state_mutations_enabled": False,
        },
        "mutation_preview": {},
        "prompt_preservation_required": True,
        "prompt_contract": {
            "baseline_thread_count": len(prompt_baseline_by_thread),
            "baseline_prompt_count": sum(item["count"] for item in prompt_baseline_by_thread.values()),
            "postcheck_mode": "exact",
        },
        "disk_preflight": disk_preflight,
        "state_quick_check_before": sqlite_quick_check(codex_home / "state_5.sqlite"),
        "state_backups": [],
        "state_absent_paths": [
            str(codex_home / name)
            for name in (
                "state_5.sqlite",
                "state_5.sqlite-wal",
                "state_5.sqlite-shm",
                "state_5.sqlite-journal",
                ".codex-global-state.json",
                ".codex-global-state.json.bak",
                "session_index.jsonl",
            )
            if not (codex_home / name).is_file()
        ],
        "title_sync": {"status": "preview_pending", "changed_count": 0, "changed_thread_ids": []},
        "archived_global_state_cleanup": {"status": "preview_pending"},
        "thread_repairs": [],
        "thread_repair_journal": [],
        "blocked": blocked,
        "log_archives": [],
        "log_archive_plan": [],
        "rollback": None,
        "postcheck": {},
    }
    write_manifest(manifest_path, manifest)

    state_guard: sqlite3.Connection | None = None
    try:
        assert_codex_offline()
        manifest["state_backups"] = backup_codex_state(codex_home, backup_root)
        write_manifest(manifest_path, manifest)
        state_guard = open_state_guard(codex_home / "state_5.sqlite")
        thread_columns = {
            str(row[1]) for row in state_guard.execute("pragma table_info(threads)").fetchall()
        }
        archived_thread_ids = (
            {
                str(row[0])
                for row in state_guard.execute("select id from threads where archived != 0").fetchall()
            }
            if "archived" in thread_columns
            else set()
        )
        title_sync_plan = build_sqlite_title_sync_plan(state_guard, codex_home / "session_index.jsonl")
        title_sync_preview = preview_sqlite_title_sync(
            state_guard,
            codex_home / "session_index.jsonl",
            plan=title_sync_plan,
        )
        title_sync_preview["allowed"] = mutation_title_sync in allowed_mutations
        global_state_preview = preview_archived_global_state_cleanup(codex_home, archived_thread_ids)
        global_state_preview["allowed"] = mutation_archived_global_state_cleanup in allowed_mutations
        manifest["mutation_preview"] = {
            mutation_title_sync: title_sync_preview,
            mutation_archived_global_state_cleanup: global_state_preview,
        }
        manifest["title_sync"] = {
            "status": "previewed_not_allowed",
            "changed_count": 0,
            "changed_thread_ids": [],
            "preview_mutation_required": title_sync_preview["mutation_required"],
        }
        manifest["archived_global_state_cleanup"] = {
            "status": "previewed_not_allowed",
            "preview_mutation_required": global_state_preview["mutation_required"],
            "removed_reference_count": 0,
        }
        write_manifest(manifest_path, manifest)

        if mutation_title_sync in allowed_mutations:
            manifest["title_sync"] = synchronize_sqlite_titles(
                state_guard,
                codex_home / "session_index.jsonl",
                precommit_guard=assert_codex_offline,
                plan=title_sync_plan,
            )
            write_manifest(manifest_path, manifest)
        if mutation_archived_global_state_cleanup in allowed_mutations:
            manifest["archived_global_state_cleanup"] = cleanup_archived_global_state_references(
                codex_home,
                archived_thread_ids,
                precommit_guard=assert_codex_offline,
            )
            write_manifest(manifest_path, manifest)
        for row, strategy in collect_repair_rows_with_targeted_slimming(
            audit,
            include_archived,
            normalized_slim_thread_ids,
        ):
            thread_id = str(row["id"])
            source_path = Path(row["rollout_path"])
            validate_thread_mapping(state_guard, thread_id, source_path, codex_home)
            common_arguments = {
                "source_path": source_path,
                "backup_root": backup_root / "rollouts",
                "expected_thread_id": thread_id,
                "audited_size": int(row["audited_size"]),
                "audited_mtime_ns": int(row["audited_mtime_ns"]),
                "audited_sha256": str(row["audited_sha256"]),
                "precommit_guard": assert_codex_offline,
            }
            journal_id = uuid4().hex

            def record_prepared_repair(prepared: dict[str, Any]) -> None:
                journal_item = {
                    **prepared,
                    "journal_id": journal_id,
                    "status": "prepared",
                    "strategy": strategy,
                    "archived": bool(row.get("archived")),
                    "parser_errors_before": int(row["scan"]["estimated_current_parser_errors"]),
                    "prepared_at_epoch": int(time.time()),
                }
                manifest["thread_repair_journal"].append(journal_item)
                write_manifest(manifest_path, manifest)

            common_arguments["before_replace"] = record_prepared_repair
            if strategy == "prompt_preserving_checkpoint_slim_view":
                result = repair_rollout_in_place(**common_arguments)
            else:
                result = repair_rollout_compatibility_in_place(**common_arguments)
            item = result.to_dict()
            if strategy == "prompt_preserving_checkpoint_slim_view" and item["active_bytes"] >= item["original_bytes"]:
                raise RuntimeError(f"targeted slim view did not reduce the active rollout: {thread_id}")
            item["strategy"] = strategy
            item["archived"] = bool(row.get("archived"))
            item["parser_errors_before"] = int(row["scan"]["estimated_current_parser_errors"])
            item["parser_errors_after"] = None
            manifest["thread_repairs"].append(item)
            journal_item = next(
                journal_entry
                for journal_entry in manifest["thread_repair_journal"]
                if journal_entry["journal_id"] == journal_id
            )
            journal_item.update(item)
            journal_item["status"] = "committed"
            journal_item["committed_at_epoch"] = int(time.time())
            write_manifest(manifest_path, manifest)
            item["parser_errors_after"] = scan_rollout(source_path).estimated_current_parser_errors
            write_manifest(manifest_path, manifest)

        manifest["log_quick_check_before_archive"] = (
            sqlite_quick_check(codex_home / "logs_2.sqlite")
            if (codex_home / "logs_2.sqlite").is_file()
            else "missing"
        )

        def record_log_plan(plan: list[dict[str, Any]]) -> None:
            manifest["log_archive_plan"] = plan
            write_manifest(manifest_path, manifest)

        manifest["log_archives"] = archive_logs(
            codex_home,
            backup_root,
            precommit_guard=assert_codex_offline,
            before_detach=record_log_plan,
        )
        write_manifest(manifest_path, manifest)

        post_parser_errors = 0
        post_parse_errors = 0
        missing_rollouts = 0
        checked_rollouts = 0
        prompt_contract_checked = 0
        archived_expression = "archived" if "archived" in thread_columns else "0"
        state_rows = state_guard.execute(
            f"select id, rollout_path, {archived_expression} from threads"
        ).fetchall()
        post_audit_rows: list[dict[str, Any]] = []
        for thread_id, rollout_text, archived in state_rows:
            rollout_path = Path(str(rollout_text))
            if not path_is_within(rollout_path, codex_home) or not rollout_path.is_file():
                missing_rollouts += 1
                continue
            scan = scan_rollout(rollout_path)
            if scan.session_meta_id != str(thread_id):
                raise RuntimeError(f"postcheck thread identity mismatch: {thread_id}")
            baseline = prompt_baseline_by_thread.get(str(thread_id))
            if baseline is None:
                raise RuntimeError(f"postcheck thread is absent from the prompt baseline: {thread_id}")
            prompt_result = validate_user_prompt_contract(
                rollout_path,
                baseline["count"],
                baseline["sha256"],
                allow_appended=False,
                current_scan=scan,
            )
            if prompt_result["appended_count"] != 0:
                raise RuntimeError(f"offline postcheck found appended prompts: {thread_id}")
            prompt_contract_checked += 1
            checked_rollouts += 1
            post_parser_errors += scan.estimated_current_parser_errors
            post_parse_errors += scan.parse_errors
            post_audit_rows.append(
                {
                    "id": str(thread_id),
                    "rollout_path": str(rollout_path),
                    "archived": bool(archived),
                    "scan": scan.to_dict(),
                }
            )

        remaining_performance = performance_findings(post_audit_rows, include_archived=True)
        remaining_compatibility, remaining_blocked = build_content_preserving_repair_plan(
            post_audit_rows,
            include_archived=True,
        )

        state_quick_check_after = "; ".join(
            str(row[0]) for row in state_guard.execute("pragma quick_check").fetchall()
        )
        manifest["postcheck"] = {
            "state_quick_check_after": state_quick_check_after,
            "database_thread_count": len(state_rows),
            "checked_rollouts": checked_rollouts,
            "missing_rollouts": missing_rollouts,
            "json_parse_errors": post_parse_errors,
            "estimated_current_parser_errors": post_parser_errors,
            "remaining_performance_candidates": len(remaining_performance),
            "remaining_compatibility_candidates": len(remaining_compatibility),
            "remaining_blocked_candidates": len(remaining_blocked),
            "prompt_contract_checked_threads": prompt_contract_checked,
            "prompt_contract_baseline_threads": len(prompt_baseline_by_thread),
            "prompt_contract_mode": "exact",
        }
        if normalized_slim_thread_ids:
            targeted_repairs = {
                str(item.get("thread_id") or ""): item
                for item in manifest["thread_repairs"]
                if item.get("strategy") == "prompt_preserving_checkpoint_slim_view"
            }
            missing_targeted_repairs = [
                thread_id for thread_id in normalized_slim_thread_ids if thread_id not in targeted_repairs
            ]
            if missing_targeted_repairs:
                raise RuntimeError(f"targeted slim repairs were not committed: {missing_targeted_repairs}")
            manifest["postcheck"]["targeted_slim"] = {
                "requested_thread_ids": normalized_slim_thread_ids,
                "committed_thread_ids": list(targeted_repairs),
                "all_reduced": all(
                    int(item["active_bytes"]) < int(item["original_bytes"])
                    for item in targeted_repairs.values()
                ),
            }
        if state_quick_check_after != "ok":
            raise RuntimeError("state database quick_check failed after repair")
        if (
            missing_rollouts
            or post_parse_errors
            or post_parser_errors
            or remaining_compatibility
            or remaining_blocked
            or prompt_contract_checked != len(prompt_baseline_by_thread)
        ):
            raise RuntimeError(f"postcheck failed: {manifest['postcheck']}")

        manifest["status"] = "applied_pending_runner_diagnostics"
        manifest["completed_at_epoch"] = int(time.time())
        assert_codex_offline()
        state_guard.execute("commit")
        state_guard.close()
        state_guard = None
        if manifest["title_sync"]["status"] == "prepared":
            manifest["title_sync"]["status"] = "committed"
        write_manifest(manifest_path, manifest)
        return manifest
    except Exception as error:
        transaction_errors: list[str] = []
        if state_guard is not None:
            try:
                state_guard.execute("rollback")
            except Exception as transaction_error:
                transaction_errors.append(f"state transaction rollback: {transaction_error}")
            finally:
                state_guard.close()
                state_guard = None
        rollback = rollback_repair_changes(
            thread_repairs=list(manifest.get("thread_repair_journal") or []),
            log_plan=list(manifest.get("log_archive_plan") or []),
            backup_root=backup_root,
        )
        rollback["state"] = restore_state_database_snapshot(
            state_backups=list(manifest.get("state_backups") or []),
            state_absent_paths=list(manifest.get("state_absent_paths") or []),
            backup_root=backup_root,
        )
        rollback["errors"].extend(transaction_errors)
        rollback["errors"].extend(rollback["state"]["errors"])
        manifest["rollback"] = rollback
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        manifest["failed_at_epoch"] = int(time.time())
        write_manifest(manifest_path, manifest)
        if rollback["errors"]:
            raise RuntimeError(f"repair failed: {error}; rollback failures: {rollback['errors']}") from error
        raise
    finally:
        if state_guard is not None:
            try:
                state_guard.execute("rollback")
            finally:
                state_guard.close()


def require_exact_path(actual: Path, expected: Path, label: str) -> None:
    if windows_path_key(actual.resolve()) != windows_path_key(expected.resolve()):
        raise RuntimeError(f"{label} is outside the bound repair transaction")


def require_path_within(actual: Path, root: Path, label: str) -> None:
    if not path_is_within(actual.resolve(), root.resolve()):
        raise RuntimeError(f"{label} escapes the bound repair transaction")


def validate_rollback_manifest_paths(
    manifest_path: Path,
    manifest: dict[str, Any],
    expected_run_id: str,
    expected_run_root: Path,
    expected_codex_home: Path,
) -> None:
    run_root = expected_run_root.resolve()
    codex_home = expected_codex_home.resolve()
    backup_root = run_root / "repair_data"
    require_exact_path(manifest_path, backup_root / "repair_manifest.json", "repair manifest")
    require_exact_path(Path(str(manifest.get("run_root") or "")), run_root, "manifest run root")
    require_exact_path(Path(str(manifest.get("codex_home") or "")), codex_home, "manifest CODEX_HOME")
    if str(manifest.get("runner_run_id") or "") != expected_run_id:
        raise RuntimeError("repair manifest run id does not match the active runner")

    for item in list(manifest.get("thread_repair_journal") or manifest.get("thread_repairs") or []):
        require_path_within(Path(str(item.get("source_path") or "")), codex_home, "rollout source")
        require_path_within(Path(str(item.get("backup_path") or "")), backup_root / "rollouts", "rollout backup")
        if item.get("journal_path"):
            require_path_within(Path(str(item["journal_path"])), backup_root / "rollouts", "rollout journal")

    allowed_log_names = {"logs_2.sqlite", "logs_2.sqlite-wal", "logs_2.sqlite-shm", "logs_2.sqlite-journal"}
    for item in list(manifest.get("log_archive_plan") or []):
        source_path = Path(str(item.get("source") or ""))
        if source_path.name not in allowed_log_names:
            raise RuntimeError(f"unexpected log rollback target: {source_path}")
        require_exact_path(source_path, codex_home / source_path.name, "log rollback target")
        require_path_within(Path(str(item.get("backup") or "")), backup_root / "logs", "log backup")

    allowed_state_backup_names = {
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
    }
    for item in list(manifest.get("state_backups") or []):
        source_path = Path(str(item.get("source") or ""))
        if source_path.name not in allowed_state_backup_names:
            raise RuntimeError(f"unexpected state rollback target: {source_path}")
        require_exact_path(source_path, codex_home / source_path.name, "state rollback target")
        require_path_within(Path(str(item.get("backup") or "")), backup_root / "state", "state backup")
    for source_text in list(manifest.get("state_absent_paths") or []):
        source_path = Path(str(source_text))
        if source_path.name not in {
            "state_5.sqlite",
            "state_5.sqlite-wal",
            "state_5.sqlite-shm",
            "state_5.sqlite-journal",
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
            "session_index.jsonl",
        }:
            raise RuntimeError(f"unexpected absent-state rollback target: {source_path}")
        require_exact_path(source_path, codex_home / source_path.name, "absent-state rollback target")

    plugin_snapshot_manifest = manifest.get("plugin_snapshot_manifest")
    if plugin_snapshot_manifest:
        require_exact_path(
            Path(str(plugin_snapshot_manifest)),
            run_root / "plugin_state_snapshot" / "plugin_state_snapshot.json",
            "plugin snapshot manifest",
        )


def rollback_completed_repair(
    manifest_path: Path,
    *,
    expected_run_id: str,
    expected_run_root: Path,
    expected_codex_home: Path,
    expected_manifest_sha256: str,
    preserve_runtime_state: bool = False,
) -> dict[str, Any]:
    assert_codex_offline()
    manifest_path = manifest_path.resolve()
    assert_backup_path(manifest_path, default_backup_root)
    manifest, manifest_sha256 = load_manifest_pair(manifest_path, expected_manifest_sha256)
    validate_rollback_manifest_paths(
        manifest_path,
        manifest,
        expected_run_id,
        expected_run_root,
        expected_codex_home,
    )
    if manifest.get("status") not in {
        "running",
        "failed",
        "applied_pending_runner_diagnostics",
        "complete",
        "pending_restart_validation",
        "pending_live_ui_validation",
        "rollback_failed",
    }:
        raise RuntimeError(f"repair manifest is not rollback-eligible: {manifest.get('status')}")
    backup_root = manifest_path.parent
    result = rollback_repair_changes(
        thread_repairs=list(manifest.get("thread_repair_journal") or manifest.get("thread_repairs") or []),
        log_plan=list(manifest.get("log_archive_plan") or []),
        backup_root=backup_root,
    )
    prior_state_restores: dict[str, dict[str, Any]] = {}
    for rollback_key in ("rollback", "runner_rollback"):
        rollback_state = dict((manifest.get(rollback_key) or {}).get("state") or {})
        for restore_item in list(rollback_state.get("restores") or []):
            if str(restore_item.get("source") or ""):
                prior_state_restores[windows_path_key(restore_item["source"])] = restore_item
    state_result = restore_state_database_snapshot(
        state_backups=list(manifest.get("state_backups") or []),
        state_absent_paths=list(manifest.get("state_absent_paths") or []),
        backup_root=backup_root,
        existing_restores=list(prior_state_restores.values()),
    )
    state_result["restores"] = list(prior_state_restores.values()) + list(state_result["restores"])
    result["state"] = state_result
    plugin_result: dict[str, Any] = {"status": "not_required", "errors": []}
    plugin_snapshot_manifest = manifest.get("plugin_snapshot_manifest")
    if plugin_snapshot_manifest:
        plugin_result = restore_plugin_state(
            Path(str(plugin_snapshot_manifest)),
            default_backup_root,
            expected_run_id=expected_run_id,
            expected_run_root=expected_run_root,
            expected_codex_home=expected_codex_home,
            expected_manifest_sha256=str(manifest.get("plugin_snapshot_manifest_sha256") or ""),
            skip_relative_paths=runtime_state_snapshot_relative_paths if preserve_runtime_state else None,
        )
    result["plugins"] = plugin_result
    combined_errors = [*result["errors"], *state_result["errors"], *list(plugin_result.get("errors") or [])]
    manifest["runner_rollback"] = result
    manifest["status"] = "rolled_back" if not combined_errors else "rollback_failed"
    manifest["rolled_back_at_epoch"] = int(time.time())
    manifest["rollback_source_manifest_sha256"] = manifest_sha256
    write_manifest(manifest_path, manifest)
    if combined_errors:
        raise RuntimeError(f"completed repair rollback failed: {combined_errors}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a one-shot offline Codex repair with exact backups.")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--expected-audit-sha256")
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--plugin-snapshot-manifest", type=Path)
    parser.add_argument("--rollback-manifest", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-run-id")
    parser.add_argument("--expected-run-root", type=Path)
    parser.add_argument("--expected-codex-home", type=Path)
    parser.add_argument("--runner-run-id")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--expected-plugin-snapshot-sha256")
    parser.add_argument("--codex-home", type=Path, default=Path(r"D:\.codex"))
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument(
        "--slim-thread-id",
        action="append",
        default=[],
        help="Explicit thread id whose active rollout may be reduced to all exact user prompts plus the latest replayable checkpoint tail.",
    )
    parser.add_argument(
        "--allow-mutation",
        action="append",
        default=[],
        choices=sorted(supported_state_mutations),
        help="Explicitly allow one audited non-rollout state mutation. Omit to keep compatibility migration rollout-only.",
    )
    parser.add_argument("--preserve-runtime-state", action="store_true")
    arguments = parser.parse_args()
    if arguments.rollback_manifest:
        if not all(
            (
                arguments.expected_manifest_sha256,
                arguments.expected_run_id,
                arguments.expected_run_root,
                arguments.expected_codex_home,
            )
        ):
            parser.error(
                "--expected-manifest-sha256, --expected-run-id, --expected-run-root and "
                "--expected-codex-home are required for rollback"
            )
        result = rollback_completed_repair(
            arguments.rollback_manifest,
            expected_run_id=arguments.expected_run_id,
            expected_run_root=arguments.expected_run_root.resolve(),
            expected_codex_home=arguments.expected_codex_home.resolve(),
            expected_manifest_sha256=arguments.expected_manifest_sha256,
            preserve_runtime_state=arguments.preserve_runtime_state,
        )
        print(json.dumps({"status": "rolled_back", "rollback": result}, indent=2))
        return 0
    if arguments.audit is None or arguments.backup_root is None or not arguments.expected_audit_sha256:
        parser.error("--audit, --expected-audit-sha256 and --backup-root are required unless --rollback-manifest is used")
    manifest = apply_repairs(
        audit_path=arguments.audit.resolve(),
        backup_root=arguments.backup_root.resolve(),
        codex_home=arguments.codex_home.resolve(),
        include_archived=arguments.include_archived,
        expected_audit_sha256=arguments.expected_audit_sha256,
        plugin_snapshot_manifest=arguments.plugin_snapshot_manifest,
        expected_plugin_snapshot_sha256=arguments.expected_plugin_snapshot_sha256,
        runner_run_id=arguments.runner_run_id,
        run_root=arguments.run_root.resolve() if arguments.run_root else None,
        slim_thread_ids=arguments.slim_thread_id,
        mutation_allowlist=arguments.allow_mutation,
    )
    print(json.dumps({"status": manifest["status"], "postcheck": manifest["postcheck"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
