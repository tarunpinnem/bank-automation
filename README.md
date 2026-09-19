# Computer-Use Automation System — Meridian Teller Console

An LLM drives a deliberately ugly, table-based legacy web app once ("discovery"), that
successful run is recorded as a versioned, typed, reviewable **artifact**, and every run
after that is a **deterministic replay** — same steps, same fallback locators, zero LLM
calls — with a human escalation path for anything the artifact can't handle on its own.

Two capabilities are implemented end-to-end against the mock app:

| Capability | Goal |
|---|---|
| `member.lookup_balance` | Look up a member and read their savings balance |
| `subaccount.open` | Open a new sub-account for a member and reach the confirmation screen |

## Why this exists

Enterprise back-office UIs (the kind this assignment models) don't get retired just
because they're ugly, and they don't get clean automation hooks either — no test IDs,
no semantic HTML, session-based navigation. Every trip through the UI to look something
up or take an action is either a person's time or an LLM call. This system pays the LLM
cost once per capability, then reuses the recorded path for free, and falls back to a
human — on the *same live browser session*, not a fresh one — the moment something the
artifact doesn't recognize happens.

## Project layout

```
mock_bank/app.py       the target app: a fake credit-union teller console (Flask)
agent/
  browser.py           launch / attach-over-CDP to a Chromium session
  observe.py            accessibility tree -> compact text observation for the LLM
  act.py                locator resolution (role -> text-proximity -> CSS) + actions
  llm.py                Claude tool definitions + system prompt
  discover.py           the real LLM-driven discovery loop (Section below: "genuinely LLM-driven")
  recorder.py           DiscoveryTrace -> saved Artifact
  artifact.py           the artifact schema (dataclasses + to/from JSON)
  capabilities.py       per-capability config (inputs/outputs/business outcomes/checkpoint)
  replay.py             deterministic replay engine, no LLM in the loop
  escalation.py         pause-for-human / resume signaling
  safety.py             allowlist, risk gating, redaction
  evidence.py           structured per-run JSONL log + screenshots
  fingerprint.py        app-version fingerprint for drift detection
operator/mock_operator.py   CLI a human uses to attach to a paused live session
cli.py                 discover / replay / check-drift subcommands
artifacts/             saved, real artifacts (from genuine discovery runs)
evidence/              real discovery + replay run logs and screenshots
tests/test_replay.py   replay-engine sanity tests (no API key required)
REPORT.md              architecture write-up (7 required sections)
```

## Setup

Requires Python 3.10+.

```bash
pip install flask anthropic playwright
python -m playwright install chromium
```

An Anthropic API key is only needed for `discover` (replay never calls the LLM):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Start the mock bank app (leave it running in its own terminal):

```bash
python mock_bank/app.py
# serves http://127.0.0.1:8000
```

## Demo path

**1. Discover** — a real Claude session drives the live app once and produces an artifact:

```bash
python cli.py discover --capability member.lookup_balance --member-id 12345
python cli.py discover --capability subaccount.open \
  --member-id 12345 --account-type vacation --opening-deposit 100
```

Each run prints the goal, the model's own checkpoint summary, and where it wrote
`artifacts/<capability>.v1.json` plus a full evidence trail under `evidence/`.
Already-recorded artifacts for both capabilities ship in `artifacts/` so replay can be
tried without an API key.

**2. Replay** — deterministic, no LLM call:

```bash
python cli.py replay --artifact artifacts/member.lookup_balance.v1.json --input member_id=12345
python cli.py replay --artifact artifacts/member.lookup_balance.v1.json --input member_id=99999   # business outcome: not found
python cli.py replay --artifact artifacts/subaccount.open.v1.json \
  --input member_id=12345 --input account_type=vacation --input opening_deposit=100
python cli.py replay --artifact artifacts/subaccount.open.v1.json \
  --input member_id=12345 --input account_type=vacation --input opening_deposit=5   # business outcome: validation error
```

`subaccount.open`'s final submit step (`s5`, "Continue" — it creates a real record) is
tagged `risk_level: risky` and pauses for approval by default:

```bash
# run this; it will print "INTERVENTION NEEDED" and block
python cli.py replay --artifact artifacts/subaccount.open.v1.json \
  --input member_id=12345 --input account_type=vacation --input opening_deposit=100

# in a second terminal, attach to the SAME live browser and act:
python operator/mock_operator.py evidence/<the run dir printed above>
operator> click button Continue
operator> resume
```

Or skip the gate for a quick end-to-end run: add `--auto-approve-risky`.

**3. Exercise the recoverable-condition path** (a "Session Expired" interstitial the
replay engine knows how to dismiss and retry from):

```bash
curl -X POST http://127.0.0.1:8000/_admin/arm_timeout
python cli.py replay --artifact artifacts/member.lookup_balance.v1.json --input member_id=12345
```

**4. Check for drift** (has the app's UI changed since this artifact was recorded?):

```bash
python cli.py check-drift --artifact artifacts/member.lookup_balance.v1.json
```

**5. Run the replay sanity tests** (hand-built artifact, no API key, no network):

```bash
python tests/test_replay.py
```

## Evidence

`/evidence/` contains real run logs (`run.jsonl`, redacted) and screenshots from actual
executions — two genuine LLM-driven discovery runs, plus replays covering every result
kind. To check any of these without reading the whole directory:

| What it proves | Evidence dir |
|---|---|
| Genuine LLM-driven discovery (2 real runs, 4 and 8 model turns) | `discover_member.lookup_balance_*`, `discover_subaccount.open_*` |
| Replay `SUCCESS` | `replay_member.lookup_balance_1789669464_ab5378` |
| Replay `BUSINESS_OUTCOME` (no such member) | `replay_member.lookup_balance_1789669468_f029d9` |
| Replay `HARD_FAILURE` | `replay_member.lookup_balance_1789671657_3cb226` |
| Replay `SUCCESS`, `subaccount.open` (own path, not just via the handoff below) | `replay_subaccount.open_1789669531_52fd83` |
| Replay `BUSINESS_OUTCOME`, `subaccount.open` (validation error) | `replay_subaccount.open_1789669542_36f3d5` |
| Replay `ESCALATED` (paused, human cancelled) | `replay_subaccount.open_1789773371_5b93da` |
| `RECOVERABLE` auto-retry (session-timeout dismissed, then success) | `replay_member.lookup_balance_1789753148_b8d0ee` |
| Live CDP handoff: paused → resumed by a second process → `SUCCESS` | `replay_subaccount.open_1789669557_b94c4b` |

See `REPORT.md`'s Determinism & error handling section for how to read a `run.jsonl`.

## What's cut / not built

See REPORT.md's **Cuts** section for the full list with reasoning — notably: no
automated test coverage for `discover.py` or the operator flow (both are exercised
manually/live, not in a repeatable test harness), no diffing of human-escalation
corrections back into the artifact (human actions are captured in evidence but not
compared against the artifact's expected steps), and the operator console is a bare CLI,
not a real-time co-browsing UI.
