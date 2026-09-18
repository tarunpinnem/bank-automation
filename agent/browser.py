"""Browser/session management shared by discovery and replay.

Launches headed-capable Chromium with a fixed remote-debugging port so the
same live page can be reattached to later -- by a resumed automation run,
or by the mock operator surface during a human handoff (see escalation.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from playwright.sync_api import sync_playwright, Browser, Page, Playwright

CHROMIUM_PATH = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
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
    browser = pw.chromium.launch(
        executable_path=CHROMIUM_PATH,
        headless=headless,
        args=[f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=0.0.0.0"],
    )
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
