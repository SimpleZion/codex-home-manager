import { mkdir } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const serviceUrl = process.argv[2] || "http://127.0.0.1:8765";
const threadTitle = process.argv[3] || "Quant Research Platform-new";
const evidenceDirectory = path.resolve("build", "prompt-order-evidence");

function assertCondition(condition, message) {
  if (!condition) throw new Error(message);
  console.log(`ok: ${message}`);
}

function promptNumbers(values) {
  return values
    .map((value) => Number(String(value).match(/Prompt\s+([\d,]+)/)?.[1]?.replaceAll(",", "")))
    .filter(Number.isFinite);
}

async function visiblePromptNumbers(dialog) {
  return promptNumbers(await dialog.locator(".prompt-entry-head strong").allTextContents());
}

async function openPromptDialog(page) {
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  const row = page.locator(".thread-table tbody tr", { hasText: threadTitle }).first();
  await row.waitFor({ timeout: 90_000 });
  await row.dblclick();
  const detailDialog = page.locator(".thread-detail-modal");
  await detailDialog.waitFor({ timeout: 30_000 });
  await detailDialog.getByRole("button", { name: "查看线程内容" }).last().click();
  const promptDialog = page.locator(".prompt-modal");
  await promptDialog.waitFor({ timeout: 30_000 });
  await promptDialog.getByRole("tab", { name: "我的输入" }).click();
  await promptDialog.locator(".prompt-entry").first().waitFor({ timeout: 90_000 });
  return promptDialog;
}

async function verifyViewport(page, width, height, label) {
  await page.setViewportSize({ width, height });
  const dialog = await openPromptDialog(page);
  const newestButton = dialog.getByRole("button", { name: "最新在前" });
  const chronologicalButton = dialog.getByRole("button", { name: "时间顺序" });
  const jumpButton = dialog.getByRole("button", { name: "跳到最新" });

  assertCondition(await newestButton.getAttribute("aria-pressed") === "true", `${label} defaults to newest-first`);
  const newestNumbers = await visiblePromptNumbers(dialog);
  assertCondition(newestNumbers.length >= 2, `${label} renders more than one prompt without loading the full history`);
  assertCondition(newestNumbers[0] > newestNumbers[1], `${label} newest-first records are descending`);

  await chronologicalButton.click();
  await page.waitForFunction(() => document.querySelector('.prompt-order-control button[aria-pressed="true"]')?.textContent?.includes("时间顺序"));
  await dialog.locator(".prompt-entry").first().waitFor({ timeout: 90_000 });
  const chronologicalNumbers = await visiblePromptNumbers(dialog);
  assertCondition(chronologicalNumbers.length >= 1, `${label} chronological records render`);
  assertCondition(chronologicalNumbers[0] < newestNumbers[0], `${label} chronological order starts from an earlier record`);
  if (chronologicalNumbers.length >= 2) {
    assertCondition(chronologicalNumbers[0] < chronologicalNumbers[1], `${label} chronological records are ascending`);
  }

  const list = dialog.locator(".prompt-list");
  await list.evaluate((element) => { element.scrollTop = Math.min(element.scrollHeight, 1200); });
  assertCondition(await list.evaluate((element) => element.scrollTop > 0), `${label} prompt list can scroll independently`);
  await jumpButton.click();
  await page.waitForFunction(() => document.querySelector('.prompt-order-control button[aria-pressed="true"]')?.textContent?.includes("最新在前"));
  await dialog.locator(".prompt-entry").first().waitFor({ timeout: 90_000 });
  assertCondition(await list.evaluate((element) => element.scrollTop <= 1), `${label} jump-to-latest resets the virtual list`);

  const metrics = await dialog.evaluate((element) => ({
    dialogClientWidth: element.clientWidth,
    dialogScrollWidth: element.scrollWidth,
    bodyClientWidth: document.documentElement.clientWidth,
    bodyScrollWidth: document.documentElement.scrollWidth,
    renderedPromptCount: element.querySelectorAll(".prompt-entry").length,
    totalPromptLabel: element.querySelector(".prompt-count-summary")?.textContent || ""
  }));
  assertCondition(metrics.dialogScrollWidth <= metrics.dialogClientWidth + 1, `${label} prompt dialog has no horizontal overflow`);
  assertCondition(metrics.bodyScrollWidth <= metrics.bodyClientWidth + 1, `${label} page has no horizontal overflow`);
  assertCondition(metrics.renderedPromptCount < 100, `${label} keeps prompt DOM window bounded`);

  await mkdir(evidenceDirectory, { recursive: true });
  await dialog.screenshot({
    path: path.join(evidenceDirectory, `${label}.png`),
    mask: [dialog.locator(".prompt-entry pre,.prompt-entry .prompt-body")],
    maskColor: "#d9dee8"
  });
  await dialog.getByRole("button", { name: "关闭详情窗口" }).click();
  await dialog.waitFor({ state: "detached" });
  console.log(`${label} metrics: ${JSON.stringify(metrics)}`);
}

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  page.on("pageerror", (error) => { throw error; });
  await verifyViewport(page, 1920, 1080, "desktop");
  await verifyViewport(page, 860, 980, "narrow");
  console.log("prompt order rendered validation PASS");
} finally {
  await browser.close();
}
