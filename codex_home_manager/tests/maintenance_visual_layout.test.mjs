import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const output = process.env.COCKPIT_TEST_OUTPUT;
assert.ok(output, "A screenshot output directory is required");
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const results = [];
try {
  const origin = process.env.COCKPIT_TEST_URL || "http://127.0.0.1:8765";
  await page.goto(`${origin}/?api_base=${encodeURIComponent(origin)}`, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "维护中心", exact: true }).click();
  await page.getByRole("heading", { name: "独立修复版生产切换与验收", exact: true }).waitFor();
  for (const [name, filename] of [["概览", "overview"], ["逐项变更", "changes"], ["证据与验证", "evidence"], ["决策与回滚", "decision"]]) {
    await page.getByRole("tab", { name: new RegExp(`^${name}`) }).click();
    await page.screenshot({ path: path.join(output, `${filename}.png`) });
    const layout = await page.locator(".maintenance-center").evaluate(root => {
      const panel = root.querySelector('[role="tabpanel"]');
      const list = root.querySelector(".maintenance-list");
      const header = root.querySelector(".maintenance-header").getBoundingClientRect();
      const tabs = root.querySelector(".maintenance-tabs").getBoundingClientRect();
      panel.scrollTop = panel.scrollHeight;
      list.scrollTop = list.scrollHeight;
      const headerAfter = root.querySelector(".maintenance-header").getBoundingClientRect();
      const tabsAfter = root.querySelector(".maintenance-tabs").getBoundingClientRect();
      const bad = [...root.querySelectorAll("*")].filter(element => {
        const style = getComputedStyle(element);
        return element.clientWidth > 0 && element.scrollWidth > element.clientWidth + 2 && !["auto", "scroll", "hidden"].includes(style.overflowX);
      }).map(element => element.className).slice(0, 8);
      const index = root.querySelector(".maintenance-index").getBoundingClientRect();
      const detail = root.querySelector(".maintenance-detail").getBoundingClientRect();
      return { pageOverflow: root.scrollHeight - root.clientHeight, headerShift: headerAfter.y - header.y, tabShift: tabsAfter.y - tabs.y, panelHeight: panel.clientHeight, indexWidth: Math.round(index.width), workspaceTopDelta: Math.round(detail.top - index.top), rows: [...list.children].slice(0, 6).map(row => Math.round(row.getBoundingClientRect().height)), bad };
    });
    assert.ok(layout.pageOverflow <= 1, `${name}: the module must not scroll away`);
    assert.equal(layout.headerShift, 0);
    assert.equal(layout.tabShift, 0);
    assert.ok(layout.panelHeight > 300);
    assert.ok(layout.indexWidth >= 300 && layout.indexWidth <= 352);
    assert.equal(layout.workspaceTopDelta, 0);
    assert.ok(layout.rows.every(height => height >= 70 && height <= 108));
    assert.deepEqual(layout.bad, []);
    results.push({ name, ...layout });
    await page.locator(".maintenance-list").evaluate(element => { element.scrollTop = 0; });
  }
  await page.setViewportSize({ width: 2034, height: 1187 });
  await page.getByRole("navigation", { name: "维护变更清单" }).getByRole("button").filter({ hasText: "6511分页与新轮正文修复包" }).click();
  await page.getByRole("tab", { name: "决策与回滚", exact: true }).click();
  await page.screenshot({ path: path.join(output, "wide-decision.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("tab", { name: "概览", exact: true }).click();
  await page.locator(".maintenance-center").evaluate(element => element.scrollIntoView({ block: "start" }));
  const mobile = await page.locator('[role="tabpanel"]').evaluate(element => ({ height: element.clientHeight, top: element.getBoundingClientRect().top, bottom: element.getBoundingClientRect().bottom }));
  assert.ok(mobile.height >= 120 && mobile.bottom <= 845, JSON.stringify(mobile));
  await page.screenshot({ path: path.join(output, "mobile-overview.png") });
  await page.getByRole("button", { name: "返回清单", exact: true }).click();
  await page.screenshot({ path: path.join(output, "mobile-index.png") });
  await fs.writeFile(path.join(output, "layout.json"), JSON.stringify({ passed: true, results, mobile }, null, 2));
  console.log(JSON.stringify({ passed: true, output }));
} finally {
  await browser.close();
}
