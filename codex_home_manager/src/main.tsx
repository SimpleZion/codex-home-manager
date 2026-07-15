import React from "react";
import { createRoot } from "react-dom/client";
import {
  Archive,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Code2,
  Copy,
  Database,
  Download,
  Eye,
  EyeOff,
  FileJson,
  FileText,
  Folder,
  FolderInput,
  FolderPen,
  ExternalLink,
  Import,
  Languages,
  Layers,
  MoveRight,
  PanelRightClose,
  PanelRightOpen,
  RefreshCcw,
  RotateCcw,
  Save,
  Search,
  ShieldCheck,
  Scissors,
  ServerCog,
  Sparkles,
  Trash2,
  Wrench,
  X
} from "lucide-react";
import "./styles.css";
import {
  exportBrowserThreadPrompts,
  pickBrowserCodexDirectory,
  readBrowserResource,
  readBrowserThreadDetail,
  readBrowserThreadDailyTokenUsage,
  readBrowserThreadLogs,
  readBrowserThreadPrompts,
  scanBrowserCodexHome,
  supportsBrowserFolderMode,
  type BrowserCodexWorkspace
} from "./browserHome";

type Visibility =
  | "visible"
  | "hidden_by_initial_limit"
  | "archived"
  | "needs_user_event_repair"
  | "missing_file"
  | "subagent"
  | "hidden";

type ThreadRecord = {
  id: string;
  title: string;
  sqliteTitle: string;
  sidebarTitle: string;
  sessionIndexTitle: string;
  sessionIndexUpdatedAt: string;
  rolloutTitle: string;
  rolloutTitleTimestamp: string;
  rolloutTitleLine: number | null;
  preview: string;
  projectPath: string;
  projectLabel: string;
  projectKind: ProjectKind;
  rolloutPath: string;
  source: string;
  threadKind: "main" | "subagent";
  threadSource: string;
  parentThreadId: string;
  subagentStatus: string;
  agentNickname: string;
  agentRole: string;
  model: string;
  createdAtMs: number;
  updatedAtMs: number;
  archived: boolean;
  archivedAtMs: number | null;
  hasUserEvent: boolean;
  hasUserSignal: boolean;
  tokensUsed: number;
  childTokensUsed: number;
  totalTokensUsed: number;
  fileExists: boolean;
  fileSizeBytes: number;
  childThreadCount: number;
  childFileSizeBytes: number;
  totalFileSizeBytes: number;
  fileModifiedAtMs: number | null;
  rolloutInArchivedStore: boolean;
  recentRank: number | null;
  threadListRank: number | null;
  sessionIndexRank: number | null;
  isPinned: boolean;
  explicitSidebarReference: boolean;
  inInitialSidebarPage: boolean;
  outsideInitialLimit: boolean;
  codexVisible: boolean;
  visibility: Visibility;
  hiddenReasons: string[];
  rolloutDisplayStatus?: string;
  rolloutDisplayResponseUserMessages?: number;
  rolloutDisplayResponseAssistantMessages?: number;
  rolloutDisplayVisibleUserMessages?: number;
  rolloutDisplayVisibleAgentMessages?: number;
  rolloutDisplayEventUserMessages?: number;
  rolloutDisplayEventAgentMessages?: number;
  gitBranch: string;
  cliVersion: string;
};

type ProjectKind = "workspace_project" | "conversation" | "other";

type ProjectRecord = {
  path: string;
  label: string;
  projectKind: ProjectKind;
  total: number;
  mainThreads: number;
  subagentThreads: number;
  active: number;
  visible: number;
  hiddenByInitialLimit: number;
  archived: number;
  needsRepair: number;
  storageBytes: number;
  emptyButHasHiddenThreads: boolean;
};

type Snapshot = {
  codexHome: string;
  databasePath: string;
  globalStatePath: string;
  sessionIndexPath: string;
  sidebarLimit: number;
  summary: {
    totalThreads: number;
    mainThreads: number;
    subagentThreads: number;
    eligibleThreads: number;
    codexVisibleThreads: number;
    hiddenByInitialLimit: number;
    archivedThreads: number;
    needsRepairThreads: number;
    savedProjects: number;
    workspaceProjects: number;
    conversationProjects: number;
    otherProjects: number;
    emptyProjectsWithHiddenThreads: number;
    totalStorageBytes: number;
  };
  threads: ThreadRecord[];
  projects: ProjectRecord[];
  generatedAtMs: number;
};

type BackupRecord = {
  backupId: string;
  createdAt: string;
  action: string;
  threadId: string | null;
  manifestPath: string;
};

type ThreadDetail = {
  thread: ThreadRecord;
  sqliteRow: Record<string, unknown>;
  rolloutStats: {
    lineCount: number;
    userMessages: number;
    assistantMessages: number;
    toolCalls: number;
    toolOutputs: number;
    eventMessages: number;
    invalidJsonLines: number;
    firstTimestamp: string | null;
    lastTimestamp: string | null;
  };
  dailyTokenUsage?: ThreadDailyTokenUsage;
  backups: BackupRecord[];
};

type ThreadDailyTokenUsage = {
  summary: {
    ownTokens: number;
    childTokens: number;
    totalTokens: number;
    days: number;
    activeDays?: number;
    rangeDays?: number;
    zeroDays?: number;
    unknownDays?: number;
    firstDate: string | null;
    lastDate: string | null;
    peakDate: string | null;
    peakTokens: number;
    ownTokenEvents: number;
    childTokenEvents: number;
    ownCountedTokenEvents: number;
    childCountedTokenEvents: number;
    zeroDeltaTokenEvents: number;
    fallbackTokenEvents: number;
    ownUnknownTokenThreads?: number;
    childUnknownTokenThreads?: number;
    unknownTokenThreads?: number;
    childThreadCount: number;
    missingChildRolloutFiles: number;
  };
  days: Array<{
    date: string;
    ownTokens: number;
    childTokens: number;
    totalTokens: number;
    ownTokenEvents: number;
    childTokenEvents: number;
    ownUnknownTokenThreads?: number;
    childUnknownTokenThreads?: number;
    unknownTokenThreads?: number;
    hasData?: boolean;
    hasUnknownTokens?: boolean;
  }>;
};

type ThreadLogEntry = {
  source: "rollout_jsonl" | "app_sqlite" | string;
  lineNumber: number | null;
  appLogId: number | null;
  timestamp: string | null;
  timestampMs: number | null;
  type: string;
  payloadType: string;
  role: string;
  kind: string;
  label: string;
  severity: "info" | "warning" | "error";
  message: string;
  messageTruncated: boolean;
  rawLine: string;
  rawLineTruncated: boolean;
  level?: string;
  target?: string;
  modulePath?: string;
  file?: string;
  fileLine?: number | null;
  processUuid?: string;
};

type ThreadLogs = {
  threadId: string;
  source: "all" | "rollout" | "app" | string;
  rolloutPath: string;
  appLogPath: string;
  offset: number;
  limit: number;
  kind: string;
  search: string;
  matchedEntries: number;
  hasMore: boolean;
  entries: ThreadLogEntry[];
  summary: {
    lineCount: number;
    parseErrors: number;
    byKind: Record<string, number>;
    bySeverity: Record<string, number>;
  };
};

type PromptRecord = {
  index: number;
  lineNumber: number;
  timestamp: string | null;
  text: string;
  characterCount: number;
  sourceType?: string;
  sourceLabel?: string;
  visibleByDefault?: boolean;
  pureText?: string;
  pureCharacterCount?: number;
  hasPureText?: boolean;
};

type ThreadPrompts = {
  threadId: string;
  title?: string | null;
  rolloutPath: string;
  promptCount: number;
  purePromptCount?: number;
  visiblePromptCount?: number;
  hiddenPromptCount?: number;
  sourceCounts?: Record<string, number>;
  prompts: PromptRecord[];
};

type ResourceRecord = {
  relativePath: string;
  path: string;
  label: string;
  category: string;
  description: string;
  exists: boolean;
  kind: "file" | "directory" | "missing";
  sizeBytes: number;
  fileCount: number;
  directoryCount: number;
  truncated: boolean;
  modifiedAtMs: number | null;
};

type HomeOverview = {
  codexHome: string;
  resources: ResourceRecord[];
  summary: {
    resourceCount: number;
    existingResourceCount: number;
    totalKnownResourceBytes: number;
    agentsFileCount: number;
    memoryExists: boolean;
    skillsExists: boolean;
  };
  generatedAtMs: number;
};

type ResourceRead = {
  metadata: ResourceRecord;
  content: string | null;
  children?: ResourceRecord[];
  truncated?: boolean;
  binary?: boolean;
};

type CapabilityRecord = {
  name: string;
  method: string;
  path: string;
  purpose: string;
  required: string[];
  backup: string;
  bodyExample?: Record<string, unknown> | null;
  successFields: string[];
  rollback?: string | null;
  riskLevel?: string;
  previewEndpoint?: string | null;
  writeEndpoint?: string | null;
  idempotency?: string;
  rollbackMode?: string | null;
};

type CapabilityResponse = {
  service: string;
  version: string;
  language: Language;
  openapiPath: string;
  mcpPath: string;
  safetyModel: Record<string, unknown>;
  commonQueryParameters: Record<string, string>;
  capabilities: CapabilityRecord[];
};

type CodexCliVersionInfo = {
  path?: string;
  exists?: boolean;
  version?: string;
  raw?: string;
  error?: string;
};

type CodexCurrentVersions = {
  configuredCli?: CodexCliVersionInfo;
  runtimeConfiguredCli?: CodexCliVersionInfo;
  pathCli?: CodexCliVersionInfo;
  desktopInstall?: {
    version?: string;
    path?: string;
    modifiedAtMs?: number;
  };
  versionCache?: Record<string, unknown>;
};

type HealthPayload = {
  writeWarnings?: string[];
  currentVersions?: CodexCurrentVersions;
};

type DiagnosticSeverity = "critical" | "warning" | "info" | "pass";

type DiagnosticIssue = {
  id: string;
  severity: DiagnosticSeverity;
  category: string;
  title: string;
  summary: string;
  recommendation: string;
  evidence: string[];
  affectedPaths: string[];
  fixCommand?: string | null;
};

type DiagnosticCheck = {
  id: string;
  category: string;
  title: string;
  status: DiagnosticSeverity;
  summary: string;
  evidence: string[];
  affectedPaths: string[];
};

type DiagnosticDetailTarget =
  | { kind: "issue"; item: DiagnosticIssue }
  | { kind: "check"; item: DiagnosticCheck };

type CapacityTrendDirection = "up" | "down" | "flat" | "unknown";

type CapacityTrendChange = {
  direction: CapacityTrendDirection;
  delta: number;
  percent: number | null;
};

type CapacityTrendMetrics = {
  sessionsBytes: number;
  largeThreadCount: number;
  backupBytes: number;
  backupFileCount: number;
  backupScanTruncated: boolean;
  mcpProcessCount: number;
  normalNodeReplProcessCount: number;
  nodeReplRiskProcessCount: number;
  legacyFallbackProcessCount: number;
  xcodebuildProcessCount: number;
  otherMcpServerProcessCount: number;
};

type CapacityTrendSnapshot = Partial<CapacityTrendMetrics> & {
  capturedAtMs: number;
};

type CapacityTrend = {
  schemaVersion: number;
  retention: {
    cadence: "daily";
    maxAgeDays: number;
    maxSnapshots: number;
  };
  storage: {
    persisted: boolean;
    recoveredFromCorruption: boolean;
    errorCode?: string;
  };
  current: CapacityTrendMetrics;
  changes: Record<"sessionsBytes" | "largeThreadCount" | "backupBytes" | "backupFileCount" | "mcpProcessCount", CapacityTrendChange>;
  history: CapacityTrendSnapshot[];
};

type DiagnosticsReport = {
  codexHome: string;
  generatedAtMs: number;
  score: number;
  status: DiagnosticSeverity;
  summary: {
    critical: number;
    warning: number;
    info: number;
    pass: number;
    checks: number;
    issues: number;
    threadCount: number | null;
  };
  paths: Record<string, string>;
  codexProcesses: Array<Record<string, unknown>>;
  capacityTrend?: CapacityTrend;
  checks: DiagnosticCheck[];
  issues: DiagnosticIssue[];
  topRecommendations: string[];
  repairHints: Record<string, string>;
  repairPrompt?: string;
};

type SlimPreview = {
  operationPreviewId?: string;
  inputHash?: string;
  expiresAtMs?: number;
  scan: {
    lineCount: number;
    parseErrors: number;
    compactedCount: number;
    embeddedImageRefs: number;
    embeddedImageUrlFields?: number;
    invalidImageUrlRefs?: number;
    encryptedContentFields?: number;
    totalBytes: number;
  };
  canRemoveImages: boolean;
  canReduceCompacted: boolean;
  warnings: string[];
};

type ImpactPreview = {
  operationPreviewId?: string;
  inputHash?: string;
  expiresAtMs?: number;
  action?: string;
  threadId?: string;
  backupId?: string;
  sourceThreadId?: string;
  targetThreadId?: string;
  preservesThreadId?: boolean;
  sourceRolloutPath?: string;
  matchedThreads?: number;
  existingRollouts?: number;
  rolloutBytes?: number;
  willRenameFolder?: boolean;
  requiresCodexClosed?: boolean;
  blockedByRunningCodex?: boolean;
  willOverwrite?: boolean;
  source?: { path?: string; kind?: string; sizeBytes?: number; exists?: boolean };
  target?: { path?: string; kind?: string; sizeBytes?: number; exists?: boolean };
  warnings?: string[];
};

type AuthToken = {
  token: string;
  headerName: string;
  expiresAtMs: number | null;
};

class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type FilterMode = "all" | "visible" | "manual_hidden" | "repair" | "archived";
type ThreadKindFilter = "main" | "all" | "subagent";
type SortMode = "status" | "rank" | "title" | "project" | "updated" | "size" | "tokens";
type SortDirection = "asc" | "desc";
type AppSection = "threads" | "diagnostics" | "resources" | "imports" | "api";
type Language = "zh" | "en";
type PromptFilterMode = "pure" | "focused" | "withAgents" | "automation" | "delegation" | "all";
type PromptCopyMode = "clean" | "metadata";
type PromptCopySpacingMode = "spaced" | "compact";
type LocalApiConnectionStatus = "checking" | "connected" | "blocked";

const dialogFocusableSelector = [
  "button:not([disabled])",
  "a[href]",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "[tabindex]:not([tabindex='-1'])"
].join(",");
const activeDialogStack: HTMLElement[] = [];

function dialogFocusableElements(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(dialogFocusableSelector)).filter((element) => (
    !element.hidden
    && !element.closest("[inert]")
    && window.getComputedStyle(element).visibility !== "hidden"
    && element.getClientRects().length > 0
  ));
}

function useModalAccessibility(isOpen: boolean, onClose: () => void): React.RefObject<HTMLElement | null> {
  const dialogRef = React.useRef<HTMLElement>(null);
  const onCloseRef = React.useRef(onClose);

  React.useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  React.useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!isOpen || !dialog) return;

    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const inertedElements: Array<{ element: HTMLElement; wasInert: boolean }> = [];
    let branch: HTMLElement = dialog;
    while (branch.parentElement) {
      const parent = branch.parentElement;
      for (const sibling of Array.from(parent.children)) {
        if (!(sibling instanceof HTMLElement) || sibling === branch) continue;
        inertedElements.push({ element: sibling, wasInert: sibling.inert });
        sibling.inert = true;
      }
      branch = parent;
      if (parent === document.body) break;
    }

    activeDialogStack.push(dialog);
    const focusFirstControl = () => {
      const preferred = dialog.querySelector<HTMLElement>("[data-dialog-initial-focus]");
      (preferred || dialogFocusableElements(dialog)[0] || dialog).focus();
    };
    const animationFrame = window.requestAnimationFrame(focusFirstControl);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (activeDialogStack.at(-1) !== dialog) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusableElements = dialogFocusableElements(dialog);
      if (!focusableElements.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const firstElement = focusableElements[0];
      const lastElement = focusableElements[focusableElements.length - 1];
      const activeElement = document.activeElement;
      if (!dialog.contains(activeElement)) {
        event.preventDefault();
        (event.shiftKey ? lastElement : firstElement).focus();
      } else if (event.shiftKey && activeElement === firstElement) {
        event.preventDefault();
        lastElement.focus();
      } else if (!event.shiftKey && activeElement === lastElement) {
        event.preventDefault();
        firstElement.focus();
      }
    };
    const handleFocusIn = (event: FocusEvent) => {
      if (activeDialogStack.at(-1) === dialog && event.target instanceof Node && !dialog.contains(event.target)) {
        focusFirstControl();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    document.addEventListener("focusin", handleFocusIn, true);

    return () => {
      window.cancelAnimationFrame(animationFrame);
      document.removeEventListener("keydown", handleKeyDown, true);
      document.removeEventListener("focusin", handleFocusIn, true);
      const stackIndex = activeDialogStack.lastIndexOf(dialog);
      if (stackIndex >= 0) activeDialogStack.splice(stackIndex, 1);
      for (const { element, wasInert } of inertedElements.reverse()) element.inert = wasInert;
      if (previouslyFocused?.isConnected && !previouslyFocused.closest("[inert]")) previouslyFocused.focus();
    };
  }, [isOpen]);

  return dialogRef;
}
type DiagnosticsLoadStatus = "idle" | "loading" | "refreshing" | "ready" | "error";
type Translator = (text: string) => string;
type NavigationItem = {
  value: AppSection;
  label: string;
  icon: typeof Eye;
};

const defaultCodexHome = "";
const languageStorageKey = "codex-home-manager-language";
const apiBaseStorageKey = "codex-home-manager-api-base-url";
const detailPanelWidthStorageKey = "codex-home-manager-detail-panel-width-v2";
const collapsedProjectGroupsStorageKey = "codex-home-manager-collapsed-project-groups";
const defaultDetailPanelWidth = 840;
const minimumDetailPanelWidth = 620;
const maximumDetailPanelWidth = 1320;
const detailPanelViewportReserve = 320;
const diagnosticsCacheTtlMs = 30_000;
const diagnosticsReportCache = new Map<string, { report: DiagnosticsReport; cachedAtMs: number }>();
const defaultLocalApiBaseUrl = "http://127.0.0.1:8765";
const githubReleaseAssetBaseUrl = "https://github.com/SimpleZion/codex-home-manager/releases/latest/download";
const hostedReleaseAssetBaseUrl = typeof window !== "undefined" && !["127.0.0.1", "localhost"].includes(window.location.hostname)
  ? window.location.origin
  : githubReleaseAssetBaseUrl;
const localConnectorDownloadUrl = `${hostedReleaseAssetBaseUrl}/codex-home-manager-local-win-x64.exe`;
const localConnectorZipDownloadUrl = `${hostedReleaseAssetBaseUrl}/codex-home-manager-local-win-x64.zip`;
const localConnectorLaunchUrl = "codex-home-manager://start";
const publicRepositoryUrl = "https://github.com/SimpleZion/codex-home-manager";
const localAppOrigins = new Set([
  "http://127.0.0.1:8765",
  "http://localhost:8765",
  "http://127.0.0.1:5173",
  "http://localhost:5173"
]);
type LocalNetworkRequestInit = RequestInit & { targetAddressSpace?: "loopback" };

function configuredApiBaseUrl(): string {
  const searchParams = new URLSearchParams(window.location.search);
  const queryApiBase = searchParams.get("api_base")?.trim();
  if (queryApiBase) {
    window.localStorage.setItem(apiBaseStorageKey, queryApiBase);
    return queryApiBase.replace(/\/+$/, "");
  }
  const storedApiBase = window.localStorage.getItem(apiBaseStorageKey)?.trim();
  if (storedApiBase) return storedApiBase.replace(/\/+$/, "");
  return localAppOrigins.has(window.location.origin) ? "" : defaultLocalApiBaseUrl;
}

function apiDisplayBaseUrl(): string {
  return configuredApiBaseUrl() || window.location.origin;
}

function isHostedConsole(): boolean {
  return !pointsToLoopback(window.location.href);
}

function clampDetailPanelWidth(width: number): number {
  const viewportMaximum = typeof window === "undefined"
    ? maximumDetailPanelWidth
    : Math.max(
      minimumDetailPanelWidth,
      Math.min(maximumDetailPanelWidth, window.innerWidth - detailPanelViewportReserve)
    );
  return Math.round(Math.min(Math.max(width, minimumDetailPanelWidth), viewportMaximum));
}

function resolveApiUrl(url: string): string {
  if (/^https?:\/\//i.test(url)) return url;
  const apiBaseUrl = configuredApiBaseUrl();
  if (!apiBaseUrl) return url;
  return `${apiBaseUrl}${url.startsWith("/") ? url : `/${url}`}`;
}

function pointsToLoopback(url: string): boolean {
  try {
    const parsedUrl = new URL(url, window.location.href);
    return parsedUrl.hostname === "127.0.0.1" || parsedUrl.hostname === "localhost" || parsedUrl.hostname === "::1";
  } catch {
    return false;
  }
}

function apiRequestOptions(resolvedUrl: string, options?: RequestInit): LocalNetworkRequestInit {
  const requestOptions: LocalNetworkRequestInit = { mode: "cors", ...options };
  if (pointsToLoopback(resolvedUrl)) {
    requestOptions.targetAddressSpace = "loopback";
  }
  return requestOptions;
}

function isLocalApiAccessError(message: string | null): boolean {
  if (!message) return false;
  const normalizedMessage = message.toLowerCase();
  return normalizedMessage.includes("failed to fetch") || normalizedMessage.includes("loopback");
}

function codexHomeDisplayValue(codexHome: string, language: Language): string {
  if (codexHome.trim()) return codexHome;
  return language === "en" ? "Local API default" : "本机 API 默认路径";
}

function stringFromRecord(record: Record<string, unknown> | undefined, key: string): string {
  const value = record?.[key];
  return typeof value === "string" ? value : "";
}

function codexVersionSummary(versions: CodexCurrentVersions | null, language: Language): string {
  if (!versions) return language === "en" ? "Not loaded" : "未读取";
  const configuredCliVersion = versions.configuredCli?.version || "";
  const desktopVersion = versions.desktopInstall?.version || "";
  const pathCliVersion = versions.pathCli?.version || "";
  const cachedLatestVersion = stringFromRecord(versions.versionCache, "latest_version");
  const versionText = configuredCliVersion || desktopVersion || pathCliVersion || cachedLatestVersion;
  return versionText || (language === "en" ? "Unknown" : "未知");
}

function codexVersionTooltip(versions: CodexCurrentVersions | null, language: Language): string {
  if (!versions) return language === "en" ? "Codex version has not been loaded yet." : "尚未读取 Codex 版本。";
  const cachedLatestVersion = stringFromRecord(versions.versionCache, "latest_version");
  const cachedCheckedAt = stringFromRecord(versions.versionCache, "last_checked_at");
  const labels = language === "en"
    ? {
        effectiveCli: "Effective CLI",
        runtimeCli: "Runtime config CLI",
        desktop: "Desktop install",
        pathCli: "PATH CLI",
        cache: "version.json latest",
      }
    : {
        effectiveCli: "当前有效 CLI",
        runtimeCli: "运行配置 CLI",
        desktop: "桌面版",
        pathCli: "PATH CLI",
        cache: "version.json 最新记录",
      };
  return [
    `${labels.effectiveCli}: ${versions.configuredCli?.version || "-"} (${versions.configuredCli?.path || "-"})`,
    `${labels.runtimeCli}: ${versions.runtimeConfiguredCli?.version || "-"} (${versions.runtimeConfiguredCli?.path || "-"})`,
    `${labels.desktop}: ${versions.desktopInstall?.version || "-"} (${versions.desktopInstall?.path || "-"})`,
    `${labels.pathCli}: ${versions.pathCli?.version || "-"} (${versions.pathCli?.path || "-"})`,
    `${labels.cache}: ${cachedLatestVersion || "-"}${cachedCheckedAt ? ` @ ${cachedCheckedAt}` : ""}`,
  ].join("\n");
}

function localApiAccessMessage(language: Language): string {
  if (language === "en") {
    return "Start the local connector, then allow this site to access the local network or apps on this device in Chrome/Edge. The online console reaches Codex only through http://127.0.0.1:8765.";
  }
  return "请先启动本机连接器，然后在 Chrome/Edge 地址栏权限里允许本站访问本地网络或本机应用。线上控制台只会通过 http://127.0.0.1:8765 连接 Codex。";
}

function localApiConnectionTitle(language: Language, status: LocalApiConnectionStatus): string {
  if (language === "en") {
    return status === "checking" ? "Connecting to the local scanner" : "Start local scanning";
  }
  return status === "checking" ? "正在连接本机扫描器" : "启动本机扫描";
}

function localApiConnectionDescription(language: Language, status: LocalApiConnectionStatus): string {
  if (status === "checking") {
    return language === "en"
      ? "Checking the local connector and browser Local Network Access permission before loading thread data."
      : "正在检测本机连接器和浏览器本地网络访问权限，成功后自动加载线程数据。";
  }
  return localApiAccessMessage(language);
}

const englishText: Record<string, string> = {
  "线程": "Threads",
  "资源": "Resources",
  "导入": "Import",
  "API": "API",
  "可见": "Visible",
  "旧版首轮排序外": "Outside legacy first-page order",
  "已归档": "Archived",
  "需修复": "Needs repair",
  "文件缺失": "Missing file",
  "子agent": "Subagent",
  "隐藏": "Hidden",
  "主线程": "Main",
  "全部": "All",
  "归档": "Archived",
  "全部项目": "All projects",
  "自建项目": "Workspace projects",
  "普通对话": "Conversations",
  "其他路径": "Other paths",
  "状态": "Status",
  "位置": "Location",
  "项目": "Project",
  "更新时间": "Updated",
  "存储": "Storage",
  "操作": "Actions",
  "自身": "Self",
  "自身 JSONL": "Own JSONL",
  "子线程": "Child threads",
  "子线程合计": "Child total",
  "子线程存储": "Child storage",
  "含子线程": "Including children",
  "含子线程总计": "Including child threads",
  "总存储": "Total storage",
  "总 Tokens": "Total tokens",
  "SQLite tokens_used 原始记录": "SQLite tokens_used raw record",
  "非账户消耗口径": "Not an account usage metric",
  "SQLite 自身": "SQLite self",
  "SQLite 子线程合计": "SQLite child total",
  "点击排序": "Click to sort",
  "升序": "Ascending",
  "降序": "Descending",
  "按状态": "Sort by status",
  "按标题": "Sort by title",
  "双击查看完整详情": "Double-click to open full details",
  "完整线程详情": "Full thread details",
  "关闭详情窗口": "Close detail window",
  "容量构成": "Storage composition",
  "Token 构成": "Token composition",
  "SQLite 原始记录构成": "SQLite raw record composition",
  "每日 Token 消耗": "Daily token usage",
  "覆盖天数": "Covered days",
  "有消耗日期": "Usage days",
  "活动日期": "Activity days",
  "可审计日": "Audited days",
  "覆盖范围": "Date range",
  "范围": "range",
  "无消耗": "No usage",
  "无可审计消耗": "No audited usage",
  "不确定": "Unknown",
  "消耗不确定": "Usage unknown",
  "不确定日": "Unknown days",
  "未记录消耗": "Unrecorded usage",
  "缺少 token_count": "Missing token_count",
  "缺少 token_count 的线程": "Threads missing token_count",
  "部分消耗不确定": "Some usage unknown",
  "无可审计数值": "No audited value",
  "全部日期均有消耗": "Every date has usage",
  "全部日期均有可审计消耗": "Every date has audited usage",
  "按需读取": "Load on demand",
  "读取并展开": "Load and expand",
  "展开图表": "Expand chart",
  "收起图表": "Collapse chart",
  "读取每日 Token 时间线": "Load daily token timeline",
  "正在读取每日 Token 时间线": "Loading daily token timeline",
  "已读取每日 Token 时间线": "Daily token timeline loaded",
  "打开后再读取完整时间线，避免大线程详情被阻塞。": "The full timeline loads only after opening this panel so large thread details are not blocked.",
  "这块放在详情末尾并按需加载；展开后可切换范围、点击日期、查看来源和完整明细。": "This section is placed at the end and loads on demand. After expanding, switch ranges, select dates, inspect sources, and scroll full details.",
  "日期范围": "Date range",
  "全部范围": "Full range",
  "最近 90 天": "Last 90 days",
  "最近 30 天": "Last 30 days",
  "只看有消耗日": "Usage days only",
  "只看可审计日": "Audited days only",
  "显示指标": "Metric",
  "合计": "Total",
  "可点击柱子选择日期，横向滚动查看更多日期。": "Click bars to select a date. Scroll horizontally to inspect more dates.",
  "选中日期": "Selected date",
  "未记录标记": "Unrecorded marker",
  "不计入合计": "Not counted in total",
  "token_count 可审计": "Audited token_count",
  "token_count 可审计 + 部分不确定": "Audited token_count + some unknown",
  "详细表格跟随上方范围；点击行可切换选中日期。": "The detail table follows the selected range. Click a row to select that date.",
  "峰值日": "Peak day",
  "峰值 Tokens": "Peak tokens",
  "解析合计": "Parsed total",
  "可审计合计": "Audited total",
  "自身消耗": "Self usage",
  "子线程消耗": "Child usage",
  "最高消耗日": "Highest usage days",
  "最近展示": "Recent window",
  "无 token_count 记录": "No token_count records",
  "计入事件": "Counted events",
  "跳过重复事件": "Skipped duplicate events",
  "不确定线程": "Unknown threads",
  "含子线程每日合计": "Daily total including children",
  "最近 60 天": "Latest 60 days",
  "天": "days",
  "每日明细": "Daily details",
  "全部日期": "All dates",
  "日期": "Date",
  "总消耗": "Total usage",
  "自身事件": "Self events",
  "子线程事件": "Child events",
  "事件数": "Events",
  "来源": "Source",
  "类型": "Type",
  "token_count 事件": "token_count events",
  "消息结构": "Message structure",
  "子线程组成": "Child thread composition",
  "没有子线程": "No child threads",
  "总计": "Total",
  "自身主线程": "Main thread self",
  "消息总量": "Message total",
  "助手消息": "Assistant messages",
  "事件消息": "Event messages",
  "模型": "Model",
  "创建时间": "Created",
  "Git 分支": "Git branch",
  "CLI 版本": "CLI version",
  "可见对话流": "Visible conversation stream",
  "恢复": "Restore",
  "显示": "Show",
  "修复显示": "Repair display",
  "只读": "Read-only",
  "需连接器": "Connector required",
  "浏览器只读": "Browser read-only",
  "浏览器文件夹模式只能查看、搜索、读取日志和导出 prompt；修复、迁移、瘦身、删除和 MCP 请使用本机连接器。": "Browser folder mode can view, search, read logs and export prompts; use the local connector for repair, migration, slimming, deletion and MCP.",
  "本机连接器完整模式": "Local connector full mode",
  "父线程": "Parent thread",
  "子 agent 线程": "Subagent thread",
  "恢复到 Codex 侧边栏": "Restore to Codex sidebar",
  "清除手动隐藏状态并恢复到 Codex 侧边栏": "Clear manual hidden state and restore to Codex sidebar",
  "从 Codex 显示索引隐藏，不归档也不删除 JSONL": "Hide from Codex display indexes without archiving or deleting JSONL",
  "修复元数据并恢复到 Codex 侧边栏": "Repair metadata and restore to Codex sidebar",
  "没有匹配的线程": "No matching threads",
  "线程详情": "Thread details",
  "收起详情面板": "Collapse details panel",
  "展开详情面板": "Expand details panel",
  "调整详情面板宽度": "Resize details panel",
  "拖拽或用左右方向键调整宽度": "Drag or use arrow keys to resize",
  "读取线程详情...": "Loading thread details...",
  "选择线程后可查看位置、存储、备份和危险操作": "Select a thread to inspect location, storage, backups, and high-risk actions",
  "创建备份": "Create backup",
  "回滚最近备份": "Restore latest backup",
  "导出 prompts": "Export prompts",
  "详细日志": "Logs",
  "显示线程": "Show thread",
  "隐藏线程": "Hide thread",
  "安全删除": "Safe delete",
  "复制线程": "Duplicate thread",
  "目标项目路径": "Target project path",
  "复制到目标项目": "Duplicate to target project",
  "线程瘦身范围": "Thread slimming scope",
  "移除嵌入图片和 data:image 内容": "Remove embedded images and data:image content",
  "只保留最新 compacted checkpoint": "Keep only the latest compacted checkpoint",
  "按选定范围瘦身": "Slim selected scope",
  "迁移线程": "Migrate thread",
  "迁移到目标项目": "Migrate to target project",
  "项目及文件夹改名": "Rename project and folder",
  "原项目路径": "Source project path",
  "同步重命名本地文件夹": "Also rename local folder",
  "执行项目重命名": "Rename project",
  "项目路径": "Project path",
  "JSONL 路径": "JSONL path",
  "Codex thread/list 排名": "Codex thread/list rank",
  "session_index 排名": "session_index rank",
  "侧栏显式引用": "Explicit sidebar reference",
  "侧边栏标题": "Sidebar title",
  "SQLite 标题": "SQLite title",
  "线程类型": "Thread type",
  "文件大小": "File size",
  "JSONL 行数": "JSONL lines",
  "用户消息": "User messages",
  "工具调用": "Tool calls",
  "工具输出": "Tool output",
  "状态说明": "Status notes",
  "备份": "Backups",
  "暂无备份": "No backups",
  "无": "None",
  "outside_initial_sidebar_limit": "Outside legacy initial sidebar rank",
  "outside_conversation_initial_page": "Outside legacy conversation first page",
  "manually_hidden_by_manager": "Manually hidden by manager",
  "archived": "Archived",
  "missing_rollout_file": "Missing rollout file",
  "subagent_child_thread": "Subagent child thread",
  "subagent_hidden_from_main_view": "Subagent hidden from main view",
  "rollout_in_archived_sessions": "Rollout stored in archived_sessions",
  "missing_visible_event_stream": "Missing visible event stream",
  "sparse_visible_event_stream": "Sparse visible event stream",
  "普通对话旧版首轮排序外": "Outside legacy conversation first page",
  "管理器手动隐藏": "Manually hidden by manager",
  "JSONL 文件缺失": "JSONL file missing",
  "子 agent 不在主线程视图": "Subagent hidden from main-thread view",
  "JSONL 位于 archived_sessions": "JSONL is in archived_sessions",
  "项目重命名": "Project rename",
  "线程瘦身": "Thread slimming",
  "手动备份": "Manual backup",
  "回滚前备份": "Pre-restore backup",
  "资源备份": "Resource backup",
  "写入资源": "Write resource",
  "导入项目": "Import project",
  "show": "Show",
  "hide": "Hide",
  "repair_user_event": "Repair user event",
  "archive": "Archive",
  "duplicate": "Duplicate",
  "migrate_project": "Migrate project",
  "rename_project": "Rename project",
  "slim": "Slim",
  "manual_backup": "Manual backup",
  "pre_restore": "Pre-restore",
  "resource_backup": "Resource backup",
  "write_resource": "Write resource",
  "copy_resource_from_home": "Copy resource from home",
  "import_thread": "Import thread",
  "import_project": "Import project",
  "有 pinned/hint 引用": "Has pinned/hint reference",
  "无显式引用": "No explicit reference",
  "不在 thread/list 排序中": "Not in thread/list order",
  "不在 session_index 修复窗口中": "Not in session_index repair window",
  "读取线程索引...": "Loading thread index...",
  "没有可用线程数据": "No thread data available",
  "全部线程": "All threads",
  "SQLite threads": "SQLite threads",
  "默认管理视图": "Default manager view",
  "评估和并行任务": "Evaluation and parallel tasks",
  "Codex 可见": "Codex visible",
  "普通主线程": "Normal main threads",
  "异常线程": "Problem threads",
  "缺文件或事件流异常": "Missing files or event-stream issues",
  "旧索引项目": "Legacy indexed projects",
  "项目内线程均不在可见首轮": "Project threads are outside the visible first page",
  "线程存储": "Thread storage",
  "JSONL 合计": "JSONL total",
  "线程态势": "Thread posture",
  "状态构成": "Status mix",
  "可见线程占比": "Visible main-thread share",
  "隐藏/需修复会影响侧边栏可见性": "Hidden and repair-needed rows affect sidebar visibility",
  "项目可见性": "Project visibility",
  "项目风险排行": "Project risk ranking",
  "按异常线程数排序": "By abnormal thread count",
  "暂无异常项目": "No abnormal projects",
  "容量": "Capacity",
  "存储与 token 大户": "Largest storage and token users",
  "存储与 SQLite 原始记录": "Largest storage and SQLite raw records",
  "按 JSONL 大小排序": "By JSONL size",
  "没有存储异常": "No storage outliers",
  "搜索标题、线程 ID、项目、JSONL 路径": "Search title, thread ID, project, JSONL path",
  "按侧边栏位置": "Sort by sidebar position",
  "按更新时间": "Sort by updated time",
  "按存储空间": "Sort by storage",
  "按 Tokens": "Sort by tokens",
  "按 SQLite 原始记录": "Sort by SQLite raw records",
  "按项目": "Sort by project",
  "当前结果": "Results",
  "当前范围": "Scope",
  "快照": "Snapshot",
  "收起详情": "Collapse details",
  "展开详情": "Expand details",
  "扫描 Codex Home...": "Scanning Codex Home...",
  "没有资源数据": "No resource data",
  "资源、配置、记忆和指令管理": "Resources, config, memories, and instructions",
  "资源入口": "Resource entries",
  "已存在": "Existing",
  "指令文件": "Instruction files",
  "记忆目录": "Memory directory",
  "技能目录": "Skills directory",
  "存在": "Exists",
  "缺失": "Missing",
  "资源体量": "Resource size",
  "已知入口合计": "Known entries total",
  "选择资源后可查看、备份或编辑文本内容": "Select a resource to inspect, back up, or edit text",
  "重读": "Reload",
  "备份资源": "Back up resource",
  "保存文本": "Save text",
  "大小": "Size",
  "文件数": "Files",
  "目录数": "Directories",
  "修改时间": "Modified",
  "目录内容": "Directory contents",
  "这是二进制资源，只显示元数据，不进入文本编辑器。": "This is a binary resource; only metadata is shown, and it is not opened in the text editor.",
  "从其他 .codex 导入线程、项目和资源": "Import threads, projects, and resources from another .codex",
  "目标：": "Target:",
  "来源 CODEX_HOME": "Source CODEX_HOME",
  "例如 E:\\backup\\.codex": "Example: E:\\backup\\.codex",
  "填写另一个 Codex Home 的根目录，例如备份盘或旧机器上的 `.codex` 文件夹；不要填单个 JSONL 文件。": "Enter the root of another Codex Home, such as a backup drive or an old machine's `.codex` folder. Do not enter a single JSONL file.",
  "导入单个线程": "Import one thread",
  "从另一个 `.codex` 的 SQLite 和 JSONL 中复制一个线程，默认生成新线程 ID。": "Copy one thread from another `.codex` SQLite and JSONL store. A new thread ID is generated by default.",
  "来源线程 ID": "Source thread ID",
  "例如 019de30a-27b8-7663-aa6a-6f9bf947202e": "Example: 019de30a-27b8-7663-aa6a-6f9bf947202e",
  "线程 ID 是 36 位 UUID，可从线程详情、JSONL 文件名或源 `.codex` 的线程列表中复制。": "The thread ID is a 36-character UUID. Copy it from thread details, a JSONL filename, or the source `.codex` thread list.",
  "例如 C:\\Projects\\ImportedProject": "Example: C:\\Projects\\ImportedProject",
  "留空会沿用源线程原来的 cwd；填写后会把复制出来的新线程绑定到这个项目路径。": "Leave blank to keep the source cwd. Fill it to bind the copied thread to this project path.",
  "导入线程": "Import thread",
  "导入整个项目": "Import full project",
  "复制来源项目下的所有匹配线程，可选择是否包括归档线程。": "Copy all matching threads under the source project, optionally including archived threads.",
  "来源项目路径": "Source project path",
  "例如 C:\\Documents\\Codex\\2026-05-01\\research": "Example: C:\\Documents\\Codex\\2026-05-01\\research",
  "填写源 `.codex` 中线程记录的项目路径/cwd，不是项目显示名，也不是 `.codex` 根目录。": "Enter the project path/cwd recorded in the source `.codex`, not the display name or the `.codex` root.",
  "例如 C:\\Projects\\ResearchImported": "Example: C:\\Projects\\ResearchImported",
  "留空会保留来源项目路径；填写后会把导入的项目线程整体映射到新路径。": "Leave blank to keep the source project path. Fill it to map imported project threads to a new path.",
  "包括归档线程": "Include archived threads",
  "可用时保留原线程 ID": "Preserve original thread IDs when available",
  "导入项目线程": "Import project threads",
  "复制 Codex Home 资源": "Copy Codex Home resources",
  "用于迁移 `AGENTS.md`、`memories`、`skills`、配置片段或其他相对路径资源。": "Use this to migrate `AGENTS.md`, `memories`, `skills`, config snippets, or other relative-path resources.",
  "来源相对路径": "Source relative path",
  "例如 AGENTS.md、memories、skills/my-skill": "Example: AGENTS.md, memories, skills/my-skill",
  "相对路径会从来源 CODEX_HOME 下面解析；复制目录时会按目录整体复制。": "Relative paths are resolved under the source CODEX_HOME. Directories are copied as whole directories.",
  "目标相对路径": "Target relative path",
  "例如 memories/imported 或留空同名复制": "Example: memories/imported, or leave blank to copy with the same name",
  "留空时目标路径与来源相同；填写后可以把资源复制到当前 CODEX_HOME 的另一个相对位置。": "Leave blank to use the same target path. Fill it to copy into another relative location in the current CODEX_HOME.",
  "允许覆盖目标资源": "Allow overwriting target resource",
  "复制资源": "Copy resource",
  "给新 agent 直接调用的稳定本地 API": "Stable local API for new agents",
  "MCP 优先接入": "Prefer MCP for agents",
  "把本地连接器作为 streamable HTTP MCP server 注册。agent 先 tools/list，再按工具 schema 调用；写入前先用对应 preview 工具取得 operationPreviewId 和 inputHash。": "Register the local connector as a streamable HTTP MCP server. Agents call tools/list first, then call tools by schema; before writing, call the matching preview tool to get operationPreviewId and inputHash.",
  "MCP 配置": "MCP config",
  "MCP 工具调用": "MCP tool call",
  "REST/OpenAPI 兜底": "REST/OpenAPI fallback",
  "REST 仍然保留给脚本和不支持 MCP 的 agent；严格 schema 可读取 OpenAPI。": "REST remains available for scripts and agents that do not support MCP; strict schemas are available from OpenAPI.",
  "MCP endpoint": "MCP endpoint",
  "OpenAPI JSON": "OpenAPI JSON",
  "公开 API 说明": "Public API guide",
  "当前未连接本机连接器，下面展示公开接入说明；tools/list、OpenAPI JSON 和真实写入能力需要先启动 http://127.0.0.1:8765。": "The local connector is not connected. This page still shows the public integration guide; tools/list, OpenAPI JSON, and real write capabilities require http://127.0.0.1:8765 to be running first.",
  "写入安全模型": "Write safety model",
  "MCP 写工具同样需要 apiToken、operationPreviewId、inputHash；Codex 正在运行时还需要 acknowledgeCodexRunningRisk=true。": "MCP write tools also require apiToken, operationPreviewId and inputHash; while Codex is running they also require acknowledgeCodexRunningRisk=true.",
  "先预览再写入": "Preview before write",
  "示例先预览显示线程，再把返回的 operationPreviewId 和 inputHash 传给写工具。": "Preview showing a thread first, then pass the returned operationPreviewId and inputHash to the write tool.",
  "能力列表": "Capability list",
  "能力发现": "Capability discovery",
  "新 agent 先请求 `/api/capabilities`，再按返回的 method/path/required/bodyExample 调用对应能力；严格 schema 可直接读取 `/openapi.json`。": "A new agent should first call `/api/capabilities`, then call each capability using the returned method/path/required/bodyExample. Strict schemas are available at `/openapi.json`.",
  "写入门禁": "Write gate",
  "写操作会创建备份；Codex 运行中时需要显式确认。": "Writes create backups; explicit acknowledgement is required while Codex is running.",
  "受保护资源": "Protected resources",
  "显示隐藏线程": "Show hidden threads",
  "预览导入线程": "Preview thread import",
  "执行导入线程": "Run thread import",
  "读取 AGENTS.md": "Read AGENTS.md",
  "必填：": "Required:",
  "；备份：": "; backup:",
  "风险：": "Risk:",
  "；幂等性：": "; idempotency:",
  "返回：": "Returns:",
  "预览：": "Preview:",
  "写入：": "Write:",
  "回滚：": "Restore:",
  "综合": "Combined",
  "请求/错误库": "Request/error database",
  "会话 JSONL": "Session JSONL",
  "请求": "Request",
  "失败/警告": "Failure/warning",
  "错误": "Error",
  "应用日志": "App log",
  "工具": "Tool",
  "事件": "Event",
  "用户": "User",
  "助手": "Assistant",
  "会话": "Session",
  "推理": "Reasoning",
  "解析错误": "Parse error",
  "警告": "Warning",
  "信息": "Info",
  "线程详细日志": "Thread detailed logs",
  "关闭日志窗口": "Close logs window",
  "搜索错误、请求 URL、工具名、原始行": "Search errors, request URLs, tool names, raw lines",
  "匹配": "Matches",
  "行数": "Lines",
  "读取日志...": "Loading logs...",
  "没有匹配日志": "No matching logs",
  "原始记录": "Raw record",
  "（已截断）": " (truncated)",
  "上一页": "Previous",
  "下一页": "Next",
  "线程、资源、导入和 agent API 管理台": "Threads, resources, imports, and agent API console",
  "当前 Home": "Current Home",
  "当前 Codex": "Current Codex",
  "兼容 limit": "Compatibility limit",
  "仅用于旧版首轮排序诊断": "Only used for legacy first-page rank diagnostics",
  "自动备份": "Auto backup",
  "写入操作会先创建可回滚备份": "Write operations create rollback backups first",
  "写入操作不会创建自动备份": "Write operations will not create automatic backups",
  "刷新": "Refresh",
  "Codex 正在运行，写入类操作会二次确认": "Codex is running; write operations require a second confirmation",
  "条风险提示": "warnings",
  "中文": "中文",
  "English": "English",
  "切换到英文": "Switch to English",
  "切换到中文": "Switch to Chinese",
  "运行中提示：": "Runtime warning:",
  "Codex 相关进程正在运行；高风险写入前请尽量先关闭 Codex Desktop：": "Codex-related process is running; close Codex Desktop before high-risk writes when possible:",
  "请传入 acknowledgeCodexRunningRisk=true 后再继续。": "Pass acknowledgeCodexRunningRisk=true to proceed.",
  "自动备份已开启：操作前会创建回滚备份。": "Auto backup is on: a rollback backup will be created before the operation.",
  "自动备份已关闭：本次操作不会创建回滚材料，之后不能用管理器一键回滚。": "Auto backup is off: this operation will not create rollback material, so one-click restore will not be available.",
  "是": "Yes",
  "否": "No",
  "JSONL 当前大小：": "Current JSONL size:",
  "行数：": "Lines:",
  "嵌入图片引用：": "Embedded image refs:",
  "嵌入 image_url：": "Embedded image_url fields:",
  "非法 image_url：": "Invalid image_url fields:",
  "encrypted_content 字段：": "encrypted_content fields:",
  "compacted checkpoint：": "Compacted checkpoints:",
  "可移除图片：": "Can remove images:",
  "可压缩 checkpoint：": "Can reduce checkpoints:",
  "匹配线程：": "Matched threads:",
  "存在的 JSONL：": "Existing JSONL:",
  "JSONL 总大小：": "Total JSONL size:",
  "会重命名文件夹：": "Will rename folder:",
  "可导入线程：": "Importable threads:",
  "来源线程：": "Source thread:",
  "目标线程：": "Target thread:",
  "保留原 ID：": "Preserve original ID:",
  "JSONL 大小：": "JSONL size:",
  "来源：": "Source:",
  "来源大小：": "Source size:",
  "目标已存在：": "Target exists:",
  "Codex Desktop 仍在运行。": "Codex Desktop is still running.",
  "继续执行": "Continue",
  "可能被正在运行的 Codex 覆盖或抢占。确认继续？": "may be overwritten or locked by the running Codex process. Continue?",
  "已取消：Codex Desktop 正在运行，未确认写入风险。": "Cancelled: Codex Desktop is running and write risk was not acknowledged.",
  "将线程恢复到 Codex 侧边栏？": "Restore this thread to Codex sidebar?",
  "已恢复到侧边栏，刷新或重启 Codex Desktop 后应可见。": "Restored to the sidebar. It should be visible after refreshing or restarting Codex Desktop.",
  "修复并显示线程？": "Repair and show this thread?",
  "会把 has_user_event 修复为可见状态，并恢复到 Codex 侧边栏。": "This will repair has_user_event into a visible state and restore the thread to the Codex sidebar.",
  "修复线程": "Repair thread",
  "已修复线程元数据，并恢复到 Codex 侧边栏。": "Thread metadata repaired and restored to the Codex sidebar.",
  "隐藏这个可见线程？": "Hide this visible thread?",
  "会从 Codex 显示索引中移除；不会归档、不会删除 JSONL，也不会删除项目绑定或 heartbeat 权限。": "This removes it from Codex display indexes; it will not archive, delete JSONL, remove project binding, or remove heartbeat permissions.",
  "已隐藏线程": "Hide thread",
  "线程已隐藏；刷新或重启 Codex Desktop 后侧边栏应不再显示。": "Thread hidden. It should disappear from the sidebar after refreshing or restarting Codex Desktop.",
  "已创建线程状态备份。": "Thread state backup created.",
  "已导出": "Exported",
  "条 prompt：": "prompts:",
  "请先填写复制目标项目路径。": "Enter the duplicate target project path first.",
  "复制线程到目标项目？": "Duplicate thread to target project?",
  "目标项目：": "Target project:",
  "会复制 JSONL、新增 SQLite 线程记录，并写入目标项目 hint。": "This will copy JSONL, create a SQLite thread row, and write the target project hint.",
  "已复制线程到": "Duplicated thread to",
  "安全删除/归档线程？": "Safe delete/archive this thread?",
  "不会永久删除 JSONL。": "This will not permanently delete JSONL.",
  "归档线程": "Archive thread",
  "已归档线程。": "Thread archived.",
  "已归档线程，并从 Codex 侧边栏索引移除；刷新或重启 Codex Desktop 后应不再显示。": "Thread archived and removed from Codex sidebar indexes. It should disappear after refreshing or restarting Codex Desktop.",
  "请至少选择一个线程瘦身范围。": "Select at least one thread slimming scope.",
  "按选定范围给线程瘦身？": "Slim the thread using the selected scope?",
  "瘦身范围：": "Slimming scope:",
  "影响预览：": "Impact preview:",
  "瘦身完成，节省": "Slimming complete, saved",
  "请先填写目标项目路径。": "Enter the target project path first.",
  "迁移线程到目标项目？": "Migrate thread to target project?",
  "迁移前必须关闭 Codex Desktop 和 Codex CLI；运行中迁移会被后端拒绝。": "Close Codex Desktop and Codex CLI before migrating; the backend rejects migration while they are running.",
  "线程迁移完成。": "Thread migration complete.",
  "请填写原项目路径和目标项目路径。": "Enter both the source and target project paths.",
  "更改项目及文件夹名？": "Rename project and folder?",
  "会更新匹配线程、Codex global state、config，并按勾选项重命名本地文件夹。": "This updates matching threads, Codex global state, config, and renames the local folder if selected.",
  "重命名项目": "Rename project",
  "项目重命名完成，更新": "Project rename complete, updated",
  "条线程。": "threads.",
  "条": "items",
  "回滚备份？": "Restore backup?",
  "回滚前会再创建一个 pre_restore 备份。": "A pre_restore backup will be created before restoring.",
  "自动备份已关闭：回滚前不会创建 pre_restore 备份。": "Auto backup is off: no pre_restore backup will be created before restoring.",
  "回滚备份": "Restore backup",
  "已回滚备份。": "Backup restored.",
  "已备份资源：": "Resource backed up:",
  "保存文本资源？": "Save text resource?",
  "保存资源": "Save resource",
  "已保存资源：": "Resource saved:",
  "请填写来源 CODEX_HOME 和来源线程 ID。": "Enter the source CODEX_HOME and source thread ID.",
  "导入线程？": "Import thread?",
  "已导入线程：": "Imported thread:",
  "请填写来源 CODEX_HOME 和来源项目路径。": "Enter the source CODEX_HOME and source project path.",
  "导入项目线程？": "Import project threads?",
  "已导入项目线程：": "Imported project threads:",
  "条。": "items.",
  "请填写来源 CODEX_HOME 和来源相对路径。": "Enter the source CODEX_HOME and source relative path.",
  "复制 Codex 资源？": "Copy Codex resource?",
  "复制 Codex 资源": "Copy Codex resource",
  "已复制资源：": "Resource copied:",
  "0 B": "0 B",
  "(empty)": "(empty)",
  "体检": "Diagnostics",
  "严重": "Critical",
  "通过": "Pass",
  "建议": "Recommendation",
  "证据": "Evidence",
  "证据与路径": "Evidence and paths",
  "展开核查证据": "Show evidence",
  "相关路径": "Affected paths",
  "无额外证据": "No additional evidence",
  "双击查看体检详情": "Double-click to open diagnostic details",
  "双击查看详情": "Double-click for details",
  "查看详情": "View details",
  "体检详情": "Diagnostic details",
  "问题详情": "Issue details",
  "检查详情": "Check details",
  "检查 ID": "Check ID",
  "问题 ID": "Issue ID",
  "分类": "Category",
  "摘要": "Summary",
  "修复命令": "Repair command",
  "复制详情": "Copy details",
  "已复制详情": "Details copied",
  "原始 JSON": "Raw JSON",
  "正在体检 Codex Home...": "Running Codex Home diagnostics...",
  "没有可用体检报告": "No diagnostics report available",
  "Codex Home 体检": "Codex Home Diagnostics",
  "重新体检": "Run diagnostics",
  "健康分": "Health score",
  "只读扫描，不修改 `.codex`。": "Read-only scan; no `.codex` writes.",
  "严重问题": "Critical issues",
  "需要优先处理": "Handle first",
  "可能影响体验": "May affect usage",
  "检查项": "Checks",
  "项通过": "passed",
  "线程数": "Threads",
  "只读扫描": "Read-only scan",
  "体检不会修改 `.codex`，用于发现状态库、侧边栏索引、插件缓存、日志、存储和运行进程风险。": "Diagnostics do not modify `.codex`; they detect state database, sidebar index, plugin cache, log, storage, and runtime process risks.",
  "生成时间": "Generated",
  "问题与建议": "Issues and recommendations",
  "检查队列": "Inspection queue",
  "关注": "Attention",
  "问题": "Issues",
  "检查": "Checks",
  "没有需要处理的建议": "No recommendations need action",
  "未发现阻塞问题": "No blocking issue found",
  "当前扫描范围内没有严重或警告级问题。": "No critical or warning-level issue was found in the current scan scope.",
  "检查明细": "Check details",
  "项需关注": "need attention"
  ,
  "给 Codex 的修复 prompt": "Repair prompt for Codex",
  "复制后粘贴给你自己的 Codex，让它基于本机证据执行修复；本工具只生成提示词，不替你直接写入。": "Copy this into your own Codex so it can repair from local evidence; this tool only generates the prompt and does not write for you.",
  "这段 prompt 会带上体检摘要、问题、证据路径和执行边界，要求目标 Codex 先复核再修复。": "The prompt includes the health summary, issues, evidence paths, and operating boundaries, and asks the target Codex to verify before repairing.",
  "复制 prompt": "Copy prompt",
  "已复制": "Copied",
  "复制失败": "Copy failed",
  "展开 prompt": "Show prompt",
  "没有可复制的修复 prompt": "No repair prompt is available",
  "查看 prompts": "View prompts",
  "线程 prompts": "Thread prompts",
  "读取 prompts...": "Loading prompts...",
  "没有 prompt": "No prompts",
  "复制全部": "Copy all",
  "复制当前筛选": "Copy current filter",
  "自动化任务": "Automations",
  "只显示 heartbeat、定时任务和自动化续跑注入的任务内容": "Show only heartbeat, scheduled-task, and automation continuation records",
  "线程转发": "Thread handoffs",
  "只显示由其他 Codex 线程发送到当前线程的委派消息": "Show only delegated messages sent from other Codex threads into this thread",
  "复制格式": "Copy format",
  "仅正文": "Text only",
  "带元信息": "With metadata",
  "复制干净文本": "Copy clean text",
  "复制带元信息": "Copy with metadata",
  "只复制 prompt 正文，不包含编号、行号、时间、来源或分隔线。": "Copy only prompt body text, without prompt numbers, line numbers, timestamps, source labels, or separators.",
  "复制 prompt 正文和编号、行号、时间、来源、分隔线，方便回溯定位。": "Copy prompt body text plus prompt numbers, line numbers, timestamps, source labels, and separators for traceability.",
  "空行处理": "Blank line handling",
  "空行": "Blank lines",
  "保留": "Keep",
  "无空行": "No blanks",
  "保留 prompt 之间和 prompt 内部的空白行。": "Keep blank lines between prompts and inside each prompt.",
  "复制时移除空白行，并用单换行连接多条 prompt。": "Remove blank lines when copying and join multiple prompts with single line breaks.",
  "字符": "chars",
  "行": "line",
  "管理操作": "Management actions",
  "展开管理操作": "Show management actions",
  "收起管理操作": "Hide management actions"
  ,
  "Prompt 来源筛选": "Prompt source filters",
  "Prompt 来源统计": "Prompt source summary",
  "Prompt 筛选": "Prompt filters",
  "上下文+输入": "Context + input",
  "内部上下文": "Internal context",
  "只显示你输入的请求文字，剔除文件列表、图片标签、子 agent 和内部上下文": "Show only your request text, excluding file lists, image labels, subagents, and internal context",
  "含子 agent": "Include subagents",
  "子 agent": "Subagent",
  "已隐藏": "Hidden",
  "当前显示": "Currently shown",
  "当前筛选没有 prompt": "No prompts match the current filter",
  "提示": "Notice",
  "显示所有用户角色记录": "Show all user-role records",
  "显示用户输入、上下文和子 agent 通知": "Show user input, context, and subagent notifications",
  "显示用户输入、附件上下文和浏览器上下文，隐藏子 agent 与内部上下文": "Show user input, attachment context, and browser context while hiding subagents and internal context",
  "浏览器上下文": "Browser context",
  "用户上下文": "User context",
  "用户输入": "User input",
  "纯文本输入": "Plain-text input",
  "续跑目标": "Continuation goal",
  "附件上下文": "Attachment context",
  "需要先关闭 Codex：": "Codex must be closed first:",
  "项目重命名要求先关闭 Codex Desktop 和 Codex CLI，否则侧边栏缓存可能会把旧项目名写回。": "Close Codex Desktop and Codex CLI before renaming a project, or the sidebar cache may restore the old project name.",
  "缓存报告": "Cached report",
  "正在刷新体检...": "Refreshing diagnostics...",
  "运行容量趋势": "Capacity trends",
  "展开运行容量趋势": "Expand capacity trends",
  "收起运行容量趋势": "Collapse capacity trends",
  "Sessions 体量": "Sessions size",
  "超大线程": "Very large threads",
  "超大线程（≥ 250 MB）": "Very large threads (>= 250 MB)",
  "管理器备份": "Manager backups",
  "MCP 子进程": "MCP child processes",
  "风险": "risk",
  "较上次增长": "Increased since previous snapshot",
  "较上次下降": "Decreased since previous snapshot",
  "较上次持平": "Unchanged since previous snapshot",
  "暂无历史基线": "No historical baseline yet",
  "个文件": "files",
  "历史走势": "History",
  "MCP 构成": "MCP composition",
  "正常 node_repl": "Normal node_repl",
  "旧版 node_repl 参数": "Legacy node_repl arguments",
  "Legacy fallback": "Legacy fallback",
  "xcodebuild 风险": "xcodebuild risk",
  "其他 MCP": "Other MCP",
  "保留策略": "Retention guidance",
  "按日保留最近 90 天，最多 90 个快照。": "Daily snapshots retained for 90 days, up to 90 snapshots.",
  "快照只记录计数、体量和时间，不记录标题、prompt、路径或命令行。": "Snapshots store only counts, sizes, and timestamps; never titles, prompts, paths, or command lines.",
  "先核对备份 manifest 和回滚价值，再将明确过期的备份移入回收站。": "Review backup manifests and rollback value before moving clearly expired backups to the recycle bin.",
  "优先对超大线程做瘦身预览，不按数量或体量直接删除会话。": "Preview slimming for very large threads first; never delete sessions based on count or size alone.",
  "仅在 legacy fallback 或 xcodebuild 风险持续增长时核对插件来源和父进程；正常 node_repl fanout 无需清理。": "Inspect plugin sources and parent processes only when legacy fallback or xcodebuild risk keeps growing; normal node_repl fanout does not need cleanup.",
  "趋势快照暂未持久化，本次体检仍然可用。": "Trend history was not persisted; this diagnostics report remains usable.",
  "备份扫描达到文件上限，当前值是已扫描部分。": "The backup scan reached its file limit; current values cover the scanned portion.",
  "已从损坏的趋势文件恢复。": "Recovered from a damaged trend file.",
  "个快照": "snapshots"
};

const internalChineseText: Record<string, string> = {
  outside_initial_sidebar_limit: "旧版首轮排序外",
  outside_conversation_initial_page: "普通对话旧版首轮排序外",
  manually_hidden_by_manager: "管理器手动隐藏",
  archived: "已归档",
  missing_rollout_file: "JSONL 文件缺失",
  subagent_child_thread: "子 agent 线程",
  subagent_hidden_from_main_view: "子 agent 不在主线程视图",
  rollout_in_archived_sessions: "JSONL 位于 archived_sessions",
  show: "显示",
  hide: "隐藏",
  repair_user_event: "修复显示",
  archive: "归档",
  duplicate: "复制线程",
  migrate_project: "迁移线程",
  rename_project: "项目重命名",
  slim: "线程瘦身",
  manual_backup: "手动备份",
  pre_restore: "回滚前备份",
  resource_backup: "资源备份",
  write_resource: "写入资源",
  copy_resource_from_home: "复制资源",
  import_thread: "导入线程",
  import_project: "导入项目"
};

function normalizeLanguage(value: string | null | undefined): Language {
  if (value === "en") return "en";
  return "zh";
}

function translateText(language: Language, text: string): string {
  if (language === "zh") return text;
  return englishText[text] || text;
}

function localizeInternalLabel(text: string, t: Translator): string {
  return t(internalChineseText[text] || text);
}

type I18nContextValue = {
  language: Language;
  t: Translator;
  formatDate: (value: number | null) => string;
};

const I18nContext = React.createContext<I18nContextValue>({
  language: "zh",
  t: (text) => text,
  formatDate: (value) => formatDateForLanguage(value, "zh")
});

function useI18n(): I18nContextValue {
  return React.useContext(I18nContext);
}

async function copyTextToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const didCopy = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!didCopy) {
    throw new Error("copy command failed");
  }
}

function navigationItems(t: Translator): NavigationItem[] {
  return [
    { value: "threads", label: t("线程"), icon: Eye },
    { value: "diagnostics", label: t("体检"), icon: ShieldCheck },
    { value: "resources", label: t("资源"), icon: BookOpen },
    { value: "imports", label: t("导入"), icon: FolderInput },
    { value: "api", label: t("API"), icon: Code2 }
  ];
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(size >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatDateForLanguage(value: number | null, language: Language): string {
  if (!value) return "-";
  return new Intl.DateTimeFormat(language === "zh" ? "zh-CN" : "en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function formatDate(value: number | null): string {
  return formatDateForLanguage(value, "zh");
}

function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

function formatCompactCount(value: number, language: Language = "en"): string {
  if (!Number.isFinite(value)) return "0";
  const sign = value < 0 ? "-" : "";
  const absoluteValue = Math.abs(value);
  if (language === "zh") {
    const units = [
      { threshold: 1_000_000_000_000, suffix: "万亿" },
      { threshold: 100_000_000, suffix: "亿" },
      { threshold: 10_000, suffix: "万" }
    ];
    const unit = units.find((item) => absoluteValue >= item.threshold);
    if (!unit) return `${sign}${formatCount(absoluteValue)}`;
    const scaledValue = absoluteValue / unit.threshold;
    const digits = scaledValue >= 1000 ? 0 : scaledValue >= 100 ? 1 : scaledValue >= 10 ? 1 : 2;
    return `${sign}${scaledValue.toFixed(digits).replace(/\.0+$|(\.\d*[1-9])0+$/, "$1")}${unit.suffix}`;
  }
  const units = [
    { threshold: 1_000_000_000_000, suffix: "T" },
    { threshold: 1_000_000_000, suffix: "B" },
    { threshold: 1_000_000, suffix: "M" },
    { threshold: 1_000, suffix: "K" }
  ];
  const unit = units.find((item) => absoluteValue >= item.threshold);
  if (!unit) return `${sign}${formatCount(absoluteValue)}`;
  const scaledValue = absoluteValue / unit.threshold;
  const digits = scaledValue >= 100 ? 0 : scaledValue >= 10 ? 1 : 2;
  return `${sign}${scaledValue.toFixed(digits).replace(/\.0+$|(\.\d*[1-9])0+$/, "$1")}${unit.suffix}`;
}

function threadChildCount(thread: ThreadRecord): number {
  return Number.isFinite(Number(thread.childThreadCount)) ? Math.max(0, Number(thread.childThreadCount)) : 0;
}

function threadOwnFileSize(thread: ThreadRecord): number {
  return Number.isFinite(Number(thread.fileSizeBytes)) ? Math.max(0, Number(thread.fileSizeBytes)) : 0;
}

function threadChildFileSize(thread: ThreadRecord): number {
  return Number.isFinite(Number(thread.childFileSizeBytes)) ? Math.max(0, Number(thread.childFileSizeBytes)) : 0;
}

function threadTotalFileSize(thread: ThreadRecord): number {
  const total = Number(thread.totalFileSizeBytes);
  return Number.isFinite(total) ? Math.max(0, total) : threadOwnFileSize(thread) + threadChildFileSize(thread);
}

function threadOwnTokens(thread: ThreadRecord): number {
  return Number.isFinite(Number(thread.tokensUsed)) ? Math.max(0, Number(thread.tokensUsed)) : 0;
}

function threadChildTokens(thread: ThreadRecord): number {
  return Number.isFinite(Number(thread.childTokensUsed)) ? Math.max(0, Number(thread.childTokensUsed)) : 0;
}

function threadTotalTokens(thread: ThreadRecord): number {
  const total = Number(thread.totalTokensUsed);
  return Number.isFinite(total) ? Math.max(0, total) : threadOwnTokens(thread) + threadChildTokens(thread);
}

function statusSortRank(thread: ThreadRecord): number {
  const order: Record<Visibility, number> = {
    visible: 0,
    hidden_by_initial_limit: 0,
    hidden: 2,
    needs_user_event_repair: 3,
    missing_file: 4,
    archived: 5,
    subagent: 6
  };
  return order[thread.visibility] ?? 99;
}

function defaultSortDirection(mode: SortMode): SortDirection {
  if (mode === "updated" || mode === "size" || mode === "tokens") return "desc";
  return "asc";
}

function compareText(left: string, right: string, language: Language): number {
  return left.localeCompare(right, language === "zh" ? "zh-CN" : "en-US", { numeric: true, sensitivity: "base" });
}

function compareThreadsByMode(left: ThreadRecord, right: ThreadRecord, mode: SortMode, direction: SortDirection, language: Language): number {
  const directionMultiplier = direction === "asc" ? 1 : -1;
  let result = 0;
  if (mode === "rank") {
    result = (left.recentRank || 999999) - (right.recentRank || 999999);
  } else if (mode === "status") {
    result = statusSortRank(left) - statusSortRank(right);
  } else if (mode === "title") {
    result = compareText(left.title, right.title, language);
  } else if (mode === "project") {
    result = compareText(left.projectLabel, right.projectLabel, language) || compareText(left.title, right.title, language);
  } else if (mode === "size") {
    result = threadTotalFileSize(left) - threadTotalFileSize(right);
  } else if (mode === "tokens") {
    result = threadTotalTokens(left) - threadTotalTokens(right);
  } else {
    result = left.updatedAtMs - right.updatedAtMs;
  }
  if (result !== 0) return result * directionMultiplier;
  return (left.recentRank || 999999) - (right.recentRank || 999999) || compareText(left.title, right.title, language);
}

function collectDescendantThreads(thread: ThreadRecord, threads: ThreadRecord[]): ThreadRecord[] {
  const childrenByParentId = new Map<string, ThreadRecord[]>();
  for (const candidate of threads) {
    if (!candidate.parentThreadId) continue;
    const children = childrenByParentId.get(candidate.parentThreadId) || [];
    children.push(candidate);
    childrenByParentId.set(candidate.parentThreadId, children);
  }
  const descendants: ThreadRecord[] = [];
  const visiting = new Set<string>();
  const visit = (threadId: string) => {
    if (visiting.has(threadId)) return;
    visiting.add(threadId);
    for (const child of childrenByParentId.get(threadId) || []) {
      descendants.push(child);
      visit(child.id);
    }
    visiting.delete(threadId);
  };
  visit(thread.id);
  descendants.sort((left, right) => (left.recentRank || 999999) - (right.recentRank || 999999) || right.updatedAtMs - left.updatedAtMs);
  return descendants;
}

function statusLabel(visibility: Visibility, t: Translator): string {
  const labels: Record<Visibility, string> = {
    visible: t("可见"),
    hidden_by_initial_limit: t("可见"),
    archived: t("已归档"),
    needs_user_event_repair: t("需修复"),
    missing_file: t("文件缺失"),
    subagent: t("子agent"),
    hidden: t("隐藏")
  };
  return labels[visibility];
}

function statusIcon(visibility: Visibility) {
  if (visibility === "visible" || visibility === "hidden_by_initial_limit") return <CheckCircle2 size={15} />;
  if (visibility === "archived") return <Archive size={15} />;
  if (visibility === "needs_user_event_repair") return <Wrench size={15} />;
  if (visibility === "missing_file") return <CircleAlert size={15} />;
  if (visibility === "subagent") return <Sparkles size={15} />;
  if (visibility === "hidden") return <EyeOff size={15} />;
  return <Eye size={15} />;
}

function isThreadActive(thread: ThreadRecord): boolean {
  return !thread.archived && thread.fileExists;
}

function isVisibleInCurrentScope(thread: ThreadRecord, threadKindFilter: ThreadKindFilter): boolean {
  if (threadKindFilter === "subagent") {
    return thread.threadKind === "subagent" && isThreadActive(thread);
  }
  if (threadKindFilter === "all") {
    return thread.codexVisible || (thread.threadKind === "subagent" && isThreadActive(thread));
  }
  return thread.codexVisible;
}

function matchesStatusFilter(thread: ThreadRecord, filterMode: FilterMode, threadKindFilter: ThreadKindFilter): boolean {
  if (filterMode === "visible") return isVisibleInCurrentScope(thread, threadKindFilter);
  if (filterMode === "manual_hidden") return thread.visibility === "hidden";
  if (filterMode === "repair") return thread.visibility === "needs_user_event_repair";
  if (filterMode === "archived") return thread.archived;
  return true;
}

function withWriteAcknowledgement<T extends Record<string, unknown>>(
  body: T,
  acknowledgeCodexRunningRisk: boolean,
  createBackup: boolean
): T & { acknowledgeCodexRunningRisk: boolean; createBackup: boolean } {
  return { ...body, acknowledgeCodexRunningRisk, createBackup };
}

function withPreviewTicket<T extends Record<string, unknown>>(body: T, preview: ImpactPreview | SlimPreview): T & {
  operationPreviewId: string | undefined;
  inputHash: string | undefined;
} {
  return { ...body, operationPreviewId: preview.operationPreviewId, inputHash: preview.inputHash };
}

function localizeWarningText(t: Translator, warning: string): string {
  const runningPrefix = "Codex-related process is running; close Codex Desktop before high-risk writes when possible:";
  const acknowledgementHint = "Pass acknowledgeCodexRunningRisk=true to proceed.";
  const migrationRequiresClosed = "Thread migration requires Codex Desktop and Codex CLI to be closed; acknowledgeCodexRunningRisk cannot make this operation safe.";
  return warning
    .replace(runningPrefix, t("Codex 相关进程正在运行；高风险写入前请尽量先关闭 Codex Desktop："))
    .replace(acknowledgementHint, t("请传入 acknowledgeCodexRunningRisk=true 后再继续。"))
    .replace(migrationRequiresClosed, t("迁移前必须关闭 Codex Desktop 和 Codex CLI；运行中迁移会被后端拒绝。"));
}

function warningsText(t: Translator, warnings?: string[]): string {
  return warnings?.length ? `\n\n${t("运行中提示：")}${localizeWarningText(t, warnings[0])}` : "";
}

function backupModeText(t: Translator, createBackups: boolean): string {
  return createBackups
    ? t("自动备份已开启：操作前会创建回滚备份。")
    : t("自动备份已关闭：本次操作不会创建回滚材料，之后不能用管理器一键回滚。");
}

function slimPreviewText(preview: SlimPreview, t: Translator): string {
  return [
    `${t("JSONL 当前大小：")}${formatBytes(preview.scan.totalBytes)}`,
    `${t("行数：")}${formatCount(preview.scan.lineCount)}`,
    `${t("嵌入图片引用：")}${formatCount(preview.scan.embeddedImageRefs)}`,
    `${t("嵌入 image_url：")}${formatCount(preview.scan.embeddedImageUrlFields ?? 0)}`,
    `${t("非法 image_url：")}${formatCount(preview.scan.invalidImageUrlRefs ?? 0)}`,
    `${t("encrypted_content 字段：")}${formatCount(preview.scan.encryptedContentFields ?? 0)}`,
    `${t("compacted checkpoint：")}${formatCount(preview.scan.compactedCount)}`,
    `${t("可移除图片：")}${preview.canRemoveImages ? t("是") : t("否")}`,
    `${t("可压缩 checkpoint：")}${preview.canReduceCompacted ? t("是") : t("否")}`
  ].join("\n");
}

function renamePreviewText(preview: ImpactPreview, t: Translator): string {
  return [
    `${t("匹配线程：")}${formatCount(preview.matchedThreads || 0)}`,
    `${t("存在的 JSONL：")}${formatCount(preview.existingRollouts || 0)}`,
    `${t("JSONL 总大小：")}${formatBytes(preview.rolloutBytes || 0)}`,
    `${t("会重命名文件夹：")}${preview.willRenameFolder ? t("是") : t("否")}`,
    `${t("需要先关闭 Codex：")}${preview.requiresCodexClosed ? t("是") : t("否")}`
  ].join("\n");
}

function importPreviewText(preview: ImpactPreview, t: Translator): string {
  return [
    `${t("可导入线程：")}${formatCount(preview.matchedThreads || 0)}`,
    `${t("JSONL 总大小：")}${formatBytes(preview.rolloutBytes || 0)}`
  ].join("\n");
}

function importThreadPreviewText(preview: ImpactPreview, t: Translator): string {
  return [
    `${t("来源线程：")}${preview.sourceThreadId || "-"}`,
    `${t("目标线程：")}${preview.targetThreadId || "-"}`,
    `${t("保留原 ID：")}${preview.preservesThreadId ? t("是") : t("否")}`,
    `${t("JSONL 大小：")}${formatBytes(preview.rolloutBytes || 0)}`
  ].join("\n");
}

function resourceCopyPreviewText(preview: ImpactPreview, t: Translator): string {
  return [
    `${t("来源：")}${preview.source?.path || "-"}`,
    `${t("目标：")}${preview.target?.path || "-"}`,
    `${t("来源大小：")}${formatBytes(preview.source?.sizeBytes || 0)}`,
    `${t("目标已存在：")}${preview.willOverwrite ? t("是") : t("否")}`
  ].join("\n");
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const resolvedUrl = resolveApiUrl(url);
  const response = await fetch(resolvedUrl, apiRequestOptions(resolvedUrl, options));
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(payload.detail || response.statusText, response.status);
  }
  return (await response.json()) as T;
}

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function collectTextParts(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap((item) => collectTextParts(item));
  if (!isObjectRecord(value)) return [];

  const directText = value.text;
  if (typeof directText === "string") return [directText];

  const content = value.content ?? value.parts ?? value.input ?? value.message;
  return collectTextParts(content);
}

function extractPromptTextFromPayload(value: unknown): string {
  return collectTextParts(value).join("\n").trim();
}

function extractPromptTextFromLogEntry(entry: ThreadLogEntry): string {
  if (entry.rawLine && !entry.rawLineTruncated) {
    try {
      const parsed = JSON.parse(entry.rawLine) as unknown;
      if (isObjectRecord(parsed)) {
        const payload = parsed.payload;
        const entryType = typeof parsed.type === "string" ? parsed.type : "";
        if (entryType === "user_message") {
          const promptText = extractPromptTextFromPayload(payload);
          if (promptText) return promptText;
        }
        if (entryType === "response_item" && isObjectRecord(payload) && payload.role === "user") {
          const promptText = extractPromptTextFromPayload(payload.content ?? payload);
          if (promptText) return promptText;
        }
        if (parsed.role === "user") {
          const promptText = extractPromptTextFromPayload(parsed.content ?? parsed);
          if (promptText) return promptText;
        }
      }
    } catch {
      // Old connectors may truncate raw JSONL. Fall back to the parsed preview below.
    }
  }
  return (entry.message || "").trim();
}

function isSubagentPromptText(text: string): boolean {
  const prefix = text.trimStart().slice(0, 5000);
  return prefix.startsWith("<subagent_notification>")
    || (prefix.includes('"agent_path"') && prefix.includes('"status"') && prefix.toLowerCase().includes("subagent"));
}

function isAutomationPromptText(text: string): boolean {
  const prefix = text.trimStart().slice(0, 5000);
  const lowerPrefix = prefix.toLowerCase();
  return lowerPrefix.startsWith("<heartbeat>")
    || lowerPrefix.startsWith("<automation>")
    || lowerPrefix.startsWith("<scheduled_task>")
    || lowerPrefix.includes("<automation_id>")
    || (lowerPrefix.includes("<current_time_iso>") && lowerPrefix.includes("<instructions>"));
}

function isThreadDelegationPromptText(text: string): boolean {
  return text.trimStart().slice(0, 5000).toLowerCase().startsWith("<codex_delegation");
}

function isCodexInternalContextPromptText(text: string): boolean {
  return text.trimStart().slice(0, 5000).startsWith("<codex_internal_context");
}

function isInternalPromptText(text: string): boolean {
  const prefix = text.trimStart().slice(0, 5000);
  return prefix.startsWith("# AGENTS.md instructions")
    || prefix.startsWith("<environment_context>")
    || prefix.startsWith("<turn_aborted>")
    || prefix.startsWith("<user_interruption>")
    || prefix.includes("<environment_context>")
    || prefix.includes("<permissions instructions>");
}

function removeEmbeddedImageBlocks(text: string): string {
  return text
    .replace(/\n?<image\b[\s\S]*?<\/image>\s*/gi, "\n")
    .replace(/\n?!\[[^\]]*]\([^)]*\)\s*/g, "\n")
    .trim();
}

function pureUserTextFromPrompt(text: string): string {
  if (isInternalPromptText(text) || isSubagentPromptText(text) || isAutomationPromptText(text) || isThreadDelegationPromptText(text) || isCodexInternalContextPromptText(text)) return "";
  const cleanedText = removeEmbeddedImageBlocks(text);
  const markerMatch = /^##\s*My request for Codex:\s*$/im.exec(cleanedText);
  if (markerMatch) return removeEmbeddedImageBlocks(cleanedText.slice(markerMatch.index + markerMatch[0].length)).trim();
  const prefix = cleanedText.trimStart();
  if (prefix.startsWith("# In app browser:") || prefix.startsWith("# Files mentioned by the user:")) return "";
  return cleanedText.trim();
}

function classifyPromptText(
  text: string
): Pick<PromptRecord, "sourceType" | "sourceLabel" | "visibleByDefault" | "pureText" | "pureCharacterCount" | "hasPureText"> {
  const prefix = text.trimStart().slice(0, 5000);
  const pureText = pureUserTextFromPrompt(text);
  const pureFields = { pureText, pureCharacterCount: pureText.length, hasPureText: Boolean(pureText) };
  if (isSubagentPromptText(text)) return { sourceType: "subagent", sourceLabel: "子 agent", visibleByDefault: false, ...pureFields };
  if (isAutomationPromptText(text)) return { sourceType: "automation", sourceLabel: "自动化任务", visibleByDefault: false, ...pureFields };
  if (isThreadDelegationPromptText(text)) return { sourceType: "delegation", sourceLabel: "线程转发", visibleByDefault: false, ...pureFields };
  if (isCodexInternalContextPromptText(text)) return { sourceType: "goal", sourceLabel: "续跑目标上下文", visibleByDefault: false, ...pureFields };
  if (isInternalPromptText(text)) return { sourceType: "internal", sourceLabel: "内部上下文", visibleByDefault: false, ...pureFields };
  if (prefix.startsWith("# In app browser:")) {
    return { sourceType: "browser", sourceLabel: "浏览器上下文", visibleByDefault: true, ...pureFields };
  }
  if (prefix.startsWith("# Files mentioned by the user:")) {
    return { sourceType: "attachment", sourceLabel: "附件上下文", visibleByDefault: true, ...pureFields };
  }
  return { sourceType: "user", sourceLabel: "用户输入", visibleByDefault: true, ...pureFields };
}

function normalizePromptRecord(prompt: PromptRecord): PromptRecord {
  const classification = classifyPromptText(prompt.text);
  return {
    ...prompt,
    characterCount: prompt.characterCount || prompt.text.length,
    sourceType: prompt.sourceType || classification.sourceType,
    sourceLabel: prompt.sourceLabel || classification.sourceLabel,
    visibleByDefault: typeof prompt.visibleByDefault === "boolean" ? prompt.visibleByDefault : classification.visibleByDefault,
    pureText: typeof prompt.pureText === "string" ? prompt.pureText : classification.pureText,
    pureCharacterCount: typeof prompt.pureCharacterCount === "number" ? prompt.pureCharacterCount : classification.pureCharacterCount,
    hasPureText: typeof prompt.hasPureText === "boolean" ? prompt.hasPureText : classification.hasPureText
  };
}

function promptSourceCounts(prompts: PromptRecord[]): Record<string, number> {
  return prompts.reduce<Record<string, number>>((counts, prompt) => {
    const sourceType = prompt.sourceType || "unknown";
    counts[sourceType] = (counts[sourceType] || 0) + 1;
    return counts;
  }, {});
}

function promptMatchesFilter(prompt: PromptRecord, filterMode: PromptFilterMode): boolean {
  if (filterMode === "pure") return Boolean((prompt.pureText || "").trim());
  if (filterMode === "all") return true;
  if (filterMode === "automation") return prompt.sourceType === "automation";
  if (filterMode === "delegation") return prompt.sourceType === "delegation";
  if (filterMode === "withAgents") return prompt.visibleByDefault !== false || prompt.sourceType === "subagent";
  return prompt.visibleByDefault !== false;
}

function promptTextForFilter(prompt: PromptRecord, filterMode: PromptFilterMode): string {
  return filterMode === "pure" ? (prompt.pureText || "").trim() : prompt.text;
}

function promptTextForCleanCopy(prompt: PromptRecord): string {
  return (prompt.pureText || prompt.text || "").trim();
}

function removeBlankLines(text: string): string {
  return text
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter((line) => line.trim().length > 0)
    .join("\n")
    .trim();
}

function threadPromptsFromLogs(thread: ThreadRecord, logs: ThreadLogs, entries: ThreadLogEntry[]): ThreadPrompts {
  const prompts = entries
    .map((entry, index) => ({
      index: index + 1,
      lineNumber: entry.lineNumber ?? 0,
      timestamp: entry.timestamp,
      text: extractPromptTextFromLogEntry(entry),
      characterCount: 0
    }))
    .filter((prompt) => prompt.text.length > 0)
    .map((prompt, index) => ({
      ...prompt,
      index: index + 1,
      characterCount: prompt.text.length,
      ...classifyPromptText(prompt.text)
    }));
  const visiblePromptCount = prompts.filter((prompt) => prompt.visibleByDefault !== false).length;
  const purePromptCount = prompts.filter((prompt) => prompt.hasPureText).length;

  return {
    threadId: thread.id,
    title: thread.title,
    rolloutPath: logs.rolloutPath || thread.rolloutPath,
    promptCount: prompts.length,
    purePromptCount,
    visiblePromptCount,
    hiddenPromptCount: prompts.length - visiblePromptCount,
    sourceCounts: promptSourceCounts(prompts),
    prompts
  };
}

async function fetchThreadPromptsFromLogs(thread: ThreadRecord, codexHome: string): Promise<ThreadPrompts> {
  const allEntries: ThreadLogEntry[] = [];
  let latestLogs: ThreadLogs | null = null;
  let offset = 0;
  const limit = 500;

  for (let page = 0; page < 20; page += 1) {
    const params = new URLSearchParams({
      codex_home: codexHome,
      offset: String(offset),
      limit: String(limit),
      kind: "user",
      source: "rollout",
      search: ""
    });
    const logs = await fetchJson<ThreadLogs>(`/api/threads/${thread.id}/logs?${params.toString()}`);
    latestLogs = logs;
    allEntries.push(...logs.entries);
    if (!logs.hasMore) break;
    offset += limit;
  }

  if (!latestLogs) {
    throw new ApiError("No prompt log entries were returned by the local connector.", 404);
  }

  return threadPromptsFromLogs(thread, latestLogs, allEntries);
}

async function fetchThreadPromptsFromLocalApi(thread: ThreadRecord, codexHome: string, language: Language): Promise<ThreadPrompts> {
  const params = new URLSearchParams({ codex_home: codexHome });
  try {
    return await fetchJson<ThreadPrompts>(`/api/threads/${thread.id}/prompts?${params.toString()}`);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
    try {
      return await fetchThreadPromptsFromLogs(thread, codexHome);
    } catch {
      throw new ApiError(
        language === "en"
          ? "The running local connector is too old to expose thread prompts. Restart or download the latest Codex Home Manager local connector, then try again."
          : "当前运行的本机连接器版本过旧，未暴露线程 prompts 接口。请重启或下载最新 Codex Home Manager 本机连接器后再试。",
        404
      );
    }
  }
}

function useSnapshot(codexHome: string, sidebarLimit: number, enabled: boolean) {
  const [snapshot, setSnapshot] = React.useState<Snapshot | null>(null);
  const [isLoading, setIsLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const refresh = React.useCallback(async () => {
    if (!enabled) return;
    setIsLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ codex_home: codexHome, sidebar_limit: String(sidebarLimit) });
      setSnapshot(await fetchJson<Snapshot>(`/api/snapshot?${params.toString()}`));
    } catch (refreshError) {
      setError(refreshError instanceof Error ? refreshError.message : String(refreshError));
    } finally {
      setIsLoading(false);
    }
  }, [codexHome, enabled, sidebarLimit]);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  return { snapshot, isLoading, error, refresh };
}

function useHomeOverview(codexHome: string, enabled: boolean) {
  const [overview, setOverview] = React.useState<HomeOverview | null>(null);
  const [isLoading, setIsLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const refresh = React.useCallback(async () => {
    if (!enabled) return;
    setIsLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ codex_home: codexHome });
      setOverview(await fetchJson<HomeOverview>(`/api/home/overview?${params.toString()}`));
    } catch (refreshError) {
      setError(refreshError instanceof Error ? refreshError.message : String(refreshError));
    } finally {
      setIsLoading(false);
    }
  }, [codexHome, enabled]);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  return { overview, isLoading, error, refresh };
}

function useCapabilities(language: Language, enabled: boolean) {
  const [capabilities, setCapabilities] = React.useState<CapabilityResponse | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const refresh = React.useCallback(async () => {
    if (!enabled) return;
    setError(null);
    try {
      setCapabilities(await fetchJson<CapabilityResponse>(`/api/capabilities?${new URLSearchParams({ lang: language }).toString()}`));
    } catch (refreshError) {
      setError(refreshError instanceof Error ? refreshError.message : String(refreshError));
    }
  }, [enabled, language]);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  return { capabilities, error, refresh };
}

function useDiagnostics(codexHome: string, sidebarLimit: number, language: Language, enabled: boolean) {
  const [report, setReport] = React.useState<DiagnosticsReport | null>(null);
  const [status, setStatus] = React.useState<DiagnosticsLoadStatus>("idle");
  const [isCached, setIsCached] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const reportRef = React.useRef<DiagnosticsReport | null>(null);
  const reportKeyRef = React.useRef<string | null>(null);
  const abortControllerRef = React.useRef<AbortController | null>(null);
  const inFlightRequestRef = React.useRef<{ key: string; controller: AbortController; promise: Promise<DiagnosticsReport | null> } | null>(null);
  const requestSequenceRef = React.useRef(0);
  const requestKey = React.useMemo(
    () => JSON.stringify([codexHome, sidebarLimit, language]),
    [codexHome, language, sidebarLimit]
  );

  const refresh = React.useCallback((options: { force?: boolean } = {}): Promise<DiagnosticsReport | null> => {
    if (!enabled) return Promise.resolve(null);
    const existingRequest = inFlightRequestRef.current;
    if (!options.force && existingRequest?.key === requestKey && !existingRequest.controller.signal.aborted) {
      return existingRequest.promise;
    }

    const cachedEntry = diagnosticsReportCache.get(requestKey);
    if (!options.force && cachedEntry && Date.now() - cachedEntry.cachedAtMs <= diagnosticsCacheTtlMs) {
      reportRef.current = cachedEntry.report;
      setReport(cachedEntry.report);
      setStatus("ready");
      setIsCached(true);
      setError(null);
      return Promise.resolve(cachedEntry.report);
    }

    abortControllerRef.current?.abort();
    const controller = new AbortController();
    const requestSequence = ++requestSequenceRef.current;
    abortControllerRef.current = controller;
    const currentReport = reportKeyRef.current === requestKey ? reportRef.current : null;
    if (!currentReport && !cachedEntry) {
      reportRef.current = null;
      reportKeyRef.current = null;
      setReport(null);
    }
    setStatus(currentReport || cachedEntry ? "refreshing" : "loading");
    setIsCached(Boolean(cachedEntry));
    setError(null);

    const requestPromise = (async () => {
      try {
        const params = new URLSearchParams({ codex_home: codexHome, sidebar_limit: String(sidebarLimit), lang: language });
        if (options.force) params.set("refresh", "true");
        const nextReport = await fetchJson<DiagnosticsReport>(`/api/diagnostics?${params.toString()}`, { signal: controller.signal });
        if (controller.signal.aborted || requestSequence !== requestSequenceRef.current) return null;
        diagnosticsReportCache.set(requestKey, { report: nextReport, cachedAtMs: Date.now() });
        reportRef.current = nextReport;
        reportKeyRef.current = requestKey;
        setReport(nextReport);
        setStatus("ready");
        setIsCached(false);
        return nextReport;
      } catch (refreshError) {
        if (controller.signal.aborted || (refreshError instanceof DOMException && refreshError.name === "AbortError")) return null;
        if (requestSequence === requestSequenceRef.current) {
          setError(refreshError instanceof Error ? refreshError.message : String(refreshError));
          setStatus("error");
        }
        return null;
      } finally {
        if (inFlightRequestRef.current?.controller === controller) inFlightRequestRef.current = null;
        if (abortControllerRef.current === controller) abortControllerRef.current = null;
      }
    })();
    inFlightRequestRef.current = { key: requestKey, controller, promise: requestPromise };
    return requestPromise;
  }, [codexHome, enabled, language, requestKey, sidebarLimit]);

  React.useEffect(() => {
    if (!enabled) {
      abortControllerRef.current?.abort();
      setStatus(reportRef.current ? "ready" : "idle");
      return undefined;
    }
    void refresh();
    return () => abortControllerRef.current?.abort();
  }, [enabled, refresh]);

  return { report, status, isLoading: status === "loading" || status === "refreshing", isCached, error, refresh };
}

function StatCard({
  label,
  value,
  sublabel,
  tone
}: {
  label: string;
  value: string | number;
  sublabel: string;
  tone?: "green" | "amber" | "red" | "blue";
}) {
  const { t } = useI18n();
  return (
    <section className={`metric-card ${tone ? `metric-${tone}` : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <em>{sublabel}</em>
    </section>
  );
}

type VisualTone = "green" | "amber" | "red" | "blue" | "neutral";

type StatusVisualRow = {
  key: string;
  label: string;
  value: number;
  tone: VisualTone;
};

function percentOf(value: number, total: number): number {
  if (!total || value <= 0) return 0;
  return Math.round((value / total) * 100);
}

function barWidth(value: number, maximum: number): string {
  if (!maximum || value <= 0) return "0%";
  return `${Math.max(3, Math.round((value / maximum) * 100))}%`;
}

function formatTokenDateLabel(date: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  return match ? `${match[2]}/${match[3]}` : date;
}

type DailyTokenRangeMode = "all" | "last30" | "last90" | "active";
type DailyTokenMetricMode = "total" | "own" | "child";
type DailyTokenDay = ThreadDailyTokenUsage["days"][number] & { hasData: boolean };

function parseTokenDateMs(date: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const time = Date.UTC(year, month - 1, day);
  return Number.isFinite(time) ? time : null;
}

function tokenDateFromMs(timestampMs: number): string {
  return new Date(timestampMs).toISOString().slice(0, 10);
}

function completeDailyTokenDays(days: ThreadDailyTokenUsage["days"]): DailyTokenDay[] {
  const sortedDays = [...days].sort((left, right) => left.date.localeCompare(right.date));
  if (!sortedDays.length) return [];
  const firstMs = parseTokenDateMs(sortedDays[0].date);
  const lastMs = parseTokenDateMs(sortedDays[sortedDays.length - 1].date);
  if (firstMs === null || lastMs === null || lastMs < firstMs) {
    return sortedDays.map((day) => ({
      ...day,
      hasData: Boolean(day.hasData ?? day.totalTokens > 0),
      ownUnknownTokenThreads: day.ownUnknownTokenThreads || 0,
      childUnknownTokenThreads: day.childUnknownTokenThreads || 0,
      unknownTokenThreads: day.unknownTokenThreads || 0,
      hasUnknownTokens: Boolean(day.hasUnknownTokens ?? (day.unknownTokenThreads || 0) > 0)
    }));
  }
  const byDate = new Map(sortedDays.map((day) => [day.date, day]));
  const completed: DailyTokenDay[] = [];
  for (let timestampMs = firstMs; timestampMs <= lastMs; timestampMs += 24 * 60 * 60 * 1000) {
    const date = tokenDateFromMs(timestampMs);
    const day = byDate.get(date);
    completed.push(day ? {
      ...day,
      hasData: Boolean(day.hasData ?? day.totalTokens > 0),
      ownUnknownTokenThreads: day.ownUnknownTokenThreads || 0,
      childUnknownTokenThreads: day.childUnknownTokenThreads || 0,
      unknownTokenThreads: day.unknownTokenThreads || 0,
      hasUnknownTokens: Boolean(day.hasUnknownTokens ?? (day.unknownTokenThreads || 0) > 0)
    } : {
      date,
      ownTokens: 0,
      childTokens: 0,
      totalTokens: 0,
      ownTokenEvents: 0,
      childTokenEvents: 0,
      ownUnknownTokenThreads: 0,
      childUnknownTokenThreads: 0,
      unknownTokenThreads: 0,
      hasData: false,
      hasUnknownTokens: false
    });
  }
  return completed;
}

function dailyTokenMetricValue(day: DailyTokenDay, mode: DailyTokenMetricMode): number {
  if (mode === "own") return day.ownTokens;
  if (mode === "child") return day.childTokens;
  return day.totalTokens;
}

function dailyTokenUnknownThreadCount(day: DailyTokenDay, mode: DailyTokenMetricMode): number {
  if (mode === "own") return day.ownUnknownTokenThreads || 0;
  if (mode === "child") return day.childUnknownTokenThreads || 0;
  return day.unknownTokenThreads || 0;
}

function tokenValueOrUnknownLabel(value: number, unknownThreads: number, compactCount: (value: number) => string, t: Translator): string {
  if (value > 0) return compactCount(value);
  return unknownThreads > 0 ? t("不确定") : compactCount(0);
}

function DailyTokenUsageChart({
  usage,
  isLoading,
  onLoad
}: {
  usage: ThreadDailyTokenUsage | undefined;
  isLoading: boolean;
  onLoad: (force?: boolean) => void;
}) {
  const { t, language } = useI18n();
  const [isExpanded, setIsExpanded] = React.useState(true);
  const [rangeMode, setRangeMode] = React.useState<DailyTokenRangeMode>("all");
  const [metricMode, setMetricMode] = React.useState<DailyTokenMetricMode>("total");
  const [selectedDate, setSelectedDate] = React.useState<string | null>(null);
  const autoLoadRequestedRef = React.useRef(false);
  const days = React.useMemo(() => [...(usage?.days || [])].sort((left, right) => left.date.localeCompare(right.date)), [usage]);
  const summary = usage?.summary;
  const completedDays = React.useMemo(() => completeDailyTokenDays(days), [days]);
  const activeDays = React.useMemo(() => completedDays.filter((day) => day.hasData || day.totalTokens > 0), [completedDays]);
  const unknownDays = React.useMemo(() => completedDays.filter((day) => (day.unknownTokenThreads || 0) > 0), [completedDays]);
  const activeDayCount = summary?.activeDays ?? summary?.days ?? activeDays.length;
  const rangeDayCount = summary?.rangeDays ?? completedDays.length;
  const unknownDayCount = summary?.unknownDays ?? unknownDays.length;
  const unknownThreadCount = summary?.unknownTokenThreads ?? 0;
  const visibleDays = React.useMemo(() => {
    if (rangeMode === "active") return activeDays;
    if (rangeMode === "last30") return completedDays.slice(-30);
    if (rangeMode === "last90") return completedDays.slice(-90);
    return completedDays;
  }, [activeDays, completedDays, rangeMode]);
  const visibleDateKey = visibleDays.map((day) => day.date).join("|");
  const maxDailyTokens = Math.max(
    1,
    ...visibleDays.map((day) => dailyTokenMetricValue(day, metricMode))
  );
  const selectedDay = selectedDate ? completedDays.find((day) => day.date === selectedDate) : null;
  const topDays = [...activeDays]
    .sort((left, right) => right.totalTokens - left.totalTokens || right.date.localeCompare(left.date))
    .slice(0, 8);
  const countedEvents = (summary?.ownCountedTokenEvents || 0) + (summary?.childCountedTokenEvents || 0);
  const compactCount = React.useCallback((value: number) => formatCompactCount(value, language), [language]);
  const activityDayCount = completedDays.filter((day) => day.totalTokens > 0 || (day.unknownTokenThreads || 0) > 0).length;
  const windowLabel = usage ? `${t("活动日期")} ${formatCount(activityDayCount)} / ${t("可审计日")} ${formatCount(activeDayCount)} / ${t("不确定日")} ${formatCount(unknownDayCount)} / ${t("范围")} ${formatCount(rangeDayCount)} ${t("天")}` : isLoading ? t("正在读取每日 Token 时间线") : t("按需读取");

  React.useEffect(() => {
    setIsExpanded(true);
    if (usage || isLoading || autoLoadRequestedRef.current) return;
    autoLoadRequestedRef.current = true;
    onLoad(false);
  }, [isLoading, onLoad, usage]);

  React.useEffect(() => {
    if (!usage) return;
    const nextSelectedDate = summary?.peakDate || activeDays[activeDays.length - 1]?.date || completedDays[completedDays.length - 1]?.date || null;
    setSelectedDate(nextSelectedDate);
  }, [activeDays, completedDays, summary?.peakDate, usage]);

  React.useEffect(() => {
    if (!visibleDays.length) return;
    if (selectedDate && visibleDays.some((day) => day.date === selectedDate)) return;
    const peakInWindow = summary?.peakDate && visibleDays.some((day) => day.date === summary.peakDate) ? summary.peakDate : null;
    setSelectedDate(peakInWindow || visibleDays[visibleDays.length - 1].date);
  }, [selectedDate, summary?.peakDate, visibleDateKey, visibleDays]);

  const openPanel = () => {
    setIsExpanded(true);
    if (!usage && !isLoading) onLoad(false);
  };

  const refreshPanel = () => {
    setIsExpanded(true);
    if (!isLoading) onLoad(true);
  };

  const togglePanel = () => {
    if (isExpanded) {
      setIsExpanded(false);
      return;
    }
    openPanel();
  };

  const metricCards = [
    { label: t("活动日期"), value: summary ? formatCount(activityDayCount) : "-", title: "", sublabel: `${t("可审计日")} ${formatCount(activeDayCount)} / ${t("不确定日")} ${formatCount(unknownDayCount)}` },
    { label: t("覆盖范围"), value: usage ? `${formatCount(rangeDayCount)} ${t("天")}` : "-", title: "", sublabel: summary?.firstDate && summary.lastDate ? `${summary.firstDate} - ${summary.lastDate}` : "-" },
    { label: t("可审计合计"), value: summary ? compactCount(summary.totalTokens) : "-", title: summary ? formatCount(summary.totalTokens) : "", sublabel: `${t("自身消耗")} ${compactCount(summary?.ownTokens || 0)} / ${t("子线程消耗")} ${compactCount(summary?.childTokens || 0)}` },
    { label: t("消耗不确定"), value: summary ? `${formatCount(unknownDayCount)} ${t("天")}` : "-", title: "", sublabel: `${t("缺少 token_count")} · ${formatCount(unknownThreadCount)} ${t("缺少 token_count 的线程")}` },
    { label: t("峰值日"), value: summary?.peakDate || "-", title: summary ? formatCount(summary.peakTokens || 0) : "", sublabel: `${t("峰值 Tokens")} ${compactCount(summary?.peakTokens || 0)}` },
    { label: t("计入事件"), value: compactCount(countedEvents), title: formatCount(countedEvents), sublabel: `${t("跳过重复事件")} ${compactCount(summary?.zeroDeltaTokenEvents || 0)} / ${t("消耗不确定")} ${compactCount(unknownThreadCount)}` }
  ];
  const rangeOptions: Array<{ mode: DailyTokenRangeMode; label: string }> = [
    { mode: "all", label: t("全部范围") },
    { mode: "last90", label: t("最近 90 天") },
    { mode: "last30", label: t("最近 30 天") },
    { mode: "active", label: t("只看可审计日") }
  ];
  const metricOptions: Array<{ mode: DailyTokenMetricMode; label: string }> = [
    { mode: "total", label: t("合计") },
    { mode: "own", label: t("自身") },
    { mode: "child", label: t("子线程") }
  ];
  const tableDays = (rangeMode === "active" ? activeDays : visibleDays).slice().reverse();

  return (
    <section className="thread-detail-card daily-token-card">
      <div className="panel-title-row">
        <div>
          <h3>{t("每日 Token 消耗")}</h3>
          <p>{t("打开后再读取完整时间线，避免大线程详情被阻塞。")}</p>
        </div>
        <div className="daily-token-actions">
          <button className="text-button compact" disabled={isLoading} onClick={refreshPanel} type="button">
            <RefreshCcw size={15} />
            {t("刷新")}
          </button>
          <button className="text-button compact" onClick={togglePanel} type="button">
            <ChevronDown className={isExpanded ? "rotate-open" : ""} size={15} />
            {isExpanded ? t("收起图表") : usage ? t("展开图表") : t("读取并展开")}
          </button>
          <span>{windowLabel}</span>
        </div>
      </div>
      {!isExpanded ? (
        <div className="daily-token-placeholder">
          <strong>{usage ? t("已读取每日 Token 时间线") : t("读取每日 Token 时间线")}</strong>
          <span>{t("这块放在详情末尾并按需加载；展开后可切换范围、点击日期、查看来源和完整明细。")}</span>
        </div>
      ) : null}
      {isExpanded && isLoading ? <div className="panel-loading compact" role="status" aria-live="polite">{t("读取线程详情...")}</div> : null}
      {isExpanded && !isLoading && usage && !days.length ? <div className="visual-empty">{t("无 token_count 记录")}</div> : null}
      {isExpanded && !isLoading && days.length ? (
        <>
          <div className="daily-token-controls">
            <div>
              <span>{t("日期范围")}</span>
              <div className="segmented-control compact">
                {rangeOptions.map((option) => (
                  <button className={rangeMode === option.mode ? "active" : ""} key={option.mode} onClick={() => setRangeMode(option.mode)} type="button">{option.label}</button>
                ))}
              </div>
            </div>
            <div>
              <span>{t("显示指标")}</span>
              <div className="segmented-control compact">
                {metricOptions.map((option) => (
                  <button className={metricMode === option.mode ? "active" : ""} key={option.mode} onClick={() => setMetricMode(option.mode)} type="button">{option.label}</button>
                ))}
              </div>
            </div>
          </div>
          <div className="daily-token-metrics">
            {metricCards.map((card) => (
              <article key={card.label} title={card.title || undefined}>
                <span>{card.label}</span>
                <strong>{card.value}</strong>
                <em>{card.sublabel}</em>
              </article>
            ))}
          </div>
          <div className="daily-token-chart-note">{t("可点击柱子选择日期，横向滚动查看更多日期。")}</div>
          <div className="daily-token-chart" role="list" aria-label={t("含子线程每日合计")}>
            {visibleDays.map((day) => {
              const metricValue = dailyTokenMetricValue(day, metricMode);
              const ownHeight = metricMode === "child" ? 0 : day.ownTokens > 0 ? Math.max(2, Math.round((day.ownTokens / maxDailyTokens) * 100)) : 0;
              const childHeight = metricMode === "own" ? 0 : day.childTokens > 0 ? Math.max(2, Math.round((day.childTokens / maxDailyTokens) * 100)) : 0;
              const isSelected = selectedDate === day.date;
              const unknownThreads = dailyTokenUnknownThreadCount(day, metricMode);
              const hasUnknown = unknownThreads > 0;
              const isUnknownOnly = metricValue <= 0 && hasUnknown;
              return (
                <button
                  className={`daily-token-column${isSelected ? " selected" : ""}${metricValue <= 0 && !hasUnknown ? " empty" : ""}${hasUnknown ? " unknown" : ""}${isUnknownOnly ? " unknown-only" : ""}`}
                  key={day.date}
                  onClick={() => setSelectedDate(day.date)}
                  title={`${day.date}: ${t("可审计合计")} ${formatCount(day.totalTokens)} Tokens (${t("自身消耗")} ${formatCount(day.ownTokens)}, ${t("子线程消耗")} ${formatCount(day.childTokens)})${hasUnknown ? ` · ${t("消耗不确定")} (${formatCount(unknownThreads)} ${t("缺少 token_count 的线程")})` : ""}`}
                  type="button"
                >
                  <div className="daily-token-bar">
                    {hasUnknown ? <span className="daily-token-unknown-marker" /> : null}
                    {day.ownTokens > 0 ? <span className="daily-token-segment own" style={{ height: `${ownHeight}%` }} /> : null}
                    {day.childTokens > 0 ? <span className="daily-token-segment child" style={{ height: `${childHeight}%` }} /> : null}
                  </div>
                  <span>{formatTokenDateLabel(day.date)}</span>
                </button>
              );
            })}
          </div>
          <div className="daily-token-legend">
            <span><i className="visual-tone-green" />{t("自身消耗")}</span>
            <span><i className="visual-tone-amber" />{t("子线程消耗")}</span>
            <span><i className="visual-tone-reference outline" />{t("消耗不确定")}</span>
          </div>
          {selectedDay ? (
            <div className="daily-token-selected">
              {(() => {
                const selectedUnknownThreads = dailyTokenUnknownThreadCount(selectedDay, "total");
                const selectedOwnUnknownThreads = dailyTokenUnknownThreadCount(selectedDay, "own");
                const selectedChildUnknownThreads = dailyTokenUnknownThreadCount(selectedDay, "child");
                const selectedTokenEvents = (selectedDay.ownTokenEvents || 0) + (selectedDay.childTokenEvents || 0);
                return (
                  <>
              <article>
                <span>{t("选中日期")}</span>
                <strong>{selectedDay.date}</strong>
                <em title={selectedDay.totalTokens > 0 ? formatCount(selectedDay.totalTokens) : ""}>
                  {selectedDay.totalTokens > 0
                    ? `${compactCount(selectedDay.totalTokens)} Tokens`
                    : selectedUnknownThreads > 0 ? t("消耗不确定") : t("无可审计消耗")}
                </em>
              </article>
              <article>
                <span>{t("自身消耗")}</span>
                <strong title={selectedDay.ownTokens > 0 ? formatCount(selectedDay.ownTokens) : ""}>{tokenValueOrUnknownLabel(selectedDay.ownTokens, selectedOwnUnknownThreads, compactCount, t)}</strong>
                <em>{compactCount(selectedDay.ownTokenEvents || 0)} {t("token_count 事件")}{selectedOwnUnknownThreads > 0 ? ` · ${formatCount(selectedOwnUnknownThreads)} ${t("缺少 token_count 的线程")}` : ""}</em>
              </article>
              <article>
                <span>{t("子线程消耗")}</span>
                <strong title={selectedDay.childTokens > 0 ? formatCount(selectedDay.childTokens) : ""}>{tokenValueOrUnknownLabel(selectedDay.childTokens, selectedChildUnknownThreads, compactCount, t)}</strong>
                <em>{compactCount(selectedDay.childTokenEvents || 0)} {t("token_count 事件")}{selectedChildUnknownThreads > 0 ? ` · ${formatCount(selectedChildUnknownThreads)} ${t("缺少 token_count 的线程")}` : ""}</em>
              </article>
              <article>
                <span>{t("来源")}</span>
                <strong>
                  {selectedTokenEvents > 0
                    ? selectedUnknownThreads > 0 ? t("token_count 可审计 + 部分不确定") : t("token_count 可审计")
                    : selectedUnknownThreads > 0 ? t("缺少 token_count") : t("无可审计消耗")}
                </strong>
                <em>{selectedUnknownThreads > 0 ? `${formatCount(selectedUnknownThreads)} ${t("缺少 token_count 的线程")}` : t("无可审计数值")}</em>
              </article>
                  </>
                );
              })()}
            </div>
          ) : null}
          <div className="daily-token-top-list">
            <strong>{t("最高消耗日")}</strong>
            <div>
              {topDays.map((day) => (
                <button className={selectedDate === day.date ? "selected" : ""} key={day.date} onClick={() => setSelectedDate(day.date)} type="button">
                  <span>{day.date}</span>
                  <strong title={formatCount(day.totalTokens)}>{compactCount(day.totalTokens)}</strong>
                  <em title={`${t("自身消耗")} ${formatCount(day.ownTokens)} / ${t("子线程消耗")} ${formatCount(day.childTokens)}`}>{t("自身消耗")} {compactCount(day.ownTokens)} / {t("子线程消耗")} {compactCount(day.childTokens)}</em>
                </button>
              ))}
            </div>
          </div>
          <div className="daily-token-detail-table">
            <div className="daily-token-detail-header">
              <strong>{t("每日明细")}</strong>
              <span>{t("详细表格跟随上方范围；点击行可切换选中日期。")} {formatCount(tableDays.length)}</span>
            </div>
            <div className="daily-token-table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>{t("日期")}</th>
                    <th>{t("总消耗")}</th>
                    <th>{t("自身消耗")}</th>
                    <th>{t("子线程消耗")}</th>
                    <th>{t("事件数")}</th>
                    <th>{t("来源")}</th>
                  </tr>
                </thead>
                <tbody>
                  {tableDays.map((day) => {
                    const dayTokenEvents = (day.ownTokenEvents || 0) + (day.childTokenEvents || 0);
                    const dayUnknownThreads = dailyTokenUnknownThreadCount(day, "total");
                    const dayOwnUnknownThreads = dailyTokenUnknownThreadCount(day, "own");
                    const dayChildUnknownThreads = dailyTokenUnknownThreadCount(day, "child");
                    return (
                      <tr className={selectedDate === day.date ? "selected" : ""} key={day.date} onClick={() => setSelectedDate(day.date)}>
                        <td><code>{day.date}</code></td>
                        <td><strong title={day.totalTokens > 0 ? formatCount(day.totalTokens) : ""}>{tokenValueOrUnknownLabel(day.totalTokens, dayUnknownThreads, compactCount, t)}</strong></td>
                        <td title={day.ownTokens > 0 ? formatCount(day.ownTokens) : ""}>{tokenValueOrUnknownLabel(day.ownTokens, dayOwnUnknownThreads, compactCount, t)}</td>
                        <td title={day.childTokens > 0 ? formatCount(day.childTokens) : ""}>{tokenValueOrUnknownLabel(day.childTokens, dayChildUnknownThreads, compactCount, t)}</td>
                        <td>
                          <strong title={formatCount(dayTokenEvents)}>{compactCount(dayTokenEvents)}</strong>
                          <span>{t("自身事件")} {compactCount(day.ownTokenEvents || 0)} / {t("子线程事件")} {compactCount(day.childTokenEvents || 0)}</span>
                        </td>
                        <td>
                          <span>{compactCount(dayTokenEvents)} {t("token_count 事件")}</span>
                          {dayUnknownThreads > 0 ? <span>{t("消耗不确定")} · {formatCount(dayUnknownThreads)} {t("缺少 token_count 的线程")}</span> : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}

function ThreadVisualSummary({ snapshot }: { snapshot: Snapshot }) {
  const { t } = useI18n();
  const baseStatusRows: StatusVisualRow[] = [
    { key: "visible", label: t("可见"), value: snapshot.threads.filter((thread) => thread.visibility === "visible" || thread.visibility === "hidden_by_initial_limit").length, tone: "green" },
    { key: "hidden", label: t("隐藏"), value: snapshot.threads.filter((thread) => thread.visibility === "hidden").length, tone: "red" },
    { key: "repair", label: t("需修复"), value: snapshot.threads.filter((thread) => thread.visibility === "needs_user_event_repair" || thread.visibility === "missing_file").length, tone: "red" },
    { key: "archived", label: t("已归档"), value: snapshot.threads.filter((thread) => thread.visibility === "archived").length, tone: "neutral" },
    { key: "subagent", label: t("子agent"), value: snapshot.threads.filter((thread) => thread.visibility === "subagent").length, tone: "blue" }
  ];
  const statusRows = baseStatusRows.filter((row) => row.value > 0);
  const totalStatus = statusRows.reduce((total, row) => total + row.value, 0);
  const visiblePercent = percentOf(snapshot.summary.codexVisibleThreads, Math.max(1, snapshot.summary.mainThreads));

  const riskyProjects = snapshot.projects
    .map((project) => ({
      project,
      score: project.needsRepair + (project.emptyButHasHiddenThreads ? 1 : 0),
      repair: project.needsRepair
    }))
    .filter((row) => row.score > 0)
    .sort((left, right) => right.score - left.score || right.project.storageBytes - left.project.storageBytes)
    .slice(0, 4);
  const maxProjectScore = Math.max(1, ...riskyProjects.map((row) => row.score));

  const largestThreads = [...snapshot.threads]
    .filter((thread) => thread.fileSizeBytes > 0)
    .sort((left, right) => right.fileSizeBytes - left.fileSizeBytes || right.tokensUsed - left.tokensUsed)
    .slice(0, 4);
  const maxThreadBytes = Math.max(1, ...largestThreads.map((thread) => thread.fileSizeBytes));

  return (
    <section className="thread-visuals" aria-label={t("线程态势")}>
      <article className="visual-card">
        <div className="visual-card-head">
          <div>
            <span className="eyebrow">{t("线程态势")}</span>
            <h3>{t("状态构成")}</h3>
          </div>
          <strong>{visiblePercent}%</strong>
        </div>
        <div className="stacked-status" aria-label={t("状态构成")}>
          {statusRows.map((row) => (
            <span
              key={row.key}
              className={`visual-tone-${row.tone}`}
              style={{ width: `${percentOf(row.value, totalStatus)}%` }}
              title={`${row.label}: ${formatCount(row.value)}`}
            />
          ))}
        </div>
        <div className="visual-legend">
          {statusRows.map((row) => (
            <span key={row.key}>
              <i className={`visual-tone-${row.tone}`} />
              {row.label} {formatCount(row.value)}
            </span>
          ))}
        </div>
        <p>{t("可见线程占比")} · {t("隐藏/需修复会影响侧边栏可见性")}</p>
      </article>

      <article className="visual-card">
        <div className="visual-card-head">
          <div>
            <span className="eyebrow">{t("项目可见性")}</span>
            <h3>{t("项目风险排行")}</h3>
          </div>
          <em>{t("按异常线程数排序")}</em>
        </div>
        <div className="visual-bars">
          {riskyProjects.length ? riskyProjects.map((row) => (
            <div className="visual-row" key={row.project.path || row.project.label} title={row.project.path}>
              <div className="visual-row-line">
                <span>{row.project.label}</span>
                <strong>{formatCount(row.score)}</strong>
              </div>
              <div className="bar-track"><span className="visual-tone-amber" style={{ width: barWidth(row.score, maxProjectScore) }} /></div>
              <small>{t("需修复")} {formatCount(row.repair)}</small>
            </div>
          )) : <div className="visual-empty">{t("暂无异常项目")}</div>}
        </div>
      </article>

      <article className="visual-card">
        <div className="visual-card-head">
          <div>
            <span className="eyebrow">{t("容量")}</span>
            <h3>{t("存储与 SQLite 原始记录")}</h3>
          </div>
          <em>{t("按 JSONL 大小排序")}</em>
        </div>
        <div className="visual-bars dense">
          {largestThreads.length ? largestThreads.map((thread) => (
            <div className="visual-row" key={thread.id} title={`${thread.title} · ${thread.rolloutPath}`}>
              <div className="visual-row-line">
                <span>{thread.title}</span>
                <strong>{formatBytes(thread.fileSizeBytes)}</strong>
              </div>
              <div className="bar-track"><span className="visual-tone-blue" style={{ width: barWidth(thread.fileSizeBytes, maxThreadBytes) }} /></div>
              <small>{formatCount(thread.tokensUsed)} {t("SQLite tokens_used 原始记录")} · {thread.projectLabel}</small>
            </div>
          )) : <div className="visual-empty">{t("没有存储异常")}</div>}
        </div>
      </article>
    </section>
  );
}

function ProjectRail({
  projects,
  selectedProject,
  projectCounts,
  onSelectProject
}: {
  projects: ProjectRecord[];
  selectedProject: string;
  projectCounts: Record<string, number>;
  onSelectProject: (projectPath: string) => void;
}) {
  const { t } = useI18n();
  const [collapsedGroups, setCollapsedGroups] = React.useState<Record<ProjectKind, boolean>>(() => {
    if (typeof window === "undefined") return { workspace_project: false, conversation: false, other: false };
    try {
      const storedGroups = JSON.parse(window.localStorage.getItem(collapsedProjectGroupsStorageKey) || "{}") as Partial<Record<ProjectKind, boolean>>;
      return {
        workspace_project: Boolean(storedGroups.workspace_project),
        conversation: Boolean(storedGroups.conversation),
        other: Boolean(storedGroups.other)
      };
    } catch {
      return { workspace_project: false, conversation: false, other: false };
    }
  });
  const totalThreads = Object.values(projectCounts).reduce((total, count) => total + count, 0);
  const groups: { kind: ProjectKind; label: string }[] = [
    { kind: "workspace_project", label: t("自建项目") },
    { kind: "conversation", label: t("普通对话") },
    { kind: "other", label: t("其他路径") }
  ];
  const toggleGroup = (kind: ProjectKind) => {
    setCollapsedGroups((currentGroups) => {
      const nextGroups = { ...currentGroups, [kind]: !currentGroups[kind] };
      if (typeof window !== "undefined") {
        window.localStorage.setItem(collapsedProjectGroupsStorageKey, JSON.stringify(nextGroups));
      }
      return nextGroups;
    });
  };
  return (
    <aside className="project-rail">
      <button className={selectedProject === "all" ? "project active" : "project"} onClick={() => onSelectProject("all")}>
        <Folder size={16} />
        <span>{t("全部项目")}</span>
        <strong>{formatCount(totalThreads)}</strong>
      </button>
      <div className="project-list">
        {groups.map((group) => {
          const groupProjects = projects.filter((project) => project.projectKind === group.kind);
          if (!groupProjects.length) return null;
          const isCollapsed = collapsedGroups[group.kind];
          const groupCount = groupProjects.reduce((total, project) => total + (projectCounts[project.path] || 0), 0);
          return (
            <React.Fragment key={group.kind}>
              <button
                className={`rail-section-toggle ${isCollapsed ? "collapsed" : ""}`}
                type="button"
                onClick={() => toggleGroup(group.kind)}
                aria-expanded={!isCollapsed}
              >
                {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                <span>{group.label}</span>
                <strong>{formatCount(groupCount)}</strong>
              </button>
              {!isCollapsed ? groupProjects.map((project) => (
                <button
                  key={project.path || project.label}
                  className={`project ${selectedProject === project.path ? "active" : ""} ${project.projectKind}`}
                  onClick={() => onSelectProject(project.path)}
                  title={project.path}
                >
                  <Folder size={16} />
                  <span>{project.label}</span>
                  <strong className={project.emptyButHasHiddenThreads ? "attention" : ""}>
                    {formatCount(projectCounts[project.path] || 0)}
                  </strong>
                </button>
              )) : null}
            </React.Fragment>
          );
        })}
      </div>
    </aside>
  );
}

function ThreadTable({
  threads,
  sortMode,
  sortDirection,
  selectedThreadId,
  readOnlyMode,
  onSelectThread,
  onOpenFullDetail,
  onSortChange,
  onShowThread,
  onHideThread,
  onRepairThread
}: {
  threads: ThreadRecord[];
  sortMode: SortMode;
  sortDirection: SortDirection;
  selectedThreadId: string | null;
  readOnlyMode: boolean;
  onSelectThread: (thread: ThreadRecord) => void;
  onOpenFullDetail: (thread: ThreadRecord) => void;
  onSortChange: (mode: SortMode) => void;
  onShowThread: (thread: ThreadRecord) => void;
  onHideThread: (thread: ThreadRecord) => void;
  onRepairThread: (thread: ThreadRecord) => void;
}) {
  const { t, formatDate } = useI18n();
  const lastRowClickRef = React.useRef<{ id: string; at: number } | null>(null);
  const handleRowClick = (thread: ThreadRecord, event: React.MouseEvent<HTMLTableRowElement>) => {
    const now = Date.now();
    const lastClick = lastRowClickRef.current;
    onSelectThread(thread);
    if (event.detail >= 2 || (lastClick?.id === thread.id && now - lastClick.at <= 2400)) {
      onOpenFullDetail(thread);
      lastRowClickRef.current = null;
      return;
    }
    lastRowClickRef.current = { id: thread.id, at: now };
  };
  const sortableHeader = (mode: SortMode, label: string) => {
    const isActive = sortMode === mode;
    const Icon = isActive ? (sortDirection === "asc" ? ArrowUp : ArrowDown) : ArrowUpDown;
    return (
      <button
        className={`sortable-header ${isActive ? "active" : ""}`}
        type="button"
        onClick={() => onSortChange(mode)}
        title={`${t("点击排序")} · ${isActive ? (sortDirection === "asc" ? t("升序") : t("降序")) : label}`}
      >
        <span>{label}</span>
        <Icon size={13} />
      </button>
    );
  };
  return (
    <div className="table-wrap">
      <table className="thread-table">
        <colgroup>
          <col className="status-column" />
          <col className="rank-column" />
          <col className="title-column" />
          <col className="project-column" />
          <col className="updated-column" />
          <col className="storage-column" />
          <col className="tokens-column" />
          <col className="actions-column" />
        </colgroup>
        <thead>
          <tr>
            <th>{sortableHeader("status", t("状态"))}</th>
            <th>{sortableHeader("rank", t("位置"))}</th>
            <th>{sortableHeader("title", t("线程"))}</th>
            <th>{sortableHeader("project", t("项目"))}</th>
            <th>{sortableHeader("updated", t("更新时间"))}</th>
            <th>{sortableHeader("size", t("存储"))}</th>
            <th>{sortableHeader("tokens", t("SQLite tokens_used 原始记录"))}</th>
            <th>{t("操作")}</th>
          </tr>
        </thead>
        <tbody>
          {threads.map((thread) => (
            <tr
              key={thread.id}
              className={selectedThreadId === thread.id ? "selected" : ""}
              tabIndex={0}
              aria-label={`${t("完整线程详情")}: ${thread.title}. ${statusLabel(thread.visibility, t)}`}
              aria-selected={selectedThreadId === thread.id}
              onClick={(event) => handleRowClick(thread, event)}
              onDoubleClick={() => onOpenFullDetail(thread)}
              onKeyDown={(event) => {
                if (event.target !== event.currentTarget || (event.key !== "Enter" && event.key !== " ")) return;
                event.preventDefault();
                onOpenFullDetail(thread);
              }}
              title={t("双击查看完整详情")}
            >
              <td>
                <span className={`status ${thread.visibility}`}>
                  {statusIcon(thread.visibility)}
                  <span>{statusLabel(thread.visibility, t)}</span>
                </span>
              </td>
              <td><span className="rank">{thread.recentRank ? `#${thread.recentRank}` : "-"}</span></td>
              <td className="title-cell">
                <strong>{thread.title}</strong>
                <div className="thread-meta">
                  <span>{thread.id}</span>
                  {thread.threadKind === "subagent" ? (
                    <em title={thread.parentThreadId ? `${t("父线程")} ${thread.parentThreadId}` : t("子 agent 线程")}>
                      {thread.agentNickname ? `${t("子agent")} · ${thread.agentNickname}` : t("子agent")}
                    </em>
                  ) : null}
                </div>
              </td>
              <td className="project-cell" title={thread.projectPath}>{thread.projectLabel}</td>
              <td>{formatDate(thread.updatedAtMs)}</td>
              <td className="storage-cell" title={`${t("自身 JSONL")}: ${formatBytes(threadOwnFileSize(thread))} · ${t("子线程存储")}: ${formatBytes(threadChildFileSize(thread))}`}>
                <strong>{formatBytes(threadTotalFileSize(thread))}</strong>
                {threadChildCount(thread) ? (
                  <span>{t("含子线程")} {formatBytes(threadChildFileSize(thread))} / {formatCount(threadChildCount(thread))}</span>
                ) : <span>{t("自身 JSONL")}</span>}
              </td>
              <td className="storage-cell" title={`${t("非账户消耗口径")} · ${t("SQLite 自身")}: ${formatCount(threadOwnTokens(thread))} · ${t("SQLite 子线程合计")}: ${formatCount(threadChildTokens(thread))}`}>
                <strong>{formatCount(threadTotalTokens(thread))}</strong>
                <span>{threadChildCount(thread) ? `${t("SQLite 子线程合计")} ${formatCount(threadChildTokens(thread))}` : t("非账户消耗口径")}</span>
              </td>
              <td>
                <div className="row-actions">
                  {readOnlyMode ? (
                    <span
                      className={thread.archived || thread.visibility === "hidden" || thread.visibility === "needs_user_event_repair" ? "row-readonly-badge attention" : "row-readonly-badge"}
                      title={t("浏览器文件夹模式只能查看、搜索、读取日志和导出 prompt；修复、迁移、瘦身、删除和 MCP 请使用本机连接器。")}
                    >
                      <ServerCog size={14} />
                      <span>{thread.archived || thread.visibility === "hidden" || thread.visibility === "needs_user_event_repair" ? t("需连接器") : t("只读")}</span>
                    </span>
                  ) : thread.archived ? (
                    <button
                      className="row-action-button primary"
                      onMouseDown={(event) => event.stopPropagation()}
                      onClick={(event) => { event.stopPropagation(); onShowThread(thread); }}
                      title={t("恢复到 Codex 侧边栏")}
                      type="button"
                    >
                      <Eye size={15} />
                      <span>{t("恢复")}</span>
                    </button>
                  ) : null}
                  {!readOnlyMode && thread.visibility === "hidden" ? (
                    <button
                      className="row-action-button primary"
                      onMouseDown={(event) => event.stopPropagation()}
                      onClick={(event) => { event.stopPropagation(); onShowThread(thread); }}
                      title={t("清除手动隐藏状态并恢复到 Codex 侧边栏")}
                      type="button"
                    >
                      <Eye size={15} />
                      <span>{t("显示")}</span>
                    </button>
                  ) : null}
                  {!readOnlyMode && thread.codexVisible && !thread.archived ? (
                    <button
                      className="row-action-button"
                      onMouseDown={(event) => event.stopPropagation()}
                      onClick={(event) => { event.stopPropagation(); onHideThread(thread); }}
                      title={t("从 Codex 显示索引隐藏，不归档也不删除 JSONL")}
                      type="button"
                    >
                      <EyeOff size={15} />
                      <span>{t("隐藏")}</span>
                    </button>
                  ) : null}
                  {!readOnlyMode && thread.visibility === "needs_user_event_repair" ? (
                    <button
                      className="row-action-button"
                      onMouseDown={(event) => event.stopPropagation()}
                      onClick={(event) => { event.stopPropagation(); onRepairThread(thread); }}
                      title={t("修复元数据并恢复到 Codex 侧边栏")}
                      type="button"
                    >
                      <Wrench size={15} />
                      <span>{t("修复显示")}</span>
                    </button>
                  ) : null}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {threads.length === 0 ? <div className="empty-state">{t("没有匹配的线程")}</div> : null}
    </div>
  );
}

function DetailPanel({
  isOpen,
  detail,
  isLoading,
  readOnlyMode,
  onToggleOpen,
  onBackup,
  onRestore,
  onExportPrompts,
  onViewPrompts,
  onViewLogs,
  onShowThread,
  onDuplicate,
  onHideThread,
  onArchive,
  onSlim,
  onMigrate,
  duplicateTargetPath,
  onDuplicateTargetPathChange,
  slimRemoveImages,
  onSlimRemoveImagesChange,
  slimKeepLatestCompacted,
  onSlimKeepLatestCompactedChange,
  migrateTargetPath,
  onMigrateTargetPathChange,
  renameSourcePath,
  renameTargetPath,
  renameFolder,
  onRenameSourcePathChange,
  onRenameTargetPathChange,
  onRenameFolderChange,
  onRenameProject
}: {
  isOpen: boolean;
  detail: ThreadDetail | null;
  isLoading: boolean;
  readOnlyMode: boolean;
  onToggleOpen: () => void;
  onBackup: (thread: ThreadRecord) => void;
  onRestore: (backup: BackupRecord) => void;
  onExportPrompts: (thread: ThreadRecord) => void;
  onViewPrompts: (thread: ThreadRecord) => void;
  onViewLogs: (thread: ThreadRecord) => void;
  onShowThread: (thread: ThreadRecord) => void;
  onDuplicate: (thread: ThreadRecord) => void;
  onHideThread: (thread: ThreadRecord) => void;
  onArchive: (thread: ThreadRecord) => void;
  onSlim: (thread: ThreadRecord) => void;
  onMigrate: (thread: ThreadRecord) => void;
  duplicateTargetPath: string;
  onDuplicateTargetPathChange: (value: string) => void;
  slimRemoveImages: boolean;
  onSlimRemoveImagesChange: (value: boolean) => void;
  slimKeepLatestCompacted: boolean;
  onSlimKeepLatestCompactedChange: (value: boolean) => void;
  migrateTargetPath: string;
  onMigrateTargetPathChange: (value: string) => void;
  renameSourcePath: string;
  renameTargetPath: string;
  renameFolder: boolean;
  onRenameSourcePathChange: (value: string) => void;
  onRenameTargetPathChange: (value: string) => void;
  onRenameFolderChange: (value: boolean) => void;
  onRenameProject: () => void;
}) {
  const { t, formatDate } = useI18n();
  const [panelWidth, setPanelWidth] = React.useState(() => {
    if (typeof window === "undefined") return defaultDetailPanelWidth;
    const storedPanelWidth = window.localStorage.getItem(detailPanelWidthStorageKey);
    const storedWidth = storedPanelWidth === null ? Number.NaN : Number(storedPanelWidth);
    return clampDetailPanelWidth(Number.isFinite(storedWidth) ? storedWidth : defaultDetailPanelWidth);
  });
  const resizeStateRef = React.useRef<{ startX: number; startWidth: number } | null>(null);
  const latestPanelWidthRef = React.useRef(panelWidth);
  const [isResizing, setIsResizing] = React.useState(false);

  React.useEffect(() => {
    latestPanelWidthRef.current = panelWidth;
  }, [panelWidth]);

  const savePanelWidth = React.useCallback((width: number) => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(detailPanelWidthStorageKey, String(width));
  }, []);

  const applyPanelWidth = React.useCallback((width: number, persist = false) => {
    const clampedWidth = clampDetailPanelWidth(width);
    latestPanelWidthRef.current = clampedWidth;
    setPanelWidth(clampedWidth);
    if (persist) savePanelWidth(clampedWidth);
    return clampedWidth;
  }, [savePanelWidth]);

  React.useEffect(() => {
    if (!isResizing) return;

    const handlePointerMove = (event: PointerEvent) => {
      const resizeState = resizeStateRef.current;
      if (!resizeState) return;
      applyPanelWidth(resizeState.startWidth + resizeState.startX - event.clientX);
    };
    const finishResize = () => {
      resizeStateRef.current = null;
      setIsResizing(false);
      savePanelWidth(latestPanelWidthRef.current);
    };

    document.body.classList.add("resizing-detail-panel");
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", finishResize, { once: true });
    window.addEventListener("pointercancel", finishResize, { once: true });
    return () => {
      document.body.classList.remove("resizing-detail-panel");
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", finishResize);
      window.removeEventListener("pointercancel", finishResize);
    };
  }, [applyPanelWidth, isResizing, savePanelWidth]);

  React.useEffect(() => {
    const handleWindowResize = () => applyPanelWidth(latestPanelWidthRef.current, true);
    window.addEventListener("resize", handleWindowResize);
    return () => window.removeEventListener("resize", handleWindowResize);
  }, [applyPanelWidth]);

  const handleResizePointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    resizeStateRef.current = {
      startX: event.clientX,
      startWidth: event.currentTarget.closest(".detail-panel")?.getBoundingClientRect().width ?? panelWidth
    };
    latestPanelWidthRef.current = resizeStateRef.current.startWidth;
    setIsResizing(true);
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const handleResizeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      applyPanelWidth(panelWidth + 40, true);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      applyPanelWidth(panelWidth - 40, true);
    } else if (event.key === "Home") {
      event.preventDefault();
      applyPanelWidth(minimumDetailPanelWidth, true);
    } else if (event.key === "End") {
      event.preventDefault();
      applyPanelWidth(maximumDetailPanelWidth, true);
    }
  };

  if (!isOpen) {
    return null;
  }

  const resizeHandle = (
    <div
      className="detail-resize-handle"
      role="separator"
      aria-label={t("调整详情面板宽度")}
      aria-orientation="vertical"
      aria-valuemin={minimumDetailPanelWidth}
      aria-valuemax={maximumDetailPanelWidth}
      aria-valuenow={panelWidth}
      tabIndex={0}
      onPointerDown={handleResizePointerDown}
      onKeyDown={handleResizeKeyDown}
      title={t("调整详情面板宽度")}
    >
      <span className="detail-resize-grip" aria-hidden="true" />
      <span className="detail-resize-hint">{t("拖拽或用左右方向键调整宽度")}</span>
    </div>
  );
  const panelStyle = {
    "--detail-panel-width": `${panelWidth}px`
  } as React.CSSProperties & { "--detail-panel-width": string };
  const renderPanel = (content: React.ReactNode) => (
    <aside className={`detail-panel expanded${isResizing ? " resizing" : ""}`} style={panelStyle}>
      {resizeHandle}
      {content}
    </aside>
  );

  const panelHeader = (
    <div className="inspector-header">
      <span>{t("线程详情")}</span>
      <button className="icon-button" onClick={onToggleOpen} title={t("收起详情面板")} aria-label={t("收起详情面板")} type="button">
        <PanelRightClose size={15} />
      </button>
    </div>
  );

  if (isLoading) {
    return renderPanel(<>{panelHeader}<div className="panel-loading" role="status" aria-live="polite">{t("读取线程详情...")}</div></>);
  }
  if (!detail) {
    return renderPanel(
      <>
        {panelHeader}
        <div className="empty-detail">
          <FileJson size={28} />
          <span>{t("选择线程后可查看位置、存储、备份和危险操作")}</span>
        </div>
      </>
    );
  }

  const { thread, rolloutStats, backups } = detail;
  return renderPanel(
    <>
      {panelHeader}
      <div className="detail-heading">
        <span className={`status ${thread.visibility}`}>{statusIcon(thread.visibility)}{statusLabel(thread.visibility, t)}</span>
        <h2>{thread.title}</h2>
        <p>{thread.id}</p>
        {thread.sidebarTitle && thread.sqliteTitle && thread.sidebarTitle !== thread.sqliteTitle ? (
          <p>SQLite title: {thread.sqliteTitle}</p>
        ) : null}
        {thread.sessionIndexTitle && thread.sessionIndexTitle !== thread.sidebarTitle ? (
          <p>session_index title: {thread.sessionIndexTitle}</p>
        ) : null}
        {thread.threadKind === "subagent" ? (
          <p>{thread.agentNickname ? `${t("子agent")}: ${thread.agentNickname}` : t("子agent")}{thread.parentThreadId ? ` · ${t("父线程")} ${thread.parentThreadId}` : ""}</p>
        ) : null}
      </div>
      <div className="detail-actions">
        {!readOnlyMode ? <button onClick={() => onBackup(thread)}><ShieldCheck size={15} /> {t("创建备份")}</button> : null}
        {!readOnlyMode && backups[0] ? <button onClick={() => onRestore(backups[0])}><RotateCcw size={15} /> {t("回滚最近备份")}</button> : null}
        <button onClick={() => onViewPrompts(thread)}><BookOpen size={15} /> {t("查看 prompts")}</button>
        <button onClick={() => onExportPrompts(thread)}><Download size={15} /> {t("导出 prompts")}</button>
        <button onClick={() => onViewLogs(thread)}><FileText size={15} /> {t("详细日志")}</button>
        {!readOnlyMode && !thread.codexVisible && !thread.archived && thread.threadKind === "main" && thread.fileExists ? (
          <button onClick={() => onShowThread(thread)}><Eye size={15} /> {t("显示线程")}</button>
        ) : null}
        {!readOnlyMode && thread.codexVisible && !thread.archived ? (
          <button onClick={() => onHideThread(thread)}><EyeOff size={15} /> {t("隐藏线程")}</button>
        ) : null}
        {!readOnlyMode ? <button className="danger-action" onClick={() => onArchive(thread)}><Trash2 size={15} /> {t("安全删除")}</button> : null}
      </div>

      {readOnlyMode ? (
        <section className="browser-readonly-callout">
          <ServerCog size={16} />
          <div>
            <strong>{t("浏览器只读")}</strong>
            <span>{t("浏览器文件夹模式只能查看、搜索、读取日志和导出 prompt；修复、迁移、瘦身、删除和 MCP 请使用本机连接器。")}</span>
          </div>
        </section>
      ) : (
        <>
          <section className="operation-section">
            <h3>{t("复制线程")}</h3>
            <label>{t("目标项目路径")}</label>
            <input aria-label={t("目标项目路径")} value={duplicateTargetPath} onChange={(event) => onDuplicateTargetPathChange(event.target.value)} />
            <button onClick={() => onDuplicate(thread)}><Copy size={15} /> {t("复制到目标项目")}</button>
          </section>

          <section className="operation-section">
            <h3>{t("线程瘦身范围")}</h3>
            <label className="checkbox-line">
              <input type="checkbox" checked={slimRemoveImages} onChange={(event) => onSlimRemoveImagesChange(event.target.checked)} />
              {t("移除嵌入图片和 data:image 内容")}
            </label>
            <label className="checkbox-line">
              <input type="checkbox" checked={slimKeepLatestCompacted} onChange={(event) => onSlimKeepLatestCompactedChange(event.target.checked)} />
              {t("只保留最新 compacted checkpoint")}
            </label>
            <button onClick={() => onSlim(thread)}><Scissors size={15} /> {t("按选定范围瘦身")}</button>
          </section>

          <section className="operation-section">
            <h3>{t("迁移线程")}</h3>
            <input aria-label={t("目标项目路径")} value={migrateTargetPath} onChange={(event) => onMigrateTargetPathChange(event.target.value)} />
            <button onClick={() => onMigrate(thread)}><MoveRight size={15} /> {t("迁移到目标项目")}</button>
          </section>

          <section className="operation-section">
            <h3>{t("项目及文件夹改名")}</h3>
            <label>{t("原项目路径")}</label>
            <input aria-label={t("原项目路径")} value={renameSourcePath} onChange={(event) => onRenameSourcePathChange(event.target.value)} />
            <label>{t("目标项目路径")}</label>
            <input aria-label={t("目标项目路径")} value={renameTargetPath} onChange={(event) => onRenameTargetPathChange(event.target.value)} />
            <label className="checkbox-line">
              <input type="checkbox" checked={renameFolder} onChange={(event) => onRenameFolderChange(event.target.checked)} />
              {t("同步重命名本地文件夹")}
            </label>
            <button onClick={onRenameProject}><FolderPen size={15} /> {t("执行项目重命名")}</button>
          </section>
        </>
      )}

      <section className="detail-section">
        <h3>{t("位置")}</h3>
        <label>{t("项目路径")}</label>
        <code>{thread.projectPath}</code>
        <label>{t("JSONL 路径")}</label>
        <code>{thread.rolloutPath}</code>
        <label>{t("Codex thread/list 排名")}</label>
        <code>{thread.threadListRank ? `#${thread.threadListRank}` : t("不在 thread/list 排序中")}</code>
        <label>{t("session_index 排名")}</label>
        <code>{thread.sessionIndexRank ? `#${thread.sessionIndexRank}` : t("不在 session_index 修复窗口中")}</code>
        <label>{t("侧栏显式引用")}</label>
        <code>{thread.explicitSidebarReference ? t("有 pinned/hint 引用") : t("无显式引用")}</code>
        <label>{t("可见对话流")}</label>
        <code>{thread.rolloutDisplayStatus || "not_scanned"} | response {formatCount((thread.rolloutDisplayResponseUserMessages || 0) + (thread.rolloutDisplayResponseAssistantMessages || 0))} / visible {formatCount((thread.rolloutDisplayVisibleUserMessages || 0) + (thread.rolloutDisplayVisibleAgentMessages || 0))}</code>
        {thread.sidebarTitle && thread.sidebarTitle !== thread.sqliteTitle ? (
          <>
            <label>{t("侧边栏标题")}</label>
            <code>{thread.sidebarTitle}</code>
            <label>{t("SQLite 标题")}</label>
            <code>{thread.sqliteTitle}</code>
            {thread.sessionIndexTitle && thread.sessionIndexTitle !== thread.sidebarTitle ? (
              <>
                <label>session_index title</label>
                <code>{thread.sessionIndexTitle}</code>
              </>
            ) : null}
            {thread.rolloutTitle ? (
              <>
                <label>rollout title event</label>
                <code>{thread.rolloutTitleLine ? `#${thread.rolloutTitleLine} ${thread.rolloutTitleTimestamp}` : thread.rolloutTitleTimestamp}</code>
              </>
            ) : null}
          </>
        ) : null}
      </section>

      <section className="detail-grid">
        <div><span>{t("线程类型")}</span><strong>{thread.threadKind === "subagent" ? t("子agent") : t("主线程")}</strong></div>
        <div><span>{t("文件大小")}</span><strong>{formatBytes(thread.fileSizeBytes)}</strong></div>
        <div><span>{t("更新时间")}</span><strong>{formatDate(thread.updatedAtMs)}</strong></div>
        <div><span>{t("JSONL 行数")}</span><strong>{formatCount(rolloutStats.lineCount)}</strong></div>
        <div><span>{t("用户消息")}</span><strong>{formatCount(rolloutStats.userMessages)}</strong></div>
        <div><span>{t("工具调用")}</span><strong>{formatCount(rolloutStats.toolCalls)}</strong></div>
        <div><span>{t("工具输出")}</span><strong>{formatCount(rolloutStats.toolOutputs)}</strong></div>
      </section>

      <section className="detail-section">
        <h3>{t("状态说明")}</h3>
        <div className="reason-list">
          {thread.hiddenReasons.length ? thread.hiddenReasons.map((reason) => <span key={reason}>{localizeInternalLabel(reason, t)}</span>) : <span>{t("无")}</span>}
        </div>
      </section>

      {!readOnlyMode ? <section className="detail-section">
        <h3>{t("备份")}</h3>
        <div className="backup-list">
          {backups.length ? backups.slice(0, 6).map((backup) => (
            <button key={backup.backupId} onClick={() => onRestore(backup)} title={backup.manifestPath}>
              <RotateCcw size={14} />
              <span>{localizeInternalLabel(backup.action, t)}</span>
              <em>{backup.createdAt}</em>
            </button>
          )) : <span className="muted">{t("暂无备份")}</span>}
        </div>
      </section> : null}
    </>
  );
}

function ThreadsModule({
  snapshot,
  isLoading,
  selectedThreadId,
  detail,
  detailLoading,
  dailyTokenLoading,
  onSelectThread,
  onLoadDailyTokens,
  onShowThread,
  onRepairThread,
  onClearSelection,
  onBackup,
  onRestore,
  onExportPrompts,
  onViewPrompts,
  onViewLogs,
  onDuplicate,
  onHideThread,
  onArchive,
  onSlim,
  onMigrate,
  duplicateTargetPath,
  setDuplicateTargetPath,
  slimRemoveImages,
  setSlimRemoveImages,
  slimKeepLatestCompacted,
  setSlimKeepLatestCompacted,
  migrateTargetPath,
  setMigrateTargetPath,
  renameSourcePath,
  renameTargetPath,
  renameFolder,
  setRenameSourcePath,
  setRenameTargetPath,
  setRenameFolder,
  onRenameProject
}: {
  snapshot: Snapshot | null;
  isLoading: boolean;
  selectedThreadId: string | null;
  detail: ThreadDetail | null;
  detailLoading: boolean;
  dailyTokenLoading: boolean;
  onSelectThread: (thread: ThreadRecord) => void;
  onLoadDailyTokens: (thread: ThreadRecord, force?: boolean) => void;
  onShowThread: (thread: ThreadRecord) => void;
  onRepairThread: (thread: ThreadRecord) => void;
  onClearSelection: () => void;
  onBackup: (thread: ThreadRecord) => void;
  onRestore: (backup: BackupRecord) => void;
  onExportPrompts: (thread: ThreadRecord) => void;
  onViewPrompts: (thread: ThreadRecord) => void;
  onViewLogs: (thread: ThreadRecord) => void;
  onDuplicate: (thread: ThreadRecord) => void;
  onHideThread: (thread: ThreadRecord) => void;
  onArchive: (thread: ThreadRecord) => void;
  onSlim: (thread: ThreadRecord) => void;
  onMigrate: (thread: ThreadRecord) => void;
  duplicateTargetPath: string;
  setDuplicateTargetPath: (value: string) => void;
  slimRemoveImages: boolean;
  setSlimRemoveImages: (value: boolean) => void;
  slimKeepLatestCompacted: boolean;
  setSlimKeepLatestCompacted: (value: boolean) => void;
  migrateTargetPath: string;
  setMigrateTargetPath: (value: string) => void;
  renameSourcePath: string;
  renameTargetPath: string;
  renameFolder: boolean;
  setRenameSourcePath: (value: string) => void;
  setRenameTargetPath: (value: string) => void;
  setRenameFolder: (value: boolean) => void;
  onRenameProject: () => void;
}) {
  const { t, formatDate, language } = useI18n();
  const [filterMode, setFilterMode] = React.useState<FilterMode>("all");
  const [threadKindFilter, setThreadKindFilter] = React.useState<ThreadKindFilter>("main");
  const [sortMode, setSortMode] = React.useState<SortMode>("rank");
  const [sortDirection, setSortDirection] = React.useState<SortDirection>(defaultSortDirection("rank"));
  const [searchText, setSearchText] = React.useState("");
  const [selectedProject, setSelectedProject] = React.useState("all");
  const [isInspectorOpen, setIsInspectorOpen] = React.useState(false);
  const [fullDetailThreadId, setFullDetailThreadId] = React.useState<string | null>(null);
  const inspectorOpenTimerRef = React.useRef<number | null>(null);

  const handleSortChange = React.useCallback((mode: SortMode) => {
    setSortMode((currentMode) => {
      if (currentMode === mode) {
        setSortDirection((currentDirection) => currentDirection === "asc" ? "desc" : "asc");
        return currentMode;
      }
      setSortDirection(defaultSortDirection(mode));
      return mode;
    });
  }, []);

  const clearInspectorOpenTimer = React.useCallback(() => {
    if (inspectorOpenTimerRef.current !== null) {
      window.clearTimeout(inspectorOpenTimerRef.current);
      inspectorOpenTimerRef.current = null;
    }
  }, []);

  const selectThreadFromTable = React.useCallback((thread: ThreadRecord) => {
    onSelectThread(thread);
    clearInspectorOpenTimer();
  }, [clearInspectorOpenTimer, onSelectThread]);

  const openFullDetailFromTable = React.useCallback((thread: ThreadRecord) => {
    clearInspectorOpenTimer();
    onSelectThread(thread);
    setIsInspectorOpen(false);
    setFullDetailThreadId(thread.id);
  }, [clearInspectorOpenTimer, onSelectThread]);

  const filteredThreads = React.useMemo(() => {
    if (!snapshot) return [];
    const search = searchText.trim().toLowerCase();
    const threads = snapshot.threads.filter((thread) => {
      if (threadKindFilter !== "all" && thread.threadKind !== threadKindFilter) return false;
      if (selectedProject !== "all" && thread.projectPath !== selectedProject) return false;
      if (!matchesStatusFilter(thread, filterMode, threadKindFilter)) return false;
      if (!search) return true;
      return [
        thread.title,
        thread.sqliteTitle,
        thread.sidebarTitle,
        thread.sessionIndexTitle,
        thread.rolloutTitle,
        thread.id,
        thread.projectPath,
        thread.rolloutPath,
        thread.preview,
        thread.parentThreadId,
        thread.agentNickname
      ]
        .join(" ")
        .toLowerCase()
        .includes(search);
    });
    const sortedThreads = [...threads];
    sortedThreads.sort((left, right) => compareThreadsByMode(left, right, sortMode, sortDirection, language));
    return sortedThreads;
  }, [filterMode, language, searchText, selectedProject, snapshot, sortDirection, sortMode, threadKindFilter]);

  const projectCounts = React.useMemo(() => {
    if (!snapshot) return {};
    return snapshot.projects.reduce<Record<string, number>>((counts, project) => {
      counts[project.path] = snapshot.threads.filter((thread) => {
        if (thread.projectPath !== project.path) return false;
        if (threadKindFilter !== "all" && thread.threadKind !== threadKindFilter) return false;
        return matchesStatusFilter(thread, filterMode, threadKindFilter);
      }).length;
      return counts;
    }, {});
  }, [filterMode, snapshot, threadKindFilter]);

  React.useEffect(() => {
    if (!snapshot || !selectedThreadId) return;
    if (!filteredThreads.some((thread) => thread.id === selectedThreadId)) {
      onClearSelection();
      setIsInspectorOpen(false);
    }
  }, [filteredThreads, onClearSelection, selectedThreadId, snapshot]);

  React.useEffect(() => {
    if (!snapshot || !fullDetailThreadId) return;
    if (!snapshot.threads.some((thread) => thread.id === fullDetailThreadId)) {
      setFullDetailThreadId(null);
    }
  }, [fullDetailThreadId, snapshot]);

  React.useEffect(() => clearInspectorOpenTimer, [clearInspectorOpenTimer]);

  if (!snapshot && isLoading) {
    return <div className="loading-page">{t("读取线程索引...")}</div>;
  }

  if (!snapshot) {
    return <div className="empty-state">{t("没有可用线程数据")}</div>;
  }

  const readOnlyMode = snapshot.codexHome.startsWith("browser://") || snapshot.threads.some((thread) => thread.source === "browser-folder");
  const fullDetailThread = fullDetailThreadId ? snapshot.threads.find((thread) => thread.id === fullDetailThreadId) || null : null;
  const fullDetail = detail && detail.thread.id === fullDetailThreadId ? detail : null;

  return (
    <div className="module-stack thread-module">
      <div className="metrics-row">
        <StatCard label={t("全部线程")} value={formatCount(snapshot.summary.totalThreads)} sublabel={t("SQLite threads")} />
        <StatCard label={t("主线程")} value={formatCount(snapshot.summary.mainThreads)} sublabel={t("默认管理视图")} tone="blue" />
        <StatCard label={t("子agent")} value={formatCount(snapshot.summary.subagentThreads)} sublabel={t("评估和并行任务")} />
        <StatCard label={t("Codex 可见")} value={formatCount(snapshot.summary.codexVisibleThreads)} sublabel={t("普通主线程")} tone="green" />
        <StatCard label={t("异常线程")} value={formatCount(snapshot.summary.needsRepairThreads)} sublabel={t("缺文件或事件流异常")} tone="red" />
        <StatCard label={t("已归档")} value={formatCount(snapshot.summary.archivedThreads)} sublabel={t("归档")} />
        <StatCard label={t("线程存储")} value={formatBytes(snapshot.summary.totalStorageBytes)} sublabel={t("JSONL 合计")} tone="blue" />
      </div>

      <ThreadVisualSummary snapshot={snapshot} />

      <div className={`workspace ${isInspectorOpen ? "inspector-open" : "inspector-collapsed"}`}>
        <ProjectRail projects={snapshot.projects} selectedProject={selectedProject} projectCounts={projectCounts} onSelectProject={setSelectedProject} />
        <section className="main-panel">
          <div className="toolbar">
            <div className="search-box">
              <Search size={16} />
              <input aria-label={t("搜索标题、线程 ID、项目、JSONL 路径")} value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder={t("搜索标题、线程 ID、项目、JSONL 路径")} />
            </div>
            <div className="segmented compact">
              {[
                ["main", t("主线程")],
                ["all", t("全部")],
                ["subagent", t("子agent")]
              ].map(([value, label]) => (
                <button key={value} className={threadKindFilter === value ? "active" : ""} onClick={() => setThreadKindFilter(value as ThreadKindFilter)}>{label}</button>
              ))}
            </div>
            <div className="segmented">
              {[
                ["all", t("全部")],
                ["visible", t("可见")],
                ["manual_hidden", t("隐藏")],
                ["repair", t("需修复")],
                ["archived", t("归档")]
              ].map(([value, label]) => (
                <button key={value} className={filterMode === value ? "active" : ""} onClick={() => setFilterMode(value as FilterMode)}>{label}</button>
              ))}
            </div>
            <div className="sort-control">
              <select
                aria-label={t("按侧边栏位置")}
                value={sortMode}
                onChange={(event) => {
                  const nextMode = event.target.value as SortMode;
                  setSortMode(nextMode);
                  setSortDirection(defaultSortDirection(nextMode));
                }}
              >
                <option value="rank">{t("按侧边栏位置")}</option>
                <option value="updated">{t("按更新时间")}</option>
                <option value="size">{t("按存储空间")}</option>
                <option value="tokens">{t("按 SQLite 原始记录")}</option>
                <option value="project">{t("按项目")}</option>
                <option value="title">{t("按标题")}</option>
                <option value="status">{t("按状态")}</option>
              </select>
            </div>
          </div>
          <div className="result-strip" role="status" aria-live="polite">
            <span>{t("当前结果")} {formatCount(filteredThreads.length)} {t("条")}</span>
            <span>{t("当前范围")} {threadKindFilter === "main" ? t("主线程") : threadKindFilter === "subagent" ? t("子agent") : t("全部线程")}</span>
            <span>{t("快照")} {formatDate(snapshot.generatedAtMs)}</span>
            <button
              className="strip-action"
              onClick={() => setIsInspectorOpen((value) => !value)}
              title={isInspectorOpen ? t("收起详情面板") : t("展开详情面板")}
            >
              {isInspectorOpen ? <PanelRightClose size={15} /> : <PanelRightOpen size={15} />}
              {isInspectorOpen ? t("收起详情") : t("展开详情")}
            </button>
          </div>
          <ThreadTable
            threads={filteredThreads}
            sortMode={sortMode}
            sortDirection={sortDirection}
            selectedThreadId={selectedThreadId}
            readOnlyMode={readOnlyMode}
            onSelectThread={selectThreadFromTable}
            onOpenFullDetail={openFullDetailFromTable}
            onSortChange={handleSortChange}
            onShowThread={onShowThread}
            onHideThread={onHideThread}
            onRepairThread={onRepairThread}
          />
        </section>
        <DetailPanel
          isOpen={isInspectorOpen}
          detail={detail}
          isLoading={detailLoading}
          readOnlyMode={readOnlyMode}
          onToggleOpen={() => setIsInspectorOpen((value) => !value)}
          onBackup={onBackup}
          onRestore={onRestore}
          onExportPrompts={onExportPrompts}
          onViewPrompts={onViewPrompts}
          onViewLogs={onViewLogs}
          onShowThread={onShowThread}
          onDuplicate={onDuplicate}
          onHideThread={onHideThread}
          onArchive={onArchive}
          onSlim={onSlim}
          onMigrate={onMigrate}
          duplicateTargetPath={duplicateTargetPath}
          onDuplicateTargetPathChange={setDuplicateTargetPath}
          slimRemoveImages={slimRemoveImages}
          onSlimRemoveImagesChange={setSlimRemoveImages}
          slimKeepLatestCompacted={slimKeepLatestCompacted}
          onSlimKeepLatestCompactedChange={setSlimKeepLatestCompacted}
          migrateTargetPath={migrateTargetPath}
          onMigrateTargetPathChange={setMigrateTargetPath}
          renameSourcePath={renameSourcePath}
          renameTargetPath={renameTargetPath}
          renameFolder={renameFolder}
          onRenameSourcePathChange={setRenameSourcePath}
          onRenameTargetPathChange={setRenameTargetPath}
          onRenameFolderChange={setRenameFolder}
          onRenameProject={onRenameProject}
        />
      </div>
      <ThreadFullDetailModal
        thread={fullDetailThread}
        allThreads={snapshot.threads}
        detail={fullDetail}
        isLoading={Boolean(fullDetailThread && detailLoading && selectedThreadId === fullDetailThread.id)}
        dailyTokenLoading={Boolean(fullDetailThread && dailyTokenLoading && selectedThreadId === fullDetailThread.id)}
        readOnlyMode={readOnlyMode}
        onClose={() => setFullDetailThreadId(null)}
        onLoadDailyTokens={onLoadDailyTokens}
        onBackup={onBackup}
        onRestore={onRestore}
        onExportPrompts={onExportPrompts}
        onViewPrompts={onViewPrompts}
        onViewLogs={onViewLogs}
        onShowThread={onShowThread}
        onDuplicate={onDuplicate}
        onHideThread={onHideThread}
        onArchive={onArchive}
        onSlim={onSlim}
        onMigrate={onMigrate}
        duplicateTargetPath={duplicateTargetPath}
        onDuplicateTargetPathChange={setDuplicateTargetPath}
        slimRemoveImages={slimRemoveImages}
        onSlimRemoveImagesChange={setSlimRemoveImages}
        slimKeepLatestCompacted={slimKeepLatestCompacted}
        onSlimKeepLatestCompactedChange={setSlimKeepLatestCompacted}
        migrateTargetPath={migrateTargetPath}
        onMigrateTargetPathChange={setMigrateTargetPath}
        renameSourcePath={renameSourcePath}
        renameTargetPath={renameTargetPath}
        renameFolder={renameFolder}
        onRenameSourcePathChange={setRenameSourcePath}
        onRenameTargetPathChange={setRenameTargetPath}
        onRenameFolderChange={setRenameFolder}
        onRenameProject={onRenameProject}
      />
    </div>
  );
}

function ThreadFullDetailModal({
  thread,
  allThreads,
  detail,
  isLoading,
  dailyTokenLoading,
  readOnlyMode,
  onClose,
  onLoadDailyTokens,
  onBackup,
  onRestore,
  onExportPrompts,
  onViewPrompts,
  onViewLogs,
  onShowThread,
  onDuplicate,
  onHideThread,
  onArchive,
  onSlim,
  onMigrate,
  duplicateTargetPath,
  onDuplicateTargetPathChange,
  slimRemoveImages,
  onSlimRemoveImagesChange,
  slimKeepLatestCompacted,
  onSlimKeepLatestCompactedChange,
  migrateTargetPath,
  onMigrateTargetPathChange,
  renameSourcePath,
  renameTargetPath,
  renameFolder,
  onRenameSourcePathChange,
  onRenameTargetPathChange,
  onRenameFolderChange,
  onRenameProject
}: {
  thread: ThreadRecord | null;
  allThreads: ThreadRecord[];
  detail: ThreadDetail | null;
  isLoading: boolean;
  dailyTokenLoading: boolean;
  readOnlyMode: boolean;
  onClose: () => void;
  onLoadDailyTokens: (thread: ThreadRecord, force?: boolean) => void;
  onBackup: (thread: ThreadRecord) => void;
  onRestore: (backup: BackupRecord) => void;
  onExportPrompts: (thread: ThreadRecord) => void;
  onViewPrompts: (thread: ThreadRecord) => void;
  onViewLogs: (thread: ThreadRecord) => void;
  onShowThread: (thread: ThreadRecord) => void;
  onDuplicate: (thread: ThreadRecord) => void;
  onHideThread: (thread: ThreadRecord) => void;
  onArchive: (thread: ThreadRecord) => void;
  onSlim: (thread: ThreadRecord) => void;
  onMigrate: (thread: ThreadRecord) => void;
  duplicateTargetPath: string;
  onDuplicateTargetPathChange: (value: string) => void;
  slimRemoveImages: boolean;
  onSlimRemoveImagesChange: (value: boolean) => void;
  slimKeepLatestCompacted: boolean;
  onSlimKeepLatestCompactedChange: (value: boolean) => void;
  migrateTargetPath: string;
  onMigrateTargetPathChange: (value: string) => void;
  renameSourcePath: string;
  renameTargetPath: string;
  renameFolder: boolean;
  onRenameSourcePathChange: (value: string) => void;
  onRenameTargetPathChange: (value: string) => void;
  onRenameFolderChange: (value: boolean) => void;
  onRenameProject: () => void;
}) {
  const { t, formatDate } = useI18n();
  const descendants = React.useMemo(() => thread ? collectDescendantThreads(thread, allThreads) : [], [allThreads, thread]);
  const dialogRef = useModalAccessibility(Boolean(thread), onClose);
  if (!thread) return null;

  const ownStorage = threadOwnFileSize(thread);
  const childStorage = threadChildFileSize(thread);
  const totalStorage = Math.max(ownStorage + childStorage, threadTotalFileSize(thread), 1);
  const ownTokens = threadOwnTokens(thread);
  const childTokens = threadChildTokens(thread);
  const totalTokens = Math.max(ownTokens + childTokens, threadTotalTokens(thread), 1);
  const rolloutStats = detail?.rolloutStats;
  const messageRows = rolloutStats ? [
    { key: "user", label: t("用户消息"), value: rolloutStats.userMessages, tone: "green" as VisualTone },
    { key: "assistant", label: t("助手消息"), value: rolloutStats.assistantMessages, tone: "blue" as VisualTone },
    { key: "tool", label: t("工具调用"), value: rolloutStats.toolCalls + rolloutStats.toolOutputs, tone: "amber" as VisualTone },
    { key: "event", label: t("事件消息"), value: rolloutStats.eventMessages, tone: "neutral" as VisualTone },
    { key: "parse", label: t("解析错误"), value: rolloutStats.invalidJsonLines, tone: "red" as VisualTone }
  ].filter((row) => row.value > 0) : [];
  const maxMessageCount = Math.max(1, ...messageRows.map((row) => row.value));

  const closeOnBackdrop = (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) onClose();
  };

  const storageLegend = [
    { label: t("自身 JSONL"), value: ownStorage, tone: "blue" as VisualTone },
    { label: t("子线程存储"), value: childStorage, tone: "amber" as VisualTone }
  ];
  const tokenLegend = [
    { label: t("SQLite 自身"), value: ownTokens, tone: "green" as VisualTone },
    { label: t("SQLite 子线程合计"), value: childTokens, tone: "amber" as VisualTone }
  ];

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={closeOnBackdrop}>
      <section ref={dialogRef} className="thread-detail-modal" role="dialog" aria-modal="true" aria-label={t("完整线程详情")} tabIndex={-1}>
        <header className="modal-header">
          <div>
            <span className="eyebrow">{t("完整线程详情")}</span>
            <h2>{thread.title}</h2>
            <p>{thread.id}</p>
          </div>
          <button className="icon-button" onClick={onClose} title={t("关闭详情窗口")} aria-label={t("关闭详情窗口")} data-dialog-initial-focus type="button">
            <X size={16} />
          </button>
        </header>

        <div className="thread-detail-body">
          <section className="thread-detail-summary">
            <span className={`status ${thread.visibility}`}>{statusIcon(thread.visibility)}{statusLabel(thread.visibility, t)}</span>
            <div>
              <strong>{thread.threadKind === "subagent" ? t("子agent") : t("主线程")}</strong>
              <span>{thread.projectLabel}</span>
            </div>
            <div>
              <strong>{formatDate(thread.updatedAtMs)}</strong>
              <span>{t("更新时间")}</span>
            </div>
            <div>
              <strong>{formatCount(threadChildCount(thread))}</strong>
              <span>{t("子线程")}</span>
            </div>
          </section>

          <details className="thread-detail-card full-detail-operations">
            <summary>
              <span className="summary-title">
                <ChevronDown className="summary-chevron" size={16} />
                <span>{t("管理操作")}</span>
              </span>
              <span className="summary-meta">
                <em>{readOnlyMode ? t("只读") : t("本机连接器完整模式")}</em>
                <strong className="summary-state">
                  <span className="summary-state-closed">{t("展开管理操作")}</span>
                  <span className="summary-state-open">{t("收起管理操作")}</span>
                </strong>
              </span>
            </summary>
            <div className="full-detail-action-grid">
              {!readOnlyMode ? <button onClick={() => onBackup(thread)} type="button"><ShieldCheck size={15} /> {t("创建备份")}</button> : null}
              {!readOnlyMode && detail?.backups[0] ? <button onClick={() => onRestore(detail.backups[0])} type="button"><RotateCcw size={15} /> {t("回滚最近备份")}</button> : null}
              <button onClick={() => onViewPrompts(thread)} type="button"><BookOpen size={15} /> {t("查看 prompts")}</button>
              <button onClick={() => onExportPrompts(thread)} type="button"><Download size={15} /> {t("导出 prompts")}</button>
              <button onClick={() => onViewLogs(thread)} type="button"><FileText size={15} /> {t("详细日志")}</button>
              {!readOnlyMode && !thread.codexVisible && !thread.archived && thread.threadKind === "main" && thread.fileExists ? (
                <button onClick={() => onShowThread(thread)} type="button"><Eye size={15} /> {t("显示线程")}</button>
              ) : null}
              {!readOnlyMode && thread.codexVisible && !thread.archived ? (
                <button onClick={() => onHideThread(thread)} type="button"><EyeOff size={15} /> {t("隐藏线程")}</button>
              ) : null}
              {!readOnlyMode ? <button className="danger-action" onClick={() => onArchive(thread)} type="button"><Trash2 size={15} /> {t("安全删除")}</button> : null}
            </div>
            {readOnlyMode ? (
              <section className="browser-readonly-callout compact">
                <ServerCog size={16} />
                <div>
                  <strong>{t("浏览器只读")}</strong>
                  <span>{t("浏览器文件夹模式只能查看、搜索、读取日志和导出 prompt；修复、迁移、瘦身、删除和 MCP 请使用本机连接器。")}</span>
                </div>
              </section>
            ) : (
              <div className="full-detail-operation-grid">
                <section className="operation-section">
                  <h3>{t("复制线程")}</h3>
                  <label>{t("目标项目路径")}</label>
                  <input aria-label={t("目标项目路径")} value={duplicateTargetPath} onChange={(event) => onDuplicateTargetPathChange(event.target.value)} />
                  <button onClick={() => onDuplicate(thread)} type="button"><Copy size={15} /> {t("复制到目标项目")}</button>
                </section>
                <section className="operation-section">
                  <h3>{t("线程瘦身范围")}</h3>
                  <label className="checkbox-line">
                    <input type="checkbox" checked={slimRemoveImages} onChange={(event) => onSlimRemoveImagesChange(event.target.checked)} />
                    {t("移除嵌入图片和 data:image 内容")}
                  </label>
                  <label className="checkbox-line">
                    <input type="checkbox" checked={slimKeepLatestCompacted} onChange={(event) => onSlimKeepLatestCompactedChange(event.target.checked)} />
                    {t("只保留最新 compacted checkpoint")}
                  </label>
                  <button onClick={() => onSlim(thread)} type="button"><Scissors size={15} /> {t("按选定范围瘦身")}</button>
                </section>
                <section className="operation-section">
                  <h3>{t("迁移线程")}</h3>
                  <input aria-label={t("目标项目路径")} value={migrateTargetPath} onChange={(event) => onMigrateTargetPathChange(event.target.value)} />
                  <button onClick={() => onMigrate(thread)} type="button"><MoveRight size={15} /> {t("迁移到目标项目")}</button>
                </section>
                <section className="operation-section">
                  <h3>{t("项目及文件夹改名")}</h3>
                  <label>{t("原项目路径")}</label>
                  <input aria-label={t("原项目路径")} value={renameSourcePath} onChange={(event) => onRenameSourcePathChange(event.target.value)} />
                  <label>{t("目标项目路径")}</label>
                  <input aria-label={t("目标项目路径")} value={renameTargetPath} onChange={(event) => onRenameTargetPathChange(event.target.value)} />
                  <label className="checkbox-line">
                    <input type="checkbox" checked={renameFolder} onChange={(event) => onRenameFolderChange(event.target.checked)} />
                    {t("同步重命名本地文件夹")}
                  </label>
                  <button onClick={onRenameProject} type="button"><FolderPen size={15} /> {t("执行项目重命名")}</button>
                </section>
              </div>
            )}
          </details>

          <section className="thread-detail-metrics">
            <article>
              <span>{t("总存储")}</span>
              <strong>{formatBytes(threadTotalFileSize(thread))}</strong>
              <em>{t("含子线程总计")}</em>
            </article>
            <article>
              <span>{t("自身 JSONL")}</span>
              <strong>{formatBytes(ownStorage)}</strong>
              <em>{thread.rolloutPath || "-"}</em>
            </article>
            <article>
              <span>{t("子线程存储")}</span>
              <strong>{formatBytes(childStorage)}</strong>
              <em>{formatCount(threadChildCount(thread))} {t("子线程")}</em>
            </article>
            <article>
              <span>{t("SQLite tokens_used 原始记录")}</span>
              <strong>{formatCount(threadTotalTokens(thread))}</strong>
              <em>{t("非账户消耗口径")} · {t("SQLite 自身")} {formatCount(ownTokens)} · {t("SQLite 子线程合计")} {formatCount(childTokens)}</em>
            </article>
          </section>

          <div className="thread-detail-grid">
            <section className="thread-detail-card">
              <div className="panel-title-row">
                <h3>{t("容量构成")}</h3>
                <span>{formatBytes(threadTotalFileSize(thread))}</span>
              </div>
              <div className="composition-bar">
                {storageLegend.map((item) => item.value > 0 ? (
                  <span
                    key={item.label}
                    className={`visual-tone-${item.tone}`}
                    style={{ width: `${percentOf(item.value, totalStorage)}%` }}
                    title={`${item.label}: ${formatBytes(item.value)}`}
                  />
                ) : null)}
              </div>
              <div className="composition-legend">
                {storageLegend.map((item) => (
                  <span key={item.label}><i className={`visual-tone-${item.tone}`} />{item.label} {formatBytes(item.value)}</span>
                ))}
              </div>
            </section>

            <section className="thread-detail-card">
              <div className="panel-title-row">
                <h3>{t("SQLite 原始记录构成")}</h3>
                <span>{formatCount(threadTotalTokens(thread))}</span>
              </div>
              <div className="composition-bar">
                {tokenLegend.map((item) => item.value > 0 ? (
                  <span
                    key={item.label}
                    className={`visual-tone-${item.tone}`}
                    style={{ width: `${percentOf(item.value, totalTokens)}%` }}
                    title={`${item.label}: ${formatCount(item.value)}`}
                  />
                ) : null)}
              </div>
              <div className="composition-legend">
                {tokenLegend.map((item) => (
                  <span key={item.label}><i className={`visual-tone-${item.tone}`} />{item.label} {formatCount(item.value)}</span>
                ))}
              </div>
            </section>

            <section className="thread-detail-card">
              <div className="panel-title-row">
                <h3>{t("消息结构")}</h3>
                <span>{rolloutStats ? formatCount(rolloutStats.lineCount) : isLoading ? t("读取线程详情...") : "-"}</span>
              </div>
              {isLoading ? <div className="panel-loading compact" role="status" aria-live="polite">{t("读取线程详情...")}</div> : null}
              {!isLoading && messageRows.length ? (
                <div className="detail-bar-list">
                  {messageRows.map((row) => (
                    <div className="detail-bar-row" key={row.key}>
                      <div><span>{row.label}</span><strong>{formatCount(row.value)}</strong></div>
                      <div className="bar-track"><span className={`visual-tone-${row.tone}`} style={{ width: barWidth(row.value, maxMessageCount) }} /></div>
                    </div>
                  ))}
                </div>
              ) : !isLoading ? <div className="visual-empty">{t("没有可用线程数据")}</div> : null}
            </section>

            <section className="thread-detail-card">
              <div className="panel-title-row">
                <h3>{t("子线程组成")}</h3>
                <span>{formatCount(descendants.length)}</span>
              </div>
              <div className="child-thread-list">
                {descendants.length ? descendants.slice(0, 24).map((child) => (
                  <article key={child.id} className="child-thread-row">
                    <span className={`status ${child.visibility}`}>{statusIcon(child.visibility)}{statusLabel(child.visibility, t)}</span>
                    <div>
                      <strong>{child.title}</strong>
                      <em>{child.id}</em>
                    </div>
                    <div>
                      <strong>{formatBytes(threadTotalFileSize(child))}</strong>
                      <em>{formatCount(threadTotalTokens(child))} {t("SQLite tokens_used 原始记录")}</em>
                    </div>
                  </article>
                )) : <div className="visual-empty">{t("没有子线程")}</div>}
              </div>
            </section>
          </div>

          <section className="thread-detail-card metadata-card">
            <div className="panel-title-row">
              <h3>{t("位置")}</h3>
              <span>{thread.source || "SQLite"}</span>
            </div>
            <div className="metadata-grid">
              <label>{t("项目路径")}</label><code>{thread.projectPath || "-"}</code>
              <label>{t("JSONL 路径")}</label><code>{thread.rolloutPath || "-"}</code>
              <label>{t("模型")}</label><code>{thread.model || "-"}</code>
              <label>{t("创建时间")}</label><code>{formatDate(thread.createdAtMs)}</code>
              <label>{t("Codex thread/list 排名")}</label><code>{thread.threadListRank ? `#${thread.threadListRank}` : t("不在 thread/list 排序中")}</code>
              <label>{t("session_index 排名")}</label><code>{thread.sessionIndexRank ? `#${thread.sessionIndexRank}` : t("不在 session_index 修复窗口中")}</code>
              <label>{t("可见对话流")}</label><code>{thread.rolloutDisplayStatus || "not_scanned"} | response {formatCount((thread.rolloutDisplayResponseUserMessages || 0) + (thread.rolloutDisplayResponseAssistantMessages || 0))} / visible {formatCount((thread.rolloutDisplayVisibleUserMessages || 0) + (thread.rolloutDisplayVisibleAgentMessages || 0))}</code>
              <label>{t("Git 分支")}</label><code>{thread.gitBranch || "-"}</code>
              <label>{t("CLI 版本")}</label><code>{thread.cliVersion || "-"}</code>
            </div>
          </section>

          <DailyTokenUsageChart
            key={thread.id}
            usage={detail?.dailyTokenUsage}
            isLoading={dailyTokenLoading}
            onLoad={(force) => onLoadDailyTokens(thread, force)}
          />
        </div>

        <footer className="modal-footer">
          <button onClick={() => onViewPrompts(thread)} type="button"><BookOpen size={15} /> {t("查看 prompts")}</button>
          <button onClick={() => onExportPrompts(thread)} type="button"><Download size={15} /> {t("导出 prompts")}</button>
          <button onClick={() => onViewLogs(thread)} type="button"><FileText size={15} /> {t("详细日志")}</button>
          <button onClick={onClose} type="button">{t("关闭详情窗口")}</button>
        </footer>
      </section>
    </div>
  );
}

function diagnosticSeverityLabel(severity: DiagnosticSeverity, t: Translator): string {
  if (severity === "critical") return t("严重");
  if (severity === "warning") return t("警告");
  if (severity === "info") return t("提示");
  return t("通过");
}

function diagnosticSeverityIcon(severity: DiagnosticSeverity) {
  if (severity === "critical") return <CircleAlert size={16} />;
  if (severity === "warning") return <CircleAlert size={16} />;
  if (severity === "info") return <FileText size={16} />;
  return <CheckCircle2 size={16} />;
}

function compactPromptText(value: unknown, maxLength = 220): string {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 1).trim()}…`;
}

function buildClientRepairPrompt(report: DiagnosticsReport, language: Language): string {
  const issueLimit = 12;
  const checkLimit = 8;
  const attentionChecks = report.checks.filter((check) => check.status === "critical" || check.status === "warning");
  if (language === "en") {
    const lines = [
      "You are Codex running on my own machine. Use this Codex Home Manager diagnostic report as a starting point, then verify local evidence before changing anything.",
      "",
      "Goal: diagnose and repair the reported Codex Desktop / CODEX_HOME problems. Do not rely only on this pasted report; inspect the real local files, SQLite databases, JSONL logs, plugin cache and running processes first.",
      "",
      "Operating rules:",
      "- Treat CODEX_HOME state files as high risk. Back up before writes and close Codex Desktop when possible.",
      "- Do not permanently delete rollout JSONL, state_5.sqlite, session_index.jsonl, config files, or plugin-cache evidence unless I explicitly approve.",
      "- Fix only issues supported by local evidence. If evidence is stale or contradictory, explain the blocker instead of guessing.",
      "",
      `CODEX_HOME: ${report.codexHome}`,
      `health: ${report.score} / ${report.status}`,
      `issues: critical=${report.summary.critical}, warning=${report.summary.warning}, info=${report.summary.info}`,
      `checks: pass=${report.summary.pass} / total=${report.summary.checks}`,
      "",
      "Important paths:",
      ...Object.entries(report.paths).map(([key, value]) => `- ${key}: ${value}`),
      "",
      "Issues:",
      ...(report.issues.length ? report.issues.slice(0, issueLimit).flatMap((issue, index) => [
        `${index + 1}. [${issue.severity}] ${issue.title} (${issue.id}, ${issue.category})`,
        `   Summary: ${compactPromptText(issue.summary)}`,
        `   Recommendation: ${compactPromptText(issue.recommendation)}`,
        ...issue.evidence.slice(0, 3).map((item) => `   Evidence: ${compactPromptText(item)}`),
        ...issue.affectedPaths.slice(0, 3).map((item) => `   Path: ${compactPromptText(item)}`)
      ]) : ["- No critical/warning/info issues were reported."]),
      "",
      "Attention checks:",
      ...(attentionChecks.length ? attentionChecks.slice(0, checkLimit).flatMap((check, index) => [
        `${index + 1}. [${check.status}] ${check.title} (${check.id}, ${check.category})`,
        `   Summary: ${compactPromptText(check.summary)}`,
        ...check.evidence.slice(0, 2).map((item) => `   Evidence: ${compactPromptText(item)}`)
      ]) : ["- No critical or warning checks were reported."]),
      "",
      "Start by confirming the current CODEX_HOME and whether Codex Desktop is running, then execute the safest evidence-backed repair path."
    ];
    return lines.join("\n").trim();
  }

  const lines = [
    "你是运行在我自己电脑上的 Codex。下面是 Codex Home Manager 的只读体检报告，请把它当作起点，但在修改任何东西之前必须先用本机真实证据复核。",
    "",
    "目标：诊断并修复报告中列出的 Codex Desktop / CODEX_HOME 问题。不要只凭这段报告下结论；先检查本机真实文件、SQLite 数据库、JSONL 日志、插件缓存和运行进程。",
    "",
    "执行边界：",
    "- CODEX_HOME 状态文件属于高风险对象。写入前先创建可回滚备份；能关闭 Codex Desktop 时先关闭。",
    "- 不要永久删除 rollout JSONL、state_5.sqlite、session_index.jsonl、配置文件和插件缓存证据，除非我明确批准。",
    "- 只修复有本机证据支撑的问题。如果证据过期或相互矛盾，先说明阻塞点，不要猜。",
    "",
    `CODEX_HOME：${report.codexHome}`,
    `健康：${report.score} / ${report.status}`,
    `问题：critical=${report.summary.critical}，warning=${report.summary.warning}，info=${report.summary.info}`,
    `检查项：通过=${report.summary.pass} / 总数=${report.summary.checks}`,
    "",
    "关键路径：",
    ...Object.entries(report.paths).map(([key, value]) => `- ${key}：${value}`),
    "",
    "问题：",
    ...(report.issues.length ? report.issues.slice(0, issueLimit).flatMap((issue, index) => [
      `${index + 1}. [${issue.severity}] ${issue.title}（${issue.id}，${issue.category}）`,
      `   摘要：${compactPromptText(issue.summary)}`,
      `   建议：${compactPromptText(issue.recommendation)}`,
      ...issue.evidence.slice(0, 3).map((item) => `   证据：${compactPromptText(item)}`),
      ...issue.affectedPaths.slice(0, 3).map((item) => `   路径：${compactPromptText(item)}`)
    ]) : ["- 体检没有报告 critical/warning/info 问题。"]),
    "",
    "需要关注的检查项：",
    ...(attentionChecks.length ? attentionChecks.slice(0, checkLimit).flatMap((check, index) => [
      `${index + 1}. [${check.status}] ${check.title}（${check.id}，${check.category}）`,
      `   摘要：${compactPromptText(check.summary)}`,
      ...check.evidence.slice(0, 2).map((item) => `   证据：${compactPromptText(item)}`)
    ]) : ["- 没有严重或警告级检查项。"]),
    "",
    "请先确认当前 CODEX_HOME 和 Codex Desktop 是否正在运行，然后执行最安全、证据充分的修复路径。"
  ];
  return lines.join("\n").trim();
}

function shouldIgnoreDiagnosticActivation(target: EventTarget | null): boolean {
  const element = target instanceof HTMLElement ? target : null;
  return Boolean(element?.closest("button, a, input, textarea, select, summary"));
}

function formatDiagnosticDetailText(target: DiagnosticDetailTarget, t: Translator): string {
  const item = target.item;
  const severity = target.kind === "issue" ? target.item.severity : target.item.status;
  const idLabel = target.kind === "issue" ? t("问题 ID") : t("检查 ID");
  const lines = [
    `${target.kind === "issue" ? t("问题详情") : t("检查详情")}：${item.title}`,
    `${idLabel}：${item.id}`,
    `${t("状态")}：${diagnosticSeverityLabel(severity, t)}`,
    `${t("分类")}：${item.category}`,
    "",
    `${t("摘要")}：${item.summary}`
  ];

  if (target.kind === "issue") {
    lines.push("", `${t("建议")}：${target.item.recommendation}`);
    if (target.item.fixCommand) {
      lines.push("", `${t("修复命令")}：${target.item.fixCommand}`);
    }
  }

  if (item.evidence.length) {
    lines.push("", `${t("证据")}：`, ...item.evidence.map((value) => `- ${value || "-"}`));
  }

  if (item.affectedPaths.length) {
    lines.push("", `${t("相关路径")}：`, ...item.affectedPaths.map((value) => `- ${value || "-"}`));
  }

  return lines.join("\n");
}

function DiagnosticDetailModal({ target, onClose }: { target: DiagnosticDetailTarget; onClose: () => void }) {
  const { t } = useI18n();
  const [copied, setCopied] = React.useState(false);
  const dialogRef = useModalAccessibility(true, onClose);
  const item = target.item;
  const severity = target.kind === "issue" ? target.item.severity : target.item.status;
  const idLabel = target.kind === "issue" ? t("问题 ID") : t("检查 ID");
  const detailText = formatDiagnosticDetailText(target, t);
  const rawJson = JSON.stringify(item, null, 2);

  const closeOnBackdrop = (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) onClose();
  };

  async function handleCopyDetails(): Promise<void> {
    await copyTextToClipboard(detailText);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={closeOnBackdrop}>
      <section ref={dialogRef} className="diagnostic-detail-modal" role="dialog" aria-modal="true" aria-label={t("体检详情")} tabIndex={-1}>
        <header className="modal-header">
          <div>
            <span className="eyebrow">{target.kind === "issue" ? t("问题详情") : t("检查详情")}</span>
            <h2>{item.title}</h2>
            <p>{item.id}</p>
          </div>
          <button className="icon-button" onClick={onClose} title={t("关闭详情窗口")} aria-label={t("关闭详情窗口")} data-dialog-initial-focus type="button">
            <X size={16} />
          </button>
        </header>

        <div className="diagnostic-detail-body">
          <section className="diagnostic-detail-summary">
            <span className={`diagnostic-pill ${severity}`}>
              {diagnosticSeverityIcon(severity)}
              {diagnosticSeverityLabel(severity, t)}
            </span>
            <div>
              <strong>{idLabel}</strong>
              <code>{item.id}</code>
            </div>
            <div>
              <strong>{t("分类")}</strong>
              <code>{item.category}</code>
            </div>
          </section>

          <section className="diagnostic-detail-card">
            <h3>{t("摘要")}</h3>
            <p>{item.summary}</p>
          </section>

          {target.kind === "issue" ? (
            <section className="diagnostic-detail-card">
              <h3>{t("建议")}</h3>
              <p>{target.item.recommendation}</p>
              {target.item.fixCommand ? <code>{target.item.fixCommand}</code> : null}
            </section>
          ) : null}

          <section className="diagnostic-detail-card">
            <h3>{t("证据")}</h3>
            <div className="diagnostic-detail-list">
              {item.evidence.length ? item.evidence.map((value, index) => (
                <code key={`${item.id}-modal-evidence-${index}`}>{value || "-"}</code>
              )) : <code>{t("无额外证据")}</code>}
            </div>
          </section>

          <section className="diagnostic-detail-card">
            <h3>{t("相关路径")}</h3>
            <div className="diagnostic-detail-list">
              {item.affectedPaths.length ? item.affectedPaths.map((value, index) => (
                <code key={`${item.id}-modal-path-${index}`}>{value || "-"}</code>
              )) : <code>{t("无额外证据")}</code>}
            </div>
          </section>

          <details className="diagnostic-detail-card diagnostic-detail-raw">
            <summary>{t("原始 JSON")}</summary>
            <pre>{rawJson}</pre>
          </details>
        </div>

        <footer className="modal-footer">
          <button onClick={() => void handleCopyDetails()} type="button"><Copy size={15} /> {copied ? t("已复制详情") : t("复制详情")}</button>
          <button onClick={onClose} type="button">{t("关闭详情窗口")}</button>
        </footer>
      </section>
    </div>
  );
}

function DiagnosticIssueCard({ issue, onOpen }: { issue: DiagnosticIssue; onOpen: () => void }) {
  const { t } = useI18n();
  const evidenceCount = issue.evidence.length + issue.affectedPaths.length;

  return (
    <article
      className={`diagnostic-card ${issue.severity} interactive`}
      onDoubleClick={(event) => {
        if (!shouldIgnoreDiagnosticActivation(event.target)) onOpen();
      }}
      title={t("双击查看体检详情")}
    >
      <div className="diagnostic-card-head">
        <span className={`diagnostic-pill ${issue.severity}`}>
          {diagnosticSeverityIcon(issue.severity)}
          {diagnosticSeverityLabel(issue.severity, t)}
        </span>
        <code>{issue.category}</code>
      </div>
      <h3>{issue.title}</h3>
      <p>{issue.summary}</p>
      <div className="diagnostic-recommendation">
        <strong>{t("建议")}</strong>
        <span>{issue.recommendation}</span>
      </div>
      <details className="diagnostic-disclosure">
        <summary>
          <span>{t("证据与路径")}</span>
          <em>{formatCount(evidenceCount)} {t("条")}</em>
        </summary>
        {issue.evidence.length ? (
          <div className="diagnostic-evidence">
            <strong>{t("证据")}</strong>
            {issue.evidence.slice(0, 6).map((item, index) => <code key={`${issue.id}-evidence-${index}`}>{item || "-"}</code>)}
          </div>
        ) : null}
        {issue.affectedPaths.length ? (
          <div className="diagnostic-paths">
            <strong>{t("相关路径")}</strong>
            {issue.affectedPaths.slice(0, 5).map((item, index) => <code key={`${issue.id}-path-${index}`}>{item || "-"}</code>)}
          </div>
        ) : null}
      </details>
      <button className="diagnostic-open-hint" onClick={onOpen} type="button">{t("查看详情")}</button>
    </article>
  );
}

function DiagnosticCheckRow({ check, onOpen }: { check: DiagnosticCheck; onOpen: () => void }) {
  const { t } = useI18n();

  return (
    <article
      className={`diagnostic-check ${check.status} interactive`}
      onDoubleClick={(event) => {
        if (!shouldIgnoreDiagnosticActivation(event.target)) onOpen();
      }}
      title={t("双击查看体检详情")}
    >
      <div>
        <span className={`diagnostic-dot ${check.status}`} />
        <strong>{check.title}</strong>
        <em>{check.category}</em>
      </div>
      <p>{check.summary}</p>
      <details className="diagnostic-check-evidence">
        <summary>{t("展开核查证据")}</summary>
        {check.evidence.length ? <code>{check.evidence.filter(Boolean).slice(0, 4).join(" | ")}</code> : <code>{t("无额外证据")}</code>}
      </details>
      <button className="diagnostic-open-hint" onClick={onOpen} type="button">{t("查看详情")}</button>
    </article>
  );
}

function DiagnosticCheckDetail({ check }: { check: DiagnosticCheck }) {
  const { t } = useI18n();
  return (
    <article className={`diagnostic-card ${check.status}`}>
      <div className="diagnostic-card-head">
        <span className={`diagnostic-pill ${check.status}`}>
          {diagnosticSeverityIcon(check.status)}
          {diagnosticSeverityLabel(check.status, t)}
        </span>
        <code>{check.category}</code>
      </div>
      <h3>{check.title}</h3>
      <p>{check.summary}</p>
      <div className="diagnostic-evidence">
        <strong>{t("证据")}</strong>
        {check.evidence.length ? check.evidence.slice(0, 8).map((item, index) => (
          <code key={`${check.id}-detail-evidence-${index}`}>{item || "-"}</code>
        )) : <code>{t("无额外证据")}</code>}
      </div>
      {check.affectedPaths.length ? (
        <div className="diagnostic-paths">
          <strong>{t("相关路径")}</strong>
          {check.affectedPaths.slice(0, 8).map((item, index) => <code key={`${check.id}-detail-path-${index}`}>{item || "-"}</code>)}
        </div>
      ) : null}
    </article>
  );
}

function CapacityTrendDirectionLabel({
  change,
  formatDelta
}: {
  change: CapacityTrendChange;
  formatDelta: (value: number) => string;
}) {
  const { t } = useI18n();
  const absoluteDelta = Math.abs(change.delta);
  if (change.direction === "up") {
    return <span className="capacity-direction up"><ArrowUp size={13} /> {t("较上次增长")} {formatDelta(absoluteDelta)}</span>;
  }
  if (change.direction === "down") {
    return <span className="capacity-direction down"><ArrowDown size={13} /> {t("较上次下降")} {formatDelta(absoluteDelta)}</span>;
  }
  if (change.direction === "flat") {
    return <span className="capacity-direction flat"><ArrowUpDown size={13} /> {t("较上次持平")}</span>;
  }
  return <span className="capacity-direction unknown"><ArrowUpDown size={13} /> {t("暂无历史基线")}</span>;
}

function CapacitySparkline({ values, label }: { values: number[]; label: string }) {
  const width = 132;
  const height = 34;
  const normalizedValues = values.length ? values : [0];
  const minimum = Math.min(...normalizedValues);
  const maximum = Math.max(...normalizedValues);
  const range = Math.max(1, maximum - minimum);
  const points = normalizedValues.map((value, index) => {
    const x = normalizedValues.length === 1 ? width / 2 : index * width / (normalizedValues.length - 1);
    const y = height - 3 - (value - minimum) / range * (height - 6);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return (
    <svg className="capacity-sparkline" role="img" aria-label={label} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
      <title>{label}</title>
      <path d={`M0 ${height - 3} H${width}`} className="capacity-sparkline-baseline" />
      <polyline points={points} />
    </svg>
  );
}

function CapacityHistoryItem({
  label,
  value,
  values,
  change,
  formatValue
}: {
  label: string;
  value: number;
  values: number[];
  change: CapacityTrendChange;
  formatValue: (value: number) => string;
}) {
  const { t } = useI18n();
  return (
    <article className="capacity-history-item">
      <div>
        <span>{label}</span>
        <strong>{formatValue(value)}</strong>
      </div>
      <CapacitySparkline values={values} label={`${label} ${t("历史走势")}`} />
      <CapacityTrendDirectionLabel change={change} formatDelta={formatValue} />
    </article>
  );
}

function DiagnosticsCapacityTrend({ trend }: { trend: CapacityTrend }) {
  const { t } = useI18n();
  const [isOpen, setIsOpen] = React.useState(false);
  const current = trend.current;
  const recentHistory = trend.history.slice(-30);
  const mcpRiskCount = current.nodeReplRiskProcessCount + current.legacyFallbackProcessCount + current.xcodebuildProcessCount;
  const countFormatter = (value: number) => formatCount(value);
  const byteFormatter = (value: number) => formatBytes(value);
  const historyValues = (metricName: keyof CapacityTrendMetrics) => recentHistory.map((snapshot) => Number(snapshot[metricName]) || 0);

  return (
    <details
      className="diagnostics-capacity-trends"
      onToggle={(event) => setIsOpen(event.currentTarget.open)}
    >
      <summary aria-label={isOpen ? t("收起运行容量趋势") : t("展开运行容量趋势")}>
        <span className="capacity-summary-title">
          <ArrowUpDown size={16} />
          <strong>{t("运行容量趋势")}</strong>
          <em>{formatCount(trend.history.length)} {t("个快照")}</em>
        </span>
        <span className="capacity-summary-metrics">
          <span>
            <em>{t("Sessions 体量")}</em>
            <strong>{formatBytes(current.sessionsBytes)}</strong>
            <CapacityTrendDirectionLabel change={trend.changes.sessionsBytes} formatDelta={byteFormatter} />
          </span>
          <span>
            <em>{t("超大线程（≥ 250 MB）")}</em>
            <strong>{formatCount(current.largeThreadCount)}</strong>
            <CapacityTrendDirectionLabel change={trend.changes.largeThreadCount} formatDelta={countFormatter} />
          </span>
          <span>
            <em>{t("管理器备份")}</em>
            <strong>{formatBytes(current.backupBytes)}</strong>
            <small>{formatCount(current.backupFileCount)} {t("个文件")}</small>
          </span>
          <span>
            <em>{t("MCP 子进程")}</em>
            <strong>{formatCount(current.mcpProcessCount)}</strong>
            <small className={mcpRiskCount ? "risk" : ""}>{formatCount(mcpRiskCount)} {t("风险")}</small>
          </span>
        </span>
        <ChevronDown className="capacity-summary-chevron" size={16} />
      </summary>

      <div className="capacity-trend-body">
        <section className="capacity-history-grid" aria-label={t("历史走势")}>
          <CapacityHistoryItem
            label={t("Sessions 体量")}
            value={current.sessionsBytes}
            values={historyValues("sessionsBytes")}
            change={trend.changes.sessionsBytes}
            formatValue={byteFormatter}
          />
          <CapacityHistoryItem
            label={t("超大线程（≥ 250 MB）")}
            value={current.largeThreadCount}
            values={historyValues("largeThreadCount")}
            change={trend.changes.largeThreadCount}
            formatValue={countFormatter}
          />
          <CapacityHistoryItem
            label={t("管理器备份")}
            value={current.backupBytes}
            values={historyValues("backupBytes")}
            change={trend.changes.backupBytes}
            formatValue={byteFormatter}
          />
          <CapacityHistoryItem
            label={`${t("管理器备份")} · ${t("文件数")}`}
            value={current.backupFileCount}
            values={historyValues("backupFileCount")}
            change={trend.changes.backupFileCount}
            formatValue={countFormatter}
          />
          <CapacityHistoryItem
            label={t("MCP 子进程")}
            value={current.mcpProcessCount}
            values={historyValues("mcpProcessCount")}
            change={trend.changes.mcpProcessCount}
            formatValue={countFormatter}
          />
        </section>

        <div className="capacity-trend-guidance">
          <section className="capacity-mcp-composition" aria-label={t("MCP 构成")}>
            <h3>{t("MCP 构成")}</h3>
            <div>
              <span><em>{t("正常 node_repl")}</em><strong>{formatCount(current.normalNodeReplProcessCount)}</strong></span>
              <span className={current.nodeReplRiskProcessCount ? "risk" : ""}><em>{t("旧版 node_repl 参数")}</em><strong>{formatCount(current.nodeReplRiskProcessCount)}</strong></span>
              <span className={current.legacyFallbackProcessCount ? "risk" : ""}><em>{t("Legacy fallback")}</em><strong>{formatCount(current.legacyFallbackProcessCount)}</strong></span>
              <span className={current.xcodebuildProcessCount ? "risk" : ""}><em>{t("xcodebuild 风险")}</em><strong>{formatCount(current.xcodebuildProcessCount)}</strong></span>
              <span><em>{t("其他 MCP")}</em><strong>{formatCount(current.otherMcpServerProcessCount)}</strong></span>
            </div>
          </section>
          <section className="capacity-retention-guidance" aria-label={t("保留策略")}>
            <h3>{t("保留策略")}</h3>
            <p>{t("按日保留最近 90 天，最多 90 个快照。")} {t("快照只记录计数、体量和时间，不记录标题、prompt、路径或命令行。")}</p>
            <ul>
              <li>{t("先核对备份 manifest 和回滚价值，再将明确过期的备份移入回收站。")}</li>
              <li>{t("优先对超大线程做瘦身预览，不按数量或体量直接删除会话。")}</li>
              <li>{t("仅在 legacy fallback 或 xcodebuild 风险持续增长时核对插件来源和父进程；正常 node_repl fanout 无需清理。")}</li>
            </ul>
          </section>
        </div>

        {!trend.storage.persisted ? <p className="capacity-trend-notice" role="status">{t("趋势快照暂未持久化，本次体检仍然可用。")}</p> : null}
        {trend.storage.recoveredFromCorruption ? <p className="capacity-trend-notice" role="status">{t("已从损坏的趋势文件恢复。")}</p> : null}
        {current.backupScanTruncated ? <p className="capacity-trend-notice warning" role="status">{t("备份扫描达到文件上限，当前值是已扫描部分。")}</p> : null}
      </div>
    </details>
  );
}

function DiagnosticsModule({
  report,
  isLoading,
  isCached,
  onRefresh
}: {
  report: DiagnosticsReport | null;
  isLoading: boolean;
  isCached: boolean;
  onRefresh: () => void;
}) {
  const { t, formatDate, language } = useI18n();
  const [diagnosticFilter, setDiagnosticFilter] = React.useState<"attention" | "all" | "pass">("attention");
  const [repairPromptCopied, setRepairPromptCopied] = React.useState(false);
  const [repairPromptCopyError, setRepairPromptCopyError] = React.useState("");
  const [repairPromptOpen, setRepairPromptOpen] = React.useState(false);
  const [diagnosticDetail, setDiagnosticDetail] = React.useState<DiagnosticDetailTarget | null>(null);
  const repairPromptRef = React.useRef<HTMLDetailsElement | null>(null);
  const closeRepairPrompt = React.useCallback(() => {
    if (repairPromptRef.current) repairPromptRef.current.open = false;
    setRepairPromptOpen(false);
  }, []);

  const criticalIssues = report?.issues.filter((issue) => issue.severity === "critical") ?? [];
  const warningIssues = report?.issues.filter((issue) => issue.severity === "warning") ?? [];
  const infoIssues = report?.issues.filter((issue) => issue.severity === "info") ?? [];
  const visibleIssues = [...criticalIssues, ...warningIssues, ...infoIssues];
  const attentionChecks = report?.checks.filter((check) => check.status === "critical" || check.status === "warning") ?? [];
  const infoChecks = report?.checks.filter((check) => check.status === "info") ?? [];
  const passedChecks = report?.checks.filter((check) => check.status === "pass") ?? [];
  const filteredChecks = diagnosticFilter === "pass" ? passedChecks : diagnosticFilter === "all" ? [...attentionChecks, ...infoChecks, ...passedChecks] : attentionChecks;
  const repairPrompt = report ? (report.repairPrompt || buildClientRepairPrompt(report, language)) : "";

  React.useEffect(() => {
    setRepairPromptCopied(false);
    setRepairPromptCopyError("");
    setRepairPromptOpen(false);
  }, [repairPrompt]);

  React.useEffect(() => {
    setDiagnosticDetail(null);
  }, [report?.codexHome, report?.generatedAtMs]);

  React.useLayoutEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      if (!repairPromptRef.current?.open) return;
      const target = event.target as Node | null;
      if (target && repairPromptRef.current?.contains(target)) return;
      closeRepairPrompt();
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !repairPromptRef.current?.open) return;
      closeRepairPrompt();
    };

    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [closeRepairPrompt]);

  async function handleCopyRepairPrompt(): Promise<void> {
    if (!repairPrompt) return;
    try {
      await copyTextToClipboard(repairPrompt);
      setRepairPromptCopied(true);
      setRepairPromptCopyError("");
      window.setTimeout(() => setRepairPromptCopied(false), 1800);
    } catch (error) {
      setRepairPromptCopied(false);
      setRepairPromptCopyError(error instanceof Error ? error.message : String(error));
    }
  }

  if (!report && isLoading) {
    return <div className="loading-page">{t("正在体检 Codex Home...")}</div>;
  }

  if (!report) {
    return <div className="empty-state">{t("没有可用体检报告")}</div>;
  }

  const summaryItems = [
    { label: t("严重问题"), value: formatCount(report.summary.critical), tone: "red" },
    { label: t("警告"), value: formatCount(report.summary.warning), tone: "amber" },
    { label: t("检查项"), value: `${formatCount(report.summary.pass)}/${formatCount(report.summary.checks)}`, tone: "blue" },
    { label: t("线程数"), value: formatCount(report.summary.threadCount || 0), tone: "neutral" }
  ];

  return (
    <div className="module-stack diagnostics-module">
      <div className="diagnostics-topline">
        <div className="diagnostics-title">
          <span className="eyebrow">Diagnostics</span>
          <h2>{t("Codex Home 体检")}</h2>
          <p>{t("只读扫描，不修改 `.codex`。")}</p>
        </div>
        <div className="diagnostics-top-actions">
          {isCached ? <span className="eyebrow">{t("缓存报告")}</span> : null}
          {report && isLoading ? <span className="eyebrow">{t("正在刷新体检...")}</span> : null}
          <section className="diagnostics-prompt-panel diagnostics-prompt-inline" aria-label={t("给 Codex 的修复 prompt")}>
            <div className="diagnostics-prompt-controls">
              <details
                ref={repairPromptRef}
                className="diagnostics-prompt-preview"
                open={repairPromptOpen}
                onKeyDownCapture={(event) => {
                  if (event.key !== "Escape") return;
                  event.preventDefault();
                  closeRepairPrompt();
                }}
                onToggle={(event) => setRepairPromptOpen(event.currentTarget.open)}
              >
                <summary>{t("展开 prompt")}</summary>
                <div className="diagnostics-prompt-popover">
                  <p>{t("这段 prompt 会带上体检摘要、问题、证据路径和执行边界，要求目标 Codex 先复核再修复。")}</p>
                  <pre>{repairPrompt || t("没有可复制的修复 prompt")}</pre>
                </div>
              </details>
              <button className="top-action" onClick={() => void handleCopyRepairPrompt()} type="button" disabled={!repairPrompt}>
                <Copy size={16} />
                {repairPromptCopied ? t("已复制") : t("复制 prompt")}
              </button>
            </div>
            {repairPromptCopyError ? <p className="diagnostics-prompt-error" role="alert">{t("复制失败")}：{repairPromptCopyError}</p> : null}
          </section>
          <button className="top-action" onClick={onRefresh} type="button" disabled={isLoading}>
            <RefreshCcw size={16} />
            {t("重新体检")}
          </button>
        </div>
      </div>

      <section className={`diagnostics-summary-strip diagnostics-status-${report.status}`}>
        <div className="diagnostics-score-chip">
          <ShieldCheck size={18} />
          <span>{t("健康分")}</span>
          <strong>{report.score}</strong>
          <em>{diagnosticSeverityLabel(report.status, t)}</em>
        </div>
        <div className="diagnostics-summary-items">
          {summaryItems.map((item) => (
            <span className={`diagnostics-summary-item ${item.tone}`} key={item.label}>
              <strong>{item.value}</strong>
              <em>{item.label}</em>
            </span>
          ))}
        </div>
        <code title={report.codexHome}>{report.codexHome}</code>
        <span className="diagnostics-generated">{t("生成时间")} {formatDate(report.generatedAtMs)}</span>
      </section>

      {report.capacityTrend ? <DiagnosticsCapacityTrend trend={report.capacityTrend} /> : null}

      <div className="diagnostics-grid">
        <section className="diagnostics-panel diagnostics-issues-panel">
          <div className="panel-title-row">
            <h3>{t("问题与建议")}</h3>
            <span>{formatCount(visibleIssues.length)} {t("条")}</span>
          </div>
          {visibleIssues.length ? (
            <div className="diagnostic-issue-list">
              {visibleIssues.map((issue) => (
                <DiagnosticIssueCard
                  key={issue.id}
                  issue={issue}
                  onOpen={() => setDiagnosticDetail({ kind: "issue", item: issue })}
                />
              ))}
            </div>
          ) : (
            <article className="diagnostic-card pass">
              <div className="diagnostic-card-head">
                <span className="diagnostic-pill pass"><CheckCircle2 size={16} /> {t("通过")}</span>
              </div>
              <h3>{t("未发现阻塞问题")}</h3>
              <p>{t("当前扫描范围内没有严重或警告级问题。")}</p>
            </article>
          )}
        </section>

        <aside className="diagnostics-panel diagnostics-checks-panel">
          <div className="panel-title-row">
            <h3>{t("检查明细")}</h3>
            <span>{formatCount(attentionChecks.length)} {t("项需关注")}</span>
          </div>
          <div className="diagnostics-filter-tabs" role="tablist" aria-label={t("检查明细")}>
            <button aria-controls="diagnostic-check-list" aria-selected={diagnosticFilter === "attention"} className={diagnosticFilter === "attention" ? "active" : ""} id="diagnostic-tab-attention" onClick={() => setDiagnosticFilter("attention")} role="tab" type="button">
              {t("关注")} <span>{formatCount(attentionChecks.length)}</span>
            </button>
            <button aria-controls="diagnostic-check-list" aria-selected={diagnosticFilter === "all"} className={diagnosticFilter === "all" ? "active" : ""} id="diagnostic-tab-all" onClick={() => setDiagnosticFilter("all")} role="tab" type="button">
              {t("全部")} <span>{formatCount(report.checks.length)}</span>
            </button>
            <button aria-controls="diagnostic-check-list" aria-selected={diagnosticFilter === "pass"} className={diagnosticFilter === "pass" ? "active" : ""} id="diagnostic-tab-pass" onClick={() => setDiagnosticFilter("pass")} role="tab" type="button">
              {t("通过")} <span>{formatCount(passedChecks.length)}</span>
            </button>
          </div>
          <div aria-labelledby={`diagnostic-tab-${diagnosticFilter}`} className="diagnostic-check-list" id="diagnostic-check-list" role="tabpanel">
            {filteredChecks.length ? filteredChecks.map((check) => (
              <DiagnosticCheckRow
                key={check.id}
                check={check}
                onOpen={() => setDiagnosticDetail({ kind: "check", item: check })}
              />
            )) : <p className="muted">{t("没有需要处理的建议")}</p>}
          </div>
        </aside>
      </div>

      {diagnosticDetail ? (
        <DiagnosticDetailModal target={diagnosticDetail} onClose={() => setDiagnosticDetail(null)} />
      ) : null}
    </div>
  );
}

function ResourcesModule({
  codexHome,
  overview,
  isLoading,
  selectedResourcePath,
  setSelectedResourcePath,
  resourceRead,
  resourceDraft,
  setResourceDraft,
  onBackupResource,
  onSaveResource,
  onReloadResource
}: {
  codexHome: string;
  overview: HomeOverview | null;
  isLoading: boolean;
  selectedResourcePath: string;
  setSelectedResourcePath: (value: string) => void;
  resourceRead: ResourceRead | null;
  resourceDraft: string;
  setResourceDraft: (value: string) => void;
  onBackupResource: () => void;
  onSaveResource: () => void;
  onReloadResource: () => void;
}) {
  const { t, formatDate } = useI18n();
  if (!overview && isLoading) {
    return <div className="loading-page">{t("扫描 Codex Home...")}</div>;
  }
  if (!overview) {
    return <div className="empty-state">{t("没有资源数据")}</div>;
  }

  const resourceGroups = overview.resources.reduce<Record<string, ResourceRecord[]>>((groups, resource) => {
    const key = resource.category || "other";
    groups[key] = groups[key] || [];
    groups[key].push(resource);
    return groups;
  }, {});

  return (
    <div className="module-stack">
      <div className="section-headline">
        <div>
          <span className="eyebrow">Codex Home</span>
          <h2>{t("资源、配置、记忆和指令管理")}</h2>
        </div>
        <code>{codexHome}</code>
      </div>

      <div className="metrics-row compact">
        <StatCard label={t("资源入口")} value={formatCount(overview.summary.existingResourceCount)} sublabel={t("已存在")} />
        <StatCard label="AGENTS.md" value={formatCount(overview.summary.agentsFileCount)} sublabel={t("指令文件")} tone="blue" />
        <StatCard label={t("记忆目录")} value={overview.summary.memoryExists ? t("存在") : t("缺失")} sublabel="memories" tone={overview.summary.memoryExists ? "green" : "amber"} />
        <StatCard label={t("技能目录")} value={overview.summary.skillsExists ? t("存在") : t("缺失")} sublabel="skills" tone={overview.summary.skillsExists ? "green" : "amber"} />
        <StatCard label={t("资源体量")} value={formatBytes(overview.summary.totalKnownResourceBytes)} sublabel={t("已知入口合计")} />
      </div>

      <div className="resource-workspace">
        <aside className="resource-browser">
          {Object.entries(resourceGroups).map(([category, resources]) => (
            <section key={category} className="resource-group">
              <h3>{category}</h3>
              {resources.map((resource) => (
                <button
                  key={resource.relativePath || resource.path}
                  className={selectedResourcePath === resource.relativePath ? "resource-row active" : "resource-row"}
                  onClick={() => setSelectedResourcePath(resource.relativePath)}
                  title={resource.path}
                >
                  {resource.kind === "directory" ? <Folder size={16} /> : <FileText size={16} />}
                  <span>{resource.label}</span>
                  <em>{resource.exists ? formatBytes(resource.sizeBytes) : t("缺失")}</em>
                </button>
              ))}
            </section>
          ))}
        </aside>

        <section className="resource-detail">
          {!resourceRead ? (
            <div className="empty-detail">
              <BookOpen size={28} />
              <span>{t("选择资源后可查看、备份或编辑文本内容")}</span>
            </div>
          ) : (
            <>
              <div className="resource-title">
                <div>
                  <span className={`resource-kind ${resourceRead.metadata.kind}`}>{resourceRead.metadata.kind}</span>
                  <h2>{resourceRead.metadata.relativePath || "CODEX_HOME"}</h2>
                  <p>{resourceRead.metadata.path}</p>
                </div>
                <div className="resource-actions">
                  <button onClick={onReloadResource}><RefreshCcw size={15} /> {t("重读")}</button>
                  <button onClick={onBackupResource}><ShieldCheck size={15} /> {t("备份资源")}</button>
                  {resourceRead.content !== null ? <button className="primary-action" onClick={onSaveResource}><Save size={15} /> {t("保存文本")}</button> : null}
                </div>
              </div>

              <div className="detail-grid">
                <div><span>{t("大小")}</span><strong>{formatBytes(resourceRead.metadata.sizeBytes)}</strong></div>
                <div><span>{t("文件数")}</span><strong>{formatCount(resourceRead.metadata.fileCount)}</strong></div>
                <div><span>{t("目录数")}</span><strong>{formatCount(resourceRead.metadata.directoryCount)}</strong></div>
                <div><span>{t("修改时间")}</span><strong>{formatDate(resourceRead.metadata.modifiedAtMs)}</strong></div>
              </div>

              {resourceRead.children ? (
                <div className="resource-children">
                  <h3>{t("目录内容")}</h3>
                  {resourceRead.children.map((child) => (
                    <button key={child.relativePath} onClick={() => setSelectedResourcePath(child.relativePath)} title={child.path}>
                      {child.kind === "directory" ? <Folder size={15} /> : <FileText size={15} />}
                      <span>{child.relativePath}</span>
                      <em>{formatBytes(child.sizeBytes)}</em>
                    </button>
                  ))}
                </div>
              ) : null}

              {resourceRead.binary ? (
                <div className="binary-note">{t("这是二进制资源，只显示元数据，不进入文本编辑器。")}</div>
              ) : null}

              {resourceRead.content !== null ? (
                <textarea
                  className="resource-editor"
                  aria-label={`${t("保存文本")}: ${resourceRead.metadata.relativePath || "CODEX_HOME"}`}
                  value={resourceDraft}
                  onChange={(event) => setResourceDraft(event.target.value)}
                  spellCheck={false}
                />
              ) : null}
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function ImportsModule({
  codexHome,
  sourceCodexHome,
  setSourceCodexHome,
  importThreadId,
  setImportThreadId,
  importThreadTargetProject,
  setImportThreadTargetProject,
  importProjectSourcePath,
  setImportProjectSourcePath,
  importProjectTargetPath,
  setImportProjectTargetPath,
  includeArchived,
  setIncludeArchived,
  preserveThreadIds,
  setPreserveThreadIds,
  copyRelativePath,
  setCopyRelativePath,
  copyTargetRelativePath,
  setCopyTargetRelativePath,
  overwriteResource,
  setOverwriteResource,
  onImportThread,
  onImportProject,
  onCopyResource
}: {
  codexHome: string;
  sourceCodexHome: string;
  setSourceCodexHome: (value: string) => void;
  importThreadId: string;
  setImportThreadId: (value: string) => void;
  importThreadTargetProject: string;
  setImportThreadTargetProject: (value: string) => void;
  importProjectSourcePath: string;
  setImportProjectSourcePath: (value: string) => void;
  importProjectTargetPath: string;
  setImportProjectTargetPath: (value: string) => void;
  includeArchived: boolean;
  setIncludeArchived: (value: boolean) => void;
  preserveThreadIds: boolean;
  setPreserveThreadIds: (value: boolean) => void;
  copyRelativePath: string;
  setCopyRelativePath: (value: string) => void;
  copyTargetRelativePath: string;
  setCopyTargetRelativePath: (value: string) => void;
  overwriteResource: boolean;
  setOverwriteResource: (value: boolean) => void;
  onImportThread: () => void;
  onImportProject: () => void;
  onCopyResource: () => void;
}) {
  const { t } = useI18n();

  return (
    <div className="module-stack">
      <div className="section-headline">
        <div>
          <span className="eyebrow">Cross Home</span>
          <h2>{t("从其他 .codex 导入线程、项目和资源")}</h2>
        </div>
        <code>{t("目标：")}{codexHome}</code>
      </div>

      <section className="import-source-card">
        <label>{t("来源 CODEX_HOME")}</label>
        <input aria-label={t("来源 CODEX_HOME")} value={sourceCodexHome} onChange={(event) => setSourceCodexHome(event.target.value)} placeholder={t("例如 E:\\backup\\.codex")} />
        <p className="field-hint">{t("填写另一个 Codex Home 的根目录，例如备份盘或旧机器上的 `.codex` 文件夹；不要填单个 JSONL 文件。")}</p>
      </section>

      <div className="operation-grid">
        <section className="operation-card">
          <div className="card-icon"><Import size={18} /></div>
          <h3>{t("导入单个线程")}</h3>
          <p>{t("从另一个 `.codex` 的 SQLite 和 JSONL 中复制一个线程，默认生成新线程 ID。")}</p>
          <label>{t("来源线程 ID")}</label>
          <input aria-label={t("来源线程 ID")} value={importThreadId} onChange={(event) => setImportThreadId(event.target.value)} placeholder={t("例如 019de30a-27b8-7663-aa6a-6f9bf947202e")} />
          <p className="field-hint">{t("线程 ID 是 36 位 UUID，可从线程详情、JSONL 文件名或源 `.codex` 的线程列表中复制。")}</p>
          <label>{t("目标项目路径")}</label>
          <input aria-label={t("目标项目路径")} value={importThreadTargetProject} onChange={(event) => setImportThreadTargetProject(event.target.value)} placeholder={t("例如 C:\\Projects\\ImportedProject")} />
          <p className="field-hint">{t("留空会沿用源线程原来的 cwd；填写后会把复制出来的新线程绑定到这个项目路径。")}</p>
          <button onClick={onImportThread}><FolderInput size={15} /> {t("导入线程")}</button>
        </section>

        <section className="operation-card">
          <div className="card-icon"><Layers size={18} /></div>
          <h3>{t("导入整个项目")}</h3>
          <p>{t("复制来源项目下的所有匹配线程，可选择是否包括归档线程。")}</p>
          <label>{t("来源项目路径")}</label>
          <input aria-label={t("来源项目路径")} value={importProjectSourcePath} onChange={(event) => setImportProjectSourcePath(event.target.value)} placeholder={t("例如 C:\\Documents\\Codex\\2026-05-01\\research")} />
          <p className="field-hint">{t("填写源 `.codex` 中线程记录的项目路径/cwd，不是项目显示名，也不是 `.codex` 根目录。")}</p>
          <label>{t("目标项目路径")}</label>
          <input aria-label={t("目标项目路径")} value={importProjectTargetPath} onChange={(event) => setImportProjectTargetPath(event.target.value)} placeholder={t("例如 C:\\Projects\\ResearchImported")} />
          <p className="field-hint">{t("留空会保留来源项目路径；填写后会把导入的项目线程整体映射到新路径。")}</p>
          <label className="checkbox-line"><input type="checkbox" checked={includeArchived} onChange={(event) => setIncludeArchived(event.target.checked)} /> {t("包括归档线程")}</label>
          <label className="checkbox-line"><input type="checkbox" checked={preserveThreadIds} onChange={(event) => setPreserveThreadIds(event.target.checked)} /> {t("可用时保留原线程 ID")}</label>
          <button onClick={onImportProject}><FolderInput size={15} /> {t("导入项目线程")}</button>
        </section>

        <section className="operation-card">
          <div className="card-icon"><Copy size={18} /></div>
          <h3>{t("复制 Codex Home 资源")}</h3>
          <p>{t("用于迁移 `AGENTS.md`、`memories`、`skills`、配置片段或其他相对路径资源。")}</p>
          <label>{t("来源相对路径")}</label>
          <input aria-label={t("来源相对路径")} value={copyRelativePath} onChange={(event) => setCopyRelativePath(event.target.value)} placeholder={t("例如 AGENTS.md、memories、skills/my-skill")} />
          <p className="field-hint">{t("相对路径会从来源 CODEX_HOME 下面解析；复制目录时会按目录整体复制。")}</p>
          <label>{t("目标相对路径")}</label>
          <input aria-label={t("目标相对路径")} value={copyTargetRelativePath} onChange={(event) => setCopyTargetRelativePath(event.target.value)} placeholder={t("例如 memories/imported 或留空同名复制")} />
          <p className="field-hint">{t("留空时目标路径与来源相同；填写后可以把资源复制到当前 CODEX_HOME 的另一个相对位置。")}</p>
          <label className="checkbox-line"><input type="checkbox" checked={overwriteResource} onChange={(event) => setOverwriteResource(event.target.checked)} /> {t("允许覆盖目标资源")}</label>
          <button onClick={onCopyResource}><Copy size={15} /> {t("复制资源")}</button>
        </section>
      </div>
    </div>
  );
}

function ApiModule({ capabilities }: { capabilities: CapabilityResponse | null }) {
  const { t } = useI18n();
  const baseUrl = apiDisplayBaseUrl();
  const hasLiveCapabilities = Boolean(capabilities);
  const openapiPath = capabilities?.openapiPath || "/openapi.json";
  const mcpPath = capabilities?.mcpPath || "/mcp";
  const openapiUrl = resolveApiUrl(openapiPath);
  const mcpUrl = resolveApiUrl(mcpPath);
  const writeGate = String(capabilities?.safetyModel?.runningCodexWriteGate || "");
  const protectedPaths = capabilities?.safetyModel?.protectedTextWritePaths;
  const mcpConfig = JSON.stringify({
    mcpServers: {
      "codex-home-manager": {
        type: "streamable-http",
        url: mcpUrl
      }
    }
  }, null, 2);
  const mcpPreviewCall = JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    method: "tools/call",
    params: {
      name: "codex_preview_thread_action",
      arguments: {
        threadId: "THREAD_ID",
        action: "show"
      }
    }
  }, null, 2);
  const mcpWriteCall = JSON.stringify({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: {
      name: "codex_show_thread",
      arguments: {
        threadId: "THREAD_ID",
        apiToken: "LOCAL_TOKEN",
        operationPreviewId: "PREVIEW_ID",
        inputHash: "INPUT_HASH",
        acknowledgeCodexRunningRisk: true
      }
    }
  }, null, 2);
  return (
    <div className="module-stack">
      <div className="section-headline">
        <div>
          <span className="eyebrow">Agent API</span>
          <h2>{t("给新 agent 直接调用的稳定本地 API")}</h2>
        </div>
        <div className="api-links">
          <a className="openapi-link" href={mcpUrl} target="_blank" rel="noreferrer">{t("MCP endpoint")}: {mcpUrl}</a>
          <a className="openapi-link" href={openapiUrl} target="_blank" rel="noreferrer">{t("OpenAPI JSON")}: {openapiUrl}</a>
        </div>
      </div>

      {!hasLiveCapabilities ? (
        <div className="api-offline-note">
          <ShieldCheck size={16} />
          <div>
            <strong>{t("公开 API 说明")}</strong>
            <span>{t("当前未连接本机连接器，下面展示公开接入说明；tools/list、OpenAPI JSON 和真实写入能力需要先启动 http://127.0.0.1:8765。")}</span>
          </div>
        </div>
      ) : null}

      <div className="api-hero">
        <div>
          <Code2 size={22} />
          <h3>{t("MCP 优先接入")}</h3>
          <p>{t("把本地连接器作为 streamable HTTP MCP server 注册。agent 先 tools/list，再按工具 schema 调用；写入前先用对应 preview 工具取得 operationPreviewId 和 inputHash。")}</p>
        </div>
        <section className="api-code-block">
          <h3>{t("MCP 配置")}</h3>
          <pre>{mcpConfig}</pre>
        </section>
      </div>

      <div className="api-safety">
        <section>
          <h3>{t("写入安全模型")}</h3>
          <p>{writeGate || t("MCP 写工具同样需要 apiToken、operationPreviewId、inputHash；Codex 正在运行时还需要 acknowledgeCodexRunningRisk=true。")}</p>
          <code>acknowledgeCodexRunningRisk=true</code>
        </section>
        <section>
          <h3>{t("受保护资源")}</h3>
          <p>{Array.isArray(protectedPaths) ? protectedPaths.join(", ") : "state_5.sqlite, .codex-global-state.json, config.toml, sessions/**"}</p>
        </section>
      </div>

      <div className="api-examples">
        <section>
          <h3>{t("MCP 工具调用")}</h3>
          <pre>{`POST ${mcpUrl}\n\n${mcpPreviewCall}`}</pre>
        </section>
        <section>
          <h3>{t("先预览再写入")}</h3>
          <p>{t("示例先预览显示线程，再把返回的 operationPreviewId 和 inputHash 传给写工具。")}</p>
          <pre>{mcpWriteCall}</pre>
        </section>
        <section>
          <h3>{t("REST/OpenAPI 兜底")}</h3>
          <p>{t("REST 仍然保留给脚本和不支持 MCP 的 agent；严格 schema 可读取 OpenAPI。")}</p>
          <pre>{`curl ${baseUrl}/api/capabilities\ncurl ${openapiUrl}`}</pre>
        </section>
        <section>
          <h3>{t("读取 AGENTS.md")}</h3>
          <pre>{`curl "${baseUrl}/api/resources/read?relative_path=AGENTS.md"`}</pre>
        </section>
      </div>

      <div className="panel-title-row api-capability-heading">
        <h3>{t("能力列表")}</h3>
        <span>{formatCount(capabilities?.capabilities.length || 0)}</span>
      </div>
      <div className="capability-list">
        {(capabilities?.capabilities || []).map((capability) => (
          <article key={capability.name} className="capability-card">
            <span>{capability.method}</span>
            <h3>{capability.name}</h3>
            <code>{capability.path}</code>
            <p>{capability.purpose}</p>
            <em>{t("必填：")}{capability.required.length ? capability.required.join(", ") : t("无")}{t("；备份：")}{capability.backup}</em>
            <em>{t("风险：")}{capability.riskLevel || "read"}{t("；幂等性：")}{capability.idempotency || "-"}</em>
            <em>{t("返回：")}{capability.successFields.join(", ")}</em>
            {capability.previewEndpoint ? <em>{t("预览：")}{capability.previewEndpoint}</em> : null}
            {capability.writeEndpoint ? <em>{t("写入：")}{capability.writeEndpoint}</em> : null}
            {capability.bodyExample ? (
              <pre>{JSON.stringify(capability.bodyExample, null, 2)}</pre>
            ) : null}
            {capability.rollback ? <em>{t("回滚：")}{capability.rollback}{capability.rollbackMode ? `; ${capability.rollbackMode}` : ""}</em> : null}
          </article>
        ))}
      </div>
    </div>
  );
}

const logKindOptions = [
  ["all", "全部"],
  ["request", "请求"],
  ["failure", "失败/警告"],
  ["error", "错误"],
  ["app_log", "应用日志"],
  ["tool", "工具"],
  ["tool_output", "工具输出"],
  ["event", "事件"],
  ["user", "用户"],
  ["assistant", "助手"],
  ["session", "会话"],
  ["reasoning", "推理"],
  ["parse_error", "解析错误"]
];

const logSourceOptions = [
  ["all", "综合"],
  ["app", "请求/错误库"],
  ["rollout", "会话 JSONL"]
];

function severityLabel(severity: ThreadLogEntry["severity"], t: Translator): string {
  if (severity === "error") return t("错误");
  if (severity === "warning") return t("警告");
  return t("信息");
}

function ThreadLogModal({
  thread,
  logs,
  isLoading,
  error,
  kind,
  source,
  search,
  offset,
  limit,
  onKindChange,
  onSourceChange,
  onSearchChange,
  onPreviousPage,
  onNextPage,
  onRefresh,
  onClose
}: {
  thread: ThreadRecord | null;
  logs: ThreadLogs | null;
  isLoading: boolean;
  error: string;
  kind: string;
  source: string;
  search: string;
  offset: number;
  limit: number;
  onKindChange: (value: string) => void;
  onSourceChange: (value: string) => void;
  onSearchChange: (value: string) => void;
  onPreviousPage: () => void;
  onNextPage: () => void;
  onRefresh: () => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useModalAccessibility(Boolean(thread), onClose);
  if (!thread) return null;
  const severitySummary = logs?.summary.bySeverity || {};
  const startLine = logs ? offset + 1 : 0;
  const endLine = logs ? offset + logs.entries.length : 0;
  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="log-modal" role="dialog" aria-modal="true" aria-label={t("线程详细日志")} tabIndex={-1}>
        <header className="modal-header">
          <div>
            <span className="eyebrow">Thread Logs</span>
            <h2>{thread.title}</h2>
            <p>{thread.id}</p>
          </div>
          <button className="icon-button" onClick={onClose} title={t("关闭日志窗口")} aria-label={t("关闭日志窗口")} data-dialog-initial-focus type="button">
            <X size={16} />
          </button>
        </header>

        <div className="log-controls">
          <select aria-label={t("来源")} value={source} onChange={(event) => onSourceChange(event.target.value)}>
            {logSourceOptions.map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}
          </select>
          <select aria-label={t("类型")} value={kind} onChange={(event) => onKindChange(event.target.value)}>
            {logKindOptions.map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}
          </select>
          <input aria-label={t("搜索错误、请求 URL、工具名、原始行")} value={search} onChange={(event) => onSearchChange(event.target.value)} placeholder={t("搜索错误、请求 URL、工具名、原始行")} />
          <button onClick={onRefresh} type="button"><RefreshCcw size={15} /> {t("重读")}</button>
        </div>

        <div className="log-summary">
          <span>{t("匹配")} {formatCount(logs?.matchedEntries || 0)}</span>
          <span>{t("行数")} {formatCount(logs?.summary.lineCount || 0)}</span>
          <span>{t("错误")} {formatCount(severitySummary.error || 0)}</span>
          <span>{t("警告")} {formatCount(severitySummary.warning || 0)}</span>
          <span>{t("解析错误")} {formatCount(logs?.summary.parseErrors || 0)}</span>
          {logs?.rolloutPath ? <code>{logs.rolloutPath}</code> : null}
          {logs?.appLogPath ? <code>{logs.appLogPath}</code> : null}
        </div>

        {error ? <div className="inline-error" role="alert">{error}</div> : null}
        {isLoading ? <div className="panel-loading" role="status" aria-live="polite">{t("读取日志...")}</div> : null}
        {!isLoading && logs ? (
          <>
            <div className="log-list">
              {logs.entries.length ? logs.entries.map((entry) => (
                <article key={`${entry.source}-${entry.lineNumber ?? entry.appLogId}-${entry.kind}`} className={`log-entry ${entry.severity}`}>
                  <div className="log-entry-head">
                    <span className={`severity-pill ${entry.severity}`}>{severityLabel(entry.severity, t)}</span>
                    <strong>{entry.label || entry.kind}</strong>
                    <em>{entry.source === "app_sqlite" ? `log #${entry.appLogId}` : `line ${entry.lineNumber}`}</em>
                    {entry.timestamp ? <time>{entry.timestamp}</time> : null}
                  </div>
                  <div className="log-entry-meta">
                    <span>{entry.source === "app_sqlite" ? "logs_2.sqlite" : "JSONL"}</span>
                    <span>{entry.kind}</span>
                    {entry.payloadType ? <span>{entry.payloadType}</span> : null}
                    {entry.role ? <span>{entry.role}</span> : null}
                    {entry.level ? <span>{entry.level}</span> : null}
                    {entry.file ? <span>{entry.file}{entry.fileLine ? `:${entry.fileLine}` : ""}</span> : null}
                  </div>
                  <p>{entry.message || t("(empty)")}</p>
                  <details className="log-raw">
                    <summary>{t("原始记录")}{entry.rawLineTruncated ? t("（已截断）") : ""}</summary>
                    <pre>{entry.rawLine}</pre>
                  </details>
                </article>
              )) : <div className="empty-state">{t("没有匹配日志")}</div>}
            </div>
            <footer className="modal-footer">
              <span>{logs.entries.length ? `${formatCount(startLine)}-${formatCount(endLine)} / ${formatCount(logs.matchedEntries)}` : "0 / 0"}</span>
              <div>
                <button onClick={onPreviousPage} disabled={offset <= 0} type="button">{t("上一页")}</button>
                <button onClick={onNextPage} disabled={!logs.hasMore} type="button">{t("下一页")}</button>
              </div>
            </footer>
          </>
        ) : null}
      </section>
    </div>
  );
}

function ThreadPromptModal({
  thread,
  prompts,
  isLoading,
  error,
  onClose
}: {
  thread: ThreadRecord | null;
  prompts: ThreadPrompts | null;
  isLoading: boolean;
  error: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useModalAccessibility(Boolean(thread), onClose);
  const [copied, setCopied] = React.useState(false);
  const [filterMode, setFilterMode] = React.useState<PromptFilterMode>("pure");
  const [copyMode, setCopyMode] = React.useState<PromptCopyMode>("clean");
  const [copySpacingMode, setCopySpacingMode] = React.useState<PromptCopySpacingMode>("spaced");

  React.useEffect(() => {
    setCopied(false);
  }, [thread?.id, prompts?.promptCount]);

  React.useEffect(() => {
    setFilterMode("pure");
    setCopyMode("clean");
    setCopySpacingMode("spaced");
  }, [thread?.id]);

  React.useEffect(() => {
    setCopied(false);
  }, [copyMode, copySpacingMode, filterMode]);

  const normalizedPrompts = React.useMemo(
    () => (prompts?.prompts || []).map((prompt) => normalizePromptRecord(prompt)),
    [prompts]
  );
  const purePromptCount = prompts?.purePromptCount ?? normalizedPrompts.filter((prompt) => prompt.hasPureText).length;
  const visiblePromptCount = normalizedPrompts.filter((prompt) => prompt.visibleByDefault !== false).length;
  const subagentPromptCount = normalizedPrompts.filter((prompt) => prompt.sourceType === "subagent").length;
  const automationPromptCount = normalizedPrompts.filter((prompt) => prompt.sourceType === "automation").length;
  const delegationPromptCount = normalizedPrompts.filter((prompt) => prompt.sourceType === "delegation").length;
  const internalPromptCount = normalizedPrompts.filter((prompt) => prompt.sourceType === "internal").length;
  const goalPromptCount = normalizedPrompts.filter((prompt) => prompt.sourceType === "goal").length;
  const sourceCounts = prompts?.sourceCounts || promptSourceCounts(normalizedPrompts);
  const filteredPrompts = normalizedPrompts.filter((prompt) => promptMatchesFilter(prompt, filterMode));
  const hiddenByFilterCount = Math.max(0, normalizedPrompts.length - filteredPrompts.length);
  const filterOptions: Array<{ value: PromptFilterMode; label: string; count: number; description: string }> = [
    {
      value: "pure",
      label: t("纯文本输入"),
      count: purePromptCount,
      description: t("只显示你输入的请求文字，剔除文件列表、图片标签、子 agent 和内部上下文")
    },
    {
      value: "focused",
      label: t("上下文+输入"),
      count: visiblePromptCount,
      description: t("显示用户输入、附件上下文和浏览器上下文，隐藏子 agent 与内部上下文")
    },
    {
      value: "withAgents",
      label: t("含子 agent"),
      count: visiblePromptCount + subagentPromptCount,
      description: t("显示用户输入、上下文和子 agent 通知")
    },
    {
      value: "automation",
      label: t("自动化任务"),
      count: automationPromptCount,
      description: t("只显示 heartbeat、定时任务和自动化续跑注入的任务内容")
    },
    {
      value: "delegation",
      label: t("线程转发"),
      count: delegationPromptCount,
      description: t("只显示由其他 Codex 线程发送到当前线程的委派消息")
    },
    {
      value: "all",
      label: t("全部"),
      count: normalizedPrompts.length,
      description: t("显示所有用户角色记录")
    }
  ];
  const sourceSummaryItems = [
    { key: "user", label: t("用户输入"), count: sourceCounts.user || 0 },
    { key: "attachment", label: t("附件上下文"), count: sourceCounts.attachment || 0 },
    { key: "browser", label: t("浏览器上下文"), count: sourceCounts.browser || 0 },
    { key: "context", label: t("用户上下文"), count: sourceCounts.context || 0 },
    { key: "automation", label: t("自动化任务"), count: automationPromptCount },
    { key: "delegation", label: t("线程转发"), count: delegationPromptCount },
    { key: "subagent", label: t("子 agent"), count: subagentPromptCount },
    { key: "goal", label: t("续跑目标"), count: goalPromptCount },
    { key: "internal", label: t("内部上下文"), count: internalPromptCount }
  ].filter((item) => item.count > 0);

  if (!thread) return null;

  const allPromptText = filteredPrompts.map((prompt) => {
    const rawPromptText = copyMode === "clean" ? promptTextForCleanCopy(prompt) : promptTextForFilter(prompt, filterMode).trim();
    const promptText = copySpacingMode === "compact" ? removeBlankLines(rawPromptText) : rawPromptText;
    if (!promptText) return "";
    if (copyMode === "clean") return promptText;
    return (
      `## Prompt ${prompt.index}\n\n` +
      `${t("行")} ${prompt.lineNumber}${prompt.timestamp ? ` | ${prompt.timestamp}` : ""}` +
      `${prompt.sourceLabel ? ` | ${prompt.sourceLabel}` : ""}\n\n` +
      promptText
    );
  }).filter(Boolean).join(
    copySpacingMode === "compact" ? (copyMode === "metadata" ? "\n---\n" : "\n") : (copyMode === "clean" ? "\n\n" : "\n\n---\n\n")
  ) || "";

  async function copyAllPrompts() {
    if (!allPromptText) return;
    try {
      await copyTextToClipboard(allPromptText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  const closeOnBackdrop = (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) onClose();
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={closeOnBackdrop}>
      <section ref={dialogRef} className="prompt-modal" role="dialog" aria-modal="true" aria-label={t("线程 prompts")} tabIndex={-1}>
        <header className="modal-header">
          <div>
            <span className="eyebrow">{t("线程 prompts")}</span>
            <h2>{thread.title}</h2>
            <p>{thread.id}</p>
          </div>
          <button className="icon-button" onClick={onClose} title={t("关闭详情窗口")} aria-label={t("关闭详情窗口")} data-dialog-initial-focus type="button">
            <X size={16} />
          </button>
        </header>

        <div className="prompt-modal-toolbar">
          <div className="prompt-modal-summary">
            <strong>{formatCount(filteredPrompts.length)}</strong>
            <span>{t("当前显示")}</span>
            <span>{t("总计")} {formatCount(normalizedPrompts.length)}</span>
            {hiddenByFilterCount ? <span>{t("已隐藏")} {formatCount(hiddenByFilterCount)}</span> : null}
            {prompts?.rolloutPath ? <code title={prompts.rolloutPath}>{prompts.rolloutPath}</code> : null}
          </div>
          <div className="prompt-copy-actions">
            <div className="prompt-copy-mode" role="group" aria-label={t("复制格式")}>
              <span>{t("复制格式")}</span>
              <button
                className={copyMode === "clean" ? "active" : ""}
                onClick={() => setCopyMode("clean")}
                title={t("只复制 prompt 正文，不包含编号、行号、时间、来源或分隔线。")}
                type="button"
              >
                {t("仅正文")}
              </button>
              <button
                className={copyMode === "metadata" ? "active" : ""}
                onClick={() => setCopyMode("metadata")}
                title={t("复制 prompt 正文和编号、行号、时间、来源、分隔线，方便回溯定位。")}
                type="button"
              >
                {t("带元信息")}
              </button>
            </div>
            <div className="prompt-copy-mode prompt-copy-spacing" role="group" aria-label={t("空行处理")}>
              <span>{t("空行")}</span>
              <button
                className={copySpacingMode === "spaced" ? "active" : ""}
                onClick={() => setCopySpacingMode("spaced")}
                title={t("保留 prompt 之间和 prompt 内部的空白行。")}
                type="button"
              >
                {t("保留")}
              </button>
              <button
                className={copySpacingMode === "compact" ? "active" : ""}
                onClick={() => setCopySpacingMode("compact")}
                title={t("复制时移除空白行，并用单换行连接多条 prompt。")}
                type="button"
              >
                {t("无空行")}
              </button>
            </div>
            <button className="prompt-copy-button" onClick={() => void copyAllPrompts()} disabled={!allPromptText || isLoading} type="button">
              <Copy size={15} />
              {copied ? t("已复制") : copyMode === "metadata" ? t("复制带元信息") : t("复制干净文本")}
            </button>
          </div>
        </div>

        <div className="prompt-filter-bar" aria-label={t("Prompt 筛选")}>
          <div className="prompt-filter-tabs" role="group" aria-label={t("Prompt 来源筛选")}>
            {filterOptions.map((option) => (
              <button
                key={option.value}
                className={filterMode === option.value ? "active" : ""}
                onClick={() => setFilterMode(option.value)}
                title={option.description}
                type="button"
              >
                <span>{option.label}</span>
                <strong>{formatCount(option.count)}</strong>
              </button>
            ))}
          </div>
          <div className="prompt-source-summary" aria-label={t("Prompt 来源统计")}>
            {sourceSummaryItems.map((item) => <span key={item.key}>{item.label} {formatCount(item.count)}</span>)}
          </div>
        </div>

        <div className="prompt-modal-content">
          {error ? <div className="inline-error" role="alert">{error}</div> : null}
          {isLoading ? <div className="panel-loading" role="status" aria-live="polite">{t("读取 prompts...")}</div> : null}
          {!isLoading && prompts ? (
            <div className="prompt-list">
              {filteredPrompts.length ? filteredPrompts.map((prompt) => {
                const displayText = promptTextForFilter(prompt, filterMode);
                return (
                  <article key={`${prompt.index}-${prompt.lineNumber}`} className={`prompt-entry prompt-entry-${prompt.sourceType || "unknown"}`}>
                    <div className="prompt-entry-head">
                      <strong>Prompt {formatCount(prompt.index)}</strong>
                      <span>{t("行")} {formatCount(prompt.lineNumber)}</span>
                      {prompt.timestamp ? <time>{prompt.timestamp}</time> : null}
                      {prompt.sourceLabel ? <span className="prompt-source-badge">{prompt.sourceLabel}</span> : null}
                      <em>{formatCount(displayText.length)} {t("字符")}</em>
                    </div>
                    <pre>{displayText}</pre>
                  </article>
                );
              }) : <div className="empty-state">{normalizedPrompts.length ? t("当前筛选没有 prompt") : t("没有 prompt")}</div>}
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}

function LocalApiConnectionGate({
  status,
  message,
  codexHome,
  apiBaseUrl,
  language,
  onRetry,
  onBrowserScan,
  browserScanLoading,
  browserScanSupported
}: {
  status: LocalApiConnectionStatus;
  message: string;
  codexHome: string;
  apiBaseUrl: string;
  language: Language;
  onRetry: () => void;
  onBrowserScan: () => void;
  browserScanLoading: boolean;
  browserScanSupported: boolean;
}) {
  const isChecking = status === "checking";
  const hostedConsole = isHostedConsole();
  const browserPermissionLine = language === "en"
    ? "Chrome/Edge uses a fixed permission prompt that says a site can view and copy files. To avoid that trust problem, the hosted page keeps folder preview behind this advanced read-only action."
    : "Chrome/Edge 会使用“网站可以查看和复制文件”的固定权限提示。为避免造成窃取数据的误解，公开站不会默认触发它，只把文件夹预览放在这个高级只读入口里。";
  const launchLabel = language === "en" ? "Start local connector" : "启动本机连接器";
  const downloadLabel = language === "en" ? "Download Windows app" : "下载 Windows 软件";
  const zipDownloadLabel = language === "en" ? "ZIP fallback" : "备用 ZIP";
  const smartscreenHint = language === "en"
    ? "Windows SmartScreen may warn because this build is unsigned. Choose More info, then Run anyway to start it."
    : "Windows SmartScreen 可能会提示无法识别的应用；点击“更多信息”，再点“仍要运行”即可启动。";
  const retryLabel = isChecking
    ? (language === "en" ? "Checking..." : "正在检测...")
    : (language === "en" ? "Retry scan connection" : "重试扫描连接");
  const connectionTitle = localApiConnectionTitle(language, status);
  const connectionDescription = localApiConnectionDescription(language, status);
  const permissionHint = language === "en"
    ? "If the connector is already running but this page still cannot connect, open the site controls next to the address bar, set Local Network Access or Apps on device to Allow, then retry."
    : "如果本机连接器已经在运行但页面仍无法连接，请打开地址栏旁边的站点权限，把“本地网络访问”或“本机应用”设为允许，然后重试。";

  return (
    <section className="connection-gate" aria-live="polite">
      <div className="connection-card compact">
        <div className="connection-hero">
          <div className={`connection-icon ${isChecking ? "checking" : "blocked"}`}>
            {isChecking ? <RefreshCcw size={24} /> : <ShieldCheck size={24} />}
          </div>
          <div className="connection-copy">
            <h2>{connectionTitle}</h2>
            <p>{connectionDescription}</p>
          </div>
        </div>
        <div className="connector-actions">
          {hostedConsole ? (
            <a className="primary-action connector-launch" href={localConnectorLaunchUrl}>
              <ServerCog size={16} />
              {launchLabel}
            </a>
          ) : null}
          <a className="secondary-action" href={localConnectorDownloadUrl}>
            <Download size={16} />
            {downloadLabel}
          </a>
          <button className="primary-action connection-retry" type="button" onClick={onRetry} disabled={isChecking}>
            <RefreshCcw size={16} />
            {retryLabel}
          </button>
        </div>
        <div className="connection-mode-summary compact">
          <article>
            <strong>{language === "en" ? "Local boundary" : "本机边界"}</strong>
            <span>
              {language === "en"
                ? "The connector scans the selected Codex Home on your computer and exposes only localhost APIs to this page."
                : "连接器在你的电脑上扫描选定的 Codex Home，并只向本页面提供 localhost API。"}
            </span>
          </article>
          <article>
            <strong>{language === "en" ? "Full feature set" : "完整功能"}</strong>
            <span>
              {language === "en"
                ? "Repair, migration, slimming, deletion, MCP and process checks require the local connector."
                : "修复、迁移、瘦身、删除、MCP 和进程检查都需要本机连接器执行。"}
            </span>
          </article>
          {hostedConsole ? (
            <article>
              <strong>{language === "en" ? "Browser permission" : "浏览器权限"}</strong>
              <span>{permissionHint}</span>
            </article>
          ) : null}
        </div>
        <details className="connection-details">
          <summary>{language === "en" ? "Advanced: read-only browser preview and connection details" : "高级：只读浏览器预览与连接详情"}</summary>
          <dl className="connection-facts">
            <div>
              <dt>{language === "en" ? "API" : "本地 API"}</dt>
              <dd>{apiBaseUrl}</dd>
            </div>
            <div>
              <dt>CODEX_HOME</dt>
              <dd>{codexHomeDisplayValue(codexHome, language)}</dd>
            </div>
          </dl>
          {browserScanSupported ? (
            <div className="browser-folder-disclosure">
              <p>{browserPermissionLine}</p>
              <button className="secondary-action browser-folder-action" type="button" onClick={onBrowserScan} disabled={browserScanLoading}>
                <FolderInput size={16} />
                {browserScanLoading
                  ? (language === "en" ? "Scanning..." : "正在扫描...")
                  : (language === "en" ? "Use read-only folder preview" : "使用只读文件夹预览")}
              </button>
            </div>
          ) : (
            <p className="browser-folder-disclosure">{language === "en"
              ? "This browser does not support direct folder preview. Use Microsoft Edge or Google Chrome, or start the local connector."
              : "当前浏览器不支持直接文件夹预览。请使用 Microsoft Edge 或 Google Chrome，或启动本机连接器。"}</p>
          )}
          <div className="connector-download-links">
            <a href={localConnectorZipDownloadUrl}><Archive size={15} />{zipDownloadLabel}</a>
          </div>
          <p className="connector-trust-line">
            <ShieldCheck size={15} />
            <span>{smartscreenHint}</span>
          </p>
          {message && !isChecking ? <code className="connection-message">{message}</code> : null}
        </details>
      </div>
    </section>
  );
}

function App() {
  const [activeSection, setActiveSection] = React.useState<AppSection>("threads");
  const [codexHome, setCodexHome] = React.useState(defaultCodexHome);
  const sidebarLimit = 50;
  const [notice, setNotice] = React.useState("");
  const [actionError, setActionError] = React.useState("");
  const [writeWarnings, setWriteWarnings] = React.useState<string[]>([]);
  const [currentVersions, setCurrentVersions] = React.useState<CodexCurrentVersions | null>(null);
  const [createBackups, setCreateBackups] = React.useState(true);
  const [browserWorkspace, setBrowserWorkspace] = React.useState<BrowserCodexWorkspace | null>(null);
  const [browserScanLoading, setBrowserScanLoading] = React.useState(false);
  const [language, setLanguage] = React.useState<Language>(() => {
    try {
      return normalizeLanguage(window.localStorage.getItem(languageStorageKey));
    } catch {
      return "zh";
    }
  });
  const [apiToken, setApiToken] = React.useState("");
  const [apiTokenHeaderName, setApiTokenHeaderName] = React.useState("X-Codex-Manager-Token");
  const [localApiConnection, setLocalApiConnection] = React.useState<{ status: LocalApiConnectionStatus; message: string }>({
    status: "checking",
    message: ""
  });
  const isLocalApiConnected = localApiConnection.status === "connected";
  const isBrowserMode = Boolean(browserWorkspace);
  const snapshotState = useSnapshot(codexHome, sidebarLimit, isLocalApiConnected && !isBrowserMode);
  const overviewState = useHomeOverview(codexHome, isLocalApiConnected && !isBrowserMode);
  const capabilityState = useCapabilities(language, isLocalApiConnected);
  const diagnosticsState = useDiagnostics(
    codexHome,
    sidebarLimit,
    language,
    isLocalApiConnected && !isBrowserMode && activeSection === "diagnostics"
  );
  const activeSnapshot = (browserWorkspace?.snapshot as Snapshot | undefined) || snapshotState.snapshot;
  const activeOverview = (browserWorkspace?.overview as HomeOverview | undefined) || overviewState.overview;
  const activeDiagnostics = (browserWorkspace?.diagnostics as DiagnosticsReport | undefined) || diagnosticsState.report;
  const activeCodexHomeLabel = browserWorkspace?.displayPath || codexHome;
  const t = React.useCallback<Translator>((text) => translateText(language, text), [language]);
  const formatDate = React.useCallback((value: number | null) => formatDateForLanguage(value, language), [language]);
  const i18nValue = React.useMemo<I18nContextValue>(() => ({ language, t, formatDate }), [formatDate, language, t]);

  React.useEffect(() => {
    try {
      window.localStorage.setItem(languageStorageKey, language);
      document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    } catch {
      // Language persistence is a convenience; UI should still switch if storage is unavailable.
    }
  }, [language]);

  const [selectedThreadId, setSelectedThreadId] = React.useState<string | null>(null);
  const [detail, setDetail] = React.useState<ThreadDetail | null>(null);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [dailyTokenLoading, setDailyTokenLoading] = React.useState(false);
  const [duplicateTargetPath, setDuplicateTargetPath] = React.useState("");
  const [slimRemoveImages, setSlimRemoveImages] = React.useState(true);
  const [slimKeepLatestCompacted, setSlimKeepLatestCompacted] = React.useState(true);
  const [migrateTargetPath, setMigrateTargetPath] = React.useState("");
  const [renameSourcePath, setRenameSourcePath] = React.useState("");
  const [renameTargetPath, setRenameTargetPath] = React.useState("");
  const [renameFolder, setRenameFolder] = React.useState(true);
  const [logThread, setLogThread] = React.useState<ThreadRecord | null>(null);
  const [threadLogs, setThreadLogs] = React.useState<ThreadLogs | null>(null);
  const [logLoading, setLogLoading] = React.useState(false);
  const [logError, setLogError] = React.useState("");
  const [logKind, setLogKind] = React.useState("all");
  const [logSource, setLogSource] = React.useState("app");
  const [logSearch, setLogSearch] = React.useState("");
  const [logOffset, setLogOffset] = React.useState(0);
  const logLimit = 80;
  const logRequestSequenceRef = React.useRef(0);
  const [promptThread, setPromptThread] = React.useState<ThreadRecord | null>(null);
  const [threadPrompts, setThreadPrompts] = React.useState<ThreadPrompts | null>(null);
  const [promptsLoading, setPromptsLoading] = React.useState(false);
  const [promptsError, setPromptsError] = React.useState("");

  const [selectedResourcePath, setSelectedResourcePath] = React.useState("");
  const [resourceRead, setResourceRead] = React.useState<ResourceRead | null>(null);
  const [resourceDraft, setResourceDraft] = React.useState("");

  const [sourceCodexHome, setSourceCodexHome] = React.useState("");
  const [importThreadId, setImportThreadId] = React.useState("");
  const [importThreadTargetProject, setImportThreadTargetProject] = React.useState("");
  const [importProjectSourcePath, setImportProjectSourcePath] = React.useState("");
  const [importProjectTargetPath, setImportProjectTargetPath] = React.useState("");
  const [includeArchived, setIncludeArchived] = React.useState(false);
  const [preserveThreadIds, setPreserveThreadIds] = React.useState(false);
  const [copyRelativePath, setCopyRelativePath] = React.useState("AGENTS.md");
  const [copyTargetRelativePath, setCopyTargetRelativePath] = React.useState("");
  const [overwriteResource, setOverwriteResource] = React.useState(false);

  const refreshApiToken = React.useCallback(async (): Promise<AuthToken> => {
    if (isHostedConsole()) {
      window.open(defaultLocalApiBaseUrl, "_blank", "noopener,noreferrer");
      throw new Error(language === "en"
        ? "Write operations are available only in the local full mode opened at http://127.0.0.1:8765."
        : "写入操作只能在已打开的本机完整模式 http://127.0.0.1:8765 中执行。");
    }
    const tokenParams = new URLSearchParams({ codex_home: codexHome });
    const tokenPayload = await fetchJson<AuthToken>(`/api/auth/token?${tokenParams.toString()}`, {
      cache: "no-store"
    });
    setApiToken(tokenPayload.token);
    setApiTokenHeaderName(tokenPayload.headerName || "X-Codex-Manager-Token");
    return tokenPayload;
  }, [codexHome, language]);

  const checkLocalApiAccess = React.useCallback(async () => {
    if (isHostedConsole()) {
      setWriteWarnings([]);
      setCurrentVersions(null);
      setLocalApiConnection({
        status: "blocked",
        message: language === "en"
          ? "Open the local full mode explicitly to scan and manage this computer."
          : "请明确点击启动本机完整模式后，再扫描和管理这台电脑。"
      });
      return false;
    }
    setLocalApiConnection({ status: "checking", message: "" });
    setActionError("");
    try {
      const tokenPayload = await refreshApiToken();
      const params = new URLSearchParams({ codex_home: codexHome });
      const health = await fetchJson<HealthPayload>(`/api/health?${params.toString()}`, {
        headers: { [tokenPayload.headerName || "X-Codex-Manager-Token"]: tokenPayload.token }
      });
      setWriteWarnings(health.writeWarnings || []);
      setCurrentVersions(health.currentVersions || null);
      setLocalApiConnection({ status: "connected", message: "" });
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setWriteWarnings([]);
      setCurrentVersions(null);
      setLocalApiConnection({ status: "blocked", message: isLocalApiAccessError(message) ? localApiAccessMessage(language) : message });
      return false;
    }
  }, [codexHome, language, refreshApiToken]);

  React.useEffect(() => {
    void checkLocalApiAccess();
  }, [checkLocalApiAccess]);

  const refreshHealth = React.useCallback(async () => {
    try {
      const params = new URLSearchParams({ codex_home: codexHome });
      const health = await fetchAuthorizedJson<HealthPayload>(`/api/health?${params.toString()}`);
      setWriteWarnings(health.writeWarnings || []);
      setCurrentVersions(health.currentVersions || null);
      if (localApiConnection.status !== "connected") {
        setLocalApiConnection({ status: "connected", message: "" });
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setWriteWarnings([]);
      setCurrentVersions(null);
      setLocalApiConnection({ status: "blocked", message: isLocalApiAccessError(message) ? localApiAccessMessage(language) : message });
    }
  }, [codexHome, language, localApiConnection.status]);

  React.useEffect(() => {
    if (isLocalApiConnected) void refreshHealth();
  }, [isLocalApiConnected, refreshHealth]);

  const rescanBrowserWorkspace = React.useCallback(async (workspace: BrowserCodexWorkspace) => {
    const nextWorkspace = await scanBrowserCodexHome(workspace.directoryHandle, sidebarLimit, language);
    setBrowserWorkspace(nextWorkspace);
    return nextWorkspace;
  }, [language, sidebarLimit]);

  const startBrowserFolderScan = React.useCallback(async () => {
    setBrowserScanLoading(true);
    setActionError("");
    setNotice("");
    try {
      const directoryHandle = await pickBrowserCodexDirectory();
      const nextWorkspace = await scanBrowserCodexHome(directoryHandle, sidebarLimit, language);
      setBrowserWorkspace(nextWorkspace);
      setSelectedThreadId(null);
      setDetail(null);
      setResourceRead(null);
      setSelectedResourcePath("");
      setWriteWarnings([]);
      setCurrentVersions(null);
      setNotice(language === "en"
        ? `Loaded ${formatCount((nextWorkspace.snapshot as Snapshot).summary.totalThreads)} threads from the selected folder.`
        : `已从所选文件夹加载 ${formatCount((nextWorkspace.snapshot as Snapshot).summary.totalThreads)} 条线程。`);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBrowserScanLoading(false);
    }
  }, [language, sidebarLimit]);

  React.useEffect(() => {
    if (!notice) return;
    const timerId = window.setTimeout(() => setNotice(""), 6500);
    return () => window.clearTimeout(timerId);
  }, [notice]);

  const selectedThread = React.useMemo(() => {
    if (!selectedThreadId) return null;
    return activeSnapshot?.threads.find((thread) => thread.id === selectedThreadId) || null;
  }, [activeSnapshot, selectedThreadId]);

  React.useEffect(() => {
    if (!selectedThreadId) {
      setDetail(null);
      setDailyTokenLoading(false);
      return;
    }
    const threadId = selectedThreadId;
    let cancelled = false;
    async function loadDetail() {
      setDetailLoading(true);
      try {
        const nextDetail = browserWorkspace
          ? await readBrowserThreadDetail(browserWorkspace, threadId) as ThreadDetail
          : await fetchJson<ThreadDetail>(`/api/threads/${threadId}?${new URLSearchParams({ codex_home: codexHome, sidebar_limit: String(sidebarLimit), include_daily_tokens: "false" }).toString()}`);
        if (!cancelled) setDetail(nextDetail);
      } catch (error) {
        if (!cancelled) setActionError(error instanceof Error ? error.message : String(error));
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    }
    void loadDetail();
    return () => {
      cancelled = true;
    };
  }, [browserWorkspace, codexHome, selectedThreadId, sidebarLimit]);

  const loadDailyTokenUsage = React.useCallback(async (thread: ThreadRecord, force = false) => {
    if (!force && detail?.thread.id === thread.id && detail.dailyTokenUsage) return;
    setDailyTokenLoading(true);
    try {
      const usage = browserWorkspace
        ? await readBrowserThreadDailyTokenUsage(browserWorkspace, thread.id) as ThreadDailyTokenUsage
        : await fetchJson<ThreadDailyTokenUsage>(`/api/threads/${thread.id}/daily-tokens?${new URLSearchParams({ codex_home: codexHome, sidebar_limit: String(sidebarLimit) }).toString()}`);
      setDetail((currentDetail) => {
        if (!currentDetail || currentDetail.thread.id !== thread.id) return currentDetail;
        return { ...currentDetail, dailyTokenUsage: usage };
      });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setDailyTokenLoading(false);
    }
  }, [browserWorkspace, codexHome, detail?.dailyTokenUsage, detail?.thread.id, sidebarLimit]);

  React.useEffect(() => {
    if (selectedThread) {
      setDuplicateTargetPath(selectedThread.projectPath);
      setSlimRemoveImages(true);
      setSlimKeepLatestCompacted(true);
      setMigrateTargetPath(selectedThread.projectPath);
      setRenameSourcePath(selectedThread.projectPath);
      setRenameTargetPath(selectedThread.projectPath);
    }
  }, [selectedThread]);

  React.useEffect(() => {
    if (selectedResourcePath || !activeOverview) return;
    const preferred = activeOverview.resources.find((resource) => resource.exists && resource.relativePath === "AGENTS.md")
      || activeOverview.resources.find((resource) => resource.exists && resource.category === "memory")
      || activeOverview.resources.find((resource) => resource.exists);
    if (preferred) setSelectedResourcePath(preferred.relativePath);
  }, [activeOverview, selectedResourcePath]);

  const loadResource = React.useCallback(async () => {
    if (!selectedResourcePath) {
      setResourceRead(null);
      return;
    }
    try {
      const nextResource = browserWorkspace
        ? await readBrowserResource(browserWorkspace, selectedResourcePath) as ResourceRead
        : await fetchJson<ResourceRead>(`/api/resources/read?${new URLSearchParams({ codex_home: codexHome, relative_path: selectedResourcePath }).toString()}`);
      setResourceRead(nextResource);
      setResourceDraft(nextResource.content || "");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    }
  }, [browserWorkspace, codexHome, selectedResourcePath]);

  React.useEffect(() => {
    void loadResource();
  }, [loadResource]);

  async function refreshAll() {
    if (browserWorkspace) {
      await runAction(async () => {
        await rescanBrowserWorkspace(browserWorkspace);
        setNotice(language === "en" ? "Browser folder scan refreshed." : "已刷新浏览器文件夹扫描。");
      });
      return;
    }
    const connected = isLocalApiConnected || await checkLocalApiAccess();
    if (!connected) return;
    if (activeSection === "diagnostics") {
      await Promise.all([diagnosticsState.refresh({ force: true }), refreshHealth()]);
      return;
    }
    if (activeSection === "resources" || activeSection === "imports") {
      await Promise.all([overviewState.refresh(), refreshHealth()]);
      return;
    }
    if (activeSection === "api") {
      await Promise.all([capabilityState.refresh(), refreshHealth()]);
      return;
    }
    await Promise.all([snapshotState.refresh(), refreshHealth()]);
  }

  async function refreshDiagnostics() {
    if (browserWorkspace) {
      await refreshAll();
      return;
    }
    await diagnosticsState.refresh({ force: true });
  }

  async function runAction(action: () => Promise<void>) {
    setActionError("");
    setNotice("");
    try {
      await action();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    }
  }

  const actionParams = React.useMemo(
    () => new URLSearchParams({ codex_home: codexHome }),
    [codexHome]
  );

  const authorizedHeaders = React.useCallback((jsonBody = false, tokenOverride?: AuthToken): Record<string, string> => {
    const headers: Record<string, string> = {};
    if (jsonBody) headers["Content-Type"] = "application/json";
    const tokenValue = tokenOverride?.token || apiToken;
    const headerName = tokenOverride?.headerName || apiTokenHeaderName;
    if (tokenValue) headers[headerName] = tokenValue;
    return headers;
  }, [apiToken, apiTokenHeaderName]);

  function ensureApiToken() {
    return apiToken;
  }

  function requireLocalConnector(actionLabel: string): boolean {
    if (isHostedConsole()) {
      window.open(defaultLocalApiBaseUrl, "_blank", "noopener,noreferrer");
      setNotice(language === "en"
        ? `${actionLabel} is available only in local full mode. Opened http://127.0.0.1:8765 in a new window.`
        : `${actionLabel} 只能在本机完整模式执行，已在新窗口打开 http://127.0.0.1:8765。`);
      return false;
    }
    if (!browserWorkspace) return true;
    setNotice(language === "en"
      ? `${actionLabel} needs the local connector because it writes Codex Home state. Browser folder mode is read-only.`
      : `${actionLabel} 需要本机连接器，因为它会写入 Codex Home 状态；浏览器文件夹模式仅做只读扫描。`);
    return false;
  }

  async function fetchAuthorizedJson<T>(url: string, options: RequestInit = {}, jsonBody = false): Promise<T> {
    const tokenPayload = apiToken
      ? { token: apiToken, headerName: apiTokenHeaderName, expiresAtMs: null }
      : await refreshApiToken();
    const headers = new Headers(options.headers);
    Object.entries(authorizedHeaders(jsonBody, tokenPayload)).forEach(([key, value]) => headers.set(key, value));
    try {
      return await fetchJson<T>(url, { ...options, headers });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        const freshTokenPayload = await refreshApiToken();
        const retryHeaders = new Headers(options.headers);
        Object.entries(authorizedHeaders(jsonBody, freshTokenPayload)).forEach(([key, value]) => retryHeaders.set(key, value));
        return fetchJson<T>(url, { ...options, headers: retryHeaders });
      }
      throw error;
    }
  }

  function paramsWithPreview(preview: ImpactPreview | SlimPreview, acknowledgeCodexRunningRisk = false, createBackup = true): URLSearchParams {
    const params = new URLSearchParams(actionParams);
    if (preview.operationPreviewId) params.set("operationPreviewId", preview.operationPreviewId);
    if (preview.inputHash) params.set("inputHash", preview.inputHash);
    if (acknowledgeCodexRunningRisk) params.set("acknowledgeCodexRunningRisk", "true");
    params.set("createBackup", String(createBackup));
    return params;
  }

  async function confirmRunningCodexWrite(actionLabel: string): Promise<boolean> {
    if (!writeWarnings.length) return false;
    const confirmed = window.confirm(
      `${t("Codex Desktop 仍在运行。")}\n\n${localizeWarningText(t, writeWarnings[0])}\n\n${t("继续执行")} "${actionLabel}" ${t("可能被正在运行的 Codex 覆盖或抢占。确认继续？")}`
    );
    if (!confirmed) {
      throw new Error(t("已取消：Codex Desktop 正在运行，未确认写入风险。"));
    }
    return true;
  }

  async function previewThreadAction(thread: ThreadRecord, action: string, extraParams?: Record<string, string>): Promise<ImpactPreview> {
    const params = new URLSearchParams({
      codex_home: codexHome,
      sidebar_limit: String(sidebarLimit),
      action
    });
    Object.entries(extraParams || {}).forEach(([key, value]) => params.set(key, value));
    return fetchJson<ImpactPreview>(`/api/threads/${thread.id}/action-preview?${params.toString()}`);
  }

  const loadThreadLogs = React.useCallback(async () => {
    if (!logThread) {
      logRequestSequenceRef.current += 1;
      setThreadLogs(null);
      return;
    }
    const requestSequence = logRequestSequenceRef.current + 1;
    logRequestSequenceRef.current = requestSequence;
    setLogLoading(true);
    setLogError("");
    try {
      const nextLogs = browserWorkspace
        ? await readBrowserThreadLogs(browserWorkspace, logThread.id, logOffset, logLimit, logKind, logSearch) as ThreadLogs
        : await fetchJson<ThreadLogs>(`/api/threads/${logThread.id}/logs?${new URLSearchParams({
            codex_home: codexHome,
            offset: String(logOffset),
            limit: String(logLimit),
            kind: logKind,
            source: logSource,
            search: logSearch
          }).toString()}`);
      if (requestSequence !== logRequestSequenceRef.current) return;
      setThreadLogs(nextLogs);
    } catch (error) {
      if (requestSequence !== logRequestSequenceRef.current) return;
      setLogError(error instanceof Error ? error.message : String(error));
    } finally {
      if (requestSequence === logRequestSequenceRef.current) {
        setLogLoading(false);
      }
    }
  }, [browserWorkspace, codexHome, logKind, logOffset, logSearch, logSource, logThread]);

  React.useEffect(() => {
    void loadThreadLogs();
  }, [loadThreadLogs]);

  function openThreadLogs(thread: ThreadRecord) {
    setLogThread(thread);
    setLogKind("all");
    setLogSource(browserWorkspace ? "rollout" : "app");
    setLogSearch("");
    setLogOffset(0);
  }

  function selectThread(thread: ThreadRecord) {
    setSelectedThreadId(thread.id);
  }

  const clearThreadSelection = React.useCallback(() => {
    setSelectedThreadId(null);
  }, []);

  async function handleShowThread(thread: ThreadRecord) {
    if (!requireLocalConnector(language === "en" ? "Showing a thread" : "显示线程")) return;
    await runAction(async () => {
      ensureApiToken();
      const preview = await previewThreadAction(thread, "show");
      const confirmed = window.confirm(`${t("将线程恢复到 Codex 侧边栏？")}\n\n${thread.title}${warningsText(t, preview.warnings)}\n\n${backupModeText(t, createBackups)}`);
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("显示线程"));
      await fetchAuthorizedJson(`/api/threads/${thread.id}/show?${paramsWithPreview(preview, riskAcknowledgement, createBackups).toString()}`, { method: "POST" });
      setNotice(t("已恢复到侧边栏，刷新或重启 Codex Desktop 后应可见。"));
      await snapshotState.refresh();
    });
  }

  async function handleRepairThread(thread: ThreadRecord) {
    if (!requireLocalConnector(language === "en" ? "Repairing visibility" : "修复可见性")) return;
    await runAction(async () => {
      ensureApiToken();
      const preview = await previewThreadAction(thread, "repair_user_event");
      const confirmed = window.confirm(`${t("修复并显示线程？")}\n\n${thread.title}${warningsText(t, preview.warnings)}\n\n${t("会把 has_user_event 修复为可见状态，并恢复到 Codex 侧边栏。")}\n\n${backupModeText(t, createBackups)}`);
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("修复线程"));
      await fetchAuthorizedJson(`/api/threads/${thread.id}/repair-user-event?${paramsWithPreview(preview, riskAcknowledgement, createBackups).toString()}`, { method: "POST" });
      setNotice(t("已修复线程元数据，并恢复到 Codex 侧边栏。"));
      await snapshotState.refresh();
    });
  }

  async function handleHideThread(thread: ThreadRecord) {
    if (!requireLocalConnector(language === "en" ? "Hiding a thread" : "隐藏线程")) return;
    await runAction(async () => {
      ensureApiToken();
      const preview = await previewThreadAction(thread, "hide");
      const confirmed = window.confirm(`${t("隐藏这个可见线程？")}\n\n${thread.title}${warningsText(t, preview.warnings)}\n\n${t("会从 Codex 显示索引中移除；不会归档、不会删除 JSONL，也不会删除项目绑定或 heartbeat 权限。")}\n\n${backupModeText(t, createBackups)}`);
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("隐藏线程"));
      await fetchAuthorizedJson(`/api/threads/${thread.id}/hide?${paramsWithPreview(preview, riskAcknowledgement, createBackups).toString()}`, { method: "POST" });
      setNotice(t("线程已隐藏；刷新或重启 Codex Desktop 后侧边栏应不再显示。"));
      await snapshotState.refresh();
    });
  }

  async function handleBackup(thread: ThreadRecord) {
    if (!requireLocalConnector(language === "en" ? "Creating a backup" : "创建备份")) return;
    await runAction(async () => {
      ensureApiToken();
      await fetchAuthorizedJson(`/api/threads/${thread.id}/backup?${actionParams.toString()}`, { method: "POST" });
      setNotice(t("已创建线程状态备份。"));
      if (selectedThreadId === thread.id) {
        const params = new URLSearchParams({ codex_home: codexHome, sidebar_limit: String(sidebarLimit), include_daily_tokens: "false" });
        const refreshedDetail = await fetchJson<ThreadDetail>(`/api/threads/${thread.id}?${params.toString()}`);
        setDetail((currentDetail) => currentDetail?.thread.id === thread.id && currentDetail.dailyTokenUsage
          ? { ...refreshedDetail, dailyTokenUsage: currentDetail.dailyTokenUsage }
          : refreshedDetail);
      }
    });
  }

  async function handleExportPrompts(thread: ThreadRecord) {
    await runAction(async () => {
      if (browserWorkspace) {
        const result = await exportBrowserThreadPrompts(browserWorkspace, thread.id);
        setNotice(language === "en"
          ? `Exported ${formatCount(result.promptCount)} prompts: ${result.filename}`
          : `已导出 ${formatCount(result.promptCount)} 条 prompt：${result.filename}`);
        return;
      }
      ensureApiToken();
      const params = new URLSearchParams({ codex_home: codexHome, format: "markdown" });
      const result = await fetchAuthorizedJson<{ outputPath: string; promptCount: number }>(`/api/threads/${thread.id}/export-prompts?${params.toString()}`);
      setNotice(`${t("已导出")} ${result.promptCount} ${t("条 prompt：")}${result.outputPath}`);
    });
  }

  async function handleViewPrompts(thread: ThreadRecord) {
    setPromptThread(thread);
    setThreadPrompts(null);
    setPromptsError("");
    setPromptsLoading(true);
    try {
      const result = browserWorkspace
        ? await readBrowserThreadPrompts(browserWorkspace, thread.id) as ThreadPrompts
        : await fetchThreadPromptsFromLocalApi(thread, codexHome, language);
      setThreadPrompts(result);
    } catch (error) {
      setPromptsError(error instanceof Error ? error.message : String(error));
    } finally {
      setPromptsLoading(false);
    }
  }

  async function handleDuplicate(thread: ThreadRecord) {
    if (!requireLocalConnector(language === "en" ? "Duplicating a thread" : "复制线程")) return;
    const targetPath = duplicateTargetPath.trim();
    if (!targetPath) {
      setNotice(t("请先填写复制目标项目路径。"));
      return;
    }
    await runAction(async () => {
      ensureApiToken();
      const preview = await previewThreadAction(thread, "duplicate", { targetProjectPath: targetPath });
      const confirmed = window.confirm(`${t("复制线程到目标项目？")}\n\n${thread.title}\n\n${t("目标项目：")}${targetPath}${warningsText(t, preview.warnings)}\n\n${t("会复制 JSONL、新增 SQLite 线程记录，并写入目标项目 hint。")}\n\n${backupModeText(t, createBackups)}`);
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("复制线程"));
      const result = await fetchAuthorizedJson<{ newThreadId: string; targetProjectPath: string }>(`/api/threads/${thread.id}/duplicate?${actionParams.toString()}`, {
        method: "POST",
        body: JSON.stringify(withWriteAcknowledgement(withPreviewTicket({ targetProjectPath: targetPath }, preview), riskAcknowledgement, createBackups))
      }, true);
      setNotice(`${t("已复制线程到")} ${result.targetProjectPath}: ${result.newThreadId}`);
      await snapshotState.refresh();
      setSelectedThreadId(result.newThreadId);
    });
  }

  async function handleArchive(thread: ThreadRecord) {
    if (!requireLocalConnector(language === "en" ? "Archiving a thread" : "归档线程")) return;
    await runAction(async () => {
      ensureApiToken();
      const preview = await previewThreadAction(thread, "archive");
      const confirmed = window.confirm(`${t("安全删除/归档线程？")}\n\n${thread.title}${warningsText(t, preview.warnings)}\n\n${t("不会永久删除 JSONL。")}\n\n${backupModeText(t, createBackups)}`);
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("归档线程"));
      await fetchAuthorizedJson(`/api/threads/${thread.id}/archive?${paramsWithPreview(preview, riskAcknowledgement, createBackups).toString()}`, { method: "POST" });
      setNotice(t("已归档线程，并从 Codex 侧边栏索引移除；刷新或重启 Codex Desktop 后应不再显示。"));
      await snapshotState.refresh();
    });
  }

  async function handleSlim(thread: ThreadRecord) {
    if (!requireLocalConnector(language === "en" ? "Slimming a thread" : "线程瘦身")) return;
    if (!slimRemoveImages && !slimKeepLatestCompacted) {
      setNotice(t("请至少选择一个线程瘦身范围。"));
      return;
    }
    await runAction(async () => {
      ensureApiToken();
      const previewParams = new URLSearchParams({
        codex_home: codexHome,
        removeImages: String(slimRemoveImages),
        keepLatestCompacted: String(slimKeepLatestCompacted)
      });
      const preview = await fetchJson<SlimPreview>(`/api/threads/${thread.id}/slim/preview?${previewParams.toString()}`);
      const scopeLines = [
        slimRemoveImages ? t("移除嵌入图片和 data:image 内容") : "",
        slimKeepLatestCompacted ? t("只保留最新 compacted checkpoint") : ""
      ].filter(Boolean).join("\n");
      const confirmed = window.confirm(
        `${t("按选定范围给线程瘦身？")}\n\n${thread.title}\n\n${t("瘦身范围：")}\n${scopeLines}\n\n${t("影响预览：")}\n${slimPreviewText(preview, t)}${warningsText(t, preview.warnings)}\n\n${backupModeText(t, createBackups)}`
      );
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("线程瘦身"));
      const result = await fetchAuthorizedJson<{ savedBytes: number }>(`/api/threads/${thread.id}/slim?${actionParams.toString()}`, {
        method: "POST",
        body: JSON.stringify(withWriteAcknowledgement(withPreviewTicket({
          removeImages: slimRemoveImages,
          keepLatestCompacted: slimKeepLatestCompacted
        }, preview), riskAcknowledgement, createBackups))
      }, true);
      setNotice(`${t("瘦身完成，节省")} ${formatBytes(result.savedBytes)}.`);
      await snapshotState.refresh();
    });
  }

  async function handleMigrate(thread: ThreadRecord) {
    if (!requireLocalConnector(language === "en" ? "Migrating a thread" : "迁移线程")) return;
    const targetPath = migrateTargetPath.trim();
    if (!targetPath) {
      setNotice(t("请先填写目标项目路径。"));
      return;
    }
    await runAction(async () => {
      ensureApiToken();
      const preview = await fetchJson<ImpactPreview>(`/api/threads/${thread.id}/migrate/preview?${new URLSearchParams({ codex_home: codexHome, sidebar_limit: String(sidebarLimit) }).toString()}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ targetProjectPath: targetPath })
      });
      const confirmed = window.confirm(`${t("迁移线程到目标项目？")}\n\n${thread.title}\n\n${targetPath}\n\n${t("迁移前必须关闭 Codex Desktop 和 Codex CLI；运行中迁移会被后端拒绝。")}${warningsText(t, preview.warnings)}\n\n${backupModeText(t, createBackups)}`);
      if (!confirmed) return;
      await fetchAuthorizedJson(`/api/threads/${thread.id}/migrate?${actionParams.toString()}`, {
        method: "POST",
        body: JSON.stringify(withWriteAcknowledgement(withPreviewTicket({ targetProjectPath: targetPath }, preview), false, createBackups))
      }, true);
      setNotice(t("线程迁移完成。"));
      await refreshAll();
    });
  }

  async function handleRenameProject() {
    if (!requireLocalConnector(language === "en" ? "Renaming a project" : "重命名项目")) return;
    const sourcePath = renameSourcePath.trim();
    const targetPath = renameTargetPath.trim();
    if (!sourcePath || !targetPath) {
      setNotice(t("请填写原项目路径和目标项目路径。"));
      return;
    }
    await runAction(async () => {
      ensureApiToken();
      const params = new URLSearchParams({ codex_home: codexHome });
      const preview = await fetchJson<ImpactPreview>(`/api/projects/rename/preview?${params.toString()}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sourceProjectPath: sourcePath, targetProjectPath: targetPath, renameFolder })
      });
      if (preview.blockedByRunningCodex) {
        throw new Error(`${t("项目重命名要求先关闭 Codex Desktop 和 Codex CLI，否则侧边栏缓存可能会把旧项目名写回。")}${warningsText(t, preview.warnings)}`);
      }
      const confirmed = window.confirm(
        `${t("更改项目及文件夹名？")}\n\n${sourcePath}\n->\n${targetPath}\n\n${t("影响预览：")}\n${renamePreviewText(preview, t)}${warningsText(t, preview.warnings)}\n\n${t("会更新匹配线程、Codex global state、config，并按勾选项重命名本地文件夹。")}\n\n${backupModeText(t, createBackups)}`
      );
      if (!confirmed) return;
      const result = await fetchAuthorizedJson<{ updatedThreads: number }>(`/api/projects/rename?${params.toString()}`, {
        method: "POST",
        body: JSON.stringify(withWriteAcknowledgement(withPreviewTicket({ sourceProjectPath: sourcePath, targetProjectPath: targetPath, renameFolder }, preview), false, createBackups))
      }, true);
      setNotice(`${t("项目重命名完成，更新")} ${result.updatedThreads} ${t("条线程。")}`);
      await refreshAll();
    });
  }

  async function handleRestore(backup: BackupRecord) {
    if (!requireLocalConnector(language === "en" ? "Restoring a backup" : "回滚备份")) return;
    await runAction(async () => {
      ensureApiToken();
      const preview = await fetchJson<ImpactPreview>(`/api/backups/${backup.backupId}/restore/preview`);
      const confirmed = window.confirm(`${t("回滚备份？")}\n\n${backup.backupId}${warningsText(t, preview.warnings)}\n\n${createBackups ? t("回滚前会再创建一个 pre_restore 备份。") : t("自动备份已关闭：回滚前不会创建 pre_restore 备份。")}`);
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("回滚备份"));
      await fetchAuthorizedJson(`/api/backups/${backup.backupId}/restore?${paramsWithPreview(preview, riskAcknowledgement, createBackups).toString()}`, { method: "POST" });
      setNotice(t("已回滚备份。"));
      await refreshAll();
    });
  }

  async function handleBackupResource() {
    if (!resourceRead) return;
    if (!requireLocalConnector(language === "en" ? "Backing up a resource" : "备份资源")) return;
    await runAction(async () => {
      ensureApiToken();
      const params = new URLSearchParams({ codex_home: codexHome });
      await fetchAuthorizedJson(`/api/resources/backup?${params.toString()}`, {
        method: "POST",
        body: JSON.stringify({ relativePath: resourceRead.metadata.relativePath })
      }, true);
      setNotice(`${t("已备份资源：")}${resourceRead.metadata.relativePath}`);
      await overviewState.refresh();
    });
  }

  async function handleSaveResource() {
    if (!resourceRead || resourceRead.content === null) return;
    if (!requireLocalConnector(language === "en" ? "Saving a resource" : "保存资源")) return;
    await runAction(async () => {
      ensureApiToken();
      const params = new URLSearchParams({ codex_home: codexHome });
      const preview = await fetchJson<ImpactPreview>(`/api/resources/write/preview?${params.toString()}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ relativePath: resourceRead.metadata.relativePath, content: resourceDraft, createParentDirectories: true })
      });
      const confirmed = window.confirm(`${t("保存文本资源？")}\n\n${resourceRead.metadata.relativePath}${warningsText(t, preview.warnings)}\n\n${backupModeText(t, createBackups)}`);
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("保存资源"));
      await fetchAuthorizedJson(`/api/resources/write?${params.toString()}`, {
        method: "POST",
        body: JSON.stringify(withWriteAcknowledgement(withPreviewTicket({ relativePath: resourceRead.metadata.relativePath, content: resourceDraft, createParentDirectories: true }, preview), riskAcknowledgement, createBackups))
      }, true);
      setNotice(`${t("已保存资源：")}${resourceRead.metadata.relativePath}`);
      await Promise.all([overviewState.refresh(), loadResource()]);
    });
  }

  async function handleImportThread() {
    if (!requireLocalConnector(language === "en" ? "Importing a thread" : "导入线程")) return;
    if (!sourceCodexHome.trim() || !importThreadId.trim()) {
      setNotice(t("请填写来源 CODEX_HOME 和来源线程 ID。"));
      return;
    }
    await runAction(async () => {
      ensureApiToken();
      const params = new URLSearchParams({ codex_home: codexHome });
      const preview = await fetchJson<ImpactPreview>(`/api/import/thread/preview?${params.toString()}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sourceCodexHome,
          sourceThreadId: importThreadId,
          targetProjectPath: importThreadTargetProject.trim() || null,
          preserveThreadId: preserveThreadIds
        })
      });
      const confirmed = window.confirm(
        `${t("导入线程？")}\n\n${importThreadId}\n\n${t("影响预览：")}\n${importThreadPreviewText(preview, t)}${warningsText(t, preview.warnings)}\n\n${backupModeText(t, createBackups)}`
      );
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("导入线程"));
      const result = await fetchAuthorizedJson<{ importedThreads: Array<{ newThreadId: string }> }>(`/api/import/thread?${params.toString()}`, {
        method: "POST",
        body: JSON.stringify(withWriteAcknowledgement(withPreviewTicket({
          sourceCodexHome,
          sourceThreadId: importThreadId,
          targetProjectPath: importThreadTargetProject.trim() || null,
          preserveThreadId: preserveThreadIds
        }, preview), riskAcknowledgement, createBackups))
      }, true);
      setNotice(`${t("已导入线程：")}${result.importedThreads[0]?.newThreadId || ""}`);
      await snapshotState.refresh();
    });
  }

  async function handleImportProject() {
    if (!requireLocalConnector(language === "en" ? "Importing a project" : "导入项目")) return;
    if (!sourceCodexHome.trim() || !importProjectSourcePath.trim()) {
      setNotice(t("请填写来源 CODEX_HOME 和来源项目路径。"));
      return;
    }
    await runAction(async () => {
      ensureApiToken();
      const params = new URLSearchParams({ codex_home: codexHome });
      const preview = await fetchJson<ImpactPreview>(`/api/import/project/preview?${params.toString()}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sourceCodexHome,
          sourceProjectPath: importProjectSourcePath,
          targetProjectPath: importProjectTargetPath.trim() || null,
          includeArchived,
          preserveThreadIds
        })
      });
      const confirmed = window.confirm(
        `${t("导入项目线程？")}\n\n${importProjectSourcePath}\n\n${t("影响预览：")}\n${importPreviewText(preview, t)}${warningsText(t, preview.warnings)}\n\n${backupModeText(t, createBackups)}`
      );
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("导入项目线程"));
      const result = await fetchAuthorizedJson<{ importedThreads: Array<{ newThreadId: string }> }>(`/api/import/project?${params.toString()}`, {
        method: "POST",
        body: JSON.stringify(withWriteAcknowledgement(withPreviewTicket({
          sourceCodexHome,
          sourceProjectPath: importProjectSourcePath,
          targetProjectPath: importProjectTargetPath.trim() || null,
          includeArchived,
          preserveThreadIds
        }, preview), riskAcknowledgement, createBackups))
      }, true);
      setNotice(`${t("已导入项目线程：")}${result.importedThreads.length} ${t("条。")}`);
      await snapshotState.refresh();
    });
  }

  async function handleCopyResource() {
    if (!requireLocalConnector(language === "en" ? "Copying a resource" : "复制资源")) return;
    if (!sourceCodexHome.trim() || !copyRelativePath.trim()) {
      setNotice(t("请填写来源 CODEX_HOME 和来源相对路径。"));
      return;
    }
    await runAction(async () => {
      ensureApiToken();
      const params = new URLSearchParams({ codex_home: codexHome });
      const preview = await fetchJson<ImpactPreview>(`/api/resources/copy-from-home/preview?${params.toString()}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sourceCodexHome,
          relativePath: copyRelativePath,
          targetRelativePath: copyTargetRelativePath.trim() || null,
          overwrite: overwriteResource
        })
      });
      const confirmed = window.confirm(
        `${t("复制 Codex 资源？")}\n\n${copyRelativePath}\n\n${t("影响预览：")}\n${resourceCopyPreviewText(preview, t)}${warningsText(t, preview.warnings)}\n\n${backupModeText(t, createBackups)}`
      );
      if (!confirmed) return;
      const riskAcknowledgement = await confirmRunningCodexWrite(t("复制 Codex 资源"));
      await fetchAuthorizedJson(`/api/resources/copy-from-home?${params.toString()}`, {
        method: "POST",
        body: JSON.stringify(withWriteAcknowledgement(withPreviewTicket({
          sourceCodexHome,
          relativePath: copyRelativePath,
          targetRelativePath: copyTargetRelativePath.trim() || null,
          overwrite: overwriteResource
        }, preview), riskAcknowledgement, createBackups))
      }, true);
      setNotice(`${t("已复制资源：")}${copyRelativePath}`);
      await overviewState.refresh();
    });
  }

  const combinedError = isBrowserMode
    ? actionError
    : snapshotState.error
      || overviewState.error
      || capabilityState.error
      || (activeSection === "diagnostics" ? diagnosticsState.error : null)
      || actionError;
  const localApiAccessBlocked = !isBrowserMode && isLocalApiAccessError(combinedError);
  const displayedError = localApiAccessBlocked ? localApiAccessMessage(language) : combinedError;

  return (
    <I18nContext.Provider value={i18nValue}>
    <main className="app-shell">
      <aside className="app-sidebar">
        <div className="brand-block">
          <div className="brand-mark"><Database size={22} /></div>
          <div>
            <h1>Codex Home Manager</h1>
            <p>{t("线程、资源、导入和 agent API 管理台")}</p>
          </div>
        </div>
        <nav className="side-nav">
          {navigationItems(t).map(({ value, label, icon: Icon }) => (
            <button key={value} className={activeSection === value ? "active" : ""} onClick={() => setActiveSection(value)}>
              <Icon size={17} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-status">
          <span>{t("当前 Home")}</span>
          <code>{codexHomeDisplayValue(activeCodexHomeLabel, language)}</code>
          <span>{t("当前 Codex")}</span>
          <code title={codexVersionTooltip(currentVersions, language)}>{codexVersionSummary(currentVersions, language)}</code>
          <span>API</span>
          <code>{isBrowserMode ? "browser-folder" : apiDisplayBaseUrl()}</code>
        </div>
      </aside>

      <section className="app-main">
        <header className="command-bar">
          <div className="path-control">
            <label>CODEX_HOME</label>
            <input
              aria-label="CODEX_HOME"
              value={codexHome}
              onChange={(event) => setCodexHome(event.target.value)}
              placeholder={language === "en" ? "Leave blank to use local API default" : "留空使用本机 API 默认路径"}
            />
          </div>
          <a className="top-link" href={publicRepositoryUrl} target="_blank" rel="noreferrer" title="GitHub">
            <ExternalLink size={15} />
            <span>GitHub</span>
          </a>
          <button
            className="language-toggle"
            onClick={() => setLanguage((value) => value === "zh" ? "en" : "zh")}
            title={language === "zh" ? t("切换到英文") : t("切换到中文")}
            type="button"
          >
            <Languages size={16} />
            <span>{language === "zh" ? "EN" : "中文"}</span>
          </button>
          <label className="backup-toggle" title={createBackups ? t("写入操作会先创建可回滚备份") : t("写入操作不会创建自动备份")}>
            <input type="checkbox" checked={createBackups} onChange={(event) => setCreateBackups(event.target.checked)} />
            <span>{t("自动备份")}</span>
          </label>
          <button className="top-action" onClick={() => void refreshAll()}>
            <RefreshCcw size={16} />
            {t("刷新")}
          </button>
        </header>

        {writeWarnings.length ? (
          <div className="health-warning-strip" title={localizeWarningText(t, writeWarnings[0])}>
            <span>{t("Codex 正在运行，写入类操作会二次确认")}</span>
            <code>{writeWarnings.length} {t("条风险提示")}</code>
          </div>
        ) : null}

        {isBrowserMode ? (
          <div className="browser-mode-strip">
            <ShieldCheck size={15} />
            <span>{language === "en" ? "Browser folder mode: read-only local scan, no connector required." : "浏览器文件夹模式：只读本机扫描，无需下载安装连接器。"}</span>
            <button type="button" onClick={() => void startBrowserFolderScan()}>
              <FolderInput size={14} />
              {language === "en" ? "Choose another folder" : "选择其他文件夹"}
            </button>
            <button type="button" onClick={() => setBrowserWorkspace(null)}>
              <ServerCog size={14} />
              {language === "en" ? "Use connector" : "使用连接器"}
            </button>
          </div>
        ) : null}

        <div className="toast-stack" aria-live="polite" aria-atomic="true">
          {notice ? <div className="notice" role="status">{notice}</div> : null}
          {displayedError ? (
            <div className={`error-banner ${localApiAccessBlocked ? "with-action" : ""}`} role="alert">
              <span>{displayedError}</span>
              {localApiAccessBlocked ? (
                <button type="button" onClick={() => void refreshAll()}>
                  <RefreshCcw size={15} />
                  {language === "en" ? "Retry access" : "重试授权"}
                </button>
              ) : null}
            </div>
          ) : null}
        </div>

        {!isLocalApiConnected && !isBrowserMode && activeSection !== "api" ? (
          <LocalApiConnectionGate
            status={localApiConnection.status}
            message={localApiConnection.message}
            codexHome={codexHome}
            apiBaseUrl={apiDisplayBaseUrl()}
            language={language}
            onRetry={() => void refreshAll()}
            onBrowserScan={() => void startBrowserFolderScan()}
            browserScanLoading={browserScanLoading}
            browserScanSupported={supportsBrowserFolderMode()}
          />
        ) : (
          <>
            {activeSection === "threads" ? (
              <ThreadsModule
                snapshot={activeSnapshot}
                isLoading={isBrowserMode ? browserScanLoading : snapshotState.isLoading}
                selectedThreadId={selectedThreadId}
                detail={detail}
                detailLoading={detailLoading}
                dailyTokenLoading={dailyTokenLoading}
                onSelectThread={selectThread}
                onLoadDailyTokens={loadDailyTokenUsage}
                onShowThread={(thread) => void handleShowThread(thread)}
                onRepairThread={(thread) => void handleRepairThread(thread)}
                onClearSelection={clearThreadSelection}
                onBackup={(thread) => void handleBackup(thread)}
                onRestore={(backup) => void handleRestore(backup)}
                onExportPrompts={(thread) => void handleExportPrompts(thread)}
                onViewPrompts={(thread) => void handleViewPrompts(thread)}
                onViewLogs={(thread) => openThreadLogs(thread)}
                onDuplicate={(thread) => void handleDuplicate(thread)}
                onHideThread={(thread) => void handleHideThread(thread)}
                onArchive={(thread) => void handleArchive(thread)}
                onSlim={(thread) => void handleSlim(thread)}
                onMigrate={(thread) => void handleMigrate(thread)}
                duplicateTargetPath={duplicateTargetPath}
                setDuplicateTargetPath={setDuplicateTargetPath}
                slimRemoveImages={slimRemoveImages}
                setSlimRemoveImages={setSlimRemoveImages}
                slimKeepLatestCompacted={slimKeepLatestCompacted}
                setSlimKeepLatestCompacted={setSlimKeepLatestCompacted}
                migrateTargetPath={migrateTargetPath}
                setMigrateTargetPath={setMigrateTargetPath}
                renameSourcePath={renameSourcePath}
                renameTargetPath={renameTargetPath}
                renameFolder={renameFolder}
                setRenameSourcePath={setRenameSourcePath}
                setRenameTargetPath={setRenameTargetPath}
                setRenameFolder={setRenameFolder}
                onRenameProject={() => void handleRenameProject()}
              />
            ) : null}

            {activeSection === "diagnostics" ? (
              <DiagnosticsModule
                report={activeDiagnostics}
                isLoading={isBrowserMode ? browserScanLoading : diagnosticsState.isLoading}
                isCached={!isBrowserMode && diagnosticsState.isCached}
                onRefresh={() => void refreshDiagnostics()}
              />
            ) : null}

            {activeSection === "resources" ? (
              <ResourcesModule
                codexHome={activeCodexHomeLabel}
                overview={activeOverview}
                isLoading={isBrowserMode ? browserScanLoading : overviewState.isLoading}
                selectedResourcePath={selectedResourcePath}
                setSelectedResourcePath={setSelectedResourcePath}
                resourceRead={resourceRead}
                resourceDraft={resourceDraft}
                setResourceDraft={setResourceDraft}
                onBackupResource={() => void handleBackupResource()}
                onSaveResource={() => void handleSaveResource()}
                onReloadResource={() => void loadResource()}
              />
            ) : null}

            {activeSection === "imports" ? (
              <ImportsModule
                codexHome={activeCodexHomeLabel}
                sourceCodexHome={sourceCodexHome}
                setSourceCodexHome={setSourceCodexHome}
                importThreadId={importThreadId}
                setImportThreadId={setImportThreadId}
                importThreadTargetProject={importThreadTargetProject}
                setImportThreadTargetProject={setImportThreadTargetProject}
                importProjectSourcePath={importProjectSourcePath}
                setImportProjectSourcePath={setImportProjectSourcePath}
                importProjectTargetPath={importProjectTargetPath}
                setImportProjectTargetPath={setImportProjectTargetPath}
                includeArchived={includeArchived}
                setIncludeArchived={setIncludeArchived}
                preserveThreadIds={preserveThreadIds}
                setPreserveThreadIds={setPreserveThreadIds}
                copyRelativePath={copyRelativePath}
                setCopyRelativePath={setCopyRelativePath}
                copyTargetRelativePath={copyTargetRelativePath}
                setCopyTargetRelativePath={setCopyTargetRelativePath}
                overwriteResource={overwriteResource}
                setOverwriteResource={setOverwriteResource}
                onImportThread={() => void handleImportThread()}
                onImportProject={() => void handleImportProject()}
                onCopyResource={() => void handleCopyResource()}
              />
            ) : null}

            {activeSection === "api" ? <ApiModule capabilities={capabilityState.capabilities} /> : null}
          </>
        )}

        <ThreadLogModal
          thread={logThread}
          logs={threadLogs}
          isLoading={logLoading}
          error={logError}
          kind={logKind}
          source={logSource}
          search={logSearch}
          offset={logOffset}
          limit={logLimit}
          onKindChange={(value) => { setLogKind(value); setLogOffset(0); }}
          onSourceChange={(value) => { setLogSource(value); setLogOffset(0); }}
          onSearchChange={(value) => { setLogSearch(value); setLogOffset(0); }}
          onPreviousPage={() => setLogOffset((value) => Math.max(0, value - logLimit))}
          onNextPage={() => setLogOffset((value) => value + logLimit)}
          onRefresh={() => void loadThreadLogs()}
          onClose={() => { setLogThread(null); setThreadLogs(null); setLogError(""); }}
        />
        <ThreadPromptModal
          thread={promptThread}
          prompts={threadPrompts}
          isLoading={promptsLoading}
          error={promptsError}
          onClose={() => { setPromptThread(null); setThreadPrompts(null); setPromptsError(""); }}
        />
      </section>
    </main>
    </I18nContext.Provider>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
