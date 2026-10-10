# Codex Provider Interoperability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a Rust-accelerated Provider Interoperability module that safely unifies Codex history and shared plugins across OpenAI Official and supported third-party providers while keeping routing, credentials, authentication, and billing isolated.

**Architecture:** Extend the existing `codex-home-manager-service` binary with a reusable Rust interoperability core and a private JSON-over-stdio worker mode. Keep authorization, state-bound previews, REST/OpenAPI, MCP, and product orchestration in the existing Python control plane; expose the workflow through a focused React module and a bundled Codex skill.

**Tech Stack:** Rust 1.85, Axum 0.8, Tokio 1.53, rusqlite 0.38, serde/serde_json, sha2, memchr, memmap2, toml_edit, Python 3.12, FastAPI/Pydantic, React 19, TypeScript 5.9, Playwright, pytest.

## Global Constraints

- Preserve `model_provider = "custom"` as history identity while deriving route and quota identity from the effective provider configuration.
- Never delete, decrypt, re-encrypt, or rewrite reasoning and `encrypted_content` during normal interoperability repair.
- Preserve user prompt count, order, and length-prefixed SHA-256 exactly.
- Use equal-length byte replacement only for the first supported `openai` to `custom` JSONL migration.
- Refuse writes for unknown CC Switch schema fingerprints; inspection remains read-only.
- Keep provider tokens, `base_url`, `requires_openai_auth`, model selection, project lists, and auth cache out of common configuration.
- Keep Desktop-generated `node_repl`, native-pipe, dynamic CLI, runtime path, and managed `notify` entries out of common configuration.
- Require preview binding, local authorization, offline process guards, sealed backups, verification, and rollback for every mutation.
- Never stop or restart Codex Desktop, Codex CLI/app-server, or CC Switch without a separate explicit user authorization at that time.
- A live route probe is separate from repair and requires explicit quota acknowledgment.
- Use the existing Rust native service; do not add another daemon or a Python production fallback for byte-sensitive mutation.
- Keep current unrelated working-tree and staged changes intact; commit feature paths with `git commit --only`.
- Rust audit benchmark: median wall time at most 65% of Python on the same 10 GiB warm-cache fixture and private memory at most 256 MiB.
- Rust cold rewrite benchmark: within 5% of sequential-copy throughput and private memory at most 256 MiB.

---

### Task 1: Rust Worker Protocol And Redaction Foundation

**Files:**
- Create: `src-tauri/native-service/src/provider_interop/mod.rs`
- Create: `src-tauri/native-service/src/provider_interop/protocol.rs`
- Create: `src-tauri/native-service/src/provider_interop/redaction.rs`
- Modify: `src-tauri/native-service/src/main.rs`
- Modify: `src-tauri/native-service/Cargo.toml`
- Test: `src-tauri/native-service/tests/provider_interop_protocol.rs`

**Interfaces:**
- Produces: `provider_interop::run_worker(request: WorkerRequest) -> Result<WorkerResult, InteropError>`.
- Produces: `WorkerRequest { protocol_version, operation_id, nonce, stage, codex_home, adapter_root, state_digest, payload }`.
- Produces: `WorkerResult { protocol_version, operation_id, nonce, state_digest, status, payload, errors }`.
- Produces: `redact_json(value: &Value) -> Value` and `contains_secret(value: &Value) -> bool`.

- [ ] **Step 1: Add failing protocol and redaction tests**

```rust
#[test]
fn worker_result_echoes_every_binding() {
    let request = fixture_request("status");
    let result = run_worker(request.clone()).unwrap();
    assert_eq!(result.operation_id, request.operation_id);
    assert_eq!(result.nonce, request.nonce);
    assert_eq!(result.state_digest, request.state_digest);
}

#[test]
fn redaction_removes_nested_tokens_and_authorization_headers() {
    let value = json!({"auth": {"experimental_bearer_token": "secret"}, "headers": {"Authorization": "Bearer secret"}});
    let redacted = redact_json(&value);
    assert!(!serde_json::to_string(&redacted).unwrap().contains("secret"));
}
```

- [ ] **Step 2: Run the focused Rust tests and confirm failure**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --test provider_interop_protocol`

Expected: compilation fails because the provider interoperability module and types do not exist.

- [ ] **Step 3: Implement typed protocol, stable error codes, recursive redaction, and `provider-interop-worker` stdin/stdout dispatch**

The worker command reads exactly one JSON request from stdin, rejects trailing non-whitespace bytes, emits one JSON result to stdout, sends diagnostics to stderr without payload data, and exits nonzero on malformed protocol input. Add `toml_edit = "0.23"` with the exact Cargo lock resolution.

- [ ] **Step 4: Run tests and CLI black-box checks**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --test provider_interop_protocol`

Run a valid and invalid request through `cargo run --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service -- provider-interop-worker`; verify one bound JSON result for the valid request and a nonzero exit with no secret echo for invalid input.

- [ ] **Step 5: Commit only Task 1 paths**

```powershell
git commit --only -m "Add provider interoperability worker protocol" -- src-tauri/native-service/Cargo.toml src-tauri/Cargo.lock src-tauri/native-service/src/main.rs src-tauri/native-service/src/provider_interop src-tauri/native-service/tests/provider_interop_protocol.rs
```

### Task 2: Native Codex And CC Switch Read-Only Adapters

**Files:**
- Create: `src-tauri/native-service/src/provider_interop/environment.rs`
- Create: `src-tauri/native-service/src/provider_interop/adapters/mod.rs`
- Create: `src-tauri/native-service/src/provider_interop/adapters/native_codex.rs`
- Create: `src-tauri/native-service/src/provider_interop/adapters/cc_switch.rs`
- Test: `src-tauri/native-service/tests/provider_interop_adapters.rs`
- Test fixtures: `tests/fixtures/provider_interop/cc_switch_schema_16.sql`
- Test fixtures: `tests/fixtures/provider_interop/codex_config_official.toml`
- Test fixtures: `tests/fixtures/provider_interop/codex_config_proxy.toml`

**Interfaces:**
- Consumes: Task 1 protocol and redaction types.
- Produces: `ResolvedEnvironment`, `AdapterInspection`, `AdapterSupport`, `RouteClassification`, and `HistoryBucketSummary`.
- Produces: `inspect_environment(request: InspectRequest) -> Result<AdapterInspection, InteropError>`.

- [ ] **Step 1: Add failing fixture-driven adapter tests**

Cover CC Switch 3.19.2/schema 16 support, a schema with one removed column, Codex Home mismatch, official configuration without `base_url`, proxy configuration with a redacted token, and absent CC Switch.

```rust
assert_eq!(inspection.adapter.support, AdapterSupport::ReadWrite);
assert_eq!(inspection.route.classification, RouteClassification::ThirdParty);
assert_eq!(inspection.route.confidence, RouteConfidence::ConfigurationOnly);
assert_eq!(inspection.auth.provider_token, Presence::Present);
assert!(inspection.public_json().to_string().find("fixture-secret").is_none());
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --test provider_interop_adapters`

- [ ] **Step 3: Implement path resolution and capability-based schema fingerprinting**

Resolve an explicit adapter root first, then running executable metadata, uninstall metadata supplied by the Python control plane, and the conventional per-user path. Open inspection databases with `SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI`, run `PRAGMA quick_check`, enumerate required table/column signatures, and map only the exact schema-16 capability set to read-write.

- [ ] **Step 4: Implement route classification without exposing configuration**

Return booleans, classifications, opaque SHA-256 identity prefixes, and redacted evidence only. Treat conflicting third-party `base_url` plus `requires_openai_auth=true` as `conflicting`, not official or third-party.

- [ ] **Step 5: Run adapters, redaction, and clippy gates**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --test provider_interop_adapters --test provider_interop_protocol`

Run: `cargo clippy --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --all-targets -- -D warnings`

- [ ] **Step 6: Commit only Task 2 paths**

```powershell
git commit --only -m "Inspect Codex and CC Switch provider state" -- src-tauri/native-service/src/provider_interop/environment.rs src-tauri/native-service/src/provider_interop/adapters tests/fixtures/provider_interop src-tauri/native-service/tests/provider_interop_adapters.rs
```

### Task 3: Rust History Audit And Preservation Contracts

**Files:**
- Create: `src-tauri/native-service/src/provider_interop/history.rs`
- Create: `src-tauri/native-service/tests/provider_interop_history.rs`
- Create: `src-tauri/native-service/tests/provider_interop_history_properties.rs`
- Create: `scripts/benchmark-provider-interoperability.ps1`

**Interfaces:**
- Consumes: `WorkerRequest`, `ResolvedEnvironment`, and adapter history profile from Tasks 1-2.
- Produces: `audit_history(request: HistoryAuditRequest, cancel: CancellationFlag) -> Result<HistoryAudit, InteropError>`.
- Produces: `PromptFingerprint`, `ProtectedContentFingerprint`, `HistoryFilePlan`, and `HistoryMigrationPlan`.

- [ ] **Step 1: Add failing golden and property tests**

Generate line-ending, whitespace, field-order, malformed-line, active/archive, source-growth, unknown-provider, reasoning, `encrypted_content`, tool-output, and legacy-user-message fixtures. Property tests generate JSON objects with one eligible `session_meta.model_provider` and assert that normalized before/after bytes differ only in the six-byte provider value.

- [ ] **Step 2: Confirm tests fail before implementation**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --test provider_interop_history --test provider_interop_history_properties`

- [ ] **Step 3: Implement one-pass memory-bounded audit**

Use buffered reads and `memchr` candidate detection. Parse only metadata and prompt-bearing candidate lines while hashing every byte. Record source size, nanosecond mtime, SHA-256, line count, parse errors, bucket counts, prompt count/order hash, protected-content hash, and estimated backup bytes.

- [ ] **Step 4: Implement temporary rewrite and preservation verification without replacing sources**

Write sibling temporary files, perform only confirmed equal-length `openai` to `custom` replacements, compute all output contracts during the same pass, and reject source-stat drift before returning a ready plan.

- [ ] **Step 5: Run correctness, cancellation, and mutation tests**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service provider_interop::history`

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --test provider_interop_history --test provider_interop_history_properties`

- [ ] **Step 6: Run the 10 GiB benchmark with memory monitoring**

The PowerShell script creates a sparse/repeated synthetic fixture under the configured large-test root, records Python and Rust medians across three warm-cache audits, performs one cold sequential-copy and Rust rewrite run, samples private bytes, writes JSON results, and exits nonzero when either specified threshold fails.

- [ ] **Step 7: Commit only Task 3 paths**

```powershell
git commit --only -m "Audit provider history with Rust" -- src-tauri/native-service/src/provider_interop/history.rs src-tauri/native-service/tests/provider_interop_history.rs src-tauri/native-service/tests/provider_interop_history_properties.rs scripts/benchmark-provider-interoperability.ps1
```

### Task 4: Shared TOML Classification And Merge Planning

**Files:**
- Create: `src-tauri/native-service/src/provider_interop/config.rs`
- Test: `src-tauri/native-service/tests/provider_interop_config.rs`
- Test fixtures: `tests/fixtures/provider_interop/common_config.toml`
- Test fixtures: `tests/fixtures/provider_interop/desktop_dynamic_config.toml`

**Interfaces:**
- Produces: `classify_config(source: &str) -> Result<ConfigClassification, InteropError>`.
- Produces: `plan_common_merge(common: &str, live: &str, provider_snapshots: &[ProviderSnapshot]) -> Result<ConfigMergePlan, InteropError>`.
- Produces: field decisions with `Shared`, `ProviderScoped`, `DesktopDynamic`, `Secret`, or `Unknown`.

- [ ] **Step 1: Add failing classification and round-trip tests**

Assert that plugins and marketplaces merge, credential-bearing MCP entries stay scoped, unknown entries block automatic copy, provider routes remain byte-identical, and dynamic `node_repl`/native-pipe/notify values never enter common output.

- [ ] **Step 2: Confirm tests fail**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --test provider_interop_config`

- [ ] **Step 3: Implement `toml_edit` classification and deterministic merge**

Use explicit allow/deny classifiers, secret-key/value detection, stable table identities, and source spans. Return planned additions, conflicts, exclusions, semantic hashes, and rendered output; do not write files in this task.

- [ ] **Step 4: Run config and adapter regression tests**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --test provider_interop_config --test provider_interop_adapters`

- [ ] **Step 5: Commit only Task 4 paths**

```powershell
git commit --only -m "Plan safe provider common configuration" -- src-tauri/native-service/src/provider_interop/config.rs src-tauri/native-service/tests/provider_interop_config.rs tests/fixtures/provider_interop/common_config.toml tests/fixtures/provider_interop/desktop_dynamic_config.toml src-tauri/native-service/Cargo.toml src-tauri/Cargo.lock
```

### Task 5: Journaled Apply, Backup, Verification, And Rollback

**Files:**
- Create: `src-tauri/native-service/src/provider_interop/sqlite.rs`
- Create: `src-tauri/native-service/src/provider_interop/transaction.rs`
- Test: `src-tauri/native-service/tests/provider_interop_transaction.rs`
- Test: `src-tauri/native-service/tests/provider_interop_fault_injection.rs`

**Interfaces:**
- Consumes: history and config plans from Tasks 3-4.
- Produces: `apply_transaction(plan, backup_root, fault) -> Result<TransactionResult, InteropError>`.
- Produces: `recover_transaction(journal_path) -> Result<RecoveryResult, InteropError>`.
- Produces: `rollback_transaction(manifest, current_state) -> Result<RollbackResult, InteropError>`.

- [ ] **Step 1: Add failing transaction state-machine tests**

Test `planned -> backed_up -> external_config_applied -> jsonl_applied -> sqlite_applied -> verified -> committed`, plus injected failure before and after every mutation boundary. Assert exact pre-image restoration and quick-check success.

- [ ] **Step 2: Confirm tests fail**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service --test provider_interop_transaction --test provider_interop_fault_injection`

- [ ] **Step 3: Implement backup capacity, copies, SQLite backup API, and fsynced journal**

Validate resolved containment and reparse points, reserve capacity using affected bytes plus 10%, hash every backup, flush the journal before replacement, explicitly close SQLite handles, and refuse apply when source state differs from the plan.

- [ ] **Step 4: Implement apply, post-verification, automatic recovery, and explicit rollback**

Never reverse-transform when a verified pre-image exists. Return `failed_rolled_back` only after every restored target passes hashes and SQLite quick check; otherwise return `failed_manual_recovery_required` with the local journal and backup IDs.

- [ ] **Step 5: Run transaction, fault, history, and config suites**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service provider_interop`

- [ ] **Step 6: Commit only Task 5 paths**

```powershell
git commit --only -m "Apply provider repairs with rollback" -- src-tauri/native-service/src/provider_interop/sqlite.rs src-tauri/native-service/src/provider_interop/transaction.rs src-tauri/native-service/tests/provider_interop_transaction.rs src-tauri/native-service/tests/provider_interop_fault_injection.rs
```

### Task 6: Python Worker Client And Control Plane

**Files:**
- Create: `backend/provider_interop/__init__.py`
- Create: `backend/provider_interop/models.py`
- Create: `backend/provider_interop/worker.py`
- Create: `backend/provider_interop/routing.py`
- Create: `backend/provider_interop/transaction.py`
- Create: `backend/provider_interop/service.py`
- Test: `tests/test_provider_interop_worker.py`
- Test: `tests/test_provider_interop_service.py`
- Test: `tests/test_provider_interop_parity.py`

**Interfaces:**
- Produces: `ProviderInteropService.status`, `.audit`, `.preview_repair`, `.apply_repair`, `.verify`, `.preview_rollback`, `.rollback`, and `.audit_thread`.
- Uses existing `create_preview_ticket`, `require_preview_ticket`, API authorization, process inspection, and backup catalog contracts.

- [ ] **Step 1: Add failing worker-security and service tests**

Cover binding mismatch, extra stdout JSON, timeout, crash, stale digest, missing worker, process blockers, redaction, preview expiry, quota acknowledgment, and golden Rust/Python public-result parity.

- [ ] **Step 2: Confirm tests fail**

Run: `python -m pytest tests/test_provider_interop_worker.py tests/test_provider_interop_service.py tests/test_provider_interop_parity.py -q`

- [ ] **Step 3: Implement hidden worker invocation and typed result validation**

Use the packaged native-service resolver, `subprocess` without a shell or visible window, JSON stdin, capped stdout/stderr, operation timeout, nonce/digest checks, and recursive public-result redaction. Do not place secrets in arguments or environment variables.

- [ ] **Step 4: Implement service orchestration and process policies**

Status stays read-only. Full audit supports cancellation. Preview creates one state-bound ticket. Apply rechecks processes and delegates one journaled Rust operation. Verify is static unless `allowLiveRequest=true` and `acknowledgeQuotaUsage=true`.

- [ ] **Step 5: Run Python tests and Rust/Python golden parity**

Run: `python -m pytest tests/test_provider_interop_worker.py tests/test_provider_interop_service.py tests/test_provider_interop_parity.py -q`

- [ ] **Step 6: Commit only Task 6 paths**

```powershell
git commit --only -m "Orchestrate provider interoperability repairs" -- backend/provider_interop tests/test_provider_interop_worker.py tests/test_provider_interop_service.py tests/test_provider_interop_parity.py
```

### Task 7: REST, OpenAPI, And MCP Exposure

**Files:**
- Create: `backend/provider_interop/mcp.py`
- Modify: `backend/server.py`
- Modify: `backend/api_contracts.py`
- Test: `tests/test_provider_interop_api.py`
- Test: `tests/test_provider_interop_mcp.py`
- Test: `tests/test_public_api_contract.py`

**Interfaces:**
- Consumes: `ProviderInteropService` from Task 6.
- Produces the eight REST routes and eight MCP tools specified by the design.

- [ ] **Step 1: Add failing REST/MCP parity tests**

Assert status/audit/task audit reads, preview-bound repair/rollback, authorization errors, live-probe acknowledgment, error-code stability, MCP token requirements, and identical normalized REST/MCP results.

- [ ] **Step 2: Confirm tests fail**

Run: `python -m pytest tests/test_provider_interop_api.py tests/test_provider_interop_mcp.py tests/test_public_api_contract.py -q`

- [ ] **Step 3: Add routes and MCP tool definitions without business logic duplication**

Routes validate Pydantic requests and call the service. MCP handlers use the same models and service methods. Add capability metadata with preview endpoint, write endpoint, backup behavior, rollback mode, risk level, and idempotency.

- [ ] **Step 4: Run API/MCP and server regression tests**

Run: `python -m pytest tests/test_provider_interop_api.py tests/test_provider_interop_mcp.py tests/test_public_api_contract.py tests/test_codex_data.py -q`

- [ ] **Step 5: Commit only Task 7 paths**

```powershell
git commit --only -m "Expose provider interoperability APIs" -- backend/provider_interop/mcp.py backend/server.py backend/api_contracts.py tests/test_provider_interop_api.py tests/test_provider_interop_mcp.py tests/test_public_api_contract.py
```

### Task 8: Provider Interoperability React Module

**Files:**
- Create: `src/providerInteroperability.ts`
- Create: `src/ProviderInteroperability.tsx`
- Create: `src/provider-interoperability.css`
- Modify: `src/main.tsx`
- Test: `tests/provider_interoperability_ui.test.mjs`
- Test: `tests/frontend_accessibility.test.mjs`
- Test: `tests/frontend_quality.test.mjs`

**Interfaces:**
- Consumes the REST types from Task 7.
- Produces the `providers` navigation section and dense operational workflow.

- [ ] **Step 1: Add failing UI contract and accessibility tests**

Cover status bands, route-confidence wording, adapter support, scope checkboxes, preview dialog, blocker display, backup list, rollback preview, task audit, browser-folder read-only mode, secret absence, focus trapping, Escape behavior, keyboard navigation, and 390/768/1440 px overflow.

- [ ] **Step 2: Confirm tests fail**

Run: `node tests/provider_interoperability_ui.test.mjs`

- [ ] **Step 3: Implement typed API hooks and module states**

Use explicit `idle`, `loading`, `ready`, `blocked`, `applying`, `verifying`, and `failed` states. Never infer success from HTTP status alone; show transaction and verification status separately.

- [ ] **Step 4: Implement UI and responsive styling**

Add a Lucide route icon, compact evidence/status bands, scope controls, command buttons, preview/rollback modal, task lookup, and recent backups. Do not render recoverable secret values. Keep buttons and counters dimensionally stable.

- [ ] **Step 5: Run frontend tests, TypeScript build, and Playwright screenshots**

Run: `npm run build`

Run: `node tests/provider_interoperability_ui.test.mjs && node tests/frontend_accessibility.test.mjs && node tests/frontend_quality.test.mjs`

Capture desktop and mobile screenshots and run the existing overflow checker; confirm no overlap, clipping, blank panel, or inaccessible control.

- [ ] **Step 6: Commit only Task 8 paths**

```powershell
git commit --only -m "Add provider interoperability console" -- src/providerInteroperability.ts src/ProviderInteroperability.tsx src/provider-interoperability.css src/main.tsx tests/provider_interoperability_ui.test.mjs tests/frontend_accessibility.test.mjs tests/frontend_quality.test.mjs
```

### Task 9: Task Compatibility Audit And Bundled Skill

**Files:**
- Create: `packaging/skills/codex-provider-interoperability/SKILL.md`
- Modify: `backend/provider_interop/service.py`
- Modify: `backend/thread_history_repair.py`
- Modify: `scripts/package-local-connector.ps1`
- Modify: `scripts/prepare-desktop-sidecar.ps1`
- Test: `tests/test_provider_interop_task_audit.py`
- Test: `tests/test_provider_interop_skill.py`

**Interfaces:**
- Produces `audit_thread(thread_id) -> TaskCompatibilityAudit` using existing checkpoint and prompt-preservation primitives.
- Produces one packaged skill that calls Home Manager APIs/MCP and never edits state directly.

- [ ] **Step 1: Read the current `skill-creator` instructions and add failing skill/task-audit tests**

Tests require status-first, preview-before-write, backup reporting, no direct database instructions, no machine-specific paths, no credential examples, and explicit separation between visibility and continuation.

- [ ] **Step 2: Confirm tests fail**

Run: `python -m pytest tests/test_provider_interop_task_audit.py tests/test_provider_interop_skill.py -q`

- [ ] **Step 3: Reuse existing thread repair scanners through a narrow adapter**

Return provider bucket, incompatible `item_*` counts, non-portable reasoning count, latest valid checkpoint, prompt baseline, and recommendation. Do not mutate the thread from the audit route.

- [ ] **Step 4: Write and package the skill**

The skill routes agents through `codex_provider_interoperability_status`, audit, preview, repair, verify, and rollback tools; it instructs them to stop on unknown schemas and to request shutdown/quota authorization only when required.

- [ ] **Step 5: Run skill, task-audit, packaging-resource, and secret scans**

Run: `python -m pytest tests/test_provider_interop_task_audit.py tests/test_provider_interop_skill.py -q`

- [ ] **Step 6: Commit only Task 9 paths**

```powershell
git commit --only -m "Bundle provider interoperability guidance" -- packaging/skills/codex-provider-interoperability backend/provider_interop/service.py backend/thread_history_repair.py scripts/package-local-connector.ps1 scripts/prepare-desktop-sidecar.ps1 tests/test_provider_interop_task_audit.py tests/test_provider_interop_skill.py
```

### Task 10: Full Quality, Packaging, And Release Verification

**Files:**
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `scripts/quality_gate.py`
- Modify: `scripts/release_manifest.py`
- Modify: `package.json`
- Modify: root `AGENTS.md` only with verified reusable lessons
- Generated release artifacts remain outside Git unless the existing release process tracks them.

**Interfaces:**
- Consumes all previous tasks.
- Produces packaged connector/desktop artifacts, synchronized public site, source evidence, and final verification reports.

- [ ] **Step 1: Add the new Rust, Python, UI, skill, secret-scan, benchmark-smoke, and API parity suites to the quality gate**

The default gate uses a small benchmark smoke fixture; the full 10 GiB benchmark is a release gate on the reference machine.

- [ ] **Step 2: Run focused gates before the full suite**

Run: `cargo test --manifest-path src-tauri/Cargo.toml -p codex-home-manager-service provider_interop`

Run: `python -m pytest tests/test_provider_interop_*.py -q`

Run: `node tests/provider_interoperability_ui.test.mjs`

- [ ] **Step 3: Run the complete product gate**

Run: `npm run gate`

Expected: TypeScript, Rust, Python, browser contracts, accessibility, packaging policy, source integrity, and secret scanning all pass.

- [ ] **Step 4: Build connector and desktop packages twice and verify reproducibility**

Run the existing local connector and desktop package scripts, compare artifact SHA-256 values, run the verifier, inspect process behavior, and verify `/api/provider-interoperability/status`, OpenAPI, and `/mcp` from the packaged binary.

- [ ] **Step 5: Perform safe live validation without stopping user applications**

Run status and full audit only against the reference `D:\.codex` and CC Switch state. Confirm the existing unified state produces no repair plan, route reporting remains redacted, and plugins/common config are consistent. Do not apply, restart, or issue a live provider request unless a real defect remains and the user separately authorizes that action.

- [ ] **Step 6: Synchronize and deploy the public site and release metadata**

Use the repository's existing Cloudflare Pages workflow, then verify the public page bundle, artifact download, manifest signature, executable hash, source evidence, and skill inclusion.

- [ ] **Step 7: Record only verified reusable lessons and commit release source changes**

```powershell
git commit --only -m "Release provider interoperability support" -- README.md SECURITY.md scripts/quality_gate.py scripts/release_manifest.py package.json AGENTS.md
```

- [ ] **Step 8: Final evidence report**

Report commit IDs, tests and benchmark measurements, package hashes, deployment URL, supported CC Switch schema fingerprint, read-only live audit result, backup behavior, and any action intentionally not performed because it required shutdown or quota authorization.
