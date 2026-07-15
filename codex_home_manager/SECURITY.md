# Security Policy

## Supported versions

Security fixes target the latest published release and the current `source` branch.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose local Codex data, bypass loopback authorization, modify Codex state without a valid preview, or compromise release signing. Use GitHub's private vulnerability reporting for `SimpleZion/codex-home-manager` and include:

- the affected version or commit;
- the operating system and Codex version;
- a minimal reproduction without real conversation content or credentials;
- the expected and observed security boundary.

Non-sensitive defects can use the public issue tracker.

## Trust boundaries

- The complete product source is public on the `source` branch.
- The `main` branch is a deployment boundary containing only the hosted static site, public checks, and signed release artifacts.
- The hosted page cannot silently scan a visitor's machine. Local access requires an explicit browser folder selection or the loopback connector.
- Never commit real Codex Home data, rollout JSONL, SQLite databases, access tokens, private signing keys, diagnostics snapshots, or unredacted screenshots.
- The Ed25519 private release key and its independent trust record must remain outside the repository.
