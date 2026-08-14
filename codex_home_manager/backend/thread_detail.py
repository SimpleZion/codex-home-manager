from __future__ import annotations

from typing import Any, Callable


ThreadMetadataBuilder = Callable[[dict[str, Any], dict[str, str] | None], dict[str, Any]]
ThreadRecordBuilder = Callable[[dict[str, Any], dict[str, str] | None], dict[str, Any]]


def assemble_thread_detail_tree(
    thread_id: str,
    rows: list[dict[str, Any]],
    spawn_edges: dict[str, dict[str, str]],
    build_metadata: ThreadMetadataBuilder,
    build_record: ThreadRecordBuilder,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    rows_by_id = {str(row["id"]): row for row in rows}
    root_row = rows_by_id.get(thread_id)
    if root_row is None:
        raise KeyError(thread_id)

    metadata_by_id = {
        current_thread_id: build_metadata(row, spawn_edges.get(current_thread_id))
        for current_thread_id, row in rows_by_id.items()
    }
    child_ids_by_parent_id: dict[str, list[str]] = {}
    for current_thread_id, metadata in metadata_by_id.items():
        parent_thread_id = str(metadata.get("parentThreadId") or "")
        if parent_thread_id and parent_thread_id in rows_by_id:
            child_ids_by_parent_id.setdefault(parent_thread_id, []).append(current_thread_id)

    related_ids: list[str] = []
    visited: set[str] = {thread_id}

    def visit(parent_thread_id: str) -> None:
        for child_thread_id in child_ids_by_parent_id.get(parent_thread_id, []):
            if child_thread_id in visited:
                continue
            visited.add(child_thread_id)
            related_ids.append(child_thread_id)
            visit(child_thread_id)

    visit(thread_id)
    record_ids = [thread_id, *related_ids]
    records_by_id = {
        current_thread_id: build_record(rows_by_id[current_thread_id], spawn_edges.get(current_thread_id))
        for current_thread_id in record_ids
    }
    aggregate_cache: dict[str, dict[str, int]] = {}

    def aggregate_descendants(current_thread_id: str, active_path: set[str] | None = None) -> dict[str, int]:
        if current_thread_id in aggregate_cache:
            return aggregate_cache[current_thread_id]
        visiting = set() if active_path is None else active_path
        if current_thread_id in visiting:
            return {"count": 0, "fileSizeBytes": 0, "tokensUsed": 0}
        visiting.add(current_thread_id)
        aggregate = {"count": 0, "fileSizeBytes": 0, "tokensUsed": 0}
        for child_thread_id in child_ids_by_parent_id.get(current_thread_id, []):
            if child_thread_id in visiting:
                continue
            child_record = records_by_id.get(child_thread_id)
            if child_record is None:
                continue
            nested = aggregate_descendants(child_thread_id, visiting)
            aggregate["count"] += 1 + nested["count"]
            aggregate["fileSizeBytes"] += int(child_record.get("fileSizeBytes") or 0) + nested["fileSizeBytes"]
            aggregate["tokensUsed"] += int(child_record.get("tokensUsed") or 0) + nested["tokensUsed"]
        visiting.remove(current_thread_id)
        aggregate_cache[current_thread_id] = aggregate
        return aggregate

    for current_thread_id, record in records_by_id.items():
        aggregate = aggregate_descendants(current_thread_id)
        record["childThreadCount"] = aggregate["count"]
        record["childFileSizeBytes"] = aggregate["fileSizeBytes"]
        record["totalFileSizeBytes"] = int(record.get("fileSizeBytes") or 0) + aggregate["fileSizeBytes"]
        record["childTokensUsed"] = aggregate["tokensUsed"]
        record["totalTokensUsed"] = int(record.get("tokensUsed") or 0) + aggregate["tokensUsed"]

    return records_by_id[thread_id], [records_by_id[current_thread_id] for current_thread_id in related_ids], root_row
