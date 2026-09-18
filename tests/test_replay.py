"""Sanity tests for the replay engine against the mock bank app.

Not a full pytest suite -- run directly: `python tests/test_replay.py`
Requires the mock bank app running on localhost:8000.

These exercise replay.py using a HAND-BUILT artifact (not one produced by
discovery) so replay logic can be verified independently of any LLM call --
useful given the discovery run itself costs an API call and should be run
deliberately, not on every test iteration.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.artifact import Artifact, Target, InputParam, OutputField, Step, BusinessOutcomeRule
from agent.replay import replay
from agent.errors import ResultKind

BASE_URL = "http://127.0.0.1:8000"


def make_lookup_artifact() -> Artifact:
    return Artifact(
        capability_id="member.lookup_balance",
        version=1,
        target=Target(app="meridian-teller-console", base_url=BASE_URL, app_version_fingerprint="test"),
        inputs={"member_id": InputParam(type="string", required=True, example="12345", sensitive=True)},
        outputs={"savings_balance": OutputField(type="number"), "member_found": OutputField(type="boolean")},
        steps=[
            Step(
                id="s0", action="type", value="{{member_id}}",
                target={"primary": {"strategy": "role", "role": "textbox", "name": "Member ID"},
                        "fallbacks": [{"strategy": "css", "selector": "input[name=\"member_id\"]"}]},
            ),
            Step(
                id="s1", action="click",
                target={"primary": {"strategy": "role", "role": "button", "name": "Search"},
                        "fallbacks": [{"strategy": "css", "selector": "input[type=\"submit\"]"}]},
            ),
            Step(id="s2", action="extract", output="savings_balance", extract_label="Savings Balance"),
        ],
        checkpoint={"success": {"role": "heading", "name_contains": "Member Detail"}},
        business_outcomes=[
            BusinessOutcomeRule(name="member_not_found", when={"text_contains": "No member found"},
                                 result={"member_found": False, "savings_balance": None}),
        ],
    )


def run(label, result, expect_kind):
    status = "PASS" if result.kind == expect_kind else "FAIL"
    print(f"[{status}] {label}: kind={result.kind.value} outputs={result.outputs} business_outcome={result.business_outcome} failure={result.failure}")
    return status == "PASS"


def main():
    artifact = make_lookup_artifact()
    all_ok = True

    r1 = replay(artifact, {"member_id": "12345"}, headless=True)
    all_ok &= run("valid member -> success", r1, ResultKind.SUCCESS)
    assert "4,820.55" in str(r1.outputs.get("savings_balance")), r1.outputs

    r2 = replay(artifact, {"member_id": "99999"}, headless=True)
    all_ok &= run("unknown member -> business outcome", r2, ResultKind.BUSINESS_OUTCOME)
    assert r2.business_outcome == "member_not_found"

    # Hard failure case: point the artifact at a URL where the expected
    # elements don't exist at all (checkpoint truly unreachable).
    broken = make_lookup_artifact()
    broken.target.base_url = BASE_URL + "/does-not-exist"
    r3 = replay(broken, {"member_id": "12345"}, headless=True)
    all_ok &= run("broken target -> hard failure", r3, ResultKind.HARD_FAILURE)

    print("\nALL PASS" if all_ok else "\nSOME FAILED")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
