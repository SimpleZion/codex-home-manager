import { existsSync } from "node:fs";
import { chromium } from "playwright";

const serviceUrl = process.argv[2] || "http://127.0.0.1:8765";
const edgeCandidates = [
  process.env.MSEDGE_EXECUTABLE,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"
].filter(Boolean);

const edgeExecutablePath = edgeCandidates.find((candidate) => existsSync(candidate));
const browser = await chromium.launch({
  headless: true,
  ...(edgeExecutablePath ? { executablePath: edgeExecutablePath } : { channel: "msedge" })
});

function assertCondition(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
  console.log(`ok: ${message}`);
}

const threadIndexTimeoutMs = 60000;
const diagnosticsTimeoutMs = 60000;

async function waitForThreadIndex(page, { requireRows = false } = {}) {
  try {
    await page.waitForSelector(".table-wrap", { timeout: threadIndexTimeoutMs });
    await page.waitForSelector(
      requireRows ? ".thread-table tbody tr" : ".thread-table tbody tr, .empty-state",
      { timeout: threadIndexTimeoutMs }
    );
  } catch (error) {
    const pageState = await page.evaluate(() => ({
      url: window.location.href,
      readyState: document.readyState,
      isLoadingPage: Boolean(document.querySelector(".loading-page")),
      hasTableWrap: Boolean(document.querySelector(".table-wrap")),
      hasEmptyState: Boolean(document.querySelector(".empty-state")),
      rowCount: document.querySelectorAll(".thread-table tbody tr").length,
      bodyText: (document.body?.innerText || "").slice(0, 800)
    })).catch((stateError) => ({ evaluateError: String(stateError) }));
    throw new Error(
      `thread index did not finish loading within ${threadIndexTimeoutMs}ms: ${JSON.stringify(pageState)}\n${error}`
    );
  }
}

async function waitForDiagnosticsPage(page) {
  try {
    await page.waitForSelector(".diagnostics-module", { timeout: diagnosticsTimeoutMs });
    await page.waitForSelector(".diagnostic-check, .diagnostic-card", { timeout: diagnosticsTimeoutMs });
  } catch (error) {
    const pageState = await page.evaluate(() => ({
      url: window.location.href,
      readyState: document.readyState,
      hasDiagnosticsModule: Boolean(document.querySelector(".diagnostics-module")),
      checkCount: document.querySelectorAll(".diagnostic-check").length,
      cardCount: document.querySelectorAll(".diagnostic-card").length,
      bodyText: (document.body?.innerText || "").slice(0, 800)
    })).catch((stateError) => ({ evaluateError: String(stateError) }));
    throw new Error(
      `diagnostics page did not finish loading within ${diagnosticsTimeoutMs}ms: ${JSON.stringify(pageState)}\n${error}`
    );
  }
}

async function openFirstThreadDetailPanel(page) {
  await page.locator(".thread-table tbody tr").first().click();
  await page.waitForTimeout(150);
  if (await page.locator(".detail-panel.expanded").count() === 0) {
    await page.locator(".strip-action").click();
  }
  await page.waitForSelector(".detail-panel.expanded", { timeout: 15000 });
}

function parseBytesLabel(value) {
  const match = String(value || "").match(/([\d.]+)\s*(B|KB|MB|GB|TB)/i);
  if (!match) return 0;
  const unitMultipliers = {
    B: 1,
    KB: 1024,
    MB: 1024 ** 2,
    GB: 1024 ** 3,
    TB: 1024 ** 4
  };
  return Number(match[1]) * (unitMultipliers[match[2].toUpperCase()] || 1);
}

async function verifyTableSortingAndFullDetail(page) {
  await page.setViewportSize({ width: 2048, height: 1152 });
  await page.addInitScript(`${parseBytesLabel.toString()}; window.__codexHomeManagerParseBytesForGate = parseBytesLabel;`);
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await waitForThreadIndex(page, { requireRows: true });

  const headerMetrics = await page.evaluate(() => ({
    headerLabels: Array.from(document.querySelectorAll(".thread-table thead .sortable-header"))
      .map((button) => button.textContent?.trim() || ""),
    pageClientWidth: document.documentElement.clientWidth,
    pageScrollWidth: document.documentElement.scrollWidth
  }));
  assertCondition(headerMetrics.headerLabels.length >= 7, "thread table exposes sortable headers for all data columns");
  assertCondition(headerMetrics.headerLabels.some((label) => label.includes("存储")), "thread table storage column is sortable from its header");
  assertCondition(headerMetrics.pageScrollWidth <= headerMetrics.pageClientWidth + 1, "sortable header layout has no horizontal page overflow");

  const storageHeader = page.locator(".thread-table thead th").filter({ hasText: "存储" }).locator(".sortable-header");
  await storageHeader.click();
  await page.waitForTimeout(250);
  const descendingMetrics = await page.evaluate(() => {
    const storageValues = Array.from(document.querySelectorAll(".thread-table tbody tr td:nth-child(6) strong"))
      .slice(0, 12)
      .map((element) => element.textContent || "");
    const storageBytes = storageValues.map((label) => window.__codexHomeManagerParseBytesForGate(label));
    return {
      sortValue: document.querySelector(".sort-control select")?.value || "",
      activeHeader: document.querySelector(".sortable-header.active")?.textContent || "",
      activeTitle: document.querySelector(".sortable-header.active")?.getAttribute("title") || "",
      storageValues,
      storageBytes,
      childAggregateRows: Array.from(document.querySelectorAll(".thread-table tbody tr td:nth-child(6)"))
        .filter((cell) => (cell.textContent || "").includes("含子线程")).length
    };
  });
  assertCondition(descendingMetrics.sortValue === "size", "clicking storage header selects storage sort mode");
  assertCondition(descendingMetrics.activeHeader.includes("存储"), "storage header becomes the active sortable header");
  assertCondition(descendingMetrics.activeTitle.includes("降序"), "storage header first click applies descending direction");
  assertCondition(
    descendingMetrics.storageBytes.every((value, index, values) => index === 0 || values[index - 1] >= value),
    "storage descending header sort orders visible rows by total storage"
  );
  assertCondition(descendingMetrics.childAggregateRows > 0, "storage cells display child-thread aggregate storage when present");

  await storageHeader.click();
  await page.waitForTimeout(250);
  const ascendingMetrics = await page.evaluate(() => {
    const storageValues = Array.from(document.querySelectorAll(".thread-table tbody tr td:nth-child(6) strong"))
      .slice(0, 12)
      .map((element) => element.textContent || "");
    const storageBytes = storageValues.map((label) => window.__codexHomeManagerParseBytesForGate(label));
    return {
      sortValue: document.querySelector(".sort-control select")?.value || "",
      activeTitle: document.querySelector(".sortable-header.active")?.getAttribute("title") || "",
      storageBytes
    };
  });
  assertCondition(ascendingMetrics.sortValue === "size", "second storage header click keeps storage sort mode");
  assertCondition(ascendingMetrics.activeTitle.includes("升序"), "storage header second click toggles to ascending direction");
  assertCondition(
    ascendingMetrics.storageBytes.every((value, index, values) => index === 0 || values[index - 1] <= value),
    "storage ascending header sort orders visible rows by total storage"
  );

  const childAggregateRowIndex = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll(".thread-table tbody tr"));
    return rows.findIndex((row) => (row.querySelector("td:nth-child(6)")?.textContent || "").includes("含子线程"));
  });
  const rowIndex = childAggregateRowIndex >= 0 ? childAggregateRowIndex : 0;
  await page.locator(".thread-table tbody tr").nth(rowIndex).dblclick();
  await page.waitForSelector(".thread-detail-modal", { timeout: 15000 });
  await page.waitForFunction(() => (
    document.querySelectorAll(".detail-bar-row").length > 0
    || Array.from(document.querySelectorAll(".thread-detail-card .visual-empty"))
      .some((element) => (element.textContent || "").includes("没有可用线程数据"))
  ), { timeout: 15000 });
  const modalMetrics = await page.evaluate(() => {
    const modal = document.querySelector(".thread-detail-modal");
    const metricTexts = Array.from(document.querySelectorAll(".thread-detail-metrics article")).map((card) => card.textContent || "");
    const childRows = document.querySelectorAll(".child-thread-row").length;
    const modalRect = modal?.getBoundingClientRect();
    return {
      modalExists: Boolean(modal),
      modalWidth: modalRect?.width ?? 0,
      modalHeight: modalRect?.height ?? 0,
      metricCardCount: metricTexts.length,
      metricTexts,
      compositionBars: document.querySelectorAll(".composition-bar").length,
      detailBarRows: document.querySelectorAll(".detail-bar-row").length,
      messageStructureSettled: document.querySelectorAll(".detail-bar-row").length > 0
        || Array.from(document.querySelectorAll(".thread-detail-card .visual-empty"))
          .some((element) => (element.textContent || "").includes("没有可用线程数据")),
      childRows,
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth
    };
  });
  assertCondition(modalMetrics.modalExists, "double-clicking a thread opens the full detail modal");
  assertCondition(modalMetrics.metricCardCount >= 4, "full detail modal renders storage and token metric cards");
  assertCondition(modalMetrics.metricTexts.some((text) => text.includes("总存储")), "full detail modal includes total storage");
  assertCondition(modalMetrics.metricTexts.some((text) => text.includes("子线程")), "full detail modal includes child-thread totals");
  assertCondition(modalMetrics.compositionBars >= 2, "full detail modal renders storage and token composition visuals");
  assertCondition(modalMetrics.messageStructureSettled, "full detail modal resolves the message structure visualization area");
  if (childAggregateRowIndex >= 0) {
    assertCondition(modalMetrics.childRows > 0, "full detail modal lists descendant child threads for aggregate-storage rows");
  }
  assertCondition(modalMetrics.modalWidth >= 900 && modalMetrics.modalHeight >= 680, "full detail modal has enough desktop reading area");
  assertCondition(modalMetrics.pageScrollWidth <= modalMetrics.pageClientWidth + 1, "full detail modal has no horizontal page overflow");
  await page.getByTitle("关闭详情窗口").click();
  await page.waitForSelector(".thread-detail-modal", { state: "detached", timeout: 10000 });
  console.log(`ui-sort-detail metrics: ${JSON.stringify({ descendingMetrics, ascendingMetrics, modalMetrics })}`);
}

async function verifyLanguageSwitching(page) {
  await page.setViewportSize({ width: 1707, height: 1067 });
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => window.localStorage.removeItem("codex-home-manager-language"));
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitForThreadIndex(page);

  const zhMetrics = await page.evaluate(() => {
    const navText = Array.from(document.querySelectorAll(".side-nav button")).map((button) => button.textContent || "");
    const searchPlaceholder = document.querySelector(".toolbar input")?.getAttribute("placeholder") || "";
    const warningTitle = document.querySelector(".health-warning-strip")?.getAttribute("title") || "";
    return {
      lang: document.documentElement.lang,
      storedLanguage: window.localStorage.getItem("codex-home-manager-language"),
      toggleText: document.querySelector(".language-toggle")?.textContent || "",
      navText,
      searchPlaceholder,
      warningTitle,
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth
    };
  });
  assertCondition(zhMetrics.lang === "zh-CN", "default language sets zh-CN document language");
  assertCondition(zhMetrics.storedLanguage === "zh", "default language persists zh");
  assertCondition(zhMetrics.toggleText.includes("EN"), "default language toggle offers English");
  assertCondition(zhMetrics.navText.some((text) => text.includes("线程")), "default language renders Chinese navigation");
  assertCondition(zhMetrics.navText.some((text) => text.includes("体检")), "default language renders Chinese diagnostics navigation");
  assertCondition(zhMetrics.searchPlaceholder.includes("搜索标题"), "default language renders Chinese search placeholder");
  assertCondition(!zhMetrics.warningTitle || zhMetrics.warningTitle.includes("Codex 相关进程"), "default language localizes running Codex warning");
  assertCondition(zhMetrics.pageScrollWidth <= zhMetrics.pageClientWidth + 1, "default language has no horizontal page overflow");

  await page.locator(".language-toggle").click();
  await page.waitForFunction(() => (
    window.localStorage.getItem("codex-home-manager-language") === "en"
    && document.documentElement.lang === "en"
    && document.body.textContent?.includes("Threads")
    && document.querySelector(".toolbar input")?.getAttribute("placeholder")?.includes("Search title")
  ));
  const enMetrics = await page.evaluate(() => {
    const navText = Array.from(document.querySelectorAll(".side-nav button")).map((button) => button.textContent || "");
    const searchPlaceholder = document.querySelector(".toolbar input")?.getAttribute("placeholder") || "";
    const warningTitle = document.querySelector(".health-warning-strip")?.getAttribute("title") || "";
    return {
      lang: document.documentElement.lang,
      storedLanguage: window.localStorage.getItem("codex-home-manager-language"),
      toggleText: document.querySelector(".language-toggle")?.textContent || "",
      navText,
      searchPlaceholder,
      warningTitle,
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth
    };
  });
  assertCondition(enMetrics.lang === "en", "English switch sets en document language");
  assertCondition(enMetrics.storedLanguage === "en", "English switch persists en");
  assertCondition(enMetrics.toggleText.includes("中文"), "English language toggle offers Chinese");
  assertCondition(enMetrics.navText.some((text) => text.includes("Threads")), "English switch renders English navigation");
  assertCondition(enMetrics.navText.some((text) => text.includes("Diagnostics")), "English switch renders English diagnostics navigation");
  assertCondition(enMetrics.searchPlaceholder.includes("Search title"), "English switch renders English search placeholder");
  assertCondition(!enMetrics.warningTitle || enMetrics.warningTitle.includes("Codex-related process"), "English switch keeps running Codex warning in English");
  assertCondition(enMetrics.pageScrollWidth <= enMetrics.pageClientWidth + 1, "English switch has no horizontal page overflow");

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => (
    window.localStorage.getItem("codex-home-manager-language") === "en"
    && document.documentElement.lang === "en"
    && document.body.textContent?.includes("Threads")
  ));
  assertCondition(await page.locator(".language-toggle").textContent().then((text) => text?.includes("中文")), "English language persists after reload");

  await page.getByRole("button", { name: "API" }).click();
  await page.waitForFunction(() => document.body.textContent?.includes("Stable local API for new agents"));
  await page.waitForFunction(() => document.body.textContent?.includes("Get the per-process token required for write endpoints. Browser access is restricted to the loopback same-origin local UI."));
  const apiEnText = await page.locator("body").innerText();
  assertCondition(apiEnText.includes("Prefer MCP for agents"), "English API page renders English section labels");
  assertCondition(apiEnText.includes("Get the per-process token required for write endpoints. Browser access is restricted to the loopback same-origin local UI."), "English API page receives English capability descriptions");

  await page.locator(".language-toggle").click();
  await page.waitForFunction(() => (
    window.localStorage.getItem("codex-home-manager-language") === "zh"
    && document.documentElement.lang === "zh-CN"
    && document.body.textContent?.includes("给新 agent 直接调用的稳定本地 API")
  ));
  await page.waitForFunction(() => !document.body.textContent?.includes("Get the per-process token required for write endpoints. Browser access is restricted to the loopback same-origin local UI."));
  const apiZhText = await page.locator("body").innerText();
  assertCondition(apiZhText.includes("MCP 优先接入"), "Chinese API page renders Chinese section labels");
  assertCondition(apiZhText.includes("获取写入接口所需的本进程本地 API token。"), "Chinese API page receives Chinese capability descriptions");
  assertCondition(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1), "Chinese API page has no horizontal overflow");

  await page.locator(".side-nav button").first().click();
  await waitForThreadIndex(page);
}

async function measureViewport(page, viewport, label) {
  await page.setViewportSize(viewport);
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await waitForThreadIndex(page);

  const metrics = await page.evaluate(() => {
    const wrap = document.querySelector(".table-wrap");
    const table = document.querySelector(".thread-table");
    const toolbar = document.querySelector(".toolbar");
    const sortControl = document.querySelector(".sort-control");
    const workspace = document.querySelector(".workspace");
    const visuals = document.querySelector(".thread-visuals");
    const visualCards = Array.from(document.querySelectorAll(".visual-card"));
    const mainPanel = document.querySelector(".main-panel");
    const detailPanel = document.querySelector(".detail-panel");
    const actionHeader = document.querySelector(".thread-table th:last-child");
    const firstActionCell = document.querySelector(".thread-table tbody tr:first-child td:last-child");
    const wrapRect = wrap?.getBoundingClientRect();
    const headerRect = actionHeader?.getBoundingClientRect();
    const actionRect = firstActionCell?.getBoundingClientRect();
    const detailStyle = detailPanel ? getComputedStyle(detailPanel) : null;

    return {
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      pageClientHeight: document.documentElement.clientHeight,
      pageScrollHeight: document.documentElement.scrollHeight,
      bodyOverflowY: getComputedStyle(document.body).overflowY,
      appShellHeight: document.querySelector(".app-shell")?.getBoundingClientRect().height ?? null,
      toolbarClientWidth: toolbar?.clientWidth ?? null,
      toolbarScrollWidth: toolbar?.scrollWidth ?? null,
      sortControlWidth: sortControl?.getBoundingClientRect().width ?? null,
      visualDisplay: visuals ? getComputedStyle(visuals).display : null,
      visualHeight: visuals?.getBoundingClientRect().height ?? null,
      visualClientWidth: visuals?.clientWidth ?? null,
      visualScrollWidth: visuals?.scrollWidth ?? null,
      visualMaxCardHeight: visualCards.length ? Math.max(...visualCards.map((card) => card.getBoundingClientRect().height)) : null,
      visualCardHorizontalOverflow: visualCards.some((card) => card.scrollWidth > card.clientWidth + 1),
      workspaceTop: workspace?.getBoundingClientRect().top ?? null,
      workspaceHeight: workspace?.getBoundingClientRect().height ?? null,
      mainPanelWidth: mainPanel?.getBoundingClientRect().width ?? null,
      mainPanelHeight: mainPanel?.getBoundingClientRect().height ?? null,
      detailPanelWidth: detailPanel?.getBoundingClientRect().width ?? null,
      detailPanelPosition: detailStyle?.position ?? null,
      tableWrapClientWidth: wrap?.clientWidth ?? null,
      tableWrapScrollWidth: wrap?.scrollWidth ?? null,
      tableWrapClientHeight: wrap?.clientHeight ?? null,
      tableWrapScrollHeight: wrap?.scrollHeight ?? null,
      tableWidth: table?.getBoundingClientRect().width ?? null,
      headerVisibleInsideWrap: Boolean(
        wrapRect && headerRect && headerRect.left >= wrapRect.left - 1 && headerRect.right <= wrapRect.right + 1
      ),
      actionVisibleInsideWrap: firstActionCell
        ? Boolean(wrapRect && actionRect && actionRect.left >= wrapRect.left - 1 && actionRect.right <= wrapRect.right + 1)
        : true
    };
  });

  assertCondition(metrics.pageScrollWidth <= metrics.pageClientWidth + 1, `${label} page has no horizontal overflow`);
  assertCondition(
    metrics.toolbarScrollWidth === null || metrics.toolbarScrollWidth <= metrics.toolbarClientWidth + 1,
    `${label} toolbar has no internal horizontal overflow`
  );
    assertCondition(
      metrics.sortControlWidth === null || metrics.sortControlWidth >= 150,
      `${label} sort control remains usable width`
    );
    if (metrics.visualDisplay !== "none" && metrics.visualHeight !== null) {
      assertCondition(
        metrics.visualScrollWidth === null || metrics.visualScrollWidth <= metrics.visualClientWidth + 1,
        `${label} visual summary has no internal horizontal overflow`
      );
      assertCondition(!metrics.visualCardHorizontalOverflow, `${label} visual summary cards contain text horizontally`);
      if (label.startsWith("desktop")) {
        assertCondition(metrics.visualHeight <= 142, `${label} visual summary remains a compact strip`);
        assertCondition(metrics.visualMaxCardHeight <= 132, `${label} visual cards keep bounded height`);
      }
    }
  if (label.startsWith("desktop")) {
    await page.evaluate(() => {
      window.scrollTo(0, 1000);
    });
    const scrollY = await page.evaluate(() => window.scrollY);
    assertCondition(scrollY === 0, `${label} window does not expose page-level vertical scrolling`);
    assertCondition(
      metrics.appShellHeight === null || metrics.appShellHeight <= metrics.pageClientHeight + 1,
      `${label} app shell fits inside viewport height`
    );
    const minimumMainPanelWidth = label === "desktop-app" ? 980 : 1180;
    assertCondition(
      metrics.mainPanelWidth !== null && metrics.mainPanelWidth >= minimumMainPanelWidth,
      `${label} thread workspace keeps a wide primary table area`
    );
    const minimumTableHeight = label === "desktop" ? 700 : label === "desktop-short" ? 650 : 560;
    assertCondition(
      metrics.tableWrapClientHeight !== null && metrics.tableWrapClientHeight >= minimumTableHeight,
      `${label} thread table keeps enough vertical workspace`
    );
    assertCondition(
      metrics.detailPanelWidth === null,
      `${label} collapsed detail inspector does not reserve a right-side rail`
    );
  }
  assertCondition(metrics.tableWrapScrollWidth !== null, `${label} thread table wrapper exists`);
  assertCondition(
    metrics.tableWrapScrollWidth <= metrics.tableWrapClientWidth + 1,
    `${label} thread table has no internal horizontal overflow`
  );
  assertCondition(metrics.headerVisibleInsideWrap, `${label} action header is visible inside table viewport`);
  assertCondition(metrics.actionVisibleInsideWrap, `${label} first action cell is visible inside table viewport`);
  console.log(`ui-overflow metrics ${label}: ${JSON.stringify(metrics)}`);
}

async function verifyProjectGroupCollapse(page) {
  await page.setViewportSize({ width: 2048, height: 1152 });
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => window.localStorage.removeItem("codex-home-manager-collapsed-project-groups"));
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitForThreadIndex(page);
  await page.waitForSelector(".rail-section-toggle", { timeout: 15000 });

  const firstGroupToggle = page.locator(".rail-section-toggle").first();
  const beforeMetrics = await page.evaluate(() => ({
    workspaceProjectCount: document.querySelectorAll(".project-list .project.workspace_project").length,
    totalProjectRows: document.querySelectorAll(".project-list .project").length,
    toggleCount: document.querySelectorAll(".rail-section-toggle").length
  }));
  assertCondition(beforeMetrics.toggleCount > 0, "project rail exposes collapsible section headers");
  assertCondition(beforeMetrics.workspaceProjectCount > 0, "project rail has workspace project rows before collapse");

  await firstGroupToggle.click();
  await page.waitForFunction(() => document.querySelectorAll(".project-list .project.workspace_project").length === 0);
  const collapsedMetrics = await page.evaluate(() => ({
    workspaceProjectCount: document.querySelectorAll(".project-list .project.workspace_project").length,
    totalProjectRows: document.querySelectorAll(".project-list .project").length,
    collapsedToggleCount: document.querySelectorAll(".rail-section-toggle.collapsed").length,
    storedWorkspaceCollapsed: JSON.parse(window.localStorage.getItem("codex-home-manager-collapsed-project-groups") || "{}").workspace_project === true,
    pageClientWidth: document.documentElement.clientWidth,
    pageScrollWidth: document.documentElement.scrollWidth
  }));
  assertCondition(collapsedMetrics.workspaceProjectCount === 0, "project rail hides workspace project rows when the section is collapsed");
  assertCondition(collapsedMetrics.totalProjectRows < beforeMetrics.totalProjectRows, "project rail collapse reduces visible project rows");
  assertCondition(collapsedMetrics.collapsedToggleCount >= 1, "project rail marks collapsed section headers");
  assertCondition(collapsedMetrics.storedWorkspaceCollapsed, "project rail persists collapsed section state");
  assertCondition(collapsedMetrics.pageScrollWidth <= collapsedMetrics.pageClientWidth + 1, "project rail collapsed state has no horizontal page overflow");

  await firstGroupToggle.click();
  await page.waitForFunction(
    (expectedCount) => document.querySelectorAll(".project-list .project.workspace_project").length === expectedCount,
    beforeMetrics.workspaceProjectCount
  );
  const expandedMetrics = await page.evaluate(() => ({
    workspaceProjectCount: document.querySelectorAll(".project-list .project.workspace_project").length,
    storedWorkspaceCollapsed: JSON.parse(window.localStorage.getItem("codex-home-manager-collapsed-project-groups") || "{}").workspace_project === true
  }));
  assertCondition(expandedMetrics.workspaceProjectCount === beforeMetrics.workspaceProjectCount, "project rail restores workspace project rows when expanded");
  assertCondition(!expandedMetrics.storedWorkspaceCollapsed, "project rail persists expanded section state");
}

async function verifySubagentVisibleScope(page) {
  await page.setViewportSize({ width: 2048, height: 1152 });
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    window.localStorage.removeItem("codex-home-manager-detail-panel-width");
    window.localStorage.removeItem("codex-home-manager-detail-panel-width-v2");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitForThreadIndex(page, { requireRows: true });
  await openFirstThreadDetailPanel(page);
  await page.waitForTimeout(300);
  const selectedMetrics = await page.evaluate(() => {
    const mainPanel = document.querySelector(".main-panel");
    const detailPanel = document.querySelector(".detail-panel");
    const detailStyle = detailPanel ? getComputedStyle(detailPanel) : null;
    const detailRect = detailPanel?.getBoundingClientRect();
    return {
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      pageClientHeight: document.documentElement.clientHeight,
      mainPanelWidth: mainPanel?.getBoundingClientRect().width ?? null,
      detailPanelPosition: detailStyle?.position ?? null,
      detailPanelWidth: detailRect?.width ?? null,
      detailPanelHeight: detailRect?.height ?? null,
      detailPanelTop: detailRect?.top ?? null,
      detailPanelBottom: detailRect?.bottom ?? null,
      resizeHandleCount: document.querySelectorAll(".detail-resize-handle").length
    };
  });
  assertCondition(selectedMetrics.mainPanelWidth >= 1180, "expanded detail inspector keeps a wide primary table area");
  assertCondition(selectedMetrics.detailPanelPosition === "fixed", "expanded detail inspector does not consume table grid width");
  assertCondition(
    selectedMetrics.detailPanelWidth >= 820 && selectedMetrics.detailPanelWidth <= 860,
    "expanded detail inspector has a wide bounded desktop width"
  );
  assertCondition(
    selectedMetrics.detailPanelHeight >= selectedMetrics.pageClientHeight - 150,
    "expanded detail inspector uses most of the available viewport height"
  );
  assertCondition(
    selectedMetrics.detailPanelTop <= 120 && selectedMetrics.detailPanelBottom >= selectedMetrics.pageClientHeight - 20,
    "expanded detail inspector is anchored from the upper content area to the viewport bottom"
  );
  assertCondition(selectedMetrics.pageScrollWidth <= selectedMetrics.pageClientWidth + 1, "expanded detail inspector has no horizontal page overflow before resizing");
  assertCondition(selectedMetrics.resizeHandleCount === 1, "expanded detail inspector exposes one resize handle");
  const resizeHandle = page.locator(".detail-resize-handle");
  const handleBox = await resizeHandle.boundingBox();
  assertCondition(Boolean(handleBox), "expanded detail inspector resize handle is measurable");
  await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(handleBox.x - 220, handleBox.y + handleBox.height / 2, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(200);
  const resizedMetrics = await page.evaluate(() => {
    const mainPanel = document.querySelector(".main-panel");
    const detailPanel = document.querySelector(".detail-panel");
    const detailRect = detailPanel?.getBoundingClientRect();
    return {
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      mainPanelWidth: mainPanel?.getBoundingClientRect().width ?? null,
      detailPanelWidth: detailRect?.width ?? null,
      storedWidth: Number(window.localStorage.getItem("codex-home-manager-detail-panel-width-v2"))
    };
  });
  assertCondition(
    resizedMetrics.detailPanelWidth >= selectedMetrics.detailPanelWidth + 180,
    "expanded detail inspector widens when its left edge is dragged left"
  );
  assertCondition(
    Math.abs(resizedMetrics.mainPanelWidth - selectedMetrics.mainPanelWidth) <= 1,
    "resizing the inspector does not reflow the primary table"
  );
  assertCondition(resizedMetrics.pageScrollWidth <= resizedMetrics.pageClientWidth + 1, "resized detail inspector has no horizontal page overflow");
  assertCondition(resizedMetrics.storedWidth >= resizedMetrics.detailPanelWidth - 2, "resized detail inspector persists the chosen width");
  const collapseDetailButton = page.locator(".detail-panel .icon-button");
  assertCondition(await collapseDetailButton.count() === 1, "expanded detail inspector exposes a single collapse button");
  await collapseDetailButton.click();
  await page.waitForSelector(".detail-panel.expanded", { state: "detached", timeout: 10000 });
  await page.getByRole("button", { name: "子agent" }).click();
  const makeMoneyButton = page.locator("button.project").filter({ hasText: "MakeMoney" }).first();
  if (await makeMoneyButton.count()) {
    await makeMoneyButton.click();
  }
  await page.waitForTimeout(300);
  const metrics = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll(".thread-table tbody tr")).map((row) => row.textContent || "");
    const detailTitle = document.querySelector(".detail-heading h2")?.textContent || "";
    const activeSegments = Array.from(document.querySelectorAll(".segmented button.active")).map((button) => button.textContent || "");
    const wrap = document.querySelector(".table-wrap");
    const toolbar = document.querySelector(".toolbar");
    const sortControl = document.querySelector(".sort-control");
    const workspace = document.querySelector(".workspace");
    const mainPanel = document.querySelector(".main-panel");
    const detailPanel = document.querySelector(".detail-panel");
    const detailStyle = detailPanel ? getComputedStyle(detailPanel) : null;
    const makeMoneyProject = Array.from(document.querySelectorAll("button.project"))
      .find((button) => button.textContent?.includes("MakeMoney"));
    window.scrollTo(0, 1000);
    return {
      activeSegments,
      makeMoneyCountText: makeMoneyProject?.textContent || "",
      rowCount: rows.length,
      firstRows: rows.slice(0, 5),
      detailTitle,
      pageClientHeight: document.documentElement.clientHeight,
      pageScrollHeight: document.documentElement.scrollHeight,
      windowScrollYAfterScrollAttempt: window.scrollY,
      tableWrapClientWidth: wrap?.clientWidth ?? null,
      tableWrapScrollWidth: wrap?.scrollWidth ?? null,
      toolbarClientWidth: toolbar?.clientWidth ?? null,
      toolbarScrollWidth: toolbar?.scrollWidth ?? null,
      sortControlWidth: sortControl?.getBoundingClientRect().width ?? null,
      workspaceHeight: workspace?.getBoundingClientRect().height ?? null,
      mainPanelWidth: mainPanel?.getBoundingClientRect().width ?? null,
      mainPanelHeight: mainPanel?.getBoundingClientRect().height ?? null,
      tableWrapClientHeight: wrap?.clientHeight ?? null,
      detailPanelPosition: detailStyle?.position ?? null
    };
  });
  assertCondition(metrics.activeSegments.includes("子agent"), "subagent scope is active");
  assertCondition(metrics.activeSegments.includes("全部"), "all status filter remains active for subagents");
  assertCondition(metrics.rowCount > 0, "subagent visible scope returns active subagent rows");
  assertCondition(!metrics.detailTitle, "stale detail panel is cleared when selected thread leaves filtered results");
  assertCondition(metrics.windowScrollYAfterScrollAttempt === 0, "subagent desktop scope has no page-level vertical scrolling");
  assertCondition(
    metrics.tableWrapScrollWidth <= metrics.tableWrapClientWidth + 1,
    "subagent desktop scope table has no internal horizontal overflow"
  );
  assertCondition(
    metrics.toolbarScrollWidth <= metrics.toolbarClientWidth + 1,
    "subagent desktop scope toolbar has no internal horizontal overflow"
  );
  assertCondition(metrics.sortControlWidth >= 150, "subagent desktop scope sort control remains usable width");
  assertCondition(metrics.mainPanelWidth >= 1180, "subagent desktop scope keeps a wide primary table area");
  assertCondition(metrics.tableWrapClientHeight >= 650, "subagent desktop scope keeps enough vertical workspace");
  console.log(`ui-subagent metrics: ${JSON.stringify(metrics)}`);
}

async function verifyCompactDetailPanelLayout(page) {
  await page.setViewportSize({ width: 2048, height: 520 });
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    window.localStorage.removeItem("codex-home-manager-detail-panel-width");
    window.localStorage.removeItem("codex-home-manager-detail-panel-width-v2");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitForThreadIndex(page, { requireRows: true });
  await openFirstThreadDetailPanel(page);
  await page.waitForTimeout(300);
  const metrics = await page.evaluate(() => {
    const workspace = document.querySelector(".workspace");
    const detailPanel = document.querySelector(".detail-panel.expanded");
    const tableWrap = document.querySelector(".table-wrap");
    const detailRect = detailPanel?.getBoundingClientRect();
    const workspaceRect = workspace?.getBoundingClientRect();
    const tableRect = tableWrap?.getBoundingClientRect();
    return {
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      pageClientHeight: document.documentElement.clientHeight,
      workspaceHeight: workspaceRect?.height ?? null,
      tableWrapHeight: tableRect?.height ?? null,
      detailPanelPosition: detailPanel ? getComputedStyle(detailPanel).position : null,
      detailPanelTop: detailRect?.top ?? null,
      detailPanelBottom: detailRect?.bottom ?? null,
      detailPanelHeight: detailRect?.height ?? null,
      detailPanelWidth: detailRect?.width ?? null
    };
  });
  assertCondition(metrics.pageScrollWidth <= metrics.pageClientWidth + 1, "compact detail layout has no horizontal page overflow");
  assertCondition(metrics.detailPanelPosition === "fixed", "compact detail inspector is viewport anchored");
  assertCondition(metrics.detailPanelWidth >= 820 && metrics.detailPanelWidth <= 860, "compact detail inspector keeps usable width");
  assertCondition(metrics.detailPanelTop <= 120, "compact detail inspector starts near the top content area");
  assertCondition(metrics.detailPanelBottom >= metrics.pageClientHeight - 20, "compact detail inspector reaches viewport bottom");
  assertCondition(metrics.detailPanelHeight >= 360, "compact detail inspector keeps enough vertical reading area");
  assertCondition(
    metrics.workspaceHeight === null || metrics.detailPanelHeight >= metrics.workspaceHeight + 80,
    "compact detail inspector is no longer capped by table header/result-strip height"
  );
  console.log(`ui-compact-detail metrics: ${JSON.stringify(metrics)}`);
}

async function verifyRowActionClickability(page) {
  await page.setViewportSize({ width: 2048, height: 1152 });
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await waitForThreadIndex(page);
  await page.getByRole("button", { name: "可见" }).click();
  await page.waitForSelector(".row-action-button", { timeout: 15000 });
  const buttonMetrics = await page.evaluate(() => {
    const legacyActionButton = Array.from(document.querySelectorAll(".row-action-button"))
      .find((button) => button.textContent?.includes("提到首轮"));
    const legacyStatusButton = Array.from(document.querySelectorAll("button"))
      .find((button) => button.textContent?.includes("首轮外"));
    const actionButton = Array.from(document.querySelectorAll(".row-action-button"))
      .find((button) => button.textContent?.trim() === "隐藏");
    const actionCell = actionButton?.closest("td");
    const buttonRect = actionButton?.getBoundingClientRect();
    const cellRect = actionCell?.getBoundingClientRect();
    return {
      exists: Boolean(actionButton),
      legacyActionExists: Boolean(legacyActionButton),
      legacyStatusExists: Boolean(legacyStatusButton),
      text: actionButton?.textContent || "",
      buttonWidth: buttonRect?.width ?? 0,
      buttonHeight: buttonRect?.height ?? 0,
      insideCell: Boolean(
        buttonRect && cellRect && buttonRect.left >= cellRect.left - 1 && buttonRect.right <= cellRect.right + 1
      )
    };
  });
  assertCondition(!buttonMetrics.legacyActionExists, "legacy first-page row action is absent");
  assertCondition(!buttonMetrics.legacyStatusExists, "legacy first-page status filter is absent");
  assertCondition(buttonMetrics.exists, "visible row hide action button exists");
  assertCondition(buttonMetrics.text.includes("隐藏"), "visible row hide action has visible label");
  assertCondition(buttonMetrics.buttonWidth >= 60 && buttonMetrics.buttonHeight >= 30, "visible row hide action has a practical click target");
  assertCondition(buttonMetrics.insideCell, "visible row hide action stays inside operation cell");

  await page.getByRole("button", { name: "全部" }).nth(1).click();
  const allFilterButtonMetrics = await page.evaluate(() => {
    const legacyActionButton = Array.from(document.querySelectorAll(".row-action-button"))
      .find((button) => button.textContent?.includes("提到首轮"));
    const actionButton = Array.from(document.querySelectorAll(".row-action-button"))
      .find((button) => button.textContent?.trim() === "隐藏");
    return {
      exists: Boolean(actionButton),
      legacyActionExists: Boolean(legacyActionButton),
      text: actionButton?.textContent || "",
    };
  });
  assertCondition(!allFilterButtonMetrics.legacyActionExists, "legacy first-page row action stays absent under all filter");
  assertCondition(allFilterButtonMetrics.exists, "visible row hide action also exists under the all status filter");
  assertCondition(allFilterButtonMetrics.text.includes("隐藏"), "visible row hide action keeps its label under the all status filter");
  console.log(`ui-row-action metrics: ${JSON.stringify(buttonMetrics)}`);
}

async function verifyLogModalLayout(page) {
  await page.setViewportSize({ width: 2048, height: 1152 });
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await waitForThreadIndex(page, { requireRows: true });
  await openFirstThreadDetailPanel(page);
  await page.getByRole("button", { name: "详细日志" }).click();
  await page.waitForSelector(".log-modal", { timeout: 15000 });
  await page.locator(".log-controls select").first().selectOption("app");
  await page.locator(".log-controls select").nth(1).selectOption("error");
  await page.waitForSelector(".log-list", { timeout: 15000 });
  await page.waitForFunction(() => (document.querySelector(".log-list")?.clientHeight || 0) > 0, { timeout: 15000 });
  await page.waitForTimeout(300);
  const metrics = await page.evaluate(() => {
    const modal = document.querySelector(".log-modal");
    const controls = document.querySelector(".log-controls");
    const summary = document.querySelector(".log-summary");
    const list = document.querySelector(".log-list");
    const modalRect = modal?.getBoundingClientRect();
    const controlsRect = controls?.getBoundingClientRect();
    const summaryRect = summary?.getBoundingClientRect();
    const listRect = list?.getBoundingClientRect();
    const codes = Array.from(document.querySelectorAll(".log-summary code")).map((element) => ({
      text: element.textContent || "",
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
      left: element.getBoundingClientRect().left,
      right: element.getBoundingClientRect().right,
      parentLeft: element.parentElement?.getBoundingClientRect().left ?? 0,
      parentRight: element.parentElement?.getBoundingClientRect().right ?? 0
    }));
    window.scrollTo(0, 1000);
    return {
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      windowScrollYAfterScrollAttempt: window.scrollY,
      modalExists: Boolean(modal),
      modalWidth: modalRect?.width ?? 0,
      modalHeight: modalRect?.height ?? 0,
      modalLeft: modalRect?.left ?? 0,
      modalRight: modalRect?.right ?? 0,
      controlsScrollWidth: controls?.scrollWidth ?? 0,
      controlsClientWidth: controls?.clientWidth ?? 0,
      summaryScrollWidth: summary?.scrollWidth ?? 0,
      summaryClientWidth: summary?.clientWidth ?? 0,
      listClientHeight: list?.clientHeight ?? 0,
      listScrollHeight: list?.scrollHeight ?? 0,
      controlsAboveSummary: Boolean(controlsRect && summaryRect && controlsRect.bottom <= summaryRect.top + 1),
      summaryAboveList: Boolean(summaryRect && listRect && summaryRect.bottom <= listRect.top + 1),
      codes
    };
  });
  assertCondition(metrics.modalExists, "log modal opens from thread detail");
  assertCondition(metrics.pageScrollWidth <= metrics.pageClientWidth + 1, "log modal page has no horizontal overflow");
  assertCondition(metrics.windowScrollYAfterScrollAttempt === 0, "log modal keeps window vertical scroll locked");
  assertCondition(metrics.modalLeft >= 0 && metrics.modalRight <= metrics.pageClientWidth + 1, "log modal stays inside viewport horizontally");
  assertCondition(metrics.modalWidth >= 760 && metrics.modalHeight >= 620, "log modal has enough desktop reading area");
  assertCondition(metrics.controlsScrollWidth <= metrics.controlsClientWidth + 1, "log modal controls have no horizontal overflow");
  assertCondition(metrics.summaryScrollWidth <= metrics.summaryClientWidth + 1, "log modal summary has no horizontal overflow");
  assertCondition(metrics.listClientHeight >= 340, "log modal keeps a substantial scrollable log list");
  assertCondition(metrics.controlsAboveSummary, "log modal controls do not overlap summary");
  assertCondition(metrics.summaryAboveList, "log modal summary does not overlap log list");
  for (const [index, code] of metrics.codes.entries()) {
    assertCondition(code.clientWidth >= 120, `log modal path code ${index} keeps readable width`);
    assertCondition(code.left >= code.parentLeft - 1 && code.right <= code.parentRight + 1, `log modal path code ${index} stays inside summary`);
  }
  await page.getByTitle("关闭日志窗口").click();
  await page.waitForSelector(".log-modal", { state: "detached", timeout: 10000 });
  console.log(`ui-log-modal metrics: ${JSON.stringify(metrics)}`);
}

function rectanglesOverlap(first, second) {
  const left = Math.max(first.left, second.left);
  const right = Math.min(first.right, second.right);
  const top = Math.max(first.top, second.top);
  const bottom = Math.min(first.bottom, second.bottom);
  return right - left > 1 && bottom - top > 1;
}

async function verifyDiagnosticsPageLayout(page, viewport, label) {
  await page.setViewportSize(viewport);
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: /体检|Diagnostics/ }).click();
  await waitForDiagnosticsPage(page);
  const promptSummary = page.locator(".diagnostics-prompt-preview summary");
  await promptSummary.click();
  await page.waitForFunction(() => document.querySelector(".diagnostics-prompt-preview")?.hasAttribute("open"));
  await page.locator(".diagnostics-title h2").click();
  await page.waitForFunction(() => !document.querySelector(".diagnostics-prompt-preview")?.hasAttribute("open"));
  await promptSummary.click();
  await page.waitForFunction(() => document.querySelector(".diagnostics-prompt-preview")?.hasAttribute("open"));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".diagnostics-prompt-preview")?.hasAttribute("open"));
  await page.waitForTimeout(300);
  const metrics = await page.evaluate(() => {
    const selectors = [".diagnostics-summary-strip", ".diagnostics-grid", ".diagnostics-issues-panel", ".diagnostics-checks-panel"];
    const blocks = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)).map((element, index) => {
      const rect = element.getBoundingClientRect();
      return {
        selector: `${selector}[${index}]`,
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height,
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
        scrollHeight: element.scrollHeight,
        clientHeight: element.clientHeight,
        overflowY: getComputedStyle(element).overflowY
      };
    }));
    const codeBlocks = Array.from(document.querySelectorAll(".diagnostic-card code, .diagnostic-check code, .diagnostics-summary-strip code"))
      .filter((element) => element.getClientRects().length > 0)
      .slice(0, 24)
      .map((element, index) => {
      const rect = element.getBoundingClientRect();
      const parentRect = element.parentElement?.getBoundingClientRect();
      const style = getComputedStyle(element);
      return {
        index,
        summaryStrip: Boolean(element.closest(".diagnostics-summary-strip")),
        left: rect.left,
        right: rect.right,
        parentLeft: parentRect?.left ?? 0,
        parentRight: parentRect?.right ?? 0,
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
        overflowX: style.overflowX,
        textOverflow: style.textOverflow,
        whiteSpace: style.whiteSpace
      };
    });
    const diagnosticCardRects = Array.from(document.querySelectorAll(".diagnostic-card")).slice(0, 8).map((element, index) => {
      const rect = element.getBoundingClientRect();
      return {
        index,
        top: rect.top,
        bottom: rect.bottom,
        height: rect.height
      };
    });
    window.scrollTo(0, 1000);
    return {
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      windowScrollYAfterScrollAttempt: window.scrollY,
      diagnosticsCards: document.querySelectorAll(".diagnostic-card").length,
      diagnosticsChecks: document.querySelectorAll(".diagnostic-check").length,
      blocks,
      codeBlocks,
      diagnosticCardRects
    };
  });
  console.log(`ui-diagnostics raw metrics ${label}: ${JSON.stringify(metrics)}`);
  assertCondition(metrics.diagnosticsChecks > 0, `${label} diagnostics page renders check rows`);
  assertCondition(metrics.diagnosticsCards > 0, `${label} diagnostics page renders issue cards`);
  assertCondition(metrics.pageScrollWidth <= metrics.pageClientWidth + 1, `${label} diagnostics page has no horizontal overflow`);
  if (viewport.width > 820) {
    assertCondition(metrics.windowScrollYAfterScrollAttempt === 0, `${label} diagnostics page keeps window vertical scroll locked`);
    const panelBlocks = metrics.blocks.filter((block) => block.selector.includes("diagnostics-issues-panel") || block.selector.includes("diagnostics-checks-panel"));
    const issuesBlock = metrics.blocks.find((block) => block.selector.includes("diagnostics-issues-panel"));
    const checksBlock = metrics.blocks.find((block) => block.selector.includes("diagnostics-checks-panel"));
    const shortestPanelHeight = Math.min(...panelBlocks.map((block) => block.clientHeight));
    const shortestPanelBottom = Math.min(...panelBlocks.map((block) => block.bottom));
    const gridBlock = metrics.blocks.find((block) => block.selector.startsWith(".diagnostics-grid"));
    assertCondition(
      gridBlock && gridBlock.top <= Math.floor(viewport.height * 0.28),
      `${label} diagnostics primary panels start high enough in the viewport`
    );
    assertCondition(
      issuesBlock && checksBlock && issuesBlock.width > checksBlock.width * 1.45,
      `${label} diagnostics uses a wide expanded issue pane with a narrower checks pane`
    );
    const tallestVisibleIssueCard = Math.max(...metrics.diagnosticCardRects.map((block) => block.height));
    assertCondition(
      tallestVisibleIssueCard <= 280,
      `${label} diagnostics issue cards stay compact by default`
    );
    assertCondition(
      shortestPanelHeight >= Math.floor(viewport.height * 0.70),
      `${label} diagnostics panels keep enough vertical workspace`
    );
    assertCondition(
      shortestPanelBottom >= Math.floor(viewport.height * 0.98),
      `${label} diagnostics panels reach the viewport bottom without clipping`
    );
  }
  for (const block of metrics.blocks) {
    assertCondition(block.scrollWidth <= block.clientWidth + 1, `${label} ${block.selector} has no internal horizontal overflow`);
    if ((block.selector.includes("diagnostics-issues-panel") || block.selector.includes("diagnostics-checks-panel")) && block.scrollHeight > block.clientHeight + 1) {
      assertCondition(
        ["auto", "scroll"].includes(block.overflowY),
        `${label} ${block.selector} exposes vertical scrolling instead of clipping`
      );
    }
  }
  for (const codeBlock of metrics.codeBlocks) {
    assertCondition(
      codeBlock.left >= codeBlock.parentLeft - 1 && codeBlock.right <= codeBlock.parentRight + 1,
      `${label} diagnostics code ${codeBlock.index} stays inside parent`
    );
    if (codeBlock.summaryStrip) {
      assertCondition(
        codeBlock.overflowX === "hidden" && codeBlock.textOverflow === "ellipsis" && codeBlock.whiteSpace === "nowrap",
        `${label} diagnostics summary code ${codeBlock.index} truncates long paths safely`
      );
    } else {
      assertCondition(
        codeBlock.scrollWidth <= codeBlock.clientWidth + 1,
        `${label} diagnostics code ${codeBlock.index} wraps long content`
      );
    }
  }
  console.log(`ui-diagnostics metrics ${label}: ${JSON.stringify(metrics)}`);
}

async function verifyApiPageLayout(page, viewport, label) {
  await page.setViewportSize(viewport);
  await page.goto(serviceUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "API" }).click();
  await page.waitForSelector(".api-hero pre", { timeout: 15000 });
  await page.waitForTimeout(300);
  const metrics = await page.evaluate(() => {
    const selectors = [".section-headline", ".api-hero", ".api-safety", ".api-examples", ".capability-list"];
    const blocks = selectors.map((selector) => {
      const element = document.querySelector(selector);
      const rect = element?.getBoundingClientRect();
      return {
        selector,
        exists: Boolean(element),
        left: rect?.left ?? 0,
        right: rect?.right ?? 0,
        top: rect?.top ?? 0,
        bottom: rect?.bottom ?? 0,
        width: rect?.width ?? 0,
        height: rect?.height ?? 0,
        scrollWidth: element?.scrollWidth ?? 0,
        clientWidth: element?.clientWidth ?? 0,
        scrollHeight: element?.scrollHeight ?? 0,
        clientHeight: element?.clientHeight ?? 0
      };
    });
    const preBlocks = Array.from(document.querySelectorAll("pre")).map((element, index) => {
      const rect = element.getBoundingClientRect();
      const parentRect = element.parentElement?.getBoundingClientRect();
      return {
        index,
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        parentLeft: parentRect?.left ?? 0,
        parentRight: parentRect?.right ?? 0,
        parentTop: parentRect?.top ?? 0,
        parentBottom: parentRect?.bottom ?? 0,
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth
      };
    });
    const apiHeroText = document.querySelector(".api-hero > div")?.getBoundingClientRect();
    const apiHeroPre = document.querySelector(".api-hero pre")?.getBoundingClientRect();
    window.scrollTo(0, 1000);
    return {
      pageClientWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      pageClientHeight: document.documentElement.clientHeight,
      pageScrollHeight: document.documentElement.scrollHeight,
      windowScrollYAfterScrollAttempt: window.scrollY,
      bodyOverflowY: getComputedStyle(document.body).overflowY,
      blocks,
      preBlocks,
      apiHeroText: apiHeroText ? {
        left: apiHeroText.left,
        right: apiHeroText.right,
        top: apiHeroText.top,
        bottom: apiHeroText.bottom
      } : null,
      apiHeroPre: apiHeroPre ? {
        left: apiHeroPre.left,
        right: apiHeroPre.right,
        top: apiHeroPre.top,
        bottom: apiHeroPre.bottom
      } : null
    };
  });
  for (const block of metrics.blocks) {
    assertCondition(block.exists, `${label} ${block.selector} exists`);
    assertCondition(block.scrollWidth <= block.clientWidth + 1, `${label} ${block.selector} has no internal horizontal overflow`);
    assertCondition(block.scrollHeight <= block.clientHeight + 1, `${label} ${block.selector} contains its children vertically`);
  }
  const verticalBlocks = metrics.blocks.filter((block) => block.exists && block.height > 0);
  for (let index = 0; index < verticalBlocks.length - 1; index += 1) {
    assertCondition(
      verticalBlocks[index].bottom <= verticalBlocks[index + 1].top + 1,
      `${label} ${verticalBlocks[index].selector} does not overlap ${verticalBlocks[index + 1].selector}`
    );
  }
  for (const preBlock of metrics.preBlocks) {
    assertCondition(
      preBlock.left >= preBlock.parentLeft - 1 && preBlock.right <= preBlock.parentRight + 1,
      `${label} pre ${preBlock.index} stays inside parent card`
    );
    assertCondition(
      preBlock.top >= preBlock.parentTop - 1 && preBlock.bottom <= preBlock.parentBottom + 1,
      `${label} pre ${preBlock.index} stays vertically inside parent card`
    );
  }
  if (metrics.apiHeroText && metrics.apiHeroPre) {
    assertCondition(
      !rectanglesOverlap(metrics.apiHeroText, metrics.apiHeroPre),
      `${label} API hero text and code block do not overlap`
    );
  }
  assertCondition(metrics.pageScrollWidth <= metrics.pageClientWidth + 1, `${label} API page has no horizontal overflow`);
  assertCondition(metrics.windowScrollYAfterScrollAttempt === 0, `${label} API page does not expose window vertical scrolling`);
  console.log(`ui-api metrics ${label}: ${JSON.stringify(metrics)}`);
}

let uiExitCode = 0;
try {
  const page = await browser.newPage();
  await page.addInitScript((apiBaseUrl) => {
    window.localStorage.setItem("codex-home-manager-api-base-url", apiBaseUrl);
  }, new URL(serviceUrl).origin);
  await verifyLanguageSwitching(page);
  await measureViewport(page, { width: 2048, height: 1200 }, "desktop");
  await measureViewport(page, { width: 2048, height: 1152 }, "desktop-short");
  await measureViewport(page, { width: 1707, height: 1067 }, "desktop-app");
  await measureViewport(page, { width: 390, height: 844 }, "mobile");
  await verifyTableSortingAndFullDetail(page);
  await verifyProjectGroupCollapse(page);
  await verifySubagentVisibleScope(page);
  await verifyCompactDetailPanelLayout(page);
  await verifyRowActionClickability(page);
  await verifyLogModalLayout(page);
  await verifyDiagnosticsPageLayout(page, { width: 2048, height: 1152 }, "diagnostics-desktop");
  await verifyDiagnosticsPageLayout(page, { width: 390, height: 844 }, "diagnostics-mobile");
  await verifyApiPageLayout(page, { width: 2048, height: 1152 }, "api-desktop");
  await verifyApiPageLayout(page, { width: 1707, height: 1067 }, "api-app");
} catch (error) {
  uiExitCode = 1;
  console.error(error instanceof Error ? error.stack || error.message : String(error));
} finally {
  // On Windows, Chromium can exit while Playwright's close pipe remains pending.
  void browser.close().catch(() => {});
  setTimeout(() => process.exit(uiExitCode), 25);
}
