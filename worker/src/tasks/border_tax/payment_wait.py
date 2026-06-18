"""QR -> payment -> receipt, ported from main's _payment_wait.py v2 state
machine and made per-state via PaymentCaptureConfig.

The only true POSITIVE signal is explicit payment-success text; explicit
pending/failed text is NEGATIVE and fails fast. "Click here" links and the QR
disappearing are AMBIGUOUS (they appear on both success and failure pages)
and never advance the run. The receipt page is recognized only when ALL of
the state's receipt markers are present. A user's "paid" message is honored
only when a positive marker is actually on screen.

Phases:
  A  qrPaymentNeeded   capture QR -> app; poll 3s for positive / negative /
                       receipt within QR_WAIT_SECS (+ grace)
  B  verifyingPayment  click "click here" ONCE (skips the 60s auto-redirect
                       on the success page), then poll 2s for receipt vs
                       negative within PAYMENT_VERIFY_SECS
  C  generatingReceipt extract fields, print to PDF, upload (retries)

One deliberate divergence from main: this stack has no fetch-receipt
fallback task, so "Phase B timed out after a positive" maps to `partial`
(-> failed + manualReview reconcile) instead of optimistic `done`. Money is
never marked moved without a captured receipt or an explicit reviewer.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import api_client
from config import PAYMENT_VERIFY_SECS, QR_WAIT_SECS
from engine.canvas import capture_element_png
from engine.steps import cdp_eval, page_text, sleep_seconds, wait_for_selector
from engine.types import RunContext, RunOutcome, ScriptedAbort, StepLog, StepStatus
from lifecycle.status import Status
from redis_client import job_key

# Default marker patterns (Python regex, matched case-insensitively against
# document.body.innerText). Verbatim categories from main.
DEFAULT_POSITIVE_PATTERNS = [
    r"payment\s*successful",
    # SBI ePay success interstitial ("Your payment was successful",
    # Account Details status "Completed Successfully") — advancing here
    # lets Phase B's click-here skip the ~10s auto-redirect.
    r"payment\s*was\s*successful",
    r"completed\s*successfully",
    r"transaction\s*successful",
    r"successfully\s*paid",
    r"transaction\s*status\s*[:\-]?\s*success",
]
DEFAULT_NEGATIVE_PATTERNS = [
    # SBI ePay Lite Remittance Information Form (QR expired without payment)
    r"transaction\s*status\s*[:\-]?\s*pending",
    r"your\s*transaction\s*status\s*is\s*pending",
    # (REMOVED: transaction confirmation pending -> now a park trigger)
    r"transaction\s*status\s*[:\-]?\s*failed",
    r"transaction\s*failed",
    r"payment\s*failed",
]
_FINAL_GRACE_SECS = 6

# The Parivahan post-SBI redirect ("Transaction confirmation pending at the
# bank side"). Ambiguous — money may or may not have moved — so we PARK at
# verifyingPendingPayment instead of failing, and the verify job reconciles.
DEFAULT_PENDING_VERIFY_PATTERNS = [
    r"transaction\s*confirmation\s*pending",
]


@dataclass
class PaymentCaptureConfig:
    """Per-state config for the payment-wait + receipt-capture flow."""

    state_name: str
    qr_selector: str
    receipt_markers: list[str]  # ALL must appear (case-insensitive) in body
    positive_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_POSITIVE_PATTERNS)
    )
    negative_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_NEGATIVE_PATTERNS)
    )
    pending_verify_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_PENDING_VERIFY_PATTERNS)
    )
    payment_wait_secs: int = QR_WAIT_SECS
    verify_phase_secs: int = PAYMENT_VERIFY_SECS
    poll_interval_secs: float = 3.0
    marker_poll_secs: float = 2.0


async def _park_for_verify(
    ctx: RunContext, *, snippet: str, bank_ref: str | None
) -> RunOutcome:
    """Pending-confirmation screen hit. Flip verifyingPendingPayment (Firestore
    write via the reporter) and end the run as `parked` — run_job records it
    without a terminal, and the backend's sweeper later calls the verify API."""
    await ctx.reporter.set_status(
        Status.VERIFYING_PENDING_PAYMENT,
        error="bank-side confirmation pending — queued for verification",
    )
    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name="pay.park_for_verify",
            status=StepStatus.HANDED_OFF,
            value=snippet,
        )
    )
    return RunOutcome(
        status="parked",
        summary=(
            "the bank hasn't confirmed the payment yet — we'll verify it and "
            "send your receipt as soon as it clears"
        ),
        error=f'portal showed: "{snippet}"',
        transaction_id=bank_ref,
        payment_likely=True,  # genuinely unknown; lean toward "might have paid"
        run_log=ctx.log.dump(),
    )


def _any_pattern(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return p
    return None


def _find_with_snippet(patterns: list[str], text: str) -> tuple[str, str] | None:
    """(pattern, human snippet) for the first match. The snippet is the
    matched text plus a little context, whitespace-collapsed — THIS is what
    goes into user-facing errors; the regex itself stays in the run log."""
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            s, e = max(0, m.start() - 30), min(len(text), m.end() + 50)
            snippet = re.sub(r"\s+", " ", text[s:e]).strip()
            return p, snippet
    return None


# The SBI page prints its reference while we poll ("SBI Reference number
# CPAGVGRTP3") — scrape it opportunistically so even failures carry a
# transaction_id the ops side can reconcile against the bank.
_SBI_REF_RX = re.compile(
    r"SBI\s*Reference\s*(?:number|no)\.?\s*:?\s*([A-Z0-9]{6,})",
    re.IGNORECASE,
)


def _receipt_ready(markers: list[str], text: str) -> bool:
    low = text.lower()
    return bool(markers) and all(m.lower() in low for m in markers)


def _date_to_iso(d: str | None) -> str | None:
    if not d:
        return None
    try:
        return datetime.strptime(d, "%d-%b-%Y").date().isoformat()
    except ValueError:
        return d


_EXTRACT_JS = """
(function(){
  var t = document.body ? (document.body.innerText||'') : '';
  function m(re){ var x = t.match(re); return x ? x[1] : null; }
  return {
    receiptNumber: m(/Receipt\\s*No\\.?\\s*:?\\s*([A-Z0-9]+)/i),
    amountText: m(/Grand\\s*Total\\s*:?\\s*([0-9][0-9,]*(?:\\.[0-9]{1,2})?)/i),
    confDate: m(/Payment\\s*Confirmation\\s*Date\\s*:?\\s*(\\d{1,2}-[A-Za-z]{3}-\\d{4})/i),
    initDate: m(/Payment\\s*Initialization\\s*Date\\s*:?\\s*(\\d{1,2}-[A-Za-z]{3}-\\d{4})/i),
    anyDate: m(/(\\d{1,2}-[A-Za-z]{3}-\\d{4})/),
    refs: (t.match(/\\b[A-Z]{2,}[A-Z0-9]{6,}\\b/g) || []).slice(0, 30)
  };
})()
"""

# Score-light port of main's _try_click_here: first a/button whose text says
# "click here". Run ONCE at Phase B entry to skip the success page's
# auto-redirect; never used as a payment signal.
_CLICK_HERE_JS = """
(function(){
  var els = Array.from(document.querySelectorAll('a, button')).filter(
    function(e){ return /click\\s*here/i.test(e.innerText||''); });
  if(!els.length) return {ok:false};
  var el = els[0];
  var text = (el.innerText||'').trim().substring(0, 80);
  try { el.click(); } catch(e) { return {ok:false, text:text}; }
  return {ok:true, text:text};
})()
"""


async def _push_qr(ctx: RunContext, config: PaymentCaptureConfig) -> None:
    p = ctx.params
    await wait_for_selector(
        ctx.session, config.qr_selector, log=ctx.log, name="pay.wait_qr", timeout=40
    )
    await sleep_seconds(1.0, log=ctx.log, name="pay.qr_settle")

    b64 = await capture_element_png(ctx.session, config.qr_selector)
    if not b64:
        raise ScriptedAbort(
            "the UPI QR did not render at the payment gateway",
            terminal="failed",
        )


    amount = ctx.scratch.get("portalAmount")

    resp = await api_client.save_qr(
        request_id=p.requestId,
        driver_id=p.driverId,
        vehicle_number=p.vehicleNumber,
        image_base64=b64,
        portal_amount=amount,
    )
    if not resp.get("ok"):
        raise ScriptedAbort(
            f"could not deliver the UPI QR to the app "
            f"({resp.get('error', 'upload failed')})",
            terminal="failed",
        )

    ctx.reporter.sync_local(Status.QR_PAYMENT_NEEDED)
    ctx.r.hset(job_key(ctx.job_id), "qrCodeUrl", resp.get("url", ""))

    # amount = ctx.scratch.get("portalAmount")
    ctx.reporter.set_wait(
        f"UPI payment required for border tax of vehicle {p.vehicleNumber} "
        f"entering {config.state_name}"
        f"{' (₹' + str(amount) + ')' if amount else ''}. A QR code is shown "
        f"in the app — scan with any UPI app and complete the payment "
        f"within ~{config.payment_wait_secs // 60} minutes.",
    )
    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name="pay.qr_pushed",
            status=StepStatus.OK,
            url=resp.get("url", ""),
        )
    )


def _consume_paid_input(ctx: RunContext) -> bool:
    val = ctx.r.hget(job_key(ctx.job_id), "humanInput")
    if val:
        ctx.r.hdel(job_key(ctx.job_id), "humanInput")
        return str(val).strip().lower() in ("paid", "done", "payment done")
    return False


async def wait_for_payment_and_capture(
    ctx: RunContext,
    *,
    config: PaymentCaptureConfig,
) -> RunOutcome:
    p = ctx.params
    await _push_qr(ctx, config)

    # ── Phase A: the QR window ─────────────────────────────────────────
    advance_reason: str | None = None
    bank_ref: str | None = None
    deadline = time.monotonic() + config.payment_wait_secs + _FINAL_GRACE_SECS
    poll_n = 0
    while time.monotonic() < deadline:
        poll_n += 1
        if ctx.r.hget(job_key(ctx.job_id), "status") == "cancelled":
            return RunOutcome(
                status="failed",
                summary="cancelled while the payment window was open — "
                "verify with the bank before retrying",
                error="cancelled during payment window",
                payment_likely=False,
                run_log=ctx.log.dump(),
            )

        text = await page_text(ctx.session)
        if bank_ref is None:
            m = _SBI_REF_RX.search(text)
            if m:
                bank_ref = m.group(1)
                ctx.log.record(
                    StepLog(
                        index=ctx.log.next_index(),
                        name="pay.sbi_reference",
                        status=StepStatus.OK,
                        value=bank_ref,
                    )
                )

        if _receipt_ready(config.receipt_markers, text):
            advance_reason = "receipt_markers"
            break

        pend = _find_with_snippet(config.pending_verify_patterns, text)
        if pend:
            return await _park_for_verify(ctx, snippet=pend[1], bank_ref=bank_ref)

        neg = _find_with_snippet(config.negative_patterns, text)
        if neg:
            pattern, snippet = neg
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="pay.negative_marker",
                    status=StepStatus.FAILED,
                    value=f"/{pattern}/",
                    error=snippet,
                )
            )
            return RunOutcome(
                status="failed",
                summary=(
                    f"border tax payment for vehicle {p.vehicleNumber} "
                    f"entering {config.state_name} was NOT completed — the "
                    f"driver most likely did not complete the UPI payment"
                    + (f" (SBI ref {bank_ref})" if bank_ref else "")
                ),
                error=f'the bank page showed: "{snippet}"',
                transaction_id=bank_ref,
                payment_likely=False,
                run_log=ctx.log.dump(),
            )

        pos = _find_with_snippet(config.positive_patterns, text)
        if pos:
            advance_reason = f'positive: "{pos[1]}"'
            break

        if _consume_paid_input(ctx):
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="pay.user_said_paid",
                    status=StepStatus.RETRIED,
                    error="no success marker on screen yet — still waiting",
                )
            )

        await asyncio.sleep(config.poll_interval_secs)

    if advance_reason is None:
        return RunOutcome(
            status="failed",
            summary=(
                f"no payment-success or payment-failure marker appeared "
                f"within {config.payment_wait_secs // 60} minutes — most "
                f"likely the driver did not initiate the UPI payment"
            ),
            error="the payment window elapsed with no activity on the bank page",
            transaction_id=bank_ref,
            payment_likely=False,
            run_log=ctx.log.dump(),
        )

    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name="pay.phase_a.advanced",
            status=StepStatus.OK,
            value=advance_reason,
        )
    )

    # ── Phase B: verify & reach the receipt page ───────────────────────
    ctx.reporter.clear_wait()
    await ctx.reporter.set_status(Status.VERIFYING_PAYMENT)

    if advance_reason != "receipt_markers":
        try:
            click_res = await cdp_eval(ctx.session, _CLICK_HERE_JS)
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="pay.phase_b.click_here",
                    status=StepStatus.OK,
                    value=(
                        f"ok={click_res.get('ok')} text={click_res.get('text')!r}"
                        if click_res
                        else "not found; waiting for auto-redirect"
                    ),
                )
            )
        except Exception:
            pass

        verify_deadline = time.monotonic() + config.verify_phase_secs
        receipt_seen = False
        while time.monotonic() < verify_deadline:
            text = await page_text(ctx.session)
            if bank_ref is None:
                m = _SBI_REF_RX.search(text)
                if m:
                    bank_ref = m.group(1)
            if _receipt_ready(config.receipt_markers, text):
                receipt_seen = True
                break

            pend = _find_with_snippet(config.pending_verify_patterns, text)
            if pend:
                return await _park_for_verify(ctx, snippet=pend[1], bank_ref=bank_ref)

            neg = _find_with_snippet(config.negative_patterns, text)
            if neg:
                pattern, snippet = neg
                ctx.log.record(
                    StepLog(
                        index=ctx.log.next_index(),
                        name="pay.negative_after_redirect",
                        status=StepStatus.FAILED,
                        value=f"/{pattern}/",
                        error=snippet,
                    )
                )
                return RunOutcome(
                    status="failed",
                    summary=(
                        f"border tax payment for vehicle {p.vehicleNumber} "
                        f"entering {config.state_name} was NOT completed — "
                        f"after the QR page redirected, the portal still "
                        f"reported no successful payment"
                        + (f" (SBI ref {bank_ref})" if bank_ref else "")
                    ),
                    error=f'the portal showed: "{snippet}"',
                    transaction_id=bank_ref,
                    payment_likely=False,
                    run_log=ctx.log.dump(),
                )
            await asyncio.sleep(config.marker_poll_secs)

        if not receipt_seen:
            return RunOutcome(
                status="partial",
                summary=(
                    "payment success was reported but the receipt page never "
                    "rendered — reconcile manually"
                    + (f" (SBI ref {bank_ref})" if bank_ref else "")
                ),
                error="receipt page not reached after positive marker",
                transaction_id=bank_ref,
                payment_likely=True,
                run_log=ctx.log.dump(),
            )

    # ── Phase C: extract, print, upload ────────────────────────────────
    return await capture_and_upload_receipt(ctx, config=config, bank_ref=bank_ref)


async def capture_and_upload_receipt(
    ctx: RunContext, *, config: PaymentCaptureConfig, bank_ref: str | None = None
) -> RunOutcome:
    """Phase C, standalone: set generatingReceipt, extract fields, printToPDF,
    save-receipt (retries). Returns done / partial. Used by the normal payment
    flow AND the verify-pending job once a receipt is on screen."""
    await ctx.reporter.set_status(Status.GENERATING_RECEIPT)
    await sleep_seconds(1.5, log=ctx.log, name="receipt.settle")
    p = ctx.params

    fields: dict = {}
    try:
        raw = await cdp_eval(ctx.session, _EXTRACT_JS) or {}
        amount = None
        if raw.get("amountText"):
            try:
                amount = float(str(raw["amountText"]).replace(",", ""))
            except ValueError:
                amount = None
        date_raw = raw.get("confDate") or raw.get("initDate") or raw.get("anyDate")
        refs = [
            ref
            for ref in (raw.get("refs") or [])
            if any(c.isdigit() for c in ref)
            and ref != p.vehicleNumber
            and ref != (raw.get("receiptNumber") or "")
        ]
        fields = {
            "receiptNumber": raw.get("receiptNumber"),
            "amount": amount,
            "paymentDate": _date_to_iso(date_raw),
            "bankRef": (refs[0] if refs else None) or bank_ref,
            "vehicleNumber": p.vehicleNumber,
        }
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="receipt.extract",
                status=StepStatus.OK,
                value=str({k: v for k, v in fields.items() if v}),
            )
        )
    except Exception as e:
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="receipt.extract",
                status=StepStatus.RETRIED,
                error=f"{type(e).__name__}: {e}",
            )
        )

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
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="receipt.print_pdf",
                status=StepStatus.FAILED,
                error=f"{type(e).__name__}: {e}",
            )
        )

    if not pdf_b64 or len(pdf_b64) < 1400:
        return RunOutcome(
            status="partial",
            summary="payment confirmed but the receipt PDF could not be "
            "rendered — reconcile manually",
            error="receipt PDF render failed",
            payment_likely=True,
            receipt_number=fields.get("receiptNumber"),
            amount=fields.get("amount"),
            transaction_id=fields.get("bankRef"),
            run_log=ctx.log.dump(),
        )

    upload: dict = {}
    for attempt in range(1, 4):
        upload = await api_client.save_receipt(
            request_id=p.requestId,
            driver_id=p.driverId,
            pdf_base64=pdf_b64,
            fields=fields,
        )
        if upload.get("ok"):
            break
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="receipt.upload",
                status=StepStatus.RETRIED,
                attempt=attempt,
                error=str(upload.get("error")),
            )
        )
        await asyncio.sleep(2.0 * attempt)

    if not upload.get("ok"):
        return RunOutcome(
            status="partial",
            summary="payment confirmed and receipt captured, but the upload "
            f"failed ({upload.get('error', 'unknown')}) — reconcile "
            "manually",
            error="receipt upload failed",
            payment_likely=True,
            receipt_number=fields.get("receiptNumber"),
            amount=fields.get("amount"),
            transaction_id=fields.get("bankRef"),
            run_log=ctx.log.dump(),
        )

    receipt_no = fields.get("receiptNumber") or "receipt"
    return RunOutcome(
        status="done",
        summary=f"border tax paid for {p.vehicleNumber}; receipt "
        f"{receipt_no} captured"
        + (f", amount ₹{fields['amount']}" if fields.get("amount") else ""),
        receipt_url=upload.get("url"),
        receipt_number=fields.get("receiptNumber"),
        amount=fields.get("amount"),
        transaction_id=fields.get("bankRef"),
        payment_likely=True,
        run_log=ctx.log.dump(),
    )
