#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]
#![recursion_limit = "256"]

use std::{
    collections::HashSet,
    net::{Ipv4Addr, SocketAddr},
    path::{Path, PathBuf},
    process::Stdio,
    sync::{Arc, Mutex as StdMutex},
    time::Duration,
};

use axum::{
    body::Body,
    extract::{ConnectInfo, Query, Request, State},
    http::{header, HeaderMap, HeaderValue, Method, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::{
    process::{Child, Command},
    sync::{watch, Mutex},
    time::{sleep, Instant},
};
use tower_http::cors::{Any, CorsLayer};
use uuid::Uuid;

use codex_home_manager_service::provider_interop;

mod catalog;
mod daily_tokens;
mod global_search;
mod history;
mod pagination_patch;
mod prompts;

use catalog::{health, native_thread_detail, snapshot_summary, thread_page, ThreadCatalogStore};
use daily_tokens::{thread_daily_tokens, DailyTokenStore};
use global_search::{
    cancel_global_thread_content_search_request, cancel_thread_timeline_search_request,
    global_thread_content_search_page, thread_timeline_search_page, GlobalSearchStore,
};
use history::{thread_timeline, thread_timeline_item};
use prompts::{
    cancel_thread_prompt_request, native_prompt_index_status, thread_prompt_context,
    thread_prompt_page, PromptStore,
};

const LISTEN_PORT: u16 = 8765;
const LEGACY_PORT: u16 = 8766;
const SERVICE_NAME: &str = "codex-home-manager";
const SERVICE_VERSION: &str = env!("CARGO_PKG_VERSION");
const SHUTDOWN_HEADER: &str = "X-Codex-Home-Manager-Shutdown";
const SHUTDOWN_TOKEN_ENVIRONMENT: &str = "CODEX_HOME_MANAGER_DESKTOP_SHUTDOWN_TOKEN";
const LEGACY_REQUEST_TIMEOUT: Duration = Duration::from_secs(120);
const PROVIDER_INTEROP_REQUEST_TIMEOUT: Duration = Duration::from_secs(15 * 60);
const LEGACY_READ_RETRY_DELAYS: [Duration; 4] = [
    Duration::from_millis(75),
    Duration::from_millis(200),
    Duration::from_millis(500),
    Duration::from_millis(1_000),
];

#[derive(Clone)]
struct AppState {
    legacy: Arc<LegacySupervisor>,
    legacy_activity: Arc<StdMutex<LegacyActivity>>,
    catalog: Arc<ThreadCatalogStore>,
    prompts: Arc<PromptStore>,
    daily_tokens: Arc<DailyTokenStore>,
    global_search: Arc<GlobalSearchStore>,
    frontend_root: Arc<PathBuf>,
    frontend_files: Arc<HashSet<String>>,
    shutdown_token: Arc<str>,
    shutdown_tx: watch::Sender<bool>,
}

#[derive(Clone, Default, Serialize)]
#[serde(rename_all = "camelCase")]
struct LegacyActivity {
    request_count: u64,
    last_path: Option<String>,
}

#[derive(Deserialize)]
struct FrontendReleaseManifest {
    files: Vec<String>,
}

struct LegacyProcess {
    child: Child,
    shutdown_token: String,
}

struct LegacySupervisor {
    client: Client,
    executable: PathBuf,
    process: Mutex<Option<LegacyProcess>>,
    start_lock: Mutex<()>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Capability {
    name: &'static str,
    method: &'static str,
    path: &'static str,
    purpose: &'static str,
    required: Vec<&'static str>,
    backup: &'static str,
    body_example: Option<Value>,
    success_fields: Vec<&'static str>,
    rollback: Option<&'static str>,
    risk_level: &'static str,
    preview_endpoint: Option<&'static str>,
    write_endpoint: Option<&'static str>,
    idempotency: &'static str,
    rollback_mode: Option<&'static str>,
}

#[derive(Deserialize)]
struct CapabilityQuery {
    lang: Option<String>,
}

impl LegacySupervisor {
    fn new(executable: PathBuf) -> Result<Self, String> {
        let client = Client::builder()
            .connect_timeout(Duration::from_millis(250))
            .timeout(LEGACY_REQUEST_TIMEOUT)
            .pool_max_idle_per_host(1)
            .no_proxy()
            .build()
            .map_err(|error| format!("cannot build legacy HTTP client: {error}"))?;
        Ok(Self {
            client,
            executable,
            process: Mutex::new(None),
            start_lock: Mutex::new(()),
        })
    }

    async fn probe(&self) -> bool {
        let response = self
            .client
            .get(format!("http://127.0.0.1:{LEGACY_PORT}/api/capabilities"))
            .timeout(Duration::from_millis(500))
            .send()
            .await;
        match response {
            Ok(response) if response.status().is_success() => response.bytes().await.is_ok(),
            _ => false,
        }
    }

    async fn ensure_running(&self) -> Result<(), String> {
        if self.probe().await {
            return Ok(());
        }
        let _start_guard = self.start_lock.lock().await;
        if self.probe().await {
            return Ok(());
        }
        if !self.executable.is_file() {
            return Err(format!(
                "legacy compatibility service is missing: {}",
                self.executable.display()
            ));
        }

        let token = Uuid::new_v4().simple().to_string();
        let mut command = Command::new(&self.executable);
        command
            .arg("--desktop-sidecar")
            .env("CODEX_HOME_MANAGER_PORT", LEGACY_PORT.to_string())
            .env(SHUTDOWN_TOKEN_ENVIRONMENT, &token)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(target_os = "windows")]
        command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
        let child = command.spawn().map_err(|error| {
            format!(
                "cannot start legacy compatibility service {}: {error}",
                self.executable.display()
            )
        })?;
        *self.process.lock().await = Some(LegacyProcess {
            child,
            shutdown_token: token,
        });

        let deadline = Instant::now() + Duration::from_secs(20);
        while Instant::now() < deadline {
            if self.probe().await {
                return Ok(());
            }
            sleep(Duration::from_millis(100)).await;
        }
        self.stop().await;
        Err("legacy compatibility service did not become ready within 20 seconds".to_string())
    }

    async fn stop(&self) {
        let process = self.process.lock().await.take();
        if let Some(mut process) = process {
            let _ = self
                .client
                .post(format!(
                    "http://127.0.0.1:{LEGACY_PORT}/api/desktop/shutdown"
                ))
                .header(SHUTDOWN_HEADER, &process.shutdown_token)
                .timeout(Duration::from_millis(750))
                .send()
                .await;
            if tokio::time::timeout(Duration::from_secs(2), process.child.wait())
                .await
                .is_err()
            {
                let _ = process.child.kill().await;
                let _ = process.child.wait().await;
            }
        }
    }

    async fn capabilities(&self, language: &str) -> Result<Response, String> {
        self.ensure_running().await?;
        let language = if language.eq_ignore_ascii_case("zh") {
            "zh"
        } else {
            "en"
        };
        let response = self
            .client
            .get(format!(
                "http://127.0.0.1:{LEGACY_PORT}/api/capabilities?lang={language}"
            ))
            .send()
            .await
            .map_err(|error| format!("legacy capabilities request failed: {error}"))?;
        let status = response.status();
        let content_type = response.headers().get(header::CONTENT_TYPE).cloned();
        let body = Body::from_stream(response.bytes_stream());
        let mut result = Response::builder().status(status);
        if let Some(content_type) = content_type {
            result = result.header(header::CONTENT_TYPE, content_type);
        }
        result
            .body(body)
            .map_err(|error| format!("cannot build capabilities response: {error}"))
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args().nth(1).as_deref() == Some("provider-interop-worker") {
        std::process::exit(provider_interop::run_worker_stdio());
    }
    let shutdown_token = std::env::var(SHUTDOWN_TOKEN_ENVIRONMENT)
        .map_err(|_| format!("{SHUTDOWN_TOKEN_ENVIRONMENT} is required"))?;
    let current_executable = std::env::current_exe()?;
    let legacy_executable = resolve_legacy_executable(&current_executable);
    let frontend_root = resolve_frontend_root(&current_executable);
    let frontend_files = load_frontend_file_allowlist(&frontend_root)?;
    let (shutdown_tx, mut shutdown_rx) = watch::channel(false);
    let state = AppState {
        legacy: Arc::new(LegacySupervisor::new(legacy_executable)?),
        legacy_activity: Arc::new(StdMutex::new(LegacyActivity::default())),
        catalog: Arc::new(ThreadCatalogStore::default()),
        prompts: Arc::new(PromptStore::default()),
        daily_tokens: Arc::new(DailyTokenStore::default()),
        global_search: Arc::new(GlobalSearchStore::default()),
        frontend_root: Arc::new(frontend_root),
        frontend_files: Arc::new(frontend_files),
        shutdown_token: Arc::from(shutdown_token),
        shutdown_tx,
    };
    let cors = CorsLayer::new()
        .allow_methods([
            Method::GET,
            Method::POST,
            Method::PUT,
            Method::PATCH,
            Method::DELETE,
        ])
        .allow_headers(Any)
        .allow_origin([
            "http://tauri.localhost".parse::<HeaderValue>()?,
            "http://127.0.0.1:8765".parse::<HeaderValue>()?,
            "http://localhost:8765".parse::<HeaderValue>()?,
        ]);
    let app = Router::new()
        .route("/api/capabilities", get(capabilities))
        .route("/api/native/runtime", get(native_runtime))
        .route("/api/native/routes", get(native_routes))
        .route("/api/health", get(health))
        .route("/api/native/threads/{thread_id}", get(native_thread_detail))
        .route("/api/threads/{thread_id}/timeline", get(thread_timeline))
        .route(
            "/api/threads/{thread_id}/daily-tokens",
            get(thread_daily_tokens),
        )
        .route(
            "/api/threads/{thread_id}/prompts/page",
            get(thread_prompt_page),
        )
        .route(
            "/api/threads/{thread_id}/prompts/context",
            get(thread_prompt_context),
        )
        .route(
            "/api/threads/{thread_id}/prompts/requests/{request_id}",
            delete(cancel_thread_prompt_request),
        )
        .route("/api/prompt-index/status", get(native_prompt_index_status))
        .route(
            "/api/search/content/page",
            get(global_thread_content_search_page),
        )
        .route(
            "/api/search/content/requests/{request_id}",
            delete(cancel_global_thread_content_search_request),
        )
        .route(
            "/api/threads/{thread_id}/timeline/search/page",
            get(thread_timeline_search_page),
        )
        .route(
            "/api/threads/{thread_id}/timeline/search/requests/{request_id}",
            delete(cancel_thread_timeline_search_request),
        )
        .route(
            "/api/threads/{thread_id}/timeline/item",
            get(thread_timeline_item),
        )
        .route("/api/snapshot/summary", get(snapshot_summary))
        .route("/api/threads/page", get(thread_page))
        .route("/api/desktop/shutdown", post(shutdown))
        .fallback(proxy_legacy)
        .layer(cors)
        .with_state(state.clone());

    let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, LISTEN_PORT)).await?;
    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(async move {
        while !*shutdown_rx.borrow() {
            if shutdown_rx.changed().await.is_err() {
                break;
            }
        }
    })
    .await?;
    state.legacy.stop().await;
    Ok(())
}

fn resolve_legacy_executable(current_executable: &Path) -> PathBuf {
    current_executable
        .parent()
        .and_then(Path::parent)
        .unwrap_or_else(|| Path::new("."))
        .join("legacy")
        .join("codex-home-manager-legacy.exe")
}

fn resolve_frontend_root(current_executable: &Path) -> PathBuf {
    if let Some(configured) = std::env::var_os("CODEX_HOME_MANAGER_FRONTEND_ROOT") {
        return PathBuf::from(configured);
    }
    let packaged = current_executable
        .parent()
        .and_then(Path::parent)
        .unwrap_or_else(|| Path::new("."))
        .join("web");
    if packaged.join("index.html").is_file() {
        return packaged;
    }
    current_executable
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .and_then(Path::parent)
        .unwrap_or_else(|| Path::new("."))
        .join("build")
        .join("frontend-release")
}

fn load_frontend_file_allowlist(
    root: &Path,
) -> Result<HashSet<String>, Box<dyn std::error::Error>> {
    let manifest_path = root.join("frontend-release-manifest.json");
    let manifest: FrontendReleaseManifest =
        serde_json::from_slice(&std::fs::read(&manifest_path)?)?;
    let files = manifest
        .files
        .into_iter()
        .filter(|value| {
            let path = Path::new(value);
            !value.is_empty()
                && !path.is_absolute()
                && path
                    .components()
                    .all(|component| matches!(component, std::path::Component::Normal(_)))
        })
        .collect::<HashSet<_>>();
    if !files.contains("index.html") || !files.contains("favicon.svg") {
        return Err(format!(
            "frontend release manifest is incomplete: {}",
            manifest_path.display()
        )
        .into());
    }
    Ok(files)
}

async fn capabilities(
    State(state): State<AppState>,
    Query(query): Query<CapabilityQuery>,
) -> Response {
    if let Some(language) = query.lang.as_deref() {
        return match state.legacy.capabilities(language).await {
            Ok(response) => response,
            Err(error) => (
                StatusCode::BAD_GATEWAY,
                Json(json!({"detail": error, "runtime": "rust", "legacyMode": "unavailable"})),
            )
                .into_response(),
        };
    }
    Json(native_capabilities()).into_response()
}

fn native_capabilities() -> Value {
    let capability = Capability {
        name: "native_runtime",
        method: "GET",
        path: "/api/native/runtime",
        purpose: "Report the native Rust runtime and migration state.",
        required: Vec::new(),
        backup: "read-only",
        body_example: None,
        success_fields: vec!["runtime", "legacyMode"],
        rollback: None,
        risk_level: "read",
        preview_endpoint: None,
        write_endpoint: None,
        idempotency: "idempotent read",
        rollback_mode: None,
    };
    json!({
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "frontendContractVersion": 2,
        "language": "en",
        "openapiPath": "/openapi.json",
        "mcpPath": "/mcp",
        "runtime": { "core": "rust", "legacyMode": "on-demand" },
        "capabilities": [capability],
    })
}

const NATIVE_ROUTE_PATTERNS: [&str; 18] = [
    "/api/capabilities",
    "/api/native/runtime",
    "/api/native/routes",
    "/api/health",
    "/api/native/threads/{thread_id}",
    "/api/threads/{thread_id}/timeline",
    "/api/threads/{thread_id}/timeline/item",
    "/api/threads/{thread_id}/prompts/page",
    "/api/threads/{thread_id}/prompts/context",
    "/api/threads/{thread_id}/prompts/requests/{request_id}",
    "/api/prompt-index/status",
    "/api/search/content/page",
    "/api/search/content/requests/{request_id}",
    "/api/threads/{thread_id}/timeline/search/page",
    "/api/threads/{thread_id}/timeline/search/requests/{request_id}",
    "/api/threads/{thread_id}/daily-tokens",
    "/api/snapshot/summary",
    "/api/threads/page",
];

fn route_manifest() -> Value {
    json!({
        "schemaVersion": 1,
        "native": NATIVE_ROUTE_PATTERNS,
        "compatibility": {
            "runtime": "python",
            "mode": "on-demand",
            "port": LEGACY_PORT,
            "policy": "unmigrated-routes-only",
        },
    })
}

async fn native_routes() -> Json<Value> {
    Json(route_manifest())
}

async fn native_runtime(State(state): State<AppState>) -> Json<Value> {
    let legacy_activity = state
        .legacy_activity
        .lock()
        .map(|activity| activity.clone())
        .unwrap_or_default();
    Json(json!({
        "runtime": "rust",
        "version": SERVICE_VERSION,
        "legacyMode": "on-demand",
        "legacyRunning": state.legacy.probe().await,
        "legacyRequestCount": legacy_activity.request_count,
        "lastLegacyPath": legacy_activity.last_path,
        "routes": route_manifest(),
    }))
}

async fn shutdown(
    State(state): State<AppState>,
    ConnectInfo(remote): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
) -> Response {
    let supplied = headers
        .get(SHUTDOWN_HEADER)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    if !remote.ip().is_loopback() || supplied.as_bytes() != state.shutdown_token.as_bytes() {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "shutdown authorization failed"})),
        )
            .into_response();
    }
    let _ = state.shutdown_tx.send(true);
    (StatusCode::OK, Json(json!({"status": "stopping"}))).into_response()
}

async fn proxy_legacy(State(state): State<AppState>, request: Request) -> Response {
    if static_frontend_path(request.method(), request.uri().path()).is_some() {
        return serve_frontend(
            &state.frontend_root,
            &state.frontend_files,
            request.uri().path(),
        )
        .await;
    }
    if !legacy_route_is_allowed(request.method(), request.uri().path()) {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({
                "detail": "route is not registered by the native or compatibility runtime",
                "runtime": "rust",
            })),
        )
            .into_response();
    }
    if let Ok(mut activity) = state.legacy_activity.lock() {
        activity.request_count = activity.request_count.saturating_add(1);
        activity.last_path = Some(request.uri().path().to_string());
    }
    if let Err(error) = state.legacy.ensure_running().await {
        return (
            StatusCode::BAD_GATEWAY,
            Json(json!({"detail": error, "runtime": "rust", "legacyMode": "unavailable"})),
        )
            .into_response();
    }

    let (parts, body) = request.into_parts();
    let target = format!("http://127.0.0.1:{LEGACY_PORT}{}", parts.uri);
    let request_timeout = legacy_proxy_timeout(parts.uri.path());
    let response_result = if legacy_method_is_idempotent(&parts.method) {
        send_idempotent_legacy_request(
            &state.legacy.client,
            parts.method,
            target,
            &parts.headers,
            request_timeout,
        )
        .await
    } else {
        let mut outbound = state
            .legacy
            .client
            .request(parts.method, target)
            .timeout(request_timeout)
            .header(header::CONNECTION, "close")
            .body(reqwest::Body::wrap_stream(body.into_data_stream()));
        for (name, value) in &parts.headers {
            if should_forward_proxy_header(name) {
                outbound = outbound.header(name, value);
            }
        }
        outbound.send().await
    };
    let response = match response_result {
        Ok(response) => response,
        Err(error) => {
            return (
                StatusCode::BAD_GATEWAY,
                Json(json!({"detail": format!("legacy request failed: {error}")})),
            )
                .into_response();
        }
    };
    let status = response.status();
    let headers = response.headers().clone();
    let mut result = Response::builder().status(status);
    for (name, value) in &headers {
        if name != header::TRANSFER_ENCODING && name != header::CONTENT_LENGTH {
            result = result.header(name, value);
        }
    }
    result = result.header("x-codex-home-manager-runtime", "legacy-python");
    result
        .body(Body::from_stream(response.bytes_stream()))
        .unwrap_or_else(|error| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"detail": format!("cannot build proxy response: {error}")})),
            )
                .into_response()
        })
}

fn legacy_route_is_allowed(method: &Method, path: &str) -> bool {
    if matches!(
        path,
        "/openapi.json" | "/docs" | "/docs/oauth2-redirect" | "/redoc"
    ) {
        return method == Method::GET;
    }
    if path == "/mcp" {
        return method == Method::GET || method == Method::POST;
    }
    if path.starts_with("/api/provider-interoperability/") {
        return method == Method::GET || method == Method::POST;
    }
    if matches!(
        path,
        "/api/maintenance/status" | "/api/maintenance/runtime" | "/api/maintenance/artifact/ready"
    ) || path.starts_with("/api/maintenance/evidence/")
    {
        return method == Method::GET;
    }
    if path.starts_with("/api/codex/official-thread-tools/repair") {
        return method == Method::POST;
    }
    if path.starts_with("/api/resources/")
        || path.starts_with("/api/import/")
        || path.starts_with("/api/backups")
        || path.starts_with("/api/projects/")
        || path.starts_with("/api/prompt-index/")
    {
        return method == Method::GET || method == Method::POST;
    }
    if matches!(
        path,
        "/api/diagnostics" | "/api/auth/token" | "/api/home/overview" | "/api/snapshot"
    ) {
        return method == Method::GET;
    }
    legacy_thread_route_is_allowed(method, path)
}

fn static_frontend_path(method: &Method, path: &str) -> Option<PathBuf> {
    if method != Method::GET && method != Method::HEAD {
        return None;
    }
    let relative = match path {
        "/" | "/index.html" => "index.html",
        "/favicon.svg" => "favicon.svg",
        "/cockpit-icon.png" => "cockpit-icon.png",
        value if value.starts_with("/assets/") => value.trim_start_matches('/'),
        _ => return None,
    };
    let candidate = PathBuf::from(relative);
    if candidate
        .components()
        .any(|component| !matches!(component, std::path::Component::Normal(_)))
    {
        return None;
    }
    Some(candidate)
}

async fn serve_frontend(
    root: &Path,
    allowed_files: &HashSet<String>,
    request_path: &str,
) -> Response {
    let Some(relative) = static_frontend_path(&Method::GET, request_path) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let relative_key = relative.to_string_lossy().replace('\\', "/");
    if !allowed_files.contains(&relative_key) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let path = root.join(&relative);
    let bytes = match tokio::fs::read(&path).await {
        Ok(bytes) => bytes,
        Err(_) => {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({
                    "detail": format!("frontend artifact is unavailable: {}", path.display()),
                    "runtime": "rust",
                })),
            )
                .into_response()
        }
    };
    let content_type = match path.extension().and_then(|value| value.to_str()) {
        Some("html") => "text/html; charset=utf-8",
        Some("js") => "text/javascript; charset=utf-8",
        Some("css") => "text/css; charset=utf-8",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("wasm") => "application/wasm",
        _ => "application/octet-stream",
    };
    let cache_control = if request_path.starts_with("/assets/") {
        "public, max-age=31536000, immutable"
    } else {
        "no-store"
    };
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, content_type)
        .header(header::CACHE_CONTROL, cache_control)
        .header(header::CONTENT_SECURITY_POLICY, "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data: blob:; font-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' http://127.0.0.1:8765 ws://127.0.0.1:8765 https://codex-home-manager.simplezion.com")
        .header("x-content-type-options", "nosniff")
        .header("referrer-policy", "no-referrer")
        .header("x-frame-options", "DENY")
        .header("permissions-policy", "camera=(), microphone=(), geolocation=()")
        .header("x-codex-home-manager-runtime", "rust")
        .body(Body::from(bytes))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

fn legacy_thread_route_is_allowed(method: &Method, path: &str) -> bool {
    let Some(rest) = path.strip_prefix("/api/threads/") else {
        return false;
    };
    let mut segments = rest.split('/');
    let thread_id = segments.next().unwrap_or_default();
    if thread_id.is_empty() {
        return false;
    }
    let suffix = segments.collect::<Vec<_>>();
    if method == Method::GET {
        return suffix.is_empty()
            || matches!(
                suffix.as_slice(),
                ["analysis" | "logs" | "prompts" | "action-preview" | "export-prompts"]
            )
            || matches!(suffix.as_slice(), ["prompts", "copy"] | ["slim", "preview"]);
    }
    if method == Method::DELETE {
        return false;
    }
    if method == Method::POST {
        return matches!(
            suffix.as_slice(),
            ["analysis", "cancel"] | ["migrate", "preview"] | ["move-workspace", "preview"]
        ) || matches!(
            suffix.as_slice(),
            ["show"
                | "hide"
                | "repair-user-event"
                | "backup"
                | "archive"
                | "duplicate"
                | "migrate"
                | "move-workspace"
                | "slim"]
        );
    }
    false
}

fn legacy_method_is_idempotent(method: &Method) -> bool {
    method == Method::GET || method == Method::HEAD
}

async fn send_idempotent_legacy_request(
    client: &Client,
    method: Method,
    target: String,
    headers: &HeaderMap,
    request_timeout: Duration,
) -> Result<reqwest::Response, reqwest::Error> {
    for attempt in 0..=LEGACY_READ_RETRY_DELAYS.len() {
        let mut outbound = client
            .request(method.clone(), &target)
            .timeout(request_timeout)
            .header(header::CONNECTION, "close");
        for (name, value) in headers {
            if should_forward_proxy_header(name) {
                outbound = outbound.header(name, value);
            }
        }
        match outbound.send().await {
            Ok(response) => return Ok(response),
            Err(error) if attempt < LEGACY_READ_RETRY_DELAYS.len() && !error.is_timeout() => {
                sleep(LEGACY_READ_RETRY_DELAYS[attempt]).await;
                drop(error);
            }
            Err(error) => return Err(error),
        }
    }
    unreachable!("the bounded legacy retry loop always returns")
}

fn should_forward_proxy_header(name: &axum::http::HeaderName) -> bool {
    name != header::CONTENT_LENGTH && name != header::CONNECTION
}

fn legacy_proxy_timeout(path: &str) -> Duration {
    if path.starts_with("/api/provider-interoperability/") {
        PROVIDER_INTEROP_REQUEST_TIMEOUT
    } else {
        LEGACY_REQUEST_TIMEOUT
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    #[test]
    fn maintenance_routes_are_read_only() {
        for path in [
            "/api/maintenance/status",
            "/api/maintenance/runtime",
            "/api/maintenance/artifact/ready",
            "/api/maintenance/evidence/known/0",
        ] {
            assert!(legacy_route_is_allowed(&Method::GET, path));
            assert!(!legacy_route_is_allowed(&Method::POST, path));
            assert!(!legacy_route_is_allowed(&Method::DELETE, path));
        }
        assert!(!legacy_route_is_allowed(
            &Method::GET,
            "/api/maintenance/execute"
        ));
    }

    #[test]
    fn legacy_executable_is_a_sibling_resource_directory() {
        let native = Path::new(
            r"C:\Program Files\Codex Home Manager\service\codex-home-manager-service.exe",
        );
        assert_eq!(
            resolve_legacy_executable(native),
            PathBuf::from(
                r"C:\Program Files\Codex Home Manager\legacy\codex-home-manager-legacy.exe"
            )
        );
    }

    #[test]
    fn frontend_artifact_is_a_sibling_resource_directory() {
        let root = tempfile::tempdir().unwrap();
        let native = root
            .path()
            .join("resources/service/codex-home-manager-service.exe");
        let web = root.path().join("resources/web");
        std::fs::create_dir_all(&web).unwrap();
        std::fs::write(web.join("index.html"), "ok").unwrap();
        assert_eq!(resolve_frontend_root(&native), web);
        assert_eq!(
            static_frontend_path(&Method::GET, "/assets/app.js"),
            Some(PathBuf::from("assets/app.js"))
        );
        assert_eq!(
            static_frontend_path(&Method::GET, "/assets/../secret"),
            None
        );
        assert_eq!(static_frontend_path(&Method::GET, "/cockpit-icon.png"), Some(PathBuf::from("cockpit-icon.png")));
        assert_eq!(static_frontend_path(&Method::HEAD, "/cockpit-icon.png"), Some(PathBuf::from("cockpit-icon.png")));
        assert_eq!(static_frontend_path(&Method::POST, "/cockpit-icon.png"), None);
    }

    #[tokio::test]
    async fn frontend_static_files_require_the_release_manifest_and_security_headers() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("assets")).unwrap();
        std::fs::write(root.path().join("index.html"), "ok").unwrap();
        std::fs::write(root.path().join("favicon.svg"), "svg").unwrap();
        std::fs::write(root.path().join("cockpit-icon.png"), b"png").unwrap();
        std::fs::write(root.path().join("assets/current.js"), "current").unwrap();
        std::fs::write(root.path().join("assets/stale.js"), "stale").unwrap();
        std::fs::write(
            root.path().join("frontend-release-manifest.json"),
            r#"{"files":["index.html","favicon.svg","cockpit-icon.png","assets/current.js"]}"#,
        )
        .unwrap();
        let allowed = load_frontend_file_allowlist(root.path()).unwrap();

        let icon = serve_frontend(root.path(), &allowed, "/cockpit-icon.png").await;
        assert_eq!(icon.status(), StatusCode::OK);
        assert_eq!(icon.headers()[header::CONTENT_TYPE], "image/png");
        let mut without_icon = allowed.clone();
        without_icon.remove("cockpit-icon.png");
        assert_eq!(serve_frontend(root.path(), &without_icon, "/cockpit-icon.png").await.status(), StatusCode::NOT_FOUND);

        let response = serve_frontend(root.path(), &allowed, "/assets/current.js").await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.headers()[header::CONTENT_SECURITY_POLICY], "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data: blob:; font-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' http://127.0.0.1:8765 ws://127.0.0.1:8765 https://codex-home-manager.simplezion.com");
        assert_eq!(response.headers()["x-content-type-options"], "nosniff");
        assert_eq!(response.headers()["referrer-policy"], "no-referrer");
        assert_eq!(response.headers()["x-frame-options"], "DENY");
        assert_eq!(
            response.headers()["permissions-policy"],
            "camera=(), microphone=(), geolocation=()"
        );
        assert_eq!(
            serve_frontend(root.path(), &allowed, "/assets/stale.js")
                .await
                .status(),
            StatusCode::NOT_FOUND
        );
    }

    #[tokio::test]
    async fn capabilities_identify_the_native_contract() {
        let payload = native_capabilities();
        assert_eq!(payload["service"], SERVICE_NAME);
        assert_eq!(payload["frontendContractVersion"], 2);
        assert_eq!(payload["runtime"]["core"], "rust");
        assert!(payload["capabilities"]
            .as_array()
            .is_some_and(|items| !items.is_empty()));
    }

    #[test]
    fn prompt_http_default_home_uses_one_index() {
        use std::io::Write;
        let directory = tempfile::tempdir().unwrap();
        let home = directory.path().join("alternate-home");
        std::fs::create_dir(&home).unwrap();
        let rollout = home.join("rollout.jsonl");
        let mut file = std::fs::File::create(&rollout).unwrap();
        for index in 0..20_000 {
            writeln!(file, "{}", json!({"type":"user_message","payload":{"text":format!("row {index} {}", if index % 30 == 0 { "needle 批量" } else { "neighbor" })}})).unwrap();
        }
        drop(file);
        let connection = rusqlite::Connection::open(home.join("state_5.sqlite")).unwrap();
        connection.execute_batch("CREATE TABLE threads(id TEXT, title TEXT, rollout_path TEXT, updated_at_ms INTEGER, updated_at INTEGER);").unwrap();
        connection
            .execute(
                "INSERT INTO threads VALUES('fixture','Fixture',?1,0,0)",
                [rollout.to_string_lossy().as_ref()],
            )
            .unwrap();
        drop(connection);
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "tests::prompt_http_default_home_child",
                "--nocapture",
            ])
            .env("PROMPT_HTTP_TEST_HOME", &home)
            .env("CODEX_HOME", &home)
            .env(
                "CODEX_HOME_MANAGER_PROMPT_INDEX_ROOT",
                directory.path().join("index"),
            )
            .status()
            .unwrap();
        assert!(status.success());
    }

    #[tokio::test]
    async fn prompt_http_default_home_child() {
        let Ok(home) = std::env::var("PROMPT_HTTP_TEST_HOME") else {
            return;
        };
        let (shutdown_tx, _) = watch::channel(false);
        let state = AppState {
            legacy: Arc::new(LegacySupervisor::new(PathBuf::new()).unwrap()),
            legacy_activity: Arc::default(),
            catalog: Arc::default(),
            prompts: Arc::default(),
            daily_tokens: Arc::default(),
            global_search: Arc::default(),
            frontend_root: Arc::new(PathBuf::new()),
            frontend_files: Arc::default(),
            shutdown_token: Arc::from("test"),
            shutdown_tx,
        };
        let router = Router::new()
            .route(
                "/api/threads/{thread_id}/prompts/page",
                get(thread_prompt_page),
            )
            .route(
                "/api/threads/{thread_id}/prompts/context",
                get(thread_prompt_context),
            )
            .route(
                "/api/threads/{thread_id}/timeline/search/page",
                get(thread_timeline_search_page),
            )
            .with_state(state);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!(
            "http://{}/api/threads/fixture/prompts",
            listener.local_addr().unwrap()
        );
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let client = Client::new();
        let mut incomplete_checked = false;
        for input in [
            None,
            Some(String::new()),
            Some("  ".into()),
            Some(format!("  {home}  ")),
        ] {
            let mut query = vec![
                ("scope", "pure".to_string()),
                ("search", "needle".into()),
                ("order", "desc".into()),
                ("scanBudgetMs", "1".into()),
            ];
            if let Some(input) = input {
                query.push(("codex_home", input));
            }
            let mut url = reqwest::Url::parse(&format!("{base}/page")).unwrap();
            url.query_pairs_mut().extend_pairs(&query);
            let page: Value = client
                .get(url)
                .send()
                .await
                .unwrap()
                .error_for_status()
                .unwrap()
                .json()
                .await
                .unwrap();
            assert_eq!(page["index"]["strategy"], "persistent");
            incomplete_checked |= page["matchCountComplete"] == false;
            let hit = &page["prompts"][0];
            assert!(hit.is_object());
            query.extend([
                ("anchorIndex", hit["index"].to_string()),
                ("anchorByteOffset", hit["byteOffset"].to_string()),
                (
                    "generation",
                    page["index"]["generation"].as_str().unwrap().to_string(),
                ),
            ]);
            let mut url = reqwest::Url::parse(&format!("{base}/context")).unwrap();
            url.query_pairs_mut().extend_pairs(&query);
            let context: Value = client
                .get(url)
                .send()
                .await
                .unwrap()
                .error_for_status()
                .unwrap()
                .json()
                .await
                .unwrap();
            assert_eq!(
                context["prompts"][context["anchorOffset"].as_u64().unwrap() as usize]
                    ["byteOffset"],
                hit["byteOffset"]
            );
            query.push(("generation", "outdated".into()));
            query.retain(|(key, value)| *key != "generation" || value == "outdated");
            let mut url = reqwest::Url::parse(&format!("{base}/context")).unwrap();
            url.query_pairs_mut().extend_pairs(&query);
            let stale = client.get(url).send().await.unwrap();
            assert_eq!(stale.status(), 400);
            assert_eq!(
                stale.json::<Value>().await.unwrap()["code"],
                "prompt_context_stale"
            );
        }
        assert!(
            incomplete_checked,
            "must exercise a hit before indexing completes"
        );
        for search in ["needle", "批量"] {
            let mut cursor = None;
            let mut identities = HashSet::new();
            let mut exhausted = false;
            for _ in 0..30 {
                let mut url = reqwest::Url::parse(&format!(
                    "{}/api/threads/fixture/timeline/search/page",
                    base.split("/api/").next().unwrap()
                )).unwrap();
                let mut parameters = url.query_pairs_mut();
                parameters.extend_pairs([
                    ("codex_home", home.as_str()), ("search", search),
                    ("kind", "user"), ("sourceFilter", "focused"),
                    ("limit", "200"), ("scanBudgetMs", "5000"),
                ]);
                if let Some(cursor) = cursor.as_deref() { parameters.append_pair("cursor", cursor); }
                drop(parameters);
                let response = client.get(url).send().await.unwrap();
                let status = response.status();
                let page: Value = response.json().await.unwrap();
                assert_eq!(status.as_u16(), 200, "{page}");
                if page["matchCountComplete"] != true { continue; }
                assert_eq!(page["matchCount"], 667);
                for row in page["matches"].as_array().unwrap() {
                    assert!(identities.insert(row["id"].as_str().unwrap().to_string()));
                    assert!(row["text"].as_str().unwrap().contains(search));
                }
                if page["hasMore"] == false { exhausted = true; break; }
                cursor = Some(page["nextCursor"].as_str().unwrap().to_string());
            }
            assert!(exhausted);
            assert_eq!(identities.len(), 667);
        }
        server.abort();
    }

    #[test]
    fn proxy_preserves_the_public_host_for_same_origin_authorization() {
        assert!(should_forward_proxy_header(&header::HOST));
        assert!(!should_forward_proxy_header(&header::CONTENT_LENGTH));
    }

    #[test]
    fn route_manifest_declares_search_and_daily_tokens_as_native() {
        let manifest = route_manifest();
        let native = manifest["native"].as_array().unwrap();
        assert!(native
            .iter()
            .any(|path| path == "/api/threads/{thread_id}/daily-tokens"));
        assert!(native
            .iter()
            .any(|path| { path == "/api/threads/{thread_id}/prompts/requests/{request_id}" }));
        assert!(native
            .iter()
            .any(|path| path == "/api/threads/{thread_id}/prompts/context"));
        assert!(native.iter().any(|path| path == "/api/search/content/page"));
        assert!(native
            .iter()
            .any(|path| path == "/api/search/content/requests/{request_id}"));
        assert!(native
            .iter()
            .any(|path| path == "/api/threads/{thread_id}/timeline/search/page"));
        assert!(native.iter().any(|path| {
            path == "/api/threads/{thread_id}/timeline/search/requests/{request_id}"
        }));
        assert_eq!(
            manifest["compatibility"]["policy"],
            "unmigrated-routes-only"
        );
    }

    #[test]
    fn compatibility_router_rejects_unknown_paths_without_starting_legacy() {
        assert!(!legacy_route_is_allowed(&Method::GET, "/api/typo"));
        assert!(!legacy_route_is_allowed(
            &Method::GET,
            "/api/threads/thread-1/daily-tokenz"
        ));
        assert!(legacy_route_is_allowed(&Method::GET, "/api/diagnostics"));
        assert!(legacy_route_is_allowed(
            &Method::GET,
            "/api/threads/thread-1/logs"
        ));
        assert!(!legacy_route_is_allowed(
            &Method::DELETE,
            "/api/threads/thread-1/prompts/requests/request-1"
        ));
        assert!(!legacy_route_is_allowed(
            &Method::DELETE,
            "/api/threads/thread-1/timeline/search/requests/request-1"
        ));
        assert!(!legacy_route_is_allowed(
            &Method::GET,
            "/api/threads/thread-1/timeline/search/page"
        ));
        assert!(!legacy_route_is_allowed(
            &Method::GET,
            "/api/search/content/page"
        ));
        assert!(!legacy_route_is_allowed(
            &Method::DELETE,
            "/api/search/content/requests/request-1"
        ));
        assert!(!legacy_route_is_allowed(
            &Method::POST,
            "/api/search/content/page"
        ));
        assert!(!legacy_route_is_allowed(
            &Method::GET,
            "/api/search/content/unknown"
        ));
        assert!(legacy_route_is_allowed(&Method::POST, "/mcp"));
    }

    #[test]
    fn legacy_transport_retry_is_limited_to_read_methods() {
        assert!(legacy_method_is_idempotent(&Method::GET));
        assert!(legacy_method_is_idempotent(&Method::HEAD));
        assert!(!legacy_method_is_idempotent(&Method::POST));
        assert!(!legacy_method_is_idempotent(&Method::DELETE));
    }

    #[test]
    fn provider_interoperability_requests_use_the_worker_timeout_budget() {
        assert_eq!(
            legacy_proxy_timeout("/api/provider-interoperability/verify"),
            PROVIDER_INTEROP_REQUEST_TIMEOUT
        );
        assert_eq!(
            legacy_proxy_timeout("/api/diagnostics"),
            LEGACY_REQUEST_TIMEOUT
        );
    }

    #[tokio::test]
    async fn idempotent_legacy_request_retries_one_transport_disconnect() {
        let listener = std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (first_connection, _) = listener.accept().unwrap();
            drop(first_connection);

            let (mut second_connection, _) = listener.accept().unwrap();
            let mut request = [0_u8; 2048];
            let _ = second_connection.read(&mut request);
            second_connection
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                .unwrap();
        });
        let client = Client::builder()
            .pool_max_idle_per_host(0)
            .no_proxy()
            .build()
            .unwrap();

        let response = send_idempotent_legacy_request(
            &client,
            Method::GET,
            format!("http://{address}/daily-tokens"),
            &HeaderMap::new(),
            Duration::from_secs(2),
        )
        .await
        .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.text().await.unwrap(), "ok");
        server.join().unwrap();
    }

    #[tokio::test]
    async fn idempotent_legacy_request_survives_a_cold_start_disconnect_window() {
        let listener = std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            for _ in 0..3 {
                let (connection, _) = listener.accept().unwrap();
                drop(connection);
            }
            let (mut connection, _) = listener.accept().unwrap();
            let mut request = [0_u8; 2048];
            let _ = connection.read(&mut request);
            connection
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                .unwrap();
        });
        let client = Client::builder()
            .pool_max_idle_per_host(0)
            .no_proxy()
            .build()
            .unwrap();

        let response = send_idempotent_legacy_request(
            &client,
            Method::GET,
            format!("http://{address}/daily-tokens"),
            &HeaderMap::new(),
            Duration::from_secs(2),
        )
        .await
        .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.text().await.unwrap(), "ok");
        server.join().unwrap();
    }
}
