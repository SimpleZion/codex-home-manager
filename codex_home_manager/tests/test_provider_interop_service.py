from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.provider_interop.service import ProviderInteropService


class FakeWorker:
    def call(self, stage: str, codex_home: str | Path, **kwargs: Any) -> dict[str, Any]:
        if stage == "plan_repair":
            return {
                "payload": {
                    "status": {
                        "environment": {"codexHome": str(codex_home)},
                        "adapter": {"support": "read_write", "schemaFingerprint": "schema", "userVersion": 16},
                        "route": {"classification": "third_party"},
                    },
                    "history": {
                        "files": [{"path": "rollout", "sizeBytes": 10, "modifiedNs": 2, "sourceSha256": "sha"}],
                        "promptFingerprint": {"count": 1, "sha256": "prompt"},
                        "protectedContentSha256": "protected",
                        "changedFileCount": 1,
                        "replacementCount": 1,
                    },
                    "adapterRepair": {
                        "commonConfigChanged": True,
                        "settingChanges": ["unifyCodexSessionHistory"],
                        "providerSnapshotsPreserved": True,
                    },
                    "scopes": kwargs["payload"]["scopes"],
                }
            }
        if stage == "inspect":
            return {
                "payload": {
                    "environment": {"codexHome": str(codex_home)},
                    "adapter": {"support": "read_write", "schemaFingerprint": "schema", "userVersion": 16},
                    "route": {"classification": "third_party"},
                }
            }
        if stage == "audit_history":
            return {
                "payload": {
                    "files": [{"path": "rollout", "sizeBytes": 10, "modifiedNs": 2, "sourceSha256": "sha"}],
                    "promptFingerprint": {"count": 1, "sha256": "prompt"},
                    "protectedContentSha256": "protected",
                    "changedFileCount": 1,
                    "replacementCount": 1,
                }
            }
        if stage == "audit_continuation":
            return {
                "payload": {
                    "candidateFileCount": 1,
                    "nonportableReasoningCount": 1,
                    "incompatibleItemIdCount": 1,
                    "unsupportedForeignItemCount": 0,
                    "parseErrorCount": 0,
                }
            }
        raise AssertionError(stage)


def test_preview_binds_status_and_full_audit(monkeypatch) -> None:
    monkeypatch.setattr("backend.provider_interop.service.process_blockers", lambda: ["Codex.exe:1"])
    service = ProviderInteropService(worker=FakeWorker())
    preview = service.preview_repair(Path("C:/fixture/.codex"), Path("C:/fixture/.cc-switch"))
    assert preview["requiresOffline"] is True
    assert preview["blockers"] == ["Codex.exe:1"]
    assert len(preview["workerStateDigest"]) == 64
    assert preview["audit"]["replacementCount"] == 1
    assert preview["adapterRepair"]["providerSnapshotsPreserved"] is True
    assert preview["scopes"]["continuationCompatibility"] is True


def test_status_includes_version_gated_runtime_compatibility(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.provider_interop.service.inspect_runtime_compatibility",
        lambda **_kwargs: {
            "classification": "official_upgrade_detected",
            "safeForProviderSwitch": True,
            "safeForDesktopUpgrade": False,
            "recommendedAction": "rebase_patch_before_switching_runtime",
        },
    )

    status = ProviderInteropService(worker=FakeWorker()).status(Path("C:/fixture/.codex"))

    assert status["runtimeCompatibility"]["classification"] == "official_upgrade_detected"
    assert status["runtimeCompatibility"]["safeForDesktopUpgrade"] is False


def test_task_audit_separates_shared_visibility_from_cross_provider_continuation(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    rollout_path = codex_home / "sessions" / "2026" / "08" / "16" / "rollout-thread-a.jsonl"
    rollout_path.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": "thread-a"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "keep my prompt"}},
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "item_proxy-reasoning",
                "encrypted_content": "opaque-ciphertext",
                "summary": [],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": "item_proxy-message",
                "content": [{"type": "output_text", "text": "answer"}],
            },
        },
    ]
    rollout_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, model_provider TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads (id, rollout_path, model_provider) VALUES (?, ?, ?)",
            ("thread-a", str(rollout_path), "custom"),
        )

    result = ProviderInteropService(worker=FakeWorker()).audit_task(codex_home, "thread-a")

    assert result["visibilityCompatible"] is True
    assert result["continuationCompatible"] is False
    assert result["incompatibleItemCount"] == 1
    assert result["nonPortableReasoningCount"] == 1
    assert result["promptFingerprint"]["count"] == 1
    assert result["sourceSha256"]
    assert result["recommendation"] == "offline_task_compatibility_repair_required"


def test_task_repair_preview_is_bound_to_one_rollout_and_requires_offline(
    tmp_path: Path, monkeypatch
) -> None:
    codex_home = tmp_path / ".codex"
    rollout_path = codex_home / "sessions" / "rollout-thread-a.jsonl"
    rollout_path.parent.mkdir(parents=True)
    rollout_path.write_text(
        '{"type":"session_meta","payload":{"id":"thread-a"}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"assistant",'
        '"id":"item_foreign","content":[{"type":"output_text","text":"answer"}]}}\n',
        encoding="utf-8",
    )
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, model_provider TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads (id, rollout_path, model_provider) VALUES (?, ?, ?)",
            ("thread-a", str(rollout_path), "custom"),
        )
    monkeypatch.setattr("backend.provider_interop.service.process_blockers", lambda: ["Codex.exe:7"])

    preview = ProviderInteropService(worker=FakeWorker()).preview_task_repair(
        codex_home, "thread-a"
    )

    assert preview["threadId"] == "thread-a"
    assert preview["requiresOffline"] is True
    assert preview["blockers"] == ["Codex.exe:7"]
    assert len(preview["workerStateDigest"]) == 64
