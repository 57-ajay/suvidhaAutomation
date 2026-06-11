# worker/src/scripted/steps.py
"""Deterministic step primitives over a browser-use BrowserSession.

Everything is driven through CDP Runtime.evaluate so it's tolerant of browser-use
version differences — we mostly run small JS snippets against the page rather than
relying on high-level page objects. `_cdp_eval` is the one integration seam; if
your installed browser-use exposes the CDP client differently, adjust it there and
the rest of the file keeps working.

Each primitive records a StepLog and raises on hard failure. Timeouts and retries
are explicit so the runner stays predictable.
"""

from __future__ import annotations

import asyncio
import json
import time

from .log import StepLogger
from .types import StepLog, StepStatus, ScriptedAbort


# ─── CDP seam ──────────────────────────────────────────────────────────


async def _cdp_eval(session, expression: str):
    """Evaluate JS in the active page and return the JSON value.

    browser-use exposes a CDP client on the session. We use Runtime.evaluate with
    returnByValue so we get plain Python values back. TODO: confirm the accessor
    (`session.cdp_client` / `session.get_cdp_session()`) against your version.
    """
    cdp = getattr(session, "cdp_client", None)
    if cdp is None:
        # Fallbacks seen across browser-use versions.
        get = getattr(session, "get_cdp_session", None)
        cdp = await get() if callable(get) else None
    if cdp is None:
        raise RuntimeError("could not obtain CDP client from session")

    result = await cdp.send(
        "Runtime.evaluate",
        {
            "expression": f"(function(){{ {expression} }})()"
            if "return" in expression
            else expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
    )
    res = result.get("result", {})
    if res.get("subtype") == "error":
        raise RuntimeError(f"cdp eval error: {res.get('description')}")
    return res.get("value")


async def _current_url(session) -> str:
    try:
        return await _cdp_eval(session, "return document.location.href")
    except Exception:
        return ""


# ─── waits ─────────────────────────────────────────────────────────────


async def wait_for_selector(session, selector: str, *, log: StepLogger,
                            name: str, timeout: float = 30.0,
                            visible: bool = True) -> None:
    started = time.monotonic()
    js_visible = "&& el.offsetParent !== null" if visible else ""
    expr = (
        f"var el = document.querySelector({json.dumps(selector)});"
        f"return !!(el {js_visible});"
    )
    while time.monotonic() - started < timeout:
        try:
            if await _cdp_eval(session, expr):
                log.record(StepLog(log.next_index(), name, StepStatus.OK,
                                   selector=selector,
                                   duration_ms=int((time.monotonic()-started)*1000)))
                return
        except Exception:
            pass
        await asyncio.sleep(0.4)
    log.record(StepLog(log.next_index(), name, StepStatus.FAILED, selector=selector,
                       error=f"selector not visible within {timeout}s"))
    raise ScriptedAbort(f"selector not found: {selector}")


async def wait_for_url(session, fragment: str, *, log: StepLogger, name: str,
                       timeout: float = 30.0) -> None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if fragment in (await _current_url(session)):
            log.record(StepLog(log.next_index(), name, StepStatus.OK,
                               value=fragment))
            return
        await asyncio.sleep(0.4)
    log.record(StepLog(log.next_index(), name, StepStatus.FAILED, value=fragment,
                       error=f"url did not contain '{fragment}' within {timeout}s"))
    raise ScriptedAbort(f"url wait timeout: {fragment}")


async def sleep_seconds(secs: float, *, log: StepLogger, name: str) -> None:
    log.record(StepLog(log.next_index(), name, StepStatus.OK, value=f"{secs}s"))
    await asyncio.sleep(secs)


# ─── actions ───────────────────────────────────────────────────────────


async def navigate(session, url: str, *, log: StepLogger, name: str) -> None:
    await _cdp_eval(session, f"document.location.href = {json.dumps(url)}")
    log.record(StepLog(log.next_index(), name, StepStatus.OK, url=url))
    await asyncio.sleep(1.5)


async def click(session, selector: str, *, log: StepLogger, name: str,
                timeout: float = 10.0, retries: int = 1) -> None:
    expr = (
        f"var el = document.querySelector({json.dumps(selector)});"
        f"if(!el) return false; el.click(); return true;"
    )
    for attempt in range(retries + 1):
        try:
            if await _cdp_eval(session, expr):
                log.record(StepLog(log.next_index(), name, StepStatus.OK,
                                   selector=selector, attempt=attempt + 1))
                return
        except Exception as e:
            if attempt == retries:
                log.record(StepLog(log.next_index(), name, StepStatus.FAILED,
                                   selector=selector, error=str(e)))
                raise ScriptedAbort(f"click failed: {selector}")
        await asyncio.sleep(0.5)
    log.record(StepLog(log.next_index(), name, StepStatus.FAILED, selector=selector,
                       error="element not found"))
    raise ScriptedAbort(f"click target not found: {selector}")


async def click_by_text(session, text: str, *, log: StepLogger, name: str,
                        tag: str = "button", timeout: float = 10.0) -> None:
    expr = (
        f"var nodes = Array.from(document.querySelectorAll({json.dumps(tag)}));"
        f"var t = {json.dumps(text)}.trim().toLowerCase();"
        f"var el = nodes.find(n => (n.innerText||n.textContent||'').trim().toLowerCase().includes(t));"
        f"if(!el) return false; el.click(); return true;"
    )
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if await _cdp_eval(session, expr):
                log.record(StepLog(log.next_index(), name, StepStatus.OK, value=text))
                return
        except Exception:
            pass
        await asyncio.sleep(0.4)
    log.record(StepLog(log.next_index(), name, StepStatus.FAILED, value=text,
                       error=f"no {tag} with text '{text}'"))
    raise ScriptedAbort(f"click_by_text not found: {text}")


async def fill(session, selector: str, value: str, *, log: StepLogger,
               name: str) -> None:
    expr = (
        f"var el = document.querySelector({json.dumps(selector)});"
        f"if(!el) return false;"
        f"el.focus(); el.value = {json.dumps(value)};"
        f"el.dispatchEvent(new Event('input', {{bubbles:true}}));"
        f"el.dispatchEvent(new Event('change', {{bubbles:true}}));"
        f"return true;"
    )
    if await _cdp_eval(session, expr):
        log.record(StepLog(log.next_index(), name, StepStatus.OK, selector=selector,
                           value=value))
        return
    log.record(StepLog(log.next_index(), name, StepStatus.FAILED, selector=selector,
                       error="input not found"))
    raise ScriptedAbort(f"fill target not found: {selector}")


async def select_by_value(session, selector: str, value: str, *,
                          log: StepLogger, name: str) -> None:
    expr = (
        f"var el = document.querySelector({json.dumps(selector)});"
        f"if(!el) return false; el.value = {json.dumps(value)};"
        f"el.dispatchEvent(new Event('change', {{bubbles:true}})); return el.value === {json.dumps(value)};"
    )
    if await _cdp_eval(session, expr):
        log.record(StepLog(log.next_index(), name, StepStatus.OK, selector=selector,
                           value=value))
        return
    log.record(StepLog(log.next_index(), name, StepStatus.FAILED, selector=selector,
                       error=f"could not select value {value}"))
    raise ScriptedAbort(f"select_by_value failed: {selector}={value}")


async def select_by_text(session, selector: str, text: str, *, log: StepLogger,
                         name: str) -> None:
    expr = (
        f"var sel = document.querySelector({json.dumps(selector)});"
        f"if(!sel) return false;"
        f"var t = {json.dumps(text)}.trim().toLowerCase();"
        f"var opt = Array.from(sel.options).find(o => (o.text||'').trim().toLowerCase().includes(t));"
        f"if(!opt) return false; sel.value = opt.value;"
        f"sel.dispatchEvent(new Event('change', {{bubbles:true}})); return true;"
    )
    if await _cdp_eval(session, expr):
        log.record(StepLog(log.next_index(), name, StepStatus.OK, selector=selector,
                           value=text))
        return
    log.record(StepLog(log.next_index(), name, StepStatus.FAILED, selector=selector,
                       error=f"no option matching '{text}'"))
    raise ScriptedAbort(f"select_by_text failed: {selector} '{text}'")


async def get_select_value(session, selector: str) -> str:
    try:
        return await _cdp_eval(
            session,
            f"var el=document.querySelector({json.dumps(selector)}); return el ? (el.value||'') : '';",
        ) or ""
    except Exception:
        return ""


# ─── popup detection ───────────────────────────────────────────────────


async def read_popup_text(session) -> str | None:
    """Return SweetAlert2 popup body text if a popup is visible, else None."""
    expr = (
        "var p = document.querySelector('.swal2-popup');"
        "if(!p || p.offsetParent === null) return '';"
        "var h = document.querySelector('#swal2-html-container');"
        "return (h && h.innerText) ? h.innerText.trim() : (p.innerText||'').trim();"
    )
    try:
        txt = await _cdp_eval(session, expr)
        return txt or None
    except Exception:
        return None


async def dismiss_popup(session, *, log: StepLogger, name: str = "dismiss_popup") -> None:
    expr = (
        "var b = document.querySelector('.swal2-confirm');"
        "if(b){b.click(); return true;} return false;"
    )
    try:
        await _cdp_eval(session, expr)
        log.record(StepLog(log.next_index(), name, StepStatus.OK))
    except Exception:
        pass
