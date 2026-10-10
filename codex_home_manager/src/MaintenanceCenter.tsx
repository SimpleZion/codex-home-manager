import React from "react";
import { ArrowDownToLine, ArrowLeft, CheckCircle2, ChevronRight, CircleAlert, FileClock, FileText, RefreshCcw, Search, ShieldCheck, XCircle } from "lucide-react";
import catalogData from "../packaging/runtime/cockpit-maintenance.json";
import "./maintenance.css";
declare const __COCKPIT_CATALOG_SHA256__: string;

type EntryStatus = "prepared" | "historical" | "withdrawn" | "unresolved" | "integrated";
type Evidence = { path: string; label: string; availability?: string; bytes?: number; modifiedAt?: string };
type Artifact = { availability: string; path?: string; bytes?: number; expectedSha256?: string; reason?: string };
type ArtifactVerification = Artifact & { actualSha256: string; hashMatches: boolean; observedAt: string; runtimeVerified: false };
type EvidenceTarget = { catalogSha256: string; entryId: string; evidenceIndex: number; path: string };
type Fingerprint = Evidence & EvidenceTarget & { sha256: string | null; hashStatus: string; observedAt: string };
type RuntimeObservation = { observedAt: string; runtimeVerified: false; checks: { id: string; state: string; reason: string; scope: string; details: Record<string, unknown> }[] };
type Entry = {
  id: string; title: string; category: string; status: EntryStatus; summary: string; reason: string;
  targets: string[]; changes: { before: string; after: string; files: string[] }[]; verification: string[];
  risks: string[]; preconditions: string[]; rollback: string[]; evidence: Evidence[]; nextAction: string;
};
type Observation = {
  observedAt: string; catalogUpdatedAt: string; catalogSha256: string; runtimeVerified: false; sourceWorkspaceAvailable: boolean;
  entries: { id: string; evidence: Evidence[] }[];
  receipts: { kind: string; availability: string; values: Record<string, string | number>; artifact?: Artifact | null }[];
};
type Props = { connected: boolean; codexHome: string; readJson: <T>(path: string, options?: RequestInit) => Promise<T> };
type DetailTab = "overview" | "changes" | "evidence" | "decision";
const entries = catalogData.entries as Entry[];
const statusLabels: Record<EntryStatus, string> = { prepared: "已准备", historical: "历史记录", withdrawn: "已撤回", unresolved: "待解决", integrated: "已集成" };
const availabilityLabels: Record<string, string> = { present: "文件存在", missing: "未找到", unavailable: "未连接源工作区", unreadable: "不可读取", invalid: "无效回执" };
const tabs: [DetailTab, string][] = [["overview", "概览"], ["changes", "逐项变更"], ["evidence", "证据与验证"], ["decision", "决策与回滚"]];
const statusIcons = { prepared: FileClock, historical: FileText, withdrawn: XCircle, unresolved: CircleAlert, integrated: CheckCircle2 };

export function evidenceVerificationUrl(target: EvidenceTarget, sourceWorkspace: string) {
  return `/api/maintenance/evidence/${encodeURIComponent(target.entryId)}/${target.evidenceIndex}?${new URLSearchParams({ source_workspace: sourceWorkspace, catalog_sha256: target.catalogSha256 })}`;
}

export function evidenceFingerprintMatches(result: Fingerprint, target: EvidenceTarget) {
  return result?.catalogSha256 === target.catalogSha256 && result.entryId === target.entryId
    && result.evidenceIndex === target.evidenceIndex && result.path === target.path
    && ((result.hashStatus === "complete" && typeof result.sha256 === "string" && /^[a-f0-9]{64}$/i.test(result.sha256))
      || (result.hashStatus === "size_limit" && result.sha256 === null));
}

export function canVerifyArtifact(target: Artifact | null | undefined): target is Artifact & { path: string; bytes: number; expectedSha256: string } {
  return target?.availability === "present" && typeof target.path === "string" && target.path.length > 0
    && typeof target.expectedSha256 === "string" && /^[a-f0-9]{64}$/i.test(target.expectedSha256)
    && typeof target.bytes === "number" && Number.isSafeInteger(target.bytes) && target.bytes >= 0;
}

export function readyArtifactVerificationUrl(target: Artifact) {
  if (!canVerifyArtifact(target)) throw new Error("展示制品缺少可核验的路径、SHA-256或字节数，请刷新证据。");
  return `/api/maintenance/artifact/ready?${new URLSearchParams({ expected_path: target.path, expected_sha256: target.expectedSha256 })}`;
}

export function artifactVerificationMatches(result: ArtifactVerification, target: Artifact) {
  return canVerifyArtifact(target) && result?.availability === "present" && result.path === target.path && result.bytes === target.bytes
    && result.expectedSha256 === target.expectedSha256 && typeof result.actualSha256 === "string" && /^[a-f0-9]{64}$/i.test(result.actualSha256)
    && result.hashMatches === (result.actualSha256.toLowerCase() === target.expectedSha256.toLowerCase());
}

export function createMaintenanceRequestPool() {
  const pending = new Map<string, { controller: AbortController; promise: Promise<unknown> }>();
  return {
    has: (key: string) => pending.has(key),
    start<T>(key: string, request: (signal: AbortSignal) => Promise<T>) {
      if (pending.has(key)) return null;
      const controller = new AbortController();
      const promise = Promise.resolve().then(() => { controller.signal.throwIfAborted(); return request(controller.signal); }).finally(() => { pending.delete(key); });
      const flight = { controller, promise };
      pending.set(key, flight);
      return flight;
    },
    // Abort invalidates consumers; keep ownership until the underlying promise settles.
    abortAll: () => { for (const flight of pending.values()) flight.controller.abort(); },
  };
}

function Status({ status }: { status: EntryStatus }) {
  const Icon = statusIcons[status];
  return <span className={`maintenance-status ${status}`}><Icon size={14} />{statusLabels[status]}</span>;
}

function DetailList({ title, items }: { title: string; items: string[] }) {
  return <section className="maintenance-detail-section"><h3>{title}</h3><ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul></section>;
}

export function RuntimeEvidenceTimes({ observation, check }: { observation: RuntimeObservation; check: RuntimeObservation["checks"][number] }) {
  const timestamp = (value: unknown) => typeof value === "string" && value.trim() && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "未提供或无法解析";
  const receiptOnly = check.id === "desktop-pagination-6511" || check.details?.evidenceScope === "receipt_only";
  return <><dl><dt>本次读取时间</dt><dd>{timestamp(observation.observedAt)}</dd>{receiptOnly ? <><dt>历史回执时间</dt><dd>{timestamp(check.details?.receiptObservedAt)}</dd></> : null}</dl>{receiptOnly ? <p className="maintenance-muted">仅为历史回执证据；本次读取不代表回执更新，未核验当前进程或修复是否生效。</p> : null}</>;
}

function downloadPlan(entry: Entry, observation: Observation | null, decision: string, note: string, artifactVerification: ArtifactVerification | null, fingerprints: Fingerprint[], runtimeObservation: RuntimeObservation | null) {
  const plan = {
    product: "Codex Cockpit", kind: "decision-plan", schemaVersion: 1, generatedAt: new Date().toISOString(),
    catalogUpdatedAt: catalogData.updatedAt, catalogSha256: __COCKPIT_CATALOG_SHA256__, entry, safetyGates: catalogData.safetyGates, decision, note,
    observation, artifactVerification, fingerprints, runtimeObservation, executionAuthorized: false, executed: false,
    readiness: "blocked", blockers: ["尚未完成当次执行授权与目标运行验收", ...(!observation ? ["没有本机证据"] : observation.catalogSha256 !== __COCKPIT_CATALOG_SHA256__ ? ["前端与服务目录指纹不一致"] : []), ...(artifactVerification && !artifactVerification.hashMatches ? ["安装包哈希不一致"] : [])],
    requiredApproval: "本文件仅记录决策，不授权重启、安装、工作区外写入或历史修改。执行前重新核验本机版本、身份、活动任务和回滚。",
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(plan, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `codex-cockpit-${entry.id}-decision.json`;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function MaintenanceCenter({ connected, codexHome, readJson }: Props) {
  const [query, setQuery] = React.useState("");
  const [category, setCategory] = React.useState("all");
  const [status, setStatus] = React.useState("all");
  const [selectedId, setSelectedId] = React.useState(entries[0]?.id ?? "");
  const [tab, setTab] = React.useState<DetailTab>("overview");
  const [observation, setObservation] = React.useState<Observation | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState("");
  const [refreshKey, setRefreshKey] = React.useState(0);
  const [mobileDetail, setMobileDetail] = React.useState(false);
  const [decisions, setDecisions] = React.useState<Record<string, { decision: string; note: string }>>({});
  const [hashResults, setHashResults] = React.useState<Record<string, string>>({});
  const [artifactVerification, setArtifactVerification] = React.useState<ArtifactVerification | null>(null);
  const [artifactBusy, setArtifactBusy] = React.useState(false);
  const [fingerprints, setFingerprints] = React.useState<Record<string, Fingerprint>>({});
  const [runtimeObservation, setRuntimeObservation] = React.useState<RuntimeObservation | null>(null);
  const [runtimeBusy, setRuntimeBusy] = React.useState(false);
  const [sourceWorkspace, setSourceWorkspace] = React.useState("");
  const [sourceWorkspaceDraft, setSourceWorkspaceDraft] = React.useState("");
  const requestController = React.useRef<AbortController | null>(null);
  const verificationInFlight = React.useRef(new Set<string>());
  const [runtimeRequests] = React.useState(createMaintenanceRequestPool);
  const scope = JSON.stringify([connected, codexHome, sourceWorkspace, refreshKey]);
  const currentScope = React.useRef(scope);
  currentScope.current = scope;
  const currentHome = React.useRef(codexHome);
  currentHome.current = codexHome;
  const requestScope = React.useRef("");
  React.useEffect(() => () => runtimeRequests.abortAll(), [runtimeRequests]);
  React.useEffect(() => {
    requestScope.current = scope;
    verificationInFlight.current = new Set();
    setObservation(null);
    setError("");
    setHashResults({});
    setFingerprints({});
    setRuntimeObservation(null);
    setRuntimeBusy(runtimeRequests.has(codexHome));
    setArtifactVerification(null);
    setArtifactBusy(false);
    if (!connected) { requestController.current = null; setLoading(false); return; }
    const controller = new AbortController();
    requestController.current = controller;
    setLoading(true);
    void readJson<Observation>(`/api/maintenance/status?${new URLSearchParams({ source_workspace: sourceWorkspace })}`, { signal: controller.signal, cache: "no-store" })
      .then((result) => { if (!controller.signal.aborted) setObservation(result); })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [connected, refreshKey, readJson, sourceWorkspace, codexHome, scope, runtimeRequests]);
  const visibleEntries = entries.filter((entry) => (status === "all" || status === entry.status)
    && (category === "all" || category === entry.category)
    && JSON.stringify(entry).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const entry = visibleEntries.find((item) => item.id === selectedId) ?? visibleEntries[0];
  const liveEntry = observation?.entries.find((item) => item.id === entry?.id);
  const runtimeCheck = runtimeObservation?.checks.find((item) => item.id === entry?.id);
  const preparedPackage = entry?.id === "desktop-pagination-6511" ? observation?.receipts.find((receipt) => receipt.kind === "ready")?.artifact : null;
  const decision = entry ? decisions[entry.id] ?? { decision: "review", note: "" } : { decision: "review", note: "" };
  const catalogMatches = observation?.catalogSha256 === __COCKPIT_CATALOG_SHA256__;
  const requestIsCurrent = (controller: AbortController) => !controller.signal.aborted && requestController.current === controller && currentScope.current === scope;
  const updateDecision = (patch: Partial<typeof decision>) => { if (entry) setDecisions((current) => ({ ...current, [entry.id]: { ...decision, ...patch } })); };
  const verifyEvidence = async (index: number) => {
    const controller = requestController.current;
    if (!entry || !connected || loading || !catalogMatches || !controller || controller.signal.aborted || requestScope.current !== scope) return;
    const record = entry.evidence[index];
    if (!record || liveEntry?.evidence.find((item) => item.path === record.path)?.availability !== "present") return;
    const key = `${entry.id}/${index}`;
    const inFlight = verificationInFlight.current;
    if (inFlight.has(key)) return;
    inFlight.add(key);
    const target = { catalogSha256: __COCKPIT_CATALOG_SHA256__, entryId: entry.id, evidenceIndex: index, path: record.path };
    setFingerprints((current) => { const next = { ...current }; delete next[key]; return next; });
    setHashResults((current) => ({ ...current, [key]: "核验中…" }));
    try {
      const result = await readJson<Fingerprint>(evidenceVerificationUrl(target, sourceWorkspace), { cache: "no-store", signal: controller.signal });
      if (requestIsCurrent(controller)) {
        if (!evidenceFingerprintMatches(result, target)) throw new Error("证据目录或目标身份已变化，请刷新后重新核验。");
        setHashResults((current) => ({ ...current, [key]: result.sha256 ? `SHA-256 ${result.sha256}` : "文件超过 128 KiB；已限制读取，未计算完整哈希。" }));
        setFingerprints((current) => ({ ...current, [key]: result }));
      }
    } catch (reason) { if (requestIsCurrent(controller)) setHashResults((current) => ({ ...current, [key]: `核验失败：${reason instanceof Error ? reason.message : String(reason)}` })); }
    finally { inFlight.delete(key); }
  };
  const verifyRuntime = async () => {
    const controller = requestController.current;
    if (!connected || loading || !controller || controller.signal.aborted || requestScope.current !== scope) return;
    const flight = runtimeRequests.start(codexHome, (signal) => readJson<RuntimeObservation>(`/api/maintenance/runtime?${new URLSearchParams(codexHome ? { codex_home: codexHome } : {})}`, { cache: "no-store", signal }));
    if (!flight) return;
    setError("");
    setRuntimeBusy(true);
    setRuntimeObservation(null);
    try {
      const result = await flight.promise;
      if (!flight.controller.signal.aborted && requestIsCurrent(controller)) setRuntimeObservation(result);
    } catch (reason) { if (!flight.controller.signal.aborted && requestIsCurrent(controller)) setError(`环境核验失败：${reason instanceof Error ? reason.message : String(reason)}`); }
    finally { if (!flight.controller.signal.aborted && currentHome.current === codexHome) setRuntimeBusy(runtimeRequests.has(codexHome)); }
  };
  const verifyPackage = async () => {
    const controller = requestController.current;
    if (!connected || loading || !controller || controller.signal.aborted || requestScope.current !== scope || !canVerifyArtifact(preparedPackage)) return;
    const inFlight = verificationInFlight.current;
    if (inFlight.has("artifact/ready")) return;
    inFlight.add("artifact/ready");
    const target = { ...preparedPackage };
    setError("");
    setArtifactBusy(true);
    setArtifactVerification(null);
    try {
      const result = await readJson<ArtifactVerification>(readyArtifactVerificationUrl(target), { cache: "no-store", signal: controller.signal });
      if (requestIsCurrent(controller)) {
        if (!artifactVerificationMatches(result, target)) throw new Error("制品路径、SHA-256或字节数与展示目标不一致，请刷新证据。");
        setArtifactVerification(result);
      }
    } catch (reason) { if (requestIsCurrent(controller)) setError(`制品核验失败：${reason instanceof Error ? reason.message : String(reason)}`); }
    finally { inFlight.delete("artifact/ready"); if (requestIsCurrent(controller)) setArtifactBusy(false); }
  };
  return <div className={`module-stack maintenance-center ${mobileDetail && entry ? "detail-open" : ""}`}>
    <div className="maintenance-workspace">
      <aside className="maintenance-index">
        <header className="maintenance-header"><div className="maintenance-heading"><span className="maintenance-eyebrow">CODEX OPERATIONS</span><h2>维护中心</h2><p>官方 Codex 修复与诊断记录</p></div></header>
        <div className="maintenance-toolbar">
          <label className="maintenance-search"><Search size={16} /><input aria-label="搜索维护变更" placeholder="搜索问题、修改或文件" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
          <div className="maintenance-filter-row">
            <select aria-label="维护分类" value={category} onChange={(event) => setCategory(event.target.value)}><option value="all">全部分类</option>{[...new Set(entries.map((item) => item.category))].map((item) => <option key={item}>{item}</option>)}</select>
            <select aria-label="变更状态" value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">全部状态</option>{Object.entries(statusLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select>
            <span className="maintenance-count">{visibleEntries.length} / {entries.length}</span>
          </div>
        </div>
        <nav className="maintenance-list" aria-label="维护变更清单">{visibleEntries.map((item) => <button type="button" key={item.id} aria-current={entry?.id === item.id ? "true" : undefined} onClick={() => { setSelectedId(item.id); setMobileDetail(true); }}><span className="maintenance-row-top"><span>{item.category}</span><Status status={item.status} /></span><strong>{item.title}</strong><span className="maintenance-row-foot">{item.changes.length} 项修改<ChevronRight size={14} /></span></button>)}</nav>
        <footer className="maintenance-footer"><span>{entries.length} 项记录 · 更新于 {catalogData.updatedAt.slice(0, 10)}</span><small>{catalogData.coverage}</small></footer>
      </aside>
      {entry ? <article className="maintenance-detail" aria-label="变更详情" key={entry.id}>
        <button type="button" className="maintenance-back" onClick={() => setMobileDetail(false)}><ArrowLeft size={16} />返回清单</button>
        <header><div className="maintenance-detail-title"><div className="maintenance-detail-kicker"><span>{entry.category}</span><span>{entry.changes.length} 项修改</span></div><div className="maintenance-detail-heading"><h2>{entry.title}</h2><Status status={entry.status} /></div><p>{entry.summary}</p></div>
          <div className="maintenance-header-actions"><button type="button" disabled={!connected || loading || runtimeBusy} onClick={() => void verifyRuntime()}><ShieldCheck size={16} />{runtimeBusy ? "环境核验中…" : "核验运行环境"}</button><button className="icon-button" type="button" aria-label="刷新本机证据" title="刷新本机证据" disabled={!connected || loading} onClick={() => setRefreshKey((value) => value + 1)}><RefreshCcw size={17} className={loading ? "maintenance-spinning" : ""} /></button></div>
          <div className="maintenance-meta"><div className="maintenance-observation" role="status"><ShieldCheck size={14} /><span>{loading ? "正在核验本机证据…" : observation ? `只读核验 ${new Date(observation.observedAt).toLocaleTimeString()} · 运行态未验证` : "静态变更目录 · 本机状态未验证"}</span></div>
            {connected ? <details className="maintenance-source"><summary>源证据工作区 · {observation?.sourceWorkspaceAvailable ? "已绑定" : "未绑定"}</summary><form onSubmit={(event) => { event.preventDefault(); setSourceWorkspace(sourceWorkspaceDraft.trim()); setRefreshKey((value) => value + 1); }}><label>工作区路径<input aria-label="源证据工作区路径" value={sourceWorkspaceDraft} onChange={(event) => setSourceWorkspaceDraft(event.target.value)} placeholder="包含AGENTS.md和scripts的Codex工作区" /></label><button type="submit">只读绑定</button></form><p>仅核验目录登记的文件；工作区中的变更目录须与此版本完全一致。留空恢复自动检测。</p></details> : null}
          </div>
          {error ? <p className="maintenance-error" role="alert">本机核验不可用：{error}。目录仍可查阅，不代表修复已生效。</p> : null}
          {observation && observation.catalogSha256 !== __COCKPIT_CATALOG_SHA256__ ? <p className="maintenance-error" role="alert">前端与服务的目录指纹不一致；请先更新服务，导出的计划标记为不可执行。</p> : null}
        </header>
        {runtimeObservation ? <div className="maintenance-runtime-summary">{runtimeObservation.checks.map((check) => <button type="button" key={check.id} onClick={() => { setQuery(""); setStatus("all"); setCategory("all"); setSelectedId(check.id); setTab("evidence"); setMobileDetail(true); }}><span>{entries.find((item) => item.id === check.id)?.title ?? check.id}</span><code>{check.state}</code><ChevronRight size={14} /></button>)}</div> : null}
        <div className="maintenance-tabs" role="tablist" aria-label="维护详情分类">{tabs.map(([value, label], index) => <button type="button" role="tab" id={`maintenance-tab-${value}`} aria-selected={tab === value} tabIndex={tab === value ? 0 : -1} aria-controls="maintenance-tabpanel" key={value} onClick={() => setTab(value)} onKeyDown={(event) => {
          const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
          if (next !== null) { event.preventDefault(); setTab(tabs[next][0]); document.getElementById(`maintenance-tab-${tabs[next][0]}`)?.focus(); }
        }}>{label}{value === "changes" ? <span>{entry.changes.length}</span> : null}</button>)}</div>
        <div role="tabpanel" id="maintenance-tabpanel" aria-labelledby={`maintenance-tab-${tab}`} key={`${entry.id}/${tab}`}>
          {(tab === "overview" || tab === "evidence") && runtimeCheck ? <section className="maintenance-detail-section maintenance-current"><h3>运行环境观测</h3><RuntimeEvidenceTimes observation={runtimeObservation!} check={runtimeCheck} /><code>{runtimeCheck.state}</code><p>{runtimeCheck.reason}</p><p className="maintenance-muted">{runtimeCheck.scope}</p><details><summary>检测详情</summary><pre>{JSON.stringify(runtimeCheck.details, null, 2)}</pre></details></section> : null}
          {tab === "overview" ? <><section className="maintenance-recommendation"><span>建议动作</span><p>{entry.nextAction}</p></section><div className="maintenance-overview"><div><section className="maintenance-detail-section"><h3>原因与范围</h3><p>{entry.reason}</p></section><DetailList title="适用对象" items={entry.targets} /></div><aside><DetailList title="风险与边界" items={entry.risks} /></aside></div></> : null}
          {tab === "changes" ? entry.changes.map((change, index) => <section className="maintenance-change" key={index}><h3>变更 {String(index + 1).padStart(2, "0")}</h3><dl><dt>修改前</dt><dd>{change.before}</dd><dt>修改后</dt><dd>{change.after}</dd></dl><ul className="maintenance-paths">{change.files.map((path) => <li key={path}><code>{path}</code></li>)}</ul></section>) : null}
          {tab === "evidence" ? <><DetailList title="验证结果与边界" items={entry.verification} />{preparedPackage ? <section className="maintenance-detail-section maintenance-package"><h3>准备好的安装包</h3><span>{availabilityLabels[preparedPackage.availability] ?? "未知"}</span><code>{preparedPackage.path ?? preparedPackage.reason}</code>{preparedPackage.bytes !== undefined ? <small>{(preparedPackage.bytes / 1024 / 1024).toFixed(1)} MiB</small> : null}<code>记录的 SHA-256：{preparedPackage.expectedSha256 ?? "未提供"}</code><button type="button" disabled={!connected || loading || !canVerifyArtifact(preparedPackage) || artifactBusy} onClick={() => void verifyPackage()}>{artifactBusy ? "核验制品中…" : "核验安装包哈希"}</button>{artifactVerification ? <p role="status" className={artifactVerification.hashMatches ? "maintenance-package-match" : "maintenance-error"}>{artifactVerification.hashMatches ? "本次读取与准备回执哈希一致。未安装，签名和当前运行态仍须另验。" : "哈希不一致，禁止使用该制品。"}</p> : null}</section> : null}<section className="maintenance-detail-section"><h3>证据文件</h3><p className="maintenance-muted">文件存在不代表内容已通过验收；这里不读取原始对话、密钥或日志正文。</p>{entry.evidence.map((record, index) => { const live = liveEntry?.evidence.find((item) => item.path === record.path); return <div className="maintenance-evidence" key={`${record.path}-${index}`}><div><strong>{record.label}</strong><span>{availabilityLabels[live?.availability ?? "unavailable"] ?? "未验证"}</span></div><code>{record.path}</code>{live?.modifiedAt ? <small>{new Date(live.modifiedAt).toLocaleString()} · {live.bytes?.toLocaleString()} bytes</small> : null}<button type="button" disabled={!connected || loading || !catalogMatches || live?.availability !== "present" || hashResults[`${entry.id}/${index}`] === "核验中…"} onClick={() => void verifyEvidence(index)}>核验文件指纹</button>{hashResults[`${entry.id}/${index}`] ? <output>{hashResults[`${entry.id}/${index}`]}</output> : null}</div>; })}</section>
            {entry.id === "desktop-pagination-6511" && observation?.receipts.some((receipt) => receipt.availability === "present") ? <details className="maintenance-receipts"><summary>分页包本机回执（未验证当前进程）</summary>{observation.receipts.map((receipt) => <section key={receipt.kind}><h4>{receipt.kind === "ready" ? "准备回执" : "安装回执"}</h4><pre>{JSON.stringify(receipt.values, null, 2)}</pre></section>)}</details> : null}</> : null}
          {tab === "decision" ? <div className="maintenance-decision-layout"><div><DetailList title="执行前置条件" items={entry.preconditions} /><DetailList title="回滚与恢复" items={entry.rollback} /><section className="maintenance-detail-section"><h3>按操作类型适用的安全门槛</h3>{catalogData.safetyGates.map((gate) => <details className="maintenance-gate" key={gate.title}><summary>{gate.title}</summary><ul>{gate.items.map((item) => <li key={item}>{item}</li>)}</ul></details>)}</section></div><section className="maintenance-detail-section maintenance-decision-form"><h3>决策记录</h3><label className="maintenance-field">处理意向<select value={decision.decision} onChange={(event) => updateDecision({ decision: event.target.value })}><option value="review">继续核验</option><option value="defer">暂缓</option><option value="plan">准备执行计划</option><option value="reject">不采用</option></select></label><label className="maintenance-field">补充意见<textarea value={decision.note} onChange={(event) => updateDecision({ note: event.target.value })} rows={3} maxLength={4000} /></label><p className="maintenance-muted">仅记录计划，不执行安装、重启、历史修改或工作区外写入。意向和意见保留在本次页面中；导出后留档。</p><button className="primary-button" type="button" onClick={() => downloadPlan(entry, observation, decision.decision, decision.note, entry.id === "desktop-pagination-6511" ? artifactVerification : null, Object.entries(fingerprints).filter(([key]) => key.startsWith(`${entry.id}/`)).map(([, value]) => value), runtimeObservation)}><ArrowDownToLine size={16} />导出决策计划</button></section></div> : null}
        </div>
      </article> : <div className="maintenance-empty"><Search size={24} /><h3>没有匹配的变更</h3><button type="button" onClick={() => { setQuery(""); setCategory("all"); setStatus("all"); }}>清除筛选</button></div>}
    </div>
  </div>;
}
