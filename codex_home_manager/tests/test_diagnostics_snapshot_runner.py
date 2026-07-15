from __future__ import annotations

import sys
from pathlib import Path


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

import run_codex_diagnostics_snapshot
from run_codex_diagnostics_snapshot import required_check_failures


def test_required_check_failures_rejects_missing_and_non_pass_checks() -> None:
    report = {
        "checks": [
            {"id": "plugins.browser", "status": "pass", "summary": "ok"},
            {"id": "plugins.sites", "status": "warning", "summary": "missing"},
        ]
    }

    failures = required_check_failures(
        report,
        ["plugins.browser", "plugins.sites", "plugins.skill_manifests"],
    )

    assert failures == [
        "plugins.sites: warning - missing",
        "plugins.skill_manifests: missing",
    ]


def test_diagnostics_gate_rejects_any_unlisted_critical_check() -> None:
    report = {
        "status": "critical",
        "checks": [
            {"id": "plugins.browser", "status": "pass", "summary": "ok"},
            {"id": "threads.main_event_stream", "status": "critical", "summary": "history missing"},
        ],
    }

    failures = run_codex_diagnostics_snapshot.diagnostics_gate_failures(report, ["plugins.browser"])

    assert failures == ["threads.main_event_stream: critical - history missing"]
