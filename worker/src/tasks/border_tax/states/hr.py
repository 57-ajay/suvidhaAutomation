# worker/src/tasks/border_tax/states/hr.py
"""Haryana border-tax runner (app source + UPI).

HR rides the SAME parivahan CheckPost wizard as UP — identical DOM through the
Disclaimer / Pay-Online step — and the SAME SBIePay Lite UPI flow after it. The
only structural divergence is a Haryana eGRAS page (egrashry.nic.in) wedged
between the parivahan payment gateway and SBIePay. So this module reuses UP's
proven, state-agnostic phases and DOM helpers verbatim (imported from .up) and
defines ONLY what is genuinely different for HR:

  open_portal        state dropdown value "HR"                        [override]
  select_service     "VEHICLE TAX COLLECTION (OTHER STATE)" + Go        [reused]
  owner_info         vehicle + Get Details -> popup poll (pending /
                     validity / district_ready / timeout) -> district ->
                     checkpoint                                         [reused]
  vehicle_info       validity checks; Vehicle Category / Permit Type /
                     Service Type / Distance — fill ONLY when the RC left
                     a field empty (HR's RC pre-fills most)           [override]
  tax_info           Tax Mode (DAYS / MONTHLY / QUARTERLY / HALF YEARLY /
                     YEARLY); Tax From/Upto are datetime-local ->
                     "YYYY-MM-DDT00:00"; non-DAYS modes auto-lock Tax Upto
                     (read back, never written); Calculate; Next      [override]
  disclaimer_captcha USER-solved captcha + confirm + "Receipt valid" popup
                     + Pay Online + Yes -> vahan eTransPgi gateway      [reused]
  payment_gateway    dropOperator = "EGRAS-SBIA" + terms + input#sendSubmit
                                                                      [override]
  egras_intermediate egrashry.nic.in: SweetAlert2 "Charges" OK -> neutralize
                     native alert/confirm -> Continue -> redirect to SBIePay
                                                                     [HR, new]
  sbiepay_upi        a[aria-label='UPI'] -> yellow CONFIRM input#Go.btn-Yellow
                                                                       [reused]
  payment            img#qrcodeImg -> payment_wait with HR receipt markers
                                                                      [override]

Status FSM is identical to UP (no new statuses): solve_captcha drives
captchaSolving, payment drives qrPaymentNeeded -> verifyingPayment ->
generatingReceipt, and the gateway/eGRAS phases run under
settingUpPaymentRequest. Money-safety mirrors UP: every stop through the
captcha is terminal=cancelled; from the gateway onward (incl. eGRAS) it is
failed (manualReview) — a GRN is a reconcilable artifact, never a silent
success.

Date NOTE: HR's #floatingTaxfrom / #uptpDate are <input type=datetime-local>;
they SILENTLY reject a bare "YYYY-MM-DD". The API normalizes dates to
YYYY-MM-DD, so we append "T00:00". HR is a NO_SAME_DAY state (taxUpto =
taxFrom + duration >= tomorrow), so midnight stays above the Tax-Upto min the
portal pins to 'now'.

Tax-mode NOTE: the option labels carry a leading space (" DAYS"). We match by
visible text first (as UP does) and fall back to HR's option *values*
(DAYS=1, MONTHLY=4, QUARTERLY=5, HALF YEARLY=6, YEARLY=7 — note these differ
from the generic parivahan map) only if the text match misses.

eGRAS NOTE: the two dialogs the page fires on Continue are NATIVE
alert()/confirm(), not DOM modals. We override window.alert/confirm/prompt
before clicking Continue so they auto-accept without wedging the run. (If the
browser session is later configured to auto-accept JS dialogs at the CDP
level, this override is simply redundant.)
"""

from __future__ import annotations

import asyncio
import json

from config import (
    CAPTCHA_WAIT_SECS,
    MAX_USER_CAPTCHA_ATTEMPTS,
    PAYMENT_VERIFY_SECS,
    QR_WAIT_SECS,
)
from engine.pipeline import Phase
from engine.steps import (
    abort_if_popup_text,
    cdp_eval,
    click,
    click_by_text,
    fill,
    get_select_value,
    navigate,
    select_by_text,
    select_by_value,
    sleep_seconds,
    wait_for_selector,
    wait_for_url,
)
from engine.types import RunContext, RunOutcome, ScriptedAbort, StepLog, StepStatus
from lifecycle.status import Status
from redis_client import job_key

from ..extract_amount import extract_and_save_border_tax_amount
from ..payment_wait import (
    DEFAULT_POSITIVE_PATTERNS,
    PaymentCaptureConfig,
    wait_for_payment_and_capture,
)
from ..tax_dates import fill_tax_dates

# UP's CheckPost wizard is identical to HR's up to the payment gateway, so we
# reuse its proven phases and the shared parivahan DOM helpers/constants
# verbatim rather than re-deriving them. HR overrides only the phases noted in
# the module docstring. (No import cycle: up.py never imports hr.py.)
from ..advance import (
    click_next_verified,
    wait_fee_calculation,
    wait_selector_or_restart,
    wait_url_or_restart,
)
from ..manual_entry import fill_vehicle_info_manual
from .up import (
    PHASE_GAP_SECS,
    PERMIT_SET_TIMEOUT_SECS,
    VALIDITY_CLOSE,
    VALIDITY_KEYWORDS,
    SEL_CAPTCHA_INPUT,
    SEL_PERMIT_TYPE,
    SEL_SERVICE_TYPE,
    SEL_TAX_FROM,
    SEL_TAX_UPTO,
    SEL_TAX_MODE_CANDIDATES,
    SEL_PG_DROPDOWN,
    SEL_PG_SUBMIT,
    SEL_QR_IMG,
    _first_non_placeholder_option,
    select_service,
    owner_info,
    disclaimer_captcha,
    sbiepay_upi,
)

ENTRY_URL = "https://parivahan.gov.in/en/node/579"

# ─── HR-only selectors ──────────────────────────────────────────────────
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"  # phase 1
SEL_VEHICLE_CATEGORY = "select#floatingVecCat"  # phase 4 (HR exposes this)
SEL_DISTANCE = "input#floatingDistance"  # phase 4 (HR-only field)
# Phase 7b — eGRAS Haryana intermediate (egrashry.nic.in)
SEL_EGRAS_SWAL_OK = "button.swal2-confirm"
SEL_EGRAS_CONTINUE = "input#ctl00_ContentPlaceHolder1_btnGo"

# HR Tax Mode <option> values — fallback when matching by visible text fails
# (labels carry a leading space). NOTE these codes are HR-specific and differ
# from the generic parivahan map (e.g. HR MONTHLY=4, not 3).
_HR_TAX_MODE_VALUES = {
    "DAYS": "1",
    "MONTHLY": "4",
    "QUARTERLY": "5",
    "HALF YEARLY": "6",
    "YEARLY": "7",
}

# eGRAS is a slow ASP.NET form; give navigation room.
EGRAS_NAV_TIMEOUT = 45  # gateway submit -> egrashry page load
EGRAS_REDIRECT_TIMEOUT = 60  # Continue -> SBIePay redirect

_HR_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Haryana",
    qr_selector=SEL_QR_IMG,
    receipt_markers=[
        "government of haryana",
        "checkpost tax e-receipt",
        "receipt no",
        "grand total",
    ],
    positive_patterns=DEFAULT_POSITIVE_PATTERNS
    + [
        r"government\s*of\s*haryana",
        r"checkpost\s*tax\s*e-?receipt",
    ],
)


# ─── HR-specific phases ──────────────────────────────────────────────────


async def open_portal(ctx: RunContext) -> None:
    await navigate(ctx.session, ENTRY_URL, log=ctx.log, name="p1.open_parivahan")
    await wait_selector_or_restart(
        ctx,
        SEL_STATE_DROPDOWN,
        name="p1.wait_state_dropdown",
        timeout=40,
    )
    await select_by_value(
        ctx.session, SEL_STATE_DROPDOWN, "HR", log=ctx.log, name="p1.select_state_hr"
    )
    await wait_url_or_restart(
        ctx, "checkpostv4", name="p1.wait_service_page", timeout=45
    )
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p1.settle")


async def vehicle_info(ctx: RunContext) -> None:
    p = ctx.params
    validity_abort = (
        f"vehicle {p.vehicleNumber} has no valid insurance/fitness/PUCC — "
        "renew before attempting border tax payment"
    )

    await abort_if_popup_text(
        ctx.session,
        VALIDITY_KEYWORDS,
        validity_abort,
        log=ctx.log,
        name="p4.check_validity_on_load",
        close_selector=VALIDITY_CLOSE,
    )

    await wait_selector_or_restart(
        ctx,
        SEL_PERMIT_TYPE,
        name="p4.wait_vehicle_info_page",
        timeout=30,
    )

    # Manual-entry path (VAHAN had no RC data): fill the ENTIRE vehicle-info
    # step from the RC record fetched in owner_info. HR has a Vehicle Category
    # select AND a Distance field; its validity inputs are plain type="date".
    if ctx.scratch.get("is_manual_entry"):
        await fill_vehicle_info_manual(
            ctx.session,
            ctx.scratch.get("manual_vehicle_details") or {},
            p,
            log=ctx.log,
            name_prefix="p4.manual",
            has_category=True,
            has_distance=True,
            datetime_local_dates=False,
        )
        await abort_if_popup_text(
            ctx.session,
            VALIDITY_KEYWORDS,
            validity_abort,
            log=ctx.log,
            name="p4.check_validity_after_manual_fill",
            close_selector=VALIDITY_CLOSE,
        )
        await click_next_verified(
            ctx,
            name="p4.click_next",
            ready_selector=SEL_TAX_FROM,
        )
        return

    # HR's RC pre-fills most of these; the spec is "keep a filled value, set
    # only an empty one".

    # Vehicle Category — only when present-but-empty. We have no category
    # param, so first-non-placeholder is a best-effort guess; if the field is
    # missing (None) or already filled we leave it. (Tuning candidate: the RC
    # almost always pre-fills this, so the empty branch should rarely fire.)
    cat = await get_select_value(ctx.session, SEL_VEHICLE_CATEGORY)
    if cat == "":
        opt = await _first_non_placeholder_option(
            ctx.session, SEL_VEHICLE_CATEGORY, timeout=10
        )
        if opt.get("ok"):
            await select_by_value(
                ctx.session,
                SEL_VEHICLE_CATEGORY,
                opt["value"],
                log=ctx.log,
                name="p4.select_vehicle_category_first",
            )
            await asyncio.sleep(1.0)

    # Permit Type — two-tier, only if empty (Service Type options depend on it).
    current_permit = await get_select_value(ctx.session, SEL_PERMIT_TYPE)
    if current_permit:
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="p4.permit_already_set",
                status=StepStatus.OK,
                selector=SEL_PERMIT_TYPE,
                value=str(current_permit),
            )
        )
    else:
        attempts: list[tuple[str, str]] = []
        if p.permitType:
            attempts.append(("primary", p.permitType))
        if p.permitTypeFallback and p.permitTypeFallback != p.permitType:
            attempts.append(("fallback", p.permitTypeFallback))

        permit_set = False
        last_err: Exception | None = None
        for label, permit_text in attempts:
            try:
                await select_by_text(
                    ctx.session,
                    SEL_PERMIT_TYPE,
                    permit_text,
                    log=ctx.log,
                    name=f"p4.set_permit_type.{label}",
                    timeout=PERMIT_SET_TIMEOUT_SECS,
                )
                permit_set = True
                break
            except ScriptedAbort as e:
                last_err = e
                continue

        if not permit_set:
            tried = " / ".join(t for _, t in attempts) or "(none)"
            raise ScriptedAbort(
                f"Permit Type was empty for {p.vehicleNumber} and none of the "
                f"configured options [{tried}] were available "
                f"({type(last_err).__name__ if last_err else 'n/a'})",
                terminal="cancelled",
            )
        await asyncio.sleep(1.5)

    await abort_if_popup_text(
        ctx.session,
        VALIDITY_KEYWORDS,
        validity_abort,
        log=ctx.log,
        name="p4.check_validity_after_permit",
        close_selector=VALIDITY_CLOSE,
    )

    # Service Type — HR keeps a pre-filled value; set only if empty/unreadable.
    service_val = await get_select_value(ctx.session, SEL_SERVICE_TYPE)
    if not service_val:
        try:
            await select_by_text(
                ctx.session,
                SEL_SERVICE_TYPE,
                p.serviceType,
                log=ctx.log,
                name="p4.select_service_type",
                timeout=15,
            )
        except ScriptedAbort as e:
            opt = await _first_non_placeholder_option(
                ctx.session, SEL_SERVICE_TYPE, timeout=5
            )
            if opt.get("ok"):
                await select_by_value(
                    ctx.session,
                    SEL_SERVICE_TYPE,
                    opt["value"],
                    log=ctx.log,
                    name="p4.select_service_type_first",
                )
            else:
                raise ScriptedAbort(
                    f"could not set Service Type (wanted {p.serviceType!r}) for "
                    f"{p.vehicleNumber}: {type(e).__name__}",
                    terminal="cancelled",
                )
        await asyncio.sleep(1.0)

    await abort_if_popup_text(
        ctx.session,
        VALIDITY_KEYWORDS,
        validity_abort,
        log=ctx.log,
        name="p4.check_validity_after_service",
        close_selector=VALIDITY_CLOSE,
    )

    # Distance (HR-only, an <input>) — RC usually pre-fills (e.g. 69 km); set
    # only when present-but-empty. The value has no effect on the computed tax.
    dist = await cdp_eval(
        ctx.session,
        "(function(s){var e=document.querySelector(s);"
        "return e ? (e.value||'') : null;})(" + json.dumps(SEL_DISTANCE) + ")",
    )
    if dist == "":
        await fill(
            ctx.session,
            SEL_DISTANCE,
            p.distance or "1000",
            log=ctx.log,
            name="p4.fill_distance",
        )

    # Verified advance to the tax-info section (input#floatingTaxfrom).
    await click_next_verified(
        ctx,
        name="p4.click_next",
        ready_selector=SEL_TAX_FROM,
    )


async def tax_info(ctx: RunContext) -> None:
    p = ctx.params
    await wait_selector_or_restart(
        ctx, SEL_TAX_FROM, name="p5.wait_tax_info_page", timeout=30
    )

    # Tax Mode — match by visible text first (UP-style), fall back to HR's
    # option values (labels carry a leading space).
    tax_mode_set = False
    last_err: Exception | None = None
    for candidate in SEL_TAX_MODE_CANDIDATES:
        try:
            await select_by_text(
                ctx.session,
                candidate,
                p.taxMode,
                log=ctx.log,
                name=f"p5.select_tax_mode[{candidate}]",
                timeout=5,
            )
            tax_mode_set = True
            break
        except ScriptedAbort as e:
            last_err = e
            continue

    if not tax_mode_set:
        val = _HR_TAX_MODE_VALUES.get((p.taxMode or "").strip().upper())
        if val:
            for candidate in SEL_TAX_MODE_CANDIDATES:
                try:
                    await select_by_value(
                        ctx.session,
                        candidate,
                        val,
                        log=ctx.log,
                        name=f"p5.select_tax_mode_byval[{candidate}]",
                        timeout=5,
                    )
                    tax_mode_set = True
                    break
                except ScriptedAbort as e:
                    last_err = e
                    continue

    if not tax_mode_set:
        raise ScriptedAbort(
            f"could not select Tax Mode {p.taxMode!r} on the Haryana portal "
            f"({type(last_err).__name__ if last_err else 'n/a'})",
            terminal="cancelled",
        )

    # Tax From (+ Tax Upto in DAYS mode). HR's fields are datetime-local;
    # fill_tax_dates resolves the time once — the caller-requested taxTime
    # when usable (a same-day past time is clamped to now), else the current
    # IST time — and reuses it for both ends, so the From->Upto span stays an
    # exact 24h multiple (HR is NO_SAME_DAY, so DAYS taxUpto is already
    # taxFrom + duration >= tomorrow, above the field min). See tax_dates.py.
    await fill_tax_dates(
        ctx.session,
        "HR",
        SEL_TAX_FROM,
        SEL_TAX_UPTO,
        p.taxFrom,
        p.taxUpto,
        fills_upto=p.fills_tax_upto,
        log=ctx.log,
        tax_time=p.taxTime,
    )

    if not p.fills_tax_upto:
        # MONTHLY / QUARTERLY / HALF YEARLY / YEARLY: the portal computes and
        # LOCKS Tax Upto from Tax From + mode. Writing it would override the
        # value behind the UI lock, so we read it back for the record only.
        await sleep_seconds(1.0, log=ctx.log, name="p5.wait_auto_tax_upto")
        portal_upto = await cdp_eval(
            ctx.session,
            "(function(s){"
            "var els=Array.prototype.slice.call(document.querySelectorAll(s))"
            "  .filter(function(e){var r=e.getBoundingClientRect();"
            "          return r.width>0 && r.height>0;});"
            "return els.length ? els[els.length-1].value : '';"
            "})(" + json.dumps(SEL_TAX_UPTO) + ")",
        )
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="p5.tax_upto_auto_filled",
                status=StepStatus.OK,
                selector=SEL_TAX_UPTO,
                value=f"portal set {portal_upto!r} for {p.taxMode}",
            )
        )
        if portal_upto:
            try:
                ctx.r.hset(job_key(ctx.job_id), "portalTaxUpto", str(portal_upto))
            except Exception:
                pass

    await sleep_seconds(1.0, log=ctx.log, name="p5.pre_calc_settle")

    await click_by_text(
        ctx.session,
        "Calculate Fee/Tax",
        log=ctx.log,
        name="p5.click_calculate",
        tag="button",
    )
    # Wait for the fee rows (popups classified inline) so the extract below
    # reads the real amount and the first Next click sticks.
    await wait_fee_calculation(ctx)

    # Best-effort amount capture; never aborts (writes a "0" sentinel on miss).
    await extract_and_save_border_tax_amount(ctx)

    # Verified advance onto the Disclaimer page (captcha + Pay Online).
    await click_next_verified(
        ctx,
        name="p5.click_next",
        ready_selector=SEL_CAPTCHA_INPUT,
    )


async def payment_gateway(ctx: RunContext) -> None:
    await wait_for_selector(
        ctx.session,
        SEL_PG_DROPDOWN,
        log=ctx.log,
        name="p7.wait_payment_gateway",
        timeout=20,
    )
    # HR lists three EGRAS aggregators (IDBI / PNB / SBI). SBI keeps the rest
    # of the flow on the same SBIePay path UP uses.
    await select_by_value(
        ctx.session,
        SEL_PG_DROPDOWN,
        "EGRAS-SBIA",
        log=ctx.log,
        name="p7.select_sbi_egras",
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


async def egras_intermediate(ctx: RunContext) -> None:
    """Haryana eGRAS page (egrashry.nic.in) between the parivahan gateway and
    SBIePay. UP has no equivalent — it goes straight to SBIePay."""
    await wait_for_url(
        ctx.session,
        "egrashry",
        log=ctx.log,
        name="p7b.wait_egras_page",
        timeout=EGRAS_NAV_TIMEOUT,
    )

    # 1) Dismiss the SweetAlert2 "Charges for Online transaction!" popup.
    await wait_for_selector(
        ctx.session,
        SEL_EGRAS_SWAL_OK,
        log=ctx.log,
        name="p7b.wait_charges_popup",
        timeout=30,
    )
    await click(
        ctx.session, SEL_EGRAS_SWAL_OK, log=ctx.log, name="p7b.click_charges_ok"
    )
    await sleep_seconds(1.0, log=ctx.log, name="p7b.settle_charges")

    # 2) Neutralize the NATIVE dialogs that Continue fires (a confirm "Please
    #    verify..." and an alert "Please note down GRN..."). Inject before the
    #    click: the page's own handler is queued behind our click listener, so
    #    the overrides are in place when it calls confirm()/alert().
    await cdp_eval(
        ctx.session,
        "(function(){window.alert=function(){return undefined;};"
        "window.confirm=function(){return true;};"
        "window.prompt=function(){return '';};})()",
    )
    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name="p7b.override_native_dialogs",
            status=StepStatus.OK,
            value="alert/confirm/prompt -> no-op/true/''",
        )
    )

    # 3) Continue -> both native dialogs auto-accepted -> redirect to SBIePay.
    await wait_for_selector(
        ctx.session,
        SEL_EGRAS_CONTINUE,
        log=ctx.log,
        name="p7b.wait_continue",
        timeout=20,
    )
    await click(ctx.session, SEL_EGRAS_CONTINUE, log=ctx.log, name="p7b.click_continue")
    await wait_for_url(
        ctx.session,
        "sbi.bank.in",
        log=ctx.log,
        name="p7b.wait_sbiepay_redirect",
        timeout=EGRAS_REDIRECT_TIMEOUT,
    )
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p7b.settle")


async def payment(ctx: RunContext) -> RunOutcome:
    return await wait_for_payment_and_capture(ctx, config=_HR_PAYMENT_CONFIG)


# enter_status mirrors UP: open_portal re-asserts aiAgentStarted so the
# pending-clear restart (pendingTransaction -> aiAgentStarted) is legal;
# captchaSolving / qrPaymentNeeded flip API-side via save-captcha / save-qr.
# egras_intermediate inherits settingUpPaymentRequest from payment_gateway
# (no own enter_status). Per-phase deadlines match UP; the two human-wait
# phases derive theirs from the configured wait windows.
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
    Phase("egras_intermediate", egras_intermediate, max_secs=300),
    Phase("sbiepay_upi", sbiepay_upi, max_secs=150),
    Phase("payment", payment, max_secs=QR_WAIT_SECS + PAYMENT_VERIFY_SECS + 300),
]
