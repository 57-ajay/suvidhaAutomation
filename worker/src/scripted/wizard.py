# worker/src/scripted/wizard.py
"""Shared parivahan CheckPost wizard phases (1-5) for the SCRIPTED (web) path.

Every scripted state rides the SAME parivahan Angular wizard the fully
automated states use — identical DOM through the Disclaimer / Pay-Online step
(proven in the old repo across UP/HR/PB/MP/UK/HP/BR/TN and re-proven by the
autoScale states). This module is the scripted path's OWN copy of those
phases: the flow logic is deliberately duplicated from tasks/border_tax
(module isolation over DRY, by decision) so a scripted issue never requires
reading the fully-automated module. Only platform pieces are shared: engine
(pipeline/steps/types/log), lifecycle (reporter/status), api_client,
redis_client, config.

Phase walk (each state builds its PHASES from these):

  open_portal      parivahan.gov.in/en/node/579 -> state dropdown value
                   (factory: make_open_portal("UK")) -> URL gains "checkpostv4"
  select_service   "VEHICLE TAX COLLECTION (OTHER STATE)" + Go
  owner_info       vehicle + Get Details -> outcome poll (popups FIRST) ->
                   district -> checkpost (exact -> case-insensitive -> FIRST
                   option, which naturally covers the old "first_option"
                   strategy states UK/TN/BR) -> Next.
                   A "pending transaction" popup ABORTS with a clear
                   "clear it first, then retry" message — the scripted path
                   has a human operator who can clear it on the portal
                   themselves, so v1 ships no auto-clear machinery (that is
                   the old handover-family behavior; see TASK.md follow-ups).
                   VAHAN "No data found" -> on-demand RC fetch + manual fill
                   (scripted.manual_entry), same as the auto path.
  vehicle_info     validity checks around every field; Vehicle Category /
                   Permit Type / Service Type / Distance handled per the
                   flags each state passes (set-if-empty semantics).
  tax_info         Tax Mode by visible text, then per-state <option> value
                   map, then (list_offered_on_miss states: UK/TN/BR) abort
                   with abort_reason "tax_mode_not_offered_for_rc" listing
                   exactly the modes the portal offered for this RC; Tax
                   From/Upto via scripted.tax_dates (runtime type detection);
                   Calculate Fee/Tax; amount extraction (best-effort); Next.

...and the wizard stops there — the next phase is scripted.handover, which
sets humanHandover and gives the browser to the operator on the live view.

Blocking-popup retry: the portal's background validity fetches (insurance /
fitness / PUCC / road tax) are flaky and throw spurious "renew your ..."
blockers a human-paced run never sees. Every blocking popup detected in
these phases therefore restarts the whole wizard from open_portal
(RestartFrom) until SCRIPTED_POPUP_MAX_ATTEMPTS total attempts are spent;
only then does the run abort — still cancelled, still carrying the portal's
own message (see _blocked_popup_restart_or_abort). SCRIPTED_NEXT_DELAY_SECS
adds extra breathing room after every Next click before the next page is
touched. Both knobs live in config.py / the environment.

Money line: everything in this module is pre-payment; every abort is
terminal="cancelled" (retryable, no money moved).
"""

from __future__ import annotations

import asyncio
import json
import time

import api_client
from config import SCRIPTED_NEXT_DELAY_SECS, SCRIPTED_POPUP_MAX_ATTEMPTS
from engine.steps import (
    abort_if_popup_text,
    cdp_eval,
    click_by_text,
    dismiss_popup,
    fill,
    get_select_value,
    navigate,
    read_popup_state,
    select_by_text,
    select_by_value,
    sleep_seconds,
    wait_for_selector,
    wait_for_url,
)
from engine.types import RestartFrom, RunContext, ScriptedAbort, StepLog, StepStatus

from .extract_amount import extract_and_save_border_tax_amount
from .manual_entry import (
    dismiss_chassis_bug_popup,
    dismiss_no_data_popup,
    fill_owner_info_manual,
    fill_vehicle_info_manual,
)
from .tax_dates import fill_tax_dates

ENTRY_URL = "https://parivahan.gov.in/en/node/579"

# ─── Selectors (shared parivahan Angular DOM, confirmed live) ────────────
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"  # phase 1
SEL_SERVICE_DROPDOWN = "select[name='serviceName']"  # phase 2
SEL_VEHICLE_INPUT = "input#floatingvehicle"  # phase 3
SEL_DISTRICT = "select#floatingDistrict"
SEL_CHECKPOINT = "select#floatingCheckpost"
SEL_VEHICLE_CATEGORY = "select#floatingVecCat"  # phase 4 (some states)
SEL_PERMIT_TYPE = "select#floatingPrmit"  # portal's own typo, from DOM
SEL_SERVICE_TYPE = "select#floatingService"
SEL_DISTANCE = "input#floatingDistance"  # HR-only field
SEL_TAX_FROM = "input#floatingTaxfrom"  # lowercase 'f', from DOM
SEL_TAX_UPTO = "input#uptpDate"  # portal's own typo, from DOM
SEL_TAX_MODE_CANDIDATES = [
    "select#floatingtaxmode",
    "select#floatingTaxMode",
    "select[name='taxmode']",
    "select[name='taxMode']",
]

VALIDITY_KEYWORDS = ["INSURANCE", "FITNESS", "PUCC", "EXPIRED", "RENEW"]
VALIDITY_CLOSE = "button.swal2-confirm, .modal-footer button, .swal-button--confirm"

# The ONLY popups allowed to pass a post-submit check. Everything else is
# treated as the portal blocking the wizard — the run ends as cancelled with
# the popup's own text (allowlist, not blocklist: the portal keeps inventing
# new blockers and an unknown popup must end the run with its message, never
# stall into a selector timeout).
BENIGN_POPUP_HINTS = ["RECEIPT VALID"]

PHASE_GAP_SECS = 1.5
PERMIT_SET_TIMEOUT_SECS = 15
CHECKPOINT_POPULATE_TIMEOUT = 10
OWNER_OUTCOME_POLL_SECS = 30.0
OWNER_OUTCOME_POLL_TICK_SECS = 0.5


# ─── Popup classification (copied from the auto path) ────────────────────


def _classify_popup(text: str) -> str:
    """'benign' for the informational notes we dismiss, 'blocking' for
    everything else (validity failures, no-tax-due, unknown errors)."""
    up_text = (text or "").upper()
    if any(h in up_text for h in BENIGN_POPUP_HINTS):
        return "benign"
    # A spurious "enter chassis no." popup is a known gov-site bug — click OK
    # and continue. Benign UNLESS it is really a validity/pending blocker that
    # merely happens to mention the chassis.
    if "CHASSIS" in up_text and not any(
        k in up_text
        for k in ("INSURANCE", "FITNESS", "PUCC", "EXPIRED", "RENEW", "PENDING")
    ):
        return "benign"
    return "blocking"


def _blocked_popup_restart_or_abort(
    ctx: RunContext,
    *,
    name: str,
    popup_text: str,
    final_message: str,
    reason: str | None = None,
) -> None:
    """A blocking portal popup was detected during form-fill (pre-handover).

    The portal fetches validity data (insurance/fitness/PUCC/road tax) through
    flaky background APIs — the same vehicle that trips "renew your ..." here
    almost always passes on a fresh manual pass. So instead of dying on the
    first blocker, rerun the whole wizard from the portal landing page while
    the attempt budget lasts (SCRIPTED_POPUP_MAX_ATTEMPTS counts TOTAL
    attempts; the counter lives in ctx.scratch and survives restarts). Once
    the budget is spent, abort exactly like before — cancelled, carrying the
    portal's own words.

    Money line: only wizard.py (phases 1-5, strictly pre-payment) calls this;
    handover.py never does, so a restart can never rewind past a payment.
    """
    attempt = int(ctx.scratch.get("popup_attempt") or 1)
    budget = max(1, SCRIPTED_POPUP_MAX_ATTEMPTS)
    if attempt < budget:
        ctx.scratch["popup_attempt"] = attempt + 1
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name=f"{name}.restart",
                status=StepStatus.RETRIED,
                attempt=attempt,
                value=(
                    f"blocking popup -> restarting wizard "
                    f"(attempt {attempt + 1}/{budget}): {popup_text[:120]}"
                ),
            )
        )
        try:
            from redis_client import job_key

            ctx.r.hset(job_key(ctx.job_id), "popupRestarts", str(attempt))
        except Exception:
            pass
        raise RestartFrom(
            "open_portal", f"portal popup: {popup_text[:80] or 'no text'}"
        )
    suffix = f" (still blocked after {budget} attempts)" if budget > 1 else ""
    raise ScriptedAbort(final_message + suffix, terminal="cancelled", reason=reason)


async def abort_on_blocking_popup(
    ctx: RunContext, name: str, *, seconds: float = 3.5
) -> None:
    """The portal validates ON the Next/Calculate click and raises its popup a
    beat later — so this watchdog runs right AFTER every submit. Any popup
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
            _blocked_popup_restart_or_abort(
                ctx,
                name=name,
                popup_text=text,
                final_message="the portal blocked the request: "
                + (text[:300] or "an error popup with no readable text"),
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


# ─── Small DOM helpers (copied from the auto path) ───────────────────────


async def first_non_placeholder_option(session, selector: str, timeout: int) -> dict:
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
    """Select an Entry Checkpost option:
      1. Wait until the dropdown has a non-placeholder option (Angular
         populates these after the district is chosen).
      2. Match `desired` by exact text, then case-insensitive equal.
      3. Otherwise pick the FIRST non-empty option.
    Match-the-district states (UP/HR/HP) hit 2; the old "first_option"
    strategy states (UK/TN/BR — checkpost names don't track the district)
    fall through to 3 naturally."""
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


# ─── Owner-info outcome probe (probe only — NO auto-clear machinery) ─────
# Copied from the auto path's pending_clear.wait_for_owner_info_outcome. The
# clear_pending_transaction flow (its captcha solver etc.) is intentionally
# NOT ported: the scripted path aborts on a pending transaction with a clear
# operator instruction instead. See the module docstring.

_OWNER_INFO_OUTCOME_JS = """
(function() {
  var popups = document.querySelectorAll('.swal2-popup');
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var text = (popups[i].textContent || '');
    var lower = text.toLowerCase();
    if (lower.indexOf('pending for verification') !== -1
        || (lower.indexOf('check pending transaction') !== -1
            && lower.indexOf('pending') !== -1)) {
      return {state: 'pending_popup', text: text.substring(0, 240)};
    }
    if (/(insurance|fitness|pucc|expired|renew|not\\s*valid)/i.test(text)) {
      return {state: 'validity_popup', text: text.substring(0, 240)};
    }
    if (lower.indexOf('no data found') !== -1) {
      return {state: 'manual_entry', text: text.substring(0, 240)};
    }
    if (lower.indexOf('chassis') !== -1) {
      return {state: 'chassis_bug', text: text.substring(0, 240)};
    }
  }
  var district = document.querySelector('select#floatingDistrict');
  if (district) {
    var rect = district.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      return {state: 'district_ready'};
    }
  }
  return {state: 'pending'};
})()
"""


async def _wait_for_owner_info_outcome(
    ctx: RunContext,
    *,
    name: str = "p3.wait_owner_outcome",
    timeout: float = OWNER_OUTCOME_POLL_SECS,
    tick: float = OWNER_OUTCOME_POLL_TICK_SECS,
) -> str:
    """Poll after Get Details for one of: district_ready / manual_entry /
    pending_popup / validity_popup / timeout. Never raises — the caller
    branches. A spurious "enter chassis no." popup (a known gov-site bug) is
    clicked away inline and polling continues."""
    started = time.monotonic()
    deadline = started + timeout
    last_state, last_text = "pending", ""

    while time.monotonic() < deadline:
        res = await cdp_eval(ctx.session, _OWNER_INFO_OUTCOME_JS)
        state = (res or {}).get("state", "pending")
        last_state = state
        last_text = (res or {}).get("text", "") or last_text

        if state == "district_ready":
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name=name,
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value="district_ready",
                )
            )
            return "district_ready"
        if state == "manual_entry":
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name=name,
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value=f"manual_entry: {last_text[:120]}",
                )
            )
            return "manual_entry"
        if state == "chassis_bug":
            await dismiss_chassis_bug_popup(
                ctx.session, log=ctx.log, name=f"{name}.chassis_bug"
            )
            await asyncio.sleep(tick)
            continue
        if state in ("pending_popup", "validity_popup"):
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name=name,
                    status=StepStatus.FAILED,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value=f"{state}: {last_text[:120]}",
                )
            )
            return state
        await asyncio.sleep(tick)

    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name=name,
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            value=f"timeout (last_state={last_state})",
            error=f"no decisive outcome within {timeout:.0f}s",
        )
    )
    return "timeout"


# ─── Phase 1 factory ─────────────────────────────────────────────────────


def make_open_portal(state_value: str, state_slug: str):
    """Phase 1: open parivahan and pick the state on the landing dropdown.
    `state_value` is the <option value> ("UK", "TN", ...)."""

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
            ctx.session,
            SEL_STATE_DROPDOWN,
            state_value,
            log=ctx.log,
            name=f"p1.select_state_{state_slug}",
        )
        await wait_for_url(
            ctx.session,
            "checkpostv4",
            log=ctx.log,
            name="p1.wait_service_page",
            timeout=45,
        )
        await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p1.settle")

    return open_portal


# ─── Phase 2 ─────────────────────────────────────────────────────────────


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
    await click_by_text(ctx.session, "Go", log=ctx.log, name="p2.click_go", tag="button")
    await wait_for_url(
        ctx.session,
        "taxCollectionOnline",
        log=ctx.log,
        name="p2.wait_owner_info_page",
        timeout=45,
    )
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p2.settle")


# ─── Phase 3 ─────────────────────────────────────────────────────────────


async def owner_info(ctx: RunContext) -> None:
    p = ctx.params
    ctx.scratch["is_manual_entry"] = False
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

    outcome = await _wait_for_owner_info_outcome(ctx)

    if outcome == "validity_popup":
        # Usually the portal's own background RC fetch failing, not a real
        # expiry — restart the wizard while the popup budget lasts (the popup
        # text itself is already in the runLog from the outcome poll).
        _blocked_popup_restart_or_abort(
            ctx,
            name="p3.owner_outcome",
            popup_text="validity popup after Get Details",
            final_message=(
                f"vehicle {p.vehicleNumber} has no valid insurance/fitness/PUCC "
                "— renew before attempting border tax payment"
            ),
        )
    if outcome == "timeout":
        raise ScriptedAbort(
            "the portal did not respond within 30s after Get Details — it "
            "may be slow or the RC fetch failed; try again shortly",
            terminal="cancelled",
        )
    if outcome == "pending_popup":
        # Scripted v1: no auto-clear. The web operator can clear it on the
        # portal ("Check Pending Transaction" under Border Tax Payment) and
        # re-run — that is exactly the old handover-family behavior. A restart
        # cannot clear it either, but the popup budget is cheap and keeps the
        # rule simple ("every blocking popup retries"); the final abort keeps
        # this message + reason so the operator playbook is unchanged.
        _blocked_popup_restart_or_abort(
            ctx,
            name="p3.owner_outcome",
            popup_text="pending-transaction popup after Get Details",
            final_message=(
                f"a previous transaction for {p.vehicleNumber} is still pending "
                "on the portal — clear it via 'Check Pending Transaction' under "
                "Border Tax Payment, then retry"
            ),
            reason="pending_transaction_popup",
        )

    if outcome == "manual_entry":
        # VAHAN returned "No data found" — fetch the RC record on demand and
        # fill owner-info by hand; vehicle_info picks the flag up from scratch.
        await dismiss_no_data_popup(ctx.session, log=ctx.log, name="p3.dismiss_no_data")
        vd = await api_client.fetch_vehicle_details(
            request_id=p.requestId,
            driver_id=p.driverId,
            vehicle_number=p.vehicleNumber,
        )
        if not vd:
            raise ScriptedAbort(
                f"VAHAN has no data for {p.vehicleNumber} and the RC lookup "
                "returned no record, so the owner/vehicle details can't be "
                "filled — check the vehicle number or try again shortly",
                terminal="cancelled",
            )
        ctx.scratch["is_manual_entry"] = True
        ctx.scratch["manual_vehicle_details"] = vd
        await dismiss_chassis_bug_popup(
            ctx.session, log=ctx.log, name="p3.dismiss_chassis_bug"
        )
        await fill_owner_info_manual(
            ctx.session,
            vd,
            log=ctx.log,
            name_prefix="p3.manual",
            mobile_number=p.mobileNumber or "0000000000",
        )

    # district_ready (VAHAN auto-filled) or manual_entry (filled just above).
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
        opt = await first_non_placeholder_option(
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

    await click_by_text(ctx.session, "Next", log=ctx.log, name="p3.click_next", tag="button")
    if SCRIPTED_NEXT_DELAY_SECS > 0:
        await sleep_seconds(
            SCRIPTED_NEXT_DELAY_SECS, log=ctx.log, name="p3.post_next_delay"
        )
    await abort_on_blocking_popup(ctx, "p3.post_next_popup_check")
    await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p3.settle")


# ─── Phase 4 (parameterized per state) ───────────────────────────────────


def make_vehicle_info(
    *,
    set_category_if_empty: bool,
    has_distance: bool,
    service_set_if_empty: bool,
    service_first_option_fallback: bool,
    manual_has_category: bool = True,
    manual_has_distance: bool = False,
    manual_datetime_dates: bool = False,
):
    """Phase 4 factory. The parivahan vehicle-info step is the same form on
    every state; RC data pre-fills most of it, and the spec everywhere is
    "keep a filled value, set only an empty one". The flags express the real
    per-state differences (from the proven auto states + the old handover
    runner):

      set_category_if_empty          HR/PB/HP/UK/TN/BR: Vehicle Category set
                                     to the first real option when empty.
                                     UP/MP: RC pre-fills it; left untouched.
      has_distance                   HR only: fill Distance when empty.
      service_set_if_empty           True everywhere except UP/MP, which set
                                     Service Type unconditionally (the old
                                     behavior for those two).
      service_first_option_fallback  when the configured serviceType isn't in
                                     the dropdown: pick the first real option
                                     (HP-family) instead of aborting (UP).
      manual_*                       flags forwarded to fill_vehicle_info_manual
                                     for the VAHAN-no-data path.
    """

    async def vehicle_info(ctx: RunContext) -> None:
        p = ctx.params
        validity_abort = (
            f"vehicle {p.vehicleNumber} has no valid insurance/fitness/PUCC — "
            "renew before attempting border tax payment"
        )

        try:
            await abort_if_popup_text(
                ctx.session,
                VALIDITY_KEYWORDS,
                validity_abort,
                log=ctx.log,
                name="p4.check_validity_on_load",
                close_selector=VALIDITY_CLOSE,
            )
        except ScriptedAbort as e:
            _blocked_popup_restart_or_abort(
                ctx,
                name="p4.check_validity_on_load",
                popup_text=e.message,
                final_message=e.message,
                reason=e.reason,
            )

        await wait_for_selector(
            ctx.session,
            SEL_PERMIT_TYPE,
            log=ctx.log,
            name="p4.wait_vehicle_info_page",
            timeout=30,
        )

        # Manual-entry path (VAHAN had no RC data): fill the ENTIRE step from
        # the RC record fetched in owner_info.
        if ctx.scratch.get("is_manual_entry"):
            await fill_vehicle_info_manual(
                ctx.session,
                ctx.scratch.get("manual_vehicle_details") or {},
                p,
                log=ctx.log,
                name_prefix="p4.manual",
                has_category=manual_has_category,
                has_distance=manual_has_distance,
                datetime_local_dates=manual_datetime_dates,
            )
            try:
                await abort_if_popup_text(
                    ctx.session,
                    VALIDITY_KEYWORDS,
                    validity_abort,
                    log=ctx.log,
                    name="p4.check_validity_after_manual_fill",
                    close_selector=VALIDITY_CLOSE,
                )
            except ScriptedAbort as e:
                _blocked_popup_restart_or_abort(
                    ctx,
                    name="p4.check_validity_after_manual_fill",
                    popup_text=e.message,
                    final_message=e.message,
                    reason=e.reason,
                )
            await click_by_text(
                ctx.session, "Next", log=ctx.log, name="p4.click_next", tag="button"
            )
            if SCRIPTED_NEXT_DELAY_SECS > 0:
                await sleep_seconds(
                    SCRIPTED_NEXT_DELAY_SECS, log=ctx.log, name="p4.post_next_delay"
                )
            await abort_on_blocking_popup(ctx, "p4.post_next_popup_check")
            await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p4.settle")
            return

        # Vehicle Category — only when present-but-empty; first real option.
        if set_category_if_empty:
            cat = await get_select_value(ctx.session, SEL_VEHICLE_CATEGORY)
            if cat == "":
                opt = await first_non_placeholder_option(
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
                else:
                    ctx.log.record(
                        StepLog(
                            index=ctx.log.next_index(),
                            name="p4.vehicle_category_empty",
                            status=StepStatus.RETRIED,
                            selector=SEL_VEHICLE_CATEGORY,
                            error="no selectable options; leaving empty",
                        )
                    )

        # Permit Type — two-tier, only if empty (Service Type options depend
        # on it).
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
            await asyncio.sleep(1.5)

        try:
            await abort_if_popup_text(
                ctx.session,
                VALIDITY_KEYWORDS,
                validity_abort,
                log=ctx.log,
                name="p4.check_validity_after_permit",
                close_selector=VALIDITY_CLOSE,
            )
        except ScriptedAbort as e:
            _blocked_popup_restart_or_abort(
                ctx,
                name="p4.check_validity_after_permit",
                popup_text=e.message,
                final_message=e.message,
                reason=e.reason,
            )

        # Service Type.
        service_val = (
            await get_select_value(ctx.session, SEL_SERVICE_TYPE)
            if service_set_if_empty
            else ""
        )
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
                if not service_first_option_fallback:
                    raise
                opt = await first_non_placeholder_option(
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
                        f"could not set Service Type (wanted {p.serviceType!r}) "
                        f"for {p.vehicleNumber}: {type(e).__name__}",
                        terminal="cancelled",
                    )
            await asyncio.sleep(1.0)

        try:
            await abort_if_popup_text(
                ctx.session,
                VALIDITY_KEYWORDS,
                validity_abort,
                log=ctx.log,
                name="p4.check_validity_after_service",
                close_selector=VALIDITY_CLOSE,
            )
        except ScriptedAbort as e:
            _blocked_popup_restart_or_abort(
                ctx,
                name="p4.check_validity_after_service",
                popup_text=e.message,
                final_message=e.message,
                reason=e.reason,
            )

        # Distance (HR only) — fill only when present-but-empty; no effect on
        # the computed tax.
        if has_distance:
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

        await click_by_text(
            ctx.session, "Next", log=ctx.log, name="p4.click_next", tag="button"
        )
        if SCRIPTED_NEXT_DELAY_SECS > 0:
            await sleep_seconds(
                SCRIPTED_NEXT_DELAY_SECS, log=ctx.log, name="p4.post_next_delay"
            )
        await abort_on_blocking_popup(ctx, "p4.post_next_popup_check")
        await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p4.settle")

    return vehicle_info


# ─── Phase 5 (parameterized per state) ───────────────────────────────────


async def _select_tax_mode_with_offered(
    ctx: RunContext, desired: str, value_map: dict[str, str], *, timeout: int = 20
) -> tuple[bool, list[str]]:
    """Select the Tax Mode by known <option> value first, then by trimmed
    case-insensitive text, re-querying the live <select> each poll (the
    control is populated asynchronously and labels carry leading spaces).
    Returns (ok, offered_labels) — the labels let the caller report exactly
    which modes the portal offered for this RC when `desired` never appears.
    Ported from the old handover runner's _select_tax_mode."""
    want_value = (value_map or {}).get(desired.strip().upper(), "")
    want_text = desired.strip().upper()

    select_js = (
        "(function(sels, wantValue, wantText){"
        "  for (var si=0; si<sels.length; si++){"
        "    var e=document.querySelector(sels[si]);"
        "    if(!(e instanceof HTMLSelectElement)) continue;"
        "    var labels=[]; var hitIdx=-1;"
        "    for(var i=0;i<e.options.length;i++){"
        "      var o=e.options[i]; var t=(o.text||'').trim(); var v=(o.value||'');"
        "      if(v!==''){ labels.push(t); }"
        "      var hit=false;"
        "      if(wantValue && v===wantValue){ hit=true; }"
        "      else if(v!=='' && t.toUpperCase()===wantText){ hit=true; }"
        "      if(hit){ hitIdx=i; }"
        "    }"
        "    if(hitIdx>=0){"
        "      e.selectedIndex=hitIdx; e.value=e.options[hitIdx].value;"
        "      e.dispatchEvent(new Event('input',{bubbles:true}));"
        "      e.dispatchEvent(new Event('change',{bubbles:true}));"
        "      return {ok:true, value:e.options[hitIdx].value,"
        "              text:(e.options[hitIdx].text||'').trim(), labels:labels};"
        "    }"
        "    return {ok:false, labels:labels};"
        "  }"
        "  return {ok:false, labels:[]};"
        "})("
        + json.dumps(SEL_TAX_MODE_CANDIDATES)
        + ", "
        + json.dumps(want_value)
        + ", "
        + json.dumps(want_text)
        + ")"
    )

    started = time.monotonic()
    deadline = started + timeout
    last_labels: list[str] = []
    while time.monotonic() < deadline:
        res = await cdp_eval(ctx.session, select_js)
        if res and res.get("ok"):
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name=f"p5.select_tax_mode[{desired}]",
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value=f"{res.get('text')} (value={res.get('value')}); "
                    f"offered={res.get('labels')}",
                )
            )
            return True, list(res.get("labels") or [])
        if res:
            last_labels = list(res.get("labels") or []) or last_labels
        await asyncio.sleep(0.5)

    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name=f"p5.select_tax_mode[{desired}]",
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"tax mode {desired!r} not found; available={last_labels}",
        )
    )
    return False, last_labels


def make_tax_info(
    *,
    state_code: str,
    tax_mode_values: dict[str, str] | None = None,
    list_offered_on_miss: bool = False,
):
    """Phase 5 factory.

      tax_mode_values      per-state <option value> fallback map used when the
                           visible-text match misses (labels carry leading
                           spaces on several states). None = text-only.
      list_offered_on_miss UK/TN/BR: the portal renders different Tax Mode
                           options per RC, so a requested mode may legitimately
                           not be offered — abort with
                           abort_reason="tax_mode_not_offered_for_rc" and the
                           offered list so the client can retry with a valid
                           mode. Off for UP/HR/MP/PB/HP (fixed mode lists;
                           validator already guards).

    Tax From / Tax Upto go through scripted.tax_dates (runtime input-type
    detection: date vs datetime-local); Tax Upto is written only in DAYS mode
    — the portal computes and LOCKS it for every other mode, and we read the
    portal's value back for the record.
    """

    async def tax_info(ctx: RunContext) -> None:
        p = ctx.params
        await wait_for_selector(
            ctx.session,
            SEL_TAX_FROM,
            log=ctx.log,
            name="p5.wait_tax_info_page",
            timeout=30,
        )

        ok, offered = await _select_tax_mode_with_offered(
            ctx, p.taxMode, tax_mode_values or {}
        )
        if not ok:
            if list_offered_on_miss:
                shown = ", ".join(offered) if offered else "(none listed)"
                raise ScriptedAbort(
                    f"the portal does not offer Tax Mode {p.taxMode!r} for "
                    f"{p.vehicleNumber}'s RC — offered: {shown}. Retry with "
                    "one of the offered modes.",
                    terminal="cancelled",
                    reason="tax_mode_not_offered_for_rc",
                )
            raise ScriptedAbort(
                f"could not select Tax Mode {p.taxMode!r} on the "
                f"{state_code} portal",
                terminal="cancelled",
            )

        await fill_tax_dates(
            ctx.session,
            p.stateCode or state_code,
            SEL_TAX_FROM,
            SEL_TAX_UPTO,
            p.taxFrom,
            p.taxUpto,
            fills_upto=p.fills_tax_upto,
            log=ctx.log,
            tax_time=getattr(p, "taxTime", ""),
        )

        if not p.fills_tax_upto:
            # Non-DAYS modes: the portal computes and LOCKS Tax Upto. Read it
            # back for the record only.
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
                    from redis_client import job_key

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
        await abort_on_blocking_popup(ctx, "p5.post_calculate_popup_check", seconds=6.0)
        await sleep_seconds(4.0, log=ctx.log, name="p5.wait_calculation")

        # Best-effort: never aborts; "0" sentinel lands in borderTaxAmount.
        await extract_and_save_border_tax_amount(ctx)

        await click_by_text(
            ctx.session, "Next", log=ctx.log, name="p5.click_next", tag="button"
        )
        if SCRIPTED_NEXT_DELAY_SECS > 0:
            await sleep_seconds(
                SCRIPTED_NEXT_DELAY_SECS, log=ctx.log, name="p5.post_next_delay"
            )
        await abort_on_blocking_popup(ctx, "p5.post_next_popup_check")
        await sleep_seconds(PHASE_GAP_SECS, log=ctx.log, name="p5.settle")

    return tax_info
