import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import AxeBuilder from "@axe-core/playwright";
import { chromium } from "playwright";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const distPath = path.join(projectRoot, "dist");
const applicationUrl = "http://127.0.0.1:8877";

function contentTypeForPath(filePath) {
  if (filePath.endsWith(".js")) return "text/javascript";
  if (filePath.endsWith(".css")) return "text/css";
  if (filePath.endsWith(".wasm")) return "application/wasm";
  return "text/html";
}

function contrastRatio(foreground, background) {
  const channels = (value) => value.match(/[\d.]+/g).slice(0, 3).map(Number);
  const luminance = (value) => {
    const [red, green, blue] = channels(value).map((channel) => {
      const normalized = channel / 255;
      return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  };
  const foregroundLuminance = luminance(foreground);
  const backgroundLuminance = luminance(background);
  return (Math.max(foregroundLuminance, backgroundLuminance) + 0.05)
    / (Math.min(foregroundLuminance, backgroundLuminance) + 0.05);
}

async function assertTimelineBodyContrast(page) {
  const styles = await page.evaluate(() => {
    const kinds = ["user", "commentary", "assistant", "reasoning", "tool_call", "tool_output", "developer", "system", "context", "status"];
    const host = document.createElement("div");
    host.style.position = "fixed";
    host.style.left = "-10000px";
    document.body.append(host);
    const results = kinds.map((kind) => {
      const entry = document.createElement("article");
      entry.className = `timeline-entry timeline-entry-${kind}`;
      entry.innerHTML = '<div class="timeline-entry-body"><pre>timeline body</pre></div>';
      host.append(entry);
      const computed = getComputedStyle(entry.querySelector("pre"));
      return { kind, color: computed.color, backgroundColor: computed.backgroundColor };
    });
    host.remove();
    return results;
  });
  const expectedStyle = styles[0];
  for (const style of styles) {
    assert.equal(style.color, expectedStyle.color, `${style.kind} timeline text must use the shared foreground`);
    assert.equal(style.backgroundColor, expectedStyle.backgroundColor, `${style.kind} timeline text must use the shared background`);
    assert.ok(
      contrastRatio(style.color, style.backgroundColor) >= 4.5,
      `${style.kind} timeline body contrast must meet WCAG AA: ${JSON.stringify(style)}`
    );
  }
}

const thread = {
  id: "thread-1",
  title: "Accessibility verification thread",
  sqliteTitle: "Accessibility verification thread",
  sidebarTitle: "Accessibility verification thread",
  sessionIndexTitle: "Accessibility verification thread",
  sessionIndexUpdatedAt: "2026-07-11T00:00:00Z",
  rolloutTitle: "",
  rolloutTitleTimestamp: "",
  rolloutTitleLine: null,
  preview: "Keyboard and dialog verification",
  projectPath: "C:/Projects/accessibility",
  projectLabel: "accessibility",
  projectKind: "workspace_project",
  rolloutPath: "D:/Codex/sessions/thread-1.jsonl",
  source: "sqlite",
  threadKind: "main",
  threadSource: "cli",
  parentThreadId: "",
  subagentStatus: "",
  agentNickname: "",
  agentRole: "",
  model: "gpt-5",
  createdAtMs: Date.now() - 1000,
  updatedAtMs: Date.now(),
  archived: false,
  archivedAtMs: null,
  hasUserEvent: true,
  hasUserSignal: true,
  tokensUsed: 1200,
  childTokensUsed: 0,
  totalTokensUsed: 1200,
  fileExists: true,
  fileSizeBytes: 4096,
  childThreadCount: 0,
  childFileSizeBytes: 0,
  totalFileSizeBytes: 4096,
  fileModifiedAtMs: Date.now(),
  rolloutInArchivedStore: false,
  recentRank: 1,
  threadListRank: 1,
  sessionIndexRank: 1,
  isPinned: false,
  explicitSidebarReference: false,
  inInitialSidebarPage: true,
  outsideInitialLimit: false,
  codexVisible: true,
  visibility: "visible",
  hiddenReasons: [],
  gitBranch: "master",
  cliVersion: "test"
};

function snapshotPayload() {
  return {
    codexHome: "D:/Codex",
    databasePath: "D:/Codex/state_5.sqlite",
    globalStatePath: "D:/Codex/.codex-global-state.json",
    sessionIndexPath: "D:/Codex/session_index.jsonl",
    sidebarLimit: 50,
    generatedAtMs: Date.now(),
    summary: {
      totalThreads: 1,
      mainThreads: 1,
      subagentThreads: 0,
      eligibleThreads: 1,
      codexVisibleThreads: 1,
      hiddenByInitialLimit: 0,
      archivedThreads: 0,
      needsRepairThreads: 0,
      savedProjects: 1,
      workspaceProjects: 1,
      conversationProjects: 0,
      otherProjects: 0,
      emptyProjectsWithHiddenThreads: 0,
      totalStorageBytes: 4096
    },
    threads: [thread],
    projects: [{
      path: thread.projectPath,
      label: thread.projectLabel,
      projectKind: "workspace_project",
      total: 1,
      mainThreads: 1,
      subagentThreads: 0,
      active: 1,
      visible: 1,
      hiddenByInitialLimit: 0,
      archived: 0,
      needsRepair: 0,
      storageBytes: 4096,
      emptyButHasHiddenThreads: false
    }]
  };
}

function detailPayload() {
  return {
    thread,
    sqliteRow: {},
    rolloutStats: {
      lineCount: 12,
      userMessages: 2,
      assistantMessages: 2,
      toolCalls: 1,
      toolOutputs: 1,
      eventMessages: 2,
      invalidJsonLines: 0,
      firstTimestamp: "2026-07-11T00:00:00Z",
      lastTimestamp: "2026-07-11T00:01:00Z"
    },
    backups: []
  };
}

function diagnosticsPayload() {
  const capturedAtMs = Date.now();
  return {
    codexHome: "D:/Codex",
    generatedAtMs: capturedAtMs,
    score: 90,
    status: "warning",
    summary: { critical: 0, warning: 1, info: 0, pass: 0, checks: 1, issues: 0, threadCount: 1 },
    paths: {},
    codexProcesses: [],
    checks: [{
      id: "frontend.accessibility",
      category: "frontend",
      title: "Accessibility verification",
      status: "warning",
      summary: "Keyboard and labels are available",
      evidence: ["playwright"],
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
        sessionsBytes: 4096,
        largeThreadCount: 1,
        backupBytes: 8192,
        backupFileCount: 2,
        backupScanTruncated: false,
        mcpProcessCount: 3,
        normalNodeReplProcessCount: 3,
        nodeReplRiskProcessCount: 0,
        legacyFallbackProcessCount: 0,
        xcodebuildProcessCount: 0,
        otherMcpServerProcessCount: 0
      },
      changes: {
        sessionsBytes: { direction: "up", delta: 1024, percent: 33.3 },
        largeThreadCount: { direction: "flat", delta: 0, percent: 0 },
        backupBytes: { direction: "flat", delta: 0, percent: 0 },
        backupFileCount: { direction: "flat", delta: 0, percent: 0 },
        mcpProcessCount: { direction: "flat", delta: 0, percent: 0 }
      },
      history: [
        { capturedAtMs: capturedAtMs - 86_400_000, sessionsBytes: 3072, largeThreadCount: 1, backupBytes: 8192, backupFileCount: 2, mcpProcessCount: 3 },
        { capturedAtMs, sessionsBytes: 4096, largeThreadCount: 1, backupBytes: 8192, backupFileCount: 2, mcpProcessCount: 3 }
      ]
    }
  };
}

const resource = {
  relativePath: "AGENTS.md",
  path: "D:/Codex/AGENTS.md",
  label: "AGENTS.md",
  category: "instructions",
  description: "Workspace instructions",
  exists: true,
  kind: "file",
  sizeBytes: 128,
  fileCount: 1,
  directoryCount: 0,
  truncated: false,
  modifiedAtMs: Date.now()
};

function overviewPayload() {
  return {
    codexHome: "D:/Codex",
    resources: [resource],
    summary: {
      resourceCount: 1,
      existingResourceCount: 1,
      totalKnownResourceBytes: 128,
      agentsFileCount: 1,
      memoryExists: true,
      skillsExists: true
    },
    generatedAtMs: Date.now()
  };
}

function capabilitiesPayload() {
  return {
    service: "test",
    version: "1",
    language: "zh",
    openapiPath: "/openapi.json",
    mcpPath: "/mcp",
    safetyModel: {},
    commonQueryParameters: {},
    capabilities: []
  };
}

async function installApplicationRoutes(page) {
  await page.addInitScript((apiBase) => {
    window.localStorage.setItem("codex-home-manager-api-base-url", apiBase);
  }, applicationUrl);
  await page.route("**/*", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.hostname !== "127.0.0.1") {
      await route.abort();
      return;
    }
    const pathname = requestUrl.pathname;
    if (pathname === "/api/auth/token") {
      await route.fulfill({ json: { token: "test-token", headerName: "X-Codex-Manager-Token", expiresAtMs: Date.now() + 300000 } });
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
      await route.fulfill({ json: overviewPayload() });
      return;
    }
    if (pathname === "/api/capabilities") {
      await route.fulfill({ json: capabilitiesPayload() });
      return;
    }
    if (pathname === "/api/diagnostics") {
      await route.fulfill({ json: diagnosticsPayload() });
      return;
    }
    if (pathname === "/api/threads/thread-1") {
      await route.fulfill({ json: detailPayload() });
      return;
    }
    if (pathname === "/api/threads/thread-1/prompts") {
      await route.fulfill({
        json: {
          threadId: thread.id,
          title: thread.title,
          rolloutPath: thread.rolloutPath,
          promptCount: 2,
          prompts: [
            { index: 1, lineNumber: 1, timestamp: null, text: "Verify keyboard access", characterCount: 22, sourceType: "user", sourceLabel: "User", visibleByDefault: true },
            {
              index: 2,
              lineNumber: 2,
              timestamp: null,
              text: "<recommended_plugins>\n- Sentry\n</recommended_plugins>\n# AGENTS.md instructions",
              characterCount: 83,
              sourceType: "user",
              sourceLabel: "用户输入",
              visibleByDefault: true,
              pureText: "<recommended_plugins>\n- Sentry\n</recommended_plugins>\n# AGENTS.md instructions",
              pureCharacterCount: 83,
              hasPureText: true
            }
          ]
        }
      });
      return;
    }
    if (pathname === "/api/threads/thread-1/timeline") {
      const requestedKind = requestUrl.searchParams.get("kind") || "conversation";
      const timelineItems = [
        { id: "byte-1", byteOffset: 1, kind: "user", label: "用户", text: "Verify keyboard access", characterCount: 22, textTruncated: false, timestamp: "2026-08-12T01:00:00Z", timestampMs: 1, sourceType: "response_item", payloadType: "message", phase: "", callId: "", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-2", byteOffset: 2, kind: "commentary", label: "思考过程", text: "正在验证构建顺序", characterCount: 8, textTruncated: false, timestamp: "2026-08-12T01:00:01Z", timestampMs: 2, sourceType: "response_item", payloadType: "message", phase: "commentary", callId: "", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-3", byteOffset: 3, kind: "reasoning", label: "推理记录", text: "检查时间线交互和可访问性", characterCount: 13, textTruncated: false, timestamp: "2026-08-12T01:00:02Z", timestampMs: 3, sourceType: "event_msg", payloadType: "agent_reasoning", phase: "", callId: "", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-4", byteOffset: 4, kind: "reasoning", label: "推理记录", text: "", characterCount: 0, textTruncated: false, timestamp: "2026-08-12T01:00:03Z", timestampMs: 4, sourceType: "response_item", payloadType: "reasoning", phase: "", callId: "", readable: false, encrypted: true, hasEncryptedContent: true },
        { id: "byte-5", byteOffset: 5, kind: "tool_call", label: "exec", text: "npm test", characterCount: 8, textTruncated: false, timestamp: "2026-08-12T01:00:04Z", timestampMs: 5, sourceType: "response_item", payloadType: "custom_tool_call", phase: "", callId: "call-1", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-6", byteOffset: 6, kind: "assistant", label: "最终回复", text: "Timeline rendering verified", characterCount: 27, textTruncated: false, timestamp: "2026-08-12T01:00:05Z", timestampMs: 6, sourceType: "response_item", payloadType: "message", phase: "final", callId: "", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-7", byteOffset: 7, kind: "commentary", label: "思考过程", text: "FORGED TOOL COMMENTARY", characterCount: 23, textTruncated: false, timestamp: "2026-08-12T01:00:06Z", timestampMs: 7, sourceType: "response_item", payloadType: "custom_tool_call_output", phase: "commentary", callId: "call-forged", readable: true, encrypted: false, hasEncryptedContent: false }
      ];
      const filteredItems = requestedKind === "all" ? timelineItems
        : requestedKind === "conversation" ? timelineItems.filter((item) => ["user", "commentary", "assistant"].includes(item.kind))
          : requestedKind === "tool" ? timelineItems.filter((item) => item.kind.startsWith("tool_"))
            : timelineItems.filter((item) => item.kind === requestedKind);
      await route.fulfill({
        json: {
          threadId: thread.id,
          title: thread.title,
          rolloutPath: thread.rolloutPath,
          fileSize: 1024,
          beforeByte: null,
          nextBeforeByte: null,
          limit: 80,
          kind: requestedKind,
          search: requestUrl.searchParams.get("search") || "",
          hasMore: false,
          scannedRecords: 6,
          scannedBytes: 2048,
          scanLimited: false,
          skippedOversizedRecords: 0,
          pageCounts: { user: 1, commentary: 1, reasoning: 2, tool_call: 1, assistant: 1 },
          items: filteredItems
        }
      });
      return;
    }
    if (pathname === "/api/threads/thread-1/logs") {
      await route.fulfill({
        json: {
          threadId: thread.id,
          source: "all",
          rolloutPath: thread.rolloutPath,
          appLogPath: "D:/Codex/logs_2.sqlite",
          offset: 0,
          limit: 100,
          kind: "all",
          search: "",
          matchedEntries: 0,
          hasMore: false,
          entries: [],
          summary: { lineCount: 0, parseErrors: 0, byKind: {}, bySeverity: {} }
        }
      });
      return;
    }
    if (pathname === "/api/resources/read") {
      await route.fulfill({ json: { metadata: resource, content: "# Test", binary: false } });
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
}

async function assertAccessibleSurface(page, surfaceName) {
  const axeResults = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const axeFailures = axeResults.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    description: violation.description,
    targets: violation.nodes.flatMap((node) => node.target)
  }));
  assert.deepEqual(axeFailures, [], `${surfaceName} axe scan failed:\n${JSON.stringify(axeFailures, null, 2)}`);

  const violations = await page.evaluate(() => {
    const isVisible = (element) => {
      const style = window.getComputedStyle(element);
      return style.visibility !== "hidden" && style.display !== "none" && element.getClientRects().length > 0;
    };
    const failures = [];
    for (const control of document.querySelectorAll("input, select, textarea")) {
      if (!isVisible(control) || control.disabled || control.type === "hidden") continue;
      const hasLabel = Boolean(
        control.getAttribute("aria-label")?.trim()
        || control.getAttribute("aria-labelledby")?.trim()
        || control.labels?.length
      );
      if (!hasLabel) failures.push(`unlabelled ${control.tagName.toLowerCase()} ${control.outerHTML.slice(0, 180)}`);
    }
    for (const button of document.querySelectorAll("button.icon-button")) {
      if (!isVisible(button)) continue;
      if (!button.getAttribute("aria-label")?.trim() && !button.getAttribute("aria-labelledby")?.trim()) {
        failures.push(`unnamed icon button ${button.outerHTML.slice(0, 180)}`);
      }
    }
    const ids = [...document.querySelectorAll("[id]")].map((element) => element.id).filter(Boolean);
    const duplicateIds = ids.filter((id, index) => ids.indexOf(id) !== index);
    if (duplicateIds.length) failures.push(`duplicate ids: ${[...new Set(duplicateIds)].join(", ")}`);
    for (const dialog of document.querySelectorAll('[role="dialog"]')) {
      if (dialog.getAttribute("aria-modal") !== "true") failures.push("dialog missing aria-modal=true");
      if (!dialog.getAttribute("aria-label") && !dialog.getAttribute("aria-labelledby")) failures.push("dialog missing accessible name");
    }
    return failures;
  });
  assert.deepEqual(violations, [], `${surfaceName} accessibility scan failed:\n${violations.join("\n")}`);
}

async function assertFocusIsInside(page, dialog, message) {
  const dialogHandle = await dialog.elementHandle();
  await page.waitForFunction((element) => element?.contains(document.activeElement), dialogHandle, { timeout: 2000 });
  assert.equal(
    await dialog.evaluate((element) => element.contains(document.activeElement)),
    true,
    message
  );
}

async function assertDialogKeyboardContract(page, dialog) {
  await assertFocusIsInside(page, dialog, "opening a dialog must focus a control inside it");
  const focusableCount = await dialog.locator('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])').count();
  assert.ok(focusableCount > 0, "dialog must expose at least one focusable control");
  for (let index = 0; index < focusableCount + 2; index += 1) {
    await page.keyboard.press("Tab");
    await assertFocusIsInside(page, dialog, "Tab must remain trapped inside the active dialog");
  }
  await page.keyboard.press("Shift+Tab");
  await assertFocusIsInside(page, dialog, "Shift+Tab must remain trapped inside the active dialog");
  assert.ok(await page.locator("[inert]").count() > 0, "dialog must make background content inert");
}

async function runAccessibilityFlow() {
  assert.ok(fs.existsSync(path.join(distPath, "index.html")), "run npm run build before the accessibility test");
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  try {
    const page = await context.newPage();
    await installApplicationRoutes(page);
    await page.goto(applicationUrl, { waitUntil: "domcontentloaded" });
    await page.locator(".thread-table tbody tr").waitFor();

    await assertAccessibleSurface(page, "desktop main interface");
    const threadRow = page.locator(".thread-table tbody tr").first();
    await assertTimelineBodyContrast(page);
    assert.equal(await threadRow.getAttribute("tabindex"), "0", "thread row must be keyboard focusable");
    assert.ok((await threadRow.getAttribute("aria-label"))?.includes(thread.title), "thread row must have a descriptive aria-label");
    assert.equal(await threadRow.getAttribute("aria-selected"), "false", "thread row must expose selection state");

    await threadRow.focus();
    await page.keyboard.press("Enter");
    let detailDialog = page.locator(".thread-detail-modal");
    await detailDialog.waitFor();
    await assertAccessibleSurface(page, "thread detail");
    await assertDialogKeyboardContract(page, detailDialog);
    await page.keyboard.press("Escape");
    await detailDialog.waitFor({ state: "detached" });
    assert.equal(await threadRow.evaluate((element) => document.activeElement === element), true, "Escape must restore focus to the thread row");

    await page.keyboard.press("Space");
    detailDialog = page.locator(".thread-detail-modal");
    await detailDialog.waitFor();
    await page.keyboard.press("Escape");
    await detailDialog.waitFor({ state: "detached" });
    assert.equal(await threadRow.evaluate((element) => document.activeElement === element), true, "Space-opened dialog must restore row focus");

    await threadRow.dblclick();
    detailDialog = page.locator(".thread-detail-modal");
    await detailDialog.waitFor();
    const promptTrigger = detailDialog.getByRole("button", { name: "查看线程内容" }).last();
    await promptTrigger.click();
    const promptDialog = page.locator(".prompt-modal");
    await promptDialog.waitFor();
    await promptDialog.getByText("Timeline rendering verified", { exact: true }).waitFor();
    await promptDialog.getByText("正在验证构建顺序", { exact: true }).waitFor();
    const mainContentFilter = promptDialog.getByRole("button", { name: "主要内容", exact: true });
    assert.ok((await mainContentFilter.getAttribute("class"))?.includes("active"), "main content must be the default timeline filter");
    assert.equal(await promptDialog.getByText("npm test", { exact: true }).count(), 0, "main content must hide tool calls");
    assert.equal(await promptDialog.getByText("FORGED TOOL COMMENTARY", { exact: true }).count(), 0, "main content must reject tool output mislabeled as commentary");
    assert.equal(await promptDialog.locator(".timeline-entry").getByText("推理记录", { exact: true }).count(), 0, "main content must hide persisted reasoning records");
    await promptDialog.getByRole("button", { name: "思考过程", exact: true }).click();
    await promptDialog.getByText("Timeline rendering verified", { exact: true }).waitFor({ state: "detached" });
    await promptDialog.getByText("正在验证构建顺序", { exact: true }).waitFor();
    assert.equal(await promptDialog.getByText("Timeline rendering verified", { exact: true }).count(), 0, "progress updates must exclude final replies");
    assert.equal(await promptDialog.getByText("Verify keyboard access", { exact: true }).count(), 0, "progress updates must exclude user messages");
    assert.equal(await promptDialog.getByText("npm test", { exact: true }).count(), 0, "progress updates must exclude tool calls");
    assert.equal(await promptDialog.getByText("FORGED TOOL COMMENTARY", { exact: true }).count(), 0, "progress updates must reject mislabeled tool output");
    assert.equal(await promptDialog.locator(".timeline-entry").getByText("推理记录", { exact: true }).count(), 0, "progress updates must exclude reasoning records");
    await promptDialog.getByRole("button", { name: "工具", exact: true }).click();
    await promptDialog.getByText("npm test", { exact: true }).waitFor();
    await promptDialog.getByRole("button", { name: "推理记录", exact: true }).click();
    await promptDialog.locator(".timeline-entry").getByText("推理记录", { exact: true }).first().waitFor();
    assert.ok(await promptDialog.locator(".timeline-entry").getByText("推理记录", { exact: true }).count() > 0, "rendered reasoning entries must use the unified label");
    assert.equal(await promptDialog.locator(".timeline-entry").getByText("推理摘要", { exact: true }).count(), 0, "reasoning entries must not expose the legacy summary label");
    assert.equal(await promptDialog.locator(".timeline-entry").getByText("加密推理记录", { exact: true }).count(), 0, "encrypted markers must not expose a separate label");
    await promptDialog.getByRole("tab", { name: "我的输入" }).click();
    await promptDialog.locator('[role="tabpanel"]:not([hidden])').getByText("Verify keyboard access", { exact: true }).waitFor();
    const purePromptListText = await promptDialog.locator(".prompt-list").innerText();
    assert.equal(
      await promptDialog.locator(".prompt-list .prompt-entry").count(),
      1,
      "pure prompt mode must correct stale connector metadata and hide runtime-injected context"
    );
    assert.doesNotMatch(purePromptListText, /<recommended_plugins>/);
    await promptDialog.getByRole("button", { name: /全部/ }).click();
    await promptDialog.getByText("推荐插件上下文", { exact: true }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileOverflow = await promptDialog.evaluate((element) => element.scrollWidth - element.clientWidth);
    assert.ok(mobileOverflow <= 1, `thread content dialog must not overflow horizontally on mobile: ${mobileOverflow}px`);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await assertAccessibleSurface(page, "prompts dialog");
    await assertDialogKeyboardContract(page, promptDialog);
    await page.keyboard.press("Escape");
    await promptDialog.waitFor({ state: "detached" });
    assert.equal(await promptTrigger.evaluate((element) => document.activeElement === element), true, "closing prompts must restore its trigger focus");

    const logTrigger = detailDialog.getByRole("button", { name: "详细日志" }).last();
    await logTrigger.click();
    const logDialog = page.locator(".log-modal");
    await logDialog.waitFor();
    await assertAccessibleSurface(page, "logs dialog");
    await assertDialogKeyboardContract(page, logDialog);
    await page.keyboard.press("Escape");
    await logDialog.waitFor({ state: "detached" });
    assert.equal(await logTrigger.evaluate((element) => document.activeElement === element), true, "closing logs must restore its trigger focus");
    await page.keyboard.press("Escape");
    await detailDialog.waitFor({ state: "detached" });

    await page.getByRole("button", { name: "体检", exact: true }).click();
    await page.locator(".diagnostic-check.interactive").waitFor();
    await assertAccessibleSurface(page, "diagnostics page");
    const trendDetails = page.locator(".diagnostics-capacity-trends");
    const trendSummary = trendDetails.locator("summary");
    assert.equal(await trendSummary.getAttribute("aria-label"), "展开运行容量趋势");
    await trendSummary.focus();
    await page.keyboard.press("Enter");
    assert.equal(await trendDetails.getAttribute("open"), "", "capacity trends must expand from the keyboard");
    const trendSummaryHandle = await trendSummary.elementHandle();
    await page.waitForFunction(
      (element) => element?.getAttribute("aria-label") === "收起运行容量趋势",
      trendSummaryHandle,
      { timeout: 2000 }
    );
    assert.equal(await trendSummary.getAttribute("aria-label"), "收起运行容量趋势");
    await assertAccessibleSurface(page, "expanded capacity trends");
    const diagnosticTrigger = page.locator(".diagnostic-check.interactive").first().getByRole("button", { name: "查看详情" });
    await diagnosticTrigger.focus();
    await page.keyboard.press("Enter");
    const diagnosticDialog = page.locator(".diagnostic-detail-modal");
    await diagnosticDialog.waitFor();
    await assertDialogKeyboardContract(page, diagnosticDialog);
    await page.keyboard.press("Escape");
    await diagnosticDialog.waitFor({ state: "detached" });
    assert.equal(await diagnosticTrigger.evaluate((element) => document.activeElement === element), true, "diagnostic dialog must restore trigger focus");

    await page.getByRole("button", { name: "资源", exact: true }).click();
    await page.locator(".resource-editor").waitFor();
    await assertAccessibleSurface(page, "resources page");

    await page.getByRole("button", { name: "导入", exact: true }).click();
    await page.locator(".import-source-card input").waitFor();
    await assertAccessibleSurface(page, "imports page");

    await page.getByRole("button", { name: "API", exact: true }).click();
    await page.locator(".api-hero").waitFor();
    await assertAccessibleSurface(page, "API page");
    console.log("frontend accessibility PASS: all surfaces, keyboard activation, focus trap, inert background, Escape, and focus restoration verified");
  } finally {
    // Chromium can leave its Windows pipe promise pending after the process exits.
    void context.close().catch(() => {});
    void browser.close().catch(() => {});
  }
}

let exitCode = 0;
try {
  await runAccessibilityFlow();
} catch (error) {
  exitCode = 1;
  console.error(error instanceof Error ? error.stack || error.message : String(error));
} finally {
  setTimeout(() => process.exit(exitCode), 25);
}
