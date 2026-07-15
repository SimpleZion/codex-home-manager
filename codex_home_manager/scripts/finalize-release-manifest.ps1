param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("PrepareMetadata", "VerifyPublication")]
    [string]$Phase,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ArtifactDeploymentId,

    [string]$MetadataDeploymentId,

    [string]$CloudflareProject = "codex-home-manager",

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string]$GithubRepository,

    [Parameter(Mandatory = $true)]
    [ValidateRange(1, [long]::MaxValue)]
    [long]$GithubReleaseId,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$GithubTag
)

$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $PSScriptRoot
$rootRepository = Split-Path -Parent $appDirectory
$publicRepository = Join-Path $rootRepository "codex-home-manager-public"
$publicSiteDirectory = Join-Path $publicRepository "site"
$buildRoot = Join-Path $appDirectory "build\local-connector"
$releaseRoot = Join-Path $appDirectory "build\releases"
$proofRoot = Join-Path $buildRoot "release-proof"
$buildSourceSnapshotPath = Join-Path $buildRoot "release-build-source.json"
$artifactPublicSourceSnapshotPath = Join-Path $proofRoot "artifact-public-source.json"
$artifactDeploymentEvidencePath = Join-Path $proofRoot "artifact-deployment.json"
$githubReleaseEvidencePath = Join-Path $proofRoot "github-release.json"
$sourceEvidenceProofPath = Join-Path $buildRoot "source-release-evidence.json"
$manifestPath = Join-Path $releaseRoot "release-manifest.json"
$signaturePath = Join-Path $releaseRoot "release-manifest.json.sig"
$publicKeyPath = Join-Path $releaseRoot "release-signing-public-key.pem"
$publicFingerprintPath = Join-Path $publicSiteDirectory "release-signing-public-key.sha256"
$trustedPublicKeyFingerprintPath = "D:\Backup\codex_home_manager\release-signing\release-signing-public-key.sha256"
$releaseManifestScript = Join-Path $PSScriptRoot "release_manifest.py"

function Get-GitOutput {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $output = & git -C $Repository @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed in $Repository"
    }
    return ($output | Out-String).Trim()
}

function Assert-CleanMainRepository {
    param([Parameter(Mandatory = $true)][string]$Repository)
    if ((Get-GitOutput -Repository $Repository -Arguments @("branch", "--show-current")) -cne "main") {
        throw "Public release proof requires public repository branch main"
    }
    if (Get-GitOutput -Repository $Repository -Arguments @("status", "--porcelain=v1", "--untracked-files=all")) {
        throw "Public release proof requires a clean public repository"
    }
}

function Get-VerifiedPagesDeployment {
    param(
        [Parameter(Mandatory = $true)][string]$DeploymentId,
        [Parameter(Mandatory = $true)][string]$ExpectedPublicCommit,
        [Parameter(Mandatory = $true)][string]$ProofLabel
    )
    if (-not $env:CLOUDFLARE_API_TOKEN -or -not $env:CLOUDFLARE_ACCOUNT_ID) {
        throw "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are required to verify the Cloudflare Pages deployment"
    }
    $requestUrl = "https://api.cloudflare.com/client/v4/accounts/$($env:CLOUDFLARE_ACCOUNT_ID)/pages/projects/$CloudflareProject/deployments/$DeploymentId"
    try {
        $response = Invoke-RestMethod -Method Get -Uri $requestUrl -Headers @{ Authorization = "Bearer $($env:CLOUDFLARE_API_TOKEN)" }
    }
    catch {
        throw "Cloudflare API lookup failed for $ProofLabel deployment ${DeploymentId}: $($_.Exception.Message)"
    }
    if (-not $response.success -or -not $response.result) {
        throw "Cloudflare API did not return a deployment for $ProofLabel deployment $DeploymentId"
    }
    $deployment = $response.result
    $metadata = $deployment.deployment_trigger.metadata
    if ($deployment.id -cne $DeploymentId) {
        throw "Cloudflare API deployment ID mismatch for $ProofLabel"
    }
    if ($metadata.branch -cne "main") {
        throw "Cloudflare API deployment branch is not main for $ProofLabel"
    }
    if ($metadata.commit_hash -cne $ExpectedPublicCommit -or $metadata.commit_dirty -ne $false) {
        throw "Cloudflare API deployment commit proof mismatch for $ProofLabel"
    }
    if ($deployment.latest_stage.name -cne "deploy" -or $deployment.latest_stage.status -cne "success") {
        throw "Cloudflare API deployment did not complete successfully for $ProofLabel"
    }
    try {
        $deploymentUri = [System.Uri]$deployment.url
    }
    catch {
        throw "Cloudflare API deployment URL is invalid for $ProofLabel"
    }
    if (-not $deploymentUri.IsAbsoluteUri -or $deploymentUri.Scheme -ne "https" -or -not $deploymentUri.Host) {
        throw "Cloudflare API deployment URL is invalid for $ProofLabel"
    }
    return [ordered]@{
        id = [string]$deployment.id
        project = $CloudflareProject
        branch = "main"
        public_commit = $ExpectedPublicCommit
        url = $deploymentUri.GetLeftPart([System.UriPartial]::Authority)
        status = "success"
    }
}

function Get-VerifiedGithubRelease {
    param(
        [Parameter(Mandatory = $true)][bool]$RequireDraft,
        [Parameter(Mandatory = $true)][bool]$RequireSignedMetadata,
        [bool]$AllowSignedMetadataRetirement = $false
    )
    $githubToken = $env:GITHUB_TOKEN
    if (-not $githubToken) {
        $githubToken = (& gh auth token 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $githubToken) {
            throw "GitHub authentication is required through GITHUB_TOKEN or an existing gh auth login"
        }
    }
    $requestUrl = "https://api.github.com/repos/$GithubRepository/releases/$GithubReleaseId"
    try {
        $release = Invoke-RestMethod -Method Get -Uri $requestUrl -Headers @{
            Authorization = "Bearer $githubToken"
            Accept = "application/vnd.github+json"
            "X-GitHub-Api-Version" = "2022-11-28"
        }
    }
    catch {
        throw "GitHub API lookup failed for release ${GithubReleaseId}: $($_.Exception.Message)"
    }
    $expectedHtmlUrl = "https://github.com/$GithubRepository/releases/tag/$GithubTag"
    $actualHtmlUrl = [string]$release.html_url
    if ([long]$release.id -ne $GithubReleaseId -or [string]$release.tag_name -cne $GithubTag) {
        throw "GitHub release ID, repository, or tag mismatch"
    }
    if ($RequireDraft) {
        $expectedDraftUrlPrefix = "https://github.com/$GithubRepository/releases/tag/untagged-"
        if (-not $actualHtmlUrl.StartsWith($expectedDraftUrlPrefix, [System.StringComparison]::Ordinal) -or
            $actualHtmlUrl.Substring($expectedDraftUrlPrefix.Length) -notmatch '^[0-9a-f]{20}$') {
            throw "GitHub draft release URL mismatch"
        }
    }
    elseif ($actualHtmlUrl -cne $expectedHtmlUrl) {
        throw "Published GitHub release URL mismatch"
    }
    if ([bool]$release.draft -ne $RequireDraft -or [bool]$release.prerelease) {
        throw "GitHub release draft or prerelease state mismatch"
    }

    $bundle = Get-Content -LiteralPath (Join-Path $publicSiteDirectory "connector-release.json") -Raw | ConvertFrom-Json
    $connectorArtifactMetadata = @($bundle.artifacts)
    if ($connectorArtifactMetadata.Count -ne 2 -or @($connectorArtifactMetadata.kind | Sort-Object -Unique) -join "," -cne "exe,zip") {
        throw "connector-release.json must contain exactly the EXE and ZIP before GitHub verification"
    }
    if (-not (Test-Path -LiteralPath $sourceEvidenceProofPath -PathType Leaf)) {
        throw "Verified source evidence proof is missing: $sourceEvidenceProofPath"
    }
    $sourceEvidence = Get-Content -LiteralPath $sourceEvidenceProofPath -Raw | ConvertFrom-Json
    $sourceEvidenceAssets = @($sourceEvidence.assets)
    $expectedSourceEvidenceNames = @(
        "codex-home-manager-source.zip",
        "codex-home-manager-source.cdx.json",
        "source-ci-test-summary.md",
        "source-provenance-attestation.sigstore.json",
        "source-sbom-attestation.sigstore.json"
    )
    if ($sourceEvidence.schema_version -ne 1 -or
        (@($sourceEvidenceAssets.name | Sort-Object) -join "`n") -cne (@($expectedSourceEvidenceNames | Sort-Object) -join "`n")) {
        throw "Verified source evidence proof has an invalid asset set"
    }
    $artifactMetadata = @($connectorArtifactMetadata) + @($sourceEvidenceAssets)
    $artifactNames = @($artifactMetadata.name)
    $signedMetadataNames = @(
        "release-manifest.json",
        "release-manifest.json.sig",
        "release-signing-public-key.pem",
        "release-signing-public-key.sha256"
    )
    $expectedNames = @($artifactNames)
    if ($RequireSignedMetadata) {
        $expectedNames += $signedMetadataNames
    }
    $actualNames = @($release.assets | ForEach-Object { [string]$_.name })
    $expectedNameSet = (@($expectedNames | Sort-Object) -join "`n")
    $actualNameSet = (@($actualNames | Sort-Object) -join "`n")
    $retirableNameSet = (@($artifactNames + $signedMetadataNames | Sort-Object) -join "`n")
    $signedMetadataRetirementRequired = (
        -not $RequireSignedMetadata -and
        $AllowSignedMetadataRetirement -and
        $actualNameSet -ceq $retirableNameSet
    )
    if ($expectedNameSet -cne $actualNameSet -and -not $signedMetadataRetirementRequired) {
        throw "GitHub release asset set mismatch; extra, missing, or fake assets are not allowed"
    }

    $evidenceAssets = @()
    foreach ($artifact in $artifactMetadata) {
        $remoteAsset = @($release.assets | Where-Object { [string]$_.name -ceq [string]$artifact.name })
        if ($remoteAsset.Count -ne 1 -or [long]$remoteAsset[0].size -ne [long]$artifact.size) {
            throw "GitHub release artifact size mismatch: $($artifact.name)"
        }
        New-Item -ItemType Directory -Force -Path $proofRoot | Out-Null
        $downloadPath = Join-Path $proofRoot ("github-" + [string]$artifact.name)
        Invoke-WebRequest -Uri ([string]$remoteAsset[0].url) -OutFile $downloadPath -Headers @{
            Authorization = "Bearer $githubToken"
            Accept = "application/octet-stream"
        }
        $downloadHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $downloadPath).Hash.ToLowerInvariant()
        if ($downloadHash -cne [string]$artifact.sha256) {
            throw "GitHub release artifact hash mismatch: $($artifact.name)"
        }
        $canonicalDownloadUrl = "https://github.com/$GithubRepository/releases/download/$GithubTag/$($artifact.name)"
        $evidenceAssets += [ordered]@{
            name = [string]$remoteAsset[0].name
            size = [long]$remoteAsset[0].size
            sha256 = $downloadHash
            browser_download_url = $canonicalDownloadUrl
            api_browser_download_url = [string]$remoteAsset[0].browser_download_url
        }
    }
    return [ordered]@{
        schema_version = 1
        repository = $GithubRepository
        release = [ordered]@{
            id = [long]$release.id
            tag_name = [string]$release.tag_name
            html_url = $expectedHtmlUrl
            api_html_url = $actualHtmlUrl
            draft = [bool]$release.draft
            prerelease = [bool]$release.prerelease
            assets = $evidenceAssets
            signed_metadata_retirement_required = $signedMetadataRetirementRequired
        }
    }
}

function Remove-VerifiedStaleGithubSignedMetadata {
    $metadataNames = @(
        "release-manifest.json",
        "release-manifest.json.sig",
        "release-signing-public-key.pem",
        "release-signing-public-key.sha256"
    )
    foreach ($metadataName in $metadataNames) {
        & gh release delete-asset $GithubTag $metadataName --repo $GithubRepository --yes
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to retire stale GitHub release metadata asset: $metadataName"
        }
    }
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    [System.IO.File]::WriteAllText($Path, (($Value | ConvertTo-Json -Depth 8) + "`n"), [System.Text.UTF8Encoding]::new($false))
}

if (-not (Test-Path -LiteralPath $releaseManifestScript -PathType Leaf)) {
    throw "Release manifest script was not found: $releaseManifestScript"
}
if (-not (Test-Path -LiteralPath $trustedPublicKeyFingerprintPath -PathType Leaf)) {
    throw "Private root trust fingerprint was not found: $trustedPublicKeyFingerprintPath"
}

if ($Phase -eq "PrepareMetadata") {
    Assert-CleanMainRepository -Repository $publicRepository
    $artifactPublicCommit = Get-GitOutput -Repository $publicRepository -Arguments @("rev-parse", "HEAD")
    $artifactDeployment = Get-VerifiedPagesDeployment `
        -DeploymentId $ArtifactDeploymentId `
        -ExpectedPublicCommit $artifactPublicCommit `
        -ProofLabel "artifact"
    Write-JsonFile -Path $artifactDeploymentEvidencePath -Value ([ordered]@{
        schema_version = 1
        deployment = $artifactDeployment
    })
    $githubDraftEvidence = Get-VerifiedGithubRelease `
        -RequireDraft $true `
        -RequireSignedMetadata $false `
        -AllowSignedMetadataRetirement $true
    if ([bool]$githubDraftEvidence.release.signed_metadata_retirement_required) {
        Remove-VerifiedStaleGithubSignedMetadata
        $githubDraftEvidence = Get-VerifiedGithubRelease -RequireDraft $true -RequireSignedMetadata $false
    }
    Write-JsonFile -Path $githubReleaseEvidencePath -Value $githubDraftEvidence

    & python $releaseManifestScript capture-artifact-public-source `
        --output $artifactPublicSourceSnapshotPath `
        --public-repo $publicRepository `
        --artifact-deployment-id $ArtifactDeploymentId
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to capture the clean artifact-phase public commit"
    }

    & python $releaseManifestScript create `
        --build-source-snapshot $buildSourceSnapshotPath `
        --artifact-public-source-snapshot $artifactPublicSourceSnapshotPath `
        --dist (Join-Path $appDirectory "dist") `
        --release-dir $releaseRoot `
        --public-site $publicSiteDirectory `
        --artifact-deployment-evidence $artifactDeploymentEvidencePath `
        --github-release-evidence $githubReleaseEvidencePath `
        --source-evidence-proof $sourceEvidenceProofPath `
        --private-key "D:\Backup\codex_home_manager\release-signing\release-signing-key.pem" `
        --trusted-public-key-fingerprint $trustedPublicKeyFingerprintPath `
        --manifest $manifestPath `
        --signature $signaturePath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the signed release manifest"
    }

    & python $releaseManifestScript verify `
        --manifest $manifestPath `
        --signature $signaturePath `
        --public-key $publicKeyPath `
        --trusted-public-key-fingerprint $trustedPublicKeyFingerprintPath `
        --root-repo $rootRepository `
        --manager-repo $appDirectory `
        --public-repo $publicRepository `
        --dist (Join-Path $appDirectory "dist") `
        --release-dir $releaseRoot `
        --public-site $publicSiteDirectory `
        --artifact-deployment-evidence $artifactDeploymentEvidencePath `
        --github-release-evidence $githubReleaseEvidencePath `
        --source-evidence-proof $sourceEvidenceProofPath
    if ($LASTEXITCODE -ne 0) {
        throw "Signed release manifest local verification failed"
    }

    if (-not (Test-Path -LiteralPath $signaturePath -PathType Leaf) -or (Get-Item -LiteralPath $signaturePath).Length -lt 1) {
        throw "Detached release-manifest.json.sig is mandatory; refusing metadata publication"
    }
    $publicManifestPath = Join-Path $publicSiteDirectory "release-manifest.json"
    $publicSignaturePath = Join-Path $publicSiteDirectory "release-manifest.json.sig"
    Copy-Item -LiteralPath $manifestPath -Destination $publicManifestPath -Force
    Copy-Item -LiteralPath $signaturePath -Destination $publicSignaturePath -Force
    Push-Location $publicRepository
    try {
        $originalPinnedFingerprint = $env:CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256
        $originalReleaseMode = $env:CODEX_HOME_MANAGER_PUBLIC_RELEASE_MODE
        $env:CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256 = (Get-Content -LiteralPath $trustedPublicKeyFingerprintPath -Raw).Trim()
        $env:CODEX_HOME_MANAGER_PUBLIC_RELEASE_MODE = "1"
        npm run check
        if ($LASTEXITCODE -ne 0) {
            throw "Public signed-release boundary validation failed"
        }
    }
    finally {
        $env:CODEX_HOME_MANAGER_RELEASE_PUBLIC_KEY_SHA256 = $originalPinnedFingerprint
        $env:CODEX_HOME_MANAGER_PUBLIC_RELEASE_MODE = $originalReleaseMode
        Pop-Location
    }
    & gh release upload $GithubTag `
        $manifestPath `
        $signaturePath `
        $publicKeyPath `
        $publicFingerprintPath `
        --clobber `
        --repo $GithubRepository
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upload the signed release metadata to the GitHub draft release"
    }
    Get-VerifiedGithubRelease -RequireDraft $true -RequireSignedMetadata $true | Out-Null
    Write-Output "Metadata files are ready. Commit only the metadata files, deploy that commit to main, then run VerifyPublication with its distinct deployment ID."
    Write-Output $manifestPath
    Write-Output $signaturePath
    exit 0
}

if (-not $MetadataDeploymentId) {
    throw "MetadataDeploymentId is required for VerifyPublication"
}
if ($MetadataDeploymentId -eq $ArtifactDeploymentId) {
    throw "The metadata deployment must be distinct from the artifact deployment"
}
if (-not (Test-Path -LiteralPath (Join-Path $publicSiteDirectory "release-manifest.json.sig") -PathType Leaf) -or
    (Get-Item -LiteralPath (Join-Path $publicSiteDirectory "release-manifest.json.sig")).Length -lt 1) {
    throw "Detached release-manifest.json.sig is mandatory; refusing final publication verification"
}
Assert-CleanMainRepository -Repository $publicRepository
$metadataPublicCommit = Get-GitOutput -Repository $publicRepository -Arguments @("rev-parse", "HEAD")
$metadataDeployment = Get-VerifiedPagesDeployment `
    -DeploymentId $MetadataDeploymentId `
    -ExpectedPublicCommit $metadataPublicCommit `
    -ProofLabel "metadata"
$artifactSnapshot = Get-Content -LiteralPath $artifactPublicSourceSnapshotPath -Raw | ConvertFrom-Json
$artifactPublicCommit = [string]$artifactSnapshot.sources.artifact_public.head
$artifactDeployment = Get-VerifiedPagesDeployment `
    -DeploymentId $ArtifactDeploymentId `
    -ExpectedPublicCommit $artifactPublicCommit `
    -ProofLabel "artifact"
Get-VerifiedGithubRelease -RequireDraft $false -RequireSignedMetadata $true | Out-Null

& python $releaseManifestScript online-verify `
    --metadata-base-url $metadataDeployment.url `
    --artifact-deployment-url $artifactDeployment.url `
    --artifact-deployment-id $artifactDeployment.id `
    --artifact-public-commit $artifactDeployment.public_commit `
    --github-repository $GithubRepository `
    --github-tag $GithubTag `
    --github-release-id $GithubReleaseId `
    --trusted-public-key-fingerprint $trustedPublicKeyFingerprintPath
if ($LASTEXITCODE -ne 0) {
    throw "Online release proof verification failed"
}
Write-Output "Online release proof verified from the Cloudflare metadata and artifact deployment URLs."
Write-Output $metadataDeployment.url
Write-Output $artifactDeployment.url
