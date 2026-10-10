import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import AxeBuilder from "@axe-core/playwright";

const origin = process.env.COCKPIT_TEST_URL || "http://127.0.0.1:8765";
const output = process.env.COCKPIT_TEST_OUTPUT || path.resolve("output/cockpit-tests", `ui-${Date.now()}`);
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const result = { browser: "Playwright Chromium", fallback: "CUA initialization returned failed to write kernel assets (os error 3); repository Playwright regression validates Cockpit independently", checks: [] };
const check = (name) => result.checks.push(name);

async function overflowCheck() {
  const overflowing = await page.locator(".maintenance-center").evaluate((root) => [...root.querySelectorAll("*")].filter((element) => {
    const style = getComputedStyle(element);
    return element.clientWidth > 0 && element.scrollWidth > element.clientWidth + 2 && !["auto", "scroll", "hidden"].includes(style.overflowX);
  }).map((element) => `${element.tagName}.${element.className}`).slice(0, 12));
  assert.deepEqual(overflowing, []);
}

try {
  await page.goto(`${origin}/?api_base=${encodeURIComponent(origin)}`, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "维护中心", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "只读核验" }).waitFor();
  assert.equal(await page.title(), "Codex Cockpit");
  assert.ok(await page.getByRole("heading", { name: "独立修复版生产切换与验收", exact: true }).isVisible());
  check("production switch evidence is the initial catalog entry");
  await page.getByRole("navigation", { name: "维护变更清单" }).getByRole("button").filter({ hasText: "6511分页与新轮正文修复包" }).click();
  assert.ok(await page.getByRole("heading", { name: "6511分页与新轮正文修复包", exact: true }).isVisible());
  await overflowCheck();
  await page.screenshot({ path: path.join(output, "cockpit-desktop.png") });
  check("desktop identity, populated catalog, current evidence and no overflow");

  await page.getByRole("tab", { name: /逐项变更/ }).click();
  assert.ok(await page.getByText("变更 13", { exact: true }).count());
  await page.getByRole("tab", { name: /逐项变更/ }).press("ArrowRight");
  assert.equal(await page.getByRole("tab", { name: "证据与验证", exact: true }).getAttribute("aria-selected"), "true");
  const fingerprintButton = page.getByRole("button", { name: "核验文件指纹", exact: true }).first();
  if (await fingerprintButton.isDisabled()) {
    await page.locator(".maintenance-source").getByText(/源证据工作区/).click();
    await page.getByLabel("源证据工作区路径", { exact: true }).fill(process.env.COCKPIT_SOURCE_WORKSPACE || path.resolve(".."));
    await page.getByRole("button", { name: "只读绑定", exact: true }).click();
    await fingerprintButton.waitFor({ state: "visible" });
    await page.waitForFunction(() => !document.querySelector(".maintenance-evidence button")?.disabled);
  }
  await page.getByRole("button", { name: "核验安装包哈希", exact: true }).click();
  await page.getByText("本次读取与准备回执哈希一致。未安装，签名和当前运行态仍须另验。", { exact: true }).waitFor();
  await fingerprintButton.click();
  await page.locator(".maintenance-evidence output").filter({ hasText: "SHA-256" }).waitFor();
  check("all pagination subchanges, keyboard tabs, package and evidence fingerprints");

  await page.getByRole("tab", { name: "决策与回滚", exact: true }).click();
  await page.getByRole("combobox", { name: /^处理意向/ }).selectOption("defer");
  await page.getByLabel("补充意见", { exact: true }).fill("QA: verify before deployment");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出决策计划", exact: true }).click();
  const download = await downloadPromise;
  assert.match(download.suggestedFilename(), /^codex-cockpit-[a-z0-9-]+-decision\.json$/);
  const planPath = path.join(output, download.suggestedFilename());
  await download.saveAs(planPath);
  const plan = JSON.parse(await fs.readFile(planPath, "utf8"));
  assert.equal(plan.executionAuthorized, false);
  assert.equal(plan.readiness, "blocked");
  assert.equal(plan.decision, "defer");
  assert.equal(plan.artifactVerification.hashMatches, true);
  assert.ok(plan.fingerprints.length > 0 && plan.fingerprints[0].sha256.length === 64);
  assert.equal(plan.catalogSha256, plan.observation.catalogSha256);
  assert.ok(plan.safetyGates.length >= 4);
  check("export preserves decision, fingerprints, catalog binding and execution blockers");

  await page.getByLabel("变更状态", { exact: true }).selectOption("withdrawn");
  assert.equal(await page.getByRole("navigation", { name: "维护变更清单" }).getByRole("button").count(), 2);
  await page.getByLabel("搜索维护变更", { exact: true }).fill("no-match-qa");
  await page.getByRole("heading", { name: "没有匹配的变更" }).waitFor();
  await page.getByRole("button", { name: "清除筛选", exact: true }).click();
  check("status filter, search empty state and reset");

  await page.getByRole("button", { name: "核验运行环境", exact: true }).click();
  await page.locator(".maintenance-runtime-summary button").first().waitFor();
  assert.equal(await page.locator(".maintenance-runtime-summary button").count(), 4);
  check("existing runtime detectors integrated without mutation");

  const accessibility = await new AxeBuilder({ page }).include(".maintenance-center").analyze();
  await fs.writeFile(path.join(output, "accessibility.json"), JSON.stringify(accessibility.violations.map((item) => ({ id: item.id, impact: item.impact, nodes: item.nodes.map((node) => ({ target: node.target, reason: node.failureSummary })) })), null, 2));
  assert.deepEqual(accessibility.violations.filter((item) => ["serious", "critical"].includes(item.impact)).map((item) => ({ id: item.id, nodes: item.nodes.length })), []);
  check("no serious or critical accessibility violations in maintenance center");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "返回清单", exact: true }).click();
  await page.getByRole("navigation", { name: "维护变更清单" }).getByRole("button").first().click();
  await page.getByRole("tab", { name: "概览", exact: true }).click();
  await page.locator(".maintenance-center").evaluate((element) => { element.scrollTop = 0; element.scrollIntoView({ block: "start" }); });
  await overflowCheck();
  await page.screenshot({ path: path.join(output, "cockpit-mobile.png") });
  await page.getByRole("button", { name: "返回清单", exact: true }).click();
  assert.ok(await page.getByRole("navigation", { name: "维护变更清单" }).isVisible());
  check("390px detail view, return navigation and text containment");

  await page.route("**/api/**", (route) => route.abort("failed"));
  await page.reload();
  await page.getByRole("button", { name: "维护中心", exact: true }).click();
  assert.equal(await page.getByText("静态变更目录 · 本机状态未验证", { exact: true }).count(), 1);
  assert.ok(await page.getByRole("navigation", { name: "维护变更清单" }).isVisible());
  check("offline catalog is accessible without connector gate");
  assert.deepEqual(errors, []);
  check("no page runtime exceptions");
} finally {
  await fs.writeFile(path.join(output, "cockpit-ui-result.json"), JSON.stringify({ ...result, errors }, null, 2));
  await browser.close();
}
console.log(JSON.stringify({ status: "passed", checks: result.checks.length, output }));
