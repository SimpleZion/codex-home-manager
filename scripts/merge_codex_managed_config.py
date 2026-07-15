from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import tomlkit


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "codex_home_manager"))

from backend.offline_repair_policy import assert_codex_offline


def ensure_table(parent: dict, key: str):
    value = parent.get(key)
    if value is None:
        value = tomlkit.table()
        parent[key] = value
    if not hasattr(value, "get"):
        raise RuntimeError(f"managed config key is not a table: {key}")
    return value


def validate_managed_config(
    path: Path,
    plugin_names: list[str],
    disabled_plugin_names: list[str],
    disabled_mcp_server_names: list[str],
    removed_marketplace_names: list[str],
) -> None:
    document = tomlkit.parse(path.read_text(encoding="utf-8-sig"))
    if "notify" in document:
        raise RuntimeError("legacy top-level notify remains in managed_config.toml")
    mcp_servers = document.get("mcp_servers") or {}
    for server_name in disabled_mcp_server_names:
        if server_name in mcp_servers:
            raise RuntimeError(f"legacy managed MCP server must be removed: {server_name}")
    plugins = document.get("plugins") or {}
    for plugin_name in plugin_names:
        if (plugins.get(f"{plugin_name}@openai-bundled") or {}).get("enabled") is not True:
            raise RuntimeError(f"managed bundled plugin is not enabled: {plugin_name}")
    for plugin_name in disabled_plugin_names:
        try:
            enabled = plugins[plugin_name]["enabled"]
        except (KeyError, TypeError) as error:
            raise RuntimeError(f"managed plugin must be explicitly disabled on this platform: {plugin_name}") from error
        if enabled is not False:
            raise RuntimeError(f"managed plugin is not explicitly disabled on this platform: {plugin_name}")
    marketplaces = document.get("marketplaces") or {}
    for marketplace_name in removed_marketplace_names:
        if marketplace_name in marketplaces:
            raise RuntimeError(
                f"Desktop-owned marketplace must not be pinned in managed config: {marketplace_name}"
            )
    if "node_repl" in mcp_servers:
        raise RuntimeError(
            "managed node_repl must be absent so Codex Desktop can inject its privileged dynamic runtime"
        )


def merge_managed_config(
    path: Path,
    plugin_names: list[str],
    disabled_plugin_names: list[str] | None = None,
    disabled_mcp_server_names: list[str] | None = None,
    removed_marketplace_names: list[str] | None = None,
) -> bool:
    original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    document = tomlkit.parse(original) if original else tomlkit.document()
    if "notify" in document:
        del document["notify"]
    mcp_servers = document.get("mcp_servers")
    if mcp_servers is not None:
        for server_name in ["node_repl", *(disabled_mcp_server_names or [])]:
            if server_name in mcp_servers:
                del mcp_servers[server_name]
        if not mcp_servers:
            del document["mcp_servers"]
    marketplaces = document.get("marketplaces")
    if marketplaces is not None:
        for marketplace_name in removed_marketplace_names or []:
            if marketplace_name in marketplaces:
                del marketplaces[marketplace_name]
        if not marketplaces:
            del document["marketplaces"]

    plugins = ensure_table(document, "plugins")
    for plugin_name in plugin_names:
        plugin = ensure_table(plugins, f"{plugin_name}@openai-bundled")
        plugin["enabled"] = True
    for plugin_name in disabled_plugin_names or []:
        plugin = ensure_table(plugins, plugin_name)
        plugin["enabled"] = False

    rendered = tomlkit.dumps(document)
    tomlkit.parse(rendered)
    if rendered == original:
        validate_managed_config(
            path,
            plugin_names,
            disabled_plugin_names or [],
            disabled_mcp_server_names or [],
            removed_marketplace_names or [],
        )
        return False
    assert_codex_offline()
    temporary_path = path.with_suffix(path.suffix + ".merging")
    temporary_path.write_text(rendered, encoding="utf-8", newline="")
    assert_codex_offline()
    os.replace(temporary_path, path)
    validate_managed_config(
        path,
        plugin_names,
        disabled_plugin_names or [],
        disabled_mcp_server_names or [],
        removed_marketplace_names or [],
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge bundled plugin entries into managed_config.toml.")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--plugins", nargs="+", required=True)
    parser.add_argument("--disable-plugin", action="append", default=[])
    parser.add_argument("--disable-mcp-server", action="append", default=[])
    parser.add_argument("--remove-marketplace", action="append", default=[])
    arguments = parser.parse_args()
    changed = merge_managed_config(
        path=arguments.path.resolve(),
        plugin_names=arguments.plugins,
        disabled_plugin_names=arguments.disable_plugin,
        disabled_mcp_server_names=arguments.disable_mcp_server,
        removed_marketplace_names=arguments.remove_marketplace,
    )
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
