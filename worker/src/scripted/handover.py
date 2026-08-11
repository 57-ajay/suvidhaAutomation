# worker/src/scripted/handover.py
"""Human handover + background receipt capture for the SCRIPTED (web) path.

The wizard stops on the Disclaimer page (captcha + Pay Online — just BEFORE
the payment gateway). This phase then:

  1. Sets lifecycle status `humanHandover` (Firestore, via the reporter) and
     Redis job status `waiting_for_human` with a waitReason, so the ops
     console surfaces the job and the operator opens the live view.
  2. The OPERATOR solves the captcha, clicks Pay Online, picks the payment
     method on the gateway (UPI / net banking / whatever the state offers),
     and completes the payment themselves. We never block on an explicit
     human "done" — they may pay and never notify us.
  3. Meanwhile this phase polls the page every few seconds, for up to
     WEB_HANDOVER_TIMEOUT_SECS (env, default 7 min), for either:
       - a NEGATIVE marker (portal says the transaction failed / is pending)
         -> failed (manualReview; money may have moved), or
       - the RECEIPT page markers (ALL must appear, case-insensitive)
         -> generatingReceipt -> extract fields -> printToPDF ->
            save-receipt (retries) -> done.
     No decisive signal by the deadline -> failed (manualReview): the
     operator held the payment page, so a silent cancel is never safe.

Ported from the old repo's _web_handover.py, adapted to the autoScale stack:
StatusReporter + the humanHandover FSM status instead of raw redis writes,
api_client.save_receipt instead of the old actions.py endpoint, and the
generic receipt-field extractor from the proven payment_wait (a deliberate
copy — module isolation over DRY, by decision).

Intentional change vs old: NO QR polling. On this path the operator makes the
payment (per product decision); if they pick UPI, they scan the on-screen QR
with their own device. Re-adding driver-facing QR push is a documented
follow-up (TASK.md).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime

import api_client
from config import WEB_HANDOVER_TIMEOUT_SECS
from engine.pipeline import Phase
from engine.steps import cdp_eval, current_url, page_text, sleep_seconds
from engine.types import RunContext, RunOutcome, StepLog, StepStatus
from lifecycle.status import Status

RECEIPT_POLL_INTERVAL_SECS = 4.0

DEFAULT_NEGATIVE_PATTERNS = [
    r"transaction\s*status\s*[:\-]?\s*pending",
    r"your\s*transaction\s*status\s*is\s*pending",
    r"transaction\s*confirmation\s*pending",
    r"transaction\s*status\s*[:\-]?\s*failed",
    r"transaction\s*failed",
    r"payment\s*failed",
]


@dataclass
class HandoverConfig:
    """Per-state config for the handover + receipt watch."""

    state_name: str
    # ALL must appear (case-insensitive) in the body text — same semantics as
    # the auto path's payment_wait._receipt_ready.
    receipt_markers: list[str]
    negative_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_NEGATIVE_PATTERNS)
    )


# ─── Receipt field extraction (copied from the proven payment_wait) ───────

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


# ─── Multi-signal receipt detection ──────────────────────────────────────
# The old rule (ALL of config.receipt_markers must appear) was blinded by a
# single string drifting: MP's receipt header dropped "Government of" and
# UK's is spelled "UTTRAKHAND" by the portal itself — every receipt then
# timed out WITH the receipt on screen. Detection now accepts several
# independent 2-signal combinations, each receipt-page-exclusive (verified
# against real UP/HR/PB/MP/HP/UK receipt PDFs, all tax modes sampled DAYS):
#
#   * a "Receipt No : <XX>R<digits>" extraction  + any receipt phrase
#   * all three receipt phrases together (survives a receipt-no relabel)
#   * the receipt ROUTE in the URL (#/public/reports/CustomerReceipt[Xx])
#     + any receipt phrase (survives wholesale body-text redesign)
#   * the legacy full marker set from config (backstop, unchanged behavior)
#
# A blank receipt shell (route loaded, data not rendered yet) matches
# nothing and keeps polling — every accepting path needs page CONTENT.

import re as _re

_RECEIPT_NO_RX = _re.compile(r"receipt\s*no\.?\s*:?\s*[A-Z]{2,4}[0-9]{8,}", _re.I)
_RECEIPT_PHRASES = [
    "checkpost tax e-receipt",
    "grand total",
    "genuinity of the receipt",
]
_RECEIPT_URL_MARKER = "customerreceipt"


def _receipt_signals(text: str, url: str, markers: list[str]) -> str | None:
    """Return a short reason string when the page is a tax receipt, else None."""
    low = (text or "").lower()
    phrases = [p for p in _RECEIPT_PHRASES if p in low]
    has_no = bool(_RECEIPT_NO_RX.search(text or ""))
    if has_no and phrases:
        return f"receipt_no+{phrases[0]}"
    if len(phrases) == len(_RECEIPT_PHRASES):
        return "all_receipt_phrases"
    if _RECEIPT_URL_MARKER in (url or "").lower() and phrases:
        return f"url+{phrases[0]}"
    if markers and all(m.lower() in low for m in markers):
        return "legacy_markers"
    return None


def _any_pattern(patterns: list[str], text: str) -> str | None:
    import re

    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return p
    return None


def _date_to_iso(d: str | None) -> str | None:
    if not d:
        return None
    try:
        return datetime.strptime(d, "%d-%b-%Y").date().isoformat()
    except ValueError:
        return d


async def _capture_and_upload_receipt(
    ctx: RunContext, *, config: HandoverConfig
) -> RunOutcome:
    """Receipt page is on screen: set generatingReceipt, extract fields,
    printToPDF, save-receipt (retries). Returns done / partial. Copied from
    payment_wait.capture_and_upload_receipt (module isolation over DRY)."""
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
            "bankRef": refs[0] if refs else None,
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
            f"failed ({upload.get('error', 'unknown')}) — reconcile manually",
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
        summary=f"border tax paid for {p.vehicleNumber} (human handover); "
        f"receipt {receipt_no} captured"
        + (f", amount ₹{fields['amount']}" if fields.get("amount") else ""),
        receipt_url=upload.get("url"),
        receipt_number=fields.get("receiptNumber"),
        amount=fields.get("amount"),
        transaction_id=fields.get("bankRef"),
        payment_likely=True,
        run_log=ctx.log.dump(),
    )


# ─── The phase ────────────────────────────────────────────────────────────


def make_handover_phase(config: HandoverConfig) -> Phase:
    """Build the human-handover phase for a state. The phase's max_secs is
    the handover window + capture/upload headroom, so a config bump via env
    never trips the pipeline watchdog."""

    async def human_handover(ctx: RunContext) -> RunOutcome:
        p = ctx.params
        timeout_secs = WEB_HANDOVER_TIMEOUT_SECS
        wait_reason = (
            f"Human handover: complete the border tax payment for vehicle "
            f"{p.vehicleNumber} entering {config.state_name} on the live "
            f"view — solve the captcha, pick a payment method, and pay. "
            f"{timeout_secs // 60} minutes."
        )
        # Firestore -> humanHandover; Redis -> waiting_for_human + waitReason
        # in the same reporter call, so the console and the client see the
        # handover the moment it starts.
        await ctx.reporter.set_status(
            Status.HUMAN_HANDOVER, wait_reason=wait_reason
        )
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="handover.entered",
                status=StepStatus.OK,
                value=f"timeout={timeout_secs}s state={config.state_name}",
            )
        )
        print(
            f"[{ctx.job_id}] handover: started for {p.vehicleNumber} "
            f"entering {config.state_name}, timeout={timeout_secs}s"
        )

        deadline = time.monotonic() + timeout_secs
        receipt_seen = False
        negative_hit: str | None = None
        poll = 0

        while time.monotonic() < deadline:
            poll += 1
            try:
                text = await page_text(ctx.session)
                if text:
                    hit = _any_pattern(config.negative_patterns, text)
                    if hit:
                        negative_hit = hit
                        ctx.log.record(
                            StepLog(
                                index=ctx.log.next_index(),
                                name="handover.negative_marker",
                                status=StepStatus.FAILED,
                                value=f"poll={poll}",
                                error=hit,
                            )
                        )
                        break
                    try:
                        url = await current_url(ctx.session)
                    except Exception:
                        url = ""
                    reason = _receipt_signals(text, url, config.receipt_markers)
                    if reason:
                        receipt_seen = True
                        ctx.log.record(
                            StepLog(
                                index=ctx.log.next_index(),
                                name="handover.receipt_detected",
                                status=StepStatus.OK,
                                value=f"poll={poll} via {reason}",
                            )
                        )
                        break
            except Exception as e:
                # Page navigating / CDP hiccup — expected during gateway hops.
                if poll % 20 == 0:
                    print(f"[{ctx.job_id}] handover poll error #{poll}: {e}")
            if poll % 30 == 0:
                ctx.log.record(
                    StepLog(
                        index=ctx.log.next_index(),
                        name=f"handover.poll_{poll:04d}",
                        status=StepStatus.OK,
                        value=f"t={int(time.monotonic() - (deadline - timeout_secs))}s waiting",
                    )
                )
            await asyncio.sleep(RECEIPT_POLL_INTERVAL_SECS)

        # Back under worker control either way.
        ctx.reporter.clear_wait()

        if negative_hit:
            return RunOutcome(
                status="failed",
                summary=(
                    f"border tax payment for {p.vehicleNumber} entering "
                    f"{config.state_name} was not completed — the portal "
                    "reported the transaction as pending or failed. Verify "
                    "with the bank before retrying."
                ),
                error=f"portal negative marker: {negative_hit}",
                abort_reason="web_handover_payment_not_completed",
                payment_likely=True,
                run_log=ctx.log.dump(),
            )

        if not receipt_seen:
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="handover.timeout",
                    status=StepStatus.FAILED,
                    error=f"no_receipt_in_{timeout_secs}s",
                )
            )
            return RunOutcome(
                status="failed",
                summary=(
                    f"border tax handover for {p.vehicleNumber} entering "
                    f"{config.state_name} timed out after "
                    f"{timeout_secs // 60} minutes with no receipt page — "
                    "the operator may not have completed the payment. "
                    "Reconcile manually before retrying."
                ),
                error="handover window expired without a receipt",
                abort_reason="web_handover_timeout",
                payment_likely=True,
                run_log=ctx.log.dump(),
            )

        return await _capture_and_upload_receipt(ctx, config=config)

    return Phase(
        "human_handover",
        human_handover,
        max_secs=WEB_HANDOVER_TIMEOUT_SECS + 180,
    )
