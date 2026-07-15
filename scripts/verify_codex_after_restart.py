from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from datetime import UTC, datetime
from typing import Any


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "codex_home_manager"))
sys.path.insert(0, str(workspace_root / "scripts"))

from audit_codex_thread_histories import audit_threads
from backend.diagnostics import capture_notify_log_boundary, query_legacy_notify_os206, run_codex_diagnostics
from backend.thread_history_repair import user_prompt_prefix_fingerprint
from backend.windows_paths import windows_path_is_within, windows_path_key
from run_codex_diagnostics_snapshot import diagnostics_gate_failures
from repair_manifest_chain import load_manifest_pair, write_manifest_pair
from live_validation_contract import (
    exact_call_arguments,
    exact_challenge_envelope,
    exact_nested_mcp_event,
    inspect_png_screenshot,
    validate_browser_probe_arguments,
    validate_ui_probe_arguments,
)


required_plugin_names = ("sites", "browser", "chrome", "computer-use", "latex")
windows_incompatible_plugin_selectors = (
    "build-ios-apps@openai-curated",
    "build-macos-apps@openai-curated",
)
required_diagnostic_checks = (
    "config.toml_parse",
    "sqlite.state",
    "threads.rollout_jsonl_integrity",
    "threads.main_title_stream",
    "threads.main_event_stream",
    "plugins.official_thread_tools_exposure",
    "plugins.macos_plugin_disabled_on_windows",
    "plugins.browser",
    "plugins.sites",
    "plugins.chrome",
    "plugins.computer-use",
    "plugins.latex",
    "plugins.marketplace",
    "plugins.bundled_marketplace_source",
    "plugins.stale_restore_artifacts",
    "plugins.chrome_native_hosts",
    "plugins.chrome_native_messaging_manifests",
    "plugins.skill_manifests",
    "plugins.advertised_skill_paths",
    "plugins.curated_marketplace_manifests",
    "plugins.node_repl_config_layer_consistency",
    "plugins.node_repl_desktop_privileged_mode",
    "plugins.computer_use_privileged_runtime",
    "runtime.legacy_notify_hook",
)
required_live_validation_checks = (
    "browser.live_tool",
    "chrome.live_tool",
    "computer_use.live_tool",
    "node_repl.live_tool",
    "official_thread_tools.live_tool",
    "sidebar.thread_visibility",
    "large_thread.ui_responsiveness",
)
required_live_validation_methods = {
    "browser.live_tool": "direct_tool_call",
    "chrome.live_tool": "direct_tool_call",
    "computer_use.live_tool": "direct_tool_call",
    "node_repl.live_tool": "direct_tool_call",
    "official_thread_tools.live_tool": "visible_cross_thread_delivery",
    "sidebar.thread_visibility": "visual_and_state_crosscheck",
    "large_thread.ui_responsiveness": "timed_ui_interaction",
}


def hidden_subprocess_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _validate_notify_log_boundary_continuity(
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> None:
    if int(baseline.get("schema_version") or 0) != 2 or int(current.get("schema_version") or 0) != 2:
        raise RuntimeError("notify log boundary schema is unsupported")
    baseline_identity = baseline.get("database_identity")
    current_identity = current.get("database_identity")
    if not isinstance(baseline_identity, dict) or not isinstance(current_identity, dict):
        raise RuntimeError("notify log database identity is missing")
    if (
        windows_path_key(baseline_identity.get("path") or "")
        != windows_path_key(current_identity.get("path") or "")
        or int(baseline_identity.get("device") or 0) != int(current_identity.get("device") or 0)
        or int(baseline_identity.get("inode") or 0) != int(current_identity.get("inode") or 0)
    ):
        raise RuntimeError("notify log database identity changed during validation")
    if (
        int(baseline.get("desktop_root_pid") or 0) != int(current.get("desktop_root_pid") or 0)
        or int(baseline.get("desktop_root_created_at_epoch") or 0)
        != int(current.get("desktop_root_created_at_epoch") or 0)
    ):
        raise RuntimeError("Codex Desktop root process changed during notify validation")
    if windows_path_key(baseline.get("current_appx_install_path") or "") != windows_path_key(
        current.get("current_appx_install_path") or ""
    ):
        raise RuntimeError("current Codex AppX changed during notify validation")
    def current_appx_app_server_pids(boundary: dict[str, Any]) -> set[int]:
        appx_root = str(boundary.get("current_appx_install_path") or "")
        return {
            int(process.get("pid") or 0)
            for process in boundary.get("app_servers") or []
            if int(process.get("pid") or 0) > 0
            and appx_root
            and windows_path_is_within(str(process.get("executablePath") or ""), appx_root)
        }

    baseline_app_servers = current_appx_app_server_pids(baseline)
    current_app_servers = current_appx_app_server_pids(current)
    if not baseline_app_servers or not current_app_servers or not baseline_app_servers.intersection(current_app_servers):
        raise RuntimeError("current-AppX Codex Desktop app-server continuity was not preserved")
    if not set(map(str, baseline.get("process_uuids") or [])).intersection(
        set(map(str, current.get("process_uuids") or []))
    ):
        raise RuntimeError("notify log has no process_uuid continuous with the Desktop process tree")
    if int(current.get("max_id") or 0) < int(baseline.get("max_id") or 0):
        raise RuntimeError("notify log max id moved backwards during validation")


def validate_restart_notify_log_window(
    log_database_path: Path,
    baseline: dict[str, Any],
    *,
    min_epoch: int,
    process_snapshot: list[dict[str, Any]] | None = None,
    current_appx_install: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_boundary = capture_notify_log_boundary(
        log_database_path,
        process_snapshot=process_snapshot,
        current_appx_install=current_appx_install,
    )
    _validate_notify_log_boundary_continuity(baseline, final_boundary)
    process_uuids = sorted(
        set(map(str, baseline.get("process_uuids") or []))
        | set(map(str, final_boundary.get("process_uuids") or []))
    )
    query = query_legacy_notify_os206(
        log_database_path,
        min_epoch=min_epoch,
        process_uuids=process_uuids,
        after_id=max(
            0,
            min(
                int(baseline["process_started_at_id"]),
                int(final_boundary["process_started_at_id"]),
            )
            - 1,
        ),
        max_id=int(final_boundary["max_id"]),
    )
    if query["match_count"]:
        matched_ids = ", ".join(str(item["id"]) for item in query["matches"])
        raise RuntimeError(f"current post-restart process logged legacy_notify os error 206 at ids: {matched_ids}")
    return {
        "schema_version": 2,
        "min_epoch": min_epoch,
        "initial_boundary": baseline,
        "live_probe_baseline": final_boundary,
        "query": query,
    }


def validate_live_notify_log_window(
    log_database_path: Path,
    baseline: dict[str, Any],
    *,
    min_epoch: int,
    process_snapshot: list[dict[str, Any]] | None = None,
    current_appx_install: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_boundary = capture_notify_log_boundary(
        log_database_path,
        process_snapshot=process_snapshot,
        current_appx_install=current_appx_install,
    )
    _validate_notify_log_boundary_continuity(baseline, final_boundary)
    process_uuids = sorted(
        set(map(str, baseline.get("process_uuids") or []))
        | set(map(str, final_boundary.get("process_uuids") or []))
    )
    query = query_legacy_notify_os206(
        log_database_path,
        min_epoch=min_epoch,
        process_uuids=process_uuids,
        after_id=int(baseline["max_id"]),
        max_id=int(final_boundary["max_id"]),
    )
    if query["match_count"]:
        matched_ids = ", ".join(str(item["id"]) for item in query["matches"])
        raise RuntimeError(f"live probes logged legacy_notify os error 206 at ids: {matched_ids}")
    return {
        "schema_version": 2,
        "baseline": baseline,
        "final_boundary": final_boundary,
        "query": query,
    }


def tree_manifest(root: Path) -> dict[str, tuple[str, int, str]]:
    if not root.is_dir():
        raise RuntimeError(f"plugin tree is missing: {root}")
    manifest: dict[str, tuple[str, int, str]] = {".": ("directory", 0, "")}
    pending = [root]
    while pending:
        parent = pending.pop()
        for entry in os.scandir(parent):
            path = Path(entry.path)
            relative = str(path.relative_to(root)).replace("\\", "/")
            if path.is_junction() or entry.is_symlink():
                manifest[relative] = ("junction", 0, os.path.realpath(path))
            elif entry.is_dir(follow_symlinks=False):
                manifest[relative] = ("directory", 0, "")
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                manifest[relative] = ("file", path.stat().st_size, digest)
            else:
                manifest[relative] = ("other", 0, "")
    return manifest


def compare_plugin_tree(source: Path, destination: Path) -> None:
    if tree_manifest(source) != tree_manifest(destination):
        raise RuntimeError(f"plugin tree mismatch: {source} != {destination}")


def assert_regular_directory(path: Path, label: str) -> None:
    if path.is_junction() or path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"{label} must be a regular directory: {path}")


def resolve_appx_bundled_marketplace() -> Path:
    if os.name != "nt":
        raise RuntimeError("AppX bundled plugin source resolution is only supported on Windows")
    command = (
        "$package = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop | "
        "Where-Object InstallLocation | Sort-Object Version -Descending | Select-Object -First 1; "
        "if ($null -eq $package) { throw 'OpenAI.Codex AppX package was not found' }; "
        "$package.InstallLocation"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=hidden_subprocess_flags(),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"could not resolve current Codex AppX package: {completed.stderr or completed.stdout}")
    install_location = Path(completed.stdout.strip())
    marketplace = install_location / "app" / "resources" / "plugins" / "openai-bundled"
    if not marketplace.is_dir():
        raise RuntimeError(f"current AppX bundled marketplace is missing: {marketplace}")
    return marketplace


def validate_plugin_trees(
    codex_home: Path,
    appx_marketplace: Path,
    plugin_registry: list[dict[str, Any]],
) -> dict[str, Any]:
    persistent_marketplace = codex_home / "cache" / "bundled-marketplaces" / "openai-bundled"
    temporary_marketplace = codex_home / ".tmp" / "bundled-marketplaces" / "openai-bundled"
    assert_regular_directory(appx_marketplace, "AppX bundled marketplace")
    assert_regular_directory(persistent_marketplace, "persistent bundled marketplace")
    assert_regular_directory(temporary_marketplace, "temporary bundled marketplace")
    compare_plugin_tree(appx_marketplace, persistent_marketplace)
    compare_plugin_tree(appx_marketplace, temporary_marketplace)

    registry_by_name = {
        str(item.get("plugin_id") or "").split("@", 1)[0].casefold(): item
        for item in plugin_registry
        if isinstance(item, dict)
    }
    plugin_reports: list[dict[str, Any]] = []
    for plugin_name in required_plugin_names:
        registry_item = registry_by_name.get(plugin_name.casefold())
        if registry_item is None:
            raise RuntimeError(f"validated plugin registry result is missing: {plugin_name}")
        version = str(registry_item.get("version") or "")
        if not version:
            raise RuntimeError(f"validated plugin version is empty: {plugin_name}")
        source_root = persistent_marketplace / "plugins" / plugin_name
        version_root = codex_home / "plugins" / "cache" / "openai-bundled" / plugin_name / version
        latest_root = version_root.parent / "latest"
        assert_regular_directory(source_root, f"bundled plugin source {plugin_name}")
        assert_regular_directory(version_root, f"bundled plugin version cache {plugin_name}")
        compare_plugin_tree(source_root, version_root)
        if not latest_root.is_junction():
            raise RuntimeError(f"plugin latest path is not a junction: {latest_root}")
        try:
            latest_matches_version = os.path.samefile(latest_root, version_root)
        except OSError as error:
            raise RuntimeError(f"plugin latest junction cannot be resolved: {latest_root}") from error
        if not latest_matches_version:
            raise RuntimeError(f"plugin latest junction target mismatch: {latest_root} != {version_root}")
        compare_plugin_tree(source_root, latest_root)
        plugin_reports.append(
            {
                "plugin": plugin_name,
                "version": version,
                "source": str(source_root),
                "version_cache": str(version_root),
                "latest": str(latest_root),
                "latest_target": str(version_root.resolve()),
            }
        )
    return {
        "appx_marketplace": str(appx_marketplace),
        "persistent_marketplace": str(persistent_marketplace),
        "temporary_marketplace": str(temporary_marketplace),
        "plugins": plugin_reports,
    }


def validate_manifest_contract(codex_home: Path, manifest_path: Path, backup_root: Path = Path(r"D:\Backup")) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    backup_root = backup_root.resolve()
    try:
        manifest_path.relative_to(backup_root)
    except ValueError as error:
        raise RuntimeError("repair manifest is outside the required backup root") from error
    lock_path = active_repair_lock_path(manifest_path)
    if not lock_path.is_file():
        raise RuntimeError("active repair lock is missing")
    lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    validate_source_binding_contract(lock, manifest_path.parent.parent)
    manifest, manifest_sha256 = load_manifest_pair(
        manifest_path,
        str(lock.get("repair_manifest_sha256") or ""),
    )
    if manifest_path.parent.name != "repair_data":
        raise RuntimeError("repair manifest is not inside a repair_data transaction directory")
    if int(manifest.get("schema_version") or 0) != 1:
        raise RuntimeError("repair manifest schema is unsupported")
    if manifest.get("status") != "pending_restart_validation":
        raise RuntimeError(f"repair manifest is not pending restart validation: {manifest.get('status')}")
    if not str(manifest.get("runner_run_id") or ""):
        raise RuntimeError("repair manifest runner_run_id is missing")
    if windows_path_key(manifest.get("codex_home") or "") != windows_path_key(codex_home.resolve()):
        raise RuntimeError("repair manifest codex_home mismatch")
    run_root = manifest_path.parent.parent
    if windows_path_key(manifest.get("run_root") or "") != windows_path_key(run_root):
        raise RuntimeError("repair manifest run root mismatch")
    if str(lock.get("run_id") or "") != str(manifest.get("runner_run_id") or ""):
        raise RuntimeError("active repair lock run id mismatch")
    if str(lock.get("repair_manifest_sha256") or "").casefold() != manifest_sha256:
        raise RuntimeError("active repair lock manifest hash mismatch")
    plugin_snapshot_path = Path(str(manifest.get("plugin_snapshot_manifest") or "")).resolve()
    expected_plugin_snapshot_path = run_root / "plugin_state_snapshot" / "plugin_state_snapshot.json"
    if windows_path_key(plugin_snapshot_path) != windows_path_key(expected_plugin_snapshot_path):
        raise RuntimeError("plugin snapshot manifest is outside the repair run")
    plugin_snapshot = json.loads(plugin_snapshot_path.read_text(encoding="utf-8"))
    if plugin_snapshot.get("schema_version") != 1 or plugin_snapshot.get("status") != "complete":
        raise RuntimeError("plugin snapshot manifest is incomplete or unsupported")
    if windows_path_key(plugin_snapshot.get("codex_home") or "") != windows_path_key(codex_home.resolve()):
        raise RuntimeError("plugin snapshot codex_home mismatch")
    if (
        str(plugin_snapshot.get("runner_run_id") or "") != str(manifest.get("runner_run_id") or "")
        or windows_path_key(plugin_snapshot.get("run_root") or "") != windows_path_key(run_root)
        or windows_path_key(plugin_snapshot.get("snapshot_root") or "")
        != windows_path_key(run_root / "plugin_state_snapshot")
    ):
        raise RuntimeError("plugin snapshot run binding mismatch")
    if hashlib.sha256(plugin_snapshot_path.read_bytes()).hexdigest() != str(
        manifest.get("plugin_snapshot_manifest_sha256") or ""
    ):
        raise RuntimeError("plugin snapshot manifest hash mismatch")
    plugin_binding_path = run_root / "plugin_state_snapshot" / "plugin_state_snapshot.sha256.json"
    plugin_binding = json.loads(plugin_binding_path.read_text(encoding="utf-8-sig"))
    if (
        str(plugin_binding.get("runner_run_id") or "") != str(manifest.get("runner_run_id") or "")
        or windows_path_key(plugin_binding.get("run_root") or "") != windows_path_key(run_root)
        or windows_path_key(plugin_binding.get("codex_home") or "") != windows_path_key(codex_home.resolve())
        or str(plugin_binding.get("manifest_sha256") or "")
        != str(manifest.get("plugin_snapshot_manifest_sha256") or "")
    ):
        raise RuntimeError("plugin snapshot SHA-256 binding mismatch")
    return manifest


def validate_source_binding_contract(lock: dict[str, Any], run_root: Path) -> dict[str, Any]:
    binding_path = Path(str(lock.get("source_binding") or "")).resolve()
    expected_binding_path = run_root.resolve() / "SOURCE_BINDING.json"
    if windows_path_key(binding_path) != windows_path_key(expected_binding_path):
        raise RuntimeError("repair source binding is outside the active run")
    if not binding_path.is_file():
        raise RuntimeError("repair source binding is missing")
    binding_bytes = binding_path.read_bytes()
    binding_sha256 = hashlib.sha256(binding_bytes).hexdigest()
    if binding_sha256 != str(lock.get("source_binding_sha256") or "").casefold():
        raise RuntimeError("repair source binding hash mismatch")
    binding = json.loads(binding_bytes.decode("utf-8-sig"))
    if int(binding.get("schema_version") or 0) != 2:
        raise RuntimeError("repair source binding schema is unsupported")
    snapshot_root = Path(str(binding.get("snapshot_root") or "")).resolve()
    if windows_path_key(snapshot_root) != windows_path_key(run_root.resolve() / "source_snapshot"):
        raise RuntimeError("repair source snapshot root mismatch")
    if windows_path_key(lock.get("source_snapshot_root") or "") != windows_path_key(snapshot_root):
        raise RuntimeError("repair lock source snapshot root mismatch")
    files = binding.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("repair source binding has no files")
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("repair source binding file entry is malformed")
        expected_hash = str(item.get("sha256") or "").casefold()
        source_path = Path(str(item.get("path") or ""))
        snapshot_path = Path(str(item.get("snapshot_path") or "")).resolve()
        try:
            snapshot_path.relative_to(snapshot_root)
        except ValueError as error:
            raise RuntimeError("repair source snapshot file escapes the snapshot root") from error
        for label, path in (("source", source_path), ("snapshot", snapshot_path)):
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise RuntimeError(f"repair {label} binding hash mismatch: {path}")
    repositories = binding.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise RuntimeError("repair source binding has no repositories")
    for repository in repositories:
        path = Path(str(repository.get("path") or ""))
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=hidden_subprocess_flags(),
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=hidden_subprocess_flags(),
        ).stdout.strip()
        status_sha256 = hashlib.sha256(status.encode("utf-8")).hexdigest()
        if head != str(repository.get("head") or "") or status_sha256 != str(repository.get("status_sha256") or ""):
            raise RuntimeError(f"repair source repository changed after arming: {path}")
    return binding


def validate_restart_report_path(manifest_path: Path, report_path: Path) -> None:
    run_root = manifest_path.resolve().parent.parent
    try:
        report_path.resolve().relative_to(run_root)
    except ValueError as error:
        raise RuntimeError("restart report must stay within the same repair run") from error


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".writing")
    with temporary_path.open("w", encoding="utf-8", newline="") as destination:
        json.dump(payload, destination, ensure_ascii=False, indent=2)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary_path, path)


def active_repair_lock_path(manifest_path: Path) -> Path:
    return manifest_path.resolve().parent.parent.parent / "active_repair.lock.json"


def validate_repair_lock_file(
    lock_path: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    expected_statuses: set[str],
) -> dict[str, Any]:
    run_root = manifest_path.resolve().parent.parent
    lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    if windows_path_key(lock.get("run_root") or "") != windows_path_key(run_root):
        raise RuntimeError("repair lock belongs to another run root")
    if str(lock.get("run_id") or "") != str(manifest.get("runner_run_id") or ""):
        raise RuntimeError("repair lock belongs to another run id")
    if str(lock.get("status") or "") not in expected_statuses:
        raise RuntimeError(f"repair lock has an unexpected status: {lock.get('status')}")
    return lock


def update_active_repair_lock(
    manifest_path: Path,
    status: str,
    expected_statuses: set[str],
    additions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lock_path = active_repair_lock_path(manifest_path)
    manifest, manifest_sha256 = load_manifest_pair(manifest_path)
    lock = validate_repair_lock_file(lock_path, manifest_path, manifest, expected_statuses)
    lock["status"] = status
    lock["updated_at_epoch"] = int(time.time())
    lock["repair_manifest_sha256"] = manifest_sha256
    if additions:
        lock.update(additions)
    write_json_atomic(lock_path, lock)
    return lock


def validate_live_artifacts(
    run_root: Path,
    check_id: str,
    artifacts: Any,
) -> list[dict[str, Any]]:
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeError(f"live validation check has no bound artifacts: {check_id}")
    artifact_root = run_root / "live_validation_artifacts"
    validated: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise RuntimeError(f"live validation artifact is malformed: {check_id}")
        artifact_path = Path(str(artifact.get("path") or "")).resolve()
        try:
            artifact_path.relative_to(artifact_root.resolve())
        except ValueError as error:
            raise RuntimeError(f"live validation artifact is outside the run: {check_id}") from error
        if not artifact_path.is_file():
            raise RuntimeError(f"live validation artifact is missing: {artifact_path}")
        artifact_bytes = artifact_path.read_bytes()
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        if artifact_sha256 != str(artifact.get("sha256") or "").casefold():
            raise RuntimeError(f"live validation artifact hash mismatch: {artifact_path}")
        if len(artifact_bytes) != int(artifact.get("bytes") or -1):
            raise RuntimeError(f"live validation artifact size mismatch: {artifact_path}")
        media_type = str(artifact.get("media_type") or "")
        png_info = inspect_png_screenshot(artifact_bytes) if media_type == "image/png" else None
        validated.append(
            {
                "path": str(artifact_path),
                "sha256": artifact_sha256,
                "bytes": len(artifact_bytes),
                "media_type": media_type,
                "is_png": png_info is not None,
                "png": png_info,
            }
        )
    return validated


def validate_live_check_contract(
    check_id: str,
    check: dict[str, Any],
    run_root: Path,
    restart_generated_at_epoch: int,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    if check.get("status") != "pass" or not isinstance(check.get("evidence"), dict):
        raise RuntimeError(f"live validation check did not pass: {check_id}")
    evidence = check["evidence"]
    if evidence.get("method") != required_live_validation_methods[check_id]:
        raise RuntimeError(f"live validation method mismatch: {check_id}")
    started_at = int(evidence.get("started_at_epoch") or 0)
    completed_at = int(evidence.get("completed_at_epoch") or 0)
    if started_at < restart_generated_at_epoch or completed_at < started_at:
        raise RuntimeError(f"live validation timing predates restart verification: {check_id}")
    result = evidence.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"live validation result is not structured: {check_id}")
    artifacts = validate_live_artifacts(run_root, check_id, evidence.get("artifacts"))
    tool_call_pattern = re.compile(r"^[A-Za-z0-9._:-]{8,}$")

    if check_id in {"browser.live_tool", "chrome.live_tool", "computer_use.live_tool", "node_repl.live_tool"}:
        tool_call_id = str(result.get("tool_call_id") or "")
        if (
            not tool_call_pattern.fullmatch(tool_call_id)
            or result.get("tool_name") != "node_repl/js"
            or result.get("invocation_status") != "completed"
            or not str(result.get("observed_result") or "")
            or result.get("provenance_kind") != "functions_exec_nested_mcp"
            or not tool_call_pattern.fullmatch(str(result.get("nested_tool_call_id") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("probe_source_sha256") or ""))
        ):
            raise RuntimeError(f"direct live tool receipt is incomplete: {check_id}")
        if check_id == "browser.live_tool" and (
            result.get("browser_backend") != "iab" or not isinstance(result.get("tab_count"), int)
        ):
            raise RuntimeError("Browser live receipt did not confirm the in-app browser backend")
        if check_id == "chrome.live_tool" and (
            result.get("browser_backend") != "extension" or not isinstance(result.get("tab_count"), int)
        ):
            raise RuntimeError("Chrome live receipt did not confirm the extension backend")
        if check_id == "computer_use.live_tool":
            if (
                not isinstance(result.get("window_count"), int)
                or int(result["window_count"]) < 1
                or result.get("screenshot_verified") is not True
                or not any(artifact["media_type"] == "image/png" and artifact["is_png"] for artifact in artifacts)
            ):
                raise RuntimeError("Computer Use live receipt lacks a bound native window screenshot")
    elif check_id == "official_thread_tools.live_tool":
        source_thread_id = str(result.get("source_thread_id") or "")
        target_thread_id = str(result.get("target_thread_id") or "")
        if (
            not source_thread_id
            or not target_thread_id
            or source_thread_id == target_thread_id
            or not tool_call_pattern.fullmatch(str(result.get("send_tool_call_id") or ""))
            or not str(result.get("delegation_marker") or "")
            or result.get("message_visible_in_target") is not True
            or not str(result.get("target_response_marker") or "")
            or result.get("target_replied") is not True
            or result.get("tool_registry_source") != "state_5.thread_dynamic_tools"
            or not {"list_threads", "read_thread", "send_message_to_thread"}.issubset(
                set(map(str, result.get("source_official_tools") or []))
            )
            or not {"list_threads", "read_thread", "send_message_to_thread"}.issubset(
                set(map(str, result.get("target_official_tools") or []))
            )
        ):
            raise RuntimeError("official cross-thread delivery receipt is incomplete")
    elif check_id == "sidebar.thread_visibility":
        visible_thread_ids = result.get("visible_thread_ids")
        state_thread_ids = result.get("state_thread_ids")
        if (
            not isinstance(visible_thread_ids, list)
            or not visible_thread_ids
            or not isinstance(state_thread_ids, list)
            or not set(map(str, visible_thread_ids)).issubset(set(map(str, state_thread_ids)))
            or set(map(str, visible_thread_ids)) != set(map(str, (result.get("matched_thread_titles") or {}).keys()))
            or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("accessibility_sha256") or ""))
            or result.get("screenshot_verified") is not True
        ):
            raise RuntimeError("sidebar visual/state crosscheck receipt is incomplete")
        if not any(artifact["media_type"] == "image/png" and artifact["is_png"] for artifact in artifacts):
            raise RuntimeError("sidebar visual/state crosscheck has no bound PNG screenshot")
    elif check_id == "large_thread.ui_responsiveness":
        slim_thread_ids = {str(thread_id) for thread_id in manifest.get("prompt_preserving_slim_thread_ids") or []}
        if (
            not str(result.get("thread_id") or "")
            or (slim_thread_ids and str(result.get("thread_id")) not in slim_thread_ids)
            or not str(result.get("thread_title") or "")
            or result.get("thread_identity_provenance") != "state_5.sqlite+target_rollout_user_prompt"
            or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("input_submission_marker_sha256") or ""))
            or int(result.get("submitted_prompt_line") or 0) < 1
            or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("prompt_composer_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("accessibility_before_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("accessibility_after_sha256") or ""))
            or not isinstance(result.get("collector_measured_elapsed_ms"), (int, float))
            or float(result["collector_measured_elapsed_ms"]) < 0
            or float(result["collector_measured_elapsed_ms"]) > 10_000
            or result.get("operations_verified") != ["open", "scroll", "input", "submit", "screenshot"]
            or result.get("screenshot_verified") is not True
        ):
            raise RuntimeError("large-thread timed UI receipt is incomplete")
        if not any(artifact["media_type"] == "image/png" and artifact["is_png"] for artifact in artifacts):
            raise RuntimeError("large-thread timed UI receipt has no bound PNG screenshot")
    return artifacts


def _event_epoch(item: dict[str, Any]) -> int:
    timestamp = str(item.get("timestamp") or "")
    if not timestamp:
        return 0
    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC).timestamp())
    except ValueError:
        return 0


def _registered_rollout_paths(codex_home: Path) -> dict[str, Path]:
    database_path = codex_home / "state_5.sqlite"
    database = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        return {
            str(thread_id): Path(str(rollout_path)).resolve()
            for thread_id, rollout_path in database.execute(
                "select id, rollout_path from threads where rollout_path is not null"
            )
        }
    finally:
        database.close()


def _registered_official_thread_tools(codex_home: Path, thread_id: str) -> set[str]:
    database_path = codex_home / "state_5.sqlite"
    database = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        table_exists = database.execute(
            "select 1 from sqlite_master where type='table' and name='thread_dynamic_tools'"
        ).fetchone()
        if table_exists is None:
            raise RuntimeError("thread_dynamic_tools table is missing")
        return {
            str(name)
            for namespace, name in database.execute(
                "select namespace, name from thread_dynamic_tools where thread_id = ?",
                (thread_id,),
            )
            if str(namespace or "") == "codex_app" and str(name or "")
        }
    finally:
        database.close()


def _read_rollout_evidence(
    requested_lines: dict[Path, set[int]],
) -> tuple[dict[tuple[Path, int], dict[str, Any]], dict[tuple[Path, int], str]]:
    records: dict[tuple[Path, int], dict[str, Any]] = {}
    prefix_hashes: dict[tuple[Path, int], str] = {}
    for path, line_numbers in requested_lines.items():
        if not path.is_file() or not line_numbers or min(line_numbers) < 1:
            raise RuntimeError(f"live validation rollout evidence is missing: {path}")
        digest = hashlib.sha256()
        remaining = set(line_numbers)
        with path.open("rb") as source:
            for line_number, raw_line in enumerate(source, 1):
                digest.update(raw_line)
                if line_number in remaining:
                    try:
                        item = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise RuntimeError(f"live validation rollout line is invalid JSON: {path}:{line_number}") from error
                    if not isinstance(item, dict):
                        raise RuntimeError(f"live validation rollout line is not an object: {path}:{line_number}")
                    records[(path, line_number)] = item
                    prefix_hashes[(path, line_number)] = digest.hexdigest()
                    remaining.remove(line_number)
                    if not remaining:
                        break
        if remaining:
            raise RuntimeError(f"live validation rollout lines are missing from {path}: {sorted(remaining)}")
    return records, prefix_hashes


def validate_live_event_provenance(
    codex_home: Path,
    checks: dict[str, dict[str, Any]],
    challenge: str,
    restart_generated_at_epoch: int,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", challenge):
        raise RuntimeError("live validation challenge is missing or invalid")
    registered_paths = _registered_rollout_paths(codex_home)
    requested_lines: dict[Path, set[int]] = {}
    claims: dict[str, dict[str, Any]] = {}
    for check_id in required_live_validation_checks:
        check = checks.get(check_id)
        evidence = check.get("evidence") if isinstance(check, dict) else None
        result = evidence.get("result") if isinstance(evidence, dict) else None
        if not isinstance(result, dict):
            raise RuntimeError(f"live validation result has no event provenance: {check_id}")
        source_thread_id = str(result.get("source_thread_id") or "")
        source_path = Path(str(result.get("source_rollout_path") or "")).resolve()
        if source_thread_id not in registered_paths or windows_path_key(registered_paths[source_thread_id]) != windows_path_key(source_path):
            raise RuntimeError(f"live validation source rollout is not SQLite-bound: {check_id}")
        call_line = int(result.get("call_line") or 0)
        output_line = int(result.get("output_line") or 0)
        if call_line < 1 or output_line <= call_line:
            raise RuntimeError(f"live validation call/output line contract is invalid: {check_id}")
        requested_lines.setdefault(source_path, set()).update({call_line, output_line})
        claim = {
            "result": result,
            "source_path": source_path,
            "call_line": call_line,
            "output_line": output_line,
        }
        if check_id != "official_thread_tools.live_tool":
            nested_event_line = int(result.get("nested_event_line") or 0)
            if nested_event_line <= call_line or nested_event_line >= output_line:
                raise RuntimeError(f"live validation nested MCP event line is invalid: {check_id}")
            requested_lines.setdefault(source_path, set()).add(nested_event_line)
            claim["nested_event_line"] = nested_event_line
        if check_id == "official_thread_tools.live_tool":
            target_thread_id = str(result.get("target_thread_id") or "")
            target_path = Path(str(result.get("target_rollout_path") or "")).resolve()
            target_line = int(result.get("target_message_line") or 0)
            target_response_line = int(result.get("target_response_line") or 0)
            if target_thread_id not in registered_paths or windows_path_key(registered_paths[target_thread_id]) != windows_path_key(target_path):
                raise RuntimeError("official target rollout is not SQLite-bound")
            if target_line < 1 or target_response_line <= target_line:
                raise RuntimeError("official target message line is invalid")
            requested_lines.setdefault(target_path, set()).update({target_line, target_response_line})
            claim.update(
                {
                    "target_path": target_path,
                    "target_line": target_line,
                    "target_response_line": target_response_line,
                }
            )
        if check_id == "large_thread.ui_responsiveness":
            target_thread_id = str(result.get("thread_id") or "")
            target_path = Path(str(result.get("target_rollout_path") or "")).resolve()
            target_line = int(result.get("submitted_prompt_line") or 0)
            if (
                target_thread_id not in registered_paths
                or windows_path_key(registered_paths[target_thread_id]) != windows_path_key(target_path)
                or target_line < 1
            ):
                registered_target = registered_paths.get(target_thread_id)
                raise RuntimeError(
                    "large-thread submitted prompt rollout is not SQLite-bound: "
                    f"thread_id={target_thread_id!r}, registered={registered_target}, "
                    f"claimed={target_path}, submitted_prompt_line={target_line}"
                )
            requested_lines.setdefault(target_path, set()).add(target_line)
            claim.update({"target_path": target_path, "target_line": target_line})
        claims[check_id] = claim

    records, prefix_hashes = _read_rollout_evidence(requested_lines)
    receipts: dict[str, Any] = {}
    for check_id, claim in claims.items():
        result = claim["result"]
        source_path = claim["source_path"]
        call_line = claim["call_line"]
        output_line = claim["output_line"]
        if prefix_hashes[(source_path, output_line)] != str(result.get("source_rollout_prefix_sha256") or ""):
            raise RuntimeError(f"live validation source rollout prefix hash mismatch: {check_id}")
        call_item = records[(source_path, call_line)]
        output_item = records[(source_path, output_line)]
        call_payload = call_item.get("payload") if isinstance(call_item.get("payload"), dict) else {}
        output_payload = output_item.get("payload") if isinstance(output_item.get("payload"), dict) else {}
        call_id = str(call_payload.get("call_id") or "")
        name = str(call_payload.get("name") or "")
        wrapper_arguments = exact_call_arguments(call_payload, challenge, check_id)
        if (
            call_item.get("type") != "response_item"
            or call_payload.get("type") not in {"function_call", "custom_tool_call"}
            or output_item.get("type") != "response_item"
            or output_payload.get("type") not in {"function_call_output", "custom_tool_call_output"}
            or not call_id
            or str(output_payload.get("call_id") or "") != call_id
            or str(result.get("tool_call_id") or result.get("send_tool_call_id") or "") != call_id
            or name.casefold() != "exec"
            or _event_epoch(call_item) < restart_generated_at_epoch
            or _event_epoch(output_item) < restart_generated_at_epoch
        ):
            raise RuntimeError(f"live validation call/output receipt does not match the real rollout: {check_id}")
        if check_id == "official_thread_tools.live_tool":
            call_arguments = wrapper_arguments
            direct_output_result = exact_challenge_envelope(output_payload, challenge, check_id)
            probe_source = str(call_arguments.get("wrapper_code") or "")
        else:
            nested_event_line = int(claim["nested_event_line"])
            if prefix_hashes[(source_path, nested_event_line)] != str(result.get("nested_event_prefix_sha256") or ""):
                raise RuntimeError(f"nested MCP event prefix hash mismatch: {check_id}")
            nested_item = records[(source_path, nested_event_line)]
            nested_payload = nested_item.get("payload") if isinstance(nested_item.get("payload"), dict) else {}
            call_arguments, direct_output_result = exact_nested_mcp_event(nested_payload, challenge, check_id)
            if (
                nested_item.get("type") != "event_msg"
                or _event_epoch(nested_item) < restart_generated_at_epoch
                or str(nested_payload.get("call_id") or "") != str(result.get("nested_tool_call_id") or "")
                or result.get("provenance_kind") != "functions_exec_nested_mcp"
            ):
                raise RuntimeError(f"nested MCP event does not match the real rollout: {check_id}")
            probe_source = str(call_arguments.get("code") or "")
        probe_source_sha256 = hashlib.sha256(probe_source.encode("utf-8")).hexdigest()
        if probe_source_sha256 != str(result.get("probe_source_sha256") or ""):
            raise RuntimeError(f"live validation probe source hash mismatch: {check_id}")
        if check_id in {"browser.live_tool", "chrome.live_tool"}:
            validate_browser_probe_arguments(call_arguments, check_id)
            expected_backend = "iab" if check_id == "browser.live_tool" else "extension"
            if direct_output_result.get("browser_backend") != expected_backend:
                raise RuntimeError(f"{check_id} output did not confirm the expected browser backend")
        if check_id in {"computer_use.live_tool", "sidebar.thread_visibility", "large_thread.ui_responsiveness"}:
            validate_ui_probe_arguments(call_arguments, check_id)
            if not str(direct_output_result.get("screenshot_data_url") or "").startswith("data:image/png;base64,"):
                raise RuntimeError(f"Windows UI output is not bound to a real screenshot: {check_id}")
            if check_id == "computer_use.live_tool" and (
                not isinstance(direct_output_result.get("window_count"), int)
                or int(direct_output_result["window_count"]) < 1
                or not str(direct_output_result.get("window_title") or "").strip()
            ):
                raise RuntimeError("Computer Use output is not bound to a real native window")
            if check_id == "sidebar.thread_visibility":
                accessibility_text = str(direct_output_result.get("accessibility_text") or "")
                matched_titles = result.get("matched_thread_titles")
                if not isinstance(matched_titles, dict) or any(
                    str(title).casefold() not in accessibility_text.casefold()
                    for title in matched_titles.values()
                ):
                    raise RuntimeError("sidebar thread titles are not present in native accessibility evidence")
                if hashlib.sha256(accessibility_text.encode("utf-8")).hexdigest() != str(
                    result.get("accessibility_sha256") or ""
                ):
                    raise RuntimeError("sidebar accessibility evidence hash mismatch")
            if check_id == "large_thread.ui_responsiveness":
                expected_title = str(result.get("thread_title") or "")
                before_text = str(direct_output_result.get("accessibility_before") or "")
                after_text = str(direct_output_result.get("accessibility_after") or "")
                prompt_composer_line = str(direct_output_result.get("prompt_composer_line") or "")
                input_marker = str(direct_output_result.get("input_submission_marker") or "")
                if (
                    not expected_title
                    or expected_title.casefold() not in before_text.casefold()
                    or expected_title.casefold() not in after_text.casefold()
                    or hashlib.sha256(before_text.encode("utf-8")).hexdigest()
                    != str(result.get("accessibility_before_sha256") or "")
                    or hashlib.sha256(after_text.encode("utf-8")).hexdigest()
                    != str(result.get("accessibility_after_sha256") or "")
                    or not prompt_composer_line
                    or re.search(r"search|filter|find|搜索|筛选|查找", prompt_composer_line, re.IGNORECASE)
                    or hashlib.sha256(prompt_composer_line.encode("utf-8")).hexdigest()
                    != str(result.get("prompt_composer_sha256") or "")
                    or not input_marker.startswith("codex-live-input-")
                    or hashlib.sha256(input_marker.encode("utf-8")).hexdigest()
                    != str(result.get("input_submission_marker_sha256") or "")
                ):
                    raise RuntimeError("large-thread accessibility evidence is not bound to its SQLite title")
                target_item = records[(claim["target_path"], claim["target_line"])]
                target_payload = target_item.get("payload") if isinstance(target_item.get("payload"), dict) else {}
                if (
                    _event_epoch(target_item) < restart_generated_at_epoch
                    or target_item.get("type") != "response_item"
                    or target_payload.get("role") != "user"
                    or input_marker not in json.dumps(target_item, ensure_ascii=False)
                ):
                    raise RuntimeError("large-thread prompt marker is not present in the SQLite-bound target rollout")
        if check_id == "official_thread_tools.live_tool":
            target_thread_id = str(result.get("target_thread_id") or "")
            if str(direct_output_result.get("target_thread_id") or direct_output_result.get("send_thread_id") or "") != target_thread_id:
                raise RuntimeError("official send output is not bound to the requested target thread")
            required_official_tools = {"list_threads", "read_thread", "send_message_to_thread"}
            source_tools = _registered_official_thread_tools(codex_home, str(result.get("source_thread_id") or ""))
            target_tools = _registered_official_thread_tools(codex_home, target_thread_id)
            if not required_official_tools.issubset(source_tools) or not required_official_tools.issubset(target_tools):
                raise RuntimeError("official thread tools are not registered on both source and target threads")
            if source_tools != set(map(str, result.get("source_official_tools") or [])):
                raise RuntimeError("source official tool registry receipt drifted")
            if target_tools != set(map(str, result.get("target_official_tools") or [])):
                raise RuntimeError("target official tool registry receipt drifted")
            target_path = claim["target_path"]
            target_line = claim["target_line"]
            target_response_line = claim["target_response_line"]
            if prefix_hashes[(target_path, target_line)] != str(result.get("target_rollout_prefix_sha256") or ""):
                raise RuntimeError("official target rollout prefix hash mismatch")
            if prefix_hashes[(target_path, target_response_line)] != str(
                result.get("target_response_prefix_sha256") or ""
            ):
                raise RuntimeError("official target response prefix hash mismatch")
            target_item = records[(target_path, target_line)]
            target_text = json.dumps(target_item, ensure_ascii=False)
            if (
                _event_epoch(target_item) < restart_generated_at_epoch
                or str(result.get("delegation_marker") or "") not in target_text
                or str(result.get("source_thread_id") or "") not in target_text
                or "codex_delegation" not in target_text
            ):
                raise RuntimeError("official delegation is not visible in the target rollout")
            target_response_item = records[(target_path, target_response_line)]
            target_response_payload = (
                target_response_item.get("payload")
                if isinstance(target_response_item.get("payload"), dict)
                else {}
            )
            if (
                _event_epoch(target_response_item) < restart_generated_at_epoch
                or target_response_item.get("type") != "response_item"
                or target_response_payload.get("type") != "message"
                or target_response_payload.get("role") != "assistant"
                or str(result.get("target_response_marker") or "")
                not in json.dumps(target_response_item, ensure_ascii=False)
            ):
                raise RuntimeError("official target thread did not produce the required assistant reply")
        receipts[check_id] = {
            "source_thread_id": result["source_thread_id"],
            "source_rollout_path": str(source_path),
            "call_line": call_line,
            "output_line": output_line,
            "call_id": call_id,
            "tool_name": name,
            "nested_event_line": claim.get("nested_event_line"),
            "nested_tool_call_id": result.get("nested_tool_call_id"),
            "probe_source_sha256": probe_source_sha256,
        }
    return receipts


def complete_live_validation(
    codex_home: Path,
    manifest_path: Path,
    evidence_path: Path,
    backup_root: Path = Path(r"D:\Backup"),
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    backup_root = backup_root.resolve()
    try:
        manifest_path.relative_to(backup_root)
    except ValueError as error:
        raise RuntimeError("repair manifest is outside the required backup root") from error
    if manifest_path.parent.name != "repair_data":
        raise RuntimeError("repair manifest is not inside a repair_data transaction directory")
    run_root = manifest_path.parent.parent
    try:
        evidence_path = evidence_path.resolve()
        evidence_path.relative_to(run_root)
    except ValueError as error:
        raise RuntimeError("live validation evidence must stay within the same repair run") from error
    manifest, _ = load_manifest_pair(manifest_path)
    manifest_status = manifest.get("status")
    if manifest.get("schema_version") != 1 or manifest_status not in {"pending_live_ui_validation", "complete"}:
        raise RuntimeError(f"repair manifest is not pending live UI validation: {manifest.get('status')}")
    if windows_path_key(manifest.get("codex_home") or "") != windows_path_key(codex_home.resolve()):
        raise RuntimeError("repair manifest codex_home mismatch")
    active_lock_path = active_repair_lock_path(manifest_path)
    completed_lock_path = run_root / "runner_lock_completed.json"
    evidence_lock_path = active_lock_path if active_lock_path.is_file() else completed_lock_path
    if not evidence_lock_path.is_file():
        raise RuntimeError("repair lock is missing before live validation")
    evidence_lock = validate_repair_lock_file(
        evidence_lock_path,
        manifest_path,
        manifest,
        {"pending_live_ui_validation", "completing_live_validation", "complete"},
    )
    validate_source_binding_contract(evidence_lock, run_root)
    restart_report_path = Path(str(manifest.get("restart_validation_report") or ""))
    try:
        restart_report_path = restart_report_path.resolve()
        restart_report_path.relative_to(run_root)
    except ValueError as error:
        raise RuntimeError("automated restart validation report is outside the repair run") from error
    if not restart_report_path.is_file():
        raise RuntimeError("automated restart validation report is missing")
    restart_report_bytes = restart_report_path.read_bytes()
    restart_report_sha256 = hashlib.sha256(restart_report_bytes).hexdigest()
    if restart_report_sha256 != str(manifest.get("restart_validation_report_sha256") or ""):
        raise RuntimeError("automated restart validation report hash mismatch")
    restart_report = json.loads(restart_report_bytes.decode("utf-8-sig"))
    run_id = str(manifest.get("runner_run_id") or "")
    if (
        restart_report.get("schema_version") != 1
        or restart_report.get("status") != "pending_live_ui_validation"
        or str(restart_report.get("run_id") or "") != run_id
        or windows_path_key(restart_report.get("codex_home") or "") != windows_path_key(codex_home.resolve())
    ):
        raise RuntimeError("automated restart validation report contract mismatch")
    request_path = Path(str(manifest.get("live_validation_request") or ""))
    try:
        request_path = request_path.resolve()
        request_path.relative_to(run_root)
    except ValueError as error:
        raise RuntimeError("live validation request is outside the repair run") from error
    if not request_path.is_file():
        raise RuntimeError("live validation request is missing")
    request_sha256 = hashlib.sha256(request_path.read_bytes()).hexdigest()
    if request_sha256 != str(manifest.get("live_validation_request_sha256") or ""):
        raise RuntimeError("live validation request hash mismatch")
    evidence_bytes = evidence_path.read_bytes()
    evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    evidence = json.loads(evidence_bytes.decode("utf-8-sig"))
    validate_live_collector_contract(evidence, run_root)
    checks = {
        str(item.get("id") or ""): item
        for item in evidence.get("checks", [])
        if isinstance(item, dict)
    }
    restart_generated_at_epoch = int(restart_report.get("generated_at_epoch") or 0)
    evidence_generated_at_epoch = int(evidence.get("generated_at_epoch") or 0)
    failures: list[str] = []
    artifact_receipts: dict[str, list[dict[str, Any]]] = {}
    event_receipts: dict[str, Any] = {}
    try:
        event_receipts = validate_live_event_provenance(
            codex_home,
            checks,
            str(manifest.get("live_validation_challenge") or ""),
            restart_generated_at_epoch,
        )
    except Exception as error:
        failures.append(f"event_provenance: {error}")
    for check_id in required_live_validation_checks:
        try:
            if check_id not in checks:
                raise RuntimeError("check is missing")
            artifact_receipts[check_id] = validate_live_check_contract(
                check_id,
                checks[check_id],
                run_root,
                restart_generated_at_epoch,
                manifest,
            )
        except Exception as error:
            failures.append(f"{check_id}: {error}")
    if (
        evidence.get("schema_version") != 2
        or evidence.get("status") != "pass"
        or restart_generated_at_epoch <= 0
        or evidence_generated_at_epoch < restart_generated_at_epoch
        or str(evidence.get("run_id") or "") != run_id
        or windows_path_key(evidence.get("codex_home") or "") != windows_path_key(codex_home.resolve())
        or str(evidence.get("restart_validation_report_sha256") or "") != restart_report_sha256
        or windows_path_key(evidence.get("live_validation_request") or "") != windows_path_key(request_path)
        or str(evidence.get("live_validation_request_sha256") or "") != request_sha256
        or failures
    ):
        raise RuntimeError(f"live validation evidence is incomplete: {failures}")

    notify_validation = restart_report.get("notify_os206_validation")
    if not isinstance(notify_validation, dict) or int(notify_validation.get("schema_version") or 0) != 2:
        raise RuntimeError("automated restart validation report has no notify os error 206 boundary")
    live_probe_baseline = notify_validation.get("live_probe_baseline")
    if not isinstance(live_probe_baseline, dict):
        raise RuntimeError("automated restart validation report has no live-probe log baseline")
    if evidence_generated_at_epoch < int(live_probe_baseline.get("captured_at_epoch") or 0):
        raise RuntimeError("live validation evidence predates its notify log baseline")
    notify_live_recheck = validate_live_notify_log_window(
        codex_home / "logs_2.sqlite",
        live_probe_baseline,
        min_epoch=restart_generated_at_epoch,
    )

    lock_path = active_lock_path
    if manifest_status == "pending_live_ui_validation":
        update_active_repair_lock(
            manifest_path,
            "completing_live_validation",
            {"pending_live_ui_validation"},
        )
        manifest["status"] = "complete"
        manifest["live_validation_completed_at_epoch"] = int(time.time())
        manifest["live_validation_evidence"] = str(evidence_path)
        manifest["live_validation_evidence_sha256"] = evidence_sha256
        manifest["live_validation_artifacts"] = artifact_receipts
        manifest["live_validation_event_receipts"] = event_receipts
        manifest["notify_os206_live_recheck"] = notify_live_recheck
        write_manifest_pair(manifest_path, manifest)
    elif (
        windows_path_key(manifest.get("live_validation_evidence") or "") != windows_path_key(evidence_path)
        or str(manifest.get("live_validation_evidence_sha256") or "") != evidence_sha256
    ):
        raise RuntimeError("completed repair manifest references different live validation evidence")
    if lock_path.is_file():
        update_active_repair_lock(
            manifest_path,
            "complete",
            {"completing_live_validation", "complete"},
            additions={
                "live_validation_evidence": str(evidence_path),
                "live_validation_evidence_sha256": evidence_sha256,
            },
        )
        manifest, _ = load_manifest_pair(manifest_path)
        validate_repair_lock_file(
            lock_path,
            manifest_path,
            manifest,
            {"complete"},
        )
        os.replace(lock_path, completed_lock_path)
    elif completed_lock_path.is_file():
        validate_repair_lock_file(
            completed_lock_path,
            manifest_path,
            manifest,
            {"complete"},
        )
    else:
        raise RuntimeError("neither active nor completed repair lock exists")
    return {
        "status": "complete",
        "manifest": str(manifest_path),
        "evidence": str(evidence_path),
        "completed_lock": str(completed_lock_path),
    }


def validate_live_collector_contract(evidence: dict[str, Any], run_root: Path) -> None:
    collector = evidence.get("collector")
    if not isinstance(collector, dict) or collector.get("name") != "collect_codex_live_validation" or collector.get("version") != 1:
        raise RuntimeError("live validation evidence was not produced by the bound collector")
    binding_path = run_root / "SOURCE_BINDING.json"
    binding = json.loads(binding_path.read_text(encoding="utf-8-sig"))
    matching = [
        item
        for item in binding.get("files", [])
        if isinstance(item, dict)
        and str(item.get("relative_path") or "").replace("\\", "/").casefold()
        == "scripts/collect_codex_live_validation.py"
    ]
    if len(matching) != 1:
        raise RuntimeError("repair source binding has no unique live validation collector")
    expected = matching[0]
    collector_path = Path(str(collector.get("path") or "")).resolve()
    if (
        windows_path_key(collector_path) != windows_path_key(expected.get("snapshot_path") or "")
        or str(collector.get("sha256") or "").casefold() != str(expected.get("sha256") or "").casefold()
        or not collector_path.is_file()
        or hashlib.sha256(collector_path.read_bytes()).hexdigest() != str(expected.get("sha256") or "").casefold()
    ):
        raise RuntimeError("live validation collector does not match the immutable source snapshot")


def read_config(codex_home: Path) -> dict[str, Any]:
    with (codex_home / "config.toml").open("rb") as source:
        runtime_config = tomllib.load(source)
    managed_config_path = codex_home / "managed_config.toml"
    if not managed_config_path.is_file():
        return runtime_config
    with managed_config_path.open("rb") as source:
        managed_config = tomllib.load(source)
    if "node_repl" in (managed_config.get("mcp_servers") or {}):
        raise RuntimeError(
            "managed_config.toml must not define mcp_servers.node_repl; it shadows the Desktop privileged runtime"
        )
    return runtime_config


def resolve_codex_cli(config: dict[str, Any]) -> Path:
    candidates = [config.get("CODEX_CLI_PATH")]
    node_repl = (config.get("mcp_servers") or {}).get("node_repl") or {}
    candidates.append((node_repl.get("env") or {}).get("CODEX_CLI_PATH"))
    for candidate in candidates:
        if candidate and Path(str(candidate)).is_file():
            return Path(str(candidate))
    raise RuntimeError("configured codex.exe could not be resolved")


def run_json_command(command: list[str], environment: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=120,
        creationflags=hidden_subprocess_flags(),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stderr or completed.stdout}"
        )
    return json.loads(completed.stdout)


def validate_plugin_registry(codex_cli: Path, codex_home: Path) -> list[dict[str, Any]]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    registry = run_json_command(
        [str(codex_cli), "plugin", "list", "--marketplace", "openai-bundled", "--available", "--json"],
        environment,
    )
    installed = {
        str(item.get("pluginId") or "").casefold(): item
        for item in registry.get("installed", [])
        if isinstance(item, dict)
    }
    validated: list[dict[str, Any]] = []
    marketplace_root = codex_home / ".tmp" / "bundled-marketplaces" / "openai-bundled"
    for plugin_name in required_plugin_names:
        selector = f"{plugin_name}@openai-bundled"
        item = installed.get(selector.casefold())
        if not item or not item.get("installed") or not item.get("enabled"):
            raise RuntimeError(f"bundled plugin is not installed and enabled: {selector}")
        manifest_path = marketplace_root / "plugins" / plugin_name / ".codex-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        expected_version = str(manifest.get("version") or "")
        if str(item.get("version") or "") != expected_version:
            raise RuntimeError(f"bundled plugin version mismatch: {selector}")
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        marketplace_source = (
            item.get("marketplaceSource") if isinstance(item.get("marketplaceSource"), dict) else {}
        )
        expected_plugin_source = marketplace_root / "plugins" / plugin_name
        if str(source.get("source") or "") != "local":
            raise RuntimeError(f"bundled plugin source is not local: {selector}")
        if str(marketplace_source.get("sourceType") or "") != "local":
            raise RuntimeError(f"bundled plugin marketplace source is not local: {selector}")
        if windows_path_key(str(source.get("path") or "")) != windows_path_key(expected_plugin_source):
            raise RuntimeError(f"bundled plugin source path mismatch: {selector}")
        if windows_path_key(str(marketplace_source.get("source") or "")) != windows_path_key(marketplace_root):
            raise RuntimeError(f"bundled plugin marketplace path mismatch: {selector}")
        validated.append(
            {
                "plugin_id": selector,
                "version": expected_version,
                "enabled": True,
                "source": source,
                "marketplace_source": marketplace_source,
            }
        )
    return validated


def validate_windows_incompatible_plugin_registry(
    codex_cli: Path,
    codex_home: Path,
) -> list[dict[str, Any]]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    registry = run_json_command(
        [str(codex_cli), "plugin", "list", "--marketplace", "openai-curated", "--available", "--json"],
        environment,
    )
    entries = [
        *[item for item in registry.get("installed", []) if isinstance(item, dict)],
        *[item for item in registry.get("available", []) if isinstance(item, dict)],
    ]
    entries_by_id = {str(item.get("pluginId") or "").casefold(): item for item in entries}
    validated: list[dict[str, Any]] = []
    for selector in windows_incompatible_plugin_selectors:
        item = entries_by_id.get(selector.casefold())
        if item is None:
            raise RuntimeError(f"curated plugin registry entry is missing: {selector}")
        if item.get("installed") or item.get("enabled"):
            raise RuntimeError(f"Windows-incompatible plugin remains installed or enabled: {selector}")
        validated.append(
            {
                "plugin_id": selector,
                "installed": False,
                "enabled": False,
                "version": str(item.get("version") or ""),
            }
        )
    return validated


def validate_node_runtime(config: dict[str, Any]) -> dict[str, Any]:
    node_repl = (config.get("mcp_servers") or {}).get("node_repl") or {}
    arguments = list(node_repl.get("args") or [])
    if "--disable-sandbox" in arguments:
        raise RuntimeError("node_repl still uses the stale --disable-sandbox override")
    environment_config = node_repl.get("env") or {}
    node_path = Path(str(environment_config.get("NODE_REPL_NODE_PATH") or ""))
    module_root_text = str(environment_config.get("NODE_REPL_NODE_MODULE_DIRS") or "")
    module_roots = [
        Path(value.strip().strip("'\""))
        for value in module_root_text.split(os.pathsep)
        if value.strip().strip("'\"")
    ]
    if not node_path.is_file() or not module_roots or any(not root.is_dir() for root in module_roots):
        raise RuntimeError("configured Node runtime or module root is missing")
    module_root = module_roots[0]
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
    if not computer_use_client.is_file():
        raise RuntimeError("the first Desktop node module root is missing the Computer Use Sky client")
    native_pipe_enabled = str(environment_config.get("SKY_CUA_NATIVE_PIPE") or "") == "1"
    native_pipe_directory = str(environment_config.get("SKY_CUA_NATIVE_PIPE_DIRECTORY") or "")
    if not native_pipe_enabled or not native_pipe_directory:
        raise RuntimeError("Desktop Node REPL native-pipe privileges are missing")
    environment = os.environ.copy()
    environment["NODE_PATH"] = os.pathsep.join(str(root) for root in module_roots)
    completed = subprocess.run(
        [
            str(node_path),
            "-e",
            "for (const name of ['playwright','playwright-core']) require.resolve(name); console.log('node-runtime-ok')",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=60,
        creationflags=hidden_subprocess_flags(),
    )
    if completed.returncode != 0 or "node-runtime-ok" not in completed.stdout:
        raise RuntimeError(f"Node runtime smoke failed: {completed.stderr or completed.stdout}")
    return {
        "node_path": str(node_path),
        "module_roots": [str(root) for root in module_roots],
        "computer_use_client": str(computer_use_client),
        "arguments": arguments,
        "native_pipe": native_pipe_directory,
        "smoke": "node-runtime-ok",
    }


def update_manifest(manifest_path: Path, report_path: Path) -> str:
    manifest, _ = load_manifest_pair(manifest_path)
    if manifest.get("status") != "pending_restart_validation":
        raise RuntimeError(f"repair manifest is not pending restart validation: {manifest.get('status')}")
    manifest["status"] = "pending_live_ui_validation"
    manifest["automated_restart_validation_at_epoch"] = int(time.time())
    manifest["restart_validation_report"] = str(report_path)
    manifest["restart_validation_report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    return write_manifest_pair(manifest_path, manifest)


def validate_post_restart_audit_summary(summary: dict[str, Any]) -> None:
    if int(summary.get("missing_rollout_count") or 0):
        raise RuntimeError(f"post-restart audit has missing rollouts: {summary['missing_rollout_count']}")
    if int(summary.get("estimated_current_parser_errors") or 0):
        raise RuntimeError(
            f"post-restart audit still has parser incompatibilities: {summary['estimated_current_parser_errors']}"
        )
    if int(summary.get("json_parse_error_count") or 0):
        raise RuntimeError(f"post-restart audit has JSON parse errors: {summary['json_parse_error_count']}")
    if int(summary.get("shared_rollout_mapping_count") or 0):
        raise RuntimeError(
            f"post-restart audit has shared rollout mappings: {summary['shared_rollout_mapping_count']}"
        )
    remaining_repair_count = sum(
        int(summary.get(key) or 0)
        for key in (
            "active_compatibility_repair_candidate_count",
            "archived_compatibility_repair_candidate_count",
            "active_hard_blocked_count",
            "archived_hard_blocked_count",
        )
    )
    if remaining_repair_count:
        raise RuntimeError(
            f"post-restart audit still has compatibility candidates or hard-blocked threads: {remaining_repair_count}"
        )


def load_prompt_baseline_audit(manifest_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    run_root = manifest_path.resolve().parent.parent
    audit_path = Path(str(manifest.get("audit_path") or "")).resolve()
    expected_audit_path = run_root / "offline_thread_audit.json"
    if windows_path_key(audit_path) != windows_path_key(expected_audit_path):
        raise RuntimeError("prompt baseline audit is outside the repair run")
    audit_bytes = audit_path.read_bytes()
    if hashlib.sha256(audit_bytes).hexdigest() != str(manifest.get("audit_sha256") or "").casefold():
        raise RuntimeError("prompt baseline audit hash mismatch")
    audit = json.loads(audit_bytes.decode("utf-8-sig"))
    policy = audit.get("policy") if isinstance(audit.get("policy"), dict) else {}
    if int(audit.get("schema_version") or 0) != 5 or int(policy.get("version") or 0) != 2:
        raise RuntimeError("prompt baseline audit schema or policy is unsupported")
    if manifest.get("prompt_preservation_required") is not True:
        raise RuntimeError("repair manifest does not require prompt preservation")
    raw_slim_thread_ids = manifest.get("prompt_preserving_slim_thread_ids") or []
    if not isinstance(raw_slim_thread_ids, list) or any(
        not isinstance(thread_id, str) or not thread_id.strip() for thread_id in raw_slim_thread_ids
    ):
        raise RuntimeError("repair manifest has an invalid targeted slim thread list")
    slim_thread_ids = [thread_id.strip() for thread_id in raw_slim_thread_ids]
    if len(set(slim_thread_ids)) != len(slim_thread_ids):
        raise RuntimeError("repair manifest has duplicate targeted slim thread ids")
    if bool(manifest.get("checkpoint_history_reduction_enabled")) != bool(slim_thread_ids):
        raise RuntimeError("repair manifest checkpoint reduction flag does not match its explicit target list")
    if slim_thread_ids:
        repairs_by_thread = {
            str(item.get("thread_id") or ""): item
            for item in manifest.get("thread_repairs") or []
            if item.get("strategy") == "prompt_preserving_checkpoint_slim_view"
        }
        for thread_id in slim_thread_ids:
            repair = repairs_by_thread.get(thread_id)
            if repair is None:
                raise RuntimeError(f"targeted slim repair is absent from the manifest: {thread_id}")
            if int(repair.get("active_bytes") or 0) >= int(repair.get("original_bytes") or 0):
                raise RuntimeError(f"targeted slim repair did not reduce the active rollout: {thread_id}")
    return audit


def validate_restart_slim_contract(
    manifest: dict[str, Any],
    baseline_audit: dict[str, Any],
    current_audit: dict[str, Any],
) -> dict[str, Any]:
    slim_thread_ids = [str(thread_id) for thread_id in manifest.get("prompt_preserving_slim_thread_ids") or []]
    baseline_by_id = {
        str(row.get("id") or ""): row
        for row in baseline_audit.get("threads") or []
        if str(row.get("id") or "")
    }
    current_by_id = {
        str(row.get("id") or ""): row
        for row in current_audit.get("threads") or []
        if str(row.get("id") or "")
    }
    reductions: list[dict[str, Any]] = []
    for thread_id in slim_thread_ids:
        baseline_row = baseline_by_id.get(thread_id)
        current_row = current_by_id.get(thread_id)
        if baseline_row is None or current_row is None:
            raise RuntimeError(f"targeted slim thread is missing from a restart audit: {thread_id}")
        baseline_scan = baseline_row.get("scan") if isinstance(baseline_row.get("scan"), dict) else {}
        current_scan = current_row.get("scan") if isinstance(current_row.get("scan"), dict) else {}
        baseline_bytes = int(baseline_scan.get("total_bytes") or 0)
        current_bytes = int(current_scan.get("total_bytes") or 0)
        if baseline_bytes <= 0 or current_bytes >= baseline_bytes:
            raise RuntimeError(f"targeted slim rollout was not smaller after restart: {thread_id}")
        reductions.append(
            {
                "thread_id": thread_id,
                "baseline_bytes": baseline_bytes,
                "current_bytes": current_bytes,
                "reduction_bytes": baseline_bytes - current_bytes,
            }
        )
    return {
        "enabled": bool(slim_thread_ids),
        "thread_count": len(slim_thread_ids),
        "reductions": reductions,
    }


def validate_restart_prompt_contract(
    baseline_audit: dict[str, Any],
    current_audit: dict[str, Any],
) -> dict[str, Any]:
    current_by_id = {
        str(row.get("id") or ""): row
        for row in current_audit.get("threads") or []
        if str(row.get("id") or "")
    }
    checked_threads = 0
    baseline_prompt_count = 0
    appended_prompt_count = 0
    for baseline_row in baseline_audit.get("threads") or []:
        thread_id = str(baseline_row.get("id") or "")
        baseline_scan = baseline_row.get("scan") if isinstance(baseline_row.get("scan"), dict) else {}
        if not thread_id or "user_prompt_count" not in baseline_scan or "user_prompt_sha256" not in baseline_scan:
            raise RuntimeError(f"prompt baseline is incomplete for thread: {thread_id or '<missing>'}")
        current_row = current_by_id.get(thread_id)
        if current_row is None:
            raise RuntimeError(f"baseline thread is missing after restart: {thread_id}")
        current_scan = current_row.get("scan") if isinstance(current_row.get("scan"), dict) else {}
        expected_count = int(baseline_scan["user_prompt_count"])
        expected_sha256 = str(baseline_scan["user_prompt_sha256"]).casefold()
        current_count = int(current_scan.get("user_prompt_count") or 0)
        current_sha256 = str(current_scan.get("user_prompt_sha256") or "").casefold()
        if current_count < expected_count:
            raise RuntimeError(f"user prompt records were removed after restart: {thread_id}")
        if current_count == expected_count:
            if current_sha256 != expected_sha256:
                raise RuntimeError(f"user prompts changed after restart: {thread_id}")
        else:
            prefix_count, prefix_sha256 = user_prompt_prefix_fingerprint(
                Path(str(current_row.get("rollout_path") or "")),
                expected_count,
            )
            if prefix_count != expected_count or prefix_sha256 != expected_sha256:
                raise RuntimeError(f"baseline prompts are not an exact prefix after restart: {thread_id}")
        checked_threads += 1
        baseline_prompt_count += expected_count
        appended_prompt_count += current_count - expected_count
    return {
        "mode": "baseline_exact_prefix",
        "checked_threads": checked_threads,
        "baseline_prompt_count": baseline_prompt_count,
        "appended_prompt_count": appended_prompt_count,
    }


def verify_after_restart(codex_home: Path, manifest_path: Path, report_path: Path) -> dict[str, Any]:
    manifest = validate_manifest_contract(codex_home, manifest_path)
    log_database_path = codex_home / "logs_2.sqlite"
    notify_log_start = capture_notify_log_boundary(log_database_path)
    notify_min_epoch = max(
        int(manifest.get("completed_at_epoch") or 0),
        int(notify_log_start.get("process_started_at_epoch") or 0),
    )
    if notify_min_epoch <= 0:
        raise RuntimeError("post-restart notify log epoch boundary is unavailable")
    baseline_audit = load_prompt_baseline_audit(manifest_path, manifest)
    validate_restart_report_path(manifest_path, report_path)
    config = read_config(codex_home)
    codex_cli = resolve_codex_cli(config)
    plugin_registry = validate_plugin_registry(codex_cli, codex_home)
    windows_incompatible_plugins = validate_windows_incompatible_plugin_registry(codex_cli, codex_home)
    appx_marketplace = resolve_appx_bundled_marketplace()
    plugin_trees = validate_plugin_trees(codex_home, appx_marketplace, plugin_registry)
    node_runtime = validate_node_runtime(config)
    diagnostics = run_codex_diagnostics(
        str(codex_home),
        sidebar_limit=1000,
        language="zh",
        comprehensive_event_stream=True,
    )
    diagnostic_failures = diagnostics_gate_failures(diagnostics, list(required_diagnostic_checks))
    audit_path = report_path.with_name("post_restart_thread_audit.json")
    audit = audit_threads(codex_home / "state_5.sqlite", audit_path)
    summary = audit["summary"]
    validate_post_restart_audit_summary(summary)
    prompt_contract = validate_restart_prompt_contract(baseline_audit, audit)
    prompt_contract["targeted_slim"] = validate_restart_slim_contract(manifest, baseline_audit, audit)
    if diagnostic_failures:
        raise RuntimeError(f"post-restart diagnostics gate failed: {diagnostic_failures}")
    notify_os206_validation = validate_restart_notify_log_window(
        log_database_path,
        notify_log_start,
        min_epoch=notify_min_epoch,
    )
    report = {
        "schema_version": 1,
        "status": "pending_live_ui_validation",
        "run_id": str(manifest["runner_run_id"]),
        "generated_at_epoch": int(time.time()),
        "codex_home": str(codex_home),
        "codex_cli": str(codex_cli),
        "plugins": plugin_registry,
        "windows_incompatible_plugins": windows_incompatible_plugins,
        "plugin_trees": plugin_trees,
        "node_runtime": node_runtime,
        "notify_os206_validation": notify_os206_validation,
        "diagnostics": {
            "score": diagnostics.get("score"),
            "status": diagnostics.get("status"),
            "required_check_failures": diagnostic_failures,
        },
        "thread_audit": summary,
        "prompt_contract": prompt_contract,
        "remaining_validation": [
            "Browser/Chrome/Computer Use live tool calls from a restarted Desktop thread",
            "Representative large-thread UI responsiveness and lazy rendering",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    update_manifest(manifest_path, report_path)
    update_active_repair_lock(
        manifest_path,
        "pending_live_ui_validation",
        {"pending_restart_validation"},
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a completed offline Codex repair after Desktop restart.")
    parser.add_argument("--codex-home", type=Path, default=Path(r"D:\.codex"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--complete-live-validation", action="store_true")
    parser.add_argument("--live-evidence", type=Path)
    arguments = parser.parse_args()
    if arguments.complete_live_validation:
        if arguments.live_evidence is None:
            parser.error("--live-evidence is required with --complete-live-validation")
        result = complete_live_validation(arguments.codex_home, arguments.manifest, arguments.live_evidence)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if arguments.report is None:
        parser.error("--report is required unless --complete-live-validation is used")
    report = verify_after_restart(arguments.codex_home, arguments.manifest, arguments.report)
    print(json.dumps({"status": report["status"], "report": str(arguments.report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
