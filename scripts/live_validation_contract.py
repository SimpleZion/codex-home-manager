from __future__ import annotations

import binascii
import hashlib
import json
import re
import struct
import zlib
from typing import Any


png_signature = b"\x89PNG\r\n\x1a\n"


def _text_output_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, dict):
        return []
    if value.get("type") == "mcp_tool_call_end":
        mcp_result = value.get("result")
        ok_result = mcp_result.get("Ok") if isinstance(mcp_result, dict) else None
        if not isinstance(ok_result, dict) or ok_result.get("isError") is True:
            return []
        content = ok_result.get("content")
        return [
            item["text"]
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ] if isinstance(content, list) else []
    output = value.get("output")
    if isinstance(output, str):
        return [output]
    if isinstance(output, dict):
        return [json.dumps(output, ensure_ascii=False, separators=(",", ":"))]
    if isinstance(output, list):
        return [
            item["text"]
            for item in output
            if isinstance(item, dict)
            and item.get("type") in {"text", "input_text", "output_text"}
            and isinstance(item.get("text"), str)
        ]
    return []


def exact_challenge_envelope(value: Any, challenge: str, check_id: str) -> dict[str, Any]:
    parsed_candidates: list[dict[str, Any]] = []
    for raw_output in _text_output_candidates(value):
        try:
            candidate = json.loads(raw_output)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed_candidates.append(candidate)
    matching = [
        candidate
        for candidate in parsed_candidates
        if set(candidate) == {"challenge", "check", "status", "result"}
        and candidate.get("challenge") == challenge
        and candidate.get("check") == check_id
        and candidate.get("status") == "pass"
    ]
    if len(matching) != 1:
        raise RuntimeError(f"tool output has no unique exact challenge envelope for {check_id}")
    envelope = matching[0]
    if not isinstance(envelope, dict) or set(envelope) != {"challenge", "check", "status", "result"}:
        raise RuntimeError(f"tool output envelope fields are not exact for {check_id}")
    if envelope["challenge"] != challenge or envelope["check"] != check_id or envelope["status"] != "pass":
        raise RuntimeError(f"tool output envelope does not match the challenge for {check_id}")
    result = envelope["result"]
    if not isinstance(result, dict):
        raise RuntimeError(f"tool output result is not an object for {check_id}")
    return result


def exact_call_arguments(call_payload: dict[str, Any], challenge: str, check_id: str) -> dict[str, Any]:
    tool_name = str(call_payload.get("name") or "")
    wrapper_code = call_payload.get("input")
    if isinstance(wrapper_code, str) and wrapper_code.strip():
        if tool_name.casefold() != "exec":
            raise RuntimeError(f"wrapper tool call is not functions.exec for {check_id}")
        if check_id == "official_thread_tools.live_tool":
            required_tools = {
                "codex_app__list_threads",
                "codex_app__read_thread",
                "codex_app__send_message_to_thread",
            }
        else:
            required_tools = {"mcp__node_repl__js"}
        executable = _javascript_without_literals_and_comments(wrapper_code)
        missing_tools = sorted(
            tool_name
            for tool_name in required_tools
            if not re.search(rf"\bawait\s+tools\.{re.escape(tool_name)}\s*\(", executable)
        )
        if missing_tools:
            raise RuntimeError(
                f"wrapper tool call does not await required tools for {check_id}: {', '.join(missing_tools)}"
            )
        if challenge not in wrapper_code or check_id not in wrapper_code:
            raise RuntimeError(f"wrapper tool call is not challenge-bound for {check_id}")
        _reject_dead_probe_branches(wrapper_code, check_id)
        return {"wrapper_code": wrapper_code, "nested_tools": sorted(required_tools)}

    raw_arguments = call_payload.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"tool call arguments are invalid JSON for {check_id}") from error
    elif isinstance(raw_arguments, dict):
        arguments = raw_arguments
    else:
        raise RuntimeError(f"tool call arguments are missing for {check_id}")
    if "challenge" in arguments or "check" in arguments:
        raise RuntimeError(f"tool call arguments use fields outside the real tool schema for {check_id}")
    if check_id == "official_thread_tools.live_tool":
        allowed_keys = {"threadId", "hostId", "prompt", "model", "thinking"}
        marker_text = arguments.get("prompt")
    else:
        allowed_keys = {"code", "timeout_ms", "title"}
        marker_text = arguments.get("code")
    if not set(arguments).issubset(allowed_keys):
        raise RuntimeError(f"tool call arguments contain unsupported fields for {check_id}")
    if not isinstance(marker_text, str) or challenge not in marker_text or check_id not in marker_text:
        raise RuntimeError(f"tool call arguments do not contain the challenge for {check_id}")
    return arguments


def exact_official_send_result(value: Any, target_thread_id: str) -> dict[str, Any]:
    parsed_candidates: list[dict[str, Any]] = []
    for raw_output in _text_output_candidates(value):
        try:
            candidate = json.loads(raw_output)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed_candidates.append(candidate)
    matching = [candidate for candidate in parsed_candidates if candidate.get("threadId") == target_thread_id]
    if len(matching) != 1:
        raise RuntimeError("official send output does not uniquely identify the target thread")
    return matching[0]


def exact_nested_mcp_event(
    payload: dict[str, Any],
    challenge: str,
    check_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if payload.get("type") != "mcp_tool_call_end":
        raise RuntimeError(f"nested event is not an MCP completion for {check_id}")
    invocation = payload.get("invocation")
    if not isinstance(invocation, dict) or invocation.get("server") != "node_repl" or invocation.get("tool") != "js":
        raise RuntimeError(f"nested MCP event is not node_repl/js for {check_id}")
    arguments = exact_call_arguments(
        {"name": "mcp__node_repl__js", "arguments": invocation.get("arguments")},
        challenge,
        check_id,
    )
    return arguments, exact_challenge_envelope(payload, challenge, check_id)


def _javascript_without_literals_and_comments(source: str) -> str:
    without_comments = []
    index = 0
    quote: str | None = None
    while index < len(source):
        character = source[index]
        next_character = source[index + 1] if index + 1 < len(source) else ""
        if quote:
            if character == "\\":
                without_comments.extend("  ")
                index += 2
                continue
            if character == quote:
                quote = None
            without_comments.append(" ")
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
            without_comments.append(" ")
            index += 1
            continue
        if character == "/" and next_character == "/":
            end = source.find("\n", index + 2)
            if end < 0:
                break
            without_comments.extend(" " * (end - index))
            index = end
            continue
        if character == "/" and next_character == "*":
            end = source.find("*/", index + 2)
            if end < 0:
                break
            without_comments.extend(" " * (end + 2 - index))
            index = end + 2
            continue
        without_comments.append(character)
        index += 1
    return "".join(without_comments)


def _brace_depths(source: str) -> list[int]:
    depths: list[int] = [0] * (len(source) + 1)
    depth = 0
    for index, character in enumerate(source):
        depths[index] = depth
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)
    depths[len(source)] = depth
    return depths


def _top_level_awaited_method_calls(source: str, object_name: str) -> dict[str, int]:
    executable = _javascript_without_literals_and_comments(source)
    depths = _brace_depths(executable)
    pattern = re.compile(
        rf"\bawait\s+{re.escape(object_name)}\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
    )
    return {
        match.group(1): match.start()
        for match in pattern.finditer(executable)
        if depths[match.start()] == 0
    }


def _top_level_match_positions(source: str, pattern: re.Pattern[str]) -> list[int]:
    executable = _javascript_without_literals_and_comments(source)
    depths = _brace_depths(executable)
    return [
        match.start()
        for match in pattern.finditer(executable)
        if depths[match.start()] == 0
    ]


def executable_sky_calls(source: str) -> set[str]:
    executable = _javascript_without_literals_and_comments(source)

    return set(re.findall(r"\bsky\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", executable))


def _probe_source(arguments: dict[str, Any], check_id: str) -> str:
    source = arguments.get("code")
    if not isinstance(source, str) or not source.strip():
        source = arguments.get("script")
    if not isinstance(source, str) or not source.strip():
        raise RuntimeError(f"live probe has no executable source for {check_id}")
    return source


def _imported_module_paths(source: str) -> list[str]:
    return [
        match.group(2).replace("\\", "/").casefold()
        for match in re.finditer(
            r"\bawait\s+import\s*\(\s*(['\"`])([^'\"`]+)\1\s*\)",
            source,
        )
    ]


def _reject_dead_probe_branches(source: str, check_id: str) -> None:
    executable = _javascript_without_literals_and_comments(source)
    if re.search(r"\b(?:if|while)\s*\(\s*(?:false|0|null|undefined)\s*\)", executable, re.IGNORECASE):
        raise RuntimeError(f"live probe contains a statically dead branch for {check_id}")


def validate_browser_probe_arguments(arguments: dict[str, Any], check_id: str) -> dict[str, Any]:
    if check_id not in {"browser.live_tool", "chrome.live_tool"}:
        raise RuntimeError(f"unsupported browser probe: {check_id}")
    source = _probe_source(arguments, check_id)
    _reject_dead_probe_branches(source, check_id)
    plugin_name = "browser" if check_id == "browser.live_tool" else "chrome"
    backend_name = "iab" if check_id == "browser.live_tool" else "extension"
    imported_paths = _imported_module_paths(source)
    expected_segment = f"/openai-bundled/{plugin_name}/"
    if not any(expected_segment in path and path.endswith("/scripts/browser-client.mjs") for path in imported_paths):
        raise RuntimeError(f"{plugin_name} probe did not import its bundled browser-client.mjs")
    executable = _javascript_without_literals_and_comments(source)
    if not re.search(r"\bawait\s+setupBrowserRuntime\s*\(", executable):
        raise RuntimeError(f"{plugin_name} probe did not initialize the browser runtime")
    backend_pattern = re.compile(
        rf"\bawait\s+agent\.browsers\.get\s*\(\s*(['\"]){re.escape(backend_name)}\1\s*\)"
    )
    if not backend_pattern.search(source):
        raise RuntimeError(f"{plugin_name} probe did not select the {backend_name} backend")
    tab_query_positions = _top_level_match_positions(
        source,
        re.compile(r"\bawait\s+[A-Za-z_$][A-Za-z0-9_$]*\.(?:tabs\.list|user\.openTabs)\s*\("),
    )
    if not tab_query_positions:
        raise RuntimeError(f"{plugin_name} probe did not perform a live tab query")
    write_positions = _top_level_match_positions(source, re.compile(r"\bnodeRepl\.write\s*\("))
    if not write_positions or max(write_positions) <= max(tab_query_positions):
        raise RuntimeError(f"{plugin_name} probe did not return a direct result")
    return {
        "backend": backend_name,
        "plugin": plugin_name,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


def validate_ui_probe_arguments(arguments: dict[str, Any], check_id: str) -> dict[str, Any]:
    script = _probe_source(arguments, check_id)
    _reject_dead_probe_branches(script, check_id)
    imported_paths = _imported_module_paths(script)
    if not any(
        "/openai-bundled/computer-use/" in path and path.endswith("/scripts/computer-use-client.mjs")
        for path in imported_paths
    ):
        raise RuntimeError(f"Windows UI probe did not import the bundled Computer Use client for {check_id}")
    executable = _javascript_without_literals_and_comments(script)
    if not re.search(r"\bawait\s+setupComputerUseRuntime\s*\(", executable):
        raise RuntimeError(f"Windows UI probe did not initialize Computer Use for {check_id}")
    required_calls = {
        "computer_use.live_tool": {"list_windows", "get_window_state"},
        "sidebar.thread_visibility": {"list_windows", "get_window_state"},
        "large_thread.ui_responsiveness": {
            "list_windows",
            "get_window_state",
            "click",
            "scroll",
            "type_text",
            "press_key",
        },
    }[check_id]
    call_positions = _top_level_awaited_method_calls(script, "sky")
    calls = set(call_positions)
    missing = sorted(required_calls - calls)
    if missing:
        raise RuntimeError(f"Windows UI probe is missing top-level awaited calls for {check_id}: {', '.join(missing)}")
    if check_id == "large_thread.ui_responsiveness":
        search_exclusion = re.search(
            r"!\s*/[^/\n]*(?:search|filter|find|搜索|筛选|查找)[^/\n]*/[a-z]*\.test\s*\(",
            script,
            re.IGNORECASE,
        )
        enter_submission = re.search(
            r"\bawait\s+sky\.press_key\s*\([^)]*\bkey\s*:\s*(['\"])Enter\1",
            script,
            re.DOTALL,
        )
        if search_exclusion is None:
            raise RuntimeError("large-thread probe does not exclude sidebar search from the prompt composer")
        if enter_submission is None:
            raise RuntimeError("large-thread probe does not submit the prompt composer input")
        if "prompt_composer_line" not in script or "input_submission_marker" not in script:
            raise RuntimeError("large-thread probe does not return prompt composer submission evidence")
    write_positions = _top_level_match_positions(script, re.compile(r"\bnodeRepl\.write\s*\("))
    if not write_positions or max(write_positions) <= max(call_positions[name] for name in required_calls):
        raise RuntimeError(f"Windows UI probe did not return the observed result after native calls for {check_id}")
    return {
        "executable_sky_calls": sorted(calls),
        "source_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
    }


def inspect_png_screenshot(content: bytes, *, minimum_width: int = 640, minimum_height: int = 360) -> dict[str, int]:
    if not content.startswith(png_signature):
        raise RuntimeError("screenshot is not a PNG")
    offset = len(png_signature)
    ihdr: bytes | None = None
    idat_parts: list[bytes] = []
    while offset < len(content):
        if offset + 12 > len(content):
            raise RuntimeError("PNG chunk is truncated")
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        chunk_type = content[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(content):
            raise RuntimeError("PNG chunk data is truncated")
        chunk_data = content[data_start:data_end]
        expected_crc = struct.unpack(">I", content[data_end:crc_end])[0]
        actual_crc = binascii.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise RuntimeError("PNG chunk CRC mismatch")
        if chunk_type == b"IHDR":
            if ihdr is not None:
                raise RuntimeError("PNG has multiple IHDR chunks")
            ihdr = chunk_data
        elif chunk_type == b"IDAT":
            idat_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            break
        offset = crc_end
    if ihdr is None or len(ihdr) != 13 or not idat_parts:
        raise RuntimeError("PNG is missing required image chunks")
    width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", ihdr)
    if width < minimum_width or height < minimum_height:
        raise RuntimeError(f"PNG screenshot is too small: {width}x{height}")
    channels_by_color_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if bit_depth != 8 or color_type not in channels_by_color_type or compression != 0 or filtering != 0 or interlace != 0:
        raise RuntimeError("PNG screenshot uses an unsupported pixel format")
    bytes_per_pixel = channels_by_color_type[color_type]
    row_bytes = width * bytes_per_pixel
    try:
        decoded = zlib.decompress(b"".join(idat_parts))
    except zlib.error as error:
        raise RuntimeError("PNG image data cannot be decompressed") from error
    if len(decoded) != height * (row_bytes + 1):
        raise RuntimeError("PNG decoded size does not match IHDR")
    previous = bytearray(row_bytes)
    unique_pixels: set[bytes] = set()
    cursor = 0
    for _ in range(height):
        filter_type = decoded[cursor]
        cursor += 1
        raw = decoded[cursor : cursor + row_bytes]
        cursor += row_bytes
        reconstructed = bytearray(row_bytes)
        for index, raw_value in enumerate(raw):
            left = reconstructed[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            up = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                estimate = left + up - upper_left
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - upper_left))
                predictor = (left, up, upper_left)[distances.index(min(distances))]
            else:
                raise RuntimeError("PNG screenshot has an invalid row filter")
            reconstructed[index] = (raw_value + predictor) & 0xFF
        for pixel_start in range(0, row_bytes, bytes_per_pixel):
            unique_pixels.add(bytes(reconstructed[pixel_start : pixel_start + bytes_per_pixel]))
            if len(unique_pixels) >= 16:
                break
        previous = reconstructed
    if len(unique_pixels) < 16:
        raise RuntimeError("PNG screenshot is blank or lacks visible pixel diversity")
    return {"width": width, "height": height, "unique_pixel_floor": len(unique_pixels)}
