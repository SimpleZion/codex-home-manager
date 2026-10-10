import { createHash } from "node:crypto";
import { readFile, readdir, lstat } from "node:fs/promises";
import { join } from "node:path";
import { Unzip, unzipSync } from "fflate";

const hash = (content) => createHash("sha256").update(content).digest("hex");

async function validateFrozenFrontend(site, expectedHash) {
  const bytes = await readFile(join(site, "frontend-release-manifest.json"));
  if (hash(bytes) !== expectedHash) throw new Error("desktop frontend differs from the frozen build receipt");
  const frozen = JSON.parse(bytes);
  const files = [];
  for (const name of ["cockpit-icon.png", "favicon.svg", "index.html"]) {
    try {
      const metadata = await lstat(join(site, name));
      if (!metadata.isFile() || metadata.isSymbolicLink()) throw new Error("desktop frontend has a non-file resource");
      files.push(name);
    } catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  for (const entry of await readdir(join(site, "assets"), {withFileTypes:true})) {
    if (!entry.isFile() || !/^[A-Za-z0-9._-]+\.(?:js|css|wasm)$/.test(entry.name)) throw new Error("desktop frontend has an unlisted resource");
    files.push(`assets/${entry.name}`);
  }
  files.sort();
  if (frozen.schemaVersion !== 2 || JSON.stringify(frozen.files) !== JSON.stringify(files)) throw new Error("desktop frontend file set differs from its receipt");
  const digest = createHash("sha256");
  for (const name of files) digest.update(name).update("\0").update(await readFile(join(site,name))).update("\0");
  if (digest.digest("hex") !== frozen.artifactDigest) throw new Error("desktop frontend resource bytes differ from the frozen receipt");
}

export async function validateDesktopRelease(manifest, bundle, site) {
  if (manifest.product !== "Codex Cockpit" || bundle.flavor !== "tauri-desktop" ||
      bundle.schemaVersion !== 2 || manifest.version !== bundle.version ||
      bundle.artifacts.length !== 2 || new Set(bundle.artifacts.map((item) => item.kind)).size !== 2) {
    throw new Error("desktop product, version or artifact contract is invalid");
  }
  if (hash(await readFile(join(site, "connector-release.json"))) !== manifest.bundle_sha256) {
    throw new Error("desktop bundle metadata differs from the signed digest");
  }
  const build = manifest.desktop_build;
  const receipt = build?.receipt;
  const executable = bundle.artifacts.find((item) => item.kind === "exe");
  if (build?.method !== "publisher-local-tauri-nsis" || build.github_built !== false || build.portable !== false ||
      build.source_snapshot_after_build !== true || receipt?.schemaVersion !== 2 ||
      receipt.productName !== manifest.product || receipt.version !== manifest.version ||
      receipt.installerSha256 !== executable?.sha256 ||
      ["catalogSha256", "frontendManifestSha256", "legacySha256", "sourceFingerprint", "resourcesFingerprint"]
        .some((key) => !/^[0-9a-f]{64}$/.test(receipt[key] || ""))) {
    throw new Error("desktop build provenance is invalid or incorrectly claims a CI build");
  }
  const source = manifest.source_validation;
  if (source?.method !== "isolated-local-full-gate" || source.quality?.status !== "passed" ||
      source.source_ref !== "refs/heads/source" || !/^[0-9a-f]{40}$/.test(source.source_commit || "") ||
      !/^[0-9a-f]{64}$/.test(source.manifest_sha256 || "") ||
      source.quality.sourceManifestSha256 !== source.manifest_sha256) {
    throw new Error("desktop source validation is not bound to a passing exact source snapshot");
  }
  await validateFrozenFrontend(site, receipt.frontendManifestSha256);
  const records = manifest.public_artifacts;
  const names = [executable.name, bundle.artifacts.find((item) => item.kind === "zip").name,
    "codex-home-manager-source.zip", "verify-codex-home-manager.ps1"];
  if (!Array.isArray(records) || records.length !== names.length ||
      new Set(records.map((item) => item.path)).size !== names.length ||
      names.some((name) => !records.some((item) => item.path === name))) {
    throw new Error("desktop signed public artifact set is invalid");
  }
  for (const artifact of bundle.artifacts) {
    const signed = records.find((item) => item.path === artifact.name);
    if (signed.sha256 !== artifact.sha256 || signed.size !== artifact.size) {
      throw new Error("desktop signed artifact differs from bundle metadata");
    }
  }
  if (JSON.stringify(source.archive) !== JSON.stringify(records.find((item) => item.path === "codex-home-manager-source.zip"))) {
    throw new Error("desktop source archive is not bound to the signed artifact");
  }
  const archiveBytes = await readFile(join(site, "codex-home-manager-source.zip"));
  const entryNames = new Set();
  const directoryReader = new Unzip((file) => {
    if (entryNames.has(file.name)) throw new Error("desktop source archive contains duplicate entries");
    entryNames.add(file.name);
  });
  directoryReader.push(archiveBytes, true);
  const archive = unzipSync(archiveBytes);
  if (entryNames.size !== Object.keys(archive).length || Object.keys(archive).some((name) => !entryNames.has(name))) {
    throw new Error("desktop source archive has inconsistent file directories");
  }
  const sourceManifest = archive["SOURCE_COMMITS.json"];
  if (!sourceManifest || hash(sourceManifest) !== source.manifest_sha256) {
    throw new Error("desktop source archive contains a different source manifest");
  }
  const sources = JSON.parse(Buffer.from(sourceManifest).toString("utf8"));
  if (sources.schemaVersion !== 2 || !sources.sources?.rootRepository || !sources.sources?.managerRepository) {
    throw new Error("desktop source archive manifest is invalid");
  }
  const expected = new Set(["SOURCE_COMMITS.json"]);
  for (const repository of Object.values(sources.sources)) {
    for (const file of repository.files) {
      if (typeof file.path !== "string" || file.path.startsWith("/") || file.path.includes("\\") ||
          file.path.split("/").some((part) => ["..", ".git", ".codex", "node_modules", "target"].includes(part)) ||
          /\.(?:pem|key|pfx|p12|sqlite|jsonl|exe|pyc|pdb)$/i.test(file.path) || expected.has(file.path) ||
          !archive[file.path] || archive[file.path].length !== file.bytes || hash(archive[file.path]) !== file.sha256) {
        throw new Error("desktop source archive has invalid, sensitive or mismatched source entries");
      }
      expected.add(file.path);
    }
  }
  if (Object.keys(archive).length !== expected.size) {
    throw new Error("desktop source archive contains unlisted entries");
  }
}
