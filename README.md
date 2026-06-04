# Codex Home Manager

Public Cloudflare Pages frontend for Codex Home Manager.

Codex Home Manager is a local-first operations console for inspecting and maintaining a Codex Desktop home directory: threads, project bindings, local resources, imports, backups, logs, and agent-facing APIs.

![Synthetic Codex Home Manager preview](site/assets/mock-dashboard.svg)

## What is included

- Static Codex Home Manager web console deployed on Cloudflare Pages.
- A browser frontend that connects to a user's local Codex Home Manager API at `http://127.0.0.1:8765` by default.
- Public-facing API capability overview and safety model documentation.
- Launch and security notes for using the product without exposing local data.

## What is intentionally not included

This repository does not contain the private local engine or implementation code that can read, repair, migrate, slim, or write a real Codex Home directory.

Excluded by design:

- Local SQLite and JSONL manipulation code.
- Real Codex Desktop session data, logs, exports, backups, or screenshots.
- Token handling, write-gate implementation, preview ticket validation, and restore internals.
- Any user-specific project paths, conversation titles, memory files, or machine identifiers.

This repository is suitable for the public frontend and deployment shell. It is not the private local maintenance engine.

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

Current Cloudflare Pages deployment: <https://codex-home-manager.pages.dev/>.

Current custom domain: <https://codex-home-manager.simplezion.com/>.

## Privacy stance

The deployed frontend reads real Codex Home data only from the user's own local API. No real Codex Home content, session JSONL, SQLite database, logs, exports, backups, screenshots, or user-specific paths are committed.
