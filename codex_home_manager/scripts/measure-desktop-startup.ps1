[CmdletBinding()]
param(
    [string]$ExecutablePath = "$env:LOCALAPPDATA\Codex Home Manager\codex-home-manager-desktop.exe",
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw "Desktop executable is missing: $ExecutablePath"
}

$watch = [System.Diagnostics.Stopwatch]::StartNew()
$started = Start-Process -FilePath $ExecutablePath -PassThru
$windowReadyMs = $null
$apiReadyMs = $null
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)

do {
    Start-Sleep -Milliseconds 100
    if ($null -eq $windowReadyMs) {
        $process = Get-Process -Id $started.Id -ErrorAction SilentlyContinue
        if ($process -and $process.MainWindowHandle -ne 0) {
            $windowReadyMs = $watch.ElapsedMilliseconds
        }
    }
    if ($null -eq $apiReadyMs) {
        try {
            $response = Invoke-RestMethod "http://127.0.0.1:8765/api/capabilities" -TimeoutSec 1
            if ($response.service -eq "codex-home-manager") {
                $apiReadyMs = $watch.ElapsedMilliseconds
            }
        }
        catch {
            # The bundled service is still starting.
        }
    }
    if ($null -ne $windowReadyMs -and $null -ne $apiReadyMs) {
        break
    }
} while ((Get-Date) -lt $deadline)

$watch.Stop()
[ordered]@{
    desktopPid = $started.Id
    windowReadyMs = $windowReadyMs
    apiReadyMs = $apiReadyMs
    completed = [bool]($null -ne $windowReadyMs -and $null -ne $apiReadyMs)
} | ConvertTo-Json

if ($null -eq $windowReadyMs -or $null -eq $apiReadyMs) {
    exit 1
}
