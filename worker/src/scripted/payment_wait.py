# worker/src/scripted/payment_wait.py
"""Wait for the UPI payment, then capture the receipt.

State machine (app + UPI):
  - Capture the QR PNG, upload via API (sets qrPaymentNeeded), set waiting_for_human.
  - Poll the page concurrently for:
      * receipt markers      -> capture receipt PNG, return done
      * positive markers     -> payment confirmed, keep polling for receipt
      * negative markers      -> 'pending'/'NA'/'failed' (your images 3-4) -> failed
      * humanInput == 'paid' -> user assertion; only trusted if a positive marker
                                also appears (never overrides a negative marker)
  - Honor a hard QR_WAIT_SECS budget. Timeout with no receipt -> failed.

If the portal shows the intermediate 'click here' link before redirecting to the
receipt, we click it. The SBI reference (e.g. 'CPAGTEUVE3') is scraped if present
and returned as transaction_id even on failure, for reconciliation.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import redis

from .log import StepLogger
from .steps import _cdp_eval, _current_url
from .types import RunOutcome, StepLog, StepStatus
from redis_client import job_key
from config import QR_WAIT_SECS, PAYMENT_VERIFY_SECS
import api_client


POSITIVE_MARKERS = [
    r"payment\s*success", r"transaction\s*success", r"successfully\s*paid",
    r"e-?receipt", r"receipt\s*no",
]
NEGATIVE_MARKERS = [
    r"transaction\s*status\s*[:\-]?\s*pending",
    r"transaction\s*confirmation\s*pending",
    r"pending\s*at\s*the\s*bank",
    r"\(na\)",
    r"transaction\s*failed", r"payment\s*failed",
]
RECEIPT_MARKERS = ["e-receipt", "receipt no", "grand total", "checkpost tax"]


async def _page_text(session) -> str:
    try:
        return (await _cdp_eval(session, "return document.body.innerText || ''")) or ""
    except Exception:
        return ""


def _any(patterns: list[str], text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


async def _capture_png(session, selector: str | None = None) -> str | None:
    """Capture a full-page or element PNG as base64 (no prefix)."""
    cdp = getattr(session, "cdp_client", None)
    if cdp is None:
        return None
    params: dict = {"format": "png", "captureBeyondViewport": True}
    if selector:
        try:
            rect = await _cdp_eval(
                session,
                f"var e=document.querySelector({json.dumps(selector)});"
                f"if(!e) return null; var r=e.getBoundingClientRect();"
                f"return {{x:r.x,y:r.y,w:r.width,h:r.height}};",
            )
            if rect and rect.get("w"):
                params["clip"] = {"x": rect["x"], "y": rect["y"],
                                  "width": rect["w"], "height": rect["h"], "scale": 1}
        except Exception:
            pass
    try:
        shot = await cdp.send("Page.captureScreenshot", params)
        return shot.get("data")
    except Exception:
        return None


async def _scrape_bank_ref(session) -> str | None:
    txt = await _page_text(session)
    m = re.search(r"\b([A-Z]{2,}[A-Z0-9]{6,})\b", txt)  # e.g. CPAGTEUVE3
    return m.group(1) if m else None


async def wait_for_payment_and_capture(
    session, *, request_id: str, driver_id: str, vehicle_number: str,
    job_id: str, r: redis.Redis, log: StepLogger, reporter=None,
    qr_selector: str = "img#qrcodeImg",
    receipt_selector: str | None = None,
) -> RunOutcome:
    # 1. Capture + upload the QR (this sets qrPaymentNeeded on the API side,
    # atomically with the qrCode.url, so the app never sees the status without
    # an image). Sync the worker's local FSM cursor to match.
    qr_png = await _capture_png(session, qr_selector)
    if qr_png:
        await api_client.save_qr(request_id=request_id, driver_id=driver_id,
                                 vehicle_number=vehicle_number, image_base64=qr_png)
        if reporter is not None:
            reporter.sync_local("qrPaymentNeeded")
        log.record(StepLog(log.next_index(), "payment.qr_uploaded", StepStatus.OK))
    else:
        log.record(StepLog(log.next_index(), "payment.qr_capture",
                           StepStatus.FAILED, error="QR not captured"))

    r.hset(job_key(job_id), mapping={
        "status": "waiting_for_human",
        "waitReason": f"Scan the UPI QR and pay for vehicle {vehicle_number}.",
    })

    started = time.monotonic()
    bank_ref: str | None = None

    while time.monotonic() - started < QR_WAIT_SECS:
        text = await _page_text(session)
        bank_ref = bank_ref or await _scrape_bank_ref(session)

        # Click the intermediate 'click here' redirect if present.
        try:
            await _cdp_eval(
                session,
                "var a=Array.from(document.querySelectorAll('a')).find("
                "x=>/click here/i.test(x.innerText||'')); if(a) a.click();",
            )
        except Exception:
            pass

        if _any(NEGATIVE_MARKERS, text):
            return RunOutcome(
                status="failed",
                summary=f"Payment not confirmed for {vehicle_number}.",
                error="bank reported transaction pending/failed",
                transaction_id=bank_ref,
                payment_likely=True,  # money may be in limbo -> reconcile
                run_log=log.dump(),
            )

        if _any(RECEIPT_MARKERS, text):
            if reporter is not None:
                await reporter.set_status("verifyingPayment")
            return await _capture_receipt(
                session, request_id=request_id, driver_id=driver_id,
                vehicle_number=vehicle_number, bank_ref=bank_ref,
                receipt_selector=receipt_selector, log=log,
            )

        if _any(POSITIVE_MARKERS, text):
            # Confirmed but receipt page not yet rendered: keep a short verify poll.
            if reporter is not None:
                await reporter.set_status("verifyingPayment")
            return await _verify_then_capture(
                session, request_id=request_id, driver_id=driver_id,
                vehicle_number=vehicle_number, bank_ref=bank_ref,
                receipt_selector=receipt_selector, log=log,
            )

        await asyncio.sleep(3.0)

    return RunOutcome(
        status="failed",
        summary=f"No payment within {QR_WAIT_SECS}s for {vehicle_number}.",
        error="payment window elapsed without confirmation",
        transaction_id=bank_ref,
        payment_likely=False,  # user likely didn't pay
        run_log=log.dump(),
    )


async def _verify_then_capture(session, *, request_id, driver_id, vehicle_number,
                               bank_ref, receipt_selector, log) -> RunOutcome:
    started = time.monotonic()
    while time.monotonic() - started < PAYMENT_VERIFY_SECS:
        text = await _page_text(session)
        if _any(RECEIPT_MARKERS, text):
            return await _capture_receipt(
                session, request_id=request_id, driver_id=driver_id,
                vehicle_number=vehicle_number, bank_ref=bank_ref,
                receipt_selector=receipt_selector, log=log,
            )
        await asyncio.sleep(2.0)
    # Paid but receipt never rendered -> partial (reconcile, not a clean success).
    return RunOutcome(
        status="partial",
        summary="Payment confirmed but receipt page did not render.",
        error="receipt not captured after payment success",
        transaction_id=bank_ref, payment_likely=True, run_log=log.dump(),
    )


async def _capture_receipt(session, *, request_id, driver_id, vehicle_number,
                           bank_ref, receipt_selector, log) -> RunOutcome:
    png = await _capture_png(session, receipt_selector)
    receipt_url = None
    if png:
        res = await api_client.save_receipt(
            request_id=request_id, driver_id=driver_id, image_base64=png,
            fields={"vehicleNumber": vehicle_number, "bankRef": bank_ref},
        )
        receipt_url = res.get("url")
        log.record(StepLog(log.next_index(), "payment.receipt_uploaded",
                           StepStatus.OK))
    else:
        log.record(StepLog(log.next_index(), "payment.receipt_capture",
                           StepStatus.FAILED, error="receipt not captured"))

    if not receipt_url:
        return RunOutcome(
            status="partial",
            summary="Payment done but receipt upload failed.",
            error="receipt capture/upload failed",
            transaction_id=bank_ref, payment_likely=True, run_log=log.dump(),
        )

    return RunOutcome(
        status="done",
        summary=f"Border tax paid and receipt captured for {vehicle_number}.",
        receipt_url=receipt_url, transaction_id=bank_ref,
        payment_likely=True, run_log=log.dump(),
    )
