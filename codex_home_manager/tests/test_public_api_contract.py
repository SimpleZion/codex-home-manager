from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

from backend import server


script_path = Path(__file__).resolve().parents[1] / "scripts" / "export_public_api_contract.py"
spec = importlib.util.spec_from_file_location("export_public_api_contract", script_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_public_api_contract_is_complete_and_tracks_runtime_version() -> None:
    payload = module.build_public_api_contract()
    runtime_capabilities = server.capabilities("en")["capabilities"]
    runtime_tools = server.mcp_tool_definitions()

    assert payload["version"] == server.app.version
    assert payload["frontendContractVersion"] == 2
    assert payload["counts"] == {
        "restCapabilities": len(runtime_capabilities),
        "mcpTools": len(runtime_tools),
    }
    assert {item["name"] for item in payload["capabilities"]} == {
        item["name"] for item in runtime_capabilities
    }
    assert {item["name"] for item in payload["mcpTools"]} == {
        item["name"] for item in runtime_tools
    }
    assert "search_thread_timeline" in {item["name"] for item in payload["capabilities"]}
    assert "codex_restore_portable_backup" in {item["name"] for item in payload["mcpTools"]}
    assert payload["safetyModel"]["physicalDelete"] is False


def test_public_api_contract_cli_writes_utf8_json(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "public-api.json"
    monkeypatch.setattr(sys, "argv", [str(script_path), "--output", str(output_path)])

    assert module.main() == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 2
    assert payload["version"] == server.app.version
