# worker/src/scripted/pending_clear.py
"""Auto-clear a 'pending transaction' blocker on the parivahan checkpostv4 portal.

When 'Get Details' is clicked for a vehicle that already has an in-flight tax
transaction, the portal shows a SweetAlert2 popup and the Owner Information form
never finishes loading. This module walks the documented recovery:

  1. Dismiss the popup.
  2. Navigate home and open Border Tax Payment -> Check Pending Transaction.
  3. Fill the vehicle number.
  4. Solve the page captcha with AI (ai_read_captcha) — background recovery.
  5. Click Go. Poll for the results table (accepted) or 'Captcha mismatch'
     (rejected -> refresh + retry).
  6. Click the bank icon on the first row. Poll for:
       - 'Please try after X Minutes' -> ("still_on_hold", "<hint>")
       - /bank/DoubleVerification or 'transaction is fail' -> ("cleared", "")

Returns (status, hint) where status in {cleared, still_on_hold, failed}.

Gated by AUTO_CLEAR_PENDING. When off, the caller bails immediately with
terminal=cancelled and error 'pending transaction not able to clear'.

NOTE: selectors marked TODO need confirmation against the live portal version
(CheckPost V4.7.x). They hang off the checkpostv4 SPA shell, not the per-state
template, so they're state-agnostic.
"""

from __future__ import annotations

import asyncio
import time

from .log import StepLogger
from .steps import (
    _cdp_eval, _current_url, click, click_by_text, dismiss_popup, fill,
    read_popup_text, sleep_seconds,
)
from .captcha_ai import ai_read_captcha
from .types import StepLog, StepStatus
from config import AUTO_CLEAR_PENDING


# Selectors (TODO: confirm live).
SEL_PENDING_MENU = "a[href*='ChecklTransactionStatus'], a:contains('Check Pending Transaction')"
SEL_PENDING_VEHICLE = "input#floatingvehicle"  # TODO confirm on the check page
SEL_PENDING_CAPTCHA_CANVAS = "canvas"           # TODO confirm
SEL_PENDING_CAPTCHA_INPUT = "input#inputcap"    # TODO confirm
SEL_PENDING_GO = "Go"
SEL_FIRST_ROW_BANK_ICON = "table tbody tr:first-child .bank-icon, table tbody tr:first-child a"  # TODO


def auto_clear_enabled() -> bool:
    return AUTO_CLEAR_PENDING


async def clear_pending_transaction(
    session, *, vehicle_number: str, request_id: str, driver_id: str,
    job_id: str, log: StepLogger,
) -> tuple[str, str]:
    cost_total = 0.0

    await dismiss_popup(session, log=log, name="pending.dismiss_initial")

    # Home -> Check Pending Transaction (router rewrites direct nav, so click).
    from .steps import navigate
    await navigate(session, "https://services.parivahan.gov.in/checkpostv4/#/",
                   log=log, name="pending.home")
    await sleep_seconds(1.5, log=log, name="pending.home_settle")

    try:
        await click_by_text(session, "Check Pending Transaction", log=log,
                            name="pending.open_check", tag="a", timeout=10)
    except Exception:
        log.record(StepLog(log.next_index(), "pending.open_check",
                           StepStatus.FAILED, error="menu link not found"))
        return ("failed", "could not open Check Pending Transaction")

    await sleep_seconds(1.0, log=log, name="pending.check_settle")
    await fill(session, SEL_PENDING_VEHICLE, vehicle_number, log=log,
               name="pending.fill_vehicle")

    # AI captcha + Go, with a couple of retries on mismatch.
    for attempt in range(1, 4):
        text, cost = await ai_read_captcha(session)
        cost_total += cost
        await fill(session, SEL_PENDING_CAPTCHA_INPUT, text, log=log,
                   name=f"pending.captcha_fill_{attempt}")
        await click_by_text(session, SEL_PENDING_GO, log=log,
                            name=f"pending.click_go_{attempt}", tag="button",
                            timeout=8)
        await sleep_seconds(2.0, log=log, name=f"pending.go_settle_{attempt}")

        popup = await read_popup_text(session)
        if popup and "captcha" in popup.lower() and "mismatch" in popup.lower():
            await dismiss_popup(session, log=log, name=f"pending.captcha_retry_{attempt}")
            continue
        break

    # Click the bank icon on the first result row.
    try:
        await click(session, SEL_FIRST_ROW_BANK_ICON, log=log,
                    name="pending.click_bank_icon", timeout=8)
    except Exception:
        return ("failed", "no pending row / bank icon")

    # Poll for outcome.
    started = time.monotonic()
    while time.monotonic() - started < 30:
        popup = await read_popup_text(session)
        if popup and "please try after" in popup.lower():
            return ("still_on_hold", popup.strip())
        url = await _current_url(session)
        if "DoubleVerification" in url:
            return ("cleared", "")
        body = await _cdp_eval(session, "return (document.body.innerText||'').toLowerCase()")
        if body and "transaction is fail" in body:
            return ("cleared", "")
        await asyncio.sleep(1.0)

    return ("failed", "clear outcome unknown after 30s")
