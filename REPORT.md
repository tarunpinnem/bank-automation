# Report — Computer-Use Automation System

## Architecture

Two execution paths share almost nothing except the locator/action layer, on purpose —
discovery is expensive and improvisational, replay is cheap and must be boring:

```
DISCOVERY (paid, LLM in the loop, run once per capability)
  goal + base_url
    -> browser.launch()                      real Chromium, fixed CDP port
    -> loop (max 12 turns):
         observe.observe(page)                accessibility tree -> compact text
         Claude (tool-use, tool_choice=any)    picks ONE structured tool call
         act.<click|type_text|select_option|extract>()   executed for real
         result fed back as a tool_result
       until: done / escalate / max turns
    -> DiscoveryTrace (goal, ordered TraceSteps, outputs, checkpoint summary)
    -> recorder.build_artifact()             -> artifacts/<capability>.v<N>.json

REPLAY (free, no LLM, run every other time)
  Artifact + typed inputs
    -> apply tenant override (if any)
    -> browser.launch()
    -> for each step: act.resolve(role -> text_near -> css) . click/fill/select
       (risky steps pause for human approval first; extract steps deferred)
    -> wait for checkpoint: success | declared business outcome | recoverable
    -> exactly one RunResult: SUCCESS | BUSINESS_OUTCOME | HARD_FAILURE | ESCALATED
```

**Perception is accessibility-tree-first, not screenshot-first.** `observe.py` calls
Playwright's `page.accessibility.snapshot()` and turns it into a short, structured
`role: 'name'` list plus a text excerpt for context. This is the representation that
still works when there's no clean DOM to grab onto (the brief's own framing), and it's
also directly what `recorder.py` turns into locators — so "what the model saw" and
"what the artifact targets" are the same vocabulary, not two systems that have to be
kept in sync by hand.

**One action layer for both paths.** `act.py`'s `resolve()` — try the primary
locator strategy, then each fallback in order — is called from `discover.py` (model
picks role+name live) and from `replay.py` (artifact supplies the full fallback chain).
There's no separate "recording" implementation of clicking a button versus a "replay"
implementation; what actually happened during discovery and what replay does are
provably the same code path.

**The mock target (`mock_bank/app.py`)** is deliberately hostile in the way real legacy
internal tools are: nested `<table>` layout, no CSS classes or `data-testid` anywhere,
form fields with no `<label for=...>` (just an adjacent `<td>`), server-side session
state instead of a REST API, and a repeatable "Session Expired" interstitial to exercise
recoverable-condition handling on demand (`POST /_admin/arm_timeout`).

## Artifact schema

An artifact is the capability contract: typed inputs/outputs, an ordered step sequence
with resilient locators, an explicit success checkpoint, declared business outcomes, and
enough identity/versioning metadata to know when it's stale. Full dataclasses in
`agent/artifact.py`; real examples in `artifacts/*.json`.

```jsonc
{
  "schema_version": 1,
  "capability_id": "member.lookup_balance",
  "version": 1,
  "target": {
    "app": "meridian-teller-console",
    "base_url": "http://127.0.0.1:8000",
    "app_version_fingerprint": "sha256:9bd1a3cd33dfbd4f"   // see Heterogeneity section
  },
  "inputs":  { "member_id": { "type": "string", "required": true, "sensitive": true } },
  "outputs": { "savings_balance": { "type": "number" }, "member_found": { "type": "boolean" } },
  "steps": [
    { "id": "s0", "action": "type", "value": "{{member_id}}", "risk_level": "safe",
      "target": {
        "primary":   { "strategy": "role", "role": "textbox", "name": "Member ID" },
        "fallbacks": [ { "strategy": "text_near", "label": "Member ID" },
                       { "strategy": "css", "selector": "input[name=\"member_id\"]" } ]
      } },
    { "id": "s1", "action": "click", "risk_level": "safe",
      "target": { "primary": { "strategy": "role", "role": "button", "name": "Search" },
                  "fallbacks": [ { "strategy": "text_near", "label": "Search" } ] } },
    { "id": "s2", "action": "extract", "output": "savings_balance", "extract_label": "Savings Balance" }
  ],
  "checkpoint": { "success": { "role": "heading", "name_contains": "Member Detail" } },
  "business_outcomes": [
    { "name": "member_not_found", "when": { "text_contains": "No member found" },
      "result": { "member_found": false, "savings_balance": null } }
  ],
  "risk_level": "safe",
  "tenant_overrides": {}
}
```

Design choices that matter:

- **Every step target has a fallback chain, not a single selector.** Primary is
  `role + accessible name` (matches how the LLM reasoned about the page during
  discovery); `text_near` is an XPath-based "find the label cell, take the next cell"
  strategy for legacy table layouts; `css` is a best-effort selector derived *at record
  time* from whatever real attributes the resolved element had. Replay tries them in
  order and only fails if none resolve.
- **Business outcomes are declared, not inferred at replay time.** "No member found" or
  "deposit below minimum" are normal, expected answers a teller can get — they're part
  of the contract (`when` a page condition, `result` the outputs to set), not something
  replay has to guess is or isn't a failure by pattern-matching page text on the fly.
- **`sensitive: true` on an input** (e.g. `member_id`) is what drives redaction — see
  Safety.
- **`risk_level` lives on the step**, not the action type, because the same `click`
  action is safe on a search button and risky on a form-submit button — see Safety.
- **Human-readable on purpose.** No compiled bytecode, no opaque replay format — an
  artifact is a JSON file a reviewer can read top to bottom and understand exactly what
  will happen on replay, which is also what makes tenant overrides (below) a readable
  diff instead of a black box.

## Determinism & error handling

Replay (`agent/replay.py`) makes **zero LLM calls** and always returns exactly one of
four terminal results — `RunResult.kind` — regardless of how the run went:

| Kind | Meaning | Example from this project's evidence |
|---|---|---|
| `SUCCESS` | Reached the checkpoint, extracted outputs | member found, balance read |
| `BUSINESS_OUTCOME` | Reached a *declared* alternate state | "No member found", deposit below $25 minimum |
| `HARD_FAILURE` | Nothing matched — genuinely unexpected | artifact pointed at a URL that doesn't exist |
| `ESCALATED` | Paused for a human and didn't come back resumed | risky step, human didn't approve/resume in time |

A fifth internal state, `RECOVERABLE`, is handled *inside* replay and never returned as
a final result: it's a known, declared interstitial (currently: the app's session-expiry
page) that the engine dismisses and retries the whole action sequence for, from a fresh
navigation. `replay()` wraps step execution in an attempt loop (`MAX_ATTEMPTS = 2`);
hitting a recoverable condition mid-sequence or at the checkpoint consumes one attempt
and starts over rather than trying to resume mid-step. That's a deliberate, documented
simplification (see Cuts) — it's sound because both capabilities are idempotent
read/navigate flows re-entered from the home page, not because a general "resume from
step N" mechanism exists.

Two bugs found and fixed while building this are worth calling out because they're
exactly the "no clean DOM" failure mode the brief warns about, not generic flakiness:

- **Extraction racing navigation.** An `extract` step placed inline, right after a click
  that triggers a page transition, sometimes read stale/empty state. Fixed by deferring
  *all* extraction until after the checkpoint confirms the expected page was reached —
  extraction is now a post-condition, never a mid-sequence step.
- **Nested-table locator ambiguity.** `text_near`'s first implementation used
  Playwright's `:has-text()`, which matched an *ancestor* `<tr>` in the mock app's nested
  layout and silently resolved to the wrong cell. Replaced with an XPath query
  (`//td[normalize-space(.)='{label}']/following-sibling::td[1]`) that only matches the
  exact label cell.

`tests/test_replay.py` exercises all three of `SUCCESS`, `BUSINESS_OUTCOME`, and
`HARD_FAILURE` against a hand-built artifact (no API key, no network) — see Cuts for
what test coverage does *not* exist.

## Heterogeneity & multi-tenant

Two mechanisms address "the same capability across slightly different deployments,"
directly modeled on diffing a supplier's config against a base template rather than
maintaining a fully separate integration per tenant:

**Base-config + override.** `Artifact.apply_tenant_override(tenant_id)` returns a new,
patched artifact — the base artifact recorded once against the reference deployment is
never mutated. A tenant override can replace `target` fields (e.g. a different
`base_url` if that tenant runs their own instance) and patch individual steps by id
(e.g. a relabeled button, a different field name) via dotted-path assignment. This keeps
per-tenant differences as a small, reviewable diff against a known-good base, the same
shape as a supplier-specific config override sitting on top of a shared template — not a
forked copy of the whole artifact that can silently drift from the base.

**App-version fingerprinting.** `fingerprint.py` hashes the sorted set of `(role, name)`
pairs visible on a capability's landing page into a short signature
(`sha256:9bd1a3cd33dfbd4f...`), stored in the artifact at record time. `cli.py
check-drift` recomputes it live and reports a mismatch. It's intentionally stable
against incidental text/formatting changes (a balance amount changing doesn't change the
signature) but changes if controls are added, removed, relabeled, or reordered in a way
that would actually threaten locator resolution — a cheap, no-LLM-call tripwire for
"this artifact might no longer be valid," to be checked before or alongside a replay
rather than discovering it via a failed run in production.

Neither mechanism was exercised against a genuinely different second app in this
submission — both artifacts here target the one mock app. That's a scoping cut (see
Cuts): the *mechanism* is real and tested against a hand-edited override, but "prove it
against two independently-built apps" was out of scope for the time available.

## Escalation & handoff

The core requirement this section answers is: when a human takes over, is it the *same*
session the automation was in the middle of, or does the person start over from scratch?
Here it's the same session, provably:

1. `browser.launch()` starts Chromium with a **fixed** `--remote-debugging-port`
   (`AGENT_CDP_PORT`, default 9333) rather than an ephemeral one.
2. When replay hits a risky step (or discovery calls the `escalate` tool),
   `escalation.pause_for_human()` writes a `pending_intervention.json` describing what's
   needed, then **polls** for a resume/cancel signal file — it never closes the page or
   the browser, and never issues another command until told to.
3. `operator/mock_operator.py`, run as a **separate process**, calls
   `browser.attach_over_cdp()` to connect to that same live, already-open browser (not a
   new one), shows the human the same accessibility-tree observation the LLM would see,
   and lets them `click` / `type` / `select` directly against the live page.
4. The operator writes `resume_signal.json` (capturing exactly which actions it took, with
   timestamps) or `cancel_signal.json`. The paused replay/discovery process notices it,
   clears the control files, and — on resume — re-observes current state and continues
   from there.

This was run for real (not simulated) against the `subaccount.open` artifact's risky
`s5` step: replay executed `s0`–`s4`, requested approval, blocked; a second process
attached over CDP to the same live page, clicked "Continue," and signaled resume; the
original process picked back up, reached the checkpoint, and returned `SUCCESS` — see
`evidence/replay_subaccount.open_*_b94c4b/`. Everything either side did is logged to the
same run's `run.jsonl`, so the handoff is auditable end to end: what was asked for, what
the human actually clicked, and when control came back.

What triggers escalation today, precisely: a step tagged `risk_level: "risky"` in the
artifact during replay (currently just `subaccount.open`'s final submit — it creates a
real record), and the model's own `escalate` tool call during discovery when it decides
it's stuck. `HARD_FAILURE` does **not** currently escalate — a replay that can't find a
locator or never reaches its checkpoint just returns `HARD_FAILURE` to the caller. Routing
hard failures to a human too is the natural next step and is called out in Cuts.

Explicitly scoped down, and said plainly rather than glossed over: the "operator
console" is a bare CLI, not a real-time co-browsing UI with a shared screen. The
mechanism it proves — pause without closing, expose the live session over CDP, capture
exactly what the human did, signal resume, keep going — is real; the polish of *how* a
human is shown the page is not what this submission spent its time on.

## Safety

Three independent, separately auditable guardrails (`agent/safety.py`), all enforced on
both discovery and replay:

- **Allowlist.** `Allowlist.check_url()` / `check_action()` reject any URL outside the
  capability's configured base origin, or any action type outside a fixed set
  (`click/type/select/navigate/wait_for/extract`) — a discovery run cannot wander off to
  an arbitrary URL or invoke an unrecognized action, and a `GuardrailViolation` is raised
  (and logged) rather than silently ignored.
- **Risk gating.** `requires_approval(step_risk_level, auto_approve_risky)` — a step
  marked `risky` (state-changing/hard-to-reverse: currently the sub-account creation
  submit) blocks on human approval by default; a caller can opt into
  `--auto-approve-risky` for unattended runs, and that choice is itself logged, not
  silent.
- **Redaction.** `redact_text()` strips SSN-shaped and long account/card-number-shaped
  patterns plus any input explicitly marked `sensitive: true` (member IDs, in this
  system) from every log line and DOM snapshot before it's written to disk —
  `EvidenceWriter` applies this to every `.log()` call and every `dom_snapshot()`, so
  evidence is safe to hand to a reviewer without hand-scrubbing it first.

These three were each verified directly (not just by code inspection): a disallowed
URL/action raises and is logged rather than executed; a redaction call on text
containing an SSN-shaped and a long-digit-run pattern returns `[REDACTED]` in place of
both. The evidence logs checked into `evidence/` are the real, already-redacted output
of real runs, not hand-sanitized after the fact.

## Cuts

Said plainly, in one place, rather than left implicit:

- **No automated test coverage for `discover.py` or the operator/escalation flow.**
  `tests/test_replay.py` covers the replay engine (the deterministic, no-LLM path)
  thoroughly and requires no API key. Discovery and the human-handoff path were verified
  by running them for real against the live mock app and inspecting the resulting
  evidence — not by a repeatable, mockable test harness. Building one (mocking the
  Anthropic client's tool-use responses, and a scripted "virtual operator" that drives
  CDP the way `mock_operator.py` does interactively) is the natural next piece of work.
- **`HARD_FAILURE` does not trigger escalation.** Only a `risky`-tagged step (replay) or
  the model's own `escalate` call (discovery) pauses for a human today. Routing hard
  failures — a locator that can't be resolved, a checkpoint that's never reached — into
  the same escalation path, instead of just returning a failed result to the caller, is
  a real gap and the most valuable next addition.
- **Recoverable-condition retry redoes the whole action sequence, not just the
  interrupted step.** Sound for this project's two capabilities (idempotent
  read/navigate flows re-entered from the home page each attempt) but not a general
  "resume from step N" mechanism — a genuinely mid-transaction interstitial would need
  per-step idempotency tracking this doesn't have.
- **No diffing of human escalation corrections back into the artifact.**
  `mock_operator.py` captures exactly which actions a human took while resuming a paused
  run (role, name, value, timestamp) into the run's evidence log, but nothing compares
  that against what the artifact expected or proposes an updated version. A scoped,
  honest version of this — for the risky-step case specifically, where the system knows
  exactly which single step the human stood in for — would diff the human's action
  against that step's target/value and surface a "drift suspected, review before
  re-recording" flag rather than silently auto-committing a new artifact version. Judged
  not worth building in the time available versus finishing the core deliverables to a
  higher bar; noted here as the clearest "if I had another day" item.
- **Multi-tenant override mechanism is implemented and unit-exercised, not proven
  against two independently different real apps.** Both shipped artifacts target the one
  mock app; the override/fingerprint machinery is real code with real tests, but there's
  no second target app in this submission to run it against end to end.
- **The operator console is a bare CLI**, not a real-time co-browsing UI — explicitly
  scoped down; see Escalation & handoff.
- **Single mock target app, two capabilities.** Enough to demonstrate the full loop
  (discover once, replay many times, handle all four result kinds, escalate and hand
  back) without spreading effort thin across more surfaces than the time available could
  do justice to.
