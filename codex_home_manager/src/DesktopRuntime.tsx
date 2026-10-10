import React from "react";
import { CheckCircle2, LoaderCircle, RefreshCcw, ServerCrash } from "lucide-react";

export type DesktopServiceSnapshot = {
  state: "starting" | "ready" | "conflict" | "failed" | "stopped" | string;
  detail: string;
  owned: boolean;
  version: string | null;
  apiBaseUrl: string;
  updatedAtMs: number;
};

type DesktopRuntimeState = {
  isDesktop: boolean;
  service: DesktopServiceSnapshot | null;
  restartService: () => Promise<void>;
};

export function isDesktopRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function useDesktopRuntime(): DesktopRuntimeState {
  const isDesktop = isDesktopRuntime();
  const [service, setService] = React.useState<DesktopServiceSnapshot | null>(null);

  React.useEffect(() => {
    if (!isDesktop) return undefined;
    let disposed = false;
    let removeListener: (() => void) | undefined;

    void (async () => {
      const [{ invoke }, { listen }] = await Promise.all([
        import("@tauri-apps/api/core"),
        import("@tauri-apps/api/event")
      ]);
      const initial = await invoke<DesktopServiceSnapshot>("desktop_service_status");
      if (!disposed) setService(initial);
      removeListener = await listen<DesktopServiceSnapshot>("desktop-service-status", (event) => {
        if (!disposed) setService(event.payload);
      });
      if (disposed) removeListener();
    })().catch((error) => {
      if (!disposed) {
        setService({
          state: "failed",
          detail: error instanceof Error ? error.message : String(error),
          owned: false,
          version: null,
          apiBaseUrl: "http://127.0.0.1:8765",
          updatedAtMs: Date.now()
        });
      }
    });

    return () => {
      disposed = true;
      removeListener?.();
    };
  }, [isDesktop]);

  const restartService = React.useCallback(async () => {
    if (!isDesktop) return;
    setService((current) => current
      ? { ...current, state: "starting", detail: "正在重新启动本机服务" }
      : current);
    const { invoke } = await import("@tauri-apps/api/core");
    const next = await invoke<DesktopServiceSnapshot>("desktop_restart_service");
    setService(next);
  }, [isDesktop]);

  return { isDesktop, service, restartService };
}

function desktopServiceLabel(service: DesktopServiceSnapshot | null, language: "zh" | "en"): string {
  if (!service || service.state === "starting") return language === "en" ? "Starting service" : "正在启动服务";
  if (service.state === "ready") {
    const ownership = service.owned
      ? (language === "en" ? "Built-in service" : "内置服务")
      : (language === "en" ? "Local service" : "本机服务");
    return service.version ? `${ownership} ${service.version}` : ownership;
  }
  return language === "en" ? "Service unavailable" : "服务不可用";
}

export function DesktopServiceControl({
  service,
  language,
  onRestart
}: {
  service: DesktopServiceSnapshot | null;
  language: "zh" | "en";
  onRestart: () => Promise<void>;
}) {
  const ready = service?.state === "ready";
  const starting = !service || service.state === "starting";
  const Icon = ready ? CheckCircle2 : starting ? LoaderCircle : ServerCrash;
  return (
    <button
      className={`desktop-service-control ${ready ? "ready" : starting ? "starting" : "failed"}`}
      type="button"
      onClick={() => { if (!ready && !starting) void onRestart(); }}
      title={service?.detail || desktopServiceLabel(service, language)}
      aria-label={ready || starting
        ? desktopServiceLabel(service, language)
        : (language === "en" ? "Restart local service" : "重启本机服务")}
    >
      <Icon size={15} className={starting ? "spin" : ""} aria-hidden="true" />
      <span>{desktopServiceLabel(service, language)}</span>
      {!ready && !starting ? <RefreshCcw size={14} aria-hidden="true" /> : null}
    </button>
  );
}

export function DesktopServiceGate({
  service,
  connectionStatus,
  connectionMessage,
  language,
  onRestart
}: {
  service: DesktopServiceSnapshot | null;
  connectionStatus: "checking" | "connected" | "launching" | "blocked";
  connectionMessage: string;
  language: "zh" | "en";
  onRestart: () => Promise<void>;
}) {
  const serviceReady = service?.state === "ready";
  const starting = !service || service.state === "starting" || (serviceReady && connectionStatus === "checking");
  const connectionFailed = serviceReady && connectionStatus === "blocked";
  const detail = connectionFailed ? connectionMessage : service?.detail;
  return (
    <section className="desktop-service-gate" aria-live="polite">
      <div className={`desktop-service-gate-icon ${starting ? "starting" : "failed"}`}>
        {starting ? <LoaderCircle className="spin" size={26} /> : <ServerCrash size={26} />}
      </div>
      <div>
        <h2>{starting
          ? (language === "en" ? "Preparing your local workspace" : "正在准备本机工作区")
          : connectionFailed
            ? (language === "en" ? "Could not connect to local data" : "无法连接本机数据")
            : (language === "en" ? "The local service could not start" : "本机服务未能启动")}</h2>
        <p>{detail || (language === "en" ? "Starting the bundled service..." : "正在启动内置服务...")}</p>
      </div>
      {!starting ? (
        <button className="top-action" type="button" onClick={() => void onRestart()}>
          <RefreshCcw size={16} />
          {language === "en" ? "Retry" : "重试"}
        </button>
      ) : null}
    </section>
  );
}
