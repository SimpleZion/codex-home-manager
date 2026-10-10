import { createHash } from "node:crypto";
import * as filesystem from "node:fs/promises";
import { createReadStream } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

const root = path.resolve(import.meta.dirname, "..");
const manifestName = "frontend-release-manifest.json";
const schemaVersion = 2;
// One source contract for the frontend and desktop consumers; no extension filter on source trees.
const trackedInputs = ["src", "public", "packaging/runtime", "scripts/frontend-release-artifact.mjs"];
const desktopInputs = [...trackedInputs, "backend", "packaging", "scripts", "src-tauri", ".cargo"];
const generatedTrees = ["src-tauri/target", "src-tauri/resources", "src-tauri/gen"];
const rootBuildInput = /^(?:index\.html|package(?:-lock)?\.json|npm-shrinkwrap\.json|tsconfig(?:\.[^.]+)*\.json|.*\.[cm]?[jt]s|.*\.toml|Cargo\.lock|rust-toolchain|requirements(?:[-.][^.]+)*\.txt|\.npmrc|\.env(?:\..+)?)$/;

export function runFrontendBuild(directory, { environment = process.env, platform = process.platform, spawn = spawnSync } = {}) {
  const windows = platform === "win32";
  const commands = environment.CODEX_STORAGE_RUN_ROOT
    ? [
        [process.execPath, ["node_modules/typescript/bin/tsc", "-b", "--pretty", "false"]],
        [process.execPath, ["node_modules/vite/bin/vite.js", "build"]]
      ]
    : [[windows ? "cmd.exe" : "npm", windows ? ["/d", "/s", "/c", "npm.cmd run build"] : ["run", "build"]]];
  for (const [command, args] of commands) {
    const result = spawn(command, args, { cwd: directory, env: environment, stdio: "inherit" });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error("Frontend build failed; no build receipt issued");
  }
}

export function createArtifactContract({ directory = root, io = { ...filesystem, createReadStream }, runBuild = runFrontendBuild,
  now = () => new Date().toISOString(), toolchain = { node: process.version, platform: process.platform, arch: process.arch }, environment = process.env } = {}) {
  const source = path.join(directory, "dist");
  const target = path.join(directory, "build", "frontend-release");
  const relative = (filename, base = directory) => path.relative(base, filename).replaceAll("\\", "/");
  const isGenerated = (name) => name.split("/").some((part) => [".git", "__pycache__", "node_modules"].includes(part)) || name.endsWith(".tsbuildinfo") || generatedTrees.some((tree) => name === tree || name.startsWith(`${tree}/`));

  async function walk(base, sourceFiles = false) {
    const metadata = await io.lstat(base);
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) throw new Error(`Not a regular artifact directory: ${relative(base)}`);
    const files = [];
    async function visit(current) {
      for (const entry of (await io.readdir(current, { withFileTypes: true })).sort((left, right) => left.name < right.name ? -1 : left.name > right.name ? 1 : 0)) {
        const filename = path.join(current, entry.name);
        if (sourceFiles && isGenerated(relative(filename))) continue;
        if (entry.isSymbolicLink()) throw new Error(`Symbolic link is not a bound artifact input: ${relative(filename)}`);
        if (entry.isDirectory()) await visit(filename);
        else if (entry.isFile()) files.push(relative(filename, base));
        else throw new Error(`Unsupported artifact entry: ${relative(filename)}`);
        if (files.length > 20000) throw new Error("Artifact file budget exceeded");
      }
    }
    await visit(base);
    return files.sort();
  }

  async function digestFiles(base, files) {
    const hash = createHash("sha256");
    for (const name of files) {
      const filename = path.join(base, name);
      const before = await io.lstat(filename);
      if (!before.isFile() || before.isSymbolicLink()) throw new Error(`Not a regular artifact file: ${name}`);
      hash.update(name).update("\0");
      // Stream binary resource trees rather than materializing entire sidecars in memory.
      for await (const chunk of io.createReadStream(filename)) hash.update(chunk);
      const after = await io.lstat(filename);
      if (!after.isFile() || after.isSymbolicLink() || before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ino !== after.ino) throw new Error(`Artifact changed while hashing: ${name}`);
      hash.update("\0");
    }
    return hash.digest("hex");
  }

  async function sourceDigest(scope = "frontend") {
    if (!["frontend", "desktop"].includes(scope)) throw new Error("Unknown source scope");
    const files = new Set();
    // Build/config families include new TS configs without hashing unrelated root logs or user history.
    for (const entry of await io.readdir(directory, { withFileTypes: true })) {
      if (!rootBuildInput.test(entry.name) || isGenerated(entry.name)) continue;
      if (entry.isSymbolicLink()) throw new Error(`Symbolic source input: ${entry.name}`);
      if (entry.isFile()) files.add(entry.name);
    }
    for (const name of scope === "desktop" ? desktopInputs : trackedInputs) {
      const filename = path.join(directory, name);
      let metadata;
      try { metadata = await io.lstat(filename); } catch (error) { if (error?.code === "ENOENT") continue; throw error; }
      if (metadata.isSymbolicLink()) throw new Error(`Symbolic source input: ${name}`);
      if (metadata.isDirectory()) for (const child of await walk(filename, true)) files.add(`${name}/${child}`);
      else if (metadata.isFile()) files.add(name);
      else throw new Error(`Unsupported source input: ${name}`);
    }
    const buildEnvironment = Object.fromEntries(Object.entries(environment).filter(([key]) => key === "NODE_ENV" || key.startsWith("VITE_")).sort(([left], [right]) => left.localeCompare(right)));
    return createHash("sha256").update(JSON.stringify({ schemaVersion, scope, toolchain, buildEnvironment })).update(await digestFiles(directory, [...files].sort())).digest("hex");
  }

  async function artifactFiles(base) {
    const files = (await walk(base)).filter((name) => name !== manifestName);
    if (!files.includes("index.html")) throw new Error("Frontend index.html is missing");
    return files;
  }

  async function snapshot(base, frontend = false) {
    const enumerate = () => frontend ? artifactFiles(base) : walk(base);
    const files = await enumerate();
    const artifactDigest = await digestFiles(base, files);
    if (JSON.stringify(files) !== JSON.stringify(await enumerate())) throw new Error("Artifact inventory changed while hashing");
    return { files, artifactDigest };
  }

  async function verifyStoredDirectory(base) {
    const manifest = JSON.parse(await io.readFile(path.join(base, manifestName), "utf8"));
    if (manifest.schemaVersion !== schemaVersion || typeof manifest.builtAt !== "string" || !Number.isFinite(Date.parse(manifest.builtAt))) throw new Error("No trusted frontend build receipt; run desktop:prepare-sidecar without skip flags");
    const { files, artifactDigest } = await snapshot(base, true);
    // Enumerate the real tree, not the receipt's potentially incomplete or unsafe path list.
    if (JSON.stringify(files) !== JSON.stringify(manifest.files) || artifactDigest !== manifest.artifactDigest) throw new Error("Frontend artifact tree does not match its build receipt");
    return manifest;
  }

  async function verifyDirectory(base) {
    const manifest = await verifyStoredDirectory(base);
    if (await sourceDigest() !== manifest.sourceDigest) throw new Error("Frontend sources or toolchain changed; rebuild the frontend");
    return manifest;
  }

  async function stage() {
    const manifest = await verifyDirectory(source);
    await io.mkdir(path.dirname(target), { recursive: true });
    const rollback = path.join(directory, "build", "frontend-release.rollback");
    try {
      const current = await verifyStoredDirectory(target);
      if (current.sourceDigest === manifest.sourceDigest && current.artifactDigest === manifest.artifactDigest) return current.artifactDigest;
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    try { await io.lstat(rollback); throw new Error("Frontend rollback slot is occupied; retire it through the storage lifecycle before staging another generation"); }
    catch (error) { if (error?.code !== "ENOENT") throw error; }
    let movedCurrent = false;
    try {
      try { await io.rename(target, rollback); movedCurrent = true; }
      catch (error) { if (error?.code !== "ENOENT") throw error; }
      await io.cp(source, target, { recursive: true, force: true });
      await verifyDirectory(source);
      await verifyDirectory(target);
      await io.writeFile(path.join(target, manifestName), `${JSON.stringify({ ...manifest, stagedAt: now() }, null, 2)}\n`, "utf8");
      return (await verifyDirectory(target)).artifactDigest;
    } catch (error) {
      if (movedCurrent) {
        try { await io.lstat(target); }
        catch (missing) { if (missing?.code === "ENOENT") await io.rename(rollback, target); }
      }
      throw error;
    }
  }

  async function build() {
    const frozenSource = await sourceDigest();
    const builtAt = now();
    await runBuild(directory);
    if (await sourceDigest() !== frozenSource) throw new Error("Frontend inputs changed during the build; no receipt issued");
    const artifact = await snapshot(source, true);
    const manifest = { schemaVersion, builtAt, sourceDigest: frozenSource, ...artifact };
    if (await sourceDigest() !== frozenSource) throw new Error("Frontend inputs changed while hashing the build");
    await io.writeFile(path.join(source, manifestName), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
    return stage();
  }

  async function assertSameTree(left, right) {
    if (JSON.stringify(await snapshot(left)) !== JSON.stringify(await snapshot(right))) throw new Error(`Embedded resource tree differs: ${relative(right)}`);
  }

  async function desktopArtifacts() {
    await verifyDirectory(target);
    const config = JSON.parse(await io.readFile(path.join(directory, "src-tauri", "tauri.conf.json"), "utf8"));
    const resources = config.bundle?.resources;
    if (!resources || Array.isArray(resources) || typeof resources !== "object" || Object.keys(resources).length === 0) throw new Error("Desktop resource mapping is not explicit");
    const files = [];
    // Consume Tauri's real resource mapping instead of maintaining another binary/extension allowlist.
    for (const resource of Object.keys(resources).sort()) {
      if (!resource.startsWith("resources/") || resource.includes("\\") || resource.split("/").includes("..") || /[*?]/.test(resource)) throw new Error("Unsupported desktop resource mapping");
      const base = path.join(directory, "src-tauri", resource);
      for (const name of await walk(base)) files.push(relative(path.join(base, name)));
    }
    const resourceRoot = path.join(directory, "src-tauri", "resources");
    await assertSameTree(target, path.join(resourceRoot, "web"));
    await assertSameTree(target, path.join(resourceRoot, "legacy", "_internal", "dist"));
    await assertSameTree(path.join(directory, "packaging", "runtime"), path.join(resourceRoot, "runtime"));
    await assertSameTree(path.join(directory, "packaging", "runtime"), path.join(resourceRoot, "legacy", "_internal", "packaging", "runtime"));
    return digestFiles(directory, [...new Set(files)].sort());
  }

  return { build, stage, verify: async () => (await verifyDirectory(target)).artifactDigest,
    verifyBuild: async () => (await verifyDirectory(source)).artifactDigest, sourceDigest, desktopArtifacts };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const contract = createArtifactContract();
  const mode = process.argv[2];
  let digest;
  if (mode === "build") digest = await contract.build();
  else if (mode === "stage") digest = await contract.stage();
  else if (mode === "verify") digest = await contract.verify();
  else if (mode === "verify-build") digest = await contract.verifyBuild();
  else if (mode === "source-digest") digest = await contract.sourceDigest(process.argv[3] || "frontend");
  else if (mode === "desktop-artifacts") digest = await contract.desktopArtifacts();
  else throw new Error("usage: node scripts/frontend-release-artifact.mjs <build|stage|verify|verify-build|source-digest [desktop]|desktop-artifacts>");
  process.stdout.write(`${digest}\n`);
}
