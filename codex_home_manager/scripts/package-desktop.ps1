[CmdletBinding()]
param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $PSScriptRoot
if (-not $env:CODEX_STORAGE_RUN_ROOT -or -not $env:CARGO_TARGET_DIR -or -not $env:CODEX_HOME_MANAGER_RELEASE_ROOT) {
    throw "Desktop packaging must run through scripts/build_home_manager_guarded.py so storage admission is enforced"
}
$cargoTargetDirectory = [IO.Path]::GetFullPath($env:CARGO_TARGET_DIR)
$bundleDirectory = Join-Path $cargoTargetDirectory "release\bundle\nsis"
$releaseDirectory = [IO.Path]::GetFullPath($env:CODEX_HOME_MANAGER_RELEASE_ROOT)
$releaseInstaller = Join-Path $releaseDirectory "Codex-Cockpit-Setup-x64.exe"
$checksumPath = Join-Path $releaseDirectory "Codex-Cockpit-Setup-x64.sha256"
$receiptPath = Join-Path $releaseDirectory "codex-cockpit-release-receipt.json"
$preparedReceiptPath = Join-Path $releaseDirectory "codex-cockpit-prepared-resources.json"
$configuration = Get-Content -LiteralPath (Join-Path $appDirectory "src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json
$catalogPath = Join-Path $appDirectory "packaging\runtime\cockpit-maintenance.json"
$bundledCatalogPath = Join-Path $appDirectory "src-tauri\resources\legacy\_internal\packaging\runtime\cockpit-maintenance.json"
$frontendManifestPath = Join-Path $appDirectory "build\frontend-release\frontend-release-manifest.json"
$frontendArtifactScript = Join-Path $appDirectory "scripts\frontend-release-artifact.mjs"
$legacyBinaryPath = Join-Path $appDirectory "src-tauri\resources\legacy\codex-home-manager-legacy.exe"
if ($configuration.productName -ne "Codex Cockpit") { throw "Unexpected desktop product identity" }
$expectedName = "Codex Cockpit_$($configuration.version)_x64-setup.exe"
$expectedInstallerPath = Join-Path $bundleDirectory $expectedName

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $algorithm.ComputeHash($stream)
        return ([System.BitConverter]::ToString($bytes) -replace "-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Invoke-ArtifactContract {
    param([string]$Mode, [string]$Scope = "")

    $arguments = @($frontendArtifactScript, $Mode)
    if ($Scope) { $arguments += $Scope }
    $digest = & node.exe @arguments
    if ($LASTEXITCODE -ne 0 -or $digest -isnot [string] -or $digest -notmatch '^[a-f0-9]{64}$') { throw "Artifact contract failed: $Mode $Scope" }
    return $digest
}

function Assert-PreparedResourceReceipt {
    param($Receipt, [string]$Version, [string]$RunRoot, [string]$SourceFingerprint, [string]$ResourcesFingerprint)

    $preparedAt = [DateTimeOffset]::MinValue
    if ($Receipt.schemaVersion -ne 1 -or $Receipt.stage -ne 'resources-prepared' -or
        $Receipt.productName -ne 'Codex Cockpit' -or $Receipt.version -ne $Version -or
        $Receipt.runRoot -ne $RunRoot -or $Receipt.sourceFingerprint -ne $SourceFingerprint -or
        $Receipt.resourcesFingerprint -ne $ResourcesFingerprint -or
        $Receipt.preparedAt -isnot [string] -or -not [DateTimeOffset]::TryParse($Receipt.preparedAt, [ref]$preparedAt)) {
        throw 'Prepared resources no longer match this run and its exact source/resource closure'
    }
}

$buildStartedAt = [DateTime]::UtcNow
$sourceFingerprint = Invoke-ArtifactContract -Mode source-digest -Scope desktop
$builtAt = $buildStartedAt.ToString("o")
if ($SkipBuild) {
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { throw "SkipBuild requires a prior verified Cockpit build receipt" }
    $previous = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $parsedBuiltAt = [DateTimeOffset]::MinValue
    if ($previous.schemaVersion -ne 2 -or $previous.builtAt -isnot [string] -or -not [DateTimeOffset]::TryParse($previous.builtAt, [ref]$parsedBuiltAt)) {
        throw "SkipBuild requires a current source/resource-bound build receipt; rebuild without SkipBuild"
    }
    $builtAt = $previous.builtAt
}
$reusePreparedResources = -not $SkipBuild -and $env:CODEX_COCKPIT_RESUME -eq '1' -and
    (Test-Path -LiteralPath $preparedReceiptPath -PathType Leaf)
if ($reusePreparedResources) {
    $prepared = Get-Content -LiteralPath $preparedReceiptPath -Raw | ConvertFrom-Json
}
if (-not $SkipBuild -and -not $reusePreparedResources) {
    & node.exe $frontendArtifactScript verify 2>$null | Out-Null
    $reuseFrozenFrontend = $LASTEXITCODE -eq 0
    $sidecarArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $appDirectory 'scripts\prepare-desktop-sidecar.ps1'))
    if ($reuseFrozenFrontend) { $sidecarArguments += '-SkipFrontendBuild' }
    & powershell.exe @sidecarArguments
    if ($LASTEXITCODE -ne 0) { throw "Desktop sidecar build failed" }
}
Invoke-ArtifactContract -Mode verify | Out-Null
$resourcesFingerprint = Invoke-ArtifactContract -Mode desktop-artifacts
if (-not $SkipBuild) {
    if ((Invoke-ArtifactContract -Mode source-digest -Scope desktop) -ne $sourceFingerprint) { throw 'Source changed during resource preparation' }
    if ($reusePreparedResources) {
        Assert-PreparedResourceReceipt -Receipt $prepared -Version $configuration.version -RunRoot ([IO.Path]::GetFullPath($env:CODEX_STORAGE_RUN_ROOT)) -SourceFingerprint $sourceFingerprint -ResourcesFingerprint $resourcesFingerprint
        Write-Output 'Resuming desktop packaging from verified prepared resources'
    } else {
        if (Test-Path -LiteralPath $preparedReceiptPath) { throw 'Refusing to overwrite an existing resource preparation receipt' }
        New-Item -ItemType Directory -Force -Path $releaseDirectory | Out-Null
        $prepared = [ordered]@{
            schemaVersion = 1; stage = 'resources-prepared'; productName = 'Codex Cockpit'; version = $configuration.version
            runRoot = [IO.Path]::GetFullPath($env:CODEX_STORAGE_RUN_ROOT)
            sourceFingerprint = $sourceFingerprint; resourcesFingerprint = $resourcesFingerprint
            preparedAt = [DateTime]::UtcNow.ToString('o')
        }
        $temporaryReceipt = "$preparedReceiptPath.tmp"
        [IO.File]::WriteAllText($temporaryReceipt, ($prepared | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporaryReceipt -Destination $preparedReceiptPath -ErrorAction Stop
    }
    # Freeze the exact resource closure before the bundler consumes it.
    Push-Location $appDirectory
    try {
        & (Join-Path $appDirectory "node_modules\.bin\tauri.cmd") build
        if ($LASTEXITCODE -ne 0) { throw "Desktop application build failed" }
    }
    finally { Pop-Location }
}

$installer = Get-Item -LiteralPath $expectedInstallerPath -ErrorAction Stop
if (-not $SkipBuild -and $installer.LastWriteTimeUtc -lt $buildStartedAt.AddSeconds(-2)) { throw "The expected installer was not produced by this build" }
$catalogHash = Get-Sha256Hex -Path $catalogPath
if ((Get-Sha256Hex -Path $bundledCatalogPath) -ne $catalogHash) { throw "Packaged maintenance catalog differs from source" }
$frontendHash = Get-Sha256Hex -Path $frontendManifestPath
$legacyHash = Get-Sha256Hex -Path $legacyBinaryPath
$installerHash = Get-Sha256Hex -Path $installer.FullName
if ((Invoke-ArtifactContract -Mode source-digest -Scope desktop) -ne $sourceFingerprint) { throw "Source changed during the build; release not staged" }
if ((Invoke-ArtifactContract -Mode desktop-artifacts) -ne $resourcesFingerprint) { throw "Resource closure changed during the build; release not staged" }
if ($SkipBuild) {
    if ($previous.productName -ne "Codex Cockpit" -or $previous.version -ne $configuration.version -or
        $previous.installerSha256 -ne $installerHash -or $previous.catalogSha256 -ne $catalogHash -or
        $previous.frontendManifestSha256 -ne $frontendHash -or $previous.legacySha256 -ne $legacyHash -or
        $previous.sourceFingerprint -ne $sourceFingerprint -or $previous.resourcesFingerprint -ne $resourcesFingerprint) {
        throw "SkipBuild artifacts no longer match the verified build receipt"
    }
}

New-Item -ItemType Directory -Force -Path $releaseDirectory | Out-Null
Copy-Item -LiteralPath $installer.FullName -Destination $releaseInstaller -Force
$hash = Get-Sha256Hex -Path $releaseInstaller
if ($hash -ne $installerHash) { throw "Installer changed while staging the release" }
[System.IO.File]::WriteAllText($checksumPath, "$hash  Codex-Cockpit-Setup-x64.exe`n", [System.Text.UTF8Encoding]::new($false))
$receipt = [ordered]@{
    schemaVersion = 2
    productName = "Codex Cockpit"; version = $configuration.version; installerName = $expectedName
    installerSha256 = $hash; catalogSha256 = $catalogHash; frontendManifestSha256 = $frontendHash
    legacySha256 = $legacyHash; sourceFingerprint = $sourceFingerprint; resourcesFingerprint = $resourcesFingerprint
    builtAt = $builtAt; stagedAt = [DateTime]::UtcNow.ToString("o"); installed = $false; published = $false
}
[System.IO.File]::WriteAllText($receiptPath, ($receipt | ConvertTo-Json), [System.Text.UTF8Encoding]::new($false))

$file = Get-Item -LiteralPath $releaseInstaller
Write-Output "Desktop installer: $($file.FullName)"
Write-Output "Size: $($file.Length) bytes"
Write-Output "SHA256: $hash"
