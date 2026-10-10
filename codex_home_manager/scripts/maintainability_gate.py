from __future__ import annotations

import ast
from pathlib import Path


manager_root = Path(__file__).resolve().parents[1]
line_budgets = {
    # 1.0.34 baseline includes stable prompt cursors and explicit message phases.
    # This fixed release baseline does not relax the cap for new functions.
    "backend/codex_data.py": 10628,
    "backend/server.py": 4709,
    "src/main.tsx": 10269,
}
legacy_function_budgets = {
    ("backend/codex_data.py", "build_snapshot"): 374,
    ("backend/codex_data.py", "restore_backup"): 276,
    ("backend/codex_data.py", "parse_rollout_stats"): 229,
    ("backend/codex_data.py", "move_thread_workspace"): 210,
    ("backend/codex_data.py", "repair_official_thread_tools_exposure"): 177,
    ("backend/codex_data.py", "slim_thread"): 140,
    ("backend/codex_data.py", "timeline_event_from_item"): 152,
    ("backend/codex_data.py", "read_thread_timeline"): 174,
    ("backend/codex_data.py", "read_recent_thread_prompt_page"): 149,
    ("backend/codex_data.py", "rename_project"): 121,
    ("backend/server.py", "mcp_execute_tool"): 632,
    ("backend/server.py", "capabilities"): 173,
}
new_function_line_budget = 120


def run_gate() -> list[str]:
    violations: list[str] = []
    for relative_path, budget in line_budgets.items():
        path = manager_root / relative_path
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > budget:
            violations.append(f"{relative_path} has {line_count} lines; ratchet budget is {budget}")

    for relative_path in ("backend/codex_data.py", "backend/server.py"):
        path = manager_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            line_count = int(node.end_lineno or node.lineno) - node.lineno + 1
            budget = legacy_function_budgets.get((relative_path, node.name), new_function_line_budget)
            if line_count > budget:
                violations.append(
                    f"{relative_path}:{node.lineno} {node.name} has {line_count} lines; ratchet budget is {budget}"
                )
    return violations


def main() -> int:
    violations = run_gate()
    if violations:
        for violation in violations:
            print(f"FAIL: {violation}")
        return 1
    print("maintainability ratchet PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
