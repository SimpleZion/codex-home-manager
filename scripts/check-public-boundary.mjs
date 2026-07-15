import { createHash, createPublicKey, verify as verifySignature } from "node:crypto";
import { readFile, readdir, stat } from "node:fs/promises";
import { extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { unzipSync } from "fflate";

const scriptRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const rootArgumentIndex = process.argv.indexOf("--root");
const root = resolve(rootArgumentIndex >= 0 ? process.argv[rootArgumentIndex + 1] : scriptRoot);
const siteDirectory = join(root, "site");
const assetsDirectory = join(siteDirectory, "assets");
const releaseMode = process.argv.includes("--release") || process.env.CODEX_HOME_MANAGER_PUBLIC_RELEASE_MODE === "1";

const exactAllowedPaths = new Set([
  ".gitattributes",
  ".gitignore",
  "LICENSE",
  "README.md",
  "SECURITY.md",
  "package-lock.json",
  "package.json",
  "wrangler.toml",
  "functions/assets/[name].js",
  "scripts/check-public-boundary.mjs",
  "scripts/check-public-boundary.test.mjs",
  "site/404.html",
  "site/SHA256SUMS.txt",
  "site/_headers",
  "site/_redirects",
  "site/app.js",
  "site/connector-release.json",
  "site/favicon.svg",
  "site/index.html",
  "site/public-api.json",
  "site/release-manifest.json",
  "site/release-manifest.json.sig",
  "site/release-signing-public-key.sha256",
  "site/release-signing-public-key.pem",
  "site/robots.txt",
  "site/sitemap.xml",
  "site/styles.css",
  "site/verify-codex-home-manager.ps1"
]);

const sourceOrDebugExtensions = new Set([
  ".c", ".cc", ".cpp", ".cs", ".h", ".hpp", ".map", ".pdb", ".py", ".pyc", ".pyo",
  ".rs", ".spec", ".ts", ".tsx"
]);
const sensitiveImplementationSymbols = [
  ["thread", "history", "repair"].join("_"),
  ["pending", "repair", "validation"].join("_"),
  ["backend", "server"].join("."),
  ["connector", "main", "py"].join("_"),
  ["release", "signing", "key"].join("_"),
  ["CODEX", "HOME", "MANAGER", "WRITE", "TOKEN"].join("_")
];
const alwaysBlockedTextFragments = [
  ["state", "5"].join("_") + ".sqlite.before",
  "C:\\" + "Zion" + "Cloud" + "Drive",
  "D:\\" + ".codex",
  ["X", "Codex", "Manager", "Token"].join("-") + ":"
];
const alwaysBlockedTextPatterns = [
  /[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"'<>]+/i,
  /[A-Za-z]:[\\/]+\.codex[\\/]+sessions/i,
  /gho_[A-Za-z0-9_]+/,
  /CLOUDFLARE_API_TOKEN\s*=/
];
const textExtensions = new Set([
  ".css", ".html", ".js", ".json", ".md", ".mjs", ".pem", ".ps1", ".svg", ".toml", ".txt", ""
]);
const releaseNamePattern = /^codex-home-manager-local-win-x64-v([0-9]+\.[0-9]+\.[0-9]+)-([0-9a-f]{12})\.(exe|zip)$/;
const publicDistRootNames = new Set(["favicon.svg", "index.html"]);
const publicDistAssetExtensions = new Set([".css", ".js", ".wasm"]);

function isPublicDistPath(path) {
  if (publicDistRootNames.has(path)) return true;
  const parts = path.split("/");
  return parts.length === 2 && parts[0] === "assets" &&
    /^[A-Za-z0-9._-]+$/.test(parts[1]) && publicDistAssetExtensions.has(extname(parts[1]).toLowerCase());
}

function validateSignedPublicDistRecords(records, label) {
  if (!Array.isArray(records) || !records.length) {
    throw new Error(`signed release manifest has no ${label} records`);
  }
  const paths = [];
  for (const record of records) {
    if (!record || typeof record.path !== "string" || !isPublicDistPath(record.path) ||
        !/^[0-9a-f]{64}$/.test(record.sha256 || "") || !Number.isInteger(record.size) || record.size < 0 ||
        paths.includes(record.path)) {
      throw new Error(`signed release manifest has an invalid ${label} record`);
    }
    paths.push(record.path);
  }
  if (!paths.includes("index.html") || JSON.stringify(paths) !== JSON.stringify([...paths].sort())) {
    throw new Error(`signed release manifest has a non-canonical ${label} record set`);
  }
  return records;
}

async function listFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const results = [];
  for (const entry of entries) {
    if ([".git", "node_modules", ".wrangler", ".tmp"].includes(entry.name)) continue;
    const fullPath = join(directory, entry.name);
    if (entry.isDirectory()) results.push(...await listFiles(fullPath));
    else results.push(fullPath);
  }
  return results;
}

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

function assertSafeExtension(relativePath) {
  if (sourceOrDebugExtensions.has(extname(relativePath).toLowerCase())) {
    throw new Error(`public boundary violation: source or debug extension in ${relativePath}`);
  }
}

function assertSafeText(relativePath, content) {
  for (const symbol of sensitiveImplementationSymbols) {
    if (content.includes(symbol)) {
      throw new Error(`public boundary violation: sensitive implementation symbol in ${relativePath}`);
    }
  }
  for (const fragment of alwaysBlockedTextFragments) {
    if (content.includes(fragment)) throw new Error(`public boundary violation in file content: ${relativePath}`);
  }
  for (const pattern of alwaysBlockedTextPatterns) {
    if (pattern.test(content)) throw new Error(`public boundary violation by pattern in file content: ${relativePath}`);
  }
}

function assertSafeZip(relativePath, content) {
  let entries;
  try {
    entries = Object.keys(unzipSync(new Uint8Array(content)));
  } catch (error) {
    throw new Error(`cannot inspect release ZIP ${relativePath}: ${error.message}`);
  }
  for (const rawEntry of entries) {
    const entry = rawEntry.replaceAll("\\", "/").toLowerCase();
    const entryExtension = extname(entry);
    if (entry.includes("/backend/") || entry.startsWith("backend/") || sourceOrDebugExtensions.has(entryExtension)) {
      throw new Error(`release ZIP contains blocked backend or source entry: ${rawEntry}`);
    }
  }
}

const releaseManifestPath = join(siteDirectory, "connector-release.json");
const releaseManifest = JSON.parse(await readFile(releaseManifestPath, "utf8"));
if (releaseManifest.schemaVersion !== 1 || !/^\d+\.\d+\.\d+$/.test(releaseManifest.version || "")) {
  throw new Error("connector-release.json has an invalid schema or version");
}
if (!Array.isArray(releaseManifest.artifacts)) throw new Error("connector-release.json artifacts must be an array");

const releaseArtifacts = new Map();
for (const artifact of releaseManifest.artifacts) {
  const match = releaseNamePattern.exec(artifact.name || "");
  if (!match || match[1] !== releaseManifest.version || match[3] !== artifact.kind) {
    throw new Error(`invalid content-addressed release artifact name: ${artifact.name}`);
  }
  if (match[2] !== artifact.sha256?.slice(0, 12)) throw new Error(`release filename hash mismatch: ${artifact.name}`);
  if (releaseArtifacts.has(artifact.name)) throw new Error(`duplicate release artifact: ${artifact.name}`);
  if (artifact.kind === "exe") {
    const audit = artifact.audit;
    if (audit?.method !== "pyi-archive-viewer+strings" || !Number.isInteger(audit.archiveEntryCount) ||
        audit.archiveEntryCount < 1 || audit.sourceFiles?.length !== 0 || audit.sensitiveStrings?.length !== 0) {
      throw new Error(`EXE lacks passing PyInstaller and strings boundary evidence: ${artifact.name}`);
    }
    const authenticode = artifact.authenticode;
    const validTrustedAuthenticode = authenticode?.status === "valid" &&
      /^[0-9A-F]{40}$/i.test(authenticode.signerThumbprint || "") && typeof authenticode.signerSubject === "string" && authenticode.signerSubject;
    const explicitlyUnavailable = authenticode?.status === "unavailable" &&
      authenticode.signerThumbprint == null && authenticode.signerSubject == null;
    if (authenticode?.detachedSignatureRequired !== true || (!validTrustedAuthenticode && !explicitlyUnavailable)) {
      throw new Error(`EXE has invalid Authenticode trust evidence or detached signature policy: ${artifact.name}`);
    }
  }
  releaseArtifacts.set(artifact.name, artifact);
}

const signedMetadataNames = [
  "release-manifest.json",
  "release-manifest.json.sig",
  "release-signing-public-key.pem",
  "release-signing-public-key.sha256"
];
const signedMetadata = new Map(await Promise.all(signedMetadataNames.map(async (name) => [
  name,
  await readFile(join(siteDirectory, name)).catch(() => null)
])));
const hasManifest = signedMetadata.get("release-manifest.json") !== null;
const hasSignature = signedMetadata.get("release-manifest.json.sig") !== null;
let signedPublicSiteDistRecords = null;
let signedPublicSiteDistPaths = null;
if (releaseMode && [...signedMetadata.values()].some((content) => content === null)) {
  throw new Error("release mode requires complete signed release metadata: manifest, detached signature, public key, and fingerprint pin");
}
if (hasManifest || hasSignature) {
  if ([...signedMetadata.values()].some((content) => content === null)) {
    throw new Error("signed release metadata is incomplete");
  }
  const publishedFingerprint = signedMetadata.get("release-signing-public-key.sha256").toString("utf8").trim().toLowerCase();
  const pinnedFingerprint = (process.env.CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256 || "").trim().toLowerCase();
  const publicVerifierText = (await readFile(join(siteDirectory, "verify-codex-home-manager.ps1"), "utf8").catch(() => ""));
  const verifierFingerprintMatch = /^\s*\$trustedPublicKeyFingerprint\s*=\s*["'](sha256:[0-9a-f]{64})["']\s*$/im.exec(publicVerifierText);
  const verifierFingerprint = verifierFingerprintMatch?.[1]?.toLowerCase();
  if (!/^sha256:[0-9a-f]{64}$/.test(publishedFingerprint)) {
    throw new Error("published release public key fingerprint is invalid");
  }
  if (!/^sha256:[0-9a-f]{64}$/.test(pinnedFingerprint)) {
    throw new Error("a separately pinned public key fingerprint is required for signed release metadata");
  }
  if (!verifierFingerprint) {
    throw new Error("public verifier has no independently pinned public key fingerprint");
  }
  const publicKey = createPublicKey(signedMetadata.get("release-signing-public-key.pem"));
  if (publicKey.asymmetricKeyType !== "ed25519") throw new Error("release public key must be Ed25519");
  const actualFingerprint = `sha256:${createHash("sha256").update(publicKey.export({ type: "spki", format: "der" })).digest("hex")}`;
  if (actualFingerprint !== publishedFingerprint || actualFingerprint !== pinnedFingerprint || actualFingerprint !== verifierFingerprint) {
    throw new Error("pinned public key fingerprint does not match the published Ed25519 key");
  }
  const manifestBytes = signedMetadata.get("release-manifest.json");
  const signatureBytes = Buffer.from(signedMetadata.get("release-manifest.json.sig").toString("ascii").trim(), "base64");
  if (!verifySignature(null, manifestBytes, publicKey, signatureBytes)) {
    throw new Error("release manifest Ed25519 signature verification failed");
  }
  let signedManifest;
  try {
    signedManifest = JSON.parse(manifestBytes.toString("utf8"));
  } catch {
    throw new Error("signed release manifest is not valid JSON");
  }
  if (![2, 3].includes(signedManifest.schema_version) ||
      (releaseMode && signedManifest.schema_version !== 3) ||
      signedManifest.public_key_fingerprint !== actualFingerprint) {
    throw new Error("signed release manifest has an invalid schema or public key fingerprint");
  }
  if (signedManifest.schema_version === 3) {
    const distRecords = validateSignedPublicDistRecords(signedManifest.dist_files, "build dist");
    signedPublicSiteDistRecords = validateSignedPublicDistRecords(
      signedManifest.public_site_dist_files,
      "public site dist"
    );
    const normalizeRecords = (records) => records.map(({ path, sha256: hash, size }) => ({ path, sha256: hash, size }));
    if (JSON.stringify(normalizeRecords(distRecords)) !== JSON.stringify(normalizeRecords(signedPublicSiteDistRecords))) {
      throw new Error("signed build and public site dist records differ");
    }
    signedPublicSiteDistPaths = new Set(signedPublicSiteDistRecords.map((record) => record.path));
    for (const record of signedPublicSiteDistRecords) {
      const content = await readFile(join(siteDirectory, ...record.path.split("/"))).catch(() => null);
      if (!content || content.length !== record.size || sha256(content) !== record.sha256) {
        throw new Error(`signed public site dist hash mismatch: ${record.path}`);
      }
    }
  }
  const deployment = signedManifest.cloudflare?.artifact_deployment;
  if (!deployment || deployment.project !== "codex-home-manager" || deployment.branch !== "main" ||
      deployment.status !== "success" || !/^https:\/\/[^/]+$/.test(deployment.url || "") ||
      !/^[0-9a-f-]+$/i.test(deployment.id || "") || !/^[0-9a-f]{40}$/i.test(deployment.public_commit || "") ||
      signedManifest.cloudflare?.metadata_deployment !== undefined) {
    throw new Error("signed release manifest has invalid artifact deployment proof");
  }
  if (!Array.isArray(signedManifest.public_artifacts) || !signedManifest.public_artifacts.length) {
    throw new Error("signed release manifest has no public artifacts");
  }
  const github = signedManifest.github;
  if (!github || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(github.repository || "") ||
      typeof github.tag !== "string" || !github.tag || !Number.isInteger(github.release_id) || github.release_id < 1 ||
      github.html_url !== `https://github.com/${github.repository}/releases/tag/${github.tag}` ||
      github.draft_verified_before_signing !== true ||
      JSON.stringify(github.metadata_assets) !== JSON.stringify(signedMetadataNames) ||
      !Array.isArray(github.artifact_assets) || github.artifact_assets.length !== releaseArtifacts.size) {
    throw new Error("signed release manifest has invalid GitHub release proof");
  }
  const githubArtifacts = new Map(github.artifact_assets.map((artifact) => [artifact?.name, artifact]));
  if (githubArtifacts.size !== releaseArtifacts.size) {
    throw new Error("signed release manifest GitHub artifact set mismatch");
  }
  for (const [name, artifact] of releaseArtifacts) {
    const githubArtifact = githubArtifacts.get(name);
    if (!githubArtifact || githubArtifact.sha256 !== artifact.sha256 || githubArtifact.size !== artifact.size) {
      throw new Error(`signed release manifest GitHub artifact mismatch: ${name}`);
    }
  }
  for (const artifact of signedManifest.public_artifacts) {
    if (!artifact || typeof artifact.path !== "string" || artifact.path.includes("/") || artifact.path.includes("\\") ||
        !/^[0-9a-f]{64}$/.test(artifact.sha256 || "") || !Number.isInteger(artifact.size)) {
      throw new Error("signed release manifest has an invalid public artifact record");
    }
    const content = await readFile(join(siteDirectory, artifact.path)).catch(() => null);
    if (!content || content.length !== artifact.size || sha256(content) !== artifact.sha256) {
      throw new Error(`signed manifest artifact hash mismatch: ${artifact.path}`);
    }
  }
}

const redirectLines = new Set(
  (await readFile(join(siteDirectory, "_redirects"), "utf8"))
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
);
const headerText = await readFile(join(siteDirectory, "_headers"), "utf8");

function headerBlock(path) {
  const lines = headerText.split(/\r?\n/);
  const start = lines.findIndex((line) => line.trim() === path);
  if (start < 0) return "";
  const block = [];
  for (let index = start + 1; index < lines.length && /^\s/.test(lines[index]); index += 1) block.push(lines[index].trim());
  return block.join("\n");
}

if (releaseMode || hasManifest || hasSignature) {
  for (const metadataName of signedMetadataNames) {
    if (!/cache-control:\s*no-store(?:,|$)/i.test(headerBlock(`/${metadataName}`))) {
      throw new Error(`signed release metadata must be no-store: /${metadataName}`);
    }
  }
}

const indexHtml = await readFile(join(siteDirectory, "index.html"), "utf8");
const readmeMarkdown = await readFile(join(root, "README.md"), "utf8").catch(() => "");
const htmlReferencedAssets = new Set([...indexHtml.matchAll(/\/assets\/([^"'\s>]+)/g)].map((match) => match[1]));
const readmeReferencedAssets = new Set([...readmeMarkdown.matchAll(/\(site\/assets\/([^\)\s]+)\)/g)].map((match) => match[1]));
const currentScriptName = [...htmlReferencedAssets].find((name) => name.endsWith(".js"));
const currentStyleName = [...htmlReferencedAssets].find((name) => name.endsWith(".css"));
const scriptReferencedAssets = new Set();
if (currentScriptName) {
  const currentScriptText = await readFile(join(assetsDirectory, currentScriptName), "utf8").catch(() => "");
  for (const match of currentScriptText.matchAll(/\/assets\/([^"'\s)]+)/g)) scriptReferencedAssets.add(match[1]);
}
const allowedAssetNames = new Set([...htmlReferencedAssets, ...scriptReferencedAssets, ...readmeReferencedAssets]);
const obsoleteAssetShimPattern = /^index-[A-Za-z0-9_-]+\.(?:js|css)$/;

async function isAllowedObsoleteShim(assetName) {
  if (!obsoleteAssetShimPattern.test(assetName)) return false;
  const content = (await readFile(join(assetsDirectory, assetName), "utf8").catch(() => "")).replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  if (assetName.endsWith(".js")) {
    return Boolean(currentScriptName && content.trim() === `/* Obsolete Codex Home Manager asset shim. */\nimport "/assets/${currentScriptName}";`);
  }
  return Boolean(currentStyleName && content.trim() === `/* Obsolete Codex Home Manager asset shim. */\n@import url("/assets/${currentStyleName}");`);
}

if (!htmlReferencedAssets.size) throw new Error("public site index.html does not reference the built product assets");

const files = await listFiles(root);
for (const file of files) {
  const relativePath = relative(root, file).replaceAll("\\", "/");
  assertSafeExtension(relativePath);
  const assetName = relativePath.startsWith("site/assets/") ? relativePath.slice("site/assets/".length) : null;
  const releaseName = relativePath.startsWith("site/") ? relativePath.slice("site/".length) : null;
  const signedAssetAllowed = assetName && signedPublicSiteDistPaths?.has(`assets/${assetName}`);
  const unsignedOrLegacyAssetAllowed = assetName && !signedPublicSiteDistPaths &&
    (allowedAssetNames.has(assetName) || await isAllowedObsoleteShim(assetName));
  const allowed = exactAllowedPaths.has(relativePath) ||
    signedAssetAllowed || unsignedOrLegacyAssetAllowed ||
    (assetName && readmeReferencedAssets.has(assetName)) ||
    (releaseName && releaseArtifacts.has(releaseName));
  if (!allowed) throw new Error(`public file is not in the public release allowlist: ${relativePath}`);

  const content = await readFile(file);
  const artifact = releaseName ? releaseArtifacts.get(releaseName) : null;
  if (artifact) {
    if (artifact.kind === "zip") assertSafeZip(relativePath, content);
    if (artifact.size !== content.length || artifact.sha256 !== sha256(content)) {
      throw new Error(`release artifact hash or size mismatch: ${relativePath}`);
    }
    continue;
  }
  if (content.length > 2_000_000) throw new Error(`unexpected large file in public repository: ${relativePath}`);
  if (textExtensions.has(extname(relativePath).toLowerCase())) assertSafeText(relativePath, content.toString("utf8"));
}

if (signedPublicSiteDistRecords) {
  const actualManagedDistPaths = files
    .map((file) => relative(siteDirectory, file).replaceAll("\\", "/"))
    .filter((path) => isPublicDistPath(path))
    .sort();
  const expectedManagedDistPaths = signedPublicSiteDistRecords.map((record) => record.path);
  if (JSON.stringify(actualManagedDistPaths) !== JSON.stringify(expectedManagedDistPaths)) {
    throw new Error("signed public site dist file set mismatch");
  }
}

for (const artifactName of releaseArtifacts.keys()) {
  if (!files.some((file) => relative(root, file).replaceAll("\\", "/") === `site/${artifactName}`)) {
    throw new Error(`connector-release.json references a missing artifact: site/${artifactName}`);
  }
}

for (const artifact of releaseArtifacts.values()) {
  const stableAliases = artifact.kind === "exe"
    ? ["/codex-home-manager-local-win-x64.exe", "/downloads/latest/windows-x64.exe"]
    : ["/codex-home-manager-local-win-x64.zip", "/downloads/latest/windows-x64.zip"];
  for (const alias of stableAliases) {
    if (!redirectLines.has(`${alias} /${artifact.name} 302`)) {
      throw new Error(`stable download redirect mismatch for ${alias}`);
    }
    if (!/cache-control:\s*no-store(?:,|$)/i.test(headerBlock(alias))) {
      throw new Error(`stable download alias must be no-store: ${alias}`);
    }
  }
}
if (releaseArtifacts.size && !/cache-control:.*max-age=31536000.*immutable/i.test(headerBlock("/codex-home-manager-local-win-x64-v*"))) {
  throw new Error("content-addressed release artifacts must have immutable cache headers");
}

const checksumEntries = new Map();
for (const [index, line] of (await readFile(join(siteDirectory, "SHA256SUMS.txt"), "utf8")).split(/\r?\n/).entries()) {
  if (!line) continue;
  const match = /^([0-9a-f]{64})  ([^/\\]+)$/.exec(line);
  if (!match || checksumEntries.has(match[2])) throw new Error(`invalid SHA256SUMS line ${index + 1}`);
  checksumEntries.set(match[2], match[1]);
}
for (const [name, expectedSha256] of checksumEntries) {
  const content = await readFile(join(siteDirectory, name)).catch(() => null);
  if (!content || sha256(content) !== expectedSha256) {
    throw new Error(`SHA256SUMS file hash mismatch for ${name}`);
  }
}
for (const artifact of releaseArtifacts.values()) {
  if (checksumEntries.get(artifact.name) !== artifact.sha256) {
    throw new Error(`SHA256SUMS mismatch for ${artifact.name}`);
  }
}

console.log(`public boundary PASS: ${files.length} allowlisted files checked`);
