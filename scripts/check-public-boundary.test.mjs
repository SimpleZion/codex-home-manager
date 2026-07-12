import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const checker = fileURLToPath(new URL("./check-public-boundary.mjs", import.meta.url));

async function createReleaseFixture() {
  const root = await mkdtemp(join(tmpdir(), "codex-home-manager-public-"));
  await mkdir(join(root, "site", "assets"), { recursive: true });
  await mkdir(join(root, "scripts"), { recursive: true });
  await writeFile(join(root, "package.json"), JSON.stringify({ name: "fixture" }));
  await writeFile(join(root, "package-lock.json"), JSON.stringify({ lockfileVersion: 3 }));
  await writeFile(join(root, "LICENSE"), "license\n");
  await writeFile(join(root, "README.md"), "public release\n");
  await writeFile(join(root, "SECURITY.md"), "security\n");
  await writeFile(join(root, "wrangler.toml"), 'name = "fixture"\n');
  await writeFile(join(root, "site", "index.html"), '<script src="/assets/app-abc123.js"></script>\n');
  await writeFile(join(root, "site", "assets", "app-abc123.js"), "console.info('public');\n");
  await writeFile(join(root, "site", "_headers"), "/*\n  Cache-Control: no-store\n");
  await writeFile(join(root, "site", "_redirects"), "\n");
  await writeFile(join(root, "site", "SHA256SUMS.txt"), "");
  await writeFile(join(root, "site", "connector-release.json"), JSON.stringify({
    schemaVersion: 1,
    version: "1.0.0",
    artifacts: []
  }));
  return root;
}

function runChecker(root, environment = {}) {
  const childEnvironment = { ...process.env };
  delete childEnvironment.CODEX_HOME_MANAGER_PUBLIC_RELEASE_MODE;
  delete childEnvironment.CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256;
  Object.assign(childEnvironment, environment);
  return spawnSync(process.execPath, [checker, "--root", root], {
    encoding: "utf8",
    windowsHide: true,
    env: childEnvironment
  });
}

async function addSignedReleaseMetadata(root, missingName = null) {
  const { name } = await addExecutableRelease(root);
  const artifactPath = join(root, "site", name);
  const bundlePath = join(root, "site", "connector-release.json");
  const checksumPath = join(root, "site", "SHA256SUMS.txt");
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  const publicKeyPem = publicKey.export({ type: "spki", format: "pem" });
  const fingerprint = `sha256:${createHash("sha256").update(publicKey.export({ type: "spki", format: "der" })).digest("hex")}`;
  const records = await Promise.all([artifactPath, bundlePath, checksumPath].map(async (path) => {
    const content = await (await import("node:fs/promises")).readFile(path);
    return { path: path.split(/[\\/]/).at(-1), sha256: createHash("sha256").update(content).digest("hex"), size: content.length };
  }));
  const manifest = {
    schema_version: 2,
    sources: { build: { root: { head: "a".repeat(40), branch: "main", clean: true }, manager: { head: "b".repeat(40), branch: "main", clean: true } }, artifact_public: { head: "c".repeat(40), branch: "main", clean: true } },
    public_key_fingerprint: fingerprint,
    public_artifacts: records,
    cloudflare: { artifact_deployment: { id: "7d7aeac7-23a7-4eca-bc4a-c76c515727c0", project: "codex-home-manager", branch: "main", public_commit: "c".repeat(40), url: "https://artifact.codex-home-manager.pages.dev", status: "success" } },
    github: {
      release_id: 42,
      repository: "example/project",
      tag: "v1.0.0",
      html_url: "https://github.com/example/project/releases/tag/v1.0.0",
      draft_verified_before_signing: true,
      artifact_assets: [{ name, sha256: createHash("sha256").update(await (await import("node:fs/promises")).readFile(artifactPath)).digest("hex"), size: (await (await import("node:fs/promises")).stat(artifactPath)).size }],
      metadata_assets: ["release-manifest.json", "release-manifest.json.sig", "release-signing-public-key.pem", "release-signing-public-key.sha256"]
    }
  };
  const manifestBytes = Buffer.from(JSON.stringify(manifest) + "\n");
  const metadata = new Map([
    ["release-manifest.json", manifestBytes],
    ["release-manifest.json.sig", sign(null, manifestBytes, privateKey).toString("base64") + "\n"],
    ["release-signing-public-key.pem", publicKeyPem],
    ["release-signing-public-key.sha256", fingerprint + "\n"]
  ]);
  for (const [name, content] of metadata) {
    if (name !== missingName) await writeFile(join(root, "site", name), content);
  }
  const headerPath = join(root, "site", "_headers");
  const existingHeaders = await (await import("node:fs/promises")).readFile(headerPath, "utf8");
  const metadataHeaders = [...metadata.keys()].map((name) => `/${name}\n  Cache-Control: no-store, max-age=0`).join("\n");
  await writeFile(headerPath, `${existingHeaders.trimEnd()}\n${metadataHeaders}\n`);
  await writeFile(join(root, "site", "verify-codex-home-manager.ps1"), `$trustedPublicKeyFingerprint = "${fingerprint}"\n`);
  return { fingerprint, name };
}

async function addExecutableRelease(root) {
  const content = Buffer.from("fake audited executable");
  const sha256 = createHash("sha256").update(content).digest("hex");
  const name = `codex-home-manager-local-win-x64-v1.0.0-${sha256.slice(0, 12)}.exe`;
  await writeFile(join(root, "site", name), content);
  await writeFile(join(root, "site", "connector-release.json"), JSON.stringify({
    schemaVersion: 1,
    version: "1.0.0",
    artifacts: [{
      name,
      kind: "exe",
      sha256,
      size: content.length,
      audit: { method: "pyi-archive-viewer+strings", archiveEntryCount: 1, sourceFiles: [], sensitiveStrings: [] },
      authenticode: { status: "unavailable", signerThumbprint: null, signerSubject: null, detachedSignatureRequired: true }
    }]
  }));
  await writeFile(join(root, "site", "SHA256SUMS.txt"), `${sha256}  ${name}\n`);
  await writeFile(join(root, "site", "_redirects"), [
    `/codex-home-manager-local-win-x64.exe /${name} 302`,
    `/downloads/latest/windows-x64.exe /${name} 302`,
    "/* /index.html 200"
  ].join("\n") + "\n");
  await writeFile(join(root, "site", "_headers"), [
    "/codex-home-manager-local-win-x64-v*",
    "  Cache-Control: public, max-age=31536000, immutable",
    "/codex-home-manager-local-win-x64.exe",
    "  Cache-Control: no-store, max-age=0",
    "/downloads/latest/windows-x64.exe",
    "  Cache-Control: no-store, max-age=0"
  ].join("\n") + "\n");
  return { name, sha256 };
}

test("rejects an unlisted file even when its name and content look harmless", async () => {
  const root = await createReleaseFixture();
  await writeFile(join(root, "site", "notes.txt"), "harmless\n");

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not in the public release allowlist/i);
});

for (const extension of ["map", "ts", "tsx", "py", "pyc", "pdb"] ) {
  test(`rejects published .${extension} source or debug material`, async () => {
    const root = await createReleaseFixture();
    await writeFile(join(root, "site", `leak.${extension}`), "source\n");

    const result = runChecker(root);

    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /source or debug extension/i);
  });
}

test("rejects sensitive backend implementation symbols in public text", async () => {
  const root = await createReleaseFixture();
  const sensitiveSymbol = ["thread", "history", "repair"].join("_");
  await writeFile(join(root, "site", "assets", "app-abc123.js"), `const value = '${sensitiveSymbol}';\n`);

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /sensitive implementation symbol/i);
});

test("rejects a release ZIP containing backend or source files", async () => {
  const root = await createReleaseFixture();
  const archiveName = "codex-home-manager-local-win-x64-v1.0.0-0123456789ab.zip";
  const packageRoot = await mkdtemp(join(tmpdir(), "codex-home-manager-package-"));
  await mkdir(join(packageRoot, "CodexHomeManagerLocal", "backend"), { recursive: true });
  await writeFile(join(packageRoot, "CodexHomeManagerLocal", "backend", "server.py"), "private source\n");
  await writeFile(join(packageRoot, "Start Codex Home Manager.cmd"), "start\n");
  const archivePath = join(root, "site", archiveName);
  const escapedPackagePattern = `${packageRoot}\\*`.replaceAll("'", "''");
  const escapedArchivePath = archivePath.replaceAll("'", "''");
  const zipResult = spawnSync(
    "powershell",
    ["-NoProfile", "-Command", `Compress-Archive -Path '${escapedPackagePattern}' -DestinationPath '${escapedArchivePath}'`],
    { encoding: "utf8", windowsHide: true }
  );
  assert.equal(zipResult.status, 0, zipResult.stderr);
  const archiveSize = (await import("node:fs/promises")).stat(archivePath).then((info) => info.size);
  await writeFile(join(root, "site", "connector-release.json"), JSON.stringify({
    schemaVersion: 1,
    version: "1.0.0",
    artifacts: [{ name: archiveName, kind: "zip", sha256: "0123456789ab" + "0".repeat(52), size: await archiveSize }]
  }));

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /release ZIP contains blocked backend or source entry/i);
});

test("rejects a stable download redirect that does not target the content-addressed artifact", async () => {
  const root = await createReleaseFixture();
  await addExecutableRelease(root);
  await writeFile(join(root, "site", "_redirects"), [
    "/codex-home-manager-local-win-x64.exe /wrong.exe 302",
    "/downloads/latest/windows-x64.exe /wrong.exe 302",
    "/* /index.html 200"
  ].join("\n") + "\n");

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /stable download redirect mismatch/i);
});

test("rejects cache headers that can cache a stable download alias", async () => {
  const root = await createReleaseFixture();
  await addExecutableRelease(root);
  await writeFile(join(root, "site", "_headers"), [
    "/codex-home-manager-local-win-x64-v*",
    "  Cache-Control: public, max-age=31536000, immutable",
    "/codex-home-manager-local-win-x64.exe",
    "  Cache-Control: public, max-age=14400"
  ].join("\n") + "\n");

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /stable download alias must be no-store/i);
});

test("rejects checksums that drift from connector release metadata", async () => {
  const root = await createReleaseFixture();
  const { name } = await addExecutableRelease(root);
  await writeFile(join(root, "site", "SHA256SUMS.txt"), `${"0".repeat(64)}  ${name}\n`);

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /SHA256SUMS.*mismatch/i);
});

test("rejects an auxiliary download whose bytes drift from SHA256SUMS", async () => {
  const root = await createReleaseFixture();
  const { name, sha256 } = await addExecutableRelease(root);
  await writeFile(join(root, "site", "verify-codex-home-manager.ps1"), "Write-Host 'verify'\n");
  await writeFile(
    join(root, "site", "SHA256SUMS.txt"),
    `${sha256}  ${name}\n${"0".repeat(64)}  verify-codex-home-manager.ps1\n`
  );

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /SHA256SUMS file hash mismatch/i);
});

test("rejects fake Authenticode trust or a release that makes detached signatures optional", async () => {
  const root = await createReleaseFixture();
  await addExecutableRelease(root);
  const bundlePath = join(root, "site", "connector-release.json");
  const bundle = JSON.parse(await (await import("node:fs/promises")).readFile(bundlePath, "utf8"));
  bundle.artifacts[0].authenticode = {
    status: "valid",
    signerThumbprint: null,
    signerSubject: null,
    detachedSignatureRequired: false
  };
  await writeFile(bundlePath, JSON.stringify(bundle));

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /invalid Authenticode trust evidence or detached signature policy/i);
});

test("validates signed release metadata only against a separately pinned public key fingerprint", async () => {
  const root = await createReleaseFixture();
  const { fingerprint } = await addSignedReleaseMetadata(root);

  const result = runChecker(root, { CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256: fingerprint });

  assert.equal(result.status, 0, result.stderr);
});

test("rejects signed metadata when the private-root fingerprint pin or published artifact bytes drift", async () => {
  const root = await createReleaseFixture();
  const { fingerprint, name } = await addSignedReleaseMetadata(root);

  let result = runChecker(root, { CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256: `sha256:${"0".repeat(64)}` });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /pinned public key fingerprint/i);

  await writeFile(join(root, "site", name), "tampered artifact");
  result = runChecker(root, { CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256: fingerprint });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /signed manifest artifact hash mismatch/i);
});

test("rejects signed metadata without an independently pinned fingerprint in the public verifier", async () => {
  const root = await createReleaseFixture();
  const { fingerprint } = await addSignedReleaseMetadata(root);
  await writeFile(join(root, "site", "verify-codex-home-manager.ps1"), "Write-Host 'missing pin'\n");

  const result = runChecker(root, { CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256: fingerprint });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /public verifier.*fingerprint/i);
});

test("release mode rejects when all signed metadata is absent", async () => {
  const root = await createReleaseFixture();

  const result = runChecker(root, { CODEX_HOME_MANAGER_PUBLIC_RELEASE_MODE: "1" });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /release mode requires complete signed release metadata/i);
});

for (const missingName of [
  "release-manifest.json",
  "release-manifest.json.sig",
  "release-signing-public-key.pem",
  "release-signing-public-key.sha256"
]) {
  test(`release mode rejects missing ${missingName}`, async () => {
    const root = await createReleaseFixture();
    const { fingerprint } = await addSignedReleaseMetadata(root, missingName);

    const result = runChecker(root, {
      CODEX_HOME_MANAGER_PUBLIC_RELEASE_MODE: "1",
      CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256: fingerprint
    });

    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /release mode requires complete signed release metadata/i);
  });
}
