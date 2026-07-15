import initSqlJs, { type SqlJsDatabase } from "sql.js";
import sqlWasmUrl from "sql.js/dist/sql-wasm.wasm?url";

type BrowserLanguage = "zh" | "en";

type FileSystemPermissionMode = "read" | "readwrite";

type FileSystemPermissionDescriptor = {
  mode?: FileSystemPermissionMode;
};

type BrowserFileHandle = {
  kind: "file";
  name: string;
  getFile(): Promise<File>;
  queryPermission?(descriptor?: FileSystemPermissionDescriptor): Promise<PermissionState>;
  requestPermission?(descriptor?: FileSystemPermissionDescriptor): Promise<PermissionState>;
};

type BrowserDirectoryHandle = {
  kind: "directory";
  name: string;
  entries(): AsyncIterable<[string, BrowserDirectoryHandle | BrowserFileHandle]>;
  getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<BrowserDirectoryHandle>;
  getFileHandle(name: string, options?: { create?: boolean }): Promise<BrowserFileHandle>;
  queryPermission?(descriptor?: FileSystemPermissionDescriptor): Promise<PermissionState>;
  requestPermission?(descriptor?: FileSystemPermissionDescriptor): Promise<PermissionState>;
};

declare global {
  interface Window {
    showDirectoryPicker?: (options?: { mode?: FileSystemPermissionMode; id?: string; startIn?: string }) => Promise<BrowserDirectoryHandle>;
  }
}

type BrowserThreadFile = {
  relativePath: string;
  handle: BrowserFileHandle;
  archivedStore: boolean;
};

type BrowserProjectKind = "workspace_project" | "conversation" | "other";

type BrowserSqlThreadRow = Record<string, unknown> & {
  id: string;
};

type BrowserGlobalState = Record<string, unknown>;

type BrowserSessionIndexRecord = {
  threadId: string;
  sidebarTitle: string;
  sessionIndexUpdatedAt: string;
  sessionIndexLine: number;
};

type BrowserThreadSnapshot = {
  id: string;
  title: string;
  projectPath: string;
  projectLabel: string;
  rolloutPath: string;
  visibility: string;
  archived: boolean;
  hasUserEvent: boolean;
  codexVisible: boolean;
  recentRank: number | null;
  updatedAtMs: number | null;
  fileSizeBytes: number;
  [key: string]: unknown;
};

let sqlJsPromise: ReturnType<typeof initSqlJs> | null = null;

function loadSqlJs() {
  if (!sqlJsPromise) {
    sqlJsPromise = initSqlJs({ locateFile: () => sqlWasmUrl });
  }
  return sqlJsPromise;
}

function numberField(record: Record<string, unknown>, key: string): number {
  const value = Number(record[key]);
  return Number.isFinite(value) ? value : 0;
}

function applyThreadChildRollups(threads: BrowserThreadSnapshot[]) {
  const threadsById = new Map(threads.map((thread) => [thread.id, thread]));
  const childrenByParentId = new Map<string, string[]>();
  for (const thread of threads) {
    const parentThreadId = String(thread.parentThreadId || "");
    if (!parentThreadId || !threadsById.has(parentThreadId)) continue;
    const children = childrenByParentId.get(parentThreadId) || [];
    children.push(thread.id);
    childrenByParentId.set(parentThreadId, children);
  }
  const cache = new Map<string, { count: number; fileSizeBytes: number; tokensUsed: number }>();

  const aggregateChildren = (threadId: string, visiting = new Set<string>()) => {
    const cached = cache.get(threadId);
    if (cached) return cached;
    if (visiting.has(threadId)) return { count: 0, fileSizeBytes: 0, tokensUsed: 0 };
    visiting.add(threadId);
    const aggregate = { count: 0, fileSizeBytes: 0, tokensUsed: 0 };
    for (const childThreadId of childrenByParentId.get(threadId) || []) {
      const childThread = threadsById.get(childThreadId);
      if (!childThread) continue;
      const childAggregate = aggregateChildren(childThreadId, visiting);
      aggregate.count += 1 + childAggregate.count;
      aggregate.fileSizeBytes += Number(childThread.fileSizeBytes || 0) + childAggregate.fileSizeBytes;
      aggregate.tokensUsed += numberField(childThread, "tokensUsed") + childAggregate.tokensUsed;
    }
    visiting.delete(threadId);
    cache.set(threadId, aggregate);
    return aggregate;
  };

  for (const thread of threads) {
    const aggregate = aggregateChildren(thread.id);
    thread.childThreadCount = aggregate.count;
    thread.childFileSizeBytes = aggregate.fileSizeBytes;
    thread.totalFileSizeBytes = Number(thread.fileSizeBytes || 0) + aggregate.fileSizeBytes;
    thread.childTokensUsed = aggregate.tokensUsed;
    thread.totalTokensUsed = numberField(thread, "tokensUsed") + aggregate.tokensUsed;
  }
}

type SessionIndexEntry = {
  rank: number;
  lineNumber: number;
  title: string;
  projectPath: string;
  updatedAtMs: number | null;
};

type ParsedThreadSample = {
  title: string;
  preview: string;
  projectPath: string;
  threadKind: "main" | "subagent";
  threadSource: string;
  parentThreadId: string;
  subagentStatus: string;
  agentNickname: string;
  agentRole: string;
  createdAtMs: number | null;
  updatedAtMs: number | null;
  hasUserEvent: boolean;
  tokensUsed: number;
  model: string;
  cliVersion: string;
  gitBranch: string;
  lineCount: number;
  parseErrors: number;
  rolloutDisplayStatus: string;
  rolloutDisplayResponseUserMessages: number;
  rolloutDisplayResponseAssistantMessages: number;
  rolloutDisplayVisibleUserMessages: number;
  rolloutDisplayVisibleAgentMessages: number;
  rolloutDisplayEventUserMessages: number;
  rolloutDisplayEventAgentMessages: number;
};

type RolloutDisplayIntegrity = {
  status: string;
  responseUserMessages: number;
  responseAssistantMessages: number;
  visibleUserMessages: number;
  visibleAgentMessages: number;
  eventUserMessages: number;
  eventAgentMessages: number;
  parseErrors: number;
};

type BrowserPluginCacheScan = {
  hasPluginCache: boolean;
  scannedRuntimeCount: number;
  validRuntimePaths: string[];
  unreadableRuntimePaths: string[];
  incompleteRuntimePaths: string[];
  truncated: boolean;
};

export type BrowserCodexWorkspace = {
  mode: "browser_folder";
  displayPath: string;
  directoryHandle: BrowserDirectoryHandle;
  snapshot: Record<string, unknown>;
  overview: Record<string, unknown>;
  diagnostics: Record<string, unknown>;
  threadFiles: Map<string, BrowserThreadFile>;
  generatedAtMs: number;
};

export type BrowserPromptRecord = {
  index: number;
  lineNumber: number;
  timestamp: string | null;
  text: string;
  characterCount: number;
  sourceType: string;
  sourceLabel: string;
  visibleByDefault: boolean;
  pureText: string;
  pureCharacterCount: number;
  hasPureText: boolean;
};

export function supportsBrowserFolderMode(): boolean {
  return typeof window !== "undefined" && typeof window.showDirectoryPicker === "function";
}

export async function pickBrowserCodexDirectory(): Promise<BrowserDirectoryHandle> {
  if (!window.showDirectoryPicker) {
    throw new Error("This browser does not support direct folder access. Use Microsoft Edge or Google Chrome, or start the local connector.");
  }
  const handle = await window.showDirectoryPicker({ id: "codex-home-manager", mode: "read" });
  await ensureReadPermission(handle);
  return handle;
}

export async function scanBrowserCodexHome(
  directoryHandle: BrowserDirectoryHandle,
  sidebarLimit: number,
  language: BrowserLanguage
): Promise<BrowserCodexWorkspace> {
  await ensureReadPermission(directoryHandle);
  const generatedAtMs = Date.now();
  const globalState = await readBrowserGlobalState(directoryHandle);
  const sessionIndexRecords = await readSessionIndexRecords(directoryHandle);
  const sessionIndex = sessionIndexMapFromRecords(sessionIndexRecords);
  const threadFiles = await listThreadJsonlFiles(directoryHandle);
  const threadFileMap = threadFileMapById(threadFiles);
  let stateRows: BrowserSqlThreadRow[] = [];
  let stateReadError = "";
  try {
    stateRows = await readBrowserStateThreadRows(directoryHandle);
  } catch (error) {
    stateReadError = error instanceof Error ? error.message : String(error);
  }
  const spawnEdges = stateRows.length ? await readBrowserStateSpawnEdges(directoryHandle) : new Map<string, Record<string, string>>();
  const threads = stateRows.length
    ? await buildBrowserThreadsFromSqlite({
        rows: stateRows,
        spawnEdges,
        sessionIndexRecords,
        sessionIndex,
        threadFileMap,
        globalState,
        sidebarLimit,
        language,
        generatedAtMs,
        validateRolloutDisplay: false
      })
    : await buildBrowserThreadsFromJsonl({
        sessionIndex,
        threadFiles,
        sidebarLimit,
        language,
        generatedAtMs,
        validateRolloutDisplay: false
      });
  applyThreadChildRollups(threads);

  const projects = stateRows.length ? buildProjects(threads, savedProjectPathsFromState(globalState), language) : buildProjects(threads, [], language);
  const totalStorageBytes = threads.reduce((total, thread) => total + Number(thread.fileSizeBytes || 0), 0);
  const mainThreads = threads.filter((thread) => thread.threadKind === "main");
  const subagentThreads = threads.filter((thread) => thread.threadKind === "subagent");
  const visibleThreads = mainThreads.filter((thread) => thread.codexVisible).length;
  const hiddenByInitialLimit = mainThreads.filter((thread) => thread.visibility === "hidden_by_initial_limit").length;
  const archivedThreads = threads.filter((thread) => thread.archived).length;
  const needsRepairThreads = mainThreads.filter((thread) => thread.visibility === "missing_file" || thread.visibility === "needs_user_event_repair").length;
  const resources = await inventoryResources(directoryHandle, language);
  const pluginCacheScan = await scanBrowserPluginCache(directoryHandle);
  const diagnostics = buildBrowserDiagnostics({
    codexHomeName: directoryHandle.name,
    generatedAtMs,
    threadCount: threads.length,
    resourceCount: resources.length,
    hasSessionIndex: await pathExists(directoryHandle, "session_index.jsonl"),
    hasStateDatabase: await pathExists(directoryHandle, "state_5.sqlite"),
    stateThreadCount: stateRows.length,
    stateReadError,
    usingStateDatabase: stateRows.length > 0,
    missingFileThreads: threads.filter((thread) => !thread.fileExists).length,
    hiddenByInitialLimit,
    pluginCacheScan,
    language
  });

  return {
    mode: "browser_folder",
    displayPath: `browser://${directoryHandle.name}`,
    directoryHandle,
    generatedAtMs,
    threadFiles: threadFileMap,
    snapshot: {
      codexHome: `browser://${directoryHandle.name}`,
      databasePath: "state_5.sqlite",
      globalStatePath: ".codex-global-state.json",
      sessionIndexPath: "session_index.jsonl",
      sidebarLimit,
      generatedAtMs,
      threads,
      projects,
      summary: {
        totalThreads: threads.length,
        mainThreads: mainThreads.length,
        subagentThreads: subagentThreads.length,
        eligibleThreads: mainThreads.filter((thread) => !thread.archived && thread.fileExists).length,
        codexVisibleThreads: visibleThreads,
        hiddenByInitialLimit,
        archivedThreads,
        needsRepairThreads,
        savedProjects: projects.length,
        workspaceProjects: projects.filter((project) => project.projectKind === "workspace_project").length,
        conversationProjects: projects.filter((project) => project.projectKind === "conversation").length,
        otherProjects: projects.filter((project) => project.projectKind === "other").length,
        emptyProjectsWithHiddenThreads: projects.filter((project) => project.emptyButHasHiddenThreads).length,
        totalStorageBytes
      }
    },
    overview: {
      codexHome: `browser://${directoryHandle.name}`,
      resources,
      generatedAtMs,
      summary: {
        resourceCount: resources.length,
        existingResourceCount: resources.filter((resource) => resource.exists).length,
        totalKnownResourceBytes: resources.reduce((total, resource) => total + Number(resource.sizeBytes || 0), 0),
        agentsFileCount: resources.filter((resource) => resource.exists && /(^|\/)AGENTS\.md$/i.test(resource.relativePath)).length,
        memoryExists: resources.some((resource) => resource.exists && resource.category === "memory"),
        skillsExists: resources.some((resource) => resource.exists && resource.category === "skill")
      }
    },
    diagnostics
  };
}

function threadFileMapById(threadFiles: BrowserThreadFile[]): Map<string, BrowserThreadFile> {
  const result = new Map<string, BrowserThreadFile>();
  for (const threadFile of threadFiles) {
    const threadId = threadIdFromPath(threadFile.relativePath);
    if (threadId) result.set(threadId, threadFile);
  }
  return result;
}

function sessionIndexMapFromRecords(records: BrowserSessionIndexRecord[]): Map<string, SessionIndexEntry> {
  const result = new Map<string, SessionIndexEntry>();
  for (const record of records) {
    result.set(record.threadId, {
      rank: record.sessionIndexLine,
      lineNumber: record.sessionIndexLine,
      title: record.sidebarTitle,
      projectPath: "",
      updatedAtMs: timestampMsFromObject({ timestamp: record.sessionIndexUpdatedAt })
    });
  }
  return result;
}

async function buildBrowserThreadsFromJsonl(input: {
  sessionIndex: Map<string, SessionIndexEntry>;
  threadFiles: BrowserThreadFile[];
  sidebarLimit: number;
  language: BrowserLanguage;
  generatedAtMs: number;
  validateRolloutDisplay: boolean;
}): Promise<BrowserThreadSnapshot[]> {
  const threads: BrowserThreadSnapshot[] = [];
  for (const threadFile of input.threadFiles) {
    const threadId = threadIdFromPath(threadFile.relativePath);
    if (!threadId) continue;
    const file = await threadFile.handle.getFile();
    const sample = await parseThreadSample(file);
    const indexEntry = input.sessionIndex.get(threadId);
    const title = firstNonEmpty(indexEntry?.title, sample.title, titleFromPath(threadFile.relativePath), threadId);
    const projectPath = firstNonEmpty(sample.projectPath, indexEntry?.projectPath, "");
    const projectKind = projectPath ? "workspace_project" : "conversation";
    const projectLabel = projectKind === "conversation" ? title : projectLabelFromPath(projectPath, input.language);
    const threadListRank = indexEntry?.rank ?? null;
    const updatedAtMs = indexEntry?.updatedAtMs || sample.updatedAtMs || file.lastModified || input.generatedAtMs;
    const createdAtMs = sample.createdAtMs || updatedAtMs;
    const hasUserEvent = sample.hasUserEvent;
    const threadKind = sample.threadKind;
    const explicitSidebarReference = Boolean(indexEntry);
    const inInitialSidebarPage = threadKind === "main" && explicitSidebarReference && threadListRank !== null && threadListRank <= input.sidebarLimit;
    const rolloutDisplayStatus = input.validateRolloutDisplay ? sample.rolloutDisplayStatus : "not_scanned";
    const displayNeedsRepair = ["missing_visible_event_stream", "sparse_visible_event_stream"].includes(rolloutDisplayStatus);
    const archived = threadFile.archivedStore;
    const fileExists = file.size > 0;
    const visibility = archived
      ? "archived"
      : !fileExists
        ? "missing_file"
        : threadKind === "subagent"
          ? "subagent"
          : displayNeedsRepair
            ? "needs_user_event_repair"
          : "visible";
    const hiddenReasons: string[] = [];
    if (archived) hiddenReasons.push("rollout_in_archived_sessions");
    if (!fileExists) hiddenReasons.push("missing_rollout_file");
    if (threadKind === "subagent" && !archived && fileExists) hiddenReasons.push("subagent_child_thread");
    if (displayNeedsRepair) hiddenReasons.push(sample.rolloutDisplayStatus);
    if (threadKind === "main" && !inInitialSidebarPage && !archived && fileExists) hiddenReasons.push("outside_initial_sidebar_limit");
    threads.push({
      id: threadId,
      title,
      sqliteTitle: "",
      sidebarTitle: indexEntry?.title || "",
      sessionIndexTitle: indexEntry?.title || "",
      sessionIndexUpdatedAt: indexEntry?.updatedAtMs ? new Date(indexEntry.updatedAtMs).toISOString() : "",
      rolloutTitle: sample.title,
      rolloutTitleTimestamp: "",
      rolloutTitleLine: null,
      preview: sample.preview,
      projectPath,
      projectLabel,
      projectKind,
      rolloutPath: threadFile.relativePath,
      source: "browser-folder-jsonl",
      threadKind,
      threadSource: sample.threadSource || "session_jsonl",
      parentThreadId: sample.parentThreadId,
      subagentStatus: sample.subagentStatus,
      agentNickname: sample.agentNickname,
      agentRole: sample.agentRole,
      model: sample.model,
      createdAtMs,
      updatedAtMs,
      archived,
      archivedAtMs: archived ? updatedAtMs : null,
      hasUserEvent,
      hasUserSignal: hasUserEvent,
      tokensUsed: sample.tokensUsed,
      fileExists,
      fileSizeBytes: file.size,
      fileModifiedAtMs: file.lastModified || null,
      rolloutInArchivedStore: archived,
      recentRank: null,
      threadListRank,
      sessionIndexRank: threadListRank,
      isPinned: false,
      explicitSidebarReference,
      inInitialSidebarPage,
      outsideInitialLimit: false,
      codexVisible: threadKind === "main" && !displayNeedsRepair && !archived && fileExists,
      visibility,
      hiddenReasons,
      rolloutDisplayStatus,
      rolloutDisplayResponseUserMessages: input.validateRolloutDisplay ? sample.rolloutDisplayResponseUserMessages : 0,
      rolloutDisplayResponseAssistantMessages: input.validateRolloutDisplay ? sample.rolloutDisplayResponseAssistantMessages : 0,
      rolloutDisplayVisibleUserMessages: input.validateRolloutDisplay ? sample.rolloutDisplayVisibleUserMessages : 0,
      rolloutDisplayVisibleAgentMessages: input.validateRolloutDisplay ? sample.rolloutDisplayVisibleAgentMessages : 0,
      rolloutDisplayEventUserMessages: input.validateRolloutDisplay ? sample.rolloutDisplayEventUserMessages : 0,
      rolloutDisplayEventAgentMessages: input.validateRolloutDisplay ? sample.rolloutDisplayEventAgentMessages : 0,
      gitBranch: sample.gitBranch,
      cliVersion: sample.cliVersion
    });
  }
  threads.sort((left, right) => Number(right.updatedAtMs || 0) - Number(left.updatedAtMs || 0));
  threads.forEach((thread, index) => {
    thread.recentRank = index + 1;
  });
  return threads;
}

async function buildBrowserThreadsFromSqlite(input: {
  rows: BrowserSqlThreadRow[];
  spawnEdges: Map<string, Record<string, string>>;
  sessionIndexRecords: BrowserSessionIndexRecord[];
  sessionIndex: Map<string, SessionIndexEntry>;
  threadFileMap: Map<string, BrowserThreadFile>;
  globalState: BrowserGlobalState;
  sidebarLimit: number;
  language: BrowserLanguage;
  generatedAtMs: number;
  validateRolloutDisplay: boolean;
}): Promise<BrowserThreadSnapshot[]> {
  const rowsByThreadId = new Map(input.rows.map((row) => [String(row.id), row]));
  const kindByThreadId = new Map<string, ReturnType<typeof threadKindMetadataBrowser>>();
  for (const row of input.rows) {
    kindByThreadId.set(String(row.id), threadKindMetadataBrowser(row, input.spawnEdges.get(String(row.id))));
  }
  const pinnedThreadIds = pinnedThreadIdsFromState(input.globalState);
  const explicitSidebarThreadIds = explicitSidebarThreadIdsFromState(input.globalState);
  const managerHiddenThreadIds = managerHiddenThreadIdsFromState(input.globalState);
  const savedProjectPaths = savedProjectPathsFromState(input.globalState);
  const savedProjectComparables = new Set(savedProjectPaths.map(comparablePathText));
  const projectlessThreadIds = projectlessThreadIdsFromState(input.globalState);
  const threadWorkspaceHints = threadWorkspaceRootHintsFromState(input.globalState);
  const conversationRoots = conversationRootCandidates(threadWorkspaceHints);
  const projectLabels = projectLabelsFromState(input.globalState);
  const sessionRankByThreadId = sidebarRankByThreadIdBrowser(input.sessionIndexRecords, rowsByThreadId, kindByThreadId);
  const threadListRankById = threadListRankByThreadIdBrowser(input.rows);
  const mainThreadListRankById = mainThreadListRankByThreadIdBrowser(input.rows, kindByThreadId);
  const threads: BrowserThreadSnapshot[] = [];

  for (const row of input.rows) {
    const threadId = String(row.id);
    const threadFile = input.threadFileMap.get(threadId);
    const fileStat = await statBrowserThreadFile(threadFile, row.rollout_path);
    const rolloutInArchivedStore = Boolean(threadFile?.archivedStore || comparablePathText(fileStat.path).includes("\\archived_sessions\\"));
    const projectPath = normalizePathText(row.cwd);
    const projectKind = classifyProjectKindBrowser(
      projectPath,
      savedProjectComparables,
      conversationRoots,
      projectlessThreadIds.has(threadId)
    );
    const projectLabel = projectDisplayLabelBrowser(projectPath, projectKind, projectLabels, input.language);
    const sessionIndexRank = sessionRankByThreadId.get(threadId) ?? null;
    const threadListRank = threadListRankById.get(threadId) ?? null;
    const mainThreadListRank = mainThreadListRankById.get(threadId) ?? null;
    const rankCandidates = [threadListRank, sessionIndexRank].filter((rank): rank is number => rank !== null);
    const recentRank = rankCandidates.length ? Math.min(...rankCandidates) : null;
    const kindMetadata = kindByThreadId.get(threadId) || threadKindMetadataBrowser(row, input.spawnEdges.get(threadId));
    const sidebarEntry = input.sessionIndex.get(threadId);
    const sqliteTitle = firstNonEmpty(stringValue(row.title), "(untitled)");
    const sessionIndexTitle = sidebarEntry?.title || "";
    const shouldResolveRolloutTitle = (
      projectKind === "conversation"
      && kindMetadata.threadKind === "main"
      && (
        pinnedThreadIds.has(threadId)
        || explicitSidebarThreadIds.has(threadId)
        || Boolean(mainThreadListRank !== null && mainThreadListRank <= input.sidebarLimit)
      )
    );
    const rolloutTitleEntry = shouldResolveRolloutTitle && threadFile
      ? await readBrowserRolloutThreadTitleUpdate(threadFile, threadId)
      : { rolloutTitle: "", rolloutTitleTimestamp: "", rolloutTitleLine: null };
    const rolloutTitle = String(rolloutTitleEntry.rolloutTitle || "");
    const sidebarTitle = rolloutTitle || sessionIndexTitle;
    const displayTitle = sidebarTitle || sqliteTitle;
    const hasUserSignal = rowHasUserSignalBrowser(row, sidebarEntry);
    const activeMainCandidate = kindMetadata.threadKind === "main" && !boolValue(row.archived) && fileStat.exists;
    const candidateThreadListVisible = Boolean(threadListRank !== null && threadListRank <= input.sidebarLimit);
    const candidateConversationVisible = Boolean(mainThreadListRank !== null && mainThreadListRank <= input.sidebarLimit);
    const candidateSessionIndexVisible = Boolean(sessionIndexRank !== null && sessionIndexRank <= input.sidebarLimit);
    const shouldValidateRolloutDisplay = input.validateRolloutDisplay && activeMainCandidate && (
      pinnedThreadIds.has(threadId)
      || explicitSidebarThreadIds.has(threadId)
      || (projectKind === "conversation" && candidateConversationVisible)
      || (projectKind !== "conversation" && (candidateThreadListVisible || candidateSessionIndexVisible))
    );
    const rolloutDisplay = shouldValidateRolloutDisplay
      ? await readBrowserRolloutDisplayIntegrity(threadFile)
      : emptyRolloutDisplayIntegrity();
    const classification = classifyThreadBrowser({
      row,
      threadId,
      threadListRank,
      mainThreadListRank,
      sessionIndexRank,
      sidebarLimit: input.sidebarLimit,
      pinnedThreadIds,
      explicitSidebarThreadIds,
      managerHiddenThreadIds,
      rolloutExists: fileStat.exists,
      threadKind: kindMetadata.threadKind,
      hasUserSignal,
      projectKind,
      rolloutInArchivedStore,
      rolloutDisplayStatus: rolloutDisplay.status
    });
    const updatedAtMs = timestampMsFromRowBrowser(row, "updated") || fileStat.modifiedAtMs || input.generatedAtMs;
    const createdAtMs = timestampMsFromRowBrowser(row, "created") || updatedAtMs;
    threads.push({
      id: threadId,
      title: displayTitle,
      sqliteTitle,
      sidebarTitle,
      sessionIndexTitle,
      sessionIndexUpdatedAt: sidebarEntry?.updatedAtMs ? new Date(sidebarEntry.updatedAtMs).toISOString() : "",
      rolloutTitle,
      rolloutTitleTimestamp: String(rolloutTitleEntry.rolloutTitleTimestamp || ""),
      rolloutTitleLine: Number(rolloutTitleEntry.rolloutTitleLine) || null,
      preview: stringValue(row.preview) || stringValue(row.first_user_message),
      projectPath,
      projectLabel,
      projectKind,
      rolloutPath: fileStat.path,
      source: stringValue(row.source) || "browser-folder-sqlite",
      ...kindMetadata,
      model: stringValue(row.model) || stringValue(row.model_provider),
      createdAtMs,
      updatedAtMs,
      archived: boolValue(row.archived),
      archivedAtMs: numberValue(row.archived_at) ? numberValue(row.archived_at) * 1000 : null,
      hasUserEvent: boolValue(row.has_user_event),
      hasUserSignal,
      tokensUsed: numberValue(row.tokens_used),
      fileExists: fileStat.exists,
      fileSizeBytes: fileStat.sizeBytes,
      fileModifiedAtMs: fileStat.modifiedAtMs,
      rolloutInArchivedStore,
      recentRank,
      threadListRank,
      mainThreadListRank,
      sessionIndexRank,
      isPinned: pinnedThreadIds.has(threadId),
      explicitSidebarReference: explicitSidebarThreadIds.has(threadId),
      managerHidden: managerHiddenThreadIds.has(threadId),
      presentInThreadList: threadListRank !== null,
      presentInSessionIndex: sessionIndexRank !== null,
      initialThreadListVisible: Boolean(threadListRank !== null && threadListRank <= input.sidebarLimit),
      initialSessionIndexVisible: Boolean(sessionIndexRank !== null && sessionIndexRank <= input.sidebarLimit),
      inInitialSidebarPage: classification.codexVisible,
      outsideInitialLimit: false,
      codexVisible: classification.codexVisible,
      visibility: classification.visibility,
      hiddenReasons: classification.hiddenReasons,
      rolloutDisplayStatus: rolloutDisplay.status,
      rolloutDisplayResponseUserMessages: rolloutDisplay.responseUserMessages,
      rolloutDisplayResponseAssistantMessages: rolloutDisplay.responseAssistantMessages,
      rolloutDisplayVisibleUserMessages: rolloutDisplay.visibleUserMessages,
      rolloutDisplayVisibleAgentMessages: rolloutDisplay.visibleAgentMessages,
      rolloutDisplayEventUserMessages: rolloutDisplay.eventUserMessages,
      rolloutDisplayEventAgentMessages: rolloutDisplay.eventAgentMessages,
      gitBranch: stringValue(row.git_branch),
      cliVersion: stringValue(row.cli_version)
    });
  }
  return threads;
}

export async function readBrowserThreadDetail(workspace: BrowserCodexWorkspace, threadId: string): Promise<Record<string, unknown>> {
  const snapshot = workspace.snapshot as { threads?: Array<Record<string, unknown>> };
  const thread = snapshot.threads?.find((item) => item.id === threadId);
  const threadFile = workspace.threadFiles.get(threadId);
  if (!thread || !threadFile) throw new Error(`Thread not found in selected folder: ${threadId}`);
  const file = await threadFile.handle.getFile();
  const sample = await parseThreadSample(file, true);
  const detailThread = {
    ...thread,
    rolloutDisplayStatus: sample.rolloutDisplayStatus,
    rolloutDisplayResponseUserMessages: sample.rolloutDisplayResponseUserMessages,
    rolloutDisplayResponseAssistantMessages: sample.rolloutDisplayResponseAssistantMessages,
    rolloutDisplayVisibleUserMessages: sample.rolloutDisplayVisibleUserMessages,
    rolloutDisplayVisibleAgentMessages: sample.rolloutDisplayVisibleAgentMessages,
    rolloutDisplayEventUserMessages: sample.rolloutDisplayEventUserMessages,
    rolloutDisplayEventAgentMessages: sample.rolloutDisplayEventAgentMessages
  };
  return {
    thread: detailThread,
    sqliteRow: {
      source: "browser-folder",
      note: "SQLite state is not parsed in browser folder mode."
    },
    rolloutStats: {
      lineCount: sample.lineCount,
      userMessages: sample.hasUserEvent ? 1 : 0,
      assistantMessages: 0,
      toolCalls: 0,
      toolOutputs: 0,
      eventMessages: 0,
      invalidJsonLines: sample.parseErrors,
      firstTimestamp: sample.createdAtMs ? new Date(sample.createdAtMs).toISOString() : null,
      lastTimestamp: sample.updatedAtMs ? new Date(sample.updatedAtMs).toISOString() : null
    },
    backups: []
  };
}

export async function readBrowserThreadDailyTokenUsage(workspace: BrowserCodexWorkspace, threadId: string) {
  return buildBrowserDailyTokenUsage(workspace, threadId);
}

async function buildBrowserDailyTokenUsage(workspace: BrowserCodexWorkspace, threadId: string) {
  const snapshot = workspace.snapshot as { threads?: Array<Record<string, unknown>> };
  const threads = snapshot.threads || [];
  const thread = threads.find((item) => item.id === threadId);
  const ownUsage = await readBrowserThreadFileDailyTokenUsage(workspace.threadFiles.get(threadId), thread);
  const childUsages = [];
  for (const childThread of browserDescendantThreads(threadId, threads)) {
    childUsages.push(await readBrowserThreadFileDailyTokenUsage(workspace.threadFiles.get(String(childThread.id || "")), childThread));
  }
  const recordsByDate = new Map<string, {
    date: string;
    ownTokens: number;
    childTokens: number;
    totalTokens: number;
    ownTokenEvents: number;
    childTokenEvents: number;
    ownUnknownTokenThreads: number;
    childUnknownTokenThreads: number;
    unknownTokenThreads: number;
    hasData: boolean;
    hasUnknownTokens: boolean;
  }>();
  const recordForDate = (date: string) => {
    let record = recordsByDate.get(date);
    if (!record) {
      record = {
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
      };
      recordsByDate.set(date, record);
    }
    return record;
  };
  for (const day of ownUsage.days) {
    const record = recordForDate(day.date);
    record.ownTokens += day.tokens;
    record.ownTokenEvents += day.tokenEvents;
    record.ownUnknownTokenThreads += day.unknownTokenThreads || 0;
  }
  for (const childUsage of childUsages) {
    for (const day of childUsage.days) {
      const record = recordForDate(day.date);
      record.childTokens += day.tokens;
      record.childTokenEvents += day.tokenEvents;
      record.childUnknownTokenThreads += day.unknownTokenThreads || 0;
    }
  }
  const datedDays = Array.from(recordsByDate.values())
    .map((record) => ({
      ...record,
      totalTokens: record.ownTokens + record.childTokens,
      unknownTokenThreads: record.ownUnknownTokenThreads + record.childUnknownTokenThreads,
      hasData: record.ownTokens + record.childTokens > 0,
      hasUnknownTokens: record.ownUnknownTokenThreads + record.childUnknownTokenThreads > 0
    }))
    .filter((record) => record.totalTokens > 0 || record.unknownTokenThreads > 0)
    .sort((left, right) => left.date.localeCompare(right.date));
  const activeDays = datedDays.filter((record) => record.totalTokens > 0);
  const days = completeBrowserDailyTokenDays(datedDays);
  const childTokens = childUsages.reduce((total, usage) => total + usage.summary.tokens, 0);
  const childTokenEvents = childUsages.reduce((total, usage) => total + usage.summary.tokenEvents, 0);
  const childCountedTokenEvents = childUsages.reduce((total, usage) => total + usage.summary.countedTokenEvents, 0);
  const childZeroDeltaTokenEvents = childUsages.reduce((total, usage) => total + usage.summary.zeroDeltaTokenEvents, 0);
  const childFallbackTokenEvents = childUsages.reduce((total, usage) => total + usage.summary.fallbackTokenEvents, 0);
  const childUnknownTokenThreads = childUsages.reduce((total, usage) => total + (usage.summary.unknownTokenThreads || 0), 0);
  const peak = activeDays.reduce<typeof activeDays[number] | null>((best, day) => !best || day.totalTokens > best.totalTokens ? day : best, null);
  return {
    summary: {
      ownTokens: ownUsage.summary.tokens,
      childTokens,
      totalTokens: ownUsage.summary.tokens + childTokens,
      days: activeDays.length,
      activeDays: activeDays.length,
      rangeDays: days.length,
      zeroDays: Math.max(0, days.length - activeDays.length),
      unknownDays: days.filter((day) => day.unknownTokenThreads > 0).length,
      firstDate: days[0]?.date || null,
      lastDate: days.at(-1)?.date || null,
      peakDate: peak?.date || null,
      peakTokens: peak?.totalTokens || 0,
      ownTokenEvents: ownUsage.summary.tokenEvents,
      childTokenEvents,
      ownCountedTokenEvents: ownUsage.summary.countedTokenEvents,
      childCountedTokenEvents,
      zeroDeltaTokenEvents: ownUsage.summary.zeroDeltaTokenEvents + childZeroDeltaTokenEvents,
      fallbackTokenEvents: ownUsage.summary.fallbackTokenEvents + childFallbackTokenEvents,
      ownUnknownTokenThreads: ownUsage.summary.unknownTokenThreads || 0,
      childUnknownTokenThreads,
      unknownTokenThreads: (ownUsage.summary.unknownTokenThreads || 0) + childUnknownTokenThreads,
      childThreadCount: childUsages.length,
      missingChildRolloutFiles: childUsages.filter((usage) => usage.summary.missingFile).length
    },
    days
  };
}

function browserDescendantThreads(threadId: string, threads: Array<Record<string, unknown>>) {
  const childrenByParentId = new Map<string, Array<Record<string, unknown>>>();
  for (const thread of threads) {
    const parentThreadId = String(thread.parentThreadId || "");
    if (!parentThreadId) continue;
    const children = childrenByParentId.get(parentThreadId) || [];
    children.push(thread);
    childrenByParentId.set(parentThreadId, children);
  }
  const descendants: Array<Record<string, unknown>> = [];
  const visit = (currentThreadId: string, visiting = new Set<string>()) => {
    if (visiting.has(currentThreadId)) return;
    visiting.add(currentThreadId);
    for (const child of childrenByParentId.get(currentThreadId) || []) {
      const childThreadId = String(child.id || "");
      if (!childThreadId) continue;
      descendants.push(child);
      visit(childThreadId, visiting);
    }
    visiting.delete(currentThreadId);
  };
  visit(threadId);
  return descendants;
}

async function readBrowserThreadFileDailyTokenUsage(threadFile: BrowserThreadFile | undefined, thread?: Record<string, unknown>) {
  if (!threadFile) return emptyBrowserDailyTokenUsage(true);
  const file = await threadFile.handle.getFile();
  const usage = parseBrowserDailyTokenUsage(await file.text());
  if (usage.summary.tokens > 0) return usage;
  const fallbackTokens = nonnegativeNumber(thread?.tokensUsed);
  const fallbackDate = dateKeyFromTimestampMs(thread?.updatedAtMs ?? thread?.fileModifiedAtMs ?? thread?.createdAtMs);
  if (!fallbackTokens || !fallbackDate) return usage;
  usage.days = [{ date: fallbackDate, tokens: 0, tokenEvents: 0, unknownTokenThreads: 1, hasUnknownTokens: true }];
  usage.summary.tokens = 0;
  usage.summary.days = 0;
  usage.summary.firstDate = fallbackDate;
  usage.summary.lastDate = fallbackDate;
  usage.summary.peakDate = null;
  usage.summary.peakTokens = 0;
  usage.summary.unknownTokenThreads = 1;
  usage.summary.hasUnknownTokens = true;
  return usage;
}

function emptyBrowserDailyTokenUsage(missingFile = false) {
  return {
    summary: {
      tokens: 0,
      days: 0,
      firstDate: null as string | null,
      lastDate: null as string | null,
      peakDate: null as string | null,
      peakTokens: 0,
      tokenEvents: 0,
      countedTokenEvents: 0,
      zeroDeltaTokenEvents: 0,
      fallbackTokenEvents: 0,
      unknownTokenThreads: 0,
      hasUnknownTokens: false,
      parseErrors: 0,
      missingFile
    },
    days: [] as Array<{ date: string; tokens: number; tokenEvents: number; unknownTokenThreads: number; hasUnknownTokens?: boolean }>
  };
}

function parseBrowserDailyTokenUsage(text: string) {
  const result = emptyBrowserDailyTokenUsage(false);
  const recordsByDate = new Map<string, { date: string; tokens: number; tokenEvents: number; unknownTokenThreads: number; hasUnknownTokens?: boolean }>();
  let previousCumulativeTokens: number | null = null;
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const item = parseJsonLine(line);
    if (!item) {
      result.summary.parseErrors += 1;
      continue;
    }
    const payload = item?.payload && typeof item.payload === "object" ? item.payload : null;
    if (item?.type !== "event_msg" || payload?.type !== "token_count") continue;
    result.summary.tokenEvents += 1;
    const date = dateKeyFromTimestamp(item.timestamp);
    if (!date) {
      result.summary.zeroDeltaTokenEvents += 1;
      continue;
    }
    const info = payload.info && typeof payload.info === "object" ? payload.info : {};
    const cumulativeTokens = usageTokenTotal(info.total_token_usage || info.totalTokenUsage);
    const lastTokens = usageTokenTotal(info.last_token_usage || info.lastTokenUsage);
    let tokenDelta: number | null = null;
    if (cumulativeTokens !== null) {
      if (previousCumulativeTokens === null) {
        tokenDelta = cumulativeTokens;
      } else if (cumulativeTokens >= previousCumulativeTokens) {
        tokenDelta = cumulativeTokens - previousCumulativeTokens;
      } else if (lastTokens !== null) {
        tokenDelta = lastTokens;
        result.summary.fallbackTokenEvents += 1;
      } else {
        tokenDelta = cumulativeTokens;
        result.summary.fallbackTokenEvents += 1;
      }
      previousCumulativeTokens = cumulativeTokens;
    } else if (lastTokens !== null) {
      tokenDelta = lastTokens;
      result.summary.fallbackTokenEvents += 1;
    }
    if (!tokenDelta || tokenDelta <= 0) {
      result.summary.zeroDeltaTokenEvents += 1;
      continue;
    }
    const record = recordsByDate.get(date) || { date, tokens: 0, tokenEvents: 0, unknownTokenThreads: 0, hasUnknownTokens: false };
    record.tokens += tokenDelta;
    record.tokenEvents += 1;
    recordsByDate.set(date, record);
    result.summary.tokens += tokenDelta;
    result.summary.countedTokenEvents += 1;
  }
  result.days = Array.from(recordsByDate.values()).sort((left, right) => left.date.localeCompare(right.date));
  result.summary.days = result.days.length;
  if (result.days.length) {
    result.summary.firstDate = result.days[0].date;
    result.summary.lastDate = result.days.at(-1)?.date || null;
    const peak = result.days.reduce((best, day) => day.tokens > best.tokens ? day : best, result.days[0]);
    result.summary.peakDate = peak.date;
    result.summary.peakTokens = peak.tokens;
  }
  return result;
}

function completeBrowserDailyTokenDays(days: Array<{
  date: string;
  ownTokens: number;
  childTokens: number;
  totalTokens: number;
  ownTokenEvents: number;
  childTokenEvents: number;
  ownUnknownTokenThreads: number;
  childUnknownTokenThreads: number;
  unknownTokenThreads: number;
  hasData: boolean;
  hasUnknownTokens: boolean;
}>) {
  if (!days.length) return days;
  const firstMs = parseBrowserDateKeyMs(days[0].date);
  const lastMs = parseBrowserDateKeyMs(days.at(-1)?.date || "");
  if (firstMs === null || lastMs === null || lastMs < firstMs) return days;
  const byDate = new Map(days.map((day) => [day.date, day]));
  const completed = [];
  for (let timestampMs = firstMs; timestampMs <= lastMs; timestampMs += 24 * 60 * 60 * 1000) {
    const date = new Date(timestampMs).toISOString().slice(0, 10);
    completed.push(byDate.get(date) || {
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

function parseBrowserDateKeyMs(date: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const parsed = Date.parse(`${date}T00:00:00.000Z`);
  return Number.isFinite(parsed) ? parsed : null;
}

function usageTokenTotal(usage: unknown): number | null {
  if (!usage || typeof usage !== "object") return null;
  const record = usage as Record<string, unknown>;
  const totalTokens = nonnegativeNumber(record.total_tokens ?? record.totalTokens);
  if (totalTokens !== null) return totalTokens;
  const inputTokens = nonnegativeNumber(record.input_tokens ?? record.inputTokens) || 0;
  const outputTokens = nonnegativeNumber(record.output_tokens ?? record.outputTokens) || 0;
  return inputTokens || outputTokens ? inputTokens + outputTokens : null;
}

function nonnegativeNumber(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : null;
}

function dateKeyFromTimestamp(timestamp: unknown): string | null {
  const timestampMs = timestampMsFromObject({ timestamp });
  return dateKeyFromTimestampMs(timestampMs);
}

function dateKeyFromTimestampMs(timestampMs: unknown): string | null {
  const normalizedTimestampMs = nonnegativeNumber(timestampMs);
  if (normalizedTimestampMs === null) return null;
  const date = new Date(normalizedTimestampMs);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export async function readBrowserThreadLogs(
  workspace: BrowserCodexWorkspace,
  threadId: string,
  offset: number,
  limit: number,
  kind: string,
  search: string
): Promise<Record<string, unknown>> {
  const threadFile = workspace.threadFiles.get(threadId);
  if (!threadFile) throw new Error(`Thread not found in selected folder: ${threadId}`);
  const file = await threadFile.handle.getFile();
  const text = await file.text();
  const normalizedSearch = search.trim().toLowerCase();
  const entries = text.split(/\r?\n/).filter(Boolean).map((line, index) => {
    const parsed = parseJsonLine(line);
    const label = labelFromLogObject(parsed) || "JSONL";
    const message = extractMessage(parsed) || line.slice(0, 500);
    const entryKind = String((parsed?.type || parsed?.payload?.type || parsed?.payloadType || "event"));
    return {
      source: "rollout_jsonl",
      lineNumber: index + 1,
      appLogId: null,
      timestamp: timestampFromObject(parsed),
      timestampMs: timestampMsFromObject(parsed),
      type: String(parsed?.type || ""),
      payloadType: String(parsed?.payload?.type || ""),
      role: String(parsed?.payload?.role || parsed?.role || ""),
      kind: entryKind,
      label,
      severity: severityFromLogObject(parsed),
      message,
      messageTruncated: message.length >= 500,
      rawLine: line.length > 2000 ? `${line.slice(0, 2000)}...` : line,
      rawLineTruncated: line.length > 2000
    };
  }).filter((entry) => {
    const kindMatches = kind === "all" || entry.kind.toLowerCase().includes(kind.toLowerCase()) || entry.label.toLowerCase().includes(kind.toLowerCase());
    const searchMatches = !normalizedSearch || entry.message.toLowerCase().includes(normalizedSearch) || entry.rawLine.toLowerCase().includes(normalizedSearch);
    return kindMatches && searchMatches;
  });
  const page = entries.slice(offset, offset + limit);
  return {
    threadId,
    source: "rollout",
    rolloutPath: threadFile.relativePath,
    appLogPath: "",
    offset,
    limit,
    kind,
    search,
    matchedEntries: entries.length,
    hasMore: offset + limit < entries.length,
    entries: page,
    summary: {
      lineCount: entries.length,
      parseErrors: entries.filter((entry) => entry.label === "parse_error").length,
      byKind: countBy(entries, (entry) => entry.kind || "event"),
      bySeverity: countBy(entries, (entry) => entry.severity || "info")
    }
  };
}

export async function exportBrowserThreadPrompts(workspace: BrowserCodexWorkspace, threadId: string): Promise<{ promptCount: number; filename: string }> {
  const threadFile = workspace.threadFiles.get(threadId);
  if (!threadFile) throw new Error(`Thread not found in selected folder: ${threadId}`);
  const file = await threadFile.handle.getFile();
  const text = await file.text();
  const allPrompts = extractUserPromptRecords(text);
  const prompts = allPrompts.filter((prompt) => prompt.hasPureText);
  const sourceCounts = browserPromptSourceCounts(allPrompts);
  const filename = `codex-thread-prompts-${threadId}.md`;
  const markdown = [
    `# Codex thread prompts`,
    ``,
    `Thread ID: \`${threadId}\``,
    `Source: \`${threadFile.relativePath}\``,
    `Prompt count: ${prompts.length}`,
    `All prompt-like records: ${allPrompts.length}`,
    `Filter scope: \`pure\``,
    `Source counts: \`${JSON.stringify(sourceCounts)}\``,
    ``,
    ...prompts.flatMap((prompt) => [
      `## Prompt ${prompt.index}`,
      ``,
      `- JSONL line: \`${prompt.lineNumber}\``,
      prompt.timestamp ? `- Timestamp: \`${prompt.timestamp}\`` : "",
      `- Source: \`${prompt.sourceLabel || prompt.sourceType}\``,
      ``,
      prompt.pureText,
      ""
    ])
  ].join("\n");
  downloadText(filename, markdown);
  return { promptCount: prompts.length, filename };
}

export async function readBrowserThreadPrompts(workspace: BrowserCodexWorkspace, threadId: string): Promise<Record<string, unknown>> {
  const threadFile = workspace.threadFiles.get(threadId);
  if (!threadFile) throw new Error(`Thread not found in selected folder: ${threadId}`);
  const file = await threadFile.handle.getFile();
  const text = await file.text();
  const prompts = extractUserPromptRecords(text);
  const thread = (workspace.snapshot.threads as Record<string, unknown>[] | undefined)?.find((item) => item.id === threadId);
  const visiblePromptCount = prompts.filter((prompt) => prompt.visibleByDefault).length;
  const purePromptCount = prompts.filter((prompt) => prompt.hasPureText).length;
  return {
    threadId,
    title: thread?.title || "",
    rolloutPath: threadFile.relativePath,
    promptCount: prompts.length,
    purePromptCount,
    visiblePromptCount,
    hiddenPromptCount: prompts.length - visiblePromptCount,
    sourceCounts: browserPromptSourceCounts(prompts),
    prompts
  };
}

export async function readBrowserResource(workspace: BrowserCodexWorkspace, relativePath: string): Promise<Record<string, unknown>> {
  const normalizedPath = normalizeRelativePath(relativePath);
  const metadata = await describeResource(workspace.directoryHandle, normalizedPath, "", "", "");
  if (!metadata.exists) throw new Error(`Resource not found: ${normalizedPath}`);
  const target = await getPathHandle(workspace.directoryHandle, normalizedPath);
  if (!target) throw new Error(`Resource not found: ${normalizedPath}`);
  if (target.kind === "directory") {
    const children = [];
    for await (const [name, child] of target.entries()) {
      const childPath = normalizedPath ? `${normalizedPath}/${name}` : name;
      children.push(await describeResource(workspace.directoryHandle, childPath, name, "directory", ""));
    }
    children.sort((left, right) => String(left.relativePath).localeCompare(String(right.relativePath)));
    return { metadata, content: null, children, truncated: false, binary: false };
  }
  const file = await target.getFile();
  const binary = isProbablyBinary(normalizedPath, file);
  return {
    metadata,
    content: binary ? null : await file.text(),
    children: [],
    truncated: false,
    binary
  };
}

async function ensureReadPermission(handle: BrowserDirectoryHandle | BrowserFileHandle): Promise<void> {
  if (!handle.queryPermission || !handle.requestPermission) return;
  const current = await handle.queryPermission({ mode: "read" });
  if (current === "granted") return;
  const requested = await handle.requestPermission({ mode: "read" });
  if (requested !== "granted") throw new Error("Folder permission was not granted.");
}

async function readBrowserGlobalState(root: BrowserDirectoryHandle): Promise<BrowserGlobalState> {
  const handle = await getPathHandle(root, ".codex-global-state.json");
  if (!handle || handle.kind !== "file") return {};
  try {
    const parsed = JSON.parse(await (await handle.getFile()).text());
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as BrowserGlobalState : {};
  } catch {
    return {};
  }
}

function sqliteRowsFromResult(result: { columns: string[]; values: unknown[][] }[]): Record<string, unknown>[] {
  const rows: Record<string, unknown>[] = [];
  for (const table of result) {
    for (const values of table.values) {
      const row: Record<string, unknown> = {};
      table.columns.forEach((column, index) => {
        row[column] = values[index];
      });
      rows.push(row);
    }
  }
  return rows;
}

function execSqliteRows(database: SqlJsDatabase, sql: string): Record<string, unknown>[] {
  return sqliteRowsFromResult(database.exec(sql));
}

async function readBrowserStateThreadRows(root: BrowserDirectoryHandle): Promise<BrowserSqlThreadRow[]> {
  const handle = await getPathHandle(root, "state_5.sqlite");
  if (!handle || handle.kind !== "file") return [];
  const file = await handle.getFile();
  const sql = await loadSqlJs();
  const database = new sql.Database(new Uint8Array(await file.arrayBuffer()));
  try {
    return execSqliteRows(database, "SELECT * FROM threads ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC, id DESC")
      .filter((row) => row.id)
      .map((row) => ({ ...row, id: String(row.id) }));
  } finally {
    database.close();
  }
}

async function readBrowserStateSpawnEdges(root: BrowserDirectoryHandle): Promise<Map<string, Record<string, string>>> {
  const handle = await getPathHandle(root, "state_5.sqlite");
  if (!handle || handle.kind !== "file") return new Map();
  const file = await handle.getFile();
  const sql = await loadSqlJs();
  const database = new sql.Database(new Uint8Array(await file.arrayBuffer()));
  try {
    const tableExists = database.exec("SELECT 1 AS exists_flag FROM sqlite_master WHERE type = 'table' AND name = 'thread_spawn_edges'");
    if (!tableExists.length || !tableExists[0]?.values?.length) return new Map();
    const rows = execSqliteRows(database, "SELECT parent_thread_id AS parentThreadId, child_thread_id AS childThreadId, status AS subagentStatus FROM thread_spawn_edges");
    const result = new Map<string, Record<string, string>>();
    for (const row of rows) {
      const childThreadId = stringValue(row.childThreadId);
      if (!childThreadId) continue;
      result.set(childThreadId, {
        parentThreadId: stringValue(row.parentThreadId),
        subagentStatus: stringValue(row.subagentStatus)
      });
    }
    return result;
  } finally {
    database.close();
  }
}

async function readSessionIndexRecords(root: BrowserDirectoryHandle): Promise<BrowserSessionIndexRecord[]> {
  const indexHandle = await getPathHandle(root, "session_index.jsonl");
  const records: BrowserSessionIndexRecord[] = [];
  if (!indexHandle || indexHandle.kind !== "file") return records;
  const text = await (await indexHandle.getFile()).text();
  let lineNumber = 0;
  for (const line of text.split(/\r?\n/)) {
    lineNumber += 1;
    if (!line.trim()) continue;
    const parsed = parseJsonLine(line);
    const threadId = stringValue(parsed?.id) || JSON.stringify(parsed).match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i)?.[0] || "";
    if (!threadId) continue;
    records.push({
      threadId,
      sidebarTitle: firstStringByKeys(parsed, ["thread_name", "title", "name", "conversationTitle"]) || "",
      sessionIndexUpdatedAt: firstStringByKeys(parsed, ["updated_at", "updatedAt", "timestamp"]) || "",
      sessionIndexLine: lineNumber
    });
  }
  return records;
}

async function readSessionIndex(root: BrowserDirectoryHandle): Promise<Map<string, SessionIndexEntry>> {
  const indexHandle = await getPathHandle(root, "session_index.jsonl");
  const entries = new Map<string, SessionIndexEntry>();
  if (!indexHandle || indexHandle.kind !== "file") return entries;
  const text = await (await indexHandle.getFile()).text();
  let lineNumber = 0;
  for (const line of text.split(/\r?\n/)) {
    lineNumber += 1;
    if (!line.trim()) continue;
    const parsed = parseJsonLine(line);
    const serialized = JSON.stringify(parsed);
    const threadId = stringValue(parsed?.id) || serialized.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i)?.[0];
    if (!threadId) continue;
    entries.set(threadId, {
      rank: lineNumber,
      lineNumber,
      title: firstStringByKeys(parsed, ["thread_name", "title", "name", "conversationTitle"]) || "",
      projectPath: firstStringByKeys(parsed, ["cwd", "projectPath", "project_path", "workspace"]) || "",
      updatedAtMs: timestampMsFromObject(parsed)
    });
  }
  return entries;
}

function sidebarRankByThreadIdBrowser(
  sessionIndexRecords: BrowserSessionIndexRecord[],
  rowsByThreadId: Map<string, BrowserSqlThreadRow>,
  kindByThreadId: Map<string, { threadKind: string }>
): Map<string, number> {
  const ranks = new Map<string, number>();
  let rawRank = 0;
  const sortedRecords = [...sessionIndexRecords].sort((left, right) => right.sessionIndexLine - left.sessionIndexLine);
  for (const record of sortedRecords) {
    const row = rowsByThreadId.get(record.threadId);
    if (!row) continue;
    if (boolValue(row.archived)) continue;
    if (kindByThreadId.get(record.threadId)?.threadKind !== "main") continue;
    rawRank += 1;
    if (!ranks.has(record.threadId)) ranks.set(record.threadId, rawRank);
  }
  return ranks;
}

function threadListRankByThreadIdBrowser(rows: BrowserSqlThreadRow[]): Map<string, number> {
  const ranks = new Map<string, number>();
  let rank = 0;
  for (const row of rows) {
    if (boolValue(row.archived)) continue;
    rank += 1;
    ranks.set(String(row.id), rank);
  }
  return ranks;
}

function mainThreadListRankByThreadIdBrowser(
  rows: BrowserSqlThreadRow[],
  kindByThreadId: Map<string, { threadKind: string }>
): Map<string, number> {
  const ranks = new Map<string, number>();
  let rank = 0;
  for (const row of rows) {
    if (boolValue(row.archived)) continue;
    const threadId = String(row.id);
    if (kindByThreadId.get(threadId)?.threadKind !== "main") continue;
    rank += 1;
    ranks.set(threadId, rank);
  }
  return ranks;
}

function parseSourceMetadataBrowser(sourceText: unknown): Record<string, unknown> {
  const text = stringValue(sourceText);
  if (!text || !text.trimStart().startsWith("{")) return {};
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function nestedRecord(value: unknown, key: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const nested = (value as Record<string, unknown>)[key];
  return nested && typeof nested === "object" && !Array.isArray(nested) ? nested as Record<string, unknown> : {};
}

function threadKindMetadataBrowser(row: BrowserSqlThreadRow, spawnEdge: Record<string, string> | undefined) {
  const sourceMetadata = parseSourceMetadataBrowser(row.source);
  const subagentMetadata = nestedRecord(sourceMetadata, "subagent");
  const subagentSpawn = nestedRecord(subagentMetadata, "thread_spawn");
  const threadSource = stringValue(row.thread_source);
  const isSubagent = Boolean(threadSource === "subagent" || spawnEdge || Object.keys(subagentMetadata).length);
  return {
    threadKind: isSubagent ? "subagent" as const : "main" as const,
    threadSource: threadSource || stringValue(row.source),
    parentThreadId: stringValue(spawnEdge?.parentThreadId) || stringValue(subagentSpawn.parent_thread_id),
    subagentStatus: stringValue(spawnEdge?.subagentStatus),
    agentNickname: stringValue(row.agent_nickname) || stringValue(subagentSpawn.agent_nickname),
    agentRole: stringValue(row.agent_role) || stringValue(subagentSpawn.agent_role)
  };
}

async function statBrowserThreadFile(threadFile: BrowserThreadFile | undefined, rolloutPath: unknown) {
  const fallbackPath = normalizePathText(rolloutPath);
  if (!threadFile) {
    return { exists: false, sizeBytes: 0, modifiedAtMs: null as number | null, path: fallbackPath };
  }
  const file = await threadFile.handle.getFile();
  return {
    exists: file.size > 0,
    sizeBytes: file.size,
    modifiedAtMs: file.lastModified || null,
    path: fallbackPath || threadFile.relativePath
  };
}

function rowHasUserSignalBrowser(row: BrowserSqlThreadRow, sidebarEntry: SessionIndexEntry | undefined): boolean {
  if (boolValue(row.has_user_event)) return true;
  if (stringValue(row.thread_source).toLowerCase() === "user") return true;
  if (stringValue(row.first_user_message)) return true;
  if (stringValue(row.preview)) return true;
  return Boolean(sidebarEntry);
}

function classifyThreadBrowser(input: {
  row: BrowserSqlThreadRow;
  threadId: string;
  threadListRank: number | null;
  mainThreadListRank: number | null;
  sessionIndexRank: number | null;
  sidebarLimit: number;
  pinnedThreadIds: Set<string>;
  explicitSidebarThreadIds: Set<string>;
  managerHiddenThreadIds: Set<string>;
  rolloutExists: boolean;
  threadKind: "main" | "subagent";
  hasUserSignal: boolean;
  projectKind: BrowserProjectKind;
  rolloutInArchivedStore: boolean;
  rolloutDisplayStatus: string;
}) {
  const archived = boolValue(input.row.archived);
  const hasUserEvent = boolValue(input.row.has_user_event);
  const isPinned = input.pinnedThreadIds.has(input.threadId);
  const hasSessionIndex = input.sessionIndexRank !== null;
  const hasThreadListRank = input.threadListRank !== null;
  const hasMainThreadListRank = input.mainThreadListRank !== null;
  const hasExplicitSidebarReference = input.explicitSidebarThreadIds.has(input.threadId);
  const isManagerHidden = input.managerHiddenThreadIds.has(input.threadId);
  const hiddenReasons: string[] = [];

  if (archived) hiddenReasons.push("archived");
  if (!input.hasUserSignal) hiddenReasons.push("missing_user_signal");
  if (input.hasUserSignal && !hasUserEvent) hiddenReasons.push("metadata_has_user_event_false");
  if (!hasSessionIndex) hiddenReasons.push("missing_session_index_entry");
  if (!input.rolloutExists) hiddenReasons.push("missing_rollout_file");
  if (input.rolloutInArchivedStore) hiddenReasons.push("rollout_in_archived_sessions");
  if (["missing_visible_event_stream", "sparse_visible_event_stream"].includes(input.rolloutDisplayStatus)) hiddenReasons.push(input.rolloutDisplayStatus);
  if (input.threadKind === "subagent") hiddenReasons.push("subagent_child_thread");
  if (hasThreadListRank && input.threadListRank && input.threadListRank > input.sidebarLimit) hiddenReasons.push("outside_thread_list_initial_page");
  if (input.projectKind === "conversation" && input.mainThreadListRank && input.mainThreadListRank > input.sidebarLimit) hiddenReasons.push("outside_conversation_initial_page");
  if (hasSessionIndex && input.sessionIndexRank && input.sessionIndexRank > input.sidebarLimit) hiddenReasons.push("outside_session_index_repair_window");
  if (isManagerHidden) hiddenReasons.push("manually_hidden_by_manager");

  const activeMain = input.threadKind === "main" && !archived && input.rolloutExists;
  const sidebarVisible = input.projectKind === "conversation"
    ? activeMain && !isManagerHidden && (
        hasMainThreadListRank
        || hasThreadListRank
        || hasSessionIndex
        || hasExplicitSidebarReference
        || isPinned
      )
    : activeMain && !isManagerHidden && (
        hasThreadListRank
        || hasSessionIndex
        || isPinned
        || hasExplicitSidebarReference
      );

  if (input.threadKind === "subagent" && !archived && input.rolloutExists) return { visibility: "subagent", hiddenReasons, codexVisible: false };
  if (archived) return { visibility: "archived", hiddenReasons, codexVisible: false };
  if (!input.rolloutExists) return { visibility: "missing_file", hiddenReasons, codexVisible: false };
  if (activeMain && input.rolloutInArchivedStore) return { visibility: "needs_user_event_repair", hiddenReasons, codexVisible: false };
  if (activeMain && isManagerHidden) return { visibility: "hidden", hiddenReasons, codexVisible: false };
  if (activeMain && ["missing_visible_event_stream", "sparse_visible_event_stream"].includes(input.rolloutDisplayStatus)) {
    return { visibility: "needs_user_event_repair", hiddenReasons, codexVisible: false };
  }
  if (sidebarVisible) return { visibility: "visible", hiddenReasons, codexVisible: true };
  if (!input.hasUserSignal) return { visibility: "hidden", hiddenReasons, codexVisible: false };
  if (!hasUserEvent || !hasSessionIndex) return { visibility: "needs_user_event_repair", hiddenReasons, codexVisible: false };
  return { visibility: "hidden", hiddenReasons, codexVisible: false };
}

async function readBrowserRolloutThreadTitleUpdate(threadFile: BrowserThreadFile, threadId: string) {
  const result = { rolloutTitle: "", rolloutTitleTimestamp: "", rolloutTitleLine: null as number | null };
  const text = await (await threadFile.handle.getFile()).text();
  let lineNumber = 0;
  for (const line of text.split(/\r?\n/)) {
    lineNumber += 1;
    if (!line.includes("thread_name_updated")) continue;
    const item = parseJsonLine(line);
    if (item?.type !== "event_msg") continue;
    const payload = item.payload || {};
    if (payload.type !== "thread_name_updated") continue;
    const payloadThreadId = stringValue(payload.thread_id);
    if (payloadThreadId && payloadThreadId !== threadId) continue;
    const threadName = stringValue(payload.thread_name);
    if (!threadName) continue;
    result.rolloutTitle = threadName;
    result.rolloutTitleTimestamp = stringValue(item.timestamp);
    result.rolloutTitleLine = lineNumber;
  }
  return result;
}


async function listThreadJsonlFiles(root: BrowserDirectoryHandle): Promise<BrowserThreadFile[]> {
  const results: BrowserThreadFile[] = [];
  for (const base of ["sessions", "archived_sessions"]) {
    const directory = await getPathHandle(root, base);
    if (!directory || directory.kind !== "directory") continue;
    await walkFiles(directory, base, results, base === "archived_sessions");
  }
  return results;
}

async function walkFiles(directory: BrowserDirectoryHandle, relativePath: string, results: BrowserThreadFile[], archivedStore: boolean): Promise<void> {
  for await (const [name, handle] of directory.entries()) {
    const childPath = `${relativePath}/${name}`;
    if (handle.kind === "directory") {
      await walkFiles(handle, childPath, results, archivedStore);
    } else if (/\.jsonl$/i.test(name)) {
      results.push({ relativePath: childPath, handle, archivedStore });
    }
  }
}

async function parseThreadSample(file: File, full = false): Promise<ParsedThreadSample> {
  const text = full || file.size <= 3_000_000
    ? await file.text()
    : `${await file.slice(0, 2_000_000).text()}\n${await file.slice(Math.max(0, file.size - 500_000)).text()}`;
  const rolloutDisplay = rolloutDisplayIntegrityFromText(text);
  const lines = text.split(/\r?\n/).filter(Boolean);
  let title = "";
  let preview = "";
  let projectPath = "";
  let createdAtMs: number | null = null;
  let updatedAtMs: number | null = null;
  let hasUserEvent = false;
  let tokensUsed = 0;
  let model = "";
  let cliVersion = "";
  let gitBranch = "";
  let threadKind: "main" | "subagent" = "main";
  let threadSource = "";
  let parentThreadId = "";
  let subagentStatus = "";
  let agentNickname = "";
  let agentRole = "";
  let parseErrors = 0;

  for (const line of lines) {
    const parsed = parseJsonLine(line);
    if (!parsed) {
      parseErrors += 1;
      continue;
    }
    const timestampMs = timestampMsFromObject(parsed);
    if (timestampMs) {
      createdAtMs = createdAtMs ? Math.min(createdAtMs, timestampMs) : timestampMs;
      updatedAtMs = updatedAtMs ? Math.max(updatedAtMs, timestampMs) : timestampMs;
    }
    projectPath ||= firstStringByKeys(parsed, ["cwd", "current_working_directory", "projectPath", "project_path"]) || "";
    model ||= firstStringByKeys(parsed, ["model", "model_slug"]) || "";
    cliVersion ||= firstStringByKeys(parsed, ["cli_version", "version", "codex_version"]) || "";
    gitBranch ||= firstStringByKeys(parsed, ["git_branch", "branch"]) || "";
    const metadata = extractThreadMetadata(parsed, line);
    if (metadata.threadKind === "subagent") threadKind = "subagent";
    threadSource ||= metadata.threadSource;
    parentThreadId ||= metadata.parentThreadId;
    subagentStatus ||= metadata.subagentStatus;
    agentNickname ||= metadata.agentNickname;
    agentRole ||= metadata.agentRole;
    tokensUsed += firstNumberByKeys(parsed, ["total_tokens", "tokens_used", "tokensUsed"]) || 0;
    const message = extractMessage(parsed);
    const role = String(parsed?.payload?.role || parsed?.role || "").toLowerCase();
    const type = String(parsed?.type || parsed?.payload?.type || "").toLowerCase();
    if (!hasUserEvent && (role === "user" || type.includes("user"))) {
      hasUserEvent = true;
      title ||= compactText(message).slice(0, 80);
      preview ||= compactText(message).slice(0, 180);
    }
  }
  if (threadKind !== "subagent" && title.toLowerCase().startsWith("# agents.md instructions for")) {
    threadKind = "subagent";
    threadSource ||= "subagent_title_heuristic";
  }
  return {
    title,
    preview,
    projectPath,
    threadKind,
    threadSource,
    parentThreadId,
    subagentStatus,
    agentNickname,
    agentRole,
    createdAtMs,
    updatedAtMs,
    hasUserEvent,
    tokensUsed,
    model,
    cliVersion,
    gitBranch,
    lineCount: lines.length,
    parseErrors,
    rolloutDisplayStatus: rolloutDisplay.status,
    rolloutDisplayResponseUserMessages: rolloutDisplay.responseUserMessages,
    rolloutDisplayResponseAssistantMessages: rolloutDisplay.responseAssistantMessages,
    rolloutDisplayVisibleUserMessages: rolloutDisplay.visibleUserMessages,
    rolloutDisplayVisibleAgentMessages: rolloutDisplay.visibleAgentMessages,
    rolloutDisplayEventUserMessages: rolloutDisplay.eventUserMessages,
    rolloutDisplayEventAgentMessages: rolloutDisplay.eventAgentMessages
  };
}

function extractThreadMetadata(parsed: any, rawLine: string) {
  const payload = parsed?.payload && typeof parsed.payload === "object" ? parsed.payload : {};
  const payloadSource = payload?.source && typeof payload.source === "object" ? payload.source : {};
  const rootSource = parsed?.source && typeof parsed.source === "object" ? parsed.source : {};
  const payloadSubagent = payload?.subagent && typeof payload.subagent === "object" ? payload.subagent : {};
  const sourceSubagent = payloadSource?.subagent && typeof payloadSource.subagent === "object"
    ? payloadSource.subagent
    : rootSource?.subagent && typeof rootSource.subagent === "object"
      ? rootSource.subagent
      : {};
  const threadSpawn = sourceSubagent?.thread_spawn && typeof sourceSubagent.thread_spawn === "object"
    ? sourceSubagent.thread_spawn
    : sourceSubagent?.threadSpawn && typeof sourceSubagent.threadSpawn === "object"
      ? sourceSubagent.threadSpawn
      : payloadSubagent?.thread_spawn && typeof payloadSubagent.thread_spawn === "object"
        ? payloadSubagent.thread_spawn
        : payloadSubagent?.threadSpawn && typeof payloadSubagent.threadSpawn === "object"
          ? payloadSubagent.threadSpawn
          : {};
  const threadSource = stringValue(payload.thread_source)
    || stringValue(payload.threadSource)
    || stringValue(parsed?.thread_source)
    || stringValue(parsed?.threadSource);
  const rawPrefix = rawLine.slice(0, 24000).toLowerCase();
  const hasSubagentObject = Boolean(Object.keys(sourceSubagent).length || Object.keys(payloadSubagent).length || Object.keys(threadSpawn).length);
  const hasSubagentTextSignal = rawPrefix.includes("\"subagent\"") && (rawPrefix.includes("thread_spawn") || rawPrefix.includes("parent_thread_id"));
  const threadKind = threadSource.toLowerCase() === "subagent" || hasSubagentObject || hasSubagentTextSignal ? "subagent" : "main";
  return {
    threadKind: threadKind as "main" | "subagent",
    threadSource: threadSource || (threadKind === "subagent" ? "subagent" : ""),
    parentThreadId: stringValue(threadSpawn.parent_thread_id)
      || stringValue(threadSpawn.parentThreadId)
      || stringValue(payload.parent_thread_id)
      || stringValue(payload.parentThreadId)
      || stringValue(parsed?.parent_thread_id)
      || stringValue(parsed?.parentThreadId),
    subagentStatus: stringValue(sourceSubagent.status)
      || stringValue(payloadSubagent.status)
      || stringValue(payload.subagent_status)
      || stringValue(payload.subagentStatus),
    agentNickname: stringValue(threadSpawn.agent_nickname)
      || stringValue(threadSpawn.agentNickname)
      || stringValue(sourceSubagent.agent_nickname)
      || stringValue(sourceSubagent.agentNickname)
      || stringValue(payload.agent_nickname)
      || stringValue(payload.agentNickname),
    agentRole: stringValue(threadSpawn.agent_role)
      || stringValue(threadSpawn.agentRole)
      || stringValue(sourceSubagent.agent_role)
      || stringValue(sourceSubagent.agentRole)
      || stringValue(payload.agent_role)
      || stringValue(payload.agentRole)
  };
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function numberValue(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function boolValue(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return ["1", "true", "yes"].includes(value.trim().toLowerCase());
  return false;
}

function emptyRolloutDisplayIntegrity(status = "not_scanned"): RolloutDisplayIntegrity {
  return {
    status,
    responseUserMessages: 0,
    responseAssistantMessages: 0,
    visibleUserMessages: 0,
    visibleAgentMessages: 0,
    eventUserMessages: 0,
    eventAgentMessages: 0,
    parseErrors: 0
  };
}

function isInternalContextPromptBrowser(text: string): boolean {
  const prefix = text.trimStart().slice(0, 5000);
  return prefix.startsWith("# AGENTS.md instructions")
    || prefix.startsWith("<environment_context>")
    || prefix.startsWith("<turn_aborted>")
    || prefix.startsWith("<user_interruption>")
    || prefix.includes("<environment_context>")
    || prefix.includes("<permissions instructions>");
}

function isSubagentNotificationPromptBrowser(text: string): boolean {
  const prefix = text.trimStart().slice(0, 5000);
  return prefix.startsWith("<subagent_notification>")
    || (prefix.includes('"agent_path"') && prefix.includes('"status"') && prefix.toLowerCase().includes("subagent"));
}

function isAutomationPromptBrowser(text: string): boolean {
  const lowerPrefix = text.trimStart().slice(0, 5000).toLowerCase();
  return lowerPrefix.startsWith("<heartbeat>")
    || lowerPrefix.startsWith("<automation>")
    || lowerPrefix.startsWith("<scheduled_task>")
    || lowerPrefix.includes("<automation_id>")
    || (lowerPrefix.includes("<current_time_iso>") && lowerPrefix.includes("<instructions>"));
}

function isThreadDelegationPromptBrowser(text: string): boolean {
  return text.trimStart().slice(0, 5000).toLowerCase().startsWith("<codex_delegation");
}

function isCodexInternalContextPromptBrowser(text: string): boolean {
  return text.trimStart().slice(0, 5000).startsWith("<codex_internal_context");
}

function isRealUserPromptBrowser(text: string): boolean {
  return Boolean(text.trim())
    && !isInternalContextPromptBrowser(text)
    && !isSubagentNotificationPromptBrowser(text)
    && !isAutomationPromptBrowser(text)
    && !isThreadDelegationPromptBrowser(text)
    && !isCodexInternalContextPromptBrowser(text);
}

function removeEmbeddedImageBlocksBrowser(text: string): string {
  return text
    .replace(/\n?<image\b[\s\S]*?<\/image>\s*/gi, "\n")
    .replace(/\n?!\[[^\]]*]\([^)]*\)\s*/g, "\n")
    .trim();
}

function pureUserTextFromPromptBrowser(text: string): string {
  if (
    isInternalContextPromptBrowser(text)
    || isSubagentNotificationPromptBrowser(text)
    || isAutomationPromptBrowser(text)
    || isThreadDelegationPromptBrowser(text)
    || isCodexInternalContextPromptBrowser(text)
  ) {
    return "";
  }
  const cleanedText = removeEmbeddedImageBlocksBrowser(text);
  const markerMatch = /^##\s*My request for Codex:\s*$/im.exec(cleanedText);
  if (markerMatch) {
    return removeEmbeddedImageBlocksBrowser(cleanedText.slice(markerMatch.index + markerMatch[0].length)).trim();
  }
  const prefix = cleanedText.trimStart();
  if (prefix.startsWith("# In app browser:") || prefix.startsWith("# Files mentioned by the user:")) {
    return "";
  }
  return cleanedText.trim();
}

function classifyPromptRecordBrowser(
  text: string
): Pick<BrowserPromptRecord, "sourceType" | "sourceLabel" | "visibleByDefault" | "pureText" | "pureCharacterCount" | "hasPureText"> {
  const prefix = text.trimStart().slice(0, 5000);
  const pureText = pureUserTextFromPromptBrowser(text);
  if (isSubagentNotificationPromptBrowser(text)) {
    return { sourceType: "subagent", sourceLabel: "子 agent", visibleByDefault: false, pureText, pureCharacterCount: pureText.length, hasPureText: Boolean(pureText) };
  }
  if (isAutomationPromptBrowser(text)) {
    return { sourceType: "automation", sourceLabel: "自动化任务", visibleByDefault: false, pureText, pureCharacterCount: pureText.length, hasPureText: Boolean(pureText) };
  }
  if (isThreadDelegationPromptBrowser(text)) {
    return { sourceType: "delegation", sourceLabel: "线程转发", visibleByDefault: false, pureText, pureCharacterCount: pureText.length, hasPureText: Boolean(pureText) };
  }
  if (isCodexInternalContextPromptBrowser(text)) {
    return { sourceType: "goal", sourceLabel: "续跑目标上下文", visibleByDefault: false, pureText, pureCharacterCount: pureText.length, hasPureText: Boolean(pureText) };
  }
  if (isInternalContextPromptBrowser(text)) {
    return { sourceType: "internal", sourceLabel: "内部上下文", visibleByDefault: false, pureText, pureCharacterCount: pureText.length, hasPureText: Boolean(pureText) };
  }
  if (prefix.startsWith("# In app browser:")) {
    return { sourceType: "browser", sourceLabel: "浏览器上下文", visibleByDefault: true, pureText, pureCharacterCount: pureText.length, hasPureText: Boolean(pureText) };
  }
  if (prefix.startsWith("# Files mentioned by the user:")) {
    return { sourceType: "attachment", sourceLabel: "附件上下文", visibleByDefault: true, pureText, pureCharacterCount: pureText.length, hasPureText: Boolean(pureText) };
  }
  return { sourceType: "user", sourceLabel: "用户输入", visibleByDefault: true, pureText, pureCharacterCount: pureText.length, hasPureText: Boolean(pureText) };
}

function browserPromptSourceCounts(prompts: BrowserPromptRecord[]): Record<string, number> {
  return prompts.reduce<Record<string, number>>((counts, prompt) => {
    counts[prompt.sourceType] = (counts[prompt.sourceType] || 0) + 1;
    return counts;
  }, {});
}

function responseRoleMessageTextBrowser(payload: any, role: string): string {
  if (!payload || typeof payload !== "object") return "";
  if (payload.type !== "message" || payload.role !== role) return "";
  return stringifyContent(payload.content).trim();
}

function eventPayloadMessageTextBrowser(payload: any): string {
  if (!payload || typeof payload !== "object") return "";
  return stringValue(payload.message) || stringValue(payload.text);
}

function rolloutDisplayStatusFromCounts(display: RolloutDisplayIntegrity): string {
  const responseChatMessages = display.responseUserMessages + display.responseAssistantMessages;
  const visibleChatMessages = display.visibleUserMessages + display.visibleAgentMessages;
  if (responseChatMessages === 0) return display.visibleUserMessages > 0 ? "ok" : "empty";
  if (display.responseUserMessages > 0 && display.visibleUserMessages === 0) return "missing_visible_event_stream";
  if (display.responseAssistantMessages > 0 && display.visibleAgentMessages === 0) return "missing_visible_event_stream";
  if (responseChatMessages >= 10 && visibleChatMessages < Math.max(2, Math.floor(responseChatMessages / 2))) return "sparse_visible_event_stream";
  return "ok";
}

function rolloutDisplayIntegrityFromText(text: string): RolloutDisplayIntegrity {
  const display = emptyRolloutDisplayIntegrity("empty");
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const parsed = parseJsonLine(line);
    if (!parsed) {
      display.parseErrors += 1;
      continue;
    }
    const itemType = String(parsed.type || "");
    const payload = parsed.payload && typeof parsed.payload === "object" ? parsed.payload : {};
    if (itemType === "user_message") {
      const message = stringifyContent(payload);
      if (isRealUserPromptBrowser(message)) {
        display.visibleUserMessages += 1;
      }
    } else if (itemType === "event_msg") {
      const payloadType = String(payload.type || "");
      const message = eventPayloadMessageTextBrowser(payload);
      if (payloadType === "user_message" && isRealUserPromptBrowser(message)) {
        display.eventUserMessages += 1;
        display.visibleUserMessages += 1;
      } else if (payloadType === "agent_message" && message) {
        display.eventAgentMessages += 1;
        display.visibleAgentMessages += 1;
      }
    } else if (itemType === "response_item" && payload.type === "message") {
      if (isRealUserPromptBrowser(responseRoleMessageTextBrowser(payload, "user"))) {
        display.responseUserMessages += 1;
      }
      if (responseRoleMessageTextBrowser(payload, "assistant")) {
        display.responseAssistantMessages += 1;
      }
    }
  }
  display.status = rolloutDisplayStatusFromCounts(display);
  return display;
}

async function readBrowserRolloutDisplayIntegrity(threadFile: BrowserThreadFile | undefined): Promise<RolloutDisplayIntegrity> {
  if (!threadFile) return emptyRolloutDisplayIntegrity("missing_rollout_file");
  const file = await threadFile.handle.getFile();
  if (!file.size) return emptyRolloutDisplayIntegrity("missing_rollout_file");
  return rolloutDisplayIntegrityFromText(await file.text());
}

function timestampMsFromRowBrowser(row: BrowserSqlThreadRow, keyPrefix: string): number {
  const millisecondValue = numberValue(row[`${keyPrefix}_at_ms`]);
  if (millisecondValue) return millisecondValue;
  const secondValue = numberValue(row[`${keyPrefix}_at`]);
  return secondValue ? secondValue * 1000 : 0;
}

function buildProjects(threads: Array<Record<string, any>>, savedProjectPaths: string[] = [], language: BrowserLanguage = "zh") {
  const byPath = new Map<string, Record<string, any>>();
  for (const savedProjectPath of savedProjectPaths) {
    const normalizedPath = normalizePathText(savedProjectPath);
    if (!normalizedPath) continue;
    byPath.set(comparablePathText(normalizedPath), {
      path: normalizedPath,
      label: projectLabelFromPath(normalizedPath, language),
      projectKind: "workspace_project",
      total: 0,
      mainThreads: 0,
      subagentThreads: 0,
      active: 0,
      visible: 0,
      hiddenByInitialLimit: 0,
      archived: 0,
      needsRepair: 0,
      storageBytes: 0,
      emptyButHasHiddenThreads: false
    });
  }
  for (const thread of threads) {
    const path = String(thread.projectPath || "");
    const key = path ? comparablePathText(path) : "__conversation__";
    const existing = byPath.get(key) || {
      path,
      label: thread.projectLabel || projectLabelFromPath(path, language),
      projectKind: thread.projectKind || (path ? "workspace_project" : "conversation"),
      total: 0,
      mainThreads: 0,
      subagentThreads: 0,
      active: 0,
      visible: 0,
      hiddenByInitialLimit: 0,
      archived: 0,
      needsRepair: 0,
      storageBytes: 0,
      emptyButHasHiddenThreads: false
    };
    existing.total += 1;
    const isMain = thread.threadKind !== "subagent";
    if (isMain) existing.mainThreads += 1;
    if (!isMain) existing.subagentThreads += 1;
    if (isMain && !thread.archived) existing.active += 1;
    if (isMain && thread.codexVisible) existing.visible += 1;
    if (isMain && thread.visibility === "hidden_by_initial_limit") existing.hiddenByInitialLimit += 1;
    if (thread.archived) existing.archived += 1;
    if (isMain && (thread.visibility === "missing_file" || thread.visibility === "needs_user_event_repair")) existing.needsRepair += 1;
    existing.storageBytes += Number(thread.fileSizeBytes || 0);
    existing.emptyButHasHiddenThreads = existing.visible === 0 && existing.hiddenByInitialLimit > 0;
    byPath.set(key, existing);
  }
  return [...byPath.values()].sort((left, right) => right.total - left.total || String(left.label).localeCompare(String(right.label)));
}

async function inventoryResources(root: BrowserDirectoryHandle, language: BrowserLanguage) {
  const knownResources = [
    ["AGENTS.md", "AGENTS.md", "instruction", "Agent instructions"],
    ["MEMORY.md", "MEMORY.md", "memory", "Memory entrypoint"],
    ["memories", "memories", "memory", "Memory folder"],
    ["skills", "skills", "skill", "Custom skills"],
    ["config.toml", "config.toml", "config", "Codex config"],
    ["state_5.sqlite", "state_5.sqlite", "state", "Codex state database"],
    ["logs_2.sqlite", "logs_2.sqlite", "log", "Codex app logs"],
    ["session_index.jsonl", "session_index.jsonl", "state", "Session sidebar index"],
    ["version.json", "version.json", "config", "Version metadata"],
    [".codex-global-state.json", ".codex-global-state.json", "state", "Global state"],
    ["sessions", "sessions", "thread", "Session JSONL store"],
    ["archived_sessions", "archived_sessions", "thread", "Archived session JSONL store"],
    ["plugins", "plugins", "plugin", "Plugin cache"]
  ];
  const resources = [];
  for (const [relativePath, label, category, description] of knownResources) {
    resources.push(await describeResource(root, relativePath, label, category, language === "en" ? description : description));
  }
  return resources;
}

async function describeResource(root: BrowserDirectoryHandle, relativePath: string, label: string, category: string, description: string) {
  const handle = await getPathHandle(root, relativePath);
  if (!handle) {
    return {
      relativePath,
      path: relativePath,
      label: label || relativePath,
      category,
      description,
      exists: false,
      kind: "missing",
      sizeBytes: 0,
      fileCount: 0,
      directoryCount: 0,
      truncated: false,
      modifiedAtMs: null
    };
  }
  if (handle.kind === "file") {
    const file = await handle.getFile();
    return {
      relativePath,
      path: relativePath,
      label: label || relativePath,
      category,
      description,
      exists: true,
      kind: "file",
      sizeBytes: file.size,
      fileCount: 1,
      directoryCount: 0,
      truncated: false,
      modifiedAtMs: file.lastModified || null
    };
  }
  const stats = await directoryStats(handle, 250);
  return {
    relativePath,
    path: relativePath,
    label: label || relativePath,
    category,
    description,
    exists: true,
    kind: "directory",
    sizeBytes: stats.sizeBytes,
    fileCount: stats.fileCount,
    directoryCount: stats.directoryCount,
    truncated: stats.truncated,
    modifiedAtMs: null
  };
}

async function directoryStats(directory: BrowserDirectoryHandle, limit: number) {
  let sizeBytes = 0;
  let fileCount = 0;
  let directoryCount = 0;
  let truncated = false;
  async function walk(current: BrowserDirectoryHandle): Promise<void> {
    for await (const [, handle] of current.entries()) {
      if (fileCount + directoryCount >= limit) {
        truncated = true;
        return;
      }
      if (handle.kind === "file") {
        fileCount += 1;
        sizeBytes += (await handle.getFile()).size;
      } else {
        directoryCount += 1;
        await walk(handle);
      }
    }
  }
  await walk(directory);
  return { sizeBytes, fileCount, directoryCount, truncated };
}

async function scanBrowserPluginCache(root: BrowserDirectoryHandle): Promise<BrowserPluginCacheScan> {
  const curatedRoot = await getPathHandle(root, "plugins/cache/openai-curated");
  if (!curatedRoot || curatedRoot.kind !== "directory") {
    return {
      hasPluginCache: false,
      scannedRuntimeCount: 0,
      validRuntimePaths: [],
      unreadableRuntimePaths: [],
      incompleteRuntimePaths: [],
      truncated: false
    };
  }

  const scan: BrowserPluginCacheScan = {
    hasPluginCache: true,
    scannedRuntimeCount: 0,
    validRuntimePaths: [],
    unreadableRuntimePaths: [],
    incompleteRuntimePaths: [],
    truncated: false
  };
  const runtimeLimit = 220;

  for await (const [pluginName, pluginHandle] of curatedRoot.entries()) {
    if (pluginHandle.kind !== "directory") continue;
    for await (const [runtimeName, runtimeHandle] of pluginHandle.entries()) {
      if (runtimeHandle.kind !== "directory") continue;
      if (scan.scannedRuntimeCount >= runtimeLimit) {
        scan.truncated = true;
        return scan;
      }
      scan.scannedRuntimeCount += 1;
      const runtimePath = `plugins/cache/openai-curated/${pluginName}/${runtimeName}`;
      try {
        const hasManifest = await pathExists(runtimeHandle, ".codex-plugin/plugin.json");
        const skillCount = await countSkillManifests(runtimeHandle, 24);
        if (hasManifest || skillCount > 0) {
          if (scan.validRuntimePaths.length < 10) scan.validRuntimePaths.push(`${runtimePath} | skills=${skillCount}`);
        } else if (scan.incompleteRuntimePaths.length < 16) {
          scan.incompleteRuntimePaths.push(runtimePath);
        }
      } catch {
        if (scan.unreadableRuntimePaths.length < 16) scan.unreadableRuntimePaths.push(runtimePath);
      }
    }
  }
  return scan;
}

async function countSkillManifests(runtimeRoot: BrowserDirectoryHandle, limit: number): Promise<number> {
  const skillsRoot = await getPathHandle(runtimeRoot, "skills");
  if (!skillsRoot || skillsRoot.kind !== "directory") return 0;
  let count = 0;
  for await (const [, skillHandle] of skillsRoot.entries()) {
    if (skillHandle.kind !== "directory") continue;
    if (await pathExists(skillHandle, "SKILL.md")) {
      count += 1;
      if (count >= limit) return count;
    }
  }
  return count;
}

function buildBrowserDiagnostics(input: {
  codexHomeName: string;
  generatedAtMs: number;
  threadCount: number;
  resourceCount: number;
  hasSessionIndex: boolean;
  hasStateDatabase: boolean;
  stateThreadCount: number;
  stateReadError: string;
  usingStateDatabase: boolean;
  missingFileThreads: number;
  hiddenByInitialLimit: number;
  pluginCacheScan: BrowserPluginCacheScan;
  language: BrowserLanguage;
}) {
  const zh = input.language === "zh";
  const pluginCacheHasProblems = input.pluginCacheScan.unreadableRuntimePaths.length > 0 || input.pluginCacheScan.incompleteRuntimePaths.length > 0;
  const checks = [
    check("browser.folder", "core", zh ? "浏览器目录授权" : "Browser folder permission", "pass", zh ? "当前网页已获得你手动选择的 .codex 目录读取权限。" : "The page has read permission for the selected .codex folder.", [input.codexHomeName]),
    check("threads.jsonl", "threads", zh ? "线程 JSONL 扫描" : "Thread JSONL scan", input.threadCount ? "pass" : "warning", zh ? `发现 ${input.threadCount} 条 JSONL 线程记录。` : `Found ${input.threadCount} JSONL thread records.`, [`threads=${input.threadCount}`]),
    check(
      "state.sqlite",
      "state",
      zh ? "SQLite 状态库" : "SQLite state database",
      input.usingStateDatabase ? "pass" : input.hasStateDatabase ? "warning" : "warning",
      input.usingStateDatabase
        ? (zh ? `已只读解析 state_5.sqlite 中 ${input.stateThreadCount} 条线程记录。` : `Read ${input.stateThreadCount} thread rows from state_5.sqlite in read-only browser mode.`)
        : input.hasStateDatabase
          ? (zh ? "已发现 state_5.sqlite，但浏览器端无法解析；相关状态只能作为不确定项处理。" : "state_5.sqlite exists, but browser parsing failed; affected state fields are treated as unknown.")
          : (zh ? "未在所选目录中发现 state_5.sqlite。" : "state_5.sqlite was not found in the selected folder."),
      input.stateReadError ? [input.stateReadError] : [`state_threads=${input.stateThreadCount}`]
    ),
    check("session.index", "state", zh ? "旧版侧边栏索引" : "Legacy sidebar index", input.hasSessionIndex ? "pass" : "info", input.hasSessionIndex ? (zh ? "已读取 session_index.jsonl，用于旧版排序兼容和修复操作参考；新版 Codex 可见性不再依赖首轮窗口。" : "session_index.jsonl was read for legacy ordering compatibility and repair actions; current Codex visibility no longer depends on the first-page window.") : (zh ? "未发现 session_index.jsonl；新版 Codex 可见性仍可根据 SQLite/JSONL 记录判断。" : "session_index.jsonl was not found; current Codex visibility can still be judged from SQLite/JSONL records."), []),
    check(
      "browser.plugin_cache",
      "plugins",
      zh ? "浏览器插件缓存扫描" : "Browser plugin cache scan",
      !input.pluginCacheScan.hasPluginCache ? "info" : pluginCacheHasProblems ? "warning" : "pass",
      !input.pluginCacheScan.hasPluginCache
        ? (zh ? "所选目录中未发现 plugins/cache/openai-curated；无法在浏览器模式下判断插件缓存。" : "plugins/cache/openai-curated was not found in the selected folder; browser mode cannot judge plugin cache health.")
        : pluginCacheHasProblems
          ? (zh ? "插件缓存里存在无法读取或缺少入口元数据的 curated runtime 目录。" : "The plugin cache contains unreadable or metadata-incomplete curated runtime directories.")
          : (zh ? "浏览器模式可读取 curated 插件缓存中的 runtime 元数据。" : "Browser mode can read curated plugin runtime metadata."),
      [
        `scanned_runtimes=${input.pluginCacheScan.scannedRuntimeCount}`,
        `unreadable=${input.pluginCacheScan.unreadableRuntimePaths.length}`,
        `incomplete=${input.pluginCacheScan.incompleteRuntimePaths.length}`,
        `truncated=${input.pluginCacheScan.truncated}`,
        ...input.pluginCacheScan.unreadableRuntimePaths.slice(0, 6),
        ...input.pluginCacheScan.incompleteRuntimePaths.slice(0, 6)
      ]
    ),
    check("browser.limits", "runtime", zh ? "浏览器模式边界" : "Browser mode boundary", "info", zh ? "无需安装即可查看、搜索、体检、读日志和导出 prompt；SQLite 修复、迁移、瘦身、删除、MCP 和进程诊断仍需要本机连接器。" : "No-install mode supports viewing, search, diagnostics, log reading and prompt export; SQLite repair, migration, slimming, deletion, MCP and process diagnostics still need the local connector.", [])
  ];
  const issues = [];
  if (!input.threadCount) {
    issues.push(issue("browser.no_threads", "warning", "threads", zh ? "未发现线程" : "No threads found", zh ? "所选目录下没有可读取的 sessions JSONL。" : "No readable session JSONL files were found.", zh ? "确认选择的是 Codex Home 根目录，而不是其中某个子目录。" : "Select the Codex Home root folder, not one of its subfolders.", []));
  }
  if (input.missingFileThreads) {
    issues.push(issue("browser.missing_files", "warning", "threads", zh ? "存在空线程文件" : "Empty thread files found", zh ? `${input.missingFileThreads} 条线程文件为空。` : `${input.missingFileThreads} thread files are empty.`, zh ? "切到本机连接器体检后再决定是否恢复或清理。" : "Use connector diagnostics before deciding whether to restore or clean them.", []));
  }
  if (!input.hasStateDatabase) {
    issues.push(issue("browser.no_state", "warning", "state", zh ? "缺少 state_5.sqlite" : "state_5.sqlite missing", zh ? "浏览器扫描没有看到 Codex 状态库。" : "Browser scan did not see the Codex state database.", zh ? "确认目录是否完整；若要修复侧边栏状态，需要本机连接器。" : "Confirm the folder is complete; sidebar state repair needs the local connector.", []));
  }
  if (input.hasStateDatabase && !input.usingStateDatabase) {
    issues.push(issue(
      "browser.state_parse_failed",
      "warning",
      "state",
      zh ? "浏览器端 SQLite 解析失败" : "Browser SQLite parsing failed",
      zh ? "浏览器模式未能解析 state_5.sqlite，线程可见性、项目分类和子线程关系无法确认。" : "Browser mode could not parse state_5.sqlite, so visibility, project kind and child-thread relationships cannot be confirmed.",
      zh ? "优先使用本机连接器；同时把这个失败信息发给 Codex 排查 wasm/sql.js 加载或 SQLite 文件权限。" : "Prefer the local connector and pass this failure to Codex to inspect wasm/sql.js loading or SQLite file permission.",
      input.stateReadError ? [input.stateReadError] : []
    ));
  }
  if (pluginCacheHasProblems) {
    issues.push(issue(
      "browser.plugin_cache_problem",
      "warning",
      "plugins",
      zh ? "插件缓存存在可疑 runtime" : "Suspicious plugin runtime cache entries",
      zh ? "浏览器扫描发现部分 curated 插件 runtime 无法读取或缺少 plugin.json/SKILL.md 入口。" : "Browser scan found curated plugin runtimes that are unreadable or missing plugin.json/SKILL.md entrypoints.",
      zh ? "切换到本机连接器体检，确认是否需要刷新插件缓存或创建兼容 junction。" : "Switch to connector diagnostics to confirm whether the plugin cache should be refreshed or compatibility junctions should be created.",
      [
        ...input.pluginCacheScan.unreadableRuntimePaths.slice(0, 8),
        ...input.pluginCacheScan.incompleteRuntimePaths.slice(0, 8)
      ]
    ));
  }
  const score = Math.max(55, 92 - issues.filter((item) => item.severity === "warning").length * 8);
  return {
    codexHome: `browser://${input.codexHomeName}`,
    generatedAtMs: input.generatedAtMs,
    score,
    status: issues.length ? "warning" : "pass",
    summary: {
      critical: 0,
      warning: issues.filter((item) => item.severity === "warning").length,
      info: checks.filter((item) => item.status === "info").length,
      pass: checks.filter((item) => item.status === "pass").length,
      checks: checks.length,
      issues: issues.length,
      threadCount: input.threadCount
    },
    paths: { codexHome: `browser://${input.codexHomeName}` },
    codexProcesses: [],
    checks,
    issues,
    topRecommendations: [
      zh ? "直接网页扫描适合查看、搜索、导出 prompt、读取日志和轻量体检。" : "Browser scan is best for viewing, search, prompt export, log reading and lightweight diagnostics.",
      zh ? "涉及 SQLite 可见性修复、迁移、删除、瘦身、MCP 或进程诊断时，需要切换到本机连接器。" : "Switch to the local connector for SQLite visibility repair, migration, deletion, slimming, MCP or process diagnostics."
    ],
    repairHints: {},
    repairPrompt: zh
      ? "你是运行在用户自己电脑上的 Codex。用户已用 Codex Home Manager 的浏览器目录模式完成只读扫描；如需执行修复，请先在本机验证 CODEX_HOME、state_5.sqlite、session_index.jsonl 和 sessions JSONL，再做带备份的改动。"
      : "You are Codex running on the user's own machine. The user completed a read-only browser-folder scan in Codex Home Manager. Before repairing anything, verify CODEX_HOME, state_5.sqlite, session_index.jsonl and sessions JSONL locally, then make backed-up changes."
  };
}

function check(id: string, category: string, title: string, status: string, summary: string, evidence: string[]) {
  return { id, category, title, status, summary, evidence, affectedPaths: [] };
}

function issue(id: string, severity: string, category: string, title: string, summary: string, recommendation: string, evidence: string[]) {
  return { id, severity, category, title, summary, recommendation, evidence, affectedPaths: [] };
}

async function getPathHandle(root: BrowserDirectoryHandle, relativePath: string): Promise<BrowserDirectoryHandle | BrowserFileHandle | null> {
  const parts = normalizeRelativePath(relativePath).split("/").filter(Boolean);
  let current: BrowserDirectoryHandle = root;
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    const isLast = index === parts.length - 1;
    if (isLast) {
      try {
        return await current.getFileHandle(part);
      } catch {
        try {
          return await current.getDirectoryHandle(part);
        } catch {
          return null;
        }
      }
    }
    try {
      current = await current.getDirectoryHandle(part);
    } catch {
      return null;
    }
  }
  return root;
}

async function pathExists(root: BrowserDirectoryHandle, relativePath: string): Promise<boolean> {
  return Boolean(await getPathHandle(root, relativePath));
}

function normalizeRelativePath(relativePath: string): string {
  return relativePath.replaceAll("\\", "/").replace(/^\/+/, "").replace(/\/+$/, "");
}

function threadIdFromPath(relativePath: string): string {
  return relativePath.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i)?.[0] || "";
}

function titleFromPath(relativePath: string): string {
  return relativePath.split("/").pop()?.replace(/^rollout-[^-]+-[^-]+-[^-]+-[^-]+-[^-]+-/, "").replace(/\.jsonl$/i, "") || relativePath;
}

function projectLabelFromPath(projectPath: string, language: BrowserLanguage): string {
  if (!projectPath) return language === "en" ? "Conversations" : "普通对话";
  const normalized = projectPath.replaceAll("\\", "/").replace(/\/+$/, "");
  return normalized.split("/").filter(Boolean).pop() || projectPath;
}

function normalizePathText(value: unknown): string {
  if (value === null || value === undefined) return "";
  return String(value)
    .trim()
    .replace(/^\\\\\?\\/, "")
    .replace(/[\\/]+$/, "");
}

function comparablePathText(value: unknown): string {
  return normalizePathText(value)
    .replaceAll("/", "\\")
    .replace(/\\+/g, "\\")
    .toLowerCase();
}

function browserPathLabel(pathText: string): string {
  const normalized = normalizePathText(pathText).replaceAll("\\", "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts.at(-1) || pathText || "";
}

function stateValue(globalState: BrowserGlobalState, key: string, fallback: unknown): unknown {
  const direct = globalState[key];
  if (direct !== undefined && direct !== null) return direct;
  const persisted = globalState["electron-persisted-atom-state"];
  if (persisted && typeof persisted === "object" && !Array.isArray(persisted)) {
    const nested = (persisted as Record<string, unknown>)[key];
    if (nested !== undefined && nested !== null) return nested;
  }
  return fallback;
}

function stringSetFromState(globalState: BrowserGlobalState, key: string): Set<string> {
  const value = stateValue(globalState, key, []);
  return new Set(Array.isArray(value) ? value.map((item) => String(item)) : []);
}

function pinnedThreadIdsFromState(globalState: BrowserGlobalState): Set<string> {
  return stringSetFromState(globalState, "pinned-thread-ids");
}

function savedProjectPathsFromState(globalState: BrowserGlobalState): string[] {
  const value = stateValue(globalState, "electron-saved-workspace-roots", []);
  return Array.isArray(value) ? value.map(normalizePathText).filter(Boolean) : [];
}

function projectlessThreadIdsFromState(globalState: BrowserGlobalState): Set<string> {
  return stringSetFromState(globalState, "projectless-thread-ids");
}

function threadWorkspaceRootHintsFromState(globalState: BrowserGlobalState): Map<string, string> {
  const value = stateValue(globalState, "thread-workspace-root-hints", {});
  const result = new Map<string, string>();
  if (!value || typeof value !== "object" || Array.isArray(value)) return result;
  for (const [threadId, pathText] of Object.entries(value as Record<string, unknown>)) {
    result.set(threadId, normalizePathText(pathText));
  }
  return result;
}

function explicitSidebarThreadIdsFromState(globalState: BrowserGlobalState): Set<string> {
  const result = new Set(pinnedThreadIdsFromState(globalState));
  for (const threadId of threadWorkspaceRootHintsFromState(globalState).keys()) result.add(threadId);
  return result;
}

function managerHiddenThreadIdsFromState(globalState: BrowserGlobalState): Set<string> {
  const result = new Set<string>();
  for (const key of ["codex_home_manager-hidden-thread-ids", "codex-thread-manager-hidden-thread-ids"]) {
    const value = stateValue(globalState, key, []);
    if (Array.isArray(value)) value.forEach((threadId) => result.add(String(threadId)));
  }
  return result;
}

function projectLabelsFromState(globalState: BrowserGlobalState): Map<string, string> {
  const value = stateValue(globalState, "electron-workspace-root-labels", {});
  const result = new Map<string, string>();
  if (!value || typeof value !== "object" || Array.isArray(value)) return result;
  for (const [pathText, label] of Object.entries(value as Record<string, unknown>)) {
    const normalizedLabel = stringValue(label);
    if (normalizedLabel) result.set(comparablePathText(pathText), normalizedLabel);
  }
  return result;
}

function conversationRootCandidates(threadWorkspaceHints: Map<string, string>): Set<string> {
  const roots = new Set<string>();
  for (const hintPath of threadWorkspaceHints.values()) {
    if (comparablePathText(hintPath).endsWith(comparablePathText("Documents\\Codex"))) {
      roots.add(normalizePathText(hintPath));
    }
  }
  return roots;
}

function classifyProjectKindBrowser(
  projectPath: string,
  savedProjectComparables: Set<string>,
  conversationRoots: Set<string>,
  isProjectlessThread: boolean
): "workspace_project" | "conversation" | "other" {
  const comparableProjectPath = comparablePathText(projectPath);
  for (const savedProject of savedProjectComparables) {
    if (comparableProjectPath === savedProject || comparableProjectPath.startsWith(`${savedProject}\\`)) return "workspace_project";
  }
  if (isProjectlessThread) return "conversation";
  for (const root of conversationRoots) {
    const comparableRoot = comparablePathText(root);
    if (comparableProjectPath === comparableRoot || comparableProjectPath.startsWith(`${comparableRoot}\\`)) return "conversation";
  }
  return "other";
}

function projectDisplayLabelBrowser(projectPath: string, projectKind: BrowserProjectKind, projectLabels: Map<string, string>, language: BrowserLanguage): string {
  const stateLabel = projectLabels.get(comparablePathText(projectPath));
  if (stateLabel) return stateLabel;
  if (projectKind === "conversation") {
    const parts = normalizePathText(projectPath).replaceAll("\\", "/").split("/").filter(Boolean);
    const codexIndex = parts.findIndex((part) => part.toLowerCase() === "codex");
    if (codexIndex >= 0 && codexIndex + 1 < parts.length) return parts.slice(codexIndex + 1).join("/");
  }
  return browserPathLabel(projectPath) || projectLabelFromPath(projectPath, language);
}

function firstNonEmpty(...values: Array<string | null | undefined>): string {
  return values.find((value) => value && value.trim())?.trim() || "";
}

function parseJsonLine(line: string): any | null {
  try {
    return JSON.parse(line);
  } catch {
    return null;
  }
}

function firstStringByKeys(value: unknown, keys: string[]): string {
  const lowerKeys = new Set(keys.map((key) => key.toLowerCase()));
  let result = "";
  function visit(current: unknown): void {
    if (result || current === null || typeof current !== "object") return;
    if (Array.isArray(current)) {
      for (const item of current) visit(item);
      return;
    }
    for (const [key, nested] of Object.entries(current as Record<string, unknown>)) {
      if (lowerKeys.has(key.toLowerCase()) && typeof nested === "string" && nested.trim()) {
        result = nested.trim();
        return;
      }
      visit(nested);
      if (result) return;
    }
  }
  visit(value);
  return result;
}

function firstNumberByKeys(value: unknown, keys: string[]): number {
  const lowerKeys = new Set(keys.map((key) => key.toLowerCase()));
  let result = 0;
  function visit(current: unknown): void {
    if (result || current === null || typeof current !== "object") return;
    if (Array.isArray(current)) {
      for (const item of current) visit(item);
      return;
    }
    for (const [key, nested] of Object.entries(current as Record<string, unknown>)) {
      if (lowerKeys.has(key.toLowerCase()) && typeof nested === "number") {
        result = nested;
        return;
      }
      visit(nested);
      if (result) return;
    }
  }
  visit(value);
  return result;
}

function extractMessage(value: any): string {
  const candidates = [
    value?.payload?.message,
    value?.payload?.text,
    value?.payload?.content,
    value?.message,
    value?.text,
    value?.content
  ];
  for (const candidate of candidates) {
    const text = stringifyContent(candidate);
    if (text) return text;
  }
  return "";
}

function stringifyContent(content: unknown): string {
  if (!content) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map(stringifyContent).filter(Boolean).join("\n");
  if (typeof content === "object") {
    const record = content as Record<string, unknown>;
    return stringifyContent(record.text || record.content || record.value || record.message);
  }
  return "";
}

function compactText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function timestampMsFromObject(value: any): number | null {
  const timestamp = value?.timestamp || value?.payload?.timestamp || value?.time || value?.created_at || value?.updated_at;
  if (typeof timestamp === "number") return timestamp > 1_000_000_000_000 ? timestamp : timestamp * 1000;
  if (typeof timestamp === "string") {
    const parsed = Date.parse(timestamp);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function timestampFromObject(value: any): string | null {
  const timestampMs = timestampMsFromObject(value);
  return timestampMs ? new Date(timestampMs).toISOString() : null;
}

function labelFromLogObject(value: any): string {
  if (!value) return "parse_error";
  return String(value?.payload?.type || value?.type || value?.role || "event");
}

function severityFromLogObject(value: any): "info" | "warning" | "error" {
  const text = JSON.stringify(value || "").toLowerCase();
  if (text.includes("error") || text.includes("exception") || text.includes("failed")) return "error";
  if (text.includes("warning") || text.includes("warn")) return "warning";
  return "info";
}

function countBy<T>(items: T[], keyFn: (item: T) => string): Record<string, number> {
  return items.reduce<Record<string, number>>((counts, item) => {
    const key = keyFn(item);
    counts[key] = (counts[key] || 0) + 1;
    return counts;
  }, {});
}

function extractUserPromptRecords(text: string): BrowserPromptRecord[] {
  const prompts: BrowserPromptRecord[] = [];
  for (const [lineIndex, line] of text.split(/\r?\n/).entries()) {
    if (!line.trim()) continue;
    const parsed = parseJsonLine(line);
    if (!parsed) continue;
    const role = String(parsed?.payload?.role || parsed?.role || "").toLowerCase();
    const type = String(parsed?.type || parsed?.payload?.type || "").toLowerCase();
    if (role === "user" || type.includes("user")) {
      const message = extractMessage(parsed).trim();
      if (message) {
        const classification = classifyPromptRecordBrowser(message);
        prompts.push({
          index: prompts.length + 1,
          lineNumber: lineIndex + 1,
          timestamp: timestampFromObject(parsed),
          text: message,
          characterCount: message.length,
          ...classification
        });
      }
    }
  }
  return prompts;
}

function downloadText(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function isProbablyBinary(relativePath: string, file: File): boolean {
  if (file.size > 2_000_000) return true;
  return /\.(sqlite|db|png|jpg|jpeg|gif|webp|ico|exe|dll|zip|7z|pdf)$/i.test(relativePath);
}
