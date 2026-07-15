from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from .windows_paths import windows_extended_path

from .codex_data import image_url_is_invalid


@dataclass(frozen=True)
class RepairThresholds:
    max_active_bytes: int = 100 * 1024 * 1024
    max_active_lines: int = 50_000
    max_tail_lines: int = 20_000


@dataclass(frozen=True)
class RolloutScan:
    path: str
    source_sha256: str
    total_bytes: int
    line_count: int
    parse_errors: int
    latest_compacted_line: int
    tail_line_count: int
    tools_summary_output_count: int
    thread_name_updated_count: int
    invalid_image_url_count: int
    estimated_current_parser_errors: int
    initial_session_meta_lines: int
    session_meta_id: str
    latest_full_world_state_line_before_compaction: int
    latest_compacted_checkpoint_valid: bool
    latest_compacted_checkpoint_reason: str
    latest_compacted_replacement_history_count: int
    latest_compacted_text_item_count: int
    user_prompt_count: int
    user_prompt_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SlimViewResult:
    source_path: str
    destination_path: str
    source_line_count: int
    view_line_count: int
    source_bytes: int
    view_bytes: int
    parse_errors: int
    latest_compacted_line: int
    preserved_full_world_state_line: int
    session_meta_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InPlaceRepairResult:
    thread_id: str
    source_path: str
    backup_path: str
    original_bytes: int
    active_bytes: int
    original_sha256: str
    backup_sha256: str
    active_sha256: str
    source_line_count: int
    active_line_count: int
    latest_compacted_line: int
    journal_path: str
    user_prompt_count: int
    user_prompt_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompatibilityViewResult:
    source_path: str
    destination_path: str
    source_line_count: int
    view_line_count: int
    source_bytes: int
    view_bytes: int
    migrated_tools_summary_count: int
    removed_thread_name_updated_count: int
    removed_invalid_image_url_count: int
    session_meta_id: str
    latest_compacted_line: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as source:
        os.fsync(source.fileno())


def _write_json_fsync(path: Path, payload: dict[str, Any]) -> None:
    durable_path = windows_extended_path(path)
    durable_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = durable_path.with_suffix(durable_path.suffix + f".{uuid4().hex}.writing")
    with temporary_path.open("w", encoding="utf-8", newline="") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary_path, durable_path)


def _is_tools_summary_output(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("type") != "response_item":
        return False
    payload = item.get("payload")
    return (
        isinstance(payload, dict)
        and payload.get("type") == "tool_search_output"
        and "tools_summary" in payload
        and "tools" not in payload
    )


def _is_thread_name_updated(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("type") != "event_msg":
        return False
    payload = item.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "thread_name_updated"


_drop_invalid_image_object = object()


def _repair_invalid_image_urls(value: Any, stats: dict[str, int]) -> Any:
    if isinstance(value, list):
        repaired: list[Any] = []
        for item in value:
            repaired_item = _repair_invalid_image_urls(item, stats)
            if repaired_item is not _drop_invalid_image_object:
                repaired.append(repaired_item)
        return repaired
    if not isinstance(value, dict):
        return value
    item_type = value.get("type")
    if "image_url" in value and isinstance(item_type, str) and image_url_is_invalid(value.get("image_url")):
        stats["removed"] += 1
        return _drop_invalid_image_object
    repaired_dict: dict[str, Any] = {}
    for key, item in value.items():
        if key == "encrypted_content":
            repaired_dict[key] = item
            continue
        if key == "image_url":
            invalid_runtime_value = (
                isinstance(item, str) and image_url_is_invalid(item)
            ) or (
                not isinstance(item, str)
                and isinstance(value.get("type"), str)
                and image_url_is_invalid(item)
            )
            if invalid_runtime_value:
                stats["removed"] += 1
                continue
        repaired_item = _repair_invalid_image_urls(item, stats)
        if repaired_item is not _drop_invalid_image_object:
            repaired_dict[key] = repaired_item
    return repaired_dict


def _count_invalid_image_urls(value: Any) -> int:
    if isinstance(value, list):
        return sum(_count_invalid_image_urls(item) for item in value)
    if not isinstance(value, dict):
        return 0
    count = 0
    for key, item in value.items():
        if key == "encrypted_content":
            continue
        if key == "image_url":
            if isinstance(item, str) and image_url_is_invalid(item):
                count += 1
            elif not isinstance(item, str) and isinstance(value.get("type"), str) and image_url_is_invalid(item):
                count += 1
        count += _count_invalid_image_urls(item)
    return count


def _validate_replacement_history_item(item: dict[str, Any]) -> tuple[bool, bool]:
    item_type = item.get("type")
    if item_type == "message":
        if item.get("role") not in {"user", "developer", "system", "assistant"}:
            return False, False
        content = item.get("content")
        if not isinstance(content, list) or not content:
            return False, False
        has_text = False
        for block in content:
            if not isinstance(block, dict):
                return False, False
            block_type = block.get("type")
            if block_type in {"input_text", "output_text"}:
                if not isinstance(block.get("text"), str):
                    return False, False
                has_text = has_text or bool(block["text"].strip())
                continue
            if block_type == "input_image":
                image_url = block.get("image_url")
                if not isinstance(image_url, str) or image_url_is_invalid(image_url):
                    return False, False
                detail = block.get("detail")
                if detail is not None and detail not in {"auto", "low", "high", "original"}:
                    return False, False
                continue
            return False, False
        return True, has_text
    if item_type == "compaction":
        compaction_id = item.get("id")
        valid = (
            (compaction_id is None or (isinstance(compaction_id, str) and bool(compaction_id.strip())))
            and isinstance(item.get("encrypted_content"), str)
            and bool(item["encrypted_content"].strip())
        )
        return valid, False
    return False, False


def compacted_checkpoint_contract(item: Any) -> tuple[bool, str, int, int]:
    if not isinstance(item, dict) or item.get("type") != "compacted":
        return False, "not_compacted", 0, 0
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return False, "missing_payload", 0, 0
    replacement_history = payload.get("replacement_history")
    if not isinstance(replacement_history, list) or not replacement_history:
        return False, "empty_replacement_history", 0, 0
    if any(not isinstance(history_item, dict) for history_item in replacement_history):
        return False, "malformed_replacement_history", len(replacement_history), 0
    try:
        json.loads(json.dumps(replacement_history, ensure_ascii=False))
    except (TypeError, ValueError):
        return False, "unserializable_replacement_history", len(replacement_history), 0
    text_item_count = 0
    compaction_item_count = 0
    for index, history_item in enumerate(replacement_history):
        item_valid, has_text = _validate_replacement_history_item(history_item)
        if not item_valid:
            return (
                False,
                f"invalid_replacement_history_item_{index}",
                len(replacement_history),
                text_item_count,
            )
        text_item_count += int(has_text)
        compaction_item_count += int(history_item.get("type") == "compaction")
    if text_item_count == 0:
        return False, "replacement_history_has_no_replayable_text", len(replacement_history), 0
    if compaction_item_count == 0:
        return False, "replacement_history_has_no_compaction_item", len(replacement_history), text_item_count
    return True, "", len(replacement_history), text_item_count


_semantic_stop_words = {
    "about",
    "after",
    "again",
    "also",
    "before",
    "from",
    "have",
    "into",
    "that",
    "their",
    "there",
    "these",
    "this",
    "with",
    "would",
}


def _semantic_tokens(text: str) -> set[str]:
    normalized = text.casefold()
    words = {
        word
        for word in re.findall(r"[a-z0-9_]{4,}", normalized)
        if word not in _semantic_stop_words
    }
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    cjk_bigrams = {
        run[index : index + 2]
        for run in cjk_runs
        for index in range(max(0, len(run) - 1))
    }
    return words | cjk_bigrams


def _message_text_tokens(item: Any) -> set[str]:
    if not isinstance(item, dict):
        return set()
    message = item
    if item.get("type") == "response_item" and isinstance(item.get("payload"), dict):
        message = item["payload"]
    if message.get("type") != "message" or message.get("role") not in {"user", "developer", "assistant"}:
        return set()
    tokens: set[str] = set()
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") in {"input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                tokens.update(_semantic_tokens(text))
    return tokens


def _is_user_prompt_record(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    item_type = item.get("type")
    payload = item.get("payload")
    return bool(
        item_type == "user_message"
        or (item_type == "response_item" and isinstance(payload, dict) and payload.get("role") == "user")
        or (
            item_type == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "user_message"
        )
    )


def _user_prompt_text_parts(item: Any) -> tuple[str, ...]:
    if not isinstance(item, dict):
        return ()
    item_type = item.get("type")
    payload = item.get("payload")
    text_parts: list[str] = []
    if item_type == "user_message":
        if isinstance(payload, str):
            text_parts.append(payload)
        elif isinstance(payload, dict):
            content = payload.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                text_parts.extend(
                    part["text"]
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
            if isinstance(payload.get("text"), str):
                text_parts.append(payload["text"])
    elif item_type == "response_item" and isinstance(payload, dict) and payload.get("role") == "user":
        content = payload.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            text_parts.extend(
                part["text"]
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    elif item_type == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
        message = payload.get("message")
        if isinstance(message, str):
            text_parts.append(message)
    return tuple(text_parts)


def _update_user_prompt_digest(digest: Any, text_parts: tuple[str, ...]) -> None:
    encoded = json.dumps(text_parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def user_prompt_prefix_fingerprint(path: Path, prompt_limit: int) -> tuple[int, str]:
    if prompt_limit < 0:
        raise ValueError("prompt_limit must be non-negative")
    digest = hashlib.sha256()
    prompt_count = 0
    if prompt_limit == 0:
        return prompt_count, digest.hexdigest()
    with path.open("rb") as source:
        for raw_line in source:
            item = json.loads(raw_line)
            if not _is_user_prompt_record(item):
                continue
            prompt_count += 1
            _update_user_prompt_digest(digest, _user_prompt_text_parts(item))
            if prompt_count == prompt_limit:
                break
    return prompt_count, digest.hexdigest()


def validate_user_prompt_contract(
    path: Path,
    expected_count: int,
    expected_sha256: str,
    *,
    allow_appended: bool,
    current_scan: RolloutScan | None = None,
) -> dict[str, Any]:
    normalized_sha256 = str(expected_sha256 or "").casefold()
    if expected_count < 0 or not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
        raise RuntimeError("user prompt baseline is invalid")
    active_scan = current_scan or scan_rollout(path)
    if allow_appended:
        if active_scan.user_prompt_count < expected_count:
            raise RuntimeError("user prompt records were removed after the baseline audit")
        if active_scan.user_prompt_count == expected_count:
            if active_scan.user_prompt_sha256 != normalized_sha256:
                raise RuntimeError("user prompt baseline is no longer an exact prefix of the active rollout")
        else:
            prefix_count, prefix_sha256 = user_prompt_prefix_fingerprint(path, expected_count)
            if prefix_count != expected_count or prefix_sha256 != normalized_sha256:
                raise RuntimeError("user prompt baseline is no longer an exact prefix of the active rollout")
    elif (
        active_scan.user_prompt_count != expected_count
        or active_scan.user_prompt_sha256 != normalized_sha256
    ):
        raise RuntimeError("active rollout user prompts differ from the baseline audit")
    return {
        "baseline_count": expected_count,
        "baseline_sha256": normalized_sha256,
        "current_count": active_scan.user_prompt_count,
        "current_sha256": active_scan.user_prompt_sha256,
        "appended_count": active_scan.user_prompt_count - expected_count,
    }


def compacted_checkpoint_semantic_contract(item: Any, source_tokens: set[str]) -> tuple[bool, str]:
    if not source_tokens:
        return True, ""
    payload = item.get("payload") if isinstance(item, dict) else None
    replacement_history = payload.get("replacement_history") if isinstance(payload, dict) else None
    checkpoint_tokens: set[str] = set()
    for history_item in replacement_history or []:
        checkpoint_tokens.update(_message_text_tokens(history_item))
    if not checkpoint_tokens:
        return False, "checkpoint_has_no_semantic_tokens"
    if source_tokens.isdisjoint(checkpoint_tokens):
        return False, "checkpoint_text_has_no_source_overlap"
    return True, ""


def _migrate_compatibility_item(item: Any) -> tuple[Any, bool, int]:
    if not isinstance(item, dict):
        return item, False, 0
    changed = False
    if _is_tools_summary_output(item):
        item["payload"]["tools"] = []
        changed = True
    image_stats = {"removed": 0}
    repaired = _repair_invalid_image_urls(item, image_stats)
    if repaired is _drop_invalid_image_object:
        raise RuntimeError("top-level rollout item cannot be an image object")
    return repaired, changed or bool(image_stats["removed"]), image_stats["removed"]


def scan_rollout(path: Path) -> RolloutScan:
    source_digest = hashlib.sha256()
    total_bytes = 0
    line_count = 0
    parse_errors = 0
    latest_compacted_line = 0
    tools_summary_output_count = 0
    thread_name_updated_count = 0
    invalid_image_url_count = 0
    initial_session_meta_lines = 0
    session_meta_id = ""
    initial_meta_phase = True
    latest_full_world_state_line = 0
    latest_compacted_checkpoint_valid = False
    latest_compacted_checkpoint_reason = "missing_compacted_checkpoint"
    latest_compacted_replacement_history_count = 0
    latest_compacted_text_item_count = 0
    source_semantic_tokens: set[str] = set()
    user_prompt_digest = hashlib.sha256()
    user_prompt_count = 0

    with path.open("rb") as source:
        for line_count, raw_line in enumerate(source, 1):
            source_digest.update(raw_line)
            total_bytes += len(raw_line)
            try:
                item = json.loads(raw_line)
            except Exception:
                parse_errors += 1
                initial_meta_phase = False
                continue

            item_type = item.get("type") if isinstance(item, dict) else None
            payload = item.get("payload") if isinstance(item, dict) and isinstance(item.get("payload"), dict) else {}
            if initial_meta_phase and item_type == "session_meta":
                initial_session_meta_lines += 1
                if not session_meta_id:
                    session_meta_id = str(payload.get("id") or "")
            else:
                initial_meta_phase = False

            if item_type == "compacted":
                latest_compacted_line = line_count
                (
                    latest_compacted_checkpoint_valid,
                    latest_compacted_checkpoint_reason,
                    latest_compacted_replacement_history_count,
                    latest_compacted_text_item_count,
                ) = compacted_checkpoint_contract(item)
                if latest_compacted_checkpoint_valid:
                    semantic_valid, semantic_reason = compacted_checkpoint_semantic_contract(
                        item, source_semantic_tokens
                    )
                    if not semantic_valid:
                        latest_compacted_checkpoint_valid = False
                        latest_compacted_checkpoint_reason = semantic_reason
            elif item_type == "world_state" and payload.get("full") is True:
                latest_full_world_state_line = line_count

            if item_type != "compacted":
                source_semantic_tokens.update(_message_text_tokens(item))

            user_prompt_text_parts = _user_prompt_text_parts(item)
            if _is_user_prompt_record(item):
                user_prompt_count += 1
                _update_user_prompt_digest(user_prompt_digest, user_prompt_text_parts)

            if _is_tools_summary_output(item):
                tools_summary_output_count += 1
            if _is_thread_name_updated(item):
                thread_name_updated_count += 1
            invalid_image_url_count += _count_invalid_image_urls(item)

    tail_line_count = line_count - latest_compacted_line + 1 if latest_compacted_line else line_count
    full_world_state_before_compaction = (
        latest_full_world_state_line
        if latest_full_world_state_line and latest_full_world_state_line < latest_compacted_line
        else 0
    )
    return RolloutScan(
        path=str(path),
        source_sha256=source_digest.hexdigest(),
        total_bytes=total_bytes,
        line_count=line_count,
        parse_errors=parse_errors,
        latest_compacted_line=latest_compacted_line,
        tail_line_count=tail_line_count,
        tools_summary_output_count=tools_summary_output_count,
        thread_name_updated_count=thread_name_updated_count,
        invalid_image_url_count=invalid_image_url_count,
        estimated_current_parser_errors=(
            tools_summary_output_count + thread_name_updated_count + invalid_image_url_count
        ),
        initial_session_meta_lines=initial_session_meta_lines,
        session_meta_id=session_meta_id,
        latest_full_world_state_line_before_compaction=full_world_state_before_compaction,
        latest_compacted_checkpoint_valid=latest_compacted_checkpoint_valid,
        latest_compacted_checkpoint_reason=latest_compacted_checkpoint_reason,
        latest_compacted_replacement_history_count=latest_compacted_replacement_history_count,
        latest_compacted_text_item_count=latest_compacted_text_item_count,
        user_prompt_count=user_prompt_count,
        user_prompt_sha256=user_prompt_digest.hexdigest(),
    )


def create_slim_view(source_path: Path, destination_path: Path, expected_thread_id: str) -> SlimViewResult:
    scan = scan_rollout(source_path)
    if scan.parse_errors:
        raise RuntimeError(f"source rollout has {scan.parse_errors} JSON parse errors")
    if not scan.latest_compacted_line:
        raise RuntimeError("source rollout has no compacted checkpoint")
    if not scan.latest_compacted_checkpoint_valid:
        raise RuntimeError(
            f"source rollout compacted checkpoint is not replayable: {scan.latest_compacted_checkpoint_reason}"
        )
    if not scan.session_meta_id:
        raise RuntimeError("source rollout has no initial session_meta id")
    if scan.session_meta_id != expected_thread_id:
        raise RuntimeError(
            f"session_meta id {scan.session_meta_id} does not match expected thread id {expected_thread_id}"
        )

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    view_line_count = 0
    parse_errors = 0
    with source_path.open("rb") as source, destination_path.open("wb") as destination:
        for line_number, raw_line in enumerate(source, 1):
            try:
                item = json.loads(raw_line)
            except Exception:
                parse_errors += 1
                continue
            keep = line_number <= scan.initial_session_meta_lines
            if line_number == scan.latest_full_world_state_line_before_compaction:
                keep = True
            if line_number >= scan.latest_compacted_line:
                keep = True
            if _is_user_prompt_record(item):
                keep = True
            if not keep:
                continue
            if line_number > scan.initial_session_meta_lines and item.get("type") == "session_meta":
                continue
            if _is_thread_name_updated(item):
                continue
            migrated_item, changed, _removed_images = _migrate_compatibility_item(item)
            if changed:
                encoded = json.dumps(migrated_item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                destination.write(encoded)
            else:
                destination.write(raw_line if raw_line.endswith(b"\n") else raw_line + b"\n")
            view_line_count += 1

    return SlimViewResult(
        source_path=str(source_path),
        destination_path=str(destination_path),
        source_line_count=scan.line_count,
        view_line_count=view_line_count,
        source_bytes=scan.total_bytes,
        view_bytes=destination_path.stat().st_size,
        parse_errors=parse_errors,
        latest_compacted_line=scan.latest_compacted_line,
        preserved_full_world_state_line=scan.latest_full_world_state_line_before_compaction,
        session_meta_id=scan.session_meta_id,
    )


def create_compatibility_view(
    source_path: Path,
    destination_path: Path,
    expected_thread_id: str,
) -> CompatibilityViewResult:
    scan = scan_rollout(source_path)
    if scan.parse_errors:
        raise RuntimeError(f"source rollout has {scan.parse_errors} JSON parse errors")
    if not scan.session_meta_id:
        raise RuntimeError("source rollout has no initial session_meta id")
    if scan.session_meta_id != expected_thread_id:
        raise RuntimeError(
            f"session_meta id {scan.session_meta_id} does not match expected thread id {expected_thread_id}"
        )

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    view_line_count = 0
    migrated_tools_summary_count = 0
    removed_thread_name_updated_count = 0
    removed_invalid_image_url_count = 0
    with source_path.open("rb") as source, destination_path.open("wb") as destination:
        for raw_line in source:
            item = json.loads(raw_line)
            if _is_thread_name_updated(item):
                removed_thread_name_updated_count += 1
                continue
            was_tools_summary = _is_tools_summary_output(item)
            migrated_item, changed, removed_images = _migrate_compatibility_item(item)
            removed_invalid_image_url_count += removed_images
            if changed:
                encoded = json.dumps(migrated_item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                destination.write(encoded)
                if was_tools_summary:
                    migrated_tools_summary_count += 1
            else:
                destination.write(raw_line if raw_line.endswith(b"\n") else raw_line + b"\n")
            view_line_count += 1

    return CompatibilityViewResult(
        source_path=str(source_path),
        destination_path=str(destination_path),
        source_line_count=scan.line_count,
        view_line_count=view_line_count,
        source_bytes=scan.total_bytes,
        view_bytes=destination_path.stat().st_size,
        migrated_tools_summary_count=migrated_tools_summary_count,
        removed_thread_name_updated_count=removed_thread_name_updated_count,
        removed_invalid_image_url_count=removed_invalid_image_url_count,
        session_meta_id=scan.session_meta_id,
        latest_compacted_line=scan.latest_compacted_line,
    )


def _repair_with_view_builder(
    source_path: Path,
    backup_root: Path,
    expected_thread_id: str,
    audited_size: int,
    audited_mtime_ns: int,
    audited_sha256: str,
    view_builder: Callable[[Path, Path, str], SlimViewResult | CompatibilityViewResult],
    precommit_guard: Callable[[], None] | None = None,
    before_replace: Callable[[dict[str, Any]], None] | None = None,
    post_replace_hook: Callable[[], None] | None = None,
) -> InPlaceRepairResult:
    source_path = source_path.resolve()
    backup_root = backup_root.resolve()
    initial_stat = source_path.stat()
    initial_identity = (initial_stat.st_dev, initial_stat.st_ino)
    if initial_stat.st_size != audited_size or initial_stat.st_mtime_ns != audited_mtime_ns:
        raise RuntimeError("source rollout changed since audit")
    if _sha256(source_path) != audited_sha256:
        raise RuntimeError("source rollout hash changed since audit")

    backup_path = backup_root / expected_thread_id / source_path.name
    if backup_path.exists():
        raise RuntimeError(f"backup path already exists: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_view = source_path.with_name(f".{source_path.name}.{uuid4().hex}.repairing")
    try:
        source_scan = scan_rollout(source_path)
        view_result = view_builder(source_path, temporary_view, expected_thread_id)
        active_scan = scan_rollout(temporary_view)
        if active_scan.parse_errors:
            raise RuntimeError(f"generated active view has {active_scan.parse_errors} JSON parse errors")
        if active_scan.estimated_current_parser_errors:
            raise RuntimeError(
                f"generated active view has {active_scan.estimated_current_parser_errors} current-parser errors"
            )
        if active_scan.session_meta_id != expected_thread_id:
            raise RuntimeError("generated active view thread id does not match expected thread id")
        if (
            active_scan.user_prompt_count != source_scan.user_prompt_count
            or active_scan.user_prompt_sha256 != source_scan.user_prompt_sha256
        ):
            raise RuntimeError("generated active view changed user prompt text or order")

        def assert_source_unchanged() -> None:
            current_stat = source_path.stat()
            if (
                current_stat.st_size != audited_size
                or current_stat.st_mtime_ns != audited_mtime_ns
                or (current_stat.st_dev, current_stat.st_ino) != initial_identity
            ):
                raise RuntimeError("source rollout identity or metadata changed since audit")
            if _sha256(source_path) != audited_sha256:
                raise RuntimeError("source rollout hash changed since audit")

        assert_source_unchanged()

        original_sha256 = _sha256(source_path)
        if original_sha256 != audited_sha256:
            raise RuntimeError("source rollout hash changed since audit")
        shutil.copy2(source_path, backup_path)
        _fsync_file(backup_path)
        backup_sha256 = _sha256(backup_path)
        if backup_sha256 != original_sha256:
            raise RuntimeError("archived rollout hash does not match original")

        assert_source_unchanged()
        journal_path = backup_path.with_suffix(backup_path.suffix + ".repair-journal.json")
        repair_journal = {
            "schema_version": 1,
            "status": "prepared",
            "thread_id": expected_thread_id,
            "source_path": str(source_path),
            "backup_path": str(backup_path),
            "original_sha256": original_sha256,
            "backup_sha256": backup_sha256,
            "original_bytes": view_result.source_bytes,
            "prepared_active_sha256": active_scan.source_sha256,
            "prepared_active_bytes": temporary_view.stat().st_size,
            "user_prompt_count": source_scan.user_prompt_count,
            "user_prompt_sha256": source_scan.user_prompt_sha256,
        }
        _write_json_fsync(journal_path, repair_journal)
        if precommit_guard is not None:
            precommit_guard()
        assert_source_unchanged()
        if before_replace is not None:
            before_replace(
                {
                    "thread_id": expected_thread_id,
                    "source_path": str(source_path),
                    "backup_path": str(backup_path),
                    "original_sha256": original_sha256,
                    "backup_sha256": backup_sha256,
                    "original_bytes": view_result.source_bytes,
                    "prepared_active_sha256": active_scan.source_sha256,
                    "prepared_active_bytes": temporary_view.stat().st_size,
                    "user_prompt_count": source_scan.user_prompt_count,
                    "user_prompt_sha256": source_scan.user_prompt_sha256,
                    "journal_path": str(journal_path),
                }
            )
        if precommit_guard is not None:
            precommit_guard()
        assert_source_unchanged()

        replacement_committed = False
        try:
            os.replace(temporary_view, source_path)
            replacement_committed = True
            if post_replace_hook is not None:
                post_replace_hook()
            active_sha256 = _sha256(source_path)
            repair_journal["status"] = "committed"
            repair_journal["active_sha256"] = active_sha256
            _write_json_fsync(journal_path, repair_journal)
        except Exception:
            if replacement_committed:
                restore_path = source_path.with_name(f".{source_path.name}.{uuid4().hex}.restoring")
                if precommit_guard is not None:
                    precommit_guard()
                shutil.copy2(backup_path, restore_path)
                if precommit_guard is not None:
                    precommit_guard()
                os.replace(restore_path, source_path)
            raise

        return InPlaceRepairResult(
            thread_id=expected_thread_id,
            source_path=str(source_path),
            backup_path=str(backup_path),
            original_bytes=view_result.source_bytes,
            active_bytes=source_path.stat().st_size,
            original_sha256=original_sha256,
            backup_sha256=backup_sha256,
            active_sha256=active_sha256,
            source_line_count=view_result.source_line_count,
            active_line_count=active_scan.line_count,
            latest_compacted_line=view_result.latest_compacted_line,
            journal_path=str(journal_path),
            user_prompt_count=source_scan.user_prompt_count,
            user_prompt_sha256=source_scan.user_prompt_sha256,
        )
    except Exception as error:
        if temporary_view.is_file():
            aborted_root = backup_root / "_aborted_views"
            aborted_root.mkdir(parents=True, exist_ok=True)
            aborted_path = aborted_root / temporary_view.name
            try:
                os.replace(temporary_view, aborted_path)
            except Exception as archive_error:
                raise RuntimeError(
                    f"repair failed and the generated view could not be archived: {archive_error}"
                ) from error
        raise


def repair_rollout_in_place(
    source_path: Path,
    backup_root: Path,
    expected_thread_id: str,
    audited_size: int,
    audited_mtime_ns: int,
    audited_sha256: str,
    precommit_guard: Callable[[], None] | None = None,
    before_replace: Callable[[dict[str, Any]], None] | None = None,
    post_replace_hook: Callable[[], None] | None = None,
) -> InPlaceRepairResult:
    return _repair_with_view_builder(
        source_path=source_path,
        backup_root=backup_root,
        expected_thread_id=expected_thread_id,
        audited_size=audited_size,
        audited_mtime_ns=audited_mtime_ns,
        audited_sha256=audited_sha256,
        view_builder=create_slim_view,
        precommit_guard=precommit_guard,
        before_replace=before_replace,
        post_replace_hook=post_replace_hook,
    )


def repair_rollout_compatibility_in_place(
    source_path: Path,
    backup_root: Path,
    expected_thread_id: str,
    audited_size: int,
    audited_mtime_ns: int,
    audited_sha256: str,
    precommit_guard: Callable[[], None] | None = None,
    before_replace: Callable[[dict[str, Any]], None] | None = None,
    post_replace_hook: Callable[[], None] | None = None,
) -> InPlaceRepairResult:
    return _repair_with_view_builder(
        source_path=source_path,
        backup_root=backup_root,
        expected_thread_id=expected_thread_id,
        audited_size=audited_size,
        audited_mtime_ns=audited_mtime_ns,
        audited_sha256=audited_sha256,
        view_builder=create_compatibility_view,
        precommit_guard=precommit_guard,
        before_replace=before_replace,
        post_replace_hook=post_replace_hook,
    )


def build_repair_plan(
    rows: Iterable[dict[str, Any]],
    thresholds: RepairThresholds | None = None,
    include_archived: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    effective_thresholds = thresholds or RepairThresholds()
    performance: list[dict[str, Any]] = []
    compatibility: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for row in rows:
        if bool(row.get("archived")) and not include_archived:
            continue
        scan = row.get("scan") or {}
        performance_risk = (
            int(scan.get("total_bytes") or 0) > effective_thresholds.max_active_bytes
            or int(scan.get("line_count") or 0) > effective_thresholds.max_active_lines
        )
        compatibility_risk = int(scan.get("estimated_current_parser_errors") or 0) > 0
        if not performance_risk and not compatibility_risk:
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
        elif performance_risk and not int(scan.get("latest_compacted_line") or 0):
            blocked_reason = "missing_compacted_checkpoint"
        elif performance_risk and not bool(scan.get("latest_compacted_checkpoint_valid")):
            blocked_reason = str(scan.get("latest_compacted_checkpoint_reason") or "invalid_compacted_checkpoint")
        elif performance_risk and int(scan.get("tail_line_count") or 0) > effective_thresholds.max_tail_lines:
            blocked_reason = "tail_after_checkpoint_too_large"

        enriched_row = dict(row)
        if blocked_reason:
            enriched_row["repair_block_reason"] = blocked_reason
            blocked.append(enriched_row)
        elif performance_risk:
            performance.append(enriched_row)
        else:
            compatibility.append(enriched_row)

    return performance, compatibility, blocked


def select_repair_candidates(
    rows: Iterable[dict[str, Any]],
    thresholds: RepairThresholds | None = None,
    include_archived: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    effective_thresholds = thresholds or RepairThresholds()
    selected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for row in rows:
        if bool(row.get("archived")) and not include_archived:
            continue
        scan = row.get("scan") or {}
        risky = (
            int(scan.get("total_bytes") or 0) > effective_thresholds.max_active_bytes
            or int(scan.get("line_count") or 0) > effective_thresholds.max_active_lines
            or int(scan.get("estimated_current_parser_errors") or 0) > 0
        )
        if not risky:
            continue
        blocked_reason = ""
        if int(scan.get("parse_errors") or 0):
            blocked_reason = "source_parse_errors"
        elif not int(scan.get("latest_compacted_line") or 0):
            blocked_reason = "missing_compacted_checkpoint"
        elif not bool(scan.get("latest_compacted_checkpoint_valid")):
            blocked_reason = str(scan.get("latest_compacted_checkpoint_reason") or "invalid_compacted_checkpoint")
        elif int(scan.get("tail_line_count") or 0) > effective_thresholds.max_tail_lines:
            blocked_reason = "tail_after_checkpoint_too_large"

        enriched_row = dict(row)
        if blocked_reason:
            enriched_row["repair_block_reason"] = blocked_reason
            blocked.append(enriched_row)
        else:
            selected.append(enriched_row)

    return selected, blocked
