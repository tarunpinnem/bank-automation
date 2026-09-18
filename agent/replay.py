"""Deterministic replay: the production execution path (Section 3.3).

No LLM in the loop. Loads a saved Artifact, executes its steps with
role/name -> text-proximity -> CSS fallback locator resolution, evaluates
the checkpoint, and returns exactly one RunResult: SUCCESS, BUSINESS_OUTCOME,
HARD_FAILURE, or ESCALATED (paused for a human). RECOVERABLE conditions are
handled internally (dismissed, then the action sequence is retried once) and
never surface as the final result.
"""
from __future__ import annotations

import time
from typing import Any

from . import act
from .artifact import Artifact
from .browser import launch, Session
from .evidence import EvidenceWriter
from .safety import Allowlist, GuardrailViolation, requires_approval
from .errors import ResultKind, RunResult, StepFailureDetail
from . import escalation

# Known recoverable interstitials this replay engine will auto-dismiss
# before proceeding, rather than treating them as failures. Declared here
# (engine-level) rather than per-artifact, since they're a property of the
# app/vendor platform, not of any one capability.
RECOVERABLE_RULES: list[dict[str, Any]] = [
    {
        "trigger": {"text_contains": "Session Expired"},
        "dismiss": {"strategy": "role", "role": "button", "name": "OK"},
        "name": "session_timeout_interstitial",
    },
]

MAX_ATTEMPTS = 2  # one retry after a recoverable condition is dismissed


def _check_and_dismiss_recoverable(page, ev: EvidenceWriter) -> bool:
    """Returns True if a recoverable condition was found and dismissed."""
    content = page.content()
    for rule in RECOVERABLE_RULES:
        trigger = rule["trigger"]
        if "text_contains" in trigger and trigger["text_contains"] in content:
            ev.log("recoverable_condition", rule=rule["name"])
            try:
                loc = act.resolve(page, {"primary": rule["dismiss"]})
                loc.click()
                ev.log("recoverable_dismissed", rule=rule["name"])
                return True
            except Exception as e:
                ev.log("recoverable_dismiss_failed", rule=rule["name"], error=str(e))
    return False


def _substitute(value: str | None, inputs: dict[str, Any]) -> str | None:
    if value is None:
        return None
    if value.startswith("{{") and value.endswith("}}"):
        key = value[2:-2]
        return str(inputs.get(key, ""))
    return value


def _wait_for_any(page, branches: list[tuple[str, dict[str, Any]]], timeout_ms: int) -> str | None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for label, condition in branches:
            if act.wait_for_condition(page, condition, timeout_ms=1):
                return label
        page.wait_for_timeout(150)
    return None


def _run_attempt(
    page, artifact: Artifact, inputs: dict[str, Any], auto_approve_risky: bool,
    ev: EvidenceWriter, escalation_timeout_s: float,
) -> tuple[bool, RunResult | None]:
    """Runs one attempt of the action-step sequence + checkpoint wait.
    Returns (recoverable_hit, result). If recoverable_hit is True, result is
    None and the caller should retry from a fresh navigation."""
    outputs: dict[str, Any] = {}
    i = 0
    while i < len(artifact.steps):
        step = artifact.steps[i]

        if _check_and_dismiss_recoverable(page, ev):
            return True, None

        if step.action == "extract":
            # Deliberately deferred: extraction only happens AFTER the
            # checkpoint confirms we've reached the expected state (below).
            # Extracting inline, mid-sequence, races whatever navigation the
            # previous step may have triggered.
            i += 1
            continue

        if requires_approval(step.risk_level, auto_approve_risky):
            outcome, _data = escalation.pause_for_human(
                evidence_dir=str(ev.dir),
                capability_id=artifact.capability_id,
                step_id=step.id,
                reason=f"Step '{step.id}' ({step.action}) is marked risky and requires approval before it runs.",
                goal_or_capability=artifact.capability_id,
                current_url=page.url,
                logger=ev.log,
                timeout_s=escalation_timeout_s,
            )
            if outcome != escalation.InterventionOutcome.RESUMED:
                ev.screenshot(page, "escalation_unresolved")
                return False, RunResult(
                    kind=ResultKind.ESCALATED,
                    escalation_reason=f"step {step.id}: {outcome}",
                    evidence_dir=str(ev.dir),
                )
            # Human handled this step live via the operator surface; move on.
            i += 1
            continue

        try:
            value = _substitute(step.value, inputs)
            if step.action == "click":
                act.resolve(page, step.target).click()
            elif step.action == "type":
                act.resolve(page, step.target).fill(value or "")
            elif step.action == "select":
                act.resolve(page, step.target).select_option(value or "")
            elif step.action == "navigate":
                page.goto(value or artifact.target.base_url)
            else:
                raise ValueError(f"Unknown action: {step.action}")
            ev.log("step_executed", step_id=step.id, action=step.action)
        except act.LocatorNotFound as e:
            ev.screenshot(page, f"hard_failure_{step.id}")
            return False, RunResult(
                kind=ResultKind.HARD_FAILURE,
                failure=StepFailureDetail(step_id=step.id, expected=str(step.target),
                                           observed=f"no locator strategy matched (tried: {e.tried})"),
                evidence_dir=str(ev.dir),
            )
        i += 1

    # --- checkpoint: success vs. declared business outcome vs. recoverable ---
    branches: list[tuple[str, dict[str, Any]]] = [("success", artifact.checkpoint.get("success", artifact.checkpoint))]
    for idx, bo in enumerate(artifact.business_outcomes):
        branches.append((f"outcome:{idx}", bo.when))
    for idx, rule in enumerate(RECOVERABLE_RULES):
        branches.append((f"recoverable:{idx}", rule["trigger"]))

    matched = _wait_for_any(page, branches, timeout_ms=5000)
    ev.screenshot(page, "checkpoint_state")

    if matched is None:
        return False, RunResult(
            kind=ResultKind.HARD_FAILURE,
            failure=StepFailureDetail(step_id="checkpoint", expected=str(branches), observed=page.inner_text("body")[:500]),
            evidence_dir=str(ev.dir),
        )

    if matched.startswith("recoverable:"):
        if _check_and_dismiss_recoverable(page, ev):
            return True, None
        return False, RunResult(
            kind=ResultKind.HARD_FAILURE,
            failure=StepFailureDetail(step_id="checkpoint", expected="dismissable interstitial", observed="matched but dismiss failed"),
            evidence_dir=str(ev.dir),
        )

    if matched == "success":
        for step in artifact.steps:
            if step.action == "extract":
                outputs[step.output] = act.extract_text_near(page, step.extract_label or "")
        # Any declared boolean output not otherwise set defaults to True on
        # the success path (e.g. member_found), rather than being silently
        # absent from the result contract.
        for name, spec in artifact.outputs.items():
            if name not in outputs and spec.type == "boolean":
                outputs[name] = True
        ev.log("replay_success", outputs=outputs)
        return False, RunResult(kind=ResultKind.SUCCESS, outputs=outputs, evidence_dir=str(ev.dir))

    idx = int(matched.split(":")[1])
    bo = artifact.business_outcomes[idx]
    outputs.update(bo.result)
    ev.log("replay_business_outcome", name=bo.name, outputs=outputs)
    return False, RunResult(kind=ResultKind.BUSINESS_OUTCOME, outputs=outputs, business_outcome=bo.name, evidence_dir=str(ev.dir))


def replay(
    artifact: Artifact,
    inputs: dict[str, Any],
    *,
    tenant_id: str | None = None,
    headless: bool = True,
    auto_approve_risky: bool = False,
    allowlist: Allowlist | None = None,
    escalation_timeout_s: float = 180.0,
) -> RunResult:
    if tenant_id:
        artifact = artifact.apply_tenant_override(tenant_id)

    for name, spec in artifact.inputs.items():
        if spec.required and name not in inputs:
            raise ValueError(f"Missing required input: {name}")

    allowlist = allowlist or Allowlist.default_for(artifact.target.base_url)
    sensitive_values = [str(v) for k, v in inputs.items() if artifact.inputs.get(k) and artifact.inputs[k].sensitive]

    ev = EvidenceWriter(run_kind="replay", capability_id=artifact.capability_id, sensitive_values=sensitive_values)
    ev.log("replay_start", capability_id=artifact.capability_id, version=artifact.version, tenant_id=tenant_id)

    session: Session = launch(headless=headless)
    page = session.page

    try:
        allowlist.check_url(artifact.target.base_url)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            page.goto(artifact.target.base_url)
            ev.log("navigate", url=artifact.target.base_url, attempt=attempt)

            recoverable_hit, result = _run_attempt(page, artifact, inputs, auto_approve_risky, ev, escalation_timeout_s)
            if recoverable_hit:
                # Known interstitial dismissed mid-flow (e.g. a session
                # timeout). Safe, general answer: redo the capability's
                # action sequence from the top rather than guess which step
                # to resume from -- sound for these idempotent read/navigate
                # flows. A precise step-level resume would need per-step
                # idempotency tracking; noted as a cut in REPORT.md.
                continue
            return result

        return RunResult(
            kind=ResultKind.HARD_FAILURE,
            failure=StepFailureDetail(step_id="checkpoint", expected="stable state after retry", observed="recoverable condition recurred"),
            evidence_dir=str(ev.dir),
        )

    except GuardrailViolation as e:
        ev.log("guardrail_blocked", error=str(e))
        return RunResult(
            kind=ResultKind.HARD_FAILURE,
            failure=StepFailureDetail(step_id="allowlist", expected="permitted URL/action", observed=str(e)),
            evidence_dir=str(ev.dir),
        )
    finally:
        ev.close()
        session.close()
