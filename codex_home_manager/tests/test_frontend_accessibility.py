from pathlib import Path
import subprocess
import sys


def test_frontend_accessibility_quality_gate() -> None:
    project_path = Path(__file__).resolve().parents[1]
    npm_command = "npm.cmd" if sys.platform == "win32" else "npm"
    build_process = subprocess.run(
        [npm_command, "run", "build"],
        cwd=project_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert build_process.returncode == 0, build_process.stdout + build_process.stderr

    completed_process = subprocess.run(
        ["node", "tests/frontend_accessibility.test.mjs"],
        cwd=project_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert completed_process.returncode == 0, completed_process.stdout + completed_process.stderr
    assert "frontend accessibility PASS" in completed_process.stdout
