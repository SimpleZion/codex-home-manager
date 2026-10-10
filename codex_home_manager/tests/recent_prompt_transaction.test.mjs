import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8');
const start = source.indexOf('    loadMorePromptsRef.current = async () => {', source.indexOf('    let recentFillInProgress = false;'));
const end = source.indexOf('\n    void continueIndexScan(true);', start);
assert.ok(start > 0 && end > start);
async function exercise(pages, cancelAfter = Infinity) {
  let active = true;
  let requests = 0;
  const loadingStates = [];
  const errors = [];
  const context = {
    performance,
    recentFillInProgress: false, ownsQuery: () => active, search: '', t: value => value,
    promptLoadingMoreRef: { current: false }, promptScanTimerRef: { current: null },
    promptNextCursorRef: { current: 'a' }, loadMorePromptsRef: { current: null },
    window: { clearTimeout() {} }, setPromptsLoadingMore: value => loadingStates.push(value),
    setPromptsError: value => errors.push(value),
    requestPage: async cursor => {
      assert.equal(context.recentFillInProgress, true);
      requests += 1;
      const page = pages.shift();
      context.promptNextCursorRef.current = page.nextCursor;
      if (requests >= cancelAfter) active = false;
      return { added: page.added, page: { index: { strategy: 'recent' }, hasMore: page.hasMore } };
    }
  };
  runInNewContext(source.slice(start, end), context);
  const added = await context.loadMorePromptsRef.current();
  assert.equal(context.recentFillInProgress, false);
  return { added, requests, loadingStates, errors };
}
let result = await exercise([
  { added: 0, hasMore: true, nextCursor: 'b' },
  { added: 0, hasMore: true, nextCursor: 'c' },
  { added: 2, hasMore: true, nextCursor: 'd' }
]);
assert.equal(result.added, 2);
assert.equal(result.requests, 3);
assert.deepEqual(result.loadingStates, [false]);
result = await exercise([{ added: 0, hasMore: true, nextCursor: 'a' }]);
assert.equal(result.requests, 1);
assert.equal(result.errors.length, 1);
result = await exercise([{ added: 0, hasMore: true, nextCursor: 'b' }], 1);
assert.equal(result.requests, 1);
assert.deepEqual(result.loadingStates, []);
assert.ok(source.includes('prompts.length >= 12'));
assert.ok(source.includes('promptRecordIdentity(prompts[0]) === anchor.firstId'));
assert.ok(source.includes('anchor.viewportOffset -= element.scrollTop - previousTop'));
console.log('empty batches remain one cancellable transaction; stalled cursors terminate; initial fill and anchor guards present');
