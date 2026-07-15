from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import backend.thread_history_repair as thread_history_repair
from backend.windows_paths import windows_extended_path
from backend.thread_history_repair import (
    RepairThresholds,
    build_repair_plan,
    compacted_checkpoint_contract,
    create_compatibility_view,
    create_slim_view,
    repair_rollout_compatibility_in_place,
    repair_rollout_in_place,
    scan_rollout,
    select_repair_candidates,
    validate_user_prompt_contract,
)


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def valid_compacted() -> dict[str, object]:
    return {
        "type": "compacted",
        "payload": {
            "replacement_history": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "checkpoint summary"}],
                },
                {
                    "type": "compaction",
                    "id": "compaction-a",
                    "encrypted_content": "encrypted-checkpoint",
                },
            ]
        },
    }


def test_compacted_checkpoint_contract_rejects_text_bearing_unknown_objects() -> None:
    for replacement_history in (
        [{"summary": "arbitrary text", "bogus": 123}],
        [{"type": "unknown", "content": "text"}],
        [{"type": "message", "role": "user", "content": "legacy string"}],
    ):
        valid, reason, _, _ = compacted_checkpoint_contract(
            {"type": "compacted", "payload": {"replacement_history": replacement_history}}
        )
        assert valid is False
        assert reason.startswith("invalid_replacement_history_item")


def test_compacted_checkpoint_contract_accepts_real_message_and_compaction_protocol() -> None:
    valid, reason, history_count, text_count = compacted_checkpoint_contract(valid_compacted())

    assert valid is True
    assert reason == ""
    assert history_count == 2
    assert text_count == 1


def test_compacted_checkpoint_contract_accepts_current_blank_blocks_and_optional_compaction_id() -> None:
    valid, reason, history_count, text_count = compacted_checkpoint_contract(
        {
            "type": "compacted",
            "payload": {
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "\n"}],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "replayable summary"}],
                    },
                    {"type": "compaction", "encrypted_content": "encrypted-checkpoint"},
                ]
            },
        }
    )

    assert valid is True
    assert reason == ""
    assert history_count == 3
    assert text_count == 1


def test_compacted_checkpoint_contract_rejects_invalid_optional_compaction_id() -> None:
    checkpoint = valid_compacted()
    checkpoint["payload"]["replacement_history"][1]["id"] = ""

    valid, reason, _, _ = compacted_checkpoint_contract(checkpoint)

    assert valid is False
    assert reason == "invalid_replacement_history_item_1"


def test_create_slim_view_rejects_structurally_valid_but_unrelated_checkpoint(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    destination_path = tmp_path / "rollout-thread-a.slim.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "repair the financial model and portfolio risk"}],
                },
            },
            valid_compacted()
            | {
                "payload": {
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "gardening weather and flower watering"}],
                        },
                        {"type": "compaction", "id": "foreign", "encrypted_content": "foreign-checkpoint"},
                    ]
                }
            },
        ],
    )

    scan = scan_rollout(source_path)
    assert scan.latest_compacted_checkpoint_valid is False
    assert scan.latest_compacted_checkpoint_reason == "checkpoint_text_has_no_source_overlap"
    with pytest.raises(RuntimeError, match="checkpoint_text_has_no_source_overlap"):
        create_slim_view(source_path, destination_path, expected_thread_id="thread-a")


def test_scan_rollout_detects_current_parser_incompatibilities(tmp_path: Path) -> None:
    rollout_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "tool_search_output",
                    "call_id": "call-1",
                    "tools_summary": {"count": 2},
                },
            },
            {"type": "event_msg", "payload": {"type": "thread_name_updated", "thread_name": "Renamed"}},
            {"type": "compacted", "payload": {"replacement_history": []}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )

    result = scan_rollout(rollout_path)

    assert result.line_count == 5
    assert result.latest_compacted_line == 4
    assert result.tail_line_count == 2
    assert result.tools_summary_output_count == 1
    assert result.thread_name_updated_count == 1
    assert result.estimated_current_parser_errors == 2
    assert result.parse_errors == 0
    assert result.latest_compacted_checkpoint_valid is False
    assert result.latest_compacted_checkpoint_reason == "empty_replacement_history"


def test_scan_and_compatibility_view_repair_invalid_runtime_image_urls(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    destination_path = tmp_path / "rollout-thread-a.compat.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "keep this text"},
                        {"type": "input_image", "image_url": "[image omitted during recovery]"},
                    ],
                },
            },
        ],
    )

    before = scan_rollout(source_path)
    assert before.invalid_image_url_count == 1
    assert before.estimated_current_parser_errors == 1

    create_compatibility_view(source_path, destination_path, expected_thread_id="thread-a")

    after = scan_rollout(destination_path)
    records = [json.loads(line) for line in destination_path.read_text(encoding="utf-8").splitlines()]
    assert after.invalid_image_url_count == 0
    assert after.estimated_current_parser_errors == 0
    assert records[1]["payload"]["content"][0]["text"] == "keep this text"
    assert all(item.get("type") != "input_image" for item in records[1]["payload"]["content"])


def test_compatibility_scan_preserves_image_url_json_schema_definition(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    destination_path = tmp_path / "rollout-thread-a.compat.jsonl"
    schema = {
        "type": "object",
        "properties": {
            "image_url": {
                "type": "string",
                "description": "A URL supplied by a future tool call",
            }
        },
    }
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "tool_search_output",
                    "tools": [{"name": "image_tool", "input_schema": schema}],
                },
            },
        ],
    )

    assert scan_rollout(source_path).invalid_image_url_count == 0
    create_compatibility_view(source_path, destination_path, expected_thread_id="thread-a")
    records = [json.loads(line) for line in destination_path.read_text(encoding="utf-8").splitlines()]
    assert records[1]["payload"]["tools"][0]["input_schema"] == schema
    assert scan_rollout(destination_path).estimated_current_parser_errors == 0


def test_create_slim_view_preserves_full_world_state_and_latest_checkpoint(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    destination_path = tmp_path / "rollout-thread-a.slim.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "thread-a"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": []}},
        {"type": "world_state", "payload": {"full": True, "state": {"goal": "keep"}}},
        {"type": "world_state", "payload": {"full": False, "state": {"patch": 1}}},
        valid_compacted(),
        {"type": "session_meta", "payload": {"id": "forked-copy"}},
        {"type": "world_state", "payload": {"full": False, "state": {"patch": 2}}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
    ]
    write_jsonl(source_path, records)

    result = create_slim_view(source_path, destination_path, expected_thread_id="thread-a")
    slim_records = [json.loads(line) for line in destination_path.read_text(encoding="utf-8").splitlines()]

    assert [record["type"] for record in slim_records] == [
        "session_meta",
        "response_item",
        "world_state",
        "compacted",
        "world_state",
        "response_item",
    ]
    assert slim_records[2]["payload"]["full"] is True
    assert slim_records[2]["payload"]["state"] == {"goal": "keep"}
    assert sum(record["type"] == "session_meta" for record in slim_records) == 1
    assert result.source_line_count == 8
    assert result.view_line_count == 6
    assert result.parse_errors == 0
    assert result.session_meta_id == "thread-a"


def test_create_slim_view_preserves_all_user_prompt_text_and_order(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    destination_path = tmp_path / "rollout-thread-a.slim.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "thread-a"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "first exact prompt\nwith spacing"}],
            },
        },
        {
            "type": "user_message",
            "payload": {"content": [{"text": "second exact prompt"}]},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "legacy event prompt\n"},
        },
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "large answer to remove"}]}},
        {"type": "world_state", "payload": {"full": True, "state": {"goal": "keep"}}},
        {
            "type": "compacted",
            "payload": {
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "first exact prompt checkpoint"}],
                    },
                    {"type": "compaction", "encrypted_content": "encrypted-checkpoint"},
                ]
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "third prompt after checkpoint"}],
            },
        },
    ]
    write_jsonl(source_path, records)

    source_scan = scan_rollout(source_path)
    create_slim_view(source_path, destination_path, expected_thread_id="thread-a")
    destination_scan = scan_rollout(destination_path)

    assert destination_scan.user_prompt_count == source_scan.user_prompt_count == 4
    assert destination_scan.user_prompt_sha256 == source_scan.user_prompt_sha256
    destination_text = destination_path.read_text(encoding="utf-8")
    assert "first exact prompt\\nwith spacing" in destination_text
    assert "second exact prompt" in destination_text
    assert "legacy event prompt\\n" in destination_text
    assert "third prompt after checkpoint" in destination_text
    assert "large answer to remove" not in destination_text


def test_user_prompt_hash_detects_reordering_and_duplicate_removal(tmp_path: Path) -> None:
    first_path = tmp_path / "first.jsonl"
    reordered_path = tmp_path / "reordered.jsonl"
    removed_duplicate_path = tmp_path / "removed-duplicate.jsonl"
    prompt_a = {"type": "event_msg", "payload": {"type": "user_message", "message": "same"}}
    prompt_b = {"type": "event_msg", "payload": {"type": "user_message", "message": "other"}}
    write_jsonl(first_path, [{"type": "session_meta", "payload": {"id": "thread-a"}}, prompt_a, prompt_b, prompt_a])
    write_jsonl(reordered_path, [{"type": "session_meta", "payload": {"id": "thread-a"}}, prompt_b, prompt_a, prompt_a])
    write_jsonl(removed_duplicate_path, [{"type": "session_meta", "payload": {"id": "thread-a"}}, prompt_a, prompt_b])

    first_scan = scan_rollout(first_path)
    reordered_scan = scan_rollout(reordered_path)
    removed_duplicate_scan = scan_rollout(removed_duplicate_path)

    assert first_scan.user_prompt_count == reordered_scan.user_prompt_count == 3
    assert first_scan.user_prompt_sha256 != reordered_scan.user_prompt_sha256
    assert removed_duplicate_scan.user_prompt_count == 2
    assert first_scan.user_prompt_sha256 != removed_duplicate_scan.user_prompt_sha256


def test_create_slim_view_preserves_image_only_user_prompt_record(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    destination_path = tmp_path / "rollout-thread-a.slim.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "https://example.com/prompt.png"}],
                },
            },
            valid_compacted(),
        ],
    )

    source_scan = scan_rollout(source_path)
    create_slim_view(source_path, destination_path, expected_thread_id="thread-a")
    destination_scan = scan_rollout(destination_path)

    assert source_scan.user_prompt_count == destination_scan.user_prompt_count == 1
    assert source_scan.user_prompt_sha256 == destination_scan.user_prompt_sha256
    assert "https://example.com/prompt.png" in destination_path.read_text(encoding="utf-8")


def test_user_prompt_contract_allows_only_appended_prompts_after_restart(tmp_path: Path) -> None:
    rollout_path = tmp_path / "rollout-thread-a.jsonl"
    baseline_records = [
        {"type": "session_meta", "payload": {"id": "thread-a"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "first"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "second"}},
    ]
    write_jsonl(rollout_path, baseline_records)
    baseline = scan_rollout(rollout_path)

    write_jsonl(
        rollout_path,
        [
            *baseline_records,
            {"type": "event_msg", "payload": {"type": "user_message", "message": "appended"}},
        ],
    )
    result = validate_user_prompt_contract(
        rollout_path,
        baseline.user_prompt_count,
        baseline.user_prompt_sha256,
        allow_appended=True,
    )
    assert result["appended_count"] == 1

    write_jsonl(
        rollout_path,
        [
            baseline_records[0],
            baseline_records[2],
            baseline_records[1],
            {"type": "event_msg", "payload": {"type": "user_message", "message": "appended"}},
        ],
    )
    with pytest.raises(RuntimeError, match="exact prefix"):
        validate_user_prompt_contract(
            rollout_path,
            baseline.user_prompt_count,
            baseline.user_prompt_sha256,
            allow_appended=True,
        )


def test_user_prompt_contract_requires_exact_match_during_offline_repair(tmp_path: Path) -> None:
    rollout_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        rollout_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "baseline"}},
        ],
    )
    baseline = scan_rollout(rollout_path)
    with rollout_path.open("ab") as target:
        target.write(b'{"type":"event_msg","payload":{"type":"user_message","message":"unexpected"}}\n')

    with pytest.raises(RuntimeError, match="differ from the baseline"):
        validate_user_prompt_contract(
            rollout_path,
            baseline.user_prompt_count,
            baseline.user_prompt_sha256,
            allow_appended=False,
        )


def test_in_place_repair_refuses_generated_view_that_drops_prompt_before_backup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "must not disappear"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "tool_search_output",
                    "tools_summary": {"omitted_tool_count": 1},
                },
            },
        ],
    )
    source_bytes = source_path.read_bytes()
    source_stat = source_path.stat()
    source_scan = scan_rollout(source_path)
    real_builder = thread_history_repair.create_compatibility_view

    def destructive_builder(source: Path, destination: Path, expected_thread_id: str):
        result = real_builder(source, destination, expected_thread_id)
        records = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
        write_jsonl(
            destination,
            [record for record in records if record.get("payload", {}).get("type") != "user_message"],
        )
        return result

    monkeypatch.setattr(thread_history_repair, "create_compatibility_view", destructive_builder)

    with pytest.raises(RuntimeError, match="changed user prompt text or order"):
        thread_history_repair.repair_rollout_compatibility_in_place(
            source_path=source_path,
            backup_root=tmp_path / "backup",
            expected_thread_id="thread-a",
            audited_size=source_stat.st_size,
            audited_mtime_ns=source_stat.st_mtime_ns,
            audited_sha256=source_scan.source_sha256,
        )

    assert source_path.read_bytes() == source_bytes
    assert not (tmp_path / "backup" / "thread-a" / source_path.name).exists()


def test_create_slim_view_migrates_incompatible_records_in_preserved_tail(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    destination_path = tmp_path / "rollout-thread-a.slim.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            valid_compacted(),
            {
                "type": "response_item",
                "payload": {
                    "type": "tool_search_output",
                    "call_id": "call-1",
                    "status": "completed",
                    "execution": "local",
                    "tools_summary": {"omitted_tool_count": 1},
                },
            },
            {"type": "event_msg", "payload": {"type": "thread_name_updated", "thread_name": "Renamed"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )

    create_slim_view(source_path, destination_path, expected_thread_id="thread-a")

    records = [json.loads(line) for line in destination_path.read_text(encoding="utf-8").splitlines()]
    assert records[2]["payload"]["tools"] == []
    assert all(record.get("payload", {}).get("type") != "thread_name_updated" for record in records)
    assert scan_rollout(destination_path).estimated_current_parser_errors == 0


def test_create_slim_view_refuses_rollout_without_compaction(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )

    with pytest.raises(RuntimeError, match="compacted checkpoint"):
        create_slim_view(source_path, tmp_path / "view.jsonl", expected_thread_id="thread-a")


@pytest.mark.parametrize(
    "replacement_history,reason",
    [
        ([], "empty_replacement_history"),
        (["not-an-object"], "malformed_replacement_history"),
        ([{"type": "message", "role": "user", "content": []}], "invalid_replacement_history_item"),
    ],
)
def test_create_slim_view_refuses_unreplayable_checkpoint(
    tmp_path: Path,
    replacement_history: list[object],
    reason: str,
) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {"type": "compacted", "payload": {"replacement_history": replacement_history}},
        ],
    )

    with pytest.raises(RuntimeError, match=reason):
        create_slim_view(source_path, tmp_path / "view.jsonl", expected_thread_id="thread-a")


def test_create_views_refuse_missing_initial_session_meta(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        source_path,
        [
            valid_compacted(),
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )

    with pytest.raises(RuntimeError, match="initial session_meta"):
        create_slim_view(source_path, tmp_path / "slim.jsonl", expected_thread_id="thread-a")
    with pytest.raises(RuntimeError, match="initial session_meta"):
        create_compatibility_view(source_path, tmp_path / "compat.jsonl", expected_thread_id="thread-a")


def test_select_repair_candidates_requires_risk_and_usable_checkpoint() -> None:
    thresholds = RepairThresholds(max_active_bytes=100, max_active_lines=10, max_tail_lines=5)
    rows = [
        {
            "id": "large-safe",
            "archived": 0,
            "scan": {
                "total_bytes": 101,
                "line_count": 11,
                "latest_compacted_line": 9,
                "latest_compacted_checkpoint_valid": True,
                "tail_line_count": 3,
                "parse_errors": 0,
                "estimated_current_parser_errors": 0,
            },
        },
        {
            "id": "schema-risk",
            "archived": 0,
            "scan": {
                "total_bytes": 50,
                "line_count": 8,
                "latest_compacted_line": 7,
                "latest_compacted_checkpoint_valid": True,
                "tail_line_count": 2,
                "parse_errors": 0,
                "estimated_current_parser_errors": 2,
            },
        },
        {
            "id": "no-checkpoint",
            "archived": 0,
            "scan": {
                "total_bytes": 1000,
                "line_count": 100,
                "latest_compacted_line": 0,
                "latest_compacted_checkpoint_valid": False,
                "tail_line_count": 100,
                "parse_errors": 0,
                "estimated_current_parser_errors": 0,
            },
        },
        {
            "id": "archived",
            "archived": 1,
            "scan": {
                "total_bytes": 1000,
                "line_count": 100,
                "latest_compacted_line": 99,
                "latest_compacted_checkpoint_valid": True,
                "tail_line_count": 2,
                "parse_errors": 0,
                "estimated_current_parser_errors": 0,
            },
        },
    ]

    selected, blocked = select_repair_candidates(rows, thresholds=thresholds, include_archived=False)

    assert [row["id"] for row in selected] == ["large-safe", "schema-risk"]
    assert {row["id"]: row["repair_block_reason"] for row in blocked} == {
        "no-checkpoint": "missing_compacted_checkpoint"
    }


def test_build_repair_plan_separates_performance_compatibility_and_blocked() -> None:
    thresholds = RepairThresholds(max_active_bytes=100, max_active_lines=10, max_tail_lines=5)
    rows = [
        {
            "id": "large-safe",
            "archived": 0,
            "scan": {
                "session_meta_id": "large-safe",
                "total_bytes": 101,
                "line_count": 11,
                "latest_compacted_line": 9,
                "latest_compacted_checkpoint_valid": True,
                "tail_line_count": 3,
                "parse_errors": 0,
                "estimated_current_parser_errors": 2,
            },
        },
        {
            "id": "compat-only",
            "archived": 0,
            "scan": {
                "session_meta_id": "compat-only",
                "total_bytes": 50,
                "line_count": 8,
                "latest_compacted_line": 0,
                "latest_compacted_checkpoint_valid": False,
                "tail_line_count": 8,
                "parse_errors": 0,
                "estimated_current_parser_errors": 1,
            },
        },
        {
            "id": "large-no-checkpoint",
            "archived": 0,
            "scan": {
                "session_meta_id": "large-no-checkpoint",
                "total_bytes": 101,
                "line_count": 11,
                "latest_compacted_line": 0,
                "latest_compacted_checkpoint_valid": False,
                "tail_line_count": 11,
                "parse_errors": 0,
                "estimated_current_parser_errors": 1,
            },
        },
        {
            "id": "wrong-id",
            "archived": 0,
            "scan": {
                "session_meta_id": "another-thread",
                "total_bytes": 50,
                "line_count": 8,
                "latest_compacted_line": 0,
                "latest_compacted_checkpoint_valid": False,
                "tail_line_count": 8,
                "parse_errors": 0,
                "estimated_current_parser_errors": 1,
            },
        },
        {
            "id": "shared",
            "archived": 0,
            "shared_rollout_with": "another-thread",
            "scan": {
                "session_meta_id": "shared",
                "total_bytes": 50,
                "line_count": 8,
                "latest_compacted_line": 0,
                "latest_compacted_checkpoint_valid": False,
                "tail_line_count": 8,
                "parse_errors": 0,
                "estimated_current_parser_errors": 1,
            },
        },
    ]

    performance, compatibility, blocked = build_repair_plan(rows, thresholds=thresholds)

    assert [row["id"] for row in performance] == ["large-safe"]
    assert [row["id"] for row in compatibility] == ["compat-only"]
    assert {row["id"]: row["repair_block_reason"] for row in blocked} == {
        "large-no-checkpoint": "missing_compacted_checkpoint",
        "wrong-id": "session_meta_id_mismatch",
        "shared": "shared_rollout_path",
    }


def test_repair_rollout_in_place_archives_exact_original_and_installs_view(tmp_path: Path) -> None:
    source_path = tmp_path / "sessions" / "rollout-thread-a.jsonl"
    source_path.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": "thread-a"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": []}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "removable pre-checkpoint answer"}],
            },
        },
        {"type": "world_state", "payload": {"full": True, "state": {"goal": "keep"}}},
        valid_compacted(),
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
    ]
    write_jsonl(source_path, records)
    original_bytes = source_path.read_bytes()
    source_stat = source_path.stat()
    audited_sha256 = scan_rollout(source_path).source_sha256

    result = repair_rollout_in_place(
        source_path=source_path,
        backup_root=tmp_path / "backup",
        expected_thread_id="thread-a",
        audited_size=source_stat.st_size,
        audited_mtime_ns=source_stat.st_mtime_ns,
        audited_sha256=audited_sha256,
    )

    backup_path = Path(result.backup_path)
    assert backup_path.read_bytes() == original_bytes
    assert result.original_sha256 == result.backup_sha256
    assert result.active_sha256 != result.original_sha256
    assert result.original_bytes == len(original_bytes)
    assert result.active_bytes == source_path.stat().st_size
    assert scan_rollout(source_path).parse_errors == 0
    assert scan_rollout(source_path).line_count == 5
    assert "removable pre-checkpoint answer" not in source_path.read_text(encoding="utf-8")


def test_repair_rollout_in_place_refuses_source_changed_since_audit(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            valid_compacted(),
        ],
    )
    source_stat = source_path.stat()
    audited_sha256 = scan_rollout(source_path).source_sha256
    with source_path.open("ab") as target:
        target.write(b'{"type":"event_msg","payload":{"type":"turn_started"}}\n')

    with pytest.raises(RuntimeError, match="changed since audit"):
        repair_rollout_in_place(
            source_path=source_path,
            backup_root=tmp_path / "backup",
            expected_thread_id="thread-a",
            audited_size=source_stat.st_size,
            audited_mtime_ns=source_stat.st_mtime_ns,
            audited_sha256=audited_sha256,
        )


def test_repair_rollout_in_place_refuses_same_size_same_mtime_hash_change(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {"type": "compacted", "payload": {"replacement_history": ["a"]}},
        ],
    )
    source_stat = source_path.stat()
    audited_sha256 = scan_rollout(source_path).source_sha256
    mutated = source_path.read_bytes().replace(b'"a"', b'"b"')
    source_path.write_bytes(mutated)
    os.utime(source_path, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))

    with pytest.raises(RuntimeError, match="hash changed since audit"):
        repair_rollout_in_place(
            source_path=source_path,
            backup_root=tmp_path / "backup",
            expected_thread_id="thread-a",
            audited_size=source_stat.st_size,
            audited_mtime_ns=source_stat.st_mtime_ns,
            audited_sha256=audited_sha256,
        )


def test_repair_rollout_in_place_runs_guard_before_commit_and_keeps_original_on_failure(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            valid_compacted(),
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )
    original_bytes = source_path.read_bytes()
    source_stat = source_path.stat()
    audited_sha256 = scan_rollout(source_path).source_sha256

    def refuse_commit() -> None:
        raise RuntimeError("Codex restarted")

    with pytest.raises(RuntimeError, match="Codex restarted"):
        repair_rollout_in_place(
            source_path=source_path,
            backup_root=tmp_path / "backup",
            expected_thread_id="thread-a",
            audited_size=source_stat.st_size,
            audited_mtime_ns=source_stat.st_mtime_ns,
            audited_sha256=audited_sha256,
            precommit_guard=refuse_commit,
        )

    assert source_path.read_bytes() == original_bytes
    assert (tmp_path / "backup" / "thread-a" / source_path.name).read_bytes() == original_bytes
    assert list(source_path.parent.glob("*.repairing")) == []
    aborted_views = list((tmp_path / "backup" / "_aborted_views").glob("*.repairing"))
    assert len(aborted_views) == 1


def test_repair_rollout_rechecks_identity_and_hash_after_before_replace_callback(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            valid_compacted(),
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )
    source_stat = source_path.stat()
    audited_sha256 = scan_rollout(source_path).source_sha256

    def mutate_same_size_and_mtime(_journal: dict[str, object]) -> None:
        original = source_path.read_bytes()
        mutated = original.replace(b"checkpoint summary", b"checkpoint changed")
        assert len(mutated) == len(original)
        source_path.write_bytes(mutated)
        os.utime(source_path, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))

    with pytest.raises(RuntimeError, match="hash changed since audit"):
        repair_rollout_in_place(
            source_path=source_path,
            backup_root=tmp_path / "backup",
            expected_thread_id="thread-a",
            audited_size=source_stat.st_size,
            audited_mtime_ns=source_stat.st_mtime_ns,
            audited_sha256=audited_sha256,
            before_replace=mutate_same_size_and_mtime,
        )

    assert b"checkpoint changed" in source_path.read_bytes()


def test_create_compatibility_view_preserves_history_and_migrates_legacy_records(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    destination_path = tmp_path / "rollout-thread-a.compat.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": []}},
            {
                "type": "response_item",
                "payload": {
                    "type": "tool_search_output",
                    "call_id": "call-1",
                    "status": "completed",
                    "execution": "local",
                    "tools_summary": {"omitted_tool_count": 4},
                },
            },
            {"type": "event_msg", "payload": {"type": "thread_name_updated", "thread_name": "Renamed"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        ],
    )

    result = create_compatibility_view(source_path, destination_path, expected_thread_id="thread-a")
    records = [json.loads(line) for line in destination_path.read_text(encoding="utf-8").splitlines()]

    assert result.migrated_tools_summary_count == 1
    assert result.removed_thread_name_updated_count == 1
    assert result.source_line_count == 5
    assert result.view_line_count == 4
    migrated_payload = records[2]["payload"]
    assert migrated_payload["tools"] == []
    assert migrated_payload["tools_summary"] == {"omitted_tool_count": 4}
    assert [record["type"] for record in records] == [
        "session_meta",
        "response_item",
        "response_item",
        "response_item",
    ]
    migrated_scan = scan_rollout(destination_path)
    assert migrated_scan.estimated_current_parser_errors == 0
    assert migrated_scan.parse_errors == 0


def test_repair_rollout_compatibility_in_place_archives_original(tmp_path: Path) -> None:
    source_path = tmp_path / "rollout-thread-a.jsonl"
    write_jsonl(
        source_path,
        [
            {"type": "session_meta", "payload": {"id": "thread-a"}},
            {"type": "event_msg", "payload": {"type": "thread_name_updated", "thread_name": "Renamed"}},
        ],
    )
    original_bytes = source_path.read_bytes()
    source_stat = source_path.stat()
    audited_sha256 = scan_rollout(source_path).source_sha256

    result = repair_rollout_compatibility_in_place(
        source_path=source_path,
        backup_root=tmp_path / "backup",
        expected_thread_id="thread-a",
        audited_size=source_stat.st_size,
        audited_mtime_ns=source_stat.st_mtime_ns,
        audited_sha256=audited_sha256,
    )

    assert Path(result.backup_path).read_bytes() == original_bytes
    assert result.backup_sha256 == result.original_sha256
    assert scan_rollout(source_path).estimated_current_parser_errors == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths are platform-specific")
def test_repair_journal_atomic_write_supports_long_temporary_path(tmp_path: Path) -> None:
    parent = tmp_path / ("backup-" + "a" * 90) / ("thread-" + "b" * 45)
    windows_extended_path(parent).mkdir(parents=True)
    journal_path = parent / ("rollout-" + "c" * 45 + ".jsonl.repair-journal.json")
    temporary_length = len(str(journal_path)) + 1 + 32 + len(".writing")
    assert temporary_length > 260

    thread_history_repair._write_json_fsync(journal_path, {"status": "prepared"})

    durable_path = windows_extended_path(journal_path)
    assert json.loads(durable_path.read_text(encoding="utf-8")) == {"status": "prepared"}
