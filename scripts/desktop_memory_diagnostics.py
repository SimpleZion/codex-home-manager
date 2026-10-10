"""CLI for the shared, read-only Home Manager Desktop memory diagnostics."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "codex_home_manager"))
from backend.desktop_memory_diagnostics import main

if __name__ == "__main__":
    main()
