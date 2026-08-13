from __future__ import annotations

import re
from pathlib import Path


manager_root = Path(__file__).resolve().parents[1]
workflow_root = (
    manager_root
    if (manager_root / ".github" / "workflows" / "source-ci.yml").is_file()
    else manager_root.parent
)
workflow_path = workflow_root / ".github" / "workflows" / "source-ci.yml"
codeql_path = workflow_root / ".github" / "workflows" / "codeql.yml"
dependabot_path = workflow_root / ".github" / "dependabot.yml"
security_path = manager_root / "SECURITY.md"
ci_requirements_path = workflow_root / ".github" / "workflows" / "requirements-ci.txt"


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


def test_source_ci_runs_on_the_exported_source_branch_with_layered_quality_gates() -> (
    None
):
    workflow = workflow_path.read_text(encoding="utf-8")

    assert workflow.count("branches: [source]") == 2
    assert "workflow_dispatch:" in workflow
    assert "pull_request_target:" not in workflow
    assert "runs-on: windows-latest" in workflow
    assert 'python-version: "3.12"' in workflow
    assert 'node-version: "22"' in workflow
    assert "--require-hashes --only-binary=:all:" in workflow
    assert "npm ci --ignore-scripts" in workflow
    assert "export_codex_home_manager_source.py verify --source ." in workflow
    assert '--junitxml="$junitPath"' in workflow
    assert "scripts/quality_gate.py" in workflow
    assert "source-ci-results-${{ github.sha }}" in workflow
    assert workflow.index("Initialize CI evidence") < workflow.index("Set up Python")
    assert workflow.index("Install locked Python dependencies") < workflow.index("Verify exported source integrity")
    assert workflow.index("Verify exported source integrity") < workflow.index("Install locked Node.js dependencies")
    assert workflow.count('Join-Path $env:RUNNER_TEMP "source-ci-artifacts"') == 3
    assert workflow.count("New-Item -ItemType Directory -Force $artifactRoot") == 2
    assert "path: ${{ runner.temp }}/source-ci-artifacts/" in workflow


def test_source_ci_uses_commit_pinned_actions_and_scoped_attestation_permissions() -> (
    None
):
    workflow = workflow_path.read_text(encoding="utf-8")
    action_references = re.findall(
        r"^\s*uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE
    )

    assert action_references
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference)
        for reference in action_references
    )
    assert "permissions:\n  contents: read" in workflow
    assert (
        "github.event_name != 'pull_request' && github.ref == 'refs/heads/source'"
        in workflow
    )
    assert "artifact-metadata: write" in workflow
    assert "attestations: write" in workflow
    assert "id-token: write" in workflow


def test_source_ci_generates_standard_sbom_and_provenance_evidence() -> None:
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "format: cyclonedx-json" in workflow
    assert "syft-version: v1.44.0" in workflow
    assert "sbom-path:" in workflow
    assert "subject-checksums: evidence/SHA256SUMS.txt" in workflow
    assert "source-sbom-attestation.sigstore.json" in workflow
    assert "source-provenance-attestation.sigstore.json" in workflow
    assert "source-release-evidence-${{ github.sha }}" in workflow
    assert "-CiBuild" in workflow
    assert "-VerifyReproducibleBuild" in workflow
    assert "windows-release-binaries-${{ github.sha }}" in workflow
    assert 'Copy-Item -LiteralPath $buildMetadataPath -Destination (Join-Path $outputRoot "windows-build-metadata.json")' in workflow
    assert "Generate binary CycloneDX SBOM with Syft" in workflow
    assert "BINARY-SBOM-SUBJECTS.txt" in workflow
    assert "BINARY-PROVENANCE-SUBJECTS.txt" in workflow
    assert "windows-sbom-attestation.sigstore.json" in workflow
    assert "windows-provenance-attestation.sigstore.json" in workflow
    assert "windows-release-evidence-${{ github.sha }}" in workflow
    assert workflow.count("runs-on: windows-latest") == 2
    assert "self-hosted" not in workflow


def test_codeql_and_dependabot_cover_the_exported_source_dependencies() -> None:
    codeql = codeql_path.read_text(encoding="utf-8")
    dependabot = dependabot_path.read_text(encoding="utf-8")

    action_references = re.findall(r"^\s*uses:\s*([^\s#]+)", codeql, flags=re.MULTILINE)
    assert action_references
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference) for reference in action_references)
    assert "github/codeql-action/init@" in codeql
    assert "github/codeql-action/analyze@" in codeql
    assert "language: [python, javascript-typescript]" in codeql
    assert "runs-on: windows-latest" in codeql
    assert "security-events: write" in codeql
    assert "build-mode: none" in codeql
    assert "package-ecosystem: github-actions" in dependabot
    assert "package-ecosystem: npm" in dependabot
    assert dependabot.count("package-ecosystem: pip") == 2
    assert 'directory: "/codex_home_manager"' in dependabot
    assert 'directory: "/.github/workflows"' in dependabot
    assert 'directory: "/codex_home_manager/packaging/windows"' in dependabot
    assert dependabot.count("target-branch: source") == 4


def test_security_policy_has_private_reporting_and_release_trust_boundaries() -> None:
    policy = security_path.read_text(encoding="utf-8")

    assert "security/advisories/new" in policy
    assert "Latest published release" in policy
    assert "Current `source` branch" in policy
    assert "3 business days" in policy
    assert "7 business days" in policy
    assert "NotSigned" in policy
    assert "detached Ed25519 manifest signature" in policy
    assert "public bug bounty" in policy


def test_source_ci_python_test_dependencies_are_hash_locked() -> None:
    blocks = requirement_blocks(ci_requirements_path.read_text(encoding="utf-8"))

    assert any(block.startswith("pytest==8.4.2 ") for block in blocks)
    assert any(block.startswith("pillow==12.1.1 ") for block in blocks)
    assert any(block.startswith("tomlkit==0.15.0 ") for block in blocks)
    assert any(block.startswith("cryptography==46.0.7 ") for block in blocks)
    assert any(block.startswith("httpx2==2.7.0 ") for block in blocks)
    for block in blocks:
        requirement = block.split(" --hash=", maxsplit=1)[0]
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s;]+", requirement)
        assert re.search(r"--hash=sha256:[0-9a-f]{64}(?:\s|$)", block)
