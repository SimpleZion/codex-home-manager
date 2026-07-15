from __future__ import annotations

import sys
from pathlib import Path

import tomlkit
import pytest


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

import merge_codex_managed_config as managed_config_module
from merge_codex_managed_config import merge_managed_config


@pytest.fixture(autouse=True)
def force_offline_guard(monkeypatch) -> None:
    monkeypatch.setattr(managed_config_module, "assert_codex_offline", lambda: None)


def test_merge_managed_config_preserves_unknown_policy_and_releases_desktop_marketplaces(tmp_path: Path) -> None:
    config_path = tmp_path / "managed_config.toml"
    config_path.write_text(
        """
[organization]
policy = "preserve-me"

[marketplaces.openai-primary-runtime]
source_type = "local"
source = "D:/existing-primary"

[plugins."custom@example"]
enabled = false
custom_value = 7
""".lstrip(),
        encoding="utf-8",
    )

    changed = merge_managed_config(
        path=config_path,
        plugin_names=["browser", "sites"],
        removed_marketplace_names=["openai-bundled", "openai-primary-runtime"],
    )
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    assert changed is True
    assert document["organization"]["policy"] == "preserve-me"
    assert "marketplaces" not in document
    assert document["plugins"]["custom@example"]["custom_value"] == 7
    assert document["plugins"]["browser@openai-bundled"]["enabled"] is True
    assert document["plugins"]["sites@openai-bundled"]["enabled"] is True


def test_merge_managed_config_persistently_disables_dangerous_plugins(tmp_path: Path) -> None:
    config_path = tmp_path / "managed_config.toml"
    config_path.write_text(
        """
notify = ["legacy-hook.exe"]

[mcp_servers.codex_thread_messenger]
command = "legacy.exe"

[plugins."build-ios-apps@openai-curated"]
enabled = true

[plugins."build-macos-apps@openai-curated"]
enabled = true
""".lstrip(),
        encoding="utf-8",
    )

    merge_managed_config(
        path=config_path,
        plugin_names=["browser"],
        disabled_plugin_names=["build-ios-apps@openai-curated", "build-macos-apps@openai-curated"],
        disabled_mcp_server_names=["codex_thread_messenger"],
        removed_marketplace_names=["openai-bundled", "openai-primary-runtime"],
    )
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    assert "notify" not in document
    assert "mcp_servers" not in document or "codex_thread_messenger" not in document["mcp_servers"]
    assert document["plugins"]["build-ios-apps@openai-curated"]["enabled"] is False
    assert document["plugins"]["build-macos-apps@openai-curated"]["enabled"] is False


def test_merge_managed_config_removes_all_node_repl_overrides(tmp_path: Path) -> None:
    runtime_config_path = tmp_path / "config.toml"
    runtime_config_path.write_text(
        f"""
[mcp_servers.node_repl]
command = "node_repl.exe"
args = ["--disable-sandbox"]

[mcp_servers.node_repl.env]
NODE_REPL_NODE_MODULE_DIRS = "{(tmp_path / 'node_modules').as_posix()}"
CODEX_CLI_PATH = "{(tmp_path / 'plugin-appserver' / 'codex.exe').as_posix()}"
NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = "{'a' * 64},{'b' * 64}"
NODE_REPL_TRUSTED_CODE_PATHS = "D:/.codex"
""".lstrip(),
        encoding="utf-8",
    )
    managed_config_path = tmp_path / "managed_config.toml"
    managed_config_path.write_text(
        """
[organization]
policy = "preserve-me"

[mcp_servers.node_repl]
command = "managed-node-repl.exe"
args = []

[mcp_servers.node_repl.env]
NODE_REPL_NODE_MODULE_DIRS = "C:/stale"
CODEX_CLI_PATH = "C:/stale/codex.exe"
NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = "stale"
MANAGED_ONLY = "remove-me"
""".lstrip(),
        encoding="utf-8",
    )

    merge_managed_config(
        path=managed_config_path,
        plugin_names=["browser"],
        removed_marketplace_names=["openai-bundled", "openai-primary-runtime"],
    )
    managed_document = tomlkit.parse(managed_config_path.read_text(encoding="utf-8"))

    assert managed_document["organization"]["policy"] == "preserve-me"
    assert "mcp_servers" not in managed_document
