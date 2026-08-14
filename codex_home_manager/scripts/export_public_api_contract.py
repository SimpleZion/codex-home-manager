from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


manager_root = Path(__file__).resolve().parents[1]
if str(manager_root) not in sys.path:
    sys.path.insert(0, str(manager_root))

from backend.server import app, capabilities, mcp_tool_definitions


def build_public_api_contract() -> dict[str, object]:
    capability_payload = capabilities("en")
    rest_capabilities = capability_payload["capabilities"]
    mcp_tools = mcp_tool_definitions()
    return {
        "schemaVersion": 2,
        "name": "codex-home-manager-public-api-contract",
        "version": app.version,
        "frontendContractVersion": capability_payload["frontendContractVersion"],
        "note": (
            "This hosted contract mirrors the complete loopback connector API. "
            "The hosted static page cannot access Codex data until it connects to the local connector."
        ),
        "discovery": {
            "capabilities": "/api/capabilities",
            "openapi": "/openapi.json",
            "mcp": "/mcp",
        },
        "counts": {
            "restCapabilities": len(rest_capabilities),
            "mcpTools": len(mcp_tools),
        },
        "capabilities": rest_capabilities,
        "mcpTools": [
            {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            }
            for tool in mcp_tools
        ],
        "safetyModel": capability_payload["safetyModel"],
        "commonQueryParameters": capability_payload["commonQueryParameters"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the exact public REST and MCP discovery contract.")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    payload = build_public_api_contract()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
