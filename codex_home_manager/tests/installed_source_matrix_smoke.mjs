import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const threadId = '019e57fc-86e4-7232-ba97-3b01b3a18f1c';
const baseUrl = 'http://127.0.0.1:8765';
const output = path.resolve(import.meta.dirname, '../output/source-matrix-verification');
await fs.mkdir(output, { recursive: true });
const expectedOffsets = [526402473, 527645649];
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const results = [];
try {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.getByRole('textbox', { name: '搜索标题、线程 ID、项目、JSONL 路径' }).fill(threadId);
  await page.locator('.thread-table tbody tr').filter({ hasText: 'Codex Home Manager' }).first().press('Enter');
  await page.locator('.thread-detail-modal').getByRole('button', { name: '查看线程内容', exact: true }).last().click();
  const modal = page.locator('.prompt-modal');
  const list = modal.getByTestId('thread-timeline-list');
  for (const width of [1280, 1920]) {
    await page.setViewportSize({ width, height: 1080 });
    await modal.getByRole('button', { name: '输入来源：全部来源', exact: true }).click();
    assert.equal(await modal.evaluate(element => Boolean(element.closest('[inert]'))), false, 'top dialog must remain interactive after background detail relayout');
  }
  for (const [kind, kindLabel] of [['conversation', '主要内容'], ['user', '用户输入']]) {
    await modal.getByRole('group', { name: '时间线类型筛选' }).getByRole('button', { name: kindLabel, exact: true }).click();
    for (const [source, sourceLabel] of [['delegation', '线程转发'], ['all', '全部来源']]) {
      const responsePromise = page.waitForResponse(response => {
        const url = new URL(response.url());
        return url.pathname.endsWith(`/threads/${threadId}/timeline`) && url.searchParams.get('kind') === kind && url.searchParams.get('sourceFilter') === source;
      });
      await modal.getByRole('button', { name: `输入来源：${sourceLabel}`, exact: true }).click();
      const data = await (await responsePromise).json();
      for (const offset of expectedOffsets) assert.ok(data.items.some(item => item.byteOffset === offset && item.promptSourceType === 'delegation'), `${kind}/${source}: missing ${offset}`);
      if (kind === 'conversation' && source === 'all') assert.ok(data.items.some(item => item.kind === 'assistant'));
      const visibleOffsets = new Set();
      // Walk the actual virtualized list, not just its API response.
      for (let step = 0; step < 100 && visibleOffsets.size < expectedOffsets.length; step++) {
        for (const offset of expectedOffsets) {
          const item = data.items.find(item => item.byteOffset === offset);
          const entry = modal.locator(`[data-timeline-id="${item.id}"]`);
          if (await entry.count()) {
            const attached = await entry.scrollIntoViewIfNeeded().then(() => true).catch(error => {
              if (error.message.includes('not attached to the DOM')) return false;
              throw error;
            });
            if (!attached) continue;
            assert.match(await entry.innerText(), /线程转发/);
            const heading = entry.locator('.timeline-entry-head');
            if (await heading.getAttribute('aria-expanded') === 'false') await heading.click();
            assert.ok((await entry.innerText()).includes(offset === 526402473 ? 'frontend sourceDigest' : '安装后独立复核更新'), 'rendered body must contain the original forwarded text');
            await page.screenshot({ path: path.join(output, `${kind}-${source}-${offset}.png`) });
            visibleOffsets.add(offset);
          }
        }
        if (visibleOffsets.size < expectedOffsets.length) {
          await list.evaluate(element => { element.scrollTop = Math.max(450, element.scrollTop - element.clientHeight * 0.7); });
          await page.waitForTimeout(70);
        }
      }
      assert.equal(visibleOffsets.size, 2, `${kind}/${source}: missing rendered messages`);
      results.push({ kind, source, loaded: data.items.length, verifiedOffsets: [...visibleOffsets] });
    }
  }
  for (const [offset, query] of [[526402473, 'frontend sourceDigest'], [527645649, '安装后独立复核更新']]) {
    let data;
    for (let attempt = 0; attempt < 40; attempt++) {
      const params = new URLSearchParams({ kind: 'conversation', sourceFilter: 'all', search: query, scanBudgetMs: '1000', limit: '200' });
      const response = await page.request.get(`${baseUrl}/api/threads/${threadId}/timeline/search/page?${params}`, { timeout: 30000 });
      assert.ok(response.ok(), await response.text());
      data = await response.json();
      if (data.matches.some(item => item.byteOffset === offset)) break;
      if (data.index?.complete) break;
    }
    assert.ok(data.matches.some(item => item.byteOffset === offset), `persistent search missing original message ${offset}`);
    results.push({ kind: 'conversation', source: 'all', search: query, verifiedOffset: offset });
  }
  assert.deepEqual(errors, []);
  await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ results, errors }, null, 2));
  console.log(JSON.stringify({ results, errors }));
} catch (error) {
  await page.screenshot({ path: path.join(output, 'failure.png') });
  console.error(await page.locator('.prompt-modal').evaluate(element => ({ inertAncestor: element.closest('[inert]')?.outerHTML.slice(0, 240), dialogs: [...document.querySelectorAll('[role="dialog"]')].map(dialog => ({ className: dialog.className, inertAncestor: dialog.closest('[inert]')?.className })) })));
  throw error;
} finally { await browser.close(); }
