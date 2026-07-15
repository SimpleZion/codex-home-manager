from __future__ import annotations

import zipfile
import struct
from pathlib import Path

import pytest

from scripts import release_manifest


def test_normalize_pyinstaller_executable_normalizes_coff_and_debug_timestamps(tmp_path: Path) -> None:
    executable_path = tmp_path / "fixture.exe"
    content = bytearray(0x800)
    content[:2] = b"MZ"
    pe_offset = 0x80
    struct.pack_into("<I", content, 0x3C, pe_offset)
    content[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<H", content, pe_offset + 6, 1)
    struct.pack_into("<I", content, pe_offset + 8, 1_700_000_123)
    struct.pack_into("<H", content, pe_offset + 20, 0xF0)
    optional_header_offset = pe_offset + 24
    struct.pack_into("<H", content, optional_header_offset, 0x20B)
    struct.pack_into("<I", content, optional_header_offset + 64, 1234)
    struct.pack_into("<I", content, optional_header_offset + 108, 16)
    data_directory_offset = optional_header_offset + 112
    debug_rva = 0x1100
    struct.pack_into("<II", content, data_directory_offset + (6 * 8), debug_rva, 28)
    section_offset = optional_header_offset + 0xF0
    content[section_offset : section_offset + 8] = b".rdata\0\0"
    struct.pack_into("<IIII", content, section_offset + 8, 0x400, 0x1000, 0x400, 0x200)
    debug_file_offset = 0x300
    struct.pack_into("<I", content, debug_file_offset + 4, 1_700_000_456)
    executable_path.write_bytes(content)

    release_manifest.normalize_pyinstaller_executable(executable_path, source_date_epoch=1_700_000_000)

    normalized = executable_path.read_bytes()
    assert struct.unpack_from("<I", normalized, pe_offset + 8)[0] == 1_700_000_000
    assert struct.unpack_from("<I", normalized, optional_header_offset + 64)[0] == 0
    assert struct.unpack_from("<I", normalized, debug_file_offset + 4)[0] == 1_700_000_000


def test_deterministic_zip_normalizes_order_time_and_permissions(tmp_path: Path) -> None:
    source = tmp_path / "payload"
    (source / "nested").mkdir(parents=True)
    (source / "z.txt").write_text("z\n", encoding="utf-8")
    (source / "nested" / "a.cmd").write_text("@echo off\n", encoding="ascii")
    first_archive = tmp_path / "first.zip"
    second_archive = tmp_path / "second.zip"

    release_manifest.create_deterministic_zip(source, first_archive, source_date_epoch=1_700_000_000)
    release_manifest.create_deterministic_zip(source, second_archive, source_date_epoch=1_700_000_000)

    assert first_archive.read_bytes() == second_archive.read_bytes()
    with zipfile.ZipFile(first_archive) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert len({entry.date_time for entry in archive.infolist()}) == 1
        assert all(entry.create_system == 3 for entry in archive.infolist())
        permissions = {entry.filename: entry.external_attr >> 16 for entry in archive.infolist()}
        assert permissions["nested/a.cmd"] == 0o755
        assert permissions["z.txt"] == 0o644


def test_reproducible_build_comparison_rejects_dist_exe_and_zip_drift(tmp_path: Path) -> None:
    first_dist = tmp_path / "first-dist"
    second_dist = tmp_path / "second-dist"
    first_dist.mkdir()
    second_dist.mkdir()
    (first_dist / "index.html").write_text("same\n", encoding="utf-8")
    (second_dist / "index.html").write_text("same\n", encoding="utf-8")
    first_executable = tmp_path / "first.exe"
    second_executable = tmp_path / "second.exe"
    first_archive = tmp_path / "first.zip"
    second_archive = tmp_path / "second.zip"
    for path, content in (
        (first_executable, b"exe"),
        (second_executable, b"exe"),
        (first_archive, b"zip"),
        (second_archive, b"zip"),
    ):
        path.write_bytes(content)

    release_manifest.compare_reproducible_builds(
        first_dist,
        second_dist,
        first_executable,
        second_executable,
        first_archive,
        second_archive,
    )

    second_executable.write_bytes(b"drift")
    with pytest.raises(release_manifest.ReleaseManifestError, match="EXE reproducibility mismatch"):
        release_manifest.compare_reproducible_builds(
            first_dist,
            second_dist,
            first_executable,
            second_executable,
            first_archive,
            second_archive,
        )


def test_github_release_rejects_extra_or_fake_assets() -> None:
    expected_names = {
        "connector.exe",
        "connector.zip",
        "release-manifest.json",
        "release-manifest.json.sig",
        "release-signing-public-key.pem",
        "release-signing-public-key.sha256",
    }
    release = {
        "id": 42,
        "tag_name": "v1.0.0",
        "html_url": "https://github.com/example/project/releases/tag/v1.0.0",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": name,
                "size": 10,
                "browser_download_url": f"https://github.com/example/project/releases/download/v1.0.0/{name}",
            }
            for name in sorted(expected_names)
        ],
    }

    release_manifest.validate_github_release_payload(
        release,
        expected_repository="example/project",
        expected_tag="v1.0.0",
        expected_release_id=42,
        expected_asset_names=expected_names,
        require_draft=False,
    )

    release["assets"].append(
        {
            "name": "fake-debug-symbols.zip",
            "size": 10,
            "browser_download_url": "https://github.com/example/project/releases/download/v1.0.0/fake-debug-symbols.zip",
        }
    )
    with pytest.raises(release_manifest.ReleaseManifestError, match="GitHub release asset set mismatch"):
        release_manifest.validate_github_release_payload(
            release,
            expected_repository="example/project",
            expected_tag="v1.0.0",
            expected_release_id=42,
            expected_asset_names=expected_names,
            require_draft=False,
        )
