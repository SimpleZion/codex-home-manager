"""Freeze product source commits without modifying the shared working indices."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

import codex_artifact_lifecycle as lifecycle
from export_codex_home_manager_source import export_source_monorepo
from slim_thread_tree_safe import require_memory


shared_sources = (
    "apply_codex_offline_repair.py", "audit_codex_thread_histories.py", "build_home_manager_guarded.py",
    "codex_artifact_lifecycle.py", "codex_directory_layout.py", "codex_plugin_state_snapshot.py",
    "collect_codex_live_validation.py", "desktop_memory_diagnostics.py", "export_codex_home_manager_source.py",
    "live_validation_contract.py", "merge_codex_managed_config.py", "merge_codex_runtime_config.py",
    "migrate_codex_thread_offline.py", "prepare_codex_live_validation.py", "repair_codex_chrome_native_hosts.py",
    "repair_manifest_chain.py", "run_codex_diagnostics_snapshot.py", "slim_thread_tree_safe.py",
    "supersede_pending_repair_validation.py", "verify_codex_after_restart.py",
    "repair_all_codex_after_exit.ps1", "repair_codex_bundled_plugins.ps1", "run_codex_offline_repair_cycle.ps1",
    "prepare_cockpit_source_publication.py",
)
generated_parts = {"node_modules", "target", "build", "dist", "resources", "__pycache__", ".git",
                   ".codex", ".tmp", ".runtime", ".diagnostics", ".pytest_cache", "output", "logs",
                   "release-signing", "git-signing"}


def git_environment():
    environment = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        environment.pop(name, None)
    return environment


def git(repository, *arguments, environment=None, input_bytes=None):
    result = subprocess.run(["git", "-C", str(repository), *arguments], input=input_bytes, env=environment or git_environment(),
                            check=False, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-4000:])
    return result.stdout


def freeze(repository, files, index_path, label):
    if index_path.exists():
        raise RuntimeError("Refusing to overwrite a source snapshot index")
    environment = dict(git_environment(), GIT_INDEX_FILE=str(index_path))
    index_path.parent.mkdir(parents=True, exist_ok=True)
    git(repository, "read-tree", "--empty", environment=environment)
    for name in files:
        path = repository / name
        if generated_parts.intersection(part.casefold() for part in Path(name).parts):
            raise RuntimeError(f"Signing or generated directory in product source: {name}")
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 20 * 1024**2:
            raise RuntimeError(f"Nonregular or excessive product source: {name}")
        if path.suffix.casefold() in {".pem", ".key", ".pfx", ".p12", ".sqlite", ".jsonl", ".exe", ".pdb"}:
            raise RuntimeError(f"Signing/history/generated material in product source: {name}")
    git(repository, "add", "--force", "--pathspec-from-file=-", "--pathspec-file-nul", environment=environment,
        input_bytes=b"\0".join(name.encode("utf-8") for name in files) + b"\0")
    tree = git(repository, "write-tree", environment=environment).decode().strip()
    head = git(repository, "rev-parse", "HEAD").decode().strip()
    commit = git(repository, "-c", "commit.gpgsign=false", "commit-tree", tree, "-p", head,
                 "-m", f"Freeze Codex Cockpit 1.0.34 {label} publication sources", environment=environment).decode().strip()
    git(repository, "update-ref", f"refs/codex-publication/1034-{label}-{commit[:12]}", commit)
    return commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--refresh-manager", action="store_true")
    arguments = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    run_root = arguments.run_root.resolve(strict=True)
    if run_root != lifecycle.build_container() / "cockpit-icon-1034" or run_root.is_symlink():
        raise RuntimeError("Source preparation escapes the authorized Cockpit transaction")
    entry = json.loads(lifecycle.registry_path.read_text(encoding="utf-8"))["runs"][str(run_root)]
    if entry["version"] != "1.0.34" or entry["phase"] != "verified":
        raise RuntimeError("A verified Cockpit 1.0.34 build is required")
    if arguments.refresh_manager:
        stage = run_root / "tmp" / "publication-source"
        source = stage / "source"
        from export_codex_home_manager_source import verify_source_monorepo
        verify_source_monorepo(source, allow_generated=True)
        require_memory(2 * 1024**3)
        app = workspace / "codex_home_manager"
        files = sorted({name.decode("utf-8") for name in git(app, "ls-files", "--cached", "--others",
                       "--exclude-standard", "-z").split(b"\0") if name})
        files = [name for name in files if not generated_parts.intersection(part.casefold() for part in Path(name).parts)
                 and not Path(name).name.startswith(".env") and (app / name).is_file()]
        index = Path(git(app, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        before = hashlib.sha256(index.read_bytes()).hexdigest()
        attempt = 1
        while (stage / f"manager-refresh-{attempt}.index").exists():
            attempt += 1
        commit = freeze(app, files, stage / f"manager-refresh-{attempt}.index", "manager-refresh")
        git(stage / "manager", "-c", "core.autocrlf=false", "checkout", "--quiet", "--detach", commit)
        root_index = Path(git(workspace, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        root_before = hashlib.sha256(root_index.read_bytes()).hexdigest()
        root_files = [".gitattributes", ".source-publication.gitignore", "README.md", "config/storage-layout.json",
                      *(f"scripts/{name}" for name in shared_sources)]
        root_commit = freeze(workspace, root_files, stage / f"root-refresh-{attempt}.index", "root-refresh")
        git(stage / "root", "-c", "core.autocrlf=false", "checkout", "--quiet", "--detach", root_commit)
        result = export_source_monorepo(stage / "root", stage / "manager", stage / "source-updated")
        if hashlib.sha256(index.read_bytes()).hexdigest() != before or hashlib.sha256(root_index.read_bytes()).hexdigest() != root_before:
            raise RuntimeError("Shared working index changed during manager refresh")
        previous_source = stage / f"source-before-refresh-{commit[:12]}"
        dependencies = source / "codex_home_manager/node_modules"
        old_lock = source / "codex_home_manager/package-lock.json"
        new_lock = stage / "source-updated/codex_home_manager/package-lock.json"
        reuse_dependencies = dependencies.is_dir() and old_lock.read_bytes() == new_lock.read_bytes()
        for target in (source, previous_source, dependencies, stage / "source-updated/codex_home_manager/node_modules"):
            target.resolve().relative_to(run_root)
        source.rename(previous_source)
        (stage / "source-updated").rename(source)
        if reuse_dependencies:
            (previous_source / "codex_home_manager/node_modules").rename(source / "codex_home_manager/node_modules")
        verify_source_monorepo(source, allow_generated=reuse_dependencies)
        print(json.dumps(result))
        return 0
    require_memory(4 * 1024**3)
    lifecycle.assert_cockpit_budget(lifecycle.storage_snapshot(), entry)
    stage = run_root / "tmp" / "publication-source"
    if stage.exists():
        raise RuntimeError("Source snapshot stage already exists; inspect its receipt instead of overwriting")
    stage.mkdir(parents=True)
    app = workspace / "codex_home_manager"
    root_files = [".gitattributes", ".source-publication.gitignore", "README.md", "config/storage-layout.json",
                  *(f"scripts/{name}" for name in shared_sources)]
    app_files = sorted({name.decode("utf-8") for name in git(app, "ls-files", "--cached", "--others",
                      "--exclude-standard", "-z").split(b"\0") if name})
    app_files = [name for name in app_files if not generated_parts.intersection(part.casefold() for part in Path(name).parts)
                 and not Path(name).name.startswith(".env") and (app / name).is_file()]
    if len(app_files) > 3000:
        raise RuntimeError("Product source file budget exceeded")
    original_indices = {}
    for name, repository in (("root", workspace), ("manager", app)):
        index = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        original_indices[name] = hashlib.sha256(index.read_bytes()).hexdigest()
    commits = {
        "root": freeze(workspace, root_files, stage / "root.index", "root"),
        "manager": freeze(app, app_files, stage / "manager.index", "manager"),
    }
    snapshots = {}
    for name, repository in (("root", workspace), ("manager", app)):
        destination = stage / name
        git(workspace, "clone", "--quiet", "--shared", "--no-checkout", str(repository), str(destination))
        git(destination, "-c", "core.autocrlf=false", "checkout", "--quiet", "--detach", commits[name])
        snapshots[name] = destination
        index = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        if hashlib.sha256(index.read_bytes()).hexdigest() != original_indices[name]:
            raise RuntimeError("Shared working index changed during source preparation")
    result = export_source_monorepo(snapshots["root"], snapshots["manager"], stage / "source")
    receipt = {"schemaVersion": 1, "commits": commits, "export": result,
               "source": str(stage / "source"), "sharedIndicesUnchanged": True}
    (stage / "source-preparation.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
