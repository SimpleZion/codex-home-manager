import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium, request } from 'playwright';

const baseUrl = process.env.TEST_BASE_URL || 'http://127.0.0.1:8765';
const threadId = '019e6365-4d4c-7af1-a907-a79d3a28afdc';
const output = path.resolve(import.meta.dirname, '../output/prompt-context-verification');
const api = await request.newContext({ baseURL: baseUrl, timeout: 15000 });
const results = [];
let browser;
let page;
await fs.mkdir(output, { recursive: true });
try {
  for (const home of [null, '', 'D:\\.codex']) {
    const params = new URLSearchParams({ scope: 'pure', search: '\u5b50agent', order: 'desc', strategy: 'index', limit: '60', scanBudgetMs: '1000' });
    if (home !== null) params.set('codex_home', home);
    let data;
    for (let scan = 0; scan < 120; scan++) {
      const response = await api.get(`/api/threads/${threadId}/prompts/page?${params}`);
      assert.ok(response.ok(), await response.text());
      data = await response.json();
      assert.equal(data.index.strategy, 'persistent', 'default Home must use the same persistent index as context');
      if (data.matchCountComplete) break;
    }
    assert.ok(data.matchCountComplete, 'index must finish within bounded scan requests');
    assert.ok(data.prompts.length > 2);
    assert.equal(data.prompts[0].byteOffset, 1622299382, 'the screenshot latest matching message must remain first');
    for (const hit of data.prompts.slice(0, 3)) {
      const contextParams = new URLSearchParams(params);
      contextParams.set('anchorIndex', String(hit.index));
      contextParams.set('anchorByteOffset', String(hit.byteOffset));
      contextParams.set('generation', data.index.generation);
      contextParams.set('before', '10');
      contextParams.set('after', '10');
      const response = await api.get(`/api/threads/${threadId}/prompts/context?${contextParams}`);
      assert.ok(response.ok(), await response.text());
      const context = await response.json();
      const anchor = context.prompts[context.anchorOffset];
      assert.equal(anchor.byteOffset, hit.byteOffset, 'anchor must identify the same physical record');
      assert.equal(anchor.text, hit.text);
      assert.ok(context.prompts.length > 1, 'nonmatching neighbors must be available');
      assert.equal(context.index.generation, data.index.generation);
      results.push({ home, anchorIndex: hit.index, byteOffset: hit.byteOffset, neighbors: context.prompts.length - 1, matchCount: data.matchCount });
    }
  }
  if (process.env.TEST_API_ONLY !== '1') {
    // Browser skill is not available in this task; use the project's Playwright workflow.
    browser = await chromium.launch({ headless: true });
    page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    if (process.env.TEST_FRONTEND_DIST === '1') {
      await page.route(`${baseUrl}/**`, async route => {
        const pathname = new URL(route.request().url()).pathname;
        if (pathname !== '/' && !pathname.startsWith('/assets/')) return route.continue();
        const file = path.resolve(import.meta.dirname, '../dist', pathname === '/' ? 'index.html' : pathname.slice(1));
        await route.fulfill({ body: await fs.readFile(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.wasm') ? 'application/wasm' : 'text/html' });
      });
    }
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => {
      if (response.url().includes('/prompts/context') && !response.ok() && response.headers()['x-test-stale'] !== '1') errors.push(`context HTTP ${response.status()}`);
    });
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
    const filteredPage = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === '/api/threads/page' && url.searchParams.get('search') === threadId && response.ok();
    });
    await page.getByRole('textbox', { name: '\u641c\u7d22\u6807\u9898\u3001\u7ebf\u7a0b ID\u3001\u9879\u76ee\u3001JSONL \u8def\u5f84' }).fill(threadId);
    await filteredPage;
    await page.waitForFunction(() => document.querySelectorAll('.thread-table tbody tr').length === 1);
    await page.locator('.thread-table tbody tr').filter({ hasText: 'Quant Research Platform' }).first().press('Enter');
    await page.locator('.thread-detail-modal').getByRole('button', { name: '\u67e5\u770b\u7ebf\u7a0b\u5185\u5bb9', exact: true }).last().click();
    const modal = page.locator('.prompt-modal');
    await modal.getByRole('tab', { name: '\u6211\u7684\u8f93\u5165', exact: true }).click();
    const search = modal.getByRole('searchbox', { name: '\u641c\u7d22\u5f53\u524d\u7b5b\u9009\u7684\u5168\u90e8\u5185\u5bb9' });
    const contextResponses = [];
    page.on('response', response => {
      if (response.url().includes('/prompts/context') && response.ok()) contextResponses.push(response);
    });
    const waitContext = async action => {
      const pending = page.waitForResponse(response => response.url().includes('/prompts/context') && response.ok());
      await action();
      const data = await (await pending).json();
      await modal.locator('.prompt-entry-active-match').waitFor();
      assert.equal(await modal.locator('.inline-error').count(), 0);
      return data;
    };
    const first = await waitContext(() => search.fill('\u5b50agent'));
    assert.equal(first.prompts[first.anchorOffset].byteOffset, results[0].byteOffset);
    assert.ok(first.prompts.some(prompt => !prompt.text.toLowerCase().includes('\u5b50agent')));
    const ordering = modal.getByRole('group', { name: '\u5185\u5bb9\u987a\u5e8f' });
    assert.equal(await ordering.getByRole('button', { name: '\u65f6\u95f4\u987a\u5e8f', exact: true }).getAttribute('aria-pressed'), 'true', 'search must default to chronological display');
    const older = await waitContext(() => modal.getByRole('button', { name: '\u4e0a\u4e00\u4e2a\u5339\u914d', exact: true }).click());
    assert.ok(older.anchorIndex < first.anchorIndex, 'up must navigate toward older matches in chronological order');
    await waitContext(() => modal.getByRole('button', { name: '\u8df3\u5230\u6700\u65b0', exact: true }).click());
    assert.equal(await ordering.getByRole('button', { name: '\u65f6\u95f4\u987a\u5e8f', exact: true }).getAttribute('aria-pressed'), 'true', 'jump latest must not change display order');
    await ordering.getByRole('button', { name: '\u6700\u65b0\u5728\u524d', exact: true }).click();
    const second = await waitContext(() => modal.getByRole('button', { name: '\u4e0b\u4e00\u4e2a\u5339\u914d', exact: true }).click());
    assert.notEqual(second.anchorIndex, first.anchorIndex);
    const previous = await waitContext(() => modal.getByRole('button', { name: '\u4e0a\u4e00\u4e2a\u5339\u914d', exact: true }).click());
    assert.equal(previous.anchorIndex, first.anchorIndex);
    const view = modal.getByRole('group', { name: '\u641c\u7d22\u7ed3\u679c\u89c6\u56fe' });
    const contextCount = contextResponses.length;
    await view.getByRole('button', { name: '\u4ec5\u5339\u914d', exact: true }).click();
    assert.equal(await view.getByRole('button', { name: '\u4ec5\u5339\u914d', exact: true }).getAttribute('aria-pressed'), 'true');
    await modal.getByRole('button', { name: '\u4e0b\u4e00\u4e2a\u5339\u914d', exact: true }).click();
    await modal.locator(`[data-prompt-index="${second.anchorIndex}"] .prompt-entry-active-match`).waitFor();
    assert.ok(await modal.locator('.prompt-entry-wrap').evaluateAll(entries => entries.every(entry => entry.querySelector('mark'))), 'matches-only must exclude all nonmatching neighbors');
    await page.screenshot({ path: path.join(output, 'installed-matches-only.png') });
    await modal.getByRole('button', { name: '\u4e0a\u4e00\u4e2a\u5339\u914d', exact: true }).click();
    await modal.locator(`[data-prompt-index="${first.anchorIndex}"] .prompt-entry-active-match`).waitFor();
    assert.equal(contextResponses.length, contextCount, 'matches-only navigation must not request context');
    assert.equal(await page.evaluate(() => localStorage.getItem('codex-home-manager.prompt-search-context')), 'false');
    let pageRequests = 0;
    page.on('request', request => { if (new URL(request.url()).pathname.endsWith('/prompts/page')) pageRequests++; });
    await ordering.getByRole('button', { name: '\u65f6\u95f4\u987a\u5e8f', exact: true }).click();
    await page.waitForTimeout(300);
    assert.equal(pageRequests, 0, 'display sorting must not refetch or reset search cursors');
    assert.equal(await modal.locator('.prompt-entry-active-match').locator('..').getAttribute('data-prompt-index'), String(first.anchorIndex));
    const visibleIndices = await modal.locator('.prompt-entry-wrap').evaluateAll(entries => entries.map(entry => Number(entry.dataset.promptIndex)));
    assert.deepEqual(visibleIndices, [...visibleIndices].sort((a, b) => a - b), 'chronological matches must ascend');
    await page.waitForTimeout(250);
    const nextPage = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname.endsWith('/prompts/page') && Boolean(url.searchParams.get('cursor')) && response.ok();
    });
    await modal.locator('.prompt-list').evaluate(element => {
      element.scrollTop = 0;
      element.dispatchEvent(new Event('scroll', { bubbles: true }));
    });
    const more = await (await nextPage).json();
    assert.equal(more.hasMore, false, 'matches-only must load the final search page');
    assert.equal(contextResponses.length, contextCount, 'matches-only paging must not fetch neighboring messages');
    const restored = await waitContext(() => view.getByRole('button', { name: '\u663e\u793a\u4e0a\u4e0b\u6587', exact: true }).click());
    assert.equal(restored.anchorIndex, first.anchorIndex, 'view switching must retain the active match');
    await page.screenshot({ path: path.join(output, 'installed-search-context.png') });
    const list = modal.locator('.prompt-list');
    const paging = page.waitForResponse(response => response.url().includes('/prompts/context') && new URL(response.url()).searchParams.get('before') === '40');
    await list.evaluate(element => {
      element.scrollTop = Math.min(700, element.scrollHeight - element.clientHeight);
      element.dispatchEvent(new Event('scroll', { bubbles: true }));
    });
    await page.waitForTimeout(200);
    await list.evaluate(element => { element.scrollTop = 0; element.dispatchEvent(new Event('scroll', { bubbles: true })); });
    assert.ok((await paging).ok(), 'upward scroll must load earlier nonmatching context');
    let injected = 0;
    await page.route('**/prompts/context?*', async route => {
      if (injected++ === 0) {
        await route.fulfill({ status: 400, headers: { 'x-test-stale': '1' }, json: { code: 'prompt_context_stale', detail: 'prompt context anchor is stale: index generation changed' } });
      } else await route.continue();
    });
    const recovered = await waitContext(() => modal.getByRole('button', { name: '\u4e0b\u4e00\u4e2a\u5339\u914d', exact: true }).click());
    assert.ok(injected >= 2 && injected <= 3, 'stale context must refresh once, without a retry loop');
    assert.ok(recovered.prompts.length > 1);
    await page.unroute('**/prompts/context?*');
    let rejected = 0;
    await page.route('**/prompts/context?*', async route => {
      rejected++;
      await route.fulfill({ status: 400, headers: { 'x-test-stale': '1' }, json: { code: 'prompt_context_stale', detail: 'prompt context anchor is stale: index generation changed' } });
    });
    await modal.getByRole('group', { name: 'Prompt \u6765\u6e90\u7b5b\u9009' }).getByRole('button', { name: '\u4e0a\u4e0b\u6587+\u8f93\u5165', exact: false }).click();
    await modal.locator('.inline-error').filter({ hasText: 'prompt context anchor is stale' }).waitFor();
    assert.equal(rejected, 2, 'persistent failure must retry once and then stop');
    await page.waitForTimeout(1200);
    assert.equal(rejected, 2, 'no background retry loop is permitted');
    await page.unroute('**/prompts/context?*');
    await waitContext(() => modal.getByRole('group', { name: 'Prompt \u6765\u6e90\u7b5b\u9009' }).getByRole('button', { name: '\u7eaf\u6587\u672c\u8f93\u5165', exact: false }).click());
    for (const width of [900, 480]) {
      await page.setViewportSize({ width, height: 1080 });
      await page.screenshot({ path: path.join(output, `installed-search-context-${width}.png`) });
      assert.equal(await modal.locator('.inline-error').count(), 0);
    }
    for (const width of [1920, 1280, 900, 480, 390]) {
      await page.setViewportSize({ width, height: 1080 });
      const details = modal.locator('.prompt-index-management');
      if (!(await details.evaluate(element => element.open))) await details.locator('summary').click();
      const panel = await modal.locator('.prompt-index-body').boundingBox();
      const bounds = await modal.boundingBox();
      assert.ok(panel && bounds && panel.x >= bounds.x && panel.x + panel.width <= bounds.x + bounds.width, `index panel must stay inside modal at ${width}px`);
      await page.screenshot({ path: path.join(output, `installed-index-panel-${width}.png`) });
      await details.locator('summary').click();
      const controls = await modal.locator('.prompt-content-search button').evaluateAll(buttons => buttons.map(button => { const r = button.getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right, bottom: r.bottom }; }));
      for (let i = 0; i < controls.length; i++) {
        assert.ok(controls[i].x >= bounds.x && controls[i].right <= bounds.x + bounds.width, `search control must remain visible at ${width}px`);
        for (const other of controls.slice(i + 1)) assert.ok(controls[i].right <= other.x || other.right <= controls[i].x || controls[i].bottom <= other.y || other.bottom <= controls[i].y, `search controls must not overlap at ${width}px`);
      }
    }
    // Recheck the user's short-result screenshot, not only the multi-page quant task.
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
    const shortThreadId = '019e57fc-86e4-7232-ba97-3b01b3a18f1c';
    const shortRows = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === '/api/threads/page' && url.searchParams.get('search') === shortThreadId && response.ok();
    });
    await page.getByRole('textbox', { name: '\u641c\u7d22\u6807\u9898\u3001\u7ebf\u7a0b ID\u3001\u9879\u76ee\u3001JSONL \u8def\u5f84' }).fill(shortThreadId);
    await shortRows;
    await page.waitForFunction(() => document.querySelectorAll('.thread-table tbody tr').length === 1);
    await page.locator('.thread-table tbody tr').first().press('Enter');
    await page.locator('.thread-detail-modal').getByRole('button', { name: '\u67e5\u770b\u7ebf\u7a0b\u5185\u5bb9', exact: true }).last().click();
    await modal.getByRole('tab', { name: '\u6211\u7684\u8f93\u5165', exact: true }).click();
    const shortPage = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === `/api/threads/${shortThreadId}/prompts/page` && url.searchParams.get('search') === '\u5b50agent' && response.ok();
    });
    await modal.getByRole('searchbox').fill('\u5b50agent');
    const shortData = await (await shortPage).json();
    await view.getByRole('button', { name: '\u4ec5\u5339\u914d', exact: true }).click();
    const shortLatest = shortData.prompts[0].index;
    await modal.locator(`[data-prompt-index="${shortLatest}"] .prompt-entry-active-match`).waitFor();
    await page.waitForTimeout(300);
    const shortIndices = await modal.locator('.prompt-entry-wrap').evaluateAll(entries => entries.map(entry => Number(entry.dataset.promptIndex)));
    assert.deepEqual(shortIndices, shortData.prompts.map(prompt => prompt.index).reverse(), 'short results must show all matches in chronological order');
    assert.equal(shortIndices.at(-1), shortLatest, 'latest match must be the last row');
    await modal.getByRole('button', { name: '\u4e0a\u4e00\u4e2a\u5339\u914d', exact: true }).click();
    await modal.locator(`[data-prompt-index="${shortData.prompts[1].index}"] .prompt-entry-active-match`).waitFor();
    await modal.getByRole('button', { name: '\u8df3\u5230\u6700\u65b0', exact: true }).click();
    await modal.locator(`[data-prompt-index="${shortLatest}"] .prompt-entry-active-match`).waitFor();
    await page.screenshot({ path: path.join(output, 'installed-short-search-chronological.png') });
    results.push({ shortResults: shortIndices, latest: shortLatest, chronological: true });
    assert.deepEqual(errors, []);
    results.push({ ui: true, contexts: contextResponses.length, newest: first.anchorIndex, next: second.anchorIndex, persistentStaleRequests: rejected, errors });
  }
  console.log(JSON.stringify(results));
} catch (error) {
  if (page) {
    await page.screenshot({ path: path.join(output, 'failure.png') });
    console.error(await page.locator('[role="dialog"]').evaluateAll(elements => elements.map(element => ({ className: element.className, text: element.textContent.slice(0, 1000) }))));
  }
  throw error;
} finally {
  await fs.writeFile(path.join(output, 'api-results.json'), JSON.stringify(results, null, 2));
  await api.dispose();
  await browser?.close();
}
