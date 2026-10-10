"""Publish the verified desktop installer without borrowing connector CI provenance."""
from __future__ import annotations

import argparse
import base64
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_manifest import (
    assert_public_key_matches_trust, canonical_json_bytes, embedded_release_public_key_fingerprint,
    export_existing_public_key, load_private_key, load_trusted_public_key_fingerprint,
    managed_public_site_dist_paths, release_signing_directory, render_powershell_artifact_verifier,
    validate_github_release_payload,
    sha256_file, write_bytes,
)


def record(path):
    return {"path": path.name, "sha256": sha256_file(path), "size": path.stat().st_size}


def verify_frozen_frontend(site, expected_manifest_hash):
    manifest_path = site / "frontend-release-manifest.json"
    if sha256_file(manifest_path) != expected_manifest_hash:
        raise RuntimeError("Public frontend differs from the frozen installer frontend")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = managed_public_site_dist_paths(site)
    files = [name for name in paths if name != manifest_path.name]
    if manifest.get("schemaVersion") != 2 or manifest.get("files") != files:
        raise RuntimeError("Frozen frontend file set differs from the public resources")
    digest = hashlib.sha256()
    records = []
    for name in files:
        path = site / name
        before = path.stat()
        digest.update(name.encode("utf-8") + b"\0")
        file_digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                file_digest.update(chunk)
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise RuntimeError("Frozen frontend changed while hashing")
        digest.update(b"\0")
        records.append({"path": name, "size": after.st_size, "sha256": file_digest.hexdigest()})
    if digest.hexdigest() != manifest.get("artifactDigest"):
        raise RuntimeError("Frozen frontend resource bytes differ from the build receipt")
    return sorted([*records, record(manifest_path)], key=lambda item: item["path"])


def verify_publication_identity(source, site, prepared, release_id, source_commit, records):
    def git(*arguments):
        return subprocess.run(["git", "-C", str(source), *arguments], check=True, capture_output=True, timeout=30).stdout

    if git("rev-parse", "refs/remotes/origin/source").decode().strip() != source_commit:
        raise RuntimeError("Public source branch does not resolve to the requested commit")
    expected = {"SOURCE_COMMITS.json": sha256_file(source / "SOURCE_COMMITS.json")}
    for repository in prepared["sourceManifest"]["sources"].values():
        expected.update({item["path"]: item["sha256"] for item in repository["files"]})
    tree = git("ls-tree", "-r", "-z", source_commit)
    actual = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        header, filename = entry.split(b"\t", 1)
        mode, kind, blob = header.split()
        name = filename.decode("utf-8")
        if kind != b"blob" or mode not in {b"100644", b"100755"} or name not in expected:
            raise RuntimeError("Public source commit contains unlisted or non-file entries")
        actual[name] = hashlib.sha256(git("cat-file", "blob", blob.decode())).hexdigest()
    if actual != expected:
        raise RuntimeError("Public source commit differs from the validated source snapshot")
    def github(path):
        return json.loads(subprocess.run(["gh", "api", f"repos/SimpleZion/codex-home-manager/{path}"],
            check=True, capture_output=True, timeout=30).stdout)

    remote_ref = github("git/ref/heads/source")
    remote_commit = github(f"git/commits/{source_commit}")
    tree_sha = git("rev-parse", f"{source_commit}^{{tree}}").decode().strip()
    if remote_ref.get("object", {}).get("sha") != source_commit or remote_commit.get("tree", {}).get("sha") != tree_sha:
        raise RuntimeError("Actual GitHub source commit does not match the validated source tree")
    release = github(f"releases/{release_id}")
    validate_github_release_payload(release, expected_repository="SimpleZion/codex-home-manager",
        expected_tag="v1.0.34", expected_release_id=release_id,
        expected_asset_names={item["path"] for item in records}, require_draft=True)
    assets = {item["name"]: item for item in release["assets"]}
    for item in records:
        asset = assets[item["path"]]
        if asset.get("size") != item["size"] or asset.get("state") != "uploaded" or asset.get("digest") != f"sha256:{item['sha256']}":
            raise RuntimeError("GitHub draft asset bytes differ from the verified local artifact")
    return {"release_payload_sha256": hashlib.sha256(canonical_json_bytes(release)).hexdigest(),
            "source_tree_verified": True, "source_tree_sha": tree_sha, "asset_digests_verified": True}


def prepare(release, source, site, quality):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from export_codex_home_manager_source import verify_source_monorepo
    verify_source_monorepo(source, allow_generated=True)
    receipt = json.loads((release / "codex-cockpit-release-receipt.json").read_text(encoding="utf-8"))
    installer = release / "Codex-Cockpit-Setup-x64.exe"
    if receipt.get("schemaVersion") != 2 or receipt.get("version") != "1.0.34" or receipt.get("productName") != "Codex Cockpit":
        raise RuntimeError("Only the verified Codex Cockpit 1.0.34 release is supported")
    if sha256_file(installer) != receipt["installerSha256"]:
        raise RuntimeError("Installer differs from the frozen build receipt")
    dist = verify_frozen_frontend(site, receipt["frontendManifestSha256"])
    if quality.get("status") != "passed" or quality.get("sourceManifestSha256") != sha256_file(source / "SOURCE_COMMITS.json"):
        raise RuntimeError("Passing source validation must bind this exact source manifest")
    fingerprint = load_trusted_public_key_fingerprint(release_signing_directory / "release-signing-public-key.sha256")
    if fingerprint != embedded_release_public_key_fingerprint:
        raise RuntimeError("Release key differs from the independently pinned key")
    private = load_private_key(release_signing_directory / "release-signing-key.pem")
    assert_public_key_matches_trust(private.public_key(), fingerprint)
    name = f"codex-home-manager-local-win-x64-v1.0.34-{receipt['installerSha256'][:12]}.exe"
    shutil.copyfile(installer, site / name)
    archive = release / "Codex-Cockpit-Setup-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.write(installer, "Codex-Cockpit-Setup-x64.exe")
        output.writestr(zipfile.ZipInfo("README.txt", date_time=(1980, 1, 1, 0, 0, 0)), "Codex Cockpit 1.0.34\nRun Codex-Cockpit-Setup-x64.exe to install. This is not a portable application.\n")
    zip_name = f"codex-home-manager-local-win-x64-v1.0.34-{sha256_file(archive)[:12]}.zip"
    shutil.copyfile(archive, site / zip_name)
    source_manifest = json.loads((source / "SOURCE_COMMITS.json").read_text(encoding="utf-8"))
    source_names = {"SOURCE_COMMITS.json"}
    for repository in source_manifest["sources"].values():
        source_names.update(item["path"] for item in repository["files"])
    with zipfile.ZipFile(site / "codex-home-manager-source.zip", "w", compression=zipfile.ZIP_DEFLATED) as output:
        for relative in sorted(source_names):
            output.write(source / relative, relative)
    artifacts = [{"name": item["path"], "kind": suffix, "sha256": item["sha256"], "size": item["size"]}
                 for suffix, item in (("exe", record(site / name)), ("zip", record(site / zip_name)))]
    artifacts[0]["authenticode"] = {"status": "not-signed", "windowsStatus": "NotSigned", "trust": "none",
        "signerThumbprint": None, "signerSubject": None, "selfSigned": False, "chainTrusted": False,
        "detachedSignatureRequired": True}
    # The publisher signature is not an Authenticode certificate or a GitHub build attestation.
    bundle = {"schemaVersion": 2, "version": "1.0.34", "flavor": "tauri-desktop", "artifacts": artifacts}
    write_bytes(site / "connector-release.json", canonical_json_bytes(bundle))
    export_existing_public_key(release_signing_directory / "release-signing-key.pem",
        site / "release-signing-public-key.pem", release_signing_directory / "release-signing-public-key.sha256")
    write_bytes(site / "release-signing-public-key.sha256", (fingerprint + "\n").encode())
    write_bytes(site / "verify-codex-home-manager.ps1", render_powershell_artifact_verifier(
        default_artifact_name=name, trusted_fingerprint=fingerprint).encode("utf-8"))
    return {"receipt": receipt, "bundle": bundle, "sourceManifest": source_manifest,
            "quality": quality, "dist": dist, "source": source}


def sign(prepared, site, release_id, source_commit):
    if release_id < 1 or len(source_commit) != 40 or any(character not in "0123456789abcdef" for character in source_commit):
        raise RuntimeError("Real GitHub release ID and source commit are required")
    records = [record(site / item["name"]) for item in prepared["bundle"]["artifacts"]]
    source_record = record(site / "codex-home-manager-source.zip")
    records.extend([source_record, record(site / "verify-codex-home-manager.ps1")])
    dist = verify_frozen_frontend(site, prepared["receipt"]["frontendManifestSha256"])
    identity = verify_publication_identity(prepared["source"], site, prepared, release_id, source_commit, records)
    manifest = {"schema_version": 6, "product": "Codex Cockpit", "version": "1.0.34",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "public_key_fingerprint": embedded_release_public_key_fingerprint,
        "public_artifacts": records, "dist_files": dist, "public_site_dist_files": dist,
        "bundle_sha256": sha256_file(site / "connector-release.json"),
        "desktop_build": {"method": "publisher-local-tauri-nsis", "github_built": False,
            "portable": False, "receipt": prepared["receipt"], "source_snapshot_after_build": True},
        "source_validation": {"method": "isolated-local-full-gate", "quality": prepared["quality"],
            "manifest_sha256": prepared["quality"]["sourceManifestSha256"], "archive": source_record,
            "source_commit": source_commit, "source_ref": "refs/heads/source"},
        "github": {"repository": "SimpleZion/codex-home-manager", "tag": "v1.0.34", "release_id": release_id,
            "html_url": "https://github.com/SimpleZion/codex-home-manager/releases/tag/v1.0.34",
            "draft_verified_before_signing": True,
            "verification": identity,
            "metadata_assets": ["release-manifest.json", "release-manifest.json.sig", "release-signing-public-key.pem", "release-signing-public-key.sha256"],
            "artifact_assets": [{"name": item["path"], "sha256": item["sha256"], "size": item["size"]} for item in records[:2]]}}
    content = canonical_json_bytes(manifest)
    private = load_private_key(release_signing_directory / "release-signing-key.pem")
    assert_public_key_matches_trust(private.public_key(), embedded_release_public_key_fingerprint)
    write_bytes(site / "release-manifest.json", content)
    write_bytes(site / "release-manifest.json.sig", base64.b64encode(private.sign(content)) + b"\n")
    checksums = "".join(f"{item['sha256']}  {item['path']}\n" for item in sorted(records, key=lambda item: item["path"]))
    write_bytes(site / "SHA256SUMS.txt", checksums.encode())
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--site", required=True, type=Path)
    parser.add_argument("--quality", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--release-id", type=int)
    parser.add_argument("--source-commit")
    arguments = parser.parse_args()
    prepared = prepare(arguments.release, arguments.source, arguments.site,
                       json.loads(arguments.quality.read_text(encoding="utf-8")))
    if arguments.prepare_only:
        print(json.dumps({"version": "1.0.34", "artifacts": prepared["bundle"]["artifacts"]}))
        return 0
    if arguments.release_id is None or arguments.source_commit is None:
        parser.error("Signing requires --release-id and --source-commit")
    manifest = sign(prepared, arguments.site, arguments.release_id, arguments.source_commit)
    print(json.dumps({"version": manifest["version"], "artifacts": manifest["public_artifacts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
