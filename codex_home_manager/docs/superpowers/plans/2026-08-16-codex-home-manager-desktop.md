# Codex Home Manager Desktop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Codex Home Manager as an installable Windows 11 x64 desktop app that silently manages its local service and preserves the existing REST and MCP behavior.

**Architecture:** Add a Tauri 2 shell around the existing React/Vite UI and package the current FastAPI/Python connector as a supervised sidecar. Keep the stable loopback API on port 8765, add a typed desktop runtime boundary, and migrate read-heavy indexing to Rust only after benchmark evidence and behavior-parity tests.

**Tech Stack:** Tauri 2.11, Rust 1.96, React 19, TypeScript 5.9, Vite 6, Python/FastAPI, PyInstaller, WebView2, NSIS, Playwright, pytest.

## Global Constraints

- The first supported platform is Windows 11 x64.
- Launching the installed app must not open a console or external browser.
- Existing REST and MCP contracts remain available at `127.0.0.1:8765`.
- Refresh failure must preserve the last valid catalog instead of replacing it with zero counts.
- The app must not close or restart Codex without explicit approval for that operation.
- The primary installer is per-user NSIS and must not require administrator privileges.
- The first local desktop release is not blocked by GitHub or Cloudflare publishing.
- Existing uncommitted user changes must be preserved.

---

### Task 1: Establish the Tauri Desktop Shell

**Files:**
- Create: `src-tauri/Cargo.toml`
- Create: `src-tauri/build.rs`
- Create: `src-tauri/tauri.conf.json`
- Create: `src-tauri/capabilities/main.json`
- Create: `src-tauri/src/main.rs`
- Create: `src-tauri/src/runtime_status.rs`
- Modify: `package.json`
- Modify: `vite.config.ts`

**Interfaces:**
- Produces: Tauri command `desktop_runtime_status() -> DesktopRuntimeStatus`.
- Produces: npm scripts `desktop:dev`, `desktop:build`, and `desktop:check`.

- [ ] Add a Rust test that serializes `DesktopRuntimeStatus` with `runtime="desktop"`, `service_state="starting"`, and `api_base_url="http://127.0.0.1:8765"`.
- [ ] Run `cargo test --manifest-path src-tauri/Cargo.toml` and verify the test fails before the type exists.
- [ ] Add the Tauri package metadata, `tauri-build`, `serde`, `serde_json`, and a focused `runtime_status.rs` module.
- [ ] Configure the main window, CSP, bundled Vite output, per-user NSIS target, embedded WebView2 bootstrapper, and a single `main` capability with no unrestricted shell or filesystem permission.
- [ ] Add exact npm dependency versions for `@tauri-apps/api` and `@tauri-apps/cli`, then wire build and check scripts.
- [ ] Run `npm run build`, `npm run desktop:check`, and the Rust unit tests.
- [ ] Commit only Task 1 files.

### Task 2: Add a Non-Browser Python Sidecar Mode

**Files:**
- Modify: `packaging/windows/connector_main.py`
- Modify: `tests/test_windows_connector.py`
- Modify: `backend/server.py`
- Modify: `tests/test_codex_data.py`

**Interfaces:**
- Consumes: launch argument `--desktop-sidecar`.
- Consumes: environment variable `CODEX_HOME_MANAGER_DESKTOP_SHUTDOWN_TOKEN`.
- Produces: `POST /api/desktop/shutdown`, accepted only for loopback requests with header `X-Codex-Home-Manager-Shutdown` matching the process token.

- [ ] Add failing tests proving desktop-sidecar mode does not call `register_browser_protocol()` or `webbrowser.open()`.
- [ ] Add failing tests proving the shutdown endpoint rejects missing or incorrect tokens and signals shutdown for the correct token.
- [ ] Extract connector launch decisions into `connector_mode(argv: Sequence[str])` and make desktop mode skip protocol registration, notifications, and browser launch.
- [ ] Register the shutdown endpoint only when the desktop token exists; compare tokens with `secrets.compare_digest` and schedule process termination after the HTTP response is sent.
- [ ] Keep ordinary connector behavior unchanged and run targeted connector and API tests.
- [ ] Run `python -m pytest tests/test_windows_connector.py tests/test_codex_data.py -q`.
- [ ] Commit only Task 2 files.

### Task 3: Supervise the Sidecar from Rust

**Files:**
- Create: `src-tauri/src/service.rs`
- Create: `src-tauri/src/windows_job.rs`
- Modify: `src-tauri/src/main.rs`
- Modify: `src-tauri/Cargo.toml`
- Modify: `src-tauri/tauri.conf.json`
- Create: `scripts/prepare-desktop-sidecar.ps1`
- Modify: `package.json`

**Interfaces:**
- Produces: `ServiceSupervisor::ensure_running(&AppHandle) -> Result<ServiceSnapshot, ServiceError>`.
- Produces: commands `desktop_service_status()` and `desktop_restart_service()`.
- Produces: event `desktop-service-status` containing `state`, `detail`, `owned`, `version`, and `updatedAt`.

- [ ] Add unit tests for compatible-service attach, occupied-port rejection, startup timeout, one-restart limit, and owned-process shutdown decisions using a mock probe and launcher.
- [ ] Implement a probe abstraction that validates service name, version, frontend contract version, OpenAPI path, MCP path, and capability list.
- [ ] Implement hidden sidecar spawn with a cryptographically random shutdown token, `--desktop-sidecar`, and no inherited console window.
- [ ] Assign the owned sidecar PID to a Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- [ ] On app exit, call the authenticated shutdown endpoint, wait a bounded interval, then close the Job Object as the final cleanup boundary.
- [ ] Add a prebuild script that packages the Python connector and copies it to Tauri's target-suffixed `externalBin` path.
- [ ] Run Rust tests, build the sidecar, launch the shell twice, and verify the second launch does not create another sidecar.
- [ ] Commit only Task 3 files.

### Task 4: Add Single-Instance and Persistent Window Behavior

**Files:**
- Modify: `src-tauri/Cargo.toml`
- Modify: `src-tauri/src/main.rs`
- Modify: `src-tauri/tauri.conf.json`

**Interfaces:**
- Consumes: Tauri window label `main`.
- Produces: second-instance behavior that restores, shows, focuses, and unminimizes the existing main window.

- [ ] Add a focused Rust test for the pure decision function that maps hidden/minimized/focused state to restore actions.
- [ ] Register `tauri-plugin-single-instance` before other plugins.
- [ ] Register `tauri-plugin-window-state` for main-window geometry persistence.
- [ ] Ensure a second launch forwards arguments and activates the existing window without spawning a service.
- [ ] Run Rust tests and a two-process installed-app smoke test.
- [ ] Commit only Task 4 files.

### Task 5: Introduce the Typed Desktop Frontend Runtime

**Files:**
- Create: `src/desktopRuntime.ts`
- Modify: `src/main.tsx`
- Modify: `src/styles.css`
- Modify: `tests/frontend_connector_contracts.test.mjs`
- Modify: `tests/frontend_accessibility.test.mjs`

**Interfaces:**
- Produces: `detectRuntime(): "desktop" | "browser"`.
- Produces: `readDesktopRuntimeStatus(): Promise<DesktopRuntimeStatus | null>`.
- Produces: `subscribeDesktopServiceStatus(listener): Promise<UnlistenFn>`.
- Consumes: Rust commands and `desktop-service-status` events from Tasks 1 and 3.

- [ ] Add failing browser-contract tests for browser mode and a Tauri-stubbed desktop mode.
- [ ] Implement the desktop runtime module with dynamic Tauri imports so the hosted web build remains valid.
- [ ] In desktop mode, remove connector URL, browser permission, connector download, and manual connector-start controls.
- [ ] Add a compact status-center entry with starting, ready, degraded, and conflict states; preserve current content during reconnect.
- [ ] Replace the blank catalog loading screen with the stable application frame, last-valid snapshot, snapshot age, and non-destructive refresh progress.
- [ ] Add accessibility assertions for service status, progress, keyboard focus, and error details.
- [ ] Run the frontend build and targeted browser contract/accessibility tests.
- [ ] Commit only Task 5 files.

### Task 6: Preserve Catalog Data During Startup and Refresh Failures

**Files:**
- Modify: `backend/codex_data.py`
- Modify: `backend/server.py`
- Modify: `src/main.tsx`
- Modify: `tests/test_codex_data.py`
- Modify: `tests/frontend_review_regressions.test.mjs`

**Interfaces:**
- Produces: catalog response metadata `snapshotCreatedAt`, `snapshotState`, `refreshState`, and `staleReason`.
- Guarantees: incomplete or failed refreshes cannot replace a complete persisted snapshot.

- [ ] Add backend tests for valid-cache warm start, failed-refresh preservation, partial-refresh preservation, and atomic replacement after a complete refresh.
- [ ] Add frontend regression coverage proving zero-count transient payloads do not replace a non-empty valid snapshot unless the payload is explicitly complete and newer.
- [ ] Implement snapshot completeness metadata and compare-and-swap persistence.
- [ ] Render stale and refresh state without blocking filtering, sorting, or opening thread details.
- [ ] Run targeted backend/frontend tests, then the complete existing backend suite.
- [ ] Commit only Task 6 files.

### Task 7: Build and Verify the Windows Installer

**Files:**
- Create: `scripts/package-desktop.ps1`
- Create: `scripts/verify-desktop-release.ps1`
- Modify: `src-tauri/tauri.conf.json`
- Modify: `packaging/windows/version_info.txt`
- Modify: `package.json`

**Interfaces:**
- Produces: `build/releases/Codex-Home-Manager-Setup-x64.exe`.
- Produces: machine-readable verification report under `build/desktop-verification/`.

- [ ] Write a release verifier that checks installer existence, hashes, version resources, bundled sidecar identity, and absence of unexpected console subsystem binaries.
- [ ] Add `package:desktop` to build the frontend, sidecar, Tauri executable, and per-user NSIS installer in order.
- [ ] Configure application icon, product metadata, per-user install mode, and embedded WebView2 bootstrapper.
- [ ] Install into a test user context, launch from the installed shortcut, and verify no browser or console appears.
- [ ] Verify `GET /api/capabilities`, `/openapi.json`, and MCP `tools/list` after desktop launch.
- [ ] Verify second launch activates the same window and uninstall removes application files while preserving user data and backups.
- [ ] Run the release verifier and commit Task 7 files.

### Task 8: Performance and Product Verification

**Files:**
- Create: `scripts/desktop-performance-benchmark.py`
- Create: `tests/desktop_smoke.spec.mjs`
- Modify: `package.json`
- Modify: performance-sensitive backend or Rust files only when a benchmark proves a failing target.

**Interfaces:**
- Produces: JSON benchmark results for cold start, warm catalog, normal thread detail, 2 GB newest-content lookup, indexed search, peak RSS, and UI long tasks.
- Produces: final installed-app smoke-test result.

- [ ] Add benchmark fixtures that use existing real-data-safe paths without modifying Codex source files.
- [ ] Measure the baseline and record pass/fail against the design thresholds.
- [ ] Profile each failed threshold and change only the measured bottleneck; use the Rust performance boundary when Python parsing is the proven bottleneck.
- [ ] Add regression tests for every performance fix.
- [ ] Run `npm test`, Rust tests, desktop smoke tests, packaging verification, and the benchmark suite.
- [ ] Confirm there are no browser windows, console windows, orphan sidecars, destructive refreshes, or MCP regressions.
- [ ] Commit the verified desktop implementation and retain the installer and verification report in `build/releases` and `build/desktop-verification`.
