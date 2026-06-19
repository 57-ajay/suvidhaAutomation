# worker/src/tasks/border_tax/states/mp.py
"""Madhya Pradesh border-tax runner (app source + UPI).

MP rides the SAME parivahan CheckPost wizard as UP and — unlike HR — has NO
intermediate page (no eGRAS/IFMS detour). The parivahan portion is identical
to UP through the Disclaimer/Pay-Online step. The one real divergence is the
SBIePay variant: MP's gateway hands off to the FULL SBIePay at
epay.sbi.bank.in (a left-sidebar of payment categories), NOT the "SBIePay
Lite" welcome page UP/HR use. So this module reuses UP's proven, state-
agnostic phases verbatim (imported from .up) and defines ONLY MP's deltas:

  open_portal        state dropdown value "MP"                        [override]
  select_service     "VEHICLE TAX COLLECTION (OTHER STATE)" + Go        [reused]
  owner_info         vehicle + Get Details -> district -> checkpost      [reused]
                     (MP's checkpost names don't match the district, so the
                     desired value falls through to the first option)
  vehicle_info       validity checks; Permit Type / Service Type set-if-
                     empty. NO Distance field (like PB); Vehicle Category is
                     RC-pre-filled and left untouched (UP's vehicle_info
                     doesn't set it).                                   [reused]
  tax_info           Tax Mode DAYS-only; Tax From/Upto are plain type="date"
                     (YYYY-MM-DD, NOT datetime-local — UP's tax_info fills
                     plain, so it fits); Calculate; Next                [reused]
  disclaimer_captcha USER-solved captcha + confirm + "Receipt valid" popup +
                     Pay Online + Yes -> vahan eTransPgi gateway         [reused]
  payment_gateway    dropOperator = "SBIe" (Direct Payment(SBIePay), MP's
                     lone aggregator) + terms + input#sendSubmit       [override]
  sbiepay_upi        epay.sbi.bank.in: sidebar UPI tab (li#activeUPI a) ->
                     UPI QR radio (input#upiQR1) -> Pay Now
                     (button#upiButton) -> QR page. NO confirm page.   [override]
  payment            QR <img> (no id — captured via the base64-src selector)
                     -> payment_wait with MP markers, ~5-min QR window  [override]

Status FSM is identical to UP (no new statuses). Money-safety mirrors UP:
every stop through the captcha is terminal=cancelled; from the gateway onward
it is failed (manualReview) — never a silent success.

Date NOTE: MP's #floatingTaxfrom / #uptpDate are plain <input type="date">
(accepted value "YYYY-MM-DD"), like UP — so UP's tax_info, which fills the
bare ISO date, is correct here. (HR/PB use datetime-local and append T00:00;
MP must NOT.) The fields carry min=today and silently drop past dates; the
validator is the guard against a stale taxFrom.

Tax-mode NOTE: MP's dropdown has exactly one real option, value "1" labelled
" DAYS" (leading space). UP's tax_info selects by visible text "DAYS", which
matches; MP is DAYS-only, enforced upstream by the validator.

SBIePay NOTE: MP's QR page has no id on the QR <img>. It IS the only
data-URI PNG on the page (the SBIePay logo is a .jpg URL), so the
base64-src selector below uniquely matches it — and the new payment-wait
uses qr_selector for BOTH page-detection and image capture, so one selector
serves both.
"""

from __future__ import annotations

from config import (
    CAPTCHA_WAIT_SECS,
    MAX_USER_CAPTCHA_ATTEMPTS,
    PAYMENT_VERIFY_SECS,
)
from engine.pipeline import Phase
from engine.steps import (
    cdp_eval,
    click,
    navigate,
    select_by_value,
    sleep_seconds,
    wait_for_selector,
    wait_for_url,
)
from engine.types import RunContext, RunOutcome
from lifecycle.status import Status

from ..payment_wait import (
    DEFAULT_POSITIVE_PATTERNS,
    PaymentCaptureConfig,
    wait_for_payment_and_capture,
)

# UP's CheckPost wizard is identical to MP's through the disclaimer step, so we
# reuse its proven phases and the two shared gateway selectors verbatim. MP
# overrides only the phases noted in the module docstring. (No import cycle:
# up.py never imports mp.py.)
from .up import (
    PHASE_GAP_SECS,
    SEL_PG_DROPDOWN,
    SEL_PG_SUBMIT,
    select_service,
    owner_info,
    vehicle_info,
    tax_info,
    disclaimer_captcha,
)

ENTRY_URL = "https://parivahan.gov.in/en/node/579"

# ─── MP-only selectors ──────────────────────────────────────────────────
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"  # phase 1
# Phase 8 — SBIePay (epay.sbi.bank.in — NOT SBIePay Lite). The UPI tab is a
# sidebar list-item's inner anchor (UP/HR's a[aria-label='UPI'] does not exist
# on this variant).
SEL_SBIEPAY_UPI_TAB = "li#activeUPI a"
SEL_SBIEPAY_UPI_QR_RADIO = "input#upiQR1"
SEL_SBIEPAY_PAY_NOW = "button#upiButton"
# QR page — the QR <img> has no id and is the only data-URI PNG on the page
# (the SBIePay logo is a .jpg URL), so this matches it uniquely for both
# page-detection and capture.
SEL_QR_IMG = "img[src^='data:image/png;base64']"

# ─── Tuning ─────────────────────────────────────────────────────────────
SBIEPAY_REDIRECT_TIMEOUT = 60  # gateway submit -> epay.sbi.bank.in
SBIEPAY_UPI_PANEL_TIMEOUT = 20  # click UPI tab -> UPI QR radio mounts
SBIEPAY_PAY_NOW_TIMEOUT = 15  # click UPI QR radio -> Pay Now enabled
 # outlive the 290s QR so a late payment's receipt is still captured
MP_QR_WAIT_SECS = 330

_MP_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Madhya Pradesh",
    qr_selector=SEL_QR_IMG,
    receipt_markers=[
        "checkpost tax e-receipt",
        "receipt no",
        "grand total",
        "madhya pradesh",
    ],
    positive_patterns=DEFAULT_POSITIVE_PATTERNS
    + [
        r"government\s*of\s*madhya\s*pradesh",
        r"checkpost\s*tax\s*e-?receipt",
    ],
    payment_wait_secs=MP_QR_WAIT_SECS,
)


# ─── MP-specific phases ──────────────────────────────────────────────────


async def open_portal(ctx: RunContext) -> None:
    await navigate(ctx.session, ENTRY_URL, log=ctx.log, name="p1.open_parivahan")
    await wait_for_selector(
        ctx.session,
        SEL_STATE_DROPDOWN,
        log=ctx.log,
        name="p1.wait_state_dropdown",
        timeout=40,
    )
    await select_by_value(
        ctx.session, SEL_STATE_DROPDOWN, "MP", log=ctx.log, name="p1.select_state_mp"
    )
    await wait_for_url(
        ctx.session, "checkpostv4", log=ctx.log, name="p1.wait_service_page", timeout=45
    )
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p1.settle")


async def payment_gateway(ctx: RunContext) -> None:
    await wait_for_selector(
        ctx.session,
        SEL_PG_DROPDOWN,
        log=ctx.log,
        name="p7.wait_payment_gateway",
        timeout=20,
    )
    # MP exposes a single aggregator: "Direct Payment(SBIePay)", value "SBIe".
    await select_by_value(
        ctx.session,
        SEL_PG_DROPDOWN,
        "SBIe",
        log=ctx.log,
        name="p7.select_sbiepay",
        timeout=10,
    )
    # Terms checkbox has no stable id — tick the first unchecked box.
    await cdp_eval(
        ctx.session,
        "(function(){var cbs=document.querySelectorAll('input[type=checkbox]');"
        "for(var i=0;i<cbs.length;i++){if(!cbs[i].checked){cbs[i].click();"
        "return true;}}return false;})()",
    )
    await click(ctx.session, SEL_PG_SUBMIT, log=ctx.log, name="p7.click_submit")
    await sleep_seconds(2.0, log=ctx.log, name="p7.settle")


async def sbiepay_upi(ctx: RunContext) -> None:
    """MP's SBIePay is the full epay.sbi.bank.in gateway (NOT SBIePay Lite).
    Sidebar UPI tab -> UPI QR radio -> Pay Now -> QR page. There is NO
    Cyber-Treasury/confirm page (unlike UP/HR's input#Go.btn-Yellow)."""
    # The gateway submit redirects to epay.sbi.bank.in.
    await wait_for_url(
        ctx.session,
        "sbi.bank.in",
        log=ctx.log,
        name="p8.wait_sbiepay",
        timeout=SBIEPAY_REDIRECT_TIMEOUT,
    )

    # 1) Left-sidebar UPI tab — the right panel updates on click.
    await wait_for_selector(
        ctx.session,
        SEL_SBIEPAY_UPI_TAB,
        log=ctx.log,
        name="p8.wait_upi_tab",
        timeout=30,
    )
    await click(ctx.session, SEL_SBIEPAY_UPI_TAB, log=ctx.log, name="p8.click_upi_tab")
    await sleep_seconds(2.0, log=ctx.log, name="p8.settle_upi_panel")

    # 2) "UPI QR" radio (mounts in the right panel after the tab click).
    await wait_for_selector(
        ctx.session,
        SEL_SBIEPAY_UPI_QR_RADIO,
        log=ctx.log,
        name="p8.wait_upi_qr_radio",
        timeout=SBIEPAY_UPI_PANEL_TIMEOUT,
    )
    await click(
        ctx.session,
        SEL_SBIEPAY_UPI_QR_RADIO,
        log=ctx.log,
        name="p8.click_upi_qr_radio",
    )
    await sleep_seconds(1.5, log=ctx.log, name="p8.settle_pay_now")

    # 3) Pay Now -> QR page. The payment phase below waits for the QR image.
    await wait_for_selector(
        ctx.session,
        SEL_SBIEPAY_PAY_NOW,
        log=ctx.log,
        name="p8.wait_pay_now",
        timeout=SBIEPAY_PAY_NOW_TIMEOUT,
    )
    await click(ctx.session, SEL_SBIEPAY_PAY_NOW, log=ctx.log, name="p8.click_pay_now")
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p8.settle")


async def payment(ctx: RunContext) -> RunOutcome:
    return await wait_for_payment_and_capture(ctx, config=_MP_PAYMENT_CONFIG)


# enter_status mirrors UP. MP has no intermediate phase, so the shape is UP's
# exactly — only sbiepay_upi's internals and the payment config differ. The
# payment deadline derives from MP's (shorter) configured QR window so a
# config change never trips the pipeline watchdog.
PHASES: list[Phase] = [
    Phase(
        "open_portal", open_portal, enter_status=Status.AI_AGENT_STARTED, max_secs=150
    ),
    Phase("select_service", select_service, max_secs=150),
    Phase("owner_info", owner_info, max_secs=420),  # incl. pending-clear
    Phase("vehicle_info", vehicle_info, max_secs=180),
    Phase("tax_info", tax_info, max_secs=180),
    Phase(
        "disclaimer_captcha",
        disclaimer_captcha,
        max_secs=MAX_USER_CAPTCHA_ATTEMPTS * CAPTCHA_WAIT_SECS + 180,
    ),
    Phase(
        "payment_gateway",
        payment_gateway,
        enter_status=Status.SETTING_UP_PAYMENT_REQUEST,
        max_secs=150,
    ),
    Phase("sbiepay_upi", sbiepay_upi, max_secs=150),
    Phase("payment", payment, max_secs=MP_QR_WAIT_SECS + PAYMENT_VERIFY_SECS + 300),
]
