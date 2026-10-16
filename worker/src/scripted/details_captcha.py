# worker/src/scripted/details_captcha.py
"""The portal's Get-Details "Verify Captcha" box — SCRIPTED (web) path.

Get Details no longer goes straight to VAHAN: the portal first opens a
"Verify Captcha" modal, and only after a correct code does it fetch the RC
(the district select, or its usual pending / validity / no-data popups).
The box, from the live DOM:

  div.captcha-box img      the code: an <img> with a base64 PNG src (NOT a
                           canvas — same capture path as HP's himkosh image)
  button.captcha-refresh   a new code; the portal allows only 2 refreshes
  input#inputVahanCap      the answer (maxlength 8)
  OK / Close               OK submits

Observed on the live portal: a WRONG code raises an error popup and the SAME
box then shows a NEW code by itself. So a rejection never costs a refresh;
the refresh button is spent only when there is no fresh code to read (an
unreadable image, or no new code after a verdict) — at most twice per pass,
the portal's own limit.

Solving is AI-only and silent, by decision (the operator only takes over at
the disclaimer): capture_element_png (upscaled onto white) -> Vertex OCR ->
fill -> OK, judged by what the PORTAL does, never by OCR confidence:

  box closes / district renders /       accepted — the caller's outcome poll
  a known outcome popup (pending,       takes over and routes the popup
  validity, no data, chassis)           exactly as before
  error popup over the box, or the      rejected — dismiss it, read the
  code swapped without a popup          fresh code the portal put up

Every read is fingerprinted (hash of the img src) and re-checked right
before OK, so an answer is never submitted against a code that has since
been swapped, and no code is ever read twice.

Budget: MAX_AI_CAPTCHA_ATTEMPTS reads per wizard pass. Spent -> restart the
whole wizard from open_portal (a fresh page = a fresh box and refresh quota)
on this module's own counter — SCRIPTED_POPUP_MAX_ATTEMPTS total passes, the
same size as the popup / stall / pending budgets — then cancel.

Deliberate twin of tasks/border_tax/details_captcha.py (module isolation over
DRY, by decision): same selectors, probe and loop; only the pass-budget knob
differs.

Money line: strictly pre-payment; every exit is a retryable "cancelled".
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import NoReturn

from config import (
    CAPTCHA_DEBUG_DIR,
    MAX_AI_CAPTCHA_ATTEMPTS,
    SCRIPTED_POPUP_MAX_ATTEMPTS,
)
from engine.canvas import capture_element_png
from engine.steps import cdp_eval, fill
from engine.types import RestartFrom, RunContext, ScriptedAbort, StepLog, StepStatus
from llm.vertex import ocr_image

# ─── Selectors (live DOM of the Get-Details "Verify Captcha" box) ───────
SEL_CAPTCHA_INPUT = "input#inputVahanCap"
SEL_CAPTCHA_IMG = "div.captcha-box img"
SEL_CAPTCHA_REFRESH = "button.captcha-refresh"
SEL_DISTRICT = "select#floatingDistrict"  # renders only once the RC is fetched

# ─── Tunables ───────────────────────────────────────────────────────────
MAX_REFRESHES_PER_PASS = 2  # the box's own note: "Only 2 refreshes are allowed"
FRESH_CODE_WAIT_SECS = 10.0  # a code to load, or the portal to swap it after a verdict
VERDICT_WAIT_SECS = 30.0  # OK -> verdict: the window Get Details always had
OK_BUTTON_WAIT_SECS = 5.0
POLL_TICK_SECS = 0.4
CAPTURE_TIMEOUT_SECS = 10.0
OCR_SCALE = 4  # the same upscale the canvas captcha probe applies
ANSWER_MIN_LEN, ANSWER_MAX_LEN = 4, 8  # input maxlength=8; live codes are 6

_LOG = "p3.captcha"

# One probe drives the whole loop. Popups are classified with the SAME rules
# and order as the owner-info outcome poll, so whatever this module hands
# back as "the portal moved on" is decisive there too:
#   captcha   mentions "captcha" (checked FIRST: "Captcha expired" must never
#             read as a validity blocker), or ANY other popup while the box
#             is still open — the portal's verdict on our code, in wording we
#             haven't seen yet
#   outcome   pending / insurance|fitness|pucc|expired|renew / no data /
#             chassis — the caller's poll owns these
#   other     unknown text with the box already gone — the caller's business
# fp is a hash of the img src (a base64 data URL, unique per code).
_PROBE_JS = (
    "(function(inSel, imgSel, distSel){"
    "function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}"
    "function lastVis(s){"
    "  var a=Array.prototype.slice.call(document.querySelectorAll(s)).filter(vis);"
    "  return a.length ? a[a.length-1] : null;}"
    "var out={open:false, loaded:false, fp:null, district:!!lastVis(distSel),"
    "         popup:null, kind:null};"
    "if(lastVis(inSel)){"
    "  out.open=true;"
    "  var img=lastVis(imgSel);"
    "  if(img){"
    "    out.loaded=!!(img.complete && img.naturalWidth>0);"
    "    var s=img.currentSrc||img.src||'', h=5381;"
    "    for(var i=0;i<s.length;i++){h=((h<<5)+h+s.charCodeAt(i))|0;}"
    "    out.fp=s.length+':'+(h>>>0).toString(16);"
    "  }"
    "}"
    "var pops=document.querySelectorAll('.swal2-popup');"
    "for(var j=pops.length-1;j>=0;j--){"
    "  if(!vis(pops[j])) continue;"
    "  var p=pops[j], l=(p.textContent||'').toLowerCase();"
    "  var tt=p.querySelector('.swal2-title'),"
    "      hc=p.querySelector('#swal2-html-container');"
    "  out.popup=(((tt&&tt.innerText)||'')+' '+((hc&&hc.innerText)||'')).trim()"
    "            || (p.innerText||'');"
    "  if(l.indexOf('captcha')!==-1) out.kind='captcha';"
    "  else if(l.indexOf('pending for verification')!==-1"
    "          || (l.indexOf('check pending transaction')!==-1"
    "              && l.indexOf('pending')!==-1)) out.kind='outcome';"
    "  else if(['insurance','fitness','pucc','expired','renew'].some("
    "          function(w){return l.indexOf(w)!==-1;})) out.kind='outcome';"
    "  else if(l.indexOf('no data found')!==-1"
    "          || l.indexOf('chassis')!==-1) out.kind='outcome';"
    "  else out.kind = out.open ? 'captcha' : 'other';"
    "  break;"
    "}"
    "return out;"
    "})(" + json.dumps(SEL_CAPTCHA_INPUT) + ", " + json.dumps(SEL_CAPTCHA_IMG)
    + ", " + json.dumps(SEL_DISTRICT) + ")"
)

# SweetAlert confirm buttons ONLY. engine.steps.dismiss_popup falls back to
# '.modal-footer button' when no SweetAlert is up — on this page that could
# hit the captcha box's own buttons, so it is never used here.
_DISMISS_SWAL_JS = (
    "(function(){var n=0, p=document.querySelectorAll('.swal2-popup');"
    "for(var i=0;i<p.length;i++){"
    "  var r=p[i].getBoundingClientRect();"
    "  if(r.width<=0||r.height<=0) continue;"
    "  var b=p[i].querySelector('button.swal2-confirm');"
    "  if(b){try{b.click();n++;}catch(e){}}"
    "}"
    "return n;})()"
)

# The box's OK: the nearest ancestor of the answer input that holds a visible
# button reading exactly "OK" (a SweetAlert's OK lives under <body>, outside
# the box, so the walk stops short of it); the portal's own button title is
# the fallback.
_CLICK_OK_JS = (
    "(function(inSel){"
    "function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}"
    "var ins=Array.prototype.slice.call(document.querySelectorAll(inSel)).filter(vis);"
    "if(!ins.length) return {ok:false, reason:'answer input not visible'};"
    "for(var n=ins[ins.length-1].parentElement; n && n!==document.body;"
    "    n=n.parentElement){"
    "  var bs=Array.prototype.slice.call(n.querySelectorAll('button'))"
    "    .filter(function(b){return vis(b)"
    "      && (b.innerText||'').trim().toUpperCase()==='OK';});"
    "  if(bs.length){bs[bs.length-1].click(); return {ok:true, via:'box'};}"
    "}"
    "var t=Array.prototype.slice.call(document.querySelectorAll("
    "  \"button[title*='submit the captcha' i]\")).filter(vis);"
    "if(t.length){t[t.length-1].click(); return {ok:true, via:'title'};}"
    "return {ok:false, reason:'OK button not found'};"
    "})(" + json.dumps(SEL_CAPTCHA_INPUT) + ")"
)

_CLICK_REFRESH_JS = (
    "(function(s){"
    "var a=Array.prototype.slice.call(document.querySelectorAll(s))"
    "  .filter(function(e){var r=e.getBoundingClientRect();"
    "                      return r.width>0&&r.height>0;});"
    "if(!a.length) return false;"
    "a[a.length-1].click(); return true;"
    "})(" + json.dumps(SEL_CAPTCHA_REFRESH) + ")"
)


# ─── Public ─────────────────────────────────────────────────────────────


def reset_pass(ctx: RunContext) -> None:
    """Fresh read / refresh counters for this wizard pass. owner_info calls it
    on every entry — a RestartFrom re-enters it on a reloaded page with a new
    box and the portal's refresh quota back."""
    ctx.scratch["details_captcha_reads"] = 0
    ctx.scratch["details_captcha_refreshes"] = 0
    ctx.scratch["details_captcha_rounds"] = 0


async def solve_details_captcha(ctx: RunContext) -> None:
    """Solve the "Verify Captcha" box on screen. Returns once the portal took
    a code — or moved on / the box went away; the caller's outcome poll then
    decides what the page is doing. Raises RestartFrom / ScriptedAbort when
    this pass's read budget (or the refresh quota it needed) is spent."""
    sc = ctx.scratch
    max_reads = max(1, MAX_AI_CAPTCHA_ATTEMPTS)
    seen_fp: str | None = None  # the last code we read — never read twice

    # Hard stop for the caller's poll <-> solver loop. A legitimate round
    # either spends a read or ends because the portal moved on, so a pass
    # needs only a couple; if the two probes ever disagreed about a popup the
    # page would bounce between them without spending anything — this cap
    # turns that into a clean restart instead of a spin to the phase timeout.
    rounds = int(sc.get("details_captcha_rounds") or 0) + 1
    sc["details_captcha_rounds"] = rounds
    if rounds > max_reads + 2:
        _restart_or_abort(
            ctx, why=f"the captcha box kept coming back ({rounds - 1} rounds)"
        )

    while True:
        reads = int(sc.get("details_captcha_reads") or 0)
        if reads >= max_reads:
            _restart_or_abort(
                ctx, why=f"{reads} captcha read(s) this pass, none accepted"
            )

        state, detail = await _await_fresh_code(ctx, not_fp=seen_fp)
        if state == "done":
            _log(ctx, "done", StepStatus.OK, value=detail)
            return
        if state == "stuck":
            # No new code after an unreadable read / a verdict: buy one.
            if not await _refresh(ctx):
                _restart_or_abort(
                    ctx,
                    why="no fresh captcha to read and the portal's "
                    f"{MAX_REFRESHES_PER_PASS} refreshes are spent",
                )
            continue
        fp = detail

        reads += 1
        sc["details_captcha_reads"] = reads
        seen_fp = fp

        b64 = await capture_element_png(
            ctx.session,
            SEL_CAPTCHA_IMG,
            timeout=CAPTURE_TIMEOUT_SECS,
            scale=OCR_SCALE,
        )
        if not b64:
            _log(
                ctx,
                "capture",
                StepStatus.RETRIED,
                attempt=reads,
                error="captcha image could not be captured",
            )
            if not await _refresh(ctx):
                _restart_or_abort(ctx, why="captcha image could not be captured")
            continue
        _debug_dump(ctx, b64)

        text, cost = await ocr_image(b64)
        text = (text or "").strip()
        readable = (
            text != "UNREADABLE" and ANSWER_MIN_LEN <= len(text) <= ANSWER_MAX_LEN
        )
        _log(
            ctx,
            "read",
            StepStatus.OK if readable else StepStatus.RETRIED,
            attempt=reads,
            value=text,
            ai_cost_usd=cost,
        )
        if not readable:
            if not await _refresh(ctx):
                _restart_or_abort(
                    ctx,
                    why=f"unreadable captcha ({text[:20]!r}) and the portal's "
                    f"{MAX_REFRESHES_PER_PASS} refreshes are spent",
                )
            continue

        try:
            await fill(
                ctx.session,
                SEL_CAPTCHA_INPUT,
                text,
                log=ctx.log,
                name=f"{_LOG}.fill_{reads}",
            )
        except ScriptedAbort:
            continue  # the box re-rendered under us — re-read whatever it shows

        # Submit only against the code we actually read.
        st = await _probe(ctx)
        if st is not None and (not st.get("open") or st.get("fp") != fp):
            _log(
                ctx,
                "stale",
                StepStatus.RETRIED,
                attempt=reads,
                error="the code changed before OK — reading the new one",
            )
            continue
        if not await _click_ok(ctx, attempt=reads):
            _restart_or_abort(ctx, why="the captcha box's OK button was not found")

        verdict, why = await _await_verdict(ctx, fp=fp)
        if verdict == "accepted":
            _log(ctx, "accepted", StepStatus.OK, attempt=reads, value=why)
            return
        if verdict == "rejected":
            # The portal shows a NEW code in the same box by itself; the next
            # fresh-code wait dismisses the popup and picks it up.
            _log(
                ctx,
                "rejected",
                StepStatus.RETRIED,
                attempt=reads,
                value=text,
                error=why[:300],
            )
            continue
        _log(
            ctx,
            "no_verdict",
            StepStatus.RETRIED,
            attempt=reads,
            value=text,
            error=f"no verdict within {VERDICT_WAIT_SECS:.0f}s after OK",
        )
        if not await _refresh(ctx):
            _restart_or_abort(ctx, why="the portal never answered the captcha")


# ─── Internals ──────────────────────────────────────────────────────────


def _log(ctx: RunContext, name: str, status: StepStatus, **kw) -> None:
    ctx.log.record(
        StepLog(index=ctx.log.next_index(), name=f"{_LOG}.{name}", status=status, **kw)
    )


async def _probe(ctx: RunContext) -> dict | None:
    """One read of the box + popup state. None when the evaluation itself
    failed (context torn down mid-render) — never evidence either way."""
    try:
        res = await cdp_eval(ctx.session, _PROBE_JS)
    except Exception:
        return None
    return res if isinstance(res, dict) else None


def _popup_text(st: dict) -> str:
    return " ".join(str(st.get("popup") or "").split())[:200]


async def _await_fresh_code(
    ctx: RunContext, *, not_fp: str | None
) -> tuple[str, str]:
    """Wait for a loaded code we haven't read yet (fingerprint != not_fp). A
    verdict popup on the way is dismissed — the portal then puts up the new
    code. Returns ("code", fp); ("done", why) when the box is gone, the RC
    rendered, or a known outcome popup is up (the caller's poll takes over);
    ("stuck", "") when no fresh code showed up in FRESH_CODE_WAIT_SECS."""
    deadline = time.monotonic() + FRESH_CODE_WAIT_SECS
    while time.monotonic() < deadline:
        st = await _probe(ctx)
        if st is None:
            await asyncio.sleep(POLL_TICK_SECS)
            continue
        kind = st.get("kind")
        if kind == "outcome":
            return "done", f"portal popup: {_popup_text(st)}"
        if kind == "captcha":
            try:
                await cdp_eval(ctx.session, _DISMISS_SWAL_JS)
            except Exception:
                pass
            await asyncio.sleep(POLL_TICK_SECS)
            continue
        if st.get("district"):
            return "done", "district rendered"
        if not st.get("open"):
            return "done", "box closed"
        fp = st.get("fp")
        if st.get("loaded") and fp and fp != not_fp:
            return "code", fp
        await asyncio.sleep(POLL_TICK_SECS)
    return "stuck", ""


async def _await_verdict(ctx: RunContext, *, fp: str) -> tuple[str, str]:
    """After OK, the portal's verdict on code `fp`:
      ("accepted", why)   box closed / RC rendered / a known outcome popup
      ("rejected", why)   a verdict popup over the box, or the code swapped
      ("no_verdict", "")  nothing decisive within VERDICT_WAIT_SECS"""
    deadline = time.monotonic() + VERDICT_WAIT_SECS
    while time.monotonic() < deadline:
        st = await _probe(ctx)
        if st is None:
            await asyncio.sleep(POLL_TICK_SECS)
            continue
        kind = st.get("kind")
        if kind == "outcome":
            return "accepted", f"portal moved on: {_popup_text(st)}"
        if st.get("district"):
            return "accepted", "district rendered"
        if not st.get("open"):
            return "accepted", "box closed"
        if kind == "captcha":
            return "rejected", _popup_text(st) or "error popup with no text"
        nfp = st.get("fp")
        if st.get("loaded") and nfp and nfp != fp:
            return "rejected", "the portal swapped the code without a popup"
        await asyncio.sleep(POLL_TICK_SECS)
    return "no_verdict", ""


async def _click_ok(ctx: RunContext, *, attempt: int) -> bool:
    deadline = time.monotonic() + OK_BUTTON_WAIT_SECS
    res: dict = {}
    while True:
        try:
            res = await cdp_eval(ctx.session, _CLICK_OK_JS) or {}
        except Exception as e:
            res = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        if res.get("ok"):
            _log(ctx, f"ok_{attempt}", StepStatus.OK, value=f"via={res.get('via')}")
            return True
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(POLL_TICK_SECS)
    _log(
        ctx,
        f"ok_{attempt}",
        StepStatus.FAILED,
        error=str(res.get("reason") or "OK button not found")[:200],
    )
    return False


async def _refresh(ctx: RunContext) -> bool:
    """Spend one of the portal's 2 refreshes on a new code. False when the
    quota is spent or the button is gone — the caller restarts the pass."""
    used = int(ctx.scratch.get("details_captcha_refreshes") or 0)
    if used >= MAX_REFRESHES_PER_PASS:
        return False
    try:
        clicked = bool(await cdp_eval(ctx.session, _CLICK_REFRESH_JS))
    except Exception:
        clicked = False
    if clicked:
        used += 1
        ctx.scratch["details_captcha_refreshes"] = used
        _log(ctx, "refresh", StepStatus.OK, value=f"{used}/{MAX_REFRESHES_PER_PASS}")
    else:
        _log(ctx, "refresh", StepStatus.FAILED, error="refresh button not found")
    return clicked


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
            f"details_captcha_{ctx.job_id}_{int(time.time() * 1000)}.png",
        )
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        _log(ctx, "debug_dump", StepStatus.OK, value=path)
    except Exception:
        pass


def _restart_or_abort(ctx: RunContext, *, why: str) -> NoReturn:
    """This pass's captcha budget is spent. Restart the whole wizard from
    open_portal while the pass budget lasts (SCRIPTED_POPUP_MAX_ATTEMPTS
    TOTAL passes; the counter lives in ctx.scratch and survives restarts) —
    a reloaded page brings a new box and the refresh quota back — then
    cancel. Pre-payment only: called from owner_info, never past it."""
    attempt = int(ctx.scratch.get("captcha_attempt") or 1)
    budget = max(1, SCRIPTED_POPUP_MAX_ATTEMPTS)
    if attempt < budget:
        ctx.scratch["captcha_attempt"] = attempt + 1
        _log(
            ctx,
            "restart",
            StepStatus.RETRIED,
            attempt=attempt,
            value=(
                f"captcha not solved -> restarting wizard "
                f"(attempt {attempt + 1}/{budget}): {why[:120]}"
            ),
        )
        try:
            from redis_client import job_key

            ctx.r.hset(job_key(ctx.job_id), "captchaRestarts", str(attempt))
        except Exception:
            pass
        raise RestartFrom("open_portal", f"get-details captcha: {why[:80]}")
    _log(ctx, "exhausted", StepStatus.FAILED, error=why[:300])
    raise ScriptedAbort(
        "the portal's Get Details captcha could not be solved automatically"
        + (f" after {budget} attempts" if budget > 1 else "")
        + " — request cancelled, no payment was made; try again shortly",
        terminal="cancelled",
        reason="details_captcha_unsolved",
    )
