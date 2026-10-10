import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium, request } from 'playwright';

const baseUrl = 'http://127.0.0.1:8765';
const threadId = '019e57fc-86e4-7232-ba97-3b01b3a18f1c';
const output = path.resolve(import.meta.dirname, '../output/search-latency-verification');
await fs.mkdir(output, { recursive: true });
const samples = [];
const errors = [];
const api = await request.newContext({ timeout: 10000 });
let browser;
let page;

async function search(port, query, offset) {
  const params = new URLSearchParams({ kind: 'conversation', sourceFilter: 'all', search: query, scanBudgetMs: '1000', limit: '200' });
  const start = performance.now();
  const response = await api.get(`http://127.0.0.1:${port}/api/threads/${threadId}/timeline/search/page?${params}`);
  assert.ok(response.ok(), await response.text());
  const data = await response.json();
  samples.push({ port, query, elapsedMs: Math.round(performance.now() - start), matches: data.matchCount, complete: data.matchCountComplete });
  assert.ok(data.matchCountComplete, 'existing persistent index must be complete');
  assert.ok(data.matches.some(item => item.byteOffset === offset), 'original message must remain searchable');
}

try {
  // Exercise the original failing request through both the proxy and its backend.
  for (let round = 0; round < 5; round++) {
    for (const port of [8765, 8766]) {
      await search(port, 'frontend sourceDigest', 526402473);
      await search(port, '安装后独立复核更新', 527645649);
    }
  }
  for (let round = 0; round < 3; round++) {
    await Promise.all([search(8765, 'frontend sourceDigest', 526402473), search(8765, '安装后独立复核更新', 527645649)]);
  }
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.getByRole('textbox', { name: '搜索标题、线程 ID、项目、JSONL 路径' }).fill(threadId);
  await page.locator('.thread-table tbody tr').filter({ hasText: 'Codex Home Manager' }).first().press('Enter');
  await page.locator('.thread-detail-modal').getByRole('button', { name: '查看线程内容', exact: true }).last().click();
  const modal = page.locator('.prompt-modal');
  await modal.getByRole('button', { name: '输入来源：全部来源', exact: true }).click();
  const searchBox = modal.getByRole('textbox', { name: '搜索完整线程内容', exact: true });
  for (const query of ['frontend sourceDigest', '安装后独立复核更新', 'frontend sourceDigest']) {
    const start = performance.now();
    const pending = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname.endsWith('/timeline/search/page') && url.searchParams.get('search') === query;
    }, { timeout: 10000 });
    await searchBox.fill(query);
    const response = await pending;
    assert.ok(response.ok());
    assert.ok((await response.json()).matchCount > 0);
    await modal.locator('.timeline-search-count').filter({ hasText: /1\s*\/\s*1/ }).waitFor({ timeout: 10000 });
    const next = modal.getByRole('button', { name: '下一个匹配', exact: true });
    await next.click();
    await modal.getByRole('button', { name: '上一个匹配', exact: true }).click();
    await modal.locator('.timeline-entry').filter({ hasText: query }).first().waitFor({ state: 'visible', timeout: 10000 });
    samples.push({ ui: true, query, elapsedMs: Math.round(performance.now() - start) });
  }
  await page.screenshot({ path: path.join(output, 'installed-search.png') });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ samples, errors }));
} catch (error) {
  errors.push(error.message);
  if (page) await page.screenshot({ path: path.join(output, 'failure.png') });
  throw error;
} finally {
  await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ samples, errors }, null, 2));
  await browser?.close();
  await api.dispose();
}
