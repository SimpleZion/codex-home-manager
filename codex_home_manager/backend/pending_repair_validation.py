from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


default_pending_repair_lock = Path(r"D:\Backup\codex_full_repair\active_repair.lock.json")
result_file_name = "automatic_restart_validation_result.json"
def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.writing")
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    with temporary_path.open("wb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary_path, path)


def _load_pending_lock(lock_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "pending_restart_validation":
        return None
    return payload


def _snapshot_root(payload: dict[str, Any]) -> Path:
    snapshot = payload.get("source_snapshot_root")
    if not snapshot:
        raise RuntimeError("pending repair lock has no source snapshot")
    root = Path(str(snapshot)).resolve()
    if not root.is_dir():
        raise RuntimeError(f"source snapshot is missing: {root}")
    return root


def _bound_verifier(payload: dict[str, Any], source_snapshot: Path, run_root: Path) -> tuple[Path, str]:
    binding_path = Path(str(payload.get("source_binding") or "")).resolve()
    if binding_path != (run_root / "SOURCE_BINDING.json").resolve() or not binding_path.is_file():
        raise RuntimeError("repair source binding is missing or outside the pending repair run")
    binding_bytes = binding_path.read_bytes()
    if hashlib.sha256(binding_bytes).hexdigest() != str(payload.get("source_binding_sha256") or "").casefold():
        raise RuntimeError("repair source binding SHA-256 does not match the pending repair lock")
    binding = json.loads(binding_bytes.decode("utf-8-sig"))
    if int(binding.get("schema_version") or 0) != 2:
        raise RuntimeError("repair source binding schema is unsupported")
    if Path(str(binding.get("snapshot_root") or "")).resolve() != source_snapshot:
        raise RuntimeError("repair source binding has a different source snapshot root")
    verifier = next(
        (
            item
            for item in binding.get("files", [])
            if isinstance(item, dict)
            and str(item.get("relative_path") or "").replace("\\", "/").casefold()
            == "scripts/verify_codex_after_restart.py"
        ),
        None,
    )
    if verifier is None:
        raise RuntimeError("repair source binding has no bound restart verifier")
    verifier_path = Path(str(verifier.get("snapshot_path") or "")).resolve()
    if not verifier_path.is_file() or not _path_within(verifier_path, source_snapshot):
        raise RuntimeError("bound verifier is missing or outside the source snapshot")
    expected_sha256 = str(verifier.get("sha256") or "").casefold()
    actual_sha256 = hashlib.sha256(verifier_path.read_bytes()).hexdigest()
    if expected_sha256 != actual_sha256:
        raise RuntimeError("bound verifier SHA-256 does not match the pending repair lock")
    python_executable = str(payload.get("python_executable") or shutil.which("python") or "")
    if not python_executable:
        if getattr(sys, "frozen", False):
            raise RuntimeError("bound verifier has no Python executable for the packaged connector")
        python_executable = sys.executable
    return verifier_path, python_executable


def _run_root(payload: dict[str, Any], lock_path: Path) -> Path:
    run_root = Path(str(payload.get("run_root") or "")).resolve()
    if not run_root.is_dir() or not _path_within(run_root, lock_path.resolve().parent):
        raise RuntimeError("pending repair run root is missing or outside the full-repair backup root")
    return run_root


def _command_from_lock(payload: dict[str, Any], lock_path: Path) -> tuple[list[str], Path]:
    run_root = _run_root(payload, lock_path)
    source_snapshot = _snapshot_root(payload)
    if not _path_within(source_snapshot, run_root):
        raise RuntimeError("source snapshot is outside the pending repair run")
    verifier_path, python_executable = _bound_verifier(payload, source_snapshot, run_root)
    manifest_path = Path(str(payload.get("manifest") or "")).resolve()
    if not manifest_path.is_file() or not _path_within(manifest_path, run_root):
        raise RuntimeError("repair manifest is missing or outside the pending repair run")
    report_path = run_root / "post_restart_validation.json"
    codex_home = Path(str(payload.get("codex_home") or r"D:\.codex"))
    command = [
        python_executable,
        str(verifier_path),
        "--codex-home",
        str(codex_home),
        "--manifest",
        str(manifest_path),
        "--report",
        str(report_path),
    ]
    return command, run_root


def run_pending_restart_validation(
    lock_path: Path = default_pending_repair_lock,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    started_at_epoch = int(time.time())
    payload = _load_pending_lock(lock_path)
    if payload is None:
        return {"status": "skipped", "reason": "no pending restart validation"}

    run_root: Path | None = None
    try:
        run_root = _run_root(payload, lock_path)
        command, run_root = _command_from_lock(payload, lock_path)
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        result: dict[str, Any] = {
            "schema_version": 1,
            "status": "complete" if completed.returncode == 0 else "failed",
            "run_id": str(payload.get("run_id") or ""),
            "started_at_epoch": started_at_epoch,
            "completed_at_epoch": int(time.time()),
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if completed.returncode != 0:
            result["error"] = f"bound restart verifier exited with code {completed.returncode}"
    except Exception as error:
        result = {
            "schema_version": 1,
            "status": "failed",
            "run_id": str(payload.get("run_id") or ""),
            "started_at_epoch": started_at_epoch,
            "completed_at_epoch": int(time.time()),
            "error": str(error),
        }

    if run_root is not None:
        _write_json_atomic(run_root / result_file_name, result)
    if result["status"] == "failed":
        logging.error("automatic restart validation failed: %s", result.get("error") or result.get("stderr"))
    else:
        logging.info("automatic restart validation completed for run_id=%s", result.get("run_id"))
    return result


def start_pending_restart_validation(
    lock_path: Path = default_pending_repair_lock,
    *,
    thread_factory: Callable[..., threading.Thread] = threading.Thread,
) -> bool:
    if _load_pending_lock(lock_path) is None:
        return False
    worker = thread_factory(
        target=lambda: run_pending_restart_validation(lock_path),
        daemon=False,
        name="codex-restart-validation",
    )
    worker.start()
    return True
