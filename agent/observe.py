"""Turn live page state into a compact textual observation for the LLM.

Biased toward the accessibility tree (role + accessible name) rather than
raw HTML or screenshot coordinates -- this is the representation that
still works when the surface has no clean DOM (the brief's explicit bias),
and it's also what the artifact recorder uses to build reviewable,
human-readable locators.
"""
from __future__ import annotations

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

    if role == "text" and name:
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


def observe(page: Page) -> Observation:
    snapshot = page.accessibility.snapshot() or {}
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
