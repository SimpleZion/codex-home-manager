"""Compare a chat's persisted prompt matches with bounded, read-only source parsing."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.codex_data import (classify_prompt_record,
    prompt_is_cross_protocol_duplicate, prompt_text_from_item, prompt_timestamp_ms,
    recent_prompt_candidate_line, rollout_session_meta_id, timeline_outer_type_pattern)
from backend.prompt_index import prompt_index_database_path, redacted_jsonl_record_bytes
from backend.search_normalization import normalize_search_text


def source_candidates(path: Path, snapshot_end: int, guard):
    # Scan forward so protocol mirrors retain the same canonical offset as the index.
    with path.open("rb") as stream:
        line_number = 0
        while stream.tell() < snapshot_end:
            start = stream.tell()
            head = stream.readline(min(64 * 1024, snapshot_end - start))
            outer = timeline_outer_type_pattern.search(head[:4096])
            candidate = bool(outer and outer.group(1) in {b"user_message", b"event_msg", b"response_item"}
                             and recent_prompt_candidate_line(head[:4096]))
            chunks = [head] if candidate else []
            buffered = len(head) if candidate else 0
            chunk = head
            while chunk and not chunk.endswith(b"\n") and stream.tell() < snapshot_end:
                guard()
                chunk = stream.readline(min(512 * 1024, snapshot_end - stream.tell()))
                if candidate and buffered <= 8 * 1024**2:
                    chunks.append(chunk)
                    buffered += len(chunk)
                    if buffered > 8 * 1024**2:
                        chunks.clear()
            end = stream.tell()
            line_number += 1
            guard()
            if not candidate:
                continue
            if buffered > 8 * 1024**2:
                raw = redacted_jsonl_record_bytes(stream, start, end, None, include_truncation_metadata=True)
                stream.seek(end)
            else:
                raw = b"".join(chunks)
                if b"data:" in raw.lower():
                    raw = redacted_jsonl_record_bytes(stream, start, end, None)
                    stream.seek(end)
            if raw is not None:
                yield start, line_number, raw


def probe(home: Path, thread: str, search: str, max_seconds: float, max_bytes: int) -> dict:
    started = time.monotonic()
    database = prompt_index_database_path(home)
    normalized_search = normalize_search_text(search)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        files = connection.execute("SELECT id,path,scanned_offset FROM prompt_files WHERE path LIKE ?", (f"%{thread}%",)).fetchall()
        if not files:
            raise RuntimeError("No indexed source segments found for the target chat")
        expected = {file_id: {row[0] for row in connection.execute(
            "SELECT byte_offset FROM prompts WHERE file_id=? AND has_pure_text=1 AND instr(pure_search_text,?)>0",
            (file_id, normalized_search))} for file_id, _, _ in files}
    total_bytes = sum(offset for _, _, offset in files)
    if total_bytes > max_bytes:
        raise RuntimeError("Source snapshot exceeds the explicit byte budget")
    result = {"threadId": thread, "search": search, "snapshotBytes": total_bytes, "segments": []}
    process = psutil.Process()
    peak_private = 0
    for file_id, path_text, snapshot_end in files:
        path = Path(path_text)
        if rollout_session_meta_id(path) != thread:
            raise RuntimeError("Source segment identity does not match the requested chat")
        recent = []
        actual = set()
        prompt_count = 0
        pure_count = 0
        invalid = 0
        last_guard = 0.0
        def guard():
            nonlocal last_guard, peak_private
            now = time.monotonic()
            if now - last_guard >= 0.5:
                last_guard = now
                peak_private = max(peak_private, process.memory_info().private)
                if now - started > max_seconds:
                    raise RuntimeError(f"Source probe exceeded {max_seconds:g}s in {path.name}")
                available = psutil.virtual_memory().available
                if available < 2 * 1024**3 or peak_private > 384 * 1024**2:
                    raise RuntimeError(f"Source probe memory guard: available={available}, private={peak_private}")
        for offset, line_position, raw in source_candidates(path, snapshot_end, guard):
            try:
                item = json.loads(raw)
                extracted = prompt_text_from_item(item)
            except (ValueError, TypeError):
                invalid += 1
                continue
            if not extracted:
                continue
            text, protocol = extracted
            text = text.strip()
            if not text:
                continue
            classification = classify_prompt_record(text)
            canonical = classification["pureText"] if classification["hasPureText"] else text
            if prompt_is_cross_protocol_duplicate(canonical, item.get("timestamp"), protocol, line_position, recent):
                continue
            recent.append((canonical, prompt_timestamp_ms(item.get("timestamp")), protocol, line_position))
            recent = recent[-8:]
            prompt_count += 1
            pure_count += int(classification["hasPureText"])
            if classification["hasPureText"] and normalized_search in normalize_search_text(classification["pureText"]):
                actual.add(offset)
        missing = sorted(actual - expected[file_id])
        extra = sorted(expected[file_id] - actual)
        result["segments"].append({"fileId": file_id, "path": path_text, "snapshotEnd": snapshot_end,
            "sourcePromptCount": prompt_count, "sourcePureCount": pure_count, "sourceMatchCount": len(actual),
            "indexedMatchCount": len(expected[file_id]), "missingOffsets": missing, "extraOffsets": extra,
            "invalidCandidates": invalid})
        print(json.dumps({"segmentComplete": file_id, "matches": len(actual), "missing": len(missing),
                          "extra": len(extra), "elapsedSeconds": round(time.monotonic() - started, 3)}), flush=True)
    result.update(passed=all(not segment["missingOffsets"] and not segment["extraOffsets"] and not segment["invalidCandidates"] for segment in result["segments"]),
        elapsedSeconds=round(time.monotonic() - started, 3), peakPrivateBytes=peak_private)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--search", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=float, default=90)
    parser.add_argument("--max-bytes", type=int, default=16 * 1024**3)
    arguments = parser.parse_args()
    report = probe(arguments.home, arguments.thread, arguments.search, arguments.max_seconds, arguments.max_bytes)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)
