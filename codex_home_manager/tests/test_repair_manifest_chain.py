from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

from repair_manifest_chain import load_manifest_pair, manifest_versions_path, write_manifest_pair


def test_manifest_pair_is_backed_by_contiguous_immutable_hash_chain(tmp_path: Path) -> None:
    manifest_path = tmp_path / "repair_data" / "repair_manifest.json"
    payload = {"status": "running", "runner_run_id": "run-a", "run_root": str(tmp_path)}

    first_hash = write_manifest_pair(manifest_path, payload)
    payload["status"] = "pending_restart_validation"
    second_hash = write_manifest_pair(manifest_path, payload)

    versions = sorted(manifest_versions_path(manifest_path).glob("*.json"))
    assert len(versions) == 2
    first = json.loads(versions[0].read_text(encoding="utf-8"))
    second = json.loads(versions[1].read_text(encoding="utf-8"))
    assert first["manifest_sequence"] == 1
    assert first["previous_manifest_sha256"] == ""
    assert second["manifest_sequence"] == 2
    assert second["previous_manifest_sha256"] == first_hash
    assert load_manifest_pair(manifest_path, second_hash)[0]["status"] == "pending_restart_validation"


def test_manifest_chain_rejects_tampered_immutable_version(tmp_path: Path) -> None:
    manifest_path = tmp_path / "repair_data" / "repair_manifest.json"
    payload = {"status": "running", "runner_run_id": "run-a", "run_root": str(tmp_path)}
    current_hash = write_manifest_pair(manifest_path, payload)
    version_path = next(manifest_versions_path(manifest_path).glob("*.json"))
    version_path.write_text("tampered", encoding="utf-8")

    with pytest.raises(RuntimeError, match="immutable version hash mismatch"):
        load_manifest_pair(manifest_path, current_hash)


@pytest.mark.parametrize("failure_stage", ["after_version", "after_mirror", "after_primary"])
def test_manifest_chain_recovers_from_orphaned_uncommitted_writes(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    manifest_path = tmp_path / "repair_data" / "repair_manifest.json"
    payload = {"status": "running", "runner_run_id": "run-a", "run_root": str(tmp_path)}
    first_hash = write_manifest_pair(manifest_path, payload)
    payload["status"] = "pending_restart_validation"

    def fail_at_stage(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(f"simulated crash at {stage}")

    with pytest.raises(RuntimeError, match="simulated crash"):
        write_manifest_pair(manifest_path, payload, fault_injector=fail_at_stage)

    committed_payload, committed_hash = load_manifest_pair(manifest_path)
    assert committed_hash == first_hash
    assert committed_payload["manifest_sequence"] == 1
    assert committed_payload["status"] == "running"

    second_hash = write_manifest_pair(manifest_path, payload)
    recovered_payload, recovered_hash = load_manifest_pair(manifest_path)
    assert recovered_hash == second_hash
    assert recovered_payload["manifest_sequence"] == 2
    assert recovered_payload["previous_manifest_sha256"] == first_hash
    assert recovered_payload["status"] == "pending_restart_validation"


def test_manifest_chain_cli_inspect_returns_committed_head_after_primary_crash(tmp_path: Path) -> None:
    manifest_path = tmp_path / "repair_data" / "repair_manifest.json"
    payload = {"status": "running", "runner_run_id": "run-a", "run_root": str(tmp_path)}
    committed_hash = write_manifest_pair(manifest_path, payload)
    payload["status"] = "uncommitted-primary"

    def fail_after_primary(stage: str) -> None:
        if stage == "after_primary":
            raise RuntimeError("simulated crash after primary")

    with pytest.raises(RuntimeError, match="simulated crash"):
        write_manifest_pair(manifest_path, payload, fault_injector=fail_after_primary)

    completed = subprocess.run(
        [sys.executable, str(scripts_path / "repair_manifest_chain.py"), "--manifest", str(manifest_path), "--inspect"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    inspected = json.loads(completed.stdout)

    assert inspected["sha256"] == committed_hash
    assert inspected["payload"]["status"] == "running"
    assert inspected["payload"]["manifest_sequence"] == 1
