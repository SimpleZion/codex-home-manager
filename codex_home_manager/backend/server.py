from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import diagnostics as diagnostics_module
from .codex_data import (
    archive_thread,
    backup_manifest_path,
    backup_thread,
    build_snapshot,
    backup_codex_resource,
    codex_home_overview,
    copy_resource_from_home,
    duplicate_thread,
    export_thread_prompts,
    get_thread_action_preview_record,
    get_thread_daily_token_usage,
    get_thread_detail,
    hide_thread_from_sidebar,
    import_project_from_home,
    import_thread_from_home,
    list_backups,
    move_thread_workspace,
    migrate_thread_project,
    preview_official_thread_tools_repair,
    preview_thread_workspace_move,
    preview_import_thread_from_home,
    preview_import_project_from_home,
    preview_project_rename,
    preview_resource_copy,
    preview_write_codex_resource,
    preview_slim_thread,
    read_codex_resource,
    read_thread_prompts,
    read_thread_logs,
    resolve_codex_paths,
    repair_official_thread_tools_exposure,
    repair_user_event,
    rename_project,
    restore_backup,
    show_thread_in_sidebar,
    slim_thread,
    validate_environment,
    write_codex_resource,
)
from .diagnostics import clear_diagnostics_runtime_caches, run_codex_diagnostics


app = FastAPI(title="Codex Home Manager", version="1.0.0")

api_token_header_name = "X-Codex-Manager-Token"
api_token = os.environ.get("CODEX_MANAGER_API_TOKEN") or secrets.token_urlsafe(32)
public_app_origins = {
    "https://codex-home-manager.simplezion.com",
}
configured_app_origins = {
    origin.strip().rstrip("/")
    for origin in os.environ.get("CODEX_HOME_MANAGER_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
loopback_app_origins = {
    "http://127.0.0.1:8765",
    "http://localhost:8765",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
}
allowed_cors_origins = {
    *loopback_app_origins,
    *public_app_origins,
    *configured_app_origins,
}
preview_ttl_ms = 10 * 60 * 1000
authorization_ttl_ms = 5 * 60 * 1000
authorization_cookie_name = "codex_manager_authorization"
preview_store: dict[str, dict[str, Any]] = {}
authorization_store: dict[str, dict[str, Any]] = {}
write_lock = threading.RLock()
authorization_lock = threading.RLock()
diagnostics_cache_ttl_seconds = max(0.0, float(os.environ.get("CODEX_HOME_MANAGER_DIAGNOSTICS_CACHE_SECONDS", "30")))
diagnostics_wait_timeout_seconds = max(
    0.01,
    float(os.environ.get("CODEX_HOME_MANAGER_DIAGNOSTICS_WAIT_TIMEOUT_SECONDS", "120")),
)
diagnostics_cache: dict[tuple[str, int, str], tuple[float, tuple[int, int], int, dict[str, Any]]] = {}
diagnostics_in_flight: dict[tuple[str, int, str], dict[str, Any]] = {}
diagnostics_generations: dict[tuple[str, int, str], int] = {}
diagnostics_cache_lock = threading.RLock()
diagnostics_cache_epoch = 0


def diagnostics_cache_key(codex_home: str | None, sidebar_limit: int, language: str) -> tuple[str, int, str]:
    home_key = str(Path(codex_home).expanduser().resolve()) if codex_home and codex_home.strip() else ""
    return home_key.casefold(), int(sidebar_limit), (language or "zh").strip().lower()


def diagnostics_epoch() -> tuple[int, int]:
    with diagnostics_module.diagnostics_runtime_cache_lock:
        runtime_epoch = diagnostics_module.diagnostics_runtime_cache_epoch
    return diagnostics_cache_epoch, runtime_epoch


def clear_diagnostics_cache() -> None:
    global diagnostics_cache_epoch

    with diagnostics_cache_lock:
        clear_diagnostics_runtime_caches()
        diagnostics_cache_epoch += 1
        diagnostics_cache.clear()
        diagnostics_in_flight.clear()


def run_shared_diagnostics_task(
    cache_key: tuple[str, int, str],
    task: dict[str, Any],
    codex_home: str | None,
    sidebar_limit: int,
    language: str,
) -> None:
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    try:
        result = run_codex_diagnostics(
            codex_home_text=codex_home,
            sidebar_limit=sidebar_limit,
            language=language,
        )
    except BaseException as task_error:
        error = task_error

    with diagnostics_cache_lock:
        is_latest_task = (
            diagnostics_in_flight.get(cache_key) is task
            and diagnostics_generations.get(cache_key) == task["generation"]
            and diagnostics_epoch() == task["epoch"]
        )
        if is_latest_task and error is None and result is not None:
            diagnostics_cache[cache_key] = (
                time.monotonic(),
                task["epoch"],
                task["generation"],
                result,
            )
        if diagnostics_in_flight.get(cache_key) is task:
            diagnostics_in_flight.pop(cache_key, None)

    future: Future[dict[str, Any]] = task["future"]
    if error is None and result is not None:
        future.set_result(result)
    else:
        future.set_exception(error or RuntimeError("Diagnostics task returned no result."))


def start_shared_diagnostics_task(
    cache_key: tuple[str, int, str],
    epoch: tuple[int, int],
    generation: int,
    force_refresh: bool,
    codex_home: str | None,
    sidebar_limit: int,
    language: str,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "epoch": epoch,
        "generation": generation,
        "force_refresh": force_refresh,
        "future": Future(),
    }
    worker = threading.Thread(
        target=run_shared_diagnostics_task,
        args=(cache_key, task, codex_home, sidebar_limit, language),
        name=f"codex-diagnostics-{generation}",
        daemon=True,
    )
    task["worker"] = worker
    diagnostics_in_flight[cache_key] = task
    try:
        worker.start()
    except BaseException as error:
        if diagnostics_in_flight.get(cache_key) is task:
            diagnostics_in_flight.pop(cache_key, None)
        task["future"].set_exception(error)
        raise
    return task


def cached_codex_diagnostics(
    codex_home: str | None,
    sidebar_limit: int,
    language: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Run one shared background scan per key, epoch, and generation."""
    cache_key = diagnostics_cache_key(codex_home, sidebar_limit, language)
    now = time.monotonic()
    with diagnostics_cache_lock:
        current_epoch = diagnostics_epoch()
        current_generation = diagnostics_generations.get(cache_key, 0)
        cached_entry = diagnostics_cache.get(cache_key)
        if (
            not force_refresh
            and cached_entry is not None
            and cached_entry[1] == current_epoch
            and cached_entry[2] == current_generation
            and now - cached_entry[0] <= diagnostics_cache_ttl_seconds
        ):
            return cached_entry[3]

        in_flight = diagnostics_in_flight.get(cache_key)
        can_join_in_flight = (
            in_flight is not None
            and in_flight["epoch"] == current_epoch
            and in_flight["generation"] == current_generation
            and (not force_refresh or in_flight["force_refresh"])
        )
        if not can_join_in_flight:
            if force_refresh:
                clear_diagnostics_runtime_caches()
                current_epoch = diagnostics_epoch()
            generation = current_generation + 1
            diagnostics_generations[cache_key] = generation
            diagnostics_cache.pop(cache_key, None)
            in_flight = start_shared_diagnostics_task(
                cache_key=cache_key,
                epoch=current_epoch,
                generation=generation,
                force_refresh=force_refresh,
                codex_home=codex_home,
                sidebar_limit=sidebar_limit,
                language=language,
            )
    future: Future[dict[str, Any]] = in_flight["future"]
    try:
        return future.result(timeout=diagnostics_wait_timeout_seconds)
    except FutureTimeoutError as error:
        raise TimeoutError(
            "Timed out after "
            f"{diagnostics_wait_timeout_seconds:g} seconds waiting for diagnostics generation "
            f"{in_flight['generation']}."
        ) from error


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


class PreviewBoundRequest(BaseModel):
    operationPreviewId: str | None = None
    inputHash: str | None = None
    createBackup: bool = True


class MigrateThreadRequest(PreviewBoundRequest):
    targetProjectPath: str = Field(min_length=1)
    acknowledgeCodexRunningRisk: bool = False


class MoveThreadWorkspaceRequest(PreviewBoundRequest):
    targetProjectPath: str = Field(min_length=1)
    includeSameSourceCwdThreads: bool = True
    moveWorkspaceFiles: bool = True
    repairUserEvent: bool = True
    preservePinned: bool = False
    acknowledgeCodexRunningRisk: bool = False


class RenameProjectRequest(PreviewBoundRequest):
    sourceProjectPath: str = Field(min_length=1)
    targetProjectPath: str = Field(min_length=1)
    renameFolder: bool = True
    acknowledgeCodexRunningRisk: bool = False


class SlimThreadRequest(PreviewBoundRequest):
    removeImages: bool = True
    keepLatestCompacted: bool = True
    acknowledgeCodexRunningRisk: bool = False


class DuplicateThreadRequest(PreviewBoundRequest):
    targetProjectPath: str = Field(min_length=1)
    acknowledgeCodexRunningRisk: bool = False


class ResourceBackupRequest(BaseModel):
    relativePath: str = ""


class CopyResourceRequest(PreviewBoundRequest):
    sourceCodexHome: str = Field(min_length=1)
    relativePath: str = Field(min_length=1)
    targetRelativePath: str | None = None
    overwrite: bool = False
    acknowledgeCodexRunningRisk: bool = False


class WriteResourceRequest(PreviewBoundRequest):
    relativePath: str = Field(min_length=1)
    content: str
    createParentDirectories: bool = True
    acknowledgeCodexRunningRisk: bool = False


class OfficialThreadToolsRepairRequest(PreviewBoundRequest):
    acknowledgeCodexRunningRisk: bool = False


class ImportThreadRequest(PreviewBoundRequest):
    sourceCodexHome: str = Field(min_length=1)
    sourceThreadId: str = Field(min_length=1)
    targetProjectPath: str | None = None
    preserveThreadId: bool = False
    acknowledgeCodexRunningRisk: bool = False


class ImportProjectRequest(PreviewBoundRequest):
    sourceCodexHome: str = Field(min_length=1)
    sourceProjectPath: str = Field(min_length=1)
    targetProjectPath: str | None = None
    includeArchived: bool = False
    preserveThreadIds: bool = False
    acknowledgeCodexRunningRisk: bool = False


class PreviewResourceCopyRequest(BaseModel):
    sourceCodexHome: str = Field(min_length=1)
    relativePath: str = Field(min_length=1)
    targetRelativePath: str | None = None
    overwrite: bool = False


class ThreadActionPreviewResponse(BaseModel):
    operationPreviewId: str
    inputHash: str
    expiresAtMs: int
    action: str
    threadId: str
    title: str | None = None
    projectPath: str | None = None
    targetProjectPath: str | None = None
    rolloutPath: str | None = None
    rolloutStat: dict[str, Any] | None = None
    warnings: list[str]


class CapabilityItem(BaseModel):
    name: str
    method: str
    path: str
    purpose: str
    required: list[str]
    backup: str
    bodyExample: dict[str, Any] | None = None
    successFields: list[str]
    rollback: str | None = None
    riskLevel: str = "read"
    previewEndpoint: str | None = None
    writeEndpoint: str | None = None
    idempotency: str = "safe to repeat read operations; write operations create new backups"
    rollbackMode: str | None = None


class CapabilitiesResponse(BaseModel):
    service: str
    version: str
    language: str = "en"
    openapiPath: str
    mcpPath: str
    safetyModel: dict[str, Any]
    commonQueryParameters: dict[str, str]
    capabilities: list[CapabilityItem]


class HealthResponse(BaseModel):
    paths: dict[str, str]
    checks: dict[str, Any]
    threadCount: int | None
    version: dict[str, Any]
    currentVersions: dict[str, Any]
    codexProcesses: list[dict[str, Any]]
    writeWarnings: list[str]


class DiagnosticIssue(BaseModel):
    id: str
    severity: str
    category: str
    title: str
    summary: str
    recommendation: str
    evidence: list[str]
    affectedPaths: list[str]
    fixCommand: str | None = None


class DiagnosticCheck(BaseModel):
    id: str
    category: str
    title: str
    status: str
    summary: str
    evidence: list[str]
    affectedPaths: list[str]


class DiagnosticsResponse(BaseModel):
    codexHome: str
    generatedAtMs: int
    score: int
    status: str
    summary: dict[str, Any]
    paths: dict[str, str]
    codexProcesses: list[dict[str, Any]]
    capacityTrend: dict[str, Any]
    checks: list[DiagnosticCheck]
    issues: list[DiagnosticIssue]
    topRecommendations: list[str]
    repairHints: dict[str, str]
    repairPrompt: str


class AuthTokenResponse(BaseModel):
    token: str
    headerName: str
    expiresAtMs: int | None = None


class SnapshotResponse(BaseModel):
    codexHome: str
    databasePath: str
    globalStatePath: str
    sessionIndexPath: str
    sidebarLimit: int
    version: dict[str, Any] | None = None
    summary: dict[str, Any]
    threads: list[dict[str, Any]]
    projects: list[dict[str, Any]]
    generatedAtMs: int


class HomeOverviewResponse(BaseModel):
    codexHome: str
    resources: list[dict[str, Any]]
    summary: dict[str, Any]
    generatedAtMs: int


class ResourceReadResponse(BaseModel):
    metadata: dict[str, Any]
    content: str | None = None
    children: list[dict[str, Any]] | None = None
    truncated: bool | None = None
    binary: bool | None = None


class BackupManifestResponse(BaseModel):
    backupId: str
    createdAt: str
    codexHome: str
    databasePath: str | None = None
    threadId: str | None = None
    action: str
    restoreMode: str | None = None
    manifestPath: str | None = None
    rowBefore: dict[str, Any] | None = None
    rolloutStatBefore: dict[str, Any] | None = None
    databaseBackupPath: str | None = None
    globalStateBackupPath: str | None = None
    configBackupPath: str | None = None
    rolloutBackupPath: str | None = None
    resourceBackups: list[dict[str, Any]] | None = None
    createdResourcePaths: list[str] | None = None
    createdThreadIds: list[str] | None = None


class BackupListResponse(BaseModel):
    backups: list[BackupManifestResponse]


class RestoreResponse(BaseModel):
    threadId: str | None = None
    restoredBackupId: str
    restoredAt: str
    notes: list[str]
    warnings: list[str]


class ThreadDetailResponse(BaseModel):
    thread: dict[str, Any]
    sqliteRow: dict[str, Any] | None = None
    rolloutStats: dict[str, Any]
    dailyTokenUsage: dict[str, Any] | None = None
    backups: list[dict[str, Any]]


class ThreadDailyTokenUsageResponse(BaseModel):
    summary: dict[str, Any]
    days: list[dict[str, Any]]


class ThreadLogsResponse(BaseModel):
    threadId: str
    source: str
    rolloutPath: str
    appLogPath: str
    offset: int
    limit: int
    kind: str
    search: str
    matchedEntries: int
    hasMore: bool
    entries: list[dict[str, Any]]
    summary: dict[str, Any]


class ThreadActionResponse(BaseModel):
    threadId: str | None = None
    backup: dict[str, Any] | None = None
    warnings: list[str] | None = None


class ShowThreadResponse(ThreadActionResponse):
    updatedAtMs: int


class HideThreadResponse(ThreadActionResponse):
    updatedAtMs: int
    sessionIndexUpdate: dict[str, Any]
    globalStateUpdate: dict[str, Any]


class ArchiveThreadResponse(ThreadActionResponse):
    archivedAt: int
    rolloutArchiveUpdate: dict[str, Any]
    sessionIndexUpdate: dict[str, Any]
    sidebarReferenceUpdate: dict[str, Any]
    globalStateUpdate: dict[str, Any]


class DuplicateThreadResponse(BaseModel):
    sourceThreadId: str
    newThreadId: str
    newRolloutPath: str
    targetProjectPath: str
    sessionIndexEntry: dict[str, Any] | None = None
    backup: dict[str, Any]
    warnings: list[str]


class MigrateThreadResponse(BaseModel):
    threadId: str
    oldProjectPath: str
    newProjectPath: str
    rewrite: dict[str, Any]
    backup: dict[str, Any]
    warnings: list[str]


class MoveThreadWorkspaceResponse(BaseModel):
    threadId: str
    sourceProjectPath: str
    targetProjectPath: str
    matchedThreads: int
    matchedThreadIds: list[str]
    updatedThreads: int
    fileMove: dict[str, Any]
    rolloutBackups: list[dict[str, str]]
    rolloutRewrites: list[dict[str, Any]]
    globalStateRewrite: dict[str, Any]
    configRewrite: dict[str, Any]
    backup: dict[str, Any]
    warnings: list[str]


class ExportPromptsResponse(BaseModel):
    threadId: str
    promptCount: int
    allPromptCount: int | None = None
    filterScope: str | None = None
    sourceCounts: dict[str, int] | None = None
    outputPath: str
    format: str


class PromptRecord(BaseModel):
    index: int
    lineNumber: int
    timestamp: str | None = None
    text: str
    characterCount: int
    sourceType: str | None = None
    sourceLabel: str | None = None
    visibleByDefault: bool | None = None
    pureText: str | None = None
    pureCharacterCount: int | None = None
    hasPureText: bool | None = None


class ThreadPromptsResponse(BaseModel):
    threadId: str
    title: str | None = None
    rolloutPath: str
    promptCount: int
    purePromptCount: int | None = None
    visiblePromptCount: int | None = None
    hiddenPromptCount: int | None = None
    sourceCounts: dict[str, int] | None = None
    prompts: list[PromptRecord]


class SlimPreviewResponse(BaseModel):
    operationPreviewId: str
    inputHash: str
    expiresAtMs: int
    threadId: str
    rolloutPath: str
    binding: dict[str, Any] | None = None
    scan: dict[str, Any]
    canRemoveImages: bool
    canReduceCompacted: bool
    warnings: list[str]


class SlimThreadResponse(BaseModel):
    threadId: str
    backup: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    stats: dict[str, Any]
    savedBytes: int
    warnings: list[str]


class RenameProjectResponse(BaseModel):
    sourceProjectPath: str
    targetProjectPath: str
    renamedFolder: bool
    updatedThreads: int
    rolloutBackups: list[dict[str, Any]]
    globalStateRewrite: dict[str, Any]
    configRewrite: dict[str, Any]
    backup: dict[str, Any]
    warnings: list[str]


class PreviewResponse(BaseModel):
    operationPreviewId: str | None = None
    inputHash: str | None = None
    expiresAtMs: int | None = None
    action: str | None = None
    threadId: str | None = None
    backupId: str | None = None
    title: str | None = None
    sourceCodexHome: str | None = None
    targetCodexHome: str | None = None
    sourceThreadId: str | None = None
    targetThreadId: str | None = None
    preservesThreadId: bool | None = None
    sourceProjectPath: str | None = None
    targetProjectPath: str | None = None
    sourceRolloutPath: str | None = None
    sourcePath: str | None = None
    targetPath: str | None = None
    matchedThreads: int | None = None
    existingRollouts: int | None = None
    rolloutBytes: int | None = None
    willRenameFolder: bool | None = None
    requiresCodexClosed: bool | None = None
    blockedByRunningCodex: bool | None = None
    willOverwrite: bool | None = None
    source: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    sourceFolder: dict[str, Any] | None = None
    targetFolder: dict[str, Any] | None = None
    archivedIncluded: bool | None = None
    warnings: list[str]


class ResourceBackupResponse(BaseModel):
    backup: dict[str, Any]
    resourcePath: str
    resourceBackupPath: str
    warnings: list[str]


class ResourceWriteResponse(BaseModel):
    resourcePath: str
    sizeBytes: int
    backup: dict[str, Any]
    warnings: list[str]


class ResourceCopyResponse(BaseModel):
    sourcePath: str
    targetPath: str
    backup: dict[str, Any]
    overwroteExisting: bool
    warnings: list[str]


class ImportResponse(BaseModel):
    importedThreads: list[dict[str, Any]]
    backup: dict[str, Any]
    warnings: list[str]


app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_private_network=True,
)


def runtime_conflict(error: RuntimeError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(error))


def unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail)


def require_api_token(token: str | None) -> None:
    if token and secrets.compare_digest(token, api_token):
        return
    with authorization_lock:
        cleanup_authorization_store()
        authorization = authorization_store.get(token or "")
    if not authorization:
        raise unauthorized(f"write requests require {api_token_header_name}")


def authorize_browser_write_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    request_origin = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
    request_hostname = (request.url.hostname or "").casefold()
    if request_hostname not in {"127.0.0.1", "localhost", "::1"} or origin.rstrip("/") != request_origin:
        raise HTTPException(
            status_code=403,
            detail="browser write authorization is available only to the loopback same-origin local UI",
        )


def authorize_write_request(request: Request) -> None:
    authorize_browser_write_origin(request)
    token = request.headers.get(api_token_header_name)
    if request.headers.get("origin"):
        token = token or request.cookies.get(authorization_cookie_name)
    require_api_token(token)


def codex_home_key(codex_home: str | None) -> str:
    return os.path.normcase(str(resolve_codex_paths(codex_home).codex_home_path))


def cleanup_authorization_store() -> None:
    now_ms = int(time.time() * 1000)
    expired_tokens = [
        token
        for token, authorization in authorization_store.items()
        if int(authorization.get("expiresAtMs", 0)) < now_ms
    ]
    for token in expired_tokens:
        authorization_store.pop(token, None)


def create_local_authorization(codex_home: str | None) -> dict[str, Any]:
    home_key = codex_home_key(codex_home)
    token = secrets.token_urlsafe(32)
    expires_at_ms = int(time.time() * 1000) + authorization_ttl_ms
    with authorization_lock:
        cleanup_authorization_store()
        if len(authorization_store) >= 256:
            oldest_token = min(
                authorization_store,
                key=lambda candidate: int(authorization_store[candidate].get("expiresAtMs", 0)),
            )
            authorization_store.pop(oldest_token, None)
        authorization_store[token] = {"codexHomeKey": home_key, "expiresAtMs": expires_at_ms}
    return {"token": token, "headerName": api_token_header_name, "expiresAtMs": expires_at_ms}


def authorize_local_data_request(request: Request, codex_home: str | None, token: str | None = None) -> None:
    authorize_browser_write_origin(request)
    supplied_token = token or request.headers.get(api_token_header_name) or request.cookies.get(authorization_cookie_name)
    with authorization_lock:
        cleanup_authorization_store()
        authorization = authorization_store.get(supplied_token or "")
    if not authorization:
        raise unauthorized(f"local data requests require short-lived {api_token_header_name} authorization")
    try:
        requested_home_key = codex_home_key(codex_home)
    except ValueError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if authorization.get("codexHomeKey") != requested_home_key:
        raise HTTPException(status_code=403, detail="authorization is not valid for the requested Codex Home")


@app.middleware("http")
async def authorize_browser_api_reads(request: Request, call_next):
    if (
        request.url.path.startswith("/api/")
        and request.url.path not in {"/api/auth/token", "/api/capabilities"}
        and request.method != "OPTIONS"
    ):
        try:
            authorize_local_data_request(request, request.query_params.get("codex_home"))
        except HTTPException as error:
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
    return await call_next(request)


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


def state_path_record(path: Path, *, include_hash: bool = False) -> dict[str, Any]:
    resolved_path = path.expanduser().resolve(strict=False)
    if not resolved_path.exists():
        return {"path": str(resolved_path), "exists": False}
    stat = resolved_path.stat()
    record: dict[str, Any] = {
        "path": str(resolved_path),
        "exists": True,
        "kind": "directory" if resolved_path.is_dir() else "file",
        "size": stat.st_size,
        "modifiedNs": stat.st_mtime_ns,
    }
    if resolved_path.is_file() and include_hash:
        record["sha256"] = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
    elif resolved_path.is_dir():
        record["children"] = [
            {
                "name": child.name,
                "kind": "directory" if child.is_dir() else "file",
                "size": child.stat().st_size,
                "modifiedNs": child.stat().st_mtime_ns,
            }
            for child in sorted(resolved_path.iterdir(), key=lambda item: item.name.casefold())
        ]
    return record


def operation_state_digest(payload: dict[str, Any]) -> str:
    codex_home_text = str(payload.get("codexHome") or "") or None
    paths = resolve_codex_paths(codex_home_text)
    state_paths: list[tuple[Path, bool]] = [
        (paths.database_path, False),
        (paths.database_path.with_name(paths.database_path.name + "-wal"), False),
        (paths.database_path.with_name(paths.database_path.name + "-shm"), False),
        (paths.global_state_path, True),
        (paths.global_state_backup_path, True),
        (paths.session_index_path, True),
        (paths.config_path, True),
        (paths.codex_home_path / "managed_config.toml", True),
    ]

    for field_name in ("relativePath", "targetRelativePath"):
        relative_text = str(payload.get(field_name) or "").strip()
        if relative_text:
            candidate = (paths.codex_home_path / relative_text).resolve(strict=False)
            try:
                candidate.relative_to(paths.codex_home_path)
            except ValueError:
                state_paths.append((candidate, False))
            else:
                state_paths.append((candidate, True))

    source_home_text = str(payload.get("sourceCodexHome") or "").strip()
    if source_home_text:
        source_paths = resolve_codex_paths(source_home_text)
        state_paths.extend(
            [
                (source_paths.database_path, False),
                (source_paths.database_path.with_name(source_paths.database_path.name + "-wal"), False),
                (source_paths.session_index_path, True),
            ]
        )

    thread_id = str(payload.get("threadId") or "").strip()
    if thread_id:
        try:
            thread_record = get_thread_action_preview_record(codex_home_text, thread_id)
        except (KeyError, OSError, ValueError):
            thread_record = {}
        rollout_path = str(thread_record.get("rolloutPath") or "").strip()
        project_path = str(thread_record.get("projectPath") or "").strip()
        if rollout_path:
            state_paths.append((Path(rollout_path), False))
        if project_path:
            state_paths.append((Path(project_path), False))

    for field_name in ("sourceProjectPath", "targetProjectPath"):
        path_text = str(payload.get(field_name) or "").strip()
        if path_text:
            state_paths.append((Path(path_text), False))

    backup_id = str(payload.get("backupId") or "").strip()
    if backup_id:
        state_paths.append((backup_manifest_path(backup_id), True))

    unique_records: dict[str, dict[str, Any]] = {}
    for state_path, include_hash in state_paths:
        record = state_path_record(state_path, include_hash=include_hash)
        unique_records[os.path.normcase(record["path"])] = record
    return canonical_payload_hash({"paths": unique_records})


def cleanup_preview_store() -> None:
    with write_lock:
        now_ms = int(time.time() * 1000)
        expired_ids = [
            preview_id
            for preview_id, preview in preview_store.items()
            if int(preview.get("expiresAtMs", 0)) < now_ms
        ]
        for preview_id in expired_ids:
            preview_store.pop(preview_id, None)


def create_preview_ticket(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    with write_lock:
        cleanup_preview_store()
        input_hash = canonical_payload_hash({"operation": operation, "payload": payload})
        preview_id = secrets.token_urlsafe(18)
        expires_at_ms = int(time.time() * 1000) + preview_ttl_ms
        state_digest = operation_state_digest(payload)
        preview_store[preview_id] = {
            "operation": operation,
            "inputHash": input_hash,
            "stateDigest": state_digest,
            "inFlight": False,
            "expiresAtMs": expires_at_ms,
        }
    return {
        "operationPreviewId": preview_id,
        "inputHash": input_hash,
        "stateDigest": state_digest,
        "expiresAtMs": expires_at_ms,
    }


def preview_thread_from_snapshot(codex_home: str | None, thread_id: str, sidebar_limit: int) -> dict[str, Any]:
    return get_thread_action_preview_record(codex_home, thread_id)


def require_preview_ticket(
    operation: str,
    payload: dict[str, Any],
    preview_id: str | None,
    input_hash: str | None,
) -> dict[str, Any]:
    cleanup_preview_store()
    if not preview_id or not input_hash:
        raise HTTPException(status_code=428, detail="write requests require operationPreviewId and inputHash from the matching preview endpoint")
    preview = preview_store.get(preview_id)
    expected_hash = canonical_payload_hash({"operation": operation, "payload": payload})
    if not preview:
        raise HTTPException(status_code=428, detail="operation preview expired or was not found")
    if preview.get("operation") != operation:
        raise HTTPException(status_code=428, detail="operation preview type does not match this write")
    if preview.get("inputHash") != input_hash or input_hash != expected_hash:
        raise HTTPException(status_code=428, detail="operation preview input hash does not match this write")
    if preview.get("inFlight"):
        raise HTTPException(status_code=409, detail="operation preview is already in flight")
    current_state_digest = operation_state_digest(payload)
    if preview.get("stateDigest") != current_state_digest:
        preview_store.pop(preview_id, None)
        raise HTTPException(status_code=409, detail="operation state changed after preview; create a new preview")
    return preview


@contextmanager
def preview_bound_write(
    operation: str,
    payload: dict[str, Any],
    preview_id: str | None,
    input_hash: str | None,
) -> Iterator[None]:
    with write_lock:
        preview = require_preview_ticket(operation, payload, preview_id, input_hash)
        preview["inFlight"] = True
        try:
            yield
        except Exception:
            current_preview = preview_store.get(preview_id or "")
            if current_preview is not None:
                if current_preview.get("stateDigest") == operation_state_digest(payload):
                    current_preview["inFlight"] = False
                else:
                    preview_store.pop(preview_id or "", None)
            raise
        else:
            preview_store.pop(preview_id or "", None)


def codex_home_value(codex_home: str | None) -> str:
    return codex_home or ""


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def slim_payload(codex_home: str | None, thread_id: str, remove_images: bool, keep_latest_compacted: bool) -> dict[str, Any]:
    return {
        "codexHome": codex_home_value(codex_home),
        "threadId": thread_id,
        "removeImages": remove_images,
        "keepLatestCompacted": keep_latest_compacted,
    }


def duplicate_payload(codex_home: str | None, thread_id: str, target_project_path: str) -> dict[str, Any]:
    return {
        "codexHome": codex_home_value(codex_home),
        "threadId": thread_id,
        "targetProjectPath": target_project_path,
    }


def thread_action_payload(codex_home: str | None, thread_id: str, action: str) -> dict[str, Any]:
    return {"codexHome": codex_home_value(codex_home), "threadId": thread_id, "action": action}


def migrate_payload(codex_home: str | None, thread_id: str, target_project_path: str) -> dict[str, Any]:
    return {"codexHome": codex_home_value(codex_home), "threadId": thread_id, "targetProjectPath": target_project_path}


def move_thread_workspace_payload(codex_home: str | None, thread_id: str, request: MoveThreadWorkspaceRequest) -> dict[str, Any]:
    return {
        "codexHome": codex_home_value(codex_home),
        "threadId": thread_id,
        "targetProjectPath": request.targetProjectPath,
        "includeSameSourceCwdThreads": request.includeSameSourceCwdThreads,
        "moveWorkspaceFiles": request.moveWorkspaceFiles,
        "repairUserEvent": request.repairUserEvent,
        "preservePinned": request.preservePinned,
    }


def rename_payload(codex_home: str | None, request: RenameProjectRequest) -> dict[str, Any]:
    return {
        "codexHome": codex_home_value(codex_home),
        "sourceProjectPath": request.sourceProjectPath,
        "targetProjectPath": request.targetProjectPath,
        "renameFolder": request.renameFolder,
    }


def import_thread_payload(codex_home: str | None, request: ImportThreadRequest) -> dict[str, Any]:
    return {
        "codexHome": codex_home_value(codex_home),
        "sourceCodexHome": request.sourceCodexHome,
        "sourceThreadId": request.sourceThreadId,
        "targetProjectPath": request.targetProjectPath,
        "preserveThreadId": request.preserveThreadId,
    }


def import_project_payload(codex_home: str | None, request: ImportProjectRequest) -> dict[str, Any]:
    return {
        "codexHome": codex_home_value(codex_home),
        "sourceCodexHome": request.sourceCodexHome,
        "sourceProjectPath": request.sourceProjectPath,
        "targetProjectPath": request.targetProjectPath,
        "includeArchived": request.includeArchived,
        "preserveThreadIds": request.preserveThreadIds,
    }


def resource_copy_payload(codex_home: str | None, request: CopyResourceRequest | PreviewResourceCopyRequest) -> dict[str, Any]:
    return {
        "codexHome": codex_home_value(codex_home),
        "sourceCodexHome": request.sourceCodexHome,
        "relativePath": request.relativePath,
        "targetRelativePath": request.targetRelativePath,
        "overwrite": request.overwrite,
    }


def resource_write_payload(codex_home: str | None, request: WriteResourceRequest) -> dict[str, Any]:
    return {
        "codexHome": codex_home_value(codex_home),
        "relativePath": request.relativePath,
        "contentSha256": content_digest(request.content),
        "contentBytes": len(request.content.encode("utf-8")),
        "createParentDirectories": request.createParentDirectories,
    }


def official_thread_tools_repair_payload(codex_home: str | None) -> dict[str, Any]:
    return {"codexHome": codex_home_value(codex_home)}


def restore_payload(codex_home: str | None, backup_id: str) -> dict[str, Any]:
    return {"codexHome": codex_home_value(codex_home), "backupId": backup_id}


def capability_language(language: str | None) -> str:
    normalized_language = (language or "en").strip().lower().replace("_", "-")
    if normalized_language in {"zh", "zh-cn", "zh-hans"}:
        return "zh"
    return "en"


capability_purpose_zh: dict[str, str] = {
    "auth_token": "获取写入接口所需的本进程本地 API token。",
    "snapshot_threads": "列出并分类所有本地 Codex 线程；新版 Codex 可显示完整线程列表，旧版首轮排序仅作为兼容信息。",
    "get_thread_detail": "读取单个线程的 SQLite 行、JSONL 统计、每日 token 消耗、文件位置和备份记录。",
    "read_thread_prompts": "只读查看一个线程里的所有用户 prompt，不写导出文件。",
    "preview_thread_action": "在写入前预览显示、隐藏、修复、归档或复制线程操作；复制预览需要 targetProjectPath。",
    "show_thread": "把隐藏或归档线程提升到 Codex Desktop 初始侧边栏排序中。",
    "hide_thread": "从管理视图隐藏一个可见主线程，并把它降到 Codex Desktop 初始侧边栏之外；不会归档或删除 JSONL。",
    "export_prompts": "把一个线程里的用户 prompt 导出为 markdown 或 json；因为会写出导出文件，所以需要 token。",
    "read_thread_logs": "分页读取结构化 rollout JSONL 日志和 Codex app 的 logs_2.sqlite 行，支持来源、搜索以及请求、失败、错误、事件、工具调用和原始行过滤。",
    "duplicate_thread": "在当前 Codex Home 内复制一个线程，为其生成新线程 ID，并把 rollout JSONL 写入指定目标项目路径。",
    "archive_thread": "通过归档实现安全删除，不永久删除 JSONL。",
    "preview_migrate_thread": "在线程项目迁移写入 SQLite、global state 或 JSONL 元数据前预览影响。",
    "migrate_thread": "把线程迁移到另一个项目路径，并重写 JSONL 中的结构化工作区元数据。",
    "preview_move_thread_workspace": "预览线程与其实际工作区文件的整体搬迁影响。",
    "move_thread_workspace": "搬迁线程、同源工作区线程、实际工作区文件、SQLite、global state 和 rollout 元数据。",
    "slim_thread": "只在显式选择的范围内缩减 JSONL 体积：removeImages、keepLatestCompacted 或两者同时执行。",
    "preview_slim_thread": "在不写文件的情况下预览所选范围的 JSONL 瘦身影响。",
    "rename_project": "重命名项目路径元数据，并可选择同步重命名本地项目文件夹；执行前必须关闭 Codex Desktop 和 Codex CLI。",
    "preview_rename_project": "预览项目重命名会匹配的线程、rollout 字节数、文件夹影响，以及是否被运行中的 Codex 阻断。",
    "import_thread_from_home": "从另一个 Codex Home 把单个线程复制到当前 Codex Home。",
    "preview_import_thread_from_home": "在不写当前 Home 的情况下预览单个来源线程导入。",
    "import_project_from_home": "从另一个 Codex Home 把某个项目下匹配的所有线程复制到当前 Codex Home。",
    "preview_import_project_from_home": "在不写当前 Home 的情况下预览来源项目导入的线程数量和 rollout 字节数。",
    "home_overview": "盘点 CODEX_HOME 资源，例如 sessions、memories、skills、AGENTS.md、config 和 logs。",
    "read_resource": "读取 CODEX_HOME 内的文本资源，或列出目录内容。",
    "backup_resource": "为 CODEX_HOME 资源创建一次显式备份。",
    "preview_write_resource": "在不写入的情况下预览文本资源写入和受保护路径校验。",
    "write_resource": "写入 CODEX_HOME 内的 UTF-8 文本资源，例如 AGENTS.md 或 memory notes；受保护 state 文件和 session JSONL 会被拒绝。",
    "copy_resource_from_home": "从另一个 Codex Home 复制 AGENTS.md、memories、skills 或安全文本资源；受保护 Codex state 文件会被拒绝。",
    "preview_copy_resource_from_home": "在不写当前 Home 的情况下预览资源复制体积和覆盖影响。",
    "preview_restore_backup": "预览备份回滚说明，并绑定一次回滚操作。",
    "restore_backup": "回滚备份 manifest，或归档通过导入/复制创建出来的线程。",
    "diagnostics": "运行只读 Codex Home 体检，覆盖本地状态、线程可见性、插件缓存、配置、日志、存储和运行进程风险。",
}


capability_text_zh: dict[str, str] = {
    "read-only": "只读",
    "thread action backup before write": "写入前创建线程操作备份",
    "writes export file in workspace data/exports": "在工作区 data/exports 写出导出文件",
    "thread action backup before write; restore archives created copy": "写入前创建线程操作备份；回滚会归档创建出的副本",
    "thread action backup before JSONL rewrite": "重写 JSONL 前创建线程操作备份",
    "backs up all matched thread rows, rollout JSONLs, global state and config": "备份所有匹配线程行、rollout JSONL、global state 和 config",
    "home state backup before import; restore archives imported thread": "导入前创建 Home 状态备份；回滚会归档导入线程",
    "home state backup before import; restore archives imported threads": "导入前创建 Home 状态备份；回滚会归档导入线程",
    "resource backup": "资源备份",
    "backs up overwritten target resource before write": "写入前备份会被覆盖的目标资源",
    "backs up overwritten target resource before copy": "复制前备份会被覆盖的目标资源",
    "creates pre_restore backup before restore": "回滚前创建 pre_restore 备份",
    "backup.backupId when the action writes state": "写入状态时返回 backup.backupId",
    "not idempotent; each successful write creates a backup and may create or move records": "非幂等；每次成功写入都会创建备份，并可能创建或移动记录",
    "idempotent read": "幂等读取",
    "restore backup by backup.backupId": "使用 backup.backupId 调用回滚接口恢复",
}


@app.get("/api/health", response_model=HealthResponse)
def health(codex_home: str | None = Query(default=None)) -> dict[str, Any]:
    try:
        return validate_environment(codex_home)
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/diagnostics", response_model=DiagnosticsResponse)
def diagnostics(
    codex_home: str | None = Query(default=None),
    sidebar_limit: int = Query(default=50, ge=1, le=1000),
    lang: str = Query(default="zh"),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return cached_codex_diagnostics(
            codex_home=codex_home,
            sidebar_limit=sidebar_limit,
            language=lang,
            force_refresh=refresh,
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/auth/token", response_model=AuthTokenResponse)
def auth_token(
    request: Request,
    response: Response,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    authorize_browser_write_origin(request)
    try:
        authorization = create_local_authorization(codex_home)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    response.set_cookie(
        authorization_cookie_name,
        authorization["token"],
        max_age=authorization_ttl_ms // 1000,
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return authorization


@app.get("/api/capabilities", response_model=CapabilitiesResponse)
def capabilities(lang: str = Query(default="en")) -> dict[str, Any]:
    language = capability_language(lang)

    def localize_text(text: str) -> str:
        if language != "zh":
            return text
        return capability_text_zh.get(text, text)

    def item(
        name: str,
        method: str,
        path: str,
        purpose: str,
        required: list[str],
        backup: str,
        body_example: dict[str, Any] | None = None,
        success_fields: list[str] | None = None,
        risk_level: str | None = None,
        preview_endpoint: str | None = None,
        write_endpoint: str | None = None,
        idempotency: str | None = None,
        rollback_mode: str | None = None,
    ) -> dict[str, Any]:
        is_write = method.upper() not in {"GET", "HEAD", "OPTIONS"} and backup != "read-only"
        idempotency_text = idempotency or ("not idempotent; each successful write creates a backup and may create or move records" if is_write else "idempotent read")
        rollback_mode_text = rollback_mode or ("restore backup by backup.backupId" if is_write else None)
        return {
            "name": name,
            "method": method,
            "path": path,
            "purpose": capability_purpose_zh.get(name, purpose) if language == "zh" else purpose,
            "required": required,
            "backup": localize_text(backup),
            "bodyExample": body_example,
            "successFields": success_fields or [localize_text("backup.backupId when the action writes state")],
            "rollback": "POST /api/backups/{backup_id}/restore" if backup != "read-only" else None,
            "riskLevel": risk_level or ("write" if is_write else "read"),
            "previewEndpoint": preview_endpoint,
            "writeEndpoint": write_endpoint or (path if is_write else None),
            "idempotency": localize_text(idempotency_text),
            "rollbackMode": localize_text(rollback_mode_text) if rollback_mode_text else None,
        }

    safety_model = {
        "writesCreateBackups": True,
        "backupRoot": "codex_home_manager/data/backups",
        "restoreEndpoint": "POST /api/backups/{backup_id}/restore",
        "physicalDelete": False,
        "deleteBehavior": "Thread delete is implemented as archive by default; JSONL files are not permanently deleted.",
        "resourcePathPolicy": "Resource APIs accept paths relative to CODEX_HOME and reject absolute paths or .. escapes.",
        "protectedTextWritePaths": [
            "state_5.sqlite",
            "logs_2.sqlite",
            "session_index.jsonl",
            "version.json",
            ".codex-global-state.json",
            "config.toml",
            "sessions/**",
            "plugins/**",
            "generated_images/**",
            "automations/**",
        ],
        "writeResponseContract": "Write endpoints return backup.backupId when createBackup=true. When createBackup=false they return backup.skipped=true and no automatic rollback material is created.",
        "concurrencyWarning": "Write endpoints include warnings when Codex-related processes are running.",
        "authorization": f"Write endpoints require {api_token_header_name}. Browser clients can fetch the token from GET /api/auth/token only from the loopback same-origin local UI.",
        "csrfProtection": "Unsafe methods reject browser requests outside the loopback same-origin local UI and require a custom token header.",
        "mcp": "MCP tools are available at /mcp. Write tools use the same local token, preview ticket, input hash, acknowledgement and operation lock model as REST write endpoints.",
        "previewBinding": "Dangerous writes require operationPreviewId and inputHash returned by the matching preview endpoint within 10 minutes.",
        "runningCodexWriteGate": "When Codex-related processes are running, write endpoints fail with HTTP 409 unless acknowledgeCodexRunningRisk=true is supplied in the query string or JSON body after token and preview validation.",
        "optionalBackups": "Automatic backups are enabled by default. Pass createBackup=false for one write when you intentionally do not want rollback material.",
        "operationLock": "Write endpoints are serialized by a process-local operation lock.",
    }
    common_query_parameters = {
        "codex_home": "Optional target Codex Home path. Defaults to CODEX_HOME env or the first detected local .codex profile.",
        "sidebar_limit": "Legacy Codex Desktop first-page ordering size. Defaults to 50 and is kept only for compatibility diagnostics.",
        "acknowledgeCodexRunningRisk": "Required for write endpoints that use query-only actions when Codex-related processes are running.",
        "createBackup": "Defaults to true. Set to false on write endpoints to skip automatic backup creation; explicit manual backup endpoints always create backups.",
        "operationPreviewId": "Preview id returned by the matching preview endpoint for query-only writes.",
        "inputHash": "Input hash returned by the matching preview endpoint for query-only writes.",
        "lang": "Capability description language. Use en or zh.",
    }
    if language == "zh":
        safety_model.update({
            "deleteBehavior": "线程删除默认实现为归档；不会永久删除 JSONL 文件。",
            "resourcePathPolicy": "资源 API 只接受相对于 CODEX_HOME 的路径，并拒绝绝对路径或 .. 跳出。",
            "writeResponseContract": "写入接口在 createBackup=true 时返回 backup.backupId；createBackup=false 时返回 backup.skipped=true，且不会创建自动回滚材料。",
            "concurrencyWarning": "当检测到 Codex 相关进程正在运行时，写入接口会返回风险提示。",
            "authorization": f"写入接口需要 {api_token_header_name}。调用本地写入 API 前，先通过 GET /api/auth/token 获取 token。",
            "csrfProtection": "非安全方法会拒绝不可信浏览器 Origin，并要求自定义 token header，因此跨站表单或 fetch 不能执行写入。",
            "previewBinding": "危险写入需要携带匹配预览接口在 10 分钟内返回的 operationPreviewId 和 inputHash。",
            "runningCodexWriteGate": "Codex 相关进程运行中时，写入接口会返回 HTTP 409；只有在 token 和预览校验后显式提供 acknowledgeCodexRunningRisk=true 才继续执行。",
            "optionalBackups": "自动备份默认开启。若本次写入明确不需要回滚材料，可传 createBackup=false。",
            "operationLock": "写入接口由进程内操作锁串行执行。",
        })
        common_query_parameters.update({
            "codex_home": "用于指定目标 Codex Home 路径；默认使用 CODEX_HOME 环境变量，未设置时自动探测本机 .codex 配置。",
            "sidebar_limit": "用于指定 Codex Desktop 初始侧边栏页大小；默认 50。",
            "acknowledgeCodexRunningRisk": "用于在 Codex 相关进程运行中确认继续执行 query-only 写入操作。",
            "createBackup": "用于指定是否创建自动备份；默认 true。写入接口传 false 会跳过自动备份，显式手动备份接口仍然总是创建备份。",
            "operationPreviewId": "用于指定匹配预览接口返回的预览 ID，query-only 写入需要携带。",
            "inputHash": "用于指定匹配预览接口返回的输入哈希，query-only 写入需要携带。",
            "lang": "用于指定能力说明语言；可传 en 或 zh。",
        })

    return {
        "service": "codex-home-manager",
        "version": app.version,
        "language": language,
        "openapiPath": "/openapi.json",
        "mcpPath": "/mcp",
        "safetyModel": safety_model,
        "commonQueryParameters": common_query_parameters,
        "capabilities": [
            item("auth_token", "GET", "/api/auth/token", "Get the per-process token required for write endpoints. Browser access is restricted to the loopback same-origin local UI.", [], "read-only", success_fields=["token", "headerName"]),
            item("diagnostics", "GET", "/api/diagnostics", "Run a read-only Codex Home health check covering local state, thread visibility, bundled plugin cache/config alignment, logs, storage and runtime process risks, and return a repairPrompt handoff for another Codex agent.", [], "read-only", success_fields=["score", "status", "summary", "checks", "issues", "topRecommendations", "repairPrompt"]),
            item("preview_official_thread_tools_repair", "POST", "/api/codex/official-thread-tools/repair/preview", "Preview whether legacy codex_thread_messenger MCP fallback is active and can hide official codex_app thread tools such as send_message_to_thread.", [], "read-only", success_fields=["operationPreviewId", "inputHash", "needsRepair", "config", "threadToolRegistry", "verificationSteps"]),
            item("repair_official_thread_tools", "POST", "/api/codex/official-thread-tools/repair", "Disable the legacy codex_thread_messenger MCP fallback in config.toml after preview validation. This does not edit SQLite dynamic tools and still requires a full Codex Desktop restart plus visible target-thread verification.", ["X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "home state backup before config.toml rewrite", {"acknowledgeCodexRunningRisk": True, "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["changed", "restartRequired", "backup.backupId", "before", "after"]),
            item("snapshot_threads", "GET", "/api/snapshot", "List and classify all local Codex threads. Current Codex versions can show the full thread list; legacy first-page ordering is exposed only as compatibility metadata.", [], "read-only", success_fields=["summary", "threads", "projects"]),
            item("get_thread_detail", "GET", "/api/threads/{thread_id}", "Read SQLite row, JSONL stats, file locations and backups for one thread. Use include_daily_tokens=false for a faster details shell, then call daily_token_usage when the timeline is opened.", ["thread_id"], "read-only", success_fields=["thread", "sqliteRow", "rolloutStats", "dailyTokenUsage", "backups"]),
            item("daily_token_usage", "GET", "/api/threads/{thread_id}/daily-tokens", "Read the per-day token timeline for one thread tree. Audited totals and peaks are derived from token_count events only. Threads with SQLite tokens_used but no token_count are marked as unknownTokenThreads; no token value is returned for them.", ["thread_id"], "read-only", success_fields=["summary", "days"]),
            item("read_thread_prompts", "GET", "/api/threads/{thread_id}/prompts", "Read classified prompt-like user role records from one thread without writing an export file. pureText contains only the user's typed/request text with file lists, image tags and internal contexts stripped.", ["thread_id"], "read-only", success_fields=["threadId", "title", "rolloutPath", "promptCount", "purePromptCount", "visiblePromptCount", "hiddenPromptCount", "sourceCounts", "prompts"]),
            item("preview_thread_action", "GET", "/api/threads/{thread_id}/action-preview", "Preview show, hide, repair, archive or duplicate before writing. Duplicate preview requires targetProjectPath.", ["thread_id", "action"], "read-only", success_fields=["operationPreviewId", "inputHash", "threadId", "warnings"]),
            item("show_thread", "POST", "/api/threads/{thread_id}/show", "Restore a hidden or archived thread to Codex Desktop's visible indexes.", ["thread_id", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "thread action backup before write", success_fields=["threadId", "updatedAtMs", "backup.backupId"]),
            item("hide_thread", "POST", "/api/threads/{thread_id}/hide", "Hide a visible main thread from Codex Desktop's visible indexes without archiving or deleting JSONL.", ["thread_id", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "thread action backup before write", success_fields=["threadId", "updatedAtMs", "sessionIndexUpdate", "globalStateUpdate", "backup.backupId"]),
            item("export_prompts", "GET", "/api/threads/{thread_id}/export-prompts", "Export prompts from a thread as markdown or json. Defaults to scope=pure, which exports only user-entered text; use scope=visible for user context too, scope=with_agents for subagent records, scope=automation for heartbeat/automation records, scope=delegation for messages sent from other Codex threads, or scope=all for complete records.", ["thread_id", "scope", "X-Codex-Manager-Token"], "writes export file in workspace data/exports", success_fields=["outputPath", "promptCount", "allPromptCount", "filterScope", "format"]),
            item("read_thread_logs", "GET", "/api/threads/{thread_id}/logs", "Read structured rollout JSONL logs and Codex app logs_2.sqlite rows with pagination, source selection, search and filters for requests, failures, errors, events, tool calls and raw lines.", ["thread_id"], "read-only", success_fields=["entries", "summary", "matchedEntries", "hasMore", "source", "rolloutPath", "appLogPath"]),
            item("duplicate_thread", "POST", "/api/threads/{thread_id}/duplicate", "Copy one thread inside the current Codex Home with a new thread id and rollout JSONL into a selected target project path.", ["thread_id", "targetProjectPath", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "thread action backup before write; restore archives created copy", {"targetProjectPath": "C:\\\\Project", "acknowledgeCodexRunningRisk": True, "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["newThreadId", "newRolloutPath", "targetProjectPath", "backup.backupId"]),
            item("archive_thread", "POST", "/api/threads/{thread_id}/archive", "Safe delete by archiving the thread without permanently deleting JSONL.", ["thread_id", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "thread action backup before write", success_fields=["threadId", "archivedAt", "backup.backupId"]),
            item("preview_migrate_thread", "POST", "/api/threads/{thread_id}/migrate/preview", "Preview thread project migration before rewriting SQLite/global state/JSONL workspace metadata.", ["thread_id", "targetProjectPath"], "read-only", {"targetProjectPath": "C:\\\\Project"}, ["operationPreviewId", "inputHash", "threadId", "targetProjectPath", "warnings"]),
            item("migrate_thread", "POST", "/api/threads/{thread_id}/migrate", "Move a thread to another project path and rewrite structured workspace metadata in JSONL.", ["thread_id", "targetProjectPath", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "thread action backup before write; Codex must be closed", {"targetProjectPath": "C:\\\\Project", "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["threadId", "oldProjectPath", "newProjectPath", "backup.backupId"]),
            item("preview_move_thread_workspace", "POST", "/api/threads/{thread_id}/move-workspace/preview", "Preview moving the selected thread, optional same-source-cwd threads, actual workspace files, SQLite/global state and rollout workspace metadata.", ["thread_id", "targetProjectPath"], "read-only", {"targetProjectPath": "C:\\\\Project", "includeSameSourceCwdThreads": True, "moveWorkspaceFiles": True}, ["operationPreviewId", "inputHash", "matchedThreads", "fileMove", "blockingErrors", "warnings"]),
            item("move_thread_workspace", "POST", "/api/threads/{thread_id}/move-workspace", "Move the selected thread plus same-source-cwd threads and top-level workspace files to a target workspace. Codex Desktop and Codex CLI must be closed first.", ["thread_id", "targetProjectPath", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "thread/workspace move backup before write; Codex must be closed", {"targetProjectPath": "C:\\\\Project", "includeSameSourceCwdThreads": True, "moveWorkspaceFiles": True, "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["threadId", "matchedThreads", "fileMove", "rolloutRewrites", "backup.backupId"]),
            item("slim_thread", "POST", "/api/threads/{thread_id}/slim", "Reduce JSONL size only within the explicitly selected scope: removeImages, keepLatestCompacted, or both.", ["thread_id", "removeImages or keepLatestCompacted", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "thread action backup before JSONL rewrite", {"removeImages": True, "keepLatestCompacted": False, "acknowledgeCodexRunningRisk": True, "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["threadId", "savedBytes", "before", "after", "backup.backupId"]),
            item("preview_slim_thread", "GET", "/api/threads/{thread_id}/slim/preview", "Preview JSONL slimming impact for a selected scope without writing files.", ["thread_id"], "read-only", success_fields=["threadId", "scan", "canRemoveImages", "canReduceCompacted", "warnings"]),
            item("rename_project", "POST", "/api/projects/rename", "Rename project path metadata and optionally the local project folder. Codex Desktop and Codex CLI must be closed because they can rewrite sidebar cache state while running.", ["sourceProjectPath", "targetProjectPath", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "backs up all matched thread rows, rollout JSONLs, global state, global state backup and config", {"sourceProjectPath": "C:\\\\OldProject", "targetProjectPath": "C:\\\\NewProject", "renameFolder": True, "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["updatedThreads", "rolloutBackups", "globalStateRewrite", "backup.backupId"]),
            item("preview_rename_project", "POST", "/api/projects/rename/preview", "Preview matched threads, rollout bytes, folder impact and whether running Codex blocks the project rename.", ["sourceProjectPath", "targetProjectPath"], "read-only", {"sourceProjectPath": "C:\\\\OldProject", "targetProjectPath": "C:\\\\NewProject", "renameFolder": True}, ["matchedThreads", "existingRollouts", "rolloutBytes", "willRenameFolder", "blockedByRunningCodex", "warnings"]),
            item("import_thread_from_home", "POST", "/api/import/thread", "Copy one thread from another Codex Home into the current Codex Home.", ["sourceCodexHome", "sourceThreadId", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "home state backup before import; restore archives imported thread", {"sourceCodexHome": "E:\\\\.codex", "sourceThreadId": "THREAD_ID", "targetProjectPath": "C:\\\\Project", "acknowledgeCodexRunningRisk": True, "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["importedThreads", "backup.backupId"]),
            item("preview_import_thread_from_home", "POST", "/api/import/thread/preview", "Preview one source thread import without writing target home.", ["sourceCodexHome", "sourceThreadId"], "read-only", {"sourceCodexHome": "E:\\\\.codex", "sourceThreadId": "THREAD_ID", "targetProjectPath": "C:\\\\Project"}, ["sourceThreadId", "targetThreadId", "rolloutBytes", "warnings"]),
            item("import_project_from_home", "POST", "/api/import/project", "Copy all matched project threads from another Codex Home into the current Codex Home.", ["sourceCodexHome", "sourceProjectPath", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "home state backup before import; restore archives imported threads", {"sourceCodexHome": "E:\\\\.codex", "sourceProjectPath": "C:\\\\OldProject", "targetProjectPath": "C:\\\\NewProject", "includeArchived": False, "acknowledgeCodexRunningRisk": True, "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["importedThreads", "backup.backupId"]),
            item("preview_import_project_from_home", "POST", "/api/import/project/preview", "Preview source project import thread count and rollout bytes without writing target home.", ["sourceCodexHome", "sourceProjectPath"], "read-only", {"sourceCodexHome": "E:\\\\.codex", "sourceProjectPath": "C:\\\\OldProject", "targetProjectPath": "C:\\\\NewProject", "includeArchived": False}, ["matchedThreads", "rolloutBytes", "warnings"]),
            item("home_overview", "GET", "/api/home/overview", "Inventory CODEX_HOME resources such as sessions, memories, skills, AGENTS.md, config and logs.", [], "read-only", success_fields=["summary", "resources"]),
            item("read_resource", "GET", "/api/resources/read", "Read a text resource or list a directory inside CODEX_HOME.", ["relative_path"], "read-only", success_fields=["metadata", "content", "children", "binary"]),
            item("backup_resource", "POST", "/api/resources/backup", "Create an explicit backup of a CODEX_HOME resource.", ["relativePath", "X-Codex-Manager-Token"], "resource backup", {"relativePath": "AGENTS.md"}, ["resourcePath", "resourceBackupPath", "backup.backupId"]),
            item("preview_write_resource", "POST", "/api/resources/write/preview", "Preview text resource write and protected-path validation without writing.", ["relativePath", "content"], "read-only", {"relativePath": "AGENTS.md", "content": "instructions"}, ["operationPreviewId", "inputHash", "target", "willOverwrite", "warnings"]),
            item("write_resource", "POST", "/api/resources/write", "Write a UTF-8 text resource such as AGENTS.md or memory notes inside CODEX_HOME. Protected state files and session JSONL are rejected.", ["relativePath", "content", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "backs up overwritten target resource before write", {"relativePath": "AGENTS.md", "content": "instructions", "acknowledgeCodexRunningRisk": True, "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["resourcePath", "sizeBytes", "backup.backupId"]),
            item("copy_resource_from_home", "POST", "/api/resources/copy-from-home", "Copy AGENTS.md, memories, skills or safe text resources from another Codex Home. Protected Codex state files are rejected.", ["sourceCodexHome", "relativePath", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "backs up overwritten target resource before copy", {"sourceCodexHome": "E:\\\\.codex", "relativePath": "memories", "targetRelativePath": "memories", "overwrite": True, "acknowledgeCodexRunningRisk": True, "operationPreviewId": "PREVIEW_ID", "inputHash": "INPUT_HASH"}, ["sourcePath", "targetPath", "backup.backupId", "overwroteExisting"]),
            item("preview_copy_resource_from_home", "POST", "/api/resources/copy-from-home/preview", "Preview resource copy size and overwrite impact without writing target home.", ["sourceCodexHome", "relativePath"], "read-only", {"sourceCodexHome": "E:\\\\.codex", "relativePath": "memories", "targetRelativePath": "memories"}, ["source", "target", "willOverwrite", "warnings"]),
            item("preview_restore_backup", "GET", "/api/backups/{backup_id}/restore/preview", "Preview backup restore notes and bind a restore operation.", ["backup_id"], "read-only", success_fields=["operationPreviewId", "inputHash", "backupId", "action", "warnings"]),
            item("restore_backup", "POST", "/api/backups/{backup_id}/restore", "Restore a backup manifest or archive imported/duplicated created threads.", ["backup_id", "X-Codex-Manager-Token", "operationPreviewId", "inputHash"], "creates pre_restore backup before restore", success_fields=["restoredBackupId", "notes"]),
        ],
    }


@app.post("/api/codex/official-thread-tools/repair/preview", response_model=PreviewResponse)
def preview_official_thread_tools_repair_endpoint(
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        result = preview_official_thread_tools_repair(codex_home_text=codex_home)
        result.update(create_preview_ticket("repair_official_thread_tools_exposure", official_thread_tools_repair_payload(codex_home)))
        return result
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/codex/official-thread-tools/repair", response_model=None)
def repair_official_thread_tools_endpoint(
    request: OfficialThreadToolsRepairRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "repair_official_thread_tools_exposure",
            official_thread_tools_repair_payload(codex_home),
            request.operationPreviewId,
            request.inputHash,
        ):
            return repair_official_thread_tools_exposure(
                codex_home_text=codex_home,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/home/overview", response_model=HomeOverviewResponse)
def home_overview(codex_home: str | None = Query(default=None)) -> dict[str, Any]:
    try:
        return codex_home_overview(codex_home)
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/snapshot", response_model=SnapshotResponse)
def snapshot(
    codex_home: str | None = Query(default=None),
    sidebar_limit: int = Query(default=50, ge=1, le=1000),
    validate_rollout_display: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return build_snapshot(
            codex_home_text=codex_home,
            sidebar_limit=sidebar_limit,
            validate_rollout_display=validate_rollout_display,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/resources/read", response_model=ResourceReadResponse)
def read_resource(
    relative_path: str = Query(default=""),
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return read_codex_resource(codex_home_text=codex_home, relative_path=relative_path)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/resources/backup", response_model=ResourceBackupResponse)
def backup_resource(
    request: ResourceBackupRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with write_lock:
            return backup_codex_resource(codex_home_text=codex_home, relative_path=request.relativePath)
    except HTTPException as error:
        raise error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/resources/write/preview", response_model=PreviewResponse)
def preview_write_resource(
    request: WriteResourceRequest,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        result = preview_write_codex_resource(
            codex_home_text=codex_home,
            relative_path=request.relativePath,
            content=request.content,
            create_parent_directories=request.createParentDirectories,
        )
        result.update(create_preview_ticket("write_resource", resource_write_payload(codex_home, request)))
        return result
    except IsADirectoryError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/resources/write", response_model=ResourceWriteResponse)
def write_resource(
    request: WriteResourceRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "write_resource",
            resource_write_payload(codex_home, request),
            request.operationPreviewId,
            request.inputHash,
        ):
            return write_codex_resource(
                codex_home_text=codex_home,
                relative_path=request.relativePath,
                content=request.content,
                create_parent_directories=request.createParentDirectories,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except IsADirectoryError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/resources/copy-from-home", response_model=ResourceCopyResponse)
def copy_resource(
    request: CopyResourceRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "copy_resource_from_home",
            resource_copy_payload(codex_home, request),
            request.operationPreviewId,
            request.inputHash,
        ):
            return copy_resource_from_home(
                target_codex_home_text=codex_home,
                source_codex_home_text=request.sourceCodexHome,
                relative_path=request.relativePath,
                target_relative_path=request.targetRelativePath,
                overwrite=request.overwrite,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/resources/copy-from-home/preview", response_model=PreviewResponse)
def preview_copy_resource(
    request: PreviewResourceCopyRequest,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        result = preview_resource_copy(
            target_codex_home_text=codex_home,
            source_codex_home_text=request.sourceCodexHome,
            relative_path=request.relativePath,
            target_relative_path=request.targetRelativePath,
        )
        result.update(create_preview_ticket("copy_resource_from_home", resource_copy_payload(codex_home, request)))
        return result
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/threads/{thread_id}", response_model=ThreadDetailResponse)
def thread_detail(
    thread_id: str,
    codex_home: str | None = Query(default=None),
    sidebar_limit: int = Query(default=50, ge=1, le=1000),
    include_daily_tokens: bool = Query(default=True),
) -> dict[str, Any]:
    try:
        return get_thread_detail(
            codex_home_text=codex_home,
            thread_id=thread_id,
            sidebar_limit=sidebar_limit,
            include_daily_token_usage=include_daily_tokens,
        )
    except HTTPException as error:
        raise error
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/threads/{thread_id}/daily-tokens", response_model=ThreadDailyTokenUsageResponse)
def thread_daily_tokens(
    thread_id: str,
    codex_home: str | None = Query(default=None),
    sidebar_limit: int = Query(default=50, ge=1, le=1000),
) -> dict[str, Any]:
    try:
        return get_thread_daily_token_usage(codex_home_text=codex_home, thread_id=thread_id, sidebar_limit=sidebar_limit)
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/threads/{thread_id}/logs", response_model=ThreadLogsResponse)
def thread_logs(
    thread_id: str,
    codex_home: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    kind: str = Query(default="all"),
    search: str = Query(default=""),
    source: str = Query(default="all", pattern="^(all|rollout|app)$"),
) -> dict[str, Any]:
    try:
        return read_thread_logs(
            codex_home_text=codex_home,
            thread_id=thread_id,
            offset=offset,
            limit=limit,
            kind_filter=kind,
            search_text=search,
            source_filter=source,
        )
    except HTTPException as error:
        raise error
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/threads/{thread_id}/prompts", response_model=ThreadPromptsResponse)
def thread_prompts_endpoint(
    thread_id: str,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return read_thread_prompts(codex_home_text=codex_home, thread_id=thread_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/threads/{thread_id}/action-preview", response_model=ThreadActionPreviewResponse)
def preview_thread_action_endpoint(
    thread_id: str,
    action: str = Query(pattern="^(show|hide|repair_user_event|archive|duplicate)$"),
    target_project_path: str | None = Query(default=None, alias="targetProjectPath"),
    codex_home: str | None = Query(default=None),
    sidebar_limit: int = Query(default=50, ge=1, le=1000),
) -> dict[str, Any]:
    try:
        thread = preview_thread_from_snapshot(codex_home, thread_id, sidebar_limit)
        if action == "duplicate":
            target_path = (target_project_path or "").strip()
            if not target_path:
                raise HTTPException(status_code=400, detail="targetProjectPath is required for duplicate preview")
            payload = duplicate_payload(codex_home, thread_id, target_path)
        else:
            target_path = None
            payload = thread_action_payload(codex_home, thread_id, action)
        return {
            **create_preview_ticket(action, payload),
            "action": action,
            "threadId": thread_id,
            "title": thread.get("title"),
            "projectPath": thread.get("projectPath"),
            "targetProjectPath": target_path,
            "rolloutPath": thread.get("rolloutPath"),
            "rolloutStat": {
                "exists": thread.get("fileExists"),
                "sizeBytes": thread.get("fileSizeBytes"),
                "modifiedAtMs": thread.get("fileModifiedAtMs"),
            },
            "warnings": validate_environment(codex_home).get("writeWarnings", []),
        }
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/threads/{thread_id}/slim/preview", response_model=SlimPreviewResponse)
def preview_slim_thread_endpoint(
    thread_id: str,
    remove_images: bool = Query(default=True, alias="removeImages"),
    keep_latest_compacted: bool = Query(default=True, alias="keepLatestCompacted"),
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        if not remove_images and not keep_latest_compacted:
            raise HTTPException(status_code=400, detail="select at least one slimming scope")
        result = preview_slim_thread(codex_home_text=codex_home, thread_id=thread_id)
        result.update(create_preview_ticket("slim_thread", slim_payload(codex_home, thread_id, remove_images, keep_latest_compacted)))
        return result
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/migrate/preview", response_model=PreviewResponse)
def preview_migrate_thread_endpoint(
    thread_id: str,
    request: MigrateThreadRequest,
    codex_home: str | None = Query(default=None),
    sidebar_limit: int = Query(default=50, ge=1, le=1000),
) -> dict[str, Any]:
    try:
        detail = get_thread_detail(codex_home_text=codex_home, thread_id=thread_id, sidebar_limit=sidebar_limit)
        thread = detail["thread"]
        return {
            **create_preview_ticket("migrate_thread", migrate_payload(codex_home, thread_id, request.targetProjectPath)),
            "threadId": thread_id,
            "sourceProjectPath": thread.get("projectPath"),
            "targetProjectPath": request.targetProjectPath,
            "sourcePath": thread.get("rolloutPath"),
            "source": {
                "path": thread.get("rolloutPath"),
                "exists": thread.get("fileExists"),
                "sizeBytes": thread.get("fileSizeBytes"),
            },
            "warnings": [
                *validate_environment(codex_home).get("writeWarnings", []),
                "Thread migration requires Codex Desktop and Codex CLI to be closed; acknowledgeCodexRunningRisk cannot make this operation safe.",
            ],
        }
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/move-workspace/preview", response_model=PreviewResponse)
def preview_move_thread_workspace_endpoint(
    thread_id: str,
    request: MoveThreadWorkspaceRequest,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        result = preview_thread_workspace_move(
            codex_home_text=codex_home,
            thread_id=thread_id,
            target_project_path=request.targetProjectPath,
            include_same_source_cwd_threads=request.includeSameSourceCwdThreads,
            move_workspace_files=request.moveWorkspaceFiles,
            repair_user_event=request.repairUserEvent,
            preserve_pinned=request.preservePinned,
        )
        result.update(create_preview_ticket("move_thread_workspace", move_thread_workspace_payload(codex_home, thread_id, request)))
        return result
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/import/thread/preview", response_model=PreviewResponse)
def preview_import_thread_endpoint(
    request: ImportThreadRequest,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        result = preview_import_thread_from_home(
            target_codex_home_text=codex_home,
            source_codex_home_text=request.sourceCodexHome,
            source_thread_id=request.sourceThreadId,
            target_project_path=request.targetProjectPath,
            preserve_thread_id=request.preserveThreadId,
        )
        result.update(create_preview_ticket("import_thread_from_home", import_thread_payload(codex_home, request)))
        return result
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"source thread not found: {request.sourceThreadId}") from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/import/thread", response_model=ImportResponse)
def import_thread_endpoint(
    request: ImportThreadRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "import_thread_from_home",
            import_thread_payload(codex_home, request),
            request.operationPreviewId,
            request.inputHash,
        ):
            return import_thread_from_home(
                target_codex_home_text=codex_home,
                source_codex_home_text=request.sourceCodexHome,
                source_thread_id=request.sourceThreadId,
                target_project_path=request.targetProjectPath,
                preserve_thread_id=request.preserveThreadId,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"source thread not found: {request.sourceThreadId}") from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/import/project/preview", response_model=PreviewResponse)
def preview_import_project_endpoint(
    request: ImportProjectRequest,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        result = preview_import_project_from_home(
            target_codex_home_text=codex_home,
            source_codex_home_text=request.sourceCodexHome,
            source_project_path=request.sourceProjectPath,
            target_project_path=request.targetProjectPath,
            include_archived=request.includeArchived,
        )
        result.update(create_preview_ticket("import_project_from_home", import_project_payload(codex_home, request)))
        return result
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/import/project", response_model=ImportResponse)
def import_project_endpoint(
    request: ImportProjectRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "import_project_from_home",
            import_project_payload(codex_home, request),
            request.operationPreviewId,
            request.inputHash,
        ):
            return import_project_from_home(
                target_codex_home_text=codex_home,
                source_codex_home_text=request.sourceCodexHome,
                source_project_path=request.sourceProjectPath,
                target_project_path=request.targetProjectPath,
                include_archived=request.includeArchived,
                preserve_thread_ids=request.preserveThreadIds,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/show", response_model=ShowThreadResponse)
def show_thread(
    thread_id: str,
    http_request: Request,
    codex_home: str | None = Query(default=None),
    acknowledge_codex_running_risk: bool = Query(default=False, alias="acknowledgeCodexRunningRisk"),
    create_backup: bool = Query(default=True, alias="createBackup"),
    operation_preview_id: str | None = Query(default=None, alias="operationPreviewId"),
    input_hash: str | None = Query(default=None, alias="inputHash"),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "show",
            thread_action_payload(codex_home, thread_id, "show"),
            operation_preview_id,
            input_hash,
        ):
            return show_thread_in_sidebar(
                codex_home_text=codex_home,
                thread_id=thread_id,
                acknowledge_codex_running_risk=acknowledge_codex_running_risk,
                create_backup=create_backup,
            )
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/hide", response_model=HideThreadResponse)
def hide_thread(
    thread_id: str,
    http_request: Request,
    codex_home: str | None = Query(default=None),
    acknowledge_codex_running_risk: bool = Query(default=False, alias="acknowledgeCodexRunningRisk"),
    create_backup: bool = Query(default=True, alias="createBackup"),
    operation_preview_id: str | None = Query(default=None, alias="operationPreviewId"),
    input_hash: str | None = Query(default=None, alias="inputHash"),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "hide",
            thread_action_payload(codex_home, thread_id, "hide"),
            operation_preview_id,
            input_hash,
        ):
            return hide_thread_from_sidebar(
                codex_home_text=codex_home,
                thread_id=thread_id,
                acknowledge_codex_running_risk=acknowledge_codex_running_risk,
                create_backup=create_backup,
            )
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/repair-user-event", response_model=ThreadActionResponse)
def repair_thread_user_event(
    thread_id: str,
    http_request: Request,
    codex_home: str | None = Query(default=None),
    acknowledge_codex_running_risk: bool = Query(default=False, alias="acknowledgeCodexRunningRisk"),
    create_backup: bool = Query(default=True, alias="createBackup"),
    operation_preview_id: str | None = Query(default=None, alias="operationPreviewId"),
    input_hash: str | None = Query(default=None, alias="inputHash"),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "repair_user_event",
            thread_action_payload(codex_home, thread_id, "repair_user_event"),
            operation_preview_id,
            input_hash,
        ):
            return repair_user_event(
                codex_home_text=codex_home,
                thread_id=thread_id,
                acknowledge_codex_running_risk=acknowledge_codex_running_risk,
                create_backup=create_backup,
            )
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/backup", response_model=BackupManifestResponse)
def create_thread_backup(
    thread_id: str,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with write_lock:
            return backup_thread(codex_home_text=codex_home, thread_id=thread_id)
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/archive", response_model=ArchiveThreadResponse)
def archive_thread_endpoint(
    thread_id: str,
    http_request: Request,
    codex_home: str | None = Query(default=None),
    acknowledge_codex_running_risk: bool = Query(default=False, alias="acknowledgeCodexRunningRisk"),
    create_backup: bool = Query(default=True, alias="createBackup"),
    operation_preview_id: str | None = Query(default=None, alias="operationPreviewId"),
    input_hash: str | None = Query(default=None, alias="inputHash"),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "archive",
            thread_action_payload(codex_home, thread_id, "archive"),
            operation_preview_id,
            input_hash,
        ):
            return archive_thread(
                codex_home_text=codex_home,
                thread_id=thread_id,
                acknowledge_codex_running_risk=acknowledge_codex_running_risk,
                create_backup=create_backup,
            )
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/duplicate", response_model=DuplicateThreadResponse)
def duplicate_thread_endpoint(
    thread_id: str,
    request: DuplicateThreadRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "duplicate",
            duplicate_payload(codex_home, thread_id, request.targetProjectPath),
            request.operationPreviewId,
            request.inputHash,
        ):
            return duplicate_thread(
                codex_home_text=codex_home,
                thread_id=thread_id,
                target_project_path=request.targetProjectPath,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/migrate", response_model=MigrateThreadResponse)
def migrate_thread_endpoint(
    thread_id: str,
    request: MigrateThreadRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "migrate_thread",
            migrate_payload(codex_home, thread_id, request.targetProjectPath),
            request.operationPreviewId,
            request.inputHash,
        ):
            return migrate_thread_project(
                codex_home_text=codex_home,
                thread_id=thread_id,
                target_project_path=request.targetProjectPath,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/move-workspace", response_model=MoveThreadWorkspaceResponse)
def move_thread_workspace_endpoint(
    thread_id: str,
    request: MoveThreadWorkspaceRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "move_thread_workspace",
            move_thread_workspace_payload(codex_home, thread_id, request),
            request.operationPreviewId,
            request.inputHash,
        ):
            return move_thread_workspace(
                codex_home_text=codex_home,
                thread_id=thread_id,
                target_project_path=request.targetProjectPath,
                include_same_source_cwd_threads=request.includeSameSourceCwdThreads,
                move_workspace_files=request.moveWorkspaceFiles,
                repair_user_event=request.repairUserEvent,
                preserve_pinned=request.preservePinned,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/threads/{thread_id}/slim", response_model=SlimThreadResponse)
def slim_thread_endpoint(
    thread_id: str,
    request: SlimThreadRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        if not request.removeImages and not request.keepLatestCompacted:
            raise HTTPException(status_code=400, detail="select at least one slimming scope")
        with preview_bound_write(
            "slim_thread",
            slim_payload(codex_home, thread_id, request.removeImages, request.keepLatestCompacted),
            request.operationPreviewId,
            request.inputHash,
        ):
            return slim_thread(
                codex_home_text=codex_home,
                thread_id=thread_id,
                remove_images=request.removeImages,
                keep_latest_compacted=request.keepLatestCompacted,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/threads/{thread_id}/export-prompts", response_model=ExportPromptsResponse)
def export_prompts_endpoint(
    thread_id: str,
    http_request: Request,
    format: str = Query(default="markdown", pattern="^(markdown|json)$"),
    scope: str = Query(default="pure", pattern="^(pure|visible|all|with_agents|automation|heartbeat|delegation)$"),
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with write_lock:
            return export_thread_prompts(codex_home_text=codex_home, thread_id=thread_id, output_format=format, scope=scope)
    except HTTPException as error:
        raise error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/projects/rename", response_model=RenameProjectResponse)
def rename_project_endpoint(
    request: RenameProjectRequest,
    http_request: Request,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "rename_project",
            rename_payload(codex_home, request),
            request.operationPreviewId,
            request.inputHash,
        ):
            return rename_project(
                codex_home_text=codex_home,
                source_project_path=request.sourceProjectPath,
                target_project_path=request.targetProjectPath,
                rename_folder=request.renameFolder,
                acknowledge_codex_running_risk=request.acknowledgeCodexRunningRisk,
                create_backup=request.createBackup,
            )
    except HTTPException as error:
        raise error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/projects/rename/preview", response_model=PreviewResponse)
def preview_rename_project_endpoint(
    request: RenameProjectRequest,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        result = preview_project_rename(
            codex_home_text=codex_home,
            source_project_path=request.sourceProjectPath,
            target_project_path=request.targetProjectPath,
            rename_folder=request.renameFolder,
        )
        result.update(create_preview_ticket("rename_project", rename_payload(codex_home, request)))
        return result
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/backups", response_model=BackupListResponse)
def backups(
    codex_home: str | None = Query(default=None),
    thread_id: str | None = Query(default=None),
) -> dict[str, Any]:
    return {"backups": list_backups(codex_home, thread_id=thread_id)}


@app.post("/api/backups/{backup_id}/restore", response_model=RestoreResponse)
def restore_thread_backup(
    backup_id: str,
    http_request: Request,
    acknowledge_codex_running_risk: bool = Query(default=False, alias="acknowledgeCodexRunningRisk"),
    create_backup: bool = Query(default=True, alias="createBackup"),
    operation_preview_id: str | None = Query(default=None, alias="operationPreviewId"),
    input_hash: str | None = Query(default=None, alias="inputHash"),
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        authorize_write_request(http_request)
        with preview_bound_write(
            "restore_backup",
            restore_payload(codex_home, backup_id),
            operation_preview_id,
            input_hash,
        ):
            return restore_backup(
                backup_id,
                codex_home,
                acknowledge_codex_running_risk=acknowledge_codex_running_risk,
                create_backup=create_backup,
            )
    except HTTPException as error:
        raise error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"backup not found: {backup_id}") from error
    except RuntimeError as error:
        raise runtime_conflict(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/backups/{backup_id}/restore/preview", response_model=PreviewResponse)
def preview_restore_thread_backup(
    backup_id: str,
    codex_home: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        matched_backup = next(
            (backup for backup in list_backups(codex_home) if backup.get("backupId") == backup_id),
            None,
        )
        if matched_backup is None:
            raise FileNotFoundError(backup_id)
        return {
            **create_preview_ticket("restore_backup", restore_payload(codex_home, backup_id)),
            "backupId": backup_id,
            "action": matched_backup.get("action"),
            "threadId": matched_backup.get("threadId"),
            "sourcePath": matched_backup.get("manifestPath"),
            "warnings": validate_environment(matched_backup.get("codexHome")).get("writeWarnings", []),
        }
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"backup not found: {backup_id}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


mcp_protocol_version = "2025-06-18"
mcp_text_limit = 80_000


class McpRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def mcp_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def mcp_tool(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": mcp_schema(properties, required),
    }


string_schema = {"type": "string"}
optional_codex_home_schema = {"type": "string", "description": "Optional CODEX_HOME path. Empty uses the local default."}
sidebar_limit_schema = {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50}
api_token_schema = {"type": "string", "description": f"Local write token from /api/auth/token or the MCP codex_auth_token tool."}
preview_id_schema = {"type": "string", "description": "operationPreviewId returned by the matching preview tool."}
input_hash_schema = {"type": "string", "description": "inputHash returned by the matching preview tool."}
ack_schema = {"type": "boolean", "default": False, "description": "Set true after reviewing runtime warnings when Codex is running."}
backup_schema = {"type": "boolean", "default": True, "description": "Set false to skip automatic rollback backup for this write."}


def mcp_base_properties() -> dict[str, Any]:
    return {"codexHome": optional_codex_home_schema, "apiToken": api_token_schema}


def mcp_preview_properties(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**mcp_base_properties(), **(extra or {})}


def mcp_write_properties(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        **mcp_base_properties(),
        "apiToken": api_token_schema,
        "operationPreviewId": preview_id_schema,
        "inputHash": input_hash_schema,
        "acknowledgeCodexRunningRisk": ack_schema,
        "createBackup": backup_schema,
        **(extra or {}),
    }


def mcp_manual_write_properties(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        **mcp_base_properties(),
        "apiToken": api_token_schema,
        **(extra or {}),
    }


def mcp_tool_definitions() -> list[dict[str, Any]]:
    thread_id = {"threadId": {"type": "string", "description": "Codex thread UUID."}}
    target_project = {"targetProjectPath": {"type": "string", "description": "Target project cwd/path for the operation."}}
    workspace_move_options = {
        "includeSameSourceCwdThreads": {"type": "boolean", "default": True, "description": "Also move all threads whose cwd exactly matches the selected thread source cwd."},
        "moveWorkspaceFiles": {"type": "boolean", "default": True, "description": "Move top-level filesystem entries from the source workspace directory into the target workspace directory."},
        "repairUserEvent": {"type": "boolean", "default": True, "description": "Set has_user_event=1 for moved threads so they remain visible in project views."},
        "preservePinned": {"type": "boolean", "default": False, "description": "Keep moved threads in pinned-thread-ids instead of moving them into the target project group."},
    }
    resource_path = {"relativePath": {"type": "string", "description": "Path relative to CODEX_HOME."}}
    source_home = {"sourceCodexHome": {"type": "string", "description": "Source .codex root directory."}}
    return [
        mcp_tool("codex_health", "Read the local connector health and runtime write warnings.", mcp_base_properties()),
        mcp_tool("codex_auth_token", "Return a short-lived local token bound to one real Codex Home.", {"codexHome": optional_codex_home_schema}),
        mcp_tool("codex_diagnostics", "Run the read-only Codex Home health check and return a repairPrompt handoff for another Codex agent.", {**mcp_base_properties(), "sidebarLimit": sidebar_limit_schema, "language": {"type": "string", "enum": ["zh", "en"], "default": "zh"}, "refresh": {"type": "boolean", "default": False, "description": "Bypass the short-lived report cache and run a fresh full scan."}}),
        mcp_tool("codex_preview_official_thread_tools_repair", "Preview whether legacy codex_thread_messenger MCP fallback is active and can hide official codex_app thread tools.", mcp_preview_properties()),
        mcp_tool("codex_repair_official_thread_tools", "Disable the legacy codex_thread_messenger MCP fallback in config.toml after preview validation. Requires a full Codex Desktop restart and target-thread verification after the write.", mcp_write_properties(), ["apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_snapshot", "List and classify local Codex threads and projects.", {**mcp_base_properties(), "sidebarLimit": sidebar_limit_schema}),
        mcp_tool("codex_home_overview", "Inventory CODEX_HOME resources, memories, skills, config and logs.", mcp_base_properties()),
        mcp_tool("codex_read_resource", "Read a text resource or list a directory inside CODEX_HOME.", mcp_preview_properties(resource_path), ["relativePath"]),
        mcp_tool("codex_thread_detail", "Read SQLite row, JSONL stats, file locations and backups for one thread. Set includeDailyTokens=false when an agent wants a faster detail shell.", mcp_preview_properties({**thread_id, "sidebarLimit": sidebar_limit_schema, "includeDailyTokens": {"type": "boolean", "default": True}}), ["threadId"]),
        mcp_tool("codex_thread_daily_tokens", "Read the per-day token timeline for one thread tree. Audited totals and peaks are derived from token_count events only. Threads with SQLite tokens_used but no token_count are marked as unknownTokenThreads; no token value is returned for them.", mcp_preview_properties({**thread_id, "sidebarLimit": sidebar_limit_schema}), ["threadId"]),
        mcp_tool("codex_thread_logs", "Read structured JSONL/app logs for one thread with pagination and filters.", mcp_preview_properties({**thread_id, "offset": {"type": "integer", "minimum": 0, "default": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}, "kind": {"type": "string", "default": "all"}, "search": {"type": "string", "default": ""}, "source": {"type": "string", "enum": ["all", "rollout", "app"], "default": "all"}}), ["threadId"]),
        mcp_tool("codex_thread_prompts", "Read all user prompts from one thread without writing an export file.", mcp_preview_properties(thread_id), ["threadId"]),
        mcp_tool("codex_list_backups", "List rollback backups, optionally filtered by thread id.", {"threadId": {"type": "string"}}),
        mcp_tool("codex_preview_thread_action", "Preview show, hide, repair, archive or duplicate before writing.", mcp_preview_properties({**thread_id, "action": {"type": "string", "enum": ["show", "hide", "repair_user_event", "archive", "duplicate"]}, **target_project, "sidebarLimit": sidebar_limit_schema}), ["threadId", "action"]),
        mcp_tool("codex_preview_slim_thread", "Preview JSONL slimming impact for selected scopes.", mcp_preview_properties({**thread_id, "removeImages": {"type": "boolean", "default": True}, "keepLatestCompacted": {"type": "boolean", "default": True}}), ["threadId"]),
        mcp_tool("codex_preview_migrate_thread", "Preview moving a thread to another project path.", mcp_preview_properties({**thread_id, **target_project, "sidebarLimit": sidebar_limit_schema}), ["threadId", "targetProjectPath"]),
        mcp_tool("codex_preview_move_thread_workspace", "Preview moving a thread, same-source-cwd threads, actual workspace files, SQLite/global state and rollout metadata.", mcp_preview_properties({**thread_id, **target_project, **workspace_move_options}), ["threadId", "targetProjectPath"]),
        mcp_tool("codex_preview_import_thread", "Preview copying one thread from another Codex Home.", mcp_preview_properties({**source_home, "sourceThreadId": {"type": "string"}, **target_project, "preserveThreadId": {"type": "boolean", "default": False}}), ["sourceCodexHome", "sourceThreadId"]),
        mcp_tool("codex_preview_import_project", "Preview copying matched project threads from another Codex Home.", mcp_preview_properties({**source_home, "sourceProjectPath": {"type": "string"}, **target_project, "includeArchived": {"type": "boolean", "default": False}, "preserveThreadIds": {"type": "boolean", "default": False}}), ["sourceCodexHome", "sourceProjectPath"]),
        mcp_tool("codex_preview_rename_project", "Preview project path rename impact and whether running Codex blocks the write.", mcp_preview_properties({"sourceProjectPath": {"type": "string"}, "targetProjectPath": {"type": "string"}, "renameFolder": {"type": "boolean", "default": True}}), ["sourceProjectPath", "targetProjectPath"]),
        mcp_tool("codex_preview_write_resource", "Preview UTF-8 text resource write and protected-path validation.", mcp_preview_properties({**resource_path, "content": {"type": "string"}, "createParentDirectories": {"type": "boolean", "default": True}}), ["relativePath", "content"]),
        mcp_tool("codex_preview_copy_resource", "Preview copying a safe resource from another Codex Home.", mcp_preview_properties({**source_home, **resource_path, "targetRelativePath": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}), ["sourceCodexHome", "relativePath"]),
        mcp_tool("codex_preview_restore_backup", "Preview restoring a backup and bind a restore ticket.", {"backupId": {"type": "string"}}, ["backupId"]),
        mcp_tool("codex_show_thread", "Restore a hidden or archived thread to Codex Desktop's visible indexes.", mcp_write_properties(thread_id), ["threadId", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_hide_thread", "Hide a visible main thread from Codex Desktop's visible indexes without deleting JSONL.", mcp_write_properties(thread_id), ["threadId", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_repair_thread_user_event", "Repair user-event metadata and restore the thread to Codex Desktop's visible indexes.", mcp_write_properties(thread_id), ["threadId", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_archive_thread", "Archive a thread as the safe delete operation without permanent JSONL deletion.", mcp_write_properties(thread_id), ["threadId", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_duplicate_thread", "Duplicate a thread to a target project path.", mcp_write_properties({**thread_id, **target_project}), ["threadId", "targetProjectPath", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_migrate_thread", "Move a thread to another project path.", mcp_write_properties({**thread_id, **target_project}), ["threadId", "targetProjectPath", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_move_thread_workspace", "Move a thread plus same-source-cwd threads and top-level workspace files to a target workspace. Codex Desktop and Codex CLI must be closed first.", mcp_write_properties({**thread_id, **target_project, **workspace_move_options}), ["threadId", "targetProjectPath", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_slim_thread", "Slim a thread JSONL using the selected safe scopes.", mcp_write_properties({**thread_id, "removeImages": {"type": "boolean", "default": True}, "keepLatestCompacted": {"type": "boolean", "default": True}}), ["threadId", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_export_prompts", "Export prompts from a thread as markdown or json. scope=pure exports only user-entered text; scope=automation exports heartbeat/automation records; scope=delegation exports messages sent from other Codex threads; scope=all exports every prompt-like record.", mcp_manual_write_properties({**thread_id, "format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"}, "scope": {"type": "string", "enum": ["pure", "visible", "with_agents", "automation", "delegation", "all"], "default": "pure"}}), ["threadId", "apiToken"]),
        mcp_tool("codex_backup_thread", "Create an explicit rollback backup for one thread.", mcp_manual_write_properties(thread_id), ["threadId", "apiToken"]),
        mcp_tool("codex_backup_resource", "Create an explicit backup for a CODEX_HOME resource.", mcp_manual_write_properties(resource_path), ["relativePath", "apiToken"]),
        mcp_tool("codex_write_resource", "Write a safe UTF-8 text resource inside CODEX_HOME.", mcp_write_properties({**resource_path, "content": {"type": "string"}, "createParentDirectories": {"type": "boolean", "default": True}}), ["relativePath", "content", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_copy_resource", "Copy a safe resource from another Codex Home.", mcp_write_properties({**source_home, **resource_path, "targetRelativePath": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}), ["sourceCodexHome", "relativePath", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_import_thread", "Copy one thread from another Codex Home into this Codex Home.", mcp_write_properties({**source_home, "sourceThreadId": {"type": "string"}, **target_project, "preserveThreadId": {"type": "boolean", "default": False}}), ["sourceCodexHome", "sourceThreadId", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_import_project", "Copy matched project threads from another Codex Home into this Codex Home.", mcp_write_properties({**source_home, "sourceProjectPath": {"type": "string"}, **target_project, "includeArchived": {"type": "boolean", "default": False}, "preserveThreadIds": {"type": "boolean", "default": False}}), ["sourceCodexHome", "sourceProjectPath", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_rename_project", "Rename project path metadata and optionally the local project folder. Codex Desktop and Codex CLI must be closed first.", mcp_write_properties({"sourceProjectPath": {"type": "string"}, "targetProjectPath": {"type": "string"}, "renameFolder": {"type": "boolean", "default": True}}), ["sourceProjectPath", "targetProjectPath", "apiToken", "operationPreviewId", "inputHash"]),
        mcp_tool("codex_restore_backup", "Restore a backup after validating the matching preview ticket.", {**mcp_write_properties(), "backupId": {"type": "string"}}, ["backupId", "apiToken", "operationPreviewId", "inputHash"]),
    ]


def mcp_tool_names() -> set[str]:
    return {tool["name"] for tool in mcp_tool_definitions()}


def mcp_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) > mcp_text_limit:
        text = text[:mcp_text_limit] + "\n... [truncated]"
    result = {"content": [{"type": "text", "text": text}], "structuredContent": payload}
    if is_error:
        result["isError"] = True
    return result


def mcp_error_result(error: Exception | str, status_code: int = 500) -> dict[str, Any]:
    if isinstance(error, HTTPException):
        status_code = int(error.status_code)
        detail = error.detail
    else:
        detail = str(error)
    return mcp_result({"status": status_code, "error": detail}, is_error=True)


def mcp_rpc_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def mcp_rpc_error_response(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error_payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error_payload["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error_payload}


def mcp_arguments(params: dict[str, Any]) -> dict[str, Any]:
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise McpRpcError(-32602, "tools/call arguments must be an object")
    return arguments


def mcp_str(arguments: dict[str, Any], name: str, default: str | None = None) -> str | None:
    value = arguments.get(name, default)
    if value is None:
        return None
    return str(value)


def mcp_required_str(arguments: dict[str, Any], name: str) -> str:
    value = mcp_str(arguments, name)
    if value is None or value == "":
        raise HTTPException(status_code=400, detail=f"{name} is required")
    return value


def mcp_bool(arguments: dict[str, Any], name: str, default: bool = False) -> bool:
    value = arguments.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def mcp_int(arguments: dict[str, Any], name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(arguments.get(name, default))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=f"{name} must be an integer") from error
    if minimum is not None and value < minimum:
        raise HTTPException(status_code=400, detail=f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise HTTPException(status_code=400, detail=f"{name} must be <= {maximum}")
    return value


def mcp_model(model_class: type[BaseModel], arguments: dict[str, Any]) -> BaseModel:
    fields = getattr(model_class, "model_fields", None) or getattr(model_class, "__fields__", {})
    payload = {key: arguments[key] for key in fields if key in arguments}
    return model_class(**payload)


def mcp_require_write(arguments: dict[str, Any]) -> None:
    require_api_token(mcp_str(arguments, "apiToken"))


def mcp_thread_action_preview(arguments: dict[str, Any]) -> dict[str, Any]:
    codex_home = mcp_str(arguments, "codexHome")
    thread_id = mcp_required_str(arguments, "threadId")
    action = mcp_required_str(arguments, "action")
    sidebar_limit = mcp_int(arguments, "sidebarLimit", 50, 1, 1000)
    thread = preview_thread_from_snapshot(codex_home, thread_id, sidebar_limit)
    if action == "duplicate":
        target_path = mcp_required_str(arguments, "targetProjectPath")
        payload = duplicate_payload(codex_home, thread_id, target_path)
    else:
        target_path = None
        payload = thread_action_payload(codex_home, thread_id, action)
    return {
        **create_preview_ticket(action, payload),
        "action": action,
        "threadId": thread_id,
        "title": thread.get("title"),
        "projectPath": thread.get("projectPath"),
        "targetProjectPath": target_path,
        "rolloutPath": thread.get("rolloutPath"),
        "rolloutStat": {
            "exists": thread.get("fileExists"),
            "sizeBytes": thread.get("fileSizeBytes"),
            "modifiedAtMs": thread.get("fileModifiedAtMs"),
        },
        "warnings": validate_environment(codex_home).get("writeWarnings", []),
    }


def mcp_execute_tool(name: str, arguments: dict[str, Any], request: Request) -> dict[str, Any]:
    try:
        codex_home = mcp_str(arguments, "codexHome")
        if name == "codex_auth_token":
            authorize_browser_write_origin(request)
            return mcp_result(create_local_authorization(codex_home))
        authorize_local_data_request(request, codex_home, mcp_str(arguments, "apiToken"))
        if name == "codex_health":
            return mcp_result(validate_environment(codex_home))
        if name == "codex_diagnostics":
            return mcp_result(cached_codex_diagnostics(
                codex_home=codex_home,
                sidebar_limit=mcp_int(arguments, "sidebarLimit", 50, 1, 1000),
                language=mcp_str(arguments, "language", "zh") or "zh",
                force_refresh=mcp_bool(arguments, "refresh", False),
            ))
        if name == "codex_preview_official_thread_tools_repair":
            result = preview_official_thread_tools_repair(codex_home_text=codex_home)
            result.update(create_preview_ticket("repair_official_thread_tools_exposure", official_thread_tools_repair_payload(codex_home)))
            return mcp_result(result)
        if name == "codex_repair_official_thread_tools":
            mcp_require_write(arguments)
            with preview_bound_write(
                "repair_official_thread_tools_exposure",
                official_thread_tools_repair_payload(codex_home),
                mcp_str(arguments, "operationPreviewId"),
                mcp_str(arguments, "inputHash"),
            ):
                return mcp_result(
                    repair_official_thread_tools_exposure(
                        codex_home_text=codex_home,
                        acknowledge_codex_running_risk=mcp_bool(arguments, "acknowledgeCodexRunningRisk", False),
                        create_backup=mcp_bool(arguments, "createBackup", True),
                    )
                )
        if name == "codex_snapshot":
            return mcp_result(build_snapshot(
                codex_home_text=codex_home,
                sidebar_limit=mcp_int(arguments, "sidebarLimit", 50, 1, 1000),
                validate_rollout_display=bool(arguments.get("validateRolloutDisplay") or False),
            ))
        if name == "codex_home_overview":
            return mcp_result(codex_home_overview(codex_home))
        if name == "codex_read_resource":
            return mcp_result(read_codex_resource(codex_home_text=codex_home, relative_path=mcp_required_str(arguments, "relativePath")))
        if name == "codex_thread_detail":
            return mcp_result(get_thread_detail(
                codex_home_text=codex_home,
                thread_id=mcp_required_str(arguments, "threadId"),
                sidebar_limit=mcp_int(arguments, "sidebarLimit", 50, 1, 1000),
                include_daily_token_usage=bool(arguments.get("includeDailyTokens", True)),
            ))
        if name == "codex_thread_daily_tokens":
            return mcp_result(get_thread_daily_token_usage(
                codex_home_text=codex_home,
                thread_id=mcp_required_str(arguments, "threadId"),
                sidebar_limit=mcp_int(arguments, "sidebarLimit", 50, 1, 1000),
            ))
        if name == "codex_thread_logs":
            return mcp_result(read_thread_logs(
                codex_home_text=codex_home,
                thread_id=mcp_required_str(arguments, "threadId"),
                offset=mcp_int(arguments, "offset", 0, 0, None),
                limit=mcp_int(arguments, "limit", 100, 1, 500),
                kind_filter=mcp_str(arguments, "kind", "all") or "all",
                search_text=mcp_str(arguments, "search", "") or "",
                source_filter=mcp_str(arguments, "source", "all") or "all",
            ))
        if name == "codex_thread_prompts":
            return mcp_result(read_thread_prompts(codex_home_text=codex_home, thread_id=mcp_required_str(arguments, "threadId")))
        if name == "codex_list_backups":
            return mcp_result({"backups": list_backups(codex_home, thread_id=mcp_str(arguments, "threadId"))})
        if name == "codex_preview_thread_action":
            return mcp_result(mcp_thread_action_preview(arguments))
        if name == "codex_preview_slim_thread":
            if not mcp_bool(arguments, "removeImages", True) and not mcp_bool(arguments, "keepLatestCompacted", True):
                raise HTTPException(status_code=400, detail="select at least one slimming scope")
            result = preview_slim_thread(codex_home_text=codex_home, thread_id=mcp_required_str(arguments, "threadId"))
            result.update(create_preview_ticket("slim_thread", slim_payload(codex_home, mcp_required_str(arguments, "threadId"), mcp_bool(arguments, "removeImages", True), mcp_bool(arguments, "keepLatestCompacted", True))))
            return mcp_result(result)
        if name == "codex_preview_migrate_thread":
            thread_id = mcp_required_str(arguments, "threadId")
            target_project_path = mcp_required_str(arguments, "targetProjectPath")
            detail = get_thread_detail(codex_home_text=codex_home, thread_id=thread_id, sidebar_limit=mcp_int(arguments, "sidebarLimit", 50, 1, 1000))
            thread = detail["thread"]
            return mcp_result({
                **create_preview_ticket("migrate_thread", migrate_payload(codex_home, thread_id, target_project_path)),
                "threadId": thread_id,
                "sourceProjectPath": thread.get("projectPath"),
                "targetProjectPath": target_project_path,
                "sourcePath": thread.get("rolloutPath"),
                "source": {"path": thread.get("rolloutPath"), "exists": thread.get("fileExists"), "sizeBytes": thread.get("fileSizeBytes")},
                "warnings": validate_environment(codex_home).get("writeWarnings", []),
            })
        if name == "codex_preview_move_thread_workspace":
            thread_id = mcp_required_str(arguments, "threadId")
            request_model = mcp_model(MoveThreadWorkspaceRequest, arguments)
            result = preview_thread_workspace_move(
                codex_home_text=codex_home,
                thread_id=thread_id,
                target_project_path=request_model.targetProjectPath,
                include_same_source_cwd_threads=request_model.includeSameSourceCwdThreads,
                move_workspace_files=request_model.moveWorkspaceFiles,
                repair_user_event=request_model.repairUserEvent,
                preserve_pinned=request_model.preservePinned,
            )
            result.update(create_preview_ticket("move_thread_workspace", move_thread_workspace_payload(codex_home, thread_id, request_model)))
            return mcp_result(result)
        if name == "codex_preview_import_thread":
            request_model = mcp_model(ImportThreadRequest, arguments)
            result = preview_import_thread_from_home(codex_home, request_model.sourceCodexHome, request_model.sourceThreadId, request_model.targetProjectPath, request_model.preserveThreadId)
            result.update(create_preview_ticket("import_thread_from_home", import_thread_payload(codex_home, request_model)))
            return mcp_result(result)
        if name == "codex_preview_import_project":
            request_model = mcp_model(ImportProjectRequest, arguments)
            result = preview_import_project_from_home(codex_home, request_model.sourceCodexHome, request_model.sourceProjectPath, request_model.targetProjectPath, request_model.includeArchived)
            result.update(create_preview_ticket("import_project_from_home", import_project_payload(codex_home, request_model)))
            return mcp_result(result)
        if name == "codex_preview_rename_project":
            request_model = mcp_model(RenameProjectRequest, arguments)
            result = preview_project_rename(codex_home, request_model.sourceProjectPath, request_model.targetProjectPath, request_model.renameFolder)
            result.update(create_preview_ticket("rename_project", rename_payload(codex_home, request_model)))
            return mcp_result(result)
        if name == "codex_preview_write_resource":
            request_model = mcp_model(WriteResourceRequest, arguments)
            result = preview_write_codex_resource(codex_home, request_model.relativePath, request_model.content, request_model.createParentDirectories)
            result.update(create_preview_ticket("write_resource", resource_write_payload(codex_home, request_model)))
            return mcp_result(result)
        if name == "codex_preview_copy_resource":
            request_model = mcp_model(PreviewResourceCopyRequest, arguments)
            result = preview_resource_copy(codex_home, request_model.sourceCodexHome, request_model.relativePath, request_model.targetRelativePath)
            result.update(create_preview_ticket("copy_resource_from_home", resource_copy_payload(codex_home, request_model)))
            return mcp_result(result)
        if name == "codex_preview_restore_backup":
            backup_id = mcp_required_str(arguments, "backupId")
            matched_backup = next(
                (backup for backup in list_backups(codex_home) if backup.get("backupId") == backup_id),
                None,
            )
            if matched_backup is None:
                raise HTTPException(status_code=404, detail=f"backup not found: {backup_id}")
            return mcp_result({
                **create_preview_ticket("restore_backup", restore_payload(codex_home, backup_id)),
                "backupId": backup_id,
                "action": matched_backup.get("action"),
                "threadId": matched_backup.get("threadId"),
                "sourcePath": matched_backup.get("manifestPath"),
                "warnings": validate_environment(matched_backup.get("codexHome")).get("writeWarnings", []),
            })
        mcp_require_write(arguments)
        acknowledge = mcp_bool(arguments, "acknowledgeCodexRunningRisk", False)
        create_backup = mcp_bool(arguments, "createBackup", True)
        thread_id = mcp_str(arguments, "threadId")
        if name == "codex_show_thread":
            with preview_bound_write("show", thread_action_payload(codex_home, mcp_required_str(arguments, "threadId"), "show"), mcp_str(arguments, "operationPreviewId"), mcp_str(arguments, "inputHash")):
                return mcp_result(show_thread_in_sidebar(codex_home, mcp_required_str(arguments, "threadId"), acknowledge, create_backup))
        if name == "codex_hide_thread":
            with preview_bound_write("hide", thread_action_payload(codex_home, mcp_required_str(arguments, "threadId"), "hide"), mcp_str(arguments, "operationPreviewId"), mcp_str(arguments, "inputHash")):
                return mcp_result(hide_thread_from_sidebar(codex_home, mcp_required_str(arguments, "threadId"), acknowledge, create_backup))
        if name == "codex_repair_thread_user_event":
            with preview_bound_write("repair_user_event", thread_action_payload(codex_home, mcp_required_str(arguments, "threadId"), "repair_user_event"), mcp_str(arguments, "operationPreviewId"), mcp_str(arguments, "inputHash")):
                return mcp_result(repair_user_event(codex_home, mcp_required_str(arguments, "threadId"), acknowledge, create_backup))
        if name == "codex_archive_thread":
            with preview_bound_write("archive", thread_action_payload(codex_home, mcp_required_str(arguments, "threadId"), "archive"), mcp_str(arguments, "operationPreviewId"), mcp_str(arguments, "inputHash")):
                return mcp_result(archive_thread(codex_home, mcp_required_str(arguments, "threadId"), acknowledge, create_backup))
        if name == "codex_duplicate_thread":
            target_project_path = mcp_required_str(arguments, "targetProjectPath")
            with preview_bound_write("duplicate", duplicate_payload(codex_home, mcp_required_str(arguments, "threadId"), target_project_path), mcp_str(arguments, "operationPreviewId"), mcp_str(arguments, "inputHash")):
                return mcp_result(duplicate_thread(codex_home, mcp_required_str(arguments, "threadId"), target_project_path, acknowledge, create_backup))
        if name == "codex_migrate_thread":
            request_model = mcp_model(MigrateThreadRequest, arguments)
            with preview_bound_write("migrate_thread", migrate_payload(codex_home, mcp_required_str(arguments, "threadId"), request_model.targetProjectPath), request_model.operationPreviewId, request_model.inputHash):
                return mcp_result(migrate_thread_project(codex_home, mcp_required_str(arguments, "threadId"), request_model.targetProjectPath, request_model.acknowledgeCodexRunningRisk, request_model.createBackup))
        if name == "codex_move_thread_workspace":
            thread_id = mcp_required_str(arguments, "threadId")
            request_model = mcp_model(MoveThreadWorkspaceRequest, arguments)
            with preview_bound_write(
                "move_thread_workspace",
                move_thread_workspace_payload(codex_home, thread_id, request_model),
                request_model.operationPreviewId,
                request_model.inputHash,
            ):
                return mcp_result(
                    move_thread_workspace(
                        codex_home_text=codex_home,
                        thread_id=thread_id,
                        target_project_path=request_model.targetProjectPath,
                        include_same_source_cwd_threads=request_model.includeSameSourceCwdThreads,
                        move_workspace_files=request_model.moveWorkspaceFiles,
                        repair_user_event=request_model.repairUserEvent,
                        preserve_pinned=request_model.preservePinned,
                        acknowledge_codex_running_risk=request_model.acknowledgeCodexRunningRisk,
                        create_backup=request_model.createBackup,
                    )
                )
        if name == "codex_slim_thread":
            request_model = mcp_model(SlimThreadRequest, arguments)
            if not request_model.removeImages and not request_model.keepLatestCompacted:
                raise HTTPException(status_code=400, detail="select at least one slimming scope")
            with preview_bound_write("slim_thread", slim_payload(codex_home, mcp_required_str(arguments, "threadId"), request_model.removeImages, request_model.keepLatestCompacted), request_model.operationPreviewId, request_model.inputHash):
                return mcp_result(slim_thread(codex_home, mcp_required_str(arguments, "threadId"), request_model.removeImages, request_model.keepLatestCompacted, request_model.acknowledgeCodexRunningRisk, request_model.createBackup))
        if name == "codex_export_prompts":
            with write_lock:
                return mcp_result(export_thread_prompts(codex_home, mcp_required_str(arguments, "threadId"), mcp_str(arguments, "format", "markdown") or "markdown", mcp_str(arguments, "scope", "pure") or "pure"))
        if name == "codex_backup_thread":
            with write_lock:
                return mcp_result(backup_thread(codex_home, mcp_required_str(arguments, "threadId")))
        if name == "codex_backup_resource":
            with write_lock:
                return mcp_result(backup_codex_resource(codex_home, mcp_required_str(arguments, "relativePath")))
        if name == "codex_write_resource":
            request_model = mcp_model(WriteResourceRequest, arguments)
            with preview_bound_write("write_resource", resource_write_payload(codex_home, request_model), request_model.operationPreviewId, request_model.inputHash):
                return mcp_result(write_codex_resource(codex_home, request_model.relativePath, request_model.content, request_model.createParentDirectories, request_model.acknowledgeCodexRunningRisk, request_model.createBackup))
        if name == "codex_copy_resource":
            request_model = mcp_model(CopyResourceRequest, arguments)
            with preview_bound_write("copy_resource_from_home", resource_copy_payload(codex_home, request_model), request_model.operationPreviewId, request_model.inputHash):
                return mcp_result(copy_resource_from_home(codex_home, request_model.sourceCodexHome, request_model.relativePath, request_model.targetRelativePath, request_model.overwrite, request_model.acknowledgeCodexRunningRisk, request_model.createBackup))
        if name == "codex_import_thread":
            request_model = mcp_model(ImportThreadRequest, arguments)
            with preview_bound_write("import_thread_from_home", import_thread_payload(codex_home, request_model), request_model.operationPreviewId, request_model.inputHash):
                return mcp_result(import_thread_from_home(codex_home, request_model.sourceCodexHome, request_model.sourceThreadId, request_model.targetProjectPath, request_model.preserveThreadId, request_model.acknowledgeCodexRunningRisk, request_model.createBackup))
        if name == "codex_import_project":
            request_model = mcp_model(ImportProjectRequest, arguments)
            with preview_bound_write("import_project_from_home", import_project_payload(codex_home, request_model), request_model.operationPreviewId, request_model.inputHash):
                return mcp_result(import_project_from_home(codex_home, request_model.sourceCodexHome, request_model.sourceProjectPath, request_model.targetProjectPath, request_model.includeArchived, request_model.preserveThreadIds, request_model.acknowledgeCodexRunningRisk, request_model.createBackup))
        if name == "codex_rename_project":
            request_model = mcp_model(RenameProjectRequest, arguments)
            with preview_bound_write("rename_project", rename_payload(codex_home, request_model), request_model.operationPreviewId, request_model.inputHash):
                return mcp_result(rename_project(codex_home, request_model.sourceProjectPath, request_model.targetProjectPath, request_model.renameFolder, request_model.acknowledgeCodexRunningRisk, request_model.createBackup))
        if name == "codex_restore_backup":
            backup_id = mcp_required_str(arguments, "backupId")
            with preview_bound_write("restore_backup", restore_payload(codex_home, backup_id), mcp_str(arguments, "operationPreviewId"), mcp_str(arguments, "inputHash")):
                return mcp_result(
                    restore_backup(
                        backup_id,
                        codex_home,
                        acknowledge_codex_running_risk=acknowledge,
                        create_backup=create_backup,
                    )
                )
        raise McpRpcError(-32602, f"unknown MCP tool: {name}")
    except ValidationError as error:
        return mcp_error_result({"validationErrors": json.loads(error.json())}, status_code=422)
    except HTTPException as error:
        return mcp_error_result(error)
    except (KeyError, FileNotFoundError) as error:
        return mcp_error_result(error, status_code=404)
    except (ValueError, IsADirectoryError) as error:
        return mcp_error_result(error, status_code=400)
    except RuntimeError as error:
        return mcp_error_result(error, status_code=409)
    except McpRpcError:
        raise
    except Exception as error:
        return mcp_error_result(error, status_code=500)


def mcp_handle_rpc(payload: dict[str, Any], request: Request) -> dict[str, Any] | None:
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise McpRpcError(-32602, "params must be an object")
    if method == "initialize":
        return mcp_rpc_response(request_id, {
            "protocolVersion": mcp_protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "codex-home-manager", "version": app.version},
            "instructions": "Use tools/list, then call read and preview tools before any write tool. Write tools require apiToken, operationPreviewId and inputHash.",
        })
    if method == "notifications/initialized":
        return None if request_id is None else mcp_rpc_response(request_id, {})
    if method == "ping":
        return mcp_rpc_response(request_id, {})
    if method == "tools/list":
        return mcp_rpc_response(request_id, {"tools": mcp_tool_definitions()})
    if method == "tools/call":
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            raise McpRpcError(-32602, "tools/call requires a tool name")
        if tool_name not in mcp_tool_names():
            raise McpRpcError(-32602, f"unknown MCP tool: {tool_name}")
        return mcp_rpc_response(request_id, mcp_execute_tool(tool_name, mcp_arguments(params), request))
    raise McpRpcError(-32601, f"method not found: {method}")


@app.get("/mcp")
def mcp_metadata() -> dict[str, Any]:
    return {
        "service": "codex-home-manager",
        "version": app.version,
        "protocolVersion": mcp_protocol_version,
        "transport": "streamable-http-json-rpc",
        "endpoint": "/mcp",
        "tools": [tool["name"] for tool in mcp_tool_definitions()],
        "security": {
            "readTools": "No token required.",
            "writeTools": f"Require apiToken matching {api_token_header_name}; dangerous writes also require a matching preview ticket and inputHash.",
        },
    }


@app.post("/mcp", response_model=None)
async def mcp_endpoint(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError as error:
        return mcp_rpc_error_response(None, -32700, "parse error", str(error))
    payloads = payload if isinstance(payload, list) else [payload]
    if not all(isinstance(entry, dict) for entry in payloads):
        return mcp_rpc_error_response(None, -32600, "request must be an object or array of objects")
    responses: list[dict[str, Any]] = []
    for entry in payloads:
        request_id = entry.get("id")
        try:
            response = mcp_handle_rpc(entry, request)
            if response is not None:
                responses.append(response)
        except McpRpcError as error:
            responses.append(mcp_rpc_error_response(request_id, error.code, error.message, error.data))
        except Exception as error:
            responses.append(mcp_rpc_error_response(request_id, -32603, "internal error", str(error)))
    if isinstance(payload, list):
        return responses
    if not responses:
        return Response(status_code=204)
    return responses[0]


static_directory = Path(__file__).resolve().parents[1] / "dist"
if static_directory.exists():
    app.mount("/", NoCacheStaticFiles(directory=static_directory, html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("backend.server:app", host="127.0.0.1", port=8765, reload=False)
