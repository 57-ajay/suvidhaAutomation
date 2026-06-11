# worker/src/browser_config.py
"""Build the browser-use BrowserSession for a job.

Runs headed (headless=False) on a per-slot X display so the run is visible over
noVNC for live intervention. chromium_sandbox=False is required in-container.
Optional --proxy-server routes portal traffic through the egress proxy that owns
the IP allow-listed with .nic.in.
"""

from __future__ import annotations

from config import EGRESS_PROXY


def make_browser(*, display: str):
    """Return a BrowserSession bound to the given X display (e.g. ':99')."""
    from browser_use import BrowserSession, BrowserProfile

    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1280,800",
        "--start-maximized",
    ]
    if EGRESS_PROXY:
        args.append(f"--proxy-server={EGRESS_PROXY}")

    profile = BrowserProfile(
        headless=False,
        chromium_sandbox=False,
        args=args,
        env={"DISPLAY": display},
    )
    return BrowserSession(browser_profile=profile)
