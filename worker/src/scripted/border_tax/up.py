# worker/src/scripted/border_tax/up.py
"""Uttar Pradesh border-tax scripted runner (app source + UPI).

Ten phases on the parivahan CheckPost V4 portal. Each phase advances the
lifecycle via the reporter and records steps. The two human/AI seams are:
  - phase 3: pending-transaction recovery (AI captcha, gated by AUTO_CLEAR_PENDING)
  - phase 6: the disclaimer page captcha, solved by the USER (3 attempts, hard cancel)
and the long wait is phase 9-10 (QR -> payment -> receipt).

Money-safety: nothing before phase 7 can move money, so any abort up to and
including the captcha is terminal=cancelled. From the gateway onward a failure is
terminal=failed/partial (reconcile), never a silent success — see payment_wait.

SELECTORS: the ids below are taken from the live UP template as seen in prod and
marked where they must be re-confirmed against the current CheckPost build. They
are intentionally centralized at the top so a portal change is a one-file edit.
"""

from __future__ import annotations

import asyncio

from ..log import StepLogger
from ..params import BorderTaxParams
from ..types import RunOutcome, StepLog, StepStatus, ScriptedAbort
from ..steps import (
    _cdp_eval, _current_url, click, click_by_text, dismiss_popup, fill,
    get_select_value, navigate, read_popup_text, select_by_text,
    select_by_value, sleep_seconds, wait_for_selector,
)
from ..captcha_user import solve_user_captcha
from ..payment_wait import wait_for_payment_and_capture
from .. import pending_clear
from lifecycle.status import Status

# ─── portal entry ──────────────────────────────────────────────────────
HOME_URL = "https://services.parivahan.gov.in/checkpostv4/#/"

# ─── selectors (TODO: confirm against current CheckPost build) ─────────
SEL_STATE_SELECT = "select#floatingState"            # phase 1
SEL_SERVICE_SELECT = "select#floatingService"        # phase 2
SEL_VEHICLE_INPUT = "input#floatingvehicle"          # phase 3
SEL_GET_DETAILS = "Get Details"                       # phase 3 (button text)
SEL_DISTRICT = "select#floatingDistrict"             # phase 3
SEL_CHECKPOST = "select#floatingCheckpost"           # phase 3
SEL_PERMIT_TYPE = "select#floatingPrmit"             # phase 4 (note portal typo)
SEL_SERVICE_TYPE = "select#floatingServiceType"      # phase 4
SEL_TAX_MODE = "select#floatingTaxMode"              # phase 5
SEL_TAX_FROM = "input#floatingTaxFrom"               # phase 5
SEL_TAX_UPTO = "input#floatingTaxUpto"               # phase 5
SEL_CALCULATE = "Calculate"                           # phase 5 (button text)
SEL_NEXT = "Next"                                     # phase 5 (button text)
SEL_DISCLAIMER_CHECKBOX = "input#agreeCheck"          # phase 6
SEL_CAPTCHA_CANVAS = "canvas"                          # phase 6 (TODO confirm)
SEL_CAPTCHA_INPUT = "input#inputcap"                  # phase 6
SEL_CAPTCHA_REFRESH = "#refreshCaptcha"               # phase 6 (TODO confirm)
SEL_PAY_ONLINE = "Pay Online"                          # phase 6 (button text)
SEL_QR_IMG = "img#qrcodeImg"                           # phase 9 (TODO confirm)


async def _captcha_rejected(session) -> bool:
    txt = await read_popup_text(session)
    return bool(txt and "captcha" in txt.lower() and (
        "invalid" in txt.lower() or "mismatch" in txt.lower()
    ))


async def _has_validity_block_popup(session) -> str | None:
    """Detect the PUCC/insurance/fitness validity popup (image 1 family)."""
    txt = await read_popup_text(session)
    if not txt:
        return None
    low = txt.lower()
    if any(k in low for k in ["pucc", "puc ", "pollution", "insurance",
                              "fitness", "permit has expired", "validity"]):
        return txt.strip()
    return None


async def run(session, params: BorderTaxParams, *, reporter, r, log: StepLogger,
              job_id: str) -> RunOutcome:
    rid, did = params.requestId, params.driverId
    veh = params.vehicleNumber

    # ── Phase 1: open portal, select state = Uttar Pradesh ──
    await navigate(session, HOME_URL, log=log, name="p1.open_home")
    await wait_for_selector(session, SEL_STATE_SELECT, log=log,
                            name="p1.wait_state", timeout=40)
    await select_by_text(session, SEL_STATE_SELECT, "Uttar Pradesh", log=log,
                         name="p1.select_state")
    await sleep_seconds(1.0, log=log, name="p1.settle")

    # ── Phase 2: select service = Border Tax Payment ──
    await wait_for_selector(session, SEL_SERVICE_SELECT, log=log,
                            name="p2.wait_service", timeout=20)
    await select_by_text(session, SEL_SERVICE_SELECT, "Border Tax", log=log,
                         name="p2.select_service")
    await sleep_seconds(1.5, log=log, name="p2.settle")

    # ── Phase 3: owner info — vehicle, Get Details, pending-tx, validity, district ──
    await wait_for_selector(session, SEL_VEHICLE_INPUT, log=log,
                            name="p3.wait_vehicle", timeout=20)
    await fill(session, SEL_VEHICLE_INPUT, veh, log=log, name="p3.fill_vehicle")
    await click_by_text(session, SEL_GET_DETAILS, log=log, name="p3.get_details",
                        tag="button", timeout=10)
    await sleep_seconds(2.5, log=log, name="p3.details_settle")

    # Validity block (PUCC/insurance/fitness) -> cancel cheaply, no money moved.
    block = await _has_validity_block_popup(session)
    if block:
        log.record(StepLog(log.next_index(), "p3.validity_block",
                           StepStatus.HANDED_OFF, error=block))
        raise ScriptedAbort(block, terminal="cancelled")

    # Pending transaction popup -> AI clear (if enabled), else cancel.
    popup = await read_popup_text(session)
    if popup and "pending" in popup.lower():
        if not pending_clear.auto_clear_enabled():
            raise ScriptedAbort(
                "pending transaction present and auto-clear disabled",
                terminal="cancelled",
            )
        await reporter.set_status(Status.PENDING_TRANSACTION)
        outcome, hint = await pending_clear.clear_pending_transaction(
            session, vehicle_number=veh, request_id=rid, driver_id=did,
            job_id=job_id, log=log,
        )
        if outcome != "cleared":
            raise ScriptedAbort(
                f"pending transaction not able to clear ({outcome}: {hint})",
                terminal="cancelled",
            )
        # Cleared -> restart the owner-info phase from the top.
        await reporter.set_status(Status.AI_AGENT_STARTED)
        await navigate(session, HOME_URL, log=log, name="p3.restart_home")
        await wait_for_selector(session, SEL_STATE_SELECT, log=log,
                                name="p3.restart_wait_state", timeout=40)
        await select_by_text(session, SEL_STATE_SELECT, "Uttar Pradesh", log=log,
                             name="p3.restart_state")
        await sleep_seconds(1.0, log=log, name="p3.restart_settle")
        await select_by_text(session, SEL_SERVICE_SELECT, "Border Tax", log=log,
                             name="p3.restart_service")
        await sleep_seconds(1.5, log=log, name="p3.restart_service_settle")
        await fill(session, SEL_VEHICLE_INPUT, veh, log=log, name="p3.restart_vehicle")
        await click_by_text(session, SEL_GET_DETAILS, log=log,
                            name="p3.restart_get_details", tag="button", timeout=10)
        await sleep_seconds(2.5, log=log, name="p3.restart_details_settle")

    # District + checkpoint.
    await wait_for_selector(session, SEL_DISTRICT, log=log, name="p3.wait_district",
                            timeout=20)
    await select_by_text(session, SEL_DISTRICT, params.entryDistrict, log=log,
                         name="p3.select_district")
    await sleep_seconds(1.0, log=log, name="p3.district_settle")
    if params.entryCheckpoint:
        try:
            await select_by_text(session, SEL_CHECKPOST, params.entryCheckpoint,
                                 log=log, name="p3.select_checkpoint")
        except ScriptedAbort:
            log.record(StepLog(log.next_index(), "p3.checkpoint_optional",
                               StepStatus.RETRIED,
                               error="named checkpoint not found; leaving default"))

    # ── Phase 4: vehicle info — permit type, service type ──
    if params.permitType:
        try:
            await select_by_text(session, SEL_PERMIT_TYPE, params.permitType,
                                 log=log, name="p4.permit_type")
        except ScriptedAbort:
            await select_by_text(session, SEL_PERMIT_TYPE, params.permitTypeFallback,
                                 log=log, name="p4.permit_type_fallback")
    else:
        cur = await get_select_value(session, SEL_PERMIT_TYPE)
        if not cur:
            await select_by_text(session, SEL_PERMIT_TYPE, params.permitTypeFallback,
                                 log=log, name="p4.permit_type_default")
    await sleep_seconds(0.8, log=log, name="p4.permit_settle")
    try:
        await select_by_text(session, SEL_SERVICE_TYPE, params.serviceType,
                             log=log, name="p4.service_type")
    except ScriptedAbort:
        log.record(StepLog(log.next_index(), "p4.service_type_optional",
                           StepStatus.RETRIED, error="service type default kept"))

    # ── Phase 5: tax info — mode, dates, Calculate, Next ──
    await select_by_value(session, SEL_TAX_MODE, params.taxMode, log=log,
                          name="p5.tax_mode")
    await sleep_seconds(0.8, log=log, name="p5.mode_settle")
    await fill(session, SEL_TAX_FROM, params.taxFrom, log=log, name="p5.tax_from")
    if params.taxUpto:
        await fill(session, SEL_TAX_UPTO, params.taxUpto, log=log, name="p5.tax_upto")
    await click_by_text(session, SEL_CALCULATE, log=log, name="p5.calculate",
                        tag="button", timeout=10)
    await sleep_seconds(1.5, log=log, name="p5.calc_settle")
    await click_by_text(session, SEL_NEXT, log=log, name="p5.next", tag="button",
                        timeout=10)
    await sleep_seconds(2.0, log=log, name="p5.next_settle")

    # ── Phase 6: disclaimer — USER captcha + agree + Pay Online ──
    await wait_for_selector(session, SEL_CAPTCHA_INPUT, log=log,
                            name="p6.wait_captcha", timeout=30)
    try:
        await click(session, SEL_DISCLAIMER_CHECKBOX, log=log,
                    name="p6.agree_checkbox", timeout=5, retries=0)
    except ScriptedAbort:
        pass  # some builds have no explicit checkbox

    await reporter.set_status(Status.CAPTCHA_SOLVING)

    async def _submit_after_captcha() -> bool:
        await click_by_text(session, SEL_PAY_ONLINE, log=log,
                            name="p6.pay_online", tag="button", timeout=10)
        await asyncio.sleep(2.0)
        # Some builds raise a 'Yes/Confirm' modal before redirecting.
        try:
            await click_by_text(session, "Yes", log=log, name="p6.confirm_yes",
                                tag="button", timeout=4)
        except Exception:
            pass
        # Advanced if we left the disclaimer (captcha input gone) or hit a gateway.
        url = await _current_url(session)
        gone = await _cdp_eval(
            session, f"return !document.querySelector('{SEL_CAPTCHA_INPUT}')",
        )
        return bool(gone) or any(g in url for g in ["sbi", "Pay", "gateway"])

    await solve_user_captcha(
        session,
        canvas_selector=SEL_CAPTCHA_CANVAS,
        input_selector=SEL_CAPTCHA_INPUT,
        refresh_selector=SEL_CAPTCHA_REFRESH,
        submit_action=_submit_after_captcha,
        is_rejected=_captcha_rejected,
        request_id=rid, driver_id=did, job_id=job_id, r=r, log=log,
    )

    # Captcha accepted -> we're heading to the gateway. Money may now move.
    await reporter.set_status(Status.SETTING_UP_PAYMENT_REQUEST)

    # ── Phase 7-8: gateway (SBI ePay) -> UPI ──
    # The portal hands off to SBI ePay; selecting UPI + confirm renders the QR.
    # These are best-effort: if the gateway auto-lands on UPI, the clicks no-op.
    try:
        await click_by_text(session, "UPI", log=log, name="p8.select_upi",
                            tag="button", timeout=15)
        await sleep_seconds(1.0, log=log, name="p8.upi_settle")
        await click_by_text(session, "Confirm", log=log, name="p8.confirm",
                            tag="button", timeout=8)
    except Exception:
        log.record(StepLog(log.next_index(), "p8.gateway_optional",
                           StepStatus.RETRIED,
                           error="UPI/Confirm not found; assuming QR auto-rendered"))
    await sleep_seconds(2.0, log=log, name="p8.gateway_settle")

    # ── Phase 9-10: QR -> payment -> receipt ──
    return await wait_for_payment_and_capture(
        session, request_id=rid, driver_id=did, vehicle_number=veh,
        job_id=job_id, r=r, log=log, reporter=reporter,
        qr_selector=SEL_QR_IMG,
    )
