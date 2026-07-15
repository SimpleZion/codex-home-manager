from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

import supersede_pending_repair_validation as supersede_module
from repair_manifest_chain import load_manifest_pair, write_manifest_pair


def test_supersede_preserves_current_codex_state_and_updates_bound_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup_root = tmp_path / "Backup"
    run_root = backup_root / "codex_full_repair" / "run-1"
    repair_data = run_root / "repair_data"
    repair_data.mkdir(parents=True)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    state_path = codex_home / "state_5.sqlite"
    state_path.write_bytes(b"current-state-must-not-change")
    state_hash_before = hashlib.sha256(state_path.read_bytes()).hexdigest()

    source_path = tmp_path / "source.py"
    source_path.write_text("new source", encoding="utf-8")
    snapshot_root = run_root / "source_snapshot"
    snapshot_path = snapshot_root / "source.py"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text("old source", encoding="utf-8")
    old_source_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    binding = {
        "schema_version": 2,
        "snapshot_root": str(snapshot_root),
        "files": [
            {
                "path": str(source_path),
                "snapshot_path": str(snapshot_path),
                "sha256": old_source_hash,
            }
        ],
        "repositories": [],
    }
    binding_path = run_root / "SOURCE_BINDING.json"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    binding_hash = hashlib.sha256(binding_path.read_bytes()).hexdigest()

    baseline_audit_path = run_root / "offline_thread_audit.json"
    baseline_audit_path.write_text("{}", encoding="utf-8")
    manifest_path = repair_data / "repair_manifest.json"
    manifest = {
        "schema_version": 1,
        "status": "pending_restart_validation",
        "runner_run_id": "run-id-1",
        "run_root": str(run_root),
        "codex_home": str(codex_home),
        "audit_path": str(baseline_audit_path),
        "audit_sha256": hashlib.sha256(baseline_audit_path.read_bytes()).hexdigest(),
        "prompt_preservation_required": True,
        "checkpoint_history_reduction_enabled": False,
    }
    manifest_hash = write_manifest_pair(manifest_path, manifest)
    lock_path = run_root.parent / "active_repair.lock.json"
    lock = {
        "run_id": "run-id-1",
        "status": "pending_restart_validation",
        "run_root": str(run_root),
        "manifest": str(manifest_path),
        "repair_manifest_sha256": manifest_hash,
        "source_binding": str(binding_path),
        "source_binding_sha256": binding_hash,
        "codex_home": str(codex_home),
    }
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    observed_audit_paths: list[Path] = []
    monkeypatch.setattr(supersede_module, "load_prompt_baseline_audit", lambda *_args: {"threads": []})

    def fake_audit_threads(_state_path: Path, audit_path: Path) -> dict[str, object]:
        observed_audit_paths.append(audit_path)
        audit_path.write_text('{"summary": {}}', encoding="utf-8")
        return {"summary": {}, "threads": []}

    monkeypatch.setattr(supersede_module, "audit_threads", fake_audit_threads)
    monkeypatch.setattr(supersede_module, "validate_post_restart_audit_summary", lambda _summary: None)
    monkeypatch.setattr(
        supersede_module,
        "validate_restart_prompt_contract",
        lambda _baseline, _current: {
            "mode": "baseline_exact_prefix",
            "checked_threads": 0,
            "baseline_prompt_count": 0,
            "appended_prompt_count": 0,
        },
    )

    report_path = run_root / "validation_superseded.json"
    preflight = supersede_module.preflight_pending_validation(
        manifest_path,
        codex_home,
        report_path,
        backup_root,
    )
    assert preflight["status"] == "preflight_passed"
    assert preflight["writes_performed"] is False
    assert preflight["report_path"] == str(report_path)
    assert preflight["audit_path"] == str(run_root / "validation_superseded.thread_audit.json")
    assert not report_path.exists()
    assert not (run_root / "validation_superseded.thread_audit.json").exists()
    assert hashlib.sha256(state_path.read_bytes()).hexdigest() == state_hash_before
    assert load_manifest_pair(manifest_path)[0]["status"] == "pending_restart_validation"
    assert json.loads(lock_path.read_text(encoding="utf-8"))["status"] == "pending_restart_validation"

    report = supersede_module.supersede_pending_validation(
        manifest_path,
        codex_home,
        report_path,
        "validator source changed after a confirmed gate defect",
        backup_root,
    )

    committed_manifest, committed_hash = load_manifest_pair(manifest_path)
    updated_lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert report["status"] == "validation_superseded"
    assert report["preserved_current_codex_state"] is True
    assert committed_manifest["status"] == "validation_superseded"
    assert updated_lock["status"] == "validation_superseded"
    assert updated_lock["repair_manifest_sha256"] == committed_hash
    assert hashlib.sha256(state_path.read_bytes()).hexdigest() == state_hash_before
    assert observed_audit_paths == [run_root / "validation_superseded.thread_audit.json"]


def test_supersession_audit_artifacts_are_unique_per_report_and_never_overwritten(
    tmp_path: Path,
) -> None:
    first_report = tmp_path / "validation_superseded_offline_20260713_140000_a1.json"
    second_report = tmp_path / "validation_superseded_offline_20260713_140001_b2.json"

    first_audit = supersede_module.require_unused_supersession_artifacts(first_report)
    second_audit = supersede_module.require_unused_supersession_artifacts(second_report)

    assert first_audit == tmp_path / "validation_superseded_offline_20260713_140000_a1.thread_audit.json"
    assert second_audit == tmp_path / "validation_superseded_offline_20260713_140001_b2.thread_audit.json"
    assert first_audit != second_audit

    stale_snapshot = first_audit.with_name(f"{first_audit.stem}.state-snapshot.sqlite")
    stale_snapshot.write_bytes(b"preserve prior evidence")

    try:
        supersede_module.require_unused_supersession_artifacts(first_report)
    except RuntimeError as error:
        assert str(stale_snapshot) in str(error)
    else:
        raise AssertionError("existing supersession evidence must block reuse")

    assert stale_snapshot.read_bytes() == b"preserve prior evidence"
