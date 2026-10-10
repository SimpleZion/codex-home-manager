from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DATABASE = Path(r"D:\.codex\thread_history_1.sqlite")
DEFAULT_BACKUP_ROOT = Path(r"D:\Backup\codex_thread_history_projection_repair")
PROJECTION_TABLES = (
    "thread_history_projection_state",
    "thread_items",
    "thread_turns",
)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_user_prompt_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    record_type = record.get("type")
    payload = record.get("payload")
    return bool(
        record_type == "user_message"
        or (record_type == "response_item" and isinstance(payload, dict) and payload.get("role") == "user")
        or (
            record_type == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "user_message"
        )
    )


def user_prompt_text_parts(record: Any) -> tuple[str, ...]:
    if not isinstance(record, dict):
        return ()
    record_type = record.get("type")
    payload = record.get("payload")
    text_parts: list[str] = []
    if record_type == "user_message":
        if isinstance(payload, str):
            text_parts.append(payload)
        elif isinstance(payload, dict):
            content = payload.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                text_parts.extend(
                    item["text"]
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
            if isinstance(payload.get("text"), str):
                text_parts.append(payload["text"])
    elif record_type == "response_item" and isinstance(payload, dict) and payload.get("role") == "user":
        content = payload.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            text_parts.extend(
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
    elif record_type == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
        message = payload.get("message")
        if isinstance(message, str):
            text_parts.append(message)
    return tuple(text_parts)


def scan_rollout(path: Path, expected_thread_id: str) -> dict[str, Any]:
    file_digest = hashlib.sha256()
    prompt_digest = hashlib.sha256()
    line_count = 0
    prompt_count = 0
    turn_ids: set[str] = set()
    terminal_turn_ids: set[str] = set()
    expected_ordinal = 0
    session_id = ""
    with path.open("rb") as source:
        for line_count, raw_line in enumerate(source, 1):
            file_digest.update(raw_line)
            try:
                record = json.loads(raw_line)
            except Exception as error:
                raise RuntimeError(f"rollout JSON parse failed at line {line_count}") from error
            ordinal = record.get("ordinal") if isinstance(record, dict) else None
            if ordinal != expected_ordinal:
                raise RuntimeError(
                    f"rollout ordinal discontinuity at line {line_count}: expected {expected_ordinal}, got {ordinal!r}"
                )
            expected_ordinal += 1
            if line_count == 1:
                payload = record.get("payload") if isinstance(record, dict) else None
                if record.get("type") != "session_meta" or not isinstance(payload, dict):
                    raise RuntimeError("rollout does not start with session_meta")
                session_id = str(payload.get("id") or payload.get("session_id") or "")
            payload = record.get("payload") if isinstance(record, dict) and isinstance(record.get("payload"), dict) else {}
            turn_id = payload.get("turn_id")
            if record.get("type") == "event_msg" and payload.get("type") == "task_started" and isinstance(turn_id, str):
                turn_ids.add(turn_id)
            if record.get("type") == "event_msg" and payload.get("type") in {"task_complete", "turn_aborted", "task_failed", "turn_error"} and isinstance(turn_id, str):
                terminal_turn_ids.add(turn_id)
            text_parts = user_prompt_text_parts(record)
            if is_user_prompt_record(record):
                encoded = json.dumps(text_parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                prompt_digest.update(len(encoded).to_bytes(8, "big"))
                prompt_digest.update(encoded)
                prompt_count += 1
    if session_id != expected_thread_id:
        raise RuntimeError(f"rollout session id {session_id!r} does not match {expected_thread_id!r}")
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": file_digest.hexdigest(),
        "lineCount": line_count,
        "lastOrdinal": expected_ordinal - 1,
        "sessionId": session_id,
        "userPromptCount": prompt_count,
        "userPromptSha256": prompt_digest.hexdigest(),
        "turnCount": len(turn_ids),
        "terminalTurnCount": len(turn_ids & terminal_turn_ids),
    }


def projection_snapshot(connection: sqlite3.Connection, thread_id: str) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    state = connection.execute(
        "select thread_id, next_rollout_byte_offset, next_rollout_ordinal from thread_history_projection_state where thread_id=?",
        (thread_id,),
    ).fetchone()
    target_counts = {
        table: connection.execute(f"select count(*) from {table} where thread_id=?", (thread_id,)).fetchone()[0]
        for table in PROJECTION_TABLES
    }
    global_counts = {
        table: connection.execute(f"select count(*) from {table}").fetchone()[0]
        for table in PROJECTION_TABLES
    }
    realtime_count = connection.execute(
        "select count(*) from thread_realtime_items where thread_id=?", (thread_id,)
    ).fetchone()[0]
    ordinal_ranges: dict[str, dict[str, int | None]] = {}
    for table in ("thread_items", "thread_turns"):
        row = connection.execute(
            f"select min(rollout_ordinal), max(rollout_ordinal) from {table} where thread_id=?", (thread_id,)
        ).fetchone()
        ordinal_ranges[table] = {"minimum": row[0], "maximum": row[1]}
    return {
        "state": dict(state) if state else None,
        "targetCounts": target_counts,
        "globalCounts": global_counts,
        "realtimeCount": realtime_count,
        "ordinalRanges": ordinal_ranges,
    }


def inspect_cursor(rollout_path: Path, state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {"present": False}
    offset = int(state["next_rollout_byte_offset"])
    expected_ordinal = int(state["next_rollout_ordinal"])
    file_size = rollout_path.stat().st_size
    if offset < 0 or offset > file_size:
        return {"present": True, "offset": offset, "expectedOrdinal": expected_ordinal, "offsetInRange": False}
    with rollout_path.open("rb") as source:
        previous = b"\n"
        if offset:
            source.seek(offset - 1)
            previous = source.read(1)
        source.seek(offset)
        fragment = source.readline()
    at_line_boundary = offset == 0 or previous == b"\n"
    parsed_ordinal: int | None = None
    parse_error = ""
    try:
        record = json.loads(fragment)
        parsed_ordinal = record.get("ordinal") if isinstance(record, dict) else None
    except Exception as error:
        parse_error = str(error)
    return {
        "present": True,
        "offset": offset,
        "expectedOrdinal": expected_ordinal,
        "offsetInRange": True,
        "atLineBoundary": at_line_boundary,
        "fragmentOrdinal": parsed_ordinal,
        "fragmentParseError": parse_error,
        "isCorrupt": not at_line_boundary or parsed_ordinal != expected_ordinal,
    }


def running_codex_processes() -> list[dict[str, Any]]:
    script = (
        "$items=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -in @('ChatGPT.exe','Codex.exe','codex.exe','Code.exe') -or "
        "($_.Name -in @('node.exe','node_repl.exe','extension-host.exe') -and "
        "$_.CommandLine -match 'app-server|mcp[/\\\\]server[.]mjs|code-mode-host') }; "
        "$items | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to inspect Codex processes: {result.stderr.strip()}")
    payload = result.stdout.strip()
    if not payload:
        return []
    parsed = json.loads(payload)
    return parsed if isinstance(parsed, list) else [parsed]


def sqlite_backup(source_path: Path, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=False)
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        if destination.execute("pragma quick_check").fetchone()[0] != "ok":
            raise RuntimeError("backup database quick_check failed")
    finally:
        destination.close()
        source.close()


def repair_projection(
    database_path: Path,
    rollout_path: Path,
    thread_id: str,
    backup_root: Path,
    *,
    require_offline: bool,
) -> dict[str, Any]:
    if require_offline:
        processes = running_codex_processes()
        if processes:
            raise RuntimeError(f"Codex is still running; offline repair refused: {json.dumps(processes, ensure_ascii=False)}")
    rollout_before = scan_rollout(rollout_path, thread_id)
    connection = sqlite3.connect(database_path, timeout=5)
    try:
        if connection.execute("pragma quick_check").fetchone()[0] != "ok":
            raise RuntimeError("source database quick_check failed")
        before = projection_snapshot(connection, thread_id)
    finally:
        connection.close()
    cursor = inspect_cursor(rollout_path, before["state"])
    if not before["state"]:
        raise RuntimeError("target thread has no projection state to repair")
    if not cursor.get("isCorrupt"):
        raise RuntimeError("target projection cursor is not proven corrupt; repair refused")
    if before["realtimeCount"]:
        raise RuntimeError("target has realtime projection items; repair scope requires review")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_directory = backup_root / f"{timestamp}-{thread_id}"
    backup_database = backup_directory / database_path.name
    sqlite_backup(database_path, backup_database)
    manifest_path = backup_directory / "manifest.json"
    manifest = {
        "threadId": thread_id,
        "databasePath": str(database_path),
        "backupDatabasePath": str(backup_database),
        "rollout": rollout_before,
        "projectionBefore": before,
        "cursor": cursor,
        "status": "backup-created",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    connection = sqlite3.connect(database_path, timeout=5, isolation_level=None)
    try:
        connection.execute("begin exclusive")
        for table in PROJECTION_TABLES:
            connection.execute(f"delete from {table} where thread_id=?", (thread_id,))
        after = projection_snapshot(connection, thread_id)
        for table in PROJECTION_TABLES:
            expected_global = before["globalCounts"][table] - before["targetCounts"][table]
            if after["globalCounts"][table] != expected_global or after["targetCounts"][table] != 0:
                raise RuntimeError(f"unexpected row count after deleting target rows from {table}")
        if connection.execute("pragma quick_check").fetchone()[0] != "ok":
            raise RuntimeError("repaired database quick_check failed")
        connection.execute("commit")
    except Exception:
        if connection.in_transaction:
            connection.execute("rollback")
        raise
    finally:
        connection.close()

    rollout_after = scan_rollout(rollout_path, thread_id)
    for field in ("sha256", "userPromptCount", "userPromptSha256", "lineCount", "lastOrdinal"):
        if rollout_after[field] != rollout_before[field]:
            raise RuntimeError(f"rollout contract changed unexpectedly: {field}")
    manifest.update({"projectionAfter": after, "rolloutAfter": rollout_after, "status": "projection-reset"})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"backupDirectory": str(backup_directory), "manifestPath": str(manifest_path), **manifest}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reset one corrupt Codex thread history projection from its canonical rollout.")
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--rollout", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-thread-id")
    parser.add_argument("--allow-running-for-copy-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rollout = scan_rollout(args.rollout, args.thread_id)
    connection = sqlite3.connect(args.database)
    try:
        snapshot = projection_snapshot(connection, args.thread_id)
    finally:
        connection.close()
    preview = {
        "threadId": args.thread_id,
        "databasePath": str(args.database),
        "rollout": rollout,
        "projection": snapshot,
        "cursor": inspect_cursor(args.rollout, snapshot["state"]),
    }
    if not args.apply:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0
    if args.confirm_thread_id != args.thread_id:
        raise RuntimeError("--confirm-thread-id must exactly match --thread-id")
    result = repair_projection(
        args.database,
        args.rollout,
        args.thread_id,
        args.backup_root,
        require_offline=not args.allow_running_for_copy_test,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)
