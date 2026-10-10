from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from pathlib import Path

workspace_root = Path(__file__).resolve().parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

from backend.thread_history_repair import repair_orphan_custom_tool_calls_in_place, unpaired_custom_tool_call_counts


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    source = arguments.source.resolve(strict=True)
    initial_stat = source.stat()
    initial_sha256 = file_sha256(source)
    orphan_calls_before = unpaired_custom_tool_call_counts(source)
    if not orphan_calls_before:
        raise RuntimeError("the source rollout has no orphan custom tool calls")
    result = repair_orphan_custom_tool_calls_in_place(
        source_path=source,
        backup_root=arguments.backup_root.resolve(strict=False),
        expected_thread_id=arguments.thread_id,
        audited_size=initial_stat.st_size,
        audited_mtime_ns=initial_stat.st_mtime_ns,
        audited_sha256=initial_sha256,
        allow_in_place_overwrite_when_replace_locked=False,
    )
    orphan_calls_after = unpaired_custom_tool_call_counts(source)
    if orphan_calls_after:
        raise RuntimeError(f"orphan custom tool calls remain after repair: {orphan_calls_after}")
    payload = dataclasses.asdict(result)
    payload.update({"status": "repaired", "orphan_calls_before": orphan_calls_before, "orphan_calls_after": orphan_calls_after})
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
