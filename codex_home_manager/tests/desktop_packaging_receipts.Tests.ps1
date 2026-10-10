$ErrorActionPreference = "Stop"
$managerRoot = Split-Path -Parent $PSScriptRoot
$packageText = [IO.File]::ReadAllText((Join-Path $managerRoot "scripts/package-desktop.ps1"))
$prepareText = [IO.File]::ReadAllText((Join-Path $managerRoot "scripts/prepare-desktop-sidecar.ps1"))
$script:checks = 0

function Assert-True {
    param([bool]$Value, [string]$Message)
    if (-not $Value) { throw $Message }
    $script:checks++
}

function Assert-Throws {
    param([scriptblock]$Action, [string]$Pattern)
    try { & $Action }
    catch {
        Assert-True ($_.Exception.Message -match $Pattern) "Unexpected rejection: $($_.Exception.Message)"
        return
    }
    throw "Expected rejection: $Pattern"
}

function Parse-Script {
    param([string]$Text)
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($Text, [ref]$tokens, [ref]$errors)
    Assert-True ($errors.Count -eq 0) "PowerShell parser rejected the packaging script"
    return $ast
}

$packageAst = Parse-Script $packageText
$prepareAst = Parse-Script $prepareText
$guards = @($packageAst.EndBlock.Statements | Where-Object { $_.Extent.Text.StartsWith('if ($SkipBuild)') })
Assert-True ($guards.Count -eq 2) "Expected receipt trust and binding guards"
$trustGuard = [scriptblock]::Create($guards[0].Extent.Text)
$bindingGuard = [scriptblock]::Create($guards[1].Extent.Text)
$receiptNode = @($packageAst.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and $_.Left.Extent.Text -eq '$receipt' })
Assert-True ($receiptNode.Count -eq 1) "Expected one release receipt writer"
$makeReceipt = [scriptblock]::Create($receiptNode[0].Extent.Text)

# Only the guards and receipt expression are evaluated. File reads below are memory mocks;
# the full packaging/build script, installer copies and WriteAllText calls never run.
function Test-Path { param($LiteralPath, $PathType) return $script:receiptExists }
function Get-Content { param($LiteralPath, [switch]$Raw) return $script:receiptJson }

function Invoke-ReceiptScenario {
    param([hashtable]$Prior, [string]$Source = "source", [string]$Resources = "resources", [bool]$Reuse = $true)
    $script:receiptExists = $null -ne $Prior
    $script:receiptJson = if ($Prior) { $Prior | ConvertTo-Json } else { "{}" }
    $SkipBuild = $Reuse
    $receiptPath = "memory-receipt.json"
    $configuration = [pscustomobject]@{ productName = "Codex Cockpit"; version = "fixture" }
    $buildStartedAt = [DateTime]::Parse("2026-09-07T12:00:00Z").ToUniversalTime()
    $builtAt = $buildStartedAt.ToString("o")
    $expectedName = "Codex Cockpit_fixture_x64-setup.exe"
    $sourceFingerprint = $Source
    $resourcesFingerprint = $Resources
    $installerHash = "installer"
    $catalogHash = "catalog"
    $frontendHash = "frontend"
    $legacyHash = "legacy"
    $hash = $installerHash
    . $trustGuard
    . $bindingGuard
    . $makeReceipt
    return $receipt
}

$prior = @{
    schemaVersion = 2; productName = "Codex Cockpit"; version = "fixture"
    installerSha256 = "installer"; catalogSha256 = "catalog"; frontendManifestSha256 = "frontend"
    legacySha256 = "legacy"; sourceFingerprint = "source"; resourcesFingerprint = "resources"
    builtAt = "2026-01-02T03:04:05.0000000+00:00"
}
$reused = Invoke-ReceiptScenario -Prior $prior
Assert-True ($reused.builtAt -ceq $prior.builtAt) "SkipBuild refreshed the original builtAt"
Assert-True ($reused.stagedAt -ne $prior.builtAt) "Staging time was not separated from build time"
Assert-True ($reused.schemaVersion -eq 2 -and $reused.resourcesFingerprint -eq "resources") "Receipt lost the complete resource binding"
$fresh = Invoke-ReceiptScenario -Prior $null -Reuse $false
Assert-True ($fresh.builtAt -like '2026-09-07T12:00:00*') "Fresh build did not retain its real build start time"
Assert-Throws { Invoke-ReceiptScenario -Prior $null } "prior verified"
foreach ($schema in @(1, $null)) {
    $old = $prior.Clone(); $old.schemaVersion = $schema
    Assert-Throws { Invoke-ReceiptScenario -Prior $old } "source/resource-bound"
}
$old = $prior.Clone(); $old.builtAt = "invalid"
Assert-Throws { Invoke-ReceiptScenario -Prior $old } "source/resource-bound"
Assert-Throws { Invoke-ReceiptScenario -Prior $prior -Source "vite-or-Cargo-or-backend-changed" } "no longer match"
Assert-Throws { Invoke-ReceiptScenario -Prior $prior -Resources "native-or-embedded-resource-changed" } "no longer match"
foreach ($field in @("installerSha256", "catalogSha256", "frontendManifestSha256", "legacySha256", "resourcesFingerprint")) {
    $old = $prior.Clone(); $old[$field] = "different"
    Assert-Throws { Invoke-ReceiptScenario -Prior $old } "no longer match"
}

# ReuseExisting must throw from the actual entry point before any source probe or resource mutation.
Assert-Throws { & ([scriptblock]::Create($prepareText)) -ReuseExisting } "source/toolchain-bound sidecar receipt"
Assert-True ($prepareText.IndexOf('if ($ReuseExisting)') -lt $prepareText.IndexOf('$appDirectory =')) "Reuse gate runs after setup work"
$modeNode = @($prepareAst.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and $_.Left.Extent.Text -eq '$frontendMode' })
Assert-True ($modeNode.Count -eq 1) "Expected one frontend mode decision"
$chooseMode = [scriptblock]::Create($modeNode[0].Extent.Text)
$SkipFrontendBuild = $true
. $chooseMode
Assert-True ($frontendMode -eq "verify") "SkipFrontendBuild can still stamp old dist as a fresh build"
$SkipFrontendBuild = $false
. $chooseMode
Assert-True ($frontendMode -eq "build") "Default frontend path does not use the build/receipt contract"
Assert-True (-not $prepareText.Contains('$frontendArtifactScript stage')) "Prepare script still stages unbound dist"
Assert-True ($packageText.Contains('Invoke-ArtifactContract -Mode source-digest -Scope desktop')) "Package script has a separate source whitelist"
Assert-True (-not $packageText.Contains('Get-ChildItem')) "Incomplete PowerShell source enumeration returned"
Assert-True ($packageText.IndexOf('$resourcesFingerprint = Invoke-ArtifactContract') -lt $packageText.IndexOf('tauri.cmd')) "Bundler consumes resources before their identity is frozen"
Assert-True ($packageText.Contains('Invoke-ArtifactContract -Mode verify')) "SkipBuild bypasses frontend receipt verification"
Assert-True ($packageText.Contains('Invoke-ArtifactContract -Mode desktop-artifacts) -ne $resourcesFingerprint')) "Bundler resource changes are not rejected"
Write-Output "PASS: $script:checks PowerShell parser, in-memory receipt and entry-point checks; no build/file writes/process commands"
