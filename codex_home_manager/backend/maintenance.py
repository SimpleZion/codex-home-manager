from __future__ import annotations

import hashlib
import asyncio
import json
import os
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from .pagination_patch_status import _candidate_paths, _read_first_state, _state_view, default_artifact_root
from .probe_budget import ProbeBudget, ProbeStopped


router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])
manifest_path = Path(__file__).resolve().parents[1] / "packaging" / "runtime" / "cockpit-maintenance.json"
maximum_evidence_bytes = 128 * 1024
runtime_probe_seconds = 5.0
runtime_probe_lock = threading.Lock()
runtime_probe_home: str | None = None


def incomplete_runtime(status: str) -> dict[str, Any]:
    return {"observedAt": datetime.now(timezone.utc).isoformat(), "runtimeVerified": False, "probeStatus": status,
            "checks": [{"id": identity, "state": "unknown", "reason": status,
                        "scope": "No completed runtime inspection; no execution authorized", "details": {}}
                       for identity in ("desktop-pagination-6511", "browser-memory-tracker", "provider-interop", "ordinal-history-recovery")]}


def prepared_artifact(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state or not isinstance(state.get("packagePath"), str):
        return None
    expected = str(state.get("packageSha256") or "").upper()
    if len(expected) != 64 or any(character not in "0123456789ABCDEF" for character in expected):
        return {"availability": "invalid", "reason": "Package SHA-256 is missing or invalid"}
    try:
        path = Path(state["packagePath"]).resolve()
        if not path.is_relative_to(default_artifact_root.resolve()) or path.suffix.lower() not in {".msix", ".msixbundle"}:
            return {"availability": "invalid", "reason": "Package is outside the managed artifact scope"}
        if not path.is_file():
            return {"availability": "missing", "path": str(path), "expectedSha256": expected}
        return {"availability": "present", "path": str(path), "bytes": path.stat().st_size, "expectedSha256": expected}
    except OSError:
        return {"availability": "unreadable"}


def catalog_bytes() -> bytes:
    with manifest_path.open("rb") as source:
        content = source.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("Maintenance catalog exceeds 2 MiB")
    return content


def load_catalog() -> dict[str, Any]:
    content = catalog_bytes()
    catalog = json.loads(content)
    if catalog.get("schemaVersion") != 1 or not isinstance(catalog.get("entries"), list):
        raise ValueError("Unsupported maintenance catalog")
    return {**catalog, "contentSha256": hashlib.sha256(content).hexdigest()}


def evidence_root(source_workspace: str | None = None, *, catalog_sha256: str | None = None) -> Path | None:
    if source_workspace:
        root = Path(source_workspace).expanduser().resolve()
        workspace_catalog = root / "codex_home_manager" / "packaging" / "runtime" / "cockpit-maintenance.json"
        if not (root / "AGENTS.md").is_file() or not (root / "scripts").is_dir() or not workspace_catalog.is_file():
            raise HTTPException(400, "The selected directory is not a maintenance source workspace")
        with workspace_catalog.open("rb") as source:
            candidate = source.read(2 * 1024 * 1024 + 1)
        expected = catalog_sha256 or hashlib.sha256(catalog_bytes()).hexdigest()
        if len(candidate) > 2 * 1024 * 1024 or hashlib.sha256(candidate).hexdigest() != expected:
            raise HTTPException(409, "Workspace catalog differs from the installed catalog")
        return root
    # Packaged installs must not infer a workspace from their installation parent.
    if getattr(sys, "frozen", False):
        return None
    root = Path(__file__).resolve().parents[2]
    return root if (root / "AGENTS.md").is_file() and (root / "scripts").is_dir() else None


def resolve_evidence(relative_path: str, root: Path | None) -> Path | None:
    if root is None:
        return None
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or ":" in relative_path:
        raise ValueError("Evidence must be workspace relative")
    resolved_root = root.resolve()
    path = (resolved_root / relative).resolve()
    if not path.is_relative_to(resolved_root):
        raise ValueError("Evidence escapes workspace")
    return path


def evidence_metadata(record: dict[str, str], root: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {**record, "availability": "unavailable"}
    try:
        path = resolve_evidence(record["path"], root)
        if path is None:
            return result
        if not path.is_file():
            return {**result, "availability": "missing"}
        stat = path.stat()
        return {**result, "availability": "present", "bytes": stat.st_size, "modifiedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()}
    except (OSError, ValueError):
        return {**result, "availability": "unreadable"}


def maintenance_status(source_workspace: str | None = None) -> dict[str, Any]:
    catalog = load_catalog()
    root = evidence_root(source_workspace)
    receipts = []
    for kind, environment_name in (("ready", "CODEX_PAGINATION_READY_STATE"), ("installed", "CODEX_PAGINATION_INSTALLED_STATE")):
        state, path, error = _read_first_state(_candidate_paths(environment_name, f"{kind}.json", os.environ))
        view = _state_view(state, path)
        # Receipts are observations, never proof that the currently running process uses the package.
        safe_fields = ("status", "mainVersion", "packageVersion", "featureContractVersion", "runtimeStatus", "runtimeValidatedAt", "preparedAt", "finishedAt", "patchedAsarSha256", "effectiveAsarHash")
        receipts.append({"kind": kind, "availability": "invalid" if error else "present" if view else "missing", "values": {key: view[key] for key in safe_fields} if view else {}, "artifact": prepared_artifact(state) if kind == "ready" else None})
    return {
        "schemaVersion": 1, "observedAt": datetime.now(timezone.utc).isoformat(),
        "catalogUpdatedAt": catalog["updatedAt"], "runtimeVerified": False,
        "catalogSha256": catalog["contentSha256"],
        "sourceWorkspaceAvailable": root is not None, "receipts": receipts,
        "entries": [{"id": entry["id"], "evidence": [evidence_metadata(record, root) for record in entry["evidence"]]} for entry in catalog["entries"]],
    }


@router.get("/status")
def get_status(response: Response, source_workspace: str | None = Query(default=None, max_length=1024)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return maintenance_status(source_workspace)


@router.get("/runtime")
async def runtime_status(request: Request, response: Response, codex_home: str | None = Query(default=None, max_length=1024)) -> dict[str, Any]:
    global runtime_probe_home
    response.headers["Cache-Control"] = "no-store"
    key = os.path.normcase(os.path.abspath(os.path.expanduser(codex_home or os.environ.get("CODEX_HOME") or "~/.codex")))
    # One process-wide slot also bounds repeated Home switches. Never queue duplicates.
    if not runtime_probe_lock.acquire(blocking=False):
        return incomplete_runtime("in_progress" if runtime_probe_home == key else "busy")
    runtime_probe_home = key
    budget = ProbeBudget(seconds=runtime_probe_seconds)

    def run() -> dict[str, Any]:
        global runtime_probe_home
        try:
            return inspect_runtime_status(codex_home, budget)
        except ProbeStopped:
            return incomplete_runtime("partial_budget")
        finally:
            runtime_probe_home = None
            runtime_probe_lock.release()

    try:
        future = asyncio.get_running_loop().run_in_executor(None, run)
    except BaseException:
        runtime_probe_home = None
        runtime_probe_lock.release()
        raise
    # OS calls cannot be force-cancelled safely; retain the slot until the worker exits.
    future.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
    try:
        while not future.done():
            if await request.is_disconnected():
                budget.cancelled.set()
                return incomplete_runtime("cancelled")
            if time.monotonic() >= budget.deadline:
                budget.cancelled.set()
                return incomplete_runtime("partial_deadline")
            await asyncio.sleep(0.025)
        return future.result()
    finally:
        budget.cancelled.set()


def inspect_runtime_status(codex_home: str | None, budget: ProbeBudget) -> dict[str, Any]:
    from .browser_rollout_patch_status import detect_browser_rollout_patch_status
    from .codex_data import detect_current_codex_versions, resolve_codex_paths
    from .pagination_patch_status import detect_pagination_patch_status
    from .pending_repair_validation import pending_repair_lock_path
    from .provider_interop.runtime import inspect_runtime_compatibility

    try:
        budget.check()
        paths = resolve_codex_paths(codex_home)
    except ValueError as error:
        raise HTTPException(400, "Invalid Codex Home") from error
    checks: list[dict[str, Any]] = []
    try:
        budget.check()
        versions = detect_current_codex_versions(paths, allow_execution=False, probe_budget=budget)
        budget.check()
        # Existing receipt reader is separately bounded to 1 MiB and at most four candidates.
        budget.reserve(4 * 1024 * 1024)
        pagination = detect_pagination_patch_status(versions.get("desktopInstall"))
        budget.check()
        checks.append({"id": "desktop-pagination-6511", "state": pagination["state"], "reason": pagination["reason"], "scope": "安装记录与回执匹配；不是当前renderer字节或进程验收", "details": {"desktopInstall": versions.get("desktopInstall"), "configuredCli": versions.get("configuredCli"), "requiredContractVersion": pagination["policy"]["requiredFeatureContractVersion"], **{key: pagination[key] for key in ("receiptObservedAt", "evidenceScope", "currentRuntimeVerified", "checkedAt", "checkedAtMs") if key in pagination}}})
    except Exception:
        checks.append({"id": "desktop-pagination-6511", "state": "unknown", "reason": "安装与分页检测器未完成", "scope": "不推断为已生效", "details": {}})
    try:
        budget.check()
        browser = detect_browser_rollout_patch_status(probe_budget=budget)
        budget.check()
        checks.append({"id": "browser-memory-tracker", "state": browser["state"], "reason": browser["reasonZh"], "scope": "既有浏览器检测器的当前worker身份与文件哈希检查", "details": {key: browser[key] for key in ("checkedAtMs", "workers", "current", "runtimeStatus", "observationSeconds") if key in browser}})
    except Exception:
        checks.append({"id": "browser-memory-tracker", "state": "unknown", "reason": "浏览器检测器未完成", "scope": "不推断为已修复", "details": {}})
    try:
        budget.check()
        runtime = inspect_runtime_compatibility(codex_home=paths.codex_home_path, allow_execution=False, probe_budget=budget)
        budget.check()
        checks.append({"id": "provider-interop", "state": runtime["classification"], "reason": runtime["recommendedAction"], "scope": "既有runtime兼容检测；不执行历史审计、修复或模型请求", "details": {key: runtime.get(key) for key in ("official", "override", "companion", "issues", "contracts", "activationConflict")}})
    except Exception:
        checks.append({"id": "provider-interop", "state": "unknown", "reason": "运行时兼容检测器未完成", "scope": "不推断为可跨提供商续跑", "details": {}})
    try:
        budget.check()
        budget.reserve(1024 * 1024)
        pending, _, error = _read_first_state([pending_repair_lock_path()])
        budget.check()
    except ProbeStopped:
        pending, error = None, "probe_stopped"
    checks.append({"id": "ordinal-history-recovery", "state": "unknown" if error == "probe_stopped" else "invalid" if error else str(pending.get("status") or "unknown") if pending else "not_found", "reason": "探测预算不足或已取消，待验收事务记录读取未完成；不判定锁损坏" if error == "probe_stopped" else "只读待验收事务记录；未执行验证命令，也未改变锁", "scope": "锁存在不证明持有writer guard，锁缺失不证明没有其他事务", "details": {key: pending[key] for key in ("status", "created_at", "updated_at", "finished_at") if pending and key in pending}})
    return {"observedAt": datetime.now(timezone.utc).isoformat(), "runtimeVerified": False,
            "probeStatus": "partial" if error or any(check["state"] == "unknown" for check in checks) else "completed", "checks": checks}


@router.get("/artifact/ready")
def verify_prepared_artifact(response: Response, expected_path: str | None = Query(default=None, max_length=4096),
                             expected_sha256: str | None = Query(default=None, max_length=64)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    state, _, error = _read_first_state(_candidate_paths("CODEX_PAGINATION_READY_STATE", "ready.json", os.environ))
    artifact = prepared_artifact(state)
    if error or not artifact or artifact.get("availability") != "present":
        raise HTTPException(409, "No readable prepared package in the managed artifact scope")
    path = Path(artifact["path"])
    if expected_path is not None:
        try:
            matches = os.path.normcase(str(Path(expected_path).resolve())) == os.path.normcase(str(path))
        except (OSError, ValueError):
            matches = False
        if not matches:
            raise HTTPException(409, "Prepared package path changed; refresh its receipt")
    if expected_sha256 is not None and expected_sha256.upper() != artifact["expectedSha256"]:
        raise HTTPException(409, "Prepared package hash changed; refresh its receipt")
    before = path.stat()
    if before.st_size > 512 * 1024 * 1024:
        raise HTTPException(413, "Package exceeds the 512 MiB verification budget")
    digest = hashlib.sha256()
    started = time.monotonic()
    read_bytes = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                read_bytes += len(chunk)
                if read_bytes > 512 * 1024 * 1024 or time.monotonic() - started > 10:
                    raise HTTPException(409, "Verification budget exceeded; no partial digest accepted")
                digest.update(chunk)
        after = path.stat()
    except OSError as error:
        raise HTTPException(409, "Prepared package changed or became unreadable") from error
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or read_bytes != before.st_size:
        raise HTTPException(409, "Prepared package changed during verification")
    actual = digest.hexdigest().upper()
    return {**artifact, "actualSha256": actual, "hashMatches": actual == artifact["expectedSha256"], "observedAt": datetime.now(timezone.utc).isoformat(), "installed": False, "runtimeVerified": False}


@router.get("/evidence/{entry_id}/{evidence_index}")
def get_evidence(entry_id: str, evidence_index: int, response: Response, source_workspace: str | None = Query(default=None, max_length=1024),
                 catalog_sha256: str | None = Query(default=None, max_length=64)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    catalog = load_catalog()
    if catalog_sha256 is not None and catalog_sha256.lower() != catalog["contentSha256"].lower():
        raise HTTPException(409, "Maintenance catalog changed; refresh evidence")
    entry = next((entry for entry in catalog["entries"] if entry["id"] == entry_id), None)
    if entry is None or not 0 <= evidence_index < len(entry["evidence"]):
        raise HTTPException(404, "Unknown registered evidence")
    record = entry["evidence"][evidence_index]
    try:
        root = evidence_root(source_workspace, catalog_sha256=catalog["contentSha256"]) if source_workspace else evidence_root(source_workspace)
        path = resolve_evidence(record["path"], root)
        if path is None:
            raise HTTPException(409, "Source workspace unavailable in this installation")
        before = path.stat()
        with path.open("rb") as source:
            content = source.read(maximum_evidence_bytes + 1)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise HTTPException(409, "Evidence changed during fingerprinting")
        truncated = len(content) > maximum_evidence_bytes
        if not truncated and len(content) != before.st_size:
            raise HTTPException(409, "Evidence size changed during fingerprinting")
        return {**evidence_metadata(record, root), "catalogSha256": catalog["contentSha256"], "entryId": entry_id,
                "evidenceIndex": evidence_index, "path": record["path"], "hashStatus": "size_limit" if truncated else "complete", "sha256": hashlib.sha256(content).hexdigest() if not truncated else None, "observedAt": datetime.now(timezone.utc).isoformat()}
    except FileNotFoundError as error:
        raise HTTPException(404, "Registered evidence file is missing") from error
    except (OSError, ValueError) as error:
        raise HTTPException(403, "Registered evidence is not readable") from error
