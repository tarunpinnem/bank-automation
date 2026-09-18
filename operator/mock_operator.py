#!/usr/bin/env python3
"""Minimal mock operator surface (Section 3.6 scope note: a bare/mock
operator UI, not a real-time co-browsing console).

Attaches to the SAME live browser session a paused replay/discovery run is
using (via CDP, see agent/browser.py:attach_over_cdp), lets a human act on
it directly with a few simple commands, then signals resume.

Usage:
    python operator/mock_operator.py <evidence_dir>

Commands once attached:
    show                        - print current URL + visible elements
    click <role> <name>         - click an element
    type <role> <name> <value>  - fill a textbox
    select <role> <name> <value>- choose a select option
    resume                      - signal the paused run to continue
    cancel                      - signal the paused run to give up
    quit                        - exit without signaling (run stays paused)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.browser import attach_over_cdp
from agent.observe import observe
from agent import act
from agent.escalation import PENDING_FILENAME, RESUME_FILENAME, CANCEL_FILENAME


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    evidence_dir = Path(sys.argv[1])
    pending_path = evidence_dir / PENDING_FILENAME
    if not pending_path.exists():
        print(f"No pending intervention found at {pending_path}. Is a run currently paused?")
        sys.exit(1)

    pending = json.loads(pending_path.read_text())
    print("=== Intervention request ===")
    print(json.dumps(pending, indent=2))
    print()

    pw, browser, page = attach_over_cdp()
    print(f"Attached to live session at {page.url}\n")

    actions_taken: list[dict] = []

    def show():
        obs = observe(page)
        print(obs.to_prompt_text())

    show()
    print("\nType 'help' for commands.\n")

    try:
        while True:
            try:
                line = input("operator> ").strip()
            except EOFError:
                break
            if not line:
                continue
            parts = line.split(maxsplit=3)
            cmd = parts[0].lower()

            if cmd == "help":
                print(__doc__)
            elif cmd == "show":
                show()
            elif cmd == "click" and len(parts) >= 3:
                role, name = parts[1], " ".join(parts[2:])
                act.resolve(page, {"primary": {"strategy": "role", "role": role, "name": name}}).click()
                actions_taken.append({"action": "click", "role": role, "name": name, "ts": time.time()})
                print("clicked.")
            elif cmd == "type" and len(parts) >= 4:
                role, name, value = parts[1], parts[2], parts[3]
                act.resolve(page, {"primary": {"strategy": "role", "role": role, "name": name}}).fill(value)
                actions_taken.append({"action": "type", "role": role, "name": name, "value": value, "ts": time.time()})
                print("typed.")
            elif cmd == "select" and len(parts) >= 4:
                role, name, value = parts[1], parts[2], parts[3]
                act.resolve(page, {"primary": {"strategy": "role", "role": role, "name": name}}).select_option(value)
                actions_taken.append({"action": "select", "role": role, "name": name, "value": value, "ts": time.time()})
                print("selected.")
            elif cmd == "resume":
                (evidence_dir / RESUME_FILENAME).write_text(json.dumps({
                    "operator": "mock_operator_cli", "actions": actions_taken, "ts": time.time(),
                }, indent=2))
                print("Resume signaled. The paused run will continue.")
                break
            elif cmd == "cancel":
                (evidence_dir / CANCEL_FILENAME).write_text(json.dumps({
                    "operator": "mock_operator_cli", "actions": actions_taken, "ts": time.time(),
                }, indent=2))
                print("Cancel signaled.")
                break
            elif cmd == "quit":
                print("Exiting without signaling; run remains paused.")
                break
            else:
                print("Unrecognized command. Type 'help'.")
    finally:
        pw.stop()  # detaches only; does not close the shared browser


if __name__ == "__main__":
    main()
