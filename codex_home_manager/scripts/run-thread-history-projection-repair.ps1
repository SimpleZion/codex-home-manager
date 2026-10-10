param(
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,
    [Parameter(Mandatory = $true)]
    [string]$RepairScript,
    [Parameter(Mandatory = $true)]
    [string]$ThreadId,
    [Parameter(Mandatory = $true)]
    [string]$RolloutPath,
    [Parameter(Mandatory = $true)]
    [string]$ResultDirectory,
    [string]$CodePath = 'C:\Program Files\Microsoft VS Code\Code.exe',
    [string]$CodexAppId = 'OpenAI.Codex_2p2nqsd0c76g0!App'
)

$ErrorActionPreference = 'Stop'
$resultRoot = [System.IO.Path]::GetFullPath($ResultDirectory)
[System.IO.Directory]::CreateDirectory($resultRoot) | Out-Null
$statusPath = Join-Path $resultRoot 'status.json'
$logPath = Join-Path $resultRoot 'runner.log'
$repairOutputPath = Join-Path $resultRoot 'repair-output.json'
$repairErrorPath = Join-Path $resultRoot 'repair-error.log'

function Write-RepairStatus {
    param([string]$Stage, [string]$Message, [bool]$Success = $false)
    $payload = [ordered]@{
        updatedAt = (Get-Date).ToString('o')
        stage = $Stage
        message = $Message
        success = $Success
        threadId = $ThreadId
        repairOutputPath = $repairOutputPath
        repairErrorPath = $repairErrorPath
    }
    $temporaryPath = "$statusPath.writing"
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporaryPath -Encoding utf8
    Move-Item -LiteralPath $temporaryPath -Destination $statusPath -Force
    "$(Get-Date -Format o) [$Stage] $Message" | Add-Content -LiteralPath $logPath -Encoding utf8
}

function Stop-CodexAndCode {
    Write-RepairStatus -Stage 'closing-apps' -Message 'Closing Codex Desktop and Visual Studio Code.'
    Get-Process -Name ChatGPT,Code -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | ForEach-Object {
        [void]$_.CloseMainWindow()
    }
    Start-Sleep -Seconds 8
    Get-Process -Name ChatGPT,Code -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('Codex.exe', 'codex.exe') -or
        (
            $_.Name -in @('node.exe', 'node_repl.exe', 'extension-host.exe') -and
            $_.CommandLine -and
            $_.CommandLine -match 'app-server|mcp[/\\]server[.]mjs|code-mode-host'
        )
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 4
    $remaining = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('ChatGPT.exe', 'Codex.exe', 'codex.exe', 'Code.exe') -or
        (
            $_.Name -in @('node.exe', 'node_repl.exe', 'extension-host.exe') -and
            $_.CommandLine -and
            $_.CommandLine -match 'app-server|mcp[/\\]server[.]mjs|code-mode-host'
        )
    }
    if ($remaining) {
        $remainingSummary = ($remaining | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress)
        throw "Codex or VS Code processes remain after shutdown: $remainingSummary"
    }
}

function Start-Apps {
    param([bool]$RecordSuccess = $true)
    if ($RecordSuccess) {
        Write-RepairStatus -Stage 'reopening-apps' -Message 'Repair succeeded; reopening Codex Desktop and Visual Studio Code.'
    }
    Start-Process -FilePath 'explorer.exe' -ArgumentList "shell:AppsFolder\$CodexAppId" -WindowStyle Hidden
    if (Test-Path -LiteralPath $CodePath) {
        Start-Process -FilePath $CodePath -WindowStyle Hidden
    }
}

try {
    Write-RepairStatus -Stage 'started' -Message 'Independent offline repair runner started.'
    Stop-CodexAndCode
    Write-RepairStatus -Stage 'repairing' -Message 'Creating rollback backup and resetting the target projection.'
    $arguments = @(
        $RepairScript,
        '--thread-id', $ThreadId,
        '--confirm-thread-id', $ThreadId,
        '--rollout', $RolloutPath,
        '--apply'
    )
    $process = Start-Process -FilePath $PythonPath -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden -RedirectStandardOutput $repairOutputPath -RedirectStandardError $repairErrorPath
    if ($process.ExitCode -ne 0) {
        $errorText = if (Test-Path -LiteralPath $repairErrorPath) { Get-Content -LiteralPath $repairErrorPath -Raw } else { '' }
        throw "Projection repair failed with exit code $($process.ExitCode): $errorText"
    }
    $repairResult = Get-Content -LiteralPath $repairOutputPath -Raw | ConvertFrom-Json
    if ($repairResult.status -ne 'projection-reset') {
        throw "Projection repair did not return projection-reset status."
    }
    Write-RepairStatus -Stage 'repaired' -Message "Target projection reset; backup: $($repairResult.backupDirectory)"
    Start-Apps -RecordSuccess $true
    Start-Sleep -Seconds 12
    $codexMain = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ChatGPT.exe' -and $_.CommandLine -notmatch '--type=' } | Select-Object -First 1
    if (-not $codexMain) {
        throw 'Codex Desktop main process did not appear after relaunch.'
    }
    Write-RepairStatus -Stage 'completed' -Message "Repair completed and Codex Desktop reopened with PID $($codexMain.ProcessId)." -Success $true
}
catch {
    $failureMessage = $_.Exception.Message
    try {
        Start-Apps -RecordSuccess $false
    }
    catch {
        "$(Get-Date -Format o) [reopen-failed] $($_.Exception.Message)" | Add-Content -LiteralPath $logPath -Encoding utf8
    }
    Write-RepairStatus -Stage 'failed' -Message $failureMessage
    exit 1
}
