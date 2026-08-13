import assert from "node:assert/strict";
import path from "node:path";
import { build } from "esbuild";

const projectRoot = path.resolve(import.meta.dirname, "..");
const bundle = await build({
  entryPoints: [path.join(projectRoot, "src", "browserHome.ts")],
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  plugins: [{
    name: "browser-home-test-stubs",
    setup(buildApi) {
      buildApi.onResolve({ filter: /^sql\.js$/ }, () => ({ path: "sql.js", namespace: "stub" }));
      buildApi.onResolve({ filter: /sql-wasm\.wasm\?url$/ }, () => ({ path: "sql-wasm", namespace: "stub" }));
      buildApi.onLoad({ filter: /.*/, namespace: "stub" }, (args) => ({
        contents: args.path === "sql.js"
          ? "export default async function initSqlJs() { throw new Error('not used in streaming test'); }"
          : "export default 'sql-wasm-test-url';",
        loader: "js"
      }));
    }
  }]
});
const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString("base64")}`;
const {
  readBrowserThreadDailyTokenUsage,
  readBrowserThreadDetail,
  readBrowserThreadLogs,
  readBrowserThreadPrompts
} = await import(moduleUrl);

const records = [
  { type: "session_meta", timestamp: "2026-08-12T00:00:00Z", payload: { id: "thread-stream", cwd: "C:\\work" } },
  { type: "response_item", timestamp: "2026-08-12T01:00:00Z", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "streamed user prompt" }] } },
  { type: "event_msg", timestamp: "2026-08-12T02:00:00Z", payload: { type: "token_count", info: { total_token_usage: { total_tokens: 100 } } } },
  { type: "event_msg", timestamp: "2026-08-13T02:00:00Z", payload: { type: "token_count", info: { total_token_usage: { total_tokens: 160 } } } },
  { type: "event_msg", timestamp: "2026-08-13T03:00:00Z", payload: { type: "agent_message", message: "needle-log" } }
];
for (let index = 0; index < 60; index += 1) {
  records.push({ type: "event_msg", timestamp: "2026-08-13T04:00:00Z", payload: { type: "status", message: `${index}:${"x".repeat(50_000)}` } });
}
const sourceFile = new File([records.map((record) => JSON.stringify(record)).join("\n") + "\n"], "rollout.jsonl", { type: "application/jsonl" });
sourceFile.text = async () => { throw new Error("whole-file text() must not be used"); };
const originalSlice = sourceFile.slice.bind(sourceFile);
let largestSlice = 0;
sourceFile.slice = (start = 0, end = sourceFile.size, contentType) => {
  largestSlice = Math.max(largestSlice, end - start);
  assert.ok(end - start <= 512 * 1024, `slice exceeded streaming boundary: ${end - start}`);
  return originalSlice(start, end, contentType);
};

const threadFile = { relativePath: "sessions/rollout-thread-stream.jsonl", archivedStore: false, handle: { getFile: async () => sourceFile } };
const workspace = {
  mode: "browser_folder",
  snapshot: { threads: [{ id: "thread-stream", title: "Streaming", tokensUsed: 160 }] },
  threadFiles: new Map([["thread-stream", threadFile]])
};

const detail = await readBrowserThreadDetail(workspace, "thread-stream");
assert.equal(detail.rolloutStats.lineCount, records.length);
assert.equal(detail.rolloutStats.aggregationExact, true);

const daily = await readBrowserThreadDailyTokenUsage(workspace, "thread-stream");
assert.equal(daily.summary.totalTokens, 160);
assert.equal(daily.summary.unknownTokenThreads, 0);
assert.equal(daily.summary.aggregationExact, true);

const logs = await readBrowserThreadLogs(workspace, "thread-stream", 0, 20, "all", "needle-log");
assert.equal(logs.matchedEntries, 1);
assert.equal(logs.entries[0].message, "needle-log");
assert.equal(logs.aggregationExact, true);

const prompts = await readBrowserThreadPrompts(workspace, "thread-stream");
assert.equal(prompts.promptCount, 1);
assert.equal(prompts.prompts[0].text, "streamed user prompt");
assert.equal(prompts.aggregationExact, true);
assert.equal(largestSlice, 512 * 1024);

function promptWorkspace(threadId, file) {
  return {
    mode: "browser_folder",
    snapshot: { threads: [{ id: threadId, title: "Prompt protocols" }] },
    threadFiles: new Map([[threadId, {
      relativePath: `sessions/rollout-${threadId}.jsonl`,
      archivedStore: false,
      handle: { getFile: async () => file }
    }]])
  };
}

function jsonlFile(records, filename) {
  const file = new File([records.map((record) => JSON.stringify(record)).join("\n") + "\n"], filename, {
    type: "application/jsonl"
  });
  file.text = async () => { throw new Error("whole-file text() must not be used"); };
  const slice = file.slice.bind(file);
  file.slice = (start = 0, end = file.size, contentType) => {
    assert.ok(end - start <= 512 * 1024, `slice exceeded streaming boundary: ${end - start}`);
    return slice(start, end, contentType);
  };
  return file;
}

const protocolRecords = [
  {
    type: "user_message",
    timestamp: "2026-08-13T05:00:00.000Z",
    payload: { content: [{ type: "text", text: "mirrored user input" }] }
  },
  {
    type: "response_item",
    timestamp: "2026-08-13T05:00:01.000Z",
    payload: { type: "message", role: "user", content: [{ type: "input_text", text: "mirrored user input" }] }
  },
  {
    type: "event_msg",
    timestamp: "2026-08-13T05:00:02.000Z",
    payload: { type: "user_message", message: "mirrored user input" }
  },
  {
    type: "event_msg",
    timestamp: "2026-08-13T05:01:00.000Z",
    payload: { type: "user_message", message: "legacy event-only input" }
  },
  {
    type: "user_message",
    timestamp: "2026-08-13T05:01:30.000Z",
    payload: "top-level string payload"
  },
  {
    type: "response_item",
    timestamp: "2026-08-13T05:02:00.000Z",
    payload: { type: "message", role: "user", content: [{ type: "input_text", text: "intentionally repeated input" }] }
  },
  {
    type: "response_item",
    timestamp: "2026-08-13T05:03:00.000Z",
    payload: { type: "message", role: "user", content: [{ type: "input_text", text: "intentionally repeated input" }] }
  },
  {
    type: "user_message",
    timestamp: "2026-08-13T05:03:10.000Z",
    payload: { text: "same text sent again later" }
  },
  {
    type: "event_msg",
    timestamp: "2026-08-13T05:03:20.000Z",
    payload: { type: "user_message", message: "same text sent again later" }
  },
  {
    type: "user_message",
    timestamp: "2026-08-13T05:04:00.000Z",
    payload: { text: "<heartbeat>\n<automation_id>nightly</automation_id>\n<instructions>run check</instructions>\n</heartbeat>" }
  },
  {
    type: "response_item",
    timestamp: "2026-08-13T05:05:00.000Z",
    payload: { type: "message", role: "user", content: [{ type: "input_text", text: "<codex_delegation>\n<input>forwarded work</input>\n</codex_delegation>" }] }
  },
  {
    type: "event_msg",
    timestamp: "2026-08-13T05:06:00.000Z",
    payload: { type: "user_message", message: '<subagent_notification>\n{"agent_path":"child","status":{"completed":"done"},"kind":"subagent"}' }
  },
  {
    type: "user_message",
    timestamp: "2026-08-13T05:07:00.000Z",
    payload: { text: "<environment_context>\n<cwd>C:\\work</cwd>\n</environment_context>" }
  }
];
const protocolFile = jsonlFile(protocolRecords, "rollout-protocols.jsonl");
const protocolPrompts = await readBrowserThreadPrompts(
  promptWorkspace("thread-protocols", protocolFile),
  "thread-protocols"
);
assert.equal(protocolPrompts.promptCount, 11);
assert.equal(protocolPrompts.purePromptCount, 7);
assert.equal(protocolPrompts.visiblePromptCount, 7);
assert.deepEqual(protocolPrompts.sourceCounts, {
  user: 7,
  automation: 1,
  delegation: 1,
  subagent: 1,
  internal: 1
});
assert.deepEqual(
  protocolPrompts.prompts.map((prompt) => prompt.protocol),
  [
    "user_message",
    "event_msg",
    "user_message",
    "response_item",
    "response_item",
    "user_message",
    "event_msg",
    "user_message",
    "response_item",
    "event_msg",
    "user_message"
  ]
);
assert.deepEqual(
  protocolPrompts.prompts.slice(0, 7).map((prompt) => prompt.text),
  [
    "mirrored user input",
    "legacy event-only input",
    "top-level string payload",
    "intentionally repeated input",
    "intentionally repeated input",
    "same text sent again later",
    "same text sent again later"
  ]
);
assert.deepEqual(
  protocolPrompts.prompts.slice(7).map((prompt) => [prompt.sourceType, prompt.visibleByDefault, prompt.hasPureText]),
  [
    ["automation", false, false],
    ["delegation", false, false],
    ["subagent", false, false],
    ["internal", false, false]
  ]
);

const attachmentPrompt = [
  "# Files mentioned by the user:",
  "",
  "![attachment](data:image/png;base64,",
  "A".repeat(9 * 1024 * 1024),
  ")",
  "",
  "## My request for Codex:",
  "recover the user text after the attachment"
].join("\n");
const oversizedAttachmentFile = jsonlFile([{
  type: "response_item",
  timestamp: "2026-08-13T06:00:00.000Z",
  payload: { type: "message", role: "user", content: [{ type: "input_text", text: attachmentPrompt }] }
}], "rollout-oversized-attachment.jsonl");
const oversizedAttachmentPrompts = await readBrowserThreadPrompts(
  promptWorkspace("thread-oversized-attachment", oversizedAttachmentFile),
  "thread-oversized-attachment"
);
assert.equal(oversizedAttachmentPrompts.promptCount, 1);
assert.equal(oversizedAttachmentPrompts.aggregationExact, false);
assert.equal(oversizedAttachmentPrompts.oversizedRecords, 1);
assert.equal(oversizedAttachmentPrompts.prompts[0].protocol, "response_item");
assert.equal(oversizedAttachmentPrompts.prompts[0].textTruncated, false);
assert.equal(oversizedAttachmentPrompts.prompts[0].pureText, "recover the user text after the attachment");
assert.ok(!oversizedAttachmentPrompts.prompts[0].text.includes("A".repeat(1_024)));
assert.ok(!oversizedAttachmentPrompts.prompts[0].text.includes("base64"));

const oversizedTextFile = jsonlFile([{
  type: "user_message",
  timestamp: "2026-08-13T07:00:00.000Z",
  payload: { text: `bounded prompt start\n${"Z".repeat(9 * 1024 * 1024)}\nbounded prompt end` }
}], "rollout-oversized-text.jsonl");
const oversizedTextPrompts = await readBrowserThreadPrompts(
  promptWorkspace("thread-oversized-text", oversizedTextFile),
  "thread-oversized-text"
);
assert.equal(oversizedTextPrompts.promptCount, 1);
assert.equal(oversizedTextPrompts.prompts[0].textTruncated, true);
assert.ok(oversizedTextPrompts.prompts[0].text.startsWith("bounded prompt start"));
assert.ok(oversizedTextPrompts.prompts[0].text.endsWith("[Browser folder mode truncated this prompt text.]"));
assert.ok(oversizedTextPrompts.prompts[0].text.length <= 256 * 1024 + 64);
assert.ok(!oversizedTextPrompts.prompts[0].text.includes("bounded prompt end"));

console.log("browser home streaming tests passed");
