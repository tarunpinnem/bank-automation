"""App-version fingerprinting for drift detection (Section 3.7 / the
supplier-config-diff analogy: catch a changed target the way you'd diff a
config against what's actually live, rather than finding out when a replay
silently does the wrong thing).

Computed from the SET of (role, name) pairs visible on a capability's
landing page -- stable against incidental text/formatting changes, but
changes if controls are added, removed, relabeled, or reordered in a way
that would matter to locator resolution.
"""
from __future__ import annotations

import hashlib
from playwright.sync_api import Page

from .observe import observe


def compute_fingerprint(page: Page) -> str:
    obs = observe(page)
    signature = "\n".join(sorted(f"{el.role}:{el.name}" for el in obs.elements))
    return "sha256:" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]


def check_drift(page: Page, expected_fingerprint: str) -> tuple[bool, str]:
    """Returns (drifted: bool, current_fingerprint: str)."""
    current = compute_fingerprint(page)
    return (current != expected_fingerprint), current
