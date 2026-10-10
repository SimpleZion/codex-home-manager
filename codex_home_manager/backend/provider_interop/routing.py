from __future__ import annotations

from typing import Any


def route_requires_attention(status: dict[str, Any]) -> bool:
    route = status.get("route") if isinstance(status, dict) else None
    return isinstance(route, dict) and route.get("classification") in {"conflicting", "unknown"}
