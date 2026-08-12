import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "esbuild";

const projectRoot = path.resolve(import.meta.dirname, "..");
const bundle = await build({
  entryPoints: [path.join(projectRoot, "src", "threadTimeline.ts")],
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false
});
const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString("base64")}`;
const { isVisibleCommentaryItem, isVisibleCommentaryRecord, timelineItemFromJson, readBrowserTimelineItem, readBrowserTimelinePage } = await import(moduleUrl);

const commentary = timelineItemFromJson({
  type: "response_item",
  timestamp: "2026-08-13T01:00:00Z",
  payload: {
    type: "message",
    role: "assistant",
    phase: "commentary",
    content: [{ type: "output_text", text: "正在核对真实页面" }]
  }
}, 1);
assert.equal(commentary.kind, "commentary");
assert.equal(commentary.label, "思考过程");
assert.equal(isVisibleCommentaryRecord({
  type: "event_msg",
  payload: { type: "agent_message", phase: "commentary", message: "正在检查阶段结果" }
}), true);

const nonCommentaryRecords = [
  { type: "event_msg", payload: { type: "exec_command_begin", phase: "commentary", command: "npm test" } },
  { type: "event_msg", payload: { type: "patch_apply_begin", phase: "commentary", patch: "*** Begin Patch" } },
  { type: "event_msg", payload: { type: "agent_reasoning", phase: "commentary", text: "内部推理" } },
  { type: "response_item", payload: { type: "custom_tool_call", phase: "commentary", call_id: "tool-1", name: "shell", input: "dir" } },
  { type: "response_item", payload: { type: "custom_tool_call_output", phase: "commentary", call_id: "tool-1", output: "command output" } },
  { type: "response_item", payload: { type: "reasoning", phase: "commentary", encrypted_content: "opaque" } },
  { type: "response_item", payload: { type: "message", role: "developer", phase: "commentary", content: [{ type: "input_text", text: "developer context" }] } }
];
for (const [index, record] of nonCommentaryRecords.entries()) {
  assert.equal(isVisibleCommentaryRecord(record), false, `record ${index} must not enter visible commentary`);
  assert.notEqual(timelineItemFromJson(record, 100 + index)?.kind, "commentary");
}
assert.equal(isVisibleCommentaryItem({ ...commentary, payloadType: "custom_tool_call_output" }), false);

const filterRecords = [
  { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "用户问题" }] } },
  { type: "response_item", payload: { type: "message", role: "assistant", phase: "commentary", content: [{ type: "output_text", text: "正在核对数据来源" }] } },
  { type: "event_msg", payload: { type: "agent_message", phase: "commentary", message: "数据来源已确认，继续验证" } },
  { type: "event_msg", payload: { type: "exec_command_begin", phase: "commentary", command: "python verify.py" } },
  { type: "event_msg", payload: { type: "patch_apply_begin", phase: "commentary", patch: "*** Begin Patch" } },
  { type: "response_item", payload: { type: "reasoning", summary: [{ type: "summary_text", text: "不可进入主要内容的推理" }], encrypted_content: "opaque" } },
  { type: "response_item", payload: { type: "custom_tool_call_output", phase: "commentary", call_id: "tool-2", output: "不可进入思考过程的工具输出" } },
  { type: "response_item", payload: { type: "message", role: "assistant", phase: "final", content: [{ type: "output_text", text: "最终结论" }] } }
];
const filterFile = new File([`${filterRecords.map((record) => JSON.stringify(record)).join("\n")}\n`], "filters.jsonl", { type: "application/jsonl" });
const mainContentPage = await readBrowserTimelinePage(filterFile, "thread-filter", "Filter", "filters.jsonl", { kind: "conversation" });
assert.deepEqual(mainContentPage.items.map((item) => [item.kind, item.text]), [
  ["user", "用户问题"],
  ["commentary", "正在核对数据来源"],
  ["commentary", "数据来源已确认，继续验证"],
  ["assistant", "最终结论"]
]);
const commentaryPage = await readBrowserTimelinePage(filterFile, "thread-filter", "Filter", "filters.jsonl", { kind: "commentary" });
assert.deepEqual(commentaryPage.items.map((item) => item.text), ["正在核对数据来源", "数据来源已确认，继续验证"]);
assert.doesNotMatch(commentaryPage.items.map((item) => item.text).join("\n"), /python verify|Begin Patch|工具输出|推理/);

const eventReasoning = timelineItemFromJson({
  type: "event_msg",
  payload: { type: "agent_reasoning", text: "持久化推理内容" }
}, 11);
assert.equal(eventReasoning.kind, "reasoning");
assert.equal(eventReasoning.label, "推理记录");

const toolSearch = timelineItemFromJson({
  type: "response_item",
  payload: { type: "tool_search_call", call_id: "search-1", query: "thread tools" }
}, 2);
assert.equal(toolSearch.kind, "tool_call");
assert.match(toolSearch.text, /thread tools/);

const redacted = timelineItemFromJson({
  type: "response_item",
  payload: {
    type: "custom_tool_call_output",
    call_id: "image-1",
    output: "preview=data:image/png;base64,PRIVATE_BYTES_123456"
  }
}, 3);
assert.doesNotMatch(redacted.text, /PRIVATE_BYTES/);
assert.match(redacted.text, /附件内容已隐藏/);

const shortDataUrl = timelineItemFromJson({
  type: "response_item",
  payload: { type: "custom_tool_call_output", call_id: "short-image", output: "preview=data:image/png;base64,YQ==" }
}, 31);
assert.doesNotMatch(shortDataUrl.text, /YQ==/);
assert.match(shortDataUrl.text, /附件内容已隐藏/);

const mixedReasoning = timelineItemFromJson({
  type: "response_item",
  payload: {
    type: "reasoning",
    summary: [{ type: "summary_text", text: "可读摘要" }],
    encrypted_content: "opaque"
  }
}, 4);
assert.equal(mixedReasoning.text, "可读摘要");
assert.equal(mixedReasoning.label, "推理记录");
assert.equal(mixedReasoning.encrypted, false);
assert.equal(mixedReasoning.hasEncryptedContent, true);

const encryptedReasoning = timelineItemFromJson({
  type: "response_item",
  payload: { type: "reasoning", encrypted_content: "opaque" }
}, 5);
assert.equal(encryptedReasoning.label, "推理记录");
assert.equal(encryptedReasoning.text, "");
assert.equal(encryptedReasoning.encrypted, true);
assert.equal(encryptedReasoning.readable, false);
assert.doesNotMatch(
  [eventReasoning.label, mixedReasoning.label, encryptedReasoning.label].join("\n"),
  /推理摘要|加密推理记录/
);

const oversizedLine = JSON.stringify({
  type: "response_item",
  payload: {
    type: "message",
    role: "user",
    content: [
      { type: "input_text", text: "超大记录中的用户正文必须恢复" },
      { type: "input_image", image_url: `data:image/png;base64,${"x".repeat(9 * 1024 * 1024)}` }
    ]
  }
});
const oversizedAssistantLine = JSON.stringify({
  type: "response_item",
  payload: {
    type: "message",
    role: "assistant",
    phase: "final",
    content: [
      { type: "output_text", text: "超大记录中的助手正文必须恢复" },
      { type: "output_image", image_url: `data:image/png;base64,${"y".repeat(9 * 1024 * 1024)}` }
    ]
  }
});
const oversizedCommentaryLine = JSON.stringify({
  type: "response_item",
  payload: {
    type: "message",
    role: "assistant",
    phase: "commentary",
    content: [
      { type: "output_text", text: "超大记录中的 commentary 正文必须恢复" },
      { type: "output_image", image_url: `data:image/png;base64,${"z".repeat(9 * 1024 * 1024)}` }
    ]
  }
});
const visibleLine = JSON.stringify({
  type: "event_msg",
  payload: { type: "agent_message", phase: "commentary", message: "安全读取" }
});
const sourceFile = new File([`${oversizedLine}\n${oversizedAssistantLine}\n${oversizedCommentaryLine}\n${visibleLine}\n`], "rollout.jsonl", { type: "application/jsonl" });
const originalSlice = sourceFile.slice.bind(sourceFile);
sourceFile.slice = (start, end, contentType) => {
  assert.ok((end ?? sourceFile.size) - (start ?? 0) <= 8 * 1024 * 1024, "reader must not allocate an oversized line");
  return originalSlice(start, end, contentType);
};
const page = await readBrowserTimelinePage(sourceFile, "thread-1", "Thread", "rollout.jsonl", { kind: "all" });
assert.deepEqual(page.items.map((item) => item.text), [
  "超大记录中的用户正文必须恢复\n[图片附件]",
  "超大记录中的助手正文必须恢复\n[图片附件]",
  "超大记录中的 commentary 正文必须恢复\n[图片附件]",
  "安全读取"
]);
assert.doesNotMatch(page.items.map((item) => item.text).join("\n"), /data:image|[xyz]{100}/);
assert.equal(page.skippedOversizedRecords, 0);
assert.equal(page.recoveredOversizedRecords, 3);
assert.equal(page.hasMore, false);
assert.equal(page.nextBeforeByte, null);
const recoveredItem = await readBrowserTimelineItem(sourceFile, 0);
assert.match(recoveredItem.text, /超大记录中的用户正文必须恢复/);
assert.doesNotMatch(recoveredItem.text, /data:image|x{100}/);

const virtualSize = 1_400_000_000;
let virtualBytesRead = 0;
const virtualFile = {
  size: virtualSize,
  slice(start = 0, end = virtualSize) {
    const length = end - start;
    virtualBytesRead += length;
    return new Blob([new Uint8Array(length).fill(120)]);
  }
};
const virtualPage = await readBrowserTimelinePage(virtualFile, "thread-virtual", "Virtual", "virtual.jsonl", { kind: "all" });
assert.equal(virtualPage.items.length, 0);
assert.equal(virtualPage.skippedOversizedRecords, 1);
assert.equal(virtualPage.hasMore, true);
assert.equal(virtualPage.nextBeforeByte, virtualSize - (64 * 1024 * 1024));
assert.ok(virtualBytesRead <= (64 * 1024 * 1024) + 1, `virtual reader read ${virtualBytesRead} bytes`);

console.log("thread timeline browser tests passed");
