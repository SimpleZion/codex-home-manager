import assert from "node:assert/strict";
import fs from "node:fs";
import ts from "typescript";

const source = fs.readFileSync(new URL("../src/main.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("main.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let effect;
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(tree) === "React.useEffect"
    && node.arguments[0]?.getText(tree).includes("const querySequence = promptPageSequenceRef.current + 1")) effect = node.arguments[0].getText(tree);
  ts.forEachChild(node, visit);
}
visit(tree);
assert.ok(effect, "test must execute the actual local prompt effect");
const ordering = fs.readFileSync(new URL("../src/PromptOrdering.tsx", import.meta.url), "utf8");
const module = { exports: {} };
new Function("require", "module", "exports", ts.transpileModule(ordering, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React }
}).outputText)(() => ({}), module, module.exports);
const { mergeOrderedPromptRecords, promptRecordIdentity } = module.exports;
const row = (id, order, offset, index = offset) => ({ recordId: id, segmentOrder: order, byteOffset: offset, index, text: id });
const old = row("1:1", 0, 100, 1), current = row("2:1", 1, 100, 2);
assert.deepEqual(mergeOrderedPromptRecords([old], [current], false, "desc", value => value).map(promptRecordIdentity), ["2:1", "1:1"]);
assert.deepEqual(mergeOrderedPromptRecords([current], [row("1:2", 0, 500)], false, "asc", value => value).map(promptRecordIdentity), ["1:2", "2:1"]);

function fixture(responses) {
  const timers = new Map(), requests = [], cancellations = [];
  let timerId = 0, activeIndex = 0;
  const state = { error: "", page: null };
  const ref = current => ({ current });
  const bindings = {
    thread: { id: "continued" }, browserWorkspace: null, contentMode: "prompts", promptSearchQuery: "needle", activePromptScope: "pure",
    promptPageSequenceRef: ref(0), promptLoadedAdditionalPagesRef: ref(false), promptNextCursorRef: ref(null), loadedPromptsRef: ref([]),
    promptPageRequestRef: ref(null), promptLoadingMoreRef: ref(false), promptScanTimerRef: ref(null), loadMorePromptsRef: ref(null),
    setLocalPromptPage: value => { state.page = value; }, setLocalNextCursor: () => {}, setPromptsError: value => { state.error = value; },
    setPromptsLoading: () => {}, setPromptsLoadingMore: () => {}, setActivePromptSearchIndex: value => { activeIndex = typeof value === "function" ? value(activeIndex) : value; },
    mergeOrderedPromptRecords, promptRecordIdentity, normalizePromptRecord: value => value, t: value => value, codexHome: "test-home", fetchReadJson: null,
    createPromptRequestId: () => `request-${requests.length + 1}`, isRecoverablePromptCursorError: error => error.message.includes("prompt cursor"),
    cancelActivePageRequest: () => {
      const pending = bindings.promptPageRequestRef.current;
      if (pending) { cancellations.push(pending.requestId); pending.controller.abort(); bindings.promptPageRequestRef.current = null; }
    },
    fetchThreadPromptPageFromLocalApi: async (_read, thread, _home, options) => {
      requests.push(options);
      const response = responses.shift();
      assert.ok(response, "unexpected page request");
      const data = await (typeof response === "function" ? response(options) : response);
      if (data instanceof Error) throw data;
      return { threadId: thread.id, requestId: options.requestId, scope: options.scope, search: options.search, order: options.order,
        prompts: [], matchCount: 0, matchCountComplete: false, hasMore: true, nextCursor: "empty", index: { scannedBytes: 1, cursorGeneration: "source-a" }, ...data };
    },
    window: { setTimeout: callback => { timers.set(++timerId, callback); return timerId; }, clearTimeout: id => timers.delete(id) }
  };
  const compiled = ts.transpileModule(`const run = ${effect};`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const cleanup = new Function(...Object.keys(bindings), `${compiled};return run();`)(...Object.values(bindings));
  return { bindings, requests, state, timers, cancellations, cleanup, active: () => activeIndex, tick: async () => {
    const [id, callback] = timers.entries().next().value || [];
    assert.ok(callback, "scan must remain scheduled"); timers.delete(id); await callback(); await settle();
  } };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

const empty = fixture([
  {}, {},
  { prompts: [current], matchCount: 2, nextCursor: "current" },
  { prompts: [old], matchCount: 2, matchCountComplete: true, hasMore: false, nextCursor: null }
]);
await settle();
assert.equal(empty.requests[0].scanBudgetMs, 0, "first screen must read known hits before starting background scanning");
await empty.bindings.loadMorePromptsRef.current();
assert.equal(empty.state.error, "", "an empty incomplete page waits for indexing rather than report a false stuck cursor");
await empty.tick();
assert.equal(empty.bindings.promptNextCursorRef.current, "current");
await empty.bindings.loadMorePromptsRef.current();
assert.deepEqual(empty.state.page.prompts.map(promptRecordIdentity), ["2:1", "1:1"]);
empty.cleanup();

let release;
const pending = new Promise(resolve => { release = resolve; });
const concurrency = fixture([
  { prompts: [current], matchCount: 3, nextCursor: "one" },
  () => pending,
  { prompts: [current], matchCount: 4, nextCursor: "restart", index: { scannedBytes: 2, cursorGeneration: "source-a" } },
  { prompts: [old], matchCount: 4, nextCursor: "duplicate" },
  { prompts: [row("1:2", 0, 200)], matchCount: 4, nextCursor: null, hasMore: false, matchCountComplete: true }
]);
await settle();
// Run the timer while foreground paging owns the request.
const timer = [...concurrency.timers.values()][0];
const more = concurrency.bindings.loadMorePromptsRef.current();
await settle();
await timer();
assert.equal(concurrency.requests.length, 2);
assert.equal(concurrency.cancellations.length, 0, "scan must not cancel a foreground page");
release({ prompts: [old], matchCount: 3, nextCursor: "two" });
await more;
await concurrency.tick();
assert.equal(concurrency.bindings.promptNextCursorRef.current, "restart", "newly discovered matches must reopen the previously crossed range");
assert.equal(concurrency.state.page.prompts.length, 2, "scan must retain already loaded rows");
await concurrency.bindings.loadMorePromptsRef.current();
assert.equal(concurrency.requests.at(-1).cursor, "duplicate", "paging must advance through already loaded matches to the new result");
assert.equal(concurrency.state.page.prompts.length, 3);
concurrency.cleanup();

const rebuilt = fixture([
  { index: { scannedBytes: 1000, cursorGeneration: "source-a" } },
  { index: { reset: true, scannedBytes: 1, cursorGeneration: "source-b" } },
  { index: { scannedBytes: 2, cursorGeneration: "source-b" } },
  { index: { scannedBytes: 3, cursorGeneration: "source-b" } }
]);
await settle();
await rebuilt.tick(); await rebuilt.tick(); await rebuilt.tick();
assert.equal(rebuilt.timers.size, 1, "progress after rebuild must not be compared with the old high-water mark");
rebuilt.cleanup();

const replacement = fixture([
  { prompts: [current, old], matchCount: 2, nextCursor: "old" },
  { prompts: [row("2:1", 1, 100)], matchCount: 1, nextCursor: null, hasMore: false, matchCountComplete: true,
    index: { scannedBytes: 5, cursorGeneration: "source-b", reset: false } }
]);
await settle(); await replacement.tick();
assert.equal(replacement.state.page.prompts.length, 1, "another request rebuilding the source must invalidate old rows even when reset is false here");
assert.equal(replacement.bindings.promptNextCursorRef.current, null);
replacement.cleanup();

let navigation;
function findNavigation(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === "selectPromptSearchResult") navigation = node.getText(tree);
  ts.forEachChild(node, findNavigation);
}
findNavigation(tree);
assert.ok(navigation);
const previousHits = [current, row("1:3", 0, 300), old];
const discovered = row("1:2", 0, 200);
let selected;
const navigationBindings = {
  promptNavigationSequenceRef: { current: 0 }, promptOrder: "desc", searchedPrompts: previousHits,
  activePromptSearchIndex: 2, promptsHaveMore: true, isBrowserPromptMode: false, promptRecordIdentity,
  setPromptNavigationStatus: () => {}, t: value => value, promptNextCursorRef: { current: null },
  loadedPromptsRef: { current: [current, previousHits[1], discovered, old] },
  loadMorePromptsRef: { current: async () => 1 }, setActivePromptSearchIndex: value => { selected = value; }
};
const navigationCode = ts.transpileModule(navigation, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
await new Function(...Object.keys(navigationBindings), `${navigationCode};return selectPromptSearchResult(1);`)(...Object.values(navigationBindings));
assert.equal(selected, 2, "next-match must select the discovered identity, not the previous list length");

const recovery = fixture([
  { prompts: [current], matchCount: 2, nextCursor: "one" },
  new Error("prompt cursor is stale because the rollout index was rebuilt"),
  { prompts: [current], matchCount: 2, nextCursor: "new" },
  { prompts: [old], matchCount: 2, nextCursor: "remaining" },
  new Error("prompt cursor is stale because the rollout index was rebuilt")
]);
await settle();
await recovery.bindings.loadMorePromptsRef.current();
assert.equal(recovery.requests.filter(request => !request.cursor).length, 2, "one recovery may re-read the first page exactly once");
assert.equal(recovery.state.error, "");
await recovery.bindings.loadMorePromptsRef.current();
assert.equal(recovery.requests.length, 5, "a second invalidation in the same query must stop rather than restart forever");
assert.match(recovery.state.error, /prompt cursor/);
recovery.cleanup();
console.log("prompt scan/cursor runtime PASS: segment identity, empty page, foreground priority, late matches, rebuild progress, bounded recovery, source replacement, late-hit navigation");

assert.ok(source.includes('prompt-search-context") === "true"'), "context is opt-in for an unset preference, without overwriting an explicit choice");
const countExpression = source.slice(source.indexOf('<span className="prompt-search-count"', source.indexOf('function selectPromptSearchResult')), source.indexOf('<PromptOrderControls', source.indexOf('function selectPromptSearchResult')));
assert.match(countExpression, /t\("匹配已加载"\).*formatCount\(searchedPrompts.length\)/, "loaded matches must not be mislabeled as the active global position");
assert.match(countExpression, /t\("当前定位"\)/, "the selected match has a separate position indicator");
assert.ok(source.includes('contextWindow={Boolean(promptSearchQuery && showPromptContext)}'));
assert.ok(source.includes('t(contextWindow ? "条上下文已加载" : "条已加载")'));
assert.ok(source.includes('t("筛选外")'), "filtered records must not be described as lost or hidden history");
