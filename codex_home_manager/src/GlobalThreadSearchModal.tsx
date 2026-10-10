import React from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Search, X } from "lucide-react";
import type { TimelineItem } from "./threadTimeline";
import { findSearchMatches } from "./threadContentSearch";

type SearchThread = { id: string };
type SearchProject = { path: string; label: string };
type SearchSnapshot<T extends SearchThread> = {
  codexHome: string;
  sidebarLimit: number;
  catalogVersion?: string;
  projects: SearchProject[];
  threads: T[];
};
type ThreadKind = "all" | "main" | "subagent";
type ContentKind = "conversation" | "user" | "commentary" | "assistant";
type SearchMatch = TimelineItem & {
  threadId: string;
  threadTitle: string;
  projectPath: string;
  projectLabel: string;
  threadKind: "main" | "subagent";
  archived: boolean;
  rolloutPath: string;
};
type SearchPage = {
  catalogVersion: string;
  requestId: string;
  scope: { threadCount: number };
  matchCount: number;
  matchCountComplete: boolean;
  matches: SearchMatch[];
  nextCursor?: string | null;
  index: {
    sourceFileCount: number;
    completeFileCount: number;
    sourceBytes: number;
    indexedBytes: number;
    progress: number;
    complete: boolean;
  };
};
type JsonRequest = <R>(url: string, options?: RequestInit) => Promise<R>;
type ResponseRequest = (url: string, options?: RequestInit) => Promise<Response>;

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(size >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function highlight(text: string, query: string): React.ReactNode {
  const matches = findSearchMatches(text, query);
  if (!matches.length) return text;
  const fragments: React.ReactNode[] = [];
  let cursor = 0;
  for (const match of matches) {
    if (match.start > cursor) fragments.push(text.slice(cursor, match.start));
    fragments.push(<mark key={`${match.start}-${match.end}`}>{text.slice(match.start, match.end)}</mark>);
    cursor = match.end;
  }
  if (cursor < text.length) fragments.push(text.slice(cursor));
  return fragments;
}

export function GlobalThreadSearchModal<T extends SearchThread>({
  open,
  snapshot,
  dialogRef,
  t,
  formatDate,
  fetchJson,
  fetchResponse,
  onOpenThread,
  onClose
}: {
  open: boolean;
  snapshot: SearchSnapshot<T>;
  dialogRef: React.RefObject<HTMLElement | null>;
  t: (text: string) => string;
  formatDate: (value: number | null) => string;
  fetchJson: JsonRequest;
  fetchResponse: ResponseRequest;
  onOpenThread: (thread: T, search: string) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = React.useState("");
  const [search, setSearch] = React.useState("");
  const [kind, setKind] = React.useState<ContentKind>("conversation");
  const [threadKind, setThreadKind] = React.useState<ThreadKind>("all");
  const [filterMode, setFilterMode] = React.useState<"all" | "archived">("all");
  const [projectPath, setProjectPath] = React.useState("all");
  const [page, setPage] = React.useState<SearchPage | null>(null);
  const [matches, setMatches] = React.useState<SearchMatch[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [openingResultKey, setOpeningResultKey] = React.useState("");
  const [error, setError] = React.useState("");
  const parentRef = React.useRef<HTMLDivElement>(null);
  const loadMoreSentinelRef = React.useRef<HTMLDivElement>(null);
  const loadingMoreRef = React.useRef(false);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const requestRef = React.useRef<{ id: string; controller: AbortController } | null>(null);
  const pollTimerRef = React.useRef<number | null>(null);
  const sequenceRef = React.useRef(0);
  const loadedQueryIdentityRef = React.useRef("");
  const resultsScrollOffsetRef = React.useRef(0);
  const lastOpenedResultIndexRef = React.useRef<number | null>(null);
  const restoreResultsPendingRef = React.useRef(false);
  const wasOpenRef = React.useRef(open);
  const queryIdentity = React.useMemo(() => JSON.stringify({
    codexHome: snapshot.codexHome,
    catalogVersion: snapshot.catalogVersion || "",
    search,
    kind,
    threadKind,
    filterMode,
    projectPath
  }), [filterMode, kind, projectPath, search, snapshot.catalogVersion, snapshot.codexHome, threadKind]);
  const virtualizer = useVirtualizer({
    count: matches.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 132,
    overscan: 8,
    getItemKey: (index) => `${matches[index]?.threadId || "row"}-${matches[index]?.rolloutPath || ""}-${matches[index]?.byteOffset || index}`
  });

  React.useLayoutEffect(() => {
    const wasOpen = wasOpenRef.current;
    wasOpenRef.current = open;
    if (!open) {
      if (wasOpen) restoreResultsPendingRef.current = true;
      return undefined;
    }
    if (!restoreResultsPendingRef.current || !parentRef.current) return undefined;
    restoreResultsPendingRef.current = false;
    let focusFrame = 0;
    const scrollFrame = window.requestAnimationFrame(() => {
      virtualizer.measure();
      virtualizer.scrollToOffset(resultsScrollOffsetRef.current, { align: "start" });
      focusFrame = window.requestAnimationFrame(() => {
        const resultIndex = lastOpenedResultIndexRef.current;
        if (resultIndex === null) return;
        parentRef.current?.querySelector<HTMLElement>(`[data-result-index="${resultIndex}"]`)?.focus({ preventScroll: true });
      });
    });
    return () => {
      window.cancelAnimationFrame(scrollFrame);
      if (focusFrame) window.cancelAnimationFrame(focusFrame);
    };
  }, [matches.length, open, virtualizer]);

  const cancelActiveRequest = React.useCallback(() => {
    if (pollTimerRef.current !== null) { window.clearTimeout(pollTimerRef.current); pollTimerRef.current = null; }
    const active = requestRef.current;
    if (!active) return;
    active.controller.abort();
    requestRef.current = null;
    const params = new URLSearchParams({ codex_home: snapshot.codexHome });
    void fetchResponse(`/api/search/content/requests/${encodeURIComponent(active.id)}?${params.toString()}`, { method: "DELETE" });
  }, [fetchResponse, snapshot.codexHome]);

  React.useEffect(() => () => cancelActiveRequest(), [cancelActiveRequest]);
  React.useEffect(() => {
    if (!open || search.length < 3 || snapshot.codexHome.startsWith("browser://")) return undefined;
    const queryChanged = loadedQueryIdentityRef.current !== queryIdentity;
    if (!queryChanged && page?.index.complete) return undefined;
    const sequence = ++sequenceRef.current;
    loadingMoreRef.current = false;
    setLoadingMore(false);
    if (queryChanged) {
      loadedQueryIdentityRef.current = queryIdentity;
      resultsScrollOffsetRef.current = 0;
      lastOpenedResultIndexRef.current = null;
      setMatches([]);
      setPage(null);
    }
    setError(""); cancelActiveRequest();
    const load = async () => {
      if (sequenceRef.current !== sequence) return;
      const requestId = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${sequence}`;
      const controller = new AbortController();
      requestRef.current = { id: requestId, controller };
      setLoading(true);
      const params = new URLSearchParams({
        codex_home: snapshot.codexHome, sidebarLimit: String(snapshot.sidebarLimit || 50), catalogVersion: snapshot.catalogVersion || "",
        search, kind, sourceFilter: "all", threadKind, filterMode, projectPath, limit: "60", scanBudgetMs: "450", requestId
      });
      try {
        const nextPage = await fetchJson<SearchPage>(`/api/search/content/page?${params.toString()}`, { cache: "no-store", signal: controller.signal });
        if (sequenceRef.current !== sequence) return;
        requestRef.current = null;
        setPage(nextPage); setMatches(nextPage.matches); setError("");
        if (!nextPage.index.complete) pollTimerRef.current = window.setTimeout(() => void load(), 90);
      } catch (requestError) {
        if (sequenceRef.current !== sequence || (requestError instanceof DOMException && requestError.name === "AbortError")) return;
        setError(requestError instanceof Error ? requestError.message : String(requestError));
      } finally {
        if (sequenceRef.current === sequence) setLoading(false);
      }
    };
    void load();
    return () => { sequenceRef.current += 1; cancelActiveRequest(); };
  }, [cancelActiveRequest, fetchJson, filterMode, kind, open, projectPath, queryIdentity, search, snapshot.catalogVersion, snapshot.codexHome, snapshot.sidebarLimit, threadKind]);

  const loadMore = React.useCallback(async () => {
    if (!page?.nextCursor || loadingMoreRef.current) return;
    const sequence = sequenceRef.current;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    const params = new URLSearchParams({
      codex_home: snapshot.codexHome, sidebarLimit: String(snapshot.sidebarLimit || 50), catalogVersion: page.catalogVersion || snapshot.catalogVersion || "",
      search, kind, sourceFilter: "all", threadKind, filterMode, projectPath, limit: "60", scanBudgetMs: "1", cursor: page.nextCursor
    });
    try {
      const nextPage = await fetchJson<SearchPage>(`/api/search/content/page?${params.toString()}`, { cache: "no-store" });
      if (sequenceRef.current !== sequence) return;
      setPage(nextPage); setMatches((current) => [...current, ...nextPage.matches]);
    } catch (requestError) {
      if (sequenceRef.current !== sequence) return;
      setError(requestError instanceof Error ? requestError.message : String(requestError));
    } finally {
      loadingMoreRef.current = false;
      if (sequenceRef.current === sequence) setLoadingMore(false);
    }
  }, [fetchJson, filterMode, kind, page, projectPath, search, snapshot.catalogVersion, snapshot.codexHome, snapshot.sidebarLimit, threadKind]);

  React.useEffect(() => {
    const root = parentRef.current;
    const sentinel = loadMoreSentinelRef.current;
    if (!root || !sentinel || !page?.nextCursor || typeof IntersectionObserver === "undefined") return undefined;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void loadMore();
    }, { root, rootMargin: "320px 0px" });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [loadMore, page?.index.complete, page?.nextCursor]);

  const handleResultsScroll = React.useCallback((event: React.UIEvent<HTMLDivElement>) => {
    const target = event.currentTarget;
    resultsScrollOffsetRef.current = target.scrollTop;
    if (target.scrollHeight - target.scrollTop - target.clientHeight <= 320) void loadMore();
  }, [loadMore]);

  const openMatch = React.useCallback(async (match: SearchMatch, resultIndex: number) => {
    const resultKey = `${match.threadId}-${match.rolloutPath}-${match.byteOffset}`;
    if (openingResultKey) return;
    lastOpenedResultIndexRef.current = resultIndex;
    setOpeningResultKey(resultKey); setError("");
    try {
      let thread = snapshot.threads.find((item) => item.id === match.threadId);
      if (!thread) {
        const params = new URLSearchParams({ codex_home: snapshot.codexHome, sidebar_limit: String(snapshot.sidebarLimit || 50) });
        const detail = await fetchJson<{ thread: T }>(`/api/native/threads/${encodeURIComponent(match.threadId)}?${params.toString()}`, { cache: "no-store" });
        thread = detail.thread;
      }
      onOpenThread(thread, search);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : String(requestError));
    } finally { setOpeningResultKey(""); }
  }, [fetchJson, onOpenThread, openingResultKey, search, snapshot.codexHome, snapshot.sidebarLimit, snapshot.threads]);

  if (!open) return null;
  const progress = Math.max(0, Math.min(1, page?.index.progress || 0));
  return (
    <div className="modal-backdrop global-search-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section ref={dialogRef} className="global-search-modal" role="dialog" aria-modal="true" aria-label={t("搜索所有线程内容")} tabIndex={-1}>
        <header className="modal-header"><div><span>{t("全局检索")}</span><h2>{t("搜索所有线程内容")}</h2><p>{t("覆盖主线程、子任务、归档及续接分段")}</p></div><button className="icon-button" onClick={onClose} title={t("关闭搜索窗口")} aria-label={t("关闭搜索窗口")} type="button"><X size={18} /></button></header>
        <form className="global-search-controls" onSubmit={(event) => { event.preventDefault(); const value = draft.trim(); if (value.length < 3) setError(t("请输入至少 3 个字符")); else setSearch(value); }}>
          <div className="search-box global-search-input"><Search size={18} /><input ref={inputRef} data-dialog-initial-focus aria-label={t("搜索所有线程内容")} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={t("输入至少 3 个字符，搜索全部对话正文")} /><button type="submit">{t("搜索")}</button></div>
          <div className="global-search-filter-row">
            <div className="segmented compact" aria-label={t("内容类型")}>{[["conversation", t("主要内容")], ["user", t("用户输入")], ["commentary", t("进度回复")], ["assistant", t("最终回复")]].map(([value, label]) => <button type="button" key={value} className={kind === value ? "active" : ""} onClick={() => setKind(value as ContentKind)}>{label}</button>)}</div>
            <div className="segmented compact" aria-label={t("线程类型")}>{[["all", t("全部线程")], ["main", t("主线程")], ["subagent", t("子agent")]].map(([value, label]) => <button type="button" key={value} className={threadKind === value ? "active" : ""} onClick={() => setThreadKind(value as ThreadKind)}>{label}</button>)}</div>
            <select aria-label={t("项目范围")} value={projectPath} onChange={(event) => setProjectPath(event.target.value)}><option value="all">{t("全部项目")}</option>{snapshot.projects.map((project) => <option key={project.path} value={project.path}>{project.label}</option>)}</select>
            <label className="global-search-archive"><input type="checkbox" checked={filterMode === "archived"} onChange={(event) => setFilterMode(event.target.checked ? "archived" : "all")} />{t("仅归档")}</label>
          </div>
        </form>
        <div className="global-search-status" role="status" aria-live="polite"><div><strong>{search ? (loading && !page ? t("正在并行搜索...") : page?.matchCountComplete ? `${(page.matchCount || 0).toLocaleString()} ${t("条结果")}` : `${(page?.matchCount || 0).toLocaleString()}+ ${t("条结果")}`) : t("等待搜索")}</strong><span>{page ? `${page.scope.threadCount.toLocaleString()} ${t("个线程")} · ${page.index.completeFileCount}/${page.index.sourceFileCount} ${t("个分段已完成")}` : loading ? `${snapshot.threads.length.toLocaleString()} ${t("个线程")} · ${t("Rust 零拷贝扫描")}` : t("索引按需建立，源文件不会修改")}</span></div><div className="global-search-progress"><progress max={1} value={page ? progress : undefined} /><span>{page ? `${Math.round(progress * 100)}% · ${formatBytes(page.index.indexedBytes)} / ${formatBytes(page.index.sourceBytes)}` : loading ? t("扫描中") : "0%"}</span>{loading ? <button type="button" onClick={cancelActiveRequest}>{t("暂停")}</button> : null}</div></div>
        {error ? <div className="inline-error global-search-error" role="alert">{error}</div> : null}
        <div ref={parentRef} className="global-search-results" onScroll={handleResultsScroll}>
          {!search && !error ? <div className="empty-state">{t("搜索词会在本机派生索引中匹配，不会上传线程内容。")}</div> : null}
          {search && !loading && !matches.length && !error && page?.index.complete ? <div className="empty-state">{t("没有匹配内容")}</div> : null}
          <div className="global-search-result-canvas" style={{ height: `${virtualizer.getTotalSize()}px` }}>{virtualizer.getVirtualItems().map((virtualRow) => { const match = matches[virtualRow.index]; const resultKey = `${match.threadId}-${match.rolloutPath}-${match.byteOffset}`; return <button ref={virtualizer.measureElement} data-index={virtualRow.index} data-result-index={virtualRow.index} key={virtualRow.key} type="button" className="global-search-result" style={{ transform: `translateY(${virtualRow.start}px)` }} disabled={Boolean(openingResultKey)} onClick={() => void openMatch(match, virtualRow.index)}><span className="global-search-result-head"><strong>{match.threadTitle}</strong><em>{match.projectLabel || t("未归类")} · {formatDate(match.timestampMs)}</em></span><span className="global-search-result-meta"><b>{match.label}</b><code>{match.threadId}</code>{match.archived ? <i>{t("归档")}</i> : null}{match.threadKind === "subagent" ? <i>{t("子agent")}</i> : null}{openingResultKey === resultKey ? <i>{t("正在打开")}</i> : null}</span><span className="global-search-result-text">{highlight(match.text, search)}</span></button>; })}</div>
          {page?.nextCursor ? <div ref={loadMoreSentinelRef} className="global-search-load-sentinel"><button type="button" className="global-search-more" onClick={() => void loadMore()} disabled={loadingMore}>{loadingMore ? t("正在加载...") : t("加载更多")}</button></div> : null}
        </div>
      </section>
    </div>
  );
}
