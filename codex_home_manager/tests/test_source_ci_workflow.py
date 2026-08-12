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
