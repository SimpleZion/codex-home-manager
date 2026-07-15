from __future__ import annotations

import datetime as datetime_module
import os
import json
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend import diagnostics as diagnostics_module
from backend import server
from backend.codex_data import (
    archive_thread,
    build_snapshot,
    codex_home_overview,
    copy_resource_from_home,
    detect_current_codex_versions,
    duplicate_thread,
    export_root_path,
    enforce_write_safety,
    export_thread_prompts,
    get_thread_daily_token_usage,
    get_thread_detail,
    hide_thread_from_sidebar,
    import_project_from_home,
    import_thread_from_home,
    inspect_codex_cli_metadata,
    is_real_codex_home_path,
    list_backups,
    move_thread_workspace,
    migrate_thread_project,
    normalize_path_text,
    parse_rollout_stats,
    preview_official_thread_tools_repair,
    preview_thread_workspace_move,
    preview_import_thread_from_home,
    preview_import_project_from_home,
    preview_project_rename,
    preview_resource_copy,
    preview_slim_thread,
    read_codex_resource,
    read_thread_prompts,
    read_thread_logs,
    rename_project,
    repair_official_thread_tools_exposure,
    restore_backup,
    repair_user_event,
    resolve_codex_paths,
    show_thread_in_sidebar,
    slim_thread,
    sync_thread_sqlite_title,
    write_codex_resource,
    write_sealed_backup_manifest,
)
from backend.diagnostics import (
    capacity_trend_state_path,
    capture_notify_log_boundary,
    extract_advertised_skill_paths,
    find_codex_cli_candidates,
    inspect_codex_cli_candidate,
    query_legacy_notify_os206,
    parse_json_file,
    parse_mcp_process_snapshot,
    node_repl_process_command_mismatches,
    record_capacity_trend,
    run_codex_diagnostics,
)


def test_node_repl_layer_consistency_allows_no_managed_override() -> None:
    contract = diagnostics_module.node_repl_effective_contract(
        '[mcp_servers.node_repl]\nargs=[]\n[mcp_servers.node_repl.env]\nA="1"\nB="2"\n',
        '[organization]\npolicy="preserve"\n',
    )

    assert contract["conflicts"] == []
    assert contract["effectiveEnv"] == {"A": "1", "B": "2"}


def test_node_repl_layer_consistency_rejects_managed_runtime_path_policy() -> None:
    contract = diagnostics_module.node_repl_effective_contract(
        '[mcp_servers.node_repl]\nargs=["--disable-sandbox"]\n[mcp_servers.node_repl.env]\nNODE_REPL_NODE_MODULE_DIRS="C:/desktop/runtime"\nCODEX_CLI_PATH="C:/stale/codex.exe"\n',
        '[mcp_servers.node_repl]\nargs=["--disable-sandbox"]\n[mcp_servers.node_repl.env]\nNODE_REPL_NODE_MODULE_DIRS="D:/Software/codex-node-runtime/node_modules"\nCODEX_CLI_PATH="D:/.codex/plugins/.plugin-appserver/codex.exe"\n',
    )

    assert contract["conflicts"]
    assert any("must be absent" in conflict for conflict in contract["conflicts"])
    assert contract["effectiveEnv"]["NODE_REPL_NODE_MODULE_DIRS"] == (
        "D:/Software/codex-node-runtime/node_modules"
    )
    assert contract["effectiveEnv"]["CODEX_CLI_PATH"] == (
        "D:/.codex/plugins/.plugin-appserver/codex.exe"
    )


def test_auth_token_accepts_same_origin_loopback_on_non_default_port(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    loopback_client = TestClient(server.app, base_url="http://127.0.0.1:8876")

    response = loopback_client.get(
        "/api/auth/token",
        params={"codex_home": str(codex_home_path)},
        headers={"Origin": "http://127.0.0.1:8876"},
    )

    assert response.status_code == 200
    assert response.json()["token"]


def test_auth_token_rejects_public_origin_even_when_target_is_loopback(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    loopback_client = TestClient(server.app, base_url="http://127.0.0.1:8876")

    response = loopback_client.get(
        "/api/auth/token",
        params={"codex_home": str(codex_home_path)},
        headers={"Origin": "https://codex-home-manager.simplezion.com"},
    )

    assert response.status_code == 403


@pytest.fixture(autouse=True)
def isolate_backup_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME_MANAGER_BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv("CODEX_HOME_MANAGER_STATE_ROOT", str(tmp_path / "manager-state"))
    monkeypatch.setattr("backend.codex_data.detect_codex_processes", lambda: [])
    monkeypatch.setattr("backend.diagnostics.detect_codex_processes", lambda: [])


def test_export_root_path_uses_configured_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    export_root = tmp_path / "exports"
    monkeypatch.setenv("CODEX_HOME_MANAGER_EXPORT_ROOT", str(export_root))

    assert export_root_path() == export_root.resolve(strict=False)


def test_backend_runtime_does_not_spawn_console_subprocesses() -> None:
    project_root = Path(__file__).resolve().parents[1]
    for relative_path in ["backend/codex_data.py", "backend/diagnostics.py"]:
        source_text = (project_root / relative_path).read_text(encoding="utf-8")
        assert "subprocess" not in source_text
        assert "tasklist" not in source_text
        assert "where.exe" not in source_text


def test_parse_mcp_process_snapshot_reports_fanout() -> None:
    snapshot = parse_mcp_process_snapshot(
        [
            {"ProcessId": 1, "ParentProcessId": 10, "Name": "node.exe", "CommandLine": '"node" ./mcp/server.mjs'},
            {"ProcessId": 2, "ParentProcessId": 10, "Name": "node.exe", "CommandLine": '"node" ./mcp/server.mjs'},
            {"ProcessId": 3, "ParentProcessId": 10, "Name": "node.exe", "CommandLine": '"node" ./mcp/server.mjs'},
            {"ProcessId": 4, "ParentProcessId": 10, "Name": "node.exe", "CommandLine": '"node" ./mcp/server.mjs'},
            *[
                {
                    "ProcessId": 50 + process_index,
                    "ParentProcessId": 20 + process_index,
                    "Name": "node.exe",
                    "CommandLine": "xcodebuildmcp mcp",
                }
                for process_index in range(8)
            ],
            {"ProcessId": 6, "ParentProcessId": 20, "Name": "node.exe", "CommandLine": "node_repl.exe"},
        ]
    )

    assert snapshot["warning"] is True
    assert snapshot["genericMcpServerCount"] == 4
    assert snapshot["xcodebuildMcpProcessCount"] == 8
    assert snapshot["xcodebuildMcpRootCount"] == 8
    assert snapshot["nodeReplProcessCount"] == 1
    assert snapshot["nodeReplProcesses"][0]["commandLine"] == "node_repl.exe"
    assert snapshot["sampleProcesses"]


def test_node_repl_process_command_mismatch_detects_stale_runtime_path() -> None:
    snapshot = parse_mcp_process_snapshot(
        [
            {
                "ProcessId": 7,
                "ParentProcessId": 1,
                "Name": "node_repl.exe",
                "CommandLine": r'"C:\old\node_repl.exe" --disable-sandbox',
            }
        ]
    )

    mismatches = node_repl_process_command_mismatches(snapshot, r"D:\current\node_repl.exe")

    assert len(mismatches) == 1
    assert "old" in mismatches[0]


def test_node_repl_process_command_mismatch_ignores_absent_runtime_process() -> None:
    assert node_repl_process_command_mismatches({"nodeReplProcesses": []}, "") == []


def test_parse_mcp_process_snapshot_allows_many_regular_plugin_servers() -> None:
    snapshot = parse_mcp_process_snapshot(
        [
            {
                "ProcessId": process_index,
                "ParentProcessId": 10,
                "Name": "node.exe",
                "CommandLine": '"node" ./mcp/server.mjs',
            }
            for process_index in range(18)
        ]
    )

    assert snapshot["warning"] is False
    assert snapshot["genericMcpServerCount"] == 18
    assert snapshot["xcodebuildMcpProcessCount"] == 0
    assert snapshot["xcodebuildMcpRootCount"] == 0
    assert snapshot["nodeReplProcessCount"] == 0


def test_parse_mcp_process_snapshot_uses_per_parent_fanout_not_global_sum() -> None:
    distributed_processes = [
        {
            "ProcessId": parent_index * 100 + process_index,
            "ParentProcessId": parent_index,
            "Name": "node.exe",
            "CommandLine": '"node" ./mcp/server.mjs',
        }
        for parent_index in range(1, 4)
        for process_index in range(18)
    ]
    distributed = parse_mcp_process_snapshot(distributed_processes)

    assert distributed["genericMcpServerCount"] == 54
    assert distributed["maxGenericMcpPerParent"] == 18
    assert distributed["warning"] is False
    assert distributed["capacityHigh"] is True

    concentrated = parse_mcp_process_snapshot(
        [
            {
                "ProcessId": process_index,
                "ParentProcessId": 10,
                "Name": "node.exe",
                "CommandLine": '"node" ./mcp/server.mjs',
            }
            for process_index in range(32)
        ]
    )

    assert concentrated["maxGenericMcpPerParent"] == 32
    assert concentrated["warning"] is False
    assert concentrated["capacityHigh"] is True


def test_parse_mcp_process_snapshot_treats_normal_node_repl_fanout_as_expected() -> None:
    snapshot = parse_mcp_process_snapshot(
        [
            {
                "ProcessId": process_index,
                "ParentProcessId": 10,
                "Name": "node_repl.exe",
                "CommandLine": '"node_repl.exe" --runtime desktop',
            }
            for process_index in range(1, 9)
        ]
    )

    assert snapshot["warning"] is False
    assert snapshot["mcpProcessCount"] == 8
    assert snapshot["normalNodeReplProcessCount"] == 8
    assert snapshot["nodeReplRiskProcessCount"] == 0
    assert snapshot["legacyThreadMessengerProcessCount"] == 0
    assert snapshot["xcodebuildMcpProcessCount"] == 0
    assert snapshot["otherMcpServerProcessCount"] == 0


def test_capacity_trend_first_snapshot_is_atomic_bounded_and_private(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    codex_home_path = tmp_path / "private-codex-home"
    secret_title = "sensitive thread title"
    secret_prompt = "sensitive prompt body"
    metrics = {
        "sessionsBytes": 1_024,
        "largeThreadCount": 2,
        "backupBytes": 2_048,
        "backupFileCount": 4,
        "mcpProcessCount": 5,
        "normalNodeReplProcessCount": 3,
        "nodeReplRiskProcessCount": 0,
        "legacyFallbackProcessCount": 1,
        "xcodebuildProcessCount": 1,
        "otherMcpServerProcessCount": 0,
        "title": secret_title,
        "prompt": secret_prompt,
        "path": str(codex_home_path),
        "commandLine": "node --secret",
    }

    trend = record_capacity_trend(
        codex_home_path,
        metrics,
        captured_at_ms=1_767_225_600_000,
        state_root=state_root,
    )

    assert trend["storage"]["persisted"] is True
    assert trend["storage"]["recoveredFromCorruption"] is False
    assert trend["retention"] == {"cadence": "daily", "maxAgeDays": 90, "maxSnapshots": 90}
    assert trend["history"] == [{"capturedAtMs": 1_767_225_600_000, **trend["current"]}]
    assert all(change["direction"] == "unknown" for change in trend["changes"].values())

    state_path = capacity_trend_state_path(codex_home_path, state_root=state_root)
    stored_text = state_path.read_text(encoding="utf-8")
    stored_payload = json.loads(stored_text)
    assert state_path.stat().st_size < 128 * 1024
    assert stored_payload["schemaVersion"] == 1
    assert len(stored_payload["snapshots"]) == 1
    assert secret_title not in stored_text
    assert secret_prompt not in stored_text
    assert str(codex_home_path) not in stored_text
    assert "node --secret" not in stored_text
    assert not list(state_root.glob("*.tmp"))


def test_capacity_trend_atomic_write_uses_unique_temporary_files_and_cleans_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state" / "capacity-trend-test.json"
    observed_temporary_paths: list[Path] = []

    def fail_replace(source: str | Path, _target: str | Path) -> None:
        observed_temporary_paths.append(Path(source))
        raise PermissionError("simulated replace failure")

    monkeypatch.setattr(diagnostics_module.os, "replace", fail_replace)
    snapshots = [{"capturedAtMs": 1_767_225_600_000, **diagnostics_module.capacity_trend_metric_defaults}]

    with pytest.raises(PermissionError):
        diagnostics_module._write_capacity_trend_atomic(state_path, snapshots)
    with pytest.raises(PermissionError):
        diagnostics_module._write_capacity_trend_atomic(state_path, snapshots)

    assert len(observed_temporary_paths) == 2
    assert observed_temporary_paths[0] != observed_temporary_paths[1]
    assert all(path.parent == state_path.parent for path in observed_temporary_paths)
    assert all(not path.exists() for path in observed_temporary_paths)


def test_capacity_trend_reports_growth_and_decline_against_previous_day(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    codex_home_path = tmp_path / "codex"
    base_metrics = {
        "sessionsBytes": 100,
        "largeThreadCount": 4,
        "backupBytes": 200,
        "backupFileCount": 8,
        "mcpProcessCount": 10,
        "normalNodeReplProcessCount": 7,
        "nodeReplRiskProcessCount": 0,
        "legacyFallbackProcessCount": 1,
        "xcodebuildProcessCount": 2,
        "otherMcpServerProcessCount": 0,
    }
    record_capacity_trend(codex_home_path, base_metrics, captured_at_ms=1_767_225_600_000, state_root=state_root)

    growth_metrics = {**base_metrics, "sessionsBytes": 150, "backupFileCount": 12, "mcpProcessCount": 8}
    growth = record_capacity_trend(
        codex_home_path,
        growth_metrics,
        captured_at_ms=1_767_312_000_000,
        state_root=state_root,
    )

    assert growth["changes"]["sessionsBytes"] == {"direction": "up", "delta": 50, "percent": 50.0}
    assert growth["changes"]["backupFileCount"]["direction"] == "up"
    assert growth["changes"]["mcpProcessCount"]["direction"] == "down"
    assert growth["changes"]["largeThreadCount"]["direction"] == "flat"

    decline_metrics = {**growth_metrics, "sessionsBytes": 120, "largeThreadCount": 2}
    decline = record_capacity_trend(
        codex_home_path,
        decline_metrics,
        captured_at_ms=1_767_398_400_000,
        state_root=state_root,
    )

    assert decline["changes"]["sessionsBytes"]["direction"] == "down"
    assert decline["changes"]["sessionsBytes"]["delta"] == -30
    assert decline["changes"]["largeThreadCount"]["direction"] == "down"


def test_capacity_trend_coalesces_daily_samples_and_truncates_retention(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    codex_home_path = tmp_path / "codex"
    start_ms = 1_767_225_600_000
    for day_index in range(95):
        metrics = {
            "sessionsBytes": day_index,
            "largeThreadCount": day_index % 3,
            "backupBytes": day_index * 2,
            "backupFileCount": day_index,
            "mcpProcessCount": 1,
            "normalNodeReplProcessCount": 1,
            "nodeReplRiskProcessCount": 0,
            "legacyFallbackProcessCount": 0,
            "xcodebuildProcessCount": 0,
            "otherMcpServerProcessCount": 0,
        }
        trend = record_capacity_trend(
            codex_home_path,
            metrics,
            captured_at_ms=start_ms + day_index * 86_400_000,
            state_root=state_root,
        )

    assert len(trend["history"]) == 90
    assert trend["history"][0]["sessionsBytes"] == 5
    assert trend["history"][-1]["sessionsBytes"] == 94

    same_day = record_capacity_trend(
        codex_home_path,
        {**metrics, "sessionsBytes": 999},
        captured_at_ms=start_ms + 94 * 86_400_000 + 3_600_000,
        state_root=state_root,
    )
    assert len(same_day["history"]) == 90
    assert same_day["history"][-1]["sessionsBytes"] == 999


def test_capacity_trend_recovers_corrupt_snapshot_without_breaking_diagnostics(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    codex_home_path = tmp_path / "codex"
    state_path = capacity_trend_state_path(codex_home_path, state_root=state_root)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"schemaVersion":1,"snapshots":[', encoding="utf-8")

    trend = record_capacity_trend(
        codex_home_path,
        {"sessionsBytes": 10, "mcpProcessCount": 1},
        captured_at_ms=1_767_225_600_000,
        state_root=state_root,
    )

    assert trend["storage"]["persisted"] is True
    assert trend["storage"]["recoveredFromCorruption"] is True
    assert len(trend["history"]) == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["snapshots"][0]["sessionsBytes"] == 10


def test_capacity_trend_write_failure_keeps_current_diagnostics_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diagnostics_module,
        "_write_capacity_trend_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("read only")),
    )

    trend = record_capacity_trend(
        tmp_path / "codex",
        {"sessionsBytes": 10, "mcpProcessCount": 2},
        captured_at_ms=1_767_225_600_000,
        state_root=tmp_path / "state",
    )

    assert trend["current"]["sessionsBytes"] == 10
    assert trend["current"]["mcpProcessCount"] == 2
    assert trend["storage"] == {
        "persisted": False,
        "recoveredFromCorruption": False,
        "errorCode": "write_failed",
    }


def test_diagnostics_report_exposes_capacity_trend_through_response_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    monkeypatch.setattr(
        diagnostics_module,
        "scan_mcp_process_snapshot",
        lambda: {
            "available": True,
            "warning": False,
            "mcpProcessCount": 4,
            "riskProcessCount": 0,
            "genericMcpServerCount": 1,
            "otherMcpServerProcessCount": 1,
            "legacyThreadMessengerProcessCount": 0,
            "xcodebuildMcpProcessCount": 0,
            "xcodebuildMcpRootCount": 0,
            "nodeReplProcessCount": 3,
            "normalNodeReplProcessCount": 3,
            "nodeReplRiskProcessCount": 0,
            "nodeReplWithoutDisableSandboxCount": 3,
            "nodeReplWithDisableSandboxCount": 0,
            "nodeReplProcesses": [],
            "extensionHostProcessCount": 1,
            "sampleProcesses": [],
        },
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    validated_report = server.DiagnosticsResponse.model_validate(report)
    trend = validated_report.capacityTrend

    assert trend["current"]["sessionsBytes"] > 0
    assert trend["current"]["largeThreadCount"] == 0
    assert trend["current"]["mcpProcessCount"] == 4
    assert trend["current"]["normalNodeReplProcessCount"] == 3
    assert trend["history"][-1]["sessionsBytes"] == trend["current"]["sessionsBytes"]


def test_parse_mcp_process_snapshot_counts_xcodebuild_process_chain_once() -> None:
    snapshot = parse_mcp_process_snapshot(
        [
            {"ProcessId": 10, "ParentProcessId": 1, "Name": "cmd.exe", "CommandLine": "npx xcodebuildmcp mcp"},
            {"ProcessId": 11, "ParentProcessId": 10, "Name": "node.exe", "CommandLine": "npx-cli.js xcodebuildmcp mcp"},
            {"ProcessId": 12, "ParentProcessId": 11, "Name": "cmd.exe", "CommandLine": "xcodebuildmcp mcp"},
            {"ProcessId": 13, "ParentProcessId": 12, "Name": "node.exe", "CommandLine": "xcodebuildmcp/build/cli.js mcp"},
            {"ProcessId": 20, "ParentProcessId": 1, "Name": "cmd.exe", "CommandLine": "npx xcodebuildmcp mcp"},
            {"ProcessId": 21, "ParentProcessId": 20, "Name": "node.exe", "CommandLine": "npx-cli.js xcodebuildmcp mcp"},
            {"ProcessId": 22, "ParentProcessId": 21, "Name": "cmd.exe", "CommandLine": "xcodebuildmcp mcp"},
            {"ProcessId": 23, "ParentProcessId": 22, "Name": "node.exe", "CommandLine": "xcodebuildmcp/build/cli.js mcp"},
            {"ProcessId": 30, "ParentProcessId": 1, "Name": "cmd.exe", "CommandLine": "npx xcodebuildmcp mcp"},
            {"ProcessId": 31, "ParentProcessId": 30, "Name": "node.exe", "CommandLine": "npx-cli.js xcodebuildmcp mcp"},
            {"ProcessId": 32, "ParentProcessId": 31, "Name": "cmd.exe", "CommandLine": "xcodebuildmcp mcp"},
            {"ProcessId": 33, "ParentProcessId": 32, "Name": "node.exe", "CommandLine": "xcodebuildmcp/build/cli.js mcp"},
        ]
    )

    assert snapshot["warning"] is False
    assert snapshot["xcodebuildMcpProcessCount"] == 12
    assert snapshot["xcodebuildMcpRootCount"] == 3


def test_parse_json_file_reads_large_valid_json(tmp_path: Path) -> None:
    json_path = tmp_path / "large-state.json"
    json_path.write_text(json.dumps({"state": "x" * 900_000, "ok": True}), encoding="utf-8")

    result = parse_json_file(json_path)

    assert result["valid"] is True
    assert result["error"] == ""
    assert result["sizeBytes"] > 800_000
    assert result["keys"] == ["ok", "state"]


def test_find_codex_cli_candidates_reads_path_without_where_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        codex_path = tmp_path / "codex"
        codex_path.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert find_codex_cli_candidates() == [str(codex_path)]
        return

    codex_cmd_path = tmp_path / "codex.cmd"
    codex_exe_path = tmp_path / "codex.exe"
    codex_cmd_path.write_text("@echo off\n", encoding="utf-8")
    codex_exe_path.write_bytes(b"MZ")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".CMD;.EXE")

    assert find_codex_cli_candidates() == [str(codex_cmd_path), str(codex_exe_path)]


def test_inspect_codex_cli_metadata_reads_binary_version_without_console(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex.exe"
    cli_path.write_bytes(b"MZ")

    monkeypatch.setattr(
        "backend.codex_data.run_hidden_command",
        lambda *_args, **_kwargs: {
            "returnCode": 0,
            "stdout": "codex-cli 0.144.0-alpha.4\n",
            "stderr": "",
            "error": "",
        },
    )

    inspection = inspect_codex_cli_metadata(str(cli_path))

    assert inspection["version"] == "0.144.0-alpha.4"
    assert inspection["source"] == "command"
    assert inspection["error"] == ""


def test_inspect_codex_cli_metadata_does_not_use_stale_npm_version_for_config_forwarder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text(
        '@echo off\nFOR /F "tokens=1" %%A IN ("config.toml") DO echo CODEX_CLI_PATH\n',
        encoding="utf-8",
    )
    package_path = tmp_path / "node_modules" / "@openai" / "codex" / "package.json"
    package_path.parent.mkdir(parents=True)
    package_path.write_text(json.dumps({"name": "@openai/codex", "version": "0.139.0"}), encoding="utf-8")
    monkeypatch.setattr(
        "backend.codex_data.run_hidden_command",
        lambda *_args, **_kwargs: {
            "returnCode": 0,
            "stdout": "codex-cli 0.144.0-alpha.4\n",
            "stderr": "",
            "error": "",
        },
    )

    inspection = inspect_codex_cli_metadata(str(cli_path))

    assert inspection["version"] == "0.144.0-alpha.4"
    assert inspection["source"] == "command"


def test_current_codex_version_uses_healthy_native_host_cli_when_runtime_path_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    stale_cli_path = tmp_path / "missing" / "codex.exe"
    (codex_home_path / "config.toml").write_text(
        f'CODEX_CLI_PATH = "{stale_cli_path.as_posix()}"\n',
        encoding="utf-8",
    )
    path_values: dict[str, str] = {}
    for key, filename in {
        "codexCliPath": "codex.exe",
        "browserClientPath": "browser-client.mjs",
        "extensionHostPath": "extension-host.exe",
        "resourcesPath": "resources",
        "nodePath": "node.exe",
        "nodeReplPath": "node_repl.exe",
    }.items():
        path = tmp_path / "current-runtime" / filename
        if filename == "resources":
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"runtime")
        path_values[key] = str(path)
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps({"schemaVersion": 2, "entries": [{"paths": path_values}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "backend.codex_data.run_hidden_command",
        lambda *_args, **_kwargs: {
            "returnCode": 0,
            "stdout": "codex-cli 0.144.2\n",
            "stderr": "",
            "error": "",
        },
    )

    versions = detect_current_codex_versions(resolve_codex_paths(str(codex_home_path)))

    assert versions["configuredCli"]["path"] == path_values["codexCliPath"]
    assert versions["configuredCli"]["version"] == "0.144.2"
    assert versions["runtimeConfiguredCli"]["exists"] is False


def test_scan_curated_plugin_registry_uses_configured_cli_and_selected_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = tmp_path / "codex-home"
    codex_home_path.mkdir()
    cli_path = tmp_path / "codex.exe"
    cli_path.write_bytes(b"MZ")
    captured: dict[str, object] = {}

    def fake_run_hidden_command(arguments, timeout_seconds=10, environment=None):
        captured["arguments"] = list(arguments)
        captured["timeout"] = timeout_seconds
        captured["environment"] = dict(environment or {})
        return {
            "returnCode": 0,
            "stdout": json.dumps(
                {
                    "installed": [
                        {
                            "pluginId": "build-ios-apps@openai-curated",
                            "installed": True,
                            "enabled": False,
                        }
                    ],
                    "available": [],
                }
            ),
            "stderr": "",
            "error": "",
        }

    monkeypatch.setattr(diagnostics_module, "run_hidden_command", fake_run_hidden_command)
    diagnostics_module.curated_plugin_registry_cache.clear()
    result = diagnostics_module.scan_curated_plugin_registry(
        codex_home_path,
        f"CODEX_CLI_PATH = '{cli_path}'\n",
    )

    assert result["available"] is True
    assert result["installedPluginIds"] == ["build-ios-apps@openai-curated"]
    assert captured["arguments"] == [
        str(cli_path),
        "plugin",
        "list",
        "--marketplace",
        "openai-curated",
        "--available",
        "--json",
    ]
    assert captured["environment"]["CODEX_HOME"] == str(codex_home_path)


def test_scan_curated_plugin_registry_uses_healthy_native_host_cli_when_config_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = tmp_path / "codex-home"
    codex_home_path.mkdir()
    current_appx_root = tmp_path / "current-appx"
    current_paths = create_complete_native_host_paths(tmp_path / "current", current_appx_root)
    current_paths["codexHome"] = str(codex_home_path)
    current_paths["nodeModuleDirs"] = [str(tmp_path / "current" / "node_modules")]
    Path(current_paths["nodeModuleDirs"][0]).mkdir()
    current_cli_path = Path(current_paths["codexCliPath"])
    (codex_home_path / "chrome-native-hosts.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "chromeNativeHosts": [
                    {key: value for key, value in current_paths.items() if key != "nodeModuleDirs"}
                ],
            }
        ),
        encoding="utf-8",
    )
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "entries": [{"paths": current_paths}],
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_hidden_command(arguments, timeout_seconds=10, environment=None):
        captured["arguments"] = list(arguments)
        return {
            "returnCode": 0,
            "stdout": json.dumps({"installed": [], "available": []}),
            "stderr": "",
            "error": "",
        }

    monkeypatch.setattr(diagnostics_module, "run_hidden_command", fake_run_hidden_command)
    monkeypatch.setattr(
        diagnostics_module,
        "inspect_current_codex_appx_install",
        lambda: fake_current_codex_appx_install(current_appx_root),
    )
    diagnostics_module.curated_plugin_registry_cache.clear()
    result = diagnostics_module.scan_curated_plugin_registry(
        codex_home_path,
        f"CODEX_CLI_PATH = '{tmp_path / 'missing' / 'codex.exe'}'\n",
    )

    assert result["available"] is True
    assert result["cliPath"] == str(current_cli_path)
    assert captured["arguments"][0] == str(current_cli_path)


def test_inspect_codex_cli_candidate_accepts_official_npm_wrapper(tmp_path: Path) -> None:
    codex_command_path = tmp_path / ("codex.cmd" if os.name == "nt" else "codex")
    package_path = tmp_path / "node_modules" / "@openai" / "codex" / "package.json"
    package_path.parent.mkdir(parents=True)
    package_path.write_text(json.dumps({"name": "@openai/codex", "version": "0.139.0"}), encoding="utf-8")
    codex_command_path.write_text(
        '@echo off\nnode "%~dp0\\node_modules\\@openai\\codex\\bin\\codex.js" %*\n'
        if os.name == "nt"
        else '#!/bin/sh\nnode "$basedir/node_modules/@openai/codex/bin/codex.js" "$@"\n',
        encoding="utf-8",
    )

    inspection = inspect_codex_cli_candidate(str(codex_command_path))

    assert inspection["exists"] is True
    assert inspection["officialNpmWrapper"] is True
    assert inspection["packageVersion"] == "0.139.0"
    assert inspection["packagePath"] == str(package_path)


def test_diagnostics_does_not_warn_for_official_npm_codex_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    codex_command_path = tmp_path / ("codex.cmd" if os.name == "nt" else "codex")
    package_path = tmp_path / "node_modules" / "@openai" / "codex" / "package.json"
    package_path.parent.mkdir(parents=True)
    package_path.write_text(json.dumps({"name": "@openai/codex", "version": "0.139.0"}), encoding="utf-8")
    codex_command_path.write_text(
        '@echo off\nnode "%~dp0\\node_modules\\@openai\\codex\\bin\\codex.js" %*\n'
        if os.name == "nt"
        else '#!/bin/sh\nnode "$basedir/node_modules/@openai/codex/bin/codex.js" "$@"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD;.EXE")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    cli_check = next(check for check in report["checks"] if check["id"] == "environment.codex_cli")

    assert cli_check["status"] == "pass"
    assert "first_official_npm_wrapper=True" in cli_check["evidence"]
    assert "first_package_version=0.139.0" in cli_check["evidence"]
    assert not any(issue["id"] == "environment.codex_cli_shadowed" for issue in report["issues"])


def test_diagnostics_reports_missing_configured_codex_cli_path(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    missing_cli_path = tmp_path / "missing" / "codex.exe"
    (codex_home_path / "config.toml").write_text(
        f"CODEX_CLI_PATH = '{missing_cli_path}'\n",
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    cli_check = next(check for check in report["checks"] if check["id"] == "environment.codex_cli")

    assert cli_check["status"] == "warning"
    assert "CODEX_CLI_PATH_exists=False" in cli_check["evidence"]
    assert any(issue["id"] == "environment.codex_cli_configured_path_missing" for issue in report["issues"])


def test_diagnostics_warns_when_conda_codex_shadows_configured_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    configured_cli_path = tmp_path / "desktop_cli" / ("codex.exe" if os.name == "nt" else "codex")
    configured_cli_path.parent.mkdir(parents=True)
    configured_cli_path.write_bytes(b"MZ")
    (codex_home_path / "config.toml").write_text(
        f"CODEX_CLI_PATH = '{configured_cli_path}'\n",
        encoding="utf-8",
    )

    conda_path = tmp_path / "anaconda3"
    conda_path.mkdir()
    codex_command_path = conda_path / ("codex.cmd" if os.name == "nt" else "codex")
    package_path = conda_path / "node_modules" / "@openai" / "codex" / "package.json"
    package_path.parent.mkdir(parents=True)
    package_path.write_text(json.dumps({"name": "@openai/codex", "version": "0.139.0"}), encoding="utf-8")
    codex_command_path.write_text(
        '@echo off\nnode "%~dp0\\node_modules\\@openai\\codex\\bin\\codex.js" %*\n'
        if os.name == "nt"
        else '#!/bin/sh\nnode "$basedir/node_modules/@openai/codex/bin/codex.js" "$@"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(conda_path))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD;.EXE")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    cli_check = next(check for check in report["checks"] if check["id"] == "environment.codex_cli")

    assert cli_check["status"] == "warning"
    assert "first_path_differs_from_config=True" in cli_check["evidence"]
    shadow_issue = next(issue for issue in report["issues"] if issue["id"] == "environment.codex_cli_shadowed")
    shadow_issue_text = "\n".join(shadow_issue["evidence"])
    assert str(configured_cli_path) in shadow_issue_text
    assert str(codex_command_path) in shadow_issue_text


def test_diagnostics_accepts_conda_codex_config_forwarder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    configured_cli_path = tmp_path / "desktop_cli" / ("codex.exe" if os.name == "nt" else "codex")
    configured_cli_path.parent.mkdir(parents=True)
    configured_cli_path.write_bytes(b"MZ")
    (codex_home_path / "config.toml").write_text(
        f"CODEX_CLI_PATH = '{configured_cli_path}'\n",
        encoding="utf-8",
    )

    conda_path = tmp_path / "anaconda3"
    conda_path.mkdir()
    codex_command_path = conda_path / ("codex.cmd" if os.name == "nt" else "codex")
    codex_command_path.write_text(
        "@echo off\nREM reads CODEX_CLI_PATH from config.toml before forwarding\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(conda_path))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD;.EXE")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    cli_check = next(check for check in report["checks"] if check["id"] == "environment.codex_cli")

    assert cli_check["status"] == "pass"
    assert "first_path_differs_from_config=True" in cli_check["evidence"]
    assert "first_config_forwarder=True" in cli_check["evidence"]
    assert not any(issue["id"] == "environment.codex_cli_shadowed" for issue in report["issues"])


def create_test_codex_home(root_path: Path) -> Path:
    codex_home_path = root_path / "codex_home"
    sessions_path = codex_home_path / "sessions"
    sessions_path.mkdir(parents=True)
    database_path = codex_home_path / "state_5.sqlite"
    rollout_paths = []
    for index in range(3):
        rollout_path = sessions_path / f"rollout-thread-{index}.jsonl"
        rollout_path.write_text(
            "\n".join(
                [
                    '{"type":"session_meta","payload":{"id":"thread-%d","cwd":"%s"}}'
                    % (index, str(root_path / "project").replace("\\", "\\\\")),
                    '{"type":"user_message","timestamp":"2026-06-03T00:00:00Z","payload":{"text":"prompt %d"}}'
                    % index,
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(rollout_path, (1000 + index, 1000 + index))
        rollout_paths.append(rollout_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                sandbox_policy TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                has_user_event INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                archived_at INTEGER,
                git_sha TEXT,
                git_branch TEXT,
                git_origin_url TEXT,
                cli_version TEXT NOT NULL DEFAULT '',
                first_user_message TEXT NOT NULL DEFAULT '',
                agent_nickname TEXT,
                agent_role TEXT,
                memory_mode TEXT NOT NULL DEFAULT 'enabled',
                model TEXT,
                reasoning_effort TEXT,
                agent_path TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                thread_source TEXT,
                preview TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            )
            """
        )
        for index, rollout_path in enumerate(rollout_paths):
            connection.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived,
                    cli_version, first_user_message, memory_mode, model, created_at_ms, updated_at_ms, preview
                ) VALUES (?, ?, ?, ?, 'test', 'openai', ?, ?, '{}', 'never', 0, 1, 0, 'test', 'hello', 'enabled', 'gpt', ?, ?, 'preview')
                """,
                (
                    f"thread-{index}",
                    str(rollout_path),
                    100 + index,
                    100 + index,
                    str(root_path / "project"),
                    f"Thread {index}",
                    (100 + index) * 1000,
                    (100 + index) * 1000,
                ),
            )
        connection.commit()
    (codex_home_path / ".codex-global-state.json").write_text(
        '{"electron-saved-workspace-roots":[],"pinned-thread-ids":[]}',
        encoding="utf-8",
    )
    (codex_home_path / "config.toml").write_text(
        "[projects.'%s']\ntrusted = true\n" % str(root_path / "project").replace("\\", "\\\\"),
        encoding="utf-8",
    )
    (codex_home_path / "session_index.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "id": f"thread-{index}",
                    "thread_name": f"Thread {index}",
                    "updated_at": f"2026-06-03T00:00:0{index}Z",
                },
                ensure_ascii=False,
            )
            for index in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    return codex_home_path


def fake_current_codex_appx_install(appx_root: Path) -> dict[str, object]:
    return {
        "available": True,
        "installPath": str(appx_root),
        "version": "26.707.9000.0",
        "error": "",
    }


def create_complete_native_host_paths(runtime_root: Path, appx_root: Path) -> dict[str, str]:
    path_values = {
        "codexCliPath": runtime_root / "codex.exe",
        "browserClientPath": runtime_root / "browser-client.mjs",
        "extensionHostPath": runtime_root / "extension-host.exe",
        "resourcesPath": appx_root / "app" / "resources",
        "nodePath": runtime_root / "node.exe",
        "nodeReplPath": runtime_root / "node_repl.exe",
    }
    for key, path in path_values.items():
        if key == "resourcesPath":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"runtime")
    return {key: str(path) for key, path in path_values.items()}


def test_real_codex_home_requires_valid_state_database_or_consistent_markers(tmp_path: Path) -> None:
    fake_home = tmp_path / "fake-home"
    (fake_home / "sessions").mkdir(parents=True)
    (fake_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")

    assert is_real_codex_home_path(fake_home) is False

    marker_home = tmp_path / "marker-home"
    (marker_home / "sessions").mkdir(parents=True)
    (marker_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
    (marker_home / "session_index.jsonl").write_text("", encoding="utf-8")

    assert is_real_codex_home_path(marker_home) is True


def test_real_codex_home_rejects_malformed_or_unrelated_sqlite_database(tmp_path: Path) -> None:
    malformed_home = tmp_path / "malformed-home"
    malformed_home.mkdir()
    (malformed_home / "state_5.sqlite").write_bytes(b"not sqlite")
    assert is_real_codex_home_path(malformed_home) is False

    unrelated_home = tmp_path / "unrelated-home"
    unrelated_home.mkdir()
    with sqlite3.connect(unrelated_home / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")

    assert is_real_codex_home_path(unrelated_home) is False


def append_token_count_event(rollout_path: Path, timestamp: str, total_tokens: int, last_tokens: int | None = None) -> None:
    token_event = {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"total_tokens": total_tokens},
                "last_token_usage": {"total_tokens": last_tokens if last_tokens is not None else total_tokens},
            },
        },
    }
    with rollout_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(token_event, ensure_ascii=False) + "\n")


def create_test_logs_database(codex_home_path: Path, thread_id: str) -> None:
    with sqlite3.connect(codex_home_path / "logs_2.sqlite") as connection:
        connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ts_nanos INTEGER NOT NULL,
                level TEXT NOT NULL,
                target TEXT NOT NULL,
                feedback_log_body TEXT,
                module_path TEXT,
                file TEXT,
                line INTEGER,
                thread_id TEXT,
                process_uuid TEXT,
                estimated_bytes INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute("CREATE INDEX idx_logs_thread_id ON logs(thread_id)")
        connection.execute("CREATE INDEX idx_logs_thread_id_ts ON logs(thread_id, ts DESC, ts_nanos DESC, id DESC)")
        rows = [
            (200, 10, "INFO", "codex_app_server::thread_state", "app_server.request rpc.method=thread/subscribe success=true", "codex_app_server::thread_state", "thread_state.rs", 10, thread_id, "pid:1", 100),
            (201, 20, "WARN", "codex_api::endpoint::responses_websocket", "stream_request retry after transient websocket timeout", "codex_api::endpoint::responses_websocket", "responses_websocket.rs", 20, thread_id, "pid:1", 150),
            (202, 30, "ERROR", "codex_api::endpoint::responses", "run_sampling_request failed with HTTP 500 Internal Server Error", "codex_api::endpoint::responses", "responses.rs", 30, thread_id, "pid:1", 180),
            (203, 40, "INFO", "codex_app_server::other", "unrelated thread event", "codex_app_server::other", "other.rs", 40, "other-thread", "pid:1", 90),
        ]
        connection.executemany(
            """
            INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body, module_path, file, line, thread_id, process_uuid, estimated_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()


def write_minimal_sandbox_runtime(codex_home_path: Path) -> None:
    sandbox_path = codex_home_path / ".sandbox"
    sandbox_secrets_path = codex_home_path / ".sandbox-secrets"
    sandbox_bin_path = codex_home_path / ".sandbox-bin"
    sandbox_path.mkdir(exist_ok=True)
    sandbox_secrets_path.mkdir(exist_ok=True)
    sandbox_bin_path.mkdir(exist_ok=True)
    (sandbox_path / "setup_marker.json").write_text("{}", encoding="utf-8")
    (sandbox_secrets_path / "sandbox_users.json").write_text("{}", encoding="utf-8")
    (sandbox_bin_path / "codex-command-runner.exe").write_bytes(b"MZ")


def test_normalize_path_text_removes_extended_prefix() -> None:
    assert normalize_path_text(r"\\?\C:\Temp\file.txt") == r"C:\Temp\file.txt"


def test_snapshot_keeps_threads_outside_legacy_sidebar_limit_visible(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=2, validate_rollout_display=True)
    legacy_outside_threads = [
        thread
        for thread in snapshot["threads"]
        if "outside_thread_list_initial_page" in thread["hiddenReasons"]
    ]
    assert len(legacy_outside_threads) == 1
    assert legacy_outside_threads[0]["recentRank"] == 3
    assert legacy_outside_threads[0]["visibility"] == "visible"
    assert legacy_outside_threads[0]["codexVisible"] is True
    assert legacy_outside_threads[0]["outsideInitialLimit"] is False
    assert snapshot["summary"]["hiddenByInitialLimit"] == 0


def test_snapshot_thread_list_rank_alone_keeps_thread_visible_without_session_index(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "session_index.jsonl").write_text("", encoding="utf-8")

    fast_snapshot = build_snapshot(str(codex_home_path), sidebar_limit=2)
    fast_thread = next(item for item in fast_snapshot["threads"] if item["id"] == "thread-2")
    assert fast_thread["visibility"] == "visible"
    assert fast_thread["codexVisible"] is True
    assert fast_thread["rolloutDisplayStatus"] == "not_scanned"

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=2, validate_rollout_display=True)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-2")

    assert thread["threadListRank"] == 1
    assert thread["mainThreadListRank"] == 1
    assert thread["sessionIndexRank"] is None
    assert thread["visibility"] == "visible"
    assert thread["codexVisible"] is True
    assert "missing_session_index_entry" in thread["hiddenReasons"]


def test_snapshot_does_not_mark_thread_visible_when_rollout_event_stream_is_missing(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "session_index.jsonl").write_text(
        json.dumps({"id": "thread-2", "thread_name": "Thread 2", "updated_at": "2026-06-03T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "thread-2", "cwd": str(tmp_path / "project")}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "这个线程在 Codex 侧边栏不可见"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "我会检查 event_msg 可见流。"}],
            },
        },
    ]
    rollout_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")

    fast_snapshot = build_snapshot(str(codex_home_path), sidebar_limit=2)
    fast_thread = next(item for item in fast_snapshot["threads"] if item["id"] == "thread-2")

    assert fast_thread["threadListRank"] == 1
    assert fast_thread["sessionIndexRank"] == 1
    assert fast_thread["visibility"] == "visible"
    assert fast_thread["codexVisible"] is True
    assert fast_thread["rolloutDisplayStatus"] == "not_scanned"

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=2, validate_rollout_display=True)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-2")

    assert thread["threadListRank"] == 1
    assert thread["visibility"] == "needs_user_event_repair"
    assert thread["codexVisible"] is False
    assert thread["rolloutDisplayStatus"] == "missing_visible_event_stream"
    assert "missing_visible_event_stream" in thread["hiddenReasons"]


def test_snapshot_stale_session_index_rank_keeps_thread_visible_with_legacy_rank_reason(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "session_index.jsonl").write_text(
        "\n".join(
            json.dumps({"id": thread_id, "thread_name": thread_id, "updated_at": "2026-06-03T00:00:00Z"})
            for thread_id in ["thread-2", "thread-1", "thread-0"]
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=2)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-2")

    assert thread["threadListRank"] == 1
    assert thread["mainThreadListRank"] == 1
    assert thread["sessionIndexRank"] == 3
    assert thread["visibility"] == "visible"
    assert thread["codexVisible"] is True
    assert "outside_session_index_repair_window" in thread["hiddenReasons"]


def test_snapshot_uses_session_index_title_for_sidebar_match(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": "thread-1",
                "thread_name": "Codex线程瘦身",
                "updated_at": "2026-06-03T00:00:00Z",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert thread["title"] == "Codex线程瘦身"
    assert thread["sidebarTitle"] == "Codex线程瘦身"
    assert thread["sqliteTitle"] == "Thread 1"


def test_snapshot_uses_rollout_title_for_visible_conversation_sidebar(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    conversation_project_path = r"C:\Users\Example\Documents\Codex\2026-05-01\hf"
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    with rollout_path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "timestamp": "2026-05-01T10:16:45.232Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_name_updated",
                        "thread_id": "thread-1",
                        "thread_name": "排查HF卡住原因",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET cwd = ?, title = ?, has_user_event = 0 WHERE id = 'thread-1'",
            (conversation_project_path, "怎么一直不动啊，查一下我的HF到底怎么了"),
        )
        connection.commit()
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "electron-saved-workspace-roots": [],
                "pinned-thread-ids": ["thread-1"],
                "projectless-thread-ids": ["thread-1"],
                "thread-workspace-root-hints": {"thread-1": r"C:\Users\Example\Documents\Codex"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (codex_home_path / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": "thread-1",
                "thread_name": "向HF上传文件",
                "updated_at": "2026-05-23T03:57:39.3886028Z",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=1)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert thread["projectKind"] == "conversation"
    assert thread["visibility"] == "visible"
    assert thread["codexVisible"] is True
    assert thread["title"] == "排查HF卡住原因"
    assert thread["projectLabel"] == "排查HF卡住原因"
    assert thread["sidebarTitle"] == "排查HF卡住原因"
    assert thread["rolloutTitle"] == "排查HF卡住原因"
    assert thread["sessionIndexTitle"] == "向HF上传文件"
    assert thread["sqliteTitle"] == "怎么一直不动啊，查一下我的HF到底怎么了"


def test_sync_thread_sqlite_title_uses_latest_sidebar_title_without_reordering(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    index_path = codex_home_path / "session_index.jsonl"
    before_index_text = index_path.read_text(encoding="utf-8")
    index_path.write_text(
        before_index_text
        + json.dumps(
            {
                "id": "thread-1",
                "thread_name": "Codex Home Manager",
                "updated_at": "2026-06-03T10:51:13.3397908Z",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    result = sync_thread_sqlite_title(str(codex_home_path), "thread-1")

    assert result["updated"] is True
    assert result["sqliteTitleBefore"] == "Thread 1"
    assert result["sqliteTitleAfter"] == "Codex Home Manager"
    assert result["sidebarTitle"] == "Codex Home Manager"
    assert result["sessionIndexLine"] == 4
    assert result["backup"]["rowBefore"]["title"] == "Thread 1"
    assert index_path.read_text(encoding="utf-8").count("thread-1") == before_index_text.count("thread-1") + 1
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        title = connection.execute("SELECT title FROM threads WHERE id = 'thread-1'").fetchone()[0]
    assert title == "Codex Home Manager"


def test_snapshot_does_not_mark_real_user_thread_as_repair_when_has_user_event_is_stale(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    project_path = str(tmp_path / "project")
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps({"pinned-project-ids": [project_path]}, ensure_ascii=False),
        encoding="utf-8",
    )
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET has_user_event = 0 WHERE id = 'thread-1'")
        connection.commit()

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert thread["hasUserEvent"] is False
    assert thread["hasUserSignal"] is True
    assert thread["visibility"] == "visible"
    assert thread["codexVisible"] is True


def test_snapshot_does_not_mark_indexed_user_thread_as_repair_when_project_is_not_sidebar_pinned(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET has_user_event = 0 WHERE id = 'thread-1'")
        connection.commit()

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert thread["hasUserEvent"] is False
    assert thread["hasUserSignal"] is True
    assert "metadata_has_user_event_false" in thread["hiddenReasons"]
    assert thread["visibility"] == "visible"
    assert thread["codexVisible"] is True


def test_snapshot_keeps_user_thread_missing_session_index_visible_when_sqlite_has_thread_list_rank(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "session_index.jsonl").write_text(
        json.dumps({"id": "thread-0", "thread_name": "Thread 0", "updated_at": "2026-06-03T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=1)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert thread["hasUserSignal"] is True
    assert thread["visibility"] == "visible"
    assert thread["outsideInitialLimit"] is False
    assert thread["codexVisible"] is True
    assert "missing_session_index_entry" in thread["hiddenReasons"]


def test_snapshot_classifies_subagents_outside_main_sidebar_limit(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET thread_source = 'subagent', has_user_event = 0, agent_nickname = 'Turing' WHERE id = 'thread-2'"
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES ('thread-1', 'thread-2', 'closed')"
        )
        connection.commit()

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=2)
    subagent_thread = next(thread for thread in snapshot["threads"] if thread["id"] == "thread-2")

    assert snapshot["summary"]["mainThreads"] == 2
    assert snapshot["summary"]["subagentThreads"] == 1
    assert subagent_thread["threadKind"] == "subagent"
    assert subagent_thread["visibility"] == "subagent"
    assert subagent_thread["parentThreadId"] == "thread-1"
    assert subagent_thread["agentNickname"] == "Turing"
    main_thread = next(thread for thread in snapshot["threads"] if thread["id"] == "thread-1")
    assert subagent_thread["threadListRank"] == 1
    assert subagent_thread["mainThreadListRank"] is None
    assert subagent_thread["sessionIndexRank"] is None
    assert not subagent_thread["codexVisible"]
    assert main_thread["threadListRank"] == 2
    assert main_thread["mainThreadListRank"] == 1
    assert main_thread["codexVisible"] is True


def test_snapshot_raw_thread_list_rank_counts_subagents_against_sidebar_limit(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET thread_source = 'subagent', has_user_event = 0, agent_nickname = 'Turing' WHERE id = 'thread-2'"
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES ('thread-1', 'thread-2', 'closed')"
        )
        connection.commit()
    (codex_home_path / "session_index.jsonl").write_text(
        "\n".join(
            json.dumps({"id": thread_id, "thread_name": thread_id, "updated_at": "2026-06-03T00:00:00Z"})
            for thread_id in ["thread-1", "thread-0", "thread-2"]
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=1)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert thread["threadKind"] == "main"
    assert thread["threadListRank"] == 2
    assert thread["mainThreadListRank"] == 1
    assert thread["sessionIndexRank"] == 2
    assert thread["visibility"] == "visible"
    assert thread["codexVisible"] is True


def test_snapshot_aggregates_descendant_subagent_storage_for_main_thread(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_sizes = {
        thread_id: (codex_home_path / "sessions" / f"rollout-{thread_id}.jsonl").stat().st_size
        for thread_id in ["thread-0", "thread-1", "thread-2"]
    }
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET tokens_used = 100 WHERE id = 'thread-0'")
        connection.execute("UPDATE threads SET thread_source = 'subagent', tokens_used = 20 WHERE id = 'thread-1'")
        connection.execute("UPDATE threads SET thread_source = 'subagent', tokens_used = 7 WHERE id = 'thread-2'")
        connection.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES ('thread-0', 'thread-1', 'closed')"
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES ('thread-1', 'thread-2', 'closed')"
        )
        connection.commit()

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    main_thread = next(thread for thread in snapshot["threads"] if thread["id"] == "thread-0")
    child_thread = next(thread for thread in snapshot["threads"] if thread["id"] == "thread-1")

    assert main_thread["childThreadCount"] == 2
    assert main_thread["childFileSizeBytes"] == rollout_sizes["thread-1"] + rollout_sizes["thread-2"]
    assert main_thread["totalFileSizeBytes"] == sum(rollout_sizes.values())
    assert main_thread["childTokensUsed"] == 27
    assert main_thread["totalTokensUsed"] == 127
    assert child_thread["childThreadCount"] == 1
    assert child_thread["totalTokensUsed"] == 27


def test_thread_detail_includes_daily_token_usage_for_thread_tree(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-0.jsonl", "2026-06-03T12:00:00Z", 100, 100)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-0.jsonl", "2026-06-03T13:00:00Z", 160, 60)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-0.jsonl", "2026-06-04T12:00:00Z", 250, 90)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-0.jsonl", "2026-06-05T12:00:00Z", 250, 90)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-0.jsonl", "2026-06-06T12:00:00Z", 300, 50)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-1.jsonl", "2026-06-03T12:30:00Z", 30, 30)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-1.jsonl", "2026-06-04T12:30:00Z", 45, 15)
    thread_two_updated_at_ms = int(datetime_module.datetime(2026, 6, 4, 13, 30, tzinfo=datetime_module.UTC).timestamp() * 1000)
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET thread_source = 'subagent' WHERE id IN ('thread-1', 'thread-2')")
        connection.execute("UPDATE threads SET tokens_used = 20, updated_at_ms = ? WHERE id = 'thread-2'", (thread_two_updated_at_ms,))
        connection.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES ('thread-0', 'thread-1', 'closed')"
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES ('thread-1', 'thread-2', 'closed')"
        )
        connection.commit()

    detail = get_thread_detail(str(codex_home_path), "thread-0", sidebar_limit=10)
    usage = detail["dailyTokenUsage"]
    rows_by_date = {row["date"]: row for row in usage["days"]}

    assert rows_by_date["2026-06-03"]["ownTokens"] == 160
    assert rows_by_date["2026-06-03"]["childTokens"] == 30
    assert rows_by_date["2026-06-03"]["totalTokens"] == 190
    assert rows_by_date["2026-06-04"]["ownTokens"] == 90
    assert rows_by_date["2026-06-04"]["childTokens"] == 15
    assert rows_by_date["2026-06-04"]["totalTokens"] == 105
    assert rows_by_date["2026-06-04"]["childUnknownTokenThreads"] == 1
    assert rows_by_date["2026-06-04"]["unknownTokenThreads"] == 1
    assert "sqliteFallbackThreads" not in rows_by_date["2026-06-04"]
    assert rows_by_date["2026-06-04"]["hasUnknownTokens"] is True
    assert rows_by_date["2026-06-05"]["totalTokens"] == 0
    assert rows_by_date["2026-06-05"]["hasData"] is False
    assert rows_by_date["2026-06-06"]["ownTokens"] == 50
    assert rows_by_date["2026-06-06"]["totalTokens"] == 50
    assert rows_by_date["2026-06-06"]["hasData"] is True
    assert usage["summary"]["ownTokens"] == 300
    assert usage["summary"]["childTokens"] == 45
    assert usage["summary"]["totalTokens"] == 345
    assert usage["summary"]["childUnknownTokenThreads"] == 1
    assert usage["summary"]["unknownTokenThreads"] == 1
    assert usage["summary"]["unknownDays"] == 1
    assert usage["summary"]["days"] == 3
    assert usage["summary"]["activeDays"] == 3
    assert usage["summary"]["rangeDays"] == 4
    assert usage["summary"]["zeroDays"] == 1
    assert usage["summary"]["peakDate"] == "2026-06-03"
    assert usage["summary"]["zeroDeltaTokenEvents"] == 1
    assert "sqliteFallbackThreads" not in usage["summary"]


def test_thread_detail_can_skip_daily_token_usage_and_read_it_separately(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-0.jsonl", "2026-06-03T12:00:00Z", 100, 100)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-0.jsonl", "2026-06-05T12:00:00Z", 150, 50)

    lightweight_detail = get_thread_detail(str(codex_home_path), "thread-0", sidebar_limit=10, include_daily_token_usage=False)
    daily_usage = get_thread_daily_token_usage(str(codex_home_path), "thread-0", sidebar_limit=10)

    assert "dailyTokenUsage" not in lightweight_detail
    assert lightweight_detail["thread"]["id"] == "thread-0"
    assert daily_usage["summary"]["totalTokens"] == 150
    assert daily_usage["summary"]["activeDays"] == 2
    assert daily_usage["summary"]["rangeDays"] == 3
    assert daily_usage["summary"]["zeroDays"] == 1
    assert daily_usage["days"][0]["date"] == "2026-06-03"
    assert daily_usage["days"][1]["date"] == "2026-06-04"
    assert daily_usage["days"][1]["totalTokens"] == 0
    assert daily_usage["days"][1]["hasData"] is False
    assert daily_usage["days"][2]["date"] == "2026-06-05"


def test_daily_token_usage_marks_sqlite_only_threads_as_unknown(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    thread_updated_at_ms = int(datetime_module.datetime(2026, 6, 4, 13, 30, tzinfo=datetime_module.UTC).timestamp() * 1000)
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET tokens_used = 2000, updated_at_ms = ? WHERE id = 'thread-0'", (thread_updated_at_ms,))
        connection.commit()

    daily_usage = get_thread_daily_token_usage(str(codex_home_path), "thread-0", sidebar_limit=10)

    assert daily_usage["summary"]["totalTokens"] == 0
    assert daily_usage["summary"]["peakTokens"] == 0
    assert daily_usage["summary"]["days"] == 0
    assert daily_usage["summary"]["rangeDays"] == 1
    assert daily_usage["summary"]["unknownTokenThreads"] == 1
    assert "sqliteFallbackThreads" not in daily_usage["summary"]
    assert daily_usage["summary"]["unknownDays"] == 1
    assert daily_usage["days"] == [
        {
            "date": "2026-06-04",
            "ownTokens": 0,
            "childTokens": 0,
            "totalTokens": 0,
            "ownTokenEvents": 0,
            "childTokenEvents": 0,
            "ownUnknownTokenThreads": 1,
            "childUnknownTokenThreads": 0,
            "unknownTokenThreads": 1,
            "hasData": False,
            "hasUnknownTokens": True,
        }
    ]


def test_snapshot_distinguishes_workspace_projects_from_conversation_paths(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    workspace_project_path = str(tmp_path / "project")
    workspace_child_path = str(tmp_path / "project" / "child_workspace")
    conversation_project_path = r"C:\Users\Example\Documents\Codex\2026-05-18\new-chat"
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET cwd = ? WHERE id = 'thread-1'", (conversation_project_path,))
        connection.execute("UPDATE threads SET cwd = ? WHERE id = 'thread-2'", (workspace_child_path,))
        connection.commit()
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "electron-saved-workspace-roots": [workspace_project_path],
                "pinned-thread-ids": [],
                "projectless-thread-ids": ["thread-1"],
                "thread-workspace-root-hints": {"thread-1": r"C:\Users\Example\Documents\Codex"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    workspace_project = next(project for project in snapshot["projects"] if project["path"] == workspace_project_path)
    workspace_child_project = next(project for project in snapshot["projects"] if project["path"] == workspace_child_path)
    conversation_project = next(project for project in snapshot["projects"] if project["path"] == conversation_project_path)

    assert workspace_project["projectKind"] == "workspace_project"
    assert workspace_child_project["projectKind"] == "workspace_project"
    assert conversation_project["projectKind"] == "conversation"
    assert conversation_project["label"] == "Thread 1"


def test_snapshot_does_not_treat_conversation_workspace_hint_as_visible(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    conversation_project_path = r"C:\Users\Example\Documents\Codex\2026-04-23-new-chat"
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET cwd = ?, has_user_event = 0 WHERE id = 'thread-1'", (conversation_project_path,))
        connection.commit()
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "electron-saved-workspace-roots": [],
                "pinned-thread-ids": [],
                "projectless-thread-ids": ["thread-1"],
                "thread-workspace-root-hints": {"thread-1": r"C:\Users\Example\Documents\Codex"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=1)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert thread["projectKind"] == "conversation"
    assert thread["explicitSidebarReference"] is True
    assert thread["mainThreadListRank"] == 2
    assert thread["visibility"] == "visible"
    assert thread["codexVisible"] is True
    assert "outside_conversation_initial_page" in thread["hiddenReasons"]


def test_snapshot_keeps_pinned_conversation_visible_outside_initial_page(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    conversation_project_path = r"C:\Users\Example\Documents\Codex\2026-05-20\windows-vpn-wsl-pip"
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET cwd = ?, has_user_event = 0 WHERE id = 'thread-1'", (conversation_project_path,))
        connection.commit()
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "electron-saved-workspace-roots": [],
                "pinned-thread-ids": ["thread-1"],
                "projectless-thread-ids": ["thread-1"],
                "thread-workspace-root-hints": {"thread-1": r"C:\Users\Example\Documents\Codex"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=1)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert thread["projectKind"] == "conversation"
    assert thread["isPinned"] is True
    assert thread["mainThreadListRank"] == 2
    assert thread["visibility"] == "visible"
    assert thread["codexVisible"] is True


def test_show_thread_promotes_thread_and_creates_backup(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    result = show_thread_in_sidebar(str(codex_home_path), "thread-0")
    assert result["threadId"] == "thread-0"
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=1)
    first_thread = min(
        (thread for thread in snapshot["threads"] if thread["id"] == "thread-0"),
        key=lambda thread: thread["recentRank"] or 999,
    )
    assert first_thread["recentRank"] == 1
    assert result["backup"]["rowBefore"]["id"] == "thread-0"
    assert result["backup"]["sessionIndexBackupPath"]
    assert result["sessionIndexEntry"]["id"] == "thread-0"


def test_show_thread_can_skip_automatic_backup(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    result = show_thread_in_sidebar(str(codex_home_path), "thread-0", create_backup=False)

    assert result["threadId"] == "thread-0"
    assert result["backup"]["backupId"] is None
    assert result["backup"]["skipped"] is True
    assert result["backup"]["reason"] == "createBackup=false"
    assert not (tmp_path / "backups").exists()

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=1)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-0")
    assert thread["recentRank"] == 1


def test_snapshot_marks_active_thread_with_archived_rollout_as_repair_needed(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    archived_directory = codex_home_path / "archived_sessions"
    archived_directory.mkdir()
    archived_rollout_path = archived_directory / "rollout-2026-06-03T23-33-47-thread-1.jsonl"
    source_rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    archived_rollout_path.write_text(source_rollout_path.read_text(encoding="utf-8"), encoding="utf-8")
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET rollout_path = ?, archived = 0, archived_at = NULL, has_user_event = 1 WHERE id = 'thread-1'",
            (str(archived_rollout_path),),
        )
        connection.commit()

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert thread["rolloutInArchivedStore"] is True
    assert thread["visibility"] == "needs_user_event_repair"
    assert thread["codexVisible"] is False
    assert "rollout_in_archived_sessions" in thread["hiddenReasons"]


def test_show_thread_restores_archived_rollout_path_to_active_sessions(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    archived_directory = codex_home_path / "archived_sessions"
    archived_directory.mkdir()
    archived_rollout_path = archived_directory / "rollout-2026-06-03T23-33-47-thread-1.jsonl"
    source_rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    archived_rollout_path.write_text(source_rollout_path.read_text(encoding="utf-8"), encoding="utf-8")
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute(
            """
            UPDATE threads
            SET rollout_path = ?, archived = 1, archived_at = 100, has_user_event = 1
            WHERE id = 'thread-1'
            """,
            (str(archived_rollout_path),),
        )
        connection.commit()

    result = show_thread_in_sidebar(str(codex_home_path), "thread-1")
    target_rollout_path = Path(result["rolloutRestore"]["targetPath"])
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")

    assert result["rolloutRestore"]["restored"] is True
    assert result["rolloutRestore"]["sourcePath"] == str(archived_rollout_path)
    assert target_rollout_path.exists()
    assert archived_rollout_path.exists()
    assert target_rollout_path.parent == codex_home_path / "sessions" / "2026" / "06" / "03"
    assert thread["rolloutPath"] == str(target_rollout_path)
    assert thread["rolloutInArchivedStore"] is False
    assert thread["visibility"] == "visible"
    assert thread["codexVisible"] is True
    assert result["backup"]["createdResourcePaths"] == [str(target_rollout_path)]


def test_repair_user_event_promotes_thread_into_initial_sidebar_page(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET has_user_event = 0, updated_at = 1, updated_at_ms = 1000 WHERE id = 'thread-0'")
        connection.commit()

    result = repair_user_event(str(codex_home_path), "thread-0")
    assert result["threadId"] == "thread-0"
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=1)
    repaired_thread = next(thread for thread in snapshot["threads"] if thread["id"] == "thread-0")

    assert repaired_thread["hasUserEvent"] is True
    assert repaired_thread["recentRank"] == 1
    assert repaired_thread["inInitialSidebarPage"] is True
    assert result["backup"]["rowBefore"]["has_user_event"] == 0
    assert result["sessionIndexEntry"]["id"] == "thread-0"


def test_repair_user_event_restores_missing_first_user_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    monkeypatch.setattr("backend.codex_data.detect_codex_processes", lambda: [])
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute(
            """
            UPDATE threads
            SET has_user_event = 1,
                first_user_message = '',
                preview = 'preview-only prompt',
                thread_source = 'user',
                updated_at = 1,
                updated_at_ms = 1000
            WHERE id = 'thread-0'
            """
        )
        connection.commit()

    snapshot_before = build_snapshot(str(codex_home_path), sidebar_limit=10)
    thread_before = next(thread for thread in snapshot_before["threads"] if thread["id"] == "thread-0")

    assert thread_before["visibility"] == "needs_user_event_repair"
    assert thread_before["codexVisible"] is False
    assert "missing_first_user_message" in thread_before["hiddenReasons"]

    result = repair_user_event(str(codex_home_path), "thread-0")
    snapshot_after = build_snapshot(str(codex_home_path), sidebar_limit=10)
    thread_after = next(thread for thread in snapshot_after["threads"] if thread["id"] == "thread-0")

    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        first_user_message = connection.execute(
            "SELECT first_user_message FROM threads WHERE id = 'thread-0'"
        ).fetchone()[0]

    assert result["threadId"] == "thread-0"
    assert first_user_message == "preview-only prompt"
    assert thread_after["visibility"] == "visible"
    assert thread_after["codexVisible"] is True


def test_parse_rollout_stats_counts_response_item_user_messages(tmp_path: Path) -> None:
    rollout_path = tmp_path / "rollout.jsonl"
    rollout_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "new format prompt"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "answer"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    stats = parse_rollout_stats(str(rollout_path))

    assert stats["userMessages"] == 1
    assert stats["assistantMessages"] == 1


def test_export_thread_prompts_writes_markdown(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    result = export_thread_prompts(str(codex_home_path), "thread-1")
    output_path = Path(result["outputPath"])
    assert result["promptCount"] == 1
    assert output_path.exists()
    assert "prompt 1" in output_path.read_text(encoding="utf-8")


def test_read_thread_prompts_returns_prompt_records(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    result = read_thread_prompts(str(codex_home_path), "thread-1")

    assert result["threadId"] == "thread-1"
    assert result["promptCount"] == 1
    assert result["purePromptCount"] == 1
    assert result["visiblePromptCount"] == 1
    assert result["hiddenPromptCount"] == 0
    assert result["sourceCounts"] == {"user": 1}
    assert result["prompts"][0]["index"] == 1
    assert result["prompts"][0]["lineNumber"] == 2
    assert result["prompts"][0]["text"] == "prompt 1"
    assert result["prompts"][0]["pureText"] == "prompt 1"
    assert result["prompts"][0]["hasPureText"] is True
    assert result["prompts"][0]["sourceType"] == "user"
    assert result["prompts"][0]["visibleByDefault"] is True


def test_read_thread_prompts_classifies_hidden_agent_and_internal_records(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    with rollout_path.open("a", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-06-03T00:01:00Z",
                    "payload": {
                        "text": '<subagent_notification>\n{"agent_path":"child-agent","status":{"completed":"done"},"kind":"subagent"}'
                    },
                }
            )
            + "\n"
        )
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-06-03T00:02:00Z",
                    "payload": {"text": "<environment_context>\n  <cwd>D:\\\\.codex</cwd>\n</environment_context>"},
                }
            )
            + "\n"
        )
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-06-03T00:02:30Z",
                    "payload": {
                        "text": "\n".join(
                            [
                                "<heartbeat>",
                                "<automation_id>csmar</automation_id>",
                                "<current_time_iso>2026-06-11T10:24:26.199Z</current_time_iso>",
                                "<instructions>",
                                "检查并推进 CSMAR 任务",
                                "</instructions>",
                                "</heartbeat>",
                            ]
                        )
                    },
                }
            )
            + "\n"
        )
        output.write(
            json.dumps(
                {
                    "type": "response_item",
                    "timestamp": "2026-06-03T00:03:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "real response prompt"}],
                    },
                }
            )
            + "\n"
        )

    result = read_thread_prompts(str(codex_home_path), "thread-1")

    assert result["promptCount"] == 5
    assert result["purePromptCount"] == 2
    assert result["visiblePromptCount"] == 2
    assert result["hiddenPromptCount"] == 3
    assert result["sourceCounts"] == {"user": 2, "subagent": 1, "internal": 1, "automation": 1}
    assert [prompt["sourceType"] for prompt in result["prompts"]] == ["user", "subagent", "internal", "automation", "user"]
    assert [prompt["visibleByDefault"] for prompt in result["prompts"]] == [True, False, False, False, True]
    assert [prompt["hasPureText"] for prompt in result["prompts"]] == [True, False, False, False, True]
    automation_prompt = result["prompts"][3]
    assert automation_prompt["sourceLabel"] == "自动化任务"
    assert automation_prompt["pureText"] == ""


def test_read_thread_prompts_classifies_thread_delegation_records(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    with rollout_path.open("a", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-07-09T04:52:23Z",
                    "payload": {
                        "content": [
                            {
                                "type": "text",
                                "text": "\n".join(
                                    [
                                        "<codex_delegation>",
                                        "  <source_thread_id>source-thread</source_thread_id>",
                                        "  <input>只做 Codex 工具暴露回归验证，不要修改任何文件。</input>",
                                        "</codex_delegation>",
                                    ]
                                ),
                                "codexDelegation": {
                                    "sourceThreadId": "source-thread",
                                    "input": "只做 Codex 工具暴露回归验证，不要修改任何文件。",
                                },
                            }
                        ]
                    },
                }
            )
            + "\n"
        )

    result = read_thread_prompts(str(codex_home_path), "thread-1")

    assert result["promptCount"] == 2
    assert result["purePromptCount"] == 1
    assert result["visiblePromptCount"] == 1
    assert result["hiddenPromptCount"] == 1
    assert result["sourceCounts"] == {"user": 1, "delegation": 1}
    delegation_prompt = result["prompts"][1]
    assert delegation_prompt["sourceType"] == "delegation"
    assert delegation_prompt["sourceLabel"] == "线程转发"
    assert delegation_prompt["visibleByDefault"] is False
    assert delegation_prompt["pureText"] == ""
    assert delegation_prompt["hasPureText"] is False


def test_read_thread_prompts_extracts_pure_user_text_from_attachment_context_and_hides_goal_context(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    with rollout_path.open("a", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-06-03T00:01:00Z",
                    "payload": {
                        "text": "\n".join(
                            [
                                "# Files mentioned by the user:",
                                "",
                                "## codex-clipboard.png: C:/Users/example/AppData/Local/Temp/codex-clipboard.png",
                                "",
                                "## My request for Codex:",
                                "你先告诉我在哪里，我点哪都显示不了",
                                "",
                                '<image name="[Image #1]" path="C:\\\\Users\\\\example\\\\AppData\\\\Local\\\\Temp\\\\codex-clipboard.png">',
                                "</image>",
                            ]
                        )
                    },
                }
            )
            + "\n"
        )
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-06-03T00:02:00Z",
                    "payload": {
                        "text": '<codex_internal_context source="goal">\nContinue working toward the active thread goal.\n</codex_internal_context>'
                    },
                }
            )
            + "\n"
        )

    result = read_thread_prompts(str(codex_home_path), "thread-1")

    attachment_prompt = result["prompts"][1]
    goal_prompt = result["prompts"][2]

    assert result["promptCount"] == 3
    assert result["purePromptCount"] == 2
    assert result["visiblePromptCount"] == 2
    assert result["hiddenPromptCount"] == 1
    assert result["sourceCounts"] == {"user": 1, "attachment": 1, "goal": 1}
    assert attachment_prompt["sourceType"] == "attachment"
    assert attachment_prompt["sourceLabel"] == "附件上下文"
    assert attachment_prompt["visibleByDefault"] is True
    assert attachment_prompt["pureText"] == "你先告诉我在哪里，我点哪都显示不了"
    assert attachment_prompt["hasPureText"] is True
    assert "# Files mentioned by the user:" in attachment_prompt["text"]
    assert goal_prompt["sourceType"] == "goal"
    assert goal_prompt["sourceLabel"] == "续跑目标上下文"
    assert goal_prompt["visibleByDefault"] is False
    assert goal_prompt["pureText"] == ""
    assert goal_prompt["hasPureText"] is False


def test_export_thread_prompts_defaults_to_pure_text_and_can_export_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    export_root = tmp_path / "exports"
    monkeypatch.setenv("CODEX_HOME_MANAGER_EXPORT_ROOT", str(export_root))
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    with rollout_path.open("a", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-06-03T00:01:00Z",
                    "payload": {
                        "text": '<subagent_notification>\n{"agent_path":"child-agent","status":{"completed":"done"},"kind":"subagent"}'
                    },
                }
            )
            + "\n"
        )
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-06-03T00:02:00Z",
                    "payload": {
                        "text": "\n".join(
                            [
                                "<heartbeat>",
                                "<automation_id>csmar</automation_id>",
                                "<current_time_iso>2026-06-11T10:24:26.199Z</current_time_iso>",
                                "<instructions>",
                                "检查并推进 CSMAR 任务",
                                "</instructions>",
                                "</heartbeat>",
                            ]
                        )
                    },
                }
            )
            + "\n"
        )
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-06-03T00:03:00Z",
                    "payload": {
                        "text": "# Files mentioned by the user:\n\n## codex-clipboard.png: C:/temp/codex-clipboard.png\n\n## My request for Codex:\n只保留我输入的文字\n\n<image name=\"[Image #1]\" path=\"C:/temp/codex-clipboard.png\">\n</image>"
                    },
                }
            )
            + "\n"
        )
        output.write(
            json.dumps(
                {
                    "type": "user_message",
                    "timestamp": "2026-06-03T00:04:00Z",
                    "payload": {
                        "text": "<codex_delegation>\n  <source_thread_id>source-thread</source_thread_id>\n  <input>线程转发正文</input>\n</codex_delegation>"
                    },
                }
            )
            + "\n"
        )

    pure_result = export_thread_prompts(str(codex_home_path), "thread-1")
    pure_text = Path(pure_result["outputPath"]).read_text(encoding="utf-8")
    all_result = export_thread_prompts(str(codex_home_path), "thread-1", scope="all")
    all_text = Path(all_result["outputPath"]).read_text(encoding="utf-8")
    automation_result = export_thread_prompts(str(codex_home_path), "thread-1", scope="automation")
    automation_text = Path(automation_result["outputPath"]).read_text(encoding="utf-8")
    delegation_result = export_thread_prompts(str(codex_home_path), "thread-1", scope="delegation")
    delegation_text = Path(delegation_result["outputPath"]).read_text(encoding="utf-8")

    assert pure_result["promptCount"] == 2
    assert pure_result["allPromptCount"] == 5
    assert "prompt 1" in pure_text
    assert "只保留我输入的文字" in pure_text
    assert "# Files mentioned by the user:" not in pure_text
    assert "<image" not in pure_text
    assert "<subagent_notification>" not in pure_text
    assert "<heartbeat>" not in pure_text
    assert "<codex_delegation>" not in pure_text
    assert all_result["promptCount"] == 5
    assert "# Files mentioned by the user:" in all_text
    assert "<subagent_notification>" in all_text
    assert "<heartbeat>" in all_text
    assert automation_result["promptCount"] == 1
    assert automation_result["allPromptCount"] == 5
    assert "<heartbeat>" in automation_text
    assert "<automation_id>csmar</automation_id>" in automation_text
    assert delegation_result["promptCount"] == 1
    assert delegation_result["allPromptCount"] == 5
    assert "<codex_delegation>" in delegation_text
    assert "线程转发正文" in delegation_text


def test_read_thread_prompts_endpoint_is_read_only(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    client = TestClient(server.app)
    headers = local_authorization_headers(client, codex_home_path)

    response = client.get(
        "/api/threads/thread-1/prompts",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["threadId"] == "thread-1"
    assert payload["promptCount"] == 1
    assert payload["prompts"][0]["text"] == "prompt 1"


def test_duplicate_thread_creates_new_sqlite_row_and_rollout(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    target_project_path = str(tmp_path / "copied_project")
    result = duplicate_thread(str(codex_home_path), "thread-1", target_project_path=target_project_path)
    assert result["newThreadId"] != "thread-1"
    assert result["targetProjectPath"] == target_project_path
    assert Path(result["newRolloutPath"]).exists()
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    copied_thread = next(thread for thread in snapshot["threads"] if thread["id"] == result["newThreadId"])
    assert copied_thread["projectPath"] == target_project_path
    first_line = Path(copied_thread["rolloutPath"]).read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first_line)["payload"]["cwd"] == target_project_path
    session_index_text = (codex_home_path / "session_index.jsonl").read_text(encoding="utf-8")
    assert result["newThreadId"] in session_index_text
    global_state = json.loads((codex_home_path / ".codex-global-state.json").read_text(encoding="utf-8"))
    assert global_state["thread-workspace-root-hints"][result["newThreadId"]] == target_project_path


def test_hide_thread_demotes_sidebar_state_without_archiving_and_show_clears_hidden_marker(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    project_path = str(tmp_path / "project")
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "electron-saved-workspace-roots": [project_path],
                "pinned-thread-ids": ["thread-2"],
                "thread-workspace-root-hints": {"thread-2": project_path},
                "heartbeat-thread-permissions-by-id": {"thread-2": {"allowed": True}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = hide_thread_from_sidebar(str(codex_home_path), "thread-2")

    assert result["threadId"] == "thread-2"
    assert result["sessionIndexUpdate"]["removedEntries"] == 1
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    hidden_thread = next(thread for thread in snapshot["threads"] if thread["id"] == "thread-2")
    assert hidden_thread["visibility"] == "hidden"
    assert hidden_thread["codexVisible"] is False
    assert hidden_thread["archived"] is False
    assert "manually_hidden_by_manager" in hidden_thread["hiddenReasons"]
    session_index_text = (codex_home_path / "session_index.jsonl").read_text(encoding="utf-8")
    assert "thread-2" not in session_index_text
    global_state = json.loads((codex_home_path / ".codex-global-state.json").read_text(encoding="utf-8"))
    assert "thread-2" in global_state["pinned-thread-ids"]
    assert global_state["thread-workspace-root-hints"]["thread-2"] == project_path
    assert global_state["heartbeat-thread-permissions-by-id"]["thread-2"]["allowed"] is True
    assert "thread-2" in global_state["codex_home_manager-hidden-thread-ids"]

    show_thread_in_sidebar(str(codex_home_path), "thread-2")
    snapshot_after_show = build_snapshot(str(codex_home_path), sidebar_limit=10)
    visible_thread = next(thread for thread in snapshot_after_show["threads"] if thread["id"] == "thread-2")
    assert visible_thread["visibility"] == "visible"
    global_state_after_show = json.loads((codex_home_path / ".codex-global-state.json").read_text(encoding="utf-8"))
    assert "thread-2" not in global_state_after_show["codex_home_manager-hidden-thread-ids"]


def test_legacy_hidden_marker_is_respected_and_migrated_on_show(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    legacy_hidden_key = "-".join(["codex", "thread", "manager", "hidden", "thread", "ids"])
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps({legacy_hidden_key: ["thread-2"]}, ensure_ascii=False),
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    hidden_thread = next(thread for thread in snapshot["threads"] if thread["id"] == "thread-2")
    assert hidden_thread["visibility"] == "hidden"
    assert "manually_hidden_by_manager" in hidden_thread["hiddenReasons"]

    show_thread_in_sidebar(str(codex_home_path), "thread-2")
    global_state_after_show = json.loads((codex_home_path / ".codex-global-state.json").read_text(encoding="utf-8"))
    assert legacy_hidden_key not in global_state_after_show
    assert "thread-2" not in global_state_after_show["codex_home_manager-hidden-thread-ids"]


def test_archive_thread_hides_thread_and_moves_rollout_to_archive_store(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    project_path = str(tmp_path / "project")
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "electron-saved-workspace-roots": [project_path],
                "pinned-thread-ids": ["thread-1"],
                "projectless-thread-ids": ["thread-1"],
                "thread-workspace-root-hints": {"thread-1": project_path},
                "heartbeat-thread-permissions-by-id": {"thread-1": {"allowed": True}},
                "electron-persisted-atom-state": {
                    "pinned-thread-ids": ["thread-1"],
                    "projectless-thread-ids": ["thread-1"],
                    "thread-workspace-root-hints": {"thread-1": project_path},
                    "heartbeat-thread-permissions-by-id": {"thread-1": {"allowed": True}},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    snapshot_before = build_snapshot(str(codex_home_path), sidebar_limit=10)
    rollout_path = next(thread["rolloutPath"] for thread in snapshot_before["threads"] if thread["id"] == "thread-1")
    result = archive_thread(str(codex_home_path), "thread-1")
    snapshot_after = build_snapshot(str(codex_home_path), sidebar_limit=10)
    archived_thread = next(thread for thread in snapshot_after["threads"] if thread["id"] == "thread-1")
    assert archived_thread["visibility"] == "archived"
    archived_rollout_path = Path(result["rolloutArchiveUpdate"]["targetPath"])
    assert result["rolloutArchiveUpdate"]["moved"] is True
    assert not Path(rollout_path).exists()
    assert archived_rollout_path.exists()
    assert archived_rollout_path.parent == codex_home_path / "archived_sessions"
    assert archived_thread["rolloutPath"] == str(archived_rollout_path)
    assert "rollout_in_archived_sessions" in archived_thread["hiddenReasons"]
    assert result["sessionIndexUpdate"]["removedEntries"] == 1
    assert result["sidebarReferenceUpdate"]["removedPinnedThreadIds"] == 2
    assert result["sidebarReferenceUpdate"]["removedProjectlessThreadIds"] == 2
    assert result["sidebarReferenceUpdate"]["removedWorkspaceRootHints"] == 2
    assert result["sidebarReferenceUpdate"]["removedHeartbeatPermissions"] == 2
    session_index_text = (codex_home_path / "session_index.jsonl").read_text(encoding="utf-8")
    assert "thread-1" not in session_index_text
    global_state = json.loads((codex_home_path / ".codex-global-state.json").read_text(encoding="utf-8"))
    assert "thread-1" not in global_state["pinned-thread-ids"]
    assert "thread-1" not in global_state["projectless-thread-ids"]
    assert "thread-1" not in global_state["thread-workspace-root-hints"]
    assert "thread-1" not in global_state["heartbeat-thread-permissions-by-id"]
    assert "thread-1" not in global_state["electron-persisted-atom-state"]["pinned-thread-ids"]
    assert "thread-1" not in global_state["electron-persisted-atom-state"]["projectless-thread-ids"]
    assert "thread-1" not in global_state["electron-persisted-atom-state"]["thread-workspace-root-hints"]
    assert "thread-1" not in global_state["electron-persisted-atom-state"]["heartbeat-thread-permissions-by-id"]
    assert "thread-1" in global_state["codex_home_manager-hidden-thread-ids"]


def test_migrate_thread_project_updates_cwd_and_rollout_reference(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    source_project_path = str(tmp_path / "project")
    target_project_path = str(tmp_path / "new_project")
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    unrelated_project_path = str(tmp_path / "unrelated_project")
    with rollout_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"type": "session_meta", "payload": {"id": "thread-1", "cwd": source_project_path}}, ensure_ascii=False) + "\n")
        file.write(
            json.dumps(
                {
                    "type": "turn_context",
                    "payload": {
                        "cwd": source_project_path,
                        "workspaceRoots": [source_project_path, unrelated_project_path],
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        file.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "shell_command",
                        "arguments": json.dumps({"command": "pwd", "workdir": source_project_path}, ensure_ascii=False),
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    state_payload = {
        "electron-saved-workspace-roots": [source_project_path],
        "project-order": [source_project_path],
        "active-workspace-roots": [source_project_path],
        "projectless-thread-ids": ["thread-1"],
        "thread-workspace-root-hints": {"thread-1": source_project_path},
        "electron-persisted-atom-state": {
            "electron-saved-workspace-roots": [source_project_path],
            "project-order": [source_project_path],
            "active-workspace-roots": [source_project_path],
            "projectless-thread-ids": ["thread-1"],
            "thread-workspace-root-hints": {"thread-1": source_project_path},
        },
    }
    for state_name in [".codex-global-state.json", ".codex-global-state.json.bak"]:
        (codex_home_path / state_name).write_text(json.dumps(state_payload, ensure_ascii=False), encoding="utf-8")

    result = migrate_thread_project(str(codex_home_path), "thread-1", target_project_path)
    assert result["newProjectPath"] == target_project_path
    assert result["rewrite"]["sessionMetaUpdates"] == 2
    assert result["rewrite"]["turnContextUpdates"] == 1
    assert result["rewrite"]["workspaceRootUpdates"] == 1
    detail_snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    migrated_thread = next(thread for thread in detail_snapshot["threads"] if thread["id"] == "thread-1")
    assert migrated_thread["projectPath"] == target_project_path
    metadata_old_path_hits = 0
    metadata_new_path_hits = 0
    historical_tool_call_workdir = ""
    for line in Path(migrated_thread["rolloutPath"]).read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("type") in {"session_meta", "turn_context"}:
            payload = item.get("payload") or {}
            values = [payload.get("cwd"), *(payload.get("workspaceRoots") or [])]
            metadata_old_path_hits += sum(1 for value in values if value == source_project_path)
            metadata_new_path_hits += sum(1 for value in values if value == target_project_path)
        if item.get("type") == "response_item":
            historical_tool_call_workdir = json.loads(item["payload"]["arguments"])["workdir"]
    assert metadata_old_path_hits == 0
    assert metadata_new_path_hits == 4
    assert historical_tool_call_workdir == source_project_path

    for state_name in [".codex-global-state.json", ".codex-global-state.json.bak"]:
        global_state = json.loads((codex_home_path / state_name).read_text(encoding="utf-8"))
        assert global_state["thread-workspace-root-hints"]["thread-1"] == target_project_path
        assert "thread-1" not in global_state["projectless-thread-ids"]
        assert global_state["active-workspace-roots"] == [target_project_path]
        assert global_state["electron-persisted-atom-state"]["thread-workspace-root-hints"]["thread-1"] == target_project_path
        assert global_state["electron-persisted-atom-state"]["active-workspace-roots"] == [target_project_path]


def test_migrate_thread_project_requires_codex_closed_even_with_acknowledgement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    monkeypatch.setattr("backend.codex_data.detect_codex_processes", lambda: [{"imageName": "Codex.exe", "pid": "1"}])
    with pytest.raises(RuntimeError, match="cannot override"):
        migrate_thread_project(
            str(codex_home_path),
            "thread-1",
            str(tmp_path / "new_project"),
            acknowledge_codex_running_risk=True,
        )


def test_move_thread_workspace_moves_files_and_same_cwd_threads(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    source_project_path = tmp_path / "project"
    target_project_path = tmp_path / "HF"
    source_project_path.mkdir()
    target_project_path.mkdir()
    (source_project_path / ".git").mkdir()
    (source_project_path / "AGENTS.md").write_text("notes", encoding="utf-8")
    (source_project_path / "upload.py").write_text("print('ok')\n", encoding="utf-8")

    for rollout_path in sorted((codex_home_path / "sessions").glob("rollout-thread-*.jsonl")):
        thread_id = rollout_path.stem.replace("rollout-", "")
        with rollout_path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {
                            "cwd": str(source_project_path),
                            "workspaceRoots": [str(source_project_path), str(tmp_path / "other")],
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            file.write(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": thread_id,
                            "cwd": str(source_project_path),
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    state_payload = {
        "electron-saved-workspace-roots": [str(source_project_path)],
        "project-order": [str(source_project_path)],
        "active-workspace-roots": [str(source_project_path)],
        "pinned-thread-ids": ["thread-1"],
        "projectless-thread-ids": ["thread-0", "thread-1", "thread-2"],
        "thread-workspace-root-hints": {"thread-0": str(source_project_path), "thread-1": str(source_project_path)},
        "electron-persisted-atom-state": {
            "electron-saved-workspace-roots": [str(source_project_path)],
            "project-order": [str(source_project_path)],
            "active-workspace-roots": [str(source_project_path)],
            "pinned-thread-ids": ["thread-1"],
            "projectless-thread-ids": ["thread-0", "thread-1", "thread-2"],
            "thread-workspace-root-hints": {"thread-0": str(source_project_path), "thread-1": str(source_project_path)},
        },
    }
    for state_name in [".codex-global-state.json", ".codex-global-state.json.bak"]:
        (codex_home_path / state_name).write_text(json.dumps(state_payload, ensure_ascii=False), encoding="utf-8")

    preview = preview_thread_workspace_move(str(codex_home_path), "thread-1", str(target_project_path))
    assert preview["matchedThreads"] == 3
    assert preview["fileMove"]["source"]["exists"] is True
    assert preview["fileMove"]["target"]["exists"] is True
    assert preview["blockingErrors"] == []

    result = move_thread_workspace(str(codex_home_path), "thread-1", str(target_project_path))

    assert result["matchedThreads"] == 3
    assert result["fileMove"]["movedTopLevelNames"] == [".git", "AGENTS.md", "upload.py"]
    assert not any(source_project_path.iterdir())
    assert sorted(child.name for child in target_project_path.iterdir()) == [".git", "AGENTS.md", "upload.py"]

    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        rows = connection.execute("SELECT id, cwd, has_user_event FROM threads ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["thread-0", "thread-1", "thread-2"]
    assert all(normalize_path_text(row[1]) == str(target_project_path) for row in rows)
    assert all(row[2] == 1 for row in rows)

    for rollout_path in sorted((codex_home_path / "sessions").glob("rollout-thread-*.jsonl")):
        metadata_old_hits = 0
        metadata_new_hits = 0
        for line in rollout_path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item.get("type") not in {"session_meta", "turn_context"}:
                continue
            payload = item.get("payload") or {}
            values = [payload.get("cwd"), *(payload.get("workspaceRoots") or [])]
            metadata_old_hits += sum(1 for value in values if value == str(source_project_path))
            metadata_new_hits += sum(1 for value in values if value == str(target_project_path))
        assert metadata_old_hits == 0
        assert metadata_new_hits >= 2

    for state_name in [".codex-global-state.json", ".codex-global-state.json.bak"]:
        global_state = json.loads((codex_home_path / state_name).read_text(encoding="utf-8"))
        assert global_state["active-workspace-roots"] == [str(target_project_path)]
        assert str(target_project_path) in global_state["electron-saved-workspace-roots"]
        assert str(source_project_path) not in global_state["electron-saved-workspace-roots"]
        assert "thread-1" not in global_state["pinned-thread-ids"]
        assert not set(["thread-0", "thread-1", "thread-2"]) & set(global_state["projectless-thread-ids"])
        assert global_state["thread-workspace-root-hints"]["thread-2"] == str(target_project_path)
        assert global_state["electron-persisted-atom-state"]["thread-workspace-root-hints"]["thread-1"] == str(target_project_path)

    config_text = (codex_home_path / "config.toml").read_text(encoding="utf-8")
    assert str(target_project_path).replace("\\", "\\\\") in config_text
    assert str(source_project_path).replace("\\", "\\\\") not in config_text
    assert result["backup"]["backupId"]


@pytest.mark.parametrize(
    "failure_stage",
    ["after_file_move", "after_database", "after_global_state", "after_config", "after_rollouts"],
)
def test_move_thread_workspace_rolls_back_every_layer_after_injected_failure(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    source_project_path = tmp_path / "project"
    target_project_path = tmp_path / "target"
    source_project_path.mkdir()
    target_project_path.mkdir()
    (source_project_path / "AGENTS.md").write_text("transaction source", encoding="utf-8")
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps({"electron-saved-workspace-roots": [str(source_project_path)]}),
        encoding="utf-8",
    )
    (codex_home_path / ".codex-global-state.json.bak").write_text(
        json.dumps({"electron-saved-workspace-roots": [str(source_project_path)]}),
        encoding="utf-8",
    )
    tracked_paths = [
        codex_home_path / ".codex-global-state.json",
        codex_home_path / ".codex-global-state.json.bak",
        codex_home_path / "config.toml",
        *sorted((codex_home_path / "sessions").glob("rollout-thread-*.jsonl")),
    ]
    bytes_before = {path: path.read_bytes() for path in tracked_paths}
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        rows_before = connection.execute("SELECT id, cwd, has_user_event FROM threads ORDER BY id").fetchall()

    def inject(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(f"injected failure at {stage}")

    with pytest.raises(RuntimeError, match="injected failure"):
        move_thread_workspace(
            str(codex_home_path),
            "thread-1",
            str(target_project_path),
            fault_injector=inject,
        )

    assert (source_project_path / "AGENTS.md").read_text(encoding="utf-8") == "transaction source"
    assert not (target_project_path / "AGENTS.md").exists()
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        rows_after = connection.execute("SELECT id, cwd, has_user_event FROM threads ORDER BY id").fetchall()
    assert rows_after == rows_before
    assert {path: path.read_bytes() for path in tracked_paths} == bytes_before


def test_restore_workspace_move_backup_restores_files_database_and_rollouts(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    source_project_path = tmp_path / "project"
    target_project_path = tmp_path / "target"
    source_project_path.mkdir()
    target_project_path.mkdir()
    (source_project_path / "AGENTS.md").write_text("restore me", encoding="utf-8")
    rollout_paths = sorted((codex_home_path / "sessions").glob("rollout-thread-*.jsonl"))
    rollout_bytes_before = {path: path.read_bytes() for path in rollout_paths}

    move_result = move_thread_workspace(str(codex_home_path), "thread-1", str(target_project_path))
    restore_backup(move_result["backup"]["backupId"], str(codex_home_path))

    assert (source_project_path / "AGENTS.md").read_text(encoding="utf-8") == "restore me"
    assert not (target_project_path / "AGENTS.md").exists()
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        restored_cwds = [normalize_path_text(row[0]) for row in connection.execute("SELECT cwd FROM threads")]
    assert restored_cwds == [str(source_project_path)] * 3
    assert {path: path.read_bytes() for path in rollout_paths} == rollout_bytes_before


def test_slim_thread_removes_old_compacted_and_embedded_image(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    rollout_path = Path(next(thread["rolloutPath"] for thread in snapshot["threads"] if thread["id"] == "thread-1"))
    encrypted_content = "gAAAAAB-encrypted-content-should-not-change"
    with rollout_path.open("a", encoding="utf-8") as file:
        file.write('{"type":"compacted","payload":{"text":"old"}}\n')
        file.write(json.dumps({"type": "user_message", "payload": {"text": 'literal "type":"compacted" text'}}) + "\n")
        file.write(
            json.dumps(
                {
                    "type": "compacted",
                    "payload": {
                        "message": "",
                        "replacement_history": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "checkpoint prompt"},
                                    {"type": "input_image", "image_url": "data:image/png;base64,CCCC"},
                                ],
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
        file.write('{"type":"response_item","payload":{"content":[{"type":"input_image","image_url":"data:image/png;base64,AAAA"}]}}\n')
        file.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "keep this text"},
                            {"type": "input_image", "image_url": "[image omitted during earlier recovery]"},
                        ],
                    },
                }
            )
            + "\n"
        )
        file.write(json.dumps({"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": encrypted_content}}) + "\n")
    result = slim_thread(str(codex_home_path), "thread-1")
    text = rollout_path.read_text(encoding="utf-8")
    assert result["savedBytes"] > 0
    assert text.count('"type":"compacted"') == 1
    parsed_lines = [json.loads(line) for line in text.splitlines()]
    assert any(item.get("payload", {}).get("text") == 'literal "type":"compacted" text' for item in parsed_lines)
    assert "data:image/" not in text
    assert "[image omitted during earlier recovery]" not in text
    assert "keep this text" in text
    assert encrypted_content in text
    assert result["after"]["embeddedImageUrlFields"] == 0
    assert result["after"]["invalidImageUrlRefs"] == 0
    assert result["stats"]["repairedCompactedMessages"] == 1
    compacted_line = next(item for item in parsed_lines if item.get("type") == "compacted")
    assert compacted_line["payload"]["message"].startswith("Compacted checkpoint preserved")
    assert "checkpoint prompt" in compacted_line["payload"]["message"]


def test_slim_thread_preserves_main_thread_event_msg_lines(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    rollout_path = Path(next(thread["rolloutPath"] for thread in snapshot["threads"] if thread["id"] == "thread-1"))
    user_event = {
        "type": "event_msg",
        "timestamp": "2026-06-03T00:01:00Z",
        "payload": {
            "type": "user_message",
            "message": "main event must survive data:image/png;base64,AAAA",
            "images": [],
            "local_images": [],
            "text_elements": [],
        },
    }
    title_event = {
        "type": "event_msg",
        "timestamp": "2026-06-03T00:01:01Z",
        "payload": {
            "type": "thread_name_updated",
            "thread_id": "thread-1",
            "thread_name": "Thread 1",
        },
    }
    with rollout_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(user_event, ensure_ascii=False) + "\n")
        file.write(json.dumps(title_event, ensure_ascii=False) + "\n")
        file.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": "data:image/png;base64,BBBB"}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    result = slim_thread(str(codex_home_path), "thread-1")
    parsed_lines = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]

    assert user_event in parsed_lines
    assert title_event in parsed_lines
    assert result["stats"]["preservedMainThreadEventMsgLines"] == 2
    assert "data:image/png;base64,BBBB" not in rollout_path.read_text(encoding="utf-8")
    assert "main event must survive data:image/png;base64,AAAA" in rollout_path.read_text(encoding="utf-8")


def test_slim_thread_rewrites_images_inside_preserved_main_thread_event_msg(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    rollout_path = Path(next(thread["rolloutPath"] for thread in snapshot["threads"] if thread["id"] == "thread-1"))
    event_with_image = {
        "type": "event_msg",
        "timestamp": "2026-06-03T00:01:00Z",
        "payload": {
            "type": "user_message",
            "message": "keep visible text",
            "images": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
            "local_images": [],
            "text_elements": [],
        },
    }
    with rollout_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event_with_image, ensure_ascii=False) + "\n")

    result = slim_thread(str(codex_home_path), "thread-1")
    text = rollout_path.read_text(encoding="utf-8")
    parsed_lines = [json.loads(line) for line in text.splitlines()]
    preserved_events = [item for item in parsed_lines if item.get("type") == "event_msg"]

    assert result["after"]["embeddedImageUrlFields"] == 0
    assert result["after"]["invalidImageUrlRefs"] == 0
    assert result["stats"]["preservedMainThreadEventMsgLines"] == 1
    assert result["stats"]["rewrittenImageLines"] == 1
    assert "data:image/png;base64,AAAA" not in text
    assert "keep visible text" in text
    assert len(preserved_events) == 1


def test_slim_thread_refuses_rollout_thread_mismatch(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    wrong_rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET rollout_path = ? WHERE id = 'thread-1'", (str(wrong_rollout_path),))
        connection.commit()

    with pytest.raises(RuntimeError, match="rollout binding is inconsistent"):
        slim_thread(str(codex_home_path), "thread-1")


def test_read_thread_logs_filters_requests_failures_errors_and_raw_lines(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    rollout_path = Path(next(thread["rolloutPath"] for thread in snapshot["threads"] if thread["id"] == "thread-1"))
    with rollout_path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "type": "response_item",
                    "timestamp": "2026-06-03T00:01:00Z",
                    "payload": {
                        "type": "function_call",
                        "name": "web_request",
                        "arguments": {"method": "GET", "url": "https://api.example.test/data"},
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        file.write(
            json.dumps(
                {
                    "type": "response_item",
                    "timestamp": "2026-06-03T00:01:01Z",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": "HTTP 500 Internal Server Error",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        file.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-06-03T00:01:02Z",
                    "payload": {"message": "request failed after retry"},
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    request_logs = read_thread_logs(str(codex_home_path), "thread-1", kind_filter="request")
    error_logs = read_thread_logs(str(codex_home_path), "thread-1", kind_filter="error")
    searched_logs = read_thread_logs(str(codex_home_path), "thread-1", kind_filter="all", search_text="Internal Server Error")

    assert request_logs["matchedEntries"] == 1
    assert request_logs["entries"][0]["label"] == "web_request"
    assert error_logs["matchedEntries"] == 2
    assert error_logs["summary"]["bySeverity"]["error"] == 2
    assert searched_logs["matchedEntries"] == 1
    assert '"function_call_output"' in searched_logs["entries"][0]["rawLine"]


def test_read_thread_logs_includes_app_sqlite_requests_failures_and_errors(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    create_test_logs_database(codex_home_path, "thread-1")

    app_errors = read_thread_logs(str(codex_home_path), "thread-1", kind_filter="error", source_filter="app")
    app_failures = read_thread_logs(str(codex_home_path), "thread-1", kind_filter="failure", source_filter="app")
    combined_requests = read_thread_logs(str(codex_home_path), "thread-1", kind_filter="request", source_filter="all")

    assert app_errors["source"] == "app"
    assert app_errors["appLogPath"].endswith("logs_2.sqlite")
    assert app_errors["matchedEntries"] == 1
    assert app_errors["entries"][0]["severity"] == "error"
    assert "HTTP 500" in app_errors["entries"][0]["message"]
    assert app_failures["matchedEntries"] == 2
    assert any(entry["source"] == "app_sqlite" for entry in combined_requests["entries"])
    assert combined_requests["summary"]["sources"]["app_sqlite"] >= 1


def test_diagnostics_recent_log_errors_include_samples(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    create_test_logs_database(codex_home_path, "thread-1")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    log_issue = next(issue for issue in report["issues"] if issue["id"] == "logs.recent_errors")
    evidence_text = "\n".join(log_issue["evidence"])
    assert "ERROR=1" in evidence_text
    assert "WARN=1" in evidence_text
    assert "HTTP 500 Internal Server Error" in evidence_text
    assert "thread=thread-1" in evidence_text


def test_diagnostics_reports_context_window_exhaustion(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    create_test_logs_database(codex_home_path, "thread-1")
    with sqlite3.connect(codex_home_path / "logs_2.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body, module_path, file, line, thread_id, process_uuid, estimated_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                204,
                50,
                "ERROR",
                "codex_core::responses_retry",
                "Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying.",
                "codex_core::responses_retry",
                "responses_retry.rs",
                50,
                "thread-2",
                "pid:1",
                210,
            ),
        )
        connection.commit()

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    context_check = next(check for check in report["checks"] if check["id"] == "logs.context_window_exhaustion")
    assert context_check["status"] == "critical"
    assert "context window" in "\n".join(context_check["evidence"])
    context_issue = next(issue for issue in report["issues"] if issue["id"] == "logs.context_window_exhausted")
    assert context_issue["severity"] == "critical"
    assert "thread-2" in "\n".join(context_issue["evidence"])


def test_diagnostics_does_not_infer_context_exhaustion_from_cumulative_tokens(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-09T06:20:53.324Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 258400},
                            "model_context_window": 258400,
                        },
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-09T06:20:53.372Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-context-full",
                        "last_agent_message": None,
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    context_check = next(check for check in report["checks"] if check["id"] == "logs.context_window_exhaustion")
    assert context_check["status"] == "pass"
    assert not any(issue["id"] == "logs.context_window_exhausted" for issue in report["issues"])


def test_diagnostics_ignores_context_window_phrases_in_conversation_and_tool_output(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    records = [
        {
            "timestamp": "2026-07-09T06:20:53.324Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Please diagnose: Codex ran out of room in the model's context window.",
                    }
                ],
            },
        },
        {
            "timestamp": "2026-07-09T06:20:53.372Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": "The source code searches for context window and context length.",
            },
        },
    ]
    with rollout_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    context_check = next(check for check in report["checks"] if check["id"] == "logs.context_window_exhaustion")
    assert context_check["status"] == "pass"
    assert not any(issue["id"] == "logs.context_window_exhausted" for issue in report["issues"])


def test_diagnostics_reports_unresolved_structured_context_window_error(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-09T06:20:53.324Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_error",
                        "turn_id": "turn-context-full",
                        "message": "Codex ran out of room in the model's context window.",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    context_check = next(check for check in report["checks"] if check["id"] == "logs.context_window_exhaustion")
    assert context_check["status"] == "critical"
    assert "structured=turn_error" in "\n".join(context_check["evidence"])


def test_diagnostics_clears_structured_context_error_after_successful_compaction(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    records = [
        {
            "timestamp": "2026-07-09T06:20:53.324Z",
            "type": "event_msg",
            "payload": {
                "type": "turn_error",
                "turn_id": "turn-context-full",
                "message": "Codex ran out of room in the model's context window.",
            },
        },
        {
            "timestamp": "2026-07-09T06:21:00.000Z",
            "type": "compacted",
            "payload": {"message": "checkpoint installed", "replacement_history": []},
        },
        {
            "timestamp": "2026-07-09T06:21:01.000Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-after-compaction",
                "last_agent_message": "Work resumed successfully.",
            },
        },
    ]
    with rollout_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    context_check = next(check for check in report["checks"] if check["id"] == "logs.context_window_exhaustion")
    assert context_check["status"] == "pass"
    assert not any(issue["id"] == "logs.context_window_exhausted" for issue in report["issues"])


def test_rename_project_updates_threads_folder_and_config(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    source_project_path = tmp_path / "project"
    target_project_path = tmp_path / "renamed_project"
    child_source_project_path = source_project_path / "nested"
    child_target_project_path = target_project_path / "nested"
    child_source_project_path.mkdir(parents=True)
    source_path_text = str(source_project_path)
    target_path_text = str(target_project_path)
    child_source_path_text = str(child_source_project_path)
    child_target_path_text = str(child_target_project_path)
    extended_child_source_path_text = "\\\\?\\" + child_source_path_text if os.name == "nt" else child_source_path_text
    state_payload = {
        "electron-saved-workspace-roots": [source_path_text, target_path_text, source_path_text],
        "project-order": [source_path_text, source_path_text],
        "pinned-project-ids": [source_path_text],
        "active-workspace-roots": [source_path_text],
        "electron-workspace-root-labels": {source_path_text: source_project_path.name},
        "thread-workspace-root-hints": {"thread-2": child_source_path_text},
        "thread-projectless-output-directories": {"thread-2": str(source_project_path / "outputs")},
        "electron-persisted-atom-state": {
            "electron-saved-workspace-roots": [source_path_text],
            "project-order": [source_path_text],
            "pinned-project-ids": [source_path_text],
            "electron-workspace-root-labels": {source_path_text: source_project_path.name},
            "thread-workspace-root-hints": {"thread-2": extended_child_source_path_text},
        },
    }
    for state_name in [".codex-global-state.json", ".codex-global-state.json.bak"]:
        (codex_home_path / state_name).write_text(json.dumps(state_payload, ensure_ascii=False), encoding="utf-8")

    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET cwd = ? WHERE id = 'thread-2'", (extended_child_source_path_text,))
        connection.commit()
    rollout_two_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_two_path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "type": "turn_context",
                    "payload": {
                        "cwd": extended_child_source_path_text,
                        "forward": child_source_path_text.replace("\\", "/"),
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    result = rename_project(str(codex_home_path), str(source_project_path), str(target_project_path), rename_folder=True)

    assert result["updatedThreads"] == 3
    assert result["renamedFolder"] is True
    assert result["globalStateRewrite"]["state"]["status"] == "applied"
    assert result["globalStateRewrite"]["backup"]["status"] == "applied"
    assert not source_project_path.exists()
    assert target_project_path.exists()
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    paths_by_thread_id = {thread["id"]: thread["projectPath"] for thread in snapshot["threads"]}
    assert paths_by_thread_id["thread-0"] == target_path_text
    assert paths_by_thread_id["thread-1"] == target_path_text
    assert paths_by_thread_id["thread-2"] == child_target_path_text
    config_text = (codex_home_path / "config.toml").read_text(encoding="utf-8")
    assert str(source_project_path).replace("\\", "\\\\") not in config_text
    assert str(target_project_path).replace("\\", "\\\\") in config_text
    rollout_text = rollout_two_path.read_text(encoding="utf-8")
    assert source_path_text not in rollout_text
    assert extended_child_source_path_text.replace("\\", "\\\\") not in rollout_text
    assert child_target_path_text.replace("\\", "\\\\") in rollout_text

    for state_name in [".codex-global-state.json", ".codex-global-state.json.bak"]:
        state = json.loads((codex_home_path / state_name).read_text(encoding="utf-8"))
        state_text = json.dumps(state, ensure_ascii=False)
        assert source_path_text not in state_text
        assert extended_child_source_path_text not in state_text
        assert state["electron-saved-workspace-roots"].count(target_path_text) == 1
        assert state["project-order"].count(target_path_text) == 1
        assert state["pinned-project-ids"] == [target_path_text]
        assert state["active-workspace-roots"] == [target_path_text]
        assert state["electron-workspace-root-labels"][target_path_text] == target_project_path.name
        assert state["thread-workspace-root-hints"]["thread-2"] == child_target_path_text
        atom_state = state["electron-persisted-atom-state"]
        assert atom_state["electron-saved-workspace-roots"] == [target_path_text]
        assert atom_state["electron-workspace-root-labels"][target_path_text] == target_project_path.name
        assert atom_state["thread-workspace-root-hints"]["thread-2"] == child_target_path_text


def test_rename_project_requires_codex_closed_even_with_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    source_project_path = tmp_path / "project"
    target_project_path = tmp_path / "renamed_project"
    source_project_path.mkdir()
    monkeypatch.setattr(
        "backend.codex_data.detect_codex_processes",
        lambda: [{"imageName": "Codex.exe", "pid": "1234"}],
    )

    with pytest.raises(RuntimeError, match="Close Codex Desktop"):
        rename_project(
            str(codex_home_path),
            str(source_project_path),
            str(target_project_path),
            rename_folder=True,
            acknowledge_codex_running_risk=True,
        )


def test_restore_project_rename_recovers_threads_folder_config_and_rollouts(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    source_project_path = tmp_path / "project"
    target_project_path = tmp_path / "renamed_project"
    source_project_path.mkdir()
    rename_result = rename_project(str(codex_home_path), str(source_project_path), str(target_project_path), rename_folder=True)

    restore_result = restore_backup(rename_result["backup"]["backupId"])

    assert "restored 3 project thread rows" in restore_result["notes"]
    assert source_project_path.exists()
    assert not target_project_path.exists()
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    assert all(thread["projectPath"] == str(source_project_path) for thread in snapshot["threads"])
    config_text = (codex_home_path / "config.toml").read_text(encoding="utf-8")
    assert str(source_project_path).replace("\\", "\\\\") in config_text
    assert str(target_project_path).replace("\\", "\\\\") not in config_text
    rollout_text = Path(snapshot["threads"][0]["rolloutPath"]).read_text(encoding="utf-8")
    assert str(source_project_path).replace("\\", "\\\\") in rollout_text


def test_restore_duplicate_archives_created_copy(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    duplicate_result = duplicate_thread(str(codex_home_path), "thread-1")

    restore_result = restore_backup(duplicate_result["backup"]["backupId"])

    assert f"archived duplicate thread {duplicate_result['newThreadId']}" in restore_result["notes"]
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    duplicate_snapshot = next(thread for thread in snapshot["threads"] if thread["id"] == duplicate_result["newThreadId"])
    assert duplicate_snapshot["visibility"] == "archived"


def test_backup_listing_and_restore_are_bound_to_selected_codex_home(tmp_path: Path) -> None:
    first_home = create_test_codex_home(tmp_path / "first")
    second_home = create_test_codex_home(tmp_path / "second")
    first_backup = show_thread_in_sidebar(str(first_home), "thread-0")["backup"]
    second_backup = show_thread_in_sidebar(str(second_home), "thread-0")["backup"]

    first_ids = {item["backupId"] for item in list_backups(str(first_home))}
    second_ids = {item["backupId"] for item in list_backups(str(second_home))}

    assert first_ids == {first_backup["backupId"]}
    assert second_ids == {second_backup["backupId"]}
    with pytest.raises(ValueError, match="different Codex Home"):
        restore_backup(first_backup["backupId"], str(second_home))


def test_restore_aborts_when_pre_restore_backup_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    backup_id = show_thread_in_sidebar(str(codex_home_path), "thread-0")["backup"]["backupId"]

    def fail_backup(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("simulated backup failure")

    monkeypatch.setattr("backend.codex_data.create_action_backup", fail_backup)

    with pytest.raises(RuntimeError, match="pre-restore backup failed"):
        restore_backup(backup_id, str(codex_home_path))


def test_backup_api_never_exposes_or_restores_another_codex_home(tmp_path: Path) -> None:
    first_home = create_test_codex_home(tmp_path / "first-api")
    second_home = create_test_codex_home(tmp_path / "second-api")
    first_backup_id = show_thread_in_sidebar(str(first_home), "thread-0")["backup"]["backupId"]
    second_backup_id = show_thread_in_sidebar(str(second_home), "thread-0")["backup"]["backupId"]
    client = TestClient(server.app)
    first_auth = client.get("/api/auth/token", params={"codex_home": str(first_home)}).json()
    second_auth = client.get("/api/auth/token", params={"codex_home": str(second_home)}).json()

    first_response = client.get(
        "/api/backups",
        params={"codex_home": str(first_home)},
        headers={first_auth["headerName"]: first_auth["token"]},
    )
    assert first_response.status_code == 200
    assert {item["backupId"] for item in first_response.json()["backups"]} == {first_backup_id}

    cross_home_preview = client.get(
        f"/api/backups/{first_backup_id}/restore/preview",
        params={"codex_home": str(second_home)},
        headers={second_auth["headerName"]: second_auth["token"]},
    )
    assert cross_home_preview.status_code == 404
    assert second_backup_id != first_backup_id


def test_restore_rejects_tampered_manifest_before_writing(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    backup = show_thread_in_sidebar(str(codex_home_path), "thread-0")["backup"]
    manifest_path = Path(tmp_path / "backups" / backup["backupId"] / "manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["rowBefore"]["title"] = "tampered title"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="integrity"):
        restore_backup(backup["backupId"], str(codex_home_path))


def test_restore_rejects_tampered_backup_file_before_writing(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    backup = show_thread_in_sidebar(str(codex_home_path), "thread-0")["backup"]
    rollout_backup_path = Path(backup["rolloutBackupPath"])
    rollout_backup_path.write_text("tampered backup", encoding="utf-8")

    with pytest.raises(ValueError, match="integrity"):
        restore_backup(backup["backupId"], str(codex_home_path))


def test_restore_rejects_signed_manifest_with_backup_source_outside_backup_directory(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    backup = show_thread_in_sidebar(str(codex_home_path), "thread-0")["backup"]
    manifest_path = tmp_path / "backups" / backup["backupId"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    outside_path = tmp_path / "outside-rollout.jsonl"
    outside_path.write_text("outside", encoding="utf-8")
    manifest["rolloutBackupPath"] = str(outside_path)
    write_sealed_backup_manifest(manifest_path, manifest)

    with pytest.raises(ValueError, match="outside backup directory"):
        restore_backup(backup["backupId"], str(codex_home_path))


def test_restore_rejects_backup_id_path_traversal(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    outside_backup_path = tmp_path / "outside-backup" / "manifest.json"
    outside_backup_path.parent.mkdir()
    write_sealed_backup_manifest(
        outside_backup_path,
        {
            "backupId": outside_backup_path.parent.name,
            "createdAt": datetime_module.datetime.now(datetime_module.UTC).isoformat(),
            "codexHome": str(codex_home_path),
            "databasePath": str(codex_home_path / "state_5.sqlite"),
            "threadId": None,
            "action": "outside",
            "restoreMode": "home_state",
        },
    )

    with pytest.raises(ValueError, match="backup id"):
        restore_backup("../outside-backup", str(codex_home_path), create_backup=False)


def test_restore_workspace_move_rejects_missing_transaction_endpoints_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    source_project_path = tmp_path / "project"
    target_project_path = tmp_path / "target"
    source_project_path.mkdir()
    target_project_path.mkdir()
    (source_project_path / "AGENTS.md").write_text("restore me", encoding="utf-8")
    move_result = move_thread_workspace(str(codex_home_path), "thread-1", str(target_project_path))
    manifest_path = tmp_path / "backups" / move_result["backup"]["backupId"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sourceProjectPath"] = ""
    write_sealed_backup_manifest(manifest_path, manifest)

    monkeypatch.setattr(
        "backend.codex_data.rollback_workspace_move_transaction",
        lambda *_args, **_kwargs: pytest.fail("rollback must not run for an invalid transaction manifest"),
    )
    with pytest.raises(ValueError, match="source and target project paths"):
        restore_backup(move_result["backup"]["backupId"], str(codex_home_path), create_backup=False)


def test_resource_read_rejects_path_escape(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    with pytest.raises(ValueError):
        read_codex_resource(str(codex_home_path), "../outside.txt")


def test_home_overview_reports_memory_and_agents_resources(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "AGENTS.md").write_text("local instruction", encoding="utf-8")
    (codex_home_path / "memories").mkdir()
    (codex_home_path / "memories" / "MEMORY.md").write_text("memory", encoding="utf-8")

    overview = codex_home_overview(str(codex_home_path))

    resource_paths = {resource["relativePath"] for resource in overview["resources"]}
    assert "AGENTS.md" in resource_paths
    assert "memories" in resource_paths
    assert overview["summary"]["memoryExists"] is True
    assert overview["summary"]["agentsFileCount"] == 1


def test_diagnostics_reports_state_threads_and_missing_plugins(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    assert report["codexHome"] == str(codex_home_path.resolve(strict=False))
    assert report["summary"]["threadCount"] == 3
    assert report["summary"]["checks"] >= 10
    assert any(check["id"] == "sqlite.state" and check["status"] == "pass" for check in report["checks"])
    assert any(check["id"] == "threads.snapshot" and check["status"] == "pass" for check in report["checks"])
    assert any(issue["id"] == "plugins.cache_incomplete" for issue in report["issues"])
    assert report["status"] in {"critical", "warning"}
    assert "You are Codex" in report["repairPrompt"]
    assert str(codex_home_path.resolve(strict=False)) in report["repairPrompt"]
    assert "plugins.cache_incomplete" in report["repairPrompt"]
    assert "state_5.sqlite" in report["repairPrompt"]


def test_diagnostics_reports_config_toml_bom(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    (codex_home_path / "config.toml").write_bytes(b"\xef\xbb\xbfmodel = \"gpt-5.5\"\n")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    config_check = next(check for check in report["checks"] if check["id"] == "config.toml_parse")
    assert config_check["status"] == "critical"
    assert any(issue["id"] == "config.toml_utf8_bom" and issue["severity"] == "critical" for issue in report["issues"])


def test_preview_official_thread_tools_repair_detects_legacy_fallback_mcp(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "config.toml").write_text(
        """
[projects.'C:\\Project']
trusted = true

[mcp_servers.codex_thread_messenger]
command = "node"
args = ["D:\\\\.codex\\\\tools\\\\codex-thread-messenger\\\\server.mjs"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    preview = preview_official_thread_tools_repair(str(codex_home_path))

    assert preview["needsRepair"] is True
    assert preview["willModifyConfigToml"] is True
    assert preview["config"]["activeTableCount"] == 1
    assert preview["threadToolRegistry"]["threadCount"] == 3


def test_repair_official_thread_tools_comments_only_active_fallback_mcp(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "config.toml").write_text(
        """
[projects.'C:\\Project']
trusted = true

[mcp_servers.codex_thread_messenger]
command = "node"
args = ["D:\\\\.codex\\\\tools\\\\codex-thread-messenger\\\\server.mjs"]

[mcp_servers.node_repl]
command = "node_repl"
args = ["--disable-sandbox"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = repair_official_thread_tools_exposure(str(codex_home_path))
    config_text = (codex_home_path / "config.toml").read_text(encoding="utf-8")
    after_preview = preview_official_thread_tools_repair(str(codex_home_path))

    assert result["changed"] is True
    assert result["restartRequired"] is True
    assert result["backup"]["backupId"]
    assert after_preview["needsRepair"] is False
    assert "# [mcp_servers.codex_thread_messenger]" in config_text
    assert "[mcp_servers.node_repl]" in config_text


def test_official_thread_tools_preview_and_repair_cover_runtime_and_managed_config(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    fallback_config = """
[mcp_servers.codex_thread_messenger]
command = "legacy.exe"

[mcp_servers.node_repl]
command = "node_repl"
args = ["--disable-sandbox"]
""".lstrip()
    (codex_home_path / "config.toml").write_text(fallback_config, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(fallback_config, encoding="utf-8")

    preview = preview_official_thread_tools_repair(str(codex_home_path))

    assert preview["config"]["activeTableCount"] == 1
    assert preview["managedConfig"]["activeTableCount"] == 1
    assert preview["willModifyConfigToml"] is True
    assert preview["willModifyManagedConfigToml"] is True

    result = repair_official_thread_tools_exposure(str(codex_home_path))
    after_preview = preview_official_thread_tools_repair(str(codex_home_path))

    assert result["changed"] is True
    assert after_preview["needsRepair"] is False
    assert "# [mcp_servers.codex_thread_messenger]" in (codex_home_path / "config.toml").read_text(encoding="utf-8")
    assert "# [mcp_servers.codex_thread_messenger]" in (codex_home_path / "managed_config.toml").read_text(
        encoding="utf-8"
    )
    assert result["backup"]["managedConfigBackupPath"]
    assert result["backup"]["modifiedConfigPath"] == str(codex_home_path / "config.toml")
    assert result["backup"]["modifiedConfigPaths"] == [
        str(codex_home_path / "config.toml"),
        str(codex_home_path / "managed_config.toml"),
    ]

    restore_backup(result["backup"]["backupId"], str(codex_home_path))
    restored_preview = preview_official_thread_tools_repair(str(codex_home_path))

    assert restored_preview["config"]["activeTableCount"] == 1
    assert restored_preview["managedConfig"]["activeTableCount"] == 1


def test_official_thread_tools_preview_reports_only_the_layer_it_will_modify(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "config.toml").write_text(
        '[mcp_servers.node_repl]\nargs = ["--disable-sandbox"]\n',
        encoding="utf-8",
    )
    (codex_home_path / "managed_config.toml").write_text(
        '[mcp_servers.codex_thread_messenger]\ncommand = "legacy.exe"\n',
        encoding="utf-8",
    )

    preview = preview_official_thread_tools_repair(str(codex_home_path))

    assert preview["needsRepair"] is True
    assert preview["willModifyConfigToml"] is False
    assert preview["willModifyManagedConfigToml"] is True


def test_diagnostics_reports_legacy_thread_messenger_shadowing_official_tools(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    (codex_home_path / "config.toml").write_text(
        """
[projects.'C:\\Project']
trusted = true

[mcp_servers.codex_thread_messenger]
command = "node"
args = ["D:\\\\.codex\\\\tools\\\\codex-thread-messenger\\\\server.mjs"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="zh")

    exposure_check = next(check for check in report["checks"] if check["id"] == "plugins.official_thread_tools_exposure")
    assert exposure_check["status"] == "warning"
    assert any(issue["id"] == "plugins.official_thread_tools_shadowed_by_fallback_mcp" for issue in report["issues"])


def test_diagnostics_uses_managed_config_for_thread_fallback_and_apple_build_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    (codex_home_path / "managed_config.toml").write_text(
        """
[mcp_servers.codex_thread_messenger]
command = "legacy.exe"

[plugins."build-ios-apps@openai-curated"]
enabled = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostics_module,
        "scan_mcp_process_snapshot",
        lambda: {
            "available": True,
            "warning": False,
            "legacyThreadMessengerProcessCount": 0,
            "xcodebuildMcpProcessCount": 0,
            "nodeReplWithoutDisableSandboxCount": 0,
            "sampleProcesses": [],
        },
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="zh")
    checks = {check["id"]: check for check in report["checks"]}

    assert checks["plugins.official_thread_tools_exposure"]["status"] == "warning"
    assert checks["plugins.macos_plugin_disabled_on_windows"]["status"] == "critical"
    assert any("build-ios-apps@openai-curated" in item for item in checks["plugins.macos_plugin_disabled_on_windows"]["evidence"])
    assert any(issue["id"] == "plugins.macos_plugin_active_on_windows" for issue in report["issues"])


def test_diagnostics_detects_installed_apple_plugin_even_when_config_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    monkeypatch.setattr(
        diagnostics_module,
        "scan_curated_plugin_registry",
        lambda *_args, **_kwargs: {
            "available": True,
            "attempted": True,
            "cliPath": str(codex_home_path / "codex.exe"),
            "installedPluginIds": ["build-macos-apps@openai-curated"],
            "enabledPluginIds": [],
            "error": "",
        },
    )
    monkeypatch.setattr(
        diagnostics_module,
        "scan_mcp_process_snapshot",
        lambda: {
            "available": True,
            "warning": False,
            "legacyThreadMessengerProcessCount": 0,
            "xcodebuildMcpProcessCount": 0,
            "nodeReplWithoutDisableSandboxCount": 0,
            "sampleProcesses": [],
        },
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="zh")
    check = next(item for item in report["checks"] if item["id"] == "plugins.macos_plugin_disabled_on_windows")

    assert check["status"] == "critical"
    assert "build-macos-apps@openai-curated:installed=True" in check["evidence"]
    assert any(issue["id"] == "plugins.macos_plugin_active_on_windows" for issue in report["issues"])


def test_diagnostics_requires_explicit_disable_for_cached_remote_apple_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    plugin_root = (
        codex_home_path
        / "plugins"
        / "cache"
        / "openai-curated-remote"
        / "build-ios-apps"
        / "0.1.2"
    )
    plugin_root.mkdir(parents=True)
    (plugin_root / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    monkeypatch.setattr(
        diagnostics_module,
        "scan_curated_plugin_registry",
        lambda *_args, **_kwargs: {
            "available": True,
            "attempted": True,
            "cliPath": str(codex_home_path / "codex.exe"),
            "installedPluginIds": [],
            "enabledPluginIds": [],
            "error": "",
        },
    )
    monkeypatch.setattr(
        diagnostics_module,
        "scan_mcp_process_snapshot",
        lambda: {
            "available": True,
            "warning": False,
            "legacyThreadMessengerProcessCount": 0,
            "xcodebuildMcpProcessCount": 0,
            "nodeReplWithoutDisableSandboxCount": 0,
            "sampleProcesses": [],
        },
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="zh")
    check = next(item for item in report["checks"] if item["id"] == "plugins.macos_plugin_disabled_on_windows")
    assert check["status"] == "critical"
    assert "build-ios-apps@openai-curated:remote_mcp_cached=True" in check["evidence"]

    disable_text = (
        '[plugins."build-ios-apps@openai-curated"]\n'
        'enabled = false\n\n'
        '[plugins."build-macos-apps@openai-curated"]\n'
        'enabled = false\n'
    )
    (codex_home_path / "config.toml").write_text(disable_text, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(disable_text, encoding="utf-8")

    repaired_report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="zh")
    repaired_check = next(
        item for item in repaired_report["checks"] if item["id"] == "plugins.macos_plugin_disabled_on_windows"
    )
    assert repaired_check["status"] == "pass"


def test_diagnostics_does_not_report_apple_plugins_clean_when_registry_or_process_scan_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    disable_text = (
        '[plugins."build-ios-apps@openai-curated"]\n'
        'enabled = false\n\n'
        '[plugins."build-macos-apps@openai-curated"]\n'
        'enabled = false\n'
    )
    (codex_home_path / "config.toml").write_text(disable_text, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(disable_text, encoding="utf-8")
    monkeypatch.setattr(
        diagnostics_module,
        "scan_curated_plugin_registry",
        lambda *_args, **_kwargs: {
            "available": False,
            "attempted": True,
            "cliPath": "",
            "installedPluginIds": [],
            "enabledPluginIds": [],
            "error": "registry unavailable",
        },
    )
    monkeypatch.setattr(
        diagnostics_module,
        "scan_mcp_process_snapshot",
        lambda: {"available": False, "warning": False, "error": "process scan unavailable"},
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="zh")
    check = next(item for item in report["checks"] if item["id"] == "plugins.macos_plugin_disabled_on_windows")
    issue = next(item for item in report["issues"] if item["id"] == "plugins.macos_plugin_active_on_windows")

    assert check["status"] == "warning"
    assert issue["severity"] == "warning"
    assert any("registry_available=False" in item for item in check["evidence"])


def test_diagnostics_treats_account_synced_apple_plugin_marker_as_critical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    plugin_root = (
        codex_home_path
        / "plugins"
        / "cache"
        / "openai-curated-remote"
        / "build-ios-apps"
    )
    plugin_root.mkdir(parents=True)
    (plugin_root / ".codex-remote-plugin-install.json").write_text(
        '{"pluginId":"plugins~Plugin_test"}\n',
        encoding="utf-8",
    )
    disable_text = (
        '[plugins."build-ios-apps@openai-curated"]\n'
        'enabled = false\n\n'
        '[plugins."build-macos-apps@openai-curated"]\n'
        'enabled = false\n'
    )
    (codex_home_path / "config.toml").write_text(disable_text, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(disable_text, encoding="utf-8")
    monkeypatch.setattr(
        diagnostics_module,
        "scan_mcp_process_snapshot",
        lambda: {
            "available": True,
            "warning": False,
            "legacyThreadMessengerProcessCount": 0,
            "xcodebuildMcpProcessCount": 0,
            "nodeReplWithDisableSandboxCount": 0,
            "sampleProcesses": [],
        },
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    check = next(item for item in report["checks"] if item["id"] == "plugins.macos_plugin_disabled_on_windows")

    assert check["status"] == "critical"
    assert "build-ios-apps@openai-curated:remote_install_marker=True" in check["evidence"]
    assert any(issue["id"] == "plugins.macos_plugin_active_on_windows" for issue in report["issues"])


def test_diagnostics_reports_node_repl_effective_layer_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    trusted_hash = "a" * 64
    (codex_home_path / "config.toml").write_text(
        f"""
[mcp_servers.node_repl]
command = "D:/runtime/node_repl.exe"
args = ["--disable-sandbox"]

[mcp_servers.node_repl.env]
NODE_REPL_NODE_MODULE_DIRS = "D:/runtime/node_modules"
NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = "{trusted_hash}"
""".lstrip(),
        encoding="utf-8",
    )
    (codex_home_path / "managed_config.toml").write_text(
        """
[mcp_servers.node_repl]
command = "C:/missing/managed-node-repl.exe"
args = []

[mcp_servers.node_repl.env]
NODE_REPL_NODE_MODULE_DIRS = "C:/stale/node_modules"
NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = "stale"
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostics_module,
        "scan_mcp_process_snapshot",
        lambda: {
            "available": True,
            "warning": False,
            "legacyThreadMessengerProcessCount": 0,
            "xcodebuildMcpProcessCount": 0,
            "nodeReplWithoutDisableSandboxCount": 0,
            "sampleProcesses": [],
        },
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    checks = {check["id"]: check for check in report["checks"]}

    assert checks["plugins.node_repl_config_layer_consistency"]["status"] == "warning"
    assert checks["plugins.node_repl_desktop_privileged_mode"]["status"] == "pass"
    assert checks["plugins.computer_use_privileged_runtime"]["status"] == "critical"
    assert any(issue["id"] == "plugins.node_repl_config_layer_conflict" for issue in report["issues"])
    assert any("NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S" in evidence for evidence in checks["plugins.node_repl_config_layer_consistency"]["evidence"])
    assert any("command:" in evidence for evidence in checks["plugins.node_repl_config_layer_consistency"]["evidence"])


def test_diagnostics_reports_active_sandbox_setup_error(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    (codex_home_path / "setup_error.json").write_text(
        '{"error":"CreateProcessWithLogonW failed: 1326"}',
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    sandbox_check = next(check for check in report["checks"] if check["id"] == "sandbox.setup_state")
    assert sandbox_check["status"] == "critical"
    assert any(issue["id"] == "sandbox.active_setup_error" for issue in report["issues"])
    assert "CreateProcessWithLogonW failed: 1326" in "\n".join(sandbox_check["evidence"])


def test_diagnostics_reports_pwsh_windowsapps_alias_risk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    alias_path = r"C:\Users\Example\AppData\Local\Microsoft\WindowsApps\pwsh.exe"
    monkeypatch.setattr("backend.diagnostics.shutil.which", lambda command: alias_path if command.startswith("pwsh") else None)

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    pwsh_check = next(check for check in report["checks"] if check["id"] == "runtime.pwsh_resolution")
    assert pwsh_check["status"] == "warning"
    assert any(issue["id"] == "runtime.pwsh_windowsapps_alias_risk" for issue in report["issues"])
    assert alias_path in "\n".join(pwsh_check["evidence"])


def test_diagnostics_reports_user_level_codex_home_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows user environment registry check only applies on Windows")
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    monkeypatch.setenv("CODEX_HOME", str(codex_home_path))
    monkeypatch.setattr(
        "backend.diagnostics.read_windows_user_environment_variable",
        lambda name: str(tmp_path / "other_codex_home") if name == "CODEX_HOME" else "",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    user_env_check = next(check for check in report["checks"] if check["id"] == "environment.user_codex_home")
    assert user_env_check["status"] == "warning"
    assert any(issue["id"] == "environment.user_codex_home_mismatch" for issue in report["issues"])


def test_diagnostics_reports_archived_thread_sidebar_references(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    project_path = str(tmp_path / "project")
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET archived = 1, archived_at = 123 WHERE id = 'thread-1'")
        connection.commit()
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "pinned-thread-ids": ["thread-1"],
                "thread-workspace-root-hints": {"thread-1": project_path},
                "heartbeat-thread-permissions-by-id": {"thread-1": {"allowed": True}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    archived_reference_check = next(check for check in report["checks"] if check["id"] == "threads.archived_sidebar_references")
    assert archived_reference_check["status"] == "warning"
    assert "thread-1" in "\n".join(archived_reference_check["evidence"])
    assert any(issue["id"] == "threads.archived_sidebar_references" for issue in report["issues"])


def test_diagnostics_allows_archived_threads_to_remain_in_session_index(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET archived = 1, archived_at = 123 WHERE id = 'thread-1'")
        connection.commit()

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    archived_reference_check = next(check for check in report["checks"] if check["id"] == "threads.archived_sidebar_references")
    assert archived_reference_check["status"] == "pass"
    assert "session_index_archived=1" in archived_reference_check["evidence"]
    assert "sidebar_refs=0" in archived_reference_check["evidence"]
    assert not any(issue["id"] == "threads.archived_sidebar_references" for issue in report["issues"])


def test_diagnostics_treats_interrupted_empty_thread_shell_as_info(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-0.jsonl"
    rollout_path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False)
            for record in [
                {"type": "session_meta", "payload": {"id": "thread-0", "cwd": str(tmp_path / "new-chat")}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "<turn_aborted>\nThe user interrupted this turn."}],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_name_updated",
                        "thread_id": "thread-0",
                        "thread_name": "检查本地网络影响",
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute(
            """
            UPDATE threads
            SET title = '',
                first_user_message = '',
                preview = '检查本地网络影响',
                has_user_event = 1,
                updated_at = 100,
                updated_at_ms = 100000
            WHERE id = 'thread-0'
            """
        )
        connection.commit()
    (codex_home_path / "session_index.jsonl").write_text(
        json.dumps({"id": "thread-0", "thread_name": "检查本地网络影响", "updated_at": "2026-06-13T00:00:00Z"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    empty_shell_check = next(check for check in report["checks"] if check["id"] == "threads.empty_shells")
    assert empty_shell_check["status"] == "info"
    assert "thread-0" in "\n".join(empty_shell_check["evidence"])
    needs_repair_issue = next((issue for issue in report["issues"] if issue["id"] == "threads.needs_visibility_repair"), None)
    assert needs_repair_issue is None or "thread-0" not in "\n".join(needs_repair_issue["evidence"])


def test_diagnostics_reports_explicit_sidebar_reference_without_session_index(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    project_path = str(tmp_path / "project")
    session_index_path = codex_home_path / "session_index.jsonl"
    session_index_lines = [
        line
        for line in session_index_path.read_text(encoding="utf-8").splitlines()
        if '"thread-1"' not in line
    ]
    session_index_path.write_text("\n".join(session_index_lines) + "\n", encoding="utf-8")
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "heartbeat-thread-permissions-by-id": {"thread-1": {"allowed": True}},
                "thread-workspace-root-hints": {"thread-1": project_path},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    sidebar_reference_check = next(check for check in report["checks"] if check["id"] == "threads.explicit_sidebar_without_session_index")
    assert sidebar_reference_check["status"] == "warning"
    assert "thread-1" in "\n".join(sidebar_reference_check["evidence"])
    assert any(issue["id"] == "threads.explicit_sidebar_without_session_index" for issue in report["issues"])


def test_diagnostics_does_not_treat_heartbeat_permission_as_sidebar_reference(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    session_index_path = codex_home_path / "session_index.jsonl"
    session_index_lines = [
        line
        for line in session_index_path.read_text(encoding="utf-8").splitlines()
        if '"thread-1"' not in line
    ]
    session_index_path.write_text("\n".join(session_index_lines) + "\n", encoding="utf-8")
    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "heartbeat-thread-permissions-by-id": {"thread-1": {"allowed": True}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    thread = next(item for item in snapshot["threads"] if item["id"] == "thread-1")
    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    assert thread["explicitSidebarReference"] is False
    sidebar_reference_check = next(check for check in report["checks"] if check["id"] == "threads.explicit_sidebar_without_session_index")
    assert sidebar_reference_check["status"] == "pass"
    assert not any(issue["id"] == "threads.explicit_sidebar_without_session_index" for issue in report["issues"])


def test_diagnostics_reports_main_thread_missing_event_msg_stream(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(tmp_path / "project")}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "请继续修复 Codex Home Manager 的线程可见性问题"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "我会先核对 state_5.sqlite 和 rollout JSONL。"}],
            },
        },
    ]
    rollout_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    event_stream_check = next(check for check in report["checks"] if check["id"] == "threads.main_event_stream")
    assert event_stream_check["status"] == "critical"
    assert "thread-1" in "\n".join(event_stream_check["evidence"])
    assert any(issue["id"] == "threads.main_event_stream_missing" for issue in report["issues"])


def test_event_stream_gate_never_passes_when_sampling_truncates_hidden_damage(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(tmp_path / "project")}},
        {"type": "developer_message", "payload": {"text": "x" * 3_100_000}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hidden user message"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hidden assistant message"}],
            },
        },
    ]
    rollout_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    sampled_report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")
    sampled_check = next(check for check in sampled_report["checks"] if check["id"] == "threads.main_event_stream")
    assert sampled_check["status"] == "warning"
    assert "truncated_scans=1" in sampled_check["evidence"]

    comprehensive_report = run_codex_diagnostics(
        str(codex_home_path),
        sidebar_limit=10,
        language="en",
        comprehensive_event_stream=True,
    )
    comprehensive_check = next(
        check for check in comprehensive_report["checks"] if check["id"] == "threads.main_event_stream"
    )
    assert comprehensive_check["status"] == "critical"
    assert "truncated_scans=0" in comprehensive_check["evidence"]


def test_diagnostics_reports_invalid_main_thread_image_url(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(tmp_path / "project")}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "继续检查这个线程"},
                    {"type": "input_image", "image_url": "[image omitted during thread recovery]"},
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "继续检查这个线程"},
        },
    ]
    rollout_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    rollout_check = next(check for check in report["checks"] if check["id"] == "threads.rollout_jsonl_integrity")
    assert rollout_check["status"] == "critical"
    assert "invalid_image_url_threads=1" in "\n".join(rollout_check["evidence"])
    assert any(issue["id"] == "threads.rollout_invalid_image_url" for issue in report["issues"])


def test_diagnostics_ignores_image_url_json_schema_properties(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(tmp_path / "project")}},
        {
            "type": "response_item",
            "payload": {
                "type": "tool_definition",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "image_url": {
                            "type": "string",
                            "description": "Image URL when type is image.",
                        },
                    },
                },
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "检查工具定义"}],
            },
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "检查工具定义"}},
    ]
    rollout_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    rollout_check = next(check for check in report["checks"] if check["id"] == "threads.rollout_jsonl_integrity")
    assert "invalid_image_url_threads=0" in "\n".join(rollout_check["evidence"])
    assert not any(issue["id"] == "threads.rollout_invalid_image_url" for issue in report["issues"])


def test_diagnostics_keeps_valid_embedded_images_and_compactions_out_of_integrity_status(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "保留这张图片"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                ],
            },
        },
        {"type": "compacted", "payload": {"message": "first", "replacement_history": []}},
        {"type": "compacted", "payload": {"message": "second", "replacement_history": []}},
    ]
    with rollout_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    rollout_check = next(check for check in report["checks"] if check["id"] == "threads.rollout_jsonl_integrity")
    assert rollout_check["status"] == "pass"
    assert "embedded_image_threads=1" in "\n".join(rollout_check["evidence"])
    assert "repeated_compacted_threads=1" in "\n".join(rollout_check["evidence"])
    assert any(issue["id"] == "threads.rollout_size_bloat" for issue in report["issues"])


def test_diagnostics_accepts_missing_legacy_title_event_when_persisted_titles_agree(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-1.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(tmp_path / "project")}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "这个线程的对话还是不对，继续检查尾部记录"}],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "这个线程的对话还是不对，继续检查尾部记录",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "先对比 response_item 和 event_msg。"}],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "先对比 response_item 和 event_msg。",
            },
        },
    ]
    rollout_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET title = ? WHERE id = 'thread-1'", ("Codex Home Manager",))
        connection.commit()
    session_index_path = codex_home_path / "session_index.jsonl"
    session_index_records = [
        json.loads(line)
        for line in session_index_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for record in session_index_records:
        if record["id"] == "thread-1":
            record["thread_name"] = "Codex Home Manager"
    session_index_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in session_index_records) + "\n",
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    title_stream_check = next(check for check in report["checks"] if check["id"] == "threads.main_title_stream")
    assert title_stream_check["status"] == "pass"
    assert "thread-1" in "\n".join(title_stream_check["evidence"])
    assert not any(issue["id"] == "threads.main_title_event_missing" for issue in report["issues"])


def test_diagnostics_rejects_active_legacy_notify_without_historical_os206_log(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    config_path = codex_home_path / "config.toml"
    config_path.write_text('notify = ["legacy-hook.exe"]\n', encoding="utf-8")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    check = next(check for check in report["checks"] if check["id"] == "runtime.legacy_notify_hook")
    assert check["status"] == "warning"
    assert any(issue["id"] == "runtime.legacy_notify_hook_active" for issue in report["issues"])


def test_diagnostics_does_not_treat_nested_notify_array_as_legacy_top_level_hook(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "config.toml").write_text(
        '[desktop]\nnotify = ["nested-setting"]\n',
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    check = next(check for check in report["checks"] if check["id"] == "runtime.legacy_notify_hook")
    assert check["status"] == "pass"


def test_diagnostics_rejects_unbound_computer_use_notify_even_when_helper_exists(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    notify_path = (
        tmp_path
        / "runtime"
        / "node_modules"
        / "@oai"
        / "sky"
        / "bin"
        / "windows"
        / "codex-computer-use.exe"
    )
    notify_path.parent.mkdir(parents=True)
    notify_path.write_bytes(b"official-runtime-helper")
    (codex_home_path / "config.toml").write_text(
        f"notify = ['{notify_path.as_posix()}', 'turn-ended']\n",
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    check = next(check for check in report["checks"] if check["id"] == "runtime.legacy_notify_hook")
    assert check["status"] == "warning"
    assert "desktop_managed_notify=False" in check["evidence"]
    assert any(issue["id"] == "runtime.legacy_notify_hook_active" for issue in report["issues"])


def create_notify_logs_database(database_path: Path) -> None:
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


def insert_notify_log(
    database_path: Path,
    *,
    log_id: int,
    epoch: int,
    process_uuid: str,
    target: str = "codex_core::hook_runtime",
    message: str = "legacy_notify failed to spawn: os error 206",
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO logs (id, ts, ts_nanos, level, target, feedback_log_body, process_uuid)
            VALUES (?, ?, ?, 'WARN', ?, ?, ?)
            """,
            (log_id, epoch, log_id * 10, target, message, process_uuid),
        )


def test_notify_os206_query_ignores_previous_process_history_and_applies_all_boundaries(tmp_path: Path) -> None:
    database_path = tmp_path / "logs_2.sqlite"
    create_notify_logs_database(database_path)
    insert_notify_log(database_path, log_id=1, epoch=100, process_uuid="previous-process")
    insert_notify_log(database_path, log_id=2, epoch=201, process_uuid="current-process", target="codex_core::other")
    insert_notify_log(
        database_path,
        log_id=3,
        epoch=202,
        process_uuid="current-process",
        message="legacy_notify completed without a Windows error",
    )
    insert_notify_log(database_path, log_id=4, epoch=203, process_uuid="current-process")
    insert_notify_log(database_path, log_id=5, epoch=204, process_uuid="current-process")

    result = query_legacy_notify_os206(
        database_path,
        min_epoch=200,
        process_uuid="current-process",
        after_id=3,
        max_id=4,
    )

    assert result["match_count"] == 1
    assert [item["id"] for item in result["matches"]] == [4]
    assert result["process_uuid"] == "current-process"
    assert result["min_epoch"] == 200
    assert result["after_id"] == 3
    assert result["max_id"] == 4
    assert result["unexpected_process_uuids"] == []


def test_notify_os206_query_checks_every_uuid_bound_to_the_desktop_process_tree(tmp_path: Path) -> None:
    database_path = tmp_path / "logs_2.sqlite"
    create_notify_logs_database(database_path)
    insert_notify_log(
        database_path,
        log_id=1,
        epoch=200,
        process_uuid="pid:200:main",
        message="legacy_notify completed",
    )
    insert_notify_log(
        database_path,
        log_id=2,
        epoch=201,
        process_uuid="pid:300:plugin",
    )
    insert_notify_log(
        database_path,
        log_id=3,
        epoch=202,
        process_uuid="pid:999:unrelated",
        message="unrelated runtime activity",
    )

    result = query_legacy_notify_os206(
        database_path,
        min_epoch=200,
        process_uuids=["pid:200:main", "pid:300:plugin"],
        after_id=0,
        max_id=3,
    )

    assert result["process_uuids"] == ["pid:200:main", "pid:300:plugin"]
    assert result["match_count"] == 1
    assert [item["process_uuid"] for item in result["matches"]] == ["pid:300:plugin"]


def test_notify_log_boundary_tracks_current_process_and_max_id(tmp_path: Path) -> None:
    database_path = tmp_path / "logs_2.sqlite"
    create_notify_logs_database(database_path)
    insert_notify_log(database_path, log_id=1, epoch=100, process_uuid="pid:900:previous-process")
    insert_notify_log(database_path, log_id=2, epoch=200, process_uuid="pid:200:current-process", message="startup")
    insert_notify_log(database_path, log_id=3, epoch=201, process_uuid="pid:200:current-process", message="ready")
    appx_root = tmp_path / "current-appx"
    desktop_path = appx_root / "app" / "ChatGPT.exe"
    codex_path = appx_root / "app" / "resources" / "codex.exe"
    process_snapshot = [
        {
            "pid": 100,
            "parentPid": 1,
            "name": "ChatGPT.exe",
            "executablePath": str(desktop_path),
            "commandLine": f'"{desktop_path}"',
            "createdAtEpoch": 199,
        },
        {
            "pid": 200,
            "parentPid": 100,
            "name": "codex.exe",
            "executablePath": str(codex_path),
            "commandLine": f'"{codex_path}" app-server',
            "createdAtEpoch": 200,
        },
    ]

    boundary = capture_notify_log_boundary(
        database_path,
        process_snapshot=process_snapshot,
        current_appx_install=fake_current_codex_appx_install(appx_root),
    )

    assert boundary["process_uuid"] == "pid:200:current-process"
    assert boundary["process_started_at_id"] == 2
    assert boundary["process_started_at_epoch"] == 200
    assert boundary["max_id"] == 3
    assert boundary["database_identity"]["path"] == str(database_path.resolve())
    assert boundary["database_identity"]["device"] >= 0
    assert boundary["database_identity"]["inode"] >= 0


def test_notify_log_boundary_binds_all_desktop_app_server_process_uuids_and_ignores_latest_unrelated_uuid(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "logs_2.sqlite"
    create_notify_logs_database(database_path)
    insert_notify_log(database_path, log_id=1, epoch=200, process_uuid="pid:200:desktop-main", message="startup")
    insert_notify_log(database_path, log_id=2, epoch=201, process_uuid="pid:300:plugin-appserver", message="startup")
    insert_notify_log(database_path, log_id=3, epoch=202, process_uuid="pid:999:unrelated-cli", message="latest")
    appx_root = tmp_path / "OpenAI.Codex_current"
    appx_resources = appx_root / "app" / "resources"
    desktop_path = appx_root / "app" / "ChatGPT.exe"
    codex_path = appx_resources / "codex.exe"
    plugin_codex_path = tmp_path / "codex_home" / "plugins" / ".plugin-appserver" / "codex.exe"
    for path in [desktop_path, codex_path, plugin_codex_path]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"runtime")
    process_snapshot = [
        {
            "pid": 100,
            "parentPid": 1,
            "name": "ChatGPT.exe",
            "executablePath": str(desktop_path),
            "commandLine": f'"{desktop_path}"',
            "createdAtEpoch": 199,
        },
        {
            "pid": 200,
            "parentPid": 100,
            "name": "codex.exe",
            "executablePath": str(codex_path),
            "commandLine": f'"{codex_path}" app-server --analytics-default-enabled',
            "createdAtEpoch": 200,
        },
        {
            "pid": 250,
            "parentPid": 200,
            "name": "node_repl.exe",
            "executablePath": str(tmp_path / "node_repl.exe"),
            "commandLine": "node_repl.exe",
            "createdAtEpoch": 200,
        },
        {
            "pid": 300,
            "parentPid": 250,
            "name": "codex.exe",
            "executablePath": str(plugin_codex_path),
            "commandLine": f'"{plugin_codex_path}" app-server --listen stdio://',
            "createdAtEpoch": 201,
        },
        {
            "pid": 999,
            "parentPid": 1,
            "name": "codex.exe",
            "executablePath": str(tmp_path / "unrelated" / "codex.exe"),
            "commandLine": "codex.exe exec",
            "createdAtEpoch": 202,
        },
    ]

    boundary = capture_notify_log_boundary(
        database_path,
        process_snapshot=process_snapshot,
        current_appx_install={"available": True, "installPath": str(appx_root), "version": "1.0"},
    )

    assert boundary["schema_version"] == 2
    assert boundary["desktop_root_pid"] == 100
    assert boundary["app_server_pids"] == [200, 300]
    assert boundary["process_uuids"] == ["pid:200:desktop-main", "pid:300:plugin-appserver"]
    assert boundary["process_uuid_by_pid"] == {
        "200": ["pid:200:desktop-main"],
        "300": ["pid:300:plugin-appserver"],
    }


def test_diagnostics_keeps_desktop_notify_static_check_green_with_historical_os206(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    appx_root = tmp_path / "current-appx"
    appx_resources = appx_root / "app" / "resources"
    dynamic_bin = tmp_path / "runtime" / "bin"
    module_root = dynamic_bin / "node_modules"
    helper_relative = Path("@oai/sky/bin/windows/codex-computer-use.exe")
    notify_path = module_root / helper_relative
    file_pairs = {
        (appx_resources / "codex.exe", tmp_path / "runtime-cli" / "codex.exe"): b"cli",
        (appx_resources / "cua_node" / "bin" / "node.exe", dynamic_bin / "node.exe"): b"node",
        (appx_resources / "cua_node" / "bin" / "node_repl.exe", dynamic_bin / "node_repl.exe"): b"node-repl",
        (appx_resources / "cua_node" / "bin" / "node_modules" / helper_relative, notify_path): b"helper",
    }
    for (appx_path, runtime_path), content in file_pairs.items():
        for path in (appx_path, runtime_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    (codex_home_path / "config.toml").write_text(
        "\n".join(
            [
                f"notify = ['{notify_path.as_posix()}', 'turn-ended']",
                "[mcp_servers.node_repl]",
                f"command = '{(dynamic_bin / 'node_repl.exe').as_posix()}'",
                "[mcp_servers.node_repl.env]",
                f"NODE_REPL_NODE_PATH = '{(dynamic_bin / 'node.exe').as_posix()}'",
                f"NODE_REPL_NODE_MODULE_DIRS = '{module_root.as_posix()}'",
                f"CODEX_CLI_PATH = '{(tmp_path / 'runtime-cli' / 'codex.exe').as_posix()}'",
                "SKY_CUA_NATIVE_PIPE = '1'",
                r"SKY_CUA_NATIVE_PIPE_DIRECTORY = '\\.\pipe\codex-computer-use-test'",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostics_module,
        "inspect_current_codex_appx_install",
        lambda: fake_current_codex_appx_install(appx_root),
    )
    database_path = codex_home_path / "logs_2.sqlite"
    create_notify_logs_database(database_path)
    insert_notify_log(database_path, log_id=1, epoch=100, process_uuid="previous-process")
    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=10, language="en")

    check = next(check for check in report["checks"] if check["id"] == "runtime.legacy_notify_hook")
    assert check["status"] == "pass"
    assert "desktop_managed_notify=True" in check["evidence"]
    assert not any(issue["id"] == "runtime.legacy_notify_os206" for issue in report["issues"])


def test_diagnostics_repair_prompt_is_localized_for_chinese(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="zh")

    assert "你是运行在我自己电脑上的 Codex" in report["repairPrompt"]
    assert "执行边界" in report["repairPrompt"]
    assert str(codex_home_path.resolve(strict=False)) in report["repairPrompt"]


def write_bundled_plugin_file(
    codex_home_path: Path,
    plugin_name: str,
    relative_path: str,
    root_name: str = "latest",
) -> None:
    file_path = codex_home_path / "plugins" / "cache" / "openai-bundled" / plugin_name / root_name / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("ok", encoding="utf-8")


def write_minimal_bundled_plugin_cache(codex_home_path: Path) -> None:
    marketplace_path = codex_home_path / "cache" / "bundled-marketplaces" / "openai-bundled" / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    marketplace_path.write_text("{}", encoding="utf-8")
    requirements = {
        "browser": [
            ".codex-plugin/plugin.json",
            "skills/control-in-app-browser/SKILL.md",
            "scripts/browser-client.mjs",
        ],
        "sites": [
            ".codex-plugin/plugin.json",
            "skills/sites-hosting/SKILL.md",
            "skills/sites-building/SKILL.md",
        ],
        "chrome": [
            ".codex-plugin/plugin.json",
            "skills/control-chrome/SKILL.md",
            "scripts/browser-client.mjs",
            "scripts/check-extension-installed.js",
        ],
        "computer-use": [
            ".codex-plugin/plugin.json",
            "skills/computer-use/SKILL.md",
            "scripts/computer-use-client.mjs",
        ],
        "latex": [
            ".codex-plugin/plugin.json",
            "skills/latex-compile/SKILL.md",
        ],
    }
    for plugin_name, relative_paths in requirements.items():
        for relative_path in relative_paths:
            write_bundled_plugin_file(codex_home_path, plugin_name, relative_path)


def write_bundled_marketplace_config(
    codex_home_path: Path,
    runtime_source: Path | None,
    managed_source: Path | None = None,
    runtime_primary_source: Path | None = None,
    managed_primary_source: Path | None = None,
) -> None:
    plugin_entries = "\n".join(
        f'[plugins."{plugin_name}@openai-bundled"]\nenabled = true'
        for plugin_name in ("browser", "sites", "chrome", "computer-use", "latex")
    )

    def marketplace_blocks(bundled_source: Path | None, primary_source: Path | None) -> str:
        blocks: list[str] = []
        if bundled_source is not None:
            blocks.append(
                "[marketplaces.openai-bundled]\n"
                "source_type = \"local\"\n"
                f"source = '{bundled_source}'"
            )
        if primary_source is not None:
            blocks.append(
                "[marketplaces.openai-primary-runtime]\n"
                "source_type = \"local\"\n"
                f"source = '{primary_source}'"
            )
        return "\n\n".join(blocks)

    runtime_marketplaces = marketplace_blocks(runtime_source, runtime_primary_source)
    managed_marketplaces = marketplace_blocks(managed_source, managed_primary_source)
    runtime_text = "\n\n".join(value for value in (runtime_marketplaces, plugin_entries) if value) + "\n"
    managed_text = "\n\n".join(value for value in (managed_marketplaces, plugin_entries) if value) + "\n"
    (codex_home_path / "config.toml").write_text(runtime_text, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(managed_text, encoding="utf-8")


def test_diagnostics_accepts_desktop_owned_runtime_marketplace_without_managed_pin(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_bundled_plugin_cache(codex_home_path)
    runtime_marketplace = codex_home_path / ".tmp" / "bundled-marketplaces" / "openai-bundled"
    runtime_manifest = (
        runtime_marketplace
        / ".agents"
        / "plugins"
        / "marketplace.json"
    )
    runtime_manifest.parent.mkdir(parents=True)
    runtime_manifest.write_text("{}", encoding="utf-8")
    persistent_marketplace = codex_home_path / "cache" / "bundled-marketplaces" / "openai-bundled"
    write_bundled_marketplace_config(codex_home_path, persistent_marketplace)

    conflicting_report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    conflicting_check = next(
        check for check in conflicting_report["checks"] if check["id"] == "plugins.bundled_marketplace_source"
    )

    assert conflicting_check["status"] == "critical"
    assert any(
        issue["id"] == "plugins.bundled_marketplace_source_conflict"
        for issue in conflicting_report["issues"]
    )

    write_bundled_marketplace_config(
        codex_home_path,
        runtime_marketplace,
        managed_source=runtime_marketplace,
    )
    managed_conflict_report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    managed_conflict_check = next(
        check for check in managed_conflict_report["checks"] if check["id"] == "plugins.bundled_marketplace_source"
    )
    assert managed_conflict_check["status"] == "critical"

    write_bundled_marketplace_config(codex_home_path, runtime_marketplace)
    runtime_residue_report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    runtime_residue_check = next(
        check for check in runtime_residue_report["checks"] if check["id"] == "plugins.bundled_marketplace_source"
    )
    assert runtime_residue_check["status"] == "pass"
    assert not any(
        issue["id"] == "plugins.bundled_marketplace_source_conflict"
        for issue in runtime_residue_report["issues"]
    )

    write_bundled_marketplace_config(codex_home_path, None)
    aligned_report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    aligned_check = next(
        check for check in aligned_report["checks"] if check["id"] == "plugins.bundled_marketplace_source"
    )

    assert aligned_check["status"] == "pass"
    assert not any(
        issue["id"] == "plugins.bundled_marketplace_source_conflict"
        for issue in aligned_report["issues"]
    )


@pytest.mark.parametrize("registration_layer", ["runtime", "managed"])
def test_diagnostics_accepts_desktop_primary_runtime_source_but_rejects_managed_pin(
    tmp_path: Path,
    registration_layer: str,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_bundled_plugin_cache(codex_home_path)
    runtime_marketplace = codex_home_path / ".tmp" / "bundled-marketplaces" / "openai-bundled"
    runtime_manifest = runtime_marketplace / ".agents" / "plugins" / "marketplace.json"
    runtime_manifest.parent.mkdir(parents=True)
    runtime_manifest.write_text("{}", encoding="utf-8")
    expected_primary_source = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "plugins"
        / "openai-primary-runtime"
    )
    write_bundled_marketplace_config(
        codex_home_path,
        None,
        runtime_primary_source=expected_primary_source if registration_layer == "runtime" else None,
        managed_primary_source=expected_primary_source if registration_layer == "managed" else None,
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    check = next(check for check in report["checks"] if check["id"] == "plugins.bundled_marketplace_source")

    expected_status = "pass" if registration_layer == "runtime" else "critical"
    assert check["status"] == expected_status
    if registration_layer == "managed":
        assert any("managed.openai-primary-runtime" in evidence for evidence in check["evidence"])


def write_curated_plugin_file(
    codex_home_path: Path,
    plugin_name: str,
    relative_path: str,
    root_name: str = "9c1190e4",
) -> None:
    file_path = codex_home_path / "plugins" / "cache" / "openai-curated" / plugin_name / root_name / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("ok", encoding="utf-8")


def test_diagnostics_accepts_versioned_bundled_plugin_cache_without_latest(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    version_root_name = "26.601.21317"
    write_minimal_bundled_plugin_cache(codex_home_path)
    plugin_cache_root = codex_home_path / "plugins" / "cache" / "openai-bundled"
    for latest_root in plugin_cache_root.glob("*/latest"):
        version_root = latest_root.parent / version_root_name
        latest_root.rename(version_root)
    plugin_config = "\n".join(
        [
            '[plugins."browser@openai-bundled"]',
            "enabled = true",
            '[plugins."sites@openai-bundled"]',
            "enabled = true",
            '[plugins."chrome@openai-bundled"]',
            "enabled = true",
            '[plugins."computer-use@openai-bundled"]',
            "enabled = true",
            '[plugins."latex@openai-bundled"]',
            "enabled = true",
        ]
    )
    (codex_home_path / "config.toml").write_text(plugin_config, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(plugin_config, encoding="utf-8")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    assert not any(issue["id"] == "plugins.cache_incomplete" for issue in report["issues"])
    assert any(
        check["id"] == "plugins.computer-use"
        and check["status"] == "pass"
        and version_root_name in "\n".join(check["evidence"])
        for check in report["checks"]
    )
    assert any(
        check["id"] == "plugins.computer_use_turn_end_hook" and check["status"] == "pass"
        for check in report["checks"]
    )
    assert any(check["id"] == "plugins.tool_exposure_model" and check["status"] == "info" for check in report["checks"])


def test_diagnostics_passes_complete_computer_use_privileged_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_sandbox_runtime(codex_home_path)
    write_minimal_bundled_plugin_cache(codex_home_path)
    runtime_root = tmp_path / "desktop-runtime"
    node_repl_path = runtime_root / "node_repl.exe"
    node_path = runtime_root / "node.exe"
    node_module_root = runtime_root / "node_modules"
    pipe_directory = runtime_root / "native-pipe"
    pipe_directory.mkdir(parents=True)
    sky_client_path = (
        node_module_root
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
    playwright_manifest = node_module_root / "playwright" / "package.json"
    for file_path in (node_repl_path, node_path, sky_client_path, playwright_manifest):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"MZ" if file_path.suffix == ".exe" else b"{}")
    plugin_entries = "\n".join(
        f'[plugins."{plugin_name}@openai-bundled"]\nenabled = true'
        for plugin_name in ("browser", "sites", "chrome", "computer-use", "latex")
    )
    (codex_home_path / "config.toml").write_text(
        f"""
[mcp_servers.node_repl]
command = "{node_repl_path.as_posix()}"
args = []

[mcp_servers.node_repl.env]
NODE_REPL_NODE_MODULE_DIRS = "{node_module_root.as_posix()}"
NODE_REPL_NODE_PATH = "{node_path.as_posix()}"
SKY_CUA_NATIVE_PIPE = "1"
SKY_CUA_NATIVE_PIPE_DIRECTORY = "{pipe_directory.as_posix()}"

{plugin_entries}
""".lstrip(),
        encoding="utf-8",
    )
    (codex_home_path / "managed_config.toml").write_text(plugin_entries + "\n", encoding="utf-8")
    monkeypatch.setattr(
        diagnostics_module,
        "scan_mcp_process_snapshot",
        lambda: {
            "available": True,
            "warning": False,
            "legacyThreadMessengerProcessCount": 0,
            "xcodebuildMcpProcessCount": 0,
            "nodeReplWithDisableSandboxCount": 0,
            "nodeReplProcesses": [
                {
                    "pid": "42",
                    "name": "node_repl.exe",
                    "commandLine": f'"{node_repl_path}"',
                }
            ],
            "sampleProcesses": [],
        },
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    checks = {check["id"]: check for check in report["checks"]}

    assert checks["plugins.node_repl_config_layer_consistency"]["status"] == "pass"
    assert checks["plugins.node_repl_desktop_privileged_mode"]["status"] == "pass"
    assert checks["plugins.computer_use_privileged_runtime"]["status"] == "pass"
    assert "no Computer Use protocol handshake was attempted" in checks[
        "plugins.computer_use_privileged_runtime"
    ]["summary"]
    assert "computer_use_protocol_handshake_verified=False" in checks[
        "plugins.computer_use_privileged_runtime"
    ]["evidence"]

    monkeypatch.setattr(
        diagnostics_module,
        "scan_mcp_process_snapshot",
        lambda: {
            "available": True,
            "warning": False,
            "legacyThreadMessengerProcessCount": 0,
            "xcodebuildMcpProcessCount": 0,
            "nodeReplWithDisableSandboxCount": 0,
            "nodeReplProcesses": [],
            "sampleProcesses": [],
        },
    )
    unbound_report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    unbound_check = next(
        check
        for check in unbound_report["checks"]
        if check["id"] == "plugins.computer_use_privileged_runtime"
    )
    assert unbound_check["status"] == "critical"
    assert any(
        "no running node_repl process matches" in evidence
        for evidence in unbound_check["evidence"]
    )


def test_computer_use_pipe_directory_probe_rejects_missing_and_inaccessible_paths(
    tmp_path: Path,
) -> None:
    missing_probe = diagnostics_module.inspect_computer_use_pipe_endpoint(
        str(tmp_path / "missing-pipe-directory")
    )
    assert missing_probe["ready"] is False
    assert "does not exist" in missing_probe["error"]

    inaccessible_path = tmp_path / "inaccessible-pipe-directory"
    inaccessible_path.mkdir()
    with patch.object(
        diagnostics_module.os,
        "scandir",
        side_effect=PermissionError("access denied by test"),
    ):
        inaccessible_probe = diagnostics_module.inspect_computer_use_pipe_endpoint(
            str(inaccessible_path)
        )
    assert inaccessible_probe["exists"] is True
    assert inaccessible_probe["accessible"] is False
    assert inaccessible_probe["ready"] is False
    assert "access denied by test" in inaccessible_probe["error"]


def test_diagnostics_reports_incomplete_browser_runtime_contract(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_bundled_plugin_cache(codex_home_path)
    plugin_config = "\n".join(
        [
            '[plugins."browser@openai-bundled"]',
            "enabled = true",
            '[plugins."sites@openai-bundled"]',
            "enabled = true",
            '[plugins."chrome@openai-bundled"]',
            "enabled = true",
            '[plugins."computer-use@openai-bundled"]',
            "enabled = true",
            '[plugins."latex@openai-bundled"]',
            "enabled = true",
        ]
    )
    (codex_home_path / "config.toml").write_text(plugin_config, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(plugin_config, encoding="utf-8")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    assert any(check["id"] == "plugins.skill_manifests" and check["status"] == "pass" for check in report["checks"])
    assert any(check["id"] == "plugins.browser_runtime_contract" and check["status"] == "warning" for check in report["checks"])
    assert any(issue["id"] == "plugins.browser_runtime_contract_incomplete" for issue in report["issues"])


def test_diagnostics_reports_stale_chrome_native_host_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    existing_node_path = tmp_path / "node.exe"
    existing_node_repl_path = tmp_path / "node_repl.exe"
    existing_browser_client_path = tmp_path / "browser-client.mjs"
    existing_extension_host_path = tmp_path / "extension-host.exe"
    existing_resources_path = tmp_path / "resources"
    missing_codex_path = tmp_path / "deleted" / "codex.exe"
    for path in [existing_node_path, existing_node_repl_path, existing_browser_client_path, existing_extension_host_path]:
        path.write_bytes(b"ok")
    existing_resources_path.mkdir()
    (codex_home_path / "chrome-native-hosts.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "chromeNativeHosts": [
                    {
                        "codexCliPath": str(missing_codex_path),
                        "browserClientPath": str(existing_browser_client_path),
                        "extensionHostPath": str(existing_extension_host_path),
                        "resourcesPath": str(existing_resources_path),
                        "nodePath": str(existing_node_path),
                        "nodeReplPath": str(existing_node_repl_path),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "entries": [
                    {
                        "paths": {
                            "codexCliPath": str(missing_codex_path),
                            "browserClientPath": str(existing_browser_client_path),
                            "extensionHostPath": str(existing_extension_host_path),
                            "resourcesPath": str(existing_resources_path),
                            "nodePath": str(existing_node_path),
                            "nodeReplPath": str(existing_node_repl_path),
                        }
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostics_module,
        "inspect_current_codex_appx_install",
        lambda: fake_current_codex_appx_install(tmp_path),
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    native_host_check = next(check for check in report["checks"] if check["id"] == "plugins.chrome_native_hosts")

    assert native_host_check["status"] == "warning"
    assert "stale_missing_paths=2" in native_host_check["evidence"]
    assert "configuration_complete=False" in native_host_check["evidence"]
    assert str(missing_codex_path) in "\n".join(native_host_check["evidence"])
    assert any(issue["id"] == "plugins.chrome_native_host_paths_unhealthy" for issue in report["issues"])


def test_diagnostics_rejects_missing_v1_and_historical_v2_native_host_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    current_paths = {
        key: str(tmp_path / filename)
        for key, filename in {
            "codexCliPath": "codex.exe",
            "browserClientPath": "browser-client.mjs",
            "extensionHostPath": "extension-host.exe",
            "resourcesPath": "resources",
            "nodePath": "node.exe",
            "nodeReplPath": "node_repl.exe",
        }.items()
    }
    for key, path_text in current_paths.items():
        path = Path(path_text)
        if key == "resourcesPath":
            path.mkdir()
        else:
            path.write_bytes(b"ok")
    missing_old_cli = tmp_path / "deleted" / "old-codex.exe"
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "entries": [
                    {"paths": current_paths},
                    {"paths": {**current_paths, "codexCliPath": str(missing_old_cli)}},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostics_module,
        "inspect_current_codex_appx_install",
        lambda: fake_current_codex_appx_install(tmp_path),
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    native_host_check = next(
        check for check in report["checks"] if check["id"] == "plugins.chrome_native_hosts"
    )

    assert native_host_check["status"] == "warning"
    assert "files=1/2" in native_host_check["evidence"]
    assert "stale_entries=2" in native_host_check["evidence"]
    assert "configuration_complete=False" in native_host_check["evidence"]
    assert any("historical=" in evidence for evidence in native_host_check["evidence"])
    assert "native_messaging_handshake_verified=False" in native_host_check["evidence"]
    assert any(issue["id"] == "plugins.chrome_native_host_paths_unhealthy" for issue in report["issues"])


def test_chrome_native_host_scan_requires_both_files_and_every_entry_to_match_current_runtime(
    tmp_path: Path,
) -> None:
    codex_home_path = tmp_path / "codex_home"
    codex_home_path.mkdir()
    appx_root = tmp_path / "current-appx"
    current_paths = create_complete_native_host_paths(tmp_path / "runtime", appx_root)
    current_paths["codexHome"] = str(codex_home_path)
    current_paths["nodeModuleDirs"] = [str(tmp_path / "runtime" / "node_modules")]
    (tmp_path / "runtime" / "node_modules").mkdir()
    stale_paths = {**current_paths, "resourcesPath": str(tmp_path / "old-appx" / "app" / "resources")}
    Path(stale_paths["resourcesPath"]).mkdir(parents=True)
    (codex_home_path / "chrome-native-hosts.json").write_text(
        json.dumps({"schemaVersion": 1, "chromeNativeHosts": [{key: value for key, value in current_paths.items() if key != "nodeModuleDirs"}]}),
        encoding="utf-8",
    )
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "entries": [
                    {"paths": current_paths},
                    {"paths": stale_paths},
                ],
            }
        ),
        encoding="utf-8",
    )

    scan = diagnostics_module.scan_chrome_native_host_paths(
        codex_home_path,
        current_appx_install=fake_current_codex_appx_install(appx_root),
        expected_paths=current_paths,
    )

    assert scan["configurationComplete"] is False
    assert scan["healthyV1Entries"] == 1
    assert scan["healthyV2Entries"] == 1
    assert scan["v2Entries"] == 2
    assert scan["staleEntries"] == 1


def test_chrome_native_host_scan_accepts_only_complete_current_v1_and_v2_files(tmp_path: Path) -> None:
    codex_home_path = tmp_path / "codex_home"
    codex_home_path.mkdir()
    appx_root = tmp_path / "current-appx"
    current_paths = create_complete_native_host_paths(tmp_path / "runtime", appx_root)
    current_paths["codexHome"] = str(codex_home_path)
    current_paths["nodeModuleDirs"] = [str(tmp_path / "runtime" / "node_modules")]
    (tmp_path / "runtime" / "node_modules").mkdir()
    (codex_home_path / "chrome-native-hosts.json").write_text(
        json.dumps({"schemaVersion": 1, "chromeNativeHosts": [{key: value for key, value in current_paths.items() if key != "nodeModuleDirs"}]}),
        encoding="utf-8",
    )
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps({"schemaVersion": 2, "entries": [{"paths": current_paths}]}),
        encoding="utf-8",
    )

    scan = diagnostics_module.scan_chrome_native_host_paths(
        codex_home_path,
        current_appx_install=fake_current_codex_appx_install(appx_root),
        expected_paths=current_paths,
    )

    assert scan["configurationComplete"] is True
    assert scan["existingFiles"] == [
        str(codex_home_path / "chrome-native-hosts.json"),
        str(codex_home_path / "chrome-native-hosts-v2.json"),
    ]
    assert scan["healthyV1Entries"] == scan["v1Entries"] == 1
    assert scan["healthyV2Entries"] == scan["v2Entries"] == 1


def test_chrome_native_host_scan_accepts_byte_identical_codex_cli_alias(tmp_path: Path) -> None:
    codex_home_path = tmp_path / "codex_home"
    codex_home_path.mkdir()
    appx_root = tmp_path / "current-appx"
    current_paths = create_complete_native_host_paths(tmp_path / "runtime", appx_root)
    current_paths["codexHome"] = str(codex_home_path)
    current_paths["nodeModuleDirs"] = [str(tmp_path / "runtime" / "node_modules")]
    (tmp_path / "runtime" / "node_modules").mkdir()
    expected_cli_path = Path(current_paths["codexCliPath"])
    alias_cli_path = tmp_path / "plugin-appserver" / "codex.exe"
    alias_cli_path.parent.mkdir()
    alias_cli_path.write_bytes(expected_cli_path.read_bytes())
    aliased_paths = {**current_paths, "codexCliPath": str(alias_cli_path)}
    (codex_home_path / "chrome-native-hosts.json").write_text(
        json.dumps({"schemaVersion": 1, "chromeNativeHosts": [{key: value for key, value in current_paths.items() if key != "nodeModuleDirs"}]}),
        encoding="utf-8",
    )
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps({"schemaVersion": 2, "entries": [{"paths": aliased_paths}, {"paths": current_paths}]}),
        encoding="utf-8",
    )

    scan = diagnostics_module.scan_chrome_native_host_paths(
        codex_home_path,
        current_appx_install=fake_current_codex_appx_install(appx_root),
        expected_paths=current_paths,
    )

    assert scan["configurationComplete"] is True
    assert scan["healthyV2Entries"] == scan["v2Entries"] == 2
    assert scan["staleEntries"] == 0
    assert len(scan["equivalentCodexCliAliases"]) == 1


def test_chrome_native_host_scan_rejects_different_codex_cli_alias(tmp_path: Path) -> None:
    codex_home_path = tmp_path / "codex_home"
    codex_home_path.mkdir()
    appx_root = tmp_path / "current-appx"
    current_paths = create_complete_native_host_paths(tmp_path / "runtime", appx_root)
    current_paths["codexHome"] = str(codex_home_path)
    current_paths["nodeModuleDirs"] = [str(tmp_path / "runtime" / "node_modules")]
    (tmp_path / "runtime" / "node_modules").mkdir()
    alias_cli_path = tmp_path / "plugin-appserver" / "codex.exe"
    alias_cli_path.parent.mkdir()
    alias_cli_path.write_bytes(b"different-cli")
    aliased_paths = {**current_paths, "codexCliPath": str(alias_cli_path)}
    (codex_home_path / "chrome-native-hosts.json").write_text(
        json.dumps({"schemaVersion": 1, "chromeNativeHosts": [{key: value for key, value in current_paths.items() if key != "nodeModuleDirs"}]}),
        encoding="utf-8",
    )
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps({"schemaVersion": 2, "entries": [{"paths": aliased_paths}, {"paths": current_paths}]}),
        encoding="utf-8",
    )

    scan = diagnostics_module.scan_chrome_native_host_paths(
        codex_home_path,
        current_appx_install=fake_current_codex_appx_install(appx_root),
        expected_paths=current_paths,
    )

    assert scan["configurationComplete"] is False
    assert scan["healthyV2Entries"] == 1
    assert scan["staleEntries"] == 1
    assert any("plugin-appserver" in value for value in scan["exactPathMismatches"])


def test_chrome_native_host_scan_rejects_missing_v2_node_module_directory(tmp_path: Path) -> None:
    codex_home_path = tmp_path / "codex_home"
    codex_home_path.mkdir()
    appx_root = tmp_path / "current-appx"
    current_paths = create_complete_native_host_paths(tmp_path / "runtime", appx_root)
    current_paths["codexHome"] = str(codex_home_path)
    current_paths["nodeModuleDirs"] = [str(tmp_path / "runtime" / "missing-node-modules")]
    (codex_home_path / "chrome-native-hosts.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "chromeNativeHosts": [
                    {key: value for key, value in current_paths.items() if key != "nodeModuleDirs"}
                ],
            }
        ),
        encoding="utf-8",
    )
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps({"schemaVersion": 2, "entries": [{"paths": current_paths}]}),
        encoding="utf-8",
    )

    scan = diagnostics_module.scan_chrome_native_host_paths(
        codex_home_path,
        current_appx_install=fake_current_codex_appx_install(appx_root),
        expected_paths=current_paths,
    )

    assert scan["configurationComplete"] is False
    assert scan["healthyV1Entries"] == 1
    assert scan["healthyV2Entries"] == 0
    assert any("missing-node-modules" in value for value in scan["staleMissingPaths"])


def test_diagnostics_rejects_chrome_native_host_with_missing_required_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    current_appx_root = tmp_path / "current-appx"
    current_appx_root.mkdir()
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps({"schemaVersion": 2, "entries": [{"paths": {}}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostics_module,
        "inspect_current_codex_appx_install",
        lambda: fake_current_codex_appx_install(current_appx_root),
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    native_host_check = next(
        check for check in report["checks"] if check["id"] == "plugins.chrome_native_hosts"
    )

    assert native_host_check["status"] == "warning"
    assert "healthy_v2_entries=0" in native_host_check["evidence"]
    assert any("resourcesPath=<missing>" in evidence for evidence in native_host_check["evidence"])
    assert any(issue["id"] == "plugins.chrome_native_host_paths_unhealthy" for issue in report["issues"])


def test_diagnostics_rejects_existing_chrome_native_host_paths_from_old_appx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    current_appx_root = tmp_path / "current-appx"
    current_appx_root.mkdir()
    old_appx_root = tmp_path / "old-appx"
    old_paths = create_complete_native_host_paths(tmp_path / "runtime", old_appx_root)
    (codex_home_path / "chrome-native-hosts-v2.json").write_text(
        json.dumps({"schemaVersion": 2, "entries": [{"paths": old_paths}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostics_module,
        "inspect_current_codex_appx_install",
        lambda: fake_current_codex_appx_install(current_appx_root),
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    native_host_check = next(
        check for check in report["checks"] if check["id"] == "plugins.chrome_native_hosts"
    )

    assert native_host_check["status"] == "warning"
    assert "wrong_appx_paths=1" in native_host_check["evidence"]
    assert "files=1/2" in native_host_check["evidence"]
    assert "wrong_appx_paths=1" in native_host_check["evidence"]
    assert any("current_appx=" in evidence for evidence in native_host_check["evidence"])


def test_diagnostics_reports_and_clears_abandoned_plugin_restore_transaction(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    stale_restore_path = codex_home_path / ".plugins.c650ff0580a94204a720126f3042c946.restoring"
    stale_restore_path.mkdir()
    unrelated_path = codex_home_path / ".plugins.not-a-transaction.restoring"
    unrelated_path.mkdir()

    warning_report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    warning_check = next(
        check for check in warning_report["checks"] if check["id"] == "plugins.stale_restore_artifacts"
    )
    assert warning_check["status"] == "warning"
    assert str(stale_restore_path) in warning_check["evidence"]
    assert str(unrelated_path) not in warning_check["evidence"]
    assert any(issue["id"] == "plugins.stale_restore_artifacts" for issue in warning_report["issues"])

    stale_restore_path.rename(tmp_path / stale_restore_path.name)
    passing_report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    passing_check = next(
        check for check in passing_report["checks"] if check["id"] == "plugins.stale_restore_artifacts"
    )
    assert passing_check["status"] == "pass"


def test_diagnostics_reports_stale_chrome_native_messaging_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    old_home_path = tmp_path / "old-home" / ".codex"
    old_extension_host_path = (
        old_home_path
        / "plugins"
        / "cache"
        / "openai-bundled"
        / "chrome"
        / "latest"
        / "extension-host"
        / "windows"
        / "x64"
        / "extension-host.exe"
    )
    old_extension_host_path.parent.mkdir(parents=True)
    old_extension_host_path.write_text("", encoding="utf-8")
    native_manifest_path = tmp_path / "com.openai.codexextension.json"
    native_manifest_path.write_text(
        json.dumps(
            {
                "name": "com.openai.codexextension",
                "description": "Codex chrome native messaging host",
                "path": str(old_extension_host_path),
                "type": "stdio",
                "allowed_origins": ["chrome-extension://hehggadaopoacecdllhhajmbjkdcmajg/"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "backend.diagnostics.read_windows_chrome_native_messaging_registry_entries",
        lambda: [
            {
                "source": r"HKCU\Software\Google\Chrome\NativeMessagingHosts\com.openai.codexextension",
                "manifestPath": str(native_manifest_path),
            }
        ],
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    manifest_check = next(check for check in report["checks"] if check["id"] == "plugins.chrome_native_messaging_manifests")

    assert manifest_check["status"] == "warning"
    assert "foreign_home_hosts=1" in manifest_check["evidence"]
    assert str(old_extension_host_path) in "\n".join(manifest_check["evidence"])
    assert any(issue["id"] == "plugins.chrome_native_messaging_manifest_stale" for issue in report["issues"])


def test_diagnostics_accepts_current_chrome_native_messaging_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    extension_host_path = (
        codex_home_path
        / "plugins"
        / "cache"
        / "openai-bundled"
        / "chrome"
        / "26.0"
        / "extension-host"
        / "windows"
        / "x64"
        / "extension-host.exe"
    )
    extension_host_path.parent.mkdir(parents=True)
    extension_host_path.write_text("", encoding="utf-8")
    native_manifest_path = tmp_path / "com.openai.codexextension.json"
    native_manifest_path.write_text(
        json.dumps(
            {
                "name": "com.openai.codexextension",
                "description": "Codex chrome native messaging host",
                "path": str(extension_host_path),
                "type": "stdio",
                "allowed_origins": ["chrome-extension://hehggadaopoacecdllhhajmbjkdcmajg/"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "backend.diagnostics.read_windows_chrome_native_messaging_registry_entries",
        lambda: [
            {
                "source": r"HKCU\Software\Google\Chrome\NativeMessagingHosts\com.openai.codexextension",
                "manifestPath": str(native_manifest_path),
            }
        ],
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    manifest_check = next(check for check in report["checks"] if check["id"] == "plugins.chrome_native_messaging_manifests")

    assert manifest_check["status"] == "pass"
    assert "foreign_home_hosts=0" in manifest_check["evidence"]
    assert not any(issue["id"] == "plugins.chrome_native_messaging_manifest_stale" for issue in report["issues"])


def test_diagnostics_reports_curated_marketplace_manifest_warnings(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    manifest_path = codex_home_path / ".tmp" / "plugins" / "plugins" / "ngs-analysis" / ".codex-plugin" / "plugin.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "name": "ngs-analysis",
                "interface": {
                    "displayName": "Life Sciences NGS Analysis",
                    "defaultPrompt": [
                        "Guide me through the minimum required NGS analysis questions, inspect available BCL/FASTQ files or count matrices, choose the right public pipeline or deeper assay-specific skill, check whether required tools already exist, and execute supported local workflows with validation, logs, manifests, QC reports, and artifact indexes."
                    ],
                    "icon_small": "../assets/app-icon.png",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")
    manifest_check = next(check for check in report["checks"] if check["id"] == "plugins.curated_marketplace_manifests")
    evidence_text = "\n".join(manifest_check["evidence"])

    assert manifest_check["status"] == "warning"
    assert "invalid_prompts=1" in manifest_check["evidence"]
    assert "invalid_icon_paths=1" in manifest_check["evidence"]
    assert "ngs-analysis:interface.defaultPrompt[0]" in evidence_text
    assert "ngs-analysis:interface.icon_small" in evidence_text
    assert any(issue["id"] == "plugins.curated_marketplace_manifest_warnings" for issue in report["issues"])


def test_diagnostics_reports_stale_curated_marketplace_config_entries(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    manifest_path = codex_home_path / ".tmp" / "plugins" / "plugins" / "github" / ".codex-plugin" / "plugin.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"name": "github"}), encoding="utf-8")
    config_text = "\n".join(
        [
            '[plugins."github@openai-curated"]',
            "enabled = true",
            '[plugins."chatgpt-apps@openai-curated"]',
            "enabled = false",
        ]
    )
    (codex_home_path / "config.toml").write_text(config_text, encoding="utf-8")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    config_check = next(check for check in report["checks"] if check["id"] == "plugins.curated_marketplace_config")
    evidence_text = "\n".join(config_check["evidence"])
    assert config_check["status"] == "warning"
    assert "stale_config_entries=1" in config_check["evidence"]
    assert "stale=chatgpt-apps" in evidence_text
    assert any(issue["id"] == "plugins.curated_marketplace_config_stale" for issue in report["issues"])


def test_diagnostics_reports_recent_runtime_blocker_messages(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    blocker_message = (
        "当前没有暴露专用 Browser/IAB 控制工具；"
        "node_repl 当前无法写入它自己的运行资产目录；"
        "build-web-apps 的技能文件路径 SKILL.md 不存在。"
    )
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {"content": [{"type": "output_text", "text": blocker_message}]},
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    blocker_check = next(check for check in report["checks"] if check["id"] == "plugins.recent_runtime_blocker_messages")
    assert blocker_check["status"] == "warning"
    findings_line = next(item for item in blocker_check["evidence"] if item.startswith("findings="))
    assert int(findings_line.split("=", 1)[1]) >= 3
    assert "Browser/IAB" in "\n".join(blocker_check["evidence"])
    blocker_issue = next(issue for issue in report["issues"] if issue["id"] == "plugins.recent_runtime_blocker_messages")
    assert blocker_issue["severity"] == "warning"


def test_diagnostics_reports_node_repl_transport_closed_tool_output(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-node-repl",
                        "output": (
                            "Wall time: 0.0084 seconds\n"
                            "Output:\n"
                            "[{\"type\":\"text\",\"text\":\"tool call error: tool call failed for `node_repl/js`\\n\\n"
                            "Caused by:\\n    Transport closed\"}]"
                        ),
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "`node_repl` 当前是好的：没有 Transport closed，Playwright 也能导入。"
                                    "接下来我看 Browser 插件入口的真实用法。"
                                ),
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "数据诊断技能文件路径在当前插件缓存里不存在，我会用本地只读审计流程继续，"
                                    "避免误把旧结果当现状。"
                                ),
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": "我还要覆盖最近日志里出现 `WebSocket is not defined` 的场景，确保体检会给出专门问题，而不是漏掉。",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": "由于当前没有专用 Browser/IAB 控制工具暴露，我会用项目已有 Playwright smoke 脚本和一个临时内联测量脚本，不新增文件。",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "\u6211\u5148\u6309\u5f53\u524d\u73b0\u573a\u518d\u590d\u6838\u4e00\u904d\uff1a"
                                    "\u786e\u8ba4 `WebSocket is not defined` "
                                    "\u7684\u517c\u5bb9\u5c42\u3001\u4ea7\u54c1\u4f53\u68c0\u9879\u548c\u6d4b\u8bd5\u7ed3\u679c\u90fd\u8fd8\u6210\u7acb\u3002"
                                ),
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    blocker_check = next(check for check in report["checks"] if check["id"] == "plugins.recent_runtime_blocker_messages")
    assert blocker_check["status"] == "warning"
    evidence_text = "\n".join(blocker_check["evidence"])
    assert "node_repl_transport_closed" in evidence_text
    assert "Transport closed" in evidence_text
    assert any(issue["id"] == "plugins.node_repl_transport_closed" for issue in report["issues"])


def test_diagnostics_downgrades_historical_node_repl_transport_when_plugins_are_healthy(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    write_minimal_bundled_plugin_cache(codex_home_path)
    plugin_config = "\n".join(
        [
            '[plugins."browser@openai-bundled"]',
            "enabled = true",
            '[plugins."sites@openai-bundled"]',
            "enabled = true",
            '[plugins."chrome@openai-bundled"]',
            "enabled = true",
            '[plugins."computer-use@openai-bundled"]',
            "enabled = true",
            '[plugins."latex@openai-bundled"]',
            "enabled = true",
        ]
    )
    (codex_home_path / "config.toml").write_text(plugin_config, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(plugin_config, encoding="utf-8")
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-node-repl",
                        "output": (
                            "Wall time: 0.0084 seconds\n"
                            "Output:\n"
                            "[{\"type\":\"text\",\"text\":\"tool call error: tool call failed for `node_repl/js`\\n\\n"
                            "Caused by:\\n    Transport closed\"}]"
                        ),
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    blocker_check = next(check for check in report["checks"] if check["id"] == "plugins.recent_runtime_blocker_messages")
    assert blocker_check["status"] == "info"
    transport_issue = next(issue for issue in report["issues"] if issue["id"] == "plugins.node_repl_transport_closed")
    assert transport_issue["severity"] == "info"


def test_diagnostics_reports_node_global_websocket_missing(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-refresh-script",
                        "output": "刷新脚本失败: ReferenceError: WebSocket is not defined",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    blocker_check = next(check for check in report["checks"] if check["id"] == "plugins.recent_runtime_blocker_messages")
    evidence_text = "\n".join(blocker_check["evidence"])
    assert blocker_check["status"] == "warning"
    assert "node_global_websocket_missing" in evidence_text
    assert any(issue["id"] == "plugins.node_websocket_compat_missing" for issue in report["issues"])


def test_diagnostics_ignores_runtime_blocker_diagnostic_narration(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "I already have hard evidence: node_repl/js is exposed but returns "
                                    "Transport closed. I am checking whether the diagnostics API reports "
                                    "it as a runtime failure instead of only checking plugin files."
                                ),
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "剩下的插件 warning 不是当前文件缺失：Browser/IAB、Computer Use、"
                                    "skill path missing 和 node_repl 都来自历史文本；实际路径已经回读验证存在，"
                                    "并且 Playwright fallback 质量门禁覆盖本地浏览器路径。"
                                ),
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "artifact_browser/content_sha256 skipped_large_or_missing hash 边界不是 Browser 插件问题。"
                                ),
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "现场结果显示插件安装/路径类检查通过，但你截图里的那种"
                                    "“没有暴露 Browser/IAB”“node_repl 无法写入运行资产目录”的文本，"
                                    "如果体检不报，就应该补成专门的诊断项。"
                                ),
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    blocker_check = next(check for check in report["checks"] if check["id"] == "plugins.recent_runtime_blocker_messages")
    assert blocker_check["status"] == "pass"
    assert "findings=0" in blocker_check["evidence"]
    assert not any(issue["id"] == "plugins.node_repl_transport_closed" for issue in report["issues"])


def test_diagnostics_ignores_runtime_recovery_narration(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    messages = [
        "`WebSocket is not defined` 仍然是可修的脚本兼容问题，应保留 warning；但这句话本身不是脚本失败输出。",
        "IAB 运行时已绑定成功，之前“没有 Browser/IAB 专用控制工具”的判断已经不成立；后续如需对用户当前浏览器做 DOM 检查，可以直接用该接口。",
        "已找到 Browser 控制工具；我会按插件路径连接 IAB 并读取其接口说明。",
        "Browser/IAB 控制链路已连上。后续不会再把“没有暴露 Browser/IAB 专用控制工具”当阻塞项。",
        "Browser/IAB 控制工具已暴露且可连接，以后不能再说“当前没有 Browser/IAB 控制工具”。",
        "我要改的是体检规则本身，只把恢复证据过滤掉，保留真正的失败语句和 `WebSocket is not defined` 直接错误。",
        "测试用例已经补上。现在跑运行时体检相关测试，确认新过滤不会吞掉真正的 `Transport closed` 和 `WebSocket is not defined` 故障。",
        "我使用 `browser:control-in-app-browser` 这条路径处理；关键点是先让 IAB 控制工具在当前会话真正可调用。",
        "我先把 Browser/IAB 控制链路按工具层实际可用能力修到可验证状态；关键不是口头说“没有暴露”。",
        "已确认 Browser 技能本轮存在，评估时不能把 “没有 Browser/IAB 专用控制工具” 当结论。",
        "Browser 控制工具现在已暴露。我会按 Browser 技能要求连接并检查当前页面。",
        "说明后续可以继续用真实浏览器做点击、截图、布局验证，不再把“没有 Browser/IAB 控制工具”当阻塞。",
        "先确认 IAB 控制工具可用和当前前端服务状态，再用 Playwright/DOM 几何采集证据。",
        "IAB 控制工具已经通过插件暴露出来了；我会按插件要求重新连接并读取浏览器运行文档。",
        "IAB 现在不是“未暴露”状态：已连到 Codex In-app Browser，当前标签就是 127.0.0.1:4173。",
        "先修这个能力缺口。product-design 被技能索引列出来但 SKILL.md 实体路径缺失，先查缓存和插件注册状态。",
        "实际情况不是没安装：product-design 包存在，技能实体也存在，但入口名是 skills/index/SKILL.md。我会创建一个同包内入口别名。",
        "Browser/IAB 的 bootstrap 已经成功，说明“没有暴露 Browser/IAB”至少不是当前会话的工具不可用问题。",
        "Browser 控制工具已加载。我会先尝试绑定内置浏览器；若插件连接失败，会按你的要求使用普通 Playwright 做同样的只读布局诊断。",
    ]
    with rollout_path.open("a", encoding="utf-8") as handle:
        for message in messages:
            handle.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"content": [{"type": "output_text", "text": message}]},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    blocker_check = next(check for check in report["checks"] if check["id"] == "plugins.recent_runtime_blocker_messages")
    assert blocker_check["status"] == "pass"
    assert "findings=0" in blocker_check["evidence"]
    assert not any(issue["id"] == "plugins.recent_runtime_blocker_messages" for issue in report["issues"])


def test_diagnostics_reports_node_repl_asset_directory_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    local_app_data_path = tmp_path / "local_app_data"
    codex_bin_path = local_app_data_path / "OpenAI" / "Codex" / "bin"
    nested_runtime_path = codex_bin_path / "runtime"
    nested_runtime_path.mkdir(parents=True)
    node_path = codex_bin_path / "node.exe"
    node_repl_path = nested_runtime_path / "node_repl.exe"
    node_path.write_bytes(b"node")
    node_repl_path.write_bytes(b"node_repl")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data_path))
    config_text = (codex_home_path / "config.toml").read_text(encoding="utf-8")
    (codex_home_path / "config.toml").write_text(
        f'{config_text}\nNODE_REPL_NODE_PATH = "{node_path.as_posix()}"\n',
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    asset_check = next(check for check in report["checks"] if check["id"] == "plugins.node_repl_asset_directories")
    assert asset_check["status"] == "pass"
    assert "node_repl_files=1" in asset_check["evidence"]
    assert "node_files=1" in asset_check["evidence"]
    assert "unwritable_directories=0" in asset_check["evidence"]
    assert not any(issue["id"] == "plugins.node_repl_runtime_assets_unhealthy" for issue in report["issues"])


def test_diagnostics_reports_node_repl_playwright_module_roots(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    node_modules_path = tmp_path / "node_modules"
    playwright_path = node_modules_path / "playwright"
    ws_path = node_modules_path / "ws"
    playwright_path.mkdir(parents=True)
    ws_path.mkdir(parents=True)
    (playwright_path / "package.json").write_text('{"name":"playwright"}', encoding="utf-8")
    (ws_path / "package.json").write_text('{"name":"ws"}', encoding="utf-8")
    config_text = (codex_home_path / "config.toml").read_text(encoding="utf-8")
    (codex_home_path / "config.toml").write_text(
        "\n".join(
            [
                config_text,
                "[mcp_servers.node_repl.env]",
                f'NODE_REPL_NODE_MODULE_DIRS = "{node_modules_path.as_posix()}"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    module_check = next(check for check in report["checks"] if check["id"] == "plugins.node_repl_node_modules")
    assert module_check["status"] == "pass"
    assert "playwright_roots=1" in module_check["evidence"]
    websocket_check = next(check for check in report["checks"] if check["id"] == "plugins.node_repl_websocket_compat")
    assert websocket_check["status"] == "pass"
    assert "ws_roots=1" in websocket_check["evidence"]
    assert not any(issue["id"] == "plugins.node_repl_playwright_module_unresolved" for issue in report["issues"])
    assert not any(issue["id"] == "plugins.node_repl_websocket_compat_missing" for issue in report["issues"])


def test_diagnostics_warns_when_browser_runtime_lacks_playwright_module_root(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    node_path = tmp_path / "node.exe"
    node_path.write_bytes(b"node")
    config_text = (codex_home_path / "config.toml").read_text(encoding="utf-8")
    (codex_home_path / "config.toml").write_text(
        "\n".join(
            [
                config_text,
                'BROWSER_USE_AVAILABLE_BACKENDS = "chrome,iab"',
                'NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = "abc123"',
                f'NODE_REPL_NODE_PATH = "{node_path.as_posix()}"',
                f'NODE_REPL_TRUSTED_CODE_PATHS = "{codex_home_path.as_posix()}"',
                'NODE_REPL_INSTRUCTIONS_USE_CASE_BROWSER = "browser"',
                'NODE_REPL_INSTRUCTIONS_USE_CASE_CHROME = "chrome"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    module_check = next(check for check in report["checks"] if check["id"] == "plugins.node_repl_node_modules")
    assert module_check["status"] == "warning"
    assert "playwright_roots=0" in module_check["evidence"]
    assert any(issue["id"] == "plugins.node_repl_playwright_module_unresolved" for issue in report["issues"])


def test_diagnostics_treats_missing_websocket_fallback_as_custom_script_info(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    node_modules_path = tmp_path / "node_modules"
    playwright_path = node_modules_path / "playwright"
    playwright_path.mkdir(parents=True)
    (playwright_path / "package.json").write_text('{"name":"playwright"}', encoding="utf-8")
    node_path = tmp_path / "node.exe"
    node_path.write_bytes(b"node")
    config_text = (codex_home_path / "config.toml").read_text(encoding="utf-8")
    (codex_home_path / "config.toml").write_text(
        "\n".join(
            [
                config_text,
                'BROWSER_USE_AVAILABLE_BACKENDS = "chrome,iab"',
                'NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = "abc123"',
                f'NODE_REPL_NODE_PATH = "{node_path.as_posix()}"',
                f'NODE_REPL_NODE_MODULE_DIRS = "{node_modules_path.as_posix()}"',
                f'NODE_REPL_TRUSTED_CODE_PATHS = "{codex_home_path.as_posix()}"',
                'NODE_REPL_INSTRUCTIONS_USE_CASE_BROWSER = "browser"',
                'NODE_REPL_INSTRUCTIONS_USE_CASE_CHROME = "chrome"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    websocket_check = next(check for check in report["checks"] if check["id"] == "plugins.node_repl_websocket_compat")
    assert websocket_check["status"] == "info"
    assert "ws_roots=0" in websocket_check["evidence"]
    assert not any(issue["id"] == "plugins.node_repl_websocket_compat_missing" for issue in report["issues"])


def test_diagnostics_reports_enabled_curated_plugin_missing_runtime(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    plugin_config = '[plugins."build-web-data-visualization@openai-curated"]\nenabled = true\n'
    (codex_home_path / "config.toml").write_text(plugin_config, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(plugin_config, encoding="utf-8")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    assert any(check["id"] == "plugins.curated_runtime_cache" and check["status"] == "warning" for check in report["checks"])
    assert any(issue["id"] == "plugins.curated_runtime_cache_incomplete" for issue in report["issues"])


def test_diagnostics_prefers_valid_curated_runtime_over_newer_empty_directory(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    plugin_name = "openai-developers"
    valid_runtime = "edd96568"
    empty_runtime = "9c1190e4"
    plugin_config = f'[plugins."{plugin_name}@openai-curated"]\nenabled = true\n'
    write_curated_plugin_file(codex_home_path, plugin_name, ".codex-plugin/plugin.json", root_name=valid_runtime)
    write_curated_plugin_file(codex_home_path, plugin_name, "skills/agents-sdk/SKILL.md", root_name=valid_runtime)
    empty_runtime_path = codex_home_path / "plugins" / "cache" / "openai-curated" / plugin_name / empty_runtime
    empty_runtime_path.mkdir(parents=True)
    os.utime(empty_runtime_path, (4000, 4000))
    (codex_home_path / "config.toml").write_text(plugin_config, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(plugin_config, encoding="utf-8")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    runtime_cache_check = next(check for check in report["checks"] if check["id"] == "plugins.curated_runtime_cache")
    assert runtime_cache_check["status"] == "pass"
    assert valid_runtime in "\n".join(runtime_cache_check["evidence"])
    assert not any(issue["id"] == "plugins.curated_runtime_cache_incomplete" for issue in report["issues"])


def create_test_junction(link_path: Path, target_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("junction regression is Windows-specific")
    link_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.mkdir(parents=True, exist_ok=True)
    completed_process = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed_process.returncode != 0:
        pytest.skip(f"cannot create junction in this test environment: {completed_process.stderr or completed_process.stdout}")


def test_diagnostics_reports_broken_curated_runtime_junction(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    plugin_name = "build-web-apps"
    installed_runtime = "e2d08a2e"
    stale_runtime = "9c1190e4"
    old_alias = "2abb1c44"
    plugin_config = f'[plugins."{plugin_name}@openai-curated"]\nenabled = true\n'
    write_curated_plugin_file(codex_home_path, plugin_name, ".codex-plugin/plugin.json", root_name=installed_runtime)
    write_curated_plugin_file(codex_home_path, plugin_name, "skills/frontend-app-builder/SKILL.md", root_name=installed_runtime)
    (codex_home_path / "config.toml").write_text(plugin_config, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(plugin_config, encoding="utf-8")
    plugin_root = codex_home_path / "plugins" / "cache" / "openai-curated" / plugin_name
    stale_runtime_path = plugin_root / stale_runtime
    create_test_junction(plugin_root / old_alias, stale_runtime_path)
    stale_runtime_path.rmdir()

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    runtime_link_check = next(check for check in report["checks"] if check["id"] == "plugins.curated_runtime_links")
    assert runtime_link_check["status"] == "critical"
    runtime_link_issue = next(issue for issue in report["issues"] if issue["id"] == "plugins.curated_runtime_link_broken")
    issue_text = "\n".join(runtime_link_issue["evidence"] + runtime_link_issue["affectedPaths"])
    assert old_alias in issue_text
    assert stale_runtime in issue_text


def test_diagnostics_reports_missing_advertised_skill_paths(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    plugin_name = "build-web-data-visualization"
    current_runtime = "9c1190e4"
    stale_runtime = "2abb1c44"
    plugin_config = f'[plugins."{plugin_name}@openai-curated"]\nenabled = true\n'
    write_curated_plugin_file(codex_home_path, plugin_name, ".codex-plugin/plugin.json", root_name=current_runtime)
    write_curated_plugin_file(codex_home_path, plugin_name, "skills/data-visualization/SKILL.md", root_name=current_runtime)
    (codex_home_path / "config.toml").write_text(plugin_config, encoding="utf-8")
    (codex_home_path / "managed_config.toml").write_text(plugin_config, encoding="utf-8")
    plugin_root = codex_home_path / "plugins" / "cache" / "openai-curated"
    exposed_skill_block = "\n".join(
        [
            "### Skill roots",
            f"- r9 = {plugin_root}",
            "### Available skills",
            f"- build-web-data-visualization:data-visualization: (file: r9/{plugin_name}/{stale_runtime}/skills/data-visualization/SKILL.md)",
        ]
    )
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "developer_message", "payload": {"text": exposed_skill_block}}, ensure_ascii=False) + "\n")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    assert any(check["id"] == "plugins.curated_runtime_cache" and check["status"] == "pass" for check in report["checks"])
    assert any(check["id"] == "plugins.advertised_skill_paths" and check["status"] == "warning" for check in report["checks"])
    missing_issue = next(issue for issue in report["issues"] if issue["id"] == "plugins.advertised_skill_path_missing")
    assert stale_runtime in "\n".join(missing_issue["evidence"] + missing_issue["affectedPaths"])


def test_advertised_skill_path_decoder_does_not_treat_windows_r_or_n_as_newline(tmp_path: Path) -> None:
    skill_root = tmp_path / "review_path_current" / "new_runtime"
    skill_block = "\n".join(
        [
            "### Skill roots",
            f"- r7 = {skill_root}",
            "### Available skills",
            "- reviewer: (file: r7/reviewer/SKILL.md)",
        ]
    )
    raw_jsonl = json.dumps({"type": "developer_message", "payload": {"text": skill_block}})

    assert extract_advertised_skill_paths(raw_jsonl) == [str(skill_root / "reviewer" / "SKILL.md")]


def test_diagnostics_downgrades_external_advertised_skill_paths(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path / "current")
    external_home_path = tmp_path / "old-home"
    exposed_skill_block = "\n".join(
        [
            "### Skill roots",
            f"- r0 = {external_home_path / 'skills'}",
            "### Available skills",
            "- doc: (file: r0/doc/SKILL.md)",
        ]
    )
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "developer_message", "payload": {"text": exposed_skill_block}}, ensure_ascii=False) + "\n")

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    advertised_check = next(check for check in report["checks"] if check["id"] == "plugins.advertised_skill_paths")
    assert advertised_check["status"] == "info"
    assert "external_missing_paths=1" in advertised_check["evidence"]
    assert not any(issue["id"] == "plugins.advertised_skill_path_missing" for issue in report["issues"])


def test_diagnostics_passes_when_no_advertised_skill_references_are_sampled(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    rollout_path = codex_home_path / "sessions" / "rollout-thread-2.jsonl"
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "message": r"Get-Content D:\.codex\plugins\cache\openai-bundled\browser\26.601.21317\skills\browser-use\SKILL.md failed",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    report = run_codex_diagnostics(str(codex_home_path), sidebar_limit=2, language="en")

    advertised_check = next(check for check in report["checks"] if check["id"] == "plugins.advertised_skill_paths")
    assert advertised_check["status"] == "pass"
    assert "referenced_paths=0" in advertised_check["evidence"]


def test_copy_resource_from_other_home_preserves_target_backup(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")
    (source_home_path / "AGENTS.md").write_text("source instructions", encoding="utf-8")
    (target_home_path / "AGENTS.md").write_text("target instructions", encoding="utf-8")

    result = copy_resource_from_home(
        str(target_home_path),
        str(source_home_path),
        "AGENTS.md",
        "AGENTS.md",
        overwrite=True,
    )

    assert (target_home_path / "AGENTS.md").read_text(encoding="utf-8") == "source instructions"
    assert result["overwroteExisting"] is True
    assert result["backup"]["resourceBackups"]


def test_copy_resource_from_other_home_rejects_protected_state_paths(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")
    with pytest.raises(ValueError):
        copy_resource_from_home(str(target_home_path), str(source_home_path), "state_5.sqlite", "state_5.sqlite", overwrite=True)
    with pytest.raises(ValueError):
        copy_resource_from_home(str(target_home_path), str(source_home_path), ".codex-global-state.json", ".codex-global-state.json", overwrite=True)
    with pytest.raises(ValueError):
        copy_resource_from_home(str(target_home_path), str(source_home_path), "config.toml", "config.toml", overwrite=True)
    with pytest.raises(ValueError):
        copy_resource_from_home(str(target_home_path), str(source_home_path), "sessions", "sessions", overwrite=True)


def test_preview_resource_copy_reports_overwrite(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")
    (source_home_path / "AGENTS.md").write_text("source", encoding="utf-8")
    (target_home_path / "AGENTS.md").write_text("target", encoding="utf-8")

    preview = preview_resource_copy(str(target_home_path), str(source_home_path), "AGENTS.md")

    assert preview["willOverwrite"] is True
    assert preview["source"]["sizeBytes"] == len("source")


def test_write_codex_resource_updates_text_with_backup(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "AGENTS.md").write_text("old instructions", encoding="utf-8")

    result = write_codex_resource(str(codex_home_path), "AGENTS.md", "new instructions")

    assert (codex_home_path / "AGENTS.md").read_text(encoding="utf-8") == "new instructions"
    assert result["backup"]["resourceBackups"]


def test_write_codex_resource_rejects_protected_state_files(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    with pytest.raises(ValueError):
        write_codex_resource(str(codex_home_path), "state_5.sqlite", "not sqlite")
    with pytest.raises(ValueError):
        write_codex_resource(str(codex_home_path), ".codex-global-state.json", "{}")
    with pytest.raises(ValueError):
        write_codex_resource(str(codex_home_path), "config.toml", "[settings]")
    with pytest.raises(ValueError):
        write_codex_resource(str(codex_home_path), "sessions/rollout.jsonl", "{}")


def test_running_codex_write_gate_requires_acknowledgement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.codex_data.detect_codex_processes",
        lambda: [{"imageName": "Codex.exe", "pid": "1234"}],
    )

    with pytest.raises(RuntimeError):
        enforce_write_safety(False)

    warnings = enforce_write_safety(True)
    assert warnings
    assert "Codex-related process is running" in warnings[0]


def test_restore_new_written_resource_moves_created_file_out_of_home(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)

    result = write_codex_resource(str(codex_home_path), "notes/new-memory.md", "new memory")
    restore_result = restore_backup(result["backup"]["backupId"])

    assert not (codex_home_path / "notes" / "new-memory.md").exists()
    assert "moved 1 created resources out of target home" in restore_result["notes"]


def test_restore_home_backup_restores_entire_database(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    result = write_codex_resource(str(codex_home_path), "AGENTS.md", "new instructions")
    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        connection.execute("DELETE FROM threads")
        connection.commit()

    restore_result = restore_backup(result["backup"]["backupId"])
    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)

    assert snapshot["summary"]["totalThreads"] == 3
    assert "restored entire SQLite database" in restore_result["notes"]


def test_import_thread_from_other_home_creates_current_home_thread(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")
    target_project_path = str(tmp_path / "target_project")

    result = import_thread_from_home(str(target_home_path), str(source_home_path), "thread-1", target_project_path)

    imported_thread = result["importedThreads"][0]
    assert imported_thread["sourceThreadId"] == "thread-1"
    assert imported_thread["newThreadId"] != "thread-1"
    snapshot = build_snapshot(str(target_home_path), sidebar_limit=10)
    imported_snapshot = next(thread for thread in snapshot["threads"] if thread["id"] == imported_thread["newThreadId"])
    assert imported_snapshot["projectPath"] == target_project_path
    assert Path(imported_thread["newRolloutPath"]).exists()
    session_index_text = (target_home_path / "session_index.jsonl").read_text(encoding="utf-8")
    assert imported_thread["newThreadId"] in session_index_text
    assert imported_thread["sessionIndexEntry"]["id"] == imported_thread["newThreadId"]


def test_preview_import_thread_reports_target_thread_id_and_bytes(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")

    preview = preview_import_thread_from_home(str(target_home_path), str(source_home_path), "thread-1")

    assert preview["sourceThreadId"] == "thread-1"
    assert preview["targetThreadId"] != "thread-1"
    assert preview["rolloutBytes"] > 0


def test_import_project_from_other_home_maps_project_paths(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")
    source_project_path = str(tmp_path / "source" / "project")
    target_project_path = str(tmp_path / "target_project")

    result = import_project_from_home(
        str(target_home_path),
        str(source_home_path),
        source_project_path,
        target_project_path,
    )

    assert len(result["importedThreads"]) == 3
    snapshot = build_snapshot(str(target_home_path), sidebar_limit=10)
    imported_ids = {thread["newThreadId"] for thread in result["importedThreads"]}
    imported_snapshots = [thread for thread in snapshot["threads"] if thread["id"] in imported_ids]
    assert len(imported_snapshots) == 3
    assert all(thread["projectPath"] == target_project_path for thread in imported_snapshots)
    session_index_text = (target_home_path / "session_index.jsonl").read_text(encoding="utf-8")
    assert all(thread_id in session_index_text for thread_id in imported_ids)


def test_preview_import_project_reports_thread_count_and_bytes(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")
    source_project_path = str(tmp_path / "source" / "project")

    preview = preview_import_project_from_home(
        str(target_home_path),
        str(source_home_path),
        source_project_path,
        str(tmp_path / "target_project"),
    )

    assert preview["matchedThreads"] == 3
    assert preview["rolloutBytes"] > 0


def test_import_project_prevalidates_missing_source_rollouts(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")
    source_project_path = str(tmp_path / "source" / "project")
    missing_rollout_path = source_home_path / "sessions" / "rollout-thread-1.jsonl"
    missing_rollout_path.unlink()

    with pytest.raises(FileNotFoundError):
        import_project_from_home(str(target_home_path), str(source_home_path), source_project_path, str(tmp_path / "target_project"))


def test_restore_copied_new_resource_moves_created_resource_out_of_home(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")
    (source_home_path / "memories").mkdir()
    (source_home_path / "memories" / "MEMORY.md").write_text("source memory", encoding="utf-8")

    result = copy_resource_from_home(
        str(target_home_path),
        str(source_home_path),
        "memories/MEMORY.md",
        "memories/MEMORY.md",
        overwrite=False,
    )
    restore_result = restore_backup(result["backup"]["backupId"])

    assert not (target_home_path / "memories" / "MEMORY.md").exists()
    assert "moved 1 created resources out of target home" in restore_result["notes"]


def test_restore_overwritten_directory_removes_extra_copied_files(tmp_path: Path) -> None:
    source_home_path = create_test_codex_home(tmp_path / "source")
    target_home_path = create_test_codex_home(tmp_path / "target")
    (source_home_path / "memories").mkdir()
    (source_home_path / "memories" / "MEMORY.md").write_text("source memory", encoding="utf-8")
    (source_home_path / "memories" / "extra.md").write_text("extra", encoding="utf-8")
    (target_home_path / "memories").mkdir()
    (target_home_path / "memories" / "MEMORY.md").write_text("target memory", encoding="utf-8")

    result = copy_resource_from_home(str(target_home_path), str(source_home_path), "memories", "memories", overwrite=True)
    restore_backup(result["backup"]["backupId"])

    assert (target_home_path / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "target memory"
    assert not (target_home_path / "memories" / "extra.md").exists()


def test_snapshot_marks_missing_rollout_as_repair_state(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    (codex_home_path / "sessions" / "rollout-thread-1.jsonl").unlink()

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=10)
    repair_thread = next(thread for thread in snapshot["threads"] if thread["id"] == "thread-1")

    assert repair_thread["visibility"] == "missing_file"
    assert snapshot["summary"]["needsRepairThreads"] == 1


def test_preview_project_rename_and_slim_thread(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    source_project_path = tmp_path / "project"
    target_project_path = tmp_path / "renamed_project"
    source_project_path.mkdir()

    rename_preview = preview_project_rename(str(codex_home_path), str(source_project_path), str(target_project_path))
    slim_preview = preview_slim_thread(str(codex_home_path), "thread-1")

    assert rename_preview["matchedThreads"] == 3
    assert rename_preview["willRenameFolder"] is True
    assert slim_preview["scan"]["lineCount"] == 2


def test_capabilities_support_chinese_and_default_english() -> None:
    client = TestClient(server.app)

    english_payload = client.get("/api/capabilities").json()
    chinese_payload = client.get("/api/capabilities", params={"lang": "zh"}).json()

    assert english_payload["language"] == "en"
    assert english_payload["mcpPath"] == "/mcp"
    assert english_payload["capabilities"][0]["purpose"].startswith("Get the per-process")
    assert chinese_payload["language"] == "zh"
    assert chinese_payload["capabilities"][0]["purpose"].startswith("获取写入接口")
    assert chinese_payload["commonQueryParameters"]["lang"].startswith("用于指定能力说明语言")
    assert chinese_payload["safetyModel"]["deleteBehavior"].startswith("线程删除默认实现为归档")


def mcp_call(client: TestClient, request_id: int, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == request_id
    return payload


def local_authorization_headers(client: TestClient, codex_home_path: Path) -> dict[str, str]:
    token_payload = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)}).json()
    return {token_payload["headerName"]: token_payload["token"]}


def mcp_local_authorization(client: TestClient, codex_home_path: Path, request_id: int = 1000) -> str:
    payload = mcp_call(
        client,
        request_id,
        "tools/call",
        {"name": "codex_auth_token", "arguments": {"codexHome": str(codex_home_path)}},
    )
    return str(payload["result"]["structuredContent"]["token"])


def test_mcp_initialize_tools_list_and_read_tool(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    client = TestClient(server.app)

    initialize_payload = mcp_call(client, 1, "initialize")
    assert initialize_payload["result"]["serverInfo"]["name"] == "codex-home-manager"
    assert initialize_payload["result"]["capabilities"]["tools"] == {"listChanged": False}

    tools_payload = mcp_call(client, 2, "tools/list")
    tools = tools_payload["result"]["tools"]
    tool_names = {tool["name"] for tool in tools}
    assert "codex_snapshot" in tool_names
    assert "codex_preview_thread_action" in tool_names
    assert "codex_show_thread" in tool_names
    assert "codex_write_resource" in tool_names
    assert "codex_preview_move_thread_workspace" in tool_names
    assert "codex_move_thread_workspace" in tool_names
    assert len(tools) >= 32
    api_token = mcp_local_authorization(client, codex_home_path)

    snapshot_payload = mcp_call(
        client,
        3,
        "tools/call",
        {
            "name": "codex_snapshot",
            "arguments": {"codexHome": str(codex_home_path), "apiToken": api_token, "sidebarLimit": 50},
        },
    )
    result = snapshot_payload["result"]
    assert result["structuredContent"]["summary"]["totalThreads"] == 3
    assert result["content"][0]["type"] == "text"

    move_preview_payload = mcp_call(
        client,
        4,
        "tools/call",
        {
            "name": "codex_preview_move_thread_workspace",
            "arguments": {
                "codexHome": str(codex_home_path),
                "apiToken": api_token,
                "threadId": "thread-1",
                "targetProjectPath": str(tmp_path / "HF"),
                "moveWorkspaceFiles": False,
            },
        },
    )
    move_preview = move_preview_payload["result"]["structuredContent"]
    assert move_preview["threadId"] == "thread-1"
    assert move_preview["matchedThreads"] == 3
    assert move_preview["operationPreviewId"]


def test_rest_and_mcp_daily_tokens_include_zero_days(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-0.jsonl", "2026-06-03T12:00:00Z", 100, 100)
    append_token_count_event(codex_home_path / "sessions" / "rollout-thread-0.jsonl", "2026-06-05T12:00:00Z", 125, 25)
    client = TestClient(server.app)
    headers = local_authorization_headers(client, codex_home_path)
    api_token = mcp_local_authorization(client, codex_home_path)

    response = client.get(
        "/api/threads/thread-0/daily-tokens",
        params={"codex_home": str(codex_home_path), "sidebar_limit": 10},
        headers=headers,
    )
    assert response.status_code == 200
    rest_payload = response.json()
    assert [day["date"] for day in rest_payload["days"]] == ["2026-06-03", "2026-06-04", "2026-06-05"]
    assert rest_payload["days"][1]["totalTokens"] == 0
    assert rest_payload["days"][1]["hasData"] is False
    assert rest_payload["summary"]["activeDays"] == 2
    assert rest_payload["summary"]["rangeDays"] == 3
    assert rest_payload["summary"]["zeroDays"] == 1

    mcp_payload = mcp_call(
        client,
        1,
        "tools/call",
        {
            "name": "codex_thread_daily_tokens",
            "arguments": {
                "codexHome": str(codex_home_path),
                "apiToken": api_token,
                "threadId": "thread-0",
                "sidebarLimit": 10,
            },
        },
    )
    assert not mcp_payload["result"].get("isError", False)
    mcp_usage = mcp_payload["result"]["structuredContent"]
    assert [day["date"] for day in mcp_usage["days"]] == ["2026-06-03", "2026-06-04", "2026-06-05"]
    assert mcp_usage["days"][1]["totalTokens"] == 0
    assert mcp_usage["days"][1]["hasData"] is False
    assert mcp_usage["summary"]["rangeDays"] == 3


def test_thread_action_preview_uses_single_thread_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    client = TestClient(server.app)
    headers = local_authorization_headers(client, codex_home_path)
    api_token = mcp_local_authorization(client, codex_home_path)

    def fail_if_full_detail_is_loaded(*args: object, **kwargs: object) -> None:
        raise AssertionError("action preview must not load full thread detail")

    def fail_if_snapshot_is_loaded(*args: object, **kwargs: object) -> None:
        raise AssertionError("action preview must not load full snapshot")

    monkeypatch.setattr(server, "get_thread_detail", fail_if_full_detail_is_loaded)
    monkeypatch.setattr(server, "build_snapshot", fail_if_snapshot_is_loaded)

    response = client.get(
        "/api/threads/thread-0/action-preview",
        params={"codex_home": str(codex_home_path), "action": "show"},
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["threadId"] == "thread-0"
    assert payload["rolloutStat"]["exists"] is True

    mcp_preview = mcp_call(
        client,
        1,
        "tools/call",
        {
            "name": "codex_preview_thread_action",
            "arguments": {
                "codexHome": str(codex_home_path),
                "apiToken": api_token,
                "threadId": "thread-0",
                "action": "show",
            },
        },
    )
    assert not mcp_preview["result"].get("isError", False)
    assert mcp_preview["result"]["structuredContent"]["threadId"] == "thread-0"


def test_auth_token_rejects_public_origin_and_allows_only_loopback_same_origin() -> None:
    public_origin = "https://codex-home-manager.simplezion.com"
    intent_headers = {
        "Origin": public_origin,
        "X-Codex-Manager-Token-Intent": "interactive-write",
    }
    local_client = TestClient(server.app, base_url="http://127.0.0.1:8765")

    public_response = local_client.get("/api/auth/token", headers=intent_headers)
    assert public_response.status_code == 403

    cross_origin_response = local_client.get(
        "/api/auth/token",
        headers={"Origin": "http://localhost:8765"},
    )
    assert cross_origin_response.status_code == 403

    local_response = local_client.get(
        "/api/auth/token",
        headers={"Origin": "http://127.0.0.1:8765"},
    )
    assert local_response.status_code == 200
    assert local_response.json()["token"] != server.api_token
    assert local_response.json()["expiresAtMs"] is not None

    public_mcp_payload = local_client.post(
        "/mcp",
        headers=intent_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "codex_auth_token", "arguments": {}},
        },
    ).json()
    assert public_mcp_payload["result"]["isError"] is True
    assert public_mcp_payload["result"]["structuredContent"]["status"] == 403


def test_browser_data_reads_require_short_lived_authorization_bound_to_real_codex_home(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path / "authorized")
    other_codex_home_path = create_test_codex_home(tmp_path / "other")
    local_client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    local_origin = "http://127.0.0.1:8765"
    public_origin = "https://codex-home-manager.simplezion.com"

    public_response = local_client.get(
        "/api/resources/read",
        params={"codex_home": "C:\\", "relative_path": r"Windows\win.ini"},
        headers={"Origin": public_origin},
    )
    assert public_response.status_code == 403

    local_agent_without_authorization = local_client.get(
        "/api/resources/read",
        params={"codex_home": str(codex_home_path), "relative_path": "config.toml"},
    )
    assert local_agent_without_authorization.status_code == 401

    unauthenticated_response = local_client.get(
        "/api/resources/read",
        params={"codex_home": str(codex_home_path), "relative_path": "config.toml"},
        headers={"Origin": local_origin},
    )
    assert unauthenticated_response.status_code == 401

    token_response = local_client.get(
        "/api/auth/token",
        params={"codex_home": str(codex_home_path)},
        headers={"Origin": local_origin},
    )
    assert token_response.status_code == 200
    assert token_response.json()["expiresAtMs"] is not None
    assert token_response.json()["expiresAtMs"] <= int(time.time() * 1000) + 6 * 60 * 1000

    authorized_response = local_client.get(
        "/api/resources/read",
        params={"codex_home": str(codex_home_path), "relative_path": "config.toml"},
        headers={"Origin": local_origin},
    )
    assert authorized_response.status_code == 200

    wrong_home_response = local_client.get(
        "/api/resources/read",
        params={"codex_home": str(other_codex_home_path), "relative_path": "config.toml"},
        headers={"Origin": local_origin},
    )
    assert wrong_home_response.status_code == 403

    token = token_response.json()["token"]
    server.authorization_store[token]["expiresAtMs"] = 0
    expired_response = local_client.get(
        "/api/resources/read",
        params={"codex_home": str(codex_home_path), "relative_path": "config.toml"},
        headers={"Origin": local_origin},
    )
    assert expired_response.status_code == 401

    invalid_home_response = local_client.get(
        "/api/auth/token",
        params={"codex_home": str(tmp_path / "not-a-codex-home")},
        headers={"Origin": local_origin},
    )
    assert invalid_home_response.status_code == 400


def test_mcp_local_agent_can_authorize_a_real_codex_home_but_public_origin_cannot_read(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    client = TestClient(server.app)

    token_payload = mcp_call(
        client,
        1,
        "tools/call",
        {"name": "codex_auth_token", "arguments": {"codexHome": str(codex_home_path)}},
    )
    token = token_payload["result"]["structuredContent"]["token"]
    snapshot_payload = mcp_call(
        client,
        2,
        "tools/call",
        {
            "name": "codex_snapshot",
            "arguments": {"codexHome": str(codex_home_path), "apiToken": token, "sidebarLimit": 50},
        },
    )
    assert snapshot_payload["result"]["structuredContent"]["summary"]["totalThreads"] == 3

    public_payload = client.post(
        "/mcp",
        headers={"Origin": "https://codex-home-manager.simplezion.com"},
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "codex_read_resource",
                "arguments": {
                    "codexHome": "C:\\",
                    "relativePath": r"Windows\win.ini",
                    "apiToken": token,
                },
            },
        },
    ).json()
    assert public_payload["result"]["isError"] is True
    assert public_payload["result"]["structuredContent"]["status"] == 403


def test_preview_ticket_is_consumed_once_after_success_and_replay_fails(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    client = TestClient(server.app)
    token_payload = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)}).json()
    headers = {token_payload["headerName"]: token_payload["token"]}
    request_body = {
        "relativePath": "AGENTS.md",
        "content": "single use",
        "createParentDirectories": True,
        "acknowledgeCodexRunningRisk": True,
    }
    preview = client.post(
        "/api/resources/write/preview",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=request_body,
    ).json()
    write_body = {**request_body, "operationPreviewId": preview["operationPreviewId"], "inputHash": preview["inputHash"]}

    first_response = client.post(
        "/api/resources/write",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=write_body,
    )
    replay_response = client.post(
        "/api/resources/write",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=write_body,
    )

    assert first_response.status_code == 200
    assert replay_response.status_code == 428


def test_preview_ticket_is_invalidated_when_target_state_changes_after_preview(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    resource_path = codex_home_path / "AGENTS.md"
    resource_path.write_text("before preview", encoding="utf-8")
    client = TestClient(server.app)
    token_payload = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)}).json()
    headers = {token_payload["headerName"]: token_payload["token"]}
    request_body = {
        "relativePath": "AGENTS.md",
        "content": "requested content",
        "createParentDirectories": True,
        "acknowledgeCodexRunningRisk": True,
    }
    preview = client.post(
        "/api/resources/write/preview",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=request_body,
    ).json()
    resource_path.write_text("external change", encoding="utf-8")

    response = client.post(
        "/api/resources/write",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json={
            **request_body,
            "operationPreviewId": preview["operationPreviewId"],
            "inputHash": preview["inputHash"],
        },
    )

    assert response.status_code == 409
    assert "state changed" in response.json()["detail"]
    assert resource_path.read_text(encoding="utf-8") == "external change"


def test_preview_ticket_allows_only_one_concurrent_success(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    client = TestClient(server.app)
    token_payload = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)}).json()
    headers = {token_payload["headerName"]: token_payload["token"]}
    request_body = {
        "relativePath": "AGENTS.md",
        "content": "concurrent single use",
        "createParentDirectories": True,
        "acknowledgeCodexRunningRisk": True,
    }
    preview = client.post(
        "/api/resources/write/preview",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=request_body,
    ).json()
    write_body = {**request_body, "operationPreviewId": preview["operationPreviewId"], "inputHash": preview["inputHash"]}

    def submit_write() -> int:
        return client.post(
            "/api/resources/write",
            params={"codex_home": str(codex_home_path)},
            headers=headers,
            json=write_body,
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        status_codes = sorted(executor.map(lambda _: submit_write(), range(2)))

    assert status_codes == [200, 428]


def test_mcp_write_tools_require_token_and_preview(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    client = TestClient(server.app)

    public_token_payload = client.post(
        "/mcp",
        headers={"Origin": "https://codex-home-manager.simplezion.com"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "codex_auth_token", "arguments": {}},
        },
    ).json()
    assert public_token_payload["result"]["isError"] is True
    assert public_token_payload["result"]["structuredContent"]["status"] == 403

    token_payload = mcp_call(
        client,
        2,
        "tools/call",
        {"name": "codex_auth_token", "arguments": {"codexHome": str(codex_home_path)}},
    )
    api_token = token_payload["result"]["structuredContent"]["token"]

    preview_payload = mcp_call(
        client,
        3,
        "tools/call",
        {
            "name": "codex_preview_thread_action",
            "arguments": {
                "codexHome": str(codex_home_path),
                "apiToken": api_token,
                "threadId": "thread-0",
                "action": "show",
            },
        },
    )
    preview = preview_payload["result"]["structuredContent"]

    missing_token_payload = mcp_call(
        client,
        4,
        "tools/call",
        {
            "name": "codex_show_thread",
            "arguments": {
                "codexHome": str(codex_home_path),
                "threadId": "thread-0",
                "operationPreviewId": preview["operationPreviewId"],
                "inputHash": preview["inputHash"],
                "acknowledgeCodexRunningRisk": True,
            },
        },
    )
    assert missing_token_payload["result"]["isError"] is True
    assert missing_token_payload["result"]["structuredContent"]["status"] == 401

    wrong_hash_payload = mcp_call(
        client,
        5,
        "tools/call",
        {
            "name": "codex_show_thread",
            "arguments": {
                "codexHome": str(codex_home_path),
                "threadId": "thread-0",
                "apiToken": api_token,
                "operationPreviewId": preview["operationPreviewId"],
                "inputHash": "bad-hash",
                "acknowledgeCodexRunningRisk": True,
            },
        },
    )
    assert wrong_hash_payload["result"]["isError"] is True
    assert wrong_hash_payload["result"]["structuredContent"]["status"] == 428

    success_payload = mcp_call(
        client,
        6,
        "tools/call",
        {
            "name": "codex_show_thread",
            "arguments": {
                "codexHome": str(codex_home_path),
                "threadId": "thread-0",
                "apiToken": api_token,
                "operationPreviewId": preview["operationPreviewId"],
                "inputHash": preview["inputHash"],
                "acknowledgeCodexRunningRisk": True,
            },
        },
    )
    assert success_payload["result"].get("isError") is not True
    assert success_payload["result"]["structuredContent"]["backup"]["backupId"]


def test_write_api_requires_token_preview_and_running_codex_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    client = TestClient(server.app)
    token_payload = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)}).json()
    headers = {token_payload["headerName"]: token_payload["token"]}
    path = "/api/threads/thread-0/show"
    params = {
        "codex_home": str(codex_home_path),
        "acknowledgeCodexRunningRisk": "true",
    }

    assert client.post(path, params=params).status_code == 401

    hostile_response = client.post(
        path,
        params=params,
        headers={**headers, "Origin": "https://example.invalid"},
    )
    assert hostile_response.status_code == 403

    no_preview_response = client.post(path, params=params, headers=headers)
    assert no_preview_response.status_code == 428

    preview_response = client.get(
        "/api/threads/thread-0/action-preview",
        params={"codex_home": str(codex_home_path), "action": "show"},
        headers=headers,
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()

    wrong_hash_response = client.post(
        path,
        params={
            "codex_home": str(codex_home_path),
            "operationPreviewId": preview["operationPreviewId"],
            "inputHash": "bad-hash",
            "acknowledgeCodexRunningRisk": "true",
        },
        headers=headers,
    )
    assert wrong_hash_response.status_code == 428

    monkeypatch.setattr(
        "backend.codex_data.detect_codex_processes",
        lambda: [{"imageName": "Codex.exe", "pid": "1234"}],
    )
    no_ack_response = client.post(
        path,
        params={
            "codex_home": str(codex_home_path),
            "operationPreviewId": preview["operationPreviewId"],
            "inputHash": preview["inputHash"],
        },
        headers=headers,
    )
    assert no_ack_response.status_code == 409

    success_response = client.post(
        path,
        params={
            "codex_home": str(codex_home_path),
            "operationPreviewId": preview["operationPreviewId"],
            "inputHash": preview["inputHash"],
            "acknowledgeCodexRunningRisk": "true",
        },
        headers=headers,
    )
    assert success_response.status_code == 200
    assert success_response.json()["backup"]["backupId"]

    skip_preview_response = client.get(
        "/api/threads/thread-0/action-preview",
        params={"codex_home": str(codex_home_path), "action": "show"},
        headers=headers,
    )
    assert skip_preview_response.status_code == 200
    skip_preview = skip_preview_response.json()
    skip_backup_response = client.post(
        path,
        params={
            "codex_home": str(codex_home_path),
            "operationPreviewId": skip_preview["operationPreviewId"],
            "inputHash": skip_preview["inputHash"],
            "acknowledgeCodexRunningRisk": "true",
            "createBackup": "false",
        },
        headers=headers,
    )
    assert skip_backup_response.status_code == 200
    assert skip_backup_response.json()["backup"]["backupId"] is None
    assert skip_backup_response.json()["backup"]["skipped"] is True


def test_resource_write_api_binds_preview_to_content_hash(tmp_path: Path) -> None:
    codex_home_path = create_test_codex_home(tmp_path)
    client = TestClient(server.app)
    token_payload = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)}).json()
    headers = {token_payload["headerName"]: token_payload["token"]}
    body = {
        "relativePath": "AGENTS.md",
        "content": "new instructions",
        "createParentDirectories": True,
        "acknowledgeCodexRunningRisk": True,
    }

    preview_response = client.post(
        "/api/resources/write/preview",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=body,
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()

    assert client.post("/api/resources/write", params={"codex_home": str(codex_home_path)}, json=body).status_code == 401

    missing_preview_response = client.post(
        "/api/resources/write",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=body,
    )
    assert missing_preview_response.status_code == 428

    tampered_body = {
        **body,
        "content": "tampered instructions",
        "operationPreviewId": preview["operationPreviewId"],
        "inputHash": preview["inputHash"],
    }
    tampered_response = client.post(
        "/api/resources/write",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=tampered_body,
    )
    assert tampered_response.status_code == 428

    success_body = {
        **body,
        "operationPreviewId": preview["operationPreviewId"],
        "inputHash": preview["inputHash"],
    }
    success_response = client.post(
        "/api/resources/write",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=success_body,
    )
    assert success_response.status_code == 200
    assert (codex_home_path / "AGENTS.md").read_text(encoding="utf-8") == "new instructions"
