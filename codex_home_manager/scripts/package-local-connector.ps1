[CmdletBinding(DefaultParameterSetName = "Release")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Release")]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$SourceEvidenceDirectory,

    [Parameter(Mandatory = $true, ParameterSetName = "Release")]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$WindowsEvidenceDirectory,

    [Parameter(Mandatory = $true, ParameterSetName = "Release")]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$SourceCommit,

    [Parameter(Mandatory = $true, ParameterSetName = "Release")]
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string]$SourceEvidenceRepository,

    [Parameter(Mandatory = $true, ParameterSetName = "Release")]
    [ValidateNotNullOrEmpty()]
    [string]$SourceEvidenceSignerWorkflow,

    [Parameter(Mandatory = $true, ParameterSetName = "CiBuild")]
    [switch]$CiBuild,

    [Parameter(Mandatory = $true, ParameterSetName = "CiBuild")]
    [ValidateNotNullOrEmpty()]
    [string]$CiOutputDirectory,

    [switch]$VerifyReproducibleBuild,

    [switch]$FullReleaseValidation
)

$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $PSScriptRoot
$rootRepository = Split-Path -Parent $appDirectory
$publicRepository = Join-Path $rootRepository "codex-home-manager-public"
$buildRoot = Join-Path $appDirectory "build\local-connector"
$reproducibleBuildRoot = Join-Path $buildRoot "reproducible"
$stablePublicSiteRoot = Join-Path $publicRepository "site"
$stableReleaseRoot = Join-Path $appDirectory "build\releases"
$publicationStageRoot = Join-Path $buildRoot "publication-stage"
$publicValidationRoot = Join-Path $publicationStageRoot "public-repository"
$publicSiteRoot = if ($PSCmdlet.ParameterSetName -eq "Release") { Join-Path $publicValidationRoot "site" } else { $stablePublicSiteRoot }
$releaseRoot = if ($PSCmdlet.ParameterSetName -eq "Release") { Join-Path $publicationStageRoot "release" } else { $stableReleaseRoot }
$venvRoot = Join-Path $buildRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$archivePath = Join-Path $releaseRoot "codex-home-manager-local-win-x64.zip"
$directExecutablePath = Join-Path $releaseRoot "codex-home-manager-local-win-x64.exe"
$checksumPath = Join-Path $releaseRoot "SHA256SUMS.txt"
$verifyScriptPath = Join-Path $releaseRoot "verify-codex-home-manager.ps1"
$releasePublicKeyPath = Join-Path $releaseRoot "release-signing-public-key.pem"
$privateKeyPath = "D:\Backup\codex_home_manager\release-signing\release-signing-key.pem"
$trustedPublicKeyFingerprintPath = "D:\Backup\codex_home_manager\release-signing\release-signing-public-key.sha256"
$embeddedReleasePublicKeyFingerprint = "sha256:ef7194fbc8fa8550430c908d9d02c74f7fc0d1e87f7f9b4ec5a164526b48f208"
$releaseManifestScript = Join-Path $PSScriptRoot "release_manifest.py"
$buildSourceSnapshotPath = Join-Path $buildRoot "release-build-source.json"
$sourceEvidenceProofPath = Join-Path $buildRoot "source-release-evidence.json"
$windowsBinaryEvidenceProofPath = Join-Path $buildRoot "windows-binary-evidence.json"
$publicDistSyncPlanPath = Join-Path $buildRoot "public-dist-sync-plan.json"
$generatedLauncherPath = Join-Path $buildRoot "connector_release_entry.py"
$pyinstallerRunnerPath = Join-Path $buildRoot "run_reproducible_pyinstaller.py"
$iconPath = Join-Path $appDirectory "packaging\windows\assets\codex-home-manager.ico"
$versionInfoPath = Join-Path $appDirectory "packaging\windows\version_info.txt"
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

function Initialize-PublicValidationRoot {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRepository,
        [Parameter(Mandatory = $true)][string]$DestinationRepository
    )
    New-Item -ItemType Directory -Force -Path $DestinationRepository | Out-Null
    $excludedRoots = @(
        [System.IO.Path]::GetFullPath((Join-Path $SourceRepository ".git")).TrimEnd('\') + '\',
        [System.IO.Path]::GetFullPath((Join-Path $SourceRepository "node_modules")).TrimEnd('\') + '\',
        [System.IO.Path]::GetFullPath((Join-Path $SourceRepository "site")).TrimEnd('\') + '\'
    )
    foreach ($sourcePath in Get-ChildItem -LiteralPath $SourceRepository -File -Recurse -Force) {
        $resolvedSource = [System.IO.Path]::GetFullPath($sourcePath.FullName)
        if ($excludedRoots | Where-Object { $resolvedSource.StartsWith($_, [System.StringComparison]::OrdinalIgnoreCase) }) {
            continue
        }
        $relativePath = [System.IO.Path]::GetRelativePath($SourceRepository, $resolvedSource)
        $destinationPath = Join-Path $DestinationRepository $relativePath
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
        [System.IO.File]::Copy($resolvedSource, $destinationPath, $true)
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $DestinationRepository "site") | Out-Null
}

function Publish-StagedReleaseSet {
    param(
        [Parameter(Mandatory = $true)][string]$StagedReleaseDirectory,
        [Parameter(Mandatory = $true)][string]$StableReleaseDirectory,
        [Parameter(Mandatory = $true)][string]$StagedSiteDirectory,
        [Parameter(Mandatory = $true)][string]$StableSiteDirectory
    )
    foreach ($requiredDirectory in @($StagedReleaseDirectory, $StagedSiteDirectory)) {
        if (-not (Test-Path -LiteralPath $requiredDirectory -PathType Container)) {
            throw "Publication staging directory is missing: $requiredDirectory"
        }
    }
    $swapRoot = Join-Path $buildRoot "publication-swap"
    Remove-InternalPath -Path $swapRoot
    New-Item -ItemType Directory -Force -Path $swapRoot | Out-Null
    $releaseRollback = Join-Path $swapRoot "release-rollback"
    $siteRollback = Join-Path $swapRoot "site-rollback"
    $failedRelease = Join-Path $swapRoot "failed-release"
    $failedSite = Join-Path $swapRoot "failed-site"
    $releasePublished = $false
    $sitePublished = $false
    try {
        Stop-VerifiedReleaseDestinationProcesses -Path (Join-Path $StableReleaseDirectory "codex-home-manager-local-win-x64.exe")
        if (Test-Path -LiteralPath $StableReleaseDirectory -PathType Container) {
            Move-Item -LiteralPath $StableReleaseDirectory -Destination $releaseRollback
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StableReleaseDirectory) | Out-Null
        Move-Item -LiteralPath $StagedReleaseDirectory -Destination $StableReleaseDirectory
        $releasePublished = $true

        if (Test-Path -LiteralPath $StableSiteDirectory -PathType Container) {
            Move-Item -LiteralPath $StableSiteDirectory -Destination $siteRollback
        }
        Move-Item -LiteralPath $StagedSiteDirectory -Destination $StableSiteDirectory
        $sitePublished = $true
    }
    catch {
        if ($sitePublished -and (Test-Path -LiteralPath $StableSiteDirectory)) {
            Move-Item -LiteralPath $StableSiteDirectory -Destination $failedSite
        }
        if (Test-Path -LiteralPath $siteRollback) {
            Move-Item -LiteralPath $siteRollback -Destination $StableSiteDirectory
        }
        if ($releasePublished -and (Test-Path -LiteralPath $StableReleaseDirectory)) {
            Move-Item -LiteralPath $StableReleaseDirectory -Destination $failedRelease
        }
        if (Test-Path -LiteralPath $releaseRollback) {
            Move-Item -LiteralPath $releaseRollback -Destination $StableReleaseDirectory
        }
        throw
    }

    Add-Type -AssemblyName Microsoft.VisualBasic
    foreach ($retiredDirectory in @($releaseRollback, $siteRollback)) {
        if (Test-Path -LiteralPath $retiredDirectory -PathType Container) {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
                $retiredDirectory,
                [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
            )
        }
    }
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

function Assert-WindowsVersionMetadata {
    param([Parameter(Mandatory = $true)][string]$Path)
    $versionInfo = (Get-Item -LiteralPath $Path).VersionInfo
    $expected = [ordered]@{
        FileVersion = $releaseVersion
        ProductVersion = $releaseVersion
        CompanyName = "SimpleZion"
        ProductName = "Codex Home Manager"
        FileDescription = "Codex Home Manager"
    }
    foreach ($name in $expected.Keys) {
        if ([string]$versionInfo.$name -cne [string]$expected[$name]) {
            throw "Windows version metadata mismatch for $name in ${Path}: expected '$($expected[$name])', got '$($versionInfo.$name)'"
        }
    }
    return $expected
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

Authenticity is established by the detached Ed25519 release manifest signature and independently pinned public-key fingerprint. Authenticode is used only when an existing publicly trusted certificate is available; otherwise Windows and release metadata explicitly report NotSigned.
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
            --icon $iconPath --version-file $versionInfoPath --distpath $payloadRoot --workpath $oneDirWorkRoot --specpath $specRoot `
            --add-data "$distSnapshot;dist" `
            --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.lifespan.on `
            --paths $appDirectory --paths (Join-Path $appDirectory "packaging\windows") `
            $generatedLauncherPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller onedir build failed in isolated build $BuildName"
    }

    & $venvPython $pyinstallerRunnerPath `
            --noconfirm --clean --name CodexHomeManagerLocal --onefile --windowed `
            --icon $iconPath --version-file $versionInfoPath --distpath $oneFileRoot --workpath $oneFileWorkRoot --specpath $specRoot `
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

function Get-CertificateChainEvidence {
    param([Parameter(Mandatory = $true)]$Certificate)
    $chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
    try {
        $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::Online
        $chain.ChainPolicy.RevocationFlag = [System.Security.Cryptography.X509Certificates.X509RevocationFlag]::ExcludeRoot
        $chainBuilt = $chain.Build($Certificate)
        $chainElements = @($chain.ChainElements)
        $selfSigned = $Certificate.Subject -eq $Certificate.Issuer -or
            ($chainElements.Count -eq 1 -and $chainElements[0].Certificate.Thumbprint -eq $Certificate.Thumbprint)
        $rootThumbprint = if ($chainElements.Count) { $chainElements[-1].Certificate.Thumbprint } else { $null }
        $publicAuthRoot = $false
        if ($rootThumbprint) {
            $publicAuthRoot = @(Get-ChildItem Cert:\LocalMachine\AuthRoot, Cert:\CurrentUser\AuthRoot -ErrorAction SilentlyContinue | Where-Object {
                $_.Thumbprint -eq $rootThumbprint
            }).Count -gt 0
        }
        return [pscustomobject]@{
            SelfSigned = $selfSigned
            PublicChainTrusted = $chainBuilt -and -not $selfSigned -and $chainElements.Count -ge 2 -and $publicAuthRoot
        }
    }
    finally {
        $chain.Dispose()
    }
}

function Get-TrustedCodeSigningCertificate {
    $requestedThumbprint = ($env:CODEX_HOME_MANAGER_SIGNING_CERT_THUMBPRINT -replace '\s', '').ToUpperInvariant()
    $candidates = @(Get-ChildItem -Path Cert:\CurrentUser\My | Where-Object {
        $_.HasPrivateKey -and $_.NotBefore -le (Get-Date) -and $_.NotAfter -gt (Get-Date) -and
        @($_.EnhancedKeyUsageList | ForEach-Object ObjectId) -contains "1.3.6.1.5.5.7.3.3" -and
        (-not $requestedThumbprint -or $_.Thumbprint.ToUpperInvariant() -eq $requestedThumbprint)
    } | Where-Object {
        (Get-CertificateChainEvidence -Certificate $_).PublicChainTrusted
    })
    if ($requestedThumbprint -and $candidates.Count -ne 1) {
        throw "CODEX_HOME_MANAGER_SIGNING_CERT_THUMBPRINT does not identify one publicly trusted code-signing certificate with a private key"
    }
    if (-not $requestedThumbprint -and $candidates.Count -gt 1) {
        throw "Multiple publicly trusted code-signing certificates are available; set CODEX_HOME_MANAGER_SIGNING_CERT_THUMBPRINT explicitly"
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
        $signature = Get-AuthenticodeSignature -LiteralPath $Path
        $chainEvidence = Get-CertificateChainEvidence -Certificate $signature.SignerCertificate
        if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
            -not $chainEvidence.PublicChainTrusted) {
            throw "Authenticode signing did not retain a publicly trusted signature"
        }
        return [ordered]@{
            status = "valid"
            windowsStatus = "Valid"
            trust = "public-trusted"
            signerThumbprint = $signature.SignerCertificate.Thumbprint
            signerSubject = $signature.SignerCertificate.Subject
            selfSigned = $false
            chainTrusted = $true
            detachedSignatureRequired = $true
        }
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::NotSigned -or
        $null -ne $signature.SignerCertificate) {
        throw "No publicly trusted code-signing certificate is configured, but the EXE is not explicitly NotSigned"
    }
    return [ordered]@{
        status = "not-signed"
        windowsStatus = "NotSigned"
        trust = "none"
        signerThumbprint = $null
        signerSubject = $null
        selfSigned = $false
        chainTrusted = $false
        detachedSignatureRequired = $true
    }
}

function Assert-CiAuthenticodeEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Evidence
    )
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($Evidence.status -ceq "not-signed") {
        if ($Evidence.windowsStatus -cne "NotSigned" -or
            $Evidence.trust -cne "none" -or
            $signature.Status -ne [System.Management.Automation.SignatureStatus]::NotSigned -or
            $null -ne $signature.SignerCertificate) {
            throw "Source CI claims NotSigned, but the downloaded EXE Authenticode state differs"
        }
        return
    }
    if ($Evidence.status -cne "valid" -or
        $Evidence.windowsStatus -cne "Valid" -or
        $Evidence.trust -cne "public-trusted" -or
        $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
        $null -eq $signature.SignerCertificate) {
        throw "Source CI Authenticode evidence is neither explicitly NotSigned nor currently valid and public-trusted"
    }
    $chainEvidence = Get-CertificateChainEvidence -Certificate $signature.SignerCertificate
    if (-not $chainEvidence.PublicChainTrusted -or
        $signature.SignerCertificate.Thumbprint -cne [string]$Evidence.signerThumbprint -or
        $signature.SignerCertificate.Subject -cne [string]$Evidence.signerSubject) {
        throw "Downloaded EXE Authenticode signer or public trust chain differs from Source CI evidence"
    }
}

Push-Location $appDirectory
try {
    if ($PSCmdlet.ParameterSetName -eq "Release" -and -not (Test-Path -LiteralPath $stablePublicSiteRoot -PathType Container)) {
        throw "Public site repository was not found: $stablePublicSiteRoot"
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
    if ($PSCmdlet.ParameterSetName -eq "Release") {
        Initialize-PublicValidationRoot `
            -SourceRepository $publicRepository `
            -DestinationRepository $publicValidationRoot
    }

    if ($PSCmdlet.ParameterSetName -eq "Release") {
        & python $releaseManifestScript capture-build-source `
            --output $buildSourceSnapshotPath `
            --root-repo $rootRepository `
            --manager-repo $appDirectory
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to capture clean root and manager HEADs before the build"
        }
    }

    if ($PSCmdlet.ParameterSetName -eq "CiBuild" -and ($FullReleaseValidation -or $VerifyReproducibleBuild)) {
        & python "scripts\quality_gate.py"
        if ($LASTEXITCODE -ne 0) {
            throw "Complete product quality gate failed; connector packaging is blocked"
        }
    }

    if ($PSCmdlet.ParameterSetName -eq "CiBuild") {
        if (-not (Test-Path -LiteralPath $iconPath)) {
            & python "scripts\generate_windows_icon.py"
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to generate Windows icon"
            }
        }
        if (-not (Test-Path -LiteralPath $iconPath)) {
            throw "Windows icon was not created: $iconPath"
        }
        if (-not (Test-Path -LiteralPath $versionInfoPath -PathType Leaf)) {
            throw "Windows version resource was not found: $versionInfoPath"
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
        if ($FullReleaseValidation -or $VerifyReproducibleBuild) {
            $secondBuild = Invoke-IsolatedConnectorBuild -BuildName "build-2"
            & python $releaseManifestScript compare-builds `
                --first-dist $firstBuild.Dist --second-dist $secondBuild.Dist `
                --first-exe $firstBuild.Exe --second-exe $secondBuild.Exe `
                --first-zip $firstBuild.Zip --second-zip $secondBuild.Zip
            if ($LASTEXITCODE -ne 0) {
                throw "Two isolated connector builds were not byte-for-byte reproducible"
            }
        }
        $authenticodeAudit = Invoke-AuthenticodePolicy -Path $firstBuild.Exe
        Assert-ReleaseZipBoundary -Path $firstBuild.Zip
        $executableAudit = Assert-PyInstallerExecutableBoundary -Path $firstBuild.Exe
        $versionAudit = Assert-WindowsVersionMetadata -Path $firstBuild.Exe
        $executableAudit["versionInfo"] = $versionAudit
        $directExecutableHash = Get-Sha256HashText -Path $firstBuild.Exe
        $archiveHash = Get-Sha256HashText -Path $firstBuild.Zip
        $publicExecutableName = Get-ContentAddressedReleaseName -Extension "exe" -Sha256 $directExecutableHash
        $publicArchiveName = Get-ContentAddressedReleaseName -Extension "zip" -Sha256 $archiveHash
        $resolvedCiOutputDirectory = [System.IO.Path]::GetFullPath($CiOutputDirectory)
        New-Item -ItemType Directory -Force -Path $resolvedCiOutputDirectory | Out-Null
        $ciExecutablePath = Join-Path $resolvedCiOutputDirectory $publicExecutableName
        $ciArchivePath = Join-Path $resolvedCiOutputDirectory $publicArchiveName
        Copy-Item -LiteralPath $firstBuild.Exe -Destination $ciExecutablePath -Force
        Copy-Item -LiteralPath $firstBuild.Zip -Destination $ciArchivePath -Force
        $ciMetadata = [ordered]@{
            schemaVersion = 1
            version = $releaseVersion
            artifacts = @(
                [ordered]@{ name = $publicExecutableName; kind = "exe"; sha256 = $directExecutableHash; size = (Get-Item -LiteralPath $ciExecutablePath).Length; audit = $executableAudit; authenticode = $authenticodeAudit },
                [ordered]@{ name = $publicArchiveName; kind = "zip"; sha256 = $archiveHash; size = (Get-Item -LiteralPath $ciArchivePath).Length }
            )
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $resolvedCiOutputDirectory "windows-build-metadata.json"),
            ($ciMetadata | ConvertTo-Json -Depth 8) + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )
        Write-Output "CI Windows release artifacts: $resolvedCiOutputDirectory"
        return
    }

    & python $releaseManifestScript prepare-windows-evidence `
        --evidence-dir $WindowsEvidenceDirectory `
        --source-commit $SourceCommit `
        --version $releaseVersion `
        --repository $SourceEvidenceRepository `
        --signer-workflow $SourceEvidenceSignerWorkflow `
        --release-dir $releaseRoot `
        --public-site $publicSiteRoot `
        --proof $windowsBinaryEvidenceProofPath
    if ($LASTEXITCODE -ne 0) {
        throw "Source CI Windows artifact or attestation verification failed"
    }
    $windowsBinaryEvidence = Get-Content -LiteralPath $windowsBinaryEvidenceProofPath -Raw | ConvertFrom-Json
    $ciExecutableArtifact = @($windowsBinaryEvidence.artifacts | Where-Object kind -CEQ "exe")
    $ciArchiveArtifact = @($windowsBinaryEvidence.artifacts | Where-Object kind -CEQ "zip")
    if ($ciExecutableArtifact.Count -ne 1 -or $ciArchiveArtifact.Count -ne 1) {
        throw "Verified Source CI Windows evidence must identify exactly one EXE and one ZIP"
    }
    $publicExecutableName = [string]$ciExecutableArtifact[0].name
    $publicArchiveName = [string]$ciArchiveArtifact[0].name
    $directExecutableHash = [string]$ciExecutableArtifact[0].sha256
    $archiveHash = [string]$ciArchiveArtifact[0].sha256
    $executableAudit = $ciExecutableArtifact[0].audit
    $authenticodeAudit = $ciExecutableArtifact[0].authenticode
    Assert-ReleaseZipBoundary -Path $archivePath
    Assert-WindowsVersionMetadata -Path $directExecutablePath | Out-Null
    Assert-CiAuthenticodeEvidence -Path $directExecutablePath -Evidence $authenticodeAudit
    $blackboxPort = Get-RandomLoopbackPort
    & python $releaseManifestScript blackbox-exe --executable $directExecutablePath --port $blackboxPort
    if ($LASTEXITCODE -ne 0) {
        throw "Downloaded Source CI EXE failed random-port public-Origin and same-origin black-box verification"
    }

    Invoke-PublicSiteDistSync `
        -DistDirectory (Join-Path $appDirectory "dist") `
        -PublicSiteDirectory $publicSiteRoot

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
    if ($trustedPublicKeyFingerprint -cne $embeddedReleasePublicKeyFingerprint) {
        throw "Release signing key rotation requires an explicit review and update of the verifier's independent trust anchor"
    }
    & python $releaseManifestScript write-user-verifier `
        --output $verifyScriptPath `
        --default-artifact-name $publicExecutableName `
        --trusted-public-key-fingerprint $embeddedReleasePublicKeyFingerprint
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to generate the independently pinned user artifact verifier"
    }
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
        schemaVersion = 2
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
    ) + @($windowsBinaryEvidence.assets | ForEach-Object { Join-Path $publicSiteRoot ([string]$_.name) })
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
  Strict-Transport-Security: max-age=63072000; includeSubDomains; preload

/assets/*.css
  Cache-Control: public, max-age=31536000, immutable, no-transform

/assets/*.js
  Cache-Control: public, max-age=31536000, immutable, no-transform

/assets/*.wasm
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

/windows-build-metadata.json
  Cache-Control: no-store, max-age=0

/codex-home-manager-windows-x64-*.cdx.json
  Cache-Control: no-store, max-age=0

/BINARY-*-SUBJECTS.txt
  Cache-Control: no-store, max-age=0

/windows-*-attestation.sigstore.json
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
    $currentWindowsEvidenceNames = @($windowsBinaryEvidence.assets | ForEach-Object { [string]$_.name })
    $stalePublicArtifacts = @(Get-ChildItem -LiteralPath $publicSiteRoot -File | Where-Object {
        $_.Name -in @("codex-home-manager-local-win-x64.exe", "codex-home-manager-local-win-x64.zip") -or
        ($_.Name -match '^codex-home-manager-local-win-x64-v\d+\.\d+\.\d+-[0-9a-f]{12}\.(exe|zip)$' -and $_.Name -notin $currentArtifactNames) -or
        ($_.Name -match '^codex-home-manager-windows-x64-[0-9a-f]{40}\.cdx\.json$' -and $_.Name -notin $currentWindowsEvidenceNames)
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
        & $releaseNpmPath audit --audit-level=high --omit=optional
        if ($LASTEXITCODE -ne 0) {
            throw "Public npm audit found a high or critical vulnerability"
        }
        & $releaseNpmPath test
        if ($LASTEXITCODE -ne 0) {
            throw "Public release boundary regression tests failed"
        }
        & $releaseNodePath (Join-Path $publicRepository "scripts\check-public-boundary.mjs") `
            --root $publicValidationRoot `
            --artifact-stage
        if ($LASTEXITCODE -ne 0) {
            throw "Staged public release boundary checks failed"
        }
    }
    finally {
        Pop-Location
    }

    & python $releaseManifestScript validate-build-source --source-snapshot $buildSourceSnapshotPath
    if ($LASTEXITCODE -ne 0) {
        throw "Root or manager source changed after the pre-build capture; refusing to publish artifacts from a drifting source tree"
    }

    Publish-StagedReleaseSet `
        -StagedReleaseDirectory $releaseRoot `
        -StableReleaseDirectory $stableReleaseRoot `
        -StagedSiteDirectory $publicSiteRoot `
        -StableSiteDirectory $stablePublicSiteRoot

    Write-Output (Join-Path $stableReleaseRoot "codex-home-manager-local-win-x64.exe")
    Write-Output (Join-Path $stableReleaseRoot "codex-home-manager-local-win-x64.zip")
    Write-Output (Join-Path $stablePublicSiteRoot $publicExecutableName)
    Write-Output (Join-Path $stablePublicSiteRoot $publicArchiveName)
    Write-Output (Join-Path $stableReleaseRoot "SHA256SUMS.txt")
    Write-Output (Join-Path $stableReleaseRoot "verify-codex-home-manager.ps1")
    Write-Output (Join-Path $stableReleaseRoot "release-signing-public-key.pem")
    Write-Output "Create a GitHub draft release containing exactly the content-addressed EXE and ZIP, deploy the artifact commit, then run finalize-release-manifest.ps1 with the Cloudflare deployment and GitHub release identifiers."
}
finally {
    Pop-Location
}
