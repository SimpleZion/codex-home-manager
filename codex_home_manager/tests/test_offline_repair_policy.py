from __future__ import annotations

import sys
from pathlib import Path

import pytest


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

import codex_plugin_state_snapshot
import merge_codex_managed_config
import merge_codex_runtime_config
from backend import offline_repair_policy


def test_backup_path_policy_rejects_paths_outside_configured_root() -> None:
    with pytest.raises(RuntimeError, match="backup root"):
        offline_repair_policy.assert_backup_path(Path(r"C:\temp\backup"), Path(r"D:\Backup"))

    offline_repair_policy.assert_backup_path(Path(r"D:\Backup\codex\run-1"), Path(r"D:\Backup"))


def test_runtime_merge_checks_offline_state_immediately_before_replace(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    original = 'model = "before"\nnotify = ["legacy.exe"]\n'
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        merge_codex_runtime_config,
        "assert_codex_offline",
        lambda: (_ for _ in ()).throw(RuntimeError("Codex process is running")),
    )

    with pytest.raises(RuntimeError, match="Codex process"):
        merge_codex_runtime_config.merge_runtime_config(
            path=config_path,
            bundled_marketplace=None,
            bundled_plugin_names=[],
            disabled_plugin_names=[],
        )

    assert config_path.read_text(encoding="utf-8") == original


def test_managed_merge_checks_offline_state_immediately_before_replace(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "managed_config.toml"
    config_path.write_text('model = "before"\n', encoding="utf-8")
    monkeypatch.setattr(
        merge_codex_managed_config,
        "assert_codex_offline",
        lambda: (_ for _ in ()).throw(RuntimeError("Codex process is running")),
    )

    with pytest.raises(RuntimeError, match="Codex process"):
        merge_codex_managed_config.merge_managed_config(
            path=config_path,
            plugin_names=["browser"],
            removed_marketplace_names=["openai-bundled", "openai-primary-runtime"],
        )

    assert config_path.read_text(encoding="utf-8") == 'model = "before"\n'


def test_plugin_restore_refuses_online_mutation_before_preparing_files(tmp_path: Path, monkeypatch) -> None:
    manifest_path = tmp_path / "plugin_state_snapshot.json"
    manifest_path.write_text('{"schema_version":1,"status":"complete"}', encoding="utf-8")
    monkeypatch.setattr(
        codex_plugin_state_snapshot,
        "assert_codex_offline",
        lambda: (_ for _ in ()).throw(RuntimeError("Codex process is running")),
    )

    with pytest.raises(RuntimeError, match="Codex process"):
        codex_plugin_state_snapshot.restore_plugin_state(manifest_path)


def test_offline_process_guard_fails_closed_when_candidate_process_cannot_be_inspected(monkeypatch) -> None:
    class InaccessibleNodeProcess:
        pid = 321
        info = {"pid": 321, "name": "node.exe"}

        def cmdline(self):
            raise offline_repair_policy.psutil.AccessDenied(pid=self.pid)

        def exe(self):
            return ""

    monkeypatch.setattr(
        offline_repair_policy.psutil,
        "process_iter",
        lambda _attributes: [InaccessibleNodeProcess()],
    )

    with pytest.raises(RuntimeError, match="could not inspect possible Codex processes"):
        offline_repair_policy.related_codex_processes()


def test_offline_process_guard_includes_packaged_local_connector(monkeypatch) -> None:
    class LocalConnectorProcess:
        pid = 8765
        info = {"pid": 8765, "name": "codex-home-manager-local-win-x64.exe"}

        def cmdline(self):
            return [r"C:\workspace\codex-home-manager-local-win-x64.exe"]

        def exe(self):
            return self.cmdline()[0]

    monkeypatch.setattr(
        offline_repair_policy.psutil,
        "process_iter",
        lambda _attributes: [LocalConnectorProcess()],
    )

    processes = offline_repair_policy.related_codex_processes()

    assert [process["pid"] for process in processes] == [8765]
    with pytest.raises(RuntimeError, match="Codex process is running"):
        offline_repair_policy.assert_codex_offline()


@pytest.mark.parametrize("process_name", ["chrome.exe", "msedge.exe"])
def test_offline_process_guard_ignores_unrelated_browsers(monkeypatch, process_name: str) -> None:
    class BrowserProcess:
        pid = 2468
        info = {"pid": 2468, "name": process_name}

        def cmdline(self):
            return [rf"C:\Program Files\Browser\{process_name}"]

        def exe(self):
            return self.cmdline()[0]

    monkeypatch.setattr(
        offline_repair_policy.psutil,
        "process_iter",
        lambda _attributes: [BrowserProcess()],
    )

    assert offline_repair_policy.related_codex_processes() == []


def test_offline_process_guard_still_blocks_codex_extension_host(monkeypatch) -> None:
    class ExtensionHostProcess:
        pid = 8642
        info = {"pid": 8642, "name": "extension-host.exe"}

        def cmdline(self):
            return [
                r"D:\.codex\plugins\cache\openai-bundled\chrome\latest\extension-host\windows\x64\extension-host.exe"
            ]

        def exe(self):
            return self.cmdline()[0]

    monkeypatch.setattr(
        offline_repair_policy.psutil,
        "process_iter",
        lambda _attributes: [ExtensionHostProcess()],
    )

    assert [process["pid"] for process in offline_repair_policy.related_codex_processes()] == [8642]
    with pytest.raises(RuntimeError, match="Codex process is running"):
        offline_repair_policy.assert_codex_offline()
