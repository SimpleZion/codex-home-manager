import { classifyPromptText } from "./promptClassification";
import { normalizeSearchText } from "./threadContentSearch";

export type TimelineKind =
  | "user"
  | "commentary"
  | "assistant"
  | "reasoning"
  | "tool_call"
  | "tool_output"
  | "developer"
  | "system"
  | "context"
  | "status";

export type TimelineFilter = "conversation" | "user" | "commentary" | "assistant" | "reasoning" | "tool" | "all";

export type TimelineItem = {
  id: string;
  byteOffset: number;
  kind: TimelineKind;
  label: string;
  text: string;
  characterCount: number;
  textTruncated: boolean;
  timestamp: string | null;
  timestampMs: number;
  sourceType: string;
  payloadType: string;
  phase: string;
  callId: string;
  readable: boolean;
  encrypted: boolean;
  hasEncryptedContent: boolean;
  promptSourceType: string;
};

export type ThreadTimeline = {
  threadId: string;
  title: string;
  rolloutPath: string;
  fileSize: number;
  beforeByte: number | null;
  nextBeforeByte: number | null;
  limit: number;
  kind: TimelineFilter;
  search: string;
  hasMore: boolean;
  scannedRecords: number;
  scannedBytes: number;
  scanLimited: boolean;
  skippedOversizedRecords: number;
  recoveredOversizedRecords?: number;
  items: TimelineItem[];
  pageCounts: Record<string, number>;
};

export function isVisibleCommentaryItem(item: TimelineItem): boolean {
  return item.kind === "commentary"
    && item.phase === "commentary"
    && ((item.sourceType === "event_msg" && item.payloadType === "agent_message")
      || (item.sourceType === "response_item" && item.payloadType === "message"));
}

const decoder = new TextDecoder("utf-8");
const conversationKinds = new Set<TimelineKind>(["user", "commentary", "assistant"]);
const inlineRecordByteLimit = 8 * 1024 * 1024;
const recoverableRecordByteLimit = 64 * 1024 * 1024;
const recoveryChunkBytes = 256 * 1024;
const recoveredStringCharacterLimit = 500_000;
const sanitizedRecordCharacterLimit = 2_000_000;

function attachmentPlaceholder(value: Record<string, unknown>): string | null {
  const contentType = String(value.type || "").trim().toLowerCase();
  if (["input_image", "output_image", "image", "image_url"].includes(contentType) || "image_url" in value) return "[图片附件]";
  if (["input_audio", "output_audio", "audio", "audio_url"].includes(contentType) || "audio_url" in value) return "[音频附件]";
  if (["input_file", "output_file", "file", "file_url"].includes(contentType)) {
    const name = String(value.filename || value.name || "").trim();
    return name ? `[文件附件：${name}]` : "[文件附件]";
  }
  if (typeof value.mimeType === "string" && "data" in value) {
    if (value.mimeType.toLowerCase().startsWith("image/")) return "[图片附件]";
    if (value.mimeType.toLowerCase().startsWith("audio/")) return "[音频附件]";
    return "[二进制附件]";
  }
  return null;
}

function safeJsonValue(value: unknown, key = ""): unknown {
  if (Array.isArray(value)) return value.map((item) => safeJsonValue(item));
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const placeholder = attachmentPlaceholder(record);
    if (placeholder) return placeholder;
    return Object.fromEntries(Object.entries(record).map(([itemKey, itemValue]) => [itemKey, safeJsonValue(itemValue, itemKey)]));
  }
  if (typeof value === "string") {
    if (["image_url", "audio_url", "video_url", "file_url"].includes(key.toLowerCase())) return "[附件内容已隐藏]";
    return redactEmbeddedDataUrls(value);
  }
  return value;
}

function redactEmbeddedDataUrls(value: string): string {
  const dataUrlStart = /data:(?:(?:image|audio|video|application|font|model|text)\/|;base64,)/i.exec(value);
  if (!dataUrlStart) return value;
  return `${value.slice(0, dataUrlStart.index)}[附件内容已隐藏]`;
}

function textFromValue(value: unknown): string {
  if (typeof value === "string") {
    const stripped = value.trim();
    if (/^[{[]/.test(stripped)) {
      try { return JSON.stringify(safeJsonValue(JSON.parse(stripped)), null, 2); } catch { /* Keep literal text. */ }
    }
    return redactEmbeddedDataUrls(stripped);
  }
  if (Array.isArray(value)) return value.map(textFromValue).filter(Boolean).join("\n").trim();
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const placeholder = attachmentPlaceholder(record);
    if (placeholder) return placeholder;
    for (const key of ["text", "message", "output", "content", "summary", "arguments", "input", "result"]) {
      if (key in record) {
        const text = textFromValue(record[key]);
        if (text) return text;
      }
    }
    return JSON.stringify(safeJsonValue(value), null, 2);
  }
  return value == null ? "" : String(value);
}

function timestampMs(value: unknown): number {
  const parsed = typeof value === "string" ? Date.parse(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : 0;
}

export function isVisibleCommentaryRecord(item: Record<string, unknown>): boolean {
  const itemType = String(item.type || "");
  const rawPayload = item.payload;
  if (!rawPayload || typeof rawPayload !== "object" || Array.isArray(rawPayload)) return false;
  const payload = rawPayload as Record<string, unknown>;
  if (payload.phase !== "commentary") return false;
  return (itemType === "event_msg" && payload.type === "agent_message")
    || (itemType === "response_item" && payload.type === "message" && payload.role === "assistant");
}

export function timelineItemFromJson(item: Record<string, unknown>, byteOffset: number): TimelineItem | null {
  const itemType = String(item.type || "");
  const rawPayload = item.payload;
  const payload = rawPayload && typeof rawPayload === "object" ? rawPayload as Record<string, unknown> : {};
  const payloadType = String(payload.type || "");
  const timestamp = typeof item.timestamp === "string" ? item.timestamp : null;
  let base: Partial<TimelineItem> | null = null;

  if (itemType === "user_message") {
    const text = textFromValue(rawPayload);
    const classification = classifyPromptText(text);
    base = {
      kind: ["user", "attachment", "browser"].includes(classification.sourceType) ? "user" : "context",
      label: classification.sourceLabel,
      text,
      promptSourceType: classification.sourceType
    };
  }
  else if (itemType === "turn_context") base = { kind: "context", label: "轮次上下文", text: textFromValue(payload) };
  else if (itemType === "compacted") base = { kind: "context", label: "上下文压缩", text: textFromValue(payload.message) };
  else if (itemType === "event_msg") {
    if (payloadType === "user_message") {
      const text = textFromValue(payload.message);
      const classification = classifyPromptText(text);
      base = { kind: ["user", "attachment", "browser"].includes(classification.sourceType) ? "user" : "context", label: classification.sourceLabel, text, promptSourceType: classification.sourceType };
    }
    else if (payloadType === "agent_message") {
      const visibleCommentary = isVisibleCommentaryRecord(item);
      const phase = String(payload.phase || "");
      base = { kind: visibleCommentary ? "commentary" : "assistant", label: visibleCommentary ? "思考过程" : "最终回复", text: textFromValue(payload.message), phase };
    }
    else if (payloadType === "agent_reasoning") base = { kind: "reasoning", label: "推理记录", text: textFromValue(payload.text), readable: true };
    else if (payloadType === "mcp_tool_call_end") {
      const invocation = payload.invocation && typeof payload.invocation === "object" ? payload.invocation as Record<string, unknown> : {};
      const toolName = [invocation.server, invocation.tool].filter(Boolean).map(String).join(".");
      base = { kind: "tool_output", label: toolName || String(payload.call_id || "MCP 工具结果"), text: textFromValue(payload.result || payload), callId: String(payload.call_id || "") };
    } else if (["tool_call", "exec_command", "command_execution", "patch_apply"].some((marker) => payloadType.includes(marker))) {
      const isOutput = ["_end", "_output", "_complete", "_completed"].some((suffix) => payloadType.endsWith(suffix));
      base = {
        kind: isOutput ? "tool_output" : "tool_call",
        label: String(payload.name || payload.call_id || payloadType || "工具活动"),
        text: textFromValue(payload) || (isOutput ? "[无文本结果]" : "[无参数]"),
        callId: String(payload.call_id || "")
      };
    } else if (["task_started", "task_complete", "turn_aborted", "context_compacted", "thread_settings_applied"].includes(payloadType)) {
      base = { kind: "status", label: payloadType, text: textFromValue(payload) };
    }
  } else if (itemType === "response_item") {
    const role = String(payload.role || "") as TimelineKind;
    if (payloadType === "message" && ["user", "assistant", "developer", "system"].includes(role)) {
      const labels: Record<string, string> = { user: "用户", assistant: "助手回复", developer: "开发者上下文", system: "系统上下文" };
      const text = textFromValue(payload.content);
      const classification = role === "user" ? classifyPromptText(text) : null;
      const phase = String(payload.phase || "");
      const visibleCommentary = isVisibleCommentaryRecord(item);
      base = {
        kind: visibleCommentary ? "commentary" : (role === "user" && classification && !["user", "attachment", "browser"].includes(classification.sourceType) ? "context" : role),
        label: classification?.sourceLabel || (role === "assistant" ? (visibleCommentary ? "思考过程" : "最终回复") : labels[role]),
        text,
        phase,
        promptSourceType: classification?.sourceType || ""
      };
    } else if (payloadType === "reasoning") {
      const readableText = textFromValue(payload.summary || payload.content);
      const hasEncryptedContent = Boolean(payload.encrypted_content);
      const encrypted = hasEncryptedContent && !readableText;
      base = { kind: "reasoning", label: "推理记录", text: readableText, readable: Boolean(readableText), encrypted, hasEncryptedContent };
    } else if (["function_call", "custom_tool_call", "tool_search_call"].includes(payloadType)) {
      const callInput = payload.arguments || payload.input || (payloadType === "tool_search_call" ? payload.query || payload : null);
      base = { kind: "tool_call", label: String(payload.name || payload.call_id || "工具调用"), text: textFromValue(callInput) || "[无参数]", callId: String(payload.call_id || "") };
    } else if (["function_call_output", "custom_tool_call_output", "tool_search_output"].includes(payloadType)) {
      base = { kind: "tool_output", label: String(payload.call_id || "工具结果"), text: textFromValue(payload.output || payload.content || payload.tools || payload) || "[无文本结果]", callId: String(payload.call_id || "") };
    }
  }
  if (!base?.kind) return null;
  const text = String(base.text || "");
  return {
    id: `byte-${byteOffset}`,
    byteOffset,
    kind: base.kind,
    label: String(base.label || base.kind),
    text,
    characterCount: text.length,
    textTruncated: false,
    timestamp,
    timestampMs: timestampMs(timestamp),
    sourceType: itemType,
    payloadType,
    phase: String(base.phase || ""),
    callId: String(base.callId || ""),
    readable: base.readable !== false,
    encrypted: Boolean(base.encrypted),
    hasEncryptedContent: Boolean(base.hasEncryptedContent || base.encrypted),
    promptSourceType: String(base.promptSourceType || "")
  };
}

function matches(item: TimelineItem, kind: TimelineFilter, search: string): boolean {
  if (item.kind === "commentary" && !isVisibleCommentaryItem(item)) return false;
  const kindMatches = kind === "all"
    || (kind === "conversation" && conversationKinds.has(item.kind))
    || (kind === "tool" && (item.kind === "tool_call" || item.kind === "tool_output"))
    || item.kind === kind;
  if (!kindMatches) return false;
  const marker = normalizeSearchText(search.trim());
  return !marker || normalizeSearchText([item.kind, item.label, item.text, item.callId].join("\n")).includes(marker);
}

function duplicate(item: TimelineItem, seen: Map<string, Array<[number, string, string, number]>>): boolean {
  const text = item.text.trim();
  const prior = seen.get(item.kind) || [];
  const repeated = prior.some(([offset, priorText, priorSourceType, priorTimestampMs]) => {
    if (priorSourceType === item.sourceType) return false;
    const nearInTime = item.timestampMs > 0 && priorTimestampMs > 0 && Math.abs(item.timestampMs - priorTimestampMs) <= 2_000;
    const nearInFile = !item.timestampMs && !priorTimestampMs && Math.abs(offset - item.byteOffset) < 1_000_000;
    if (!nearInTime && !nearInFile) return false;
    if (!text || !priorText) return !text && !priorText;
    if (text === priorText) return true;
    return ["assistant", "reasoning"].includes(item.kind)
      && (text.startsWith(priorText) || priorText.startsWith(text));
  });
  if (!repeated) {
    prior.push([item.byteOffset, text, item.sourceType, item.timestampMs]);
    if (prior.length > 256) prior.splice(0, prior.length - 256);
    seen.set(item.kind, prior);
  }
  return repeated;
}

function decodeJsonStringPrefix(rawValue: string): string {
  for (let trimCount = 0; trimCount <= 6 && trimCount <= rawValue.length; trimCount += 1) {
    try {
      return JSON.parse(`"${rawValue.slice(0, rawValue.length - trimCount)}"`) as string;
    } catch {
      // A bounded suffix trim avoids ending inside an escape sequence.
    }
  }
  return "";
}

async function sanitizedOversizedRecord(
  file: File,
  lineStart: number,
  lineEnd: number,
  signal?: AbortSignal
): Promise<{ parsed: Record<string, unknown>; textTruncated: boolean } | null> {
  if (lineEnd - lineStart > recoverableRecordByteLimit) return null;
  const localDecoder = new TextDecoder("utf-8");
  const outputParts: string[] = [];
  let outputCharacters = 0;
  let inString = false;
  let escaped = false;
  let stringPrefix = "";
  let stringTruncated = false;
  let recordTextTruncated = false;

  const append = (value: string): boolean => {
    if (outputCharacters + value.length > sanitizedRecordCharacterLimit) return false;
    outputParts.push(value);
    outputCharacters += value.length;
    return true;
  };
  const captureStringCharacter = (value: string): void => {
    if (stringPrefix.length + value.length <= recoveredStringCharacterLimit) stringPrefix += value;
    else stringTruncated = true;
  };

  for (let position = lineStart; position < lineEnd; position += recoveryChunkBytes) {
    signal?.throwIfAborted();
    const chunkEnd = Math.min(lineEnd, position + recoveryChunkBytes);
    const chunk = localDecoder.decode(await file.slice(position, chunkEnd).arrayBuffer(), { stream: chunkEnd < lineEnd });
    for (const character of chunk) {
      if (!inString) {
        if (character === '"') {
          inString = true;
          escaped = false;
          stringPrefix = "";
          stringTruncated = false;
        } else if (!append(character)) {
          return null;
        }
        continue;
      }
      if (escaped) {
        captureStringCharacter(character);
        escaped = false;
        continue;
      }
      if (character === "\\") {
        captureStringCharacter(character);
        escaped = true;
        continue;
      }
      if (character !== '"') {
        captureStringCharacter(character);
        continue;
      }

      const containsEmbeddedData = /data:[^,\s]{1,200},/i.test(stringPrefix);
      let safeString: string;
      if (containsEmbeddedData) {
        safeString = "[附件内容已隐藏]";
      } else if (stringTruncated) {
        safeString = `${decodeJsonStringPrefix(stringPrefix)}\n[超长文本已截断]`;
        recordTextTruncated = true;
      } else {
        safeString = decodeJsonStringPrefix(stringPrefix);
      }
      if (!append(JSON.stringify(safeString))) return null;
      inString = false;
    }
  }
  if (inString) return null;
  try {
    const parsed = JSON.parse(outputParts.join("")) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? { parsed: parsed as Record<string, unknown>, textTruncated: recordTextTruncated }
      : null;
  } catch {
    return null;
  }
}

async function* reverseJsonlLines(
  file: File,
  beforeByte?: number | null,
  signal?: AbortSignal,
  maxLineBytes = inlineRecordByteLimit
): AsyncGenerator<[number, number, string | null, boolean]> {
  let position = beforeByte == null ? file.size : Math.max(0, Math.min(beforeByte, file.size));
  if (position > 0) {
    const tail = new Uint8Array(await file.slice(position - 1, position).arrayBuffer());
    if (tail[0] === 10) position -= 1;
  }
  let lineEnd = position;
  let retainedFragments: Uint8Array[] = [];
  let retainedBytes = 0;
  let crossedInlineLimit = false;

  const prependFragment = (fragment: Uint8Array) => {
    if (fragment.length === 0 || crossedInlineLimit) return;
    retainedFragments.unshift(fragment);
    retainedBytes += fragment.length;
    if (retainedBytes > maxLineBytes) {
      retainedFragments = [];
      retainedBytes = 0;
      crossedInlineLimit = true;
    }
  };
  const decodeRetained = () => {
    if (crossedInlineLimit) return null;
    if (retainedFragments.length === 1) return decoder.decode(retainedFragments[0]);
    const combined = new Uint8Array(retainedBytes);
    let offset = 0;
    for (const fragment of retainedFragments) {
      combined.set(fragment, offset);
      offset += fragment.length;
    }
    return decoder.decode(combined);
  };
  const resetLine = (nextLineEnd: number) => {
    lineEnd = nextLineEnd;
    retainedFragments = [];
    retainedBytes = 0;
    crossedInlineLimit = false;
  };

  while (position > 0) {
    signal?.throwIfAborted();
    const blockStart = Math.max(0, position - recoveryChunkBytes, lineEnd - recoverableRecordByteLimit);
    const bytes = new Uint8Array(await file.slice(blockStart, position).arrayBuffer());
    let segmentEnd = bytes.length;
    for (let index = bytes.length - 1; index >= 0; index -= 1) {
      if (bytes[index] !== 10) continue;
      prependFragment(bytes.subarray(index + 1, segmentEnd));
      const lineStart = blockStart + index + 1;
      yield [lineStart, lineEnd, decodeRetained(), true];
      resetLine(blockStart + index);
      segmentEnd = index;
    }
    prependFragment(bytes.subarray(0, segmentEnd));
    position = blockStart;
    if (lineEnd - blockStart >= recoverableRecordByteLimit && blockStart > 0) {
      yield [blockStart, lineEnd, null, false];
      resetLine(blockStart);
      break;
    }
  }
  if (lineEnd > 0 && position === 0) yield [0, lineEnd, decodeRetained(), true];
}

export async function readBrowserTimelinePage(
  file: File,
  threadId: string,
  title: string,
  rolloutPath: string,
  options: { beforeByte?: number | null; limit?: number; kind?: TimelineFilter; search?: string; contentLimit?: number; signal?: AbortSignal } = {}
): Promise<ThreadTimeline> {
  const limit = Math.max(1, Math.min(200, options.limit || 80));
  const kind = options.kind || "conversation";
  const search = options.search || "";
  const contentLimit = Math.max(2_000, Math.min(500_000, options.contentLimit || 120_000));
  const newest: TimelineItem[] = [];
  const seen = new Map<string, Array<[number, string, string, number]>>();
  let scannedRecords = 0;
  let scannedBytes = 0;
  let scanLimited = false;
  let nextBeforeByte: number | null = null;
  let hasMore = false;
  let skippedOversizedRecords = 0;
  let recoveredOversizedRecords = 0;
  for await (const [byteOffset, lineEnd, rawLine, completeRecord] of reverseJsonlLines(file, options.beforeByte, options.signal)) {
    options.signal?.throwIfAborted();
    scannedRecords += 1;
    const scanOrigin = options.beforeByte == null ? file.size : Math.max(0, Math.min(options.beforeByte, file.size));
    scannedBytes = Math.max(scannedBytes, scanOrigin - byteOffset);
    if (scannedRecords > 10_000 || (scannedRecords > 1 && scannedBytes > 32 * 1024 * 1024)) {
      hasMore = true; scanLimited = true; nextBeforeByte = lineEnd; break;
    }
    let parsed: Record<string, unknown>;
    let recoveredTextTruncated = false;
    if (rawLine === null) {
      const recovered = completeRecord
        ? await sanitizedOversizedRecord(file, byteOffset, lineEnd, options.signal)
        : null;
      if (!recovered) {
        skippedOversizedRecords += 1;
        hasMore = byteOffset > 0;
        scanLimited = hasMore;
        nextBeforeByte = hasMore ? byteOffset : null;
        break;
      }
      parsed = recovered.parsed;
      recoveredTextTruncated = recovered.textTruncated;
    } else {
      try { parsed = JSON.parse(rawLine) as Record<string, unknown>; } catch { continue; }
    }
    if (scannedRecords % 250 === 0) await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 0));
    const item = timelineItemFromJson(parsed, byteOffset);
    if (!item || !matches(item, kind, search) || (!item.text && item.kind !== "reasoning") || duplicate(item, seen)) continue;
    if (newest.length >= limit) { hasMore = true; nextBeforeByte = lineEnd; break; }
    if (rawLine === null) {
      recoveredOversizedRecords += 1;
      item.textTruncated = recoveredTextTruncated;
    }
    if (item.text.length > contentLimit) {
      item.text = item.text.slice(0, contentLimit);
      item.textTruncated = true;
    }
    newest.push(item);
  }
  if (hasMore && nextBeforeByte == null) nextBeforeByte = newest.at(-1)?.byteOffset ?? null;
  const items = newest.reverse();
  return {
    threadId, title, rolloutPath, fileSize: file.size,
    beforeByte: options.beforeByte ?? null, nextBeforeByte: hasMore ? nextBeforeByte : null,
    limit, kind, search, hasMore, scannedRecords, scannedBytes, scanLimited, skippedOversizedRecords, recoveredOversizedRecords, items,
    pageCounts: items.reduce<Record<string, number>>((counts, item) => ({ ...counts, [item.kind]: (counts[item.kind] || 0) + 1 }), {})
  };
}

export async function scanBrowserTimelineSearchPages(
  readPage: (beforeByte: number | null) => Promise<ThreadTimeline>,
  options: {
    beforeByte?: number | null;
    onProgress?: (timeline: ThreadTimeline) => void;
    maxItems?: number;
    signal?: AbortSignal;
  } = {}
): Promise<ThreadTimeline> {
  const initialBeforeByte = options.beforeByte ?? null;
  const maxItems = Math.max(1, Math.min(200, options.maxItems || 80));
  let beforeByte = initialBeforeByte;
  let accumulatedItems: TimelineItem[] = [];
  let scannedRecords = 0;
  let scannedBytes = 0;
  let skippedOversizedRecords = 0;
  let recoveredOversizedRecords = 0;

  while (true) {
    options.signal?.throwIfAborted();
    const page = await readPage(beforeByte);
    options.signal?.throwIfAborted();
    const seenIds = new Set(accumulatedItems.map((item) => item.id));
    const pageItems = page.items.filter((item) => !seenIds.has(item.id));
    if (accumulatedItems.length && accumulatedItems.length + pageItems.length > maxItems) {
      return {
        ...page,
        beforeByte: initialBeforeByte,
        nextBeforeByte: beforeByte,
        hasMore: true,
        scanLimited: true,
        scannedRecords,
        scannedBytes,
        skippedOversizedRecords,
        recoveredOversizedRecords,
        items: accumulatedItems,
        pageCounts: accumulatedItems.reduce<Record<string, number>>((counts, item) => {
          counts[item.kind] = (counts[item.kind] || 0) + 1;
          return counts;
        }, {})
      };
    }
    accumulatedItems = [...pageItems, ...accumulatedItems];
    scannedRecords += page.scannedRecords;
    scannedBytes += page.scannedBytes;
    skippedOversizedRecords += page.skippedOversizedRecords;
    recoveredOversizedRecords += page.recoveredOversizedRecords || 0;
    const accumulated: ThreadTimeline = {
      ...page,
      beforeByte: initialBeforeByte,
      scannedRecords,
      scannedBytes,
      skippedOversizedRecords,
      recoveredOversizedRecords,
      items: accumulatedItems,
      pageCounts: accumulatedItems.reduce<Record<string, number>>((counts, item) => {
        counts[item.kind] = (counts[item.kind] || 0) + 1;
        return counts;
      }, {})
    };
    options.onProgress?.(accumulated);

    if (accumulatedItems.length >= maxItems) return accumulated;

    const nextBeforeByte = page.nextBeforeByte;
    const scanBoundary = beforeByte ?? page.fileSize;
    if (!page.scanLimited || !page.hasMore || nextBeforeByte === null || nextBeforeByte >= scanBoundary) return accumulated;
    beforeByte = nextBeforeByte;
  }
}

export async function readBrowserTimelineItem(file: File, byteOffset: number, signal?: AbortSignal): Promise<TimelineItem> {
  let lineEnd = file.size;
  const maxRawRecordBytes = recoverableRecordByteLimit;
  for (let position = byteOffset; position < file.size; position += 1_048_576) {
    signal?.throwIfAborted();
    if (position - byteOffset >= maxRawRecordBytes) throw new Error("The selected JSONL record exceeds the 64 MB safety limit.");
    const end = Math.min(file.size, position + 1_048_576);
    const bytes = new Uint8Array(await file.slice(position, end).arrayBuffer());
    const newlineIndex = bytes.indexOf(10);
    if (newlineIndex >= 0) { lineEnd = position + newlineIndex; break; }
  }
  const recovered = lineEnd - byteOffset > inlineRecordByteLimit
    ? await sanitizedOversizedRecord(file, byteOffset, lineEnd, signal)
    : null;
  if (lineEnd - byteOffset > inlineRecordByteLimit && !recovered) {
    throw new Error("The selected JSONL record could not be recovered within the safe parsing budget.");
  }
  const parsed = recovered?.parsed || JSON.parse(await file.slice(byteOffset, lineEnd).text()) as Record<string, unknown>;
  const item = timelineItemFromJson(parsed, byteOffset);
  if (!item) throw new Error("The selected JSONL record is not a displayable timeline item.");
  item.textTruncated = Boolean(recovered?.textTruncated);
  if (item.text.length > 5_000_000) {
    item.text = item.text.slice(0, 5_000_000);
    item.textTruncated = true;
  }
  return item;
}
