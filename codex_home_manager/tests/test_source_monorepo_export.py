from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


export_script_path = Path(__file__).resolve().parents[2] / "scripts" / "export_codex_home_manager_source.py"
spec = importlib.util.spec_from_file_location("export_codex_home_manager_source", export_script_path)
assert spec and spec.loader
export_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = export_module
spec.loader.exec_module(export_module)


def run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def create_repository(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    run_git(path, "init", "--quiet")
    run_git(path, "config", "user.email", "test@example.com")
    run_git(path, "config", "user.name", "Test")
    for relative_path, content in files.items():
        destination = path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    run_git(path, "add", ".")
    run_git(path, "commit", "--quiet", "-m", "source")
    return path


def source_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root_repository = create_repository(
        tmp_path / "root",
        {
            ".gitattributes": "* text=auto eol=lf\n",
            "README.md": "# Exported source\n",
            "scripts/export_codex_home_manager_source.py": export_script_path.read_text(encoding="utf-8"),
            "scripts/repair_all_codex_after_exit.ps1": "Write-Output repair\n",
            "scripts/verify_codex_after_restart.py": "verification_value = 1\n",
            "scripts/root_helper.py": "root_value = 7\n",
            "AGENTS.md": "workspace-only rules\n",
            "private/diagnostic.txt": "must not be exported\n",
        },
    )
    manager_repository = create_repository(
        tmp_path / "manager",
        {
            "package.json": json.dumps(
                {
                    "name": "source-export-fixture",
                    "version": "1.0.0",
                    "scripts": {"gate": "python scripts/quality_gate.py"},
                }
            )
            + "\n",
            "package-lock.json": json.dumps(
                {
                    "name": "source-export-fixture",
                    "version": "1.0.0",
                    "lockfileVersion": 3,
                    "requires": True,
                    "packages": {"": {"name": "source-export-fixture", "version": "1.0.0"}},
                }
            )
            + "\n",
            ".github/workflows/source-ci.yml": "name: Source CI\n",
            ".github/workflows/requirements-ci.txt": "pytest==8.4.2\n",
            "backend/__init__.py": "\n",
            "backend/server.py": "from root_helper import root_value\nserver_value = root_value\n",
            "scripts/manager_helper.py": "manager_value = 11\n",
            "scripts/quality_gate.py": (
                "from pathlib import Path\n"
                "import sys\n"
                "manager_root = Path(__file__).resolve().parents[1]\n"
                "source_root = manager_root.parent\n"
                "sys.path.insert(0, str(source_root / 'scripts'))\n"
                "sys.path.insert(0, str(manager_root))\n"
                "from backend.server import server_value\n"
                "if __name__ == '__main__':\n"
                "    assert Path.cwd() == manager_root\n"
                "    assert server_value == 7\n"
                "    assert (source_root / 'scripts' / 'repair_all_codex_after_exit.ps1').is_file()\n"
                "    (source_root / 'GATE_RAN').write_text('ok\\n', encoding='utf-8')\n"
            ),
            "scripts/check-source.ps1": "$value = 1\n",
            "tests/test_offline_runner_ps1.py": "def test_placeholder(): pass\n",
            "src/main.tsx": "export {}\n",
        },
    )
    return root_repository, manager_repository


def test_export_contains_only_monorepo_sources_and_commit_manifest(tmp_path: Path) -> None:
    root_repository, manager_repository = source_fixture(tmp_path)
    output = tmp_path / "export"

    result = export_module.export_source_monorepo(root_repository, manager_repository, output)

    assert result["rootFiles"] == 6
    assert result["managerFiles"] == 11
    assert (output / "README.md").is_file()
    assert (output / ".gitattributes").read_text(encoding="utf-8") == "* text=auto eol=lf\n"
    assert (output / ".github" / "workflows" / "source-ci.yml").is_file()
    assert (output / ".github" / "workflows" / "requirements-ci.txt").is_file()
    assert (output / "scripts" / "repair_all_codex_after_exit.ps1").is_file()
    assert (output / "codex_home_manager" / "backend" / "server.py").is_file()
    assert not (output / "codex_home_manager" / ".github").exists()
    assert not (output / "AGENTS.md").exists()
    assert not (output / "private").exists()

    manifest = json.loads((output / "SOURCE_COMMITS.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 2
    assert manifest["layout"] == {
        "readme": "README.md",
        "sharedScripts": "scripts",
        "workflows": ".github/workflows",
        "managerRepository": "codex_home_manager",
    }
    assert manifest["sources"]["rootRepository"]["commit"] == result["rootCommit"]
    assert manifest["sources"]["managerRepository"]["commit"] == result["managerCommit"]

    records = [
        *manifest["sources"]["rootRepository"]["files"],
        *manifest["sources"]["managerRepository"]["files"],
    ]
    assert {record["path"] for record in records} == {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path.name != "SOURCE_COMMITS.json"
    }
    for record in records:
        content = (output / record["path"]).read_bytes()
        assert record["bytes"] == len(content)
        assert record["sha256"] == hashlib.sha256(content).hexdigest()
        assert record["gitMode"] in {"100644", "100755"}
        assert len(record["gitObject"]) in {40, 64}


@pytest.mark.parametrize("dirty_repository", ["root", "manager"])
def test_export_refuses_dirty_source_repository(tmp_path: Path, dirty_repository: str) -> None:
    root_repository, manager_repository = source_fixture(tmp_path)
    repository = root_repository if dirty_repository == "root" else manager_repository
    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(export_module.SourceExportError, match="must be clean"):
        export_module.export_source_monorepo(root_repository, manager_repository, tmp_path / "export")

    assert not (tmp_path / "export").exists()


def test_export_accepts_git_worktree_repository(tmp_path: Path) -> None:
    root_repository, manager_repository = source_fixture(tmp_path)
    root_worktree = tmp_path / "root-worktree"
    run_git(root_repository, "worktree", "add", "--quiet", "--detach", str(root_worktree))

    result = export_module.export_source_monorepo(root_worktree, manager_repository, tmp_path / "export")

    assert result["rootCommit"] == run_git(root_repository, "rev-parse", "HEAD").strip()


def test_export_refuses_to_overwrite_existing_directory(tmp_path: Path) -> None:
    root_repository, manager_repository = source_fixture(tmp_path)
    output = tmp_path / "export"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(export_module.SourceExportError, match="refusing to overwrite"):
        export_module.export_source_monorepo(root_repository, manager_repository, output)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_verifier_rejects_hash_drift_and_unlisted_files(tmp_path: Path) -> None:
    root_repository, manager_repository = source_fixture(tmp_path)
    output = tmp_path / "export"
    export_module.export_source_monorepo(root_repository, manager_repository, output)

    server_path = output / "codex_home_manager" / "backend" / "server.py"
    original = server_path.read_bytes()
    server_path.write_bytes(original + b"# drift\n")
    with pytest.raises(export_module.SourceExportError, match="SHA-256 mismatch"):
        export_module.verify_source_monorepo(output)

    server_path.write_bytes(original)
    (output / "unlisted.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(export_module.SourceExportError, match="unlisted files"):
        export_module.verify_source_monorepo(output)


@pytest.mark.skipif(not subprocess.run(["pwsh", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.Major"], capture_output=True).returncode == 0, reason="PowerShell 7 is required")
def test_exported_verifier_runs_import_parser_and_core_gate_in_isolation(tmp_path: Path) -> None:
    root_repository, manager_repository = source_fixture(tmp_path)
    output = tmp_path / "export"
    export_module.export_source_monorepo(root_repository, manager_repository, output)
    exported_script = output / "scripts" / "export_codex_home_manager_source.py"

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(exported_script),
            "verify",
            "--source",
            str(output),
            "--install-node-dependencies",
            "--run-gate",
        ],
        cwd=output,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={key: value for key, value in os.environ.items() if key.upper() not in {"PYTHONHOME", "PYTHONPATH"}},
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads([line for line in completed.stdout.splitlines() if line.strip()][-1])
    assert payload["status"] == "ok"
    assert payload["integrity"] == "ok"
    assert payload["pythonImports"] == "ok"
    assert payload["powerShellParser"] == "ok"
    assert payload["coreGate"] == "ok"
    assert (output / "GATE_RAN").read_text(encoding="utf-8") == "ok\n"
    assert not list(output.rglob("__pycache__"))
    assert not list(output.rglob("*.pyc"))
