from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any


def run_hidden_command(
    arguments: Sequence[str],
    timeout_seconds: float = 10,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    startup_info = None
    creation_flags = 0
    if os.name == "nt":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE
        creation_flags = subprocess.CREATE_NO_WINDOW

    try:
        completed = subprocess.run(
            list(arguments),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            env=dict(environment) if environment is not None else None,
            startupinfo=startup_info,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"returnCode": -1, "stdout": "", "stderr": "", "error": str(error)}

    return {
        "returnCode": int(completed.returncode),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "error": "",
    }
