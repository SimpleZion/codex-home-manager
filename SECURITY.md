# Security Boundary

Codex Home Manager is local-first by design. The public repository contains the hosted static frontend, public release downloads, deployment files, and public safety checks. It does not contain the private local engine.

The hosted frontend can operate in a read-only browser folder mode when a user explicitly selects a local `.codex` directory. Full write-capable management requires the local connector running on the user's own machine.

## Non-disclosure boundary

The private local engine is intentionally excluded from this repository. Do not add:

- Code that reads or writes a real `.codex` directory.
- Real `state_5.sqlite`, `logs_2.sqlite`, `session_index.jsonl`, rollout JSONL, exports, backups, or memory files.
- Real screenshots containing conversation titles, project paths, machine usernames, account names, prompts, tokens, or logs.
- Internal recovery scripts or one-off repair scripts.

## Public screenshots and demo data

Only synthetic or anonymized data may be used in this repository. If a screenshot is needed, remove or replace real conversation titles, project paths, machine usernames, account names, prompts, tokens, and logs before committing it.

## Reporting

For security issues in the public website, open a GitHub issue. For the private local engine, do not publish repro data that includes local Codex Home files or real conversation content.
