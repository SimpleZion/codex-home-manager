param(
    [int]$TimeoutMinutes = 180,
    [int]$ExecutionTimeoutMinutes = 240,
    [int]$StageTimeoutMinutes = 75,
    [int]$QuietSeconds = 15,
    [string]$CodexHome = "D:\.codex",
    [string]$BackupParent = "D:\Backup\codex_full_repair",
    [string[]]$SlimThreadId = @(),
    [string]$SlimThreadIdsJson = "",
    [switch]$PrintNormalizedSlimThreadIdsAndExit,
    [switch]$PreflightSourceInputsOnly,
    [switch]$RecoverOnly
)

$ErrorActionPreference = "Stop"

if (-not [string]::IsNullOrWhiteSpace($SlimThreadIdsJson)) {
    try {
        $decodedSlimThreadIds = ConvertFrom-Json -InputObject $SlimThreadIdsJson -NoEnumerate
    }
    catch {
        throw "SlimThreadIdsJson must be a valid JSON array of thread IDs. $($_.Exception.Message)"
    }
    if ($decodedSlimThreadIds -is [string] -or $decodedSlimThreadIds -isnot [System.Collections.IEnumerable]) {
        throw "SlimThreadIdsJson must decode to a JSON array."
    }
    $SlimThreadId = @($SlimThreadId) + @($decodedSlimThreadIds | ForEach-Object { [string]$_ })
}
$SlimThreadId = @(
    $SlimThreadId |
        ForEach-Object { [string]$_ } |
        ForEach-Object { $_.Trim() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Sort-Object -Unique
)
if ($PrintNormalizedSlimThreadIdsAndExit) {
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject @($SlimThreadId) -Compress))
    return
}

function Stop-ProcessTreeIdempotent {
    param([Parameter(Mandatory = $true)][int]$TargetProcessId)

    $existingProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $TargetProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $existingProcess) {
        return "already_exited"
    }

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Join-Path $env:SystemRoot "System32\taskkill.exe"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in @("/PID", [string]$TargetProcessId, "/T", "/F")) {
        $null = $startInfo.ArgumentList.Add($argument)
    }

    $taskkillProcess = [System.Diagnostics.Process]::new()
    $taskkillProcess.StartInfo = $startInfo
    try {
        $null = $taskkillProcess.Start()
        $standardOutput = $taskkillProcess.StandardOutput.ReadToEnd()
        $standardError = $taskkillProcess.StandardError.ReadToEnd()
        $taskkillProcess.WaitForExit()
        $taskkillExitCode = $taskkillProcess.ExitCode
    }
    finally {
        $taskkillProcess.Dispose()
    }

    if ($taskkillExitCode -eq 0) {
        return "stopped"
    }

    $remainingProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $TargetProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $remainingProcess) {
        return "stopped"
    }

    Stop-Process -Id $TargetProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 100
    $remainingProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $TargetProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $remainingProcess) {
        return "stopped"
    }

    $taskkillEvidence = (@($standardOutput, $standardError) -join " ").Trim()
    throw "Unable to stop process tree rooted at PID $TargetProcessId. taskkill exit code: $taskkillExitCode. Output: $taskkillEvidence"
}

function Resolve-SafeContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RequiredRoot
    )

    $validator = "from pathlib import Path; import sys; candidate=Path(sys.argv[1]).resolve(strict=False); root=Path(sys.argv[2]).resolve(strict=False); candidate.relative_to(root); print(candidate)"
    $resolved = (& python -c $validator $Path $RequiredRoot | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($resolved)) {
        throw "Path escapes the required root after resolving reparse points: $Path"
    }
    return $resolved
}

$requiredBackupRoot = [System.IO.Path]::GetFullPath("D:\Backup").TrimEnd('\')
$resolvedBackupParent = (Resolve-SafeContainedPath -Path $BackupParent -RequiredRoot $requiredBackupRoot).TrimEnd('\')
$BackupParent = $resolvedBackupParent

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$auditScript = Join-Path $PSScriptRoot "audit_codex_thread_histories.py"
$applyScript = Join-Path $PSScriptRoot "apply_codex_offline_repair.py"
$pluginRepairScript = Join-Path $PSScriptRoot "repair_codex_bundled_plugins.ps1"
$pluginSnapshotScript = Join-Path $PSScriptRoot "codex_plugin_state_snapshot.py"
$nativeHostRepairScript = Join-Path $PSScriptRoot "repair_codex_chrome_native_hosts.py"
$manifestChainScript = Join-Path $PSScriptRoot "repair_manifest_chain.py"
$diagnosticsScript = Join-Path $PSScriptRoot "run_codex_diagnostics_snapshot.py"
$supersedeValidationScript = Join-Path $PSScriptRoot "supersede_pending_repair_validation.py"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$runId = [guid]::NewGuid().ToString("N")
$liveValidationChallenge = [guid]::NewGuid().ToString("N")
$runRoot = Join-Path $BackupParent "${timestamp}_$runId"
$repairDataRoot = Join-Path $runRoot "repair_data"
$auditPath = Join-Path $runRoot "offline_thread_audit.json"
$logPath = Join-Path $runRoot "offline_repair.log"
$pendingValidationPath = Join-Path $runRoot "PENDING_RESTART_VALIDATION.json"
$failurePath = Join-Path $runRoot "FAILED.txt"
$postDiagnosticsPath = Join-Path $runRoot "post_repair_diagnostics.json"
$nativeHostRepairReportPath = Join-Path $runRoot "chrome_native_host_repair.json"
$sourceBindingPath = Join-Path $runRoot "SOURCE_BINDING.json"
$sourceSnapshotRoot = Join-Path $runRoot "source_snapshot"
$pluginSnapshotRoot = Join-Path $runRoot "plugin_state_snapshot"
$pluginSnapshotManifest = Join-Path $pluginSnapshotRoot "plugin_state_snapshot.json"
$staleRestoreArchiveRoot = Join-Path $runRoot "stale_restore_artifacts"
$repairManifestPath = Join-Path $repairDataRoot "repair_manifest.json"
$mutexName = "Global\OpenAI-Codex-Full-Repair-OneShot"
$mutex = [System.Threading.Mutex]::new($false, $mutexName)
$ownsMutex = $false
$lockPath = Join-Path $BackupParent "active_repair.lock.json"
$applyCompleted = $false
$pluginSnapshotCompleted = $false
$staleRestoreArchiveStarted = $false
$staleRestoreArchiveCompleted = $false
$staleRunRoot = $null
$staleRunId = $null
$preserveActiveLock = $false
$activeLockOwnedByCurrentRun = $false
$sourceBindingSha256 = $null
$codexHomeManagerRoot = Join-Path $workspaceRoot "codex_home_manager"
$sourceRepositories = @($workspaceRoot, $codexHomeManagerRoot)
$requiredSourcePaths = @(
    $MyInvocation.MyCommand.Path,
    (Join-Path $workspaceRoot "AGENTS.md"),
    (Join-Path $workspaceRoot "pytest.ini"),
    $auditScript,
    $applyScript,
    $pluginRepairScript,
    $pluginSnapshotScript,
    $nativeHostRepairScript,
    $manifestChainScript,
    $diagnosticsScript,
    $supersedeValidationScript,
    (Join-Path $PSScriptRoot "verify_codex_after_restart.py"),
    (Join-Path $PSScriptRoot "prepare_codex_live_validation.py"),
    (Join-Path $PSScriptRoot "collect_codex_live_validation.py"),
    (Join-Path $PSScriptRoot "live_validation_contract.py"),
    (Join-Path $PSScriptRoot "merge_codex_runtime_config.py"),
    (Join-Path $PSScriptRoot "merge_codex_managed_config.py"),
    (Join-Path $codexHomeManagerRoot "backend\thread_history_repair.py"),
    (Join-Path $codexHomeManagerRoot "backend\offline_repair_policy.py"),
    (Join-Path $codexHomeManagerRoot "backend\diagnostics.py"),
    (Join-Path $codexHomeManagerRoot "backend\codex_data.py"),
    (Join-Path $codexHomeManagerRoot "backend\windows_paths.py")
)
$requiredSourcePaths += @(Get-ChildItem -LiteralPath (Join-Path $codexHomeManagerRoot "tests") -File -Filter "test_*.py" | ForEach-Object FullName)
$requiredSourcePaths += @(Get-ChildItem -LiteralPath (Join-Path $codexHomeManagerRoot "backend") -Recurse -File -Filter "*.py" | ForEach-Object FullName)
$requiredSourcePaths = @($requiredSourcePaths | Sort-Object -Unique)

function Assert-RepairSourceInputsReady {
    param(
        [Parameter(Mandatory = $true)][string[]]$Paths,
        [Parameter(Mandatory = $true)][string[]]$RepositoryPaths
    )

    $resolvedRepositories = @($RepositoryPaths | ForEach-Object { [System.IO.Path]::GetFullPath($_).TrimEnd('\') })
    foreach ($repositoryPath in $resolvedRepositories) {
        $head = (& git -C $repositoryPath rev-parse --verify HEAD | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($head)) {
            throw "Repair source repository has no valid HEAD: $repositoryPath"
        }
    }

    foreach ($path in $Paths) {
        $resolvedPath = [System.IO.Path]::GetFullPath($path)
        if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
            throw "Bound repair source is missing: $resolvedPath"
        }
        $owningRepository = @($resolvedRepositories | Where-Object {
            $resolvedPath.StartsWith($_ + '\', [System.StringComparison]::OrdinalIgnoreCase)
        } | Sort-Object Length -Descending | Select-Object -First 1)
        if ($owningRepository.Count -ne 1) {
            throw "Bound repair source has no owning repository: $resolvedPath"
        }
        $relativePath = [System.IO.Path]::GetRelativePath($owningRepository[0], $resolvedPath).Replace('\', '/')
        & git -C $owningRepository[0] ls-files --error-unmatch -- $relativePath | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Bound repair source is not tracked by Git: $resolvedPath"
        }
    }
    return [pscustomobject]@{
        status = "source_inputs_ready"
        source_file_count = @($Paths).Count
        repository_count = $resolvedRepositories.Count
    }
}

if ($PreflightSourceInputsOnly) {
    Assert-RepairSourceInputsReady -Paths $requiredSourcePaths -RepositoryPaths $sourceRepositories |
        ConvertTo-Json -Compress
    return
}

function Get-FileSha256Lower {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Get-CommittedRepairManifest {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)

    $inspectionJson = (& python $manifestChainScript --manifest $ManifestPath --inspect | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($inspectionJson)) {
        throw "Unable to inspect the committed repair manifest head: $ManifestPath"
    }
    $inspection = $inspectionJson | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace([string]$inspection.sha256) -or $null -eq $inspection.payload) {
        throw "Committed repair manifest inspection returned an invalid result: $ManifestPath"
    }
    return $inspection
}

function Write-Utf8FileDurable {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        [System.IO.Directory]::CreateDirectory($parent) | Out-Null
    }
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $bytes = $encoding.GetBytes($Content)
    $stream = [System.IO.FileStream]::new(
        $Path,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

function Write-JsonAtomicDurable {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Payload
    )

    $temporaryPath = "$Path.$([guid]::NewGuid().ToString('N')).writing"
    Write-Utf8FileDurable -Path $temporaryPath -Content ($Payload | ConvertTo-Json -Depth 10)
    [System.IO.File]::Move($temporaryPath, $Path, $true)
}

function Save-RepairFailureArtifacts {
    param(
        [Parameter(Mandatory = $true)]$FailurePayload,
        [Parameter(Mandatory = $true)][string]$FailureManifestPath,
        [Parameter(Mandatory = $true)][string]$ActiveLockPath,
        [Parameter(Mandatory = $true)][bool]$OwnsActiveLock,
        [Parameter(Mandatory = $true)][string]$FailureLockStatus,
        [Parameter(Mandatory = $true)][string]$CurrentRunId,
        [Parameter(Mandatory = $true)][string]$CurrentRunRoot,
        [Parameter(Mandatory = $true)][string]$CurrentMutexName,
        [AllowNull()][string]$CurrentSourceBinding,
        [AllowNull()][string]$CurrentSourceBindingSha256
    )

    $errors = [System.Collections.Generic.List[string]]::new()
    try {
        Write-JsonAtomicDurable -Path $FailureManifestPath -Payload $FailurePayload
    }
    catch {
        $errors.Add("failure manifest persistence: $($_.Exception.Message)")
    }
    if ($OwnsActiveLock) {
        try {
            $lockPayload = [ordered]@{
                run_id = $CurrentRunId
                process_id = $PID
                status = $FailureLockStatus
                updated_at = (Get-Date -Format o)
                mutex = $CurrentMutexName
                run_root = $CurrentRunRoot
                failure = $FailureManifestPath
                error = [string]$FailurePayload.error
                rollback_status = [string]$FailurePayload.rollback_status
                rollback_errors = @($FailurePayload.rollback_errors)
                source_binding = $CurrentSourceBinding
                source_binding_sha256 = $CurrentSourceBindingSha256
            }
            Write-JsonAtomicDurable -Path $ActiveLockPath -Payload $lockPayload
        }
        catch {
            $errors.Add("active failure lock persistence: $($_.Exception.Message)")
        }
    }
    return @($errors)
}

function Switch-ActiveRepairLock {
    param(
        [Parameter(Mandatory = $true)][string]$LockPath,
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$ExpectedRunId,
        [Parameter(Mandatory = $true)][string]$ExpectedRunRoot,
        [Parameter(Mandatory = $true)]$NewPayload,
        [scriptblock]$BeforeAtomicReplace
    )
    $oldBytes = [System.IO.File]::ReadAllBytes($LockPath)
    $oldHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($oldBytes)).ToLowerInvariant()
    $oldPayload = [System.Text.Encoding]::UTF8.GetString($oldBytes) | ConvertFrom-Json
    if ([string]$oldPayload.run_id -ne $ExpectedRunId -or [string]$oldPayload.run_root -ne $ExpectedRunRoot) {
        throw "The active repair lock no longer matches the recovered transaction."
    }
    Write-Utf8FileDurable -Path $ArchivePath -Content ([System.Text.Encoding]::UTF8.GetString($oldBytes))
    if ((Get-FileSha256Lower -Path $ArchivePath) -ne $oldHash) {
        throw "The recovered repair lock archive could not be verified."
    }
    $temporaryPath = "$LockPath.$([guid]::NewGuid().ToString('N')).writing"
    try {
        Write-Utf8FileDurable -Path $temporaryPath -Content ($NewPayload | ConvertTo-Json -Depth 8)
        if ((Get-FileSha256Lower -Path $LockPath) -ne $oldHash) {
            throw "The active repair lock changed before atomic handoff."
        }
        if ($BeforeAtomicReplace) {
            & $BeforeAtomicReplace
        }
        [System.IO.File]::Move($temporaryPath, $LockPath, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function New-RepairSourceBinding {
    param(
        [Parameter(Mandatory = $true)][string[]]$Paths,
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [Parameter(Mandatory = $true)][string[]]$RepositoryPaths,
        [Parameter(Mandatory = $true)][string]$SnapshotRoot,
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot
    )
    [System.IO.Directory]::CreateDirectory($SnapshotRoot) | Out-Null
    $resolvedWorkspaceRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\')
    $resolvedRepositories = @($RepositoryPaths | ForEach-Object { [System.IO.Path]::GetFullPath($_).TrimEnd('\') })
    $files = foreach ($path in $Paths) {
        $resolvedPath = [System.IO.Path]::GetFullPath($path)
        if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
            throw "Bound repair source is missing: $resolvedPath"
        }
        $owningRepository = @($resolvedRepositories | Where-Object {
            $resolvedPath.StartsWith($_ + '\', [System.StringComparison]::OrdinalIgnoreCase)
        } | Sort-Object Length -Descending | Select-Object -First 1)
        if ($owningRepository.Count -ne 1) {
            throw "Bound repair source has no owning repository: $resolvedPath"
        }
        $relativePath = [System.IO.Path]::GetRelativePath($owningRepository[0], $resolvedPath).Replace('\', '/')
        & git -C $owningRepository[0] ls-files --error-unmatch -- $relativePath | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Bound repair source is not tracked by Git: $resolvedPath"
        }
        $snapshotRelativePath = [System.IO.Path]::GetRelativePath($resolvedWorkspaceRoot, $resolvedPath)
        if ($snapshotRelativePath.StartsWith("..") -or [System.IO.Path]::IsPathRooted($snapshotRelativePath)) {
            throw "Bound repair source is outside the workspace snapshot root: $resolvedPath"
        }
        $snapshotPath = Join-Path $SnapshotRoot $snapshotRelativePath
        [System.IO.Directory]::CreateDirectory((Split-Path -Parent $snapshotPath)) | Out-Null
        [System.IO.File]::Copy($resolvedPath, $snapshotPath, $true)
        $sourceHash = Get-FileSha256Lower -Path $resolvedPath
        if ((Get-FileSha256Lower -Path $snapshotPath) -ne $sourceHash) {
            throw "Repair source snapshot hash mismatch: $resolvedPath"
        }
        (Get-Item -LiteralPath $snapshotPath).IsReadOnly = $true
        [ordered]@{
            path = $resolvedPath
            repository = $owningRepository[0]
            relative_path = $relativePath
            bytes = (Get-Item -LiteralPath $resolvedPath).Length
            sha256 = $sourceHash
            snapshot_path = [System.IO.Path]::GetFullPath($snapshotPath)
            snapshot_relative_path = $snapshotRelativePath.Replace('\', '/')
        }
    }
    $repositories = foreach ($repositoryPath in $RepositoryPaths) {
        $resolvedRepository = [System.IO.Path]::GetFullPath($repositoryPath)
        $head = (& git -C $resolvedRepository rev-parse --verify HEAD | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($head)) {
            throw "Repair source repository has no valid HEAD: $resolvedRepository"
        }
        $status = (& git -C $resolvedRepository status --porcelain --untracked-files=all | Out-String).Trim()
        [ordered]@{
            path = $resolvedRepository
            head = $head
            status_sha256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($status))).ToLowerInvariant()
            status = $status
        }
    }
    $binding = [ordered]@{
        schema_version = 2
        captured_at = (Get-Date -Format o)
        workspace_root = $resolvedWorkspaceRoot
        snapshot_root = [System.IO.Path]::GetFullPath($SnapshotRoot)
        files = @($files)
        repositories = @($repositories)
    }
    $temporaryPath = "$OutputPath.writing"
    $binding | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    [System.IO.File]::Move($temporaryPath, $OutputPath, $true)
    return Get-FileSha256Lower -Path $OutputPath
}

function Assert-RepairSourceBinding {
    param(
        [Parameter(Mandatory = $true)][string]$BindingPath,
        [Parameter(Mandatory = $true)][string]$ExpectedBindingSha256
    )
    if ((Get-FileSha256Lower -Path $BindingPath) -ne $ExpectedBindingSha256.ToLowerInvariant()) {
        throw "Repair source binding manifest changed after arming."
    }
    $binding = Get-Content -LiteralPath $BindingPath -Raw | ConvertFrom-Json
    if ([int]$binding.schema_version -ne 2) {
        throw "Repair source binding schema is unsupported."
    }
    foreach ($file in @($binding.files)) {
        $path = [string]$file.path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Bound repair source disappeared: $path"
        }
        if ((Get-Item -LiteralPath $path).Length -ne [long]$file.bytes -or (Get-FileSha256Lower -Path $path) -ne [string]$file.sha256) {
            throw "Bound repair source changed after arming: $path"
        }
        $snapshotPath = [string]$file.snapshot_path
        if (-not (Test-Path -LiteralPath $snapshotPath -PathType Leaf) -or (Get-FileSha256Lower -Path $snapshotPath) -ne [string]$file.sha256) {
            throw "Bound repair source snapshot changed after arming: $snapshotPath"
        }
        & git -C ([string]$file.repository) ls-files --error-unmatch -- ([string]$file.relative_path) | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Bound repair source is no longer tracked by Git: $path"
        }
    }
    foreach ($repository in @($binding.repositories)) {
        $path = [string]$repository.path
        $head = (& git -C $path rev-parse --verify HEAD | Out-String).Trim()
        $status = (& git -C $path status --porcelain --untracked-files=all | Out-String).Trim()
        $statusSha256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($status))).ToLowerInvariant()
        if ($LASTEXITCODE -ne 0 -or $head -ne [string]$repository.head -or $statusSha256 -ne [string]$repository.status_sha256) {
            throw "Bound repair repository changed after arming: $path"
        }
    }
}

function Assert-RepairSourceSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$BindingPath,
        [Parameter(Mandatory = $true)][string]$ExpectedBindingSha256
    )
    if ((Get-FileSha256Lower -Path $BindingPath) -ne $ExpectedBindingSha256.ToLowerInvariant()) {
        throw "Repair source binding manifest changed during execution."
    }
    $binding = Get-Content -LiteralPath $BindingPath -Raw | ConvertFrom-Json
    if ([int]$binding.schema_version -ne 2) {
        throw "Repair source binding schema is unsupported."
    }
    foreach ($file in @($binding.files)) {
        $snapshotPath = [string]$file.snapshot_path
        if (-not (Test-Path -LiteralPath $snapshotPath -PathType Leaf) -or (Get-FileSha256Lower -Path $snapshotPath) -ne [string]$file.sha256) {
            throw "Repair source snapshot changed during execution: $snapshotPath"
        }
    }
}

function Get-RepairSnapshotPath {
    param(
        [Parameter(Mandatory = $true)][string]$BindingPath,
        [Parameter(Mandatory = $true)][string]$SourcePath
    )
    $binding = Get-Content -LiteralPath $BindingPath -Raw | ConvertFrom-Json
    $resolvedSource = [System.IO.Path]::GetFullPath($SourcePath)
    $match = @($binding.files | Where-Object { [string]$_.path -ieq $resolvedSource })
    if ($match.Count -ne 1) {
        throw "Repair source has no unique snapshot mapping: $resolvedSource"
    }
    return [string]$match[0].snapshot_path
}

try {
    try {
        $ownsMutex = $mutex.WaitOne(0, $false)
    }
    catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) {
        throw "Another Codex full repair runner already owns the global mutex."
    }
    New-Item -ItemType Directory -Path $BackupParent -Force | Out-Null
    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
    if (Test-Path -LiteralPath $lockPath) {
        try {
            $staleLock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
            if ([string]::IsNullOrWhiteSpace([string]$staleLock.run_root)) {
                throw "Existing repair lock has no run_root."
            }
            if ([string]::IsNullOrWhiteSpace([string]$staleLock.run_id)) {
                throw "Existing repair lock has no run_id."
            }
            $staleRunRoot = (Resolve-SafeContainedPath -Path ([string]$staleLock.run_root) -RequiredRoot $BackupParent).TrimEnd('\')
            $staleRunId = [string]$staleLock.run_id
            $staleRepairManifestForGate = Join-Path $staleRunRoot "repair_data\repair_manifest.json"
            if (Test-Path -LiteralPath $staleRepairManifestForGate) {
                $staleManifestForGate = (Get-CommittedRepairManifest -ManifestPath $staleRepairManifestForGate).payload
                if ($staleManifestForGate.status -in @("pending_restart_validation", "pending_live_ui_validation")) {
                    throw "The prior repair is awaiting restart/live validation and must keep its active lock: $staleRepairManifestForGate"
                }
            }
        }
        catch {
            throw "Existing active repair lock could not be safely adopted; it was left unchanged. $($_.Exception.Message)"
        }
    }
    else {
        [ordered]@{
            run_id = $runId
            process_id = $PID
            started_at = (Get-Date -Format o)
            mutex = $mutexName
            run_root = $runRoot
            source_binding = $sourceBindingPath
            source_binding_sha256 = $sourceBindingSha256
        } | ConvertTo-Json | Set-Content -LiteralPath $lockPath -Encoding UTF8
        $activeLockOwnedByCurrentRun = $true
    }
}
catch {
    if ($ownsMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
    throw
}

function Write-RepairLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $line = "$(Get-Date -Format o) $Message"
    $line | Tee-Object -FilePath $logPath -Append
}

function Test-CodexPluginExtensionHostProcess {
    param([Parameter(Mandatory = $true)]$ProcessRecord)

    $name = [string]$ProcessRecord.Name
    $commandLine = [string]$ProcessRecord.CommandLine
    $normalizedCommandLine = $commandLine.ToLowerInvariant().Replace("/", "\")
    $normalizedCodexHome = [System.IO.Path]::GetFullPath($CodexHome).TrimEnd('\').ToLowerInvariant()
    return (
        $name -iin @("cmd.exe", "extension-host.exe") -and
        $normalizedCommandLine.Contains("$normalizedCodexHome\plugins\cache\") -and
        $normalizedCommandLine.Contains("\extension-host\") -and
        $normalizedCommandLine.Contains("extension-host.exe")
    )
}

function Test-CodexCoreProcess {
    param([Parameter(Mandatory = $true)]$ProcessRecord)

    $name = [string]$ProcessRecord.Name
    $commandLine = [string]$ProcessRecord.CommandLine
    $normalizedCommandLine = $commandLine.ToLowerInvariant().Replace("/", "\")
    return (
        $name -ieq "ChatGPT.exe" -or
        $name -ieq "Codex.exe" -or
        $name -ieq "node_repl.exe" -or
        $name -ieq "codex-code-mode-host.exe" -or
        $name -ieq "codex-command-runner.exe" -or
        $name -ieq "codex-home-manager-local-win-x64.exe" -or
        $normalizedCommandLine.Contains("xcodebuildmcp") -or
        $normalizedCommandLine.Contains("mcp\server.mjs") -or
        $normalizedCommandLine.Contains("mcp\server.bundle.mjs") -or
        $normalizedCommandLine.Contains("mcp\server.cjs")
    )
}

function Test-CodexProcess {
    param([Parameter(Mandatory = $true)]$ProcessRecord)

    return (
        (Test-CodexCoreProcess -ProcessRecord $ProcessRecord) -or
        (Test-CodexPluginExtensionHostProcess -ProcessRecord $ProcessRecord)
    )
}

function Get-CodexCoreProcesses {
    return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        Test-CodexCoreProcess -ProcessRecord $_
    })
}

function Get-CodexProcesses {
    return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        Test-CodexProcess -ProcessRecord $_
    })
}

function Restore-StaleRestoreArtifacts {
    param(
        [Parameter(Mandatory = $true)][string]$ArchiveRoot,
        [Parameter(Mandatory = $true)][string]$CodexHomePath
    )

    if (-not (Test-Path -LiteralPath $ArchiveRoot -PathType Container)) {
        return 0
    }
    $resolvedArchiveRoot = [System.IO.Path]::GetFullPath($ArchiveRoot).TrimEnd('\')
    $resolvedCodexHome = [System.IO.Path]::GetFullPath($CodexHomePath).TrimEnd('\')
    $candidates = @(Get-ChildItem -LiteralPath $resolvedArchiveRoot -Force | Where-Object {
        $_.Name -match '^\..+\.[0-9a-fA-F]{32}\.restoring$'
    } | Sort-Object Name)
    $restoredCount = 0
    foreach ($candidate in $candidates) {
        if (@(Get-CodexProcesses).Count -gt 0) {
            throw "Codex started while stale restore artifacts were being recovered."
        }
        $sourcePath = [System.IO.Path]::GetFullPath($candidate.FullName)
        if (-not $sourcePath.StartsWith("$resolvedArchiveRoot\", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Stale restore archive candidate escaped its transaction root: $sourcePath"
        }
        $destinationPath = Join-Path $resolvedCodexHome $candidate.Name
        if (Test-Path -LiteralPath $destinationPath) {
            throw "Stale restore recovery target already exists: $destinationPath"
        }
        Move-Item -LiteralPath $sourcePath -Destination $destinationPath
        if ((Test-Path -LiteralPath $sourcePath) -or -not (Test-Path -LiteralPath $destinationPath)) {
            throw "Stale restore recovery verification failed: $destinationPath"
        }
        $restoredCount += 1
    }
    return $restoredCount
}

function Get-CodexNativeHostBrowserRootProcessIds {
    param(
        [Parameter(Mandatory = $true)][object[]]$ExtensionHosts,
        [Parameter(Mandatory = $true)][object[]]$ProcessSnapshot
    )

    $processById = @{}
    foreach ($processRecord in $ProcessSnapshot) {
        $processById[[int]$processRecord.ProcessId] = $processRecord
    }
    $browserRootIds = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($extensionHost in $ExtensionHosts) {
        $ancestorId = [int]$extensionHost.ParentProcessId
        $visited = [System.Collections.Generic.HashSet[int]]::new()
        $highestBrowserAncestorId = 0
        while ($ancestorId -gt 0 -and $visited.Add($ancestorId) -and $processById.ContainsKey($ancestorId)) {
            $ancestor = $processById[$ancestorId]
            if ([string]$ancestor.Name -iin @("chrome.exe", "msedge.exe")) {
                $highestBrowserAncestorId = $ancestorId
            }
            $ancestorId = [int]$ancestor.ParentProcessId
        }
        if ($highestBrowserAncestorId -gt 0) {
            $null = $browserRootIds.Add($highestBrowserAncestorId)
        }
    }
    Write-Output -NoEnumerate $browserRootIds
}

function Stop-OrphanedCodexPluginExtensionHosts {
    param(
        [System.Collections.Generic.HashSet[int]]$AllowedCoreProcessIds,
        [int]$TimeoutSeconds = 15,
        [int]$QuietPeriodMilliseconds = 1500
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $quietSince = $null
    $stoppedAny = $false
    while ((Get-Date) -lt $deadline) {
        $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        $blockingCoreProcesses = @($snapshot | Where-Object {
            (Test-CodexCoreProcess -ProcessRecord $_) -and
            ($null -eq $AllowedCoreProcessIds -or -not $AllowedCoreProcessIds.Contains([int]$_.ProcessId))
        })
        if ($blockingCoreProcesses.Count -gt 0) {
            return $false
        }

        $extensionHosts = @($snapshot | Where-Object { Test-CodexPluginExtensionHostProcess -ProcessRecord $_ })
        if ($extensionHosts.Count -eq 0) {
            if (-not $stoppedAny) {
                return $false
            }
            if ($null -eq $quietSince) {
                $quietSince = Get-Date
            }
            if (((Get-Date) - $quietSince).TotalMilliseconds -ge $QuietPeriodMilliseconds) {
                if ($stoppedAny) {
                    Write-RepairLog "Orphaned Codex plugin extension-host cleanup completed after a ${QuietPeriodMilliseconds}ms quiet period."
                }
                return $stoppedAny
            }
            Start-Sleep -Milliseconds 200
            continue
        }

        $quietSince = $null
        $stoppedAny = $true
        $extensionHostIds = [System.Collections.Generic.HashSet[int]]::new()
        foreach ($extensionHost in $extensionHosts) {
            $null = $extensionHostIds.Add([int]$extensionHost.ProcessId)
        }
        $rootProcesses = @($extensionHosts | Where-Object {
            -not $extensionHostIds.Contains([int]$_.ParentProcessId)
        })
        $browserRootIds = Get-CodexNativeHostBrowserRootProcessIds -ExtensionHosts $extensionHosts -ProcessSnapshot $snapshot
        $evidence = @($extensionHosts | ForEach-Object {
            [ordered]@{
                process_id = [int]$_.ProcessId
                parent_process_id = [int]$_.ParentProcessId
                name = [string]$_.Name
                command_line = [string]$_.CommandLine
            }
        })
        Write-RepairLog "Codex core is offline; stopping $($extensionHosts.Count) plugin extension-host process(es) and $($browserRootIds.Count) browser root(s) that launched them. Evidence: $($evidence | ConvertTo-Json -Compress -Depth 4)"

        foreach ($browserRootId in $browserRootIds) {
            $null = Stop-ProcessTreeIdempotent -TargetProcessId ([int]$browserRootId)
        }
        foreach ($rootProcess in $rootProcesses) {
            $rootProcessId = [int]$rootProcess.ProcessId
            $stopResult = Stop-ProcessTreeIdempotent -TargetProcessId $rootProcessId
            if ($stopResult -eq "already_exited") {
                $stillRunning = Get-CimInstance Win32_Process -Filter "ProcessId = $rootProcessId" -ErrorAction SilentlyContinue
                if ($null -ne $stillRunning -and (Test-CodexPluginExtensionHostProcess -ProcessRecord $stillRunning)) {
                    Write-RepairLog "Plugin extension-host PID $rootProcessId is still running; retrying within the bounded cleanup window."
                }
                else {
                    Write-RepairLog "Plugin extension-host PID $rootProcessId exited before taskkill completed; treating the idempotent cleanup as successful."
                }
            }
        }
        Start-Sleep -Milliseconds 250
    }

    $remaining = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        Test-CodexPluginExtensionHostProcess -ProcessRecord $_
    })
    $remainingEvidence = @($remaining | ForEach-Object {
        [ordered]@{
            process_id = [int]$_.ProcessId
            parent_process_id = [int]$_.ParentProcessId
            name = [string]$_.Name
            command_line = [string]$_.CommandLine
        }
    })
    throw "Codex plugin extension-host processes did not stay offline for ${QuietPeriodMilliseconds}ms within ${TimeoutSeconds}s. Remaining: $($remainingEvidence | ConvertTo-Json -Compress -Depth 4)"
}

function Get-ProcessTreeIds {
    param(
        [Parameter(Mandatory = $true)][int]$RootProcessId,
        [Parameter(Mandatory = $true)][object[]]$ProcessSnapshot
    )

    $ids = [System.Collections.Generic.HashSet[int]]::new()
    $null = $ids.Add($RootProcessId)
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($item in $ProcessSnapshot) {
            if ($ids.Contains([int]$item.ParentProcessId) -and $ids.Add([int]$item.ProcessId)) {
                $changed = $true
            }
        }
    }
    Write-Output -NoEnumerate $ids
}

function New-RepairStageStartInfo {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $ArgumentList) {
        $startInfo.ArgumentList.Add([string]$argument)
    }
    return $startInfo
}

function Invoke-RepairStage {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][datetime]$GlobalDeadline,
        [bool]$MonitorDesktop = $true
    )

    $remainingSeconds = [int][Math]::Floor(($GlobalDeadline - (Get-Date)).TotalSeconds)
    if ($remainingSeconds -le 0) {
        throw "Global repair deadline reached before stage '$Name'."
    }
    $stageSeconds = [Math]::Min($remainingSeconds, $StageTimeoutMinutes * 60)
    $stageOut = Join-Path $runRoot "$Name.stdout.log"
    $stageErr = Join-Path $runRoot "$Name.stderr.log"
    if ($MonitorDesktop) {
        $null = Stop-OrphanedCodexPluginExtensionHosts
        if (@(Get-CodexCoreProcesses).Count -gt 0) {
            throw "A Codex core process is running before stage '$Name'; no stage was started."
        }
    }
    if ($sourceBindingSha256) {
        Assert-RepairSourceBinding -BindingPath $sourceBindingPath -ExpectedBindingSha256 $sourceBindingSha256
    }
    Write-RepairLog "Starting stage '$Name' with timeout ${stageSeconds}s."
    $startInfo = New-RepairStageStartInfo -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $sourceSnapshotRoot
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $stageOutStream = $null
    $stageErrStream = $null
    $stageOutCopy = $null
    $stageErrCopy = $null
    try {
        $stageOutStream = [System.IO.FileStream]::new(
            $stageOut,
            [System.IO.FileMode]::Create,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::Read
        )
        $stageErrStream = [System.IO.FileStream]::new(
            $stageErr,
            [System.IO.FileMode]::Create,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::Read
        )
        if (-not $process.Start()) {
            throw "Stage '$Name' could not start."
        }
        $stageOutCopy = $process.StandardOutput.BaseStream.CopyToAsync($stageOutStream)
        $stageErrCopy = $process.StandardError.BaseStream.CopyToAsync($stageErrStream)
        $deadline = (Get-Date).AddSeconds($stageSeconds)
        $nextSnapshotCheck = Get-Date
        while (-not $process.HasExited -and (Get-Date) -lt $deadline) {
            if ((Get-Date) -ge $nextSnapshotCheck) {
                Assert-RepairSourceSnapshot -BindingPath $sourceBindingPath -ExpectedBindingSha256 $sourceBindingSha256
                $nextSnapshotCheck = (Get-Date).AddSeconds(1)
            }
            if ($MonitorDesktop) {
                $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop)
                $allowedProcessIds = Get-ProcessTreeIds -RootProcessId $process.Id -ProcessSnapshot $snapshot
                $unexpectedCoreProcesses = @($snapshot | Where-Object {
                    (Test-CodexCoreProcess -ProcessRecord $_) -and
                    -not $allowedProcessIds.Contains([int]$_.ProcessId)
                })
                if ($unexpectedCoreProcesses.Count -gt 0) {
                    $null = Stop-ProcessTreeIdempotent -TargetProcessId $process.Id
                    $process.WaitForExit()
                    throw "An external Codex core process started during stage '$Name'; the stage was terminated."
                }
                $null = Stop-OrphanedCodexPluginExtensionHosts -AllowedCoreProcessIds $allowedProcessIds
            }
            Start-Sleep -Milliseconds 100
            $process.Refresh()
        }
        if (-not $process.HasExited) {
            $null = Stop-ProcessTreeIdempotent -TargetProcessId $process.Id
            $process.WaitForExit()
            throw "Stage '$Name' exceeded its timeout of ${stageSeconds}s."
        }
        $process.WaitForExit()
        [System.Threading.Tasks.Task]::WaitAll(@($stageOutCopy, $stageErrCopy))
        $stageOutStream.Flush($true)
        $stageErrStream.Flush($true)
        $stageExitCode = $process.ExitCode
    }
    finally {
        if ($null -ne $stageOutCopy -and -not $stageOutCopy.IsCompleted) {
            $stageOutCopy.GetAwaiter().GetResult()
        }
        if ($null -ne $stageErrCopy -and -not $stageErrCopy.IsCompleted) {
            $stageErrCopy.GetAwaiter().GetResult()
        }
        if ($null -ne $stageOutStream) {
            $stageOutStream.Dispose()
        }
        if ($null -ne $stageErrStream) {
            $stageErrStream.Dispose()
        }
        $process.Dispose()
    }
    if (Test-Path -LiteralPath $stageOut) {
        Get-Content -LiteralPath $stageOut | Tee-Object -FilePath $logPath -Append | Out-Host
    }
    if (Test-Path -LiteralPath $stageErr) {
        Get-Content -LiteralPath $stageErr | Tee-Object -FilePath $logPath -Append | Out-Host
    }
    if ($stageExitCode -ne 0) {
        throw "Stage '$Name' failed with exit code $stageExitCode."
    }
    if ($MonitorDesktop) {
        $null = Stop-OrphanedCodexPluginExtensionHosts
        if (@(Get-CodexCoreProcesses).Count -gt 0) {
            throw "Stage '$Name' left a Codex core process running; the repair stopped before the next stage."
        }
    }
    if ($sourceBindingSha256) {
        Assert-RepairSourceBinding -BindingPath $sourceBindingPath -ExpectedBindingSha256 $sourceBindingSha256
    }
    Write-RepairLog "Stage '$Name' completed successfully."
}

function Set-RepairManifestPendingValidation {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)

    $null = Stop-OrphanedCodexPluginExtensionHosts
    if (@(Get-CodexCoreProcesses).Count -gt 0) {
        throw "Codex restarted before the repair manifest could enter restart validation."
    }
    Assert-RepairSourceBinding -BindingPath $sourceBindingPath -ExpectedBindingSha256 $sourceBindingSha256
    $currentHash = [string](Get-CommittedRepairManifest -ManifestPath $ManifestPath).sha256
    $result = & python $manifestChainScript `
        --manifest $ManifestPath `
        --expected-sha256 $currentHash `
        --expected-run-id $runId `
        --expected-run-root $runRoot `
        --expected-status "applied_pending_runner_diagnostics" `
        --new-status "pending_restart_validation" `
        --set "pending_restart_validation_at=$(Get-Date -Format o)" `
        --set "live_validation_challenge=$liveValidationChallenge"
    if ($LASTEXITCODE -ne 0) {
        throw "Repair manifest chain update failed with exit code $LASTEXITCODE"
    }
    $null = Stop-OrphanedCodexPluginExtensionHosts
    if (@(Get-CodexCoreProcesses).Count -gt 0) {
        throw "Codex restarted before the pending-validation manifest swap."
    }
    Assert-RepairSourceBinding -BindingPath $sourceBindingPath -ExpectedBindingSha256 $sourceBindingSha256
    return ($result | ConvertFrom-Json).sha256
}

function Wait-CodexOfflineForRollback {
    param([int]$Seconds = 20)

    $rollbackWaitDeadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $rollbackWaitDeadline) {
        if (@(Get-CodexCoreProcesses).Count -eq 0) {
            $null = Stop-OrphanedCodexPluginExtensionHosts
            return $true
        }
        Start-Sleep -Seconds 1
    }
    if (@(Get-CodexCoreProcesses).Count -eq 0) {
        $null = Stop-OrphanedCodexPluginExtensionHosts
        return $true
    }
    return $false
}

function Get-RollbackOfflineState {
    param(
        [scriptblock]$WaitProbe = { Wait-CodexOfflineForRollback },
        [scriptblock]$ProcessProbe = { @(Get-CodexCoreProcesses) }
    )

    try {
        $offlineConfirmed = [bool](& $WaitProbe)
        $runningProcesses = @(& $ProcessProbe)
        return [pscustomobject]@{
            probe_succeeded = $true
            offline_confirmed = ($offlineConfirmed -and $runningProcesses.Count -eq 0)
            running_processes = @($runningProcesses)
            error = $null
        }
    }
    catch {
        return [pscustomobject]@{
            probe_succeeded = $false
            offline_confirmed = $false
            running_processes = @()
            error = $_.Exception.Message
        }
    }
}

$scriptExitCode = 1
$executionDeadline = $null
try {
    foreach ($requiredPath in $requiredSourcePaths) {
        if (-not (Test-Path -LiteralPath $requiredPath)) {
            throw "Required repair component is missing: $requiredPath"
        }
    }
    $sourceBindingSha256 = New-RepairSourceBinding `
        -Paths $requiredSourcePaths `
        -OutputPath $sourceBindingPath `
        -RepositoryPaths $sourceRepositories `
        -SnapshotRoot $sourceSnapshotRoot `
        -WorkspaceRoot $workspaceRoot
    $auditScript = Get-RepairSnapshotPath -BindingPath $sourceBindingPath -SourcePath $auditScript
    $applyScript = Get-RepairSnapshotPath -BindingPath $sourceBindingPath -SourcePath $applyScript
    $pluginRepairScript = Get-RepairSnapshotPath -BindingPath $sourceBindingPath -SourcePath $pluginRepairScript
    $pluginSnapshotScript = Get-RepairSnapshotPath -BindingPath $sourceBindingPath -SourcePath $pluginSnapshotScript
    $manifestChainScript = Get-RepairSnapshotPath -BindingPath $sourceBindingPath -SourcePath $manifestChainScript
    $diagnosticsScript = Get-RepairSnapshotPath -BindingPath $sourceBindingPath -SourcePath $diagnosticsScript
    if ($activeLockOwnedByCurrentRun) {
        $currentRunLock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
        $currentRunLock | Add-Member -NotePropertyName source_binding -NotePropertyValue $sourceBindingPath -Force
        $currentRunLock | Add-Member -NotePropertyName source_binding_sha256 -NotePropertyValue $sourceBindingSha256 -Force
        $currentRunLock | Add-Member -NotePropertyName source_snapshot_root -NotePropertyValue $sourceSnapshotRoot -Force
        $currentRunLock | ConvertTo-Json | Set-Content -LiteralPath $lockPath -Encoding UTF8
    }

    Write-RepairLog "One-shot repair armed. Waiting for Codex Desktop, app-server, local connector, and plugin hosts to exit."
    $waitDeadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $quietSince = $null
    $lastBlockingSignature = $null
    $lastBlockingLogAt = [datetime]::MinValue
    while ((Get-Date) -lt $waitDeadline) {
        if (@(Get-CodexCoreProcesses).Count -eq 0) {
            $null = Stop-OrphanedCodexPluginExtensionHosts
        }
        $processes = @(Get-CodexProcesses)
        if ($processes.Count -eq 0) {
            if (-not $quietSince) {
                $quietSince = Get-Date
                Write-RepairLog "No Codex process remains. Starting the quiet-period confirmation."
            }
            if (((Get-Date) - $quietSince).TotalSeconds -ge $QuietSeconds) {
                break
            }
        }
        else {
            $quietSince = $null
            $blockingSignature = (@($processes | Sort-Object ProcessId | ForEach-Object {
                "$($_.Name):$($_.ProcessId):$($_.ParentProcessId)"
            }) -join ",")
            if (
                $blockingSignature -ne $lastBlockingSignature -or
                ((Get-Date) - $lastBlockingLogAt).TotalSeconds -ge 15
            ) {
                Write-RepairLog "Still waiting for Codex process(es): $blockingSignature"
                $lastBlockingSignature = $blockingSignature
                $lastBlockingLogAt = Get-Date
            }
        }
        Start-Sleep -Seconds 3
    }

    if ((Get-Date) -ge $waitDeadline) {
        throw "Timed out waiting for a complete Codex exit. Remaining: $lastBlockingSignature. No Codex files were changed."
    }

    Assert-RepairSourceBinding -BindingPath $sourceBindingPath -ExpectedBindingSha256 $sourceBindingSha256

    $executionDeadline = (Get-Date).AddMinutes($ExecutionTimeoutMinutes)
    if ($RecoverOnly -and -not $staleRunRoot) {
        throw "RecoverOnly requires an existing safely adopted repair transaction lock."
    }
    if ($staleRunRoot) {
        Write-RepairLog "Found an abandoned prior runner lock. Recovering the prior transaction before new work: $staleRunRoot"
        $staleRepairManifest = Join-Path $staleRunRoot "repair_data\repair_manifest.json"
        $stalePluginSnapshotManifest = Join-Path $staleRunRoot "plugin_state_snapshot\plugin_state_snapshot.json"
        $stalePluginsRecoveredByRepairManifest = $false
        if (Test-Path -LiteralPath $staleRepairManifest) {
            $staleManifestInspection = Get-CommittedRepairManifest -ManifestPath $staleRepairManifest
            $staleManifest = $staleManifestInspection.payload
            if ($staleManifest.status -in @("pending_restart_validation", "pending_live_ui_validation")) {
                throw "The prior repair is awaiting restart/live validation and must not be overwritten: $staleRepairManifest"
            }
            if (
                $staleManifest.status -in @("complete", "validation_superseded") -or
                (
                    $staleManifest.status -eq "rolled_back" -and
                    -not [string]::IsNullOrWhiteSpace([string]$staleManifest.plugin_snapshot_manifest)
                )
            ) {
                $stalePluginsRecoveredByRepairManifest = $true
            }
            if ($staleManifest.status -notin @("rolled_back", "complete", "validation_superseded")) {
                $staleManifestSha256 = [string]$staleManifestInspection.sha256
                Invoke-RepairStage -Name "00_recover_abandoned_threads" -FilePath "python" -ArgumentList @(
                    $applyScript, "--rollback-manifest", $staleRepairManifest,
                    "--expected-manifest-sha256", $staleManifestSha256,
                    "--expected-run-id", $staleRunId,
                    "--expected-run-root", $staleRunRoot,
                    "--expected-codex-home", $CodexHome,
                    "--preserve-runtime-state"
                ) -GlobalDeadline $executionDeadline
                $stalePluginsRecoveredByRepairManifest = -not [string]::IsNullOrWhiteSpace([string]$staleManifest.plugin_snapshot_manifest)
            }
        }
        if ((Test-Path -LiteralPath $stalePluginSnapshotManifest) -and -not $stalePluginsRecoveredByRepairManifest) {
            $stalePluginSnapshotSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $stalePluginSnapshotManifest).Hash.ToLowerInvariant()
            Invoke-RepairStage -Name "00_recover_abandoned_plugins" -FilePath "python" -ArgumentList @(
                $pluginSnapshotScript, "restore", "--manifest", $stalePluginSnapshotManifest,
                "--expected-run-id", $staleRunId,
                "--expected-run-root", $staleRunRoot,
                "--expected-codex-home", $CodexHome,
                "--expected-manifest-sha256", $stalePluginSnapshotSha256
            ) -GlobalDeadline $executionDeadline
        }
        $staleRestoreArchiveForRecovery = Join-Path $staleRunRoot "stale_restore_artifacts"
        if (Test-Path -LiteralPath $staleRestoreArchiveForRecovery -PathType Container) {
            $restoredStaleArtifactCount = Restore-StaleRestoreArtifacts `
                -ArchiveRoot $staleRestoreArchiveForRecovery `
                -CodexHomePath $CodexHome
            Write-RepairLog "Recovered $restoredStaleArtifactCount stale .restoring artifact(s) from the abandoned transaction."
        }
        [ordered]@{
            status = "recovered"
            recovered_at = (Get-Date -Format o)
            previous_run_root = $staleRunRoot
        } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot "ABANDONED_RUN_RECOVERED.json") -Encoding UTF8
        Write-RepairLog "Abandoned prior transaction was recovered successfully."
        $currentStaleLock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
        if ([string]$currentStaleLock.run_id -ne $staleRunId -or [string]$currentStaleLock.run_root -ne $staleRunRoot) {
            throw "The abandoned transaction lock changed during recovery; the new run was not activated."
        }
        $archivedStaleLock = Join-Path $staleRunRoot "recovered_runner_lock.json"
        if (Test-Path -LiteralPath $archivedStaleLock) {
            $archivedStaleLock = Join-Path $staleRunRoot "recovered_runner_lock_$runId.json"
        }
        $newActiveLock = [ordered]@{
            run_id = $runId
            process_id = $PID
            started_at = (Get-Date -Format o)
            mutex = $mutexName
            run_root = $runRoot
            source_binding = $sourceBindingPath
            source_binding_sha256 = $sourceBindingSha256
            source_snapshot_root = $sourceSnapshotRoot
        }
        Switch-ActiveRepairLock -LockPath $lockPath -ArchivePath $archivedStaleLock -ExpectedRunId $staleRunId -ExpectedRunRoot $staleRunRoot -NewPayload $newActiveLock
        $activeLockOwnedByCurrentRun = $true
    }
    if ($RecoverOnly) {
        [ordered]@{
            status = "recovered_only"
            recovered_at = (Get-Date -Format o)
            recovered_run_root = $staleRunRoot
            recovery_run_root = $runRoot
        } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot "RECOVERY_ONLY_COMPLETE.json") -Encoding UTF8
        Write-RepairLog "Recovery-only mode completed. No new repair stages were started."
        $scriptExitCode = 0
        return
    }
    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $windowsPowerShell)) {
        throw "Windows PowerShell 5.1 is required to resolve the current Codex AppX package."
    }
    $appxQuery = "`$package = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop | Where-Object InstallLocation | Sort-Object Version -Descending | Select-Object -First 1; if (`$null -eq `$package) { throw 'OpenAI.Codex AppX package was not found' }; `$package.InstallLocation"
    $appxInstallLocation = (& $windowsPowerShell -NoProfile -NonInteractive -Command $appxQuery | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($appxInstallLocation)) {
        throw "Could not resolve the current Codex AppX installation."
    }
    $bundledMarketplaceSource = Join-Path $appxInstallLocation "app\resources\plugins\openai-bundled"
    if (-not (Test-Path -LiteralPath (Join-Path $bundledMarketplaceSource ".agents\plugins\marketplace.json"))) {
        throw "Current Codex AppX bundled marketplace is incomplete: $bundledMarketplaceSource"
    }
    Write-RepairLog "Codex stayed offline for $QuietSeconds seconds. Capturing an exact plugin/config/state snapshot before mutation."
    Invoke-RepairStage -Name "00_plugin_snapshot" -FilePath "python" -ArgumentList @(
        $pluginSnapshotScript, "snapshot", "--codex-home", $CodexHome, "--snapshot-root", $pluginSnapshotRoot,
        "--repair-source", $bundledMarketplaceSource,
        "--run-id", $runId, "--run-root", $runRoot
    ) -GlobalDeadline $executionDeadline
    $pluginSnapshotCompleted = $true
    $pluginSnapshotSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $pluginSnapshotManifest).Hash.ToLowerInvariant()

    Write-RepairLog "Archiving abandoned .restoring artifacts into the current D:\Backup repair run."
    $staleRestoreArchiveStarted = $true
    Invoke-RepairStage -Name "00_archive_stale_restores" -FilePath "python" -ArgumentList @(
        $pluginSnapshotScript, "archive-stale-restores", "--codex-home", $CodexHome,
        "--archive-root", $staleRestoreArchiveRoot
    ) -GlobalDeadline $executionDeadline
    $staleRestoreArchiveCompleted = $true

    Invoke-RepairStage -Name "00_verify_appx_source_before_repair" -FilePath "python" -ArgumentList @(
        $pluginSnapshotScript, "verify-sources", "--manifest", $pluginSnapshotManifest,
        "--expected-manifest-sha256", $pluginSnapshotSha256
    ) -GlobalDeadline $executionDeadline

    Write-RepairLog "Repairing bundled plugin cache and runtime links from the current AppX source."
    $powerShellPath = (Get-Process -Id $PID).Path
    Invoke-RepairStage -Name "01_plugins" -FilePath $powerShellPath -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $pluginRepairScript,
        "-CodexHome", $CodexHome, "-BackupRoot", (Join-Path $runRoot "plugin_backups")
    ) -GlobalDeadline $executionDeadline
    Invoke-RepairStage -Name "01_verify_appx_source_after_repair" -FilePath "python" -ArgumentList @(
        $pluginSnapshotScript, "verify-sources", "--manifest", $pluginSnapshotManifest,
        "--expected-manifest-sha256", $pluginSnapshotSha256
    ) -GlobalDeadline $executionDeadline

    Write-RepairLog "Rebuilding both Chrome native-host runtime files from the current AppX and Desktop runtime."
    $localCodexRuntimeRoot = Join-Path $env:LOCALAPPDATA "OpenAI\Codex"
    Invoke-RepairStage -Name "01_chrome_native_hosts" -FilePath "python" -ArgumentList @(
        $nativeHostRepairScript,
        "--codex-home", $CodexHome,
        "--appx-resources", (Join-Path $appxInstallLocation "app\resources"),
        "--target-root", $localCodexRuntimeRoot,
        "--backup-root", (Join-Path $runRoot "chrome_native_host_backups"),
        "--report", $nativeHostRepairReportPath
    ) -GlobalDeadline $executionDeadline

    Write-RepairLog "Running a fresh complete rollout audit after plugin repair."
    Invoke-RepairStage -Name "02_audit" -FilePath "python" -ArgumentList @(
        $auditScript, "--state", (Join-Path $CodexHome "state_5.sqlite"), "--report", $auditPath
    ) -GlobalDeadline $executionDeadline
    $auditSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $auditPath).Hash.ToLowerInvariant()

    $applyArguments = @(
        $applyScript, "--audit", $auditPath, "--backup-root", $repairDataRoot,
        "--codex-home", $CodexHome, "--include-archived",
        "--expected-audit-sha256", $auditSha256,
        "--plugin-snapshot-manifest", $pluginSnapshotManifest,
        "--expected-plugin-snapshot-sha256", $pluginSnapshotSha256,
        "--runner-run-id", $runId,
        "--run-root", $runRoot
    )
    foreach ($targetThreadId in @($SlimThreadId)) {
        if ([string]::IsNullOrWhiteSpace($targetThreadId)) {
            throw "SlimThreadId cannot contain an empty value."
        }
        $applyArguments += @("--slim-thread-id", $targetThreadId.Trim())
    }
    if (@($SlimThreadId).Count -gt 0) {
        Write-RepairLog "Applying compatibility repairs and explicit prompt-preserving checkpoint slimming for: $(@($SlimThreadId) -join ', ')."
    }
    else {
        Write-RepairLog "Applying prompt-preserving thread compatibility repairs, state backups, and log archival. Performance findings are reported without checkpoint history reduction."
    }
    Invoke-RepairStage -Name "03_apply" -FilePath "python" -ArgumentList $applyArguments -GlobalDeadline $executionDeadline
    $applyCompleted = $true

    Write-RepairLog "Running the complete post-repair Codex Home diagnostics snapshot."
    Invoke-RepairStage -Name "04_diagnostics" -FilePath "python" -ArgumentList @(
        $diagnosticsScript, "--codex-home", $CodexHome, "--report", $postDiagnosticsPath,
        "--language", "zh", "--sidebar-limit", "1000", "--comprehensive-event-stream",
        "--require-pass", "config.toml_parse",
        "--require-pass", "sqlite.state",
        "--require-pass", "threads.rollout_jsonl_integrity",
        "--require-pass", "threads.main_title_stream",
        "--require-pass", "threads.main_event_stream",
        "--require-pass", "plugins.official_thread_tools_exposure",
        "--require-pass", "plugins.macos_plugin_disabled_on_windows",
        "--require-pass", "plugins.browser",
        "--require-pass", "plugins.sites",
        "--require-pass", "plugins.chrome",
        "--require-pass", "plugins.computer-use",
        "--require-pass", "plugins.latex",
        "--require-pass", "plugins.marketplace",
        "--require-pass", "plugins.bundled_marketplace_source",
        "--require-pass", "plugins.stale_restore_artifacts",
        "--require-pass", "plugins.chrome_native_hosts",
        "--require-pass", "plugins.chrome_native_messaging_manifests",
        "--require-pass", "plugins.skill_manifests",
        "--require-pass", "plugins.advertised_skill_paths",
        "--require-pass", "plugins.curated_marketplace_manifests",
        "--require-pass", "plugins.node_repl_config_layer_consistency",
        "--require-pass", "runtime.legacy_notify_hook"
    ) -GlobalDeadline $executionDeadline

    $repairManifestSha256 = Set-RepairManifestPendingValidation -ManifestPath $repairManifestPath

    $result = [ordered]@{
        status = "pending_restart_validation"
        completed_at = (Get-Date -Format o)
        run_id = $runId
        run_root = $runRoot
        audit = $auditPath
        manifest = $repairManifestPath
        plugin_snapshot_manifest = $pluginSnapshotManifest
        post_diagnostics = $postDiagnosticsPath
        log = $logPath
        runner = "one-shot"
    }
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $pendingValidationPath -Encoding UTF8
    $preserveActiveLock = $true
    [ordered]@{
        run_id = $runId
        process_id = $PID
        status = "pending_restart_validation"
        updated_at = (Get-Date -Format o)
        mutex = $mutexName
        run_root = $runRoot
        manifest = $repairManifestPath
        pending_validation = $pendingValidationPath
        repair_manifest_sha256 = $repairManifestSha256
        source_binding = $sourceBindingPath
        source_binding_sha256 = $sourceBindingSha256
        source_snapshot_root = $sourceSnapshotRoot
        python_executable = (Get-Command python -ErrorAction Stop).Source
        codex_home = $CodexHome
        live_validation_challenge = $liveValidationChallenge
    } | ConvertTo-Json | Set-Content -LiteralPath $lockPath -Encoding UTF8
    Write-RepairLog "Offline repair passed all gates and is pending restart/live validation. The runner is exiting."
    $scriptExitCode = 0
}
catch {
    $primaryError = $_.Exception.Message
    $preserveActiveLock = $true
    $rollbackErrors = [System.Collections.Generic.List[string]]::new()
    $rollbackStatus = "rollback_probe_pending"
    $initialFailure = [ordered]@{
        status = "failed"
        failed_at = (Get-Date -Format o)
        run_id = $runId
        error = $primaryError
        rollback_status = $rollbackStatus
        rollback_errors = @()
        run_root = $runRoot
    }
    $initialPersistenceErrors = @(Save-RepairFailureArtifacts `
        -FailurePayload $initialFailure `
        -FailureManifestPath $failurePath `
        -ActiveLockPath $lockPath `
        -OwnsActiveLock $activeLockOwnedByCurrentRun `
        -FailureLockStatus "failed_pending_offline_assessment" `
        -CurrentRunId $runId `
        -CurrentRunRoot $runRoot `
        -CurrentMutexName $mutexName `
        -CurrentSourceBinding $sourceBindingPath `
        -CurrentSourceBindingSha256 $sourceBindingSha256)
    foreach ($persistenceError in $initialPersistenceErrors) {
        $rollbackErrors.Add([string]$persistenceError)
    }

    $offlineState = Get-RollbackOfflineState
    if (-not $offlineState.probe_succeeded) {
        $rollbackStatus = "offline_probe_failed"
        $rollbackErrors.Add("offline rollback probe: $($offlineState.error)")
    }
    elseif (-not $offlineState.offline_confirmed) {
        $rollbackStatus = "pending_offline_rollback"
        $runningDescription = @($offlineState.running_processes | ForEach-Object {
            "$($_.Name):$($_.ProcessId)"
        }) -join ", "
        $rollbackErrors.Add("Codex offline state was not confirmed; no rollback was attempted. Remaining: $runningDescription")
    }

    $repairManifestExists = $false
    $repairManifestProbeSucceeded = $true
    try {
        $repairManifestExists = Test-Path -LiteralPath $repairManifestPath
    }
    catch {
        $repairManifestProbeSucceeded = $false
        $rollbackErrors.Add("repair manifest probe: $($_.Exception.Message)")
    }

    if ($offlineState.probe_succeeded -and $offlineState.offline_confirmed -and $repairManifestProbeSucceeded) {
        if ($applyCompleted -or $repairManifestExists) {
            try {
                $rollbackDeadline = (Get-Date).AddMinutes($StageTimeoutMinutes)
                $repairManifestSha256 = [string](Get-CommittedRepairManifest -ManifestPath $repairManifestPath).sha256
                Invoke-RepairStage -Name "90_rollback_threads" -FilePath "python" -ArgumentList @(
                    $applyScript, "--rollback-manifest", $repairManifestPath,
                    "--expected-manifest-sha256", $repairManifestSha256,
                    "--expected-run-id", $runId,
                    "--expected-run-root", $runRoot,
                    "--expected-codex-home", $CodexHome
                ) -GlobalDeadline $rollbackDeadline
                $rollbackStatus = "integrated_rollback_complete"
            }
            catch {
                $rollbackErrors.Add("integrated rollback: $($_.Exception.Message)")
            }
        }
        elseif ($pluginSnapshotCompleted) {
            try {
                $rollbackDeadline = (Get-Date).AddMinutes($StageTimeoutMinutes)
                $pluginSnapshotSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $pluginSnapshotManifest).Hash.ToLowerInvariant()
                Invoke-RepairStage -Name "90_rollback_snapshot_only" -FilePath "python" -ArgumentList @(
                    $pluginSnapshotScript, "restore", "--manifest", $pluginSnapshotManifest,
                    "--expected-run-id", $runId,
                    "--expected-run-root", $runRoot,
                    "--expected-codex-home", $CodexHome,
                    "--expected-manifest-sha256", $pluginSnapshotSha256
                ) -GlobalDeadline $rollbackDeadline
                $rollbackStatus = "plugin_rollback_complete"
            }
            catch {
                $rollbackErrors.Add("plugin rollback: $($_.Exception.Message)")
            }
        }

        $staleRestoreArchiveNeedsRecovery = $staleRestoreArchiveStarted -or $staleRestoreArchiveCompleted
        try {
            $staleRestoreArchiveNeedsRecovery = $staleRestoreArchiveNeedsRecovery -or (
                Test-Path -LiteralPath $staleRestoreArchiveRoot -PathType Container
            )
        }
        catch {
            $rollbackErrors.Add("stale restore archive probe: $($_.Exception.Message)")
        }
        if ($staleRestoreArchiveNeedsRecovery) {
            try {
                $restoredStaleArtifactCount = Restore-StaleRestoreArtifacts `
                    -ArchiveRoot $staleRestoreArchiveRoot `
                    -CodexHomePath $CodexHome
                Write-RepairLog "Recovered $restoredStaleArtifactCount stale .restoring artifact(s) during rollback."
            }
            catch {
                $rollbackErrors.Add("stale restore artifact rollback: $($_.Exception.Message)")
            }
        }
        if ($rollbackErrors.Count -eq 0) {
            $rollbackStatus = if (
                $applyCompleted -or $pluginSnapshotCompleted -or $staleRestoreArchiveNeedsRecovery -or $repairManifestExists
            ) { "all_mutations_rolled_back" } else { "no_mutations_required" }
        }
        else {
            $rollbackStatus = "pending_offline_rollback"
        }
    }
    elseif ($rollbackStatus -eq "rollback_probe_pending") {
        $rollbackStatus = "pending_offline_rollback"
    }

    $failure = [ordered]@{
        status = "failed"
        failed_at = [string]$initialFailure.failed_at
        finalized_at = (Get-Date -Format o)
        run_id = $runId
        error = $primaryError
        rollback_status = $rollbackStatus
        rollback_errors = @($rollbackErrors)
        run_root = $runRoot
    }
    $failureLockStatus = if ($rollbackStatus -in @("all_mutations_rolled_back", "no_mutations_required")) {
        "failed_rolled_back"
    }
    else {
        "pending_offline_rollback"
    }
    $finalPersistenceErrors = @(Save-RepairFailureArtifacts `
        -FailurePayload $failure `
        -FailureManifestPath $failurePath `
        -ActiveLockPath $lockPath `
        -OwnsActiveLock $activeLockOwnedByCurrentRun `
        -FailureLockStatus $failureLockStatus `
        -CurrentRunId $runId `
        -CurrentRunRoot $runRoot `
        -CurrentMutexName $mutexName `
        -CurrentSourceBinding $sourceBindingPath `
        -CurrentSourceBindingSha256 $sourceBindingSha256)
    foreach ($persistenceError in $finalPersistenceErrors) {
        $rollbackErrors.Add([string]$persistenceError)
    }
    try {
        Write-RepairLog "FAILED: $primaryError; rollback_status=$rollbackStatus; rollback_errors=$($rollbackErrors -join ' | ')"
    }
    catch {
        # The durable failure manifest and active lock remain the diagnostic source if logging also fails.
    }
    $scriptExitCode = 1
}
finally {
    if ($activeLockOwnedByCurrentRun -and -not $preserveActiveLock -and (Test-Path -LiteralPath $lockPath)) {
        $lockForRelease = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
        if ([string]$lockForRelease.run_id -ne $runId) {
            throw "Active repair lock ownership changed; refusing to release another run's lock."
        }
        $releasedLockPath = Join-Path $runRoot "runner_lock_released.json"
        Move-Item -LiteralPath $lockPath -Destination $releasedLockPath -Force
    }
    if ($ownsMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}

exit $scriptExitCode
