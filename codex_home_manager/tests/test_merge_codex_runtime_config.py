from __future__ import annotations

import os
import sys
from pathlib import Path

import tomlkit
import pytest


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

import merge_codex_runtime_config as runtime_config_module
from merge_codex_runtime_config import merge_runtime_config, validate_runtime_config


@pytest.fixture(autouse=True)
def force_offline_guard(monkeypatch) -> None:
    monkeypatch.setattr(runtime_config_module, "assert_codex_offline", lambda: None)


def write_current_desktop_runtime(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    appx_resources = tmp_path / "appx" / "app" / "resources"
    appx_runtime = appx_resources / "cua_node"
    dynamic_runtime = tmp_path / "local" / "runtimes" / "cua_node" / "current"
    module_root = dynamic_runtime / "bin" / "node_modules"
    helper_relative_path = Path("@oai/sky/bin/windows/codex-computer-use.exe")
    for relative_path, content in {
        Path("codex.exe"): b"current-cli",
        Path("cua_node/manifest.json"): b'{"runtime":"current"}',
        Path("cua_node/bin/node.exe"): b"current-node",
        Path("cua_node/bin/node_repl.exe"): b"current-node-repl",
        Path("cua_node/bin/node_modules") / helper_relative_path: b"current-helper",
    }.items():
        destination = appx_resources / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    for relative_path in [
        Path("manifest.json"),
        Path("bin/node.exe"),
        Path("bin/node_repl.exe"),
        Path("bin/node_modules") / helper_relative_path,
    ]:
        source = appx_runtime / relative_path
        destination = dynamic_runtime / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    dynamic_cli = tmp_path / "local" / "bin" / "current" / "codex.exe"
    dynamic_cli.parent.mkdir(parents=True)
    dynamic_cli.write_bytes((appx_resources / "codex.exe").read_bytes())
    return (
        appx_resources,
        dynamic_runtime / "bin" / "node_repl.exe",
        dynamic_runtime / "bin" / "node.exe",
        module_root,
        dynamic_cli,
    )


def runtime_config_text(
    *,
    helper_path: Path,
    node_repl_path: Path,
    node_path: Path,
    module_roots: list[Path],
    cli_path: Path,
) -> str:
    return "\n".join(
        [
            f'notify = ["{helper_path.as_posix()}", "turn-ended"]',
            "",
            "[mcp_servers.node_repl]",
            f'command = "{node_repl_path.as_posix()}"',
            "args = []",
            "",
            "[mcp_servers.node_repl.env]",
            f'NODE_REPL_NODE_PATH = "{node_path.as_posix()}"',
            f'NODE_REPL_NODE_MODULE_DIRS = "{os.pathsep.join(path.as_posix() for path in module_roots)}"',
            f'CODEX_CLI_PATH = "{cli_path.as_posix()}"',
            'SKY_CUA_NATIVE_PIPE = "1"',
            r"SKY_CUA_NATIVE_PIPE_DIRECTORY = '\\.\pipe\codex-computer-use-test'",
            "",
        ]
    )


def test_merge_runtime_config_preserves_unrelated_values_and_desktop_node_repl(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
model = "gpt-test"
notify = ["legacy.exe"]

[desktop]
notify = "preserve-nested"

[mcp_servers.node_repl]
command = "node_repl.exe"
args = []
startup_timeout_sec = 30

[mcp_servers.node_repl.env]
NODE_REPL_NODE_MODULE_DIRS = "C:/stale/node_modules"
NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = "aaa,bbb"
PRESERVE_ME = "yes"

[mcp_servers.codex_thread_messenger]
command = "legacy-thread-messenger.exe"

[plugins."build-ios-apps@openai-curated"]
enabled = true

[plugins."build-macos-apps@openai-curated"]
enabled = true
""".lstrip(),
        encoding="utf-8",
    )

    existing_hash_a = "a" * 64
    existing_hash_b = "b" * 64
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        .replace("aaa,bbb", f"{existing_hash_a},{existing_hash_b}"),
        encoding="utf-8",
    )

    changed = merge_runtime_config(
        path=config_path,
        bundled_marketplace=tmp_path / "bundled-marketplace",
        bundled_plugin_names=["browser", "sites"],
        disabled_plugin_names=["build-ios-apps@openai-curated", "build-macos-apps@openai-curated"],
        disabled_mcp_server_names=["codex_thread_messenger"],
        removed_marketplace_names=["openai-bundled", "openai-primary-runtime"],
    )
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    assert changed is True
    assert "notify" not in document
    assert document["desktop"]["notify"] == "preserve-nested"
    assert document["model"] == "gpt-test"
    node_repl = document["mcp_servers"]["node_repl"]
    assert list(node_repl["args"]) == []
    assert node_repl["startup_timeout_sec"] == 30
    assert node_repl["env"]["NODE_REPL_NODE_MODULE_DIRS"] == "C:/stale/node_modules"
    assert "CODEX_CLI_PATH" not in node_repl["env"]
    assert node_repl["env"]["NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S"] == f"{existing_hash_a},{existing_hash_b}"
    assert node_repl["env"]["PRESERVE_ME"] == "yes"
    assert document["marketplaces"]["openai-bundled"]["source"] == str(
        (tmp_path / "bundled-marketplace").resolve()
    )
    assert document["plugins"]["browser@openai-bundled"]["enabled"] is True
    assert document["plugins"]["sites@openai-bundled"]["enabled"] is True
    assert document["plugins"]["build-ios-apps@openai-curated"]["enabled"] is False
    assert document["plugins"]["build-macos-apps@openai-curated"]["enabled"] is False
    assert "codex_thread_messenger" not in document["mcp_servers"]
    validate_runtime_config(
        path=config_path,
        bundled_marketplace=tmp_path / "bundled-marketplace",
        bundled_plugin_names=["browser", "sites"],
        disabled_plugin_names=["build-ios-apps@openai-curated", "build-macos-apps@openai-curated"],
        disabled_mcp_server_names=["codex_thread_messenger"],
        removed_marketplace_names=["openai-bundled", "openai-primary-runtime"],
    )


def test_merge_runtime_config_does_not_synthesize_missing_node_repl_tables(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-test"\n', encoding="utf-8")

    merge_runtime_config(
        path=config_path,
        bundled_marketplace=None,
        bundled_plugin_names=[],
        disabled_plugin_names=[],
    )
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    assert "mcp_servers" not in document


def test_merge_runtime_config_preserves_current_desktop_computer_use_notify(tmp_path: Path) -> None:
    appx_resources, node_repl_path, node_path, module_root, cli_path = write_current_desktop_runtime(tmp_path)
    helper_path = module_root / "@oai" / "sky" / "bin" / "windows" / "codex-computer-use.exe"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        runtime_config_text(
            helper_path=helper_path,
            node_repl_path=node_repl_path,
            node_path=node_path,
            module_roots=[module_root],
            cli_path=cli_path,
        ),
        encoding="utf-8",
    )

    merge_runtime_config(
        path=config_path,
        bundled_marketplace=None,
        bundled_plugin_names=[],
        disabled_plugin_names=[],
        removed_marketplace_names=["openai-bundled", "openai-primary-runtime"],
        appx_resources=appx_resources,
    )
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    assert list(document["notify"]) == [str(helper_path.as_posix()), "turn-ended"]


def test_merge_runtime_config_removes_notify_from_wrong_sky_module_root(tmp_path: Path) -> None:
    appx_resources, node_repl_path, node_path, module_root, cli_path = write_current_desktop_runtime(tmp_path)
    stale_module_root = tmp_path / "stale" / "node_modules"
    stale_helper = stale_module_root / "@oai" / "sky" / "bin" / "windows" / "codex-computer-use.exe"
    stale_helper.parent.mkdir(parents=True)
    stale_helper.write_bytes(b"stale-helper")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        runtime_config_text(
            helper_path=stale_helper,
            node_repl_path=node_repl_path,
            node_path=node_path,
            module_roots=[module_root, stale_module_root],
            cli_path=cli_path,
        ),
        encoding="utf-8",
    )

    merge_runtime_config(
        path=config_path,
        bundled_marketplace=None,
        bundled_plugin_names=[],
        disabled_plugin_names=[],
        appx_resources=appx_resources,
    )

    assert "notify" not in tomlkit.parse(config_path.read_text(encoding="utf-8"))


def test_validate_runtime_config_rejects_notify_when_dynamic_cli_is_not_current_appx(tmp_path: Path) -> None:
    appx_resources, node_repl_path, node_path, module_root, cli_path = write_current_desktop_runtime(tmp_path)
    cli_path.write_bytes(b"stale-cli")
    helper_path = module_root / "@oai" / "sky" / "bin" / "windows" / "codex-computer-use.exe"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        runtime_config_text(
            helper_path=helper_path,
            node_repl_path=node_repl_path,
            node_path=node_path,
            module_roots=[module_root],
            cli_path=cli_path,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="current AppX|CLI"):
        validate_runtime_config(
            path=config_path,
            bundled_marketplace=None,
            bundled_plugin_names=[],
            appx_resources=appx_resources,
        )


def test_merge_runtime_config_releases_desktop_owned_marketplaces(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[marketplaces.openai-bundled]
source_type = "local"
source = "D:/.codex/.tmp/bundled-marketplaces/openai-bundled"

[marketplaces.openai-primary-runtime]
source_type = "local"
source = "D:/.codex/cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime"
""".lstrip(),
        encoding="utf-8",
    )

    merge_runtime_config(
        path=config_path,
        bundled_marketplace=None,
        bundled_plugin_names=[],
        disabled_plugin_names=[],
        removed_marketplace_names=["openai-bundled", "openai-primary-runtime"],
    )
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    assert "marketplaces" not in document
