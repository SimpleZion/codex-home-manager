from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .codex_data import build_snapshot_catalog, build_snapshot_summary, query_snapshot_thread_page


def snapshot_mcp_tool_definitions(
    *,
    build_tool: Callable[..., dict[str, Any]],
    base_properties: Callable[[], dict[str, Any]],
    sidebar_limit_schema: dict[str, Any],
) -> list[dict[str, Any]]:
    shared_properties = {**base_properties(), "sidebarLimit": sidebar_limit_schema}
    return [
        build_tool(
            "codex_snapshot",
            "Compatibility tool that returns the complete classified thread catalog. Prefer codex_snapshot_summary plus codex_thread_page for bounded agent reads.",
            shared_properties,
        ),
        build_tool(
            "codex_snapshot_summary",
            "Read a bounded Codex Home summary with project aggregates, status counts, and four large-thread highlights without returning the complete thread catalog.",
            {**shared_properties, "refresh": {"type": "boolean", "default": False}},
        ),
        build_tool(
            "codex_thread_page",
            "Page and search classified threads with an opaque catalog-bound cursor. This is the preferred agent interface for thread inventory.",
            {
                **shared_properties,
                "threadKind": {"type": "string", "enum": ["main", "all", "subagent"], "default": "main"},
                "filterMode": {"type": "string", "enum": ["all", "visible", "manual_hidden", "repair", "archived"], "default": "all"},
                "projectPath": {"type": "string", "default": "all"},
                "search": {"type": "string", "default": ""},
                "sortMode": {"type": "string", "enum": ["rank", "status", "title", "project", "updated", "size", "tokens"], "default": "rank"},
                "sortDirection": {"type": "string", "enum": ["asc", "desc"], "default": "asc"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "cursor": {"type": "string"},
                "refresh": {"type": "boolean", "default": False},
            },
        ),
    ]


def execute_snapshot_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    codex_home: str | None,
    read_int: Callable[[dict[str, Any], str, int, int | None, int | None], int],
    read_str: Callable[[dict[str, Any], str, str | None], str | None],
    read_bool: Callable[[dict[str, Any], str, bool], bool],
) -> dict[str, Any] | None:
    sidebar_limit = read_int(arguments, "sidebarLimit", 50, 1, 1000)
    if name == "codex_snapshot":
        return build_snapshot_catalog(
            codex_home_text=codex_home,
            sidebar_limit=sidebar_limit,
            validate_rollout_display=bool(arguments.get("validateRolloutDisplay") or False),
        )
    if name == "codex_snapshot_summary":
        return build_snapshot_summary(
            codex_home_text=codex_home,
            sidebar_limit=sidebar_limit,
            force_refresh=read_bool(arguments, "refresh", False),
        )
    if name != "codex_thread_page":
        return None
    return query_snapshot_thread_page(
        codex_home_text=codex_home,
        sidebar_limit=sidebar_limit,
        thread_kind=read_str(arguments, "threadKind", "main") or "main",
        filter_mode=read_str(arguments, "filterMode", "all") or "all",
        project_path=read_str(arguments, "projectPath", "all") or "all",
        search=read_str(arguments, "search", "") or "",
        sort_mode=read_str(arguments, "sortMode", "rank") or "rank",
        sort_direction=read_str(arguments, "sortDirection", "asc") or "asc",
        limit=read_int(arguments, "limit", 50, 1, 200),
        cursor=read_str(arguments, "cursor", None),
        force_refresh=read_bool(arguments, "refresh", False),
    )
