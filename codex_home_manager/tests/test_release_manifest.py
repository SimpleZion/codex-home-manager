from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import release_manifest


def test_download_bytes_uses_github_release_api_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_headers: dict[str, str] = {}

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request: object, timeout: int) -> Response:
        assert timeout == 30
        captured_headers.update(dict(request.header_items()))
        return Response()

    monkeypatch.setattr(release_manifest, "urlopen", fake_urlopen)

    assert release_manifest.download_bytes(
        "https://api.github.com/repos/example/project/releases/tags/v1.0.1"
    ) == b"{}"
    assert captured_headers["Accept"] == "application/vnd.github+json"
    assert captured_headers["X-github-api-version"] == "2022-11-28"


def test_authenticode_evidence_never_treats_self_signed_as_public_trust() -> None:
    self_signed = {
        "status": "self-signed",
        "trust": "untrusted",
        "signerThumbprint": "A" * 40,
        "signerSubject": "CN=Self-Signed Test",
        "selfSigned": True,
        "chainTrusted": False,
        "detachedSignatureRequired": True,
    }
    with pytest.raises(release_manifest.ReleaseManifestError, match="inconsistent Authenticode"):
        release_manifest.validate_authenticode_evidence(self_signed)

    forged_public_trust = {**self_signed, "status": "valid", "trust": "public-trusted", "chainTrusted": True}
    with pytest.raises(release_manifest.ReleaseManifestError, match="inconsistent Authenticode"):
        release_manifest.validate_authenticode_evidence(forged_public_trust)


def run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Release Test", "-c", "user.email=release@example.invalid", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_repository(path: Path, filename: str) -> Path:
    path.mkdir(parents=True)
    run_git(path, "init", "--quiet")
    run_git(path, "branch", "-M", "main")
    (path / filename).write_text(f"{path.name}\n", encoding="utf-8")
    run_git(path, "add", filename)
    run_git(path, "commit", "--quiet", "-m", "initial")
    return path


def write_public_bundle(site_directory: Path, executable: Path, archive: Path) -> tuple[Path, Path]:
    executable_name = "codex-home-manager-local-win-x64-v1.0.0-" + release_manifest.sha256_file(executable)[:12] + ".exe"
    archive_name = "codex-home-manager-local-win-x64-v1.0.0-" + release_manifest.sha256_file(archive)[:12] + ".zip"
    executable_target = site_directory / executable_name
    archive_target = site_directory / archive_name
    executable_target.write_bytes(executable.read_bytes())
    archive_target.write_bytes(archive.read_bytes())
    bundle = site_directory / "connector-release.json"
    bundle.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "version": "1.0.0",
                "artifacts": [
                    {
                        "name": executable_name,
                        "kind": "exe",
                        "sha256": release_manifest.sha256_file(executable_target),
                        "size": executable_target.stat().st_size,
                        "audit": {
                            "method": "pyi-archive-viewer+strings",
                            "archiveEntryCount": 1,
                            "sourceFiles": [],
                            "sensitiveStrings": [],
                            "versionInfo": {
                                "FileVersion": "1.0.0",
                                "ProductVersion": "1.0.0",
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
                        "sha256": release_manifest.sha256_file(archive_target),
                        "size": archive_target.stat().st_size,
                    },
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    checksum = site_directory / "SHA256SUMS.txt"
    checksum.write_text(
        "\n".join(
            f"{release_manifest.sha256_file(path)}  {path.name}"
            for path in (executable_target, archive_target, bundle)
        )
        + "\n",
        encoding="utf-8",
    )
    return bundle, checksum


def write_source_evidence_assets(
    release_directory: Path,
    site_directory: Path,
    checksum_path: Path,
    *,
    root_commit: str,
    manager_commit: str,
) -> tuple[Path, dict[str, object]]:
    contents = {
        "codex-home-manager-source.zip": b"source archive fixture",
        "codex-home-manager-source.cdx.json": b'{"bomFormat":"CycloneDX","specVersion":"1.6","serialNumber":"urn:uuid:test"}\n',
        "source-ci-test-summary.md": b"# Source CI test summary\n\n- Tests: 12\n- Failures: 0\n- Errors: 0\n",
        "source-provenance-attestation.sigstore.json": b'{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n',
        "source-sbom-attestation.sigstore.json": b'{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n',
    }
    assets = []
    for name, content in contents.items():
        (release_directory / name).write_bytes(content)
        (site_directory / name).write_bytes(content)
        assets.append({"name": name, "sha256": release_manifest.sha256_bytes(content), "size": len(content)})
    checksum_entries = release_manifest.load_checksum_entries(checksum_path)
    checksum_entries.update({asset["name"]: asset["sha256"] for asset in assets})
    checksum_path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksum_entries.items())),
        encoding="ascii",
    )
    proof = {
        "schema_version": 1,
        "source_commit": "d" * 40,
        "source_ref": "refs/heads/source",
        "source_commits": {"root": root_commit, "manager": manager_commit},
        "repository": "example/project",
        "signer_workflow": "github.com/example/project/.github/workflows/source-ci.yml",
        "attestations": {
            "verifier": "gh attestation verify",
            "deny_self_hosted_runners": True,
            "sbom_predicate_type": release_manifest.source_sbom_predicate_type,
            "provenance_predicate_type": release_manifest.source_provenance_predicate_type,
        },
        "quality": {"tests": 12, "failures": 0, "errors": 0, "skipped": 1, "pytest_seconds": 2.5},
        "assets": sorted(assets, key=lambda asset: asset["name"]),
    }
    proof_path = release_directory / "source-release-evidence.json"
    proof_path.write_bytes(release_manifest.canonical_json_bytes(proof))
    return proof_path, proof


def write_windows_binary_evidence_assets(
    release_directory: Path,
    site_directory: Path,
    checksum_path: Path,
    bundle_path: Path,
) -> tuple[Path, dict[str, object]]:
    public_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    artifacts = sorted(public_bundle["artifacts"], key=lambda artifact: artifact["name"])
    sbom_name = f"codex-home-manager-windows-x64-{'d' * 40}.cdx.json"
    metadata = {"schemaVersion": 1, "version": public_bundle["version"], "artifacts": artifacts}
    contents = {
        release_manifest.windows_build_metadata_name: (json.dumps(metadata) + "\n").encode(),
        sbom_name: b'{"bomFormat":"CycloneDX","specVersion":"1.6","serialNumber":"urn:uuid:test"}\n',
        release_manifest.windows_sbom_subjects_name: "".join(
            f"{artifact['sha256']} *{artifact['name']}\n" for artifact in artifacts
        ).encode("ascii"),
        release_manifest.windows_sbom_attestation_name: b'{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n',
        release_manifest.windows_provenance_attestation_name: b'{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n',
    }
    provenance_subjects = {
        **{artifact["name"]: artifact["sha256"] for artifact in artifacts},
        sbom_name: release_manifest.sha256_bytes(contents[sbom_name]),
    }
    contents[release_manifest.windows_provenance_subjects_name] = "".join(
        f"{digest} *{name}\n" for name, digest in sorted(provenance_subjects.items())
    ).encode("ascii")
    assets = []
    for name, content in contents.items():
        (release_directory / name).write_bytes(content)
        (site_directory / name).write_bytes(content)
        assets.append({"name": name, "sha256": release_manifest.sha256_bytes(content), "size": len(content)})
    checksum_entries = release_manifest.load_checksum_entries(checksum_path)
    checksum_entries.update({asset["name"]: asset["sha256"] for asset in assets})
    checksum_path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksum_entries.items())),
        encoding="ascii",
    )
    artifact_names = {artifact["name"] for artifact in artifacts}
    proof = {
        "schema_version": 1,
        "source_commit": "d" * 40,
        "source_ref": "refs/heads/source",
        "repository": "example/project",
        "signer_workflow": "github.com/example/project/.github/workflows/source-ci.yml",
        "version": "1.0.0",
        "attestations": {
            "verifier": "gh attestation verify",
            "deny_self_hosted_runners": True,
            "sbom_predicate_type": release_manifest.source_sbom_predicate_type,
            "provenance_predicate_type": release_manifest.source_provenance_predicate_type,
            "sbom_subjects": sorted(artifact_names),
            "provenance_subjects": sorted(artifact_names | {sbom_name}),
        },
        "artifacts": artifacts,
        "assets": sorted(assets, key=lambda asset: asset["name"]),
    }
    proof_path = release_directory / release_manifest.windows_binary_evidence_proof_name
    proof_path.write_bytes(release_manifest.canonical_json_bytes(proof))
    return proof_path, proof


@pytest.fixture
def release_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path | str]:
    root_repository = create_repository(tmp_path / "root", "root.txt")
    manager_repository = create_repository(tmp_path / "manager", "manager.txt")
    public_repository = create_repository(tmp_path / "public", "public.txt")
    (manager_repository / ".gitignore").write_text("dist/\nbuild/\n", encoding="utf-8")
    run_git(manager_repository, "add", ".gitignore")
    run_git(manager_repository, "commit", "--quiet", "-m", "ignore release outputs")
    dist_directory = manager_repository / "dist"
    dist_directory.mkdir()
    (dist_directory / "index.html").write_text("<main>release</main>\n", encoding="utf-8")
    (dist_directory / "assets").mkdir()
    (dist_directory / "assets" / "app.js").write_text("console.log('release');\n", encoding="utf-8")

    release_directory = manager_repository / "build" / "releases"
    release_directory.mkdir(parents=True)
    executable = release_directory / "codex-home-manager-local-win-x64.exe"
    archive = release_directory / "codex-home-manager-local-win-x64.zip"
    executable.write_bytes(b"signed executable fixture")
    archive.write_bytes(b"signed archive fixture")

    signing_directory = tmp_path / "signing"
    monkeypatch.setattr(release_manifest, "release_signing_directory", signing_directory)
    private_key = signing_directory / "release-signing-key.pem"
    public_key = release_directory / "release-signing-public-key.pem"
    trusted_fingerprint = signing_directory / "release-signing-public-key.sha256"
    release_manifest.generate_key_pair(private_key, public_key, trusted_fingerprint)

    site_directory = public_repository / "site"
    site_directory.mkdir()
    for dist_path in dist_directory.rglob("*"):
        if dist_path.is_file():
            public_path = site_directory / dist_path.relative_to(dist_directory)
            public_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dist_path, public_path)
    bundle, checksum = write_public_bundle(site_directory, executable, archive)
    source_evidence_proof, source_evidence = write_source_evidence_assets(
        release_directory,
        site_directory,
        checksum,
        root_commit=run_git(root_repository, "rev-parse", "HEAD"),
        manager_commit=run_git(manager_repository, "rev-parse", "HEAD"),
    )
    windows_evidence_proof, windows_evidence = write_windows_binary_evidence_assets(
        release_directory,
        site_directory,
        checksum,
        bundle,
    )
    public_key_target = site_directory / public_key.name
    public_key_target.write_bytes(public_key.read_bytes())
    (site_directory / "release-signing-public-key.sha256").write_text(
        trusted_fingerprint.read_text(encoding="ascii"), encoding="ascii"
    )
    verifier_path = site_directory / "codex-home-manager-verifier-win-x64.exe"
    verifier_path.write_bytes(b"self-contained verifier fixture")
    powershell_verifier_path = site_directory / "verify-codex-home-manager.ps1"
    powershell_verifier_path.write_text("# transparent verifier fixture\n", encoding="utf-8")
    checksum_entries = release_manifest.load_checksum_entries(checksum)
    for path in (verifier_path, powershell_verifier_path, public_key_target, site_directory / "release-signing-public-key.sha256"):
        checksum_entries[path.name] = release_manifest.sha256_file(path)
    checksum.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksum_entries.items())),
        encoding="ascii",
    )
    run_git(public_repository, "add", "site")
    run_git(public_repository, "commit", "--quiet", "-m", "artifact phase")

    manifest = release_directory / "release-manifest.json"
    signature = release_directory / "release-manifest.json.sig"
    build_snapshot = release_directory / "release-build-source.json"
    public_snapshot = release_directory / "release-artifact-public-source.json"
    deployment_evidence = release_directory / "artifact-deployment.json"
    github_evidence = release_directory / "github-release.json"
    deployment_id = "7d7aeac7-23a7-4eca-bc4a-c76c515727c0"
    github_repository = "example/project"
    github_tag = "v1.0.0"
    github_release_id = 42

    release_manifest.capture_build_source_state(
        build_snapshot,
        root_repository=root_repository,
        manager_repository=manager_repository,
    )
    release_manifest.capture_artifact_public_source_state(
        public_snapshot,
        public_repository=public_repository,
        artifact_deployment_id=deployment_id,
    )
    public_state = json.loads(public_snapshot.read_text(encoding="utf-8"))["sources"]["artifact_public"]
    deployment_evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "deployment": {
                    "id": deployment_id,
                    "project": "codex-home-manager",
                    "branch": "main",
                    "public_commit": public_state["head"],
                    "url": "https://artifact-deployment.codex-home-manager.pages.dev",
                    "status": "success",
                },
            }
        ),
        encoding="utf-8",
    )
    public_bundle = json.loads(bundle.read_text(encoding="utf-8"))
    github_evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release": {
                    "id": github_release_id,
                    "tag_name": github_tag,
                    "html_url": f"https://github.com/{github_repository}/releases/tag/{github_tag}",
                    "draft": True,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": artifact["name"],
                            "size": artifact["size"],
                            "sha256": artifact["sha256"],
                            "browser_download_url": f"https://github.com/{github_repository}/releases/download/{github_tag}/{artifact['name']}",
                        }
                        for artifact in [
                            *public_bundle["artifacts"],
                            {
                                "name": verifier_path.name,
                                "size": verifier_path.stat().st_size,
                                "sha256": release_manifest.sha256_file(verifier_path),
                            },
                            *source_evidence["assets"],
                        ]
                    ],
                },
                "repository": github_repository,
            }
        ),
        encoding="utf-8",
    )
    release_manifest.create_signed_manifest(
        build_source_snapshot_path=build_snapshot,
        artifact_public_source_snapshot_path=public_snapshot,
        dist_directory=dist_directory,
        release_directory=release_directory,
        public_site_directory=site_directory,
        artifact_deployment_evidence_path=deployment_evidence,
        github_release_evidence_path=github_evidence,
        source_evidence_proof_path=source_evidence_proof,
        private_key_path=private_key,
        trusted_public_key_fingerprint_path=trusted_fingerprint,
        manifest_path=manifest,
        signature_path=signature,
    )

    return {
        "root_repository": root_repository,
        "manager_repository": manager_repository,
        "public_repository": public_repository,
        "dist_directory": dist_directory,
        "release_directory": release_directory,
        "site_directory": site_directory,
        "executable": executable,
        "archive": archive,
        "verifier": verifier_path,
        "bundle": bundle,
        "checksum": checksum,
        "private_key": private_key,
        "public_key": public_key,
        "trusted_fingerprint": trusted_fingerprint,
        "manifest": manifest,
        "signature": signature,
        "build_snapshot": build_snapshot,
        "public_snapshot": public_snapshot,
        "deployment_evidence": deployment_evidence,
        "github_evidence": github_evidence,
        "source_evidence_proof": source_evidence_proof,
        "windows_evidence_proof": windows_evidence_proof,
        "windows_evidence": windows_evidence,
        "deployment_id": deployment_id,
        "github_repository": github_repository,
        "github_tag": github_tag,
        "github_release_id": github_release_id,
    }


def verify_release(release_fixture: dict[str, Path | str], **overrides: object) -> None:
    arguments = {
        "manifest_path": release_fixture["manifest"],
        "signature_path": release_fixture["signature"],
        "public_key_path": release_fixture["public_key"],
        "trusted_public_key_fingerprint_path": release_fixture["trusted_fingerprint"],
        "root_repository": release_fixture["root_repository"],
        "manager_repository": release_fixture["manager_repository"],
        "public_repository": release_fixture["public_repository"],
        "dist_directory": release_fixture["dist_directory"],
        "release_directory": release_fixture["release_directory"],
        "public_site_directory": release_fixture["site_directory"],
        "artifact_deployment_evidence_path": release_fixture["deployment_evidence"],
        "github_release_evidence_path": release_fixture["github_evidence"],
        "source_evidence_proof_path": release_fixture["source_evidence_proof"],
    }
    arguments.update(overrides)
    release_manifest.verify_release(**arguments)


def test_user_artifact_verifier_rejects_manifest_signature_pem_and_artifact_tampering(
    release_fixture: dict[str, Path | str], tmp_path: Path
) -> None:
    verifier_path = tmp_path / "verify_release_artifact.py"
    verifier_path.write_text(release_manifest.user_artifact_verifier_source, encoding="utf-8")
    manifest = json.loads(Path(release_fixture["manifest"]).read_text(encoding="utf-8"))
    executable_record = next(
        record
        for record in manifest["public_artifacts"]
        if record["path"].startswith("codex-home-manager-local-win-x64-v") and record["path"].endswith(".exe")
    )
    paths = {
        "manifest": tmp_path / "release-manifest.json",
        "signature": tmp_path / "release-manifest.json.sig",
        "public_key": tmp_path / "release-signing-public-key.pem",
        "artifact": tmp_path / executable_record["path"],
    }
    originals = {
        "manifest": Path(release_fixture["manifest"]).read_bytes(),
        "signature": Path(release_fixture["signature"]).read_bytes(),
        "public_key": Path(release_fixture["public_key"]).read_bytes(),
        "artifact": (Path(release_fixture["site_directory"]) / executable_record["path"]).read_bytes(),
    }
    trusted_fingerprint = Path(release_fixture["trusted_fingerprint"]).read_text(encoding="ascii").strip()

    def run_verifier() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(verifier_path),
                "--manifest",
                str(paths["manifest"]),
                "--signature",
                str(paths["signature"]),
                "--public-key",
                str(paths["public_key"]),
                "--trusted-public-key-fingerprint",
                trusted_fingerprint,
                "--artifact-kind",
                "exe",
                "--artifact",
                str(paths["artifact"]),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    for name, content in originals.items():
        paths[name].write_bytes(content)
    assert run_verifier().returncode == 0

    tampered_signature = bytearray(base64.b64decode(originals["signature"].strip(), validate=True))
    tampered_signature[0] ^= 1
    replacement_public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    cases = (
        ("manifest", originals["manifest"] + b" ", "signature verification failed"),
        ("signature", base64.b64encode(bytes(tampered_signature)) + b"\n", "signature verification failed"),
        ("public_key", replacement_public_key, "SPKI SHA-256"),
        ("artifact", originals["artifact"] + b"tampered", "artifact size mismatch"),
    )
    for target, tampered_content, expected_error in cases:
        for name, content in originals.items():
            paths[name].write_bytes(content)
        paths[target].write_bytes(tampered_content)
        result = run_verifier()
        assert result.returncode != 0, target
        assert expected_error in result.stderr, result.stderr


def test_self_contained_verifier_entry_accepts_valid_artifact_and_rejects_tampering(
    release_fixture: dict[str, Path | str], tmp_path: Path
) -> None:
    manager_root = Path(__file__).resolve().parents[1]
    manifest = json.loads(Path(release_fixture["manifest"]).read_text(encoding="utf-8"))
    executable_record = next(
        record
        for record in manifest["public_artifacts"]
        if record["path"].startswith("codex-home-manager-local-win-x64-v") and record["path"].endswith(".exe")
    )
    metadata_directory = tmp_path / "metadata"
    metadata_directory.mkdir()
    for name, source in (
        ("release-manifest.json", Path(release_fixture["manifest"])),
        ("release-manifest.json.sig", Path(release_fixture["signature"])),
        ("release-signing-public-key.pem", Path(release_fixture["public_key"])),
    ):
        (metadata_directory / name).write_bytes(source.read_bytes())
    artifact = tmp_path / executable_record["path"]
    artifact.write_bytes((Path(release_fixture["site_directory"]) / executable_record["path"]).read_bytes())
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(manager_root / "scripts")
    trusted_fingerprint = Path(release_fixture["trusted_fingerprint"]).read_text(encoding="ascii").strip()
    verifier_source = (manager_root / "packaging" / "windows" / "release_verifier.py").read_text(encoding="utf-8")
    test_verifier = tmp_path / "release_verifier_test_entry.py"
    test_verifier.write_text(
        verifier_source.replace(
            release_manifest.embedded_release_public_key_fingerprint,
            trusted_fingerprint,
        ),
        encoding="utf-8",
    )

    def run_verifier() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(test_verifier),
                "--metadata-directory",
                str(metadata_directory),
                "--file-path",
                str(artifact),
                "--artifact-kind",
                "exe",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    valid = run_verifier()
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["sha256"] == executable_record["sha256"]

    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    invalid = run_verifier()
    assert invalid.returncode == 1
    assert "VERIFICATION_FAILED" in invalid.stderr


def test_signed_manifest_binds_prebuild_sources_public_artifacts_and_first_deployment(
    release_fixture: dict[str, Path | str],
) -> None:
    verify_release(release_fixture)

    manifest = json.loads(Path(release_fixture["manifest"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 5
    assert manifest["source_evidence"]["source_commit"] == "d" * 40
    assert manifest["windows_binary_evidence"] == release_fixture["windows_evidence"]
    assert manifest["dist_files"] == manifest["public_site_dist_files"]
    assert {item["path"] for item in manifest["public_site_dist_files"]} == {
        "assets/app.js",
        "index.html",
    }
    assert set(manifest["sources"]["build"]) == {"root", "manager"}
    assert manifest["sources"]["artifact_public"]["head"] == json.loads(
        Path(release_fixture["public_snapshot"]).read_text(encoding="utf-8")
    )["sources"]["artifact_public"]["head"]
    assert {item["path"] for item in manifest["public_artifacts"]} == {
        Path(release_fixture["bundle"]).name,
        Path(release_fixture["checksum"]).name,
        *release_manifest.source_evidence_public_names,
        *release_manifest.windows_binary_evidence_public_names("d" * 40),
        *release_manifest.verification_public_names,
        "codex-home-manager-local-win-x64-v1.0.0-"
        + release_manifest.sha256_file(Path(release_fixture["executable"]))[:12]
        + ".exe",
        "codex-home-manager-local-win-x64-v1.0.0-"
        + release_manifest.sha256_file(Path(release_fixture["archive"]))[:12]
        + ".zip",
    }
    assert manifest["cloudflare"] == {
        "artifact_deployment": {
            "id": release_fixture["deployment_id"],
            "project": "codex-home-manager",
            "branch": "main",
            "public_commit": manifest["sources"]["artifact_public"]["head"],
            "url": "https://artifact-deployment.codex-home-manager.pages.dev",
            "status": "success",
        }
    }
    assert "metadata_deployment" not in manifest["cloudflare"]
    assert manifest["github"]["repository"] == release_fixture["github_repository"]
    assert manifest["github"]["tag"] == release_fixture["github_tag"]
    assert manifest["github"]["release_id"] == release_fixture["github_release_id"]


def test_private_key_is_rejected_outside_backup_signing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed_directory = tmp_path / "allowed"
    monkeypatch.setattr(release_manifest, "release_signing_directory", allowed_directory)

    with pytest.raises(release_manifest.ReleaseManifestError, match="private key must be located"):
        release_manifest.generate_key_pair(
            tmp_path / "outside" / "private.pem", tmp_path / "public.pem", allowed_directory / "fingerprint"
        )


def test_capture_rejects_dirty_build_source_repository(tmp_path: Path) -> None:
    root_repository = create_repository(tmp_path / "root", "root.txt")
    manager_repository = create_repository(tmp_path / "manager", "manager.txt")
    (manager_repository / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match="manager repository is not clean"):
        release_manifest.capture_build_source_state(
            tmp_path / "snapshot.json",
            root_repository=root_repository,
            manager_repository=manager_repository,
        )


def test_manifest_creation_rejects_source_drift_after_prebuild_capture(
    release_fixture: dict[str, Path | str],
) -> None:
    root_repository = Path(release_fixture["root_repository"])
    (root_repository / "next.txt").write_text("next\n", encoding="utf-8")
    run_git(root_repository, "add", "next.txt")
    run_git(root_repository, "commit", "--quiet", "-m", "next")

    with pytest.raises(release_manifest.ReleaseManifestError, match="root HEAD changed after build source capture"):
        release_manifest.create_signed_manifest(
            build_source_snapshot_path=Path(release_fixture["build_snapshot"]),
            artifact_public_source_snapshot_path=Path(release_fixture["public_snapshot"]),
            dist_directory=Path(release_fixture["dist_directory"]),
            release_directory=Path(release_fixture["release_directory"]),
            public_site_directory=Path(release_fixture["site_directory"]),
            artifact_deployment_evidence_path=Path(release_fixture["deployment_evidence"]),
            github_release_evidence_path=Path(release_fixture["github_evidence"]),
            source_evidence_proof_path=Path(release_fixture["source_evidence_proof"]),
            private_key_path=Path(release_fixture["private_key"]),
            trusted_public_key_fingerprint_path=Path(release_fixture["trusted_fingerprint"]),
            manifest_path=Path(release_fixture["manifest"]),
            signature_path=Path(release_fixture["signature"]),
        )


def test_manifest_creation_rejects_public_head_after_artifact_capture(
    release_fixture: dict[str, Path | str],
) -> None:
    public_repository = Path(release_fixture["public_repository"])
    (public_repository / "unrelated.txt").write_text("later commit\n", encoding="utf-8")
    run_git(public_repository, "add", "unrelated.txt")
    run_git(public_repository, "commit", "--quiet", "-m", "later public commit")

    with pytest.raises(release_manifest.ReleaseManifestError, match="public HEAD changed after artifact capture"):
        release_manifest.create_signed_manifest(
            build_source_snapshot_path=Path(release_fixture["build_snapshot"]),
            artifact_public_source_snapshot_path=Path(release_fixture["public_snapshot"]),
            dist_directory=Path(release_fixture["dist_directory"]),
            release_directory=Path(release_fixture["release_directory"]),
            public_site_directory=Path(release_fixture["site_directory"]),
            artifact_deployment_evidence_path=Path(release_fixture["deployment_evidence"]),
            github_release_evidence_path=Path(release_fixture["github_evidence"]),
            source_evidence_proof_path=Path(release_fixture["source_evidence_proof"]),
            private_key_path=Path(release_fixture["private_key"]),
            trusted_public_key_fingerprint_path=Path(release_fixture["trusted_fingerprint"]),
            manifest_path=Path(release_fixture["manifest"]),
            signature_path=Path(release_fixture["signature"]),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda evidence: evidence["deployment"].update(id="forged"), "deployment ID"),
        (lambda evidence: evidence["deployment"].update(project="other-project"), "deployment project"),
        (lambda evidence: evidence["deployment"].update(public_commit="0" * 40), "deployment public commit"),
    ],
)
def test_manifest_creation_rejects_unverified_or_mismatched_artifact_deployment(
    release_fixture: dict[str, Path | str], mutation: object, message: str
) -> None:
    evidence_path = Path(release_fixture["deployment_evidence"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    mutation(evidence)
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match=message):
        release_manifest.create_signed_manifest(
            build_source_snapshot_path=Path(release_fixture["build_snapshot"]),
            artifact_public_source_snapshot_path=Path(release_fixture["public_snapshot"]),
            dist_directory=Path(release_fixture["dist_directory"]),
            release_directory=Path(release_fixture["release_directory"]),
            public_site_directory=Path(release_fixture["site_directory"]),
            artifact_deployment_evidence_path=evidence_path,
            github_release_evidence_path=Path(release_fixture["github_evidence"]),
            source_evidence_proof_path=Path(release_fixture["source_evidence_proof"]),
            private_key_path=Path(release_fixture["private_key"]),
            trusted_public_key_fingerprint_path=Path(release_fixture["trusted_fingerprint"]),
            manifest_path=Path(release_fixture["manifest"]),
            signature_path=Path(release_fixture["signature"]),
        )


@pytest.mark.parametrize("fixture_key", ["executable", "archive", "checksum", "bundle"])
def test_verifier_rejects_artifact_or_bundle_byte_drift(
    release_fixture: dict[str, Path | str], fixture_key: str
) -> None:
    Path(release_fixture[fixture_key]).write_bytes(b"tampered")
    if fixture_key in {"checksum", "bundle"}:
        public_repository = Path(release_fixture["public_repository"])
        run_git(public_repository, "add", "site")
        run_git(public_repository, "commit", "--quiet", "-m", "tamper public release")

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="hash mismatch|invalid SHA256SUMS|cannot read JSON",
    ):
        verify_release(release_fixture)


def test_verifier_rejects_dist_drift(release_fixture: dict[str, Path | str]) -> None:
    (Path(release_fixture["dist_directory"]) / "index.html").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match="dist hash mismatch"):
        verify_release(release_fixture)


def test_manifest_creation_rejects_public_site_dist_drift(release_fixture: dict[str, Path | str]) -> None:
    site_index = Path(release_fixture["site_directory"]) / "index.html"
    site_index.write_text("stale public UI\n", encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match="public site dist hash mismatch"):
        release_manifest.create_signed_manifest(
            build_source_snapshot_path=Path(release_fixture["build_snapshot"]),
            artifact_public_source_snapshot_path=Path(release_fixture["public_snapshot"]),
            dist_directory=Path(release_fixture["dist_directory"]),
            release_directory=Path(release_fixture["release_directory"]),
            public_site_directory=Path(release_fixture["site_directory"]),
            artifact_deployment_evidence_path=Path(release_fixture["deployment_evidence"]),
            github_release_evidence_path=Path(release_fixture["github_evidence"]),
            source_evidence_proof_path=Path(release_fixture["source_evidence_proof"]),
            private_key_path=Path(release_fixture["private_key"]),
            trusted_public_key_fingerprint_path=Path(release_fixture["trusted_fingerprint"]),
            manifest_path=Path(release_fixture["manifest"]),
            signature_path=Path(release_fixture["signature"]),
        )


def test_verifier_rejects_public_site_dist_drift(release_fixture: dict[str, Path | str]) -> None:
    site_asset = Path(release_fixture["site_directory"]) / "assets" / "app.js"
    site_asset.write_text("console.log('stale');\n", encoding="utf-8")
    public_repository = Path(release_fixture["public_repository"])
    run_git(public_repository, "add", "site")
    run_git(public_repository, "commit", "--quiet", "-m", "drift public UI")

    with pytest.raises(release_manifest.ReleaseManifestError, match="public site dist hash mismatch"):
        verify_release(release_fixture)


def test_public_dist_sync_plan_is_deterministic_and_preserves_public_only_files(tmp_path: Path) -> None:
    dist_directory = tmp_path / "dist"
    site_directory = tmp_path / "site"
    (dist_directory / "assets").mkdir(parents=True)
    (site_directory / "assets").mkdir(parents=True)
    (dist_directory / "index.html").write_text("new UI\n", encoding="utf-8")
    (dist_directory / "favicon.svg").write_text("<svg/>\n", encoding="utf-8")
    (dist_directory / "assets" / "index-new.js").write_text("new script\n", encoding="utf-8")
    (dist_directory / "assets" / "index-new.css").write_text("new style\n", encoding="utf-8")
    (dist_directory / "assets" / "sql-wasm-new.wasm").write_bytes(b"wasm")
    (site_directory / "index.html").write_text("old UI\n", encoding="utf-8")
    (site_directory / "assets" / "index-old.js").write_text("old script\n", encoding="utf-8")
    (site_directory / "assets" / "product-screenshot.png").write_bytes(b"screenshot")
    (site_directory / "release-manifest.json").write_text("published metadata\n", encoding="utf-8")
    (site_directory / "_headers").write_text("headers\n", encoding="utf-8")

    first_plan = release_manifest.plan_public_site_dist_sync(dist_directory, site_directory)
    second_plan = release_manifest.plan_public_site_dist_sync(dist_directory, site_directory)

    assert first_plan == second_plan
    assert [record["path"] for record in first_plan["copy_files"]] == [
        "assets/index-new.css",
        "assets/index-new.js",
        "assets/sql-wasm-new.wasm",
        "favicon.svg",
        "index.html",
    ]
    assert first_plan["stale_files"] == ["assets/index-old.js"]
    assert (site_directory / "assets" / "product-screenshot.png").read_bytes() == b"screenshot"
    assert (site_directory / "release-manifest.json").read_text(encoding="utf-8") == "published metadata\n"
    assert (site_directory / "_headers").read_text(encoding="utf-8") == "headers\n"


def test_public_dist_sync_plan_rejects_files_outside_build_allowlist(tmp_path: Path) -> None:
    dist_directory = tmp_path / "dist"
    site_directory = tmp_path / "site"
    dist_directory.mkdir()
    site_directory.mkdir()
    (dist_directory / "index.html").write_text("UI\n", encoding="utf-8")
    (dist_directory / "backend.py").write_text("not public\n", encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match="dist allowlist"):
        release_manifest.plan_public_site_dist_sync(dist_directory, site_directory)


def test_verifier_rejects_public_key_replacement(release_fixture: dict[str, Path | str]) -> None:
    replacement_private = Path(release_fixture["private_key"]).with_name("replacement-key.pem")
    replacement_public = Path(release_fixture["public_key"]).with_name("replacement-public.pem")
    replacement_trust = Path(release_fixture["trusted_fingerprint"]).with_name("replacement-fingerprint.sha256")
    release_manifest.generate_key_pair(replacement_private, replacement_public, replacement_trust)
    Path(release_fixture["public_key"]).write_bytes(replacement_public.read_bytes())

    with pytest.raises(release_manifest.ReleaseManifestError, match="public key fingerprint mismatch"):
        verify_release(release_fixture)


def test_online_verifier_rejects_missing_downloads_byte_drift_and_key_replacement(
    release_fixture: dict[str, Path | str], tmp_path: Path
) -> None:
    metadata_base_url = "https://metadata.example.invalid"
    artifact_base_url = "https://artifact-deployment.codex-home-manager.pages.dev"
    github_repository = str(release_fixture["github_repository"])
    github_tag = str(release_fixture["github_tag"])
    github_api_url = f"https://api.github.com/repos/{github_repository}/releases/tags/{github_tag}"
    github_download_base = f"https://github.com/{github_repository}/releases/download/{github_tag}"
    manifest_path = Path(release_fixture["manifest"])
    signature_path = Path(release_fixture["signature"])
    public_key_path = Path(release_fixture["public_key"])
    fingerprint_path = Path(release_fixture["trusted_fingerprint"])
    site_directory = Path(release_fixture["site_directory"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    github_asset_bytes = {
        release_manifest.release_manifest_name: manifest_path.read_bytes(),
        release_manifest.release_signature_name: signature_path.read_bytes(),
        release_manifest.release_public_key_name: public_key_path.read_bytes(),
        release_manifest.release_fingerprint_name: fingerprint_path.read_bytes(),
    }
    for artifact in manifest["github"]["artifact_assets"]:
        github_asset_bytes[artifact["name"]] = (site_directory / artifact["name"]).read_bytes()
    github_release = {
        "id": release_fixture["github_release_id"],
        "tag_name": github_tag,
        "html_url": f"https://github.com/{github_repository}/releases/tag/{github_tag}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": name,
                "size": len(content),
                "browser_download_url": f"{github_download_base}/{name}",
            }
            for name, content in sorted(github_asset_bytes.items())
        ],
    }

    responses = {
        f"{metadata_base_url}/{release_manifest.release_manifest_name}": manifest_path.read_bytes(),
        f"{metadata_base_url}/{release_manifest.release_signature_name}": signature_path.read_bytes(),
        f"{metadata_base_url}/{release_manifest.release_public_key_name}": public_key_path.read_bytes(),
        f"{metadata_base_url}/{release_manifest.release_fingerprint_name}": fingerprint_path.read_bytes(),
        github_api_url: json.dumps(github_release).encode("utf-8"),
    }
    for path in site_directory.iterdir():
        if path.is_file():
            responses[f"{artifact_base_url}/{path.name}"] = path.read_bytes()
    for record in manifest["public_site_dist_files"]:
        public_path = site_directory / record["path"]
        responses[f"{artifact_base_url}/{record['path']}"] = public_path.read_bytes()
        responses[f"{metadata_base_url}/{record['path']}"] = public_path.read_bytes()
    for name, content in github_asset_bytes.items():
        responses[f"{github_download_base}/{name}"] = content
    executable_name = next(artifact["name"] for artifact in manifest["github"]["artifact_assets"] if artifact["name"].endswith(".exe"))
    archive_name = next(artifact["name"] for artifact in manifest["github"]["artifact_assets"] if artifact["name"].endswith(".zip"))
    for alias, name in {
        "codex-home-manager-local-win-x64.exe": executable_name,
        "downloads/latest/windows-x64.exe": executable_name,
        "codex-home-manager-local-win-x64.zip": archive_name,
        "downloads/latest/windows-x64.zip": archive_name,
    }.items():
        responses[f"{metadata_base_url}/{alias}"] = (site_directory / name).read_bytes()

    def fetch(url: str) -> bytes:
        if url not in responses:
            raise release_manifest.ReleaseManifestError(f"online release URL returned 404: {url}")
        return responses[url]

    online_arguments = {
        "metadata_base_url": metadata_base_url,
        "expected_artifact_deployment_url": artifact_base_url,
        "expected_artifact_deployment_id": str(release_fixture["deployment_id"]),
        "expected_artifact_public_commit": json.loads(Path(release_fixture["public_snapshot"]).read_text(encoding="utf-8"))["sources"]["artifact_public"]["head"],
        "expected_github_repository": github_repository,
        "expected_github_tag": github_tag,
        "expected_github_release_id": int(release_fixture["github_release_id"]),
        "trusted_public_key_fingerprint_path": fingerprint_path,
        "fetch_bytes": fetch,
    }

    release_manifest.verify_online_release(**online_arguments)

    forged_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    forged_manifest["cloudflare"]["artifact_deployment"]["id"] = "forged-deployment-id"
    forged_manifest_bytes = release_manifest.canonical_json_bytes(forged_manifest)
    forged_signature = release_manifest.load_private_key(Path(release_fixture["private_key"])).sign(forged_manifest_bytes)
    responses[f"{metadata_base_url}/{release_manifest.release_manifest_name}"] = forged_manifest_bytes
    responses[f"{metadata_base_url}/{release_manifest.release_signature_name}"] = base64.b64encode(forged_signature) + b"\n"
    with pytest.raises(release_manifest.ReleaseManifestError, match="artifact deployment ID mismatch"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{metadata_base_url}/{release_manifest.release_manifest_name}"] = manifest_path.read_bytes()
    responses[f"{metadata_base_url}/{release_manifest.release_signature_name}"] = signature_path.read_bytes()

    forged_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    forged_manifest["source_evidence"]["source_commits"]["manager"] = "0" * 40
    forged_manifest_bytes = release_manifest.canonical_json_bytes(forged_manifest)
    forged_signature = release_manifest.load_private_key(Path(release_fixture["private_key"])).sign(forged_manifest_bytes)
    responses[f"{metadata_base_url}/{release_manifest.release_manifest_name}"] = forged_manifest_bytes
    responses[f"{metadata_base_url}/{release_manifest.release_signature_name}"] = base64.b64encode(forged_signature) + b"\n"
    with pytest.raises(release_manifest.ReleaseManifestError, match="source evidence commits"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{metadata_base_url}/{release_manifest.release_manifest_name}"] = manifest_path.read_bytes()
    responses[f"{metadata_base_url}/{release_manifest.release_signature_name}"] = signature_path.read_bytes()

    responses.pop(f"{metadata_base_url}/{release_manifest.release_signature_name}")
    with pytest.raises(release_manifest.ReleaseManifestError, match="404"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{metadata_base_url}/{release_manifest.release_signature_name}"] = signature_path.read_bytes()

    responses[f"{artifact_base_url}/{executable_name}"] = b"byte drift"
    with pytest.raises(release_manifest.ReleaseManifestError, match="online public artifact hash mismatch"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{artifact_base_url}/{executable_name}"] = (site_directory / executable_name).read_bytes()

    source_evidence_name = "codex-home-manager-source.cdx.json"
    responses[f"{artifact_base_url}/{source_evidence_name}"] = b"source evidence drift"
    with pytest.raises(release_manifest.ReleaseManifestError, match="online public artifact hash mismatch"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{artifact_base_url}/{source_evidence_name}"] = (site_directory / source_evidence_name).read_bytes()

    frontend_path = manifest["public_site_dist_files"][0]["path"]
    original_frontend_bytes = (site_directory / frontend_path).read_bytes()
    responses[f"{artifact_base_url}/{frontend_path}"] = b"artifact deployment frontend drift"
    with pytest.raises(release_manifest.ReleaseManifestError, match="artifact deployment public site dist hash mismatch"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{artifact_base_url}/{frontend_path}"] = original_frontend_bytes

    responses[f"{metadata_base_url}/{frontend_path}"] = b"metadata deployment frontend drift"
    with pytest.raises(release_manifest.ReleaseManifestError, match="metadata deployment public site dist hash mismatch"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{metadata_base_url}/{frontend_path}"] = original_frontend_bytes

    github_release["assets"].append(
        {
            "name": "fake-debug.zip",
            "size": 4,
            "browser_download_url": f"{github_download_base}/fake-debug.zip",
        }
    )
    responses[github_api_url] = json.dumps(github_release).encode("utf-8")
    with pytest.raises(release_manifest.ReleaseManifestError, match="GitHub release asset set mismatch"):
        release_manifest.verify_online_release(**online_arguments)
    github_release["assets"].pop()
    responses[github_api_url] = json.dumps(github_release).encode("utf-8")

    responses[f"{github_download_base}/{executable_name}"] = b"github byte drift"
    with pytest.raises(release_manifest.ReleaseManifestError, match="Cloudflare and GitHub artifact drift"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{github_download_base}/{executable_name}"] = (site_directory / executable_name).read_bytes()

    responses[f"{github_download_base}/{source_evidence_name}"] = b"github source evidence drift"
    with pytest.raises(release_manifest.ReleaseManifestError, match="Cloudflare and GitHub artifact drift"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{github_download_base}/{source_evidence_name}"] = (site_directory / source_evidence_name).read_bytes()

    responses[f"{metadata_base_url}/codex-home-manager-local-win-x64.exe"] = b"retired executable"
    with pytest.raises(release_manifest.ReleaseManifestError, match="stable release alias still serves an older artifact"):
        release_manifest.verify_online_release(**online_arguments)
    responses[f"{metadata_base_url}/codex-home-manager-local-win-x64.exe"] = (site_directory / executable_name).read_bytes()

    replacement_private = Path(release_fixture["private_key"]).with_name("online-replacement-key.pem")
    replacement_public = Path(release_fixture["public_key"]).with_name("online-replacement-public.pem")
    replacement_trust = Path(release_fixture["trusted_fingerprint"]).with_name("online-replacement-fingerprint.sha256")
    release_manifest.generate_key_pair(replacement_private, replacement_public, replacement_trust)
    responses[f"{metadata_base_url}/{release_manifest.release_public_key_name}"] = replacement_public.read_bytes()
    with pytest.raises(release_manifest.ReleaseManifestError, match="public key fingerprint mismatch"):
        release_manifest.verify_online_release(**online_arguments)


def test_packaging_and_finalize_scripts_enforce_two_deployment_proof_chain() -> None:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "package-local-connector.ps1"
    script = script_path.read_text(encoding="utf-8")
    finalize_path = Path(__file__).resolve().parents[1] / "scripts" / "finalize-release-manifest.ps1"
    finalize = finalize_path.read_text(encoding="utf-8")

    assert "capture-build-source" in script
    assert "validate-build-source" in script
    assert "release-signing-public-key.sha256" in script
    assert "PrepareMetadata" not in script
    assert "PrepareMetadata" in finalize
    assert "VerifyPublication" in finalize
    assert "Invoke-RestMethod" in finalize
    assert "/pages/projects/$CloudflareProject/deployments/$DeploymentId" in finalize
    assert "artifact-deployment" in finalize
    assert "--artifact-deployment-id" in finalize
    assert "--artifact-public-commit" in finalize
    assert "metadata_deployment" not in finalize
    assert "online-verify" in finalize
    assert "GITHUB_TOKEN" in finalize
    assert "/repos/$GithubRepository/releases/$GithubReleaseId" in finalize
    assert "github-release.json" in finalize
    assert "--github-release-evidence" in finalize
    assert "gh release upload" in finalize
    assert "CODEX_HOME_MANAGER_PUBLIC_RELEASE_MODE" in finalize
    assert "release-manifest.json.sig" in finalize
    assert "--github-repository" in finalize
    assert "--github-tag" in finalize
    assert "--github-release-id" in finalize
