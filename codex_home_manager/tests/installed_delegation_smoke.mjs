import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const threadId = process.env.CHM_TEST_THREAD_ID || '019ff4e9-1fae-7053-8557-8cde76fcc757';
const baseUrl = process.env.CHM_TEST_BASE_URL || 'http://127.0.0.1:8765';
const output = path.resolve(import.meta.dirname, '../output/delegation-verification');
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errors = [];
page.on('pageerror', (error) => errors.push(error.message));
try {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.getByRole('textbox', { name: '搜索标题、线程 ID、项目、JSONL 路径' }).fill(threadId);
  const row = page.locator('.thread-table tbody tr').filter({ hasText: 'SystemCockpit' });
  await row.first().press('Enter');
  await page.locator('.thread-detail-modal').getByRole('button', { name: '查看线程内容', exact: true }).last().click();
  const modal = page.locator('.prompt-modal');
  await modal.getByRole('button', { name: '输入来源：线程转发', exact: true }).click();
  await modal.getByText(/最终UI验收已通过：1120\/1440/).waitFor({ timeout: 60000 });
  await modal.getByText(/本任务已完成并向用户交付/).waitFor();
  assert.equal(await modal.getByText('当前筛选没有可显示的线程内容', { exact: true }).count(), 0);
  await page.screenshot({ path: path.join(output, 'installed-delegation.png') });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ url: page.url(), title: await page.title(), meta: await modal.locator('.timeline-meta').innerText(), expectedMessagesVisible: 2, errors }));
} catch (error) {
  await page.screenshot({ path: path.join(output, 'failure.png') });
  console.error(await page.locator('.prompt-modal').evaluate((element) => ({ inertAncestor: element.closest('[inert]')?.className, pointerEvents: getComputedStyle(element).pointerEvents, rect: element.getBoundingClientRect().toJSON() })));
  throw error;
} finally { await browser.close(); }
