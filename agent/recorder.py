"""Converts a successful DiscoveryTrace into a saved, reusable Artifact.

Deliberately NOT fully automatic: a human (or capability author) still
supplies which literal values used during discovery are actually input
PARAMETERS (vs. incidental choices), what the declared outputs/business
outcomes are, and the target app identity. This mirrors how the brief
frames it -- reviewable, versioned, decoupled from the raw transcript --
rather than pretending a single run can infer a general-purpose contract
on its own.
"""
from __future__ import annotations

from .artifact import Artifact, Target, InputParam, OutputField, Step, BusinessOutcomeRule
from .discover import DiscoveryTrace


def build_artifact(
    trace: DiscoveryTrace,
    *,
    app_name: str,
    app_version_fingerprint: str,
    inputs: dict[str, InputParam],
    value_to_param: dict[str, str],
    outputs: dict[str, OutputField],
    business_outcomes: list[BusinessOutcomeRule],
    checkpoint: dict,
    risk_level: str = "safe",
    version: int = 1,
    risky_step_ids: set[str] | None = None,
) -> Artifact:
    risky_step_ids = risky_step_ids or set()
    steps: list[Step] = []
    for ts in trace.steps:
        if ts.action == "extract":
            steps.append(Step(
                id=ts.id, action="extract",
                output=ts.extract_output, extract_label=ts.extract_label,
            ))
            continue

        target = None
        value = None
        if ts.role and ts.name:
            fallbacks = [{"strategy": "text_near", "label": ts.name}]
            if ts.css_fallback:
                fallbacks.append({"strategy": "css", "selector": ts.css_fallback})
            target = {
                "primary": {"strategy": "role", "role": ts.role, "name": ts.name},
                "fallbacks": fallbacks,
            }
        if ts.value is not None:
            value = value_to_param.get(ts.value, ts.value)
            if value in inputs:
                value = "{{" + value + "}}"

        steps.append(Step(
            id=ts.id, action=ts.action, target=target, value=value,
            risk_level="risky" if ts.id in risky_step_ids else "safe",
        ))

    return Artifact(
        capability_id=trace.capability_id,
        version=version,
        target=Target(app=app_name, base_url=trace.base_url, app_version_fingerprint=app_version_fingerprint),
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        checkpoint=checkpoint,
        business_outcomes=business_outcomes,
        risk_level=risk_level,
    )
