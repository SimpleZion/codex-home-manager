from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping


pagination_state_schema_version = 3
required_feature_contract_version = 5
maximum_state_bytes = 1024 * 1024
default_artifact_root = Path(r"D:\ZFiles\CXProject\Codex")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _hash(value: Any) -> str:
    return _text(value).upper()


def _positive_integer(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return parsed if parsed > 0 else fallback


def _boolean(value: Any) -> bool:
    return value is True


def _version_parts(value: Any) -> tuple[int, ...] | None:
    text = _text(value)
    if not text:
        return None
    try:
        parts = tuple(int(part) for part in text.split("."))
    except ValueError:
        return None
    return parts if parts and all(part >= 0 for part in parts) else None


def _package_full_name_from_path(path_text: str) -> str:
    if not path_text:
        return ""
    for part in reversed(Path(path_text).parts):
        if part.casefold().startswith("openai.codex_"):
            return part
    return ""


def _candidate_paths(environment_name: str, filename: str, environment: Mapping[str, str]) -> list[Path]:
    candidates: list[Path] = []
    configured = _text(environment.get(environment_name))
    if configured:
        return [Path(configured).expanduser()]
    local_app_data = _text(environment.get("LOCALAPPDATA"))
    if local_app_data:
        candidates.append(Path(local_app_data) / "CodexHomeManager" / "pagination" / filename)
    candidates.append(default_artifact_root / f"codex-pagination-{filename}")
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _read_first_state(paths: list[Path]) -> tuple[dict[str, Any] | None, str, str]:
    for path in paths:
        try:
            if not path.is_file():
                continue
            if path.stat().st_size > maximum_state_bytes:
                return None, str(path), "state file exceeds 1 MiB"
            with path.open("rb") as source:
                content = source.read(maximum_state_bytes + 1)
            if len(content) > maximum_state_bytes:
                return None, str(path), "state file exceeds 1 MiB"
            value = json.loads(content)
            if not isinstance(value, dict):
                return None, str(path), "state root is not an object"
            return value, str(path), ""
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return None, str(path), str(error)
    return None, "", ""


def _state_view(state: dict[str, Any] | None, path: str) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "path": path,
        "status": _text(state.get("status")),
        "targetPackageFullName": _text(state.get("mainPackageFullName")),
        "mainVersion": _text(state.get("mainVersion")),
        "packageVersion": _text(state.get("packageVersion")),
        "packageStatus": _text(state.get("packageStatus")),
        "featureContractVersion": _positive_integer(state.get("featureContractVersion"), 0),
        "freshTurnReadItemBoundary": _boolean(state.get("freshTurnReadItemBoundary")),
        "bundleRuntimeAuditPassed": _boolean(state.get("bundleRuntimeAuditPassed")),
        "initialTurnItemsPaginated": _boolean(state.get("initialTurnItemsPaginated")),
        "initialTurnItemPageSize": _positive_integer(state.get("initialTurnItemPageSize"), 0),
        "promptRailLoadedTurnsOnly": _boolean(state.get("promptRailLoadedTurnsOnly")),
        "periodicStatusMetadataOnly": _boolean(state.get("periodicStatusMetadataOnly")),
        "priorityFeedMetadataOnly": _boolean(state.get("priorityFeedMetadataOnly")),
        "passiveTabRestorationLoadedTurnsOnly": _boolean(
            state.get("passiveTabRestorationLoadedTurnsOnly")
        ),
        "runtimeAcceptanceRequired": _boolean(state.get("runtimeAcceptanceRequired")),
        "runtimeStatus": _text(state.get("runtimeStatus")),
        "runtimeValidatedAt": _text(state.get("runtimeValidatedAt")),
        "runtimeObservationSeconds": _positive_integer(state.get("runtimeObservationSeconds"), 0),
        "runtimeBackgroundTurnListRequests": state.get("runtimeBackgroundTurnListRequests"),
        "runtimeMemoryGrowthMiB": state.get("runtimeMemoryGrowthMiB"),
        "runtimeTargetThreadId": _text(state.get("runtimeTargetThreadId")),
        "runtimeReportPath": _text(state.get("runtimeReportPath")),
        "patchedAsarSha256": _hash(state.get("patchedAsarSha256")),
        "effectiveAsarHash": _hash(state.get("effectiveAsarHash")),
        "initialTurns": _positive_integer(state.get("initialTurns"), 1),
        "userPageTurns": _positive_integer(state.get("userPageTurns"), 3),
        "preparedAt": _text(state.get("preparedAt")),
        "finishedAt": _text(state.get("finishedAt")),
        "error": _text(state.get("error")),
    }


def detect_pagination_patch_status(
    desktop_install: Mapping[str, Any] | None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = environment or os.environ
    current_path = _text((desktop_install or {}).get("path"))
    current_package = _text((desktop_install or {}).get("packageFullName")) or _package_full_name_from_path(current_path)
    current_version = _text((desktop_install or {}).get("version"))
    modern_contract = (_version_parts(current_version) or (999,)) >= (26, 901, 6511, 0)
    contract_version = 7 if modern_contract else required_feature_contract_version
    ready_state, ready_path, ready_error = _read_first_state(
        _candidate_paths("CODEX_PAGINATION_READY_STATE", "ready.json", environment)
    )
    installed_state, installed_path, installed_error = _read_first_state(
        _candidate_paths("CODEX_PAGINATION_INSTALLED_STATE", "installed.json", environment)
    )
    ready = _state_view(ready_state, ready_path)
    installed = _state_view(installed_state, installed_path)
    receipt_observed_at = _text((installed or {}).get("runtimeValidatedAt"))

    state = "missing"
    action = "prepare"
    update_available = False
    reason = "No prepared or installed pagination patch evidence was found."
    if ready_error or installed_error:
        state = "invalid"
        action = "verify"
        reason = f"Pagination state evidence cannot be parsed: {ready_error or installed_error}"
    elif not current_package:
        state = "unknown"
        action = "verify"
        reason = "The active Codex package identity is unavailable."
    else:
        installed_target = _text((installed or {}).get("targetPackageFullName"))
        ready_target = _text((ready or {}).get("targetPackageFullName"))
        installed_hash = _hash((installed or {}).get("effectiveAsarHash"))
        expected_hash = _hash((installed or {}).get("patchedAsarSha256"))
        installed_receipt_matches = bool(
            installed
            and installed_target == current_package
            and installed.get("status") == "success"
            and installed.get("packageStatus") == "Ok"
            and installed_hash
            and installed_hash == expected_hash
            and installed.get("featureContractVersion", 0) >= contract_version
            and (not modern_contract or (installed.get("freshTurnReadItemBoundary") is True and installed.get("bundleRuntimeAuditPassed") is True))
            and installed.get("initialTurnItemsPaginated") is True
            and installed.get("initialTurnItemPageSize") == 100
            and installed.get("promptRailLoadedTurnsOnly") is True
            and installed.get("periodicStatusMetadataOnly") is True
            and installed.get("priorityFeedMetadataOnly") is True
            and installed.get("passiveTabRestorationLoadedTurnsOnly") is True
            and installed.get("runtimeAcceptanceRequired") is True
        )
        runtime_status = _text((installed or {}).get("runtimeStatus")).lower()
        receipt_runtime_passed = bool(
            runtime_status == "passed"
            and (installed or {}).get("runtimeObservationSeconds", 0) >= 180
            and (installed or {}).get("runtimeBackgroundTurnListRequests") == 0
        )
        ready_compatible = bool(
            ready
            and ready_target == current_package
            and ready.get("status") == "ready"
            and ready.get("featureContractVersion", 0) >= contract_version
            and (not modern_contract or (ready.get("freshTurnReadItemBoundary") is True and ready.get("bundleRuntimeAuditPassed") is True))
            and ready.get("initialTurnItemsPaginated") is True
            and ready.get("initialTurnItemPageSize") == 100
            and ready.get("promptRailLoadedTurnsOnly") is True
            and ready.get("periodicStatusMetadataOnly") is True
            and ready.get("priorityFeedMetadataOnly") is True
            and ready.get("passiveTabRestorationLoadedTurnsOnly") is True
            and ready.get("runtimeAcceptanceRequired") is True
        )
        ready_version = _version_parts((ready or {}).get("packageVersion"))
        installed_version = _version_parts((installed or {}).get("packageVersion"))
        update_available = bool(
            ready_compatible
            and ready_version is not None
            and installed_version is not None
            and ready_version > installed_version
        )
        if installed_receipt_matches and update_available:
            state = "ready"
            action = "install"
            reason = "A newer patch receipt satisfies the package contract; current runtime state has not been verified."
        elif installed_receipt_matches and receipt_runtime_passed:
            # Receipt contents cannot attest a live process or its currently loaded bytes.
            state = "unverified"
            action = "verify"
            reason = (
                f"The installation receipt reports passed runtime acceptance at {receipt_observed_at or 'an unknown time'}; "
                "current PID/creation time, Optional registration, and loaded ASAR bytes have not been verified."
            )
        elif installed_receipt_matches and runtime_status == "failed":
            state = "invalid"
            action = "verify"
            reason = "The installation receipt reports failed long-thread runtime acceptance; current runtime state has not been verified."
        elif installed_receipt_matches:
            state = "unverified"
            action = "verify"
            reason = "The installation receipt matches the package contract, but does not report complete runtime acceptance; current runtime state has not been verified."
        elif ready_compatible:
            state = "ready"
            action = "install"
            reason = "A prepared patch receipt satisfies the package contract; current runtime state has not been verified."
        elif (installed_target and installed_target != current_package) or (ready_target and ready_target != current_package):
            state = "stale"
            action = "rebuild"
            reason = "Codex was updated after the pagination patch was prepared or installed."
        elif installed or ready:
            state = "invalid"
            action = "verify"
            reason = "Pagination evidence exists but its status or effective ASAR hash does not satisfy the patch contract."

    return {
        "schemaVersion": pagination_state_schema_version,
        "state": state,
        "action": action,
        "reason": reason,
        "checkedAtMs": int(time.time() * 1000),
        "evidenceScope": "receipt_only",
        "receiptObservedAt": receipt_observed_at,
        "currentRuntimeVerified": False,
        "current": {
            "packageFullName": current_package,
            "version": current_version,
            "path": current_path,
        },
        "policy": {
            "requiredFeatureContractVersion": contract_version,
            "initialTurns": 1,
            "initialTurnItemsPaginated": True,
            "initialTurnItemPageSize": 100,
            "userPageTurns": 3,
            "oneRequestPerTopGesture": True,
            "promptRailLoadedTurnsOnly": True,
            "periodicStatusMetadataOnly": True,
            "priorityFeedMetadataOnly": True,
            "passiveTabRestorationLoadedTurnsOnly": True,
            "runtimeObservationSeconds": 180,
        },
        "updateAvailable": update_available,
        "prepared": ready,
        "installed": installed,
    }
