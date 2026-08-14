param(
    [Parameter(Mandatory = $true)]
    [string]$StatusPath,

    [string]$CodexHome = "D:\.codex",

    [string]$BackupRoot = "D:\Backup\codex_home_manager",

    [int]$InitialDelaySeconds = 5
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$repairScript = Join-Path $PSScriptRoot "repair-official-thread-tool-session-meta.py"
$statusDirectory = Split-Path -Parent $StatusPath
New-Item -ItemType Directory -Path $statusDirectory -Force | Out-Null

function Write-RunnerFailure {
    param([string]$Message)
    $failure = @{
        state = "runner_failed"
        failedAt = [DateTimeOffset]::Now.ToString("o")
        error = $Message
    }
    $failure | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $StatusPath -Encoding utf8
}

function Get-CodexRuntimeProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -ieq "ChatGPT.exe" -or
        $_.Name -ieq "node_repl.exe" -or
        $_.Name -ieq "codex-code-mode-host.exe" -or
        $_.Name -ieq "codex.exe" -or
        ($_.Name -ieq "node.exe" -and $_.CommandLine -match "mcp[\\/]server\.(mjs|cjs|bundle\.mjs)") -or
        ($_.CommandLine -match "xcodebuildmcp@latest\s+mcp")
    })
}

function Stop-CodexRuntimeProcesses {
    $fastProcesses = @(Get-Process -Name ChatGPT, codex, node_repl, codex-code-mode-host -ErrorAction SilentlyContinue)
    foreach ($process in ($fastProcesses | Sort-Object Id -Descending)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    foreach ($process in (Get-CodexRuntimeProcesses | Where-Object {
        $_.Name -ieq "node.exe" -or $_.CommandLine -match "xcodebuildmcp@latest\s+mcp"
    } | Sort-Object ProcessId -Descending)) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

try {
    Start-Sleep -Seconds $InitialDelaySeconds
    Stop-CodexRuntimeProcesses

    $deadline = [DateTimeOffset]::Now.AddSeconds(60)
    $stableSince = $null
    do {
        Start-Sleep -Milliseconds 50
        Stop-CodexRuntimeProcesses
        $remainingProcesses = Get-CodexRuntimeProcesses
        if ($remainingProcesses.Count -eq 0) {
            if ($null -eq $stableSince) {
                $stableSince = [DateTimeOffset]::Now
            }
        } else {
            $stableSince = $null
        }
    } while (
        ($null -eq $stableSince -or ([DateTimeOffset]::Now - $stableSince).TotalSeconds -lt 2) -and
        [DateTimeOffset]::Now -lt $deadline
    )

    $remainingProcesses = Get-CodexRuntimeProcesses
    if ($remainingProcesses.Count -gt 0) {
        throw "Codex runtime processes did not stop: $($remainingProcesses.ProcessId -join ', ')"
    }

    $pythonExecutable = (Get-Command python -ErrorAction Stop).Source
    $env:PYTHONPATH = $projectRoot
    $stdoutPath = $StatusPath + ".stdout.log"
    $stderrPath = $StatusPath + ".stderr.log"
    $repairProcess = Start-Process -FilePath $pythonExecutable -ArgumentList @(
        "-B",
        $repairScript,
        "--codex-home", $CodexHome,
        "--backup-root", $BackupRoot,
        "--status-path", $StatusPath,
        "--external-process-guard-active"
    ) -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

    while (-not $repairProcess.HasExited) {
        Stop-CodexRuntimeProcesses
        Start-Sleep -Milliseconds 50
        $repairProcess.Refresh()
    }
    if ($repairProcess.ExitCode -ne 0) {
        $stderrText = if (Test-Path -LiteralPath $stderrPath) {
            (Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue).Trim()
        } else {
            ""
        }
        throw "Session metadata repair exited with code $($repairProcess.ExitCode): $stderrText"
    }

    Start-Process -FilePath "explorer.exe" -ArgumentList "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
}
catch {
    if (-not (Test-Path -LiteralPath $StatusPath)) {
        Write-RunnerFailure -Message $_.Exception.Message
    }
    exit 1
}
