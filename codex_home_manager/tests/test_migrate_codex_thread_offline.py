from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "migrate_codex_thread_offline.py"
SPEC = importlib.util.spec_from_file_location("migrate_codex_thread_offline", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_rollout(path: Path, payload: dict[str, object]) -> None:
    record = {"timestamp": "2026-09-03T00:00:00Z", "type": "response_item", "payload": payload, "ordinal": 0}
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def test_reasoning_missing_content_and_null_content_are_semantically_equal(tmp_path: Path) -> None:
    before_path = tmp_path / "before.jsonl"
    after_path = tmp_path / "after.jsonl"
    payload = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "same"}]}
    write_rollout(before_path, payload)
    write_rollout(after_path, {**payload, "content": None})

    before = MODULE.rollout_semantic_fingerprints(before_path)
    after = MODULE.rollout_semantic_fingerprints(after_path)

    MODULE.validate_migration_semantics(before, after)


def test_reasoning_non_null_content_change_still_fails(tmp_path: Path) -> None:
    before_path = tmp_path / "before.jsonl"
    after_path = tmp_path / "after.jsonl"
    write_rollout(before_path, {"type": "reasoning", "summary": [], "content": None})
    write_rollout(after_path, {"type": "reasoning", "summary": [], "content": [{"type": "text", "text": "changed"}]})

    before = MODULE.rollout_semantic_fingerprints(before_path)
    after = MODULE.rollout_semantic_fingerprints(after_path)

    with pytest.raises(RuntimeError, match="Migration changed response items"):
        MODULE.validate_migration_semantics(before, after)
