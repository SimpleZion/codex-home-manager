from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audit_state_digest(status: dict[str, Any], audit: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "codexHome": status.get("environment", {}).get("codexHome"),
            "adapterSchema": status.get("adapter", {}).get("schemaFingerprint"),
            "adapterUserVersion": status.get("adapter", {}).get("userVersion"),
            "files": [
                {
                    "path": item.get("path"),
                    "sizeBytes": item.get("sizeBytes"),
                    "modifiedNs": item.get("modifiedNs"),
                    "sourceSha256": item.get("sourceSha256"),
                }
                for item in audit.get("files", [])
            ],
            "promptFingerprint": audit.get("promptFingerprint"),
            "protectedContentSha256": audit.get("protectedContentSha256"),
        }
    )


def default_backup_root() -> Path:
    configured = __import__("os").environ.get("CODEX_HOME_MANAGER_BACKUP_ROOT")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    windows_backup = Path("D:/Backup/CodexHomeManager")
    if __import__("os").name == "nt" and windows_backup.parent.is_dir():
        return windows_backup.resolve(strict=False)
    return Path(__file__).resolve().parents[2] / "data" / "backups"
