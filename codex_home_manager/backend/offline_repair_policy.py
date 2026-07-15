from __future__ import annotations

from pathlib import Path
from typing import Any

import psutil

from .windows_paths import windows_path_is_within


default_backup_root = Path(r"D:\Backup")


def related_codex_processes() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    inspection_errors: list[str] = []
    candidate_names = {
        "chatgpt.exe",
        "codex.exe",
        "node_repl.exe",
        "codex-code-mode-host.exe",
        "codex-command-runner.exe",
        "codex-home-manager-local-win-x64.exe",
        "cmd.exe",
        "node.exe",
        "extension-host.exe",
    }
    for process in psutil.process_iter(["pid", "name"]):
        name = str(process.info.get("name") or "").casefold()
        if name not in candidate_names:
            continue
        try:
            executable = str(process.exe() or "")
            command_line = " ".join(str(part) for part in process.cmdline())
        except psutil.AccessDenied as error:
            inspection_errors.append(f"pid={process.pid} name={name}: {error}")
            continue
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        normalized = command_line.replace("/", "\\").casefold()
        related = (
            name in {
                "chatgpt.exe",
                "codex.exe",
                "node_repl.exe",
                "codex-code-mode-host.exe",
                "codex-command-runner.exe",
                "codex-home-manager-local-win-x64.exe",
            }
            or "xcodebuildmcp" in normalized
            or "mcp\\server.mjs" in normalized
            or "mcp\\server.bundle.mjs" in normalized
            or "mcp\\server.cjs" in normalized
            or ("\\.codex\\plugins\\" in normalized and "extension-host" in normalized)
        )
        if related:
            found.append(
                {
                    "pid": process.pid,
                    "name": process.info.get("name"),
                    "exe": executable,
                    "cmdline": command_line,
                }
            )
    if inspection_errors:
        raise RuntimeError(f"could not inspect possible Codex processes: {inspection_errors}")
    return found


def assert_codex_offline() -> None:
    processes = related_codex_processes()
    if processes:
        raise RuntimeError(f"Codex process is running: {processes[:8]}")


def assert_backup_path(path: Path, required_root: Path = default_backup_root) -> None:
    if not windows_path_is_within(path.resolve(), required_root.resolve()):
        raise RuntimeError(f"backup root must stay within {required_root.resolve()}: {path.resolve()}")
