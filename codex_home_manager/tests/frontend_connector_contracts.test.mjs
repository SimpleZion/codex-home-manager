import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { build } from "esbuild";

const projectRoot = path.resolve(import.meta.dirname, "..");
const uiBehaviorSource = fs.readFileSync(path.join(projectRoot, "src", "uiBehavior.ts"), "utf8");
const bundle = await build({
  entryPoints: [path.join(projectRoot, "src", "main.tsx")],
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  plugins: [{
    name: "frontend-connector-test-stubs",
    setup(buildApi) {
      buildApi.onResolve({ filter: /\.css$/ }, () => ({ path: "styles.css", namespace: "stub" }));
      buildApi.onResolve({ filter: /^react-dom\/client$/ }, () => ({ path: "react-dom-client", namespace: "stub" }));
      buildApi.onResolve({ filter: /^sql\.js$/ }, () => ({ path: "sql.js", namespace: "stub" }));
      buildApi.onResolve({ filter: /sql-wasm\.wasm\?url$/ }, () => ({ path: "sql-wasm", namespace: "stub" }));
      buildApi.onLoad({ filter: /.*/, namespace: "stub" }, (args) => ({
        contents: args.path === "styles.css"
          ? ""
          : args.path === "react-dom-client"
            ? "export function createRoot() { return { render() {} }; }"
          : args.path === "sql.js"
          ? "export default async function initSqlJs() { throw new Error('not used'); }"
          : "export default 'sql-wasm-test-url';",
        loader: "js"
      }));
    }
  }]
});

globalThis.window = {
  location: {
    href: "https://codex-home-manager.simplezion.com/",
    hostname: "codex-home-manager.simplezion.com",
    origin: "https://codex-home-manager.simplezion.com"
  },
  localStorage: { getItem: () => null, setItem: () => {} },
  setTimeout,
  clearTimeout,
  innerWidth: 1440
};
globalThis.document = { getElementById: () => ({}) };

const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString("base64")}`;
const {
  capabilityResponseIsCompatible,
  connectorReleaseTrustMessage,
  parseConnectorReleaseMetadata,
  probeLocalConnector
} = await import(moduleUrl);
const packageVersion = JSON.parse(fs.readFileSync(path.join(projectRoot, "package.json"), "utf8")).version;
const mainSource = fs.readFileSync(path.join(projectRoot, "src", "main.tsx"), "utf8");
assert.match(mainSource, /"进度说明": "Progress updates"/);
assert.doesNotMatch(mainSource, /"思考过程": "Progress (?:updates|reasoning)"/);
assert.match(mainSource, /useState<TimelineFilter>\("conversation"\)/, "main content must remain the default timeline filter");
assert.match(mainSource, /\/prompts\/page\?\$\{params\.toString\(\)\}/, "local prompt reads must use the paged backend endpoint");
assert.doesNotMatch(mainSource, /fetchThreadPromptsFromLocalApi/, "the frontend must not retain the legacy full prompt loader");
assert.match(
  mainSource,
  /readBrowserThreadPrompts\(browserWorkspace, threadId,\s*\{/,
  "browser folder mode must retain its paged prompt reader with explicit options"
);
assert.match(mainSource, /if \(isBrowserPromptMode\)[\s\S]{0,300}copyTextToClipboard\(allPromptText\)/, "browser folder copy must remain in memory");
assert.match(mainSource, /response\.body\.getReader\(\)/, "connector copy must consume the streaming response body");
assert.match(mainSource, /maxClipboardBytes\s*=\s*16\s*\*\s*1024\s*\*\s*1024/, "clipboard copy must have a hard memory bound");
assert.match(mainSource, /showSaveFilePicker/, "complete prompt export must use a streaming file destination");
assert.match(mainSource, /await writer\.write\(value\)/, "complete prompt export must write response chunks without concatenating the full rollout");
assert.match(mainSource, /条匹配记录/, "timeline search must label counts as matching records rather than text occurrences");
assert.match(mainSource, /if \(filterMode === "focused"\) return "visible"/);
assert.match(mainSource, /if \(filterMode === "withAgents"\) return "with_agents"/);
assert.match(mainSource, /type FilterMode = "all" \| "archived";/, "legacy visibility states must not remain as primary thread filters");
assert.doesNotMatch(mainSource, /<h3>\{t\("复制线程"\)\}<\/h3>/, "current-home duplication must leave the management UI");
assert.doesNotMatch(mainSource, /<h3>\{t\("迁移线程"\)\}<\/h3>/, "thread migration must leave the management UI");
assert.doesNotMatch(mainSource, /<h3>\{t\("项目及文件夹改名"\)\}<\/h3>/, "project rename must leave the management UI");
assert.match(mainSource, /catalog_version: snapshot\?\.catalogVersion \|\| ""/, "thread filters must pin requests to the catalog already shown to the user");
assert.match(mainSource, /stableProjectCounts/, "project counts must survive an in-flight filter request");
assert.match(
  mainSource,
  /useHomeOverview\([\s\S]{0,180}activeSection === "resources"/,
  "the expensive CODEX_HOME resource walk must be lazy and must not block the thread landing page"
);
assert.match(
  mainSource,
  /useCapabilities\(language, isLocalApiConnected && activeSection === "api"\)/,
  "the full compatibility capability catalog must remain lazy"
);
assert.match(
  mainSource,
  /await fetchJson<CapabilityResponse>\("\/api\/capabilities", \{ cache: "no-store" \}\)/,
  "local connection detection must use the native read-only identity probe"
);
assert.doesNotMatch(
  mainSource,
  /const tokenPayload = await refreshApiToken\(\)/,
  "startup connection detection must not launch the legacy write authorization service"
);
assert.match(
  mainSource,
  /fetchJson<HealthPayload>\(`\/api\/health\?\$\{params\.toString\(\)\}`, \{ cache: "no-store" \}\)/,
  "health refresh must remain a read-only native request"
);
assert.doesNotMatch(
  mainSource,
  /fetchAuthorizedJson<HealthPayload>\(`\/api\/health/,
  "health refresh must not acquire a legacy write token"
);
assert.match(
  mainSource,
  /const detailRoute = isDesktopRuntime\(\) \? `\/api\/native\/threads\/\$\{threadId\}` : `\/api\/threads\/\$\{threadId\}`/,
  "the first thread detail shell must use the native catalog on desktop and the compatibility route in the local web console"
);
assert.match(
  mainSource,
  /const analysis = await fetchJson</,
  "read-only thread analysis must not acquire a write authorization token"
);
assert.match(
  mainSource,
  /const attempts = browserWorkspace \? 1 : 2;[\s\S]{0,1600}setDailyTokenError/,
  "automatic daily-token reads must retry a transient local compatibility-service failure"
);
assert.doesNotMatch(
  mainSource,
  /loadDailyTokenUsage[\s\S]{0,1800}setActionError/,
  "daily-token failures must stay inside the token panel instead of raising a global application error"
);
assert.match(
  mainSource,
  /if \(selectedThreadIdRef\.current !== thread\.id\)[\s\S]{0,260}dailyTokenRequestSequenceRef\.current \+= 1/,
  "double-clicking the same thread must not cancel its in-flight automatic daily-token retry"
);
assert.match(
  mainSource,
  /return request<ThreadTimeline>\(`\/api\/threads\/\$\{thread\.id\}\/timeline/,
  "thread timeline reads must remain behind the injected request boundary"
);
assert.match(
  mainSource,
  /fetchTimelineJson=\{fetchJson\}/,
  "default timeline and item reads must use the native read-only path without launching legacy authorization"
);
assert.match(
  mainSource,
  /fetchThreadPromptPageFromLocalApi\(fetchReadJson, thread, codexHome/,
  "paged prompt reads must use the read-only JSON boundary"
);
assert.match(
  mainSource,
  /fetchThreadPromptCopyStream\(fetchReadResponse, thread, codexHome/,
  "streamed prompt exports must use the read-only response boundary"
);
assert.match(
  mainSource,
  /cancelThreadTimelineSearchRequest\(fetchReadResponse/,
  "same-origin runtime cancellation must not acquire a persistent-write token"
);
assert.match(
  mainSource,
  /fetchReadJson<PromptIndexStatus>/,
  "prompt index status must use the read-only compatibility boundary"
);
assert.doesNotMatch(
  mainSource,
  /Boolean\(apiToken\)[\s\S]{0,120}activeSection === "resources"/,
  "resource discovery must not wait for write authorization"
);
assert.match(
  mainSource,
  /useHomeOverview\([\s\S]{0,180}fetchJson/,
  "resource discovery must use the same-origin read-only request path"
);
for (const previewPath of [
  "/api/resources/write/preview",
  "/api/import/thread/preview",
  "/api/import/project/preview",
  "/api/resources/copy-from-home/preview"
]) {
  const escapedPath = previewPath.replaceAll("/", "\\/");
  assert.match(
    mainSource,
    new RegExp(`fetchAuthorizedJson<ImpactPreview>\\(\\\`${escapedPath}`),
    `${previewPath} must use transparent local authorization`
  );
}
assert.match(
  mainSource,
  /params\.set\("catalog_version", threadPage\.catalogVersion\)/,
  "manual pagination must keep using the immutable catalog that produced its cursor"
);
assert.match(
  uiBehaviorSource,
  /if \(catalogVersion\) params\.set\("catalog_version", catalogVersion\)/,
  "automatic depth restoration must keep using the immutable catalog that produced its cursor"
);
const validCapabilities = {
  service: "codex-home-manager",
  version: packageVersion,
  frontendContractVersion: 2,
  language: "en",
  openapiPath: "/openapi.json",
  mcpPath: "/mcp",
  safetyModel: {},
  commonQueryParameters: {},
  capabilities: []
};

assert.equal(capabilityResponseIsCompatible(validCapabilities), true);
assert.equal(capabilityResponseIsCompatible({ ...validCapabilities, service: "another-service" }), false);
assert.equal(capabilityResponseIsCompatible({ ...validCapabilities, version: "2.0.0" }), false);
assert.equal(capabilityResponseIsCompatible({ ...validCapabilities, version: "1" }), false);
assert.equal(capabilityResponseIsCompatible({ ...validCapabilities, openapiPath: "/other" }), false);

let jsonReads = 0;
globalThis.fetch = async () => ({
  ok: true,
  status: 200,
  json: async () => { jsonReads += 1; return validCapabilities; }
});
assert.equal(await probeLocalConnector(), true);
assert.equal(jsonReads, 1, "a successful probe must parse the capability payload");

globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => { throw new Error("must not parse"); } });
assert.equal(await probeLocalConnector(), false, "401 is not proof that Codex Home Manager exists");
globalThis.fetch = async () => ({ ok: false, status: 403, json: async () => { throw new Error("must not parse"); } });
assert.equal(await probeLocalConnector(), false, "403 is not proof that Codex Home Manager exists");
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ message: "arbitrary localhost service" }) });
assert.equal(await probeLocalConnector(), false, "an arbitrary successful localhost response must be rejected");
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ...validCapabilities, version: "2.0.0" }) });
assert.equal(await probeLocalConnector(), false, "an incompatible connector version must be rejected");

const selfSignedLikeMetadata = {
  schemaVersion: 1,
  version: packageVersion,
  artifacts: [{
    name: "connector.exe",
    kind: "exe",
    authenticode: {
      status: "valid",
      signerSubject: "CN=Local Self-Signed Test",
      detachedSignatureRequired: true
    }
  }]
};
assert.equal(parseConnectorReleaseMetadata(selfSignedLikeMetadata), selfSignedLikeMetadata, "schema 1 release metadata must remain compatible");
const englishTrust = connectorReleaseTrustMessage(selfSignedLikeMetadata, "en");
assert.match(englishTrust, /reports Authenticode status/);
assert.match(englishTrust, /does not prove public certificate trust/);
assert.match(englishTrust, /published SHA-256/);
assert.doesNotMatch(englishTrust, /this build is unsigned/i);
const chineseTrust = connectorReleaseTrustMessage(selfSignedLikeMetadata, "zh");
assert.match(chineseTrust, /不证明证书.*公共受信任代码签名/);
assert.match(chineseTrust, /SHA-256/);
const unavailableTrust = connectorReleaseTrustMessage(null, "en");
assert.match(unavailableTrust, /verification metadata is unavailable/);
assert.doesNotMatch(unavailableTrust, /this build is unsigned/i);

const schemaTwoUnavailableMetadata = {
  schemaVersion: 2,
  version: packageVersion,
  artifacts: [{
    name: "connector.exe",
    kind: "exe",
    sha256: "a".repeat(64),
    authenticode: {
      status: "unavailable",
      trust: "none",
      signerThumbprint: null,
      signerSubject: null,
      selfSigned: false,
      chainTrusted: false,
      detachedSignatureRequired: true
    }
  }]
};
assert.equal(parseConnectorReleaseMetadata(schemaTwoUnavailableMetadata), schemaTwoUnavailableMetadata, "schema 2 unavailable/none release metadata must parse as a valid published state");
const schemaTwoTrust = connectorReleaseTrustMessage(schemaTwoUnavailableMetadata, "en");
assert.match(schemaTwoTrust, /no trusted Authenticode signature/);
assert.match(schemaTwoTrust, /published SHA-256/);
assert.equal(parseConnectorReleaseMetadata({ ...schemaTwoUnavailableMetadata, schemaVersion: 3 }), null, "unknown release metadata schemas must be rejected");
assert.equal(parseConnectorReleaseMetadata({ ...schemaTwoUnavailableMetadata, version: "999.0.0" }), null, "release metadata for another frontend version must be rejected");
assert.equal(parseConnectorReleaseMetadata({ ...schemaTwoUnavailableMetadata, artifacts: [{ name: "connector.exe", kind: "exe", authenticode: { status: "unavailable", trust: 0 } }] }), null, "malformed Authenticode trust metadata must be rejected");
assert.equal(parseConnectorReleaseMetadata({ ...schemaTwoUnavailableMetadata, artifacts: [{ name: "connector.exe", kind: "exe", sha256: "short" }] }), null, "malformed SHA-256 metadata must be rejected");
assert.doesNotMatch(mainSource, /localConnectorVerifierDownloadUrl/, "the UI must not advertise the detached verifier when its manifest is not published");
assert.match(mainSource, /publishedExecutable\?\.sha256/, "the UI must expose the exact published executable checksum");

console.log("frontend connector contract tests passed");
