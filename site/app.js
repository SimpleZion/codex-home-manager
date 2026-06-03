const languageStoreKey = "codex-home-manager-public-language";

const copy = {
  zh: {
    navProduct: "产品",
    navSafety: "安全边界",
    navApi: "Agent API",
    navDeploy: "部署",
    switchLanguage: "English",
    eyebrow: "本地优先 · 公开预览 · 核心私有",
    title: "Codex Home Manager",
    subtitle: "管理 Codex Desktop 本地线程、资源、导入、备份、日志和 agent API 的专业控制台。公开站点只展示脱敏产品体验，不包含本地核心引擎代码。",
    primary: "查看公开 API 契约",
    secondary: "查看安全边界",
    privacyPill: "合成数据截图",
    localPill: "真实数据永不进入公开站点",
    metricsThreads: "线程索引",
    metricsVisible: "可见主线程",
    metricsActions: "可控操作",
    metricsCore: "核心代码泄露",
    zero: "0",
    featureTitle: "面向本地 Codex Home 的完整维护工作台",
    featureSubtitle: "公开版只保留产品壳和脱敏演示；真实 SQLite/JSONL 写入、恢复和迁移逻辑留在私有本地引擎中。",
    feature1Title: "线程可见性治理",
    feature1Text: "定位首轮外、隐藏、需修复、归档和子 agent 线程，支持显示、隐藏、修复、归档、复制、迁移和导出 prompt。",
    feature2Title: "资源与导入管理",
    feature2Text: "查看 AGENTS.md、memories、skills、配置片段和日志元数据，并从其他 .codex Home 导入线程、项目或资源。",
    feature3Title: "Agent 可调用 API",
    feature3Text: "能力发现、OpenAPI、token、preview ticket、write gate 和可选备份模式，让新的 agent 不必依赖 UI。",
    feature4Title: "隐私保护设计",
    feature4Text: "公开页面使用假项目、假线程、假 ID 和假路径。真实对话标题、用户 prompt、文件路径和日志不会进入仓库或部署。",
    safetyTitle: "公开仓库不包含核心代码",
    safetyText: "为了满足开源展示和上线需求，同时保护本地维护能力，公开仓库只包含官网、静态 mock、公开 API 合同和安全边界声明。",
    allowTitle: "可以公开",
    allow1: "产品介绍、交互式 mock UI、合成截图",
    allow2: "公开 API 能力清单和安全模型",
    allow3: "Cloudflare Pages 静态部署配置",
    denyTitle: "不会公开",
    deny1: "真实 Codex Home SQLite/JSONL 操作代码",
    deny2: "真实线程标题、prompt、路径、日志和备份",
    deny3: "token、预览票据、写入门禁和恢复实现",
    apiTitle: "Agent API 公开契约",
    apiSubtitle: "下面是给 agent 理解能力边界的公开摘要，不是私有后端源码。",
    deployTitle: "上线目标",
    deployText: "静态公开站点部署到 Cloudflare Pages，并绑定到 simplezion.com 的子域名。核心维护引擎仍只在本地私有环境运行。",
    footer: "Public preview. Private local engine excluded.",
    mockSearch: "搜索标题、线程 ID、项目、JSONL 路径",
    mockVisible: "可见",
    mockHidden: "首轮外",
    mockAgent: "子 agent",
    mockAction: "操作",
    mockHide: "隐藏",
    mockShow: "显示",
    mockLogs: "日志"
  },
  en: {
    navProduct: "Product",
    navSafety: "Safety",
    navApi: "Agent API",
    navDeploy: "Deploy",
    switchLanguage: "中文",
    eyebrow: "Local-first · Public preview · Private core",
    title: "Codex Home Manager",
    subtitle: "A professional console for local Codex Desktop threads, resources, imports, backups, logs, and agent APIs. This public site shows a sanitized product experience only; it does not include the local core engine.",
    primary: "View API contract",
    secondary: "View safety boundary",
    privacyPill: "Synthetic screenshots",
    localPill: "Real local data stays private",
    metricsThreads: "Thread index",
    metricsVisible: "Visible main threads",
    metricsActions: "Controlled actions",
    metricsCore: "Core code leaked",
    zero: "0",
    featureTitle: "A complete maintenance console for local Codex Home operations",
    featureSubtitle: "The public edition only contains the product shell and sanitized demo. Real SQLite/JSONL writes, restore logic, and migration internals stay in the private local engine.",
    feature1Title: "Thread visibility governance",
    feature1Text: "Find outside-first-page, hidden, repair-needed, archived, and subagent threads. Show, hide, repair, archive, duplicate, migrate, slim, and export prompts.",
    feature2Title: "Resources and imports",
    feature2Text: "Inspect AGENTS.md, memories, skills, config snippets, and log metadata. Import threads, projects, or resources from another .codex Home.",
    feature3Title: "Agent-callable API",
    feature3Text: "Capability discovery, OpenAPI, token, preview ticket, write gate, and optional backups let new agents operate without depending on the UI.",
    feature4Title: "Privacy by design",
    feature4Text: "The public page uses fake projects, fake threads, fake IDs, and fake paths. Real titles, prompts, paths, and logs are not committed or deployed.",
    safetyTitle: "The public repository does not include core code",
    safetyText: "To support open publication and deployment while protecting the local maintenance capability, this repository only includes the website, static mock, public API contract, and safety boundary.",
    allowTitle: "Public",
    allow1: "Product overview, interactive mock UI, synthetic screenshots",
    allow2: "Public API capability list and safety model",
    allow3: "Cloudflare Pages static deployment config",
    denyTitle: "Private",
    deny1: "Real Codex Home SQLite/JSONL operation code",
    deny2: "Real thread titles, prompts, paths, logs, and backups",
    deny3: "Token, preview ticket, write gate, and restore internals",
    apiTitle: "Agent API public contract",
    apiSubtitle: "This is a capability summary for agents, not private backend source code.",
    deployTitle: "Deployment target",
    deployText: "The static public site is deployed to Cloudflare Pages and bound to a subdomain under simplezion.com. The core maintenance engine remains local and private.",
    footer: "Public preview. Private local engine excluded.",
    mockSearch: "Search title, thread ID, project, JSONL path",
    mockVisible: "Visible",
    mockHidden: "Outside first page",
    mockAgent: "Subagent",
    mockAction: "Action",
    mockHide: "Hide",
    mockShow: "Show",
    mockLogs: "Logs"
  }
};

function setLanguage(language) {
  const normalizedLanguage = language === "en" ? "en" : "zh";
  document.documentElement.lang = normalizedLanguage === "zh" ? "zh-CN" : "en";
  localStorage.setItem(languageStoreKey, normalizedLanguage);
  document.querySelectorAll("[data-copy]").forEach((element) => {
    const key = element.dataset.copy;
    element.textContent = copy[normalizedLanguage][key] || key;
  });
  document.querySelectorAll("[data-placeholder-copy]").forEach((element) => {
    const key = element.dataset.placeholderCopy;
    element.setAttribute("placeholder", copy[normalizedLanguage][key] || key);
  });
  document.querySelector("[data-language-toggle]").setAttribute("aria-label", copy[normalizedLanguage].switchLanguage);
}

document.querySelector("[data-language-toggle]").addEventListener("click", () => {
  const current = localStorage.getItem(languageStoreKey) === "en" ? "en" : "zh";
  setLanguage(current === "zh" ? "en" : "zh");
});

document.querySelectorAll("[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-filter]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
  });
});

setLanguage(localStorage.getItem(languageStoreKey) || "zh");
