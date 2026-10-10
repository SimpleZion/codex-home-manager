[CmdletBinding()]
param(
    [string]$ExecutablePath = "",
    [int]$TimeoutSeconds = 10,
    [switch]$ProbeLegacy
)

$ErrorActionPreference = "Stop"
if (-not $ExecutablePath) {
    $ExecutablePath = Join-Path (Split-Path -Parent $PSScriptRoot) "src-tauri\target\release\codex-home-manager-service.exe"
}
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
$shutdownToken = [Guid]::NewGuid().ToString("N")
$previousToken = $env:CODEX_HOME_MANAGER_DESKTOP_SHUTDOWN_TOKEN
$env:CODEX_HOME_MANAGER_DESKTOP_SHUTDOWN_TOKEN = $shutdownToken
$watch = [System.Diagnostics.Stopwatch]::StartNew()
$process = Start-Process -FilePath $resolvedExecutable -WindowStyle Hidden -PassThru
$apiReadyMs = $null
$legacyReadyMs = $null
$legacyThreadCount = $null
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)

try {
    do {
        Start-Sleep -Milliseconds 20
        try {
            $response = Invoke-RestMethod "http://127.0.0.1:8765/api/capabilities" -TimeoutSec 1
            if ($response.service -eq "codex-home-manager" -and $response.runtime.core -eq "rust") {
                $apiReadyMs = $watch.ElapsedMilliseconds
                break
            }
        }
        catch {
            # The native listener is still starting.
        }
    } while ((Get-Date) -lt $deadline)

    if ($ProbeLegacy -and $null -ne $apiReadyMs) {
        $legacyResponse = Invoke-RestMethod `
            "http://127.0.0.1:8765/api/threads/page?limit=1&thread_kind=main" `
            -TimeoutSec 30
        $legacyReadyMs = $watch.ElapsedMilliseconds
        $legacyThreadCount = @($legacyResponse.threads).Count
    }

    [ordered]@{
        processId = $process.Id
        apiReadyMs = $apiReadyMs
        legacyReadyMs = $legacyReadyMs
        legacyThreadCount = $legacyThreadCount
        completed = [bool]($null -ne $apiReadyMs)
    } | ConvertTo-Json
}
finally {
    try {
        Invoke-RestMethod `
            "http://127.0.0.1:8765/api/desktop/shutdown" `
            -Method Post `
            -Headers @{ "X-Codex-Home-Manager-Shutdown" = $shutdownToken } `
            -TimeoutSec 2 | Out-Null
    }
    catch {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    if ($null -eq $previousToken) {
        Remove-Item Env:CODEX_HOME_MANAGER_DESKTOP_SHUTDOWN_TOKEN -ErrorAction SilentlyContinue
    }
    else {
        $env:CODEX_HOME_MANAGER_DESKTOP_SHUTDOWN_TOKEN = $previousToken
    }
}

if ($null -eq $apiReadyMs) {
    exit 1
}
