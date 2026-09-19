"""Turn live page state into a compact textual observation for the LLM.

Biased toward the accessibility tree (role + accessible name) rather than
raw HTML or screenshot coordinates -- this is the representation that
still works when the surface has no clean DOM (the brief's explicit bias),
and it's also what the artifact recorder uses to build reviewable,
human-readable locators.

Built on Page.locator("body").aria_snapshot() (Playwright's current, YAML-based
accessibility-tree API). The older `page.accessibility.snapshot()` this was
originally written against was removed from Playwright entirely (gone as of
the 1.6x series) -- any fresh `pip install playwright` today only has the
newer API, so `_parse_aria_snapshot` below turns that YAML text back into the
same {role, name, children} shape the rest of this module (and fingerprint.py)
already expects, keeping the traversal/name-recovery logic unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from playwright.sync_api import Page


# Interactive roles worth surfacing to the model. Keeps the observation
# short and focused instead of dumping the entire accessibility tree.
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "combobox", "checkbox", "radio",
    "listbox", "option", "heading", "text",
}


@dataclass
class ObservedElement:
    role: str
    name: str
    path: tuple[int, ...] = field(default_factory=tuple)  # child-index path from root
    inferred: bool = False  # name synthesized from a nearby label, not a real accessible name


@dataclass
class Observation:
    url: str
    title: str
    elements: list[ObservedElement]
    raw_text_excerpt: str

    def to_prompt_text(self) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}", "", "Visible elements (role, name):"]
        for el in self.elements:
            suffix = "  [name inferred from a nearby label -- target it by this text anyway]" if el.inferred else ""
            lines.append(f"  - {el.role}: {el.name!r}{suffix}")
        lines.append("")
        lines.append("Page text excerpt (for context only, do not target by raw text position):")
        lines.append(self.raw_text_excerpt[:1500])
        return "\n".join(lines)


# Roles where an empty accessible name is worth trying to recover -- form
# controls a legacy table layout typically labels with adjacent text rather
# than a proper <label>, rather than roles like "button" where an empty
# name usually just means "genuinely unlabeled, skip it."
NAME_RECOVERABLE_ROLES = {"textbox", "combobox", "checkbox", "radio", "listbox"}


def _walk(node: dict, path: tuple[int, ...], out: list[ObservedElement], state: dict) -> None:
    role = (node.get("role") or "").lower()
    name = (node.get("name") or "").strip()

    # aria_snapshot() folds plain text into the name of its nearest labeled
    # ancestor/sibling container (e.g. a table cell) instead of emitting a
    # separate "text" node the way the old accessibility tree did -- so any
    # named, non-interactive node is a candidate label source, not just an
    # explicit "text" role.
    if name and role not in INTERACTIVE_ROLES:
        state["last_text"] = name

    if role in INTERACTIVE_ROLES:
        display_name, inferred = name, False
        if not display_name and role in NAME_RECOVERABLE_ROLES:
            # Real-world legacy pattern we hit against the mock bank app:
            # accessibility.snapshot() reports these controls with name=""
            # because the "label" is just a plain <td> next to them, not a
            # <label for=...>. The nearest preceding "text" node in tree
            # order is, in practice, that label -- so use it rather than
            # silently dropping the control from what the model can see.
            display_name, inferred = state.get("last_text", ""), True
        if display_name:
            out.append(ObservedElement(role=role, name=display_name, path=path, inferred=inferred))

    for i, child in enumerate(node.get("children") or []):
        _walk(child, path + (i,), out, state)


# Matches one line of Playwright's aria_snapshot() YAML, e.g.:
#   - textbox "Member ID" [level=2]:
# Property lines like "- /url: /" are handled separately (see _parse_aria_snapshot).
_LINE_RE = re.compile(
    r'^(?P<indent>\s*)-\s+(?P<role>[A-Za-z][A-Za-z0-9_-]*)'
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r'(?:\s+\[[^\]]*\])?'
    r':?\s*$'
)


def _parse_aria_snapshot(text: str) -> dict:
    """Parse aria_snapshot()'s indented YAML into the {role, name, children}
    tree shape _walk() expects (the same shape the old accessibility.snapshot()
    dict returned)."""
    root: dict = {"role": "root", "name": "", "children": []}
    stack: list[tuple[int, dict]] = [(-1, root)]

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.lstrip(" ")
        indent = len(raw_line) - len(stripped)
        # Pseudo-property lines (e.g. "/url: /", "/checked: true") describe
        # an attribute of the previous node, not a new accessibility node --
        # skip them rather than mis-parsing them as a role.
        if stripped.startswith("- /"):
            continue
        m = _LINE_RE.match(raw_line)
        if not m:
            continue
        level = indent // 2
        name = (m.group("name") or "").replace('\\"', '"')
        node: dict = {"role": m.group("role"), "name": name, "children": []}
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack[-1][1]["children"].append(node)
        stack.append((level, node))

    return root


def observe(page: Page) -> Observation:
    snapshot_text = page.locator("body").aria_snapshot()
    snapshot = _parse_aria_snapshot(snapshot_text)
    elements: list[ObservedElement] = []
    _walk(snapshot, (), elements, state={})

    try:
        text_excerpt = page.inner_text("body")
    except Exception:
        text_excerpt = ""

    return Observation(
        url=page.url,
        title=page.title(),
        elements=elements,
        raw_text_excerpt=text_excerpt,
    )
