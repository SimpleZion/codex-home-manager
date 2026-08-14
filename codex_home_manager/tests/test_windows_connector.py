from __future__ import annotations

import importlib.util
import sys
import types
import urllib.error
import json
from pathlib import Path

import pytest


connector_path = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "connector_main.py"


def load_connector(monkeypatch, module_name: str):
    uvicorn = types.ModuleType("uvicorn")
    uvicorn.run = lambda *_args, **_kwargs: None
    server = types.ModuleType("backend.server")
    server.app = types.SimpleNamespace(version="1.0.10")
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "backend.server", server)
    spec = importlib.util.spec_from_file_location(module_name, connector_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_connector_defaults_backup_root_to_writable_user_data_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.delenv("CODEX_HOME_MANAGER_BACKUP_ROOT", raising=False)

    load_connector(monkeypatch, "connector_default_backup_test")

    backup_root = Path(sys.modules["os"].environ["CODEX_HOME_MANAGER_BACKUP_ROOT"])
    assert backup_root == (tmp_path / "local-app-data" / "CodexHomeManager" / "backups").resolve()
    assert backup_root.is_dir()


def test_windows_connector_preserves_explicit_absolute_backup_root(monkeypatch, tmp_path: Path) -> None:
    explicit_backup_root = tmp_path / "explicit-backup"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("CODEX_HOME_MANAGER_BACKUP_ROOT", str(explicit_backup_root))

    load_connector(monkeypatch, "connector_explicit_backup_test")

    assert Path(sys.modules["os"].environ["CODEX_HOME_MANAGER_BACKUP_ROOT"]) == explicit_backup_root.resolve()
    assert explicit_backup_root.is_dir()


def test_connector_starts_pending_validation_watcher_for_server_owning_process(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.delenv("CODEX_HOME_MANAGER_BACKUP_ROOT", raising=False)
    connector = load_connector(monkeypatch, "connector_pending_validation_test")
    calls: list[Path] = []
    monkeypatch.setattr(connector, "register_browser_protocol", lambda: None)
    monkeypatch.setattr(connector, "existing_connector_is_running", lambda: False)
    monkeypatch.setattr(connector, "port_is_available", lambda: True)
    monkeypatch.setattr(connector, "open_local_console_after_start", lambda: None)
    monkeypatch.setattr(connector, "start_pending_restart_validation", lambda path: calls.append(path) or True)

    connector.main()

    expected_lock = tmp_path / "local-app-data" / "CodexHomeManager" / "codex_full_repair" / "active_repair.lock.json"
    assert calls == [expected_lock.resolve()]


def test_existing_connector_probe_uses_public_capabilities_endpoint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    connector = load_connector(monkeypatch, "connector_capabilities_probe_test")
    requested_urls: list[str] = []

    class Response:
        status = 200

        def read(self):
            return json.dumps({
                "service": "codex-home-manager",
                "version": "1.0.10",
                "frontendContractVersion": 2,
                "openapiPath": "/openapi.json",
                "mcpPath": "/mcp",
                "capabilities": [],
            }).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(url: str, timeout: float):
        requested_urls.append(url)
        assert timeout == 1.5
        return Response()

    monkeypatch.setattr(connector.urllib.request, "urlopen", urlopen)

    assert connector.existing_connector_is_running() is True
    assert requested_urls == ["http://127.0.0.1:8765/api/capabilities"]


def test_existing_connector_probe_rejects_auth_rejection_without_product_identity(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    connector = load_connector(monkeypatch, "connector_auth_probe_test")

    def urlopen(url: str, timeout: float):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(connector.urllib.request, "urlopen", urlopen)

    assert connector.existing_connector_is_running() is False


def test_existing_connector_probe_rejects_arbitrary_success_service(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    connector = load_connector(monkeypatch, "connector_arbitrary_success_test")

    class Response:
        status = 200

        def read(self):
            return b'{"service":"other-service","version":"1.0.10","frontendContractVersion":2,"openapiPath":"/openapi.json","mcpPath":"/mcp","capabilities":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(connector.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert connector.existing_connector_is_running() is False


def test_existing_connector_probe_rejects_unreachable_service(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    connector = load_connector(monkeypatch, "connector_unreachable_probe_test")

    def urlopen(url: str, timeout: float):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(connector.urllib.request, "urlopen", urlopen)

    assert connector.existing_connector_is_running() is False


def test_stop_refuses_instance_record_with_mismatched_process_identity(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    connector = load_connector(monkeypatch, "connector_stop_identity_test")
    connector.instance_path.write_text(
        json.dumps({"pid": 1234, "executable": str(tmp_path / "expected.exe")}),
        encoding="utf-8",
    )
    monkeypatch.setattr(connector, "probe_connector", lambda: {"service": "codex-home-manager"})
    monkeypatch.setattr(connector, "process_executable_path", lambda _pid: tmp_path / "different.exe")

    with pytest.raises(RuntimeError, match="does not match"):
        connector.stop_verified_connector()
