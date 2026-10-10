$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    if (-not (Test-Path "node_modules")) {
        npm ci --cache "D:\ZFiles\CXProject\Codex\cache\npm" --prefer-offline --no-audit --no-fund
    }
    npm run build
    python -B -m backend.server
}
finally {
    Pop-Location
}
