from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from backend import pending_repair_validation


def write_pending_lock(tmp_path: Path) -> tuple[Path, Path, Path]:
    backup_root = tmp_path / "codex_full_repair"
    run_root = backup_root / "run-001"
    snapshot_root = run_root / "source_snapshot"
    verifier_path = snapshot_root / "scripts" / "verify_codex_after_restart.py"
    verifier_path.parent.mkdir(parents=True)
    verifier_path.write_text("print('bound verifier')\n", encoding="utf-8")
    manifest_path = run_root / "repair_data" / "repair_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"status":"pending_restart_validation"}', encoding="utf-8")
    binding_path = run_root / "SOURCE_BINDING.json"
    binding_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "snapshot_root": str(snapshot_root),
                "files": [
                    {
                        "path": str(tmp_path / "workspace" / "scripts" / verifier_path.name),
                        "relative_path": "scripts/verify_codex_after_restart.py",
                        "snapshot_path": str(verifier_path),
                        "sha256": hashlib.sha256(verifier_path.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    lock_path = backup_root / "active_repair.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "run_id": "run-001",
                "run_root": str(run_root),
                "status": "pending_restart_validation",
                "manifest": str(manifest_path),
                "codex_home": str(tmp_path / "codex-home"),
                "source_binding": str(binding_path),
                "source_binding_sha256": hashlib.sha256(binding_path.read_bytes()).hexdigest(),
                "source_snapshot_root": str(snapshot_root),
                "python_executable": sys.executable,
            }
        ),
        encoding="utf-8",
    )
    return lock_path, run_root, verifier_path


def test_run_pending_restart_validation_invokes_bound_snapshot_verifier_and_records_result(
    tmp_path: Path,
) -> None:
    lock_path, run_root, verifier_path = write_pending_lock(tmp_path)
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="verified\n", stderr="")

    result = pending_repair_validation.run_pending_restart_validation(lock_path, runner=runner)

    expected_report = run_root / "post_restart_validation.json"
    assert calls == [
        [
            sys.executable,
            str(verifier_path),
            "--codex-home",
            str(tmp_path / "codex-home"),
            "--manifest",
            str(run_root / "repair_data" / "repair_manifest.json"),
            "--report",
            str(expected_report),
        ]
    ]
    assert result["status"] == "complete"
    result_path = run_root / "automatic_restart_validation_result.json"
    assert json.loads(result_path.read_text(encoding="utf-8"))["stdout"] == "verified\n"


def test_start_pending_restart_validation_is_one_shot_and_does_not_poll_without_pending_lock(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "active_repair.lock.json"
    started_targets: list[object] = []

    class RecordingThread:
        def __init__(self, *, target: object, daemon: bool, name: str) -> None:
            assert daemon is True
            assert name == "codex-restart-validation"
            started_targets.append(target)

        def start(self) -> None:
            raise AssertionError("a thread must not start without a pending lock")

    assert pending_repair_validation.start_pending_restart_validation(lock_path, thread_factory=RecordingThread) is False
    lock_path.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    assert pending_repair_validation.start_pending_restart_validation(lock_path, thread_factory=RecordingThread) is False
    assert started_targets == []


def test_start_pending_restart_validation_starts_exactly_one_bounded_worker(tmp_path: Path) -> None:
    lock_path, _run_root, _verifier_path = write_pending_lock(tmp_path)
    starts: list[str] = []

    class RecordingThread:
        def __init__(self, *, target: object, daemon: bool, name: str) -> None:
            assert callable(target)
            assert daemon is False
            self.name = name

        def start(self) -> None:
            starts.append(self.name)

    assert pending_repair_validation.start_pending_restart_validation(lock_path, thread_factory=RecordingThread) is True
    assert starts == ["codex-restart-validation"]


def test_pending_validation_rejects_verifier_outside_bound_source_snapshot(tmp_path: Path) -> None:
    lock_path, run_root, _verifier_path = write_pending_lock(tmp_path)
    outside_verifier = tmp_path / "outside.py"
    outside_verifier.write_text("print('outside')\n", encoding="utf-8")
    lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
    binding_path = Path(lock_payload["source_binding"])
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["files"][0]["snapshot_path"] = str(outside_verifier)
    binding["files"][0]["sha256"] = hashlib.sha256(outside_verifier.read_bytes()).hexdigest()
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    lock_payload["source_binding_sha256"] = hashlib.sha256(binding_path.read_bytes()).hexdigest()
    lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")

    result = pending_repair_validation.run_pending_restart_validation(lock_path)

    assert result["status"] == "failed"
    assert "source snapshot" in result["error"]
    assert (run_root / "automatic_restart_validation_result.json").is_file()


def test_pending_validation_start_is_one_shot_per_process_and_never_polls(tmp_path: Path) -> None:
    lock_path = tmp_path / "active_repair.lock.json"
    lock_path.write_text(
        json.dumps({"status": "pending_restart_validation", "run_id": "run-001"}), encoding="utf-8"
    )
    starts: list[object] = []

    class RecordingThread:
        def __init__(self, *, target: object, daemon: bool, name: str) -> None:
            assert callable(target)
            assert daemon is False
            assert name == "codex-restart-validation"
            self.target = target

        def start(self) -> None:
            starts.append(self.target)

    assert pending_repair_validation.start_pending_restart_validation(
        lock_path, thread_factory=RecordingThread
    ) is True
    assert len(starts) == 1
