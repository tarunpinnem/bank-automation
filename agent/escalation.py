"""Human-in-the-loop escalation & handoff (Section 3.6).

Model: automation PAUSES (keeps the live browser open, keeps polling) --
it never exits and never closes the session. A separate "mock operator"
process attaches to the SAME browser over CDP (see browser.attach_over_cdp),
acts on the live page directly, then writes a resume signal file. The
paused automation process notices the signal, re-observes current state,
and continues. Both sides read/write the same run's evidence directory, so
the handoff is auditable: what was asked for, what the human did, when
control came back.

Scope note (documented, per the brief): the "operator console" here is a
bare CLI (operator/mock_operator.py), not a real-time co-browsing UI. The
control-transfer mechanism itself -- pause, expose the live session, signal
resume, capture what happened -- is real.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

PENDING_FILENAME = "pending_intervention.json"
RESUME_FILENAME = "resume_signal.json"
CANCEL_FILENAME = "cancel_signal.json"


@dataclass
class PendingIntervention:
    capability_id: str
    step_id: str
    reason: str
    goal_or_capability: str
    current_url: str
    ts: float


def write_pending(evidence_dir: str, intervention: PendingIntervention) -> Path:
    path = Path(evidence_dir) / PENDING_FILENAME
    path.write_text(json.dumps(asdict(intervention), indent=2))
    return path


def clear_control_files(evidence_dir: str) -> None:
    for fname in (PENDING_FILENAME, RESUME_FILENAME, CANCEL_FILENAME):
        p = Path(evidence_dir) / fname
        if p.exists():
            p.unlink()


class InterventionOutcome:
    RESUMED = "resumed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


def pause_for_human(
    evidence_dir: str,
    capability_id: str,
    step_id: str,
    reason: str,
    goal_or_capability: str,
    current_url: str,
    logger,
    timeout_s: float = 180.0,
    poll_interval_s: float = 1.0,
) -> tuple[str, dict[str, Any] | None]:
    """Blocks (polling) until a resume/cancel signal appears or timeout.
    Does NOT touch the browser -- the caller keeps its page handle open and
    simply stops issuing commands until this returns RESUMED."""
    intervention = PendingIntervention(
        capability_id=capability_id, step_id=step_id, reason=reason,
        goal_or_capability=goal_or_capability, current_url=current_url, ts=time.time(),
    )
    write_pending(evidence_dir, intervention)
    logger("escalation_requested", step_id=step_id, reason=reason, evidence_dir=evidence_dir)
    print(f"\n>>> INTERVENTION NEEDED: {reason}")
    print(f">>> Run: python operator/mock_operator.py {evidence_dir}")
    print(f">>> Waiting up to {timeout_s:.0f}s for a human to act and resume...\n")

    resume_path = Path(evidence_dir) / RESUME_FILENAME
    cancel_path = Path(evidence_dir) / CANCEL_FILENAME
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        if cancel_path.exists():
            data = json.loads(cancel_path.read_text())
            logger("escalation_cancelled", by=data.get("operator", "unknown"))
            clear_control_files(evidence_dir)
            return InterventionOutcome.CANCELLED, data
        if resume_path.exists():
            data = json.loads(resume_path.read_text())
            logger("escalation_resumed", operator_actions=data.get("actions", []))
            clear_control_files(evidence_dir)
            return InterventionOutcome.RESUMED, data
        time.sleep(poll_interval_s)

    logger("escalation_timed_out")
    clear_control_files(evidence_dir)
    return InterventionOutcome.TIMED_OUT, None
