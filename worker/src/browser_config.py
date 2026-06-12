"""Headed Chromium on the slot's Xvfb display, driven by browser-use over CDP.

Headed (not headless) on purpose: the x11vnc live view mirrors this exact
display, and the SBI gateway is less hostile to a headed browser. EGRESS_PROXY
routes portal traffic through the IP that is allow-listed with .nic.in.
"""

from __future__ import annotations

from browser_use import BrowserProfile, BrowserSession

from config import EGRESS_PROXY


def build_browser_session(display: str) -> BrowserSession:
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1280,800",
        "--window-position=0,0",
    ]
    if EGRESS_PROXY:
        args.append(f"--proxy-server={EGRESS_PROXY}")

    profile = BrowserProfile(
        headless=False,
        chromium_sandbox=False,
        args=args,
        env={"DISPLAY": display},
        keep_alive=False,
    )
    return BrowserSession(browser_profile=profile)
