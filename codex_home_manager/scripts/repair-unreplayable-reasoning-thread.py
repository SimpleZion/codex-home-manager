from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


workspace_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace_root))

from backend.thread_history_repair import (
    repair_rollout_compatibility_from_reference_in_place,
    repair_rollout_compatibility_in_place,
    scan_rollout,
)


def read_thread_binding(state_path: Path, thread_id: str) -> tuple[Path, bool]:
    database = sqlite3.connect(f"file:{state_path.as_posix()}?mode=ro", uri=True)
    try:
        row = database.execute(
            "select rollout_path, archived from threads where id = ?",
            (thread_id,),
        ).fetchone()
    finally:
        database.close()
    if row is None:
        raise RuntimeError(f"thread is missing from the state database: {thread_id}")
    return Path(str(row[0])), bool(row[1])


def repair_thread(
    *,
    state_path: Path,
    thread_id: str,
    backup_root: Path,
    allow_online_overwrite: bool,
    reference_prefix_path: Path | None = None,
) -> dict[str, object]:
    rollout_path, archived = read_thread_binding(state_path, thread_id)
    if archived:
        raise RuntimeError(f"thread is archived: {thread_id}")
    before = scan_rollout(rollout_path)
    if before.session_meta_id != thread_id:
        raise RuntimeError(
            f"session_meta id {before.session_meta_id} does not match thread id {thread_id}"
        )
    if before.parse_errors:
        raise RuntimeError(f"source rollout has {before.parse_errors} JSON parse errors")
    reference_scan = None
    if reference_prefix_path is not None:
        reference_prefix_path = reference_prefix_path.resolve()
        reference_scan = scan_rollout(reference_prefix_path)
        if reference_scan.parse_errors:
            raise RuntimeError(
                f"reference rollout has {reference_scan.parse_errors} JSON parse errors"
            )
        if reference_scan.session_meta_id != thread_id:
            raise RuntimeError(
                f"reference session_meta id {reference_scan.session_meta_id} does not match thread id "
                f"{thread_id}"
            )
        if (
            reference_scan.unreplayable_reasoning_item_count == 0
            and reference_scan.incompatible_proxy_item_id_count == 0
        ):
            raise RuntimeError(f"reference has no incompatible proxy response items: {thread_id}")
    if reference_scan is None and (
        before.unreplayable_reasoning_item_count == 0
        and before.incompatible_proxy_item_id_count == 0
    ):
        raise RuntimeError(f"thread has no incompatible proxy response items: {thread_id}")
    source_stat = rollout_path.stat()
    expected_rollout_path = str(rollout_path.resolve()).casefold()

    def precommit_guard() -> None:
        current_rollout_path, current_archived = read_thread_binding(state_path, thread_id)
        if str(current_rollout_path.resolve()).casefold() != expected_rollout_path:
            raise RuntimeError(f"rollout binding changed before commit: {thread_id}")
        if current_archived:
            raise RuntimeError(f"thread became archived before commit: {thread_id}")

    common_arguments = {
        "source_path": rollout_path,
        "backup_root": backup_root,
        "expected_thread_id": thread_id,
        "audited_size": source_stat.st_size,
        "audited_mtime_ns": source_stat.st_mtime_ns,
        "audited_sha256": before.source_sha256,
        "precommit_guard": precommit_guard,
        "allow_in_place_overwrite_when_replace_locked": allow_online_overwrite,
    }
    if reference_scan is not None and reference_prefix_path is not None:
        result = repair_rollout_compatibility_from_reference_in_place(
            reference_prefix_path=reference_prefix_path,
            **common_arguments,
        )
    else:
        result = repair_rollout_compatibility_in_place(**common_arguments)
    after = scan_rollout(rollout_path)
    if after.parse_errors or after.estimated_current_parser_errors:
        raise RuntimeError(
            f"compatibility errors remain after repair: parse={after.parse_errors}, current={after.estimated_current_parser_errors}"
        )
    if (
        after.user_prompt_count != before.user_prompt_count
        or after.user_prompt_sha256 != before.user_prompt_sha256
    ):
        raise RuntimeError("user prompt count, text, or order changed during repair")
    return {
        "thread_id": thread_id,
        "source_path": str(rollout_path),
        "backup_path": result.backup_path,
        "journal_path": result.journal_path,
        "commit_mode": result.commit_mode,
        "before_bytes": before.total_bytes,
        "after_bytes": after.total_bytes,
        "before_lines": before.line_count,
        "after_lines": after.line_count,
        "removed_unreplayable_reasoning_items": before.unreplayable_reasoning_item_count,
        "migrated_proxy_item_ids": before.incompatible_proxy_item_id_count,
        "restored_from_reference_proxy_items": (
            reference_scan.incompatible_proxy_item_id_count if reference_scan is not None else 0
        ),
        "user_prompt_count": after.user_prompt_count,
        "user_prompt_sha256": after.user_prompt_sha256,
        "source_sha256_before": before.source_sha256,
        "source_sha256_after": after.source_sha256,
        "parse_errors_after": after.parse_errors,
        "compatibility_errors_after": after.estimated_current_parser_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Repair a SQLite-bound Codex rollout that contains non-replayable proxy item_ response records."
        )
    )
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path(r"D:\.codex\state_5.sqlite"))
    parser.add_argument("--allow-online-overwrite", action="store_true")
    parser.add_argument("--reference-prefix", type=Path)
    arguments = parser.parse_args()
    result = repair_thread(
        state_path=arguments.state.resolve(),
        thread_id=arguments.thread_id,
        backup_root=arguments.backup_root.resolve(),
        allow_online_overwrite=arguments.allow_online_overwrite,
        reference_prefix_path=(
            arguments.reference_prefix.resolve() if arguments.reference_prefix is not None else None
        ),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
