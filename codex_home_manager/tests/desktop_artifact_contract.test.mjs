import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { createArtifactContract, runFrontendBuild } from "../scripts/frontend-release-artifact.mjs";

test("frontend build reuses an admitted desktop lifecycle instead of reserving storage twice", () => {
  const calls = [];
  runFrontendBuild("C:\\fixture", {
    environment: { CODEX_STORAGE_RUN_ROOT: "D:\\managed-build" },
    platform: "win32",
    spawn: (command, args, options) => {
      calls.push({ command, args, options });
      return { status: 0 };
    }
  });
  assert.deepEqual(calls.map(({ args }) => args), [
    ["node_modules/typescript/bin/tsc", "-b", "--pretty", "false"],
    ["node_modules/vite/bin/vite.js", "build"]
  ]);
  assert.ok(calls.every(({ command, options }) => command === process.execPath && options.cwd === "C:\\fixture"));

  calls.length = 0;
  runFrontendBuild("C:\\fixture", {
    environment: {},
    platform: "win32",
    spawn: (command, args) => {
      calls.push({ command, args });
      return { status: 0 };
    }
  });
  assert.deepEqual(calls, [{ command: "cmd.exe", args: ["/d", "/s", "/c", "npm.cmd run build"] }]);
});

// Entire filesystem and build command are in memory; no dist, services or compiler are touched.
function fixture() {
  const directory = path.resolve("virtual-cm04");
  const files = new Map();
  const directories = new Set([directory]);
  const links = new Set();
  const writes = [];
  let revision = 0;
  let builds = 0;
  const absolute = (name) => path.resolve(directory, name);
  const missing = () => Object.assign(new Error("missing fixture path"), { code: "ENOENT" });
  function put(name, content) {
    const filename = absolute(name);
    files.set(filename, { data: Buffer.from(content), revision: ++revision });
    for (let parent = path.dirname(filename); parent !== path.dirname(parent); parent = path.dirname(parent)) directories.add(parent);
  }
  function metadata(filename) {
    filename = absolute(filename);
    if (!files.has(filename) && !directories.has(filename) && !links.has(filename)) throw missing();
    return { isFile: () => files.has(filename), isDirectory: () => directories.has(filename), isSymbolicLink: () => links.has(filename),
      size: files.get(filename)?.data.length ?? 0, mtimeMs: files.get(filename)?.revision ?? 0, ino: filename };
  }
  function copyTree(from, to) {
    from = absolute(from); to = absolute(to);
    if (!directories.has(from)) throw missing();
    for (const [filename, record] of [...files]) if (filename.startsWith(`${from}${path.sep}`)) put(path.join(to, path.relative(from, filename)), record.data);
  }
  const io = {
    lstat: async (filename) => metadata(filename),
    readdir: async (base) => {
      base = absolute(base);
      if (!directories.has(base)) throw missing();
      const names = new Set([...files.keys(), ...directories, ...links].filter((filename) => path.dirname(filename) === base));
      return [...names].map((filename) => ({ name: path.basename(filename), ...metadata(filename) }));
    },
    readFile: async (filename, encoding) => {
      const record = files.get(absolute(filename));
      if (!record) throw missing();
      return encoding ? record.data.toString(encoding) : record.data;
    },
    createReadStream: async function* (filename) {
      const record = files.get(absolute(filename));
      if (!record) throw missing();
      yield record.data;
    },
    mkdir: async (filename) => { writes.push("mkdir"); directories.add(absolute(filename)); },
    writeFile: async (filename, data) => { writes.push("write"); put(filename, data); },
    cp: async (from, to) => { writes.push("copy"); copyTree(from, to); },
    rename: async (from, to) => {
      from = absolute(from); to = absolute(to);
      if (!directories.has(from)) throw missing();
      writes.push("rename");
      copyTree(from, to);
      for (const filename of [...files.keys()]) if (filename.startsWith(`${from}${path.sep}`)) files.delete(filename);
      for (const name of [...directories]) if (name === from || name.startsWith(`${from}${path.sep}`)) directories.delete(name);
    },
  };
  const resources = Object.fromEntries(["service", "legacy", "skills", "runtime", "web"].map((name) => [`resources/${name}/`, `${name}/`]));
  for (const [name, content] of Object.entries({
    "index.html": "source html", "package.json": "{}", "package-lock.json": "{}", "vite.config.ts": "vite config", "tsconfig.json": "{}",
    "src/main.tsx": "main", "public/favicon.svg": "icon", "packaging/runtime/cockpit-maintenance.json": '{"revision":1}',
    "packaging/windows/requirements-connector.txt": "locked python", "scripts/frontend-release-artifact.mjs": "contract v2",
    "backend/maintenance.py": "backend", "src-tauri/native-service/src/main.rs": "native route", "src-tauri/Cargo.lock": "lock",
    "src-tauri/Cargo.toml": "workspace", "src-tauri/native-service/Cargo.toml": "service", "src-tauri/build.rs": "build script",
    "src-tauri/icons/icon.ico": "icon", "src-tauri/tauri.conf.json": JSON.stringify({ bundle: { resources } }),
  })) put(name, content);
  const options = { directory, io, environment: {}, toolchain: { node: "fixture", platform: "win32", arch: "x64" },
    now: () => `2026-09-07T00:00:${String(revision % 60).padStart(2, "0")}.000Z`,
    runBuild: async () => { builds++; put("dist/index.html", "built html"); put("dist/assets/main.js", "built js"); } };
  const contract = createArtifactContract(options);
  function bundle() {
    copyTree("build/frontend-release", "src-tauri/resources/web");
    copyTree("build/frontend-release", "src-tauri/resources/legacy/_internal/dist");
    copyTree("packaging/runtime", "src-tauri/resources/runtime");
    copyTree("packaging/runtime", "src-tauri/resources/legacy/_internal/packaging/runtime");
    put("src-tauri/resources/service/service.exe", "native binary");
    put("src-tauri/resources/legacy/legacy.exe", "legacy binary");
    put("src-tauri/resources/legacy/_internal/runtime.dll", "python runtime");
    put("src-tauri/resources/skills/SKILL.md", "skill");
  }
  const manifest = async (base = "build/frontend-release") => JSON.parse(await io.readFile(absolute(`${base}/frontend-release-manifest.json`), "utf8"));
  return { contract, options, files, links, writes, put, absolute, bundle, manifest, builds: () => builds };
}

test("fresh build issues one receipt; verification/restaging preserve original builtAt", async () => {
  const f = fixture();
  const digest = await f.contract.build();
  const original = await f.manifest();
  assert.equal(f.builds(), 1);
  assert.equal(original.schemaVersion, 2);
  assert.equal(await f.contract.verify(), digest);
  assert.equal(await f.contract.verifyBuild(), digest);
  assert.equal(await f.contract.stage(), digest);
  assert.equal((await f.manifest()).builtAt, original.builtAt);
  assert.equal((await f.manifest("dist")).builtAt, original.builtAt);
});

test("build reuse rejects changed source or changed build bytes", async () => {
  const f = fixture();
  await f.contract.build();
  f.put("dist/index.html", "mutated output");
  await assert.rejects(f.contract.verifyBuild(), /build receipt/);
  const changed = fixture();
  await changed.contract.build();
  changed.put("src/main.tsx", "changed source");
  await assert.rejects(changed.contract.verifyBuild(), /toolchain changed/);
});

test("frontend staging keeps one fixed rollback generation and refuses unbounded copies", async () => {
  const f = fixture();
  await f.contract.build();
  f.put("src/main.tsx", "second source generation");
  await f.contract.build();
  assert.ok(f.files.has(f.absolute("build/frontend-release.rollback/index.html")));
  assert.equal([...f.files.keys()].filter((name) => name.includes("frontend-release.rollback")).length > 0, true);
  f.put("src/main.tsx", "third source generation");
  await assert.rejects(f.contract.build(), /rollback slot is occupied/);
  assert.equal([...f.files.keys()].some((name) => name.includes("frontend-release.previous-")), false);
});

test("old dist without a trusted build receipt cannot be staged or refreshed", async () => {
  const f = fixture();
  f.put("dist/index.html", "old dist");
  await assert.rejects(f.contract.stage());
  f.put("dist/frontend-release-manifest.json", JSON.stringify({ schemaVersion: 1, builtAt: "2026-01-01" }));
  await assert.rejects(f.contract.stage(), /No trusted/);
  assert.deepEqual(f.writes, []);
  assert.equal(f.builds(), 0);
});

for (const name of ["index.html", "vite.config.ts", "tsconfig.json", "tsconfig.app.json", "package-lock.json", "public/favicon.svg", "packaging/runtime/cockpit-maintenance.json"]) {
  test(`frontend reuse rejects changed input: ${name}`, async () => {
    const f = fixture();
    await f.contract.build();
    f.writes.length = 0;
    f.put(name, "changed input");
    await assert.rejects(f.contract.verify(), /sources or toolchain changed/);
    await assert.rejects(f.contract.stage(), /sources or toolchain changed/);
    assert.deepEqual(f.writes, []);
  });
}

for (const name of ["index.html", "vite.config.ts", "tsconfig.json", "src-tauri/Cargo.lock", "src-tauri/build.rs", "src-tauri/native-service/Cargo.toml", "src-tauri/icons/icon.ico", "backend/maintenance.py", "src-tauri/native-service/src/main.rs", "packaging/runtime/cockpit-maintenance.json", "packaging/windows/requirements-connector.txt"]) {
  test(`shared desktop source contract binds ${name}`, async () => {
    const f = fixture();
    const before = await f.contract.sourceDigest("desktop");
    f.put(name, "changed input");
    assert.notEqual(await f.contract.sourceDigest("desktop"), before);
  });
}

test("generated resources, caches and unrelated root logs do not change source identity", async () => {
  const f = fixture();
  const before = await f.contract.sourceDigest("desktop");
  for (const name of ["src-tauri/target/file", "src-tauri/resources/service/new.exe", "src-tauri/gen/schemas/desktop.json", "backend/__pycache__/module.pyc", "tsconfig.tsbuildinfo", "server.log", "session_index.jsonl"]) f.put(name, "generated");
  assert.equal(await f.contract.sourceDigest("desktop"), before);
});

for (const change of ["extra", "missing", "corrupt", "unsafe-receipt", "old-schema"]) {
  test(`frontend exact tree verification rejects ${change}`, async () => {
    const f = fixture();
    await f.contract.build();
    if (change === "extra") f.put("build/frontend-release/assets/extra.js", "extra");
    if (change === "missing") f.files.delete(f.absolute("build/frontend-release/assets/main.js"));
    if (change === "corrupt") f.put("build/frontend-release/assets/main.js", "changed");
    if (change === "unsafe-receipt" || change === "old-schema") {
      const manifest = await f.manifest();
      if (change === "unsafe-receipt") manifest.files = ["../outside"];
      else manifest.schemaVersion = 1;
      f.put("build/frontend-release/frontend-release-manifest.json", JSON.stringify(manifest));
    }
    await assert.rejects(f.contract.verify(), /artifact tree|No trusted/);
  });
}

test("a failed build or concurrent source change cannot issue build evidence", async () => {
  for (const fail of [true, false]) {
    const f = fixture();
    const contract = createArtifactContract({ ...f.options, runBuild: async () => {
      if (fail) throw new Error("build failed");
      f.put("packaging/runtime/cockpit-maintenance.json", "changed during build");
    } });
    await assert.rejects(contract.build(), /build failed|inputs changed/);
    assert.deepEqual(f.writes, []);
  }
});

test("toolchain and Vite environment changes invalidate frontend build evidence", async () => {
  const f = fixture();
  await f.contract.build();
  await assert.rejects(createArtifactContract({ ...f.options, toolchain: { node: "different" } }).verify(), /toolchain changed/);
  await assert.rejects(createArtifactContract({ ...f.options, environment: { VITE_BACKEND: "different" } }).verify(), /toolchain changed/);
});

test("files arriving during artifact verification cannot escape the receipt inventory", async () => {
  const f = fixture();
  await f.contract.build();
  const io = { ...f.options.io, createReadStream: async function* (filename) {
    yield await f.options.io.readFile(filename);
    if (filename.endsWith(path.join("frontend-release", "assets", "main.js"))) f.put("build/frontend-release/assets/late.js", "late content");
  } };
  await assert.rejects(createArtifactContract({ ...f.options, io }).verify(), /inventory changed/);
});

test("artifact/source links and in-flight file mutations fail closed", async () => {
  const f = fixture();
  await f.contract.build();
  f.links.add(f.absolute("build/frontend-release"));
  await assert.rejects(f.contract.verify(), /regular artifact directory/);
  f.links.clear();
  f.links.add(f.absolute("src/main.tsx"));
  await assert.rejects(f.contract.sourceDigest(), /Symbolic link/);
  f.links.clear();
  const io = { ...f.options.io, createReadStream: async function* (filename) {
    yield await f.options.io.readFile(filename);
    f.put(filename, "changed while read");
  } };
  await assert.rejects(createArtifactContract({ ...f.options, io }).sourceDigest(), /changed while hashing/);
});

test("desktop artifacts bind every mapped resource, including binary runtime dependencies", async () => {
  const f = fixture();
  await f.contract.build();
  f.bundle();
  const first = await f.contract.desktopArtifacts();
  f.put("src-tauri/resources/service/service.exe", "new binary");
  const second = await f.contract.desktopArtifacts();
  assert.notEqual(second, first);
  f.put("src-tauri/resources/legacy/_internal/runtime.dll", "new runtime");
  const third = await f.contract.desktopArtifacts();
  assert.notEqual(third, second);
  f.put("src-tauri/resources/service/extra.dat", "unlisted in old receipts");
  assert.notEqual(await f.contract.desktopArtifacts(), third);
});

for (const name of ["legacy/_internal/packaging/runtime/cockpit-maintenance.json", "runtime/cockpit-maintenance.json", "web/assets/main.js", "legacy/_internal/dist/assets/main.js"]) {
  test(`desktop verification rejects stale embedded content: ${name}`, async () => {
    const f = fixture();
    await f.contract.build();
    f.bundle();
    f.put(`src-tauri/resources/${name}`, "stale embedded content");
    await assert.rejects(f.contract.desktopArtifacts(), /Embedded resource tree differs/);
  });
}
