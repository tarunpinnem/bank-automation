"""Discovery: the goal -> LLM-driven -> live-surface loop (Section 3.1).

Produces a DiscoveryTrace that recorder.py turns into a saved Artifact.
This is the one path in the whole system that must be genuinely LLM-driven
end to end -- no shortcuts, no scripted fallback standing in for the model.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

from . import act, observe as obs_mod
from .browser import launch, Session
from .evidence import EvidenceWriter
from .safety import Allowlist, GuardrailViolation
from .errors import ResultKind, RunResult


@dataclass
class TraceStep:
    id: str
    action: str
    role: str | None = None
    name: str | None = None
    value: str | None = None
    css_fallback: str | None = None
    extract_output: str | None = None
    extract_label: str | None = None


@dataclass
class DiscoveryTrace:
    goal: str
    capability_id: str
    base_url: str
    steps: list[TraceStep] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    checkpoint_summary: str = ""
    final_url: str = ""
    result: RunResult | None = None
    evidence_dir: str = ""


MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "12"))


def _tool_result_message(tool_use_id: str, content: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
    }


def run_discovery(
    goal: str,
    base_url: str,
    capability_id: str,
    allowlist: Allowlist | None = None,
    headless: bool = True,
    api_key: str | None = None,
) -> DiscoveryTrace:
    allowlist = allowlist or Allowlist.default_for(base_url)
    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    ev = EvidenceWriter(run_kind="discover", capability_id=capability_id)
    ev.log("discovery_start", goal=goal, base_url=base_url, capability_id=capability_id)

    session: Session = launch(headless=headless)
    page = session.page
    trace = DiscoveryTrace(goal=goal, capability_id=capability_id, base_url=base_url, evidence_dir=str(ev.dir))

    try:
        allowlist.check_url(base_url)
        allowlist.check_action("navigate")
        page.goto(base_url)
        ev.log("navigate", url=base_url)

        messages: list[dict[str, Any]] = []
        step_counter = 0

        for turn in range(MAX_STEPS):
            observation = obs_mod.observe(page)
            ev.log("observation", url=observation.url, n_elements=len(observation.elements))

            if turn == 0:
                user_text = f"Goal: {goal}\n\n{observation.to_prompt_text()}"
            else:
                user_text = observation.to_prompt_text()
            messages.append({"role": "user", "content": user_text})

            response = client.messages.create(
                model=os.environ.get("AGENT_LLM_MODEL", "claude-sonnet-4-5-20250929"),
                max_tokens=1024,
                system=_system_prompt(),
                tools=_tools(),
                tool_choice={"type": "any"},
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_blocks:
                ev.log("no_tool_call", stop_reason=response.stop_reason)
                break
            block = tool_blocks[0]
            tool_name = block.name
            tool_input = block.input
            ev.log("model_action", tool=tool_name, input=tool_input)

            if tool_name == "done":
                trace.outputs.update(tool_input.get("outputs") or {})
                trace.checkpoint_summary = tool_input.get("checkpoint_summary", "")
                trace.final_url = page.url
                ev.screenshot(page, "final_success")
                trace.result = RunResult(kind=ResultKind.SUCCESS, outputs=trace.outputs, evidence_dir=str(ev.dir))
                ev.log("discovery_done", outputs=trace.outputs, checkpoint=trace.checkpoint_summary)
                break

            if tool_name == "escalate":
                reason = tool_input.get("reason", "unspecified")
                ev.screenshot(page, "escalation")
                trace.result = RunResult(kind=ResultKind.ESCALATED, escalation_reason=reason, evidence_dir=str(ev.dir))
                ev.log("discovery_escalated", reason=reason)
                break

            try:
                allowlist.check_action(_action_kind(tool_name))
                step = _execute_tool(page, tool_name, tool_input, step_counter)
                if step:
                    trace.steps.append(step)
                    step_counter += 1
                result_text = "ok"
            except GuardrailViolation as e:
                result_text = f"BLOCKED by allowlist: {e}"
                ev.log("guardrail_blocked", tool=tool_name, error=str(e))
            except Exception as e:
                result_text = f"error: {e}"
                ev.log("action_error", tool=tool_name, error=str(e))

            messages.append(_tool_result_message(block.id, result_text))
        else:
            trace.result = RunResult(kind=ResultKind.HARD_FAILURE, evidence_dir=str(ev.dir))
            ev.log("discovery_max_steps_exceeded")

    finally:
        ev.close()
        session.close()

    return trace


def _action_kind(tool_name: str) -> str:
    return {
        "click": "click", "type_text": "type", "select_option": "select",
        "extract": "extract",
    }.get(tool_name, tool_name)


def _target_with_fallback(role: str, name: str) -> dict[str, Any]:
    """The model refers to elements by (role, name) from the observation --
    which, for an unlabeled legacy control, is a NAME WE SYNTHESIZED (see
    observe.py), not the element's real accessible name. A strict role+name
    lookup would then find nothing, so every live action also carries a
    text_near fallback under that same name."""
    return {
        "primary": {"strategy": "role", "role": role, "name": name},
        "fallbacks": [{"strategy": "text_near", "label": name}],
    }


def _execute_tool(page, tool_name: str, tool_input: dict[str, Any], step_idx: int) -> TraceStep | None:
    step_id = f"s{step_idx}"
    if tool_name == "click":
        role, name = tool_input["role"], tool_input["name"]
        loc = act.resolve(page, _target_with_fallback(role, name))
        css = act.best_effort_css(loc)
        loc.click()
        return TraceStep(id=step_id, action="click", role=role, name=name, css_fallback=css)

    if tool_name == "type_text":
        role, name, value = tool_input["role"], tool_input["name"], tool_input["value"]
        loc = act.resolve(page, _target_with_fallback(role, name))
        css = act.best_effort_css(loc)
        loc.fill(value)
        return TraceStep(id=step_id, action="type", role=role, name=name, value=value, css_fallback=css)

    if tool_name == "select_option":
        role, name, value = tool_input["role"], tool_input["name"], tool_input["value"]
        loc = act.resolve(page, _target_with_fallback(role, name))
        css = act.best_effort_css(loc)
        loc.select_option(value)
        return TraceStep(id=step_id, action="select", role=role, name=name, value=value, css_fallback=css)

    if tool_name == "extract":
        output_name, label = tool_input["output_name"], tool_input["label"]
        return TraceStep(id=step_id, action="extract", extract_output=output_name, extract_label=label)

    return None


def _system_prompt() -> str:
    from .llm import SYSTEM_PROMPT
    return SYSTEM_PROMPT


def _tools() -> list[dict[str, Any]]:
    from .llm import TOOLS
    return TOOLS
