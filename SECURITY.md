# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| Latest published release | Yes |
| Current `source` branch | Yes |
| Older releases | No |

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose local Codex data, bypass loopback authorization, modify Codex state without a valid preview, or compromise release signing. Use [GitHub private vulnerability reporting](https://github.com/SimpleZion/codex-home-manager/security/advisories/new) and include:

- the affected version or commit;
- the operating system and Codex version;
- a minimal reproduction without real conversation content or credentials;
- the expected and observed security boundary.

Do not include real conversation content, credentials, access tokens, signing material, or unredacted local paths. Expect an acknowledgement within 3 business days and an initial triage decision within 7 business days. Coordinated disclosure timing is agreed after impact and remediation are understood.

Non-sensitive defects can use the public issue tracker. The project does not offer a public bug bounty or guarantee rewards.

## Trust boundaries

- The complete product source is public on the `source` branch.
- The `main` branch is a deployment boundary containing only the hosted static site, public checks, and signed release artifacts.
- The hosted page cannot silently scan a visitor's machine. Local access requires an explicit browser folder selection or the loopback connector.
- Never commit real Codex Home data, rollout JSONL, SQLite databases, access tokens, private signing keys, diagnostics snapshots, or unredacted screenshots.
- The Ed25519 private release key and its independent trust record must remain outside the repository.
- Authenticode is optional. Without a publicly trusted code-signing certificate, release metadata must report Windows `NotSigned`; it must not claim an Authenticode signature. Release authenticity still requires the detached Ed25519 manifest signature and independently pinned public-key fingerprint.
