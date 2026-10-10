from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--backup-root", type=Path, default=Path(r"D:\Backup\codex_thread_migrations"))
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--maximum-read-mib-per-second", type=int, default=64)
    parser.add_argument("--minimum-start-available-gib", type=float, default=8.0)
    parser.add_argument("--minimum-running-available-gib", type=float, default=4.0)
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(1, 9):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 8:
                raise
            time.sleep(min(0.1 * 2 ** (attempt - 1), 2.0))


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = "file:" + str(path.resolve()).replace("\\", "/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    return connection


def sqlite_backup(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(str(source), timeout=60)
    destination_connection = sqlite3.connect(str(destination), timeout=60)
    try:
        source_connection.backup(destination_connection, pages=4096, sleep=0.02)
        destination_connection.commit()
        check = destination_connection.execute("pragma quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"SQLite backup quick_check failed for {source}: {check}")
    finally:
        destination_connection.close()
        source_connection.close()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=16 * 1024 * 1024) as source:
        while block := source.read(16 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


def copy_and_hash(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb", buffering=16 * 1024 * 1024) as input_file:
        with destination.open("wb", buffering=16 * 1024 * 1024) as output_file:
            while block := input_file.read(16 * 1024 * 1024):
                output_file.write(block)
                digest.update(block)
            output_file.flush()
            os.fsync(output_file.fileno())
    shutil.copystat(source, destination)
    return digest.hexdigest().upper()


def add_sequence_value(digest: Any, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def collect_encrypted_content(value: Any, digest: Any) -> int:
    count = 0
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "encrypted_content" and isinstance(child, str):
                add_sequence_value(digest, child)
                count += 1
            else:
                count += collect_encrypted_content(child, digest)
    elif isinstance(value, list):
        for child in value:
            count += collect_encrypted_content(child, digest)
    return count


def normalized_user_content(content: Any) -> list[Any]:
    if not isinstance(content, list):
        return [content]
    normalized: list[Any] = []
    for child in content:
        if not isinstance(child, dict):
            normalized.append(child)
            continue
        child_type = str(child.get("type", "")).lower()
        if child_type in {"input_text", "text"}:
            normalized.append({
                "type": "text",
                "text": child.get("text", ""),
                "text_elements": child.get("text_elements", []),
            })
        elif child_type in {"input_image", "image"}:
            item = {"type": "image", "image_url": child.get("image_url")}
            if child.get("detail") is not None:
                item["detail"] = child["detail"]
            normalized.append(item)
        elif child_type == "local_image":
            item = {"type": "local_image", "path": child.get("path")}
            if child.get("detail") is not None:
                item["detail"] = child["detail"]
            normalized.append(item)
        elif child_type in {"input_audio", "audio"}:
            normalized.append({"type": "audio", "audio_url": child.get("audio_url")})
        elif child_type == "local_audio":
            normalized.append({"type": "local_audio", "path": child.get("path")})
        else:
            normalized.append(child)
    return normalized


def legacy_user_message(payload: dict[str, Any]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    message = payload.get("message", "")
    if isinstance(message, str) and message.strip():
        content.append({"type": "text", "text": message, "text_elements": payload.get("text_elements", [])})
    image_details = payload.get("image_details", [])
    for index, image_url in enumerate(payload.get("images") or []):
        item = {"type": "image", "image_url": image_url}
        if index < len(image_details) and image_details[index] is not None:
            item["detail"] = image_details[index]
        content.append(item)
    local_image_details = payload.get("local_image_details", [])
    for index, path in enumerate(payload.get("local_images") or []):
        item = {"type": "local_image", "path": path}
        if index < len(local_image_details) and local_image_details[index] is not None:
            item["detail"] = local_image_details[index]
        content.append(item)
    content.extend({"type": "audio", "audio_url": value} for value in payload.get("audio") or [])
    content.extend({"type": "local_audio", "path": value} for value in payload.get("local_audio") or [])
    result: dict[str, Any] = {"content": content}
    if payload.get("client_id") is not None:
        result["client_id"] = payload["client_id"]
    return result


def normalized_user_message(record_type: Any, payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if record_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        return {"content": normalized_user_content(payload.get("content"))}
    if record_type == "user_message":
        if "message" in payload:
            return legacy_user_message(payload)
        return {"content": normalized_user_content(payload.get("content", payload))}
    if record_type != "event_msg":
        return None
    if payload.get("type") == "user_message":
        return legacy_user_message(payload)
    if payload.get("type") == "item_completed" and isinstance(payload.get("item"), dict):
        item = payload["item"]
        if str(item.get("type", "")).lower().replace("_", "") == "usermessage":
            result: dict[str, Any] = {"content": normalized_user_content(item.get("content"))}
            if item.get("client_id") is not None:
                result["client_id"] = item["client_id"]
            return result
    return None


def normalized_assistant_message(record_type: Any, payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if record_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
        content = []
        for child in payload.get("content") or []:
            if isinstance(child, dict) and str(child.get("type", "")).lower() in {"output_text", "text"}:
                content.append({"type": "text", "text": child.get("text", "")})
            else:
                content.append(child)
        result: dict[str, Any] = {"content": content}
        for key in ("phase", "memory_citation", "delivery", "questions"):
            if payload.get(key) is not None:
                result[key] = payload[key]
        return result
    if record_type != "event_msg":
        return None
    if payload.get("type") == "agent_message" and payload.get("message"):
        result = {"content": [{"type": "text", "text": payload["message"]}]}
        for key in ("phase", "memory_citation", "delivery", "questions"):
            if payload.get(key) is not None:
                result[key] = payload[key]
        return result
    if payload.get("type") == "item_completed" and isinstance(payload.get("item"), dict):
        item = payload["item"]
        if str(item.get("type", "")).lower().replace("_", "") == "agentmessage":
            content = []
            for child in item.get("content") or []:
                if isinstance(child, dict) and str(child.get("type", "")).lower() in {"output_text", "text"}:
                    content.append({"type": "text", "text": child.get("text", "")})
                else:
                    content.append(child)
            result = {"content": content}
            for key in ("phase", "memory_citation", "delivery", "questions"):
                if item.get(key) is not None:
                    result[key] = item[key]
            return result
    return None


def normalized_response_item_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    normalized = payload
    if payload.get("type") == "reasoning" and payload.get("content") is None:
        normalized = dict(normalized)
        normalized.pop("content", None)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict) and set(metadata) == {"turn_id"} and isinstance(metadata.get("turn_id"), str):
        if normalized is payload:
            normalized = dict(normalized)
        normalized.pop("metadata", None)
    return normalized


def rollout_semantic_fingerprints(path: Path) -> dict[str, Any]:
    user_digest = hashlib.sha256()
    assistant_digest = hashlib.sha256()
    encrypted_digest = hashlib.sha256()
    response_item_digest = hashlib.sha256()
    record_count = 0
    user_count = 0
    assistant_count = 0
    encrypted_count = 0
    response_item_count = 0
    reasoning_null_content_normalized_count = 0
    rollback_count = 0
    encrypted_by_record_type: dict[str, int] = {}
    invalid_json = 0
    ordinal_errors = 0
    has_ordinals = None
    with path.open("r", encoding="utf-8", buffering=16 * 1024 * 1024) as source:
        for line in source:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_json += 1
                continue
            current_has_ordinal = isinstance(record.get("ordinal"), int)
            if has_ordinals is None:
                has_ordinals = current_has_ordinal
            if current_has_ordinal and record["ordinal"] != record_count:
                ordinal_errors += 1
            record_count += 1
            record_type = record.get("type")
            payload = record.get("payload")
            event_type = payload.get("type") if record_type == "event_msg" and isinstance(payload, dict) else None
            synthetic_projection_event = bool(
                record_type == "event_msg" and isinstance(payload, dict) and payload.get("_compat_projection") is True
            )
            if event_type == "thread_rolled_back":
                rollback_count += int(payload.get("num_turns", 0) or 0)
            encrypted_category = f"{record_type}/{event_type}" if event_type else str(record_type)
            category_digest = hashlib.sha256()
            category_count = collect_encrypted_content(payload, category_digest)
            if category_count:
                encrypted_by_record_type[encrypted_category] = encrypted_by_record_type.get(encrypted_category, 0) + category_count
            encrypted_count += collect_encrypted_content(payload, encrypted_digest)
            if record_type == "response_item":
                normalized_response_item = normalized_response_item_payload(payload)
                if normalized_response_item is not payload:
                    reasoning_null_content_normalized_count += 1
                add_sequence_value(response_item_digest, normalized_response_item)
                response_item_count += 1
            user_message = None if synthetic_projection_event else normalized_user_message(record_type, payload)
            if user_message is not None:
                add_sequence_value(user_digest, user_message)
                user_count += 1
            assistant_message = None if synthetic_projection_event else normalized_assistant_message(record_type, payload)
            if assistant_message is not None:
                add_sequence_value(assistant_digest, assistant_message)
                assistant_count += 1
    return {
        "recordCount": record_count,
        "invalidJson": invalid_json,
        "hasOrdinals": bool(has_ordinals),
        "ordinalErrors": ordinal_errors,
        "userPromptCount": user_count,
        "userPromptSequenceSha256": user_digest.hexdigest().upper(),
        "assistantMessageCount": assistant_count,
        "assistantMessageSequenceSha256": assistant_digest.hexdigest().upper(),
        "responseItemCount": response_item_count,
        "responseItemSequenceSha256": response_item_digest.hexdigest().upper(),
        "reasoningNullContentNormalizedCount": reasoning_null_content_normalized_count,
        "threadRollbackCount": rollback_count,
        "encryptedContentCount": encrypted_count,
        "encryptedContentSequenceSha256": encrypted_digest.hexdigest().upper(),
        "encryptedContentByRecordType": encrypted_by_record_type,
    }


def validate_migration_semantics(before: dict[str, Any], after: dict[str, Any]) -> None:
    if before["invalidJson"] or after["invalidJson"]:
        raise RuntimeError(f"Rollout JSON validation failed: before={before['invalidJson']}, after={after['invalidJson']}")
    if not after["hasOrdinals"] or after["ordinalErrors"]:
        raise RuntimeError(f"Migrated rollout ordinal validation failed: {after}")
    exact_fields = (
        ("userPromptCount", "userPromptSequenceSha256", "user prompts"),
        ("assistantMessageCount", "assistantMessageSequenceSha256", "assistant messages"),
        ("responseItemCount", "responseItemSequenceSha256", "response items"),
        ("encryptedContentCount", "encryptedContentSequenceSha256", "encrypted content"),
    )
    if before.get("threadRollbackCount", 0):
        for count_key, _, label in exact_fields:
            if after[count_key] > before[count_key]:
                raise RuntimeError(
                    f"Migration added {label} while resolving legacy rollback: "
                    f"before_count={before[count_key]}, after_count={after[count_key]}"
                )
        return
    for count_key, hash_key, label in exact_fields:
        if before[count_key] != after[count_key] or before[hash_key] != after[hash_key]:
            raise RuntimeError(
                f"Migration changed {label}: before_count={before[count_key]}, after_count={after[count_key]}, "
                f"before_hash={before[hash_key]}, after_hash={after[hash_key]}"
            )


def latest_session_name(index_path: Path, thread_id: str) -> str | None:
    latest_name = None
    with index_path.open("r", encoding="utf-8") as source:
        for line in source:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if value.get("id") == thread_id and isinstance(value.get("thread_name"), str):
                latest_name = value["thread_name"]
    return latest_name


def read_thread(state_path: Path, thread_id: str) -> dict[str, Any]:
    connection = connect_read_only(state_path)
    try:
        row = connection.execute("select * from threads where id = ?", (thread_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Thread is missing from state database: {thread_id}")
        return dict(row)
    finally:
        connection.close()


def shared_rollout_rows(state_path: Path, rollout_path: str) -> list[dict[str, Any]]:
    connection = connect_read_only(state_path)
    try:
        rows = connection.execute(
            "select id, history_mode from threads where rollout_path = ? order by id", (rollout_path,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def quick_check(path: Path) -> str:
    connection = connect_read_only(path)
    try:
        return str(connection.execute("pragma quick_check").fetchone()[0])
    finally:
        connection.close()


def thread_projection_summary(history_path: Path, thread_id: str, rollout_size: int) -> dict[str, Any]:
    connection = connect_read_only(history_path)
    try:
        projection = connection.execute(
            "select next_rollout_byte_offset, next_rollout_ordinal from thread_history_projection_state where thread_id = ?",
            (thread_id,),
        ).fetchone()
        turn_count = int(connection.execute("select count(*) from thread_turns where thread_id = ?", (thread_id,)).fetchone()[0])
        item_count = int(connection.execute("select count(*) from thread_items where thread_id = ?", (thread_id,)).fetchone()[0])
        realtime_count = int(
            connection.execute("select count(*) from thread_realtime_items where thread_id = ?", (thread_id,)).fetchone()[0]
        )
        invalid_item_json = 0
        for (item_json,) in connection.execute("select item_json from thread_items where thread_id = ?", (thread_id,)):
            try:
                json.loads(item_json)
            except (json.JSONDecodeError, TypeError):
                invalid_item_json += 1
        invalid_realtime_json = 0
        for (item_json,) in connection.execute(
            "select item_json from thread_realtime_items where thread_id = ?", (thread_id,)
        ):
            try:
                json.loads(item_json)
            except (json.JSONDecodeError, TypeError):
                invalid_realtime_json += 1
        invalid_error_json = 0
        for (error_json,) in connection.execute(
            "select error_json from thread_turns where thread_id = ? and error_json is not null", (thread_id,)
        ):
            try:
                json.loads(error_json)
            except (json.JSONDecodeError, TypeError):
                invalid_error_json += 1
        if projection is None:
            raise RuntimeError("Projection state was not created.")
        offset = int(projection[0])
        if offset != rollout_size:
            raise RuntimeError(f"Projection byte offset {offset} does not equal rollout size {rollout_size}.")
        if turn_count <= 0 or item_count <= 0:
            raise RuntimeError(f"Projection is empty: turns={turn_count}, items={item_count}")
        if invalid_item_json or invalid_realtime_json or invalid_error_json:
            raise RuntimeError(
                "Projected JSON validation failed: "
                f"items={invalid_item_json}, realtime={invalid_realtime_json}, errors={invalid_error_json}"
            )
        return {
            "nextRolloutByteOffset": offset,
            "nextRolloutOrdinal": int(projection[1]),
            "turnCount": turn_count,
            "itemCount": item_count,
            "realtimeItemCount": realtime_count,
            "invalidItemJson": invalid_item_json,
            "invalidRealtimeJson": invalid_realtime_json,
            "invalidErrorJson": invalid_error_json,
        }
    finally:
        connection.close()


def terminate_process_tree(process: psutil.Process) -> None:
    descendants = process.children(recursive=True)
    for child in reversed(descendants):
        try:
            child.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        process.terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    _, alive = psutil.wait_procs(descendants + [process], timeout=8)
    for item in alive:
        try:
            item.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def run_migration(
    node_path: Path,
    codex_script: Path,
    thread_id: str,
    maximum_read_mib_per_second: int,
    minimum_running_available_gib: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    command = [
        str(node_path), str(codex_script), "migrate-rollouts", "--apply", "--thread", thread_id,
        "--max-mib-per-second", str(maximum_read_mib_per_second), "--json",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    samples = []
    low_memory_samples = 0
    while True:
        try:
            output, _ = process.communicate(timeout=2)
            break
        except subprocess.TimeoutExpired:
            memory = psutil.virtual_memory()
            available_gib = memory.available / 1024**3
            private_bytes = 0
            try:
                migration_process = psutil.Process(process.pid)
                private_bytes = migration_process.memory_info().rss + sum(
                    child.memory_info().rss for child in migration_process.children(recursive=True)
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            samples.append({"at": utc_now(), "availableGiB": round(available_gib, 3), "processRssMiB": round(private_bytes / 1024**2, 2)})
            low_memory_samples = low_memory_samples + 1 if available_gib < minimum_running_available_gib else 0
            if low_memory_samples >= 3:
                terminate_process_tree(psutil.Process(process.pid))
                output, _ = process.communicate()
                raise RuntimeError(
                    f"Migration stopped because available memory remained below {minimum_running_available_gib} GiB."
                )
    if process.returncode != 0:
        raise RuntimeError(f"Official migration exited with code {process.returncode}: {output[-4000:]}")
    try:
        response = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Migration returned invalid JSON: {output[-4000:]}") from error
    outcomes = response.get("outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != 1 or outcomes[0].get("status") != "migrated":
        raise RuntimeError(f"Unexpected migration outcome: {response}")
    return response, samples, output


def read_json_rpc_response(process: subprocess.Popen[str], output_queue: queue.Queue[str], request_id: int, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = output_queue.get(timeout=min(1.0, max(0.01, deadline - time.monotonic())))
        except queue.Empty:
            if process.poll() is not None:
                raise RuntimeError(f"app-server exited before response {request_id}: {process.returncode}")
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get("id") == request_id:
            return value
    raise RuntimeError(f"Timed out waiting for app-server response {request_id}.")


def probe_items_api(node_path: Path, codex_script: Path, thread_id: str, codex_home: Path | None = None) -> dict[str, Any]:
    command = [str(node_path), str(codex_script), "app-server", "--stdio"]
    environment = os.environ.copy()
    if codex_home is not None:
        environment["CODEX_HOME"] = str(codex_home)
    process = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    output_queue: queue.Queue[str] = queue.Queue()
    stderr_lines: list[str] = []
    stdout_thread = threading.Thread(target=lambda: [output_queue.put(line) for line in process.stdout or []], daemon=True)
    stderr_thread = threading.Thread(target=lambda: [stderr_lines.append(line) for line in process.stderr or []], daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        messages = [
            {"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "migration-verifier", "version": "1.0.0"}, "capabilities": {"experimentalApi": True}}},
        ]
        assert process.stdin is not None
        for message in messages:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
        initialize = read_json_rpc_response(process, output_queue, 1, 45)
        if "error" in initialize:
            raise RuntimeError(f"app-server initialize failed: {initialize['error']}")
        process.stdin.write(json.dumps({"method": "initialized", "params": {}}, separators=(",", ":")) + "\n")
        process.stdin.write(
            json.dumps(
                {"method": "thread/items/list", "id": 2, "params": {"threadId": thread_id, "limit": 1, "sortDirection": "desc"}},
                separators=(",", ":"),
            ) + "\n"
        )
        process.stdin.flush()
        response = read_json_rpc_response(process, output_queue, 2, 120)
        if "error" in response:
            raise RuntimeError(f"thread/items/list failed: {response['error']}")
        data = response.get("result", {}).get("data")
        if not isinstance(data, list) or len(data) != 1:
            raise RuntimeError(f"thread/items/list returned an unexpected page: {response}")
        return {"passed": True, "itemCount": len(data), "nextCursorPresent": response.get("result", {}).get("nextCursor") is not None}
    finally:
        try:
            terminate_process_tree(psutil.Process(process.pid))
        except psutil.NoSuchProcess:
            pass


def preserve_failed_current(path: Path, failed_root: Path) -> None:
    if not path.exists():
        return
    failed_root.mkdir(parents=True, exist_ok=True)
    destination = failed_root / path.name
    if destination.exists():
        destination = failed_root / f"{path.name}.{time.time_ns()}"
    os.replace(path, destination)


def restore_from_backup(backup_directory: Path, originals: dict[str, Path]) -> None:
    failed_root = backup_directory / "failed-current"
    for name, original in originals.items():
        backup = backup_directory / name
        if not backup.exists():
            continue
        for suffix in ("-wal", "-shm"):
            preserve_failed_current(Path(str(original) + suffix), failed_root)
        preserve_failed_current(original, failed_root)
        shutil.copy2(backup, original)


def main() -> int:
    arguments = parse_arguments()
    state_path = Path(r"D:\.codex\state_5.sqlite")
    history_path = Path(r"D:\.codex\thread_history_1.sqlite")
    index_path = Path(r"D:\.codex\session_index.jsonl")
    global_path = Path(r"D:\.codex\.codex-global-state.json")
    global_backup_path = Path(r"D:\.codex\.codex-global-state.json.bak")
    node_path = Path(r"D:\Software\anaconda3\node.exe")
    codex_script = Path(r"D:\Software\anaconda3\node_modules\@openai\codex\bin\codex.js")
    required = [state_path, history_path, index_path, global_path, global_backup_path, node_path, codex_script]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"Required files are missing: {missing}")
    before_thread = read_thread(state_path, arguments.thread_id)
    if before_thread.get("history_mode") != "legacy":
        raise RuntimeError(f"Pilot requires a legacy thread; found {before_thread.get('history_mode')!r}.")
    rollout_path = Path(str(before_thread["rollout_path"]))
    if not rollout_path.is_file():
        raise RuntimeError(f"Rollout is missing: {rollout_path}")
    shared = shared_rollout_rows(state_path, str(before_thread["rollout_path"]))
    if len(shared) != 1 or shared[0]["id"] != arguments.thread_id:
        raise RuntimeError(f"Shared rollout aliases block migration: {shared}")
    expected_name = latest_session_name(index_path, arguments.thread_id)
    if not expected_name:
        raise RuntimeError("Latest session index name is missing; name preservation cannot be verified.")
    preflight = {
        "threadId": arguments.thread_id,
        "historyMode": before_thread["history_mode"],
        "rolloutPath": str(rollout_path),
        "rolloutSize": rollout_path.stat().st_size,
        "expectedName": expected_name,
        "sharedRows": shared,
        "stateQuickCheck": quick_check(state_path),
        "historyQuickCheck": quick_check(history_path),
        "availableMemoryGiB": round(psutil.virtual_memory().available / 1024**3, 3),
        "freeBackupDiskGiB": round(shutil.disk_usage(arguments.backup_root.parent).free / 1024**3, 3),
    }
    if preflight["stateQuickCheck"] != "ok" or preflight["historyQuickCheck"] != "ok":
        raise RuntimeError(f"Preflight SQLite check failed: {preflight}")
    if not arguments.apply:
        write_json_atomic(arguments.result, {"schemaVersion": 1, "status": "preflight_passed", "at": utc_now(), "preflight": preflight})
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0
    if preflight["availableMemoryGiB"] < arguments.minimum_start_available_gib:
        raise RuntimeError(
            f"Available memory {preflight['availableMemoryGiB']} GiB is below start gate {arguments.minimum_start_available_gib} GiB."
        )
    required_disk = rollout_path.stat().st_size * 2 + state_path.stat().st_size * 2 + history_path.stat().st_size * 3
    if shutil.disk_usage(arguments.backup_root.parent).free < required_disk:
        raise RuntimeError("Backup disk does not have enough free space for backup and migration growth.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_directory = arguments.backup_root / f"{stamp}-{arguments.thread_id}"
    backup_directory.mkdir(parents=True, exist_ok=False)
    originals = {
        "state_5.sqlite": state_path,
        "thread_history_1.sqlite": history_path,
        "session_index.jsonl": index_path,
        ".codex-global-state.json": global_path,
        ".codex-global-state.json.bak": global_backup_path,
        rollout_path.name: rollout_path,
    }
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "running",
        "startedAt": utc_now(),
        "threadId": arguments.thread_id,
        "backupDirectory": str(backup_directory),
        "preflight": preflight,
    }
    write_json_atomic(arguments.result, result)
    mutation_started = False
    try:
        sqlite_backup(state_path, backup_directory / "state_5.sqlite")
        sqlite_backup(history_path, backup_directory / "thread_history_1.sqlite")
        shutil.copy2(index_path, backup_directory / "session_index.jsonl")
        shutil.copy2(global_path, backup_directory / ".codex-global-state.json")
        shutil.copy2(global_backup_path, backup_directory / ".codex-global-state.json.bak")
        rollout_sha256 = copy_and_hash(rollout_path, backup_directory / rollout_path.name)
        backup_sha256 = hash_file(backup_directory / rollout_path.name)
        if rollout_sha256 != backup_sha256:
            raise RuntimeError("Rollout backup hash does not match the source hash.")
        before_semantics = rollout_semantic_fingerprints(backup_directory / rollout_path.name)
        result["backup"] = {
            "rolloutSha256": rollout_sha256,
            "rolloutBackupSha256": backup_sha256,
            "stateQuickCheck": quick_check(backup_directory / "state_5.sqlite"),
            "historyQuickCheck": quick_check(backup_directory / "thread_history_1.sqlite"),
            "rolloutSemantics": before_semantics,
        }
        write_json_atomic(backup_directory / "manifest.json", result)
        mutation_started = True
        migration, memory_samples, raw_output = run_migration(
            node_path, codex_script, arguments.thread_id, arguments.maximum_read_mib_per_second,
            arguments.minimum_running_available_gib,
        )
        after_thread = read_thread(state_path, arguments.thread_id)
        if after_thread.get("history_mode") != "paginated":
            raise RuntimeError(f"Thread history mode is not paginated: {after_thread.get('history_mode')!r}")
        if after_thread.get("name") != expected_name:
            raise RuntimeError(
                f"Thread name was not preserved: expected {expected_name!r}, found {after_thread.get('name')!r}."
            )
        rollout_after_sha256 = hash_file(rollout_path)
        after_semantics = rollout_semantic_fingerprints(rollout_path)
        validate_migration_semantics(before_semantics, after_semantics)
        projection = thread_projection_summary(history_path, arguments.thread_id, rollout_path.stat().st_size)
        if quick_check(state_path) != "ok" or quick_check(history_path) != "ok":
            raise RuntimeError("Post-migration SQLite quick_check failed.")
        items_api = probe_items_api(node_path, codex_script, arguments.thread_id)
        result.update(
            {
                "status": "migration_validated",
                "finishedAt": utc_now(),
                "migration": migration,
                "migrationRawOutput": raw_output,
                "memorySamples": memory_samples,
                "afterThread": {"historyMode": after_thread["history_mode"], "name": after_thread["name"]},
                "projection": projection,
                "itemsApi": items_api,
                "rolloutAfterSha256": rollout_after_sha256,
                "rolloutAfterSemantics": after_semantics,
                "rolledBack": False,
            }
        )
        write_json_atomic(backup_directory / "manifest.json", result)
        write_json_atomic(arguments.result, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        rollback_error = None
        if mutation_started:
            try:
                restore_from_backup(backup_directory, originals)
                if quick_check(state_path) != "ok" or quick_check(history_path) != "ok":
                    raise RuntimeError("Rollback SQLite quick_check failed.")
            except Exception as restore_error:
                rollback_error = repr(restore_error)
        result.update(
            {
                "status": "failed_rolled_back" if mutation_started and rollback_error is None else "failed",
                "finishedAt": utc_now(),
                "error": repr(error),
                "rolledBack": mutation_started and rollback_error is None,
                "rollbackError": rollback_error,
            }
        )
        write_json_atomic(backup_directory / "manifest.json", result)
        write_json_atomic(arguments.result, result)
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
