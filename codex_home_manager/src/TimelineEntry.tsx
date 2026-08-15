import React from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { TimelineItem } from "./threadTimeline";

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
  label: string;
  bodyLabel: string;
  t: (value: string) => string;
  formatCount: (value: number) => string;
  renderText: (text: string) => React.ReactNode;
  measureElement: (element: HTMLElement | null) => void;
  onToggle: () => void;
};

export function TimelineEntry({
  item, displayItem, index, start, isExpanded, isActiveMatch, hideTimestamp,
  isLoadingFullItem, hasFullItem, label, bodyLabel, t, formatCount, renderText,
  measureElement, onToggle
}: Props) {
  const canExpand = Boolean(displayItem.text || item.encrypted);
  const isNaturalLanguage = ["user", "commentary", "assistant", "context"].includes(item.kind);
  return (
    <article
      ref={measureElement}
      data-index={index}
      className={`timeline-entry timeline-entry-${item.kind} ${isExpanded ? "expanded" : "collapsed"} ${isActiveMatch ? "timeline-entry-active-match" : ""}`}
      style={{ transform: `translateY(${start}px)` }}
    >
      <button className="timeline-entry-head" onClick={() => canExpand && onToggle()} aria-expanded={isExpanded} type="button">
        <span className="timeline-kind-badge">{label}</span>
        {item.timestamp && !hideTimestamp ? <time>{new Date(item.timestamp).toLocaleString()}</time> : null}
        {item.callId ? <code>{item.callId}</code> : null}
        <em>{formatCount(item.characterCount)} {t("字符")}</em>
        {canExpand ? isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} /> : null}
      </button>
      {isExpanded ? (
        <div className="timeline-entry-body">
          {item.encrypted && !displayItem.text
            ? <p className="encrypted-reasoning-note">{t("该条推理只存储了加密内容，无法读取正文。")}</p>
            : isNaturalLanguage
              ? <div className="timeline-readable-text" tabIndex={0} aria-label={bodyLabel}>{renderText(displayItem.text)}</div>
              : <pre tabIndex={0} aria-label={bodyLabel}>{renderText(displayItem.text)}</pre>}
          {item.hasEncryptedContent && displayItem.text ? <p className="encrypted-reasoning-note">{t("该记录另含加密推理正文；这里只显示可读摘要。")}</p> : null}
          {isLoadingFullItem ? <span className="timeline-loading-full">{t("正在读取完整内容...")}</span> : null}
          {item.textTruncated && !hasFullItem && !isLoadingFullItem ? <span className="timeline-loading-full">{t("当前为内容预览；折叠后重新展开可重试完整读取。")}</span> : null}
          {hasFullItem && displayItem.textTruncated ? <span className="timeline-loading-full">{t("该记录超过单次读取上限，已显示可安全读取的部分。")}</span> : null}
        </div>
      ) : displayItem.text ? <p className="timeline-preview">{renderText(displayItem.text.slice(0, 180))}</p> : null}
    </article>
  );
}
