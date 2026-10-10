from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from backend import server


def create_codex_home(root_path: Path) -> Path:
    codex_home = root_path / "codex_home"
    (codex_home / "sessions").mkdir(parents=True)
    with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as connection, connection:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                cwd TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
    return codex_home


def authorization_headers(client: TestClient, codex_home: Path) -> dict[str, str]:
    response = client.get("/api/auth/token", params={"codex_home": str(codex_home)})
    assert response.status_code == 200
    payload = response.json()
    return {payload["headerName"]: payload["token"]}


class FakeService:
    def status(self, codex_home: str | Path, adapter_root=None):
        return {
            "environment": {"codexHome": str(codex_home)},
            "adapter": {"support": "read_write", "schemaFingerprint": "schema"},
            "route": {"classification": "third_party", "confidence": "configuration_only"},
            "authentication": {"providerToken": "[REDACTED]"},
            "historyBuckets": {"custom": 3},
        }

    def audit(self, codex_home: str | Path, adapter_root=None, **kwargs):
        return {
            "files": [],
            "replacementCount": 0,
            "changedFileCount": 0,
            "requiredBackupBytes": 0,
            "promptFingerprint": {"count": 1, "sha256": "prompt"},
            "protectedContentSha256": "protected",
        }

    def preview_repair(self, codex_home: str | Path, adapter_root=None, **kwargs):
        return {
            "operationId": "operation-1",
            "workerStateDigest": "d" * 64,
            "status": self.status(codex_home, adapter_root),
            "audit": self.audit(codex_home, adapter_root),
            "blockers": [],
            "requiresOffline": True,
            "backupRoot": "C:/backup",
        }

    def audit_task(self, codex_home: str | Path, thread_id: str):
        return {
            "threadId": thread_id,
            "providerBucket": "custom",
            "visibilityCompatible": True,
            "continuationCompatible": False,
            "incompatibleItemCount": 1,
            "nonPortableReasoningCount": 1,
            "promptFingerprint": {"count": 7, "sha256": "prompt-sequence"},
            "recommendation": "offline_task_compatibility_repair_required",
        }

    def preview_task_repair(self, codex_home: str | Path, thread_id: str, adapter_root=None):
        return {
            "operationId": "task-operation-1",
            "threadId": thread_id,
            "workerStateDigest": "e" * 64,
            "status": self.status(codex_home, adapter_root),
            "audit": self.audit(codex_home, adapter_root),
            "task": self.audit_task(codex_home, thread_id),
            "blockers": ["Codex.exe:7"],
            "requiresOffline": True,
            "backupRoot": "C:/backup",
        }


def test_status_audit_and_preview_are_redacted_and_bound(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "get_provider_interop_service", lambda: FakeService())
    codex_home = create_codex_home(tmp_path)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    headers = authorization_headers(client, codex_home)
    status = client.get(
        "/api/provider-interoperability/status",
        params={"codex_home": str(codex_home)},
    )
    assert status.status_code == 200
    assert status.json()["route"]["classification"] == "third_party"

    audit = client.post(
        "/api/provider-interoperability/audit",
        params={"codex_home": str(codex_home)},
        headers=headers,
        json={},
    )
    assert audit.status_code == 200
    assert audit.json()["replacementCount"] == 0

    preview = client.post(
        "/api/provider-interoperability/repair/preview",
        params={"codex_home": str(codex_home)},
        headers=headers,
        json={},
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["operationPreviewId"]
    assert payload["inputHash"]
    assert payload["workerStateDigest"] == "d" * 64
    assert payload["status"]["authentication"]["providerToken"] == "[REDACTED]"
    assert "sk-private" not in preview.text.casefold()


def test_provider_bucket_values_are_closed_enums(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "get_provider_interop_service", lambda: FakeService())
    codex_home = create_codex_home(tmp_path)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    headers = authorization_headers(client, codex_home)
    response = client.post(
        "/api/provider-interoperability/audit",
        params={"codex_home": str(codex_home)},
        headers=headers,
        json={"sourceBucket": "arbitrary"},
    )
    assert response.status_code == 422


def test_provider_repair_scope_cannot_be_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "get_provider_interop_service", lambda: FakeService())
    codex_home = create_codex_home(tmp_path)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    response = client.post(
        "/api/provider-interoperability/repair/preview",
        params={"codex_home": str(codex_home)},
        headers=authorization_headers(client, codex_home),
        json={
            "history": False,
            "continuationCompatibility": False,
            "commonConfig": False,
            "interoperabilitySettings": False,
        },
    )
    assert response.status_code == 422


def test_task_compatibility_audit_route_reports_visibility_and_continuation_separately(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(server, "get_provider_interop_service", lambda: FakeService())
    codex_home = create_codex_home(tmp_path)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")

    response = client.get(
        "/api/provider-interoperability/tasks/thread-a/audit",
        params={"codex_home": str(codex_home)},
        headers=authorization_headers(client, codex_home),
    )

    assert response.status_code == 200
    assert response.json()["visibilityCompatible"] is True
    assert response.json()["continuationCompatible"] is False


def test_task_compatibility_repair_preview_is_ticket_bound(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "get_provider_interop_service", lambda: FakeService())
    codex_home = create_codex_home(tmp_path)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")

    response = client.post(
        "/api/provider-interoperability/tasks/thread-a/repair/preview",
        params={"codex_home": str(codex_home)},
        headers=authorization_headers(client, codex_home),
        json={},
    )

    assert response.status_code == 200
    assert response.json()["threadId"] == "thread-a"
    assert response.json()["workerStateDigest"] == "e" * 64
    assert response.json()["operationPreviewId"]
    assert response.json()["inputHash"]
