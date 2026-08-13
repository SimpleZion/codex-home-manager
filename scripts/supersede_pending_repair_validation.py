from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "codex_home_manager"))
sys.path.insert(0, str(workspace_root / "scripts"))

from audit_codex_thread_histories import audit_threads
from backend.windows_paths import windows_path_key
from repair_manifest_chain import load_manifest_pair, write_manifest_pair
from verify_codex_after_restart import (
    load_prompt_baseline_audit,
    validate_post_restart_audit_summary,
    validate_restart_prompt_contract,
    write_json_atomic,
)


eligible_statuses = {"pending_restart_validation", "pending_live_ui_validation"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_state(path: Path) -> tuple[str, str]:
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    return head, hashlib.sha256(status.encode("utf-8")).hexdigest()


def supersession_audit_path(report_path: Path) -> Path:
    return report_path.with_name(f"{report_path.stem}.thread_audit.json")


def require_unused_supersession_artifacts(report_path: Path) -> Path:
    audit_path = supersession_audit_path(report_path)
    snapshot_path = audit_path.with_name(f"{audit_path.stem}.state-snapshot.sqlite")
    existing_paths = [
        path
        for path in (report_path, audit_path, snapshot_path)
        if path.exists()
    ]
    if existing_paths:
        joined_paths = ", ".join(str(path) for path in existing_paths)
        raise RuntimeError(f"supersession artifacts already exist: {joined_paths}")
    return audit_path


def validate_pending_contract(
    manifest_path: Path,
    codex_home: Path,
    backup_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path, list[str]]:
    manifest_path = manifest_path.resolve()
    backup_root = backup_root.resolve()
    try:
        manifest_path.relative_to(backup_root)
    except ValueError as error:
        raise RuntimeError("repair manifest is outside the required backup root") from error
    if manifest_path.parent.name != "repair_data":
        raise RuntimeError("repair manifest is not inside a repair_data transaction directory")
    run_root = manifest_path.parent.parent
    lock_path = run_root.parent / "active_repair.lock.json"
    if not lock_path.is_file():
        raise RuntimeError("active repair lock is missing")
    lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    if windows_path_key(lock.get("run_root") or "") != windows_path_key(run_root):
        raise RuntimeError("active repair lock points to another run")
    if windows_path_key(lock.get("manifest") or "") != windows_path_key(manifest_path):
        raise RuntimeError("active repair lock points to another manifest")
    if windows_path_key(lock.get("codex_home") or "") != windows_path_key(codex_home):
        raise RuntimeError("active repair lock points to another Codex Home")

    manifest, manifest_sha256 = load_manifest_pair(
        manifest_path,
        str(lock.get("repair_manifest_sha256") or ""),
    )
    if str(manifest.get("status") or "") not in eligible_statuses:
        raise RuntimeError(f"repair manifest is not pending validation: {manifest.get('status')}")
    if str(lock.get("run_id") or "") != str(manifest.get("runner_run_id") or ""):
        raise RuntimeError("active repair lock run id mismatch")
    if windows_path_key(manifest.get("run_root") or "") != windows_path_key(run_root):
        raise RuntimeError("repair manifest run root mismatch")
    if windows_path_key(manifest.get("codex_home") or "") != windows_path_key(codex_home):
        raise RuntimeError("repair manifest Codex Home mismatch")

    binding_path = Path(str(lock.get("source_binding") or "")).resolve()
    expected_binding_path = run_root / "SOURCE_BINDING.json"
    if windows_path_key(binding_path) != windows_path_key(expected_binding_path):
        raise RuntimeError("repair source binding is outside the active run")
    binding_bytes = binding_path.read_bytes()
    if hashlib.sha256(binding_bytes).hexdigest() != str(lock.get("source_binding_sha256") or "").casefold():
        raise RuntimeError("repair source binding hash mismatch")
    binding = json.loads(binding_bytes.decode("utf-8-sig"))
    if int(binding.get("schema_version") or 0) != 2:
        raise RuntimeError("repair source binding schema is unsupported")
    snapshot_root = Path(str(binding.get("snapshot_root") or "")).resolve()
    if windows_path_key(snapshot_root) != windows_path_key(run_root / "source_snapshot"):
        raise RuntimeError("repair source snapshot root mismatch")

    source_changes: list[str] = []
    for item in binding.get("files") or []:
        expected_hash = str(item.get("sha256") or "").casefold()
        snapshot_path = Path(str(item.get("snapshot_path") or "")).resolve()
        try:
            snapshot_path.relative_to(snapshot_root)
        except ValueError as error:
            raise RuntimeError("repair source snapshot file escapes the snapshot root") from error
        if not snapshot_path.is_file() or file_sha256(snapshot_path) != expected_hash:
            raise RuntimeError(f"repair source snapshot changed: {snapshot_path}")
        source_path = Path(str(item.get("path") or ""))
        if not source_path.is_file() or file_sha256(source_path) != expected_hash:
            source_changes.append(f"file changed: {source_path}")
    for repository in binding.get("repositories") or []:
        repository_path = Path(str(repository.get("path") or ""))
        current_head, current_status_sha256 = git_state(repository_path)
        if (
            current_head != str(repository.get("head") or "")
            or current_status_sha256 != str(repository.get("status_sha256") or "")
        ):
            source_changes.append(
                f"repository changed: {repository_path} | {repository.get('head')} -> {current_head}"
            )
    if not source_changes:
        raise RuntimeError("repair source is unchanged; complete the original validation instead of superseding it")
    manifest["_last_manifest_sha256"] = manifest_sha256
    return manifest, lock, lock_path, source_changes


def supersede_pending_validation(
    manifest_path: Path,
    codex_home: Path,
    report_path: Path,
    reason: str,
    backup_root: Path = Path(r"D:\Backup"),
) -> dict[str, Any]:
    manifest, lock, lock_path, source_changes = validate_pending_contract(
        manifest_path,
        codex_home.resolve(),
        backup_root,
    )
    run_root = manifest_path.resolve().parent.parent
    report_path = report_path.resolve()
    try:
        report_path.relative_to(run_root)
    except ValueError as error:
        raise RuntimeError("supersession report must stay inside the repair run") from error

    audit_path = require_unused_supersession_artifacts(report_path)
    baseline_audit = load_prompt_baseline_audit(manifest_path, manifest)
    current_audit = audit_threads(codex_home.resolve() / "state_5.sqlite", audit_path)
    validate_post_restart_audit_summary(current_audit["summary"])
    prompt_contract = validate_restart_prompt_contract(baseline_audit, current_audit)
    report = {
        "schema_version": 1,
        "status": "validation_superseded",
        "generated_at_epoch": int(time.time()),
        "run_id": str(manifest.get("runner_run_id") or ""),
        "run_root": str(run_root),
        "codex_home": str(codex_home.resolve()),
        "reason": reason,
        "source_changes": source_changes,
        "thread_audit": current_audit["summary"],
        "prompt_contract": prompt_contract,
        "preserved_current_codex_state": True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(report_path, report)
    report_sha256 = file_sha256(report_path)

    previous_status = str(manifest.get("status") or "")
    manifest["status"] = "validation_superseded"
    manifest["validation_superseded_at_epoch"] = int(time.time())
    manifest["validation_superseded_from_status"] = previous_status
    manifest["validation_superseded_reason"] = reason
    manifest["validation_superseded_report"] = str(report_path)
    manifest["validation_superseded_report_sha256"] = report_sha256
    manifest["validation_superseded_prompt_contract"] = prompt_contract
    new_manifest_sha256 = write_manifest_pair(manifest_path, manifest)

    lock["status"] = "validation_superseded"
    lock["updated_at_epoch"] = int(time.time())
    lock["repair_manifest_sha256"] = new_manifest_sha256
    lock["validation_superseded_report"] = str(report_path)
    write_json_atomic(lock_path, lock)
    return report


def preflight_pending_validation(
    manifest_path: Path,
    codex_home: Path,
    report_path: Path,
    backup_root: Path = Path(r"D:\Backup"),
) -> dict[str, Any]:
    manifest, lock, lock_path, source_changes = validate_pending_contract(
        manifest_path,
        codex_home.resolve(),
        backup_root,
    )
    run_root = manifest_path.resolve().parent.parent
    report_path = report_path.resolve()
    try:
        report_path.relative_to(run_root)
    except ValueError as error:
        raise RuntimeError("supersession report must stay inside the repair run") from error
    audit_path = require_unused_supersession_artifacts(report_path)
    return {
        "schema_version": 1,
        "status": "preflight_passed",
        "run_id": str(manifest.get("runner_run_id") or ""),
        "run_root": str(run_root),
        "codex_home": str(codex_home.resolve()),
        "lock_path": str(lock_path),
        "lock_status": str(lock.get("status") or ""),
        "report_path": str(report_path),
        "audit_path": str(audit_path),
        "source_changes": source_changes,
        "writes_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely supersede a pending Codex repair validation without rolling back newer prompts."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--backup-root", type=Path, default=Path(r"D:\Backup"))
    parser.add_argument("--preflight-only", action="store_true")
    arguments = parser.parse_args()
    if arguments.preflight_only:
        report = preflight_pending_validation(
            arguments.manifest,
            arguments.codex_home,
            arguments.report,
            arguments.backup_root,
        )
    else:
        report = supersede_pending_validation(
            arguments.manifest,
            arguments.codex_home,
            arguments.report,
            arguments.reason,
            arguments.backup_root,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
