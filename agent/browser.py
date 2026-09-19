"""Browser/session management shared by discovery and replay.

Launches headed-capable Chromium with a fixed remote-debugging port so the
same live page can be reattached to later -- by a resumed automation run,
or by the mock operator surface during a human handoff (see escalation.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from playwright.sync_api import sync_playwright, Browser, Page, Playwright

# Optional override for environments that pin a specific Chromium binary
# (e.g. a sandboxed CI image with a pre-installed, non-default browser path).
# Unset by default -- Playwright then resolves its own installed browser the
# normal way (respects its own PLAYWRIGHT_BROWSERS_PATH if set, otherwise the
# standard per-OS cache dir from `playwright install chromium`). Hardcoding a
# path here would only work on the one machine it was recorded on.
CHROMIUM_PATH = os.environ.get("AGENT_CHROMIUM_PATH")
CDP_PORT = int(os.environ.get("AGENT_CDP_PORT", "9333"))


@dataclass
class Session:
    playwright: Playwright
    browser: Browser
    page: Page

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self.playwright.stop()


def launch(headless: bool = True) -> Session:
    """Start a fresh browser+page with CDP enabled on a fixed port."""
    pw = sync_playwright().start()
    launch_kwargs: dict = {
        "headless": headless,
        "args": [f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=0.0.0.0"],
    }
    if CHROMIUM_PATH:
        launch_kwargs["executable_path"] = CHROMIUM_PATH
    browser = pw.chromium.launch(**launch_kwargs)
    page = browser.new_page()
    return Session(playwright=pw, browser=browser, page=page)


def attach_over_cdp() -> tuple[Playwright, Browser, Page]:
    """Reattach to the SAME live browser session via CDP.

    Used by (a) the automation process resuming after a human handoff, and
    (b) the mock operator surface taking control during one. Both connect
    to the identical running Chromium process/page -- not a fresh session.
    """
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    return pw, browser, page
