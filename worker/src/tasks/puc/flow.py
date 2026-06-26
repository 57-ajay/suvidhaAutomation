"""PUC-certificate runner.

Three phases on the shared engine.pipeline:

  open_portal        navigate to the PUC form, wait for #regnNumber. No status
                     write of its own, so the whole-flow RestartFrom (-> here)
                     never asserts an illegal backward transition.
  fetch_certificate  fill reg + chassis, solve the captcha (AI-silent or human),
                     click "PUC Details". Distinguishes results / not-found /
                     wrong-captcha. A definitive not-found is a terminal cancel;
                     the captcha solver handles wrong-captcha internally.
  print_and_capture  click "Print" -> Form-59 page -> printToPDF -> save_receipt.
                     generatingReceipt is entered only AFTER the PDF is in hand,
                     so a failure before it rewinds cleanly (cancel-able), and
                     the committed section can only complete or, on a pure
                     storage failure, fail (manualReview). No money ever moves.

Whole-flow retry: run via run_phases(..., max_restarts=PUC_MAX_RETRIES). Transient
failures raise RestartFrom("open_portal"); the cap turns exhaustion into a clean
cancel.
"""

from __future__ import annotations

import asyncio
import time

import api_client
from engine.pipeline import Phase
from engine.steps import (
    cdp_eval,
    click,
    click_by_text,
    fill,
    navigate,
    sleep_seconds,
    wait_for_selector,
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

# Shared captcha policy (AI-silent OCR -> human fallback), lives with border-tax
# but is task-agnostic. ctx.task routes its DB writes to the right collection.
from tasks.border_tax.captcha_user import solve_captcha

from . import selectors as S

# Tunables (small; the job-level ceiling and the captcha budgets already bound
# the run). Kept local rather than env so this stays a drop-in.
SUBMIT_SETTLE_SECS = 30.0  # wait for the PUC-Details postback/reload to settle
FORM59_WAIT_SECS = 45.0  # the printable certificate page can be slow
PRINTPDF_TIMEOUT = 60
SAVE_RECEIPT_ATTEMPTS = 3


# ── helpers ─────────────────────────────────────────────────────────────────


async def _fill_reg_chassis(ctx: RunContext) -> None:
    """Fill the two text fields. Called once upfront (before the captcha is
    captured) and again from submit_action only if a postback wiped them, so we
    don't retrigger a field-change postback that would refresh the captcha."""
    p = ctx.params
    await fill(ctx.session, S.SEL_REGN, p.regn, log=ctx.log, name="puc.fill_regn")
    await fill(
        ctx.session, S.SEL_CHASSIS, p.chassisLast5, log=ctx.log, name="puc.fill_chassis"
    )


async def _read_state(ctx: RunContext) -> dict:
    # During the PUC-Details POST the page reloads; cdp_eval can transiently
    # fail mid-navigation. Treat that as "pending" so the caller keeps polling.
    try:
        st = await cdp_eval(ctx.session, S.SUBMIT_STATE_JS)
    except Exception:
        return {"state": "pending"}
    return st or {"state": "pending"}


async def _print_to_pdf(ctx: RunContext) -> str | None:
    """CDP printToPDF of the current (Form-59) page. Mirrors the border-tax
    receipt capture. Returns base64 or None."""
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
            timeout=PRINTPDF_TIMEOUT,
        )
        return (
            resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
        )
    except Exception as e:
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="puc.print_pdf",
                status=StepStatus.FAILED,
                error=f"{type(e).__name__}: {e}",
            )
        )
        return None


# ── phases ──────────────────────────────────────────────────────────────────


async def open_portal(ctx: RunContext) -> None:
    # Fresh attempt: clear the per-run scratch flags the fetch phase may set.
    ctx.scratch.pop("puc_not_found", None)
    ctx.scratch.pop("puc_msg", None)
    try:
        await navigate(
            ctx.session, S.PORTAL_URL, log=ctx.log, name="puc.open", timeout=60
        )
        await wait_for_selector(
            ctx.session, S.SEL_REGN, log=ctx.log, name="puc.wait_form", timeout=30
        )
        await sleep_seconds(0.8, log=ctx.log, name="puc.settle")
    except Exception as e:
        raise RestartFrom("open_portal", f"PUC form did not load: {type(e).__name__}")


async def fetch_certificate(ctx: RunContext) -> None:
    p = ctx.params

    async def submit() -> bool:
        # The captcha solver has just filled #txtCaptchaId (fill() already fired
        # input+change, so the value is committed). Only (re)fill reg + chassis
        # if a postback actually wiped them. Do NOT re-touch them otherwise: a
        # change event on reg/chassis triggers the portal's field postback, which
        # REFRESHES the captcha image and would invalidate the answer we just
        # typed — that is exactly what caused the refresh-on-every-attempt loop.
        if not await cdp_eval(ctx.session, S.FIELDS_FILLED_JS):
            await _fill_reg_chassis(ctx)
        # Click "PUC Details". This is the same click primitive the border-tax
        # runners use to post their (PrimeFaces) forms; here it submits the
        # full-page POST. CLICK_SUBMIT_JS is a fallback if the element can't be
        # resolved.
        try:
            await click(
                ctx.session,
                S.SEL_SUBMIT,
                log=ctx.log,
                name="puc.click_details",
                timeout=10,
            )
        except Exception:
            await cdp_eval(ctx.session, S.CLICK_SUBMIT_JS)
        deadline = time.monotonic() + SUBMIT_SETTLE_SECS
        while time.monotonic() < deadline:
            st = await _read_state(ctx)
            state = st.get("state")
            if state == "results":
                return True
            if state == "not_found":
                # Captcha was fine; there is simply no certificate. Flag it and
                # report "advanced" so the solver stops — the phase aborts below.
                ctx.scratch["puc_not_found"] = True
                ctx.scratch["puc_msg"] = st.get("msg", "")
                return True
            if state == "captcha_error":
                return False
            await asyncio.sleep(0.5)
        return False  # nothing decisive -> treat as not-advanced; solver re-evaluates

    async def rejected() -> bool:
        st = await _read_state(ctx)
        return st.get("state") == "captcha_error"

    # Fill reg + chassis FIRST so the captcha is captured and answered LAST.
    # fill() fires input+change, so if the portal does a field-change postback
    # on these (which is what refreshes the captcha image), it happens NOW —
    # before the solver grabs the captcha — and the settle below lets it land.
    # The captcha the solver then reads is the post-refresh one, so the answer
    # stays valid through submit.
    await _fill_reg_chassis(ctx)
    await sleep_seconds(1.5, log=ctx.log, name="puc.fields_settle")

    try:
        # img-based captcha (like the Himkosh ASP.NET one) -> image_selector
        # routes capture to capture_element_png. Exhaustion is a safe cancel
        # (no money moved) -> solve_captcha's default terminal is correct.
        # stage="disclaimer" reuses the known stage->captchaSolving mapping so
        # no API stage-map change is needed; swap to a dedicated "puc" stage if
        # you prefer a distinct label (one line in api/src/internal/captcha.ts).
        await solve_captcha(
            ctx,
            status=Status.CAPTCHA_SOLVING,
            stage="disclaimer",
            input_selector=S.SEL_CAPTCHA_INPUT,
            refresh_selector=S.SEL_CAPTCHA_REFRESH,
            submit_action=submit,
            is_rejected=rejected,
            image_selector=S.SEL_CAPTCHA_IMG,
            terminal="cancelled",
        )
    except ScriptedAbort:
        raise  # captcha exhausted (terminal) — not a whole-flow retry
    except RestartFrom:
        raise
    except Exception as e:
        raise RestartFrom("open_portal", f"fetch failed: {type(e).__name__}: {e}")

    if ctx.scratch.get("puc_not_found"):
        reg = p.regn
        msg = (ctx.scratch.get("puc_msg") or "").strip()
        raise ScriptedAbort(
            f"no PUC certificate is on record for {reg} (chassis …{p.chassisLast5}) "
            "on the parivahan portal" + (f" — portal said: {msg}" if msg else ""),
            terminal="cancelled",
        )


async def print_and_capture(ctx: RunContext) -> RunOutcome:
    p = ctx.params

    # ── pre-commit: get the printable certificate on screen and into a PDF ──
    try:
        await click_by_text(
            ctx.session, "Print", log=ctx.log, name="puc.click_print", tag="button"
        )
        # Form-59 renders on pucPublicNew.xhtml; wait on its text markers
        # (more robust than the URL).
        deadline = time.monotonic() + FORM59_WAIT_SECS
        ready = False
        while time.monotonic() < deadline:
            if await cdp_eval(ctx.session, S.FORM59_READY_JS):
                ready = True
                break
            await asyncio.sleep(0.6)
        if not ready:
            raise RestartFrom("open_portal", "Form-59 certificate page never rendered")

        await sleep_seconds(1.0, log=ctx.log, name="puc.form59_settle")

        fields: dict = {
            "registrationNumber": p.regn,
            "chassisLast5": p.chassisLast5,
        }
        try:
            scraped = await cdp_eval(ctx.session, S.EXTRACT_FIELDS_JS) or {}
            fields.update({k: v for k, v in scraped.items() if v})
        except Exception:
            pass  # best-effort; the PDF is the artifact

        pdf_b64 = await _print_to_pdf(ctx)
        if not pdf_b64 or len(pdf_b64) < 1400:
            raise RestartFrom("open_portal", "printToPDF produced no/tiny output")
    except RestartFrom:
        raise
    except ScriptedAbort:
        raise
    except Exception as e:
        raise RestartFrom(
            "open_portal", f"print/capture failed: {type(e).__name__}: {e}"
        )

    # ── committed: PDF is in hand. From here we only complete or, on a pure
    #    storage failure, fail (manualReview). Never cancel, never rewind —
    #    those would be illegal transitions out of generatingReceipt. ──────
    await ctx.reporter.set_status(Status.GENERATING_RECEIPT)
    await sleep_seconds(0.5, log=ctx.log, name="puc.generating_receipt")

    upload: dict = {}
    for attempt in range(1, SAVE_RECEIPT_ATTEMPTS + 1):
        upload = await api_client.save_receipt(
            request_id=p.requestId,
            driver_id=p.driverId,
            pdf_base64=pdf_b64,
            fields=fields,
            task="puc-certificate",
        )
        if upload.get("ok"):
            break
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="puc.save_receipt",
                status=StepStatus.RETRIED,
                attempt=attempt,
                error=str(upload.get("error")),
            )
        )
        await asyncio.sleep(2.0 * attempt)

    if not upload.get("ok"):
        # We have the certificate but couldn't store it — a storage problem,
        # not a portal one. Surface for manual handling rather than a silent
        # cancel (the run genuinely produced a result).
        raise ScriptedAbort(
            "captured the PUC certificate but the upload failed "
            f"({upload.get('error', 'unknown')}) — needs manual handling",
            terminal="failed",
        )

    sl = fields.get("certificateSlNo")
    return RunOutcome(
        status="done",
        summary=(
            f"PUC certificate captured for {p.regn}" + (f" (SL No. {sl})" if sl else "")
        ),
        receipt_url=upload.get("url"),
        receipt_number=sl,
        run_log=ctx.log.dump(),
    )


# open_portal carries enter_status only so the very first entry is a no-op
# (status is already aiAgentStarted, set in run_job). It is deliberately NOT
# re-asserted here, so RestartFrom -> open_portal never writes a backward
# transition. Per-phase deadlines bound each step; the job-level ceiling and the
# captcha budget bound the whole run.
PHASES: list[Phase] = [
    Phase("open_portal", open_portal, max_secs=120),
    Phase("fetch_certificate", fetch_certificate, max_secs=600),
    Phase("print_and_capture", print_and_capture, max_secs=180),
]
