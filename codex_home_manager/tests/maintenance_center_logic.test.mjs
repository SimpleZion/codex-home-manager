import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { build } from "esbuild";
import ts from "typescript";

const sourcePath = new URL("../src/MaintenanceCenter.tsx", import.meta.url);
const source = fs.readFileSync(sourcePath, "utf8");
const catalogSha = "a".repeat(64);
const fileSha = "b".repeat(64);
const otherSha = "c".repeat(64);
const bundle = await build({
  entryPoints: [fileURLToPath(sourcePath)], bundle: true, format: "esm", platform: "node", write: false,
  define: { __COCKPIT_CATALOG_SHA256__: JSON.stringify(catalogSha) },
  plugins: [{ name: "no-css-output", setup(api) { api.onLoad({ filter: /maintenance\.css$/ }, () => ({ contents: "", loader: "js" })); } }],
});
const logic = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString("base64")}`);
const syntax = ts.createSourceFile("MaintenanceCenter.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const component = syntax.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "MaintenanceCenter");

// Execute the production handlers, not copies of their logic or a browser/hooks emulator.
function expression(name) {
  let found;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(syntax) === name) found = node.initializer;
    ts.forEachChild(node, visit);
  }
  visit(component);
  assert.ok(found, `production handler ${name} exists`);
  return found.getText(syntax);
}
function evaluate(text, context) {
  const javascript = ts.transpileModule(`(${text})`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return vm.runInNewContext(javascript, context);
}
function handler(name, context) { return evaluate(expression(name), context); }
const statusEffect = component.body.statements.filter(ts.isExpressionStatement).map((node) => node.expression)
  .find((node) => ts.isCallExpression(node) && node.expression.getText(syntax) === "React.useEffect" && node.arguments[0].getText(syntax).includes("setObservation(null)"))?.arguments[0];
assert.ok(statusEffect);
const download = syntax.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "downloadPlan");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const target = { catalogSha256: catalogSha, entryId: "entry-a", evidenceIndex: 0, path: "docs/a.md" };
const fingerprint = { ...target, label: "A", availability: "present", sha256: fileSha, hashStatus: "complete", observedAt: "2026-09-07" };
const artifact = { availability: "present", path: "D:/ready/package A.msix", expectedSha256: fileSha, bytes: 4096 };
const verifiedArtifact = { ...artifact, actualSha256: fileSha, hashMatches: true, observedAt: "2026-09-07", runtimeVerified: false };
function context(readJson) {
  const state = { fingerprints: {}, hashResults: {}, artifactVerification: null, runtimeObservation: null, runtimeBusy: false, artifactBusy: false, error: "", observation: null, loading: false };
  const controller = new AbortController();
  const value = {
    ...logic, URLSearchParams, AbortController, Set, __COCKPIT_CATALOG_SHA256__: catalogSha,
    connected: true, loading: false, catalogMatches: true, sourceWorkspace: "D:/source & evidence", codexHome: "D:/home-a", scope: "view-a",
    currentScope: { current: "view-a" }, currentHome: { current: "D:/home-a" }, requestScope: { current: "view-a" },
    requestController: { current: controller }, verificationInFlight: { current: new Set() }, runtimeRequests: logic.createMaintenanceRequestPool(),
    entry: { id: target.entryId, evidence: [{ path: target.path, label: "A" }] },
    liveEntry: { evidence: [{ path: target.path, availability: "present" }] }, preparedPackage: { ...artifact }, readJson, state,
  };
  for (const key of Object.keys(state)) value[`set${key[0].toUpperCase()}${key.slice(1)}`] = (next) => { state[key] = typeof next === "function" ? next(state[key]) : next; };
  value.requestIsCurrent = handler("requestIsCurrent", value);
  return value;
}
async function exportedPlan(value) {
  let blob;
  const exportContext = {
    Blob, URL: { createObjectURL: (input) => { blob = input; return "in-memory-plan"; }, revokeObjectURL() {} },
    document: { createElement: () => ({ click() {} }) }, window: { setTimeout() {} },
    catalogData: { updatedAt: "2026-09-07", safetyGates: [] }, __COCKPIT_CATALOG_SHA256__: catalogSha,
  };
  const exportPlan = evaluate(download.getText(syntax), exportContext);
  exportPlan(value.entry, null, "review", "", value.state.artifactVerification, Object.values(value.state.fingerprints), value.state.runtimeObservation);
  return JSON.parse(await blob.text());
}

test("evidence URL pins compiled catalog; all response identity fields must match", async () => {
  let url;
  const value = context(async (path) => { url = new URL(path, "http://test.invalid"); return fingerprint; });
  await handler("verifyEvidence", value)(0);
  assert.equal(url.searchParams.get("catalog_sha256"), catalogSha);
  assert.equal(url.searchParams.get("source_workspace"), value.sourceWorkspace);
  assert.equal(url.pathname, "/api/maintenance/evidence/entry-a/0");
  assert.equal(value.state.fingerprints["entry-a/0"].sha256, fileSha);
  for (const patch of [{ catalogSha256: otherSha }, { entryId: "entry-b" }, { evidenceIndex: 1 }, { evidenceIndex: "0" }, { path: "docs/b.md" }, { sha256: "not-a-digest" }, { hashStatus: "unavailable" }]) {
    assert.equal(logic.evidenceFingerprintMatches({ ...fingerprint, ...patch }, target), false);
  }
  assert.equal(logic.evidenceFingerprintMatches(null, target), false);
  assert.equal(logic.evidenceFingerprintMatches({ ...fingerprint, sha256: null, hashStatus: "size_limit" }, target), true);
});

test("catalog mismatch blocks the actual handler as well as the evidence button", async () => {
  let calls = 0;
  const value = context(async () => { calls++; return fingerprint; });
  value.catalogMatches = false;
  await handler("verifyEvidence", value)(0);
  assert.equal(calls, 0);
  assert.match(source, /disabled=\{!connected \|\| loading \|\| !catalogMatches \|\| live\?\.availability/);
});

test("service catalog/order changes cannot publish or export an old success", async () => {
  for (const patch of [{ catalogSha256: otherSha }, { path: "docs/b.md" }, { entryId: "entry-b" }, { evidenceIndex: 1 }]) {
    const pending = deferred();
    const value = context(() => pending.promise);
    value.state.fingerprints["entry-a/0"] = fingerprint;
    const run = handler("verifyEvidence", value)(0);
    assert.equal(Object.keys(value.state.fingerprints).length, 0, "old success removed before awaiting response");
    pending.resolve({ ...fingerprint, ...patch });
    await run;
    assert.match(value.state.hashResults["entry-a/0"], /^核验失败/);
    assert.deepEqual((await exportedPlan(value)).fingerprints, []);
  }
});

test("failed retry removes prior evidence success and rapid duplicate clicks are gated", async () => {
  const pending = deferred();
  let calls = 0;
  const value = context(() => { calls++; return pending.promise; });
  value.state.fingerprints["entry-a/0"] = fingerprint;
  const verify = handler("verifyEvidence", value);
  const first = verify(0);
  await verify(0);
  assert.equal(calls, 1);
  pending.reject(new Error("409 catalog changed"));
  await first;
  assert.deepEqual((await exportedPlan(value)).fingerprints, []);
  assert.equal(value.verificationInFlight.current.size, 0);
});

test("evidence callbacks retain abort, controller generation and scope checks", async () => {
  for (const invalidate of [
    (value) => value.requestController.current.abort(),
    (value) => { value.requestController.current = new AbortController(); },
    (value) => { value.currentScope.current = "another-home-or-source"; },
  ]) {
    const pending = deferred();
    const value = context(() => pending.promise);
    const run = handler("verifyEvidence", value)(0);
    invalidate(value);
    pending.resolve(fingerprint);
    await run;
    assert.equal(Object.keys(value.state.fingerprints).length, 0);
    assert.doesNotMatch(value.state.hashResults["entry-a/0"], /^SHA-256/);
  }
});

test("ready request pins displayed path/hash and checks bytes plus digest consistency", async () => {
  let url;
  const value = context(async (path) => { url = new URL(path, "http://test.invalid"); return verifiedArtifact; });
  await handler("verifyPackage", value)();
  assert.equal(url.searchParams.get("expected_path"), artifact.path);
  assert.equal(url.searchParams.get("expected_sha256"), artifact.expectedSha256);
  assert.equal(value.state.artifactVerification.hashMatches, true);
  for (const patch of [{ path: "D:/ready/package B.msix" }, { expectedSha256: otherSha }, { bytes: 4097 }, { bytes: "4096" }, { actualSha256: otherSha }, { actualSha256: "invalid" }, { hashMatches: false }]) {
    assert.equal(logic.artifactVerificationMatches({ ...verifiedArtifact, ...patch }, artifact), false);
  }
  assert.equal(logic.artifactVerificationMatches(null, artifact), false);
  assert.equal(logic.artifactVerificationMatches({ ...verifiedArtifact, actualSha256: otherSha, hashMatches: false }, artifact), true, "real mismatch may be shown as failure, never success");
});

test("ready A becoming B and verification errors clear previous exported success", async () => {
  for (const response of [
    { ...verifiedArtifact, path: "D:/ready/package B.msix" }, { ...verifiedArtifact, expectedSha256: otherSha },
    { ...verifiedArtifact, bytes: 4097 }, { ...verifiedArtifact, actualSha256: otherSha }, new Error("409 ready changed"),
  ]) {
    const pending = deferred();
    const value = context(() => pending.promise);
    value.state.artifactVerification = verifiedArtifact;
    const run = handler("verifyPackage", value)();
    assert.equal(value.state.artifactVerification, null);
    if (response instanceof Error) pending.reject(response); else pending.resolve(response);
    await run;
    assert.equal((await exportedPlan(value)).artifactVerification, null);
    assert.match(value.state.error, /^制品核验失败/);
    assert.equal(value.state.artifactBusy, false);
  }
});

test("ready target must be complete; duplicate clicks and stale callbacks cannot publish", async () => {
  for (const patch of [{ path: undefined }, { bytes: undefined }, { bytes: NaN }, { bytes: -1 }, { expectedSha256: undefined }, { availability: "missing" }]) {
    assert.equal(logic.canVerifyArtifact({ ...artifact, ...patch }), false);
    assert.throws(() => logic.readyArtifactVerificationUrl({ ...artifact, ...patch }));
  }
  const pending = deferred();
  let calls = 0;
  const value = context(() => { calls++; return pending.promise; });
  const verify = handler("verifyPackage", value);
  const first = verify();
  await verify();
  assert.equal(calls, 1);
  value.requestController.current.abort();
  pending.resolve(verifiedArtifact);
  await first;
  assert.equal(value.state.artifactVerification, null);
});

test("refresh runs the production effect without releasing the runtime in-flight lock", async () => {
  const pending = deferred();
  let runtimeCalls = 0;
  const value = context((path) => {
    if (path.startsWith("/api/maintenance/runtime?")) { runtimeCalls++; return pending.promise; }
    return Promise.resolve({ catalogSha256: catalogSha, entries: [], receipts: [] });
  });
  const run = handler("verifyRuntime", value)();
  await Promise.resolve();
  const runtimeSignal = value.runtimeRequests.has(value.codexHome);
  assert.equal(runtimeSignal, true);
  value.requestController.current.abort();
  const refresh = evaluate(statusEffect.getText(syntax), value);
  const cleanup = refresh();
  assert.equal(value.state.runtimeBusy, true, "metadata refresh must retain busy for pending home");
  await handler("verifyRuntime", value)();
  assert.equal(runtimeCalls, 1);
  pending.resolve({ checks: [{ id: "old-result" }] });
  await run;
  assert.equal(value.state.runtimeObservation, null, "pre-refresh result is obsolete");
  assert.equal(value.state.runtimeBusy, false);
  cleanup();
});

test("Home A/B/A keeps per-home ownership and rejects cross-home results", async () => {
  const a = deferred(), b = deferred();
  const urls = [];
  const value = context((path) => { urls.push(path); return path.includes("home-a") ? a.promise : b.promise; });
  const runA = handler("verifyRuntime", value)();
  await Promise.resolve();
  value.requestController.current.abort();
  const homeB = { ...value, codexHome: "D:/home-b", scope: "view-b", requestScope: { current: "view-b" } };
  homeB.currentScope.current = homeB.scope;
  homeB.currentHome.current = homeB.codexHome;
  homeB.requestController.current = new AbortController();
  homeB.requestIsCurrent = handler("requestIsCurrent", homeB);
  const runB = handler("verifyRuntime", homeB)();
  await Promise.resolve();
  assert.equal(homeB.runtimeRequests.has("D:/home-a"), true);
  assert.equal(homeB.runtimeRequests.has("D:/home-b"), true);
  homeB.requestController.current.abort();
  const backA = { ...value, scope: "view-a-new", requestScope: { current: "view-a-new" } };
  backA.currentScope.current = backA.scope;
  backA.currentHome.current = backA.codexHome;
  backA.requestController.current = new AbortController();
  backA.requestIsCurrent = handler("requestIsCurrent", backA);
  await handler("verifyRuntime", backA)();
  assert.equal(urls.length, 2, "return to A cannot start another A request");
  assert.equal(new URL(urls[0], "http://test.invalid").searchParams.get("codex_home"), "D:/home-a");
  assert.equal(new URL(urls[1], "http://test.invalid").searchParams.get("codex_home"), "D:/home-b");
  b.resolve({ checks: [{ id: "home-b" }] });
  await runB;
  assert.equal(backA.state.runtimeObservation, null);
  a.resolve({ checks: [{ id: "home-a-old" }] });
  await runA;
  assert.equal(backA.state.runtimeObservation, null);
  assert.equal(backA.state.runtimeBusy, false);
});

test("pool abort holds ownership until settle; runtime failure permits a fresh retry", async () => {
  const pool = logic.createMaintenanceRequestPool();
  const pending = deferred();
  const flight = pool.start("home", () => pending.promise);
  await Promise.resolve();
  pool.abortAll();
  assert.equal(flight.controller.signal.aborted, true);
  assert.equal(pool.has("home"), true);
  assert.equal(pool.start("home", async () => "duplicate"), null);
  pending.resolve("old");
  await flight.promise;
  assert.equal(pool.has("home"), false);
  const value = context(async () => { throw new Error("runtime timeout"); });
  await handler("verifyRuntime", value)();
  assert.equal(value.state.runtimeBusy, false);
  assert.equal(value.runtimeRequests.has(value.codexHome), false);
  assert.match(value.state.error, /runtime timeout/);
});

test("unmount before dispatch does not start a runtime request", async () => {
  const pool = logic.createMaintenanceRequestPool();
  let calls = 0;
  const flight = pool.start("home", async () => { calls++; });
  pool.abortAll();
  await assert.rejects(flight.promise, { name: "AbortError" });
  assert.equal(calls, 0);
  assert.equal(pool.has("home"), false);
});

test("pagination labels receipt age separately from the fresh read time", () => {
  const observedAt = "2026-09-07T08:00:00Z", receiptObservedAt = "2000-01-01T01:00:00Z";
  const markup = renderToStaticMarkup(logic.RuntimeEvidenceTimes({
    observation: { observedAt, runtimeVerified: false, checks: [] },
    check: { id: "desktop-pagination-6511", state: "unverified", details: { receiptObservedAt, evidenceScope: "receipt_only", currentRuntimeVerified: false } },
  }));
  assert.ok(markup.includes(`<dt>本次读取时间</dt><dd>${new Date(observedAt).toLocaleString()}</dd>`));
  assert.ok(markup.includes(`<dt>历史回执时间</dt><dd>${new Date(receiptObservedAt).toLocaleString()}</dd>`));
  assert.match(markup, /仅为历史回执证据/);
  assert.match(markup, /未核验当前进程或修复是否生效/);
});

test("missing or invalid receipt timestamps never fall back to fresh observedAt", () => {
  for (const receiptObservedAt of [undefined, "", "invalid-date"]) {
    const markup = renderToStaticMarkup(logic.RuntimeEvidenceTimes({
      observation: { observedAt: "2026-09-07T08:00:00Z", runtimeVerified: false, checks: [] },
      check: { id: "desktop-pagination-6511", state: "unverified", details: { receiptObservedAt } },
    }));
    assert.match(markup, /<dt>历史回执时间<\/dt><dd>未提供或无法解析<\/dd>/);
    assert.match(markup, /本次读取不代表回执更新/);
  }
});
