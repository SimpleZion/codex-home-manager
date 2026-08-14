$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    if (-not (Test-Path "node_modules")) {
        npm install
    }
    npm run build
    python -m backend.server
}
finally {
    Pop-Location
}

