from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from ..probe_budget import ProbeBudget, probe_text


runtime_manifest_name = "provider-interop-runtime.json"
required_runtime_contracts = {
    "responseItemTypePrefix": 1,
    "portableToolsForExplicitBaseUrl": 1,
}


def inspect_runtime_compatibility(
    *,
    official_resources: str | Path | None = None,
    override_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    codex_home: str | Path | None = None,
    official_cli_version: str | None = None,
    override_cli_version: str | None = None,
    runtime_catalog_root: str | Path | None = None,
    allow_execution: bool = True,
    probe_budget: ProbeBudget | None = None,
) -> dict[str, Any]:
    if probe_budget is not None:
        probe_budget.check()
    resources = resolve_official_resources(official_resources, allow_execution=allow_execution)
    configured_override, activation_sources, activation_conflict = resolve_runtime_activation(
        override_path,
        codex_home,
        probe_budget=probe_budget,
    )
    official_cli = resources / "codex.exe" if resources else None
    official_host = resources / "codex-code-mode-host.exe" if resources else None
    patched_host = configured_override.parent / "codex-code-mode-host.exe" if configured_override else None
    issues: list[dict[str, str]] = []
    override_record = runtime_record(configured_override, override_cli_version, probe_budget=probe_budget)
    manifest_file = resolve_manifest_path(
        configured_override,
        manifest_path,
        runtime_catalog_root=runtime_catalog_root,
        override_sha256=str(override_record.get("sha256") or ""),
        probe_budget=probe_budget,
    )
    manifest = read_manifest(manifest_file, issues, probe_budget=probe_budget)

    result: dict[str, Any] = {
        "classification": "unmanaged",
        "safeForProviderSwitch": False,
        "safeForDesktopUpgrade": False,
        "officialUpdateDetected": False,
        "recommendedAction": "inspect_runtime_configuration",
        "contractVersion": str(manifest.get("contractVersion") or ""),
        "contracts": manifest_contracts(manifest),
        "activationSources": activation_sources,
        "official": runtime_record(official_cli, official_cli_version, probe_budget=probe_budget),
        "override": override_record,
        "companion": runtime_record(patched_host, None, probe_budget=probe_budget),
        "manifestPath": str(manifest_file) if manifest_file else "",
        "issues": issues,
    }
    result["official"]["appVersion"] = appx_version(resources)
    result["official"]["companionExists"] = bool(official_host and official_host.is_file())
    actual_official_hash = str(result["official"].get("sha256") or "")
    official_verified = bool(resources and resources.is_dir() and result["official"]["exists"]
                             and re.fullmatch(r"[0-9a-fA-F]{64}", actual_official_hash))

    if configured_override is None:
        if not official_verified:
            issues.append(issue("official_runtime_unverified", "Official runtime directory, file or valid current SHA-256 is unavailable"))
            result.update(classification="unknown", recommendedAction="verify_official_runtime_source")
            return result
        result.update(
            classification="official_runtime_active",
            recommendedAction="validate_official_fix_before_cross_provider_continuation",
            safeForDesktopUpgrade=bool(official_cli and official_cli.is_file()),
        )
        return result
    if not manifest:
        result.update(classification="unmanaged_override", recommendedAction="register_runtime_manifest")
        return result

    expected_patched_hash = nested_text(manifest, "patched", "cliSha256")
    expected_companion_hash = nested_text(manifest, "companion", "sha256")
    patched_hash = str(result["override"].get("sha256") or "")
    companion_hash = str(result["companion"].get("sha256") or "")
    if not configured_override.is_file():
        issues.append(issue("patched_cli_missing", "Configured patched Codex CLI is missing"))
    elif not expected_patched_hash or patched_hash.casefold() != expected_patched_hash.casefold():
        issues.append(issue("patched_cli_hash_mismatch", "Patched Codex CLI hash does not match its manifest"))
    if patched_host is None or not patched_host.is_file():
        issues.append(issue("companion_missing", "Patched runtime companion is missing"))
    elif not expected_companion_hash or companion_hash.casefold() != expected_companion_hash.casefold():
        issues.append(issue("companion_hash_mismatch", "Patched runtime companion hash does not match its manifest"))
    if issues:
        result.update(
            classification="patched_runtime_invalid",
            recommendedAction="restore_or_reinstall_verified_patch",
        )
        return result

    missing_contracts = [
        name
        for name, required_version in required_runtime_contracts.items()
        if result["contracts"].get(name, 0) < required_version
    ]
    if missing_contracts:
        issues.append(
            issue(
                "runtime_contract_outdated",
                "Patched runtime does not provide every required provider interoperability contract",
            )
        )
        result.update(
            classification="runtime_contract_outdated",
            recommendedAction="install_current_provider_interop_runtime",
            missingContracts=missing_contracts,
        )
        return result

    if activation_conflict:
        issues.append(
            issue(
                "runtime_activation_source_conflict",
                "Codex config and the inherited CODEX_CLI_PATH select different runtimes",
            )
        )
        result.update(
            classification="runtime_activation_conflict",
            recommendedAction="synchronize_runtime_activation_sources",
        )
        return result

    expected_official_hash = nested_text(manifest, "official", "cliSha256")
    baseline = manifest.get("official")
    baseline_verified = bool(isinstance(baseline, dict) and isinstance(baseline.get("cliSha256"), str)
                            and re.fullmatch(r"[0-9a-fA-F]{64}", expected_official_hash))
    if not official_verified or not baseline_verified:
        if not official_verified:
            issues.append(issue("official_runtime_unverified", "Official runtime directory, file or valid current SHA-256 is unavailable"))
        if not baseline_verified:
            issues.append(issue("official_baseline_unverified", "Manifest official baseline SHA-256 is missing or invalid"))
        result.update(classification="unknown", recommendedAction="verify_official_runtime_source_and_baseline")
        return result

    result["safeForProviderSwitch"] = True
    official_changed = actual_official_hash.casefold() != expected_official_hash.casefold()
    result["officialUpdateDetected"] = official_changed
    if official_changed:
        result.update(
            classification="official_upgrade_detected",
            recommendedAction="rebase_patch_before_switching_runtime",
        )
        return result

    result.update(
        classification="verified_patched",
        safeForDesktopUpgrade=True,
        recommendedAction="hold_verified_patch",
    )
    return result


def resolve_official_resources(explicit: str | Path | None, *, allow_execution: bool = True) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    configured = os.environ.get("CODEX_OFFICIAL_RESOURCES_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    if not allow_execution or os.name != "nt":
        return None
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not powershell:
        return None
    script = (
        "$package = Get-AppxPackage -Name OpenAI.Codex | "
        "Sort-Object {[version]$_.Version} -Descending | Select-Object -First 1; "
        "if ($null -ne $package) { Join-Path $package.InstallLocation 'app\\resources' }"
    )
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            startupinfo=startup_info,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path_text = completed.stdout.strip() if completed.returncode == 0 else ""
    return Path(path_text).resolve(strict=False) if path_text else None


def resolve_runtime_activation(
    explicit: str | Path | None,
    codex_home: str | Path | None,
    *, probe_budget: ProbeBudget | None = None,
) -> tuple[Path | None, dict[str, str], bool]:
    if explicit is not None:
        path = resolved_path(explicit)
        return path, {"explicit": str(path) if path else ""}, False

    process_path = resolved_path(os.environ.get("CODEX_CLI_PATH", ""))
    config_path = configured_runtime_path(codex_home, probe_budget=probe_budget)
    selected = config_path or process_path
    sources = {
        "config": str(config_path) if config_path else "",
        "processEnvironment": str(process_path) if process_path else "",
    }
    conflict = bool(config_path and process_path and not same_path(config_path, process_path))
    return selected, sources, conflict


def resolve_override_path(explicit: str | Path | None) -> Path | None:
    value = str(explicit).strip() if explicit is not None else os.environ.get("CODEX_CLI_PATH", "").strip()
    return resolved_path(value)


def configured_runtime_path(codex_home: str | Path | None, *, probe_budget: ProbeBudget | None = None) -> Path | None:
    home_value = str(codex_home).strip() if codex_home is not None else os.environ.get("CODEX_HOME", "").strip()
    home = Path(home_value).expanduser().resolve(strict=False) if home_value else Path.home() / ".codex"
    config_path = home / "config.toml"
    if not config_path.is_file():
        return None
    try:
        config = tomllib.loads(probe_text(config_path, probe_budget))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    top_level = config.get("CODEX_CLI_PATH", "")
    mcp_servers = config.get("mcp_servers")
    node_repl = mcp_servers.get("node_repl") if isinstance(mcp_servers, dict) else None
    node_repl_environment = node_repl.get("env") if isinstance(node_repl, dict) else None
    node_repl_value = (
        node_repl_environment.get("CODEX_CLI_PATH", "")
        if isinstance(node_repl_environment, dict)
        else ""
    )
    return resolved_path(node_repl_value or top_level)


def resolved_path(value: str | Path | None) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def resolve_manifest_path(
    override: Path | None,
    explicit: str | Path | None,
    *,
    runtime_catalog_root: str | Path | None = None,
    override_sha256: str = "",
    probe_budget: ProbeBudget | None = None,
) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    if override is None:
        return None
    adjacent = override.parent / runtime_manifest_name
    if adjacent.is_file():
        return adjacent
    catalog_root = (
        Path(runtime_catalog_root).expanduser().resolve(strict=False)
        if runtime_catalog_root is not None
        else Path(__file__).resolve().parents[2] / "packaging" / "runtime"
    )
    if not catalog_root.is_dir() or not re.fullmatch(r"[0-9a-fA-F]{64}", override_sha256):
        return adjacent
    matches: list[Path] = []
    for candidate in sorted(catalog_root.glob("provider-interop-runtime-*.json"))[:64]:
        try:
            value = json.loads(probe_text(candidate, probe_budget))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("schemaVersion") == 1
            and nested_text(value, "patched", "cliSha256").casefold() == override_sha256.casefold()
        ):
            matches.append(candidate.resolve(strict=False))
    return matches[0] if len(matches) == 1 else adjacent


def read_manifest(path: Path | None, issues: list[dict[str, str]], *, probe_budget: ProbeBudget | None = None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(probe_text(path, probe_budget))
    except (OSError, json.JSONDecodeError):
        issues.append(issue("manifest_invalid", "Provider interoperability runtime manifest is invalid"))
        return {}
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        issues.append(issue("manifest_schema_unsupported", "Provider interoperability runtime manifest schema is unsupported"))
        return {}
    return value


def runtime_record(path: Path | None, version: str | None, *, probe_budget: ProbeBudget | None = None) -> dict[str, Any]:
    if probe_budget is not None:
        probe_budget.check()
    exists = bool(path and path.is_file())
    return {
        "path": str(path) if path else "",
        "exists": exists,
        "version": str(version or ""),
        "sha256": sha256_file(path, probe_budget=probe_budget) if exists and path else "",
    }


def sha256_file(path: Path, chunk_bytes: int = 4 * 1024 * 1024, *, probe_budget: ProbeBudget | None = None) -> str:
    if probe_budget is not None:
        return probe_budget.read(path, maximum_bytes=512 * 1024 * 1024, digest=True).upper()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest().upper()


def nested_text(value: dict[str, Any], section: str, key: str) -> str:
    nested = value.get(section)
    return str(nested.get(key) or "") if isinstance(nested, dict) else ""


def manifest_contracts(manifest: dict[str, Any]) -> dict[str, int]:
    value = manifest.get("contracts")
    if not isinstance(value, dict):
        return {}
    return {
        str(name): version
        for name, version in value.items()
        if isinstance(version, int) and not isinstance(version, bool) and version >= 0
    }


def appx_version(resources: Path | None) -> str:
    if resources is None:
        return ""
    match = re.search(r"OpenAI\.Codex_([^_\\/]+)", str(resources), re.IGNORECASE)
    return match.group(1) if match else ""


def issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}
