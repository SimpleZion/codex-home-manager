param([int]$InitialDelaySeconds = 8, [int]$ObservationSeconds = 90)

$ErrorActionPreference = "Stop"
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$artifactRoot = Join-Path $workspaceRoot "artifacts\appserver-shield-activation\$timestamp"
$resultPath = Join-Path $artifactRoot "result.json"
$transcriptPath = Join-Path $artifactRoot "runner.log"
$result = [ordered]@{schemaVersion=1;status="started";startedAt=(Get-Date).ToUniversalTime().ToString("o");artifactRoot=$artifactRoot}

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
    foreach ($process in @($processes | Where-Object { $descendants.Contains([int]$_.ProcessId) } | Sort-Object { if ($_.Name -ieq "ChatGPT.exe") { 1 } else { 0 } })) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
    foreach ($process in @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -ieq "ChatGPT.exe" -or
        $_.Name -ieq "codex-appserver-shield.exe" -or
        ($_.Name -ieq "codex.exe" -and [string]$_.CommandLine -match "app-server") -or
        $_.Name -ieq "node_repl.exe" -or
        $_.Name -ieq "codex-code-mode-host.exe" -or
        $_.Name -ieq "codex-command-runner.exe"
    })) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $blockers = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -ieq "ChatGPT.exe" -or
        $_.Name -ieq "codex-appserver-shield.exe" -or
        ($_.Name -ieq "codex.exe" -and [string]$_.CommandLine -match "app-server") -or
        $_.Name -ieq "node_repl.exe" -or
        $_.Name -ieq "codex-code-mode-host.exe" -or
        $_.Name -ieq "codex-command-runner.exe"
    })
    if ($blockers.Count -gt 0) { throw "Codex did not become fully offline. Blocking PIDs: $($blockers.ProcessId -join ', ')" }
}

function Sync-UserCodexEnvironment {
    foreach ($name in @("CODEX_HOME", "CODEX_APP_SERVER_REAL_EXE", "CODEX_CLI_PATH")) {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if ($null -eq $value) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue } else { Set-Item "Env:$name" $value }
    }
}

function Start-CodexDesktop {
    Sync-UserCodexEnvironment
    $package = Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1
    $executable = Join-Path $package.InstallLocation "app\ChatGPT.exe"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Codex Desktop executable is missing: $executable" }
    Start-Process -FilePath $executable
}

function Get-ProtectedProcessSample {
    $processes = @(Get-CimInstance Win32_Process)
    $main = @($processes | Where-Object { $_.Name -ieq "ChatGPT.exe" -and [string]$_.CommandLine -notmatch "--type=" })
    $shield = @($processes | Where-Object { $_.Name -ieq "codex-appserver-shield.exe" -and [string]$_.CommandLine -match "app-server" })
    $real = @($processes | Where-Object { $_.Name -ieq "codex.exe" -and [string]$_.CommandLine -match "app-server" -and $_.ParentProcessId -in @($shield.ProcessId) })
    return [ordered]@{
        at = (Get-Date).ToUniversalTime().ToString("o")
        mainPids = @($main.ProcessId)
        shieldPids = @($shield.ProcessId)
        realPids = @($real.ProcessId)
        healthy = ($main.Count -gt 0 -and $shield.Count -eq 1 -and $real.Count -eq 1)
    }
}

try {
    Save-Result
    Start-Sleep -Seconds $InitialDelaySeconds
    $result.userEnvironment = [ordered]@{
        CODEX_HOME = [Environment]::GetEnvironmentVariable("CODEX_HOME", "User")
        CODEX_APP_SERVER_REAL_EXE = [Environment]::GetEnvironmentVariable("CODEX_APP_SERVER_REAL_EXE", "User")
        CODEX_CLI_PATH = [Environment]::GetEnvironmentVariable("CODEX_CLI_PATH", "User")
    }
    $shieldPath = [string]$result.userEnvironment.CODEX_CLI_PATH
    $realPath = [string]$result.userEnvironment.CODEX_APP_SERVER_REAL_EXE
    if (-not (Test-Path -LiteralPath $shieldPath -PathType Leaf)) { throw "Stable shield is missing: $shieldPath" }
    if (-not (Test-Path -LiteralPath $realPath -PathType Leaf)) { throw "Managed real Codex is missing: $realPath" }
    $result.shieldSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $shieldPath).Hash.ToLowerInvariant()
    $result.realSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $realPath).Hash.ToLowerInvariant()
    if ($result.shieldSha256 -eq $result.realSha256) { throw "Shield and managed real Codex unexpectedly have the same hash." }
    $automationPaths = @("D:\.codex\automations\miracleplus\automation.toml", "D:\.codex\automations\resume-factor-evaluations\automation.toml")
    $result.automationStatusBefore = @($automationPaths | ForEach-Object {[ordered]@{path=$_;status=((Select-String -LiteralPath $_ -Pattern '^status\s*=\s*"([^"]+)"').Matches.Groups[1].Value)}})
    Stop-CodexDesktopTree
    $result.codexOfflineAt = (Get-Date).ToUniversalTime().ToString("o")
    Save-Result
    Start-CodexDesktop

    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 2
        $sample = Get-ProtectedProcessSample
    } while (-not $sample.healthy -and (Get-Date) -lt $deadline)
    if (-not $sample.healthy) { throw "Codex did not start through the stable shield within 120 seconds." }
    $result.firstHealthySample = $sample
    $samples = New-Object System.Collections.ArrayList
    $observationDeadline = (Get-Date).AddSeconds($ObservationSeconds)
    do {
        Start-Sleep -Seconds 5
        $sample = Get-ProtectedProcessSample
        $null = $samples.Add($sample)
        if (-not $sample.healthy) { throw "Protected app-server process chain became unhealthy during observation." }
    } while ((Get-Date) -lt $observationDeadline)
    $result.samples = @($samples)
    $result.finalSample = Get-ProtectedProcessSample
    $result.automationStatusAfter = @($automationPaths | ForEach-Object {[ordered]@{path=$_;status=((Select-String -LiteralPath $_ -Pattern '^status\s*=\s*"([^"]+)"').Matches.Groups[1].Value)}})
    if (@($result.automationStatusAfter | Where-Object { $_.status -ne "PAUSED" }).Count -gt 0) { throw "A manually paused automation changed state during activation." }
    $result.status = "activated_and_observed"
    $result.completedAt = (Get-Date).ToUniversalTime().ToString("o")
    Save-Result
} catch {
    $result.status = "failed"
    $result.error = $_.Exception.ToString()
    Save-Result
    try { Start-CodexDesktop } catch { $result.restartError = $_.Exception.ToString(); Save-Result }
} finally {
    try { Stop-Transcript | Out-Null } catch {}
}
