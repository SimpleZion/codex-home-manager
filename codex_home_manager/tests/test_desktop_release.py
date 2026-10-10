from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts import desktop_release


def frontend_fixture(root: Path) -> str:
    (root / "assets").mkdir()
    (root / "index.html").write_bytes(b"fixture")
    manifest = {"schemaVersion": 2, "files": ["index.html"],
                "artifactDigest": hashlib.sha256(b"index.html\0fixture\0").hexdigest()}
    path = root / "frontend-release-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return desktop_release.sha256_file(path)


def test_desktop_frozen_frontend_binds_real_bytes(tmp_path):
    fingerprint = frontend_fixture(tmp_path)
    assert len(desktop_release.verify_frozen_frontend(tmp_path, fingerprint)) == 2
    (tmp_path / "index.html").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="resource bytes"):
        desktop_release.verify_frozen_frontend(tmp_path, fingerprint)


def test_desktop_frozen_frontend_rejects_added_resources(tmp_path):
    fingerprint = frontend_fixture(tmp_path)
    (tmp_path / "assets/extra.js").write_bytes(b"extra")
    with pytest.raises(RuntimeError, match="file set"):
        desktop_release.verify_frozen_frontend(tmp_path, fingerprint)


@pytest.mark.parametrize("mutation", [None, "asset", "source", "not-draft"])
def test_desktop_identity_requires_actual_github_and_source_tree(tmp_path, monkeypatch, mutation):
    real_run = subprocess.run

    def git(*arguments):
        return real_run(["git", "-C", str(tmp_path), *arguments], check=True, capture_output=True).stdout.decode().strip()

    git("init", "--quiet")
    source_manifest = {"schemaVersion": 2, "sources": {"rootRepository": {"files": []},
        "managerRepository": {"files": [{"path": "app.py", "sha256": hashlib.sha256(b"pass\n").hexdigest(), "bytes": 5}]}}}
    (tmp_path / "SOURCE_COMMITS.json").write_text(json.dumps(source_manifest), encoding="utf-8")
    (tmp_path / "app.py").write_bytes(b"pass\n")
    git("-c", "core.autocrlf=false", "add", "SOURCE_COMMITS.json", "app.py")
    tree = git("write-tree")
    commit = git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit-tree", tree, "-m", "Fixture")
    git("update-ref", "refs/remotes/origin/source", commit)
    records = [{"path": "fixture.zip", "size": 1, "sha256": "a" * 64}]
    release = {"id": 42, "tag_name": "v1.0.34", "draft": mutation != "not-draft", "prerelease": False,
        "html_url": "https://github.com/SimpleZion/codex-home-manager/releases/tag/v1.0.34", "assets": [
            {"name": "fixture.zip", "state": "uploaded", "size": 1,
             "digest": "sha256:" + ("b" if mutation == "asset" else "a") * 64,
             "browser_download_url": "https://github.com/SimpleZion/codex-home-manager/releases/download/v1.0.34/fixture.zip"}]}

    def run(command, **keywords):
        if command[0] != "gh":
            return real_run(command, **keywords)
        endpoint = command[-1]
        payload = release if "/releases/" in endpoint else (
            {"tree": {"sha": "f" * 40 if mutation == "source" else tree}} if "/commits/" in endpoint else {"object": {"sha": commit}})
        return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(desktop_release.subprocess, "run", run)
    if mutation:
        with pytest.raises(RuntimeError):
            desktop_release.verify_publication_identity(tmp_path, tmp_path, {"sourceManifest": source_manifest}, 42, commit, records)
    else:
        result = desktop_release.verify_publication_identity(tmp_path, tmp_path, {"sourceManifest": source_manifest}, 42, commit, records)
        assert result["source_tree_verified"] and result["asset_digests_verified"]
