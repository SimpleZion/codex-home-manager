param(
    [string]$CodexHome = "D:\.codex",
    [string[]]$RequiredPlugins = @("sites", "browser", "chrome", "computer-use", "latex"),
    [string]$BackupRoot = "D:\Backup\codex_home_manager\backups",
    [switch]$CheckAppleRemoteMarkersOnly
)

$ErrorActionPreference = "Stop"

function Resolve-SafeContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RequiredRoot
    )

    $validator = "from pathlib import Path; import sys; candidate=Path(sys.argv[1]).resolve(strict=False); root=Path(sys.argv[2]).resolve(strict=False); candidate.relative_to(root); print(candidate)"
    $resolved = (& python -c $validator $Path $RequiredRoot | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($resolved)) {
        throw "Path escapes the required root after resolving reparse points: $Path"
    }
    return $resolved
}

function Assert-CodexOffline {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $name = [string]$_.Name
        $commandLine = ([string]$_.CommandLine).ToLowerInvariant().Replace("/", "\")
        $name -ieq "ChatGPT.exe" -or
        $name -ieq "Codex.exe" -or
        $name -ieq "node_repl.exe" -or
        $name -ieq "codex-code-mode-host.exe" -or
        $name -ieq "codex-command-runner.exe" -or
        $name -ieq "codex-home-manager-local-win-x64.exe" -or
        $commandLine.Contains("xcodebuildmcp") -or
        $commandLine.Contains("mcp\server.mjs") -or
        $commandLine.Contains("mcp\server.bundle.mjs") -or
        $commandLine.Contains("mcp\server.cjs") -or
        ($commandLine.Contains("\.codex\plugins\") -and $commandLine.Contains("extension-host"))
    })
    if ($processes.Count -gt 0) {
        throw "Codex must remain fully offline during plugin repair: $($processes.ProcessId -join ', ')"
    }
}

function Assert-BackupRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    $requiredRoot = [System.IO.Path]::GetFullPath("D:\Backup").TrimEnd('\')
    $null = Resolve-SafeContainedPath -Path $Path -RequiredRoot $requiredRoot
}

function Resolve-CodexExe {
    $candidates = New-Object System.Collections.Generic.List[string]
    $pluginAppServerPath = Join-Path $CodexHome "plugins\.plugin-appserver\codex.exe"
    $candidates.Add($pluginAppServerPath)
    $configPath = Join-Path $CodexHome "config.toml"
    if (Test-Path -LiteralPath $configPath) {
        $configText = [System.IO.File]::ReadAllText($configPath)
        $match = [regex]::Match($configText, '(?m)^\s*CODEX_CLI_PATH\s*=\s*["'']([^"'']+)["'']')
        if ($match.Success) {
            $candidates.Add($match.Groups[1].Value)
        }
    }

    $localBinRoot = Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin"
    if (Test-Path -LiteralPath $localBinRoot) {
        Get-ChildItem -LiteralPath $localBinRoot -Recurse -Filter codex.exe -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            ForEach-Object { $candidates.Add($_.FullName) }
    }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    throw "Could not find codex.exe under CODEX_HOME config or the local Codex bin directory."
}

function Resolve-BundledMarketplaceSource {
    $candidateSources = New-Object System.Collections.Generic.List[string]

    Get-Process -Name Codex -ErrorAction SilentlyContinue |
        Where-Object { $_.Path } |
        ForEach-Object {
            $appRoot = Split-Path -Parent $_.Path
            $candidateSources.Add((Join-Path $appRoot "plugins\openai-bundled"))
            $candidateSources.Add((Join-Path $appRoot "resources\plugins\openai-bundled"))
        }

    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (Test-Path -LiteralPath $windowsPowerShell) {
        $appxQuery = "Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop | Where-Object InstallLocation | Sort-Object Version -Descending | ForEach-Object { `$_.InstallLocation } | ConvertTo-Json -Compress"
        $appxJson = (& $windowsPowerShell -NoProfile -NonInteractive -Command $appxQuery | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $appxJson) {
            foreach ($installLocation in @($appxJson | ConvertFrom-Json)) {
                $candidateSources.Add((Join-Path ([string]$installLocation) "app\resources\plugins\openai-bundled"))
            }
        }
    }

    Get-ChildItem -LiteralPath "C:\Program Files\WindowsApps" -Directory -Filter "OpenAI.Codex_*_x64__2p2nqsd0c76g0" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        ForEach-Object {
            $candidateSources.Add((Join-Path $_.FullName "app\resources\plugins\openai-bundled"))
        }

    $source = $candidateSources |
        Select-Object -Unique |
        Where-Object {
            Test-Path -LiteralPath (Join-Path $_ ".agents\plugins\marketplace.json")
        } |
        Select-Object -First 1

    if (-not $source) {
        throw "Could not find the bundled openai-bundled marketplace in WindowsApps."
    }

    return $source
}

function Backup-File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Reason
    )

    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
    $backupDirectory = Join-Path $BackupRoot "${timestamp}_repair_codex_bundled_plugins_$Reason"
    New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
    $backupPath = Join-Path $backupDirectory (Split-Path -Leaf $Path)
    Copy-Item -LiteralPath $Path -Destination $backupPath -Force
    return $backupPath
}

function Invoke-RobocopyMirrorWithoutDelete {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string[]]$RequiredRelativePathsAfterFailure = @()
    )

    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Source path does not exist: $Source"
    }
    if (-not (Test-Path -LiteralPath $Destination)) {
        Assert-CodexOffline
        New-Item -ItemType Directory -Path $Destination | Out-Null
    }

    Assert-CodexOffline
    robocopy $Source $Destination /E /IS /IT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    $exitCode = $LASTEXITCODE
    if ($exitCode -gt 7) {
        throw "robocopy failed from '$Source' to '$Destination' with exit code $exitCode"
    }

    $invalidRequiredPaths = New-Object System.Collections.Generic.List[string]
    foreach ($relativePath in $RequiredRelativePathsAfterFailure) {
        $sourceRequiredPath = Join-Path $Source $relativePath
        $destinationRequiredPath = Join-Path $Destination $relativePath
        if (-not (Test-Path -LiteralPath $sourceRequiredPath) -or -not (Test-Path -LiteralPath $destinationRequiredPath)) {
            $invalidRequiredPaths.Add("missing: $relativePath")
            continue
        }
        $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourceRequiredPath).Hash
        $destinationHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $destinationRequiredPath).Hash
        if ($sourceHash -ne $destinationHash) {
            $invalidRequiredPaths.Add("hash mismatch: $relativePath")
        }
    }

    if ($invalidRequiredPaths.Count -gt 0) {
        throw ("robocopy finished from '$Source' to '$Destination', but required files failed verification:`n" + ($invalidRequiredPaths -join "`n"))
    }
}

function Get-PluginTreeManifest {
    param([Parameter(Mandatory = $true)][string]$Root)

    $manifest = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Root)) {
        return ,$manifest
    }
    $manifest["."] = [ordered]@{ type = "directory"; bytes = 0; sha256 = ""; target = "" }
    foreach ($item in Get-ChildItem -LiteralPath $Root -Recurse -Force) {
        $relativePath = [System.IO.Path]::GetRelativePath($Root, $item.FullName).Replace('/', '\')
        if ($item.LinkType -in @("Junction", "SymbolicLink")) {
            $manifest[$relativePath] = [ordered]@{
                type = $item.LinkType.ToLowerInvariant()
                bytes = 0
                sha256 = ""
                target = [string](@($item.Target)[0])
            }
        }
        elseif ($item.PSIsContainer) {
            $manifest[$relativePath] = [ordered]@{ type = "directory"; bytes = 0; sha256 = ""; target = "" }
        }
        elseif ($item -is [System.IO.FileInfo]) {
            $manifest[$relativePath] = [ordered]@{
                type = "file"
                bytes = [int64]$item.Length
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $item.FullName).Hash.ToLowerInvariant()
                target = ""
            }
        }
        else {
            $manifest[$relativePath] = [ordered]@{ type = "other"; bytes = 0; sha256 = ""; target = "" }
        }
    }
    return ,$manifest
}

function Compare-PluginTreeManifest {
    param(
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$SourceManifest,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$DestinationManifest
    )

    $differences = New-Object System.Collections.Generic.List[string]
    $keys = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($key in $SourceManifest.Keys) { $null = $keys.Add([string]$key) }
    foreach ($key in $DestinationManifest.Keys) { $null = $keys.Add([string]$key) }
    foreach ($key in $keys) {
        if (-not $SourceManifest.Contains($key)) {
            $differences.Add("extra: $key")
            continue
        }
        if (-not $DestinationManifest.Contains($key)) {
            $differences.Add("missing: $key")
            continue
        }
        $sourceEntry = $SourceManifest[$key]
        $destinationEntry = $DestinationManifest[$key]
        foreach ($property in @("type", "bytes", "sha256", "target")) {
            if ([string]$sourceEntry[$property] -cne [string]$destinationEntry[$property]) {
                $differences.Add("$property mismatch: $key")
                break
            }
        }
    }
    return @($differences)
}

function Ensure-PluginCacheComplete {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string[]]$RequiredRelativePaths,
        [switch]$AllowDestinationRootJunction
    )

    $sourceItem = Get-Item -LiteralPath $Source -Force -ErrorAction Stop
    if ($sourceItem.LinkType -in @("Junction", "SymbolicLink") -or -not $sourceItem.PSIsContainer) {
        throw "Plugin source root must be a regular directory: $Source"
    }
    $sourceManifest = Get-PluginTreeManifest -Root $Source
    $allSourceRelativePaths = @($sourceManifest.Keys | Where-Object { $sourceManifest[$_].type -eq "file" })
    if ($allSourceRelativePaths.Count -eq 0) {
        throw "Plugin source contains no files: $Source"
    }
    $sourcePathSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($relativePath in $allSourceRelativePaths) {
        $null = $sourcePathSet.Add($relativePath)
    }
    foreach ($requiredRelativePath in $RequiredRelativePaths) {
        if (-not $sourcePathSet.Contains($requiredRelativePath)) {
            throw "Plugin source is incomplete: $(Join-Path $Source $requiredRelativePath)"
        }
    }

    $destinationManifest = Get-PluginTreeManifest -Root $Destination
    $invalidBeforeCopy = @(Compare-PluginTreeManifest -SourceManifest $sourceManifest -DestinationManifest $destinationManifest)
    $destinationItem = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if (
        $null -ne $destinationItem -and
        $destinationItem.LinkType -in @("Junction", "SymbolicLink") -and
        -not $AllowDestinationRootJunction
    ) {
        $invalidBeforeCopy += "root type mismatch: destination must be a regular directory"
    }

    if ($invalidBeforeCopy.Count -eq 0) {
        Write-Output "Plugin cache already matches the bundled source: $Destination"
        return
    }

    if (
        $null -ne $destinationItem -and
        $destinationItem.LinkType -in @("Junction", "SymbolicLink") -and
        $AllowDestinationRootJunction
    ) {
        throw "Junction target is inconsistent with bundled source and cannot be rebuilt in place: $Destination"
    }

    Write-Output "Plugin cache differs from the bundled source, rebuilding exact tree: $Destination"
    Write-Output "Invalid before rebuild: $($invalidBeforeCopy -join ', ')"
    if ($null -ne $destinationItem) {
        $timestamp = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
        $archiveDirectory = Join-Path $BackupRoot "${timestamp}_repair_codex_bundled_plugins_exact_tree"
        New-Item -ItemType Directory -Path $archiveDirectory -Force | Out-Null
        $archivedPath = Join-Path $archiveDirectory (Split-Path -Leaf $Destination)
        Assert-CodexOffline
        Move-Item -LiteralPath $Destination -Destination $archivedPath
        Write-Output "Archived inconsistent plugin tree before rebuild: $archivedPath"
    }
    Invoke-RobocopyMirrorWithoutDelete -Source $Source -Destination $Destination -RequiredRelativePathsAfterFailure $allSourceRelativePaths

    $invalidAfterCopy = @(Compare-PluginTreeManifest -SourceManifest $sourceManifest -DestinationManifest (Get-PluginTreeManifest -Root $Destination))

    if ($invalidAfterCopy.Count -gt 0) {
        throw ("Plugin cache remains inconsistent after copy:`n" + ($invalidAfterCopy -join "`n"))
    }
}

function Read-PluginVersion {
    param([Parameter(Mandatory = $true)][string]$PluginRoot)

    $manifestPath = Join-Path $PluginRoot ".codex-plugin\plugin.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "Missing plugin manifest: $manifestPath"
    }

    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    return [string]$manifest.version
}

function Ensure-Junction {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Target
    )

    $existing = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        if ($existing.LinkType -eq "Junction") {
            $existingTarget = [string](@($existing.Target)[0])
            if ($existingTarget -and (Test-Path -LiteralPath $existingTarget)) {
                $resolvedExistingTarget = (Resolve-Path -LiteralPath $existingTarget).Path
                $resolvedTarget = (Resolve-Path -LiteralPath $Target).Path
                if ($resolvedExistingTarget -ieq $resolvedTarget) {
                    return
                }
            }
        }

        $timestamp = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
        $archiveDirectory = Join-Path $BackupRoot "${timestamp}_repair_codex_bundled_plugins_latest_path"
        New-Item -ItemType Directory -Path $archiveDirectory -Force | Out-Null
        $archivedPath = Join-Path $archiveDirectory (Split-Path -Leaf $Path)
        Assert-CodexOffline
        Move-Item -LiteralPath $Path -Destination $archivedPath
        Assert-CodexOffline
        New-Item -ItemType Junction -Path $Path -Target $Target | Out-Null
        Write-Output "Replaced latest plugin path: $Path -> $Target (previous path: $archivedPath)"
        return
    }

    Assert-CodexOffline
    New-Item -ItemType Junction -Path $Path -Target $Target | Out-Null
}

function Invoke-CodexCommand {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    Assert-CodexOffline
    & $codexExe @Arguments | Out-Host
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "codex $($Arguments -join ' ') failed with exit code $exitCode"
    }
}

function Repair-KnownCuratedManifestWarnings {
    param([Parameter(Mandatory = $true)][string]$CodexHomePath)

    $ngsManifestPath = Join-Path $CodexHomePath ".tmp\plugins\plugins\ngs-analysis\.codex-plugin\plugin.json"
    if (-not (Test-Path -LiteralPath $ngsManifestPath)) {
        return
    }

    $manifest = Get-Content -LiteralPath $ngsManifestPath -Raw | ConvertFrom-Json
    if (-not $manifest.interface.defaultPrompt -or $manifest.interface.defaultPrompt.Count -eq 0) {
        return
    }

    $prompt = [string]$manifest.interface.defaultPrompt[0]
    if ($prompt.Length -le 128) {
        return
    }

    $manifest.interface.defaultPrompt[0] = "Guide NGS intake, inspect BCL/FASTQ/count inputs, choose a public pipeline, and run validated local workflows."
    Assert-CodexOffline
    $manifest | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath $ngsManifestPath -Encoding UTF8
    Write-Output "Shortened ngs-analysis defaultPrompt to satisfy the 128-character manifest limit."
}

function Repair-CuratedAgentIconWarnings {
    param([Parameter(Mandatory = $true)][string]$CodexHomePath)

    $searchRoots = @(
        (Join-Path $CodexHomePath ".tmp\plugins\plugins"),
        (Join-Path $CodexHomePath "plugins\cache")
    ) | Where-Object { Test-Path -LiteralPath $_ }

    if (-not $searchRoots) {
        return
    }

    $yamlFiles = Get-ChildItem -LiteralPath $searchRoots -Recurse -Filter openai.yaml -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FullName -match "\\agents\\openai\.yaml$" -and
            (Select-String -LiteralPath $_.FullName -Pattern "icon_(small|large):.*\.\." -Quiet)
        }

    $updatedFiles = 0
    $copiedAssets = 0
    foreach ($yamlFile in $yamlFiles) {
        $yamlPath = $yamlFile.FullName
        $agentDir = Split-Path -Parent $yamlPath
        $skillRoot = Split-Path -Parent $agentDir
        $skillAssetDir = Join-Path $skillRoot "assets"
        $content = [System.IO.File]::ReadAllText($yamlPath)
        $repairState = [pscustomobject]@{
            FileChanged = $false
            CopiedAssets = 0
        }

        $newContent = [regex]::Replace(
            $content,
            "(?m)^(\s*icon_(?:small|large):\s*)([`"']?)([^`"'\r\n#]+)([`"']?)",
            [System.Text.RegularExpressions.MatchEvaluator]{
                param($match)

                $prefix = $match.Groups[1].Value
                $iconPath = $match.Groups[3].Value.Trim()
                if ($iconPath -notmatch "\.\.") {
                    return $match.Value
                }

                $sourceCandidates = @(
                    [System.IO.Path]::GetFullPath((Join-Path $skillRoot $iconPath)),
                    [System.IO.Path]::GetFullPath((Join-Path $agentDir $iconPath))
                ) | Select-Object -Unique
                $sourcePath = $sourceCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
                if (-not $sourcePath) {
                    Write-Warning "Could not resolve icon asset '$iconPath' for $yamlPath"
                    return $match.Value
                }

                if (-not (Test-Path -LiteralPath $skillAssetDir)) {
                    Assert-CodexOffline
                    New-Item -ItemType Directory -Path $skillAssetDir | Out-Null
                }

                $assetName = Split-Path -Leaf $sourcePath
                $destinationPath = Join-Path $skillAssetDir $assetName
                if (([System.IO.Path]::GetFullPath($sourcePath)) -ne ([System.IO.Path]::GetFullPath($destinationPath))) {
                    $shouldCopy = $true
                    if (Test-Path -LiteralPath $destinationPath) {
                        $sourceItem = Get-Item -LiteralPath $sourcePath
                        $destinationItem = Get-Item -LiteralPath $destinationPath
                        $shouldCopy = $sourceItem.Length -ne $destinationItem.Length
                    }

                    if ($shouldCopy) {
                        Assert-CodexOffline
                        Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force
                        $repairState.CopiedAssets += 1
                    }
                }

                $repairState.FileChanged = $true
                return "$prefix`"./assets/$assetName`""
            }
        )

        if ($repairState.FileChanged -and $newContent -ne $content) {
            Assert-CodexOffline
            [System.IO.File]::WriteAllText($yamlPath, $newContent, [System.Text.UTF8Encoding]::new($false))
            $updatedFiles += 1
        }
        $copiedAssets += $repairState.CopiedAssets
    }

    if ($updatedFiles -gt 0 -or $copiedAssets -gt 0) {
        Write-Output "Repaired curated agent icon metadata: updatedFiles=$updatedFiles copiedAssets=$copiedAssets"
    }
}

function Ensure-ManagedBundledPluginConfig {
    param(
        [Parameter(Mandatory = $true)][string]$CodexHomePath,
        [Parameter(Mandatory = $true)][string[]]$DisabledPluginNames
    )

    $managedConfigPath = Join-Path $CodexHomePath "managed_config.toml"
    $mergeScriptPath = Join-Path $PSScriptRoot "merge_codex_managed_config.py"
    if (-not (Test-Path -LiteralPath $mergeScriptPath)) {
        throw "Managed config merge helper is missing: $mergeScriptPath"
    }

    if (Test-Path -LiteralPath $managedConfigPath) {
        $backupPath = Backup-File -Path $managedConfigPath -Reason "bundled_plugin_managed_config"
        Write-Output "Backed up existing managed_config.toml: $backupPath"
    }

    $mergeArguments = @(
        $mergeScriptPath,
        "--path", $managedConfigPath,
        "--plugins", "sites", "browser", "chrome", "computer-use", "latex"
    )
    foreach ($pluginName in $DisabledPluginNames) {
        $mergeArguments += @("--disable-plugin", $pluginName)
    }
    $mergeArguments += @("--disable-mcp-server", "codex_thread_messenger")
    $mergeArguments += @("--remove-marketplace", "openai-bundled")
    $mergeArguments += @("--remove-marketplace", "openai-primary-runtime")
    & python @mergeArguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Managed config merge failed with exit code $LASTEXITCODE"
    }
    Write-Output "Merged bundled plugin config without replacing unrelated managed settings: $managedConfigPath"
}

function Ensure-RuntimeConfig {
    param(
        [Parameter(Mandatory = $true)][string]$CodexHomePath,
        [Parameter(Mandatory = $true)][string[]]$PluginNames,
        [string]$BundledMarketplacePath = "",
        [Parameter(Mandatory = $true)][string]$AppxResourcesPath,
        [string[]]$DisabledPluginNames = @(),
        [switch]$ReleaseDesktopMarketplaceOwnership
    )

    $configPath = Join-Path $CodexHomePath "config.toml"
    $mergeScriptPath = Join-Path $PSScriptRoot "merge_codex_runtime_config.py"
    if (-not (Test-Path -LiteralPath $mergeScriptPath)) {
        throw "Runtime config merge helper is missing: $mergeScriptPath"
    }
    $backupPath = Backup-File -Path $configPath -Reason "runtime_config_structured_merge"
    $mergeArguments = @(
        $mergeScriptPath,
        "--path", $configPath,
        "--appx-resources", $AppxResourcesPath
    )
    if ($ReleaseDesktopMarketplaceOwnership) {
        $mergeArguments += @("--remove-marketplace", "openai-bundled")
    }
    else {
        if ([string]::IsNullOrWhiteSpace($BundledMarketplacePath)) {
            throw "BundledMarketplacePath is required while registering plugins."
        }
        $mergeArguments += @("--bundled-marketplace", $BundledMarketplacePath)
        $mergeArguments += @("--remove-marketplace", "openai-bundled")
    }
    $mergeArguments += @("--remove-marketplace", "openai-primary-runtime")
    foreach ($pluginName in $PluginNames) {
        $mergeArguments += @("--bundled-plugin", $pluginName)
    }
    foreach ($pluginName in $DisabledPluginNames) {
        $mergeArguments += @("--disable-plugin", $pluginName)
    }
    $mergeArguments += @("--disable-mcp-server", "codex_thread_messenger")
    & python @mergeArguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Runtime config merge failed with exit code $LASTEXITCODE"
    }
    Write-Output "Merged runtime config without replacing unrelated settings. Backup: $backupPath"
}

function Get-InstalledBundledPluginIds {
    $result = Get-BundledPluginRegistry
    $installedIds = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($plugin in $result.installed) {
        if ($plugin.installed -and $plugin.enabled) {
            $null = $installedIds.Add([string]$plugin.pluginId)
        }
    }
    Write-Output -NoEnumerate $installedIds
}

function Get-BundledPluginRegistry {
    $json = (& $codexExe plugin list --marketplace openai-bundled --available --json | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "codex plugin list --marketplace openai-bundled --available --json failed with exit code $LASTEXITCODE"
    }

    $result = $json | ConvertFrom-Json
    Write-Output -NoEnumerate $result
}

function Get-CuratedPluginRegistry {
    $json = (& $codexExe plugin list --marketplace openai-curated --available --json | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "codex plugin list --marketplace openai-curated --available --json failed with exit code $LASTEXITCODE"
    }

    $result = $json | ConvertFrom-Json
    Write-Output -NoEnumerate $result
}

function Ensure-WindowsIncompatiblePluginsRemoved {
    param(
        [Parameter(Mandatory = $true)][string]$CodexHomePath,
        [Parameter(Mandatory = $true)][string[]]$PluginSelectors
    )

    $registry = Get-CuratedPluginRegistry
    foreach ($pluginSelector in $PluginSelectors) {
        $entry = @($registry.installed | Where-Object { [string]$_.pluginId -ieq $pluginSelector }) | Select-Object -First 1
        if ($null -eq $entry) {
            $entry = @($registry.available | Where-Object { [string]$_.pluginId -ieq $pluginSelector }) | Select-Object -First 1
        }
        if ($null -eq $entry -or (-not $entry.installed -and -not $entry.enabled)) {
            Write-Output "Windows-incompatible plugin is already absent: $pluginSelector"
            continue
        }

        Write-Output "Removing Windows-incompatible plugin through the official Codex plugin registry: $pluginSelector"
        Invoke-CodexCommand -Arguments @("plugin", "remove", $pluginSelector, "--json")
    }

    $verifiedRegistry = Get-CuratedPluginRegistry
    foreach ($pluginSelector in $PluginSelectors) {
        $allEntries = @($verifiedRegistry.installed) + @($verifiedRegistry.available)
        $activeEntries = @($allEntries | Where-Object {
                [string]$_.pluginId -ieq $pluginSelector -and ($_.installed -or $_.enabled)
            })
        if ($activeEntries.Count -gt 0) {
            throw "Windows-incompatible plugin remains installed or enabled after official removal: $pluginSelector"
        }

    }
    Assert-NoWindowsIncompatibleRemoteInstallMarkers -CodexHomePath $CodexHomePath -PluginSelectors $PluginSelectors
}

function Assert-NoWindowsIncompatibleRemoteInstallMarkers {
    param(
        [Parameter(Mandatory = $true)][string]$CodexHomePath,
        [Parameter(Mandatory = $true)][string[]]$PluginSelectors
    )

    foreach ($pluginSelector in $PluginSelectors) {
        $pluginName = $pluginSelector.Split('@')[0]
        $remoteInstallMarker = Join-Path $CodexHomePath "plugins\cache\openai-curated-remote\$pluginName\.codex-remote-plugin-install.json"
        if (Test-Path -LiteralPath $remoteInstallMarker -PathType Leaf) {
            throw "Account-synced remote plugin marker remains for $pluginSelector. Uninstall it from the signed-in Codex Plugins UI before running offline repair: $remoteInstallMarker"
        }
    }
}

function Assert-BundledPluginRegistryEntry {
    param(
        [Parameter(Mandatory = $true)][object]$Registry,
        [Parameter(Mandatory = $true)][string]$PluginName,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$ExpectedPluginSource,
        [Parameter(Mandatory = $true)][string]$ExpectedMarketplaceSource
    )

    $pluginSelector = "$PluginName@openai-bundled"
    $entry = @($Registry.installed | Where-Object { [string]$_.pluginId -ieq $pluginSelector }) | Select-Object -First 1
    if ($null -eq $entry) {
        throw "Official plugin registry entry is missing: $pluginSelector"
    }
    if (-not $entry.installed -or -not $entry.enabled) {
        throw "Official plugin registry entry is not installed and enabled: $pluginSelector"
    }
    if ([string]$entry.version -ne $ExpectedVersion) {
        throw "Official plugin registry version mismatch for ${pluginSelector}: $($entry.version) != $ExpectedVersion"
    }
    if ([string]$entry.source.source -ne "local" -or [string]$entry.marketplaceSource.sourceType -ne "local") {
        throw "Official plugin registry source type is not local for $pluginSelector"
    }
    $actualPluginSource = [System.IO.Path]::GetFullPath([string]$entry.source.path).TrimEnd('\')
    $actualMarketplaceSource = [System.IO.Path]::GetFullPath([string]$entry.marketplaceSource.source).TrimEnd('\')
    $expectedPluginPath = [System.IO.Path]::GetFullPath($ExpectedPluginSource).TrimEnd('\')
    $expectedMarketplacePath = [System.IO.Path]::GetFullPath($ExpectedMarketplaceSource).TrimEnd('\')
    if ($actualPluginSource -ine $expectedPluginPath) {
        throw "Official plugin registry plugin source mismatch for ${pluginSelector}: $actualPluginSource != $expectedPluginPath"
    }
    if ($actualMarketplaceSource -ine $expectedMarketplacePath) {
        throw "Official plugin registry marketplace source mismatch for ${pluginSelector}: $actualMarketplaceSource != $expectedMarketplacePath"
    }
}

$codexHomePath = (Resolve-Path -LiteralPath $CodexHome).Path
$windowsIncompatiblePluginSelectors = @(
    "build-ios-apps@openai-curated",
    "build-macos-apps@openai-curated"
)
Assert-NoWindowsIncompatibleRemoteInstallMarkers -CodexHomePath $codexHomePath -PluginSelectors $windowsIncompatiblePluginSelectors
if ($CheckAppleRemoteMarkersOnly) {
    Write-Output "No account-synced build-ios/build-macos remote plugin markers were found."
    return
}

Assert-BackupRoot -Path $BackupRoot
Assert-CodexOffline
$persistentMarketplace = Join-Path $codexHomePath "cache\bundled-marketplaces\openai-bundled"
$temporaryMarketplace = Join-Path $codexHomePath ".tmp\bundled-marketplaces\openai-bundled"
$registrationMarketplace = $temporaryMarketplace
$pluginCacheRoot = Join-Path $codexHomePath "plugins\cache\openai-bundled"
$codexExe = Resolve-CodexExe
$sourceMarketplace = Resolve-BundledMarketplaceSource
$appxResourcesPath = Split-Path -Parent (Split-Path -Parent $sourceMarketplace)
$pluginRuntimeKeyPaths = @{
    "browser"      = @("skills\control-in-app-browser\SKILL.md", "scripts\browser-client.mjs")
    "chrome"       = @("skills\control-chrome\SKILL.md", "scripts\browser-client.mjs", "extension-host\windows\x64\extension-host.exe")
    "computer-use" = @("skills\computer-use\SKILL.md", "scripts\computer-use-client.mjs")
    "latex"        = @("skills\latex-compile\SKILL.md")
    "sites"        = @("skills\sites-hosting\SKILL.md")
}
$env:CODEX_HOME = $codexHomePath

Write-Output "CodexHome=$codexHomePath"
Write-Output "CodexExe=$codexExe"
Write-Output "SourceMarketplace=$sourceMarketplace"

Ensure-PluginCacheComplete -Source $sourceMarketplace -Destination $persistentMarketplace -RequiredRelativePaths @(".agents\plugins\marketplace.json")
Ensure-PluginCacheComplete -Source $persistentMarketplace -Destination $temporaryMarketplace -RequiredRelativePaths @(".agents\plugins\marketplace.json")

foreach ($pluginName in $RequiredPlugins) {
    $sourcePluginRoot = Join-Path $persistentMarketplace "plugins\$pluginName"
    $temporaryPluginRoot = Join-Path $temporaryMarketplace "plugins\$pluginName"
    $requiredMarketplaceRelativePaths = @(".codex-plugin\plugin.json")
    foreach ($relativePath in $pluginRuntimeKeyPaths[$pluginName]) {
        $requiredMarketplaceRelativePaths += $relativePath
    }

    Ensure-PluginCacheComplete -Source $sourcePluginRoot -Destination $temporaryPluginRoot -RequiredRelativePaths $requiredMarketplaceRelativePaths
}

Repair-KnownCuratedManifestWarnings -CodexHomePath $codexHomePath
Repair-CuratedAgentIconWarnings -CodexHomePath $codexHomePath

try {
Ensure-RuntimeConfig -CodexHomePath $codexHomePath `
    -BundledMarketplacePath $registrationMarketplace `
    -AppxResourcesPath $appxResourcesPath `
    -PluginNames $RequiredPlugins `
    -DisabledPluginNames $windowsIncompatiblePluginSelectors
Ensure-ManagedBundledPluginConfig -CodexHomePath $codexHomePath `
    -DisabledPluginNames $windowsIncompatiblePluginSelectors
Ensure-WindowsIncompatiblePluginsRemoved -CodexHomePath $codexHomePath -PluginSelectors $windowsIncompatiblePluginSelectors
$registryBeforeInstall = Get-BundledPluginRegistry
foreach ($pluginName in $RequiredPlugins) {
    $pluginSelector = "$pluginName@openai-bundled"
    $sourcePluginRoot = Join-Path $registrationMarketplace "plugins\$pluginName"
    $pluginVersion = Read-PluginVersion -PluginRoot $sourcePluginRoot
    $registrationIsCurrent = $true
    try {
        Assert-BundledPluginRegistryEntry `
            -Registry $registryBeforeInstall `
            -PluginName $pluginName `
            -ExpectedVersion $pluginVersion `
            -ExpectedPluginSource $sourcePluginRoot `
            -ExpectedMarketplaceSource $registrationMarketplace
    }
    catch {
        $registrationIsCurrent = $false
        Write-Output "Official plugin registration requires refresh: $pluginSelector ($($_.Exception.Message))"
    }
    if ($registrationIsCurrent) {
        Write-Output "Official plugin registration is already current: $pluginSelector $pluginVersion"
        continue
    }
    Write-Output "Installing bundled plugin through the official Codex plugin registry: $pluginSelector"
    Invoke-CodexCommand -Arguments @("plugin", "add", $pluginSelector, "--json")
}
foreach ($pluginName in $RequiredPlugins) {
    $pluginSelector = "$pluginName@openai-bundled"
    $sourcePluginRoot = Join-Path $registrationMarketplace "plugins\$pluginName"
    $pluginVersion = Read-PluginVersion -PluginRoot $sourcePluginRoot
    $cachePluginRoot = Join-Path $pluginCacheRoot "$pluginName\$pluginVersion"
    Write-Output "Ensuring plugin cache: $pluginSelector $pluginVersion"

    $requiredCacheRelativePaths = @(".codex-plugin\plugin.json")
    foreach ($relativePath in $pluginRuntimeKeyPaths[$pluginName]) {
        $requiredCacheRelativePaths += $relativePath
    }

    Ensure-PluginCacheComplete -Source $sourcePluginRoot -Destination $cachePluginRoot -RequiredRelativePaths $requiredCacheRelativePaths
    Ensure-Junction -Path (Join-Path $pluginCacheRoot "$pluginName\latest") -Target $cachePluginRoot
    Ensure-PluginCacheComplete -Source $sourcePluginRoot -Destination (Join-Path $pluginCacheRoot "$pluginName\latest") -RequiredRelativePaths $requiredCacheRelativePaths -AllowDestinationRootJunction
}

Write-Output "Official plugin registration completed for: $($RequiredPlugins -join ', ')"

$missing = New-Object System.Collections.Generic.List[string]
foreach ($pluginName in $RequiredPlugins) {
    $sourcePluginRoot = Join-Path $registrationMarketplace "plugins\$pluginName"
    $pluginVersion = Read-PluginVersion -PluginRoot $sourcePluginRoot
    $keyPaths = @(
        (Join-Path $sourcePluginRoot ".codex-plugin\plugin.json"),
        (Join-Path $persistentMarketplace "plugins\$pluginName\.codex-plugin\plugin.json"),
        (Join-Path $pluginCacheRoot "$pluginName\latest\.codex-plugin\plugin.json")
    )

    foreach ($relativePath in $pluginRuntimeKeyPaths[$pluginName]) {
        $keyPaths += (Join-Path $pluginCacheRoot "$pluginName\latest\$relativePath")
    }

    foreach ($keyPath in $keyPaths) {
        if (-not (Test-Path -LiteralPath $keyPath)) {
            $missing.Add($keyPath)
        }
    }
}

if ($missing.Count -gt 0) {
    throw ("Repair verification failed:`n" + ($missing -join "`n"))
}

$registry = Get-BundledPluginRegistry
foreach ($pluginName in $RequiredPlugins) {
    $sourcePluginRoot = Join-Path $registrationMarketplace "plugins\$pluginName"
    $pluginVersion = Read-PluginVersion -PluginRoot $sourcePluginRoot
    Assert-BundledPluginRegistryEntry `
        -Registry $registry `
        -PluginName $pluginName `
        -ExpectedVersion $pluginVersion `
        -ExpectedPluginSource $sourcePluginRoot `
        -ExpectedMarketplaceSource $registrationMarketplace
}
Ensure-WindowsIncompatiblePluginsRemoved -CodexHomePath $codexHomePath -PluginSelectors $windowsIncompatiblePluginSelectors

}
finally {
    Write-Output "Releasing Desktop-owned marketplace registrations from persistent configuration."
    Ensure-RuntimeConfig -CodexHomePath $codexHomePath `
        -AppxResourcesPath $appxResourcesPath `
        -PluginNames $RequiredPlugins `
        -DisabledPluginNames $windowsIncompatiblePluginSelectors `
        -ReleaseDesktopMarketplaceOwnership
    Ensure-ManagedBundledPluginConfig -CodexHomePath $codexHomePath `
        -DisabledPluginNames $windowsIncompatiblePluginSelectors
}

Write-Output "Bundled plugin repair verification passed."
