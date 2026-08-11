# worker/src/tasks/border_tax/advance.py
"""Page-stall recovery + verified wizard advances for the FULLY-AUTOMATED path.

Port of the scripted path's machinery (scripted/wizard.py — proven there
first) to the app/fullyAutomated runners, which were showing the same
failure class: "page element did not appear: select#floatingPrmit" (the p3
Next never advanced the section) and ": input#inputcap" (the p5 Next was a
no-op because the portal's server-side fee calculation was still running).
In the container the portal's Angular bundle can crawl past the fixed waits
that always pass locally; a fresh pass usually loads fine.

Three tools, to be wired into the STRICTLY PRE-PAYMENT phases only (entry
waits of phases 1-6 and the p3/p4/p5 Next clicks — never past Pay Online):

  wait_selector_or_restart / wait_url_or_restart
      the plain waits, but a timeout restarts the wizard from open_portal
      under the stall budget (AUTO_STALL_MAX_ATTEMPTS total attempts)
      instead of cancelling the run on first miss.

  click_next_verified
      click Next and VERIFY the next section's marker actually rendered,
      re-clicking up to a few times (a click can land before Angular wires
      the handler); exhausted -> stall budget.

  wait_fee_calculation
      after Calculate Fee/Tax, poll for the fee table's rows (the portal
      computes server-side, regularly 30+ seconds under load) so the amount
      extract reads the real total and the p5 Next isn't swallowed.

POPUP SEMANTICS ARE THE AUTO PATH'S OWN: a blocking popup ABORTS immediately
with the portal's text (exactly like the runners' _abort_on_blocking_popup)
— there is NO popup-restart budget here, that is a scripted-path concept.
Benign popups ("RECEIPT VALID", the chassis-bug note) are dismissed and
polling continues.

Money line: everything here is pre-payment; every abort is
terminal="cancelled" (retryable, no money moved), and a RestartFrom can
never rewind past a payment because it is never called past the disclaimer.
"""

from __future__ import annotations

import asyncio
import json
import time

from config import AUTO_STALL_MAX_ATTEMPTS
from engine.steps import (
    cdp_eval,
    click_by_text,
    dismiss_popup,
    read_popup_state,
    sleep_seconds,
    wait_for_selector,
    wait_for_url,
)
from engine.types import RestartFrom, RunContext, ScriptedAbort, StepLog, StepStatus

_PHASE_GAP_SECS = 1.5
NEXT_VERIFY_TIMEOUT_SECS = 12.0
NEXT_CLICK_ATTEMPTS = 3
CALC_RESULT_TIMEOUT_SECS = 45.0

# Same allowlist as the state runners' _classify_popup (kept locally so the
# runners can import this module without a cycle).
_BENIGN_POPUP_HINTS = ["RECEIPT VALID"]


def _classify_popup(text: str) -> str:
    up_text = (text or "").upper()
    if any(h in up_text for h in _BENIGN_POPUP_HINTS):
        return "benign"
    if "CHASSIS" in up_text and not any(
        k in up_text
        for k in ("INSURANCE", "FITNESS", "PUCC", "EXPIRED", "RENEW", "PENDING")
    ):
        return "benign"
    return "blocking"


async def _route_visible_popup(ctx: RunContext, name: str) -> bool:
    """One popup tick for a poll loop: no popup -> False; benign popup ->
    dismissed, True (caller continues polling); blocking popup -> logged,
    dismissed, run ABORTS with the portal's text (auto-path semantics)."""
    st = await read_popup_state(ctx.session)
    if not st.get("present"):
        return False
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
        await dismiss_popup(ctx.session, log=ctx.log, name=f"{name}.dismiss_benign")
        return True
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


async def _is_visible(session, selector: str) -> bool | None:
    """One-shot visibility probe (same non-zero-rect rule as wait_for_selector).
    Tri-state: True/False from a successful probe, None when the eval itself
    failed (context torn down mid-navigation, CDP hiccup) — callers must not
    read a failed probe as evidence about the page."""
    try:
        res = await cdp_eval(
            session,
            "(function(s){var els=document.querySelectorAll(s);"
            "for(var i=0;i<els.length;i++){"
            "  var r=els[i].getBoundingClientRect();"
            "  if(r.width>0 && r.height>0) return true;"
            "}"
            "return false;})(" + json.dumps(selector) + ")",
        )
        return bool(res)
    except Exception:
        return None


def stalled_page_restart_or_abort(
    ctx: RunContext,
    *,
    name: str,
    detail: str,
    final_message: str,
) -> None:
    """A wizard page never rendered or a Next click refused to advance.

    Rerun the whole wizard from the landing page while the stall budget
    lasts (AUTO_STALL_MAX_ATTEMPTS counts TOTAL attempts; the counter lives
    in ctx.scratch and survives restarts), then cancel with the stall
    message. Pre-payment only — callers must never use this past the
    disclaimer."""
    attempt = int(ctx.scratch.get("stall_attempt") or 1)
    budget = max(1, AUTO_STALL_MAX_ATTEMPTS)
    if attempt < budget:
        ctx.scratch["stall_attempt"] = attempt + 1
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name=f"{name}.restart",
                status=StepStatus.RETRIED,
                attempt=attempt,
                value=(
                    f"page stalled -> restarting wizard "
                    f"(attempt {attempt + 1}/{budget}): {detail[:120]}"
                ),
            )
        )
        try:
            from redis_client import job_key

            ctx.r.hset(job_key(ctx.job_id), "stallRestarts", str(attempt))
        except Exception:
            pass
        raise RestartFrom("open_portal", f"page stalled: {detail[:80]}")
    raise ScriptedAbort(
        final_message + f" (page still stalled after {budget} attempts)",
        terminal="cancelled",
        reason="portal_page_stalled",
    )


async def _route_popup_or_stall(
    ctx: RunContext, *, name: str, detail: str, final_message: str
) -> None:
    """A wait timed out. A blocking popup on screen is the real blocker —
    abort with its text (auto-path rule); otherwise treat the timeout as a
    page stall and use the stall budget."""
    try:
        blocked = await _route_visible_popup(ctx, name)  # raises on blocking
    except ScriptedAbort:
        raise
    except Exception:
        blocked = False
    del blocked  # benign-or-none either way: fall through to the stall path
    stalled_page_restart_or_abort(
        ctx, name=name, detail=detail, final_message=final_message
    )


async def wait_selector_or_restart(
    ctx: RunContext, selector: str, *, name: str, timeout: int
) -> None:
    """wait_for_selector, but a timeout restarts the wizard (stall budget)
    instead of cancelling the run outright."""
    try:
        await wait_for_selector(
            ctx.session, selector, log=ctx.log, name=name, timeout=timeout
        )
    except ScriptedAbort as e:
        await _route_popup_or_stall(
            ctx, name=name, detail=e.message, final_message=e.message
        )


async def wait_url_or_restart(
    ctx: RunContext, contains: str, *, name: str, timeout: int
) -> None:
    """wait_for_url, but a timeout restarts the wizard (stall budget)."""
    try:
        await wait_for_url(
            ctx.session, contains, log=ctx.log, name=name, timeout=timeout
        )
    except ScriptedAbort as e:
        await _route_popup_or_stall(
            ctx, name=name, detail=e.message, final_message=e.message
        )


async def click_next_verified(
    ctx: RunContext,
    *,
    name: str,
    ready_selector: str,
    attempts: int = NEXT_CLICK_ATTEMPTS,
    verify_timeout: float = NEXT_VERIFY_TIMEOUT_SECS,
) -> None:
    """Click the wizard's Next and VERIFY the section actually swapped.

    On a slow page the click can land before Angular wires the handler (or
    hit a stale still-visible button), leaving the run on the SAME page —
    the next phase then dies on "page element did not appear". Advanced = a
    POSITIVE sighting of `ready_selector` only (the wizard appends sections,
    so "old section gone" carries no signal, and a failed probe never counts
    as progress). Popups are classified each tick: benign -> dismissed,
    blocking -> abort with the portal's text. No progress after `attempts`
    clicks -> stall budget."""
    for attempt in range(1, attempts + 1):
        try:
            await click_by_text(
                ctx.session,
                "Next",
                log=ctx.log,
                name=f"{name}.click" if attempt == 1 else f"{name}.click_{attempt}",
                tag="button",
            )
        except ScriptedAbort as e:
            # The button itself never became clickable — poll below anyway
            # (the page may still be mounting) and let the budget decide.
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name=f"{name}.click_missing",
                    status=StepStatus.RETRIED,
                    attempt=attempt,
                    error=e.message[:200],
                )
            )

        deadline = time.monotonic() + verify_timeout
        while time.monotonic() < deadline:
            if await _route_visible_popup(ctx, f"{name}.popup"):
                await asyncio.sleep(0.5)
                continue
            if await _is_visible(ctx.session, ready_selector) is True:
                ctx.log.record(
                    StepLog(
                        index=ctx.log.next_index(),
                        name=f"{name}.advanced",
                        status=StepStatus.OK,
                        attempt=attempt,
                        selector=ready_selector,
                    )
                )
                await sleep_seconds(
                    _PHASE_GAP_SECS, log=ctx.log, name=f"{name}.settle"
                )
                return
            await asyncio.sleep(0.5)

        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name=f"{name}.not_advanced",
                status=StepStatus.RETRIED,
                attempt=attempt,
                error=(
                    f"page did not advance to {ready_selector} within "
                    f"{verify_timeout:.0f}s"
                ),
            )
        )

    await _route_popup_or_stall(
        ctx,
        name=name,
        detail=f"'Next' never advanced the wizard to {ready_selector}",
        final_message=(
            "the portal page kept reloading without advancing past this step"
        ),
    )


_FEE_ROWS_JS = (
    "(function(){"
    "var tables=document.querySelectorAll('table');"
    "for(var i=0;i<tables.length;i++){"
    "  var thead=tables[i].querySelector('thead');"
    "  var h=((thead&&thead.innerText)||'').toLowerCase();"
    "  if(h.indexOf('tax/fee particulars')>=0 && h.indexOf('amount')>=0){"
    "    return tables[i].querySelectorAll('tbody tr').length;"
    "  }"
    "}"
    "return 0;})()"
)


async def wait_fee_calculation(
    ctx: RunContext,
    *,
    name: str = "p5.wait_fee_calculation",
    timeout: float = CALC_RESULT_TIMEOUT_SECS,
) -> None:
    """After the Calculate Fee/Tax click, wait for the fee table's rows.

    The portal computes the fee server-side and can take 30+ seconds under
    load; until the table renders, Next is silently ignored and a too-early
    extract loses the amount (borderTaxAmount stays the \"0\" sentinel). Any
    popup raised by the calculation is classified on the way (blocking ->
    abort with its text). Times out QUIETLY: extraction is best-effort and
    the verified Next still guards the advance."""
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if await _route_visible_popup(ctx, name):
            await asyncio.sleep(0.5)
            continue
        try:
            rows = await cdp_eval(ctx.session, _FEE_ROWS_JS)
        except Exception:
            rows = 0
        if isinstance(rows, (int, float)) and rows > 0:
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name=name,
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value=f"fee table rendered ({int(rows)} row(s))",
                )
            )
            return
        await asyncio.sleep(1.0)
    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name=name,
            status=StepStatus.RETRIED,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"fee table did not render within {timeout:.0f}s",
        )
    )
