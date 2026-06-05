# Codex Home Manager

This repository contains only the hosted Codex Home Manager frontend and public release downloads.

The hosted page connects to a local connector running on the user's own machine at `http://127.0.0.1:8765`. The connector reads and manages the selected Codex Home directory. The hosted page itself does not contain the local engine source and does not upload `.codex` data.

![Codex Home Manager thread dashboard](site/assets/codex-home-manager-screenshot.png)

## What is included

- The static web frontend deployed on Cloudflare Pages.
- Public release downloads for the Windows local connector.
- A public API capability overview and safety boundary notes.
- Cloudflare Pages deployment files.

## What is intentionally not included

This repository does not contain the private local engine implementation that reads, repairs, migrates, slims, or writes a real Codex Home directory.

Excluded by design:

- Local SQLite and JSONL manipulation code.
- Real Codex Desktop session data, logs, exports, backups, or screenshots.
- Token handling, write-gate implementation, preview ticket validation, and restore internals.
- Any user-specific project paths, conversation titles, memory files, or machine identifiers.

The public repository is only the hosted frontend and distribution shell.

## Use the hosted product

Open:

<https://codex-home-manager.simplezion.com/>

On Windows, download and run the local connector:

<https://github.com/SimpleZion/codex-home-manager/releases/latest/download/codex-home-manager-local-win-x64.exe>

The connector starts a local API on `127.0.0.1:8765`, registers the `codex-home-manager://start` browser protocol for the current Windows user, and opens the hosted page.

If the browser or Windows warns that the executable is not commonly downloaded, verify the SHA256 checksum before running:

<https://github.com/SimpleZion/codex-home-manager/releases/latest/download/SHA256SUMS.txt>

## Local preview

Open `site/index.html` directly in a browser, or serve the directory with any static server:

```powershell
cd codex-home-manager-public
npx wrangler pages dev site
```

## Deployment

The production site is designed for Cloudflare Pages:

```powershell
npx wrangler pages deploy site --project-name codex-home-manager --branch main
```

Production custom domain: <https://codex-home-manager.simplezion.com/>.

## Privacy stance

The deployed frontend reads real Codex Home data only from the user's own local API. No real Codex Home content, session JSONL, SQLite database, logs, exports, backups, screenshots, or user-specific paths are committed.
