from __future__ import annotations

from typing import Any


class ProviderInteropError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def public_record(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": redact_public(self.details)}


redacted_value = "[REDACTED]"
sensitive_key_fragments = (
    "authorization",
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
    "cookie",
)


def redact_public(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): redacted_value if sensitive_key(str(key)) else redact_public(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_public(item) for item in value]
    if isinstance(value, tuple):
        return [redact_public(item) for item in value]
    if isinstance(value, str) and secret_string(value):
        return redacted_value
    return value


def sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(fragment in normalized for fragment in sensitive_key_fragments)


def secret_string(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized.startswith(("bearer ", "basic ")) or (
        normalized.startswith("sk-") and len(normalized) >= 12
    )
