import React from "react";
import {
  CheckCircle2,
  CircleAlert,
  FileClock,
  History,
  PlugZap,
  RefreshCcw,
  RotateCcw,
  Route,
  ScanSearch,
  Settings2,
  ShieldCheck,
  X
} from "lucide-react";
import {
  defaultRepairScope,
  providerInteropParams,
  repairNeeded,
  repairRequest,
  routeNeedsAttention,
  transactionCommitted,
  type AuthorizedJson,
  type ProviderInteropAudit,
  type ProviderInteropPhase,
  type ProviderInteropPreview,
  type ProviderInteropStatus,
  type ProviderInteropTransaction,
  type ProviderInteropVerification,
  type ReadJson,
  type RepairScope,
  type RollbackPreview,
  type TaskCompatibilityAudit,
  type TaskRepairPreview
} from "./providerInteroperability";

function displayPath(value: string): string {
  const normalized = value.trim();
  return normalized.startsWith("\\\\?\\") ? normalized.slice(4) : normalized;
}

type Props = {
  codexHome: string;
  language: "zh" | "en";
  browserMode: boolean;
  refreshKey: number;
  readJson: ReadJson;
  authorizedJson: AuthorizedJson;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
};

type DialogState =
  | { kind: "repair"; preview: ProviderInteropPreview }
  | { kind: "task-repair"; preview: TaskRepairPreview }
  | { kind: "rollback"; preview: RollbackPreview }
  | null;

const byteUnits = ["B", "KB", "MB", "GB", "TB"];

function formatBytes(value: number): string {
  let size = Math.max(0, value || 0);
  let index = 0;
  while (size >= 1024 && index < byteUnits.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 || size >= 10 ? 0 : 1)} ${byteUnits[index]}`;
}

function shortHash(value?: string | null): string {
  return value ? value.slice(0, 12) : "-";
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function runtimeGateMessage(
  runtime: NonNullable<ProviderInteropStatus["runtimeCompatibility"]>,
  zh: boolean
): string {
  switch (runtime.classification) {
    case "unmanaged_override":
      return zh
        ? "当前 Codex 核心尚未匹配到经过验证的互通清单；需要核验核心、伴随程序和协议契约后登记，这不表示补丁文件已经损坏。"
        : "The active Codex runtime has no matching verified interoperability manifest. Register it only after verifying the core, companion, and protocol contracts; this does not mean the patch is corrupt.";
    case "patched_runtime_invalid":
      return zh
        ? "当前互通运行时的核心或伴随程序与登记哈希不一致，请恢复或重新安装已验证制品。"
        : "The interoperability core or companion does not match its registered hash. Restore or reinstall the verified artifact.";
    case "runtime_contract_outdated":
      return zh
        ? "当前运行时缺少供应商互通所需协议，请安装包含完整协议契约的已验证运行时。"
        : "The runtime lacks required provider-interoperability contracts. Install a verified runtime with the complete contract set.";
    case "runtime_activation_conflict":
      return zh
        ? "配置与进程环境指向不同 Codex 核心；统一激活来源后才能安全切换供应商。"
        : "Configuration and process environment select different Codex runtimes. Unify the activation source before switching providers.";
    case "official_upgrade_detected":
      return zh
        ? "检测到官方 Codex 核心升级；继续保留已验证运行时，完成同版本重放测试和哈希登记前不会切换到未知核心。"
        : "An official Codex runtime upgrade was detected. Keep the verified runtime until the new version passes same-version replay tests and hash registration.";
    default:
      return zh
        ? "当前 Codex 核心尚未通过互通版本门控；请按上方升级策略完成对应验证。"
        : "The active Codex runtime has not passed the interoperability gate. Complete the verification indicated by the upgrade policy above.";
  }
}

function useDialogKeyboard(open: boolean, close: () => void) {
  const ref = React.useRef<HTMLElement | null>(null);
  React.useEffect(() => {
    if (!open) return;
    const dialog = ref.current;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        "button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex='-1'])"
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previous?.focus();
    };
  }, [close, open]);
  return ref;
}

function useProviderInteropController(props: Props) {
  const { codexHome, language, browserMode, refreshKey, readJson, authorizedJson, onNotice, onError } = props;
  const zh = language === "zh";
  const [phase, setPhase] = React.useState<ProviderInteropPhase>("idle");
  const [status, setStatus] = React.useState<ProviderInteropStatus | null>(null);
  const [audit, setAudit] = React.useState<ProviderInteropAudit | null>(null);
  const [scope, setScope] = React.useState<RepairScope>(defaultRepairScope);
  const [dialog, setDialog] = React.useState<DialogState>(null);
  const [transaction, setTransaction] = React.useState<ProviderInteropTransaction | null>(null);
  const [verification, setVerification] = React.useState<ProviderInteropVerification | null>(null);
  const [manifestPath, setManifestPath] = React.useState("");
  const [rollbackOperationId, setRollbackOperationId] = React.useState("");
  const [rollbackDigest, setRollbackDigest] = React.useState("");
  const [taskId, setTaskId] = React.useState("");
  const [taskAudit, setTaskAudit] = React.useState<TaskCompatibilityAudit | null>(null);

  const closeDialog = React.useCallback(() => setDialog(null), []);
  const dialogRef = useDialogKeyboard(Boolean(dialog), closeDialog);
  const params = React.useMemo(() => providerInteropParams(codexHome), [codexHome]);

  const loadStatus = React.useCallback(async () => {
    if (browserMode) return;
    setPhase("loading");
    try {
      const result = await readJson<ProviderInteropStatus>(
        `/api/provider-interoperability/status?${params.toString()}`,
        { cache: "no-store" }
      );
      setStatus(result);
      setPhase(routeNeedsAttention(result) || result.adapter.support !== "read_write" ? "blocked" : "ready");
    } catch (error) {
      setPhase("failed");
      onError(message(error));
    }
  }, [browserMode, onError, params, readJson]);

  React.useEffect(() => {
    void loadStatus();
  }, [loadStatus, refreshKey]);

  const post = React.useCallback(async <T,>(path: string, body: object): Promise<T> => (
    authorizedJson<T>(`${path}?${params.toString()}`, {
      method: "POST",
      body: JSON.stringify(body)
    }, true)
  ), [authorizedJson, params]);

  async function scan() {
    setPhase("loading");
    onError("");
    try {
      const [nextStatus, nextAudit] = await Promise.all([
        readJson<ProviderInteropStatus>(`/api/provider-interoperability/status?${params.toString()}`, { cache: "no-store" }),
        post<ProviderInteropAudit>("/api/provider-interoperability/audit", repairRequest(scope))
      ]);
      setStatus(nextStatus);
      setAudit(nextAudit);
      setPhase(routeNeedsAttention(nextStatus) || nextStatus.adapter.support !== "read_write" ? "blocked" : "ready");
    } catch (error) {
      setPhase("failed");
      onError(message(error));
    }
  }

  async function previewRepair() {
    onError("");
    try {
      const preview = await post<ProviderInteropPreview>(
        "/api/provider-interoperability/repair/preview",
        repairRequest(scope)
      );
      setStatus(preview.status);
      setAudit(preview.audit);
      setDialog({ kind: "repair", preview });
      setPhase(preview.blockers.length ? "blocked" : "ready");
    } catch (error) {
      setPhase("failed");
      onError(message(error));
    }
  }

  async function applyRepair(preview: ProviderInteropPreview) {
    setPhase("applying");
    onError("");
    try {
      const result = await post<ProviderInteropTransaction>("/api/provider-interoperability/repair", {
        ...repairRequest(preview.scopes),
        operationPreviewId: preview.operationPreviewId,
        inputHash: preview.inputHash,
        operationId: preview.operationId,
        workerStateDigest: preview.workerStateDigest,
        backupRoot: preview.backupRoot
      });
      setTransaction(result);
      setDialog(null);
      if (!transactionCommitted(result)) {
        setPhase("failed");
        onError(zh ? "事务未提交，请查看回滚状态。" : "The transaction did not commit; inspect its rollback status.");
        return;
      }
      setManifestPath(result.manifestPath);
      setRollbackOperationId(result.operationId);
      setRollbackDigest(preview.workerStateDigest);
      onNotice(zh ? "互通修复已提交并完成事务内验证。" : "Interoperability repair committed and passed transaction verification.");
      await verify(preview.scopes);
    } catch (error) {
      setPhase("failed");
      onError(message(error));
    }
  }

  async function verify(selectedScope = scope) {
    setPhase("verifying");
    onError("");
    try {
      const result = await post<ProviderInteropVerification>(
        "/api/provider-interoperability/verify",
        repairRequest(selectedScope)
      );
      setVerification(result);
      setStatus(result.status);
      setAudit(result.audit);
      setPhase(result.verified ? "ready" : "blocked");
      onNotice(result.verified
        ? (zh ? "静态验证通过。" : "Static verification passed.")
        : (zh ? "静态验证发现仍需处理的差异。" : "Static verification found remaining differences."));
    } catch (error) {
      setPhase("failed");
      onError(message(error));
    }
  }

  async function previewRollback() {
    onError("");
    try {
      const preview = await post<RollbackPreview>("/api/provider-interoperability/rollback/preview", {
        manifestPath,
        operationId: rollbackOperationId,
        workerStateDigest: rollbackDigest
      });
      setDialog({ kind: "rollback", preview });
    } catch (error) {
      onError(message(error));
    }
  }

  async function applyRollback(preview: RollbackPreview) {
    setPhase("applying");
    try {
      const result = await post<{ status: string; restoredFileCount: number }>(
        "/api/provider-interoperability/rollback",
        {
          manifestPath: preview.manifestPath,
          operationId: preview.operationId,
          workerStateDigest: preview.workerStateDigest,
          operationPreviewId: preview.operationPreviewId,
          inputHash: preview.inputHash
        }
      );
      setDialog(null);
      setPhase(result.status === "rolled_back" ? "ready" : "failed");
      onNotice(zh
        ? `已从预映像恢复 ${result.restoredFileCount} 个历史文件。`
        : `Restored ${result.restoredFileCount} history files from pre-images.`);
      await scan();
    } catch (error) {
      setPhase("failed");
      onError(message(error));
    }
  }

  async function inspectTask() {
    const value = taskId.trim();
    if (!value) return;
    onError("");
    try {
      const result = await readJson<TaskCompatibilityAudit>(
        `/api/provider-interoperability/tasks/${encodeURIComponent(value)}/audit?${params.toString()}`,
        { cache: "no-store" }
      );
      setTaskAudit(result);
    } catch (error) {
      onError(message(error));
    }
  }

  async function previewTaskRepair() {
    const value = taskId.trim();
    if (!value) return;
    onError("");
    try {
      const preview = await post<TaskRepairPreview>(
        `/api/provider-interoperability/tasks/${encodeURIComponent(value)}/repair/preview`,
        {}
      );
      setDialog({ kind: "task-repair", preview });
      setPhase(preview.blockers.length ? "blocked" : "ready");
    } catch (error) {
      setPhase("failed");
      onError(message(error));
    }
  }

  async function applyTaskRepair(preview: TaskRepairPreview) {
    setPhase("applying");
    onError("");
    try {
      const result = await post<ProviderInteropTransaction>(
        `/api/provider-interoperability/tasks/${encodeURIComponent(preview.threadId)}/repair`,
        {
          operationPreviewId: preview.operationPreviewId,
          inputHash: preview.inputHash,
          operationId: preview.operationId,
          workerStateDigest: preview.workerStateDigest,
          backupRoot: preview.backupRoot
        }
      );
      setTransaction(result);
      setDialog(null);
      setManifestPath(result.manifestPath);
      setRollbackOperationId(result.operationId);
      setRollbackDigest(preview.workerStateDigest);
      await inspectTask();
      setPhase(transactionCommitted(result) ? "ready" : "failed");
      onNotice(zh ? "该任务的跨提供商活动上下文已修复。" : "The task active context is now cross-provider compatible.");
    } catch (error) {
      setPhase("failed");
      onError(message(error));
    }
  }

  return {
    zh, phase, status, audit, scope, setScope, dialog, transaction, verification,
    manifestPath, setManifestPath, rollbackOperationId, setRollbackOperationId,
    rollbackDigest, setRollbackDigest, taskId, setTaskId, taskAudit, dialogRef, closeDialog,
    scan, previewRepair, applyRepair, verify, previewRollback, applyRollback,
    inspectTask, previewTaskRepair, applyTaskRepair
  };
}

export function ProviderInteroperability(props: Props) {
  const { browserMode, codexHome, language } = props;
  const {
    zh, phase, status, audit, scope, setScope, dialog, transaction, verification,
    manifestPath, setManifestPath, rollbackOperationId, setRollbackOperationId,
    rollbackDigest, setRollbackDigest, taskId, setTaskId, taskAudit, dialogRef, closeDialog,
    scan, previewRepair, applyRepair, verify, previewRollback, applyRollback,
    inspectTask, previewTaskRepair, applyTaskRepair
  } = useProviderInteropController(props);

  if (browserMode) {
    return (
      <div className="provider-interoperability provider-browser-readonly">
        <ShieldCheck size={20} aria-hidden="true" />
        <div>
          <h2>{zh ? "供应商互通" : "Provider interoperability"}</h2>
          <p>{zh
            ? "浏览器文件夹模式只能读取用户选择的 Codex Home，无法检查 CC Switch 外部状态，也不会提供修复或回滚。"
            : "Browser-folder mode can read only the selected Codex Home. It cannot inspect CC Switch state or offer repair and rollback."}</p>
        </div>
      </div>
    );
  }

  const routeWarning = routeNeedsAttention(status);
  const runtimeCompatibility = status?.runtimeCompatibility;
  const runtimeWarning = runtimeCompatibility != null && runtimeCompatibility.classification !== "verified_patched";
  const scopeIsEmpty = !scope.history && !scope.continuationCompatibility && !scope.commonConfig && !scope.interoperabilitySettings;
  const busy = ["loading", "applying", "verifying"].includes(phase);

  return (
    <div className="provider-interoperability" data-phase={phase}>
      <div className="provider-page-header">
        <div>
          <span className="eyebrow">Provider interoperability</span>
          <h2>{zh ? "供应商互通" : "Provider interoperability"}</h2>
          <p>{zh
            ? "共享任务历史、插件与通用配置，同时隔离官方认证、代理令牌、路由和额度归属。"
            : "Share task history, plugins, and common configuration while isolating official auth, proxy tokens, routing, and billing identity."}</p>
        </div>
        <div className="provider-command-row">
          <button type="button" onClick={() => void scan()} disabled={busy} title={zh ? "运行只读扫描" : "Run read-only scan"}>
            <ScanSearch size={16} aria-hidden="true" />{zh ? "扫描" : "Scan"}
          </button>
          <button type="button" onClick={() => void previewRepair()} disabled={busy || scopeIsEmpty}>
            <FileClock size={16} aria-hidden="true" />{zh ? "预览修复" : "Preview repair"}
          </button>
          <button type="button" onClick={() => void verify()} disabled={busy || scopeIsEmpty}>
            <ShieldCheck size={16} aria-hidden="true" />{phase === "verifying" ? (zh ? "验证中" : "Verifying") : (zh ? "验证" : "Verify")}
          </button>
        </div>
      </div>

      <section className="provider-status-band" aria-label={zh ? "互通状态" : "Interoperability status"}>
        <StatusCell icon={Route} label={zh ? "实际路由证据" : "Route evidence"} value={status?.route.classification || "-"} tone={routeWarning ? "warning" : "normal"} />
        <StatusCell icon={History} label={zh ? "共享历史桶" : "Shared history bucket"} value={`custom · ${status?.historyBuckets.custom ?? 0}`} />
        <StatusCell icon={PlugZap} label={zh ? "外部适配器" : "External adapter"} value={status?.adapter.support || "-"} tone={status?.adapter.support === "read_write" ? "good" : "warning"} />
        <StatusCell icon={Settings2} label={zh ? "互通设置" : "Interop settings"} value={status?.adapter.settings?.unifySessionHistory ? (zh ? "已启用" : "Enabled") : (zh ? "待检查" : "Needs review")} />
        <StatusCell icon={ShieldCheck} label={zh ? "核心版本门控" : "Runtime version gate"} value={runtimeCompatibility?.classification || "-"} tone={runtimeWarning ? "warning" : runtimeCompatibility ? "good" : "normal"} />
        <StatusCell icon={verification?.verified ? CheckCircle2 : CircleAlert} label={zh ? "静态验证" : "Static verification"} value={verification ? (verification.verified ? (zh ? "通过" : "Passed") : (zh ? "未通过" : "Not passed")) : "-"} tone={verification?.verified ? "good" : "normal"} />
      </section>

      {routeWarning ? (
        <div className="provider-warning" role="status">
          <CircleAlert size={17} aria-hidden="true" />
          <span>{zh
            ? "当前路由证据冲突或不足。历史桶 custom 只负责共享可见性，不能用于判断实际请求由谁计费。"
            : "Route evidence is conflicting or incomplete. The custom history bucket controls shared visibility and does not identify billing."}</span>
        </div>
      ) : null}

      {runtimeWarning ? (
        <div className="provider-warning" role="status">
          <CircleAlert size={17} aria-hidden="true" />
          <span>{runtimeGateMessage(runtimeCompatibility, zh)}</span>
        </div>
      ) : null}

      <div className="provider-work-grid">
        <section className="provider-panel evidence-panel">
          <div className="provider-panel-heading">
            <div><span className="eyebrow">Evidence</span><h3>{zh ? "当前证据" : "Current evidence"}</h3></div>
            <code>{phase}</code>
          </div>
          <dl className="provider-evidence-list">
            <div><dt>CODEX_HOME</dt><dd title={status?.environment.codexHome}>{displayPath(status?.environment.codexHome || codexHome) || "-"}</dd></div>
            <div><dt>{zh ? "提供商" : "Provider"}</dt><dd>{status?.adapter.currentProviderName || status?.route.providerName || "-"}</dd></div>
            <div><dt>{zh ? "路由置信度" : "Route confidence"}</dt><dd>{status?.route.confidence || "-"}</dd></div>
            <div><dt>{zh ? "历史身份" : "History identity"}</dt><dd>{status?.route.modelProvider || "-"}</dd></div>
            <div><dt>{zh ? "适配器 schema" : "Adapter schema"}</dt><dd>{status?.adapter.userVersion == null ? "-" : `v${status.adapter.userVersion} · ${shortHash(status.adapter.schemaFingerprint)}`}</dd></div>
            <div><dt>{zh ? "SQLite 完整性" : "SQLite integrity"}</dt><dd>{status?.adapter.databaseQuickCheck || "-"}</dd></div>
            <div><dt>{zh ? "官方认证要求" : "Official auth required"}</dt><dd>{status?.authentication.officialAuthRequired || "-"}</dd></div>
            <div><dt>{zh ? "第三方凭据状态" : "Third-party credential state"}</dt><dd>{status?.authentication.providerToken || "-"}</dd></div>
            <div><dt>{zh ? "核心升级策略" : "Runtime upgrade policy"}</dt><dd>{runtimeCompatibility?.recommendedAction || "-"}</dd></div>
          </dl>
        </section>

        <section className="provider-panel scope-panel">
          <div className="provider-panel-heading"><div><span className="eyebrow">Repair scope</span><h3>{zh ? "修复范围" : "Repair scope"}</h3></div></div>
          <ScopeToggle checked={scope.history} icon={History} title={zh ? "共享历史身份" : "Shared history identity"} description={zh ? "仅把 openai 历史身份等长替换为 custom；不据此判断实际路由或额度。" : "Replace only the equal-length openai history identity with custom; never use it to infer routing or billing."} onChange={(value) => setScope((current) => ({ ...current, history: value }))} />
          <ScopeToggle checked={scope.continuationCompatibility} icon={RefreshCcw} title={zh ? "跨提供商续聊守卫" : "Continuation compatibility guard"} description={zh ? "切到官方前移除无法由官方重放的代理 reasoning，并剥离类型不合法的代理响应项 ID；原始字节完整保留在事务备份中。" : "Before an official switch, remove proxy reasoning that OpenAI cannot replay and strip type-invalid proxy item IDs; preserve exact originals in the transaction backup."} onChange={(value) => setScope((current) => ({ ...current, continuationCompatibility: value }))} />
          <ScopeToggle checked={scope.commonConfig} icon={PlugZap} title={zh ? "插件与通用配置" : "Plugins and common config"} description={zh ? "把提供商无关的插件、市场和安全 MCP 声明合并到 CC Switch 通用层。" : "Merge provider-independent plugins, marketplaces, and safe MCP declarations into the CC Switch common layer."} onChange={(value) => setScope((current) => ({ ...current, commonConfig: value }))} />
          <ScopeToggle checked={scope.interoperabilitySettings} icon={Settings2} title={zh ? "互通开关" : "Interoperability settings"} description={zh ? "保留官方登录缓存，并启用统一历史和既有历史迁移。" : "Preserve official login cache and enable unified history plus existing-history migration."} onChange={(value) => setScope((current) => ({ ...current, interoperabilitySettings: value }))} />
          {scopeIsEmpty ? <p className="provider-inline-error" role="alert">{zh ? "至少选择一个修复范围。" : "Select at least one repair scope."}</p> : null}
        </section>

        <section className="provider-panel audit-panel">
          <div className="provider-panel-heading"><div><span className="eyebrow">Audit</span><h3>{zh ? "历史审计" : "History audit"}</h3></div></div>
          <div className="provider-metrics">
            <Metric label={zh ? "已扫描文件" : "Scanned files"} value={audit?.scannedFileCount ?? 0} />
            <Metric label={zh ? "待改文件" : "Files to change"} value={audit?.changedFileCount ?? 0} />
            <Metric label={zh ? "待转换记录" : "Planned rewrites"} value={audit?.replacementCount ?? 0} />
            <Metric label={zh ? "续聊风险项" : "Continuation risks"} value={(audit?.nonportableReasoningCount ?? 0) + (audit?.incompatibleItemIdCount ?? 0)} />
            <Metric label={zh ? "备份空间" : "Backup bytes"} value={formatBytes(audit?.requiredBackupBytes ?? 0)} />
          </div>
          <dl className="provider-audit-hashes">
            <div><dt>{zh ? "用户输入序列" : "Prompt sequence"}</dt><dd>{audit ? `${audit.promptFingerprint.count} · ${shortHash(audit.promptFingerprint.sha256)}` : "-"}</dd></div>
            <div><dt>{zh ? "受保护内容" : "Protected content"}</dt><dd>{shortHash(audit?.protectedContentSha256)}</dd></div>
            <div><dt>{zh ? "解析错误" : "Parse errors"}</dt><dd>{audit?.parseErrorCount ?? 0}</dd></div>
          </dl>
        </section>

        <section className="provider-panel task-panel">
          <div className="provider-panel-heading"><div><span className="eyebrow">Task compatibility</span><h3>{zh ? "任务续聊审计" : "Task continuation audit"}</h3></div></div>
          <div className="provider-input-action">
            <input aria-label={zh ? "任务 ID" : "Task ID"} value={taskId} onChange={(event) => setTaskId(event.target.value)} placeholder="019f..." />
            <button type="button" onClick={() => void inspectTask()} disabled={!taskId.trim()}><ScanSearch size={15} aria-hidden="true" />{zh ? "检查" : "Inspect"}</button>
          </div>
          {taskAudit ? (
            <dl className="provider-task-result">
              <div><dt>{zh ? "可见性" : "Visibility"}</dt><dd>{taskAudit.visibilityCompatible ? (zh ? "兼容" : "Compatible") : (zh ? "异常" : "Issue")}</dd></div>
              <div><dt>{zh ? "续聊" : "Continuation"}</dt><dd>{taskAudit.continuationCompatible ? (zh ? "兼容" : "Compatible") : (zh ? "有风险" : "At risk")}</dd></div>
              <div><dt>{zh ? "不可移植推理" : "Non-portable reasoning"}</dt><dd>{taskAudit.nonPortableReasoningCount}</dd></div>
              <p>{taskAudit.recommendation}</p>
              {!taskAudit.continuationCompatible ? <button type="button" onClick={() => void previewTaskRepair()}><FileClock size={15} aria-hidden="true" />{zh ? "预览此任务修复" : "Preview task repair"}</button> : null}
            </dl>
          ) : <p className="provider-muted">{zh ? "可见历史与跨提供商续聊是两项独立检查。" : "Visible history and cross-provider continuation are checked independently."}</p>}
        </section>

        <section className="provider-panel rollback-panel">
          <div className="provider-panel-heading"><div><span className="eyebrow">Rollback</span><h3>{zh ? "备份与回滚" : "Backup and rollback"}</h3></div></div>
          {transaction ? (
            <div className="provider-backup-record">
              <CheckCircle2 size={17} aria-hidden="true" />
              <div><strong>{transaction.status}</strong><code title={transaction.manifestPath}>{transaction.manifestPath}</code></div>
            </div>
          ) : <p className="provider-muted">{zh ? "成功修复后，事务清单会自动出现在这里。" : "The transaction manifest appears here after a successful repair."}</p>}
          <label><span>{zh ? "事务清单路径" : "Transaction manifest path"}</span><input value={manifestPath} onChange={(event) => setManifestPath(event.target.value)} /></label>
          <div className="provider-rollback-binding">
            <label><span>{zh ? "操作 ID" : "Operation ID"}</span><input value={rollbackOperationId} onChange={(event) => setRollbackOperationId(event.target.value)} /></label>
            <label><span>{zh ? "状态摘要" : "State digest"}</span><input value={rollbackDigest} onChange={(event) => setRollbackDigest(event.target.value)} /></label>
          </div>
          <button className="provider-secondary-command" type="button" onClick={() => void previewRollback()} disabled={!manifestPath || !rollbackOperationId || rollbackDigest.length !== 64 || busy}>
            <RotateCcw size={15} aria-hidden="true" />{zh ? "预览回滚" : "Preview rollback"}
          </button>
        </section>
      </div>

      {dialog ? (
        <div className="provider-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeDialog(); }}>
          <section ref={dialogRef} className="provider-review-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-review-title" tabIndex={-1}>
            <div className="provider-dialog-header">
              <div><span className="eyebrow">Change review</span><h3 id="provider-review-title">{dialog.kind === "rollback" ? (zh ? "回滚审阅" : "Rollback review") : (zh ? "修复变更审阅" : "Repair change review")}</h3></div>
              <button type="button" onClick={closeDialog} aria-label={zh ? "关闭审阅" : "Close review"}><X size={18} aria-hidden="true" /></button>
            </div>
            {dialog.kind === "repair" ? (
              <RepairReview preview={dialog.preview} language={language} />
            ) : dialog.kind === "task-repair" ? (
              <TaskRepairReview preview={dialog.preview} language={language} />
            ) : (
              <div className="provider-dialog-copy"><p>{zh ? "将从校验通过的预映像恢复历史、Codex 状态库、CC Switch 数据库和设置。" : "Restore history, Codex state, CC Switch database, and settings from verified pre-images."}</p><code>{dialog.preview.manifestPath}</code></div>
            )}
            <div className="provider-dialog-actions">
              <button type="button" onClick={closeDialog}>{zh ? "取消" : "Cancel"}</button>
              {dialog.kind === "repair" ? (
                <button className="primary" type="button" onClick={() => void applyRepair(dialog.preview)} disabled={dialog.preview.blockers.length > 0 || !repairNeeded(dialog.preview)}>
                  <RefreshCcw size={15} aria-hidden="true" />{dialog.preview.blockers.length ? (zh ? "请先关闭相关进程" : "Close blocked processes") : (zh ? "执行修复" : "Apply repair")}
                </button>
              ) : dialog.kind === "task-repair" ? (
                <button className="primary" type="button" onClick={() => void applyTaskRepair(dialog.preview)} disabled={dialog.preview.blockers.length > 0 || dialog.preview.audit.replacementCount === 0}>
                  <RefreshCcw size={15} aria-hidden="true" />{dialog.preview.blockers.length ? (zh ? "请先关闭相关进程" : "Close blocked processes") : (zh ? "修复此任务" : "Repair task")}
                </button>
              ) : (
                <button className="primary danger" type="button" onClick={() => void applyRollback(dialog.preview)}><RotateCcw size={15} aria-hidden="true" />{zh ? "执行回滚" : "Apply rollback"}</button>
              )}
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function StatusCell({ icon: Icon, label, value, tone = "normal" }: { icon: typeof Route; label: string; value: string; tone?: "normal" | "good" | "warning" }) {
  return <div className={`provider-status-cell tone-${tone}`}><Icon size={16} aria-hidden="true" /><span>{label}</span><strong>{value}</strong></div>;
}

function ScopeToggle({ checked, icon: Icon, title, description, onChange }: { checked: boolean; icon: typeof Route; title: string; description: string; onChange: (value: boolean) => void }) {
  return <label className="provider-scope-toggle"><input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} /><Icon size={17} aria-hidden="true" /><span><strong>{title}</strong><small>{description}</small></span></label>;
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return <div><span>{label}</span><strong>{typeof value === "number" ? value.toLocaleString("en-US") : value}</strong></div>;
}

function RepairReview({ preview, language }: { preview: ProviderInteropPreview; language: "zh" | "en" }) {
  const zh = language === "zh";
  const adapter = preview.adapterRepair;
  return <div className="provider-review-content">
    <div className="provider-review-metrics">
      <Metric label={zh ? "JSONL 文件" : "JSONL files"} value={preview.audit.changedFileCount} />
      <Metric label={zh ? "身份替换" : "Replacements"} value={preview.audit.replacementCount} />
      <Metric label={zh ? "共享配置项" : "Shared config entries"} value={adapter?.commonConfigAdditions ?? 0} />
      <Metric label={zh ? "所需备份" : "Required backup"} value={formatBytes(preview.audit.requiredBackupBytes)} />
    </div>
    <div className="provider-preservation-contract"><ShieldCheck size={17} aria-hidden="true" /><p>{zh ? "硬契约：用户输入顺序、受保护内容、提供商专属设置和凭据保持不变。" : "Hard contract: prompt order, protected content, provider-specific settings, and credentials remain unchanged."}</p></div>
    {preview.blockers.length ? <div className="provider-blockers" role="alert"><strong>{zh ? "写入阻塞" : "Write blockers"}</strong>{preview.blockers.map((blocker) => <code key={blocker}>{blocker}</code>)}</div> : null}
    {!repairNeeded(preview) ? <p className="provider-no-change"><CheckCircle2 size={16} aria-hidden="true" />{zh ? "当前范围无需修复。" : "No repair is required for the selected scope."}</p> : null}
    <dl className="provider-review-details">
      <div><dt>{zh ? "备份目录" : "Backup root"}</dt><dd>{preview.backupRoot}</dd></div>
      <div><dt>{zh ? "设置变更" : "Setting changes"}</dt><dd>{adapter?.settingChanges.join(", ") || "-"}</dd></div>
      <div><dt>{zh ? "提供商快照" : "Provider snapshots"}</dt><dd>{adapter?.providerSnapshotsPreserved ? (zh ? "仅校验，不改写" : "Verified, never rewritten") : "-"}</dd></div>
    </dl>
  </div>;
}

function TaskRepairReview({ preview, language }: { preview: TaskRepairPreview; language: "zh" | "en" }) {
  const zh = language === "zh";
  return <div className="provider-review-content">
    <div className="provider-review-metrics">
      <Metric label={zh ? "任务" : "Task"} value={preview.threadId.slice(0, 8)} />
      <Metric label={zh ? "不可移植推理" : "Non-portable reasoning"} value={preview.audit.nonportableReasoningCount} />
      <Metric label={zh ? "不合法 ID" : "Invalid IDs"} value={preview.audit.incompatibleItemIdCount} />
      <Metric label={zh ? "所需备份" : "Required backup"} value={formatBytes(preview.audit.requiredBackupBytes)} />
    </div>
    <div className="provider-preservation-contract"><ShieldCheck size={17} aria-hidden="true" /><p>{zh ? "只处理最后一次压缩及其后的活动重放上下文；压缩前历史保持原字节，完整源文件写入事务备份。" : "Only the active replay context at and after the latest compaction is changed; earlier history stays byte-exact and the full source is stored in the transaction backup."}</p></div>
    {preview.blockers.length ? <div className="provider-blockers" role="alert"><strong>{zh ? "写入阻塞" : "Write blockers"}</strong>{preview.blockers.map((blocker) => <code key={blocker}>{blocker}</code>)}</div> : null}
  </div>;
}
