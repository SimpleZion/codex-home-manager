from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from backend.search_normalization import normalize_search_text, normalized_match_original_span

prompt_index_schema_version = 15
prompt_index_boundary_bytes = 64 * 1024
prompt_index_commit_records = 256
prompt_index_commit_bytes = 8 * 1024 * 1024
prompt_index_scan_chunk_bytes = 1024 * 1024
prompt_index_candidate_overlap_bytes = 512
prompt_index_direct_candidate_bytes = 64 * 1024 * 1024
prompt_index_sanitized_record_bytes = 72 * 1024 * 1024
prompt_index_sanitized_string_bytes = 64 * 1024 * 1024
prompt_search_result_item_characters = 64 * 1024
prompt_search_result_page_characters = 2 * 1024 * 1024
prompt_index_redacted_attachment = "[附件内容已隐藏]".encode("utf-8")
prompt_index_default_max_total_bytes = 1024 * 1024 * 1024
prompt_index_default_max_idle_seconds = 30 * 24 * 60 * 60
prompt_index_cleanup_interval_seconds = 60
prompt_index_database_pattern = re.compile(r"^[0-9a-f]{64}\.sqlite$")


class PromptIndexCancelled(RuntimeError):
    pass


class PromptIndexInUse(RuntimeError):
    pass


_request_lock = threading.Lock()
_request_events: dict[str, tuple[str, str, threading.Event]] = {}
_file_locks_lock = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}
_database_locks_lock = threading.Lock()
_database_locks: dict[str, threading.RLock] = {}
_database_read_setup_locks: dict[str, threading.Lock] = {}
_database_active_counts: dict[str, int] = {}
_database_read_handles: dict[str, tuple[Any, int]] = {}
_cleanup_lock = threading.Lock()
_cleanup_last_run_ns: dict[str, int] = {}


def prompt_index_root_path() -> Path:
    configured_root = os.environ.get("CODEX_HOME_MANAGER_PROMPT_INDEX_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve(strict=False)
    local_app_data = os.environ.get("LOCALAPPDATA")
    user_data_root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return (user_data_root / "CodexHomeManager" / "prompt-indexes").resolve(strict=False)


def prompt_index_database_path(codex_home_path: Path) -> Path:
    normalized_home = os.path.normcase(str(codex_home_path.expanduser().resolve(strict=False)))
    database_name = hashlib.sha256(normalized_home.encode("utf-8")).hexdigest() + ".sqlite"
    return prompt_index_root_path() / database_name


def _database_key(database_path: Path) -> str:
    return os.path.normcase(str(database_path.expanduser().resolve(strict=False)))


def _database_lock(database_path: Path) -> threading.RLock:
    key = _database_key(database_path)
    with _database_locks_lock:
        lock = _database_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _database_locks[key] = lock
        return lock


def _database_read_setup_lock(database_path: Path) -> threading.Lock:
    key = _database_key(database_path)
    with _database_locks_lock:
        lock = _database_read_setup_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _database_read_setup_locks[key] = lock
        return lock


def _lock_database_file(lock_file, *, blocking: bool, exclusive: bool = True) -> bool:
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        if exclusive:
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        else:
            mode = msvcrt.LK_RLCK if blocking else msvcrt.LK_NBRLCK
        try:
            msvcrt.locking(lock_file.fileno(), mode, 1)
        except OSError:
            return False
        return True

    import fcntl

    flags = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(lock_file.fileno(), flags)
    except OSError:
        return False
    return True


def _unlock_database_file(lock_file) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _database_use(database_path: Path, *, blocking: bool = True) -> Iterator[None]:
    database_path = database_path.expanduser().resolve(strict=False)
    local_lock = _database_lock(database_path)
    if not local_lock.acquire(blocking=blocking):
        raise PromptIndexInUse("prompt index is currently in use")
    lock_file = None
    locked = False
    registered = False
    key = _database_key(database_path)
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = database_path.with_name(database_path.name + ".lock")
        lock_file = lock_path.open("a+b")
        locked = _lock_database_file(lock_file, blocking=blocking, exclusive=True)
        if not locked:
            raise PromptIndexInUse("prompt index is currently in use by another process")
        with _database_locks_lock:
            _database_active_counts[key] = _database_active_counts.get(key, 0) + 1
        registered = True
        yield
    finally:
        if locked and lock_file is not None:
            try:
                _unlock_database_file(lock_file)
            except OSError:
                pass
        if lock_file is not None:
            lock_file.close()
        if registered:
            with _database_locks_lock:
                active_count = _database_active_counts.get(key, 0)
                if active_count <= 1:
                    _database_active_counts.pop(key, None)
                else:
                    _database_active_counts[key] = active_count - 1
        local_lock.release()


@contextmanager
def _database_read(database_path: Path, *, blocking: bool = True) -> Iterator[None]:
    database_path = database_path.expanduser().resolve(strict=False)
    setup_lock = _database_read_setup_lock(database_path)
    lock_file = None
    registered = False
    key = _database_key(database_path)
    try:
        if not setup_lock.acquire(blocking=blocking):
            raise PromptIndexInUse("prompt index read handle is currently being prepared")
        try:
            with _database_locks_lock:
                current_handle = _database_read_handles.get(key)
            if current_handle is None:
                lock_path = database_path.with_name(database_path.name + ".lock")
                lock_file = lock_path.open("a+b")
                if not _lock_database_file(lock_file, blocking=blocking, exclusive=False):
                    lock_file.close()
                    lock_file = None
                    raise PromptIndexInUse("prompt index is currently being rebuilt by another process")
            with _database_locks_lock:
                if current_handle is None:
                    _database_read_handles[key] = (lock_file, 1)
                else:
                    _database_read_handles[key] = (current_handle[0], current_handle[1] + 1)
                _database_active_counts[key] = _database_active_counts.get(key, 0) + 1
        finally:
            setup_lock.release()
        registered = True
        yield
    finally:
        if registered:
            setup_lock.acquire()
            try:
                lock_file_to_close = None
                with _database_locks_lock:
                    current_handle = _database_read_handles.get(key)
                    if current_handle is not None:
                        if current_handle[1] <= 1:
                            lock_file_to_close = current_handle[0]
                            _database_read_handles.pop(key, None)
                        else:
                            _database_read_handles[key] = (current_handle[0], current_handle[1] - 1)
                    active_count = _database_active_counts.get(key, 0)
                    if active_count <= 1:
                        _database_active_counts.pop(key, None)
                    else:
                        _database_active_counts[key] = active_count - 1
                if lock_file_to_close is not None:
                    try:
                        _unlock_database_file(lock_file_to_close)
                    except OSError:
                        pass
                    lock_file_to_close.close()
            finally:
                setup_lock.release()


def _database_active_count(database_path: Path) -> int:
    with _database_locks_lock:
        return _database_active_counts.get(_database_key(database_path), 0)


def _database_is_in_use(database_path: Path) -> bool:
    if _database_active_count(database_path) > 0:
        return True
    try:
        with _database_use(database_path, blocking=False):
            return False
    except PromptIndexInUse:
        return True


def _configured_positive_int(name: str, default: int, minimum: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return max(minimum, int(raw_value))
    except ValueError:
        return default


def prompt_index_retention_limits() -> dict[str, int]:
    max_total_bytes = _configured_positive_int(
        "CODEX_HOME_MANAGER_PROMPT_INDEX_MAX_TOTAL_BYTES",
        prompt_index_default_max_total_bytes,
        1024 * 1024,
    )
    max_idle_seconds = _configured_positive_int(
        "CODEX_HOME_MANAGER_PROMPT_INDEX_MAX_IDLE_SECONDS",
        prompt_index_default_max_idle_seconds,
        60,
    )
    return {
        "maxTotalBytes": max_total_bytes,
        "maxIdleSeconds": max_idle_seconds,
    }


def begin_prompt_index_request(
    thread_id: str,
    request_id: str | None = None,
    scope_key: str = "",
) -> tuple[str, threading.Event]:
    normalized_request_id = str(request_id or uuid.uuid4().hex).strip()
    if not normalized_request_id or len(normalized_request_id) > 128:
        raise ValueError("invalid prompt request id")
    event = threading.Event()
    with _request_lock:
        if normalized_request_id in _request_events:
            raise ValueError("prompt request id is already active")
        _request_events[normalized_request_id] = (scope_key, thread_id, event)
    return normalized_request_id, event


def finish_prompt_index_request(request_id: str) -> None:
    with _request_lock:
        _request_events.pop(request_id, None)


def cancel_prompt_index_request(thread_id: str, request_id: str, scope_key: str = "") -> bool:
    with _request_lock:
        request = _request_events.get(request_id)
        if request is None or request[0] != scope_key or request[1] != thread_id:
            return False
        request[2].set()
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
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    previous_schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if previous_schema_version != prompt_index_schema_version:
        # All three tables contain rebuildable derived data. Recreate the file
        # progress schema atomically instead of maintaining migrations for a
        # local cache that can always be regenerated from the rollout JSONL.
        connection.executescript(
            """
            DROP TABLE IF EXISTS timeline_search_fts;
            DROP TABLE IF EXISTS prompt_search_fts;
            DROP TABLE IF EXISTS timeline_events;
            DROP TABLE IF EXISTS prompts;
            DROP TABLE IF EXISTS prompt_files;
            """
        )
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
            partial_offset INTEGER NOT NULL,
            partial_candidate INTEGER NOT NULL,
            complete INTEGER NOT NULL,
            timeline_scanned_offset INTEGER NOT NULL,
            timeline_scanned_line_count INTEGER NOT NULL,
            timeline_boundary_hash TEXT NOT NULL,
            timeline_partial_offset INTEGER NOT NULL,
            timeline_partial_candidate INTEGER NOT NULL,
            timeline_complete INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS timeline_events (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL REFERENCES prompt_files(id) ON DELETE CASCADE,
            event_index INTEGER NOT NULL,
            line_number INTEGER NOT NULL,
            byte_offset INTEGER NOT NULL,
            timestamp TEXT,
            timestamp_ms INTEGER,
            kind TEXT NOT NULL,
            label TEXT NOT NULL,
            text TEXT NOT NULL,
            search_text TEXT NOT NULL,
            character_count INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            payload_type TEXT NOT NULL,
            phase TEXT NOT NULL,
            call_id TEXT NOT NULL,
            readable INTEGER NOT NULL,
            encrypted INTEGER NOT NULL,
            has_encrypted_content INTEGER NOT NULL,
            prompt_source_type TEXT NOT NULL,
            UNIQUE(file_id, event_index)
        );
        CREATE INDEX IF NOT EXISTS timeline_events_file_order_idx ON timeline_events(file_id, event_index);
        CREATE INDEX IF NOT EXISTS timeline_events_file_kind_idx ON timeline_events(file_id, kind, event_index);
        CREATE VIRTUAL TABLE IF NOT EXISTS prompt_search_fts USING fts5(
            file_id UNINDEXED,
            prompt_index UNINDEXED,
            search_text,
            pure_search_text,
            tokenize='trigram'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS timeline_search_fts USING fts5(
            file_id UNINDEXED,
            event_index UNINDEXED,
            search_text,
            tokenize='trigram'
        );
        CREATE TABLE IF NOT EXISTS prompt_index_metadata (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            last_accessed_ns INTEGER NOT NULL
        );
        """
    )
    if previous_schema_version != prompt_index_schema_version:
        connection.execute(f"PRAGMA user_version={prompt_index_schema_version}")
    connection.execute(
        """
        INSERT INTO prompt_index_metadata(singleton, last_accessed_ns) VALUES (1, ?)
        ON CONFLICT(singleton) DO UPDATE SET last_accessed_ns = excluded.last_accessed_ns
        """,
        (time.time_ns(),),
    )
    connection.commit()
    if previous_schema_version != prompt_index_schema_version:
        # Schema changes rebuild this derived cache from the rollout. Reclaim
        # pages left by the old FTS tables immediately so a smaller index does
        # not retain the previous multi-gigabyte file allocation.
        connection.execute("VACUUM")
    return connection


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    uri = database_path.expanduser().resolve(strict=False).as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


@contextmanager
def _cancelable_query(
    connection: sqlite3.Connection,
    cancel_check: Callable[[], bool] | None,
) -> Iterator[None]:
    if cancel_check is not None:
        connection.set_progress_handler(lambda: 1 if cancel_check() else 0, 1_000)
    try:
        yield
    except sqlite3.OperationalError as error:
        if cancel_check is not None and (cancel_check() or "interrupted" in str(error).lower()):
            raise PromptIndexCancelled("prompt search was cancelled") from error
        raise
    finally:
        if cancel_check is not None:
            connection.set_progress_handler(None, 0)


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
                scanned_offset, scanned_line_count, boundary_hash, partial_offset,
                partial_candidate, complete,
                timeline_scanned_offset, timeline_scanned_line_count, timeline_boundary_hash,
                timeline_partial_offset, timeline_partial_candidate, timeline_complete, updated_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, 0, 0, 0, 0, ?, 0, 0, 0, ?)
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
                hashlib.sha256(b"").hexdigest(),
                time.time_ns(),
            ),
        )
    else:
        connection.execute("DELETE FROM prompt_search_fts WHERE file_id = ?", (int(existing_row["id"]),))
        connection.execute("DELETE FROM timeline_search_fts WHERE file_id = ?", (int(existing_row["id"]),))
        connection.execute("DELETE FROM prompts WHERE file_id = ?", (int(existing_row["id"]),))
        connection.execute("DELETE FROM timeline_events WHERE file_id = ?", (int(existing_row["id"]),))
        connection.execute(
            """
            UPDATE prompt_files
            SET device = ?, inode = ?, created_ns = ?, generation = ?, observed_size = ?,
                observed_mtime_ns = ?, scanned_offset = 0, scanned_line_count = 0,
                boundary_hash = ?, partial_offset = 0, partial_candidate = 0,
                complete = 0, timeline_scanned_offset = 0,
                timeline_scanned_line_count = 0, timeline_boundary_hash = ?,
                timeline_partial_offset = 0, timeline_partial_candidate = 0,
                timeline_complete = 0, updated_at_ns = ?
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
                hashlib.sha256(b"").hexdigest(),
                time.time_ns(),
                int(existing_row["id"]),
            ),
        )
    connection.commit()
    if existing_row is not None:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
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
        timeline_scanned_offset = int(row["timeline_scanned_offset"])
        partial_offset = int(row["partial_offset"])
        timeline_partial_offset = int(row["timeline_partial_offset"])
        if (
            stat_result.st_size < scanned_offset
            or stat_result.st_size < timeline_scanned_offset
            or stat_result.st_size < partial_offset
            or stat_result.st_size < timeline_partial_offset
        ):
            reset = True
        elif stat_result.st_size == int(row["observed_size"]) and stat_result.st_mtime_ns != int(row["observed_mtime_ns"]):
            reset = True
        elif scanned_offset or timeline_scanned_offset:
            with rollout_path.open("rb") as file:
                if scanned_offset and _boundary_hash(file, scanned_offset) != str(row["boundary_hash"]):
                    reset = True
                elif timeline_scanned_offset and _boundary_hash(file, timeline_scanned_offset) != str(
                    row["timeline_boundary_hash"]
                ):
                    reset = True
    if reset:
        row = _reset_file_index(connection, path_text, stat_result, row)
    return row, reset


def _scope_clause(scope: str, table_alias: str = "") -> tuple[str, list[Any]]:
    normalized_scope = (scope or "visible").strip().lower()
    prefix = f"{table_alias}." if table_alias else ""
    if normalized_scope in {"pure", "text", "user_text", "user-text"}:
        return f"{prefix}has_pure_text = 1", []
    if normalized_scope == "all":
        return "1 = 1", []
    if normalized_scope in {"automation", "automations", "heartbeat", "heartbeats"}:
        return f"{prefix}source_type = ?", ["automation"]
    if normalized_scope in {"delegation", "delegations", "thread_delegation", "thread-delegation", "handoff", "handoffs"}:
        return f"{prefix}source_type = ?", ["delegation"]
    if normalized_scope in {"with_agents", "with-agent", "with_agents_and_user", "agents"}:
        return f"({prefix}visible_by_default = 1 OR {prefix}source_type = 'subagent')", []
    if normalized_scope == "visible":
        return f"{prefix}visible_by_default = 1", []
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


def _prompt_from_row_with_budget(
    row: sqlite3.Row,
    normalized_query: str,
    byte_limit: int,
) -> dict[str, Any]:
    item = _prompt_from_row(row)
    text = str(item["text"])
    pure_text = str(item["pureText"])
    if pure_text == text:
        excerpt, truncated = _search_result_excerpt(text, normalized_query, byte_limit)
        item["text"] = excerpt
        item["pureText"] = excerpt
        source_truncated = int(item["characterCount"]) > len(text)
        item["textTruncated"] = source_truncated or truncated
        item["pureTextTruncated"] = int(item["pureCharacterCount"]) > len(pure_text) or truncated
        return item
    text_budget = max(1, byte_limit // 2)
    pure_budget = max(1, byte_limit - text_budget)
    item["text"], text_excerpt_truncated = _search_result_excerpt(text, normalized_query, text_budget)
    item["textTruncated"] = int(item["characterCount"]) > len(text) or text_excerpt_truncated
    item["pureText"], pure_excerpt_truncated = _search_result_excerpt(
        pure_text,
        normalized_query,
        pure_budget,
    )
    item["pureTextTruncated"] = int(item["pureCharacterCount"]) > len(pure_text) or pure_excerpt_truncated
    return item


def _check_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise PromptIndexCancelled("prompt indexing was cancelled")


def redacted_jsonl_record_bytes(
    file,
    line_start: int,
    line_end: int,
    cancel_check: Callable[[], bool] | None,
    *,
    include_truncation_metadata: bool = False,
) -> bytes | None:
    line_size = line_end - line_start
    file.seek(line_start)
    output = bytearray()
    string_buffer = bytearray()
    inside_string = False
    escaped = False
    string_truncated = False
    redacting_data_url = False
    redact_data_url_until_string_end = False
    string_character_count = 0
    string_escape_mode = ""
    unicode_escape_digits = bytearray()
    pending_high_surrogate = False
    remaining = line_size

    def append_output(value: bytes) -> bool:
        if len(output) + len(value) > prompt_index_sanitized_record_bytes:
            return False
        output.extend(value)
        return True

    def append_string_byte(byte_value: int) -> None:
        nonlocal string_truncated
        if string_truncated:
            return
        if len(string_buffer) < prompt_index_sanitized_string_bytes:
            string_buffer.append(byte_value)
        else:
            string_truncated = True

    def count_string_byte(byte_value: int) -> None:
        nonlocal string_character_count, string_escape_mode, pending_high_surrogate
        if string_escape_mode == "unicode":
            unicode_escape_digits.append(byte_value)
            if len(unicode_escape_digits) < 4:
                return
            try:
                code_unit = int(bytes(unicode_escape_digits).decode("ascii"), 16)
            except (UnicodeDecodeError, ValueError):
                code_unit = -1
            unicode_escape_digits.clear()
            string_escape_mode = ""
            if 0xD800 <= code_unit <= 0xDBFF:
                if pending_high_surrogate:
                    string_character_count += 1
                pending_high_surrogate = True
            elif 0xDC00 <= code_unit <= 0xDFFF and pending_high_surrogate:
                string_character_count += 1
                pending_high_surrogate = False
            else:
                if pending_high_surrogate:
                    string_character_count += 1
                    pending_high_surrogate = False
                string_character_count += 1
            return
        if string_escape_mode == "slash":
            if byte_value == 117:  # u
                string_escape_mode = "unicode"
                unicode_escape_digits.clear()
                return
            if pending_high_surrogate:
                string_character_count += 1
                pending_high_surrogate = False
            string_character_count += 1
            string_escape_mode = ""
            return
        if byte_value == 92:  # backslash
            string_escape_mode = "slash"
            return
        if pending_high_surrogate:
            string_character_count += 1
            pending_high_surrogate = False
        if byte_value < 0x80 or byte_value & 0xC0 != 0x80:
            string_character_count += 1

    def finish_string() -> bool:
        nonlocal pending_high_surrogate, string_character_count
        raw_string = bytes(string_buffer)
        try:
            decoded = json.loads(b'"' + raw_string + b'"')
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = raw_string.decode("utf-8", errors="replace")
        if string_truncated:
            decoded = f"{decoded}\n[超长文本已截断]"
            if pending_high_surrogate:
                string_character_count += 1
                pending_high_surrogate = False
            if include_truncation_metadata:
                decoded = f"{decoded}\u0000CHM_ORIGINAL_CHARACTERS:{string_character_count}\u0000"
        return append_output(json.dumps(decoded, ensure_ascii=False).encode("utf-8"))

    try:
        while remaining > 0:
            _check_cancelled(cancel_check)
            chunk = file.read(min(prompt_index_scan_chunk_bytes, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            for byte_value in chunk:
                if not inside_string:
                    if byte_value == 34:
                        inside_string = True
                        escaped = False
                        string_truncated = False
                        redacting_data_url = False
                        redact_data_url_until_string_end = False
                        string_character_count = 0
                        string_escape_mode = ""
                        unicode_escape_digits.clear()
                        pending_high_surrogate = False
                        string_buffer.clear()
                    elif not append_output(bytes((byte_value,))):
                        return None
                    continue

                if byte_value == 34 and not escaped:
                    if redacting_data_url:
                        for replacement_byte in prompt_index_redacted_attachment:
                            append_string_byte(replacement_byte)
                    if not finish_string():
                        return None
                    inside_string = False
                    redacting_data_url = False
                    redact_data_url_until_string_end = False
                    continue

                count_string_byte(byte_value)
                if redacting_data_url:
                    if not redact_data_url_until_string_end and byte_value in b" \t\r\n":
                        for replacement_byte in prompt_index_redacted_attachment:
                            append_string_byte(replacement_byte)
                        append_string_byte(byte_value)
                        redacting_data_url = False
                    if byte_value == 92 and not escaped:
                        escaped = True
                    else:
                        escaped = False
                    continue

                append_string_byte(byte_value)
                if byte_value == 44 and not string_truncated:
                    probe_start = max(0, len(string_buffer) - 206)
                    marker_match = re.search(
                        rb"data:([^,\s\"]{0,200}),$",
                        bytes(string_buffer[probe_start:]),
                        flags=re.IGNORECASE,
                    )
                    if marker_match is not None:
                        del string_buffer[probe_start + marker_match.start():]
                        redacting_data_url = True
                        header = marker_match.group(1).lower()
                        redact_data_url_until_string_end = b";base64" not in header
                if byte_value == 92 and not escaped:
                    escaped = True
                else:
                    escaped = False

        if inside_string:
            return None
        return bytes(output)
    finally:
        file.seek(line_end)


def update_prompt_index(
    database_path: Path,
    rollout_path: Path,
    *,
    candidate_check: Callable[[bytes], bool],
    extract_prompt: Callable[[dict[str, Any]], tuple[str, str] | None],
    classify_prompt: Callable[[str], dict[str, Any]],
    timestamp_to_ms: Callable[[Any], int | None],
    is_duplicate: Callable[[str, Any, str, int, list[tuple[str, int | None, str, int]]], bool],
    extract_timeline_event: Callable[[dict[str, Any], int], dict[str, Any] | None] | None = None,
    is_timeline_duplicate: Callable[
        [dict[str, Any], dict[str, list[tuple[int, str, str, int]]]], bool
    ] | None = None,
    index_kind: str = "prompts",
    max_scan_ms: int | None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    scan_budget_seconds = None if max_scan_ms is None else max(1, int(max_scan_ms)) / 1000
    rollout_path = rollout_path.expanduser().resolve(strict=False)
    if not rollout_path.is_file():
        raise FileNotFoundError(str(rollout_path))

    normalized_index_kind = str(index_kind or "prompts").strip().lower()
    if normalized_index_kind not in {"prompts", "timeline"}:
        raise ValueError(f"unsupported prompt index kind: {index_kind}")
    include_prompts = normalized_index_kind == "prompts"
    include_timeline = normalized_index_kind == "timeline"
    progress_offset_field = "scanned_offset" if include_prompts else "timeline_scanned_offset"
    progress_line_field = "scanned_line_count" if include_prompts else "timeline_scanned_line_count"
    progress_boundary_field = "boundary_hash" if include_prompts else "timeline_boundary_hash"
    progress_partial_offset_field = "partial_offset" if include_prompts else "timeline_partial_offset"
    progress_partial_candidate_field = "partial_candidate" if include_prompts else "timeline_partial_candidate"
    progress_complete_field = "complete" if include_prompts else "timeline_complete"

    _maybe_cleanup_prompt_indexes(database_path)
    with _database_use(database_path), _file_lock(rollout_path), closing(_connect(database_path)) as connection:
        row, reset = _prepare_file_index(connection, rollout_path)
        scan_start_offset = int(row[progress_offset_field])
        scan_start_line = int(row[progress_line_field])
        stat_before = rollout_path.stat()
        if (
            bool(row[progress_complete_field])
            and int(row[progress_offset_field]) >= stat_before.st_size
            and int(row["observed_size"]) == stat_before.st_size
            and int(row["observed_mtime_ns"]) == stat_before.st_mtime_ns
        ):
            return _index_state(
                row,
                stat_before,
                reset,
                scan_start_offset,
                started_at,
                0,
                progress_offset_field=progress_offset_field,
                progress_line_field=progress_line_field,
                progress_complete_field=progress_complete_field,
                index_kind=normalized_index_kind,
            )

        recent_rows = connection.execute(
            """
            SELECT text, timestamp_ms, protocol, line_number
            FROM prompts WHERE file_id = ? ORDER BY prompt_index DESC LIMIT 8
            """,
            (int(row["id"]),),
        ).fetchall() if include_prompts else []
        recent_prompts = [
            (str(item["text"]).strip(), item["timestamp_ms"], str(item["protocol"]), int(item["line_number"]))
            for item in reversed(recent_rows)
        ]
        prompt_index = int(
            connection.execute(
                "SELECT COALESCE(MAX(prompt_index), 0) FROM prompts WHERE file_id = ?", (int(row["id"]),)
            ).fetchone()[0]
        ) if include_prompts else 0
        event_index = int(
            connection.execute(
                "SELECT COALESCE(MAX(event_index), 0) FROM timeline_events WHERE file_id = ?", (int(row["id"]),)
            ).fetchone()[0]
        ) if include_timeline else 0
        recent_event_rows = connection.execute(
            """
            SELECT byte_offset, kind, text, source_type, timestamp_ms, event_index
            FROM timeline_events WHERE file_id = ? ORDER BY event_index DESC LIMIT 512
            """,
            (int(row["id"]),),
        ).fetchall() if include_timeline else []
        recent_timeline_events: dict[str, list[tuple[int, str, str, int, int]]] = {}
        for item in reversed(recent_event_rows):
            recent_timeline_events.setdefault(str(item["kind"]), []).append(
                (
                    int(item["byte_offset"]),
                    str(item["text"]).strip(),
                    str(item["source_type"]),
                    int(item["timestamp_ms"] or 0),
                    int(item["event_index"]),
                )
            )
        scanned_offset = scan_start_offset
        partial_offset = int(row[progress_partial_offset_field])
        partial_candidate = bool(row[progress_partial_candidate_field])
        line_number = scan_start_line
        batch_records: list[tuple[Any, ...]] = []
        batch_event_records: list[tuple[Any, ...]] = []
        batch_bytes = 0
        indexed_records = 0
        reached_eof = False

        def commit_progress(file, complete: bool) -> sqlite3.Row:
            nonlocal batch_records, batch_event_records, batch_bytes
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
                connection.executemany(
                    """
                    INSERT INTO prompt_search_fts(file_id, prompt_index, search_text, pure_search_text)
                    VALUES (?, ?, ?, ?)
                    """,
                    ((record[0], record[1], record[8], record[14]) for record in batch_records),
                )
            if batch_event_records:
                connection.executemany(
                    """
                    INSERT INTO timeline_events(
                        file_id, event_index, line_number, byte_offset, timestamp, timestamp_ms,
                        kind, label, text, search_text, character_count, source_type, payload_type,
                        phase, call_id, readable, encrypted, has_encrypted_content, prompt_source_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch_event_records,
                )
                connection.executemany(
                    """
                    INSERT INTO timeline_search_fts(file_id, event_index, search_text)
                    VALUES (?, ?, ?)
                    """,
                    ((record[0], record[1], record[9]) for record in batch_event_records),
                )
            current_stat = os.fstat(file.fileno())
            connection.execute(
                f"""
                UPDATE prompt_files
                SET observed_size = ?, observed_mtime_ns = ?, {progress_offset_field} = ?,
                    {progress_line_field} = ?, {progress_boundary_field} = ?,
                    {progress_partial_offset_field} = ?, {progress_partial_candidate_field} = ?,
                    {progress_complete_field} = ?, updated_at_ns = ?
                WHERE id = ?
                """,
                (
                    int(current_stat.st_size),
                    int(current_stat.st_mtime_ns),
                    scanned_offset,
                    line_number,
                    _boundary_hash(file, scanned_offset),
                    partial_offset,
                    int(partial_candidate),
                    int(complete),
                    time.time_ns(),
                    int(row["id"]),
                ),
            )
            connection.commit()
            batch_records = []
            batch_event_records = []
            batch_bytes = 0
            updated_row = connection.execute("SELECT * FROM prompt_files WHERE id = ?", (int(row["id"]),)).fetchone()
            if updated_row is None:
                raise RuntimeError("prompt index metadata disappeared")
            return updated_row

        scan_deadline = None if scan_budget_seconds is None else time.perf_counter() + scan_budget_seconds
        with rollout_path.open("rb") as file:
            file.seek(partial_offset if partial_offset > scan_start_offset else scan_start_offset)
            while True:
                _check_cancelled(cancel_check)
                if scan_deadline is not None and time.perf_counter() >= scan_deadline and scanned_offset > scan_start_offset:
                    break
                resuming_partial_line = partial_offset > scanned_offset
                line_start = scanned_offset if resuming_partial_line else file.tell()
                candidate_line = partial_candidate if resuming_partial_line else False
                if resuming_partial_line:
                    probe_start = max(line_start, file.tell() - prompt_index_candidate_overlap_bytes)
                    current_position = file.tell()
                    file.seek(probe_start)
                    candidate_probe_tail = file.read(current_position - probe_start)
                    file.seek(current_position)
                else:
                    candidate_probe_tail = b""
                direct_line_buffer = bytearray()
                direct_line_overflow = resuming_partial_line
                line_has_newline = False
                paused_mid_line = False
                while True:
                    _check_cancelled(cancel_check)
                    chunk_start = file.tell()
                    chunk = file.readline(prompt_index_scan_chunk_bytes)
                    if not chunk:
                        break
                    was_candidate = candidate_line
                    if not candidate_line:
                        candidate_probe = candidate_probe_tail + chunk
                        candidate_line = candidate_check(candidate_probe)
                        candidate_probe_tail = candidate_probe[-prompt_index_candidate_overlap_bytes:]
                    if candidate_line and not direct_line_overflow:
                        # Non-candidate rollout records dominate large files.
                        # Avoid copying them into a second buffer merely to
                        # discard them. A marker found after the first chunk
                        # falls back to one bounded reread of the complete line.
                        marker_found_after_unbuffered_prefix = not was_candidate and chunk_start != line_start
                        if marker_found_after_unbuffered_prefix:
                            direct_line_buffer.clear()
                            direct_line_overflow = True
                        elif len(direct_line_buffer) + len(chunk) <= prompt_index_direct_candidate_bytes:
                            direct_line_buffer.extend(chunk)
                        else:
                            direct_line_buffer.clear()
                            direct_line_overflow = True
                    if chunk.endswith(b"\n"):
                        line_has_newline = True
                        break
                    if scan_deadline is not None and time.perf_counter() >= scan_deadline:
                        partial_offset = file.tell()
                        partial_candidate = candidate_line
                        paused_mid_line = True
                        break
                line_end = file.tell()
                if paused_mid_line:
                    break
                if line_end == line_start:
                    reached_eof = True
                    break
                if not line_has_newline and not candidate_line:
                    file.seek(line_start)
                    break
                raw_line: bytes | None = None
                if candidate_line:
                    if not direct_line_overflow or line_end - line_start <= prompt_index_direct_candidate_bytes:
                        if direct_line_overflow:
                            file.seek(line_start)
                            direct_line = file.read(line_end - line_start)
                            file.seek(line_end)
                        else:
                            direct_line = bytes(direct_line_buffer)
                        raw_line = (
                            redacted_jsonl_record_bytes(file, line_start, line_end, cancel_check)
                            if b"data:" in direct_line.lower()
                            else direct_line
                        )
                    else:
                            raw_line = redacted_jsonl_record_bytes(
                                file,
                                line_start,
                                line_end,
                                cancel_check,
                                include_truncation_metadata=True,
                            )
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
                partial_offset = 0
                partial_candidate = False
                batch_bytes += line_end - line_start
                if parsed_item is not None:
                    if include_timeline and extract_timeline_event is not None:
                        timeline_event = extract_timeline_event(parsed_item, line_start)
                        if timeline_event is not None:
                            event_text = str(timeline_event.get("text") or "")
                            event_kind = str(timeline_event.get("kind") or "status")
                            event_source_type = str(timeline_event.get("sourceType") or "")
                            event_timestamp_ms = int(timeline_event.get("timestampMs") or 0)
                            event_history = recent_timeline_events.setdefault(event_kind, [])
                            superseded_event_indices: list[int] = []
                            for prior_record in event_history:
                                prior_offset, prior_text, prior_source_type, prior_timestamp_ms, prior_event_index = prior_record
                                if prior_source_type == event_source_type or not prior_text or not event_text:
                                    continue
                                near_in_time = (
                                    event_timestamp_ms > 0
                                    and prior_timestamp_ms > 0
                                    and abs(event_timestamp_ms - prior_timestamp_ms) <= 2_000
                                )
                                near_in_file = (
                                    not event_timestamp_ms
                                    and not prior_timestamp_ms
                                    and abs(prior_offset - line_start) < 1_000_000
                                )
                                if (
                                    (near_in_time or near_in_file)
                                    and len(event_text.strip()) > len(prior_text)
                                    and event_text.strip().startswith(prior_text)
                                ):
                                    superseded_event_indices.append(prior_event_index)
                            if superseded_event_indices:
                                superseded_set = set(superseded_event_indices)
                                batch_event_records[:] = [
                                    record for record in batch_event_records if int(record[1]) not in superseded_set
                                ]
                                placeholders = ",".join("?" for _ in superseded_event_indices)
                                connection.execute(
                                    f"DELETE FROM timeline_search_fts WHERE file_id = ? AND CAST(event_index AS INTEGER) IN ({placeholders})",
                                    [int(row["id"]), *superseded_event_indices],
                                )
                                connection.execute(
                                    f"DELETE FROM timeline_events WHERE file_id = ? AND event_index IN ({placeholders})",
                                    [int(row["id"]), *superseded_event_indices],
                                )
                                event_history[:] = [
                                    record for record in event_history if int(record[4]) not in superseded_set
                                ]
                            duplicate_event = bool(
                                is_timeline_duplicate
                                and is_timeline_duplicate(timeline_event, recent_timeline_events)
                            )
                            if not duplicate_event:
                                event_index += 1
                                batch_event_records.append(
                                    (
                                        int(row["id"]),
                                        event_index,
                                        line_number,
                                        line_start,
                                        timeline_event.get("timestamp") if isinstance(timeline_event.get("timestamp"), str) else None,
                                        int(timeline_event.get("timestampMs") or 0),
                                        event_kind,
                                        str(timeline_event.get("label") or event_kind),
                                        event_text,
                                        normalize_search_text(event_text),
                                        len(event_text),
                                        str(timeline_event.get("sourceType") or ""),
                                        str(timeline_event.get("payloadType") or ""),
                                        str(timeline_event.get("phase") or ""),
                                        str(timeline_event.get("callId") or ""),
                                        int(timeline_event.get("readable") is not False),
                                        int(bool(timeline_event.get("encrypted"))),
                                        int(bool(timeline_event.get("hasEncryptedContent"))),
                                        str(timeline_event.get("promptSourceType") or ""),
                                    )
                                )
                                event_history.append(
                                    (
                                        line_start,
                                        event_text.strip(),
                                        event_source_type,
                                        event_timestamp_ms,
                                        event_index,
                                    )
                                )
                                if len(event_history) > 256:
                                    del event_history[:-256]
                    extracted = extract_prompt(parsed_item) if include_prompts else None
                    if extracted is not None:
                        text, protocol = extracted
                        text = text.strip()
                        timestamp = parsed_item.get("timestamp")
                        if text and not is_duplicate(text, timestamp, protocol, line_number, recent_prompts):
                            prompt_index += 1
                            classification = classify_prompt(text)
                            text = str(classification.get("text") or text)
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
                                    normalize_search_text(text),
                                    int(classification.get("characterCount") or len(text)),
                                    str(classification.get("sourceType") or "user"),
                                    str(classification.get("sourceLabel") or "用户输入"),
                                    int(classification.get("visibleByDefault") is not False),
                                    pure_text,
                                    normalize_search_text(pure_text),
                                    int(classification.get("pureCharacterCount") or len(pure_text)),
                                    int(bool(classification.get("hasPureText"))),
                                )
                            )
                            indexed_records += 1
                            recent_prompts.append(
                                (text, timestamp_to_ms(timestamp), protocol, line_number)
                            )
                            recent_prompts = recent_prompts[-8:]
                if (
                    len(batch_records) + len(batch_event_records) >= prompt_index_commit_records
                    or batch_bytes >= prompt_index_commit_bytes
                ):
                    row = commit_progress(file, complete=False)
                    _check_cancelled(cancel_check)
                if not line_has_newline:
                    reached_eof = True
                    break
            stat_at_end = os.fstat(file.fileno())
            complete = reached_eof and partial_offset == 0 and scanned_offset >= int(stat_at_end.st_size)
            row = commit_progress(file, complete=complete)

        row, reset_after_scan = _prepare_file_index(connection, rollout_path)
        reset = reset or reset_after_scan
        if reset_after_scan:
            indexed_records = 0
        return _index_state(
            row,
            os.stat(rollout_path),
            reset,
            scan_start_offset,
            started_at,
            indexed_records,
            progress_offset_field=progress_offset_field,
            progress_line_field=progress_line_field,
            progress_complete_field=progress_complete_field,
            index_kind=normalized_index_kind,
        )


def _index_state(
    row: sqlite3.Row,
    stat_result: os.stat_result,
    reset: bool,
    scan_start_offset: int,
    started_at: float,
    indexed_records: int,
    *,
    progress_offset_field: str = "scanned_offset",
    progress_line_field: str = "scanned_line_count",
    progress_complete_field: str = "complete",
    index_kind: str = "prompts",
) -> dict[str, Any]:
    return {
        "kind": index_kind,
        "generation": str(row["generation"]),
        "fileIdentity": {
            "device": str(row["device"]),
            "inode": str(row["inode"]),
            "createdNs": int(row["created_ns"]),
        },
        "fileSize": int(stat_result.st_size),
        "fileMtimeNs": int(stat_result.st_mtime_ns),
        "scannedBytes": max(
            int(row[progress_offset_field]),
            int(row["partial_offset"] if progress_offset_field == "scanned_offset" else row["timeline_partial_offset"]),
        ),
        "scannedLines": int(row[progress_line_field]),
        "scanStartOffset": scan_start_offset,
        "scanAddedPrompts": indexed_records,
        "complete": bool(row[progress_complete_field])
        and int(row[progress_offset_field]) >= int(stat_result.st_size),
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
        {"scope": scope.strip().lower(), "search": normalize_search_text(search), "sourceType": source_type or ""},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _fts_literal(normalized_query: str, column: str) -> str:
    escaped_query = normalized_query.replace('"', '""')
    return f'{column} : "{escaped_query}"'


def _use_trigram_index(normalized_query: str) -> bool:
    return len(normalized_query) >= 3


def _prompt_search_column(scope: str) -> str:
    return "pure_search_text" if (scope or "").strip().lower() in {"pure", "text", "user_text", "user-text"} else "search_text"


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
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    safe_limit = max(1, min(500, int(limit)))
    scope_sql, scope_parameters = _scope_clause(scope, "p")
    normalized_search = normalize_search_text(search.strip())
    search_column = _prompt_search_column(scope)
    use_fts = bool(normalized_search and _use_trigram_index(normalized_search))
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
    with _database_read(database_path), closing(_connect_read_only(database_path)) as connection, _cancelable_query(connection, cancel_check):
        file_row = connection.execute("SELECT * FROM prompt_files WHERE path = ?", (path_text,)).fetchone()
        if file_row is None:
            raise RuntimeError("prompt index metadata is missing")
        clauses = ["p.file_id = ?", "p.prompt_index > ?", scope_sql]
        parameters: list[Any] = [int(file_row["id"]), after_index, *scope_parameters]
        if source_type:
            clauses.append("p.source_type = ?")
            parameters.append(source_type)
        if normalized_search:
            if use_fts:
                clauses.append("prompt_search_fts MATCH ?")
                parameters.append(_fts_literal(normalized_search, search_column))
            else:
                clauses.append(f"instr(p.{search_column}, ?) > 0")
                parameters.append(normalized_search)
        where_sql = " AND ".join(f"({clause})" for clause in clauses)
        from_sql = "prompts AS p"
        if use_fts:
            from_sql += (
                " JOIN prompt_search_fts AS f ON CAST(f.file_id AS INTEGER) = p.file_id"
                " AND CAST(f.prompt_index AS INTEGER) = p.prompt_index"
            )
        row_cursor = connection.execute(
            f"SELECT p.* FROM {from_sql} WHERE {where_sql} ORDER BY p.prompt_index LIMIT ?",
            (*parameters, safe_limit + 1),
        )
        prompts: list[dict[str, Any]] = []
        last_index = after_index
        remaining_page_bytes = prompt_search_result_page_characters
        has_indexed_more = False
        while len(prompts) < safe_limit:
            row = row_cursor.fetchone()
            if row is None:
                break
            if remaining_page_bytes <= 0:
                has_indexed_more = True
                break
            item_budget = min(prompt_search_result_item_characters, remaining_page_bytes)
            prompt = _prompt_from_row_with_budget(row, normalized_search, item_budget)
            prompts.append(prompt)
            remaining_page_bytes -= len(str(prompt["text"]).encode("utf-8"))
            if prompt["pureText"] != prompt["text"]:
                remaining_page_bytes -= len(str(prompt["pureText"]).encode("utf-8"))
            last_index = int(row["prompt_index"])
        if not has_indexed_more and len(prompts) >= safe_limit:
            has_indexed_more = row_cursor.fetchone() is not None
        next_cursor = None
        if has_indexed_more or not index_state["complete"]:
            next_cursor = _encode_cursor(
                {"generation": index_state["generation"], "query": query_signature, "after": last_index}
            )
        count_clauses = ["p.file_id = ?", scope_sql]
        count_parameters: list[Any] = [int(file_row["id"]), *scope_parameters]
        if source_type:
            count_clauses.append("p.source_type = ?")
            count_parameters.append(source_type)
        if normalized_search:
            if use_fts:
                count_clauses.append("prompt_search_fts MATCH ?")
                count_parameters.append(_fts_literal(normalized_search, search_column))
            else:
                count_clauses.append(f"instr(p.{search_column}, ?) > 0")
                count_parameters.append(normalized_search)
        match_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {from_sql} WHERE {' AND '.join(f'({clause})' for clause in count_clauses)}",
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


def _timeline_kind_clause(kind: str) -> tuple[str, list[Any]]:
    normalized_kind = (kind or "conversation").strip().lower()
    if normalized_kind == "all":
        return "1 = 1", []
    if normalized_kind == "conversation":
        return "kind IN ('user', 'commentary', 'assistant')", []
    if normalized_kind == "tool":
        return "kind IN ('tool_call', 'tool_output')", []
    allowed_kinds = {
        "user", "commentary", "assistant", "reasoning", "tool_call", "tool_output",
        "developer", "system", "context", "status",
    }
    if normalized_kind not in allowed_kinds:
        raise ValueError(f"unsupported timeline kind: {kind}")
    return "kind = ?", [normalized_kind]


def _timeline_event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return _timeline_event_from_row_with_budget(row, "", prompt_search_result_item_characters)


def _utf8_prefix(value: str, byte_limit: int) -> str:
    if byte_limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _search_result_excerpt(text: str, normalized_query: str, byte_limit: int) -> tuple[str, bool]:
    if len(text.encode("utf-8")) <= byte_limit:
        return text, False
    match_span = normalized_match_original_span(text, normalized_query) if normalized_query else None
    if match_span is None:
        return _utf8_prefix(text, byte_limit), True
    match_start, match_end = match_span
    match_text = text[match_start:match_end]
    match_bytes = len(match_text.encode("utf-8"))
    if match_bytes >= byte_limit:
        return _utf8_prefix(match_text, byte_limit), True
    side_budget = max(0, (byte_limit - match_bytes - 6) // 2)
    prefix = _utf8_prefix(text[:match_start][::-1], side_budget)[::-1]
    suffix = _utf8_prefix(text[match_end:], byte_limit - match_bytes - len(prefix.encode("utf-8")) - 6)
    return f"…{prefix}{match_text}{suffix}…", True


def _timeline_event_from_row_with_budget(
    row: sqlite3.Row,
    normalized_query: str,
    byte_limit: int,
) -> dict[str, Any]:
    original_text = str(row["text"])
    text, text_truncated = _search_result_excerpt(original_text, normalized_query, byte_limit)
    return {
        "id": f"byte-{int(row['byte_offset'])}",
        "byteOffset": int(row["byte_offset"]),
        "timestamp": row["timestamp"],
        "timestampMs": int(row["timestamp_ms"] or 0),
        "kind": str(row["kind"]),
        "label": str(row["label"]),
        "text": text,
        "characterCount": int(row["character_count"]),
        "textTruncated": text_truncated,
        "sourceType": str(row["source_type"]),
        "payloadType": str(row["payload_type"]),
        "phase": str(row["phase"]),
        "callId": str(row["call_id"]),
        "readable": bool(row["readable"]),
        "encrypted": bool(row["encrypted"]),
        "hasEncryptedContent": bool(row["has_encrypted_content"]),
        "promptSourceType": str(row["prompt_source_type"]),
    }


def read_timeline_search_page(
    database_path: Path,
    rollout_path: Path,
    *,
    kind: str,
    search: str,
    cursor: str | None,
    limit: int,
    index_state: dict[str, Any],
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    safe_limit = max(1, min(200, int(limit)))
    kind_sql, kind_parameters = _timeline_kind_clause(kind)
    normalized_search = normalize_search_text(search.strip())
    use_fts = bool(normalized_search and _use_trigram_index(normalized_search))
    query_signature = hashlib.sha256(
        json.dumps(
            {"kind": (kind or "conversation").strip().lower(), "search": normalized_search},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    after_index = 0
    if cursor:
        cursor_payload = _decode_cursor(cursor)
        if cursor_payload.get("generation") != index_state["generation"]:
            raise ValueError("timeline search cursor belongs to an outdated index generation")
        if cursor_payload.get("query") != query_signature:
            raise ValueError("timeline search cursor does not match the current query")
        after_index = max(0, int(cursor_payload.get("after") or 0))

    path_text = str(rollout_path.expanduser().resolve(strict=False))
    with _database_read(database_path), closing(_connect_read_only(database_path)) as connection, _cancelable_query(connection, cancel_check):
        file_row = connection.execute("SELECT id FROM prompt_files WHERE path = ?", (path_text,)).fetchone()
        if file_row is None:
            raise RuntimeError("timeline index metadata is unavailable")
        qualified_kind_sql = kind_sql.replace("kind", "e.kind")
        clauses = ["e.file_id = ?", "e.event_index > ?", qualified_kind_sql]
        parameters: list[Any] = [int(file_row["id"]), after_index, *kind_parameters]
        if normalized_search:
            if use_fts:
                clauses.append("timeline_search_fts MATCH ?")
                parameters.append(_fts_literal(normalized_search, "search_text"))
            else:
                clauses.append("instr(e.search_text, ?) > 0")
                parameters.append(normalized_search)
        where_sql = " AND ".join(f"({clause})" for clause in clauses)
        from_sql = "timeline_events AS e"
        if use_fts:
            from_sql += (
                " JOIN timeline_search_fts AS f ON CAST(f.file_id AS INTEGER) = e.file_id"
                " AND CAST(f.event_index AS INTEGER) = e.event_index"
            )
        row_cursor = connection.execute(
            f"SELECT e.* FROM {from_sql} WHERE {where_sql} ORDER BY e.event_index LIMIT ?",
            [*parameters, safe_limit + 1],
        )
        events: list[dict[str, Any]] = []
        last_index = after_index
        remaining_page_bytes = prompt_search_result_page_characters
        has_indexed_more = False
        while len(events) < safe_limit:
            row = row_cursor.fetchone()
            if row is None:
                break
            if remaining_page_bytes <= 0:
                has_indexed_more = True
                break
            item_budget = min(prompt_search_result_item_characters, remaining_page_bytes)
            event = _timeline_event_from_row_with_budget(row, normalized_search, item_budget)
            events.append(event)
            remaining_page_bytes -= len(str(event["text"]).encode("utf-8"))
            last_index = int(row["event_index"])
        if not has_indexed_more and len(events) >= safe_limit:
            has_indexed_more = row_cursor.fetchone() is not None
        next_cursor = None
        if has_indexed_more or not index_state["complete"]:
            next_cursor = _encode_cursor(
                {"generation": index_state["generation"], "query": query_signature, "after": last_index}
            )
        count_parameters: list[Any] = [int(file_row["id"]), *kind_parameters]
        count_clauses = ["e.file_id = ?", qualified_kind_sql]
        if normalized_search:
            if use_fts:
                count_clauses.append("timeline_search_fts MATCH ?")
                count_parameters.append(_fts_literal(normalized_search, "search_text"))
            else:
                count_clauses.append("instr(e.search_text, ?) > 0")
                count_parameters.append(normalized_search)
        match_count = int(connection.execute(
            f"SELECT COUNT(*) FROM {from_sql} WHERE {' AND '.join(f'({clause})' for clause in count_clauses)}",
            count_parameters,
        ).fetchone()[0])
    return {
        "kind": (kind or "conversation").strip().lower(),
        "search": search,
        "matchCount": match_count,
        "matchCountComplete": bool(index_state["complete"]),
        "matches": events,
        "nextCursor": next_cursor,
        "hasMore": bool(has_indexed_more or not index_state["complete"]),
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
    scope_sql, scope_parameters = _scope_clause(scope, "p")
    clauses = ["p.file_id = ?", scope_sql]
    path_text = str(rollout_path.expanduser().resolve(strict=False))
    with _database_read(database_path), closing(_connect_read_only(database_path)) as connection, _cancelable_query(connection, cancel_check):
        file_row = connection.execute("SELECT id FROM prompt_files WHERE path = ?", (path_text,)).fetchone()
        if file_row is None:
            return
        parameters: list[Any] = [int(file_row["id"]), *scope_parameters]
        if source_type:
            clauses.append("p.source_type = ?")
            parameters.append(source_type)
        normalized_search = normalize_search_text(search.strip())
        search_column = _prompt_search_column(scope)
        use_fts = bool(normalized_search and _use_trigram_index(normalized_search))
        if normalized_search:
            if use_fts:
                clauses.append("prompt_search_fts MATCH ?")
                parameters.append(_fts_literal(normalized_search, search_column))
            else:
                clauses.append(f"instr(p.{search_column}, ?) > 0")
                parameters.append(normalized_search)
        from_sql = "prompts AS p"
        if use_fts:
            from_sql += (
                " JOIN prompt_search_fts AS f ON CAST(f.file_id AS INTEGER) = p.file_id"
                " AND CAST(f.prompt_index AS INTEGER) = p.prompt_index"
            )
        cursor = connection.execute(
            f"SELECT p.* FROM {from_sql} WHERE {' AND '.join(f'({clause})' for clause in clauses)} ORDER BY p.prompt_index",
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


def _database_files(database_path: Path) -> tuple[Path, Path, Path]:
    return (
        database_path,
        database_path.with_name(database_path.name + "-wal"),
        database_path.with_name(database_path.name + "-shm"),
    )


def _database_size_bytes(database_path: Path) -> int:
    total_size = 0
    for path in _database_files(database_path):
        try:
            total_size += path.stat().st_size
        except FileNotFoundError:
            continue
    return total_size


def _prompt_index_databases(root_path: Path) -> list[Path]:
    try:
        candidates = root_path.iterdir()
    except FileNotFoundError:
        return []
    return sorted(
        (
            path
            for path in candidates
            if path.is_file() and prompt_index_database_pattern.fullmatch(path.name)
        ),
        key=lambda path: path.name,
    )


def _inspect_prompt_index_database(database_path: Path) -> dict[str, Any]:
    stat_result = database_path.stat()
    result: dict[str, Any] = {
        "sizeBytes": _database_size_bytes(database_path),
        "lastAccessedNs": int(stat_result.st_mtime_ns),
        "schemaVersion": 0,
        "sourceRolloutCount": 0,
        "missingSourceRolloutCount": 0,
        "promptCount": 0,
        "timelineEventCount": 0,
        "missingFileIds": [],
    }
    database_uri = database_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True, timeout=1.0)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1000")
        result["schemaVersion"] = int(connection.execute("PRAGMA user_version").fetchone()[0])
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('prompt_files', 'prompts', 'timeline_events', 'prompt_index_metadata')"
            )
        }
        if "prompt_index_metadata" in table_names:
            metadata_row = connection.execute(
                "SELECT last_accessed_ns FROM prompt_index_metadata WHERE singleton = 1"
            ).fetchone()
            if metadata_row is not None:
                result["lastAccessedNs"] = int(metadata_row[0])
        if "prompt_files" not in table_names:
            return result
        source_rows = connection.execute("SELECT id, path FROM prompt_files").fetchall()
        missing_file_ids: list[int] = []
        existing_count = 0
        for row in source_rows:
            if Path(str(row["path"])).is_file():
                existing_count += 1
            else:
                missing_file_ids.append(int(row["id"]))
        result["sourceRolloutCount"] = len(source_rows)
        result["existingSourceRolloutCount"] = existing_count
        result["missingSourceRolloutCount"] = len(missing_file_ids)
        result["missingFileIds"] = missing_file_ids
        if "prompts" in table_names:
            result["promptCount"] = int(connection.execute("SELECT COUNT(*) FROM prompts").fetchone()[0])
        if "timeline_events" in table_names:
            result["timelineEventCount"] = int(connection.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0])
    return result


def _purge_missing_rollouts(database_path: Path, missing_file_ids: list[int]) -> None:
    if not missing_file_ids:
        return
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA secure_delete=ON")
        connection.executemany("DELETE FROM prompt_files WHERE id = ?", ((file_id,) for file_id in missing_file_ids))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _delete_database_files(database_path: Path) -> tuple[int, int]:
    reclaimed_bytes = _database_size_bytes(database_path)
    deleted_files = 0
    remaining_errors: list[OSError] = []
    for attempt in range(4):
        remaining_errors = []
        for path in reversed(_database_files(database_path)):
            try:
                path.unlink()
                deleted_files += 1
            except FileNotFoundError:
                continue
            except OSError as error:
                remaining_errors.append(error)
        if not remaining_errors:
            return deleted_files, reclaimed_bytes
        time.sleep(0.05 * (attempt + 1))
    raise PromptIndexInUse("prompt index files are locked and could not be cleared") from remaining_errors[-1]


def clear_prompt_index(codex_home_path: Path) -> dict[str, Any]:
    database_path = prompt_index_database_path(codex_home_path)
    existed = any(path.exists() for path in _database_files(database_path))
    if not existed:
        return {
            "cleared": False,
            "databaseExisted": False,
            "deletedFileCount": 0,
            "reclaimedBytes": 0,
        }
    with _database_use(database_path, blocking=False):
        deleted_file_count, reclaimed_bytes = _delete_database_files(database_path)
    return {
        "cleared": True,
        "databaseExisted": True,
        "deletedFileCount": deleted_file_count,
        "reclaimedBytes": reclaimed_bytes,
    }


def cleanup_prompt_indexes(
    *,
    root_path: Path | None = None,
    max_total_bytes: int | None = None,
    max_idle_seconds: int | None = None,
    now_ns: int | None = None,
    protected_database_paths: set[Path] | None = None,
) -> dict[str, Any]:
    root = (root_path or prompt_index_root_path()).expanduser().resolve(strict=False)
    limits = prompt_index_retention_limits()
    total_limit = max(
        1024 * 1024,
        int(limits["maxTotalBytes"] if max_total_bytes is None else max_total_bytes),
    )
    idle_limit = max(
        60,
        int(limits["maxIdleSeconds"] if max_idle_seconds is None else max_idle_seconds),
    )
    current_time_ns = int(now_ns or time.time_ns())
    protected_keys = {
        _database_key(path)
        for path in (protected_database_paths or set())
    }
    summary = {
        "examinedDatabases": 0,
        "deletedDatabases": 0,
        "deletedCorruptDatabases": 0,
        "purgedMissingRollouts": 0,
        "skippedInUse": 0,
        "reclaimedBytes": 0,
        "maxTotalBytes": total_limit,
        "maxIdleSeconds": idle_limit,
    }
    candidates: list[dict[str, Any]] = []

    for database_path in _prompt_index_databases(root):
        summary["examinedDatabases"] += 1
        is_protected = _database_key(database_path) in protected_keys
        try:
            with _database_use(database_path, blocking=False):
                metadata = _inspect_prompt_index_database(database_path)
                missing_file_ids = list(metadata.pop("missingFileIds", []))
                if missing_file_ids:
                    _purge_missing_rollouts(database_path, missing_file_ids)
                    summary["purgedMissingRollouts"] += len(missing_file_ids)
                    metadata = _inspect_prompt_index_database(database_path)
                    metadata.pop("missingFileIds", None)
                if is_protected:
                    continue
                has_sources = int(metadata.get("sourceRolloutCount", 0)) > 0
                is_idle = current_time_ns - int(metadata["lastAccessedNs"]) > idle_limit * 1_000_000_000
                if not has_sources or is_idle:
                    _, reclaimed_bytes = _delete_database_files(database_path)
                    summary["deletedDatabases"] += 1
                    summary["reclaimedBytes"] += reclaimed_bytes
                    continue
                candidates.append({"path": database_path, **metadata})
        except (PromptIndexInUse, PermissionError, sqlite3.OperationalError):
            summary["skippedInUse"] += 1
        except sqlite3.DatabaseError:
            try:
                with _database_use(database_path, blocking=False):
                    _, reclaimed_bytes = _delete_database_files(database_path)
                summary["deletedDatabases"] += 1
                summary["deletedCorruptDatabases"] += 1
                summary["reclaimedBytes"] += reclaimed_bytes
            except (PromptIndexInUse, PermissionError, sqlite3.Error):
                summary["skippedInUse"] += 1

    total_size = sum(_database_size_bytes(path) for path in _prompt_index_databases(root))
    if total_size > total_limit:
        for candidate in sorted(candidates, key=lambda item: int(item["lastAccessedNs"])):
            if total_size <= total_limit:
                break
            database_path = candidate["path"]
            try:
                with _database_use(database_path, blocking=False):
                    _, reclaimed_bytes = _delete_database_files(database_path)
                total_size = max(0, total_size - reclaimed_bytes)
                summary["deletedDatabases"] += 1
                summary["reclaimedBytes"] += reclaimed_bytes
            except (PromptIndexInUse, PermissionError, sqlite3.OperationalError, sqlite3.DatabaseError):
                summary["skippedInUse"] += 1
    summary["remainingDatabases"] = len(_prompt_index_databases(root))
    summary["remainingBytes"] = sum(_database_size_bytes(path) for path in _prompt_index_databases(root))
    summary["overCapacity"] = summary["remainingBytes"] > total_limit
    return summary


def _maybe_cleanup_prompt_indexes(protected_database_path: Path) -> None:
    root = prompt_index_root_path()
    root_key = os.path.normcase(str(root.resolve(strict=False)))
    now_ns = time.time_ns()
    with _cleanup_lock:
        last_run_ns = _cleanup_last_run_ns.get(root_key, 0)
        if now_ns - last_run_ns < prompt_index_cleanup_interval_seconds * 1_000_000_000:
            return
        _cleanup_last_run_ns[root_key] = now_ns
    try:
        cleanup_prompt_indexes(
            root_path=root,
            now_ns=now_ns,
            protected_database_paths={protected_database_path},
        )
    except OSError:
        return


def prompt_index_status(codex_home_path: Path) -> dict[str, Any]:
    root = prompt_index_root_path()
    database_path = prompt_index_database_path(codex_home_path)
    database_paths = _prompt_index_databases(root)
    limits = prompt_index_retention_limits()
    total_size_bytes = sum(_database_size_bytes(path) for path in database_paths)
    active_database_count = sum(1 for path in database_paths if _database_is_in_use(path))
    database_status: dict[str, Any] | None = None
    if database_path.is_file():
        in_use = _database_is_in_use(database_path)
        readable: bool | None = None
        inspection_state = "in_use" if in_use else "available"
        metadata: dict[str, Any] = {}
        if not in_use:
            try:
                with _database_use(database_path, blocking=False):
                    metadata = _inspect_prompt_index_database(database_path)
                    metadata.pop("missingFileIds", None)
                readable = True
            except (PromptIndexInUse, PermissionError, sqlite3.OperationalError):
                in_use = True
                inspection_state = "in_use"
            except sqlite3.DatabaseError:
                readable = False
                inspection_state = "unreadable"
        database_status = {
            "sizeBytes": _database_size_bytes(database_path),
            "inUse": in_use,
            "activeOperations": _database_active_count(database_path),
            "readable": readable,
            "inspectionState": inspection_state,
            "lastAccessedAtMs": (
                int(metadata["lastAccessedNs"]) // 1_000_000
                if metadata.get("lastAccessedNs") is not None
                else None
            ),
            "schemaVersion": metadata.get("schemaVersion"),
            "sourceRolloutCount": metadata.get("sourceRolloutCount"),
            "missingSourceRolloutCount": metadata.get("missingSourceRolloutCount"),
            "promptCount": metadata.get("promptCount"),
            "timelineEventCount": metadata.get("timelineEventCount"),
        }
    return {
        "databaseExists": database_status is not None,
        "database": database_status,
        "storage": {
            "rootPath": str(root),
            "databaseCount": len(database_paths),
            "activeDatabaseCount": active_database_count,
            "totalSizeBytes": total_size_bytes,
            "maxTotalBytes": limits["maxTotalBytes"],
            "maxIdleSeconds": limits["maxIdleSeconds"],
            "overCapacity": total_size_bytes > limits["maxTotalBytes"],
        },
    }
