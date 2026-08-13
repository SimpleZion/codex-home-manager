from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

prompt_index_schema_version = 3
prompt_index_boundary_bytes = 64 * 1024
prompt_index_commit_records = 256
prompt_index_commit_bytes = 8 * 1024 * 1024
prompt_index_scan_chunk_bytes = 1024 * 1024
prompt_index_candidate_overlap_bytes = 512
prompt_index_direct_candidate_bytes = 4 * 1024 * 1024
prompt_index_redacted_attachment = "[附件内容已隐藏]".encode("utf-8")


class PromptIndexCancelled(RuntimeError):
    pass


_request_lock = threading.Lock()
_request_events: dict[str, tuple[str, threading.Event]] = {}
_file_locks_lock = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}


def prompt_index_root_path() -> Path:
    configured_root = os.environ.get("CODEX_HOME_MANAGER_PROMPT_INDEX_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve(strict=False)
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        user_data_root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return (user_data_root / "CodexHomeManager" / "prompt-indexes").resolve(strict=False)
    return (Path(__file__).resolve().parents[1] / "data" / "prompt-indexes").resolve(strict=False)


def prompt_index_database_path(codex_home_path: Path) -> Path:
    normalized_home = os.path.normcase(str(codex_home_path.expanduser().resolve(strict=False)))
    database_name = hashlib.sha256(normalized_home.encode("utf-8")).hexdigest() + ".sqlite"
    return prompt_index_root_path() / database_name


def begin_prompt_index_request(thread_id: str, request_id: str | None = None) -> tuple[str, threading.Event]:
    normalized_request_id = str(request_id or uuid.uuid4().hex).strip()
    if not normalized_request_id or len(normalized_request_id) > 128:
        raise ValueError("invalid prompt request id")
    event = threading.Event()
    with _request_lock:
        if normalized_request_id in _request_events:
            raise ValueError("prompt request id is already active")
        _request_events[normalized_request_id] = (thread_id, event)
    return normalized_request_id, event


def finish_prompt_index_request(request_id: str) -> None:
    with _request_lock:
        _request_events.pop(request_id, None)


def cancel_prompt_index_request(thread_id: str, request_id: str) -> bool:
    with _request_lock:
        request = _request_events.get(request_id)
        if request is None or request[0] != thread_id:
            return False
        request[1].set()
        return True


def _file_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _file_locks_lock:
        lock = _file_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _file_locks[key] = lock
        return lock


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    previous_schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS prompt_files (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            device TEXT NOT NULL,
            inode TEXT NOT NULL,
            created_ns INTEGER NOT NULL,
            generation TEXT NOT NULL,
            observed_size INTEGER NOT NULL,
            observed_mtime_ns INTEGER NOT NULL,
            scanned_offset INTEGER NOT NULL,
            scanned_line_count INTEGER NOT NULL,
            boundary_hash TEXT NOT NULL,
            complete INTEGER NOT NULL,
            updated_at_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prompts (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL REFERENCES prompt_files(id) ON DELETE CASCADE,
            prompt_index INTEGER NOT NULL,
            line_number INTEGER NOT NULL,
            byte_offset INTEGER NOT NULL,
            timestamp TEXT,
            timestamp_ms INTEGER,
            protocol TEXT NOT NULL,
            text TEXT NOT NULL,
            search_text TEXT NOT NULL,
            character_count INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            source_label TEXT NOT NULL,
            visible_by_default INTEGER NOT NULL,
            pure_text TEXT NOT NULL,
            pure_search_text TEXT NOT NULL,
            pure_character_count INTEGER NOT NULL,
            has_pure_text INTEGER NOT NULL,
            UNIQUE(file_id, prompt_index)
        );
        CREATE INDEX IF NOT EXISTS prompts_file_order_idx ON prompts(file_id, prompt_index);
        CREATE INDEX IF NOT EXISTS prompts_file_source_idx ON prompts(file_id, source_type, prompt_index);
        """
    )
    if previous_schema_version != prompt_index_schema_version:
        # Parsed prompt rows are derived data. Rebuild them whenever the schema
        # or extraction semantics change instead of serving stale cached rows.
        connection.execute("DELETE FROM prompt_files")
        connection.execute(f"PRAGMA user_version={prompt_index_schema_version}")
        connection.commit()
    return connection


def _file_identity(stat_result: os.stat_result) -> tuple[str, str, int]:
    return (
        f"dev:{int(getattr(stat_result, 'st_dev', 0) or 0)}",
        f"ino:{int(getattr(stat_result, 'st_ino', 0) or 0)}",
        int(getattr(stat_result, "st_ctime_ns", int(stat_result.st_ctime * 1_000_000_000))),
    )


def _same_file_identity(row: sqlite3.Row, stat_result: os.stat_result) -> bool:
    device, inode, created_ns = _file_identity(stat_result)
    if inode != "ino:0" and str(row["inode"]) != "ino:0":
        return device == str(row["device"]) and inode == str(row["inode"])
    return created_ns == int(row["created_ns"])


def _boundary_hash(file, scanned_offset: int) -> str:
    if scanned_offset <= 0:
        return hashlib.sha256(b"").hexdigest()
    original_position = file.tell()
    boundary_start = max(0, scanned_offset - prompt_index_boundary_bytes)
    file.seek(boundary_start)
    digest = hashlib.sha256(file.read(scanned_offset - boundary_start)).hexdigest()
    file.seek(original_position)
    return digest


def _reset_file_index(
    connection: sqlite3.Connection,
    path_text: str,
    stat_result: os.stat_result,
    existing_row: sqlite3.Row | None,
) -> sqlite3.Row:
    device, inode, created_ns = _file_identity(stat_result)
    generation = uuid.uuid4().hex
    if existing_row is None:
        connection.execute(
            """
            INSERT INTO prompt_files(
                path, device, inode, created_ns, generation, observed_size, observed_mtime_ns,
                scanned_offset, scanned_line_count, boundary_hash, complete, updated_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, ?)
            """,
            (
                path_text,
                device,
                inode,
                created_ns,
                generation,
                int(stat_result.st_size),
                int(stat_result.st_mtime_ns),
                hashlib.sha256(b"").hexdigest(),
                time.time_ns(),
            ),
        )
    else:
        connection.execute("DELETE FROM prompts WHERE file_id = ?", (int(existing_row["id"]),))
        connection.execute(
            """
            UPDATE prompt_files
            SET device = ?, inode = ?, created_ns = ?, generation = ?, observed_size = ?,
                observed_mtime_ns = ?, scanned_offset = 0, scanned_line_count = 0,
                boundary_hash = ?, complete = 0, updated_at_ns = ?
            WHERE id = ?
            """,
            (
                device,
                inode,
                created_ns,
                generation,
                int(stat_result.st_size),
                int(stat_result.st_mtime_ns),
                hashlib.sha256(b"").hexdigest(),
                time.time_ns(),
                int(existing_row["id"]),
            ),
        )
    connection.commit()
    row = connection.execute("SELECT * FROM prompt_files WHERE path = ?", (path_text,)).fetchone()
    if row is None:
        raise RuntimeError("failed to initialize prompt index metadata")
    return row


def _prepare_file_index(connection: sqlite3.Connection, rollout_path: Path) -> tuple[sqlite3.Row, bool]:
    path_text = str(rollout_path.resolve(strict=False))
    stat_result = rollout_path.stat()
    row = connection.execute("SELECT * FROM prompt_files WHERE path = ?", (path_text,)).fetchone()
    reset = row is None or not _same_file_identity(row, stat_result)
    if row is not None and not reset:
        scanned_offset = int(row["scanned_offset"])
        if stat_result.st_size < scanned_offset:
            reset = True
        elif stat_result.st_size == int(row["observed_size"]) and stat_result.st_mtime_ns != int(row["observed_mtime_ns"]):
            reset = True
        elif scanned_offset:
            with rollout_path.open("rb") as file:
                if _boundary_hash(file, scanned_offset) != str(row["boundary_hash"]):
                    reset = True
    if reset:
        row = _reset_file_index(connection, path_text, stat_result, row)
    return row, reset


def _scope_clause(scope: str) -> tuple[str, list[Any]]:
    normalized_scope = (scope or "visible").strip().lower()
    if normalized_scope in {"pure", "text", "user_text", "user-text"}:
        return "has_pure_text = 1", []
    if normalized_scope == "all":
        return "1 = 1", []
    if normalized_scope in {"automation", "automations", "heartbeat", "heartbeats"}:
        return "source_type = ?", ["automation"]
    if normalized_scope in {"delegation", "delegations", "thread_delegation", "thread-delegation", "handoff", "handoffs"}:
        return "source_type = ?", ["delegation"]
    if normalized_scope in {"with_agents", "with-agent", "with_agents_and_user", "agents"}:
        return "(visible_by_default = 1 OR source_type = 'subagent')", []
    if normalized_scope == "visible":
        return "visible_by_default = 1", []
    raise ValueError(f"unsupported prompt scope: {scope}")


def _prompt_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "index": int(row["prompt_index"]),
        "lineNumber": int(row["line_number"]),
        "byteOffset": int(row["byte_offset"]),
        "timestamp": row["timestamp"],
        "text": str(row["text"]),
        "characterCount": int(row["character_count"]),
        "protocol": str(row["protocol"]),
        "sourceType": str(row["source_type"]),
        "sourceLabel": str(row["source_label"]),
        "visibleByDefault": bool(row["visible_by_default"]),
        "pureText": str(row["pure_text"]),
        "pureCharacterCount": int(row["pure_character_count"]),
        "hasPureText": bool(row["has_pure_text"]),
    }


def _check_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise PromptIndexCancelled("prompt indexing was cancelled")


def _redacted_candidate_json_bytes(
    file,
    line_start: int,
    line_end: int,
    cancel_check: Callable[[], bool] | None,
) -> bytes:
    line_size = line_end - line_start
    file.seek(line_start)
    if line_size <= prompt_index_direct_candidate_bytes:
        return file.read(line_size)

    output = bytearray()
    inside_string = False
    escaped = False
    data_probe: bytearray | None = None
    skipping_data_payload = False
    remaining = line_size
    data_prefix = b"data:"
    payload_delimiters = b" \t\r\n<>\"'\\"

    def append_normal(byte_value: int) -> None:
        nonlocal inside_string, escaped, data_probe
        if not inside_string:
            output.append(byte_value)
            if byte_value == 34:
                inside_string = True
            return
        if escaped:
            output.append(byte_value)
            escaped = False
            return
        if byte_value == 92:
            output.append(byte_value)
            escaped = True
            return
        if byte_value == 34:
            output.append(byte_value)
            inside_string = False
            return
        if byte_value in {68, 100}:
            data_probe = bytearray([byte_value])
            return
        output.append(byte_value)

    while remaining > 0:
        _check_cancelled(cancel_check)
        chunk = file.read(min(prompt_index_scan_chunk_bytes, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        for byte_value in chunk:
            if skipping_data_payload:
                if byte_value in payload_delimiters:
                    skipping_data_payload = False
                    append_normal(byte_value)
                continue
            if data_probe is not None:
                data_probe.append(byte_value)
                lowered_probe = bytes(data_probe).lower()
                if len(data_probe) <= len(data_prefix):
                    if data_prefix.startswith(lowered_probe):
                        continue
                    output.extend(data_probe)
                    data_probe = None
                    continue
                header = data_probe[len(data_prefix) :]
                if byte_value == 44 and 2 <= len(header) <= 201:
                    output.extend(prompt_index_redacted_attachment)
                    data_probe = None
                    skipping_data_payload = True
                    continue
                if byte_value in payload_delimiters or len(header) > 201:
                    output.extend(data_probe)
                    data_probe = None
                    continue
                continue
            append_normal(byte_value)
    if data_probe is not None:
        output.extend(data_probe)
    file.seek(line_end)
    return bytes(output)


def update_prompt_index(
    database_path: Path,
    rollout_path: Path,
    *,
    candidate_check: Callable[[bytes], bool],
    extract_prompt: Callable[[dict[str, Any]], tuple[str, str] | None],
    classify_prompt: Callable[[str], dict[str, Any]],
    timestamp_to_ms: Callable[[Any], int | None],
    is_duplicate: Callable[[str, Any, str, int, list[tuple[str, int | None, str, int]]], bool],
    max_scan_ms: int | None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    scan_budget_seconds = None if max_scan_ms is None else max(1, int(max_scan_ms)) / 1000
    rollout_path = rollout_path.expanduser().resolve(strict=False)
    if not rollout_path.is_file():
        raise FileNotFoundError(str(rollout_path))

    with _file_lock(rollout_path), closing(_connect(database_path)) as connection:
        row, reset = _prepare_file_index(connection, rollout_path)
        scan_start_offset = int(row["scanned_offset"])
        scan_start_line = int(row["scanned_line_count"])
        stat_before = rollout_path.stat()
        if (
            bool(row["complete"])
            and int(row["observed_size"]) == stat_before.st_size
            and int(row["observed_mtime_ns"]) == stat_before.st_mtime_ns
        ):
            return _index_state(row, stat_before, reset, scan_start_offset, started_at, 0)

        recent_rows = connection.execute(
            """
            SELECT text, timestamp_ms, protocol, line_number
            FROM prompts WHERE file_id = ? ORDER BY prompt_index DESC LIMIT 8
            """,
            (int(row["id"]),),
        ).fetchall()
        recent_prompts = [
            (str(item["text"]).strip(), item["timestamp_ms"], str(item["protocol"]), int(item["line_number"]))
            for item in reversed(recent_rows)
        ]
        prompt_index = int(
            connection.execute(
                "SELECT COALESCE(MAX(prompt_index), 0) FROM prompts WHERE file_id = ?", (int(row["id"]),)
            ).fetchone()[0]
        )
        scanned_offset = scan_start_offset
        line_number = scan_start_line
        batch_records: list[tuple[Any, ...]] = []
        batch_bytes = 0
        indexed_records = 0
        reached_eof = False

        def commit_progress(file, complete: bool) -> sqlite3.Row:
            nonlocal batch_records, batch_bytes
            if batch_records:
                connection.executemany(
                    """
                    INSERT INTO prompts(
                        file_id, prompt_index, line_number, byte_offset, timestamp, timestamp_ms,
                        protocol, text, search_text, character_count, source_type, source_label,
                        visible_by_default, pure_text, pure_search_text, pure_character_count, has_pure_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch_records,
                )
            current_stat = os.fstat(file.fileno())
            connection.execute(
                """
                UPDATE prompt_files
                SET observed_size = ?, observed_mtime_ns = ?, scanned_offset = ?,
                    scanned_line_count = ?, boundary_hash = ?, complete = ?, updated_at_ns = ?
                WHERE id = ?
                """,
                (
                    int(current_stat.st_size),
                    int(current_stat.st_mtime_ns),
                    scanned_offset,
                    line_number,
                    _boundary_hash(file, scanned_offset),
                    int(complete),
                    time.time_ns(),
                    int(row["id"]),
                ),
            )
            connection.commit()
            batch_records = []
            batch_bytes = 0
            updated_row = connection.execute("SELECT * FROM prompt_files WHERE id = ?", (int(row["id"]),)).fetchone()
            if updated_row is None:
                raise RuntimeError("prompt index metadata disappeared")
            return updated_row

        scan_deadline = None if scan_budget_seconds is None else time.perf_counter() + scan_budget_seconds
        with rollout_path.open("rb") as file:
            file.seek(scan_start_offset)
            while True:
                _check_cancelled(cancel_check)
                if scan_deadline is not None and time.perf_counter() >= scan_deadline and scanned_offset > scan_start_offset:
                    break
                line_start = file.tell()
                candidate_line = False
                candidate_probe_tail = b""
                line_has_newline = False
                stopped_for_budget = False
                while True:
                    _check_cancelled(cancel_check)
                    if scan_deadline is not None and time.perf_counter() >= scan_deadline and file.tell() > line_start:
                        stopped_for_budget = True
                        break
                    chunk = file.readline(prompt_index_scan_chunk_bytes)
                    if not chunk:
                        break
                    if not candidate_line:
                        candidate_probe = candidate_probe_tail + chunk
                        candidate_line = candidate_check(candidate_probe)
                        candidate_probe_tail = candidate_probe[-prompt_index_candidate_overlap_bytes:]
                    if chunk.endswith(b"\n"):
                        line_has_newline = True
                        break
                line_end = file.tell()
                if stopped_for_budget:
                    file.seek(line_start)
                    break
                if line_end == line_start:
                    reached_eof = True
                    break
                if not line_has_newline and not candidate_line:
                    file.seek(line_start)
                    break
                raw_line: bytes | None = None
                if candidate_line:
                    raw_line = _redacted_candidate_json_bytes(file, line_start, line_end, cancel_check)
                parsed_item: dict[str, Any] | None = None
                if raw_line is not None:
                    try:
                        candidate_item = json.loads(raw_line)
                        if isinstance(candidate_item, dict):
                            parsed_item = candidate_item
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        if not line_has_newline:
                            file.seek(line_start)
                            break
                line_number += 1
                scanned_offset = line_end
                batch_bytes += line_end - line_start
                if parsed_item is not None:
                    extracted = extract_prompt(parsed_item)
                    if extracted is not None:
                        text, protocol = extracted
                        text = text.strip()
                        timestamp = parsed_item.get("timestamp")
                        if text and not is_duplicate(text, timestamp, protocol, line_number, recent_prompts):
                            prompt_index += 1
                            classification = classify_prompt(text)
                            pure_text = str(classification.get("pureText") or "")
                            batch_records.append(
                                (
                                    int(row["id"]),
                                    prompt_index,
                                    line_number,
                                    line_start,
                                    timestamp if isinstance(timestamp, str) else None,
                                    timestamp_to_ms(timestamp),
                                    protocol,
                                    text,
                                    text.casefold(),
                                    len(text),
                                    str(classification.get("sourceType") or "user"),
                                    str(classification.get("sourceLabel") or "用户输入"),
                                    int(classification.get("visibleByDefault") is not False),
                                    pure_text,
                                    pure_text.casefold(),
                                    int(classification.get("pureCharacterCount") or len(pure_text)),
                                    int(bool(classification.get("hasPureText"))),
                                )
                            )
                            indexed_records += 1
                            recent_prompts.append(
                                (text, timestamp_to_ms(timestamp), protocol, line_number)
                            )
                            recent_prompts = recent_prompts[-8:]
                if len(batch_records) >= prompt_index_commit_records or batch_bytes >= prompt_index_commit_bytes:
                    row = commit_progress(file, complete=False)
                    _check_cancelled(cancel_check)
                if not line_has_newline:
                    reached_eof = True
                    break
            stat_at_end = os.fstat(file.fileno())
            complete = reached_eof and scanned_offset >= int(stat_at_end.st_size)
            row = commit_progress(file, complete=complete)

        row, reset_after_scan = _prepare_file_index(connection, rollout_path)
        reset = reset or reset_after_scan
        if reset_after_scan:
            indexed_records = 0
        return _index_state(row, os.stat(rollout_path), reset, scan_start_offset, started_at, indexed_records)


def _index_state(
    row: sqlite3.Row,
    stat_result: os.stat_result,
    reset: bool,
    scan_start_offset: int,
    started_at: float,
    indexed_records: int,
) -> dict[str, Any]:
    return {
        "generation": str(row["generation"]),
        "fileIdentity": {
            "device": str(row["device"]),
            "inode": str(row["inode"]),
            "createdNs": int(row["created_ns"]),
        },
        "fileSize": int(stat_result.st_size),
        "fileMtimeNs": int(stat_result.st_mtime_ns),
        "scannedBytes": int(row["scanned_offset"]),
        "scannedLines": int(row["scanned_line_count"]),
        "scanStartOffset": scan_start_offset,
        "scanAddedPrompts": indexed_records,
        "complete": bool(row["complete"]) and int(row["scanned_offset"]) >= int(stat_result.st_size),
        "reset": reset,
        "elapsedMs": round((time.perf_counter() - started_at) * 1000, 3),
    }


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw_payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid prompt cursor") from error
    if not isinstance(payload, dict):
        raise ValueError("invalid prompt cursor")
    return payload


def _query_signature(scope: str, search: str, source_type: str | None) -> str:
    value = json.dumps(
        {"scope": scope.strip().lower(), "search": search.casefold(), "sourceType": source_type or ""},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def read_prompt_page(
    database_path: Path,
    rollout_path: Path,
    *,
    scope: str,
    search: str,
    source_type: str | None,
    cursor: str | None,
    limit: int,
    index_state: dict[str, Any],
) -> dict[str, Any]:
    safe_limit = max(1, min(500, int(limit)))
    scope_sql, scope_parameters = _scope_clause(scope)
    normalized_search = search.strip().casefold()
    query_signature = _query_signature(scope, search, source_type)
    after_index = 0
    if cursor:
        cursor_payload = _decode_cursor(cursor)
        if cursor_payload.get("generation") != index_state["generation"]:
            raise ValueError("prompt cursor is stale because the rollout index was rebuilt")
        if cursor_payload.get("query") != query_signature:
            raise ValueError("prompt cursor does not match the current filters")
        after_index = max(0, int(cursor_payload.get("after") or 0))

    path_text = str(rollout_path.expanduser().resolve(strict=False))
    with closing(_connect(database_path)) as connection:
        file_row = connection.execute("SELECT * FROM prompt_files WHERE path = ?", (path_text,)).fetchone()
        if file_row is None:
            raise RuntimeError("prompt index metadata is missing")
        clauses = ["file_id = ?", "prompt_index > ?", scope_sql]
        parameters: list[Any] = [int(file_row["id"]), after_index, *scope_parameters]
        if source_type:
            clauses.append("source_type = ?")
            parameters.append(source_type)
        if normalized_search:
            clauses.append("(instr(search_text, ?) > 0 OR instr(pure_search_text, ?) > 0)")
            parameters.extend([normalized_search, normalized_search])
        where_sql = " AND ".join(f"({clause})" for clause in clauses)
        rows = connection.execute(
            f"SELECT * FROM prompts WHERE {where_sql} ORDER BY prompt_index LIMIT ?",
            (*parameters, safe_limit + 1),
        ).fetchall()
        has_indexed_more = len(rows) > safe_limit
        page_rows = rows[:safe_limit]
        prompts = [_prompt_from_row(row) for row in page_rows]
        last_index = prompts[-1]["index"] if prompts else after_index
        next_cursor = None
        if has_indexed_more or not index_state["complete"]:
            next_cursor = _encode_cursor(
                {"generation": index_state["generation"], "query": query_signature, "after": last_index}
            )
        count_clauses = ["file_id = ?", scope_sql]
        count_parameters: list[Any] = [int(file_row["id"]), *scope_parameters]
        if source_type:
            count_clauses.append("source_type = ?")
            count_parameters.append(source_type)
        if normalized_search:
            count_clauses.append("(instr(search_text, ?) > 0 OR instr(pure_search_text, ?) > 0)")
            count_parameters.extend([normalized_search, normalized_search])
        match_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM prompts WHERE {' AND '.join(f'({clause})' for clause in count_clauses)}",
                count_parameters,
            ).fetchone()[0]
        )
        summary = prompt_index_summary(connection, int(file_row["id"]))
    return {
        "prompts": prompts,
        "nextCursor": next_cursor,
        "hasMore": bool(has_indexed_more or not index_state["complete"]),
        "matchCount": match_count,
        "matchCountComplete": bool(index_state["complete"]),
        "sourceCounts": summary["sourceCounts"],
        "promptCount": summary["promptCount"],
        "purePromptCount": summary["purePromptCount"],
        "visiblePromptCount": summary["visiblePromptCount"],
        "hiddenPromptCount": summary["hiddenPromptCount"],
        "index": index_state,
    }


def prompt_index_summary(connection: sqlite3.Connection, file_id: int) -> dict[str, Any]:
    counts = connection.execute(
        """
        SELECT COUNT(*) AS prompt_count,
               COALESCE(SUM(has_pure_text), 0) AS pure_count,
               COALESCE(SUM(visible_by_default), 0) AS visible_count
        FROM prompts WHERE file_id = ?
        """,
        (file_id,),
    ).fetchone()
    source_rows = connection.execute(
        "SELECT source_type, COUNT(*) AS count FROM prompts WHERE file_id = ? GROUP BY source_type ORDER BY MIN(prompt_index)",
        (file_id,),
    ).fetchall()
    prompt_count = int(counts["prompt_count"])
    visible_count = int(counts["visible_count"])
    return {
        "promptCount": prompt_count,
        "purePromptCount": int(counts["pure_count"]),
        "visiblePromptCount": visible_count,
        "hiddenPromptCount": prompt_count - visible_count,
        "sourceCounts": {str(row["source_type"]): int(row["count"]) for row in source_rows},
    }


def iter_prompt_records(
    database_path: Path,
    rollout_path: Path,
    *,
    scope: str = "all",
    search: str = "",
    source_type: str | None = None,
    fetch_size: int = 128,
    cancel_check: Callable[[], bool] | None = None,
) -> Iterator[dict[str, Any]]:
    scope_sql, scope_parameters = _scope_clause(scope)
    clauses = ["file_id = ?", scope_sql]
    path_text = str(rollout_path.expanduser().resolve(strict=False))
    with closing(_connect(database_path)) as connection:
        file_row = connection.execute("SELECT id FROM prompt_files WHERE path = ?", (path_text,)).fetchone()
        if file_row is None:
            return
        parameters: list[Any] = [int(file_row["id"]), *scope_parameters]
        if source_type:
            clauses.append("source_type = ?")
            parameters.append(source_type)
        normalized_search = search.strip().casefold()
        if normalized_search:
            clauses.append("(instr(search_text, ?) > 0 OR instr(pure_search_text, ?) > 0)")
            parameters.extend([normalized_search, normalized_search])
        cursor = connection.execute(
            f"SELECT * FROM prompts WHERE {' AND '.join(f'({clause})' for clause in clauses)} ORDER BY prompt_index",
            parameters,
        )
        while True:
            _check_cancelled(cancel_check)
            rows = cursor.fetchmany(max(1, min(1000, int(fetch_size))))
            if not rows:
                break
            for row in rows:
                _check_cancelled(cancel_check)
                yield _prompt_from_row(row)
