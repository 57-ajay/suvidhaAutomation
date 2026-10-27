# worker/src/scripted/disclaimer_captcha.py
"""Step-4 Disclaimer captcha, AI-solved right BEFORE the human handover —
SCRIPTED (web) path.

The wizard ends on the Disclaimer page: a canvas captcha (input#inputcap),
the "I confirm" checkbox and Pay Online. The operator used to start there;
now the worker clears that page first, the way the fully-automated path's
disclaimer_captcha phase does — its proven selectors, checkbox probe and
submit sequence, copied (module isolation over DRY, by decision):

  capture the VISIBLE canvas (paint-wait; the page also keeps a hidden 0x0
  <canvas id="captcha">) -> Vertex OCR -> fill input#inputcap -> tick
  "I confirm" (guarded: never un-ticks) -> close the "Receipt valid" note ->
  Pay Online -> "Are you sure?" Yes -> poll for the payment gateway

so the operator only picks the payment method and pays. Wrong code (a
captcha popup) -> dismiss, refresh, read again, up to MAX_AI_CAPTCHA_ATTEMPTS.

solve_before_handover() returns how far it got; handover.py words the
operator's instructions from it:

  "gateway"     solved — Pay Online -> Yes done, the gateway loaded
  "submitted"   Yes was clicked but no decisive signal followed: handed over
                AS-IS and never retried (a payment request may be in flight)
  "unsolved"    reads spent / capture failed / budget or error: the operator
                solves the captcha on the live view, exactly as before
  "not_found"   no captcha on screen: handed over as-is
  "disabled"    SCRIPTED_SOLVE_DISCLAIMER_CAPTCHA=false: the old flow exactly

Popups are only ever closed with OK (plain notices) or No/Cancel (two-button
dialogs); the one "Yes" the worker presses is the explicit confirm click.

Nothing here can fail the run: every exit is a handover, and the whole solve
is capped at SOLVE_BUDGET_SECS (handover.py adds it to the phase deadline, so
the operator's WEB_HANDOVER_TIMEOUT_SECS window is untouched).

Money line: everything up to Yes is pre-payment, the same as the auto path's
disclaimer phase; status stays aiAgentStarted until handover.py sets
humanHandover right after this returns.
"""

from __future__ import annotations

import asyncio
import json
import time

from config import (
    CAPTCHA_DEBUG_DIR,
    MAX_AI_CAPTCHA_ATTEMPTS,
    SCRIPTED_SOLVE_DISCLAIMER_CAPTCHA,
)
from engine.canvas import wait_and_capture_canvas_png
from engine.steps import (
    cdp_eval,
    click_by_text,
    current_url,
    fill,
    read_popup_state,
)
from engine.types import RunContext, ScriptedAbort, StepLog, StepStatus
from llm.vertex import ocr_image

# ─── Selectors (the auto path's, confirmed live; same SPA shell) ────────
SEL_CAPTCHA_INPUT = "input#inputcap"
SEL_CAPTCHA_REFRESH = "button[data-bs-original-title*='generate']"
GATEWAY_URL_MARKERS = ["etranspgi", "vahanpgi", "paymentgateway"]

# ─── Tunables ───────────────────────────────────────────────────────────
SUBMIT_SETTLE_SECS = 15.0  # "Yes" -> eTransPgi redirect is slow/variable
SOLVE_BUDGET_SECS = 240.0  # the whole solve; past it -> hand over as-is

_LOG = "p6.captcha"

# Verbatim from tasks/border_tax/states/up.py (proven on the live page).
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

# LAST VISIBLE refresh button: a closed Get-Details captcha box still in the
# DOM can carry a matching tooltip title, so a first-match pick could hit it.
_REFRESH_JS = (
    "(function(s){"
    "var a=Array.prototype.slice.call(document.querySelectorAll(s))"
    "  .filter(function(e){var r=e.getBoundingClientRect();"
    "                      return r.width>0&&r.height>0;});"
    "if(!a.length) return false;"
    "a[a.length-1].click(); return true;"
    "})(" + json.dumps(SEL_CAPTCHA_REFRESH) + ")"
)

# The ONLY way this module closes a popup: OK on a plain notice, the first
# VISIBLE No/Cancel on a two-button dialog — never a two-button dialog's
# primary button, which after Pay Online means "yes, go ahead and pay". (The
# generic engine.steps.dismiss_popup presses swal2-confirm on anything; and
# SweetAlert2 keeps a hidden Deny button ahead of Cancel in the DOM, so the
# visibility filter is load-bearing.) The single deliberate "Yes" is the
# explicit click_by_text in _submit.
_SAFE_DISMISS_JS = (
    "(function(){"
    "function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}"
    "var n=0, p=document.querySelectorAll('.swal2-popup');"
    "for(var i=0;i<p.length;i++){"
    "  if(!vis(p[i])) continue;"
    "  var alt=Array.prototype.slice.call("
    "    p[i].querySelectorAll('button.swal2-cancel, button.swal2-deny')).filter(vis);"
    "  var b=alt.length ? alt[0] : p[i].querySelector('button.swal2-confirm');"
    "  if(b && vis(b)){try{b.click();n++;}catch(e){}}"
    "}"
    "return n;})()"
)

_INPUT_VISIBLE_JS = (
    "(function(s){var e=document.querySelectorAll(s);"
    "for(var i=0;i<e.length;i++){var r=e[i].getBoundingClientRect();"
    "  if(r.width>0&&r.height>0) return true;}"
    "return false;})(" + json.dumps(SEL_CAPTCHA_INPUT) + ")"
)


# ─── Public ─────────────────────────────────────────────────────────────


async def solve_before_handover(ctx: RunContext) -> str:
    """Clear the Disclaimer page before the operator takes over. Returns
    "gateway" / "submitted" / "unsolved" / "not_found" / "disabled" (see
    module docstring). Never raises — the old handover is always the
    fallback."""
    if not SCRIPTED_SOLVE_DISCLAIMER_CAPTCHA:
        _log(ctx, "skipped", StepStatus.OK, value="SCRIPTED_SOLVE_DISCLAIMER_CAPTCHA=false")
        return "disabled"
    try:
        return await asyncio.wait_for(_solve(ctx), timeout=SOLVE_BUDGET_SECS)
    except asyncio.TimeoutError:
        _log(
            ctx,
            "budget",
            StepStatus.HANDED_OFF,
            error=f"not solved within {SOLVE_BUDGET_SECS:.0f}s — handing over",
        )
        return "unsolved"
    except Exception as e:  # anything at all: hand over as before
        _log(
            ctx,
            "error",
            StepStatus.HANDED_OFF,
            error=f"{type(e).__name__}: {e}"[:300],
        )
        return "unsolved"


# ─── Internals ──────────────────────────────────────────────────────────


def _log(ctx: RunContext, name: str, status: StepStatus, **kw) -> None:
    ctx.log.record(
        StepLog(index=ctx.log.next_index(), name=f"{_LOG}.{name}", status=status, **kw)
    )


async def _solve(ctx: RunContext) -> str:
    try:
        on_page = bool(await cdp_eval(ctx.session, _INPUT_VISIBLE_JS))
    except Exception:
        on_page = False
    if not on_page:
        _log(ctx, "not_found", StepStatus.HANDED_OFF, error="no captcha input on screen")
        return "not_found"

    attempts = max(1, MAX_AI_CAPTCHA_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        b64 = await _capture(ctx)
        if b64 is None:
            _log(
                ctx,
                "unsolved",
                StepStatus.HANDED_OFF,
                attempt=attempt,
                error="captcha canvas could not be captured",
            )
            return "unsolved"

        text, cost = await ocr_image(b64)
        text = (text or "").strip()
        readable = bool(text) and text != "UNREADABLE"
        _log(
            ctx,
            "read",
            StepStatus.OK if readable else StepStatus.RETRIED,
            attempt=attempt,
            value=text,
            ai_cost_usd=cost,
        )
        if not readable:
            await _refresh(ctx)
            continue

        try:
            await fill(
                ctx.session,
                SEL_CAPTCHA_INPUT,
                text,
                log=ctx.log,
                name=f"{_LOG}.fill_{attempt}",
            )
        except ScriptedAbort:
            await _refresh(ctx)
            continue

        verdict, detail = await _submit(ctx, attempt)
        if verdict == "gateway":
            _log(ctx, "accepted", StepStatus.OK, attempt=attempt, value=detail)
            return "gateway"
        if verdict == "submitted":
            _log(ctx, "submitted", StepStatus.HANDED_OFF, attempt=attempt, error=detail)
            return "submitted"
        # rejected / not_advanced: nothing was initiated — fresh code, again.
        _log(ctx, "rejected", StepStatus.RETRIED, attempt=attempt, value=text, error=detail)
        await _safe_dismiss(ctx, f"dismiss_{attempt}")
        await _refresh(ctx)

    # Spent: leave a clean field for the operator (best effort).
    try:
        await fill(ctx.session, SEL_CAPTCHA_INPUT, "", log=ctx.log, name=f"{_LOG}.clear")
    except ScriptedAbort:
        pass
    _log(
        ctx,
        "unsolved",
        StepStatus.HANDED_OFF,
        error=f"{attempts} read(s), none accepted — the operator solves it",
    )
    return "unsolved"


async def _capture(ctx: RunContext) -> str | None:
    """The auto path's capture discipline: wait for the VISIBLE canvas to
    paint (whole-document probe — the hidden 0x0 #captcha canvas is filtered
    out), refreshing at most once on a blank."""
    await asyncio.sleep(1.0)  # let the section settle before the first probe
    for round_no in (1, 2):
        b64, detail = await wait_and_capture_canvas_png(
            ctx.session, timeout=6.0, tick=0.6, scope_selector="canvas"
        )
        if b64:
            _debug_dump(ctx, b64)
            _log(ctx, "capture", StepStatus.OK, attempt=round_no, value=detail)
            return b64
        _log(ctx, "capture", StepStatus.RETRIED, attempt=round_no, error=detail)
        if round_no == 1:
            await _refresh(ctx)
    return None


async def _safe_dismiss(ctx: RunContext, name: str) -> None:
    try:
        n = await cdp_eval(ctx.session, _SAFE_DISMISS_JS)
        if n:
            _log(ctx, name, StepStatus.OK, value=f"closed={n}")
        await asyncio.sleep(0.5)
    except Exception:
        pass


async def _refresh(ctx: RunContext) -> None:
    try:
        await cdp_eval(ctx.session, _REFRESH_JS)
    except Exception:
        pass
    await asyncio.sleep(1.0)


async def _submit(ctx: RunContext, attempt: int) -> tuple[str, str]:
    """The auto path's submit sequence. Returns (verdict, detail):
      "gateway"        gateway URL reached / the captcha left the page
      "rejected"       a captcha popup — nothing initiated, safe to retry
      "not_advanced"   Yes never appeared and nothing decisive — safe to retry
      "submitted"      Yes WAS clicked, nothing decisive — never retried"""
    try:
        res = await cdp_eval(ctx.session, _TICK_CONFIRM_CHECKBOX_JS)
        _log(
            ctx,
            f"confirm_checkbox_{attempt}",
            StepStatus.OK if res and res.get("ok") else StepStatus.RETRIED,
            value=str(res),
        )
    except Exception:
        pass
    # Ticking can raise the "Receipt valid for X Days" summary popup.
    await asyncio.sleep(0.8)
    await _safe_dismiss(ctx, f"close_validity_note_{attempt}")

    try:
        await click_by_text(
            ctx.session,
            "Pay Online",
            log=ctx.log,
            name=f"{_LOG}.pay_online_{attempt}",
            tag="button",
            timeout=10,
        )
    except ScriptedAbort:
        return "not_advanced", "Pay Online button not found"
    await asyncio.sleep(2.0)

    # "Are you sure? You want to pay online?" -> Yes.
    yes = False
    try:
        await click_by_text(
            ctx.session,
            "Yes",
            log=ctx.log,
            name=f"{_LOG}.confirm_yes_{attempt}",
            tag="button",
            timeout=4,
        )
        yes = True
    except ScriptedAbort:
        pass

    deadline = time.monotonic() + SUBMIT_SETTLE_SECS
    while time.monotonic() < deadline:
        url = (await current_url(ctx.session)).lower()
        if any(g in url for g in GATEWAY_URL_MARKERS):
            return "gateway", f"url={url[:120]}"
        try:
            gone = await cdp_eval(
                ctx.session,
                "!document.querySelector(" + json.dumps(SEL_CAPTCHA_INPUT) + ")",
            )
        except Exception:
            gone = False  # mid-navigation: the next tick reads the new page
        if gone:
            return "gateway", "the captcha left the page"
        st = await read_popup_state(ctx.session)
        if st.get("present"):
            ptxt = str(st.get("text") or "")
            if "captcha" in ptxt.lower():
                return "rejected", ptxt[:200]
            if not yes:
                # A slow "Are you sure?" confirm: answer it now.
                try:
                    await click_by_text(
                        ctx.session,
                        "Yes",
                        log=ctx.log,
                        name=f"{_LOG}.confirm_yes_late_{attempt}",
                        tag="button",
                        timeout=1,
                    )
                    yes = True
                    continue
                except ScriptedAbort:
                    pass
                # Something else came up (a required field, a portal note,
                # a confirm dialog we don't recognise): nothing was initiated.
                # Close it safely — No/Cancel, never its primary button.
                await _safe_dismiss(ctx, f"dismiss_unknown_{attempt}")
                return "not_advanced", ptxt[:200] or "popup with no text"
        await asyncio.sleep(0.5)
    if yes:
        return "submitted", f"no gateway within {SUBMIT_SETTLE_SECS:.0f}s after Yes"
    return "not_advanced", "no confirm dialog after Pay Online"


def _debug_dump(ctx: RunContext, b64: str) -> None:
    """CAPTCHA_DEBUG_DIR (debug only): keep exactly what the model was sent."""
    if not CAPTCHA_DEBUG_DIR:
        return
    try:
        import base64
        import os

        os.makedirs(CAPTCHA_DEBUG_DIR, exist_ok=True)
        path = os.path.join(
            CAPTCHA_DEBUG_DIR,
            f"disclaimer_captcha_{ctx.job_id}_{int(time.time() * 1000)}.png",
        )
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        _log(ctx, "debug_dump", StepStatus.OK, value=path)
    except Exception:
        pass
