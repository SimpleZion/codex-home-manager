from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


script_path = Path(__file__).resolve().parents[1] / "scripts" / "maintainability_gate.py"
spec = importlib.util.spec_from_file_location("maintainability_gate", script_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_maintainability_ratchet_has_no_regression() -> None:
    assert module.run_gate() == []
