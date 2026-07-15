import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import ts from "typescript";
import { chromium } from "playwright";

const projectRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/(?:[A-Za-z]:)/, (value) => value.slice(1))), "..");
const mainPath = path.join(projectRoot, "src", "main.tsx");
const sourceText = fs.readFileSync(mainPath, "utf8");
const sourceFile = ts.createSourceFile(mainPath, sourceText, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

function visit(node, callback) {
  callback(node);
  ts.forEachChild(node, (child) => visit(child, callback));
}

function findVariableInitializer(variableName) {
  let initializer;
  visit(sourceFile, (node) => {
    if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || node.name.text !== variableName) return;
    initializer = node.initializer;
  });
  assert.ok(initializer, `missing variable: ${variableName}`);
  return initializer;
}

function findFunction(functionName) {
  let declaration;
  visit(sourceFile, (node) => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === functionName) declaration = node;
  });
  assert.ok(declaration, `missing function: ${functionName}`);
  return declaration;
}

function nodeText(node) {
  return node.getText(sourceFile);
}

const englishTextInitializer = findVariableInitializer("englishText");
assert.ok(ts.isObjectLiteralExpression(englishTextInitializer), "englishText must remain a statically inspectable object literal");
const translatedKeys = new Set(
  englishTextInitializer.properties
    .filter(ts.isPropertyAssignment)
    .map((property) => property.name)
    .filter((name) => ts.isStringLiteral(name) || ts.isNoSubstitutionTemplateLiteral(name))
    .map((name) => name.text)
);

const translationCalls = new Set();
visit(sourceFile, (node) => {
  if (!ts.isCallExpression(node) || !ts.isIdentifier(node.expression) || node.expression.text !== "t") return;
  const argument = node.arguments[0];
  if (argument && (ts.isStringLiteral(argument) || ts.isNoSubstitutionTemplateLiteral(argument))) {
    translationCalls.add(argument.text);
  }
});

const missingTranslations = [...translationCalls].filter((key) => !translatedKeys.has(key)).sort();
assert.deepEqual(
  missingTranslations,
  [],
  `all t("...") literals must exist in englishText; missing ${missingTranslations.length}:\n${missingTranslations.join("\n")}`
);

const emptyTranslations = englishTextInitializer.properties
  .filter(ts.isPropertyAssignment)
  .filter((property) => ts.isStringLiteral(property.name) || ts.isNoSubstitutionTemplateLiteral(property.name))
  .filter((property) => !ts.isStringLiteral(property.initializer) && !ts.isNoSubstitutionTemplateLiteral(property.initializer))
  .map((property) => property.name.getText(sourceFile));
assert.deepEqual(emptyTranslations, [], "englishText values must remain explicit string literals");

const nonLiteralTranslationCalls = [];
visit(sourceFile, (node) => {
  if (!ts.isCallExpression(node) || !ts.isIdentifier(node.expression) || node.expression.text !== "t") return;
  const argument = node.arguments[0];
  if (!argument || ts.isStringLiteral(argument) || ts.isNoSubstitutionTemplateLiteral(argument)) return;
  nonLiteralTranslationCalls.push(nodeText(argument));
});
const approvedDynamicTranslationArguments = new Set([
  "label",
  "internalChineseText[text] || text",
  "value"
]);
const unexpectedDynamicTranslationCalls = nonLiteralTranslationCalls.filter(
  (argument) => !approvedDynamicTranslationArguments.has(argument)
);
assert.deepEqual(
  unexpectedDynamicTranslationCalls,
  [],
  `dynamic t(...) calls must use an audited translation source; unexpected:\n${unexpectedDynamicTranslationCalls.join("\n")}`
);

const appText = nodeText(findFunction("App"));
const diagnosticsHookText = nodeText(findFunction("useDiagnostics"));
const initialThreadPageDiagnosticsRequestBudget = 0;
const diagnosticsEnabledOnInitialThreadPage = /useDiagnostics\([^;]+isLocalApiConnected\s*&&\s*!isBrowserMode\s*\)/s.test(appText);
const estimatedInitialDiagnosticsRequests = diagnosticsEnabledOnInitialThreadPage ? 1 : 0;

assert.equal(
  estimatedInitialDiagnosticsRequests,
  initialThreadPageDiagnosticsRequestBudget,
  "performance budget: the initial thread page may issue 0 full /api/diagnostics requests"
);
assert.match(appText, /useDiagnostics\([^;]+activeSection\s*===\s*["']diagnostics["']/s, "diagnostics must be enabled only while its page is active");
assert.match(diagnosticsHookText, /new AbortController\(\)/, "diagnostics requests must be cancellable");
assert.match(diagnosticsHookText, /signal:\s*[^,}\n]+\.signal/, "diagnostics fetch must receive the AbortController signal");
assert.match(diagnosticsHookText, /\.abort\(\)/, "leaving diagnostics or changing inputs must abort obsolete work");
assert.match(diagnosticsHookText, /inFlight/i, "diagnostics must deduplicate concurrent refreshes");
assert.match(diagnosticsHookText, /cache/i, "diagnostics must expose an explicit cache path");
assert.doesNotMatch(
  appText,
  /Promise\.all\(\[[^\]]*diagnosticsState\.refresh\(\)[^\]]*\]\)/s,
  "global refresh must not start diagnostics while another page is active"
);

function contentTypeForPath(filePath) {
  if (filePath.endsWith(".js")) return "text/javascript";
  if (filePath.endsWith(".css")) return "text/css";
  if (filePath.endsWith(".wasm")) return "application/wasm";
  return "text/html";
}

function snapshotPayload() {
  return {
    codexHome: "D:/Codex",
    databasePath: "D:/Codex/state_5.sqlite",
    globalStatePath: "D:/Codex/.codex-global-state.json",
    sessionIndexPath: "D:/Codex/session_index.jsonl",
    sidebarLimit: 50,
    generatedAtMs: Date.now(),
    summary: {
      totalThreads: 0,
      mainThreads: 0,
      subagentThreads: 0,
      eligibleThreads: 0,
      codexVisibleThreads: 0,
      hiddenByInitialLimit: 0,
      archivedThreads: 0,
      needsRepairThreads: 0,
      savedProjects: 0,
      workspaceProjects: 0,
      conversationProjects: 0,
      otherProjects: 0,
      emptyProjectsWithHiddenThreads: 0,
      totalStorageBytes: 0
    },
    threads: [],
    projects: []
  };
}

function diagnosticsPayload() {
  const capturedAtMs = Date.now();
  return {
    codexHome: "D:/Codex",
    generatedAtMs: capturedAtMs,
    score: 100,
    status: "pass",
    summary: { critical: 0, warning: 0, info: 0, pass: 1, checks: 1, issues: 0, threadCount: 0 },
    paths: {},
    codexProcesses: [],
    checks: [{
      id: "test.frontend",
      category: "test",
      title: "Frontend behavior",
      status: "pass",
      summary: "Test report",
      evidence: [],
      affectedPaths: []
    }],
    issues: [],
    topRecommendations: [],
    repairHints: {},
    capacityTrend: {
      schemaVersion: 1,
      retention: { cadence: "daily", maxAgeDays: 90, maxSnapshots: 90 },
      storage: { persisted: true, recoveredFromCorruption: false },
      current: {
        sessionsBytes: 5_368_709_120,
        largeThreadCount: 3,
        backupBytes: 1_073_741_824,
        backupFileCount: 42,
        backupScanTruncated: false,
        mcpProcessCount: 9,
        normalNodeReplProcessCount: 6,
        nodeReplRiskProcessCount: 0,
        legacyFallbackProcessCount: 1,
        xcodebuildProcessCount: 2,
        otherMcpServerProcessCount: 0
      },
      changes: {
        sessionsBytes: { direction: "up", delta: 1_073_741_824, percent: 25 },
        largeThreadCount: { direction: "down", delta: -1, percent: -25 },
        backupBytes: { direction: "flat", delta: 0, percent: 0 },
        backupFileCount: { direction: "up", delta: 2, percent: 5 },
        mcpProcessCount: { direction: "down", delta: -3, percent: -25 }
      },
      history: [
        { capturedAtMs: capturedAtMs - 172_800_000, sessionsBytes: 3_221_225_472, largeThreadCount: 5, backupBytes: 805_306_368, backupFileCount: 36, mcpProcessCount: 14 },
        { capturedAtMs: capturedAtMs - 86_400_000, sessionsBytes: 4_294_967_296, largeThreadCount: 4, backupBytes: 1_073_741_824, backupFileCount: 40, mcpProcessCount: 12 },
        { capturedAtMs, sessionsBytes: 5_368_709_120, largeThreadCount: 3, backupBytes: 1_073_741_824, backupFileCount: 42, mcpProcessCount: 9 }
      ]
    }
  };
}

async function runDiagnosticsBehaviorTest() {
  const distPath = path.join(projectRoot, "dist");
  assert.ok(fs.existsSync(path.join(distPath, "index.html")), "run npm run build before the frontend behavior test");
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    let diagnosticsRequests = 0;
    await page.addInitScript((apiBase) => {
      window.localStorage.setItem("codex-home-manager-api-base-url", apiBase);
    }, "http://127.0.0.1:8876");
    await page.route("**/*", async (route) => {
      const requestUrl = new URL(route.request().url());
      const pathname = requestUrl.pathname;
      if (requestUrl.hostname !== "127.0.0.1") {
        await route.abort();
        return;
      }
      if (pathname === "/api/auth/token") {
        await route.fulfill({ json: { token: "test-local-token", headerName: "X-Codex-Manager-Token", expiresAtMs: Date.now() + 300000 } });
        return;
      }
      if (pathname === "/api/health") {
        await route.fulfill({ json: { writeWarnings: [], currentVersions: {} } });
        return;
      }
      if (pathname === "/api/snapshot") {
        await route.fulfill({ json: snapshotPayload() });
        return;
      }
      if (pathname === "/api/home/overview") {
        await route.fulfill({
          json: {
            codexHome: "D:/Codex",
            resources: [],
            summary: { resourceCount: 0, existingResourceCount: 0, totalKnownResourceBytes: 0, agentsFileCount: 0, memoryExists: false, skillsExists: false },
            generatedAtMs: Date.now()
          }
        });
        return;
      }
      if (pathname === "/api/capabilities") {
        await route.fulfill({ json: { service: "test", version: "1", language: "zh", openapiPath: "/openapi.json", mcpPath: "/mcp", safetyModel: {}, commonQueryParameters: {}, capabilities: [] } });
        return;
      }
      if (pathname === "/api/diagnostics") {
        diagnosticsRequests += 1;
        await route.fulfill({ json: diagnosticsPayload() });
        return;
      }
      if (pathname.startsWith("/api/")) {
        await route.fulfill({ status: 404, body: `not found: ${pathname}` });
        return;
      }
      const relativePath = pathname === "/" ? "index.html" : decodeURIComponent(pathname).replace(/^\/+/, "");
      const candidatePath = path.resolve(distPath, relativePath);
      const filePath = candidatePath.startsWith(distPath) && fs.existsSync(candidatePath) && fs.statSync(candidatePath).isFile()
        ? candidatePath
        : path.join(distPath, "index.html");
      await route.fulfill({ body: fs.readFileSync(filePath), contentType: contentTypeForPath(filePath) });
    });

    await page.goto("http://127.0.0.1:8876", { waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "体检" }).waitFor();
    assert.equal(await page.evaluate(() => window.location.hostname), "127.0.0.1");
    assert.equal(diagnosticsRequests, 0, "local thread landing page must not start diagnostics");

    await page.getByRole("button", { name: "体检" }).click();
    try {
      await page.getByText("Codex Home 体检").waitFor({ timeout: 15000 });
    } catch (error) {
      const bodyText = (await page.locator("body").innerText()).slice(0, 4000);
      throw new Error(`diagnostics did not open; requests=${diagnosticsRequests}; body=${bodyText}\n${error}`);
    }
    assert.equal(diagnosticsRequests, 1, "opening diagnostics must request exactly one report");
    const capacityTrend = page.locator(".diagnostics-capacity-trends");
    await capacityTrend.waitFor();
    assert.equal(await capacityTrend.getAttribute("open"), null, "capacity trends must be collapsed by default");
    await capacityTrend.getByText("运行容量趋势").waitFor();
    const capacitySummary = capacityTrend.locator("summary");
    await capacitySummary.getByText("5.0 GB").waitFor();
    await capacitySummary.getByText("42 个文件").waitFor();
    await capacitySummary.press("Enter");
    assert.equal(await capacityTrend.getAttribute("open"), "", "capacity trends must support keyboard expansion");
    await capacityTrend.getByText("按日保留最近 90 天，最多 90 个快照。").waitFor();
    await capacityTrend.getByText("正常 node_repl", { exact: true }).waitFor();
    await capacityTrend.getByText("xcodebuild 风险", { exact: true }).waitFor();

    await page.setViewportSize({ width: 390, height: 844 });
    const trendOverflow = await capacityTrend.evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth
    }));
    assert.ok(
      trendOverflow.scrollWidth <= trendOverflow.clientWidth + 1,
      `capacity trends must not overflow at 390px: ${JSON.stringify(trendOverflow)}`
    );

    await page.locator('button[title="切换到英文"]').click();
    await capacityTrend.getByText("Capacity trends").waitFor();
    if (await capacityTrend.getAttribute("open") === null) {
      await capacityTrend.locator("summary").press("Enter");
    }
    await capacityTrend.getByText("Daily snapshots retained for 90 days, up to 90 snapshots.").waitFor();
    await page.locator('button[title="Switch to Chinese"]').click();
    await page.waitForTimeout(250);
    const requestsAfterLanguageCheck = diagnosticsRequests;
    assert.ok(
      requestsAfterLanguageCheck >= 1 && requestsAfterLanguageCheck <= 3,
      `language switching may reuse or fetch localized reports, received ${requestsAfterLanguageCheck} requests`
    );

    await page.getByRole("button", { name: "线程" }).click();
    await page.getByRole("button", { name: "体检" }).click();
    await page.getByText("Codex Home 体检").waitFor();
    assert.equal(diagnosticsRequests, requestsAfterLanguageCheck, "reopening diagnostics inside the cache TTL must reuse the localized report");
    const applicationErrors = await page.locator(".error-banner").allTextContents();
    assert.equal(applicationErrors.length, 0, `local diagnostics flow must not surface an application error: ${applicationErrors.join(" | ")}`);

    await page.getByRole("button", { name: "重新体检" }).click();
    await page.getByText("Codex Home 体检").waitFor();
    assert.equal(diagnosticsRequests, requestsAfterLanguageCheck + 1, "manual diagnostics refresh must bypass the cache");
  } finally {
    // In the Windows runner, Chromium exits but browser.close() can leave its pipe Promise pending.
    if (browser) void browser.close().catch(() => {});
  }
}

async function runHostedBehaviorTest() {
  const distPath = path.join(projectRoot, "dist");
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const localApiRequests = [];
    const popupUrls = [];
    page.on("popup", (popup) => popupUrls.push(popup.url()));
    await page.route("**/*", async (route) => {
      const requestUrl = new URL(route.request().url());
      if (["127.0.0.1", "localhost", "::1"].includes(requestUrl.hostname)) {
        localApiRequests.push(requestUrl.href);
        await route.abort();
        return;
      }
      if (requestUrl.hostname !== "codex-home-manager.simplezion.com") {
        await route.abort();
        return;
      }
      const pathname = requestUrl.pathname;
      const relativePath = pathname === "/" ? "index.html" : decodeURIComponent(pathname).replace(/^\/+/, "");
      const candidatePath = path.resolve(distPath, relativePath);
      const filePath = candidatePath.startsWith(distPath) && fs.existsSync(candidatePath) && fs.statSync(candidatePath).isFile()
        ? candidatePath
        : path.join(distPath, "index.html");
      await route.fulfill({ body: fs.readFileSync(filePath), contentType: contentTypeForPath(filePath) });
    });

    await page.goto("https://codex-home-manager.simplezion.com/", { waitUntil: "networkidle" });
    await page.getByRole("heading", { name: "启动本机扫描" }).waitFor();
    const launchLink = page.getByRole("link", { name: "启动本机连接器" });
    assert.equal(await launchLink.getAttribute("href"), "codex-home-manager://start");
    assert.equal(localApiRequests.length, 0, `hosted startup must not read localhost APIs: ${localApiRequests.join(" | ")}`);
    assert.equal(popupUrls.length, 0, `hosted startup must not open a local window without a user action: ${popupUrls.join(" | ")}`);
    assert.equal(await page.getByRole("button", { name: "体检" }).count(), 1, "hosted shell must retain the product navigation");
  } finally {
    if (browser) void browser.close().catch(() => {});
  }
}

let qualityExitCode = 0;
try {
  await runDiagnosticsBehaviorTest();
  await runHostedBehaviorTest();
  console.log(`frontend quality PASS: ${translationCalls.size} translated literals, ${nonLiteralTranslationCalls.length} audited dynamic calls, diagnostics startup budget ${estimatedInitialDiagnosticsRequests}/${initialThreadPageDiagnosticsRequestBudget}, hosted behavior verified`);
} catch (error) {
  qualityExitCode = 1;
  console.error(error instanceof Error ? error.stack || error.message : String(error));
} finally {
  // See the browser.close() note above. Allow stdout/stderr to flush before ending Node explicitly.
  setTimeout(() => process.exit(qualityExitCode), 25);
}
