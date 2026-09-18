"""Result taxonomy shared by discovery and replay.

The core discipline (per the design): a "no such member" style answer is a
BUSINESS_OUTCOME, not a FAILURE. RECOVERABLE conditions are known,
declared, and auto-handled. Only genuinely unexpected states become
HARD_FAILURE. Replay always returns exactly one of these.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResultKind(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"          # handled internally; not returned as final state
    HARD_FAILURE = "hard_failure"
    ESCALATED = "escalated"              # paused for human intervention


@dataclass
class StepFailureDetail:
    step_id: str
    expected: str
    observed: str


@dataclass
class RunResult:
    kind: ResultKind
    outputs: dict[str, Any] = field(default_factory=dict)
    business_outcome: str | None = None
    failure: StepFailureDetail | None = None
    escalation_reason: str | None = None
    evidence_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind.value}
        if self.outputs:
            d["outputs"] = self.outputs
        if self.business_outcome:
            d["business_outcome"] = self.business_outcome
        if self.failure:
            d["failure"] = {
                "step_id": self.failure.step_id,
                "expected": self.failure.expected,
                "observed": self.failure.observed,
            }
        if self.escalation_reason:
            d["escalation_reason"] = self.escalation_reason
        if self.evidence_dir:
            d["evidence_dir"] = self.evidence_dir
        return d


class GuardrailViolation(Exception):
    """Raised when an action would violate the allowlist or risk policy."""
