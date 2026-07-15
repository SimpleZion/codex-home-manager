from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import sys


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root / "codex_home_manager"))

from backend.offline_repair_policy import assert_backup_path, assert_codex_offline, default_backup_root


default_relative_paths = [
    "config.toml",
    "managed_config.toml",
    "state_5.sqlite",
    "state_5.sqlite-wal",
    "state_5.sqlite-shm",
    "state_5.sqlite-journal",
    ".codex-global-state.json",
    ".codex-global-state.json.bak",
    "session_index.jsonl",
    "plugins",
    ".tmp/plugins",
    ".tmp/bundled-marketplaces",
    "cache/bundled-marketplaces",
]
stale_restore_name_pattern = re.compile(r"^\..+\.[0-9a-f]{32}\.restoring$", re.IGNORECASE)


def filesystem_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    absolute = os.path.abspath(os.fspath(path))
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{absolute[2:]}")
    return Path(f"\\\\?\\{absolute}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with filesystem_path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_bytes(path: Path) -> bytes:
    return filesystem_path(path).read_bytes()


def read_text(path: Path, encoding: str = "utf-8-sig") -> str:
    return filesystem_path(path).read_text(encoding=encoding)


def write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    filesystem_path(path).write_text(content, encoding=encoding)


def mkdir_path(path: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
    filesystem_path(path).mkdir(parents=parents, exist_ok=exist_ok)


def replace_path(source: Path, destination: Path) -> None:
    os.replace(filesystem_path(source), filesystem_path(destination))


def lexists(path: Path) -> bool:
    return os.path.lexists(filesystem_path(path))


def validate_relative_paths(relative_paths: list[str]) -> list[Path]:
    normalized: list[Path] = []
    for text in relative_paths:
        relative_path = Path(text)
        if relative_path.is_absolute() or ".." in relative_path.parts or str(relative_path) in {"", "."}:
            raise RuntimeError(f"unsafe snapshot relative path: {text}")
        normalized.append(relative_path)
    for index, path in enumerate(normalized):
        for other in normalized[index + 1 :]:
            if path in other.parents or other in path.parents:
                raise RuntimeError(f"overlapping snapshot paths are not allowed: {path}, {other}")
    return normalized


def path_signature(path: Path) -> list[dict[str, Any]]:
    filesystem_root = filesystem_path(path)
    if filesystem_root.is_symlink():
        raise RuntimeError(f"symbolic links are not allowed in plugin snapshots: {path}")
    if filesystem_root.is_junction():
        return [{"path": ".", "type": "junction", "target": os.readlink(filesystem_root)}]
    if filesystem_root.is_file():
        return [
            {
                "path": ".",
                "type": "file",
                "bytes": filesystem_root.stat().st_size,
                "sha256": file_sha256(filesystem_root),
            }
        ]
    if not filesystem_root.is_dir():
        raise RuntimeError(f"unsupported snapshot path type: {path}")

    entries: list[dict[str, Any]] = [{"path": ".", "type": "directory"}]

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            relative = child.relative_to(filesystem_root).as_posix()
            if child.is_symlink():
                raise RuntimeError(f"symbolic links are not allowed in plugin snapshots: {child}")
            if child.is_junction():
                entries.append({"path": relative, "type": "junction", "target": os.readlink(child)})
            elif child.is_dir():
                entries.append({"path": relative, "type": "directory"})
                visit(child)
            elif child.is_file():
                entries.append(
                    {
                        "path": relative,
                        "type": "file",
                        "bytes": child.stat().st_size,
                        "sha256": file_sha256(child),
                    }
                )
            else:
                raise RuntimeError(f"unsupported item in snapshot tree: {child}")

    visit(filesystem_root)
    return entries


def signature_sha256(signature: list[dict[str, Any]]) -> str:
    content = json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def repair_source_record(path: Path) -> dict[str, Any]:
    signature = path_signature(path)
    return {
        "path": str(path.resolve()),
        "entry_count": len(signature),
        "file_bytes": sum(int(item.get("bytes") or 0) for item in signature if item.get("type") == "file"),
        "tree_sha256": signature_sha256(signature),
    }


def verify_repair_sources(manifest_path: Path, expected_manifest_sha256: str | None = None) -> list[str]:
    manifest_bytes = read_bytes(manifest_path)
    expected_hash = str(expected_manifest_sha256 or "").strip().casefold()
    if expected_hash and file_sha256(manifest_path) != expected_hash:
        raise RuntimeError("plugin snapshot manifest hash does not match the bound repair transaction")
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    if int(manifest.get("schema_version") or 0) != 1 or manifest.get("status") != "complete":
        raise RuntimeError("plugin snapshot manifest is incomplete or unsupported")
    verified: list[str] = []
    for expected in manifest.get("repair_sources") or []:
        source_path = Path(str(expected.get("path") or "")).resolve()
        if not source_path.is_dir():
            raise RuntimeError(f"repair source tree is missing: {source_path}")
        actual = repair_source_record(source_path)
        if actual != expected:
            raise RuntimeError(f"repair source tree changed after snapshot: {source_path}")
        verified.append(str(source_path))
    if not verified:
        raise RuntimeError("plugin snapshot manifest has no bound repair source tree")
    return verified


def archive_stale_restore_artifacts(
    codex_home: Path,
    archive_root: Path,
    backup_policy_root: Path = default_backup_root,
) -> dict[str, Any]:
    assert_codex_offline()
    codex_home = codex_home.resolve()
    archive_root = archive_root.resolve(strict=False)
    assert_backup_path(archive_root, backup_policy_root)
    mkdir_path(archive_root, parents=True, exist_ok=True)
    archived: list[dict[str, Any]] = []
    candidates = sorted(
        (
            candidate
            for candidate in filesystem_path(codex_home).iterdir()
            if stale_restore_name_pattern.fullmatch(candidate.name)
        ),
        key=lambda candidate: candidate.name.casefold(),
    )
    for candidate in candidates:
        source_path = codex_home / candidate.name
        archive_path = archive_root / candidate.name
        if lexists(archive_path):
            raise RuntimeError(f"stale restore archive target already exists: {archive_path}")
        source_stat = filesystem_path(source_path).lstat()
        artifact_type = (
            "junction"
            if filesystem_path(source_path).is_junction()
            else "directory"
            if filesystem_path(source_path).is_dir()
            else "file"
        )
        assert_codex_offline()
        replace_path(source_path, archive_path)
        if lexists(source_path) or not lexists(archive_path):
            raise RuntimeError(f"stale restore artifact archive verification failed: {source_path}")
        archived.append(
            {
                "source": str(source_path),
                "archive": str(archive_path),
                "name": candidate.name,
                "type": artifact_type,
                "size": int(source_stat.st_size),
                "mtime_ns": int(source_stat.st_mtime_ns),
            }
        )

    result = {
        "schema_version": 1,
        "status": "complete",
        "codex_home": str(codex_home),
        "archive_root": str(archive_root),
        "archived": archived,
        "archived_count": len(archived),
        "completed_at_epoch": int(time.time()),
    }
    result_path = archive_root / "stale_restore_archive.json"
    temporary_result = result_path.with_suffix(".json.writing")
    write_text(temporary_result, json.dumps(result, ensure_ascii=False, indent=2))
    replace_path(temporary_result, result_path)
    return result


def signature_without_junctions(signature: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in signature if entry["type"] != "junction"]


def copy_without_junctions(source: Path, destination: Path) -> None:
    filesystem_source = filesystem_path(source)
    filesystem_destination = filesystem_path(destination)
    if filesystem_source.is_symlink():
        raise RuntimeError(f"symbolic links are not allowed in plugin snapshots: {source}")
    if filesystem_source.is_file():
        mkdir_path(destination.parent, parents=True, exist_ok=True)
        shutil.copy2(filesystem_source, filesystem_destination)
        return
    mkdir_path(destination, parents=True, exist_ok=False)
    for child in filesystem_source.iterdir():
        if child.is_symlink():
            raise RuntimeError(f"symbolic links are not allowed in plugin snapshots: {child}")
        if child.is_junction():
            continue
        target = filesystem_destination / child.name
        if child.is_dir():
            copy_without_junctions(child, target)
        elif child.is_file():
            shutil.copy2(child, target)
        else:
            raise RuntimeError(f"unsupported item while copying snapshot: {child}")


def path_bytes_without_junctions(path: Path) -> int:
    filesystem_root = filesystem_path(path)
    if filesystem_root.is_symlink():
        raise RuntimeError(f"symbolic links are not allowed in plugin snapshots: {path}")
    if filesystem_root.is_junction():
        return 0
    if filesystem_root.is_file():
        return filesystem_root.stat().st_size
    if not filesystem_root.is_dir():
        return 0
    return sum(path_bytes_without_junctions(child) for child in filesystem_root.iterdir())


def existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        raise RuntimeError(f"could not find existing parent for snapshot: {path}")
    return candidate


def snapshot_space_budget(source_bytes: int, repair_source_bytes: int = 0) -> dict[str, int]:
    backup_reserve_bytes = 1024 * 1024 * 1024
    source_reserve_bytes = 512 * 1024 * 1024
    components: dict[str, int] = {
        "snapshot_copy_bytes": source_bytes,
        "repair_archive_bytes": source_bytes,
        "repair_rebuild_bytes": source_bytes,
        "restore_temporary_bytes": source_bytes,
        "rollback_failed_state_bytes": source_bytes,
        "rollback_artifact_bytes": source_bytes,
        "repair_source_bytes": repair_source_bytes,
        "new_repair_materialization_bytes": repair_source_bytes * 3,
        "new_repair_rollback_bytes": repair_source_bytes,
        "backup_reserve_bytes": backup_reserve_bytes,
        "source_reserve_bytes": source_reserve_bytes,
    }
    components["backup_required_bytes"] = source_bytes * 4 + repair_source_bytes + backup_reserve_bytes
    components["source_required_bytes"] = source_bytes * 2 + repair_source_bytes * 3 + source_reserve_bytes
    components["same_volume_required_bytes"] = (
        source_bytes * 6 + repair_source_bytes * 4 + backup_reserve_bytes + source_reserve_bytes
    )
    components["required_bytes"] = components["same_volume_required_bytes"]
    return components


def create_junction(path: Path, target: str) -> None:
    mkdir_path(path.parent, parents=True, exist_ok=True)
    target_path = Path(target)
    filesystem_target = filesystem_path(target_path) if target_path.is_absolute() else target_path
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(filesystem_path(path)), str(filesystem_target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"could not create junction {path} -> {target}: {completed.stderr or completed.stdout}")


def recreate_junctions(root: Path, signature: list[dict[str, Any]]) -> None:
    for entry in signature:
        if entry["type"] != "junction":
            continue
        junction_path = root if entry["path"] == "." else root / Path(entry["path"])
        create_junction(junction_path, str(entry["target"]))


def snapshot_plugin_state(
    codex_home: Path,
    snapshot_root: Path,
    relative_paths: list[str] | None = None,
    repair_source_paths: list[Path] | None = None,
    runner_run_id: str | None = None,
    run_root: Path | None = None,
) -> Path:
    assert_codex_offline()
    codex_home = codex_home.resolve()
    snapshot_root = snapshot_root.resolve()
    effective_run_root = (run_root or snapshot_root.parent).resolve()
    effective_run_id = str(runner_run_id or f"standalone-{uuid4().hex}")
    if run_root is not None and snapshot_root != effective_run_root / "plugin_state_snapshot":
        raise RuntimeError("plugin snapshot root must belong to the bound repair run")
    if not codex_home.is_dir():
        raise RuntimeError(f"CODEX_HOME does not exist: {codex_home}")
    try:
        snapshot_root.relative_to(codex_home)
    except ValueError:
        pass
    else:
        raise RuntimeError("plugin snapshot must be outside CODEX_HOME")
    selected_paths = validate_relative_paths(relative_paths or default_relative_paths)
    source_bytes = sum(
        path_bytes_without_junctions(codex_home / relative_path)
        for relative_path in selected_paths
        if lexists(codex_home / relative_path)
    )
    resolved_repair_sources = sorted(
        {path.resolve() for path in (repair_source_paths or [])},
        key=lambda path: str(path).casefold(),
    )
    for repair_source in resolved_repair_sources:
        if not repair_source.is_dir():
            raise RuntimeError(f"repair source does not exist: {repair_source}")
    repair_sources = [repair_source_record(path) for path in resolved_repair_sources]
    repair_source_bytes = sum(path_bytes_without_junctions(path) for path in resolved_repair_sources)
    space_budget = snapshot_space_budget(source_bytes, repair_source_bytes)
    required_bytes = space_budget["required_bytes"]
    backup_probe_path = existing_parent(snapshot_root)
    source_probe_path = existing_parent(codex_home)
    backup_free_bytes = shutil.disk_usage(backup_probe_path).free
    source_free_bytes = shutil.disk_usage(source_probe_path).free
    same_volume = backup_probe_path.drive.casefold() == source_probe_path.drive.casefold()
    if same_volume and backup_free_bytes < space_budget["same_volume_required_bytes"]:
        raise RuntimeError(
            f"insufficient snapshot space: free={backup_free_bytes} required={space_budget['same_volume_required_bytes']}"
        )
    if not same_volume and backup_free_bytes < space_budget["backup_required_bytes"]:
        raise RuntimeError(
            f"insufficient backup-volume snapshot space: free={backup_free_bytes} required={space_budget['backup_required_bytes']}"
        )
    if not same_volume and source_free_bytes < space_budget["source_required_bytes"]:
        raise RuntimeError(
            f"insufficient CODEX_HOME-volume repair space: free={source_free_bytes} required={space_budget['source_required_bytes']}"
        )
    mkdir_path(snapshot_root, parents=True, exist_ok=False)
    data_root = snapshot_root / "data"
    mkdir_path(data_root)
    roots: list[dict[str, Any]] = []

    for relative_path in selected_paths:
        assert_codex_offline()
        source_path = codex_home / relative_path
        snapshot_path = data_root / relative_path
        item: dict[str, Any] = {
            "relative_path": relative_path.as_posix(),
            "existed": lexists(source_path),
            "source_signature": [],
            "snapshot_signature": [],
        }
        if item["existed"]:
            source_signature = path_signature(source_path)
            copy_without_junctions(source_path, snapshot_path)
            snapshot_signature = path_signature(snapshot_path)
            if signature_without_junctions(source_signature) != snapshot_signature:
                raise RuntimeError(f"snapshot verification failed: {source_path}")
            assert_codex_offline()
            if path_signature(source_path) != source_signature:
                raise RuntimeError(f"plugin state changed during snapshot: {source_path}")
            item["source_signature"] = source_signature
            item["snapshot_signature"] = snapshot_signature
        roots.append(item)

    for expected_repair_source in repair_sources:
        source_path = Path(expected_repair_source["path"])
        if repair_source_record(source_path) != expected_repair_source:
            raise RuntimeError(f"repair source tree changed during snapshot: {source_path}")

    manifest = {
        "schema_version": 1,
        "status": "complete",
        "runner_run_id": effective_run_id,
        "run_root": str(effective_run_root),
        "created_at_epoch": int(time.time()),
        "codex_home": str(codex_home),
        "snapshot_root": str(snapshot_root),
        "repair_source_paths": [str(path) for path in resolved_repair_sources],
        "repair_sources": repair_sources,
        "disk_preflight": {
            "source_bytes": source_bytes,
            **space_budget,
            "same_volume": same_volume,
            "backup_free_bytes": backup_free_bytes,
            "source_free_bytes": source_free_bytes,
            "backup_probe_path": str(backup_probe_path),
            "source_probe_path": str(source_probe_path),
        },
        "roots": roots,
    }
    manifest_path = snapshot_root / "plugin_state_snapshot.json"
    temporary_manifest = manifest_path.with_suffix(".json.writing")
    write_text(temporary_manifest, json.dumps(manifest, ensure_ascii=False, indent=2))
    replace_path(temporary_manifest, manifest_path)
    manifest_sha256 = file_sha256(manifest_path)
    binding_path = snapshot_root / "plugin_state_snapshot.sha256.json"
    binding = {
        "schema_version": 1,
        "runner_run_id": effective_run_id,
        "run_root": str(effective_run_root),
        "codex_home": str(codex_home),
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha256,
    }
    temporary_binding = binding_path.with_suffix(".json.writing")
    write_text(temporary_binding, json.dumps(binding, ensure_ascii=False, indent=2))
    replace_path(temporary_binding, binding_path)
    return manifest_path


def restore_plugin_state(
    manifest_path: Path,
    backup_policy_root: Path = default_backup_root,
    *,
    expected_run_id: str | None = None,
    expected_run_root: Path | None = None,
    expected_codex_home: Path | None = None,
    expected_manifest_sha256: str | None = None,
    skip_relative_paths: set[str] | None = None,
) -> dict[str, object]:
    assert_codex_offline()
    manifest_path = manifest_path.resolve()
    assert_backup_path(manifest_path, backup_policy_root)
    manifest_bytes = read_bytes(manifest_path)
    actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    normalized_expected_hash = str(expected_manifest_sha256 or "").strip().casefold()
    if expected_run_id is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_expected_hash):
            raise RuntimeError("expected plugin snapshot manifest SHA-256 is invalid")
        if actual_manifest_sha256 != normalized_expected_hash:
            raise RuntimeError("plugin snapshot manifest does not match the runner-bound SHA-256")
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    if manifest.get("schema_version") != 1 or manifest.get("status") != "complete":
        raise RuntimeError("plugin snapshot manifest is incomplete or unsupported")
    codex_home = Path(manifest["codex_home"]).resolve()
    snapshot_root = Path(manifest["snapshot_root"]).resolve()
    if manifest_path.parent != snapshot_root:
        raise RuntimeError("plugin snapshot manifest path does not match snapshot root")
    if expected_run_id is not None:
        expected_root = Path(expected_run_root or "").resolve()
        expected_home = Path(expected_codex_home or "").resolve()
        if str(manifest.get("runner_run_id") or "") != expected_run_id:
            raise RuntimeError("plugin snapshot run id mismatch")
        if Path(str(manifest.get("run_root") or "")).resolve() != expected_root:
            raise RuntimeError("plugin snapshot run root mismatch")
        if snapshot_root != expected_root / "plugin_state_snapshot":
            raise RuntimeError("plugin snapshot root is outside the bound repair run")
        if codex_home != expected_home:
            raise RuntimeError("plugin snapshot CODEX_HOME mismatch")
        binding_path = snapshot_root / "plugin_state_snapshot.sha256.json"
        binding = json.loads(read_text(binding_path))
        if (
            str(binding.get("runner_run_id") or "") != expected_run_id
            or Path(str(binding.get("run_root") or "")).resolve() != expected_root
            or Path(str(binding.get("codex_home") or "")).resolve() != expected_home
            or str(binding.get("manifest_sha256") or "").casefold() != actual_manifest_sha256
        ):
            raise RuntimeError("plugin snapshot SHA-256 binding is invalid")
    failed_state = snapshot_root / "failed_state"
    mkdir_path(failed_state, exist_ok=True)
    restored: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[str] = []
    prepared: list[dict[str, Any]] = []
    swapped: list[dict[str, Any]] = []
    normalized_skips = {
        validate_relative_paths([relative_path])[0].as_posix().casefold()
        for relative_path in skip_relative_paths or set()
    }
    manifest_relative_paths = {
        validate_relative_paths([str(item["relative_path"])])[0].as_posix().casefold()
        for item in manifest["roots"]
    }
    unknown_skips = normalized_skips - manifest_relative_paths
    if unknown_skips:
        raise RuntimeError(f"plugin restore skip path is not part of the snapshot: {sorted(unknown_skips)}")
    try:
        for item in manifest["roots"]:
            assert_codex_offline()
            relative_path = validate_relative_paths([str(item["relative_path"])])[0]
            if relative_path.as_posix().casefold() in normalized_skips:
                skipped.append(relative_path.as_posix())
                continue
            source_path = codex_home / relative_path
            snapshot_path = snapshot_root / "data" / relative_path
            temporary_restore = source_path.with_name(f".{source_path.name}.{uuid4().hex}.restoring")
            mkdir_path(source_path.parent, parents=True, exist_ok=True)
            if item["existed"]:
                if path_signature(snapshot_path) != item["snapshot_signature"]:
                    raise RuntimeError(f"snapshot data changed: {snapshot_path}")
                copy_without_junctions(snapshot_path, temporary_restore)
                recreate_junctions(temporary_restore, item["source_signature"])
                if path_signature(temporary_restore) != item["source_signature"]:
                    raise RuntimeError(f"prepared restore tree does not match source snapshot: {source_path}")
            prepared.append(
                {
                    "item": item,
                    "relative_path": relative_path,
                    "source_path": source_path,
                    "temporary_restore": temporary_restore,
                }
            )

        for prepared_item in prepared:
            assert_codex_offline()
            item = prepared_item["item"]
            relative_path = prepared_item["relative_path"]
            source_path = prepared_item["source_path"]
            temporary_restore = prepared_item["temporary_restore"]
            failed_path = failed_state / relative_path
            current_archived = False
            if lexists(source_path):
                mkdir_path(failed_path.parent, parents=True, exist_ok=True)
                if lexists(failed_path):
                    failed_path = failed_path.with_name(f"{failed_path.name}.{uuid4().hex}")
                replace_path(source_path, failed_path)
                current_archived = True
            swap_record = {
                **prepared_item,
                "failed_path": failed_path,
                "current_archived": current_archived,
            }
            swapped.append(swap_record)
            if item["existed"]:
                assert_codex_offline()
                replace_path(temporary_restore, source_path)
                if path_signature(source_path) != item["source_signature"]:
                    raise RuntimeError(f"restored plugin state verification failed: {source_path}")
            elif lexists(source_path):
                raise RuntimeError(f"path should be absent after restore: {source_path}")

            restored.append(
                {
                    "relative_path": relative_path.as_posix(),
                    "restored_existence": bool(item["existed"]),
                    "archived_failed_path": str(failed_path) if current_archived else None,
                }
            )
    except Exception as error:
        rollback_errors: list[str] = []
        for swap_record in reversed(swapped):
            source_path = swap_record["source_path"]
            failed_path = swap_record["failed_path"]
            relative_path = swap_record["relative_path"]
            try:
                if lexists(source_path):
                    artifact_path = failed_state / "restore_artifacts" / f"{source_path.name}.{uuid4().hex}.partial"
                    mkdir_path(artifact_path.parent, parents=True, exist_ok=True)
                    assert_codex_offline()
                    replace_path(source_path, artifact_path)
                if swap_record["current_archived"] and lexists(failed_path):
                    mkdir_path(source_path.parent, parents=True, exist_ok=True)
                    assert_codex_offline()
                    replace_path(failed_path, source_path)
            except Exception as rollback_error:
                rollback_errors.append(f"{relative_path}: {rollback_error}")
        for prepared_item in prepared:
            temporary_restore = prepared_item["temporary_restore"]
            if lexists(temporary_restore):
                artifact_path = failed_state / "restore_artifacts" / temporary_restore.name
                mkdir_path(artifact_path.parent, parents=True, exist_ok=True)
                assert_codex_offline()
                replace_path(temporary_restore, artifact_path)
        errors.append(str(error))
        errors.extend(f"transaction rollback failed: {message}" for message in rollback_errors)

    result: dict[str, object] = {
        "status": "complete" if not errors else "failed",
        "restored": restored,
        "skipped": skipped,
        "errors": errors,
        "completed_at_epoch": int(time.time()),
    }
    result_path = snapshot_root / "plugin_state_restore.json"
    temporary_result = result_path.with_suffix(".json.writing")
    write_text(temporary_result, json.dumps(result, ensure_ascii=False, indent=2))
    replace_path(temporary_result, result_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot or restore all Codex plugin/config mutation surfaces.")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--codex-home", type=Path, required=True)
    snapshot_parser.add_argument("--snapshot-root", type=Path, required=True)
    snapshot_parser.add_argument("--repair-source", type=Path, action="append", default=[])
    snapshot_parser.add_argument("--run-id", required=True)
    snapshot_parser.add_argument("--run-root", type=Path, required=True)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--manifest", type=Path, required=True)
    restore_parser.add_argument("--expected-run-id", required=True)
    restore_parser.add_argument("--expected-run-root", type=Path, required=True)
    restore_parser.add_argument("--expected-codex-home", type=Path, required=True)
    restore_parser.add_argument("--expected-manifest-sha256", required=True)
    restore_parser.add_argument("--skip-relative-path", action="append", default=[])
    verify_parser = subparsers.add_parser("verify-sources")
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--expected-manifest-sha256")
    archive_parser = subparsers.add_parser("archive-stale-restores")
    archive_parser.add_argument("--codex-home", type=Path, required=True)
    archive_parser.add_argument("--archive-root", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.mode == "snapshot":
        assert_backup_path(arguments.snapshot_root, default_backup_root)
        path = snapshot_plugin_state(
            arguments.codex_home,
            arguments.snapshot_root,
            repair_source_paths=arguments.repair_source,
            runner_run_id=arguments.run_id,
            run_root=arguments.run_root,
        )
        print(json.dumps({"status": "complete", "manifest": str(path)}, ensure_ascii=False))
        return 0
    if arguments.mode == "verify-sources":
        verified = verify_repair_sources(arguments.manifest, arguments.expected_manifest_sha256)
        print(json.dumps({"status": "complete", "verified_sources": verified}, ensure_ascii=False))
        return 0
    if arguments.mode == "archive-stale-restores":
        archived = archive_stale_restore_artifacts(arguments.codex_home, arguments.archive_root)
        print(json.dumps(archived, ensure_ascii=False))
        return 0
    result = restore_plugin_state(
        arguments.manifest,
        default_backup_root,
        expected_run_id=arguments.expected_run_id,
        expected_run_root=arguments.expected_run_root,
        expected_codex_home=arguments.expected_codex_home,
        expected_manifest_sha256=arguments.expected_manifest_sha256,
        skip_relative_paths=set(arguments.skip_relative_path),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
