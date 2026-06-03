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
  "codex_thread_manager"
];

const alwaysBlockedTextFragments = [
  "state_5.sqlite.before",
  "X-Codex-Manager-Token:"
];

const alwaysBlockedTextPatterns = [
  /[A-Za-z]:[\\/]+Users[\\/]+[^\\/\\s"'<>]+/i,
  /[A-Za-z]:[\\/]+\\.codex[\\/]+sessions/i,
  /gho_[A-Za-z0-9_]+/,
  /CLOUDFLARE_API_TOKEN\\s*=/
];

const privateRuntimeTextFragments = [
  "logs_2.sqlite",
  "session_index.jsonl",
  "rollout-",
  "data/backups",
  "data/exports"
];

const documentationFiles = new Set([
  "README.md",
  "SECURITY.md"
]);

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
  if (!documentationFiles.has(relativePath)) {
    for (const fragment of privateRuntimeTextFragments) {
      if (content.includes(fragment)) {
        throw new Error(`private runtime reference outside documentation: ${relativePath}`);
      }
    }
  }
}

console.log(`public boundary PASS: ${files.length} files checked`);
