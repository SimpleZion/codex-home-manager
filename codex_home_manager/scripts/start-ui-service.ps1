$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $PSScriptRoot
$healthUrl = "http://127.0.0.1:8765/api/health"
$appUrl = "http://127.0.0.1:8765/"
$serverLogPath = Join-Path $appDirectory "server.log"
$serverErrorLogPath = Join-Path $appDirectory "server.err.log"

function Test-ServiceHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    }
    catch {
        return $false
    }
}

function Start-AppBrowser {
    Start-Process $appUrl
}

if (Test-ServiceHealth) {
    Start-AppBrowser
    exit 0
}

Push-Location $appDirectory
try {
    if (-not (Test-Path "node_modules")) {
        npm install
    }

    if (-not (Test-Path "dist\index.html")) {
        npm run build
    }
}
finally {
    Pop-Location
}

if (Test-Path $serverLogPath) {
    Clear-Content -LiteralPath $serverLogPath
}
if (Test-Path $serverErrorLogPath) {
    Clear-Content -LiteralPath $serverErrorLogPath
}

Start-Process `
    -WindowStyle Hidden `
    -FilePath "python" `
    -ArgumentList @("-m", "backend.server") `
    -WorkingDirectory $appDirectory `
    -RedirectStandardOutput $serverLogPath `
    -RedirectStandardError $serverErrorLogPath | Out-Null

$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    if (Test-ServiceHealth) {
        Start-AppBrowser
        exit 0
    }
    Start-Sleep -Milliseconds 500
}

Write-Error "Codex Home Manager did not become healthy on $healthUrl. Check $serverErrorLogPath and $serverLogPath."
