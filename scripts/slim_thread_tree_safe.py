from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


image_marker = "[image omitted from session history for performance; preserved in main-thread backup]"
drop_value = object()
gibibyte = 1024**3


class MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_physical", ctypes.c_ulonglong),
        ("available_physical", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("available_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("available_virtual", ctypes.c_ulonglong),
        ("available_extended_virtual", ctypes.c_ulonglong),
    ]


class FileOperation(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_void_p),
        ("function", ctypes.c_uint),
        ("from_path", ctypes.c_wchar_p),
        ("to_path", ctypes.c_wchar_p),
        ("flags", ctypes.c_ushort),
        ("aborted", ctypes.c_int),
        ("name_mappings", ctypes.c_void_p),
        ("progress_title", ctypes.c_wchar_p),
    ]


def available_memory_bytes() -> int:
    status = MemoryStatusEx()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    return int(status.available_physical)


def require_memory(minimum_bytes: int) -> None:
    available_bytes = available_memory_bytes()
    if available_bytes < minimum_bytes:
        raise RuntimeError(
            f"available physical memory is {available_bytes / gibibyte:.2f} GiB; "
            f"at least {minimum_bytes / gibibyte:.2f} GiB is required"
        )


def recycle_file(path: Path) -> None:
    if not path.exists():
        return
    path_text = str(path.resolve())
    if path_text.startswith("\\\\?\\UNC\\"):
        path_text = "\\\\" + path_text[8:]
    elif path_text.startswith("\\\\?\\"):
        path_text = path_text[4:]
    operation = FileOperation()
    operation.function = 3
    operation.from_path = path_text + "\0\0"
    operation.flags = 0x0004 | 0x0010 | 0x0040 | 0x0400
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.aborted:
        raise RuntimeError(f"failed to move temporary file to Recycle Bin: {path} (code={result})")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def update_digest(digest: Any, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def content_text_parts(content: Any) -> tuple[str, ...]:
    if isinstance(content, str):
        return (content,)
    if not isinstance(content, list):
        return ()
    return tuple(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"input_text", "output_text", "text"}
        and isinstance(block.get("text"), str)
    )


def user_prompt_parts(item: Any) -> tuple[str, ...] | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    payload = item.get("payload")
    if item_type == "user_message":
        if isinstance(payload, str):
            return (payload,)
        if not isinstance(payload, dict):
            return ()
        parts = list(content_text_parts(payload.get("content")))
        if isinstance(payload.get("text"), str):
            parts.append(payload["text"])
        return tuple(parts)
    if item_type == "response_item" and isinstance(payload, dict) and payload.get("role") == "user":
        return content_text_parts(payload.get("content"))
    if item_type == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
        message = payload.get("message")
        return (message,) if isinstance(message, str) else ()
    return None


def visible_message_parts(item: Any) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    payload = item.get("payload")
    if item_type == "response_item" and isinstance(payload, dict):
        if payload.get("type") == "message" and payload.get("role") in {
            "user",
            "assistant",
            "developer",
            "system",
        }:
            return str(payload.get("role")), content_text_parts(payload.get("content"))
    if item_type == "user_message":
        parts = user_prompt_parts(item)
        return "user", parts or ()
    if item_type == "event_msg" and isinstance(payload, dict):
        event_type = str(payload.get("type") or "")
        if event_type in {"user_message", "agent_message", "assistant_message"}:
            message = payload.get("message")
            return event_type, (message,) if isinstance(message, str) else ()
    return None


def iter_encrypted_values(value: Any) -> Iterable[Any]:
    if isinstance(value, list):
        for item in value:
            yield from iter_encrypted_values(item)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key == "encrypted_content":
            yield item
        else:
            yield from iter_encrypted_values(item)


def is_data_image(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith("data:image/")


def is_invalid_input_image(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("type") != "input_image":
        return False
    image_url = value.get("image_url")
    if not isinstance(image_url, str):
        return True
    normalized = image_url.strip().lower()
    return not normalized.startswith(("http://", "https://", "data:image/", "file://"))


def count_slimmable_images(value: Any, parent_key: str | None = None) -> tuple[int, int]:
    if isinstance(value, list):
        embedded_count = 0
        invalid_count = 0
        for item in value:
            item_embedded, item_invalid = count_slimmable_images(item)
            embedded_count += item_embedded
            invalid_count += item_invalid
        return embedded_count, invalid_count
    if isinstance(value, dict):
        if is_invalid_input_image(value):
            return 0, 1
        embedded_count = 0
        invalid_count = 0
        for key, item in value.items():
            if key == "encrypted_content":
                continue
            item_embedded, item_invalid = count_slimmable_images(item, parent_key=key)
            embedded_count += item_embedded
            invalid_count += item_invalid
        return embedded_count, invalid_count
    if parent_key != "encrypted_content" and is_data_image(value):
        return 1, 0
    return 0, 0


def is_compacted_user_message(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "message"
        and value.get("role") == "user"
    )


def replacement_history(value: Any) -> list[Any]:
    if not isinstance(value, dict) or value.get("type") != "compacted":
        return []
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return []
    history = payload.get("replacement_history")
    return history if isinstance(history, list) else []


def prune_compacted_history(
    value: dict[str, Any],
    recent_user_messages: int,
    stats: dict[str, int],
) -> dict[str, Any]:
    history = replacement_history(value)
    if not history or recent_user_messages <= 0:
        return value
    user_indices = [
        index
        for index, history_item in enumerate(history)
        if is_compacted_user_message(history_item)
    ]
    retained_user_indices = set(user_indices[-recent_user_messages:])
    if len(retained_user_indices) == len(user_indices):
        return value
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return value
    payload["replacement_history"] = [
        history_item
        for index, history_item in enumerate(history)
        if not is_compacted_user_message(history_item) or index in retained_user_indices
    ]
    stats["removed_compacted_user_messages"] += len(user_indices) - len(retained_user_indices)
    stats["retained_compacted_user_messages"] += len(retained_user_indices)
    stats["compacted_history_items_before"] += len(history)
    stats["compacted_history_items_after"] += len(payload["replacement_history"])
    return value


def slim_value(value: Any, stats: dict[str, int], parent_key: str | None = None) -> Any:
    if isinstance(value, list):
        output: list[Any] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "input_image":
                image_url = item.get("image_url")
                if is_data_image(image_url) or is_invalid_input_image(item):
                    stats["removed_image_objects"] += 1
                    if is_data_image(image_url):
                        stats["removed_image_bytes"] += len(image_url.encode("utf-8", errors="replace"))
                    else:
                        stats["removed_invalid_image_objects"] += 1
                    continue
            slimmed_item = slim_value(item, stats)
            if slimmed_item is not drop_value:
                output.append(slimmed_item)
        return output
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            image_url = value.get("image_url")
            if is_data_image(image_url) or is_invalid_input_image(value):
                stats["removed_image_objects"] += 1
                if is_data_image(image_url):
                    stats["removed_image_bytes"] += len(image_url.encode("utf-8", errors="replace"))
                else:
                    stats["removed_invalid_image_objects"] += 1
                return drop_value
        output: dict[str, Any] = {}
        for key, item in value.items():
            if key == "encrypted_content":
                output[key] = item
                stats["preserved_encrypted_fields"] += 1
                continue
            slimmed_item = slim_value(item, stats, parent_key=key)
            if slimmed_item is not drop_value:
                output[key] = slimmed_item
        return output
    if parent_key != "encrypted_content" and is_data_image(value):
        stats["replaced_image_strings"] += 1
        stats["removed_image_bytes"] += len(value.encode("utf-8", errors="replace"))
        return image_marker
    return value


def event_contract_value(value: Any) -> Any:
    if isinstance(value, list):
        output = []
        for item in value:
            normalized = event_contract_value(item)
            if normalized is not drop_value:
                output.append(normalized)
        return output
    if isinstance(value, dict):
        if value.get("type") == "input_image" and (
            is_data_image(value.get("image_url")) or is_invalid_input_image(value)
        ):
            return drop_value
        output = {}
        for key, item in value.items():
            if key == "encrypted_content":
                output[key] = item
                continue
            normalized = event_contract_value(item)
            if normalized is not drop_value:
                output[key] = normalized
        return output
    if is_data_image(value):
        return image_marker
    return value


def scan_file(path: Path) -> dict[str, Any]:
    require_memory(3 * gibibyte)
    source_digest = hashlib.sha256()
    line_count = 0
    parse_errors = 0
    compacted_count = 0
    latest_compacted_line = 0
    latest_compacted_user_messages = 0
    embedded_images = 0
    invalid_input_images = 0
    session_meta_id = ""
    with path.open("rb") as source:
        for line_count, raw_line in enumerate(source, 1):
            source_digest.update(raw_line)
            try:
                item = json.loads(raw_line)
            except Exception:
                parse_errors += 1
                continue
            if item.get("type") == "session_meta" and not session_meta_id:
                payload = item.get("payload")
                if isinstance(payload, dict):
                    session_meta_id = str(payload.get("id") or "")
            if item.get("type") == "compacted":
                compacted_count += 1
                latest_compacted_line = line_count
                latest_compacted_user_messages = sum(
                    1
                    for history_item in replacement_history(item)
                    if is_compacted_user_message(history_item)
                )
            item_embedded, item_invalid = count_slimmable_images(item)
            embedded_images += item_embedded
            invalid_input_images += item_invalid
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": source_digest.hexdigest(),
        "line_count": line_count,
        "parse_errors": parse_errors,
        "compacted_count": compacted_count,
        "latest_compacted_line": latest_compacted_line,
        "latest_compacted_user_messages": latest_compacted_user_messages,
        "embedded_images": embedded_images,
        "invalid_input_images": invalid_input_images,
        "session_meta_id": session_meta_id,
    }


def contract_scan(path: Path, latest_compacted_line: int) -> dict[str, Any]:
    prompt_digest = hashlib.sha256()
    visible_digest = hashlib.sha256()
    encrypted_digest = hashlib.sha256()
    event_digest = hashlib.sha256()
    prompt_count = 0
    visible_count = 0
    encrypted_count = 0
    event_count = 0
    compacted_count = 0
    embedded_images = 0
    invalid_input_images = 0
    parse_errors = 0
    with path.open("rb") as source:
        for line_number, raw_line in enumerate(source, 1):
            try:
                item = json.loads(raw_line)
            except Exception:
                parse_errors += 1
                continue
            is_compacted = item.get("type") == "compacted"
            if is_compacted:
                compacted_count += 1
                if line_number != latest_compacted_line:
                    continue
            prompt_parts = user_prompt_parts(item)
            if prompt_parts is not None:
                prompt_count += 1
                update_digest(prompt_digest, prompt_parts)
            visible_parts = visible_message_parts(item)
            if visible_parts is not None:
                visible_count += 1
                update_digest(visible_digest, visible_parts)
            for encrypted_value in iter_encrypted_values(item):
                encrypted_count += 1
                update_digest(encrypted_digest, encrypted_value)
            if item.get("type") == "event_msg":
                event_count += 1
                update_digest(event_digest, event_contract_value(item))
            item_embedded, item_invalid = count_slimmable_images(item)
            embedded_images += item_embedded
            invalid_input_images += item_invalid
    return {
        "parse_errors": parse_errors,
        "compacted_count": compacted_count,
        "prompt_count": prompt_count,
        "prompt_sha256": prompt_digest.hexdigest(),
        "visible_count": visible_count,
        "visible_sha256": visible_digest.hexdigest(),
        "encrypted_count": encrypted_count,
        "encrypted_sha256": encrypted_digest.hexdigest(),
        "event_count": event_count,
        "event_sha256": event_digest.hexdigest(),
        "embedded_images": embedded_images,
        "invalid_input_images": invalid_input_images,
    }


def contracts_match(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = (
        "prompt_count",
        "prompt_sha256",
        "visible_count",
        "visible_sha256",
        "encrypted_count",
        "encrypted_sha256",
        "event_count",
        "event_sha256",
    )
    return all(before[key] == after[key] for key in keys)


def compacted_history_contract(
    path: Path,
    latest_compacted_line: int,
    recent_user_messages: int,
) -> dict[str, Any]:
    user_items: list[Any] = []
    non_user_digest = hashlib.sha256()
    non_user_count = 0
    with path.open("rb") as source:
        for line_number, raw_line in enumerate(source, 1):
            if line_number != latest_compacted_line:
                continue
            item = json.loads(raw_line)
            for history_item in replacement_history(item):
                if is_compacted_user_message(history_item):
                    user_items.append(history_item)
                else:
                    non_user_count += 1
                    update_digest(non_user_digest, history_item)
            break
    retained_user_items = user_items[-recent_user_messages:] if recent_user_messages > 0 else user_items
    retained_user_digest = hashlib.sha256()
    for history_item in retained_user_items:
        update_digest(retained_user_digest, event_contract_value(history_item))
    return {
        "user_count": len(user_items),
        "retained_user_count": len(retained_user_items),
        "retained_user_sha256": retained_user_digest.hexdigest(),
        "non_user_count": non_user_count,
        "non_user_sha256": non_user_digest.hexdigest(),
    }


def compacted_history_contracts_match(
    before: dict[str, Any],
    after: dict[str, Any],
    recent_user_messages: int,
) -> bool:
    expected_user_count = min(before["user_count"], recent_user_messages)
    return (
        after["user_count"] == expected_user_count
        and after["retained_user_count"] == expected_user_count
        and before["retained_user_count"] == expected_user_count
        and after["retained_user_sha256"] == before["retained_user_sha256"]
        and after["non_user_count"] == before["non_user_count"]
        and after["non_user_sha256"] == before["non_user_sha256"]
    )


def build_temp(
    path: Path,
    scan: dict[str, Any],
    temp_path: Path,
    recent_compacted_user_messages: int = 0,
    preserve_ordinal_gaps: bool = False,
    only_compacted_records: bool = False,
    latest_checkpoint_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stats = {
        "removed_compacted_lines": 0,
        "ordinal_gap_placeholders": 0,
        "removed_compacted_user_messages": 0,
        "retained_compacted_user_messages": 0,
        "compacted_history_items_before": 0,
        "compacted_history_items_after": 0,
        "rewritten_image_lines": 0,
        "removed_image_objects": 0,
        "removed_invalid_image_objects": 0,
        "replaced_image_strings": 0,
        "removed_image_bytes": 0,
        "preserved_encrypted_fields": 0,
    }
    source_digest = hashlib.sha256()
    with path.open("rb") as source, temp_path.open("wb") as output:
        for line_number, raw_line in enumerate(source, 1):
            source_digest.update(raw_line)
            item = json.loads(raw_line)
            if item.get("type") == "compacted" and line_number != scan["latest_compacted_line"]:
                stats["removed_compacted_lines"] += 1
                if preserve_ordinal_gaps:
                    placeholder = {
                        "timestamp": item.get("timestamp"),
                        "type": "_slimmed_record_gap",
                        "payload": {},
                    }
                    output.write(
                        (json.dumps(placeholder, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                    )
                    stats["ordinal_gap_placeholders"] += 1
                continue
            slimmed_item = item
            compacted_history_changed = False
            if item.get("type") == "compacted" and line_number == scan["latest_compacted_line"] and latest_checkpoint_override is not None:
                slimmed_item = latest_checkpoint_override
                compacted_history_changed = True
            if (
                item.get("type") == "compacted"
                and line_number == scan["latest_compacted_line"]
                and recent_compacted_user_messages > 0
            ):
                user_messages_before = stats["removed_compacted_user_messages"]
                slimmed_item = prune_compacted_history(
                    item,
                    recent_compacted_user_messages,
                    stats,
                )
                compacted_history_changed = (
                    stats["removed_compacted_user_messages"] > user_messages_before
                )
            if not only_compacted_records and (b"data:image/" in raw_line or b"input_image" in raw_line):
                before_stats = dict(stats)
                slimmed_item = slim_value(item, stats)
                if stats != before_stats:
                    stats["rewritten_image_lines"] += 1
            if slimmed_item is drop_value:
                continue
            if slimmed_item is item and not compacted_history_changed:
                output.write(raw_line)
            else:
                output.write(
                    (json.dumps(slimmed_item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                )
        output.flush()
        os.fsync(output.fileno())
    if source_digest.hexdigest() != scan["sha256"]:
        raise RuntimeError("source content changed while building slim output")
    return stats


def replace_validated_file(temp_path: Path, destination: Path) -> str:
    try:
        os.replace(temp_path, destination)
        return "os_replace"
    except PermissionError:
        with temp_path.open("rb") as source, destination.open("r+b") as target:
            target.seek(0)
            shutil.copyfileobj(source, target, length=4 * 1024 * 1024)
            target.truncate()
            target.flush()
            os.fsync(target.fileno())
        recycle_file(temp_path)
        return "in_place_overwrite"


def fetch_tree(database_path: Path, root_id: str) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        quick_check = connection.execute("pragma quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {quick_check}")
        edges = [dict(row) for row in connection.execute(
            "select parent_thread_id, child_thread_id, status from thread_spawn_edges"
        )]
        children: dict[str, list[str]] = defaultdict(list)
        edge_status: dict[str, str] = {}
        for edge in edges:
            children[str(edge["parent_thread_id"])].append(str(edge["child_thread_id"]))
            edge_status[str(edge["child_thread_id"])] = str(edge["status"] or "")
        tree_ids: list[str] = []
        seen: set[str] = set()
        queue = deque([root_id])
        while queue:
            thread_id = queue.popleft()
            if thread_id in seen:
                continue
            seen.add(thread_id)
            tree_ids.append(thread_id)
            queue.extend(children.get(thread_id, []))
        rows_by_id: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(tree_ids), 500):
            chunk = tree_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in connection.execute(
                f"select id, rollout_path, cwd, title, archived, updated_at from threads where id in ({placeholders})",
                chunk,
            ):
                rows_by_id[str(row["id"])] = dict(row)
        dangling_ids = [thread_id for thread_id in tree_ids if thread_id not in rows_by_id]
        rows: list[dict[str, Any]] = []
        for thread_id in tree_ids:
            row = rows_by_id.get(thread_id)
            if row is None:
                continue
            row["tree_status"] = "root" if thread_id == root_id else edge_status.get(thread_id, "unknown")
            rows.append(row)
        status_counts: dict[str, int] = defaultdict(int)
        for row in rows:
            status_counts[str(row["tree_status"])] += 1
        return rows, dangling_ids, dict(status_counts)
    finally:
        connection.close()


def stable_paths(rows: list[dict[str, Any]], wait_seconds: int) -> tuple[list[Path], list[Path]]:
    paths = [Path(str(row["rollout_path"])) for row in rows if row.get("rollout_path")]
    paths = [path for path in paths if path.exists()]
    first = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in paths}
    print(f"Stability sample started for {len(paths)} files; waiting {wait_seconds}s", flush=True)
    time.sleep(wait_seconds)
    stable: list[Path] = []
    changed: list[Path] = []
    for path in paths:
        current = (path.stat().st_size, path.stat().st_mtime_ns)
        if current == first[str(path)]:
            stable.append(path)
        else:
            changed.append(path)
    return stable, changed


def write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".writing")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary_path, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--codex-home", default=r"D:\.codex")
    parser.add_argument("--backup-root", default=r"D:\Backup\codex_home_manager\backups")
    parser.add_argument("--reuse-root-backup")
    parser.add_argument("--report", required=True)
    parser.add_argument("--stability-seconds", type=int, default=20)
    parser.add_argument(
        "--include-statuses",
        default="root,open,closed",
        help="Comma-separated tree statuses to process, such as root,open.",
    )
    parser.add_argument(
        "--recent-compacted-user-messages",
        type=int,
        default=0,
        help=(
            "Keep only this many recent user messages inside the latest compacted "
            "checkpoint. This intentionally changes active model context and is "
            "restricted to --include-statuses root."
        ),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--preserve-ordinal-gaps",
        action="store_true",
        help=(
            "Replace removed compacted records with rejected gap placeholders so "
            "paginated rollout ordinals remain stable."
        ),
    )
    args = parser.parse_args()

    codex_home = Path(args.codex_home)
    database_path = codex_home / "state_5.sqlite"
    report_path = Path(args.report).resolve()
    rows, dangling_ids, status_counts = fetch_tree(database_path, args.root_id)
    included_statuses = {
        status.strip()
        for status in args.include_statuses.split(",")
        if status.strip()
    }
    allowed_statuses = {"root", "open", "closed", "unknown"}
    invalid_statuses = included_statuses - allowed_statuses
    if not included_statuses:
        raise RuntimeError("at least one tree status must be included")
    if invalid_statuses:
        raise RuntimeError(f"invalid tree statuses: {sorted(invalid_statuses)}")
    if args.recent_compacted_user_messages < 0:
        raise RuntimeError("--recent-compacted-user-messages cannot be negative")
    if args.recent_compacted_user_messages > 0 and included_statuses != {"root"}:
        raise RuntimeError(
            "--recent-compacted-user-messages is restricted to --include-statuses root"
        )
    rows = [row for row in rows if str(row["tree_status"]) in included_statuses]
    rows_by_path = {str(Path(str(row["rollout_path"]))): row for row in rows if row.get("rollout_path")}
    stable, initially_changed = stable_paths(rows, args.stability_seconds)
    stable_set = {str(path) for path in stable}
    ordered_rows = sorted(rows, key=lambda row: (row["id"] != args.root_id, str(row["id"])))
    scans: dict[str, dict[str, Any]] = {}
    missing_paths: list[str] = []
    parse_error_threads: list[str] = []
    binding_errors: list[str] = []
    total_before_bytes = 0
    for index, row in enumerate(ordered_rows, 1):
        path = Path(str(row["rollout_path"]))
        if not path.exists():
            missing_paths.append(str(path))
            continue
        total_before_bytes += path.stat().st_size
        if str(path) not in stable_set:
            continue
        scan = scan_file(path)
        scan["thread_id"] = str(row["id"])
        scan["tree_status"] = str(row["tree_status"])
        scans[str(row["id"])] = scan
        if scan["parse_errors"]:
            parse_error_threads.append(str(row["id"]))
        if scan["session_meta_id"] and scan["session_meta_id"] != row["id"]:
            binding_errors.append(str(row["id"]))
        if index % 25 == 0 or index == len(ordered_rows):
            print(
                f"Scanned {index}/{len(ordered_rows)} files; available memory "
                f"{available_memory_bytes() / gibibyte:.2f} GiB",
                flush=True,
            )

    candidates = [
        scan
        for scan in scans.values()
        if not scan["parse_errors"]
        and scan["session_meta_id"] in {"", scan["thread_id"]}
        and (
            scan["compacted_count"] > 1
            or scan["embedded_images"] > 0
            or scan["invalid_input_images"] > 0
            or (
                args.recent_compacted_user_messages > 0
                and scan["latest_compacted_user_messages"]
                > args.recent_compacted_user_messages
            )
        )
    ]
    candidates.sort(key=lambda scan: (scan["thread_id"] != args.root_id, scan["thread_id"]))
    print(
        f"Tree files={len(rows)}, candidates={len(candidates)}, "
        f"initially_changed={len(initially_changed)}, total={total_before_bytes / gibibyte:.3f} GiB",
        flush=True,
    )

    if parse_error_threads or binding_errors:
        raise RuntimeError(
            f"refusing to apply: parse_errors={parse_error_threads}, binding_errors={binding_errors}"
        )
    if not args.apply:
        write_json(
            report_path,
            {
                "mode": "preview",
                "root_id": args.root_id,
                "thread_count": len(rows),
                "dangling_ids": dangling_ids,
                "status_counts": status_counts,
                "included_statuses": sorted(included_statuses),
                "recent_compacted_user_messages": args.recent_compacted_user_messages,
                "candidate_count": len(candidates),
                "initially_changed": [str(path) for path in initially_changed],
                "total_before_bytes": total_before_bytes,
            },
        )
        return 0

    root_scan = scans.get(args.root_id)
    if root_scan is None:
        raise RuntimeError("root rollout was not stable during the initial sampling window")
    root_path = Path(root_scan["path"])
    if args.reuse_root_backup:
        root_backup_path = Path(args.reuse_root_backup).resolve()
        if not root_backup_path.exists():
            raise RuntimeError(f"reused main-thread backup does not exist: {root_backup_path}")
        backup_directory = root_backup_path.parent
        manifest_path = backup_directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("root_id") != args.root_id:
            raise RuntimeError("reused main-thread backup manifest has the wrong root thread id")
        root_backup_sha256 = file_sha256(root_backup_path)
        if root_backup_sha256 != manifest.get("sha256"):
            raise RuntimeError("reused main-thread backup hash does not match its manifest")
        print(f"Existing main-thread backup reverified: {root_backup_path}", flush=True)
    else:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        backup_directory = Path(args.backup_root) / f"{timestamp}_slim_quant_research_platform_tree_{args.root_id[:8]}"
        backup_directory.mkdir(parents=True, exist_ok=False)
        current_root_stat = root_path.stat()
        if (current_root_stat.st_size, current_root_stat.st_mtime_ns) != (
            root_scan["bytes"],
            root_scan["mtime_ns"],
        ):
            raise RuntimeError("root rollout changed before backup")
        root_backup_path = backup_directory / root_path.name
        shutil.copy2(root_path, root_backup_path)
        root_backup_sha256 = file_sha256(root_backup_path)
        if root_backup_sha256 != root_scan["sha256"]:
            raise RuntimeError("main-thread backup hash does not match the audited source")
        write_json(
            backup_directory / "manifest.json",
            {
                "created_at": datetime.now().astimezone().isoformat(),
                "root_id": args.root_id,
                "backup_policy": "main_thread_only_by_user_request",
                "source_path": str(root_path),
                "backup_path": str(root_backup_path),
                "sha256": root_backup_sha256,
                "bytes": root_backup_path.stat().st_size,
            },
        )
        print(f"Main-thread backup verified: {root_backup_path}", flush=True)

    results: list[dict[str, Any]] = []
    baseline_contracts: dict[str, dict[str, Any]] = {}
    baseline_compacted_history_contracts: dict[str, dict[str, Any]] = {}
    skipped_changed: list[str] = []
    contract_mismatches: list[str] = []
    for index, scan in enumerate(candidates, 1):
        require_memory(3 * gibibyte)
        thread_id = str(scan["thread_id"])
        path = Path(scan["path"])
        current_stat = path.stat()
        if (current_stat.st_size, current_stat.st_mtime_ns) != (scan["bytes"], scan["mtime_ns"]):
            skipped_changed.append(thread_id)
            print(f"Skipped changed source {thread_id}", flush=True)
            continue
        before_contract = contract_scan(path, scan["latest_compacted_line"])
        before_compacted_history_contract = compacted_history_contract(
            path,
            scan["latest_compacted_line"],
            args.recent_compacted_user_messages,
        )
        temp_path = path.with_name(path.name + f".slim-{os.getpid()}.tmp")
        try:
            stats = build_temp(
                path,
                scan,
                temp_path,
                recent_compacted_user_messages=args.recent_compacted_user_messages,
                preserve_ordinal_gaps=args.preserve_ordinal_gaps,
            )
            temp_scan = scan_file(temp_path)
            after_contract = contract_scan(temp_path, temp_scan["latest_compacted_line"])
            after_compacted_history_contract = compacted_history_contract(
                temp_path,
                temp_scan["latest_compacted_line"],
                args.recent_compacted_user_messages,
            )
            if after_contract["parse_errors"]:
                raise RuntimeError(f"temporary output has parse errors: {after_contract['parse_errors']}")
            if after_contract["compacted_count"] > 1:
                raise RuntimeError("temporary output still has duplicate compacted records")
            if after_contract["embedded_images"] or after_contract["invalid_input_images"]:
                raise RuntimeError("temporary output still has slim-target image records")
            if args.preserve_ordinal_gaps and temp_scan["line_count"] != scan["line_count"]:
                raise RuntimeError("temporary output changed paginated rollout line count")
            if not contracts_match(before_contract, after_contract):
                contract_mismatches.append(thread_id)
                raise RuntimeError("context contract mismatch")
            if (
                args.recent_compacted_user_messages > 0
                and not compacted_history_contracts_match(
                    before_compacted_history_contract,
                    after_compacted_history_contract,
                    args.recent_compacted_user_messages,
                )
            ):
                contract_mismatches.append(thread_id)
                raise RuntimeError("compacted history contract mismatch")
            current_stat = path.stat()
            if (current_stat.st_size, current_stat.st_mtime_ns) != (scan["bytes"], scan["mtime_ns"]):
                skipped_changed.append(thread_id)
                recycle_file(temp_path)
                print(f"Skipped source changed during transform {thread_id}", flush=True)
                continue
            before_bytes = path.stat().st_size
            replacement_method = replace_validated_file(temp_path, path)
            post_scan = scan_file(path)
            post_contract = contract_scan(path, post_scan["latest_compacted_line"])
            post_compacted_history_contract = compacted_history_contract(
                path,
                post_scan["latest_compacted_line"],
                args.recent_compacted_user_messages,
            )
            if post_scan["parse_errors"] or not contracts_match(before_contract, post_contract):
                raise RuntimeError("post-write validation failed")
            if args.preserve_ordinal_gaps and post_scan["line_count"] != scan["line_count"]:
                raise RuntimeError("post-write paginated rollout line count mismatch")
            if (
                args.recent_compacted_user_messages > 0
                and not compacted_history_contracts_match(
                    before_compacted_history_contract,
                    post_compacted_history_contract,
                    args.recent_compacted_user_messages,
                )
            ):
                raise RuntimeError("post-write compacted history validation failed")
            baseline_contracts[thread_id] = before_contract
            baseline_compacted_history_contracts[thread_id] = (
                before_compacted_history_contract
            )
            results.append(
                {
                    "id": thread_id,
                    "tree_status": scan["tree_status"],
                    "before_bytes": before_bytes,
                    "after_bytes": path.stat().st_size,
                    "saved_bytes": before_bytes - path.stat().st_size,
                    "compacted_before": scan["compacted_count"],
                    "compacted_after": post_scan["compacted_count"],
                    "embedded_images_before": scan["embedded_images"],
                    "embedded_images_after": post_scan["embedded_images"],
                    "invalid_input_images_before": scan["invalid_input_images"],
                    "invalid_input_images_after": post_scan["invalid_input_images"],
                    "replace_method": replacement_method,
                    "contract_preserved": True,
                    "compacted_history_contract_preserved": True,
                    "compacted_history_before": before_compacted_history_contract,
                    "compacted_history_after": post_compacted_history_contract,
                    "stats": stats,
                }
            )
        except Exception:
            if temp_path.exists():
                recycle_file(temp_path)
            raise
        if index % 10 == 0 or index == len(candidates):
            print(
                f"Applied {index}/{len(candidates)} candidates; available memory "
                f"{available_memory_bytes() / gibibyte:.2f} GiB",
                flush=True,
            )

    print("Post-write stability sample started", flush=True)
    written_stats = {
        result["id"]: (
            Path(scans[result["id"]]["path"]).stat().st_size,
            Path(scans[result["id"]]["path"]).stat().st_mtime_ns,
        )
        for result in results
    }
    time.sleep(args.stability_seconds)
    post_write_changed: list[str] = []
    post_write_parse_errors: list[str] = []
    post_write_contract_mismatches: list[str] = []
    for result in results:
        thread_id = result["id"]
        path = Path(scans[thread_id]["path"])
        current_stats = (path.stat().st_size, path.stat().st_mtime_ns)
        if current_stats != written_stats[thread_id]:
            post_write_changed.append(thread_id)
        post_scan = scan_file(path)
        if post_scan["parse_errors"]:
            post_write_parse_errors.append(thread_id)
        current_contract = contract_scan(path, post_scan["latest_compacted_line"])
        current_compacted_history_contract = compacted_history_contract(
            path,
            post_scan["latest_compacted_line"],
            args.recent_compacted_user_messages,
        )
        if (
            current_contract["embedded_images"]
            or current_contract["invalid_input_images"]
            or (thread_id not in post_write_changed and not contracts_match(baseline_contracts[thread_id], current_contract))
            or (
                args.recent_compacted_user_messages > 0
                and thread_id not in post_write_changed
                and not compacted_history_contracts_match(
                    baseline_compacted_history_contracts[thread_id],
                    current_compacted_history_contract,
                    args.recent_compacted_user_messages,
                )
            )
        ):
            post_write_contract_mismatches.append(thread_id)

    total_after_bytes = sum(
        Path(str(row["rollout_path"])).stat().st_size
        for row in rows
        if row.get("rollout_path") and Path(str(row["rollout_path"])).exists()
    )
    report = {
        "completed_at": datetime.now().astimezone().isoformat(),
        "root_id": args.root_id,
        "thread_count": len(rows),
        "dangling_ids": dangling_ids,
            "status_counts": status_counts,
            "included_statuses": sorted(included_statuses),
            "recent_compacted_user_messages": args.recent_compacted_user_messages,
            "preserve_ordinal_gaps": args.preserve_ordinal_gaps,
            "candidate_count": len(candidates),
        "modified_count": len(results),
        "initially_changed_threads": [rows_by_path.get(str(path), {}).get("id", str(path)) for path in initially_changed],
        "skipped_changed_threads": skipped_changed,
        "contract_mismatch_threads": contract_mismatches,
        "post_write_changed_threads": post_write_changed,
        "post_write_parse_error_threads": post_write_parse_errors,
        "post_write_contract_mismatch_threads": post_write_contract_mismatches,
        "backup_policy": "main_thread_only_by_user_request",
        "root_backup": str(root_backup_path),
        "root_backup_sha256": root_backup_sha256,
        "total_before_bytes": total_before_bytes,
        "total_after_bytes": total_after_bytes,
        "saved_bytes": total_before_bytes - total_after_bytes,
        "results": results,
    }
    write_json(report_path, report)
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
