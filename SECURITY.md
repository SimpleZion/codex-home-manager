# Security Boundary

Codex Home Manager is local-first by design. The public repository contains a static website and mock product preview only.

## Non-disclosure boundary

The private local engine is intentionally excluded from this repository. Do not add:

- Code that reads or writes a real `.codex` directory.
- Real `state_5.sqlite`, `logs_2.sqlite`, `session_index.jsonl`, rollout JSONL, exports, backups, or memory files.
- Real screenshots containing conversation titles, project paths, machine usernames, account names, prompts, tokens, or logs.
- Internal recovery scripts or one-off repair scripts.

## Public demo data

Only synthetic data may be used in this repository. If a screenshot is needed, generate it from the mock UI in `site/`, not from the private local product.

## Reporting

For security issues in the public website, open a GitHub issue. For the private local engine, do not publish repro data that includes local Codex Home files or real conversation content.
