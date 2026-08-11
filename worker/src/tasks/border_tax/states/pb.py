# worker/src/tasks/border_tax/states/pb.py
"""Punjab border-tax runner (app source + UPI).

PB is the closest sibling to HR: it rides the SAME parivahan CheckPost wizard
and inserts an intermediate page between the parivahan gateway and SBIePay.
But PB's intermediate is an IFMS bank-selection page (a plain select + Continue
— NO SweetAlert/native dialogs, unlike HR's eGRAS), and after it PB lands on
the SAME "SBIePay Lite" page UP/HR use (a[aria-label='UPI'] -> yellow CONFIRM
input#Go.btn-Yellow -> img#qrcodeImg). So PB reuses UP's proven phases verbatim
(including sbiepay_upi and the QR selector) and defines ONLY its deltas:

  open_portal        state dropdown value "PB"                        [override]
  select_service     "VEHICLE TAX COLLECTION (OTHER STATE)" + Go        [reused]
  owner_info         vehicle + Get Details -> district -> checkpost      [reused]
                     (PB's compound checkpost names don't match the
                     district, so the desired value falls through to first)
  vehicle_info       validity checks; Vehicle Category / Permit Type /
                     Service Type set-if-empty. NO Distance field (like MP;
                     HR has one)                                      [override]
  tax_info           Tax Mode DAYS or QUARTERLY (text match only); Tax
                     From/Upto are datetime-local -> "YYYY-MM-DDT00:00"
                     (like HR, NOT plain date); QUARTERLY auto-locks Tax
                     Upto (read back, never written); Calculate; Next [override]
  disclaimer_captcha USER-solved captcha + confirm + "Receipt valid" popup +
                     Pay Online + Yes -> vahan eTransPgi gateway         [reused]
  payment_gateway    dropOperator = "IFMS" (PB's lone aggregator) + terms +
                     input#sendSubmit                                 [override]
  ifms_intermediate  IFMS bank-select: select#Bank = SBI (value 1001509) ->
                     Continue -> redirect to SBIePay Lite. No popups.  [PB, new]
  sbiepay_upi        SBIePay Lite: a[aria-label='UPI'] -> yellow CONFIRM
                     input#Go.btn-Yellow                                [reused]
  payment            img#qrcodeImg -> payment_wait with PB markers      [override]

Status FSM is identical to UP (no new statuses). Money-safety mirrors UP:
every stop through the captcha is terminal=cancelled; from the gateway onward
(incl. IFMS) it is failed (manualReview) — never a silent success.

Date NOTE: PB's #floatingTaxfrom / #uptpDate are <input type="datetime-local">
(accepted value "YYYY-MM-DDTHH:MM"), like HR — so we append "T00:00" to the
API-normalized ISO date. (UP/MP use plain type="date" and must NOT append.)
PB is a NO_SAME_DAY state, so DAYS taxUpto = taxFrom + duration (>= tomorrow),
above the field min.

Tax-mode NOTE: PB's dropdown lists DAYS and QUARTERLY only (no MONTHLY). We
match by visible text; the option labels carry a leading space (" DAYS").
DAYS is editable Tax Upto; QUARTERLY auto-locks it (same as UP/HR's period
modes), so the validator strips taxUpto for QUARTERLY and we read it back.

IFMS NOTE: the IFMS page lists three banks (PNB Aggregate / SBI BANK / UNION
BANK OF INDIA). We always pick SBI BANK so the downstream flow is the SBIePay
Lite path UP/HR already handle. The page has no trustworthy URL signal, so we
detect it by the bank dropdown mounting; the post-Continue redirect host is
"merchant.onlinesbi.sbi" (PB) or "merchant.sbi.bank.in" (HR), matched on the
shared "sbi" token, with sbiepay_upi's UPI-link wait as the tighter check.
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

# UP's CheckPost wizard is identical to PB's through the disclaimer step, and
# PB's SBIePay is the same "Lite" product UP/HR use — so we reuse UP's proven
# phases (incl. sbiepay_upi) and the shared selectors/helpers verbatim. PB
# overrides only the phases noted in the module docstring. (No import cycle:
# up.py never imports pb.py.)
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

# ─── PB-only selectors ──────────────────────────────────────────────────
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"  # phase 1
SEL_VEHICLE_CATEGORY = "select#floatingVecCat"  # phase 4 (set-if-empty)
# Phase 7b — IFMS bank-selection (Punjab-specific intermediate). The Continue
# input has no id; identify it by class + type + value from the live HTML.
SEL_BANK_DROPDOWN = "select#Bank"
SEL_BANK_CONTINUE = "input.btnsubmit[type='submit'][value='Continue']"
BANK_VALUE_SBI = "1001509"  # SBI BANK (vs PNB Aggregate / UNION BANK)

# ─── Tuning ─────────────────────────────────────────────────────────────
IFMS_BANK_PAGE_TIMEOUT = 45  # gateway submit -> IFMS bank-select page mount
SBIEPAY_REDIRECT_TIMEOUT = 60  # IFMS Continue -> SBIePay Lite redirect

_PB_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Punjab",
    qr_selector=SEL_QR_IMG,  # img#qrcodeImg (SBIePay Lite), same as UP/HR
    receipt_markers=[
        "government of punjab",
        "checkpost tax e-receipt",
        "receipt no",
        "grand total",
    ],
    positive_patterns=DEFAULT_POSITIVE_PATTERNS
    + [
        r"government\s*of\s*punjab",
        r"checkpost\s*tax\s*e-?receipt",
    ],
)


# ─── PB-specific phases ──────────────────────────────────────────────────


async def open_portal(ctx: RunContext) -> None:
    await navigate(ctx.session, ENTRY_URL, log=ctx.log, name="p1.open_parivahan")
    await wait_selector_or_restart(
        ctx,
        SEL_STATE_DROPDOWN,
        name="p1.wait_state_dropdown",
        timeout=40,
    )
    await select_by_value(
        ctx.session, SEL_STATE_DROPDOWN, "PB", log=ctx.log, name="p1.select_state_pb"
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
    # step from the RC record fetched in owner_info. PB has a Vehicle Category
    # select, no Distance field, and plain <input type="date"> validity inputs.
    if ctx.scratch.get("is_manual_entry"):
        await fill_vehicle_info_manual(
            ctx.session,
            ctx.scratch.get("manual_vehicle_details") or {},
            p,
            log=ctx.log,
            name_prefix="p4.manual",
            has_category=True,
            has_distance=False,
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

    # PB's RC pre-fills most fields; the spec is "keep a filled value, set only
    # an empty one". (Same surface as HR, but PB has no Distance field.)

    # Vehicle Category — only when present-but-empty; pick the first real
    # option (no category param). Missing (None) or filled -> leave it.
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

    # Service Type — set only if empty/unreadable.
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

    # NO Distance field on PB.

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

    # Tax Mode — text match only (PB: DAYS or QUARTERLY; labels carry a
    # leading space). No value fallback: PB's option values aren't pinned and
    # the visible text is stable.
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
        raise ScriptedAbort(
            f"could not select Tax Mode {p.taxMode!r} on the Punjab portal "
            f"(PB supports DAYS or QUARTERLY) "
            f"({type(last_err).__name__ if last_err else 'n/a'})",
            terminal="cancelled",
        )

    # Tax From (+ Tax Upto in DAYS mode). PB's fields are datetime-local;
    # fill_tax_dates resolves the time once — the caller-requested taxTime
    # when usable (a same-day past time is clamped to now), else the current
    # IST time — and reuses it for both ends, so the From->Upto span stays an
    # exact 24h multiple (PB is NO_SAME_DAY, so DAYS taxUpto is already
    # taxFrom + duration >= tomorrow, above the field min). See tax_dates.py.
    await fill_tax_dates(
        ctx.session,
        "PB",
        SEL_TAX_FROM,
        SEL_TAX_UPTO,
        p.taxFrom,
        p.taxUpto,
        fills_upto=p.fills_tax_upto,
        log=ctx.log,
        tax_time=p.taxTime,
    )

    if not p.fills_tax_upto:
        # QUARTERLY — the portal computes and LOCKS Tax Upto from Tax From +
        # mode. Writing it would override the value behind the UI lock, so we
        # read it back for the record only.
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

    # Best-effort amount capture; never aborts.
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
    # PB exposes a single aggregator: "IFMS".
    await select_by_value(
        ctx.session,
        SEL_PG_DROPDOWN,
        "IFMS",
        log=ctx.log,
        name="p7.select_ifms",
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


async def ifms_intermediate(ctx: RunContext) -> None:
    """Punjab IFMS bank-selection page between the parivahan gateway and
    SBIePay Lite. No popups, no native dialogs — select SBI + Continue. (HR's
    analog is eGRAS; UP/MP have no such page.)"""
    # The IFMS page has no trustworthy URL signal — wait for the bank dropdown.
    await wait_for_selector(
        ctx.session,
        SEL_BANK_DROPDOWN,
        log=ctx.log,
        name="p7b.wait_ifms_bank_page",
        timeout=IFMS_BANK_PAGE_TIMEOUT,
    )
    # PB's IFMS lists PNB Aggregate / SBI BANK / UNION BANK OF INDIA; SBI BANK
    # (value 1001509) continues into the SBIePay Lite flow UP/HR already use.
    await select_by_value(
        ctx.session,
        SEL_BANK_DROPDOWN,
        BANK_VALUE_SBI,
        log=ctx.log,
        name="p7b.select_sbi_bank",
        timeout=10,
    )
    await click(ctx.session, SEL_BANK_CONTINUE, log=ctx.log, name="p7b.click_continue")
    # Redirect to SBIePay Lite (PB: merchant.onlinesbi.sbi; HR:
    # merchant.sbi.bank.in) — match the shared "sbi" host token. sbiepay_upi's
    # UPI-link wait below is the tighter sanity check.
    await wait_for_url(
        ctx.session,
        "sbi",
        log=ctx.log,
        name="p7b.wait_sbiepay_redirect",
        timeout=SBIEPAY_REDIRECT_TIMEOUT,
    )
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p7b.settle")


async def payment(ctx: RunContext) -> RunOutcome:
    return await wait_for_payment_and_capture(ctx, config=_PB_PAYMENT_CONFIG)


# enter_status mirrors UP/HR. PB's shape matches HR's (an intermediate phase
# between payment_gateway and sbiepay_upi); only the intermediate's body and
# the tax/gateway specifics differ. ifms_intermediate inherits
# settingUpPaymentRequest from payment_gateway (no own enter_status).
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
    Phase("ifms_intermediate", ifms_intermediate, max_secs=150),
    Phase("sbiepay_upi", sbiepay_upi, max_secs=150),
    Phase("payment", payment, max_secs=QR_WAIT_SECS + PAYMENT_VERIFY_SECS + 300),
]
