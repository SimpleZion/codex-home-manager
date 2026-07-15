[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
    [Parameter(Mandatory = $true)][string]$CycleRoot,
    [Parameter(Mandatory = $true)][string]$PendingManifest,
    [Parameter(Mandatory = $true)][string]$SupersedeReport,
    [string]$CodexHome = "D:\.codex",
    [string]$BackupParent = "D:\Backup\codex_full_repair",
    [string]$PythonExecutable = "D:\Software\anaconda3\python.exe",
    [string]$PowerShellExecutable = "D:\Software\PowerShell\7\pwsh.exe",
    [string]$CodexAppUserModelId = "OpenAI.Codex_2p2nqsd0c76g0!App",
    [string]$ShutdownAuthorizationPath = "",
    [string[]]$SlimThreadId = @(),
    [switch]$SkipPriorValidationSupersession,
    [switch]$PreflightOnly,
    [switch]$AllowOfflineFallback
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-ContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RequiredRoot
    )

    $resolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $resolvedRoot = [System.IO.Path]::GetFullPath($RequiredRoot).TrimEnd('\')
    $isRoot = $resolvedPath.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)
    $isChild = $resolvedPath.StartsWith(
        "$resolvedRoot\",
        [System.StringComparison]::OrdinalIgnoreCase
    )
    if (-not ($isRoot -or $isChild)) {
        throw "Path is outside the required root: $resolvedPath"
    }
    return $resolvedPath
}

$workspacePath = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\')
$backupPath = [System.IO.Path]::GetFullPath($BackupParent).TrimEnd('\')
$cyclePath = Resolve-ContainedPath -Path $CycleRoot -RequiredRoot $backupPath
$manifestPath = Resolve-ContainedPath -Path $PendingManifest -RequiredRoot $backupPath
$reportPath = Resolve-ContainedPath -Path $SupersedeReport -RequiredRoot $backupPath
$codexHomePath = [System.IO.Path]::GetFullPath($CodexHome).TrimEnd('\')

if (-not (Test-Path -LiteralPath $workspacePath -PathType Container)) {
    throw "Workspace root does not exist: $workspacePath"
}
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Pending manifest does not exist: $manifestPath"
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python executable does not exist: $PythonExecutable"
}
if (-not (Test-Path -LiteralPath $PowerShellExecutable -PathType Leaf)) {
    throw "PowerShell executable does not exist: $PowerShellExecutable"
}

New-Item -ItemType Directory -Path $cyclePath -Force | Out-Null
$statusPath = Join-Path $cyclePath "repair_cycle_status.json"
$logPath = Join-Path $cyclePath "repair_cycle.log"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Write-CycleLog {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Message)

    $line = "{0} {1}" -f (Get-Date -Format o), $Message
    [System.IO.File]::AppendAllText($logPath, "$line`r`n", $utf8NoBom)
}

function Write-CycleStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][string]$Detail,
        [hashtable]$Data = @{}
    )

    $payload = [ordered]@{
        status = $Status
        detail = $Detail
        updated_at = (Get-Date -Format o)
        process_id = $PID
        cycle_root = $cyclePath
        log = $logPath
    }
    foreach ($key in $Data.Keys) {
        $payload[$key] = $Data[$key]
    }
    $temporaryPath = "$statusPath.tmp.$PID"
    [System.IO.File]::WriteAllText(
        $temporaryPath,
        ($payload | ConvertTo-Json -Depth 10),
        $utf8NoBom
    )
    [System.IO.File]::Move($temporaryPath, $statusPath, $true)
    Write-CycleLog "$Status - $Detail"
}

function Invoke-LoggedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$ArgumentList
    )

    Write-CycleLog "EXEC $FilePath $($ArgumentList -join ' ')"
    & $FilePath @ArgumentList 2>&1 | ForEach-Object {
        Write-CycleLog ([string]$_)
    }
    return $LASTEXITCODE
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Get-JsonSha256 {
    param([Parameter(Mandatory = $true)][object]$Value)

    $json = $Value | ConvertTo-Json -Depth 20 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    return [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

function Write-JsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )

    $temporaryPath = "$Path.writing.$PID.$([guid]::NewGuid().ToString('N'))"
    [System.IO.File]::WriteAllText(
        $temporaryPath,
        ($Value | ConvertTo-Json -Depth 20),
        $utf8NoBom
    )
    [System.IO.File]::Move($temporaryPath, $Path, $true)
}

function New-ShutdownPlan {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Binding,
        [datetimeoffset]$NowUtc = [DateTimeOffset]::UtcNow
    )

    $randomBytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($randomBytes)
    $payload = [ordered]@{
        schema_version = 1
        purpose = "codex_offline_repair_shutdown"
        plan_nonce = [Convert]::ToHexString($randomBytes).ToLowerInvariant()
        created_at_utc = $NowUtc.UtcDateTime.ToString("o")
        expires_at_utc = $NowUtc.AddMinutes(5).UtcDateTime.ToString("o")
        binding = $Binding
        binding_sha256 = Get-JsonSha256 -Value $Binding
    }
    $payloadJson = $payload | ConvertTo-Json -Depth 20 -Compress
    $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($payloadJson)
    $planSha256 = [Convert]::ToHexString(
        [System.Security.Cryptography.SHA256]::HashData($payloadBytes)
    ).ToLowerInvariant()
    $envelope = [ordered]@{
        schema_version = 1
        payload_base64 = [Convert]::ToBase64String($payloadBytes)
        plan_sha256 = $planSha256
    }
    Write-JsonAtomic -Path $Path -Value $envelope
    return [pscustomobject]@{
        path = $Path
        payload = [pscustomobject]$payload
        plan_sha256 = $planSha256
    }
}

function Read-ShutdownPlan {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$ExpectedBinding,
        [datetimeoffset]$NowUtc = [DateTimeOffset]::UtcNow
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "A current shutdown plan is required before formal execution: $Path"
    }
    $envelope = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ([int]$envelope.schema_version -ne 1) {
        throw "Unsupported shutdown plan schema: $($envelope.schema_version)"
    }
    try {
        $payloadBytes = [Convert]::FromBase64String([string]$envelope.payload_base64)
    }
    catch {
        throw "Shutdown plan payload is not valid base64."
    }
    $actualPlanSha256 = [Convert]::ToHexString(
        [System.Security.Cryptography.SHA256]::HashData($payloadBytes)
    ).ToLowerInvariant()
    if ($actualPlanSha256 -ne ([string]$envelope.plan_sha256).ToLowerInvariant()) {
        throw "Shutdown plan payload hash mismatch."
    }
    $payload = [System.Text.Encoding]::UTF8.GetString($payloadBytes) | ConvertFrom-Json -DateKind String
    if ([int]$payload.schema_version -ne 1 -or [string]$payload.purpose -ne "codex_offline_repair_shutdown") {
        throw "Shutdown plan purpose or schema is invalid."
    }
    if ([string]$payload.binding_sha256 -ne (Get-JsonSha256 -Value $payload.binding)) {
        throw "Shutdown plan binding is internally inconsistent."
    }
    if ([string]$payload.binding_sha256 -ne (Get-JsonSha256 -Value $ExpectedBinding)) {
        throw "Shutdown plan no longer matches the current repair inputs. Run PreflightOnly again."
    }
    $createdAt = [DateTimeOffset]::Parse([string]$payload.created_at_utc).ToUniversalTime()
    $expiresAt = [DateTimeOffset]::Parse([string]$payload.expires_at_utc).ToUniversalTime()
    if ($createdAt -gt $NowUtc.AddSeconds(15) -or $expiresAt -le $NowUtc) {
        throw "Shutdown plan is not currently valid. Run PreflightOnly again."
    }
    if ($expiresAt -gt $createdAt.AddMinutes(5).AddSeconds(1)) {
        throw "Shutdown plan validity exceeds the five-minute limit."
    }
    if ([string]$payload.plan_nonce -notmatch '^[0-9a-f]{64}$') {
        throw "Shutdown plan nonce is invalid."
    }
    return [pscustomobject]@{
        path = $Path
        payload = $payload
        plan_sha256 = $actualPlanSha256
    }
}

function Read-ShutdownAuthorization {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Plan,
        [Parameter(Mandatory = $true)][string]$CycleRoot,
        [datetimeoffset]$NowUtc = [DateTimeOffset]::UtcNow
    )

    $resolvedPath = Resolve-ContainedPath -Path $Path -RequiredRoot $CycleRoot
    if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
        throw "Shutdown authorization material does not exist: $resolvedPath"
    }
    $envelope = Get-Content -LiteralPath $resolvedPath -Raw | ConvertFrom-Json
    if (
        [int]$envelope.schema_version -ne 1 -or
        [string]$envelope.protection -ne "windows-dpapi-current-user"
    ) {
        throw "Shutdown authorization must be a Windows DPAPI CurrentUser envelope."
    }
    try {
        $protectedBytes = [Convert]::FromBase64String([string]$envelope.protected_payload_base64)
        $payloadBytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $protectedBytes,
            $null,
            [System.Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        $authorization = [System.Text.Encoding]::UTF8.GetString($payloadBytes) | ConvertFrom-Json -DateKind String
    }
    catch {
        throw "Shutdown authorization failed DPAPI integrity or user-scope validation."
    }

    if (
        [int]$authorization.schema_version -ne 1 -or
        [string]$authorization.purpose -ne "codex_offline_repair_shutdown" -or
        $authorization.approved -ne $true
    ) {
        throw "Shutdown authorization purpose, schema, or approval is invalid."
    }
    if (
        [string]$authorization.plan_sha256 -ne [string]$Plan.plan_sha256 -or
        [string]$authorization.plan_nonce -ne [string]$Plan.payload.plan_nonce
    ) {
        throw "Shutdown authorization is not bound to the current plan hash and nonce."
    }
    $authorizationId = [guid]::Empty
    if (-not [guid]::TryParse([string]$authorization.authorization_id, [ref]$authorizationId)) {
        throw "Shutdown authorization id is invalid."
    }
    $authorizedAtText = [string]$authorization.authorized_at_utc
    $expiresAtText = [string]$authorization.expires_at_utc
    if ($authorizedAtText -notmatch 'Z$' -or $expiresAtText -notmatch 'Z$') {
        throw "Shutdown authorization timestamps must explicitly use UTC (Z)."
    }
    $authorizedAt = [DateTimeOffset]::Parse($authorizedAtText).ToUniversalTime()
    $expiresAt = [DateTimeOffset]::Parse($expiresAtText).ToUniversalTime()
    $planCreatedAt = [DateTimeOffset]::Parse([string]$Plan.payload.created_at_utc).ToUniversalTime()
    $planExpiresAt = [DateTimeOffset]::Parse([string]$Plan.payload.expires_at_utc).ToUniversalTime()
    if ($authorizedAt -lt $planCreatedAt -or $authorizedAt -gt $NowUtc.AddSeconds(15)) {
        throw "Shutdown authorization time is outside the current plan window."
    }
    if ($expiresAt -le $NowUtc -or $expiresAt -gt $authorizedAt.AddMinutes(2).AddSeconds(1)) {
        throw "Shutdown authorization is expired or exceeds the two-minute limit."
    }
    if ($expiresAt -gt $planExpiresAt -or $NowUtc -gt $authorizedAt.AddMinutes(2).AddSeconds(1)) {
        throw "Shutdown authorization is outside the current short-lived plan window."
    }
    return [pscustomobject]@{
        path = $resolvedPath
        authorization_id = $authorizationId.ToString("D")
        authorized_at_utc = $authorizedAt.UtcDateTime.ToString("o")
        expires_at_utc = $expiresAt.UtcDateTime.ToString("o")
        plan_sha256 = [string]$Plan.plan_sha256
        plan_nonce = [string]$Plan.payload.plan_nonce
        material_sha256 = Get-FileSha256 -Path $resolvedPath
    }
}

function Use-ShutdownAuthorization {
    param(
        [Parameter(Mandatory = $true)][object]$Authorization,
        [Parameter(Mandatory = $true)][string]$CycleRoot
    )

    $consumedRoot = Join-Path $CycleRoot "consumed_shutdown_authorizations"
    New-Item -ItemType Directory -Path $consumedRoot -Force | Out-Null
    $consumedPath = Join-Path $consumedRoot "$($Authorization.authorization_id).json"
    $payload = [ordered]@{
        status = "consumed"
        authorization_id = $Authorization.authorization_id
        authorized_at_utc = $Authorization.authorized_at_utc
        consumed_at_utc = [DateTimeOffset]::UtcNow.UtcDateTime.ToString("o")
        plan_sha256 = $Authorization.plan_sha256
        plan_nonce = $Authorization.plan_nonce
        material_sha256 = $Authorization.material_sha256
    }
    $bytes = $utf8NoBom.GetBytes(($payload | ConvertTo-Json -Depth 10))
    try {
        $stream = [System.IO.FileStream]::new(
            $consumedPath,
            [System.IO.FileMode]::CreateNew,
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
    catch [System.IO.IOException] {
        throw "Shutdown authorization was already consumed: $($Authorization.authorization_id)"
    }
    return $consumedPath
}

function Invoke-AppleRemoteMarkerPreflight {
    param(
        [Parameter(Mandatory = $true)][string]$PowerShellPath,
        [Parameter(Mandatory = $true)][string]$PluginRepairScript,
        [Parameter(Mandatory = $true)][string]$CodexHomePath
    )

    $output = @(& $PowerShellPath -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $PluginRepairScript -CodexHome $CodexHomePath -CheckAppleRemoteMarkersOnly 2>&1)
    $exitCode = $LASTEXITCODE
    $output | ForEach-Object { Write-CycleLog ([string]$_) }
    if ($exitCode -ne 0) {
        throw "build-ios/build-macos account marker preflight blocked shutdown. Uninstall the remote plugin from the signed-in Codex Plugins UI. $($output -join ' ')"
    }
}

function Invoke-CyclePreflight {
    param(
        [Parameter(Mandatory = $true)][string]$PowerShellPath,
        [Parameter(Mandatory = $true)][string]$RepairScript,
        [Parameter(Mandatory = $true)][string]$PluginRepairScript,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$SupersedeScript,
        [Parameter(Mandatory = $true)][string]$ManifestChainScript,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$CodexHomePath,
        [Parameter(Mandatory = $true)][string]$ReportPath,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [switch]$PriorValidationAlreadySuperseded
    )

    $sourcePreflightExitCode = Invoke-LoggedCommand -FilePath $PowerShellPath -ArgumentList @(
        "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", $RepairScript, "-PreflightSourceInputsOnly"
    )
    if ($sourcePreflightExitCode -ne 0) {
        throw "Repair source input preflight failed with exit code $sourcePreflightExitCode."
    }
    Invoke-AppleRemoteMarkerPreflight -PowerShellPath $PowerShellPath -PluginRepairScript $PluginRepairScript -CodexHomePath $CodexHomePath
    if ($PriorValidationAlreadySuperseded) {
        $inspectionOutput = @(& $PythonPath $ManifestChainScript --manifest $ManifestPath --inspect 2>&1)
        $inspectionOutput | ForEach-Object { Write-CycleLog ([string]$_) }
        if ($LASTEXITCODE -ne 0) {
            throw "Committed manifest inspection failed with exit code $LASTEXITCODE."
        }
        $inspection = (($inspectionOutput | ForEach-Object { [string]$_ }) -join "`n") | ConvertFrom-Json
        if ([string]$inspection.payload.status -ne "validation_superseded") {
            throw "SkipPriorValidationSupersession requires a committed validation_superseded manifest."
        }
        return
    }

    $preflightExitCode = Invoke-LoggedCommand -FilePath $PythonPath -ArgumentList @(
        $SupersedeScript,
        "--manifest", $ManifestPath,
        "--codex-home", $CodexHomePath,
        "--report", $ReportPath,
        "--reason", "Offline supersession before the controlled repair cycle",
        "--backup-root", $BackupRoot,
        "--preflight-only"
    )
    if ($preflightExitCode -ne 0) {
        throw "Supersession preflight failed with exit code $preflightExitCode."
    }
}

function Assert-RepairRestartGate {
    param(
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ManifestInspector
    )

    $lockPath = Join-Path $BackupRoot "active_repair.lock.json"
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        throw "The offline repair did not leave an active validation lock: $lockPath"
    }
    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
    if ([string]$lock.status -ne "pending_restart_validation") {
        throw "The active repair lock is not pending restart validation: $($lock.status)"
    }
    $runRoot = Resolve-ContainedPath -Path ([string]$lock.run_root) -RequiredRoot $BackupRoot
    $manifestPath = Resolve-ContainedPath -Path ([string]$lock.manifest) -RequiredRoot $runRoot
    $pendingValidationPath = Resolve-ContainedPath -Path ([string]$lock.pending_validation) -RequiredRoot $runRoot
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "The pending repair manifest is missing: $manifestPath"
    }
    if (-not (Test-Path -LiteralPath $pendingValidationPath -PathType Leaf)) {
        throw "The pending restart validation marker is missing: $pendingValidationPath"
    }

    $inspectionOutput = @(& $PythonPath $ManifestInspector --manifest $manifestPath --inspect 2>&1)
    $inspectionExitCode = $LASTEXITCODE
    if ($inspectionExitCode -ne 0) {
        throw "Committed repair manifest inspection failed with exit code $inspectionExitCode. $($inspectionOutput -join ' ')"
    }
    $inspection = (($inspectionOutput | ForEach-Object { [string]$_ }) -join "`n") | ConvertFrom-Json
    if ([string]$inspection.payload.status -ne "pending_restart_validation") {
        throw "The committed repair manifest is not pending restart validation: $($inspection.payload.status)"
    }
    if (
        [string]$inspection.payload.runner_run_id -ne [string]$lock.run_id -or
        [System.IO.Path]::GetFullPath([string]$inspection.payload.run_root).TrimEnd('\') -ne $runRoot
    ) {
        throw "The committed repair manifest does not match the active repair transaction."
    }
    if ([string]$inspection.sha256 -ne [string]$lock.repair_manifest_sha256) {
        throw "The active repair lock does not bind the committed manifest head."
    }

    $pendingValidation = Get-Content -LiteralPath $pendingValidationPath -Raw | ConvertFrom-Json
    if (
        [string]$pendingValidation.status -ne "pending_restart_validation" -or
        [string]$pendingValidation.run_id -ne [string]$lock.run_id -or
        [System.IO.Path]::GetFullPath([string]$pendingValidation.manifest).TrimEnd('\') -ne $manifestPath
    ) {
        throw "The pending restart validation marker does not match the active repair transaction."
    }
    return [pscustomobject]@{
        run_id = [string]$lock.run_id
        run_root = $runRoot
        manifest = $manifestPath
        manifest_sha256 = [string]$inspection.sha256
    }
}

function Get-CodexRuntimeProcesses {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    return @($processes | Where-Object {
        $name = [string]$_.Name
        $commandLine = [string]$_.CommandLine
        $name -eq "ChatGPT.exe" -or
        $name -eq "codex.exe" -or
        $name -eq "node_repl.exe" -or
        $name -eq "codex-home-manager-local-win-x64.exe" -or
        (
            $name -in @("node.exe", "cmd.exe") -and
            $commandLine -match "(?i)(xcodebuildmcp|[\\/]mcp[\\/](server|index)|codex-computer-use|@oai[\\/]sky)"
        )
    })
}

function Stop-CodexRuntime {
    $chatGptProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" -ErrorAction SilentlyContinue)
    foreach ($process in $chatGptProcesses) {
        & taskkill.exe /PID ([string]$process.ProcessId) /T /F 2>&1 | ForEach-Object {
            Write-CycleLog ([string]$_)
        }
    }

    $deadline = (Get-Date).AddSeconds(90)
    do {
        $remaining = @(Get-CodexRuntimeProcesses)
        foreach ($process in $remaining) {
            try {
                Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
                Write-CycleLog "Stopped remaining runtime process $($process.Name) pid=$($process.ProcessId)."
            }
            catch {
                Write-CycleLog "Process stop retry pid=$($process.ProcessId): $($_.Exception.Message)"
            }
        }
        Start-Sleep -Milliseconds 750
        $remaining = @(Get-CodexRuntimeProcesses)
    } while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)

    if ($remaining.Count -gt 0) {
        $description = ($remaining | ForEach-Object { "$($_.Name):$($_.ProcessId)" }) -join ", "
        throw "Codex runtime did not stop cleanly: $description"
    }
    Start-Sleep -Seconds 3
    if (@(Get-CodexRuntimeProcesses).Count -gt 0) {
        throw "Codex runtime restarted during the offline quiet period."
    }
}

function Start-CodexDesktop {
    $desktop = @(Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" -ErrorAction SilentlyContinue)
    if ($desktop.Count -eq 0) {
        Write-CycleLog "Launching Codex Desktop through AppUserModelId $CodexAppUserModelId."
        Start-Process -FilePath "explorer.exe" -ArgumentList "shell:AppsFolder\$CodexAppUserModelId"
    }
    else {
        Write-CycleLog "Codex Desktop is already running; no duplicate launch is needed."
    }
    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 2
        $desktop = @(Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" -ErrorAction SilentlyContinue)
    } while ($desktop.Count -eq 0 -and (Get-Date) -lt $deadline)
    if ($desktop.Count -eq 0) {
        throw "Codex Desktop did not start within 120 seconds."
    }
    return @($desktop | Select-Object -ExpandProperty ProcessId)
}

function Start-CodexHomeManagerConnector {
    param([AllowNull()][string]$ExecutablePath)

    if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
        Write-CycleLog "No running local connector was captured before shutdown; connector restart is not required."
        return @()
    }
    if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
        throw "Captured local connector executable no longer exists: $ExecutablePath"
    }

    $running = @(Get-CimInstance Win32_Process -Filter "Name = 'codex-home-manager-local-win-x64.exe'" -ErrorAction SilentlyContinue)
    if ($running.Count -eq 0) {
        Write-CycleLog "Launching Codex Home Manager connector: $ExecutablePath"
        $startParameters = @{
            FilePath = $ExecutablePath
            WorkingDirectory = (Split-Path -Parent $ExecutablePath)
            WindowStyle = "Hidden"
        }
        Start-Process @startParameters
    }

    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 2
        $running = @(Get-CimInstance Win32_Process -Filter "Name = 'codex-home-manager-local-win-x64.exe'" -ErrorAction SilentlyContinue)
        $listener = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
    } while (($running.Count -eq 0 -or $listener.Count -eq 0) -and (Get-Date) -lt $deadline)
    if ($running.Count -eq 0 -or $listener.Count -eq 0) {
        throw "Codex Home Manager connector did not restore port 8765 within 120 seconds."
    }
    return @($running | Select-Object -ExpandProperty ProcessId)
}

function Restore-CapturedRuntime {
    param(
        [Parameter(Mandatory = $true)][bool]$DesktopWasRunning,
        [Parameter(Mandatory = $true)][bool]$ConnectorWasRunning,
        [AllowNull()][string]$ConnectorExecutablePath,
        [scriptblock]$DesktopStarter = { Start-CodexDesktop },
        [scriptblock]$ConnectorStarter = {
            param([AllowNull()][string]$ExecutablePath)
            Start-CodexHomeManagerConnector -ExecutablePath $ExecutablePath
        }
    )

    $errors = [System.Collections.Generic.List[string]]::new()
    $desktopProcessIds = @()
    $connectorProcessIds = @()
    if ($DesktopWasRunning) {
        try {
            $desktopProcessIds = @(& $DesktopStarter)
        }
        catch {
            $errors.Add("Desktop: $($_.Exception.Message)")
        }
    }
    if ($ConnectorWasRunning) {
        try {
            $connectorProcessIds = @(& $ConnectorStarter $ConnectorExecutablePath)
        }
        catch {
            $errors.Add("Connector: $($_.Exception.Message)")
        }
    }
    return [pscustomobject]@{
        desktop_process_ids = $desktopProcessIds
        connector_process_ids = $connectorProcessIds
        errors = @($errors)
    }
}

$mutexName = "Global\OpenAI-Codex-Controlled-Offline-Repair-Cycle"
$mutex = [System.Threading.Mutex]::new($false, $mutexName)
$ownsMutex = $false
$repairExitCode = $null
$cycleError = $null
$restartProcessIds = @()
$connectorProcessIds = @()
$connectorExecutablePath = $null
$restartAuthorized = $false
$restartGate = $null
$shutdownAttempted = $false
$desktopWasRunning = $false
$connectorWasRunning = $false
$authorization = $null
$authorizationConsumedPath = $null

try {
    try {
        $ownsMutex = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) {
        throw "Another controlled offline repair cycle is already active."
    }

    Set-Location -LiteralPath $workspacePath
    $supersedeScript = Join-Path $workspacePath "scripts\supersede_pending_repair_validation.py"
    $repairScript = Join-Path $workspacePath "scripts\repair_all_codex_after_exit.ps1"
    $manifestChainScript = Join-Path $workspacePath "scripts\repair_manifest_chain.py"
    $pluginRepairScript = Join-Path $workspacePath "scripts\repair_codex_bundled_plugins.ps1"
    foreach ($requiredFile in @($supersedeScript, $repairScript, $manifestChainScript, $pluginRepairScript)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Required repair script is missing: $requiredFile"
        }
    }

    $normalizedSlimThreadIds = @(
        $SlimThreadId |
            ForEach-Object { [string]$_ } |
            ForEach-Object { $_.Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Sort-Object -Unique
    )
    $planBinding = [ordered]@{
        workspace_root = $workspacePath
        cycle_root = $cyclePath
        pending_manifest = $manifestPath
        pending_manifest_sha256 = Get-FileSha256 -Path $manifestPath
        supersede_report = $reportPath
        codex_home = $codexHomePath
        backup_parent = $backupPath
        python_executable = [System.IO.Path]::GetFullPath($PythonExecutable)
        powershell_executable = [System.IO.Path]::GetFullPath($PowerShellExecutable)
        codex_app_user_model_id = $CodexAppUserModelId
        skip_prior_validation_supersession = [bool]$SkipPriorValidationSupersession
        slim_thread_ids = @($normalizedSlimThreadIds)
        scripts = [ordered]@{
            controlled_offline_cycle_sha256 = Get-FileSha256 -Path $PSCommandPath
            supersede_pending_repair_validation_sha256 = Get-FileSha256 -Path $supersedeScript
            repair_all_codex_after_exit_sha256 = Get-FileSha256 -Path $repairScript
            repair_manifest_chain_sha256 = Get-FileSha256 -Path $manifestChainScript
            repair_codex_bundled_plugins_sha256 = Get-FileSha256 -Path $pluginRepairScript
        }
    }
    $shutdownPlanPath = Join-Path $cyclePath "shutdown_plan.json"
    $authorizationProvided = -not [string]::IsNullOrWhiteSpace($ShutdownAuthorizationPath)
    $effectivePreflightOnly = $PreflightOnly -or -not $authorizationProvided

    Write-CycleStatus -Status "preflight_running" -Detail "Validating the complete shutdown plan while Codex remains online."
    Invoke-CyclePreflight `
        -PowerShellPath $PowerShellExecutable `
        -RepairScript $repairScript `
        -PluginRepairScript $pluginRepairScript `
        -PythonPath $PythonExecutable `
        -SupersedeScript $supersedeScript `
        -ManifestChainScript $manifestChainScript `
        -ManifestPath $manifestPath `
        -CodexHomePath $codexHomePath `
        -ReportPath $reportPath `
        -BackupRoot $backupPath `
        -PriorValidationAlreadySuperseded:$SkipPriorValidationSupersession

    if ($effectivePreflightOnly) {
        $shutdownPlan = New-ShutdownPlan -Path $shutdownPlanPath -Binding $planBinding
        $preflightStatus = if ($authorizationProvided) { "preflight_passed" } else { "authorization_required" }
        $preflightDetail = if ($authorizationProvided) {
            "Preflight completed without stopping Codex; a new plan was issued and formal execution still requires a matching authorization."
        }
        else {
            "Preflight completed without stopping Codex. A current DPAPI-protected, plan-bound authorization is required for formal execution."
        }
        Write-CycleStatus -Status $preflightStatus -Detail $preflightDetail -Data @{
            shutdown_plan = $shutdownPlan.path
            plan_sha256 = $shutdownPlan.plan_sha256
            plan_nonce = $shutdownPlan.payload.plan_nonce
            plan_created_at_utc = $shutdownPlan.payload.created_at_utc
            plan_expires_at_utc = $shutdownPlan.payload.expires_at_utc
            authorization_required = $true
            codex_was_stopped = $false
        }
        exit 0
    }

    if (-not $AllowOfflineFallback) {
        Write-CycleStatus -Status "online_repair_required" -Detail "The default repair policy is online-first. Offline fallback was not explicitly enabled, so Codex was not stopped." -Data @{
            codex_was_stopped = $false
            offline_fallback_enabled = $false
            authorization_consumed = $false
        }
        throw "Offline fallback is disabled by default. Retry online repair first; use -AllowOfflineFallback only after a verified online transaction conflict and fresh user approval."
    }

    $shutdownPlan = Read-ShutdownPlan -Path $shutdownPlanPath -ExpectedBinding $planBinding
    $authorization = Read-ShutdownAuthorization `
        -Path $ShutdownAuthorizationPath `
        -Plan $shutdownPlan `
        -CycleRoot $cyclePath

    $connectorProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'codex-home-manager-local-win-x64.exe'" -ErrorAction SilentlyContinue)
    $desktopProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" -ErrorAction SilentlyContinue)
    $desktopWasRunning = $desktopProcesses.Count -gt 0
    $connectorWasRunning = $connectorProcesses.Count -gt 0
    $connectorExecutablePath = @($connectorProcesses | ForEach-Object { [string]$_.ExecutablePath } | Where-Object { $_ } | Select-Object -Unique | Select-Object -First 1)
    if ($connectorExecutablePath.Count -gt 0) {
        $connectorExecutablePath = [string]$connectorExecutablePath[0]
    }
    else {
        $connectorExecutablePath = $null
    }
    if ($connectorWasRunning -and [string]::IsNullOrWhiteSpace($connectorExecutablePath)) {
        throw "The running local connector executable path could not be captured; shutdown is blocked because its prior state cannot be restored."
    }

    Invoke-AppleRemoteMarkerPreflight `
        -PowerShellPath $PowerShellExecutable `
        -PluginRepairScript $pluginRepairScript `
        -CodexHomePath $codexHomePath
    $authorizationConsumedPath = Use-ShutdownAuthorization -Authorization $authorization -CycleRoot $cyclePath
    Write-CycleStatus -Status "shutdown_authorized" -Detail "A fresh one-time authorization was consumed for this exact shutdown plan." -Data @{
        authorization_id = $authorization.authorization_id
        authorized_at_utc = $authorization.authorized_at_utc
        authorization_expires_at_utc = $authorization.expires_at_utc
        authorization_consumed = $authorizationConsumedPath
        plan_sha256 = $shutdownPlan.plan_sha256
        plan_nonce = $shutdownPlan.payload.plan_nonce
        desktop_was_running = $desktopWasRunning
        connector_was_running = $connectorWasRunning
        connector_executable = $connectorExecutablePath
    }

    $shutdownAttempted = $true
    Write-CycleStatus -Status "stopping_codex" -Detail "Stopping Codex Desktop and its runtime process tree."
    Stop-CodexRuntime
    Write-CycleStatus -Status "offline_confirmed" -Detail "Codex runtime is fully offline."

    if ($SkipPriorValidationSupersession) {
        Write-CycleStatus -Status "prior_validation_already_superseded" -Detail "The prior transaction is already superseded; the new repair will create a fresh full prompt audit."
    }
    else {
        Write-CycleStatus -Status "superseding_prior_validation" -Detail "Auditing every active thread and preserving the exact prompt sequence."
        $supersedeExitCode = Invoke-LoggedCommand -FilePath $PythonExecutable -ArgumentList @(
            $supersedeScript,
            "--manifest", $manifestPath,
            "--codex-home", $codexHomePath,
            "--report", $reportPath,
            "--reason", "Superseded offline after committed marketplace ownership fixes",
            "--backup-root", $backupPath
        )
        if ($supersedeExitCode -ne 0) {
            throw "Prior validation supersession failed with exit code $supersedeExitCode."
        }
        Write-CycleStatus -Status "prior_validation_superseded" -Detail "The prior transaction passed the offline prompt-preservation audit."
    }

    Write-CycleStatus -Status "repair_running" -Detail "Running the complete one-shot Codex repair and diagnostics pipeline."
    $repairArguments = @(
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", $repairScript,
        "-TimeoutMinutes", "3",
        "-ExecutionTimeoutMinutes", "240",
        "-StageTimeoutMinutes", "75",
        "-QuietSeconds", "10",
        "-CodexHome", $codexHomePath,
        "-BackupParent", $backupPath
    )
    if ($normalizedSlimThreadIds.Count -gt 0) {
        $slimThreadIdsJson = ConvertTo-Json -InputObject @($normalizedSlimThreadIds) -Compress
        $repairArguments += @("-SlimThreadIdsJson", $slimThreadIdsJson)
        Write-CycleLog "Targeted prompt-preserving slim threads: $($normalizedSlimThreadIds -join ', ')"
    }
    $repairExitCode = Invoke-LoggedCommand -FilePath $PowerShellExecutable -ArgumentList $repairArguments
    if ($repairExitCode -ne 0) {
        throw "Complete offline repair failed with exit code $repairExitCode."
    }
    $restartGate = Assert-RepairRestartGate `
        -BackupRoot $backupPath `
        -PythonPath $PythonExecutable `
        -ManifestInspector $manifestChainScript
    $restartAuthorized = $true
    Write-CycleStatus -Status "repair_pending_restart_validation" -Detail "Offline repair passed all restart gates and is ready for live validation." -Data @{
        run_id = $restartGate.run_id
        run_root = $restartGate.run_root
        manifest = $restartGate.manifest
        manifest_sha256 = $restartGate.manifest_sha256
    }
}
catch {
    $cycleError = $_.Exception.Message
    $failureStatus = if ($shutdownAttempted) { "repair_failed_offline" } else { "failed_before_shutdown" }
    Write-CycleStatus -Status $failureStatus -Detail $cycleError -Data @{
        repair_exit_code = $repairExitCode
        shutdown_attempted = $shutdownAttempted
        authorization_id = if ($null -ne $authorization) { $authorization.authorization_id } else { $null }
    }
}
finally {
    if ($shutdownAttempted) {
        $repairSuccessGatePassed = $restartAuthorized -and $repairExitCode -eq 0 -and $null -eq $cycleError
        $restoreStatus = if ($repairSuccessGatePassed) { "restoring_runtime_pending_validation" } else { "restoring_runtime_after_failure" }
        Write-CycleStatus -Status $restoreStatus -Detail "Restoring the Codex runtime state captured before shutdown." -Data @{
            repair_success_gate_passed = $repairSuccessGatePassed
            repair_error = $cycleError
            desktop_was_running = $desktopWasRunning
            connector_was_running = $connectorWasRunning
            connector_executable = $connectorExecutablePath
        }
        $restoreResult = Restore-CapturedRuntime `
            -DesktopWasRunning $desktopWasRunning `
            -ConnectorWasRunning $connectorWasRunning `
            -ConnectorExecutablePath $connectorExecutablePath
        $restartProcessIds = @($restoreResult.desktop_process_ids)
        $connectorProcessIds = @($restoreResult.connector_process_ids)
        $restartErrors = @($restoreResult.errors)

        if ($restartErrors.Count -gt 0) {
            $restartError = $restartErrors -join " | "
            Write-CycleStatus -Status "runtime_restore_partial_failure" -Detail $restartError -Data @{
                repair_success_gate_passed = $repairSuccessGatePassed
                repair_error = $cycleError
                restart_process_ids = $restartProcessIds
                connector_process_ids = $connectorProcessIds
            }
            $cycleError = if ($null -eq $cycleError) { $restartError } else { "$cycleError | runtime restore: $restartError" }
        }
        elseif ($repairSuccessGatePassed) {
            Write-CycleStatus -Status "runtime_restored_pending_live_validation" -Detail "The captured runtime state was restored after the pending_restart_validation gate passed." -Data @{
                repair_success_gate_passed = $true
                repair_outcome = "pending_live_validation"
                restart_process_ids = $restartProcessIds
                connector_process_ids = $connectorProcessIds
                manifest = $restartGate.manifest
                manifest_sha256 = $restartGate.manifest_sha256
            }
        }
        else {
            $blockedReason = if ($null -ne $cycleError) {
                $cycleError
            }
            elseif ($repairExitCode -ne 0) {
                "Offline repair exit code was $repairExitCode."
            }
            else {
                "The pending_restart_validation restart gate was not committed."
            }
            Write-CycleStatus -Status "runtime_restored_after_repair_failure" -Detail "The captured runtime state was restored, but the repair remains failed: $blockedReason" -Data @{
                repair_exit_code = $repairExitCode
                repair_success_gate_passed = $false
                pending_restart_validation_committed = $restartAuthorized
                restart_process_ids = $restartProcessIds
                connector_process_ids = $connectorProcessIds
            }
            if ($null -eq $cycleError) {
                $cycleError = $blockedReason
            }
        }
    }
    if ($ownsMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}

if ($null -ne $cycleError) {
    exit 1
}
exit 0
