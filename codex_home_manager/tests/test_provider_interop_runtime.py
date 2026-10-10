from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.probe_budget import ProbeBudget
from backend.provider_interop.runtime import inspect_runtime_compatibility


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def create_runtime_fixture(tmp_path: Path, *, include_companion: bool = True) -> tuple[Path, Path, Path]:
    official_resources = tmp_path / "OpenAI.Codex_26.810.7004.0_x64_test" / "app" / "resources"
    official_resources.mkdir(parents=True)
    official_cli = official_resources / "codex.exe"
    official_host = official_resources / "codex-code-mode-host.exe"
    official_cli.write_bytes(b"official-cli")
    official_host.write_bytes(b"official-host")

    patched_root = tmp_path / "provider-interop" / "0.148.0-alpha.9-p2"
    patched_root.mkdir(parents=True)
    patched_cli = patched_root / "codex.exe"
    patched_host = patched_root / "codex-code-mode-host.exe"
    patched_cli.write_bytes(b"patched-cli")
    if include_companion:
        patched_host.write_bytes(b"official-host")
    manifest_path = patched_root / "provider-interop-runtime.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "contractVersion": "provider-interoperability-v2",
                "contracts": {
                    "responseItemTypePrefix": 1,
                    "portableToolsForExplicitBaseUrl": 1,
                },
                "official": {
                    "appVersion": "26.810.7004.0",
                    "cliVersion": "0.148.0-alpha.9",
                    "cliSha256": file_sha256(official_cli),
                },
                "patched": {
                    "cliVersion": "0.148.0-alpha.9",
                    "cliSha256": file_sha256(patched_cli),
                },
                "companion": {"sha256": file_sha256(official_host)},
            }
        ),
        encoding="utf-8",
    )
    return official_resources, patched_cli, manifest_path


def test_verified_patch_is_held_until_a_validated_upgrade_exists(tmp_path: Path) -> None:
    official_resources, patched_cli, manifest_path = create_runtime_fixture(tmp_path)

    result = inspect_runtime_compatibility(
        official_resources=official_resources,
        override_path=patched_cli,
        manifest_path=manifest_path,
        official_cli_version="0.148.0-alpha.9",
        override_cli_version="0.148.0-alpha.9",
        allow_execution=False,
        probe_budget=ProbeBudget(maximum_bytes=65536),
    )

    assert result["classification"] == "verified_patched"
    assert result["safeForProviderSwitch"] is True
    assert result["officialUpdateDetected"] is False
    assert result["recommendedAction"] == "hold_verified_patch"


def test_verified_bundled_manifest_is_discovered_by_runtime_hash(tmp_path: Path) -> None:
    official_resources, patched_cli, adjacent_manifest = create_runtime_fixture(tmp_path)
    runtime_catalog = tmp_path / "packaging" / "runtime"
    runtime_catalog.mkdir(parents=True)
    bundled_manifest = runtime_catalog / "provider-interop-runtime-fixture.json"
    bundled_manifest.write_bytes(adjacent_manifest.read_bytes())
    adjacent_manifest.unlink()

    result = inspect_runtime_compatibility(
        official_resources=official_resources,
        override_path=patched_cli,
        runtime_catalog_root=runtime_catalog,
        official_cli_version="0.148.0-alpha.9",
        override_cli_version="0.148.0-alpha.9",
        allow_execution=False,
        probe_budget=ProbeBudget(maximum_bytes=65536),
    )

    assert result["classification"] == "verified_patched"
    assert result["safeForProviderSwitch"] is True
    assert result["manifestPath"] == str(bundled_manifest.resolve())


def test_config_and_process_environment_runtime_paths_must_agree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    official_resources, patched_cli, _manifest_path = create_runtime_fixture(tmp_path)
    stale_cli = tmp_path / "provider-interop" / "0.148.0-alpha.9-p1" / "codex.exe"
    stale_cli.parent.mkdir(parents=True)
    stale_cli.write_bytes(b"stale-cli")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "[mcp_servers.node_repl.env]\n"
        f"CODEX_CLI_PATH = '{patched_cli}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_CLI_PATH", str(stale_cli))

    result = inspect_runtime_compatibility(
        official_resources=official_resources,
        codex_home=codex_home,
        official_cli_version="0.148.0-alpha.9",
        override_cli_version="0.148.0-alpha.9",
    )

    assert result["classification"] == "runtime_activation_conflict"
    assert result["safeForProviderSwitch"] is False
    assert result["recommendedAction"] == "synchronize_runtime_activation_sources"
    assert result["activationSources"]["config"] == str(patched_cli.resolve())
    assert result["activationSources"]["processEnvironment"] == str(stale_cli.resolve())
    assert {item["code"] for item in result["issues"]} == {
        "runtime_activation_source_conflict"
    }


def test_official_binary_change_is_detected_by_hash_not_directory_name(tmp_path: Path) -> None:
    official_resources, patched_cli, manifest_path = create_runtime_fixture(tmp_path)
    (official_resources / "codex.exe").write_bytes(b"new-official-cli")

    result = inspect_runtime_compatibility(
        official_resources=official_resources,
        override_path=patched_cli,
        manifest_path=manifest_path,
        official_cli_version="0.149.0",
        override_cli_version="0.148.0-alpha.9",
        allow_execution=False,
        probe_budget=ProbeBudget(maximum_bytes=65536),
    )

    assert result["classification"] == "official_upgrade_detected"
    assert result["safeForProviderSwitch"] is True
    assert result["officialUpdateDetected"] is True
    assert result["recommendedAction"] == "rebase_patch_before_switching_runtime"


def test_legacy_id_only_patch_is_not_declared_safe_for_provider_switch(tmp_path: Path) -> None:
    official_resources, patched_cli, manifest_path = create_runtime_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contractVersion"] = "response-item-type-prefix-v1"
    manifest.pop("contracts")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = inspect_runtime_compatibility(
        official_resources=official_resources,
        override_path=patched_cli,
        manifest_path=manifest_path,
        official_cli_version="0.148.0-alpha.9",
        override_cli_version="0.148.0-alpha.9",
    )

    assert result["classification"] == "runtime_contract_outdated"
    assert result["safeForProviderSwitch"] is False
    assert result["missingContracts"] == [
        "responseItemTypePrefix",
        "portableToolsForExplicitBaseUrl",
    ]
    assert result["recommendedAction"] == "install_current_provider_interop_runtime"


def test_tampered_patch_or_missing_companion_fails_closed(tmp_path: Path) -> None:
    official_resources, patched_cli, manifest_path = create_runtime_fixture(tmp_path, include_companion=False)
    patched_cli.write_bytes(b"tampered")

    result = inspect_runtime_compatibility(
        official_resources=official_resources,
        override_path=patched_cli,
        manifest_path=manifest_path,
        official_cli_version="0.148.0-alpha.9",
        override_cli_version="0.148.0-alpha.9",
    )

    assert result["classification"] == "patched_runtime_invalid"
    assert result["safeForProviderSwitch"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "patched_cli_hash_mismatch",
        "companion_missing",
    }
    assert result["recommendedAction"] == "restore_or_reinstall_verified_patch"


@pytest.mark.parametrize("source", ["absent", "missing_directory", "missing_file", "empty_hash", "invalid_hash"])
@pytest.mark.parametrize("with_override", [False, True])
def test_unverified_official_runtime_never_reports_active_or_upgraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, with_override: bool) -> None:
    from backend.provider_interop import runtime
    resources, patched_cli, manifest_path = create_runtime_fixture(tmp_path)
    monkeypatch.delenv("CODEX_OFFICIAL_RESOURCES_PATH", raising=False)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: pytest.fail("read-only inspection executed a process"))
    if source == "absent":
        resources = None
    elif source == "missing_directory":
        resources = tmp_path / "missing-resources"
    elif source == "missing_file":
        resources = tmp_path / "empty-resources"
        resources.mkdir()
    else:
        original_hash = runtime.sha256_file
        official_cli = resources / "codex.exe"
        monkeypatch.setattr(runtime, "sha256_file", lambda path, **kwargs: (
            "" if source == "empty_hash" else "z" * 64) if path == official_cli else original_hash(path, **kwargs))

    result = inspect_runtime_compatibility(official_resources=resources, override_path=patched_cli if with_override else "",
        manifest_path=manifest_path if with_override else None, codex_home=tmp_path / "unused-home",
        allow_execution=False, probe_budget=ProbeBudget(maximum_bytes=65536))
    assert result["classification"] == "unknown"
    assert result["officialUpdateDetected"] is False
    assert result["safeForProviderSwitch"] is False
    assert result["safeForDesktopUpgrade"] is False
    assert {item["code"] for item in result["issues"]} == {"official_runtime_unverified"}


@pytest.mark.parametrize("baseline", [None, "", "abc", "g" * 64, 1, int("1" * 64)])
def test_missing_or_invalid_official_baseline_is_not_an_upgrade(tmp_path: Path, baseline) -> None:
    resources, patched_cli, manifest_path = create_runtime_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["official"] = {} if baseline is None else {"cliSha256": baseline}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = inspect_runtime_compatibility(official_resources=resources, override_path=patched_cli,
        manifest_path=manifest_path, allow_execution=False, probe_budget=ProbeBudget(maximum_bytes=65536))
    assert result["classification"] == "unknown"
    assert result["officialUpdateDetected"] is False
    assert result["safeForProviderSwitch"] is False
    assert result["safeForDesktopUpgrade"] is False
    assert {item["code"] for item in result["issues"]} == {"official_baseline_unverified"}


def test_valid_official_runtime_without_override_still_reports_active(tmp_path: Path) -> None:
    resources, _, _ = create_runtime_fixture(tmp_path)
    result = inspect_runtime_compatibility(official_resources=resources, override_path="",
        allow_execution=False, probe_budget=ProbeBudget(maximum_bytes=65536))
    assert result["classification"] == "official_runtime_active"
    assert result["officialUpdateDetected"] is False
    assert result["safeForProviderSwitch"] is False
    assert result["safeForDesktopUpgrade"] is True
