from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import os
import logging
from pathlib import Path


local_console_url = "http://127.0.0.1:8765/"
connector_probe_url = "http://127.0.0.1:8765/api/capabilities"
data_root: Path
log_path: Path
stdio_log_handle = None


def configure_persistent_data_paths() -> None:
    global data_root, log_path
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        data_root = Path(local_app_data) / "CodexHomeManager"
    else:
        data_root = Path.home() / "AppData" / "Local" / "CodexHomeManager"
    data_root.mkdir(parents=True, exist_ok=True)
    log_path = data_root / "local-connector.log"
    os.environ.setdefault("CODEX_HOME_MANAGER_BACKUP_ROOT", r"D:\Backup\codex_home_manager")
    os.environ.setdefault("CODEX_HOME_MANAGER_EXPORT_ROOT", str(data_root / "exports"))


configure_persistent_data_paths()


def configure_process_logging() -> None:
    global stdio_log_handle
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if sys.stdout is None or sys.stderr is None:
        stdio_log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = stdio_log_handle
        sys.stderr = stdio_log_handle


configure_process_logging()

import uvicorn

from backend.server import app
from backend.pending_repair_validation import default_pending_repair_lock, start_pending_restart_validation


def notify_user(title: str, message: str) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
            logging.info("%s: %s", title, message)
            return
        except Exception:
            pass
    logging.info("%s: %s", title, message)
    print(f"{title}: {message}", flush=True)


def register_browser_protocol() -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg

        executable_path = sys.executable
        command = f'"{executable_path}" "%1"'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\codex-home-manager") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Codex Home Manager")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\codex-home-manager\DefaultIcon") as icon_key:
            winreg.SetValueEx(icon_key, "", 0, winreg.REG_SZ, f'"{executable_path}",0')
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Classes\codex-home-manager\shell\open\command",
        ) as command_key:
            winreg.SetValueEx(command_key, "", 0, winreg.REG_SZ, command)
        logging.info("browser protocol registered: %s", executable_path)
    except Exception as error:
        logging.exception("failed to register browser launch protocol")
        print(f"Warning: failed to register browser launch protocol: {error}", flush=True)


def unregister_browser_protocol() -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg

        for key_path in [
            r"Software\Classes\codex-home-manager\shell\open\command",
            r"Software\Classes\codex-home-manager\shell\open",
            r"Software\Classes\codex-home-manager\shell",
            r"Software\Classes\codex-home-manager\DefaultIcon",
            r"Software\Classes\codex-home-manager",
        ]:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except FileNotFoundError:
                continue
    except Exception as error:
        logging.exception("failed to unregister browser launch protocol")
        print(f"Warning: failed to unregister browser launch protocol: {error}", flush=True)


def existing_connector_is_running() -> bool:
    try:
        with urllib.request.urlopen(connector_probe_url, timeout=1.5) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as error:
        return error.code in {401, 403}
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def port_is_available() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", 8765)) != 0


def open_local_console_after_start() -> None:
    time.sleep(1.5)
    webbrowser.open(local_console_url)


def main() -> None:
    logging.info("connector process starting argv=%s executable=%s", sys.argv, sys.executable)
    if "--unregister-protocol" in sys.argv:
        unregister_browser_protocol()
        notify_user("Codex Home Manager", "Browser launch protocol removed for this Windows user.")
        return

    register_browser_protocol()
    if "--register-protocol" in sys.argv:
        notify_user("Codex Home Manager", "Browser launch protocol installed for this Windows user.")
        return

    if existing_connector_is_running():
        logging.info("existing connector detected; opening local console")
        webbrowser.open(local_console_url)
        return

    if not port_is_available():
        notify_user(
            "Codex Home Manager",
            "Port 8765 is already in use. The local console will open; if it does not load, close the other process using port 8765 first.",
        )
        webbrowser.open(local_console_url)
        return

    start_pending_restart_validation(default_pending_repair_lock)
    threading.Thread(target=open_local_console_after_start, daemon=True).start()
    logging.info("starting uvicorn on 127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, reload=False, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
