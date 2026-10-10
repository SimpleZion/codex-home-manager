[CmdletBinding()]
param(
    [switch]$SkipFrontendBuild,
    [switch]$ReuseExisting
)

$ErrorActionPreference = "Stop"

# Existing sidecars have no receipt binding source, toolchain and embedded catalogs.
# Refuse before building or replacing any resource; marker files cannot establish provenance.
if ($ReuseExisting) { throw "ReuseExisting requires a source/toolchain-bound sidecar receipt; run a fresh build without ReuseExisting" }

$appDirectory = Split-Path -Parent $PSScriptRoot
if (-not $env:CODEX_STORAGE_RUN_ROOT -or -not $env:CARGO_TARGET_DIR -or -not $env:CODEX_HOME_MANAGER_BUILD_ROOT) {
    throw "Desktop sidecar builds must run through scripts/build_home_manager_guarded.py so storage admission is enforced"
}
$buildRoot = [System.IO.Path]::GetFullPath($env:CODEX_HOME_MANAGER_BUILD_ROOT)
$legacyDistRoot = Join-Path $buildRoot "legacy-dist"
$legacyWorkRoot = Join-Path $buildRoot "legacy-work"
$legacySpecRoot = Join-Path $buildRoot "legacy-spec"
$nativeResourceRoot = Join-Path $appDirectory "src-tauri\resources\service"
$legacyResourceRoot = Join-Path $appDirectory "src-tauri\resources\legacy"
$skillsSourceRoot = Join-Path $appDirectory "packaging\skills"
$skillsResourceRoot = Join-Path $appDirectory "src-tauri\resources\skills"
$runtimeCatalogSourceRoot = Join-Path $appDirectory "packaging\runtime"
$runtimeCatalogResourceRoot = Join-Path $appDirectory "src-tauri\resources\runtime"
$frontendResourceRoot = Join-Path $appDirectory "src-tauri\resources\web"
$nativeTargetBinary = Join-Path $nativeResourceRoot "codex-home-manager-service.exe"
$legacyTargetBinary = Join-Path $legacyResourceRoot "codex-home-manager-legacy.exe"
$providerInteropSkillMarker = Join-Path $skillsResourceRoot "codex-provider-interoperability\SKILL.md"
$runtimeCatalogMarker = Join-Path $runtimeCatalogResourceRoot "provider-interop-runtime-0.154.0-p1.json"
$maintenanceCatalogMarker = Join-Path $runtimeCatalogResourceRoot "cockpit-maintenance.json"
$iconPath = Join-Path $appDirectory "packaging\windows\assets\codex-home-manager.ico"
$versionInfoPath = Join-Path $appDirectory "packaging\windows\version_info.txt"
$entryPath = Join-Path $appDirectory "packaging\windows\connector_main.py"
$packagingPython = Join-Path $appDirectory "build\local-connector\.venv\Scripts\python.exe"
$pyinstallerRunner = Join-Path $appDirectory "build\local-connector\run_reproducible_pyinstaller.py"

$resourcePrefix = [System.IO.Path]::GetFullPath((Join-Path $appDirectory "src-tauri\resources")) + [System.IO.Path]::DirectorySeparatorChar
foreach ($resourcePath in @($nativeResourceRoot, $legacyResourceRoot, $skillsResourceRoot, $runtimeCatalogResourceRoot, $frontendResourceRoot)) {
    if (-not [System.IO.Path]::GetFullPath($resourcePath).StartsWith($resourcePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Resource staging path escapes the workspace" }
}
$buildFullPath = [System.IO.Path]::GetFullPath($buildRoot)
$allowedBuildPrefixes = @((Join-Path $appDirectory "build"), "D:\ZFiles\CXProject\Codex")
if (-not ($allowedBuildPrefixes | Where-Object { $buildFullPath.StartsWith([System.IO.Path]::GetFullPath($_) + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) })) { throw "Build staging path is outside approved artifact roots" }

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

$frontendArtifactScript = Join-Path $appDirectory "scripts\frontend-release-artifact.mjs"
$sourceFingerprint = & node.exe $frontendArtifactScript source-digest desktop
if ($LASTEXITCODE -ne 0) { throw "Failed to freeze desktop source inputs" }
$frontendMode = if ($SkipFrontendBuild) { "verify" } else { "build" }
& node.exe $frontendArtifactScript $frontendMode
if ($LASTEXITCODE -ne 0) {
    throw "Frontend build receipt is unavailable or stale; rebuild without SkipFrontendBuild"
}

$frontendDist = Join-Path $appDirectory "build\frontend-release"
if (-not (Test-Path -LiteralPath $frontendDist -PathType Container)) {
    throw "Frontend release artifact is missing: $frontendDist"
}

New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
if (Test-Path -LiteralPath $frontendResourceRoot -PathType Container) {
    if (Test-Path -LiteralPath (Join-Path $buildRoot "previous-web-resource")) { throw 'Previous web staging slot is occupied; retire the duplicate through the admitted lifecycle before resuming' }
    Move-Item -LiteralPath $frontendResourceRoot -Destination (Join-Path $buildRoot "previous-web-resource")
}
New-Item -ItemType Directory -Force -Path $frontendResourceRoot | Out-Null
Copy-Item -Path (Join-Path $frontendDist "*") -Destination $frontendResourceRoot -Recurse -Force

New-Item -ItemType Directory -Force -Path $legacyDistRoot, $legacyWorkRoot, $legacySpecRoot | Out-Null

& cargo build --locked --manifest-path (Join-Path $appDirectory "src-tauri\Cargo.toml") -p codex-home-manager-service --release
if ($LASTEXITCODE -ne 0) {
    throw "Rust native service build failed"
}
$builtNativeBinary = Join-Path ([IO.Path]::GetFullPath($env:CARGO_TARGET_DIR)) "release\codex-home-manager-service.exe"
if (-not (Test-Path -LiteralPath $builtNativeBinary -PathType Leaf)) {
    throw "Rust native service output is missing: $builtNativeBinary"
}

$requirements = Join-Path $appDirectory 'packaging\windows\requirements-connector.txt'
if (-not (Test-Path -LiteralPath $packagingPython -PathType Leaf)) {
    & 'D:\Software\anaconda3\python.exe' -I -m venv (Split-Path -Parent (Split-Path -Parent $packagingPython))
    if ($LASTEXITCODE -ne 0) { throw 'Failed to restore the isolated connector packaging environment' }
}
$pipCache = 'D:\ZFiles\CXProject\Codex\cache\connector-python'
& $packagingPython -I -m pip install --require-hashes --only-binary=:all: --cache-dir $pipCache -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'Locked connector dependencies could not be verified' }
& $packagingPython -I -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Isolated connector dependency check failed' }
# Reuse the existing reproducibility runner without executing its publication script.
$parseTokens = $null
$parseErrors = $null
$packagingAst = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'package-local-connector.ps1'), [ref]$parseTokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Connector packaging source could not be parsed' }
$runnerDefinition = $packagingAst.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Write-PyInstallerReproducibilityRunner' }, $false)
if (-not $runnerDefinition) { throw 'The existing connector reproducibility runner is missing' }
$pyinstallerRunnerPath = $pyinstallerRunner
& ($runnerDefinition.Body.GetScriptBlock())
if (-not (Test-Path -LiteralPath $pyinstallerRunner -PathType Leaf)) { throw 'Connector reproducibility runner was not restored' }
$packagingCommand = $packagingPython
$pyinstallerArguments = @('-I', $pyinstallerRunner)
$pyinstallerArguments += @(
    "--noconfirm",
    "--name", "codex-home-manager-legacy",
    "--onedir",
    "--windowed",
    "--icon", $iconPath,
    "--version-file", $versionInfoPath,
    "--distpath", $legacyDistRoot,
    "--workpath", $legacyWorkRoot,
    "--specpath", $legacySpecRoot,
    "--add-data", "$frontendDist;dist",
    "--add-data", "$skillsSourceRoot;packaging/skills",
    "--add-data", "$runtimeCatalogSourceRoot;packaging/runtime",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "uvicorn.lifespan.on",
    "--paths", $appDirectory,
    "--paths", (Join-Path $appDirectory "packaging\windows"),
    $entryPath
)

& $packagingCommand @pyinstallerArguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to build the legacy compatibility service"
}

$builtLegacyDirectory = Join-Path $legacyDistRoot "codex-home-manager-legacy"
$builtLegacyBinary = Join-Path $builtLegacyDirectory "codex-home-manager-legacy.exe"
if (-not (Test-Path -LiteralPath $builtLegacyBinary -PathType Leaf)) {
    throw "Legacy compatibility service output is missing: $builtLegacyBinary"
}

$supersededRoot = Join-Path $buildRoot "superseded-resources"
New-Item -ItemType Directory -Force -Path $supersededRoot | Out-Null
if (Test-Path -LiteralPath $nativeResourceRoot -PathType Container) {
    Move-Item -LiteralPath $nativeResourceRoot -Destination (Join-Path $supersededRoot "service")
}
if (Test-Path -LiteralPath $legacyResourceRoot -PathType Container) {
    Move-Item -LiteralPath $legacyResourceRoot -Destination (Join-Path $supersededRoot "legacy")
}
if (Test-Path -LiteralPath $skillsResourceRoot -PathType Container) {
    Move-Item -LiteralPath $skillsResourceRoot -Destination (Join-Path $supersededRoot "skills")
}
if (Test-Path -LiteralPath $runtimeCatalogResourceRoot -PathType Container) {
    Move-Item -LiteralPath $runtimeCatalogResourceRoot -Destination (Join-Path $supersededRoot "runtime")
}
New-Item -ItemType Directory -Force -Path $nativeResourceRoot, $legacyResourceRoot, $skillsResourceRoot, $runtimeCatalogResourceRoot | Out-Null
Copy-Item -LiteralPath $builtNativeBinary -Destination $nativeTargetBinary -Force
Copy-Item -Path (Join-Path $builtLegacyDirectory "*") -Destination $legacyResourceRoot -Recurse -Force
Copy-Item -Path (Join-Path $skillsSourceRoot "*") -Destination $skillsResourceRoot -Recurse -Force
Copy-Item -Path (Join-Path $runtimeCatalogSourceRoot "*") -Destination $runtimeCatalogResourceRoot -Recurse -Force

$currentSourceFingerprint = & node.exe $frontendArtifactScript source-digest desktop
if ($LASTEXITCODE -ne 0 -or $currentSourceFingerprint -ne $sourceFingerprint) { throw "Desktop inputs changed during the sidecar build" }
foreach ($marker in @($providerInteropSkillMarker, $runtimeCatalogMarker, $maintenanceCatalogMarker)) {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { throw "Required built resource is missing: $marker" }
}
& node.exe $frontendArtifactScript desktop-artifacts
if ($LASTEXITCODE -ne 0) { throw "Built sidecar resource trees or embedded catalogs failed verification" }

Write-Output "Native service: $nativeTargetBinary"
Write-Output "Native SHA256: $(Get-Sha256Hex -Path $nativeTargetBinary)"
Write-Output "Legacy compatibility service: $legacyTargetBinary"
Write-Output "Legacy SHA256: $(Get-Sha256Hex -Path $legacyTargetBinary)"
Write-Output "Bundled skills: $skillsResourceRoot"
Write-Output "Runtime compatibility catalog: $runtimeCatalogResourceRoot"
