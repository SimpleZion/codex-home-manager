from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

import tomlkit


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "codex_home_manager"))

from backend.offline_repair_policy import assert_codex_offline


desktop_computer_use_helper_suffix = (
    r"\node_modules\@oai\sky\bin\windows\codex-computer-use.exe"
)


def ensure_table(parent: dict, key: str):
    value = parent.get(key)
    if value is None:
        value = tomlkit.table()
        parent[key] = value
    if not hasattr(value, "get"):
        raise RuntimeError(f"config key is not a table: {key}")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_match(left: Path, right: Path) -> bool:
    try:
        return left.is_file() and right.is_file() and left.stat().st_size == right.stat().st_size and file_sha256(left) == file_sha256(right)
    except OSError:
        return False


def first_valid_sky_module_root(module_roots: list[Path]) -> Path | None:
    helper_relative_path = Path("@oai") / "sky" / "bin" / "windows" / "codex-computer-use.exe"
    return next((root for root in module_roots if (root / helper_relative_path).is_file()), None)


def is_desktop_managed_computer_use_notify(
    value: object,
    document: object | None = None,
    appx_resources: Path | None = None,
) -> bool:
    try:
        entries = list(value)  # tomlkit arrays are list-like but not plain lists.
    except TypeError:
        return False
    if len(entries) != 2 or str(entries[1]) != "turn-ended":
        return False
    if document is None or appx_resources is None:
        return False
    try:
        node_repl = document["mcp_servers"]["node_repl"]
        environment = node_repl["env"]
        module_roots = [
            Path(value).expanduser()
            for value in str(environment.get("NODE_REPL_NODE_MODULE_DIRS") or "").split(os.pathsep)
            if value.strip()
        ]
        module_root = first_valid_sky_module_root(module_roots)
        if module_root is None:
            return False
        helper_relative_path = Path("@oai") / "sky" / "bin" / "windows" / "codex-computer-use.exe"
        helper_path = Path(str(entries[0])).expanduser()
        expected_dynamic_helper = module_root / helper_relative_path
        node_repl_path = Path(str(node_repl.get("command") or "")).expanduser()
        node_path = Path(str(environment.get("NODE_REPL_NODE_PATH") or "")).expanduser()
        cli_path = Path(str(environment.get("CODEX_CLI_PATH") or "")).expanduser()
        appx_resources = appx_resources.resolve(strict=False)
        expected_appx_paths = {
            "helper": appx_resources / "cua_node" / "bin" / "node_modules" / helper_relative_path,
            "node_repl": appx_resources / "cua_node" / "bin" / "node_repl.exe",
            "node": appx_resources / "cua_node" / "bin" / "node.exe",
            "cli": appx_resources / "codex.exe",
        }
    except (KeyError, TypeError, OSError):
        return False
    normalized_path = str(helper_path).replace("/", "\\").casefold()
    return (
        normalized_path.endswith(desktop_computer_use_helper_suffix.casefold())
        and helper_path.resolve(strict=False) == expected_dynamic_helper.resolve(strict=False)
        and str(environment.get("SKY_CUA_NATIVE_PIPE") or "") == "1"
        and bool(str(environment.get("SKY_CUA_NATIVE_PIPE_DIRECTORY") or "").strip())
        and files_match(helper_path, expected_appx_paths["helper"])
        and files_match(node_repl_path, expected_appx_paths["node_repl"])
        and files_match(node_path, expected_appx_paths["node"])
        and files_match(cli_path, expected_appx_paths["cli"])
    )


def validate_runtime_config(
    path: Path,
    bundled_marketplace: Path | None,
    bundled_plugin_names: list[str],
    disabled_plugin_names: list[str] | None = None,
    disabled_mcp_server_names: list[str] | None = None,
    removed_marketplace_names: list[str] | None = None,
    appx_resources: Path | None = None,
) -> None:
    disabled_plugin_names = list(disabled_plugin_names or [])
    document = tomlkit.parse(path.read_text(encoding="utf-8-sig"))
    if "notify" in document and not is_desktop_managed_computer_use_notify(
        document["notify"], document, appx_resources
    ):
        raise RuntimeError("notify is not bound to the current AppX CLI and Desktop CUA runtime")
    if bundled_marketplace is not None:
        try:
            bundled = document["marketplaces"]["openai-bundled"]
        except (KeyError, TypeError) as error:
            raise RuntimeError("bundled marketplace config is missing") from error
        configured_marketplace = Path(str(bundled.get("source") or "")).resolve()
        if str(configured_marketplace).casefold() != str(bundled_marketplace.resolve()).casefold():
            raise RuntimeError("bundled marketplace source does not match the Desktop runtime marketplace")
        if str(bundled.get("source_type") or "") != "local":
            raise RuntimeError("bundled marketplace source_type is not local")
    marketplaces = document.get("marketplaces") or {}
    for marketplace_name in removed_marketplace_names or []:
        if marketplace_name == "openai-bundled" and bundled_marketplace is not None:
            continue
        if marketplace_name in marketplaces:
            raise RuntimeError(
                f"Desktop-owned marketplace must not remain in runtime config: {marketplace_name}"
            )
    for plugin_name in bundled_plugin_names:
        try:
            enabled = document["plugins"][f"{plugin_name}@openai-bundled"]["enabled"]
        except (KeyError, TypeError) as error:
            raise RuntimeError(f"bundled plugin config is missing: {plugin_name}") from error
        if enabled is not True:
            raise RuntimeError(f"bundled plugin is not enabled: {plugin_name}")
    configured_plugins = document.get("plugins") or {}
    for plugin_name in disabled_plugin_names:
        try:
            enabled = configured_plugins[plugin_name]["enabled"]
        except (KeyError, TypeError) as error:
            raise RuntimeError(f"plugin must be explicitly disabled on this platform: {plugin_name}") from error
        if enabled is not False:
            raise RuntimeError(f"plugin is not explicitly disabled on this platform: {plugin_name}")
    configured_mcp_servers = document.get("mcp_servers") or {}
    for server_name in disabled_mcp_server_names or []:
        if server_name in configured_mcp_servers:
            raise RuntimeError(f"legacy MCP server must be removed: {server_name}")


def merge_runtime_config(
    path: Path,
    bundled_marketplace: Path | None,
    bundled_plugin_names: list[str],
    disabled_plugin_names: list[str] | None = None,
    disabled_mcp_server_names: list[str] | None = None,
    removed_marketplace_names: list[str] | None = None,
    appx_resources: Path | None = None,
) -> bool:
    disabled_plugin_names = list(disabled_plugin_names or [])
    original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    document = tomlkit.parse(original) if original else tomlkit.document()
    if "notify" in document and not is_desktop_managed_computer_use_notify(
        document["notify"], document, appx_resources
    ):
        del document["notify"]

    mcp_servers = document.get("mcp_servers")
    if mcp_servers is not None:
        for server_name in disabled_mcp_server_names or []:
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

    if bundled_marketplace is not None:
        marketplaces = ensure_table(document, "marketplaces")
        bundled = ensure_table(marketplaces, "openai-bundled")
        bundled["source_type"] = "local"
        bundled["source"] = str(bundled_marketplace.resolve())
    if bundled_plugin_names:
        plugins = ensure_table(document, "plugins")
        for plugin_name in bundled_plugin_names:
            plugin = ensure_table(plugins, f"{plugin_name}@openai-bundled")
            plugin["enabled"] = True
    if disabled_plugin_names:
        plugins = ensure_table(document, "plugins")
        for plugin_name in disabled_plugin_names:
            plugin = ensure_table(plugins, plugin_name)
            plugin["enabled"] = False

    rendered = tomlkit.dumps(document)
    tomlkit.parse(rendered)
    if rendered == original:
        validate_runtime_config(
            path,
            bundled_marketplace,
            bundled_plugin_names,
            disabled_plugin_names,
            disabled_mcp_server_names,
            removed_marketplace_names,
            appx_resources,
        )
        return False
    assert_codex_offline()
    temporary_path = path.with_suffix(path.suffix + ".merging")
    temporary_path.write_text(rendered, encoding="utf-8", newline="")
    assert_codex_offline()
    os.replace(temporary_path, path)
    validate_runtime_config(
        path,
        bundled_marketplace,
        bundled_plugin_names,
        disabled_plugin_names,
        disabled_mcp_server_names,
        removed_marketplace_names,
        appx_resources,
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge safe runtime repairs into config.toml.")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--bundled-marketplace", type=Path)
    parser.add_argument("--bundled-plugin", action="append", default=[])
    parser.add_argument("--disable-plugin", action="append", default=[])
    parser.add_argument("--disable-mcp-server", action="append", default=[])
    parser.add_argument("--remove-marketplace", action="append", default=[])
    parser.add_argument("--appx-resources", type=Path)
    arguments = parser.parse_args()
    changed = merge_runtime_config(
        path=arguments.path.resolve(),
        bundled_marketplace=arguments.bundled_marketplace.resolve() if arguments.bundled_marketplace else None,
        bundled_plugin_names=arguments.bundled_plugin,
        disabled_plugin_names=arguments.disable_plugin,
        disabled_mcp_server_names=arguments.disable_mcp_server,
        removed_marketplace_names=arguments.remove_marketplace,
        appx_resources=arguments.appx_resources.resolve() if arguments.appx_resources else None,
    )
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
