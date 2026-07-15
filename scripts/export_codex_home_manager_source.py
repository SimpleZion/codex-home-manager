from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from typing import Any, Callable


manifest_name = "SOURCE_COMMITS.json"
supported_git_modes = {"100644", "100755"}
manager_workflow_prefix = ".github/workflows/"
required_export_paths = {
    ".gitattributes",
    ".github/workflows/requirements-ci.txt",
    ".github/workflows/source-ci.yml",
    "README.md",
    "scripts/export_codex_home_manager_source.py",
    "scripts/repair_all_codex_after_exit.ps1",
    "scripts/verify_codex_after_restart.py",
    "codex_home_manager/package.json",
    "codex_home_manager/package-lock.json",
    "codex_home_manager/backend/server.py",
    "codex_home_manager/scripts/quality_gate.py",
    "codex_home_manager/tests/test_offline_runner_ps1.py",
}


class SourceExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitTreeEntry:
    source_path: str
    git_mode: str
    git_object: str


@dataclass(frozen=True)
class RepositorySnapshot:
    path: Path
    commit: str
    branch: str
    entries: tuple[GitTreeEntry, ...]


@dataclass(frozen=True)
class ExportFile:
    source_name: str
    source_path: str
    export_path: str
    git_mode: str
    git_object: str
    content: bytes

    def manifest_record(self) -> dict[str, Any]:
        return {
            "path": self.export_path,
            "sourcePath": self.source_path,
            "gitMode": self.git_mode,
            "gitObject": self.git_object,
            "bytes": len(self.content),
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }


def run_git_bytes(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        raise SourceExportError(f"git {' '.join(arguments)} failed in {repository}: {detail}")
    return completed.stdout


def run_git(repository: Path, *arguments: str) -> str:
    return run_git_bytes(repository, *arguments).decode("utf-8", errors="strict")


def same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve(strict=False))) == os.path.normcase(str(second.resolve(strict=False)))


def validate_relative_path(relative_path: str) -> str:
    if not relative_path or "\\" in relative_path or ":" in relative_path or "//" in relative_path:
        raise SourceExportError(f"source contains a non-portable path: {relative_path!r}")
    parsed_path = PurePosixPath(relative_path)
    if parsed_path.is_absolute() or any(part in {"", ".", ".."} for part in parsed_path.parts):
        raise SourceExportError(f"source contains an unsafe relative path: {relative_path!r}")
    return parsed_path.as_posix()


def parse_git_tree(repository: Path) -> tuple[GitTreeEntry, ...]:
    raw_tree = run_git_bytes(repository, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    entries: list[GitTreeEntry] = []
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry:
            continue
        try:
            raw_metadata, raw_path = raw_entry.split(b"\t", 1)
            git_mode, object_type, git_object = raw_metadata.decode("ascii").split(" ")
            source_path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise SourceExportError(f"cannot parse Git tree entry in {repository}") from error
        source_path = validate_relative_path(source_path)
        if object_type != "blob":
            entries.append(GitTreeEntry(source_path, git_mode, git_object))
            continue
        entries.append(GitTreeEntry(source_path, git_mode, git_object))
    return tuple(entries)


def repository_snapshot(
    repository: Path,
    include_path: Callable[[str], bool],
) -> RepositorySnapshot:
    resolved_repository = repository.resolve(strict=True)
    top_level_text = run_git(resolved_repository, "rev-parse", "--show-toplevel").strip()
    top_level = Path(top_level_text).resolve(strict=True)
    if not same_path(resolved_repository, top_level):
        raise SourceExportError(f"repository path must be the Git top level: {resolved_repository}")

    status = run_git_bytes(resolved_repository, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status:
        raise SourceExportError(f"repository must be clean before source export: {resolved_repository}")

    commit = run_git(resolved_repository, "rev-parse", "HEAD").strip()
    branch = run_git(resolved_repository, "branch", "--show-current").strip()
    selected_entries = tuple(entry for entry in parse_git_tree(resolved_repository) if include_path(entry.source_path))
    if not selected_entries:
        raise SourceExportError(f"repository has no selected tracked files: {resolved_repository}")

    for entry in selected_entries:
        if entry.git_mode not in supported_git_modes:
            raise SourceExportError(
                f"source export supports only regular tracked files; {entry.source_path} has Git mode {entry.git_mode}"
            )
    return RepositorySnapshot(resolved_repository, commit, branch, selected_entries)


def assert_snapshot_unchanged(snapshot: RepositorySnapshot) -> None:
    current_commit = run_git(snapshot.path, "rev-parse", "HEAD").strip()
    current_status = run_git_bytes(snapshot.path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if current_commit != snapshot.commit or current_status:
        raise SourceExportError(f"repository changed during source export: {snapshot.path}")


def collect_export_files(
    snapshot: RepositorySnapshot,
    source_name: str,
    export_prefix: str = "",
    export_path_mapper: Callable[[str], str] | None = None,
) -> list[ExportFile]:
    exported_files: list[ExportFile] = []
    for entry in snapshot.entries:
        mapped_path = (
            export_path_mapper(entry.source_path)
            if export_path_mapper
            else f"{export_prefix}{entry.source_path}"
        )
        export_path = validate_relative_path(mapped_path)
        content = run_git_bytes(snapshot.path, "cat-file", "blob", entry.git_object)
        exported_files.append(
            ExportFile(
                source_name=source_name,
                source_path=entry.source_path,
                export_path=export_path,
                git_mode=entry.git_mode,
                git_object=entry.git_object,
                content=content,
            )
        )
    return exported_files


def assert_unique_export_paths(exported_files: list[ExportFile]) -> None:
    exact_paths: set[str] = set()
    portable_paths: dict[str, str] = {}
    for exported_file in exported_files:
        export_path = exported_file.export_path
        if export_path in exact_paths:
            raise SourceExportError(f"duplicate export path: {export_path}")
        exact_paths.add(export_path)
        portable_path = export_path.casefold()
        previous = portable_paths.get(portable_path)
        if previous is not None:
            raise SourceExportError(f"case-insensitive export path collision: {previous} and {export_path}")
        portable_paths[portable_path] = export_path


def build_manifest(
    root_snapshot: RepositorySnapshot,
    manager_snapshot: RepositorySnapshot,
    exported_files: list[ExportFile],
) -> dict[str, Any]:
    def source_manifest(source_name: str, snapshot: RepositorySnapshot, selection: list[str]) -> dict[str, Any]:
        return {
            "branch": snapshot.branch,
            "commit": snapshot.commit,
            "selection": selection,
            "files": [
                exported_file.manifest_record()
                for exported_file in exported_files
                if exported_file.source_name == source_name
            ],
        }

    return {
        "schemaVersion": 2,
        "generatedAt": datetime.now(UTC).isoformat(),
        "layout": {
            "readme": "README.md",
            "sharedScripts": "scripts",
            "workflows": ".github/workflows",
            "managerRepository": "codex_home_manager",
        },
        "sources": {
            "rootRepository": source_manifest(
                "rootRepository",
                root_snapshot,
                [".gitattributes", "README.md", "scripts/**"],
            ),
            "managerRepository": source_manifest(
                "managerRepository",
                manager_snapshot,
                ["**"],
            ),
        },
    }


def export_source_monorepo(root_repository: Path, manager_repository: Path, output_directory: Path) -> dict[str, Any]:
    output_path = output_directory.resolve(strict=False)
    if output_path.exists():
        raise SourceExportError(f"output directory already exists; refusing to overwrite: {output_path}")

    root_snapshot = repository_snapshot(
        root_repository,
        lambda source_path: source_path in {".gitattributes", "README.md"}
        or source_path.startswith("scripts/"),
    )
    manager_snapshot = repository_snapshot(manager_repository, lambda _source_path: True)

    def manager_export_path(source_path: str) -> str:
        if source_path.startswith(manager_workflow_prefix):
            return source_path
        return f"codex_home_manager/{source_path}"

    exported_files = [
        *collect_export_files(root_snapshot, "rootRepository"),
        *collect_export_files(manager_snapshot, "managerRepository", export_path_mapper=manager_export_path),
    ]
    exported_files.sort(key=lambda exported_file: exported_file.export_path)
    assert_unique_export_paths(exported_files)

    exported_paths = {exported_file.export_path for exported_file in exported_files}
    missing_paths = sorted(required_export_paths - exported_paths)
    if missing_paths:
        raise SourceExportError(f"source export is incomplete: {', '.join(missing_paths)}")

    assert_snapshot_unchanged(root_snapshot)
    assert_snapshot_unchanged(manager_snapshot)
    manifest = build_manifest(root_snapshot, manager_snapshot, exported_files)

    output_path.mkdir(parents=True, exist_ok=False)
    for exported_file in exported_files:
        destination_path = output_path.joinpath(*PurePosixPath(exported_file.export_path).parts)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with destination_path.open("xb") as destination_file:
            destination_file.write(exported_file.content)
        if exported_file.git_mode == "100755" and os.name != "nt":
            destination_path.chmod(destination_path.stat().st_mode | 0o111)

    manifest_path = output_path / manifest_name
    with manifest_path.open("x", encoding="utf-8", newline="\n") as manifest_file:
        json.dump(manifest, manifest_file, ensure_ascii=False, indent=2)
        manifest_file.write("\n")

    root_file_count = sum(exported_file.source_name == "rootRepository" for exported_file in exported_files)
    manager_file_count = sum(exported_file.source_name == "managerRepository" for exported_file in exported_files)
    return {
        "output": str(output_path),
        "rootCommit": root_snapshot.commit,
        "managerCommit": manager_snapshot.commit,
        "rootFiles": root_file_count,
        "managerFiles": manager_file_count,
        "manifest": str(manifest_path),
    }


def scan_export_files(source_path: Path) -> set[str]:
    discovered_files: set[str] = set()
    for current_root, directory_names, file_names in os.walk(source_path, followlinks=False):
        current_path = Path(current_root)
        relative_root = current_path.relative_to(source_path)
        if relative_root == Path(".") and ".git" in directory_names:
            directory_names.remove(".git")
        for directory_name in list(directory_names):
            directory_path = current_path / directory_name
            if directory_path.is_symlink():
                relative_path = directory_path.relative_to(source_path).as_posix()
                discovered_files.add(relative_path)
                directory_names.remove(directory_name)
        for file_name in file_names:
            relative_path = (current_path / file_name).relative_to(source_path).as_posix()
            if relative_path == ".git":
                continue
            discovered_files.add(relative_path)
    return discovered_files


def validate_manifest_record(source_path: Path, record: Any) -> tuple[str, str]:
    if not isinstance(record, dict):
        raise SourceExportError("source manifest contains a non-object file record")
    export_path = validate_relative_path(record.get("path") if isinstance(record.get("path"), str) else "")
    original_source_path = validate_relative_path(
        record.get("sourcePath") if isinstance(record.get("sourcePath"), str) else ""
    )
    if record.get("gitMode") not in supported_git_modes:
        raise SourceExportError(f"source manifest has an unsupported Git mode: {export_path}")
    if not isinstance(record.get("gitObject"), str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record["gitObject"]) is None:
        raise SourceExportError(f"source manifest has an invalid Git object: {export_path}")
    if not isinstance(record.get("bytes"), int) or isinstance(record.get("bytes"), bool) or record["bytes"] < 0:
        raise SourceExportError(f"source manifest has an invalid byte count: {export_path}")
    if not isinstance(record.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None:
        raise SourceExportError(f"source manifest has an invalid SHA-256: {export_path}")

    file_path = source_path.joinpath(*PurePosixPath(export_path).parts)
    if file_path.is_symlink() or not file_path.is_file():
        raise SourceExportError(f"manifest-listed source file is missing or not regular: {export_path}")
    content = file_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != record["sha256"]:
        raise SourceExportError(f"SHA-256 mismatch for source file: {export_path}")
    if len(content) != record["bytes"]:
        raise SourceExportError(f"byte count mismatch for source file: {export_path}")
    return export_path, original_source_path


def verify_source_monorepo(source_directory: Path) -> dict[str, Any]:
    source_path = source_directory.resolve(strict=True)
    if not source_path.is_dir():
        raise SourceExportError(f"source monorepo is not a directory: {source_path}")
    manifest_path = source_path / manifest_name
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SourceExportError(f"source manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceExportError(f"cannot read source manifest: {manifest_path}") from error

    expected_layout = {
        "readme": "README.md",
        "sharedScripts": "scripts",
        "workflows": ".github/workflows",
        "managerRepository": "codex_home_manager",
    }
    if manifest.get("schemaVersion") != 2 or manifest.get("layout") != expected_layout:
        raise SourceExportError("source manifest has an unsupported schema or layout")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or set(sources) != {"rootRepository", "managerRepository"}:
        raise SourceExportError("source manifest does not describe both source repositories")

    expected_files: set[str] = set()
    portable_paths: dict[str, str] = {}
    commits: dict[str, str] = {}
    for source_name in ("rootRepository", "managerRepository"):
        source_record = sources[source_name]
        if not isinstance(source_record, dict) or not isinstance(source_record.get("files"), list):
            raise SourceExportError(f"source manifest has an invalid {source_name} record")
        commit = source_record.get("commit")
        if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
            raise SourceExportError(f"source manifest has an invalid {source_name} commit")
        commits[source_name] = commit
        for file_record in source_record["files"]:
            export_path, original_source_path = validate_manifest_record(source_path, file_record)
            if source_name == "rootRepository":
                if export_path != original_source_path or not (
                    export_path in {".gitattributes", "README.md"}
                    or export_path.startswith("scripts/")
                ):
                    raise SourceExportError(f"root repository file is outside the monorepo layout: {export_path}")
            elif original_source_path.startswith(manager_workflow_prefix):
                if export_path != original_source_path:
                    raise SourceExportError(f"manager workflow is outside the source workflow root: {export_path}")
            elif export_path != f"codex_home_manager/{original_source_path}":
                raise SourceExportError(f"manager repository file is outside the monorepo layout: {export_path}")
            if export_path in expected_files:
                raise SourceExportError(f"duplicate source manifest path: {export_path}")
            portable_path = export_path.casefold()
            if portable_path in portable_paths:
                raise SourceExportError(
                    f"case-insensitive source manifest collision: {portable_paths[portable_path]} and {export_path}"
                )
            portable_paths[portable_path] = export_path
            expected_files.add(export_path)

    missing_required_paths = sorted(required_export_paths - expected_files)
    if missing_required_paths:
        raise SourceExportError(f"source manifest is incomplete: {', '.join(missing_required_paths)}")
    actual_files = scan_export_files(source_path)
    expected_tree_files = expected_files | {manifest_name}
    missing_files = sorted(expected_tree_files - actual_files)
    unlisted_files = sorted(actual_files - expected_tree_files)
    if missing_files:
        raise SourceExportError(f"source export has missing files: {', '.join(missing_files)}")
    if unlisted_files:
        raise SourceExportError(f"source export has unlisted files: {', '.join(unlisted_files)}")
    return {
        "files": len(expected_files),
        "rootCommit": commits["rootRepository"],
        "managerCommit": commits["managerRepository"],
    }


def clean_python_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_captured_process(
    command: list[str],
    working_directory: Path,
    *,
    input_text: str | None = None,
) -> None:
    completed = subprocess.run(
        command,
        cwd=working_directory,
        input=input_text,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=clean_python_environment(),
    )
    if completed.returncode != 0:
        detail = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        if len(detail) > 6000:
            detail = detail[-6000:]
        raise SourceExportError(f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}")


def run_python_import_check(source_path: Path) -> None:
    import_probe = r'''
import importlib
import importlib.util
from pathlib import Path
import sys

source_root = Path(sys.argv[1]).resolve(strict=True)
scripts_root = source_root / "scripts"
manager_root = source_root / "codex_home_manager"
manager_scripts = manager_root / "scripts"
sys.path[:0] = [str(scripts_root), str(manager_scripts), str(manager_root)]

for script_path in sorted(scripts_root.glob("*.py")):
    importlib.import_module(script_path.stem)

importlib.import_module("backend")
for module_path in sorted((manager_root / "backend").glob("*.py")):
    if module_path.stem != "__init__":
        importlib.import_module(f"backend.{module_path.stem}")

def import_file(module_path: Path, module_name: str) -> None:
    module_spec = importlib.util.spec_from_file_location(module_name, module_path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot create import spec for {module_path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    module_spec.loader.exec_module(module)

for index, module_path in enumerate(sorted(manager_scripts.glob("*.py"))):
    import_file(module_path, f"_source_manager_script_{index}")

connector_path = manager_root / "packaging" / "windows" / "connector_main.py"
if connector_path.is_file():
    import_file(connector_path, "_source_windows_connector")
'''
    run_captured_process(
        [sys.executable, "-I", "-B", "-c", import_probe, str(source_path)],
        source_path,
    )


def run_powershell_parser_check(source_path: Path) -> None:
    powershell_path = shutil.which("pwsh")
    if powershell_path is None:
        raise SourceExportError("PowerShell 7 executable not found: pwsh")
    powershell_files = sorted(path.relative_to(source_path).as_posix() for path in source_path.rglob("*.ps1"))
    if not powershell_files:
        raise SourceExportError("source monorepo has no PowerShell files to parse")
    parser_probe = r'''
$relativePaths = @([Console]::In.ReadToEnd() | ConvertFrom-Json)
$failures = [System.Collections.Generic.List[string]]::new()
foreach ($relativePath in $relativePaths) {
    $tokens = $null
    $parseErrors = $null
    $fullPath = Join-Path (Get-Location) ([string]$relativePath)
    [System.Management.Automation.Language.Parser]::ParseFile($fullPath, [ref]$tokens, [ref]$parseErrors) | Out-Null
    foreach ($parseError in @($parseErrors)) {
        $failures.Add("$relativePath`: $($parseError.Message)")
    }
}
if ($failures.Count -gt 0) {
    throw ($failures -join [Environment]::NewLine)
}
'''
    run_captured_process(
        [powershell_path, "-NoProfile", "-NonInteractive", "-Command", parser_probe],
        source_path,
        input_text=json.dumps(powershell_files),
    )


def install_node_dependencies(source_path: Path) -> None:
    npm_path = shutil.which("npm")
    if npm_path is None:
        raise SourceExportError("npm executable not found")
    manager_path = source_path / "codex_home_manager"
    if (manager_path / "node_modules").exists():
        raise SourceExportError("node_modules already exists; dependency installation requires a fresh source export")
    run_captured_process(
        [npm_path, "ci", "--ignore-scripts", "--no-fund", "--no-audit"],
        manager_path,
    )


def run_core_gate(source_path: Path) -> None:
    manager_path = source_path / "codex_home_manager"
    gate_path = manager_path / "scripts" / "quality_gate.py"
    run_captured_process(
        [sys.executable, "-I", "-B", str(gate_path)],
        manager_path,
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export or verify the complete Codex Home Manager source monorepo.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export clean committed sources into a new directory.")
    export_parser.add_argument("--root-repository", required=True, type=Path)
    export_parser.add_argument("--manager-repository", required=True, type=Path)
    export_parser.add_argument("--output", required=True, type=Path)

    verify_parser = subparsers.add_parser("verify", help="Verify an exported or cloned source monorepo.")
    verify_parser.add_argument("--source", required=True, type=Path)
    verify_parser.add_argument(
        "--install-node-dependencies",
        action="store_true",
        help="Run npm ci only after strict source integrity verification.",
    )
    verify_parser.add_argument(
        "--run-gate",
        action="store_true",
        help="Run codex_home_manager/scripts/quality_gate.py after import and parser checks.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        if arguments.command == "export":
            result = export_source_monorepo(
                arguments.root_repository,
                arguments.manager_repository,
                arguments.output,
            )
        else:
            source_path = arguments.source.resolve(strict=True)
            integrity_result = verify_source_monorepo(source_path)
            run_python_import_check(source_path)
            run_powershell_parser_check(source_path)
            if arguments.install_node_dependencies:
                install_node_dependencies(source_path)
            core_gate_status = "not-run"
            if arguments.run_gate:
                run_core_gate(source_path)
                core_gate_status = "ok"
            result = {
                "source": str(source_path),
                "integrity": "ok",
                "pythonImports": "ok",
                "powerShellParser": "ok",
                "nodeDependencies": "installed" if arguments.install_node_dependencies else "not-installed",
                "coreGate": core_gate_status,
                **integrity_result,
            }
    except (OSError, SourceExportError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
