from backend.server import mcp_tool_definitions


def test_provider_interoperability_mcp_surface_is_complete() -> None:
    tools = {tool["name"]: tool for tool in mcp_tool_definitions()}
    expected = {
        "codex_provider_interoperability_status",
        "codex_provider_interoperability_audit",
        "codex_audit_provider_interoperability_task",
        "codex_preview_provider_interoperability_task_repair",
        "codex_repair_provider_interoperability_task",
        "codex_preview_provider_interoperability_repair",
        "codex_repair_provider_interoperability",
        "codex_verify_provider_interoperability",
        "codex_preview_provider_interoperability_rollback",
        "codex_rollback_provider_interoperability",
    }
    assert expected <= tools.keys()
    repair_required = set(tools["codex_repair_provider_interoperability"]["inputSchema"]["required"])
    assert {"apiToken", "operationPreviewId", "inputHash", "operationId", "workerStateDigest"} <= repair_required
    task_repair_required = set(
        tools["codex_repair_provider_interoperability_task"]["inputSchema"]["required"]
    )
    assert {"apiToken", "threadId", "operationPreviewId", "inputHash"} <= task_repair_required
    assert tools["codex_provider_interoperability_status"]["inputSchema"]["properties"]["apiToken"]
