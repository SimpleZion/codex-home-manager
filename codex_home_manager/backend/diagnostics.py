from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from .codex_data import (
    active_toml_table_block_ranges,
    backup_root_path,
    build_snapshot,
    connect_database,
    collect_jsonl_schema_stats,
    detect_codex_processes,
    explicit_sidebar_thread_ids_from_state,
    is_compacted_item,
    read_session_index_records,
    read_global_state,
    resolve_codex_paths,
    rollout_display_integrity,
    stat_file,
)
from .hidden_process import run_hidden_command
from .process_utils import list_windows_processes
from .windows_paths import windows_path_key


severity_rank = {"critical": 0, "warning": 1, "info": 2, "pass": 3}
diagnostics_runtime_cache_lock = threading.RLock()
diagnostics_runtime_cache_epoch = 0
mcp_process_snapshot_cache: tuple[int, float, dict[str, Any]] | None = None
curated_plugin_registry_cache: dict[tuple[str, str], tuple[int, float, dict[str, Any]]] = {}
current_codex_appx_install_cache: tuple[int, float, dict[str, Any]] | None = None
stale_restore_artifact_pattern = re.compile(r"^\..+\.[0-9a-fA-F]{32}\.restoring$")
capacity_trend_lock = threading.RLock()
capacity_trend_schema_version = 1
capacity_trend_max_age_days = 90
capacity_trend_max_snapshots = 90
capacity_trend_max_file_bytes = 128 * 1024
capacity_trend_metric_defaults: dict[str, int | bool] = {
    "sessionsBytes": 0,
    "largeThreadCount": 0,
    "backupBytes": 0,
    "backupFileCount": 0,
    "backupScanTruncated": False,
    "mcpProcessCount": 0,
    "normalNodeReplProcessCount": 0,
    "nodeReplRiskProcessCount": 0,
    "legacyFallbackProcessCount": 0,
    "xcodebuildProcessCount": 0,
    "otherMcpServerProcessCount": 0,
}
capacity_trend_direction_metrics = (
    "sessionsBytes",
    "largeThreadCount",
    "backupBytes",
    "backupFileCount",
    "mcpProcessCount",
)


def clear_diagnostics_runtime_caches() -> None:
    global current_codex_appx_install_cache
    global diagnostics_runtime_cache_epoch
    global mcp_process_snapshot_cache

    with diagnostics_runtime_cache_lock:
        diagnostics_runtime_cache_epoch += 1
        mcp_process_snapshot_cache = None
        curated_plugin_registry_cache.clear()
        current_codex_appx_install_cache = None


def localize(language: str, english_text: str, chinese_text: str) -> str:
    return chinese_text if language == "zh" else english_text


def normalize_language(language: str | None) -> str:
    normalized_language = (language or "zh").strip().lower().replace("_", "-")
    if normalized_language in {"en", "en-us", "en-gb"}:
        return "en"
    return "zh"


def capacity_trend_state_root() -> Path:
    configured_state_root = os.environ.get("CODEX_HOME_MANAGER_STATE_ROOT")
    if configured_state_root:
        return Path(configured_state_root).expanduser().resolve(strict=False)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data).expanduser().resolve(strict=False) / "CodexHomeManager" / "state"
    return Path.home().expanduser().resolve(strict=False) / ".codex-home-manager" / "state"


def capacity_trend_state_path(codex_home_path: Path, state_root: Path | None = None) -> Path:
    normalized_home = os.path.normcase(str(codex_home_path.expanduser().resolve(strict=False)))
    home_key = hashlib.sha256(normalized_home.encode("utf-8")).hexdigest()[:24]
    return (state_root or capacity_trend_state_root()) / f"capacity-trend-{home_key}.json"


def _sanitize_capacity_metrics(metrics: dict[str, Any]) -> dict[str, int | bool]:
    sanitized: dict[str, int | bool] = {}
    for metric_name, default_value in capacity_trend_metric_defaults.items():
        raw_value = metrics.get(metric_name, default_value)
        if isinstance(default_value, bool):
            sanitized[metric_name] = bool(raw_value)
            continue
        try:
            sanitized[metric_name] = max(0, int(raw_value or 0))
        except (TypeError, ValueError, OverflowError):
            sanitized[metric_name] = 0
    return sanitized


def _capacity_day_key(captured_at_ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(captured_at_ms / 1000))


def _read_capacity_trend_snapshots(state_path: Path) -> tuple[list[dict[str, Any]], str]:
    if not state_path.exists():
        return [], "missing"
    try:
        if state_path.stat().st_size > capacity_trend_max_file_bytes:
            return [], "corrupt"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schemaVersion") != capacity_trend_schema_version:
            return [], "corrupt"
        raw_snapshots = payload.get("snapshots")
        if not isinstance(raw_snapshots, list):
            return [], "corrupt"
        snapshots: list[dict[str, Any]] = []
        for raw_snapshot in raw_snapshots:
            if not isinstance(raw_snapshot, dict):
                continue
            try:
                captured_at_ms = max(0, int(raw_snapshot.get("capturedAtMs") or 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if not captured_at_ms:
                continue
            snapshots.append({"capturedAtMs": captured_at_ms, **_sanitize_capacity_metrics(raw_snapshot)})
        snapshots.sort(key=lambda item: int(item["capturedAtMs"]))
        return snapshots[-capacity_trend_max_snapshots:], "ok"
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return [], "corrupt"
    except OSError:
        return [], "read_failed"


def _write_capacity_trend_atomic(state_path: Path, snapshots: list[dict[str, Any]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": capacity_trend_schema_version,
        "retention": {
            "cadence": "daily",
            "maxAgeDays": capacity_trend_max_age_days,
            "maxSnapshots": capacity_trend_max_snapshots,
        },
        "snapshots": snapshots,
    }
    encoded_payload = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded_payload) > capacity_trend_max_file_bytes:
        raise ValueError("capacity trend payload exceeds the bounded state-file limit")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=state_path.parent,
            prefix=f".{state_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file_handle:
            temporary_path = Path(file_handle.name)
            file_handle.write(encoded_payload)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, state_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _capacity_metric_change(current_value: int, previous_value: int | None) -> dict[str, Any]:
    if previous_value is None:
        return {"direction": "unknown", "delta": 0, "percent": None}
    delta = current_value - previous_value
    direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
    percent = round(delta / previous_value * 100, 1) if previous_value else 0.0 if delta == 0 else None
    return {"direction": direction, "delta": delta, "percent": percent}


def record_capacity_trend(
    codex_home_path: Path,
    metrics: dict[str, Any],
    *,
    captured_at_ms: int | None = None,
    state_root: Path | None = None,
) -> dict[str, Any]:
    current_captured_at_ms = int(captured_at_ms if captured_at_ms is not None else time.time() * 1000)
    current_metrics = _sanitize_capacity_metrics(metrics)
    state_path = capacity_trend_state_path(codex_home_path, state_root=state_root)

    with capacity_trend_lock:
        snapshots, read_status = _read_capacity_trend_snapshots(state_path)
        current_day_key = _capacity_day_key(current_captured_at_ms)
        snapshots = [
            snapshot
            for snapshot in snapshots
            if _capacity_day_key(int(snapshot["capturedAtMs"])) != current_day_key
        ]
        snapshots.append({"capturedAtMs": current_captured_at_ms, **current_metrics})
        cutoff_ms = current_captured_at_ms - capacity_trend_max_age_days * 86_400_000
        snapshots = [
            snapshot
            for snapshot in snapshots
            if cutoff_ms <= int(snapshot["capturedAtMs"]) <= current_captured_at_ms
        ][-capacity_trend_max_snapshots:]
        persisted = False
        error_code = ""
        try:
            _write_capacity_trend_atomic(state_path, snapshots)
            persisted = True
        except (OSError, ValueError):
            error_code = "write_failed"

    previous_metrics = snapshots[-2] if len(snapshots) >= 2 else None
    changes = {
        metric_name: _capacity_metric_change(
            int(current_metrics[metric_name]),
            int(previous_metrics[metric_name]) if previous_metrics is not None else None,
        )
        for metric_name in capacity_trend_direction_metrics
    }
    storage = {
        "persisted": persisted,
        "recoveredFromCorruption": read_status == "corrupt" and persisted,
        "errorCode": error_code or (read_status if read_status == "read_failed" else ""),
    }
    if not storage["errorCode"]:
        storage.pop("errorCode")
    return {
        "schemaVersion": capacity_trend_schema_version,
        "retention": {
            "cadence": "daily",
            "maxAgeDays": capacity_trend_max_age_days,
            "maxSnapshots": capacity_trend_max_snapshots,
        },
        "storage": storage,
        "current": current_metrics,
        "changes": changes,
        "history": snapshots,
    }


def prompt_text(value: Any, max_length: int = 260) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_length:
        return text
    return f"{text[: max(0, max_length - 1)].rstrip()}…"


def prompt_list(values: list[Any], max_items: int = 5, max_length: int = 260) -> list[str]:
    lines: list[str] = []
    for value in values[:max_items]:
        text = prompt_text(value, max_length=max_length)
        if text:
            lines.append(text)
    if len(values) > max_items:
        lines.append(f"... {len(values) - max_items} more")
    return lines


def build_codex_repair_prompt(report: dict[str, Any], language: str) -> str:
    summary = report.get("summary", {})
    paths = report.get("paths", {})
    issues = list(report.get("issues", []))
    failed_checks = [check for check in report.get("checks", []) if check.get("status") != "pass"]
    issue_limit = 14
    check_limit = 10

    if language == "en":
        lines = [
            "You are Codex running on my own machine. Use this Codex Home Manager diagnostic report as the starting point, then verify the evidence locally before changing anything.",
            "",
            "Goal: diagnose and repair the reported Codex Desktop / CODEX_HOME problems. Do not rely only on this pasted report; inspect the real local files, SQLite databases, JSONL logs, plugin cache and running processes first.",
            "",
            "Operating rules:",
            "- Treat CODEX_HOME state files as high risk. Before writes, create a restorable backup and close Codex Desktop when possible.",
            "- Do not delete data permanently. Preserve rollout JSONL, state_5.sqlite, session_index.jsonl, config files and plugin-cache evidence unless I explicitly approve deletion.",
            "- Fix only issues that are supported by local evidence. If evidence is stale or contradictory, explain the blocker instead of guessing.",
            "- After each repair batch, rerun the relevant checks and report changed files, backup paths, commands run, remaining risks and anything that still needs my confirmation.",
            "",
            "Diagnostic summary:",
            f"- CODEX_HOME: {report.get('codexHome')}",
            f"- generatedAtMs: {report.get('generatedAtMs')}",
            f"- health score/status: {report.get('score')} / {report.get('status')}",
            f"- issue counts: critical={summary.get('critical')}, warning={summary.get('warning')}, info={summary.get('info')}",
            f"- checks: pass={summary.get('pass')} / total={summary.get('checks')}",
            f"- threadCount: {summary.get('threadCount')}",
            "",
            "Important paths:",
        ]
        for key, value in paths.items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "Issues to investigate and repair in priority order:"])
        if not issues:
            lines.append("- No critical/warning/info issues were reported. Still verify the checks before declaring the environment healthy.")
        for index, issue in enumerate(issues[:issue_limit], start=1):
            lines.append(f"{index}. [{issue.get('severity')}] {issue.get('title')} ({issue.get('id')}, {issue.get('category')})")
            lines.append(f"   Summary: {prompt_text(issue.get('summary'))}")
            lines.append(f"   Recommendation: {prompt_text(issue.get('recommendation'))}")
            for evidence in prompt_list(list(issue.get("evidence") or []), max_items=4):
                lines.append(f"   Evidence: {evidence}")
            for path in prompt_list(list(issue.get("affectedPaths") or []), max_items=4):
                lines.append(f"   Path: {path}")
        if len(issues) > issue_limit:
            lines.append(f"- The pasted report has {len(issues) - issue_limit} additional issues. Re-run diagnostics or request the full report before broad repair.")
        lines.extend(["", "Failed or attention checks:"])
        if not failed_checks:
            lines.append("- No failed checks were reported.")
        for index, check in enumerate(failed_checks[:check_limit], start=1):
            lines.append(f"{index}. [{check.get('status')}] {check.get('title')} ({check.get('id')}, {check.get('category')})")
            lines.append(f"   Summary: {prompt_text(check.get('summary'))}")
            for evidence in prompt_list(list(check.get("evidence") or []), max_items=3):
                lines.append(f"   Evidence: {evidence}")
        if len(failed_checks) > check_limit:
            lines.append(f"- The pasted report has {len(failed_checks) - check_limit} additional attention checks.")
        lines.extend(["", "Start by confirming the current CODEX_HOME and whether Codex Desktop is running, then propose or execute the safest evidence-backed repair path."])
        return "\n".join(lines).strip()

    lines = [
        "你是运行在我自己电脑上的 Codex。下面是 Codex Home Manager 的只读体检报告，请把它当作起点，但在修改任何东西之前必须先用本机真实证据复核。",
        "",
        "目标：诊断并修复报告中列出的 Codex Desktop / CODEX_HOME 问题。不要只凭这段报告下结论；先检查本机真实文件、SQLite 数据库、JSONL 日志、插件缓存和运行进程。",
        "",
        "执行边界：",
        "- CODEX_HOME 状态文件属于高风险对象。写入前先创建可回滚备份；能关闭 Codex Desktop 时先关闭。",
        "- 不要永久删除数据。除非我明确批准，否则保留 rollout JSONL、state_5.sqlite、session_index.jsonl、配置文件和插件缓存证据。",
        "- 只修复有本机证据支撑的问题。如果证据过期或相互矛盾，先说明阻塞点，不要猜。",
        "- 每批修复后重新运行相关检查，并汇报改动文件、备份位置、执行命令、剩余风险和仍需我确认的事项。",
        "",
        "体检摘要：",
        f"- CODEX_HOME：{report.get('codexHome')}",
        f"- 生成时间戳：{report.get('generatedAtMs')}",
        f"- 健康分/状态：{report.get('score')} / {report.get('status')}",
        f"- 问题数量：critical={summary.get('critical')}，warning={summary.get('warning')}，info={summary.get('info')}",
        f"- 检查项：通过={summary.get('pass')} / 总数={summary.get('checks')}",
        f"- 线程数：{summary.get('threadCount')}",
        "",
        "关键路径：",
    ]
    for key, value in paths.items():
        lines.append(f"- {key}：{value}")
    lines.extend(["", "需要按优先级核查并修复的问题："])
    if not issues:
        lines.append("- 体检没有报告 critical/warning/info 问题。仍然需要复核检查项后再判断环境健康。")
    for index, issue in enumerate(issues[:issue_limit], start=1):
        lines.append(f"{index}. [{issue.get('severity')}] {issue.get('title')}（{issue.get('id')}，{issue.get('category')}）")
        lines.append(f"   摘要：{prompt_text(issue.get('summary'))}")
        lines.append(f"   建议：{prompt_text(issue.get('recommendation'))}")
        for evidence in prompt_list(list(issue.get("evidence") or []), max_items=4):
            lines.append(f"   证据：{evidence}")
        for path in prompt_list(list(issue.get("affectedPaths") or []), max_items=4):
            lines.append(f"   路径：{path}")
    if len(issues) > issue_limit:
        lines.append(f"- 这份报告还有 {len(issues) - issue_limit} 个额外问题；做大范围修复前请重新运行体检或索取完整报告。")
    lines.extend(["", "未通过或需要关注的检查项："])
    if not failed_checks:
        lines.append("- 没有未通过检查项。")
    for index, check in enumerate(failed_checks[:check_limit], start=1):
        lines.append(f"{index}. [{check.get('status')}] {check.get('title')}（{check.get('id')}，{check.get('category')}）")
        lines.append(f"   摘要：{prompt_text(check.get('summary'))}")
        for evidence in prompt_list(list(check.get("evidence") or []), max_items=3):
            lines.append(f"   证据：{evidence}")
    if len(failed_checks) > check_limit:
        lines.append(f"- 这份报告还有 {len(failed_checks) - check_limit} 个额外关注检查项。")
    lines.extend(["", "请先确认当前 CODEX_HOME 和 Codex Desktop 是否正在运行，然后提出或直接执行最安全、证据充分的修复路径。"])
    return "\n".join(lines).strip()


def path_exists_text(path: Path) -> str:
    return f"{path} ({'exists' if path.exists() else 'missing'})"


def scan_stale_restore_artifacts(codex_home_path: Path) -> dict[str, Any]:
    artifacts: list[str] = []
    scan_errors: list[str] = []
    try:
        children = list(codex_home_path.iterdir()) if codex_home_path.is_dir() else []
    except OSError as error:
        children = []
        scan_errors.append(str(error))
    for child in children:
        if stale_restore_artifact_pattern.fullmatch(child.name):
            artifacts.append(str(child))
    return {
        "artifacts": sorted(artifacts, key=str.casefold),
        "scanErrors": scan_errors,
    }


def bundled_plugin_roots(plugin_cache_root: Path, plugin_name: str) -> list[Path]:
    plugin_root = plugin_cache_root / plugin_name
    roots: list[Path] = []
    latest_root = plugin_root / "latest"
    if latest_root.exists():
        roots.append(latest_root)
    if plugin_root.exists():
        version_roots = [
            child
            for child in plugin_root.iterdir()
            if child.is_dir()
            and child.name != "latest"
            and not child.name.startswith("plugin-install-")
        ]
        roots.extend(
            sorted(
                version_roots,
                key=lambda child: (child.stat().st_mtime, child.name),
                reverse=True,
            )
        )
    seen: set[str] = set()
    unique_roots: list[Path] = []
    for root in roots:
        resolved_text = str(root.resolve(strict=False)).lower()
        if resolved_text not in seen:
            seen.add(resolved_text)
            unique_roots.append(root)
    return unique_roots


def select_bundled_plugin_root(
    plugin_cache_root: Path,
    plugin_name: str,
    required_relative_paths: list[str],
) -> tuple[Path, list[Path], list[str]]:
    candidate_roots = bundled_plugin_roots(plugin_cache_root, plugin_name)
    fallback_root = plugin_cache_root / plugin_name / "latest"
    if not candidate_roots:
        return fallback_root, [], required_relative_paths
    selected_root = candidate_roots[0]
    selected_missing = [
        relative_path
        for relative_path in required_relative_paths
        if not (selected_root / relative_path).exists()
    ]
    for candidate_root in candidate_roots:
        missing_files = [
            relative_path
            for relative_path in required_relative_paths
            if not (candidate_root / relative_path).exists()
        ]
        if not missing_files:
            return candidate_root, candidate_roots, []
    return selected_root, candidate_roots, selected_missing


def read_text_safely(path: Path, max_bytes: int = 800_000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    with path.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    return content[:max_bytes].decode("utf-8", errors="replace")


def read_text_edges(path: Path, edge_bytes: int = 700_000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        size_bytes = path.stat().st_size
        with path.open("rb") as handle:
            if size_bytes <= edge_bytes * 2:
                content = handle.read()
            else:
                head = handle.read(edge_bytes)
                handle.seek(max(0, size_bytes - edge_bytes))
                tail = handle.read(edge_bytes)
                content = head + b"\n...\n" + tail
    except OSError:
        return ""
    return content.decode("utf-8", errors="replace")


def sqlite_quick_check(database_path: Path, run_quick_check: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": database_path.exists(),
        "quickCheck": "",
        "tables": [],
        "error": "",
    }
    if not database_path.exists():
        return result
    try:
        with connect_database(database_path, readonly=True) as connection:
            result["quickCheck"] = str(connection.execute("PRAGMA quick_check").fetchone()[0]) if run_quick_check else "skipped"
            result["tables"] = [
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
            ]
    except Exception as error:
        result["error"] = str(error)
    return result


def sqlite_table_count(database_path: Path, table_name: str) -> int | None:
    if not database_path.exists():
        return None
    try:
        with connect_database(database_path, readonly=True) as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            if not table_exists:
                return None
            return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
    except Exception:
        return None


def count_recent_log_levels(log_database_path: Path, sample_limit: int = 5000) -> dict[str, int]:
    if not log_database_path.exists():
        return {}
    try:
        with connect_database(log_database_path, readonly=True) as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'logs'"
            ).fetchone()
            if not table_exists:
                return {}
            rows = connection.execute(
                """
                SELECT UPPER(level) AS level_name
                FROM logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (sample_limit,),
            ).fetchall()
    except Exception:
        return {}
    return dict(Counter(str(row["level_name"]) for row in rows))


def recent_log_problem_samples(log_database_path: Path, sample_limit: int = 8) -> list[str]:
    if not log_database_path.exists():
        return []
    try:
        with connect_database(log_database_path, readonly=True) as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'logs'"
            ).fetchone()
            if not table_exists:
                return []
            rows = connection.execute(
                """
                SELECT
                    datetime(ts, 'unixepoch', 'localtime') AS local_time,
                    UPPER(level) AS level_name,
                    target,
                    feedback_log_body,
                    thread_id
                FROM logs
                WHERE UPPER(level) IN ('ERROR', 'WARN', 'WARNING')
                ORDER BY ts DESC, ts_nanos DESC, id DESC
                LIMIT ?
                """,
                (sample_limit,),
            ).fetchall()
    except Exception:
        return []

    samples: list[str] = []
    for row in rows:
        message = prompt_text(row["feedback_log_body"], max_length=220)
        samples.append(
            f"{row['local_time']} {row['level_name']} {row['target']} thread={row['thread_id'] or '-'}: {message}"
        )
    return samples


def recent_log_matches(log_database_path: Path, required_terms: list[str], sample_limit: int = 8) -> list[str]:
    if not log_database_path.exists() or not required_terms:
        return []
    try:
        with connect_database(log_database_path, readonly=True) as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'logs'"
            ).fetchone()
            if not table_exists:
                return []
            where_clause = " AND ".join(["feedback_log_body LIKE ?"] * len(required_terms))
            rows = connection.execute(
                f"""
                SELECT
                    datetime(ts, 'unixepoch', 'localtime') AS local_time,
                    UPPER(level) AS level_name,
                    target,
                    feedback_log_body,
                    thread_id
                FROM logs
                WHERE UPPER(level) IN ('ERROR', 'WARN', 'WARNING') AND {where_clause}
                ORDER BY ts DESC, ts_nanos DESC, id DESC
                LIMIT ?
                """,
                [f"%{term}%" for term in required_terms] + [sample_limit],
            ).fetchall()
    except Exception:
        return []

    samples: list[str] = []
    for row in rows:
        message = prompt_text(row["feedback_log_body"], max_length=240)
        samples.append(
            f"{row['local_time']} {row['level_name']} {row['target']} thread={row['thread_id'] or '-'}: {message}"
        )
    return samples


def notify_log_database_identity(log_database_path: Path) -> dict[str, Any]:
    resolved_path = log_database_path.resolve(strict=True)
    file_stat = resolved_path.stat()
    return {
        "path": str(resolved_path),
        "device": int(file_stat.st_dev),
        "inode": int(file_stat.st_ino),
    }


def _require_notify_log_schema(connection: sqlite3.Connection) -> None:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'logs'"
    ).fetchone()
    if not table_exists:
        raise RuntimeError("logs_2.sqlite does not contain the logs table")
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(logs)").fetchall()}
    required_columns = {"id", "ts", "ts_nanos", "level", "target", "feedback_log_body", "process_uuid"}
    missing_columns = sorted(required_columns - columns)
    if missing_columns:
        raise RuntimeError(f"logs_2.sqlite logs table is missing columns: {', '.join(missing_columns)}")


def snapshot_codex_processes() -> list[dict[str, Any]]:
    try:
        import psutil
    except Exception as error:
        raise RuntimeError(f"psutil is required to bind Codex logs to the live Desktop process tree: {error}") from error
    processes: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "create_time"]):
        try:
            info = process.info
            processes.append(
                {
                    "pid": int(info.get("pid") or 0),
                    "parentPid": int(info.get("ppid") or 0),
                    "name": str(info.get("name") or ""),
                    "executablePath": str(info.get("exe") or ""),
                    "commandLine": " ".join(str(part) for part in (info.get("cmdline") or [])),
                    "createdAtEpoch": int(float(info.get("create_time") or 0)),
                }
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError, ValueError):
            continue
    return processes


def codex_desktop_process_tree(
    process_snapshot: list[dict[str, Any]] | None = None,
    current_appx_install: dict[str, Any] | None = None,
) -> dict[str, Any]:
    processes = list(process_snapshot) if process_snapshot is not None else snapshot_codex_processes()
    appx_install = current_appx_install or inspect_current_codex_appx_install()
    appx_install_path_text = str(appx_install.get("installPath") or "").strip()
    if not appx_install.get("available") or not appx_install_path_text:
        raise RuntimeError(
            "current Codex AppX installation is unavailable for Desktop process binding: "
            + str(appx_install.get("error") or "unknown error")
        )
    appx_install_path = Path(appx_install_path_text)
    normalized: dict[int, dict[str, Any]] = {}
    for raw_process in processes:
        process_id = int(raw_process.get("pid") or raw_process.get("ProcessId") or 0)
        if process_id <= 0:
            continue
        normalized[process_id] = {
            "pid": process_id,
            "parentPid": int(raw_process.get("parentPid") or raw_process.get("ParentProcessId") or 0),
            "name": str(raw_process.get("name") or raw_process.get("Name") or ""),
            "executablePath": str(raw_process.get("executablePath") or raw_process.get("ExecutablePath") or ""),
            "commandLine": str(raw_process.get("commandLine") or raw_process.get("CommandLine") or ""),
            "createdAtEpoch": int(raw_process.get("createdAtEpoch") or raw_process.get("CreationEpoch") or 0),
        }
    desktop_roots = [
        process
        for process in normalized.values()
        if process["name"].casefold() == "chatgpt.exe"
        and "--type=" not in process["commandLine"].casefold()
        and path_is_inside_directory(process["executablePath"], appx_install_path)
    ]
    if len(desktop_roots) != 1:
        raise RuntimeError(
            f"expected one current-AppX Codex Desktop root process, found {len(desktop_roots)}: "
            + ", ".join(str(process["pid"]) for process in desktop_roots)
        )
    desktop_root = desktop_roots[0]
    child_ids_by_parent: dict[int, list[int]] = {}
    for process in normalized.values():
        child_ids_by_parent.setdefault(int(process["parentPid"]), []).append(int(process["pid"]))
    tree_process_ids: set[int] = set()
    pending = [int(desktop_root["pid"])]
    while pending:
        process_id = pending.pop()
        if process_id in tree_process_ids:
            continue
        tree_process_ids.add(process_id)
        pending.extend(child_ids_by_parent.get(process_id, []))
    app_server_processes = [
        normalized[process_id]
        for process_id in sorted(tree_process_ids)
        if normalized[process_id]["name"].casefold() == "codex.exe"
        and re.search(r"(?:^|\s)app-server(?:\s|$)", normalized[process_id]["commandLine"], re.IGNORECASE)
    ]
    current_appx_app_servers = [
        process
        for process in app_server_processes
        if path_is_inside_directory(process["executablePath"], appx_install_path)
    ]
    if not app_server_processes or not current_appx_app_servers:
        raise RuntimeError("current Codex Desktop tree has no current-AppX app-server process")
    return {
        "desktopRootPid": int(desktop_root["pid"]),
        "desktopRootCreatedAtEpoch": int(desktop_root["createdAtEpoch"]),
        "desktopRootExecutablePath": str(desktop_root["executablePath"]),
        "treeProcessIds": sorted(tree_process_ids),
        "appServerPids": [int(process["pid"]) for process in app_server_processes],
        "appServers": app_server_processes,
        "currentAppxInstallPath": appx_install_path_text,
        "currentAppxVersion": str(appx_install.get("version") or ""),
    }


def process_uuid_pid(process_uuid: str) -> int | None:
    match = re.fullmatch(r"pid:(\d+):[0-9A-Za-z-]+", process_uuid)
    return int(match.group(1)) if match else None


def capture_notify_log_boundary(
    log_database_path: Path,
    *,
    process_snapshot: list[dict[str, Any]] | None = None,
    current_appx_install: dict[str, Any] | None = None,
) -> dict[str, Any]:
    database_identity = notify_log_database_identity(log_database_path)
    process_tree = codex_desktop_process_tree(process_snapshot, current_appx_install)
    app_server_pids = list(map(int, process_tree["appServerPids"]))
    process_created_at = {
        int(process["pid"]): int(process.get("createdAtEpoch") or 0)
        for process in process_tree["appServers"]
    }
    connection = connect_database(log_database_path, readonly=True)
    try:
        connection.execute("BEGIN")
        _require_notify_log_schema(connection)
        max_id = int(connection.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM logs").fetchone()["max_id"])
        minimum_process_epoch = max(0, min(process_created_at.values()) - 2)
        uuid_rows = connection.execute(
            """
            SELECT process_uuid, MIN(id) AS first_id, MIN(ts) AS first_ts, MAX(id) AS last_id, MAX(ts) AS last_ts
            FROM logs
            WHERE id <= ?
              AND ts >= ?
              AND process_uuid IS NOT NULL
              AND TRIM(process_uuid) <> ''
            GROUP BY process_uuid
            ORDER BY process_uuid
            """,
            (max_id, minimum_process_epoch),
        ).fetchall()
    finally:
        connection.close()
    process_uuid_rows: list[dict[str, Any]] = []
    for row in uuid_rows:
        process_uuid = str(row["process_uuid"])
        process_id = process_uuid_pid(process_uuid)
        if process_id not in process_created_at:
            continue
        if int(row["first_ts"]) < process_created_at[process_id] - 2:
            continue
        process_uuid_rows.append(
            {
                "process_uuid": process_uuid,
                "pid": process_id,
                "first_id": int(row["first_id"]),
                "first_ts": int(row["first_ts"]),
                "last_id": int(row["last_id"]),
                "last_ts": int(row["last_ts"]),
            }
        )
    uuids_by_pid: dict[str, list[str]] = {}
    for process_id in app_server_pids:
        values = sorted(
            row["process_uuid"] for row in process_uuid_rows if int(row["pid"]) == process_id
        )
        if not values:
            raise RuntimeError(f"logs_2.sqlite has no process_uuid bound to live app-server PID {process_id}")
        uuids_by_pid[str(process_id)] = values
    process_uuids = sorted(row["process_uuid"] for row in process_uuid_rows)
    process_started_at_id = min(row["first_id"] for row in process_uuid_rows)
    process_started_at_epoch = min(row["first_ts"] for row in process_uuid_rows)
    if notify_log_database_identity(log_database_path) != database_identity:
        raise RuntimeError("logs_2.sqlite database identity changed while its boundary was captured")
    return {
        "schema_version": 2,
        "database_identity": database_identity,
        "captured_at_epoch": int(time.time()),
        "max_id": max_id,
        "process_uuid": process_uuids[0],
        "process_uuids": process_uuids,
        "process_uuid_by_pid": uuids_by_pid,
        "process_started_at_id": process_started_at_id,
        "process_started_at_epoch": process_started_at_epoch,
        "desktop_root_pid": int(process_tree["desktopRootPid"]),
        "desktop_root_created_at_epoch": int(process_tree["desktopRootCreatedAtEpoch"]),
        "desktop_root_executable_path": str(process_tree["desktopRootExecutablePath"]),
        "desktop_tree_pids": list(process_tree["treeProcessIds"]),
        "app_server_pids": app_server_pids,
        "app_servers": list(process_tree["appServers"]),
        "current_appx_install_path": str(process_tree["currentAppxInstallPath"]),
        "current_appx_version": str(process_tree["currentAppxVersion"]),
    }


def query_legacy_notify_os206(
    log_database_path: Path,
    *,
    min_epoch: int,
    process_uuid: str | None = None,
    process_uuids: list[str] | None = None,
    after_id: int = 0,
    max_id: int,
    sample_limit: int = 8,
) -> dict[str, Any]:
    if min_epoch < 0:
        raise ValueError("min_epoch must be non-negative")
    bound_process_uuids = sorted(
        {
            str(value).strip()
            for value in (process_uuids or ([process_uuid] if process_uuid else []))
            if str(value).strip()
        }
    )
    if not bound_process_uuids:
        raise ValueError("at least one process_uuid is required")
    if after_id < 0 or max_id < after_id:
        raise ValueError("log id boundaries are invalid")
    if sample_limit < 1:
        raise ValueError("sample_limit must be positive")

    connection = connect_database(log_database_path, readonly=True)
    try:
        _require_notify_log_schema(connection)
        observed_process_uuids = [
            str(row["process_uuid"])
            for row in connection.execute(
                """
                SELECT DISTINCT process_uuid
                FROM logs
                WHERE id > ?
                  AND id <= ?
                  AND ts >= ?
                  AND process_uuid IS NOT NULL
                  AND TRIM(process_uuid) <> ''
                ORDER BY process_uuid
                """,
                (after_id, max_id, min_epoch),
            ).fetchall()
        ]
        match_count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS match_count
                FROM logs
                WHERE id > ?
                  AND id <= ?
                  AND ts >= ?
                  AND target = 'codex_core::hook_runtime'
                  AND INSTR(LOWER(COALESCE(feedback_log_body, '')), 'legacy_notify') > 0
                  AND INSTR(LOWER(COALESCE(feedback_log_body, '')), 'os error 206') > 0
                """,
                (after_id, max_id, min_epoch),
            ).fetchone()["match_count"]
        )
        rows = connection.execute(
            """
            SELECT id, ts, ts_nanos, UPPER(level) AS level_name, target, feedback_log_body, process_uuid
            FROM logs
            WHERE id > ?
              AND id <= ?
              AND ts >= ?
              AND target = 'codex_core::hook_runtime'
              AND INSTR(LOWER(COALESCE(feedback_log_body, '')), 'legacy_notify') > 0
              AND INSTR(LOWER(COALESCE(feedback_log_body, '')), 'os error 206') > 0
            ORDER BY id ASC
            LIMIT ?
            """,
            (after_id, max_id, min_epoch, sample_limit),
        ).fetchall()
    finally:
        connection.close()

    matches = [
        {
            "id": int(row["id"]),
            "epoch": int(row["ts"]),
            "epoch_nanos": int(row["ts_nanos"]),
            "level": str(row["level_name"]),
            "target": str(row["target"]),
            "process_uuid": str(row["process_uuid"]),
            "message": prompt_text(row["feedback_log_body"], max_length=320),
        }
        for row in rows
    ]
    return {
        "schema_version": 2,
        "min_epoch": min_epoch,
        "process_uuid": bound_process_uuids[0],
        "process_uuids": bound_process_uuids,
        "after_id": after_id,
        "max_id": max_id,
        "match_count": match_count,
        "observed_process_uuids": observed_process_uuids,
        "unexpected_process_uuids": sorted(set(observed_process_uuids) - set(bound_process_uuids)),
        "unbound_match_process_uuids": sorted(
            {
                item["process_uuid"]
                for item in matches
                if item["process_uuid"] not in bound_process_uuids
            }
        ),
        "matches": matches,
    }


CONTEXT_WINDOW_ERROR_SIGNATURES = (
    "ran out of room in the model's context window",
    "context_length_exceeded",
    "maximum context length",
    "context window exceeded",
)


def context_window_error_signature(value: Any) -> str:
    normalized_value = str(value or "").casefold()
    return next((signature for signature in CONTEXT_WINDOW_ERROR_SIGNATURES if signature in normalized_value), "")


def recent_context_window_log_matches(log_database_path: Path, sample_limit: int = 8) -> list[str]:
    if not log_database_path.exists():
        return []
    try:
        with connect_database(log_database_path, readonly=True) as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'logs'"
            ).fetchone()
            if not table_exists:
                return []
            signature_clause = " OR ".join(["LOWER(feedback_log_body) LIKE ?"] * len(CONTEXT_WINDOW_ERROR_SIGNATURES))
            rows = connection.execute(
                f"""
                SELECT
                    datetime(ts, 'unixepoch', 'localtime') AS local_time,
                    UPPER(level) AS level_name,
                    target,
                    feedback_log_body,
                    thread_id
                FROM logs
                WHERE UPPER(level) IN ('ERROR', 'WARN', 'WARNING')
                  AND ts >= (SELECT COALESCE(MAX(ts), 0) - 604800 FROM logs)
                  AND ({signature_clause})
                ORDER BY ts DESC, ts_nanos DESC, id DESC
                LIMIT ?
                """,
                [f"%{signature}%" for signature in CONTEXT_WINDOW_ERROR_SIGNATURES] + [sample_limit],
            ).fetchall()
    except Exception:
        return []

    return [
        (
            f"{row['local_time']} {row['level_name']} {row['target']} "
            f"thread={row['thread_id'] or '-'}: {prompt_text(row['feedback_log_body'], max_length=240)}"
        )
        for row in rows
    ]


def parse_json_file(path: Path, max_bytes: int = 80_000_000) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": path.exists(),
        "valid": False,
        "keys": [],
        "error": "",
        "replacementCharacters": 0,
        "sizeBytes": 0,
    }
    if not path.exists():
        return result
    try:
        result["sizeBytes"] = path.stat().st_size
        if result["sizeBytes"] > max_bytes:
            result["error"] = f"file too large to parse safely: {result['sizeBytes']} bytes"
            return result
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        result["error"] = str(error)
        return result
    result["replacementCharacters"] = text.count("\ufffd")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        result["error"] = str(error)
        return result
    result["valid"] = isinstance(value, dict)
    if isinstance(value, dict):
        result["keys"] = sorted(str(key) for key in value.keys())[:40]
    return result


def parse_session_index(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": path.exists(),
        "records": 0,
        "invalidLines": 0,
        "duplicateThreadIds": 0,
        "replacementCharacters": 0,
        "sampleDuplicates": [],
    }
    if not path.exists():
        return result
    thread_ids: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            result["replacementCharacters"] += line.count("\ufffd")
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                result["invalidLines"] += 1
                continue
            thread_id = str(item.get("id") or "")
            if thread_id:
                thread_ids.append(thread_id)
    counts = Counter(thread_ids)
    duplicates = [thread_id for thread_id, count in counts.items() if count > 1]
    result["records"] = len(thread_ids)
    result["duplicateThreadIds"] = len(duplicates)
    result["sampleDuplicates"] = duplicates[:8]
    return result


def compact_diagnostic_text(value: Any, max_length: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_length:
        return text
    return f"{text[: max(0, max_length - 1)].rstrip()}…"


def response_item_message_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "input_text", "output_text"):
                    text = item.get(key)
                    if isinstance(text, str):
                        parts.append(text)
                        break
    return "".join(parts).strip()


def is_codex_context_payload(text: str) -> bool:
    prefix = text.lstrip()[:5000]
    return (
        prefix.startswith("# AGENTS.md instructions")
        or prefix.startswith("<environment_context>")
        or prefix.startswith("<turn_aborted>")
        or prefix.startswith("<user_interruption>")
        or "<environment_context>" in prefix
        or "<INSTRUCTIONS>" in prefix
        or "<permissions instructions>" in prefix
    )


def normalized_compare_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def title_matches_message(title: str, message: str) -> bool:
    normalized_title = normalized_compare_text(title)
    normalized_message = normalized_compare_text(message)
    if not normalized_title or not normalized_message:
        return False
    return normalized_message.startswith(normalized_title) or normalized_title.startswith(normalized_message[: len(normalized_title)])


def scan_rollout_event_stream_integrity(
    rollout_path_text: str,
    max_bytes: int | None = None,
    max_lines: int | None = None,
) -> dict[str, Any]:
    rollout_path = Path(rollout_path_text)
    result: dict[str, Any] = {
        "exists": rollout_path.exists(),
        "parseErrors": 0,
        "lineCount": 0,
        "responseUserMessages": 0,
        "responseAssistantMessages": 0,
        "eventUserMessages": 0,
        "eventAgentMessages": 0,
        "eventThreadNameUpdated": 0,
        "responseChatMessages": 0,
        "eventChatMessages": 0,
        "compactedCount": 0,
        "embeddedImageRefs": 0,
        "embeddedImageUrlFields": 0,
        "invalidImageUrlRefs": 0,
        "encryptedContentFields": 0,
        "firstResponseUserMessage": "",
        "firstEventUserMessage": "",
        "latestThreadNameUpdated": "",
        "truncated": False,
        "scannedBytesApprox": 0,
    }
    if not rollout_path.exists() or not rollout_path.is_file():
        return result

    try:
        with rollout_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                result["lineCount"] += 1
                result["scannedBytesApprox"] += len(line)
                result["embeddedImageRefs"] += line.count("data:image/")
                if (max_lines is not None and int(result["lineCount"]) > max_lines) or (
                    max_bytes is not None and int(result["scannedBytesApprox"]) > max_bytes
                ):
                    result["truncated"] = True
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    result["parseErrors"] += 1
                    continue
                if not isinstance(item, dict):
                    continue
                if is_compacted_item(item):
                    result["compactedCount"] += 1
                collect_jsonl_schema_stats(item, result)
                item_type = item.get("type")
                payload = item.get("payload")
                if item_type == "response_item" and isinstance(payload, dict) and payload.get("type") == "message":
                    role = str(payload.get("role") or "")
                    text = response_item_message_text(payload)
                    if role == "user" and text and not is_codex_context_payload(text):
                        result["responseUserMessages"] += 1
                        if not result["firstResponseUserMessage"]:
                            result["firstResponseUserMessage"] = compact_diagnostic_text(text)
                    elif role == "assistant" and text:
                        result["responseAssistantMessages"] += 1
                    continue
                if item_type == "event_msg" and isinstance(payload, dict):
                    event_type = str(payload.get("type") or "")
                    if event_type == "user_message":
                        message = str(payload.get("message") or payload.get("text") or "").strip()
                        if message:
                            result["eventUserMessages"] += 1
                            if not result["firstEventUserMessage"]:
                                result["firstEventUserMessage"] = compact_diagnostic_text(message)
                    elif event_type == "agent_message":
                        message = str(payload.get("message") or payload.get("text") or "").strip()
                        if message:
                            result["eventAgentMessages"] += 1
                    elif event_type == "thread_name_updated":
                        title = str(payload.get("name") or payload.get("thread_name") or payload.get("title") or "").strip()
                        result["eventThreadNameUpdated"] += 1
                        if title:
                            result["latestThreadNameUpdated"] = title
                if (max_lines is not None and int(result["lineCount"]) >= max_lines) or (
                    max_bytes is not None and int(result["scannedBytesApprox"]) >= max_bytes
                ):
                    result["truncated"] = True
                    break
    except OSError:
        result["parseErrors"] += 1

    result["responseChatMessages"] = int(result["responseUserMessages"]) + int(result["responseAssistantMessages"])
    result["eventChatMessages"] = int(result["eventUserMessages"]) + int(result["eventAgentMessages"])
    return result


def main_event_stream_missing(scan: dict[str, Any]) -> bool:
    response_chat_messages = int(scan.get("responseChatMessages") or 0)
    event_chat_messages = int(scan.get("eventChatMessages") or 0)
    response_assistant_messages = int(scan.get("responseAssistantMessages") or 0)
    event_agent_messages = int(scan.get("eventAgentMessages") or 0)
    return (
        response_chat_messages >= 2
        and event_chat_messages == 0
    ) or (
        response_assistant_messages >= 2
        and event_agent_messages == 0
        and event_chat_messages <= 1
    )


def main_event_stream_sparse(scan: dict[str, Any]) -> bool:
    response_chat_messages = int(scan.get("responseChatMessages") or 0)
    event_chat_messages = int(scan.get("eventChatMessages") or 0)
    return response_chat_messages >= 10 and event_chat_messages < max(2, response_chat_messages // 2)


def event_stream_evidence(scan: dict[str, Any]) -> str:
    return (
        f"{scan.get('title')} | {scan.get('threadId')} | "
        f"response_user={scan.get('responseUserMessages')} "
        f"response_assistant={scan.get('responseAssistantMessages')} "
        f"event_user={scan.get('eventUserMessages')} "
        f"event_agent={scan.get('eventAgentMessages')} "
        f"title_events={scan.get('eventThreadNameUpdated')} | "
        f"first_response_user={scan.get('firstResponseUserMessage')}"
    )


def rollout_jsonl_evidence(scan: dict[str, Any]) -> str:
    return (
        f"{scan.get('title')} | {scan.get('threadId')} | "
        f"parse_errors={scan.get('jsonlParseErrors')} "
        f"invalid_image_url={scan.get('jsonlInvalidImageUrlRefs')} "
        f"embedded_image_url={scan.get('jsonlEmbeddedImageUrlFields')} "
        f"compacted={scan.get('jsonlCompactedCount')} "
        f"encrypted_content={scan.get('jsonlEncryptedContentFields')}"
    )


def config_plugin_enabled(config_text: str, plugin_name: str, source: str = "openai-bundled") -> bool:
    block_pattern = re.compile(
        rf'^\[plugins\."{re.escape(plugin_name)}@{re.escape(source)}"\]\s*$(.*?)(?=^\[|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    match = block_pattern.search(config_text)
    if not match:
        return False
    return re.search(r"^\s*enabled\s*=\s*true\s*$", match.group(1), re.MULTILINE | re.IGNORECASE) is not None


def config_plugin_disabled(config_text: str, plugin_name: str, source: str) -> bool:
    block_pattern = re.compile(
        rf'^\[plugins\."{re.escape(plugin_name)}@{re.escape(source)}"\]\s*$(.*?)(?=^\[|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    match = block_pattern.search(config_text)
    if not match:
        return False
    return re.search(r"^\s*enabled\s*=\s*false\s*$", match.group(1), re.MULTILINE | re.IGNORECASE) is not None


def remote_plugin_mcp_cache_present(codex_home_path: Path, plugin_name: str) -> bool:
    plugin_root = codex_home_path / "plugins" / "cache" / "openai-curated-remote" / plugin_name
    if not plugin_root.is_dir():
        return False
    try:
        return any(candidate.is_file() for candidate in plugin_root.glob("*/.mcp.json"))
    except OSError:
        return False


def remote_plugin_install_marker_present(codex_home_path: Path, plugin_name: str) -> bool:
    marker_path = (
        codex_home_path
        / "plugins"
        / "cache"
        / "openai-curated-remote"
        / plugin_name
        / ".codex-remote-plugin-install.json"
    )
    return marker_path.is_file()


def bundled_marketplace_source_contract(
    config_text: str,
    managed_config_text: str,
    codex_home_path: Path,
) -> dict[str, Any]:
    expected_source = codex_home_path / ".tmp" / "bundled-marketplaces" / "openai-bundled"
    expected_primary_runtime_source = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "plugins"
        / "openai-primary-runtime"
    )

    def read_document(text: str) -> dict[str, Any]:
        try:
            return tomllib.loads(text.lstrip("\ufeff")) if text.strip() else {}
        except tomllib.TOMLDecodeError:
            return {}

    def read_marketplace(document: dict[str, Any], name: str, expected: Path) -> dict[str, Any]:
        marketplace = ((document.get("marketplaces") or {}).get(name) or {})
        source = str(marketplace.get("source") or "")
        source_type = str(marketplace.get("source_type") or "")
        present = bool(marketplace)
        return {
            "present": present,
            "source": source,
            "sourceType": source_type,
            "matchesRuntimeSource": (
                bool(source)
                and source_type == "local"
                and windows_path_key(source) == windows_path_key(expected)
            ),
        }

    runtime_document = read_document(config_text)
    managed_document = read_document(managed_config_text)
    runtime_layer = read_marketplace(runtime_document, "openai-bundled", expected_source)
    managed_layer = read_marketplace(managed_document, "openai-bundled", expected_source)
    primary_runtime_layer = read_marketplace(
        runtime_document,
        "openai-primary-runtime",
        expected_primary_runtime_source,
    )
    primary_managed_layer = read_marketplace(
        managed_document,
        "openai-primary-runtime",
        expected_primary_runtime_source,
    )
    conflicts: list[str] = []
    for marketplace_name, runtime, managed in (
        ("openai-bundled", runtime_layer, managed_layer),
        ("openai-primary-runtime", primary_runtime_layer, primary_managed_layer),
    ):
        if runtime["present"] and not runtime["matchesRuntimeSource"]:
            conflicts.append(
                f"config.{marketplace_name}: runtime source does not match the Desktop-owned source "
                f"source_type={runtime['sourceType']!r} "
                f"source={runtime['source']!r}"
            )
        if managed["present"]:
            conflicts.append(
                f"managed.{marketplace_name}: Desktop-owned marketplace is pinned "
                f"source_type={managed['sourceType']!r} source={managed['source']!r}"
            )
    return {
        "expectedSource": str(expected_source),
        "expectedPrimaryRuntimeSource": str(expected_primary_runtime_source),
        "runtime": runtime_layer,
        "managed": managed_layer,
        "primaryRuntime": primary_runtime_layer,
        "primaryManaged": primary_managed_layer,
        "conflicts": conflicts,
    }


def enabled_configured_plugins(config_text: str, source: str) -> list[str]:
    block_pattern = re.compile(
        rf'^\[plugins\."([^"]+)@{re.escape(source)}"\]\s*$(.*?)(?=^\[|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    plugin_names: list[str] = []
    for match in block_pattern.finditer(config_text):
        if re.search(r"^\s*enabled\s*=\s*true\s*$", match.group(2), re.MULTILINE | re.IGNORECASE):
            plugin_names.append(str(match.group(1)))
    return sorted(set(plugin_names))


def configured_plugins(config_text: str, source: str) -> list[str]:
    block_pattern = re.compile(
        rf'^\[plugins\."([^"]+)@{re.escape(source)}"\]\s*$',
        re.MULTILINE,
    )
    return sorted(set(match.group(1) for match in block_pattern.finditer(config_text)))


def extract_config_path(config_text: str, key: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
    match = pattern.search(config_text)
    return match.group(1) if match else ""


def extract_config_array_values(config_text: str, key: str) -> list[str]:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*\[(.*?)\]", re.MULTILINE | re.DOTALL)
    match = pattern.search(config_text)
    if not match:
        return []
    return [item for item in re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)) if item]


def extract_top_level_toml_array_values(config_text: str, key: str) -> list[str]:
    if not config_text.strip():
        return []
    try:
        document = tomllib.loads(config_text.lstrip("\ufeff"))
    except Exception:
        return []
    value = document.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def files_have_same_sha256(left: Path, right: Path) -> bool:
    try:
        if not left.is_file() or not right.is_file() or left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            return hashlib.file_digest(left_handle, "sha256").digest() == hashlib.file_digest(
                right_handle, "sha256"
            ).digest()
    except OSError:
        return False


def is_desktop_managed_computer_use_notify(
    values: list[str],
    config_text: str = "",
    managed_config_text: str = "",
    current_appx_install: dict[str, Any] | None = None,
) -> bool:
    if len(values) != 2 or values[1].casefold() != "turn-ended":
        return False
    contract = node_repl_effective_contract(config_text, managed_config_text)
    if contract["conflicts"]:
        return False
    environment = contract["effectiveEnv"]
    module_roots = [Path(value).expanduser() for value in split_path_list_text(environment.get("NODE_REPL_NODE_MODULE_DIRS", ""))]
    helper_relative_path = Path("@oai") / "sky" / "bin" / "windows" / "codex-computer-use.exe"
    module_root = next((root for root in module_roots if (root / helper_relative_path).is_file()), None)
    appx_install = current_appx_install or inspect_current_codex_appx_install()
    install_path_text = str(appx_install.get("installPath") or "")
    if module_root is None or not appx_install.get("available") or not install_path_text:
        return False
    appx_resources = Path(install_path_text) / "app" / "resources"
    executable_path = Path(values[0]).expanduser()
    expected_dynamic_helper = module_root / helper_relative_path
    node_repl_path = Path(str(contract["effectiveCommand"] or "")).expanduser()
    node_path = Path(str(environment.get("NODE_REPL_NODE_PATH") or "")).expanduser()
    cli_path = Path(str(environment.get("CODEX_CLI_PATH") or "")).expanduser()
    return (
        same_resolved_path(executable_path, expected_dynamic_helper)
        and str(environment.get("SKY_CUA_NATIVE_PIPE") or "") == "1"
        and bool(str(environment.get("SKY_CUA_NATIVE_PIPE_DIRECTORY") or "").strip())
        and files_have_same_sha256(
            executable_path,
            appx_resources / "cua_node" / "bin" / "node_modules" / helper_relative_path,
        )
        and files_have_same_sha256(node_repl_path, appx_resources / "cua_node" / "bin" / "node_repl.exe")
        and files_have_same_sha256(node_path, appx_resources / "cua_node" / "bin" / "node.exe")
        and files_have_same_sha256(cli_path, appx_resources / "codex.exe")
    )


def extract_config_table_array_values(config_text: str, table_name: str, key: str) -> list[str]:
    table_pattern = re.compile(
        rf"^\[{re.escape(table_name)}\]\s*$(.*?)(?=^\[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    table_match = table_pattern.search(config_text)
    if not table_match:
        return []
    return extract_config_array_values(table_match.group(1), key)


def node_repl_effective_contract(config_text: str, managed_config_text: str) -> dict[str, Any]:
    def read_layer(text: str) -> dict[str, Any]:
        try:
            document = tomllib.loads(text.lstrip("\ufeff")) if text.strip() else {}
        except tomllib.TOMLDecodeError:
            document = {}
        mcp_servers = document.get("mcp_servers") or {}
        table_present = "node_repl" in mcp_servers
        node_repl = mcp_servers.get("node_repl") or {}
        environment = node_repl.get("env") or {}
        return {
            "tablePresent": table_present,
            "commandPresent": "command" in node_repl,
            "command": str(node_repl.get("command") or ""),
            "argsPresent": "args" in node_repl,
            "args": [str(value) for value in node_repl.get("args") or []],
            "env": {str(key): str(value) for key, value in environment.items()},
        }

    runtime_layer = read_layer(config_text)
    managed_layer = read_layer(managed_config_text)
    effective_environment = {**runtime_layer["env"], **managed_layer["env"]}
    effective_command = managed_layer["command"] if managed_layer["commandPresent"] else runtime_layer["command"]
    effective_args = managed_layer["args"] if managed_layer["argsPresent"] else runtime_layer["args"]
    conflicts: list[str] = []
    if managed_layer["tablePresent"]:
        conflicts.append(
            "managed node_repl table must be absent; it shadows the privileged runtime that Codex Desktop generates"
        )
        if managed_layer["commandPresent"]:
            conflicts.append(f"command: managed override={managed_layer['command']!r}")
        if managed_layer["argsPresent"]:
            conflicts.append(
                f"args: managed override={json.dumps(managed_layer['args'], ensure_ascii=False)}"
            )
        for key in sorted(managed_layer["env"]):
            conflicts.append(f"env.{key}: managed override={managed_layer['env'][key]!r}")
    return {
        "runtime": runtime_layer,
        "managed": managed_layer,
        "effectiveCommand": effective_command,
        "effectiveArgs": effective_args,
        "effectiveEnv": effective_environment,
        "conflicts": conflicts,
    }


def split_csv_text(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def split_path_list_text(value: str) -> list[str]:
    if not value:
        return []
    separators = {",", os.pathsep}
    if os.name == "nt":
        separators.add(";")
    parts = [value]
    for separator in separators:
        next_parts: list[str] = []
        for part in parts:
            next_parts.extend(part.split(separator))
        parts = next_parts
    return [part.strip().strip("'\"") for part in parts if part.strip().strip("'\"")]


def normalize_skill_reference_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def collect_nested_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in collect_nested_string_values(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in collect_nested_string_values(item)]
    return []


def decoded_skill_reference_candidates(text: str) -> list[str]:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if parsed is not None:
        return collect_nested_string_values(parsed)

    candidates: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed_line = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            candidates.append(line)
        else:
            candidates.extend(collect_nested_string_values(parsed_line))
    return candidates or [text]


def build_skill_path(root_text: str, relative_text: str) -> Path:
    root_path = Path(root_text.strip().strip("`'\" "))
    relative_parts = [part for part in re.split(r"[\\/]+", relative_text.strip().strip("`'\" ")) if part]
    return root_path.joinpath(*relative_parts)


def extract_advertised_skill_paths(text: str) -> list[str]:
    referenced_paths: list[str] = []
    for candidate in decoded_skill_reference_candidates(text):
        normalized_text = normalize_skill_reference_text(candidate)
        root_matches = re.finditer(r"(?m)\b(r\d+)\s*=\s*([A-Za-z]:[^\r\n`\"<>]+)", normalized_text)
        root_paths = {
            match.group(1): match.group(2).strip().rstrip(" ,;")
            for match in root_matches
        }
        for match in re.finditer(r"\(file:\s*(r\d+)/([^)]+?SKILL\.md)\)", normalized_text):
            root_text = root_paths.get(match.group(1))
            if root_text:
                referenced_paths.append(str(build_skill_path(root_text, match.group(2))))

    seen: set[str] = set()
    unique_paths: list[str] = []
    for path_text in referenced_paths:
        normalized_key = str(Path(path_text).resolve(strict=False)).lower() if os.name == "nt" else str(Path(path_text).resolve(strict=False))
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        unique_paths.append(path_text)
    return unique_paths


def is_path_under(child_path: Path, parent_path: Path) -> bool:
    try:
        child_path.resolve(strict=False).relative_to(parent_path.resolve(strict=False))
        return True
    except ValueError:
        return False


def recent_snapshot_rollout_paths(snapshot: dict[str, Any] | None, max_paths: int = 14) -> list[Path]:
    if not snapshot:
        return []
    threads = sorted(
        snapshot.get("threads", []),
        key=lambda item: int(item.get("updatedAtMs") or 0),
        reverse=True,
    )
    rollout_paths: list[Path] = []
    seen: set[str] = set()
    for thread in threads:
        rollout_text = str(thread.get("rolloutPath") or "")
        if not rollout_text:
            continue
        rollout_path = Path(rollout_text)
        normalized_key = str(rollout_path.resolve(strict=False)).lower() if os.name == "nt" else str(rollout_path.resolve(strict=False))
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        rollout_paths.append(rollout_path)
        if len(rollout_paths) >= max_paths:
            break
    return rollout_paths


def scan_advertised_skill_paths(codex_home_path: Path, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    source_paths = recent_snapshot_rollout_paths(snapshot)
    source_paths.append(codex_home_path / ".codex-global-state.json")
    source_paths.append(codex_home_path / "config.toml")

    referenced_by_path: dict[str, list[str]] = {}
    scanned_paths: list[str] = []
    for source_path in source_paths:
        if not source_path.exists() or not source_path.is_file():
            continue
        scanned_paths.append(str(source_path))
        for skill_path_text in extract_advertised_skill_paths(read_text_edges(source_path)):
            referenced_by_path.setdefault(skill_path_text, []).append(str(source_path))

    missing_paths: list[str] = []
    external_missing_paths: list[str] = []
    for skill_path_text in sorted(referenced_by_path):
        skill_path = Path(skill_path_text)
        if skill_path.exists():
            continue
        if is_path_under(skill_path, codex_home_path):
            missing_paths.append(skill_path_text)
        else:
            external_missing_paths.append(skill_path_text)
    return {
        "scannedPaths": scanned_paths,
        "referencedCount": len(referenced_by_path),
        "missingPaths": missing_paths,
        "externalMissingPaths": external_missing_paths,
        "missingSources": {
            skill_path_text: referenced_by_path.get(skill_path_text, [])[:4]
            for skill_path_text in missing_paths[:12]
        },
        "externalMissingSources": {
            skill_path_text: referenced_by_path.get(skill_path_text, [])[:4]
            for skill_path_text in external_missing_paths[:12]
        },
        "sampleReferencedPaths": sorted(referenced_by_path)[:12],
    }


def scan_runtime_junctions(plugin_cache_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": plugin_cache_root.exists(),
        "scannedLinks": 0,
        "brokenLinks": [],
        "sampleLinks": [],
    }
    if not plugin_cache_root.exists():
        return result
    try:
        plugin_directories = sorted(
            [path for path in plugin_cache_root.iterdir() if path.is_dir()],
            key=lambda path: path.name.lower(),
        )
    except OSError:
        return result
    for plugin_directory in plugin_directories:
        try:
            children = sorted(plugin_directory.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            continue
        for child in children:
            try:
                is_junction = child.is_junction()
            except OSError:
                continue
            if not is_junction:
                continue
            target_path = child.resolve(strict=False)
            target_exists = target_path.exists()
            line = f"{child} -> {target_path} ({'exists' if target_exists else 'missing'})"
            result["scannedLinks"] += 1
            if len(result["sampleLinks"]) < 16:
                result["sampleLinks"].append(line)
            if not target_exists:
                result["brokenLinks"].append(line)
    return result


def probe_directory_writable(directory_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(directory_path),
        "exists": directory_path.exists() and directory_path.is_dir(),
        "writable": False,
        "error": "",
    }
    if not result["exists"]:
        result["error"] = "directory missing"
        return result
    probe_path = directory_path / f".codex-home-manager-write-test-{os.getpid()}-{time.time_ns()}.tmp"
    try:
        probe_path.write_text("codex-home-manager-runtime-probe", encoding="utf-8")
        if probe_path.read_text(encoding="utf-8") != "codex-home-manager-runtime-probe":
            result["error"] = "probe readback mismatch"
            return result
        result["writable"] = True
    except OSError as error:
        result["error"] = str(error)
    finally:
        try:
            if probe_path.exists():
                probe_path.unlink()
        except OSError as error:
            result["error"] = f"{result['error']}; cleanup failed: {error}".strip("; ")
    return result


def scan_node_repl_asset_directories(config_text: str) -> dict[str, Any]:
    directories: list[Path] = []
    files: list[Path] = []

    def add_directory(path: Path) -> None:
        if not str(path):
            return
        normalized_key = str(path.resolve(strict=False)).lower() if os.name == "nt" else str(path.resolve(strict=False))
        if normalized_key not in seen_directories:
            seen_directories.add(normalized_key)
            directories.append(path)

    def add_file(path: Path) -> None:
        if not str(path):
            return
        normalized_key = str(path.resolve(strict=False)).lower() if os.name == "nt" else str(path.resolve(strict=False))
        if normalized_key not in seen_files:
            seen_files.add(normalized_key)
            files.append(path)
            add_directory(path.parent)

    seen_directories: set[str] = set()
    seen_files: set[str] = set()
    node_repl_node_path = extract_config_path(config_text, "NODE_REPL_NODE_PATH")
    if node_repl_node_path:
        add_file(Path(node_repl_node_path))

    local_app_data = os.environ.get("LOCALAPPDATA") or ""
    codex_bin_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin" if local_app_data else Path()
    if local_app_data:
        add_directory(codex_bin_root)
        for executable_name in ("node_repl.exe", "node.exe", "codex.exe"):
            direct_path = codex_bin_root / executable_name
            if direct_path.exists():
                add_file(direct_path)
        if codex_bin_root.exists():
            try:
                for executable_path in list(codex_bin_root.glob("*/node_repl.exe"))[:12]:
                    add_file(executable_path)
                for executable_path in list(codex_bin_root.glob("*/node.exe"))[:12]:
                    add_file(executable_path)
            except OSError:
                pass

    probes = [probe_directory_writable(directory_path) for directory_path in directories]
    missing_files = [str(file_path) for file_path in files if not file_path.exists()]
    node_repl_files = [str(file_path) for file_path in files if file_path.name.lower() == "node_repl.exe" and file_path.exists()]
    node_files = [str(file_path) for file_path in files if file_path.name.lower() == "node.exe" and file_path.exists()]
    return {
        "codexBinRoot": str(codex_bin_root) if local_app_data else "",
        "directories": [str(directory_path) for directory_path in directories],
        "probes": probes,
        "unwritableDirectories": [
            f"{probe['path']} | {probe['error'] or 'not writable'}"
            for probe in probes
            if probe.get("exists") and not probe.get("writable")
        ],
        "missingDirectories": [
            str(probe.get("path") or "")
            for probe in probes
            if not probe.get("exists")
        ],
        "missingFiles": missing_files,
        "nodeReplFiles": node_repl_files,
        "nodeFiles": node_files,
    }


def scan_node_repl_node_module_roots(
    config_text: str,
    managed_config_text: str = "",
) -> dict[str, Any]:
    contract = node_repl_effective_contract(config_text, managed_config_text)
    configured_text = str(
        contract["effectiveEnv"].get("NODE_REPL_NODE_MODULE_DIRS") or ""
    )
    roots = [Path(os.path.expandvars(os.path.expanduser(path_text))) for path_text in split_path_list_text(configured_text)]
    seen_roots: set[str] = set()
    unique_roots: list[Path] = []
    for root in roots:
        normalized_key = str(root.resolve(strict=False)).lower() if os.name == "nt" else str(root.resolve(strict=False))
        if normalized_key in seen_roots:
            continue
        seen_roots.add(normalized_key)
        unique_roots.append(root)

    existing_roots = [root for root in unique_roots if root.exists() and root.is_dir()]
    missing_roots = [str(root) for root in unique_roots if not root.exists() or not root.is_dir()]
    playwright_roots = [
        str(root)
        for root in existing_roots
        if (root / "playwright" / "package.json").exists()
    ]
    ws_roots = [
        str(root)
        for root in existing_roots
        if (root / "ws" / "package.json").exists()
    ]
    computer_use_client_relative_path = Path(
        "@oai/sky/dist/project/cua/sky_js/src/targets/windows/internal/computer_use_client_base.js"
    )
    computer_use_sky_roots = [
        str(root)
        for root in existing_roots
        if (root / computer_use_client_relative_path).is_file()
    ]
    package_samples: list[str] = []
    for root in existing_roots[:8]:
        package_samples.append(
            f"{root} | playwright={'present' if (root / 'playwright' / 'package.json').exists() else 'missing'} "
            f"| ws={'present' if (root / 'ws' / 'package.json').exists() else 'missing'} "
            f"| computer_use_sky={'present' if (root / computer_use_client_relative_path).is_file() else 'missing'}"
        )
    return {
        "configuredText": configured_text,
        "roots": [str(root) for root in unique_roots],
        "existingRoots": [str(root) for root in existing_roots],
        "missingRoots": missing_roots,
        "playwrightRoots": playwright_roots,
        "wsRoots": ws_roots,
        "computerUseSkyRoots": computer_use_sky_roots,
        "firstRoot": str(unique_roots[0]) if unique_roots else "",
        "firstRootHasComputerUseSky": bool(
            unique_roots and (unique_roots[0] / computer_use_client_relative_path).is_file()
        ),
        "packageSamples": package_samples,
    }


def inspect_current_codex_appx_install() -> dict[str, Any]:
    global current_codex_appx_install_cache
    if os.name != "nt":
        return {
            "available": False,
            "installPath": "",
            "version": "",
            "error": "Current Codex AppX inspection is only available on Windows.",
        }
    now = time.monotonic()
    with diagnostics_runtime_cache_lock:
        cache_epoch = diagnostics_runtime_cache_epoch
        cached_entry = current_codex_appx_install_cache
        if cached_entry and cached_entry[0] == cache_epoch and now - cached_entry[1] < 30:
            return cached_entry[2]

    def remember(result: dict[str, Any]) -> dict[str, Any]:
        global current_codex_appx_install_cache

        with diagnostics_runtime_cache_lock:
            if cache_epoch == diagnostics_runtime_cache_epoch:
                current_codex_appx_install_cache = (cache_epoch, time.monotonic(), result)
        return result

    powershell_path = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not powershell_path:
        result = {
            "available": False,
            "installPath": "",
            "version": "",
            "error": "PowerShell is unavailable; the current Codex AppX installation cannot be verified.",
        }
        return remember(result)

    script = (
        "$package = Get-AppxPackage -Name OpenAI.Codex | "
        "Sort-Object {[version]$_.Version} -Descending | Select-Object -First 1; "
        "if ($null -eq $package) { exit 3 }; "
        "[pscustomobject]@{installPath=$package.InstallLocation; version=$package.Version.ToString()} | "
        "ConvertTo-Json -Compress"
    )
    command_result = run_hidden_command(
        [powershell_path, "-NoProfile", "-NonInteractive", "-Command", script],
        timeout_seconds=10,
    )
    result = {
        "available": False,
        "installPath": "",
        "version": "",
        "error": "",
    }
    if command_result.get("returnCode") != 0:
        result["error"] = str(
            command_result.get("error")
            or command_result.get("stderr")
            or f"Get-AppxPackage exited with code {command_result.get('returnCode')}"
        ).strip()
        return remember(result)
    try:
        package_data = json.loads(str(command_result.get("stdout") or ""))
    except json.JSONDecodeError as error:
        result["error"] = f"Could not parse Get-AppxPackage output: {error}"
        return remember(result)

    install_path_text = str(package_data.get("installPath") or "").strip() if isinstance(package_data, dict) else ""
    version_text = str(package_data.get("version") or "").strip() if isinstance(package_data, dict) else ""
    if not install_path_text:
        result["error"] = "Get-AppxPackage did not return an InstallLocation."
    else:
        install_path = Path(install_path_text)
        try:
            install_path_available = install_path.is_dir()
        except OSError as error:
            install_path_available = False
            result["error"] = f"Current Codex AppX InstallLocation is inaccessible: {error}"
        if install_path_available:
            result.update({"available": True, "installPath": install_path_text, "version": version_text})
        elif not result["error"]:
            result["error"] = f"Current Codex AppX InstallLocation does not exist: {install_path_text}"
    return remember(result)


def scan_chrome_native_host_paths(
    codex_home_path: Path,
    current_appx_install: dict[str, Any] | None = None,
    expected_paths: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files = [
        codex_home_path / "chrome-native-hosts.json",
        codex_home_path / "chrome-native-hosts-v2.json",
    ]
    required_path_keys = [
        "codexCliPath",
        "browserClientPath",
        "extensionHostPath",
        "resourcesPath",
        "nodePath",
        "nodeReplPath",
    ]
    appx_install = current_appx_install or inspect_current_codex_appx_install()
    current_appx_path_text = str(appx_install.get("installPath") or "").strip()
    current_appx_path = Path(current_appx_path_text) if current_appx_path_text else None
    result: dict[str, Any] = {
        "files": [str(path) for path in files],
        "existingFiles": [],
        "entries": 0,
        "v1Entries": 0,
        "v2Entries": 0,
        "healthyEntries": 0,
        "healthyV1Entries": 0,
        "healthyV2Entries": 0,
        "healthyCodexCliPaths": [],
        "staleEntries": 0,
        "checkedPaths": 0,
        "missingPaths": [],
        "staleMissingPaths": [],
        "wrongAppxPaths": [],
        "exactPathMismatches": [],
        "equivalentCodexCliAliases": [],
        "parseErrors": [],
        "samples": [],
        "currentAppxAvailable": bool(appx_install.get("available")),
        "currentAppxInstallPath": current_appx_path_text,
        "currentAppxVersion": str(appx_install.get("version") or ""),
        "currentAppxError": str(appx_install.get("error") or ""),
        "expectedPathsComplete": expected_paths is None or bool(
            all(str((expected_paths or {}).get(key) or "").strip() for key in [*required_path_keys, "codexHome"])
            and isinstance((expected_paths or {}).get("nodeModuleDirs"), list)
            and (expected_paths or {}).get("nodeModuleDirs")
        ),
        "configurationComplete": False,
    }

    def comparable_path(value: Any) -> str:
        try:
            resolved = str(Path(str(value)).expanduser().resolve(strict=False))
        except OSError:
            resolved = str(value)
        return os.path.normcase(resolved) if os.name == "nt" else resolved

    def inspect_entry(file_path: Path, entry_index: int, path_values: Any, protocol: str) -> None:
        result["entries"] += 1
        result[f"{protocol}Entries"] += 1
        entry_missing_paths: list[str] = []
        entry_wrong_appx_paths: list[str] = []
        entry_exact_mismatches: list[str] = []
        values = path_values if isinstance(path_values, dict) else {}
        if not isinstance(path_values, dict):
            result["parseErrors"].append(
                f"{file_path.name}[{entry_index}].paths must be an object"
                if protocol == "v2"
                else f"{file_path.name}[{entry_index}] must be an object"
            )
        for key in required_path_keys:
            value = str(values.get(key) or "").strip()
            sample_prefix = f"{file_path.name}[{entry_index}].{key}"
            if not value:
                missing_value = f"{sample_prefix}=<missing>"
                entry_missing_paths.append(missing_value)
                result["samples"].append(missing_value)
                continue
            expanded_value = os.path.expandvars(os.path.expanduser(value))
            result["checkedPaths"] += 1
            try:
                exists = Path(expanded_value).exists()
            except OSError:
                exists = False
            result["samples"].append(f"{sample_prefix}={value} exists={exists}")
            if not exists:
                entry_missing_paths.append(f"{sample_prefix}={value}")
            if key == "resourcesPath":
                expected_resources_path = current_appx_path / "app" / "resources" if current_appx_path else None
                appx_matches = bool(
                    appx_install.get("available")
                    and expected_resources_path
                    and comparable_path(expanded_value) == comparable_path(expected_resources_path)
                )
                if not appx_matches:
                    entry_wrong_appx_paths.append(
                        f"{sample_prefix}={value} current_appx={current_appx_path_text or '<unavailable>'}"
                    )
            expected_value = (expected_paths or {}).get(key)
            paths_match = not expected_value or comparable_path(expanded_value) == comparable_path(expected_value)
            if not paths_match and key == "codexCliPath":
                actual_cli_path = Path(expanded_value)
                expected_cli_path = Path(str(expected_value))
                paths_match = files_have_same_sha256(actual_cli_path, expected_cli_path)
                if paths_match:
                    result["equivalentCodexCliAliases"].append(
                        f"{sample_prefix}={value} equivalent={expected_value}"
                    )
            if not paths_match:
                entry_exact_mismatches.append(
                    f"{sample_prefix}={value} expected={expected_value}"
                )
        codex_home_value = str(values.get("codexHome") or "").strip()
        expected_codex_home = str((expected_paths or {}).get("codexHome") or codex_home_path)
        if (
            not codex_home_value
            or not Path(codex_home_value).is_dir()
            or comparable_path(codex_home_value) != comparable_path(expected_codex_home)
        ):
            entry_exact_mismatches.append(
                f"{file_path.name}[{entry_index}].codexHome={codex_home_value or '<missing>'} expected={expected_codex_home}"
            )
        if protocol == "v2" and expected_paths is not None:
            actual_module_dirs = values.get("nodeModuleDirs")
            expected_module_dirs = expected_paths.get("nodeModuleDirs")
            actual_module_keys = [comparable_path(value) for value in actual_module_dirs] if isinstance(actual_module_dirs, list) else []
            expected_module_keys = [comparable_path(value) for value in expected_module_dirs] if isinstance(expected_module_dirs, list) else []
            missing_module_dirs = [
                str(value)
                for value in actual_module_dirs or []
                if not Path(str(value)).is_dir()
            ] if isinstance(actual_module_dirs, list) else []
            entry_missing_paths.extend(
                f"{file_path.name}[{entry_index}].nodeModuleDirs={value}"
                for value in missing_module_dirs
            )
            if actual_module_keys != expected_module_keys:
                entry_exact_mismatches.append(
                    f"{file_path.name}[{entry_index}].nodeModuleDirs={actual_module_dirs!r} expected={expected_module_dirs!r}"
                )
        if entry_missing_paths or entry_wrong_appx_paths or entry_exact_mismatches:
            result["staleEntries"] += 1
            result["staleMissingPaths"].extend(entry_missing_paths)
            result["wrongAppxPaths"].extend(entry_wrong_appx_paths)
            result["exactPathMismatches"].extend(entry_exact_mismatches)
            return
        result["healthyEntries"] += 1
        result[f"healthy{protocol.upper()}Entries"] += 1
        codex_cli_path = str(values.get("codexCliPath") or "").strip()
        if codex_cli_path and codex_cli_path not in result["healthyCodexCliPaths"]:
            result["healthyCodexCliPaths"].append(codex_cli_path)

    for file_path in files:
        if not file_path.exists():
            continue
        result["existingFiles"].append(str(file_path))
        try:
            data = json.loads(file_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as error:
            result["parseErrors"].append(f"{file_path}: {error}")
            continue
        if file_path.name == "chrome-native-hosts-v2.json":
            entries = data.get("entries") if isinstance(data, dict) else []
            if not isinstance(entries, list):
                result["parseErrors"].append(f"{file_path}: entries must be an array")
                entries = []
            for entry_index, entry in enumerate(entries):
                path_values = entry.get("paths") if isinstance(entry, dict) else None
                inspect_entry(file_path, entry_index, path_values, "v2")
        else:
            entries = data.get("chromeNativeHosts") if isinstance(data, dict) else []
            if not isinstance(entries, list):
                result["parseErrors"].append(f"{file_path}: chromeNativeHosts must be an array")
                entries = []
            for entry_index, entry in enumerate(entries):
                inspect_entry(file_path, entry_index, entry, "v1")
    if not result["healthyEntries"]:
        result["missingPaths"] = [
            *result["staleMissingPaths"],
            *result["wrongAppxPaths"],
            *result["exactPathMismatches"],
        ]
    result["configurationComplete"] = bool(
        len(result["existingFiles"]) == 2
        and result["expectedPathsComplete"]
        and not result["parseErrors"]
        and result["v1Entries"] > 0
        and result["v2Entries"] > 0
        and result["healthyV1Entries"] == result["v1Entries"]
        and result["healthyV2Entries"] == result["v2Entries"]
        and result["staleEntries"] == 0
    )
    return result


def path_is_inside_directory(path_text: str | Path | None, directory_path: Path) -> bool:
    if not path_text:
        return False
    try:
        candidate_path = Path(str(path_text)).expanduser().resolve(strict=False)
        root_path = directory_path.expanduser().resolve(strict=False)
    except OSError:
        return False
    if os.name == "nt":
        candidate_text = os.path.normcase(str(candidate_path))
        root_text = os.path.normcase(str(root_path))
        return candidate_text == root_text or candidate_text.startswith(root_text.rstrip("\\/") + "\\")
    try:
        candidate_path.relative_to(root_path)
    except ValueError:
        return False
    return True


def inspect_chrome_native_messaging_manifest(
    manifest_path: Path,
    codex_home_path: Path,
    source: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": source,
        "manifestPath": str(manifest_path),
        "manifestExists": manifest_path.exists(),
        "hostPath": "",
        "hostExists": False,
        "hostInsideCodexHome": False,
        "parseError": "",
    }
    if not manifest_path.exists():
        return result
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as error:
        result["parseError"] = str(error)
        return result
    host_path_text = str(data.get("path") or "") if isinstance(data, dict) else ""
    result["hostPath"] = host_path_text
    if host_path_text:
        host_path = Path(host_path_text)
        result["hostExists"] = host_path.exists()
        result["hostInsideCodexHome"] = path_is_inside_directory(host_path, codex_home_path)
    return result


def read_windows_chrome_native_messaging_registry_entries() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    registry_roots = [
        ("HKCU", winreg.HKEY_CURRENT_USER),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE),
    ]
    registry_paths = [
        r"Software\Google\Chrome\NativeMessagingHosts\com.openai.codexextension",
        r"Software\Chromium\NativeMessagingHosts\com.openai.codexextension",
        r"Software\Microsoft\Edge\NativeMessagingHosts\com.openai.codexextension",
    ]
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for root_label, root_key in registry_roots:
        for registry_path in registry_paths:
            try:
                with winreg.OpenKey(root_key, registry_path) as key:
                    manifest_path_text, _ = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            source = f"{root_label}\\{registry_path}"
            key = (source.lower(), str(manifest_path_text).lower())
            if key in seen:
                continue
            seen.add(key)
            entries.append({"source": source, "manifestPath": str(manifest_path_text)})
    return entries


def scan_chrome_native_messaging_manifests(codex_home_path: Path) -> dict[str, Any]:
    registry_entries = read_windows_chrome_native_messaging_registry_entries()
    inspections: list[dict[str, Any]] = []
    seen_manifest_paths: set[str] = set()
    for entry in registry_entries:
        manifest_path_text = str(entry.get("manifestPath") or "")
        if not manifest_path_text:
            continue
        comparable_path = manifest_path_text.lower() if os.name == "nt" else manifest_path_text
        if comparable_path in seen_manifest_paths:
            continue
        seen_manifest_paths.add(comparable_path)
        inspections.append(
            inspect_chrome_native_messaging_manifest(
                Path(manifest_path_text),
                codex_home_path,
                str(entry.get("source") or "registry"),
            )
        )

    missing_manifests: list[str] = []
    parse_errors: list[str] = []
    missing_host_paths: list[str] = []
    foreign_home_host_paths: list[str] = []
    samples: list[str] = []
    manifest_files: list[str] = []
    for inspection in inspections:
        manifest_path_text = str(inspection.get("manifestPath") or "")
        source = str(inspection.get("source") or "")
        if bool(inspection.get("manifestExists")):
            manifest_files.append(manifest_path_text)
        else:
            missing_manifests.append(f"{source}: manifest={manifest_path_text}")
        parse_error = str(inspection.get("parseError") or "")
        if parse_error:
            parse_errors.append(f"{source}: manifest={manifest_path_text} error={parse_error}")
        host_path_text = str(inspection.get("hostPath") or "")
        if host_path_text and not bool(inspection.get("hostExists")):
            missing_host_paths.append(f"{source}: host={host_path_text}")
        if host_path_text and bool(inspection.get("hostExists")) and not bool(inspection.get("hostInsideCodexHome")):
            foreign_home_host_paths.append(f"{source}: host={host_path_text}")
        samples.append(
            f"{source}: manifest={manifest_path_text} host={host_path_text or '-'} "
            f"host_exists={bool(inspection.get('hostExists'))} "
            f"inside_codex_home={bool(inspection.get('hostInsideCodexHome'))}"
        )

    return {
        "registryEntries": registry_entries,
        "manifestCount": len(inspections),
        "manifestFiles": manifest_files,
        "missingManifestPaths": missing_manifests,
        "parseErrors": parse_errors,
        "missingHostPaths": missing_host_paths,
        "foreignHomeHostPaths": foreign_home_host_paths,
        "samples": samples,
    }


def scan_curated_marketplace_manifest_warnings(codex_home_path: Path) -> dict[str, Any]:
    marketplace_plugins_path = codex_home_path / ".tmp" / "plugins" / "plugins"
    result: dict[str, Any] = {
        "root": str(marketplace_plugins_path),
        "rootExists": marketplace_plugins_path.exists(),
        "scannedManifests": 0,
        "invalidPrompts": [],
        "invalidIconPaths": [],
        "parseErrors": [],
        "pluginNames": [],
        "samples": [],
    }
    if not marketplace_plugins_path.exists():
        return result
    try:
        manifest_paths = sorted(marketplace_plugins_path.glob("*/.codex-plugin/plugin.json"))
    except OSError as error:
        result["parseErrors"].append(f"{marketplace_plugins_path}: {error}")
        return result

    def scan_default_prompts(plugin_name: str, manifest_path: Path, label: str, prompts: Any) -> None:
        if not isinstance(prompts, list):
            return
        for prompt_index, prompt_value in enumerate(prompts):
            if isinstance(prompt_value, str) and len(prompt_value) > 128:
                result["invalidPrompts"].append(
                    f"{plugin_name}:{label}[{prompt_index}] length={len(prompt_value)} path={manifest_path}"
                )

    for manifest_path in manifest_paths:
        plugin_name = manifest_path.parent.parent.name
        result["pluginNames"].append(plugin_name)
        result["scannedManifests"] += 1
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as error:
            result["parseErrors"].append(f"{plugin_name}: {error} path={manifest_path}")
            continue
        if not isinstance(data, dict):
            result["parseErrors"].append(f"{plugin_name}: manifest root is not an object path={manifest_path}")
            continue

        interface_data = data.get("interface") if isinstance(data.get("interface"), dict) else {}
        scan_default_prompts(plugin_name, manifest_path, "defaultPrompt", data.get("defaultPrompt"))
        scan_default_prompts(plugin_name, manifest_path, "interface.defaultPrompt", interface_data.get("defaultPrompt"))
        for icon_key in ("icon_small", "icon_large", "composerIcon", "logo"):
            icon_path = interface_data.get(icon_key)
            if not isinstance(icon_path, str) or not icon_path:
                continue
            path_parts = [part for part in icon_path.replace("\\", "/").split("/") if part]
            if ".." in path_parts:
                result["invalidIconPaths"].append(
                    f"{plugin_name}:interface.{icon_key} path={icon_path} manifest={manifest_path}"
                )
        if len(result["samples"]) < 10:
            result["samples"].append(f"{plugin_name}: {manifest_path}")
    return result


def recent_session_rollout_paths(codex_home_path: Path, max_paths: int = 60) -> list[Path]:
    sessions_path = codex_home_path / "sessions"
    if not sessions_path.exists():
        return []
    rollout_paths: list[Path] = []
    try:
        for root_path, directory_names, file_names in os.walk(sessions_path):
            directory_names[:] = [name for name in directory_names if not name.startswith(".")]
            for file_name in file_names:
                if file_name.endswith(".jsonl"):
                    rollout_paths.append(Path(root_path) / file_name)
    except OSError:
        return []
    def modified_time(path: Path) -> float:
        try:
            return path.stat().st_mtime if path.exists() else 0
        except OSError:
            return 0

    rollout_paths.sort(key=modified_time, reverse=True)
    return rollout_paths[:max_paths]


runtime_blocker_patterns: list[tuple[str, re.Pattern[str]]] = [
    (
        "node_repl_transport_closed",
        re.compile(
            r"node_repl/(?:js|js_reset)[\s\S]{0,220}(Transport closed|tool call failed)"
            r"|tool call failed[\s\S]{0,160}`?node_repl/(?:js|js_reset)`?"
            r"|Transport closed[\s\S]{0,180}node_repl/(?:js|js_reset)",
            re.IGNORECASE,
        ),
    ),
    (
        "browser_or_computer_use_not_exposed",
        re.compile(r"(没有暴露|未暴露|not exposed|not available|unavailable).{0,120}(Browser|IAB|Computer Use|computer-use|browser)", re.IGNORECASE),
    ),
    (
        "browser_iab_control_missing",
        re.compile(r"(Browser/IAB|Browser|IAB).{0,120}(控制工具|控制能力|not exposed|not available|missing)", re.IGNORECASE),
    ),
    (
        "node_repl_unwritable",
        re.compile(r"node_repl.{0,140}(无法写入|不能写入|写入失败|cannot write|not writable|unwritable)", re.IGNORECASE),
    ),
    (
        "node_global_websocket_missing",
        re.compile(
            r"ReferenceError:\s*WebSocket is not defined"
            r"|(?:失败|报错|直接报|failed|error|exception|threw).{0,120}WebSocket is not defined"
            r"|global(?:This)?\.?WebSocket.{0,120}(missing|undefined|not defined)",
            re.IGNORECASE,
        ),
    ),
    (
        "skill_path_missing",
        re.compile(r"(SKILL\.md|技能文件路径|skill file path).{0,160}(不存在|缺失|不一致|missing|not found|not installed)", re.IGNORECASE),
    ),
    (
        "plugin_cache_mismatch",
        re.compile(r"(插件缓存|plugin cache).{0,160}(不一致|缺失|missing|not installed|metadata)", re.IGNORECASE),
    ),
]
runtime_blocker_trigger_terms = (
    "node_repl",
    "transport closed",
    "websocket",
    "browser",
    "iab",
    "computer use",
    "computer-use",
    "skill.md",
    "skill file path",
    "技能文件路径",
    "插件缓存",
    "plugin cache",
    "没有暴露",
    "未暴露",
    "not exposed",
    "not available",
)
diagnostic_process_markers_lower = tuple(
    marker.lower()
    for marker in (
        "hard evidence",
        "diagnostics API",
        "runtime failure",
        "diagnostic process",
        "diagnostic evidence",
        "not a current failure",
        "not current file missing",
        "plugin files themselves have been fixed",
        "已经有硬证据",
        "体检接口",
        "体检 API",
        "体检现在能报出",
        "诊断过程",
        "作为运行时故障报出来",
        "不是当前文件缺失",
        "不是当前故障",
        "插件文件本身已经修好",
        "本地插件缓存现在已经健康",
        "实际路径已经回读验证存在",
        "本地只读审计流程",
        "避免误把旧结果当现状",
    )
)
runtime_noise_markers_lower = tuple(
    marker.lower()
    for marker in (
        "误报",
        "测试会覆盖",
        "匹配条件",
        "正则",
        "算作问题",
        "不把",
        "新增扫描",
        "回归测试",
        "纳入体检规则",
        "运行时错误模式",
        "记录格式",
        "工具输出",
        "精准 MCP",
        "补测试",
        "体检捕获",
        "漏了这一类",
        "tool_search",
        "可调用工具面",
        "会话启动时注入",
        "重新注入",
        "Playwright fallback",
        "Playwright 质量门禁",
        "质量门禁覆盖",
        "artifact_browser",
        "source_sha256",
        "skipped_large_or_missing",
        "hash",
        "ensure_ascii",
        "scanned_paths=",
        "截图里",
        "截图里的",
        "用户截图",
        "当前是好的",
        "当前能跑",
        "能导入",
        "没有 transport closed",
        "no transport closed",
        "如果体检不报",
        "体检不报",
        "补成专门的诊断项",
        "专门的诊断项",
        "工具降级说法",
        "容易漏报",
        "扫描范围",
        "扫描入口",
        "node_repl_transport_closed:",
        "issue 还太泛",
        "单独生成",
        "不是简单的插件文件缺失",
        "源码体检",
        "运行版 API 验证",
        "pattern",
        "regex",
        "体检新增",
        "新增 `scripts/node_websocket_compat",
        "诊断扫描也会识别",
        "加入运行时阻塞识别",
        "能发现",
        "这类错误",
        "兼容模块",
        "回退模块",
        "回退包",
        "覆盖最近日志",
        "确保体检",
        "专门问题",
        "由于当前没有专用",
        "我会用项目已有 playwright",
        "临时内联测量脚本",
        "不新增文件",
        "会改动 `d:\\.codex` 下的运行配置/插件缓存",
        "改动 `d:\\.codex` 下的运行配置/插件缓存",
        "先复制缺失的官方 `sites` 插件包",
        "实际存在的 codex/node 路径",
        "我先读它的约束",
        "再决定是否走 node/playwright 校验",
        "改用 node repl + playwright 做结构回归",
        "范围只测页面布局与溢出",
        "不排除真实的 `not installed`",
        "不排除真实的 not installed",
        "不排除真实的 `websocket is not defined`",
        "不排除真实的 websocket is not defined",
    )
)


runtime_noise_markers_lower += tuple(
    marker.lower()
    for marker in (
        "websocket is not defined` compat",
        "websocket is not defined compatibility",
        "websocket compatibility layer",
        "websocket compat layer",
        "compatibility layer",
        "websocket fallback",
        "ws fallback",
        "ws@",
        "confirm `websocket is not defined`",
        "verify `websocket is not defined`",
        "diagnostic item",
        "diagnostics item",
        "diagnostics api",
        "runtime blocker scan",
        "`websocket is not defined` 的兼容层",
        "websocket is not defined 的兼容层",
        "确认 `websocket is not defined`",
        "复核一遍",
        "保留真正的失败语句",
        "不会吞掉真正的",
        "不会吞掉真实",
        "确认新过滤",
        "测试用例已经补上",
        "运行时体检相关测试",
        "产品体检项",
        "体检项",
        "兼容层",
        "兼容入口",
        "ws 兼容",
        "没有全局 `websocket`",
        "没有全局 websocket",
        "不能依赖 node 自带能力",
        "iab 运行时已绑定成功",
        "browser/iab 控制链路已连上",
        "browser/iab 控制工具已暴露且可连接",
        "browser 控制工具现在已暴露",
        "控制工具现在已暴露",
        "已找到 browser 控制工具",
        "已确认 browser 技能",
        "已恢复的浏览器控制链路",
        "可以直接用该接口",
        "继续用真实浏览器",
        "控制工具可用",
        "通过插件暴露出来",
        "不是“未暴露”状态",
        "不是\"未暴露\"状态",
        "已连到 codex in-app browser",
        "当前会话真正可调用",
        "修到可验证状态",
        "实际可用能力",
        "不是口头说",
        "不能把 “没有 browser/iab",
        "不能把 \"没有 browser/iab",
        "不再把“没有 browser/iab",
        "不再把\"没有 browser/iab",
        "不能再说“当前没有 browser/iab",
        "不能再说\"当前没有 browser/iab",
        "不再把“没有暴露",
        "不再把\"没有暴露",
        "不会再把“没有暴露",
        "不会再把\"没有暴露",
        "先修这个能力缺口",
        "实际情况不是没安装",
        "入口名是",
        "创建一个同包内入口别名",
        "no longer treat",
        "not a blocker",
        "bootstrap 已经成功",
        "不是当前会话的工具不可用问题",
        "控制工具已加载",
        "若插件连接失败",
        "如果插件连接失败",
        "插件连接失败，会按",
        "先尝试绑定内置浏览器",
    )
)


def is_targeted_runtime_tool_error(text: str) -> bool:
    stripped_text = text.strip()
    direct_error = re.search(
        r"^tool call error:\s*tool call failed for `node_repl/(?:js|js_reset)`[\s\S]{0,220}Transport closed",
        stripped_text,
        re.IGNORECASE,
    )
    wrapped_tool_output = re.search(
        r"^(?:Wall time:[^\n]*\n)?Output:\s*\[\{\"type\":\"text\",\"text\":\"tool call error:\s*tool call failed for `node_repl/(?:js|js_reset)`[\s\S]{0,260}Transport closed",
        stripped_text,
        re.IGNORECASE,
    )
    return bool(direct_error or wrapped_tool_output)


def is_direct_runtime_blocker_output(text: str) -> bool:
    return is_targeted_runtime_tool_error(text) or bool(
        re.search(
            r"ReferenceError:\s*WebSocket is not defined"
            r"|(?:失败|报错|直接报|failed|error|exception|threw).{0,120}WebSocket is not defined",
            text,
            re.IGNORECASE,
        )
    )


def extract_runtime_message_texts(record: dict[str, Any]) -> list[str]:
    record_type = str(record.get("type") or "")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    texts: list[str] = []

    if record_type == "user_message":
        text = payload.get("text")
        if isinstance(text, str):
            texts.append(text)

    if record_type == "event_msg":
        payload_type = str(payload.get("type") or "")
        message = payload.get("message")
        if payload_type in {"agent_message", "user_message"} and isinstance(message, str):
            texts.append(message)
        result = payload.get("result")
        if payload_type == "mcp_tool_call_end" and isinstance(result, dict):
            error_text = result.get("Err")
            if isinstance(error_text, str) and is_targeted_runtime_tool_error(error_text):
                texts.append(error_text)

    if record_type == "response_item":
        item_type = str(payload.get("type") or "")
        if item_type == "function_call_output":
            output_text = payload.get("output")
            if isinstance(output_text, str) and is_direct_runtime_blocker_output(output_text):
                texts.append(output_text)
            return texts
        if item_type in {"function_call", "function_call_output", "tool_call", "tool_result"}:
            return texts
        content_items = payload.get("content")
        if isinstance(content_items, list):
            for content_item in content_items:
                if not isinstance(content_item, dict):
                    continue
                content_type = str(content_item.get("type") or "")
                text = content_item.get("text")
                if content_type in {"output_text", "text"} and isinstance(text, str):
                    texts.append(text)
        text = payload.get("text")
        if isinstance(text, str):
            texts.append(text)

    return texts


def read_runtime_message_texts_from_jsonl(path: Path, edge_bytes: int = 260_000) -> list[str]:
    text = read_text_edges(path, edge_bytes=edge_bytes)
    if not text:
        return []
    messages: list[str] = []
    for line in text.splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line == "...":
            continue
        try:
            record = json.loads(stripped_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        messages.extend(extract_runtime_message_texts(record))
    return messages


def is_runtime_blocker_noise(text: str) -> bool:
    normalized_text = re.sub(r"\s+", " ", text).strip()
    normalized_text_lower = normalized_text.lower()
    if is_targeted_runtime_tool_error(normalized_text):
        return False
    if any(marker in normalized_text_lower for marker in diagnostic_process_markers_lower):
        return True
    return any(marker in normalized_text_lower for marker in runtime_noise_markers_lower)


def scan_recent_runtime_blocker_messages(codex_home_path: Path, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    source_paths = recent_snapshot_rollout_paths(snapshot, max_paths=24)
    seen: set[str] = {
        str(path.resolve(strict=False)).lower() if os.name == "nt" else str(path.resolve(strict=False))
        for path in source_paths
    }
    for path in recent_session_rollout_paths(codex_home_path, max_paths=60):
        normalized_key = str(path.resolve(strict=False)).lower() if os.name == "nt" else str(path.resolve(strict=False))
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        source_paths.append(path)
        if len(source_paths) >= 72:
            break

    findings: list[str] = []
    scanned_paths: list[str] = []
    for source_path in source_paths:
        if not source_path.exists() or not source_path.is_file():
            continue
        scanned_paths.append(str(source_path))
        message_texts = read_runtime_message_texts_from_jsonl(source_path, edge_bytes=1_000_000)
        if not message_texts:
            continue
        normalized_texts = [
            normalize_skill_reference_text(text)
            for text in message_texts
            if text
            and any(term in text.lower() for term in runtime_blocker_trigger_terms)
            and not is_runtime_blocker_noise(text)
        ]
        for message_index, normalized_text in enumerate(normalized_texts):
            for label, pattern in runtime_blocker_patterns:
                for match in pattern.finditer(normalized_text):
                    snippet = re.sub(r"\s+", " ", normalized_text[max(0, match.start() - 120): match.end() + 160]).strip()
                    findings.append(f"{label}: {source_path}#message-{message_index + 1} | {snippet[:520]}")
                    break
                if len(findings) >= 20:
                    break
            if len(findings) >= 20:
                break
        if len(findings) >= 20:
            break
    return {
        "scannedPaths": scanned_paths,
        "findings": findings,
        "truncated": len(findings) >= 20,
    }


def read_text_tail(path: Path, tail_bytes: int = 1_200_000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        size_bytes = path.stat().st_size
        with path.open("rb") as handle:
            if size_bytes > tail_bytes:
                handle.seek(max(0, size_bytes - tail_bytes))
            content = handle.read(tail_bytes)
    except OSError:
        return ""
    return content.decode("utf-8", errors="replace")


def structured_context_window_error(record: dict[str, Any]) -> tuple[str, str]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    record_type = str(record.get("type") or "").casefold()
    payload_type = str(payload.get("type") or "").casefold()
    structured_error_types = {
        "error",
        "request_error",
        "request_failed",
        "stream_error",
        "task_failed",
        "turn_error",
        "turn_failed",
    }
    structured_type = payload_type if payload_type in structured_error_types else record_type
    if structured_type not in structured_error_types:
        return "", ""

    candidate_values: list[Any] = [
        payload.get("message"),
        payload.get("error"),
        payload.get("detail"),
        payload.get("text"),
        record.get("message"),
        record.get("error"),
        record.get("detail"),
    ]
    for value in list(candidate_values):
        if isinstance(value, dict):
            candidate_values.extend(value.get(key) for key in ("message", "error", "detail", "text", "code"))
    for value in candidate_values:
        signature = context_window_error_signature(value)
        if signature:
            return structured_type, signature
    return "", ""


def record_resolves_context_window_error(record: dict[str, Any]) -> bool:
    record_type = str(record.get("type") or "").casefold()
    if record_type == "compacted":
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        return isinstance(payload.get("replacement_history"), list)
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "").casefold()
    if record_type == "response_item" and payload_type == "message" and str(payload.get("role") or "").casefold() == "assistant":
        return bool(response_item_message_text(payload))
    if record_type != "event_msg":
        return False
    if payload_type == "agent_message":
        return bool(str(payload.get("message") or payload.get("text") or "").strip())
    if payload_type == "task_complete":
        return bool(str(payload.get("last_agent_message") or "").strip())
    return False


def scan_recent_context_window_exhaustion(codex_home_path: Path, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    threads_by_rollout_path: dict[str, dict[str, Any]] = {}
    if snapshot:
        for thread in snapshot.get("threads", []):
            rollout_text = str(thread.get("rolloutPath") or "")
            if not rollout_text:
                continue
            normalized_key = str(Path(rollout_text).resolve(strict=False)).lower() if os.name == "nt" else str(Path(rollout_text).resolve(strict=False))
            threads_by_rollout_path[normalized_key] = thread

    source_paths = recent_snapshot_rollout_paths(snapshot, max_paths=32)
    seen: set[str] = {
        str(path.resolve(strict=False)).lower() if os.name == "nt" else str(path.resolve(strict=False))
        for path in source_paths
    }
    for path in recent_session_rollout_paths(codex_home_path, max_paths=80):
        normalized_key = str(path.resolve(strict=False)).lower() if os.name == "nt" else str(path.resolve(strict=False))
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        source_paths.append(path)
        if len(source_paths) >= 90:
            break

    findings: list[str] = []
    affected_paths: list[str] = []
    scanned_paths: list[str] = []
    for source_path in source_paths:
        if not source_path.exists() or not source_path.is_file():
            continue
        scanned_paths.append(str(source_path))
        normalized_key = str(source_path.resolve(strict=False)).lower() if os.name == "nt" else str(source_path.resolve(strict=False))
        thread = threads_by_rollout_path.get(normalized_key, {})
        thread_label = str(thread.get("title") or source_path.stem)
        thread_id = str(thread.get("id") or "")
        text = read_text_tail(source_path)
        if not text:
            continue
        pending_structured_error: tuple[int, str, str] | None = None
        for tail_line_index, line in enumerate(text.splitlines(), start=1):
            stripped_line = line.strip()
            if not stripped_line or stripped_line == "...":
                continue
            try:
                record = json.loads(stripped_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            structured_type, signature = structured_context_window_error(record)
            if structured_type and signature:
                pending_structured_error = (tail_line_index, structured_type, signature)
                continue
            if pending_structured_error and record_resolves_context_window_error(record):
                pending_structured_error = None
        if pending_structured_error:
            error_line, structured_type, signature = pending_structured_error
            findings.append(
                f"{thread_label} | {thread_id or '-'} | structured={structured_type} signature={signature} line_tail={error_line} | {source_path}"
            )
            affected_paths.append(str(source_path))
        if len(findings) >= 12:
            break
    return {
        "scannedPaths": scanned_paths,
        "findings": findings,
        "affectedPaths": affected_paths,
    }


def parse_mcp_process_snapshot(processes: list[dict[str, Any]]) -> dict[str, Any]:
    generic_mcp_processes: list[dict[str, Any]] = []
    xcodebuild_mcp_processes: list[dict[str, Any]] = []
    node_repl_processes: list[dict[str, Any]] = []
    extension_host_processes: list[dict[str, Any]] = []
    legacy_thread_messenger_processes: list[dict[str, Any]] = []
    process_keys: dict[int, str] = {}
    for process_index, process in enumerate(processes):
        process_id = str(process.get("ProcessId") or process.get("pid") or "").strip()
        process_keys[id(process)] = f"pid:{process_id}" if process_id else f"index:{process_index}"
        command_line = str(process.get("CommandLine") or process.get("commandLine") or "")
        command_lower = command_line.lower().replace("\\", "/")
        name_lower = str(process.get("Name") or process.get("name") or "").lower()
        if "./mcp/server.mjs" in command_lower:
            generic_mcp_processes.append(process)
        if "codex-thread-messenger" in command_lower or "codex_thread_messenger" in command_lower:
            legacy_thread_messenger_processes.append(process)
        if "xcodebuildmcp" in command_lower:
            xcodebuild_mcp_processes.append(process)
        if "node_repl" in command_lower or "node_repl" in name_lower:
            node_repl_processes.append(process)
        if "extension-host" in command_lower or "extension-host" in name_lower:
            extension_host_processes.append(process)
    generic_mcp_count = len(generic_mcp_processes)
    xcodebuild_mcp_count = len(xcodebuild_mcp_processes)
    node_repl_count = len(node_repl_processes)
    node_repl_without_disable_sandbox = [
        process
        for process in node_repl_processes
        if "--disable-sandbox" not in str(process.get("CommandLine") or process.get("commandLine") or "")
    ]
    node_repl_with_disable_sandbox = [
        process
        for process in node_repl_processes
        if "--disable-sandbox" in str(process.get("CommandLine") or process.get("commandLine") or "")
    ]
    xcodebuild_mcp_process_ids = {str(process.get("ProcessId") or process.get("pid") or "") for process in xcodebuild_mcp_processes}
    xcodebuild_mcp_roots = [
        process
        for process in xcodebuild_mcp_processes
        if str(process.get("ParentProcessId") or process.get("parentPid") or "") not in xcodebuild_mcp_process_ids
    ]
    xcodebuild_mcp_root_count = len(xcodebuild_mcp_roots)
    generic_mcp_keys = {process_keys[id(process)] for process in generic_mcp_processes}
    legacy_thread_messenger_keys = {process_keys[id(process)] for process in legacy_thread_messenger_processes}
    xcodebuild_mcp_keys = {process_keys[id(process)] for process in xcodebuild_mcp_processes}
    node_repl_keys = {process_keys[id(process)] for process in node_repl_processes}
    node_repl_risk_keys = {process_keys[id(process)] for process in node_repl_with_disable_sandbox}
    other_mcp_server_keys = generic_mcp_keys - legacy_thread_messenger_keys - xcodebuild_mcp_keys - node_repl_keys
    mcp_process_keys = generic_mcp_keys | legacy_thread_messenger_keys | xcodebuild_mcp_keys | node_repl_keys
    risk_process_keys = legacy_thread_messenger_keys | xcodebuild_mcp_keys | node_repl_risk_keys
    generic_mcp_parent_counts = Counter(
        str(process.get("ParentProcessId") or process.get("parentPid") or "unknown")
        for process in generic_mcp_processes
    )
    max_generic_mcp_per_parent = max(generic_mcp_parent_counts.values(), default=0)
    capacity_high = generic_mcp_count >= 32 or node_repl_count >= 16
    warning = (
        xcodebuild_mcp_root_count >= 4
        or bool(legacy_thread_messenger_keys)
        or bool(node_repl_risk_keys)
    )
    return {
        "available": True,
        "warning": warning,
        "capacityHigh": capacity_high,
        "mcpProcessCount": len(mcp_process_keys),
        "riskProcessCount": len(risk_process_keys),
        "genericMcpServerCount": generic_mcp_count,
        "maxGenericMcpPerParent": max_generic_mcp_per_parent,
        "otherMcpServerProcessCount": len(other_mcp_server_keys),
        "legacyThreadMessengerProcessCount": len(legacy_thread_messenger_processes),
        "xcodebuildMcpProcessCount": xcodebuild_mcp_count,
        "xcodebuildMcpRootCount": xcodebuild_mcp_root_count,
        "nodeReplProcessCount": node_repl_count,
        "normalNodeReplProcessCount": len(node_repl_without_disable_sandbox),
        "nodeReplRiskProcessCount": len(node_repl_with_disable_sandbox),
        "nodeReplWithoutDisableSandboxCount": len(node_repl_without_disable_sandbox),
        "nodeReplWithDisableSandboxCount": len(node_repl_with_disable_sandbox),
        "nodeReplProcesses": [
            {
                "pid": str(process.get("ProcessId") or process.get("pid") or ""),
                "name": str(process.get("Name") or process.get("name") or ""),
                "commandLine": str(process.get("CommandLine") or process.get("commandLine") or ""),
            }
            for process in node_repl_processes
        ],
        "extensionHostProcessCount": len(extension_host_processes),
        "sampleProcesses": [
            {
                "pid": str(process.get("ProcessId") or process.get("pid") or ""),
                "parentPid": str(process.get("ParentProcessId") or process.get("parentPid") or ""),
                "name": str(process.get("Name") or process.get("name") or ""),
                "commandLine": str(process.get("CommandLine") or process.get("commandLine") or "")[:240],
            }
            for process in [*legacy_thread_messenger_processes[:4], *generic_mcp_processes[:4], *xcodebuild_mcp_roots[:4], *node_repl_processes[:2]]
        ],
    }


def node_repl_process_command_mismatches(
    process_snapshot: dict[str, Any],
    effective_command: str,
) -> list[str]:
    node_repl_processes = list(process_snapshot.get("nodeReplProcesses") or [])
    if not node_repl_processes:
        return []
    expected_command = effective_command.strip().strip('"').replace("\\", "/").casefold()
    if not expected_command:
        return ["effective node_repl command is empty"]
    expected_is_path = "/" in expected_command
    expected_name = expected_command.rsplit("/", 1)[-1]
    mismatches: list[str] = []
    for process in node_repl_processes:
        command_line = str(process.get("commandLine") or "").replace("\\", "/").casefold()
        command_matches = expected_command in command_line if expected_is_path else expected_name in command_line
        if not command_matches:
            mismatches.append(
                f"pid={process.get('pid') or '-'} expected={effective_command!r} actual={process.get('commandLine') or ''!r}"
            )
    return mismatches


def node_repl_matching_processes(
    process_snapshot: dict[str, Any],
    effective_command: str,
) -> list[dict[str, Any]]:
    expected_command = effective_command.strip().strip('"').replace("\\", "/").casefold()
    if not expected_command:
        return []
    expected_is_path = "/" in expected_command
    expected_name = expected_command.rsplit("/", 1)[-1]
    return [
        process
        for process in list(process_snapshot.get("nodeReplProcesses") or [])
        if (
            expected_command
            if expected_is_path
            else expected_name
        )
        in str(process.get("commandLine") or "").replace("\\", "/").casefold()
    ]


def inspect_computer_use_pipe_endpoint(pipe_path_text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": pipe_path_text,
        "kind": "",
        "exists": False,
        "accessible": False,
        "ready": False,
        "error": "",
        "handshakeVerified": False,
    }
    if not pipe_path_text:
        result["error"] = "SKY_CUA_NATIVE_PIPE_DIRECTORY is missing"
        return result

    expanded_path_text = os.path.expandvars(os.path.expanduser(pipe_path_text))
    normalized_path_text = expanded_path_text.replace("/", "\\")
    if normalized_path_text.casefold().startswith("\\\\.\\pipe\\"):
        result["kind"] = "windows_named_pipe"
        if os.name != "nt":
            result["error"] = "Windows named-pipe availability cannot be checked on this platform"
            return result
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            wait_named_pipe = kernel32.WaitNamedPipeW
            wait_named_pipe.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            wait_named_pipe.restype = ctypes.c_int
            if wait_named_pipe(expanded_path_text, 50):
                result.update({"exists": True, "accessible": True, "ready": True})
                return result
            error_code = ctypes.get_last_error()
            if error_code == 121:
                result["exists"] = True
                result["error"] = "named pipe exists but no instance became available"
            elif error_code == 5:
                result["exists"] = True
                result["error"] = "named pipe access was denied"
            elif error_code in {2, 3}:
                result["error"] = "named pipe endpoint does not exist"
            else:
                result["error"] = f"WaitNamedPipeW failed with Windows error {error_code}"
        except (AttributeError, OSError, ValueError) as error:
            result["error"] = f"named pipe availability probe failed: {error}"
        return result

    result["kind"] = "filesystem_directory"
    pipe_path = Path(expanded_path_text)
    try:
        result["exists"] = pipe_path.exists() and pipe_path.is_dir()
        if not result["exists"]:
            result["error"] = "pipe directory does not exist or is not a directory"
            return result
        with os.scandir(pipe_path):
            pass
        result.update({"accessible": True, "ready": True})
    except OSError as error:
        result["error"] = f"pipe directory is inaccessible: {error}"
    return result


def scan_mcp_process_snapshot() -> dict[str, Any]:
    global mcp_process_snapshot_cache
    if os.name != "nt":
        return {"available": False, "warning": False, "error": "Windows process command-line scan is only available on Windows."}
    now = time.monotonic()
    with diagnostics_runtime_cache_lock:
        cache_epoch = diagnostics_runtime_cache_epoch
        cached_entry = mcp_process_snapshot_cache
        if cached_entry and cached_entry[0] == cache_epoch and now - cached_entry[1] < 20:
            return cached_entry[2]

    def remember(result: dict[str, Any]) -> dict[str, Any]:
        global mcp_process_snapshot_cache

        with diagnostics_runtime_cache_lock:
            if cache_epoch == diagnostics_runtime_cache_epoch:
                mcp_process_snapshot_cache = (cache_epoch, time.monotonic(), result)
        return result

    processes: list[dict[str, Any]] = []
    patterns = ("./mcp/server.mjs", "codex-thread-messenger", "codex_thread_messenger", "xcodebuildmcp", "node_repl", "extension-host")
    command_line_candidate_names = {
        "node.exe",
        "cmd.exe",
        "codex.exe",
        "node_repl.exe",
        "codexhomemanagerlocal.exe",
        "codex-home-manager-local-win-x64.exe",
    }
    try:
        import psutil
    except Exception:
        psutil = None  # type: ignore[assignment]

    try:
        for process in list_windows_processes():
            process_id = str(process.get("pid") or "")
            name_text = str(process.get("imageName") or process.get("Name") or "")
            name_lower = name_text.lower()
            parent_process_id = str(process.get("parentPid") or process.get("ParentProcessId") or "")
            command_line = ""
            needs_command_line = (
                name_lower in command_line_candidate_names
                or "node" in name_lower
                or "codex" in name_lower
                or "extension-host" in name_lower
            )
            if needs_command_line and psutil is not None:
                try:
                    command_line = " ".join(str(part) for part in psutil.Process(int(process_id)).cmdline())
                except Exception:
                    command_line = ""
            command_lower = command_line.lower().replace("\\", "/")
            if not any(pattern in command_lower or pattern in name_lower for pattern in patterns):
                continue
            processes.append(
                {
                    "ProcessId": process_id,
                    "ParentProcessId": parent_process_id,
                    "Name": name_text,
                    "CommandLine": command_line,
                }
            )
    except Exception as error:
        result = {"available": False, "warning": False, "error": str(error)}
        return remember(result)

    result = parse_mcp_process_snapshot(processes)
    return remember(result)


def directory_size(path: Path, max_files: int = 40_000) -> dict[str, Any]:
    result = {"exists": path.exists(), "sizeBytes": 0, "fileCount": 0, "truncated": False}
    if not path.exists():
        return result
    skipped_directories = {"node_modules", ".git", "__pycache__"}
    for root_path, directory_names, file_names in os.walk(path):
        directory_names[:] = [name for name in directory_names if name not in skipped_directories]
        for file_name in file_names:
            file_path = Path(root_path) / file_name
            try:
                result["sizeBytes"] += file_path.stat().st_size
                result["fileCount"] += 1
            except OSError:
                continue
            if result["fileCount"] >= max_files:
                result["truncated"] = True
                return result
    return result


def find_codex_cli_candidates() -> list[str]:
    path_text = os.environ.get("PATH", "")
    if os.name == "nt":
        suffixes = [suffix.lower() for suffix in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if suffix]
        suffixes = ["", *suffixes]
    else:
        suffixes = [""]

    candidates: list[str] = []
    seen: set[str] = set()
    for directory_text in path_text.split(os.pathsep):
        if not directory_text:
            continue
        directory_path = Path(directory_text)
        for suffix in suffixes:
            candidate_path = directory_path / f"codex{suffix}"
            try:
                is_candidate = candidate_path.is_file()
            except OSError:
                continue
            if not is_candidate:
                continue
            normalized_key = str(candidate_path).lower() if os.name == "nt" else str(candidate_path)
            if normalized_key in seen:
                continue
            seen.add(normalized_key)
            candidates.append(str(candidate_path))
    return candidates


def inspect_codex_cli_candidate(candidate_text: str) -> dict[str, Any]:
    candidate_path = Path(candidate_text)
    result: dict[str, Any] = {
        "path": candidate_text,
        "exists": candidate_path.exists() and candidate_path.is_file(),
        "officialNpmWrapper": False,
        "configForwarder": False,
        "packageVersion": "",
        "packagePath": "",
        "error": "",
    }
    if not result["exists"]:
        return result
    try:
        wrapper_text = candidate_path.read_text(encoding="utf-8", errors="replace")[:5000]
    except OSError as error:
        result["error"] = str(error)
        return result
    normalized_wrapper_text = wrapper_text.replace("\\", "/").lower()
    result["officialNpmWrapper"] = "node_modules/@openai/codex/bin/codex.js" in normalized_wrapper_text
    result["configForwarder"] = "codex_cli_path" in normalized_wrapper_text and "config.toml" in normalized_wrapper_text
    package_path = candidate_path.parent / "node_modules" / "@openai" / "codex" / "package.json"
    result["packagePath"] = str(package_path)
    if package_path.exists():
        try:
            package_data = json.loads(package_path.read_text(encoding="utf-8", errors="replace"))
            if package_data.get("name") == "@openai/codex":
                result["packageVersion"] = str(package_data.get("version") or "")
                result["officialNpmWrapper"] = bool(result["officialNpmWrapper"])
        except (OSError, json.JSONDecodeError) as error:
            result["error"] = str(error)
    return result


def scan_curated_plugin_registry(
    codex_home_path: Path,
    config_text: str,
    managed_config_text: str = "",
) -> dict[str, Any]:
    node_repl_contract = node_repl_effective_contract(config_text, managed_config_text)
    configured_cli_path = str(node_repl_contract["effectiveEnv"].get("CODEX_CLI_PATH") or "")
    if not configured_cli_path:
        configured_cli_path = extract_config_path(config_text, "CODEX_CLI_PATH")
    cli_path = configured_cli_path if configured_cli_path and Path(configured_cli_path).is_file() else ""
    if not cli_path:
        native_host_scan = scan_chrome_native_host_paths(codex_home_path)
        cli_path = next(
            (
                candidate_path
                for candidate_path in native_host_scan["healthyCodexCliPaths"]
                if Path(candidate_path).is_file()
            ),
            "",
        )
    if not cli_path:
        plugin_appserver_cli_path = codex_home_path / "plugins" / ".plugin-appserver" / (
            "codex.exe" if os.name == "nt" else "codex"
        )
        if plugin_appserver_cli_path.is_file():
            cli_path = str(plugin_appserver_cli_path)
    if not cli_path:
        return {
            "available": False,
            "attempted": False,
            "cliPath": "",
            "installedPluginIds": [],
            "enabledPluginIds": [],
            "error": "Codex CLI is unavailable.",
        }

    cache_key = (windows_path_key(codex_home_path), windows_path_key(cli_path))
    now = time.monotonic()
    with diagnostics_runtime_cache_lock:
        cache_epoch = diagnostics_runtime_cache_epoch
        cached_entry = curated_plugin_registry_cache.get(cache_key)
        if cached_entry and cached_entry[0] == cache_epoch and now - cached_entry[1] < 30:
            return cached_entry[2]

    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home_path)
    command_result = run_hidden_command(
        [cli_path, "plugin", "list", "--marketplace", "openai-curated", "--available", "--json"],
        timeout_seconds=15,
        environment=environment,
    )
    result: dict[str, Any] = {
        "available": False,
        "attempted": True,
        "cliPath": cli_path,
        "installedPluginIds": [],
        "enabledPluginIds": [],
        "error": str(command_result.get("error") or command_result.get("stderr") or "").strip(),
    }
    if int(command_result.get("returnCode") or 0) == 0:
        try:
            registry = json.loads(str(command_result.get("stdout") or ""))
            entries = [*list(registry.get("installed") or []), *list(registry.get("available") or [])]
            result.update(
                {
                    "available": True,
                    "installedPluginIds": sorted(
                        {
                            str(entry.get("pluginId") or "")
                            for entry in entries
                            if entry.get("installed") and entry.get("pluginId")
                        }
                    ),
                    "enabledPluginIds": sorted(
                        {
                            str(entry.get("pluginId") or "")
                            for entry in entries
                            if entry.get("enabled") and entry.get("pluginId")
                        }
                    ),
                    "error": "",
                }
            )
        except (json.JSONDecodeError, TypeError, AttributeError) as error:
            result["error"] = f"Invalid curated plugin registry JSON: {error}"
    elif not result["error"]:
        result["error"] = f"Codex plugin list exited with code {command_result.get('returnCode')}"
    with diagnostics_runtime_cache_lock:
        if cache_epoch == diagnostics_runtime_cache_epoch:
            curated_plugin_registry_cache[cache_key] = (cache_epoch, time.monotonic(), result)
    return result


def file_has_utf8_bom(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(3) == b"\xef\xbb\xbf"
    except OSError:
        return False


def read_windows_user_environment_variable(name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value)


def same_resolved_path(left_text: str | Path | None, right_path: Path) -> bool:
    if not left_text:
        return False
    try:
        left_path = Path(str(left_text)).expanduser().resolve(strict=False)
        right_resolved_path = right_path.expanduser().resolve(strict=False)
    except OSError:
        return False
    if os.name == "nt":
        return str(left_path).lower() == str(right_resolved_path).lower()
    return left_path == right_resolved_path


def parse_toml_config(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": path.exists(),
        "valid": False,
        "hasBom": False,
        "replacementCharacters": 0,
        "topLevelKeys": [],
        "projectCount": 0,
        "pluginCount": 0,
        "error": "",
    }
    if not path.exists():
        return result
    try:
        raw_data = path.read_bytes()
    except OSError as error:
        result["error"] = str(error)
        return result
    result["hasBom"] = raw_data.startswith(b"\xef\xbb\xbf")
    try:
        text = raw_data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        text = raw_data.decode("utf-8", errors="replace")
        result["error"] = str(error)
    result["replacementCharacters"] = text.count("\ufffd")
    if result["error"]:
        return result
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        result["error"] = str(error)
        return result
    result["valid"] = isinstance(data, dict)
    if isinstance(data, dict):
        result["topLevelKeys"] = sorted(str(key) for key in data.keys())[:40]
        projects = data.get("projects")
        plugins = data.get("plugins")
        result["projectCount"] = len(projects) if isinstance(projects, dict) else 0
        result["pluginCount"] = len(plugins) if isinstance(plugins, dict) else 0
    return result


def find_sandbox_setup_error_files(codex_home_path: Path, max_entries: int = 24) -> list[dict[str, str]]:
    candidates = [codex_home_path / "setup_error.json"]
    try:
        for child in codex_home_path.iterdir():
            if len(candidates) >= max_entries:
                break
            if child.is_dir() and child.name not in {"sessions", "plugins", "cache", "memories"}:
                candidates.append(child / "setup_error.json")
    except OSError:
        pass

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized_key = str(candidate.resolve(strict=False)).lower() if os.name == "nt" else str(candidate.resolve(strict=False))
        if normalized_key in seen or not candidate.exists():
            continue
        seen.add(normalized_key)
        summary = ""
        try:
            summary = candidate.read_text(encoding="utf-8", errors="replace")[:500]
        except OSError as error:
            summary = str(error)
        results.append({"path": str(candidate), "summary": re.sub(r"\s+", " ", summary).strip()})
    return results


def inspect_sandbox_runtime_files(codex_home_path: Path) -> dict[str, Any]:
    sandbox_path = codex_home_path / ".sandbox"
    sandbox_secrets_path = codex_home_path / ".sandbox-secrets"
    sandbox_bin_path = codex_home_path / ".sandbox-bin"
    marker_path = sandbox_path / "setup_marker.json"
    users_secret_path = sandbox_secrets_path / "sandbox_users.json"
    command_runner_path = sandbox_bin_path / "codex-command-runner.exe"
    stale_setup_error_files: list[str] = []
    try:
        stale_setup_error_files = [
            str(path)
            for path in sandbox_path.glob("setup_error.json.*")
            if path.is_file()
        ][:8]
    except OSError:
        stale_setup_error_files = []
    return {
        "sandboxPath": str(sandbox_path),
        "sandboxExists": sandbox_path.exists(),
        "sandboxSecretsPath": str(sandbox_secrets_path),
        "sandboxSecretsExists": sandbox_secrets_path.exists(),
        "sandboxBinPath": str(sandbox_bin_path),
        "sandboxBinExists": sandbox_bin_path.exists(),
        "setupMarkerPath": str(marker_path),
        "setupMarkerExists": marker_path.exists(),
        "sandboxUsersSecretPath": str(users_secret_path),
        "sandboxUsersSecretExists": users_secret_path.exists(),
        "commandRunnerPath": str(command_runner_path),
        "commandRunnerExists": command_runner_path.exists(),
        "staleSetupErrorFiles": stale_setup_error_files,
    }


def inspect_pwsh_resolution() -> dict[str, Any]:
    resolved_path = shutil.which("pwsh") or shutil.which("pwsh.exe") or ""
    path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    windows_apps_entries = [entry for entry in path_entries if "windowsapps" in entry.lower()]
    preferred_path = Path(r"D:\Software\PowerShell\7\pwsh.exe")
    resolved_lower = resolved_path.lower()
    return {
        "resolvedPath": resolved_path,
        "resolvedExists": Path(resolved_path).exists() if resolved_path else False,
        "resolvedFromWindowsApps": bool(resolved_path and "windowsapps" in resolved_lower),
        "windowsAppsPathEntries": windows_apps_entries[:6],
        "preferredPath": str(preferred_path),
        "preferredExists": preferred_path.exists(),
    }


def run_codex_diagnostics(
    codex_home_text: str | None = None,
    sidebar_limit: int = 50,
    language: str | None = "zh",
    comprehensive_event_stream: bool = False,
) -> dict[str, Any]:
    language = normalize_language(language)
    paths = resolve_codex_paths(codex_home_text)
    issues: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    def add_issue(
        issue_id: str,
        severity: str,
        category: str,
        title: str,
        summary: str,
        recommendation: str,
        evidence: list[str] | None = None,
        affected_paths: list[str] | None = None,
        fix_command: str | None = None,
    ) -> None:
        issues.append(
            {
                "id": issue_id,
                "severity": severity,
                "category": category,
                "title": title,
                "summary": summary,
                "recommendation": recommendation,
                "evidence": evidence or [],
                "affectedPaths": affected_paths or [],
                "fixCommand": fix_command,
            }
        )

    def add_check(
        check_id: str,
        category: str,
        title: str,
        status: str,
        summary: str,
        evidence: list[str] | None = None,
        affected_paths: list[str] | None = None,
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "category": category,
                "title": title,
                "status": status,
                "summary": summary,
                "evidence": evidence or [],
                "affectedPaths": affected_paths or [],
            }
        )

    codex_home_exists = paths.codex_home_path.exists() and paths.codex_home_path.is_dir()
    add_check(
        "codex_home",
        "core",
        localize(language, "Codex Home path", "Codex Home 路径"),
        "pass" if codex_home_exists else "critical",
        localize(language, "Codex Home directory is reachable.", "Codex Home 目录可访问。")
        if codex_home_exists
        else localize(language, "Codex Home directory is missing.", "Codex Home 目录不存在。"),
        [path_exists_text(paths.codex_home_path)],
        [str(paths.codex_home_path)],
    )
    if not codex_home_exists:
        add_issue(
            "core.codex_home_missing",
            "critical",
            "core",
            localize(language, "Codex Home is missing", "Codex Home 不存在"),
            localize(language, "The configured Codex Home directory cannot be reached.", "当前配置的 Codex Home 目录不可访问。"),
            localize(language, "Correct CODEX_HOME or the path field before running thread or resource operations.", "先修正 CODEX_HOME 或顶部路径输入，再执行线程和资源操作。"),
            [path_exists_text(paths.codex_home_path)],
            [str(paths.codex_home_path)],
        )

    required_files = [
        ("state_database", paths.database_path, "critical", "state_5.sqlite"),
        ("global_state", paths.global_state_path, "warning", ".codex-global-state.json"),
        ("session_index", paths.session_index_path, "warning", "session_index.jsonl"),
        ("config", paths.config_path, "warning", "config.toml"),
        ("version", paths.version_path, "info", "version.json"),
    ]
    for check_id, path, missing_severity, label in required_files:
        exists = path.exists()
        add_check(
            f"file.{check_id}",
            "state",
            label,
            "pass" if exists else missing_severity,
            localize(language, f"{label} exists.", f"{label} 存在。")
            if exists
            else localize(language, f"{label} is missing.", f"{label} 缺失。"),
            [path_exists_text(path)],
            [str(path)],
        )
        if not exists and missing_severity in {"critical", "warning"}:
            add_issue(
                f"state.{check_id}_missing",
                missing_severity,
                "state",
                localize(language, f"{label} is missing", f"{label} 缺失"),
                localize(language, "Codex Desktop may not be able to restore the expected local state.", "Codex Desktop 可能无法恢复预期的本地状态。"),
                localize(language, "Check whether CODEX_HOME points to the active Codex profile and restore this file from backup if needed.", "确认 CODEX_HOME 指向当前活跃配置；必要时从备份恢复该文件。"),
                [path_exists_text(path)],
                [str(path)],
            )

    config_parse = parse_toml_config(paths.config_path)
    config_parse_ok = config_parse["exists"] and config_parse["valid"] and not config_parse["error"]
    config_has_encoding_risk = bool(config_parse["hasBom"] or config_parse["replacementCharacters"])
    add_check(
        "config.toml_parse",
        "config",
        localize(language, "Active config TOML parse", "当前 config.toml 解析"),
        "critical" if config_parse["exists"] and (not config_parse_ok or config_has_encoding_risk) else "pass" if config_parse_ok else "warning",
        localize(language, "config.toml is valid UTF-8 TOML without a UTF-8 BOM.", "config.toml 是无 BOM 的有效 UTF-8 TOML。")
        if config_parse_ok and not config_has_encoding_risk
        else localize(language, "config.toml is missing, invalid, or has encoding markers that can break Codex startup.", "config.toml 缺失、无效，或带有可能影响 Codex 启动的编码标记。"),
        [
            f"exists={config_parse['exists']}",
            f"valid={config_parse['valid']}",
            f"has_bom={config_parse['hasBom']}",
            f"replacement_characters={config_parse['replacementCharacters']}",
            f"project_count={config_parse['projectCount']}",
            f"plugin_count={config_parse['pluginCount']}",
            f"error={config_parse['error'] or '-'}",
        ],
        [str(paths.config_path)],
    )
    if config_parse["exists"] and not config_parse_ok:
        add_issue(
            "config.toml_invalid",
            "critical",
            "config",
            localize(language, "config.toml cannot be parsed", "config.toml 无法解析"),
            localize(language, "Codex Desktop can fail setup or fall back to stale runtime state when config.toml is invalid.", "config.toml 无效时，Codex Desktop 可能启动失败或回落到陈旧运行态。"),
            localize(language, "Do not patch blindly. Compare with the newest known-good backup, remove malformed project/plugin blocks, and verify with a TOML parser before restart.", "不要盲目修补。先和最新可信备份比对，移除损坏的项目或插件配置块，并在重启前用 TOML 解析器验证。"),
            [config_parse["error"]],
            [str(paths.config_path)],
        )
    if bool(config_parse["hasBom"]):
        add_issue(
            "config.toml_utf8_bom",
            "critical",
            "config",
            localize(language, "config.toml has a UTF-8 BOM", "config.toml 带有 UTF-8 BOM"),
            localize(language, "The active config starts with a UTF-8 BOM; this has caused Codex config parsing/setup failures in this environment.", "当前配置文件以 UTF-8 BOM 开头；这个环境里它曾导致 Codex 配置解析或 setup 失败。"),
            localize(language, "Rewrite config.toml as UTF-8 without BOM only after backing it up and validating the parsed TOML content.", "先备份并验证 TOML 内容，再把 config.toml 重写为无 BOM UTF-8。"),
            ["first_bytes=EF BB BF"],
            [str(paths.config_path)],
        )
    if int(config_parse["replacementCharacters"]):
        add_issue(
            "config.toml_encoding_replacement",
            "warning",
            "config",
            localize(language, "config.toml contains replacement characters", "config.toml 含有替换字符"),
            localize(language, "U+FFFD replacement characters usually mean a previous non-UTF-8 write damaged the file.", "U+FFFD 替换字符通常说明文件曾被非 UTF-8 写入损坏。"),
            localize(language, "Restore the damaged values from a trusted backup instead of editing around the replacement characters.", "从可信备份恢复受损字段，不要围绕替换字符就地糊补。"),
            [f"replacement_characters={config_parse['replacementCharacters']}"],
            [str(paths.config_path)],
        )

    sandbox_setup_errors = find_sandbox_setup_error_files(paths.codex_home_path)
    sandbox_runtime_files = inspect_sandbox_runtime_files(paths.codex_home_path)
    sandbox_missing = [
        key
        for key in [
            "sandboxExists",
            "sandboxSecretsExists",
            "sandboxBinExists",
            "setupMarkerExists",
            "sandboxUsersSecretExists",
            "commandRunnerExists",
        ]
        if not sandbox_runtime_files.get(key)
    ]
    sandbox_status = "critical" if sandbox_setup_errors else "warning" if sandbox_missing else "pass"
    sandbox_evidence = [
        f"active_setup_error_files={len(sandbox_setup_errors)}",
        f"missing={','.join(sandbox_missing) or '-'}",
        f"stale_setup_error_files={len(sandbox_runtime_files['staleSetupErrorFiles'])}",
        f"setup_marker={sandbox_runtime_files['setupMarkerPath']} exists={sandbox_runtime_files['setupMarkerExists']}",
        f"sandbox_users={sandbox_runtime_files['sandboxUsersSecretPath']} exists={sandbox_runtime_files['sandboxUsersSecretExists']}",
        f"command_runner={sandbox_runtime_files['commandRunnerPath']} exists={sandbox_runtime_files['commandRunnerExists']}",
    ]
    sandbox_evidence.extend(f"setup_error={item['path']} {item['summary'][:180]}" for item in sandbox_setup_errors[:4])
    add_check(
        "sandbox.setup_state",
        "runtime",
        localize(language, "Codex sandbox setup state", "Codex sandbox setup 状态"),
        sandbox_status,
        localize(language, "Sandbox marker, secret and command runner files are present, and no active setup_error.json remains.", "sandbox marker、secret 和命令执行器存在，且没有残留 active setup_error.json。")
        if sandbox_status == "pass"
        else localize(language, "Sandbox setup state is incomplete or still contains an active setup_error.json.", "sandbox setup 状态不完整，或仍残留 active setup_error.json。"),
        sandbox_evidence,
        [
            sandbox_runtime_files["sandboxPath"],
            sandbox_runtime_files["sandboxSecretsPath"],
            sandbox_runtime_files["sandboxBinPath"],
            *[item["path"] for item in sandbox_setup_errors[:4]],
        ],
    )
    if sandbox_setup_errors:
        add_issue(
            "sandbox.active_setup_error",
            "critical",
            "runtime",
            localize(language, "Active sandbox setup_error.json exists", "存在 active sandbox setup_error.json"),
            localize(language, "Codex Desktop can keep showing setup failures when a live setup_error.json remains under CODEX_HOME.", "CODEX_HOME 下残留 live setup_error.json 时，Codex Desktop 可能持续显示 setup 失败。"),
            localize(language, "Verify the current sandbox with codex doctor/sandbox in a clean environment, then move stale setup_error.json aside instead of deleting evidence permanently.", "先在干净环境中用 codex doctor/sandbox 验证当前 sandbox，再把陈旧 setup_error.json 移走而不是永久删除证据。"),
            [f"{item['path']} | {item['summary'][:220]}" for item in sandbox_setup_errors[:6]],
            [item["path"] for item in sandbox_setup_errors[:6]],
        )
    elif sandbox_missing:
        add_issue(
            "sandbox.runtime_files_missing",
            "warning",
            "runtime",
            localize(language, "Sandbox runtime files are incomplete", "sandbox 运行文件不完整"),
            localize(language, "Missing sandbox marker/secret/runner files can cause admin or non-admin sandbox setup loops after restart.", "sandbox marker、secret 或 runner 缺失会导致重启后反复进入管理员或非管理员 sandbox setup。"),
            localize(language, "Repair through the official Codex sandbox setup path or a clean Codex environment; do not hand-edit secrets in a live session.", "通过官方 Codex sandbox setup 或干净 Codex 环境修复；不要在当前 live 会话里手写 secret。"),
            sandbox_missing,
            [
                sandbox_runtime_files["sandboxPath"],
                sandbox_runtime_files["sandboxSecretsPath"],
                sandbox_runtime_files["sandboxBinPath"],
            ],
        )

    pwsh_resolution = inspect_pwsh_resolution()
    pwsh_resolution_status = "warning" if pwsh_resolution["resolvedFromWindowsApps"] else "pass" if pwsh_resolution["resolvedPath"] else "info"
    pwsh_resolution_evidence = [
        f"resolved_path={pwsh_resolution['resolvedPath'] or '-'}",
        f"resolved_exists={pwsh_resolution['resolvedExists']}",
        f"resolved_from_windowsapps={pwsh_resolution['resolvedFromWindowsApps']}",
        f"preferred_path={pwsh_resolution['preferredPath']} exists={pwsh_resolution['preferredExists']}",
        *[f"path_entry={entry}" for entry in pwsh_resolution["windowsAppsPathEntries"]],
    ]
    add_check(
        "runtime.pwsh_resolution",
        "runtime",
        localize(language, "PowerShell 7 command resolution", "PowerShell 7 命令解析"),
        pwsh_resolution_status,
        localize(language, "pwsh resolves outside WindowsApps, so sandboxed process creation is less likely to hit App Execution Alias failures.", "pwsh 解析到 WindowsApps 之外，sandbox 进程创建不容易命中 App Execution Alias 故障。")
        if pwsh_resolution_status == "pass"
        else localize(language, "pwsh currently resolves through WindowsApps or is missing; Codex sandbox can fail with CreateProcessAsUserW 1312 when it launches that alias.", "pwsh 当前解析到 WindowsApps 或缺失；Codex sandbox 启动该 alias 时可能触发 CreateProcessAsUserW 1312。"),
        pwsh_resolution_evidence,
        [pwsh_resolution["resolvedPath"], pwsh_resolution["preferredPath"]],
    )
    if pwsh_resolution["resolvedFromWindowsApps"]:
        add_issue(
            "runtime.pwsh_windowsapps_alias_risk",
            "warning",
            "runtime",
            localize(language, "pwsh resolves to WindowsApps alias", "pwsh 解析到 WindowsApps alias"),
            localize(language, "Recent CreateProcessAsUserW 1312 errors can occur when the restricted sandbox launches the Store/App Execution Alias version of PowerShell 7.", "受限 sandbox 启动 Store/App Execution Alias 版 PowerShell 7 时，可能出现最近日志里的 CreateProcessAsUserW 1312。"),
            localize(language, "Install or copy a normal-directory PowerShell 7 build, put it before WindowsApps in user PATH, restart Codex Desktop, and prefer the absolute pwsh.exe path for sandbox smoke tests.", "安装或复制普通目录版 PowerShell 7，把它放到用户 PATH 中 WindowsApps 之前，重启 Codex Desktop；做 sandbox smoke 时优先使用绝对 pwsh.exe 路径。"),
            pwsh_resolution_evidence,
            [pwsh_resolution["resolvedPath"], pwsh_resolution["preferredPath"]],
        )

    sqlite_state = sqlite_quick_check(paths.database_path)
    thread_count = sqlite_table_count(paths.database_path, "threads")
    sqlite_ok = sqlite_state["exists"] and sqlite_state["quickCheck"] == "ok" and "threads" in sqlite_state["tables"]
    add_check(
        "sqlite.state",
        "state",
        localize(language, "Thread SQLite integrity", "线程 SQLite 完整性"),
        "pass" if sqlite_ok else "critical",
        localize(language, "state_5.sqlite passed quick_check and contains the threads table.", "state_5.sqlite 通过 quick_check，且包含 threads 表。")
        if sqlite_ok
        else localize(language, "state_5.sqlite is unreadable, corrupt, or missing the threads table.", "state_5.sqlite 不可读、损坏或缺少 threads 表。"),
        [
            f"quick_check={sqlite_state['quickCheck'] or '-'}",
            f"tables={', '.join(sqlite_state['tables'][:12]) or '-'}",
            f"thread_count={thread_count if thread_count is not None else '-'}",
            sqlite_state["error"],
        ],
        [str(paths.database_path)],
    )
    if not sqlite_ok:
        add_issue(
            "sqlite.state_unhealthy",
            "critical",
            "state",
            localize(language, "Thread database is not healthy", "线程数据库异常"),
            localize(language, "The main thread metadata database failed a structural check.", "主线程元数据数据库没有通过结构检查。"),
            localize(language, "Stop Codex Desktop, preserve a copy of state_5.sqlite, then restore from a known-good backup or inspect the SQLite file manually.", "先关闭 Codex Desktop 并复制保留 state_5.sqlite，再从可信备份恢复或手动检查 SQLite。"),
            [sqlite_state["error"], f"quick_check={sqlite_state['quickCheck'] or '-'}"],
            [str(paths.database_path)],
        )

    log_database_path = paths.codex_home_path / "logs_2.sqlite"
    logs_state = sqlite_quick_check(log_database_path, run_quick_check=False)
    logs_ok = logs_state["exists"] and "logs" in logs_state["tables"]
    log_levels = count_recent_log_levels(log_database_path)
    add_check(
        "sqlite.logs",
        "logs",
        localize(language, "Codex logs database", "Codex 日志库"),
        "pass" if logs_ok else "warning",
        localize(language, "logs_2.sqlite is readable and contains the logs table.", "logs_2.sqlite 可读，且包含 logs 表。")
        if logs_ok
        else localize(language, "logs_2.sqlite is missing or does not expose the expected logs table.", "logs_2.sqlite 缺失或没有预期的 logs 表。"),
        [
            f"quick_check={logs_state['quickCheck'] or '-'}",
            f"tables={', '.join(logs_state['tables'][:12]) or '-'}",
            f"latest_sample_levels={json.dumps(log_levels, ensure_ascii=False)}",
            logs_state["error"],
        ],
        [str(log_database_path)],
    )
    recent_errors = int(log_levels.get("ERROR", 0))
    recent_warnings = int(log_levels.get("WARN", 0) + log_levels.get("WARNING", 0))
    recent_log_samples = recent_log_problem_samples(log_database_path) if logs_ok else []
    if logs_ok and recent_errors:
        add_issue(
            "logs.recent_errors",
            "warning",
            "logs",
            localize(language, "Recent Codex errors exist", "近期 Codex 日志存在错误"),
            localize(language, f"The latest sampled logs contain {recent_errors} ERROR rows.", f"最新日志样本里有 {recent_errors} 条 ERROR 记录。"),
            localize(language, "Use the thread log viewer to filter failures and inspect the matching request ids before changing state.", "先在线程日志查看器中过滤 failure/error，并结合 request id 定位原因。"),
            [f"ERROR={recent_errors}", f"WARN={recent_warnings}", *recent_log_samples],
            [str(log_database_path)],
        )
    context_window_samples = recent_context_window_log_matches(log_database_path, sample_limit=6) if logs_ok else []
    rollout_context_window_scan = scan_recent_context_window_exhaustion(paths.codex_home_path, snapshot if "snapshot" in locals() else None)
    context_window_evidence = [
        *(f"log: {sample}" for sample in context_window_samples),
        *(f"rollout: {sample}" for sample in rollout_context_window_scan.get("findings", [])),
    ]
    context_window_paths = [
        str(log_database_path),
        *[str(path) for path in rollout_context_window_scan.get("affectedPaths", [])],
    ]
    add_check(
        "logs.context_window_exhaustion",
        "logs",
        localize(language, "Thread context-window exhaustion", "线程上下文窗口溢出"),
        "critical" if context_window_evidence else "pass",
        localize(language, "No recent context-window exhaustion errors were found.", "近期没有发现上下文窗口溢出错误。")
        if not context_window_evidence
        else localize(language, "Recent logs or rollout tails show at least one thread exhausted the model context window.", "近期日志或 rollout 尾部显示至少有一个线程耗尽了模型上下文窗口。"),
        context_window_evidence or ["context-window exhaustion not found in recent sampled logs or rollout tails"],
        context_window_paths,
    )
    if context_window_evidence:
        add_issue(
            "logs.context_window_exhausted",
            "critical",
            "logs",
            localize(language, "A thread exhausted the model context window", "有线程耗尽模型上下文窗口"),
            localize(
                language,
                "The target thread can still receive a delegated message, but the next model turn fails before useful work starts because the retained history is too large.",
                "目标线程仍可能收到转发消息，但下一轮模型在真正开始工作前就会因为保留历史过大而失败。",
            ),
            localize(
                language,
                "Do not keep sending long messages to that thread. Open the thread detail, inspect storage and token history, then slim safe embedded/invalid payloads or fork a fresh continuation thread with a concise handoff.",
                "不要继续向该线程发送长消息。先打开线程详情，检查存储和 token 历史；随后对安全的嵌入/无效载荷做瘦身，或分叉/新建一个只带精简交接说明的续跑线程。",
            ),
            context_window_evidence,
            context_window_paths,
        )
    managed_config_path_for_notify = paths.codex_home_path / "managed_config.toml"
    config_text_for_notify = read_text_safely(paths.config_path)
    managed_config_text_for_notify = read_text_safely(managed_config_path_for_notify)
    active_notify_values = [
        *extract_top_level_toml_array_values(config_text_for_notify, "notify"),
        *extract_top_level_toml_array_values(managed_config_text_for_notify, "notify"),
    ]
    desktop_managed_notify = is_desktop_managed_computer_use_notify(
        active_notify_values,
        config_text_for_notify,
        managed_config_text_for_notify,
    )
    unsafe_notify_active = bool(active_notify_values) and not desktop_managed_notify
    add_check(
        "runtime.legacy_notify_hook",
        "runtime",
        localize(language, "Notify hook runtime contract", "Notify 钩子运行时契约"),
        "warning" if unsafe_notify_active else "pass",
        localize(language, "A legacy notify hook is still configured and can reintroduce turn-end failures.", "仍配置了 legacy notify 钩子，可能再次触发 turn 结束故障。")
        if unsafe_notify_active
        else localize(language, "The Desktop-managed Computer Use turn-end helper has the expected static contract.", "Desktop 管理的 Computer Use turn-end helper 符合预期静态契约。")
        if desktop_managed_notify
        else localize(language, "No notify hook is configured.", "当前没有配置 notify 钩子。"),
        [
            f"notify_entries={len(active_notify_values)}",
            f"desktop_managed_notify={desktop_managed_notify}",
            f"notify_command={active_notify_values[0] if active_notify_values else '-'}",
            f"notify_event={active_notify_values[1] if len(active_notify_values) > 1 else '-'}",
        ],
        [str(paths.config_path), str(managed_config_path_for_notify), *(active_notify_values[:1])],
    )
    if unsafe_notify_active:
        add_issue(
            "runtime.legacy_notify_hook_active",
            "warning",
            "runtime",
            localize(language, "Unsupported legacy notify hook is active", "不受支持的 legacy notify 钩子仍在启用"),
            localize(
                language,
                "The configured top-level notify command is not the Desktop-managed Computer Use turn-end helper contract.",
                "当前 top-level notify 命令不符合 Desktop 管理的 Computer Use turn-end helper 契约。",
            ),
            localize(
                language,
                "Remove only the unsupported legacy notify configuration. Preserve a Desktop-managed helper that matches the current runtime path and turn-ended contract.",
                "只移除不受支持的 legacy notify 配置；对于路径属于当前运行时且参数为 turn-ended 的 Desktop-managed helper，应予以保留。",
            ),
            [f"active_notify_entries={len(active_notify_values)}", *active_notify_values[:2]],
            [str(paths.config_path), str(managed_config_path_for_notify)],
        )

    global_state_parse = parse_json_file(paths.global_state_path)
    global_state_ok = global_state_parse["exists"] and global_state_parse["valid"] and not global_state_parse["error"]
    add_check(
        "json.global_state",
        "state",
        localize(language, "Global state JSON", "全局状态 JSON"),
        "pass" if global_state_ok else "critical",
        localize(language, ".codex-global-state.json is valid JSON.", ".codex-global-state.json 是有效 JSON。")
        if global_state_ok
        else localize(language, ".codex-global-state.json is missing or invalid.", ".codex-global-state.json 缺失或 JSON 无效。"),
        [
            f"size_bytes={global_state_parse['sizeBytes']}",
            f"keys={', '.join(global_state_parse['keys'][:12]) or '-'}",
            f"replacement_characters={global_state_parse['replacementCharacters']}",
            global_state_parse["error"],
        ],
        [str(paths.global_state_path)],
    )
    if not global_state_ok:
        add_issue(
            "json.global_state_invalid",
            "critical",
            "state",
            localize(language, "Global state cannot be parsed", "全局状态无法解析"),
            localize(language, "Sidebar pins, project roots and visibility hints depend on this JSON file.", "侧边栏置顶、项目根目录和可见性线索都依赖这个 JSON 文件。"),
            localize(language, "Close Codex Desktop and restore this file from a backup before running visibility repairs.", "关闭 Codex Desktop，并先从备份恢复该文件，再做可见性修复。"),
            [global_state_parse["error"]],
            [str(paths.global_state_path)],
        )
    elif int(global_state_parse["replacementCharacters"]):
        add_issue(
            "json.global_state_encoding_replacement",
            "warning",
            "state",
            localize(language, "Global state contains replacement characters", "全局状态包含替换字符"),
            localize(language, "The file contains U+FFFD replacement characters, usually from an encoding-damaged write.", "文件中出现 U+FFFD，通常说明曾经发生编码损坏写入。"),
            localize(language, "Avoid editing this file with non-UTF-8 tools; compare with backups before further writes.", "避免用非 UTF-8 工具编辑该文件；继续写入前先和备份比对。"),
            [f"replacement_characters={global_state_parse['replacementCharacters']}"],
            [str(paths.global_state_path)],
        )

    session_index_parse = parse_session_index(paths.session_index_path)
    session_index_status = "pass"
    if not session_index_parse["exists"]:
        session_index_status = "warning"
    elif session_index_parse["invalidLines"] or session_index_parse["replacementCharacters"]:
        session_index_status = "warning"
    add_check(
        "jsonl.session_index",
        "state",
        localize(language, "Session index JSONL", "会话索引 JSONL"),
        session_index_status,
        localize(language, "session_index.jsonl can be scanned.", "session_index.jsonl 可扫描。")
        if session_index_status == "pass"
        else localize(language, "session_index.jsonl has missing, invalid, or encoding-damaged rows.", "session_index.jsonl 缺失，或存在无效/编码受损行。"),
        [
            f"records={session_index_parse['records']}",
            f"invalid_lines={session_index_parse['invalidLines']}",
            f"duplicates={session_index_parse['duplicateThreadIds']}",
            f"replacement_characters={session_index_parse['replacementCharacters']}",
        ],
        [str(paths.session_index_path)],
    )
    if session_index_parse["invalidLines"] or session_index_parse["replacementCharacters"]:
        add_issue(
            "jsonl.session_index_damaged",
            "warning",
            "state",
            localize(language, "Session index has damaged rows", "会话索引存在异常行"),
            localize(language, "Invalid or encoding-damaged session_index rows can confuse sidebar ordering.", "无效或编码损坏的 session_index 行会干扰侧边栏顺序判断。"),
            localize(language, "Back up the file, then rebuild only the damaged rows from SQLite and rollout JSONL evidence.", "先备份该文件，再只基于 SQLite 与 rollout JSONL 证据重建异常行。"),
            [
                f"invalid_lines={session_index_parse['invalidLines']}",
                f"replacement_characters={session_index_parse['replacementCharacters']}",
            ],
            [str(paths.session_index_path)],
        )
    if session_index_parse["duplicateThreadIds"]:
        add_issue(
            "jsonl.session_index_duplicates",
            "info",
            "state",
            localize(language, "Session index contains duplicate thread ids", "会话索引存在重复线程 ID"),
            localize(language, "Duplicate IDs are common after repairs, but large duplication can hide the real latest sidebar title.", "修复后出现重复 ID 并不少见，但重复过多会掩盖真实最新侧边栏标题。"),
            localize(language, "Keep the newest valid evidence and avoid bulk rewrites while Codex Desktop is running.", "保留最新可信证据；Codex Desktop 运行时不要批量重写。"),
            [", ".join(session_index_parse["sampleDuplicates"])],
            [str(paths.session_index_path)],
        )

    snapshot: dict[str, Any] | None = None
    snapshot_error = ""
    total_storage_bytes = 0
    doctor_slow_thread_count = 0
    try:
        snapshot = build_snapshot(codex_home_text=str(paths.codex_home_path), sidebar_limit=sidebar_limit, validate_rollout_display=False)
    except Exception as error:
        snapshot_error = str(error)
    add_check(
        "threads.snapshot",
        "threads",
        localize(language, "Thread snapshot classification", "线程快照分类"),
        "pass" if snapshot is not None else "critical",
        localize(language, "Thread snapshot can be built from SQLite, global state and JSONL stats.", "可以基于 SQLite、global state 和 JSONL 状态构建线程快照。")
        if snapshot is not None
        else localize(language, "Thread snapshot failed to build.", "线程快照构建失败。"),
        [snapshot_error] if snapshot_error else [],
        [str(paths.database_path), str(paths.global_state_path), str(paths.session_index_path)],
    )
    if snapshot is None:
        add_issue(
            "threads.snapshot_failed",
            "critical",
            "threads",
            localize(language, "Thread snapshot failed", "线程快照构建失败"),
            localize(language, "The manager cannot reliably classify visibility without a valid snapshot.", "无法构建快照时，管理器不能可靠判断可见性。"),
            localize(language, "Fix the state/database errors above first, then rerun diagnostics.", "先修复上面的 state/database 问题，再重新体检。"),
            [snapshot_error],
            [str(paths.codex_home_path)],
        )
    else:
        summary = snapshot.get("summary", {})
        threads = snapshot.get("threads", [])
        projects = snapshot.get("projects", [])
        missing_threads = [thread for thread in threads if thread.get("visibility") == "missing_file"]
        candidate_repair_threads = [thread for thread in threads if thread.get("visibility") == "needs_user_event_repair"]
        empty_shell_threads: list[dict[str, Any]] = []
        for thread in candidate_repair_threads:
            if "missing_first_user_message" not in (thread.get("hiddenReasons") or []):
                continue
            rollout_display = rollout_display_integrity(str(thread.get("rolloutPath") or ""))
            if rollout_display.get("status") != "empty":
                continue
            empty_thread = dict(thread)
            empty_thread["rolloutDisplayStatus"] = rollout_display.get("status")
            empty_shell_threads.append(empty_thread)
        empty_shell_thread_ids = {str(thread.get("id") or "") for thread in empty_shell_threads}
        repair_threads = [
            thread
            for thread in candidate_repair_threads
            if str(thread.get("id") or "") not in empty_shell_thread_ids
        ]
        legacy_outside_rank_threads = [
            thread
            for thread in threads
            if "outside_initial_sidebar_limit" in (thread.get("hiddenReasons") or [])
            or "outside_conversation_initial_page" in (thread.get("hiddenReasons") or [])
        ]
        large_threads = [thread for thread in sorted(threads, key=lambda item: int(item.get("fileSizeBytes") or 0), reverse=True) if int(thread.get("fileSizeBytes") or 0) >= 100 * 1024 * 1024]
        archived_threads = [thread for thread in threads if thread.get("archived")]
        session_index_ids = {str(record.get("threadId") or "") for record in read_session_index_records(paths)}
        explicit_sidebar_ids = explicit_sidebar_thread_ids_from_state(read_global_state(paths))
        archived_session_index_threads = [
            thread
            for thread in archived_threads
            if str(thread.get("id") or "") in session_index_ids
        ]
        archived_sidebar_reference_threads = [
            thread
            for thread in archived_threads
            if str(thread.get("id") or "") in explicit_sidebar_ids
        ]
        explicit_sidebar_without_session_index_threads = [
            thread
            for thread in threads
            if not thread.get("archived")
            and thread.get("explicitSidebarReference")
            and not thread.get("presentInSessionIndex")
            and "missing_session_index_entry" in (thread.get("hiddenReasons") or [])
        ]
        eligible_event_stream_threads = [
            thread
            for thread in threads
            if not thread.get("archived") and thread.get("threadKind") == "main" and thread.get("fileExists")
        ]
        eligible_event_stream_threads.sort(
            key=lambda thread: (
                0 if thread.get("codexVisible") else 1,
                int(thread.get("recentRank") or 10**9),
                -int(thread.get("fileSizeBytes") or 0),
            )
        )
        event_stream_scan_limit = len(eligible_event_stream_threads) if comprehensive_event_stream else 48
        event_stream_scan_max_bytes = None if comprehensive_event_stream else 3_000_000
        event_stream_scan_max_lines = None if comprehensive_event_stream else 12_000
        skipped_event_stream_threads = max(0, len(eligible_event_stream_threads) - event_stream_scan_limit)
        main_event_stream_scans: list[dict[str, Any]] = []
        for thread in eligible_event_stream_threads[:event_stream_scan_limit]:
            rollout_path_text = str(thread.get("rolloutPath") or "")
            scan = scan_rollout_event_stream_integrity(
                rollout_path_text,
                max_bytes=event_stream_scan_max_bytes,
                max_lines=event_stream_scan_max_lines,
            )
            scan.update(
                {
                    "threadId": str(thread.get("id") or ""),
                    "title": str(thread.get("title") or ""),
                    "sqliteTitle": str(thread.get("sqliteTitle") or ""),
                    "sessionIndexTitle": str(thread.get("sessionIndexTitle") or ""),
                    "rolloutPath": rollout_path_text,
                    "jsonlParseErrors": int(scan.get("parseErrors") or 0),
                    "jsonlCompactedCount": int(scan.get("compactedCount") or 0),
                    "jsonlEmbeddedImageRefs": int(scan.get("embeddedImageRefs") or 0),
                    "jsonlEmbeddedImageUrlFields": int(scan.get("embeddedImageUrlFields") or 0),
                    "jsonlInvalidImageUrlRefs": int(scan.get("invalidImageUrlRefs") or 0),
                    "jsonlEncryptedContentFields": int(scan.get("encryptedContentFields") or 0),
                    "scanTruncated": bool(scan.get("truncated")),
                }
            )
            main_event_stream_scans.append(scan)
        missing_main_event_stream_threads = [
            scan for scan in main_event_stream_scans if main_event_stream_missing(scan)
        ]
        sparse_main_event_stream_threads = [
            scan
            for scan in main_event_stream_scans
            if not main_event_stream_missing(scan) and main_event_stream_sparse(scan)
        ]
        missing_user_event_stream_threads = [
            scan
            for scan in main_event_stream_scans
            if int(scan.get("responseUserMessages") or 0) > 0
            and int(scan.get("eventUserMessages") or 0) == 0
        ]
        missing_agent_event_stream_threads = [
            scan
            for scan in main_event_stream_scans
            if int(scan.get("responseAssistantMessages") or 0) > 0
            and int(scan.get("eventAgentMessages") or 0) == 0
        ]
        main_event_stream_attention_threads: list[dict[str, Any]] = []
        seen_event_stream_attention_ids: set[str] = set()
        for scan in (
            missing_main_event_stream_threads
            + sparse_main_event_stream_threads
            + missing_user_event_stream_threads
            + missing_agent_event_stream_threads
        ):
            thread_id = str(scan.get("threadId") or "")
            if thread_id in seen_event_stream_attention_ids:
                continue
            seen_event_stream_attention_ids.add(thread_id)
            main_event_stream_attention_threads.append(scan)
        title_event_missing_threads: list[dict[str, Any]] = []
        title_event_mismatch_threads: list[dict[str, Any]] = []
        for scan in main_event_stream_scans:
            session_index_title = str(scan.get("sessionIndexTitle") or "")
            sqlite_title = str(scan.get("sqliteTitle") or "")
            display_title = str(scan.get("title") or session_index_title or sqlite_title)
            latest_event_title = str(scan.get("latestThreadNameUpdated") or "")
            first_user_message = str(scan.get("firstEventUserMessage") or scan.get("firstResponseUserMessage") or "")
            if (
                int(scan.get("eventThreadNameUpdated") or 0) == 0
                and display_title
                and first_user_message
                and not title_matches_message(display_title, first_user_message)
            ):
                title_event_missing_threads.append(scan)
            if latest_event_title:
                comparable_titles = [title for title in (session_index_title, sqlite_title) if title]
                if any(normalized_compare_text(title) != normalized_compare_text(latest_event_title) for title in comparable_titles):
                    title_event_mismatch_threads.append(scan)
            elif session_index_title and sqlite_title and normalized_compare_text(session_index_title) != normalized_compare_text(sqlite_title):
                title_event_mismatch_threads.append(scan)
        jsonl_parse_error_threads = [
            scan for scan in main_event_stream_scans if int(scan.get("jsonlParseErrors") or 0) > 0
        ]
        invalid_image_url_threads = [
            scan for scan in main_event_stream_scans if int(scan.get("jsonlInvalidImageUrlRefs") or 0) > 0
        ]
        embedded_image_threads = [
            scan for scan in main_event_stream_scans if int(scan.get("jsonlEmbeddedImageUrlFields") or 0) > 0
        ]
        repeated_compacted_threads = [
            scan for scan in main_event_stream_scans if int(scan.get("jsonlCompactedCount") or 0) > 1
        ]
        truncated_event_stream_scans = [
            scan for scan in main_event_stream_scans if scan.get("scanTruncated")
        ]
        add_check(
            "threads.visibility",
            "threads",
            localize(language, "Thread visibility model", "线程可见性模型"),
            "pass" if not repair_threads and not missing_threads else "warning",
            localize(
                language,
                f"{summary.get('codexVisibleThreads', 0)} visible main threads; {summary.get('needsRepairThreads', 0)} need repair.",
                f"{summary.get('codexVisibleThreads', 0)} 条主线程可见，{summary.get('needsRepairThreads', 0)} 条需修复。",
            ),
            [
                f"total={summary.get('totalThreads', 0)}",
                f"main={summary.get('mainThreads', 0)}",
                f"subagent={summary.get('subagentThreads', 0)}",
                f"needs_repair={summary.get('needsRepairThreads', 0)}",
                f"legacy_outside_rank={len(legacy_outside_rank_threads)}",
            ],
        )
        add_check(
            "threads.archived_sidebar_references",
            "threads",
            localize(language, "Archived thread sidebar references", "归档线程侧边栏残留引用"),
            "warning" if archived_sidebar_reference_threads else "pass",
            localize(
                language,
                "Archived threads may remain in the session index for the archive view; none have explicit sidebar references.",
                "归档线程可保留在 session_index 中供归档视图使用；未发现显式侧边栏引用。",
            )
            if not archived_sidebar_reference_threads
            else localize(
                language,
                f"{len(archived_sidebar_reference_threads)} archived threads still have explicit sidebar references.",
                f"{len(archived_sidebar_reference_threads)} 条归档线程仍存在显式侧边栏引用。",
            ),
            [
                f"archived={len(archived_threads)}",
                f"session_index_archived={len(archived_session_index_threads)}",
                f"sidebar_refs={len(archived_sidebar_reference_threads)}",
                *[
                    f"{thread.get('title')} | {thread.get('id')} | session_index={str(thread.get('id') or '') in session_index_ids} | sidebar_ref={str(thread.get('id') or '') in explicit_sidebar_ids}"
                    for thread in archived_sidebar_reference_threads[:8]
                ],
            ],
            [str(paths.session_index_path), str(paths.global_state_path)],
        )
        if archived_sidebar_reference_threads:
            add_issue(
                "threads.archived_sidebar_references",
                "warning",
                "threads",
                localize(language, "Archived threads still have sidebar references", "归档线程仍残留侧边栏引用"),
                localize(
                    language,
                    f"{len(archived_sidebar_reference_threads)} archived threads can still be referenced by Codex Desktop sidebar state.",
                    f"{len(archived_sidebar_reference_threads)} 条已归档线程仍可能被 Codex Desktop 侧边栏状态引用。",
                ),
                localize(
                    language,
                    "Remove only the stale global sidebar references. Keep the session index entry and rollout JSONL so the thread remains available in the archive view.",
                    "仅移除过期的全局侧边栏引用；保留 session_index 条目和 rollout JSONL，以便线程仍可在归档视图中查看。",
                ),
                [f"{thread.get('title')} | {thread.get('id')}" for thread in archived_sidebar_reference_threads[:8]],
                [str(paths.session_index_path), str(paths.global_state_path)],
            )
        add_check(
            "threads.explicit_sidebar_without_session_index",
            "threads",
            localize(language, "Explicit sidebar references without session index entries", "缺少 session_index 的显式侧边栏引用"),
            "warning" if explicit_sidebar_without_session_index_threads else "pass",
            localize(
                language,
                "No active threads are being kept visible only by explicit sidebar state after leaving the session index.",
                "未发现 active 线程在离开 session_index 后仍被显式侧边栏状态保留。",
            )
            if not explicit_sidebar_without_session_index_threads
            else localize(
                language,
                f"{len(explicit_sidebar_without_session_index_threads)} active threads have explicit sidebar references but no session index entry.",
                f"{len(explicit_sidebar_without_session_index_threads)} 条 active 线程有显式侧边栏引用，但缺少 session_index 记录。",
            ),
            [
                f"stale_refs={len(explicit_sidebar_without_session_index_threads)}",
                *[
                    f"{thread.get('title')} | {thread.get('id')} | visibility={thread.get('visibility')} | reasons={','.join(thread.get('hiddenReasons') or [])}"
                    for thread in explicit_sidebar_without_session_index_threads[:8]
                ],
            ],
            [str(paths.session_index_path), str(paths.global_state_path)],
        )
        if explicit_sidebar_without_session_index_threads:
            add_issue(
                "threads.explicit_sidebar_without_session_index",
                "warning",
                "threads",
                localize(language, "Sidebar references can keep hidden/deleted threads visible", "侧边栏引用可能让隐藏/删除线程继续可见"),
                localize(
                    language,
                    f"{len(explicit_sidebar_without_session_index_threads)} active threads are absent from session_index but still referenced by explicit sidebar state.",
                    f"{len(explicit_sidebar_without_session_index_threads)} 条 active 线程不在 session_index 中，但仍被显式侧边栏状态引用。",
                ),
                localize(
                    language,
                    "If these threads were meant to be hidden or archived, run Safe delete/archive again with the updated manager and verify the post-write result.",
                    "如果这些线程本应隐藏或归档，请用更新后的管理器重新执行安全删除/归档，并核验写后结果。",
                ),
                [f"{thread.get('title')} | {thread.get('id')}" for thread in explicit_sidebar_without_session_index_threads[:8]],
                [str(paths.session_index_path), str(paths.global_state_path)],
            )
        main_event_stream_status = "pass"
        if missing_main_event_stream_threads:
            main_event_stream_status = "critical"
        elif sparse_main_event_stream_threads or missing_user_event_stream_threads or missing_agent_event_stream_threads:
            main_event_stream_status = "warning"
        elif skipped_event_stream_threads or truncated_event_stream_scans:
            main_event_stream_status = "warning"
        add_check(
            "threads.main_event_stream",
            "threads",
            localize(language, "Main thread event_msg conversation stream", "主线程 event_msg 可见对话流"),
            main_event_stream_status,
            localize(
                language,
                "Main thread response_item messages are backed by event_msg user/agent messages.",
                "主线程 response_item 中的真实对话有对应 event_msg user/agent 可见消息。",
            )
            if main_event_stream_status == "pass"
            else localize(
                language,
                f"{len(missing_main_event_stream_threads)} main threads have missing event streams, {len(sparse_main_event_stream_threads)} are sparse, and {len(main_event_stream_attention_threads)} have user/agent role gaps.",
                f"{len(missing_main_event_stream_threads)} 条主线程 event_msg 可见对话流缺失，{len(sparse_main_event_stream_threads)} 条主线程可见对话流明显稀疏，{len(main_event_stream_attention_threads)} 条主线程存在 user/agent 角色缺口。",
            ),
            [
                f"scanned_main_threads={len(main_event_stream_scans)}",
                f"eligible_main_threads={len(eligible_event_stream_threads)}",
                f"skipped_by_limit={skipped_event_stream_threads}",
                f"scan_limit={event_stream_scan_limit}",
                f"per_file_max_bytes={event_stream_scan_max_bytes}",
                f"per_file_max_lines={event_stream_scan_max_lines}",
                f"truncated_scans={len(truncated_event_stream_scans)}",
                f"missing_streams={len(missing_main_event_stream_threads)}",
                f"sparse_streams={len(sparse_main_event_stream_threads)}",
                f"missing_user_events={len(missing_user_event_stream_threads)}",
                f"missing_agent_events={len(missing_agent_event_stream_threads)}",
                *[event_stream_evidence(scan) for scan in main_event_stream_attention_threads[:8]],
            ],
        )
        if missing_main_event_stream_threads:
            add_issue(
                "threads.main_event_stream_missing",
                "critical",
                "threads",
                localize(language, "Main threads lost visible event_msg conversation records", "主线程丢失 event_msg 可见对话记录"),
                localize(
                    language,
                    f"{len(missing_main_event_stream_threads)} main threads still contain response_item messages but have no usable event_msg user/agent stream.",
                    f"{len(missing_main_event_stream_threads)} 条主线程仍有 response_item 真实对话，但 event_msg user/agent 可见对话流不可用。",
                ),
                localize(
                    language,
                    "Do not slim these main threads again. Rebuild the visible event_msg stream from response_item text after backing up the rollout JSONL.",
                    "不要继续瘦身这些主线程。先备份 rollout JSONL，再从 response_item 文本重建 event_msg 可见对话流。",
                ),
                [event_stream_evidence(scan) for scan in missing_main_event_stream_threads[:8]],
                [str(scan.get("rolloutPath") or "") for scan in missing_main_event_stream_threads[:8]],
            )
        if sparse_main_event_stream_threads:
            add_issue(
                "threads.main_event_stream_sparse",
                "warning",
                "threads",
                localize(language, "Main thread event_msg stream is sparse", "主线程 event_msg 可见对话流明显稀疏"),
                localize(
                    language,
                    f"{len(sparse_main_event_stream_threads)} main threads have far fewer event_msg chat records than response_item messages.",
                    f"{len(sparse_main_event_stream_threads)} 条主线程 event_msg 对话记录明显少于 response_item 真实对话。",
                ),
                localize(
                    language,
                    "Compare the rollout tail before repair; rebuild only missing visible text and keep tool output, images and encrypted_content out of synthetic event_msg records.",
                    "修复前先对比 rollout 尾部；只补可见 user/assistant 文本，不把 tool 输出、图片和 encrypted_content 写入合成 event_msg。",
                ),
                [event_stream_evidence(scan) for scan in sparse_main_event_stream_threads[:8]],
                [str(scan.get("rolloutPath") or "") for scan in sparse_main_event_stream_threads[:8]],
            )
        if missing_user_event_stream_threads or missing_agent_event_stream_threads:
            add_issue(
                "threads.main_event_stream_role_gap",
                "warning" if not missing_main_event_stream_threads else "critical",
                "threads",
                localize(language, "Main thread event_msg user/agent roles are incomplete", "主线程 event_msg 用户/助手角色记录不完整"),
                localize(
                    language,
                    f"{len(missing_user_event_stream_threads)} threads are missing user_message events and {len(missing_agent_event_stream_threads)} are missing agent_message events.",
                    f"{len(missing_user_event_stream_threads)} 条线程缺少 user_message event，{len(missing_agent_event_stream_threads)} 条线程缺少 agent_message event。",
                ),
                localize(
                    language,
                    "Treat this as a visibility corruption risk before any further slimming or closed-child cleanup.",
                    "继续瘦身或清理 closed 子线程前，先把它当作可见对话损坏风险处理。",
                ),
                [
                    event_stream_evidence(scan)
                    for scan in (missing_user_event_stream_threads + missing_agent_event_stream_threads)[:8]
                ],
                [
                    str(scan.get("rolloutPath") or "")
                    for scan in (missing_user_event_stream_threads + missing_agent_event_stream_threads)[:8]
                ],
            )
        # Current Codex releases reject legacy thread_name_updated events during replay.
        # Persisted SQLite/session_index title agreement is the supported integrity signal.
        main_title_stream_status = "warning" if title_event_mismatch_threads else "pass"
        add_check(
            "threads.main_title_stream",
            "threads",
            localize(language, "Main thread title event/index consistency", "主线程标题事件与索引一致性"),
            main_title_stream_status,
            localize(language, "Main thread title events and persisted titles are consistent.", "主线程标题事件与持久化标题一致。")
            if main_title_stream_status == "pass"
            else localize(
                language,
                f"{len(title_event_mismatch_threads)} main threads have persisted title mismatches.",
                f"{len(title_event_mismatch_threads)} 条主线程的持久化标题存储不一致。",
            ),
            [
                f"missing_title_events={len(title_event_missing_threads)}",
                f"title_mismatches={len(title_event_mismatch_threads)}",
                *[
                    f"{scan.get('title')} | {scan.get('threadId')} | sqlite={scan.get('sqliteTitle')} | session_index={scan.get('sessionIndexTitle')} | latest_event={scan.get('latestThreadNameUpdated')}"
                    for scan in (title_event_missing_threads + title_event_mismatch_threads)[:8]
                ],
            ],
            [str(paths.session_index_path), str(paths.database_path)],
        )
        if title_event_mismatch_threads:
            add_issue(
                "threads.main_title_index_mismatch",
                "warning",
                "threads",
                localize(language, "Main thread titles disagree across rollout, SQLite or session_index", "主线程标题在 rollout、SQLite 或 session_index 之间不一致"),
                localize(
                    language,
                    f"{len(title_event_mismatch_threads)} main threads have inconsistent title sources.",
                    f"{len(title_event_mismatch_threads)} 条主线程存在标题来源不一致。",
                ),
                localize(
                    language,
                    "Repair the title sources together; changing only one layer can make Codex Desktop show the wrong thread name.",
                    "需要联动修复标题来源；只改其中一层可能导致 Codex Desktop 显示错误线程名。",
                ),
                [
                    f"{scan.get('title')} | {scan.get('threadId')} | sqlite={scan.get('sqliteTitle')} | session_index={scan.get('sessionIndexTitle')} | latest_event={scan.get('latestThreadNameUpdated')}"
                    for scan in title_event_mismatch_threads[:8]
                ],
                [str(paths.session_index_path), str(paths.database_path)],
            )
        rollout_jsonl_status = "pass"
        if jsonl_parse_error_threads or invalid_image_url_threads:
            rollout_jsonl_status = "critical"
        add_check(
            "threads.rollout_jsonl_integrity",
            "threads",
            localize(language, "Main thread rollout JSONL integrity", "主线程 rollout JSONL 完整性"),
            rollout_jsonl_status,
            localize(
                language,
                "Main thread rollout JSONL files have valid rows and safe image_url fields; valid embedded images and repeated checkpoints are tracked separately as storage signals.",
                "主线程 rollout JSONL 行有效，且 image_url 字段安全；合法嵌入图片和重复 checkpoint 仅作为独立存储信号统计。",
            )
            if rollout_jsonl_status == "pass"
            else localize(
                language,
                f"{len(jsonl_parse_error_threads)} threads have parse errors, {len(invalid_image_url_threads)} have invalid image_url values, {len(embedded_image_threads)} contain embedded image URLs, and {len(repeated_compacted_threads)} have repeated compacted records.",
                f"{len(jsonl_parse_error_threads)} 条线程存在 JSONL 解析错误，{len(invalid_image_url_threads)} 条线程存在无效 image_url，{len(embedded_image_threads)} 条线程仍含嵌入图片 URL，{len(repeated_compacted_threads)} 条线程存在重复 compacted 记录。",
            ),
            [
                f"parse_error_threads={len(jsonl_parse_error_threads)}",
                f"invalid_image_url_threads={len(invalid_image_url_threads)}",
                f"embedded_image_threads={len(embedded_image_threads)}",
                f"repeated_compacted_threads={len(repeated_compacted_threads)}",
                *[
                    rollout_jsonl_evidence(scan)
                    for scan in (jsonl_parse_error_threads + invalid_image_url_threads + embedded_image_threads + repeated_compacted_threads)[:8]
                ],
            ],
        )
        if jsonl_parse_error_threads:
            add_issue(
                "threads.rollout_jsonl_parse_errors",
                "critical",
                "threads",
                localize(language, "Main thread rollout JSONL has parse errors", "主线程 rollout JSONL 存在解析错误"),
                localize(
                    language,
                    f"{len(jsonl_parse_error_threads)} main thread rollout files contain invalid JSONL rows.",
                    f"{len(jsonl_parse_error_threads)} 条主线程 rollout 文件包含无效 JSONL 行。",
                ),
                localize(
                    language,
                    "Restore from backup or remove only the invalid rows after confirming they are not user-visible conversation records.",
                    "先从备份恢复，或在确认不是用户可见对话记录后只移除无效行。",
                ),
                [rollout_jsonl_evidence(scan) for scan in jsonl_parse_error_threads[:8]],
                [str(scan.get("rolloutPath") or "") for scan in jsonl_parse_error_threads[:8]],
            )
        if invalid_image_url_threads:
            add_issue(
                "threads.rollout_invalid_image_url",
                "critical",
                "threads",
                localize(language, "Main thread rollout contains invalid image_url fields", "主线程 rollout 包含无效 image_url 字段"),
                localize(
                    language,
                    f"{len(invalid_image_url_threads)} main thread rollout files contain image_url values that are not http, https or data:image URLs.",
                    f"{len(invalid_image_url_threads)} 条主线程 rollout 文件包含非 http、https 或 data:image 的 image_url 值。",
                ),
                localize(
                    language,
                    "Run a slimming repair that removes only invalid image_url fields while preserving response text, event_msg, and encrypted_content.",
                    "执行只移除无效 image_url 字段的瘦身修复，同时保留 response 文本、event_msg 和 encrypted_content。",
                ),
                [rollout_jsonl_evidence(scan) for scan in invalid_image_url_threads[:8]],
                [str(scan.get("rolloutPath") or "") for scan in invalid_image_url_threads[:8]],
            )
        if embedded_image_threads or repeated_compacted_threads:
            add_issue(
                "threads.rollout_size_bloat",
                "warning",
                "storage",
                localize(language, "Main thread rollout has embedded image or repeated compacted bloat", "主线程 rollout 存在嵌入图片或重复 compacted 膨胀"),
                localize(
                    language,
                    f"{len(embedded_image_threads)} main threads still contain embedded image_url data and {len(repeated_compacted_threads)} have repeated compacted records.",
                    f"{len(embedded_image_threads)} 条主线程仍含嵌入 image_url 数据，{len(repeated_compacted_threads)} 条主线程存在重复 compacted 记录。",
                ),
                localize(
                    language,
                    "Preview slimming first; for main threads, keep event_msg visible conversation records and do not rewrite encrypted_content.",
                    "先预览瘦身；对主线程必须保留 event_msg 可见对话记录，且不要改写 encrypted_content。",
                ),
                [
                    rollout_jsonl_evidence(scan)
                    for scan in (embedded_image_threads + repeated_compacted_threads)[:8]
                ],
                [
                    str(scan.get("rolloutPath") or "")
                    for scan in (embedded_image_threads + repeated_compacted_threads)[:8]
                ],
            )
        if missing_threads:
            add_issue(
                "threads.missing_rollouts",
                "critical",
                "threads",
                localize(language, "Threads reference missing JSONL files", "线程引用的 JSONL 文件缺失"),
                localize(language, f"{len(missing_threads)} thread rows point to rollout files that do not exist.", f"{len(missing_threads)} 条线程记录指向不存在的 rollout 文件。"),
                localize(language, "Restore the missing JSONL files from backup or archive the stale rows after confirming they are not needed.", "从备份恢复缺失 JSONL；确认不需要后再归档陈旧记录。"),
                [f"{thread.get('title')} | {thread.get('id')}" for thread in missing_threads[:8]],
                [str(thread.get("rolloutPath") or "") for thread in missing_threads[:8]],
            )
        if repair_threads:
            add_issue(
                "threads.needs_visibility_repair",
                "warning",
                "threads",
                localize(language, "Threads need visibility repair", "存在需要可见性修复的线程"),
                localize(language, f"{len(repair_threads)} main threads have inconsistent user-event, session-index or archive state.", f"{len(repair_threads)} 条主线程的用户事件、会话索引或归档状态不一致。"),
                localize(language, "Open the thread list with filter=Needs repair and repair the specific rows after checking their evidence.", "在线程页切到“需修复”过滤，核验证据后逐条修复。"),
                [f"{thread.get('title')} | {thread.get('id')}" for thread in repair_threads[:8]],
                [str(thread.get("rolloutPath") or "") for thread in repair_threads[:8]],
            )
        add_check(
            "threads.empty_shells",
            "threads",
            localize(language, "Empty interrupted thread shells", "空的中断线程壳"),
            "info" if empty_shell_threads else "pass",
            localize(
                language,
                "No interrupted thread shells were found in the visible repair set.",
                "可见修复集合中没有空的中断线程壳。",
            )
            if not empty_shell_threads
            else localize(
                language,
                "Some visible thread entries have only an interruption placeholder and a title, without recoverable user-visible conversation content.",
                "部分可见线程条目只有中断占位记录和标题，没有可恢复的用户可见对话内容。",
            ),
            [
                f"empty_shells={len(empty_shell_threads)}",
                *[
                    f"{thread.get('title')} | {thread.get('id')} | rollout_status={thread.get('rolloutDisplayStatus')}"
                    for thread in empty_shell_threads[:8]
                ],
            ],
            [str(thread.get("rolloutPath") or "") for thread in empty_shell_threads[:8]],
        )
        total_storage_bytes = int(summary.get("totalStorageBytes") or 0)
        if total_storage_bytes >= 2 * 1024 * 1024 * 1024 or large_threads:
            add_issue(
                "threads.large_storage",
                "warning",
                "storage",
                localize(language, "Thread storage is large", "线程存储体量较大"),
                localize(language, f"Thread JSONL storage totals {total_storage_bytes} bytes.", f"线程 JSONL 合计 {total_storage_bytes} 字节。"),
                localize(language, "Use the size sort and slimming preview on the largest threads; avoid blind deletion.", "按存储空间排序，对最大线程先做瘦身预览；不要盲删。"),
                [f"{thread.get('title')} | {thread.get('fileSizeBytes')} bytes" for thread in large_threads[:8]],
                [str(thread.get("rolloutPath") or "") for thread in large_threads[:8]],
            )
        doctor_slow_threads = [
            thread
            for thread in sorted(threads, key=lambda item: int(item.get("fileSizeBytes") or 0), reverse=True)
            if int(thread.get("fileSizeBytes") or 0) >= 250 * 1024 * 1024
        ]
        doctor_slow_thread_count = len(doctor_slow_threads)
        doctor_scan_risk = total_storage_bytes >= 4 * 1024 * 1024 * 1024 or bool(doctor_slow_threads)
        add_check(
            "runtime.codex_doctor_sessions_scan_risk",
            "runtime",
            localize(language, "Official codex doctor sessions scan risk", "官方 codex doctor 会话扫描风险"),
            "warning" if doctor_scan_risk else "pass",
            localize(
                language,
                "The sessions tree is small enough that official codex doctor should not be dominated by JSONL scanning.",
                "sessions 目录体量较小，官方 codex doctor 不应主要被 JSONL 扫描拖慢。",
            )
            if not doctor_scan_risk
            else localize(
                language,
                "The sessions tree is large enough that official codex doctor can become very slow or time out while scanning thread JSONL files.",
                "sessions 目录体量较大，官方 codex doctor 扫描线程 JSONL 时可能明显变慢或超时。",
            ),
            [
                f"total_thread_storage_bytes={total_storage_bytes}",
                f"threads_over_250mb={len(doctor_slow_threads)}",
                *[f"{thread.get('title')} | {thread.get('fileSizeBytes')} bytes" for thread in doctor_slow_threads[:8]],
            ],
            [str(thread.get("rolloutPath") or "") for thread in doctor_slow_threads[:8]],
        )
        if doctor_scan_risk:
            add_issue(
                "runtime.codex_doctor_sessions_scan_risk",
                "warning",
                "runtime",
                localize(language, "Official codex doctor can time out on sessions", "官方 codex doctor 可能因 sessions 超时"),
                localize(
                    language,
                    "The current Codex Home contains very large rollout JSONL files; official codex doctor may spend most of its time scanning them before reporting plugin or runtime checks.",
                    "当前 Codex Home 包含很大的 rollout JSONL 文件；官方 codex doctor 可能在报告插件或运行时检查前，把大量时间花在扫描这些文件上。",
                ),
                localize(
                    language,
                    "Use Codex Home Manager diagnostics for fast local checks, then slim the largest threads after previewing exactly what will be removed.",
                    "先用 Codex Home Manager 体检做快速本地检查，再对最大线程执行瘦身预览，确认会移除什么后再处理。",
                ),
                [
                    f"total_thread_storage_bytes={total_storage_bytes}",
                    f"threads_over_250mb={len(doctor_slow_threads)}",
                    *[f"{thread.get('title')} | {thread.get('fileSizeBytes')} bytes" for thread in doctor_slow_threads[:8]],
                ],
                [str(thread.get("rolloutPath") or "") for thread in doctor_slow_threads[:8]],
            )

    stale_restore_scan = scan_stale_restore_artifacts(paths.codex_home_path)
    stale_restore_artifacts = stale_restore_scan["artifacts"]
    stale_restore_scan_errors = stale_restore_scan["scanErrors"]
    stale_restore_status = "warning" if stale_restore_artifacts or stale_restore_scan_errors else "pass"
    add_check(
        "plugins.stale_restore_artifacts",
        "plugins",
        localize(language, "Abandoned plugin restore transactions", "遗留插件恢复事务"),
        stale_restore_status,
        localize(
            language,
            "No abandoned top-level plugin restore transaction artifacts were found.",
            "未发现遗留在 CODEX_HOME 顶层的插件恢复事务目录。",
        )
        if stale_restore_status == "pass"
        else localize(
            language,
            "Codex Home contains abandoned or unreadable plugin restore transaction state.",
            "Codex Home 中存在遗留或无法读取的插件恢复事务状态。",
        ),
        [
            f"artifacts={len(stale_restore_artifacts)}",
            f"scan_errors={len(stale_restore_scan_errors)}",
            *stale_restore_artifacts[:8],
            *stale_restore_scan_errors[:4],
        ],
        stale_restore_artifacts[:8] or [str(paths.codex_home_path)],
    )
    if stale_restore_status == "warning":
        add_issue(
            "plugins.stale_restore_artifacts",
            "warning",
            "plugins",
            localize(language, "Plugin restore transaction was left behind", "插件恢复事务未完成"),
            localize(
                language,
                "A previous plugin-state restore did not finish cleaning its transaction directory, or the Codex Home root could not be scanned safely.",
                "之前的插件状态恢复未完成事务目录清理，或当前无法安全扫描 Codex Home 根目录。",
            ),
            localize(
                language,
                "Fully exit Codex Desktop, verify the transaction-name pattern, then move the artifacts into the current D:\\Backup repair run instead of deleting them.",
                "完全退出 Codex Desktop，核对事务目录命名后，将其移动到本次 D:\\Backup 修复批次中留存，不要直接删除。",
            ),
            [*stale_restore_artifacts[:8], *stale_restore_scan_errors[:4]],
            stale_restore_artifacts[:8] or [str(paths.codex_home_path)],
        )

    config_text = read_text_safely(paths.config_path)
    managed_config_path = paths.codex_home_path / "managed_config.toml"
    managed_config_text = read_text_safely(managed_config_path)
    node_repl_contract = node_repl_effective_contract(config_text, managed_config_text)
    bundled_marketplace_contract = bundled_marketplace_source_contract(
        config_text,
        managed_config_text,
        paths.codex_home_path,
    )
    marketplace_path = paths.codex_home_path / "cache" / "bundled-marketplaces" / "openai-bundled" / ".agents" / "plugins" / "marketplace.json"
    runtime_marketplace_path = (
        paths.codex_home_path
        / ".tmp"
        / "bundled-marketplaces"
        / "openai-bundled"
        / ".agents"
        / "plugins"
        / "marketplace.json"
    )
    marketplace_ok = marketplace_path.exists()
    add_check(
        "plugins.marketplace",
        "plugins",
        localize(language, "OpenAI bundled marketplace", "OpenAI bundled marketplace"),
        "pass" if marketplace_ok else "critical",
        localize(language, "Bundled marketplace metadata is present.", "内置 marketplace 元数据存在。")
        if marketplace_ok
        else localize(language, "Bundled marketplace metadata is missing.", "内置 marketplace 元数据缺失。"),
        [path_exists_text(marketplace_path)],
        [str(marketplace_path)],
    )
    if not marketplace_ok:
        add_issue(
            "plugins.marketplace_missing",
            "critical",
            "plugins",
            localize(language, "Bundled plugin marketplace is missing", "内置插件 marketplace 缺失"),
            localize(language, "Plugin install/repair cannot reliably resolve browser, sites, chrome or computer-use bundles.", "插件安装/修复将无法可靠解析 browser、sites、chrome 或 computer-use 包。"),
            localize(language, "Restart Codex Desktop after preserving logs; if it is still missing, reinstall the affected bundled plugins.", "先保留日志后重启 Codex Desktop；仍缺失时重新安装受影响的内置插件。"),
            [path_exists_text(marketplace_path)],
            [str(marketplace_path)],
        )

    bundled_marketplace_source_conflicts = list(bundled_marketplace_contract["conflicts"])
    if not runtime_marketplace_path.exists():
        bundled_marketplace_source_conflicts.append(
            f"runtime marketplace metadata missing: {runtime_marketplace_path}"
        )
    bundled_marketplace_source_ok = not bundled_marketplace_source_conflicts
    add_check(
        "plugins.bundled_marketplace_source",
        "plugins",
        localize(language, "Desktop bundled marketplace source", "Desktop 内置 marketplace 来源"),
        "pass" if bundled_marketplace_source_ok else "critical",
        localize(
            language,
            "Runtime registrations are absent or point to Desktop-owned sources, and managed policy does not pin them.",
            "运行层登记为空或指向 Desktop 自有来源，托管策略未固定这些来源。",
        )
        if bundled_marketplace_source_ok
        else localize(
            language,
            "The configured bundled marketplace conflicts with the source owned by Codex Desktop.",
            "配置中的内置 marketplace 与 Codex Desktop 所有的来源冲突。",
        ),
        [
            f"expected_source={bundled_marketplace_contract['expectedSource']}",
            f"runtime_source={bundled_marketplace_contract['runtime']['source']!r}",
            f"managed_source={bundled_marketplace_contract['managed']['source']!r}",
            f"expected_primary_runtime_source={bundled_marketplace_contract['expectedPrimaryRuntimeSource']}",
            f"primary_runtime_source={bundled_marketplace_contract['primaryRuntime']['source']!r}",
            f"primary_managed_source={bundled_marketplace_contract['primaryManaged']['source']!r}",
            path_exists_text(runtime_marketplace_path),
            *bundled_marketplace_source_conflicts,
        ],
        [str(paths.config_path), str(managed_config_path), str(runtime_marketplace_path)],
    )
    if not bundled_marketplace_source_ok:
        add_issue(
            "plugins.bundled_marketplace_source_conflict",
            "critical",
            "plugins",
            localize(language, "Bundled marketplace source conflict", "内置 marketplace 来源冲突"),
            localize(
                language,
                "Codex Desktop cannot reconcile its runtime bundled marketplace when the same marketplace name is registered from a different persistent source; Browser can be uninstalled during startup and not restored.",
                "同名 marketplace 被注册到另一个持久来源时，Codex Desktop 无法协调自己的运行时内置 marketplace；Browser 可能在启动时被卸载且无法恢复。",
            ),
            localize(
                language,
                "Keep Desktop-owned runtime registration only when it points to the current .tmp source. Remove marketplace pins from managed policy and verify the official plugin registry after startup.",
                "仅当 Desktop 运行层登记指向当前 .tmp 来源时保留它；从托管策略中移除 marketplace 固定项，并在启动后复核官方插件注册表。",
            ),
            bundled_marketplace_source_conflicts,
            [str(paths.config_path), str(managed_config_path), str(runtime_marketplace_path)],
        )

    plugin_requirements = {
        "browser": [
            ".codex-plugin/plugin.json",
            "skills/control-in-app-browser/SKILL.md",
            "scripts/browser-client.mjs",
        ],
        "sites": [
            ".codex-plugin/plugin.json",
            "skills/sites-hosting/SKILL.md",
            "skills/sites-building/SKILL.md",
        ],
        "chrome": [
            ".codex-plugin/plugin.json",
            "skills/control-chrome/SKILL.md",
            "scripts/browser-client.mjs",
            "scripts/check-extension-installed.js",
        ],
        "computer-use": [
            ".codex-plugin/plugin.json",
            "skills/computer-use/SKILL.md",
            "scripts/computer-use-client.mjs",
        ],
        "latex": [
            ".codex-plugin/plugin.json",
            "skills/latex-compile/SKILL.md",
        ],
    }
    plugin_cache_root = paths.codex_home_path / "plugins" / "cache" / "openai-bundled"
    broken_plugins: list[str] = []
    disabled_plugins: list[str] = []
    selected_plugin_roots: dict[str, Path] = {}
    for plugin_name, required_relative_paths in plugin_requirements.items():
        selected_root, candidate_roots, missing_files = select_bundled_plugin_root(
            plugin_cache_root,
            plugin_name,
            required_relative_paths,
        )
        selected_plugin_roots[plugin_name] = selected_root
        config_enabled = config_plugin_enabled(config_text, plugin_name)
        managed_enabled = config_plugin_enabled(managed_config_text, plugin_name) if managed_config_text else False
        plugin_ok = selected_root.exists() and not missing_files and config_enabled and managed_enabled
        add_check(
            f"plugins.{plugin_name}",
            "plugins",
            f"{plugin_name}@openai-bundled",
            "pass" if plugin_ok else "critical",
            localize(language, "Plugin cache and config are aligned.", "插件缓存与配置一致。")
            if plugin_ok
            else localize(language, "Plugin cache or enabled config is incomplete.", "插件缓存或启用配置不完整。"),
            [
                f"selected_root={path_exists_text(selected_root)}",
                f"candidate_roots={', '.join(str(root) for root in candidate_roots) or '-'}",
                f"missing_files={', '.join(missing_files) or '-'}",
                f"config_enabled={config_enabled}",
                f"managed_config_enabled={managed_enabled}",
            ],
            [str(selected_root), str(paths.config_path), str(managed_config_path)],
        )
        if missing_files or not selected_root.exists():
            broken_plugins.append(plugin_name)
        if not config_enabled or not managed_enabled:
            disabled_plugins.append(plugin_name)
    if broken_plugins:
        add_issue(
            "plugins.cache_incomplete",
            "critical",
            "plugins",
            localize(language, "Bundled plugin cache is incomplete", "内置插件缓存不完整"),
            localize(language, f"Missing runtime files for: {', '.join(broken_plugins)}.", f"以下插件缺少运行时文件：{', '.join(broken_plugins)}。"),
            localize(language, "Reinstall the affected bundled plugins through Codex plugin install flow, then restart Codex Desktop and rerun diagnostics.", "通过 Codex 插件安装流程重装受影响的内置插件，然后重启 Codex Desktop 并重新体检。"),
            broken_plugins,
            [str(plugin_cache_root)],
        )
    if disabled_plugins:
        add_issue(
            "plugins.disabled_or_unmanaged",
            "warning",
            "plugins",
            localize(language, "Bundled plugins are not enabled in config", "部分内置插件未在配置中启用"),
            localize(language, f"Config entries are missing or disabled for: {', '.join(disabled_plugins)}.", f"以下插件的配置项缺失或未启用：{', '.join(disabled_plugins)}。"),
            localize(language, "Enable the same plugin entries in config.toml and managed_config.toml, then restart Codex Desktop.", "在 config.toml 和 managed_config.toml 中同步启用这些插件，然后重启 Codex Desktop。"),
            disabled_plugins,
            [str(paths.config_path), str(managed_config_path)],
        )

    curated_cache_root = paths.codex_home_path / "plugins" / "cache" / "openai-curated"
    enabled_curated_plugins = sorted(
        set(enabled_configured_plugins(config_text, "openai-curated"))
        | set(enabled_configured_plugins(managed_config_text, "openai-curated"))
    )
    curated_runtime_missing: list[str] = []
    curated_runtime_evidence: list[str] = []
    for plugin_name in enabled_curated_plugins:
        candidate_roots = bundled_plugin_roots(curated_cache_root, plugin_name)
        selected_root = next(
            (candidate_root for candidate_root in candidate_roots if (candidate_root / ".codex-plugin" / "plugin.json").exists()),
            candidate_roots[0] if candidate_roots else curated_cache_root / plugin_name / "latest",
        )
        plugin_manifest_path = selected_root / ".codex-plugin" / "plugin.json"
        skill_count = len(list((selected_root / "skills").glob("*/SKILL.md"))) if (selected_root / "skills").exists() else 0
        curated_runtime_evidence.append(
            f"{plugin_name}: selected_root={path_exists_text(selected_root)} | plugin_json={plugin_manifest_path.exists()} | skills={skill_count}"
        )
        if not selected_root.exists() or not plugin_manifest_path.exists():
            curated_runtime_missing.append(plugin_name)
    add_check(
        "plugins.curated_runtime_cache",
        "plugins",
        localize(language, "Curated plugin runtime cache", "Curated 插件运行时缓存"),
        "warning" if curated_runtime_missing else "pass" if enabled_curated_plugins else "info",
        localize(language, "Enabled curated plugin runtime roots are present.", "已启用 curated 插件的运行时目录存在。")
        if not curated_runtime_missing
        else localize(language, "One or more enabled curated plugins are missing runtime metadata.", "一个或多个已启用 curated 插件缺少运行时元数据。"),
        [
            f"enabled_curated={', '.join(enabled_curated_plugins) or '-'}",
            *curated_runtime_evidence[:16],
        ],
        [str(curated_cache_root)],
    )
    if curated_runtime_missing:
        add_issue(
            "plugins.curated_runtime_cache_incomplete",
            "warning",
            "plugins",
            localize(language, "Curated plugin runtime cache is incomplete", "Curated 插件运行时缓存不完整"),
            localize(language, f"Missing runtime metadata for: {', '.join(curated_runtime_missing)}.", f"以下 curated 插件缺少运行时元数据：{', '.join(curated_runtime_missing)}。"),
            localize(language, "Refresh or reinstall the affected curated plugins, then restart Codex Desktop and rerun diagnostics.", "刷新或重装受影响的 curated 插件，然后重启 Codex Desktop 并重新体检。"),
            curated_runtime_missing,
            [str(curated_cache_root)],
        )

    curated_runtime_link_scan = scan_runtime_junctions(curated_cache_root)
    curated_broken_runtime_links = curated_runtime_link_scan["brokenLinks"]
    curated_runtime_link_status = (
        "critical"
        if curated_broken_runtime_links
        else "pass"
        if curated_runtime_link_scan["scannedLinks"]
        else "info"
    )
    add_check(
        "plugins.curated_runtime_links",
        "plugins",
        localize(language, "Curated runtime compatibility links", "Curated runtime 兼容链接"),
        curated_runtime_link_status,
        localize(language, "Curated runtime junctions resolve to existing runtime directories.", "Curated runtime junction 均指向存在的运行时目录。")
        if curated_runtime_link_status == "pass"
        else localize(language, "One or more curated runtime junctions point to missing directories.", "一个或多个 curated runtime junction 指向不存在的目录。")
        if curated_runtime_link_status == "critical"
        else localize(language, "No curated runtime compatibility junctions were found.", "未发现 curated runtime 兼容 junction。"),
        [
            f"scanned_links={curated_runtime_link_scan['scannedLinks']}",
            f"broken_links={len(curated_broken_runtime_links)}",
            *curated_broken_runtime_links[:12],
            *curated_runtime_link_scan["sampleLinks"][:6],
        ],
        curated_broken_runtime_links[:12],
    )
    if curated_broken_runtime_links:
        add_issue(
            "plugins.curated_runtime_link_broken",
            "critical",
            "plugins",
            localize(language, "Curated runtime compatibility links are broken", "Curated runtime 兼容链接断裂"),
            localize(language, "Some plugin cache junctions resolve to missing runtime directories, so advertised skill paths can fail even when a newer runtime exists.", "部分插件缓存 junction 指向缺失的运行时目录；即使存在较新的 runtime，会话暴露的技能路径仍可能无法读取。"),
            localize(language, "Create non-destructive compatibility junctions from the missing runtime aliases to the installed runtime, or refresh the affected plugin cache and restart Codex Desktop.", "为缺失的 runtime alias 创建指向已安装 runtime 的非破坏性兼容 junction，或刷新受影响插件缓存后重启 Codex Desktop。"),
            curated_broken_runtime_links[:12],
            curated_broken_runtime_links[:12],
        )

    curated_manifest_scan = scan_curated_marketplace_manifest_warnings(paths.codex_home_path)
    curated_manifest_findings = [
        *curated_manifest_scan["invalidPrompts"],
        *curated_manifest_scan["invalidIconPaths"],
        *curated_manifest_scan["parseErrors"],
    ]
    curated_manifest_status = (
        "warning"
        if curated_manifest_findings
        else "pass"
        if curated_manifest_scan["scannedManifests"]
        else "info"
    )
    add_check(
        "plugins.curated_marketplace_manifests",
        "plugins",
        localize(language, "Curated marketplace manifests", "Curated marketplace 清单"),
        curated_manifest_status,
        localize(language, "Curated marketplace plugin manifests satisfy the loader constraints.", "Curated marketplace 插件清单满足 loader 约束。")
        if curated_manifest_status == "pass"
        else localize(language, "One or more curated marketplace plugin manifests violate loader constraints.", "一个或多个 curated marketplace 插件清单违反 loader 约束。")
        if curated_manifest_status == "warning"
        else localize(language, "Curated marketplace plugin manifests were not found in the local temporary cache.", "本地临时缓存中没有找到 curated marketplace 插件清单。"),
        [
            f"root={curated_manifest_scan['root']}",
            f"root_exists={curated_manifest_scan['rootExists']}",
            f"scanned_manifests={curated_manifest_scan['scannedManifests']}",
            f"invalid_prompts={len(curated_manifest_scan['invalidPrompts'])}",
            f"invalid_icon_paths={len(curated_manifest_scan['invalidIconPaths'])}",
            f"parse_errors={len(curated_manifest_scan['parseErrors'])}",
            *curated_manifest_findings[:12],
            *curated_manifest_scan["samples"][:6],
        ],
        [curated_manifest_scan["root"], *curated_manifest_findings[:12]],
    )
    if curated_manifest_findings:
        add_issue(
            "plugins.curated_marketplace_manifest_warnings",
            "warning",
            "plugins",
            localize(language, "Curated marketplace manifest warnings", "Curated marketplace 清单存在告警"),
            localize(
                language,
                "Codex can warn while building tools even for not-installed marketplace plugins when their cached manifests contain invalid default prompts or icon paths.",
                "即使 marketplace 插件未安装，只要本地缓存清单含有非法 defaultPrompt 或 icon 路径，Codex 构建工具时仍可能持续写入告警日志。",
            ),
            localize(
                language,
                "Back up and repair the specific cached plugin.json fields, then restart Codex Desktop; if the cache is regenerated with the same data, treat it as an upstream marketplace issue.",
                "备份并修复具体 cached plugin.json 字段，然后重启 Codex Desktop；如果缓存刷新后同样复现，应当按上游 marketplace 清单问题处理。",
            ),
            curated_manifest_findings[:12],
            [curated_manifest_scan["root"], *curated_manifest_findings[:12]],
        )

    curated_configured_plugins = sorted(
        set(configured_plugins(config_text, "openai-curated"))
        | set(configured_plugins(managed_config_text, "openai-curated"))
    )
    curated_marketplace_plugins = sorted(set(curated_manifest_scan.get("pluginNames") or []))
    stale_curated_config_plugins = sorted(set(curated_configured_plugins) - set(curated_marketplace_plugins))
    curated_marketplace_config_status = (
        "warning"
        if stale_curated_config_plugins
        else "pass"
        if curated_marketplace_plugins
        else "info"
    )
    add_check(
        "plugins.curated_marketplace_config",
        "plugins",
        localize(language, "Curated marketplace config entries", "Curated marketplace 配置项"),
        curated_marketplace_config_status,
        localize(language, "Configured curated plugins exist in the current marketplace cache.", "已配置的 curated 插件存在于当前 marketplace 缓存。")
        if curated_marketplace_config_status == "pass"
        else localize(language, "Some configured curated plugins are no longer present in the current marketplace cache.", "部分已配置 curated 插件已不在当前 marketplace 缓存中。")
        if curated_marketplace_config_status == "warning"
        else localize(language, "Curated marketplace plugin names could not be derived from the local cache.", "无法从本地缓存推导 curated marketplace 插件名。"),
        [
            f"configured_curated={len(curated_configured_plugins)}",
            f"marketplace_plugins={len(curated_marketplace_plugins)}",
            f"stale_config_entries={len(stale_curated_config_plugins)}",
            *[f"stale={plugin_name}" for plugin_name in stale_curated_config_plugins[:12]],
        ],
        [str(paths.config_path), str(managed_config_path), curated_manifest_scan["root"]],
    )
    if stale_curated_config_plugins:
        add_issue(
            "plugins.curated_marketplace_config_stale",
            "warning",
            "plugins",
            localize(language, "Curated plugin config references stale marketplace entries", "Curated 插件配置引用了失效 marketplace 项"),
            localize(
                language,
                f"Configured curated plugins are absent from the current marketplace cache: {', '.join(stale_curated_config_plugins)}.",
                f"以下已配置 curated 插件不在当前 marketplace 缓存中：{', '.join(stale_curated_config_plugins)}。",
            ),
            localize(
                language,
                "Remove disabled stale plugin blocks or refresh the marketplace before relying on them; stale configured blocks can keep loader warnings alive.",
                "删除已禁用的失效插件块，或刷新 marketplace 后再依赖它们；失效配置块可能持续触发 loader 告警。",
            ),
            stale_curated_config_plugins[:12],
            [str(paths.config_path), str(managed_config_path), curated_manifest_scan["root"]],
        )

    expected_skill_manifests = {
        "browser:control-in-app-browser": selected_plugin_roots.get("browser", plugin_cache_root / "browser" / "latest") / "skills" / "control-in-app-browser" / "SKILL.md",
        "chrome:control-chrome": selected_plugin_roots.get("chrome", plugin_cache_root / "chrome" / "latest") / "skills" / "control-chrome" / "SKILL.md",
        "computer-use:computer-use": selected_plugin_roots.get("computer-use", plugin_cache_root / "computer-use" / "latest") / "skills" / "computer-use" / "SKILL.md",
        "sites:sites-hosting": selected_plugin_roots.get("sites", plugin_cache_root / "sites" / "latest") / "skills" / "sites-hosting" / "SKILL.md",
        "sites:sites-building": selected_plugin_roots.get("sites", plugin_cache_root / "sites" / "latest") / "skills" / "sites-building" / "SKILL.md",
    }
    missing_skill_manifests = [
        f"{skill_name} -> {skill_path}"
        for skill_name, skill_path in expected_skill_manifests.items()
        if not skill_path.exists()
    ]
    add_check(
        "plugins.skill_manifests",
        "plugins",
        localize(language, "Bundled browser skill manifests", "内置浏览器/桌面控制技能入口"),
        "pass" if not missing_skill_manifests else "critical",
        localize(language, "Browser, Chrome, Sites and Computer Use skill manifests are readable.", "Browser、Chrome、Sites 和 Computer Use 的技能入口文件可读。")
        if not missing_skill_manifests
        else localize(language, "One or more bundled browser-control skill manifests are missing.", "一个或多个内置浏览器/桌面控制技能入口文件缺失。"),
        [path_exists_text(path) for path in expected_skill_manifests.values()],
        [str(path) for path in expected_skill_manifests.values()],
    )
    if missing_skill_manifests:
        add_issue(
            "plugins.skill_manifest_missing",
            "critical",
            "plugins",
            localize(language, "Bundled control skills are missing", "内置控制类技能入口缺失"),
            localize(language, "The skill manifest layer is incomplete even if the plugin directory exists.", "即使插件目录存在，技能清单层也不完整。"),
            localize(language, "Reinstall the affected bundled plugin and restart Codex Desktop before relying on Browser, Chrome, Sites or Computer Use.", "重新安装受影响的内置插件并重启 Codex Desktop 后，再使用 Browser、Chrome、Sites 或 Computer Use。"),
            missing_skill_manifests,
            [str(plugin_cache_root)],
        )

    advertised_skill_scan = scan_advertised_skill_paths(paths.codex_home_path, snapshot)
    advertised_missing_paths = advertised_skill_scan["missingPaths"]
    advertised_external_missing_paths = advertised_skill_scan["externalMissingPaths"]
    advertised_status = (
        "warning"
        if advertised_missing_paths
        else "info"
        if advertised_external_missing_paths
        else "pass"
    )
    add_check(
        "plugins.advertised_skill_paths",
        "plugins",
        localize(language, "Advertised session skill paths", "会话暴露技能路径"),
        advertised_status,
        localize(
            language,
            "No missing SKILL.md path was found in the sampled local state.",
            "抽样本地状态中没有发现缺失的 SKILL.md 路径。",
        )
        if advertised_status == "pass"
        else localize(language, "Recently advertised skill paths include missing files.", "近期会话暴露的技能路径包含缺失文件。")
        if advertised_status == "warning"
        else localize(
            language,
            "Only historical or external Codex Home skill path drift was found in the sampled local state.",
            "抽样本地状态中仅发现历史或外部 Codex Home 的技能路径漂移。",
        )
        if advertised_external_missing_paths
        else localize(language, "No advertised skill path references were found in the sampled local state.", "抽样本地状态中没有发现会话暴露的技能路径引用。"),
        [
            f"scanned_paths={len(advertised_skill_scan['scannedPaths'])}",
            f"referenced_paths={advertised_skill_scan['referencedCount']}",
            f"missing_paths={len(advertised_missing_paths)}",
            f"external_missing_paths={len(advertised_external_missing_paths)}",
            *advertised_missing_paths[:10],
            *[f"external:{path_text}" for path_text in advertised_external_missing_paths[:6]],
        ],
        advertised_missing_paths[:12],
    )
    if advertised_missing_paths:
        add_issue(
            "plugins.advertised_skill_path_missing",
            "warning",
            "plugins",
            localize(language, "Advertised skill paths are missing", "会话暴露的技能路径缺失"),
            localize(language, "The current or recent Codex session advertised SKILL.md paths that do not exist on disk.", "当前或近期 Codex 会话暴露了磁盘上不存在的 SKILL.md 路径。"),
            localize(language, "Refresh the plugin cache or create a non-destructive compatibility junction from the stale runtime path to the installed runtime, then restart Codex Desktop.", "刷新插件缓存，或为旧 runtime 路径创建指向已安装 runtime 的非破坏性兼容 junction，然后重启 Codex Desktop。"),
            [
                f"{path_text} | sources={', '.join(advertised_skill_scan['missingSources'].get(path_text, []))}"
                for path_text in advertised_missing_paths[:10]
            ],
            advertised_missing_paths[:12],
        )

    runtime_blocker_scan = scan_recent_runtime_blocker_messages(paths.codex_home_path, snapshot)
    runtime_blocker_findings = runtime_blocker_scan["findings"]
    runtime_blocker_script_compat_findings = [
        finding
        for finding in runtime_blocker_findings
        if finding.startswith("node_global_websocket_missing")
    ]
    runtime_blocker_current_structural_problem = bool(missing_skill_manifests or advertised_missing_paths)
    runtime_blocker_status = (
        "pass"
        if not runtime_blocker_findings
        else "warning"
        if runtime_blocker_current_structural_problem or runtime_blocker_script_compat_findings
        else "info"
    )
    add_check(
        "plugins.recent_runtime_blocker_messages",
        "plugins",
        localize(language, "Recent runtime blocker messages", "最近运行时阻塞消息"),
        runtime_blocker_status,
        localize(
            language,
            "Recent sessions did not report Browser/IAB, Computer Use, node_repl or plugin-cache runtime blockers.",
            "最近会话没有报告 Browser/IAB、Computer Use、node_repl 或插件缓存运行时阻塞。",
        )
        if not runtime_blocker_findings
        else localize(
            language,
            "Only historical runtime-blocker narration was found; current plugin file checks do not show the same structural failure.",
            "仅发现历史运行时阻塞叙述；当前插件文件检查没有显示同类结构性故障。",
        )
        if runtime_blocker_status == "info"
        else localize(
            language,
            "Recent sessions reported runtime blockers that can occur even when plugin files currently exist.",
            "最近会话报告过运行时阻塞；即使当前插件文件存在，也不能忽略这些真实失败证据。",
        ),
        [
            f"scanned_paths={len(runtime_blocker_scan['scannedPaths'])}",
            f"findings={len(runtime_blocker_findings)}",
            f"truncated={runtime_blocker_scan['truncated']}",
            *runtime_blocker_findings[:10],
        ],
        runtime_blocker_scan["scannedPaths"][:12],
    )
    if runtime_blocker_findings:
        runtime_blocker_issue_summary = (
            localize(
                language,
                "The evidence is historical unless a current plugin path or script compatibility check is also failing.",
                "这些证据属于历史记录；只有当前插件路径或脚本兼容检查也失败时，才按当前故障处理。",
            )
            if runtime_blocker_status == "info"
            else localize(
                language,
                "The local plugin cache or script runtime still has evidence-backed runtime problems.",
                "本地插件缓存或脚本运行时仍存在有证据支撑的运行时问题。",
            )
        )
        runtime_blocker_issue_recommendation = (
            localize(
                language,
                "Keep this as audit evidence, then run a fresh Browser/node_repl smoke before repairing plugin cache paths.",
                "把它作为审计证据保留，然后先做新的 Browser/node_repl 实连 smoke，再决定是否修插件缓存路径。",
            )
            if runtime_blocker_status == "info"
            else localize(
                language,
                "Rerun diagnostics after restart, verify Browser/node_repl live connections, and repair plugin cache, advertised paths or WebSocket compatibility only where fresh evidence reproduces.",
                "重启后重新体检，验证 Browser/node_repl 实连；只有新证据仍复现时，再修复插件缓存、暴露路径或 WebSocket 兼容层。",
            )
        )
        add_issue(
            "plugins.recent_runtime_blocker_messages",
            runtime_blocker_status,
            "plugins",
            localize(language, "Recent sessions reported runtime blockers", "最近会话报告过运行时阻塞"),
            runtime_blocker_issue_summary,
            runtime_blocker_issue_recommendation,
            runtime_blocker_findings[:12],
            runtime_blocker_scan["scannedPaths"][:12],
        )
        node_repl_transport_findings = [
            finding
            for finding in runtime_blocker_findings
            if finding.startswith("node_repl_transport_closed")
        ]
        if node_repl_transport_findings:
            node_repl_transport_severity = "warning" if runtime_blocker_current_structural_problem else "info"
            add_issue(
                "plugins.node_repl_transport_closed",
                node_repl_transport_severity,
                "plugins",
                localize(language, "Node REPL MCP transport is closed", "Node REPL MCP 连接已关闭"),
                localize(
                    language,
                    "A current or recent Codex session exposed the node_repl tools, but a node_repl/js or node_repl/js_reset call failed with Transport closed.",
                    "当前或最近的 Codex 会话暴露了 node_repl 工具，但 node_repl/js 或 node_repl/js_reset 调用返回过 Transport closed。",
                ),
                localize(
                    language,
                    "Treat this as historical unless a fresh node_repl call also fails. If it repeats after restarting Codex Desktop, inspect the node_repl MCP server process and PATH/runtime configuration.",
                    "除非新的 node_repl 调用也失败，否则先按历史证据处理；如果重启 Codex Desktop 后仍复现，再检查 node_repl MCP server 进程以及 PATH/runtime 配置。",
                ),
                node_repl_transport_findings[:8],
                runtime_blocker_scan["scannedPaths"][:12],
            )
        websocket_findings = [
            finding
            for finding in runtime_blocker_findings
            if finding.startswith("node_global_websocket_missing")
        ]
        if websocket_findings:
            add_issue(
                "plugins.node_websocket_compat_missing",
                "warning",
                "plugins",
                localize(language, "Node WebSocket compatibility is missing", "Node WebSocket 兼容层缺失"),
                localize(
                    language,
                    "A recent local script failed because this Node runtime does not expose global WebSocket.",
                    "最近的本地脚本因为当前 Node 运行时没有暴露全局 WebSocket 而失败。",
                ),
                localize(
                    language,
                    "Install the ws package in the module root used by the script or import scripts/node_websocket_compat.mjs before opening CDP/WebSocket connections.",
                    "在脚本使用的模块根目录安装 ws，或在打开 CDP/WebSocket 连接前导入 scripts/node_websocket_compat.mjs。",
                ),
                websocket_findings[:8],
                runtime_blocker_scan["scannedPaths"][:12],
            )

    add_check(
        "plugins.tool_exposure_model",
        "plugins",
        localize(language, "Current session tool exposure model", "当前会话工具暴露模型"),
        "info",
        localize(
            language,
            "Bundled Browser, Chrome and Computer Use are loaded through the generic Node REPL runtime; a separate computer-use.click or browser.click namespace is not required.",
            "Browser、Chrome 和 Computer Use 内置插件通过通用 Node REPL 运行时加载；当前会话不需要、也不一定会出现单独的 computer-use.click 或 browser.click 命名空间。",
        ),
        [
            localize(
                language,
                "Cache/config checks prove local install health, not the exact tool names exported into one conversation.",
                "缓存和配置检查只能证明本地安装健康，不能单独证明某个对话里导出了哪些工具名。",
            ),
            localize(
                language,
                "A live connection should be verified by loading browser-client.mjs or computer-use-client.mjs and running a lightweight open-tabs/list-apps probe.",
                "实连应通过加载 browser-client.mjs 或 computer-use-client.mjs，并执行轻量 open-tabs/list-apps 探测来验证。",
            ),
            localize(
                language,
                "Computer Use can be installed and reachable while a browser-window action is still stopped by URL-confidence policy.",
                "Computer Use 可以处于已安装且可连通状态，但浏览器窗口操作仍可能被 URL 置信度策略中止。",
            ),
        ],
        [
            str(selected_plugin_roots.get("browser", plugin_cache_root / "browser" / "latest")),
            str(selected_plugin_roots.get("chrome", plugin_cache_root / "chrome" / "latest")),
            str(selected_plugin_roots.get("computer-use", plugin_cache_root / "computer-use" / "latest")),
        ],
    )

    browser_backends = split_csv_text(extract_config_path(config_text, "BROWSER_USE_AVAILABLE_BACKENDS"))
    trusted_browser_clients = split_csv_text(extract_config_path(config_text, "NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S"))
    node_repl_node_path = extract_config_path(config_text, "NODE_REPL_NODE_PATH")
    node_repl_trusted_code_paths = extract_config_path(config_text, "NODE_REPL_TRUSTED_CODE_PATHS")
    browser_instruction = extract_config_path(config_text, "NODE_REPL_INSTRUCTIONS_USE_CASE_BROWSER")
    chrome_instruction = extract_config_path(config_text, "NODE_REPL_INSTRUCTIONS_USE_CASE_CHROME")
    browser_runtime_missing = []
    if not {"chrome", "iab"}.issubset(set(browser_backends)):
        browser_runtime_missing.append("BROWSER_USE_AVAILABLE_BACKENDS must include chrome and iab")
    if not trusted_browser_clients:
        browser_runtime_missing.append("NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S is empty")
    if not node_repl_node_path or not Path(node_repl_node_path).exists():
        browser_runtime_missing.append("NODE_REPL_NODE_PATH is missing or points to a missing file")
    if str(paths.codex_home_path).lower() not in node_repl_trusted_code_paths.lower():
        browser_runtime_missing.append("NODE_REPL_TRUSTED_CODE_PATHS does not include selected Codex Home")
    if not browser_instruction:
        browser_runtime_missing.append("NODE_REPL_INSTRUCTIONS_USE_CASE_BROWSER is missing")
    if not chrome_instruction:
        browser_runtime_missing.append("NODE_REPL_INSTRUCTIONS_USE_CASE_CHROME is missing")
    add_check(
        "plugins.browser_runtime_contract",
        "plugins",
        localize(language, "Browser runtime wiring", "Browser 运行时接线"),
        "pass" if not browser_runtime_missing else "warning",
        localize(language, "Node REPL is wired for in-app browser and Chrome browser backends.", "Node REPL 已接入应用内浏览器和 Chrome 浏览器后端。")
        if not browser_runtime_missing
        else localize(language, "Browser runtime environment is missing one or more required wiring values.", "Browser 运行时环境缺少一个或多个必要接线值。"),
        [
            f"BROWSER_USE_AVAILABLE_BACKENDS={','.join(browser_backends) or '-'}",
            f"NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S={len(trusted_browser_clients)} entries",
            f"NODE_REPL_NODE_PATH={node_repl_node_path or '-'}",
            f"NODE_REPL_NODE_PATH_exists={Path(node_repl_node_path).exists() if node_repl_node_path else '-'}",
            f"NODE_REPL_TRUSTED_CODE_PATHS={node_repl_trusted_code_paths or '-'}",
            f"NODE_REPL_INSTRUCTIONS_USE_CASE_BROWSER={'present' if browser_instruction else 'missing'}",
            f"NODE_REPL_INSTRUCTIONS_USE_CASE_CHROME={'present' if chrome_instruction else 'missing'}",
        ],
        [str(paths.config_path), node_repl_node_path] if node_repl_node_path else [str(paths.config_path)],
    )
    if browser_runtime_missing:
        add_issue(
            "plugins.browser_runtime_contract_incomplete",
            "warning",
            "plugins",
            localize(language, "Browser runtime wiring is incomplete", "Browser 运行时接线不完整"),
            localize(language, "Browser/Chrome files may exist, but the current runtime may not expose usable browser control.", "Browser/Chrome 文件可能存在，但当前运行时可能无法暴露可用的浏览器控制能力。"),
            localize(language, "Repair the node_repl browser environment in config.toml, restart Codex Desktop, then run a live Browser connection test.", "修复 config.toml 中的 node_repl 浏览器环境，重启 Codex Desktop 后再执行 Browser 实连测试。"),
            browser_runtime_missing,
            [str(paths.config_path)],
        )

    current_appx_for_native_hosts = inspect_current_codex_appx_install()
    current_appx_install_path = str(current_appx_for_native_hosts.get("installPath") or "")
    current_appx_resources = (
        str(Path(current_appx_install_path) / "app" / "resources")
        if current_appx_install_path
        else ""
    )
    chrome_root_for_native_hosts = selected_plugin_roots.get(
        "chrome", plugin_cache_root / "chrome" / "latest"
    )
    native_host_expected_paths = {
        "codexCliPath": str(node_repl_contract["effectiveEnv"].get("CODEX_CLI_PATH") or ""),
        "browserClientPath": str(chrome_root_for_native_hosts / "scripts" / "browser-client.mjs"),
        "extensionHostPath": str(
            chrome_root_for_native_hosts / "extension-host" / "windows" / "x64" / "extension-host.exe"
        ),
        "resourcesPath": current_appx_resources,
        "nodePath": str(node_repl_contract["effectiveEnv"].get("NODE_REPL_NODE_PATH") or ""),
        "nodeReplPath": str(node_repl_contract["effectiveCommand"] or ""),
        "codexHome": str(paths.codex_home_path),
        "nodeModuleDirs": split_path_list_text(
            str(node_repl_contract["effectiveEnv"].get("NODE_REPL_NODE_MODULE_DIRS") or "")
        ),
    }
    native_host_scan = scan_chrome_native_host_paths(
        paths.codex_home_path,
        current_appx_install=current_appx_for_native_hosts,
        expected_paths=native_host_expected_paths,
    )
    native_host_missing_paths = native_host_scan["missingPaths"]
    native_host_stale_paths = native_host_scan["staleMissingPaths"]
    native_host_wrong_appx_paths = native_host_scan["wrongAppxPaths"]
    native_host_parse_errors = native_host_scan["parseErrors"]
    native_host_status = (
        "pass" if native_host_scan["configurationComplete"] else "warning"
    )
    add_check(
        "plugins.chrome_native_hosts",
        "plugins",
        localize(language, "Chrome native host paths", "Chrome native host 路径"),
        native_host_status,
        localize(
            language,
            "Chrome native host static structure is complete and its resources path is bound to the current AppX installation; no native-messaging handshake was attempted.",
            "Chrome native host 静态结构完整，resources 路径已绑定当前 AppX 安装；本检查未执行 native messaging 握手。",
        )
        if native_host_status == "pass"
        else localize(language, "Chrome native host configuration has incomplete, missing, unreadable, or non-current-AppX runtime paths.", "Chrome native host 配置存在字段不完整、路径缺失或不可读、或未绑定当前 AppX 的问题。")
        if native_host_status == "warning"
        else localize(language, "Chrome native host configuration was not found.", "没有找到 Chrome native host 配置。"),
        [
            f"files={len(native_host_scan['existingFiles'])}/2",
            f"entries={native_host_scan['entries']}",
            f"v2_entries={native_host_scan['v2Entries']}",
            f"healthy_entries={native_host_scan['healthyEntries']}",
            f"healthy_v2_entries={native_host_scan['healthyV2Entries']}",
            f"stale_entries={native_host_scan['staleEntries']}",
            f"checked_paths={native_host_scan['checkedPaths']}",
            f"missing_paths={len(native_host_missing_paths)}",
            f"stale_missing_paths={len(native_host_stale_paths)}",
            f"wrong_appx_paths={len(native_host_wrong_appx_paths)}",
            f"exact_path_mismatches={len(native_host_scan['exactPathMismatches'])}",
            f"parse_errors={len(native_host_parse_errors)}",
            f"configuration_complete={native_host_scan['configurationComplete']}",
            f"current_appx_available={native_host_scan['currentAppxAvailable']}",
            f"current_appx_version={native_host_scan['currentAppxVersion'] or '-'}",
            f"current_appx_install_path={native_host_scan['currentAppxInstallPath'] or '-'}",
            f"current_appx_error={native_host_scan['currentAppxError'] or '-'}",
            "verification_scope=static_structure_and_current_appx_binding",
            "native_messaging_handshake_verified=False",
            *native_host_missing_paths[:8],
            *[f"historical={path_text}" for path_text in native_host_stale_paths[:8]],
            *native_host_wrong_appx_paths[:8],
            *native_host_scan["exactPathMismatches"][:8],
            *native_host_parse_errors[:4],
            *native_host_scan["samples"][:10],
        ],
        [*native_host_scan["existingFiles"], *native_host_missing_paths[:8]],
    )
    if native_host_status == "warning":
        add_issue(
            "plugins.chrome_native_host_paths_unhealthy",
            "warning",
            "plugins",
            localize(language, "Chrome native host paths are incomplete or stale", "Chrome native host 路径不完整或已陈旧"),
            localize(
                language,
                "Browser/Chrome automation can fail when required native-host fields are absent, runtime paths are missing, or resourcesPath belongs to an older Codex AppX installation.",
                "native-host 必填字段缺失、运行时路径不存在，或 resourcesPath 属于旧 Codex AppX 安装时，Browser/Chrome 自动化可能失败。",
            ),
            localize(
                language,
                "Regenerate chrome-native-hosts.json and chrome-native-hosts-v2.json with every required path present and resourcesPath under the current Get-AppxPackage InstallLocation; then perform a real native-messaging probe.",
                "重新生成 chrome-native-hosts.json 和 chrome-native-hosts-v2.json，确保所有必填路径都存在，且 resourcesPath 位于当前 Get-AppxPackage InstallLocation 下；随后执行真实 native messaging 探针。",
            ),
            [*native_host_missing_paths[:8], *native_host_wrong_appx_paths[:8], *native_host_parse_errors[:4]],
            [*native_host_scan["existingFiles"], *native_host_missing_paths[:8]],
        )

    native_messaging_scan = scan_chrome_native_messaging_manifests(paths.codex_home_path)
    native_messaging_problems = [
        *native_messaging_scan["missingManifestPaths"],
        *native_messaging_scan["parseErrors"],
        *native_messaging_scan["missingHostPaths"],
        *native_messaging_scan["foreignHomeHostPaths"],
    ]
    native_messaging_status = (
        "warning"
        if native_messaging_problems
        else "pass"
        if native_messaging_scan["manifestCount"]
        else "info"
    )
    add_check(
        "plugins.chrome_native_messaging_manifests",
        "plugins",
        localize(language, "Chrome native messaging manifests", "Chrome native messaging manifest"),
        native_messaging_status,
        localize(
            language,
            "Browser extension native messaging manifests point at the selected Codex Home.",
            "浏览器扩展 native messaging manifest 指向当前选择的 Codex Home。",
        )
        if native_messaging_status == "pass"
        else localize(
            language,
            "Browser extension native messaging manifests are missing, unreadable, or point at another Codex Home.",
            "浏览器扩展 native messaging manifest 缺失、不可读，或仍指向其它 Codex Home。",
        )
        if native_messaging_status == "warning"
        else localize(
            language,
            "No Chrome or Edge native messaging registry entry was found for Codex.",
            "未发现 Codex 的 Chrome 或 Edge native messaging 注册表项。",
        ),
        [
            f"registry_entries={len(native_messaging_scan['registryEntries'])}",
            f"manifest_count={native_messaging_scan['manifestCount']}",
            f"missing_manifests={len(native_messaging_scan['missingManifestPaths'])}",
            f"parse_errors={len(native_messaging_scan['parseErrors'])}",
            f"missing_hosts={len(native_messaging_scan['missingHostPaths'])}",
            f"foreign_home_hosts={len(native_messaging_scan['foreignHomeHostPaths'])}",
            *native_messaging_problems[:8],
            *native_messaging_scan["samples"][:8],
        ],
        [
            *native_messaging_scan["manifestFiles"],
            *native_messaging_scan["missingManifestPaths"][:4],
            *native_messaging_scan["missingHostPaths"][:4],
            *native_messaging_scan["foreignHomeHostPaths"][:4],
        ],
    )
    if native_messaging_status == "warning":
        add_issue(
            "plugins.chrome_native_messaging_manifest_stale",
            "warning",
            "plugins",
            localize(language, "Chrome native messaging manifest is stale", "Chrome native messaging manifest 已陈旧"),
            localize(
                language,
                "Chrome or Edge can start the Browser/Chrome extension host from an old Codex Home even when the selected Codex Home looks healthy.",
                "即使当前选择的 Codex Home 内部配置正常，Chrome 或 Edge 仍可能从旧 Codex Home 启动 Browser/Chrome extension-host。",
            ),
            localize(
                language,
                "Update the native messaging manifest path to the selected Codex Home, then restart Chrome or end the stale extension-host process.",
                "将 native messaging manifest 的 path 更新到当前选择的 Codex Home，然后重启 Chrome 或结束陈旧的 extension-host 进程。",
            ),
            native_messaging_problems[:8],
            [
                *native_messaging_scan["manifestFiles"],
                *native_messaging_scan["missingManifestPaths"][:4],
                *native_messaging_scan["missingHostPaths"][:4],
                *native_messaging_scan["foreignHomeHostPaths"][:4],
            ],
        )

    node_module_scan = scan_node_repl_node_module_roots(config_text, managed_config_text)
    browser_runtime_configured = bool(
        browser_backends
        or trusted_browser_clients
        or node_repl_node_path
        or node_repl_trusted_code_paths
        or browser_instruction
        or chrome_instruction
    )
    node_module_status = (
        "pass"
        if node_module_scan["playwrightRoots"]
        else "warning"
        if browser_runtime_configured or node_module_scan["roots"]
        else "info"
    )
    add_check(
        "plugins.node_repl_node_modules",
        "plugins",
        localize(language, "Node REPL Node module roots", "Node REPL Node 模块根目录"),
        node_module_status,
        localize(language, "Node REPL can resolve the Playwright package from its configured module roots.", "Node REPL 可以从已配置模块根目录解析 Playwright 包。")
        if node_module_status == "pass"
        else localize(
            language,
            "Node REPL module roots do not currently prove that Playwright can be imported.",
            "Node REPL 模块根目录当前不能证明 Playwright 可以被导入。",
        ),
        [
            f"NODE_REPL_NODE_MODULE_DIRS={node_module_scan['configuredText'] or '-'}",
            f"configured_roots={len(node_module_scan['roots'])}",
            f"existing_roots={len(node_module_scan['existingRoots'])}",
            f"playwright_roots={len(node_module_scan['playwrightRoots'])}",
            *[f"playwright_root={path_text}" for path_text in node_module_scan["playwrightRoots"][:4]],
            *[f"missing_root={path_text}" for path_text in node_module_scan["missingRoots"][:4]],
            *node_module_scan["packageSamples"][:6],
        ],
        [str(paths.config_path), *node_module_scan["roots"][:8]],
    )
    if node_module_status == "warning":
        add_issue(
            "plugins.node_repl_playwright_module_unresolved",
            "warning",
            "plugins",
            localize(language, "Node REPL cannot prove Playwright importability", "Node REPL 无法证明 Playwright 可导入"),
            localize(
                language,
                "Browser/IAB fallback checks and local browser validation can fail when node_repl cannot resolve the Playwright package.",
                "当 node_repl 无法解析 Playwright 包时，Browser/IAB fallback 检查和本地浏览器验证可能失败。",
            ),
            localize(
                language,
                "Remove managed node_repl overrides first. If the Desktop-generated runtime still lacks Playwright, repair the official Browser runtime and let Codex Desktop regenerate this path, then restart and run an import probe.",
                "先删除 managed_config 中的 node_repl 覆盖。如果 Desktop 生成的运行时仍缺少 Playwright，应修复官方 Browser 运行时并让 Codex Desktop 重新生成该路径，再重启并执行导入探测。",
            ),
            [
                f"NODE_REPL_NODE_MODULE_DIRS={node_module_scan['configuredText'] or '-'}",
                f"configured_roots={len(node_module_scan['roots'])}",
                f"existing_roots={len(node_module_scan['existingRoots'])}",
                f"playwright_roots={len(node_module_scan['playwrightRoots'])}",
                *[f"missing_root={path_text}" for path_text in node_module_scan["missingRoots"][:6]],
                *node_module_scan["packageSamples"][:6],
            ],
            [str(paths.config_path), *node_module_scan["roots"][:8]],
        )

    websocket_compat_status = (
        "pass"
        if node_module_scan["wsRoots"]
        else "info"
    )
    add_check(
        "plugins.node_repl_websocket_compat",
        "plugins",
        localize(language, "Node WebSocket fallback module", "Node WebSocket 回退模块"),
        websocket_compat_status,
        localize(
            language,
            "A ws fallback package is available for Node runtimes that do not expose global WebSocket.",
            "已提供 ws 回退包，可兼容没有全局 WebSocket 的 Node 运行时。",
        )
        if websocket_compat_status == "pass"
        else localize(
            language,
            "The Desktop-owned runtime does not include a ws fallback package. This is not a Browser or Computer Use failure; only custom CDP/WebSocket scripts need to provide their own compatible client.",
            "Desktop 管理的运行时未包含 ws 回退包。这不代表 Browser 或 Computer Use 故障；只有自定义 CDP/WebSocket 脚本需要自行提供兼容客户端。",
        ),
        [
            f"NODE_REPL_NODE_MODULE_DIRS={node_module_scan['configuredText'] or '-'}",
            f"configured_roots={len(node_module_scan['roots'])}",
            f"existing_roots={len(node_module_scan['existingRoots'])}",
            f"ws_roots={len(node_module_scan['wsRoots'])}",
            *[f"ws_root={path_text}" for path_text in node_module_scan["wsRoots"][:4]],
            *node_module_scan["packageSamples"][:6],
        ],
        [str(paths.config_path), *node_module_scan["roots"][:8]],
    )
    node_repl_asset_scan = scan_node_repl_asset_directories(config_text)
    node_repl_asset_status = (
        "critical"
        if not node_repl_asset_scan["nodeFiles"] or not node_repl_asset_scan["nodeReplFiles"]
        else "warning"
        if node_repl_asset_scan["missingFiles"]
        or node_repl_asset_scan["missingDirectories"]
        or node_repl_asset_scan["unwritableDirectories"]
        else "pass"
    )
    add_check(
        "plugins.node_repl_asset_directories",
        "plugins",
        localize(language, "Node REPL runtime asset directories", "Node REPL 运行资产目录"),
        node_repl_asset_status,
        localize(language, "Node REPL runtime executables and asset directories are reachable.", "Node REPL 运行时可执行文件和资产目录可访问。")
        if node_repl_asset_status == "pass"
        else localize(
            language,
            "Node REPL runtime assets are missing or not writable, which can stop Browser/IAB validation even when plugin files exist.",
            "Node REPL 运行资产缺失或不可写；即使插件文件存在，也可能导致 Browser/IAB 验证失败。",
        ),
        [
            f"codex_bin_root={node_repl_asset_scan['codexBinRoot'] or '-'}",
            f"directories={len(node_repl_asset_scan['directories'])}",
            f"node_repl_files={len(node_repl_asset_scan['nodeReplFiles'])}",
            f"node_files={len(node_repl_asset_scan['nodeFiles'])}",
            f"missing_directories={len(node_repl_asset_scan['missingDirectories'])}",
            f"missing_files={len(node_repl_asset_scan['missingFiles'])}",
            f"unwritable_directories={len(node_repl_asset_scan['unwritableDirectories'])}",
            *[f"node_repl={path_text}" for path_text in node_repl_asset_scan["nodeReplFiles"][:4]],
            *[f"node={path_text}" for path_text in node_repl_asset_scan["nodeFiles"][:4]],
            *node_repl_asset_scan["missingDirectories"][:4],
            *node_repl_asset_scan["missingFiles"][:4],
            *node_repl_asset_scan["unwritableDirectories"][:4],
        ],
        [
            *node_repl_asset_scan["directories"][:8],
            *node_repl_asset_scan["nodeReplFiles"][:4],
            *node_repl_asset_scan["nodeFiles"][:4],
        ],
    )
    if node_repl_asset_status != "pass":
        node_repl_asset_evidence = [
            *[f"missing_directory={path_text}" for path_text in node_repl_asset_scan["missingDirectories"][:8]],
            *[f"missing_file={path_text}" for path_text in node_repl_asset_scan["missingFiles"][:8]],
            *[f"unwritable_directory={path_text}" for path_text in node_repl_asset_scan["unwritableDirectories"][:8]],
        ]
        if not node_repl_asset_scan["nodeReplFiles"]:
            node_repl_asset_evidence.append("node_repl.exe was not found in the configured/runtime asset paths")
        if not node_repl_asset_scan["nodeFiles"]:
            node_repl_asset_evidence.append("node.exe was not found in the configured/runtime asset paths")
        add_issue(
            "plugins.node_repl_runtime_assets_unhealthy",
            "critical" if node_repl_asset_status == "critical" else "warning",
            "plugins",
            localize(language, "Node REPL runtime assets are unhealthy", "Node REPL 运行资产异常"),
            localize(
                language,
                "Browser, Chrome and Computer Use rely on the generic Node REPL runtime; missing or unwritable asset paths can make those tools disappear or fail after restart.",
                "Browser、Chrome 和 Computer Use 依赖通用 Node REPL 运行时；资产路径缺失或不可写会导致这些工具重启后消失或调用失败。",
            ),
            localize(
                language,
                "Repair Codex Desktop runtime permissions or reinstall the bundled runtime, then restart Codex Desktop and rerun the Browser live probe.",
                "修复 Codex Desktop 运行时目录权限，或重新安装内置运行时；随后重启 Codex Desktop 并重新执行 Browser 实连探测。",
            ),
            node_repl_asset_evidence,
            [
                *node_repl_asset_scan["directories"][:8],
                *node_repl_asset_scan["missingDirectories"][:8],
                *node_repl_asset_scan["missingFiles"][:8],
            ],
        )

    notify_values = extract_config_array_values(config_text, "notify")
    computer_use_root = selected_plugin_roots.get("computer-use", plugin_cache_root / "computer-use" / "latest")
    computer_use_client_path = computer_use_root / "scripts" / "computer-use-client.mjs"
    computer_use_skill_path = computer_use_root / "skills" / "computer-use" / "SKILL.md"
    computer_use_asset_paths = [computer_use_client_path, computer_use_skill_path]
    missing_computer_use_assets = [str(path) for path in computer_use_asset_paths if not path.exists()]
    computer_use_assets_ok = not missing_computer_use_assets
    computer_use_notify_paths = [value for value in notify_values if "codex-computer-use" in value.lower()]
    computer_use_notify_missing = [value for value in computer_use_notify_paths if not Path(value).exists()]
    computer_use_notify_desktop_managed = is_desktop_managed_computer_use_notify(
        notify_values,
        config_text,
        managed_config_text,
    )
    add_check(
        "plugins.computer_use_turn_end_hook",
        "plugins",
        localize(language, "Computer Use client assets", "Computer Use 客户端资产"),
        "pass" if computer_use_assets_ok else "warning",
        localize(
            language,
            "Computer Use client entry and skill manifest are present; the client loads the bundled CUA runtime internally.",
            "Computer Use 客户端入口和技能文件均存在；客户端会在内部加载内置 CUA 运行时。",
        )
        if computer_use_assets_ok
        else localize(language, "Computer Use client entry or skill manifest is missing.", "Computer Use 客户端入口或技能文件缺失。"),
        [
            f"notify_entries={len(notify_values)}",
            f"desktop_managed_notify={computer_use_notify_desktop_managed}",
            f"notify_path={computer_use_notify_paths[0] if computer_use_notify_paths else '-'}",
            f"notify_missing={', '.join(computer_use_notify_missing) or '-'}",
            path_exists_text(computer_use_client_path),
            path_exists_text(computer_use_skill_path),
        ],
        [str(paths.config_path), *(str(path) for path in computer_use_asset_paths), *(computer_use_notify_paths[:1])],
    )
    if not computer_use_assets_ok:
        add_issue(
            "plugins.computer_use_assets_missing",
            "warning",
            "plugins",
            localize(language, "Computer Use client assets are missing", "Computer Use 客户端资产缺失"),
            localize(language, "Computer Use may be listed as installed, but its client entry or skill manifest is incomplete.", "Computer Use 可能显示为已安装，但客户端入口或技能文件不完整。"),
            localize(language, "Reinstall or repair the Computer Use bundled plugin, then restart Codex Desktop and rerun diagnostics.", "重新安装或修复 Computer Use 内置插件，然后重启 Codex Desktop 并重新体检。"),
            missing_computer_use_assets,
            [str(path) for path in computer_use_asset_paths],
        )

    mcp_process_snapshot = scan_mcp_process_snapshot()
    computer_use_runtime_environment = node_repl_contract["effectiveEnv"]
    computer_use_runtime_problems = list(node_repl_contract["conflicts"])
    computer_use_node_repl_command = str(node_repl_contract.get("effectiveCommand") or "").strip()
    computer_use_node_path_text = str(
        computer_use_runtime_environment.get("NODE_REPL_NODE_PATH") or ""
    ).strip()
    computer_use_pipe_enabled = (
        str(computer_use_runtime_environment.get("SKY_CUA_NATIVE_PIPE") or "").strip() == "1"
    )
    computer_use_pipe_path = str(
        computer_use_runtime_environment.get("SKY_CUA_NATIVE_PIPE_DIRECTORY") or ""
    ).strip()
    computer_use_pipe_probe = inspect_computer_use_pipe_endpoint(computer_use_pipe_path)
    computer_use_matching_processes = node_repl_matching_processes(
        mcp_process_snapshot,
        computer_use_node_repl_command,
    )
    normalized_pipe_path = computer_use_pipe_path.replace("/", "\\").casefold()
    computer_use_pipe_contract_matches = False
    if computer_use_pipe_probe["kind"] == "windows_named_pipe":
        computer_use_pipe_contract_matches = bool(
            re.fullmatch(
                r"\\\\\.\\pipe\\codex-computer-use-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                normalized_pipe_path,
            )
        )
    elif computer_use_pipe_probe["kind"] == "filesystem_directory" and computer_use_node_repl_command:
        runtime_command_path = Path(
            os.path.expandvars(os.path.expanduser(computer_use_node_repl_command))
        )
        computer_use_pipe_contract_matches = path_is_inside_directory(
            computer_use_pipe_path,
            runtime_command_path.parent,
        )
    computer_use_pipe_runtime_bound = bool(
        computer_use_matching_processes and computer_use_pipe_contract_matches
    )
    if not node_repl_contract["runtime"]["tablePresent"]:
        computer_use_runtime_problems.append("Desktop node_repl runtime table is missing")
    if not computer_use_node_repl_command:
        computer_use_runtime_problems.append("Desktop node_repl command is missing")
    elif not Path(os.path.expandvars(os.path.expanduser(computer_use_node_repl_command))).is_file():
        computer_use_runtime_problems.append(
            f"Desktop node_repl command does not exist: {computer_use_node_repl_command}"
        )
    if not computer_use_node_path_text:
        computer_use_runtime_problems.append("NODE_REPL_NODE_PATH is missing")
    elif not Path(os.path.expandvars(os.path.expanduser(computer_use_node_path_text))).is_file():
        computer_use_runtime_problems.append(
            f"NODE_REPL_NODE_PATH does not exist: {computer_use_node_path_text}"
        )
    if not node_module_scan["firstRootHasComputerUseSky"]:
        computer_use_runtime_problems.append(
            "the first NODE_REPL_NODE_MODULE_DIRS entry does not contain the bundled @oai/sky Computer Use client"
        )
    if not computer_use_pipe_enabled:
        computer_use_runtime_problems.append("SKY_CUA_NATIVE_PIPE is not enabled")
    if not computer_use_pipe_path:
        computer_use_runtime_problems.append("SKY_CUA_NATIVE_PIPE_DIRECTORY is missing")
    elif not computer_use_pipe_probe["ready"]:
        computer_use_runtime_problems.append(
            f"SKY_CUA_NATIVE_PIPE_DIRECTORY is unavailable or inaccessible: {computer_use_pipe_probe['error'] or computer_use_pipe_path}"
        )
    if computer_use_pipe_path and not computer_use_pipe_contract_matches:
        computer_use_runtime_problems.append(
            "SKY_CUA_NATIVE_PIPE_DIRECTORY is not a current Desktop Computer Use pipe contract"
        )
    if computer_use_pipe_path and not computer_use_matching_processes:
        computer_use_runtime_problems.append(
            "no running node_repl process matches the effective Desktop runtime command"
        )
    computer_use_runtime_ok = computer_use_assets_ok and not computer_use_runtime_problems
    add_check(
        "plugins.computer_use_privileged_runtime",
        "plugins",
        localize(language, "Computer Use privileged runtime", "Computer Use 特权运行时"),
        "pass" if computer_use_runtime_ok else "critical",
        localize(
            language,
            "The effective Node REPL files, matching runtime process and native-pipe endpoint are structurally ready for Computer Use; no Computer Use protocol handshake was attempted.",
            "当前 Node REPL 文件、匹配的运行时进程和原生管道端点在结构上已就绪；本检查未执行 Computer Use 协议握手。",
        )
        if computer_use_runtime_ok
        else localize(
            language,
            "Computer Use files may exist, but the effective Node REPL configuration cannot expose its privileged native-pipe runtime.",
            "Computer Use 文件虽然可能存在，但当前有效 Node REPL 配置无法暴露其特权原生管道运行时。",
        ),
        [
            f"effective_command={computer_use_node_repl_command or '-'}",
            f"NODE_REPL_NODE_PATH={computer_use_node_path_text or '-'}",
            f"first_module_root={node_module_scan['firstRoot'] or '-'}",
            f"first_root_has_computer_use_sky={node_module_scan['firstRootHasComputerUseSky']}",
            f"SKY_CUA_NATIVE_PIPE={computer_use_runtime_environment.get('SKY_CUA_NATIVE_PIPE') or '-'}",
            f"SKY_CUA_NATIVE_PIPE_DIRECTORY={computer_use_pipe_path or '-'}",
            f"pipe_kind={computer_use_pipe_probe['kind'] or '-'}",
            f"pipe_exists={computer_use_pipe_probe['exists']}",
            f"pipe_accessible={computer_use_pipe_probe['accessible']}",
            f"pipe_contract_matches={computer_use_pipe_contract_matches}",
            f"matching_node_repl_processes={len(computer_use_matching_processes)}",
            f"pipe_runtime_bound={computer_use_pipe_runtime_bound}",
            "verification_scope=static_structure_process_and_pipe_availability",
            "computer_use_protocol_handshake_verified=False",
            *computer_use_runtime_problems,
        ],
        [
            str(paths.config_path),
            str(managed_config_path),
            computer_use_node_repl_command,
            computer_use_node_path_text,
            node_module_scan["firstRoot"],
            computer_use_pipe_path,
        ],
    )
    if not computer_use_runtime_ok:
        add_issue(
            "plugins.computer_use_privileged_runtime_unavailable",
            "critical",
            "plugins",
            localize(language, "Computer Use privileged runtime is unavailable", "Computer Use 特权运行时不可用"),
            localize(
                language,
                "A managed node_repl override or incomplete Desktop runtime can remove nodeRepl.config, nodeRepl.nativePipe and the bundled @oai/sky client even while Browser remains usable.",
                "managed_config 中的 node_repl 覆盖或不完整的 Desktop 运行时，会移除 nodeRepl.config、nodeRepl.nativePipe 和内置 @oai/sky 客户端，即使 Browser 仍可使用。",
            ),
            localize(
                language,
                "Remove the entire managed mcp_servers.node_repl table, preserve the Desktop-generated runtime command and environment, then fully restart Codex Desktop and run a real Computer Use probe.",
                "删除 managed_config.toml 中完整的 mcp_servers.node_repl 表，保留 Desktop 生成的运行时命令和环境；随后完整重启 Codex Desktop，并执行真实 Computer Use 调用验证。",
            ),
            computer_use_runtime_problems,
            [str(paths.config_path), str(managed_config_path)],
        )
    temp_plugin_dirs = [path.name for path in plugin_cache_root.glob("plugin-install-*") if path.is_dir()] if plugin_cache_root.exists() else []
    if temp_plugin_dirs:
        add_issue(
            "plugins.install_temp_dirs",
            "info",
            "plugins",
            localize(language, "Plugin install temp directories remain", "存在插件安装临时目录"),
            localize(language, f"{len(temp_plugin_dirs)} plugin-install-* directories remain in the bundled cache.", f"内置插件缓存里残留 {len(temp_plugin_dirs)} 个 plugin-install-* 临时目录。"),
            localize(language, "They are usually harmless, but repeated growth indicates interrupted plugin installs.", "通常无害，但如果持续增长，说明插件安装过程经常中断。"),
            temp_plugin_dirs[:8],
            [str(plugin_cache_root)],
        )

    env_codex_home = os.environ.get("CODEX_HOME") or ""
    if env_codex_home and Path(env_codex_home).expanduser().resolve(strict=False) != paths.codex_home_path:
        add_issue(
            "environment.codex_home_mismatch",
            "warning",
            "environment",
            localize(language, "CODEX_HOME differs from selected path", "CODEX_HOME 与当前选择路径不一致"),
            localize(language, "The process environment and the UI target path point to different Codex homes.", "当前进程环境变量和 UI 目标路径指向不同的 Codex Home。"),
            localize(language, "Use one canonical Codex Home when running diagnostics and write operations.", "诊断和写入操作应统一使用同一个 Codex Home。"),
            [f"CODEX_HOME={env_codex_home}", f"selected={paths.codex_home_path}"],
            [env_codex_home, str(paths.codex_home_path)],
        )
    user_codex_home = read_windows_user_environment_variable("CODEX_HOME")
    process_env_matches_selected = same_resolved_path(env_codex_home, paths.codex_home_path)
    user_env_matches_selected = same_resolved_path(user_codex_home, paths.codex_home_path)
    user_env_status = "info"
    if os.name == "nt":
        user_env_status = "pass" if user_env_matches_selected or not process_env_matches_selected else "warning"
    add_check(
        "environment.user_codex_home",
        "environment",
        localize(language, "User-level CODEX_HOME", "用户级 CODEX_HOME"),
        user_env_status,
        localize(language, "The user-level CODEX_HOME matches the active Codex Home or is not required for this run.", "用户级 CODEX_HOME 与当前 Codex Home 一致，或本次运行不依赖它。")
        if user_env_status != "warning"
        else localize(language, "The current process uses the selected Codex Home, but the Windows user-level CODEX_HOME does not match.", "当前进程使用了所选 Codex Home，但 Windows 用户级 CODEX_HOME 不一致。"),
        [
            f"process_CODEX_HOME={env_codex_home or '-'}",
            f"user_CODEX_HOME={user_codex_home or '-'}",
            f"selected={paths.codex_home_path}",
            f"process_matches_selected={process_env_matches_selected}",
            f"user_matches_selected={user_env_matches_selected}",
        ],
        [value for value in [env_codex_home, user_codex_home, str(paths.codex_home_path)] if value],
    )
    if user_env_status == "warning":
        add_issue(
            "environment.user_codex_home_mismatch",
            "warning",
            "environment",
            localize(language, "User-level CODEX_HOME will not persist this profile", "用户级 CODEX_HOME 不会持久指向当前配置"),
            localize(language, "After restarting Codex Desktop or Windows, the app may use a different profile than the one this process is diagnosing.", "重启 Codex Desktop 或 Windows 后，应用可能使用与当前诊断不同的配置目录。"),
            localize(language, "Set the Windows user-level CODEX_HOME to the intended Codex Home only after confirming that this is the canonical profile.", "确认当前目录就是规范配置目录后，再把 Windows 用户级 CODEX_HOME 设置为该路径。"),
            [
                f"process_CODEX_HOME={env_codex_home or '-'}",
                f"user_CODEX_HOME={user_codex_home or '-'}",
                f"selected={paths.codex_home_path}",
            ],
            [value for value in [env_codex_home, user_codex_home, str(paths.codex_home_path)] if value],
        )
    runtime_cli_path = extract_config_path(config_text, "CODEX_CLI_PATH")
    configured_cli_path = str(node_repl_contract["effectiveEnv"].get("CODEX_CLI_PATH") or runtime_cli_path)
    runtime_cli_exists = bool(runtime_cli_path) and Path(runtime_cli_path).is_file()
    configured_cli_exists = bool(configured_cli_path) and Path(configured_cli_path).is_file()
    cli_candidates = find_codex_cli_candidates()
    first_cli_inspection = inspect_codex_cli_candidate(cli_candidates[0]) if cli_candidates else {}
    first_cli_differs_from_config = bool(
        configured_cli_path
        and configured_cli_exists
        and cli_candidates
        and not same_resolved_path(cli_candidates[0], Path(configured_cli_path))
    )
    cli_path_shadow_warning = bool(
        first_cli_differs_from_config
        and cli_candidates
        and re.search(r"anaconda|conda", cli_candidates[0], re.IGNORECASE)
        and not first_cli_inspection.get("configForwarder")
    )
    cli_candidate_warning = bool(
        cli_candidates
        and re.search(r"anaconda|conda", cli_candidates[0], re.IGNORECASE)
        and not first_cli_inspection.get("officialNpmWrapper")
        and not first_cli_inspection.get("configForwarder")
    )
    configured_cli_missing_warning = bool(configured_cli_path and not configured_cli_exists)
    cli_warning = configured_cli_missing_warning or cli_candidate_warning or cli_path_shadow_warning
    add_check(
        "environment.codex_cli",
        "environment",
        localize(language, "Codex CLI path", "Codex CLI 路径"),
        "warning" if cli_warning else "pass",
        localize(language, "Codex CLI path can be resolved.", "Codex CLI 路径可解析。")
        if not cli_warning
        else localize(language, "The configured CLI path or first PATH codex command needs attention.", "配置的 CLI 路径或 PATH 中第一个 codex 命令需要关注。"),
        [
            f"runtime_CODEX_CLI_PATH={runtime_cli_path or '-'}",
            f"runtime_CODEX_CLI_PATH_exists={runtime_cli_exists if runtime_cli_path else '-'}",
            f"CODEX_CLI_PATH={configured_cli_path or '-'}",
            f"CODEX_CLI_PATH_exists={configured_cli_exists if configured_cli_path else '-'}",
            f"path_codex={json.dumps(cli_candidates[:5], ensure_ascii=False)}",
            f"first_path_differs_from_config={first_cli_differs_from_config}",
            f"first_official_npm_wrapper={first_cli_inspection.get('officialNpmWrapper', '-')}",
            f"first_config_forwarder={first_cli_inspection.get('configForwarder', '-')}",
            f"first_package_version={first_cli_inspection.get('packageVersion', '-')}",
            f"first_package_path={first_cli_inspection.get('packagePath', '-')}",
        ],
        [configured_cli_path] if configured_cli_path else [],
    )
    if configured_cli_missing_warning:
        add_issue(
            "environment.codex_cli_configured_path_missing",
            "warning",
            "environment",
            localize(language, "Configured Codex CLI path is missing", "配置的 Codex CLI 路径不存在"),
            localize(language, "CODEX_CLI_PATH points to a file that is no longer present on disk.", "CODEX_CLI_PATH 指向了磁盘上已经不存在的文件。"),
            localize(language, "Update CODEX_CLI_PATH to the current Codex Desktop CLI path, then restart Codex Desktop if tool startup still uses the stale path.", "把 CODEX_CLI_PATH 更新到当前 Codex Desktop CLI 路径；如果工具启动仍使用旧路径，再重启 Codex Desktop。"),
            [f"CODEX_CLI_PATH={configured_cli_path}"],
            [configured_cli_path],
        )
    if cli_candidate_warning:
        add_issue(
            "environment.codex_cli_shadowed",
            "warning",
            "environment",
            localize(language, "PATH may resolve the wrong codex command", "PATH 可能优先解析到错误的 codex 命令"),
            localize(language, "The first codex executable on PATH does not look like the Codex Desktop CLI.", "PATH 中优先命中的 codex 不像 Codex Desktop CLI。"),
            localize(language, "Use the configured CODEX_CLI_PATH or adjust PATH order before invoking codex from scripts.", "脚本调用 codex 时使用 CODEX_CLI_PATH，或调整 PATH 顺序。"),
            cli_candidates[:5],
            cli_candidates[:5],
        )
    elif cli_path_shadow_warning:
        add_issue(
            "environment.codex_cli_shadowed",
            "warning",
            "environment",
            localize(language, "PATH resolves a different codex command", "PATH 优先解析到另一个 codex 命令"),
            localize(language, "A configured Codex Desktop CLI exists, but scripts that run bare codex will use the first PATH candidate instead.", "配置的 Codex Desktop CLI 存在，但直接运行裸 codex 的脚本会优先使用 PATH 中的第一个候选。"),
            localize(language, "Use CODEX_CLI_PATH explicitly in scripts or move the Codex Desktop CLI ahead of Conda/Anaconda on PATH.", "脚本中显式使用 CODEX_CLI_PATH，或把 Codex Desktop CLI 放到 Conda/Anaconda 之前。"),
            [
                f"CODEX_CLI_PATH={configured_cli_path}",
                f"first_path_codex={cli_candidates[0] if cli_candidates else '-'}",
                f"first_package_version={first_cli_inspection.get('packageVersion', '-')}",
            ],
            [configured_cli_path, *(cli_candidates[:5])],
        )

    codex_processes = detect_codex_processes()
    add_check(
        "processes.codex",
        "runtime",
        localize(language, "Running Codex processes", "运行中的 Codex 进程"),
        "warning" if codex_processes else "pass",
        localize(language, "Codex-related processes are running.", "检测到 Codex 相关进程正在运行。")
        if codex_processes
        else localize(language, "No Codex-related process was detected.", "未检测到 Codex 相关进程。"),
        [", ".join(f"{item.get('imageName')}:{item.get('pid')}" for item in codex_processes[:10])],
    )
    if codex_processes:
        add_issue(
            "runtime.codex_running",
            "warning",
            "runtime",
            localize(language, "Codex Desktop is running", "Codex Desktop 正在运行"),
            localize(language, "High-risk writes can be overwritten or race with the live app process.", "高风险写入可能被正在运行的 Codex Desktop 覆盖或抢占。"),
            localize(language, "Prefer closing Codex Desktop before state repairs, imports, migration, slimming or restore operations.", "执行状态修复、导入、迁移、瘦身或回滚前，优先关闭 Codex Desktop。"),
            [", ".join(f"{item.get('imageName')}:{item.get('pid')}" for item in codex_processes[:10])],
        )

    mcp_process_status = "warning" if mcp_process_snapshot.get("warning") else (
        "info" if mcp_process_snapshot.get("capacityHigh") else "pass"
    )
    if not mcp_process_snapshot.get("available", True):
        mcp_process_status = "info"
    mcp_process_evidence = [
        f"mcp_processes_total={mcp_process_snapshot.get('mcpProcessCount', '-')}",
        f"mcp_risk_processes={mcp_process_snapshot.get('riskProcessCount', '-')}",
        f"generic_mcp_servers={mcp_process_snapshot.get('genericMcpServerCount', '-')}",
        f"max_generic_mcp_per_parent={mcp_process_snapshot.get('maxGenericMcpPerParent', '-')}",
        f"other_mcp_servers={mcp_process_snapshot.get('otherMcpServerProcessCount', '-')}",
        f"legacy_thread_messenger_processes={mcp_process_snapshot.get('legacyThreadMessengerProcessCount', '-')}",
        f"xcodebuildmcp_processes={mcp_process_snapshot.get('xcodebuildMcpProcessCount', '-')}",
        f"xcodebuildmcp_roots={mcp_process_snapshot.get('xcodebuildMcpRootCount', '-')}",
        f"node_repl_processes={mcp_process_snapshot.get('nodeReplProcessCount', '-')}",
        f"normal_node_repl_processes={mcp_process_snapshot.get('normalNodeReplProcessCount', '-')}",
        f"node_repl_risk_processes={mcp_process_snapshot.get('nodeReplRiskProcessCount', '-')}",
        f"node_repl_without_disable_sandbox={mcp_process_snapshot.get('nodeReplWithoutDisableSandboxCount', '-')}",
        f"node_repl_with_disable_sandbox={mcp_process_snapshot.get('nodeReplWithDisableSandboxCount', '-')}",
        f"extension_host_processes={mcp_process_snapshot.get('extensionHostProcessCount', '-')}",
    ]
    if mcp_process_snapshot.get("error"):
        mcp_process_evidence.append(f"scan_error={mcp_process_snapshot.get('error')}")
    for process in list(mcp_process_snapshot.get("sampleProcesses") or [])[:8]:
        mcp_process_evidence.append(
            f"pid={process.get('pid')} parent={process.get('parentPid')} name={process.get('name')} command={process.get('commandLine')}"
        )
    add_check(
        "processes.mcp_child_processes",
        "runtime",
        localize(language, "MCP child process fanout", "MCP 子进程数量"),
        mcp_process_status,
        localize(language, "MCP child process count is within the expected range.", "MCP 子进程数量在预期范围内。")
        if mcp_process_status == "pass"
        else (
            localize(
                language,
                "MCP capacity is elevated but no risky process signature was found; use the trend instead of a one-time total.",
                "MCP 容量较高，但未发现危险进程特征；应结合趋势判断，不按单次总数报故障。",
            )
            if mcp_process_status == "info"
            else localize(language, "Risky MCP child processes were detected or the scan was incomplete.", "检测到危险 MCP 子进程，或当前无法完整检查。")
        ),
        mcp_process_evidence,
    )
    if mcp_process_snapshot.get("warning"):
        add_issue(
            "processes.mcp_child_process_fanout",
            "warning",
            "runtime",
            localize(language, "Risky MCP child processes detected", "检测到危险 MCP 子进程"),
            localize(
                language,
                "Legacy fallback, incompatible xcodebuild, or unsafe node_repl process signatures can interfere with official tool discovery and plugin availability.",
                "legacy fallback、不兼容的 xcodebuild 或危险 node_repl 进程特征可能干扰官方工具发现与插件可用性。",
            ),
            localize(
                language,
                "Inspect the exact risky category and its configured source first. Do not restart or terminate Codex solely because the total process count is high.",
                "先核对具体危险类别及其配置来源；不要仅因进程总数较高就重启或结束 Codex。",
            ),
            mcp_process_evidence,
            [],
        )

    legacy_thread_messenger_blocks = active_toml_table_block_ranges(config_text, "mcp_servers.codex_thread_messenger")
    managed_legacy_thread_messenger_blocks = active_toml_table_block_ranges(
        managed_config_text, "mcp_servers.codex_thread_messenger"
    )
    legacy_thread_messenger_process_count = int(mcp_process_snapshot.get("legacyThreadMessengerProcessCount") or 0)
    legacy_thread_messenger_status = (
        "warning"
        if legacy_thread_messenger_blocks or managed_legacy_thread_messenger_blocks or legacy_thread_messenger_process_count
        else "pass"
    )
    legacy_thread_messenger_evidence = [
        f"active_config_blocks={len(legacy_thread_messenger_blocks)}",
        f"active_managed_config_blocks={len(managed_legacy_thread_messenger_blocks)}",
        f"running_processes={legacy_thread_messenger_process_count}",
        *[
            f"active_block_lines={block.get('startLine')}-{block.get('endLine')}"
            for block in legacy_thread_messenger_blocks[:4]
        ],
        *[
            f"active_managed_block_lines={block.get('startLine')}-{block.get('endLine')}"
            for block in managed_legacy_thread_messenger_blocks[:4]
        ],
    ]
    add_check(
        "plugins.official_thread_tools_exposure",
        "plugins",
        localize(language, "Official thread tool exposure", "官方线程工具暴露"),
        legacy_thread_messenger_status,
        localize(language, "No legacy thread messenger fallback is active.", "未检测到生效的旧线程 messenger fallback。")
        if legacy_thread_messenger_status == "pass"
        else localize(language, "A legacy thread messenger fallback is active or still running and can hide official codex_app thread tools.", "检测到旧线程 messenger fallback 仍在配置中生效或进程仍在运行，可能遮蔽官方 codex_app 线程工具。"),
        legacy_thread_messenger_evidence,
        [str(paths.config_path), str(managed_config_path)],
    )
    if legacy_thread_messenger_status != "pass":
        add_issue(
            "plugins.official_thread_tools_shadowed_by_fallback_mcp",
            "warning",
            "plugins",
            localize(language, "Official thread tools can be shadowed", "官方线程工具可能被 fallback 遮蔽"),
            localize(
                language,
                "The legacy codex_thread_messenger MCP fallback is not equivalent to official codex_app.send_message_to_thread. It can make some conversations expose fallback tools while official list_threads/read_thread/send_message_to_thread are missing.",
                "旧的 codex_thread_messenger MCP fallback 不等价于官方 codex_app.send_message_to_thread。它会让部分对话只暴露 fallback 工具，而缺少官方 list_threads/read_thread/send_message_to_thread。",
            ),
            localize(
                language,
                "Use Codex Home Manager's official-thread-tools repair preview/write API or MCP tools, then fully quit and reopen Codex Desktop. Success still requires visible target-thread delivery and target-side official tool exposure verification.",
                "使用 Codex Home Manager 的官方线程工具修复预览/写入 API 或 MCP 工具处理，然后完全退出并重新打开 Codex Desktop。成功仍必须以目标线程可见收到消息、且目标线程自身能看到官方工具为准。",
            ),
            legacy_thread_messenger_evidence,
            [str(paths.config_path)],
        )

    ios_plugin_name = "build-ios-apps@openai-curated"
    macos_plugin_name = "build-macos-apps@openai-curated"
    runtime_curated_plugins = set(configured_plugins(config_text, "openai-curated"))
    managed_curated_plugins = set(configured_plugins(managed_config_text, "openai-curated"))
    ios_configured_in_runtime = "build-ios-apps" in runtime_curated_plugins
    ios_configured_in_managed = "build-ios-apps" in managed_curated_plugins
    macos_configured_in_runtime = "build-macos-apps" in runtime_curated_plugins
    macos_configured_in_managed = "build-macos-apps" in managed_curated_plugins
    ios_enabled_in_config = config_plugin_enabled(config_text, "build-ios-apps", "openai-curated")
    ios_enabled_in_managed = config_plugin_enabled(managed_config_text, "build-ios-apps", "openai-curated")
    macos_enabled_in_config = config_plugin_enabled(config_text, "build-macos-apps", "openai-curated")
    macos_enabled_in_managed = config_plugin_enabled(managed_config_text, "build-macos-apps", "openai-curated")
    ios_disabled_in_config = config_plugin_disabled(config_text, "build-ios-apps", "openai-curated")
    ios_disabled_in_managed = config_plugin_disabled(managed_config_text, "build-ios-apps", "openai-curated")
    macos_disabled_in_config = config_plugin_disabled(config_text, "build-macos-apps", "openai-curated")
    macos_disabled_in_managed = config_plugin_disabled(managed_config_text, "build-macos-apps", "openai-curated")
    ios_remote_mcp_cached = remote_plugin_mcp_cache_present(paths.codex_home_path, "build-ios-apps")
    macos_remote_mcp_cached = remote_plugin_mcp_cache_present(paths.codex_home_path, "build-macos-apps")
    ios_remote_install_marker = remote_plugin_install_marker_present(paths.codex_home_path, "build-ios-apps")
    macos_remote_install_marker = remote_plugin_install_marker_present(paths.codex_home_path, "build-macos-apps")
    curated_registry = scan_curated_plugin_registry(
        paths.codex_home_path,
        config_text,
        managed_config_text,
    )
    installed_curated_plugins = {
        str(plugin_id).casefold() for plugin_id in curated_registry.get("installedPluginIds") or []
    }
    ios_installed = ios_plugin_name.casefold() in installed_curated_plugins
    macos_installed = macos_plugin_name.casefold() in installed_curated_plugins
    xcodebuild_process_count = int(mcp_process_snapshot.get("xcodebuildMcpProcessCount") or 0)
    apple_plugin_enabled = any(
        (
            ios_configured_in_runtime and ios_enabled_in_config,
            ios_configured_in_managed and ios_enabled_in_managed,
            macos_configured_in_runtime and macos_enabled_in_config,
            macos_configured_in_managed and macos_enabled_in_managed,
        )
    )
    cached_remote_plugin_not_disabled = (
        ios_remote_mcp_cached and not (ios_disabled_in_config and ios_disabled_in_managed)
    ) or (
        macos_remote_mcp_cached and not (macos_disabled_in_config and macos_disabled_in_managed)
    )
    apple_plugin_problem = (
        apple_plugin_enabled
        or ios_installed
        or macos_installed
        or xcodebuild_process_count
        or cached_remote_plugin_not_disabled
        or ios_remote_install_marker
        or macos_remote_install_marker
    )
    apple_plugin_scan_incomplete = not bool(curated_registry.get("available")) or not bool(
        mcp_process_snapshot.get("available")
    )
    macos_plugin_status = "critical" if apple_plugin_problem else "warning" if apple_plugin_scan_incomplete else "pass"
    macos_plugin_evidence = [
        f"registry_available={curated_registry.get('available', False)}",
        f"registry_cli={curated_registry.get('cliPath') or '-'}",
        f"registry_error={curated_registry.get('error') or '-'}",
        f"{ios_plugin_name}:installed={ios_installed}",
        f"{ios_plugin_name}:config_present={ios_configured_in_runtime}",
        f"{ios_plugin_name}:managed_config_present={ios_configured_in_managed}",
        f"{ios_plugin_name}:config_enabled={ios_enabled_in_config}",
        f"{ios_plugin_name}:managed_config_enabled={ios_enabled_in_managed}",
        f"{ios_plugin_name}:config_disabled={ios_disabled_in_config}",
        f"{ios_plugin_name}:managed_config_disabled={ios_disabled_in_managed}",
        f"{ios_plugin_name}:remote_mcp_cached={ios_remote_mcp_cached}",
        f"{ios_plugin_name}:remote_install_marker={ios_remote_install_marker}",
        f"{macos_plugin_name}:installed={macos_installed}",
        f"{macos_plugin_name}:config_present={macos_configured_in_runtime}",
        f"{macos_plugin_name}:managed_config_present={macos_configured_in_managed}",
        f"{macos_plugin_name}:config_enabled={macos_enabled_in_config}",
        f"{macos_plugin_name}:managed_config_enabled={macos_enabled_in_managed}",
        f"{macos_plugin_name}:config_disabled={macos_disabled_in_config}",
        f"{macos_plugin_name}:managed_config_disabled={macos_disabled_in_managed}",
        f"{macos_plugin_name}:remote_mcp_cached={macos_remote_mcp_cached}",
        f"{macos_plugin_name}:remote_install_marker={macos_remote_install_marker}",
        f"xcodebuildmcp_processes={xcodebuild_process_count}",
    ]
    add_check(
        "plugins.macos_plugin_disabled_on_windows",
        "plugins",
        localize(language, "Apple build plugins disabled on Windows", "Windows 已禁用 Apple 构建插件"),
        macos_plugin_status,
        localize(language, "The iOS and macOS build plugins are absent from the official registry and no account-synced remote install marker remains.", "iOS 和 macOS 构建插件未安装，也没有残留账号同步的远程安装标记。")
        if macos_plugin_status == "pass"
        else localize(language, "An Apple build plugin remains active, or the registry/process scan could not prove that it is absent.", "Windows 上仍有 Apple 构建插件处于活动状态，或注册表/进程扫描无法证明它已完全移除。"),
        macos_plugin_evidence,
        [str(paths.config_path), str(managed_config_path)],
    )
    if macos_plugin_status != "pass":
        add_issue(
            "plugins.macos_plugin_active_on_windows",
            "critical" if macos_plugin_status == "critical" else "warning",
            "plugins",
            localize(language, "Apple build plugin is active or could not be fully verified", "Apple 构建插件仍活动或无法完整核验"),
            localize(language, "The iOS or macOS plugin can repeatedly spawn xcodebuildmcp child processes and destabilize Codex Desktop. A failed registry or process scan must not be treated as a clean result.", "iOS 或 macOS 插件会反复拉起 xcodebuildmcp 子进程并影响 Codex Desktop 稳定性。注册表或进程扫描失败时也不能按正常结果处理。"),
            localize(language, "Uninstall both plugins from the signed-in Codex Plugins UI so account sync removes the remote install marker; keep enabled=false locally, then stop their process trees while Codex is offline and restart Codex.", "在已登录的 Codex 插件页面卸载这两个插件，让账号同步移除远程安装标记；本地继续保持 enabled=false，然后在 Codex 离线时停止对应进程树并重启。"),
            macos_plugin_evidence,
            [str(paths.config_path), str(managed_config_path)],
        )

    node_repl_running_with_disable_sandbox = int(mcp_process_snapshot.get("nodeReplWithDisableSandboxCount") or 0)
    node_repl_args = node_repl_contract["effectiveArgs"]
    node_repl_effective_command = str(node_repl_contract.get("effectiveCommand") or "")
    node_repl_command_mismatches = node_repl_process_command_mismatches(
        mcp_process_snapshot,
        node_repl_effective_command,
    )
    node_repl_config_has_disable_sandbox = "--disable-sandbox" in node_repl_args
    node_repl_privileged_mode_problem = (
        node_repl_config_has_disable_sandbox
        or node_repl_running_with_disable_sandbox > 0
        or bool(node_repl_command_mismatches)
    )
    node_repl_layer_conflicts = node_repl_contract["conflicts"]
    add_check(
        "plugins.node_repl_config_layer_consistency",
        "plugins",
        localize(language, "Node REPL effective configuration layers", "Node REPL 有效配置层"),
        "warning" if node_repl_layer_conflicts else "pass",
        localize(language, "Managed Node REPL policy does not override Desktop-owned dynamic runtime fields.", "Node REPL 管理策略未覆盖 Desktop 所有的动态运行时字段。")
        if not node_repl_layer_conflicts
        else localize(language, "Managed Node REPL values override Desktop-owned dynamic runtime fields.", "Node REPL 管理层覆盖了 Desktop 所有的动态运行时字段。"),
        [
            f"effective_args={json.dumps(node_repl_args, ensure_ascii=False)}",
            f"runtime_env_keys={len(node_repl_contract['runtime']['env'])}",
            f"managed_env_keys={len(node_repl_contract['managed']['env'])}",
            *node_repl_layer_conflicts,
        ],
        [str(paths.config_path), str(managed_config_path)],
    )
    if node_repl_layer_conflicts:
        add_issue(
            "plugins.node_repl_config_layer_conflict",
            "warning",
            "plugins",
            localize(language, "Node REPL configuration layers conflict", "Node REPL 配置层冲突"),
            localize(language, "managed_config.toml pins dynamic node_repl command or environment values that Codex Desktop regenerates on startup.", "managed_config.toml 固定了 Codex Desktop 启动时会重新生成的 node_repl 命令或环境变量。"),
            localize(language, "Remove the entire managed mcp_servers.node_repl table; let Desktop own the command, arguments and environment, then fully restart.", "删除 managed_config.toml 中完整的 mcp_servers.node_repl 表；命令、参数和环境全部交给 Desktop 管理，然后完整重启。"),
            node_repl_layer_conflicts,
            [str(paths.config_path), str(managed_config_path)],
        )
    add_check(
        "plugins.node_repl_desktop_privileged_mode",
        "plugins",
        localize(language, "Node REPL Desktop privileged mode", "Node REPL Desktop 特权模式"),
        "warning" if node_repl_privileged_mode_problem else "pass",
        localize(language, "Node REPL uses the Desktop-owned privileged runtime mode.", "Node REPL 正在使用 Desktop 管理的特权运行时模式。")
        if not node_repl_privileged_mode_problem
        else localize(language, "Node REPL is running with a stale direct-runtime override that can disable Computer Use privileges.", "Node REPL 正在使用陈旧的直接运行时覆盖，可能导致 Computer Use 特权能力失效。"),
        [
            f"configured_args={json.dumps(node_repl_args, ensure_ascii=False)}",
            f"effective_command={node_repl_effective_command!r}",
            f"config_has_disable_sandbox={node_repl_config_has_disable_sandbox}",
            f"running_node_repl_with_disable_sandbox={node_repl_running_with_disable_sandbox}",
            *node_repl_command_mismatches,
        ],
        [str(paths.config_path), str(managed_config_path)],
    )
    if node_repl_privileged_mode_problem:
        add_issue(
            "plugins.node_repl_desktop_privileged_mode_unavailable",
            "warning",
            "plugins",
            localize(language, "Node REPL privileged mode is shadowed", "Node REPL 特权模式被覆盖"),
            localize(
                language,
                "The current Desktop runtime injects native-pipe privileges into Node REPL. Forcing --disable-sandbox through managed configuration bypasses that runtime and can leave Browser working while Computer Use fails.",
                "当前 Desktop 运行时会向 Node REPL 注入原生管道特权。通过 managed_config 强制 --disable-sandbox 会绕过该运行时，造成 Browser 可用但 Computer Use 失败。",
            ),
            localize(
                language,
                "Remove managed mcp_servers.node_repl overrides and fully restart Codex Desktop so Node REPL is relaunched from the Desktop-generated runtime contract.",
                "删除 managed_config.toml 中的 mcp_servers.node_repl 覆盖，并完整重启 Codex Desktop，让 Node REPL 按 Desktop 生成的运行时契约重新启动。",
            ),
            [
                f"configured_args={json.dumps(node_repl_args, ensure_ascii=False)}",
                f"effective_command={node_repl_effective_command!r}",
                f"config_has_disable_sandbox={node_repl_config_has_disable_sandbox}",
                f"running_node_repl_with_disable_sandbox={node_repl_running_with_disable_sandbox}",
                *node_repl_command_mismatches,
            ],
            [str(paths.config_path), str(managed_config_path)],
        )
    if node_repl_command_mismatches:
        add_issue(
            "plugins.node_repl_process_command_mismatch",
            "warning",
            "plugins",
            localize(language, "Node REPL is running from a stale executable", "Node REPL 正在使用旧可执行文件"),
            localize(
                language,
                "The running node_repl process command line does not contain the effective executable configured by Codex.",
                "当前 node_repl 进程命令行不包含 Codex 实际生效配置指定的可执行文件。",
            ),
            localize(
                language,
                "Fully exit Codex Desktop and its extension hosts, then restart so node_repl is launched from the effective command.",
                "完全退出 Codex Desktop 及其 extension host 后重新启动，让 node_repl 从实际生效命令重新拉起。",
            ),
            node_repl_command_mismatches,
            [str(paths.config_path), str(managed_config_path)],
        )

    backup_stats = directory_size(backup_root_path())
    capacity_trend = record_capacity_trend(
        paths.codex_home_path,
        {
            "sessionsBytes": total_storage_bytes,
            "largeThreadCount": doctor_slow_thread_count,
            "backupBytes": int(backup_stats["sizeBytes"]),
            "backupFileCount": int(backup_stats["fileCount"]),
            "backupScanTruncated": bool(backup_stats["truncated"]),
            "mcpProcessCount": int(mcp_process_snapshot.get("mcpProcessCount") or 0),
            "normalNodeReplProcessCount": int(mcp_process_snapshot.get("normalNodeReplProcessCount") or 0),
            "nodeReplRiskProcessCount": int(mcp_process_snapshot.get("nodeReplRiskProcessCount") or 0),
            "legacyFallbackProcessCount": int(mcp_process_snapshot.get("legacyThreadMessengerProcessCount") or 0),
            "xcodebuildProcessCount": int(mcp_process_snapshot.get("xcodebuildMcpProcessCount") or 0),
            "otherMcpServerProcessCount": int(mcp_process_snapshot.get("otherMcpServerProcessCount") or 0),
        },
    )
    add_check(
        "storage.backups",
        "storage",
        localize(language, "Manager backup store", "管理器备份区"),
        "warning" if int(backup_stats["sizeBytes"]) >= 1024 * 1024 * 1024 else "pass",
        localize(language, "Backup store size was measured.", "已统计备份区体量。"),
        [
            f"path={backup_root_path()}",
            f"size_bytes={backup_stats['sizeBytes']}",
            f"file_count={backup_stats['fileCount']}",
            f"truncated={backup_stats['truncated']}",
        ],
        [str(backup_root_path())],
    )
    if int(backup_stats["sizeBytes"]) >= 1024 * 1024 * 1024:
        add_issue(
            "storage.backups_large",
            "warning",
            "storage",
            localize(language, "Backup store is large", "备份区体量较大"),
            localize(language, f"Manager backups total {backup_stats['sizeBytes']} bytes.", f"管理器备份合计 {backup_stats['sizeBytes']} 字节。"),
            localize(language, "Review old backup manifests before moving anything to the recycle bin.", "先核对旧备份 manifest，再决定是否放入回收站清理。"),
            [f"file_count={backup_stats['fileCount']}"],
            [str(backup_root_path())],
        )

    agents_path = paths.codex_home_path / "AGENTS.md"
    memory_summary_path = paths.codex_home_path / "memories" / "memory_summary.md"
    add_check(
        "resources.guidance",
        "resources",
        localize(language, "Instructions and memory", "指令与记忆资源"),
        "pass" if agents_path.exists() or memory_summary_path.exists() else "info",
        localize(language, "Instruction or memory files are available.", "存在可用的指令或记忆文件。")
        if agents_path.exists() or memory_summary_path.exists()
        else localize(language, "No root AGENTS.md or memory summary was found.", "未找到根 AGENTS.md 或 memory summary。"),
        [path_exists_text(agents_path), path_exists_text(memory_summary_path)],
        [str(agents_path), str(memory_summary_path)],
    )

    issue_counts = Counter(str(issue["severity"]) for issue in issues)
    check_counts = Counter(str(check["status"]) for check in checks)
    score = max(0, 100 - issue_counts["critical"] * 25 - issue_counts["warning"] * 8 - issue_counts["info"])
    overall_status = "critical" if issue_counts["critical"] else "warning" if issue_counts["warning"] else "pass"
    issues.sort(key=lambda item: (severity_rank.get(str(item["severity"]), 99), str(item["category"]), str(item["id"])))
    checks.sort(key=lambda item: (severity_rank.get(str(item["status"]), 99), str(item["category"]), str(item["id"])))
    top_recommendations = []
    for issue in issues:
        recommendation = str(issue.get("recommendation") or "")
        if recommendation and recommendation not in top_recommendations:
            top_recommendations.append(recommendation)
        if len(top_recommendations) >= 8:
            break

    report = {
        "codexHome": str(paths.codex_home_path),
        "generatedAtMs": int(time.time() * 1000),
        "score": score,
        "status": overall_status,
        "summary": {
            "critical": issue_counts["critical"],
            "warning": issue_counts["warning"],
            "info": issue_counts["info"],
            "pass": check_counts["pass"],
            "checks": len(checks),
            "issues": len(issues),
            "threadCount": thread_count,
        },
        "paths": {
            "codexHome": str(paths.codex_home_path),
            "database": str(paths.database_path),
            "globalState": str(paths.global_state_path),
            "sessionIndex": str(paths.session_index_path),
            "config": str(paths.config_path),
            "managedConfig": str(managed_config_path),
            "logs": str(log_database_path),
            "pluginCache": str(plugin_cache_root),
            "backupRoot": str(backup_root_path()),
        },
        "codexProcesses": codex_processes,
        "capacityTrend": capacity_trend,
        "checks": checks,
        "issues": issues,
        "topRecommendations": top_recommendations,
        "repairHints": {
            "bundledPlugins": localize(
                language,
                "Reinstall browser, sites, chrome and computer-use through the Codex bundled plugin install flow, then rerun diagnostics after restart.",
                "通过 Codex 内置插件安装流程重装 browser、sites、chrome 和 computer-use，重启后重新体检。",
            ),
            "stateWrites": localize(
                language,
                "Close Codex Desktop before high-risk state writes whenever possible.",
                "高风险状态写入前尽量先关闭 Codex Desktop。",
            ),
        },
    }
    report["repairPrompt"] = build_codex_repair_prompt(report, language)
    return report
