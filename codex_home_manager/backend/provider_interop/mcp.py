from __future__ import annotations

from collections.abc import Callable
from typing import Any


def provider_interop_mcp_tool_definitions(
    *,
    build_tool: Callable[..., dict[str, Any]],
    preview_properties: Callable[[dict[str, Any] | None], dict[str, Any]],
    write_properties: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> list[dict[str, Any]]:
    adapter = {"adapterRoot": {"type": "string", "description": "Optional CC Switch state root."}}
    buckets = {
        "sourceBucket": {"type": "string", "enum": ["openai"], "default": "openai"},
        "targetBucket": {"type": "string", "enum": ["custom"], "default": "custom"},
    }
    scopes = {
        "history": {"type": "boolean", "default": True},
        "continuationCompatibility": {"type": "boolean", "default": True},
        "commonConfig": {"type": "boolean", "default": True},
        "interoperabilitySettings": {"type": "boolean", "default": True},
    }
    repair_binding = {
        "operationId": {"type": "string"},
        "workerStateDigest": {"type": "string", "minLength": 64, "maxLength": 64},
        "backupRoot": {"type": "string"},
    }
    rollback = {
        "manifestPath": {"type": "string"},
        "operationId": {"type": "string"},
        "workerStateDigest": {"type": "string"},
    }
    task = {"threadId": {"type": "string", "description": "Codex task UUID."}}
    return [
        build_tool(
            "codex_provider_interoperability_status",
            "Inspect the effective Codex Home, provider route, history buckets, authentication mode, and supported external manager without exposing credentials.",
            preview_properties(adapter),
        ),
        build_tool(
            "codex_provider_interoperability_audit",
            "Run a complete read-only Rust audit of provider history and preservation fingerprints.",
            preview_properties({**adapter, **buckets, **scopes}),
        ),
        build_tool(
            "codex_audit_provider_interoperability_task",
            "Check shared visibility and cross-provider continuation compatibility for one Codex task without reading secret values.",
            preview_properties(task),
            ["threadId"],
        ),
        build_tool(
            "codex_preview_provider_interoperability_task_repair",
            "Preview an offline, task-scoped active-context compatibility repair without scanning unrelated rollout bodies.",
            preview_properties({**task, **adapter}),
            ["threadId"],
        ),
        build_tool(
            "codex_repair_provider_interoperability_task",
            "Apply a preview-bound task compatibility repair with an exact rollback pre-image.",
            write_properties({**task, **adapter, **repair_binding}),
            ["apiToken", "threadId", "operationPreviewId", "inputHash", "operationId", "workerStateDigest"],
        ),
        build_tool(
            "codex_preview_provider_interoperability_repair",
            "Preview provider history repair, backup bytes, process blockers, and state binding without writing.",
            preview_properties({**adapter, **buckets, **scopes}),
        ),
        build_tool(
            "codex_repair_provider_interoperability",
            "Apply a preview-bound offline provider history repair with verified backups and automatic rollback.",
            write_properties({**adapter, **buckets, **scopes, **repair_binding}),
            ["apiToken", "operationPreviewId", "inputHash", "operationId", "workerStateDigest"],
        ),
        build_tool(
            "codex_verify_provider_interoperability",
            "Repeat provider status and full history audit after repair or a user-driven provider switch.",
            preview_properties({**adapter, **buckets, **scopes}),
        ),
        build_tool(
            "codex_preview_provider_interoperability_rollback",
            "Preview rollback of one provider interoperability backup manifest.",
            preview_properties(rollback),
            ["manifestPath", "operationId", "workerStateDigest"],
        ),
        build_tool(
            "codex_rollback_provider_interoperability",
            "Restore verified provider interoperability pre-images after matching preview authorization.",
            write_properties(rollback),
            ["apiToken", "operationPreviewId", "inputHash", "manifestPath", "operationId", "workerStateDigest"],
        ),
    ]
