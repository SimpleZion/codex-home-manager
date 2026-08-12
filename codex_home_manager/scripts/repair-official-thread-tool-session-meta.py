from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.official_thread_tools_session_meta import repair_official_thread_tool_session_meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--backup-root", required=True)
    parser.add_argument("--status-path", required=True)
    parser.add_argument("--external-process-guard-active", action="store_true")
    arguments = parser.parse_args()
    if not arguments.external_process_guard_active:
        parser.error("--external-process-guard-active is required")
    codex_home = Path(arguments.codex_home)
    backup_root = Path(arguments.backup_root)
    status_path = Path(arguments.status_path)
    repair_official_thread_tool_session_meta(
        codex_home,
        backup_root,
        status_path,
        require_codex_stopped=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
