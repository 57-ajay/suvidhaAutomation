"""Challan-payment runner: four phases on the shared engine.pipeline.

Fault model, phase by phase:

    open_portal / search_challan / open_record
        Pre-payment. Every failure is a ScriptedAbort(terminal="cancelled") —
        nothing irreversible happened, the client may retry. "This number does
        not exist" is a cancel with a readable reason, not a crash.

    human_handover
        Past the money line. The operator is on a live payment page, so the
        phase NEVER ends silently: either the receipt page appears (-> done),
        the portal declares the transaction failed/pending (-> failed), or the
        window expires (-> failed). `failed` flips manualReview on the API side
        and leaves aiAgentPaymentStatus.status = "failed" with the reason.

Captcha: the same securimage widget challan-settlement already solves, so the
same shared solver (tasks.border_tax.captcha_user.solve_captcha) in AI mode.
The client-facing human fallback is OFF: this task writes no captcha document,
so there is no captcha UI for a driver to answer. When OCR is exhausted we hand
the captcha to the OPERATOR on the live view instead (Redis waitReason +
humanInput, the same seam the payment handover uses) rather than failing a run
that a human can finish in five seconds.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import time

import api_client
from engine.pipeline import Phase
from engine.steps import (
    cdp_eval,
    click,
    click_by_text,
    fill,
    navigate,
    page_text,
    select_by_text,
    sleep_seconds,
    wait_for_selector,
)
from engine.types import RunContext, RunOutcome, ScriptedAbort, StepLog, StepStatus
from lifecycle.status import Status
from redis_client import job_key
from tasks.border_tax.captcha_user import solve_captcha
from tasks.challan_settlement.extract import (
    INVALID_CAPTCHA_MARKERS,
    NOT_FOUND_MARKERS,
    RESULTS_MARKER,
)

from . import selectors as S
from .departments import department_for_payment
from .params import ChallanPaymentParams

# ── tunables ─────────────────────────────────────────────────────────────
RESULTS_POLL_SECS = 45.0  # after Submit: wait for results / not-found
PAGE_SETTLE_SECS = 1.5
RECEIPT_POLL_INTERVAL_SECS = 5.0
RECEIPT_PAINT_SETTLE_SECS = 2.0

# How long the operator has to solve the captcha on the live view after OCR
# gives up. Short by design: they are already watching the slot.
OPERATOR_CAPTCHA_SECS = int(os.environ.get("CHALLAN_PAYMENT_CAPTCHA_WAIT_SECS", "300"))

# How long the operator has to do Get OTP -> OTP -> pay -> receipt. Generous:
# the OTP round-trip goes through the driver's phone.
HANDOVER_SECS = int(os.environ.get("CHALLAN_PAYMENT_HANDOVER_SECS", "900"))

CAPTCHA_ATTEMPTS = int(os.environ.get("CHALLAN_PAYMENT_CAPTCHA_ATTEMPTS", "5"))
_SOLVE_EXTRA_KW: dict = (
    {"max_ai_attempts": CAPTCHA_ATTEMPTS}
    if "max_ai_attempts" in inspect.signature(solve_captcha).parameters
    else {}
)

# Same Bootstrap-modal handling challan-settlement needs: vcourts renders both
# "Invalid Captcha" and "This number does not exist" as modals, and a stale
# open modal would poison every later text check.
_ALERT_TRAP_JS = (
    "(function(){"
    "  if(window.__cpTrap) return true;"
    "  window.__cpTrap = true; window.__cpAlerts = [];"
    "  window.alert = function(m){ window.__cpAlerts.push(String(m)); };"
    "  window.confirm = function(m){ window.__cpAlerts.push(String(m)); return true; };"
    "  return true;"
    "})()"
)

_MODAL_TEXT_JS = (
    "(function(){"
    "  var out = [];"
    "  document.querySelectorAll('.modal .alert-danger, .modal .alert-success')"
    "    .forEach(function(el){"
    "      var r = el.getBoundingClientRect();"
    "      if(r.width > 0 && r.height > 0){ out.push(el.innerText || ''); }"
    "    });"
    "  return out.join(' | ');"
    "})()"
)

_DISMISS_MODAL_JS = (
    "(function(){"
    "  var n = 0;"
    "  document.querySelectorAll('.modal').forEach(function(m){"
    "    var r = m.getBoundingClientRect();"
    "    if(!(r.width > 0 && r.height > 0)) return;"
    "    var b = m.querySelector('[data-bs-dismiss=\"modal\"], .close');"
    "    if(b){ try{ b.click(); }catch(e){} }"
    "    m.classList.remove('show');"
    "    m.style.display = 'none';"
    "    n++;"
    "  });"
    "  document.querySelectorAll('.modal-backdrop').forEach(function(bd){ bd.remove(); });"
    "  document.body.classList.remove('modal-open');"
    "  return n;"
    "})()"
)


def _log(
    ctx: RunContext,
    name: str,
    status: StepStatus,
    value: str = "",
    error: str | None = None,
) -> None:
    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name=name,
            status=status,
            value=value[:500] if value else None,
            error=error[:500] if error else None,
        )
    )


async def _page_signals(ctx: RunContext) -> str:
    """Visible body text + visible modal alerts + trapped native alerts,
    lowercased, for marker checks."""
    try:
        body = await page_text(ctx.session) or ""
        modal = await cdp_eval(ctx.session, _MODAL_TEXT_JS) or ""
        alerts = await cdp_eval(ctx.session, "(window.__cpAlerts||[]).join(' | ')") or ""
        return f"{body}\n{modal}\n{alerts}".lower()
    except Exception:
        return ""


async def _dismiss_modals(ctx: RunContext, where: str) -> None:
    try:
        n = await cdp_eval(ctx.session, _DISMISS_MODAL_JS)
        if n:
            _log(ctx, f"{where}.dismiss_modal", StepStatus.OK, value=f"closed {n}")
    except Exception:
        pass


async def _wait_for_operator(ctx: RunContext, reason: str, timeout: int) -> str | None:
    """Park on the live view until the operator posts humanInput (or the job is
    cancelled, or the window expires). Redis-only: this task has no request doc,
    so there is nothing to push to a client.

    Returns the operator's text, or None on timeout.
    """
    key = job_key(ctx.job_id)
    ctx.reporter.set_wait(reason)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if ctx.r.hget(key, "status") == "cancelled":
                raise ScriptedAbort(
                    "cancelled by user while waiting for the operator",
                    terminal="cancelled",
                )
            answer = ctx.r.hget(key, "humanInput")
            if answer:
                ctx.r.hdel(key, "humanInput")
                return str(answer).strip()
            await asyncio.sleep(1.0)
        return None
    finally:
        ctx.reporter.clear_wait()


# ── phase 1: open the portal on the right department ─────────────────────


async def open_portal(ctx: RunContext) -> None:
    p: ChallanPaymentParams = ctx.params

    department = department_for_payment(p.challanNo, p.department)
    if not department:
        # The API rejects unmapped challans at enqueue, so reaching here means
        # the tables drifted — say so plainly instead of guessing a court.
        raise ScriptedAbort(
            f"challan {p.challanNo} does not map to any Virtual Courts "
            "department — pass an explicit `department` to override",
            terminal="cancelled",
            reason="department_unresolved",
        )
    ctx.scratch["department"] = department

    await navigate(ctx.session, S.VC_HOME, log=ctx.log, name="portal.open_home")
    await wait_for_selector(
        ctx.session,
        S.SEL_DEPT_DROPDOWN,
        log=ctx.log,
        name="portal.wait_dropdown",
        timeout=30,
    )
    await select_by_text(
        ctx.session,
        S.SEL_DEPT_DROPDOWN,
        department,
        log=ctx.log,
        name="portal.select_department",
        timeout=15,
    )
    await click(ctx.session, S.SEL_PROCEED, log=ctx.log, name="portal.proceed")
    await wait_for_selector(
        ctx.session, S.SEL_TAB_POLICE, log=ctx.log, name="portal.wait_main", timeout=30
    )
    _log(ctx, "portal.ready", StepStatus.OK, value=department)


# ── phase 2: search the challan number ───────────────────────────────────


async def search_challan(ctx: RunContext) -> None:
    p: ChallanPaymentParams = ctx.params
    challan = p.challanNo
    department = ctx.scratch.get("department", "")

    await cdp_eval(ctx.session, _ALERT_TRAP_JS)
    await click(ctx.session, S.SEL_TAB_POLICE, log=ctx.log, name="search.tab_police")
    await wait_for_selector(
        ctx.session,
        S.SEL_CHALLAN_INPUT,
        log=ctx.log,
        name="search.wait_challan_input",
        timeout=20,
    )
    await fill(
        ctx.session, S.SEL_CHALLAN_INPUT, challan, log=ctx.log, name="search.fill_challan"
    )
    await sleep_seconds(PAGE_SETTLE_SECS, log=ctx.log, name="search.settle")

    async def _submit_and_check() -> bool:
        """Close any leftover modal, re-fill the challan field (a rejected
        captcha resets the form), Submit, then poll for a decisive outcome.
        True on results OR not-found (both decisive); False on invalid-captcha
        or silence, which is what tells the solver to retry."""
        await _dismiss_modals(ctx, "search")
        try:
            await fill(
                ctx.session,
                S.SEL_CHALLAN_INPUT,
                challan,
                log=ctx.log,
                name="search.refill_challan",
            )
        except Exception:
            pass
        try:
            await click(ctx.session, S.SEL_SUBMIT, log=ctx.log, name="search.submit")
        except Exception:
            return False
        deadline = time.monotonic() + RESULTS_POLL_SECS
        while time.monotonic() < deadline:
            text = await _page_signals(ctx)
            if RESULTS_MARKER.lower() in text:
                return True
            if any(m in text for m in NOT_FOUND_MARKERS):
                return True
            if any(m in text for m in INVALID_CAPTCHA_MARKERS):
                await _dismiss_modals(ctx, "search")
                return False
            await asyncio.sleep(1.0)
        return False

    async def _is_rejected() -> bool:
        """Decisive-first: results or not-found present -> NOT rejected, even
        if stale error text lingers. Otherwise treat as a captcha rejection and
        dismiss so the retry starts clean."""
        text = await _page_signals(ctx)
        if RESULTS_MARKER.lower() in text or any(m in text for m in NOT_FOUND_MARKERS):
            return False
        await _dismiss_modals(ctx, "search")
        return True

    ai_solved = True
    try:
        await solve_captcha(
            ctx,
            stage="challan-payment",
            input_selector=S.SEL_CAPTCHA_INPUT,
            refresh_selector=S.SEL_CAPTCHA_REFRESH,
            image_selector=S.SEL_CAPTCHA_IMAGE,
            submit_action=_submit_and_check,
            is_rejected=_is_rejected,
            mode="ai",
            human_fallback=False,  # no client captcha UI on a subChallan doc
            terminal="cancelled",
            abort_message=f"captcha could not be read for {department}",
            **_SOLVE_EXTRA_KW,
        )
    except ScriptedAbort:
        ai_solved = False

    # OCR exhausted -> the operator finishes it on the live view. Cheaper than
    # failing a run a human can close in seconds, and still pre-payment.
    if not ai_solved:
        _log(ctx, "search.captcha_to_operator", StepStatus.HANDED_OFF)
        reply = await _wait_for_operator(
            ctx,
            (
                f"Virtual Courts ({department}): challan {challan} is filled. "
                "Read the CAPTCHA on screen, type it into 'Enter Captcha', click "
                "Submit, then send 'done'."
            ),
            OPERATOR_CAPTCHA_SECS,
        )
        if not reply:
            raise ScriptedAbort(
                "the captcha was not solved in time — nothing was paid, "
                "the request can be retried",
                terminal="cancelled",
                reason="captcha_operator_timeout",
            )
        deadline = time.monotonic() + RESULTS_POLL_SECS
        while time.monotonic() < deadline:
            if RESULTS_MARKER.lower() in await _page_signals(ctx):
                break
            await asyncio.sleep(1.0)

    # Read the outcome. Do NOT dismiss first: the not-found signal IS the
    # visible modal text.
    text = await _page_signals(ctx)
    if any(m in text for m in NOT_FOUND_MARKERS) and RESULTS_MARKER.lower() not in text:
        await _dismiss_modals(ctx, "search")
        raise ScriptedAbort(
            f"challan {challan} was not found on {department} — it may already "
            "be paid, or it belongs to a different court",
            terminal="cancelled",
            reason="not_found",
        )
    if RESULTS_MARKER.lower() not in text:
        raise ScriptedAbort(
            f"the Virtual Courts search for challan {challan} did not return a "
            "result — the portal may be down, please retry",
            terminal="cancelled",
            reason="no_response",
        )

    await _dismiss_modals(ctx, "search")
    _log(ctx, "search.results", StepStatus.OK, value=f"challan={challan}")


# ── phase 3: open the record and fill the verification fields ────────────


async def open_record(ctx: RunContext) -> None:
    p: ChallanPaymentParams = ctx.params

    # Searching by exact challan number yields a single record, so the lone
    # green "View" is unambiguous.
    await click_by_text(
        ctx.session, S.TXT_VIEW_BUTTON, tag="button", log=ctx.log, name="record.view"
    )
    await click(
        ctx.session, S.SEL_REVEAL_FIELDS, log=ctx.log, name="record.reveal_fields"
    )

    if not p.phoneNo:
        raise ScriptedAbort(
            "a mobile number is required to receive the Virtual Courts OTP — "
            "resend the request with phoneNo",
            terminal="cancelled",
            reason="missing_phone",
        )
    chassis = p.chassis_last4()
    if not chassis:
        raise ScriptedAbort(
            "the last 4 characters of the chassis number are required to verify "
            "the challan — resend the request with chassisNo",
            terminal="cancelled",
            reason="missing_chassis",
        )

    await wait_for_selector(
        ctx.session,
        S.SEL_OTP_MOBILE,
        log=ctx.log,
        name="record.wait_verify_fields",
        timeout=20,
    )
    await fill(
        ctx.session, S.SEL_OTP_MOBILE, p.phoneNo, log=ctx.log, name="record.fill_mobile"
    )
    await fill(
        ctx.session,
        S.SEL_CHASSIS_LAST4,
        chassis,
        log=ctx.log,
        name="record.fill_chassis_last4",
    )
    _log(ctx, "record.ready_for_payment", StepStatus.OK, value=f"mobile={p.phoneNo}")


# ── phase 4: operator does OTP + payment; we watch for the receipt ───────


def _any_negative(text: str) -> str | None:
    for pattern in S.NEGATIVE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None


async def _capture_and_upload_receipt(ctx: RunContext) -> RunOutcome:
    """Receipt page is on screen: generatingReceipt -> printToPDF -> upload.

    No field scraping. The Print button already proved a real receipt is
    rendered, and the PDF is the deliverable — receiptNumber/amount would only
    add a way to fail. (Same call the legacy flow made, for the same reason.)
    """
    p: ChallanPaymentParams = ctx.params
    department = ctx.scratch.get("department", "")

    await ctx.reporter.set_status(Status.GENERATING_RECEIPT)
    await sleep_seconds(RECEIPT_PAINT_SETTLE_SECS, log=ctx.log, name="receipt.settle")

    pdf_b64: str | None = None
    try:
        cdp = await ctx.session.get_or_create_cdp_session()
        resp = await asyncio.wait_for(
            cdp.cdp_client.send.Page.printToPDF(
                params={
                    "landscape": False,
                    "printBackground": True,
                    "preferCSSPageSize": True,
                },
                session_id=cdp.session_id,
            ),
            timeout=60,
        )
        pdf_b64 = (
            resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
        )
    except Exception as e:
        _log(ctx, "receipt.print_pdf", StepStatus.FAILED, error=f"{type(e).__name__}: {e}")

    if not pdf_b64 or len(pdf_b64) < 1400:
        return RunOutcome(
            status="partial",
            summary=(
                f"challan {p.challanNo} ({department}) was paid, but the receipt "
                "PDF could not be rendered — reconcile manually"
            ),
            error="receipt PDF render failed",
            payment_likely=True,
            run_log=ctx.log.dump(),
        )

    upload: dict = {}
    for attempt in range(1, 4):
        upload = await api_client.save_challan_receipt(
            job_id=ctx.job_id,
            request_id=p.requestId,
            driver_id=p.driverId,
            vehicle_number=p.vehicleNumber,
            challan_no=p.challanNo,
            department=department,
            pdf_base64=pdf_b64,
        )
        if upload.get("ok"):
            break
        _log(
            ctx,
            "receipt.upload",
            StepStatus.RETRIED,
            error=str(upload.get("error")),
        )
        await asyncio.sleep(2.0 * attempt)

    if not upload.get("ok"):
        return RunOutcome(
            status="partial",
            summary=(
                f"challan {p.challanNo} ({department}) was paid and the receipt "
                f"captured, but the upload failed ({upload.get('error', 'unknown')}) "
                "— reconcile manually"
            ),
            error="receipt upload failed",
            payment_likely=True,
            run_log=ctx.log.dump(),
        )

    return RunOutcome(
        status="done",
        summary=(
            f"challan {p.challanNo} paid on {department} for {p.vehicleNumber}; "
            "receipt captured"
        ),
        receipt_url=upload.get("url"),
        payment_likely=True,
        run_log=ctx.log.dump(),
    )


async def human_handover(ctx: RunContext) -> RunOutcome:
    p: ChallanPaymentParams = ctx.params
    department = ctx.scratch.get("department", "")

    wait_reason = (
        f"Human handover: challan {p.challanNo} ({department}) for "
        f"{p.vehicleNumber} is open with mobile {p.phoneNo} and the chassis "
        "digits filled. On the live view click 'Get OTP', enter the OTP the "
        f"driver receives on {p.phoneNo}, verify, and complete the payment. "
        f"{HANDOVER_SECS // 60} minutes."
    )
    await ctx.reporter.set_status(Status.HUMAN_HANDOVER, wait_reason=wait_reason)
    _log(ctx, "handover.entered", StepStatus.OK, value=f"timeout={HANDOVER_SECS}s")
    print(
        f"[{ctx.job_id}] handover: challan {p.challanNo} on {department}, "
        f"timeout={HANDOVER_SECS}s"
    )

    deadline = time.monotonic() + HANDOVER_SECS
    receipt_seen = False
    negative_hit: str | None = None
    poll = 0

    while time.monotonic() < deadline:
        poll += 1
        try:
            res = await cdp_eval(ctx.session, S.RECEIPT_READY_JS)
            if res and res.get("found"):
                receipt_seen = True
                _log(ctx, "handover.receipt_detected", StepStatus.OK, value=f"poll={poll}")
                break
            text = await page_text(ctx.session) or ""
            if text:
                hit = _any_negative(text)
                if hit:
                    negative_hit = hit
                    _log(
                        ctx,
                        "handover.negative_marker",
                        StepStatus.FAILED,
                        value=f"poll={poll}",
                        error=hit,
                    )
                    break
        except Exception as e:
            # Expected while the gateway navigates — only surface it if noisy.
            if poll % 20 == 0:
                print(f"[{ctx.job_id}] handover poll error #{poll}: {e}")
        if poll % 30 == 0:
            _log(ctx, f"handover.poll_{poll:04d}", StepStatus.OK, value="waiting")
        await asyncio.sleep(RECEIPT_POLL_INTERVAL_SECS)

    ctx.reporter.clear_wait()

    if negative_hit:
        return RunOutcome(
            status="failed",
            summary=(
                f"challan {p.challanNo} ({department}) was not paid — the portal "
                "reported the transaction as failed or pending. Verify with the "
                "bank before retrying."
            ),
            error=f"portal negative marker: {negative_hit}",
            abort_reason="payment_not_completed",
            payment_likely=True,
            run_log=ctx.log.dump(),
        )

    if not receipt_seen:
        _log(ctx, "handover.timeout", StepStatus.FAILED, error=f"no_receipt_in_{HANDOVER_SECS}s")
        return RunOutcome(
            status="failed",
            summary=(
                f"the handover for challan {p.challanNo} ({department}) expired "
                f"after {HANDOVER_SECS // 60} minutes with no receipt page — the "
                "operator may not have completed the payment. Reconcile before "
                "retrying."
            ),
            error="handover window expired without a receipt",
            abort_reason="handover_timeout",
            payment_likely=True,
            run_log=ctx.log.dump(),
        )

    return await _capture_and_upload_receipt(ctx)


# ── the pipeline ─────────────────────────────────────────────────────────

PHASES: list[Phase] = [
    Phase("open_portal", open_portal, enter_status=Status.AI_AGENT_STARTED, max_secs=180),
    Phase("search_challan", search_challan, max_secs=OPERATOR_CAPTCHA_SECS + 240),
    Phase("open_record", open_record, max_secs=180),
    Phase("human_handover", human_handover, max_secs=HANDOVER_SECS + 240),
]
