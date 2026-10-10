"""Central storage admission and lifecycle registry for Codex build artifacts."""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from filelock import FileLock
import psutil

from codex_directory_layout import generated_roots, layout_violations


gib = 1024**3
workspace_root = Path(__file__).resolve().parents[1]
artifact_root = Path(r"D:\ZFiles\CXProject\Codex")
backup_root = Path(r"D:\Backup")
registry_path = workspace_root / "output" / "artifact-registry.json"
build_name_pattern = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{2,63}")
intermediate_names = (
    "stage",
    "candidate-direct.app.asar",
    "staging",
    "verify",
    "package-verify",
    "patched-asar-verify",
    "asar-verify",
    "msix-asar-verify",
    "repacked-verify",
    "msix-module-verify",
    "legacy-dist",
    "legacy-work",
    "legacy-spec",
    "sidecar",
    "tmp",
    "superseded-resources",
    "superseded-release",
    "superseded-frontend-rollback",
    "superseded-frontend-stage",
    "previous-web-resource",
    "patched.app.asar",
    "native-target",
)

# Peak/live limits. Builds are rejected before writing when a reservation would
# cross a limit; changing directory names cannot bypass these counters.
scope_budgets = {
    "artifact-store": 16 * gib,
    "workspace-generated": 8 * gib,
    "codex-backups": 8 * gib,
}
managed_budget = 24 * gib
default_reservation = 8 * gib
retired_budget = 4 * gib
cache_budget = 10 * gib
inventory_budget = 2 * gib
free_floor = 200 * gib


def build_container() -> Path:
    return artifact_root / "b"


def workspace_generated_roots() -> tuple[Path, ...]:
    roots = list(generated_roots(workspace_root))
    roots.extend((workspace_root / "codex_home_manager" / "src-tauri" / "target",
                  workspace_root / "codex-home-manager-public" / ".tmp",
                  workspace_root / "codex-home-manager-public" / ".wrangler",
                  workspace_root / "codex-home-manager-public" / "dist",
                  workspace_root / "codex-home-manager-public" / "node_modules"))
    return tuple(roots)


def codex_backup_roots() -> tuple[Path, ...]:
    if not backup_root.is_dir():
        return ()
    return tuple(
        Path(entry.path)
        for entry in os.scandir(backup_root)
        if entry.name.casefold().startswith("codex") and not entry.is_symlink()
    )


def _allocation(path: Path, info: os.stat_result) -> int:
    if stat.S_ISDIR(info.st_mode):
        return 0
    if os.name != "nt":
        return getattr(info, "st_blocks", 0) * 512 or info.st_size
    high = ctypes.c_ulong()
    ctypes.set_last_error(0)
    low = ctypes.windll.kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high)) & 0xFFFFFFFF
    if low == 0xFFFFFFFF and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    return (high.value << 32) | low


def _extended_path(path: Path) -> Path:
    value = str(Path(path).absolute())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return Path(value)
    return Path("\\\\?\\" + value)


def _is_pytest_current_alias(path: Path) -> bool:
    if not path.name.endswith("current") or not path.is_symlink():
        return False
    try:
        parent = _extended_path(path.parent).resolve(strict=True)
        target = _extended_path(path).resolve(strict=True)
    except OSError:
        return False
    try:
        relative_parent = parent.relative_to(_extended_path(workspace_root).resolve(strict=True))
    except OSError:
        return False
    except ValueError:
        try:
            relative_parent = parent.relative_to(_extended_path(artifact_root).resolve(strict=True))
        except (OSError, ValueError):
            return False
        if (len(relative_parent.parts) < 3 or relative_parent.parts[0] != "b"
                or not build_name_pattern.fullmatch(relative_parent.parts[1]) or relative_parent.parts[2] != "tmp"):
            return False
        return (target.parent == parent and target.is_dir() and not target.is_symlink()
                and re.fullmatch(re.escape(path.name[:-7]) + r"\d+", target.name) is not None)
    for index in range(len(relative_parent.parts) - 1):
        if relative_parent.parts[index:index + 2] == (".tmp", "pytest"):
            pytest_root = _extended_path(workspace_root).resolve(strict=True).joinpath(*relative_parent.parts[:index + 2])
            if target.is_relative_to(pytest_root) and target.is_dir():
                return True
    # pytest also creates sibling aliases in explicitly selected basetemp roots.
    return (".tmp" in relative_parent.parts and target.parent == parent
            and re.fullmatch(re.escape(path.name[:-7]) + r"\d+", target.name) is not None
            and target.is_dir() and not target.is_symlink())


def tree_usage(roots: tuple[Path, ...] | list[Path]) -> dict[str, int]:
    logical = allocated = files = directories = 0
    identities: set[tuple[int, int]] = set()
    pending = [_extended_path(path) for path in roots if path.exists()]
    while pending:
        current = pending.pop()
        info = current.stat(follow_symlinks=False)
        if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError(f"Reparse entry in managed storage: {current}")
        if current.is_file():
            identity = (info.st_dev, info.st_ino)
            if identity not in identities:
                identities.add(identity)
                logical += info.st_size
                allocated += _allocation(current, info)
                files += 1
            continue
        directories += 1
        with os.scandir(current) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                entry_info = entry_path.stat(follow_symlinks=False)
                if getattr(entry_info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    if _is_pytest_current_alias(entry_path):
                        continue
                    raise RuntimeError(f"Reparse entry in managed storage: {entry.path}")
                if stat.S_ISDIR(entry_info.st_mode):
                    pending.append(entry_path)
                elif stat.S_ISREG(entry_info.st_mode):
                    identity = (entry_info.st_dev, entry_info.st_ino)
                    if identity not in identities:
                        identities.add(identity)
                        logical += entry_info.st_size
                        allocated += _allocation(entry_path, entry_info)
                        files += 1
                else:
                    raise RuntimeError(f"Unsupported managed-storage entry: {entry.path}")
    return {"logicalBytes": logical, "allocatedBytes": allocated, "files": files, "directories": directories}


def tree_bytes(roots):
    """Compatibility API used by the retirement tool; returns logical bytes."""
    return tree_usage(list(roots))["logicalBytes"]


def intermediate_bytes(root: Path) -> int:
    return tree_usage([root / name for name in intermediate_names])["allocatedBytes"]


def _artifact_top_level_violations() -> list[str]:
    if not artifact_root.is_dir():
        return []
    allowed = {"b", "cache", "inventory", "retired"}
    return [str(Path(entry.path)) for entry in os.scandir(artifact_root) if entry.name not in allowed]


def storage_snapshot() -> dict[str, object]:
    scopes = {
        "artifact-store": tree_usage([artifact_root]),
        "workspace-generated": tree_usage(list(workspace_generated_roots())),
        "codex-backups": tree_usage(list(codex_backup_roots())),
    }
    violations = [*layout_violations(), *_artifact_top_level_violations()]
    for name, usage in scopes.items():
        if usage["allocatedBytes"] > scope_budgets[name]:
            violations.append(f"{name} exceeds {scope_budgets[name] / gib:.0f} GiB")
    total = sum(value["allocatedBytes"] for value in scopes.values())
    if total > managed_budget:
        violations.append(f"managed total exceeds {managed_budget / gib:.0f} GiB")
    for name, budget in (("retired", retired_budget), ("cache", cache_budget), ("inventory", inventory_budget)):
        usage = tree_usage([artifact_root / name])
        if usage["allocatedBytes"] > budget:
            violations.append(f"{name} exceeds {budget / gib:.0f} GiB")
    free_bytes = shutil.disk_usage(artifact_root.parent if artifact_root.parent.exists() else workspace_root).free
    if free_bytes < free_floor:
        violations.append(f"D free-space is below {free_floor / gib:.0f} GiB")
    return {
        "schemaVersion": 2,
        "capturedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scopes": scopes,
        "managedAllocatedBytes": total,
        "freeBytes": free_bytes,
        "violations": violations,
    }


def _read_registry() -> dict[str, object]:
    if not registry_path.exists():
        return {"schemaVersion": 2, "runs": {}}
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    if raw.get("schemaVersion") == 2:
        return raw
    return {"schemaVersion": 2, "runs": {}, "migratedSchema1Builds": raw.get("builds", {})}


def _write_registry(registry: dict[str, object]) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = registry_path.with_name(registry_path.name + f".{os.getpid()}.pending")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, registry_path)


def _owner() -> dict[str, object]:
    process = psutil.Process().parent()
    return {"ownerPid": process.pid, "ownerCreated": process.create_time()}


def _validate_build_root(root: Path) -> Path:
    root = Path(os.path.abspath(root))
    container = Path(os.path.abspath(build_container()))
    if root.parent != container or not build_name_pattern.fullmatch(root.name):
        raise RuntimeError(f"Build root must be one short-path run directly under {container}")
    if root.resolve(strict=False) != root:
        raise RuntimeError("Build root must not traverse a link or alias")
    return root


def _validate_verified_report(root: Path, report_path: Path | None) -> dict[str, object]:
    report = report_path or root / "full-package-build-report.json"
    if not report.is_file() or report.stat().st_size > 2 * 1024**2:
        raise RuntimeError("Cannot register a build without a bounded verification report")
    value = json.loads(report.read_text(encoding="utf-8-sig"))
    if value.get("passed") is not True:
        raise RuntimeError("Cannot register an unverified release")
    retained = value.get("retainedFiles")
    if retained is None:
        retained = [value.get("packagePath")]
    if not isinstance(retained, list) or not retained:
        raise RuntimeError("Verification report has no retained artifacts")
    retained_paths = []
    for item in retained:
        path = Path(item)
        if not path.is_absolute():
            path = root / path
        path = Path(os.path.abspath(path))
        if path == root or not path.is_relative_to(root) or not path.is_file():
            raise RuntimeError(f"Retained artifact escapes its run root: {path}")
        retained_paths.append(str(path))
    return {"report": str(report), "retainedFiles": retained_paths}


def _assert_admission(snapshot: dict[str, object], reservation: int, temporary_budget_bytes: int = 0, *, managed_ceiling_bytes: int | None = None) -> None:
    # Backup retention is an independent, disposable recovery concern. A
    # pre-existing backup-only overage remains visible in the audit, but must
    # not force an unrelated build to rewrite or relocate D:\Backup.
    blocking_violations = [
        violation
        for violation in snapshot["violations"]
        if not violation.startswith("codex-backups exceeds ")
        and not (temporary_budget_bytes and (violation.startswith("artifact-store exceeds ") or violation.startswith("managed total exceeds ")))
    ]
    if blocking_violations:
        raise RuntimeError("Storage policy is already out of bounds: " + "; ".join(blocking_violations))
    artifact_bytes = snapshot["scopes"]["artifact-store"]["allocatedBytes"]
    if artifact_bytes + reservation > scope_budgets["artifact-store"] + temporary_budget_bytes:
        raise RuntimeError("Artifact-store reservation would exceed its 16 GiB budget")
    managed_ceiling = managed_budget + temporary_budget_bytes if managed_ceiling_bytes is None else managed_ceiling_bytes
    if snapshot["managedAllocatedBytes"] + reservation > managed_ceiling:
        limit = "24" if managed_ceiling_bytes is None else f"{managed_ceiling / gib:g}"
        raise RuntimeError(f"Managed-storage reservation would exceed its {limit} GiB budget")
    if snapshot["freeBytes"] - reservation < free_floor:
        raise RuntimeError("D: would fall below the 200 GiB free-space floor")


cockpit_budget_grants = {
    "01a11dff-fdf3-7c30-b673-2fe67a365df3": ("1.0.31", "cockpit-prompt-1031", int(6.5 * gib), 25 * gib, 40 * gib),
    "request_user_input_async/call_934d4dca7a3a43e2942a7fe3ccdc0c14/0": ("1.0.32", "cockpit-prompt-1032", 2 * gib, 30 * gib, 53 * gib),
    "request_user_input_async/call_db06005aa1a047768ff1d0775a0c008a/0": ("1.0.33", "cockpit-classification-1033", 2 * gib, 30 * gib, 53 * gib),
    "request_user_input_async/call_18MgAYXSEqKSwWkoiofrkAbx/0": ("1.0.34", "cockpit-icon-1034", 2 * gib, 31 * gib, 55 * gib),
}


def assert_cockpit_budget(snapshot: dict[str, object], entry: dict[str, object], reservation: int = 0) -> None:
    grant = cockpit_budget_grants.get(entry.get("cockpitBudgetAuthorization"))
    if grant is None:
        raise RuntimeError("Cockpit budget requires this repair's explicit authorization")
    _, _, allowance, artifact_ceiling, managed_ceiling = grant
    _assert_admission(snapshot, reservation, artifact_ceiling - scope_budgets["artifact-store"], managed_ceiling_bytes=managed_ceiling)
    baseline = entry["admissionSnapshot"]
    growth = sum(max(0, snapshot["scopes"][name]["allocatedBytes"] - baseline["scopes"][name]["allocatedBytes"]) for name in scope_budgets)
    if growth + reservation > allowance:
        raise RuntimeError(f"Cockpit repair exceeded its {allowance / gib:g} GiB incremental allowance")


migration_rebuild_authorization = "request_user_input_async/call_0bb3f8fb6dc0441db2bae448cfcd3530/0"
corrected_native_package_authorization = "request_user_input_async/call_9d178e25b66f4851b77ae7a58d892a89/0"
shared_storage_calibration_authorization = "request_user_input_async/call_99692d2550c54c898fa3234ac5f92398/0"
shared_backup_adjustment_bytes = 7_710_429_184


def _shared_storage_calibrated(entry: dict[str, object]) -> bool:
    calibration = entry.get("sharedStorageCalibration")
    if calibration is None:
        return False
    if (calibration.get("authorization") != shared_storage_calibration_authorization
            or calibration.get("externalBackupIncrementBytes") != shared_backup_adjustment_bytes):
        raise RuntimeError("Shared storage calibration differs from its explicit bounded grant")
    return True


def assert_migration_rebuild_budget(snapshot: dict[str, object], baseline: dict[str, object], reservation: int = 0, *, entry: dict[str, object] | None = None) -> None:
    calibrated = _shared_storage_calibrated(entry or {})
    if snapshot["scopes"]["artifact-store"]["allocatedBytes"] + reservation > 27 * gib:
        raise RuntimeError("Migration rebuild would exceed its 27 GiB artifact ceiling")
    _assert_admission(snapshot, reservation, 11 * gib, managed_ceiling_bytes=(51 if calibrated else 43) * gib)
    growth = sum(max(0, snapshot["scopes"][name]["allocatedBytes"] - baseline["scopes"][name]["allocatedBytes"]) for name in scope_budgets)
    if calibrated:
        backup_growth = max(0, snapshot["scopes"]["codex-backups"]["allocatedBytes"] - baseline["scopes"]["codex-backups"]["allocatedBytes"])
        growth -= min(shared_backup_adjustment_bytes, backup_growth)
    if growth + reservation > int(3.5 * gib):
        raise RuntimeError("Migration rebuild and package exceeded their shared 3.5 GiB incremental allowance")


def assert_native_package_budget(snapshot: dict[str, object], entry: dict[str, object], reservation: int = 0) -> None:
    calibrated = _shared_storage_calibrated(entry)
    if calibrated and (entry.get("version") != "26.930.3748.19" or entry.get("packageBudgetAuthorization") != corrected_native_package_authorization):
        raise RuntimeError("Shared storage calibration is restricted to the corrected native package")
    if entry.get("packageBudgetAuthorization") == migration_rebuild_authorization:
        if entry.get("version") != "26.930.3748.18" or entry.get("kind") != "codex-package":
            raise RuntimeError("Migration package requires its explicitly authorized release identity")
        assert_migration_rebuild_budget(snapshot, entry["migrationBudgetBaseline"], reservation)
        return
    corrected = entry.get("packageBudgetAuthorization") == corrected_native_package_authorization
    expected_authorization = corrected_native_package_authorization if corrected else "request_user_input_async/call_80c6f71f18d849e2ad3d567782c0cf00/0"
    if (entry.get("packageBudgetAuthorization") != expected_authorization
            or entry.get("version") != ("26.930.3748.19" if corrected else "26.930.3748.17") or entry.get("kind") != "codex-package"):
        raise RuntimeError("Native package requires its own explicit packaging authorization")
    _assert_admission(snapshot, reservation, (13 if corrected else 8) * gib, managed_ceiling_bytes=(54 if calibrated else 46 if corrected else 40) * gib)
    baseline = entry["admissionSnapshot"]
    growth = sum(max(0, snapshot["scopes"][name]["allocatedBytes"] - baseline["scopes"][name]["allocatedBytes"]) for name in scope_budgets)
    if growth + reservation > int(2.5 * gib):
        raise RuntimeError("Native package exceeded its 2.5 GiB incremental allowance")


def update(
    root: Path,
    version: str,
    phase: str,
    *,
    reservation_bytes: int = default_reservation,
    kind: str = "codex-package",
    report_path: Path | None = None,
    temporary_budget_bytes: int = 0,
    authorization_item_id: str | None = None,
    resume: bool = False,
    remote_receipt_bytes: int = 0,
    package_budget_authorization: str | None = None,
    managed_budget_authorization: str | None = None,
    cockpit_budget_authorization: str | None = None,
    local_native_bytes: int = 0,
    shared_storage_authorization: str | None = None,
) -> dict[str, object]:
    root = _validate_build_root(root)
    if reservation_bytes <= 0 or reservation_bytes > 12 * gib:
        raise RuntimeError("Reservation must be between 1 byte and 12 GiB")
    if shared_storage_authorization and (
            shared_storage_authorization != shared_storage_calibration_authorization or phase != "begin"
            or not ((kind == "native-cli" and version == "0.161.0-mcp-lazy-r1" and root.name == "0161-mcp-lazy-r1"
                     and resume and remote_receipt_bytes and not local_native_bytes and managed_budget_authorization == migration_rebuild_authorization)
                    or (kind == "codex-package" and version == "26.930.3748.19" and root.name == "2630-native-mcp-p19"
                        and package_budget_authorization == corrected_native_package_authorization))):
        raise RuntimeError("Shared storage calibration is restricted to the existing native receipt and corrected .19 package")
    cockpit_grant = cockpit_budget_grants.get(cockpit_budget_authorization)
    if cockpit_budget_authorization and (
            cockpit_grant is None
            or phase != "begin" or kind != "cockpit" or version != cockpit_grant[0]
            or root.name != cockpit_grant[1]
            or reservation_bytes != (min(cockpit_grant[2], gib) if version == "1.0.33" else cockpit_grant[2])
            or temporary_budget_bytes or package_budget_authorization or managed_budget_authorization or remote_receipt_bytes or local_native_bytes):
        raise RuntimeError("Cockpit budget authorization is restricted to this prompt repair")
    if resume and phase != "begin":
        raise RuntimeError("Resume is only valid for a native begin")
    if managed_budget_authorization and (
            managed_budget_authorization not in ("request_user_input_async/call_22cc0a12e7214dad9adbf657d052a192/0", migration_rebuild_authorization)
            or phase != "begin" or kind != "native-cli" or version != "0.161.0-mcp-lazy-r1"
            or root.name != "0161-mcp-lazy-r1" or not resume or not (remote_receipt_bytes or local_native_bytes)
            or temporary_budget_bytes != 8 * gib or reservation_bytes != 8 * gib
            or package_budget_authorization):
        raise RuntimeError("Managed budget authorization is restricted to this existing native resume")
    if local_native_bytes and (
            managed_budget_authorization != migration_rebuild_authorization or remote_receipt_bytes
            or not 0 < local_native_bytes <= 2 * gib):
        raise RuntimeError("Local native continuation requires the migration grant and at most 2 GiB")
    if package_budget_authorization:
        migration_package = version == "26.930.3748.18"
        corrected_native_package = version == "26.930.3748.19"
        native_package = version in ("26.930.3748.17", "26.930.3748.19")
        expected_root = "2630-native-mcp-p19" if corrected_native_package else "2630-browser-crash-p18" if migration_package else "2630-native-mcp-p17" if native_package else "2630-background-retry-p16"
        expected_authorization = corrected_native_package_authorization if corrected_native_package else migration_rebuild_authorization if migration_package else "request_user_input_async/call_80c6f71f18d849e2ad3d567782c0cf00/0"
        if (phase != "begin" or kind != "codex-package" or version not in ("26.930.3748.16", "26.930.3748.17", "26.930.3748.18", "26.930.3748.19")
                or root.name != expected_root or resume or temporary_budget_bytes or managed_budget_authorization
                or reservation_bytes > int(2.5 * gib)
                or not re.fullmatch(r"request_user_input_async/call_[0-9a-f]{32}/0", package_budget_authorization)
                or (package_budget_authorization == corrected_native_package_authorization and not corrected_native_package)
                or ((native_package or migration_package) and package_budget_authorization != expected_authorization)):
            raise RuntimeError("Package budget authorization is restricted to this 2.5 GiB release")
        settings_name = f"browser-crash-build-{version}.json" if migration_package else f"native-mcp-build-{version}.json" if native_package else "background-retry-build-26.930.3748.16.json"
        settings = json.loads((workspace_root / "config" / settings_name).read_text(encoding="utf-8"))
        expected_kind = "browser-navigation-crash-frontend-only" if migration_package else "native-optional-mcp-lazy-startup" if native_package else "background-retry-frontend-only"
        if (settings["repairKind"] != expected_kind or bool(settings.get("nativeRuntimeManifestPath")) != native_package
                or Path(settings["buildRoot"]).resolve() != root):
            raise RuntimeError("Package budget authorization cannot fund a native replacement or another release mode")
    if remote_receipt_bytes and (phase != "begin" or kind != "native-cli" or not resume or not temporary_budget_bytes or not authorization_item_id or not 0 < remote_receipt_bytes <= 2 * gib):
        raise RuntimeError("Remote receipt reservation requires the same authorized native resume and at most 2 GiB")
    if temporary_budget_bytes:
        if phase != "begin" or kind != "native-cli" or not 0 < temporary_budget_bytes <= 8 * gib or not authorization_item_id:
            raise RuntimeError("Temporary budget requires a native-cli begin, at most 8 GiB, and explicit authorization identity")
        uuid.UUID(authorization_item_id)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(registry_path) + ".lock", timeout=15):
        registry = _read_registry()
        runs = registry["runs"]
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if phase == "begin":
            previous_entry = runs.get(str(root))
            package_retry = bool(package_budget_authorization == corrected_native_package_authorization
                                 and previous_entry and previous_entry.get("phase") == "failed" and not root.exists()
                                 and all(previous_entry.get(key) == expected for key, expected in (
                                     ("kind", kind), ("version", version), ("reservationBytes", reservation_bytes),
                                     ("packageBudgetAuthorization", package_budget_authorization)))
                                 and previous_entry.get("sharedStorageCalibration", {}).get("authorization") == shared_storage_authorization)
            if package_retry:
                try:
                    previous_owner = psutil.Process(previous_entry["ownerPid"])
                    if previous_owner.create_time() == previous_entry["ownerCreated"]:
                        raise RuntimeError("Cannot retry while the previous package owner is alive")
                except psutil.NoSuchProcess:
                    pass
            if resume:
                if not root.is_dir() or not previous_entry or previous_entry.get("phase") not in ("begin", "failed") or (kind != "native-cli" and not cockpit_budget_authorization) or any(previous_entry.get(key) != expected for key, expected in (("kind", kind), ("version", version), ("budgetAuthorizationItemId", authorization_item_id), ("temporaryBudgetBytes", temporary_budget_bytes or None), ("reservationBytes", reservation_bytes), ("cockpitBudgetAuthorization", cockpit_budget_authorization))):
                    raise RuntimeError("Resume requires the same admitted native build and budget authorization")
                try:
                    previous_owner = psutil.Process(previous_entry["ownerPid"])
                    if previous_owner.create_time() == previous_entry["ownerCreated"]:
                        raise RuntimeError("Cannot resume while the previous build owner is alive")
                except psutil.NoSuchProcess:
                    pass
            elif root.exists():
                raise RuntimeError("Refusing existing build directory")
            if temporary_budget_bytes and any(previous.get("budgetAuthorizationItemId") == authorization_item_id and previous_root != str(root) for previous_root, previous in runs.items()):
                raise RuntimeError("Temporary budget authorization is already bound to another build reservation")
            if package_budget_authorization and any(previous.get("packageBudgetAuthorization") == package_budget_authorization
                                                   and not (package_retry and previous_root == str(root)) for previous_root, previous in runs.items()):
                raise RuntimeError("Package budget authorization is already bound")
            if managed_budget_authorization and any(previous.get("managedBudgetAuthorization") == managed_budget_authorization and previous_root != str(root) for previous_root, previous in runs.items()):
                raise RuntimeError("Managed budget authorization is already bound to another build")
            if cockpit_budget_authorization and any(previous.get("cockpitBudgetAuthorization") == cockpit_budget_authorization and previous_root != str(root) for previous_root, previous in runs.items()):
                raise RuntimeError("Cockpit budget authorization is already bound to another build")
            snapshot = storage_snapshot()
            calibration = ({"authorization": shared_storage_authorization, "externalBackupIncrementBytes": shared_backup_adjustment_bytes}
                           if shared_storage_authorization else None)
            migration_grant = migration_rebuild_authorization in (managed_budget_authorization, package_budget_authorization)
            migration_baseline = None
            if migration_grant:
                baselines = [previous["migrationBudgetBaseline"] for previous in runs.values() if previous.get("migrationBudgetAuthorization") == migration_rebuild_authorization]
                if baselines and any(value != baselines[0] for value in baselines):
                    raise RuntimeError("Shared migration budget baseline is inconsistent")
                if package_budget_authorization and not baselines:
                    raise RuntimeError("Migration package must share its admitted native rebuild baseline")
                migration_baseline = json.loads(json.dumps(baselines[0] if baselines else snapshot))
            active_reservations = 0
            for previous in runs.values():
                if previous.get("phase") != "begin":
                    continue
                try:
                    owner = psutil.Process(previous["ownerPid"])
                    active = owner.create_time() == previous["ownerCreated"]
                except (KeyError, TypeError, ValueError, psutil.Error):
                    active = False
                if active:
                    active_reservations += int(previous.get("reservationBytes", default_reservation))
            existing_usage = tree_usage([root])["allocatedBytes"] if resume else 0
            remaining_reservation = max(0, reservation_bytes - existing_usage) if resume else reservation_bytes
            if remote_receipt_bytes:
                remaining_reservation = min(remaining_reservation, remote_receipt_bytes)
            if local_native_bytes:
                remaining_reservation = min(remaining_reservation, local_native_bytes)
            if migration_grant:
                if kind == "native-cli" and existing_usage > reservation_bytes:
                    raise RuntimeError("Existing native candidate exceeds its original 8 GiB root limit")
                assert_migration_rebuild_budget(snapshot, migration_baseline, remaining_reservation + active_reservations,
                                               entry={"sharedStorageCalibration": calibration} if calibration else None)
            elif cockpit_budget_authorization:
                baseline = json.loads(json.dumps(previous_entry["admissionSnapshot"] if resume else snapshot))
                consumed = sum(max(0, snapshot["scopes"][name]["allocatedBytes"] - baseline["scopes"][name]["allocatedBytes"]) for name in scope_budgets)
                remaining_reservation = max(0, reservation_bytes - consumed)
                assert_cockpit_budget(snapshot, {"cockpitBudgetAuthorization": cockpit_budget_authorization, "admissionSnapshot": baseline}, remaining_reservation + active_reservations)
            elif package_budget_authorization:
                package_baseline = previous_entry["admissionSnapshot"] if package_retry else snapshot
                if package_retry:
                    consumed = sum(max(0, snapshot["scopes"][name]["allocatedBytes"] - package_baseline["scopes"][name]["allocatedBytes"]) for name in scope_budgets)
                    remaining_reservation = max(0, reservation_bytes - consumed)
                total_reservation = remaining_reservation + active_reservations
                if native_package:
                    assert_native_package_budget(snapshot, {"packageBudgetAuthorization": package_budget_authorization,
                                                "version": version, "kind": kind, "admissionSnapshot": package_baseline,
                                                **({"sharedStorageCalibration": calibration} if calibration else {})}, total_reservation)
                else:
                    _assert_admission(snapshot, total_reservation, 9 * gib)
                    if snapshot["scopes"]["artifact-store"]["allocatedBytes"] + total_reservation > 18 * gib:
                        raise RuntimeError("Authorized package would exceed the 18 GiB artifact ceiling")
            elif managed_budget_authorization:
                if existing_usage > reservation_bytes:
                    raise RuntimeError("Existing native candidate exceeds its original 8 GiB root limit")
                _assert_admission(snapshot, remaining_reservation + active_reservations, temporary_budget_bytes, managed_ceiling_bytes=35 * gib)
            else:
                _assert_admission(snapshot, remaining_reservation + active_reservations, temporary_budget_bytes)
            entry = {
                "version": version,
                "kind": kind,
                "phase": phase,
                "reservationBytes": reservation_bytes,
                "createdAt": now,
                "updatedAt": now,
                "admissionSnapshot": package_baseline if package_retry else snapshot,
                **({"remoteReceiptBytes": remote_receipt_bytes, "remoteReceiptCeilingBytes": existing_usage + remaining_reservation} if remote_receipt_bytes else {}),
                **({"localNativeBytes": local_native_bytes, "localNativeCeilingBytes": existing_usage + remaining_reservation} if local_native_bytes else {}),
                **_owner(),
            }
            if calibration:
                entry["sharedStorageCalibration"] = calibration
            if temporary_budget_bytes:
                entry["temporaryBudgetBytes"] = temporary_budget_bytes
                entry["budgetAuthorizationItemId"] = authorization_item_id
            if package_budget_authorization:
                entry.update(packageBudgetAuthorization=package_budget_authorization,
                             packageReservationCeilingBytes=int(2.5 * gib),
                             packageArtifactCeilingBytes=(29 if corrected_native_package else 27 if migration_package else 24 if native_package else 18) * gib,
                             packageManagedCeilingBytes=(54 if calibration else 46 if corrected_native_package else 43 if migration_package else 40 if native_package else 33) * gib)
            if managed_budget_authorization:
                entry.update(managedBudgetAuthorization=managed_budget_authorization, managedCeilingBytes=(43 if migration_grant else 35) * gib)
            if migration_grant:
                entry.update(migrationBudgetAuthorization=migration_rebuild_authorization, migrationBudgetBaseline=migration_baseline,
                             artifactCeilingBytes=27 * gib, managedCeilingBytes=(51 if calibration else 43) * gib, sharedGrowthCeilingBytes=int(3.5 * gib))
            if cockpit_budget_authorization:
                entry.update(cockpitBudgetAuthorization=cockpit_budget_authorization, admissionSnapshot=baseline,
                             artifactCeilingBytes=cockpit_grant[3], managedCeilingBytes=cockpit_grant[4])
        else:
            if str(root) not in runs:
                raise RuntimeError("Build was not admitted by the lifecycle registry")
            entry = dict(runs[str(root)])
            if entry.get("cockpitBudgetAuthorization") and (kind != entry["kind"] or version != entry["version"]):
                raise RuntimeError("Cockpit budget finalization must match its admitted identity")
            if entry.get("packageBudgetAuthorization") and (kind != entry["kind"] or version != entry["version"]):
                raise RuntimeError("Package budget finalization must match its admitted identity")
            if phase == "verified":
                entry.update(_validate_verified_report(root, report_path))
                requires_budget_snapshot = (entry.get("cockpitBudgetAuthorization") or entry.get("migrationBudgetAuthorization")
                                            or (entry.get("packageBudgetAuthorization") and entry["version"] in ("26.930.3748.17", "26.930.3748.18", "26.930.3748.19")))
                budget_snapshot = storage_snapshot() if requires_budget_snapshot else None
                if entry.get("cockpitBudgetAuthorization"):
                    assert_cockpit_budget(budget_snapshot, entry)
                if entry.get("packageBudgetAuthorization") and entry["version"] in ("26.930.3748.17", "26.930.3748.18", "26.930.3748.19"):
                    assert_native_package_budget(budget_snapshot, entry)
                if entry.get("migrationBudgetAuthorization"):
                    if kind != entry["kind"] or version != entry["version"]:
                        raise RuntimeError("Migration rebuild finalization must match its admitted identity")
                    assert_migration_rebuild_budget(budget_snapshot, entry["migrationBudgetBaseline"], entry=entry)
                    if entry.get("packageBudgetAuthorization") == migration_rebuild_authorization:
                        entry.update(packageArtifactCeilingBytes=27 * gib, packageManagedCeilingBytes=43 * gib)
            intermediate = intermediate_bytes(root)
            root_usage = tree_usage([root])
            if phase == "verified" and entry.get("packageBudgetAuthorization") and root_usage["allocatedBytes"] > entry["reservationBytes"]:
                raise RuntimeError("Frontend package exceeded its authorized incremental allocation")
            if entry.get("managedBudgetAuthorization") and root_usage["allocatedBytes"] > entry["reservationBytes"]:
                raise RuntimeError("Native candidate exceeded its original 8 GiB root limit")
            if phase == "verified" and entry.get("localNativeCeilingBytes") and root_usage["allocatedBytes"] > entry["localNativeCeilingBytes"]:
                raise RuntimeError("Local native continuation exceeded its admitted stage allocation")
            entry.update(
                {
                    "phase": phase,
                    "updatedAt": now,
                    "rootUsage": root_usage,
                    "intermediateBytes": intermediate,
                    "cleanupRequired": intermediate > 0,
                }
            )
        runs[str(root)] = entry
        _write_registry(registry)
        return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--phase", choices=("begin", "verified", "failed"))
    parser.add_argument("--kind", default="codex-package")
    parser.add_argument("--reservation-gib", type=float, default=8.0)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--temporary-budget-gib", type=float, default=0.0)
    parser.add_argument("--authorization-item-id")
    parser.add_argument("--package-budget-authorization")
    arguments = parser.parse_args()
    if arguments.status:
        print(json.dumps(storage_snapshot(), ensure_ascii=False, indent=2))
        return
    if arguments.root is None or arguments.version is None or arguments.phase is None:
        parser.error("--root, --version and --phase are required unless --status is used")
    result = update(
        arguments.root,
        arguments.version,
        arguments.phase,
        reservation_bytes=int(arguments.reservation_gib * gib),
        kind=arguments.kind,
        report_path=arguments.report,
        temporary_budget_bytes=int(arguments.temporary_budget_gib * gib),
        authorization_item_id=arguments.authorization_item_id,
        package_budget_authorization=arguments.package_budget_authorization,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
