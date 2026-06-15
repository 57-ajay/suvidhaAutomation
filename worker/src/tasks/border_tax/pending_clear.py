"""Pending-transaction detection + auto-clear on the CheckPost portal.

Ported from main's _pending_clear.py — same JS probes, same selectors (live
HTML, CheckPost V4.7.x), same outcome semantics. Two public surfaces:

wait_for_owner_info_outcome(): after "Get Details", a single JS evaluation
distinguishes (popups FIRST — with a blocker up, the district select never
renders): pending_popup / validity_popup / district_ready / timeout.

clear_pending_transaction(): the documented recovery. The Angular router
rewrites direct deep links to "/", so we always land on the portal home and
click the "Check Pending Transaction" routerLink. The page captcha is solved
with Vertex OCR (background recovery, the user never sees it), Go is
classified as results / captcha-mismatch / pending, the first row's
bank icon gets the triple-click treatment (icon + td + synthetic event —
the Angular binding moves around), and the outcome poll decides:
  "Please try after X Minutes"            -> ("still_on_hold", hint)
  URL DoubleVerification|/bank/ or body
  "transaction is fail"|"initiate
  transaction again"                      -> ("cleared", "")
  nothing decisive in 30s                 -> ("failed", last context)

Gated by AUTO_CLEAR_PENDING (default OFF): when off, the state runner
cancels with a "clear the pending transaction and retry" message instead.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from config import AUTO_CLEAR_PENDING

# from engine.canvas import wait_and_capture_canvas_png
from engine.log import StepLogger
from engine.steps import (
    cdp_eval,
    click_by_text,
    # current_url,
    fill,
    navigate,
    sleep_seconds,
    wait_for_selector,
)
from engine.types import RunContext, ScriptedAbort, StepLog, StepStatus

# from llm.vertex import ocr_image
# from redis_client import job_key
from lifecycle.status import Status
from .captcha_user import solve_captcha

# ─── Selectors (live HTML, CheckPost V4.7.x — state-agnostic SPA shell) ─
URL_PORTAL_HOME = "https://services.parivahan.gov.in/checkpostv4/#/"
SEL_PENDING_TX_LINK = 'a[href="#/public/payment/ChecklTransactionStatus"]'
SEL_PENDING_VEHICLE_INPUT = "input#inputVehicleNo"
SEL_PENDING_CAPTCHA_CONTAINER = "div#captcha"
SEL_PENDING_CAPTCHA_CANVAS = "div#captcha canvas"
SEL_PENDING_CAPTCHA_INPUT = "input#inputcaptcha"
# Refresh is a SIBLING of div#captcha (NOT a child).
SEL_PENDING_CAPTCHA_REFRESH = "div#captcha + button, button.btn-primary.m-left"
SEL_PENDING_GO_BTN = "button.go-but"

# ─── Tunables (main's values) ───────────────────────────────────────────
OWNER_OUTCOME_POLL_SECS = 30.0
OWNER_OUTCOME_POLL_TICK_SECS = 0.5
GO_SUBMIT_POLL_SECS = 10.0
BANK_OUTCOME_POLL_SECS = 30.0
BANK_OUTCOME_POLL_TICK_SECS = 0.5
BANK_OUTCOME_DEBUG_TICK_SECS = 3.0
MAX_CAPTCHA_ATTEMPTS = 5
LINK_WAIT_TIMEOUT_SECS = 20
PAGE_LOAD_TIMEOUT_SECS = 20


def auto_clear_enabled() -> bool:
    return AUTO_CLEAR_PENDING


# ─── JS probes (verbatim from main) ─────────────────────────────────────

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

_DISMISS_SWAL_JS = """
(function() {
  var popups = document.querySelectorAll('.swal2-popup');
  var clicked = 0;
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var btn = popups[i].querySelector('button.swal2-confirm');
    if (!btn) continue;
    try { btn.click(); clicked++; } catch (e) {}
  }
  return {dismissed: clicked > 0, count: clicked};
})()
"""

_GO_SUBMIT_STATE_JS = """
(function() {
  var popups = document.querySelectorAll('.swal2-popup');
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var text = (popups[i].textContent || '').toLowerCase();
    if (text.indexOf('captcha') !== -1 &&
        (text.indexOf('mismatch') !== -1
         || text.indexOf('invalid') !== -1
         || text.indexOf('correct captcha') !== -1
         || text.indexOf('wrong') !== -1)) {
      return {state: 'captcha_mismatch', text: popups[i].textContent.trim().substring(0, 200)};
    }
  }
  var rows = document.querySelectorAll('table.table-bordered tbody tr');
  if (rows && rows.length > 0) {
    var first = rows[0].querySelectorAll('td');
    if (first.length >= 6) {
      return {state: 'results', count: rows.length};
    }
  }
  return {state: 'pending'};
})()
"""

_CLICK_BANK_ICON_JS = """
(function() {
  var rows = document.querySelectorAll('table.table-bordered tbody tr');
  if (!rows || rows.length === 0) return {ok: false, reason: 'no_rows'};
  var icon = rows[0].querySelector('i.fa-university');
  if (!icon) return {ok: false, reason: 'no_bank_icon'};
  var td = icon.closest('td') || icon.parentElement;

  var attempts = [];
  try { td.click(); attempts.push('td.click'); }
  catch (e) { attempts.push('td.click_threw:' + (e.message || 'unknown')); }
  try { icon.click(); attempts.push('icon.click'); }
  catch (e) { attempts.push('icon.click_threw:' + (e.message || 'unknown')); }
  try {
    var evt = new MouseEvent('click', {bubbles: true, cancelable: true, view: window});
    td.dispatchEvent(evt);
    attempts.push('td.dispatchEvent');
  } catch (e) { attempts.push('td.dispatch_threw:' + (e.message || 'unknown')); }

  return {ok: true, attempts: attempts};
})()
"""

_BANK_OUTCOME_JS = """
(function() {
  var ctx = {url: window.location.href, popups: [], body_snippet: ''};
  var popups = document.querySelectorAll('.swal2-popup');
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var text = (popups[i].textContent || '').trim();
    ctx.popups.push(text.substring(0, 160));
    if (/try\\s*after/i.test(text)) {
      return {state: 'still_on_hold', hint: text.substring(0, 200), ctx: ctx};
    }
  }
  var url = ctx.url || '';
  if (url.indexOf('DoubleVerification') !== -1 || url.indexOf('/bank/') !== -1) {
    return {state: 'cleared', via: 'url', ctx: ctx};
  }
  var body = (document.body && document.body.innerText || '').toLowerCase();
  ctx.body_snippet = body.substring(0, 200);
  if (body.indexOf('transaction is fail') !== -1
      || body.indexOf('initiate transaction again') !== -1) {
    return {state: 'cleared', via: 'body_text', ctx: ctx};
  }
  return {state: 'pending', ctx: ctx};
})()
"""


# ─── Public: owner-info outcome polling ─────────────────────────────────


async def wait_for_owner_info_outcome(
    session,
    log: StepLogger,
    *,
    name: str = "p3.wait_owner_outcome",
    timeout: float = OWNER_OUTCOME_POLL_SECS,
    tick: float = OWNER_OUTCOME_POLL_TICK_SECS,
) -> str:
    """Poll after Get Details for one of: district_ready / pending_popup /
    validity_popup / timeout. Never raises — the caller branches. The popup
    text rides along in the StepLog so the run-log shows the routing."""
    started = time.monotonic()
    deadline = started + timeout
    last_state, last_text = "pending", ""

    while time.monotonic() < deadline:
        res = await cdp_eval(session, _OWNER_INFO_OUTCOME_JS)
        state = (res or {}).get("state", "pending")
        last_state = state
        last_text = (res or {}).get("text", "") or last_text

        if state == "district_ready":
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value="district_ready",
                )
            )
            return "district_ready"
        if state in ("pending_popup", "validity_popup"):
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.FAILED,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value=f"{state}: {last_text[:120]}",
                )
            )
            return state
        await asyncio.sleep(tick)

    log.record(
        StepLog(
            index=log.next_index(),
            name=name,
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            value=f"timeout (last_state={last_state})",
            error=f"no decisive outcome within {timeout:.0f}s",
        )
    )
    return "timeout"


def owner_outcome_popup_text(res_text: str) -> str:
    return res_text


# ─── Internals ──────────────────────────────────────────────────────────


async def _dismiss_all(ctx: RunContext, name: str) -> None:
    try:
        res = await cdp_eval(ctx.session, _DISMISS_SWAL_JS)
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name=name,
                status=StepStatus.OK,
                value=f"dismissed={res.get('count', 0) if res else 0}",
            )
        )
    except Exception:
        pass


async def _refresh_pending_captcha(ctx: RunContext) -> None:
    try:
        await cdp_eval(
            ctx.session,
            "(function(s){var e=document.querySelector(s);"
            "if(e) e.click(); return !!e;})("
            + json.dumps(SEL_PENDING_CAPTCHA_REFRESH)
            + ")",
        )
    except Exception:
        pass
    await asyncio.sleep(1.2)


async def _solve_pending_captcha(ctx: RunContext) -> None:
    """Solve the check-pending captcha under the global CAPTCHA_MODE. AI mode
    OCRs it silently (no DB write, no captcha status); human / AI-fallback shows
    it as pendingTransactionCaptcha. Raises ScriptedAbort(cancelled) if it
    can't be solved (nothing irreversible has happened)."""

    async def _submit() -> bool:
        # Click Go, then poll the submit for the results table.
        try:
            await cdp_eval(
                ctx.session,
                "(function(s){var e=document.querySelector(s);"
                "if(e){e.click();return true;}return false;})("
                + json.dumps(SEL_PENDING_GO_BTN)
                + ")",
            )
        except Exception as e:
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="pending.click_go",
                    status=StepStatus.FAILED,
                    error=f"{type(e).__name__}: {e}",
                )
            )
            return False

        deadline = time.monotonic() + GO_SUBMIT_POLL_SECS
        while time.monotonic() < deadline:
            res = await cdp_eval(ctx.session, _GO_SUBMIT_STATE_JS)
            state = (res or {}).get("state", "pending")
            if state == "results":
                ctx.log.record(
                    StepLog(
                        index=ctx.log.next_index(),
                        name="pending.go_submit",
                        status=StepStatus.OK,
                        value=f"results rows={res.get('count')}",
                    )
                )
                return True
            if state == "captcha_mismatch":
                return False
            await asyncio.sleep(0.5)
        return False  # neither results nor mismatch -> treat as not advanced

    async def _rejected() -> bool:
        res = await cdp_eval(ctx.session, _GO_SUBMIT_STATE_JS)
        return (res or {}).get("state") == "captcha_mismatch"

    await solve_captcha(
        ctx,
        status=Status.PENDING_TRANSACTION_CAPTCHA,
        stage="pending",
        input_selector=SEL_PENDING_CAPTCHA_INPUT,
        refresh_selector=SEL_PENDING_CAPTCHA_REFRESH,
        submit_action=_submit,
        is_rejected=_rejected,
        canvas_scope=SEL_PENDING_CAPTCHA_CANVAS,
    )


# ─── Public: pending-tx clear flow ──────────────────────────────────────


async def clear_pending_transaction(ctx: RunContext) -> tuple[str, str]:
    """Returns (outcome, hint): outcome in {cleared, still_on_hold, failed}."""
    p = ctx.params

    await _dismiss_all(ctx, "pending.dismiss_blocker")

    # Direct deep links get rewritten to "/" by the router — home + click.
    await navigate(
        ctx.session,
        URL_PORTAL_HOME,
        log=ctx.log,
        name="pending.home",
        timeout=PAGE_LOAD_TIMEOUT_SECS + 25,
    )
    await sleep_seconds(1.5, log=ctx.log, name="pending.home_settle")

    try:
        await wait_for_selector(
            ctx.session,
            SEL_PENDING_TX_LINK,
            log=ctx.log,
            name="pending.wait_check_link",
            timeout=LINK_WAIT_TIMEOUT_SECS,
        )
        await cdp_eval(
            ctx.session,
            "(function(s){var e=document.querySelector(s);"
            "if(e){e.click(); return true;} return false;})("
            + json.dumps(SEL_PENDING_TX_LINK)
            + ")",
        )
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="pending.open_check",
                status=StepStatus.OK,
                selector=SEL_PENDING_TX_LINK,
            )
        )
    except ScriptedAbort:
        # Some shell builds hide the routerLink behind the dropdown toggle.
        try:
            await click_by_text(
                ctx.session,
                "Check Pending Transaction",
                log=ctx.log,
                name="pending.open_check_text",
                tag="a",
                timeout=8,
            )
        except ScriptedAbort:
            return ("failed", "could not open Check Pending Transaction")

    try:
        await wait_for_selector(
            ctx.session,
            SEL_PENDING_VEHICLE_INPUT,
            log=ctx.log,
            name="pending.wait_check_page",
            timeout=PAGE_LOAD_TIMEOUT_SECS,
        )
    except ScriptedAbort:
        return ("failed", "Check Pending Transaction page did not load")

    await fill(
        ctx.session,
        SEL_PENDING_VEHICLE_INPUT,
        p.vehicleNumber,
        log=ctx.log,
        name="pending.fill_vehicle",
    )

    await _solve_pending_captcha(ctx)
    if ctx.reporter.current == Status.PENDING_TRANSACTION_CAPTCHA:
        await ctx.reporter.set_status(Status.PENDING_TRANSACTION)

    # if not await _solve_pending_captcha(ctx):
    #     return ("failed", f"captcha not accepted after {MAX_CAPTCHA_ATTEMPTS} attempts")

    res = await cdp_eval(ctx.session, _CLICK_BANK_ICON_JS)
    if not res or not res.get("ok"):
        return ("failed", f"bank icon: {(res or {}).get('reason', 'click failed')}")
    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name="pending.click_bank_icon",
            status=StepStatus.OK,
            value=",".join(res.get("attempts", [])),
        )
    )

    # Outcome poll with periodic debug context (gov portal is slow).
    started = time.monotonic()
    last_debug = 0.0
    last_ctx = {}
    while time.monotonic() - started < BANK_OUTCOME_POLL_SECS:
        res = await cdp_eval(ctx.session, _BANK_OUTCOME_JS)
        state = (res or {}).get("state", "pending")
        last_ctx = (res or {}).get("ctx", {}) or last_ctx

        if state == "still_on_hold":
            hint = str((res or {}).get("hint", "")).strip()
            m = re.search(r"try\s*after\s*(.+)", hint, re.IGNORECASE)
            return ("still_on_hold", (m.group(1).strip() if m else hint))
        if state == "cleared":
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="pending.cleared",
                    status=StepStatus.OK,
                    value=f"via={res.get('via')}",
                )
            )
            return ("cleared", "")

        now = time.monotonic()
        if now - last_debug >= BANK_OUTCOME_DEBUG_TICK_SECS:
            last_debug = now
            print(
                f"[pending_clear] poll t={now - started:.0f}s "
                f"url={last_ctx.get('url', '?')} "
                f"popups={last_ctx.get('popups', [])}"
            )
        await asyncio.sleep(BANK_OUTCOME_POLL_TICK_SECS)

    return (
        "failed",
        f"no decisive clear signal within {BANK_OUTCOME_POLL_SECS:.0f}s "
        f"(last url={last_ctx.get('url', '?')})",
    )
