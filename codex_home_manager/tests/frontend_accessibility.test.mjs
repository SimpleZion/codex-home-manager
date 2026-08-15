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

const raceThread = {
  ...thread,
  id: "thread-2",
  title: "Fast B prompt thread",
  sqliteTitle: "Fast B prompt thread",
  sidebarTitle: "Fast B prompt thread",
  sessionIndexTitle: "Fast B prompt thread",
  preview: "Fast prompt response",
  rolloutPath: "D:/Codex/sessions/thread-2.jsonl",
  updatedAtMs: thread.updatedAtMs - 1000,
  fileModifiedAtMs: thread.fileModifiedAtMs - 1000,
  recentRank: 2,
  threadListRank: 2,
  sessionIndexRank: 2
};

const scrollingThreads = Array.from({ length: 120 }, (_, offset) => ({
  ...thread,
  id: `scroll-thread-${String(offset + 1).padStart(3, "0")}`,
  title: `Scrollable thread ${String(offset + 1).padStart(3, "0")}`,
  sqliteTitle: `Scrollable thread ${String(offset + 1).padStart(3, "0")}`,
  sidebarTitle: `Scrollable thread ${String(offset + 1).padStart(3, "0")}`,
  sessionIndexTitle: `Scrollable thread ${String(offset + 1).padStart(3, "0")}`,
  rolloutPath: `D:/Codex/sessions/scroll-thread-${String(offset + 1).padStart(3, "0")}.jsonl`,
  recentRank: offset + 1,
  threadListRank: offset + 1,
  sessionIndexRank: offset + 1,
  updatedAtMs: thread.updatedAtMs - offset * 1000,
  fileModifiedAtMs: thread.fileModifiedAtMs - offset * 1000
}));

const promptRecords = [
  { index: 1, lineNumber: 1, timestamp: null, text: "Verify keyboard access", characterCount: 22, sourceType: "user", sourceLabel: "User", visibleByDefault: true, pureText: "Verify keyboard access", pureCharacterCount: 22, hasPureText: true },
  { index: 2, lineNumber: 2, timestamp: null, text: "<recommended_plugins>\n- Sentry\n</recommended_plugins>\n# AGENTS.md instructions", characterCount: 83, sourceType: "internal", sourceLabel: "推荐插件上下文", visibleByDefault: false, pureText: "", pureCharacterCount: 0, hasPureText: false },
  ...Array.from({ length: 120 }, (_, offset) => {
    const index = offset + 3;
    const pagedMatchNumber = offset === 29 ? 1 : offset === 74 ? 2 : offset === 109 ? 3 : 0;
    const text = offset === 119
      ? "<codex_internal_context>顶刊能力建设的尾部搜索目标</codex_internal_context>"
      : pagedMatchNumber
        ? `<codex_internal_context>paged-search match ${pagedMatchNumber}</codex_internal_context>`
        : `<codex_internal_context>运行时内部记录 ${offset + 1}</codex_internal_context>`;
    return { index, lineNumber: index, timestamp: null, text, characterCount: text.length, sourceType: "internal", sourceLabel: "内部上下文", visibleByDefault: false, pureText: "", pureCharacterCount: 0, hasPureText: false };
  }),
  { index: 123, lineNumber: 123, timestamp: null, text: "Turkish capital: İZMİR", characterCount: 22, sourceType: "internal", sourceLabel: "内部上下文", visibleByDefault: false, pureText: "", pureCharacterCount: 0, hasPureText: false },
  { index: 124, lineNumber: 124, timestamp: null, text: "Combining accent: Cafe\u0301", characterCount: 23, sourceType: "internal", sourceLabel: "内部上下文", visibleByDefault: false, pureText: "", pureCharacterCount: 0, hasPureText: false },
  { index: 125, lineNumber: 125, timestamp: null, text: "Emoji sequence: 👩‍💻🚀", characterCount: 24, sourceType: "internal", sourceLabel: "内部上下文", visibleByDefault: false, pureText: "", pureCharacterCount: 0, hasPureText: false }
];

function promptScopeMatches(prompt, scope) {
  if (scope === "pure") return prompt.hasPureText;
  if (scope === "visible") return prompt.visibleByDefault !== false;
  if (scope === "with_agents") return prompt.visibleByDefault !== false || prompt.sourceType === "subagent";
  if (scope === "automation" || scope === "delegation") return prompt.sourceType === scope;
  return true;
}

function snapshotPayload(threads = [thread]) {
  return {
    codexHome: "D:/Codex",
    databasePath: "D:/Codex/state_5.sqlite",
    globalStatePath: "D:/Codex/.codex-global-state.json",
    sessionIndexPath: "D:/Codex/session_index.jsonl",
    sidebarLimit: 50,
    generatedAtMs: Date.now(),
    summary: {
      totalThreads: threads.length,
      mainThreads: threads.length,
      subagentThreads: 0,
      eligibleThreads: threads.length,
      codexVisibleThreads: threads.length,
      hiddenByInitialLimit: 0,
      archivedThreads: 0,
      needsRepairThreads: 0,
      savedProjects: 1,
      workspaceProjects: 1,
      conversationProjects: 0,
      otherProjects: 0,
      emptyProjectsWithHiddenThreads: 0,
      totalStorageBytes: 4096 * threads.length
    },
    threads,
    projects: [{
      path: thread.projectPath,
      label: thread.projectLabel,
      projectKind: "workspace_project",
      total: threads.length,
      mainThreads: threads.length,
      subagentThreads: 0,
      active: threads.length,
      visible: threads.length,
      hiddenByInitialLimit: 0,
      archived: 0,
      needsRepair: 0,
      storageBytes: 4096 * threads.length,
      emptyButHasHiddenThreads: false
    }]
  };
}

function detailPayload(threadRecord = thread) {
  return {
    thread: threadRecord,
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

function createPromptIndexApiState() {
  return {
    statusRequests: [],
    previewRequests: [],
    clearRequests: [],
    databaseExists: true,
    rebuildPending: false,
    rebuildPageRequests: 0,
    clearConflict: false
  };
}

function promptIndexStatusPayload(state) {
  return {
    databaseExists: state.databaseExists,
    database: state.databaseExists ? {
      sizeBytes: 524_288,
      inUse: false,
      activeOperations: 0,
      readable: true,
      inspectionState: "available",
      lastAccessedAtMs: Date.now() - 5_000,
      schemaVersion: 3,
      sourceRolloutCount: 1,
      missingSourceRolloutCount: 0,
      promptCount: 125
    } : null,
    storage: {
      rootPath: "C:\\Users\\Test\\AppData\\Local\\CodexHomeManager\\prompt-index",
      databaseCount: state.databaseExists ? 3 : 2,
      activeDatabaseCount: 0,
      totalSizeBytes: state.databaseExists ? 1_572_864 : 1_048_576,
      maxTotalBytes: 1_073_741_824,
      maxIdleSeconds: 2_592_000,
      overCapacity: false
    }
  };
}

async function installApplicationRoutes(page, {
  includeRaceThread = false,
  emulateUncancellableSlowPrompt = false,
  simulateColdIndex = false,
  promptApiState = { pageRequests: [], cancelRequests: [], copyRequests: [] },
  promptIndexApiState = createPromptIndexApiState(),
  threadCatalog = null,
  snapshotApiState = { catalogVersion: "test-catalog", summaryRequests: 0, pageRequests: [] },
  timelineUniformTimestamps = false
} = {}) {
  await page.addInitScript((apiBase) => {
    window.localStorage.setItem("codex-home-manager-api-base-url", apiBase);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (text) => { window.__copiedPromptText = text; } }
    });
  }, applicationUrl);
  if (emulateUncancellableSlowPrompt) {
    await page.addInitScript(({ slowThreadId, slowThreadTitle }) => {
      const nativeFetch = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const requestUrl = new URL(typeof input === "string" ? input : input.url, window.location.href);
        if (requestUrl.pathname === `/api/threads/${slowThreadId}/prompts/page`) {
          const requestId = requestUrl.searchParams.get("requestId");
          const scope = requestUrl.searchParams.get("scope") || "visible";
          const search = requestUrl.searchParams.get("search") || "";
          const order = requestUrl.searchParams.get("order") || "asc";
          window.__slowPromptRequestStarted = true;
          init?.signal?.addEventListener("abort", () => {
            window.__slowPromptAbortObserved = true;
          }, { once: true });
          return new Promise((resolve) => window.setTimeout(() => {
            window.__slowPromptResponseReturned = true;
            resolve(new Response(JSON.stringify({
              threadId: slowThreadId,
              title: slowThreadTitle,
              rolloutPath: `D:/Codex/sessions/${slowThreadId}.jsonl`,
              requestId,
              scope,
              search,
              order,
              sourceType: null,
              promptCount: 1,
              purePromptCount: 1,
              visiblePromptCount: 1,
              hiddenPromptCount: 0,
              sourceCounts: { user: 1 },
              matchCount: 1,
              matchCountComplete: true,
              nextCursor: null,
              hasMore: false,
              index: { complete: true, scannedBytes: 100, fileSize: 100 },
              prompts: [{
                index: 1,
                lineNumber: 1,
                timestamp: null,
                text: "SLOW A STALE PROMPT",
                characterCount: 19,
                sourceType: "user",
                sourceLabel: "User",
                visibleByDefault: true,
                pureText: "SLOW A STALE PROMPT",
                hasPureText: true
              }]
            }), { status: 200, headers: { "Content-Type": "application/json" } }));
          }, 500));
        }
        return nativeFetch(input, init);
      };
    }, { slowThreadId: thread.id, slowThreadTitle: thread.title });
  }
  await page.route("**/*", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.hostname !== "127.0.0.1") {
      await route.abort();
      return;
    }
    const pathname = requestUrl.pathname;
    const activeThreads = threadCatalog || (includeRaceThread ? [thread, raceThread] : [thread]);
    if (pathname === "/api/auth/token") {
      await route.fulfill({ json: { token: "test-token", headerName: "X-Codex-Manager-Token", expiresAtMs: Date.now() + 300000 } });
      return;
    }
    if (pathname === "/api/health") {
      await route.fulfill({ json: { writeWarnings: [], currentVersions: {} } });
      return;
    }
    if (pathname === "/api/snapshot/summary") {
      snapshotApiState.summaryRequests += 1;
      const payload = snapshotPayload(activeThreads);
      const { threads: _threads, ...summary } = payload;
      await route.fulfill({ json: { ...summary, catalogVersion: snapshotApiState.catalogVersion, statusCounts: { visible: payload.threads.length }, threadHighlights: payload.threads.slice(0, 20) } });
      return;
    }
    if (pathname === "/api/threads/page") {
      const limit = Math.max(1, Number(requestUrl.searchParams.get("limit") || 50));
      const start = Number(requestUrl.searchParams.get("cursor") || 0);
      const threads = activeThreads.slice(start, start + limit);
      const nextOffset = start + threads.length;
      snapshotApiState.pageRequests.push({ catalogVersion: snapshotApiState.catalogVersion, start, limit });
      await route.fulfill({
        json: {
          codexHome: "D:/Codex",
          catalogVersion: snapshotApiState.catalogVersion,
          generatedAtMs: Date.now(),
          matchedThreads: activeThreads.length,
          loadedThreads: threads.length,
          threads,
          projectCounts: { [thread.projectPath]: activeThreads.length },
          hasMore: nextOffset < activeThreads.length,
          nextCursor: nextOffset < activeThreads.length ? String(nextOffset) : null
        }
      });
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
    if (pathname === "/api/prompt-index/status") {
      promptIndexApiState.statusRequests.push({ codexHome: requestUrl.searchParams.get("codex_home") });
      await route.fulfill({ json: promptIndexStatusPayload(promptIndexApiState) });
      return;
    }
    if (pathname === "/api/prompt-index/clear/preview") {
      promptIndexApiState.previewRequests.push({ method: route.request().method(), codexHome: requestUrl.searchParams.get("codex_home") });
      await route.fulfill({
        json: {
          operationPreviewId: "prompt-index-preview-1",
          inputHash: "prompt-index-input-hash-1",
          expiresAtMs: Date.now() + 60_000,
          stateDigest: "prompt-index-state-1",
          willClear: promptIndexApiState.databaseExists,
          reclaimableBytes: promptIndexApiState.databaseExists ? 524_288 : 0,
          inUse: false,
          warning: "Source rollouts are unchanged and can rebuild it."
        }
      });
      return;
    }
    if (pathname === "/api/prompt-index/clear") {
      promptIndexApiState.clearRequests.push({
        method: route.request().method(),
        codexHome: requestUrl.searchParams.get("codex_home"),
        token: route.request().headers()["x-codex-manager-token"],
        body: route.request().postDataJSON()
      });
      if (promptIndexApiState.clearConflict) {
        await route.fulfill({ status: 409, json: { detail: "prompt index is currently in use" } });
        return;
      }
      const databaseExisted = promptIndexApiState.databaseExists;
      promptIndexApiState.databaseExists = false;
      promptIndexApiState.rebuildPending = databaseExisted;
      await route.fulfill({
        json: {
          cleared: databaseExisted,
          databaseExisted,
          deletedFileCount: databaseExisted ? 3 : 0,
          reclaimedBytes: databaseExisted ? 524_288 : 0
        }
      });
      return;
    }
    if (pathname === "/api/threads/thread-1") {
      await route.fulfill({ json: detailPayload() });
      return;
    }
    if (pathname === "/api/threads/thread-2") {
      await route.fulfill({ json: detailPayload(raceThread) });
      return;
    }
    const cancelMatch = pathname.match(/^\/api\/threads\/([^/]+)\/prompts\/requests\/([^/]+)$/);
    if (cancelMatch && route.request().method() === "DELETE") {
      promptApiState.cancelRequests.push({ threadId: cancelMatch[1], requestId: decodeURIComponent(cancelMatch[2]) });
      await route.fulfill({ json: { threadId: cancelMatch[1], requestId: decodeURIComponent(cancelMatch[2]), cancelled: true } });
      return;
    }
    const promptPageMatch = pathname.match(/^\/api\/threads\/(thread-[12])\/prompts\/page$/);
    if (promptPageMatch) {
      const threadId = promptPageMatch[1];
      const requestId = requestUrl.searchParams.get("requestId") || "missing-request-id";
      const scope = requestUrl.searchParams.get("scope") || "visible";
      const search = requestUrl.searchParams.get("search") || "";
      const order = requestUrl.searchParams.get("order") || "asc";
      const cursor = requestUrl.searchParams.get("cursor");
      const scanBudgetMs = Number(requestUrl.searchParams.get("scanBudgetMs") || 0);
      if (promptIndexApiState.rebuildPending && !cursor) {
        promptIndexApiState.rebuildPending = false;
        promptIndexApiState.databaseExists = true;
        promptIndexApiState.rebuildPageRequests += 1;
      }
      promptApiState.queryCalls ||= new Map();
      const queryKey = `${threadId}|${scope}|${search}|${order}`;
      const queryCallCount = cursor ? (promptApiState.queryCalls.get(queryKey) || 1) : (promptApiState.queryCalls.get(queryKey) || 0) + 1;
      if (!cursor) promptApiState.queryCalls.set(queryKey, queryCallCount);
      promptApiState.pageRequests.push({ threadId, requestId, scope, search, order, cursor, scanBudgetMs, queryCallCount });
      // Keep the follow-up scan pending long enough for the UI test to exercise
      // request cancellation deterministically on slower and faster runners.
      if (simulateColdIndex && !cursor && queryCallCount > 1) await new Promise((resolve) => setTimeout(resolve, 2000));

      const sourcePrompts = threadId === raceThread.id
        ? [{ index: 1, lineNumber: 1, timestamp: null, text: "FAST B CURRENT PROMPT", characterCount: 21, sourceType: "user", sourceLabel: "User", visibleByDefault: true, pureText: "FAST B CURRENT PROMPT", pureCharacterCount: 21, hasPureText: true }]
        : promptRecords;
      const normalizedSearch = search.normalize("NFD").toLocaleLowerCase("und").replace(/\p{M}/gu, "");
      const matchingPrompts = sourcePrompts.filter((prompt) => {
        if (!promptScopeMatches(prompt, scope)) return false;
        if (!normalizedSearch) return true;
        const text = `${prompt.text}\n${prompt.pureText || ""}`.normalize("NFD").toLocaleLowerCase("und").replace(/\p{M}/gu, "");
        return text.includes(normalizedSearch);
      });
      if (order === "desc") matchingPrompts.reverse();
      const pageSize = search === "paged-search" ? 2 : Math.min(60, Number(requestUrl.searchParams.get("limit") || 60));
      const start = cursor ? Number(cursor.replace("cursor-", "")) : 0;
      const pagePrompts = matchingPrompts.slice(start, start + pageSize);
      const end = start + pagePrompts.length;
      const matchCountComplete = !simulateColdIndex || Boolean(cursor) || queryCallCount > 1;
      const hasMore = end < matchingPrompts.length || !matchCountComplete;
      const nextCursor = hasMore ? `cursor-${end}` : null;
      const sourceCounts = sourcePrompts.reduce((counts, prompt) => {
        const key = prompt.sourceType || "unknown";
        counts[key] = (counts[key] || 0) + 1;
        return counts;
      }, {});
      try {
        await route.fulfill({
          json: {
            threadId,
            title: threadId === raceThread.id ? raceThread.title : thread.title,
            rolloutPath: threadId === raceThread.id ? raceThread.rolloutPath : thread.rolloutPath,
            requestId,
            scope,
            search,
            order,
            sourceType: null,
            promptCount: sourcePrompts.length,
            purePromptCount: sourcePrompts.filter((prompt) => prompt.hasPureText).length,
            visiblePromptCount: sourcePrompts.filter((prompt) => prompt.visibleByDefault !== false).length,
            hiddenPromptCount: sourcePrompts.filter((prompt) => prompt.visibleByDefault === false).length,
            sourceCounts,
            matchCount: matchingPrompts.length,
            matchCountComplete,
            prompts: pagePrompts,
            nextCursor,
            hasMore,
            index: { complete: matchCountComplete, scannedBytes: matchCountComplete ? 1000 : 100, fileSize: 1000, elapsedMs: scanBudgetMs }
          }
        });
      } catch {
        // An aborted Playwright route can finish after the frontend has already issued DELETE.
      }
      return;
    }
    const promptCopyMatch = pathname.match(/^\/api\/threads\/(thread-[12])\/prompts\/copy$/);
    if (promptCopyMatch) {
      const threadId = promptCopyMatch[1];
      const requestId = requestUrl.searchParams.get("requestId") || "missing-copy-request-id";
      const scope = requestUrl.searchParams.get("scope") || "pure";
      const search = requestUrl.searchParams.get("search") || "";
      const format = requestUrl.searchParams.get("format") || "text";
      promptApiState.copyRequests.push({ threadId, requestId, scope, search, format });
      const normalizedSearch = search.toLocaleLowerCase();
      const matchingPrompts = promptRecords.filter((prompt) => promptScopeMatches(prompt, scope) && (!normalizedSearch || `${prompt.text}\n${prompt.pureText || ""}`.toLocaleLowerCase().includes(normalizedSearch)));
      const body = format === "jsonl"
        ? matchingPrompts.map((prompt) => JSON.stringify({ ...prompt, exportText: scope === "pure" ? prompt.pureText : prompt.text })).join("\n") + "\n"
        : matchingPrompts.map((prompt) => scope === "pure" ? prompt.pureText : prompt.text).join("\n\n");
      await route.fulfill({
        body,
        contentType: format === "jsonl" ? "application/x-ndjson; charset=utf-8" : "text/plain; charset=utf-8",
        headers: { "X-Prompt-Request-Id": requestId }
      });
      return;
    }
    if (pathname === "/api/threads/thread-1/timeline/search/page") {
      const search = requestUrl.searchParams.get("search") || "";
      const requestId = requestUrl.searchParams.get("requestId") || "missing-timeline-request-id";
      const timelineSearchResults = new Map([
        ["正在验证", "正在验证构建顺序"],
        ["office", "oﬃce compatibility ligature"],
        ["strasse", "Straße"],
        ["οσ", "ΟΣ"],
        ["cafe", "Cafe\u0301"],
        ["izmir", "İZMİR"]
      ]);
      const matchText = timelineSearchResults.get(search) || "";
      await new Promise((resolve) => setTimeout(resolve, search === "正在验证" || search === "missing-result" ? 800 : 30));
      const match = {
        id: "byte-2",
        byteOffset: 2,
        kind: "commentary",
        label: "思考过程",
        text: matchText,
        characterCount: matchText.length,
        textTruncated: false,
        timestamp: "2026-08-12T01:00:01Z",
        timestampMs: 2,
        sourceType: "response_item",
        payloadType: "message",
        phase: "commentary",
        callId: "",
        readable: true,
        encrypted: false,
        hasEncryptedContent: false
      };
      await route.fulfill({
        json: {
          threadId: thread.id,
          requestId,
          kind: requestUrl.searchParams.get("kind") || "conversation",
          search,
          matchCount: matchText ? 1 : 0,
          matchCountComplete: true,
          matches: matchText ? [match] : [],
          nextCursor: null,
          hasMore: false,
          index: { complete: true, scannedBytes: 1024, scannedLines: 7, fileSize: 1024, elapsedMs: 800 }
        }
      });
      return;
    }
    if (pathname.match(/^\/api\/threads\/thread-1\/timeline\/search\/requests\/[^/]+$/) && route.request().method() === "DELETE") {
      await route.fulfill({ json: { threadId: thread.id, requestId: pathname.split("/").at(-1), cancelled: true } });
      return;
    }
    if (pathname === "/api/threads/thread-1/timeline") {
      const requestedKind = requestUrl.searchParams.get("kind") || "conversation";
      let timelineItems = [
        { id: "byte-1", byteOffset: 1, kind: "user", label: "用户", text: "Verify keyboard access", characterCount: 22, textTruncated: false, timestamp: "2026-08-12T01:00:00Z", timestampMs: 1, sourceType: "response_item", payloadType: "message", phase: "", callId: "", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-2", byteOffset: 2, kind: "commentary", label: "思考过程", text: "正在验证构建顺序", characterCount: 8, textTruncated: false, timestamp: "2026-08-12T01:00:01Z", timestampMs: 2, sourceType: "response_item", payloadType: "message", phase: "commentary", callId: "", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-3", byteOffset: 3, kind: "reasoning", label: "推理记录", text: "检查时间线交互和可访问性", characterCount: 13, textTruncated: false, timestamp: "2026-08-12T01:00:02Z", timestampMs: 3, sourceType: "event_msg", payloadType: "agent_reasoning", phase: "", callId: "", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-4", byteOffset: 4, kind: "reasoning", label: "推理记录", text: "", characterCount: 0, textTruncated: false, timestamp: "2026-08-12T01:00:03Z", timestampMs: 4, sourceType: "response_item", payloadType: "reasoning", phase: "", callId: "", readable: false, encrypted: true, hasEncryptedContent: true },
        { id: "byte-5", byteOffset: 5, kind: "tool_call", label: "exec", text: "npm test", characterCount: 8, textTruncated: false, timestamp: "2026-08-12T01:00:04Z", timestampMs: 5, sourceType: "response_item", payloadType: "custom_tool_call", phase: "", callId: "call-1", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-6", byteOffset: 6, kind: "assistant", label: "最终回复", text: "Timeline rendering verified", characterCount: 27, textTruncated: false, timestamp: "2026-08-12T01:00:05Z", timestampMs: 6, sourceType: "response_item", payloadType: "message", phase: "final", callId: "", readable: true, encrypted: false, hasEncryptedContent: false },
        { id: "byte-7", byteOffset: 7, kind: "commentary", label: "思考过程", text: "FORGED TOOL COMMENTARY", characterCount: 23, textTruncated: false, timestamp: "2026-08-12T01:00:06Z", timestampMs: 7, sourceType: "response_item", payloadType: "custom_tool_call_output", phase: "commentary", callId: "call-forged", readable: true, encrypted: false, hasEncryptedContent: false }
      ];
      if (timelineUniformTimestamps) timelineItems = timelineItems.map((item) => ({ ...item, timestamp: "2026-08-12T01:00:00Z", timestampMs: 1 }));
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
    if (pathname === "/api/threads/thread-2/timeline") {
      await route.fulfill({
        json: {
          threadId: raceThread.id,
          title: raceThread.title,
          rolloutPath: raceThread.rolloutPath,
          fileSize: 1024,
          beforeByte: null,
          nextBeforeByte: null,
          limit: 80,
          kind: requestUrl.searchParams.get("kind") || "conversation",
          search: "",
          hasMore: false,
          scannedRecords: 0,
          scannedBytes: 0,
          scanLimited: false,
          skippedOversizedRecords: 0,
          pageCounts: {},
          items: []
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

async function assertPromptModalLayoutAndHitTargets(page, promptDialog, viewportLabel) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const layout = await promptDialog.evaluate((dialog) => {
    const promptView = dialog.querySelector(".prompt-view:not([hidden])");
    const search = promptView?.querySelector(".prompt-content-search");
    const content = promptView?.querySelector(".prompt-modal-content");
    const list = promptView?.querySelector(".prompt-list");
    if (!promptView || !search || !content || !list) return null;
    const rectangle = (element) => {
      const bounds = element.getBoundingClientRect();
      return { top: bounds.top, right: bounds.right, bottom: bounds.bottom, left: bounds.left };
    };
    const buttonHits = [...search.querySelectorAll("button")].map((button) => {
      const bounds = button.getBoundingClientRect();
      const centerX = bounds.left + bounds.width / 2;
      const centerY = bounds.top + bounds.height / 2;
      const hit = document.elementFromPoint(centerX, centerY);
      return {
        label: button.getAttribute("aria-label"),
        centerX,
        centerY,
        hit: Boolean(hit && button.contains(hit))
      };
    });
    return {
      gridRows: getComputedStyle(promptView).gridTemplateRows.trim().split(/\s+/).filter(Boolean),
      childClasses: [...promptView.children].map((child) => child.className),
      search: rectangle(search),
      content: rectangle(content),
      list: rectangle(list),
      buttonHits
    };
  });
  assert.ok(layout, `${viewportLabel}: prompt layout elements must render`);
  assert.equal(layout.gridRows.length, 4, `${viewportLabel}: prompt view must expose four explicit grid rows`);
  assert.deepEqual(layout.childClasses, ["prompt-modal-toolbar", "prompt-filter-bar", "prompt-content-search", "prompt-modal-content"], `${viewportLabel}: all four prompt rows must coexist in DOM order`);
  assert.ok(layout.search.bottom <= layout.content.top + 1, `${viewportLabel}: search row must end before content row starts`);
  assert.ok(layout.search.bottom <= layout.list.top + 1, `${viewportLabel}: search row must not overlap the virtual list`);
  assert.equal(layout.buttonHits.length, 6, `${viewportLabel}: prompt search must render order, jump, and match-navigation controls`);
  for (const target of layout.buttonHits) {
    assert.ok(target.centerX >= 0 && target.centerX <= page.viewportSize().width, `${viewportLabel}: ${target.label} center must remain in the viewport`);
    assert.ok(target.centerY >= 0 && target.centerY <= page.viewportSize().height, `${viewportLabel}: ${target.label} center must remain in the viewport`);
    assert.equal(target.hit, true, `${viewportLabel}: ${target.label} center hit-test must resolve to its button`);
  }
  return layout;
}

async function assertTimelineReadingLayout(page, promptDialog, viewportLabel) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const layout = await promptDialog.evaluate((dialog) => {
    const panel = dialog.querySelector(".thread-timeline-panel");
    const toolbar = dialog.querySelector(".timeline-toolbar");
    const meta = dialog.querySelector(".timeline-meta");
    const scroll = dialog.querySelector(".timeline-scroll");
    const commentaryBody = dialog.querySelector(".timeline-entry-commentary .timeline-readable-text");
    if (!panel || !toolbar || !meta || !scroll || !commentaryBody) return null;
    const bounds = (element) => {
      const rectangle = element.getBoundingClientRect();
      return { width: rectangle.width, height: rectangle.height, top: rectangle.top, bottom: rectangle.bottom };
    };
    const bodyStyle = getComputedStyle(commentaryBody);
    return {
      panel: bounds(panel),
      toolbar: bounds(toolbar),
      meta: bounds(meta),
      scroll: bounds(scroll),
      panelOverflow: panel.scrollWidth - panel.clientWidth,
      toolbarOverflow: toolbar.scrollWidth - toolbar.clientWidth,
      readableTag: commentaryBody.tagName,
      readableFont: bodyStyle.fontFamily,
      readableBackground: bodyStyle.backgroundColor
    };
  });
  assert.ok(layout, `${viewportLabel}: timeline reading surface must render`);
  assert.ok(layout.scroll.height >= layout.panel.height * 0.62, `${viewportLabel}: the timeline must retain at least 62% of the panel height for reading`);
  assert.ok(layout.toolbar.height <= 96, `${viewportLabel}: timeline controls must not dominate the vertical viewport`);
  assert.ok(layout.panelOverflow <= 1, `${viewportLabel}: timeline panel must not overflow horizontally`);
  assert.ok(layout.toolbarOverflow <= 1, `${viewportLabel}: timeline toolbar must not expose a horizontal scrollbar`);
  assert.equal(layout.readableTag, "DIV", `${viewportLabel}: natural-language progress must not render as a code block`);
  assert.notEqual(layout.readableFont, "monospace", `${viewportLabel}: natural-language progress must use the application reading font`);
  return layout;
}

async function visibleThreadAnchor(page) {
  return page.locator(".table-wrap").evaluate((container) => {
    const containerTop = container.getBoundingClientRect().top;
    const rows = [...container.querySelectorAll("tbody tr[data-thread-id]")];
    const row = rows.find((candidate) => candidate.getBoundingClientRect().bottom > containerTop + 1);
    return row ? {
      threadId: row.dataset.threadId,
      offset: row.getBoundingClientRect().top - containerTop,
      scrollTop: container.scrollTop
    } : null;
  });
}

async function waitForPromptApiState(page, predicate, message, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await page.waitForTimeout(20);
  }
  assert.fail(message);
}

async function runThreadScrollAnchorFlow() {
  const snapshotApiState = { catalogVersion: "scroll-catalog-1", summaryRequests: 0, pageRequests: [] };
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  try {
    const page = await context.newPage();
    await installApplicationRoutes(page, { threadCatalog: scrollingThreads, snapshotApiState });
    await page.goto(applicationUrl, { waitUntil: "domcontentloaded" });
    const table = page.locator(".thread-table");
    await table.locator("tbody tr").first().waitFor();
    const tableWrap = page.locator(".table-wrap");
    await tableWrap.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
      element.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await page.waitForFunction(() => document.querySelectorAll(".thread-table tbody tr").length >= 100);
    const targetRow = table.locator('tbody tr[data-thread-id="scroll-thread-072"]');
    await targetRow.scrollIntoViewIfNeeded();
    await tableWrap.evaluate((element) => element.dispatchEvent(new Event("scroll", { bubbles: true })));
    const before = await visibleThreadAnchor(page);
    assert.ok(before?.threadId, "a visible row anchor must exist before refresh");
    assert.ok(before.scrollTop > 500, "the test must exercise a deeply scrolled table");

    const priorPageRequests = snapshotApiState.pageRequests.length;
    snapshotApiState.catalogVersion = "scroll-catalog-2";
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await waitForPromptApiState(
      page,
      () => snapshotApiState.pageRequests.length >= priorPageRequests + 2,
      "catalog refresh must reload enough pages to preserve the loaded depth"
    );
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const after = await visibleThreadAnchor(page);
    assert.equal(after?.threadId, before.threadId, "catalog refresh must preserve the first visible thread anchor");
    assert.ok(Math.abs((after?.offset || 0) - before.offset) <= 3, `catalog refresh must preserve the anchor pixel offset: before=${before.offset}, after=${after?.offset}`);
    assert.ok((after?.scrollTop || 0) > 500, "catalog refresh must not jump the thread table back to the top");
  } finally {
    void context.close().catch(() => {});
    void browser.close().catch(() => {});
  }
}

async function runPromptPaginationFlow() {
  const promptApiState = { pageRequests: [], cancelRequests: [], copyRequests: [] };
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  try {
    const page = await context.newPage();
    await installApplicationRoutes(page, { simulateColdIndex: true, promptApiState });
    await page.goto(applicationUrl, { waitUntil: "domcontentloaded" });
    const threadRow = page.locator(".thread-table tbody tr", { hasText: thread.title });
    await threadRow.waitFor();
    await threadRow.dblclick();
    const detailDialog = page.locator(".thread-detail-modal");
    await detailDialog.waitFor();
    await detailDialog.getByRole("button", { name: "查看线程内容" }).last().click();
    const promptDialog = page.locator(".prompt-modal");
    await promptDialog.waitFor();
    await promptDialog.getByRole("tab", { name: "我的输入" }).click();

    await promptDialog.locator(".prompt-list").getByText("Verify keyboard access", { exact: true }).waitFor();
    const promptScanStatus = promptDialog.locator(".prompt-index-scanning");
    await promptScanStatus.waitFor();
    assert.match(await promptScanStatus.textContent(), /^扫描中(?: \d+(?:\.\d+)?%)? · 1$/, "cold indexing must expose progress and the current match count");
    assert.equal(promptApiState.pageRequests[0].scope, "pure", "pure filter must map to the pure backend scope");
    assert.equal(promptApiState.pageRequests[0].order, "desc", "prompt view must open newest-first");
    assert.ok(promptApiState.pageRequests[0].scanBudgetMs <= 1200, "cold indexing must keep each scan request within the interactive budget");
    assert.equal(promptApiState.pageRequests[0].cursor, null, "the first screen must start without a cursor");

    await waitForPromptApiState(page, () => promptApiState.pageRequests.filter((request) => request.scope === "pure").length >= 2, "cold pure index did not continue scanning");
    const cancelledPureRequest = promptApiState.pageRequests.filter((request) => request.scope === "pure").at(-1);
    await promptDialog.getByRole("button", { name: /^全部 \d+$/ }).click();
    await waitForPromptApiState(page, () => promptApiState.cancelRequests.some((request) => request.requestId === cancelledPureRequest.requestId), "changing scope did not DELETE-cancel the prior request");

    await waitForPromptApiState(page, () => promptApiState.pageRequests.filter((request) => request.scope === "all" && request.search === "").length >= 2, "cold all-scope index did not continue scanning");
    const newestFirstRequest = promptApiState.pageRequests.filter((request) => request.scope === "all" && request.search === "" && request.order === "desc").at(-1);
    await promptDialog.getByRole("button", { name: "时间顺序" }).click();
    await waitForPromptApiState(page, () => promptApiState.pageRequests.some((request) => request.scope === "all" && request.search === "" && request.order === "asc"), "chronological order did not issue an ascending page request");
    await waitForPromptApiState(page, () => promptApiState.cancelRequests.some((request) => request.requestId === newestFirstRequest.requestId), "changing prompt order did not cancel the newest-first request");
    assert.equal(await promptDialog.getByRole("button", { name: "时间顺序" }).getAttribute("aria-pressed"), "true", "chronological order must expose its selected state");

    const descendingRequestCount = promptApiState.pageRequests.filter((request) => request.scope === "all" && request.search === "" && request.order === "desc").length;
    await promptDialog.getByRole("button", { name: "跳到最新" }).click();
    await waitForPromptApiState(page, () => promptApiState.pageRequests.filter((request) => request.scope === "all" && request.search === "" && request.order === "desc").length > descendingRequestCount, "jump to latest did not issue a newest-first page request");
    assert.equal(await promptDialog.getByRole("button", { name: "最新在前" }).getAttribute("aria-pressed"), "true", "jump to latest must select newest-first order");
    assert.ok(await promptDialog.locator(".prompt-list").evaluate((element) => element.scrollTop <= 1), "jump to latest must reset the virtual list to its first newest record");

    await promptDialog.getByRole("button", { name: "时间顺序" }).click();
    await waitForPromptApiState(page, () => promptApiState.pageRequests.filter((request) => request.scope === "all" && request.search === "" && request.order === "asc").length >= 2, "chronological order did not restart paging");
    const cancelledAllRequest = promptApiState.pageRequests.filter((request) => request.scope === "all" && request.search === "" && request.order === "asc").at(-1);
    const promptSearch = promptDialog.getByRole("searchbox", { name: "搜索当前筛选的全部内容" });
    await promptSearch.fill("paged-search");
    await waitForPromptApiState(page, () => promptApiState.cancelRequests.some((request) => request.requestId === cancelledAllRequest.requestId), "changing search did not DELETE-cancel the prior request");

    await promptDialog.getByText(/^1 \/ 3 · 扫描中(?: \d+(?:\.\d+)?%)?$/).waitFor();
    await promptDialog.getByText("1 / 3 · 仍有结果未加载", { exact: true }).waitFor();
    assert.equal(await promptDialog.getByText("paged-search match 3", { exact: false }).count(), 0, "a later search page must not be in the DOM before paging");
    const nextMatch = promptDialog.getByRole("button", { name: "下一个匹配" });
    const previousMatch = promptDialog.getByRole("button", { name: "上一个匹配" });
    assert.equal(await previousMatch.isEnabled(), true, "previous match must remain usable at the first loaded item");
    await previousMatch.click();
    await promptDialog.getByText("2 / 3 · 仍有结果未加载", { exact: true }).waitFor();
    await promptDialog.getByText("已循环到当前已加载的最后匹配；后端仍有匹配尚未加载。", { exact: true }).waitFor();
    await nextMatch.click();
    await promptDialog.getByText("3 / 3 · 完整", { exact: true }).waitFor();
    await promptDialog.getByText("paged-search match 3", { exact: false }).waitFor();
    assert.ok(promptApiState.pageRequests.some((request) => request.search === "paged-search" && request.cursor === "cursor-2"), "next-match navigation must request the next backend page");
    await nextMatch.click();
    await promptDialog.getByText("1 / 3 · 完整", { exact: true }).waitFor();
    await promptDialog.getByText("已循环到第一个匹配。", { exact: true }).waitFor();
    await previousMatch.click();
    await promptDialog.getByText("3 / 3 · 完整", { exact: true }).waitFor();
    await promptDialog.getByText("已循环到最后一个匹配。", { exact: true }).waitFor();

    assert.equal(promptApiState.copyRequests.length, 0, "copy content must not be requested before the user clicks copy");
    await promptDialog.getByRole("button", { name: "复制干净文本" }).click();
    await promptDialog.getByRole("button", { name: "已复制" }).waitFor();
    const textCopyRequest = promptApiState.copyRequests.at(-1);
    assert.equal(textCopyRequest.threadId, thread.id);
    assert.equal(textCopyRequest.scope, "all", "copy must use the active backend scope");
    assert.equal(textCopyRequest.search, "paged-search", "copy must use the active backend search");
    assert.equal(textCopyRequest.format, "text", "clean copy must request streamed text");
    assert.ok(textCopyRequest.requestId, "copy must provide a cancellable requestId");
    const copiedText = await page.evaluate(() => window.__copiedPromptText);
    assert.match(copiedText, /paged-search match 1/);
    assert.match(copiedText, /paged-search match 3/);
    await promptDialog.getByRole("button", { name: "带元信息" }).click();
    await promptDialog.getByRole("button", { name: "复制带元信息" }).click();
    await promptDialog.getByRole("button", { name: "已复制" }).waitFor();
    assert.equal(promptApiState.copyRequests.at(-1).format, "jsonl", "metadata copy must request the streamed jsonl format");
    assert.match(await page.evaluate(() => window.__copiedPromptText), /## Prompt 32/);
  } finally {
    void context.close().catch(() => {});
    void browser.close().catch(() => {});
  }
}

async function runPromptRequestRaceFlow() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  try {
    const page = await context.newPage();
    const promptApiState = { pageRequests: [], cancelRequests: [], copyRequests: [] };
    await installApplicationRoutes(page, { includeRaceThread: true, emulateUncancellableSlowPrompt: true, promptApiState });
    await page.goto(applicationUrl, { waitUntil: "domcontentloaded" });
    await page.locator(".thread-table tbody tr").first().waitFor();

    const slowThreadRow = page.locator(".thread-table tbody tr", { hasText: thread.title });
    await slowThreadRow.dblclick();
    let detailDialog = page.locator(".thread-detail-modal");
    await detailDialog.waitFor();
    await detailDialog.getByRole("button", { name: "查看线程内容" }).last().click();
    let promptDialog = page.locator(".prompt-modal");
    await promptDialog.waitFor();
    await promptDialog.getByRole("tab", { name: "我的输入" }).click();
    await page.waitForFunction(() => window.__slowPromptRequestStarted === true);

    await promptDialog.getByRole("button", { name: "关闭详情窗口" }).click();
    await promptDialog.waitFor({ state: "detached" });
    await page.waitForFunction(() => window.__slowPromptAbortObserved === true);
    await waitForPromptApiState(page, () => promptApiState.cancelRequests.some((request) => request.threadId === thread.id), "closing the prompt dialog did not DELETE-cancel slow A");
    await detailDialog.getByRole("button", { name: "关闭详情窗口" }).first().click();
    await detailDialog.waitFor({ state: "detached" });

    const fastThreadRow = page.locator(".thread-table tbody tr", { hasText: raceThread.title });
    await fastThreadRow.dblclick();
    detailDialog = page.locator(".thread-detail-modal");
    await detailDialog.waitFor();
    await detailDialog.getByRole("button", { name: "查看线程内容" }).last().click();
    promptDialog = page.locator(".prompt-modal");
    await promptDialog.waitFor();
    await promptDialog.getByRole("tab", { name: "我的输入" }).click();
    await promptDialog.getByText("FAST B CURRENT PROMPT", { exact: true }).waitFor();
    await page.waitForFunction(() => window.__slowPromptResponseReturned === true);
    await page.waitForTimeout(50);

    assert.equal(await promptDialog.getByRole("heading", { name: raceThread.title }).count(), 1, "fast B must remain the active prompt dialog");
    assert.equal(await promptDialog.getByText("FAST B CURRENT PROMPT", { exact: true }).count(), 1, "fast B prompt data must remain rendered");
    assert.equal(await promptDialog.getByText("SLOW A STALE PROMPT", { exact: true }).count(), 0, "slow A response must not overwrite fast B after A returns");
  } finally {
    void context.close().catch(() => {});
    void browser.close().catch(() => {});
  }
}

async function runPromptIndexEnglishFlow() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  try {
    const page = await context.newPage();
    await installApplicationRoutes(page, { promptIndexApiState: createPromptIndexApiState() });
    await page.goto(applicationUrl, { waitUntil: "domcontentloaded" });
    await page.locator(".thread-table tbody tr").first().waitFor();
    await page.locator(".language-toggle").click();
    await page.waitForFunction(() => document.documentElement.lang === "en");
    const threadRow = page.locator(".thread-table tbody tr").first();
    await threadRow.dblclick();
    const detailDialog = page.locator(".thread-detail-modal");
    await detailDialog.waitFor();
    await detailDialog.getByRole("button", { name: "View thread content" }).last().click();
    const promptDialog = page.locator(".prompt-modal");
    await promptDialog.waitFor();
    await promptDialog.getByRole("tab", { name: "My input" }).click();
    const management = promptDialog.locator(".prompt-index-management");
    await management.locator("summary").focus();
    await page.keyboard.press("Enter");
    await management.getByText("Derived plaintext index", { exact: true }).waitFor();
    await management
      .locator(".prompt-index-path code")
      .getByText("C:\\Users\\Test\\AppData\\Local\\CodexHomeManager\\prompt-index", { exact: true })
      .waitFor();
    const text = await management.innerText();
    assert.match(text, /Local search index/);
    assert.match(text, /This is a derived plaintext index stored on this device/);
    assert.match(text, /Local index directory\s+C:\\Users\\Test\\AppData\\Local\\CodexHomeManager\\prompt-index/);
    assert.match(text, /Idle cleanup\s+30 days/);
    assert.match(text, /Capacity limit 1\.0 GB/);
    assert.match(text, /automatically reclaimed after its source rollout is deleted/);
    assert.match(text, /Clearing the index does not delete source threads/);
    assert.equal(await management.getByRole("button", { name: "Clear index" }).count(), 1);
    await assertAccessibleSurface(page, "English prompt index management");
  } finally {
    void context.close().catch(() => {});
    void browser.close().catch(() => {});
  }
}

async function runAccessibilityFlow() {
  assert.ok(fs.existsSync(path.join(distPath, "index.html")), "run npm run build before the accessibility test");
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  try {
    const page = await context.newPage();
    const promptApiState = { pageRequests: [], cancelRequests: [], copyRequests: [] };
    const promptIndexApiState = createPromptIndexApiState();
    await installApplicationRoutes(page, { promptApiState, promptIndexApiState, timelineUniformTimestamps: true });
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
    await assertTimelineReadingLayout(page, promptDialog, "1440x1000");
    assert.equal(await promptDialog.locator(".timeline-entry-commentary pre").count(), 0, "natural-language progress must never render in terminal-style code blocks");
    await promptDialog.getByText("源记录没有可区分的逐条时间，已隐藏重复时间。", { exact: true }).waitFor();
    assert.equal(await promptDialog.locator(".timeline-entry-head time").count(), 0, "indistinguishable source timestamps must not be repeated on every timeline item");
    await page.keyboard.press("Control+f");
    const timelineSearch = promptDialog.getByRole("textbox", { name: "搜索完整线程内容" });
    assert.equal(await timelineSearch.evaluate((element) => document.activeElement === element), true, "Ctrl+F must focus full-timeline search instead of browser find");
    await timelineSearch.fill("正在验证");
    await promptDialog.getByText("1 / 1 条匹配记录 · 扫描中", { exact: true }).waitFor();
    assert.equal(await promptDialog.locator(".timeline-entry mark", { hasText: "正在验证" }).count(), 1, "loaded timeline content must be searched and highlighted before the full index finishes");
    await promptDialog.getByText("1 / 1 条匹配记录", { exact: true }).waitFor();
    await page.keyboard.press("Escape");
    assert.equal(await promptDialog.isVisible(), true, "the first Escape with a timeline query must keep the content dialog open");
    assert.equal(await timelineSearch.inputValue(), "", "the first Escape must clear the timeline query");
    assert.equal(await timelineSearch.evaluate((element) => document.activeElement === element), true, "clearing timeline search with Escape must retain search focus");
    await promptDialog.getByText("Timeline rendering verified", { exact: true }).waitFor();
    await timelineSearch.fill("missing-result");
    await promptDialog.locator(".timeline-search-count").getByText("扫描中 · 0", { exact: true }).waitFor();
    assert.equal(await promptDialog.getByText("0 / 0", { exact: true }).count(), 0, "an incomplete zero-match scan must never look final");
    await promptDialog.getByText("没有匹配内容", { exact: true }).waitFor();
    await page.keyboard.press("Escape");

    const timelineUnicodeCases = [
      ["office", "oﬃce"],
      ["strasse", "Straße"],
      ["οσ", "ΟΣ"],
      ["cafe", "Cafe\u0301"],
      ["izmir", "İZMİR"]
    ];
    for (const [query, expectedHighlight] of timelineUnicodeCases) {
      await timelineSearch.fill(query);
      await promptDialog.getByText("1 / 1 条匹配记录", { exact: true }).waitFor();
      const highlight = promptDialog.locator(".timeline-entry mark").first();
      assert.equal(await highlight.textContent(), expectedHighlight, `${query} must highlight the complete original grapheme sequence`);
      await page.keyboard.press("Escape");
    }
    await promptDialog.getByText("Timeline rendering verified", { exact: true }).waitFor();
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
    const promptIndexManagement = promptDialog.locator(".prompt-index-management");
    const promptIndexSummary = promptIndexManagement.locator("summary");
    await promptIndexSummary.focus();
    await page.keyboard.press("Enter");
    assert.equal(await promptIndexManagement.getAttribute("open"), "", "prompt index disclosure must open from the keyboard");
    await promptIndexManagement.getByText("派生明文索引", { exact: true }).waitFor();
    await promptIndexManagement
      .locator(".prompt-index-path code")
      .getByText("C:\\Users\\Test\\AppData\\Local\\CodexHomeManager\\prompt-index", { exact: true })
      .waitFor();
    const promptIndexText = await promptIndexManagement.innerText();
    assert.match(promptIndexText, /这是存储在本机的派生明文索引/);
    assert.match(promptIndexText, /本机索引目录\s+C:\\Users\\Test\\AppData\\Local\\CodexHomeManager\\prompt-index/);
    assert.match(promptIndexText, /全部索引大小\s+1\.5 MB/);
    assert.match(promptIndexText, /数据库数\s+3/);
    assert.match(promptIndexText, /当前索引大小\s+512 KB/);
    assert.match(promptIndexText, /索引 prompt\s+125/);
    assert.match(promptIndexText, /空闲回收\s+30 天/);
    assert.match(promptIndexText, /容量上限 1\.0 GB/);
    assert.match(promptIndexText, /源 rollout 删除后，对应派生索引会自动回收/);
    assert.match(promptIndexText, /清空索引不会删除源线程，后续搜索会自动重建/);
    assert.equal(await promptIndexManagement.locator(".prompt-index-path code").textContent(), "C:\\Users\\Test\\AppData\\Local\\CodexHomeManager\\prompt-index", "the UI must display the exact rootPath returned by the status API");
    const clearPromptIndexButton = promptIndexManagement.getByRole("button", { name: "清空索引" });
    const pageRequestsBeforeClear = promptApiState.pageRequests.length;
    const statusRequestsBeforeClear = promptIndexApiState.statusRequests.length;
    page.once("dialog", async (dialog) => {
      assert.match(dialog.message(), /不会删除源线程或 rollout/);
      assert.match(dialog.message(), /当前搜索内容会从源线程重新建立索引/);
      await dialog.accept();
    });
    await clearPromptIndexButton.focus();
    await page.keyboard.press("Enter");
    await waitForPromptApiState(page, () => promptIndexApiState.clearRequests.length === 1, "clear-index write request was not sent");
    await promptIndexManagement.getByText("索引已清空；当前内容正在从源线程重建。", { exact: true }).waitFor();
    assert.equal(promptIndexApiState.previewRequests[0].method, "POST", "clear-index preview must use POST");
    assert.equal(promptIndexApiState.clearRequests[0].method, "POST", "clear-index write must use POST");
    assert.equal(promptIndexApiState.clearRequests[0].token, "test-token", "clear-index write must use the existing authorization header");
    assert.deepEqual(promptIndexApiState.clearRequests[0].body, {
      operationPreviewId: "prompt-index-preview-1",
      inputHash: "prompt-index-input-hash-1"
    }, "clear-index write must bind the preview ticket");
    await waitForPromptApiState(page, () => promptIndexApiState.rebuildPageRequests === 1, "clearing the index did not refresh current prompt content");
    await waitForPromptApiState(page, () => promptIndexApiState.statusRequests.length >= statusRequestsBeforeClear + 2, "clearing and rebuilding did not refresh index status");
    assert.ok(promptApiState.pageRequests.length > pageRequestsBeforeClear, "clearing the index must issue a fresh current-content request");
    await promptDialog.getByText("Verify keyboard access", { exact: true }).waitFor();
    await page.waitForFunction(() => {
      const button = document.querySelector(".prompt-index-management .prompt-index-actions .danger-action");
      return button instanceof HTMLButtonElement && !button.disabled;
    });

    promptIndexApiState.clearConflict = true;
    page.once("dialog", async (dialog) => dialog.accept());
    await clearPromptIndexButton.focus();
    await page.keyboard.press("Space");
    await waitForPromptApiState(page, () => promptIndexApiState.clearRequests.length === 2, "in-use clear-index write request was not observed");
    const promptIndexError = promptIndexManagement.locator(".prompt-index-message.error");
    await promptIndexError.waitFor();
    assert.equal(
      await promptIndexError.textContent(),
      "索引正在使用中，暂时不能清空。请等待当前搜索或扫描完成后重试。",
      "409 must render the dedicated localized in-use message"
    );
    assert.equal(promptIndexApiState.rebuildPageRequests, 1, "409 must not trigger a second content rebuild");
    promptIndexApiState.clearConflict = false;
    await assertAccessibleSurface(page, "expanded prompt index management");
    const purePromptListText = await promptDialog.locator(".prompt-list").innerText();
    assert.equal(
      await promptDialog.locator(".prompt-list .prompt-entry").count(),
      1,
      "pure prompt mode must correct stale connector metadata and hide runtime-injected context"
    );
    assert.doesNotMatch(purePromptListText, /<recommended_plugins>/);
    await promptDialog.locator(".prompt-filter-bar").click({ position: { x: 6, y: 6 } });
    assert.equal(await promptIndexManagement.getAttribute("open"), null, "clicking outside the index popover must dismiss it");
    await promptDialog.getByRole("button", { name: /^全部 \d+$/ }).click();
    await promptDialog.getByRole("button", { name: "时间顺序" }).click();
    await promptDialog.getByText("推荐插件上下文", { exact: true }).waitFor();
    await assertPromptModalLayoutAndHitTargets(page, promptDialog, "1440x1000");
    assert.equal(await promptDialog.getByText("顶刊能力建设的尾部搜索目标", { exact: false }).count(), 0, "virtualized tail content must not be present in the DOM before searching");
    await page.keyboard.press("Control+f");
    const promptSearch = promptDialog.getByRole("searchbox", { name: "搜索当前筛选的全部内容" });
    assert.equal(await promptSearch.evaluate((element) => document.activeElement === element), true, "Ctrl+F must focus the full-content prompt search instead of browser find");
    await promptSearch.fill("顶刊");
    await promptDialog.getByText("1 / 1 · 完整", { exact: true }).waitFor();
    await promptDialog.getByText("顶刊能力建设的尾部搜索目标", { exact: false }).waitFor();
    assert.equal(await promptDialog.locator(".prompt-entry mark", { hasText: "顶刊" }).count(), 1, "full-content search must highlight matches found outside the rendered window");
    await promptDialog.getByRole("button", { name: "清空搜索" }).click();
    assert.equal(await promptSearch.inputValue(), "", "clear search must restore the unfiltered prompt list");
    await promptSearch.fill("paged-search");
    await waitForPromptApiState(page, () => promptApiState.pageRequests.some((request) => request.search === "paged-search"), "paged-search request was not observed");
    const promptSearchCount = promptDialog.locator(".prompt-search-count > span:first-child");
    await page.waitForFunction(() => /^1 \/ 3 · (仍有结果未加载|完整)$/.test(document.querySelector(".prompt-modal .prompt-search-count > span:first-child")?.textContent?.trim() || ""));
    await promptDialog.locator(".prompt-entry").nth(1).waitFor();
    await assertPromptModalLayoutAndHitTargets(page, promptDialog, "1440x1000 searched");
    const previousMatch = promptDialog.getByRole("button", { name: "上一个匹配" });
    const nextMatch = promptDialog.getByRole("button", { name: "下一个匹配" });
    await previousMatch.focus();
    await previousMatch.press("Enter");
    await page.waitForFunction(() => !document.querySelector(".prompt-modal .prompt-search-count > span:first-child")?.textContent?.trim().startsWith("1 / 3"));
    const desktopPreviousCount = (await promptSearchCount.textContent())?.trim();
    assert.ok(
      desktopPreviousCount === "2 / 3 · 仍有结果未加载" || desktopPreviousCount === "3 / 3 · 完整",
      `previous match must cycle away from the first result: ${desktopPreviousCount}`
    );
    await nextMatch.focus();
    await nextMatch.press("Space");
    await promptDialog.getByText(desktopPreviousCount?.startsWith("2 / 3") ? "3 / 3 · 完整" : "1 / 3 · 完整", { exact: true }).waitFor();
    await promptDialog.getByRole("button", { name: "清空搜索" }).click();
    await promptSearch.fill("izmir");
    await promptDialog.getByText("1 / 1 · 完整", { exact: true }).waitFor();
    const turkishHighlight = promptDialog.locator(".prompt-entry mark").first();
    assert.equal(await turkishHighlight.textContent(), "İZMİR", "Turkish dotted capital I must map back to the complete original text");
    await promptDialog.getByRole("button", { name: "清空搜索" }).click();
    await promptSearch.fill("CAFÉ");
    await promptDialog.getByText("1 / 1 · 完整", { exact: true }).waitFor();
    const combiningHighlight = promptDialog.locator(".prompt-entry mark").first();
    assert.equal(await combiningHighlight.textContent(), "Cafe\u0301", "canonical-equivalent search must retain the original combining sequence");
    await promptDialog.getByRole("button", { name: "清空搜索" }).click();
    await page.waitForFunction(() => {
      const count = document.querySelector(".prompt-modal .prompt-search-count > span:first-child");
      return /^\d+ \/ 125 · (仍有结果未加载|完整)$/.test(count?.textContent?.trim() || "");
    });
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileOverflow = await promptDialog.evaluate((element) => element.scrollWidth - element.clientWidth);
    assert.ok(mobileOverflow <= 1, `thread content dialog must not overflow horizontally on mobile: ${mobileOverflow}px`);
    const mobilePromptIndexLayout = await promptIndexManagement.evaluate((element) => {
      const facts = element.querySelector(".prompt-index-facts");
      return {
        overflow: element.scrollWidth - element.clientWidth,
        factColumns: facts ? getComputedStyle(facts).gridTemplateColumns.trim().split(/\s+/).filter(Boolean).length : 0
      };
    });
    assert.ok(mobilePromptIndexLayout.overflow <= 1, `prompt index management must not overflow on mobile: ${mobilePromptIndexLayout.overflow}px`);
    assert.equal(mobilePromptIndexLayout.factColumns, 2, "prompt index facts must use two columns on mobile");
    await promptSearch.fill("paged-search");
    await page.waitForFunction(() => /^1 \/ 3 · (仍有结果未加载|完整)$/.test(document.querySelector(".prompt-modal .prompt-search-count > span:first-child")?.textContent?.trim() || ""));
    const mobileLayout = await assertPromptModalLayoutAndHitTargets(page, promptDialog, "390x844");
    const mobilePreviousTarget = mobileLayout.buttonHits.find((target) => target.label === "上一个匹配");
    assert.ok(mobilePreviousTarget, "390x844: previous-match center must be available for pointer activation");
    await page.mouse.click(mobilePreviousTarget.centerX, mobilePreviousTarget.centerY);
    await page.waitForFunction(() => !document.querySelector(".prompt-modal .prompt-search-count > span:first-child")?.textContent?.trim().startsWith("1 / 3"));
    const mobilePreviousCount = (await promptSearchCount.textContent())?.trim();
    assert.ok(
      mobilePreviousCount === "2 / 3 · 仍有结果未加载" || mobilePreviousCount === "2 / 3 · 完整" || mobilePreviousCount === "3 / 3 · 完整",
      `mobile previous-match center click must cycle away from the first result: ${mobilePreviousCount}`
    );
    await nextMatch.focus();
    await nextMatch.press("Enter");
    await promptDialog.getByText(mobilePreviousCount?.startsWith("2 / 3") ? "3 / 3 · 完整" : "1 / 3 · 完整", { exact: true }).waitFor();
    await promptDialog.getByRole("button", { name: "清空搜索" }).click();
    await page.setViewportSize({ width: 1440, height: 1000 });
    await assertAccessibleSurface(page, "prompts dialog");
    await assertDialogKeyboardContract(page, promptDialog);
    await promptSearch.fill("👩‍💻");
    await promptDialog.getByText("1 / 1 · 完整", { exact: true }).waitFor();
    const emojiHighlight = promptDialog.locator(".prompt-entry mark").first();
    assert.equal(await emojiHighlight.textContent(), "👩‍💻", "emoji grapheme search must highlight the intact original sequence");
    await page.keyboard.press("Escape");
    assert.equal(await promptDialog.isVisible(), true, "the first Escape with a non-empty prompt search must keep the dialog open");
    assert.equal(await promptSearch.inputValue(), "", "the first Escape must clear the prompt search");
    assert.equal(await promptSearch.evaluate((element) => document.activeElement === element), true, "clearing search with Escape must retain search focus");
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
  await runThreadScrollAnchorFlow();
  await runPromptPaginationFlow();
  await runPromptRequestRaceFlow();
  await runPromptIndexEnglishFlow();
  await runAccessibilityFlow();
} catch (error) {
  exitCode = 1;
  console.error(error instanceof Error ? error.stack || error.message : String(error));
} finally {
  setTimeout(() => process.exit(exitCode), 25);
}
