"""Per-capability configuration used when turning a discovery trace into a
saved artifact. Kept separate from recorder.py (the generic converter) so
new capabilities are just new entries here, not new code.

Each entry says: what inputs/outputs the capability contract is, which
literal values used during discovery correspond to which input parameter,
which declared business outcomes to attach, the success checkpoint, and
which step(s) are risky and therefore gated behind human approval on
replay.
"""
from __future__ import annotations

from dataclasses import dataclass

from .artifact import InputParam, OutputField, BusinessOutcomeRule


@dataclass
class CapabilityConfig:
    capability_id: str
    goal_template: str
    app_name: str
    inputs: dict[str, InputParam]
    outputs: dict[str, OutputField]
    value_to_param: dict[str, str]
    business_outcomes: list[BusinessOutcomeRule]
    checkpoint: dict
    risky_step_ids: set[str]


CAPABILITIES: dict[str, CapabilityConfig] = {

    "member.lookup_balance": CapabilityConfig(
        capability_id="member.lookup_balance",
        goal_template=(
            "Look up member {member_id} in the Member Lookup screen and read their "
            "current savings balance. Extract the numeric balance as output "
            "'savings_balance' (a plain number, no currency symbol or commas). "
            "If the member is not found, that is a normal result -- report it, "
            "don't treat it as an error."
        ),
        app_name="meridian-teller-console",
        inputs={
            "member_id": InputParam(type="string", required=True, example="12345", sensitive=True),
        },
        outputs={
            "savings_balance": OutputField(type="number"),
            "member_found": OutputField(type="boolean"),
        },
        value_to_param={},  # filled at record time with {literal_value_used: "member_id"}
        business_outcomes=[
            BusinessOutcomeRule(
                name="member_not_found",
                when={"text_contains": "No member found"},
                result={"member_found": False, "savings_balance": None},
            ),
        ],
        checkpoint={"success": {"role": "heading", "name_contains": "Member Detail"}},
        risky_step_ids=set(),  # read-only flow, nothing risky
    ),

    "subaccount.open": CapabilityConfig(
        capability_id="subaccount.open",
        goal_template=(
            "For member {member_id}, open a new sub-account of type '{account_type}' "
            "with an opening deposit of {opening_deposit} dollars, and reach the "
            "confirmation screen. Extract the new account number as output "
            "'account_number'."
        ),
        app_name="meridian-teller-console",
        inputs={
            "member_id": InputParam(type="string", required=True, example="12345", sensitive=True),
            "account_type": InputParam(type="string", required=True, example="vacation"),
            "opening_deposit": InputParam(type="number", required=True, example="100"),
        },
        outputs={
            "account_number": OutputField(type="string"),
        },
        value_to_param={},  # filled at record time
        business_outcomes=[
            BusinessOutcomeRule(
                name="validation_error_deposit_too_low",
                when={"text_contains": "Validation error"},
                result={"account_number": None},
            ),
        ],
        checkpoint={"success": {"role": "heading", "name_contains": "Confirmation"}},
        risky_step_ids=set(),  # set after discovery once we know which step id clicked "Continue" on the create form
    ),
}
