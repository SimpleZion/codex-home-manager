from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts import release_manifest


source_commit = "a" * 40
root_commit = "b" * 40
manager_commit = "c" * 40
repository = "example/codex-home-manager"
signer_workflow = "github.com/example/codex-home-manager/.github/workflows/source-ci.yml"


def write_source_evidence(directory: Path) -> dict[str, Path]:
    directory.mkdir()
    archive = directory / f"codex-home-manager-source-{source_commit}.zip"
    source_commits = {
        "schemaVersion": 2,
        "sources": {
            "rootRepository": {"branch": "master", "commit": root_commit, "files": []},
            "managerRepository": {"branch": "master", "commit": manager_commit, "files": []},
        },
    }
    with zipfile.ZipFile(archive, "w") as source_zip:
        source_zip.writestr("SOURCE_COMMITS.json", json.dumps(source_commits))
        source_zip.writestr("codex_home_manager/package.json", "{}\n")

    junit = directory / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="12" failures="0" errors="0" skipped="1" time="2.5" /></testsuites>\n',
        encoding="utf-8",
    )
    quality_status = directory / "quality-gate-status.txt"
    quality_status.write_text("passed\n", encoding="utf-8")
    quality_log = directory / "quality-gate.log"
    quality_log.write_text("quality gate passed\n", encoding="utf-8")
    test_summary = directory / "test-summary.md"
    test_summary.write_text(
        "# Source CI test summary\n\n"
        "- Tests: 12\n- Failures: 0\n- Errors: 0\n- Skipped: 1\n"
        "- Pytest time: 2.500 seconds\n- Complete quality gate: passed\n",
        encoding="utf-8",
    )
    sbom = directory / f"codex-home-manager-source-{source_commit}.cdx.json"
    sbom.write_text(
        json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "serialNumber": "urn:uuid:test", "version": 1, "components": []}) + "\n",
        encoding="utf-8",
    )

    checksum_subjects = [archive, junit, quality_log, quality_status, sbom, test_summary]
    checksum = directory / "SHA256SUMS.txt"
    checksum.write_text(
        "".join(f"{release_manifest.sha256_file(path)} *evidence/{path.name}\n" for path in sorted(checksum_subjects)),
        encoding="ascii",
    )
    sbom_bundle = directory / "source-sbom-attestation.sigstore.json"
    provenance_bundle = directory / "source-provenance-attestation.sigstore.json"
    sbom_bundle.write_text('{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n', encoding="utf-8")
    provenance_bundle.write_text('{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n', encoding="utf-8")
    return {
        "archive": archive,
        "junit": junit,
        "quality_log": quality_log,
        "quality_status": quality_status,
        "test_summary": test_summary,
        "sbom": sbom,
        "checksum": checksum,
        "sbom_bundle": sbom_bundle,
        "provenance_bundle": provenance_bundle,
    }


def prepare(tmp_path: Path, verifier=None):
    evidence_directory = tmp_path / "evidence"
    files = write_source_evidence(evidence_directory)
    release_directory = tmp_path / "release"
    public_site_directory = tmp_path / "site"
    release_directory.mkdir()
    public_site_directory.mkdir()
    (public_site_directory / "SHA256SUMS.txt").write_text("", encoding="ascii")
    proof_path = tmp_path / "source-evidence.json"
    calls: list[dict[str, object]] = []

    def successful_verifier(**arguments: object) -> None:
        calls.append(arguments)

    release_manifest.prepare_source_release_evidence(
        evidence_directory=evidence_directory,
        expected_source_commit=source_commit,
        expected_build_sources={
            "root": {"head": root_commit, "branch": "master", "clean": True},
            "manager": {"head": manager_commit, "branch": "master", "clean": True},
        },
        repository=repository,
        signer_workflow=signer_workflow,
        release_directory=release_directory,
        public_site_directory=public_site_directory,
        proof_path=proof_path,
        attestation_verifier=verifier or successful_verifier,
    )
    return files, release_directory, public_site_directory, proof_path, calls


def test_prepares_only_public_source_evidence_with_stable_names(tmp_path: Path) -> None:
    files, release_directory, public_site_directory, proof_path, calls = prepare(tmp_path)

    expected_names = set(release_manifest.source_evidence_public_names)
    assert {path.name for path in release_directory.iterdir()} == expected_names
    assert expected_names <= {path.name for path in public_site_directory.iterdir()}
    assert files["quality_log"].name not in expected_names
    assert files["junit"].name not in expected_names
    assert len(calls) == 7
    assert all(call["repository"] == repository for call in calls)
    assert all(call["signer_workflow"] == signer_workflow for call in calls)
    assert all(call["source_commit"] == source_commit for call in calls)
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["schema_version"] == 1
    assert proof["source_commit"] == source_commit
    assert proof["source_commits"] == {"root": root_commit, "manager": manager_commit}
    assert proof["quality"] == {"tests": 12, "failures": 0, "errors": 0, "skipped": 1, "pytest_seconds": 2.5}
    assert {record["name"] for record in proof["assets"]} == expected_names
    assert "quality-gate.log" not in public_site_directory.joinpath("SHA256SUMS.txt").read_text(encoding="ascii")


@pytest.mark.parametrize("removed_name", ["junit.xml", "source-provenance-attestation.sigstore.json"])
def test_rejects_missing_evidence_file(tmp_path: Path, removed_name: str) -> None:
    directory = tmp_path / "evidence"
    write_source_evidence(directory)
    (directory / removed_name).unlink()

    with pytest.raises(release_manifest.ReleaseManifestError, match="source evidence file set mismatch"):
        prepare_existing(directory, tmp_path)


def test_rejects_extra_evidence_file(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    write_source_evidence(directory)
    (directory / "extra.txt").write_text("extra\n", encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match="source evidence file set mismatch"):
        prepare_existing(directory, tmp_path)


def test_rejects_checksum_tampering(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    files = write_source_evidence(directory)
    files["sbom"].write_text("{}\n", encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match="source evidence SHA256 mismatch"):
        prepare_existing(directory, tmp_path)


def test_rejects_wrong_source_commit(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    write_source_evidence(directory)

    with pytest.raises(release_manifest.ReleaseManifestError, match="source evidence commit mismatch"):
        prepare_existing(directory, tmp_path, expected_source_commit="d" * 40)


def test_rejects_failed_ci_even_when_files_are_present(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    files = write_source_evidence(directory)
    files["quality_status"].write_text("failed\n", encoding="utf-8")
    rewrite_checksum(files)

    with pytest.raises(release_manifest.ReleaseManifestError, match="quality gate did not pass"):
        prepare_existing(directory, tmp_path)


def test_rejects_private_paths_in_public_evidence(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    files = write_source_evidence(directory)
    files["sbom"].write_text(
        json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "serialNumber": "urn:uuid:test", "comment": r"C:\Users\secret\project"}),
        encoding="utf-8",
    )
    rewrite_checksum(files)

    with pytest.raises(release_manifest.ReleaseManifestError, match="privacy policy"):
        prepare_existing(directory, tmp_path)


def test_rejects_a_bundle_when_official_attestation_verification_fails(tmp_path: Path) -> None:
    def rejected_verifier(**_arguments: object) -> None:
        raise release_manifest.ReleaseManifestError("GitHub attestation verification failed")

    with pytest.raises(release_manifest.ReleaseManifestError, match="GitHub attestation verification failed"):
        prepare(tmp_path, verifier=rejected_verifier)


def test_official_attestation_verifier_pins_repository_workflow_commit_and_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_command: list[str] = []

    class Result:
        returncode = 0
        stdout = "[{}]"
        stderr = ""

    def fake_run(command: list[str], **_arguments: object) -> Result:
        captured_command.extend(command)
        return Result()

    monkeypatch.setattr(release_manifest.subprocess, "run", fake_run)
    subject = tmp_path / "subject.zip"
    bundle = tmp_path / "bundle.json"
    subject.write_bytes(b"subject")
    bundle.write_text("{}", encoding="utf-8")

    release_manifest.verify_github_attestation(
        subject_path=subject,
        bundle_path=bundle,
        repository=repository,
        signer_workflow=signer_workflow,
        source_commit=source_commit,
        predicate_type=release_manifest.source_sbom_predicate_type,
    )

    assert captured_command[:3] == ["gh", "attestation", "verify"]
    for flag, value in (
        ("--bundle", str(bundle)),
        ("--repo", repository),
        ("--signer-workflow", signer_workflow),
        ("--source-digest", source_commit),
        ("--source-ref", "refs/heads/source"),
        ("--predicate-type", release_manifest.source_sbom_predicate_type),
    ):
        index = captured_command.index(flag)
        assert captured_command[index + 1] == value
    assert "--deny-self-hosted-runners" in captured_command
    assert captured_command[-2:] == ["--format", "json"]


def rewrite_checksum(files: dict[str, Path]) -> None:
    subjects = [files[name] for name in ("archive", "junit", "quality_log", "quality_status", "sbom", "test_summary")]
    files["checksum"].write_text(
        "".join(f"{release_manifest.sha256_file(path)} *evidence/{path.name}\n" for path in sorted(subjects)),
        encoding="ascii",
    )


def prepare_existing(
    evidence_directory: Path,
    tmp_path: Path,
    *,
    expected_source_commit: str = source_commit,
) -> None:
    release_directory = tmp_path / "release"
    public_site_directory = tmp_path / "site"
    release_directory.mkdir(exist_ok=True)
    public_site_directory.mkdir(exist_ok=True)
    (public_site_directory / "SHA256SUMS.txt").write_text("", encoding="ascii")
    release_manifest.prepare_source_release_evidence(
        evidence_directory=evidence_directory,
        expected_source_commit=expected_source_commit,
        expected_build_sources={
            "root": {"head": root_commit, "branch": "master", "clean": True},
            "manager": {"head": manager_commit, "branch": "master", "clean": True},
        },
        repository=repository,
        signer_workflow=signer_workflow,
        release_directory=release_directory,
        public_site_directory=public_site_directory,
        proof_path=tmp_path / "proof.json",
        attestation_verifier=lambda **_arguments: None,
    )
