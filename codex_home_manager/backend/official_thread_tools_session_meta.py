from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex_data import strip_extended_prefix
from .process_utils import list_windows_processes
from .thread_history_repair import scan_rollout, validate_user_prompt_contract


required_thread_tool_names = {
    "list_threads",
    "read_thread",
    "send_message_to_thread",
}

blocking_process_names = {
    "chatgpt.exe",
    "node_repl.exe",
    "codex-code-mode-host.exe",
}


class RolloutWriteConflict(RuntimeError):
    """Raised when a rollout cannot be protected from concurrent writers."""


@contextmanager
def rollout_write_guard(path: Path):
    """Block new Windows writers while a validated rollout is atomically replaced."""
    if os.name != "nt":
        yield
        return

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    absolute_path = str(path.resolve())
    if not absolute_path.startswith("\\\\?\\"):
        absolute_path = "\\\\?\\" + absolute_path
    handle = create_file(
        absolute_path,
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0x00000001 | 0x00000004,  # FILE_SHARE_READ | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle_value = ctypes.c_void_p(-1).value
    if handle == invalid_handle_value:
        error_code = ctypes.get_last_error()
        if error_code in {5, 32, 33}:
            raise RolloutWriteConflict(
                f"rollout has an active or conflicting writer (WinError {error_code}): {path}"
            )
        raise ctypes.WinError(error_code)
    try:
        yield handle
    finally:
        close_handle(handle)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    last_error: OSError | None = None
    for attempt_index in range(10):
        try:
            os.replace(temporary_path, path)
            return
        except PermissionError as error:
            last_error = error
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32}:
                raise
            last_error = error
        time.sleep(min(0.05 * (2 ** attempt_index), 0.5))
    temporary_path.unlink(missing_ok=True)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"failed to replace JSON status file: {path}")


def active_codex_processes() -> list[dict[str, str]]:
    return [
        process
        for process in list_windows_processes()
        if str(process.get("imageName") or "").casefold() in blocking_process_names
        or str(process.get("imageName") or "").casefold() == "codex.exe"
    ]


def reverse_jsonl_lines(path: Path, chunk_size: int = 1024 * 1024):
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        position = source.tell()
        remainder = b""
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            source.seek(position)
            block = source.read(read_size) + remainder
            lines = block.split(b"\n")
            remainder = lines[0]
            for raw_line in reversed(lines[1:]):
                if raw_line:
                    yield raw_line
        if remainder:
            yield remainder


def latest_session_meta(path: Path) -> tuple[int, dict[str, Any]]:
    for raw_line in reverse_jsonl_lines(path):
        try:
            record = json.loads(raw_line)
        except Exception:
            continue
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            return 0, record
    raise RuntimeError(f"rollout has no session_meta record: {path}")


def initial_session_meta_records(path: Path) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    with path.open("rb") as source:
        for line_number, raw_line in enumerate(source, 1):
            try:
                record = json.loads(raw_line)
            except Exception as error:
                raise RuntimeError(
                    f"invalid JSON in initial rollout metadata at line {line_number}: {path}"
                ) from error
            if record.get("type") != "session_meta":
                break
            if not isinstance(record.get("payload"), dict):
                raise RuntimeError(
                    f"initial session_meta payload is not an object at line {line_number}: {path}"
                )
            records.append((line_number, record))
    if not records:
        raise RuntimeError(f"rollout has no initial session_meta record: {path}")
    return records


def codex_app_tool_names(session_meta_record: dict[str, Any]) -> set[str]:
    payload = session_meta_record.get("payload")
    dynamic_tools = payload.get("dynamic_tools") if isinstance(payload, dict) else None
    for item in dynamic_tools or []:
        if not isinstance(item, dict) or item.get("type") != "namespace" or item.get("name") != "codex_app":
            continue
        return {
            str(tool.get("name") or "")
            for tool in item.get("tools") or []
            if isinstance(tool, dict) and tool.get("type") == "function"
        }
    return set()


def has_unsupported_repair_marker(session_meta_record: dict[str, Any]) -> bool:
    payload = session_meta_record.get("payload")
    return isinstance(payload, dict) and "codex_home_manager_repair" in payload


def dynamic_tools_protocol(session_meta_record: dict[str, Any]) -> dict[str, int | bool]:
    payload = session_meta_record.get("payload")
    dynamic_tools = payload.get("dynamic_tools") if isinstance(payload, dict) else None
    items = dynamic_tools if isinstance(dynamic_tools, list) else []
    namespace_count = sum(
        1 for item in items if isinstance(item, dict) and item.get("type") == "namespace"
    )
    legacy_flat_count = sum(
        1 for item in items if not (isinstance(item, dict) and item.get("type") == "namespace")
    )
    return {
        "namespaceCount": namespace_count,
        "legacyFlatCount": legacy_flat_count,
        "mixed": namespace_count > 0 and legacy_flat_count > 0,
    }


def codex_app_namespace(
    tool_rows: list[sqlite3.Row],
    *,
    require_contiguous_positions: bool = True,
) -> dict[str, Any]:
    tools: list[dict[str, Any]] = []
    positions = [int(row["position"]) for row in tool_rows]
    if require_contiguous_positions and positions != list(range(len(positions))):
        raise RuntimeError(f"thread_dynamic_tools positions are not contiguous: {positions}")
    for row in tool_rows:
        input_schema = json.loads(str(row["input_schema"]))
        if not isinstance(input_schema, dict):
            raise RuntimeError(f"input schema is not an object for {row['name']}")
        tool = {
            "type": "function",
            "name": str(row["name"]),
            "description": str(row["description"]),
            "inputSchema": input_schema,
        }
        if int(row["defer_loading"] or 0):
            tool["deferLoading"] = True
        tools.append(tool)
    tool_names = {str(tool["name"]) for tool in tools}
    missing_names = sorted(required_thread_tool_names - tool_names)
    if missing_names:
        raise RuntimeError(f"thread_dynamic_tools is missing required official tools: {missing_names}")
    return {
        "type": "namespace",
        "name": "codex_app",
        "description": "",
        "tools": tools,
    }


def official_tool_candidates(
    codex_home_path: Path,
    *,
    require_contiguous_positions: bool = True,
    thread_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    database_path = codex_home_path / "state_5.sqlite"
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    candidates: list[dict[str, Any]] = []
    try:
        thread_rows = connection.execute(
            """
            SELECT DISTINCT threads.id, threads.rollout_path, threads.title
            FROM threads
            JOIN thread_dynamic_tools ON thread_dynamic_tools.thread_id = threads.id
            WHERE threads.archived = 0 AND thread_dynamic_tools.namespace = 'codex_app'
            ORDER BY threads.id
            """
        ).fetchall()
        for thread_row in thread_rows:
            if thread_ids is not None and str(thread_row["id"]) not in thread_ids:
                continue
            tool_rows = connection.execute(
                """
                SELECT position, name, description, input_schema, defer_loading
                FROM thread_dynamic_tools
                WHERE thread_id = ? AND namespace = 'codex_app'
                ORDER BY position
                """,
                (thread_row["id"],),
            ).fetchall()
            namespace = codex_app_namespace(
                tool_rows,
                require_contiguous_positions=require_contiguous_positions,
            )
            rollout_path = Path(strip_extended_prefix(str(thread_row["rollout_path"])))
            if not rollout_path.exists():
                raise RuntimeError(f"registered rollout does not exist: {rollout_path}")
            initial_records = initial_session_meta_records(rollout_path)
            initial_tool_names = [
                {
                    "lineNumber": line_number,
                    "toolNames": sorted(codex_app_tool_names(record)),
                    "hasUnsupportedRepairMarker": has_unsupported_repair_marker(record),
                    "protocol": dynamic_tools_protocol(record),
                }
                for line_number, record in initial_records
            ]
            initial_session_meta_ids = sorted(
                {
                    str(record.get("payload", {}).get("id") or "")
                    for _, record in initial_records
                }
            )
            identity_matches = initial_session_meta_ids == [str(thread_row["id"])]
            if all(
                required_thread_tool_names.issubset(set(item["toolNames"]))
                and not item["hasUnsupportedRepairMarker"]
                and int(item["protocol"]["legacyFlatCount"]) == 0
                for item in initial_tool_names
            ) and identity_matches:
                continue
            candidates.append(
                {
                    "threadId": str(thread_row["id"]),
                    "title": str(thread_row["title"]),
                    "rolloutPath": str(rollout_path),
                    "initialSessionMetaLines": [line_number for line_number, _ in initial_records],
                    "initialSessionMeta": [record for _, record in initial_records],
                    "initialSessionMetaIds": initial_session_meta_ids,
                    "identityMatches": identity_matches,
                    "initialToolNames": initial_tool_names,
                    "namespace": namespace,
                }
            )
    finally:
        connection.close()
    return candidates


def partition_candidates_by_rollout_identity(
    codex_home_path: Path,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    database_path = codex_home_path / "state_5.sqlite"
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        thread_rows = connection.execute(
            """
            SELECT id, title, rollout_path
            FROM threads
            WHERE archived = 0 AND rollout_path IS NOT NULL AND rollout_path <> ''
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()

    threads_by_rollout_path: dict[str, list[dict[str, str]]] = {}
    for row in thread_rows:
        normalized_path = str(Path(strip_extended_prefix(str(row["rollout_path"]))))
        threads_by_rollout_path.setdefault(normalized_path, []).append(
            {
                "threadId": str(row["id"]),
                "title": str(row["title"] or "")[:200],
            }
        )

    safe_candidates: list[dict[str, Any]] = []
    blocked_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        meta_ids = list(candidate.get("initialSessionMetaIds") or [])
        if meta_ids == [candidate["threadId"]]:
            safe_candidates.append(candidate)
            continue
        rollout_path = str(Path(candidate["rolloutPath"]))
        shared_threads = threads_by_rollout_path.get(rollout_path, [])
        shared_thread_ids = {item["threadId"] for item in shared_threads}
        is_shared_alias = bool(shared_threads) and any(
            meta_id in shared_thread_ids for meta_id in meta_ids
        )
        blocked_candidates.append(
            {
                "threadId": candidate["threadId"],
                "title": str(candidate.get("title") or "")[:200],
                "rolloutPath": candidate["rolloutPath"],
                "initialSessionMetaIds": meta_ids,
                "reason": (
                    "shared_rollout_alias"
                    if is_shared_alias
                    else "session_meta_id_mismatch"
                ),
                "sharedThreads": shared_threads,
            }
        )
    return safe_candidates, blocked_candidates


def repaired_initial_session_meta(
    record: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    record = copy.deepcopy(record)
    payload = record["payload"]
    existing_dynamic_tools = payload.get("dynamic_tools")
    existing_items = existing_dynamic_tools if isinstance(existing_dynamic_tools, list) else []
    official_namespace = copy.deepcopy(candidate["namespace"])
    official_tools = official_namespace["tools"]
    official_tool_names = {
        str(tool.get("name") or "") for tool in official_tools if isinstance(tool, dict)
    }

    for item in existing_items:
        if not isinstance(item, dict):
            raise RuntimeError("legacy dynamic tool item is not an object")
        if item.get("type") == "namespace":
            if item.get("name") != "codex_app":
                continue
            legacy_tools = item.get("tools") if isinstance(item.get("tools"), list) else []
            for legacy_tool in legacy_tools:
                if not isinstance(legacy_tool, dict):
                    raise RuntimeError("codex_app namespace contains a non-object tool")
                name = str(legacy_tool.get("name") or "")
                if name and name not in official_tool_names:
                    official_tools.append(copy.deepcopy(legacy_tool))
                    official_tool_names.add(name)
            continue

        namespace = str(item.get("namespace") or "")
        name = str(item.get("name") or "")
        if namespace not in {"", "codex_app"}:
            raise RuntimeError(
                f"cannot migrate legacy dynamic tool from unsupported namespace {namespace}: {name}"
            )
        if not name:
            raise RuntimeError("legacy dynamic tool has no name")
        if namespace == "" and name not in official_tool_names | {"install_workspace_dependencies"}:
            raise RuntimeError(f"cannot safely assign namespace to legacy dynamic tool: {name}")
        if name in official_tool_names:
            continue
        converted_tool: dict[str, Any] = {
            "type": "function",
            "name": name,
            "description": str(item.get("description") or ""),
            "inputSchema": copy.deepcopy(item.get("inputSchema") or {}),
        }
        if bool(item.get("deferLoading")):
            converted_tool["deferLoading"] = True
        official_tools.append(converted_tool)
        official_tool_names.add(name)

    preserved_dynamic_tools: list[Any] = []
    codex_app_inserted = False
    for item in existing_items:
        is_codex_app_namespace = (
            isinstance(item, dict)
            and item.get("type") == "namespace"
            and item.get("name") == "codex_app"
        )
        is_legacy_flat_tool = not (
            isinstance(item, dict) and item.get("type") == "namespace"
        )
        if is_codex_app_namespace or is_legacy_flat_tool:
            if not codex_app_inserted:
                preserved_dynamic_tools.append(official_namespace)
                codex_app_inserted = True
            continue
        preserved_dynamic_tools.append(copy.deepcopy(item))
    if not codex_app_inserted:
        preserved_dynamic_tools.append(official_namespace)
    payload["dynamic_tools"] = preserved_dynamic_tools
    payload.pop("codex_home_manager_repair", None)
    return record


def write_repaired_initial_session_meta(
    source_path: Path,
    temporary_path: Path,
    candidate: dict[str, Any],
) -> int:
    rewritten_count = 0
    initial_phase = True
    with source_path.open("rb") as source, temporary_path.open("wb") as destination:
        for line_number, raw_line in enumerate(source, 1):
            if initial_phase:
                record = json.loads(raw_line)
                if record.get("type") == "session_meta":
                    if not isinstance(record.get("payload"), dict):
                        raise RuntimeError(
                            f"initial session_meta payload is not an object at line {line_number}: {source_path}"
                        )
                    repaired_record = repaired_initial_session_meta(record, candidate)
                    raw_line = (
                        json.dumps(repaired_record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        + b"\n"
                    )
                    rewritten_count += 1
                else:
                    initial_phase = False
            destination.write(raw_line)
        destination.flush()
        os.fsync(destination.fileno())
    if rewritten_count == 0:
        raise RuntimeError(f"rollout has no initial session_meta record: {source_path}")
    return rewritten_count


def copy_validated_file(source_path: Path, destination_path: Path) -> str:
    temporary_path = destination_path.with_name(
        destination_path.name + ".official-thread-tools.restore.tmp"
    )
    shutil.copy2(source_path, temporary_path)
    try:
        os.replace(temporary_path, destination_path)
        return "atomic_replace"
    except PermissionError:
        with temporary_path.open("rb") as source, destination_path.open("r+b") as destination:
            destination.seek(0)
            shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
            destination.truncate()
            destination.flush()
            os.fsync(destination.fileno())
        temporary_path.unlink(missing_ok=True)
        return "validated_in_place_overwrite"


def overwrite_file_through_guarded_handle(
    source_path: Path,
    guarded_handle: int,
    *,
    remove_source: bool,
) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_file_pointer = kernel32.SetFilePointerEx
    set_file_pointer.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
    set_file_pointer.restype = wintypes.BOOL
    write_file = kernel32.WriteFile
    write_file.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    write_file.restype = wintypes.BOOL
    set_end_of_file = kernel32.SetEndOfFile
    set_end_of_file.argtypes = [wintypes.HANDLE]
    set_end_of_file.restype = wintypes.BOOL
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [wintypes.HANDLE]
    flush_file_buffers.restype = wintypes.BOOL

    if not set_file_pointer(guarded_handle, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    with source_path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            buffer = ctypes.create_string_buffer(block)
            written = wintypes.DWORD()
            if not write_file(guarded_handle, buffer, len(block), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if int(written.value) != len(block):
                raise RuntimeError(
                    f"guarded rollout write was short: expected {len(block)}, wrote {written.value}"
                )
    if not set_end_of_file(guarded_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    if not flush_file_buffers(guarded_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    if remove_source:
        source_path.unlink(missing_ok=True)


def install_validated_rollout(
    temporary_path: Path,
    rollout_path: Path,
    guarded_handle: int | None = None,
) -> str:
    if os.name == "nt" and guarded_handle is not None:
        overwrite_file_through_guarded_handle(
            temporary_path,
            guarded_handle,
            remove_source=True,
        )
        return "guarded_in_place_overwrite"
    os.replace(temporary_path, rollout_path)
    return "atomic_replace"


def sha256_file_prefix(path: Path, byte_count: int) -> str:
    digest = hashlib.sha256()
    remaining = byte_count
    with path.open("rb") as source:
        while remaining > 0:
            block = source.read(min(8 * 1024 * 1024, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    if remaining:
        raise RuntimeError(f"file is shorter than the validated prefix: {path}")
    return digest.hexdigest()


def restore_modified_rollouts(modified_entries: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for entry in reversed(modified_entries):
        source_path = Path(entry["rolloutPath"])
        backup_path = Path(entry["backupPath"])
        restore_path = source_path.with_name(
            source_path.name + f".{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.rollback.tmp"
        )
        try:
            shutil.copy2(backup_path, restore_path)
            with rollout_write_guard(source_path) as guarded_handle:
                expected_source_sha256 = str(entry.get("rollbackExpectedSourceSha256") or "")
                current_scan = scan_rollout(source_path)
                if expected_source_sha256 and current_scan.source_sha256 != expected_source_sha256:
                    entry["state"] = "rollback_preserved_changed_active"
                    errors.append(
                        f"{entry['threadId']}: active rollout changed after repair; preserved instead of rolling back"
                    )
                    continue
                if os.name == "nt" and guarded_handle is not None:
                    overwrite_file_through_guarded_handle(
                        restore_path,
                        guarded_handle,
                        remove_source=True,
                    )
                else:
                    os.replace(restore_path, source_path)
            restored_scan = scan_rollout(source_path)
            if restored_scan.source_sha256 != entry["before"]["source_sha256"]:
                raise RuntimeError("restored rollout SHA-256 differs from the baseline")
            entry["state"] = "rolled_back"
        except RolloutWriteConflict as error:
            entry["state"] = "rollback_blocked_by_active_writer"
            errors.append(f"{entry['threadId']}: {error}")
        except Exception as error:
            entry["state"] = "rollback_failed"
            errors.append(f"{entry['threadId']}: {error}")
        finally:
            restore_path.unlink(missing_ok=True)
    return errors


def rollback_official_thread_tool_session_meta(repair_result: dict[str, Any]) -> list[str]:
    installed_entries = [
        entry
        for entry in repair_result.get("threads") or []
        if entry.get("installedSourceSha256")
    ]
    errors = restore_modified_rollouts(installed_entries)
    repair_result["state"] = "rolled_back" if not errors else "rollback_incomplete"
    repair_result["completedCount"] = 0
    repair_result["rolledBackCount"] = sum(
        1 for entry in installed_entries if entry.get("state") == "rolled_back"
    )
    repair_result["rollbackErrors"] = errors
    status_path_text = str(repair_result.get("statusPath") or "")
    if status_path_text:
        write_json_atomic(Path(status_path_text), repair_result)
    return errors


def repair_official_thread_tool_session_meta(
    codex_home_path: Path,
    backup_root: Path,
    status_path: Path,
    *,
    require_codex_stopped: bool = True,
    thread_ids: set[str] | None = None,
) -> dict[str, Any]:
    started_at = utc_timestamp()
    if require_codex_stopped:
        running_processes = active_codex_processes()
        if running_processes:
            raise RuntimeError(f"Codex runtime processes are still running: {running_processes}")

    all_candidates = official_tool_candidates(
        codex_home_path,
        require_contiguous_positions=False,
        thread_ids=thread_ids,
    )
    candidates, blocked_candidates = partition_candidates_by_rollout_identity(
        codex_home_path,
        all_candidates,
    )
    operation_directory = backup_root / (
        datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_official_thread_tool_session_meta"
    )
    operation_directory.mkdir(parents=True, exist_ok=False)
    manifest_path = operation_directory / "manifest.json"
    status: dict[str, Any] = {
        "state": "preparing",
        "startedAt": started_at,
        "updatedAt": utc_timestamp(),
        "codexHome": str(codex_home_path),
        "backupDirectory": str(operation_directory),
        "manifestPath": str(manifest_path),
        "candidateCount": len(all_candidates),
        "safeCandidateCount": len(candidates),
        "blockedCount": len(blocked_candidates),
        "blockedThreads": blocked_candidates,
        "completedCount": 0,
        "threads": [],
    }
    write_json_atomic(status_path, status)

    runtime_blocked_thread_ids: set[str] = set()

    def mark_runtime_blocked(
        candidate: dict[str, Any],
        reason: str,
        **details: Any,
    ) -> None:
        thread_id = str(candidate["threadId"])
        if thread_id in runtime_blocked_thread_ids:
            return
        runtime_blocked_thread_ids.add(thread_id)
        blocked_candidates.append(
            {
                "threadId": thread_id,
                "title": str(candidate.get("title") or "")[:200],
                "rolloutPath": str(candidate["rolloutPath"]),
                "initialSessionMetaIds": list(candidate.get("initialSessionMetaIds") or []),
                "reason": reason,
                **details,
            }
        )
        status["safeCandidateCount"] = len(candidates) - len(runtime_blocked_thread_ids)
        status["blockedCount"] = len(blocked_candidates)
        status["blockedThreads"] = blocked_candidates
        status["updatedAt"] = utc_timestamp()
        write_json_atomic(status_path, status)

    total_source_bytes = sum(Path(candidate["rolloutPath"]).stat().st_size for candidate in candidates)
    free_bytes = shutil.disk_usage(operation_directory).free
    if free_bytes < total_source_bytes + 1024 * 1024 * 1024:
        raise RuntimeError(
            f"backup drive has insufficient free space: need {total_source_bytes} bytes plus 1 GiB reserve"
        )

    entries: list[dict[str, Any]] = []
    installed_entries: list[dict[str, Any]] = []
    completed_entries: list[dict[str, Any]] = []
    try:
        status["state"] = "backing_up_and_auditing"
        write_json_atomic(status_path, status)
        for candidate in candidates:
            rollout_path = Path(candidate["rolloutPath"])
            before_stat = rollout_path.stat()
            before_scan = scan_rollout(rollout_path)
            if before_scan.parse_errors:
                raise RuntimeError(
                    f"rollout has {before_scan.parse_errors} JSON parse errors: {rollout_path}"
                )
            if before_scan.session_meta_id != candidate["threadId"]:
                raise RuntimeError(
                    f"initial session_meta id differs from thread id for {candidate['threadId']}"
                )
            stable_stat = rollout_path.stat()
            if (stable_stat.st_size, stable_stat.st_mtime_ns) != (
                before_stat.st_size,
                before_stat.st_mtime_ns,
            ):
                mark_runtime_blocked(
                    candidate,
                    "rollout_changed_during_baseline_scan",
                    beforeSize=before_stat.st_size,
                    afterSize=stable_stat.st_size,
                )
                continue

            backup_path = operation_directory / f"{candidate['threadId']}.jsonl.before"
            shutil.copy2(rollout_path, backup_path)
            final_stat = rollout_path.stat()
            if (final_stat.st_size, final_stat.st_mtime_ns) != (
                before_stat.st_size,
                before_stat.st_mtime_ns,
            ):
                mark_runtime_blocked(
                    candidate,
                    "rollout_changed_while_creating_backup",
                    beforeSize=before_stat.st_size,
                    afterSize=final_stat.st_size,
                    unverifiedBackupPath=str(backup_path),
                )
                continue
            backup_scan = scan_rollout(backup_path)
            comparable_backup_scan = asdict(backup_scan)
            comparable_backup_scan["path"] = before_scan.path
            if comparable_backup_scan != asdict(before_scan):
                raise RuntimeError(f"backup full scan differs from source: {rollout_path}")
            entry = {
                "threadId": candidate["threadId"],
                "title": candidate["title"],
                "rolloutPath": str(rollout_path),
                "backupPath": str(backup_path),
                "initialSessionMetaLines": candidate["initialSessionMetaLines"],
                "initialToolNamesBefore": candidate["initialToolNames"],
                "before": before_scan.to_dict(),
            }
            entries.append(entry)
            status["threads"] = entries
            status["updatedAt"] = utc_timestamp()
            write_json_atomic(status_path, status)

        write_json_atomic(
            manifest_path,
            {
                "schemaVersion": 2,
                "action": "repair_official_thread_tool_initial_session_meta",
                "createdAt": utc_timestamp(),
                "candidateCount": len(all_candidates),
                "safeCandidateCount": len(entries),
                "blockedCount": len(blocked_candidates),
                "blockedThreads": blocked_candidates,
                "totalSourceBytes": total_source_bytes,
                "threads": entries,
            },
        )

        status["state"] = "rewriting_initial_metadata"
        write_json_atomic(status_path, status)
        candidate_by_id = {candidate["threadId"]: candidate for candidate in candidates}
        for entry in entries:
            candidate = candidate_by_id[entry["threadId"]]
            rollout_path = Path(entry["rolloutPath"])
            before_stat = rollout_path.stat()
            temporary_path = rollout_path.with_name(
                rollout_path.name + f".{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.official-thread-tools.tmp"
            )
            rewritten_count = write_repaired_initial_session_meta(
                rollout_path,
                temporary_path,
                candidate,
            )
            stable_stat = rollout_path.stat()
            if (stable_stat.st_size, stable_stat.st_mtime_ns) != (
                before_stat.st_size,
                before_stat.st_mtime_ns,
            ):
                temporary_path.unlink(missing_ok=True)
                mark_runtime_blocked(
                    candidate,
                    "rollout_changed_before_metadata_replacement",
                    beforeSize=before_stat.st_size,
                    afterSize=stable_stat.st_size,
                )
                entry["state"] = "blocked_before_write"
                continue
            temporary_scan = scan_rollout(temporary_path)
            if temporary_scan.parse_errors:
                raise RuntimeError(f"temporary rollout has JSON parse errors: {temporary_path}")
            if temporary_scan.line_count != int(entry["before"]["line_count"]):
                raise RuntimeError(f"temporary rollout line count changed: {rollout_path}")
            validate_user_prompt_contract(
                temporary_path,
                int(entry["before"]["user_prompt_count"]),
                str(entry["before"]["user_prompt_sha256"]),
                allow_appended=False,
                current_scan=temporary_scan,
            )
            try:
                with rollout_write_guard(rollout_path) as guarded_handle:
                    guarded_stat = rollout_path.stat()
                    guarded_sha256 = sha256_file_prefix(rollout_path, guarded_stat.st_size)
                    if (
                        (guarded_stat.st_size, guarded_stat.st_mtime_ns)
                        != (before_stat.st_size, before_stat.st_mtime_ns)
                        or guarded_sha256 != str(entry["before"]["source_sha256"])
                    ):
                        raise RolloutWriteConflict(
                            f"rollout changed before guarded replacement: {rollout_path}"
                        )
                    try:
                        replacement_mode = install_validated_rollout(
                            temporary_path,
                            rollout_path,
                            guarded_handle,
                        )
                    except Exception:
                        if os.name == "nt" and guarded_handle is not None:
                            overwrite_file_through_guarded_handle(
                                Path(entry["backupPath"]),
                                guarded_handle,
                                remove_source=False,
                            )
                        raise
            except RolloutWriteConflict as error:
                temporary_path.unlink(missing_ok=True)
                mark_runtime_blocked(
                    candidate,
                    "rollout_write_guard_conflict",
                    detail=str(error),
                )
                entry["state"] = "blocked_before_write"
                continue
            except OSError as error:
                if not isinstance(error, PermissionError) and getattr(error, "winerror", None) not in {5, 32, 33}:
                    raise
                temporary_path.unlink(missing_ok=True)
                mark_runtime_blocked(
                    candidate,
                    "rollout_atomic_replace_conflict",
                    detail=str(error),
                )
                entry["state"] = "blocked_before_write"
                continue
            entry["installedSourceSha256"] = temporary_scan.source_sha256
            entry["installedSourceBytes"] = temporary_scan.total_bytes
            entry["rollbackExpectedSourceSha256"] = temporary_scan.source_sha256
            entry["state"] = "installed_pending_verification"
            installed_entries.append(entry)

            after_scan = scan_rollout(rollout_path)
            if after_scan.source_sha256 != temporary_scan.source_sha256:
                prefix_sha256 = sha256_file_prefix(
                    rollout_path,
                    temporary_scan.total_bytes,
                )
                if prefix_sha256 != temporary_scan.source_sha256:
                    raise RuntimeError(
                        f"active rollout does not preserve the validated replacement as a prefix: {rollout_path}"
                    )
                validate_user_prompt_contract(
                    rollout_path,
                    int(entry["before"]["user_prompt_count"]),
                    str(entry["before"]["user_prompt_sha256"]),
                    allow_appended=True,
                    current_scan=after_scan,
                )
                entry["concurrentAppendPreserved"] = True
            entry["rollbackExpectedSourceSha256"] = after_scan.source_sha256
            initial_records = initial_session_meta_records(rollout_path)
            initial_tool_names = [
                {
                    "lineNumber": line_number,
                    "toolNames": sorted(codex_app_tool_names(record)),
                    "hasUnsupportedRepairMarker": has_unsupported_repair_marker(record),
                    "protocol": dynamic_tools_protocol(record),
                }
                for line_number, record in initial_records
            ]
            if not all(
                required_thread_tool_names.issubset(set(item["toolNames"]))
                and not item["hasUnsupportedRepairMarker"]
                and int(item["protocol"]["legacyFlatCount"]) == 0
                for item in initial_tool_names
            ):
                raise RuntimeError(
                    f"initial session_meta still lacks official thread tools or contains unsupported fields: {rollout_path}"
                )

            entry["after"] = after_scan.to_dict()
            entry["state"] = "complete"
            entry["rewrittenInitialSessionMetaCount"] = rewritten_count
            entry["replacementMode"] = replacement_mode
            entry["initialToolNamesAfter"] = initial_tool_names
            completed_entries.append(entry)
            status["completedCount"] = len(completed_entries)
            status["updatedAt"] = utc_timestamp()
            write_json_atomic(status_path, status)

        result = {
            "state": "complete",
            "startedAt": started_at,
            "completedAt": utc_timestamp(),
            "candidateCount": len(all_candidates),
            "safeCandidateCount": len(candidates) - len(runtime_blocked_thread_ids),
            "blockedCount": len(blocked_candidates),
            "blockedThreads": blocked_candidates,
            "completedCount": len(completed_entries),
            "totalSourceBytes": total_source_bytes,
            "backupDirectory": str(operation_directory),
            "manifestPath": str(manifest_path),
            "threads": entries,
        }
        write_json_atomic(manifest_path, {"schemaVersion": 2, **result})
        write_json_atomic(status_path, result)
        return result
    except Exception as error:
        installed_count = len(installed_entries)
        rollback_errors = restore_modified_rollouts(installed_entries)
        rolled_back_count = sum(
            1 for entry in installed_entries if entry.get("state") == "rolled_back"
        )
        failed_result = {
            "state": "failed",
            "startedAt": started_at,
            "failedAt": utc_timestamp(),
            "error": str(error),
            "rollbackErrors": rollback_errors,
            "candidateCount": len(all_candidates),
            "safeCandidateCount": len(candidates) - len(runtime_blocked_thread_ids),
            "blockedCount": len(blocked_candidates),
            "blockedThreads": blocked_candidates,
            "completedCount": 0,
            "installedCount": installed_count,
            "rolledBackCount": rolled_back_count,
            "backupDirectory": str(operation_directory),
            "manifestPath": str(manifest_path),
            "threads": entries,
        }
        write_json_atomic(manifest_path, {"schemaVersion": 2, **failed_result})
        write_json_atomic(status_path, failed_result)
        raise
