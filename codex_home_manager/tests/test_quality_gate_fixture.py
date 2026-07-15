from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from backend.codex_data import build_snapshot
from scripts.quality_gate import (
    create_quality_gate_codex_home,
    quality_gate_subagent_thread_count,
    quality_gate_thread_count,
)


def test_quality_gate_fixture_is_complete_and_isolated(tmp_path: Path) -> None:
    codex_home_path = create_quality_gate_codex_home(tmp_path)

    with sqlite3.connect(codex_home_path / "state_5.sqlite") as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone() == (quality_gate_thread_count,)
        assert connection.execute("SELECT COUNT(*) FROM thread_spawn_edges").fetchone() == (
            quality_gate_subagent_thread_count,
        )
    with sqlite3.connect(codex_home_path / "logs_2.sqlite") as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM logs").fetchone() == (
            quality_gate_thread_count * 3,
        )

    snapshot = build_snapshot(str(codex_home_path), sidebar_limit=50)
    assert len(snapshot["threads"]) == quality_gate_thread_count
    assert len(snapshot["projects"]) == 1
    assert any(thread["threadKind"] == "main" for thread in snapshot["threads"])
    assert any(thread["threadKind"] == "subagent" for thread in snapshot["threads"])
    main_threads = [thread for thread in snapshot["threads"] if thread["threadKind"] == "main"]
    assert main_threads and all(thread["codexVisible"] for thread in main_threads)
    assert all(Path(thread["rolloutPath"]).is_file() for thread in snapshot["threads"])

    global_state = json.loads((codex_home_path / ".codex-global-state.json").read_text(encoding="utf-8"))
    assert len(global_state["thread-workspace-root-hints"]) == quality_gate_thread_count
