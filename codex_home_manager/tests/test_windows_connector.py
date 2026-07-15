from __future__ import annotations

import importlib.util
import sys
import types
import urllib.error
from pathlib import Path


connector_path = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "connector_main.py"


def load_connector(monkeypatch, module_name: str):
    uvicorn = types.ModuleType("uvicorn")
    uvicorn.run = lambda *_args, **_kwargs: None
    server = types.ModuleType("backend.server")
    server.app = object()
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "backend.server", server)
    spec = importlib.util.spec_from_file_location(module_name, connector_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_connector_defaults_backup_root_to_shared_windows_backup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.delenv("CODEX_HOME_MANAGER_BACKUP_ROOT", raising=False)

    load_connector(monkeypatch, "connector_default_backup_test")

    assert sys.modules["os"].environ["CODEX_HOME_MANAGER_BACKUP_ROOT"] == r"D:\Backup\codex_home_manager"


def test_connector_starts_pending_validation_watcher_for_server_owning_process(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    connector = load_connector(monkeypatch, "connector_pending_validation_test")
    calls: list[Path] = []
    monkeypatch.setattr(connector, "register_browser_protocol", lambda: None)
    monkeypatch.setattr(connector, "existing_connector_is_running", lambda: False)
    monkeypatch.setattr(connector, "port_is_available", lambda: True)
    monkeypatch.setattr(connector, "open_local_console_after_start", lambda: None)
    monkeypatch.setattr(connector, "start_pending_restart_validation", lambda path: calls.append(path) or True)

    connector.main()

    assert calls == [Path(r"D:\Backup\codex_full_repair\active_repair.lock.json")]


def test_existing_connector_probe_uses_public_capabilities_endpoint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    connector = load_connector(monkeypatch, "connector_capabilities_probe_test")
    requested_urls: list[str] = []

    class Response:
        status = 200

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


def test_existing_connector_probe_treats_auth_rejection_as_running(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    connector = load_connector(monkeypatch, "connector_auth_probe_test")

    def urlopen(url: str, timeout: float):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(connector.urllib.request, "urlopen", urlopen)

    assert connector.existing_connector_is_running() is True


def test_existing_connector_probe_rejects_unreachable_service(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    connector = load_connector(monkeypatch, "connector_unreachable_probe_test")

    def urlopen(url: str, timeout: float):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(connector.urllib.request, "urlopen", urlopen)

    assert connector.existing_connector_is_running() is False
