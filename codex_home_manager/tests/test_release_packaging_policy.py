from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image
import pytest

from scripts import release_manifest


manager_root = Path(__file__).resolve().parents[1]
requirements_path = manager_root / "packaging" / "windows" / "requirements-connector.txt"
package_script_path = manager_root / "scripts" / "package-local-connector.ps1"
version_info_path = manager_root / "packaging" / "windows" / "version_info.txt"
finalize_script_path = manager_root / "scripts" / "finalize-release-manifest.ps1"
desktop_prepare_script_path = manager_root / "scripts" / "prepare-desktop-sidecar.ps1"
tauri_config_path = manager_root / "src-tauri" / "tauri.conf.json"
frontend_artifact_script_path = manager_root / "scripts" / "frontend-release-artifact.mjs"
windows_icon_path = manager_root / "packaging" / "windows" / "assets" / "codex-home-manager.ico"
tauri_icon_path = manager_root / "src-tauri" / "icons" / "icon.ico"


def requirement_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not raw_line[:1].isspace() and current:
            blocks.append(" ".join(current))
            current = []
        current.append(line.removesuffix("\\").strip())
    if current:
        blocks.append(" ".join(current))
    return blocks


def test_connector_requirements_are_a_complete_hashed_lock() -> None:
    blocks = requirement_blocks(requirements_path.read_text(encoding="utf-8"))

    assert len(blocks) >= 10, "the lock must include transitive dependencies"
    for block in blocks:
        requirement = block.split(" --hash=", maxsplit=1)[0]
        assert re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+(?:\s*;.+)?", requirement)
        assert re.search(r"--hash=sha256:[0-9a-f]{64}(?:\s|$)", block)


def test_packaging_installs_only_hashed_binary_requirements() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "--require-hashes" in script
    assert "--only-binary=:all:" in script
    assert "pip install --upgrade pip" not in script


def test_provider_interoperability_skill_is_bundled_in_every_windows_runtime() -> None:
    connector_script = package_script_path.read_text(encoding="utf-8")
    desktop_script = desktop_prepare_script_path.read_text(encoding="utf-8")
    tauri_config = json.loads(tauri_config_path.read_text(encoding="utf-8"))
    skill_root = manager_root / "packaging" / "skills" / "codex-provider-interoperability"

    assert (skill_root / "SKILL.md").is_file()
    assert (skill_root / "agents" / "openai.yaml").is_file()
    assert (skill_root / "references" / "compatibility-contract.md").is_file()
    assert connector_script.count('--add-data "$skillsSourceRoot;packaging/skills"') == 2
    assert desktop_script.count('"--add-data", "$skillsSourceRoot;packaging/skills"') == 1
    assert "providerInteropSkillMarker" in desktop_script
    assert tauri_config["bundle"]["resources"]["resources/skills/"] == "skills/"


def test_desktop_and_legacy_runtime_share_one_verified_frontend_artifact() -> None:
    desktop_script = desktop_prepare_script_path.read_text(encoding="utf-8")
    artifact_script = frontend_artifact_script_path.read_text(encoding="utf-8")
    tauri_config = json.loads(tauri_config_path.read_text(encoding="utf-8"))

    assert "frontend-release-artifact.mjs" in desktop_script
    assert "build\\frontend-release" in desktop_script
    assert tauri_config["build"]["frontendDist"] == "../build/frontend-release"
    assert tauri_config["build"]["beforeBuildCommand"] == "node scripts/frontend-release-artifact.mjs verify"
    assert tauri_config["bundle"]["resources"]["resources/web/"] == "web/"
    assert "$frontendResourceRoot" in desktop_script
    assert 'Copy-Item -Path (Join-Path $frontendDist "*")' in desktop_script
    assert '$buildRoot = [System.IO.Path]::GetFullPath($env:CODEX_HOME_MANAGER_BUILD_ROOT)' in desktop_script
    assert 'Join-Path $buildParent $buildIdentifier' not in desktop_script
    assert desktop_script.index('New-Item -ItemType Directory -Force -Path $buildRoot') < desktop_script.index(
        'Move-Item -LiteralPath $frontendResourceRoot'
    )
    assert 'mode === "stage"' in artifact_script
    assert 'mode === "verify"' in artifact_script
    assert "sourceDigest" in artifact_script
    assert "artifactDigest" in artifact_script


def test_prepared_resources_resume_rejects_changed_source_resource_and_run() -> None:
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is required to execute the packaging receipt check")
    command = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Packaging script parse failed' }
$definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-PreparedResourceReceipt' }, $false)
if (-not $definition) { throw 'Receipt validator is missing' }
. ([scriptblock]::Create($definition.Extent.Text))
$valid = @{schemaVersion=1; stage='resources-prepared'; productName='Codex Cockpit'; version='1.0.34'; runRoot='D:\fixture'; sourceFingerprint=('a'*64); resourcesFingerprint=('b'*64); preparedAt='2026-10-10T00:00:00Z'}
Assert-PreparedResourceReceipt -Receipt ([pscustomobject]$valid) -Version '1.0.34' -RunRoot 'D:\fixture' -SourceFingerprint ('a'*64) -ResourcesFingerprint ('b'*64)
foreach ($key in $valid.Keys) {
    $changed = $valid.Clone(); $changed[$key] = 'invalid'
    $rejected = $false
    try { Assert-PreparedResourceReceipt -Receipt ([pscustomobject]$changed) -Version '1.0.34' -RunRoot 'D:\fixture' -SourceFingerprint ('a'*64) -ResourcesFingerprint ('b'*64) } catch { $rejected = $true }
    if (-not $rejected) { throw "Receipt accepted changed $key" }
}
Write-Output 'prepared-resource-receipt: passed'
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", "& {" + command + "} '" + str(manager_root / "scripts" / "package-desktop.ps1") + "'"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "prepared-resource-receipt: passed" in result.stdout


def test_windows_desktop_uses_one_antialiased_multiresolution_icon() -> None:
    tauri_config = json.loads(tauri_config_path.read_text(encoding="utf-8"))
    desktop_lib = (manager_root / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    cargo_manifest = (manager_root / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    required_sizes = {
        (16, 16), (20, 20), (24, 24), (30, 30), (32, 32), (36, 36), (40, 40),
        (48, 48), (60, 60), (64, 64), (72, 72), (80, 80), (96, 96), (128, 128), (256, 256),
    }

    assert tauri_config["bundle"]["icon"] == ["icons/icon.ico"]
    assert 'tauri::include_image!("./icons/icon.ico")' in desktop_lib
    assert "window.set_icon(APP_ICON.clone())?" in desktop_lib
    assert 'features = ["image-ico"]' in cargo_manifest
    assert windows_icon_path.read_bytes() == tauri_icon_path.read_bytes()
    with Image.open(windows_icon_path) as icon:
        assert required_sizes.issubset(icon.ico.sizes())
        taskbar_icon = icon.ico.getimage((32, 32)).convert("RGBA")
        foreground_pixels = [
            (x, y)
            for y in range(taskbar_icon.height)
            for x in range(taskbar_icon.width)
            if min(taskbar_icon.getpixel((x, y))[:3]) >= 150
            and taskbar_icon.getpixel((x, y))[0] > taskbar_icon.getpixel((x, y))[1] * 0.93
            and taskbar_icon.getpixel((x, y))[3] >= 220
            and 3 <= x < 29 and 3 <= y < 29
        ]
        foreground_bounds = (
            min(x for x, _ in foreground_pixels), min(y for _, y in foreground_pixels),
            max(x for x, _ in foreground_pixels), max(y for _, y in foreground_pixels),
        )
        assert foreground_bounds[2] - foreground_bounds[0] + 1 >= 19
        assert foreground_bounds[3] - foreground_bounds[1] + 1 >= 18


def test_provider_interoperability_skill_keeps_external_doctors_read_only_and_non_authoritative() -> None:
    skill_root = manager_root / "packaging" / "skills" / "codex-provider-interoperability"
    skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    contract_text = (skill_root / "references" / "compatibility-contract.md").read_text(encoding="utf-8")

    assert "source path, size, modification time, and SHA-256 are unchanged" in skill_text
    assert "not proof of corruption" in skill_text
    assert "Never run an external salvage" in skill_text
    assert "not runtime compatibility authorities" in contract_text
    assert "cannot validate the `item_...` versus `rs_...` failure" in contract_text
    assert "cannot validate post-compaction tool availability" in contract_text
    assert "Never run salvage" in contract_text


def test_runtime_compatibility_catalog_is_bundled_in_every_windows_runtime() -> None:
    connector_script = package_script_path.read_text(encoding="utf-8")
    desktop_script = desktop_prepare_script_path.read_text(encoding="utf-8")
    tauri_config = json.loads(tauri_config_path.read_text(encoding="utf-8"))
    catalog = manager_root / "packaging" / "runtime" / "provider-interop-runtime-0.148.0-alpha.9-p2.json"

    assert catalog.is_file()
    assert connector_script.count('--add-data "$runtimeCatalogSourceRoot;packaging/runtime"') == 2
    assert desktop_script.count('"--add-data", "$runtimeCatalogSourceRoot;packaging/runtime"') == 1
    assert "runtimeCatalogMarker" in desktop_script
    assert tauri_config["bundle"]["resources"]["resources/runtime/"] == "runtime/"


def test_packaging_uses_content_addressed_artifacts_and_stable_latest_redirects() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Get-ContentAddressedReleaseName" in script
    assert "codex-home-manager-local-win-x64-v$releaseVersion-" in script
    assert '"/codex-home-manager-local-win-x64.exe"' in script
    assert '"/downloads/latest/windows-x64.exe"' in script
    assert "Cache-Control: no-store" in script
    assert "immutable" in script


def test_packaging_generates_hsts_and_non_overlapping_wasm_cache_headers() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Strict-Transport-Security: max-age=63072000; includeSubDomains; preload" in script
    assert script.count("/assets/*.wasm") == 1
    assert "/assets/*\n" not in script


def test_packaging_audits_zip_and_pyinstaller_executable_before_public_copy() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Assert-ReleaseZipBoundary" in script
    assert "Assert-PyInstallerExecutableBoundary" in script
    assert "pyi-archive_viewer" in script
    assert "connector-release.json" in script
    assert "sensitiveStrings" in script
    assert "sourceFiles" in script


def test_windows_executables_receive_the_package_product_version_resource() -> None:
    script = package_script_path.read_text(encoding="utf-8")
    version_info = version_info_path.read_text(encoding="utf-8")
    package_version = json.loads((manager_root / "package.json").read_text(encoding="utf-8"))["version"]
    version_tuple = ", ".join([*package_version.split("."), "0"])

    assert script.count("--version-file $versionInfoPath") == 3
    assert "Assert-WindowsVersionMetadata" in script
    assert f"filevers=({version_tuple})" in version_info
    assert f"prodvers=({version_tuple})" in version_info
    for name, value in {
        "CompanyName": "SimpleZion",
        "ProductName": "Codex Home Manager",
        "FileDescription": "Codex Home Manager",
        "FileVersion": package_version,
        "ProductVersion": package_version,
    }.items():
        assert f'StringStruct("{name}", "{value}")' in version_info


def test_packaging_recreates_and_audits_public_node_dependencies_from_lock() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "$releaseNpmPath ci --ignore-scripts" in script
    assert "$releaseNpmPath audit --audit-level=high --omit=optional" in script
    assert "$releaseNpmPath test" in script
    assert "--root $publicValidationRoot" in script
    assert "--artifact-stage" in script
    assert "--root $publicValidationRoot `\n            --release" not in script


def test_packaging_selects_a_complete_node_22_or_newer_toolchain() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Get-ReleaseNodeToolchain" in script
    assert "C:\\Program Files\\nodejs" in script
    assert "npm.cmd" in script
    assert "$env:PATH" in script
    assert "(& node --version)" not in script


def test_packaging_captures_and_rechecks_immutable_build_sources_and_private_key_pin() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "capture-build-source" in script
    assert "validate-build-source" in script
    assert "release-build-source.json" in script
    assert "release-signing-public-key.sha256" in script
    assert "--trusted-public-key-fingerprint" in script
    assert release_manifest.embedded_release_public_key_fingerprint in script
    assert "write-user-verifier" in script
    assert "$expectedSha256" not in script
    assert "frontendChecksumPath" not in script


def test_packaging_requires_attested_source_ci_evidence_before_public_checks() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    for parameter in (
        "SourceEvidenceDirectory",
        "SourceCommit",
        "SourceEvidenceRepository",
        "SourceEvidenceSignerWorkflow",
    ):
        assert f"${parameter}" in script
    assert "prepare-source-evidence" in script
    assert "source-release-evidence.json" in script
    assert "codex-home-manager-source.cdx.json" in script
    assert script.index("prepare-source-evidence") < script.index("$releaseNpmPath test")


def test_release_consumes_same_commit_windows_ci_outputs_without_a_local_rebuild() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "[string]$WindowsEvidenceDirectory" in script
    assert "prepare-windows-evidence" in script
    assert "windows-binary-evidence.json" in script
    assert "BINARY-*-SUBJECTS.txt" in script
    assert "windows-*-attestation.sigstore.json" in script
    ci_branch = script.index('if ($PSCmdlet.ParameterSetName -eq "CiBuild") {', script.index("quality_gate.py"))
    ci_build = script.index('$firstBuild = Invoke-IsolatedConnectorBuild -BuildName "build-1"', ci_branch)
    ci_sign = script.index("Invoke-AuthenticodePolicy -Path $firstBuild.Exe", ci_build)
    release_evidence = script.index("prepare-windows-evidence", ci_sign)
    assert ci_branch < ci_build < ci_sign < release_evidence
    assert "Assert-CiAuthenticodeEvidence" in script
    assert script.index("prepare-windows-evidence") < script.index("$releaseNpmPath test")


def test_packaging_requires_two_reproducible_isolated_builds_and_canonical_zip() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "SOURCE_DATE_EPOCH" in script
    assert "Invoke-IsolatedConnectorBuild" in script
    assert "compare-builds" in script
    assert "deterministic-zip" in script
    assert "normalize-pyinstaller-exe" in script
    assert "run_reproducible_pyinstaller.py" in script
    assert "sorted(modules_toc" in script
    assert "Compress-Archive" not in script


def test_packaging_reuses_the_release_builder_for_ci_binary_output() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert 'ParameterSetName = "CiBuild"' in script
    assert "$CiOutputDirectory" in script
    assert "$VerifyReproducibleBuild" in script
    assert "windows-build-metadata.json" in script
    assert 'if ($PSCmdlet.ParameterSetName -eq "CiBuild")' in script


def test_packaging_black_box_tests_the_final_executable_on_a_random_port() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "blackbox-exe" in script
    assert "CODEX_HOME_MANAGER_PORT" in script
    assert "CODEX_HOME_MANAGER_NO_BROWSER" in script
    assert "Get-RandomLoopbackPort" in script


def test_packaging_checks_the_stable_executable_only_at_transactional_publish() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Assert-ReleaseDestinationAvailable" in script
    assert "[System.IO.FileShare]::None" in script
    assert "Publish-StagedReleaseSet" in script
    assert 'Join-Path $StableReleaseDirectory "codex-home-manager-local-win-x64.exe"' in script
    assert script.index("Stop-VerifiedReleaseDestinationProcesses -Path (Join-Path $StableReleaseDirectory") > script.index(
        "function Publish-StagedReleaseSet"
    )


def test_ci_packaging_never_mutates_the_stable_release_directory() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Stop-VerifiedReleaseDestinationProcesses" in script
    assert "ExecutablePath" in script
    assert "Stop-Process -Id $process.ProcessId -Force" in script
    ci_branch = script.index('if ($PSCmdlet.ParameterSetName -eq "CiBuild") {', script.index("quality_gate.py"))
    ci_end = script.index("\n    & python $releaseManifestScript prepare-windows-evidence", ci_branch)
    ci_text = script[ci_branch:ci_end]
    assert "Stop-VerifiedReleaseDestinationProcesses" not in ci_text
    assert "-Destination $directExecutablePath" not in ci_text
    assert "-Destination $archivePath" not in ci_text
    assert "Copy-Item -LiteralPath $firstBuild.Exe -Destination $ciExecutablePath -Force" in ci_text
    assert "Copy-Item -LiteralPath $firstBuild.Zip -Destination $ciArchivePath -Force" in ci_text
    release_prepare = script.index("prepare-windows-evidence")
    release_publish = script.index("Publish-StagedReleaseSet `", release_prepare)
    assert release_prepare < release_publish
    assert '$stableReleaseRoot = Join-Path $appDirectory "build\\releases"' in script
    assert '$releaseRoot = if ($PSCmdlet.ParameterSetName -eq "Release") { Join-Path $publicationStageRoot "release" }' in script


def test_packaging_retires_stale_signed_metadata_before_public_release_checks() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert '$staleSignedMetadataNames = @("release-manifest.json", "release-manifest.json.sig")' in script
    assert "SendToRecycleBin" in script
    retire_metadata = script.index("$staleSignedMetadataNames")
    public_check = script.index("$releaseNpmPath test")
    assert retire_metadata < public_check


def test_release_publishes_only_after_staged_public_boundary_and_source_drift_checks() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    staged_boundary = script.index("--root $publicValidationRoot")
    source_drift = script.rindex("validate-build-source")
    publish = script.index("Publish-StagedReleaseSet `", source_drift)
    assert staged_boundary < source_drift < publish
    assert "release-rollback" in script
    assert "site-rollback" in script


def test_packaging_syncs_and_verifies_public_dist_before_release_metadata() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "plan-public-dist-sync" in script
    assert "verify-public-dist" in script
    assert "copy_files" in script
    assert "stale_files" in script
    assert "SendToRecycleBin" in script
    assert "Assert-PublicDistRelativePath" in script
    assert "$copyPathSet.Contains($relativePath)" in script
    plan = script.index("plan-public-dist-sync")
    copy = script.index("[System.IO.File]::Copy", plan)
    recycle = script.index("SendToRecycleBin", copy)
    verify = script.index("verify-public-dist", recycle)
    assert plan < copy < recycle < verify


def test_checked_in_public_site_matches_current_manager_dist() -> None:
    public_site = manager_root.parent / "codex-home-manager-public" / "site"

    if not public_site.is_dir():
        assert (manager_root.parent / "SOURCE_COMMITS.json").is_file()
        assert (manager_root / "dist").is_dir()
        return

    release_manifest.verify_public_site_dist(manager_root / "dist", public_site)


def test_packaging_does_not_treat_untrusted_authenticode_as_trust() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Get-AuthenticodeSignature" in script
    assert "CODEX_HOME_MANAGER_SIGNING_CERT_THUMBPRINT" in script
    assert "detachedSignatureRequired" in script
    assert "New-SelfSignedCertificate" not in script
    assert 'status = "not-signed"' in script
    assert 'windowsStatus = "NotSigned"' in script
    assert "SignatureStatus]::NotSigned" in script
    assert '"public-trusted"' in script
    assert 'status = "self-signed"' not in script
    assert 'status = "untrusted"' not in script
    assert "PublicChainTrusted" in script
    assert "Cert:\\LocalMachine\\AuthRoot" in script
    assert "Cert:\\CurrentUser\\AuthRoot" in script
    assert "schemaVersion = 2" in script


def test_user_verifier_is_pinned_and_does_not_trust_same_origin_checksums() -> None:
    verifier = release_manifest.render_powershell_artifact_verifier(
        default_artifact_name="codex-home-manager-local-win-x64-v1.2.3-0123456789ab.exe",
        trusted_fingerprint=release_manifest.embedded_release_public_key_fingerprint,
    )

    assert f'$trustedPublicKeyFingerprint = "{release_manifest.embedded_release_public_key_fingerprint}"' in verifier
    assert "release-manifest.json.sig" in verifier
    assert "release-signing-public-key.pem" in verifier
    assert "DEPENDENCY_UNAVAILABLE" in verifier
    assert "serialization.PublicFormat.SubjectPublicKeyInfo" in verifier
    assert "public_key.verify(signature_bytes, manifest_bytes)" in verifier
    assert verifier.index("public_key.verify(signature_bytes, manifest_bytes)") < verifier.index("json.loads(manifest_bytes")
    assert "SHA256SUMS.txt" not in verifier
    assert "connector-release.json" not in verifier


def test_release_finalizer_distinguishes_draft_and_published_github_urls() -> None:
    script = finalize_script_path.read_text(encoding="utf-8")

    assert '$expectedDraftUrlPrefix = "https://github.com/$GithubRepository/releases/tag/untagged-"' in script
    assert "$actualHtmlUrl.StartsWith($expectedDraftUrlPrefix" in script
    assert "$actualHtmlUrl -cne $expectedHtmlUrl" in script
    assert "html_url = $expectedHtmlUrl" in script
    assert "api_html_url = $actualHtmlUrl" in script
    assert "Invoke-WebRequest -Uri ([string]$remoteAsset[0].url)" in script
    assert '$canonicalDownloadUrl = "https://github.com/$GithubRepository/releases/download/$GithubTag/$($artifact.name)"' in script
    assert "browser_download_url = $canonicalDownloadUrl" in script
    assert "api_browser_download_url = [string]$remoteAsset[0].browser_download_url" in script


def test_release_finalizer_retires_only_verified_stale_signed_metadata() -> None:
    script = finalize_script_path.read_text(encoding="utf-8")

    assert "$AllowSignedMetadataRetirement = $false" in script
    assert "$actualNameSet -ceq $retirableNameSet" in script
    assert "signed_metadata_retirement_required = $signedMetadataRetirementRequired" in script
    assert "function Remove-VerifiedStaleGithubSignedMetadata" in script
    assert "gh release delete-asset $GithubTag $metadataName --repo $GithubRepository --yes" in script
    assert script.index("Remove-VerifiedStaleGithubSignedMetadata") < script.index(
        'gh release upload $GithubTag'
    )


def test_release_finalizer_requires_source_evidence_in_exact_github_asset_set() -> None:
    script = finalize_script_path.read_text(encoding="utf-8")

    assert "source-release-evidence.json" in script
    assert "expectedSourceEvidenceNames" in script
    assert "source-provenance-attestation.sigstore.json" in script
    assert "source-sbom-attestation.sigstore.json" in script
    assert script.count("--source-evidence-proof") == 2
