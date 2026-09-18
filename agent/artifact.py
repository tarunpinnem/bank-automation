"""Artifact schema: the saved, reusable, agent-invocable capability.

Plain dataclasses + to/from dict (no pydantic dependency needed) so the
schema is easy to read end-to-end in one file. Matches the Phase 2 design:
- ordered steps with primary+fallback locators
- typed inputs/outputs
- an explicit checkpoint
- declared business outcomes (not inferred at replay time)
- a risk level per step (safety gate)
- an app fingerprint + optional per-tenant overrides (multi-tenant reuse)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass
class InputParam:
    type: str
    required: bool = True
    example: Any = None
    sensitive: bool = False  # redact this value from logs/screenshots if True


@dataclass
class OutputField:
    type: str


@dataclass
class Step:
    id: str
    action: str  # click | type | select | navigate | wait_for | extract
    target: dict[str, Any] | None = None
    value: str | None = None           # literal or "{{param_name}}" template
    condition: dict[str, Any] | None = None  # for wait_for
    output: str | None = None          # for extract: which output field this fills
    extract_label: str | None = None
    risk_level: str = "safe"           # safe | risky


@dataclass
class BusinessOutcomeRule:
    when: dict[str, Any]     # e.g. {"text_contains": "No member found"}
    result: dict[str, Any]   # outputs to set, e.g. {"member_found": False}
    name: str = "unspecified"


@dataclass
class Target:
    app: str
    base_url: str
    app_version_fingerprint: str = ""


@dataclass
class Artifact:
    capability_id: str
    version: int
    target: Target
    inputs: dict[str, InputParam]
    outputs: dict[str, OutputField]
    steps: list[Step]
    checkpoint: dict[str, Any]
    business_outcomes: list[BusinessOutcomeRule] = field(default_factory=list)
    risk_level: str = "safe"
    schema_version: int = SCHEMA_VERSION
    tenant_overrides: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "version": self.version,
            "target": asdict(self.target),
            "inputs": {k: asdict(v) for k, v in self.inputs.items()},
            "outputs": {k: asdict(v) for k, v in self.outputs.items()},
            "steps": [asdict(s) for s in self.steps],
            "checkpoint": self.checkpoint,
            "business_outcomes": [asdict(b) for b in self.business_outcomes],
            "risk_level": self.risk_level,
            "tenant_overrides": self.tenant_overrides,
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @staticmethod
    def load(path: str | Path) -> "Artifact":
        data = json.loads(Path(path).read_text())
        return Artifact(
            capability_id=data["capability_id"],
            version=data["version"],
            target=Target(**data["target"]),
            inputs={k: InputParam(**v) for k, v in data["inputs"].items()},
            outputs={k: OutputField(**v) for k, v in data["outputs"].items()},
            steps=[Step(**s) for s in data["steps"]],
            checkpoint=data["checkpoint"],
            business_outcomes=[BusinessOutcomeRule(**b) for b in data.get("business_outcomes", [])],
            risk_level=data.get("risk_level", "safe"),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            tenant_overrides=data.get("tenant_overrides", {}),
        )

    def apply_tenant_override(self, tenant_id: str) -> "Artifact":
        """Return a NEW Artifact with a tenant's overrides patched in, leaving
        the base artifact untouched. This is the base-config + override
        pattern: one artifact per vendor app, specialized per tenant rather
        than re-recorded per tenant."""
        override = self.tenant_overrides.get(tenant_id)
        if not override:
            return self

        import copy
        patched = copy.deepcopy(self)
        if "target" in override:
            for k, v in override["target"].items():
                setattr(patched.target, k, v)
        for patch in override.get("steps_patch", []):
            step_id = patch["id"]
            for step in patched.steps:
                if step.id == step_id:
                    for dotted_key, val in patch.items():
                        if dotted_key == "id":
                            continue
                        _set_dotted(step, dotted_key, val)
        return patched


def _set_dotted(obj: Any, dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    cur = obj
    for p in parts[:-1]:
        cur = cur[p] if isinstance(cur, dict) else getattr(cur, p)
    last = parts[-1]
    if isinstance(cur, dict):
        cur[last] = value
    else:
        setattr(cur, last, value)
