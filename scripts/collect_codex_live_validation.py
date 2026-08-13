from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "scripts"))

from repair_manifest_chain import load_manifest_pair
from live_validation_contract import (
    exact_call_arguments,
    exact_challenge_envelope,
    exact_nested_mcp_event,
    inspect_png_screenshot,
    validate_browser_probe_arguments,
    validate_ui_probe_arguments,
)


required_checks = (
    "browser.live_tool",
    "chrome.live_tool",
    "computer_use.live_tool",
    "node_repl.live_tool",
    "official_thread_tools.live_tool",
    "sidebar.thread_visibility",
    "large_thread.ui_responsiveness",
)
required_methods = {
    "browser.live_tool": "direct_tool_call",
    "chrome.live_tool": "direct_tool_call",
    "computer_use.live_tool": "direct_tool_call",
    "node_repl.live_tool": "direct_tool_call",
    "official_thread_tools.live_tool": "visible_cross_thread_delivery",
    "sidebar.thread_visibility": "visual_and_state_crosscheck",
    "large_thread.ui_responsiveness": "timed_ui_interaction",
}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".writing")
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    with temporary_path.open("wb") as target:
        target.write(content)
        target.flush()
    temporary_path.replace(path)


def thread_rollout_path(codex_home: Path, thread_id: str) -> Path:
    database = sqlite3.connect(f"file:{(codex_home / 'state_5.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        row = database.execute("select rollout_path from threads where id = ?", (thread_id,)).fetchone()
    finally:
        database.close()
    if row is None:
        raise RuntimeError(f"thread is not registered in state_5.sqlite: {thread_id}")
    path = Path(str(row[0])).resolve()
    if not path.is_file():
        raise RuntimeError(f"thread rollout is missing: {path}")
    return path


def thread_rows(codex_home: Path, thread_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not thread_ids:
        return {}
    database = sqlite3.connect(f"file:{(codex_home / 'state_5.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        columns = {str(row[1]) for row in database.execute("pragma table_info(threads)")}
        if "title" not in columns:
            raise RuntimeError("threads table has no title column for UI evidence binding")
        placeholders = ",".join("?" for _ in thread_ids)
        rows = database.execute(
            f"select id, title, rollout_path from threads where id in ({placeholders})",
            thread_ids,
        ).fetchall()
    finally:
        database.close()
    result = {
        str(thread_id): {
            "title": str(title or "").strip(),
            "rollout_path": str(rollout_path or ""),
        }
        for thread_id, title, rollout_path in rows
    }
    missing = [thread_id for thread_id in thread_ids if thread_id not in result]
    if missing:
        raise RuntimeError(f"UI evidence thread ids are not registered: {', '.join(missing)}")
    untitled = [thread_id for thread_id in thread_ids if not result[thread_id]["title"]]
    if untitled:
        raise RuntimeError(f"UI evidence threads have no title: {', '.join(untitled)}")
    return result


def registered_thread_rollouts(codex_home: Path) -> dict[str, Path]:
    database = sqlite3.connect(f"file:{(codex_home / 'state_5.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        return {
            str(thread_id): Path(str(rollout_path)).resolve()
            for thread_id, rollout_path in database.execute(
                "select id, rollout_path from threads where rollout_path is not null and trim(rollout_path) <> ''"
            )
        }
    finally:
        database.close()


def user_prompt_text_parts(record: dict[str, Any]) -> list[str]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    record_type = str(record.get("type") or "")
    if record_type == "response_item" and payload.get("role") == "user":
        content = payload.get("content")
    elif record_type == "user_message":
        content = payload.get("content") if "content" in payload else payload.get("message")
    elif record_type == "event_msg" and payload.get("type") == "user_message":
        content = payload.get("message") if "message" in payload else payload.get("content")
    else:
        return []
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") in {"text", "input_text", "output_text"}
        and isinstance(item.get("text"), str)
    ]


def user_prompt_marker_lines(path: Path, marker: str, *, first_line: int = 1) -> list[int]:
    marker_bytes = marker.encode("utf-8")
    matches: list[int] = []
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if line_number < first_line or marker_bytes not in raw_line:
                continue
            try:
                record = json.loads(raw_line.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and marker in user_prompt_text_parts(record):
                matches.append(line_number)
    return matches


def validate_large_thread_input_binding(
    codex_home: Path,
    thread_id: str,
    marker: str,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    if str(baseline.get("thread_id") or "") != thread_id:
        raise RuntimeError("large-thread rollout baseline is bound to a different thread")
    registered_paths = registered_thread_rollouts(codex_home)
    target_path = registered_paths.get(thread_id)
    if target_path is None or not target_path.is_file():
        raise RuntimeError("large-thread target rollout is not registered or is missing")
    if Path(str(baseline.get("rollout_path") or "")).resolve() != target_path:
        raise RuntimeError("large-thread rollout baseline path no longer matches state_5.sqlite")
    prefix_bytes = int(baseline.get("prefix_bytes") or -1)
    record_count = int(baseline.get("record_count") or -1)
    prefix_sha256 = str(baseline.get("prefix_sha256") or "").casefold()
    if prefix_bytes < 0 or record_count < 0 or not re.fullmatch(r"[0-9a-f]{64}", prefix_sha256):
        raise RuntimeError("large-thread rollout baseline is malformed")
    with target_path.open("rb") as handle:
        current_prefix = handle.read(prefix_bytes)
    if len(current_prefix) != prefix_bytes or hashlib.sha256(current_prefix).hexdigest() != prefix_sha256:
        raise RuntimeError("large-thread target rollout no longer preserves the prepared prefix")
    target_matches = user_prompt_marker_lines(target_path, marker, first_line=record_count + 1)
    if len(target_matches) != 1:
        raise RuntimeError("large-thread input marker was not submitted exactly once to the target rollout")
    misbound_threads: list[str] = []
    checked_paths: set[str] = set()
    for registered_thread_id, rollout_path in registered_paths.items():
        path_key = str(rollout_path).casefold()
        if rollout_path == target_path or path_key in checked_paths or not rollout_path.is_file():
            continue
        checked_paths.add(path_key)
        if user_prompt_marker_lines(rollout_path, marker):
            misbound_threads.append(registered_thread_id)
    if misbound_threads:
        raise RuntimeError(
            "large-thread input marker was submitted to a non-target thread: " + ", ".join(sorted(misbound_threads))
        )
    return {
        "thread_id": thread_id,
        "rollout_path": str(target_path),
        "submitted_prompt_line": target_matches[0],
    }


def official_thread_tool_names(codex_home: Path, thread_id: str) -> list[str]:
    database = sqlite3.connect(f"file:{(codex_home / 'state_5.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        table_exists = database.execute(
            "select 1 from sqlite_master where type='table' and name='thread_dynamic_tools'"
        ).fetchone()
        if table_exists is None:
            raise RuntimeError("thread_dynamic_tools table is missing")
        return sorted(
            {
                str(name)
                for namespace, name in database.execute(
                    "select namespace, name from thread_dynamic_tools where thread_id = ?",
                    (thread_id,),
                )
                if str(namespace or "") == "codex_app" and str(name or "")
            }
        )
    finally:
        database.close()


def rollout_official_thread_tool_names(records: list[dict[str, Any]]) -> list[str]:
    names: set[str] = set()
    for item in records:
        if item.get("type") != "session_meta":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        dynamic_tools = payload.get("dynamic_tools")
        if not isinstance(dynamic_tools, list):
            continue
        for namespace in dynamic_tools:
            if not isinstance(namespace, dict) or namespace.get("name") != "codex_app":
                continue
            tools = namespace.get("tools")
            if not isinstance(tools, list):
                continue
            names.update(
                str(tool.get("name") or "")
                for tool in tools
                if isinstance(tool, dict) and str(tool.get("name") or "")
            )
    return sorted(names)


def event_duration_ms(payload: dict[str, Any]) -> float:
    duration = payload.get("duration")
    if not isinstance(duration, dict):
        raise RuntimeError("nested MCP event has no duration")
    seconds = duration.get("secs")
    nanoseconds = duration.get("nanos")
    if not isinstance(seconds, int) or not isinstance(nanoseconds, int):
        raise RuntimeError("nested MCP event duration is malformed")
    if seconds < 0 or nanoseconds < 0 or nanoseconds >= 1_000_000_000:
        raise RuntimeError("nested MCP event duration is out of range")
    return seconds * 1000 + nanoseconds / 1_000_000


def read_rollout(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    prefix_hashes: list[str] = []
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for line_number, raw_line in enumerate(source, 1):
            digest.update(raw_line)
            try:
                item = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeError(f"rollout is invalid JSON at {path}:{line_number}") from error
            if not isinstance(item, dict):
                raise RuntimeError(f"rollout item is not an object at {path}:{line_number}")
            records.append(item)
            prefix_hashes.append(digest.hexdigest())
    return records, prefix_hashes


def structured_output(value: Any, challenge: str, check_id: str) -> dict[str, Any]:
    return exact_challenge_envelope(value, challenge, check_id)


def collect(
    codex_home: Path,
    manifest_path: Path,
    request_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    manifest, _ = load_manifest_pair(manifest_path)
    run_root = manifest_path.resolve().parent.parent
    if output_path.resolve().parent != run_root:
        raise RuntimeError("collector output must be written directly inside the repair run")
    request_bytes = request_path.read_bytes()
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    request = json.loads(request_bytes.decode("utf-8-sig"))
    if request.get("schema_version") != 2:
        raise RuntimeError("live validation request schema is unsupported")
    if str(manifest.get("live_validation_request_sha256") or "") != request_sha256:
        raise RuntimeError("live validation request is not hash-bound to the repair manifest")
    challenge = str(manifest.get("live_validation_challenge") or "")
    if str(request.get("challenge") or "") != challenge:
        raise RuntimeError("live validation request challenge does not match the repair manifest")
    source_thread_id = str(request.get("source_thread_id") or "")
    target_thread_id = str(request.get("target_thread_id") or "")
    delegation_marker = str(request.get("delegation_marker") or "")
    target_response_marker = str(request.get("target_response_marker") or "")
    if (
        not source_thread_id
        or not target_thread_id
        or source_thread_id == target_thread_id
        or not delegation_marker
        or not target_response_marker
    ):
        raise RuntimeError("live validation request must identify distinct source/target threads and a delegation marker")
    probes = request.get("probes")
    if not isinstance(probes, dict) or set(probes) != set(required_checks):
        raise RuntimeError("live validation request must bind one source probe for every required check")
    for check_id, probe in probes.items():
        if not isinstance(probe, dict):
            raise RuntimeError(f"live validation probe is malformed: {check_id}")
        source = probe.get("source")
        if not isinstance(source, str) or not source.strip():
            raise RuntimeError(f"live validation probe has no source: {check_id}")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != str(probe.get("source_sha256") or ""):
            raise RuntimeError(f"live validation probe source hash mismatch: {check_id}")
    sidebar_thread_ids = request.get("sidebar_thread_ids")
    if (
        not isinstance(sidebar_thread_ids, list)
        or not sidebar_thread_ids
        or any(not isinstance(thread_id, str) or not thread_id.strip() for thread_id in sidebar_thread_ids)
        or len(set(sidebar_thread_ids)) != len(sidebar_thread_ids)
    ):
        raise RuntimeError("live validation request has no unique sidebar thread ids")
    large_thread_id = str(request.get("large_thread_id") or "")
    if not large_thread_id:
        raise RuntimeError("live validation request has no large thread id")
    large_input_marker = str(request.get("large_thread_input_marker") or "")
    large_rollout_baseline = request.get("large_thread_rollout_baseline")
    if (
        not large_input_marker.startswith("codex-live-input-")
        or challenge not in large_input_marker
        or not isinstance(large_rollout_baseline, dict)
    ):
        raise RuntimeError("live validation request has no bound large-thread input marker and rollout baseline")
    requested_ui_ids = list(dict.fromkeys([*sidebar_thread_ids, large_thread_id]))
    ui_thread_rows = thread_rows(codex_home, requested_ui_ids)
    slim_thread_ids = [str(thread_id) for thread_id in manifest.get("prompt_preserving_slim_thread_ids") or []]
    if slim_thread_ids and large_thread_id not in slim_thread_ids:
        raise RuntimeError("large-thread UI probe is not bound to a prompt-preserving slim target")
    if not slim_thread_ids:
        registered_paths = {
            thread_id: Path(row["rollout_path"])
            for thread_id, row in thread_rows(codex_home, requested_ui_ids).items()
        }
        if not registered_paths[large_thread_id].is_file() or registered_paths[large_thread_id].stat().st_size < 1_000_000:
            raise RuntimeError("large-thread UI probe is not bound to a rollout of at least 1 MB")
    restart_report_path = Path(str(manifest.get("restart_validation_report") or ""))
    restart_report_bytes = restart_report_path.read_bytes()
    restart_report = json.loads(restart_report_bytes.decode("utf-8-sig"))
    source_path = thread_rollout_path(codex_home, source_thread_id)
    target_path = thread_rollout_path(codex_home, target_thread_id)
    source_records, source_prefix_hashes = read_rollout(source_path)
    target_records, target_prefix_hashes = read_rollout(target_path)

    outputs: dict[str, tuple[int, dict[str, Any]]] = {}
    nested_mcp_events: list[tuple[int, dict[str, Any]]] = []
    for line_number, item in enumerate(source_records, 1):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if item.get("type") == "response_item" and payload.get("type") in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            outputs[str(payload.get("call_id") or "")] = (line_number, payload)
        if item.get("type") == "event_msg" and payload.get("type") == "mcp_tool_call_end":
            nested_mcp_events.append((line_number, payload))

    checks: list[dict[str, Any]] = []
    artifact_root = run_root / "live_validation_artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    for check_id in required_checks:
        selected: dict[str, Any] | None = None
        for line_number, item in enumerate(source_records, 1):
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item.get("type") != "response_item" or payload.get("type") not in {
                "function_call",
                "custom_tool_call",
            }:
                continue
            try:
                call_arguments = exact_call_arguments(payload, challenge, check_id)
            except RuntimeError:
                continue
            call_id = str(payload.get("call_id") or "")
            if call_id not in outputs:
                continue
            output_line, output_payload = outputs[call_id]
            if output_line <= line_number:
                continue
            expected_probe = probes[check_id]
            if check_id == "official_thread_tools.live_tool":
                wrapper_source = call_arguments.get("wrapper_code")
                if wrapper_source != expected_probe["source"]:
                    continue
                try:
                    result = structured_output(output_payload, challenge, check_id)
                except RuntimeError:
                    continue
                if str(result.get("target_thread_id") or result.get("send_thread_id") or "") != target_thread_id:
                    continue
                selected = {
                    "call_line": line_number,
                    "call_payload": payload,
                    "output_line": output_line,
                    "output_payload": output_payload,
                    "result": result,
                    "call_arguments": call_arguments,
                    "nested_event_line": 0,
                    "nested_event_payload": None,
                }
                continue
            matching_events: list[tuple[int, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
            for event_line, event_payload in nested_mcp_events:
                if event_line <= line_number or event_line >= output_line:
                    continue
                try:
                    nested_arguments, result = exact_nested_mcp_event(event_payload, challenge, check_id)
                except RuntimeError:
                    continue
                if nested_arguments.get("code") != expected_probe["source"]:
                    continue
                matching_events.append((event_line, event_payload, nested_arguments, result))
            if len(matching_events) != 1:
                continue
            event_line, event_payload, nested_arguments, result = matching_events[0]
            selected = {
                "call_line": line_number,
                "call_payload": payload,
                "output_line": output_line,
                "output_payload": output_payload,
                "result": result,
                "call_arguments": nested_arguments,
                "nested_event_line": event_line,
                "nested_event_payload": event_payload,
            }
        if selected is None:
            raise RuntimeError(f"no completed challenge-bound tool call was found for {check_id}")
        call_line = int(selected["call_line"])
        call_payload = selected["call_payload"]
        output_line = int(selected["output_line"])
        output_payload = selected["output_payload"]
        nested_event_line = int(selected["nested_event_line"])
        nested_event_payload = selected["nested_event_payload"]
        result = selected["result"]
        call_id = str(call_payload.get("call_id") or "")
        result = dict(result)
        call_arguments = selected["call_arguments"]
        result.update(
            {
                "source_thread_id": source_thread_id,
                "source_rollout_path": str(source_path),
                "source_rollout_prefix_sha256": source_prefix_hashes[output_line - 1],
                "tool_call_id": call_id,
                "call_line": call_line,
                "output_line": output_line,
                "probe_source_sha256": probes[check_id]["source_sha256"],
            }
        )
        if nested_event_line:
            result.update(
                {
                    "nested_event_line": nested_event_line,
                    "nested_event_prefix_sha256": source_prefix_hashes[nested_event_line - 1],
                    "nested_tool_call_id": str(nested_event_payload.get("call_id") or ""),
                    "provenance_kind": "functions_exec_nested_mcp",
                }
            )
        if check_id in {"browser.live_tool", "chrome.live_tool", "computer_use.live_tool", "node_repl.live_tool"}:
            result["tool_name"] = "node_repl/js"
            result.setdefault("invocation_status", "completed")
            result.setdefault("observed_result", "challenge-bound nested MCP result")
        if check_id in {"browser.live_tool", "chrome.live_tool"}:
            result["browser_probe"] = validate_browser_probe_arguments(call_arguments, check_id)
        if check_id in {"computer_use.live_tool", "sidebar.thread_visibility", "large_thread.ui_responsiveness"}:
            result["ui_probe"] = validate_ui_probe_arguments(call_arguments, check_id)
            screenshot_data_url = result.get("screenshot_data_url")
            if not isinstance(screenshot_data_url, str) or not screenshot_data_url.startswith("data:image/png;base64,"):
                raise RuntimeError(f"Computer Use result has no PNG screenshot for {check_id}")
            if check_id == "computer_use.live_tool":
                if not isinstance(result.get("window_count"), int) or int(result["window_count"]) < 1:
                    raise RuntimeError("Computer Use result has no native windows")
                if not str(result.get("window_title") or "").strip():
                    raise RuntimeError("Computer Use result has no native window title")
            if check_id == "sidebar.thread_visibility":
                accessibility_text = str(result.pop("accessibility_text", ""))
                normalized_accessibility = accessibility_text.casefold()
                matched_titles = {
                    thread_id: ui_thread_rows[thread_id]["title"]
                    for thread_id in sidebar_thread_ids
                    if ui_thread_rows[thread_id]["title"].casefold() in normalized_accessibility
                }
                if set(matched_titles) != set(sidebar_thread_ids):
                    missing = sorted(set(sidebar_thread_ids) - set(matched_titles))
                    raise RuntimeError(
                        f"sidebar accessibility evidence does not contain the SQLite-bound thread titles: {', '.join(missing)}"
                    )
                result.update(
                    {
                        "visible_thread_ids": list(sidebar_thread_ids),
                        "state_thread_ids": list(sidebar_thread_ids),
                        "matched_thread_titles": matched_titles,
                        "accessibility_sha256": hashlib.sha256(accessibility_text.encode("utf-8")).hexdigest(),
                    }
                )
            if check_id == "large_thread.ui_responsiveness":
                expected_title = ui_thread_rows[large_thread_id]["title"]
                before_text = str(result.pop("accessibility_before", ""))
                after_text = str(result.pop("accessibility_after", ""))
                if expected_title.casefold() not in before_text.casefold() or expected_title.casefold() not in after_text.casefold():
                    raise RuntimeError("large-thread accessibility evidence does not contain the SQLite-bound title")
                prompt_composer_line = str(result.pop("prompt_composer_line", ""))
                result_marker = str(result.pop("input_submission_marker", ""))
                if (
                    not prompt_composer_line
                    or re.search(r"search|filter|find|搜索|筛选|查找", prompt_composer_line, re.IGNORECASE)
                    or not re.search(r"edit|textbox|message|prompt|ask|reply|send|输入|消息|提问|回复", prompt_composer_line, re.IGNORECASE)
                ):
                    raise RuntimeError("large-thread probe did not bind a non-search prompt composer")
                if result_marker != large_input_marker:
                    raise RuntimeError("large-thread probe returned a different input marker")
                target_binding = validate_large_thread_input_binding(
                    codex_home,
                    large_thread_id,
                    large_input_marker,
                    large_rollout_baseline,
                )
                result.update(
                    {
                        "thread_id": large_thread_id,
                        "thread_title": expected_title,
                        "thread_identity_provenance": "state_5.sqlite+target_rollout_user_prompt",
                        "target_rollout_path": target_binding["rollout_path"],
                        "submitted_prompt_line": target_binding["submitted_prompt_line"],
                        "input_submission_marker_sha256": hashlib.sha256(large_input_marker.encode("utf-8")).hexdigest(),
                        "prompt_composer_sha256": hashlib.sha256(prompt_composer_line.encode("utf-8")).hexdigest(),
                        "accessibility_before_sha256": hashlib.sha256(before_text.encode("utf-8")).hexdigest(),
                        "accessibility_after_sha256": hashlib.sha256(after_text.encode("utf-8")).hexdigest(),
                        "collector_measured_elapsed_ms": event_duration_ms(nested_event_payload),
                        "operations_verified": ["open", "scroll", "input", "submit", "screenshot"],
                    }
                )
        if check_id == "official_thread_tools.live_tool":
            target_line = 0
            target_response_line = 0
            for line_number, target_item in enumerate(target_records, 1):
                serialized_target = json.dumps(target_item, ensure_ascii=False)
                target_payload = target_item.get("payload") if isinstance(target_item.get("payload"), dict) else {}
                if (
                    target_item.get("type") == "response_item"
                    and target_payload.get("type") == "message"
                    and target_payload.get("role") == "user"
                    and delegation_marker in serialized_target
                    and source_thread_id in serialized_target
                    and "codex_delegation" in serialized_target
                ):
                    target_line = line_number
                if (
                    target_line
                    and line_number > target_line
                    and target_item.get("type") == "response_item"
                    and target_payload.get("type") == "message"
                    and target_payload.get("role") == "assistant"
                    and target_response_marker in serialized_target
                ):
                    target_response_line = line_number
            if not target_line:
                raise RuntimeError("delegation marker was not found in the target rollout")
            if not target_response_line:
                raise RuntimeError("target thread did not produce a challenge-bound assistant response")
            required_official_tools = {"list_threads", "read_thread", "send_message_to_thread"}
            source_database_tools = official_thread_tool_names(codex_home, source_thread_id)
            target_database_tools = official_thread_tool_names(codex_home, target_thread_id)
            if not required_official_tools.issubset(source_database_tools):
                raise RuntimeError("source thread is missing official codex_app thread tools in SQLite")
            if not required_official_tools.issubset(target_database_tools):
                raise RuntimeError("target thread is missing official codex_app thread tools in SQLite")
            source_rollout_tools = rollout_official_thread_tool_names(source_records)
            target_rollout_tools = rollout_official_thread_tool_names(target_records)
            if source_rollout_tools and not required_official_tools.issubset(source_rollout_tools):
                raise RuntimeError("source session metadata is missing official codex_app thread tools")
            if target_rollout_tools and not required_official_tools.issubset(target_rollout_tools):
                raise RuntimeError("target session metadata is missing official codex_app thread tools")
            result.update(
                {
                    "target_thread_id": target_thread_id,
                    "target_rollout_path": str(target_path),
                    "target_rollout_prefix_sha256": target_prefix_hashes[target_line - 1],
                    "target_response_prefix_sha256": target_prefix_hashes[target_response_line - 1],
                    "target_message_line": target_line,
                    "target_response_line": target_response_line,
                    "target_response_marker": target_response_marker,
                    "send_tool_call_id": call_id,
                    "delegation_marker": delegation_marker,
                    "message_visible_in_target": True,
                    "target_replied": True,
                    "source_official_tools": source_database_tools,
                    "target_official_tools": target_database_tools,
                    "source_session_meta_tools": source_rollout_tools,
                    "target_session_meta_tools": target_rollout_tools,
                    "tool_registry_source": "state_5.thread_dynamic_tools",
                }
            )
        transcript = {
            "check": check_id,
            "challenge": challenge,
            "source_thread_id": source_thread_id,
            "call_line": call_line,
            "output_line": output_line,
            "call": call_payload,
            "output": output_payload,
            "nested_event_line": nested_event_line or None,
            "nested_event": nested_event_payload,
        }
        artifact_path = artifact_root / f"{check_id.replace('.', '-')}.json"
        artifact_content = json.dumps(transcript, ensure_ascii=False, indent=2).encode("utf-8")
        artifact_path.write_bytes(artifact_content)
        artifacts = [
            {
                "path": str(artifact_path),
                "sha256": hashlib.sha256(artifact_content).hexdigest(),
                "bytes": len(artifact_content),
                "media_type": "application/json",
            }
        ]
        if check_id in {"computer_use.live_tool", "sidebar.thread_visibility", "large_thread.ui_responsiveness"}:
            data_url = str(result.pop("screenshot_data_url", ""))
            prefix = "data:image/png;base64,"
            if not data_url.startswith(prefix):
                raise RuntimeError(f"Computer Use result has no PNG screenshot for {check_id}")
            try:
                screenshot_bytes = base64.b64decode(data_url[len(prefix) :], validate=True)
            except ValueError as error:
                raise RuntimeError(f"Computer Use PNG screenshot is invalid for {check_id}") from error
            if not screenshot_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(f"Computer Use screenshot is not a PNG for {check_id}")
            screenshot_info = inspect_png_screenshot(screenshot_bytes)
            result["screenshot"] = screenshot_info
            result["screenshot_verified"] = True
            screenshot_path = artifact_root / f"{check_id.replace('.', '-')}.png"
            screenshot_path.write_bytes(screenshot_bytes)
            artifacts.append(
                {
                    "path": str(screenshot_path),
                    "sha256": hashlib.sha256(screenshot_bytes).hexdigest(),
                    "bytes": len(screenshot_bytes),
                    "media_type": "image/png",
                }
            )
        if check_id == "large_thread.ui_responsiveness":
            result.pop("measurements_ms", None)
        checks.append(
            {
                "id": check_id,
                "status": "pass",
                "evidence": {
                    "method": required_methods[check_id],
                    "started_at_epoch": int(restart_report["generated_at_epoch"]),
                    "completed_at_epoch": int(time.time()),
                    "result": result,
                    "artifacts": artifacts,
                },
            }
        )

    collector_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    evidence = {
        "schema_version": 2,
        "status": "pass",
        "run_id": str(manifest.get("runner_run_id") or ""),
        "codex_home": str(codex_home.resolve()),
        "restart_validation_report_sha256": hashlib.sha256(restart_report_bytes).hexdigest(),
        "live_validation_request": str(request_path.resolve()),
        "live_validation_request_sha256": request_sha256,
        "generated_at_epoch": int(time.time()),
        "collector": {
            "name": "collect_codex_live_validation",
            "version": 1,
            "path": str(Path(__file__).resolve()),
            "sha256": collector_sha256,
        },
        "checks": checks,
    }
    write_json_atomic(output_path, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Codex live validation evidence from real rollout events.")
    parser.add_argument("--codex-home", type=Path, default=Path(r"D:\.codex"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    evidence = collect(
        arguments.codex_home.resolve(),
        arguments.manifest.resolve(),
        arguments.request.resolve(),
        arguments.output.resolve(),
    )
    print(json.dumps({"status": evidence["status"], "output": str(arguments.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
