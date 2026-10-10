import * as React from "react";
import { ArrowDownToLine } from "lucide-react";

export type PromptOrder = "asc" | "desc";

export function usePromptOrdering() {
  const [promptOrder, setPromptOrder] = React.useState<PromptOrder>("asc");
  const [promptListResetNonce, setPromptListResetNonce] = React.useState(0);

  const changePromptOrder = React.useCallback((order: PromptOrder) => {
    setPromptOrder(order);
    setPromptListResetNonce((current) => current + 1);
  }, []);

  const jumpToLatestPrompt = React.useCallback(() => {
    setPromptListResetNonce((current) => current + 1);
  }, []);

  const resetPromptOrdering = React.useCallback(() => {
    setPromptOrder("asc");
    setPromptListResetNonce(0);
  }, []);

  return { promptOrder, promptListResetNonce, changePromptOrder, jumpToLatestPrompt, resetPromptOrdering };
}

export function promptRecordIdentity(record: { index: number; lineNumber?: number; byteOffset?: number; recordId?: string }): string {
  if (record.recordId) return record.recordId;
  return record.byteOffset == null
    ? `index:${record.index}:line:${record.lineNumber || 0}`
    : `byte:${record.byteOffset}`;
}

export function mergeOrderedPromptRecords<T extends { index: number; byteOffset?: number; recordId?: string; segmentOrder?: number }>(
  current: T[],
  incoming: T[],
  replace: boolean,
  order: PromptOrder,
  normalize: (record: T) => T
): T[] {
  const recordsByIdentity = new Map((replace ? [] : current).map((record) => [promptRecordIdentity(record), record]));
  for (const record of incoming) {
    const normalized = normalize(record);
    recordsByIdentity.set(promptRecordIdentity(normalized), normalized);
  }
  const position = (record: T) => record.byteOffset ?? record.index;
  return Array.from(recordsByIdentity.values()).sort((left, right) => {
    const difference = (left.segmentOrder ?? 0) - (right.segmentOrder ?? 0) || position(left) - position(right);
    return order === "desc" ? -difference : difference;
  });
}

export function buildPromptCopyText(
  prompt: { index: number; lineNumber: number; byteOffset?: number; ordinalExact?: boolean; timestamp: string | null },
  options: { rawText: string; clean: boolean; compact: boolean; sourceLabel: string; lineLabel: string; filePositionLabel: string }
): string {
  const promptText = options.compact ? options.rawText.split(/\r?\n/).filter((line) => line.trim()).join("\n").trim() : options.rawText;
  if (!promptText || options.clean) return promptText;
  const location = prompt.ordinalExact === false
    ? `${options.filePositionLabel} ${prompt.byteOffset || 0}`
    : `Prompt ${prompt.index}${prompt.lineNumber > 0 ? ` | ${options.lineLabel} ${prompt.lineNumber}` : ""}`;
  const metadata = [prompt.timestamp || "", options.sourceLabel].filter(Boolean).join(" | ");
  return `## ${location}\n\n${metadata}${metadata ? "\n\n" : ""}${promptText}`;
}

export function PromptOrderControls({
  order,
  newestLabel,
  chronologicalLabel,
  jumpLabel,
  groupLabel,
  onChange,
  onJumpLatest
}: {
  order: PromptOrder;
  newestLabel: string;
  chronologicalLabel: string;
  jumpLabel: string;
  groupLabel: string;
  onChange: (order: PromptOrder) => void;
  onJumpLatest: () => void;
}) {
  return <>
    <div className="prompt-order-control" role="group" aria-label={groupLabel}>
      <button className={order === "desc" ? "active" : ""} onClick={() => onChange("desc")} aria-pressed={order === "desc"} type="button">{newestLabel}</button>
      <button className={order === "asc" ? "active" : ""} onClick={() => onChange("asc")} aria-pressed={order === "asc"} type="button">{chronologicalLabel}</button>
    </div>
    <button className="prompt-jump-latest" onClick={onJumpLatest} title={jumpLabel} aria-label={jumpLabel} type="button"><ArrowDownToLine size={16} /><span>{jumpLabel}</span></button>
  </>;
}
