from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "scripts"))

from repair_manifest_chain import load_manifest_pair, write_manifest_pair


required_checks = (
    "browser.live_tool",
    "chrome.live_tool",
    "computer_use.live_tool",
    "node_repl.live_tool",
    "official_thread_tools.live_tool",
    "sidebar.thread_visibility",
    "large_thread.ui_responsiveness",
)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> bytes:
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    temporary_path = path.with_suffix(path.suffix + ".writing")
    with temporary_path.open("wb") as destination:
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary_path, path)
    return content


def javascript_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def rollout_prefix_baseline(thread_id: str, rollout_path: Path) -> dict[str, Any]:
    resolved_path = rollout_path.resolve()
    content = resolved_path.read_bytes()
    return {
        "thread_id": thread_id,
        "rollout_path": str(resolved_path),
        "prefix_bytes": len(content),
        "prefix_sha256": hashlib.sha256(content).hexdigest(),
        "record_count": len(content.splitlines()),
    }


def plugin_root(codex_home: Path, plugin_name: str) -> Path:
    plugin_base = codex_home / "plugins" / "cache" / "openai-bundled" / plugin_name
    latest = plugin_base / "latest"
    if latest.exists():
        candidate = latest.resolve()
        if (candidate / ".codex-plugin" / "plugin.json").is_file():
            return candidate
    candidates = sorted(
        (
            path
            for path in plugin_base.iterdir()
            if path.is_dir() and path.name != "latest" and (path / ".codex-plugin" / "plugin.json").is_file()
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    ) if plugin_base.is_dir() else []
    if not candidates:
        raise RuntimeError(f"bundled plugin has no complete version directory: {plugin_name}")
    return candidates[0].resolve()


def read_threads(codex_home: Path) -> dict[str, dict[str, Any]]:
    database = sqlite3.connect(f"file:{(codex_home / 'state_5.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        return {
            str(thread_id): {
                "title": str(title or "").strip(),
                "rollout_path": str(rollout_path or ""),
                "archived": bool(archived),
                "agent_role": str(agent_role or ""),
                "has_user_event": bool(has_user_event),
            }
            for thread_id, title, rollout_path, archived, agent_role, has_user_event in database.execute(
                "select id, title, rollout_path, archived, agent_role, has_user_event from threads"
            )
        }
    finally:
        database.close()


def largest_main_thread_id(threads: dict[str, dict[str, Any]]) -> str:
    candidates: list[tuple[int, str]] = []
    for thread_id, row in threads.items():
        if row["archived"] or row["agent_role"] or not row["has_user_event"]:
            continue
        rollout_path = Path(row["rollout_path"])
        if rollout_path.is_file():
            candidates.append((rollout_path.stat().st_size, thread_id))
    if not candidates:
        raise RuntimeError("no visible main thread rollout is available for the large-thread probe")
    candidates.sort(reverse=True)
    size, thread_id = candidates[0]
    if size < 1_000_000:
        raise RuntimeError("the largest visible main thread rollout is smaller than 1 MB")
    return thread_id


def validate_large_thread_selection(
    threads: dict[str, dict[str, Any]],
    thread_id: str,
    slim_thread_ids: list[str],
) -> None:
    row = threads.get(thread_id)
    if row is None or not row["title"]:
        raise RuntimeError("large-thread probe target is not a titled registered thread")
    if slim_thread_ids:
        if thread_id not in slim_thread_ids:
            raise RuntimeError("large-thread probe target must be one of the prompt-preserving slim targets")
        return
    rollout_path = Path(row["rollout_path"])
    if row["archived"] or row["agent_role"] or not row["has_user_event"]:
        raise RuntimeError("large-thread probe target must be a visible main thread")
    if not rollout_path.is_file() or rollout_path.stat().st_size < 1_000_000:
        raise RuntimeError("large-thread probe target rollout is smaller than 1 MB")


def envelope_source(challenge: str, check_id: str, result_expression: str) -> str:
    return (
        "nodeRepl.write(JSON.stringify({"
        f"challenge:{javascript_string(challenge)},check:{javascript_string(check_id)},"
        f"status:\"pass\",result:{result_expression}"
        "}));"
    )


def browser_probe_source(plugin_path: Path, challenge: str, check_id: str, backend: str) -> str:
    client_path = (plugin_path / "scripts" / "browser-client.mjs").as_posix()
    return "\n".join(
        [
            "if (globalThis.agent?.browsers == null) {",
            f"  var {{ setupBrowserRuntime }} = await import({javascript_string(client_path)});",
            "  await setupBrowserRuntime({ globals: globalThis });",
            "}",
            f"var liveBrowser = await agent.browsers.get({javascript_string(backend)});",
            "var liveTabs = await liveBrowser.tabs.list();",
            envelope_source(
                challenge,
                check_id,
                f"{{browser_backend:{javascript_string(backend)},tab_count:liveTabs.length}}",
            ),
        ]
    )


def computer_use_setup_source(plugin_path: Path) -> list[str]:
    client_path = (plugin_path / "scripts" / "computer-use-client.mjs").as_posix()
    return [
        "if (!globalThis.sky) {",
        f"  var {{ setupComputerUseRuntime }} = await import({javascript_string(client_path)});",
        "  await setupComputerUseRuntime({ globals: globalThis });",
        "}",
    ]


def accessibility_expression(state_name: str) -> str:
    return (
        f"[{state_name}.accessibility?.tree,{state_name}.accessibility?.document_text,"
        f"{state_name}.accessibility?.focused_element,{state_name}.accessibility?.selected_text]"
        ".filter(value => typeof value === \"string\" && value).join(\"\\n\")"
    )


def computer_use_probe_source(plugin_path: Path, challenge: str) -> str:
    check_id = "computer_use.live_tool"
    lines = computer_use_setup_source(plugin_path)
    lines.extend(
        [
            "var liveWindows = await sky.list_windows();",
            "if (!liveWindows.length) throw new Error(\"No native windows are available\");",
            "var liveWindow = liveWindows.find(window => /Codex/i.test(window.title || \"\")) || liveWindows[0];",
            "var liveState = await sky.get_window_state({ window: liveWindow, include_screenshot: true, include_text: true });",
            "var liveScreenshot = liveState.screenshots[liveState.screenshots.length - 1];",
            "if (!liveScreenshot?.url) throw new Error(\"Native window screenshot is missing\");",
            envelope_source(
                challenge,
                check_id,
                "{window_count:liveWindows.length,window_title:liveWindow.title || liveWindow.app,"
                "screenshot_data_url:liveScreenshot.url}",
            ),
        ]
    )
    return "\n".join(lines)


def sidebar_probe_source(
    plugin_path: Path,
    challenge: str,
    expected_titles: list[str],
) -> str:
    check_id = "sidebar.thread_visibility"
    lines = computer_use_setup_source(plugin_path)
    lines.extend(
        [
            f"var sidebarExpectedTitles = {json.dumps(expected_titles, ensure_ascii=False)};",
            "var sidebarWindows = await sky.list_windows();",
            "var sidebarWindow = sidebarWindows.find(window => /Codex/i.test(window.title || \"\"));",
            "if (!sidebarWindow) throw new Error(\"Codex window is not available\");",
            "var sidebarState = await sky.get_window_state({ window: sidebarWindow, include_screenshot: true, include_text: true });",
            f"var sidebarAccessibilityText = {accessibility_expression('sidebarState')};",
            "var sidebarMissingTitles = sidebarExpectedTitles.filter(title => !sidebarAccessibilityText.toLocaleLowerCase().includes(title.toLocaleLowerCase()));",
            "if (sidebarMissingTitles.length) throw new Error(`Sidebar titles are missing: ${sidebarMissingTitles.join(\", \")}`);",
            "var sidebarScreenshot = sidebarState.screenshots[sidebarState.screenshots.length - 1];",
            "if (!sidebarScreenshot?.url) throw new Error(\"Sidebar screenshot is missing\");",
            envelope_source(
                challenge,
                check_id,
                "{accessibility_text:sidebarAccessibilityText,screenshot_data_url:sidebarScreenshot.url}",
            ),
        ]
    )
    return "\n".join(lines)


def large_thread_probe_source(
    plugin_path: Path,
    challenge: str,
    thread_id: str,
    thread_title: str,
    input_marker: str,
) -> str:
    check_id = "large_thread.ui_responsiveness"
    lines = computer_use_setup_source(plugin_path)
    lines.extend(
        [
            f"var largeThreadTitle = {javascript_string(thread_title)};",
            f"var largeInputMarker = {javascript_string(input_marker)};",
            "var largeWindows = await sky.list_windows();",
            "var largeWindow = largeWindows.find(window => /Codex/i.test(window.title || \"\"));",
            "if (!largeWindow) throw new Error(\"Codex window is not available\");",
            "var largeBeforeState = await sky.get_window_state({ window: largeWindow, include_screenshot: true, include_text: true });",
            f"var largeBeforeText = {accessibility_expression('largeBeforeState')};",
            "var largeTitleLine = largeBeforeText.split(\"\\n\").find(line => line.toLocaleLowerCase().includes(largeThreadTitle.toLocaleLowerCase()));",
            "if (!largeTitleLine) throw new Error(\"Large thread title is not visible in Codex\");",
            "var largeTitleIndexMatch = largeTitleLine.match(/\\[(\\d+)\\]/) || largeTitleLine.match(/index[=: ]+(\\d+)/i);",
            "if (!largeTitleIndexMatch) throw new Error(\"Large thread accessibility index is missing\");",
            "await sky.click({ window: largeWindow, element_index: Number(largeTitleIndexMatch[1]) });",
            "var largeOpenedState = await sky.get_window_state({ window: largeWindow, include_screenshot: true, include_text: true });",
            "var largeOpenedScreenshot = largeOpenedState.screenshots[largeOpenedState.screenshots.length - 1];",
            "var largeScrollX = Math.max(1, Math.floor((largeOpenedScreenshot?.width || 800) / 2));",
            "var largeScrollY = Math.max(1, Math.floor((largeOpenedScreenshot?.height || 600) / 2));",
            "await sky.scroll({ window: largeWindow, x: largeScrollX, y: largeScrollY, scrollX: 0, scrollY: 480, screenshotId: largeOpenedScreenshot?.id });",
            "var largeScrolledState = await sky.get_window_state({ window: largeWindow, include_screenshot: true, include_text: true });",
            f"var largeScrolledText = {accessibility_expression('largeScrolledState')};",
            "var largeComposerLines = largeScrolledText.split(\"\\n\").filter(line => "
            "/edit|textbox/i.test(line) && /message|prompt|ask|reply|send|输入|消息|提问|回复/i.test(line) && "
            "!/search|filter|find|搜索|筛选|查找/i.test(line) && "
            "(/\\[(\\d+)\\]/.test(line) || /index[=: ]+(\\d+)/i.test(line)));",
            "if (!largeComposerLines.length) throw new Error(\"Codex prompt composer is missing; sidebar search is not a valid input\");",
            "var largeEditorLine = largeComposerLines[0];",
            "var largeEditorIndexMatch = largeEditorLine.match(/\\[(\\d+)\\]/) || largeEditorLine.match(/index[=: ]+(\\d+)/i);",
            "await sky.click({ window: largeWindow, element_index: Number(largeEditorIndexMatch[1]) });",
            "await sky.type_text({ window: largeWindow, text: largeInputMarker });",
            "var largeTypedState = await sky.get_window_state({ window: largeWindow, include_screenshot: false, include_text: true });",
            f"var largeTypedText = {accessibility_expression('largeTypedState')};",
            "if (!largeTypedText.includes(largeInputMarker)) throw new Error(\"Codex input did not reflect typed text\");",
            "await sky.press_key({ window: largeWindow, key: \"Enter\" });",
            "await new Promise(resolve => setTimeout(resolve, 750));",
            "var largeAfterState = await sky.get_window_state({ window: largeWindow, include_screenshot: true, include_text: true });",
            f"var largeAfterText = {accessibility_expression('largeAfterState')};",
            "if (!largeAfterText.toLocaleLowerCase().includes(largeThreadTitle.toLocaleLowerCase())) throw new Error(\"Large thread title disappeared after interaction\");",
            "var largeAfterScreenshot = largeAfterState.screenshots[largeAfterState.screenshots.length - 1];",
            "if (!largeAfterScreenshot?.url) throw new Error(\"Large thread screenshot is missing\");",
            envelope_source(
                challenge,
                check_id,
                "{accessibility_before:largeBeforeText,accessibility_after:largeAfterText,"
                "prompt_composer_line:largeEditorLine,input_submission_marker:largeInputMarker,"
                "input_verified:true,screenshot_data_url:largeAfterScreenshot.url}",
            ),
        ]
    )
    return "\n".join(lines)


def node_probe_source(challenge: str) -> str:
    return envelope_source(challenge, "node_repl.live_tool", "{value:7}")


def exec_wrapper_source(source: str, check_id: str) -> str:
    return "\n".join(
        [
            f"const nested = await tools.mcp__node_repl__js({{title:{javascript_string('Validate ' + check_id)},code:{javascript_string(source)}}});",
            "for (const item of (nested?.content ?? [])) {",
            "  if (item.type === \"text\") text(item.text);",
            "  else if (item.type === \"image\") image(item);",
            "}",
        ]
    )


def official_probe_source(
    challenge: str,
    source_thread_id: str,
    target_thread_id: str,
    target_title: str,
    delegation_marker: str,
    target_response_marker: str,
) -> str:
    check_id = "official_thread_tools.live_tool"
    prompt = (
        "<codex_delegation>\n"
        f"<source_thread_id>{source_thread_id}</source_thread_id>\n"
        "<input>\n"
        f"Live validation marker: {delegation_marker}. Reply exactly with {target_response_marker} and do not perform any other action.\n"
        "</input>\n"
        "</codex_delegation>"
    )
    return "\n".join(
        [
            f"const listed = await tools.codex_app__list_threads({{limit:100,query:{javascript_string(target_title)}}});",
            f"const read = await tools.codex_app__read_thread({{threadId:{javascript_string(target_thread_id)},turnLimit:1}});",
            f"const sent = await tools.codex_app__send_message_to_thread({{threadId:{javascript_string(target_thread_id)},prompt:{javascript_string(prompt)}}});",
            "text(JSON.stringify({"
            f"challenge:{javascript_string(challenge)},check:{javascript_string(check_id)},status:\"pass\","
            f"result:{{target_thread_id:(sent?.threadId || {javascript_string(target_thread_id)}),"
            "list_completed:listed != null,read_completed:read != null}}"
            "}));",
        ]
    )


def build_request(
    codex_home: Path,
    manifest: dict[str, Any],
    source_thread_id: str,
    target_thread_id: str,
    sidebar_thread_ids: list[str],
    large_thread_id: str | None,
) -> dict[str, Any]:
    challenge = str(manifest.get("live_validation_challenge") or "")
    if len(challenge) != 32 or any(character not in "0123456789abcdef" for character in challenge):
        raise RuntimeError("repair manifest has no valid live validation challenge")
    threads = read_threads(codex_home)
    if source_thread_id not in threads or target_thread_id not in threads or source_thread_id == target_thread_id:
        raise RuntimeError("source and target must be distinct registered threads")
    if not threads[source_thread_id]["title"] or not threads[target_thread_id]["title"]:
        raise RuntimeError("source and target threads must have titles")
    raw_slim_thread_ids = manifest.get("prompt_preserving_slim_thread_ids") or []
    if not isinstance(raw_slim_thread_ids, list) or any(
        not isinstance(thread_id, str) or not thread_id.strip() for thread_id in raw_slim_thread_ids
    ):
        raise RuntimeError("repair manifest has an invalid prompt-preserving slim target list")
    slim_thread_ids = [thread_id.strip() for thread_id in raw_slim_thread_ids]
    if len(set(slim_thread_ids)) != len(slim_thread_ids):
        raise RuntimeError("repair manifest has duplicate prompt-preserving slim targets")
    selected_large_thread_id = large_thread_id or (slim_thread_ids[0] if slim_thread_ids else largest_main_thread_id(threads))
    validate_large_thread_selection(threads, selected_large_thread_id, slim_thread_ids)
    selected_sidebar_ids = list(dict.fromkeys(sidebar_thread_ids or [source_thread_id]))
    if any(thread_id not in threads or not threads[thread_id]["title"] for thread_id in selected_sidebar_ids):
        raise RuntimeError("sidebar probe targets must be titled registered threads")

    browser_root = plugin_root(codex_home, "browser")
    chrome_root = plugin_root(codex_home, "chrome")
    computer_use_root = plugin_root(codex_home, "computer-use")
    delegation_marker = f"codex-live-delegation-{challenge}"
    target_response_marker = f"codex-live-response-{challenge}"
    large_input_marker = f"codex-live-input-{challenge}"
    large_rollout_path = Path(str(threads[selected_large_thread_id]["rollout_path"] or ""))
    if not large_rollout_path.is_file():
        raise RuntimeError("large-thread rollout is missing before live validation")
    large_rollout_baseline = rollout_prefix_baseline(selected_large_thread_id, large_rollout_path)
    sources = {
        "browser.live_tool": browser_probe_source(browser_root, challenge, "browser.live_tool", "iab"),
        "chrome.live_tool": browser_probe_source(chrome_root, challenge, "chrome.live_tool", "extension"),
        "computer_use.live_tool": computer_use_probe_source(computer_use_root, challenge),
        "node_repl.live_tool": node_probe_source(challenge),
        "sidebar.thread_visibility": sidebar_probe_source(
            computer_use_root,
            challenge,
            [threads[thread_id]["title"] for thread_id in selected_sidebar_ids],
        ),
        "large_thread.ui_responsiveness": large_thread_probe_source(
            computer_use_root,
            challenge,
            selected_large_thread_id,
            threads[selected_large_thread_id]["title"],
            large_input_marker,
        ),
        "official_thread_tools.live_tool": official_probe_source(
            challenge,
            source_thread_id,
            target_thread_id,
            threads[target_thread_id]["title"],
            delegation_marker,
            target_response_marker,
        ),
    }
    probes: dict[str, dict[str, Any]] = {}
    for check_id in required_checks:
        source = sources[check_id]
        probes[check_id] = {
            "surface": "functions.exec"
            if check_id == "official_thread_tools.live_tool"
            else "functions.exec -> node_repl/js",
            "source": source,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "exec_source": source if check_id == "official_thread_tools.live_tool" else exec_wrapper_source(source, check_id),
        }
    return {
        "schema_version": 2,
        "challenge": challenge,
        "source_thread_id": source_thread_id,
        "target_thread_id": target_thread_id,
        "delegation_marker": delegation_marker,
        "target_response_marker": target_response_marker,
        "sidebar_thread_ids": selected_sidebar_ids,
        "large_thread_id": selected_large_thread_id,
        "large_thread_input_marker": large_input_marker,
        "large_thread_rollout_baseline": large_rollout_baseline,
        "probes": probes,
        "execution_order": list(required_checks),
        "generated_at_epoch": int(time.time()),
    }


def prepare(
    codex_home: Path,
    manifest_path: Path,
    output_path: Path,
    source_thread_id: str,
    target_thread_id: str,
    sidebar_thread_ids: list[str],
    large_thread_id: str | None,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest, manifest_sha256 = load_manifest_pair(manifest_path)
    if manifest.get("status") != "pending_live_ui_validation":
        raise RuntimeError("repair manifest is not pending live UI validation")
    run_root = manifest_path.parent.parent
    if Path(str(manifest.get("run_root") or "")).resolve() != run_root:
        raise RuntimeError("repair manifest run root does not match its transaction directory")
    if Path(str(manifest.get("codex_home") or "")).resolve() != codex_home.resolve():
        raise RuntimeError("repair manifest codex_home does not match the requested Codex home")
    output_path = output_path.resolve()
    if output_path.parent != run_root:
        raise RuntimeError("live validation request must be written directly inside the repair run")
    lock_path = run_root.parent / "active_repair.lock.json"
    if not lock_path.is_file():
        raise RuntimeError("active repair lock is missing")
    lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    if (
        str(lock.get("run_id") or "") != str(manifest.get("runner_run_id") or "")
        or Path(str(lock.get("run_root") or "")).resolve() != run_root
        or lock.get("status") != "pending_live_ui_validation"
        or str(lock.get("repair_manifest_sha256") or "").casefold() != manifest_sha256
    ):
        raise RuntimeError("active repair lock does not match the pending live validation run")
    request = build_request(
        codex_home,
        manifest,
        source_thread_id,
        target_thread_id,
        sidebar_thread_ids,
        large_thread_id,
    )
    request_bytes = write_json_atomic(output_path, request)
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    manifest.update(
        {
            "live_validation_request": str(output_path),
            "live_validation_request_sha256": request_sha256,
            "live_validation_source_thread_id": source_thread_id,
            "live_validation_target_thread_id": target_thread_id,
            "live_validation_request_prepared_at_epoch": int(time.time()),
        }
    )
    manifest_sha256 = write_manifest_pair(manifest_path, manifest)
    lock.update(
        {
            "repair_manifest_sha256": manifest_sha256,
            "live_validation_request": str(output_path),
            "live_validation_request_sha256": request_sha256,
            "updated_at_epoch": int(time.time()),
        }
    )
    write_json_atomic(lock_path, lock)
    return request


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare immutable Codex live validation probes.")
    parser.add_argument("--codex-home", type=Path, default=Path(r"D:\.codex"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-thread-id", required=True)
    parser.add_argument("--target-thread-id", required=True)
    parser.add_argument("--sidebar-thread-id", action="append", default=[])
    parser.add_argument("--large-thread-id")
    arguments = parser.parse_args()
    manifest_path = arguments.manifest.resolve()
    output_path = arguments.output or (manifest_path.parent.parent / "live_validation_request.json")
    request = prepare(
        arguments.codex_home.resolve(),
        manifest_path,
        output_path,
        arguments.source_thread_id,
        arguments.target_thread_id,
        arguments.sidebar_thread_id,
        arguments.large_thread_id,
    )
    print(
        json.dumps(
            {
                "status": "prepared",
                "output": str(Path(output_path).resolve()),
                "source_thread_id": request["source_thread_id"],
                "target_thread_id": request["target_thread_id"],
                "large_thread_id": request["large_thread_id"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
