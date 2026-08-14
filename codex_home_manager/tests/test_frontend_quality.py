from pathlib import Path
import subprocess
import sys


def test_frontend_performance_and_i18n_quality_gate() -> None:
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
        ["node", "tests/frontend_quality.test.mjs"],
        cwd=project_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed_process.returncode == 0, completed_process.stdout + completed_process.stderr
