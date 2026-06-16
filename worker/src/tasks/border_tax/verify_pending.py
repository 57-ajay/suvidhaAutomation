"""Deferred verification of a parked payment.

When the payment flow hits "Transaction confirmation pending at the bank side"
it can't tell whether money moved, so it parks the request at
verifyingPendingPayment and exits. The backend later POSTs /api/verify-pending,
which enqueues a `verifyPendingPayment` job; this module is that job.

One shot, 5 minutes total: drive the Check Transaction Status page (the same
state-agnostic page pending_clear uses), solve its captcha with AI ONLY (finite
budget, no human seam), click the row's bank icon, then:
  - receipt on screen        -> capture_and_upload_receipt -> completed
  - blocking popup / "try     -> failed (reconcile)
    after" / negative text
  - nothing decisive in time  -> failed (reconcile)

Everything here is POST-payment, so every non-receipt exit is terminal=failed;
money is never marked moved without a captured receipt.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from functools import partial

from config import MAX_AI_CAPTCHA_ATTEMPTS  # noqa: F401  (used via solve_captcha)
from engine.pipeline import Phase
from engine.steps import (
    cdp_eval,
    fill,
    navigate,
    page_text,
    sleep_seconds,
    wait_for_selector,
)
from engine.types import RunContext, RunOutcome, ScriptedAbort, StepLog, StepStatus
from lifecycle.status import Status

from . import pending_clear as pc
from .captcha_user import solve_captcha
from .payment_wait import (
    DEFAULT_NEGATIVE_PATTERNS,
    PaymentCaptureConfig,
    _find_with_snippet,
    _receipt_ready,
    capture_and_upload_receipt,
)

VERIFY_MAX_SECS = 300  # 5 minutes, process start to finish
BANK_RESULT_POLL_SECS = 45.0  # after the bank-icon click, wait for the receipt
BANK_RESULT_TICK_SECS = 1.0

# Any visible swal popup after the bank-icon click means "can't clear it"
# (incl. "Please try after X Minutes"). Per spec that's a hard fail here.
_VERIFY_POPUP_JS = """
(function() {
  var popups = document.querySelectorAll('.swal2-popup');
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      return {popup: true, text: (popups[i].textContent || '').trim().substring(0, 200)};
    }
  }
  return {popup: false};
})()
"""


@dataclass
class VerifyPendingParams:
    requestId: str
    driverId: str
    vehicleNumber: str
    state: str

    @classmethod
    def from_job(cls, raw: dict) -> "VerifyPendingParams":
        missing = [
            k
            for k in ("requestId", "driverId", "vehicleNumber", "state")
            if not raw.get(k)
        ]
        if missing:
            raise ScriptedAbort(
                f"verify-pending job missing: {', '.join(missing)}",
                terminal="failed",
            )
        return cls(
            requestId=raw["requestId"],
            driverId=raw["driverId"],
            vehicleNumber=raw["vehicleNumber"],
            state=raw["state"],
        )


async def _open_check_page(ctx: RunContext) -> bool:
    """Home -> Check Transaction Status -> vehicle filled. Mirrors pending_clear
    (router rewrites deep links, so we click the routerLink)."""
    await pc._dismiss_all(ctx, "verify.dismiss_blocker")
    await navigate(
        ctx.session,
        pc.URL_PORTAL_HOME,
        log=ctx.log,
        name="verify.home",
        timeout=pc.PAGE_LOAD_TIMEOUT_SECS + 25,
    )
    await sleep_seconds(1.5, log=ctx.log, name="verify.home_settle")
    try:
        await wait_for_selector(
            ctx.session,
            pc.SEL_PENDING_TX_LINK,
            log=ctx.log,
            name="verify.wait_link",
            timeout=pc.LINK_WAIT_TIMEOUT_SECS,
        )
        await cdp_eval(
            ctx.session,
            "(function(s){var e=document.querySelector(s);"
            "if(e){e.click();return true;}return false;})("
            + json.dumps(pc.SEL_PENDING_TX_LINK)
            + ")",
        )
        await wait_for_selector(
            ctx.session,
            pc.SEL_PENDING_VEHICLE_INPUT,
            log=ctx.log,
            name="verify.wait_page",
            timeout=pc.PAGE_LOAD_TIMEOUT_SECS,
        )
    except ScriptedAbort:
        return False
    await fill(
        ctx.session,
        pc.SEL_PENDING_VEHICLE_INPUT,
        ctx.params.vehicleNumber,
        log=ctx.log,
        name="verify.fill_vehicle",
    )
    return True


async def _solve_check_captcha_ai_only(ctx: RunContext) -> None:
    async def _submit() -> bool:
        await cdp_eval(
            ctx.session,
            "(function(s){var e=document.querySelector(s);"
            "if(e){e.click();return true;}return false;})("
            + json.dumps(pc.SEL_PENDING_GO_BTN)
            + ")",
        )
        deadline = time.monotonic() + pc.GO_SUBMIT_POLL_SECS
        while time.monotonic() < deadline:
            res = await cdp_eval(ctx.session, pc._GO_SUBMIT_STATE_JS)
            state = (res or {}).get("state", "pending")
            if state == "results":
                return True
            if state == "captcha_mismatch":
                return False
            await asyncio.sleep(0.5)
        return False

    async def _rejected() -> bool:
        res = await cdp_eval(ctx.session, pc._GO_SUBMIT_STATE_JS)
        return (res or {}).get("state") == "captcha_mismatch"

    await solve_captcha(
        ctx,
        input_selector=pc.SEL_PENDING_CAPTCHA_INPUT,
        refresh_selector=pc.SEL_PENDING_CAPTCHA_REFRESH,
        submit_action=_submit,
        is_rejected=_rejected,
        canvas_scope=pc.SEL_PENDING_CAPTCHA_CANVAS,
        mode="ai",
        human_fallback=False,  # detached sweep: no user
        terminal="failed",  # post-payment
        abort_message="couldn't read the status-check captcha within the AI "
        "budget — payment left unverified, reconcile manually",
    )


async def verify_pending_payment(
    ctx: RunContext, *, config: PaymentCaptureConfig
) -> RunOutcome:
    p = ctx.params

    if not await _open_check_page(ctx):
        return RunOutcome(
            status="failed",
            summary="couldn't open the Check Transaction Status page to verify "
            f"{p.vehicleNumber} — reconcile manually",
            error="check-status page did not load",
            payment_likely=True,
            run_log=ctx.log.dump(),
        )

    await _solve_check_captcha_ai_only(ctx)  # raises failed on AI exhaustion

    res = await cdp_eval(ctx.session, pc._CLICK_BANK_ICON_JS)
    if not res or not res.get("ok"):
        return RunOutcome(
            status="failed",
            summary=f"couldn't open the payment status for {p.vehicleNumber} — "
            "reconcile manually",
            error=f"bank icon: {(res or {}).get('reason', 'click failed')}",
            payment_likely=True,
            run_log=ctx.log.dump(),
        )

    deadline = time.monotonic() + BANK_RESULT_POLL_SECS
    while time.monotonic() < deadline:
        text = await page_text(ctx.session)
        if _receipt_ready(config.receipt_markers, text):
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="verify.receipt",
                    status=StepStatus.OK,
                    value="markers present",
                )
            )
            return await capture_and_upload_receipt(ctx, config=config)

        neg = _find_with_snippet(DEFAULT_NEGATIVE_PATTERNS, text)
        if neg:
            return RunOutcome(
                status="failed",
                summary=f"the bank reported no successful payment for "
                f"{p.vehicleNumber} — reconcile manually",
                error=f'portal showed: "{neg[1]}"',
                payment_likely=False,  # explicit failure text: money did NOT move
                run_log=ctx.log.dump(),
            )

        popup = await cdp_eval(ctx.session, _VERIFY_POPUP_JS)
        if popup and popup.get("popup"):
            return RunOutcome(
                status="failed",
                summary=f"couldn't confirm the payment for {p.vehicleNumber} — "
                "reconcile manually",
                error=f'portal popup: "{popup.get("text", "")}"',
                payment_likely=True,
                run_log=ctx.log.dump(),
            )
        await asyncio.sleep(BANK_RESULT_TICK_SECS)

    return RunOutcome(
        status="failed",
        summary=f"verification timed out for {p.vehicleNumber} without a "
        "receipt — reconcile manually",
        error="no receipt or decisive signal within the verify window",
        payment_likely=True,
        run_log=ctx.log.dump(),
    )


def make_verify_phases(config: PaymentCaptureConfig) -> list[Phase]:
    return [
        Phase(
            "verify_pending",
            partial(verify_pending_payment, config=config),
            max_secs=VERIFY_MAX_SECS,
        )
    ]
