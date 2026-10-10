from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderInteropRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapterRoot: str | None = None
    sourceBucket: Literal["openai"] = "openai"
    targetBucket: Literal["custom"] = "custom"
    history: bool = True
    continuationCompatibility: bool = True
    commonConfig: bool = True
    interoperabilitySettings: bool = True

    @model_validator(mode="after")
    def require_repair_scope(self) -> "ProviderInteropRequest":
        if not (
            self.history
            or self.continuationCompatibility
            or self.commonConfig
            or self.interoperabilitySettings
        ):
            raise ValueError("at least one provider interoperability scope is required")
        return self


class ProviderInteropApplyRequest(ProviderInteropRequest):
    operationPreviewId: str
    inputHash: str
    operationId: str
    workerStateDigest: str = Field(min_length=64, max_length=64)
    backupRoot: str | None = None


class ProviderInteropRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifestPath: str
    operationId: str
    workerStateDigest: str
    operationPreviewId: str | None = None
    inputHash: str | None = None


class ProviderInteropTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapterRoot: str | None = None


class ProviderInteropTaskApplyRequest(ProviderInteropTaskRequest):
    operationPreviewId: str
    inputHash: str
    operationId: str
    workerStateDigest: str = Field(min_length=64, max_length=64)
    backupRoot: str | None = None


def repair_ticket_payload(
    codex_home: str | None,
    request: ProviderInteropRequest,
    *,
    operation_id: str,
    worker_state_digest: str,
) -> dict[str, Any]:
    return {
        "codexHome": codex_home or "",
        "adapterRoot": request.adapterRoot or "",
        "sourceBucket": request.sourceBucket,
        "targetBucket": request.targetBucket,
        "history": request.history,
        "continuationCompatibility": request.continuationCompatibility,
        "commonConfig": request.commonConfig,
        "interoperabilitySettings": request.interoperabilitySettings,
        "operationId": operation_id,
        "workerStateDigest": worker_state_digest,
    }


def rollback_ticket_payload(
    codex_home: str | None,
    request: ProviderInteropRollbackRequest,
) -> dict[str, Any]:
    return {
        "codexHome": codex_home or "",
        "manifestPath": request.manifestPath,
        "operationId": request.operationId,
        "workerStateDigest": request.workerStateDigest,
    }


def task_repair_ticket_payload(
    codex_home: str | None,
    thread_id: str,
    request: ProviderInteropTaskRequest,
    *,
    operation_id: str,
    worker_state_digest: str,
) -> dict[str, Any]:
    return {
        "codexHome": codex_home or "",
        "threadId": thread_id,
        "adapterRoot": request.adapterRoot or "",
        "operationId": operation_id,
        "workerStateDigest": worker_state_digest,
    }
