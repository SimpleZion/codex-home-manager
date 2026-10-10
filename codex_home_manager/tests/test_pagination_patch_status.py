from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend import pagination_patch_status


package_full_name = "OpenAI.Codex_26.831.1445.0_x64__2p2nqsd0c76g0"


def feature_contract(**overrides: object) -> dict:
    value = {
        "featureContractVersion": 5,
        "initialTurnItemsPaginated": True,
        "initialTurnItemPageSize": 100,
        "promptRailLoadedTurnsOnly": True,
        "periodicStatusMetadataOnly": True,
        "priorityFeedMetadataOnly": True,
        "passiveTabRestorationLoadedTurnsOnly": True,
        "runtimeAcceptanceRequired": True,
    }
    value.update(overrides)
    return value


def write_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "CODEX_PAGINATION_READY_STATE": str(tmp_path / "ready.json"),
        "CODEX_PAGINATION_INSTALLED_STATE": str(tmp_path / "installed.json"),
    }


def desktop_install() -> dict[str, str]:
    return {
        "version": "26.831.1445.0",
        "path": rf"C:\Program Files\WindowsApps\{package_full_name}\app\ChatGPT.exe",
    }


def test_matching_receipt_does_not_prove_current_runtime(tmp_path: Path) -> None:
    test_environment = environment(tmp_path)
    write_state(
        Path(test_environment["CODEX_PAGINATION_INSTALLED_STATE"]),
        feature_contract(**{
            "status": "success",
            "mainPackageFullName": package_full_name,
            "packageStatus": "Ok",
            "patchedAsarSha256": "ABCD",
            "effectiveAsarHash": "abcd",
            "initialTurns": 1,
            "userPageTurns": 3,
            "runtimeStatus": "passed",
            "runtimeObservationSeconds": 180,
            "runtimeBackgroundTurnListRequests": 0,
        }),
    )

    result = pagination_patch_status.detect_pagination_patch_status(desktop_install(), test_environment)

    assert result["state"] == "unverified"
    assert result["action"] == "verify"
    assert result["currentRuntimeVerified"] is False
    assert result["evidenceScope"] == "receipt_only"
    assert result["receiptObservedAt"] == ""
    assert result["policy"]["initialTurns"] == 1
    assert result["policy"]["userPageTurns"] == 3
    assert result["policy"]["initialTurnItemsPaginated"] is True
    assert result["policy"]["initialTurnItemPageSize"] == 100
    assert result["policy"]["promptRailLoadedTurnsOnly"] is True
    assert result["policy"]["priorityFeedMetadataOnly"] is True
    assert result["policy"]["passiveTabRestorationLoadedTurnsOnly"] is True
    assert result["updateAvailable"] is False


def test_newer_ready_package_takes_priority_over_matching_installed_receipt(tmp_path: Path) -> None:
    test_environment = environment(tmp_path)
    write_state(
        Path(test_environment["CODEX_PAGINATION_INSTALLED_STATE"]),
        feature_contract(**{
            "status": "success",
            "mainPackageFullName": package_full_name,
            "packageStatus": "Ok",
            "packageVersion": "26.831.1445.1",
            "patchedAsarSha256": "AAAA",
            "effectiveAsarHash": "AAAA",
            "runtimeStatus": "passed",
            "runtimeObservationSeconds": 180,
            "runtimeBackgroundTurnListRequests": 0,
        }),
    )
    write_state(
        Path(test_environment["CODEX_PAGINATION_READY_STATE"]),
        feature_contract(**{
            "status": "ready",
            "mainPackageFullName": package_full_name,
            "packageVersion": "26.831.1445.2",
            "patchedAsarSha256": "BBBB",
        }),
    )

    result = pagination_patch_status.detect_pagination_patch_status(desktop_install(), test_environment)

    assert result["state"] == "ready"
    assert result["action"] == "install"
    assert result["updateAvailable"] is True
    assert result["currentRuntimeVerified"] is False


def test_store_upgrade_marks_old_evidence_stale(tmp_path: Path) -> None:
    test_environment = environment(tmp_path)
    write_state(
        Path(test_environment["CODEX_PAGINATION_READY_STATE"]),
        feature_contract(status="ready", mainPackageFullName="OpenAI.Codex_26.825.6671.0_x64__2p2nqsd0c76g0"),
    )

    result = pagination_patch_status.detect_pagination_patch_status(desktop_install(), test_environment)

    assert result["state"] == "stale"
    assert result["action"] == "rebuild"


def test_current_ready_package_is_reported_as_ready(tmp_path: Path) -> None:
    test_environment = environment(tmp_path)
    write_state(
        Path(test_environment["CODEX_PAGINATION_READY_STATE"]),
        feature_contract(**{
            "status": "ready",
            "mainPackageFullName": package_full_name,
            "initialTurns": 1,
            "userPageTurns": 3,
        }),
    )

    result = pagination_patch_status.detect_pagination_patch_status(desktop_install(), test_environment)

    assert result["state"] == "ready"
    assert result["action"] == "install"


def test_malformed_state_is_invalid(tmp_path: Path) -> None:
    test_environment = environment(tmp_path)
    ready_path = Path(test_environment["CODEX_PAGINATION_READY_STATE"])
    ready_path.write_text("{", encoding="utf-8")

    result = pagination_patch_status.detect_pagination_patch_status(desktop_install(), test_environment)

    assert result["state"] == "invalid"
    assert result["action"] == "verify"


def test_installed_package_waits_for_runtime_acceptance(tmp_path: Path) -> None:
    test_environment = environment(tmp_path)
    write_state(
        Path(test_environment["CODEX_PAGINATION_INSTALLED_STATE"]),
        feature_contract(**{
            "status": "success",
            "mainPackageFullName": package_full_name,
            "packageStatus": "Ok",
            "patchedAsarSha256": "ABCD",
            "effectiveAsarHash": "ABCD",
            "runtimeStatus": "pending",
        }),
    )

    result = pagination_patch_status.detect_pagination_patch_status(desktop_install(), test_environment)

    assert result["state"] == "unverified"
    assert result["action"] == "verify"


def test_old_feature_contract_is_not_reported_as_effective(tmp_path: Path) -> None:
    test_environment = environment(tmp_path)
    write_state(
        Path(test_environment["CODEX_PAGINATION_INSTALLED_STATE"]),
        {
            "status": "success",
            "mainPackageFullName": package_full_name,
            "packageStatus": "Ok",
            "patchedAsarSha256": "ABCD",
            "effectiveAsarHash": "ABCD",
        },
    )

    result = pagination_patch_status.detect_pagination_patch_status(desktop_install(), test_environment)

    assert result["state"] == "invalid"
    assert result["action"] == "verify"


def test_6511_requires_fresh_boundary_and_bundle_execution_evidence(tmp_path: Path) -> None:
    test_environment = environment(tmp_path)
    package = "OpenAI.Codex_26.901.6511.0_x64__2p2nqsd0c76g0"
    desktop = {"version": "26.901.6511.0", "packageFullName": package}
    state = feature_contract(status="ready", mainPackageFullName=package, featureContractVersion=7, freshTurnReadItemBoundary=True, bundleRuntimeAuditPassed=True)
    path = Path(test_environment["CODEX_PAGINATION_READY_STATE"])
    write_state(path, state)
    assert pagination_patch_status.detect_pagination_patch_status(desktop, test_environment)["state"] == "ready"
    for flag in ("freshTurnReadItemBoundary", "bundleRuntimeAuditPassed"):
        write_state(path, {**state, flag: False})
        assert pagination_patch_status.detect_pagination_patch_status(desktop, test_environment)["state"] == "invalid"
    write_state(path, {**state, "featureContractVersion": 6})
    assert pagination_patch_status.detect_pagination_patch_status(desktop, test_environment)["state"] == "invalid"


receipt_cases = json.loads((Path(__file__).parent / "fixtures" / "pagination_receipt_status.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", receipt_cases["cases"], ids=lambda case: case["name"])
def test_receipt_only_status_contract(case: dict) -> None:
    installed = None if case.get("installed", {}) is None else {**receipt_cases["installed"], **case.get("installed", {})}
    ready = {**receipt_cases["installed"], **case["ready"]} if "ready" in case else None
    desktop = {**receipt_cases["desktop"], **case.get("desktop", {})}
    original = json.dumps({"installed": installed, "ready": ready, "desktop": desktop}, sort_keys=True)
    now = 2_000_000_000
    with patch.object(pagination_patch_status, "_candidate_paths", side_effect=lambda _name, filename, _env: [Path(filename)]), patch.object(
        pagination_patch_status, "_read_first_state", side_effect=[(ready, "ready.json", ""), (installed, "installed.json", case.get("installedError", ""))]
    ), patch.object(pagination_patch_status.time, "time", return_value=now), patch.object(Path, "open", side_effect=AssertionError("unexpected live file read")):
        result = pagination_patch_status.detect_pagination_patch_status(desktop, {"test": "receipt-only"})

    assert result["state"] == case["state"]
    assert result["action"] == case["action"]
    assert result["state"] != "effective"
    assert result["evidenceScope"] == "receipt_only"
    assert result["currentRuntimeVerified"] is False
    assert result["checkedAtMs"] == now * 1000
    assert result["receiptObservedAt"] == ((installed or {}).get("runtimeValidatedAt") or "")
    assert result["updateAvailable"] == case.get("updateAvailable", False)
    assert case.get("reasonContains", "") in result["reason"]
    assert result["schemaVersion"] == 3
    if installed:
        for field in ("preparedAt", "finishedAt", "runtimeStatus", "runtimeReportPath", "runtimeMemoryGrowthMiB"):
            assert result["installed"][field] == installed[field]
    assert json.dumps({"installed": installed, "ready": ready, "desktop": desktop}, sort_keys=True) == original
