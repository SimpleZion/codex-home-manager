import React from "react";
import {
  BrainCircuit,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Code2,
  FileText,
  Forward,
  MessageSquareText,
  TerminalSquare,
  UserRound
} from "lucide-react";
import type { TimelineItem } from "./threadTimeline";

const MarkdownContent = React.lazy(() => import("./MarkdownContent"));

export const timelineEntryPreviewCharacterLimit = 180;

type Props = {
  item: TimelineItem;
  displayItem: TimelineItem;
  index: number;
  start: number;
  isExpanded: boolean;
  isActiveMatch: boolean;
  hideTimestamp: boolean;
  isLoadingFullItem: boolean;
  hasFullItem: boolean;
  relatedCommentaryCount: number;
  relatedCommentaryCollapsedCount: number;
  label: string;
  bodyLabel: string;
  t: (value: string) => string;
  formatCount: (value: number) => string;
  renderText: (text: string) => React.ReactNode;
  renderMarkdown: boolean;
  markdownCapable: boolean;
  searchQuery: string;
  measureElement: (element: HTMLElement | null) => void;
  resizeItem: (index: number, size: number) => void;
  onToggle: () => void;
  onToggleMarkdown: () => void;
  onToggleRelatedCommentary: (shouldExpand: boolean) => void;
  onPreviewOverflowChange?: (itemId: string, clipped: boolean) => void;
};

export function TimelineEntry({
  item, displayItem, index, start, isExpanded, isActiveMatch, hideTimestamp,
  isLoadingFullItem, hasFullItem, relatedCommentaryCount, relatedCommentaryCollapsedCount,
  label, bodyLabel, t, formatCount, renderText, renderMarkdown, markdownCapable, searchQuery, measureElement, resizeItem, onToggle, onToggleMarkdown,
  onToggleRelatedCommentary, onPreviewOverflowChange
}: Props) {
  const entryRef = React.useRef<HTMLElement | null>(null);
  const previewRef = React.useRef<HTMLParagraphElement | null>(null);
  const [previewClipped, setPreviewClipped] = React.useState(false);
  const setMeasuredElement = React.useCallback((element: HTMLElement | null) => {
    entryRef.current = element;
    measureElement(element);
  }, [measureElement]);
  const remeasure = React.useCallback(() => {
    const element = entryRef.current;
    measureElement(element);
    if (element) {
      const height = element.getBoundingClientRect().height;
      resizeItem(index, height);
    }
  }, [index, measureElement, resizeItem]);
  const previewCharacterLimit = timelineEntryPreviewCharacterLimit;
  const displayText = typeof displayItem.text === "string" ? displayItem.text : "";
  const canExpand = Boolean(
    item.encrypted
    || item.textTruncated
    || displayText.length > previewCharacterLimit
    || previewClipped
  );
  const effectiveExpanded = canExpand && isExpanded;
  const showBody = renderMarkdown || effectiveExpanded;
  const canToggleExpansion = canExpand && !renderMarkdown;
  const toggleLabel = isExpanded ? t("收起") : t("展开");
  const allRelatedCommentaryCollapsed = relatedCommentaryCount > 0 && relatedCommentaryCollapsedCount === relatedCommentaryCount;
  const relatedCommentaryToggleLabel = allRelatedCommentaryCollapsed ? t("展开后续进度") : t("折叠后续进度");
  const isNaturalLanguage = ["user", "commentary", "assistant", "assistant_unclassified", "context"].includes(item.kind);
  const isDelegation = item.promptSourceType === "delegation";
  const KindIcon = isDelegation ? Forward : item.kind === "user"
    ? UserRound
    : item.kind === "commentary"
      ? MessageSquareText
      : item.kind === "assistant" || item.kind === "assistant_unclassified"
        ? MessageSquareText
        : item.kind === "tool_call" || item.kind === "tool_output"
          ? TerminalSquare
          : CircleDot;
  React.useLayoutEffect(() => {
    setPreviewClipped(false);
  }, [displayText, item.id]);
  React.useLayoutEffect(() => {
    const preview = previewRef.current;
    if (!preview) return undefined;
    const update = () => {
      const clipped = preview.scrollWidth - preview.clientWidth > 1 || preview.scrollHeight - preview.clientHeight > 1;
      setPreviewClipped((current) => current === clipped ? current : clipped);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(preview);
    return () => observer.disconnect();
  }, [displayText, showBody]);
  React.useEffect(() => {
    onPreviewOverflowChange?.(item.id, previewClipped);
  }, [item.id, onPreviewOverflowChange, previewClipped]);
  return (
    <article
      ref={setMeasuredElement}
      data-index={index}
      data-timeline-id={item.id}
      data-prompt-source={item.promptSourceType || undefined}
      className={`timeline-entry timeline-entry-${item.kind} ${showBody ? "expanded" : "collapsed"} ${isActiveMatch ? "timeline-entry-active-match" : ""}`}
      style={{ transform: `translateY(${start}px)` }}
    >
      <div className="timeline-entry-head-row">
        <button className="timeline-entry-head" onClick={() => canToggleExpansion && onToggle()} aria-expanded={canToggleExpansion ? effectiveExpanded : undefined} aria-label={canToggleExpansion ? `${toggleLabel}${label}` : undefined} title={canToggleExpansion ? `${toggleLabel}${label}` : undefined} type="button">
          <span className="timeline-kind-marker">
            <KindIcon className="timeline-kind-icon" size={16} aria-hidden="true" />
            <span className="timeline-kind-badge">{label}</span>
          </span>
          {item.timestamp && !hideTimestamp ? <time>{new Date(item.timestamp).toLocaleString()}</time> : null}
          {item.callId ? <code>{item.callId}</code> : null}
          <em>{formatCount(item.characterCount)} {t("字符")}</em>
          {canToggleExpansion ? <span className="timeline-entry-toggle"><span>{toggleLabel}</span>{effectiveExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</span> : null}
        </button>
        {markdownCapable ? (
          <button className={`timeline-markdown-toggle ${renderMarkdown ? "active" : ""}`} onClick={onToggleMarkdown} aria-pressed={renderMarkdown} aria-label={renderMarkdown ? t("显示原文") : t("渲染 Markdown")} title={renderMarkdown ? t("显示原文") : t("渲染 Markdown")} type="button">
            {renderMarkdown ? <FileText size={14} aria-hidden="true" /> : <Code2 size={14} aria-hidden="true" />}
          </button>
        ) : null}
        {relatedCommentaryCount > 0 ? (
          <button className="timeline-related-commentary-toggle" onClick={() => onToggleRelatedCommentary(allRelatedCommentaryCollapsed)} aria-label={`${relatedCommentaryToggleLabel} ${formatCount(relatedCommentaryCount)}`} title={relatedCommentaryToggleLabel} type="button">
            <BrainCircuit size={14} aria-hidden="true" />
            <span>{relatedCommentaryToggleLabel}</span>
            <small>{formatCount(relatedCommentaryCount)}</small>
          </button>
        ) : null}
      </div>
      {showBody ? (
        <div className="timeline-entry-body">
          {item.encrypted && !displayText
            ? <p className="encrypted-reasoning-note">{t("该条推理只存储了加密内容，无法读取正文。")}</p>
            : renderMarkdown
              ? <React.Suspense fallback={<div className="timeline-readable-text" tabIndex={0} aria-label={bodyLabel}>{renderText(displayText)}</div>}><MarkdownContent text={displayText} searchQuery={searchQuery} ariaLabel={bodyLabel} onRendered={remeasure} /></React.Suspense>
            : isNaturalLanguage
              ? <div className="timeline-readable-text" tabIndex={0} aria-label={bodyLabel}>{renderText(displayText)}</div>
              : <pre tabIndex={0} aria-label={bodyLabel}>{renderText(displayText)}</pre>}
          {item.hasEncryptedContent && displayText ? <p className="encrypted-reasoning-note">{t("该记录另含加密推理正文；这里只显示可读摘要。")}</p> : null}
          {isLoadingFullItem ? <span className="timeline-loading-full">{t("正在读取完整内容...")}</span> : null}
          {item.textTruncated && !hasFullItem && !isLoadingFullItem ? <span className="timeline-loading-full">{t("当前为内容预览；折叠后重新展开可重试完整读取。")}</span> : null}
          {hasFullItem && displayItem.textTruncated ? <span className="timeline-loading-full">{t("该记录超过单次读取上限，已显示可安全读取的部分。")}</span> : null}
        </div>
      ) : displayText ? <p ref={previewRef} className="timeline-preview">{renderText(displayText.slice(0, previewCharacterLimit))}</p> : null}
    </article>
  );
}
