$ErrorActionPreference = "Stop"

$port = 8765
$connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $connections) {
    Write-Output "Codex Home Manager is not running on port $port."
    exit 0
}

$stoppedAny = $false
foreach ($connection in $connections) {
    $processId = [int]$connection.OwningProcess
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $processInfo) {
        continue
    }

    $commandLine = [string]$processInfo.CommandLine
    $executablePath = [string]$processInfo.ExecutablePath
    $isPythonManagerServer = $processInfo.Name -match "python" -and $commandLine -match "backend\.server"
    $isPackagedManagerServer = $processInfo.Name -match "^codex-home-manager-local" -or $executablePath -match "codex_home_manager\\build\\releases\\codex-home-manager-local"
    $isManagerServer = $isPythonManagerServer -or $isPackagedManagerServer
    if (-not $isManagerServer) {
        Write-Warning "Port $port is owned by $($processInfo.Name) pid $processId, not the Codex Home Manager backend. It was not stopped."
        continue
    }

    Stop-Process -Id $processId -Force
    Write-Output "Stopped Codex Home Manager backend pid $processId."
    $stoppedAny = $true
}

if (-not $stoppedAny) {
    Write-Warning "No Codex Home Manager backend process was stopped."
}
