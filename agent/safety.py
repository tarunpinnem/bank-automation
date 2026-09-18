"""Safety & policy guardrails.

Three responsibilities, kept separate on purpose so each is easy to audit:
1. Allowlist enforcement -- what URLs/routes and action types are permitted.
2. Risk gating -- risky (irreversible-ish) actions require explicit approval.
3. Redaction -- sensitive values never reach logs/artifacts/screenshots raw.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .errors import GuardrailViolation

DEFAULT_ALLOWED_ACTIONS = {"click", "type", "select", "navigate", "wait_for", "extract"}
RISKY_ACTIONS = {"submit_form"}  # actions that create/modify state; click on a
# submit button is tagged risky at the STEP level (risk_level), not by action
# name, since the same "click" action is safe on a search button and risky
# on a "confirm" button. See Step.risk_level in artifact.py.


@dataclass
class Allowlist:
    allowed_base_urls: list[str] = field(default_factory=list)
    allowed_action_types: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED_ACTIONS))

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if not any(origin == allowed or url.startswith(allowed) for allowed in self.allowed_base_urls):
            raise GuardrailViolation(f"URL not in allowlist: {url}")

    def check_action(self, action_type: str) -> None:
        if action_type not in self.allowed_action_types:
            raise GuardrailViolation(f"Action type not permitted: {action_type}")

    @staticmethod
    def default_for(base_url: str) -> "Allowlist":
        return Allowlist(allowed_base_urls=[base_url])


def requires_approval(step_risk_level: str, auto_approve_risky: bool) -> bool:
    """Risky steps pause for explicit approval unless the caller opted in
    (and even then it's logged -- see recorder/replay call sites)."""
    return step_risk_level == "risky" and not auto_approve_risky


# --- Redaction ---------------------------------------------------------

_SENSITIVE_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),             # SSN-like
    re.compile(r"\b\d{12,19}\b"),                       # long account/card-like numbers
    re.compile(r"\b(?:sk|api|token)[-_][A-Za-z0-9]{8,}\b", re.IGNORECASE),  # secrets/tokens
]


def redact_text(text: str, extra_values: list[str] | None = None) -> str:
    """Redact known-sensitive patterns plus any explicitly-flagged runtime
    values (e.g. an input param marked sensitive=True) before the text is
    written to a log, artifact, or evidence file."""
    redacted = text
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    for value in extra_values or []:
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def redact_dict(d: dict, sensitive_keys: set[str]) -> dict:
    out = {}
    for k, v in d.items():
        if k in sensitive_keys:
            out[k] = "[REDACTED]"
        elif isinstance(v, str):
            out[k] = redact_text(v)
        else:
            out[k] = v
    return out
