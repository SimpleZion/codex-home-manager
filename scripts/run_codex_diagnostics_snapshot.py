from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "codex_home_manager"))

from backend.diagnostics import run_codex_diagnostics


def required_check_failures(report: dict[str, object], required_check_ids: list[str]) -> list[str]:
    checks = {
        str(check.get("id")): check
        for check in report.get("checks", [])
        if isinstance(check, dict) and check.get("id")
    }
    failures: list[str] = []
    for check_id in required_check_ids:
        check = checks.get(check_id)
        if check is None:
            failures.append(f"{check_id}: missing")
            continue
        status = str(check.get("status") or "unknown")
        if status != "pass":
            failures.append(f"{check_id}: {status} - {check.get('summary')}")
    return failures


def diagnostics_gate_failures(report: dict[str, object], required_check_ids: list[str]) -> list[str]:
    failures = required_check_failures(report, required_check_ids)
    required_ids = set(required_check_ids)
    for check in report.get("checks", []):
        if not isinstance(check, dict):
            continue
        check_id = str(check.get("id") or "")
        if check_id in required_ids or str(check.get("status") or "") != "critical":
            continue
        failures.append(f"{check_id}: critical - {check.get('summary')}")
    if str(report.get("status") or "") == "critical" and not failures:
        failures.append("report: critical - no critical check detail was emitted")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a Codex Home Manager diagnostics snapshot.")
    parser.add_argument("--codex-home", default=r"D:\.codex")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--sidebar-limit", type=int, default=1000)
    parser.add_argument("--comprehensive-event-stream", action="store_true")
    parser.add_argument("--require-pass", action="append", default=[])
    arguments = parser.parse_args()
    report = run_codex_diagnostics(
        codex_home_text=arguments.codex_home,
        sidebar_limit=arguments.sidebar_limit,
        language=arguments.language,
        comprehensive_event_stream=arguments.comprehensive_event_stream,
    )
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = diagnostics_gate_failures(report, arguments.require_pass)
    print(
        json.dumps(
            {
                "score": report.get("score"),
                "status": report.get("status"),
                "summary": report.get("summary"),
                "requiredCheckFailures": failures,
            },
            ensure_ascii=False,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
