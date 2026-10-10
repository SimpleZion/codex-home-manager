"""Read-only bounded Desktop memory evidence; no process control or history reads."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import time
import threading
from typing import Any

import psutil


snapshot_fields = frozenset((
    "thread_count_total", "thread_count_loaded_recent", "thread_count_active",
    "thread_count_streaming_owner", "thread_count_streaming_follower",
    "thread_count_streaming_with_active_runtime", "thread_count_streaming_without_active_runtime",
    "thread_count_with_inflight_turn", "turn_count_total_loaded", "item_count_total_loaded",
    "max_turns_in_single_thread", "max_items_in_single_turn", "pending_request_count",
    "inflight_turn_count", "review_diff_files_total", "review_diff_lines_total",
    "review_diff_bytes_estimate", "renderer_process_count_total", "renderer_webcontents_count_total",
    "renderer_process_working_set_kb_total", "renderer_process_working_set_kb",
    "main_process_rss_bytes", "main_process_heap_used_bytes", "main_process_external_bytes",
    "app_server_stdio_bytes_read_last_30s", "app_server_stdio_bytes_written_last_30s",
    "system_free_memory_bytes", "delta_bytes_last_30s_estimate",
))
required_detail_fields = frozenset(("item_count_total_loaded", "max_items_in_single_turn",
                                    "renderer_process_count_total", "renderer_process_working_set_kb_total"))
identity_cache: OrderedDict = OrderedDict()
identity_lock = threading.Lock()
evidence_lock = threading.Lock()
evidence_cache: tuple[float, dict] | None = None


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) <= 2**53 and math.isfinite(value)


def read_scope_snapshot(path: Path, *, now: float, oldest_start: float | None = None) -> dict:
    result = {"state": "unavailable", "source": "official_local_breadcrumb", "metrics": {},
              "pageAttributionAvailable": False, "processBindingVerified": False}
    try:
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return dict(result, reason="scope_size_limit")
        value = json.loads(data)
        if not isinstance(value, dict) or not isinstance(value.get("scope"), dict):
            return dict(result, reason="invalid_scope")
        crumbs = value["scope"].get("breadcrumbs", [])
        if not isinstance(crumbs, list) or len(crumbs) > 256:
            return dict(result, reason="breadcrumb_count_limit")
        snapshots = [item for item in crumbs if isinstance(item, dict)
                     and item.get("category") == "app_state" and item.get("message") == "app_state_snapshot"
                     and finite_number(item.get("timestamp")) and isinstance(item.get("data"), dict)]
        if not snapshots:
            return dict(result, reason="no_official_snapshot")
        valid = [item for item in snapshots if item["timestamp"] <= now + 5 and now - item["timestamp"] <= 120
                 and (oldest_start is None or item["timestamp"] >= oldest_start)]
        if not valid:
            return dict(result, state="stale", reason="snapshot_outside_current_observation")
        snapshot = max(valid, key=lambda item: item["timestamp"])
        timestamp = snapshot["timestamp"]
        fields = snapshot["data"]
        metrics = {key: fields[key] for key in snapshot_fields
                   if finite_number(fields.get(key)) and fields[key] >= 0}
        missing = sorted(required_detail_fields - metrics.keys())
        # A breadcrumb is not a PID binding. Only expose allowlisted numeric fields.
        return dict(result, state="partial" if missing or fields.get("codex_truncated_breadcrumb_data") else "available",
                    timestamp=timestamp, ageSeconds=round(max(0, now - timestamp), 2), metrics=metrics,
                    missingDetailFields=missing, truncated=fields.get("codex_truncated_breadcrumb_data") is True)
    except (OSError, ValueError, RecursionError):
        return dict(result, reason="scope_unreadable_or_changing")


def descendant_ids(entries: list[dict], root_ids: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for item in entries:
        children.setdefault(int(item["parentPid"]), []).append(int(item["pid"]))
    seen = set(root_ids)
    pending = list(root_ids)
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in seen:
                seen.add(child)
                pending.append(child)
    return seen


def parse_debug_snapshot(line: str, *, now: float, root: dict) -> dict | None:
    parts = line.split(" ", 4)
    if len(parts) != 5 or parts[1:4] != ["debug", "[sampler-manager]", "app_state_snapshot"]:
        return None
    try:
        timestamp = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).timestamp()
        if not root["createdAt"] <= timestamp <= now + 5 or now - timestamp > 120:
            return None
        fields = dict(token.split("=", 1) for token in shlex.split(parts[4]) if "=" in token)
        metrics = {}
        for key in snapshot_fields:
            if key in fields:
                try:
                    number = float(fields[key])
                    if finite_number(number) and number >= 0:
                        metrics[key] = int(number) if number.is_integer() else number
                except ValueError:
                    pass
        missing = sorted(required_detail_fields - metrics.keys())
        webcontents = fields.get("renderer_webcontents_id", "")
        kinds = [fields.get(f"renderer_process_rank_{rank}_kind") for rank in range(1, 4)
                 if fields.get(f"renderer_process_rank_{rank}_has_current_webcontents") == "true"]
        return {"state": "partial" if missing else "available", "source": "official_debug_log",
                "timestamp": timestamp, "ageSeconds": round(max(0, now - timestamp), 2), "metrics": metrics,
                "missingDetailFields": missing, "truncated": False, "pageAttributionAvailable": False,
                "processBindingVerified": True, "sourceMainPid": root["pid"],
                "rendererProcessBindingVerified": False,
                "webContentsId": int(webcontents) if webcontents.isdigit() and len(webcontents) < 10 else None,
                "rendererKind": kinds[0] if kinds and kinds[0] in {"main_window", "iab", "mixed", "other"} else "unknown"}
    except (ValueError, OverflowError, OSError):
        return None


def read_debug_snapshot(log_root: Path, *, now: float, roots: list[dict]) -> dict:
    result = {"state": "unavailable", "source": "official_debug_log", "metrics": {},
              "pageAttributionAvailable": False, "processBindingVerified": False,
              "reason": "no_fresh_bounded_debug_snapshot"}
    by_pid = {root["pid"]: root for root in roots[:4]}
    candidates = []
    today = datetime.fromtimestamp(now, timezone.utc)
    pattern = re.compile(r"codex-desktop-[0-9a-f-]+-(\d+)-t0-i\d+-\d+-\d+\.log$")
    for day in (today, today - timedelta(days=1)):
        try:
            with os.scandir(log_root / day.strftime("%Y/%m/%d")) as entries:
                for index, entry in enumerate(entries):
                    if index >= 512:
                        break
                    match = pattern.fullmatch(entry.name)
                    if match and int(match[1]) in by_pid:
                        candidates.append((Path(entry.path), by_pid[int(match[1])]))
        except OSError:
            continue
    snapshots = []
    # Metadata may show zero bytes until the live writer closes. Seek on the opened file.
    for path, root in sorted(candidates, key=lambda item: item[0].name, reverse=True)[:10]:
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                end = stream.tell()
                stream.seek(max(0, end - 256 * 1024))
                data = stream.read(256 * 1024)
            for line in data.decode("utf-8", errors="replace").splitlines():
                snapshot = parse_debug_snapshot(line, now=now, root=root)
                if snapshot:
                    snapshots.append(snapshot)
        except OSError:
            continue
    return max(snapshots, key=lambda item: item["timestamp"]) if snapshots else result


def process_role(name: str, executable: str, command: list[str], root: bool) -> str:
    if root:
        return "desktop_main"
    lower = name.lower()
    if lower == "chatgpt.exe":
        kinds = {"renderer": "renderer", "gpu-process": "gpu", "utility": "utility",
                 "crashpad-handler": "crash_handler"}
        return next((kinds.get(arg.partition("=")[2], "desktop_helper")
                     for arg in command if arg.startswith("--type=")), "desktop_helper")
    if lower == "codex.exe" and "app-server" in command:
        return "app_server"
    if lower in {"node.exe", "node_repl.exe"} and "\\runtimes\\cua_node\\" in executable.lower():
        if any(arg.replace("\\", "/").rsplit("/", 1)[-1] == "trusted-worker.js" for arg in command):
            return "cua_trusted_worker"
        return "cua_runtime"
    if lower == "node_repl.exe":
        return "tool_runtime"
    return "task_or_plugin_child"


def read_identity(process, name: str, created: float, root: bool) -> tuple[str, str]:
    key = process.pid, created, root
    now = time.monotonic()
    with identity_lock:
        cached = identity_cache.get(key)
        if cached and now - cached[0] < 60:
            return cached[1], cached[2]
    executable = process.exe() if name.lower() in {"chatgpt.exe", "node.exe", "node_repl.exe", "codex.exe"} else ""
    inspect_command = name.lower() in {"chatgpt.exe", "codex.exe"} or (
        name.lower() in {"node.exe", "node_repl.exe"} and "\\runtimes\\cua_node\\" in executable.lower())
    command = process.cmdline() if inspect_command else []
    role = process_role(name, executable, command, root)
    with identity_lock:
        identity_cache[key] = now, executable, role
        identity_cache.move_to_end(key)
        while len(identity_cache) > 512:
            identity_cache.popitem(last=False)
    return executable, role


def process_snapshot(inventory=None) -> dict:
    # Reuse the product's Win32 process inventory; it avoids opening every Node command line.
    from .process_utils import list_windows_processes

    started = time.monotonic()
    entries = (inventory or list_windows_processes)()
    result = {"complete": True, "processes": [], "roots": [], "errors": [], "budgetKind": "soft", "budgetSeconds": 4}
    if not entries or len(entries) > 8192:
        return dict(result, complete=False, errors=["process_inventory_unavailable_or_over_limit"])
    roots = {}
    for item in entries:
        if time.monotonic() - started > 4:
            return dict(result, complete=False, errors=["root_scan_budget"])
        if item["imageName"].lower() != "chatgpt.exe":
            continue
        try:
            process = psutil.Process(int(item["pid"]))
            command = process.cmdline()
            executable = process.exe()
            codex_package = any(prefix in executable.lower() for prefix in ("\\windowsapps\\openai.codex_", "\\windowsapps\\simplezion.codexlocalrepair_"))
            if codex_package and not any(arg.startswith("--type=") for arg in command):
                roots[process.pid] = {"pid": process.pid, "createdAt": process.create_time()}
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError):
            result["complete"] = False
    if not roots:
        return dict(result, complete=False, errors=["desktop_root_not_found"])
    members = descendant_ids(entries, set(roots))
    by_pid = {int(item["pid"]): item for item in entries}
    # Measure Desktop and CUA first; task-launched services can be numerous.
    def priority(pid):
        item = by_pid[pid]
        if item["imageName"].lower() in {"chatgpt.exe", "codex.exe"}:
            return 0, pid
        parent = by_pid.get(int(item["parentPid"]), {})
        if item["imageName"].lower() == "node.exe" and parent.get("imageName", "").lower() == "node_repl.exe":
            return 1, pid
        return 2, pid
    ordered = []
    visited = set()
    def visit(pid):
        if pid in visited or pid not in members:
            return
        visited.add(pid)
        if pid not in roots:
            visit(int(by_pid[pid]["parentPid"]))
        ordered.append(pid)
    for pid in sorted(members, key=priority):
        visit(pid)
    oldest_start = min(root["createdAt"] for root in roots.values())
    generations = {}
    for pid in ordered:
        if len(result["processes"]) >= 512 or time.monotonic() - started > 4:
            result["complete"] = False
            result["errors"].append("process_scan_budget")
            break
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                created = process.create_time()
                if (created < oldest_start or process.ppid() != int(by_pid[pid]["parentPid"])
                        or (pid in roots and created != roots[pid]["createdAt"])):
                    result["complete"] = False
                    result["errors"].append(f"process_identity_changed:{pid}")
                    continue
                generations[pid] = created
                memory = process.memory_info()
                name = by_pid[pid]["imageName"]
                executable, role = read_identity(process, name, created, pid in roots)
                io = process.io_counters()
                result["processes"].append({"pid": pid, "parentPid": process.ppid(), "createdAt": created,
                    "role": role,
                    "rssBytes": memory.rss, "privateCommitBytes": getattr(memory, "private", memory.vms),
                    "readBytes": io.read_bytes, "writeBytes": io.write_bytes})
        except psutil.NoSuchProcess:
            result["complete"] = False
            result["errors"].append(f"process_exited:{pid}")
            continue
        except (psutil.Error, OSError):
            result["complete"] = False
            result["errors"].append(f"process_unreadable:{pid}")
    # Require a measured, creation-ordered ancestry back to an unchanged root.
    def valid_ancestry(pid):
        seen = set()
        while pid not in roots:
            if pid in seen or pid not in generations:
                return False
            seen.add(pid)
            parent = int(by_pid[pid]["parentPid"])
            if parent not in generations or generations[parent] > generations[pid]:
                return False
            pid = parent
        return generations.get(pid) == roots[pid]["createdAt"]
    verified = [item for item in result["processes"] if valid_ancestry(item["pid"])]
    if len(verified) != len(result["processes"]):
        result["complete"] = False
        result["errors"].append("unverified_ancestry_excluded")
    result["processes"] = verified
    result["roots"] = [root for pid, root in roots.items() if valid_ancestry(pid)]
    result["elapsedMs"] = round((time.monotonic() - started) * 1000)
    if result["elapsedMs"] > 4000:
        result["complete"] = False
        if "process_scan_budget" not in result["errors"]:
            result["errors"].append("process_scan_budget")
    groups = {}
    for item in result["processes"]:
        group = groups.setdefault(item["role"], {"count": 0, "rssBytesSum": 0, "privateCommitBytesSum": 0})
        group["count"] += 1
        group["rssBytesSum"] += item["rssBytes"]
        group["privateCommitBytesSum"] += item["privateCommitBytes"]
    result["groups"] = groups
    return result


def collect() -> dict:
    processes = process_snapshot()
    roots = processes.get("roots", [])
    scope = Path(os.environ["APPDATA"]) / "Codex/web/Codex/sentry/scope_v3.json"
    snapshot = read_scope_snapshot(scope, now=time.time(), oldest_start=min((root["createdAt"] for root in roots), default=None))
    if roots:
        debug = read_debug_snapshot(Path(os.environ["LOCALAPPDATA"]) / "Codex/Logs", now=time.time(), roots=roots)
        if debug["state"] == "available" or (debug["state"] == "partial" and snapshot["state"] not in {"available", "partial"}):
            snapshot = debug
    if not roots:
        snapshot["state"] = "unavailable"
        snapshot["metrics"] = {}
        snapshot["reason"] = "live_desktop_root_not_found"
    return {"schemaVersion": 1, "timestamp": time.time(), "readOnly": True,
            "availablePhysicalBytes": psutil.virtual_memory().available, "processes": processes,
            "officialSnapshot": snapshot, "diagnosis": "incomplete_page_attribution",
            "metricNotes": ["RSS sums may double-count shared pages; not the Task Manager application-group metric.",
                            "Private commit is not resident RAM. Process I/O includes pipes, not only disk files.",
                            "Loaded turns are not mounted DOM nodes. No leak or full-render diagnosis from this snapshot."]}


def build_diagnostic_evidence() -> dict:
    report = collect()
    processes = report["processes"]
    snapshot = report["officialSnapshot"]
    evidence = [f"collected_at={report['timestamp']}",
                f"scan_complete={processes['complete']}; budget=soft_4_seconds",
                f"snapshot={snapshot['state']}; source={snapshot['source']}; page_attribution=false; main_process_binding={snapshot['processBindingVerified']}"]
    if snapshot.get("webContentsId") is not None:
        evidence.append(f"webcontents_id={snapshot['webContentsId']}; renderer_kind={snapshot.get('rendererKind', 'unknown')}; renderer_pid_binding=false")
    if "timestamp" in snapshot:
        evidence.append(f"snapshot_timestamp={snapshot['timestamp']}; age_at_collection_seconds={snapshot.get('ageSeconds', '-')}")
    for role, group in sorted(processes.get("groups", {}).items()):
        evidence.append(f"{role}: count={group['count']}; rss_sum_mib={group['rssBytesSum'] / 2**20:.1f}; "
                        f"private_commit_sum_mib={group['privateCommitBytesSum'] / 2**20:.1f}")
    evidence.extend(f"snapshot.{key}={value}" for key, value in sorted(snapshot["metrics"].items()))
    evidence.extend(f"scan_error={error}" for error in processes["errors"])
    evidence.extend(f"missing={key}" for key in snapshot.get("missingDetailFields", []))
    valid_until = snapshot["timestamp"] + 120 if snapshot.get("state") in {"available", "partial"} else report["timestamp"] + 30
    return {"evidence": evidence, "validUntil": valid_until}


def diagnostic_evidence() -> list[str]:
    global evidence_cache
    # A concurrent health check must not multiply process scans or wait for another scan.
    if not evidence_lock.acquire(blocking=False):
        return ["scan_complete=false; scan_already_in_progress=true"]
    try:
        now = time.monotonic()
        if (evidence_cache is not None and 0 <= now - evidence_cache[0] < 30
                and time.time() <= evidence_cache[1]["validUntil"]):
            return [f"cached_age_seconds={now - evidence_cache[0]:.1f}", *evidence_cache[1]["evidence"]]
        try:
            evidence = build_diagnostic_evidence()
        except (OSError, ValueError, KeyError, psutil.Error):
            evidence = {"evidence": ["scan_complete=false; evidence_unavailable=true"], "validUntil": time.time() + 30}
        evidence_cache = time.monotonic(), evidence
        return list(evidence["evidence"])
    finally:
        evidence_lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seconds", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.seconds <= 3600:
        parser.error("seconds must be in [0, 3600]")
    output = None
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        output = args.output.open("x", encoding="utf-8")
    try:
        start = time.monotonic()
        while True:
            report = collect()
            encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
            if output:
                if output.tell() + len(encoded.encode("utf-8")) > 64 * 1024 * 1024:
                    raise RuntimeError("Observation output reached 64 MiB bound")
                output.write(encoded + "\n")
                output.flush()
            print(json.dumps({"timestamp": report["timestamp"], "groups": report["processes"].get("groups"),
                              "complete": report["processes"]["complete"], "officialSnapshot": report["officialSnapshot"]}), flush=True)
            if time.monotonic() - start >= args.seconds:
                break
            time.sleep(min(10, max(0, args.seconds - (time.monotonic() - start))))
    finally:
        if output:
            output.close()


if __name__ == "__main__":
    main()
