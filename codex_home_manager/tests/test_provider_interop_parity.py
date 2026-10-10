from __future__ import annotations

from backend.provider_interop.models import redact_public


def test_python_public_redaction_matches_worker_contract() -> None:
    value = {
        "authorization": "Bearer fixture-secret",
        "nested": {"experimental_bearer_token": "fixture-token"},
        "safe": "visible",
    }
    redacted = redact_public(value)
    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["nested"]["experimental_bearer_token"] == "[REDACTED]"
    assert redacted["safe"] == "visible"
