import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const root = path.resolve(import.meta.dirname, '..');
const output = path.join(root, 'output/status-layout-verification');
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (error) => errors.push(error.message));
if (process.argv.includes('--dist')) {
  await page.route('http://127.0.0.1:8765/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.startsWith('/api/')) return route.continue();
    const filename = path.resolve(root, 'dist', pathname === '/' ? 'index.html' : pathname.slice(1));
    assert.ok(filename.startsWith(path.join(root, 'dist') + path.sep));
    const contentType = filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : filename.endsWith('.wasm') ? 'application/wasm' : 'text/html';
    await route.fulfill({ body: await fs.readFile(filename), contentType });
  });
}
try {
  await page.goto('http://127.0.0.1:8765/', { waitUntil: 'domcontentloaded' });
  const status = page.getByRole('group', { name: '应用状态提示' });
  await status.waitFor();
  const results = [];
  for (const width of [1920, 1280, 900]) {
    await page.setViewportSize({ width, height: 900 });
    const bounds = await status.evaluate((element) => {
      const box = element.getBoundingClientRect();
      return { width: box.width, height: box.height, children: [...element.children].map((child) => {
        const rect = child.getBoundingClientRect();
        return { x: rect.x - box.x, y: rect.y - box.y, width: rect.width, height: rect.height };
      }) };
    });
    assert.ok(bounds.height <= (width >= 1280 ? 40 : 80), JSON.stringify(bounds));
    assert.ok(bounds.children.every((child) => child.x >= 0 && child.x + child.width <= bounds.width));
    assert.ok(bounds.children[0].x <= 20, 'notices should align with the content, not leave a broad blank row');
    if (width >= 1280) assert.ok(bounds.children.every((child) => child.y === bounds.children[0].y), 'desktop notices share one row');
    await page.screenshot({ path: path.join(output, `status-${width}.png`) });
    results.push({ viewportWidth: width, ...bounds });
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ results, errors }));
} finally { await browser.close(); }
