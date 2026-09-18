#!/usr/bin/env python3
"""Command-line entrypoint.

    python cli.py discover --capability member.lookup_balance --member-id 12345
    python cli.py discover --capability subaccount.open --member-id 12345 --account-type vacation --opening-deposit 100

    python cli.py replay --artifact artifacts/member.lookup_balance.v1.json --input member_id=23456
    python cli.py replay --artifact artifacts/subaccount.open.v1.json \\
        --input member_id=12345 --input account_type=vacation --input opening_deposit=100

    python cli.py check-drift --artifact artifacts/member.lookup_balance.v1.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from agent.capabilities import CAPABILITIES
from agent.recorder import build_artifact
from agent.discover import run_discovery
from agent.replay import replay as do_replay
from agent.artifact import Artifact
from agent.safety import Allowlist
from agent.browser import launch
from agent.fingerprint import compute_fingerprint, check_drift

BASE_URL_DEFAULT = "http://127.0.0.1:8000"
ARTIFACTS_DIR = "artifacts"


def cmd_discover(args: argparse.Namespace) -> int:
    config = CAPABILITIES.get(args.capability)
    if not config:
        print(f"Unknown capability: {args.capability}. Known: {list(CAPABILITIES)}")
        return 1

    field_values = {
        "member_id": args.member_id,
        "account_type": args.account_type,
        "opening_deposit": args.opening_deposit,
    }
    goal = config.goal_template.format(**{k: v for k, v in field_values.items() if v is not None})
    print(f"Goal: {goal}\n")

    allowlist = Allowlist.default_for(args.base_url)
    trace = run_discovery(
        goal=goal,
        base_url=args.base_url,
        capability_id=args.capability,
        allowlist=allowlist,
        headless=not args.headed,
    )

    if trace.result is None or trace.result.kind.value != "success":
        print(f"Discovery did not complete successfully: {trace.result}")
        print(f"Evidence: {trace.evidence_dir}")
        return 1

    print(f"Discovery succeeded. Evidence: {trace.evidence_dir}")
    print(f"Checkpoint summary (model's own words): {trace.checkpoint_summary}")
    print(f"Extracted outputs: {trace.outputs}")

    # value_to_param: map the literal input values we told the model to use
    # back to their parameter names, so the recorder can templatize them.
    value_to_param = {}
    for pname, pval in field_values.items():
        if pval is not None:
            value_to_param[str(pval)] = pname

    # Determine risky step ids: for subaccount.open, the final click on
    # "Continue" (the form submit) creates a real record -- gate it.
    risky_step_ids = set()
    if args.capability == "subaccount.open":
        for s in trace.steps:
            if s.action == "click" and s.name and "continue" in s.name.lower():
                risky_step_ids.add(s.id)

    fp_session = launch(headless=True)
    try:
        fp_session.page.goto(args.base_url)
        fingerprint = compute_fingerprint(fp_session.page)
    finally:
        fp_session.close()

    artifact = build_artifact(
        trace,
        app_name=config.app_name,
        app_version_fingerprint=fingerprint,
        inputs=config.inputs,
        value_to_param=value_to_param,
        outputs=config.outputs,
        business_outcomes=config.business_outcomes,
        checkpoint=config.checkpoint,
        risky_step_ids=risky_step_ids,
    )

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    out_path = os.path.join(ARTIFACTS_DIR, f"{args.capability}.v{artifact.version}.json")
    artifact.save(out_path)
    print(f"\nSaved artifact: {out_path}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    artifact = Artifact.load(args.artifact)
    inputs: dict[str, object] = {}
    for kv in args.input or []:
        k, _, v = kv.partition("=")
        if v.replace(".", "", 1).isdigit():
            v = float(v) if "." in v else int(v)
        inputs[k] = v

    result = do_replay(
        artifact, inputs,
        tenant_id=args.tenant,
        headless=not args.headed,
        auto_approve_risky=args.auto_approve_risky,
        escalation_timeout_s=args.escalation_timeout,
    )
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if result.kind.value in ("success", "business_outcome") else 2


def cmd_check_drift(args: argparse.Namespace) -> int:
    artifact = Artifact.load(args.artifact)
    session = launch(headless=True)
    try:
        session.page.goto(artifact.target.base_url)
        drifted, current = check_drift(session.page, artifact.target.app_version_fingerprint)
    finally:
        session.close()
    print(f"Stored fingerprint:  {artifact.target.app_version_fingerprint}")
    print(f"Current fingerprint: {current}")
    print("DRIFTED" if drifted else "No drift detected")
    return 1 if drifted else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_discover = sub.add_parser("discover")
    p_discover.add_argument("--capability", required=True, choices=list(CAPABILITIES))
    p_discover.add_argument("--base-url", default=BASE_URL_DEFAULT)
    p_discover.add_argument("--member-id", default="12345")
    p_discover.add_argument("--account-type", default=None)
    p_discover.add_argument("--opening-deposit", default=None)
    p_discover.add_argument("--headed", action="store_true")
    p_discover.set_defaults(func=cmd_discover)

    p_replay = sub.add_parser("replay")
    p_replay.add_argument("--artifact", required=True)
    p_replay.add_argument("--input", action="append", help="key=value, repeatable")
    p_replay.add_argument("--tenant", default=None)
    p_replay.add_argument("--headed", action="store_true")
    p_replay.add_argument("--auto-approve-risky", action="store_true")
    p_replay.add_argument("--escalation-timeout", type=float, default=180.0)
    p_replay.set_defaults(func=cmd_replay)

    p_drift = sub.add_parser("check-drift")
    p_drift.add_argument("--artifact", required=True)
    p_drift.set_defaults(func=cmd_check_drift)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
