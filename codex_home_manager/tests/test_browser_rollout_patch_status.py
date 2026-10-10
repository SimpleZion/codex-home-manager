from __future__ import annotations

import hashlib
import ast
import importlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


@pytest.fixture
def scenario(tmp_path):
    root = tmp_path / "local" / "OpenAI" / "Codex" / "runtimes" / "cua_node"
    runtime = root / "version-one"
    modules = runtime / "bin" / "node_modules"
    service = modules / "@oai" / "browser-desktop" / "scripts" / "browser-service.mjs"
    service.parent.mkdir(parents=True)
    service.write_bytes(b"patched service")
    helper = service.with_name("reader.cjs")
    helper.write_bytes(b"bounded reader")
    now = time.time()
    manifest = {
        "sourcePath": str(service),
        "sourceHash": "3032666de451e423db76b5f9b0456e370d4f57416bbf53d0c746be636359ba02",
        "patchedHash": hashlib.sha256(service.read_bytes()).hexdigest(),
        "helperName": helper.name,
        "helperHash": hashlib.sha256(helper.read_bytes()).hexdigest(),
    }
    receipt = dict(manifest, installedAt=timestamp(now - 2000), runtimeStatus="verified",
                   runtimeValidatedAt=timestamp(now), workerPid=123, workerCreatedAt=timestamp(now - 1800),
                   observationSeconds=900)
    worker = {"pid": 123, "createdAt": now - 1800, "exe": str(runtime / "bin" / "node.exe"),
              "cmdline": ["node.exe", str(tmp_path / "trusted-worker.js")],
              "environment": {"NODE_REPL_NODE_MODULE_DIRS": str(modules),
                              "NODE_REPL_TRUSTED_SERVICES": '{"browser":"@oai/browser-desktop/service"}'}}
    manifest_path, receipt_path = tmp_path / "manifest.json", tmp_path / "installed.json"
    snapshot = {"workers": [worker], "complete": True}

    def detect(**overrides):
        module = importlib.import_module("backend.browser_rollout_patch_status")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        arguments = dict(environment={"LOCALAPPDATA": str(tmp_path / "local")}, manifest_path=manifest_path,
                         receipt_path=receipt_path, process_probe=lambda: snapshot, now=now, cache_ttl=0)
        arguments.update(overrides)
        return module.detect_browser_rollout_patch_status(**arguments)

    return locals()


def test_live_verified_receipt(scenario):
    result = scenario["detect"]()
    assert result["state"] == "verified", result


@pytest.mark.parametrize("field,value", [("runtimeStatus", "pending"), ("observationSeconds", 899),
                                        ("runtimeValidatedAt", "bad"), ("observationSeconds", float("inf")),
                                        ("installedAt", None)])
def test_incomplete_runtime_acceptance(scenario, field, value):
    scenario["receipt"][field] = value
    assert scenario["detect"]()["state"] == "installed_pending_runtime"


@pytest.mark.parametrize("change", ["pid_reused", "pid_missing", "other_runtime", "exe_mismatch"])
def test_old_receipt_never_verifies(scenario, change):
    if change == "pid_reused":
        scenario["worker"]["createdAt"] += 30
    elif change == "pid_missing":
        scenario["worker"]["pid"] += 1
    elif change == "other_runtime":
        scenario["worker"]["environment"]["NODE_REPL_NODE_MODULE_DIRS"] = str(scenario["root"] / "new" / "bin" / "node_modules")
    else:
        scenario["worker"]["exe"] = str(scenario["root"] / "other" / "bin" / "node.exe")
    assert scenario["detect"]()["state"] != "verified"


def test_new_worker_with_matching_files_is_pending(scenario):
    scenario["worker"]["pid"] += 1
    result = scenario["detect"]()
    assert result["state"] == "installed_pending_runtime"


@pytest.mark.parametrize("change", ["no_worker", "env_denied", "incomplete_probe", "unrelated_process"])
def test_unconfirmed_runtime_is_unknown(scenario, change):
    if change == "no_worker":
        scenario["snapshot"]["workers"] = []
    elif change == "env_denied":
        scenario["worker"]["environment"] = {}
    elif change == "incomplete_probe":
        scenario["snapshot"]["complete"] = False
    else:
        scenario["worker"]["cmdline"] = ["node.exe", "server.js"]
    assert scenario["detect"]()["state"] == "unknown"


@pytest.mark.parametrize("change", ["service", "helper", "receipt_hash", "manifest_hash", "traversal"])
def test_tampered_files_and_evidence(scenario, change):
    if change == "service":
        scenario["service"].write_bytes(b"unrecognized version")
    elif change == "helper":
        scenario["helper"].write_bytes(b"changed helper")
    elif change == "traversal":
        scenario["receipt"]["helperName"] = "../outside.cjs"
    else:
        scenario["receipt" if change == "receipt_hash" else "manifest"]["patchedHash"] = "0" * 64
    assert scenario["detect"]()["state"] == "stale"


def test_known_bad_hash_has_priority(scenario, monkeypatch):
    module = importlib.import_module("backend.browser_rollout_patch_status")
    monkeypatch.setattr(module, "known_bad_sha256", scenario["manifest"]["patchedHash"])
    assert scenario["detect"]()["state"] == "vulnerable"


def test_prepared_files_are_not_installation(scenario):
    assert scenario["detect"](receipt_path=scenario["root"] / "missing.json")["state"] == "unknown"


def test_default_manifest_follows_current_receipt(scenario, monkeypatch):
    module = importlib.import_module("backend.browser_rollout_patch_status")
    artifact = scenario["tmp_path"] / "artifacts"
    candidate = artifact / "candidate-current"
    candidate.mkdir(parents=True)
    manifest_path = candidate / "manifest.json"
    manifest_path.write_text(json.dumps(scenario["manifest"]), encoding="utf-8")
    receipt_path = artifact / "installed.json"
    receipt_path.write_text(json.dumps(scenario["receipt"]), encoding="utf-8")
    monkeypatch.setattr(module, "artifact_root", artifact)
    result = module.detect_browser_rollout_patch_status(
        environment={"LOCALAPPDATA": str(scenario["tmp_path"] / "local")},
        receipt_path=receipt_path,
        process_probe=lambda: scenario["snapshot"],
        now=scenario["now"],
        cache_ttl=0,
    )
    assert result["state"] == "verified"
    assert Path(result["manifestPath"]).resolve() == manifest_path.resolve()


def test_unknown_new_version_is_not_officially_fixed(scenario):
    assert scenario["detect"](manifest_path=scenario["root"] / "missing.json",
                              receipt_path=scenario["root"] / "missing.json")["state"] == "unknown"


def test_missing_runtime(scenario, tmp_path):
    scenario["snapshot"]["workers"] = []
    assert scenario["detect"](environment={"LOCALAPPDATA": str(tmp_path / "absent")})["state"] == "not_found"


def test_missing_environment_never_uses_current_directory(scenario):
    assert scenario["detect"](environment={})["state"] == "unknown"


def test_probe_failure_is_unknown(scenario):
    def denied():
        raise PermissionError("denied")
    assert scenario["detect"](process_probe=denied)["state"] == "unknown"


@pytest.mark.parametrize("field,value", [("runtimeValidatedAt", 60), ("workerCreatedAt", 100), ("installedAt", 100)])
def test_future_or_inconsistent_times_do_not_verify(scenario, field, value):
    scenario["receipt"][field] = timestamp(scenario["now"] + value)
    assert scenario["detect"]()["state"] != "verified"


def test_multiple_workers_require_all_to_be_validated(scenario):
    scenario["snapshot"]["workers"].append(dict(scenario["worker"], pid=456))
    assert scenario["detect"]()["state"] == "installed_pending_runtime"


def test_cached_hashes_do_not_cache_worker_identity(scenario):
    assert scenario["detect"](cache_ttl=30)["state"] == "verified"
    scenario["worker"]["createdAt"] += 60
    assert scenario["detect"](cache_ttl=30)["state"] == "installed_pending_runtime"


def test_changed_file_invalidates_cache(scenario):
    assert scenario["detect"](cache_ttl=30)["state"] == "verified"
    scenario["helper"].write_bytes(b"different size")
    assert scenario["detect"](cache_ttl=30)["state"] == "stale"


def test_worker_exits_during_file_inspection(scenario):
    calls = 0

    def probe():
        nonlocal calls
        calls += 1
        return scenario["snapshot"] if calls == 1 else {"workers": [], "complete": True}

    assert scenario["detect"](process_probe=probe)["state"] != "verified"


def test_file_changes_during_final_process_probe(scenario):
    calls = 0

    def probe():
        nonlocal calls
        calls += 1
        if calls == 2:
            scenario["helper"].write_bytes(b"modified during final check")
        return scenario["snapshot"]

    assert scenario["detect"](process_probe=probe)["state"] != "verified"


def test_process_probe_reads_only_relevant_environment(monkeypatch):
    import psutil
    module = importlib.import_module("backend.browser_rollout_patch_status")

    class Process:
        info = {"name": "node.exe"}
        pid = 123

        def cmdline(self):
            return ["node.exe", "trusted-worker.js", "private argument"]

        def environ(self):
            return {"SECRET": "must not escape", "NODE_REPL_NODE_MODULE_DIRS": "modules",
                    "NODE_REPL_TRUSTED_SERVICES": '{"browser":"@oai/browser-desktop/service"}'}

        def create_time(self):
            return 100

        def exe(self):
            return "node.exe"

    monkeypatch.setattr(psutil, "process_iter", lambda fields: iter([Process()]))
    result = module._probe_workers()
    assert result["complete"] is True
    assert len(result["workers"]) == 1
    assert "SECRET" not in result["workers"][0]["environment"]
    assert result["workers"][0]["cmdline"] == ["trusted-worker.js"]


def test_environment_access_denied_is_not_healthy(monkeypatch):
    import psutil
    module = importlib.import_module("backend.browser_rollout_patch_status")

    class Process:
        info = {"name": "node.exe"}

        def cmdline(self):
            return ["trusted-worker.js"]

        def environ(self):
            raise psutil.AccessDenied(123)

    monkeypatch.setattr(psutil, "process_iter", lambda fields: iter([Process()]))
    assert module._probe_workers() == {"workers": [], "complete": False}


@pytest.mark.parametrize("payload", ["{broken", "[]", " " * 65537], ids=["malformed", "array", "oversized"])
def test_invalid_or_oversized_receipt(scenario, tmp_path, payload):
    path = tmp_path / "invalid.json"
    path.write_text(payload, encoding="utf-8")
    assert scenario["detect"](receipt_path=path)["state"] == "unknown"


def test_oversized_service_is_not_read(scenario, monkeypatch):
    module = importlib.import_module("backend.browser_rollout_patch_status")
    monkeypatch.setattr(module, "maximum_file_bytes", 1)
    assert scenario["detect"]()["state"] == "unknown"


def test_scan_limit_is_fail_closed(scenario, monkeypatch):
    module = importlib.import_module("backend.browser_rollout_patch_status")
    monkeypatch.setattr(module, "maximum_versions", 0)
    scenario["snapshot"]["workers"] = []
    assert scenario["detect"]()["state"] == "unknown"


def test_no_rollout_reads_or_runtime_writes(scenario, monkeypatch):
    scenario["detect"]()
    module = importlib.import_module("backend.browser_rollout_patch_status")
    allowed = {scenario[key].resolve() for key in ("manifest_path", "receipt_path", "service", "helper")}
    original = Path.open

    def guarded(path, mode="r", *args, **kwargs):
        assert path.resolve() in allowed
        assert mode == "rb"
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    result = module.detect_browser_rollout_patch_status(
        environment={"LOCALAPPDATA": str(scenario["tmp_path"] / "local")},
        manifest_path=scenario["manifest_path"], receipt_path=scenario["receipt_path"],
        process_probe=lambda: scenario["snapshot"], now=scenario["now"], cache_ttl=0)
    assert result["state"] == "verified"


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("state,status", [("vulnerable", "warning"), ("installed_pending_runtime", "warning"),
                                         ("verified", "pass"), ("stale", "warning"),
                                         ("unknown", "warning"), ("not_found", "info")])
def test_diagnostics_existing_ui_contract(language, state, status):
    from backend import diagnostics

    # Execute the actual inserted block without invoking unrelated machine-wide diagnostics.
    tree = ast.parse(Path(diagnostics.__file__).read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_codex_diagnostics")
    start = next(index for index, node in enumerate(function.body)
                 if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "browser_patch" for target in node.targets))
    end = next(index for index in range(start, len(function.body))
               if isinstance(function.body[index], ast.Assign) and any(
                   isinstance(target, ast.Name) and target.id == "required_files" for target in function.body[index].targets))
    checks, issues = [], []
    namespace = {"language": language, "localize": diagnostics.localize,
                 "desktop_memory_evidence": lambda: ["scan_complete=false"],
                 "add_check": lambda *args: checks.append(args), "add_issue": lambda *args: issues.append(args),
                 "detect_browser_rollout_patch_status": lambda: {"state": state, "reason": "English reason.",
                                                                 "reasonZh": "中文原因。", "current": [], "workers": []}}
    exec(compile(ast.Module(body=function.body[start:end], type_ignores=[]), diagnostics.__file__, "exec"), namespace)
    assert checks[0][0] == "desktop.browser_rollout_patch"
    assert checks[0][3] == status
    assert checks[0][4] == ("English reason." if language == "en" else "中文原因。")
    assert bool(issues) == (state not in {"verified", "not_found"})
    assert f"state={state}" in checks[0][5]
