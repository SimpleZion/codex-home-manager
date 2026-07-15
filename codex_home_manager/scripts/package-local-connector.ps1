param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$SourceEvidenceDirectory,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$SourceCommit,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string]$SourceEvidenceRepository,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SourceEvidenceSignerWorkflow
)

$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $PSScriptRoot
$rootRepository = Split-Path -Parent $appDirectory
$publicRepository = Join-Path $rootRepository "codex-home-manager-public"
$publicSiteRoot = Join-Path $publicRepository "site"
$buildRoot = Join-Path $appDirectory "build\local-connector"
$reproducibleBuildRoot = Join-Path $buildRoot "reproducible"
$releaseRoot = Join-Path $appDirectory "build\releases"
$venvRoot = Join-Path $buildRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$archivePath = Join-Path $releaseRoot "codex-home-manager-local-win-x64.zip"
$directExecutablePath = Join-Path $releaseRoot "codex-home-manager-local-win-x64.exe"
$checksumPath = Join-Path $releaseRoot "SHA256SUMS.txt"
$verifyScriptPath = Join-Path $releaseRoot "verify-codex-home-manager.ps1"
$releasePublicKeyPath = Join-Path $releaseRoot "release-signing-public-key.pem"
$privateKeyPath = "D:\Backup\codex_home_manager\release-signing\release-signing-key.pem"
$trustedPublicKeyFingerprintPath = "D:\Backup\codex_home_manager\release-signing\release-signing-public-key.sha256"
$releaseManifestScript = Join-Path $PSScriptRoot "release_manifest.py"
$buildSourceSnapshotPath = Join-Path $buildRoot "release-build-source.json"
$sourceEvidenceProofPath = Join-Path $buildRoot "source-release-evidence.json"
$publicDistSyncPlanPath = Join-Path $buildRoot "public-dist-sync-plan.json"
$generatedLauncherPath = Join-Path $buildRoot "connector_release_entry.py"
$pyinstallerRunnerPath = Join-Path $buildRoot "run_reproducible_pyinstaller.py"
$iconPath = Join-Path $appDirectory "packaging\windows\assets\codex-home-manager.ico"
$requirementsPath = Join-Path $appDirectory "packaging\windows\requirements-connector.txt"
$releaseVersion = (Get-Content -LiteralPath (Join-Path $appDirectory "package.json") -Raw | ConvertFrom-Json).version

if ($releaseVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "package.json version must be semantic x.y.z for public release naming: $releaseVersion"
}

function Remove-InternalPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolvedBuildRoot = [System.IO.Path]::GetFullPath($buildRoot)
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedBuildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside build root: $resolvedPath"
    }
    if (Test-Path -LiteralPath $resolvedPath) {
        Remove-Item -LiteralPath $resolvedPath -Recurse -Force
    }
}

function Get-Sha256HashText {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([System.BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Assert-PublicDistRelativePath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    if ($RelativePath -cne $RelativePath.Replace('\', '/')) {
        throw "Public dist paths must use canonical forward slashes: $RelativePath"
    }
    if ($RelativePath.Equals("favicon.svg", [System.StringComparison]::OrdinalIgnoreCase) -or
        $RelativePath.Equals("index.html", [System.StringComparison]::OrdinalIgnoreCase)) {
        if ($RelativePath -cnotin @("favicon.svg", "index.html")) {
            throw "Public dist root path has non-canonical casing: $RelativePath"
        }
        return
    }
    if ($RelativePath -cnotmatch '^assets/[A-Za-z0-9._-]+\.(css|js|wasm)$') {
        throw "Public dist path is outside the execution allowlist: $RelativePath"
    }
}

function Resolve-AllowedChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    if ([System.IO.Path]::IsPathRooted($RelativePath) -or $RelativePath -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "Refusing a rooted or traversing release path: $RelativePath"
    }
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $resolvedPath = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot ($RelativePath.Replace('/', '\'))))
    $rootPrefix = $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing a release path outside its allowed root: $resolvedPath"
    }
    return $resolvedPath
}

function Invoke-PublicSiteDistSync {
    param(
        [Parameter(Mandatory = $true)][string]$DistDirectory,
        [Parameter(Mandatory = $true)][string]$PublicSiteDirectory
    )
    & python $releaseManifestScript plan-public-dist-sync `
        --dist $DistDirectory `
        --public-site $PublicSiteDirectory `
        --output $publicDistSyncPlanPath
    if ($LASTEXITCODE -ne 0) {
        throw "Public site dist synchronization plan failed"
    }
    $syncPlan = Get-Content -LiteralPath $publicDistSyncPlanPath -Raw | ConvertFrom-Json
    if ($syncPlan.schema_version -ne 1) {
        throw "Public site dist synchronization plan has an unsupported schema"
    }
    $copyFiles = @($syncPlan.copy_files)
    $staleFiles = @($syncPlan.stale_files)
    if ($copyFiles.Count -lt 1) {
        throw "Public site dist synchronization plan has no files"
    }

    $copyPathSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    foreach ($record in $copyFiles) {
        $relativePath = [string]$record.path
        Assert-PublicDistRelativePath -RelativePath $relativePath
        if (-not $copyPathSet.Add($relativePath)) {
            throw "Public site dist synchronization plan has a duplicate copy path: $relativePath"
        }
        $sourcePath = Resolve-AllowedChildPath -Root $DistDirectory -RelativePath $relativePath
        $destinationPath = Resolve-AllowedChildPath -Root $PublicSiteDirectory -RelativePath $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Public site dist source disappeared during synchronization: $relativePath"
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
        [System.IO.File]::Copy($sourcePath, $destinationPath, $true)
        if ((Get-Item -LiteralPath $destinationPath).Length -ne [long]$record.size -or
            (Get-Sha256HashText -Path $destinationPath) -cne [string]$record.sha256) {
            throw "Public site dist copy verification failed: $relativePath"
        }
    }

    Add-Type -AssemblyName Microsoft.VisualBasic
    $resolvedAssetsRoot = [System.IO.Path]::GetFullPath((Join-Path $PublicSiteDirectory "assets")).TrimEnd('\')
    foreach ($relativePathValue in $staleFiles) {
        $relativePath = [string]$relativePathValue
        Assert-PublicDistRelativePath -RelativePath $relativePath
        if ($copyPathSet.Contains($relativePath)) {
            throw "Public site dist synchronization plan overlaps copy and stale paths: $relativePath"
        }
        $stalePath = Resolve-AllowedChildPath -Root $PublicSiteDirectory -RelativePath $relativePath
        if ([System.IO.Path]::GetFullPath((Split-Path -Parent $stalePath)).TrimEnd('\') -cne $resolvedAssetsRoot) {
            throw "Refusing to retire a stale frontend asset outside the public assets allowlist: $stalePath"
        }
        if (Test-Path -LiteralPath $stalePath -PathType Leaf) {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
                $stalePath,
                [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
            )
        }
    }

    & python $releaseManifestScript verify-public-dist `
        --dist $DistDirectory `
        --public-site $PublicSiteDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Public site dist verification failed after synchronization"
    }
}

function Assert-ReleaseDestinationAvailable {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        throw "Release destination is locked. Stop the verified Codex Home Manager connector before packaging: $Path"
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Stop-VerifiedReleaseDestinationProcesses {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.ExecutablePath -and
        [System.IO.Path]::GetFullPath($_.ExecutablePath) -ieq $resolvedPath
    })
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    }
    if ($processes.Count -gt 0) {
        Start-Sleep -Milliseconds 750
    }
    $remaining = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.ExecutablePath -and
        [System.IO.Path]::GetFullPath($_.ExecutablePath) -ieq $resolvedPath
    })
    if ($remaining.Count -gt 0) {
        throw "Verified old connector processes still hold the release destination: $($remaining.ProcessId -join ', ')"
    }
    Assert-ReleaseDestinationAvailable -Path $resolvedPath
}

function Get-ReleaseNodeToolchain {
    $candidateDirectories = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($directory in @(
        "C:\Program Files\nodejs",
        "C:\Program Files (x86)\nodejs"
    )) {
        [void]$candidateDirectories.Add($directory)
    }
    foreach ($command in @(Get-Command node -All -ErrorAction SilentlyContinue)) {
        if ($command.Source) {
            [void]$candidateDirectories.Add((Split-Path -Parent $command.Source))
        }
    }
    foreach ($directory in $candidateDirectories) {
        $nodePath = Join-Path $directory "node.exe"
        $npmPath = Join-Path $directory "npm.cmd"
        if (-not (Test-Path -LiteralPath $nodePath -PathType Leaf) -or
            -not (Test-Path -LiteralPath $npmPath -PathType Leaf)) {
            continue
        }
        $versionText = (& $nodePath --version 2>$null | Out-String).Trim().TrimStart("v")
        $version = $null
        if ([System.Version]::TryParse($versionText, [ref]$version) -and $version.Major -ge 22) {
            return [pscustomobject]@{
                Node = $nodePath
                Npm = $npmPath
                Directory = $directory
                Version = $version.ToString()
            }
        }
    }
    throw "Public release requires a complete Node.js 22 or newer installation with npm.cmd"
}

function Get-ContentAddressedReleaseName {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("exe", "zip")][string]$Extension,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$Sha256
    )
    return "codex-home-manager-local-win-x64-v$releaseVersion-$($Sha256.Substring(0, 12)).$Extension"
}

function Assert-ReleaseZipBoundary {
    param([Parameter(Mandatory = $true)][string]$Path)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $blockedExtensions = @(".c", ".cc", ".cpp", ".cs", ".h", ".hpp", ".map", ".pdb", ".py", ".pyc", ".pyo", ".rs", ".ts", ".tsx")
        $blockedEntries = @($archive.Entries | Where-Object {
            $entryName = $_.FullName.Replace("\", "/").ToLowerInvariant()
            $extension = [System.IO.Path]::GetExtension($entryName)
            $entryName.StartsWith("backend/") -or $entryName.Contains("/backend/") -or $blockedExtensions -contains $extension
        } | ForEach-Object FullName)
        if ($blockedEntries.Count -gt 0) {
            throw "Release ZIP contains backend, source, source map, or debug entries: $($blockedEntries -join ', ')"
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Assert-PyInstallerExecutableBoundary {
    param([Parameter(Mandatory = $true)][string]$Path)
    $archiveViewer = Join-Path $venvRoot "Scripts\pyi-archive_viewer.exe"
    if (-not (Test-Path -LiteralPath $archiveViewer -PathType Leaf)) {
        throw "pyi-archive_viewer was not installed in the locked packaging environment"
    }
    $archiveEntries = @(& $archiveViewer --recursive --brief $Path 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "pyi-archive_viewer failed for $Path"
    }
    $sourceFiles = @($archiveEntries | ForEach-Object { $_.Trim() } | Where-Object {
        $_ -match '(?i)(?:[/\\][^/\\]+|^[^./\\]+)\.(c|cc|cpp|cs|h|hpp|map|pdb|py|pyc|pyo|rs|ts|tsx)$'
    })
    $executableText = [System.Text.Encoding]::ASCII.GetString([System.IO.File]::ReadAllBytes($Path))
    $sensitiveMarkers = @(
        $appDirectory,
        "packaging\windows\connector_main.py",
        "backend\server.py",
        "release-signing-key.pem",
        "CODEX_HOME_MANAGER_WRITE_TOKEN"
    )
    $sensitiveStrings = @($sensitiveMarkers | Where-Object { $executableText.Contains($_) })
    if ($sourceFiles.Count -gt 0 -or $sensitiveStrings.Count -gt 0) {
        throw "PyInstaller EXE exposes source/debug entries or sensitive implementation strings"
    }
    return [ordered]@{
        method = "pyi-archive-viewer+strings"
        archiveEntryCount = $archiveEntries.Count
        sourceFiles = @($sourceFiles)
        sensitiveStrings = @($sensitiveStrings)
    }
}

function Get-RandomLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try {
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Write-ConnectorReleaseLauncher {
    $launcher = @'
from __future__ import annotations

import os
import socket

import connector_main


connector_port = int(os.environ.get("CODEX_HOME_MANAGER_PORT", "8765"))
if connector_port < 1 or connector_port > 65535:
    raise RuntimeError("CODEX_HOME_MANAGER_PORT must be between 1 and 65535")

connector_main.local_console_url = f"http://127.0.0.1:{connector_port}/"
connector_main.connector_probe_url = f"http://127.0.0.1:{connector_port}/api/capabilities"


def configured_port_is_available() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", connector_port)) != 0


connector_main.port_is_available = configured_port_is_available
original_uvicorn_run = connector_main.uvicorn.run


def configured_uvicorn_run(*args, **kwargs):
    kwargs["host"] = "127.0.0.1"
    kwargs["port"] = connector_port
    return original_uvicorn_run(*args, **kwargs)


connector_main.uvicorn.run = configured_uvicorn_run
if os.environ.get("CODEX_HOME_MANAGER_NO_BROWSER") == "1":
    connector_main.open_local_console_after_start = lambda: None
    connector_main.webbrowser.open = lambda *_args, **_kwargs: False
if os.environ.get("CODEX_HOME_MANAGER_SKIP_PROTOCOL") == "1":
    connector_main.register_browser_protocol = lambda: None

connector_main.main()
'@
    [System.IO.File]::WriteAllText($generatedLauncherPath, $launcher, [System.Text.UTF8Encoding]::new($false))
}

function Write-PyInstallerReproducibilityRunner {
    $runner = @'
from __future__ import annotations

import sys

from PyInstaller.building import utils as building_utils


original_create_base_library_zip = building_utils.create_base_library_zip


def deterministic_create_base_library_zip(filename, modules_toc, code_cache=None):
    ordered_modules = sorted(modules_toc, key=lambda entry: (entry[0], entry[1] or "", entry[2]))
    return original_create_base_library_zip(filename, ordered_modules, code_cache)


building_utils.create_base_library_zip = deterministic_create_base_library_zip

from PyInstaller.__main__ import run

run(sys.argv[1:])
'@
    [System.IO.File]::WriteAllText($pyinstallerRunnerPath, $runner, [System.Text.UTF8Encoding]::new($false))
}

function Write-ConnectorPackageFiles {
    param([Parameter(Mandatory = $true)][string]$PackageDirectory)
    @'
@echo off
setlocal

set "APP_DIR=%~dp0CodexHomeManagerLocal"
start "Codex Home Manager Local Connector" "%APP_DIR%\CodexHomeManagerLocal.exe"
exit /b 0
'@ | Set-Content -LiteralPath (Join-Path $PackageDirectory "Start Codex Home Manager.cmd") -Encoding ASCII

    @'
@echo off
setlocal

set "LAUNCHER=%~dp0CodexHomeManagerLocal\CodexHomeManagerLocal.exe"
reg add "HKCU\Software\Classes\codex-home-manager" /ve /d "URL:Codex Home Manager" /f >nul
reg add "HKCU\Software\Classes\codex-home-manager" /v "URL Protocol" /d "" /f >nul
reg add "HKCU\Software\Classes\codex-home-manager\shell\open\command" /ve /d "\"%LAUNCHER%\" \"%%1\"" /f >nul

echo Codex Home Manager browser launch protocol installed for this Windows user.
pause
'@ | Set-Content -LiteralPath (Join-Path $PackageDirectory "Install browser launch protocol.cmd") -Encoding ASCII

    @'
@echo off
setlocal

reg delete "HKCU\Software\Classes\codex-home-manager" /f >nul 2>nul
echo Codex Home Manager browser launch protocol removed for this Windows user.
pause
'@ | Set-Content -LiteralPath (Join-Path $PackageDirectory "Uninstall browser launch protocol.cmd") -Encoding ASCII

    @'
Codex Home Manager Local Connector

Direct download:
- codex-home-manager-local-win-x64.exe is the recommended single-file Windows app.
- Double-clicking it starts the local connector and opens the loopback-only local product.

ZIP fallback:
1. Run "Start Codex Home Manager.cmd" to start the connector.
2. Run "Install browser launch protocol.cmd" only when the browser protocol is not registered.
3. Set CODEX_HOME before starting the connector if your .codex directory is not in a common location.

Authenticity is established by the detached Ed25519 release manifest signature and independently pinned public-key fingerprint. Authenticode is reported only when a trusted Windows code-signing certificate is available.
'@ | Set-Content -LiteralPath (Join-Path $PackageDirectory "README.txt") -Encoding UTF8
}

function Invoke-IsolatedConnectorBuild {
    param([Parameter(Mandatory = $true)][string]$BuildName)
    $iterationRoot = Join-Path $reproducibleBuildRoot $BuildName
    $distSnapshot = Join-Path $iterationRoot "dist"
    $payloadRoot = Join-Path $iterationRoot "payload"
    $packageRoot = Join-Path $iterationRoot "package"
    $oneFileRoot = Join-Path $iterationRoot "onefile"
    $oneDirWorkRoot = Join-Path $iterationRoot "work-onedir"
    $oneFileWorkRoot = Join-Path $iterationRoot "work-onefile"
    $specRoot = Join-Path $iterationRoot "spec"
    $archive = Join-Path $iterationRoot "codex-home-manager-local-win-x64.zip"

    New-Item -ItemType Directory -Force -Path $iterationRoot, $payloadRoot, $packageRoot, $oneFileRoot, $specRoot | Out-Null
    & $releaseNpmPath run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed in isolated build $BuildName"
    }
    Copy-Item -LiteralPath (Join-Path $appDirectory "dist") -Destination $distSnapshot -Recurse -Force

    & $venvPython $pyinstallerRunnerPath `
            --noconfirm --clean --name CodexHomeManagerLocal --onedir --windowed `
            --icon $iconPath --distpath $payloadRoot --workpath $oneDirWorkRoot --specpath $specRoot `
            --add-data "$distSnapshot;dist" `
            --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.lifespan.on `
            --paths $appDirectory --paths (Join-Path $appDirectory "packaging\windows") `
            $generatedLauncherPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller onedir build failed in isolated build $BuildName"
    }

    & $venvPython $pyinstallerRunnerPath `
            --noconfirm --clean --name CodexHomeManagerLocal --onefile --windowed `
            --icon $iconPath --distpath $oneFileRoot --workpath $oneFileWorkRoot --specpath $specRoot `
            --add-data "$distSnapshot;dist" `
            --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.lifespan.on `
            --paths $appDirectory --paths (Join-Path $appDirectory "packaging\windows") `
            $generatedLauncherPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller onefile build failed in isolated build $BuildName"
    }

    $directExecutable = Join-Path $oneFileRoot "CodexHomeManagerLocal.exe"
    $oneDirExecutable = Join-Path $payloadRoot "CodexHomeManagerLocal\CodexHomeManagerLocal.exe"
    foreach ($path in @($directExecutable, $oneDirExecutable)) {
        & python $releaseManifestScript normalize-pyinstaller-exe --path $path --source-date-epoch $sourceDateEpoch
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller executable normalization failed in isolated build $BuildName"
        }
    }
    Copy-Item -LiteralPath (Join-Path $payloadRoot "CodexHomeManagerLocal") -Destination (Join-Path $packageRoot "CodexHomeManagerLocal") -Recurse -Force
    Write-ConnectorPackageFiles -PackageDirectory $packageRoot
    & python $releaseManifestScript deterministic-zip --source $packageRoot --output $archive --source-date-epoch $sourceDateEpoch
    if ($LASTEXITCODE -ne 0) {
        throw "Canonical ZIP creation failed in isolated build $BuildName"
    }
    return [pscustomobject]@{
        Dist = $distSnapshot
        Exe = $directExecutable
        Zip = $archive
    }
}

function Get-TrustedCodeSigningCertificate {
    $requestedThumbprint = ($env:CODEX_HOME_MANAGER_SIGNING_CERT_THUMBPRINT -replace '\s', '').ToUpperInvariant()
    $candidates = @(Get-ChildItem -Path Cert:\CurrentUser\My | Where-Object {
        $_.HasPrivateKey -and $_.NotBefore -le (Get-Date) -and $_.NotAfter -gt (Get-Date) -and
        @($_.EnhancedKeyUsageList | ForEach-Object ObjectId) -contains "1.3.6.1.5.5.7.3.3" -and
        (-not $requestedThumbprint -or $_.Thumbprint.ToUpperInvariant() -eq $requestedThumbprint)
    } | Where-Object {
        $chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
        try {
            $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::Online
            $chain.ChainPolicy.RevocationFlag = [System.Security.Cryptography.X509Certificates.X509RevocationFlag]::ExcludeRoot
            $chain.Build($_)
        }
        finally {
            $chain.Dispose()
        }
    })
    if ($requestedThumbprint -and $candidates.Count -ne 1) {
        throw "CODEX_HOME_MANAGER_SIGNING_CERT_THUMBPRINT does not identify one trusted code-signing certificate with a private key"
    }
    if (-not $requestedThumbprint -and $candidates.Count -gt 1) {
        throw "Multiple trusted code-signing certificates are available; set CODEX_HOME_MANAGER_SIGNING_CERT_THUMBPRINT explicitly"
    }
    return $candidates | Select-Object -First 1
}

function Invoke-AuthenticodePolicy {
    param([Parameter(Mandatory = $true)][string]$Path)
    $certificate = Get-TrustedCodeSigningCertificate
    if ($certificate) {
        $timestampServer = if ($env:CODEX_HOME_MANAGER_TIMESTAMP_SERVER) { $env:CODEX_HOME_MANAGER_TIMESTAMP_SERVER } else { "http://timestamp.digicert.com" }
        $signingResult = Set-AuthenticodeSignature -LiteralPath $Path -Certificate $certificate -TimestampServer $timestampServer -HashAlgorithm SHA256
        if ($signingResult.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
            throw "Authenticode signing did not produce a valid trusted signature: $($signingResult.StatusMessage)"
        }
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    $valid = $signature.Status -eq [System.Management.Automation.SignatureStatus]::Valid
    return [ordered]@{
        status = if ($valid) { "valid" } else { "unavailable" }
        signerThumbprint = if ($valid) { $signature.SignerCertificate.Thumbprint } else { $null }
        signerSubject = if ($valid) { $signature.SignerCertificate.Subject } else { $null }
        detachedSignatureRequired = $true
    }
}

Push-Location $appDirectory
try {
    if (-not (Test-Path -LiteralPath $publicSiteRoot -PathType Container)) {
        throw "Public site repository was not found: $publicSiteRoot"
    }
    if (-not (Test-Path -LiteralPath $releaseManifestScript -PathType Leaf)) {
        throw "Release manifest script was not found: $releaseManifestScript"
    }

    $nodeToolchain = Get-ReleaseNodeToolchain
    $releaseNodePath = $nodeToolchain.Node
    $releaseNpmPath = $nodeToolchain.Npm
    $env:PATH = "$($nodeToolchain.Directory);$env:PATH"

    $sourceDateEpochText = (& git -C $rootRepository show -s --format=%ct HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceDateEpochText -notmatch '^\d+$') {
        throw "Cannot derive SOURCE_DATE_EPOCH from the root release commit"
    }
    [long]$sourceDateEpoch = $sourceDateEpochText
    $env:SOURCE_DATE_EPOCH = $sourceDateEpochText

    Remove-InternalPath -Path $buildRoot
    New-Item -ItemType Directory -Force -Path $reproducibleBuildRoot, $releaseRoot | Out-Null

    & python $releaseManifestScript capture-build-source `
        --output $buildSourceSnapshotPath `
        --root-repo $rootRepository `
        --manager-repo $appDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to capture clean root and manager HEADs before the build"
    }

    Assert-ReleaseDestinationAvailable -Path $directExecutablePath
    Assert-ReleaseDestinationAvailable -Path $archivePath

    & python "scripts\quality_gate.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Complete product quality gate failed; connector packaging is blocked"
    }

    if (-not (Test-Path -LiteralPath $iconPath)) {
        & python "scripts\generate_windows_icon.py"
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to generate Windows icon"
        }
    }
    if (-not (Test-Path -LiteralPath $iconPath)) {
        throw "Windows icon was not created: $iconPath"
    }

    python -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create connector packaging venv"
    }
    & $venvPython -m pip install --require-hashes --only-binary=:all: -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install connector packaging requirements"
    }
    Write-ConnectorReleaseLauncher
    Write-PyInstallerReproducibilityRunner
    $firstBuild = Invoke-IsolatedConnectorBuild -BuildName "build-1"
    $secondBuild = Invoke-IsolatedConnectorBuild -BuildName "build-2"
    & python $releaseManifestScript compare-builds `
        --first-dist $firstBuild.Dist --second-dist $secondBuild.Dist `
        --first-exe $firstBuild.Exe --second-exe $secondBuild.Exe `
        --first-zip $firstBuild.Zip --second-zip $secondBuild.Zip
    if ($LASTEXITCODE -ne 0) {
        throw "Two isolated connector builds were not byte-for-byte reproducible"
    }
    Invoke-PublicSiteDistSync `
        -DistDirectory (Join-Path $appDirectory "dist") `
        -PublicSiteDirectory $publicSiteRoot

    Stop-VerifiedReleaseDestinationProcesses -Path $directExecutablePath
    Copy-Item -LiteralPath $firstBuild.Exe -Destination $directExecutablePath -Force
    Copy-Item -LiteralPath $firstBuild.Zip -Destination $archivePath -Force
    $authenticodeAudit = Invoke-AuthenticodePolicy -Path $directExecutablePath
    $blackboxPort = Get-RandomLoopbackPort
    & python $releaseManifestScript blackbox-exe --executable $directExecutablePath --port $blackboxPort
    if ($LASTEXITCODE -ne 0) {
        throw "Final packaged EXE failed random-port public-Origin and same-origin black-box verification"
    }

    Assert-ReleaseZipBoundary -Path $archivePath
    $executableAudit = Assert-PyInstallerExecutableBoundary -Path $directExecutablePath
    $directExecutableHash = Get-Sha256HashText -Path $directExecutablePath
    $archiveHash = Get-Sha256HashText -Path $archivePath
    $publicExecutableName = Get-ContentAddressedReleaseName -Extension "exe" -Sha256 $directExecutableHash
    $publicArchiveName = Get-ContentAddressedReleaseName -Extension "zip" -Sha256 $archiveHash
    $publicExecutablePath = Join-Path $publicSiteRoot $publicExecutableName
    $publicArchivePath = Join-Path $publicSiteRoot $publicArchiveName
    $stableExePath = "/codex-home-manager-local-win-x64.exe"
    $stableZipPath = "/codex-home-manager-local-win-x64.zip"
    $latestExePath = "/downloads/latest/windows-x64.exe"
    $latestZipPath = "/downloads/latest/windows-x64.zip"
    & python $releaseManifestScript keygen `
        --private-key $privateKeyPath `
        --public-key $releasePublicKeyPath `
        --trusted-public-key-fingerprint $trustedPublicKeyFingerprintPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to prepare the private-root Ed25519 release signing trust"
    }
    $trustedPublicKeyFingerprint = (Get-Content -LiteralPath $trustedPublicKeyFingerprintPath -Raw).Trim()
    $verifyScriptText = @"
`$ErrorActionPreference = "Stop"

`$filePath = Join-Path `$env:USERPROFILE "Downloads\$publicExecutableName"
`$expectedSha256 = "$directExecutableHash"
`$trustedPublicKeyFingerprint = "$trustedPublicKeyFingerprint"

if (-not (Test-Path -LiteralPath `$filePath)) {
    Write-Host "File not found: `$filePath"
    Write-Host "Move this script next to the downloaded EXE or edit `$filePath, then run it again."
    exit 2
}

`$actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath `$filePath).Hash.ToLowerInvariant()
if (`$actualSha256 -eq `$expectedSha256) {
    Write-Host "OK: checksum matches the release artifact."
    Write-Host "Pinned Ed25519 public key fingerprint: `$trustedPublicKeyFingerprint"
    exit 0
}

Write-Host "FAILED: checksum mismatch. Do not run this file."
Write-Host "Expected: `$expectedSha256"
Write-Host "Actual:   `$actualSha256"
exit 1
"@
    [System.IO.File]::WriteAllText($verifyScriptPath, $verifyScriptText, [System.Text.UTF8Encoding]::new($false))
    if (-not (Test-Path -LiteralPath $verifyScriptPath)) {
        throw "Verification script was not created: $verifyScriptPath"
    }
    $checksums = @($directExecutablePath, $archivePath, $verifyScriptPath) | ForEach-Object {
        $hash = Get-Sha256HashText -Path $_
        "{0}  {1}" -f $hash, (Split-Path -Leaf $_)
    }
    $checksumText = ($checksums -join "`n") + "`n"
    [System.IO.File]::WriteAllText($checksumPath, $checksumText, [System.Text.UTF8Encoding]::new($false))
    if (-not (Test-Path -LiteralPath $checksumPath)) {
        throw "Checksum file was not created: $checksumPath"
    }
    Copy-Item -LiteralPath $directExecutablePath -Destination $publicExecutablePath -Force
    Copy-Item -LiteralPath $archivePath -Destination $publicArchivePath -Force
    Copy-Item -LiteralPath $verifyScriptPath -Destination (Join-Path $publicSiteRoot "verify-codex-home-manager.ps1") -Force
    Copy-Item -LiteralPath $releasePublicKeyPath -Destination (Join-Path $publicSiteRoot "release-signing-public-key.pem") -Force
    [System.IO.File]::WriteAllText(
        (Join-Path $publicSiteRoot "release-signing-public-key.sha256"),
        $trustedPublicKeyFingerprint + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )

    $releaseMetadata = [ordered]@{
        schemaVersion = 1
        version = $releaseVersion
        artifacts = @(
            [ordered]@{
                name = $publicExecutableName
                kind = "exe"
                sha256 = $directExecutableHash
                size = (Get-Item -LiteralPath $directExecutablePath).Length
                audit = $executableAudit
                authenticode = $authenticodeAudit
            },
            [ordered]@{
                name = $publicArchiveName
                kind = "zip"
                sha256 = $archiveHash
                size = (Get-Item -LiteralPath $archivePath).Length
            }
        )
    }
    $releaseMetadataText = $releaseMetadata | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText(
        (Join-Path $publicSiteRoot "connector-release.json"),
        $releaseMetadataText + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )

    $publicChecksumPaths = @(
        $publicExecutablePath,
        $publicArchivePath,
        (Join-Path $publicSiteRoot "connector-release.json"),
        (Join-Path $publicSiteRoot "verify-codex-home-manager.ps1"),
        (Join-Path $publicSiteRoot "release-signing-public-key.pem"),
        (Join-Path $publicSiteRoot "release-signing-public-key.sha256")
    )
    $publicChecksums = @($publicChecksumPaths | ForEach-Object {
        "{0}  {1}" -f (Get-Sha256HashText -Path $_), (Split-Path -Leaf $_)
    }) -join "`n"
    [System.IO.File]::WriteAllText(
        (Join-Path $publicSiteRoot "SHA256SUMS.txt"),
        $publicChecksums + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )

    & python $releaseManifestScript prepare-source-evidence `
        --evidence-dir $SourceEvidenceDirectory `
        --source-commit $SourceCommit `
        --repository $SourceEvidenceRepository `
        --signer-workflow $SourceEvidenceSignerWorkflow `
        --build-source-snapshot $buildSourceSnapshotPath `
        --release-dir $releaseRoot `
        --public-site $publicSiteRoot `
        --proof $sourceEvidenceProofPath
    if ($LASTEXITCODE -ne 0) {
        throw "Source CI evidence verification or publication preparation failed"
    }

    $redirectText = @(
        "$stableExePath /$publicExecutableName 302",
        "$stableZipPath /$publicArchiveName 302",
        "$latestExePath /$publicExecutableName 302",
        "$latestZipPath /$publicArchiveName 302",
        "/* /index.html 200"
    ) -join "`n"
    [System.IO.File]::WriteAllText(
        (Join-Path $publicSiteRoot "_redirects"),
        $redirectText + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )

    $headerText = @"
/*
  Content-Security-Policy: default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self' http://127.0.0.1:8765 http://localhost:8765 https://github.com https://objects.githubusercontent.com https://release-assets.githubusercontent.com; worker-src 'none'; manifest-src 'self'
  Cache-Control: public, max-age=0, must-revalidate, no-transform
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
  X-Frame-Options: DENY

/assets/*
  Cache-Control: public, max-age=31536000, immutable, no-transform

/codex-home-manager-local-win-x64-v*
  Cache-Control: public, max-age=31536000, immutable, no-transform

$stableExePath
  Cache-Control: no-store, max-age=0

$stableZipPath
  Cache-Control: no-store, max-age=0

$latestExePath
  Cache-Control: no-store, max-age=0

$latestZipPath
  Cache-Control: no-store, max-age=0

/connector-release.json
  Cache-Control: no-store, max-age=0

/SHA256SUMS.txt
  Cache-Control: no-store, max-age=0

/codex-home-manager-source.zip
  Cache-Control: no-store, max-age=0

/codex-home-manager-source.cdx.json
  Cache-Control: no-store, max-age=0

/source-ci-test-summary.md
  Cache-Control: no-store, max-age=0

/source-*-attestation.sigstore.json
  Cache-Control: no-store, max-age=0

/release-manifest.json
  Cache-Control: no-store, max-age=0

/release-manifest.json.sig
  Cache-Control: no-store, max-age=0

/release-signing-public-key.pem
  Cache-Control: no-store, max-age=0

/release-signing-public-key.sha256
  Cache-Control: no-store, max-age=0
"@
    [System.IO.File]::WriteAllText(
        (Join-Path $publicSiteRoot "_headers"),
        $headerText,
        [System.Text.UTF8Encoding]::new($false)
    )

    Add-Type -AssemblyName Microsoft.VisualBasic
    $resolvedPublicSiteRoot = [System.IO.Path]::GetFullPath($publicSiteRoot).TrimEnd('\')
    $currentArtifactNames = @($publicExecutableName, $publicArchiveName)
    $stalePublicArtifacts = @(Get-ChildItem -LiteralPath $publicSiteRoot -File | Where-Object {
        $_.Name -in @("codex-home-manager-local-win-x64.exe", "codex-home-manager-local-win-x64.zip") -or
        ($_.Name -match '^codex-home-manager-local-win-x64-v\d+\.\d+\.\d+-[0-9a-f]{12}\.(exe|zip)$' -and $_.Name -notin $currentArtifactNames)
    })
    foreach ($staleArtifact in $stalePublicArtifacts) {
        if ([System.IO.Path]::GetFullPath($staleArtifact.DirectoryName).TrimEnd('\') -cne $resolvedPublicSiteRoot) {
            throw "Refusing to retire a release artifact outside the public site root: $($staleArtifact.FullName)"
        }
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            $staleArtifact.FullName,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
        )
    }
    $staleSignedMetadataNames = @("release-manifest.json", "release-manifest.json.sig")
    foreach ($metadataName in $staleSignedMetadataNames) {
        $metadataPath = Join-Path $publicSiteRoot $metadataName
        if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
            continue
        }
        if ([System.IO.Path]::GetFullPath((Split-Path -Parent $metadataPath)).TrimEnd('\') -cne $resolvedPublicSiteRoot) {
            throw "Refusing to retire signed metadata outside the public site root: $metadataPath"
        }
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            $metadataPath,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
        )
    }

    $nodeVersion = (& $releaseNodePath --version).TrimStart("v").Split(".")
    if ($LASTEXITCODE -ne 0 -or [int]$nodeVersion[0] -lt 22) {
        throw "Public release checks require Node.js 22 or newer"
    }
    Push-Location $publicRepository
    try {
        & $releaseNpmPath ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to recreate public dependencies from package-lock.json"
        }
        & $releaseNpmPath audit --audit-level=high
        if ($LASTEXITCODE -ne 0) {
            throw "Public npm audit found a high or critical vulnerability"
        }
        & $releaseNpmPath run check
        if ($LASTEXITCODE -ne 0) {
            throw "Public release boundary checks failed"
        }
    }
    finally {
        Pop-Location
    }

    & python $releaseManifestScript validate-build-source --source-snapshot $buildSourceSnapshotPath
    if ($LASTEXITCODE -ne 0) {
        throw "Root or manager source changed after the pre-build capture; refusing to publish artifacts from a drifting source tree"
    }

    Write-Output $directExecutablePath
    Write-Output $archivePath
    Write-Output $publicExecutablePath
    Write-Output $publicArchivePath
    Write-Output $checksumPath
    Write-Output $verifyScriptPath
    Write-Output $releasePublicKeyPath
    Write-Output "Create a GitHub draft release containing exactly the content-addressed EXE and ZIP, deploy the artifact commit, then run finalize-release-manifest.ps1 with the Cloudflare deployment and GitHub release identifiers."
}
finally {
    Pop-Location
}
