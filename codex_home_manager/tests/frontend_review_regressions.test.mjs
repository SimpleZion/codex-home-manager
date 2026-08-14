import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { build } from "esbuild";

const projectRoot = path.resolve(import.meta.dirname, "..");

async function importBrowserBundle(entryPoint, mainBundle = false) {
  const bundle = await build({
    entryPoints: [path.join(projectRoot, "src", entryPoint)],
    bundle: true,
    format: "esm",
    platform: "browser",
    write: false,
    plugins: [{
      name: "frontend-review-test-stubs",
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
                ? "export default async function initSqlJs() { throw new Error('sql.js is not used in this test'); }"
                : "export default 'sql-wasm-test-url';",
          loader: "js"
        }));
      }
    }]
  });
  if (mainBundle) {
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
  }
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString("base64")}`;
  return import(moduleUrl);
}

const promptModule = await importBrowserBundle("promptClassification.ts");
const goal = promptModule.classifyPromptText("<goal_context>\ncontinue the active goal\n</goal_context>");
assert.equal(goal.sourceType, "goal");
assert.equal(goal.sourceLabel, "续跑目标上下文");
assert.equal(goal.visibleByDefault, false);
assert.equal(goal.hasPureText, false);

const timelineModule = await importBrowserBundle("threadTimeline.ts");
const svgTimelineItem = timelineModule.timelineItemFromJson({
  type: "response_item",
  payload: {
    type: "custom_tool_call_output",
    call_id: "svg-output",
    output: "preview=data:image/svg+xml,<svg viewBox='0 0 20 20'><text>PRIVATE SVG VALUE</text></svg>"
  }
}, 1);
assert.doesNotMatch(svgTimelineItem.text, /PRIVATE SVG VALUE|<svg|viewBox/);
assert.match(svgTimelineItem.text, /附件内容已隐藏/);

let timelinePageNumber = 0;
const boundedTimeline = await timelineModule.scanBrowserTimelineSearchPages(async (beforeByte) => {
  timelinePageNumber += 1;
  const pageStart = beforeByte ?? 1_000;
  return {
    threadId: "bounded",
    title: "Bounded",
    rolloutPath: "bounded.jsonl",
    fileSize: 1_000,
    beforeByte,
    nextBeforeByte: pageStart - 100,
    limit: 80,
    kind: "conversation",
    search: "needle",
    hasMore: true,
    scannedRecords: 100,
    scannedBytes: 100,
    scanLimited: true,
    skippedOversizedRecords: 0,
    recoveredOversizedRecords: 0,
    items: Array.from({ length: 60 }, (_, index) => ({
      id: `page-${timelinePageNumber}-${index}`,
      byteOffset: pageStart - index,
      kind: "user",
      label: "用户输入",
      text: `needle ${timelinePageNumber}-${index}`,
      characterCount: 10,
      textTruncated: false,
      timestamp: null,
      timestampMs: 0,
      sourceType: "response_item",
      payloadType: "message",
      phase: "",
      callId: "",
      readable: true,
      encrypted: false,
      hasEncryptedContent: false,
      promptSourceType: "user"
    })),
    pageCounts: { user: 60 }
  };
}, { maxItems: 80 });
assert.equal(timelinePageNumber, 2);
assert.equal(boundedTimeline.items.length, 60);
assert.equal(boundedTimeline.hasMore, true);
assert.equal(boundedTimeline.nextBeforeByte, 900);

const browserModule = await importBrowserBundle("browserHome.ts");
function browserWorkspace(threadId, file) {
  return {
    mode: "browser_folder",
    snapshot: { threads: [{ id: threadId, title: "Paged prompts" }] },
    threadFiles: new Map([[threadId, {
      relativePath: `sessions/rollout-${threadId}.jsonl`,
      archivedStore: false,
      handle: { getFile: async () => file }
    }]])
  };
}

const promptRecords = Array.from({ length: 137 }, (_, index) => ({
  type: "response_item",
  timestamp: `2026-08-14T00:${String(index % 60).padStart(2, "0")}:00.000Z`,
  payload: { type: "message", role: "user", content: [{ type: "input_text", text: `paged needle ${index}` }] }
}));
const promptFile = new File([`${promptRecords.map((record) => JSON.stringify(record)).join("\n")}\n`], "paged.jsonl");
const workspace = browserWorkspace("paged", promptFile);
let cursor = null;
let loadedPromptCount = 0;
let pageCount = 0;
do {
  const page = await browserModule.readBrowserThreadPrompts(workspace, "paged", {
    cursor,
    limit: 25,
    scope: "all",
    search: "needle"
  });
  assert.ok(page.prompts.length <= 25, "browser prompt pages must remain bounded");
  loadedPromptCount += page.prompts.length;
  pageCount += 1;
  cursor = page.nextCursor;
} while (cursor);
assert.equal(loadedPromptCount, 137);
assert.ok(pageCount > 1);

const svgPromptText = [
  "![diagram](data:image/svg+xml,",
  "<svg viewBox='0 0 10 10'>",
  "<text>PRIVATE XML CONTENT</text>",
  "</svg>",
  ")",
  "",
  "## My request for Codex:",
  "keep this real request"
].join("\n");
const svgPromptFile = new File([`${JSON.stringify({
  type: "response_item",
  payload: { type: "message", role: "user", content: [{ type: "input_text", text: svgPromptText }] }
})}\n`], "svg-prompt.jsonl");
const svgPromptPage = await browserModule.readBrowserThreadPrompts(browserWorkspace("svg", svgPromptFile), "svg");
assert.doesNotMatch(svgPromptPage.prompts[0].text, /PRIVATE XML CONTENT|<svg|viewBox/);
assert.equal(svgPromptPage.prompts[0].pureText, "keep this real request");

function notFound() {
  const error = new Error("not found");
  error.name = "NotFoundError";
  throw error;
}
const slowSessions = {
  kind: "directory",
  name: "sessions",
  async *entries() {
    for (let index = 0; index < 100; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1));
      yield [`rollout-${index}.jsonl`, { kind: "file", name: `rollout-${index}.jsonl`, getFile: async () => new File([], `rollout-${index}.jsonl`) }];
    }
  },
  getDirectoryHandle: async () => notFound(),
  getFileHandle: async () => notFound()
};
const slowRoot = {
  kind: "directory",
  name: ".codex",
  queryPermission: async () => "granted",
  requestPermission: async () => "granted",
  async *entries() {},
  getDirectoryHandle: async (name) => name === "sessions" ? slowSessions : notFound(),
  getFileHandle: async () => notFound()
};
const scanController = new AbortController();
const scanPromise = browserModule.scanBrowserCodexHome(slowRoot, 50, "en", scanController.signal);
setTimeout(() => scanController.abort(), 8);
await assert.rejects(scanPromise, (error) => error instanceof DOMException && error.name === "AbortError");

const mainModule = await importBrowserBundle("main.tsx", true);
const packageVersion = JSON.parse(fs.readFileSync(path.join(projectRoot, "package.json"), "utf8")).version;
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
assert.equal(mainModule.capabilityResponseIsCompatible(validCapabilities), true);
assert.equal(mainModule.capabilityResponseIsCompatible({ ...validCapabilities, frontendContractVersion: 1 }), false);
const { frontendContractVersion: _removedContract, ...missingContract } = validCapabilities;
assert.equal(mainModule.capabilityResponseIsCompatible(missingContract), false);

const mainSource = fs.readFileSync(path.join(projectRoot, "src", "main.tsx"), "utf8");
assert.match(mainSource, /useVirtualizer\(/, "virtual lists must remain enabled");
assert.match(mainSource, /event\.ctrlKey \|\| event\.metaKey/, "Ctrl+F interception must remain enabled");
assert.match(mainSource, /clearSearchOnEscape/, "Escape search clearing must remain enabled");
assert.match(mainSource, /await writer\.write\(value\)/, "connector copy downloads must remain streamed");
assert.match(mainSource, /localizedPromptSourceLabel\(prompt, language\)/, "dynamic source labels must use the translation mapping");
assert.doesNotMatch(mainSource, />\{prompt\.sourceLabel\}</, "raw dynamic source labels must not bypass translation");

console.log("frontend review regression tests passed");
