import { readdir, readFile, stat } from "node:fs/promises";
import { join, relative } from "node:path";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

const blockedPathFragments = [
  "backend",
  "state_5.sqlite",
  "logs_2.sqlite",
  "session_index.jsonl",
  "rollout-",
  "data/backups",
  "data/exports",
  ".codex_tmp",
  ["codex", "thread", "manager"].join("_")
];

const alwaysBlockedTextFragments = [
  "state_5.sqlite.before",
  "C:\\" + "Zion" + "Cloud" + "Drive",
  "X-Codex-Manager-Token:"
];

const alwaysBlockedTextPatterns = [
  /[A-Za-z]:[\\/]+Users[\\/]+[^\\/\\s"'<>]+/i,
  /[A-Za-z]:[\\/]+\\.codex[\\/]+sessions/i,
  /gho_[A-Za-z0-9_]+/,
  /CLOUDFLARE_API_TOKEN\\s*=/
];

async function listFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const results = [];
  for (const entry of entries) {
    const fullPath = join(directory, entry.name);
    if (entry.name === ".git" || entry.name === "node_modules" || entry.name === ".wrangler") continue;
    if (entry.isDirectory()) {
      results.push(...await listFiles(fullPath));
    } else {
      results.push(fullPath);
    }
  }
  return results;
}

const files = await listFiles(root);
const siteDirectory = join(root, "site");
const assetsDirectory = join(siteDirectory, "assets");
const indexHtml = await readFile(join(siteDirectory, "index.html"), "utf8");
const htmlReferencedAssets = new Set(
  [...indexHtml.matchAll(/\/assets\/([^"'\s>]+)/g)].map((match) => match[1])
);
const allowedAssetNames = new Set([...htmlReferencedAssets, "mock-dashboard.svg"]);

if (!htmlReferencedAssets.size) {
  throw new Error("public site index.html does not reference the built product assets");
}

for (const entry of await readdir(assetsDirectory, { withFileTypes: true })) {
  if (entry.isFile() && !allowedAssetNames.has(entry.name)) {
    throw new Error(`stale or unreferenced public asset: site/assets/${entry.name}`);
  }
}

for (const file of files) {
  const relativePath = relative(root, file).replaceAll("\\", "/");
  for (const fragment of blockedPathFragments) {
    if (relativePath.includes(fragment)) {
      throw new Error(`public boundary violation in path: ${relativePath}`);
    }
  }
  const info = await stat(file);
  if (info.size > 2_000_000) {
    throw new Error(`unexpected large file in public repository: ${relativePath}`);
  }
  if (relativePath === "scripts/check-public-boundary.mjs") {
    continue;
  }
  const content = await readFile(file, "utf8").catch(() => "");
  for (const fragment of alwaysBlockedTextFragments) {
    if (content.includes(fragment)) {
      throw new Error(`public boundary violation in file content: ${relativePath}`);
    }
  }
  for (const pattern of alwaysBlockedTextPatterns) {
    if (pattern.test(content)) {
      throw new Error(`public boundary violation by pattern in file content: ${relativePath}`);
    }
  }
}

console.log(`public boundary PASS: ${files.length} files checked`);
