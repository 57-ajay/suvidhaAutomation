"""Uttar Pradesh border-tax runner (app source + UPI).

Flow, selectors, and helpers are ported from main's PROVEN up.py — the same
ten portal steps, collapsed into our phase pipeline:

  open_portal        parivahan.gov.in/en/node/579 -> state dropdown value UP
                     -> URL gains "checkpostv4"
  select_service     "VEHICLE TAX COLLECTION (OTHER STATE)" + Go
                     -> URL gains "taxCollectionOnline"
  owner_info         vehicle + Get Details -> outcome poll (popups FIRST:
                     pending / validity / district_ready / timeout) ->
                     district (text, first-option fallback) -> checkpoint
                     (exact -> case-insensitive -> first) -> Next
  vehicle_info       validity checks before/after each field; Permit Type
                     only if empty (two-tier fallback; Service Type options
                     depend on it); Service Type; Next
  tax_info           tax-mode candidate selectors; Tax From/Upto (portal ids
                     keep their typos: floatingTaxfrom, uptpDate);
                     "Calculate Fee/Tax"; amount extraction (best-effort);
                     Next
  disclaimer_captcha USER-solved captcha (div#captcha canvas) + confirm
                     checkbox + "Receipt valid" popup + Pay Online + Yes ->
                     URL gains etranspgi/vahanpgi/paymentgateway
  payment_gateway    dropOperator=SBI + terms checkbox + input#sendSubmit
  sbiepay_upi        a[aria-label='UPI'] -> yellow CONFIRM input#Go.btn-Yellow
  payment            img#qrcodeImg -> payment_wait with UP markers

Money-safety: through the captcha every stop is terminal=cancelled; from the
gateway onward it is failed/partial (manualReview), never a silent success.
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
    dismiss_popup,
    fill,
    get_select_value,
    navigate,
    read_popup_state,
    read_popup_text,
    select_by_text,
    select_by_value,
    sleep_seconds,
    wait_for_selector,
    wait_for_url,
)
from engine.types import (
    RestartFrom,
    RunContext,
    RunOutcome,
    ScriptedAbort,
    StepLog,
    StepStatus,
)
from lifecycle.status import Status
from redis_client import job_key

from .. import pending_clear
from ..captcha_user import solve_captcha
from ..extract_amount import extract_and_save_border_tax_amount
from ..payment_wait import (
    DEFAULT_POSITIVE_PATTERNS,
    PaymentCaptureConfig,
    wait_for_payment_and_capture,
)
from ..tax_dates import fill_tax_dates

ENTRY_URL = "https://parivahan.gov.in/en/node/579"

# ─── Selectors (main's, confirmed against the live portal) ──────────────
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"  # phase 1
SEL_SERVICE_DROPDOWN = "select[name='serviceName']"  # phase 2
SEL_VEHICLE_INPUT = "input#floatingvehicle"  # phase 3
SEL_DISTRICT = "select#floatingDistrict"
SEL_CHECKPOINT = "select#floatingCheckpost"
SEL_PERMIT_TYPE = "select#floatingPrmit"  # portal's own typo, from DOM
SEL_SERVICE_TYPE = "select#floatingService"  # phase 4
SEL_TAX_FROM = "input#floatingTaxfrom"  # lowercase 'f', from DOM
SEL_TAX_UPTO = "input#uptpDate"  # portal's own typo, from DOM
SEL_TAX_MODE_CANDIDATES = [
    "select#floatingtaxmode",
    "select#floatingTaxMode",
    "select#floatingtaxmode",
    "select[name='taxmode']",
    "select[name='taxMode']",
]
# Captcha capture scans the whole document on purpose: the page keeps a
# hidden <canvas id="captcha"> (0x0 rect) next to the visible no-id canvas,
# so any container/id-scoped grab can ship the wrong code.
SEL_CAPTCHA_INPUT = "input#inputcap"  # phase 6
SEL_CAPTCHA_REFRESH = "button[data-bs-original-title*='generate']"
SEL_PG_DROPDOWN = "select#dropOperator"  # phase 7
SEL_PG_SUBMIT = "input#sendSubmit"
SEL_UPI_LINK = "a[aria-label='UPI']"  # phase 8
SEL_SBIEPAY_CONFIRM = "input#Go.btn-Yellow"
SEL_QR_IMG = "img#qrcodeImg"  # phase 9

GATEWAY_URL_MARKERS = ["etranspgi", "vahanpgi", "paymentgateway"]

VALIDITY_KEYWORDS = ["INSURANCE", "FITNESS", "PUCC", "EXPIRED", "RENEW"]
VALIDITY_CLOSE = "button.swal2-confirm, .modal-footer button, .swal-button--confirm"

# The ONLY popups allowed to pass a post-submit check. Everything else is
# treated as the portal blocking the wizard — the run ends as cancelled with
# the popup's own text. Allowlist, not blocklist: the portal keeps inventing
# new blockers ("ALL INDIA TOURIST PERMIT holder are not required to pay any
# Fees and Taxes.") and an unknown popup must end the run with its message,
# never stall into a selector timeout.
BENIGN_POPUP_HINTS = ["RECEIPT VALID"]


def _classify_popup(text: str) -> str:
    """'benign' for the informational notes we dismiss, 'blocking' for
    everything else (validity failures, no-tax-due, unknown errors)."""
    up_text = (text or "").upper()
    if any(h in up_text for h in BENIGN_POPUP_HINTS):
        return "benign"
    return "blocking"


async def _abort_on_blocking_popup(
    ctx: RunContext, name: str, *, seconds: float = 3.5
) -> None:
    """The portal validates ON the Next/Calculate click and raises its popup
    a beat later — so this watchdog runs right AFTER every submit. Any popup
    that isn't explicitly benign ends the run as cancelled, carrying the
    portal's own words (no money has moved at these steps)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        st = await read_popup_state(ctx.session)
        if st.get("present"):
            text = (st.get("text") or "").strip()
            if _classify_popup(text) == "benign":
                ctx.log.record(
                    StepLog(
                        index=ctx.log.next_index(),
                        name=name,
                        status=StepStatus.OK,
                        value=f"benign popup: {text[:80]}",
                    )
                )
                await dismiss_popup(
                    ctx.session, log=ctx.log, name=f"{name}.dismiss_benign"
                )
                await asyncio.sleep(0.5)
                continue
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name=name,
                    status=StepStatus.FAILED,
                    value=f"error_icon={st.get('error')}",
                    error=text[:300] or "(error popup with no text)",
                )
            )
            await dismiss_popup(ctx.session, log=ctx.log, name=f"{name}.close")
            raise ScriptedAbort(
                "the portal blocked the request: "
                + (text[:300] or "an error popup with no readable text"),
                terminal="cancelled",
            )
        await asyncio.sleep(0.5)
    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name=name,
            status=StepStatus.OK,
            value="no blocking popup",
        )
    )


PHASE_GAP_SECS = 1.5
PERMIT_SET_TIMEOUT_SECS = 15
CHECKPOINT_POPULATE_TIMEOUT = 10
# "Yes" -> eTransPgi redirect is slow/variable; how long submit() polls for a
# decisive advanced/rejected signal before reporting not-advanced.
SUBMIT_SETTLE_SECS = 15

_UP_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Uttar Pradesh",
    qr_selector=SEL_QR_IMG,
    receipt_markers=[
        "government of uttar pradesh",
        "checkpost tax e-receipt",
        "receipt no",
        "grand total",
    ],
    positive_patterns=DEFAULT_POSITIVE_PATTERNS
    + [
        r"government\s*of\s*uttar\s*pradesh",
        r"checkpost\s*tax\s*e-?receipt",
    ],
)


# ─── Helpers (ported from main) ──────────────────────────────────────────


async def _first_non_placeholder_option(session, selector: str, timeout: int) -> dict:
    """Wait until a <select> has a real option, pick the first one."""
    deadline = time.monotonic() + timeout
    expr = (
        "(function(s){"
        "var e=document.querySelector(s);"
        "if(!(e instanceof HTMLSelectElement)) return {ok:false, reason:'not_a_select'};"
        "for(var i=0;i<e.options.length;i++){"
        "  var o=e.options[i];"
        "  if(o.value && o.value!=='' && !/select/i.test(o.text||'')){"
        "    e.value=o.value;"
        "    e.dispatchEvent(new Event('input',{bubbles:true}));"
        "    e.dispatchEvent(new Event('change',{bubbles:true}));"
        "    return {ok:true, value:o.value, text:(o.text||'').trim()};"
        "  }"
        "}"
        "return {ok:false, reason:'not_populated_yet'};"
        "})(" + json.dumps(selector) + ")"
    )
    last = {"ok": False, "reason": "no_result"}
    while time.monotonic() < deadline:
        res = await cdp_eval(session, expr)
        if res and res.get("ok"):
            return res
        last = res or last
        await asyncio.sleep(0.5)
    return last


async def _select_checkpoint_with_fallback(
    ctx: RunContext,
    selector: str,
    desired: str,
    *,
    name: str,
    populate_timeout: int = CHECKPOINT_POPULATE_TIMEOUT,
) -> dict:
    """Select an Entry Checkpost option (main's logic verbatim):
      1. Wait until the dropdown has a non-placeholder option (Angular
         populates these after the district is chosen).
      2. Match `desired` by exact text, then case-insensitive equal.
      3. Otherwise pick the FIRST non-empty option.
    UP's district and checkpoint usually share the same name, so the desired
    value typically matches; the fallback covers renamed entries."""
    started = time.monotonic()
    deadline = started + populate_timeout

    expr = (
        "(function(s, d) {"
        "  var e = document.querySelector(s);"
        "  if (!(e instanceof HTMLSelectElement)) return {ok:false, reason:'not_a_select'};"
        "  var trimmed = (d || '').trim();"
        "  var upper = trimmed.toUpperCase();"
        "  var nonEmptyCount = 0;"
        "  for (var c = 0; c < e.options.length; c++) {"
        "    if (e.options[c].value && e.options[c].value !== '') nonEmptyCount++;"
        "  }"
        "  if (nonEmptyCount === 0) return {ok:false, reason:'not_populated_yet'};"
        "  var matchedIdx = -1;"
        "  if (trimmed) {"
        "    for (var i = 0; i < e.options.length; i++) {"
        "      if ((e.options[i].text || '').trim() === trimmed) { matchedIdx = i; break; }"
        "    }"
        "    if (matchedIdx < 0) {"
        "      for (var j = 0; j < e.options.length; j++) {"
        "        if ((e.options[j].text || '').trim().toUpperCase() === upper) { matchedIdx = j; break; }"
        "      }"
        "    }"
        "  }"
        "  var fallbackUsed = false;"
        "  if (matchedIdx < 0) {"
        "    fallbackUsed = true;"
        "    for (var k = 0; k < e.options.length; k++) {"
        "      if (e.options[k].value && e.options[k].value !== '') { matchedIdx = k; break; }"
        "    }"
        "  }"
        "  if (matchedIdx < 0) return {ok:false, reason:'no_options'};"
        "  var opt = e.options[matchedIdx];"
        "  e.value = opt.value;"
        "  e.dispatchEvent(new Event('input', {bubbles:true}));"
        "  e.dispatchEvent(new Event('change', {bubbles:true}));"
        "  return {ok:true, selected:(opt.text||'').trim(), value:opt.value, fallback_used:fallbackUsed};"
        "})(" + json.dumps(selector) + ", " + json.dumps(desired or "") + ")"
    )

    last_reason = "no_result"
    while True:
        res = await cdp_eval(ctx.session, expr)
        if res and res.get("ok"):
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name=name,
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=selector,
                    value=(
                        f"{res.get('selected')!r} (value={res.get('value')!r}, "
                        f"fallback={res.get('fallback_used')})"
                    ),
                )
            )
            return res
        if res:
            last_reason = res.get("reason") or "unknown"
        if time.monotonic() > deadline:
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name=name,
                    status=StepStatus.FAILED,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=selector,
                    error=f"checkpoint_select_timeout: {last_reason}",
                )
            )
            return {"ok": False, "reason": last_reason}
        await asyncio.sleep(0.5)


# Tick the "I confirm that above information are correct..." checkbox.
# Anchored on nearby text; guarded so a second attempt never UN-ticks it.
_TICK_CONFIRM_CHECKBOX_JS = """
(function(){
  var boxes = Array.from(document.querySelectorAll('input[type=checkbox]'));
  function ctxText(cb){
    var t = '';
    if (cb.id) {
      var lbl = document.querySelector('label[for="' + cb.id + '"]');
      if (lbl) t += (lbl.innerText || '') + ' ';
    }
    var pe = cb.closest('label,div,td,p,span');
    if (pe) t += (pe.innerText || '');
    return t.toLowerCase();
  }
  function visible(cb){
    var r = cb.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  var pick = boxes.find(function(cb){ return visible(cb) && /confirm/.test(ctxText(cb)); })
          || boxes.find(visible);
  if (!pick) return {ok:false, reason:'no_checkbox'};
  if (pick.checked) return {ok:true, already:true};
  pick.click();
  return {ok:true, already:false};
})()
"""


# ─── Phases ──────────────────────────────────────────────────────────────


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
        ctx.session, SEL_STATE_DROPDOWN, "UP", log=ctx.log, name="p1.select_state_up"
    )
    await wait_for_url(
        ctx.session, "checkpostv4", log=ctx.log, name="p1.wait_service_page", timeout=45
    )
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p1.settle")


async def select_service(ctx: RunContext) -> None:
    await wait_for_selector(
        ctx.session,
        SEL_SERVICE_DROPDOWN,
        log=ctx.log,
        name="p2.wait_service_dropdown",
        timeout=45,
    )
    await select_by_text(
        ctx.session,
        SEL_SERVICE_DROPDOWN,
        "VEHICLE TAX COLLECTION (OTHER STATE)",
        log=ctx.log,
        name="p2.select_service",
    )
    await click_by_text(
        ctx.session, "Go", log=ctx.log, name="p2.click_go", tag="button"
    )
    await wait_for_url(
        ctx.session,
        "taxCollectionOnline",
        log=ctx.log,
        name="p2.wait_owner_info_page",
        timeout=45,
    )
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p2.settle")


async def owner_info(ctx: RunContext) -> None:
    p = ctx.params
    await wait_for_selector(
        ctx.session,
        SEL_VEHICLE_INPUT,
        log=ctx.log,
        name="p3.wait_vehicle_input",
        timeout=30,
    )
    await fill(
        ctx.session,
        SEL_VEHICLE_INPUT,
        p.vehicleNumber,
        log=ctx.log,
        name="p3.fill_vehicle",
    )
    await click_by_text(
        ctx.session,
        "Get Details",
        log=ctx.log,
        name="p3.click_get_details",
        tag="button",
    )

    outcome = await pending_clear.wait_for_owner_info_outcome(
        ctx.session,
        ctx.log,
        name="p3.wait_owner_outcome",
    )

    if outcome == "validity_popup":
        raise ScriptedAbort(
            f"vehicle {p.vehicleNumber} has no valid insurance/fitness/PUCC "
            "— renew before attempting border tax payment",
            terminal="cancelled",
        )
    if outcome == "timeout":
        raise ScriptedAbort(
            "the portal did not respond within 30s after Get Details — it "
            "may be slow or the RC fetch failed; try again shortly",
            terminal="cancelled",
        )
    if outcome == "pending_popup":
        if not pending_clear.auto_clear_enabled():
            raise ScriptedAbort(
                f"a previous transaction for {p.vehicleNumber} is still "
                "pending on the portal — clear it via 'Check Pending "
                "Transaction' under Border Tax Payment and try again",
                terminal="cancelled",
            )
        await ctx.reporter.set_status(Status.PENDING_TRANSACTION)
        clear_status, hint = await pending_clear.clear_pending_transaction(ctx)
        if clear_status == "still_on_hold":
            raise ScriptedAbort(
                f"a pending transaction for {p.vehicleNumber} cannot be cleared yet",
                # f"cleared yet — the portal says retry in ~{hint}",
                terminal="cancelled",
            )
        if clear_status != "cleared":
            raise ScriptedAbort(
                f"pending transaction could not be cleared ({hint.removesuffix("OKNoCancel")})",
                terminal="cancelled",
            )
        raise RestartFrom("open_portal", "pending transaction cleared")

    # outcome == district_ready
    try:
        await select_by_text(
            ctx.session,
            SEL_DISTRICT,
            p.entryDistrict,
            log=ctx.log,
            name="p3.select_district",
            timeout=10,
        )
    except ScriptedAbort:
        opt = await _first_non_placeholder_option(
            ctx.session,
            SEL_DISTRICT,
            timeout=CHECKPOINT_POPULATE_TIMEOUT,
        )
        if not opt.get("ok"):
            raise ScriptedAbort(
                "the Entry District dropdown had no selectable options",
                terminal="cancelled",
            )
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="p3.select_district_first",
                status=StepStatus.OK,
                value=f"{opt.get('text')!r} (fallback)",
            )
        )

    await asyncio.sleep(1.0)  # checkpost options populate after district

    desired_checkpoint = p.entryCheckpoint or p.entryDistrict
    cp = await _select_checkpoint_with_fallback(
        ctx,
        SEL_CHECKPOINT,
        desired_checkpoint,
        name="p3.select_checkpoint",
    )
    if not cp.get("ok"):
        raise ScriptedAbort(
            f"could not select an Entry Checkpost for district "
            f"{p.entryDistrict!r} ({cp.get('reason')}) — the dropdown was "
            "empty or never populated",
            terminal="cancelled",
        )

    await click_by_text(
        ctx.session, "Next", log=ctx.log, name="p3.click_next", tag="button"
    )
    await _abort_on_blocking_popup(ctx, "p3.post_next_popup_check")
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p3.settle")


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

    # RC data sometimes pre-fills Permit Type; Service Type options are
    # conditional on it. Fill only when empty, primary -> fallback.
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
                f"Permit Type was empty for {p.vehicleNumber} and none of "
                f"the configured options [{tried}] were available "
                f"({type(last_err).__name__ if last_err else 'n/a'})",
                terminal="cancelled",
            )
        # Give Angular a beat before Service Type's options are queried.
        await asyncio.sleep(1.5)

    await abort_if_popup_text(
        ctx.session,
        VALIDITY_KEYWORDS,
        validity_abort,
        log=ctx.log,
        name="p4.check_validity_after_permit",
        close_selector=VALIDITY_CLOSE,
    )

    await select_by_text(
        ctx.session,
        SEL_SERVICE_TYPE,
        p.serviceType,
        log=ctx.log,
        name="p4.select_service_type",
        timeout=15,
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
            f"could not find the Tax Mode dropdown "
            f"({type(last_err).__name__ if last_err else 'n/a'})",
            terminal="cancelled",
        )

    # Tax From (+ Tax Upto in DAYS mode). UP's fields are plain type="date",
    # so fill_tax_dates detects that and sends the bare ISO date (no time) —
    # byte-identical to before, now routed through the shared, config-checked
    # path so new states inherit correct date/datetime handling. MP reuses
    # this tax_info verbatim and is also type="date"; stateCode keeps the
    # config/drift logs labelled per job. See tax_dates.py.
    await fill_tax_dates(
        ctx.session,
        p.stateCode or "UP",
        SEL_TAX_FROM,
        SEL_TAX_UPTO,
        p.taxFrom,
        p.taxUpto,
        fills_upto=p.fills_tax_upto,
        log=ctx.log,
    )

    if not p.fills_tax_upto:
        # MONTHLY/QUARTERLY/YEARLY: the portal computes and locks Tax Upto
        # from Tax From + mode. Writing it would override that value behind
        # the UI lock, so we don't touch it — we read it back for the record.
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
                value=f"portal set {portal_upto!r} for {p.taxMode}"
                + (
                    " (client-sent taxUpto ignored by design)"
                    if str(getattr(p, "taxUpto", "")).strip()
                    else ""
                ),
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

    # Best-effort: never aborts; "0" sentinel lands in borderTaxAmount.
    await extract_and_save_border_tax_amount(ctx)

    await click_by_text(
        ctx.session, "Next", log=ctx.log, name="p5.click_next", tag="button"
    )
    await _abort_on_blocking_popup(ctx, "p5.post_next_popup_check")
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p5.settle")


async def disclaimer_captcha(ctx: RunContext) -> None:
    await wait_for_selector(
        ctx.session, SEL_CAPTCHA_INPUT, log=ctx.log, name="p6.wait_captcha", timeout=30
    )

    async def submit() -> bool:
        # Tick "I confirm..." (guarded — never un-ticks on a retry).
        try:
            res = await cdp_eval(ctx.session, _TICK_CONFIRM_CHECKBOX_JS)
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="p6.confirm_checkbox",
                    status=StepStatus.OK
                    if res and res.get("ok")
                    else StepStatus.RETRIED,
                    value=str(res),
                )
            )
        except Exception:
            pass
        # Ticking can raise the "Receipt valid for X Days" summary popup.
        await asyncio.sleep(0.8)
        await dismiss_popup(ctx.session, log=ctx.log, name="p6.close_validity_note")

        await click_by_text(
            ctx.session,
            "Pay Online",
            log=ctx.log,
            name="p6.pay_online",
            tag="button",
            timeout=10,
        )
        await asyncio.sleep(2.0)
        # "Are you sure? You want to pay online?" -> Yes.
        try:
            await click_by_text(
                ctx.session,
                "Yes",
                log=ctx.log,
                name="p6.confirm_yes",
                tag="button",
                timeout=4,
            )
        except ScriptedAbort:
            pass
        
        # OLD STALE CODE
        # await asyncio.sleep(1.5)
        #
        # url = (await current_url(ctx.session)).lower()
        # captcha_gone = await cdp_eval(
        #     ctx.session,
        #     "!document.querySelector(" + json.dumps(SEL_CAPTCHA_INPUT) + ")",
        # )
        # return any(g in url for g in GATEWAY_URL_MARKERS) or bool(captcha_gone)
        # OLD STALE CODE END


        # The eTransPgi redirect after "Yes" is slow and highly variable. Poll
        # for a DECISIVE signal instead of checking once after a fixed sleep:
        #   advanced = gateway URL present, OR the captcha input left the DOM
        #              (we navigated off the disclaimer page)
        #   rejected = a captcha invalid/mismatch popup is up -> stop early
        # Without this, a slow redirect reads as "not advanced" while we're
        # still on the disclaimer page with the captcha present, so the solver
        #refreshes and retries a captcha the portal already ACCEPTED (and in
        # AI mode the retry then finds no canvas and fails/hands off).
        deadline = time.monotonic() + SUBMIT_SETTLE_SECS

        while time.monotonic() < deadline:
            url = (await current_url(ctx.session)).lower()

            if any(g in url for g in GATEWAY_URL_MARKERS):
                return True

            captcha_gone = await cdp_eval(
                ctx.session,
                "!document.querySelector(" + json.dumps(SEL_CAPTCHA_INPUT) + ")",
            )

            if captcha_gone:
                return True

            # Decisive the other way: a captcha invalid/mismatch popup. Inlined
            # (vs calling rejected()) so submit() stays self-contained.
            from engine.steps import read_popup_text

            ptxt = (await read_popup_text(ctx.session) or "").lower()

            if "captcha" in ptxt and ("invalid" in ptxt or "mismatch" in ptxt):
                return False

            await asyncio.sleep(0.5)

        # No decisive signal within the window — report not-advanced. Safe: no
        # money moves before the gateway page, and solve_captcha re-evaluates.
        return False

    async def rejected() -> bool:
        from engine.steps import read_popup_text

        txt = await read_popup_text(ctx.session)
        if not txt:
            return False
        low = txt.lower()
        return "captcha" in low and ("invalid" in low or "mismatch" in low)

    await solve_captcha(
        ctx,
        status=Status.CAPTCHA_SOLVING,
        stage="disclaimer",
        input_selector=SEL_CAPTCHA_INPUT,
        refresh_selector=SEL_CAPTCHA_REFRESH,
        submit_action=submit,
        is_rejected=rejected,
        canvas_scope=None,   
    )

async def payment_gateway(ctx: RunContext) -> None:
    await wait_for_selector(
        ctx.session,
        SEL_PG_DROPDOWN,
        log=ctx.log,
        name="p7.wait_payment_gateway",
        timeout=20,
    )
    await select_by_value(
        ctx.session,
        SEL_PG_DROPDOWN,
        "SBI",
        log=ctx.log,
        name="p7.select_sbi",
        timeout=10,
    )
    # Terms checkbox has no stable id — tick the first unchecked box.
    await cdp_eval(
        ctx.session,
        """
        (function() {
            var cbs = document.querySelectorAll('input[type=checkbox]');
            for (var i = 0; i < cbs.length; i++) {
                if (!cbs[i].checked) { cbs[i].click(); return true; }
            }
            return false;
        })()
    """,
    )
    await click(ctx.session, SEL_PG_SUBMIT, log=ctx.log, name="p7.click_submit")
    await sleep_seconds(2.0, log=ctx.log, name="p7.settle")


async def sbiepay_upi(ctx: RunContext) -> None:
    await wait_for_selector(
        ctx.session,
        SEL_UPI_LINK,
        log=ctx.log,
        name="p8.wait_sbiepay_welcome",
        timeout=30,
    )
    await click(ctx.session, SEL_UPI_LINK, log=ctx.log, name="p8.click_upi", timeout=10)
    await sleep_seconds(2.0, log=ctx.log, name="p8.settle")
    await wait_for_selector(
        ctx.session,
        SEL_SBIEPAY_CONFIRM,
        log=ctx.log,
        name="p8b.wait_confirm_page",
        timeout=30,
    )
    await click(ctx.session, SEL_SBIEPAY_CONFIRM, log=ctx.log, name="p8b.click_confirm")
    await sleep_seconds(2.0, log=ctx.log, name="p8b.settle")


async def payment(ctx: RunContext) -> RunOutcome:
    return await wait_for_payment_and_capture(ctx, config=_UP_PAYMENT_CONFIG)


# enter_status notes: open_portal re-asserts aiAgentStarted so the
# pending-clear restart (pendingTransaction -> aiAgentStarted) is legal;
# captchaSolving flips API-side with save-captcha; qrPaymentNeeded with
# save-qr — image and status land together for the client.
# Per-phase deadlines: portal-only phases get tight budgets; the two
# human-wait phases derive theirs from the configured wait windows so a
# config bump never trips the pipeline watchdog.
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
    Phase("payment", payment, max_secs=QR_WAIT_SECS + PAYMENT_VERIFY_SECS + 300),
]
