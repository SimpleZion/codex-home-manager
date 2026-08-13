from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import release_manifest


source_commit = "d" * 40
repository = "example/project"
signer_workflow = "github.com/example/project/.github/workflows/source-ci.yml"
version = "1.0.8"


def write_windows_evidence(directory: Path) -> dict[str, Path]:
    directory.mkdir()
    executable_content = b"final Source CI executable"
    archive_content = b"final Source CI archive"
    executable_hash = release_manifest.sha256_bytes(executable_content)
    archive_hash = release_manifest.sha256_bytes(archive_content)
    executable_name = f"codex-home-manager-local-win-x64-v{version}-{executable_hash[:12]}.exe"
    archive_name = f"codex-home-manager-local-win-x64-v{version}-{archive_hash[:12]}.zip"
    executable_path = directory / executable_name
    archive_path = directory / archive_name
    executable_path.write_bytes(executable_content)
    archive_path.write_bytes(archive_content)
    metadata = {
        "schemaVersion": 1,
        "version": version,
        "artifacts": [
            {
                "name": executable_name,
                "kind": "exe",
                "sha256": executable_hash,
                "size": len(executable_content),
                "audit": {
                    "method": "pyi-archive-viewer+strings",
                    "archiveEntryCount": 12,
                    "sourceFiles": [],
                    "sensitiveStrings": [],
                    "versionInfo": {
                        "FileVersion": version,
                        "ProductVersion": version,
                        "CompanyName": "SimpleZion",
                        "ProductName": "Codex Home Manager",
                        "FileDescription": "Codex Home Manager",
                    },
                },
                "authenticode": {
                    "status": "not-signed",
                    "windowsStatus": "NotSigned",
                    "trust": "none",
                    "signerThumbprint": None,
                    "signerSubject": None,
                    "selfSigned": False,
                    "chainTrusted": False,
                    "detachedSignatureRequired": True,
                },
            },
            {
                "name": archive_name,
                "kind": "zip",
                "sha256": archive_hash,
                "size": len(archive_content),
            },
        ],
    }
    metadata_path = directory / release_manifest.windows_build_metadata_name
    metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    sbom_name = f"codex-home-manager-windows-x64-{source_commit}.cdx.json"
    sbom_path = directory / sbom_name
    sbom_path.write_text(
        json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "serialNumber": "urn:uuid:test"}) + "\n",
        encoding="utf-8",
    )
    (directory / release_manifest.windows_sbom_subjects_name).write_text(
        "".join(f"{digest} *{name}\n" for name, digest in sorted({executable_name: executable_hash, archive_name: archive_hash}.items())),
        encoding="ascii",
    )
    provenance_subjects = {
        executable_name: executable_hash,
        archive_name: archive_hash,
        sbom_name: release_manifest.sha256_file(sbom_path),
    }
    (directory / release_manifest.windows_provenance_subjects_name).write_text(
        "".join(f"{digest} *{name}\n" for name, digest in sorted(provenance_subjects.items())),
        encoding="ascii",
    )
    for bundle_name in (
        release_manifest.windows_sbom_attestation_name,
        release_manifest.windows_provenance_attestation_name,
    ):
        (directory / bundle_name).write_text(
            '{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n',
            encoding="utf-8",
        )
    return {
        "executable": executable_path,
        "archive": archive_path,
        "metadata": metadata_path,
        "sbom": sbom_path,
    }


def prepare(tmp_path: Path, verifier):
    evidence_directory = tmp_path / "download"
    files = write_windows_evidence(evidence_directory)
    release_directory = tmp_path / "release"
    public_site_directory = tmp_path / "site"
    proof_path = tmp_path / release_manifest.windows_binary_evidence_proof_name
    proof = release_manifest.prepare_windows_binary_evidence(
        evidence_directory=evidence_directory,
        expected_source_commit=source_commit,
        expected_version=version,
        repository=repository,
        signer_workflow=signer_workflow,
        release_directory=release_directory,
        public_site_directory=public_site_directory,
        proof_path=proof_path,
        attestation_verifier=verifier,
    )
    return files, release_directory, public_site_directory, proof_path, proof


def test_prepares_only_verified_source_ci_windows_outputs_without_changing_bytes(tmp_path: Path) -> None:
    verification_calls: list[dict[str, object]] = []

    def verifier(**arguments: object) -> None:
        verification_calls.append(arguments)

    files, release_directory, public_site_directory, proof_path, proof = prepare(tmp_path, verifier)

    assert proof_path.read_bytes() == release_manifest.canonical_json_bytes(proof)
    assert proof["source_commit"] == source_commit
    assert proof["version"] == version
    assert {artifact["kind"] for artifact in proof["artifacts"]} == {"exe", "zip"}
    assert {asset["name"] for asset in proof["assets"]} == set(
        release_manifest.windows_binary_evidence_public_names(source_commit)
    )
    assert (release_directory / release_manifest.local_artifact_names[0]).read_bytes() == files["executable"].read_bytes()
    assert (release_directory / release_manifest.local_artifact_names[1]).read_bytes() == files["archive"].read_bytes()
    for artifact in proof["artifacts"]:
        assert (public_site_directory / artifact["name"]).read_bytes() == (
            files["executable"] if artifact["kind"] == "exe" else files["archive"]
        ).read_bytes()
    assert len(verification_calls) == 5
    assert all(call["repository"] == repository for call in verification_calls)
    assert all(call["signer_workflow"] == signer_workflow for call in verification_calls)
    assert all(call["source_commit"] == source_commit for call in verification_calls)
    assert sum(call["predicate_type"] == release_manifest.source_sbom_predicate_type for call in verification_calls) == 2
    assert sum(call["predicate_type"] == release_manifest.source_provenance_predicate_type for call in verification_calls) == 3


def test_gh_download_layout(tmp_path: Path) -> None:
    merged_directory = tmp_path / "merged"
    files = write_windows_evidence(merged_directory)
    download_directory = tmp_path / "download"
    binary_directory = download_directory / f"windows-release-binaries-{source_commit}"
    evidence_directory = download_directory / f"windows-release-evidence-{source_commit}"
    binary_directory.mkdir(parents=True)
    evidence_directory.mkdir(parents=True)
    for path in merged_directory.iterdir():
        if path.name == release_manifest.windows_build_metadata_name or path.suffix.lower() in {".exe", ".zip"}:
            (binary_directory / path.name).write_bytes(path.read_bytes())
        if path.name != release_manifest.windows_build_metadata_name:
            (evidence_directory / path.name).write_bytes(path.read_bytes())

    proof = release_manifest.prepare_windows_binary_evidence(
        evidence_directory=download_directory,
        expected_source_commit=source_commit,
        expected_version=version,
        repository=repository,
        signer_workflow=signer_workflow,
        release_directory=tmp_path / "release",
        public_site_directory=tmp_path / "site",
        proof_path=tmp_path / "proof.json",
        attestation_verifier=lambda **arguments: None,
    )

    assert {artifact["sha256"] for artifact in proof["artifacts"]} == {
        release_manifest.sha256_file(files["executable"]),
        release_manifest.sha256_file(files["archive"]),
    }


def test_rejects_extra_or_missing_windows_evidence_files(tmp_path: Path) -> None:
    evidence_directory = tmp_path / "download"
    write_windows_evidence(evidence_directory)
    (evidence_directory / "unexpected.log").write_text("not allowlisted\n", encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match="file set mismatch"):
        release_manifest.prepare_windows_binary_evidence(
            evidence_directory=evidence_directory,
            expected_source_commit=source_commit,
            expected_version=version,
            repository=repository,
            signer_workflow=signer_workflow,
            release_directory=tmp_path / "release",
            public_site_directory=tmp_path / "site",
            proof_path=tmp_path / "proof.json",
            attestation_verifier=lambda **arguments: None,
        )


def test_rejects_binary_subject_hash_drift_and_legacy_unsigned_claim(tmp_path: Path) -> None:
    evidence_directory = tmp_path / "download"
    files = write_windows_evidence(evidence_directory)
    subject_path = evidence_directory / release_manifest.windows_sbom_subjects_name
    subject_path.write_text(subject_path.read_text(encoding="ascii").replace(subject_path.read_text(encoding="ascii")[:64], "0" * 64), encoding="ascii")
    with pytest.raises(release_manifest.ReleaseManifestError, match="SBOM subject set"):
        release_manifest.prepare_windows_binary_evidence(
            evidence_directory=evidence_directory,
            expected_source_commit=source_commit,
            expected_version=version,
            repository=repository,
            signer_workflow=signer_workflow,
            release_directory=tmp_path / "release-a",
            public_site_directory=tmp_path / "site-a",
            proof_path=tmp_path / "proof-a.json",
            attestation_verifier=lambda **arguments: None,
        )

    metadata = json.loads(files["metadata"].read_text(encoding="utf-8"))
    metadata["artifacts"][0]["authenticode"]["status"] = "unavailable"
    metadata["artifacts"][0]["authenticode"].pop("windowsStatus")
    files["metadata"].write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    write_windows_evidence(tmp_path / "fresh")
    fresh_metadata = tmp_path / "fresh" / release_manifest.windows_build_metadata_name
    fresh_value = json.loads(fresh_metadata.read_text(encoding="utf-8"))
    fresh_value["artifacts"][0]["authenticode"]["status"] = "unavailable"
    fresh_value["artifacts"][0]["authenticode"].pop("windowsStatus")
    fresh_metadata.write_text(json.dumps(fresh_value) + "\n", encoding="utf-8")
    with pytest.raises(release_manifest.ReleaseManifestError, match="Authenticode"):
        release_manifest.prepare_windows_binary_evidence(
            evidence_directory=tmp_path / "fresh",
            expected_source_commit=source_commit,
            expected_version=version,
            repository=repository,
            signer_workflow=signer_workflow,
            release_directory=tmp_path / "release-b",
            public_site_directory=tmp_path / "site-b",
            proof_path=tmp_path / "proof-b.json",
            attestation_verifier=lambda **arguments: None,
        )


def test_rejects_failed_official_attestation_verification(tmp_path: Path) -> None:
    def verifier(**arguments: object) -> None:
        raise release_manifest.ReleaseManifestError("GitHub attestation verification failed")

    with pytest.raises(release_manifest.ReleaseManifestError, match="GitHub attestation verification failed"):
        prepare(tmp_path, verifier)
