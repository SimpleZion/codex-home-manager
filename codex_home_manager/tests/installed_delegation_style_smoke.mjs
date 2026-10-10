import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const root = path.resolve(import.meta.dirname, '..');
const preview = process.argv.includes('--dist');
const output = path.join(root, 'output/delegation-style-verification');
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
if (preview) await page.route('http://127.0.0.1:8765/**', async route => {
  const pathname = new URL(route.request().url()).pathname;
  if (pathname.startsWith('/api/')) return route.continue();
  const filename = path.resolve(root, 'dist', pathname === '/' ? 'index.html' : pathname.slice(1));
  assert.ok(filename.startsWith(path.join(root, 'dist') + path.sep));
  await route.fulfill({ body: await fs.readFile(filename), contentType: filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : filename.endsWith('.wasm') ? 'application/wasm' : 'text/html' });
});
function luminance(rgb) {
  const channels = rgb.match(/[\d.]+/g).slice(0, 3).map(value => Number(value) / 255).map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}
try {
  await page.goto('http://127.0.0.1:8765/', { waitUntil: 'domcontentloaded' });
  await page.getByRole('textbox', { name: '搜索标题、线程 ID、项目、JSONL 路径' }).fill('019e57fc-86e4-7232-ba97-3b01b3a18f1c');
  await page.locator('.thread-table tbody tr').filter({ hasText: 'Codex Home Manager' }).first().press('Enter');
  await page.locator('.thread-detail-modal').getByRole('button', { name: '查看线程内容', exact: true }).last().click();
  const modal = page.locator('.prompt-modal');
  await modal.getByRole('button', { name: '输入来源：线程转发', exact: true }).click();
  const entry = modal.locator('[data-timeline-id="byte-527645649"]');
  const results = [];
  for (const width of [1920, 900, 480]) {
    await page.setViewportSize({ width, height: 1080 });
    for (const expanded of [false, true]) {
      await entry.scrollIntoViewIfNeeded();
      const heading = entry.locator('.timeline-entry-head');
      if ((await heading.getAttribute('aria-expanded') === 'true') !== expanded) await heading.click();
      await entry.scrollIntoViewIfNeeded();
      const style = await entry.evaluate(element => {
        const computed = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        const marker = element.querySelector('.timeline-kind-marker');
        return { background: computed.backgroundColor, edge: computed.borderRightColor, edgeWidth: computed.borderRightWidth, color: getComputedStyle(marker).color, icon: Boolean(element.querySelector('.lucide-forward')), overflows: element.scrollWidth > element.clientWidth + 1, left: rect.left, right: rect.right };
      });
      assert.equal(style.background, 'rgb(245, 239, 251)');
      assert.equal(style.edgeWidth, '4px');
      assert.ok(style.icon, 'forwarding must have a directional icon, not generic context icon');
      assert.equal(style.overflows, false);
      assert.ok(style.left >= 0 && style.right <= width, JSON.stringify(style));
      const contrast = (luminance(style.background) + 0.05) / (luminance(style.color) + 0.05);
      assert.ok(contrast >= 4.5, `label contrast ${contrast}`);
      await page.screenshot({ path: path.join(output, `${preview ? 'preview' : 'installed'}-${width}-${expanded ? 'expanded' : 'collapsed'}.png`) });
      results.push({ width, expanded, contrast, ...style });
    }
  }
  assert.deepEqual(errors, []);
  await fs.writeFile(path.join(output, `${preview ? 'preview' : 'installed'}-results.json`), JSON.stringify({ results, errors }, null, 2));
  console.log(JSON.stringify({ results, errors }));
} catch (error) {
  await page.screenshot({ path: path.join(output, 'failure.png') });
  throw error;
} finally { await browser.close(); }
