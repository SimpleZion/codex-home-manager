import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { build } from "esbuild";

const projectRoot = path.resolve(import.meta.dirname, "..");
const bundle = await build({
  entryPoints: [path.join(projectRoot, "src", "main.tsx")],
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  plugins: [{
    name: "frontend-connector-test-stubs",
    setup(buildApi) {
      buildApi.onResolve({ filter: /styles\.css$/ }, () => ({ path: "styles.css", namespace: "stub" }));
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
  probeLocalConnector
} = await import(moduleUrl);
const packageVersion = JSON.parse(fs.readFileSync(path.join(projectRoot, "package.json"), "utf8")).version;
const mainSource = fs.readFileSync(path.join(projectRoot, "src", "main.tsx"), "utf8");
assert.match(mainSource, /"思考过程": "Progress updates"/);
assert.doesNotMatch(mainSource, /"思考过程": "Progress reasoning"/);
assert.match(mainSource, /useState<TimelineFilter>\("conversation"\)/, "main content must remain the default timeline filter");
const validCapabilities = {
  service: "codex-home-manager",
  version: packageVersion,
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
const englishTrust = connectorReleaseTrustMessage(selfSignedLikeMetadata, "en");
assert.match(englishTrust, /reports Authenticode status/);
assert.match(englishTrust, /does not prove public certificate trust/);
assert.match(englishTrust, /detached Ed25519 release manifest/);
assert.doesNotMatch(englishTrust, /this build is unsigned/i);
const chineseTrust = connectorReleaseTrustMessage(selfSignedLikeMetadata, "zh");
assert.match(chineseTrust, /不证明证书.*公共受信任代码签名/);
assert.match(chineseTrust, /固定公钥指纹/);
const unavailableTrust = connectorReleaseTrustMessage(null, "en");
assert.match(unavailableTrust, /not confirmed/);
assert.doesNotMatch(unavailableTrust, /this build is unsigned/i);

console.log("frontend connector contract tests passed");
