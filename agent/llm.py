"""Anthropic tool-use wrapper for the discovery agent loop.

The model is given a fixed set of structured tools (never raw coordinates)
and must call exactly one per turn. This keeps the discovery trace directly
convertible into artifact steps -- the recorder doesn't have to guess what
the model "meant."
"""
from __future__ import annotations

import os
from typing import Any

import anthropic

DEFAULT_MODEL = os.environ.get("AGENT_LLM_MODEL", "claude-sonnet-4-5-20250929")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click an interactive element identified by its accessibility role and accessible name (as shown in the observation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["role", "name"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into a textbox identified by role+name. Overwrites existing content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["role", "name", "value"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option in a combobox/select identified by role+name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["role", "name", "value"],
        },
    },
    {
        "name": "extract",
        "description": "Record a value visible on the current page as a named output of this capability.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_name": {"type": "string"},
                "label": {"type": "string", "description": "The visible label next to the value, e.g. 'Savings Balance'"},
            },
            "required": ["output_name", "label"],
        },
    },
    {
        "name": "done",
        "description": "Call this once the goal has been fully accomplished. Provide any extracted outputs and a one-line summary of the final state (used as the replay checkpoint).",
        "input_schema": {
            "type": "object",
            "properties": {
                "outputs": {"type": "object"},
                "checkpoint_summary": {"type": "string", "description": "A short description of what confirms success, e.g. \"heading 'Member Detail' is visible\""},
            },
            "required": ["checkpoint_summary"],
        },
    },
    {
        "name": "escalate",
        "description": "Call this if you cannot safely determine the next action -- ambiguous state, no matching element after inspecting the page, or you would need to perform an irreversible action you're unsure about. Do NOT guess.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

SYSTEM_PROMPT = """You are a computer-use agent operating a legacy internal banking application \
on behalf of a downstream AI system. You are given a goal and must accomplish it by calling \
exactly one tool per turn based on the current page observation.

Rules:
- Only ever target elements by role + accessible name, exactly as shown in the observation. Never invent an element that isn't listed.
- Work step by step: one action per turn, then wait for the next observation.
- If the page shows a legitimate business result (e.g. "no member found", a validation error), that is a normal outcome -- extract/report it and call done, do not treat it as a failure.
- If you open a form that submits data (creating or changing a record), you may proceed, but be deliberate -- this is a demo system, not a real bank.
- If you are unsure what to do next, or a required element is missing, call escalate rather than guessing.
- Call done as soon as the goal's checkpoint state is reached.
"""


def next_action(client: anthropic.Anthropic, messages: list[dict[str, Any]], model: str = DEFAULT_MODEL) -> Any:
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_choice={"type": "any"},
        messages=messages,
    )
    return response
