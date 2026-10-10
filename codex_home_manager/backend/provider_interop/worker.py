from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .models import ProviderInteropError, redact_public


protocol_version = 1
max_worker_output_bytes = 32 * 1024 * 1024


class NativeWorkerClient:
    def __init__(self, command: Sequence[str] | None = None, timeout_seconds: float = 900) -> None:
        self.command = tuple(command) if command is not None else (str(resolve_native_worker()),)
        self.timeout_seconds = timeout_seconds

    def call(
        self,
        stage: str,
        codex_home: str | Path,
        *,
        adapter_root: str | Path | None = None,
        state_digest: str,
        payload: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        operation_id = operation_id or str(uuid.uuid4())
        nonce = secrets.token_urlsafe(24)
        request = {
            "protocolVersion": protocol_version,
            "operationId": operation_id,
            "nonce": nonce,
            "stage": stage,
            "codexHome": str(Path(codex_home).resolve(strict=False)),
            "adapterRoot": str(Path(adapter_root).resolve(strict=False)) if adapter_root else None,
            "stateDigest": state_digest,
            "payload": payload or {},
        }
        completed = run_hidden_worker(
            [*self.command, "provider-interop-worker"],
            json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            self.timeout_seconds,
        )
        if len(completed.stdout.encode("utf-8", errors="replace")) > max_worker_output_bytes:
            raise ProviderInteropError("native_worker_output_too_large", "native worker output exceeded the limit")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ProviderInteropError("native_worker_invalid_output", "native worker did not return one JSON result") from error
        if completed.returncode != 0:
            errors = result.get("errors") if isinstance(result, dict) else None
            first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
            raise ProviderInteropError(
                str(first.get("code") or "native_worker_failed"),
                str(first.get("message") or "native worker operation failed"),
            )
        expected = {
            "protocolVersion": protocol_version,
            "operationId": operation_id,
            "nonce": nonce,
            "stateDigest": state_digest,
        }
        if not isinstance(result, dict) or any(result.get(key) != value for key, value in expected.items()):
            raise ProviderInteropError("native_worker_binding_mismatch", "native worker result binding did not match the request")
        if result.get("status") != "ok" or result.get("errors"):
            raise ProviderInteropError("native_worker_failed", "native worker returned an unsuccessful result")
        return redact_public(result)


def resolve_native_worker() -> Path:
    configured = os.environ.get("CODEX_HOME_MANAGER_NATIVE_SERVICE")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    package_root = Path(__file__).resolve().parents[2]
    executable_name = "codex-home-manager-service.exe" if os.name == "nt" else "codex-home-manager-service"
    candidates.extend(
        [
            package_root / "src-tauri" / "target" / "release" / executable_name,
            package_root / "src-tauri" / "target" / "debug" / executable_name,
        ]
    )
    bundle_root = Path(getattr(sys, "_MEIPASS", package_root))
    candidates.extend(
        [
            bundle_root / "service" / executable_name,
            Path(sys.executable).resolve().parent.parent / "service" / executable_name,
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ProviderInteropError("native_worker_unavailable", "provider interoperability native worker is unavailable")


def run_hidden_worker(arguments: Sequence[str], input_text: str, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    startup_info = None
    creation_flags = 0
    if os.name == "nt":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE
        creation_flags = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(
            list(arguments),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            startupinfo=startup_info,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired as error:
        raise ProviderInteropError("native_worker_timeout", "native worker operation timed out") from error
    except OSError as error:
        raise ProviderInteropError("native_worker_unavailable", "native worker could not be started") from error
