# Compatibility contract

## Identity and routing

- Use `custom` as the shared task visibility identity for both official and third-party sessions.
- Keep official OAuth state, third-party tokens, endpoints, and billing identity provider-scoped.
- Do not use `model_provider`, task visibility, or the CC Switch selected label as proof of quota ownership.

## Active replay boundary

Codex reconstructs the next request from the latest valid `compacted.payload.replacement_history` plus subsequent rollout records. If no compacted record exists, the entire rollout is active.

For a foreign ID such as `item_...`:

- omit active `reasoning`, `compaction`, or `context_compaction` items whose opaque ciphertext belongs to another backend;
- remove only the invalid `id` from replayable message, function-call, or custom-tool-call items;
- preserve message content, `call_id`, function arguments, outputs, order, and line ending;
- reject unknown active item types;
- leave pre-compaction history byte-exact.

The OpenAI Responses API uses type-specific IDs such as `rs_`, `msg_`, and `fc_`. A generic non-empty prefix check is insufficient.

## Tool transport boundary

- Responses Lite is an OpenAI-internal transport contract. It sends tools through an `additional_tools` developer input item and marks the request with `x-openai-internal-codex-responses-lite`.
- An explicit third-party `base_url` must use the public Responses API shape with top-level `instructions` and `tools`, even when the selected model catalog entry advertises Responses Lite.
- The built-in OpenAI provider, an OpenAI actor-authorized route, and the shared official history bucket without an explicit `base_url` may retain Responses Lite.
- Validate the post-compaction continuation request itself. A healthy local shell or code-mode host does not prove that the model received tool declarations.

## Runtime activation boundary

- Treat the user/process `CODEX_CLI_PATH`, `mcp_servers.node_repl.env.CODEX_CLI_PATH` in the live config, the verified runtime manifest, and the actual Codex Desktop child-process executable as one activation contract.
- If the paths disagree, fail closed with `runtime_activation_source_conflict`; do not report the newer on-disk runtime as active.
- Runtime installation must update both persistent activation pointers. Verification occurs only after a full Desktop restart and must hash the executable of the newly spawned Desktop child process.

## External rollout diagnostics

- External rollout doctors are optional evidence collectors, not runtime compatibility authorities and not repair dependencies.
- Before a read-only doctor run, record the source path, byte length, modification time, and SHA-256. Require all four to remain identical afterward.
- Share only bounded counters, versions, exit codes, and sanitized classifications. Never export raw rollout or SQLite content, prompts, credentials, provider tokens, encrypted state, thread identifiers, or private paths.
- `UNKNOWN_OPERATIONAL_SCHEMA`, missing tool names, unmatched outputs, and unfinished calls can reflect an older parser encountering newer Codex event types. Correlate them with the active replay boundary before classifying a task as corrupt.
- A doctor that does not understand type-specific response-item prefixes cannot validate the `item_...` versus `rs_...` failure. A doctor that does not inspect outbound request serialization cannot validate post-compaction tool availability.
- Never run salvage, inferred-ID rewriting, provider-state replay, or other mutation against a live rollout. Recovery requires an explicitly authorized, byte-exact copy and separate verification.

## Transaction contract

- Bind preview and apply to path, size, modification time, source SHA-256, prompt fingerprint, adapter schema, nonce, and state digest.
- Store exact JSONL and SQLite pre-images before mutation; synchronize journal and manifest before commit.
- Roll back automatically on any post-backup failure.
- Never rewrite or delete source `encrypted_content`; when an active compatibility view omits foreign ciphertext, preserve the complete source in the transaction backup.
