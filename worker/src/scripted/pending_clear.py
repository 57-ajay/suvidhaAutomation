# worker/src/scripted/pending_clear.py
"""Pending-transaction auto-clear for the SCRIPTED (web) path.

Deliberate copy of tasks/border_tax/pending_clear.py adapted for this path
(module isolation over DRY, by decision): same portal flow — home ->
"Check Pending Transaction" -> vehicle number -> captcha -> Go -> first
row's bank icon -> outcome poll — with ONE deliberate difference: the page
captcha is solved by AI ONLY (Vertex OCR, silently, up to
MAX_AI_CAPTCHA_ATTEMPTS). The scripted path's human seam is the web
operator on the live view, not the app user, so the fully-automated path's
save-captcha/app fallback would show the captcha to the wrong audience.
If OCR never gets through, the clear reports ("failed", ...) and the
wizard's pending budget decides: retry the whole clear on a fresh pass or
abort with the manual-clear instruction.

Outcomes (same semantics as the fully-automated module):
  "Please try after X Minutes"                  -> ("still_on_hold", hint)
  URL DoubleVerification|/bank/ or body says
  "transaction is fail"|"initiate transaction
  again"                                        -> ("cleared", "")
  nothing decisive in BANK_OUTCOME_POLL_SECS    -> ("failed", last context)

Gated by SCRIPTED_AUTO_CLEAR_PENDING (default ON): when off, the wizard
keeps the old behavior — abort with the "clear it via 'Check Pending
Transaction', then retry" operator message.

Money line: the clear runs strictly pre-payment (it either voids the stale
transaction or does nothing), so every failure here stays terminal
"cancelled" — retryable, no money moved.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from config import MAX_AI_CAPTCHA_ATTEMPTS, SCRIPTED_AUTO_CLEAR_PENDING
from engine.canvas import wait_and_capture_canvas_png
from engine.steps import (
    cdp_eval,
    click_by_text,
    fill,
    navigate,
    sleep_seconds,
    wait_for_selector,
)
from engine.types import RunContext, ScriptedAbort, StepLog, StepStatus
from llm.vertex import ocr_image

# ─── Selectors (live HTML, CheckPost V4.7.x — state-agnostic SPA shell) ─
URL_PORTAL_HOME = "https://services.parivahan.gov.in/checkpostv4/#/"
SEL_PENDING_TX_LINK = 'a[href="#/public/payment/ChecklTransactionStatus"]'
SEL_PENDING_VEHICLE_INPUT = "input#inputVehicleNo"
SEL_PENDING_CAPTCHA_CANVAS = "div#captcha canvas"
SEL_PENDING_CAPTCHA_INPUT = "input#inputcaptcha"
# Refresh is a SIBLING of div#captcha (NOT a child).
SEL_PENDING_CAPTCHA_REFRESH = "div#captcha + button, button.btn-primary.m-left"
SEL_PENDING_GO_BTN = "button.go-but"

# ─── Tunables (the fully-automated module's values) ─────────────────────
GO_SUBMIT_POLL_SECS = 10.0
BANK_OUTCOME_POLL_SECS = 30.0
BANK_OUTCOME_POLL_TICK_SECS = 0.5
BANK_OUTCOME_DEBUG_TICK_SECS = 3.0
LINK_WAIT_TIMEOUT_SECS = 20
PAGE_LOAD_TIMEOUT_SECS = 20


def auto_clear_enabled() -> bool:
    return SCRIPTED_AUTO_CLEAR_PENDING


# ─── JS probes (verbatim from tasks/border_tax/pending_clear.py) ────────

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


async def _refresh_captcha(ctx: RunContext) -> None:
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


async def _capture_captcha(ctx: RunContext) -> str | None:
    """Wait for the visible captcha canvas to PAINT, refreshing at most once
    on a blank — the same capture discipline as the fully-automated path."""
    await asyncio.sleep(1.0)  # let the section mount before the first probe
    for round_no in (1, 2):
        b64, detail = await wait_and_capture_canvas_png(
            ctx.session,
            timeout=6.0,
            tick=0.6,
            scope_selector=SEL_PENDING_CAPTCHA_CANVAS,
        )
        if b64:
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="pending.captcha_capture",
                    status=StepStatus.OK,
                    attempt=round_no,
                    value=detail,
                )
            )
            return b64
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="pending.captcha_capture",
                status=StepStatus.RETRIED,
                attempt=round_no,
                error=detail,
            )
        )
        if round_no == 1:
            await _refresh_captcha(ctx)
    return None


async def _click_go(ctx: RunContext) -> bool:
    try:
        await cdp_eval(
            ctx.session,
            "(function(s){var e=document.querySelector(s);"
            "if(e){e.click();return true;}return false;})("
            + json.dumps(SEL_PENDING_GO_BTN)
            + ")",
        )
        return True
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


async def _go_outcome(ctx: RunContext) -> str:
    """Poll after Go for the results table or a captcha-mismatch popup.
    Returns 'results' / 'captcha_mismatch' / 'pending' (no decisive signal).
    A failed eval is a 'nothing yet' tick, never an error — the page may be
    re-rendering under the poll."""
    deadline = time.monotonic() + GO_SUBMIT_POLL_SECS
    while time.monotonic() < deadline:
        try:
            res = await cdp_eval(ctx.session, _GO_SUBMIT_STATE_JS)
        except Exception:
            await asyncio.sleep(0.5)
            continue
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
            return "results"
        if state == "captcha_mismatch":
            return "captcha_mismatch"
        await asyncio.sleep(0.5)
    return "pending"


async def _solve_captcha_ai(ctx: RunContext) -> bool:
    """Silent Vertex-OCR attempts against the check-pending captcha; no image
    upload, no captcha status — nobody is shown a captcha the AI is solving.
    Returns True once Go produced the results table, False when the OCR
    budget (MAX_AI_CAPTCHA_ATTEMPTS) is exhausted or capture failed."""
    for attempt in range(1, MAX_AI_CAPTCHA_ATTEMPTS + 1):
        b64 = await _capture_captcha(ctx)
        if b64 is None:
            return False

        text, cost = await ocr_image(b64)
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="pending.captcha_ai_ocr",
                status=StepStatus.OK if text != "UNREADABLE" else StepStatus.RETRIED,
                attempt=attempt,
                value=text,
                ai_cost_usd=cost,
            )
        )
        if text == "UNREADABLE":
            await _refresh_captcha(ctx)
            continue

        try:
            await fill(
                ctx.session,
                SEL_PENDING_CAPTCHA_INPUT,
                text,
                log=ctx.log,
                name=f"pending.captcha_ai_fill_{attempt}",
            )
        except ScriptedAbort:
            # Input not fillable this instant — burn the attempt, not the run.
            await _refresh_captcha(ctx)
            continue
        if await _click_go(ctx) and await _go_outcome(ctx) == "results":
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="pending.captcha_ai_accepted",
                    status=StepStatus.OK,
                    attempt=attempt,
                )
            )
            return True

        # Click failure, mismatch, or nothing decisive: one burned attempt —
        # clear any popup, refresh, try again (the twin's policy).
        await _dismiss_all(ctx, f"pending.captcha_ai_dismiss_{attempt}")
        await _refresh_captcha(ctx)

    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name="pending.captcha_ai_exhausted",
            status=StepStatus.FAILED,
            error=f"{MAX_AI_CAPTCHA_ATTEMPTS} OCR attempts failed",
        )
    )
    return False


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

    if not await _solve_captcha_ai(ctx):
        return (
            "failed",
            f"the captcha was not accepted within {MAX_AI_CAPTCHA_ATTEMPTS} "
            "AI attempts",
        )

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
    last_ctx: dict = {}
    while time.monotonic() - started < BANK_OUTCOME_POLL_SECS:
        try:
            res = await cdp_eval(ctx.session, _BANK_OUTCOME_JS)
        except Exception:
            # The "cleared" signal IS a navigation (URL gains /bank/ or
            # DoubleVerification) — evaluating mid-navigation can throw while
            # the context is torn down. That's evidence the poll should keep
            # going, never an error.
            await asyncio.sleep(BANK_OUTCOME_POLL_TICK_SECS)
            continue
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
                f"[scripted.pending_clear] poll t={now - started:.0f}s "
                f"url={last_ctx.get('url', '?')} "
                f"popups={last_ctx.get('popups', [])}"
            )
        await asyncio.sleep(BANK_OUTCOME_POLL_TICK_SECS)

    return (
        "failed",
        f"no decisive clear signal within {BANK_OUTCOME_POLL_SECS:.0f}s "
        f"(last url={last_ctx.get('url', '?')})",
    )
