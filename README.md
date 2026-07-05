# Codex Home Manager

This repository contains only the hosted Codex Home Manager frontend and public release downloads.

The hosted page has two operating modes:

- Browser folder mode: the user manually selects a local `.codex` directory in a Chromium browser. The page can read thread JSONL, resources, logs, diagnostics inputs, and prompt exports through the browser File System Access API. This mode is read-only.
- Local connector mode: the user runs the Windows connector on their own machine at `http://127.0.0.1:8765`. The connector enables the full local management surface, including repairs, migration, deletion, slimming, MCP, process checks, and guarded writes.

The hosted page itself does not contain the private local engine source and does not upload `.codex` data.

![Codex Home Manager thread dashboard](site/assets/codex-home-manager-screenshot.png)

Diagnostics view:

![Codex Home Manager diagnostics](site/assets/codex-home-manager-diagnostics.webp)

Thread detail daily token timeline:

![Codex Home Manager daily token timeline](site/assets/codex-home-manager-daily-tokens.png)

## What is included

- The static web frontend deployed on Cloudflare Pages.
- Public release downloads for the Windows local connector.
- A public API capability overview, MCP-oriented endpoints, and safety boundary notes.
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

For read-only use, choose `.codex` directly from the hosted page in a Chromium browser.

For the full local management mode on Windows, download and run the local connector:

<https://github.com/SimpleZion/codex-home-manager/releases/latest/download/codex-home-manager-local-win-x64.exe>

The connector starts the full local product at `http://127.0.0.1:8765/` and registers the `codex-home-manager://start` browser protocol for the current Windows user.

The current Windows build is unsigned. If Windows SmartScreen shows "Windows protected your PC", choose "More info" and then "Run anyway" to start the app.

Agents can use the same local connector directly through HTTP or MCP. Thread detail reads can skip the heavier daily token timeline, then load `/api/threads/{thread_id}/daily-tokens` only when that visualization or audit data is needed. That endpoint returns numeric token usage only from auditable `token_count` events. Threads that only have SQLite `tokens_used` are marked with `unknownTokenThreads`; no token value is returned for those unknown records.

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

The deployed frontend can read real Codex Home data only from a user-selected local folder or from the user's own local connector API. Real Codex Home content is not uploaded by the hosted page. No real session JSONL, SQLite database, logs, exports, backups, screenshots, or user-specific paths are committed.
