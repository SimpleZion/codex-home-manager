from __future__ import annotations

import re
from pathlib import Path

from scripts import release_manifest


manager_root = Path(__file__).resolve().parents[1]
requirements_path = manager_root / "packaging" / "windows" / "requirements-connector.txt"
package_script_path = manager_root / "scripts" / "package-local-connector.ps1"
finalize_script_path = manager_root / "scripts" / "finalize-release-manifest.ps1"


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


def test_packaging_uses_content_addressed_artifacts_and_stable_latest_redirects() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Get-ContentAddressedReleaseName" in script
    assert "codex-home-manager-local-win-x64-v$releaseVersion-" in script
    assert '"/codex-home-manager-local-win-x64.exe"' in script
    assert '"/downloads/latest/windows-x64.exe"' in script
    assert "Cache-Control: no-store" in script
    assert "immutable" in script


def test_packaging_audits_zip_and_pyinstaller_executable_before_public_copy() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Assert-ReleaseZipBoundary" in script
    assert "Assert-PyInstallerExecutableBoundary" in script
    assert "pyi-archive_viewer" in script
    assert "connector-release.json" in script
    assert "sensitiveStrings" in script
    assert "sourceFiles" in script


def test_packaging_recreates_and_audits_public_node_dependencies_from_lock() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "$releaseNpmPath ci --ignore-scripts" in script
    assert "$releaseNpmPath audit --audit-level=high" in script
    assert "$releaseNpmPath run check" in script


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
    assert script.index("prepare-source-evidence") < script.index("$releaseNpmPath run check")


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


def test_packaging_black_box_tests_the_final_executable_on_a_random_port() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "blackbox-exe" in script
    assert "CODEX_HOME_MANAGER_PORT" in script
    assert "CODEX_HOME_MANAGER_NO_BROWSER" in script
    assert "Get-RandomLoopbackPort" in script


def test_packaging_fails_fast_when_the_release_executable_is_locked() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Assert-ReleaseDestinationAvailable" in script
    assert "[System.IO.FileShare]::None" in script
    assert script.index("Assert-ReleaseDestinationAvailable -Path $directExecutablePath") < script.index(
        '& python "scripts\\quality_gate.py"'
    )


def test_packaging_stops_only_verified_old_connector_processes_before_final_copy() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert "Stop-VerifiedReleaseDestinationProcesses" in script
    assert "ExecutablePath" in script
    assert "Stop-Process -Id $process.ProcessId -Force" in script
    stop_call = script.index("Stop-VerifiedReleaseDestinationProcesses -Path $directExecutablePath")
    final_copy = script.index("Copy-Item -LiteralPath $firstBuild.Exe -Destination $directExecutablePath -Force")
    compare_builds = script.index("compare-builds")
    assert compare_builds < stop_call < final_copy


def test_packaging_retires_stale_signed_metadata_before_public_release_checks() -> None:
    script = package_script_path.read_text(encoding="utf-8")

    assert '$staleSignedMetadataNames = @("release-manifest.json", "release-manifest.json.sig")' in script
    assert "SendToRecycleBin" in script
    retire_metadata = script.index("$staleSignedMetadataNames")
    public_check = script.index("$releaseNpmPath run check")
    assert retire_metadata < public_check


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
