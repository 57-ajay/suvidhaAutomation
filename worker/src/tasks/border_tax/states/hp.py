# worker/src/tasks/border_tax/states/hp.py
"""Himachal Pradesh border-tax runner (app source + UPI).

HP rides the SAME parivahan CheckPost wizard as UP — identical DOM through the
Disclaimer / Pay-Online step — and the SAME SBIePay Lite UPI flow after it.
Structurally HP is the closest sibling to PB: an intermediate treasury page is
wedged between the parivahan payment gateway and SBIePay Lite. PB's intermediate
is an IFMS bank-select (a plain select + Continue); HP's is a himkosh Cyber
Treasury eChallan page that ALSO carries its own captcha. So HP reuses UP's
proven, state-agnostic phases and DOM helpers verbatim (imported from .up) and
defines ONLY what is genuinely different for HP:

  open_portal        state dropdown value "HP"                        [override]
  select_service     "VEHICLE TAX COLLECTION (OTHER STATE)" + Go        [reused]
  owner_info         vehicle + Get Details -> popup poll (pending /
                     validity / district_ready / timeout) -> district ->
                     checkpost (checkpost name == district)             [reused]
  vehicle_info       validity checks; Vehicle Category / Permit Type /
                     Service Type set-if-empty. NO Distance field (like
                     PB/MP; HR has one)                               [override]
  tax_info           Tax Mode DAYS / QUARTERLY / YEARLY (text match, then
                     HP option-value fallback); Tax From/Upto are
                     datetime-local -> "YYYY-MM-DDTHH:MM" (like HR/PB, NOT
                     plain date); non-DAYS modes auto-lock Tax Upto (read
                     back, never written); Calculate; Next            [override]
  disclaimer_captcha USER/AI-solved captcha (div#captcha canvas, input#inputcap)
                     + confirm + "Receipt valid" popup + Pay Online + Yes ->
                     vahan eTransPgi gateway                            [reused]
  payment_gateway    dropOperator = "CTP" + terms + input#sendSubmit  [override]
  himkosh_intermediate himkosh.hp.nic.in eChallan: bank ddBank = SBI MOPS
                     (value "MOP"); e-banking radio (default); solve the
                     page's OWN ASP.NET <img> captcha; Make Payment ->
                     native confirm() auto-accepted -> redirect to SBIePay
                     Lite                                             [HP, new]
  sbiepay_upi        SBIePay Lite: a[aria-label='UPI'] -> yellow CONFIRM
                     input#Go.btn-Yellow                                [reused]
  payment            img#qrcodeImg -> payment_wait with HP receipt markers
                                                                      [override]

Status FSM is identical to UP (no new statuses): solve_captcha drives
captchaSolving (silent in AI mode), payment drives qrPaymentNeeded ->
verifyingPayment -> generatingReceipt, and the gateway/himkosh phases run under
settingUpPaymentRequest. Money-safety mirrors UP: every stop through a captcha
is terminal=cancelled (the himkosh captcha GATES Make Payment — until it is
accepted the page never POSTs to SBIePay, so no money has moved); from the
SBIePay UPI step onward the payment phase owns the failed/parked outcomes — a
captured receipt or an explicit reviewer is the only "money moved" signal.

Date NOTE: HP's #floatingTaxfrom / #uptpDate are <input type="datetime-local">
(accepted value "YYYY-MM-DDTHH:MM"), like HR/PB — so fill_tax_dates stamps the
current IST time onto the API-normalized ISO date. (UP/MP use plain
type="date" and must NOT append a time.) HP is a NO_SAME_DAY state, so in DAYS
mode the API already sets taxUpto = taxFrom + duration (>= tomorrow), which
keeps Tax Upto above the portal's "min pinned to now" even with a now-time
stamp.

Tax-mode NOTE: HP's dropdown lists DAYS / QUARTERLY / YEARLY only (no MONTHLY /
HALF YEARLY). The option labels carry a leading space (" DAYS"); we match by
visible text first (as UP does) and fall back to HP's option *values*
(DAYS=1, QUARTERLY=5, YEARLY=7 — note these differ from HR's map) only if the
text match misses. DAYS keeps Tax Upto editable; QUARTERLY / YEARLY auto-lock
it, so the validator strips taxUpto for those and we read it back.

himkosh NOTE: the eChallan page has no trustworthy URL signal, so we detect it
by the bank dropdown (select#ContentPlaceHolder1_ddBank) mounting. Its captcha
is an ASP.NET <img> (../Common/Captcha.aspx), NOT a canvas — solve_captcha's
image_selector routes the capture through capture_element_png. There is no
refresh control: a rejected captcha triggers an ASP.NET postback that ships a
fresh image, so the refresh selector is intentionally a no-match and the
solver re-captures after the reject path settles. Make Payment's onclick fires
a NATIVE confirm() via ConfirmOn(); we override window.confirm/alert/prompt
before clicking so it auto-accepts without wedging the run. (If the browser
session is later configured to auto-accept JS dialogs at the CDP level, this
override is simply redundant.)
"""

from __future__ import annotations

import asyncio
import json
import time

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
    current_url,
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

from ..captcha_user import solve_captcha
from ..extract_amount import extract_and_save_border_tax_amount
from ..payment_wait import (
    DEFAULT_POSITIVE_PATTERNS,
    PaymentCaptureConfig,
    wait_for_payment_and_capture,
)
from ..tax_dates import fill_tax_dates

# UP's CheckPost wizard is identical to HP's through the disclaimer step, and
# HP's SBIePay is the same "Lite" product UP/HR/PB use — so we reuse UP's proven
# phases (incl. sbiepay_upi) and the shared selectors/helpers verbatim. HP
# overrides only the phases noted in the module docstring. (No import cycle:
# up.py never imports hp.py.)
from ..manual_entry import fill_vehicle_info_manual
from .up import (
    PHASE_GAP_SECS,
    PERMIT_SET_TIMEOUT_SECS,
    VALIDITY_CLOSE,
    VALIDITY_KEYWORDS,
    SEL_PERMIT_TYPE,
    SEL_SERVICE_TYPE,
    SEL_TAX_FROM,
    SEL_TAX_UPTO,
    SEL_TAX_MODE_CANDIDATES,
    SEL_PG_DROPDOWN,
    SEL_PG_SUBMIT,
    SEL_QR_IMG,
    _abort_on_blocking_popup,
    _first_non_placeholder_option,
    select_service,
    owner_info,
    disclaimer_captcha,
    sbiepay_upi,
)

ENTRY_URL = "https://parivahan.gov.in/en/node/579"

# ─── HP-only selectors ───────────────────────────────────────────────────
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"  # phase 1
SEL_VEHICLE_CATEGORY = "select#floatingVecCat"  # phase 4 (set-if-empty)

# Phase 7b — himkosh Cyber Treasury eChallan (HP-specific intermediate).
# The page has no trustworthy URL signal, so we wait for the bank dropdown.
SEL_HIMKOSH_BANK = "select#ContentPlaceHolder1_ddBank"
SEL_HIMKOSH_CAPTCHA_IMG = "img#ContentPlaceHolder1_Img1"  # ASP.NET <img>, not canvas
SEL_HIMKOSH_CAPTCHA_INPUT = "input#ContentPlaceHolder1_searchtext"
SEL_HIMKOSH_SUBMIT = "input#ContentPlaceHolder1_btnSubmit"  # "MAKE PAYMENT"
# No refresh control on the himkosh captcha: a rejected captcha postback ships a
# fresh image on its own. A deliberate no-match keeps the solver's _refresh a
# no-op so it simply re-captures after the reject path settles.
SEL_HIMKOSH_CAPTCHA_NOREFRESH = "#__hp_himkosh_no_refresh__"
# SBI MOPS keeps the rest of the flow on the SBIePay Lite path UP/HR/PB use.
HIMKOSH_BANK_VALUE_SBI_MOPS = "MOP"

# ─── Tuning ──────────────────────────────────────────────────────────────
HIMKOSH_PAGE_TIMEOUT = 45  # gateway Submit -> himkosh eChallan page mount
HIMKOSH_SUBMIT_SETTLE_SECS = 25  # Make Payment -> SBIePay redirect / reject
HIMKOSH_REDIRECT_TIMEOUT = 60  # accepted captcha -> SBIePay Lite redirect

_HP_TAX_MODE_VALUES = {"DAYS": "1", "QUARTERLY": "5", "YEARLY": "7"}

_HP_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Himachal Pradesh",
    qr_selector=SEL_QR_IMG,  # img#qrcodeImg (SBIePay Lite), same as UP/HR/PB
    receipt_markers=[
        "government of himachal pradesh",
        "checkpost tax e-receipt",
        "receipt no",
        "grand total",
    ],
    positive_patterns=DEFAULT_POSITIVE_PATTERNS
    + [
        r"government\s*of\s*himachal\s*pradesh",
        r"checkpost\s*tax\s*e-?receipt",
    ],
)


# ─── HP-specific phases ──────────────────────────────────────────────────


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
        ctx.session, SEL_STATE_DROPDOWN, "HP", log=ctx.log, name="p1.select_state_hp"
    )
    await wait_for_url(
        ctx.session, "checkpostv4", log=ctx.log, name="p1.wait_service_page", timeout=45
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

    await wait_for_selector(
        ctx.session,
        SEL_PERMIT_TYPE,
        log=ctx.log,
        name="p4.wait_vehicle_info_page",
        timeout=30,
    )

    # Manual-entry path (VAHAN had no RC data): fill the ENTIRE vehicle-info
    # step from the RC record fetched in owner_info. HP has a Vehicle Category
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
        await click_by_text(
            ctx.session, "Next", log=ctx.log, name="p4.click_next", tag="button"
        )
        await _abort_on_blocking_popup(ctx, "p4.post_next_popup_check")
        await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p4.settle")
        return

    # HP's RC pre-fills most fields; the spec is "keep a filled value, set only
    # an empty one". (Same surface as PB — HP has no Distance field either.)

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

    # NOTE: HP has NO Distance field (unlike HR). Nothing to fill here.

    await click_by_text(
        ctx.session, "Next", log=ctx.log, name="p4.click_next", tag="button"
    )
    await _abort_on_blocking_popup(ctx, "p4.post_next_popup_check")
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p4.settle")


async def tax_info(ctx: RunContext) -> None:
    p = ctx.params
    await wait_for_selector(
        ctx.session, SEL_TAX_FROM, log=ctx.log, name="p5.wait_tax_info_page", timeout=30
    )

    # Tax Mode — match by visible text first (UP-style), fall back to HP's
    # option values (labels carry a leading space; DAYS=1/QUARTERLY=5/YEARLY=7).
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
        val = _HP_TAX_MODE_VALUES.get((p.taxMode or "").strip().upper())
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
            f"could not select Tax Mode {p.taxMode!r} on the Himachal Pradesh "
            f"portal ({type(last_err).__name__ if last_err else 'n/a'})",
            terminal="cancelled",
        )

    # Tax From (+ Tax Upto in DAYS mode). HP's fields are datetime-local;
    # fill_tax_dates resolves the time once — the caller-requested taxTime
    # when usable (a same-day past time is clamped to now), else the current
    # IST time — and reuses it for both ends, so the From->Upto span stays an
    # exact 24h multiple (HP is NO_SAME_DAY, so DAYS taxUpto is already
    # taxFrom + duration >= tomorrow, above the field min). See tax_dates.py.
    await fill_tax_dates(
        ctx.session,
        "HP",
        SEL_TAX_FROM,
        SEL_TAX_UPTO,
        p.taxFrom,
        p.taxUpto,
        fills_upto=p.fills_tax_upto,
        log=ctx.log,
        tax_time=p.taxTime,
    )

    if not p.fills_tax_upto:
        # QUARTERLY / YEARLY: the portal computes and LOCKS Tax Upto from Tax
        # From + mode. Writing it would override the value behind the UI lock,
        # so we read it back for the record only.
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
    await _abort_on_blocking_popup(ctx, "p5.post_calculate_popup_check", seconds=6.0)
    await sleep_seconds(4.0, log=ctx.log, name="p5.wait_calculation")

    # Best-effort amount capture; never aborts (writes a "0" sentinel on miss).
    await extract_and_save_border_tax_amount(ctx)

    await click_by_text(
        ctx.session, "Next", log=ctx.log, name="p5.click_next", tag="button"
    )
    await _abort_on_blocking_popup(ctx, "p5.post_next_popup_check")
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p5.settle")


async def payment_gateway(ctx: RunContext) -> None:
    await wait_for_selector(
        ctx.session,
        SEL_PG_DROPDOWN,
        log=ctx.log,
        name="p7.wait_payment_gateway",
        timeout=20,
    )
    # HP exposes the "CTP" aggregator (Cyber Treasury Payment), which routes
    # through the himkosh eChallan page. Select by value, then fall back to the
    # visible label if HP's option value ever differs from the displayed text.
    try:
        await select_by_value(
            ctx.session,
            SEL_PG_DROPDOWN,
            "CTP",
            log=ctx.log,
            name="p7.select_ctp",
            timeout=10,
        )
    except ScriptedAbort:
        await select_by_text(
            ctx.session,
            SEL_PG_DROPDOWN,
            "CTP",
            log=ctx.log,
            name="p7.select_ctp_bytext",
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


async def himkosh_intermediate(ctx: RunContext) -> None:
    """HP Cyber-Treasury eChallan page (himkosh.hp.nic.in) between the parivahan
    CTP gateway and SBIePay Lite. Select SBI MOPS, solve the page's OWN image
    captcha (ASP.NET <img>, not a canvas), then Make Payment -> native confirm()
    auto-accepted -> redirect to SBIePay Lite. PB's analog is IFMS (no captcha);
    HR's is eGRAS; UP/MP have no such page."""
    # The himkosh page has no trustworthy URL signal — wait for the bank
    # dropdown to mount.
    await wait_for_selector(
        ctx.session,
        SEL_HIMKOSH_BANK,
        log=ctx.log,
        name="p7b.wait_himkosh_page",
        timeout=HIMKOSH_PAGE_TIMEOUT,
    )
    # SBI MOPS keeps the downstream flow on the SBIePay Lite path UP/HR/PB use.
    await select_by_value(
        ctx.session,
        SEL_HIMKOSH_BANK,
        HIMKOSH_BANK_VALUE_SBI_MOPS,
        log=ctx.log,
        name="p7b.select_sbi_mops",
        timeout=10,
    )
    # Payment Type radio defaults to "e-banking" — leave it as-is.

    async def submit() -> bool:
        # Re-assert the bank (idempotent; harmless if a rejected-captcha
        # postback reset the <select>) and neutralize the NATIVE confirm() that
        # Make Payment's ConfirmOn() fires, so it auto-accepts instead of
        # wedging the run. Inject BEFORE the click: the page's own handler is
        # queued behind our click listener, so the override is in place when it
        # calls confirm().
        await select_by_value(
            ctx.session,
            SEL_HIMKOSH_BANK,
            HIMKOSH_BANK_VALUE_SBI_MOPS,
            log=ctx.log,
            name="p7b.reassert_sbi_mops",
            timeout=5,
        )
        await cdp_eval(
            ctx.session,
            "(function(){window.confirm=function(){return true;};"
            "window.alert=function(){return undefined;};"
            "window.prompt=function(){return '';};})()",
        )
        await click(
            ctx.session,
            SEL_HIMKOSH_SUBMIT,
            log=ctx.log,
            name="p7b.click_make_payment",
        )
        deadline = time.monotonic() + HIMKOSH_SUBMIT_SETTLE_SECS
        while time.monotonic() < deadline:
            url = (await current_url(ctx.session)).lower()
            if "sbi" in url:
                return True
            captcha_gone = await cdp_eval(
                ctx.session,
                "!document.querySelector("
                + json.dumps(SEL_HIMKOSH_CAPTCHA_INPUT)
                + ")",
            )
            if captcha_gone:
                return True
            await asyncio.sleep(0.5)
        # No decisive signal within the window — report not-advanced. Safe:
        # until the captcha is accepted the page never POSTs to SBIePay, so no
        # money has moved and solve_captcha re-evaluates with a fresh image.
        return False

    async def rejected() -> bool:
        # A rejected captcha reloads the himkosh page (still NOT on SBI) with
        # the captcha input still present.
        url = (await current_url(ctx.session)).lower()
        if "sbi" in url:
            return False
        still_there = await cdp_eval(
            ctx.session,
            "!!document.querySelector(" + json.dumps(SEL_HIMKOSH_CAPTCHA_INPUT) + ")",
        )
        return bool(still_there)

    # The himkosh captcha is an ASP.NET <img> (../Common/Captcha.aspx), not a
    # canvas — image_selector routes the capture to capture_element_png. The
    # captcha GATES Make Payment, so exhaustion is a safe cancel (no money has
    # moved): solve_captcha's default terminal="cancelled" is correct here.
    await solve_captcha(
        ctx,
        status=Status.CAPTCHA_SOLVING,
        stage="himkosh",
        input_selector=SEL_HIMKOSH_CAPTCHA_INPUT,
        refresh_selector=SEL_HIMKOSH_CAPTCHA_NOREFRESH,
        submit_action=submit,
        is_rejected=rejected,
        image_selector=SEL_HIMKOSH_CAPTCHA_IMG,
    )

    # solve_captcha returns only once Make Payment was accepted; confirm the
    # SBIePay Lite redirect before handing off to sbiepay_upi. (SBIePay Lite
    # host is merchant.onlinesbi.sbi — matched on the shared "sbi" token;
    # sbiepay_upi's UPI-link wait is the tighter check.)
    await wait_for_url(
        ctx.session,
        "sbi",
        log=ctx.log,
        name="p7b.wait_sbiepay_redirect",
        timeout=HIMKOSH_REDIRECT_TIMEOUT,
    )
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p7b.settle")


async def payment(ctx: RunContext) -> RunOutcome:
    return await wait_for_payment_and_capture(ctx, config=_HP_PAYMENT_CONFIG)


# enter_status mirrors UP/HR/PB. HP's shape matches PB's (an intermediate phase
# between payment_gateway and sbiepay_upi); only the intermediate's body and the
# tax/gateway specifics differ. himkosh_intermediate inherits
# settingUpPaymentRequest from payment_gateway (no own enter_status); its
# mid-phase captchaSolving (human fallback only — silent in AI mode) is
# overwritten by the payment phase's save-qr (qrPaymentNeeded).
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
    Phase(
        "himkosh_intermediate",
        himkosh_intermediate,
        max_secs=MAX_USER_CAPTCHA_ATTEMPTS * CAPTCHA_WAIT_SECS + 240,
    ),
    Phase("sbiepay_upi", sbiepay_upi, max_secs=150),
    Phase("payment", payment, max_secs=QR_WAIT_SECS + PAYMENT_VERIFY_SECS + 300),
]
