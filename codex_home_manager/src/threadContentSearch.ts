import type { TimelineFilter, TimelineItem } from "./threadTimeline";

export type SearchMatch = {
  start: number;
  end: number;
};

export type ThreadTimelineSearchPage = {
  threadId: string;
  requestId: string;
  kind: TimelineFilter;
  search: string;
  matchCount: number;
  matchCountComplete: boolean;
  matches: TimelineItem[];
  nextCursor?: string | null;
  hasMore: boolean;
  index: {
    complete: boolean;
    scannedBytes?: number;
    scannedLines?: number;
    fileSize?: number;
    elapsedMs?: number;
    [key: string]: unknown;
  };
};

export type ThreadTimelineSearchRequest = {
  threadId: string;
  codexHome: string;
  kind: TimelineFilter;
  search: string;
  cursor?: string | null;
  limit: number;
  scanBudgetMs: number;
  requestId: string;
};

const searchSegmenter = new Intl.Segmenter("und", { granularity: "grapheme" });
export const maxSearchMatchesPerText = 200;

function foldGrapheme(segment: string): string {
  return segment
    .normalize("NFKD")
    .toLocaleLowerCase("und")
    .replace(/ß/g, "ss")
    .replace(/ς/g, "σ")
    .replace(/\u0345/g, "ι")
    .replace(/\p{M}/gu, "");
}

export function normalizeSearchText(text: string): string {
  return foldGrapheme(text);
}

export function findSearchMatches(
  text: string,
  query: string,
  matchLimit = maxSearchMatchesPerText
): SearchMatch[] {
  const normalizedQuery = normalizeSearchText(query.trim());
  const boundedMatchLimit = Math.max(0, Math.min(maxSearchMatchesPerText, Math.floor(matchLimit)));
  if (!normalizedQuery || boundedMatchLimit === 0) return [];

  const ranges: Array<{
    normalizedStart: number;
    normalizedEnd: number;
    originalStart: number;
    originalEnd: number;
  }> = [];
  const normalizedChunks: string[] = [];
  let normalizedLength = 0;

  for (const { segment, index } of searchSegmenter.segment(text)) {
    const normalizedSegment = foldGrapheme(segment);
    if (!normalizedSegment) continue;
    const normalizedStart = normalizedLength;
    normalizedChunks.push(normalizedSegment);
    normalizedLength += normalizedSegment.length;
    ranges.push({
      normalizedStart,
      normalizedEnd: normalizedLength,
      originalStart: index,
      originalEnd: index + segment.length
    });
  }
  const normalizedText = normalizedChunks.join("");

  const matches: SearchMatch[] = [];
  let firstRangeIndex = 0;
  let lastRangeIndex = 0;
  let searchFrom = 0;
  let normalizedMatchStart = normalizedText.indexOf(normalizedQuery, searchFrom);
  while (normalizedMatchStart >= 0 && matches.length < boundedMatchLimit) {
    const normalizedMatchEnd = normalizedMatchStart + normalizedQuery.length;
    while (firstRangeIndex < ranges.length && ranges[firstRangeIndex].normalizedEnd <= normalizedMatchStart) firstRangeIndex += 1;
    if (lastRangeIndex < firstRangeIndex) lastRangeIndex = firstRangeIndex;
    while (lastRangeIndex + 1 < ranges.length && ranges[lastRangeIndex + 1].normalizedStart < normalizedMatchEnd) lastRangeIndex += 1;
    const firstRange = ranges[firstRangeIndex];
    const lastRange = ranges[lastRangeIndex];
    if (firstRange && lastRange) {
      const match = { start: firstRange.originalStart, end: lastRange.originalEnd };
      const previous = matches.at(-1);
      if (!previous || previous.start !== match.start || previous.end !== match.end) matches.push(match);
    }
    searchFrom = normalizedMatchEnd;
    normalizedMatchStart = normalizedText.indexOf(normalizedQuery, searchFrom);
  }
  return matches;
}

export function createThreadTimelineSearchPagePath(request: ThreadTimelineSearchRequest): string {
  const params = new URLSearchParams({
    codex_home: request.codexHome,
    kind: request.kind,
    search: request.search,
    limit: String(request.limit),
    scanBudgetMs: String(request.scanBudgetMs),
    requestId: request.requestId
  });
  if (request.cursor) params.set("cursor", request.cursor);
  return `/api/threads/${encodeURIComponent(request.threadId)}/timeline/search/page?${params.toString()}`;
}

export function createThreadTimelineSearchCancelPath(threadId: string, requestId: string, codexHome: string): string {
  const params = new URLSearchParams({ codex_home: codexHome });
  return `/api/threads/${encodeURIComponent(threadId)}/timeline/search/requests/${encodeURIComponent(requestId)}?${params.toString()}`;
}
