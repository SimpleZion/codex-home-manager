from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from ..process_utils import list_windows_processes
from ..thread_history_repair import scan_rollout
from .models import ProviderInteropError
from .runtime import inspect_runtime_compatibility
from .transaction import audit_state_digest, default_backup_root
from .worker import NativeWorkerClient


class ProviderInteropService:
    def __init__(self, worker: NativeWorkerClient | None = None) -> None:
        self.worker = worker or NativeWorkerClient()

    def status(self, codex_home: str | Path, adapter_root: str | Path | None = None) -> dict[str, Any]:
        status = self.worker.call(
            "inspect",
            codex_home,
            adapter_root=resolved_adapter_root(adapter_root),
            state_digest="read-only",
        )["payload"]
        status["runtimeCompatibility"] = inspect_runtime_compatibility(codex_home=codex_home)
        return status

    def audit(
        self,
        codex_home: str | Path,
        adapter_root: str | Path | None = None,
        *,
        source_bucket: str = "openai",
        target_bucket: str = "custom",
    ) -> dict[str, Any]:
        return self.worker.call(
            "audit_history",
            codex_home,
            adapter_root=resolved_adapter_root(adapter_root),
            state_digest="read-only",
            payload={
                "sourceBucket": source_bucket,
                "targetBucket": target_bucket,
                "prepareTemporaryRewrites": False,
                "continuationCompatibility": True,
            },
        )["payload"]

    def audit_task(self, codex_home: str | Path, thread_id: str) -> dict[str, Any]:
        home = Path(codex_home).expanduser().resolve(strict=False)
        database_path = home / "state_5.sqlite"
        if not database_path.is_file():
            raise ProviderInteropError("codex_state_not_found", "Codex state database was not found")
        with closing(sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True, timeout=15)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT id, rollout_path, model_provider FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            raise ProviderInteropError("thread_not_found", "Codex task was not found")
        rollout_path = _validated_rollout_path(home, str(row["rollout_path"] or ""))
        if not rollout_path.is_file():
            raise ProviderInteropError("rollout_not_found", "Codex task rollout was not found")
        scan = scan_rollout(rollout_path)
        if scan.session_meta_id != thread_id:
            raise ProviderInteropError(
                "thread_rollout_binding_mismatch",
                "Codex task id does not match its rollout session metadata",
            )
        stat = rollout_path.stat()
        provider_bucket = str(row["model_provider"] or "")
        continuation_audit = self.worker.call(
            "audit_continuation",
            home,
            state_digest="read-only",
            payload={"paths": [str(rollout_path)]},
        )["payload"]
        active_parser_errors = (
            int(continuation_audit.get("nonportableReasoningCount", 0))
            + int(continuation_audit.get("incompatibleItemIdCount", 0))
            + int(continuation_audit.get("unsupportedForeignItemCount", 0))
            + int(continuation_audit.get("parseErrorCount", 0))
        )
        continuation_compatible = active_parser_errors == 0
        if provider_bucket != "custom":
            recommendation = "shared_history_identity_repair_required"
        elif not continuation_compatible:
            recommendation = "offline_task_compatibility_repair_required"
        else:
            recommendation = "compatible"
        return {
            "threadId": thread_id,
            "rolloutPath": str(rollout_path),
            "providerBucket": provider_bucket,
            "visibilityCompatible": provider_bucket == "custom",
            "continuationCompatible": continuation_compatible,
            "incompatibleItemCount": int(continuation_audit.get("incompatibleItemIdCount", 0)),
            "nonPortableReasoningCount": int(continuation_audit.get("nonportableReasoningCount", 0)),
            "unsupportedForeignItemCount": int(continuation_audit.get("unsupportedForeignItemCount", 0)),
            "estimatedParserErrorCount": active_parser_errors,
            "parseErrorCount": scan.parse_errors,
            "latestValidCheckpoint": (
                f"line:{scan.latest_compacted_line}"
                if scan.latest_compacted_line and scan.latest_compacted_checkpoint_valid
                else None
            ),
            "latestCheckpointReason": scan.latest_compacted_checkpoint_reason,
            "promptFingerprint": {
                "count": scan.user_prompt_count,
                "sha256": scan.user_prompt_sha256,
            },
            "sourceBytes": stat.st_size,
            "sourceModifiedNs": stat.st_mtime_ns,
            "sourceSha256": scan.source_sha256,
            "recommendation": recommendation,
        }

    def preview_task_repair(
        self,
        codex_home: str | Path,
        thread_id: str,
        adapter_root: str | Path | None = None,
    ) -> dict[str, Any]:
        task = self.audit_task(codex_home, thread_id)
        operation_id = str(uuid.uuid4())
        plan = self.worker.call(
            "plan_repair",
            codex_home,
            adapter_root=resolved_adapter_root(adapter_root),
            state_digest="read-only",
            operation_id=operation_id,
            payload={
                "sourceBucket": "openai",
                "targetBucket": "custom",
                "paths": [task["rolloutPath"]],
                "scopes": {
                    "history": False,
                    "continuationCompatibility": True,
                    "commonConfig": False,
                    "interoperabilitySettings": False,
                },
            },
        )["payload"]
        status = plan["status"]
        audit = plan["history"]
        return {
            "operationId": operation_id,
            "threadId": thread_id,
            "workerStateDigest": audit_state_digest(status, audit),
            "status": status,
            "audit": audit,
            "task": task,
            "blockers": process_blockers(),
            "requiresOffline": True,
            "backupRoot": str(default_backup_root()),
        }

    def apply_task_repair(
        self,
        codex_home: str | Path,
        thread_id: str,
        adapter_root: str | Path | None,
        *,
        operation_id: str,
        expected_state_digest: str,
        backup_root: str | Path | None = None,
    ) -> dict[str, Any]:
        preview = self.preview_task_repair(codex_home, thread_id, adapter_root)
        if preview["workerStateDigest"] != expected_state_digest:
            raise ProviderInteropError(
                "state_changed_since_preview",
                "provider interoperability task state changed after preview",
            )
        if preview["blockers"]:
            raise ProviderInteropError(
                "offline_processes_running",
                "Codex and CC Switch must be fully closed before task compatibility repair",
                details={"blockers": preview["blockers"]},
            )
        root = Path(backup_root).resolve(strict=False) if backup_root else default_backup_root()
        return self.worker.call(
            "apply_history",
            codex_home,
            adapter_root=resolved_adapter_root(adapter_root),
            state_digest=expected_state_digest,
            operation_id=operation_id,
            payload={
                "sourceBucket": "openai",
                "targetBucket": "custom",
                "backupRoot": str(root),
                "paths": [preview["task"]["rolloutPath"]],
                "scopes": {
                    "history": False,
                    "continuationCompatibility": True,
                    "commonConfig": False,
                    "interoperabilitySettings": False,
                },
            },
        )["payload"]

    def preview_repair(
        self,
        codex_home: str | Path,
        adapter_root: str | Path | None = None,
        *,
        source_bucket: str = "openai",
        target_bucket: str = "custom",
        history: bool = True,
        continuation_compatibility: bool = True,
        common_config: bool = True,
        interoperability_settings: bool = True,
    ) -> dict[str, Any]:
        operation_id = str(uuid.uuid4())
        plan = self.worker.call(
            "plan_repair",
            codex_home,
            adapter_root=resolved_adapter_root(adapter_root),
            state_digest="read-only",
            operation_id=operation_id,
            payload={
                "sourceBucket": source_bucket,
                "targetBucket": target_bucket,
                "scopes": {
                    "history": history,
                    "continuationCompatibility": continuation_compatibility,
                    "commonConfig": common_config,
                    "interoperabilitySettings": interoperability_settings,
                },
            },
        )["payload"]
        status = plan["status"]
        audit = plan["history"]
        blockers = process_blockers()
        if status.get("adapter", {}).get("support") == "read_only":
            blockers.append(str(status.get("adapter", {}).get("reason") or "unsupported_adapter_schema"))
        return {
            "operationId": operation_id,
            "workerStateDigest": audit_state_digest(status, audit),
            "status": status,
            "audit": audit,
            "blockers": blockers,
            "requiresOffline": True,
            "sourceBucket": source_bucket,
            "targetBucket": target_bucket,
            "backupRoot": str(default_backup_root()),
            "adapterRepair": plan.get("adapterRepair"),
            "scopes": plan.get("scopes", {}),
        }

    def apply_repair(
        self,
        codex_home: str | Path,
        adapter_root: str | Path | None,
        *,
        operation_id: str,
        expected_state_digest: str,
        source_bucket: str = "openai",
        target_bucket: str = "custom",
        backup_root: str | Path | None = None,
        history: bool = True,
        continuation_compatibility: bool = True,
        common_config: bool = True,
        interoperability_settings: bool = True,
    ) -> dict[str, Any]:
        preview = self.preview_repair(
            codex_home,
            adapter_root,
            source_bucket=source_bucket,
            target_bucket=target_bucket,
            history=history,
            continuation_compatibility=continuation_compatibility,
            common_config=common_config,
            interoperability_settings=interoperability_settings,
        )
        if preview["workerStateDigest"] != expected_state_digest:
            raise ProviderInteropError("state_changed_since_preview", "provider interoperability state changed after preview")
        if preview["blockers"]:
            raise ProviderInteropError(
                "offline_processes_running",
                "Codex and CC Switch must be fully closed before provider interoperability repair",
                details={"blockers": preview["blockers"]},
            )
        root = Path(backup_root).resolve(strict=False) if backup_root else default_backup_root()
        return self.worker.call(
            "apply_history",
            codex_home,
            adapter_root=resolved_adapter_root(adapter_root),
            state_digest=expected_state_digest,
            operation_id=operation_id,
            payload={
                "sourceBucket": source_bucket,
                "targetBucket": target_bucket,
                "backupRoot": str(root),
                "scopes": {
                    "history": history,
                    "continuationCompatibility": continuation_compatibility,
                    "commonConfig": common_config,
                    "interoperabilitySettings": interoperability_settings,
                },
            },
        )["payload"]

    def verify(
        self,
        codex_home: str | Path,
        adapter_root: str | Path | None = None,
        *,
        source_bucket: str = "openai",
        target_bucket: str = "custom",
        history: bool = True,
        continuation_compatibility: bool = True,
        common_config: bool = True,
        interoperability_settings: bool = True,
    ) -> dict[str, Any]:
        preview = self.preview_repair(
            codex_home,
            adapter_root,
            source_bucket=source_bucket,
            target_bucket=target_bucket,
            history=history,
            continuation_compatibility=continuation_compatibility,
            common_config=common_config,
            interoperability_settings=interoperability_settings,
        )
        adapter = preview.get("adapterRepair") or {}
        verified = (
            (not history or preview["audit"].get("replacementCount", 0) == 0)
            and (
                not continuation_compatibility
                or (
                    preview["audit"].get("nonportableReasoningCount", 0) == 0
                    and preview["audit"].get("incompatibleItemIdCount", 0) == 0
                    and preview["audit"].get("unsupportedForeignItemCount", 0) == 0
                )
            )
            and (not common_config or not adapter.get("commonConfigChanged", False))
            and (not interoperability_settings or not adapter.get("settingChanges"))
        )
        return {
            "status": preview["status"],
            "audit": preview["audit"],
            "adapterRepair": preview.get("adapterRepair"),
            "scopes": preview.get("scopes", {}),
            "blockers": preview.get("blockers", []),
            "verified": verified,
        }

    def rollback(
        self,
        codex_home: str | Path,
        manifest_path: str | Path,
        *,
        operation_id: str,
        state_digest: str,
    ) -> dict[str, Any]:
        blockers = process_blockers()
        if blockers:
            raise ProviderInteropError(
                "offline_processes_running",
                "Codex and CC Switch must be fully closed before provider interoperability rollback",
                details={"blockers": blockers},
            )
        return self.worker.call(
            "rollback_history",
            codex_home,
            state_digest=state_digest,
            operation_id=operation_id,
            payload={"manifestPath": str(Path(manifest_path).resolve(strict=False))},
        )["payload"]


def resolved_adapter_root(adapter_root: str | Path | None) -> Path | None:
    if adapter_root:
        return Path(adapter_root).expanduser().resolve(strict=False)
    conventional = Path.home() / ".cc-switch"
    return conventional.resolve(strict=False) if conventional.is_dir() else None


def _validated_rollout_path(codex_home: Path, rollout_path_text: str) -> Path:
    normalized = rollout_path_text.removeprefix("\\\\?\\")
    rollout_path = Path(normalized).expanduser().resolve(strict=False)
    home_key = os.path.normcase(str(codex_home))
    rollout_key = os.path.normcase(str(rollout_path))
    try:
        inside_home = os.path.commonpath([home_key, rollout_key]) == home_key
    except ValueError:
        inside_home = False
    if not inside_home:
        raise ProviderInteropError("rollout_outside_codex_home", "Codex task rollout is outside Codex Home")
    return rollout_path


def process_blockers() -> list[str]:
    if os.name != "nt":
        return []
    blocked_names = {"codex.exe", "codex", "codex.cmd", "cc-switch.exe", "chatgpt.exe"}
    return [
        f"{process['imageName']}:{process['pid']}"
        for process in list_windows_processes()
        if process["imageName"].casefold() in blocked_names
    ][:32]
