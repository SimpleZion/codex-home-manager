# Contributing

## Development

Use Node.js 22 or newer, Python 3.12, and PowerShell 7 on Windows.

```powershell
npm ci
python -m pip install --require-hashes --only-binary=:all: -r packaging/windows/requirements-connector.txt
npm run gate
```

Keep changes scoped and add tests proportional to the state or security surface being changed. UI changes must be verified in a real browser at desktop and mobile sizes.

## State-changing code

Code that writes Codex Home must preserve these invariants:

- short-lived local authorization;
- state-bound preview tickets;
- explicit running-Codex acknowledgement;
- optional, verifiable backups;
- offline-only execution for SQLite, global state, plugin configuration, and workspace-binding repairs;
- no daemon, startup entry, or persistent watchdog as a repair mechanism.

## Pull requests

Run `npm run gate`, `git diff --check`, and a secret scan before opening a pull request. Never include real conversation text, local databases, credentials, private keys, or personal paths.
