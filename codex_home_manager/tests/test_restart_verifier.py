from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import base64
import binascii
import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

import verify_codex_after_restart
import collect_codex_live_validation
import prepare_codex_live_validation
import repair_codex_chrome_native_hosts
from backend.thread_history_repair import scan_rollout
from live_validation_contract import (
    exact_call_arguments,
    inspect_png_screenshot,
    validate_browser_probe_arguments,
    validate_ui_probe_arguments,
)
from verify_codex_after_restart import (
    read_config,
    resolve_codex_cli,
    update_manifest,
    validate_plugin_registry,
    validate_node_runtime,
    validate_post_restart_audit_summary,
    validate_restart_prompt_contract,
    validate_windows_incompatible_plugin_registry,
)


def test_native_host_repair_rebuilds_both_files_in_every_target_and_keeps_backups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    mirror_root = tmp_path / "local-runtime"
    appx_resources = tmp_path / "OpenAI.Codex_1.2.3.4_x64_test" / "app" / "resources"
    appx_runtime = appx_resources / "cua_node" / "bin"
    dynamic_runtime = tmp_path / "dynamic" / "bin"
    module_root = dynamic_runtime / "node_modules"
    helper_relative = Path("@oai/sky/bin/windows/codex-computer-use.exe")
    file_pairs = {
        (appx_resources / "codex.exe", tmp_path / "dynamic-cli" / "codex.exe"): b"cli",
        (appx_runtime / "node.exe", dynamic_runtime / "node.exe"): b"node",
        (appx_runtime / "node_repl.exe", dynamic_runtime / "node_repl.exe"): b"node-repl",
        (appx_runtime / "node_modules" / helper_relative, module_root / helper_relative): b"helper",
    }
    for (appx_path, dynamic_path), content in file_pairs.items():
        for path in (appx_path, dynamic_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    chrome_root = codex_home / "plugins" / "cache" / "openai-bundled" / "chrome" / "latest"
    for path in (
        chrome_root / "scripts" / "browser-client.mjs",
        chrome_root / "extension-host" / "windows" / "x64" / "extension-host.exe",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"chrome")
    manifest_path = chrome_root / ".codex-plugin" / "plugin.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
    config_text = "\n".join(
        [
            "[mcp_servers.node_repl]",
            f'command = "{(dynamic_runtime / "node_repl.exe").as_posix()}"',
            "[mcp_servers.node_repl.env]",
            f'NODE_REPL_NODE_PATH = "{(dynamic_runtime / "node.exe").as_posix()}"',
            f'NODE_REPL_NODE_MODULE_DIRS = "{module_root.as_posix()}"',
            f'CODEX_CLI_PATH = "{(tmp_path / "dynamic-cli" / "codex.exe").as_posix()}"',
        ]
    )
    codex_home.mkdir(exist_ok=True)
    (codex_home / "config.toml").write_text(config_text, encoding="utf-8")
    for target_root in (codex_home, mirror_root):
        target_root.mkdir(exist_ok=True)
        for file_name in repair_codex_chrome_native_hosts.required_file_names:
            (target_root / file_name).write_text('{"stale": true}', encoding="utf-8")
    monkeypatch.setattr(repair_codex_chrome_native_hosts, "assert_codex_offline", lambda: None)

    result = repair_codex_chrome_native_hosts.repair_native_hosts(
        codex_home=codex_home,
        appx_resources=appx_resources,
        target_roots=[codex_home, mirror_root],
        backup_root=tmp_path / "backups",
    )

    assert result["status"] == "complete"
    assert len(result["targets"]) == 2
    assert all(target["scan"]["configurationComplete"] for target in result["targets"])
    assert sum(len(target["backups"]) for target in result["targets"]) == 4
    for target_root in (codex_home, mirror_root):
        assert len(json.loads((target_root / "chrome-native-hosts.json").read_text())["chromeNativeHosts"]) == 1
        assert len(json.loads((target_root / "chrome-native-hosts-v2.json").read_text())["entries"]) == 1


def test_native_host_repair_rollback_restores_both_original_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_root = tmp_path / "target"
    target_root.mkdir()
    originals = {
        file_name: json.dumps({"original": file_name})
        for file_name in repair_codex_chrome_native_hosts.required_file_names
    }
    for file_name, content in originals.items():
        (target_root / file_name).write_text(content, encoding="utf-8")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(repair_codex_chrome_native_hosts, "assert_codex_offline", lambda: None)
    repair_codex_chrome_native_hosts.backup_existing_files(target_root, backup_root)
    for file_name in originals:
        (target_root / file_name).write_text('{"replacement": true}', encoding="utf-8")

    repair_codex_chrome_native_hosts.rollback_target(target_root, backup_root)

    assert {
        file_name: (target_root / file_name).read_text(encoding="utf-8")
        for file_name in originals
    } == originals


def test_native_host_repair_rolls_back_every_target_when_later_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_roots = [tmp_path / "codex-home", tmp_path / "local-runtime"]
    originals: dict[Path, dict[str, str]] = {}
    for target_index, target_root in enumerate(target_roots):
        target_root.mkdir()
        originals[target_root] = {}
        for file_name in repair_codex_chrome_native_hosts.required_file_names:
            content = json.dumps({"target": target_index, "file": file_name})
            originals[target_root][file_name] = content
            (target_root / file_name).write_text(content, encoding="utf-8")

    payloads = {
        "chrome-native-hosts.json": {"schemaVersion": 1, "chromeNativeHosts": []},
        "chrome-native-hosts-v2.json": {"schemaVersion": 2, "entries": []},
    }
    validation_count = 0

    def fail_second_validation(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal validation_count
        validation_count += 1
        if validation_count == 2:
            raise RuntimeError("injected second-target validation failure")
        return {"configurationComplete": True}

    monkeypatch.setattr(repair_codex_chrome_native_hosts, "assert_codex_offline", lambda: None)
    monkeypatch.setattr(
        repair_codex_chrome_native_hosts,
        "expected_native_host_paths",
        lambda *_args, **_kwargs: ({"codexHome": str(target_roots[0])}, "1.2.3"),
    )
    monkeypatch.setattr(
        repair_codex_chrome_native_hosts,
        "build_payloads",
        lambda *_args, **_kwargs: payloads,
    )
    monkeypatch.setattr(repair_codex_chrome_native_hosts, "validate_target", fail_second_validation)

    with pytest.raises(RuntimeError, match="injected second-target validation failure"):
        repair_codex_chrome_native_hosts.repair_native_hosts(
            codex_home=target_roots[0],
            appx_resources=tmp_path / "OpenAI.Codex_1.2.3.4_x64_test" / "app" / "resources",
            target_roots=target_roots,
            backup_root=tmp_path / "backups",
        )

    assert validation_count == 2
    for target_root in target_roots:
        assert {
            file_name: (target_root / file_name).read_text(encoding="utf-8")
            for file_name in repair_codex_chrome_native_hosts.required_file_names
        } == originals[target_root]


def test_notify_boundary_continuity_requires_the_current_appx_app_server_pid() -> None:
    database_identity = {"path": "D:/logs_2.sqlite", "device": 1, "inode": 2}
    appx_root = "C:/Program Files/WindowsApps/OpenAI.Codex_current"
    baseline = {
        "schema_version": 2,
        "database_identity": database_identity,
        "max_id": 10,
        "desktop_root_pid": 100,
        "desktop_root_created_at_epoch": 1,
        "current_appx_install_path": appx_root,
        "app_servers": [
            {"pid": 200, "executablePath": f"{appx_root}/app/resources/codex.exe"},
            {"pid": 300, "executablePath": "D:/plugins/codex.exe"},
        ],
        "process_uuids": ["pid:200:main", "pid:300:plugin"],
    }
    current = {
        **baseline,
        "max_id": 11,
        "app_servers": [
            {"pid": 201, "executablePath": f"{appx_root}/app/resources/codex.exe"},
            {"pid": 300, "executablePath": "D:/plugins/codex.exe"},
        ],
        "process_uuids": ["pid:201:replacement", "pid:300:plugin"],
    }

    with pytest.raises(RuntimeError, match="current-AppX.*continuity"):
        verify_codex_after_restart._validate_notify_log_boundary_continuity(baseline, current)


def test_validate_node_runtime_accepts_desktop_privileged_contract_without_ws(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_path = tmp_path / "node.exe"
    node_path.write_bytes(b"node")
    module_root = tmp_path / "node_modules"
    computer_use_client = (
        module_root
        / "@oai"
        / "sky"
        / "dist"
        / "project"
        / "cua"
        / "sky_js"
        / "src"
        / "targets"
        / "windows"
        / "internal"
        / "computer_use_client_base.js"
    )
    computer_use_client.parent.mkdir(parents=True)
    computer_use_client.write_text("export {};", encoding="utf-8")
    (module_root / "playwright").mkdir()
    (module_root / "playwright-core").mkdir()

    monkeypatch.setattr(
        verify_codex_after_restart.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args, returncode=0, stdout="node-runtime-ok\n", stderr=""),
    )
    result = validate_node_runtime(
        {
            "mcp_servers": {
                "node_repl": {
                    "args": [],
                    "env": {
                        "NODE_REPL_NODE_PATH": str(node_path),
                        "NODE_REPL_NODE_MODULE_DIRS": str(module_root),
                        "SKY_CUA_NATIVE_PIPE": "1",
                        "SKY_CUA_NATIVE_PIPE_DIRECTORY": r"\\.\pipe\codex-computer-use-test",
                    },
                }
            }
        }
    )

    assert result["arguments"] == []
    assert result["module_roots"] == [str(module_root)]
    assert result["computer_use_client"] == str(computer_use_client)
    assert result["smoke"] == "node-runtime-ok"


def test_validate_node_runtime_rejects_disable_sandbox(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="stale --disable-sandbox"):
        validate_node_runtime({"mcp_servers": {"node_repl": {"args": ["--disable-sandbox"]}}})


def test_read_config_rejects_managed_node_repl_override(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.toml").write_text(
        """
[mcp_servers.node_repl]
command = "C:/desktop/node_repl.exe"
args = []

[mcp_servers.node_repl.env]
NODE_REPL_NODE_MODULE_DIRS = "C:/desktop/node_modules"
CODEX_CLI_PATH = "C:/desktop/codex.exe"
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "managed_config.toml").write_text(
        """
[mcp_servers.node_repl]
args = ["--disable-sandbox"]

[mcp_servers.node_repl.env]
NODE_REPL_NODE_MODULE_DIRS = "D:/Software/codex-node-runtime/node_modules"
CODEX_CLI_PATH = "D:/.codex/plugins/.plugin-appserver/codex.exe"
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="must not define mcp_servers.node_repl"):
        read_config(tmp_path)


def write_chained_repair_manifest(
    run_root: Path,
    payload: dict[str, object],
    *,
    backup_root: Path | None = None,
    lock_status: str | None = None,
) -> tuple[Path, str]:
    repair_data = run_root / "repair_data"
    repair_data.mkdir(parents=True, exist_ok=True)
    manifest_path = repair_data / "repair_manifest.json"
    payload = {**payload, "run_root": str(run_root)}
    manifest_sha256 = verify_codex_after_restart.write_manifest_pair(manifest_path, payload)
    if lock_status is not None:
        effective_backup_root = backup_root or run_root.parent
        lock_path = effective_backup_root / "active_repair.lock.json"
        repository = verify_codex_after_restart.workspace_root / "codex_home_manager"
        root_repository = verify_codex_after_restart.workspace_root
        source_path = repository / "backend" / "windows_paths.py"
        collector_source_path = root_repository / "scripts" / "collect_codex_live_validation.py"
        snapshot_path = run_root / "source_snapshot" / "codex_home_manager" / "backend" / "windows_paths.py"
        collector_snapshot_path = run_root / "source_snapshot" / "scripts" / "collect_codex_live_validation.py"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        collector_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, snapshot_path)
        shutil.copy2(collector_source_path, collector_snapshot_path)
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        repository_head = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        repository_status = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        root_repository_head = subprocess.run(
            ["git", "-C", str(root_repository), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        root_repository_status = subprocess.run(
            ["git", "-C", str(root_repository), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        binding_path = run_root / "SOURCE_BINDING.json"
        binding_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "workspace_root": str(verify_codex_after_restart.workspace_root),
                    "snapshot_root": str(run_root / "source_snapshot"),
                    "files": [
                        {
                            "path": str(source_path),
                            "repository": str(repository),
                            "relative_path": "backend/windows_paths.py",
                            "bytes": source_path.stat().st_size,
                            "sha256": source_sha256,
                            "snapshot_path": str(snapshot_path),
                            "snapshot_relative_path": "codex_home_manager/backend/windows_paths.py",
                        },
                        {
                            "path": str(collector_source_path),
                            "repository": str(root_repository),
                            "relative_path": "scripts/collect_codex_live_validation.py",
                            "bytes": collector_source_path.stat().st_size,
                            "sha256": hashlib.sha256(collector_source_path.read_bytes()).hexdigest(),
                            "snapshot_path": str(collector_snapshot_path),
                            "snapshot_relative_path": "scripts/collect_codex_live_validation.py",
                        },
                    ],
                    "repositories": [
                        {
                            "path": str(repository),
                            "head": repository_head,
                            "status": repository_status,
                            "status_sha256": hashlib.sha256(repository_status.encode("utf-8")).hexdigest(),
                        },
                        {
                            "path": str(root_repository),
                            "head": root_repository_head,
                            "status": root_repository_status,
                            "status_sha256": hashlib.sha256(root_repository_status.encode("utf-8")).hexdigest(),
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        lock_path.write_text(
            json.dumps(
                {
                    "run_id": payload["runner_run_id"],
                    "run_root": str(run_root),
                    "status": lock_status,
                    "repair_manifest_sha256": manifest_sha256,
                    "source_binding": str(binding_path),
                    "source_binding_sha256": hashlib.sha256(binding_path.read_bytes()).hexdigest(),
                    "source_snapshot_root": str(run_root / "source_snapshot"),
                }
            ),
            encoding="utf-8",
        )
    return manifest_path, manifest_sha256


def collector_metadata(run_root: Path) -> dict[str, object]:
    binding = json.loads((run_root / "SOURCE_BINDING.json").read_text(encoding="utf-8"))
    item = next(
        item
        for item in binding["files"]
        if item["relative_path"] == "scripts/collect_codex_live_validation.py"
    )
    return {
        "name": "collect_codex_live_validation",
        "version": 1,
        "path": item["snapshot_path"],
        "sha256": item["sha256"],
    }


def test_source_binding_contract_rejects_snapshot_drift(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    manifest_path, _ = write_chained_repair_manifest(
        run_root,
        {
            "schema_version": 1,
            "status": "pending_restart_validation",
            "runner_run_id": "run-a",
        },
        backup_root=tmp_path,
        lock_status="pending_restart_validation",
    )
    lock = json.loads((tmp_path / "active_repair.lock.json").read_text(encoding="utf-8"))
    binding = verify_codex_after_restart.validate_source_binding_contract(lock, run_root)
    Path(binding["files"][0]["snapshot_path"]).write_text("drift", encoding="utf-8")

    with pytest.raises(RuntimeError, match="snapshot binding hash mismatch"):
        verify_codex_after_restart.validate_source_binding_contract(lock, run_root)


def write_bound_artifact(run_root: Path, name: str, payload: dict[str, object]) -> dict[str, object]:
    artifact_path = run_root / "live_validation_artifacts" / name
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, sort_keys=True).encode("utf-8")
    artifact_path.write_bytes(content)
    return {
        "path": str(artifact_path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }


tiny_png_bytes = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def make_test_screenshot_png(width: int = 800, height: int = 450) -> bytes:
    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend((x % 251, y % 241, (x + y) % 239))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), level=6))
        + chunk(b"IEND", b"")
    )


test_png_bytes = make_test_screenshot_png()


def test_png_receipt_rejects_tiny_or_blank_screenshot() -> None:
    with pytest.raises(RuntimeError, match="too small"):
        inspect_png_screenshot(tiny_png_bytes)


def test_collector_rejects_nested_handwritten_challenge_envelope() -> None:
    with pytest.raises(RuntimeError, match="envelope"):
        collect_codex_live_validation.structured_output(
            {"output": {"nested": {"challenge": "a" * 32, "check": "browser.live_tool", "result": {}}}},
            "a" * 32,
            "browser.live_tool",
        )


def test_ui_probe_rejects_sky_names_that_only_appear_in_comments_or_strings() -> None:
    with pytest.raises(RuntimeError, match="missing top-level awaited calls"):
        validate_ui_probe_arguments(
            {
                "code": (
                    "if (!globalThis.sky) { const { setupComputerUseRuntime } = await import(\"D:/"
                    ".codex/plugins/cache/openai-bundled/computer-use/1/scripts/computer-use-client.mjs\"); "
                    "await setupComputerUseRuntime({ globals: globalThis }); } "
                    "// sky.getWindows(); sky.screenshot();\n"
                    "const fake = 'sky.getWindows() sky.screenshot()'; nodeRepl.write(fake);"
                )
            },
            "sidebar.thread_visibility",
        )


def test_ui_probe_rejects_native_calls_hidden_in_a_statically_dead_branch() -> None:
    with pytest.raises(RuntimeError, match="statically dead branch"):
        validate_ui_probe_arguments(
            {
                "code": (
                    "if (!globalThis.sky) { const { setupComputerUseRuntime } = await import(\"D:/"
                    ".codex/plugins/cache/openai-bundled/computer-use/1/scripts/computer-use-client.mjs\"); "
                    "await setupComputerUseRuntime({ globals: globalThis }); } "
                    "if (false) { await sky.getWindows(); await sky.screenshot({ windowId: 1 }); } "
                    "nodeRepl.write('forged');"
                )
            },
            "computer_use.live_tool",
        )


def test_browser_probe_requires_node_runtime_setup_and_the_requested_backend() -> None:
    browser_code = (
        "if (globalThis.agent?.browsers == null) { const { setupBrowserRuntime } = await import(\"D:/"
        ".codex/plugins/cache/openai-bundled/browser/1/scripts/browser-client.mjs\"); "
        "await setupBrowserRuntime({ globals: globalThis }); } "
        "var browserProbe = await agent.browsers.get(\"iab\"); "
        "var browserTabs = await browserProbe.tabs.list(); nodeRepl.write(browserTabs.length);"
    )
    assert validate_browser_probe_arguments({"code": browser_code}, "browser.live_tool")["backend"] == "iab"
    chrome_code = browser_code.replace("/browser/", "/chrome/")
    with pytest.raises(RuntimeError, match="extension backend"):
        validate_browser_probe_arguments({"code": chrome_code}, "chrome.live_tool")


def test_live_probes_reject_results_returned_only_from_nested_functions() -> None:
    browser_code = (
        "if (globalThis.agent?.browsers == null) { const { setupBrowserRuntime } = await import(\"D:/"
        ".codex/plugins/cache/openai-bundled/browser/1/scripts/browser-client.mjs\"); "
        "await setupBrowserRuntime({ globals: globalThis }); } "
        "var browserProbe = await agent.browsers.get(\"iab\"); "
        "var browserTabs = await browserProbe.tabs.list(); "
        "function forgeResult() { nodeRepl.write(browserTabs.length); }"
    )
    with pytest.raises(RuntimeError, match="direct result"):
        validate_browser_probe_arguments({"code": browser_code}, "browser.live_tool")

    ui_code = (
        "if (!globalThis.sky) { const { setupComputerUseRuntime } = await import(\"D:/"
        ".codex/plugins/cache/openai-bundled/computer-use/1/scripts/computer-use-client.mjs\"); "
        "await setupComputerUseRuntime({ globals: globalThis }); } "
        "var liveWindows = await sky.list_windows(); "
        "var liveState = await sky.get_window_state({ window: liveWindows[0], include_screenshot: true }); "
        "function forgeResult() { nodeRepl.write(liveState); }"
    )
    with pytest.raises(RuntimeError, match="did not return the observed result"):
        validate_ui_probe_arguments({"code": ui_code}, "computer_use.live_tool")


def test_large_thread_probe_rejects_sidebar_search_as_the_prompt_input() -> None:
    ui_code = (
        "if (!globalThis.sky) { const { setupComputerUseRuntime } = await import(\"D:/"
        ".codex/plugins/cache/openai-bundled/computer-use/1/scripts/computer-use-client.mjs\"); "
        "await setupComputerUseRuntime({ globals: globalThis }); } "
        "var windows = await sky.list_windows(); "
        "var state = await sky.get_window_state({ window: windows[0], include_screenshot: true, include_text: true }); "
        "await sky.click({ window: windows[0], element_index: 3 }); "
        "await sky.scroll({ window: windows[0], x: 20, y: 20, scrollY: 200 }); "
        "var searchLine = 'textbox Search threads [3]'; "
        "await sky.type_text({ window: windows[0], text: 'marker' }); "
        "await sky.press_key({ window: windows[0], key: 'Enter' }); "
        "nodeRepl.write(JSON.stringify({searchLine}));"
    )

    with pytest.raises(RuntimeError, match="search|composer"):
        validate_ui_probe_arguments({"code": ui_code}, "large_thread.ui_responsiveness")


def test_prepared_probe_sources_match_outer_exec_and_nested_runtime_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = "abababababababababababababababab"
    rollout_path = tmp_path / "rollout.jsonl"
    rollout_path.write_text("{}\n", encoding="utf-8")
    threads = {
        thread_id: {
            "title": title,
            "rollout_path": str(rollout_path),
            "archived": False,
            "agent_role": "",
            "has_user_event": True,
        }
        for thread_id, title in {
            "source-thread": "Source validation thread",
            "target-thread": "Target validation thread",
            "thread-a": "Sidebar validation thread",
            "large-thread-a": "Large validation thread",
        }.items()
    }
    monkeypatch.setattr(prepare_codex_live_validation, "read_threads", lambda _codex_home: threads)
    monkeypatch.setattr(
        prepare_codex_live_validation,
        "plugin_root",
        lambda _codex_home, plugin_name: Path(f"D:/.codex/plugins/cache/openai-bundled/{plugin_name}/1"),
    )

    request = prepare_codex_live_validation.build_request(
        tmp_path,
        {
            "live_validation_challenge": challenge,
            "prompt_preserving_slim_thread_ids": ["large-thread-a"],
        },
        "source-thread",
        "target-thread",
        ["thread-a"],
        None,
    )

    assert request["schema_version"] == 2
    assert request["large_thread_id"] == "large-thread-a"
    assert request["large_thread_input_marker"].startswith("codex-live-input-")
    assert request["large_thread_rollout_baseline"] == {
        "thread_id": "large-thread-a",
        "rollout_path": str(rollout_path.resolve()),
        "prefix_bytes": rollout_path.stat().st_size,
        "prefix_sha256": hashlib.sha256(rollout_path.read_bytes()).hexdigest(),
        "record_count": 1,
    }
    large_probe_source = request["probes"]["large_thread.ui_responsiveness"]["source"]
    assert request["large_thread_input_marker"] in large_probe_source
    assert "search|filter" in large_probe_source
    for check_id, probe in request["probes"].items():
        assert hashlib.sha256(probe["source"].encode("utf-8")).hexdigest() == probe["source_sha256"]
        outer = exact_call_arguments(
            {"name": "exec", "input": probe["exec_source"]},
            challenge,
            check_id,
        )
        if check_id == "official_thread_tools.live_tool":
            assert outer["nested_tools"] == [
                "codex_app__list_threads",
                "codex_app__read_thread",
                "codex_app__send_message_to_thread",
            ]
        elif check_id in {"browser.live_tool", "chrome.live_tool"}:
            validate_browser_probe_arguments({"code": probe["source"]}, check_id)
        elif check_id in {
            "computer_use.live_tool",
            "sidebar.thread_visibility",
            "large_thread.ui_responsiveness",
        }:
            validate_ui_probe_arguments({"code": probe["source"]}, check_id)


def test_prepare_rejects_mismatched_lock_before_writing_request(tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    run_root = backup_root / "run"
    manifest_path = run_root / "repair_data" / "repair_manifest.json"
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    manifest_sha256 = verify_codex_after_restart.write_manifest_pair(
        manifest_path,
        {
            "schema_version": 1,
            "status": "pending_live_ui_validation",
            "runner_run_id": "run-a",
            "run_root": str(run_root),
            "codex_home": str(codex_home),
            "live_validation_challenge": "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
        },
    )
    lock_path = backup_root / "active_repair.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "run_id": "run-a",
                "run_root": str(run_root),
                "status": "pending_live_ui_validation",
                "repair_manifest_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    output_path = run_root / "live_validation_request.json"

    with pytest.raises(RuntimeError, match="active repair lock does not match"):
        prepare_codex_live_validation.prepare(
            codex_home,
            manifest_path,
            output_path,
            "source-thread",
            "target-thread",
            [],
            None,
        )

    assert not output_path.exists()
    assert verify_codex_after_restart.load_manifest_pair(manifest_path)[1] == manifest_sha256
    assert not output_path.with_suffix(".json.writing").exists()


def write_bound_png_artifact(run_root: Path, name: str) -> dict[str, object]:
    artifact_path = run_root / "live_validation_artifacts" / name
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(test_png_bytes)
    return {
        "path": str(artifact_path),
        "sha256": hashlib.sha256(test_png_bytes).hexdigest(),
        "bytes": len(test_png_bytes),
        "media_type": "image/png",
    }


def write_plugin_snapshot_contract(run_root: Path, codex_home: Path, run_id: str) -> Path:
    snapshot_root = run_root / "plugin_state_snapshot"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    manifest_path = snapshot_root / "plugin_state_snapshot.json"
    manifest = {
        "schema_version": 1,
        "status": "complete",
        "runner_run_id": run_id,
        "run_root": str(run_root),
        "codex_home": str(codex_home),
        "snapshot_root": str(snapshot_root),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (snapshot_root / "plugin_state_snapshot.sha256.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runner_run_id": run_id,
                "run_root": str(run_root),
                "codex_home": str(codex_home),
                "manifest": str(manifest_path),
                "manifest_sha256": manifest_sha256,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def live_probe_sources(challenge: str, input_marker: str | None = None) -> dict[str, str]:
    computer_use_root = Path("D:/.codex/plugins/cache/openai-bundled/computer-use/1")
    large_input_marker = input_marker or f"codex-live-input-{challenge}"
    return {
        "browser.live_tool": prepare_codex_live_validation.browser_probe_source(
            Path("D:/.codex/plugins/cache/openai-bundled/browser/1"), challenge, "browser.live_tool", "iab"
        ),
        "chrome.live_tool": prepare_codex_live_validation.browser_probe_source(
            Path("D:/.codex/plugins/cache/openai-bundled/chrome/1"), challenge, "chrome.live_tool", "extension"
        ),
        "computer_use.live_tool": prepare_codex_live_validation.computer_use_probe_source(computer_use_root, challenge),
        "node_repl.live_tool": prepare_codex_live_validation.node_probe_source(challenge),
        "sidebar.thread_visibility": prepare_codex_live_validation.sidebar_probe_source(
            computer_use_root, challenge, ["Sidebar validation thread"]
        ),
        "large_thread.ui_responsiveness": prepare_codex_live_validation.large_thread_probe_source(
            computer_use_root,
            challenge,
            "large-thread-a",
            "Large validation thread",
            large_input_marker,
        ),
        "official_thread_tools.live_tool": prepare_codex_live_validation.official_probe_source(
            challenge,
            "source-thread",
            "target-thread",
            "Target validation thread",
            "delegation-receipt-001",
            "target-response-receipt-001",
        ),
    }


def write_test_live_validation_request(
    run_root: Path,
    challenge: str,
    codex_home: Path,
) -> tuple[Path, dict[str, object], str]:
    large_rollout = codex_home / "sessions" / "large.jsonl"
    large_rollout.parent.mkdir(parents=True, exist_ok=True)
    if not large_rollout.is_file():
        large_rollout.write_bytes(b'{"type":"event_msg","payload":{}}\n' * 32_000)
    large_baseline_bytes = large_rollout.read_bytes()
    input_marker = f"codex-live-input-{challenge}"
    sources = live_probe_sources(challenge, input_marker)
    probes: dict[str, dict[str, object]] = {}
    for check_id in verify_codex_after_restart.required_live_validation_checks:
        source = sources[check_id]
        probes[check_id] = {
            "surface": "functions.exec"
            if check_id == "official_thread_tools.live_tool"
            else "functions.exec -> node_repl/js",
            "source": source,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "exec_source": source
            if check_id == "official_thread_tools.live_tool"
            else prepare_codex_live_validation.exec_wrapper_source(source, check_id),
        }
    request: dict[str, object] = {
        "schema_version": 2,
        "challenge": challenge,
        "source_thread_id": "source-thread",
        "target_thread_id": "target-thread",
        "delegation_marker": "delegation-receipt-001",
        "target_response_marker": "target-response-receipt-001",
        "sidebar_thread_ids": ["thread-a"],
        "large_thread_id": "large-thread-a",
        "large_thread_input_marker": input_marker,
        "large_thread_rollout_baseline": {
            "thread_id": "large-thread-a",
            "rollout_path": str(large_rollout.resolve()),
            "prefix_bytes": len(large_baseline_bytes),
            "prefix_sha256": hashlib.sha256(large_baseline_bytes).hexdigest(),
            "record_count": 32_000,
        },
        "probes": probes,
        "execution_order": list(verify_codex_after_restart.required_live_validation_checks),
        "generated_at_epoch": 100,
    }
    request_path = run_root / "live_validation_request.json"
    request_bytes = json.dumps(request, ensure_ascii=False, indent=2).encode("utf-8")
    request_path.write_bytes(request_bytes)
    return request_path, request, hashlib.sha256(request_bytes).hexdigest()


def make_live_checks(
    run_root: Path,
    restart_epoch: int,
    codex_home: Path,
    challenge: str,
) -> list[dict[str, object]]:
    source_thread_id = "source-thread"
    target_thread_id = "target-thread"
    sidebar_thread_id = "thread-a"
    large_thread_id = "large-thread-a"
    sidebar_title = "Sidebar validation thread"
    large_thread_title = "Large validation thread"
    source_rollout = codex_home / "sessions" / "source.jsonl"
    target_rollout = codex_home / "sessions" / "target.jsonl"
    large_rollout = codex_home / "sessions" / "large.jsonl"
    source_rollout.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.fromtimestamp(restart_epoch + 10, tz=UTC).isoformat().replace("+00:00", "Z")
    official_tools = ["list_threads", "read_thread", "send_message_to_thread"]
    dynamic_tools = [{"name": "codex_app", "tools": [{"name": name} for name in official_tools]}]
    source_records: list[dict[str, object]] = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": source_thread_id, "dynamic_tools": dynamic_tools},
        }
    ]
    target_records: list[dict[str, object]] = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": target_thread_id, "dynamic_tools": dynamic_tools},
        }
    ]
    large_input_marker = f"codex-live-input-{challenge}"
    sources = live_probe_sources(challenge, large_input_marker)

    def direct_result(check_id: str) -> dict[str, object]:
        screenshot_data_url = "data:image/png;base64," + base64.b64encode(test_png_bytes).decode("ascii")
        if check_id == "browser.live_tool":
            return {"browser_backend": "iab", "tab_count": 1}
        if check_id == "chrome.live_tool":
            return {"browser_backend": "extension", "tab_count": 1}
        if check_id == "computer_use.live_tool":
            return {"window_count": 1, "window_title": "Codex", "screenshot_data_url": screenshot_data_url}
        if check_id == "node_repl.live_tool":
            return {"value": 7}
        if check_id == "sidebar.thread_visibility":
            return {
                "accessibility_text": f"[11] {sidebar_title}",
                "screenshot_data_url": screenshot_data_url,
            }
        if check_id == "large_thread.ui_responsiveness":
            return {
                "accessibility_before": f"[12] {large_thread_title}",
                "accessibility_after": f"[12] {large_thread_title}\n[20] Message Codex textbox",
                "prompt_composer_line": "[20] Message Codex textbox",
                "input_submission_marker": large_input_marker,
                "input_verified": True,
                "screenshot_data_url": screenshot_data_url,
            }
        return {"target_thread_id": target_thread_id, "list_completed": True, "read_completed": True}

    def append_call(check_id: str, index: int) -> dict[str, object]:
        call_id = f"call-live-{index:02d}"
        nested_call_id = f"nested-live-{index:02d}"
        source = sources[check_id]
        exec_source = (
            source
            if check_id == "official_thread_tools.live_tool"
            else prepare_codex_live_validation.exec_wrapper_source(source, check_id)
        )
        output_result = direct_result(check_id)
        envelope = {
            "challenge": challenge,
            "check": check_id,
            "status": "pass",
            "result": output_result,
        }
        source_records.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec",
                    "call_id": call_id,
                    "input": exec_source,
                },
            }
        )
        call_line = len(source_records)
        nested_event_line = 0
        if check_id != "official_thread_tools.live_tool":
            source_records.append(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {
                        "type": "mcp_tool_call_end",
                        "call_id": nested_call_id,
                        "invocation": {
                            "server": "node_repl",
                            "tool": "js",
                            "arguments": {"title": f"Validate {check_id}", "code": source},
                        },
                        "duration": {"secs": 0, "nanos": 500_000_000},
                        "result": {
                            "Ok": {
                                "content": [{"type": "text", "text": json.dumps(envelope)}],
                                "isError": False,
                            }
                        },
                    },
                }
            )
            nested_event_line = len(source_records)
        source_records.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(envelope),
                },
            }
        )
        return {
            "tool_call_id": call_id,
            "call_line": call_line,
            "output_line": len(source_records),
            "nested_event_line": nested_event_line,
            "nested_tool_call_id": nested_call_id,
            "probe_source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }

    checks: list[dict[str, object]] = []
    for index, check_id in enumerate(verify_codex_after_restart.required_live_validation_checks, 1):
        artifact = (
            write_bound_png_artifact(run_root, f"{index:02d}-{check_id.replace('.', '-')}.png")
            if check_id in {"computer_use.live_tool", "sidebar.thread_visibility", "large_thread.ui_responsiveness"}
            else write_bound_artifact(run_root, f"{index:02d}-{check_id.replace('.', '-')}.json", {"check": check_id})
        )
        provenance = append_call(check_id, index)
        if check_id in {"browser.live_tool", "chrome.live_tool", "computer_use.live_tool", "node_repl.live_tool"}:
            result: dict[str, object] = {
                **provenance,
                "tool_name": "node_repl/js",
                "invocation_status": "completed",
                "observed_result": "live receipt captured",
                "provenance_kind": "functions_exec_nested_mcp",
            }
            if check_id == "browser.live_tool":
                result.update({"browser_backend": "iab", "tab_count": 1})
            elif check_id == "chrome.live_tool":
                result.update({"browser_backend": "extension", "tab_count": 1})
            elif check_id == "computer_use.live_tool":
                result.update({"window_count": 1, "screenshot_verified": True})
        elif check_id == "official_thread_tools.live_tool":
            result = {
                **provenance,
                "source_thread_id": source_thread_id,
                "target_thread_id": target_thread_id,
                "send_tool_call_id": provenance["tool_call_id"],
                "delegation_marker": "delegation-receipt-001",
                "message_visible_in_target": True,
                "target_response_marker": "target-response-receipt-001",
                "target_replied": True,
                "source_official_tools": official_tools,
                "target_official_tools": official_tools,
                "tool_registry_source": "state_5.thread_dynamic_tools",
            }
            target_records.append(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"<codex_delegation><source_thread_id>{source_thread_id}</source_thread_id>delegation-receipt-001</codex_delegation>",
                            }
                        ],
                    },
                }
            )
            result["target_message_line"] = len(target_records)
            target_records.append(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "target-response-receipt-001"}],
                    },
                }
            )
            result["target_response_line"] = len(target_records)
        elif check_id == "sidebar.thread_visibility":
            accessibility_text = f"[11] {sidebar_title}"
            result = {
                **provenance,
                "provenance_kind": "functions_exec_nested_mcp",
                "visible_thread_ids": [sidebar_thread_id],
                "state_thread_ids": [sidebar_thread_id],
                "matched_thread_titles": {sidebar_thread_id: sidebar_title},
                "accessibility_sha256": hashlib.sha256(accessibility_text.encode("utf-8")).hexdigest(),
                "screenshot_verified": True,
            }
        else:
            before_text = f"[12] {large_thread_title}"
            after_text = f"[12] {large_thread_title}\n[20] Message Codex textbox"
            prompt_composer_line = "[20] Message Codex textbox"
            result = {
                **provenance,
                "provenance_kind": "functions_exec_nested_mcp",
                "thread_id": large_thread_id,
                "thread_title": large_thread_title,
                "thread_identity_provenance": "state_5.sqlite+target_rollout_user_prompt",
                "input_submission_marker_sha256": hashlib.sha256(large_input_marker.encode("utf-8")).hexdigest(),
                "submitted_prompt_line": 32_001,
                "prompt_composer_sha256": hashlib.sha256(prompt_composer_line.encode("utf-8")).hexdigest(),
                "accessibility_before_sha256": hashlib.sha256(before_text.encode("utf-8")).hexdigest(),
                "accessibility_after_sha256": hashlib.sha256(after_text.encode("utf-8")).hexdigest(),
                "collector_measured_elapsed_ms": 500,
                "operations_verified": ["open", "scroll", "input", "submit", "screenshot"],
                "screenshot_verified": True,
            }
        checks.append(
            {
                "id": check_id,
                "status": "pass",
                "evidence": {
                    "method": verify_codex_after_restart.required_live_validation_methods[check_id],
                    "started_at_epoch": restart_epoch + index,
                    "completed_at_epoch": restart_epoch + index + 1,
                    "result": result,
                    "artifacts": [artifact],
                },
            }
        )
    source_rollout.write_text("".join(json.dumps(item) + "\n" for item in source_records), encoding="utf-8")
    target_rollout.write_text("".join(json.dumps(item) + "\n" for item in target_records), encoding="utf-8")
    if not large_rollout.is_file():
        large_rollout.write_bytes(b'{"type":"event_msg","payload":{}}\n' * 32_000)
    with large_rollout.open("a", encoding="utf-8") as large_handle:
        large_handle.write(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": large_input_marker}],
                    },
                }
            )
            + "\n"
        )
    source_lines = source_rollout.read_bytes().splitlines(keepends=True)
    target_lines = target_rollout.read_bytes().splitlines(keepends=True)
    for check in checks:
        result = check["evidence"]["result"]
        output_line = int(result["output_line"])
        result["source_thread_id"] = source_thread_id
        result["source_rollout_path"] = str(source_rollout)
        result["source_rollout_prefix_sha256"] = hashlib.sha256(b"".join(source_lines[:output_line])).hexdigest()
        if check["id"] != "official_thread_tools.live_tool":
            nested_event_line = int(result["nested_event_line"])
            result["nested_event_prefix_sha256"] = hashlib.sha256(
                b"".join(source_lines[:nested_event_line])
            ).hexdigest()
        if check["id"] == "official_thread_tools.live_tool":
            target_line = int(result["target_message_line"])
            target_response_line = int(result["target_response_line"])
            result["target_rollout_path"] = str(target_rollout)
            result["target_rollout_prefix_sha256"] = hashlib.sha256(b"".join(target_lines[:target_line])).hexdigest()
            result["target_response_prefix_sha256"] = hashlib.sha256(
                b"".join(target_lines[:target_response_line])
            ).hexdigest()
        if check["id"] == "large_thread.ui_responsiveness":
            result["target_rollout_path"] = str(large_rollout)
    database = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        database.execute("create table threads(id text primary key, title text not null, rollout_path text not null)")
        database.executemany(
            "insert into threads values (?, ?, ?)",
            [
                (source_thread_id, "Source validation thread", str(source_rollout)),
                (target_thread_id, "Target validation thread", str(target_rollout)),
                (sidebar_thread_id, sidebar_title, str(source_rollout)),
                (large_thread_id, large_thread_title, str(large_rollout)),
            ],
        )
        database.execute("create table thread_dynamic_tools(thread_id text, namespace text, name text)")
        database.executemany(
            "insert into thread_dynamic_tools values (?, ?, ?)",
            [
                (thread_id, "codex_app", tool_name)
                for thread_id in (source_thread_id, target_thread_id)
                for tool_name in official_tools
            ],
        )
        database.commit()
    finally:
        database.close()
    return checks


def test_resolve_codex_cli_accepts_node_repl_environment_path(tmp_path: Path) -> None:
    codex_cli = tmp_path / "codex.exe"
    codex_cli.write_bytes(b"test")
    config = {
        "mcp_servers": {
            "node_repl": {
                "env": {"CODEX_CLI_PATH": str(codex_cli)},
            }
        }
    }

    assert resolve_codex_cli(config) == codex_cli


def test_update_manifest_only_advances_pending_restart_state(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    manifest_path, _ = write_chained_repair_manifest(
        run_root,
        {"status": "pending_restart_validation"},
    )
    report_path = tmp_path / "post_restart.json"
    report_path.write_text('{"status":"pending_live_ui_validation"}', encoding="utf-8")

    update_manifest(manifest_path, report_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "pending_live_ui_validation"
    assert manifest["restart_validation_report"] == str(report_path)
    assert manifest["restart_validation_report_sha256"] == hashlib.sha256(report_path.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="not pending restart validation"):
        update_manifest(manifest_path, report_path)


def test_restart_registry_validation_rejects_remote_or_wrong_source(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex_home"
    marketplace = codex_home / ".tmp" / "bundled-marketplaces" / "openai-bundled"
    installed = []
    for plugin_name in verify_codex_after_restart.required_plugin_names:
        manifest_path = marketplace / "plugins" / plugin_name / ".codex-plugin" / "plugin.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
        installed.append(
            {
                "pluginId": f"{plugin_name}@openai-bundled",
                "installed": True,
                "enabled": True,
                "version": "1.0",
                "source": {"source": "local", "path": str(marketplace / "plugins" / plugin_name)},
                "marketplaceSource": {"sourceType": "local", "source": str(marketplace)},
            }
        )

    monkeypatch.setattr(
        verify_codex_after_restart,
        "run_json_command",
        lambda _command, _environment: {"installed": installed},
    )
    assert len(validate_plugin_registry(tmp_path / "codex.exe", codex_home)) == 5

    installed[0]["source"] = {"source": "remote", "path": r"C:\wrong"}
    with pytest.raises(RuntimeError, match="source is not local"):
        validate_plugin_registry(tmp_path / "codex.exe", codex_home)


def test_restart_registry_validation_rejects_persistent_mirror_as_runtime_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / "codex_home"
    runtime_marketplace = codex_home / ".tmp" / "bundled-marketplaces" / "openai-bundled"
    persistent_marketplace = codex_home / "cache" / "bundled-marketplaces" / "openai-bundled"
    installed = []
    for plugin_name in verify_codex_after_restart.required_plugin_names:
        manifest_path = runtime_marketplace / "plugins" / plugin_name / ".codex-plugin" / "plugin.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
        installed.append(
            {
                "pluginId": f"{plugin_name}@openai-bundled",
                "installed": True,
                "enabled": True,
                "version": "1.0",
                "source": {
                    "source": "local",
                    "path": str(persistent_marketplace / "plugins" / plugin_name),
                },
                "marketplaceSource": {
                    "sourceType": "local",
                    "source": str(persistent_marketplace),
                },
            }
        )

    monkeypatch.setattr(
        verify_codex_after_restart,
        "run_json_command",
        lambda _command, _environment: {"installed": installed},
    )

    with pytest.raises(RuntimeError, match="source path mismatch|marketplace path mismatch"):
        validate_plugin_registry(tmp_path / "codex.exe", codex_home)


def test_restart_registry_validation_requires_apple_plugins_to_be_uninstalled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / "codex_home"
    entries = [
        {
            "pluginId": selector,
            "installed": False,
            "enabled": False,
            "version": "1.0",
        }
        for selector in verify_codex_after_restart.windows_incompatible_plugin_selectors
    ]
    monkeypatch.setattr(
        verify_codex_after_restart,
        "run_json_command",
        lambda _command, _environment: {"installed": [], "available": entries},
    )

    assert len(validate_windows_incompatible_plugin_registry(tmp_path / "codex.exe", codex_home)) == 2

    entries[0]["installed"] = True
    with pytest.raises(RuntimeError, match="remains installed or enabled"):
        validate_windows_incompatible_plugin_registry(tmp_path / "codex.exe", codex_home)


def test_restart_verifier_requires_desktop_runtime_marketplace_source_check() -> None:
    assert "plugins.bundled_marketplace_source" in verify_codex_after_restart.required_diagnostic_checks
    assert "plugins.stale_restore_artifacts" in verify_codex_after_restart.required_diagnostic_checks
    assert "plugins.chrome_native_hosts" in verify_codex_after_restart.required_diagnostic_checks
    assert "plugins.chrome_native_messaging_manifests" in verify_codex_after_restart.required_diagnostic_checks
    offline_runner_text = (scripts_path / "repair_all_codex_after_exit.ps1").read_text(encoding="utf-8")
    assert '--require-pass", "plugins.chrome_native_hosts"' in offline_runner_text
    assert '--require-pass", "plugins.chrome_native_messaging_manifests"' in offline_runner_text


def test_restart_audit_gate_allows_performance_findings_but_rejects_hard_blockers() -> None:
    summary = {
        "missing_rollout_count": 0,
        "estimated_current_parser_errors": 0,
        "json_parse_error_count": 0,
        "shared_rollout_mapping_count": 0,
        "active_performance_repair_candidate_count": 12,
        "active_compatibility_repair_candidate_count": 0,
        "active_blocked_count": 9,
        "active_hard_blocked_count": 0,
        "archived_performance_repair_candidate_count": 4,
        "archived_compatibility_repair_candidate_count": 0,
        "archived_blocked_count": 3,
        "archived_hard_blocked_count": 0,
    }

    validate_post_restart_audit_summary(summary)

    summary["active_hard_blocked_count"] = 1

    with pytest.raises(RuntimeError, match="compatibility candidates or hard-blocked threads"):
        validate_post_restart_audit_summary(summary)


def test_restart_prompt_contract_allows_append_but_rejects_changed_history(tmp_path: Path) -> None:
    rollout_path = tmp_path / "rollout-thread-a.jsonl"
    baseline_records = [
        {"type": "session_meta", "payload": {"id": "thread-a"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "first"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "second"}},
    ]

    def write_records(records: list[dict[str, object]]) -> None:
        rollout_path.write_text(
            "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
            encoding="utf-8",
        )

    write_records(baseline_records)
    baseline_scan = scan_rollout(rollout_path)
    baseline_audit = {
        "threads": [
            {
                "id": "thread-a",
                "rollout_path": str(rollout_path),
                "scan": baseline_scan.to_dict(),
            }
        ]
    }
    write_records(
        [
            *baseline_records,
            {"type": "event_msg", "payload": {"type": "user_message", "message": "appended"}},
        ]
    )
    current_scan = scan_rollout(rollout_path)
    current_audit = {
        "threads": [
            {
                "id": "thread-a",
                "rollout_path": str(rollout_path),
                "scan": current_scan.to_dict(),
            }
        ]
    }

    result = validate_restart_prompt_contract(baseline_audit, current_audit)
    assert result["appended_prompt_count"] == 1

    write_records(
        [
            baseline_records[0],
            baseline_records[2],
            baseline_records[1],
            {"type": "event_msg", "payload": {"type": "user_message", "message": "appended"}},
        ]
    )
    current_audit["threads"][0]["scan"] = scan_rollout(rollout_path).to_dict()
    with pytest.raises(RuntimeError, match="not an exact prefix"):
        validate_restart_prompt_contract(baseline_audit, current_audit)


def test_restart_verifier_accepts_only_bound_prompt_preserving_targeted_slimming(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    repair_data = run_root / "repair_data"
    repair_data.mkdir(parents=True)
    manifest_path = repair_data / "repair_manifest.json"
    baseline_audit = {
        "schema_version": 5,
        "policy": {"version": 2},
        "threads": [
            {
                "id": "thread-a",
                "scan": {"total_bytes": 1_000, "user_prompt_count": 2, "user_prompt_sha256": "a" * 64},
            }
        ],
    }
    audit_path = run_root / "offline_thread_audit.json"
    audit_bytes = json.dumps(baseline_audit).encode("utf-8")
    audit_path.write_bytes(audit_bytes)
    manifest = {
        "audit_path": str(audit_path),
        "audit_sha256": hashlib.sha256(audit_bytes).hexdigest(),
        "prompt_preservation_required": True,
        "checkpoint_history_reduction_enabled": True,
        "prompt_preserving_slim_thread_ids": ["thread-a"],
        "thread_repairs": [
            {
                "thread_id": "thread-a",
                "strategy": "prompt_preserving_checkpoint_slim_view",
                "original_bytes": 1_000,
                "active_bytes": 250,
            }
        ],
    }

    loaded = verify_codex_after_restart.load_prompt_baseline_audit(manifest_path, manifest)
    current_audit = {
        "threads": [
            {
                "id": "thread-a",
                "scan": {"total_bytes": 260, "user_prompt_count": 2, "user_prompt_sha256": "a" * 64},
            }
        ]
    }
    result = verify_codex_after_restart.validate_restart_slim_contract(manifest, loaded, current_audit)

    assert result == {
        "enabled": True,
        "thread_count": 1,
        "reductions": [
            {
                "thread_id": "thread-a",
                "baseline_bytes": 1_000,
                "current_bytes": 260,
                "reduction_bytes": 740,
            }
        ],
    }

    manifest["thread_repairs"][0]["active_bytes"] = 1_000
    with pytest.raises(RuntimeError, match="did not reduce"):
        verify_codex_after_restart.load_prompt_baseline_audit(manifest_path, manifest)


def test_compare_plugin_tree_detects_empty_directory_and_entry_type_drift(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / "empty").mkdir(parents=True)
    destination.mkdir()

    with pytest.raises(RuntimeError, match="tree mismatch"):
        verify_codex_after_restart.compare_plugin_tree(source, destination)

    (destination / "empty").write_text("wrong type", encoding="utf-8")
    with pytest.raises(RuntimeError, match="tree mismatch"):
        verify_codex_after_restart.compare_plugin_tree(source, destination)


def make_junction(path: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)


def test_restart_plugin_tree_validation_checks_all_materialized_layers(tmp_path: Path) -> None:
    appx_marketplace = tmp_path / "appx" / "openai-bundled"
    codex_home = tmp_path / "codex_home"
    persistent_marketplace = codex_home / "cache" / "bundled-marketplaces" / "openai-bundled"
    temporary_marketplace = codex_home / ".tmp" / "bundled-marketplaces" / "openai-bundled"
    registry: list[dict[str, object]] = []

    for marketplace in (appx_marketplace, persistent_marketplace, temporary_marketplace):
        (marketplace / ".agents" / "plugins").mkdir(parents=True)
        (marketplace / ".agents" / "plugins" / "marketplace.json").write_text("{}", encoding="utf-8")

    for plugin_name in verify_codex_after_restart.required_plugin_names:
        for marketplace in (appx_marketplace, persistent_marketplace, temporary_marketplace):
            plugin_root = marketplace / "plugins" / plugin_name
            (plugin_root / ".codex-plugin").mkdir(parents=True)
            (plugin_root / "empty-runtime").mkdir()
            (plugin_root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"version": "1.0"}),
                encoding="utf-8",
            )
        source_plugin = persistent_marketplace / "plugins" / plugin_name
        version_root = codex_home / "plugins" / "cache" / "openai-bundled" / plugin_name / "1.0"
        version_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_plugin, version_root)
        make_junction(version_root.parent / "latest", version_root)
        registry.append({"plugin_id": f"{plugin_name}@openai-bundled", "version": "1.0"})

    report = verify_codex_after_restart.validate_plugin_trees(
        codex_home,
        appx_marketplace,
        registry,
    )

    assert len(report["plugins"]) == len(verify_codex_after_restart.required_plugin_names)
    assert report["persistent_marketplace"] == str(persistent_marketplace)

    first_plugin = verify_codex_after_restart.required_plugin_names[0]
    version_root = codex_home / "plugins" / "cache" / "openai-bundled" / first_plugin / "1.0"
    relocated_version_root = tmp_path / "relocated-version-root"
    os.replace(version_root, relocated_version_root)
    make_junction(version_root, relocated_version_root)
    with pytest.raises(RuntimeError, match="version cache.*regular directory"):
        verify_codex_after_restart.validate_plugin_trees(codex_home, appx_marketplace, registry)
    os.rmdir(version_root)
    os.replace(relocated_version_root, version_root)

    corrupt_file = (
        codex_home
        / "plugins"
        / "cache"
        / "openai-bundled"
        / first_plugin
        / "1.0"
        / "extra.txt"
    )
    corrupt_file.write_text("unexpected", encoding="utf-8")
    with pytest.raises(RuntimeError, match="tree mismatch"):
        verify_codex_after_restart.validate_plugin_trees(codex_home, appx_marketplace, registry)

def test_restart_verifier_rejects_manifest_for_another_codex_home(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    backup_root = tmp_path / "backup"
    repair_data = backup_root / "run" / "repair_data"
    repair_data.mkdir(parents=True)
    plugin_snapshot_manifest = write_plugin_snapshot_contract(backup_root / "run", codex_home, "run-id")
    manifest_path, _ = write_chained_repair_manifest(
        backup_root / "run",
        {
            "schema_version": 1,
            "status": "pending_restart_validation",
            "runner_run_id": "run-id",
            "codex_home": str(tmp_path / "other_home"),
            "plugin_snapshot_manifest": str(plugin_snapshot_manifest),
            "plugin_snapshot_manifest_sha256": hashlib.sha256(plugin_snapshot_manifest.read_bytes()).hexdigest(),
        },
        backup_root=backup_root,
        lock_status="pending_restart_validation",
    )

    with pytest.raises(RuntimeError, match="codex_home mismatch"):
        verify_codex_after_restart.validate_manifest_contract(codex_home, manifest_path, backup_root)


def test_restart_verifier_binds_manifest_snapshot_and_report_to_same_run(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    backup_root = tmp_path / "backup"
    run_root = backup_root / "run"
    repair_data = run_root / "repair_data"
    repair_data.mkdir(parents=True)
    snapshot_manifest = write_plugin_snapshot_contract(run_root, codex_home, "run-id")
    manifest_path, _ = write_chained_repair_manifest(
        run_root,
        {
            "schema_version": 1,
            "status": "pending_restart_validation",
            "runner_run_id": "run-id",
            "codex_home": str(codex_home),
            "plugin_snapshot_manifest": str(snapshot_manifest),
            "plugin_snapshot_manifest_sha256": hashlib.sha256(snapshot_manifest.read_bytes()).hexdigest(),
        },
        backup_root=backup_root,
        lock_status="pending_restart_validation",
    )

    verify_codex_after_restart.validate_manifest_contract(codex_home, manifest_path, backup_root)
    verify_codex_after_restart.validate_restart_report_path(manifest_path, run_root / "post_restart.json")

    with pytest.raises(RuntimeError, match="same repair run"):
        verify_codex_after_restart.validate_restart_report_path(manifest_path, backup_root / "other.json")


def test_live_validation_completion_requires_all_checks_and_releases_persistent_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    database_path = write_restart_notify_database(codex_home)
    process_snapshot, current_appx_install = make_notify_process_context(tmp_path)
    original_capture = verify_codex_after_restart.capture_notify_log_boundary
    monkeypatch.setattr(
        verify_codex_after_restart,
        "capture_notify_log_boundary",
        lambda path, **kwargs: original_capture(
            path,
            process_snapshot=process_snapshot,
            current_appx_install=current_appx_install,
        ),
    )
    notify_boundary = verify_codex_after_restart.capture_notify_log_boundary(database_path)
    notify_boundary["captured_at_epoch"] = 100
    backup_root = tmp_path / "backup"
    run_root = backup_root / "run"
    repair_data = run_root / "repair_data"
    repair_data.mkdir(parents=True)
    run_id = "run-id"
    restart_report = run_root / "post_restart.json"
    restart_payload = {
        "schema_version": 1,
        "status": "pending_live_ui_validation",
        "run_id": run_id,
        "codex_home": str(codex_home),
        "generated_at_epoch": 100,
        "notify_os206_validation": {
            "schema_version": 2,
            "min_epoch": 100,
            "initial_boundary": notify_boundary,
            "live_probe_baseline": notify_boundary,
            "query": {
                "schema_version": 2,
                "min_epoch": 100,
                "process_uuid": "pid:200:current-process",
                "process_uuids": ["pid:200:current-process"],
                "after_id": 0,
                "max_id": 1,
                "match_count": 0,
                "matches": [],
            },
        },
    }
    restart_report.write_text(json.dumps(restart_payload), encoding="utf-8")
    restart_sha256 = hashlib.sha256(restart_report.read_bytes()).hexdigest()
    request_path, _, request_sha256 = write_test_live_validation_request(
        run_root, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", codex_home
    )
    manifest_path, _ = write_chained_repair_manifest(
        run_root,
        {
            "schema_version": 1,
            "status": "pending_live_ui_validation",
            "runner_run_id": run_id,
            "codex_home": str(codex_home),
            "restart_validation_report": str(restart_report),
            "restart_validation_report_sha256": restart_sha256,
            "live_validation_challenge": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "live_validation_request": str(request_path),
            "live_validation_request_sha256": request_sha256,
            "prompt_preserving_slim_thread_ids": ["large-thread-a"],
        },
        backup_root=backup_root,
        lock_status="pending_live_ui_validation",
    )
    lock_path = backup_root / "active_repair.lock.json"
    evidence_path = run_root / "live_validation.json"
    evidence = {
        "schema_version": 2,
        "status": "pass",
        "run_id": run_id,
        "codex_home": str(codex_home),
        "restart_validation_report_sha256": restart_sha256,
        "live_validation_request": str(request_path),
        "live_validation_request_sha256": request_sha256,
        "generated_at_epoch": 120,
        "collector": collector_metadata(run_root),
        "checks": make_live_checks(run_root, 100, codex_home, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    result = verify_codex_after_restart.complete_live_validation(
        codex_home,
        manifest_path,
        evidence_path,
        backup_root,
    )

    assert result["status"] == "complete"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "complete"
    completed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed_manifest["live_validation_evidence_sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert json.loads((repair_data / "repair_manifest.mirror.json").read_text(encoding="utf-8"))["status"] == "complete"
    assert not lock_path.exists()
    assert (run_root / "runner_lock_completed.json").is_file()

    os.replace(run_root / "runner_lock_completed.json", lock_path)
    retry_result = verify_codex_after_restart.complete_live_validation(
        codex_home,
        manifest_path,
        evidence_path,
        backup_root,
    )
    assert retry_result["status"] == "complete"
    assert not lock_path.exists()
    assert (run_root / "runner_lock_completed.json").is_file()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO logs (id, ts, ts_nanos, level, target, feedback_log_body, process_uuid)
            VALUES (2, 121, 20, 'WARN', 'codex_core::hook_runtime', 'legacy_notify failed: os error 206', 'pid:200:current-process')
            """
        )
    os.replace(run_root / "runner_lock_completed.json", lock_path)
    with pytest.raises(RuntimeError, match="live probes logged legacy_notify os error 206"):
        verify_codex_after_restart.complete_live_validation(
            codex_home,
            manifest_path,
            evidence_path,
            backup_root,
        )
    assert lock_path.is_file()


def make_notify_process_context(tmp_path: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    appx_root = tmp_path / "current-appx"
    desktop_path = appx_root / "app" / "ChatGPT.exe"
    codex_path = appx_root / "app" / "resources" / "codex.exe"
    return (
        [
            {
                "pid": 100,
                "parentPid": 1,
                "name": "ChatGPT.exe",
                "executablePath": str(desktop_path),
                "commandLine": f'"{desktop_path}"',
                "createdAtEpoch": 99,
            },
            {
                "pid": 200,
                "parentPid": 100,
                "name": "codex.exe",
                "executablePath": str(codex_path),
                "commandLine": f'"{codex_path}" app-server --analytics-default-enabled',
                "createdAtEpoch": 100,
            },
        ],
        {"available": True, "installPath": str(appx_root), "version": "1.0", "error": ""},
    )


def write_restart_notify_database(codex_home: Path, process_uuid: str = "pid:200:current-process") -> Path:
    database_path = codex_home / "logs_2.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY,
                ts INTEGER NOT NULL,
                ts_nanos INTEGER NOT NULL,
                level TEXT NOT NULL,
                target TEXT NOT NULL,
                feedback_log_body TEXT,
                process_uuid TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO logs (id, ts, ts_nanos, level, target, feedback_log_body, process_uuid)
            VALUES (1, 100, 10, 'INFO', 'codex_app_server::startup', 'ready', ?)
            """,
            (process_uuid,),
        )
    return database_path


def test_live_notify_recheck_fails_when_probe_adds_os206(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    database_path = write_restart_notify_database(codex_home)
    process_snapshot, current_appx_install = make_notify_process_context(tmp_path)
    baseline = verify_codex_after_restart.capture_notify_log_boundary(
        database_path,
        process_snapshot=process_snapshot,
        current_appx_install=current_appx_install,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO logs (id, ts, ts_nanos, level, target, feedback_log_body, process_uuid)
            VALUES (2, 101, 20, 'WARN', 'codex_core::hook_runtime', 'legacy_notify failed: os error 206', 'pid:200:current-process')
            """
        )

    with pytest.raises(RuntimeError, match="os error 206"):
        verify_codex_after_restart.validate_live_notify_log_window(
            database_path,
            baseline,
            min_epoch=100,
            process_snapshot=process_snapshot,
            current_appx_install=current_appx_install,
        )


def test_restart_notify_check_ignores_previous_process_history(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    database_path = write_restart_notify_database(codex_home)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE logs SET process_uuid = 'pid:900:previous-process', ts = 100 WHERE id = 1")
        connection.execute(
            """
            INSERT INTO logs (id, ts, ts_nanos, level, target, feedback_log_body, process_uuid)
            VALUES (2, 100, 20, 'INFO', 'codex_app_server::startup', 'current process ready', 'pid:200:current-process')
            """
        )
    process_snapshot, current_appx_install = make_notify_process_context(tmp_path)
    baseline = verify_codex_after_restart.capture_notify_log_boundary(
        database_path,
        process_snapshot=process_snapshot,
        current_appx_install=current_appx_install,
    )

    result = verify_codex_after_restart.validate_restart_notify_log_window(
        database_path,
        baseline,
        min_epoch=100,
        process_snapshot=process_snapshot,
        current_appx_install=current_appx_install,
    )

    assert result["query"]["match_count"] == 0
    assert result["query"]["unexpected_process_uuids"] == []
    assert result["live_probe_baseline"]["process_uuid"] == "pid:200:current-process"


def test_live_notify_recheck_passes_without_new_match(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    database_path = write_restart_notify_database(codex_home)
    process_snapshot, current_appx_install = make_notify_process_context(tmp_path)
    baseline = verify_codex_after_restart.capture_notify_log_boundary(
        database_path,
        process_snapshot=process_snapshot,
        current_appx_install=current_appx_install,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO logs (id, ts, ts_nanos, level, target, feedback_log_body, process_uuid)
            VALUES (2, 101, 20, 'INFO', 'codex_core::hook_runtime', 'legacy_notify completed', 'pid:200:current-process')
            """
        )

    result = verify_codex_after_restart.validate_live_notify_log_window(
        database_path,
        baseline,
        min_epoch=100,
        process_snapshot=process_snapshot,
        current_appx_install=current_appx_install,
    )

    assert result["query"]["match_count"] == 0
    assert result["final_boundary"]["max_id"] == 2


@pytest.mark.parametrize("changed_field", ["database_identity", "desktop_root_pid"])
def test_live_notify_recheck_rejects_log_identity_or_process_change(tmp_path: Path, changed_field: str) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    database_path = write_restart_notify_database(codex_home)
    process_snapshot, current_appx_install = make_notify_process_context(tmp_path)
    baseline = verify_codex_after_restart.capture_notify_log_boundary(
        database_path,
        process_snapshot=process_snapshot,
        current_appx_install=current_appx_install,
    )
    if changed_field == "database_identity":
        baseline["database_identity"] = {**baseline["database_identity"], "inode": baseline["database_identity"]["inode"] + 1}
        expected_error = "database identity changed"
    else:
        baseline["desktop_root_pid"] += 1
        expected_error = "Desktop root process changed"

    with pytest.raises(RuntimeError, match=expected_error):
        verify_codex_after_restart.validate_live_notify_log_window(
            database_path,
            baseline,
            min_epoch=100,
            process_snapshot=process_snapshot,
            current_appx_install=current_appx_install,
        )


def test_live_validation_completion_keeps_lock_when_evidence_is_incomplete(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    backup_root = tmp_path / "backup"
    run_root = backup_root / "run"
    repair_data = run_root / "repair_data"
    repair_data.mkdir(parents=True)
    run_id = "run-id"
    restart_report = run_root / "post_restart.json"
    restart_report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "pending_live_ui_validation",
                "run_id": run_id,
                "codex_home": str(codex_home),
                "generated_at_epoch": 100,
            }
        ),
        encoding="utf-8",
    )
    restart_sha256 = hashlib.sha256(restart_report.read_bytes()).hexdigest()
    request_path, _, request_sha256 = write_test_live_validation_request(
        run_root, "ffffffffffffffffffffffffffffffff", codex_home
    )
    manifest_path, _ = write_chained_repair_manifest(
        run_root,
        {
            "schema_version": 1,
            "status": "pending_live_ui_validation",
            "runner_run_id": run_id,
            "codex_home": str(codex_home),
            "restart_validation_report": str(restart_report),
            "restart_validation_report_sha256": restart_sha256,
            "live_validation_challenge": "ffffffffffffffffffffffffffffffff",
            "live_validation_request": str(request_path),
            "live_validation_request_sha256": request_sha256,
        },
        backup_root=backup_root,
        lock_status="pending_live_ui_validation",
    )
    lock_path = backup_root / "active_repair.lock.json"
    evidence_path = run_root / "live_validation.json"
    evidence_path.write_text(json.dumps({"schema_version": 1, "status": "pass", "checks": []}))

    with pytest.raises(RuntimeError, match="collector|evidence is incomplete"):
        verify_codex_after_restart.complete_live_validation(codex_home, manifest_path, evidence_path, backup_root)

    assert lock_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "pending_live_ui_validation"


def test_live_event_provenance_rejects_forged_call_id_even_with_complete_claims(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    run_root = tmp_path / "run"
    challenge = "cccccccccccccccccccccccccccccccc"
    check_items = make_live_checks(run_root, 100, codex_home, challenge)
    checks = {str(item["id"]): item for item in check_items}
    checks["browser.live_tool"]["evidence"]["result"]["tool_call_id"] = "call-forged-000"

    with pytest.raises(RuntimeError, match="does not match the real rollout"):
        verify_codex_after_restart.validate_live_event_provenance(codex_home, checks, challenge, 100)


def test_live_event_provenance_rejects_target_reply_changed_after_collection(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    run_root = tmp_path / "run"
    challenge = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    check_items = make_live_checks(run_root, 100, codex_home, challenge)
    checks = {str(item["id"]): item for item in check_items}
    result = checks["official_thread_tools.live_tool"]["evidence"]["result"]
    target_path = Path(str(result["target_rollout_path"]))
    target_lines = target_path.read_text(encoding="utf-8").splitlines()
    response_index = int(result["target_response_line"]) - 1
    response_record = json.loads(target_lines[response_index])
    response_record["payload"]["content"].append({"type": "output_text", "text": "tampered after collection"})
    target_lines[response_index] = json.dumps(response_record)
    target_path.write_text("\n".join(target_lines) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="target response prefix hash mismatch"):
        verify_codex_after_restart.validate_live_event_provenance(codex_home, checks, challenge, 100)


def test_bound_collector_derives_receipts_from_rollouts_without_accepting_call_ids(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    run_root = tmp_path / "run"
    (run_root / "repair_data").mkdir(parents=True)
    challenge = "dddddddddddddddddddddddddddddddd"
    restart_report = run_root / "restart.json"
    restart_report.write_text(json.dumps({"generated_at_epoch": 100}), encoding="utf-8")
    request_path, _, request_sha256 = write_test_live_validation_request(run_root, challenge, codex_home)
    manifest_path = run_root / "repair_data" / "repair_manifest.json"
    verify_codex_after_restart.write_manifest_pair(
        manifest_path,
        {
            "schema_version": 1,
            "status": "pending_live_ui_validation",
            "runner_run_id": "run-a",
            "codex_home": str(codex_home),
            "restart_validation_report": str(restart_report),
            "live_validation_challenge": challenge,
            "live_validation_request": str(request_path),
            "live_validation_request_sha256": request_sha256,
            "prompt_preserving_slim_thread_ids": ["large-thread-a"],
        },
    )
    make_live_checks(run_root, 100, codex_home, challenge)
    output_path = run_root / "collected.json"

    evidence = collect_codex_live_validation.collect(
        codex_home,
        manifest_path,
        request_path,
        output_path,
    )

    assert evidence["schema_version"] == 2
    assert evidence["collector"]["name"] == "collect_codex_live_validation"
    assert {item["id"] for item in evidence["checks"]} == set(
        verify_codex_after_restart.required_live_validation_checks
    )
    assert all(item["evidence"]["result"]["call_line"] > 0 for item in evidence["checks"])
    large_check = next(item for item in evidence["checks"] if item["id"] == "large_thread.ui_responsiveness")
    large_result = large_check["evidence"]["result"]
    assert large_result["thread_id"] == "large-thread-a"
    assert large_result["thread_identity_provenance"] == "state_5.sqlite+target_rollout_user_prompt"
    assert large_result["submitted_prompt_line"] == 32_001

    request = json.loads(request_path.read_text(encoding="utf-8"))
    marker = str(request["large_thread_input_marker"])
    large_rollout = codex_home / "sessions" / "large.jsonl"
    large_lines = large_rollout.read_text(encoding="utf-8").splitlines()
    large_rollout.write_text("\n".join(large_lines[:-1]) + "\n", encoding="utf-8")
    source_rollout = codex_home / "sessions" / "source.jsonl"
    with source_rollout.open("a", encoding="utf-8") as source_handle:
        source_handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": marker}],
                    },
                }
            )
            + "\n"
        )
    with pytest.raises(RuntimeError, match="target rollout|target thread|input marker"):
        collect_codex_live_validation.collect(
            codex_home,
            manifest_path,
            request_path,
            run_root / "misbound.json",
        )


def test_live_check_rejects_unstructured_claim_and_tampered_artifact(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    check_id = "browser.live_tool"
    claimed = {
        "status": "pass",
        "evidence": {
            "method": "direct_tool_call",
            "started_at_epoch": 101,
            "completed_at_epoch": 102,
            "result": "verified browser",
            "artifacts": [],
        },
    }
    with pytest.raises(RuntimeError, match="result is not structured"):
        verify_codex_after_restart.validate_live_check_contract(check_id, claimed, run_root, 100, {})

    artifact = write_bound_artifact(run_root, "browser.json", {"status": "ok"})
    artifact_path = Path(str(artifact["path"]))
    artifact_path.write_text("tampered", encoding="utf-8")
    structured = {
        "status": "pass",
        "evidence": {
            "method": "direct_tool_call",
            "started_at_epoch": 101,
            "completed_at_epoch": 102,
            "result": {
                "tool_call_id": "call-browser-001",
                "tool_name": "browser",
                "invocation_status": "completed",
                "observed_result": "ok",
            },
            "artifacts": [artifact],
        },
    }
    with pytest.raises(RuntimeError, match="artifact hash mismatch"):
        verify_codex_after_restart.validate_live_check_contract(check_id, structured, run_root, 100, {})


def test_ui_live_check_requires_a_hash_bound_png_screenshot(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    artifact = write_bound_artifact(run_root, "sidebar.json", {"not": "a screenshot"})
    check = {
        "status": "pass",
        "evidence": {
            "method": "visual_and_state_crosscheck",
            "started_at_epoch": 101,
            "completed_at_epoch": 102,
            "result": {
                "visible_thread_ids": ["thread-a"],
                "state_thread_ids": ["thread-a"],
                "matched_thread_titles": {"thread-a": "Thread A"},
                "accessibility_sha256": "a" * 64,
                "screenshot_verified": True,
            },
            "artifacts": [artifact],
        },
    }

    with pytest.raises(RuntimeError, match="PNG screenshot"):
        verify_codex_after_restart.validate_live_check_contract(
            "sidebar.thread_visibility",
            check,
            run_root,
            100,
            {},
        )


def test_completed_old_run_cannot_move_a_new_runs_active_lock(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    backup_root = tmp_path / "backup"
    old_run_root = backup_root / "old-run"
    repair_data = old_run_root / "repair_data"
    repair_data.mkdir(parents=True)
    run_id = "old-run-id"
    restart_report = old_run_root / "post_restart.json"
    restart_report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "pending_live_ui_validation",
                "run_id": run_id,
                "codex_home": str(codex_home),
                "generated_at_epoch": 100,
            }
        ),
        encoding="utf-8",
    )
    restart_sha256 = hashlib.sha256(restart_report.read_bytes()).hexdigest()
    manifest_path, _ = write_chained_repair_manifest(
        old_run_root,
        {
            "schema_version": 1,
            "status": "complete",
            "runner_run_id": run_id,
            "codex_home": str(codex_home),
            "restart_validation_report": str(restart_report),
            "restart_validation_report_sha256": restart_sha256,
            "live_validation_challenge": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        },
        backup_root=backup_root,
        lock_status="complete",
    )
    evidence_path = old_run_root / "live_validation.json"
    evidence = {
        "schema_version": 2,
        "status": "pass",
        "run_id": run_id,
        "codex_home": str(codex_home),
        "restart_validation_report_sha256": restart_sha256,
        "generated_at_epoch": 120,
        "collector": collector_metadata(old_run_root),
        "checks": make_live_checks(old_run_root, 100, codex_home, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    manifest, _ = verify_codex_after_restart.load_manifest_pair(manifest_path)
    manifest["live_validation_evidence"] = str(evidence_path)
    manifest["live_validation_evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    verify_codex_after_restart.write_manifest_pair(manifest_path, manifest)
    new_lock = {
        "run_id": "new-run-id",
        "run_root": str(backup_root / "new-run"),
        "status": "pending_offline_rollback",
    }
    lock_path = backup_root / "active_repair.lock.json"
    lock_path.write_text(json.dumps(new_lock), encoding="utf-8")

    with pytest.raises(RuntimeError, match="another run root"):
        verify_codex_after_restart.complete_live_validation(codex_home, manifest_path, evidence_path, backup_root)

    assert json.loads(lock_path.read_text(encoding="utf-8")) == new_lock
