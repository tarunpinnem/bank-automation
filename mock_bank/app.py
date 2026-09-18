"""
Mock "legacy" core banking app — the proxy target for the automation system.

Deliberately built like an old enterprise internal tool:
- server-rendered HTML, no JS framework
- table-based layout, no CSS classes/ids meant for automation
- no data-testid attributes anywhere
- session-based navigation (server holds state in a dict, not REST-y)

Two flows, matching the assignment's own example goals:
  1. Member lookup -> read savings balance ("look up member 12345 and read
     their current savings balance")
  2. Open a new sub-account -> confirmation screen ("open a new sub-account
     for this member and reach the confirmation screen")

Not a real bank. Fake data only. Runs on localhost.
"""
from __future__ import annotations

import random
import string
import time
from flask import Flask, request, redirect, url_for, session

app = Flask(__name__)
app.secret_key = "dev-only-not-a-secret"

# ---------------------------------------------------------------------------
# Fake data — no real people, no real PII.
# ---------------------------------------------------------------------------
MEMBERS = {
    "12345": {"name": "Alicia Rowan", "savings_balance": 4820.55, "checking_balance": 1120.00},
    "23456": {"name": "Devon Sharma", "savings_balance": 150.10, "checking_balance": 300.25},
    "34567": {"name": "Priya Nadeau", "savings_balance": 98213.40, "checking_balance": 5210.00},
}

SUBACCOUNTS: dict[str, list[dict]] = {mid: [] for mid in MEMBERS}

# Simple in-memory session flag to simulate a session timeout on demand,
# used to exercise the "recoverable condition" replay path.
SIMULATE_TIMEOUT_ONCE = {"armed": False}


def _account_number() -> str:
    return "SA-" + "".join(random.choices(string.digits, k=8))


def layout(title: str, body: str) -> str:
    # Old-school table layout on purpose: no <header>/<nav>/<main>, no classes.
    return f"""<html>
<head><title>{title}</title></head>
<body>
<table border="1" width="100%" cellpadding="4" cellspacing="0">
<tr><td bgcolor="#003366">
  <font color="white" size="4"><b>Meridian Credit Union &mdash; Teller Console</b></font>
</td></tr>
<tr><td>
<table border="0" cellpadding="8">
<tr><td valign="top" width="140">
  <table border="0">
    <tr><td><a href="/">Member Lookup</a></td></tr>
    <tr><td><a href="/">Home</a></td></tr>
  </table>
</td>
<td valign="top">
{body}
</td></tr>
</table>
</td></tr>
<tr><td align="center"><font size="1">Internal use only &mdash; Teller Console v3.2 (demo)</font></td></tr>
</table>
</body>
</html>"""


@app.route("/", methods=["GET"])
def home():
    body = """
<h2>Member Lookup</h2>
<form method="GET" action="/search">
<table border="0" cellpadding="4">
<tr>
  <td>Member ID</td>
  <td><input type="text" name="member_id" size="20"></td>
</tr>
<tr>
  <td colspan="2" align="right"><input type="submit" value="Search"></td>
</tr>
</table>
</form>
"""
    return layout("Teller Console - Home", body)


@app.route("/search", methods=["GET"])
def search():
    member_id = (request.args.get("member_id") or "").strip()

    # Simulate a transient slow load if requested (for replay robustness demo)
    if request.args.get("_slow"):
        time.sleep(2)

    if not member_id:
        body = "<h2>Member Lookup</h2><p><b>Validation error:</b> Member ID is required.</p><p><a href='/'>Back</a></p>"
        return layout("Teller Console - Error", body)

    member = MEMBERS.get(member_id)
    if not member:
        body = f"""
<h2>Search Results</h2>
<p>No member found for ID <b>{member_id}</b>.</p>
<p><a href="/">Back to search</a></p>
"""
        return layout("Teller Console - No Results", body)

    body = f"""
<h2>Member Detail</h2>
<table border="1" cellpadding="6" cellspacing="0">
<tr><td><b>Member ID</b></td><td>{member_id}</td></tr>
<tr><td><b>Name</b></td><td>{member['name']}</td></tr>
<tr><td><b>Savings Balance</b></td><td>${member['savings_balance']:,.2f}</td></tr>
<tr><td><b>Checking Balance</b></td><td>${member['checking_balance']:,.2f}</td></tr>
</table>
<br>
<form method="GET" action="/subaccount/new">
<input type="hidden" name="member_id" value="{member_id}">
<input type="submit" value="Open Sub-Account">
</form>
<p><a href="/">Back to search</a></p>
"""
    return layout("Teller Console - Member Detail", body)


@app.route("/subaccount/new", methods=["GET"])
def subaccount_new_form():
    member_id = (request.args.get("member_id") or "").strip()
    member = MEMBERS.get(member_id)
    if not member:
        body = "<p>Unknown member.</p><p><a href='/'>Back</a></p>"
        return layout("Teller Console - Error", body)

    body = f"""
<h2>Open New Sub-Account</h2>
<p>Member: {member['name']} ({member_id})</p>
<form method="POST" action="/subaccount/create">
<input type="hidden" name="member_id" value="{member_id}">
<table border="0" cellpadding="4">
<tr><td>Sub-Account Type</td>
  <td>
    <select name="account_type">
      <option value="holiday_club">Holiday Club</option>
      <option value="vacation">Vacation Fund</option>
      <option value="emergency">Emergency Fund</option>
    </select>
  </td>
</tr>
<tr><td>Opening Deposit ($)</td><td><input type="text" name="opening_deposit" size="10"></td></tr>
<tr><td colspan="2" align="right"><input type="submit" value="Continue"></td></tr>
</table>
</form>
<p><a href="/search?member_id={member_id}">Back to member detail</a></p>
"""
    return layout("Teller Console - New Sub-Account", body)


@app.route("/subaccount/create", methods=["POST"])
def subaccount_create():
    member_id = (request.form.get("member_id") or "").strip()
    account_type = request.form.get("account_type") or ""
    deposit_raw = (request.form.get("opening_deposit") or "").strip()

    member = MEMBERS.get(member_id)
    if not member:
        body = "<p>Unknown member.</p><p><a href='/'>Back</a></p>"
        return layout("Teller Console - Error", body)

    # Validation error path (business outcome, not a crash)
    try:
        deposit = float(deposit_raw)
        if deposit < 25:
            raise ValueError()
    except ValueError:
        body = f"""
<h2>Open New Sub-Account</h2>
<p><b>Validation error:</b> Opening deposit must be a number of at least $25.00.</p>
<form method="GET" action="/subaccount/new">
<input type="hidden" name="member_id" value="{member_id}">
<input type="submit" value="Back to form">
</form>
"""
        return layout("Teller Console - Validation Error", body)

    acct_number = _account_number()
    SUBACCOUNTS[member_id].append(
        {"account_number": acct_number, "type": account_type, "deposit": deposit}
    )

    body = f"""
<h2>Confirmation</h2>
<p>Sub-account opened successfully.</p>
<table border="1" cellpadding="6" cellspacing="0">
<tr><td><b>Member</b></td><td>{member['name']} ({member_id})</td></tr>
<tr><td><b>New Account Number</b></td><td>{acct_number}</td></tr>
<tr><td><b>Type</b></td><td>{account_type}</td></tr>
<tr><td><b>Opening Deposit</b></td><td>${deposit:,.2f}</td></tr>
</table>
<p><a href="/search?member_id={member_id}">Back to member detail</a></p>
"""
    return layout("Teller Console - Confirmation", body)


@app.route("/_admin/arm_timeout", methods=["POST"])
def arm_timeout():
    """Test-only hook: makes the NEXT /search request show a fake session-expired
    interstitial once, to exercise the recoverable-condition replay path."""
    SIMULATE_TIMEOUT_ONCE["armed"] = True
    return {"armed": True}


@app.before_request
def maybe_inject_timeout():
    if request.path == "/search" and SIMULATE_TIMEOUT_ONCE["armed"]:
        SIMULATE_TIMEOUT_ONCE["armed"] = False
        body = """
<h2>Session Expired</h2>
<p>Your session has timed out. Please acknowledge to continue.</p>
<form method="GET" action="/">
<input type="submit" value="OK">
</form>
"""
        return layout("Teller Console - Session Expired", body)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
