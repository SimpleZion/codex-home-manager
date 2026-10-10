from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import maintenance


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    catalog = {"schemaVersion": 1, "updatedAt": "2026-09-06T00:00:00Z", "entries": [{"id": "known", "evidence": [{"path": "proof.md", "label": "Proof"}]}]}
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    (tmp_path / "proof.md").write_text("Private text must not be returned", encoding="utf-8")
    monkeypatch.setattr(maintenance, "manifest_path", catalog_path)
    monkeypatch.setattr(maintenance, "evidence_root", lambda source_workspace=None: tmp_path)
    monkeypatch.setenv("CODEX_PAGINATION_READY_STATE", str(tmp_path / "ready.json"))
    monkeypatch.setenv("CODEX_PAGINATION_INSTALLED_STATE", str(tmp_path / "installed.json"))
    app = FastAPI()
    app.include_router(maintenance.router)
    return TestClient(app)


def test_status_separates_receipt_from_runtime(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "installed.json").write_text(json.dumps({"status": "success", "runtimeStatus": "passed", "secret": "do-not-return"}), encoding="utf-8")
    response = client.get("/api/maintenance/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["runtimeVerified"] is False
    assert payload["entries"][0]["evidence"][0]["availability"] == "present"
    assert "do-not-return" not in response.text
    assert payload["receipts"][1]["values"]["runtimeStatus"] == "passed"


def test_fingerprint_does_not_return_file_content(client: TestClient) -> None:
    response = client.get("/api/maintenance/evidence/known/0")
    assert response.status_code == 200
    assert len(response.json()["sha256"]) == 64
    assert "Private text" not in response.text
    assert "content" not in response.json()


def test_unknown_evidence_cannot_read_arbitrary_paths(client: TestClient) -> None:
    assert client.get("/api/maintenance/evidence/unknown/0").status_code == 404
    assert client.get("/api/maintenance/evidence/known/-1").status_code == 404
    assert client.get("/api/maintenance/evidence/known/99").status_code == 404
    assert client.post("/api/maintenance/status").status_code == 405


@pytest.mark.parametrize("path", ["../outside.md", "C:/secret.md", "D:\\secret.md", "a/../../outside.md"])
def test_evidence_scope_is_fail_closed(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        maintenance.resolve_evidence(path, tmp_path)


def test_large_evidence_has_no_partial_hash(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "proof.md").write_bytes(b"a" * (maintenance.maximum_evidence_bytes + 1))
    payload = client.get("/api/maintenance/evidence/known/0").json()
    assert payload["sha256"] is None
    assert payload["hashStatus"] == "size_limit"


def test_packaged_evidence_is_unavailable_not_missing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(maintenance, "evidence_root", lambda source_workspace=None: None)
    payload = client.get("/api/maintenance/status").json()
    assert payload["sourceWorkspaceAvailable"] is False
    assert payload["entries"][0]["evidence"][0]["availability"] == "unavailable"
    assert client.get("/api/maintenance/evidence/known/0").status_code == 409


def test_malformed_receipt_is_not_success(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "ready.json").write_text("broken", encoding="utf-8")
    assert client.get("/api/maintenance/status").json()["receipts"][0]["availability"] == "invalid"


def test_shipped_catalog_is_complete_and_unique() -> None:
    catalog = maintenance.load_catalog()
    ids = [entry["id"] for entry in catalog["entries"]]
    assert len(ids) == len(set(ids)) == 21
    assert catalog["entries"][0]["id"] == "standalone-renderer-production"
    assert {"project-document-loading-limit", "browser-kernel-asset-startup"}.issubset(ids)
    assert set(ids).isdisjoint({"manager-packaging-security", "manager-search-timeline", "maintenance-evidence-model", "cockpit-maintenance-integration"})
    assert all(entry["category"] not in {"Cockpit源码能力", "管理器"} for entry in catalog["entries"])
    shared_gates = [item for gate in catalog["safetyGates"] for item in gate["items"] if "共享" in item]
    assert any("阻断常规写入" in item and "已" in item and "writer guard" in item and "不豁免" in item for item in shared_gates)
    assert {"desktop-pagination-6511", "legacy-pagination-packages", "browser-tab-restoration", "browser-memory-tracker", "memory-observation", "cli-prerelease-trial", "shield-withdrawal", "restart-supervision", "ordinal-history-recovery", "legacy-history-migration", "checkpoint-context-management", "official-tools-history", "task-project-state", "provider-interop", "plugin-native-hosts", "sandbox-acl", "token-budget-withdrawal", "agents-governance"}.issubset(ids)
    for entry in catalog["entries"]:
        assert entry["status"] in {"prepared", "historical", "withdrawn", "unresolved", "integrated"}
        for field in ("title", "summary", "reason", "targets", "changes", "verification", "risks", "preconditions", "rollback", "evidence", "nextAction"):
            assert entry[field], (entry["id"], field)
        for record in entry["evidence"]:
            maintenance.resolve_evidence(record["path"], Path(__file__).resolve().parents[2])


def test_prepared_package_is_bound_to_receipt_hash(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib
    monkeypatch.setattr(maintenance, "default_artifact_root", tmp_path)
    package = tmp_path / "package.msix"
    package.write_bytes(b"test package")
    expected = hashlib.sha256(package.read_bytes()).hexdigest()
    receipt = tmp_path / "ready.json"
    receipt.write_text(json.dumps({"packagePath": str(package), "packageSha256": expected}), encoding="utf-8")
    response = client.get("/api/maintenance/artifact/ready")
    assert response.status_code == 200
    assert response.json()["hashMatches"] is True
    assert response.json()["runtimeVerified"] is False
    package.write_bytes(b"changed package")
    assert client.get("/api/maintenance/artifact/ready").json()["hashMatches"] is False


def test_artifact_scope_and_type_are_checked(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(maintenance, "default_artifact_root", tmp_path / "managed")
    (tmp_path / "ready.json").write_text(json.dumps({"packagePath": str(tmp_path / "private.txt"), "packageSha256": "a" * 64}), encoding="utf-8")
    assert client.get("/api/maintenance/artifact/ready").status_code == 409


def test_frozen_build_can_bind_only_matching_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source"
    catalog_path = root / "codex_home_manager/packaging/runtime/cockpit-maintenance.json"
    catalog_path.parent.mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "AGENTS.md").write_text("scope", encoding="utf-8")
    catalog_path.write_text('{"schemaVersion":1}', encoding="utf-8")
    installed_catalog = tmp_path / "installed-catalog.json"
    installed_catalog.write_bytes(catalog_path.read_bytes())
    monkeypatch.setattr(maintenance, "manifest_path", installed_catalog)
    monkeypatch.setattr(maintenance.sys, "frozen", True, raising=False)
    assert maintenance.evidence_root() is None
    assert maintenance.evidence_root(str(root)) == root
    catalog_path.write_text('{"schemaVersion":2}', encoding="utf-8")
    with pytest.raises(maintenance.HTTPException) as failure:
        maintenance.evidence_root(str(root))
    assert failure.value.status_code == 409


def test_evidence_expected_catalog_and_response_identity(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = maintenance.load_catalog()
    calls = []
    monkeypatch.setattr(maintenance, "load_catalog", lambda: calls.append(1) or catalog)
    response = client.get("/api/maintenance/evidence/known/0", params={"catalog_sha256": catalog["contentSha256"]})
    assert response.status_code == 200 and calls == [1]
    assert {key: response.json()[key] for key in ("catalogSha256", "entryId", "evidenceIndex", "path")} == {
        "catalogSha256": catalog["contentSha256"], "entryId": "known", "evidenceIndex": 0, "path": "proof.md"}
    monkeypatch.setattr(maintenance, "resolve_evidence", lambda *args: pytest.fail("stale catalog must not open evidence"))
    assert client.get("/api/maintenance/evidence/known/0", params={"catalog_sha256": "0" * 64}).status_code == 409


def test_ready_expected_identity_rejects_changed_receipt(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib
    monkeypatch.setattr(maintenance, "default_artifact_root", tmp_path)
    package = tmp_path / "new.msix"
    package.write_bytes(b"new")
    digest = hashlib.sha256(b"new").hexdigest()
    (tmp_path / "ready.json").write_text(json.dumps({"packagePath": str(package), "packageSha256": digest}), encoding="utf-8")
    for expected in ({"expected_path": str(tmp_path / "old.msix")}, {"expected_sha256": "0" * 64}, {"expected_sha256": "invalid"}):
        assert client.get("/api/maintenance/artifact/ready", params=expected).status_code == 409
    response = client.get("/api/maintenance/artifact/ready", params={"expected_path": str(package), "expected_sha256": digest})
    assert response.status_code == 200 and response.json()["hashMatches"] is True
    assert response.json()["path"] == str(package.resolve())


@pytest.mark.parametrize("suffix", [".exe", ".cmd", ".ps1", ".sh"])
def test_cli_metadata_nonexecuting_and_legacy_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    from backend import codex_data
    from backend.probe_budget import ProbeBudget
    executable = tmp_path / ("untrusted" + suffix)
    executable.write_text("arbitrary program", encoding="utf-8")
    calls = []
    monkeypatch.setattr(codex_data, "run_hidden_command", lambda command, **kwargs: calls.append(command) or {"stdout": "codex-cli 1.2.3"})
    safe = codex_data.inspect_codex_cli_metadata(str(executable), allow_execution=False, probe_budget=ProbeBudget())
    assert calls == [] and safe["version"] == "" and "not executed" in safe["error"]
    authorized = codex_data.inspect_codex_cli_metadata(str(executable))
    assert authorized["version"] == "1.2.3" and calls == [[str(executable), "--version"]]


def test_runtime_get_never_executes_home_program(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import codex_data, browser_rollout_patch_status, pagination_patch_status
    from backend.provider_interop import runtime
    executable = tmp_path / "payload.exe"
    executable.write_bytes(b"untrusted")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "session_index.jsonl").write_text("", encoding="utf-8")
    for name in ("config.toml", "managed_config.toml"):
        (tmp_path / name).write_text('CODEX_CLI_PATH = ' + json.dumps(executable.as_posix()), encoding="utf-8")
    monkeypatch.setattr(codex_data, "run_hidden_command", lambda *args, **kwargs: pytest.fail("GET executed a Home command"))
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: pytest.fail("GET started a process"))
    monkeypatch.setattr(codex_data.shutil, "which", lambda *args: None)
    monkeypatch.setattr(codex_data, "find_codex_desktop_installations", lambda **kwargs: [])
    monkeypatch.setattr(browser_rollout_patch_status, "detect_browser_rollout_patch_status", lambda **kwargs: {"state": "unknown", "reasonZh": "fixture"})
    pagination_fields = {"receiptObservedAt": "2000-01-01T00:00:00Z", "evidenceScope": "receipt_only", "currentRuntimeVerified": False, "checkedAt": "2026-09-07T00:00:00Z"}
    monkeypatch.setattr(pagination_patch_status, "detect_pagination_patch_status", lambda *args: {"state": "unverified", "reason": "fixture", "policy": {"requiredFeatureContractVersion": 7}, **pagination_fields})
    monkeypatch.setattr(runtime, "resolve_official_resources", lambda explicit, **kwargs: None)
    monkeypatch.setattr(maintenance, "_read_first_state", lambda *args: (None, "", ""))
    response = client.get("/api/maintenance/runtime", params={"codex_home": str(tmp_path)})
    assert response.status_code == 200
    configured = response.json()["checks"][0]["details"]["configuredCli"]
    assert configured["path"] == executable.as_posix() and configured["exists"] is True and configured["version"] == ""
    assert response.json()["runtimeVerified"] is False
    details = response.json()["checks"][0]["details"]
    assert all(details[key] == value for key, value in pagination_fields.items())
    assert "checkedAtMs" not in details


def test_nonexecuting_provider_discovery_does_not_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.provider_interop import runtime
    monkeypatch.delenv("CODEX_OFFICIAL_RESOURCES_PATH", raising=False)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: pytest.fail("discovery executed"))
    assert runtime.resolve_official_resources(None, allow_execution=False) is None


def test_probe_hash_limits_cancel_and_default_hash(tmp_path: Path) -> None:
    import hashlib
    from backend.probe_budget import ProbeBudget, ProbeStopped
    from backend.provider_interop.runtime import sha256_file
    path = tmp_path / "runtime.exe"
    path.write_bytes(b"payload")
    expected = hashlib.sha256(b"payload").hexdigest().upper()
    assert sha256_file(path) == expected
    budget = ProbeBudget(maximum_bytes=10)
    assert sha256_file(path, probe_budget=budget) == expected
    with pytest.raises(ProbeStopped):
        sha256_file(path, probe_budget=budget)
    assert budget.cancelled.is_set()
    with pytest.raises(ProbeStopped):
        sha256_file(path, probe_budget=ProbeBudget(seconds=0))
    budget = ProbeBudget()
    budget.cancelled.set()
    with pytest.raises(ProbeStopped):
        sha256_file(path, probe_budget=budget)


def test_probe_hash_checks_deadline_between_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import probe_budget
    path = tmp_path / "runtime.exe"
    path.write_bytes(b"x" * 200000)
    clock = [0.0]
    monkeypatch.setattr(probe_budget.time, "monotonic", lambda: clock[0])
    budget = probe_budget.ProbeBudget(seconds=1)
    original_open = Path.open

    class SlowReader:
        def __enter__(self):
            self.stream = original_open(path, "rb")
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size):
            value = self.stream.read(size)
            clock[0] = 2
            return value
        def fileno(self):
            return self.stream.fileno()

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: SlowReader())
    with pytest.raises(probe_budget.ProbeStopped):
        budget.read(path, digest=True)
    assert budget.read_bytes == 65536


def test_provider_large_digest_streams_without_aggregation(monkeypatch: pytest.MonkeyPatch) -> None:
    import stat
    from types import SimpleNamespace
    from backend import probe_budget
    from backend.provider_interop.runtime import sha256_file
    file_size = 295408944
    metadata = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=1, st_size=file_size, st_mtime_ns=1)
    chunk = b"x" * 65536
    counts = {"read": 0, "hashed": 0, "calls": 0}

    class Stream:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            assert 0 < size <= 65536
            length = min(size, file_size - counts["read"])
            counts["read"] += length
            counts["calls"] += 1
            return chunk if length == len(chunk) else chunk[:length]
        def fileno(self):
            return 123

    class Digest:
        def update(self, value):
            assert len(value) <= 65536
            counts["hashed"] += len(value)
        def hexdigest(self):
            assert counts["hashed"] == file_size
            return "ab" * 32

    def reject_aggregation(*args, **kwargs):
        pytest.fail("digest must not allocate an aggregate byte buffer")

    path = SimpleNamespace(stat=lambda: metadata, open=lambda mode: Stream())
    monkeypatch.setattr(probe_budget.os, "fstat", lambda descriptor: metadata)
    monkeypatch.setattr(probe_budget.hashlib, "sha256", Digest)
    monkeypatch.setattr(probe_budget, "bytearray", reject_aggregation, raising=False)
    monkeypatch.setattr(probe_budget.time, "monotonic", lambda: 0)
    budget = probe_budget.ProbeBudget()
    assert budget.maximum_bytes == 1024 * 1024 * 1024 and budget.deadline == 5
    assert sha256_file(path, probe_budget=budget) == "AB" * 32
    assert budget.read_bytes == counts["hashed"] == file_size
    assert counts["calls"] == (file_size + 65535) // 65536 + 1
    metadata.st_size = 512 * 1024 * 1024 + 1
    with pytest.raises(probe_budget.ProbeStopped):
        sha256_file(path, probe_budget=probe_budget.ProbeBudget())


@pytest.mark.parametrize("limit", [1024 * 1024, 65536])
def test_text_limit_remains_independent_of_digest_budget(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    import stat
    from types import SimpleNamespace
    from backend.probe_budget import ProbeBudget, ProbeStopped, probe_text
    path = SimpleNamespace(stat=lambda: SimpleNamespace(st_mode=stat.S_IFREG, st_size=limit + 1),
                           open=lambda *args: pytest.fail("oversized text must not be opened"))
    with pytest.raises(ProbeStopped):
        probe_text(path, ProbeBudget(), **({"maximum_bytes": limit} if limit == 65536 else {}))


@pytest.mark.parametrize("budget_stopped", [True, False])
def test_pending_budget_stop_is_unknown_not_invalid(monkeypatch: pytest.MonkeyPatch, budget_stopped: bool) -> None:
    from types import SimpleNamespace
    from backend import codex_data, browser_rollout_patch_status, pagination_patch_status
    from backend.provider_interop import runtime
    from backend.probe_budget import ProbeBudget
    monkeypatch.setattr(codex_data, "resolve_codex_paths", lambda *args: SimpleNamespace(codex_home_path=Path("unused-home")))
    monkeypatch.setattr(codex_data, "detect_current_codex_versions", lambda *args, **kwargs: {})
    monkeypatch.setattr(pagination_patch_status, "detect_pagination_patch_status", lambda *args: {
        "state": "unverified", "reason": "receipt only", "policy": {"requiredFeatureContractVersion": 7}})
    monkeypatch.setattr(browser_rollout_patch_status, "detect_browser_rollout_patch_status", lambda **kwargs: {"state": "unverified", "reasonZh": "fixture"})
    monkeypatch.setattr(runtime, "inspect_runtime_compatibility", lambda **kwargs: {"classification": "unverified", "recommendedAction": "fixture"})

    def read_pending(*args):
        assert not budget_stopped, "pending read must not start without its budget"
        return None, "", "invalid_json"

    monkeypatch.setattr(maintenance, "_read_first_state", read_pending)
    result = maintenance.inspect_runtime_status(None, ProbeBudget(maximum_bytes=(4 if budget_stopped else 5) * 1024 * 1024))
    pending = next(check for check in result["checks"] if check["id"] == "ordinal-history-recovery")
    assert result["probeStatus"] == "partial"
    assert pending["state"] == ("unknown" if budget_stopped else "invalid")
    if budget_stopped:
        assert "预算不足" in pending["reason"] and "不判定锁损坏" in pending["reason"]


@pytest.mark.parametrize("disconnect", [False, True])
def test_runtime_timeout_or_disconnect_retains_slot_and_skips_later_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disconnect: bool) -> None:
    import asyncio
    import threading
    from fastapi import Response
    from backend import codex_data, browser_rollout_patch_status, pagination_patch_status
    from backend.provider_interop import runtime
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "session_index.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(maintenance, "runtime_probe_seconds", 0.05)

    def slow_versions(*args, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(2)
        return {}

    monkeypatch.setattr(codex_data, "detect_current_codex_versions", slow_versions)
    monkeypatch.setattr(browser_rollout_patch_status, "detect_browser_rollout_patch_status", lambda **kwargs: pytest.fail("started browser after cancellation"))
    monkeypatch.setattr(pagination_patch_status, "detect_pagination_patch_status", lambda *args: pytest.fail("started pagination after cancellation"))
    monkeypatch.setattr(runtime, "inspect_runtime_compatibility", lambda **kwargs: pytest.fail("started provider after cancellation"))
    original = maintenance.inspect_runtime_status

    def inspect(*args):
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(maintenance, "inspect_runtime_status", inspect)

    class Request:
        async def is_disconnected(self):
            return disconnect and entered.is_set()

    async def exercise():
        try:
            result = await maintenance.runtime_status(Request(), Response(), str(tmp_path))
            assert result["probeStatus"] == ("cancelled" if disconnect else "partial_deadline")
            assert maintenance.runtime_probe_lock.locked()
            same = await maintenance.runtime_status(Request(), Response(), str(tmp_path))
            other = await maintenance.runtime_status(Request(), Response(), str(tmp_path / "other-home"))
            assert same["probeStatus"] == "in_progress" and other["probeStatus"] == "busy"
            assert len(calls) == 1 and calls[0]["allow_execution"] is False
        finally:
            release.set()
        for _ in range(100):
            if not maintenance.runtime_probe_lock.locked():
                break
            await asyncio.sleep(0.01)
        assert finished.is_set() and not maintenance.runtime_probe_lock.locked()

    asyncio.run(exercise())


def test_oversized_config_stops_all_following_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import browser_rollout_patch_status, pagination_patch_status
    from backend.provider_interop import runtime
    from backend.probe_budget import ProbeBudget
    (tmp_path / "config.toml").write_bytes(b" " * (1024 * 1024 + 1))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "session_index.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(browser_rollout_patch_status, "detect_browser_rollout_patch_status", lambda **kwargs: pytest.fail("browser started"))
    monkeypatch.setattr(pagination_patch_status, "detect_pagination_patch_status", lambda *args: pytest.fail("pagination started"))
    monkeypatch.setattr(runtime, "inspect_runtime_compatibility", lambda **kwargs: pytest.fail("provider started"))
    monkeypatch.setattr(maintenance, "_read_first_state", lambda *args: pytest.fail("pending started"))
    result = maintenance.inspect_runtime_status(str(tmp_path), ProbeBudget())
    assert result["probeStatus"] == "partial"
    assert all(check["state"] == "unknown" for check in result["checks"])
