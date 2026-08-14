from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from backend import codex_data as codex_data_module
from backend import prompt_index as prompt_index_module
from backend.codex_data import read_thread_prompt_page, read_thread_timeline
from backend.prompt_index import prompt_index_database_path


def create_test_home(root_path: Path, records: list[dict[str, object]]) -> tuple[Path, Path]:
    codex_home_path = root_path / "codex-home"
    sessions_path = codex_home_path / "sessions"
    sessions_path.mkdir(parents=True)
    rollout_path = sessions_path / "rollout-thread-1.jsonl"
    with rollout_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(
            json.dumps(
                {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(root_path / "project")}},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    with closing(sqlite3.connect(codex_home_path / "state_5.sqlite")) as connection, connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT)")
        connection.execute(
            "INSERT INTO threads(id, rollout_path, cwd, title) VALUES (?, ?, ?, ?)",
            ("thread-1", str(rollout_path), str(root_path / "project"), "Regression test"),
        )
    return codex_home_path, rollout_path


def message_record(text: str, *, role: str = "user") -> dict[str, object]:
    content_type = "input_text" if role == "user" else "output_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": content_type, "text": text}],
        },
    }


@pytest.fixture(autouse=True)
def isolate_prompt_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME_MANAGER_PROMPT_INDEX_ROOT", str(tmp_path / "prompt-indexes"))


def test_svg_and_xml_data_url_bodies_never_reach_timeline_or_prompt_index(tmp_path: Path) -> None:
    svg_text = (
        'before data:image/svg+xml,<svg viewBox="0 0 10 10">'
        '<text fill="red">SVG_BODY_SECRET</text></svg> after-svg'
    )
    xml_text = (
        'before data:text/xml,<?xml version="1.0"?>'
        '<root attr="a b"><child>XML_BODY_SECRET</child></root> after-xml'
    )
    default_media_text = 'before data:,<svg aria-label="a b"><text>DEFAULT_BODY_SECRET</text></svg> after-default'
    codex_home_path, _ = create_test_home(
        tmp_path,
        [message_record(svg_text), message_record(xml_text), message_record(default_media_text)],
    )

    timeline_page = read_thread_timeline(
        str(codex_home_path),
        "thread-1",
        kind_filter="conversation",
        limit=20,
    )
    prompt_page = read_thread_prompt_page(
        str(codex_home_path),
        "thread-1",
        scope="all",
        limit=20,
        scan_budget_ms=5_000,
    )
    serialized_results = json.dumps(
        {"timeline": timeline_page, "prompts": prompt_page},
        ensure_ascii=False,
    )

    assert "SVG_BODY_SECRET" not in serialized_results
    assert "XML_BODY_SECRET" not in serialized_results
    assert "DEFAULT_BODY_SECRET" not in serialized_results
    assert "<svg" not in serialized_results
    assert "<root" not in serialized_results
    assert serialized_results.count("[附件内容已隐藏]") >= 6
    assert "after-svg" in json.dumps(timeline_page, ensure_ascii=False)
    assert "after-xml" in json.dumps(timeline_page, ensure_ascii=False)
    assert "after-default" in json.dumps(timeline_page, ensure_ascii=False)

    for secret in ("SVG_BODY_SECRET", "XML_BODY_SECRET", "DEFAULT_BODY_SECRET"):
        search_page = read_thread_prompt_page(
            str(codex_home_path),
            "thread-1",
            scope="all",
            search=secret,
            limit=20,
            scan_budget_ms=5_000,
        )
        assert search_page["matchCount"] == 0

    database_path = prompt_index_database_path(codex_home_path)
    derived_bytes = b""
    for path in (
        database_path,
        database_path.with_name(database_path.name + "-wal"),
        database_path.with_name(database_path.name + "-shm"),
    ):
        if path.exists():
            derived_bytes += path.read_bytes()
    assert b"SVG_BODY_SECRET" not in derived_bytes
    assert b"XML_BODY_SECRET" not in derived_bytes
    assert b"DEFAULT_BODY_SECRET" not in derived_bytes


def test_database_read_never_waits_for_file_lock_while_holding_registry_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "prompt-indexes" / "index.sqlite"
    database_path.parent.mkdir(parents=True)
    original_lock_database_file = prompt_index_module._lock_database_file
    observed_calls = 0

    def checked_lock_database_file(lock_file, *, blocking: bool, exclusive: bool = True) -> bool:
        nonlocal observed_calls
        observed_calls += 1
        registry_available = prompt_index_module._database_locks_lock.acquire(blocking=False)
        assert registry_available, "file locking must happen outside the global database registry lock"
        prompt_index_module._database_locks_lock.release()
        return original_lock_database_file(lock_file, blocking=blocking, exclusive=exclusive)

    monkeypatch.setattr(prompt_index_module, "_lock_database_file", checked_lock_database_file)

    with prompt_index_module._database_read(database_path):
        assert prompt_index_module._database_active_count(database_path) == 1

    assert observed_calls == 1
    assert prompt_index_module._database_active_count(database_path) == 0


def test_waiting_writer_cannot_block_reader_from_releasing_shared_lock(tmp_path: Path) -> None:
    database_path = tmp_path / "prompt-indexes" / "index.sqlite"
    database_path.parent.mkdir(parents=True)
    reader_ready = threading.Event()
    release_reader = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()

    def reader() -> None:
        with prompt_index_module._database_read(database_path):
            reader_ready.set()
            release_reader.wait(timeout=2)

    def writer() -> None:
        reader_ready.wait(timeout=2)
        writer_started.set()
        with prompt_index_module._database_use(database_path):
            writer_finished.set()

    reader_thread = threading.Thread(target=reader, daemon=True)
    writer_thread = threading.Thread(target=writer, daemon=True)
    reader_thread.start()
    assert reader_ready.wait(timeout=2)
    writer_thread.start()
    assert writer_started.wait(timeout=2)
    time.sleep(0.05)
    release_reader.set()
    reader_thread.join(timeout=2)
    writer_thread.join(timeout=2)

    assert not reader_thread.is_alive()
    assert not writer_thread.is_alive()
    assert writer_finished.is_set()


@pytest.mark.parametrize(
    ("source_text", "query"),
    [
        ("un café noir", "cafe"),
        ("die Straße ist lang", "strasse"),
        ("symbol Σ in output", "σ"),
    ],
)
def test_byte_cursor_unicode_prefilter_matches_without_parsing_ascii_noise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_text: str,
    query: str,
) -> None:
    records = [message_record(source_text, role="assistant")]
    records.extend(message_record(f"irrelevant ascii row {index}", role="assistant") for index in range(1_000))
    codex_home_path, _ = create_test_home(tmp_path, records)
    original_json_loads = codex_data_module.json.loads
    parsed_record_count = 0

    def counted_json_loads(value, *args, **kwargs):
        nonlocal parsed_record_count
        parsed_record_count += 1
        return original_json_loads(value, *args, **kwargs)

    monkeypatch.setattr(codex_data_module.json, "loads", counted_json_loads)

    page = read_thread_timeline(
        str(codex_home_path),
        "thread-1",
        kind_filter="conversation",
        search_text=query,
        limit=20,
        scan_record_limit=2_000,
        scan_byte_limit=64 * 1024 * 1024,
    )

    assert [item["text"] for item in page["items"]] == [source_text]
    assert parsed_record_count == 1
