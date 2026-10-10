param(
    [ValidateSet("Preview", "Apply", "Rollback")][string]$Mode = "Preview",
    [string]$CodexHome = "D:\.codex",
    [string]$BackupRoot = "D:\Backup\codex_home_manager\appserver-shield",
    [string]$RollbackBackup = ""
)

$ErrorActionPreference = "Stop"

function Get-Sha256([string]$Path) { return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }

function Invoke-ShieldSelfTest([string]$Path) {
    $output = (& $Path --codex-appserver-shield-self-test 2>&1 | Out-String).Trim()
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "The shield Ctrl+C isolation self-test failed: $output" }
    return $output
}

function Invoke-ShieldVersion([string]$ShieldPath, [string]$RealPath, [string]$HomePath) {
    $previousReal = $env:CODEX_APP_SERVER_REAL_EXE
    $previousHome = $env:CODEX_HOME
    $previousActive = $env:CODEX_APP_SERVER_SHIELD_ACTIVE
    try {
        $env:CODEX_APP_SERVER_REAL_EXE = $RealPath
        $env:CODEX_HOME = $HomePath
        Remove-Item Env:CODEX_APP_SERVER_SHIELD_ACTIVE -ErrorAction SilentlyContinue
        $output = (& $ShieldPath --version 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw "The shield version probe failed: $output" }
        return $output
    } finally {
        if ($null -eq $previousReal) { Remove-Item Env:CODEX_APP_SERVER_REAL_EXE -ErrorAction SilentlyContinue } else { $env:CODEX_APP_SERVER_REAL_EXE = $previousReal }
        if ($null -eq $previousHome) { Remove-Item Env:CODEX_HOME -ErrorAction SilentlyContinue } else { $env:CODEX_HOME = $previousHome }
        if ($null -eq $previousActive) { Remove-Item Env:CODEX_APP_SERVER_SHIELD_ACTIVE -ErrorAction SilentlyContinue } else { $env:CODEX_APP_SERVER_SHIELD_ACTIVE = $previousActive }
    }
}

function Get-EnvironmentSnapshot {
    return [ordered]@{
        CODEX_CLI_PATH = [Environment]::GetEnvironmentVariable("CODEX_CLI_PATH", "User")
        CODEX_APP_SERVER_REAL_EXE = [Environment]::GetEnvironmentVariable("CODEX_APP_SERVER_REAL_EXE", "User")
        CODEX_HOME = [Environment]::GetEnvironmentVariable("CODEX_HOME", "User")
    }
}

function Set-UserEnvironment([string]$Name, $Value) { [Environment]::SetEnvironmentVariable($Name, $Value, "User") }

function Broadcast-EnvironmentChange {
    if (-not ("EnvironmentBroadcast" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class EnvironmentBroadcast {
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);
}
"@
    }
    $result = [UIntPtr]::Zero
    $null = [EnvironmentBroadcast]::SendMessageTimeout([IntPtr]0xffff, 0x001a, [UIntPtr]::Zero, "Environment", 0x0002, 5000, [ref]$result)
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$builtShieldPath = Join-Path $workspaceRoot "tools\codex-appserver-shield\target\release\codex-appserver-shield.exe"
$managedRealPath = Join-Path $CodexHome "plugins\.plugin-appserver\codex.exe"
$stableDirectory = Join-Path $CodexHome "bin\codex-appserver-shield"
$stableShieldPath = Join-Path $stableDirectory "codex-appserver-shield.exe"
$manifestPath = Join-Path $stableDirectory "manifest.json"
$legacyRealPath = Join-Path $CodexHome "plugins\.plugin-appserver\codex-appserver-real.exe"
$legacyManifestPath = Join-Path $CodexHome "plugins\.plugin-appserver\codex-appserver-shield.json"

if (-not (Test-Path -LiteralPath $builtShieldPath -PathType Leaf)) { throw "Build the shield first: cargo build --release --manifest-path tools\codex-appserver-shield\Cargo.toml" }
if (-not (Test-Path -LiteralPath $managedRealPath -PathType Leaf)) { throw "The Desktop-managed Codex executable is missing: $managedRealPath" }

$selfTestOutput = Invoke-ShieldSelfTest $builtShieldPath
$realVersion = (& $managedRealPath --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "The Desktop-managed Codex executable failed its version probe." }
$forwardedVersion = Invoke-ShieldVersion $builtShieldPath $managedRealPath $CodexHome
if ($forwardedVersion -ne $realVersion) { throw "The shield did not preserve Codex --version output." }

$preview = [ordered]@{
    mode = $Mode
    architecture = "stable-launcher"
    codexHome = $CodexHome
    managedRealPath = $managedRealPath
    managedRealSha256 = Get-Sha256 $managedRealPath
    stableShieldPath = $stableShieldPath
    currentStableShieldSha256 = if (Test-Path -LiteralPath $stableShieldPath) { Get-Sha256 $stableShieldPath } else { $null }
    builtShieldSha256 = Get-Sha256 $builtShieldPath
    forwardedVersion = $forwardedVersion
    selfTest = $selfTestOutput
    currentUserEnvironment = Get-EnvironmentSnapshot
    restartRequired = $true
}

if ($Mode -eq "Preview") { $preview | ConvertTo-Json -Depth 5; exit 0 }

if ($Mode -eq "Rollback") {
    if (-not $RollbackBackup) { throw "Rollback requires -RollbackBackup with a verified backup directory." }
    $backupDirectory = [System.IO.Path]::GetFullPath($RollbackBackup)
    $requiredRoot = [System.IO.Path]::GetFullPath($BackupRoot).TrimEnd('\') + '\'
    if (-not $backupDirectory.StartsWith($requiredRoot, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Rollback backup must remain under $BackupRoot" }
    $environmentPath = Join-Path $backupDirectory "user-environment.json"
    if (-not (Test-Path -LiteralPath $environmentPath -PathType Leaf)) { throw "Rollback environment snapshot is missing: $environmentPath" }
    $environment = Get-Content -LiteralPath $environmentPath -Raw | ConvertFrom-Json
    Set-UserEnvironment "CODEX_CLI_PATH" $environment.CODEX_CLI_PATH
    Set-UserEnvironment "CODEX_APP_SERVER_REAL_EXE" $environment.CODEX_APP_SERVER_REAL_EXE
    Set-UserEnvironment "CODEX_HOME" $environment.CODEX_HOME
    Broadcast-EnvironmentChange
    [ordered]@{status="rolled_back";backup=$backupDirectory;restoredUserEnvironment=Get-EnvironmentSnapshot;restartRequired=$true} | ConvertTo-Json -Depth 5
    exit 0
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDirectory = Join-Path $BackupRoot $timestamp
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
$environmentBefore = Get-EnvironmentSnapshot
$environmentBefore | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $backupDirectory "user-environment.json") -Encoding utf8
if (Test-Path -LiteralPath $stableShieldPath) { Copy-Item -LiteralPath $stableShieldPath -Destination (Join-Path $backupDirectory "codex-appserver-shield.exe") -Force }
if (Test-Path -LiteralPath $manifestPath) { Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $backupDirectory "manifest.json") -Force }
if (Test-Path -LiteralPath $legacyRealPath) { Move-Item -LiteralPath $legacyRealPath -Destination (Join-Path $backupDirectory "legacy-codex-appserver-real.exe") -Force }
if (Test-Path -LiteralPath $legacyManifestPath) { Move-Item -LiteralPath $legacyManifestPath -Destination (Join-Path $backupDirectory "legacy-codex-appserver-shield.json") -Force }

New-Item -ItemType Directory -Path $stableDirectory -Force | Out-Null
Copy-Item -LiteralPath $builtShieldPath -Destination $stableShieldPath -Force
$installedShieldSha256 = Get-Sha256 $stableShieldPath
if ($installedShieldSha256 -ne (Get-Sha256 $builtShieldPath)) { throw "Installed shield hash differs from the verified build." }
$installedVersion = Invoke-ShieldVersion $stableShieldPath $managedRealPath $CodexHome
if ($installedVersion -ne $realVersion) { throw "Installed shield failed the final Codex version probe." }

$manifest = [ordered]@{
    schemaVersion = 2
    architecture = "stable-launcher"
    installedAt = (Get-Date).ToUniversalTime().ToString("o")
    codexHome = $CodexHome
    managedRealPath = $managedRealPath
    managedRealSha256AtInstall = Get-Sha256 $managedRealPath
    stableShieldPath = $stableShieldPath
    shieldSha256 = $installedShieldSha256
    codexVersionAtInstall = $installedVersion
    environmentBefore = $environmentBefore
    backupDirectory = $backupDirectory
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding utf8

Set-UserEnvironment "CODEX_HOME" $CodexHome
Set-UserEnvironment "CODEX_APP_SERVER_REAL_EXE" $managedRealPath
Set-UserEnvironment "CODEX_CLI_PATH" $stableShieldPath
Broadcast-EnvironmentChange
$environmentAfter = Get-EnvironmentSnapshot
if ($environmentAfter.CODEX_CLI_PATH -ne $stableShieldPath -or $environmentAfter.CODEX_APP_SERVER_REAL_EXE -ne $managedRealPath) { throw "User environment did not persist the stable launcher paths." }

[ordered]@{
    status = "installed_pending_restart"
    manifest = $manifestPath
    backup = $backupDirectory
    shieldPath = $stableShieldPath
    shieldSha256 = $installedShieldSha256
    managedRealPath = $managedRealPath
    managedRealSha256 = Get-Sha256 $managedRealPath
    codexVersion = $installedVersion
    userEnvironment = $environmentAfter
    restartRequired = $true
} | ConvertTo-Json -Depth 6
