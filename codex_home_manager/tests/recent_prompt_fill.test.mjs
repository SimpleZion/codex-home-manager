import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../src/main.tsx", import.meta.url), "utf8");
const start = source.indexOf("    function scheduleRecentFill(");
const end = source.indexOf("\n    loadMorePromptsRef.current", start);
assert.ok(start >= 0 && end > start);
let callback;
let active = true;
let requests = 0;
const loadedPromptsRef = { current: [] };
const context = {
  ownsQuery: () => active,
  promptScanTimerRef: { current: null },
  promptNextCursorRef: { current: "next" },
  loadedPromptsRef,
  window: { setTimeout: (handler) => { callback = handler; return 1; } },
  loadMorePromptsRef: { current: async () => { requests += 1; return 1; } }
};
runInNewContext(`${source.slice(start, end)}; scheduleRecentFill();`, context);
assert.equal(typeof callback, "function");
await callback();
assert.equal(requests, 1);
active = false;
await callback();
assert.equal(requests, 1);
active = true;
loadedPromptsRef.current = [{}];
await callback();
assert.equal(requests, 1);
runInNewContext(`${source.slice(start, end)}; scheduleRecentFill(1);`, context);
await callback();
assert.equal(requests, 2);
loadedPromptsRef.current.push({});
await callback();
assert.equal(requests, 2);
console.log("recent empty-page continuation and generation/visible-row guards passed");
