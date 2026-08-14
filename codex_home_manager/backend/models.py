from __future__ import annotations

from pydantic import BaseModel


class PromptIndexClearRequest(BaseModel):
    operationPreviewId: str | None = None
    inputHash: str | None = None


class PromptIndexDatabaseStatus(BaseModel):
    sizeBytes: int
    inUse: bool
    activeOperations: int
    readable: bool | None = None
    inspectionState: str
    lastAccessedAtMs: int | None = None
    schemaVersion: int | None = None
    sourceRolloutCount: int | None = None
    missingSourceRolloutCount: int | None = None
    promptCount: int | None = None
    timelineEventCount: int | None = None


class PromptIndexStorageStatus(BaseModel):
    rootPath: str
    databaseCount: int
    activeDatabaseCount: int
    totalSizeBytes: int
    maxTotalBytes: int
    maxIdleSeconds: int
    overCapacity: bool


class PromptIndexStatusResponse(BaseModel):
    databaseExists: bool
    database: PromptIndexDatabaseStatus | None = None
    storage: PromptIndexStorageStatus


class PromptIndexClearPreviewResponse(BaseModel):
    operationPreviewId: str
    inputHash: str
    expiresAtMs: int
    stateDigest: str
    willClear: bool
    reclaimableBytes: int
    inUse: bool
    warning: str


class PromptIndexClearResponse(BaseModel):
    cleared: bool
    databaseExisted: bool
    deletedFileCount: int
    reclaimedBytes: int
