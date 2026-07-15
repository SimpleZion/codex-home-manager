from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable


version_name_pattern = re.compile(r"^(?P<sequence>\d{8})_(?P<sha256>[0-9a-f]{64})\.json$")
runtime_only_keys = {"_last_manifest_sha256"}


def payload_bytes(payload: dict[str, Any]) -> bytes:
    serializable = {key: value for key, value in payload.items() if key not in runtime_only_keys}
    return json.dumps(serializable, ensure_ascii=False, indent=2).encode("utf-8")


def bytes_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def file_sha256(path: Path) -> str:
    return bytes_sha256(path.read_bytes())


def write_bytes_atomic(path: Path, content: bytes) -> None:
    temporary_path = path.with_suffix(path.suffix + ".writing")
    with temporary_path.open("wb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary_path, path)


def manifest_versions_path(manifest_path: Path) -> Path:
    return manifest_path.with_name("repair_manifest.versions")


def manifest_commit_path(manifest_path: Path) -> Path:
    return manifest_path.with_name("repair_manifest.commit.json")


def _read_version(manifest_path: Path, sequence: int, expected_hash: str) -> tuple[dict[str, Any], bytes]:
    version_path = manifest_versions_path(manifest_path) / f"{sequence:08d}_{expected_hash}.json"
    if not version_path.is_file():
        raise RuntimeError(f"committed repair manifest version is missing: {version_path}")
    content = version_path.read_bytes()
    if bytes_sha256(content) != expected_hash:
        raise RuntimeError(f"repair manifest immutable version hash mismatch: {version_path}")
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"repair manifest immutable version is invalid JSON: {version_path}") from error
    if not isinstance(payload, dict) or int(payload.get("manifest_sequence") or 0) != sequence:
        raise RuntimeError(f"repair manifest immutable version sequence mismatch: {version_path}")
    return payload, content


def _validate_committed_chain(manifest_path: Path, sequence: int, current_hash: str) -> tuple[dict[str, Any], bytes]:
    if sequence < 1 or not re.fullmatch(r"[0-9a-f]{64}", current_hash):
        raise RuntimeError("repair manifest commit pointer is invalid")
    head_payload: dict[str, Any] | None = None
    head_content = b""
    expected_hash = current_hash
    for expected_sequence in range(sequence, 0, -1):
        payload, content = _read_version(manifest_path, expected_sequence, expected_hash)
        if head_payload is None:
            head_payload = payload
            head_content = content
        previous_hash = str(payload.get("previous_manifest_sha256") or "")
        if expected_sequence == 1:
            if previous_hash:
                raise RuntimeError("repair manifest immutable version chain has a broken initial hash")
        elif not re.fullmatch(r"[0-9a-f]{64}", previous_hash):
            raise RuntimeError("repair manifest immutable version chain has a broken previous hash")
        expected_hash = previous_hash
    assert head_payload is not None
    return head_payload, head_content


def _read_commit(manifest_path: Path) -> tuple[int, str] | None:
    commit_path = manifest_commit_path(manifest_path)
    if not commit_path.is_file():
        return None
    try:
        commit = json.loads(commit_path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("repair manifest commit pointer is invalid JSON") from error
    if not isinstance(commit, dict) or int(commit.get("schema_version") or 0) != 1:
        raise RuntimeError("repair manifest commit pointer schema is unsupported")
    sequence = int(commit.get("committed_sequence") or 0)
    current_hash = str(commit.get("committed_sha256") or "").casefold()
    return sequence, current_hash


def _load_legacy_head(manifest_path: Path) -> tuple[int, str, dict[str, Any], bytes] | None:
    versions_path = manifest_versions_path(manifest_path)
    if not versions_path.is_dir():
        return None
    candidates: list[tuple[int, str, dict[str, Any], bytes]] = []
    for pointer_path in (manifest_path, manifest_path.with_name("repair_manifest.mirror.json")):
        if not pointer_path.is_file():
            continue
        content = pointer_path.read_bytes()
        current_hash = bytes_sha256(content)
        try:
            payload = json.loads(content.decode("utf-8-sig"))
            sequence = int(payload.get("manifest_sequence") or 0)
            validated_payload, validated_content = _validate_committed_chain(manifest_path, sequence, current_hash)
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError, ValueError):
            continue
        if validated_content == content:
            candidates.append((sequence, current_hash, validated_payload, validated_content))
    return max(candidates, key=lambda item: item[0]) if candidates else None


def _committed_head(manifest_path: Path) -> tuple[int, str, dict[str, Any], bytes] | None:
    commit = _read_commit(manifest_path)
    if commit is None:
        return _load_legacy_head(manifest_path)
    sequence, current_hash = commit
    payload, content = _validate_committed_chain(manifest_path, sequence, current_hash)
    return sequence, current_hash, payload, content


def write_manifest_pair(
    manifest_path: Path,
    payload: dict[str, Any],
    fault_injector: Callable[[str], None] | None = None,
) -> str:
    manifest_path = manifest_path.resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    committed = _committed_head(manifest_path)
    previous_sequence = committed[0] if committed else 0
    previous_hash = committed[1] if committed else ""
    sequence = previous_sequence + 1
    payload["manifest_sequence"] = sequence
    payload["previous_manifest_sha256"] = previous_hash
    content = payload_bytes(payload)
    current_hash = bytes_sha256(content)

    versions_path = manifest_versions_path(manifest_path)
    versions_path.mkdir(parents=True, exist_ok=True)
    version_path = versions_path / f"{sequence:08d}_{current_hash}.json"
    if version_path.exists():
        if version_path.read_bytes() != content:
            raise RuntimeError(f"immutable repair manifest version changed: {version_path}")
    else:
        with version_path.open("xb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
    if fault_injector:
        fault_injector("after_version")

    mirror_path = manifest_path.with_name("repair_manifest.mirror.json")
    write_bytes_atomic(mirror_path, content)
    if fault_injector:
        fault_injector("after_mirror")
    write_bytes_atomic(manifest_path, content)
    if fault_injector:
        fault_injector("after_primary")
    if mirror_path.read_bytes() != content or manifest_path.read_bytes() != content:
        raise RuntimeError("repair manifest primary/mirror verification failed")

    commit_content = json.dumps(
        {
            "schema_version": 1,
            "committed_sequence": sequence,
            "committed_sha256": current_hash,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    write_bytes_atomic(manifest_commit_path(manifest_path), commit_content)
    if fault_injector:
        fault_injector("after_commit")
    committed_after_write = _committed_head(manifest_path)
    if committed_after_write is None or committed_after_write[:2] != (sequence, current_hash):
        raise RuntimeError("repair manifest commit verification failed")
    payload["_last_manifest_sha256"] = current_hash
    return current_hash


def load_manifest_pair(
    manifest_path: Path,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    manifest_path = manifest_path.resolve()
    committed = _committed_head(manifest_path)
    if committed is None:
        raise RuntimeError("repair manifest has no committed immutable version")
    _, current_hash, selected_payload, _ = committed
    normalized_expected = str(expected_sha256 or "").strip().casefold()
    if normalized_expected:
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_expected):
            raise RuntimeError("expected repair manifest SHA-256 is invalid")
        if current_hash != normalized_expected:
            raise RuntimeError("repair manifest does not match the runner-bound SHA-256")
    payload = dict(selected_payload)
    payload["_last_manifest_sha256"] = current_hash
    return payload, current_hash


def main() -> int:
    parser = argparse.ArgumentParser(description="Update and verify the chained Codex repair manifest pair.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--expected-run-id")
    parser.add_argument("--expected-run-root", type=Path)
    parser.add_argument("--expected-status")
    parser.add_argument("--new-status")
    parser.add_argument("--set", action="append", default=[])
    arguments = parser.parse_args()
    payload, committed_hash = load_manifest_pair(arguments.manifest, arguments.expected_sha256)
    if arguments.inspect:
        if any(
            value is not None
            for value in (
                arguments.expected_run_id,
                arguments.expected_run_root,
                arguments.expected_status,
                arguments.new_status,
            )
        ) or arguments.set:
            raise RuntimeError("--inspect cannot be combined with manifest mutation arguments")
        print(json.dumps({"sha256": committed_hash, "payload": payload}, ensure_ascii=False))
        return 0
    if not all(
        (
            arguments.expected_run_id,
            arguments.expected_run_root,
            arguments.expected_status,
            arguments.new_status,
        )
    ):
        raise RuntimeError("manifest mutation requires run id, run root, expected status, and new status")
    if str(payload.get("runner_run_id") or "") != arguments.expected_run_id:
        raise RuntimeError("repair manifest run id mismatch")
    if Path(str(payload.get("run_root") or "")).resolve() != arguments.expected_run_root.resolve():
        raise RuntimeError("repair manifest run root mismatch")
    if str(payload.get("status") or "") != arguments.expected_status:
        raise RuntimeError(f"repair manifest has unexpected status: {payload.get('status')}")
    payload["status"] = arguments.new_status
    for entry in arguments.set:
        if "=" not in entry:
            raise RuntimeError(f"invalid --set entry: {entry}")
        key, value = entry.split("=", 1)
        if not key:
            raise RuntimeError("manifest update key is empty")
        payload[key] = value
    current_hash = write_manifest_pair(arguments.manifest, payload)
    print(json.dumps({"status": payload["status"], "sha256": current_hash}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
