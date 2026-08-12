export type PromptClassification = {
  sourceType: string;
  sourceLabel: string;
  visibleByDefault: boolean;
  pureText: string;
  pureCharacterCount: number;
  hasPureText: boolean;
};

const runtimeContextStartMarkers = [
  "<recommended_plugins>",
  "<skills_instructions>",
  "<apps_instructions>",
  "<plugins_instructions>",
  "<collaboration_mode>"
];

function promptPrefix(text: string): string {
  return text.trimStart().slice(0, 5000);
}

export function isCodexRuntimeContextPrompt(text: string): boolean {
  const lowerPrefix = promptPrefix(text).toLowerCase();
  return runtimeContextStartMarkers.some((marker) => lowerPrefix.startsWith(marker));
}

function runtimeContextLabel(text: string): string {
  return promptPrefix(text).toLowerCase().startsWith("<recommended_plugins>")
    ? "推荐插件上下文"
    : "运行时上下文";
}

function isSubagentPrompt(text: string): boolean {
  const prefix = promptPrefix(text);
  return prefix.startsWith("<subagent_notification>")
    || (prefix.includes('"agent_path"') && prefix.includes('"status"') && prefix.toLowerCase().includes("subagent"));
}

function isAutomationPrompt(text: string): boolean {
  const lowerPrefix = promptPrefix(text).toLowerCase();
  return lowerPrefix.startsWith("<heartbeat>")
    || lowerPrefix.startsWith("<automation>")
    || lowerPrefix.startsWith("<scheduled_task>")
    || lowerPrefix.includes("<automation_id>")
    || (lowerPrefix.includes("<current_time_iso>") && lowerPrefix.includes("<instructions>"));
}

function isThreadDelegationPrompt(text: string): boolean {
  return promptPrefix(text).toLowerCase().startsWith("<codex_delegation");
}

function isCodexInternalContextPrompt(text: string): boolean {
  return promptPrefix(text).startsWith("<codex_internal_context");
}

function isInternalContextPrompt(text: string): boolean {
  const prefix = promptPrefix(text);
  return isCodexRuntimeContextPrompt(text)
    || prefix.startsWith("# AGENTS.md instructions")
    || prefix.startsWith("<environment_context>")
    || prefix.startsWith("<turn_aborted>")
    || prefix.startsWith("<user_interruption>")
    || prefix.includes("<environment_context>")
    || prefix.includes("<permissions instructions>");
}

export function isRealUserPromptText(text: string): boolean {
  return Boolean(text.trim())
    && !isInternalContextPrompt(text)
    && !isSubagentPrompt(text)
    && !isAutomationPrompt(text)
    && !isThreadDelegationPrompt(text)
    && !isCodexInternalContextPrompt(text);
}

function removeEmbeddedImageBlocks(text: string): string {
  return text
    .replace(/\n?<image\b[\s\S]*?<\/image>\s*/gi, "\n")
    .replace(/\n?!\[[^\]]*]\([^)]*\)\s*/g, "\n")
    .trim();
}

function pureUserTextFromPrompt(text: string): string {
  if (!isRealUserPromptText(text)) return "";
  const cleanedText = removeEmbeddedImageBlocks(text);
  const markerMatch = /^##\s*My request for Codex:\s*$/im.exec(cleanedText);
  if (markerMatch) {
    return removeEmbeddedImageBlocks(cleanedText.slice(markerMatch.index + markerMatch[0].length)).trim();
  }
  const prefix = cleanedText.trimStart();
  if (prefix.startsWith("# In app browser:") || prefix.startsWith("# Files mentioned by the user:")) return "";
  return cleanedText.trim();
}

function classification(
  sourceType: string,
  sourceLabel: string,
  visibleByDefault: boolean,
  pureText: string
): PromptClassification {
  return {
    sourceType,
    sourceLabel,
    visibleByDefault,
    pureText,
    pureCharacterCount: pureText.length,
    hasPureText: Boolean(pureText)
  };
}

export function classifyPromptText(text: string): PromptClassification {
  const prefix = promptPrefix(text);
  const pureText = pureUserTextFromPrompt(text);
  if (isSubagentPrompt(text)) return classification("subagent", "子 agent", false, pureText);
  if (isAutomationPrompt(text)) return classification("automation", "自动化任务", false, pureText);
  if (isThreadDelegationPrompt(text)) return classification("delegation", "线程转发", false, pureText);
  if (isCodexInternalContextPrompt(text)) return classification("goal", "续跑目标上下文", false, pureText);
  if (isCodexRuntimeContextPrompt(text)) return classification("internal", runtimeContextLabel(text), false, pureText);
  if (isInternalContextPrompt(text)) return classification("internal", "内部上下文", false, pureText);
  if (prefix.startsWith("# In app browser:")) return classification("browser", "浏览器上下文", true, pureText);
  if (prefix.startsWith("# Files mentioned by the user:")) return classification("attachment", "附件上下文", true, pureText);
  return classification("user", "用户输入", true, pureText);
}
