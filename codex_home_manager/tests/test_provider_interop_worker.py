from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from backend.provider_interop.models import ProviderInteropError
from backend.provider_interop.worker import NativeWorkerClient


def write_worker(path: Path, *, mismatch: bool = False) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, sys",
                "request = json.load(sys.stdin)",
                f"nonce = 'wrong' if {mismatch!r} else request['nonce']",
                "json.dump({'protocolVersion': request['protocolVersion'], 'operationId': request['operationId'], 'nonce': nonce, 'stateDigest': request['stateDigest'], 'status': 'ok', 'payload': {'route': 'official'}, 'errors': []}, sys.stdout)",
            ]
        ),
        encoding="utf-8",
    )


def test_worker_client_validates_all_bindings(tmp_path: Path) -> None:
    script = tmp_path / "worker.py"
    write_worker(script)
    client = NativeWorkerClient([sys.executable, str(script)])
    result = client.call("inspect", tmp_path, state_digest="digest", operation_id="operation")
    assert result["payload"] == {"route": "official"}


def test_worker_client_rejects_nonce_mismatch(tmp_path: Path) -> None:
    script = tmp_path / "worker.py"
    write_worker(script, mismatch=True)
    client = NativeWorkerClient([sys.executable, str(script)])
    with pytest.raises(ProviderInteropError, match="binding") as captured:
        client.call("inspect", tmp_path, state_digest="digest", operation_id="operation")
    assert captured.value.code == "native_worker_binding_mismatch"


def test_worker_client_does_not_expose_input_in_invalid_output(tmp_path: Path) -> None:
    script = tmp_path / "worker.py"
    script.write_text("print('not-json')", encoding="utf-8")
    client = NativeWorkerClient([sys.executable, str(script)])
    with pytest.raises(ProviderInteropError) as captured:
        client.call(
            "inspect",
            tmp_path,
            state_digest="digest",
            payload={"experimental_bearer_token": "fixture-secret"},
        )
    assert "fixture-secret" not in str(captured.value)
