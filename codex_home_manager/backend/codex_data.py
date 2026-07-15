from __future__ import annotations

import datetime as datetime_module
import copy
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .hidden_process import run_hidden_command
from .process_utils import list_windows_processes


@dataclass(frozen=True)
class CodexPaths:
    codex_home_path: Path
    database_path: Path
    global_state_path: Path
    global_state_backup_path: Path
    session_index_path: Path
    version_path: Path
    config_path: Path


thread_columns = [
    "id",
    "rollout_path",
    "created_at",
    "updated_at",
    "source",
    "model_provider",
    "cwd",
    "title",
    "sandbox_policy",
    "approval_mode",
    "tokens_used",
    "has_user_event",
    "archived",
    "archived_at",
    "git_sha",
    "git_branch",
    "git_origin_url",
    "cli_version",
    "first_user_message",
    "agent_nickname",
    "agent_role",
    "memory_mode",
    "model",
    "reasoning_effort",
    "agent_path",
    "created_at_ms",
    "updated_at_ms",
    "thread_source",
    "preview",
]

manager_hidden_thread_ids_key = "codex_home_manager-hidden-thread-ids"
manager_hidden_thread_ids_updated_at_key = f"{manager_hidden_thread_ids_key}-updated-at"
legacy_manager_hidden_thread_ids_key = "-".join(["codex", "thread", "manager", "hidden", "thread", "ids"])
legacy_manager_hidden_thread_ids_updated_at_key = f"{legacy_manager_hidden_thread_ids_key}-updated-at"


def strip_extended_prefix(path_text: str | None) -> str:
    if not path_text:
        return ""
    if path_text.startswith("\\\\?\\"):
        return path_text[4:]
    return path_text


def normalize_path_text(path_text: str | None) -> str:
    cleaned_path_text = strip_extended_prefix(path_text)
    if not cleaned_path_text:
        return ""
    return os.path.normpath(cleaned_path_text)


def replace_file_with_retry(temp_path: Path, target_path: Path, *, attempts: int = 8) -> None:
    last_error: OSError | None = None
    for attempt_index in range(attempts):
        try:
            os.replace(temp_path, target_path)
            return
        except PermissionError as error:
            last_error = error
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32}:
                raise
            last_error = error
        time.sleep(min(0.05 * (2 ** attempt_index), 0.5))
    if last_error:
        raise last_error
    os.replace(temp_path, target_path)


def comparable_path_text(path_text: str | None) -> str:
    return os.path.normcase(normalize_path_text(path_text)).rstrip("\\/")


def path_label(path_text: str | None) -> str:
    normalized_path = normalize_path_text(path_text)
    if not normalized_path:
        return "No project"
    return Path(normalized_path).name or normalized_path


def default_codex_home_path() -> Path:
    configured_path = os.environ.get("CODEX_HOME")
    if configured_path:
        return Path(configured_path).expanduser().resolve(strict=False)
    home_candidate = Path.home() / ".codex"
    candidates = [home_candidate]
    if os.name == "nt":
        candidates.extend(Path(f"{drive}:/.codex") for drive in ("D", "C"))

    normalized_candidates: list[Path] = []
    seen_candidates: set[str] = set()
    for candidate in candidates:
        resolved_candidate = candidate.expanduser().resolve(strict=False)
        candidate_key = os.path.normcase(str(resolved_candidate))
        if candidate_key in seen_candidates:
            continue
        seen_candidates.add(candidate_key)
        normalized_candidates.append(resolved_candidate)

    for candidate in normalized_candidates:
        if (candidate / "state_5.sqlite").exists():
            return candidate
    for candidate in normalized_candidates:
        if candidate.exists():
            return candidate
    return home_candidate.expanduser().resolve(strict=False)


def is_real_codex_home_path(path: Path) -> bool:
    if not path.is_dir():
        return False
    database_path = path / "state_5.sqlite"
    if database_path.is_file():
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=2)
            thread_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(threads)").fetchall()
                if len(row) > 1
            }
            return {"id", "rollout_path", "cwd"}.issubset(thread_columns)
        except (OSError, sqlite3.Error, ValueError):
            return False
        finally:
            if connection is not None:
                connection.close()

    return (
        (path / "config.toml").is_file()
        and (path / "sessions").is_dir()
        and ((path / "session_index.jsonl").is_file() or (path / ".codex-global-state.json").is_file())
    )


def resolve_codex_paths(codex_home_text: str | None = None) -> CodexPaths:
    codex_home_path = Path(codex_home_text).expanduser() if codex_home_text else default_codex_home_path()
    codex_home_path = codex_home_path.resolve(strict=False)
    if codex_home_text and not is_real_codex_home_path(codex_home_path):
        raise ValueError(f"codex_home must identify an existing Codex Home: {codex_home_path}")
    return CodexPaths(
        codex_home_path=codex_home_path,
        database_path=codex_home_path / "state_5.sqlite",
        global_state_path=codex_home_path / ".codex-global-state.json",
        global_state_backup_path=codex_home_path / ".codex-global-state.json.bak",
        session_index_path=codex_home_path / "session_index.jsonl",
        version_path=codex_home_path / "version.json",
        config_path=codex_home_path / "config.toml",
    )


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_global_state(paths: CodexPaths) -> dict[str, Any]:
    return read_json_file(paths.global_state_path)


def read_version(paths: CodexPaths) -> dict[str, Any]:
    return read_json_file(paths.version_path)


def active_toml_table_block_ranges(text: str, table_name: str) -> list[dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    header_pattern = re.compile(rf"^\s*\[{re.escape(table_name)}\]\s*(?:#.*)?$")
    any_header_pattern = re.compile(r"^\s*\[[^\]]+\]\s*(?:#.*)?$")
    ranges: list[dict[str, Any]] = []
    offset = 0
    line_offsets: list[int] = []
    for line in lines:
        line_offsets.append(offset)
        offset += len(line)
    for line_index, line in enumerate(lines):
        stripped_line = line.lstrip()
        if stripped_line.startswith("#"):
            continue
        if not header_pattern.match(line):
            continue
        end_index = len(lines)
        for next_index in range(line_index + 1, len(lines)):
            next_line = lines[next_index]
            if next_line.lstrip().startswith("#"):
                continue
            if any_header_pattern.match(next_line):
                end_index = next_index
                break
        start_offset = line_offsets[line_index]
        end_offset = line_offsets[end_index] if end_index < len(line_offsets) else len(text)
        ranges.append(
            {
                "startLine": line_index + 1,
                "endLine": end_index,
                "startOffset": start_offset,
                "endOffset": end_offset,
                "text": text[start_offset:end_offset],
            }
        )
    return ranges


def extract_toml_string_value(text: str, key: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1) if match else ""


def inspect_codex_cli_metadata(cli_path_text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": cli_path_text,
        "exists": False,
        "version": "",
        "source": "",
        "error": "",
    }
    if not cli_path_text:
        return result
    cli_path = Path(cli_path_text).expanduser()
    result["exists"] = cli_path.exists()
    if not result["exists"]:
        return result
    official_npm_wrapper = False
    if cli_path.suffix.lower() in {"", ".cmd", ".bat", ".ps1", ".sh"}:
        try:
            wrapper_text = cli_path.read_text(encoding="utf-8", errors="replace")[:5000]
            official_npm_wrapper = "node_modules/@openai/codex/bin/codex.js" in wrapper_text.replace("\\", "/").lower()
        except OSError:
            official_npm_wrapper = False
    package_path = cli_path.parent / "node_modules" / "@openai" / "codex" / "package.json"
    if official_npm_wrapper and package_path.exists():
        try:
            package_data = json.loads(package_path.read_text(encoding="utf-8", errors="replace"))
            if package_data.get("name") == "@openai/codex":
                result["version"] = str(package_data.get("version") or "")
                result["source"] = str(package_path)
        except (OSError, json.JSONDecodeError) as error:
            result["error"] = str(error)
    if not result["version"]:
        command_result = run_hidden_command([str(cli_path), "--version"], timeout_seconds=8)
        version_text = f"{command_result.get('stdout', '')}\n{command_result.get('stderr', '')}".strip()
        version_match = re.search(r"\bcodex(?:-cli)?\s+([^\s]+)", version_text, re.IGNORECASE)
        if version_match:
            result["version"] = version_match.group(1)
            result["source"] = "command"
        else:
            result["source"] = "file"
            result["error"] = str(command_result.get("error") or "")
    return result


def desktop_version_from_text(text: str) -> str:
    match = re.search(r"OpenAI\.Codex_([^\\/]+?)_x64__", text)
    return match.group(1) if match else ""


def desktop_install_from_resources_path(path_text: str) -> dict[str, Any]:
    if not path_text:
        return {}
    version = desktop_version_from_text(path_text)
    if not version:
        return {}
    install_root = re.split(r"\\app\\resources|/app/resources", path_text, maxsplit=1)[0]
    return {
        "version": version,
        "path": install_root,
        "modifiedAtMs": int(Path(install_root).stat().st_mtime * 1000) if Path(install_root).exists() else 0,
    }


def read_native_host_desktop_install(paths: CodexPaths) -> dict[str, Any]:
    for file_name in ("chrome-native-hosts-v2.json", "chrome-native-hosts.json"):
        native_host_path = paths.codex_home_path / file_name
        try:
            data = json.loads(native_host_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        candidate_paths: list[str] = []
        if isinstance(data, dict):
            if isinstance(data.get("entries"), list):
                for entry in data["entries"]:
                    if isinstance(entry, dict) and isinstance(entry.get("paths"), dict):
                        candidate_paths.append(str(entry["paths"].get("resourcesPath") or ""))
            if isinstance(data.get("chromeNativeHosts"), list):
                for entry in data["chromeNativeHosts"]:
                    if isinstance(entry, dict):
                        candidate_paths.append(str(entry.get("resourcesPath") or ""))
        for candidate_path in candidate_paths:
            install = desktop_install_from_resources_path(candidate_path)
            if install:
                return install
    return {}


def read_healthy_native_host_cli_path(paths: CodexPaths) -> str:
    native_host_path = paths.codex_home_path / "chrome-native-hosts-v2.json"
    try:
        data = json.loads(native_host_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ""
    entries = data.get("entries") if isinstance(data, dict) else []
    required_keys = (
        "codexCliPath",
        "browserClientPath",
        "extensionHostPath",
        "resourcesPath",
        "nodePath",
        "nodeReplPath",
    )
    for entry in entries if isinstance(entries, list) else []:
        path_values = entry.get("paths") if isinstance(entry, dict) else None
        if not isinstance(path_values, dict):
            continue
        required_paths = [str(path_values.get(key) or "") for key in required_keys]
        if all(path_text and Path(path_text).exists() for path_text in required_paths):
            return required_paths[0]
    return ""


def find_codex_desktop_installations() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    windows_apps_path = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    installations: list[dict[str, Any]] = []
    try:
        candidates = list(windows_apps_path.glob("OpenAI.Codex_*"))
    except OSError:
        return installations
    for candidate in candidates:
        match = re.search(r"OpenAI\.Codex_([^_]+)", candidate.name)
        if not match:
            continue
        try:
            modified_at_ms = int(candidate.stat().st_mtime * 1000)
        except OSError:
            modified_at_ms = 0
        installations.append(
            {
                "version": match.group(1),
                "path": str(candidate),
                "modifiedAtMs": modified_at_ms,
            }
        )
    return sorted(installations, key=lambda item: (item["modifiedAtMs"], item["version"]), reverse=True)


def detect_current_codex_versions(paths: CodexPaths) -> dict[str, Any]:
    config_text = ""
    try:
        config_text = paths.config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        config_text = ""
    managed_config_text = ""
    try:
        managed_config_text = (paths.codex_home_path / "managed_config.toml").read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        managed_config_text = ""
    runtime_cli_path = extract_toml_string_value(config_text, "CODEX_CLI_PATH")
    configured_cli_path = extract_toml_string_value(managed_config_text, "CODEX_CLI_PATH")
    if not configured_cli_path or not Path(configured_cli_path).is_file():
        configured_cli_path = runtime_cli_path
    if not configured_cli_path or not Path(configured_cli_path).is_file():
        configured_cli_path = read_healthy_native_host_cli_path(paths)
    if not configured_cli_path:
        plugin_appserver_cli_path = paths.codex_home_path / "plugins" / ".plugin-appserver" / (
            "codex.exe" if os.name == "nt" else "codex"
        )
        if plugin_appserver_cli_path.is_file():
            configured_cli_path = str(plugin_appserver_cli_path)
    path_cli_path = shutil.which("codex") or ""
    configured_cli = inspect_codex_cli_metadata(configured_cli_path)
    path_cli = inspect_codex_cli_metadata(path_cli_path)
    desktop_installations = find_codex_desktop_installations()
    native_host_desktop_install = read_native_host_desktop_install(paths)
    version_cache = read_version(paths)
    return {
        "configuredCli": configured_cli,
        "runtimeConfiguredCli": inspect_codex_cli_metadata(runtime_cli_path),
        "pathCli": path_cli,
        "desktopInstall": desktop_installations[0] if desktop_installations else native_host_desktop_install,
        "desktopInstallations": desktop_installations[:5],
        "versionCache": version_cache,
    }


def connect_database(database_path: Path, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{database_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=15)
    else:
        connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def checkpoint_database(database_path: Path) -> None:
    with connect_database(database_path, readonly=False) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def fetch_thread_rows(paths: CodexPaths) -> list[dict[str, Any]]:
    if not paths.database_path.exists():
        raise FileNotFoundError(f"state database not found: {paths.database_path}")
    with connect_database(paths.database_path, readonly=True) as connection:
        rows = connection.execute(
            "SELECT * FROM threads ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC, id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def fetch_thread_spawn_edges(paths: CodexPaths) -> dict[str, dict[str, str]]:
    if not paths.database_path.exists():
        return {}
    with connect_database(paths.database_path, readonly=True) as connection:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'thread_spawn_edges'"
        ).fetchone()
        if not table_exists:
            return {}
        rows = connection.execute(
            "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges"
        ).fetchall()
    return {
        str(row["child_thread_id"]): {
            "parentThreadId": str(row["parent_thread_id"]),
            "subagentStatus": str(row["status"]),
        }
        for row in rows
    }


def fetch_thread_row(paths: CodexPaths, thread_id: str, readonly: bool = True) -> dict[str, Any] | None:
    with connect_database(paths.database_path, readonly=readonly) as connection:
        row = connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return dict(row) if row else None


def read_session_index_records(paths: CodexPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not paths.session_index_path.exists():
        return records
    with paths.session_index_path.open("r", encoding="utf-8", errors="replace") as file:
        for line_number, line in enumerate(file, 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            thread_id = str(item.get("id") or "")
            thread_name = str(item.get("thread_name") or "").strip()
            if not thread_id:
                continue
            records.append(
                {
                    "threadId": thread_id,
                    "sidebarTitle": thread_name,
                    "sessionIndexUpdatedAt": str(item.get("updated_at") or ""),
                    "sessionIndexLine": line_number,
                }
            )
    return records


def read_session_index_entries(paths: CodexPaths) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for record in read_session_index_records(paths):
        entries[str(record["threadId"])] = {
            "sidebarTitle": record["sidebarTitle"],
            "sessionIndexUpdatedAt": record["sessionIndexUpdatedAt"],
            "sessionIndexLine": record["sessionIndexLine"],
        }
    return entries


rollout_title_cache: dict[tuple[str, str], tuple[int, int, dict[str, Any]]] = {}
rollout_stats_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
rollout_daily_token_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
rollout_display_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}


def read_rollout_thread_title_update(rollout_path_text: str | None, thread_id: str) -> dict[str, Any]:
    normalized_path = normalize_path_text(rollout_path_text)
    empty_result: dict[str, Any] = {
        "rolloutTitle": "",
        "rolloutTitleTimestamp": "",
        "rolloutTitleLine": None,
    }
    if not normalized_path:
        return empty_result
    rollout_path = Path(normalized_path)
    if not rollout_path.exists():
        return empty_result

    stat = rollout_path.stat()
    cache_key = (normalized_path, thread_id)
    cached = rollout_title_cache.get(cache_key)
    if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
        return dict(cached[2])

    result = dict(empty_result)
    with rollout_path.open("r", encoding="utf-8", errors="replace") as file:
        for line_number, line in enumerate(file, 1):
            if '"thread_name_updated"' not in line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") != "event_msg":
                continue
            payload = item.get("payload") or {}
            if payload.get("type") != "thread_name_updated":
                continue
            payload_thread_id = str(payload.get("thread_id") or "").strip()
            if payload_thread_id and payload_thread_id != thread_id:
                continue
            thread_name = str(payload.get("thread_name") or "").strip()
            if not thread_name:
                continue
            result = {
                "rolloutTitle": thread_name,
                "rolloutTitleTimestamp": str(item.get("timestamp") or ""),
                "rolloutTitleLine": line_number,
            }

    rollout_title_cache[cache_key] = (stat.st_size, stat.st_mtime_ns, dict(result))
    return result


def sidebar_rank_by_thread_id(
    session_index_records: list[dict[str, Any]],
    rows_by_thread_id: dict[str, dict[str, Any]],
    kind_by_thread_id: dict[str, dict[str, Any]],
) -> dict[str, int]:
    ranks: dict[str, int] = {}
    raw_rank = 0
    for record in sorted(session_index_records, key=lambda item: int(item.get("sessionIndexLine") or 0), reverse=True):
        thread_id = str(record["threadId"])
        row = rows_by_thread_id.get(thread_id)
        if not row:
            continue
        if bool(row.get("archived")):
            continue
        if kind_by_thread_id.get(thread_id, {}).get("threadKind") != "main":
            continue
        raw_rank += 1
        if thread_id not in ranks:
            ranks[thread_id] = raw_rank
    return ranks


def thread_list_rank_by_thread_id(rows: list[dict[str, Any]]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    rank = 0
    for row in rows:
        if bool(row.get("archived")):
            continue
        thread_id = str(row["id"])
        rank += 1
        ranks[thread_id] = rank
    return ranks


def main_thread_list_rank_by_thread_id(
    rows: list[dict[str, Any]],
    kind_by_thread_id: dict[str, dict[str, Any]],
) -> dict[str, int]:
    ranks: dict[str, int] = {}
    rank = 0
    for row in rows:
        if bool(row.get("archived")):
            continue
        thread_id = str(row["id"])
        if kind_by_thread_id.get(thread_id, {}).get("threadKind") != "main":
            continue
        rank += 1
        ranks[thread_id] = rank
    return ranks


def parse_source_metadata(source_text: str | None) -> dict[str, Any]:
    if not source_text or not source_text.lstrip().startswith("{"):
        return {}
    try:
        parsed = json.loads(source_text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def thread_kind_metadata(row: dict[str, Any], spawn_edge: dict[str, str] | None) -> dict[str, Any]:
    source_metadata = parse_source_metadata(row.get("source"))
    subagent_spawn = source_metadata.get("subagent", {}).get("thread_spawn", {})
    if not isinstance(subagent_spawn, dict):
        subagent_spawn = {}
    thread_source = str(row.get("thread_source") or "")
    is_subagent = bool(
        thread_source == "subagent"
        or spawn_edge
        or source_metadata.get("subagent")
    )
    return {
        "threadKind": "subagent" if is_subagent else "main",
        "threadSource": thread_source or str(row.get("source") or ""),
        "parentThreadId": (spawn_edge or {}).get("parentThreadId") or str(subagent_spawn.get("parent_thread_id") or ""),
        "subagentStatus": (spawn_edge or {}).get("subagentStatus") or "",
        "agentNickname": row.get("agent_nickname") or subagent_spawn.get("agent_nickname") or "",
        "agentRole": row.get("agent_role") or subagent_spawn.get("agent_role") or "",
    }


def timestamp_ms_from_row(row: dict[str, Any], key_prefix: str) -> int:
    millisecond_value = row.get(f"{key_prefix}_at_ms")
    if millisecond_value:
        return int(millisecond_value)
    second_value = row.get(f"{key_prefix}_at")
    if second_value:
        return int(second_value) * 1000
    return 0


def stat_file(path_text: str | None) -> dict[str, Any]:
    normalized_path = normalize_path_text(path_text)
    if not normalized_path:
        return {
            "exists": False,
            "sizeBytes": 0,
            "modifiedAtMs": None,
            "path": "",
        }
    path = Path(normalized_path)
    if not path.exists():
        return {
            "exists": False,
            "sizeBytes": 0,
            "modifiedAtMs": None,
            "path": normalized_path,
        }
    stat_result = path.stat()
    return {
        "exists": True,
        "sizeBytes": int(stat_result.st_size),
        "modifiedAtMs": int(stat_result.st_mtime * 1000),
        "path": normalized_path,
    }


def is_archived_rollout_path(paths: CodexPaths, path_text: str | None) -> bool:
    normalized_path = comparable_path_text(path_text)
    archived_root = comparable_path_text(str(paths.codex_home_path / "archived_sessions"))
    return bool(normalized_path and (normalized_path == archived_root or normalized_path.startswith(archived_root + os.sep)))


def active_rollout_target_path(paths: CodexPaths, rollout_path: Path) -> Path:
    match = re.search(r"rollout-(\d{4})-(\d{2})-(\d{2})T", rollout_path.name)
    if match:
        year, month, day = match.groups()
    else:
        now = datetime_module.datetime.now()
        year, month, day = f"{now.year:04d}", f"{now.month:02d}", f"{now.day:02d}"
    target_directory = paths.codex_home_path / "sessions" / year / month / day
    target_path = target_directory / rollout_path.name
    if not target_path.exists():
        return target_path
    if target_path.resolve(strict=False) == rollout_path.resolve(strict=False):
        return target_path
    return target_directory / f"{rollout_path.stem}.restored-{uuid.uuid4().hex[:8]}{rollout_path.suffix}"


def archived_rollout_target_path(paths: CodexPaths, rollout_path: Path) -> Path:
    target_directory = paths.codex_home_path / "archived_sessions"
    target_path = target_directory / rollout_path.name
    if not target_path.exists():
        return target_path
    if target_path.resolve(strict=False) == rollout_path.resolve(strict=False):
        return target_path
    return target_directory / f"{rollout_path.stem}.archived-{uuid.uuid4().hex[:8]}{rollout_path.suffix}"


def move_rollout_to_archive_if_needed(paths: CodexPaths, row: dict[str, Any]) -> dict[str, Any]:
    normalized_source_path = normalize_path_text(row.get("rollout_path"))
    if not normalized_source_path:
        return {"moved": False, "sourcePath": "", "targetPath": "", "missingSource": True}
    source_path = Path(normalized_source_path)
    if is_archived_rollout_path(paths, str(source_path)):
        return {"moved": False, "sourcePath": str(source_path), "targetPath": str(source_path), "alreadyArchived": True}
    if not source_path.exists():
        return {"moved": False, "sourcePath": str(source_path), "targetPath": "", "missingSource": True}

    target_path = archived_rollout_target_path(paths, source_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_stat = source_path.stat()
    if source_path.resolve(strict=False) != target_path.resolve(strict=False):
        shutil.move(str(source_path), str(target_path))
    os.utime(target_path, (source_stat.st_atime, source_stat.st_mtime))

    with connect_database(paths.database_path, readonly=False) as connection:
        connection.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", (str(target_path), str(row["id"])))
        connection.commit()

    return {
        "moved": True,
        "sourcePath": str(source_path),
        "targetPath": str(target_path),
        "sizeBytes": int(source_stat.st_size),
    }


def restore_rollout_from_archive_if_needed(
    paths: CodexPaths,
    row: dict[str, Any],
    target_updated_at: int | None = None,
) -> dict[str, Any]:
    source_path = Path(normalize_path_text(row.get("rollout_path")))
    if not is_archived_rollout_path(paths, str(source_path)):
        return {"restored": False, "sourcePath": str(source_path), "targetPath": str(source_path)}
    if not source_path.exists():
        return {"restored": False, "sourcePath": str(source_path), "targetPath": "", "missingSource": True}

    target_path = active_rollout_target_path(paths, source_path)
    target_exists_before = target_path.exists()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.resolve(strict=False) != target_path.resolve(strict=False):
        shutil.copy2(source_path, target_path)
    if target_updated_at is not None:
        os.utime(target_path, (target_updated_at, target_updated_at))

    with connect_database(paths.database_path, readonly=False) as connection:
        connection.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", (str(target_path), str(row["id"])))
        connection.commit()

    return {
        "restored": True,
        "sourcePath": str(source_path),
        "targetPath": str(target_path),
        "createdTarget": not target_exists_before,
    }


def pinned_thread_ids_from_state(global_state: dict[str, Any]) -> set[str]:
    pinned_ids = global_state.get("pinned-thread-ids")
    if pinned_ids is None:
        pinned_ids = global_state.get("electron-persisted-atom-state", {}).get("pinned-thread-ids")
    return set(pinned_ids or [])


def pinned_project_paths_from_state(global_state: dict[str, Any]) -> list[str]:
    project_paths = global_state.get("pinned-project-ids")
    if project_paths is None:
        project_paths = global_state.get("electron-persisted-atom-state", {}).get("pinned-project-ids")
    return [normalize_path_text(project_path) for project_path in (project_paths or [])]


def saved_project_paths_from_state(global_state: dict[str, Any]) -> list[str]:
    project_paths = global_state.get("electron-saved-workspace-roots")
    if project_paths is None:
        project_paths = global_state.get("electron-persisted-atom-state", {}).get("electron-saved-workspace-roots")
    return [normalize_path_text(project_path) for project_path in (project_paths or [])]


def state_value(global_state: dict[str, Any], key: str, fallback: Any) -> Any:
    value = global_state.get(key)
    if value is None:
        value = global_state.get("electron-persisted-atom-state", {}).get(key)
    return fallback if value is None else value


def projectless_thread_ids_from_state(global_state: dict[str, Any]) -> set[str]:
    return {str(thread_id) for thread_id in (state_value(global_state, "projectless-thread-ids", []) or [])}


def thread_workspace_root_hints_from_state(global_state: dict[str, Any]) -> dict[str, str]:
    hints = state_value(global_state, "thread-workspace-root-hints", {}) or {}
    if not isinstance(hints, dict):
        return {}
    return {str(thread_id): normalize_path_text(str(path_text)) for thread_id, path_text in hints.items()}


def explicit_sidebar_thread_ids_from_state(global_state: dict[str, Any]) -> set[str]:
    thread_ids = set(pinned_thread_ids_from_state(global_state))
    thread_workspace_hints = state_value(global_state, "thread-workspace-root-hints", {}) or {}
    if isinstance(thread_workspace_hints, dict):
        thread_ids.update(str(thread_id) for thread_id in thread_workspace_hints.keys())
    return thread_ids


def manager_hidden_thread_ids_from_state(global_state: dict[str, Any]) -> set[str]:
    hidden_ids: list[Any] = []
    for key in (manager_hidden_thread_ids_key, legacy_manager_hidden_thread_ids_key):
        key_hidden_ids = state_value(global_state, key, []) or []
        if isinstance(key_hidden_ids, list):
            hidden_ids.extend(key_hidden_ids)
    return {str(thread_id) for thread_id in hidden_ids}


def conversation_root_candidates(thread_workspace_hints: dict[str, str]) -> set[str]:
    roots: set[str] = set()
    for hint_path_text in thread_workspace_hints.values():
        normalized_hint = normalize_path_text(hint_path_text)
        if comparable_path_text(normalized_hint).endswith(comparable_path_text(r"Documents\Codex")):
            roots.add(normalized_hint)
    return roots


def classify_project_kind(
    project_path: str,
    saved_project_comparables: set[str],
    conversation_roots: set[str],
    is_projectless_thread: bool = False,
) -> str:
    comparable_project_path = comparable_path_text(project_path)
    for saved_project in saved_project_comparables:
        if comparable_project_path == saved_project or comparable_project_path.startswith(saved_project + os.sep):
            return "workspace_project"
    if is_projectless_thread:
        return "conversation"
    for root in conversation_roots:
        comparable_root = comparable_path_text(root)
        if comparable_project_path == comparable_root or comparable_project_path.startswith(comparable_root + os.sep):
            return "conversation"
    return "other"


def path_matches_project(project_path: str, project_comparables: set[str]) -> bool:
    comparable_project_path = comparable_path_text(project_path)
    for candidate in project_comparables:
        if comparable_project_path == candidate or comparable_project_path.startswith(candidate + os.sep):
            return True
    return False


def project_display_label(project_path: str, project_kind: str, project_labels: dict[str, str]) -> str:
    comparable_project_path = comparable_path_text(project_path)
    state_label = project_labels.get(comparable_project_path)
    if state_label:
        return state_label
    if project_kind == "conversation":
        normalized_project_path = normalize_path_text(project_path)
        parts = Path(normalized_project_path).parts
        for index, part in enumerate(parts):
            if comparable_path_text(part) == "codex" and index + 1 < len(parts):
                return str(Path(*parts[index + 1:]))
    return path_label(project_path)


def row_has_user_signal(row: dict[str, Any], sidebar_entry: dict[str, str] | None = None) -> bool:
    if bool(row.get("has_user_event")):
        return True
    if str(row.get("thread_source") or "").lower() == "user":
        return True
    if str(row.get("first_user_message") or "").strip():
        return True
    if str(row.get("preview") or "").strip():
        return True
    return bool(sidebar_entry)


def row_has_native_first_user_message(row: dict[str, Any]) -> bool:
    return bool(str(row.get("first_user_message") or "").strip())


def project_labels_from_state(global_state: dict[str, Any]) -> dict[str, str]:
    labels = global_state.get("electron-workspace-root-labels")
    if labels is None:
        labels = global_state.get("electron-persisted-atom-state", {}).get("electron-workspace-root-labels")
    result: dict[str, str] = {}
    for path_text, label in (labels or {}).items():
        result[comparable_path_text(path_text)] = str(label)
    return result


def classify_thread(
    row: dict[str, Any],
    thread_list_rank: int | None,
    main_thread_list_rank: int | None,
    session_index_rank: int | None,
    sidebar_limit: int,
    pinned_thread_ids: set[str],
    explicit_sidebar_thread_ids: set[str],
    manager_hidden_thread_ids: set[str],
    rollout_stat: dict[str, Any],
    thread_kind: str = "main",
    has_user_signal: bool = False,
    project_kind: str = "workspace_project",
    rollout_in_archived_store: bool = False,
    rollout_display_status: str = "not_scanned",
) -> tuple[str, list[str], bool]:
    thread_id = str(row["id"])
    archived = bool(row.get("archived"))
    has_user_event = bool(row.get("has_user_event"))
    has_native_first_user_message = row_has_native_first_user_message(row)
    is_pinned = thread_id in pinned_thread_ids
    has_session_index = session_index_rank is not None
    has_thread_list_rank = thread_list_rank is not None
    has_main_thread_list_rank = main_thread_list_rank is not None
    has_explicit_sidebar_reference = thread_id in explicit_sidebar_thread_ids
    is_manager_hidden = thread_id in manager_hidden_thread_ids
    reasons: list[str] = []

    if archived:
        reasons.append("archived")
    if not has_user_signal:
        reasons.append("missing_user_signal")
    if has_user_signal and not has_user_event:
        reasons.append("metadata_has_user_event_false")
    if has_user_signal and not has_native_first_user_message:
        reasons.append("missing_first_user_message")
    if not has_session_index:
        reasons.append("missing_session_index_entry")
    if not rollout_stat["exists"]:
        reasons.append("missing_rollout_file")
    if rollout_in_archived_store:
        reasons.append("rollout_in_archived_sessions")
    if rollout_display_status in {"missing_visible_event_stream", "sparse_visible_event_stream"}:
        reasons.append(rollout_display_status)
    if thread_kind == "subagent":
        reasons.append("subagent_child_thread")
    if has_thread_list_rank and thread_list_rank and thread_list_rank > sidebar_limit:
        reasons.append("outside_thread_list_initial_page")
    if project_kind == "conversation" and main_thread_list_rank and main_thread_list_rank > sidebar_limit:
        reasons.append("outside_conversation_initial_page")
    if has_session_index and session_index_rank and session_index_rank > sidebar_limit:
        reasons.append("outside_session_index_repair_window")
    if is_manager_hidden:
        reasons.append("manually_hidden_by_manager")

    active_main = thread_kind == "main" and not archived and rollout_stat["exists"]
    in_thread_list_initial_page = thread_list_rank is not None and thread_list_rank <= sidebar_limit
    in_conversation_initial_page = main_thread_list_rank is not None and main_thread_list_rank <= sidebar_limit
    in_session_index_initial_page = session_index_rank is not None and session_index_rank <= sidebar_limit
    if project_kind == "conversation":
        sidebar_visible = active_main and not is_manager_hidden and (
            has_main_thread_list_rank
            or has_thread_list_rank
            or has_session_index
            or has_explicit_sidebar_reference
            or is_pinned
        )
    else:
        sidebar_visible = active_main and not is_manager_hidden and (
            has_thread_list_rank
            or has_session_index
            or is_pinned
            or has_explicit_sidebar_reference
        )

    if thread_kind == "subagent" and not archived and rollout_stat["exists"]:
        return "subagent", reasons, False
    if archived:
        return "archived", reasons, False
    if not rollout_stat["exists"]:
        return "missing_file", reasons, False
    if active_main and rollout_in_archived_store:
        return "needs_user_event_repair", reasons, False
    if active_main and is_manager_hidden:
        return "hidden", reasons, False
    if active_main and rollout_display_status in {"missing_visible_event_stream", "sparse_visible_event_stream"}:
        return "needs_user_event_repair", reasons, False
    if active_main and has_user_signal and not has_native_first_user_message:
        return "needs_user_event_repair", reasons, False
    if sidebar_visible:
        return "visible", reasons, True
    if not has_user_signal:
        return "hidden", reasons, False
    if not has_user_event or not has_session_index:
        return "needs_user_event_repair", reasons, False
    return "hidden", reasons, False


def session_index_sort_line(thread_id: str, session_index_entries: dict[str, dict[str, Any]]) -> int:
    entry = session_index_entries.get(thread_id)
    if not entry:
        return 0
    return int(entry.get("sessionIndexLine") or 0)


def build_snapshot(
    codex_home_text: str | None = None,
    sidebar_limit: int = 50,
    validate_rollout_display: bool = False,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    global_state = read_global_state(paths)
    version = read_version(paths)
    pinned_thread_ids = pinned_thread_ids_from_state(global_state)
    explicit_sidebar_thread_ids = explicit_sidebar_thread_ids_from_state(global_state)
    manager_hidden_thread_ids = manager_hidden_thread_ids_from_state(global_state)
    saved_project_paths = saved_project_paths_from_state(global_state)
    saved_project_comparables = {comparable_path_text(path) for path in saved_project_paths}
    project_labels = project_labels_from_state(global_state)
    projectless_thread_ids = projectless_thread_ids_from_state(global_state)
    thread_workspace_hints = thread_workspace_root_hints_from_state(global_state)
    conversation_roots = conversation_root_candidates(thread_workspace_hints)

    rows = fetch_thread_rows(paths)
    rows_by_thread_id = {str(row["id"]): row for row in rows}
    session_index_records = read_session_index_records(paths)
    session_index_entries: dict[str, dict[str, Any]] = {}
    for record in session_index_records:
        session_index_entries[str(record["threadId"])] = {
            "sidebarTitle": record["sidebarTitle"],
            "sessionIndexUpdatedAt": record["sessionIndexUpdatedAt"],
            "sessionIndexLine": record["sessionIndexLine"],
        }
    spawn_edges = fetch_thread_spawn_edges(paths)
    kind_by_thread_id = {
        str(row["id"]): thread_kind_metadata(row, spawn_edges.get(str(row["id"])))
        for row in rows
    }
    rollout_stats_by_thread_id = {
        str(row["id"]): stat_file(row.get("rollout_path"))
        for row in rows
    }
    session_rank_by_thread_id = sidebar_rank_by_thread_id(
        session_index_records=session_index_records,
        rows_by_thread_id=rows_by_thread_id,
        kind_by_thread_id=kind_by_thread_id,
    )
    thread_list_rank_by_id = thread_list_rank_by_thread_id(rows)
    main_thread_list_rank_by_id = main_thread_list_rank_by_thread_id(rows, kind_by_thread_id)
    eligible_rows = [
        row for row in rows
        if str(row["id"]) in session_rank_by_thread_id or str(row["id"]) in thread_list_rank_by_id
    ]

    threads: list[dict[str, Any]] = []
    project_map: dict[str, dict[str, Any]] = {}
    total_storage_bytes = 0

    for saved_project_path in saved_project_paths:
        comparable_project_path = comparable_path_text(saved_project_path)
        project_kind = "workspace_project"
        label = project_display_label(saved_project_path, project_kind, project_labels)
        project_map[comparable_project_path] = {
            "path": saved_project_path,
            "label": label,
            "projectKind": project_kind,
            "total": 0,
            "mainThreads": 0,
            "subagentThreads": 0,
            "active": 0,
            "visible": 0,
            "hiddenByInitialLimit": 0,
            "archived": 0,
            "needsRepair": 0,
            "storageBytes": 0,
            "emptyButHasHiddenThreads": False,
        }

    for row in rows:
        thread_id = str(row["id"])
        project_path = normalize_path_text(row.get("cwd"))
        comparable_project_path = comparable_path_text(project_path)
        project_kind = classify_project_kind(
            project_path=project_path,
            saved_project_comparables=saved_project_comparables,
            conversation_roots=conversation_roots,
            is_projectless_thread=thread_id in projectless_thread_ids,
        )
        label = project_display_label(project_path, project_kind, project_labels)
        rollout_stat = rollout_stats_by_thread_id[thread_id]
        rollout_in_archived_store = is_archived_rollout_path(paths, rollout_stat["path"])
        session_index_rank = session_rank_by_thread_id.get(thread_id)
        thread_list_rank = thread_list_rank_by_id.get(thread_id)
        main_thread_list_rank = main_thread_list_rank_by_id.get(thread_id)
        rank_candidates = [
            rank for rank in (thread_list_rank, session_index_rank)
            if rank is not None
        ]
        recent_rank = min(rank_candidates) if rank_candidates else None
        kind_metadata = kind_by_thread_id[thread_id]
        sidebar_entry = session_index_entries.get(thread_id, {})
        sqlite_title = row.get("title") or "(untitled)"
        session_index_title = sidebar_entry.get("sidebarTitle") or ""
        should_resolve_rollout_title = (
            project_kind == "conversation"
            and kind_metadata["threadKind"] == "main"
            and (
                thread_id in pinned_thread_ids
                or thread_id in explicit_sidebar_thread_ids
                or bool(main_thread_list_rank is not None and main_thread_list_rank <= sidebar_limit)
            )
        )
        rollout_title_entry = (
            read_rollout_thread_title_update(row.get("rollout_path"), thread_id)
            if should_resolve_rollout_title
            else {"rolloutTitle": "", "rolloutTitleTimestamp": "", "rolloutTitleLine": None}
        )
        rollout_title = str(rollout_title_entry.get("rolloutTitle") or "")
        sidebar_title = rollout_title or session_index_title
        display_title = sidebar_title or sqlite_title
        has_user_signal = row_has_user_signal(row, sidebar_entry)
        active_main_candidate = kind_metadata["threadKind"] == "main" and not bool(row.get("archived")) and bool(rollout_stat["exists"])
        candidate_thread_list_visible = bool(thread_list_rank is not None and thread_list_rank <= sidebar_limit)
        candidate_conversation_visible = bool(main_thread_list_rank is not None and main_thread_list_rank <= sidebar_limit)
        candidate_session_index_visible = bool(session_index_rank is not None and session_index_rank <= sidebar_limit)
        should_validate_rollout_display = bool(validate_rollout_display) and active_main_candidate and (
            thread_id in pinned_thread_ids
            or thread_id in explicit_sidebar_thread_ids
            or (
                project_kind == "conversation"
                and candidate_conversation_visible
            )
            or (
                project_kind != "conversation"
                and (candidate_session_index_visible or thread_id in pinned_thread_ids or thread_id in explicit_sidebar_thread_ids)
            )
        )
        rollout_display = (
            rollout_display_integrity(str(row.get("rollout_path") or ""))
            if should_validate_rollout_display
            else {
                "status": "not_scanned",
                "responseUserMessages": 0,
                "responseAssistantMessages": 0,
                "visibleUserMessages": 0,
                "visibleAgentMessages": 0,
                "eventUserMessages": 0,
                "eventAgentMessages": 0,
                "parseErrors": 0,
            }
        )
        visibility, hidden_reasons, codex_visible = classify_thread(
            row=row,
            thread_list_rank=thread_list_rank,
            main_thread_list_rank=main_thread_list_rank,
            session_index_rank=session_index_rank,
            sidebar_limit=sidebar_limit,
            pinned_thread_ids=pinned_thread_ids,
            explicit_sidebar_thread_ids=explicit_sidebar_thread_ids,
            manager_hidden_thread_ids=manager_hidden_thread_ids,
            rollout_stat=rollout_stat,
            thread_kind=kind_metadata["threadKind"],
            has_user_signal=has_user_signal,
            project_kind=project_kind,
            rollout_in_archived_store=rollout_in_archived_store,
            rollout_display_status=str(rollout_display.get("status") or "not_scanned"),
        )
        total_storage_bytes += int(rollout_stat["sizeBytes"])

        if comparable_project_path not in project_map:
            project_map[comparable_project_path] = {
                "path": project_path,
                "label": label,
                "projectKind": project_kind,
                "total": 0,
                "mainThreads": 0,
                "subagentThreads": 0,
                "active": 0,
                "visible": 0,
                "hiddenByInitialLimit": 0,
                "archived": 0,
                "needsRepair": 0,
                "storageBytes": 0,
                "emptyButHasHiddenThreads": False,
            }
        project_entry = project_map[comparable_project_path]
        project_entry["total"] += 1
        if kind_metadata["threadKind"] == "subagent":
            project_entry["subagentThreads"] += 1
        else:
            project_entry["mainThreads"] += 1
        project_entry["storageBytes"] += int(rollout_stat["sizeBytes"])
        if kind_metadata["threadKind"] == "main" and not bool(row.get("archived")) and rollout_stat["exists"]:
            project_entry["active"] += 1
        if codex_visible:
            project_entry["visible"] += 1
        outside_initial_limit = False
        if outside_initial_limit:
            project_entry["hiddenByInitialLimit"] += 1
        if bool(row.get("archived")):
            project_entry["archived"] += 1
        if visibility in {"needs_user_event_repair", "missing_file"}:
            project_entry["needsRepair"] += 1

        threads.append(
            {
                "id": str(row["id"]),
                "title": display_title,
                "sqliteTitle": sqlite_title,
                "sidebarTitle": sidebar_title,
                "sessionIndexTitle": session_index_title,
                "sessionIndexUpdatedAt": sidebar_entry.get("sessionIndexUpdatedAt") or "",
                "rolloutTitle": rollout_title,
                "rolloutTitleTimestamp": rollout_title_entry.get("rolloutTitleTimestamp") or "",
                "rolloutTitleLine": rollout_title_entry.get("rolloutTitleLine"),
                "preview": row.get("preview") or row.get("first_user_message") or "",
                "projectPath": project_path,
                "projectLabel": label,
                "projectKind": project_kind,
                "rolloutPath": rollout_stat["path"],
                "source": row.get("source") or "",
                **kind_metadata,
                "model": row.get("model") or row.get("model_provider") or "",
                "createdAtMs": timestamp_ms_from_row(row, "created"),
                "updatedAtMs": timestamp_ms_from_row(row, "updated"),
                "archived": bool(row.get("archived")),
                "archivedAtMs": int(row["archived_at"]) * 1000 if row.get("archived_at") else None,
                "hasUserEvent": bool(row.get("has_user_event")),
                "hasUserSignal": has_user_signal,
                "tokensUsed": int(row.get("tokens_used") or 0),
                "fileExists": bool(rollout_stat["exists"]),
                "fileSizeBytes": int(rollout_stat["sizeBytes"]),
                "childThreadCount": 0,
                "childFileSizeBytes": 0,
                "totalFileSizeBytes": int(rollout_stat["sizeBytes"]),
                "childTokensUsed": 0,
                "totalTokensUsed": int(row.get("tokens_used") or 0),
                "fileModifiedAtMs": rollout_stat["modifiedAtMs"],
                "rolloutInArchivedStore": rollout_in_archived_store,
                "recentRank": recent_rank,
                "threadListRank": thread_list_rank,
                "mainThreadListRank": main_thread_list_rank,
                "sessionIndexRank": session_index_rank,
                "isPinned": thread_id in pinned_thread_ids,
                "explicitSidebarReference": thread_id in explicit_sidebar_thread_ids,
                "managerHidden": thread_id in manager_hidden_thread_ids,
                "presentInThreadList": thread_list_rank is not None,
                "presentInSessionIndex": session_index_rank is not None,
                "initialThreadListVisible": bool(thread_list_rank is not None and thread_list_rank <= sidebar_limit),
                "initialSessionIndexVisible": bool(session_index_rank is not None and session_index_rank <= sidebar_limit),
                "inInitialSidebarPage": bool(codex_visible),
                "outsideInitialLimit": outside_initial_limit,
                "codexVisible": codex_visible,
                "visibility": visibility,
                "hiddenReasons": hidden_reasons,
                "rolloutDisplayStatus": rollout_display.get("status") or "not_scanned",
                "rolloutDisplayResponseUserMessages": int(rollout_display.get("responseUserMessages") or 0),
                "rolloutDisplayResponseAssistantMessages": int(rollout_display.get("responseAssistantMessages") or 0),
                "rolloutDisplayVisibleUserMessages": int(rollout_display.get("visibleUserMessages") or 0),
                "rolloutDisplayVisibleAgentMessages": int(rollout_display.get("visibleAgentMessages") or 0),
                "rolloutDisplayEventUserMessages": int(rollout_display.get("eventUserMessages") or 0),
                "rolloutDisplayEventAgentMessages": int(rollout_display.get("eventAgentMessages") or 0),
                "gitBranch": row.get("git_branch") or "",
                "cliVersion": row.get("cli_version") or "",
            }
        )

    threads_by_id = {str(thread["id"]): thread for thread in threads}
    children_by_parent_id: dict[str, list[str]] = {}
    for thread in threads:
        parent_thread_id = str(thread.get("parentThreadId") or "")
        if parent_thread_id and parent_thread_id in threads_by_id:
            children_by_parent_id.setdefault(parent_thread_id, []).append(str(thread["id"]))

    aggregate_cache: dict[str, dict[str, int]] = {}

    def child_thread_aggregate(thread_id: str, visiting: set[str] | None = None) -> dict[str, int]:
        if thread_id in aggregate_cache:
            return aggregate_cache[thread_id]
        if visiting is None:
            visiting = set()
        if thread_id in visiting:
            return {"count": 0, "fileSizeBytes": 0, "tokensUsed": 0}
        visiting.add(thread_id)
        aggregate = {"count": 0, "fileSizeBytes": 0, "tokensUsed": 0}
        for child_thread_id in children_by_parent_id.get(thread_id, []):
            child_thread = threads_by_id.get(child_thread_id)
            if not child_thread:
                continue
            descendant_aggregate = child_thread_aggregate(child_thread_id, visiting)
            aggregate["count"] += 1 + descendant_aggregate["count"]
            aggregate["fileSizeBytes"] += int(child_thread.get("fileSizeBytes") or 0) + descendant_aggregate["fileSizeBytes"]
            aggregate["tokensUsed"] += int(child_thread.get("tokensUsed") or 0) + descendant_aggregate["tokensUsed"]
        visiting.remove(thread_id)
        aggregate_cache[thread_id] = aggregate
        return aggregate

    for thread in threads:
        aggregate = child_thread_aggregate(str(thread["id"]))
        thread["childThreadCount"] = aggregate["count"]
        thread["childFileSizeBytes"] = aggregate["fileSizeBytes"]
        thread["totalFileSizeBytes"] = int(thread.get("fileSizeBytes") or 0) + aggregate["fileSizeBytes"]
        thread["childTokensUsed"] = aggregate["tokensUsed"]
        thread["totalTokensUsed"] = int(thread.get("tokensUsed") or 0) + aggregate["tokensUsed"]

    projects = list(project_map.values())
    threads_by_project_key: dict[str, list[dict[str, Any]]] = {}
    for thread in threads:
        threads_by_project_key.setdefault(comparable_path_text(thread["projectPath"]), []).append(thread)
    for project in projects:
        project["emptyButHasHiddenThreads"] = (
            project["active"] > 0
            and project["visible"] == 0
            and (project["needsRepair"] > 0 or project["hiddenByInitialLimit"] > 0)
        )
        if project["projectKind"] == "conversation" and project["total"] == 1:
            project_threads = threads_by_project_key.get(comparable_path_text(project["path"]), [])
            if project_threads and project_threads[0].get("title"):
                conversation_label = str(project_threads[0]["title"]).strip()
                if conversation_label:
                    project["label"] = conversation_label
                    project_threads[0]["projectLabel"] = conversation_label
    project_kind_order = {"workspace_project": 0, "conversation": 1, "other": 2}
    projects.sort(key=lambda project: (
        project_kind_order.get(project["projectKind"], 99),
        project["label"].lower(),
        project["path"].lower(),
    ))

    visible_count = sum(1 for thread in threads if thread["codexVisible"])
    hidden_by_limit_count = sum(1 for thread in threads if thread["outsideInitialLimit"])
    archived_count = sum(1 for thread in threads if thread["archived"])
    main_count = sum(1 for thread in threads if thread["threadKind"] == "main")
    subagent_count = sum(1 for thread in threads if thread["threadKind"] == "subagent")
    needs_repair_count = sum(
        1 for thread in threads if thread["visibility"] in {"needs_user_event_repair", "missing_file"}
    )

    return {
        "codexHome": str(paths.codex_home_path),
        "databasePath": str(paths.database_path),
        "globalStatePath": str(paths.global_state_path),
        "sessionIndexPath": str(paths.session_index_path),
        "sidebarLimit": sidebar_limit,
        "version": version,
        "summary": {
            "totalThreads": len(threads),
            "mainThreads": main_count,
            "subagentThreads": subagent_count,
            "eligibleThreads": len(eligible_rows),
            "codexVisibleThreads": visible_count,
            "hiddenByInitialLimit": hidden_by_limit_count,
            "archivedThreads": archived_count,
            "needsRepairThreads": needs_repair_count,
            "savedProjects": len(saved_project_paths),
            "workspaceProjects": sum(1 for project in projects if project["projectKind"] == "workspace_project"),
            "conversationProjects": sum(1 for project in projects if project["projectKind"] == "conversation"),
            "otherProjects": sum(1 for project in projects if project["projectKind"] == "other"),
            "emptyProjectsWithHiddenThreads": sum(1 for project in projects if project["emptyButHasHiddenThreads"]),
            "totalStorageBytes": total_storage_bytes,
        },
        "threads": threads,
        "projects": projects,
        "generatedAtMs": int(time.time() * 1000),
    }


def parse_rollout_stats(rollout_path_text: str) -> dict[str, Any]:
    normalized_path = normalize_path_text(rollout_path_text)
    path = Path(normalized_path) if normalized_path else None
    if path and path.exists():
        stat = path.stat()
        cached = rollout_stats_cache.get(normalized_path)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return copy.deepcopy(cached[2])
    result = {
        "lineCount": 0,
        "userMessages": 0,
        "assistantMessages": 0,
        "toolCalls": 0,
        "toolOutputs": 0,
        "eventMessages": 0,
        "invalidJsonLines": 0,
        "firstTimestamp": None,
        "lastTimestamp": None,
    }
    display_result: dict[str, Any] = {
        "status": "not_scanned",
        "responseUserMessages": 0,
        "responseAssistantMessages": 0,
        "visibleUserMessages": 0,
        "visibleAgentMessages": 0,
        "topLevelUserMessages": 0,
        "eventUserMessages": 0,
        "eventAgentMessages": 0,
        "parseErrors": 0,
    }
    daily_result = {
        "summary": {
            "tokens": 0,
            "days": 0,
            "firstDate": None,
            "lastDate": None,
            "peakDate": None,
            "peakTokens": 0,
            "tokenEvents": 0,
            "countedTokenEvents": 0,
            "zeroDeltaTokenEvents": 0,
            "fallbackTokenEvents": 0,
            "parseErrors": 0,
            "missingFile": False,
        },
        "days": [],
    }
    if not path or not path.exists():
        display_result["status"] = "missing_rollout_file"
        daily_result["summary"]["missingFile"] = True
        return result

    daily_records: dict[str, dict[str, Any]] = {}
    previous_cumulative_tokens: int | None = None
    with path.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            result["lineCount"] += 1
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                result["invalidJsonLines"] += 1
                display_result["parseErrors"] += 1
                if "token_count" in line:
                    daily_result["summary"]["parseErrors"] += 1
                continue
            timestamp = item.get("timestamp")
            if timestamp and result["firstTimestamp"] is None:
                result["firstTimestamp"] = timestamp
            if timestamp:
                result["lastTimestamp"] = timestamp
            item_type = item.get("type")
            payload = item.get("payload") or {}
            if item_type == "user_message":
                result["userMessages"] += 1
                text = text_from_user_message_payload(payload)
                if is_real_user_prompt(text):
                    display_result["topLevelUserMessages"] += 1
                    display_result["visibleUserMessages"] += 1
            elif item_type == "event_msg":
                result["eventMessages"] += 1
                if isinstance(payload, dict):
                    payload_type = str(payload.get("type") or "")
                    message = event_message_text(payload)
                    if payload_type == "user_message" and is_real_user_prompt(message):
                        display_result["eventUserMessages"] += 1
                        display_result["visibleUserMessages"] += 1
                    elif payload_type == "agent_message" and message:
                        display_result["eventAgentMessages"] += 1
                        display_result["visibleAgentMessages"] += 1
                    elif payload_type == "token_count":
                        daily_result["summary"]["tokenEvents"] += 1
                        date_key = local_date_key_from_timestamp(item.get("timestamp"))
                        if not date_key:
                            daily_result["summary"]["zeroDeltaTokenEvents"] += 1
                            continue
                        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                        cumulative_tokens = token_usage_total(info.get("total_token_usage") or info.get("totalTokenUsage"))
                        last_tokens = token_usage_total(info.get("last_token_usage") or info.get("lastTokenUsage"))
                        token_delta: int | None = None
                        if cumulative_tokens is not None:
                            if previous_cumulative_tokens is None:
                                token_delta = cumulative_tokens
                            elif cumulative_tokens >= previous_cumulative_tokens:
                                token_delta = cumulative_tokens - previous_cumulative_tokens
                            elif last_tokens is not None:
                                token_delta = last_tokens
                                daily_result["summary"]["fallbackTokenEvents"] += 1
                            else:
                                token_delta = cumulative_tokens
                                daily_result["summary"]["fallbackTokenEvents"] += 1
                            previous_cumulative_tokens = cumulative_tokens
                        elif last_tokens is not None:
                            token_delta = last_tokens
                            daily_result["summary"]["fallbackTokenEvents"] += 1

                        if not token_delta or token_delta <= 0:
                            daily_result["summary"]["zeroDeltaTokenEvents"] += 1
                            continue

                        daily_record = daily_records.setdefault(
                            date_key,
                            {"date": date_key, "tokens": 0, "tokenEvents": 0},
                        )
                        daily_record["tokens"] += int(token_delta)
                        daily_record["tokenEvents"] += 1
                        daily_result["summary"]["tokens"] += int(token_delta)
                        daily_result["summary"]["countedTokenEvents"] += 1
            elif item_type == "response_item":
                payload_type = payload.get("type")
                if payload_type == "message":
                    role = payload.get("role")
                    if role == "user":
                        result["userMessages"] += 1
                    elif role == "assistant":
                        result["assistantMessages"] += 1
                    user_text = text_from_response_role_payload(payload, "user")
                    if is_real_user_prompt(user_text):
                        display_result["responseUserMessages"] += 1
                    assistant_text = text_from_response_role_payload(payload, "assistant")
                    if assistant_text:
                        display_result["responseAssistantMessages"] += 1
                elif payload_type == "function_call":
                    result["toolCalls"] += 1
                elif payload_type == "function_call_output":
                    result["toolOutputs"] += 1
    response_chat_messages = int(display_result["responseUserMessages"]) + int(display_result["responseAssistantMessages"])
    visible_chat_messages = int(display_result["visibleUserMessages"]) + int(display_result["visibleAgentMessages"])
    if response_chat_messages == 0:
        display_result["status"] = "ok" if int(display_result["visibleUserMessages"]) > 0 else "empty"
    elif int(display_result["responseUserMessages"]) > 0 and int(display_result["visibleUserMessages"]) == 0:
        display_result["status"] = "missing_visible_event_stream"
    elif int(display_result["responseAssistantMessages"]) > 0 and int(display_result["visibleAgentMessages"]) == 0:
        display_result["status"] = "missing_visible_event_stream"
    elif response_chat_messages >= 10 and visible_chat_messages < max(2, response_chat_messages // 2):
        display_result["status"] = "sparse_visible_event_stream"
    else:
        display_result["status"] = "ok"

    daily_days = sorted(daily_records.values(), key=lambda record: str(record["date"]))
    daily_result["days"] = daily_days
    daily_result["summary"]["days"] = len(daily_days)
    if daily_days:
        daily_result["summary"]["firstDate"] = daily_days[0]["date"]
        daily_result["summary"]["lastDate"] = daily_days[-1]["date"]
        peak_record = max(daily_days, key=lambda record: int(record["tokens"]))
        daily_result["summary"]["peakDate"] = peak_record["date"]
        daily_result["summary"]["peakTokens"] = int(peak_record["tokens"])
    stat = path.stat()
    rollout_stats_cache[normalized_path] = (stat.st_size, stat.st_mtime_ns, copy.deepcopy(result))
    rollout_display_cache[normalized_path] = (stat.st_size, stat.st_mtime_ns, copy.deepcopy(display_result))
    rollout_daily_token_cache[normalized_path] = (stat.st_size, stat.st_mtime_ns, copy.deepcopy(daily_result))
    return result


def nonnegative_int(value: Any) -> int | None:
    try:
        parsed_value = int(value)
    except (TypeError, ValueError):
        return None
    return parsed_value if parsed_value >= 0 else None


def token_usage_total(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    total_tokens = nonnegative_int(usage.get("total_tokens") or usage.get("totalTokens"))
    if total_tokens is not None:
        return total_tokens
    input_tokens = nonnegative_int(usage.get("input_tokens") or usage.get("inputTokens")) or 0
    output_tokens = nonnegative_int(usage.get("output_tokens") or usage.get("outputTokens")) or 0
    if input_tokens or output_tokens:
        return input_tokens + output_tokens
    return None


def local_date_key_from_timestamp(timestamp_text: Any) -> str | None:
    if not timestamp_text:
        return None
    try:
        timestamp = datetime_module.datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone()
    return timestamp.date().isoformat()


def local_date_key_from_timestamp_ms(timestamp_ms: Any) -> str | None:
    parsed_timestamp_ms = nonnegative_int(timestamp_ms)
    if parsed_timestamp_ms is None:
        return None
    return datetime_module.datetime.fromtimestamp(parsed_timestamp_ms / 1000).date().isoformat()


def parse_local_date_key(date_key: Any) -> datetime_module.date | None:
    if not date_key:
        return None
    try:
        return datetime_module.date.fromisoformat(str(date_key))
    except ValueError:
        return None


def token_usage_zero_day_record(date_key: str) -> dict[str, Any]:
    return {
        "date": date_key,
        "ownTokens": 0,
        "childTokens": 0,
        "totalTokens": 0,
        "ownTokenEvents": 0,
        "childTokenEvents": 0,
        "ownUnknownTokenThreads": 0,
        "childUnknownTokenThreads": 0,
        "unknownTokenThreads": 0,
        "hasData": False,
        "hasUnknownTokens": False,
    }


def token_count_payload(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict) or item.get("type") != "event_msg":
        return None
    payload = item.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    return payload


def parse_rollout_daily_token_usage(rollout_path_text: str) -> dict[str, Any]:
    normalized_path = normalize_path_text(rollout_path_text)
    path = Path(normalized_path) if normalized_path else None
    if path and path.exists():
        stat = path.stat()
        cached = rollout_daily_token_cache.get(normalized_path)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return copy.deepcopy(cached[2])
        parse_rollout_stats(normalized_path)
        cached = rollout_daily_token_cache.get(normalized_path)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return copy.deepcopy(cached[2])
    result = {
        "summary": {
            "tokens": 0,
            "days": 0,
            "firstDate": None,
            "lastDate": None,
            "peakDate": None,
            "peakTokens": 0,
            "tokenEvents": 0,
            "countedTokenEvents": 0,
            "zeroDeltaTokenEvents": 0,
            "fallbackTokenEvents": 0,
            "parseErrors": 0,
            "missingFile": False,
        },
        "days": [],
    }
    if not path or not path.exists():
        result["summary"]["missingFile"] = True
        return result

    daily_records: dict[str, dict[str, Any]] = {}
    previous_cumulative_tokens: int | None = None
    with path.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            if "token_count" not in line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                result["summary"]["parseErrors"] += 1
                continue
            payload = token_count_payload(item)
            if payload is None:
                continue
            result["summary"]["tokenEvents"] += 1
            date_key = local_date_key_from_timestamp(item.get("timestamp"))
            if not date_key:
                result["summary"]["zeroDeltaTokenEvents"] += 1
                continue
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            cumulative_tokens = token_usage_total(info.get("total_token_usage") or info.get("totalTokenUsage"))
            last_tokens = token_usage_total(info.get("last_token_usage") or info.get("lastTokenUsage"))
            token_delta: int | None = None
            if cumulative_tokens is not None:
                if previous_cumulative_tokens is None:
                    token_delta = cumulative_tokens
                elif cumulative_tokens >= previous_cumulative_tokens:
                    token_delta = cumulative_tokens - previous_cumulative_tokens
                elif last_tokens is not None:
                    token_delta = last_tokens
                    result["summary"]["fallbackTokenEvents"] += 1
                else:
                    token_delta = cumulative_tokens
                    result["summary"]["fallbackTokenEvents"] += 1
                previous_cumulative_tokens = cumulative_tokens
            elif last_tokens is not None:
                token_delta = last_tokens
                result["summary"]["fallbackTokenEvents"] += 1

            if not token_delta or token_delta <= 0:
                result["summary"]["zeroDeltaTokenEvents"] += 1
                continue

            record = daily_records.setdefault(date_key, {"date": date_key, "tokens": 0, "tokenEvents": 0})
            record["tokens"] += int(token_delta)
            record["tokenEvents"] += 1
            result["summary"]["tokens"] += int(token_delta)
            result["summary"]["countedTokenEvents"] += 1

    days = sorted(daily_records.values(), key=lambda record: str(record["date"]))
    result["days"] = days
    result["summary"]["days"] = len(days)
    if days:
        result["summary"]["firstDate"] = days[0]["date"]
        result["summary"]["lastDate"] = days[-1]["date"]
        peak_record = max(days, key=lambda record: int(record["tokens"]))
        result["summary"]["peakDate"] = peak_record["date"]
        result["summary"]["peakTokens"] = int(peak_record["tokens"])
    stat = path.stat()
    rollout_daily_token_cache[normalized_path] = (stat.st_size, stat.st_mtime_ns, copy.deepcopy(result))
    return result


def thread_daily_token_usage_with_unknown_markers(thread: dict[str, Any]) -> dict[str, Any]:
    usage = parse_rollout_daily_token_usage(str(thread.get("rolloutPath") or ""))
    usage.setdefault("summary", {})
    usage["summary"].setdefault("unknownTokenThreads", 0)
    usage["summary"].setdefault("hasUnknownTokens", False)
    for day in usage.get("days") or []:
        day.setdefault("unknownTokenThreads", 0)
        day.setdefault("hasUnknownTokens", False)
    if int((usage.get("summary") or {}).get("tokens") or 0) > 0:
        return usage
    sqlite_tokens = nonnegative_int(thread.get("tokensUsed"))
    if not sqlite_tokens:
        return usage
    date_key = (
        local_date_key_from_timestamp_ms(thread.get("updatedAtMs"))
        or local_date_key_from_timestamp_ms(thread.get("fileModifiedAtMs"))
        or local_date_key_from_timestamp_ms(thread.get("createdAtMs"))
    )
    if not date_key:
        return usage
    usage["days"] = [
        {
            "date": date_key,
            "tokens": 0,
            "tokenEvents": 0,
            "unknownTokenThreads": 1,
            "hasUnknownTokens": True,
        }
    ]
    usage["summary"].update(
        {
            "tokens": 0,
            "days": 0,
            "firstDate": date_key,
            "lastDate": date_key,
            "peakDate": None,
            "peakTokens": 0,
            "unknownTokenThreads": 1,
            "hasUnknownTokens": True,
        }
    )
    return usage


def descendant_threads_from_snapshot(thread_id: str, threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    threads_by_parent_id: dict[str, list[dict[str, Any]]] = {}
    for thread in threads:
        parent_thread_id = str(thread.get("parentThreadId") or "")
        if not parent_thread_id:
            continue
        threads_by_parent_id.setdefault(parent_thread_id, []).append(thread)

    descendants: list[dict[str, Any]] = []

    def visit(current_thread_id: str, visiting: set[str]) -> None:
        if current_thread_id in visiting:
            return
        visiting.add(current_thread_id)
        for child_thread in threads_by_parent_id.get(current_thread_id, []):
            child_thread_id = str(child_thread.get("id") or "")
            if not child_thread_id:
                continue
            descendants.append(child_thread)
            visit(child_thread_id, visiting)
        visiting.remove(current_thread_id)

    visit(thread_id, set())
    return descendants


def build_thread_daily_token_usage(thread: dict[str, Any], threads: list[dict[str, Any]]) -> dict[str, Any]:
    own_usage = thread_daily_token_usage_with_unknown_markers(thread)
    child_usages = [
        thread_daily_token_usage_with_unknown_markers(child_thread)
        for child_thread in descendant_threads_from_snapshot(str(thread.get("id") or ""), threads)
    ]
    records_by_date: dict[str, dict[str, Any]] = {}

    def record_for_date(date_key: str) -> dict[str, Any]:
        return records_by_date.setdefault(
            date_key,
            {
                "date": date_key,
                "ownTokens": 0,
                "childTokens": 0,
                "totalTokens": 0,
                "ownTokenEvents": 0,
                "childTokenEvents": 0,
                "ownUnknownTokenThreads": 0,
                "childUnknownTokenThreads": 0,
                "unknownTokenThreads": 0,
                "hasData": False,
                "hasUnknownTokens": False,
            },
        )

    for day in own_usage.get("days") or []:
        date_key = str(day.get("date") or "")
        if not date_key:
            continue
        record = record_for_date(date_key)
        record["ownTokens"] += int(day.get("tokens") or 0)
        record["ownTokenEvents"] += int(day.get("tokenEvents") or 0)
        record["ownUnknownTokenThreads"] += int(day.get("unknownTokenThreads") or 0)

    for child_usage in child_usages:
        for day in child_usage.get("days") or []:
            date_key = str(day.get("date") or "")
            if not date_key:
                continue
            record = record_for_date(date_key)
            record["childTokens"] += int(day.get("tokens") or 0)
            record["childTokenEvents"] += int(day.get("tokenEvents") or 0)
            record["childUnknownTokenThreads"] += int(day.get("unknownTokenThreads") or 0)

    active_days = []
    dated_days = []
    for record in sorted(records_by_date.values(), key=lambda item: str(item["date"])):
        record["totalTokens"] = int(record["ownTokens"]) + int(record["childTokens"])
        record["unknownTokenThreads"] = int(record["ownUnknownTokenThreads"]) + int(record["childUnknownTokenThreads"])
        if record["unknownTokenThreads"] > 0:
            record["hasUnknownTokens"] = True
        if record["totalTokens"] > 0:
            record["hasData"] = True
            active_days.append(record)
        if record["totalTokens"] > 0 or record["unknownTokenThreads"] > 0:
            dated_days.append(record)

    days: list[dict[str, Any]] = []
    first_active_date = parse_local_date_key(dated_days[0]["date"]) if dated_days else None
    last_active_date = parse_local_date_key(dated_days[-1]["date"]) if dated_days else None
    if first_active_date and last_active_date and first_active_date <= last_active_date:
        active_records_by_date = {str(record["date"]): record for record in dated_days}
        current_date = first_active_date
        while current_date <= last_active_date:
            date_key = current_date.isoformat()
            days.append(active_records_by_date.get(date_key, token_usage_zero_day_record(date_key)))
            current_date += datetime_module.timedelta(days=1)
    else:
        days = dated_days

    own_summary = own_usage.get("summary") or {}
    child_tokens = sum(int((child_usage.get("summary") or {}).get("tokens") or 0) for child_usage in child_usages)
    child_token_events = sum(int((child_usage.get("summary") or {}).get("tokenEvents") or 0) for child_usage in child_usages)
    child_counted_events = sum(int((child_usage.get("summary") or {}).get("countedTokenEvents") or 0) for child_usage in child_usages)
    child_zero_delta_events = sum(int((child_usage.get("summary") or {}).get("zeroDeltaTokenEvents") or 0) for child_usage in child_usages)
    child_fallback_events = sum(int((child_usage.get("summary") or {}).get("fallbackTokenEvents") or 0) for child_usage in child_usages)
    child_unknown_token_threads = sum(int((child_usage.get("summary") or {}).get("unknownTokenThreads") or 0) for child_usage in child_usages)
    child_missing_files = sum(1 for child_usage in child_usages if (child_usage.get("summary") or {}).get("missingFile"))
    peak_record = max(active_days, key=lambda item: int(item["totalTokens"]), default=None)
    unknown_days = sum(1 for day in days if int(day.get("unknownTokenThreads") or 0) > 0)
    return {
        "summary": {
            "ownTokens": int(own_summary.get("tokens") or 0),
            "childTokens": child_tokens,
            "totalTokens": int(own_summary.get("tokens") or 0) + child_tokens,
            "days": len(active_days),
            "activeDays": len(active_days),
            "rangeDays": len(days),
            "zeroDays": max(0, len(days) - len(active_days)),
            "unknownDays": unknown_days,
            "firstDate": days[0]["date"] if days else None,
            "lastDate": days[-1]["date"] if days else None,
            "peakDate": peak_record["date"] if peak_record else None,
            "peakTokens": int(peak_record["totalTokens"]) if peak_record else 0,
            "ownTokenEvents": int(own_summary.get("tokenEvents") or 0),
            "childTokenEvents": child_token_events,
            "ownCountedTokenEvents": int(own_summary.get("countedTokenEvents") or 0),
            "childCountedTokenEvents": child_counted_events,
            "zeroDeltaTokenEvents": int(own_summary.get("zeroDeltaTokenEvents") or 0) + child_zero_delta_events,
            "fallbackTokenEvents": int(own_summary.get("fallbackTokenEvents") or 0) + child_fallback_events,
            "ownUnknownTokenThreads": int(own_summary.get("unknownTokenThreads") or 0),
            "childUnknownTokenThreads": child_unknown_token_threads,
            "unknownTokenThreads": int(own_summary.get("unknownTokenThreads") or 0) + child_unknown_token_threads,
            "childThreadCount": len(child_usages),
            "missingChildRolloutFiles": child_missing_files,
        },
        "days": days,
    }


def text_parts_from_message_content(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return parts


def text_from_user_message_payload(payload: Any) -> str:
    text_parts: list[str] = []
    if isinstance(payload, str):
        text_parts.append(payload)
    elif isinstance(payload, dict):
        text_parts.extend(text_parts_from_message_content(payload.get("content")))
        text = payload.get("text")
        if isinstance(text, str):
            text_parts.append(text)
    return "\n".join(part for part in text_parts if part).strip()


def text_from_response_message_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") != "message" or payload.get("role") != "user":
        return ""
    return "\n".join(part for part in text_parts_from_message_content(payload.get("content")) if part).strip()


def text_from_response_role_payload(payload: Any, role: str) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") != "message" or payload.get("role") != role:
        return ""
    return "\n".join(part for part in text_parts_from_message_content(payload.get("content")) if part).strip()


def text_from_compacted_history_message(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    if item.get("type") != "message" or item.get("role") != "user":
        return ""
    return "\n".join(part for part in text_parts_from_message_content(item.get("content")) if part).strip()


def is_agent_instruction_prompt(text: str) -> bool:
    return text.lstrip().startswith("# AGENTS.md instructions")


def is_subagent_notification_prompt(text: str) -> bool:
    prefix = text.lstrip()[:5000]
    return prefix.startswith("<subagent_notification>") or (
        '"agent_path"' in prefix and '"status"' in prefix and "subagent" in prefix.lower()
    )


def is_automation_prompt(text: str) -> bool:
    prefix = text.lstrip()[:5000]
    lower_prefix = prefix.lower()
    return (
        lower_prefix.startswith("<heartbeat>")
        or lower_prefix.startswith("<automation>")
        or lower_prefix.startswith("<scheduled_task>")
        or "<automation_id>" in lower_prefix
        or "<current_time_iso>" in lower_prefix and "<instructions>" in lower_prefix
    )


def is_thread_delegation_prompt(text: str) -> bool:
    prefix = text.lstrip()[:5000]
    lower_prefix = prefix.lower()
    return lower_prefix.startswith("<codex_delegation")


def is_codex_internal_context_prompt(text: str) -> bool:
    prefix = text.lstrip()[:5000]
    return prefix.startswith("<codex_internal_context")


def is_internal_context_prompt(text: str) -> bool:
    prefix = text.lstrip()[:5000]
    return (
        is_agent_instruction_prompt(text)
        or prefix.startswith("<environment_context>")
        or prefix.startswith("<turn_aborted>")
        or prefix.startswith("<user_interruption>")
        or "<environment_context>" in prefix
        or "<permissions instructions>" in prefix
    )


def is_real_user_prompt(text: str) -> bool:
    return (
        bool(text.strip())
        and not is_internal_context_prompt(text)
        and not is_subagent_notification_prompt(text)
        and not is_automation_prompt(text)
        and not is_thread_delegation_prompt(text)
        and not is_codex_internal_context_prompt(text)
    )


def remove_embedded_image_blocks(text: str) -> str:
    without_named_blocks = re.sub(r"\n?<image\b.*?</image>\s*", "\n", text, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\n?!\[[^\]]*]\([^)]*\)\s*", "\n", without_named_blocks).strip()


def pure_user_text_from_prompt(text: str) -> str:
    if (
        is_internal_context_prompt(text)
        or is_subagent_notification_prompt(text)
        or is_automation_prompt(text)
        or is_thread_delegation_prompt(text)
        or is_codex_internal_context_prompt(text)
    ):
        return ""
    cleaned_text = remove_embedded_image_blocks(text)
    marker_match = re.search(r"(?im)^##\s*My request for Codex:\s*$", cleaned_text)
    if marker_match:
        return remove_embedded_image_blocks(cleaned_text[marker_match.end() :]).strip()
    if cleaned_text.lstrip().startswith("# In app browser:") or cleaned_text.lstrip().startswith("# Files mentioned by the user:"):
        return ""
    return cleaned_text.strip()


def classify_prompt_record(text: str) -> dict[str, Any]:
    prefix = text.lstrip()[:5000]
    pure_text = pure_user_text_from_prompt(text)
    if is_subagent_notification_prompt(text):
        return {
            "sourceType": "subagent",
            "sourceLabel": "子 agent",
            "visibleByDefault": False,
            "pureText": pure_text,
            "pureCharacterCount": len(pure_text),
            "hasPureText": bool(pure_text),
        }
    if is_automation_prompt(text):
        return {
            "sourceType": "automation",
            "sourceLabel": "自动化任务",
            "visibleByDefault": False,
            "pureText": pure_text,
            "pureCharacterCount": len(pure_text),
            "hasPureText": bool(pure_text),
        }
    if is_thread_delegation_prompt(text):
        return {
            "sourceType": "delegation",
            "sourceLabel": "线程转发",
            "visibleByDefault": False,
            "pureText": pure_text,
            "pureCharacterCount": len(pure_text),
            "hasPureText": bool(pure_text),
        }
    if is_codex_internal_context_prompt(text):
        return {
            "sourceType": "goal",
            "sourceLabel": "续跑目标上下文",
            "visibleByDefault": False,
            "pureText": pure_text,
            "pureCharacterCount": len(pure_text),
            "hasPureText": bool(pure_text),
        }
    if is_internal_context_prompt(text):
        return {
            "sourceType": "internal",
            "sourceLabel": "内部上下文",
            "visibleByDefault": False,
            "pureText": pure_text,
            "pureCharacterCount": len(pure_text),
            "hasPureText": bool(pure_text),
        }
    if prefix.startswith("# In app browser:"):
        return {
            "sourceType": "browser",
            "sourceLabel": "浏览器上下文",
            "visibleByDefault": True,
            "pureText": pure_text,
            "pureCharacterCount": len(pure_text),
            "hasPureText": bool(pure_text),
        }
    if prefix.startswith("# Files mentioned by the user:"):
        return {
            "sourceType": "attachment",
            "sourceLabel": "附件上下文",
            "visibleByDefault": True,
            "pureText": pure_text,
            "pureCharacterCount": len(pure_text),
            "hasPureText": bool(pure_text),
        }
    return {
        "sourceType": "user",
        "sourceLabel": "用户输入",
        "visibleByDefault": True,
        "pureText": pure_text,
        "pureCharacterCount": len(pure_text),
        "hasPureText": bool(pure_text),
    }


def prompt_source_counts(prompts: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for prompt in prompts:
        source_type = str(prompt.get("sourceType") or "unknown")
        counts[source_type] = counts.get(source_type, 0) + 1
    return counts


def filter_prompts_for_scope(prompts: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    normalized_scope = (scope or "pure").strip().lower()
    if normalized_scope in {"pure", "text", "user_text", "user-text"}:
        return [prompt for prompt in prompts if str(prompt.get("pureText") or "").strip()]
    if normalized_scope == "all":
        return prompts
    if normalized_scope in {"automation", "automations", "heartbeat", "heartbeats"}:
        return [prompt for prompt in prompts if prompt.get("sourceType") == "automation"]
    if normalized_scope in {"delegation", "delegations", "thread_delegation", "thread-delegation", "handoff", "handoffs"}:
        return [prompt for prompt in prompts if prompt.get("sourceType") == "delegation"]
    if normalized_scope in {"with_agents", "with-agent", "with_agents_and_user", "agents"}:
        return [prompt for prompt in prompts if prompt.get("visibleByDefault") is not False or prompt.get("sourceType") == "subagent"]
    return [prompt for prompt in prompts if prompt.get("visibleByDefault") is not False]


def prompt_text_for_scope(prompt: dict[str, Any], scope: str) -> str:
    normalized_scope = (scope or "pure").strip().lower()
    if normalized_scope in {"pure", "text", "user_text", "user-text"}:
        return str(prompt.get("pureText") or "").strip()
    return str(prompt.get("text") or "").strip()


def event_message_text(payload: Any) -> str:
    if isinstance(payload, dict):
        text = payload.get("message")
        if isinstance(text, str):
            return text.strip()
        text = payload.get("text")
        if isinstance(text, str):
            return text.strip()
    if isinstance(payload, str):
        return payload.strip()
    return ""


def rollout_display_integrity(rollout_path_text: str) -> dict[str, Any]:
    normalized_path = normalize_path_text(rollout_path_text)
    path = Path(normalized_path) if normalized_path else None
    if path and path.exists():
        stat = path.stat()
        cached = rollout_display_cache.get(normalized_path)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return copy.deepcopy(cached[2])
        parse_rollout_stats(normalized_path)
        cached = rollout_display_cache.get(normalized_path)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return copy.deepcopy(cached[2])
    result: dict[str, Any] = {
        "status": "not_scanned",
        "responseUserMessages": 0,
        "responseAssistantMessages": 0,
        "visibleUserMessages": 0,
        "visibleAgentMessages": 0,
        "topLevelUserMessages": 0,
        "eventUserMessages": 0,
        "eventAgentMessages": 0,
        "parseErrors": 0,
    }
    if not path or not path.exists():
        result["status"] = "missing_rollout_file"
        return result

    with path.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                result["parseErrors"] += 1
                continue
            item_type = item.get("type")
            payload = item.get("payload") or {}
            if item_type == "user_message":
                text = text_from_user_message_payload(payload)
                if is_real_user_prompt(text):
                    result["topLevelUserMessages"] += 1
                    result["visibleUserMessages"] += 1
            elif item_type == "event_msg" and isinstance(payload, dict):
                payload_type = str(payload.get("type") or "")
                message = event_message_text(payload)
                if payload_type == "user_message" and is_real_user_prompt(message):
                    result["eventUserMessages"] += 1
                    result["visibleUserMessages"] += 1
                elif payload_type == "agent_message" and message:
                    result["eventAgentMessages"] += 1
                    result["visibleAgentMessages"] += 1
            elif item_type == "response_item" and isinstance(payload, dict) and payload.get("type") == "message":
                user_text = text_from_response_role_payload(payload, "user")
                if is_real_user_prompt(user_text):
                    result["responseUserMessages"] += 1
                assistant_text = text_from_response_role_payload(payload, "assistant")
                if assistant_text:
                    result["responseAssistantMessages"] += 1

    response_chat_messages = int(result["responseUserMessages"]) + int(result["responseAssistantMessages"])
    visible_chat_messages = int(result["visibleUserMessages"]) + int(result["visibleAgentMessages"])
    if response_chat_messages == 0:
        result["status"] = "ok" if int(result["visibleUserMessages"]) > 0 else "empty"
    elif int(result["responseUserMessages"]) > 0 and int(result["visibleUserMessages"]) == 0:
        result["status"] = "missing_visible_event_stream"
    elif int(result["responseAssistantMessages"]) > 0 and int(result["visibleAgentMessages"]) == 0:
        result["status"] = "missing_visible_event_stream"
    elif response_chat_messages >= 10 and visible_chat_messages < max(2, response_chat_messages // 2):
        result["status"] = "sparse_visible_event_stream"
    else:
        result["status"] = "ok"
    stat = path.stat()
    rollout_display_cache[normalized_path] = (stat.st_size, stat.st_mtime_ns, copy.deepcopy(result))
    return result


def first_real_user_prompt_from_rollout(rollout_path_text: str, limit: int = 4000) -> str:
    normalized_path = normalize_path_text(rollout_path_text)
    if not normalized_path or not Path(normalized_path).exists():
        return ""

    compacted_fallback = ""
    with Path(normalized_path).open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_type = item.get("type")
            payload = item.get("payload") or {}
            text = ""
            if item_type == "user_message":
                text = text_from_user_message_payload(payload)
            elif item_type == "response_item":
                text = text_from_response_message_payload(payload)
            elif item_type == "compacted" and isinstance(payload, dict):
                for history_item in payload.get("replacement_history") or []:
                    history_text = text_from_compacted_history_message(history_item)
                    if is_real_user_prompt(history_text):
                        compacted_fallback = compacted_fallback or history_text
                        break
            if is_real_user_prompt(text):
                return text[:limit]
    return compacted_fallback[:limit]


def apply_rollout_user_metadata_to_thread_row(row: dict[str, Any]) -> dict[str, str]:
    first_user_message = str(row.get("first_user_message") or "").strip()
    preview = str(row.get("preview") or "").strip()
    rollout_first_user_message = first_real_user_prompt_from_rollout(str(row.get("rollout_path") or ""))
    resolved_first_user_message = first_user_message if is_real_user_prompt(first_user_message) else rollout_first_user_message
    resolved_preview = preview or resolved_first_user_message
    return {
        "first_user_message": resolved_first_user_message,
        "preview": resolved_preview,
    }


def truncate_text(value: str, limit: int = 4000) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit] + "\n...[truncated]", True


def stringify_log_value(value: Any, limit: int = 4000) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, str):
        return truncate_text(value, limit)
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except TypeError:
        text = str(value)
    return truncate_text(text, limit)


def first_log_text_from_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [first_log_text_from_value(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "message", "error", "stderr", "stdout", "output", "content", "result"):
            text = first_log_text_from_value(value.get(key))
            if text:
                return text
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return str(value)


error_text_pattern = re.compile(
    r"\b(error|exception|traceback|failed|failure|fatal|unauthorized|forbidden|timeout|timed out|refused|denied|crash|panic)\b",
    flags=re.IGNORECASE,
)
app_error_text_pattern = re.compile(
    r"\b(exception|traceback|failed|failure|fatal|unauthorized|forbidden|refused|denied|crash|panic|internal server error)\b",
    flags=re.IGNORECASE,
)
warning_text_pattern = re.compile(r"\b(warn|warning|retry|rate limit|deprecated)\b", flags=re.IGNORECASE)
http_error_pattern = re.compile(r"\b(?:HTTP[/ ]?\d(?:\.\d)?\s+|status(?:_code)?[=: ]+|code[=: ]+)([45]\d\d)\b", flags=re.IGNORECASE)


def log_severity(text: str) -> str:
    if http_error_pattern.search(text) or error_text_pattern.search(text):
        return "error"
    if warning_text_pattern.search(text):
        return "warning"
    return "info"


def app_log_severity(text: str) -> str:
    if http_error_pattern.search(text) or app_error_text_pattern.search(text):
        return "error"
    if warning_text_pattern.search(text) or re.search(r"\b(timeout|timed out)\b", text, flags=re.IGNORECASE):
        return "warning"
    return "info"


def classify_log_item(item: dict[str, Any], raw_line: str, line_number: int) -> dict[str, Any]:
    item_type = str(item.get("type") or "unknown")
    payload = item.get("payload")
    timestamp = item.get("timestamp")
    kind = item_type
    label = item_type
    message = ""
    payload_type = ""
    role = ""

    if item_type == "session_meta" and isinstance(payload, dict):
        kind = "session"
        label = str(payload.get("id") or "session_meta")
        message = f"cwd: {payload.get('cwd') or ''}"
    elif item_type == "user_message":
        kind = "user"
        label = "user"
        message = first_log_text_from_value(payload)
    elif item_type == "event_msg":
        kind = "event"
        label = "event"
        message = first_log_text_from_value(payload)
    elif item_type == "turn_context":
        kind = "context"
        label = "turn_context"
        message = first_log_text_from_value(payload)
    elif item_type == "compacted":
        kind = "compacted"
        label = "compacted"
        message = first_log_text_from_value(payload)
    elif item_type == "response_item" and isinstance(payload, dict):
        payload_type = str(payload.get("type") or "")
        role = str(payload.get("role") or "")
        if payload_type == "function_call":
            kind = "request"
            label = str(payload.get("name") or payload.get("call_id") or "tool_call")
            message = first_log_text_from_value(payload.get("arguments") or payload)
        elif payload_type == "function_call_output":
            kind = "tool_output"
            label = str(payload.get("call_id") or "tool_output")
            message = first_log_text_from_value(payload.get("output") or payload.get("content") or payload)
        elif payload_type == "message":
            kind = role or "message"
            label = role or "message"
            message = first_log_text_from_value(payload.get("content") or payload)
        elif payload_type == "reasoning":
            kind = "reasoning"
            label = "reasoning"
            message = first_log_text_from_value(payload.get("summary") or payload.get("content") or payload)
        else:
            kind = "response"
            label = payload_type or "response_item"
            message = first_log_text_from_value(payload)
    else:
        message = first_log_text_from_value(payload if payload is not None else item)

    message_preview, message_truncated = truncate_text(re.sub(r"\s+", " ", message).strip(), 1200)
    raw_preview, raw_truncated = truncate_text(raw_line.rstrip("\n"), 12000)
    if kind in {"user", "assistant", "developer", "system", "reasoning", "session", "context", "compacted"}:
        severity = "info"
    else:
        severity = log_severity("\n".join([item_type, payload_type, role, label, message, raw_line[:2000]]))
    return {
        "source": "rollout_jsonl",
        "lineNumber": line_number,
        "appLogId": None,
        "timestamp": timestamp,
        "timestampMs": timestamp_ms_from_iso_text(timestamp),
        "type": item_type,
        "payloadType": payload_type,
        "role": role,
        "kind": kind,
        "label": label,
        "severity": severity,
        "message": message_preview,
        "messageTruncated": message_truncated,
        "rawLine": raw_preview,
        "rawLineTruncated": raw_truncated,
    }


def log_entry_matches_kind(entry: dict[str, Any], kind_filter: str) -> bool:
    if kind_filter == "all":
        return True
    if kind_filter == "error":
        return entry["severity"] == "error"
    if kind_filter == "failure":
        return entry["severity"] in {"error", "warning"}
    if kind_filter == "tool":
        return entry["kind"] in {"request", "tool_output"}
    return entry["kind"] == kind_filter


allowed_log_kinds = {
    "all",
    "error",
    "failure",
    "request",
    "tool",
    "tool_output",
    "event",
    "user",
    "assistant",
    "session",
    "context",
    "reasoning",
    "compacted",
    "response",
    "parse_error",
    "app_log",
}


def validate_log_kind(kind_filter: str) -> str:
    normalized_kind = (kind_filter or "all").strip().lower()
    if normalized_kind not in allowed_log_kinds:
        raise ValueError(f"unsupported log kind: {kind_filter}")
    return normalized_kind


def timestamp_ms_from_iso_text(timestamp_text: Any) -> int | None:
    if not timestamp_text:
        return None
    try:
        normalized_timestamp = str(timestamp_text).replace("Z", "+00:00")
        return int(datetime_module.datetime.fromisoformat(normalized_timestamp).timestamp() * 1000)
    except ValueError:
        return None


def timestamp_text_from_app_log(row: sqlite3.Row) -> tuple[str, int]:
    timestamp_ms = int(row["ts"]) * 1000 + int(int(row["ts_nanos"]) / 1_000_000)
    timestamp_text = datetime_module.datetime.fromtimestamp(
        int(row["ts"]) + int(row["ts_nanos"]) / 1_000_000_000,
        datetime_module.UTC,
    ).isoformat()
    return timestamp_text, timestamp_ms


def classify_app_log_row(row: sqlite3.Row) -> dict[str, Any]:
    feedback_body = str(row["feedback_log_body"] or "")
    target = str(row["target"] or "")
    level = str(row["level"] or "")
    searchable_text = "\n".join([level, target, feedback_body])
    level_upper = level.upper()
    if level_upper == "ERROR":
        severity = "error"
    elif level_upper in {"WARN", "WARNING"}:
        severity = "warning"
    else:
        severity = app_log_severity(searchable_text)
    lower_text = searchable_text.lower()
    if "request" in lower_text or "rpc.method" in lower_text or "responses_websocket" in lower_text:
        kind = "request"
    elif "event.name" in lower_text or "websocket event" in lower_text:
        kind = "event"
    else:
        kind = "app_log"
    message_preview, message_truncated = truncate_text(re.sub(r"\s+", " ", feedback_body).strip(), 1200)
    raw_payload = {
        "id": row["id"],
        "ts": row["ts"],
        "ts_nanos": row["ts_nanos"],
        "level": level,
        "target": target,
        "feedback_log_body": feedback_body,
        "module_path": row["module_path"],
        "file": row["file"],
        "line": row["line"],
        "thread_id": row["thread_id"],
        "process_uuid": row["process_uuid"],
        "estimated_bytes": row["estimated_bytes"],
    }
    raw_preview, raw_truncated = truncate_text(json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":"), default=str), 12000)
    timestamp_text, timestamp_ms = timestamp_text_from_app_log(row)
    return {
        "source": "app_sqlite",
        "lineNumber": None,
        "appLogId": int(row["id"]),
        "timestamp": timestamp_text,
        "timestampMs": timestamp_ms,
        "type": "app_log",
        "payloadType": "",
        "role": "",
        "kind": kind,
        "label": target or level or "app_log",
        "severity": severity,
        "message": message_preview,
        "messageTruncated": message_truncated,
        "rawLine": raw_preview,
        "rawLineTruncated": raw_truncated,
        "level": level,
        "target": target,
        "modulePath": row["module_path"] or "",
        "file": row["file"] or "",
        "fileLine": row["line"],
        "processUuid": row["process_uuid"] or "",
    }


def empty_log_summary() -> dict[str, Any]:
    return {
        "lineCount": 0,
        "parseErrors": 0,
        "byKind": {},
        "bySeverity": {"info": 0, "warning": 0, "error": 0},
        "sources": {"rollout_jsonl": 0, "app_sqlite": 0},
    }


def add_log_summary_entry(summary: dict[str, Any], entry: dict[str, Any]) -> None:
    summary["byKind"][entry["kind"]] = int(summary["byKind"].get(entry["kind"], 0)) + 1
    summary["bySeverity"][entry["severity"]] = int(summary["bySeverity"].get(entry["severity"], 0)) + 1
    source = str(entry.get("source") or "unknown")
    sources = summary.setdefault("sources", {})
    sources[source] = int(sources.get(source, 0)) + 1


def merge_log_summaries(*summaries: dict[str, Any]) -> dict[str, Any]:
    merged = empty_log_summary()
    for summary in summaries:
        merged["lineCount"] += int(summary.get("lineCount") or 0)
        merged["parseErrors"] += int(summary.get("parseErrors") or 0)
        for key, value in (summary.get("byKind") or {}).items():
            merged["byKind"][key] = int(merged["byKind"].get(key, 0)) + int(value)
        for key, value in (summary.get("bySeverity") or {}).items():
            merged["bySeverity"][key] = int(merged["bySeverity"].get(key, 0)) + int(value)
        for key, value in (summary.get("sources") or {}).items():
            merged["sources"][key] = int(merged["sources"].get(key, 0)) + int(value)
    return merged


def read_rollout_thread_logs(
    thread_id: str,
    rollout_path: Path,
    offset: int = 0,
    limit: int = 100,
    kind_filter: str = "all",
    search_text: str = "",
) -> dict[str, Any]:
    if not rollout_path.exists():
        raise FileNotFoundError(str(rollout_path))
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(500, int(limit)))
    normalized_kind = validate_log_kind(kind_filter)
    search_marker = search_text.strip().lower()
    entries: list[dict[str, Any]] = []
    summary = empty_log_summary()
    matched_entries = 0
    with rollout_path.open("r", encoding="utf-8", errors="replace") as file:
        for line_number, line in enumerate(file, 1):
            summary["lineCount"] += 1
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                summary["parseErrors"] += 1
                raw_preview, raw_truncated = truncate_text(line.rstrip("\n"), 12000)
                entry = {
                    "source": "rollout_jsonl",
                    "lineNumber": line_number,
                    "appLogId": None,
                    "timestamp": None,
                    "timestampMs": None,
                    "type": "parse_error",
                    "payloadType": "",
                    "role": "",
                    "kind": "parse_error",
                    "label": "JSON parse error",
                    "severity": "error",
                    "message": str(error),
                    "messageTruncated": False,
                    "rawLine": raw_preview,
                    "rawLineTruncated": raw_truncated,
                }
            else:
                entry = classify_log_item(item, line, line_number)
            add_log_summary_entry(summary, entry)
            if not log_entry_matches_kind(entry, normalized_kind):
                continue
            searchable_text = "\n".join(
                str(entry.get(key) or "")
                for key in ("type", "payloadType", "role", "kind", "label", "severity", "message", "rawLine")
            ).lower()
            if search_marker and search_marker not in searchable_text:
                continue
            if matched_entries >= safe_offset and len(entries) < safe_limit:
                entries.append(entry)
            matched_entries += 1
    return {
        "threadId": thread_id,
        "source": "rollout",
        "rolloutPath": str(rollout_path),
        "appLogPath": "",
        "offset": safe_offset,
        "limit": safe_limit,
        "kind": normalized_kind,
        "search": search_text,
        "matchedEntries": matched_entries,
        "hasMore": safe_offset + len(entries) < matched_entries,
        "entries": entries,
        "summary": summary,
    }


def app_log_where_clause(thread_id: str, normalized_kind: str, search_text: str) -> tuple[str, list[Any]]:
    clauses = ["thread_id = ?"]
    params: list[Any] = [thread_id]
    body_text = "lower(coalesce(feedback_log_body, '') || ' ' || coalesce(target, '') || ' ' || coalesce(module_path, '') || ' ' || coalesce(file, '') || ' ' || coalesce(level, ''))"
    if normalized_kind == "error":
        clauses.append(
            f"(upper(level) = 'ERROR' OR {body_text} LIKE '%http 4%' OR {body_text} LIKE '%http 5%' OR {body_text} LIKE '%http/1.1 4%' OR {body_text} LIKE '%http/1.1 5%' OR {body_text} LIKE '%status=4%' OR {body_text} LIKE '%status=5%' OR {body_text} LIKE '%status_code=4%' OR {body_text} LIKE '%status_code=5%' OR {body_text} LIKE '%internal server error%' OR {body_text} LIKE '%exception%' OR {body_text} LIKE '%failed%' OR {body_text} LIKE '%failure%' OR {body_text} LIKE '%unauthorized%' OR {body_text} LIKE '%forbidden%' OR {body_text} LIKE '%panic%')"
        )
    elif normalized_kind == "failure":
        clauses.append(
            f"(upper(level) IN ('ERROR', 'WARN', 'WARNING') OR {body_text} LIKE '%http 4%' OR {body_text} LIKE '%http 5%' OR {body_text} LIKE '%http/1.1 4%' OR {body_text} LIKE '%http/1.1 5%' OR {body_text} LIKE '%status=4%' OR {body_text} LIKE '%status=5%' OR {body_text} LIKE '%status_code=4%' OR {body_text} LIKE '%status_code=5%' OR {body_text} LIKE '%internal server error%' OR {body_text} LIKE '%exception%' OR {body_text} LIKE '%failed%' OR {body_text} LIKE '%failure%' OR {body_text} LIKE '%timeout%' OR {body_text} LIKE '%retry%' OR {body_text} LIKE '%rate limit%' OR {body_text} LIKE '%unauthorized%' OR {body_text} LIKE '%forbidden%' OR {body_text} LIKE '%panic%')"
        )
    elif normalized_kind in {"request", "tool"}:
        clauses.append(
            f"({body_text} LIKE '%request%' OR {body_text} LIKE '%rpc.method%' OR {body_text} LIKE '%stream_request%' OR {body_text} LIKE '%responses_websocket%')"
        )
    elif normalized_kind == "event":
        clauses.append(f"({body_text} LIKE '%event.name%' OR {body_text} LIKE '%websocket event%')")
    elif normalized_kind in {"tool_output", "user", "assistant", "session", "context", "reasoning", "compacted", "response", "parse_error"}:
        clauses.append("0 = 1")
    search_marker = search_text.strip().lower()
    if search_marker:
        clauses.append(f"{body_text} LIKE ?")
        params.append(f"%{search_marker}%")
    return " AND ".join(clauses), params


def read_app_thread_logs(
    paths: CodexPaths,
    thread_id: str,
    offset: int = 0,
    limit: int = 100,
    kind_filter: str = "all",
    search_text: str = "",
    missing_ok: bool = False,
) -> dict[str, Any]:
    app_log_path = paths.codex_home_path / "logs_2.sqlite"
    if not app_log_path.exists():
        if missing_ok:
            return {
                "threadId": thread_id,
                "source": "app",
                "rolloutPath": "",
                "appLogPath": str(app_log_path),
                "offset": max(0, int(offset)),
                "limit": max(1, min(500, int(limit))),
                "kind": (kind_filter or "all").strip().lower(),
                "search": search_text,
                "matchedEntries": 0,
                "hasMore": False,
                "entries": [],
                "summary": empty_log_summary(),
            }
        raise FileNotFoundError(str(app_log_path))
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(500, int(limit)))
    normalized_kind = validate_log_kind(kind_filter)
    where_clause, params = app_log_where_clause(thread_id, normalized_kind, search_text)
    entries: list[dict[str, Any]] = []
    summary = empty_log_summary()
    with sqlite3.connect(f"file:{app_log_path}?mode=ro", uri=True, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        matched_entries = int(connection.execute(f"SELECT COUNT(*) AS count FROM logs WHERE {where_clause}", params).fetchone()["count"] or 0)
        severity_rows = connection.execute(
            f"SELECT upper(level) AS level, COUNT(*) AS count FROM logs WHERE {where_clause} GROUP BY upper(level)",
            params,
        ).fetchall()
        rows = connection.execute(
            f"""
            SELECT id, ts, ts_nanos, level, target, feedback_log_body, module_path, file, line, thread_id, process_uuid, estimated_bytes
            FROM logs
            WHERE {where_clause}
            ORDER BY ts DESC, ts_nanos DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, safe_limit, safe_offset],
        ).fetchall()
    for row in rows:
        entry = classify_app_log_row(row)
        entries.append(entry)
        add_log_summary_entry(summary, entry)
    summary["lineCount"] = matched_entries
    summary["sources"]["app_sqlite"] = matched_entries
    summary["bySeverity"] = {"info": 0, "warning": 0, "error": 0}
    for severity_row in severity_rows:
        level = str(severity_row["level"] or "")
        count = int(severity_row["count"] or 0)
        if level == "ERROR":
            summary["bySeverity"]["error"] += count
        elif level in {"WARN", "WARNING"}:
            summary["bySeverity"]["warning"] += count
    counted_failure_levels = summary["bySeverity"]["error"] + summary["bySeverity"]["warning"]
    if normalized_kind == "error":
        summary["bySeverity"]["error"] = max(summary["bySeverity"]["error"], matched_entries)
    elif normalized_kind == "failure":
        summary["bySeverity"]["warning"] += max(0, matched_entries - counted_failure_levels)
    else:
        summary["bySeverity"]["info"] = max(0, matched_entries - counted_failure_levels)
    return {
        "threadId": thread_id,
        "source": "app",
        "rolloutPath": "",
        "appLogPath": str(app_log_path),
        "offset": safe_offset,
        "limit": safe_limit,
        "kind": normalized_kind,
        "search": search_text,
        "matchedEntries": matched_entries,
        "hasMore": safe_offset + len(entries) < matched_entries,
        "entries": entries,
        "summary": summary,
    }


def read_thread_logs(
    codex_home_text: str | None,
    thread_id: str,
    offset: int = 0,
    limit: int = 100,
    kind_filter: str = "all",
    search_text: str = "",
    source_filter: str = "all",
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    row = fetch_thread_row(paths, thread_id)
    if row is None:
        raise KeyError(thread_id)
    rollout_path = Path(normalize_path_text(row.get("rollout_path")))
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(500, int(limit)))
    normalized_kind = validate_log_kind(kind_filter)
    normalized_source = (source_filter or "all").strip().lower()
    allowed_sources = {"all", "rollout", "app"}
    if normalized_source not in allowed_sources:
        raise ValueError(f"unsupported log source: {source_filter}")
    if normalized_source == "rollout":
        return read_rollout_thread_logs(thread_id, rollout_path, safe_offset, safe_limit, normalized_kind, search_text)
    if normalized_source == "app":
        return read_app_thread_logs(paths, thread_id, safe_offset, safe_limit, normalized_kind, search_text)
    source_limit = safe_offset + safe_limit
    rollout_logs = read_rollout_thread_logs(thread_id, rollout_path, 0, source_limit, normalized_kind, search_text)
    app_logs = read_app_thread_logs(paths, thread_id, 0, source_limit, normalized_kind, search_text, missing_ok=True)
    combined_entries = [*rollout_logs["entries"], *app_logs["entries"]]
    combined_entries.sort(
        key=lambda entry: (
            int(entry.get("timestampMs") or 0),
            int(entry.get("appLogId") or 0),
            int(entry.get("lineNumber") or 0),
        ),
        reverse=True,
    )
    page_entries = combined_entries[safe_offset:safe_offset + safe_limit]
    matched_entries = int(rollout_logs["matchedEntries"] or 0) + int(app_logs["matchedEntries"] or 0)
    return {
        "threadId": thread_id,
        "source": "all",
        "rolloutPath": str(rollout_path),
        "appLogPath": str(paths.codex_home_path / "logs_2.sqlite"),
        "offset": safe_offset,
        "limit": safe_limit,
        "kind": normalized_kind,
        "search": search_text,
        "matchedEntries": matched_entries,
        "hasMore": safe_offset + len(page_entries) < matched_entries,
        "entries": page_entries,
        "summary": merge_log_summaries(rollout_logs["summary"], app_logs["summary"]),
    }


def get_thread_daily_token_usage(codex_home_text: str | None, thread_id: str, sidebar_limit: int = 50) -> dict[str, Any]:
    snapshot = build_snapshot(codex_home_text=codex_home_text, sidebar_limit=sidebar_limit)
    thread = next((item for item in snapshot["threads"] if item["id"] == thread_id), None)
    if thread is None:
        raise KeyError(thread_id)
    return build_thread_daily_token_usage(thread, snapshot["threads"])


def get_thread_action_preview_record(codex_home_text: str | None, thread_id: str) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    row = fetch_thread_row(paths, thread_id)
    if row is None:
        raise KeyError(thread_id)
    rollout_stat = stat_file(row.get("rollout_path"))
    title = str(row.get("title") or row.get("first_user_message") or row.get("preview") or "(untitled)")
    return {
        "id": str(row["id"]),
        "title": title,
        "projectPath": normalize_path_text(row.get("cwd")),
        "rolloutPath": rollout_stat["path"],
        "fileExists": bool(rollout_stat["exists"]),
        "fileSizeBytes": int(rollout_stat["sizeBytes"] or 0),
        "fileModifiedAtMs": rollout_stat["modifiedAtMs"],
        "archived": bool(row.get("archived")),
        "hasUserEvent": bool(row.get("has_user_event")),
        "createdAtMs": timestamp_ms_from_row(row, "created"),
        "updatedAtMs": timestamp_ms_from_row(row, "updated"),
    }


def get_thread_detail(
    codex_home_text: str | None,
    thread_id: str,
    sidebar_limit: int = 50,
    include_daily_token_usage: bool = True,
) -> dict[str, Any]:
    snapshot = build_snapshot(codex_home_text=codex_home_text, sidebar_limit=sidebar_limit)
    thread = next((item for item in snapshot["threads"] if item["id"] == thread_id), None)
    if thread is None:
        raise KeyError(thread_id)
    paths = resolve_codex_paths(codex_home_text)
    row = fetch_thread_row(paths, thread_id)
    rollout_display = rollout_display_integrity(thread["rolloutPath"])
    thread.update(
        {
            "rolloutDisplayStatus": rollout_display.get("status") or "not_scanned",
            "rolloutDisplayResponseUserMessages": int(rollout_display.get("responseUserMessages") or 0),
            "rolloutDisplayResponseAssistantMessages": int(rollout_display.get("responseAssistantMessages") or 0),
            "rolloutDisplayVisibleUserMessages": int(rollout_display.get("visibleUserMessages") or 0),
            "rolloutDisplayVisibleAgentMessages": int(rollout_display.get("visibleAgentMessages") or 0),
            "rolloutDisplayEventUserMessages": int(rollout_display.get("eventUserMessages") or 0),
            "rolloutDisplayEventAgentMessages": int(rollout_display.get("eventAgentMessages") or 0),
        }
    )
    rollout_stats = parse_rollout_stats(thread["rolloutPath"])
    detail = {
        "thread": thread,
        "sqliteRow": row,
        "rolloutStats": rollout_stats,
        "backups": list_backups(codex_home_text, thread_id=thread_id),
    }
    if include_daily_token_usage:
        detail["dailyTokenUsage"] = build_thread_daily_token_usage(thread, snapshot["threads"])
    return detail


def backup_root_path() -> Path:
    configured_backup_root = os.environ.get("CODEX_HOME_MANAGER_BACKUP_ROOT")
    if not configured_backup_root:
        legacy_backup_root_key = "CODEX_" + "THREAD" + "_MANAGER_BACKUP_ROOT"
        configured_backup_root = os.environ.get(legacy_backup_root_key)
    if configured_backup_root:
        return Path(configured_backup_root).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parents[1] / "data" / "backups"


def backup_directory_path(backup_id: str) -> Path:
    normalized_backup_id = str(backup_id or "").strip()
    if (
        not normalized_backup_id
        or normalized_backup_id in {".", ".."}
        or Path(normalized_backup_id).name != normalized_backup_id
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", normalized_backup_id)
    ):
        raise ValueError("invalid backup id")
    backup_root = backup_root_path().resolve(strict=False)
    backup_directory = (backup_root / normalized_backup_id).resolve(strict=False)
    if backup_directory.parent != backup_root:
        raise ValueError("invalid backup id")
    return backup_directory


def backup_manifest_path(backup_id: str) -> Path:
    return backup_directory_path(backup_id) / "manifest.json"


def codex_home_binding_key(codex_home_path: Path) -> str:
    canonical_home = os.path.normcase(str(codex_home_path.expanduser().resolve(strict=False)))
    return hashlib.sha256(canonical_home.encode("utf-8")).hexdigest()


def backup_manifest_key(create: bool) -> bytes | None:
    root_path = backup_root_path()
    key_path = root_path / ".backup-manifest.key"
    if key_path.is_file():
        key = key_path.read_bytes()
        if len(key) != 32:
            raise ValueError("backup manifest integrity key is invalid")
        return key
    if not create:
        return None
    root_path.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    try:
        with key_path.open("xb") as key_file:
            key_file.write(key)
            key_file.flush()
            os.fsync(key_file.fileno())
        try:
            key_path.chmod(0o600)
        except OSError:
            pass
        return key
    except FileExistsError:
        stored_key = key_path.read_bytes()
        if len(stored_key) != 32:
            raise ValueError("backup manifest integrity key is invalid")
        return stored_key


def backup_file_integrity_records(backup_directory: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for file_path in sorted(backup_directory.rglob("*"), key=lambda candidate: candidate.as_posix().casefold()):
        if not file_path.is_file() or file_path.name == "manifest.json" or file_path.name.endswith(".writing"):
            continue
        relative_path = file_path.relative_to(backup_directory).as_posix()
        records[relative_path] = {
            "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest(),
            "sizeBytes": file_path.stat().st_size,
        }
    return records


def backup_manifest_signature_payload(manifest: dict[str, Any], files: dict[str, dict[str, Any]]) -> bytes:
    unsigned_manifest = copy.deepcopy(manifest)
    unsigned_manifest.pop("integrity", None)
    return json.dumps(
        {"manifest": unsigned_manifest, "files": files},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def write_sealed_backup_manifest(manifest_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    backup_directory = manifest_path.parent.resolve(strict=False)
    sealed_manifest = copy.deepcopy(manifest)
    sealed_manifest.pop("integrity", None)
    if str(sealed_manifest.get("backupId") or "") != backup_directory.name:
        raise ValueError("backup manifest integrity check failed: backup id does not match its directory")
    codex_home_text = normalize_path_text(sealed_manifest.get("codexHome"))
    if not codex_home_text:
        raise ValueError("backup manifest integrity check failed: Codex Home binding is missing")
    sealed_manifest["schemaVersion"] = 2
    sealed_manifest["codexHomeKey"] = codex_home_binding_key(Path(codex_home_text))
    files = backup_file_integrity_records(backup_directory)
    key = backup_manifest_key(create=True)
    assert key is not None
    signature = hmac.new(key, backup_manifest_signature_payload(sealed_manifest, files), hashlib.sha256).hexdigest()
    sealed_manifest["integrity"] = {
        "version": 1,
        "algorithm": "hmac-sha256",
        "files": files,
        "hmacSha256": signature,
    }
    temporary_path = manifest_path.with_name(f"{manifest_path.name}.{os.getpid()}.writing")
    temporary_path.write_text(json.dumps(sealed_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary_path, manifest_path)
    return sealed_manifest


def read_verified_backup_manifest(manifest_path: Path, *, allow_new_files: bool = False) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
        raise ValueError(f"backup manifest integrity check failed: {error}") from error
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "hmac-sha256":
        raise ValueError("backup manifest integrity check failed: seal is missing")
    if str(manifest.get("backupId") or "") != manifest_path.parent.name:
        raise ValueError("backup manifest integrity check failed: backup id mismatch")
    codex_home_text = normalize_path_text(manifest.get("codexHome"))
    if not codex_home_text or manifest.get("codexHomeKey") != codex_home_binding_key(Path(codex_home_text)):
        raise ValueError("backup manifest integrity check failed: Codex Home binding mismatch")
    stored_files = integrity.get("files")
    if not isinstance(stored_files, dict):
        raise ValueError("backup manifest integrity check failed: file inventory is missing")
    current_files = backup_file_integrity_records(manifest_path.parent)
    if allow_new_files:
        for relative_path, expected_record in stored_files.items():
            if current_files.get(relative_path) != expected_record:
                raise ValueError(f"backup manifest integrity check failed: {relative_path}")
    elif current_files != stored_files:
        raise ValueError("backup manifest integrity check failed: backup file inventory changed")
    key = backup_manifest_key(create=False)
    if key is None:
        raise ValueError("backup manifest integrity check failed: key is missing")
    expected_signature = hmac.new(
        key,
        backup_manifest_signature_payload(manifest, stored_files),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(str(integrity.get("hmacSha256") or ""), expected_signature):
        raise ValueError("backup manifest integrity check failed: HMAC mismatch")
    return manifest


def path_is_within(path: Path, root_path: Path) -> bool:
    try:
        path.expanduser().resolve(strict=False).relative_to(root_path.expanduser().resolve(strict=False))
        return True
    except ValueError:
        return False


def validate_restore_manifest_paths(manifest: dict[str, Any], manifest_path: Path, paths: CodexPaths) -> None:
    backup_directory = manifest_path.parent.resolve(strict=False)
    backup_source_fields = (
        "databaseBackupPath",
        "globalStateBackupPath",
        "globalStateBakBackupPath",
        "configBackupPath",
        "managedConfigBackupPath",
        "sessionIndexBackupPath",
        "rolloutBackupPath",
        "matchedThreadsPath",
        "transactionJournalPath",
    )
    backup_sources = [manifest.get(field_name) for field_name in backup_source_fields]
    backup_sources.extend(item.get("backup") for item in manifest.get("projectRolloutBackups") or [])
    backup_sources.extend(item.get("backup") for item in manifest.get("workspaceMoveRolloutBackups") or [])
    backup_sources.extend(item.get("backup") for item in manifest.get("resourceBackups") or [])
    for source_text in backup_sources:
        normalized_source = normalize_path_text(source_text)
        if normalized_source and not path_is_within(Path(normalized_source), backup_directory):
            raise ValueError(f"backup manifest path is outside backup directory: {normalized_source}")

    expected_database_path = paths.database_path.resolve(strict=False)
    manifest_database_text = normalize_path_text(manifest.get("databasePath"))
    if manifest_database_text and Path(manifest_database_text).resolve(strict=False) != expected_database_path:
        raise ValueError("backup manifest database path does not match the bound Codex Home")

    target_paths: list[str | None] = []
    row_before = manifest.get("rowBefore")
    if isinstance(row_before, dict):
        target_paths.append(normalize_path_text(row_before.get("rollout_path")))
    target_paths.extend(normalize_path_text(item.get("source")) for item in manifest.get("projectRolloutBackups") or [])
    target_paths.extend(normalize_path_text(item.get("source")) for item in manifest.get("workspaceMoveRolloutBackups") or [])
    target_paths.extend(normalize_path_text(item.get("target")) for item in manifest.get("resourceBackups") or [])
    target_paths.extend(normalize_path_text(item) for item in manifest.get("createdResourcePaths") or [])
    for target_text in target_paths:
        if target_text and not path_is_within(Path(target_text), paths.codex_home_path):
            raise ValueError(f"backup manifest restore target is outside the bound Codex Home: {target_text}")


def safe_backup_fragment(value: str | None, fallback: str = "home") -> str:
    cleaned_value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-._")
    return cleaned_value[:90] or fallback


def create_backup_directory(action: str, subject: str | None) -> tuple[str, Path]:
    timestamp_text = datetime_module.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_id = f"{timestamp_text}_{safe_backup_fragment(action, 'action')}_{safe_backup_fragment(subject)}"
    target_directory = backup_directory_path(backup_id)
    target_directory.mkdir(parents=True, exist_ok=False)
    return backup_id, target_directory


def create_database_backup(source_database_path: Path, backup_database_path: Path) -> None:
    backup_database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_database_path, timeout=30) as source_connection:
        with sqlite3.connect(backup_database_path, timeout=30) as backup_connection:
            source_connection.backup(backup_connection)


def create_home_state_backup(paths: CodexPaths, action: str, subject: str | None = None) -> dict[str, Any]:
    backup_id, target_directory = create_backup_directory(action, subject)
    database_backup_path = None
    if paths.database_path.exists():
        database_backup_path = target_directory / "state_5.sqlite.before"
        create_database_backup(paths.database_path, database_backup_path)
    global_state_backup_path = None
    if paths.global_state_path.exists():
        global_state_backup_path = target_directory / ".codex-global-state.json.before"
        shutil.copy2(paths.global_state_path, global_state_backup_path)
    global_state_bak_backup_path = None
    if paths.global_state_backup_path.exists():
        global_state_bak_backup_path = target_directory / ".codex-global-state.json.bak.before"
        shutil.copy2(paths.global_state_backup_path, global_state_bak_backup_path)
    config_backup_path = None
    if paths.config_path.exists():
        config_backup_path = target_directory / "config.toml.before"
        shutil.copy2(paths.config_path, config_backup_path)
    managed_config_path = paths.codex_home_path / "managed_config.toml"
    managed_config_backup_path = None
    if managed_config_path.exists():
        managed_config_backup_path = target_directory / "managed_config.toml.before"
        shutil.copy2(managed_config_path, managed_config_backup_path)
    manifest = {
        "backupId": backup_id,
        "createdAt": datetime_module.datetime.now(datetime_module.UTC).isoformat(),
        "codexHome": str(paths.codex_home_path),
        "databasePath": str(paths.database_path),
        "threadId": None,
        "action": action,
        "subject": subject,
        "databaseBackupPath": str(database_backup_path) if database_backup_path else None,
        "globalStateBackupPath": str(global_state_backup_path) if global_state_backup_path else None,
        "globalStateBakBackupPath": str(global_state_bak_backup_path) if global_state_bak_backup_path else None,
        "configBackupPath": str(config_backup_path) if config_backup_path else None,
        "managedConfigBackupPath": str(managed_config_backup_path) if managed_config_backup_path else None,
        "restoreMode": "home_state",
    }
    manifest_path = target_directory / "manifest.json"
    return write_sealed_backup_manifest(manifest_path, manifest)


def skipped_backup_manifest(
    paths: CodexPaths,
    action: str,
    subject: str | None = None,
    thread_id: str | None = None,
    row_before: dict[str, Any] | None = None,
    restore_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "backupId": None,
        "createdAt": datetime_module.datetime.now(datetime_module.UTC).isoformat(),
        "codexHome": str(paths.codex_home_path),
        "databasePath": str(paths.database_path),
        "threadId": thread_id,
        "action": action,
        "subject": subject,
        "rowBefore": row_before,
        "rolloutStatBefore": stat_file(row_before.get("rollout_path")) if row_before else None,
        "restoreMode": restore_mode,
        "skipped": True,
        "reason": "createBackup=false",
    }


def create_optional_home_state_backup(paths: CodexPaths, action: str, subject: str | None = None, create_backup: bool = True) -> dict[str, Any]:
    if create_backup:
        return create_home_state_backup(paths, action, subject)
    return skipped_backup_manifest(paths, action=action, subject=subject, restore_mode="skipped")


def detect_codex_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    processes: list[dict[str, Any]] = []
    codex_process_names = {"codex.exe", "codex.cmd", "codex"}
    for process in list_windows_processes():
        image_name = process["imageName"]
        if image_name.lower() in codex_process_names:
            processes.append(process)
    return processes


def write_safety_warnings() -> list[str]:
    codex_processes = detect_codex_processes()
    if not codex_processes:
        return []
    process_text = ", ".join(f"{item['imageName']}:{item['pid']}" for item in codex_processes[:8])
    return [f"Codex-related process is running; close Codex Desktop before high-risk writes when possible: {process_text}"]


def enforce_write_safety(acknowledge_codex_running_risk: bool = False) -> list[str]:
    warnings = write_safety_warnings()
    if warnings and not acknowledge_codex_running_risk:
        raise RuntimeError(warnings[0] + " Pass acknowledgeCodexRunningRisk=true to proceed.")
    return warnings


def enforce_project_rename_safety(acknowledge_codex_running_risk: bool = False) -> list[str]:
    warnings = write_safety_warnings()
    if warnings:
        acknowledgement_note = (
            " acknowledgeCodexRunningRisk cannot override this operation because Codex can rewrite the "
            "Electron sidebar cache during shutdown or while running."
            if acknowledge_codex_running_risk
            else ""
        )
        raise RuntimeError(
            warnings[0]
            + " Close Codex Desktop and any Codex CLI process before renaming a project."
            + acknowledgement_note
        )
    return warnings


def enforce_thread_migration_safety(acknowledge_codex_running_risk: bool = False) -> list[str]:
    warnings = write_safety_warnings()
    if warnings:
        acknowledgement_note = (
            " acknowledgeCodexRunningRisk cannot override this operation because Codex can rewrite the "
            "Electron sidebar cache, workspace hints, and active workspace roots while running."
            if acknowledge_codex_running_risk
            else ""
        )
        raise RuntimeError(
            warnings[0]
            + " Close Codex Desktop and any Codex CLI process before migrating a thread."
            + acknowledgement_note
        )
    return warnings


def create_action_backup(paths: CodexPaths, thread_id: str, action: str) -> dict[str, Any]:
    row_before = fetch_thread_row(paths, thread_id)
    if row_before is None:
        raise KeyError(thread_id)
    backup_id, target_directory = create_backup_directory(action, thread_id)
    database_backup_path = target_directory / "state_5.sqlite.before"
    create_database_backup(paths.database_path, database_backup_path)
    global_state_backup_path = None
    if paths.global_state_path.exists():
        global_state_backup_path = target_directory / ".codex-global-state.json.before"
        shutil.copy2(paths.global_state_path, global_state_backup_path)
    global_state_bak_backup_path = None
    if paths.global_state_backup_path.exists():
        global_state_bak_backup_path = target_directory / ".codex-global-state.json.bak.before"
        shutil.copy2(paths.global_state_backup_path, global_state_bak_backup_path)
    config_backup_path = None
    if paths.config_path.exists():
        config_backup_path = target_directory / "config.toml.before"
        shutil.copy2(paths.config_path, config_backup_path)
    session_index_backup_path = None
    if paths.session_index_path.exists():
        session_index_backup_path = target_directory / "session_index.jsonl.before"
        shutil.copy2(paths.session_index_path, session_index_backup_path)

    rollout_stat_before = stat_file(row_before.get("rollout_path"))
    rollout_backup_path = None
    normalized_rollout_path = normalize_path_text(row_before.get("rollout_path"))
    if normalized_rollout_path and Path(normalized_rollout_path).exists():
        rollout_backup_path = target_directory / Path(normalized_rollout_path).name
        shutil.copy2(normalized_rollout_path, rollout_backup_path)
    manifest = {
        "backupId": backup_id,
        "createdAt": datetime_module.datetime.now(datetime_module.UTC).isoformat(),
        "codexHome": str(paths.codex_home_path),
        "databasePath": str(paths.database_path),
        "threadId": thread_id,
        "action": action,
        "rowBefore": row_before,
        "rolloutStatBefore": rollout_stat_before,
        "databaseBackupPath": str(database_backup_path),
        "globalStateBackupPath": str(global_state_backup_path) if global_state_backup_path else None,
        "globalStateBakBackupPath": str(global_state_bak_backup_path) if global_state_bak_backup_path else None,
        "configBackupPath": str(config_backup_path) if config_backup_path else None,
        "sessionIndexBackupPath": str(session_index_backup_path) if session_index_backup_path else None,
        "rolloutBackupPath": str(rollout_backup_path) if rollout_backup_path else None,
        "restoreMode": "row_global_state_session_index_rollout",
    }
    manifest_path = target_directory / "manifest.json"
    return write_sealed_backup_manifest(manifest_path, manifest)


def create_optional_action_backup(paths: CodexPaths, thread_id: str, action: str, create_backup: bool = True) -> dict[str, Any]:
    if create_backup:
        return create_action_backup(paths, thread_id, action)
    row_before = fetch_thread_row(paths, thread_id)
    if row_before is None:
        raise KeyError(thread_id)
    return skipped_backup_manifest(
        paths,
        action=action,
        thread_id=thread_id,
        row_before=row_before,
        restore_mode="skipped",
    )


def update_backup_manifest(backup_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    manifest_path = backup_manifest_path(backup_id)
    if not manifest_path.exists():
        raise FileNotFoundError(backup_id)
    manifest = read_verified_backup_manifest(manifest_path, allow_new_files=True)
    manifest.update(updates)
    return write_sealed_backup_manifest(manifest_path, manifest)


def update_optional_backup_manifest(manifest: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    backup_id = manifest.get("backupId")
    if backup_id:
        return update_backup_manifest(str(backup_id), updates)
    manifest.update(updates)
    return manifest


def manifest_list_marker(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def append_backup_manifest_lists(backup_id: str, updates: dict[str, list[Any]]) -> dict[str, Any]:
    manifest_path = backup_manifest_path(backup_id)
    if not manifest_path.exists():
        raise FileNotFoundError(backup_id)
    manifest = read_verified_backup_manifest(manifest_path, allow_new_files=True)
    for key, values in updates.items():
        current_values = list(manifest.get(key) or [])
        current_set = {manifest_list_marker(value) for value in current_values}
        for value in values:
            marker = manifest_list_marker(value)
            if marker not in current_set:
                current_values.append(value)
                current_set.add(marker)
        manifest[key] = current_values
    return write_sealed_backup_manifest(manifest_path, manifest)


def append_optional_backup_manifest_lists(manifest: dict[str, Any], updates: dict[str, list[Any]]) -> dict[str, Any]:
    backup_id = manifest.get("backupId")
    if backup_id:
        return append_backup_manifest_lists(str(backup_id), updates)
    for key, values in updates.items():
        current_values = list(manifest.get(key) or [])
        current_set = {manifest_list_marker(value) for value in current_values}
        for value in values:
            marker = manifest_list_marker(value)
            if marker not in current_set:
                current_values.append(value)
                current_set.add(marker)
        manifest[key] = current_values
    return manifest


def upsert_thread_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    columns = [column for column in thread_columns if column in row]
    existing_row = connection.execute("SELECT id FROM threads WHERE id = ?", (row["id"],)).fetchone()
    if existing_row:
        assignments = [f"{column} = ?" for column in columns if column != "id"]
        values = [row.get(column) for column in columns if column != "id"]
        values.append(row["id"])
        connection.execute(f"UPDATE threads SET {', '.join(assignments)} WHERE id = ?", values)
        return
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO threads ({', '.join(columns)}) VALUES ({placeholders})",
        [row.get(column) for column in columns],
    )


def max_thread_updated_at_ms(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT MAX(COALESCE(updated_at_ms, updated_at * 1000)) AS max_updated_at_ms FROM threads"
    ).fetchone()
    return int(row["max_updated_at_ms"] or 0)


def session_index_timestamp_from_ms(updated_at_ms: int) -> str:
    timestamp = datetime_module.datetime.fromtimestamp(updated_at_ms / 1000, datetime_module.UTC)
    return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sidebar_index_title_for_row(paths: CodexPaths, row: dict[str, Any]) -> str:
    existing_entry = read_session_index_entries(paths).get(str(row["id"]), {})
    existing_title = str(existing_entry.get("sidebarTitle") or "").strip()
    if existing_title:
        return existing_title
    for key in ("title", "preview", "first_user_message"):
        value = re.sub(r"\s+", " ", str(row.get(key) or "")).strip()
        if value:
            return value[:120]
    return str(row["id"])


def repair_first_user_message_for_row(paths: CodexPaths, row: dict[str, Any]) -> str:
    for key in ("first_user_message", "preview", "title"):
        value = str(row.get(key) or "").strip()
        if value:
            return value[:2000]
    existing_entry = read_session_index_entries(paths).get(str(row["id"]), {})
    existing_title = str(existing_entry.get("sidebarTitle") or "").strip()
    if existing_title:
        return existing_title[:2000]
    return str(row["id"])


def resolve_thread_title_sync_target(paths: CodexPaths, row: dict[str, Any], target_title: str | None = None) -> dict[str, Any]:
    thread_id = str(row["id"])
    sidebar_entry = read_session_index_entries(paths).get(thread_id, {})
    sqlite_title = str(row.get("title") or "").strip()
    sidebar_title = str(sidebar_entry.get("sidebarTitle") or "").strip()
    requested_title = str(target_title or "").strip()
    resolved_title = requested_title or sidebar_title
    if not resolved_title:
        raise ValueError("target title is empty and no sidebar title exists")
    return {
        "threadId": thread_id,
        "sqliteTitle": sqlite_title,
        "sidebarTitle": sidebar_title,
        "targetTitle": resolved_title,
        "usesExplicitTarget": bool(requested_title),
        "needsUpdate": sqlite_title != resolved_title,
        "sessionIndexLine": sidebar_entry.get("sessionIndexLine"),
        "sessionIndexUpdatedAt": sidebar_entry.get("sessionIndexUpdatedAt") or "",
    }


def sync_thread_sqlite_title(
    codex_home_text: str | None,
    thread_id: str,
    target_title: str | None = None,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    row_before = fetch_thread_row(paths, thread_id, readonly=True)
    if row_before is None:
        raise KeyError(thread_id)
    title_resolution = resolve_thread_title_sync_target(paths, row_before, target_title)
    backup_manifest = create_optional_action_backup(paths, thread_id, action="sync_thread_sqlite_title", create_backup=create_backup)
    if title_resolution["needsUpdate"]:
        with connect_database(paths.database_path, readonly=False) as connection:
            connection.execute(
                "UPDATE threads SET title = ? WHERE id = ?",
                (title_resolution["targetTitle"], thread_id),
            )
            connection.commit()
    row_after = fetch_thread_row(paths, thread_id, readonly=True)
    return {
        "threadId": thread_id,
        "sqliteTitleBefore": title_resolution["sqliteTitle"],
        "sqliteTitleAfter": str((row_after or {}).get("title") or ""),
        "sidebarTitle": title_resolution["sidebarTitle"],
        "targetTitle": title_resolution["targetTitle"],
        "updated": bool(title_resolution["needsUpdate"]),
        "sessionIndexLine": title_resolution["sessionIndexLine"],
        "sessionIndexUpdatedAt": title_resolution["sessionIndexUpdatedAt"],
        "backup": backup_manifest,
        "warnings": warnings,
    }


def append_session_index_entry(paths: CodexPaths, row: dict[str, Any], updated_at_ms: int) -> dict[str, Any]:
    paths.session_index_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": str(row["id"]),
        "thread_name": sidebar_index_title_for_row(paths, row),
        "updated_at": session_index_timestamp_from_ms(updated_at_ms),
    }
    needs_leading_newline = False
    if paths.session_index_path.exists() and paths.session_index_path.stat().st_size > 0:
        with paths.session_index_path.open("rb") as file:
            file.seek(-1, os.SEEK_END)
            needs_leading_newline = file.read(1) != b"\n"
    with paths.session_index_path.open("a", encoding="utf-8", newline="") as file:
        if needs_leading_newline:
            file.write("\n")
        file.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    return entry


def remove_session_index_entries(paths: CodexPaths, thread_id: str) -> dict[str, Any]:
    if not paths.session_index_path.exists():
        return {"status": "missing", "removedEntries": 0}
    before_stat = paths.session_index_path.stat()
    temp_path = paths.session_index_path.with_name(paths.session_index_path.name + ".remove-thread.tmp")
    removed_entries = 0
    with paths.session_index_path.open("r", encoding="utf-8", errors="replace") as source, temp_path.open("w", encoding="utf-8", newline="") as output:
        for line in source:
            should_remove = False
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                item = None
            if isinstance(item, dict) and str(item.get("id") or "") == thread_id:
                should_remove = True
            if should_remove:
                removed_entries += 1
                continue
            output.write(line)
    current_stat = paths.session_index_path.stat()
    if current_stat.st_size != before_stat.st_size or current_stat.st_mtime_ns != before_stat.st_mtime_ns:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("session_index.jsonl changed during thread hide")
    if removed_entries:
        replace_file_with_retry(temp_path, paths.session_index_path)
    else:
        temp_path.unlink(missing_ok=True)
    return {"status": "applied" if removed_entries else "no_change", "removedEntries": removed_entries}


def mutate_thread_reference_containers(global_state: dict[str, Any], mutator: Any) -> int:
    change_count = mutator(global_state)
    nested_state = global_state.get("electron-persisted-atom-state")
    if isinstance(nested_state, dict):
        change_count += mutator(nested_state)
    return change_count


def mark_thread_hidden_in_global_state(paths: CodexPaths, thread_id: str) -> dict[str, Any]:
    result = {"addedHiddenMarker": False, "hiddenMarkerContainersTouched": 0}

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        def hidden_ids_from_container(container: dict[str, Any]) -> list[Any]:
            hidden_ids: list[Any] = []
            for key in (manager_hidden_thread_ids_key, legacy_manager_hidden_thread_ids_key):
                key_hidden_ids = container.get(key)
                if isinstance(key_hidden_ids, list):
                    hidden_ids.extend(key_hidden_ids)
            return hidden_ids

        def add_hidden_marker(container: dict[str, Any]) -> int:
            hidden_ids = hidden_ids_from_container(container)
            existing_ids = {str(item) for item in hidden_ids}
            if thread_id in existing_ids:
                container[manager_hidden_thread_ids_key] = hidden_ids
                container.pop(legacy_manager_hidden_thread_ids_key, None)
                container.pop(legacy_manager_hidden_thread_ids_updated_at_key, None)
                return 0
            container[manager_hidden_thread_ids_key] = [*hidden_ids, thread_id]
            container.pop(legacy_manager_hidden_thread_ids_key, None)
            container.pop(legacy_manager_hidden_thread_ids_updated_at_key, None)
            return 1

        touched_containers = mutate_thread_reference_containers(data, add_hidden_marker)
        result["hiddenMarkerContainersTouched"] = touched_containers
        result["addedHiddenMarker"] = touched_containers > 0
        hidden_ids = hidden_ids_from_container(data)
        existing_ids = {str(item) for item in hidden_ids}
        if thread_id not in existing_ids:
            data[manager_hidden_thread_ids_key] = [*hidden_ids, thread_id]
            result["addedHiddenMarker"] = True
        else:
            data[manager_hidden_thread_ids_key] = hidden_ids
        data[manager_hidden_thread_ids_updated_at_key] = datetime_module.datetime.now(datetime_module.UTC).isoformat()
        data.pop(legacy_manager_hidden_thread_ids_key, None)
        data.pop(legacy_manager_hidden_thread_ids_updated_at_key, None)
        return data

    update_global_state(paths, updater)
    return result


def remove_thread_sidebar_references_from_global_state(paths: CodexPaths, thread_id: str) -> dict[str, Any]:
    result = {
        "removedPinnedThreadIds": 0,
        "removedProjectlessThreadIds": 0,
        "removedWorkspaceRootHints": 0,
        "removedHeartbeatPermissions": 0,
        "containersTouched": 0,
    }

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        def remove_from_container(container: dict[str, Any]) -> int:
            touched = 0
            for key, result_key in (
                ("pinned-thread-ids", "removedPinnedThreadIds"),
                ("projectless-thread-ids", "removedProjectlessThreadIds"),
            ):
                values = container.get(key)
                if not isinstance(values, list):
                    continue
                next_values = [value for value in values if str(value) != thread_id]
                removed_count = len(values) - len(next_values)
                if removed_count:
                    container[key] = next_values
                    result[result_key] += removed_count
                    touched += 1

            for key, result_key in (
                ("thread-workspace-root-hints", "removedWorkspaceRootHints"),
                ("heartbeat-thread-permissions-by-id", "removedHeartbeatPermissions"),
            ):
                values = container.get(key)
                if not isinstance(values, dict) or thread_id not in values:
                    continue
                values.pop(thread_id, None)
                container[key] = values
                result[result_key] += 1
                touched += 1
            return touched

        result["containersTouched"] = mutate_thread_reference_containers(data, remove_from_container)
        return data

    update_global_state(paths, updater)
    return result


def combine_sidebar_reference_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    combined = {
        "removedPinnedThreadIds": 0,
        "removedProjectlessThreadIds": 0,
        "removedWorkspaceRootHints": 0,
        "removedHeartbeatPermissions": 0,
        "containersTouched": 0,
        "passes": results,
    }
    for result in results:
        for key in (
            "removedPinnedThreadIds",
            "removedProjectlessThreadIds",
            "removedWorkspaceRootHints",
            "removedHeartbeatPermissions",
            "containersTouched",
        ):
            combined[key] += int(result.get(key) or 0)
    return combined


def clear_manager_hidden_thread_from_global_state(paths: CodexPaths, thread_id: str) -> dict[str, Any]:
    result = {"removedHiddenEntries": 0}

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        removed_entries = 0

        def remove_from_container(container: dict[str, Any]) -> int:
            hidden_ids: list[Any] = []
            for key in (manager_hidden_thread_ids_key, legacy_manager_hidden_thread_ids_key):
                key_hidden_ids = container.get(key)
                if isinstance(key_hidden_ids, list):
                    hidden_ids.extend(key_hidden_ids)
            if not hidden_ids:
                container.pop(legacy_manager_hidden_thread_ids_key, None)
                container.pop(legacy_manager_hidden_thread_ids_updated_at_key, None)
                return 0
            next_hidden_ids = [item for item in hidden_ids if str(item) != thread_id]
            container[manager_hidden_thread_ids_key] = next_hidden_ids
            container.pop(legacy_manager_hidden_thread_ids_key, None)
            container.pop(legacy_manager_hidden_thread_ids_updated_at_key, None)
            return len(hidden_ids) - len(next_hidden_ids)

        removed_entries += mutate_thread_reference_containers(data, remove_from_container)
        result["removedHiddenEntries"] = removed_entries
        return data

    update_global_state(paths, updater)
    return result


def min_thread_updated_at_ms(connection: sqlite3.Connection, excluded_thread_id: str) -> int:
    row = connection.execute(
        "SELECT MIN(COALESCE(updated_at_ms, updated_at * 1000)) AS min_updated_at_ms FROM threads WHERE id != ?",
        (excluded_thread_id,),
    ).fetchone()
    return int(row["min_updated_at_ms"] or 0)


def show_thread_in_sidebar(
    codex_home_text: str | None,
    thread_id: str,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    backup_manifest = create_optional_action_backup(paths, thread_id, action="show", create_backup=create_backup)
    row_before = backup_manifest["rowBefore"]
    repaired_first_user_message = repair_first_user_message_for_row(paths, row_before)
    with connect_database(paths.database_path, readonly=False) as connection:
        row = connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if row is None:
            raise KeyError(thread_id)
        target_updated_at_ms = max(max_thread_updated_at_ms(connection) + 1000, int(time.time() * 1000))
        target_updated_at = target_updated_at_ms // 1000
        connection.execute(
            """
            UPDATE threads
            SET updated_at = ?,
                updated_at_ms = ?,
                has_user_event = 1,
                first_user_message = CASE
                    WHEN TRIM(COALESCE(first_user_message, '')) = '' THEN ?
                    ELSE first_user_message
                END,
                preview = CASE
                    WHEN TRIM(COALESCE(preview, '')) = '' THEN ?
                    ELSE preview
                END,
                archived = 0,
                archived_at = NULL
            WHERE id = ?
            """,
            (target_updated_at, target_updated_at_ms, repaired_first_user_message, repaired_first_user_message, thread_id),
        )
        connection.commit()

    rollout_restore = restore_rollout_from_archive_if_needed(paths, row_before, target_updated_at)
    rollout_path = Path(normalize_path_text(rollout_restore.get("targetPath") or backup_manifest["rowBefore"].get("rollout_path")))
    if rollout_path.exists():
        os.utime(rollout_path, (target_updated_at, target_updated_at))
    session_index_entry = append_session_index_entry(paths, row_before, target_updated_at_ms)
    clear_hidden_result = clear_manager_hidden_thread_from_global_state(paths, thread_id)
    manifest_updates: dict[str, Any] = {"rolloutRestore": rollout_restore}
    if rollout_restore.get("createdTarget"):
        manifest_updates["createdResourcePaths"] = [rollout_restore["targetPath"]]
    backup_manifest = update_optional_backup_manifest(backup_manifest, manifest_updates)

    return {
        "threadId": thread_id,
        "updatedAtMs": target_updated_at_ms,
        "sessionIndexEntry": session_index_entry,
        "clearedHiddenState": clear_hidden_result,
        "rolloutRestore": rollout_restore,
        "backup": backup_manifest,
        "warnings": warnings,
    }


def repair_user_event(
    codex_home_text: str | None,
    thread_id: str,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    backup_manifest = create_optional_action_backup(paths, thread_id, action="repair_user_event", create_backup=create_backup)
    row_before = backup_manifest["rowBefore"]
    repaired_first_user_message = repair_first_user_message_for_row(paths, row_before)
    with connect_database(paths.database_path, readonly=False) as connection:
        target_updated_at_ms = max(max_thread_updated_at_ms(connection) + 1000, int(time.time() * 1000))
        target_updated_at = target_updated_at_ms // 1000
        connection.execute(
            """
            UPDATE threads
            SET has_user_event = 1,
                first_user_message = CASE
                    WHEN TRIM(COALESCE(first_user_message, '')) = '' THEN ?
                    ELSE first_user_message
                END,
                preview = CASE
                    WHEN TRIM(COALESCE(preview, '')) = '' THEN ?
                    ELSE preview
                END,
                archived = 0,
                archived_at = NULL,
                updated_at = ?,
                updated_at_ms = ?
            WHERE id = ?
            """,
            (repaired_first_user_message, repaired_first_user_message, target_updated_at, target_updated_at_ms, thread_id),
        )
        connection.commit()

    rollout_restore = restore_rollout_from_archive_if_needed(paths, row_before, target_updated_at)
    rollout_path = Path(normalize_path_text(rollout_restore.get("targetPath") or backup_manifest["rowBefore"].get("rollout_path")))
    if rollout_path.exists():
        os.utime(rollout_path, (target_updated_at, target_updated_at))
    session_index_entry = append_session_index_entry(paths, row_before, target_updated_at_ms)
    clear_hidden_result = clear_manager_hidden_thread_from_global_state(paths, thread_id)
    manifest_updates: dict[str, Any] = {"rolloutRestore": rollout_restore}
    if rollout_restore.get("createdTarget"):
        manifest_updates["createdResourcePaths"] = [rollout_restore["targetPath"]]
    backup_manifest = update_optional_backup_manifest(backup_manifest, manifest_updates)

    return {
        "threadId": thread_id,
        "updatedAtMs": target_updated_at_ms,
        "sessionIndexEntry": session_index_entry,
        "clearedHiddenState": clear_hidden_result,
        "rolloutRestore": rollout_restore,
        "backup": backup_manifest,
        "warnings": warnings,
    }


def hide_thread_from_sidebar(
    codex_home_text: str | None,
    thread_id: str,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    backup_manifest = create_optional_action_backup(paths, thread_id, action="hide", create_backup=create_backup)
    target_updated_at_ms = 0
    with connect_database(paths.database_path, readonly=False) as connection:
        row = connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if row is None:
            raise KeyError(thread_id)
        oldest_other_updated_at_ms = min_thread_updated_at_ms(connection, thread_id)
        target_updated_at_ms = max(0, oldest_other_updated_at_ms - 1000)
        connection.execute(
            """
            UPDATE threads
            SET updated_at = ?,
                updated_at_ms = ?,
                archived = 0,
                archived_at = NULL
            WHERE id = ?
            """,
            (target_updated_at_ms // 1000, target_updated_at_ms, thread_id),
        )
        connection.commit()

    rollout_path = Path(normalize_path_text(backup_manifest["rowBefore"].get("rollout_path")))
    if rollout_path.exists():
        updated_at_seconds = target_updated_at_ms // 1000
        os.utime(rollout_path, (updated_at_seconds, updated_at_seconds))
    session_index_result = remove_session_index_entries(paths, thread_id)
    global_state_result = mark_thread_hidden_in_global_state(paths, thread_id)
    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "hiddenAtMs": int(time.time() * 1000),
            "targetUpdatedAtMs": target_updated_at_ms,
            "sessionIndexUpdate": session_index_result,
            "globalStateUpdate": global_state_result,
        },
    )
    return {
        "threadId": thread_id,
        "updatedAtMs": target_updated_at_ms,
        "sessionIndexUpdate": session_index_result,
        "globalStateUpdate": global_state_result,
        "backup": backup_manifest,
        "warnings": warnings,
    }


def backup_thread(codex_home_text: str | None, thread_id: str) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    return create_action_backup(paths, thread_id, action="manual_backup")


def list_backups(codex_home_text: str | None = None, thread_id: str | None = None) -> list[dict[str, Any]]:
    selected_home = resolve_codex_paths(codex_home_text).codex_home_path
    selected_home_key = os.path.normcase(str(selected_home))
    root_path = backup_root_path()
    if not root_path.exists():
        return []
    backups: list[dict[str, Any]] = []
    for manifest_path in root_path.glob("*/manifest.json"):
        try:
            manifest = read_verified_backup_manifest(manifest_path)
        except ValueError:
            continue
        manifest_home_text = normalize_path_text(manifest.get("codexHome"))
        if not manifest_home_text:
            continue
        manifest_home_key = os.path.normcase(str(Path(manifest_home_text).expanduser().resolve(strict=False)))
        if manifest_home_key != selected_home_key:
            continue
        if thread_id and manifest.get("threadId") != thread_id:
            continue
        manifest["manifestPath"] = str(manifest_path)
        backups.append(manifest)
    backups.sort(key=lambda item: item.get("createdAt", ""), reverse=True)
    return backups


def restore_backup(
    backup_id: str,
    codex_home_text: str | None = None,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    manifest_path = backup_manifest_path(backup_id)
    if not manifest_path.exists():
        raise FileNotFoundError(backup_id)
    manifest = read_verified_backup_manifest(manifest_path)
    manifest_home_text = normalize_path_text(manifest.get("codexHome"))
    if not manifest_home_text:
        raise ValueError("backup manifest has no Codex Home binding")
    manifest_paths = resolve_codex_paths(manifest_home_text)
    paths = resolve_codex_paths(codex_home_text) if codex_home_text is not None else manifest_paths
    if os.path.normcase(str(paths.codex_home_path)) != os.path.normcase(str(manifest_paths.codex_home_path)):
        raise ValueError("backup belongs to a different Codex Home")
    validate_restore_manifest_paths(manifest, manifest_path, paths)
    thread_id = manifest.get("threadId")
    row_before = manifest.get("rowBefore")
    restore_notes: list[str] = []
    if create_backup:
        try:
            if thread_id:
                create_action_backup(paths, thread_id, action="pre_restore")
            else:
                backup_reference = hashlib.sha256(backup_id.encode("utf-8")).hexdigest()[:16]
                create_home_state_backup(paths, action="pre_restore", subject=backup_reference)
        except Exception as backup_error:
            raise RuntimeError(f"pre-restore backup failed: {backup_error}") from backup_error
    else:
        restore_notes.append("pre_restore backup skipped by createBackup=false")

    if manifest.get("restoreMode") == "thread_workspace_move_state_rollouts_files":
        source_path_text = normalize_path_text(manifest.get("sourceProjectPath"))
        target_path_text = normalize_path_text(manifest.get("targetProjectPath"))
        if not source_path_text or not target_path_text:
            raise ValueError("workspace move backup source and target project paths are required")
        source_path = Path(source_path_text)
        target_path = Path(target_path_text)
        if not source_path.is_absolute() or not target_path.is_absolute():
            raise ValueError("workspace move backup source and target project paths must be absolute")
        if source_path.resolve(strict=False) == target_path.resolve(strict=False):
            raise ValueError("workspace move backup source and target project paths must differ")
        if path_is_within(source_path, target_path) or path_is_within(target_path, source_path):
            raise ValueError("workspace move backup source and target project paths cannot be nested")
        moved_names = list(
            (manifest.get("fileMove") or {}).get("movedTopLevelNames")
            or manifest.get("transactionMovedTopLevelNames")
            or []
        )
        restore_notes.extend(
            rollback_workspace_move_transaction(paths, manifest, source_path, target_path, moved_names)
        )
        record_workspace_move_journal(
            manifest,
            stage="restored_by_user",
            source_path=source_path,
            target_path=target_path,
            moved_names=moved_names,
        )
        return {
            "threadId": thread_id,
            "restoredBackupId": backup_id,
            "restoredAt": datetime_module.datetime.now(datetime_module.UTC).isoformat(),
            "notes": restore_notes,
            "warnings": warnings,
        }

    restore_entire_database = bool(manifest.get("restoreEntireDatabase")) or (
        not thread_id and bool(manifest.get("databaseBackupPath"))
    )
    if restore_entire_database:
        database_backup_path = manifest.get("databaseBackupPath")
        if database_backup_path and Path(database_backup_path).exists():
            paths.database_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(database_backup_path, paths.database_path)
            restore_notes.append("restored entire SQLite database")

    if not restore_entire_database:
        with connect_database(paths.database_path, readonly=False) as connection:
            if row_before:
                upsert_thread_row(connection, row_before)
            matched_threads_path = manifest.get("matchedThreadsPath")
            if matched_threads_path and Path(matched_threads_path).exists():
                matched_rows = json.loads(Path(matched_threads_path).read_text(encoding="utf-8"))
                for matched_row in matched_rows:
                    upsert_thread_row(connection, matched_row)
                restore_notes.append(f"restored {len(matched_rows)} project thread rows")
            created_thread_ids = []
            if manifest.get("createdThreadId"):
                created_thread_ids.append(manifest["createdThreadId"])
            created_thread_ids.extend(manifest.get("createdThreadIds") or [])
            for created_thread_id in sorted(set(created_thread_ids)):
                archived_at = int(time.time())
                connection.execute(
                    "UPDATE threads SET archived = 1, archived_at = ? WHERE id = ?",
                    (archived_at, created_thread_id),
                )
                restore_notes.append(f"archived duplicate thread {created_thread_id}")
            connection.commit()

    rollout_backup_path = manifest.get("rolloutBackupPath")
    if rollout_backup_path and Path(rollout_backup_path).exists() and row_before:
        rollout_target_path = Path(normalize_path_text(row_before.get("rollout_path")))
        rollout_target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rollout_backup_path, rollout_target_path)
        restore_notes.append("restored primary rollout")

    restored_project_rollouts = 0
    for rollout_backup in manifest.get("projectRolloutBackups") or []:
        backup_path = Path(rollout_backup.get("backup", ""))
        source_text = normalize_path_text(rollout_backup.get("source"))
        if not source_text:
            continue
        target_path = Path(source_text)
        if backup_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, target_path)
            restored_project_rollouts += 1
    if restored_project_rollouts:
        restore_notes.append(f"restored {restored_project_rollouts} project rollouts")

    global_state_backup_path = manifest.get("globalStateBackupPath")
    if global_state_backup_path and Path(global_state_backup_path).exists():
        shutil.copy2(global_state_backup_path, paths.global_state_path)
        restore_notes.append("restored global state")

    global_state_bak_backup_path = manifest.get("globalStateBakBackupPath")
    if global_state_bak_backup_path and Path(global_state_bak_backup_path).exists():
        shutil.copy2(global_state_bak_backup_path, paths.global_state_backup_path)
        restore_notes.append("restored global state backup")

    config_backup_path = manifest.get("configBackupPath")
    if config_backup_path and Path(config_backup_path).exists():
        shutil.copy2(config_backup_path, paths.config_path)
        restore_notes.append("restored config")

    managed_config_backup_path = manifest.get("managedConfigBackupPath")
    if managed_config_backup_path and Path(managed_config_backup_path).exists():
        shutil.copy2(managed_config_backup_path, paths.codex_home_path / "managed_config.toml")
        restore_notes.append("restored managed config")

    session_index_backup_path = manifest.get("sessionIndexBackupPath")
    if session_index_backup_path and Path(session_index_backup_path).exists():
        shutil.copy2(session_index_backup_path, paths.session_index_path)
        restore_notes.append("restored session index")

    for resource_backup in manifest.get("resourceBackups") or []:
        backup_path = Path(resource_backup.get("backup", ""))
        target_text = normalize_path_text(resource_backup.get("target"))
        if not target_text or not backup_path.exists():
            continue
        target_path = Path(target_text)
        if backup_path.is_dir():
            if target_path.exists():
                replaced_resource_hold_path = backup_directory_path(backup_id) / "resources_replaced_during_restore"
                replaced_resource_hold_path.mkdir(parents=True, exist_ok=True)
                hold_target = replaced_resource_hold_path / safe_backup_fragment(target_path.name)
                if hold_target.exists():
                    hold_target = replaced_resource_hold_path / f"{safe_backup_fragment(target_path.name)}_{uuid.uuid4().hex[:8]}"
                shutil.move(str(target_path), str(hold_target))
            shutil.copytree(backup_path, target_path, dirs_exist_ok=True)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, target_path)
        restore_notes.append(f"restored resource {target_path}")

    created_resource_revert_count = 0
    created_resource_hold_path = backup_directory_path(backup_id) / "created_resources_reverted"
    for created_resource_text in manifest.get("createdResourcePaths") or []:
        target_text = normalize_path_text(created_resource_text)
        if not target_text:
            continue
        target_path = Path(target_text)
        if not target_path.exists():
            continue
        created_resource_hold_path.mkdir(parents=True, exist_ok=True)
        hold_target = created_resource_hold_path / safe_backup_fragment(target_path.name)
        if hold_target.exists():
            hold_target = created_resource_hold_path / f"{safe_backup_fragment(target_path.stem)}_{uuid.uuid4().hex[:8]}{target_path.suffix}"
        shutil.move(str(target_path), str(hold_target))
        created_resource_revert_count += 1
    if created_resource_revert_count:
        restore_notes.append(f"moved {created_resource_revert_count} created resources out of target home")

    folder_rename = manifest.get("folderRename") or {}
    if folder_rename.get("renamed"):
        source_text = normalize_path_text(folder_rename.get("sourcePath"))
        target_text = normalize_path_text(folder_rename.get("targetPath"))
        if not source_text or not target_text:
            restore_notes.append("project folder rollback skipped because folder paths are incomplete")
            source_path = None
            target_path = None
        else:
            source_path = Path(source_text)
            target_path = Path(target_text)
        if target_path is not None and source_path is not None and target_path.exists() and not source_path.exists():
            source_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.rename(source_path)
            restore_notes.append("renamed project folder back")
        elif target_path is not None and source_path is not None and target_path.exists() and source_path.exists():
            restore_notes.append("project folder rollback skipped because both source and target exist")
        elif target_path is not None and not target_path.exists():
            restore_notes.append("project folder rollback skipped because target folder no longer exists")

    rollout_stat_before = manifest.get("rolloutStatBefore") or {}
    rollout_path = normalize_path_text(row_before.get("rollout_path")) if row_before else ""
    modified_at_ms = rollout_stat_before.get("modifiedAtMs")
    if rollout_path and modified_at_ms and Path(rollout_path).exists():
        modified_at = int(modified_at_ms) / 1000
        os.utime(rollout_path, (modified_at, modified_at))

    restored_at = datetime_module.datetime.now(datetime_module.UTC).isoformat()
    update_backup_manifest(backup_id, {"lastRestoredAt": restored_at})
    return {
        "threadId": thread_id,
        "restoredBackupId": backup_id,
        "restoredAt": restored_at,
        "notes": restore_notes,
        "warnings": warnings,
    }


def update_global_state(paths: CodexPaths, updater: Any) -> dict[str, Any]:
    data = read_global_state(paths)
    updated_data = updater(data)
    if updated_data is None:
        updated_data = data
    paths.global_state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = paths.global_state_path.with_name(paths.global_state_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(updated_data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
        newline="",
    )
    replace_file_with_retry(temp_path, paths.global_state_path)
    return updated_data


def update_json_state_file(path: Path, updater: Any, create_if_missing: bool = False) -> dict[str, Any]:
    if not path.exists():
        if create_if_missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8", newline="")
        else:
            return {"path": str(path), "status": "missing", "changeCount": 0}
    if not path.exists():
        return {"path": str(path), "status": "missing", "changeCount": 0}
    before_stat = path.stat()
    data = read_json_file(path)
    updated_data = updater(data)
    if updated_data is None:
        updated_data = data
    change_count = int(updated_data.pop("__codex_home_manager_change_count__", 0)) if isinstance(updated_data, dict) else 0
    current_stat = path.stat()
    if current_stat.st_size != before_stat.st_size or current_stat.st_mtime_ns != before_stat.st_mtime_ns:
        raise RuntimeError(f"{path} changed during state rewrite")
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        json.dumps(updated_data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
        newline="",
    )
    replace_file_with_retry(temp_path, path)
    return {"path": str(path), "status": "applied", "changeCount": change_count}


def apply_thread_project_to_state_data(data: dict[str, Any], project_path: str, thread_id: str | None = None, make_active: bool = False) -> int:
    normalized_project_path = normalize_path_text(project_path)
    changed_count = 0

    def set_ordered_path_list(container: dict[str, Any], key: str) -> None:
        nonlocal changed_count
        values = container.get(key)
        if not isinstance(values, list):
            values = []
        filtered_values = [value for value in values if comparable_path_text(value) != comparable_path_text(normalized_project_path)]
        new_values = [normalized_project_path, *filtered_values]
        if container.get(key) != new_values:
            container[key] = new_values
            changed_count += 1

    def set_active_path_list(container: dict[str, Any]) -> None:
        nonlocal changed_count
        new_values = [normalized_project_path]
        if container.get("active-workspace-roots") != new_values:
            container["active-workspace-roots"] = new_values
            changed_count += 1

    def set_thread_hint(container: dict[str, Any]) -> None:
        nonlocal changed_count
        if not thread_id:
            return
        hints = container.get("thread-workspace-root-hints")
        if not isinstance(hints, dict):
            hints = {}
            container["thread-workspace-root-hints"] = hints
            changed_count += 1
        if hints.get(thread_id) != normalized_project_path:
            hints[thread_id] = normalized_project_path
            changed_count += 1
        projectless = container.get("projectless-thread-ids")
        if isinstance(projectless, list) and thread_id in projectless:
            container["projectless-thread-ids"] = [item for item in projectless if item != thread_id]
            changed_count += 1

    for container in [data, data.get("electron-persisted-atom-state")]:
        if not isinstance(container, dict):
            continue
        for key in ("electron-saved-workspace-roots", "project-order"):
            set_ordered_path_list(container, key)
        if make_active:
            set_active_path_list(container)
        set_thread_hint(container)
    changed_count += ensure_project_label_in_state(data, normalized_project_path)
    return changed_count


def add_project_to_global_state(paths: CodexPaths, project_path: str, thread_id: str | None = None, make_active: bool = False) -> dict[str, Any]:
    normalized_project_path = normalize_path_text(project_path)

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        change_count = apply_thread_project_to_state_data(data, normalized_project_path, thread_id=thread_id, make_active=make_active)
        data["__codex_home_manager_change_count__"] = change_count
        return data

    state_result = update_json_state_file(paths.global_state_path, updater, create_if_missing=True)
    backup_result = update_json_state_file(paths.global_state_backup_path, updater)
    return {
        "state": state_result,
        "backup": backup_result,
        "changeCount": int(state_result.get("changeCount") or 0) + int(backup_result.get("changeCount") or 0),
    }


def replace_paths_in_text(text: str, replacements: list[tuple[str, str]]) -> tuple[str, int]:
    output = text
    total_count = 0
    for old_text, new_text in replacements:
        if not old_text:
            continue
        output, count = re.subn(re.escape(old_text), lambda _match: new_text, output, flags=re.IGNORECASE)
        total_count += count
    return output, total_count


def windows_extended_path_text(path_text: str) -> str:
    normalized_path = normalize_path_text(path_text)
    if re.match(r"^[A-Za-z]:\\", normalized_path):
        return "\\\\?\\" + normalized_path
    return normalized_path


def path_replacement_variants(old_path: str, new_path: str) -> list[tuple[str, str]]:
    normalized_old_path = normalize_path_text(old_path)
    normalized_new_path = normalize_path_text(new_path)
    base_variants = [
        (normalized_old_path, normalized_new_path),
        (normalized_old_path.replace("\\", "/"), normalized_new_path.replace("\\", "/")),
        (windows_extended_path_text(normalized_old_path), windows_extended_path_text(normalized_new_path)),
    ]
    variants: list[tuple[str, str]] = []
    for old_value, new_value in base_variants:
        variants.append((old_value, new_value))
        variants.append(
            (
                json.dumps(old_value, ensure_ascii=False)[1:-1],
                json.dumps(new_value, ensure_ascii=False)[1:-1],
            )
        )
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for old_value, new_value in variants:
        marker = old_value.lower()
        if old_value and marker not in seen:
            output.append((old_value, new_value))
            seen.add(marker)
    return output


def state_path_replacement_variants(old_path: str, new_path: str) -> list[tuple[str, str]]:
    normalized_old_path = normalize_path_text(old_path)
    normalized_new_path = normalize_path_text(new_path)
    base_variants = [
        (windows_extended_path_text(normalized_old_path), normalized_new_path),
        (normalized_old_path, normalized_new_path),
        (normalized_old_path.replace("\\", "/"), normalized_new_path.replace("\\", "/")),
    ]
    variants: list[tuple[str, str]] = []
    for old_value, new_value in base_variants:
        variants.append((old_value, new_value))
        variants.append(
            (
                json.dumps(old_value, ensure_ascii=False)[1:-1],
                json.dumps(new_value, ensure_ascii=False)[1:-1],
            )
        )
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for old_value, new_value in variants:
        marker = old_value.lower()
        if old_value and marker not in seen:
            output.append((old_value, new_value))
            seen.add(marker)
    return output


def replace_paths_in_jsonl(path: Path, replacements: list[tuple[str, str]]) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "replacementCount": 0}
    before_stat = path.stat()
    temp_path = path.with_name(path.name + ".path-rewrite.tmp")
    replacement_count = 0
    parse_errors = 0
    with path.open("r", encoding="utf-8", errors="replace") as source, temp_path.open("w", encoding="utf-8", newline="") as output:
        for line in source:
            updated_line, count = replace_paths_in_text(line, replacements)
            replacement_count += count
            try:
                json.loads(updated_line)
            except json.JSONDecodeError:
                parse_errors += 1
            output.write(updated_line)
    if parse_errors:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"path rewrite created invalid JSONL lines: {parse_errors}")
    current_stat = path.stat()
    if current_stat.st_size != before_stat.st_size or current_stat.st_mtime_ns != before_stat.st_mtime_ns:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("source JSONL changed during path rewrite")
    if replacement_count:
        replace_file_with_retry(temp_path, path)
    else:
        temp_path.unlink(missing_ok=True)
    return {"status": "applied" if replacement_count else "no_change", "replacementCount": replacement_count}


def extract_user_prompts_from_rollout(rollout_path_text: str) -> list[dict[str, Any]]:
    normalized_path = normalize_path_text(rollout_path_text)
    prompts: list[dict[str, Any]] = []
    if not normalized_path or not Path(normalized_path).exists():
        return prompts

    with Path(normalized_path).open("r", encoding="utf-8", errors="replace") as file:
        for line_number, line in enumerate(file, 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = item.get("timestamp")
            item_type = item.get("type")
            payload = item.get("payload") or {}
            text_parts: list[str] = []

            if item_type == "user_message":
                if isinstance(payload, str):
                    text_parts.append(payload)
                elif isinstance(payload, dict):
                    content = payload.get("content")
                    if isinstance(content, str):
                        text_parts.append(content)
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                text = part.get("text")
                                if isinstance(text, str):
                                    text_parts.append(text)
                    text = payload.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)

            if item_type == "response_item" and payload.get("role") == "user":
                content = payload.get("content")
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            text = part.get("text")
                            if isinstance(text, str):
                                text_parts.append(text)

            text = "\n".join(part for part in text_parts if part).strip()
            if text:
                prompt_classification = classify_prompt_record(text)
                prompts.append(
                    {
                        "index": len(prompts) + 1,
                        "lineNumber": line_number,
                        "timestamp": timestamp,
                        "text": text,
                        "characterCount": len(text),
                        **prompt_classification,
                    }
                )
    return prompts


def read_thread_prompts(codex_home_text: str | None, thread_id: str) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    row = fetch_thread_row(paths, thread_id)
    if row is None:
        raise KeyError(thread_id)
    prompts = extract_user_prompts_from_rollout(row.get("rollout_path"))
    visible_prompt_count = len(filter_prompts_for_scope(prompts, "visible"))
    pure_prompt_count = len(filter_prompts_for_scope(prompts, "pure"))
    return {
        "threadId": thread_id,
        "title": row.get("title"),
        "rolloutPath": normalize_path_text(row.get("rollout_path")),
        "promptCount": len(prompts),
        "purePromptCount": pure_prompt_count,
        "visiblePromptCount": visible_prompt_count,
        "hiddenPromptCount": len(prompts) - visible_prompt_count,
        "sourceCounts": prompt_source_counts(prompts),
        "prompts": prompts,
    }


def export_root_path() -> Path:
    configured_export_root = os.environ.get("CODEX_HOME_MANAGER_EXPORT_ROOT")
    if configured_export_root:
        return Path(configured_export_root).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parents[1] / "data" / "exports"


def safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return cleaned[:80] or "thread"


def export_thread_prompts(
    codex_home_text: str | None,
    thread_id: str,
    output_format: str = "markdown",
    scope: str = "pure",
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    row = fetch_thread_row(paths, thread_id)
    if row is None:
        raise KeyError(thread_id)
    all_prompts = extract_user_prompts_from_rollout(row.get("rollout_path"))
    prompts = filter_prompts_for_scope(all_prompts, scope)
    source_counts = prompt_source_counts(all_prompts)
    timestamp_text = datetime_module.datetime.now().strftime("%Y%m%d_%H%M%S")
    export_directory = export_root_path()
    export_directory.mkdir(parents=True, exist_ok=True)
    filename_base = f"{timestamp_text}_{safe_filename_component(thread_id)}_prompts"

    if output_format == "json":
        output_path = export_directory / f"{filename_base}.json"
        output_payload = {
            "threadId": thread_id,
            "title": row.get("title"),
            "rolloutPath": normalize_path_text(row.get("rollout_path")),
            "promptCount": len(prompts),
            "allPromptCount": len(all_prompts),
            "filterScope": scope,
            "sourceCounts": source_counts,
            "prompts": [
                {
                    **prompt,
                    "exportText": prompt_text_for_scope(prompt, scope),
                }
                for prompt in prompts
            ],
        }
        output_path.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        output_path = export_directory / f"{filename_base}.md"
        lines = [
            f"# Prompts for {row.get('title') or thread_id}",
            "",
            f"- Thread ID: `{thread_id}`",
            f"- Rollout path: `{normalize_path_text(row.get('rollout_path'))}`",
            f"- Prompt count: {len(prompts)}",
            f"- All prompt-like records: {len(all_prompts)}",
            f"- Filter scope: `{scope}`",
            f"- Source counts: `{json.dumps(source_counts, ensure_ascii=False)}`",
            "",
        ]
        for prompt in prompts:
            export_text = prompt_text_for_scope(prompt, scope)
            lines.extend(
                [
                    f"## Prompt {prompt['index']}",
                    "",
                    f"- Timestamp: `{prompt.get('timestamp') or '-'}`",
                    f"- JSONL line: `{prompt['lineNumber']}`",
                    f"- Source: `{prompt.get('sourceLabel') or prompt.get('sourceType') or '-'}`",
                    "",
                    "```text",
                    export_text,
                    "```",
                    "",
                ]
            )
        output_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "threadId": thread_id,
        "promptCount": len(prompts),
        "allPromptCount": len(all_prompts),
        "filterScope": scope,
        "sourceCounts": source_counts,
        "outputPath": str(output_path),
        "format": output_format,
    }


def replace_workspace_root_value(value: Any, old_cwd: str, new_cwd: str) -> tuple[Any, int]:
    old_key = comparable_path_text(old_cwd)
    if isinstance(value, str):
        if comparable_path_text(value) == old_key:
            return new_cwd, 1
        return value, 0
    if isinstance(value, list):
        changed_count = 0
        updated_values: list[Any] = []
        for item in value:
            updated_item, item_changes = replace_workspace_root_value(item, old_cwd, new_cwd)
            changed_count += item_changes
            updated_values.append(updated_item)
        return updated_values, changed_count
    return value, 0


def update_rollout_workspace_metadata(rollout_path: Path, thread_id: str, old_cwd: str, new_cwd: str) -> dict[str, Any]:
    if not rollout_path.exists():
        return {
            "status": "missing",
            "path": str(rollout_path),
            "sessionMetaUpdates": 0,
            "turnContextUpdates": 0,
            "workspaceRootUpdates": 0,
            "parseErrors": 0,
            "sessionMetaIdMismatches": 0,
            "replacementCount": 0,
        }
    normalized_old_cwd = normalize_path_text(old_cwd)
    normalized_new_cwd = normalize_path_text(new_cwd)
    before_stat = rollout_path.stat()
    temp_path = rollout_path.with_name(rollout_path.name + ".workspace-metadata.tmp")
    session_meta_updates = 0
    turn_context_updates = 0
    workspace_root_updates = 0
    parse_errors = 0
    session_meta_id_mismatches = 0

    with rollout_path.open("r", encoding="utf-8", errors="replace") as source, temp_path.open("w", encoding="utf-8", newline="") as output:
        for line in source:
            if "session_meta" not in line and "turn_context" not in line:
                output.write(line)
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                output.write(line)
                continue

            item_type = item.get("type")
            payload = item.get("payload")
            if not isinstance(payload, dict):
                output.write(line)
                continue

            changed = False
            if item_type == "session_meta":
                payload_id = str(payload.get("id") or "")
                if payload_id and payload_id != thread_id:
                    session_meta_id_mismatches += 1
                if not payload_id or payload_id == thread_id:
                    updated_cwd, cwd_changes = replace_workspace_root_value(payload.get("cwd"), normalized_old_cwd, normalized_new_cwd)
                    if cwd_changes:
                        payload["cwd"] = updated_cwd
                        session_meta_updates += cwd_changes
                        changed = True
            elif item_type == "turn_context":
                updated_cwd, cwd_changes = replace_workspace_root_value(payload.get("cwd"), normalized_old_cwd, normalized_new_cwd)
                if cwd_changes:
                    payload["cwd"] = updated_cwd
                    turn_context_updates += cwd_changes
                    changed = True

            for root_key in ("workspaceRoots", "workspace_roots"):
                if root_key in payload:
                    updated_roots, root_changes = replace_workspace_root_value(payload.get(root_key), normalized_old_cwd, normalized_new_cwd)
                    if root_changes:
                        payload[root_key] = updated_roots
                        workspace_root_updates += root_changes
                        changed = True

            output.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" if changed else line)

    current_stat = rollout_path.stat()
    if current_stat.st_size != before_stat.st_size or current_stat.st_mtime_ns != before_stat.st_mtime_ns:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("source JSONL changed during workspace metadata rewrite")

    total_updates = session_meta_updates + turn_context_updates + workspace_root_updates
    if total_updates:
        replace_file_with_retry(temp_path, rollout_path)
    else:
        temp_path.unlink(missing_ok=True)
    return {
        "status": "applied" if total_updates else "no_change",
        "path": str(rollout_path),
        "sessionMetaUpdates": session_meta_updates,
        "turnContextUpdates": turn_context_updates,
        "workspaceRootUpdates": workspace_root_updates,
        "parseErrors": parse_errors,
        "sessionMetaIdMismatches": session_meta_id_mismatches,
        "replacementCount": total_updates,
    }


def update_session_meta_for_copy(source_rollout_path: Path, target_rollout_path: Path, new_thread_id: str, target_cwd: str) -> None:
    with source_rollout_path.open("r", encoding="utf-8", errors="replace") as source, target_rollout_path.open("w", encoding="utf-8", newline="") as output:
        for line in source:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                output.write(line)
                continue
            if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict):
                item["payload"]["id"] = new_thread_id
                item["payload"]["cwd"] = target_cwd
                output.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                output.write(line)


def duplicate_thread(
    codex_home_text: str | None,
    thread_id: str,
    target_project_path: str | None = None,
    title_suffix: str = " copy",
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    source_row = fetch_thread_row(paths, thread_id)
    if source_row is None:
        raise KeyError(thread_id)
    backup_manifest = create_optional_action_backup(paths, thread_id, action="duplicate", create_backup=create_backup)

    source_rollout_path = Path(normalize_path_text(source_row.get("rollout_path")))
    if not source_rollout_path.exists():
        raise FileNotFoundError(str(source_rollout_path))
    source_cwd = normalize_path_text(source_row.get("cwd"))
    target_cwd = normalize_path_text(target_project_path) if target_project_path is not None else source_cwd
    if not target_cwd:
        raise ValueError("target project path is required for duplicate thread")
    database_cwd = "\\\\?\\" + target_cwd if re.match(r"^[A-Za-z]:\\", target_cwd) else target_cwd
    new_thread_id = str(uuid.uuid4())
    now = datetime_module.datetime.now()
    now_ms = int(time.time() * 1000)
    target_directory = paths.codex_home_path / "sessions" / now.strftime("%Y") / now.strftime("%m")
    target_directory.mkdir(parents=True, exist_ok=True)
    target_rollout_path = target_directory / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{new_thread_id}.jsonl"
    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "createdThreadId": new_thread_id,
            "createdRolloutPath": str(target_rollout_path),
            "targetProjectPath": target_cwd,
            "restoreMode": "row_global_state_rollout_archive_duplicate",
        },
    )
    update_session_meta_for_copy(source_rollout_path, target_rollout_path, new_thread_id, strip_extended_prefix(target_cwd))
    if source_cwd and comparable_path_text(source_cwd) != comparable_path_text(target_cwd):
        replace_paths_in_jsonl(target_rollout_path, path_replacement_variants(source_cwd, target_cwd))

    insert_row = dict(source_row)
    insert_row["id"] = new_thread_id
    insert_row["rollout_path"] = str(target_rollout_path)
    insert_row["created_at"] = now_ms // 1000
    insert_row["updated_at"] = now_ms // 1000
    insert_row["created_at_ms"] = now_ms
    insert_row["updated_at_ms"] = now_ms
    insert_row["cwd"] = database_cwd
    insert_row["title"] = f"{source_row.get('title') or 'Untitled'}{title_suffix}"
    insert_row["archived"] = 0
    insert_row["archived_at"] = None
    insert_row["has_user_event"] = 1
    insert_row["thread_source"] = "duplicate"
    columns = [column for column in thread_columns if column in insert_row]
    placeholders = ", ".join("?" for _ in columns)
    with connect_database(paths.database_path, readonly=False) as connection:
        connection.execute(
            f"INSERT INTO threads ({', '.join(columns)}) VALUES ({placeholders})",
            [insert_row.get(column) for column in columns],
        )
        connection.commit()

    add_project_to_global_state(paths, target_cwd, thread_id=new_thread_id)
    session_index_entry = append_session_index_entry(paths, insert_row, now_ms)
    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "createdAtCommitted": datetime_module.datetime.now(datetime_module.UTC).isoformat(),
            "sessionIndexEntry": session_index_entry,
        },
    )
    return {
        "sourceThreadId": thread_id,
        "newThreadId": new_thread_id,
        "newRolloutPath": str(target_rollout_path),
        "targetProjectPath": target_cwd,
        "sessionIndexEntry": session_index_entry,
        "backup": backup_manifest,
        "warnings": warnings,
    }


def archive_thread(
    codex_home_text: str | None,
    thread_id: str,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    backup_manifest = create_optional_action_backup(paths, thread_id, action="archive", create_backup=create_backup)
    row_before = backup_manifest["rowBefore"]
    archived_at = int(time.time())
    with connect_database(paths.database_path, readonly=False) as connection:
        connection.execute(
            "UPDATE threads SET archived = 1, archived_at = ? WHERE id = ?",
            (archived_at, thread_id),
        )
        connection.commit()
    rollout_archive_result = move_rollout_to_archive_if_needed(paths, row_before)
    session_index_result = remove_session_index_entries(paths, thread_id)
    sidebar_reference_passes = [remove_thread_sidebar_references_from_global_state(paths, thread_id)]
    global_state_result = mark_thread_hidden_in_global_state(paths, thread_id)
    time.sleep(0.5)
    sidebar_reference_passes.append(remove_thread_sidebar_references_from_global_state(paths, thread_id))
    sidebar_reference_result = combine_sidebar_reference_results(sidebar_reference_passes)
    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "archivedAt": archived_at,
            "rolloutArchiveUpdate": rollout_archive_result,
            "sessionIndexUpdate": session_index_result,
            "sidebarReferenceUpdate": sidebar_reference_result,
            "globalStateUpdate": global_state_result,
        },
    )
    return {
        "threadId": thread_id,
        "archivedAt": archived_at,
        "rolloutArchiveUpdate": rollout_archive_result,
        "sessionIndexUpdate": session_index_result,
        "sidebarReferenceUpdate": sidebar_reference_result,
        "globalStateUpdate": global_state_result,
        "backup": backup_manifest,
        "warnings": warnings,
    }


def migrate_thread_project(
    codex_home_text: str | None,
    thread_id: str,
    target_project_path: str,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_thread_migration_safety(acknowledge_codex_running_risk)
    row_before = fetch_thread_row(paths, thread_id)
    if row_before is None:
        raise KeyError(thread_id)
    backup_manifest = create_optional_action_backup(paths, thread_id, action="migrate_project", create_backup=create_backup)
    old_cwd = normalize_path_text(row_before.get("cwd"))
    new_cwd = normalize_path_text(target_project_path)
    database_cwd = "\\\\?\\" + new_cwd if re.match(r"^[A-Za-z]:\\", new_cwd) else new_cwd

    with connect_database(paths.database_path, readonly=False) as connection:
        connection.execute("UPDATE threads SET cwd = ? WHERE id = ?", (database_cwd, thread_id))
        connection.commit()
    checkpoint_database(paths.database_path)
    global_state_result = add_project_to_global_state(paths, new_cwd, thread_id=thread_id, make_active=True)
    rollout_path = Path(normalize_path_text(row_before.get("rollout_path")))
    rewrite_result = update_rollout_workspace_metadata(rollout_path, thread_id, old_cwd, new_cwd)
    return {
        "threadId": thread_id,
        "oldProjectPath": old_cwd,
        "newProjectPath": new_cwd,
        "rewrite": rewrite_result,
        "globalState": global_state_result,
        "backup": backup_manifest,
        "warnings": warnings,
    }


def rows_for_exact_cwd(paths: CodexPaths, cwd_text: str, include_archived: bool = True) -> list[dict[str, Any]]:
    source_key = comparable_path_text(cwd_text)
    rows: list[dict[str, Any]] = []
    for row in fetch_thread_rows(paths):
        if not include_archived and bool(row.get("archived")):
            continue
        if comparable_path_text(normalize_path_text(row.get("cwd"))) == source_key:
            rows.append(row)
    return rows


def direct_child_names(path: Path) -> list[str]:
    if not path.exists() or not path.is_dir():
        return []
    return sorted(child.name for child in path.iterdir())


def workspace_move_safety(
    source_path: Path,
    target_path: Path,
    move_workspace_files: bool,
) -> dict[str, Any]:
    source_stats = collect_path_stats(source_path)
    target_stats = collect_path_stats(target_path)
    source_children = direct_child_names(source_path)
    target_children = direct_child_names(target_path)
    blocking_errors: list[str] = []
    warnings: list[str] = []

    if not move_workspace_files:
        warnings.append("moveWorkspaceFiles=false; only thread metadata and workspace hints will be rewritten")
        return {
            "source": source_stats,
            "target": target_stats,
            "sourceTopLevelNames": source_children,
            "targetTopLevelNames": target_children,
            "conflictingTopLevelNames": [],
            "blockingErrors": blocking_errors,
            "warnings": warnings,
            "canMoveFiles": True,
        }

    source_key = comparable_path_text(str(source_path))
    target_key = comparable_path_text(str(target_path))
    if source_key == target_key:
        blocking_errors.append("source and target workspace paths are identical")
    elif target_key.startswith(source_key + os.sep) or source_key.startswith(target_key + os.sep):
        blocking_errors.append("source and target workspace paths must not be nested inside each other")

    if source_path.exists() and not source_path.is_dir():
        blocking_errors.append(f"source workspace is not a directory: {source_path}")
    if target_path.exists() and not target_path.is_dir():
        blocking_errors.append(f"target workspace is not a directory: {target_path}")
    if not source_path.exists() and not target_path.exists():
        blocking_errors.append(f"source workspace is missing and target workspace does not exist: {source_path}")

    conflicting_names = [name for name in source_children if (target_path / name).exists()]
    if source_children and target_children:
        blocking_errors.append(
            f"target workspace is not empty while source still has content: {target_path}"
        )
    if conflicting_names:
        blocking_errors.append(f"target workspace has conflicting top-level names: {conflicting_names}")
    if not source_children and target_children:
        warnings.append("source workspace is already empty and target has content; file move will be treated as idempotent")

    return {
        "source": source_stats,
        "target": target_stats,
        "sourceTopLevelNames": source_children,
        "targetTopLevelNames": target_children,
        "conflictingTopLevelNames": conflicting_names,
        "blockingErrors": blocking_errors,
        "warnings": warnings,
        "canMoveFiles": not blocking_errors,
    }


def preview_thread_workspace_move(
    codex_home_text: str | None,
    thread_id: str,
    target_project_path: str,
    include_same_source_cwd_threads: bool = True,
    move_workspace_files: bool = True,
    repair_user_event: bool = True,
    preserve_pinned: bool = False,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    row = fetch_thread_row(paths, thread_id)
    if row is None:
        raise KeyError(thread_id)
    source_cwd = normalize_path_text(row.get("cwd"))
    if not source_cwd:
        raise ValueError("source thread cwd is empty")
    target_cwd = normalize_path_text(target_project_path)
    if not target_cwd:
        raise ValueError("targetProjectPath is empty")
    source_path = Path(source_cwd)
    target_path = Path(target_cwd)
    matched_rows = rows_for_exact_cwd(paths, source_cwd, include_archived=True) if include_same_source_cwd_threads else [row]
    matched_thread_ids = [str(matched_row["id"]) for matched_row in matched_rows]
    rollout_paths = [Path(normalize_path_text(matched_row.get("rollout_path"))) for matched_row in matched_rows]
    existing_rollout_paths = [rollout_path for rollout_path in rollout_paths if rollout_path.exists()]
    file_safety = workspace_move_safety(source_path, target_path, move_workspace_files)
    runtime_warnings = write_safety_warnings()
    warnings = [
        *file_safety["warnings"],
        *runtime_warnings,
    ]
    return {
        "threadId": thread_id,
        "sourceProjectPath": str(source_path),
        "targetProjectPath": str(target_path),
        "includeSameSourceCwdThreads": include_same_source_cwd_threads,
        "moveWorkspaceFiles": move_workspace_files,
        "repairUserEvent": repair_user_event,
        "preservePinned": preserve_pinned,
        "matchedThreads": len(matched_rows),
        "matchedThreadIds": matched_thread_ids,
        "existingRollouts": len(existing_rollout_paths),
        "rolloutBytes": sum(rollout_path.stat().st_size for rollout_path in existing_rollout_paths),
        "fileMove": file_safety,
        "requiresCodexClosed": True,
        "blockedByRunningCodex": bool(runtime_warnings),
        "blockingErrors": list(file_safety["blockingErrors"]),
        "warnings": warnings,
        "canApply": not file_safety["blockingErrors"] and not runtime_warnings,
    }


def apply_thread_workspace_move_to_state_data(
    data: dict[str, Any],
    source_path: str,
    target_path: str,
    thread_ids: list[str],
    preserve_pinned: bool = False,
    make_active: bool = True,
) -> int:
    before_marker = manifest_list_marker(data)
    updated_data, replacement_count = replace_paths_in_state_value(data, state_path_replacement_variants(source_path, target_path))
    if isinstance(updated_data, dict):
        data.clear()
        data.update(updated_data)

    target_path_text = normalize_path_text(target_path)
    thread_id_set = set(thread_ids)

    def update_container(container: dict[str, Any]) -> None:
        for key in ("electron-saved-workspace-roots", "project-order"):
            values = container.get(key)
            if not isinstance(values, list):
                values = []
            values = [value for value in values if comparable_path_text(value) != comparable_path_text(target_path_text)]
            container[key] = unique_path_state_list([target_path_text, *values])
        if make_active:
            container["active-workspace-roots"] = [target_path_text]

        projectless = container.get("projectless-thread-ids")
        if isinstance(projectless, list):
            container["projectless-thread-ids"] = [value for value in projectless if value not in thread_id_set]
        if not preserve_pinned:
            pinned = container.get("pinned-thread-ids")
            if isinstance(pinned, list):
                container["pinned-thread-ids"] = [value for value in pinned if value not in thread_id_set]

        hints = container.get("thread-workspace-root-hints")
        if not isinstance(hints, dict):
            hints = {}
            container["thread-workspace-root-hints"] = hints
        for matched_thread_id in thread_ids:
            hints[matched_thread_id] = target_path_text

    for container in [data, data.get("electron-persisted-atom-state")]:
        if isinstance(container, dict):
            update_container(container)
    ensure_project_label_in_state(data, target_path_text)
    after_marker = manifest_list_marker(data)
    return replacement_count + (1 if before_marker != after_marker else 0)


def update_thread_workspace_move_state(
    paths: CodexPaths,
    source_path: str,
    target_path: str,
    thread_ids: list[str],
    preserve_pinned: bool = False,
) -> dict[str, Any]:
    def updater(data: dict[str, Any]) -> dict[str, Any]:
        change_count = apply_thread_workspace_move_to_state_data(
            data,
            source_path,
            target_path,
            thread_ids,
            preserve_pinned=preserve_pinned,
            make_active=True,
        )
        data["__codex_home_manager_change_count__"] = change_count
        return data

    state_result = update_json_state_file(paths.global_state_path, updater, create_if_missing=True)
    backup_result = update_json_state_file(paths.global_state_backup_path, updater)
    return {
        "state": state_result,
        "backup": backup_result,
        "changeCount": int(state_result.get("changeCount") or 0) + int(backup_result.get("changeCount") or 0),
    }


def move_workspace_top_level_items(source_path: Path, target_path: Path) -> dict[str, Any]:
    safety = workspace_move_safety(source_path, target_path, move_workspace_files=True)
    if safety["blockingErrors"]:
        raise RuntimeError("; ".join(safety["blockingErrors"]))
    source_children = [source_path / name for name in safety["sourceTopLevelNames"]]
    if not source_children:
        return {
            "status": "no_change",
            "movedTopLevelNames": [],
            "sourceRemainingTopLevelNames": direct_child_names(source_path),
            "targetTopLevelNames": direct_child_names(target_path),
        }
    target_path.mkdir(parents=True, exist_ok=True)
    moved_names: list[str] = []
    for child in source_children:
        destination = target_path / child.name
        shutil.move(str(child), str(destination))
        moved_names.append(child.name)
    remaining_names = direct_child_names(source_path)
    if remaining_names:
        raise RuntimeError(f"source workspace still has top-level items after move: {remaining_names}")
    return {
        "status": "applied",
        "movedTopLevelNames": moved_names,
        "sourceRemainingTopLevelNames": remaining_names,
        "targetTopLevelNames": direct_child_names(target_path),
    }


def backup_workspace_move_rollouts(
    backup_manifest: dict[str, Any],
    matched_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    backup_id = backup_manifest.get("backupId")
    if not backup_id:
        return backup_manifest, []
    backup_directory = backup_directory_path(str(backup_id))
    matched_threads_path = backup_directory / "matched_threads.json"
    matched_threads_path.write_text(json.dumps(matched_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    rollout_backup_directory = backup_directory / "workspace_move_rollouts"
    rollout_backup_directory.mkdir(parents=True, exist_ok=True)
    rollout_backups: list[dict[str, str]] = []
    for matched_row in matched_rows:
        rollout_path = Path(normalize_path_text(matched_row.get("rollout_path")))
        if not rollout_path.exists():
            continue
        backup_path = rollout_backup_directory / f"{matched_row['id']}_{rollout_path.name}"
        shutil.copy2(rollout_path, backup_path)
        rollout_backups.append({"threadId": str(matched_row["id"]), "source": str(rollout_path), "backup": str(backup_path)})
    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "matchedThreadsPath": str(matched_threads_path),
            "matchedThreadIds": [str(row["id"]) for row in matched_rows],
            "workspaceMoveRolloutBackups": rollout_backups,
            "restoreMode": "thread_workspace_move_state_rollouts_files",
        },
    )
    return backup_manifest, rollout_backups


def record_workspace_move_journal(
    backup_manifest: dict[str, Any],
    *,
    stage: str,
    source_path: Path,
    target_path: Path,
    moved_names: list[str],
    error: str | None = None,
) -> dict[str, Any]:
    backup_id = str(backup_manifest.get("backupId") or "")
    if not backup_id:
        raise RuntimeError("workspace move requires a retained transaction backup")
    manifest_path = backup_manifest_path(backup_id)
    current_manifest = read_verified_backup_manifest(manifest_path)
    journal_path = manifest_path.parent / "workspace_move_journal.json"
    journal_payload = {
        "schemaVersion": 1,
        "backupId": backup_id,
        "stage": stage,
        "sourceProjectPath": str(source_path),
        "targetProjectPath": str(target_path),
        "movedTopLevelNames": list(moved_names),
        "error": error,
        "updatedAt": datetime_module.datetime.now(datetime_module.UTC).isoformat(),
    }
    temporary_path = journal_path.with_name(f"{journal_path.name}.{os.getpid()}.writing")
    temporary_path.write_text(json.dumps(journal_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary_path, journal_path)
    current_manifest.update(
        {
            "transactionJournalPath": str(journal_path),
            "transactionStage": stage,
            "transactionMovedTopLevelNames": list(moved_names),
            "transactionError": error,
        }
    )
    return write_sealed_backup_manifest(manifest_path, current_manifest)


def rollback_workspace_move_transaction(
    paths: CodexPaths,
    backup_manifest: dict[str, Any],
    source_path: Path,
    target_path: Path,
    moved_names: list[str],
) -> list[str]:
    rollback_notes: list[str] = []
    source_path.mkdir(parents=True, exist_ok=True)
    for moved_name in reversed(moved_names):
        source_item = source_path / moved_name
        target_item = target_path / moved_name
        if target_item.exists() and source_item.exists():
            raise RuntimeError(f"workspace rollback conflict for {moved_name}")
        if target_item.exists():
            shutil.move(str(target_item), str(source_item))
            rollback_notes.append(f"restored workspace item {moved_name}")

    database_backup_text = normalize_path_text(backup_manifest.get("databaseBackupPath"))
    if database_backup_text:
        database_backup_path = Path(database_backup_text)
        if not database_backup_path.is_file():
            raise RuntimeError("workspace rollback database backup is missing")
        shutil.copy2(database_backup_path, paths.database_path)
        rollback_notes.append("restored SQLite database")

    core_restore_pairs = [
        (backup_manifest.get("globalStateBackupPath"), paths.global_state_path, "global state"),
        (backup_manifest.get("globalStateBakBackupPath"), paths.global_state_backup_path, "global state backup"),
        (backup_manifest.get("configBackupPath"), paths.config_path, "config"),
        (backup_manifest.get("sessionIndexBackupPath"), paths.session_index_path, "session index"),
    ]
    for backup_text, target_file, label in core_restore_pairs:
        normalized_backup = normalize_path_text(backup_text)
        if normalized_backup:
            backup_file = Path(normalized_backup)
            if not backup_file.is_file():
                raise RuntimeError(f"workspace rollback {label} backup is missing")
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_file, target_file)
            rollback_notes.append(f"restored {label}")

    for rollout_backup in backup_manifest.get("workspaceMoveRolloutBackups") or []:
        backup_path = Path(normalize_path_text(rollout_backup.get("backup")))
        target_rollout = Path(normalize_path_text(rollout_backup.get("source")))
        if not backup_path.is_file():
            raise RuntimeError(f"workspace rollback rollout backup is missing: {backup_path}")
        target_rollout.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, target_rollout)
        rollback_notes.append(f"restored rollout {rollout_backup.get('threadId')}")
    return rollback_notes


def move_thread_workspace(
    codex_home_text: str | None,
    thread_id: str,
    target_project_path: str,
    include_same_source_cwd_threads: bool = True,
    move_workspace_files: bool = True,
    repair_user_event: bool = True,
    preserve_pinned: bool = False,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_thread_migration_safety(acknowledge_codex_running_risk)
    row = fetch_thread_row(paths, thread_id)
    if row is None:
        raise KeyError(thread_id)
    source_cwd = normalize_path_text(row.get("cwd"))
    target_cwd = normalize_path_text(target_project_path)
    if not source_cwd or not target_cwd:
        raise ValueError("source and target project paths are required")
    source_path = Path(source_cwd)
    target_path = Path(target_cwd)
    matched_rows = rows_for_exact_cwd(paths, source_cwd, include_archived=True) if include_same_source_cwd_threads else [row]
    if not matched_rows:
        raise ValueError("no threads matched source cwd")
    matched_thread_ids = [str(matched_row["id"]) for matched_row in matched_rows]
    file_safety = workspace_move_safety(source_path, target_path, move_workspace_files)
    if file_safety["blockingErrors"]:
        raise RuntimeError("; ".join(file_safety["blockingErrors"]))
    if not create_backup:
        raise ValueError("workspace move requires createBackup=true for transactional rollback")

    backup_manifest = create_optional_action_backup(paths, thread_id, action="move_thread_workspace", create_backup=create_backup)
    backup_manifest, rollout_backups = backup_workspace_move_rollouts(backup_manifest, matched_rows)
    source_top_level_names = list(file_safety.get("sourceTopLevelNames") or [])
    moved_names: list[str] = []
    file_move_result: dict[str, Any] = {"status": "skipped", "movedTopLevelNames": []}
    global_state_result: dict[str, Any] = {}
    config_rewrite: dict[str, Any] = {}
    rollout_rewrites: list[dict[str, Any]] = []
    backup_manifest = record_workspace_move_journal(
        backup_manifest,
        stage="prepared",
        source_path=source_path,
        target_path=target_path,
        moved_names=moved_names,
    )
    try:
        file_move_result = (
            move_workspace_top_level_items(source_path, target_path)
            if move_workspace_files
            else {"status": "skipped", "movedTopLevelNames": []}
        )
        moved_names = list(file_move_result.get("movedTopLevelNames") or [])
        backup_manifest = record_workspace_move_journal(
            backup_manifest,
            stage="files_moved",
            source_path=source_path,
            target_path=target_path,
            moved_names=moved_names,
        )
        if fault_injector:
            fault_injector("after_file_move")

        database_cwd = "\\\\?\\" + target_cwd if re.match(r"^[A-Za-z]:\\", target_cwd) else target_cwd
        with connect_database(paths.database_path, readonly=False) as connection:
            if repair_user_event:
                connection.executemany(
                    "UPDATE threads SET cwd = ?, has_user_event = 1 WHERE id = ?",
                    [(database_cwd, matched_thread_id) for matched_thread_id in matched_thread_ids],
                )
            else:
                connection.executemany(
                    "UPDATE threads SET cwd = ? WHERE id = ?",
                    [(database_cwd, matched_thread_id) for matched_thread_id in matched_thread_ids],
                )
            connection.commit()
        checkpoint_database(paths.database_path)
        backup_manifest = record_workspace_move_journal(
            backup_manifest,
            stage="database_updated",
            source_path=source_path,
            target_path=target_path,
            moved_names=moved_names,
        )
        if fault_injector:
            fault_injector("after_database")

        global_state_result = update_thread_workspace_move_state(
            paths,
            str(source_path),
            str(target_path),
            matched_thread_ids,
            preserve_pinned=preserve_pinned,
        )
        backup_manifest = record_workspace_move_journal(
            backup_manifest,
            stage="global_state_updated",
            source_path=source_path,
            target_path=target_path,
            moved_names=moved_names,
        )
        if fault_injector:
            fault_injector("after_global_state")

        config_rewrite = replace_paths_in_text_file(paths.config_path, str(source_path), str(target_path))
        backup_manifest = record_workspace_move_journal(
            backup_manifest,
            stage="config_updated",
            source_path=source_path,
            target_path=target_path,
            moved_names=moved_names,
        )
        if fault_injector:
            fault_injector("after_config")

        for matched_row in matched_rows:
            rollout_path = Path(normalize_path_text(matched_row.get("rollout_path")))
            rollout_rewrites.append(
                update_rollout_workspace_metadata(
                    rollout_path,
                    str(matched_row["id"]),
                    normalize_path_text(matched_row.get("cwd")),
                    target_cwd,
                )
            )
        backup_manifest = record_workspace_move_journal(
            backup_manifest,
            stage="rollouts_updated",
            source_path=source_path,
            target_path=target_path,
            moved_names=moved_names,
        )
        if fault_injector:
            fault_injector("after_rollouts")
    except Exception as operation_error:
        if move_workspace_files and not moved_names:
            moved_names = [
                name
                for name in source_top_level_names
                if not (source_path / name).exists() and (target_path / name).exists()
            ]
        try:
            backup_manifest = record_workspace_move_journal(
                backup_manifest,
                stage="rollback_started",
                source_path=source_path,
                target_path=target_path,
                moved_names=moved_names,
                error=str(operation_error),
            )
            rollback_notes = rollback_workspace_move_transaction(
                paths,
                backup_manifest,
                source_path,
                target_path,
                moved_names,
            )
            record_workspace_move_journal(
                backup_manifest,
                stage="rolled_back",
                source_path=source_path,
                target_path=target_path,
                moved_names=moved_names,
                error=str(operation_error),
            )
        except Exception as rollback_error:
            raise RuntimeError(
                f"workspace move failed: {operation_error}; automatic rollback failed: {rollback_error}"
            ) from operation_error
        raise

    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "sourceProjectPath": str(source_path),
            "targetProjectPath": str(target_path),
            "includeSameSourceCwdThreads": include_same_source_cwd_threads,
            "moveWorkspaceFiles": move_workspace_files,
            "repairUserEvent": repair_user_event,
            "preservePinned": preserve_pinned,
            "fileMove": file_move_result,
            "globalStateRewrite": global_state_result,
            "configRewrite": config_rewrite,
            "rolloutRewrites": rollout_rewrites,
        },
    )
    backup_manifest = record_workspace_move_journal(
        backup_manifest,
        stage="committed",
        source_path=source_path,
        target_path=target_path,
        moved_names=moved_names,
    )
    return {
        "threadId": thread_id,
        "sourceProjectPath": str(source_path),
        "targetProjectPath": str(target_path),
        "matchedThreads": len(matched_rows),
        "matchedThreadIds": matched_thread_ids,
        "updatedThreads": len(matched_rows),
        "fileMove": file_move_result,
        "rolloutBackups": rollout_backups,
        "rolloutRewrites": rollout_rewrites,
        "globalStateRewrite": global_state_result,
        "configRewrite": config_rewrite,
        "backup": backup_manifest,
        "warnings": warnings,
    }


def map_child_path(path_text: str, source_path: str, target_path: str) -> str:
    normalized_path = normalize_path_text(path_text)
    normalized_source = normalize_path_text(source_path)
    normalized_target = normalize_path_text(target_path)
    if comparable_path_text(normalized_path) == comparable_path_text(normalized_source):
        return normalized_target
    source_marker = comparable_path_text(normalized_source) + os.sep
    path_marker = comparable_path_text(normalized_path)
    if path_marker.startswith(source_marker):
        relative_text = normalized_path[len(normalized_source):].lstrip("\\/")
        return str(Path(normalized_target) / relative_text)
    return normalized_path


state_path_list_keys = {
    "active-workspace-roots",
    "electron-saved-workspace-roots",
    "pinned-project-ids",
    "project-order",
}


def unique_path_state_list(values: list[Any]) -> list[Any]:
    output: list[Any] = []
    seen_values: set[str] = set()
    for value in values:
        if isinstance(value, str):
            key = comparable_path_text(value)
        else:
            key = manifest_list_marker(value)
        if key in seen_values:
            continue
        seen_values.add(key)
        output.append(value)
    return output


def ensure_project_label_in_state(data: dict[str, Any], target_path: str) -> int:
    changed_count = 0
    target_label = path_label(target_path)
    for container in [data, data.get("electron-persisted-atom-state")]:
        if not isinstance(container, dict):
            continue
        labels = container.get("electron-workspace-root-labels")
        if not isinstance(labels, dict):
            labels = {}
            container["electron-workspace-root-labels"] = labels
            changed_count += 1
        if labels.get(target_path) != target_label:
            labels[target_path] = target_label
            changed_count += 1
    return changed_count


def replace_paths_in_state_value(value: Any, replacements: list[tuple[str, str]], key_name: str | None = None) -> tuple[Any, int]:
    if isinstance(value, str):
        return replace_paths_in_text(value, replacements)
    if isinstance(value, list):
        changed_count = 0
        output: list[Any] = []
        for item in value:
            updated_item, item_changes = replace_paths_in_state_value(item, replacements)
            changed_count += item_changes
            output.append(updated_item)
        if key_name in state_path_list_keys:
            deduped_output = unique_path_state_list(output)
            if len(deduped_output) != len(output):
                changed_count += len(output) - len(deduped_output)
            output = deduped_output
        return output, changed_count
    if isinstance(value, dict):
        changed_count = 0
        output: dict[str, Any] = {}
        for item_key, item_value in value.items():
            updated_key, key_changes = replace_paths_in_text(str(item_key), replacements)
            updated_value, value_changes = replace_paths_in_state_value(item_value, replacements, updated_key)
            changed_count += key_changes + value_changes
            output[updated_key] = updated_value
        return output, changed_count
    return value, 0


def replace_paths_in_state_file(path: Path, source_path: str, target_path: str) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "status": "missing", "replacementCount": 0}
    before_stat = path.stat()
    data = json.loads(path.read_text(encoding="utf-8"))
    updated_data, replacement_count = replace_paths_in_state_value(data, state_path_replacement_variants(source_path, target_path))
    if isinstance(updated_data, dict):
        replacement_count += ensure_project_label_in_state(updated_data, normalize_path_text(target_path))
    current_stat = path.stat()
    if current_stat.st_size != before_stat.st_size or current_stat.st_mtime_ns != before_stat.st_mtime_ns:
        raise RuntimeError(f"{path} changed during state rewrite")
    if replacement_count:
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(
            json.dumps(updated_data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
            newline="",
        )
        replace_file_with_retry(temp_path, path)
    return {"path": str(path), "status": "applied" if replacement_count else "no_change", "replacementCount": replacement_count}


def replace_paths_in_global_state(paths: CodexPaths, source_path: str, target_path: str) -> dict[str, Any]:
    state_result = replace_paths_in_state_file(paths.global_state_path, source_path, target_path)
    backup_result = replace_paths_in_state_file(paths.global_state_backup_path, source_path, target_path)
    return {
        "state": state_result,
        "backup": backup_result,
        "replacementCount": int(state_result.get("replacementCount") or 0) + int(backup_result.get("replacementCount") or 0),
    }


def replace_paths_in_text_file(path: Path, source_path: str, target_path: str) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "replacementCount": 0}
    before_stat = path.stat()
    text = path.read_text(encoding="utf-8", errors="replace")
    updated_text, replacement_count = replace_paths_in_text(text, path_replacement_variants(source_path, target_path))
    current_stat = path.stat()
    if current_stat.st_size != before_stat.st_size or current_stat.st_mtime_ns != before_stat.st_mtime_ns:
        raise RuntimeError(f"{path} changed during rewrite")
    if replacement_count:
        path.write_text(updated_text, encoding="utf-8", newline="")
    return {"status": "applied" if replacement_count else "no_change", "replacementCount": replacement_count}


def rename_project(
    codex_home_text: str | None,
    source_project_path: str,
    target_project_path: str,
    rename_folder: bool = True,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_project_rename_safety(acknowledge_codex_running_risk)
    source_path = Path(normalize_path_text(source_project_path))
    target_path = Path(normalize_path_text(target_project_path))
    if comparable_path_text(str(source_path)) == comparable_path_text(str(target_path)):
        raise ValueError("source and target project paths are identical")
    matching_rows = [
        row
        for row in fetch_thread_rows(paths)
        if comparable_path_text(normalize_path_text(row.get("cwd"))) == comparable_path_text(str(source_path))
        or comparable_path_text(normalize_path_text(row.get("cwd"))).startswith(comparable_path_text(str(source_path)) + os.sep)
    ]
    if not matching_rows:
        raise ValueError("no threads matched source project path")
    backup_manifest = create_optional_action_backup(paths, str(matching_rows[0]["id"]), action="rename_project", create_backup=create_backup)
    backup_id = backup_manifest.get("backupId")
    extra_backup_path = None
    if backup_id:
        extra_backup_path = backup_directory_path(str(backup_id)) / "matched_threads.json"
        extra_backup_path.write_text(json.dumps(matching_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    rollout_backups: list[dict[str, str]] = []
    if backup_id:
        rollout_backup_directory = backup_directory_path(str(backup_id)) / "project_rollouts"
        rollout_backup_directory.mkdir(parents=True, exist_ok=True)
        for row in matching_rows:
            rollout_path = Path(normalize_path_text(row.get("rollout_path")))
            if rollout_path.exists():
                backup_path = rollout_backup_directory / f"{row['id']}_{rollout_path.name}"
                shutil.copy2(rollout_path, backup_path)
                rollout_backups.append({"threadId": str(row["id"]), "source": str(rollout_path), "backup": str(backup_path)})

    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "matchedThreadsPath": str(extra_backup_path) if extra_backup_path else None,
            "matchedThreadIds": [str(row["id"]) for row in matching_rows],
            "projectRolloutBackups": rollout_backups,
            "folderRename": {
                "requested": rename_folder,
                "renamed": False,
                "sourcePath": str(source_path),
                "targetPath": str(target_path),
            },
            "restoreMode": "project_rename_all_threads_global_config_rollouts_folder",
        },
    )

    folder_renamed = False
    if rename_folder:
        if source_path.exists() and target_path.exists():
            raise FileExistsError(f"target project folder already exists: {target_path}")
        if source_path.exists() and not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.rename(target_path)
            folder_renamed = True
            backup_manifest = update_optional_backup_manifest(
                backup_manifest,
                {
                    "folderRename": {
                        "requested": rename_folder,
                        "renamed": True,
                        "sourcePath": str(source_path),
                        "targetPath": str(target_path),
                    },
                },
            )
        elif target_path.exists():
            warnings.append("source project folder was already missing and target folder already exists; only metadata was rewritten")
        else:
            raise FileNotFoundError(str(source_path))

    updates: list[tuple[str, str]] = []
    for row in matching_rows:
        old_cwd = normalize_path_text(row.get("cwd"))
        new_cwd = map_child_path(old_cwd, str(source_path), str(target_path))
        database_cwd = "\\\\?\\" + new_cwd if re.match(r"^[A-Za-z]:\\", new_cwd) else new_cwd
        updates.append((database_cwd, str(row["id"])))
        rollout_path = Path(normalize_path_text(row.get("rollout_path")))
        if rollout_path.exists():
            replace_paths_in_jsonl(
                rollout_path,
                path_replacement_variants(str(source_path), str(target_path)),
            )

    with connect_database(paths.database_path, readonly=False) as connection:
        connection.executemany("UPDATE threads SET cwd = ? WHERE id = ?", updates)
        connection.commit()
    global_state_rewrite = replace_paths_in_global_state(paths, str(source_path), str(target_path))
    config_rewrite = replace_paths_in_text_file(paths.config_path, str(source_path), str(target_path))
    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "folderRename": {
                "requested": rename_folder,
                "renamed": folder_renamed,
                "sourcePath": str(source_path),
                "targetPath": str(target_path),
            },
            "globalStateRewrite": global_state_rewrite,
            "configRewrite": config_rewrite,
        },
    )
    return {
        "sourceProjectPath": str(source_path),
        "targetProjectPath": str(target_path),
        "renamedFolder": folder_renamed,
        "updatedThreads": len(updates),
        "rolloutBackups": rollout_backups,
        "globalStateRewrite": global_state_rewrite,
        "configRewrite": config_rewrite,
        "backup": backup_manifest,
        "warnings": warnings,
    }


def preview_project_rename(codex_home_text: str | None, source_project_path: str, target_project_path: str, rename_folder: bool = True) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    source_path = Path(normalize_path_text(source_project_path))
    target_path = Path(normalize_path_text(target_project_path))
    matching_rows = rows_for_project(paths, str(source_path), include_archived=True)
    rollout_paths = [Path(normalize_path_text(row.get("rollout_path"))) for row in matching_rows]
    existing_rollouts = [path for path in rollout_paths if path.exists()]
    warnings = write_safety_warnings()
    return {
        "sourceProjectPath": str(source_path),
        "targetProjectPath": str(target_path),
        "matchedThreads": len(matching_rows),
        "existingRollouts": len(existing_rollouts),
        "rolloutBytes": sum(path.stat().st_size for path in existing_rollouts),
        "willRenameFolder": bool(rename_folder and source_path.exists() and not target_path.exists()),
        "requiresCodexClosed": True,
        "blockedByRunningCodex": bool(warnings),
        "sourceFolder": collect_path_stats(source_path),
        "targetFolder": collect_path_stats(target_path),
        "warnings": warnings,
    }


drop_json_value = object()
valid_image_url_prefixes = ("http://", "https://", "data:image/")


def is_compacted_item(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "compacted"


def is_event_msg_item(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "event_msg"


def image_url_needs_slimming(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    normalized_value = value.strip().lower()
    if normalized_value.startswith("data:image/"):
        return True
    return not normalized_value.startswith(valid_image_url_prefixes)


def image_url_is_invalid(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    return not value.strip().lower().startswith(valid_image_url_prefixes)


def record_image_url_removal(value: Any, stats: dict[str, int]) -> None:
    if isinstance(value, str):
        encoded_size = len(value.encode("utf-8", errors="replace"))
        stats["removedImageBytes"] += encoded_size
        if value.strip().lower().startswith("data:image/"):
            stats["removedEmbeddedImageUrls"] += 1
        else:
            stats["removedInvalidImageUrls"] += 1
    else:
        stats["removedInvalidImageUrls"] += 1


def is_removable_image_object(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    item_type = str(value.get("type") or "").strip().lower()
    if item_type == "input_image":
        return image_url_needs_slimming(value.get("image_url"))
    return "image_url" in value and image_url_needs_slimming(value.get("image_url"))


def slim_json_value(value: Any, stats: dict[str, int], parent_key: str | None = None) -> Any:
    image_marker = "[image omitted from session history for performance; preserved in backup]"
    if isinstance(value, list):
        output: list[Any] = []
        for item in value:
            if is_removable_image_object(item):
                stats["removedInputImages"] += 1
                record_image_url_removal(item.get("image_url") if isinstance(item, dict) else None, stats)
                continue
            slimmed_item = slim_json_value(item, stats)
            if slimmed_item is drop_json_value:
                stats["removedNestedImageObjects"] += 1
                continue
            output.append(slimmed_item)
        return output
    if isinstance(value, dict):
        if is_removable_image_object(value):
            stats["removedImageObjects"] += 1
            record_image_url_removal(value.get("image_url"), stats)
            return drop_json_value
        output: dict[str, Any] = {}
        for key, item in value.items():
            if key == "encrypted_content":
                stats["preservedEncryptedContentFields"] += 1
                output[key] = item
                continue
            if key == "image_url" and image_url_needs_slimming(item):
                stats["replacedImageFields"] += 1
                record_image_url_removal(item, stats)
            else:
                slimmed_item = slim_json_value(item, stats, parent_key=key)
                if slimmed_item is drop_json_value:
                    stats["removedNestedImageObjects"] += 1
                    continue
                output[key] = slimmed_item
        return output
    if parent_key != "encrypted_content" and isinstance(value, str) and value.strip().lower().startswith("data:image/"):
        stats["replacedImageStrings"] += 1
        stats["removedImageBytes"] += len(value.encode("utf-8", errors="replace"))
        return image_marker
    return value


def collect_jsonl_schema_stats(value: Any, stats: dict[str, int]) -> None:
    if isinstance(value, list):
        for item in value:
            collect_jsonl_schema_stats(item, stats)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key == "encrypted_content":
            stats["encryptedContentFields"] += 1
            continue
        if key == "image_url":
            if isinstance(item, str) and item.strip().lower().startswith("data:image/"):
                stats["embeddedImageUrlFields"] += 1
            if isinstance(item, str) and image_url_is_invalid(item):
                stats["invalidImageUrlRefs"] += 1
            elif not isinstance(item, str) and isinstance(value.get("type"), str) and image_url_is_invalid(item):
                stats["invalidImageUrlRefs"] += 1
        collect_jsonl_schema_stats(item, stats)


def scan_jsonl(path: Path) -> dict[str, int]:
    result = {
        "lineCount": 0,
        "parseErrors": 0,
        "compactedCount": 0,
        "embeddedImageRefs": 0,
        "embeddedImageUrlFields": 0,
        "invalidImageUrlRefs": 0,
        "encryptedContentFields": 0,
        "totalBytes": 0,
    }
    with path.open("rb") as handle:
        for raw_line in handle:
            result["lineCount"] += 1
            result["totalBytes"] += len(raw_line)
            result["embeddedImageRefs"] += raw_line.count(b"data:image/")
            try:
                item = json.loads(raw_line)
            except Exception:
                result["parseErrors"] += 1
                continue
            if is_compacted_item(item):
                result["compactedCount"] += 1
            collect_jsonl_schema_stats(item, result)
    return result


def repair_compacted_checkpoint_message(item: Any) -> bool:
    if not is_compacted_item(item):
        return False
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return False
    if str(payload.get("message") or "").strip():
        return False
    first_prompt = ""
    for history_item in payload.get("replacement_history") or []:
        text = text_from_compacted_history_message(history_item)
        if is_real_user_prompt(text):
            first_prompt = re.sub(r"\s+", " ", text).strip()
            break
    if not first_prompt:
        return False
    payload["message"] = f"Compacted checkpoint preserved by Codex Home Manager. First user prompt: {first_prompt[:500]}"
    return True


def rollout_path_is_inside_codex_home(paths: CodexPaths, rollout_path: Path) -> bool:
    rollout_key = comparable_path_text(str(rollout_path))
    home_key = comparable_path_text(str(paths.codex_home_path))
    return bool(rollout_key and home_key and (rollout_key == home_key or rollout_key.startswith(home_key + os.sep)))


def rollout_session_meta_id(rollout_path: Path, max_lines: int = 80) -> str:
    with rollout_path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if line_number > max_lines:
                break
            try:
                item = json.loads(raw_line)
            except Exception:
                continue
            if item.get("type") != "session_meta" or not isinstance(item.get("payload"), dict):
                continue
            return str(item["payload"].get("id") or "").strip()
    return ""


def validate_rollout_thread_binding(paths: CodexPaths, row: dict[str, Any], rollout_path: Path) -> dict[str, Any]:
    thread_id = str(row.get("id") or "")
    filename_mentions_thread = bool(thread_id and thread_id in rollout_path.stem)
    session_meta_id = rollout_session_meta_id(rollout_path) if rollout_path.exists() else ""
    errors: list[str] = []
    if not rollout_path_is_inside_codex_home(paths, rollout_path):
        errors.append("rollout path is outside the selected CODEX_HOME")
    if session_meta_id and session_meta_id != thread_id:
        errors.append(f"session_meta id {session_meta_id} does not match requested thread id {thread_id}")
    if not session_meta_id and not filename_mentions_thread:
        errors.append("rollout filename does not contain the requested thread id and no session_meta id was found")
    if errors:
        raise RuntimeError("refusing to slim thread because rollout binding is inconsistent: " + "; ".join(errors))
    return {
        "threadId": thread_id,
        "rolloutPath": str(rollout_path),
        "filenameMentionsThread": filename_mentions_thread,
        "sessionMetaId": session_meta_id,
        "insideCodexHome": True,
    }


def preview_slim_thread(codex_home_text: str | None, thread_id: str) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    row = fetch_thread_row(paths, thread_id)
    if row is None:
        raise KeyError(thread_id)
    rollout_path = Path(normalize_path_text(row.get("rollout_path")))
    if not rollout_path.exists():
        raise FileNotFoundError(str(rollout_path))
    binding = validate_rollout_thread_binding(paths, row, rollout_path)
    scan = scan_jsonl(rollout_path)
    return {
        "threadId": thread_id,
        "rolloutPath": str(rollout_path),
        "binding": binding,
        "scan": scan,
        "canRemoveImages": scan["embeddedImageRefs"] > 0 or scan["invalidImageUrlRefs"] > 0,
        "canReduceCompacted": scan["compactedCount"] > 1,
        "warnings": write_safety_warnings(),
    }


def slim_thread(
    codex_home_text: str | None,
    thread_id: str,
    remove_images: bool = True,
    keep_latest_compacted: bool = True,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    row = fetch_thread_row(paths, thread_id)
    if row is None:
        raise KeyError(thread_id)
    backup_manifest = create_optional_action_backup(paths, thread_id, action="slim", create_backup=create_backup)
    rollout_path = Path(normalize_path_text(row.get("rollout_path")))
    if not rollout_path.exists():
        raise FileNotFoundError(str(rollout_path))
    binding = validate_rollout_thread_binding(paths, row, rollout_path)
    preserve_event_msg_lines = thread_id not in fetch_thread_spawn_edges(paths)
    before_stat = rollout_path.stat()
    before_scan = scan_jsonl(rollout_path)
    if before_scan["parseErrors"]:
        raise RuntimeError(f"source JSONL has parse errors; repair it before slimming: {before_scan['parseErrors']}")

    latest_compacted_line = 0
    if keep_latest_compacted:
        with rollout_path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                try:
                    item = json.loads(raw_line)
                except Exception:
                    continue
                if is_compacted_item(item):
                    latest_compacted_line = line_number

    stats = {
        "removedCompactedLines": 0,
        "repairedCompactedMessages": 0,
        "rewrittenImageLines": 0,
        "droppedImageLines": 0,
        "removedInputImages": 0,
        "removedImageObjects": 0,
        "removedNestedImageObjects": 0,
        "removedEmbeddedImageUrls": 0,
        "removedInvalidImageUrls": 0,
        "replacedImageFields": 0,
        "replacedImageStrings": 0,
        "preservedEncryptedContentFields": 0,
        "preservedMainThreadEventMsgLines": 0,
        "removedImageBytes": 0,
    }
    temp_path = rollout_path.with_name(rollout_path.name + ".slim.tmp")
    try:
        with rollout_path.open("rb") as source, temp_path.open("wb") as output:
            for line_number, raw_line in enumerate(source, 1):
                parsed_item: Any | None = None
                parsed = False

                def item_for_line() -> Any:
                    nonlocal parsed_item, parsed
                    if not parsed:
                        parsed_item = json.loads(raw_line)
                        parsed = True
                    return parsed_item

                if preserve_event_msg_lines and b"event_msg" in raw_line:
                    item = item_for_line()
                    if is_event_msg_item(item):
                        stats["preservedMainThreadEventMsgLines"] += 1
                        if remove_images and (b"data:image/" in raw_line or b"image_url" in raw_line or b"input_image" in raw_line):
                            line_stats_before = dict(stats)
                            slimmed_item = slim_json_value(item, stats)
                            line_changed = stats != line_stats_before
                            if slimmed_item is not drop_json_value and line_changed:
                                output.write((json.dumps(slimmed_item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                                stats["rewrittenImageLines"] += 1
                                continue
                        output.write(raw_line)
                        continue
                if keep_latest_compacted:
                    item = item_for_line()
                    if is_compacted_item(item) and line_number != latest_compacted_line:
                        stats["removedCompactedLines"] += 1
                        continue
                    if is_compacted_item(item) and repair_compacted_checkpoint_message(item):
                        stats["repairedCompactedMessages"] += 1
                        raw_line = (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                if remove_images and (b"data:image/" in raw_line or b"image_url" in raw_line or b"input_image" in raw_line):
                    item = item_for_line()
                    line_stats_before = dict(stats)
                    slimmed_item = slim_json_value(item, stats)
                    line_changed = stats != line_stats_before
                    if slimmed_item is drop_json_value:
                        stats["droppedImageLines"] += 1
                        stats["rewrittenImageLines"] += 1
                        continue
                    if line_changed:
                        output.write((json.dumps(slimmed_item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                        stats["rewrittenImageLines"] += 1
                        continue
                output.write(raw_line)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    validation = scan_jsonl(temp_path)
    if validation["parseErrors"]:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"slim output has parse errors: {validation['parseErrors']}")
    if remove_images and validation["invalidImageUrlRefs"]:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"slim output still has invalid image_url fields: {validation['invalidImageUrlRefs']}")
    if remove_images and validation["embeddedImageUrlFields"]:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"slim output still has embedded image_url data: {validation['embeddedImageUrlFields']}")
    current_stat = rollout_path.stat()
    if current_stat.st_size != before_stat.st_size or current_stat.st_mtime_ns != before_stat.st_mtime_ns:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("source JSONL changed during slimming")
    replace_file_with_retry(temp_path, rollout_path)
    after_scan = scan_jsonl(rollout_path)
    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "slimBinding": binding,
            "slimBefore": before_scan,
            "slimAfter": after_scan,
            "slimStats": stats,
            "restoreMode": backup_manifest.get("restoreMode") or "row_global_state_session_index_rollout",
        },
    )
    return {
        "threadId": thread_id,
        "backup": backup_manifest,
        "before": before_scan,
        "after": after_scan,
        "stats": stats,
        "savedBytes": before_scan["totalBytes"] - after_scan["totalBytes"],
        "warnings": warnings,
    }


def ensure_relative_resource_path(relative_path: str | None) -> str:
    text = (relative_path or "").strip().replace("/", "\\").strip("\\")
    if not text or text == ".":
        return ""
    candidate = Path(text)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError(f"resource path must stay inside CODEX_HOME: {relative_path}")
    return str(candidate)


def resolve_resource_path(paths: CodexPaths, relative_path: str | None) -> Path:
    safe_relative_path = ensure_relative_resource_path(relative_path)
    candidate_path = (paths.codex_home_path / safe_relative_path).resolve(strict=False)
    root_text = comparable_path_text(str(paths.codex_home_path.resolve(strict=False)))
    candidate_text = comparable_path_text(str(candidate_path))
    if candidate_text != root_text and not candidate_text.startswith(root_text + os.sep):
        raise ValueError(f"resource path escapes CODEX_HOME: {relative_path}")
    return candidate_path


blocked_text_write_exact_paths = {
    "state_5.sqlite",
    "logs_2.sqlite",
    "session_index.jsonl",
    "version.json",
    ".codex-global-state.json",
    "config.toml",
}

blocked_text_write_roots = {
    "sessions",
    "plugins",
    "generated_images",
    "automations",
}

editable_text_suffixes = {
    ".md",
    ".txt",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
    ".ini",
    ".env",
    ".gitignore",
}


allowed_resource_copy_roots = {
    "memories",
    "skills",
}

allowed_resource_copy_exact_paths = {
    "AGENTS.md",
}


def assert_resource_copy_allowed(relative_path: str | None) -> str:
    safe_relative_path = ensure_relative_resource_path(relative_path)
    comparable_relative_path = comparable_path_text(safe_relative_path)
    if not safe_relative_path:
        raise ValueError("resource path is required")
    if comparable_relative_path in {comparable_path_text(path) for path in blocked_text_write_exact_paths}:
        raise ValueError(f"resource copy is not allowed for protected Codex state file: {safe_relative_path}")
    first_part = safe_relative_path.split("\\", 1)[0]
    if comparable_path_text(first_part) in {comparable_path_text(path) for path in blocked_text_write_roots}:
        raise ValueError(f"resource copy is not allowed for protected Codex resource directory: {safe_relative_path}")
    if comparable_relative_path in {comparable_path_text(path) for path in allowed_resource_copy_exact_paths}:
        return safe_relative_path
    if comparable_path_text(first_part) in {comparable_path_text(path) for path in allowed_resource_copy_roots}:
        return safe_relative_path
    suffix = Path(safe_relative_path).suffix.lower()
    if suffix in editable_text_suffixes:
        return safe_relative_path
    raise ValueError(f"resource copy requires a safe text/config/memory/skill path: {relative_path}")


def assert_text_resource_write_allowed(paths: CodexPaths, relative_path: str | None, content: str) -> str:
    safe_relative_path = ensure_relative_resource_path(relative_path)
    comparable_relative_path = comparable_path_text(safe_relative_path)
    if not safe_relative_path:
        raise ValueError("resource path is required")
    if comparable_relative_path in {comparable_path_text(path) for path in blocked_text_write_exact_paths}:
        raise ValueError(f"text write is not allowed for protected Codex state file: {safe_relative_path}")
    first_part = safe_relative_path.split("\\", 1)[0]
    if comparable_path_text(first_part) in {comparable_path_text(path) for path in blocked_text_write_roots}:
        raise ValueError(f"text write is not allowed for protected Codex resource directory: {safe_relative_path}")
    target_path = resolve_resource_path(paths, safe_relative_path)
    suffix = target_path.suffix.lower()
    if target_path.name == "AGENTS.md":
        return safe_relative_path
    if suffix not in editable_text_suffixes:
        raise ValueError(f"text write requires an editable text extension: {safe_relative_path}")
    if "\x00" in content[:4096]:
        raise ValueError("text resource content contains NUL bytes")
    return safe_relative_path


def collect_path_stats(path: Path, max_entries: int = 20000) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "kind": "missing",
            "sizeBytes": 0,
            "fileCount": 0,
            "directoryCount": 0,
            "truncated": False,
            "modifiedAtMs": None,
        }
    if path.is_file():
        stat_result = path.stat()
        return {
            "exists": True,
            "kind": "file",
            "sizeBytes": int(stat_result.st_size),
            "fileCount": 1,
            "directoryCount": 0,
            "truncated": False,
            "modifiedAtMs": int(stat_result.st_mtime * 1000),
        }
    total_size = 0
    file_count = 0
    directory_count = 0
    entry_count = 0
    latest_modified_at_ms = int(path.stat().st_mtime * 1000)
    stack = [path]
    truncated = False
    while stack:
        current_path = stack.pop()
        try:
            entries = list(os.scandir(current_path))
        except OSError:
            continue
        for entry in entries:
            entry_count += 1
            if entry_count > max_entries:
                truncated = True
                stack.clear()
                break
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            latest_modified_at_ms = max(latest_modified_at_ms, int(entry_stat.st_mtime * 1000))
            if entry.is_dir(follow_symlinks=False):
                directory_count += 1
                stack.append(Path(entry.path))
            else:
                file_count += 1
                total_size += int(entry_stat.st_size)
    return {
        "exists": True,
        "kind": "directory",
        "sizeBytes": total_size,
        "fileCount": file_count,
        "directoryCount": directory_count,
        "truncated": truncated,
        "modifiedAtMs": latest_modified_at_ms,
    }


def preview_resource_copy(
    target_codex_home_text: str | None,
    source_codex_home_text: str,
    relative_path: str,
    target_relative_path: str | None = None,
) -> dict[str, Any]:
    safe_relative_path = assert_resource_copy_allowed(relative_path)
    safe_target_relative_path = assert_resource_copy_allowed(target_relative_path or relative_path)
    source_paths = resolve_codex_paths(source_codex_home_text)
    target_paths = resolve_codex_paths(target_codex_home_text)
    source_path = resolve_resource_path(source_paths, safe_relative_path)
    target_path = resolve_resource_path(target_paths, safe_target_relative_path)
    return {
        "sourceCodexHome": str(source_paths.codex_home_path),
        "targetCodexHome": str(target_paths.codex_home_path),
        "sourcePath": str(source_path),
        "targetPath": str(target_path),
        "source": collect_path_stats(source_path),
        "target": collect_path_stats(target_path),
        "willOverwrite": target_path.exists(),
        "warnings": write_safety_warnings(),
    }


def resource_record(paths: CodexPaths, relative_path: str, label: str, category: str, description: str) -> dict[str, Any]:
    target_path = resolve_resource_path(paths, relative_path)
    stats = collect_path_stats(target_path)
    return {
        "relativePath": ensure_relative_resource_path(relative_path),
        "path": str(target_path),
        "label": label,
        "category": category,
        "description": description,
        **stats,
    }


def find_agents_files(paths: CodexPaths, max_files: int = 100) -> list[dict[str, Any]]:
    agents_files: list[dict[str, Any]] = []
    skipped_directories = {"node_modules", ".git", "plugins", "generated_images"}
    for root_path, directory_names, file_names in os.walk(paths.codex_home_path):
        directory_names[:] = [name for name in directory_names if name not in skipped_directories]
        if "AGENTS.md" not in file_names:
            continue
        file_path = Path(root_path) / "AGENTS.md"
        try:
            relative_path = str(file_path.relative_to(paths.codex_home_path))
        except ValueError:
            continue
        agents_files.append(resource_record(paths, relative_path, "AGENTS.md", "instructions", "Codex agent instruction file"))
        if len(agents_files) >= max_files:
            break
    return agents_files


def codex_home_overview(codex_home_text: str | None = None) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    known_resources = [
        ("config.toml", "config.toml", "config", "Codex configuration and trusted projects"),
        (".codex-global-state.json", "global state", "state", "Codex Desktop sidebar, project and pin state"),
        ("state_5.sqlite", "state_5.sqlite", "state", "Thread metadata SQLite database"),
        ("logs_2.sqlite", "logs_2.sqlite", "logs", "Codex local logs database"),
        ("session_index.jsonl", "session_index.jsonl", "state", "Session index JSONL"),
        ("sessions", "sessions", "threads", "Thread rollout JSONL storage"),
        ("memories", "memories", "memory", "Memory registry, rollout summaries and saved guidance"),
        ("skills", "skills", "skills", "Local user skills"),
        ("automations", "automations", "automation", "Codex recurring automations"),
        ("plugins", "plugins", "plugins", "Installed plugin cache and metadata"),
        ("generated_images", "generated_images", "media", "Generated image artifacts"),
        ("AGENTS.md", "AGENTS.md", "instructions", "Root Codex Home instructions if present"),
    ]
    resources = [resource_record(paths, relative_path, label, category, description) for relative_path, label, category, description in known_resources]
    agents_files = find_agents_files(paths)
    resources_by_path = {resource["relativePath"].lower(): resource for resource in resources}
    for agents_file in agents_files:
        resources_by_path.setdefault(agents_file["relativePath"].lower(), agents_file)
    resources = list(resources_by_path.values())
    total_size = sum(resource["sizeBytes"] for resource in resources if resource["exists"])
    return {
        "codexHome": str(paths.codex_home_path),
        "resources": resources,
        "summary": {
            "resourceCount": len(resources),
            "existingResourceCount": sum(1 for resource in resources if resource["exists"]),
            "totalKnownResourceBytes": total_size,
            "agentsFileCount": len(agents_files),
            "memoryExists": (paths.codex_home_path / "memories").exists(),
            "skillsExists": (paths.codex_home_path / "skills").exists(),
        },
        "generatedAtMs": int(time.time() * 1000),
    }


def list_resource_children(codex_home_text: str | None, relative_path: str | None, max_children: int = 300) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    target_path = resolve_resource_path(paths, relative_path)
    if not target_path.exists():
        raise FileNotFoundError(str(target_path))
    if not target_path.is_dir():
        raise NotADirectoryError(str(target_path))
    children: list[dict[str, Any]] = []
    for child_path in sorted(target_path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:max_children]:
        relative_child = str(child_path.relative_to(paths.codex_home_path))
        children.append(resource_record(paths, relative_child, child_path.name, "child", "Child resource"))
    return {
        "relativePath": ensure_relative_resource_path(relative_path),
        "path": str(target_path),
        "children": children,
        "truncated": len(children) >= max_children,
    }


def read_codex_resource(codex_home_text: str | None, relative_path: str | None, max_bytes: int = 300000) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    target_path = resolve_resource_path(paths, relative_path)
    if not target_path.exists():
        raise FileNotFoundError(str(target_path))
    metadata = resource_record(paths, str(target_path.relative_to(paths.codex_home_path)), target_path.name, "resource", "Selected resource")
    if target_path.is_dir():
        return {
            "metadata": metadata,
            **list_resource_children(codex_home_text, str(target_path.relative_to(paths.codex_home_path))),
            "content": None,
        }
    with target_path.open("rb") as handle:
        content_bytes = handle.read(max_bytes + 1)
    truncated = len(content_bytes) > max_bytes
    if truncated:
        content_bytes = content_bytes[:max_bytes]
    if b"\x00" in content_bytes[:4096]:
        return {
            "metadata": metadata,
            "content": None,
            "truncated": truncated,
            "binary": True,
        }
    return {
        "metadata": metadata,
        "content": content_bytes.decode("utf-8", errors="replace"),
        "truncated": truncated,
        "binary": False,
    }


def copy_path_to_backup(source_path: Path, backup_path: Path) -> None:
    if source_path.is_dir():
        shutil.copytree(source_path, backup_path, dirs_exist_ok=True)
    else:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, backup_path)


def backup_codex_resource(codex_home_text: str | None, relative_path: str | None) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    target_path = resolve_resource_path(paths, relative_path)
    if not target_path.exists():
        raise FileNotFoundError(str(target_path))
    manifest = create_home_state_backup(paths, action="resource_backup", subject=ensure_relative_resource_path(relative_path) or "root")
    backup_directory = backup_root_path() / manifest["backupId"] / "resources"
    backup_target = backup_directory / safe_backup_fragment(ensure_relative_resource_path(relative_path) or "root")
    copy_path_to_backup(target_path, backup_target)
    manifest = update_backup_manifest(
        manifest["backupId"],
        {
            "resourceBackups": [{"target": str(target_path), "backup": str(backup_target)}],
            "restoreMode": "resource_backup",
        },
    )
    return {"backup": manifest, "resourcePath": str(target_path), "resourceBackupPath": str(backup_target), "warnings": write_safety_warnings()}


def write_codex_resource(
    codex_home_text: str | None,
    relative_path: str | None,
    content: str,
    create_parent_directories: bool = True,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    safe_relative_path = assert_text_resource_write_allowed(paths, relative_path, content)
    target_path = resolve_resource_path(paths, safe_relative_path)
    if target_path.exists() and target_path.is_dir():
        raise IsADirectoryError(str(target_path))
    manifest = create_optional_home_state_backup(paths, action="write_resource", subject=safe_relative_path, create_backup=create_backup)
    resource_backups: list[dict[str, str]] = []
    backup_id = manifest.get("backupId")
    if target_path.exists() and backup_id:
        backup_target = backup_directory_path(str(backup_id)) / "resources_before" / safe_backup_fragment(target_path.name)
        copy_path_to_backup(target_path, backup_target)
        resource_backups.append({"target": str(target_path), "backup": str(backup_target)})
    created_resource_paths = [] if target_path.exists() else [str(target_path)]
    manifest = update_optional_backup_manifest(
        manifest,
        {
            "resourceBackups": resource_backups,
            "createdResourcePaths": created_resource_paths,
            "writtenResourcePath": str(target_path),
            "restoreMode": "write_resource",
        },
    )
    if create_parent_directories:
        target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8", newline="")
    manifest = update_optional_backup_manifest(
        manifest,
        {
            "writtenSizeBytes": target_path.stat().st_size,
        },
    )
    return {
        "resourcePath": str(target_path),
        "sizeBytes": target_path.stat().st_size,
        "backup": manifest,
        "warnings": warnings,
    }


def preview_write_codex_resource(
    codex_home_text: str | None,
    relative_path: str | None,
    content: str,
    create_parent_directories: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    safe_relative_path = assert_text_resource_write_allowed(paths, relative_path, content)
    target_path = resolve_resource_path(paths, safe_relative_path)
    if target_path.exists() and target_path.is_dir():
        raise IsADirectoryError(str(target_path))
    target_stat = collect_path_stats(target_path)
    return {
        "targetCodexHome": str(paths.codex_home_path),
        "targetPath": str(target_path),
        "target": {"path": str(target_path), **target_stat},
        "willOverwrite": target_path.exists(),
        "contentBytes": len(content.encode("utf-8")),
        "createParentDirectories": create_parent_directories,
        "warnings": write_safety_warnings(),
    }


def copy_resource_from_home(
    target_codex_home_text: str | None,
    source_codex_home_text: str,
    relative_path: str,
    target_relative_path: str | None = None,
    overwrite: bool = False,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    safe_relative_path = assert_resource_copy_allowed(relative_path)
    safe_target_relative_path = assert_resource_copy_allowed(target_relative_path or relative_path)
    source_paths = resolve_codex_paths(source_codex_home_text)
    target_paths = resolve_codex_paths(target_codex_home_text)
    source_path = resolve_resource_path(source_paths, safe_relative_path)
    target_path = resolve_resource_path(target_paths, safe_target_relative_path)
    if not source_path.exists():
        raise FileNotFoundError(str(source_path))
    if target_path.exists() and not overwrite:
        raise FileExistsError(f"target resource exists: {target_path}")
    manifest = create_optional_home_state_backup(
        target_paths,
        action="copy_resource_from_home",
        subject=ensure_relative_resource_path(target_relative_path or relative_path),
        create_backup=create_backup,
    )
    resource_backups: list[dict[str, str]] = []
    backup_id = manifest.get("backupId")
    if target_path.exists() and backup_id:
        backup_target = backup_directory_path(str(backup_id)) / "resources_before" / safe_backup_fragment(str(target_path.name))
        copy_path_to_backup(target_path, backup_target)
        resource_backups.append({"target": str(target_path), "backup": str(backup_target)})
    created_resource_paths = [] if target_path.exists() else [str(target_path)]
    manifest = update_optional_backup_manifest(
        manifest,
        {
            "sourceCodexHome": str(source_paths.codex_home_path),
            "sourceResourcePath": str(source_path),
            "targetResourcePath": str(target_path),
            "resourceBackups": resource_backups,
            "createdResourcePaths": created_resource_paths,
            "restoreMode": "copy_resource_from_home",
        },
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.is_dir():
        shutil.copytree(source_path, target_path, dirs_exist_ok=True)
    else:
        shutil.copy2(source_path, target_path)
    manifest = update_optional_backup_manifest(
        manifest,
        {
            "copiedAt": datetime_module.datetime.now(datetime_module.UTC).isoformat(),
        },
    )
    return {
        "sourcePath": str(source_path),
        "targetPath": str(target_path),
        "backup": manifest,
        "overwroteExisting": bool(resource_backups),
        "warnings": warnings,
    }


def choose_import_thread_id(target_paths: CodexPaths, source_thread_id: str, preserve_thread_id: bool) -> str:
    if preserve_thread_id and fetch_thread_row(target_paths, source_thread_id) is None:
        return source_thread_id
    return str(uuid.uuid4())


def copy_thread_row_to_target_home(
    source_paths: CodexPaths,
    target_paths: CodexPaths,
    source_row: dict[str, Any],
    target_project_path: str | None = None,
    source_project_path: str | None = None,
    preserve_thread_id: bool = False,
    title_suffix: str = " imported",
    backup_id: str | None = None,
) -> dict[str, Any]:
    source_thread_id = str(source_row["id"])
    new_thread_id = choose_import_thread_id(target_paths, source_thread_id, preserve_thread_id)
    source_rollout_path = Path(normalize_path_text(source_row.get("rollout_path")))
    if not source_rollout_path.exists():
        raise FileNotFoundError(str(source_rollout_path))
    source_cwd = normalize_path_text(source_row.get("cwd"))
    if target_project_path and source_project_path:
        target_cwd = map_child_path(source_cwd, source_project_path, target_project_path)
    elif target_project_path:
        target_cwd = normalize_path_text(target_project_path)
    else:
        target_cwd = source_cwd
    database_cwd = "\\\\?\\" + target_cwd if re.match(r"^[A-Za-z]:\\", target_cwd) else target_cwd
    now = datetime_module.datetime.now()
    now_ms = int(time.time() * 1000)
    target_directory = target_paths.codex_home_path / "sessions" / now.strftime("%Y") / now.strftime("%m")
    target_directory.mkdir(parents=True, exist_ok=True)
    target_rollout_path = target_directory / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{new_thread_id}.jsonl"
    if backup_id:
        append_backup_manifest_lists(
            backup_id,
            {
                "createdThreadIds": [new_thread_id],
                "createdRolloutPaths": [str(target_rollout_path)],
            },
        )
    update_session_meta_for_copy(source_rollout_path, target_rollout_path, new_thread_id, strip_extended_prefix(target_cwd))
    if source_cwd and target_cwd and comparable_path_text(source_cwd) != comparable_path_text(target_cwd):
        replace_paths_in_jsonl(target_rollout_path, path_replacement_variants(source_cwd, target_cwd))

    insert_row = dict(source_row)
    insert_row["id"] = new_thread_id
    insert_row["rollout_path"] = str(target_rollout_path)
    insert_row["created_at"] = now_ms // 1000
    insert_row["updated_at"] = now_ms // 1000
    insert_row["created_at_ms"] = now_ms
    insert_row["updated_at_ms"] = now_ms
    insert_row["cwd"] = database_cwd
    insert_row["title"] = f"{source_row.get('title') or 'Untitled'}{title_suffix}"
    insert_row["archived"] = 0
    insert_row["archived_at"] = None
    insert_row["has_user_event"] = 1
    insert_row["thread_source"] = "imported"
    with connect_database(target_paths.database_path, readonly=False) as connection:
        upsert_thread_row(connection, insert_row)
        connection.commit()
    if target_cwd:
        add_project_to_global_state(target_paths, target_cwd, thread_id=new_thread_id)
    session_index_entry = append_session_index_entry(target_paths, insert_row, now_ms)
    if backup_id:
        append_backup_manifest_lists(
            backup_id,
            {
                "sessionIndexEntries": [session_index_entry],
            },
        )
    return {
        "sourceThreadId": source_thread_id,
        "newThreadId": new_thread_id,
        "sourceRolloutPath": str(source_rollout_path),
        "newRolloutPath": str(target_rollout_path),
        "targetProjectPath": target_cwd,
        "sessionIndexEntry": session_index_entry,
    }


def import_thread_from_home(
    target_codex_home_text: str | None,
    source_codex_home_text: str,
    source_thread_id: str,
    target_project_path: str | None = None,
    preserve_thread_id: bool = False,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    source_paths = resolve_codex_paths(source_codex_home_text)
    target_paths = resolve_codex_paths(target_codex_home_text)
    source_row = fetch_thread_row(source_paths, source_thread_id)
    if source_row is None:
        raise KeyError(source_thread_id)
    manifest = create_optional_home_state_backup(target_paths, action="import_thread", subject=source_thread_id, create_backup=create_backup)
    imported_thread = copy_thread_row_to_target_home(
        source_paths=source_paths,
        target_paths=target_paths,
        source_row=source_row,
        target_project_path=target_project_path,
        preserve_thread_id=preserve_thread_id,
        backup_id=manifest.get("backupId"),
    )
    manifest = update_optional_backup_manifest(
        manifest,
        {
            "sourceCodexHome": str(source_paths.codex_home_path),
            "createdThreadIds": [imported_thread["newThreadId"]],
            "createdRolloutPaths": [imported_thread["newRolloutPath"]],
            "restoreMode": "import_thread_archive_created",
        },
    )
    return {"importedThreads": [imported_thread], "backup": manifest, "warnings": warnings}


def preview_import_thread_from_home(
    target_codex_home_text: str | None,
    source_codex_home_text: str,
    source_thread_id: str,
    target_project_path: str | None = None,
    preserve_thread_id: bool = False,
) -> dict[str, Any]:
    source_paths = resolve_codex_paths(source_codex_home_text)
    target_paths = resolve_codex_paths(target_codex_home_text)
    source_row = fetch_thread_row(source_paths, source_thread_id)
    if source_row is None:
        raise KeyError(source_thread_id)
    rollout_path = Path(normalize_path_text(source_row.get("rollout_path")))
    if not rollout_path.exists():
        raise FileNotFoundError(str(rollout_path))
    target_thread_id = choose_import_thread_id(target_paths, source_thread_id, preserve_thread_id)
    source_project_path = normalize_path_text(source_row.get("cwd"))
    return {
        "sourceCodexHome": str(source_paths.codex_home_path),
        "targetCodexHome": str(target_paths.codex_home_path),
        "sourceThreadId": source_thread_id,
        "targetThreadId": target_thread_id,
        "preservesThreadId": target_thread_id == source_thread_id,
        "sourceProjectPath": source_project_path,
        "targetProjectPath": normalize_path_text(target_project_path) if target_project_path else source_project_path,
        "sourceRolloutPath": str(rollout_path),
        "rolloutBytes": rollout_path.stat().st_size,
        "warnings": write_safety_warnings(),
    }


def validate_import_source_rows(source_rows: list[dict[str, Any]]) -> None:
    missing_paths = []
    for source_row in source_rows:
        rollout_path = Path(normalize_path_text(source_row.get("rollout_path")))
        if not rollout_path.exists():
            missing_paths.append(str(rollout_path))
    if missing_paths:
        raise FileNotFoundError("missing source rollout files: " + "; ".join(missing_paths[:8]))


def rows_for_project(paths: CodexPaths, source_project_path: str, include_archived: bool = False) -> list[dict[str, Any]]:
    source_path_text = normalize_path_text(source_project_path)
    source_comparable = comparable_path_text(source_path_text)
    rows = []
    for row in fetch_thread_rows(paths):
        cwd_text = comparable_path_text(normalize_path_text(row.get("cwd")))
        if cwd_text == source_comparable or cwd_text.startswith(source_comparable + os.sep):
            if include_archived or not bool(row.get("archived")):
                rows.append(row)
    return rows


def import_project_from_home(
    target_codex_home_text: str | None,
    source_codex_home_text: str,
    source_project_path: str,
    target_project_path: str | None = None,
    include_archived: bool = False,
    preserve_thread_ids: bool = False,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    source_paths = resolve_codex_paths(source_codex_home_text)
    target_paths = resolve_codex_paths(target_codex_home_text)
    source_rows = rows_for_project(source_paths, source_project_path, include_archived=include_archived)
    if not source_rows:
        raise ValueError("no source project threads matched")
    validate_import_source_rows(source_rows)
    manifest = create_optional_home_state_backup(
        target_paths,
        action="import_project",
        subject=Path(normalize_path_text(source_project_path)).name,
        create_backup=create_backup,
    )
    imported_threads: list[dict[str, Any]] = []
    for source_row in source_rows:
        imported_threads.append(
            copy_thread_row_to_target_home(
                source_paths=source_paths,
                target_paths=target_paths,
                source_row=source_row,
                target_project_path=target_project_path,
                source_project_path=source_project_path,
                preserve_thread_id=preserve_thread_ids,
                backup_id=manifest.get("backupId"),
            )
        )
        manifest = update_optional_backup_manifest(
            manifest,
            {
                "sourceCodexHome": str(source_paths.codex_home_path),
                "sourceProjectPath": normalize_path_text(source_project_path),
                "targetProjectPath": normalize_path_text(target_project_path) if target_project_path else None,
                "createdThreadIds": [thread["newThreadId"] for thread in imported_threads],
                "createdRolloutPaths": [thread["newRolloutPath"] for thread in imported_threads],
                "restoreMode": "import_project_archive_created",
            },
        )
    manifest = update_optional_backup_manifest(
        manifest,
        {
            "sourceCodexHome": str(source_paths.codex_home_path),
            "sourceProjectPath": normalize_path_text(source_project_path),
            "targetProjectPath": normalize_path_text(target_project_path) if target_project_path else None,
            "createdThreadIds": [thread["newThreadId"] for thread in imported_threads],
            "createdRolloutPaths": [thread["newRolloutPath"] for thread in imported_threads],
            "restoreMode": "import_project_archive_created",
        },
    )
    return {"importedThreads": imported_threads, "backup": manifest, "warnings": warnings}


def preview_import_project_from_home(
    target_codex_home_text: str | None,
    source_codex_home_text: str,
    source_project_path: str,
    target_project_path: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    source_paths = resolve_codex_paths(source_codex_home_text)
    target_paths = resolve_codex_paths(target_codex_home_text)
    source_rows = rows_for_project(source_paths, source_project_path, include_archived=include_archived)
    if not source_rows:
        raise ValueError("no source project threads matched")
    validate_import_source_rows(source_rows)
    rollout_paths = [Path(normalize_path_text(row.get("rollout_path"))) for row in source_rows]
    return {
        "sourceCodexHome": str(source_paths.codex_home_path),
        "targetCodexHome": str(target_paths.codex_home_path),
        "sourceProjectPath": normalize_path_text(source_project_path),
        "targetProjectPath": normalize_path_text(target_project_path) if target_project_path else None,
        "matchedThreads": len(source_rows),
        "archivedIncluded": include_archived,
        "rolloutBytes": sum(path.stat().st_size for path in rollout_paths),
        "warnings": write_safety_warnings(),
    }


def validate_environment(codex_home_text: str | None = None) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    checks = {
        "codexHomeExists": paths.codex_home_path.exists(),
        "databaseExists": paths.database_path.exists(),
        "globalStateExists": paths.global_state_path.exists(),
        "sessionIndexExists": paths.session_index_path.exists(),
        "configExists": paths.config_path.exists(),
        "backupRoot": str(backup_root_path()),
    }
    thread_count = None
    if checks["databaseExists"]:
        with connect_database(paths.database_path, readonly=True) as connection:
            thread_count = int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
    return {
        "paths": {
            "codexHome": str(paths.codex_home_path),
            "database": str(paths.database_path),
            "globalState": str(paths.global_state_path),
            "sessionIndex": str(paths.session_index_path),
            "config": str(paths.config_path),
        },
        "checks": checks,
        "threadCount": thread_count,
        "version": read_version(paths),
        "currentVersions": detect_current_codex_versions(paths),
        "codexProcesses": detect_codex_processes(),
        "writeWarnings": write_safety_warnings(),
    }


legacy_thread_messenger_table_name = "mcp_servers.codex_thread_messenger"
official_thread_tool_names = {
    "create_thread",
    "fork_thread",
    "handoff_thread",
    "list_projects",
    "list_threads",
    "read_thread",
    "send_message_to_thread",
    "set_thread_archived",
    "set_thread_pinned",
    "set_thread_title",
}


def sqlite_table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    )


def inspect_official_thread_tool_registry(paths: CodexPaths, sample_limit: int = 8) -> dict[str, Any]:
    result: dict[str, Any] = {
        "databaseExists": paths.database_path.exists(),
        "tableExists": False,
        "threadCount": 0,
        "codexAppToolRows": 0,
        "threadsWithCodexAppTools": 0,
        "threadsWithOfficialSendMessage": 0,
        "threadsMissingOfficialSendMessage": 0,
        "sampleThreadsMissingOfficialSendMessage": [],
        "error": "",
    }
    if not paths.database_path.exists():
        return result
    try:
        with connect_database(paths.database_path, readonly=True) as connection:
            result["threadCount"] = int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] or 0)
            result["tableExists"] = sqlite_table_exists(connection, "thread_dynamic_tools")
            if not result["tableExists"]:
                return result
            namespace_exists = any(
                str(row["name"]) == "namespace"
                for row in connection.execute("PRAGMA table_info(thread_dynamic_tools)").fetchall()
            )
            namespace_predicate = "namespace = 'codex_app'" if namespace_exists else "name IN ({})".format(
                ",".join("?" for _ in official_thread_tool_names)
            )
            namespace_params = [] if namespace_exists else sorted(official_thread_tool_names)
            result["codexAppToolRows"] = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM thread_dynamic_tools WHERE {namespace_predicate}",
                    namespace_params,
                ).fetchone()[0]
                or 0
            )
            result["threadsWithCodexAppTools"] = int(
                connection.execute(
                    f"SELECT COUNT(DISTINCT thread_id) FROM thread_dynamic_tools WHERE {namespace_predicate}",
                    namespace_params,
                ).fetchone()[0]
                or 0
            )
            send_predicate = "namespace = 'codex_app' AND name = 'send_message_to_thread'" if namespace_exists else "name = 'send_message_to_thread'"
            result["threadsWithOfficialSendMessage"] = int(
                connection.execute(
                    f"SELECT COUNT(DISTINCT thread_id) FROM thread_dynamic_tools WHERE {send_predicate}"
                ).fetchone()[0]
                or 0
            )
            result["threadsMissingOfficialSendMessage"] = max(
                0,
                int(result["threadCount"]) - int(result["threadsWithOfficialSendMessage"]),
            )
            missing_rows = connection.execute(
                f"""
                SELECT id, title, cwd, tokens_used, updated_at, rollout_path
                FROM threads
                WHERE id NOT IN (
                    SELECT DISTINCT thread_id FROM thread_dynamic_tools WHERE {send_predicate}
                )
                ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC, id DESC
                LIMIT ?
                """,
                (sample_limit,),
            ).fetchall()
            result["sampleThreadsMissingOfficialSendMessage"] = [dict(row) for row in missing_rows]
    except Exception as error:
        result["error"] = str(error)
    return result


def inspect_legacy_thread_messenger_path(config_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "configPath": str(config_path),
        "configExists": config_path.exists(),
        "activeTableCount": 0,
        "activeTables": [],
        "configSha256": "",
        "error": "",
    }
    if not config_path.exists():
        return result
    try:
        config_text = config_path.read_text(encoding="utf-8")
        result["configSha256"] = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
        active_blocks = active_toml_table_block_ranges(config_text, legacy_thread_messenger_table_name)
        result["activeTableCount"] = len(active_blocks)
        result["activeTables"] = [
            {
                "startLine": int(block["startLine"]),
                "endLine": int(block["endLine"]),
                "preview": str(block["text"])[:500],
            }
            for block in active_blocks
        ]
    except Exception as error:
        result["error"] = str(error)
    return result


def inspect_legacy_thread_messenger_config(paths: CodexPaths) -> dict[str, Any]:
    return inspect_legacy_thread_messenger_path(paths.config_path)


def preview_official_thread_tools_repair(codex_home_text: str | None = None) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    config_scan = inspect_legacy_thread_messenger_config(paths)
    managed_config_scan = inspect_legacy_thread_messenger_path(paths.codex_home_path / "managed_config.toml")
    registry_scan = inspect_official_thread_tool_registry(paths)
    active_fallback_count = int(config_scan.get("activeTableCount") or 0) + int(
        managed_config_scan.get("activeTableCount") or 0
    )
    needs_repair = active_fallback_count > 0
    return {
        "action": "repair_official_thread_tools_exposure",
        "codexHome": str(paths.codex_home_path),
        "config": config_scan,
        "managedConfig": managed_config_scan,
        "threadToolRegistry": registry_scan,
        "needsRepair": needs_repair,
        "willModifyConfigToml": int(config_scan.get("activeTableCount") or 0) > 0,
        "willModifyManagedConfigToml": int(managed_config_scan.get("activeTableCount") or 0) > 0,
        "requiresCodexRestart": needs_repair,
        "warnings": [
            *write_safety_warnings(),
            "This repair disables only the legacy codex_thread_messenger MCP fallback. It does not edit state_5.sqlite dynamic tool rows.",
            "After the write, fully quit and reopen Codex Desktop, then verify the target thread itself can see official codex_app.list_threads/read_thread/send_message_to_thread.",
        ],
        "verificationSteps": [
            "Run codex doctor --summary and confirm the fallback MCP server count drops.",
            "Fully quit and reopen Codex Desktop because running conversations cache MCP metadata.",
            "Use the official codex_app.send_message_to_thread tool and verify the target Desktop thread visibly receives a <codex_delegation> message.",
            "In the target thread, verify tool_search or direct calls expose codex_app.list_threads, codex_app.read_thread and codex_app.send_message_to_thread.",
        ],
    }


def comment_active_toml_blocks(text: str, blocks: list[dict[str, Any]], reason: str) -> str:
    updated_text = text
    for block in sorted(blocks, key=lambda item: int(item["startOffset"]), reverse=True):
        block_text = str(block["text"])
        commented_lines: list[str] = []
        for line in block_text.splitlines(keepends=True):
            if line.strip() and not line.lstrip().startswith("#"):
                commented_lines.append("# " + line)
            else:
                commented_lines.append(line)
        replacement = reason + "".join(commented_lines)
        updated_text = updated_text[: int(block["startOffset"])] + replacement + updated_text[int(block["endOffset"]) :]
    return updated_text


def repair_official_thread_tools_exposure(
    codex_home_text: str | None = None,
    acknowledge_codex_running_risk: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    paths = resolve_codex_paths(codex_home_text)
    before = preview_official_thread_tools_repair(codex_home_text)
    config_paths = [paths.config_path, paths.codex_home_path / "managed_config.toml"]
    originals = {path: path.read_text(encoding="utf-8") if path.exists() else "" for path in config_paths}
    active_blocks_by_path = {
        path: active_toml_table_block_ranges(text, legacy_thread_messenger_table_name)
        for path, text in originals.items()
    }
    if not any(active_blocks_by_path.values()):
        return {
            "changed": False,
            "codexHome": str(paths.codex_home_path),
            "backup": skipped_backup_manifest(
                paths,
                action="repair_official_thread_tools_exposure",
                subject="config.toml + managed_config.toml",
                restore_mode="skipped",
            ),
            "before": before,
            "after": before,
            "warnings": before.get("warnings", []),
            "restartRequired": False,
        }
    warnings = enforce_write_safety(acknowledge_codex_running_risk)
    backup_manifest = create_optional_home_state_backup(
        paths,
        action="repair_official_thread_tools_exposure",
        subject="config.toml + managed_config.toml",
        create_backup=create_backup,
    )
    reason = (
        "# Disabled by Codex Home Manager: legacy codex_thread_messenger fallback can hide official "
        "codex_app thread tools. Re-enable only after official send_message_to_thread exposure is verified.\n"
    )
    updated_texts = {
        path: comment_active_toml_blocks(originals[path], blocks, reason)
        for path, blocks in active_blocks_by_path.items()
        if blocks
    }
    for path, updated_text in updated_texts.items():
        try:
            tomllib.loads(updated_text)
        except tomllib.TOMLDecodeError as error:
            raise RuntimeError(f"refusing to write invalid {path.name} after repair: {error}") from error
    temp_paths = {
        path: path.with_name(path.name + ".official-thread-tools.tmp")
        for path in updated_texts
    }
    replaced_paths: list[Path] = []
    try:
        for path, temp_path in temp_paths.items():
            temp_path.write_text(updated_texts[path], encoding="utf-8", newline="")
        for path, temp_path in temp_paths.items():
            replace_file_with_retry(temp_path, path)
            replaced_paths.append(path)
    except Exception:
        for temp_path in temp_paths.values():
            temp_path.unlink(missing_ok=True)
        for path in replaced_paths:
            rollback_path = path.with_name(path.name + ".official-thread-tools.rollback")
            rollback_path.write_text(originals[path], encoding="utf-8", newline="")
            replace_file_with_retry(rollback_path, path)
        raise
    after = preview_official_thread_tools_repair(codex_home_text)
    backup_manifest = update_optional_backup_manifest(
        backup_manifest,
        {
            "repairBefore": before,
            "repairAfter": after,
            "modifiedConfigPath": str(paths.config_path) if paths.config_path in updated_texts else None,
            "modifiedConfigPaths": [str(path) for path in updated_texts],
            "disabledTableName": legacy_thread_messenger_table_name,
            "disabledTableCount": sum(len(blocks) for blocks in active_blocks_by_path.values()),
            "restoreMode": backup_manifest.get("restoreMode") or "home_state",
        },
    )
    return {
        "changed": True,
        "codexHome": str(paths.codex_home_path),
        "backup": backup_manifest,
        "before": before,
        "after": after,
        "warnings": [
            *warnings,
            "Fully quit and reopen Codex Desktop before declaring the official thread tools fixed.",
        ],
        "restartRequired": True,
    }


def copy_static_build_if_needed(source_path: Path, target_path: Path) -> None:
    if not source_path.exists():
        return
    if target_path.exists():
        shutil.rmtree(target_path)
    shutil.copytree(source_path, target_path)
