"""Action execution: resolves a target spec to a Playwright locator using a
primary strategy + ordered fallbacks, then performs the action.

This module is used by BOTH discovery (LLM picks role+name, no fallbacks
needed yet) and replay (full fallback chain from the saved artifact) -- one
execution path, so "what actually ran during discovery" and "what replay
does" can never silently diverge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from playwright.sync_api import Page, Locator, TimeoutError as PWTimeoutError


class LocatorNotFound(Exception):
    def __init__(self, target: dict[str, Any], tried: list[str]):
        self.target = target
        self.tried = tried
        super().__init__(f"No strategy matched target {target!r}; tried: {tried}")


def _locator_for_strategy(page: Page, strategy: dict[str, Any]) -> Locator | None:
    kind = strategy.get("strategy")
    try:
        if kind == "role":
            return page.get_by_role(strategy["role"], name=strategy["name"], exact=False)
        if kind == "text_near":
            # Legacy-safe: locate the TD whose full text content (label
            # cells are often wrapped in <b>, so we match the subtree, not
            # just direct text nodes) exactly equals the label, then look
            # for a control in the very next cell. Deliberately XPath +
            # exact-text rather than ":has-text" -- a nested-table legacy
            # layout (see mock_bank) makes ":has-text" match ancestor rows
            # too, which silently grabs the wrong cell. This is the kind of
            # gotcha the brief means by "no clean DOM."
            label = strategy["label"]
            xp = f"xpath=//td[normalize-space(.)='{label}']/following-sibling::td[1]"
            next_cell = page.locator(xp).first
            if next_cell.count() > 0:
                candidate = next_cell.locator("input, select, textarea, button, a")
                if candidate.count() > 0:
                    return candidate.first
            return page.get_by_text(label, exact=False)
        if kind == "css":
            return page.locator(strategy["selector"])
    except Exception:
        return None
    return None


def resolve(page: Page, target: dict[str, Any], timeout_ms: int = 3000) -> Locator:
    """Try primary strategy, then each fallback in order. Raises LocatorNotFound
    if none resolve to a visible element within timeout."""
    strategies = [target["primary"], *target.get("fallbacks", [])]
    tried: list[str] = []
    for strat in strategies:
        tried.append(str(strat))
        loc = _locator_for_strategy(page, strat)
        if loc is None:
            continue
        try:
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            return loc.first
        except PWTimeoutError:
            continue
    raise LocatorNotFound(target, tried)


@dataclass
class ActionResult:
    ok: bool
    detail: str = ""


def click(page: Page, target: dict[str, Any]) -> ActionResult:
    loc = resolve(page, target)
    loc.click()
    return ActionResult(ok=True)


def type_text(page: Page, target: dict[str, Any], value: str) -> ActionResult:
    loc = resolve(page, target)
    loc.fill(value)
    return ActionResult(ok=True)


def select_option(page: Page, target: dict[str, Any], value: str) -> ActionResult:
    loc = resolve(page, target)
    loc.select_option(value)
    return ActionResult(ok=True)


def navigate(page: Page, url: str) -> ActionResult:
    page.goto(url)
    return ActionResult(ok=True)


def extract_text_near(page: Page, label: str) -> str | None:
    """Legacy-safe extraction: find the TD whose full text content exactly
    matches the label, read the very next TD. See the note in
    _locator_for_strategy about why ":has-text" is the wrong tool on a
    nested-table layout."""
    xp = f"xpath=//td[normalize-space(.)='{label}']/following-sibling::td[1]"
    cell = page.locator(xp).first
    if cell.count() == 0:
        return None
    return cell.inner_text().strip()


def best_effort_css(loc: Locator) -> str | None:
    """Used only at RECORD time (never at replay time) to derive a CSS
    fallback from whatever attributes the resolved element actually has --
    a name/id if present, else None (the role/text_near strategies still
    cover it). This is how a concrete fallback chain gets built without
    guessing blindly."""
    try:
        return loc.evaluate(
            """
            (el) => {
                const tag = el.tagName.toLowerCase();
                if (el.id) return `#${el.id}`;
                if (el.getAttribute('name')) return `${tag}[name="${el.getAttribute('name')}"]`;
                return null;
            }
            """
        )
    except Exception:
        return None


def wait_for_condition(page: Page, condition: dict[str, Any], timeout_ms: int = 5000) -> bool:
    """condition = {"any_of": [ {role,name_contains} | {text_contains} , ... ]}
    Returns True as soon as any branch matches; False on timeout (caller decides
    whether that's a hard failure)."""
    import time as _time

    deadline = _time.time() + timeout_ms / 1000
    branches = condition.get("any_of", [condition])
    while _time.time() < deadline:
        for branch in branches:
            if "text_contains" in branch:
                if branch["text_contains"] in page.content():
                    return True
            elif "role" in branch:
                try:
                    loc = page.get_by_role(branch["role"], name=branch.get("name_contains", ""), exact=False)
                    if loc.count() > 0:
                        return True
                except Exception:
                    pass
        page.wait_for_timeout(150)
    return False
