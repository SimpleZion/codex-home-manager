export type ProviderInteropPhase =
  | "idle"
  | "loading"
  | "ready"
  | "blocked"
  | "applying"
  | "verifying"
  | "failed";

export type RepairScope = {
  history: boolean;
  continuationCompatibility: boolean;
  commonConfig: boolean;
  interoperabilitySettings: boolean;
};

export type ProviderInteropStatus = {
  runtimeCompatibility?: {
    classification: "verified_patched" | "official_upgrade_detected" | "patched_runtime_invalid" | "runtime_contract_outdated" | "runtime_activation_conflict" | "official_runtime_active" | "unmanaged_override" | "unmanaged" | "unknown";
    safeForProviderSwitch: boolean;
    safeForDesktopUpgrade: boolean;
    officialUpdateDetected: boolean;
    recommendedAction: string;
    contractVersion?: string;
    official?: { path?: string; version?: string; sha256?: string; appVersion?: string };
    override?: { path?: string; version?: string; sha256?: string };
    issues?: Array<{ code: string; message: string }>;
  };
  environment: {
    codexHome: string;
    configPath: string;
    stateDatabasePath: string;
  };
  adapter: {
    kind: string;
    support: "read_write" | "read_only" | "not_found";
    root?: string | null;
    userVersion?: number | null;
    schemaFingerprint?: string | null;
    databaseQuickCheck?: string | null;
    boundCodexHome?: string | null;
    currentProviderName?: string | null;
    currentProviderIdHash?: string | null;
    settings?: {
      preserveOfficialAuth?: boolean | null;
      unifySessionHistory?: boolean | null;
      migrateExistingHistory?: boolean | null;
    };
    reason?: string | null;
  };
  route: {
    classification: "official" | "third_party" | "local" | "conflicting" | "unknown";
    confidence: "observed" | "configuration_only" | "conflicting" | "unknown";
    modelProvider?: string | null;
    providerName?: string | null;
    baseUrlPresent: boolean;
  };
  authentication: {
    officialAuthRequired: "present" | "absent" | "unknown";
    providerToken: "present" | "absent" | "unknown" | "[REDACTED]";
  };
  historyBuckets: Record<string, number>;
};

export type ProviderInteropAudit = {
  scannedFileCount: number;
  changedFileCount: number;
  replacementCount: number;
  continuationChangedFileCount: number;
  nonportableReasoningCount: number;
  incompatibleItemIdCount: number;
  unsupportedForeignItemCount: number;
  parseErrorCount: number;
  requiredBackupBytes: number;
  providerBuckets: Record<string, number>;
  promptFingerprint: { count: number; sha256: string };
  protectedContentSha256: string;
};

export type AdapterRepairSummary = {
  commonConfigChanged: boolean;
  commonConfigAdditions: number;
  commonConfigConflicts: string[];
  commonConfigExclusions: string[];
  settingChanges: string[];
  providerSnapshotCount: number;
  providerSnapshotsPreserved: boolean;
};

export type ProviderInteropPreview = {
  operationId: string;
  operationPreviewId: string;
  inputHash: string;
  workerStateDigest: string;
  status: ProviderInteropStatus;
  audit: ProviderInteropAudit;
  adapterRepair: AdapterRepairSummary | null;
  blockers: string[];
  requiresOffline: boolean;
  backupRoot: string;
  scopes: RepairScope;
};

export type ProviderInteropTransaction = {
  status: "committed" | "failed_rolled_back" | "failed_manual_recovery_required";
  operationId: string;
  backupDirectory: string;
  manifestPath: string;
  journalPath: string;
  jsonlFilesChanged: number;
  jsonlReplacements: number;
  sqliteRowsChanged: number;
};

export type ProviderInteropVerification = {
  status: ProviderInteropStatus;
  audit: ProviderInteropAudit;
  adapterRepair: AdapterRepairSummary | null;
  blockers: string[];
  scopes: RepairScope;
  verified: boolean;
};

export type RollbackPreview = {
  operationPreviewId: string;
  inputHash: string;
  manifestPath: string;
  operationId: string;
  workerStateDigest: string;
  requiresOffline: boolean;
};

export type TaskCompatibilityAudit = {
  threadId: string;
  providerBucket: string;
  visibilityCompatible: boolean;
  continuationCompatible: boolean;
  incompatibleItemCount: number;
  nonPortableReasoningCount: number;
  latestValidCheckpoint?: string | null;
  promptFingerprint?: { count: number; sha256: string };
  recommendation: string;
};

export type TaskRepairPreview = {
  operationId: string;
  operationPreviewId: string;
  inputHash: string;
  workerStateDigest: string;
  threadId: string;
  status: ProviderInteropStatus;
  audit: ProviderInteropAudit;
  task: TaskCompatibilityAudit;
  blockers: string[];
  requiresOffline: boolean;
  backupRoot: string;
};

export type ReadJson = <T>(url: string, options?: RequestInit) => Promise<T>;
export type AuthorizedJson = <T>(url: string, options?: RequestInit, jsonBody?: boolean) => Promise<T>;

export const defaultRepairScope: RepairScope = {
  history: true,
  continuationCompatibility: true,
  commonConfig: true,
  interoperabilitySettings: true
};

export function providerInteropParams(codexHome: string): URLSearchParams {
  return new URLSearchParams({ codex_home: codexHome });
}

export function repairRequest(scope: RepairScope): RepairScope & { sourceBucket: "openai"; targetBucket: "custom" } {
  if (!scope.history && !scope.continuationCompatibility && !scope.commonConfig && !scope.interoperabilitySettings) {
    throw new Error("at least one provider interoperability scope is required");
  }
  return { ...scope, sourceBucket: "openai", targetBucket: "custom" };
}

export function routeNeedsAttention(status: ProviderInteropStatus | null): boolean {
  return status?.route.classification === "conflicting" || status?.route.classification === "unknown";
}

export function repairNeeded(preview: ProviderInteropPreview | null): boolean {
  if (!preview) return false;
  return preview.audit.replacementCount > 0
    || Boolean(preview.adapterRepair?.commonConfigChanged)
    || Boolean(preview.adapterRepair?.settingChanges.length);
}

export function transactionCommitted(transaction: ProviderInteropTransaction | null): boolean {
  return transaction?.status === "committed";
}
