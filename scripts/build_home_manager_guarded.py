"""Build Cockpit serially and stop only this build tree under memory pressure."""
import argparse
import atexit
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import psutil

import codex_artifact_lifecycle as lifecycle
from slim_thread_tree_safe import available_memory_bytes


def run_exported_frontend(root):
    """Validate an exported source tree without invoking workstation deployment."""
    source_root = root.parent.resolve(strict=True)
    run_root_text = os.environ.get("CODEX_STORAGE_RUN_ROOT")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if source_root != Path(os.environ["GITHUB_WORKSPACE"]).resolve(strict=True):
            raise RuntimeError("CI source root does not match the checked-out workspace")
    elif not run_root_text or not source_root.is_relative_to(Path(run_root_text).resolve(strict=True) / "tmp"):
        raise RuntimeError("Local source validation must remain inside an admitted build's tmp directory")
    from export_codex_home_manager_source import verify_source_monorepo
    verify_source_monorepo(source_root, allow_generated=True)
    node = shutil.which("node.exe")
    if node is None:
        raise RuntimeError("Node.js is required for source frontend validation")
    if available_memory_bytes() < 2 * 1024**3:
        raise RuntimeError("Less than 2 GiB available; source frontend verification not started")
    cached = subprocess.run([node, "scripts/frontend-release-artifact.mjs", "verify-build"], cwd=root,
        capture_output=True, timeout=30)
    if cached.returncode == 0:
        print("Source frontend build reused after exact source, toolchain and artifact verification", flush=True)
        return 0
    if available_memory_bytes() < 4 * 1024**3:
        raise RuntimeError("Less than 4 GiB available; source frontend build not started")
    environment = dict(os.environ, NODE_OPTIONS="--max-old-space-size=1536")
    for command in ([node, "node_modules/typescript/bin/tsc", "-b", "--pretty", "false"],
                    [node, "node_modules/vite/bin/vite.js", "build"]):
        child = subprocess.Popen(command, cwd=root, env=environment)
        while child.poll() is None:
            if available_memory_bytes() < 2 * 1024**3:
                process = psutil.Process(child.pid)
                for descendant in reversed(process.children(recursive=True)):
                    try:
                        descendant.terminate()
                    except psutil.NoSuchProcess:
                        pass
                child.terminate()
                child.wait(timeout=30)
                raise RuntimeError("Source frontend validation stopped under memory pressure")
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if child.returncode:
            raise RuntimeError(f"Source frontend validation failed: {command[1]}")
    verify_source_monorepo(source_root, allow_generated=True)
    # Both compiler steps succeeded; the existing contract now binds their output.
    subprocess.run([node, "--input-type=module", "-e",
        "import {createArtifactContract} from './scripts/frontend-release-artifact.mjs';"
        "await createArtifactContract({runBuild:()=>{}}).build();"], cwd=root, check=True, timeout=30)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", choices=("package", "frontend", "web", "sidecar", "dev", "check", "native-route-check", "native-receipt-check", "native-prompt-check", "prompt-repair", "icon-repair"), default="package")
    parser.add_argument("--cockpit-budget-authorization")
    parser.add_argument("--resume", action="store_true")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parent.parent / "codex_home_manager"
    if arguments.step == "web" and (root.parent / "SOURCE_COMMITS.json").is_file():
        return run_exported_frontend(root)
    version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
    (root / ".tmp" / "typescript").mkdir(parents=True, exist_ok=True)
    prefix = "release" if arguments.step == "package" else arguments.step
    repair_steps = ("prompt-repair", "icon-repair")
    if bool(arguments.cockpit_budget_authorization) != (arguments.step in repair_steps):
        raise RuntimeError("This Cockpit allowance requires an authorized combined repair step")
    grant = lifecycle.cockpit_budget_grants.get(arguments.cockpit_budget_authorization)
    if arguments.step in repair_steps and grant is None:
        raise RuntimeError("Unknown Cockpit repair allowance")
    output = lifecycle.build_container() / (grant[1] if grant else time.strftime(f"cockpit-{version}-{prefix}-%Y%m%d-%H%M%S"))
    reservation = min(grant[2], lifecycle.gib) if grant and version == "1.0.33" else grant[2] if grant else 6 * lifecycle.gib
    admission = lifecycle.update(output, version, "begin", reservation_bytes=reservation, kind="cockpit", resume=arguments.resume,
                                 cockpit_budget_authorization=arguments.cockpit_budget_authorization)
    output.mkdir(parents=True, exist_ok=arguments.resume)
    completed = False


    def record_incomplete_run() -> None:
        if not completed and output.exists():
            try:
                lifecycle.update(output, version, "failed", reservation_bytes=reservation, kind="cockpit")
            except Exception:
                pass


    atexit.register(record_incomplete_run)
    temporary = output / "tmp"
    temporary.mkdir(exist_ok=arguments.resume)
    environment = dict(os.environ, CARGO_BUILD_JOBS="1", CMAKE_BUILD_PARALLEL_LEVEL="1",
                       CARGO_INCREMENTAL="0", CARGO_TARGET_DIR=str(lifecycle.artifact_root / "cache" / "cargo-home-manager"),
                       CARGO_PROFILE_DEV_DEBUG="0", CARGO_PROFILE_TEST_DEBUG="0",
                       NODE_OPTIONS="--max-old-space-size=1536", PYTHONDONTWRITEBYTECODE="1",
                       TEMP=str(temporary), TMP=str(temporary),
                       CODEX_HOME_MANAGER_BUILD_ROOT=str(output / "sidecar"),
                       CODEX_HOME_MANAGER_RELEASE_ROOT=str(output / "release"),
                       CODEX_COCKPIT_RESUME="1" if arguments.resume else "0",
                       CODEX_STORAGE_RUN_ROOT=str(output))
    steps = [("package", ["pwsh.exe", "-NoProfile", "-File", "scripts/package-desktop.ps1"])]
    if arguments.step == "frontend":
        steps = [("typecheck", ["node.exe", "node_modules/typescript/bin/tsc", "-b", "--pretty", "false"]),
                 ("frontend", ["node.exe", "--input-type=module", "-e", "import {build} from 'vite'; await build({build:{outDir:process.argv[1],emptyOutDir:false}});", str(output / "frontend")])]
    elif arguments.step == "web":
        steps = [("typecheck", ["node.exe", "node_modules/typescript/bin/tsc", "-b", "--pretty", "false"]),
                 ("web", ["node.exe", "--input-type=module", "-e", "import {build} from 'vite'; await build();"])]
    elif arguments.step == "sidecar":
        steps = [("sidecar", ["pwsh.exe", "-NoProfile", "-File", "scripts/prepare-desktop-sidecar.ps1"])]
    elif arguments.step == "dev":
        steps = [("dev", ["cmd.exe", "/d", "/s", "/c", "node_modules\\.bin\\tauri.cmd dev"])]
    elif arguments.step == "check":
        steps = [("check", ["cargo.exe", "check", "--locked", "--manifest-path", "src-tauri/Cargo.toml", "--workspace"])]
    native_tests = {"native-route-check": "tests::maintenance_routes_are_read_only", "native-receipt-check": "pagination_patch::tests::receipt_only_status_matches_shared_contract"}
    if arguments.step in ("native-prompt-check", "prompt-repair"):
        native_tests = {f"native-prompt-{position}": f"prompts::prompt_index::tests::{name}" for position, name in enumerate((
            "persistent_cursor_is_bound_to_generation_and_filters",
            "continued_prompt_cursor_survives_predecessor_index_growth",
            "fixed_continuation_sources_page_without_advancing_scan_and_keep_stable_context",
            "recent_browsing_reuses_warm_index_and_preserves_cold_cursor_and_latest_tail",
            "global_timeline_search_is_rust_native_and_pages_continuations",
            "legacy_zero_identity_bootstraps_without_rebuilding_and_replacement_is_detected",
            "prompt_index_lock_wait_can_be_cancelled_without_releasing_the_owner",
            "rust_index_is_readable_by_python_schema_20",
            "python_index_is_extended_without_rebuild_by_rust",
        ), start=1)}
        native_tests["native-prompt-http"] = "tests::prompt_http_default_home_uses_one_index"
        if version == "1.0.33":
            native_tests["native-classification-contract"] = "history::tests::shared_timeline_classification_contract"
            native_tests["native-classification-migration"] = "prompts::prompt_index::tests::classification_migration_preserves_progress_and_search"
    if arguments.step == "icon-repair":
        native_tests = {
            "native-icon-files": "tests::frontend_static_files_require_the_release_manifest_and_security_headers",
            "native-icon-paths": "tests::frontend_artifact_is_a_sibling_resource_directory",
            "native-classification-contract": "history::tests::shared_timeline_classification_contract",
        }
    if arguments.step in native_tests or arguments.step in ("native-prompt-check", *repair_steps):
        environment["CODEX_HOME_MANAGER_PROMPT_INDEX_ROOT"] = str(temporary / "prompt-indexes")
        selected_tests = native_tests.items() if arguments.step in ("native-prompt-check", *repair_steps) else [(arguments.step, native_tests[arguments.step])]
        steps = [(name, ["cargo.exe", "test", "--locked", "--manifest-path", "src-tauri/native-service/Cargo.toml", test, "--", "--exact", "--test-threads=1"]) for name, test in selected_tests]
        if arguments.step in repair_steps:
            steps.append(("package", ["pwsh.exe", "-NoProfile", "-File", "scripts/package-desktop.ps1"]))
    print(json.dumps({"output": str(output)}), flush=True)
    with (output / "memory.jsonl").open("a" if arguments.resume else "x") as samples:
        for name, command in steps:
            if available_memory_bytes() < 4 * 1024**3:
                raise RuntimeError("Less than 4 GiB available; build not started")
            with (output / (name + ".log")).open("a" if arguments.resume else "x", encoding="utf-8") as log:
                log.seek(0, 2)
                run_log_offset = log.tell()
                child = subprocess.Popen(command, cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                process = psutil.Process(child.pid)
                created = process.create_time()
                last_report = 0.0
                while child.poll() is None:
                    available = available_memory_bytes()
                    sample = dict(step=name, timestamp=time.time(), pid=child.pid, availableGiB=round(available / 1024**3, 3))
                    samples.write(json.dumps(sample) + "\n")
                    samples.flush()
                    if time.monotonic() - last_report >= 30 or available < 3 * 1024**3:
                        print(json.dumps(sample), flush=True)
                        last_report = time.monotonic()
                    if available < 2 * 1024**3:
                        if process.is_running() and process.create_time() == created:
                            descendants = process.children(recursive=True)
                            for descendant in reversed(descendants):
                                try:
                                    descendant.terminate()
                                except psutil.NoSuchProcess:
                                    pass
                            process.terminate()
                        child.wait(timeout=30)
                        raise RuntimeError("Build cancelled under memory pressure; unrelated processes untouched")
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                if child.returncode:
                    raise RuntimeError(f"{name} failed with code {child.returncode}; see {output / (name + '.log')}")
            if name in native_tests:
                with (output / (name + ".log")).open("rb") as log:
                    log.seek(max(run_log_offset, log.seek(0, 2) - 32768))
                    tail = log.read().decode("utf-8", errors="replace")
                # A child harness can print between this exact test's name and its final ok.
                if f"test {native_tests[name]} ... " not in tail or "test result: ok. 1 passed; 0 failed;" not in tail:
                    raise RuntimeError("Expected native test did not run successfully")
    if arguments.step == "frontend" and not (output / "frontend" / "index.html").is_file():
        raise RuntimeError("Frontend build did not produce its entry point")
    if arguments.step == "web" and not (root / "dist" / "index.html").is_file():
        raise RuntimeError("Web build did not produce dist/index.html")
    final_snapshot = lifecycle.storage_snapshot()
    if arguments.cockpit_budget_authorization:
        lifecycle.assert_cockpit_budget(final_snapshot, admission)
    elif final_snapshot["violations"]:
        raise RuntimeError("Build crossed the storage budget: " + "; ".join(final_snapshot["violations"]))
    retained_files = [output / "memory.jsonl", *(output / (name + ".log") for name, _ in steps)]
    if arguments.step in ("package", *repair_steps):
        retained_files.extend(sorted((output / "release").glob("*")))
    elif arguments.step == "frontend":
        retained_files.append(output / "frontend" / "index.html")
    retained_files = [path for path in retained_files if path.is_file()]
    result_path = output / "artifact-lifecycle-result.json"
    retained_records = []
    for path in retained_files:
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                checksum.update(block)
        retained_records.append({"path": str(path), "bytes": path.stat().st_size, "sha256": checksum.hexdigest()})
    result_path.write_text(json.dumps({
        "passed": True,
        "retainedFiles": [record["path"] for record in retained_records],
        "retainedFileRecords": retained_records,
    }, indent=2), encoding="utf-8")
    lifecycle.update(output, version, "verified", reservation_bytes=reservation, kind="cockpit", report_path=result_path)
    completed = True
    print(json.dumps({"status": "built", "output": str(output)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
