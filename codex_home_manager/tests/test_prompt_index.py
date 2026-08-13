from __future__ import annotations

import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from backend import server
from backend import prompt_index as prompt_index_module
from backend.codex_data import export_thread_prompts, read_thread_prompt_page
from backend.prompt_index import (
    PromptIndexCancelled,
    begin_prompt_index_request,
    cleanup_prompt_indexes,
    finish_prompt_index_request,
    iter_prompt_records,
    prompt_index_database_path,
    prompt_index_root_path,
)


def create_prompt_test_home(root_path: Path, records: list[dict[str, object]] | None = None) -> tuple[Path, Path]:
    codex_home_path = root_path / "codex-home"
    sessions_path = codex_home_path / "sessions"
    sessions_path.mkdir(parents=True)
    rollout_path = sessions_path / "rollout-thread-1.jsonl"
    with rollout_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(
            json.dumps(
                {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(root_path / "project")}},
                ensure_ascii=False,
            )
            + "\n"
        )
        for record in records or []:
            output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    with closing(sqlite3.connect(codex_home_path / "state_5.sqlite")) as connection, connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT)")
        connection.execute(
            "INSERT INTO threads(id, rollout_path, cwd, title) VALUES (?, ?, ?, ?)",
            ("thread-1", str(rollout_path), str(root_path / "project"), "Prompt index test"),
        )
    return codex_home_path, rollout_path


def user_record(text: str, index: int = 0) -> dict[str, object]:
    return {
        "type": "response_item",
        "timestamp": f"2026-08-14T00:{index % 60:02d}:00Z",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


@pytest.fixture(autouse=True)
def isolate_prompt_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME_MANAGER_PROMPT_INDEX_ROOT", str(tmp_path / "prompt-indexes"))


def test_ten_thousand_prompts_page_search_and_incremental_tail_scan(tmp_path: Path) -> None:
    codex_home_path, rollout_path = create_prompt_test_home(tmp_path)
    with rollout_path.open("a", encoding="utf-8", newline="\n") as output:
        for index in range(10_000):
            marker = " unique-search-marker" if index == 8_765 else ""
            output.write(
                json.dumps(user_record(f"bulk prompt {index:05d}{marker}", index), ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )

    process = psutil.Process()
    rss_before = process.memory_info().rss
    started_at = time.perf_counter()
    first_page = read_thread_prompt_page(
        str(codex_home_path),
        "thread-1",
        scope="all",
        limit=73,
        scan_budget_ms=5_000,
    )
    elapsed = time.perf_counter() - started_at
    rss_growth = max(0, process.memory_info().rss - rss_before)

    assert elapsed < 10
    assert rss_growth < 128 * 1024 * 1024
    assert first_page["index"]["complete"] is True
    assert first_page["promptCount"] == 10_000
    assert len(first_page["prompts"]) == 73
    assert first_page["prompts"][0]["text"] == "bulk prompt 00000"
    assert first_page["nextCursor"]

    second_page = read_thread_prompt_page(
        str(codex_home_path),
        "thread-1",
        scope="all",
        cursor=first_page["nextCursor"],
        limit=73,
        scan_budget_ms=50,
    )
    assert second_page["prompts"][0]["index"] == 74
    assert second_page["prompts"][0]["text"] == "bulk prompt 00073"

    search_page = read_thread_prompt_page(
        str(codex_home_path),
        "thread-1",
        scope="all",
        search="UNIQUE-search-marker",
        limit=10,
        scan_budget_ms=50,
    )
    assert search_page["matchCount"] == 1
    assert search_page["matchCountComplete"] is True
    assert search_page["prompts"][0]["index"] == 8_766

    old_size = rollout_path.stat().st_size
    with rollout_path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(user_record("appended prompt", 1), ensure_ascii=False, separators=(",", ":")) + "\n")
    appended_page = read_thread_prompt_page(
        str(codex_home_path),
        "thread-1",
        scope="all",
        search="appended prompt",
        limit=10,
        scan_budget_ms=1_000,
    )
    assert appended_page["index"]["reset"] is False
    assert appended_page["index"]["scanStartOffset"] == old_size
    assert appended_page["index"]["scanAddedPrompts"] == 1
    assert appended_page["promptCount"] == 10_001


def test_prompt_index_schema_upgrade_rebuilds_stale_derived_rows(tmp_path: Path) -> None:
    codex_home_path, _ = create_prompt_test_home(tmp_path, [user_record("schema upgrade prompt")])
    first_page = read_thread_prompt_page(str(codex_home_path), "thread-1", scope="all", scan_budget_ms=1_000)
    assert [prompt["text"] for prompt in first_page["prompts"]] == ["schema upgrade prompt"]

    database_path = prompt_index_database_path(codex_home_path)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("DELETE FROM prompts")
        connection.execute("PRAGMA user_version=1")

    rebuilt_page = read_thread_prompt_page(str(codex_home_path), "thread-1", scope="all", scan_budget_ms=1_000)
    assert [prompt["text"] for prompt in rebuilt_page["prompts"]] == ["schema upgrade prompt"]
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_two_gib_sparse_rollout_has_bounded_latency_and_memory(tmp_path: Path) -> None:
    codex_home_path, rollout_path = create_prompt_test_home(tmp_path, [user_record("prompt before sparse hole")])
    with rollout_path.open("r+b") as output:
        output.seek(2 * 1024 * 1024 * 1024 - 1)
        output.write(b"\n")

    process = psutil.Process()
    rss_before = process.memory_info().rss
    started_at = time.perf_counter()
    page = read_thread_prompt_page(
        str(codex_home_path),
        "thread-1",
        scope="all",
        limit=10,
        scan_budget_ms=20,
    )
    elapsed = time.perf_counter() - started_at
    rss_growth = max(0, process.memory_info().rss - rss_before)

    assert rollout_path.stat().st_size >= 2 * 1024 * 1024 * 1024
    assert elapsed < 2
    assert rss_growth < 64 * 1024 * 1024
    assert page["index"]["complete"] is False
    assert page["index"]["scannedBytes"] < page["index"]["fileSize"]
    assert [prompt["text"] for prompt in page["prompts"]] == ["prompt before sparse hole"]

    checks = 0

    def cancel_soon() -> bool:
        nonlocal checks
        checks += 1
        return checks > 3

    with pytest.raises(PromptIndexCancelled):
        read_thread_prompt_page(
            str(codex_home_path),
            "thread-1",
            scope="all",
            limit=10,
            scan_budget_ms=5_000,
            cancel_check=cancel_soon,
        )


def test_prompt_page_redacts_data_urls_and_rejects_stale_cursor_after_rewrite(tmp_path: Path) -> None:
    records = [
        user_record("inspect data:image/png;base64,AAAA and keep alpha"),
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": "data:image/png;base64,BBBB"}],
            },
        },
    ]
    codex_home_path, rollout_path = create_prompt_test_home(tmp_path, records)
    first_page = read_thread_prompt_page(
        str(codex_home_path), "thread-1", scope="all", limit=1, scan_budget_ms=1_000
    )
    assert "data:image" not in first_page["prompts"][0]["text"]
    assert "[附件内容已隐藏]" in first_page["prompts"][0]["text"]
    old_cursor = first_page["nextCursor"]
    assert old_cursor

    original_bytes = rollout_path.read_bytes()
    rewritten_bytes = original_bytes.replace(b"alpha", b"bravo")
    assert len(rewritten_bytes) == len(original_bytes)
    rollout_path.write_bytes(rewritten_bytes)
    current_stat = rollout_path.stat()
    os.utime(rollout_path, ns=(current_stat.st_atime_ns, current_stat.st_mtime_ns + 1_000_000_000))

    with pytest.raises(ValueError, match="cursor is stale"):
        read_thread_prompt_page(
            str(codex_home_path),
            "thread-1",
            scope="all",
            cursor=old_cursor,
            limit=1,
            scan_budget_ms=1_000,
        )
    rewritten_page = read_thread_prompt_page(
        str(codex_home_path), "thread-1", scope="all", limit=10, scan_budget_ms=1_000
    )
    assert any("bravo" in prompt["text"] for prompt in rewritten_page["prompts"])


def test_large_data_url_prompt_is_redacted_before_json_materialization(tmp_path: Path) -> None:
    codex_home_path, rollout_path = create_prompt_test_home(tmp_path)
    prefix = (
        b'{"type":"response_item","payload":{"type":"message","role":"user","content":'
        b'[{"type":"input_text","text":"before data:image/png;base64,'
    )
    suffix = b' after"}]}}\n'
    with rollout_path.open("ab") as output:
        output.write(prefix)
        for _ in range(12):
            output.write(b"A" * 1024 * 1024)
        output.write(suffix)

    process = psutil.Process()
    rss_before = process.memory_info().rss
    page = read_thread_prompt_page(
        str(codex_home_path), "thread-1", scope="all", limit=10, scan_budget_ms=5_000
    )
    rss_growth = max(0, process.memory_info().rss - rss_before)

    assert rss_growth < 64 * 1024 * 1024
    assert page["index"]["complete"] is True
    assert page["prompts"][0]["text"] == "before [附件内容已隐藏] after"
    assert "AAAA" not in page["prompts"][0]["text"]


def test_prompt_page_and_streaming_copy_api_contracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home_path, _ = create_prompt_test_home(
        tmp_path,
        [user_record("first API prompt"), user_record("second searchable API prompt", 1)],
    )
    client = TestClient(server.app)
    token_payload = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)}).json()
    headers = {token_payload["headerName"]: token_payload["token"]}

    page_response = client.get(
        "/api/threads/thread-1/prompts/page",
        params={
            "codex_home": str(codex_home_path),
            "scope": "all",
            "search": "searchable",
            "requestId": "page-contract-request",
            "scanBudgetMs": 1_000,
        },
        headers=headers,
    )
    assert page_response.status_code == 200
    page = page_response.json()
    assert page["requestId"] == "page-contract-request"
    assert page["matchCount"] == 1
    assert page["matchCountComplete"] is True
    assert page["prompts"][0]["text"] == "second searchable API prompt"

    copy_response = client.get(
        "/api/threads/thread-1/prompts/copy",
        params={"codex_home": str(codex_home_path), "scope": "pure", "format": "jsonl"},
        headers=headers,
    )
    assert copy_response.status_code == 200
    copied_prompts = [json.loads(line) for line in copy_response.text.splitlines()]
    assert [item["exportText"] for item in copied_prompts] == ["first API prompt", "second searchable API prompt"]
    assert copy_response.headers["x-prompt-request-id"]

    export_root = tmp_path / "exports"
    monkeypatch.setenv("CODEX_HOME_MANAGER_EXPORT_ROOT", str(export_root))
    json_export = export_thread_prompts(str(codex_home_path), "thread-1", output_format="json", scope="all")
    exported_payload = json.loads(Path(json_export["outputPath"]).read_text(encoding="utf-8"))
    assert exported_payload["promptCount"] == 2
    assert [item["text"] for item in exported_payload["prompts"]] == [
        "first API prompt",
        "second searchable API prompt",
    ]

    request_id, _ = begin_prompt_index_request("thread-1", "cancel-contract-request")
    try:
        cancel_response = client.delete(
            f"/api/threads/thread-1/prompts/requests/{request_id}",
            params={"codex_home": str(codex_home_path)},
            headers=headers,
        )
        assert cancel_response.status_code == 200
        assert cancel_response.json()["cancelled"] is True
    finally:
        finish_prompt_index_request(request_id)


def test_prompt_page_http_request_can_be_cancelled_during_sparse_scan(tmp_path: Path) -> None:
    codex_home_path, rollout_path = create_prompt_test_home(tmp_path, [user_record("before cancellation")])
    with rollout_path.open("r+b") as output:
        output.seek(2 * 1024 * 1024 * 1024 - 1)
        output.write(b"\n")
    controlling_client = TestClient(server.app)
    token_payload = controlling_client.get(
        "/api/auth/token", params={"codex_home": str(codex_home_path)}
    ).json()
    headers = {token_payload["headerName"]: token_payload["token"]}
    request_id = "http-cancel-sparse-request"

    def run_page_request():
        with TestClient(server.app) as page_client:
            return page_client.get(
                "/api/threads/thread-1/prompts/page",
                params={
                    "codex_home": str(codex_home_path),
                    "scope": "all",
                    "requestId": request_id,
                    "scanBudgetMs": 5_000,
                },
                headers=headers,
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        page_future = executor.submit(run_page_request)
        cancelled = False
        for _ in range(50):
            cancel_response = controlling_client.delete(
                f"/api/threads/thread-1/prompts/requests/{request_id}",
                params={"codex_home": str(codex_home_path)},
                headers=headers,
            )
            if cancel_response.json()["cancelled"]:
                cancelled = True
                break
            time.sleep(0.02)
        page_response = page_future.result(timeout=10)

    assert cancelled is True
    assert page_response.status_code == 499
    assert page_response.json()["detail"] == "prompt indexing was cancelled"


def test_prompt_index_status_and_clear_api_do_not_disclose_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_prompt = "private-prompt-marker-that-must-not-leak"
    monkeypatch.setenv("CODEX_HOME_MANAGER_PROMPT_INDEX_MAX_TOTAL_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("CODEX_HOME_MANAGER_PROMPT_INDEX_MAX_IDLE_SECONDS", "120")
    codex_home_path, rollout_path = create_prompt_test_home(tmp_path, [user_record(secret_prompt)])
    read_thread_prompt_page(str(codex_home_path), "thread-1", scope="all", scan_budget_ms=1_000)
    database_path = prompt_index_database_path(codex_home_path)
    assert database_path.is_file()

    client = TestClient(server.app)
    token_payload = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)}).json()
    headers = {token_payload["headerName"]: token_payload["token"]}
    status_response = client.get(
        "/api/prompt-index/status",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
    )

    assert status_response.status_code == 200
    status = status_response.json()
    assert status["databaseExists"] is True
    assert status["database"]["promptCount"] == 1
    assert status["database"]["readable"] is True
    assert status["database"]["inspectionState"] == "available"
    assert status["storage"]["rootPath"] == str(prompt_index_root_path())
    assert status["storage"]["maxTotalBytes"] == 2 * 1024 * 1024
    assert status["storage"]["maxIdleSeconds"] == 120
    serialized_status = json.dumps(status, ensure_ascii=False)
    assert secret_prompt not in serialized_status
    assert str(rollout_path) not in serialized_status
    assert "rolloutPath" not in serialized_status

    with TestClient(server.app) as unauthenticated_client:
        unauthenticated_preview = unauthenticated_client.post(
            "/api/prompt-index/clear/preview",
            params={"codex_home": str(codex_home_path)},
        )
    assert unauthenticated_preview.status_code == 401

    preview_response = client.post(
        "/api/prompt-index/clear/preview",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["willClear"] is True
    assert preview["reclaimableBytes"] > 0
    assert secret_prompt not in json.dumps(preview, ensure_ascii=False)

    missing_ticket_response = client.post(
        "/api/prompt-index/clear",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json={},
    )
    assert missing_ticket_response.status_code == 428

    request_body = {
        "operationPreviewId": preview["operationPreviewId"],
        "inputHash": preview["inputHash"],
    }
    clear_response = client.post(
        "/api/prompt-index/clear",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=request_body,
    )
    assert clear_response.status_code == 200
    assert clear_response.json()["cleared"] is True
    assert rollout_path.is_file()
    assert not database_path.exists()

    replay_response = client.post(
        "/api/prompt-index/clear",
        params={"codex_home": str(codex_home_path)},
        headers=headers,
        json=request_body,
    )
    assert replay_response.status_code == 428


def test_prompt_index_cleanup_removes_orphans_idle_and_capacity_overflow(tmp_path: Path) -> None:
    root_path = tmp_path / "prompt-indexes"
    orphan_home, orphan_rollout = create_prompt_test_home(tmp_path / "orphan", [user_record("orphan")])
    idle_home, _ = create_prompt_test_home(tmp_path / "idle", [user_record("idle")])
    old_home, _ = create_prompt_test_home(tmp_path / "old", [user_record("old")])
    recent_home, _ = create_prompt_test_home(tmp_path / "recent", [user_record("recent")])
    for codex_home_path in (orphan_home, idle_home, old_home, recent_home):
        read_thread_prompt_page(str(codex_home_path), "thread-1", scope="all", scan_budget_ms=1_000)

    orphan_database = prompt_index_database_path(orphan_home)
    idle_database = prompt_index_database_path(idle_home)
    old_database = prompt_index_database_path(old_home)
    recent_database = prompt_index_database_path(recent_home)
    orphan_rollout.unlink()
    now_ns = time.time_ns()
    with closing(sqlite3.connect(idle_database)) as connection, connection:
        connection.execute(
            "UPDATE prompt_index_metadata SET last_accessed_ns = ? WHERE singleton = 1",
            (now_ns - 120 * 1_000_000_000,),
        )

    lifecycle_cleanup = cleanup_prompt_indexes(
        root_path=root_path,
        max_total_bytes=1024 * 1024 * 1024,
        max_idle_seconds=60,
        now_ns=now_ns,
    )
    assert lifecycle_cleanup["purgedMissingRollouts"] == 1
    assert lifecycle_cleanup["deletedDatabases"] == 2
    assert not orphan_database.exists()
    assert not idle_database.exists()

    for database_path, last_accessed_ns in (
        (old_database, now_ns - 30 * 1_000_000_000),
        (recent_database, now_ns - 5 * 1_000_000_000),
    ):
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute("CREATE TABLE capacity_padding (payload BLOB NOT NULL)")
            connection.execute("INSERT INTO capacity_padding(payload) VALUES (zeroblob(?))", (2 * 1024 * 1024,))
            connection.execute(
                "UPDATE prompt_index_metadata SET last_accessed_ns = ? WHERE singleton = 1",
                (last_accessed_ns,),
            )
    old_size = old_database.stat().st_size
    recent_size = recent_database.stat().st_size
    capacity_cleanup = cleanup_prompt_indexes(
        root_path=root_path,
        max_total_bytes=recent_size + 128 * 1024,
        max_idle_seconds=3600,
        now_ns=now_ns,
    )
    assert old_size > 0
    assert capacity_cleanup["deletedDatabases"] == 1
    assert not old_database.exists()
    assert recent_database.exists()
    assert capacity_cleanup["overCapacity"] is False


def test_prompt_index_automatic_cleanup_purges_missing_rollout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan_home, orphan_rollout = create_prompt_test_home(tmp_path / "orphan", [user_record("orphan")])
    active_home, _ = create_prompt_test_home(tmp_path / "active", [user_record("active")])
    for codex_home_path in (orphan_home, active_home):
        read_thread_prompt_page(str(codex_home_path), "thread-1", scope="all", scan_budget_ms=1_000)
    orphan_database = prompt_index_database_path(orphan_home)
    active_database = prompt_index_database_path(active_home)
    orphan_rollout.unlink()
    monkeypatch.setattr(prompt_index_module, "prompt_index_cleanup_interval_seconds", 0)

    page = read_thread_prompt_page(str(active_home), "thread-1", scope="all", scan_budget_ms=1_000)

    assert page["promptCount"] == 1
    assert not orphan_database.exists()
    assert active_database.exists()


def test_prompt_index_cleanup_skips_database_held_by_streaming_reader(tmp_path: Path) -> None:
    codex_home_path, rollout_path = create_prompt_test_home(
        tmp_path,
        [user_record("first"), user_record("second", 1)],
    )
    read_thread_prompt_page(str(codex_home_path), "thread-1", scope="all", scan_budget_ms=1_000)
    database_path = prompt_index_database_path(codex_home_path)
    records = iter_prompt_records(database_path, rollout_path, scope="all", fetch_size=1)
    assert next(records)["text"] == "first"

    cleanup_result = cleanup_prompt_indexes(
        root_path=database_path.parent,
        max_total_bytes=1024 * 1024 * 1024,
        max_idle_seconds=60,
        now_ns=time.time_ns() + 120 * 1_000_000_000,
    )
    assert cleanup_result["skippedInUse"] == 1
    assert database_path.exists()

    records.close()
    cleanup_after_close = cleanup_prompt_indexes(
        root_path=database_path.parent,
        max_total_bytes=1024 * 1024 * 1024,
        max_idle_seconds=60,
        now_ns=time.time_ns() + 120 * 1_000_000_000,
    )
    assert cleanup_after_close["deletedDatabases"] == 1
    assert not database_path.exists()


def test_prompt_index_mcp_and_capability_metadata_are_explicit(tmp_path: Path) -> None:
    codex_home_path, _ = create_prompt_test_home(tmp_path, [user_record("metadata secret")])
    read_thread_prompt_page(str(codex_home_path), "thread-1", scope="all", scan_budget_ms=1_000)
    client = TestClient(server.app)
    token_payload = client.get("/api/auth/token", params={"codex_home": str(codex_home_path)}).json()
    api_token = token_payload["token"]

    tools_response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ).json()
    tools = {tool["name"]: tool for tool in tools_response["result"]["tools"]}
    assert {
        "codex_prompt_index_status",
        "codex_preview_clear_prompt_index",
        "codex_clear_prompt_index",
    } <= tools.keys()
    clear_tool = tools["codex_clear_prompt_index"]
    assert {"apiToken", "operationPreviewId", "inputHash"} <= set(clear_tool["inputSchema"]["required"])
    assert "no cache backup" in clear_tool["description"]
    assert "prompt text" in tools["codex_prompt_index_status"]["description"]

    status_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "codex_prompt_index_status",
                "arguments": {"codexHome": str(codex_home_path), "apiToken": api_token},
            },
        },
    ).json()
    structured_status = status_response["result"]["structuredContent"]
    assert structured_status["database"]["promptCount"] == 1
    assert "metadata secret" not in json.dumps(structured_status, ensure_ascii=False)

    preview_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "codex_preview_clear_prompt_index",
                "arguments": {"codexHome": str(codex_home_path), "apiToken": api_token},
            },
        },
    ).json()
    preview = preview_response["result"]["structuredContent"]
    clear_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "codex_clear_prompt_index",
                "arguments": {
                    "codexHome": str(codex_home_path),
                    "apiToken": api_token,
                    "operationPreviewId": preview["operationPreviewId"],
                    "inputHash": preview["inputHash"],
                },
            },
        },
    ).json()
    assert clear_response["result"]["structuredContent"]["cleared"] is True
    assert not prompt_index_database_path(codex_home_path).exists()

    capabilities = client.get("/api/capabilities").json()["capabilities"]
    capabilities_by_name = {capability["name"]: capability for capability in capabilities}
    clear_capability = capabilities_by_name["clear_prompt_index"]
    assert clear_capability["previewEndpoint"] == "/api/prompt-index/clear/preview"
    assert clear_capability["rollback"] is None
    assert clear_capability["riskLevel"] == "write"


def test_prompt_index_module_forbids_whole_file_mapping() -> None:
    source_text = (Path(__file__).resolve().parents[1] / "backend" / "prompt_index.py").read_text(encoding="utf-8")
    assert "import mmap" not in source_text
    assert ".read_bytes(" not in source_text
    assert "prompt_index_scan_chunk_bytes" in source_text
    assert "fetchmany(" in source_text
