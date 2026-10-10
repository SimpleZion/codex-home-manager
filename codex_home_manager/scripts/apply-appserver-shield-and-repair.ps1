param(
    [int]$InitialDelaySeconds = 8,
    [string]$ThreadId = "019fcfbd-174c-7fd0-b84a-8c7a883a9e2f",
    [string]$RolloutPath = "D:\.codex\sessions\2026\08\05\rollout-2026-08-05T10-25-03-019fcfbd-174c-7fd0-b84a-8c7a883a9e2f.jsonl"
)

$ErrorActionPreference = "Stop"
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$artifactRoot = Join-Path $workspaceRoot "artifacts\appserver-shield-repair\$timestamp"
$resultPath = Join-Path $artifactRoot "result.json"
$transcriptPath = Join-Path $artifactRoot "runner.log"
$repairResultPath = Join-Path $artifactRoot "thread-repair.json"
$backupRoot = "D:\Backup\codex_home_manager\orphan-tool-call-repair-$timestamp"
$installScript = Join-Path $PSScriptRoot "install-codex-appserver-shield.ps1"
$repairScript = Join-Path $PSScriptRoot "repair-orphan-custom-tool-calls-once.py"
$result = [ordered]@{schemaVersion=1;status="started";startedAt=(Get-Date).ToUniversalTime().ToString("o");threadId=$ThreadId;rolloutPath=$RolloutPath;artifactRoot=$artifactRoot;backupRoot=$backupRoot}
$installApplied = $false
$installBackup = $null

New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null
Start-Transcript -LiteralPath $transcriptPath -Force | Out-Null

function Save-Result {
    $result.updatedAt = (Get-Date).ToUniversalTime().ToString("o")
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding utf8
}

function Stop-CodexDesktopTree {
    $processes = @(Get-CimInstance Win32_Process)
    $roots = @($processes | Where-Object { $_.Name -ieq "ChatGPT.exe" } | Select-Object -ExpandProperty ProcessId)
    $descendants = New-Object System.Collections.Generic.HashSet[int]
    foreach ($root in $roots) { $null = $descendants.Add([int]$root) }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $processes) {
            if ($descendants.Contains([int]$process.ParentProcessId) -and $descendants.Add([int]$process.ProcessId)) { $changed = $true }
        }
    }
    $ordered = @($processes | Where-Object { $descendants.Contains([int]$_.ProcessId) } | Sort-Object { if ($_.Name -ieq "ChatGPT.exe") { 1 } else { 0 } })
    foreach ($process in $ordered) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
    $remaining = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -ieq "ChatGPT.exe" -or
        ($_.Name -ieq "codex.exe" -and [string]$_.CommandLine -match "app-server") -or
        $_.Name -ieq "codex-appserver-shield.exe" -or
        $_.Name -ieq "node_repl.exe" -or
        $_.Name -ieq "codex-code-mode-host.exe" -or
        $_.Name -ieq "codex-command-runner.exe"
    })
    foreach ($process in $remaining) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $blockers = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -ieq "ChatGPT.exe" -or
        ($_.Name -ieq "codex.exe" -and [string]$_.CommandLine -match "app-server") -or
        $_.Name -ieq "codex-appserver-shield.exe" -or
        $_.Name -ieq "node_repl.exe" -or
        $_.Name -ieq "codex-code-mode-host.exe" -or
        $_.Name -ieq "codex-command-runner.exe"
    })
    if ($blockers.Count -gt 0) { throw "Codex did not become fully offline. Blocking PIDs: $($blockers.ProcessId -join ', ')" }
}

function Start-CodexDesktop {
    foreach ($name in @("CODEX_HOME", "CODEX_APP_SERVER_REAL_EXE", "CODEX_CLI_PATH")) {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if ($null -eq $value) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue } else { Set-Item "Env:$name" $value }
    }
    $package = Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1
    $executable = Join-Path $package.InstallLocation "app\ChatGPT.exe"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Codex Desktop executable is missing: $executable" }
    Start-Process -FilePath $executable
}

function Wait-CodexDesktopReady {
    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Seconds 2
        $processes = @(Get-CimInstance Win32_Process)
        $main = @($processes | Where-Object { $_.Name -ieq "ChatGPT.exe" -and [string]$_.CommandLine -notmatch "--type=" })
        $shield = @($processes | Where-Object { $_.Name -ieq "codex-appserver-shield.exe" -and [string]$_.CommandLine -match "app-server" })
        $real = @($processes | Where-Object { $_.Name -ieq "codex.exe" -and [string]$_.CommandLine -match "app-server" -and $_.ParentProcessId -in @($shield.ProcessId) })
        if ($main.Count -gt 0 -and $shield.Count -gt 0 -and $real.Count -gt 0) {
            Start-Sleep -Seconds 12
            $stillAlive = @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -in @($shield.ProcessId + $real.ProcessId) })
            if ($stillAlive.Count -ge 2) { return [ordered]@{mainPids=@($main.ProcessId);shieldPids=@($shield.ProcessId);realPids=@($real.ProcessId)} }
        }
    } while ((Get-Date) -lt $deadline)
    throw "Codex Desktop did not reach the protected app-server ready state within 90 seconds."
}

try {
    Save-Result
    Start-Sleep -Seconds $InitialDelaySeconds
    $automationPaths = @("D:\.codex\automations\miracleplus\automation.toml", "D:\.codex\automations\resume-factor-evaluations\automation.toml")
    $result.automationStatusBefore = @($automationPaths | ForEach-Object {[ordered]@{path=$_;status=((Select-String -LiteralPath $_ -Pattern '^status\s*=\s*"([^"]+)"').Matches.Groups[1].Value)}})
    Stop-CodexDesktopTree
    $result.codexOfflineAt = (Get-Date).ToUniversalTime().ToString("o")
    Save-Result

    $env:PYTHONPATH = "$workspaceRoot;$workspaceRoot\.."
    & python $repairScript --source $RolloutPath --thread-id $ThreadId --backup-root $backupRoot --output $repairResultPath
    if ($LASTEXITCODE -ne 0) { throw "Thread orphan-call repair failed with exit code $LASTEXITCODE" }
    $result.threadRepair = Get-Content -Raw -LiteralPath $repairResultPath | ConvertFrom-Json
    Save-Result

    $installText = (& $installScript -Mode Apply 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "App-server shield installation failed: $installText" }
    $installResult = $installText | ConvertFrom-Json
    $installApplied = $true
    $installBackup = [string]$installResult.backup
    $result.install = $installResult
    Save-Result

    $env:CODEX_HOME = [string]$installResult.userEnvironment.CODEX_HOME
    $env:CODEX_APP_SERVER_REAL_EXE = [string]$installResult.userEnvironment.CODEX_APP_SERVER_REAL_EXE
    $env:CODEX_CLI_PATH = [string]$installResult.userEnvironment.CODEX_CLI_PATH
    Start-CodexDesktop
    $result.processes = Wait-CodexDesktopReady
    $targetHash = (Get-FileHash -Algorithm SHA256 -LiteralPath ([string]$installResult.shieldPath)).Hash.ToLowerInvariant()
    if ($targetHash -ne [string]$installResult.shieldSha256) { throw "Desktop startup replaced the installed shield executable." }
    $result.automationStatusAfter = @($automationPaths | ForEach-Object {[ordered]@{path=$_;status=((Select-String -LiteralPath $_ -Pattern '^status\s*=\s*"([^"]+)"').Matches.Groups[1].Value)}})
    if (@($result.automationStatusAfter | Where-Object { $_.status -ne "PAUSED" }).Count -gt 0) { throw "A manually paused automation changed state during repair." }
    $result.status = "installed_and_restarted"
    $result.completedAt = (Get-Date).ToUniversalTime().ToString("o")
    Save-Result
} catch {
    $result.status = "failed"
    $result.error = $_.Exception.ToString()
    Save-Result
    if ($installApplied -and $installBackup) {
        try {
            Stop-CodexDesktopTree
            $rollbackText = (& $installScript -Mode Rollback -RollbackBackup $installBackup 2>&1 | Out-String).Trim()
            $result.rollback = $rollbackText
            Start-CodexDesktop
            $result.status = "failed_and_rolled_back"
        } catch {
            $result.rollbackError = $_.Exception.ToString()
            $result.status = "failed_and_rollback_failed"
        }
        Save-Result
    } else {
        try { Start-CodexDesktop } catch { $result.restartError = $_.Exception.ToString(); Save-Result }
    }
} finally {
    try { Stop-Transcript | Out-Null } catch {}
}
