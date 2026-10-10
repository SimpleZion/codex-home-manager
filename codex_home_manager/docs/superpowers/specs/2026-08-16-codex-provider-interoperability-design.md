# Codex Provider Interoperability Design

## 1. Purpose

Add a product-grade provider interoperability capability to Codex Home Manager so users can switch between OpenAI Official and compatible third-party Codex providers without losing task visibility or shared plugins, while authentication, routing, credentials, and billing remain isolated.

The feature is named **Provider Interoperability** in English and **供应商互通** in Chinese.

The implementation must convert the verified CC Switch repair experience into a deterministic, reusable product workflow. Core repair logic belongs in Codex Home Manager. REST and the existing MCP endpoint expose the same operations to agents. A bundled skill may orchestrate these product APIs, but it must not duplicate or bypass the repair engine.

## 2. Outcomes

The feature is successful when a user can:

1. Identify the real active `CODEX_HOME`, current request route, authentication mode, history bucket, and external provider manager state.
2. See whether official and third-party configurations share one history bucket without confusing that bucket with the route that consumes quota.
3. Preview and repair historical provider-bucket drift in rollout JSONL and `state_5.sqlite`.
4. Preview and repair shared plugin and marketplace configuration while leaving provider-scoped settings isolated.
5. Preserve the OpenAI Official login cache while switching through CC Switch when the installed CC Switch version supports that contract.
6. Verify a switch using local configuration evidence and, only after explicit opt-in, a live request probe that may consume provider quota.
7. Roll back every applied interoperability repair from a sealed backup manifest.
8. Diagnose individual tasks that cannot continue across providers without rewriting all history during normal switching.

## 3. Non-Goals

The first release does not:

- replace CC Switch or become a general-purpose provider catalog editor;
- store, display, export, or synchronize full provider tokens;
- infer billing from `model_provider` alone;
- merge official and third-party authentication material;
- rewrite reasoning ciphertext or attempt to decrypt `encrypted_content`;
- automatically rewrite incompatible task response IDs during every switch;
- silently stop or restart Codex Desktop, Codex CLI, CC Switch, or other Codex hosts;
- support unrecognized external-manager schemas in write mode;
- run a second standalone MCP service;
- introduce a generic plugin synchronization framework unrelated to Codex provider interoperability.

## 4. Verified Domain Model

Provider interoperability has three independent layers.

### 4.1 History Identity

Codex task visibility is filtered by the persisted `model_provider` value in rollout `session_meta` records and the `threads.model_provider` column in `state_5.sqlite`.

For the validated CC Switch workflow, both official and third-party configurations use the stable `custom` history bucket. Historical official records with the `openai` bucket are migrated to `custom`. This value is a visibility identity, not proof of the network route or quota owner.

### 4.2 Request Route And Authentication

The effective request route is derived from the active configuration and authentication contract:

- provider definition selected by top-level `model_provider`;
- provider `base_url` and wire API;
- provider-scoped bearer token presence;
- `requires_openai_auth` behavior;
- official login cache presence and status;
- active provider recorded by a supported external manager;
- optional observed request host from an explicit live probe.

OpenAI Official must not inherit a third-party `base_url` or provider token. Third-party providers must not overwrite the official login cache. A report may say that the route is confirmed only when the evidence is sufficient; otherwise it reports `unknown` or `configuration_only` instead of guessing.

### 4.3 Shared Capabilities

Plugins, marketplaces, and other provider-independent declarations belong in a shared common layer when an external manager supports one. Provider credentials, route definitions, model selection, project lists, history database state, and authentication files do not belong in that layer.

Codex Desktop dynamically generates privileged runtime configuration for features such as `node_repl` and Computer Use. Dynamic command paths, native-pipe environment variables, temporary runtime paths, and Desktop-managed `notify` entries must never be promoted into CC Switch common configuration. The shared-config classifier must recognize and exclude these values.

## 5. Architecture

The feature uses a Rust data plane and a Python control plane. It extends the Rust `codex-home-manager-service` already being introduced by the desktop work; it does not create another long-running service or a second implementation of the same domain rules.

Rust owns all performance-critical and byte-sensitive work:

- multi-gigabyte JSONL discovery, streaming audit, fingerprints, and rewrites;
- SQLite schema inspection, backup, read transactions, and write transactions;
- TOML round-trip parsing, classification, and deterministic merge planning;
- large backup copies, free-space accounting, operation journals, and post-write verification;
- CC Switch schema fingerprints and guarded database operations.

Python retains the established product control plane:

- loopback authorization and short-lived tokens;
- state-bound preview ticket creation and validation;
- REST/OpenAPI and MCP dispatch;
- process policy, user acknowledgments, and error translation;
- integration with the existing backup catalog and sealed manifests;
- orchestration of the Rust operation and publication of redacted results.

```text
React Provider Interoperability module
                |
        REST and existing MCP
                |
 Python provider_interop control plane
        | authorization, preview, policy
        |
 Existing codex-home-manager-service binary
        | provider-interop worker protocol
        |
 Rust provider_interop core
        |-- native Codex and CC Switch adapters
        |-- history and common-config engines
        |-- transaction journal and verifier
```

The Python control-plane package contains:

- `environment.py`: resolves the selected and effective Codex Home and detects relevant running processes.
- `models.py`: typed internal records, status enums, preview plans, evidence, and public response builders.
- `redaction.py`: recursively redacts secrets before data crosses a logging, API, MCP, or manifest boundary.
- `routing.py`: determines configuration-derived route identity and handles optional live-probe evidence.
- `worker.py`: invokes the packaged Rust executable through the private worker protocol and validates its schema and result binding.
- `transaction.py`: coordinates authorization, preflight, sealed backup manifests, worker execution, and rollback reporting.
- `service.py`: exposes status, preview, apply, verify, task audit, and rollback use cases.

The Rust core is placed under the existing native-service workspace and contains focused modules:

- `provider_interop/mod.rs`: versioned request/result protocol and public core entry points.
- `provider_interop/history.rs`: memory-bounded JSONL scan, fingerprint, rewrite, and verification.
- `provider_interop/config.rs`: `toml_edit`-based classification and round-trip merge.
- `provider_interop/sqlite.rs`: read-only inspection, SQLite backup API, transactions, and quick checks.
- `provider_interop/transaction.rs`: operation journal, file preconditions, apply stages, and rollback.
- `provider_interop/redaction.rs`: defense-in-depth secret detection for worker output.
- `provider_interop/adapters/mod.rs`: versioned adapter protocol.
- `provider_interop/adapters/native_codex.rs`: native Codex configuration and state adapter.
- `provider_interop/adapters/cc_switch.rs`: capability-probed CC Switch adapter.

The Rust core is a library module used by the existing service binary. The same binary accepts a private `provider-interop-worker` subcommand for the Python compatibility service. Requests are JSON on standard input and results are JSON on standard output; secrets and progress never appear in command-line arguments. The worker is short-lived, inherits no network capability requirement, and exits after one operation. This is not a second daemon.

The worker request includes protocol version, operation ID, selected roots, state digest, stage, and a random invocation nonce. The result echoes those bindings. Python rejects mismatched, extra, truncated, or unsupported results. Apply requests are accepted only after Python has validated the local token and preview ticket.

The control plane depends on narrow existing utilities for Codex path resolution, authorization, process inspection, and sealed backup catalogs. The Rust core owns the byte-sensitive primitives and exposes stable typed results. Neither side reimplements the other's responsibility.

## 6. Adapter Contract

The Rust external-provider-manager adapter exposes:

- `detect()`: installation path, version, state root, database path, settings path, and capability set;
- `inspect(codex_home)`: current provider, common configuration, provider summaries, schema fingerprint, and interoperability settings;
- `plan_repair(request)`: deterministic proposed changes without writing;
- `backup_targets(plan)`: every external file or database that must be preserved;
- `apply(plan)`: guarded external-manager updates;
- `verify(plan)`: post-apply schema, setting, and configuration checks.

Adapters return normalized data and redacted evidence. They never return raw credentials to the API or UI.

### 6.1 CC Switch Detection

The Windows adapter checks, in order:

1. an explicit request path when provided;
2. the running CC Switch executable path and adjacent application metadata;
3. the per-user uninstall registry entry;
4. the conventional per-user state directory.

The adapter treats `settings.json` as the user-facing setting source and `cc-switch.db` as structured provider/common-config state. It validates:

- the installed version;
- SQLite `PRAGMA user_version`;
- `PRAGMA quick_check`;
- required tables and columns;
- expected setting and provider value shapes;
- binding between CC Switch's `codexConfigDir` and the selected Codex Home.

The first supported contract is CC Switch 3.19.2 with database user version 16 and the probed schema used by that release. Later releases are added as explicit schema profiles. A schema profile is selected by capabilities and schema fingerprint, not version text alone.

If the schema is unknown or partially compatible, inspection remains available but write operations return `unsupported_adapter_schema`. The product must not attempt a best-effort database mutation.

### 6.2 Independent Operation Without CC Switch

When CC Switch is absent, native inspection, history-bucket audit, route inspection, task compatibility audit, backup, and verification remain available. Shared external-manager configuration repair and provider switching are marked unavailable. Home Manager does not create its own hidden provider-switch database in the first release.

## 7. Status And Evidence

`GET /api/provider-interoperability/status` returns a redacted status document with:

- selected and effective Codex Home paths and confidence;
- detected Codex and CC Switch processes;
- adapter kind, version, schema support, and bound Codex Home;
- current provider display name and opaque stable ID hash;
- history-bucket distribution from SQLite and rollout metadata;
- configured route classification: `official`, `third_party`, `local`, `unknown`;
- route confidence: `observed`, `configuration_only`, `conflicting`, `unknown`;
- authentication classification without token values;
- official login cache presence and freshness status when safely observable;
- plugin/common-config consistency summary;
- interoperability setting states;
- blocking conditions, warnings, and recommended actions.

Evidence entries contain a type, status, source category, redacted summary, timestamp, and optional local path. They do not contain raw configuration documents because those may hold secrets.

Status is read-only and safe while Codex and CC Switch are running. SQLite inspection uses read-only connections and short timeouts. Expensive full rollout scans are not performed on routine page load; the status endpoint uses SQLite plus bounded metadata sampling and exposes whether a full audit is required.

## 8. Repair Scope

The repair request has explicit scopes:

- `history`: migrate eligible history identities to the selected shared bucket;
- `common_config`: merge safe provider-independent declarations into CC Switch common configuration and synchronize the active Codex config;
- `settings`: enable supported interoperability settings and bind CC Switch to the selected Codex Home;
- `all`: apply all supported scopes.

The default shared history bucket for the CC Switch adapter is `custom`. Callers cannot provide arbitrary bucket names in the first release.

### 8.1 Eligibility

History migration changes only records whose source bucket is in the adapter profile's explicit eligible set. For the validated official unification operation, that set is exactly `openai`. Existing `custom` records remain unchanged. Unknown provider identities are reported and left untouched.

Common configuration repair changes only declarations classified as provider-independent. It never copies an entire official provider snapshot into common configuration.

Settings repair can enable:

- `preserveCodexOfficialAuthOnSwitch`;
- `unifyCodexSessionHistory`;
- `unifyCodexMigrateExisting` when history migration is selected;
- `codexConfigDir` binding to the selected Codex Home.

The existing setting state is shown in preview. Disabling interoperability or restoring original history ownership is a separate rollback operation, not an implicit side effect of another repair.

## 9. History Audit And Migration Contract

### 9.1 Full Audit

A history repair preview performs a memory-bounded Rust streaming scan of active and archived rollout JSONL files and reads every relevant SQLite thread row. It records:

- files and rows by provider bucket;
- parse errors and missing rollout paths;
- eligible change count;
- prompt sequence fingerprint;
- protected-content fingerprint;
- exact source file size, modification time, and SHA-256 for changed files;
- required backup bytes;
- Codex-running blockers.

The prompt fingerprint covers top-level user messages and supported legacy user-message forms in original order, using the existing length-prefixed SHA-256 contract.

The protected-content fingerprint covers every `encrypted_content` value, reasoning payload, non-target response item, tool output, and message content in changed files. It is used to prove that migration changed only allowed provider fields.

### 9.2 JSONL Rewrite

The Rust rewriter:

1. reads files as bytes and splits them without changing line endings;
2. uses `memchr` to locate candidate metadata lines and parses only candidates structurally with `serde_json`;
3. confirms the thread/session identity and exact eligible source bucket;
4. performs an equal-length byte substitution for `openai` to `custom` within the confirmed `model_provider` string;
5. writes a sibling temporary file through buffered sequential I/O while computing source and output hashes in one pass;
6. parses every output line and compares semantic contracts;
7. replaces the source only if the audited source stat and SHA-256 still match.

If a future migration requires unequal-length values, it needs a new adapter profile and an explicit serialization-preservation design. The first release rejects it.

### 9.3 SQLite Rewrite

The transaction:

- creates a SQLite backup through the backup API;
- opens the live database only after all process guards pass;
- verifies the bound input state;
- starts an immediate transaction;
- updates only eligible `threads.model_provider` rows;
- checks affected-row counts;
- commits and closes the connection;
- runs `PRAGMA quick_check` on the resulting database;
- verifies the database file can be reopened and no file handle remains leaked.

### 9.4 Preservation Gates

Apply fails and rolls back if any of these change unexpectedly:

- prompt count, order, or SHA-256;
- protected-content fingerprint;
- JSONL line count or parseability;
- non-target JSONL bytes;
- expected SQLite row count;
- SQLite quick check;
- source-to-backup hash binding.

The normal history repair never removes, rewrites, decrypts, or re-encrypts reasoning and `encrypted_content`.

## 10. Common Configuration Contract

### 10.1 Shared Declarations

The first classifier supports these provider-independent categories when present and valid:

- plugin declarations and enabled state;
- marketplace declarations and local marketplace source paths;
- stable user-installed MCP declarations that do not contain provider credentials;
- stable shared feature flags verified to be independent of the active provider.

The classifier uses Rust `toml_edit` round-trip parsing. It produces a field-level decision list with `shared`, `provider_scoped`, `desktop_dynamic`, `secret`, or `unknown` classification.

Unknown declarations are not copied automatically.

### 10.2 Excluded Declarations

The following remain provider scoped or unmanaged:

- `model`, `model_provider`, `model_providers`, profiles, and model catalogs;
- `base_url`, wire API, request headers, bearer tokens, API keys, and environment credentials;
- `requires_openai_auth` and official authentication state;
- project lists and workspace bindings;
- history databases and session indexes;
- CC Switch proxy and failover state;
- Desktop-generated `node_repl`, native-pipe, temporary runtime, dynamic CLI, and managed `notify` configuration;
- unrecognized fields.

### 10.3 Merge And Verification

The merge preserves comments and stable ordering through `toml_edit`. It updates CC Switch's common layer and then derives the expected active Codex configuration using the adapter's merge contract. It does not make provider `settings_config` snapshots identical.

Post-apply verification checks:

- common and live shared declarations are semantically equal;
- every provider-scoped snapshot hash is unchanged unless an explicit adapter setting change requires otherwise;
- no secret migrated into common configuration;
- official and third-party route definitions remain distinct;
- Windows-incompatible Apple build plugins remain disabled;
- Desktop dynamic runtime entries were not captured or overwritten;
- plugin registry and required cache paths agree where the local Codex CLI supports registry inspection.

## 11. Transaction And Backup Model

All mutations use the existing state-bound preview pattern.

### 11.1 Preview

`POST /api/provider-interoperability/repair/preview` returns:

- `operationPreviewId`, `inputHash`, `stateDigest`, and expiry;
- adapter and schema binding;
- selected scopes and exact planned actions;
- files, databases, and settings affected;
- eligible JSONL and SQLite counts;
- estimated backup bytes and destination root;
- process shutdown requirements;
- live-probe requirement, if any;
- warnings and blockers.

The input hash includes the selected Codex Home, adapter path, schema fingerprint, scopes, target bucket, and relevant source state fingerprints.

### 11.2 Apply

Apply requires:

- a short-lived local API token;
- matching preview ID and input hash;
- unchanged state digest;
- explicit acknowledgment of every required shutdown;
- Codex Desktop, Codex CLI/app-server, and CC Switch fully stopped when their state is written.

Home Manager never stops those processes automatically. The UI explains why shutdown is required and asks the user to close them. A later desktop enhancement may offer an explicit supervised shutdown, but it is outside this feature.

The transaction order is coordinated by Python and executed through a Rust operation journal:

1. repeat preflight and process guard;
2. create a pending sealed-backup record and a worker operation journal;
3. copy target files with buffered Rust I/O and back up SQLite databases through `rusqlite`'s backup API;
4. apply external-manager settings and common configuration;
5. migrate JSONL history;
6. migrate SQLite history rows;
7. synchronize the live Codex shared configuration;
8. run Rust offline verification and finalize the worker journal;
9. validate the worker result in Python, seal the backup/result manifest, and commit the operation.

Any failure after backup triggers automatic restoration of already-mutated targets. A worker crash is recoverable because each stage is flushed to the journal before mutation and records whether replacement completed. On the next inspection, Python invokes Rust recovery against the sealed pre-images before accepting another repair. The response distinguishes `failed_rolled_back` from `failed_manual_recovery_required`.

### 11.3 Backup Manifest

Backups use `CODEX_HOME_MANAGER_BACKUP_ROOT`. The reference machine is configured to use `D:\Backup\codex_home_manager`. A general packaged installation without that configuration uses the existing controlled per-user backup root and never assumes that a `D:` drive exists.

The sealed manifest records:

- schema version and operation ID;
- Codex Home binding hash;
- adapter type, version, and schema fingerprint;
- source and backup paths;
- file sizes and SHA-256 values;
- SQLite quick-check results;
- planned and applied action counts;
- redacted before/after summaries;
- preservation-contract results;
- rollback instructions.

Secrets are not copied into the manifest. Source configuration backups may contain credentials and therefore remain local, inherit restrictive permissions where supported, and cannot be exported through portable backup without a future credential-aware design.

### 11.4 Rollback

Rollback uses a dedicated preview and apply operation bound to one interoperability backup. It validates the sealed manifest and all backup hashes, creates a pre-rollback backup of current state, requires the same process guards, restores files and databases, runs quick checks, and reports the resulting status.

It never reconstructs old state by applying a reverse text transformation when an original backup exists.

## 12. Verification

### 12.1 Offline Verification

Offline verification checks:

- configuration parseability and route separation;
- CC Switch schema and setting state;
- history-bucket distributions;
- JSONL parseability and preservation contracts;
- SQLite quick check;
- shared plugin/common configuration equivalence;
- official-auth cache preservation by hash and metadata, without exposing content;
- absence of secrets in reports and common configuration.

### 12.2 Restart Verification

After the user restarts applications, verification checks:

- the actual `CODEX_HOME` used by the running process;
- the live `config.toml` and current external-manager provider;
- whether the same sampled ordered task IDs are visible under each provider configuration;
- whether representative migrated and native tasks remain readable;
- plugin registry and skill-path availability;
- route classification and conflicts.

The product cannot automatically switch back and forth through CC Switch unless CC Switch later provides a stable public automation API. In the first release, the UI gives a two-step verification checklist and stores redacted snapshots before and after the user switches.

### 12.3 Live Route Probe

A live route probe is optional and separate from repair. The user must select a provider and acknowledge that the probe can consume quota and transmit a fixed harmless prompt to that provider.

The probe records only:

- selected provider identity hash;
- observed destination host classification;
- HTTP success or categorized failure;
- model name when returned;
- request time and usage metadata when available;
- redacted response checksum.

It never stores request authorization headers or full response bodies. Configuration-only evidence cannot be labeled as observed routing.

## 13. Task Compatibility Doctor

Cross-provider visibility and cross-provider continuation are separate checks.

The task audit reports:

- history bucket and visibility risk;
- non-portable encrypted reasoning records;
- incompatible proxy `item_*` IDs;
- latest valid compaction checkpoint;
- prompt preservation baseline;
- recommended action.

Normal interoperability repair does not mutate task response records. Advanced repair is a separate, task-scoped preview/apply workflow that reuses the existing thread-history repair engine and requires a validated checkpoint or another explicit compatibility strategy. It must back up the task and preserve all user prompts.

The UI must explain that unreadable provider-specific reasoning can prevent continuation without deleting or hiding earlier visible conversation.

## 14. API And MCP Surface

### 14.1 REST

- `GET /api/provider-interoperability/status`
- `POST /api/provider-interoperability/audit`
- `POST /api/provider-interoperability/repair/preview`
- `POST /api/provider-interoperability/repair`
- `POST /api/provider-interoperability/verify`
- `POST /api/provider-interoperability/backups/{backup_id}/rollback/preview`
- `POST /api/provider-interoperability/backups/{backup_id}/rollback`
- `GET /api/provider-interoperability/tasks/{thread_id}/audit`

Status and task audit are read-only. A full audit may be long-running and returns progress-compatible operation metadata. Repair and rollback use authorization plus preview binding. Verify is read-only unless the request explicitly enables a live route probe; a live probe additionally requires local authorization and quota acknowledgment.

### 14.2 MCP

The existing `/mcp` endpoint exposes:

- `codex_provider_interoperability_status`
- `codex_provider_interoperability_audit`
- `codex_preview_provider_interoperability_repair`
- `codex_repair_provider_interoperability`
- `codex_verify_provider_interoperability`
- `codex_preview_provider_interoperability_rollback`
- `codex_rollback_provider_interoperability`
- `codex_audit_provider_interoperability_task`

MCP tools enforce the same authorization, preview, process, and redaction contracts as REST. MCP is an access surface, not a second implementation.

### 14.3 Bundled Skill

The repository includes a distributable `codex-provider-interoperability` skill. Its instructions require agents to:

1. call status before proposing changes;
2. use preview before every repair or rollback;
3. never edit CC Switch databases, Codex history, or common config directly when the product API is available;
4. explain shutdown and quota implications before requesting acknowledgment;
5. verify after apply;
6. escalate unknown adapter schemas instead of bypassing the guard;
7. preserve reasoning and encrypted content;
8. report the backup ID and verification status.

The skill is packaged with Home Manager and may be installed into the user's selected Codex Home through the existing resource-management workflow. It contains no credentials, machine-specific paths, or duplicated migration code.

## 15. User Interface

Add a `供应商互通` / `Providers` navigation item using a suitable Lucide route or refresh icon.

The page is an operational console, not a setup landing page. It contains:

- a compact status band for route, history, authentication, plugins, and adapter support;
- an evidence panel showing the active Codex Home, current provider, route confidence, and external-manager binding;
- repair-scope checkboxes for history, common config, and interoperability settings;
- primary commands for scan, preview repair, apply, verify, and rollback;
- a change review dialog showing exact counts, backup size, blockers, and acknowledgments;
- a task compatibility lookup for one task ID;
- recent interoperability backups and their verification state.

Secret fields are never rendered, even masked as recoverable values. The UI may show `present`, `absent`, `official_cache`, or a short non-reversible fingerprint.

Browser-folder mode remains read-only. It may show native Codex history status but cannot inspect CC Switch state outside the user-selected folder and cannot offer repair.

## 16. Error Handling

Errors use stable machine-readable codes. Required codes include:

- `codex_home_mismatch`
- `adapter_not_found`
- `unsupported_adapter_schema`
- `external_manager_running`
- `codex_processes_running`
- `preview_expired`
- `state_changed_since_preview`
- `insufficient_backup_space`
- `jsonl_parse_error`
- `history_preservation_failed`
- `sqlite_integrity_failed`
- `common_config_classification_blocked`
- `secret_redaction_failed`
- `route_evidence_conflict`
- `live_probe_not_acknowledged`
- `failed_rolled_back`
- `failed_manual_recovery_required`

Errors identify the failed stage, whether any mutation occurred, rollback status, backup ID, and safe next action. They do not include secrets or complete configuration values.

## 17. Security Requirements

- All writes remain loopback-only and require the existing short-lived local token.
- Preview tickets are single-use and bound to state.
- CC Switch and Codex database paths must resolve inside explicitly detected state roots.
- No SQL table or column identifier comes from request input.
- SQLite connections use read-only URI mode for inspection and parameterized statements for writes.
- Symlinks and junctions are resolved and containment-checked before backup or replacement.
- External-manager schema fingerprinting occurs before every write.
- Token-like data is redacted recursively before logging, serialization, or MCP output.
- Worker requests never place credentials or configuration text in process arguments or environment variables.
- The Rust worker does not initiate network requests; the optional route probe remains in the authorized control plane.
- Worker protocol versions, operation IDs, nonces, roots, and state digests must match before a result is trusted.
- Test fixtures use synthetic secrets and include leak-detection assertions.
- Live probe hosts must match the selected active provider configuration; callers cannot supply arbitrary URLs.
- Repair reports must remain useful without containing raw `auth.json`, bearer tokens, request headers, or complete provider configurations.

## 18. Test Strategy

### 18.1 Unit Tests

- Rust unit and property tests for JSONL byte selection, equal-length replacement, hashes, TOML classification, SQLite operations, journals, and redaction;
- environment resolution with multiple Codex Homes;
- CC Switch detection and supported/unsupported schema profiles;
- route classification for official, third-party, conflicting, and unknown states;
- TOML classification and exclusion of secrets and Desktop dynamic runtime entries;
- equal-length history rewrite and refusal of unsupported substitutions;
- prompt and protected-content fingerprint preservation;
- recursive redaction and leak scanning;
- preview/state binding and error codes.

### 18.2 Integration Tests

Use isolated synthetic Codex Homes and CC Switch state roots to test:

- mixed `openai`, `custom`, and unknown history buckets;
- active and archived sessions;
- malformed JSONL and SQLite integrity failures;
- successful repair, mid-transaction failure, automatic rollback, and explicit rollback;
- provider snapshot hash preservation;
- common/live plugin equivalence;
- unknown schema read-only behavior;
- process guard refusal;
- backup-manifest integrity and restore.

Tests must verify that non-target JSONL bytes, prompt sequence hashes, `encrypted_content`, reasoning, tool output, and provider-scoped route configuration remain unchanged.

Python and Rust must run the same golden fixtures. Parity tests compare normalized plans, preservation fingerprints, error codes, and redacted public results. Python does not provide a production fallback implementation when the Rust worker is missing or incompatible; write operations fail closed with `native_worker_unavailable`.

### 18.3 Scale And Performance Tests

- stream multi-gigabyte fixture generation without holding the full file in memory;
- bound memory during audit and rewrite;
- benchmark Rust against the existing Python audit primitives on the same 10 GiB parse-heavy fixture; native audit must have median wall time no more than 65% of the Python baseline across three warm-cache runs and private memory no greater than 256 MiB;
- on a cold, disk-bound 10 GiB rewrite, require the Rust path to remain within 5% of measured sequential-copy throughput while keeping private memory no greater than 256 MiB;
- require a single sequential source read for candidate discovery, protected-content fingerprints, and SHA-256 where possible;
- verify progress reporting and cancellation;
- prove that routine status avoids a full history scan;
- simulate concurrent source growth and require the apply operation to stop before replacement.

### 18.4 API, MCP, And UI Tests

- REST and MCP parity for all new operations;
- authorization and preview requirements;
- no-secret response snapshots;
- Chinese and English UI strings;
- keyboard access, focus management, responsive layouts, and text overflow;
- browser-folder read-only behavior;
- Playwright flows for supported, unsupported, blocked, repaired, verified, and rolled-back states.

### 18.5 Live Validation

Live validation on the reference Windows machine is staged:

1. status and full audit only;
2. dry-run against a copied synthetic state root;
3. offline repair against a disposable copied Codex Home;
4. packaged connector and desktop UI verification;
5. user-approved live machine apply only if a real defect remains;
6. user-driven CC Switch round trip and task/plugin visibility checks;
7. optional user-approved route probes for official and third-party providers.

No test automatically stops the user's running Codex or CC Switch processes.

## 19. Delivery And Compatibility

Implementation lands in small commits that do not include unrelated work from the active desktop branch. The new domain package and tests are introduced before wiring the existing large server and frontend entry files.

The implementation reuses the active `codex-home-manager-service` Rust workspace, `axum` service shell, `rusqlite`, `memmap2`, `memchr`, `serde_json`, and `sha2`. It adds `toml_edit` and only other narrowly justified crates with locked versions. The packaged local connector and desktop application include the same Rust core; source-mode development can build it with the existing Cargo workspace. There is no Python implementation of the large-file mutation path and no separate interoperability executable to install or supervise.

Delivery is divided into five gated slices:

1. Rust protocol, golden fixtures, environment resolution, native/CC Switch read-only adapters, redaction, and status.
2. Rust full audit, provider-bucket planning, prompt/protected-content contracts, benchmarks, and cancellation.
3. Sealed backup integration, journaled history/common-config/settings repair, fault injection, and rollback.
4. REST/OpenAPI/MCP exposure, Provider Interoperability UI, task compatibility audit, and the bundled skill.
5. Packaged connector, Tauri desktop, public site, source publication, and user-driven live round-trip verification.

Each slice must pass its own unit, contract, security, and parity gates before the next slice begins. Mutation routes remain feature-disabled until slices 1 through 3 pass together. Public release waits for all five slices.

Release work includes:

- source repository export and clean-room verification;
- local connector packaging;
- desktop package integration;
- public site synchronization;
- Cloudflare Pages deployment;
- release manifest, signature, artifact hash, and download verification;
- documentation of the supported CC Switch schema profile;
- bundled skill inclusion and installation verification.

If the external manager changes its schema, existing releases keep inspection where safe and disable mutations until a new tested adapter profile ships.

## 20. Acceptance Criteria

The feature is complete when all of the following pass:

1. A mixed synthetic history is unified with only allowed `model_provider` bytes and SQLite fields changed.
2. Prompt sequence and protected-content fingerprints are identical before and after repair.
3. Shared plugins survive both official and third-party CC Switch configurations while provider route and authentication snapshots remain distinct.
4. Unknown CC Switch schemas are inspectable and cannot be mutated.
5. Every repair produces a sealed local backup and can be rolled back through preview-bound REST, MCP, and UI workflows.
6. Route reporting distinguishes observed routing from configuration inference and never derives billing from the history bucket.
7. Task compatibility warnings distinguish visibility from continuation and do not alter reasoning during normal repair.
8. API, MCP, UI, security, scale, packaging, and source-publication gates pass.
9. Packaged Windows users can perform the workflow without editing files or running commands manually.
10. No credential appears in logs, reports, fixtures, manifests, MCP output, or UI snapshots.
11. The Rust audit meets the 65%-of-Python median wall-time threshold, the disk-bound rewrite stays within 5% of sequential-copy throughput, and both remain within the 256 MiB private-memory limit on the specified 10 GiB fixtures.
