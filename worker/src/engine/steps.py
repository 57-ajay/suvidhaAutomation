"""Browser primitives over CDP. Every public step logs OK/FAILED with timing
and raises ScriptedAbort on failure so phases stay linear.

TARGETING RULE — the CheckPost wizard APPENDS each step's section to the page
and never unmounts the previous ones (verified on the live portal: the
Vehicle Information section, with its own Previous/Next, stays mounted above
the Tax Information section, and the Disclaimer page carries TWO "Next"
buttons in the DOM). Stale sections can also duplicate ids. So every
primitive resolves its target as the LAST VISIBLE match: newest section is
last in DOM order, and visibility filters out the hidden stale copies. A
first-match querySelector here clicks the PREVIOUS step's button — that is
exactly the bug class that let runs march past the portal's validity popups.

Every successful action also breathes for STEP_DELAY_SECS afterwards: these
portals are slow Angular apps and racing their re-renders causes phantom
state. The CDP seam is `cdp_eval`; confirm the accessor against the
installed browser-use version before first live run.
"""

from __future__ import annotations

import asyncio
import json
import time

from config import STEP_DELAY_SECS

from .log import StepLogger
from .types import ScriptedAbort, StepLog, StepStatus


# ─── CDP evaluation ─────────────────────────────────────────────────────


async def cdp_eval(session, expr: str):
    """Evaluate JS in the page and return the JSON value.

    `expr` may be a bare expression ("document.readyState"), a function body
    containing `return`, or a self-invoking function — all are normalized.
    """
    src = expr.strip()
    if not src.startswith("("):
        body = src if "return" in src else f"return ({src});"
        src = "(function(){" + body + "})()"

    cdp = await session.get_or_create_cdp_session()
    # Bounded so a wedged browser can't hang a poll loop forever; callers'
    # retry/except paths treat it like any other failed evaluation.
    result = await asyncio.wait_for(
        cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": src, "returnByValue": True, "awaitPromise": True},
            session_id=cdp.session_id,
        ),
        timeout=30,
    )
    if isinstance(result, dict):
        exc = result.get("exceptionDetails")
        if exc:
            raise RuntimeError(f"cdp_eval exception: {exc.get('text', exc)}")
        return (result.get("result") or {}).get("value")
    res = getattr(result, "result", None)
    return getattr(res, "value", None) if res is not None else None


async def current_url(session) -> str:
    try:
        return await cdp_eval(session, "window.location.href") or ""
    except Exception:
        return ""


async def page_text(session) -> str:
    try:
        return (
            await cdp_eval(session, "document.body ? document.body.innerText : ''")
            or ""
        )
    except Exception:
        return ""


# JS fragment: resolve a selector to its LAST VISIBLE match (fallback: last
# match of any kind). See the module docstring for why.
_PICK_JS = (
    "function __pick(s){"
    "  var els = Array.prototype.slice.call(document.querySelectorAll(s));"
    "  if(!els.length) return null;"
    "  var vis = els.filter(function(e){"
    "    var r = e.getBoundingClientRect();"
    "    return r.width > 0 && r.height > 0;"
    "  });"
    "  return vis.length ? vis[vis.length-1] : els[els.length-1];"
    "}"
)


async def _breathe(delay = 0.0) -> None:
    if delay > 0.0:
        await asyncio.sleep(delay)
    elif STEP_DELAY_SECS > 0:
        await asyncio.sleep(STEP_DELAY_SECS)


# ─── logging helpers ────────────────────────────────────────────────────


def _ok(log: StepLogger, name: str, started: float, **kw) -> None:
    log.record(
        StepLog(
            index=log.next_index(),
            name=name,
            status=StepStatus.OK,
            duration_ms=int((time.monotonic() - started) * 1000),
            **kw,
        )
    )


def _fail(log: StepLogger, name: str, started: float, err, **kw) -> None:
    log.record(
        StepLog(
            index=log.next_index(),
            name=name,
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(err).__name__}: {err}",
            **kw,
        )
    )


# ─── steps ──────────────────────────────────────────────────────────────


async def navigate(
    session, url: str, *, log: StepLogger, name: str, timeout: int = 45
) -> None:
    started = time.monotonic()
    try:
        cdp = await session.get_or_create_cdp_session()
        await asyncio.wait_for(
            cdp.cdp_client.send.Page.navigate(
                params={"url": url},
                session_id=cdp.session_id,
            ),
            timeout=30,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await cdp_eval(session, "document.readyState") == "complete":
                _ok(log, name, started, url=url)
                return
            await asyncio.sleep(0.25)
        raise TimeoutError(f"navigation to {url} not complete in {timeout}s")
    except Exception as e:
        _fail(log, name, started, e, url=url)
        raise ScriptedAbort(f"could not open {url}")


async def wait_for_selector(
    session, selector: str, *, log: StepLogger, name: str, timeout: int = 30
) -> None:
    started = time.monotonic()
    expr = (
        "(function(s){var e=document.querySelector(s);"
        "if(!e) return null;"
        "var els=document.querySelectorAll(s);"
        "for(var i=0;i<els.length;i++){"
        "  var r=els[i].getBoundingClientRect();"
        "  if(r.width>0 && r.height>0) return {w:r.width,h:r.height};"
        "}"
        "return {w:0,h:0};})(" + json.dumps(selector) + ")"
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            info = await cdp_eval(session, expr)
            if info and info.get("w", 0) > 0 and info.get("h", 0) > 0:
                _ok(log, name, started, selector=selector)
                await _breathe(0.4)
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"not visible within {timeout}s: {selector}")
            await asyncio.sleep(0.3)
    except Exception as e:
        _fail(log, name, started, e, selector=selector)
        # A stall here is almost always a portal popup blocking the wizard —
        # put its text in the error instead of a bare selector name.
        popup = await read_popup_text(session)
        raise ScriptedAbort(
            f"page element did not appear: {selector}"
            + (f" — portal popup on screen: {popup[:250]}" if popup else ""),
        )


async def wait_for_url(
    session, contains: str, *, log: StepLogger, name: str, timeout: int = 30
) -> None:
    started = time.monotonic()
    deadline = time.monotonic() + timeout
    try:
        while True:
            url = await current_url(session)
            if contains.lower() in url.lower():
                _ok(log, name, started, url=url)
                await _breathe(0.4)
                return
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"url did not contain '{contains}' in {timeout}s (last: {url})"
                )
            await asyncio.sleep(0.4)
    except Exception as e:
        _fail(log, name, started, e)
        popup = await read_popup_text(session)
        raise ScriptedAbort(
            f"expected page never loaded ({contains})"
            + (f" — portal popup on screen: {popup[:250]}" if popup else ""),
        )


async def click(
    session,
    selector: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 10,
    retries: int = 1,
) -> None:
    started = time.monotonic()
    expr = (
        "(function(s){" + _PICK_JS + "var e=__pick(s);"
        "if(!e) return false; e.click(); return true;})(" + json.dumps(selector) + ")"
    )
    for _ in range(retries + 1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await cdp_eval(session, expr):
                    _ok(log, name, started, selector=selector)
                    await _breathe()
                    return
            except Exception:
                pass
            await asyncio.sleep(0.4)
    _fail(log, name, started, TimeoutError("element not found"), selector=selector)
    raise ScriptedAbort(f"could not click: {selector}")


async def click_by_text(
    session,
    text: str,
    *,
    log: StepLogger,
    name: str,
    tag: str = "button",
    timeout: int = 10,
) -> None:
    """Click the LAST VISIBLE <tag> whose text contains `text` — on stacked
    wizard pages the earlier sections keep their own buttons mounted."""
    started = time.monotonic()
    expr = (
        "(function(tag, t){"
        "var els = Array.prototype.slice.call(document.querySelectorAll(tag))"
        "  .filter(function(x){"
        "    if(!(x.innerText||'').trim().toLowerCase().includes(t.toLowerCase()))"
        "      return false;"
        "    var r = x.getBoundingClientRect();"
        "    return r.width > 0 && r.height > 0;"
        "  });"
        "if(!els.length) return false;"
        "els[els.length-1].click();"
        "return els.length;"
        "})(" + json.dumps(tag) + "," + json.dumps(text) + ")"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            n = await cdp_eval(session, expr)
            if n:
                _ok(
                    log,
                    name,
                    started,
                    value=text + (f" (picked last of {n} visible)" if n > 1 else ""),
                )
                await _breathe()
                return
        except Exception:
            pass
        await asyncio.sleep(0.4)
    _fail(log, name, started, TimeoutError(f"no visible {tag} with text"), value=text)
    raise ScriptedAbort(f"button not found: {text}")


async def fill(
    session, selector: str, value: str, *, log: StepLogger, name: str
) -> None:
    started = time.monotonic()
    expr = (
        "(function(s, v){" + _PICK_JS + "var el=__pick(s);"
        "if(!el) return false;"
        "el.focus(); el.value = v;"
        "el.dispatchEvent(new Event('input', {bubbles:true}));"
        "el.dispatchEvent(new Event('change', {bubbles:true}));"
        "return el.value === v;"
        "})(" + json.dumps(selector) + "," + json.dumps(value) + ")"
    )
    try:
        if await cdp_eval(session, expr):
            _ok(log, name, started, selector=selector, value=value)
            await _breathe()
            return
        raise RuntimeError("input missing or value did not stick")
    except Exception as e:
        _fail(log, name, started, e, selector=selector, value=value)
        raise ScriptedAbort(f"could not fill {selector}")


async def select_by_value(
    session, selector: str, value: str, *, log: StepLogger, name: str, timeout: int = 10
) -> None:
    """Set a <select> by option value, polling until the option exists
    (options often populate async after a prior field changes)."""
    started = time.monotonic()
    expr = (
        "(function(s, v){" + _PICK_JS + "var e=__pick(s);"
        "if(!(e instanceof HTMLSelectElement)) return {ok:false, reason:'no_select'};"
        "for(var i=0;i<e.options.length;i++){"
        "  if(e.options[i].value===v){"
        "    e.value=v;"
        "    e.dispatchEvent(new Event('input',{bubbles:true}));"
        "    e.dispatchEvent(new Event('change',{bubbles:true}));"
        "    return {ok:true};}}"
        "return {ok:false, reason:'value_not_found',"
        "        seen:Array.from(e.options).map(o=>o.value)};"
        "})(" + json.dumps(selector) + "," + json.dumps(value) + ")"
    )
    deadline = time.monotonic() + timeout
    last = None
    try:
        while True:
            res = await cdp_eval(session, expr)
            if res and res.get("ok"):
                _ok(log, name, started, selector=selector, value=value)
                await _breathe()
                return
            last = res
            if time.monotonic() > deadline:
                raise RuntimeError(f"value_not_found after {timeout}s; saw={last}")
            await asyncio.sleep(0.5)
    except Exception as e:
        _fail(log, name, started, e, selector=selector, value=value)
        raise ScriptedAbort(f"could not select {value} in {selector}")


async def select_by_text(
    session, selector: str, text: str, *, log: StepLogger, name: str, timeout: int = 10
) -> None:
    """Set a <select> by (case-insensitive, substring) option text, polling."""
    started = time.monotonic()
    expr = (
        "(function(s, t){" + _PICK_JS + "var sel=__pick(s);"
        "if(!(sel instanceof HTMLSelectElement)) return false;"
        "var opt=Array.from(sel.options).find("
        "  o=>(o.text||'').trim().toLowerCase().includes(t.trim().toLowerCase()));"
        "if(!opt) return false;"
        "sel.value=opt.value;"
        "sel.dispatchEvent(new Event('input',{bubbles:true}));"
        "sel.dispatchEvent(new Event('change',{bubbles:true}));"
        "return true;"
        "})(" + json.dumps(selector) + "," + json.dumps(text) + ")"
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            if await cdp_eval(session, expr):
                _ok(log, name, started, selector=selector, value=text)
                await _breathe()
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"no option matching '{text}'")
            await asyncio.sleep(0.5)
    except Exception as e:
        _fail(log, name, started, e, selector=selector, value=text)
        raise ScriptedAbort(f"could not select '{text}' in {selector}")


async def get_select_value(session, selector: str) -> str | None:
    """Current value of the LAST VISIBLE <select> matching the selector:
    '' for placeholder, None if not a select. No log entry."""
    expr = (
        "(function(s){" + _PICK_JS + "var e=__pick(s);"
        "if(!(e instanceof HTMLSelectElement)) return null;"
        "return e.value;})(" + json.dumps(selector) + ")"
    )
    try:
        return await cdp_eval(session, expr)
    except Exception:
        return None


async def get_input_type(session, selector: str) -> str | None:
    """`type` of the LAST VISIBLE input matching the selector (e.g. 'date' or
    'datetime-local'), or None if not found / not an input. No log entry.
    Lets a state choose date vs datetime-local formatting from the live DOM."""
    expr = (
        "(function(s){" + _PICK_JS + "var e=__pick(s);"
        "return e ? e.type : null;})(" + json.dumps(selector) + ")"
    )
    try:
        return await cdp_eval(session, expr)
    except Exception:
        return None


async def get_text(session, selector: str) -> str:
    expr = (
        "(function(s){" + _PICK_JS + "var e=__pick(s);"
        "return e ? (e.textContent||'').trim() : '';})(" + json.dumps(selector) + ")"
    )
    try:
        return await cdp_eval(session, expr) or ""
    except Exception:
        return ""


async def sleep_seconds(secs: float, *, log: StepLogger, name: str = "sleep") -> None:
    started = time.monotonic()
    await asyncio.sleep(secs)
    _ok(log, name, started, value=f"{secs}s")


# ─── popups (SweetAlert2 + generic modals) ──────────────────────────────


async def read_popup_text(session) -> str | None:
    """Visible SweetAlert2 popup body text, else None."""
    st = await read_popup_state(session)
    return st.get("text") or None if st.get("present") else None


async def read_popup_state(session) -> dict:
    """Visible SweetAlert2 popup as {present, text, error} — `error` is True
    when the popup carries swal2's error (red X) icon."""
    expr = (
        "(function(){"
        "var popups=document.querySelectorAll('.swal2-popup');"
        "for(var i=popups.length-1;i>=0;i--){"
        "  var r=popups[i].getBoundingClientRect();"
        "  if(r.width<=0 || r.height<=0) continue;"
        "  var h=popups[i].querySelector('#swal2-html-container');"
        "  var t=((h && h.innerText) ? h.innerText : (popups[i].innerText||'')).trim();"
        "  var err=!!popups[i].querySelector("
        "    '.swal2-icon-error, .swal2-icon.swal2-error, .swal2-x-mark');"
        "  return {present:true, text:t.slice(0,300), error:err};"
        "}"
        "return {present:false};"
        "})()"
    )
    try:
        return await cdp_eval(session, expr) or {"present": False}
    except Exception:
        return {"present": False}


async def dismiss_popup(
    session, *, log: StepLogger, name: str = "dismiss_popup"
) -> None:
    """Click the confirm button of every VISIBLE SweetAlert2 popup, plus the
    generic modal-footer variants some portal pages use."""
    expr = (
        "(function(){"
        "var clicked=0;"
        "var popups=document.querySelectorAll('.swal2-popup');"
        "for(var i=0;i<popups.length;i++){"
        "  var r=popups[i].getBoundingClientRect();"
        "  if(r.width<=0 || r.height<=0) continue;"
        "  var b=popups[i].querySelector('button.swal2-confirm');"
        "  if(b){ try{b.click(); clicked++;}catch(e){} }"
        "}"
        "if(!clicked){"
        "  var alt=document.querySelector("
        "    '.modal-footer button, .swal-button--confirm');"
        "  if(alt){ try{alt.click(); clicked++;}catch(e){} }"
        "}"
        "return clicked;"
        "})()"
    )
    started = time.monotonic()
    try:
        await cdp_eval(session, expr)
        _ok(log, name, started)
        await _breathe(0.5)
    except Exception:
        pass


async def abort_if_popup_text(
    session,
    keywords: list[str],
    abort_message: str,
    *,
    log: StepLogger,
    name: str,
    terminal: str = "cancelled",
    close_selector: str | None = None,
) -> None:
    """If any visible modal contains a keyword, optionally click
    close_selector, then raise ScriptedAbort with the portal's own popup text
    appended so the user sees the real reason."""
    started = time.monotonic()
    expr = (
        "(function(kw){"
        "var nodes=document.querySelectorAll("
        "  '.modal,.modal-content,.popup,[role=\"dialog\"],.swal2-popup');"
        "var text='';"
        "for(var i=0;i<nodes.length;i++){var n=nodes[i];"
        "  var r=n.getBoundingClientRect();"
        "  if(r.width>0 && r.height>0) text += (n.textContent||'') + ' ';}"
        "if(!text) return {found:false};"
        "var up=text.toUpperCase();"
        "for(var j=0;j<kw.length;j++)"
        "  if(up.indexOf(kw[j].toUpperCase())>=0)"
        "    return {found:true, matched:kw[j], text:text.trim().slice(0,300)};"
        "return {found:false};"
        "})(" + json.dumps(keywords) + ")"
    )
    res = await cdp_eval(session, expr)
    if res and res.get("found"):
        if close_selector:
            try:
                await cdp_eval(
                    session,
                    "(function(s){var e=document.querySelector(s);"
                    "if(e) e.click(); return !!e;})("
                    + json.dumps(close_selector)
                    + ")",
                )
            except Exception:
                pass
        detail = res.get("text", "")
        _fail(
            log,
            name,
            started,
            ScriptedAbort(abort_message),
            value=f"matched={res.get('matched')}",
        )
        raise ScriptedAbort(f"{abort_message}: {detail}", terminal=terminal)
    _ok(log, name, started)
