# Codex Home Manager Windows Desktop Design

## 1. Goal

Turn Codex Home Manager into a Windows 11 x64 desktop application that opens as a normal app, requires no browser or terminal interaction, starts its local service automatically, preserves the existing REST and MCP contracts, and remains responsive when managing multi-gigabyte Codex thread files.

The desktop app becomes the primary product. The hosted web console and browser-folder mode remain compatibility surfaces, not constraints on the desktop experience.

## 2. Success Criteria

- A user installs one per-user `setup.exe` and launches Codex Home Manager from the Start menu or desktop shortcut.
- Launching the app never opens a terminal or external browser.
- The main window appears within one second and shows a usable cached thread catalog within 500 milliseconds when a valid snapshot exists.
- The app silently starts and supervises the existing local Python service.
- Existing REST endpoints and the MCP endpoint remain available at `127.0.0.1:8765` for agents and existing integrations.
- Catalog refresh failures never replace valid data with zero counts or an empty result.
- Opening the newest content of an indexed 2 GB JSONL file takes no more than one second on the reference machine.
- Search against an established local index starts returning results within 150 milliseconds.
- Memory use is bounded by the visible and prefetched windows rather than total thread size.
- The app never closes or restarts Codex without explicit approval for that operation.

## 3. Chosen Architecture

Use Tauri 2 as the Windows desktop shell, preserve the React/Vite frontend, and package the existing Python/FastAPI backend as a managed sidecar. Introduce a Rust data layer only behind measured performance interfaces; migrate individual read-heavy hotspots after benchmark evidence, not through a full backend rewrite.

This architecture preserves the existing repair logic, MCP implementation, API behavior, and regression suite while removing browser and terminal friction. It also keeps a clear path to native performance without risking a broad rewrite.

### 3.1 Desktop Core

The Rust Tauri core owns:

- single-instance behavior and window activation;
- startup and shutdown sequencing;
- sidecar discovery, startup, readiness checks, supervision, and cleanup;
- native dialogs, application paths, logs, and window-state persistence;
- desktop capability boundaries;
- update checks and user-approved installation;
- future Rust indexing commands.

The core starts the sidecar without a console window. On Windows it places the sidecar in a Job Object configured so that an unexpected desktop-app exit cannot leave an orphan service.

### 3.2 Frontend

The existing React/Vite application remains the UI codebase. A runtime transport boundary separates desktop and web concerns:

- desktop mode obtains service state and native operations through a narrow Tauri bridge;
- browser mode retains its existing connector and folder-access behavior;
- shared feature components consume the same typed domain contracts.

Desktop mode removes connector addresses, browser permission instructions, connector download prompts, and manual-start controls. Connection state moves into a compact status center and appears prominently only when degraded.

### 3.3 Python Service

The Python service remains the source of truth for existing Codex inspection, diagnostics, repair workflows, backups, REST endpoints, and MCP tools. It receives an explicit desktop-sidecar launch mode that:

- does not register browser launch behavior;
- does not open a browser;
- writes structured readiness and failure information;
- shuts down gracefully when instructed by the desktop core;
- keeps the stable loopback API and MCP contract on port 8765.

If a compatible healthy service is already running, the desktop app attaches to it instead of starting a duplicate. If another process owns port 8765, the app shows an actionable conflict diagnosis and does not wait indefinitely.

### 3.4 Rust Performance Layer

The first desktop release defines, benchmarks, and tests a Rust indexing boundary. Python remains the implementation until a Rust replacement passes behavior-parity and performance gates. Candidate migrations are:

1. thread-catalog filesystem reconciliation;
2. reverse and incremental JSONL record scanning;
3. Unicode-normalized full-text indexing and search;
4. file-identity and append-only change detection.

Mutation and repair routines stay in Python unless a separate design proves a native rewrite safer.

## 4. Runtime Flow

1. Tauri creates the main window immediately and the frontend renders its static shell plus the last valid catalog snapshot.
2. The desktop core probes the expected local service contract.
3. It attaches to a compatible service or starts the packaged sidecar silently.
4. The service returns its persisted SQLite snapshot before beginning expensive JSONL reconciliation.
5. Background updates stream changed entities to the frontend; they do not clear the current view.
6. Search, timeline, prompt, assistant reply, and natural-language progress views use cursor-based indexed queries and cancellable requests.
7. On exit, the core stops new work, requests graceful sidecar shutdown when it owns the process, and relies on the Job Object as the final cleanup boundary.

## 5. Data and Cache Rules

- SQLite metadata is the fast catalog source; JSONL files are the background verification source.
- Every snapshot has a schema version, creation time, source identity, and completeness state.
- Refresh uses stale-while-revalidate semantics.
- A failed or partial refresh cannot atomically replace the last valid complete snapshot.
- Project classification follows current SQLite `cwd` and verified Codex state, not stale path hints.
- Incremental indexes persist byte offsets and prefixed Windows file-identity values.
- Parser or classification changes increment the index schema and rebuild derived rows without modifying source JSONL.
- Search and displayed counts use the same Unicode normalization and classification semantics.
- Large records are size-limited before decoding; embedded `data:` payloads are never indexed as display text.

## 6. Desktop UX

The product keeps five primary areas: Threads, Diagnostics, Resources, Import, and API. The desktop title bar contains window controls and a compact status entry, not web-host or connector controls.

Startup uses a stable application frame with explicit stages and cached content. It must not show an empty page while waiting for a full scan. Long-running work exposes progress, cancellation, and the age of currently displayed data.

Thread content uses a virtual list, latest-first by default, cursor pagination, request cancellation, bounded prefetch, and a one-click jump to the newest item. User prompts, natural-language progress, and final replies remain visually and semantically distinct.

## 7. Failure Handling

- Sidecar startup has bounded deadlines and structured error states.
- A sidecar crash triggers one supervised restart. A repeated crash enters diagnostic mode and preserves the last valid UI state.
- Port conflicts identify the owning executable where possible and provide a safe next action.
- Background refresh errors are non-destructive and retain cached data.
- Write operations keep existing preview, confirmation, backup-policy, and post-write verification requirements.
- Operations that require stopping Codex explain the reason, impact, and expected duration and require explicit approval each time.
- Update installation is user initiated and never silently restarts Codex.

## 8. Security

- Tauri capabilities expose only named commands required by the main window.
- The webview receives no arbitrary shell or unrestricted filesystem permission.
- The packaged frontend is the only remote-capability origin; hosted pages cannot call desktop commands.
- Local APIs stay loopback-only and retain origin and request validation.
- Sidecar identity, version, and API contract are verified before attachment or shutdown.
- Executable signing and updater signatures are separate mandatory release gates before public distribution.

## 9. Packaging and Updates

The primary artifact is a per-user NSIS `setup.exe` that requires no administrator privileges. It includes the WebView2 bootstrapper rather than a fixed WebView2 runtime. The installer creates Start-menu integration and a normal uninstall entry.

The Tauri updater checks a signed release manifest and reports an available update. Download and installation require an explicit user action. GitHub publishing, Cloudflare deployment, and automatic release infrastructure are not completion gates for the first local desktop implementation.

## 10. Verification

The implementation is accepted only after all relevant checks pass:

- Rust unit and integration tests for process lifecycle, single-instance routing, port conflict, readiness, and cleanup;
- the existing Python backend test suite;
- existing frontend contract, accessibility, maintainability, and regression tests;
- Playwright desktop-flow tests against the packaged webview where feasible;
- cold and warm startup measurements;
- ordinary and 2 GB JSONL first-content and indexed-search benchmarks;
- sidecar crash and repeated-crash recovery tests;
- valid-cache preservation under refresh failure;
- install, launch, second-launch activation, update prompt, and uninstall tests;
- confirmation that no console or browser window appears;
- confirmation that the MCP endpoint remains callable after desktop launch.

## 11. Delivery Sequence

1. Add the Tauri shell and runtime transport boundary without changing feature behavior.
2. Add desktop-sidecar mode, hidden startup, readiness, supervision, and cleanup.
3. Adapt the UI for desktop startup, connection state, and native window behavior.
4. Package a per-user Windows installer and verify installed execution.
5. Establish performance benchmarks and fix catalog and timeline regressions.
6. Migrate only benchmark-proven read hotspots to Rust while preserving contract parity.
7. Run full product verification and produce the locally installable release artifact.

## 12. Explicit Non-Goals for the First Release

- macOS or Linux release artifacts;
- a full Rust rewrite of repair and mutation logic;
- a full WinUI rewrite;
- mandatory cloud accounts or remote telemetry;
- automatic Codex shutdown or restart;
- making public-site deployment or GitHub release automation block local desktop delivery.
