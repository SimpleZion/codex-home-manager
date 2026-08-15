import * as React from "react";
import { ArrowDownToLine } from "lucide-react";

export type PromptOrder = "asc" | "desc";

export function usePromptOrdering() {
  const [promptOrder, setPromptOrder] = React.useState<PromptOrder>("desc");
  const [promptListResetNonce, setPromptListResetNonce] = React.useState(0);

  const changePromptOrder = React.useCallback((order: PromptOrder) => {
    setPromptOrder(order);
    setPromptListResetNonce((current) => current + 1);
  }, []);

  const jumpToLatestPrompt = React.useCallback(() => {
    setPromptOrder("desc");
    setPromptListResetNonce((current) => current + 1);
  }, []);

  const resetPromptOrdering = React.useCallback(() => {
    setPromptOrder("desc");
    setPromptListResetNonce(0);
  }, []);

  return { promptOrder, promptListResetNonce, changePromptOrder, jumpToLatestPrompt, resetPromptOrdering };
}

export function mergeOrderedPromptRecords<T extends { index: number; byteOffset?: number }>(
  current: T[],
  incoming: T[],
  replace: boolean,
  order: PromptOrder,
  normalize: (record: T) => T
): T[] {
  const identity = (record: T) => record.byteOffset == null ? `index:${record.index}` : `byte:${record.byteOffset}`;
  const recordsByIdentity = new Map((replace ? [] : current).map((record) => [identity(record), record]));
  for (const record of incoming) {
    const normalized = normalize(record);
    recordsByIdentity.set(identity(normalized), normalized);
  }
  const position = (record: T) => record.byteOffset ?? record.index;
  return Array.from(recordsByIdentity.values()).sort((left, right) => (
    order === "desc" ? position(right) - position(left) : position(left) - position(right)
  ));
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
