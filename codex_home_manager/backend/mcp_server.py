from __future__ import annotations

from collections.abc import Callable
from typing import Any


def prompt_index_mcp_tool_definitions(
    *,
    build_tool: Callable[..., dict[str, Any]],
    preview_properties: Callable[[dict[str, Any] | None], dict[str, Any]],
    write_properties: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        build_tool(
            "codex_prompt_index_status",
            "Read privacy-safe prompt-index lifecycle metadata for one Codex Home. Returns the local derived-index root plus counts, sizes, timestamps, limits, and in-use state; never prompt text or source rollout paths.",
            preview_properties(None),
        ),
        build_tool(
            "codex_preview_clear_prompt_index",
            "Preview clearing the disposable prompt index for one Codex Home and return a single-use operationPreviewId/inputHash ticket. No prompt content is returned.",
            preview_properties(None),
        ),
        build_tool(
            "codex_clear_prompt_index",
            "Clear the disposable prompt index for one Codex Home after matching preview authorization. The source rollouts are not modified and no cache backup is created.",
            write_properties(None),
            ["apiToken", "operationPreviewId", "inputHash"],
        ),
    ]
