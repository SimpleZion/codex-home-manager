import React from "react";

type ThreadPageLike<T extends { id: string }> = {
  threads: T[];
  hasMore: boolean;
  nextCursor: string | null;
};

export async function loadThreadPagesToDepth<T extends { id: string }, P extends ThreadPageLike<T>>(
  queryString: string,
  targetThreadCount: number,
  loadPage: (queryString: string) => Promise<P>
): Promise<{ page: P | null; threads: T[] }> {
  const collectedThreads: T[] = [];
  const knownIds = new Set<string>();
  let cursor: string | null = null;
  let page: P | null = null;
  do {
    const params = new URLSearchParams(queryString);
    if (cursor) params.set("cursor", cursor);
    page = await loadPage(params.toString());
    for (const thread of page.threads) {
      if (knownIds.has(thread.id)) continue;
      knownIds.add(thread.id);
      collectedThreads.push(thread);
    }
    cursor = page.hasMore ? page.nextCursor : null;
  } while (cursor && collectedThreads.length < targetThreadCount);
  return { page, threads: collectedThreads };
}

export function useThreadScrollAnchor<T extends { id: string }>(scrollContextKey: string, threads: T[]) {
  const scrollElementRef = React.useRef<HTMLDivElement>(null);
  const anchorRef = React.useRef<{ threadId: string; offset: number } | null>(null);
  const previousContextRef = React.useRef(scrollContextKey);
  const capture = React.useCallback(() => {
    const container = scrollElementRef.current;
    if (!container) return;
    const containerTop = container.getBoundingClientRect().top;
    const rows = Array.from(container.querySelectorAll<HTMLTableRowElement>("tbody tr[data-thread-id]"));
    const row = rows.find((candidate) => candidate.getBoundingClientRect().bottom > containerTop + 1) || rows[0];
    anchorRef.current = row?.dataset.threadId
      ? { threadId: row.dataset.threadId, offset: row.getBoundingClientRect().top - containerTop }
      : null;
  }, []);

  React.useLayoutEffect(() => {
    const container = scrollElementRef.current;
    if (!container) return;
    if (previousContextRef.current !== scrollContextKey) {
      previousContextRef.current = scrollContextKey;
      anchorRef.current = null;
      container.scrollTop = 0;
      return;
    }
    const anchor = anchorRef.current;
    if (!anchor) return;
    const row = Array.from(container.querySelectorAll<HTMLTableRowElement>("tbody tr[data-thread-id]"))
      .find((candidate) => candidate.dataset.threadId === anchor.threadId);
    if (!row) return;
    container.scrollTop += row.getBoundingClientRect().top - container.getBoundingClientRect().top - anchor.offset;
    capture();
  }, [capture, scrollContextKey, threads]);

  return { scrollElementRef, captureScrollAnchor: capture };
}

export function useDismissOpenDetailsOutside(
  detailsRef: React.RefObject<HTMLDetailsElement | null>,
  enabled: boolean
) {
  React.useEffect(() => {
    if (!enabled) return undefined;
    const dismiss = (event: PointerEvent) => {
      const details = detailsRef.current;
      if (!details?.open || !(event.target instanceof Node) || details.contains(event.target)) return;
      details.open = false;
    };
    document.addEventListener("pointerdown", dismiss, true);
    return () => document.removeEventListener("pointerdown", dismiss, true);
  }, [detailsRef, enabled]);
}
