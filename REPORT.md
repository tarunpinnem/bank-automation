# Report — Computer-Use Automation System

Effort was concentrated on the pieces the brief calls load-bearing — artifact schema,
deterministic replay + error handling, and the safety/escalation model — not spread
evenly. No stretch goal was attempted; that time went into depth on those three instead
(see Cuts). Where something was deliberately kept thin, it's called out in place.

## Architecture

Two paths share only the locator/action layer, on purpose — discovery is expensive and
improvisational, replay is cheap and must be boring:

```
DISCOVERY (paid, LLM in the loop, once per capability)
  goal + base_url -> browser.launch() (real Chromium, fixed CDP port)
  loop (max 12 turns): observe() accessibility tree -> Claude picks ONE tool call
    -> act.<click|type|select|extract>() executed for real -> result fed back
  until done / escalate / max turns -> DiscoveryTrace -> recorder.build_artifact()

REPLAY (free, no LLM, every other time)
  Artifact + typed inputs -> apply tenant override -> browser.launch()
  for each step: act.resolve(role -> text_near -> css) . click/fill/select
    (risky steps pause for approval; extract deferred to after checkpoint)
  -> wait for checkpoint: success | business outcome | recoverable
  -> exactly one RunResult: SUCCESS | BUSINESS_OUTCOME | HARD_FAILURE | ESCALATED
```

**The cost model this buys, measured from this project's own evidence, not asserted:**
discovering `member.lookup_balance` took 4 LLM turns / 12.3s wall clock;
`subaccount.open` (more steps, a form) took 8 turns / 24.8s. Every replay after that is
**0 LLM calls**, ~4s for the lookup and ~10s for the sub-account flow (`evidence/replay_*`
timestamps). That's the "reliably and cheaply" the brief's Section 1 asks for, not a
claim about it.

**Perception is accessibility-tree-first**, not screenshot-first: `observe.py` turns
Playwright's `page.accessibility.snapshot()` into a compact `role: 'name'` list. This is
what still works with no clean DOM (the brief's own framing), and it's the same
vocabulary `recorder.py` turns into locators — what the model saw and what the artifact
targets never have to be kept in sync by hand.

**One action layer for both paths.** `act.py`'s `resolve()` (primary strategy, then each
fallback in order) is called from both `discover.py` and `replay.py` — there's no
separate "recording" implementation vs. "replay" implementation to drift apart.

**The mock target** (`mock_bank/app.py`) is deliberately hostile the way real legacy
tools are: nested tables, no CSS classes or `data-testid`, fields with no `<label>`, and
a repeatable "Session Expired" interstitial (`POST /_admin/arm_timeout`) to exercise
recoverable-condition handling on demand.

## Artifact schema

The capability contract: typed inputs/outputs, ordered steps with resilient locators, an
explicit checkpoint, declared business outcomes, versioning/identity metadata. Full
dataclasses in `agent/artifact.py`; real examples in `artifacts/*.json`.

```jsonc
{
  "capability_id": "member.lookup_balance", "version": 1,
  "target": { "app": "meridian-teller-console", "base_url": "...",
              "app_version_fingerprint": "sha256:9bd1a3cd33dfbd4f" },
  "inputs":  { "member_id": { "type": "string", "required": true, "sensitive": true } },
  "outputs": { "savings_balance": { "type": "number" }, "member_found": { "type": "boolean" } },
  "steps": [
    { "id": "s0", "action": "type", "value": "{{member_id}}", "risk_level": "safe",
      "target": { "primary": { "strategy": "role", "role": "textbox", "name": "Member ID" },
                  "fallbacks": [{ "strategy": "text_near", "label": "Member ID" },
                                { "strategy": "css", "selector": "input[name=\"member_id\"]" }] } },
    { "id": "s2", "action": "extract", "output": "savings_balance", "extract_label": "Savings Balance" }
  ],
  "checkpoint": { "success": { "role": "heading", "name_contains": "Member Detail" } },
  "business_outcomes": [{ "name": "member_not_found", "when": { "text_contains": "No member found" },
                           "result": { "member_found": false, "savings_balance": null } }],
  "tenant_overrides": {}
}
```
(full examples: `artifacts/*.json`)

- **Every target has a fallback chain, not one selector.** Primary is `role + accessible
  name` (how the LLM reasoned live); `text_near` is an XPath "label cell -> next cell"
  strategy for legacy tables; `css` is derived *at record time* from the resolved
  element's real attributes. Replay tries them in order.
- **Business outcomes are declared, not inferred at replay time** — "no such member" is
  part of the contract (`when`/`result`), not text-pattern-matched on the fly.
- **`sensitive: true`** on an input drives redaction (see Safety). **`risk_level` lives on
  the step**, not the action type, since the same `click` is safe on Search and risky on
  a form submit.
- **Human-readable on purpose** — a reviewer can read the JSON top to bottom and know
  exactly what replay will do, which is also what makes a tenant override (below) a
  small readable diff instead of a black box.

## Determinism & error handling

Replay makes **zero LLM calls** and always returns exactly one of four terminal results.
Every row below is a real run — `evidence/<dir>/run.jsonl`, not a description:

| Kind | Meaning | Real evidence dir |
|---|---|---|
| `SUCCESS` | Reached checkpoint, extracted outputs | `replay_member.lookup_balance_1789669464_ab5378` |
| `BUSINESS_OUTCOME` | Reached a *declared* alternate state | `replay_member.lookup_balance_1789669468_f029d9` |
| `HARD_FAILURE` | Nothing matched — genuinely unexpected | `replay_member.lookup_balance_1789671657_3cb226` |
| `ESCALATED` | Paused, didn't come back resumed | `replay_subaccount.open_1789773371_5b93da` (cancelled) |

A fifth internal state, `RECOVERABLE`, never surfaces as final: a known interstitial
(session-timeout page, armed via `POST /_admin/arm_timeout`) is dismissed and the whole
action sequence is retried from a fresh navigation (`MAX_ATTEMPTS = 2`) — real run in
`replay_member.lookup_balance_1789753148_b8d0ee` (two `navigate` events, one dismissal,
then `SUCCESS`). The retry-the-whole-sequence approach is a deliberate simplification
(see Cuts) — sound because both capabilities are idempotent flows re-entered from the
home page, not because a general "resume mid-step" mechanism exists. (Escalation that
*is* resumed by a human, rather than cancelled, is covered separately below — it isn't a
`RunResult` kind, it's the live handoff itself.)

Two bugs found and fixed are worth naming, since they're the "no clean DOM" failure mode
the brief warns about, not generic flakiness:

- **Extraction racing navigation** — an `extract` right after a navigating click
  sometimes read stale state. Fixed by deferring all extraction until after the
  checkpoint confirms the expected page.
- **Nested-table locator ambiguity** — `:has-text()` matched an *ancestor* row in the
  mock app's nested tables. Replaced with XPath
  (`//td[normalize-space(.)='{label}']/following-sibling::td[1]`) that only matches the
  exact label cell.

`tests/test_replay.py` exercises `SUCCESS`/`BUSINESS_OUTCOME`/`HARD_FAILURE` against a
hand-built artifact — no API key, no network. Coverage gaps are in Cuts.

## Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `observe.py` + `act.py`: an artifact's steps target
`{role, name}`, and `observe()`/`resolve()` are the only two functions that know the
surface is a browser at all. Everything above that line — the artifact schema,
`replay.py`'s attempt loop, the checkpoint/business-outcome contract — talks only in
role/name/text, never in DOM terms. A legacy web app (iframes, framesets) is already the
easy case: Playwright resolves frames the same way, so it's a change inside `observe()`,
not to the schema. A desktop app is a bigger lift but the same shape: swap
`page.accessibility.snapshot()` for an OS accessibility API (UI Automation on Windows,
the Accessibility API on macOS) behind the same `observe() -> Observation` /
`resolve(target) -> control` interface, and an artifact recorded against a desktop screen
would still be `{role: "button", name: "Search"}` — the primary locator strategy carries
over unchanged. Only the *fallback* strategies are surface-specific (`text_near` and
`css` are DOM concepts); a desktop driver would need its own fallback family, which is
new code behind the existing seam, not a schema redesign. None of this is built — it's
the reason `act.py`'s strategies are named and pluggable rather than inlined into
`replay.py`.

**Multi-tenant reuse.** Two mechanisms, modeled on diffing a supplier config against a
base template rather than a fully separate integration per tenant:

**Base-config + override.** `Artifact.apply_tenant_override(tenant_id)` returns a new,
patched artifact — the base is never mutated. An override can replace `target` fields
(a tenant-specific URL) and patch individual steps by id (a relabeled button, a different
field name). Per-tenant differences stay a small, reviewable diff against a known-good
base rather than a forked copy that can silently drift.

**App-version fingerprinting.** `fingerprint.py` hashes the sorted set of `(role, name)`
pairs on a capability's landing page into a short signature, stored at record time.
`cli.py check-drift` recomputes it live and reports a mismatch — stable against
incidental text changes, sensitive to anything that would actually break locator
resolution. A cheap, no-LLM-call tripwire, meant to be checked *before* a failed replay
in production discovers the drift for you.

Both mechanisms are real, hand-exercised code, not just design prose — but neither has
been run against a second, genuinely different app (see Cuts).

## Escalation & handoff

The requirement this answers: when a human takes over, is it the *same* session the
automation was mid-flow in, or a fresh one? Here it's the same session, provably:

1. `browser.launch()` starts Chromium with a **fixed** CDP port, not an ephemeral one.
2. A risky step (replay) or the model's `escalate` call (discovery) triggers
   `pause_for_human()`: writes `pending_intervention.json`, then **polls** for a
   resume/cancel signal — never closes the page, never issues another command until told.
3. `operator/mock_operator.py`, a **separate process**, calls `attach_over_cdp()` onto
   that same live browser, shows the human the same accessibility-tree observation the
   LLM would see, and lets them act directly on the live page.
4. The operator writes `resume_signal.json` (exactly which actions, timestamped) or
   `cancel_signal.json`. The paused process notices, clears the control files, re-observes
   current state, and continues.

Run for real, not simulated: replay on `subaccount.open`'s risky `s5` executed `s0`–`s4`,
requested approval, blocked; a second process attached over CDP, clicked "Continue,"
signaled resume; the original process picked back up and returned `SUCCESS` —
`evidence/replay_subaccount.open_*_b94c4b/`. Both sides log to the same `run.jsonl`, so
the handoff is auditable: what was asked for, what the human did, when control returned.

What triggers escalation today: a `risky`-tagged step (replay) and the model's own
`escalate` call (discovery). `HARD_FAILURE` does **not** currently escalate — it just
returns to the caller. Routing hard failures into the same path is the most valuable next
addition (Cuts).

Scoped down deliberately: the operator console is a bare CLI, not real-time co-browsing.
The mechanism it proves — pause without closing, expose the live session over CDP,
capture what happened, resume — is real; the console's polish is not what this submission
spent its time on.

## Safety

Three independent, separately auditable guardrails (`agent/safety.py`), enforced on both
discovery and replay:

- **Allowlist** — `Allowlist.check_url()`/`check_action()` reject any URL outside the
  capability's base origin or any action outside a fixed set; raises and logs rather than
  silently ignoring.
- **Risk gating** — `requires_approval(step_risk_level, auto_approve_risky)`: a `risky`
  step blocks on human approval by default; `--auto-approve-risky` opts out for
  unattended runs, and that choice is itself logged.
- **Redaction** — `redact_text()` strips SSN-shaped and long account/card-number-shaped
  patterns plus any input marked `sensitive: true` from every log line and DOM snapshot
  before it's written; `EvidenceWriter` applies this everywhere, so evidence is safe to
  hand a reviewer unmodified.

All three verified directly, not just by inspection: a disallowed URL/action raises and
logs rather than executing; a redaction call on SSN-shaped and long-digit-run text
returns `[REDACTED]` for both. The logs in `evidence/` are the real, already-redacted
output of real runs.

## Cuts

- **No stretch goals attempted, deliberately** — per Section 5's own framing (depth over
  breadth), with the core not yet fully proven, an added feature would have traded depth
  on the load-bearing pieces for breadth that isn't rewarded. If extending, the
  agent-facing capability interface (exposing `artifacts/*.json` as a typed, invokable
  catalog) is what I'd build first.
- **No automated coverage for `discover.py` or the operator/escalation flow** —
  `tests/test_replay.py` covers the deterministic path fully with no API key; discovery
  and handoff were verified by running them for real, not a repeatable harness. Mocking
  the Anthropic tool-use responses and scripting a "virtual operator" over CDP is next.
- **`HARD_FAILURE` doesn't trigger escalation** — only a risky step or the model's own
  `escalate` call does today. Routing hard failures into the same path is the clearest gap.
- **Recoverable retry redoes the whole action sequence**, not just the interrupted step —
  sound for these two idempotent flows, not a general "resume from step N" mechanism.
- **No diffing of human corrections back into the artifact** — captured in evidence, but
  never compared against what the artifact expected. A scoped version, for the risky-step
  case where we know exactly which step the human stood in for, would flag "drift
  suspected" rather than auto-committing. The clearest "with another day" item.
- **One mock app, two capabilities, and the surface/tenant mechanisms only hand-exercised**
  — real code (overrides, fingerprinting, the observe/act seam), not proven against a
  second real app or a desktop surface; that was out of scope for the time available.
- **The operator console is a bare CLI**, not real-time co-browsing — scoped down
  deliberately; see Escalation & handoff.
