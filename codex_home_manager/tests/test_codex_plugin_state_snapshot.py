from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(scripts_path))

import codex_plugin_state_snapshot
from codex_plugin_state_snapshot import (
    archive_stale_restore_artifacts,
    restore_plugin_state,
    snapshot_plugin_state,
    verify_repair_sources,
)


def create_junction(path: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)


def test_create_junction_tolerates_localized_non_utf8_output(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*arguments, **keyword_arguments):
        captured["args"] = arguments
        captured.update(keyword_arguments)
        return subprocess.CompletedProcess(arguments[0], 0, stdout="", stderr="")

    monkeypatch.setattr(codex_plugin_state_snapshot.subprocess, "run", fake_run)
    junction_parent = (
        tmp_path
        / ("junction-segment-a-" + "a" * 70)
        / ("junction-segment-b-" + "b" * 70)
        / ("junction-segment-c-" + "c" * 70)
    )
    codex_plugin_state_snapshot.filesystem_path(junction_parent).mkdir(parents=True)
    junction_path = junction_parent / "latest"
    target_path = (tmp_path / "target").resolve()
    codex_plugin_state_snapshot.create_junction(junction_path, str(target_path))

    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    command = captured["args"][0]
    assert command[-2] == str(codex_plugin_state_snapshot.filesystem_path(junction_path))
    assert command[-1] == str(codex_plugin_state_snapshot.filesystem_path(target_path))


def test_archive_stale_restore_artifacts_moves_only_transaction_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(codex_plugin_state_snapshot, "assert_codex_offline", lambda: None)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    stale_directory = codex_home / ".plugins.c650ff0580a94204a720126f3042c946.restoring"
    stale_directory.mkdir()
    (stale_directory / "marker.txt").write_text("abandoned", encoding="utf-8")
    unrelated_directory = codex_home / ".plugins.not-a-transaction.restoring"
    unrelated_directory.mkdir()
    archive_root = tmp_path / "backup" / "stale_restore_artifacts"

    result = archive_stale_restore_artifacts(
        codex_home,
        archive_root,
        backup_policy_root=tmp_path / "backup",
    )

    assert result["status"] == "complete"
    assert result["archived_count"] == 1
    assert not stale_directory.exists()
    archived_directory = archive_root / stale_directory.name
    assert (archived_directory / "marker.txt").read_text(encoding="utf-8") == "abandoned"
    assert unrelated_directory.is_dir()
    archive_report = json.loads(
        (archive_root / "stale_restore_archive.json").read_text(encoding="utf-8")
    )
    assert archive_report == result


def test_snapshot_restore_preserves_files_junctions_and_original_absence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_plugin_state_snapshot, "assert_codex_offline", lambda: None)
    codex_home = tmp_path / "codex_home"
    backup_root = tmp_path / "snapshot"
    plugin_root = codex_home / "plugins"
    version_one = plugin_root / "cache" / "browser" / "1.0"
    version_two = plugin_root / "cache" / "browser" / "2.0"
    version_one.mkdir(parents=True)
    version_two.mkdir(parents=True)
    (version_one / "runtime.mjs").write_text("version-one", encoding="utf-8")
    (version_two / "runtime.mjs").write_text("version-two", encoding="utf-8")
    latest = plugin_root / "cache" / "browser" / "latest"
    create_junction(latest, version_one)
    (codex_home / "config.toml").write_text('model = "before"\n', encoding="utf-8")
    repair_source = tmp_path / "appx-bundled-source"
    repair_source.mkdir()
    (repair_source / "runtime.bin").write_bytes(b"appx-runtime")

    manifest_path = snapshot_plugin_state(
        codex_home=codex_home,
        snapshot_root=backup_root,
        relative_paths=["config.toml", "managed_config.toml", "plugins"],
        repair_source_paths=[repair_source],
    )
    snapshot_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    disk_preflight = snapshot_manifest["disk_preflight"]
    assert disk_preflight["required_bytes"] == (
        disk_preflight["source_bytes"] * 6
        + disk_preflight["repair_source_bytes"] * 4
        + 1536 * 1024**2
    )
    assert disk_preflight["repair_source_bytes"] == len(b"appx-runtime")
    assert disk_preflight["restore_temporary_bytes"] == disk_preflight["source_bytes"]
    assert disk_preflight["repair_archive_bytes"] == disk_preflight["source_bytes"]
    assert snapshot_manifest["repair_sources"] == [codex_plugin_state_snapshot.repair_source_record(repair_source)]
    assert verify_repair_sources(manifest_path) == [str(repair_source.resolve())]

    (repair_source / "runtime.bin").write_bytes(b"tampered-appx-runtime")
    with pytest.raises(RuntimeError, match="repair source tree changed"):
        verify_repair_sources(manifest_path)

    (codex_home / "config.toml").write_text('model = "after"\n', encoding="utf-8")
    (codex_home / "managed_config.toml").write_text("created-after-snapshot", encoding="utf-8")
    os.replace(latest, plugin_root / "latest-before-restore")
    create_junction(latest, version_two)
    (version_one / "runtime.mjs").write_text("mutated", encoding="utf-8")

    result = restore_plugin_state(manifest_path, tmp_path)

    assert result["errors"] == []
    assert (codex_home / "config.toml").read_text(encoding="utf-8") == 'model = "before"\n'
    assert not (codex_home / "managed_config.toml").exists()
    assert latest.is_junction()
    assert os.path.samefile(latest, version_one)
    assert (version_one / "runtime.mjs").read_text(encoding="utf-8") == "version-one"
    assert list((backup_root / "failed_state").rglob("managed_config.toml"))
    assert list((backup_root / "failed_state").rglob("runtime.mjs"))


def test_restore_can_preserve_runtime_state_while_rolling_back_plugin_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_plugin_state_snapshot, "assert_codex_offline", lambda: None)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    state_path = codex_home / "state_5.sqlite"
    config_path.write_text("before-config", encoding="utf-8")
    state_path.write_text("before-state", encoding="utf-8")
    manifest_path = snapshot_plugin_state(
        codex_home,
        tmp_path / "snapshot",
        ["config.toml", "state_5.sqlite"],
    )
    config_path.write_text("after-config", encoding="utf-8")
    state_path.write_text("post-restart-state", encoding="utf-8")

    result = restore_plugin_state(
        manifest_path,
        tmp_path,
        skip_relative_paths={"state_5.sqlite"},
    )

    assert result["status"] == "complete"
    assert result["skipped"] == ["state_5.sqlite"]
    assert config_path.read_text(encoding="utf-8") == "before-config"
    assert state_path.read_text(encoding="utf-8") == "post-restart-state"


def test_restore_is_transactional_when_a_later_root_swap_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_plugin_state_snapshot, "assert_codex_offline", lambda: None)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    first = codex_home / "first.txt"
    second = codex_home / "second.txt"
    first.write_text("before-first", encoding="utf-8")
    second.write_text("before-second", encoding="utf-8")
    manifest_path = snapshot_plugin_state(
        codex_home=codex_home,
        snapshot_root=tmp_path / "snapshot",
        relative_paths=["first.txt", "second.txt"],
    )
    first.write_text("after-first", encoding="utf-8")
    second.write_text("after-second", encoding="utf-8")

    real_replace_path = codex_plugin_state_snapshot.replace_path
    failed = False

    def fail_second_install(source, destination) -> None:
        nonlocal failed
        if not failed and Path(destination) == second and ".restoring" in Path(source).name:
            failed = True
            raise OSError("simulated second root install failure")
        real_replace_path(source, destination)

    monkeypatch.setattr(codex_plugin_state_snapshot, "replace_path", fail_second_install)
    result = restore_plugin_state(manifest_path, tmp_path)

    assert result["status"] == "failed"
    assert result["errors"]
    assert first.read_text(encoding="utf-8") == "after-first"
    assert second.read_text(encoding="utf-8") == "after-second"
    assert not list(codex_home.glob(".*.restoring"))
    assert not (tmp_path / "snapshot" / "failed_state" / "first.txt").exists()
    assert not (tmp_path / "snapshot" / "failed_state" / "second.txt").exists()


def test_restore_result_is_written_by_atomic_replace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_plugin_state_snapshot, "assert_codex_offline", lambda: None)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "before"\n', encoding="utf-8")
    snapshot_root = tmp_path / "snapshot"
    manifest_path = snapshot_plugin_state(codex_home, snapshot_root, ["config.toml"])
    config_path.write_text('model = "after"\n', encoding="utf-8")
    result_path = snapshot_root / "plugin_state_restore.json"
    result_path.write_text('{"status":"previous"}', encoding="utf-8")

    real_replace_path = codex_plugin_state_snapshot.replace_path
    observed_replacements: list[tuple[Path, Path]] = []

    def observe_result_replace(source: Path, destination: Path) -> None:
        if destination == result_path:
            assert source == result_path.with_suffix(".json.writing")
            assert json.loads(result_path.read_text(encoding="utf-8")) == {"status": "previous"}
            assert json.loads(codex_plugin_state_snapshot.read_text(source))["status"] == "complete"
            observed_replacements.append((source, destination))
        real_replace_path(source, destination)

    monkeypatch.setattr(codex_plugin_state_snapshot, "replace_path", observe_result_replace)
    result = restore_plugin_state(manifest_path, tmp_path)

    assert observed_replacements == [(result_path.with_suffix(".json.writing"), result_path)]
    assert json.loads(result_path.read_text(encoding="utf-8")) == result
    assert not result_path.with_suffix(".json.writing").exists()


def test_snapshot_refuses_insufficient_disk_space_before_creating_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_plugin_state_snapshot, "assert_codex_offline", lambda: None)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("test", encoding="utf-8")
    snapshot_root = tmp_path / "snapshot"
    disk_usage = type("usage", (), {"total": 1, "used": 1, "free": 0})
    monkeypatch.setattr(codex_plugin_state_snapshot.shutil, "disk_usage", lambda _path: disk_usage)

    with pytest.raises(RuntimeError, match="insufficient snapshot space"):
        snapshot_plugin_state(codex_home, snapshot_root, ["config.toml"])

    assert not snapshot_root.exists()


def test_snapshot_rejects_symbolic_link_that_escapes_codex_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_plugin_state_snapshot, "assert_codex_offline", lambda: None)
    codex_home = tmp_path / "codex_home"
    plugin_root = codex_home / "plugins"
    plugin_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    symbolic_link = plugin_root / "outside-link"
    try:
        symbolic_link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are not available in this Windows test environment: {error}")

    with pytest.raises(RuntimeError, match="symbolic links are not allowed"):
        snapshot_plugin_state(
            codex_home=codex_home,
            snapshot_root=tmp_path / "snapshot",
            relative_paths=["plugins"],
        )

    assert not (tmp_path / "snapshot").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path behavior")
def test_snapshot_and_restore_support_extended_length_windows_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_plugin_state_snapshot, "assert_codex_offline", lambda: None)
    codex_home = tmp_path / "codex_home"
    nested_relative = (
        Path("plugins")
        / "cache"
        / "computer-use"
        / "26.707.31428"
        / "node_modules"
        / ("nested-package-" + "x" * 48)
        / "payload.bin"
    )
    source_file = codex_home / nested_relative
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"before")
    snapshot_root = tmp_path / ("snapshot-" + "s" * 110)
    expected_snapshot_file = snapshot_root / "data" / nested_relative
    assert len(str(expected_snapshot_file)) > 260

    manifest_path = snapshot_plugin_state(
        codex_home=codex_home,
        snapshot_root=snapshot_root,
        relative_paths=["plugins"],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["roots"][0]["source_signature"] == manifest["roots"][0]["snapshot_signature"]
    assert codex_plugin_state_snapshot.filesystem_path(expected_snapshot_file).read_bytes() == b"before"

    source_file.write_bytes(b"after")
    result = restore_plugin_state(manifest_path, tmp_path)

    assert result["status"] == "complete"
    assert source_file.read_bytes() == b"before"
    assert codex_plugin_state_snapshot.path_signature(codex_home / "plugins") == manifest["roots"][0][
        "source_signature"
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path behavior")
def test_snapshot_and_restore_support_transaction_root_over_windows_max_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_plugin_state_snapshot, "assert_codex_offline", lambda: None)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "before"\n', encoding="utf-8")
    transaction_parent = (
        tmp_path
        / ("transaction-segment-a-" + "a" * 70)
        / ("transaction-segment-b-" + "b" * 70)
        / ("transaction-segment-c-" + "c" * 70)
    )
    codex_plugin_state_snapshot.filesystem_path(transaction_parent).mkdir(parents=True)
    snapshot_root = transaction_parent / "plugin_state_snapshot"
    assert len(str(snapshot_root)) > 260

    manifest_path = snapshot_plugin_state(codex_home, snapshot_root, ["config.toml"])
    assert codex_plugin_state_snapshot.filesystem_path(manifest_path).is_file()

    config_path.write_text('model = "after"\n', encoding="utf-8")
    result = restore_plugin_state(manifest_path, tmp_path)

    assert result["status"] == "complete"
    assert config_path.read_text(encoding="utf-8") == 'model = "before"\n'
    assert codex_plugin_state_snapshot.filesystem_path(snapshot_root / "plugin_state_restore.json").is_file()
