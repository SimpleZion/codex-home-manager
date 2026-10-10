"""Canonical directory layout and root-clutter enforcement for this workspace."""
from __future__ import annotations

import json
import os
from pathlib import Path


workspace_root = Path(__file__).resolve().parents[1]
cockpit_root = workspace_root / "codex_home_manager"
artifact_root = Path(r"D:\ZFiles\CXProject\Codex")
manifest_path = workspace_root / "config" / "storage-layout.json"


def load_manifest() -> dict[str, object]:
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if value.get("schemaVersion") != 1:
        raise RuntimeError("Unsupported storage-layout schema")
    return value


def generated_roots(root: Path | None = None) -> tuple[Path, ...]:
    manifest = load_manifest()
    root = Path(root) if root is not None else workspace_root
    roots = []
    for name, section_root in (("workspace", root), ("cockpit", root / "codex_home_manager")):
        roots.extend(section_root / relative for relative in manifest[name]["generatedDirectories"])
    return tuple(roots)


def _root_violations(root: Path, policy: dict[str, object], label: str) -> list[str]:
    if not root.is_dir():
        return [f"{label} root is missing: {root}"]
    allowed_files = set(policy["allowedFiles"])
    allowed_directories = set(policy["sourceDirectories"]) | set(policy["generatedDirectories"])
    violations = []
    for entry in os.scandir(root):
        if entry.is_symlink():
            violations.append(f"{label} root contains a link: {entry.path}")
        elif entry.is_dir(follow_symlinks=False):
            if entry.name not in allowed_directories:
                violations.append(f"{label} root directory is unclassified: {entry.path}")
        elif entry.is_file(follow_symlinks=False):
            if entry.name not in allowed_files:
                violations.append(f"{label} root file is unclassified: {entry.path}")
        else:
            violations.append(f"{label} root entry is unsupported: {entry.path}")
    return violations


def layout_violations() -> list[str]:
    manifest = load_manifest()
    violations = _root_violations(workspace_root, manifest["workspace"], "workspace")
    violations.extend(_root_violations(cockpit_root, manifest["cockpit"], "cockpit"))
    artifact_policy = manifest["artifactStore"]
    if artifact_root.is_dir():
        allowed_directories = set(artifact_policy["allowedDirectories"])
        allowed_files = set(artifact_policy["allowedFiles"])
        for entry in os.scandir(artifact_root):
            if entry.is_symlink():
                violations.append(f"artifact root contains a link: {entry.path}")
            elif entry.is_dir(follow_symlinks=False) and entry.name not in allowed_directories:
                violations.append(f"artifact root directory is unclassified: {entry.path}")
            elif entry.is_file(follow_symlinks=False) and entry.name not in allowed_files:
                violations.append(f"artifact root file is unclassified: {entry.path}")
    return sorted(violations)
