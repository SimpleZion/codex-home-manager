param(
    [Parameter(Mandatory = $true)]
    [string]$StatusPath,

    [string]$CodexHome = "D:\.codex",

    [int]$InitialDelaySeconds = 5
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$statusDirectory = Split-Path -Parent $StatusPath
New-Item -ItemType Directory -Path $statusDirectory -Force | Out-Null

function Write-RepairStatus {
    param(
        [string]$State,
        [hashtable]$Details = @{}
    )

    $status = @{
        state = $State
        updatedAt = [DateTimeOffset]::Now.ToString("o")
        details = $Details
    }
    $status | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $StatusPath -Encoding utf8
}

function Get-CodexRuntimeProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -ieq "ChatGPT.exe" -or
        $_.Name -ieq "node_repl.exe" -or
        $_.Name -ieq "codex-code-mode-host.exe" -or
        ($_.Name -ieq "codex.exe" -and $_.ExecutablePath -like "*WindowsApps\OpenAI.Codex_*")
    })
}

Write-RepairStatus -State "waiting" -Details @{ initialDelaySeconds = $InitialDelaySeconds }
Start-Sleep -Seconds $InitialDelaySeconds

try {
    Write-RepairStatus -State "stopping_codex"
    foreach ($process in (Get-CodexRuntimeProcesses | Sort-Object ProcessId -Descending)) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }

    $deadline = [DateTimeOffset]::Now.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $remainingProcesses = Get-CodexRuntimeProcesses
        foreach ($process in $remainingProcesses) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } while ($remainingProcesses.Count -gt 0 -and [DateTimeOffset]::Now -lt $deadline)

    $remainingProcesses = Get-CodexRuntimeProcesses
    if ($remainingProcesses.Count -gt 0) {
        throw "Codex runtime processes did not stop within 30 seconds: $($remainingProcesses.ProcessId -join ', ')"
    }

    Write-RepairStatus -State "repairing"
    $env:PYTHONPATH = $projectRoot
    $env:CODEX_HOME_MANAGER_BACKUP_ROOT = "D:\Backup\codex_home_manager"
    $pythonExecutable = (Get-Command python -ErrorAction Stop).Source
    $pythonSource = @'
import json
import os

from backend.codex_data import repair_official_thread_tools_exposure

result = repair_official_thread_tools_exposure(
    os.environ["CODEX_HOME_TARGET"],
    acknowledge_codex_running_risk=False,
    create_backup=True,
)
print(json.dumps(result, ensure_ascii=False))
'@
    $env:CODEX_HOME_TARGET = $CodexHome
    $repairOutput = $pythonSource | & $pythonExecutable -
    if ($LASTEXITCODE -ne 0) {
        throw "Official thread tools repair exited with code $LASTEXITCODE"
    }
    $repairResult = $repairOutput | ConvertFrom-Json
    Write-RepairStatus -State "complete" -Details @{
        changed = [bool]$repairResult.changed
        backupId = $repairResult.backup.backupId
        databaseBackupPath = $repairResult.backup.databaseBackupPath
        normalizedThreadCount = [int]$repairResult.normalizedThreadToolPositions.normalizedThreadCount
        normalizedRowCount = [int]$repairResult.normalizedThreadToolPositions.normalizedRowCount
        remainingPositionIssues = [int]$repairResult.after.threadToolRegistry.threadsWithNonContiguousPositions
    }
}
catch {
    Write-RepairStatus -State "failed" -Details @{
        error = $_.Exception.Message
        scriptStackTrace = $_.ScriptStackTrace
    }
}
finally {
    Start-Process -FilePath "explorer.exe" -ArgumentList "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
}
