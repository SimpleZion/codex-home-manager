from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


project_path = Path(__file__).resolve().parents[1]
service_port = int(os.environ.get("CODEX_HOME_MANAGER_GATE_PORT", "8876"))
service_url = os.environ.get("CODEX_HOME_MANAGER_GATE_URL", f"http://127.0.0.1:{service_port}").rstrip("/")
ui_service_url = f"{service_url}/?api_base={urllib.parse.quote(service_url, safe='')}"
quality_gate_main_thread_count = 24
quality_gate_subagent_thread_count = 16
quality_gate_thread_count = quality_gate_main_thread_count + quality_gate_subagent_thread_count


def run_command(command: list[str]) -> None:
    print(" ".join(command))
    executable_path = shutil.which(command[0])
    if executable_path is None:
        raise RuntimeError(f"command executable not found: {command[0]}")
    completed_process = subprocess.run([executable_path, *command[1:]], cwd=project_path, check=False)
    if completed_process.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(command)}")


def request_json(
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 15,
) -> tuple[int, dict[str, Any]]:
    data = None
    request_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{service_url}{path}", data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        payload_text = error.read().decode("utf-8")
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = {"detail": payload_text}
        return error.code, payload


def wait_for_service() -> bool:
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            status_code, _payload = request_json("/api/capabilities")
            if status_code == 200:
                return True
        except Exception:
            time.sleep(0.5)
    return False


def create_quality_gate_codex_home(root_path: Path) -> Path:
    codex_home_path = root_path / ".codex"
    sessions_path = codex_home_path / "sessions"
    project_path = root_path / "quality-gate-project"
    sessions_path.mkdir(parents=True)
    project_path.mkdir()
    for directory_name in ("memories", "skills", "plugins"):
        (codex_home_path / directory_name).mkdir()

    thread_rows: list[tuple[str, Path, str, int, str]] = []
    for index in range(quality_gate_thread_count):
        thread_id = f"quality-gate-thread-{index}"
        title = f"Quality gate thread {index}"
        rollout_path = sessions_path / f"rollout-{thread_id}.jsonl"
        records = [
            {
                "timestamp": f"2026-07-15T00:{index:02d}:00Z",
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "cwd": str(project_path),
                    "model_provider": "openai",
                    "cli_version": "quality-gate",
                },
            },
            {
                "timestamp": f"2026-07-15T00:{index:02d}:10Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Quality gate prompt {index}"}],
                },
            },
            {
                "timestamp": f"2026-07-15T00:{index:02d}:20Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"Quality gate response {index}"}],
                },
            },
            {
                "timestamp": f"2026-07-15T00:{index:02d}:30Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": f"Quality gate prompt {index}"},
            },
            {
                "timestamp": f"2026-07-15T00:{index:02d}:40Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": f"Quality gate response {index}"},
            },
        ]
        rollout_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8",
        )
        thread_source = "subagent" if index >= quality_gate_main_thread_count else "cli"
        thread_rows.append((thread_id, rollout_path, title, index, thread_source))

    database_path = codex_home_path / "state_5.sqlite"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
                approval_mode TEXT NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
                has_user_event INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                archived_at INTEGER, git_sha TEXT, git_branch TEXT, git_origin_url TEXT,
                cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '',
                agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT 'enabled',
                model TEXT, reasoning_effort TEXT, agent_path TEXT, created_at_ms INTEGER,
                updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            )
            """
        )
        for thread_id, rollout_path, title, index, thread_source in thread_rows:
            timestamp_seconds = 1_752_537_600 + index
            connection.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived,
                    cli_version, first_user_message, memory_mode, model, reasoning_effort,
                    created_at_ms, updated_at_ms, thread_source, preview
                ) VALUES (?, ?, ?, ?, ?, 'openai', ?, ?, '{}', 'never', ?, 1, 0,
                    'quality-gate', ?, 'enabled', 'gpt-5', 'medium', ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    str(rollout_path),
                    timestamp_seconds,
                    timestamp_seconds,
                    thread_source,
                    str(project_path),
                    title,
                    (index + 1) * 1000,
                    f"Quality gate prompt {index}",
                    timestamp_seconds * 1000,
                    timestamp_seconds * 1000,
                    thread_source,
                    f"Quality gate preview {index}",
                ),
            )
        connection.executemany(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES (?, ?, ?)",
            [
                (thread_rows[0][0], child_thread_id, "completed")
                for child_thread_id, _rollout_path, _title, _index, thread_source in thread_rows
                if thread_source == "subagent"
            ],
        )
        connection.commit()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise RuntimeError(f"quality gate fixture database failed quick_check: {quick_check}")
    finally:
        connection.close()

    log_connection = sqlite3.connect(codex_home_path / "logs_2.sqlite")
    try:
        log_connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ts_nanos INTEGER NOT NULL,
                level TEXT NOT NULL,
                target TEXT NOT NULL,
                feedback_log_body TEXT,
                module_path TEXT,
                file TEXT,
                line INTEGER,
                thread_id TEXT,
                process_uuid TEXT,
                estimated_bytes INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        log_connection.execute("CREATE INDEX idx_logs_thread_id ON logs(thread_id)")
        log_connection.execute(
            "CREATE INDEX idx_logs_thread_id_ts ON logs(thread_id, ts DESC, ts_nanos DESC, id DESC)"
        )
        log_rows = []
        for thread_id, _rollout_path, _title, index, _thread_source in thread_rows:
            for severity_index, (level, target, body) in enumerate(
                (
                    ("INFO", "codex_app_server::thread_state", "app_server.request rpc.method=thread/read success=true"),
                    ("WARN", "codex_api::endpoint::responses_websocket", "stream_request retry after transient timeout"),
                    ("ERROR", "codex_api::endpoint::responses", "run_sampling_request failed with HTTP 500 fixture error"),
                )
            ):
                log_rows.append(
                    (
                        1_752_537_600 + index,
                        severity_index,
                        level,
                        target,
                        body,
                        target,
                        "quality_gate.rs",
                        10 + severity_index,
                        thread_id,
                        "quality-gate-process",
                        len(body),
                    )
                )
        log_connection.executemany(
            """
            INSERT INTO logs (
                ts, ts_nanos, level, target, feedback_log_body, module_path,
                file, line, thread_id, process_uuid, estimated_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            log_rows,
        )
        log_connection.commit()
        log_quick_check = log_connection.execute("PRAGMA quick_check").fetchone()
        if log_quick_check is None or log_quick_check[0] != "ok":
            raise RuntimeError(f"quality gate log fixture failed quick_check: {log_quick_check}")
    finally:
        log_connection.close()

    (codex_home_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "electron-saved-workspace-roots": [str(project_path)],
                "pinned-thread-ids": [thread_rows[0][0]],
                "thread-workspace-root-hints": {
                    thread_id: str(project_path) for thread_id, *_rest in thread_rows
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (codex_home_path / "config.toml").write_text(
        f"[projects.{json.dumps(str(project_path))}]\ntrusted = true\n",
        encoding="utf-8",
    )
    (codex_home_path / "session_index.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "id": thread_id,
                    "thread_name": title,
                    "updated_at": f"2026-07-15T00:{index:02d}:00Z",
                },
                ensure_ascii=False,
            )
            for thread_id, _rollout_path, title, index, _thread_source in thread_rows
        )
        + "\n",
        encoding="utf-8",
    )
    return codex_home_path


def ensure_service(codex_home_path: Path | None = None) -> subprocess.Popen[bytes] | None:
    if os.environ.get("CODEX_HOME_MANAGER_GATE_REUSE_SERVICE") == "1" and wait_for_service():
        return None
    if wait_for_service():
        raise RuntimeError(
            f"gate service url is already occupied: {service_url}. "
            "Set CODEX_HOME_MANAGER_GATE_PORT to a free port or CODEX_HOME_MANAGER_GATE_REUSE_SERVICE=1 to reuse it."
        )
    process_env = dict(os.environ)
    if codex_home_path is not None:
        process_env["CODEX_HOME"] = str(codex_home_path)
        process_env["CODEX_HOME_MANAGER_BACKUP_ROOT"] = str(codex_home_path.parent / "backups")
        process_env["CODEX_HOME_MANAGER_EXPORT_ROOT"] = str(codex_home_path.parent / "exports")
    process_env["CODEX_HOME_MANAGER_ALLOWED_ORIGINS"] = ",".join(
        origin
        for origin in [
            process_env.get("CODEX_HOME_MANAGER_ALLOWED_ORIGINS", "").strip(),
            service_url,
        ]
        if origin
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", str(service_port)],
        cwd=project_path,
        env=process_env,
    )
    if not wait_for_service():
        process.terminate()
        raise RuntimeError("service did not become healthy")
    return process


def assert_condition(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok: {message}")


def function_block(source_text: str, function_name: str) -> str:
    marker = f"def {function_name}("
    start_index = source_text.find(marker)
    if start_index < 0:
        raise AssertionError(f"function not found: {function_name}")
    next_endpoint_index = source_text.find("\n\n@app.", start_index + len(marker))
    static_index = source_text.find("\n\nstatic_directory", start_index + len(marker))
    candidates = [index for index in (next_endpoint_index, static_index) if index >= 0]
    end_index = min(candidates) if candidates else len(source_text)
    return source_text[start_index:end_index]


def assert_block_contains(source_text: str, function_name: str, required_fragments: list[str]) -> None:
    block = function_block(source_text, function_name)
    for fragment in required_fragments:
        assert_condition(fragment in block, f"{function_name} contains {fragment}")


def run_static_security_gate() -> None:
    server_text = (project_path / "backend" / "server.py").read_text(encoding="utf-8")
    assert_condition("write_lock = threading.RLock()" in server_text, "operation-level write lock is declared")
    assert_block_contains(
        server_text,
        "require_api_token",
        ["secrets.compare_digest", "api_token_header_name"],
    )
    assert_block_contains(
        server_text,
        "authorize_write_request",
        ["authorize_browser_write_origin", "require_api_token"],
    )
    assert_block_contains(
        server_text,
        "require_preview_ticket",
        ["preview_store", "canonical_payload_hash", "expected_hash", "operation preview input hash"],
    )
    for function_name in [
        "write_resource",
        "copy_resource",
        "import_thread_endpoint",
        "import_project_endpoint",
        "show_thread",
        "repair_thread_user_event",
        "archive_thread_endpoint",
        "duplicate_thread_endpoint",
        "migrate_thread_endpoint",
        "slim_thread_endpoint",
        "rename_project_endpoint",
        "restore_thread_backup",
    ]:
        assert_block_contains(function_name=function_name, source_text=server_text, required_fragments=[
            "authorize_write_request",
            "preview_bound_write",
        ])
    for function_name in ["backup_resource", "create_thread_backup", "export_prompts_endpoint"]:
        assert_block_contains(function_name=function_name, source_text=server_text, required_fragments=[
            "authorize_write_request",
            "with write_lock",
        ])
    for function_name in [
        "preview_write_resource",
        "preview_copy_resource",
        "preview_thread_action_endpoint",
        "preview_slim_thread_endpoint",
        "preview_migrate_thread_endpoint",
        "preview_import_thread_endpoint",
        "preview_import_project_endpoint",
        "preview_rename_project_endpoint",
        "preview_restore_thread_backup",
    ]:
        assert_block_contains(function_name=function_name, source_text=server_text, required_fragments=["create_preview_ticket"])


def run_static_browser_mode_gate() -> None:
    browser_home_text = (project_path / "src" / "browserHome.ts").read_text(encoding="utf-8")
    main_text = (project_path / "src" / "main.tsx").read_text(encoding="utf-8")
    assert_condition("extractThreadMetadata(parsed, line)" in browser_home_text, "browser folder scan parses thread metadata")
    assert_condition("threadSource.toLowerCase() === \"subagent\"" in browser_home_text, "browser folder scan detects explicit subagent thread_source")
    assert_condition("sourceSubagent" in browser_home_text and "threadSpawn" in browser_home_text, "browser folder scan detects subagent spawn metadata")
    assert_condition("subagent_title_heuristic" in browser_home_text, "browser folder scan keeps AGENTS.md subagent fallback")
    assert_condition("mainThreads: mainThreads.length" in browser_home_text, "browser folder summary separates main threads")
    assert_condition("subagentThreads: subagentThreads.length" in browser_home_text, "browser folder summary separates subagent threads")
    assert_condition("outsideInitialLimit: false" in browser_home_text, "browser folder does not treat legacy first-page rank as hidden")
    assert_condition("visibility: \"hidden_by_initial_limit\"" not in browser_home_text, "browser folder no longer emits hidden_by_initial_limit")
    assert_condition("scanBrowserPluginCache" in browser_home_text, "browser folder scans curated plugin cache")
    assert_condition("browser.plugin_cache" in browser_home_text, "browser folder diagnostics include plugin cache health")
    assert_condition("browser.plugin_cache_problem" in browser_home_text, "browser folder diagnostics report plugin cache problems")
    assert_condition("readOnlyMode={readOnlyMode}" in main_text, "thread table and detail panel receive read-only mode")
    assert_condition("!readOnlyMode && thread.outsideInitialLimit" not in main_text, "legacy first-page write action is removed")
    assert_condition("!readOnlyMode && thread.codexVisible" in main_text, "browser read-only mode hides hide-thread write action")
    assert_condition("browser-readonly-callout" in main_text, "browser read-only detail panel explains connector boundary")
    assert_condition("check.status === \"critical\" || check.status === \"warning\"" in main_text, "diagnostics attention filter excludes info-only checks")


def run_api_gate() -> None:
    status_code, token_payload = request_json("/api/auth/token")
    assert_condition(status_code == 200 and token_payload.get("token"), "auth token endpoint returns a token")
    token_headers = {token_payload.get("headerName", "X-Codex-Manager-Token"): token_payload["token"]}
    status_code, public_token_payload = request_json(
        "/api/auth/token",
        headers={"Origin": "https://codex-home-manager.simplezion.com"},
    )
    assert_condition(status_code == 403 and "loopback" in str(public_token_payload.get("detail")).lower(), "public origin cannot mint local authorization")
    status_code, public_intent_token_payload = request_json(
        "/api/auth/token",
        headers={
            "Origin": "https://codex-home-manager.simplezion.com",
            "X-Codex-Manager-Token-Intent": "interactive-write",
        },
    )
    assert_condition(status_code == 403 and not public_intent_token_payload.get("token"), "public origin cannot bypass authorization with a legacy intent header")
    status_code, loopback_token_payload = request_json(
        "/api/auth/token",
        headers={"Origin": service_url},
    )
    assert_condition(status_code == 200 and loopback_token_payload.get("token"), "loopback same-origin UI can mint local authorization")

    status_code, capabilities = request_json("/api/capabilities")
    assert_condition(status_code == 200, "capabilities endpoint returns 200")
    assert_condition(capabilities.get("openapiPath") == "/openapi.json", "capabilities exposes OpenAPI path")
    assert_condition(capabilities.get("mcpPath") == "/mcp", "capabilities exposes MCP path")
    assert_condition(len(capabilities.get("capabilities", [])) >= 22, "capabilities lists agent-callable actions")
    safety_model = capabilities.get("safetyModel", {})
    protected_paths = safety_model.get("protectedTextWritePaths", [])
    assert_condition(".codex-global-state.json" in protected_paths, "global state is listed as protected")
    assert_condition("config.toml" in protected_paths, "config is listed as protected")
    assert_condition(
        "acknowledgeCodexRunningRisk" in str(safety_model.get("runningCodexWriteGate", "")),
        "running Codex write gate is documented",
    )
    assert_condition(
        "X-Codex-Manager-Token" in str(safety_model.get("authorization", "")),
        "write token authorization is documented",
    )
    assert_condition(
        "operationPreviewId" in str(safety_model.get("previewBinding", "")),
        "preview binding is documented",
    )
    assert_condition(
        "/mcp" in str(safety_model.get("mcp", "")),
        "MCP safety model is documented",
    )

    status_code, mcp_metadata = request_json("/mcp")
    assert_condition(status_code == 200 and mcp_metadata.get("endpoint") == "/mcp", "MCP metadata endpoint returns /mcp")
    assert_condition("codex_snapshot" in mcp_metadata.get("tools", []), "MCP metadata lists codex_snapshot")
    status_code, mcp_initialize = request_json(
        "/mcp",
        method="POST",
        body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert_condition(status_code == 200 and mcp_initialize.get("result", {}).get("serverInfo", {}).get("name") == "codex-home-manager", "MCP initialize returns server info")
    status_code, mcp_tools = request_json(
        "/mcp",
        method="POST",
        body={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    mcp_tool_names = {tool.get("name") for tool in mcp_tools.get("result", {}).get("tools", [])}
    assert_condition(status_code == 200 and "codex_show_thread" in mcp_tool_names, "MCP tools/list includes write tools")
    assert_condition("codex_write_resource" in mcp_tool_names, "MCP tools/list includes resource write tool")

    status_code, openapi_schema = request_json("/openapi.json")
    assert_condition(status_code == 200, "OpenAPI endpoint returns 200")
    assert_condition("/api/import/thread/preview" in openapi_schema.get("paths", {}), "OpenAPI includes import thread preview")
    write_request_schema = openapi_schema.get("components", {}).get("schemas", {}).get("WriteResourceRequest", {})
    assert_condition(
        "acknowledgeCodexRunningRisk" in write_request_schema.get("properties", {}),
        "OpenAPI includes write acknowledgement field",
    )

    status_code, payload = request_json(
        "/api/resources/copy-from-home/preview",
        method="POST",
        headers=token_headers,
        body={
            "sourceCodexHome": "D:\\.codex",
            "relativePath": ".codex-global-state.json",
            "targetRelativePath": ".codex-global-state.json",
        },
    )
    assert_condition(status_code == 400 and "protected Codex state file" in str(payload.get("detail")), "protected global state copy is rejected")

    status_code, payload = request_json(
        "/api/resources/write/preview",
        method="POST",
        headers=token_headers,
        body={
            "relativePath": ".codex-global-state.json",
            "content": "{}",
        },
    )
    assert_condition(status_code == 400 and "protected Codex state file" in str(payload.get("detail")), "protected global state write preview is rejected")

    status_code, health = request_json("/api/health", headers=token_headers)
    assert_condition(status_code == 200, "health endpoint returns 200")
    status_code, diagnostics = request_json("/api/diagnostics?lang=en", headers=token_headers, timeout_seconds=120)
    assert_condition(status_code == 200, "diagnostics endpoint returns 200")
    assert_condition(
        "score" in diagnostics and "issues" in diagnostics and "checks" in diagnostics and "repairPrompt" in diagnostics,
        "diagnostics returns score, issues, checks, and repair prompt",
    )
    repair_prompt = str(diagnostics.get("repairPrompt") or "")
    assert_condition("You are Codex" in repair_prompt and "CODEX_HOME" in repair_prompt, "diagnostics repair prompt is ready for Codex")
    assert_condition(any(check.get("id") == "sqlite.state" for check in diagnostics.get("checks", [])), "diagnostics includes SQLite state check")
    assert_condition(any(check.get("id") == "plugins.curated_runtime_links" for check in diagnostics.get("checks", [])), "diagnostics includes curated runtime link check")
    assert_condition(any(check.get("id") == "config.toml_parse" for check in diagnostics.get("checks", [])), "diagnostics includes config TOML parse check")
    assert_condition(any(check.get("id") == "sandbox.setup_state" for check in diagnostics.get("checks", [])), "diagnostics includes sandbox setup state check")
    assert_condition(any(check.get("id") == "environment.user_codex_home" for check in diagnostics.get("checks", [])), "diagnostics includes user CODEX_HOME check")
    status_code, snapshot = request_json("/api/snapshot?sidebar_limit=50", headers=token_headers, timeout_seconds=90)
    assert_condition(status_code == 200 and snapshot.get("threads"), "snapshot returns threads for write-gate smoke")
    main_thread = next(
        (thread for thread in snapshot["threads"] if thread.get("threadKind") == "main"),
        None,
    )
    assert_condition(main_thread is not None, "snapshot returns a main thread for write-gate smoke")
    thread_id = main_thread["id"]

    status_code, mcp_missing_token = request_json(
        "/mcp",
        method="POST",
        body={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "codex_show_thread",
                "arguments": {"threadId": thread_id, "operationPreviewId": "missing", "inputHash": "missing"},
            },
        },
    )
    mcp_missing_token_result = mcp_missing_token.get("result", {})
    assert_condition(
        status_code == 200
        and mcp_missing_token_result.get("isError") is True
        and mcp_missing_token_result.get("structuredContent", {}).get("status") == 401,
        "MCP write without token returns tool error",
    )

    status_code, payload = request_json(f"/api/threads/{thread_id}/show", method="POST")
    assert_condition(status_code == 401 and "X-Codex-Manager-Token" in str(payload.get("detail")), "write without token returns 401")

    status_code, payload = request_json(
        f"/api/threads/{thread_id}/show?acknowledgeCodexRunningRisk=true",
        method="POST",
        headers=token_headers,
    )
    assert_condition(status_code == 428 and "operationPreviewId" in str(payload.get("detail")), "write without preview ticket returns 428")

    hostile_origin_headers = {**token_headers, "Origin": "https://example.invalid"}
    status_code, payload = request_json(
        f"/api/threads/{thread_id}/show?acknowledgeCodexRunningRisk=true",
        method="POST",
        headers=hostile_origin_headers,
    )
    assert_condition(status_code == 403 and "origin" in str(payload.get("detail")).lower(), "write with hostile browser Origin returns 403")

    write_warnings = health.get("writeWarnings") or []
    if write_warnings:
        status_code, preview = request_json(
            f"/api/threads/{thread_id}/action-preview?action=show", headers=token_headers
        )
        assert_condition(status_code == 200 and preview.get("operationPreviewId"), "thread action preview returns a ticket")
        status_code, payload = request_json(
            f"/api/threads/{thread_id}/show?operationPreviewId={preview['operationPreviewId']}&inputHash={preview['inputHash']}",
            method="POST",
            headers=token_headers,
        )
        assert_condition(status_code == 409 and "acknowledgeCodexRunningRisk" in str(payload.get("detail")), "write without acknowledgement returns 409 while Codex is running")
    else:
        print("skip: no running Codex process warning, live 409 write-gate check not attempted")


def main() -> int:
    service_process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory(prefix="codex-home-manager-quality-gate-") as temporary_directory:
        try:
            run_static_security_gate()
            run_static_browser_mode_gate()
            run_command(["npm", "run", "build"])
            run_command([sys.executable, "-m", "pytest", "tests"])
            run_command(["npm", "audit", "--audit-level=moderate"])
            fixture_codex_home = create_quality_gate_codex_home(Path(temporary_directory))
            service_process = ensure_service(fixture_codex_home)
            run_command(["node", "scripts/ui_overflow_check.mjs", ui_service_url])
            run_api_gate()
        finally:
            if service_process is not None:
                service_process.terminate()
                service_process.wait(timeout=10)
    print("quality gate PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
