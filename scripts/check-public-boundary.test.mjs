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
  await writeFile(join(root, "README.md"), [
    "https://codex-home-manager.simplezion.com/downloads/latest/windows-x64.exe",
    "https://github.com/SimpleZion/codex-home-manager/releases/latest",
    "https://codex-home-manager.simplezion.com/SHA256SUMS.txt",
    "https://codex-home-manager.simplezion.com/release-manifest.json.sig",
    "https://codex-home-manager.simplezion.com/release-signing-public-key.pem"
  ].join("\n") + "\n");
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

async function addSignedReleaseMetadata(root, missingName = null, mutateManifest = null) {
  const { name } = await addExecutableRelease(root);
  const artifactPath = join(root, "site", name);
  const bundlePath = join(root, "site", "connector-release.json");
  const checksumPath = join(root, "site", "SHA256SUMS.txt");
  const sourceEvidenceContent = new Map([
    ["codex-home-manager-source.zip", Buffer.from("public source archive")],
    ["codex-home-manager-source.cdx.json", Buffer.from('{"bomFormat":"CycloneDX","specVersion":"1.6","serialNumber":"urn:uuid:test"}\n')],
    ["source-ci-test-summary.md", Buffer.from("# Source CI test summary\n\n- Tests: 12\n- Failures: 0\n- Errors: 0\n")],
    ["source-provenance-attestation.sigstore.json", Buffer.from('{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n')],
    ["source-sbom-attestation.sigstore.json", Buffer.from('{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n')]
  ]);
  for (const [sourceName, content] of sourceEvidenceContent) {
    await writeFile(join(root, "site", sourceName), content);
  }
  const connectorChecksum = await (await import("node:fs/promises")).readFile(checksumPath, "utf8");
  const sourceChecksumLines = [...sourceEvidenceContent].map(([sourceName, content]) =>
    `${createHash("sha256").update(content).digest("hex")}  ${sourceName}`
  );
  await writeFile(checksumPath, connectorChecksum.trimEnd() + "\n" + sourceChecksumLines.join("\n") + "\n");
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  const publicKeyPem = publicKey.export({ type: "spki", format: "pem" });
  const fingerprint = `sha256:${createHash("sha256").update(publicKey.export({ type: "spki", format: "der" })).digest("hex")}`;
  const records = await Promise.all([artifactPath, bundlePath, checksumPath, ...[...sourceEvidenceContent.keys()].map((sourceName) => join(root, "site", sourceName))].map(async (path) => {
    const content = await (await import("node:fs/promises")).readFile(path);
    return { path: path.split(/[\\/]/).at(-1), sha256: createHash("sha256").update(content).digest("hex"), size: content.length };
  }));
  const publicSiteDistRecords = await Promise.all([
    join(root, "site", "assets", "app-abc123.js"),
    join(root, "site", "index.html")
  ].map(async (path) => {
    const content = await (await import("node:fs/promises")).readFile(path);
    return {
      path: path.slice(join(root, "site").length + 1).replaceAll("\\", "/"),
      sha256: createHash("sha256").update(content).digest("hex"),
      size: content.length
    };
  }));
  const manifest = {
    schema_version: 4,
    sources: { build: { root: { head: "a".repeat(40), branch: "main", clean: true }, manager: { head: "b".repeat(40), branch: "main", clean: true } }, artifact_public: { head: "c".repeat(40), branch: "main", clean: true } },
    public_key_fingerprint: fingerprint,
    dist_files: publicSiteDistRecords,
    public_site_dist_files: publicSiteDistRecords.map((record) => ({ ...record })),
    public_artifacts: records,
    source_evidence: {
      schema_version: 1,
      source_commit: "d".repeat(40),
      source_ref: "refs/heads/source",
      source_commits: { root: "a".repeat(40), manager: "b".repeat(40) },
      repository: "example/project",
      signer_workflow: "github.com/example/project/.github/workflows/source-ci.yml",
      attestations: {
        verifier: "gh attestation verify",
        deny_self_hosted_runners: true,
        sbom_predicate_type: "https://cyclonedx.org/bom",
        provenance_predicate_type: "https://slsa.dev/provenance/v1"
      },
      quality: { tests: 12, failures: 0, errors: 0, skipped: 1, pytest_seconds: 2.5 },
      assets: [...sourceEvidenceContent].map(([sourceName, content]) => ({
        name: sourceName,
        sha256: createHash("sha256").update(content).digest("hex"),
        size: content.length
      })).sort((first, second) => first.name.localeCompare(second.name))
    },
    cloudflare: { artifact_deployment: { id: "7d7aeac7-23a7-4eca-bc4a-c76c515727c0", project: "codex-home-manager", branch: "main", public_commit: "c".repeat(40), url: "https://artifact.codex-home-manager.pages.dev", status: "success" } },
    github: {
      release_id: 42,
      repository: "example/project",
      tag: "v1.0.0",
      html_url: "https://github.com/example/project/releases/tag/v1.0.0",
      draft_verified_before_signing: true,
      artifact_assets: [
        { name, sha256: createHash("sha256").update(await (await import("node:fs/promises")).readFile(artifactPath)).digest("hex"), size: (await (await import("node:fs/promises")).stat(artifactPath)).size },
        ...[...sourceEvidenceContent].map(([sourceName, content]) => ({
          name: sourceName,
          sha256: createHash("sha256").update(content).digest("hex"),
          size: content.length
        }))
      ].sort((first, second) => first.name.localeCompare(second.name)),
      metadata_assets: ["release-manifest.json", "release-manifest.json.sig", "release-signing-public-key.pem", "release-signing-public-key.sha256"]
    }
  };
  if (mutateManifest) mutateManifest(manifest);
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
  const sourceHeaders = [...sourceEvidenceContent.keys()].map((sourceName) => `/${sourceName}\n  Cache-Control: no-store, max-age=0`).join("\n");
  await writeFile(headerPath, `${existingHeaders.trimEnd()}\n${metadataHeaders}\n${sourceHeaders}\n`);
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

test("rejects a README that links to an unpublished GitHub latest asset alias", async () => {
  const root = await createReleaseFixture();
  await writeFile(
    join(root, "README.md"),
    "https://github.com/SimpleZion/codex-home-manager/releases/latest/download/codex-home-manager-local-win-x64.exe\n"
  );

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must not use a GitHub latest-download alias/i);
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

test("rejects public frontend bytes that drift from the signed dist mirror", async () => {
  const root = await createReleaseFixture();
  const { fingerprint } = await addSignedReleaseMetadata(root);
  await writeFile(join(root, "site", "assets", "app-abc123.js"), "console.info('drifted public UI');\n");

  const result = runChecker(root, { CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256: fingerprint });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /signed public site dist hash mismatch/i);
});

test("rejects source evidence bytes that drift from the signed manifest", async () => {
  const root = await createReleaseFixture();
  const { fingerprint } = await addSignedReleaseMetadata(root);
  await writeFile(join(root, "site", "codex-home-manager-source.cdx.json"), '{"bomFormat":"CycloneDX","tampered":true}\n');

  const result = runChecker(root, { CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256: fingerprint });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /source evidence hash mismatch|manifest artifact hash mismatch/i);
});

test("rejects a signed source evidence commit that differs from build sources", async () => {
  const root = await createReleaseFixture();
  const { fingerprint } = await addSignedReleaseMetadata(root, null, (manifest) => {
    manifest.source_evidence.source_commits.manager = "e".repeat(40);
  });

  const result = runChecker(root, { CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256: fingerprint });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /invalid source evidence proof/i);
});

test("rejects private local paths in source evidence intended for publication", async () => {
  const root = await createReleaseFixture();
  await writeFile(
    join(root, "site", "codex-home-manager-source.cdx.json"),
    JSON.stringify({ bomFormat: "CycloneDX", path: ["C:", "Users", "private-user", "project"].join("\\") })
  );
  const existingHeaders = await (await import("node:fs/promises")).readFile(join(root, "site", "_headers"), "utf8");
  await writeFile(join(root, "site", "_headers"), `${existingHeaders}\n/codex-home-manager-source.cdx.json\n  Cache-Control: no-store, max-age=0\n`);

  const result = runChecker(root);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /public boundary violation by pattern/i);
});

test("rejects a signed manifest whose build and public dist sets differ", async () => {
  const root = await createReleaseFixture();
  const { fingerprint } = await addSignedReleaseMetadata(root, null, (manifest) => {
    manifest.public_site_dist_files = manifest.public_site_dist_files.slice(1);
  });

  const result = runChecker(root, { CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256: fingerprint });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /signed build and public site dist records differ/i);
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
