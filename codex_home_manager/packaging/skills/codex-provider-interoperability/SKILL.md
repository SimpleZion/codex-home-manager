---
name: codex-provider-interoperability
description: Audit and repair interoperability between OpenAI Official and third-party Codex providers. Use when provider switching hides tasks or plugins, continuation fails with invalid_id_prefix or foreign encrypted reasoning, CC Switch and Codex Home disagree, or routing and quota ownership must be verified without conflating them with the shared history bucket.
---

# Codex provider interoperability

Use Codex Home Manager's provider-interoperability REST or MCP surface. Do not directly rewrite JSONL, SQLite, CC Switch snapshots, credentials, or `encrypted_content`.

## Diagnose

1. Read provider status. Treat `model_provider = "custom"` only as the shared history identity.
2. Classify the actual route from endpoint, token presence, `requires_openai_auth`, adapter state, and—only with user permission—a live request. Never attribute quota from the history identity alone.
3. Audit the affected task when a task ID is known. Distinguish visibility compatibility from continuation compatibility.
4. For `invalid_id_prefix`, inspect only the latest compacted replacement history and records after it. Earlier records are inactive history and must remain byte-exact.
5. If tools disappear only after compaction, inspect the request transport contract. An explicit third-party `base_url` must use public top-level `instructions` and `tools`; OpenAI-internal Responses Lite headers and `additional_tools` input items are not a portable third-party contract.
6. Compare every runtime activation source before declaring a patch active: the inherited user/process `CODEX_CLI_PATH`, `mcp_servers.node_repl.env.CODEX_CLI_PATH` in the live config, the runtime manifest, and the actual Desktop child process executable. A valid binary on disk is not proof that Desktop launched it.
7. An optional external rollout doctor may supplement diagnosis only when it is run read-only and the source path, size, modification time, and SHA-256 are unchanged afterward. Treat unsupported or unknown operational schemas as compatibility gaps, not proof of corruption. Do not use an external `HEALTHY` result as proof that provider switching, type-specific response-item IDs, or post-compaction tool transport are safe.

Read [references/compatibility-contract.md](references/compatibility-contract.md) before evaluating an unfamiliar error signature or adapter schema.

## Repair

1. Run a preview first and retain its operation ID, state digest, preview ticket, input hash, backup requirement, and blockers.
2. Prefer the task-scoped repair endpoint for one failing task. Use the global repair only for history identity migration, common plugin/config synchronization, or an explicitly requested fleet audit.
3. Require Codex and CC Switch to be fully closed before applying or rolling back. Do not close them without user permission.
4. Apply only the preview-bound transaction. Preserve exact pre-images under the configured backup root.
5. Verify SQLite integrity, the user-prompt fingerprint, active compatibility, provider-snapshot isolation, and the repaired task status after commit.
6. Do not issue a quota-consuming provider request unless the user explicitly permits it.
7. Treat an ID-only patched runtime as outdated. Provider switching is safe only when the runtime manifest also declares `portableToolsForExplicitBaseUrl = 1`.
8. Synchronize the live config and user-level `CODEX_CLI_PATH` in the same activation transaction. A mismatch is `runtime_activation_source_conflict` and requires a full Desktop restart before verification.
9. Never run an external salvage, rewrite, replay, or inferred-ID repair against the live rollout. If recovery is explicitly requested later, operate only on a verified copy and keep the original byte-exact.

## Fail closed

Stop without mutation when the adapter schema is unknown, the rollout changes after preview, an active foreign item type is unsupported, JSONL parsing fails, the backup cannot be synchronized, or protected prompt/content contracts differ. Report the exact blocker and keep the rollback manifest when one exists.
