from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .probe_budget import ProbeBudget


known_bad_sha256 = "3032666de451e423db76b5f9b0456e370d4f57416bbf53d0c746be636359ba02"
artifact_root = Path(r"D:\ZFiles\CXProject\Codex\browser-rollout-repair")
service_relative = Path("bin/node_modules/@oai/browser-desktop/scripts/browser-service.mjs")
maximum_file_bytes = 16 * 1024 * 1024
maximum_versions = 32
hash_cache: OrderedDict = OrderedDict()
hash_cache_lock = threading.Lock()


def _path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _signature(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _file_hash(path: Path, cache_ttl: float, probe_budget: ProbeBudget | None = None) -> str:
    if probe_budget is not None:
        return probe_budget.read(path, maximum_bytes=maximum_file_bytes, digest=True)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_file_bytes:
        raise ValueError("Runtime file is not regular or exceeds the 16 MiB limit")
    key = (_path_key(path), _signature(info))
    clock = time.monotonic()
    with hash_cache_lock:
        cached = hash_cache.get(key)
        if cached and 0 <= clock - cached[0] < cache_ttl:
            return cached[1]
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            total += len(chunk)
            if total > maximum_file_bytes:
                raise ValueError("Runtime file grew beyond the 16 MiB limit")
            digest.update(chunk)
        # Windows stat/fstat can expose different ctime semantics; compare handle identity and mtime.
        if _signature(os.fstat(stream.fileno()))[:4] != _signature(info)[:4]:
            raise ValueError("Runtime file changed during hashing")
    if _signature(path.stat()) != _signature(info):
        raise ValueError("Runtime file was replaced during hashing")
    value = digest.hexdigest()
    with hash_cache_lock:
        hash_cache[key] = clock, value
        hash_cache.move_to_end(key)
        while len(hash_cache) > 128:
            hash_cache.popitem(last=False)
    return value


def _read_evidence(path: Path, probe_budget: ProbeBudget | None = None) -> dict[str, Any] | None:
    try:
        if probe_budget is not None:
            data = probe_budget.read(path, maximum_bytes=65536)
        else:
            with path.open("rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Evidence is not a regular file")
                data = stream.read(65537)
    except FileNotFoundError:
        return None
    if len(data) > 65536:
        raise ValueError("Evidence exceeds the 64 KiB limit")
    value = json.loads(data.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Evidence root is not an object")
    return value


def _same_patch_contract(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    for key in ("sourceHash", "patchedHash", "helperHash", "helperName"):
        if left.get(key) != right.get(key):
            return False
    left_source, right_source = left.get("sourcePath"), right.get("sourcePath")
    return bool(isinstance(left_source, str) and isinstance(right_source, str)
                and _path_key(left_source) == _path_key(right_source))


def _resolve_manifest_path(receipt: Mapping[str, Any], probe_budget: ProbeBudget | None = None) -> Path:
    declared = receipt.get("manifestPath")
    if isinstance(declared, str) and declared:
        path = Path(declared).resolve(strict=False)
        if path.is_relative_to(artifact_root.resolve(strict=False)):
            evidence = _read_evidence(path, probe_budget)
            if evidence and _same_patch_contract(evidence, receipt):
                return path

    matches: list[Path] = []
    try:
        with os.scandir(artifact_root) as entries:
            for index, entry in enumerate(entries):
                if probe_budget is not None:
                    probe_budget.check()
                if index >= maximum_versions:
                    raise ValueError("Patch manifest scan budget exceeded")
                if not entry.is_dir(follow_symlinks=False):
                    continue
                candidate = Path(entry.path) / "manifest.json"
                evidence = _read_evidence(candidate, probe_budget)
                if evidence and _same_patch_contract(evidence, receipt):
                    matches.append(candidate)
    except FileNotFoundError:
        pass
    if matches:
        return sorted(matches, key=lambda path: str(path).casefold())[0]
    return artifact_root / "candidate-v1/manifest.json"


def _seconds(value: Any) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            result = parsed.timestamp()
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _probe_workers(probe_budget: ProbeBudget | None = None) -> dict[str, Any]:
    import psutil

    workers = []
    complete = True
    started = time.monotonic()
    for index, process in enumerate(psutil.process_iter(["pid", "name"])):
        if probe_budget is not None:
            probe_budget.check()
        if index >= 8192 or time.monotonic() - started > 5:
            return {"workers": workers, "complete": False, "error": "Process scan budget exceeded"}
        if (process.info.get("name") or "").lower() not in {"node.exe", "node_repl.exe", "node", "node_repl"}:
            continue
        try:
            command = process.cmdline()
            if not any(Path(argument).name.lower() == "trusted-worker.js" for argument in command):
                continue
            environment = process.environ()
            services = json.loads(environment.get("NODE_REPL_TRUSTED_SERVICES", "{}"))
            if isinstance(services, dict) and "browser" not in services:
                continue
            # Do not retain unrelated environment variables or command-line payloads.
            workers.append({"pid": process.pid, "createdAt": process.create_time(), "exe": process.exe(),
                            "cmdline": [argument for argument in command if argument.endswith("trusted-worker.js")],
                            "environment": {key: environment.get(key, "") for key in
                                            ("NODE_REPL_NODE_MODULE_DIRS", "NODE_REPL_TRUSTED_SERVICES")}})
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError, ValueError):
            complete = False
    return {"workers": workers, "complete": complete}


def _worker_service(worker: Mapping[str, Any], root: Path) -> Path | None:
    command = worker.get("cmdline", [])
    if not isinstance(command, list) or not any(
        isinstance(argument, str) and Path(argument).name.lower() == "trusted-worker.js" for argument in command
    ):
        return None
    environment = worker.get("environment") or {}
    services = json.loads(environment.get("NODE_REPL_TRUSTED_SERVICES", "{}"))
    if not isinstance(services, dict) or services.get("browser") != "@oai/browser-desktop/service":
        return None
    executable = Path(worker.get("exe") or "").resolve()
    runtime = executable.parent.parent
    if executable.name.lower() not in {"node.exe", "node_repl.exe", "node", "node_repl"}:
        return None
    if _path_key(runtime.parent) != _path_key(root) or executable.parent.name.lower() != "bin":
        return None
    modules = environment.get("NODE_REPL_NODE_MODULE_DIRS", "")
    if not isinstance(modules, str) or not modules:
        return None
    # Module lookup order matters: a later matching root cannot prove which service was loaded.
    first_module = modules.split(os.pathsep)[0]
    if _path_key(first_module) != _path_key(runtime / "bin" / "node_modules"):
        return None
    service = runtime / service_relative
    if not service.resolve().is_relative_to(runtime):
        return None
    return service


def _contract_matches(evidence: Mapping[str, Any], service: Path) -> bool:
    helper_name = evidence.get("helperName")
    if not isinstance(helper_name, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:cjs|mjs|js)", helper_name):
        return False
    if helper_name.lower() == service.name.lower():
        return False
    if not all(isinstance(evidence.get(key), str) and re.fullmatch(r"[a-fA-F0-9]{64}", evidence[key])
               for key in ("sourceHash", "patchedHash", "helperHash")):
        return False
    source = evidence.get("sourcePath")
    return bool(isinstance(source, str) and source and _path_key(source) == _path_key(service)
                and evidence["sourceHash"].lower() == known_bad_sha256
                and evidence["patchedHash"].lower() != known_bad_sha256)


def detect_browser_rollout_patch_status(
    environment: Mapping[str, str] | None = None,
    *,
    manifest_path: Path | str | None = None,
    receipt_path: Path | str | None = None,
    process_probe: Callable[[], Mapping[str, Any]] | None = None,
    now: float | None = None,
    cache_ttl: float = 15,
    probe_budget: ProbeBudget | None = None,
) -> dict[str, Any]:
    """Read bounded runtime evidence; never read rollouts or cache process validation."""
    environment = os.environ if environment is None else environment
    now = time.time() if now is None else now
    manifest_path = Path(manifest_path) if manifest_path is not None else None
    receipt_path = Path(receipt_path) if receipt_path is not None else artifact_root / "installed.json"
    result: dict[str, Any] = {"state": "unknown", "checkedAtMs": int(now * 1000), "workers": [],
                              "current": [], "manifestPath": str(manifest_path or "auto"), "receiptPath": str(receipt_path)}

    def finish(state: str, reason: str, reason_zh: str) -> dict[str, Any]:
        return dict(result, state=state, reason=reason, reasonZh=reason_zh)

    local_data = environment.get("LOCALAPPDATA")
    if not local_data:
        return finish("unknown", "LOCALAPPDATA is unavailable.", "无法确定 LOCALAPPDATA，未推断活跃 runtime。")
    root = Path(local_data) / "OpenAI/Codex/runtimes/cua_node"
    try:
        probe = process_probe or ((lambda: _probe_workers(probe_budget)) if probe_budget is not None else _probe_workers)
        if probe_budget is not None:
            probe_budget.check()
        snapshot = probe()
        workers = snapshot.get("workers", [])
        if not isinstance(workers, list) or len(workers) > 128:
            raise ValueError("Invalid or oversized worker snapshot")
        complete = snapshot.get("complete") is True
        active: dict[str, tuple[Path, list[Mapping[str, Any]]]] = {}
        for worker in workers:
            if probe_budget is not None:
                probe_budget.check()
            service = _worker_service(worker, root)
            if service is None:
                complete = False
                continue
            active.setdefault(_path_key(service), (service, []))[1].append(worker)
            result["workers"].append({"pid": worker.get("pid"), "createdAt": worker.get("createdAt"),
                                      "exe": worker.get("exe"), "servicePath": str(service)})
        if not active:
            # Inventory only immediate version entries, never choose by modification time.
            found = False
            try:
                with os.scandir(root) as entries:
                    for index, entry in enumerate(entries):
                        if probe_budget is not None:
                            probe_budget.check()
                        if index >= maximum_versions:
                            raise ValueError("Runtime directory scan budget exceeded")
                        if entry.is_dir(follow_symlinks=False) and (Path(entry.path) / service_relative).is_file():
                            found = True
            except FileNotFoundError:
                pass
            if not found and complete and not workers:
                return finish("not_found", "No browser runtime service was found.", "未发现浏览器 runtime 服务文件。")
            return finish("unknown", "No active browser runtime could be confirmed; disk versions are not proof.",
                          "无法确认活跃浏览器 runtime；磁盘版本和旧凭据不代表当前已生效。")
        if len(active) > 8:
            raise ValueError("Active runtime scan budget exceeded")

        file_signatures = {}
        for service, _ in active.values():
            file_signatures[service] = _signature(service.stat())
            service_hash = _file_hash(service, cache_ttl, probe_budget) if probe_budget is not None else _file_hash(service, cache_ttl)
            result["current"].append({"sourcePath": str(service), "serviceHash": service_hash})
        if any(item["serviceHash"] == known_bad_sha256 for item in result["current"]):
            return finish("vulnerable", "An active runtime still contains the known vulnerable service hash.",
                          "活跃 runtime 的服务文件仍命中已知缺陷 SHA-256。")
        if not complete:
            return finish("unknown", "Some live worker identities or environments could not be confirmed.",
                          "部分活跃 worker 的身份或环境无法核实，不能判为验证通过。")

        receipt = _read_evidence(receipt_path, probe_budget) if probe_budget is not None else _read_evidence(receipt_path)
        if receipt and manifest_path is None:
            manifest_path = _resolve_manifest_path(receipt, probe_budget)
            result["manifestPath"] = str(manifest_path)
        manifest = (_read_evidence(manifest_path, probe_budget) if probe_budget is not None else _read_evidence(manifest_path)) if manifest_path else None
        if not manifest or not receipt:
            return finish("unknown", "Prepared and installed evidence is incomplete; unknown versions are not assumed fixed.",
                          "准备清单或安装凭据缺失；未知版本不会自动视为官方已修复。")
        result["runtimeStatus"] = receipt.get("runtimeStatus")
        result["observationSeconds"] = receipt.get("observationSeconds")
        for (service, _), current in zip(active.values(), result["current"]):
            if not _contract_matches(manifest, service) or not _contract_matches(receipt, service):
                return finish("stale", "Patch evidence does not match the active runtime contract.", "补丁凭据与活跃 runtime 契约不匹配。")
            for key in ("sourceHash", "patchedHash", "helperHash"):
                if manifest[key].lower() != receipt[key].lower():
                    return finish("stale", "Prepared and installed hashes disagree.", "准备清单与安装凭据中的哈希不一致。")
            if manifest["helperName"] != receipt["helperName"]:
                return finish("stale", "Prepared and installed helper names disagree.", "准备清单与安装凭据中的 helper 名称不一致。")
            helper = service.with_name(receipt["helperName"])
            if not helper.resolve().is_relative_to(service.parent.resolve()):
                return finish("stale", "The helper resolves outside the service directory.", "helper 实际路径超出服务目录。")
            try:
                file_signatures[helper] = _signature(helper.stat())
                current["helperHash"] = _file_hash(helper, cache_ttl, probe_budget) if probe_budget is not None else _file_hash(helper, cache_ttl)
            except FileNotFoundError:
                return finish("stale", "The installed helper is missing.", "安装凭据中的 helper 文件不存在。")
            if current["serviceHash"] != receipt["patchedHash"].lower() or current["helperHash"] != receipt["helperHash"].lower():
                return finish("stale", "Service or helper contents no longer match the patch hashes.", "服务或 helper 文件内容与补丁哈希不再一致。")

        live_workers = [worker for _, group in active.values() for worker in group]
        if receipt.get("runtimeStatus") == "verified":
            matching = [worker for worker in live_workers if type(worker.get("pid")) is int
                        and type(receipt.get("workerPid")) is int and worker["pid"] == receipt["workerPid"]]
            created = _seconds(receipt.get("workerCreatedAt"))
            if not matching or created is None or _seconds(matching[0].get("createdAt")) is None or abs(
                _seconds(matching[0]["createdAt"]) - created
            ) > 0.01:
                return finish("installed_pending_runtime", "Patch files match, but the prior runtime validation belongs to a different worker.",
                              "补丁文件匹配，但既有运行验证属于另一个 worker；当前 worker 尚待完整运行验收。")
            installed = _seconds(receipt.get("installedAt"))
            validated = _seconds(receipt.get("runtimeValidatedAt"))
            duration = _seconds(receipt.get("observationSeconds"))
            if (len(live_workers) == 1 and installed is not None and validated is not None and duration is not None
                    and duration >= 900 and installed <= created <= validated <= now + 0.001
                    and validated - created >= duration):
                fresh = probe()
                fresh_workers = fresh.get("workers", [])
                if fresh.get("complete") is not True or not isinstance(fresh_workers, list) or len(fresh_workers) != 1:
                    return finish("unknown", "The live worker set changed during inspection.", "检查期间活跃 worker 集合发生变化或无法再次确认。")
                fresh_worker = fresh_workers[0]
                fresh_service = _worker_service(fresh_worker, root)
                fresh_created = _seconds(fresh_worker.get("createdAt"))
                if (fresh_service is None or _path_key(fresh_service) not in active or fresh_created is None
                        or type(fresh_worker.get("pid")) is not int or fresh_worker["pid"] != receipt["workerPid"]
                        or abs(fresh_created - created) > 0.01):
                    return finish("stale", "The validated worker identity changed during inspection.", "检查期间已验证 worker 的身份或 runtime 归属发生变化。")
                if any(_signature(path.stat()) != signature for path, signature in file_signatures.items()):
                    return finish("unknown", "Runtime files changed during process revalidation.", "再次核对进程身份期间 runtime 文件发生变化。")
                return finish("verified", "Patch hashes and the live worker match a runtime validation of at least 900 seconds.",
                              "补丁文件、活跃 worker 身份与至少 900 秒运行验证凭据一致。")
        return finish("installed_pending_runtime", "Patch files match, but current-worker runtime acceptance remains unverified.",
                      "补丁文件匹配，但当前 worker 尚未满足完整运行验证条件。")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as error:
        result["error"] = str(error)[:300]
        return finish("unknown", "Read-only evidence could not be fully inspected.", "只读证据检查未能完整完成，无法确认修复状态。")
