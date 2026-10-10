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
- The Ed25519 private release key and its independent trust record remain in the publisher's ignored `.codex/release-signing` directory, outside the tracked and exported source tree. This is persistent signing state, not a disposable backup or build artifact.
- Authenticode is optional. Without a publicly trusted code-signing certificate, release metadata must report Windows `NotSigned`; it must not claim an Authenticode signature. Release authenticity still requires the detached Ed25519 manifest signature and independently pinned public-key fingerprint.

## Release-key rotation: 2026-10-10

The private key for the previous release trust anchor is unavailable. Its former location was under a disposable backup directory that was recycled; the corresponding recovery payload is no longer present. This is a publisher storage/protection failure, not a user configuration error. There is no evidence here that establishes key compromise, and the new key is not cross-signed by the unavailable old key.

- Historical release anchor: `sha256:ef7194fbc8fa8550430c908d9d02c74f7fc0d1e87f7f9b4ec5a164526b48f208`.
- New release anchor, starting with the forthcoming 1.0.34 publication: `sha256:ac9c44ea419ded2b51226e77444b11b2a28962b284b4789fbf282ae05c70607f`.
- Existing release assets, signatures and historical verifiers are not rewritten. Use their original anchor to verify those releases. A new verifier must reject a manifest signed only by the historical key; merely downloading a different public key from the same website does not change its embedded trust.
- This record announces a trust-anchor change; it does not itself prove that 1.0.34 has been published. The release's manifest, detached signature, verifier and independently checked download must all be available before publication is complete.

Signing keys and signing directories are excluded from cleanup candidates even if an inventory incorrectly labels their parent as disposable. Ordinary packaging fails before compilation when required signing material is missing; creating a replacement key is a separate, explicit publisher operation.
