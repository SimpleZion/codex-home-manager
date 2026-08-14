from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import server


@pytest.fixture(autouse=True)
def clear_runtime_ticket_stores() -> None:
    server.preview_store.clear()
    server.authorization_store.clear()
    yield
    server.preview_store.clear()
    server.authorization_store.clear()


def create_codex_home(
    root_path: Path,
    threads: list[tuple[str, str, bytes]] | None = None,
) -> tuple[Path, dict[str, Path]]:
    codex_home_path = root_path / "codex_home"
    sessions_path = codex_home_path / "sessions"
    sessions_path.mkdir(parents=True)
    rollout_paths: dict[str, Path] = {}

    with closing(sqlite3.connect(codex_home_path / "state_5.sqlite")) as connection, connection:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                cwd TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                updated_at_ms INTEGER
            )
            """
        )
        for thread_id, project_path, rollout_content in threads or []:
            rollout_path = sessions_path / f"rollout-{thread_id}.jsonl"
            rollout_path.write_bytes(rollout_content)
            connection.execute(
                "INSERT INTO threads (id, rollout_path, cwd, archived, updated_at, updated_at_ms) "
                "VALUES (?, ?, ?, 0, 0, 0)",
                (thread_id, str(rollout_path), project_path),
            )
            rollout_paths[thread_id] = rollout_path

    (codex_home_path / "config.toml").write_text("model = \"gpt-5\"\n", encoding="utf-8")
    (codex_home_path / "session_index.jsonl").write_text("", encoding="utf-8")
    (codex_home_path / ".codex-global-state.json").write_text("{}", encoding="utf-8")
    return codex_home_path, rollout_paths


def authorization_headers(client: TestClient, codex_home_path: Path) -> dict[str, str]:
    response = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)})
    assert response.status_code == 200
    payload = response.json()
    return {payload["headerName"]: payload["token"]}


def replace_one_byte_without_changing_size_or_mtime(path: Path) -> None:
    before_stat = path.stat()
    with path.open("r+b") as handle:
        handle.seek(-2, os.SEEK_END)
        original_byte = handle.read(1)
        handle.seek(-1, os.SEEK_CUR)
        handle.write(b"y" if original_byte != b"y" else b"z")
    os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    after_stat = path.stat()
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def assert_state_conflict(response: object) -> None:
    assert getattr(response, "status_code") == 409
    assert "state changed" in getattr(response, "json")()["detail"]


def test_resource_copy_ticket_binds_source_home_and_relative_path(tmp_path: Path) -> None:
    source_home, _ = create_codex_home(tmp_path / "source")
    target_home, _ = create_codex_home(tmp_path / "target")
    source_resource = source_home / "memories" / "state.md"
    source_resource.parent.mkdir()
    source_resource.write_text("source-one", encoding="utf-8")

    with TestClient(server.app) as client:
        headers = authorization_headers(client, target_home)
        request_body = {
            "sourceCodexHome": str(source_home),
            "relativePath": "memories/state.md",
            "targetRelativePath": "memories/state.md",
            "overwrite": False,
        }
        preview_response = client.post(
            "/api/resources/copy-from-home/preview",
            params={"codex_home": str(target_home)},
            headers=headers,
            json=request_body,
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()

        before_stat = source_resource.stat()
        source_resource.write_text("source-two", encoding="utf-8")
        os.utime(source_resource, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))

        apply_response = client.post(
            "/api/resources/copy-from-home",
            params={"codex_home": str(target_home)},
            headers=headers,
            json={
                **request_body,
                "acknowledgeCodexRunningRisk": True,
                "createBackup": False,
                "operationPreviewId": preview["operationPreviewId"],
                "inputHash": preview["inputHash"],
            },
        )

    assert_state_conflict(apply_response)
    assert not (target_home / "memories" / "state.md").exists()


def test_thread_import_ticket_streams_and_binds_source_thread_rollout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path = str(tmp_path / "source-project")
    large_rollout = (
        b'{"type":"session_meta","payload":{"id":"source-thread"}}\n'
        + b'{"type":"user_message","payload":{"text":"'
        + (b"x" * (server.state_hash_chunk_bytes * 2 + 17))
        + b'"}}\n'
    )
    source_home, source_rollouts = create_codex_home(
        tmp_path / "source",
        [("source-thread", project_path, large_rollout)],
    )
    target_home, _ = create_codex_home(tmp_path / "target")

    def reject_unbounded_read(_path: Path) -> bytes:
        raise AssertionError("Path.read_bytes must not be used for preview state hashing")

    monkeypatch.setattr(Path, "read_bytes", reject_unbounded_read)

    with TestClient(server.app) as client:
        headers = authorization_headers(client, target_home)
        request_body = {
            "sourceCodexHome": str(source_home),
            "sourceThreadId": "source-thread",
            "targetProjectPath": str(tmp_path / "target-project"),
            "preserveThreadId": False,
        }
        preview_response = client.post(
            "/api/import/thread/preview",
            params={"codex_home": str(target_home)},
            headers=headers,
            json=request_body,
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()

        replace_one_byte_without_changing_size_or_mtime(source_rollouts["source-thread"])
        apply_response = client.post(
            "/api/import/thread",
            params={"codex_home": str(target_home)},
            headers=headers,
            json={
                **request_body,
                "acknowledgeCodexRunningRisk": True,
                "createBackup": False,
                "operationPreviewId": preview["operationPreviewId"],
                "inputHash": preview["inputHash"],
            },
        )

    assert_state_conflict(apply_response)


def test_project_import_ticket_binds_every_matching_rollout(tmp_path: Path) -> None:
    source_project_path = str(tmp_path / "source-project")
    source_home, source_rollouts = create_codex_home(
        tmp_path / "source",
        [
            ("source-thread-a", source_project_path, b'{"thread":"a","value":"xxxx"}\n'),
            ("source-thread-b", source_project_path, b'{"thread":"b","value":"xxxx"}\n'),
        ],
    )
    target_home, _ = create_codex_home(tmp_path / "target")

    with TestClient(server.app) as client:
        headers = authorization_headers(client, target_home)
        request_body = {
            "sourceCodexHome": str(source_home),
            "sourceProjectPath": source_project_path,
            "targetProjectPath": str(tmp_path / "target-project"),
            "includeArchived": False,
            "preserveThreadIds": False,
        }
        preview_response = client.post(
            "/api/import/project/preview",
            params={"codex_home": str(target_home)},
            headers=headers,
            json=request_body,
        )
        assert preview_response.status_code == 200, preview_response.text
        assert preview_response.json()["matchedThreads"] == 2
        preview = preview_response.json()

        replace_one_byte_without_changing_size_or_mtime(source_rollouts["source-thread-b"])
        apply_response = client.post(
            "/api/import/project",
            params={"codex_home": str(target_home)},
            headers=headers,
            json={
                **request_body,
                "acknowledgeCodexRunningRisk": True,
                "createBackup": False,
                "operationPreviewId": preview["operationPreviewId"],
                "inputHash": preview["inputHash"],
            },
        )

    assert_state_conflict(apply_response)


def test_capabilities_exposes_exact_frontend_contract_version() -> None:
    with TestClient(server.app) as client:
        response = client.get("/api/capabilities")
        openapi_response = client.get("/openapi.json")

    assert response.status_code == 200
    contract_version = response.json()["frontendContractVersion"]
    assert type(contract_version) is int
    assert contract_version == 2
    contract_schema = openapi_response.json()["components"]["schemas"]["CapabilitiesResponse"]["properties"][
        "frontendContractVersion"
    ]
    assert contract_schema["const"] == 2
