# Security Boundary

Codex Home Manager is local-first by design. The complete product is open source on the default `source` branch. The `main` branch contains the hosted static frontend, public release downloads, deployment files, and public safety checks.

Windows Authenticode is optional evidence, not a fallback trust root. A release may claim valid Authenticode only when Windows validates an existing trusted code-signing certificate. Releases without such a certificate explicitly report Authenticode as unavailable and still require the detached Ed25519 manifest signature plus an independently pinned public-key fingerprint. Self-signed certificates are not accepted as publisher trust.

The hosted frontend can operate in a read-only browser folder mode when a user explicitly selects a local `.codex` directory. Full write-capable management requires the local connector running on the user's own machine.

## Deployment and data boundary

The local connector implementation is public on `source` but intentionally excluded from the deployed browser bundle. Do not add any of the following to either branch:

- Real `state_5.sqlite`, `logs_2.sqlite`, `session_index.jsonl`, rollout JSONL, exports, backups, or memory files.
- Real screenshots containing conversation titles, project paths, machine usernames, account names, prompts, tokens, or logs.
- Credentials, access tokens, private release-signing keys, or unredacted diagnostics snapshots.

## Public screenshots and demo data

Only synthetic or anonymized data may be used in this repository. If a screenshot is needed, remove or replace real conversation titles, project paths, machine usernames, account names, prompts, tokens, and logs before committing it.

## Reporting

Use GitHub private vulnerability reporting for security issues. Public issues must not include local Codex Home files, credentials, real conversation content, or machine identifiers.
