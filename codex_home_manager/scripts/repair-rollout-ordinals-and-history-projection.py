from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DATABASE = Path(r"D:\.codex\thread_history_1.sqlite")
DEFAULT_BACKUP_ROOT = Path(r"D:\Backup\codex_rollout_ordinal_projection_repair")
ORDINAL_PREFIX = re.compile(br'^(\{"timestamp":"[^"]+","ordinal":)-?\d+(,"type":)')
PROJECTION_TABLES = ("thread_history_projection_state", "thread_items", "thread_turns")


def is_user_prompt_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    payload = record.get("payload")
    return bool(
        record.get("type") == "user_message"
        or (record.get("type") == "response_item" and isinstance(payload, dict) and payload.get("role") == "user")
        or (record.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message")
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
                text_parts.extend(item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
            if isinstance(payload.get("text"), str):
                text_parts.append(payload["text"])
    elif record_type == "response_item" and isinstance(payload, dict) and payload.get("role") == "user":
        content = payload.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            text_parts.extend(item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
    elif record_type == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
        if isinstance(payload.get("message"), str):
            text_parts.append(payload["message"])
    return tuple(text_parts)


def projection_snapshot(connection: sqlite3.Connection, thread_id: str) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    state = connection.execute(
        "select thread_id, next_rollout_byte_offset, next_rollout_ordinal from thread_history_projection_state where thread_id=?",
        (thread_id,),
    ).fetchone()
    target_counts = {table: connection.execute(f"select count(*) from {table} where thread_id=?", (thread_id,)).fetchone()[0] for table in PROJECTION_TABLES}
    global_counts = {table: connection.execute(f"select count(*) from {table}").fetchone()[0] for table in PROJECTION_TABLES}
    realtime_count = connection.execute("select count(*) from thread_realtime_items where thread_id=?", (thread_id,)).fetchone()[0]
    ordinal_ranges: dict[str, dict[str, int | None]] = {}
    for table in ("thread_items", "thread_turns"):
        row = connection.execute(f"select min(rollout_ordinal), max(rollout_ordinal) from {table} where thread_id=?", (thread_id,)).fetchone()
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
    try:
        record = json.loads(fragment)
        parsed_ordinal = record.get("ordinal") if isinstance(record, dict) else None
        parse_error = ""
    except Exception as error:
        parsed_ordinal = None
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


def strict_scan_rollout(path: Path, expected_thread_id: str) -> dict[str, Any]:
    analysis = analyze_rollout(path, expected_thread_id)
    if analysis["ordinalAnomalies"] or analysis["lastOrdinal"] != analysis["lineCount"] - 1:
        raise RuntimeError("rollout ordinals are not canonical")
    turn_ids: set[str] = set()
    terminal_turn_ids: set[str] = set()
    with path.open("rb") as source:
        for raw_line in source:
            record = json.loads(raw_line)
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            turn_id = payload.get("turn_id")
            if record.get("type") == "event_msg" and payload.get("type") == "task_started" and isinstance(turn_id, str):
                turn_ids.add(turn_id)
            if record.get("type") == "event_msg" and payload.get("type") in {"task_complete", "turn_aborted", "task_failed", "turn_error"} and isinstance(turn_id, str):
                terminal_turn_ids.add(turn_id)
    return {**analysis, "turnCount": len(turn_ids), "terminalTurnCount": len(turn_ids & terminal_turn_ids)}


def running_history_writers() -> list[dict[str, Any]]:
    script = (
        "$items=Get-CimInstance Win32_Process | Where-Object { "
        "(($_.Name -in @('ChatGPT.exe','Codex.exe')) -and "
        "($_.ExecutablePath -like '*WindowsApps\\OpenAI.Codex_*' -or $_.CommandLine -like '*WindowsApps\\OpenAI.Codex_*')) -or "
        "(($_.Name -eq 'codex.exe') -and $_.CommandLine -match 'app-server') }; "
        "$items | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
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
        raise RuntimeError(f"failed to inspect Codex history writers: {result.stderr.strip()}")
    payload = result.stdout.strip()
    if not payload:
        return []
    parsed = json.loads(payload)
    return parsed if isinstance(parsed, list) else [parsed]


def length_prefixed_update(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def analyze_rollout(path: Path, expected_thread_id: str) -> dict[str, Any]:
    prompt_digest = hashlib.sha256()
    semantic_digest = hashlib.sha256()
    file_digest = hashlib.sha256()
    prompt_count = 0
    line_count = 0
    previous_ordinal: int | None = None
    anomalies: list[dict[str, Any]] = []
    session_id = ""
    with path.open("rb") as source:
        for line_count, raw_line in enumerate(source, 1):
            file_digest.update(raw_line)
            try:
                record = json.loads(raw_line)
            except Exception as error:
                raise RuntimeError(f"rollout JSON parse failed at line {line_count}") from error
            if not isinstance(record, dict) or not isinstance(record.get("ordinal"), int):
                raise RuntimeError(f"rollout ordinal is missing or invalid at line {line_count}")
            ordinal = int(record["ordinal"])
            if line_count == 1:
                payload = record.get("payload")
                if record.get("type") != "session_meta" or not isinstance(payload, dict):
                    raise RuntimeError("rollout does not start with session_meta")
                session_id = str(payload.get("id") or payload.get("session_id") or "")
                if ordinal != 0:
                    raise RuntimeError(f"first rollout ordinal must be zero, got {ordinal}")
            if previous_ordinal is not None and ordinal != previous_ordinal + 1:
                payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
                anomalies.append(
                    {
                        "line": line_count,
                        "expected": previous_ordinal + 1,
                        "actual": ordinal,
                        "delta": ordinal - previous_ordinal,
                        "recordType": record.get("type"),
                        "payloadType": payload.get("type"),
                        "timestamp": record.get("timestamp"),
                    }
                )
            previous_ordinal = ordinal
            text_parts = user_prompt_text_parts(record)
            if is_user_prompt_record(record):
                encoded_prompt = json.dumps(text_parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                length_prefixed_update(prompt_digest, encoded_prompt)
                prompt_count += 1
            semantic_record = dict(record)
            semantic_record.pop("ordinal", None)
            encoded_semantic = json.dumps(
                semantic_record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            length_prefixed_update(semantic_digest, encoded_semantic)
    if session_id != expected_thread_id:
        raise RuntimeError(f"rollout session id {session_id!r} does not match {expected_thread_id!r}")
    allowed = bool(anomalies) and all(
        anomaly["delta"] == 0
        and anomaly["recordType"] == "event_msg"
        and anomaly["payloadType"] == "thread_settings_applied"
        for anomaly in anomalies
    )
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": file_digest.hexdigest(),
        "lineCount": line_count,
        "lastOrdinal": previous_ordinal,
        "sessionId": session_id,
        "userPromptCount": prompt_count,
        "userPromptSha256": prompt_digest.hexdigest(),
        "nonOrdinalSemanticSha256": semantic_digest.hexdigest(),
        "ordinalAnomalies": anomalies,
        "normalizationAllowed": allowed,
    }


def write_normalized_candidate(source_path: Path, candidate_path: Path) -> None:
    with source_path.open("rb") as source, candidate_path.open("xb") as destination:
        for zero_based_line, raw_line in enumerate(source):
            replacement = rb"\g<1>" + str(zero_based_line).encode("ascii") + rb"\g<2>"
            normalized_line, replacement_count = ORDINAL_PREFIX.subn(replacement, raw_line, count=1)
            if replacement_count != 1:
                raise RuntimeError(f"top-level ordinal prefix was not uniquely matched at line {zero_based_line + 1}")
            destination.write(normalized_line)
        destination.flush()
        os.fsync(destination.fileno())


def reset_projection(database_path: Path, thread_id: str) -> dict[str, Any]:
    connection = sqlite3.connect(database_path, timeout=15, isolation_level=None)
    try:
        if connection.execute("pragma quick_check").fetchone()[0] != "ok":
            raise RuntimeError("history projection database quick_check failed")
        before = projection_snapshot(connection, thread_id)
        if before["realtimeCount"]:
            raise RuntimeError("target has realtime projection items; repair refused")
        connection.execute("begin exclusive")
        for table in PROJECTION_TABLES:
            connection.execute(f"delete from {table} where thread_id=?", (thread_id,))
        after = projection_snapshot(connection, thread_id)
        for table in PROJECTION_TABLES:
            expected_global = before["globalCounts"][table] - before["targetCounts"][table]
            if after["targetCounts"][table] != 0 or after["globalCounts"][table] != expected_global:
                raise RuntimeError(f"unexpected target/global count after resetting {table}")
        if connection.execute("pragma quick_check").fetchone()[0] != "ok":
            raise RuntimeError("history projection database quick_check failed after reset")
        connection.execute("commit")
        return {"before": before, "after": after}
    except Exception:
        if connection.in_transaction:
            connection.execute("rollback")
        raise
    finally:
        connection.close()


def backup_sqlite_into_existing_directory(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        if destination.execute("pragma quick_check").fetchone()[0] != "ok":
            raise RuntimeError("history projection backup database quick_check failed")
    finally:
        destination.close()
        source.close()


def repair(
    rollout_path: Path,
    database_path: Path,
    thread_id: str,
    backup_root: Path,
    *,
    require_offline: bool,
) -> dict[str, Any]:
    if require_offline:
        processes = running_history_writers()
        if processes:
            raise RuntimeError(f"Codex is still running; offline repair refused: {json.dumps(processes, ensure_ascii=False)}")
    source_before = analyze_rollout(rollout_path, thread_id)
    if not source_before["normalizationAllowed"]:
        raise RuntimeError("rollout anomalies are absent or not limited to duplicate thread_settings_applied ordinals")
    connection = sqlite3.connect(database_path, timeout=15)
    try:
        if connection.execute("pragma quick_check").fetchone()[0] != "ok":
            raise RuntimeError("source history projection database quick_check failed")
        projection_before = projection_snapshot(connection, thread_id)
    finally:
        connection.close()
    cursor = inspect_cursor(rollout_path, projection_before["state"])
    if not projection_before["state"] or not cursor.get("isCorrupt"):
        raise RuntimeError("target history projection cursor is not proven corrupt; repair refused")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_directory = backup_root / f"{timestamp}-{thread_id}"
    backup_directory.mkdir(parents=True, exist_ok=False)
    backup_rollout = backup_directory / rollout_path.name
    backup_database = backup_directory / database_path.name
    shutil.copy2(rollout_path, backup_rollout)
    backup_sqlite_into_existing_directory(database_path, backup_database)
    if analyze_rollout(backup_rollout, thread_id)["sha256"] != source_before["sha256"]:
        raise RuntimeError("rollout backup hash mismatch")

    candidate_path = rollout_path.with_name(f".{rollout_path.name}.{uuid.uuid4().hex}.normalized.tmp")
    manifest_path = backup_directory / "manifest.json"
    replaced_rollout = False
    try:
        write_normalized_candidate(rollout_path, candidate_path)
        candidate = analyze_rollout(candidate_path, thread_id)
        strict_candidate = strict_scan_rollout(candidate_path, thread_id)
        for field in ("lineCount", "sessionId", "userPromptCount", "userPromptSha256", "nonOrdinalSemanticSha256"):
            if candidate[field] != source_before[field]:
                raise RuntimeError(f"normalized rollout contract changed unexpectedly: {field}")
        if candidate["ordinalAnomalies"] or candidate["lastOrdinal"] != candidate["lineCount"] - 1:
            raise RuntimeError("normalized rollout is not canonically ordinaled")
        os.replace(candidate_path, rollout_path)
        replaced_rollout = True
        projection = reset_projection(database_path, thread_id)
        source_after = analyze_rollout(rollout_path, thread_id)
        for field in ("lineCount", "sessionId", "userPromptCount", "userPromptSha256", "nonOrdinalSemanticSha256"):
            if source_after[field] != source_before[field]:
                raise RuntimeError(f"post-repair rollout contract changed unexpectedly: {field}")
        result = {
            "status": "success",
            "threadId": thread_id,
            "rolloutPath": str(rollout_path),
            "databasePath": str(database_path),
            "backupDirectory": str(backup_directory),
            "backupRolloutPath": str(backup_rollout),
            "backupDatabasePath": str(backup_database),
            "cursorBefore": cursor,
            "rolloutBefore": source_before,
            "rolloutAfter": source_after,
            "strictRolloutAfter": strict_candidate,
            "projection": projection,
        }
        manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"manifestPath": str(manifest_path), **result}
    except Exception:
        if candidate_path.exists():
            candidate_path.unlink()
        if replaced_rollout:
            shutil.copy2(backup_rollout, rollout_path)
            shutil.copy2(backup_database, database_path)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair duplicate restart ordinals and reset one derived history projection.")
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
    preview = analyze_rollout(args.rollout, args.thread_id)
    if not args.apply:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0
    if args.confirm_thread_id != args.thread_id:
        raise RuntimeError("--confirm-thread-id must exactly match --thread-id")
    result = repair(
        args.rollout,
        args.database,
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
