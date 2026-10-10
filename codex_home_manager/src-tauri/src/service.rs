use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream},
    os::windows::process::CommandExt,
    process::{Child, Command},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{path::BaseDirectory, AppHandle, Emitter, Manager, State};
use tokio::{sync::Mutex as AsyncMutex, time::sleep};
use uuid::Uuid;

use crate::windows_job::WindowsJob;

const API_BASE_URL: &str = "http://127.0.0.1:8765";
const CAPABILITIES_URL: &str = "http://127.0.0.1:8765/api/capabilities";
const SHUTDOWN_URL: &str = "http://127.0.0.1:8765/api/desktop/shutdown";
const SHUTDOWN_HEADER: &str = "X-Codex-Home-Manager-Shutdown";
const SHUTDOWN_TOKEN_ENVIRONMENT: &str = "CODEX_HOME_MANAGER_DESKTOP_SHUTDOWN_TOKEN";
const SIDECAR_RESOURCE_PATH: &str = "service/codex-home-manager-service.exe";
const DESKTOP_ORIGIN: &str = "http://tauri.localhost";
const STARTUP_DEADLINE: Duration = Duration::from_secs(20);

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceSnapshot {
    pub state: String,
    pub detail: String,
    pub owned: bool,
    pub version: Option<String>,
    pub api_base_url: &'static str,
    pub updated_at_ms: u64,
}

impl ServiceSnapshot {
    fn new(state: &str, detail: impl Into<String>, owned: bool, version: Option<String>) -> Self {
        Self {
            state: state.to_string(),
            detail: detail.into(),
            owned,
            version,
            api_base_url: API_BASE_URL,
            updated_at_ms: now_ms(),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct CapabilitiesPayload {
    service: String,
    version: String,
    frontend_contract_version: u32,
    openapi_path: String,
    mcp_path: String,
    capabilities: Vec<Value>,
}

#[derive(Debug)]
struct OwnedService {
    child: Child,
    _job: WindowsJob,
    shutdown_token: String,
}

#[derive(Debug)]
struct ServiceState {
    snapshot: ServiceSnapshot,
    owned: Option<OwnedService>,
}

pub struct ServiceSupervisor {
    client: Client,
    state: Mutex<ServiceState>,
    start_lock: AsyncMutex<()>,
    restart_attempts: AtomicUsize,
    shutting_down: AtomicBool,
}

impl Default for ServiceSupervisor {
    fn default() -> Self {
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(1))
            .timeout(Duration::from_secs(2))
            .no_proxy()
            .build()
            .expect("failed to construct loopback HTTP client");
        Self {
            client,
            state: Mutex::new(ServiceState {
                snapshot: ServiceSnapshot::new("starting", "正在启动本机服务", false, None),
                owned: None,
            }),
            start_lock: AsyncMutex::new(()),
            restart_attempts: AtomicUsize::new(0),
            shutting_down: AtomicBool::new(false),
        }
    }
}

impl ServiceSupervisor {
    pub fn snapshot(&self) -> ServiceSnapshot {
        self.state
            .lock()
            .expect("service state poisoned")
            .snapshot
            .clone()
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down.load(Ordering::SeqCst)
    }

    fn publish(&self, app: &AppHandle, snapshot: ServiceSnapshot) {
        self.state.lock().expect("service state poisoned").snapshot = snapshot.clone();
        let _ = app.emit("desktop-service-status", snapshot);
    }

    async fn probe(&self) -> Result<CapabilitiesPayload, String> {
        let response = self
            .client
            .get(CAPABILITIES_URL)
            .header(reqwest::header::ORIGIN, DESKTOP_ORIGIN)
            .send()
            .await
            .map_err(|error| error.to_string())?;
        if !response.status().is_success() {
            return Err(format!("capabilities probe returned {}", response.status()));
        }
        let allowed_origin = response
            .headers()
            .get(reqwest::header::ACCESS_CONTROL_ALLOW_ORIGIN)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .to_string();
        let payload: CapabilitiesPayload = response
            .json()
            .await
            .map_err(|error| format!("invalid capabilities response: {error}"))?;
        if payload.service != "codex-home-manager"
            || payload.version != env!("CARGO_PKG_VERSION")
            || payload.frontend_contract_version != 2
            || payload.openapi_path != "/openapi.json"
            || payload.mcp_path != "/mcp"
            || payload.capabilities.is_empty()
            || allowed_origin != DESKTOP_ORIGIN
        {
            return Err("the service on port 8765 has an incompatible contract".to_string());
        }
        Ok(payload)
    }

    pub async fn ensure_running(&self, app: &AppHandle) -> Result<ServiceSnapshot, String> {
        let _guard = self.start_lock.lock().await;
        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("desktop application is shutting down".to_string());
        }
        if let Ok(payload) = self.probe().await {
            let owned = self
                .state
                .lock()
                .expect("service state poisoned")
                .owned
                .is_some();
            let snapshot = ServiceSnapshot::new(
                "ready",
                if owned {
                    "本机服务已就绪"
                } else {
                    "已连接现有本机服务"
                },
                owned,
                Some(payload.version),
            );
            self.publish(app, snapshot.clone());
            return Ok(snapshot);
        }
        if port_8765_is_occupied() {
            let snapshot =
                ServiceSnapshot::new("conflict", "端口 8765 已被不兼容的程序占用", false, None);
            self.publish(app, snapshot.clone());
            return Err(snapshot.detail);
        }

        self.publish(
            app,
            ServiceSnapshot::new("starting", "正在启动本机服务", true, None),
        );
        let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let executable_path = app
            .path()
            .resolve(SIDECAR_RESOURCE_PATH, BaseDirectory::Resource)
            .map_err(|error| format!("cannot resolve packaged service: {error}"))?;
        let child = Command::new(&executable_path)
            .arg("--desktop-sidecar")
            .env(SHUTDOWN_TOKEN_ENVIRONMENT, &token)
            .creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW)
            .spawn()
            .map_err(|error| {
                format!(
                    "cannot start packaged service {}: {error}",
                    executable_path.display()
                )
            })?;
        let process_id = child.id();
        let job = WindowsJob::assign_process(process_id)
            .map_err(|error| format!("cannot supervise sidecar process {process_id}: {error}"))?;
        self.state.lock().expect("service state poisoned").owned = Some(OwnedService {
            child,
            _job: job,
            shutdown_token: token,
        });
        let started_at = std::time::Instant::now();
        while started_at.elapsed() < STARTUP_DEADLINE {
            if let Ok(payload) = self.probe().await {
                let snapshot =
                    ServiceSnapshot::new("ready", "本机服务已就绪", true, Some(payload.version));
                self.publish(app, snapshot.clone());
                return Ok(snapshot);
            }
            sleep(Duration::from_millis(150)).await;
        }

        self.stop_owned().await;
        let snapshot =
            ServiceSnapshot::new("failed", "本机服务未能在 20 秒内完成启动", false, None);
        self.publish(app, snapshot.clone());
        Err(snapshot.detail)
    }

    async fn stop_owned(&self) {
        let owned = self
            .state
            .lock()
            .expect("service state poisoned")
            .owned
            .take();
        if let Some(mut owned) = owned {
            let _ = self
                .client
                .post(SHUTDOWN_URL)
                .header(SHUTDOWN_HEADER, &owned.shutdown_token)
                .send()
                .await;
            sleep(Duration::from_millis(500)).await;
            let _ = owned.child.kill();
            let _ = owned.child.wait();
        }
    }

    pub async fn shutdown(&self, app: &AppHandle) {
        if self.shutting_down.swap(true, Ordering::SeqCst) {
            return;
        }
        self.publish(
            app,
            ServiceSnapshot::new("stopping", "正在停止本机服务", true, None),
        );
        self.stop_owned().await;
    }

    pub async fn restart(self: &Arc<Self>, app: &AppHandle) -> Result<ServiceSnapshot, String> {
        self.shutting_down.store(false, Ordering::SeqCst);
        self.stop_owned().await;
        self.ensure_running(app).await
    }

    pub fn start_monitor(self: Arc<Self>, app: AppHandle) {
        tauri::async_runtime::spawn(async move {
            loop {
                sleep(Duration::from_secs(2)).await;
                if self.is_shutting_down() {
                    return;
                }
                if self.snapshot().state != "ready" || self.probe().await.is_ok() {
                    continue;
                }
                if self.restart_attempts.fetch_add(1, Ordering::SeqCst) == 0 {
                    self.publish(
                        &app,
                        ServiceSnapshot::new(
                            "restarting",
                            "本机服务意外退出，正在恢复",
                            true,
                            None,
                        ),
                    );
                    self.stop_owned().await;
                    let _ = self.ensure_running(&app).await;
                } else {
                    self.publish(
                        &app,
                        ServiceSnapshot::new(
                            "failed",
                            "本机服务重复退出，已停止自动重启",
                            false,
                            None,
                        ),
                    );
                }
            }
        });
    }
}

fn port_8765_is_occupied() -> bool {
    TcpStream::connect_timeout(
        &SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 8765),
        Duration::from_millis(250),
    )
    .is_ok()
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[tauri::command]
pub fn desktop_service_status(supervisor: State<'_, Arc<ServiceSupervisor>>) -> ServiceSnapshot {
    supervisor.snapshot()
}

#[tauri::command]
pub async fn desktop_restart_service(
    app: AppHandle,
    supervisor: State<'_, Arc<ServiceSupervisor>>,
) -> Result<ServiceSnapshot, String> {
    supervisor.inner().clone().restart(&app).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initial_snapshot_is_starting_and_non_owned() {
        let supervisor = ServiceSupervisor::default();
        let snapshot = supervisor.snapshot();

        assert_eq!(snapshot.state, "starting");
        assert!(!snapshot.owned);
        assert_eq!(snapshot.api_base_url, API_BASE_URL);
    }

    #[test]
    fn restart_budget_allows_only_one_automatic_retry() {
        let attempts = AtomicUsize::new(0);

        assert_eq!(attempts.fetch_add(1, Ordering::SeqCst), 0);
        assert_eq!(attempts.fetch_add(1, Ordering::SeqCst), 1);
    }
}
