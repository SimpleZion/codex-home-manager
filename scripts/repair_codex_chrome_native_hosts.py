from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "codex_home_manager"))

from backend.diagnostics import scan_chrome_native_host_paths
from backend.offline_repair_policy import assert_codex_offline


helper_relative_path = Path("@oai") / "sky" / "bin" / "windows" / "codex-computer-use.exe"
required_file_names = ("chrome-native-hosts.json", "chrome-native-hosts-v2.json")


def split_path_list(value: str) -> list[str]:
    separators = [os.pathsep]
    if ";" not in separators:
        separators.append(";")
    values = [value]
    for separator in separators:
        values = [item for current in values for item in current.split(separator)]
    return [item.strip().strip("'\"") for item in values if item.strip().strip("'\"")]


def files_match(left: Path, right: Path) -> bool:
    try:
        if not left.is_file() or not right.is_file() or left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            return hashlib.file_digest(left_handle, "sha256").digest() == hashlib.file_digest(
                right_handle, "sha256"
            ).digest()
    except OSError:
        return False


def read_runtime_paths(codex_home: Path, appx_resources: Path) -> dict[str, Any]:
    config_path = codex_home / "config.toml"
    document = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    node_repl = (document.get("mcp_servers") or {}).get("node_repl") or {}
    environment = node_repl.get("env") or {}
    module_roots = [Path(value).expanduser() for value in split_path_list(str(environment.get("NODE_REPL_NODE_MODULE_DIRS") or ""))]
    module_root = next((root for root in module_roots if (root / helper_relative_path).is_file()), None)
    if module_root is None:
        raise RuntimeError("the Desktop runtime has no valid @oai/sky module root")
    paths: dict[str, Any] = {
        "codexCliPath": str(Path(str(environment.get("CODEX_CLI_PATH") or "")).expanduser()),
        "resourcesPath": str(appx_resources),
        "nodePath": str(Path(str(environment.get("NODE_REPL_NODE_PATH") or "")).expanduser()),
        "nodeReplPath": str(Path(str(node_repl.get("command") or "")).expanduser()),
        "codexHome": str(codex_home),
        "nodeModuleDirs": [str(path) for path in module_roots],
    }
    required_runtime_paths = ["codexCliPath", "nodePath", "nodeReplPath"]
    missing = [paths[key] for key in required_runtime_paths if not Path(paths[key]).is_file()]
    missing.extend(str(path) for path in module_roots if not path.is_dir())
    if not appx_resources.is_dir():
        missing.append(str(appx_resources))
    if missing:
        raise RuntimeError("required Desktop runtime paths are missing: " + ", ".join(missing))
    appx_matches = {
        "codexCliPath": appx_resources / "codex.exe",
        "nodePath": appx_resources / "cua_node" / "bin" / "node.exe",
        "nodeReplPath": appx_resources / "cua_node" / "bin" / "node_repl.exe",
    }
    mismatches = [
        f"{key}: {paths[key]} != {expected_path}"
        for key, expected_path in appx_matches.items()
        if not files_match(Path(str(paths[key])), expected_path)
    ]
    dynamic_helper = module_root / helper_relative_path
    appx_helper = appx_resources / "cua_node" / "bin" / "node_modules" / helper_relative_path
    if not files_match(dynamic_helper, appx_helper):
        mismatches.append(f"notify helper: {dynamic_helper} != {appx_helper}")
    if mismatches:
        raise RuntimeError("Desktop runtime is not bound to the current AppX: " + "; ".join(mismatches))
    return paths


def plugin_version(chrome_root: Path) -> str:
    manifest_path = chrome_root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    value = str(manifest.get("version") or "").strip()
    if not value:
        raise RuntimeError(f"Chrome plugin version is missing: {manifest_path}")
    return value


def expected_native_host_paths(codex_home: Path, appx_resources: Path) -> tuple[dict[str, Any], str]:
    paths = read_runtime_paths(codex_home, appx_resources)
    chrome_root = codex_home / "plugins" / "cache" / "openai-bundled" / "chrome" / "latest"
    paths.update(
        {
            "browserClientPath": str(chrome_root / "scripts" / "browser-client.mjs"),
            "extensionHostPath": str(
                chrome_root / "extension-host" / "windows" / "x64" / "extension-host.exe"
            ),
        }
    )
    missing = [
        str(paths[key])
        for key in ("browserClientPath", "extensionHostPath")
        if not Path(str(paths[key])).is_file()
    ]
    if missing:
        raise RuntimeError("required Chrome native-host assets are missing: " + ", ".join(missing))
    return paths, plugin_version(chrome_root)


def stable_identifier(prefix: str, paths: dict[str, Any]) -> str:
    encoded = json.dumps(paths, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:32]}"


def current_app_version(appx_resources: Path) -> str:
    install_name = appx_resources.parents[1].name
    prefix = "OpenAI.Codex_"
    if install_name.startswith(prefix):
        return install_name[len(prefix) :].split("_", 1)[0]
    return ""


def build_payloads(paths: dict[str, Any], plugin_version_text: str, app_version: str) -> dict[str, dict[str, Any]]:
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    v1_paths = {key: value for key, value in paths.items() if key != "nodeModuleDirs"}
    v1_entry = {
        "schemaVersion": 1,
        **v1_paths,
        "extensionIds": ["hehggadaopoacecdllhhajmbjkdcmajg"],
        "nativeHostName": "com.openai.codexextension",
        "pluginVersion": plugin_version_text,
        "proxyHost": "127.0.0.1",
        "proxyPort": 0,
        "updatedAt": now,
    }
    v2_entry = {
        "schemaVersion": 2,
        "appServerProtocolVersion": 2,
        "appVersion": app_version,
        "channel": "prod",
        "cliVersion": plugin_version_text,
        "entryId": stable_identifier("codex-runtime", paths),
        "extensionBuildChannels": ["prod"],
        "extensionIds": ["hehggadaopoacecdllhhajmbjkdcmajg"],
        "installId": stable_identifier("codex-install", {"codexHome": paths["codexHome"]}),
        "nativeHostNames": ["com.openai.codexextension"],
        "nativeHostProtocolVersion": 2,
        "nativeHostVersion": plugin_version_text,
        "paths": paths,
        "proxyHost": "127.0.0.1",
        "proxyPort": 0,
        "updatedAt": now,
    }
    return {
        "chrome-native-hosts.json": {"schemaVersion": 1, "chromeNativeHosts": [v1_entry]},
        "chrome-native-hosts-v2.json": {"schemaVersion": 2, "entries": [v2_entry]},
    }


def target_backup_directory(target_root: Path, backup_root: Path) -> Path:
    return backup_root / hashlib.sha256(str(target_root).encode("utf-8")).hexdigest()[:12]


def backup_existing_files(target_root: Path, backup_root: Path) -> list[str]:
    destination = target_backup_directory(target_root, backup_root)
    destination.mkdir(parents=True, exist_ok=True)
    backups: list[str] = []
    for file_name in required_file_names:
        source = target_root / file_name
        if source.is_file():
            backup_path = destination / file_name
            shutil.copy2(source, backup_path)
            backups.append(str(backup_path))
    return backups


def replace_payloads(target_root: Path, payloads: dict[str, dict[str, Any]]) -> None:
    target_root.mkdir(parents=True, exist_ok=True)
    temporary_paths: dict[str, Path] = {}
    for file_name, payload in payloads.items():
        target_path = target_root / file_name
        temporary_path = target_path.with_suffix(target_path.suffix + ".repairing")
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        json.loads(rendered)
        temporary_path.write_text(rendered, encoding="utf-8", newline="")
        temporary_paths[file_name] = temporary_path
    assert_codex_offline()
    for file_name, temporary_path in temporary_paths.items():
        target_path = target_root / file_name
        assert_codex_offline()
        os.replace(temporary_path, target_path)


def rollback_target(target_root: Path, backup_root: Path) -> None:
    backup_directory = target_backup_directory(target_root, backup_root)
    backup_directory.mkdir(parents=True, exist_ok=True)
    for file_name in required_file_names:
        target_path = target_root / file_name
        backup_path = backup_directory / file_name
        assert_codex_offline()
        if backup_path.is_file():
            temporary_path = target_path.with_suffix(target_path.suffix + ".rolling-back")
            shutil.copyfile(backup_path, temporary_path)
            os.replace(temporary_path, target_path)
        elif target_path.is_file():
            failed_new_path = backup_directory / f"failed-new-{file_name}"
            os.replace(target_path, failed_new_path)


def validate_target(target_root: Path, appx_resources: Path, expected_paths: dict[str, Any]) -> dict[str, Any]:
    scan = scan_chrome_native_host_paths(
        target_root,
        current_appx_install={
            "available": True,
            "installPath": str(appx_resources.parents[1]),
            "version": current_app_version(appx_resources),
            "error": "",
        },
        expected_paths=expected_paths,
    )
    if not scan["configurationComplete"]:
        raise RuntimeError(
            f"Chrome native-host validation failed for {target_root}: "
            + json.dumps(scan, ensure_ascii=False, sort_keys=True)
        )
    return scan


def repair_native_hosts(
    *,
    codex_home: Path,
    appx_resources: Path,
    target_roots: list[Path],
    backup_root: Path,
) -> dict[str, Any]:
    assert_codex_offline()
    expected_paths, plugin_version_text = expected_native_host_paths(codex_home, appx_resources)
    payloads = build_payloads(expected_paths, plugin_version_text, current_app_version(appx_resources))
    unique_targets: list[Path] = []
    seen: set[str] = set()
    for target_root in target_roots:
        key = os.path.normcase(str(target_root.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            unique_targets.append(target_root)
    backups_by_target: dict[str, list[str]] = {}
    for target_root in unique_targets:
        assert_codex_offline()
        backups_by_target[os.path.normcase(str(target_root.resolve(strict=False)))] = backup_existing_files(
            target_root,
            backup_root,
        )

    results = []
    attempted_targets: list[Path] = []
    try:
        for target_root in unique_targets:
            assert_codex_offline()
            attempted_targets.append(target_root)
            replace_payloads(target_root, payloads)
            scan = validate_target(target_root, appx_resources, expected_paths)
            target_key = os.path.normcase(str(target_root.resolve(strict=False)))
            results.append(
                {
                    "targetRoot": str(target_root),
                    "backups": backups_by_target[target_key],
                    "scan": scan,
                }
            )
    except Exception as repair_error:
        rollback_errors: list[str] = []
        for target_root in reversed(attempted_targets):
            try:
                rollback_target(target_root, backup_root)
            except Exception as rollback_error:
                rollback_errors.append(f"{target_root}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                "Chrome native-host repair failed and rollback was incomplete: " + "; ".join(rollback_errors)
            ) from repair_error
        raise
    return {"schemaVersion": 1, "status": "complete", "expectedPaths": expected_paths, "targets": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild current Codex Chrome native-host runtime files.")
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--appx-resources", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, action="append", default=[])
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    codex_home = arguments.codex_home.resolve(strict=False)
    target_roots = [codex_home, *[path.resolve(strict=False) for path in arguments.target_root]]
    result = repair_native_hosts(
        codex_home=codex_home,
        appx_resources=arguments.appx_resources.resolve(strict=False),
        target_roots=target_roots,
        backup_root=arguments.backup_root.resolve(strict=False),
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(rendered, encoding="utf-8", newline="")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
