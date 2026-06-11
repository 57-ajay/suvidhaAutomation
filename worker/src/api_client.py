# worker/src/api_client.py
"""HTTP client for the API's internal endpoints. The worker never writes
Firestore directly — it POSTs here and the API persists. Fire-and-forget where
the run shouldn't block on persistence; awaited where correctness depends on it
(captcha/QR uploads, terminal completion)."""

from __future__ import annotations

import httpx

from config import API_URL


async def _post(path: str, payload: dict, *, timeout: float = 30.0) -> dict:
    url = f"{API_URL}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        try:
            data = resp.json()
        except Exception:
            data = {"ok": resp.is_success, "status_code": resp.status_code}
        if not resp.is_success:
            print(f"[api_client] {path} -> {resp.status_code}: {data}")
        return data


async def status_update(
    *, request_id: str, driver_id: str, status: str, source: str = "app",
    error: str | None = None, extra: dict | None = None,
) -> dict:
    return await _post(
        "/api/internal/status-update",
        {
            "requestId": request_id,
            "driverId": driver_id,
            "status": status,
            "source": source,
            "error": error,
            "extra": extra,
        },
    )


async def save_captcha(
    *, request_id: str, driver_id: str, image_base64: str, attempt: int,
) -> dict:
    return await _post(
        "/api/internal/save-captcha",
        {
            "requestId": request_id,
            "driverId": driver_id,
            "imageBase64": image_base64,
            "attempt": attempt,
        },
    )


async def save_qr(
    *, request_id: str, driver_id: str, vehicle_number: str, image_base64: str,
) -> dict:
    return await _post(
        "/api/internal/save-qr",
        {
            "requestId": request_id,
            "driverId": driver_id,
            "vehicleNumber": vehicle_number,
            "imageBase64": image_base64,
        },
    )


async def save_receipt(
    *, request_id: str, driver_id: str, image_base64: str | None = None,
    pdf_base64: str | None = None, fields: dict | None = None,
) -> dict:
    return await _post(
        "/api/internal/save-receipt",
        {
            "requestId": request_id,
            "driverId": driver_id,
            "imageBase64": image_base64,
            "pdfBase64": pdf_base64,
            "fields": fields or {},
        },
    )


async def job_completed(
    *, job_id: str, request_id: str, driver_id: str, status: str,
    source: str = "app", summary: str | None = None, error: str | None = None,
    receipt_url: str | None = None, transaction_id: str | None = None,
    payment_likely: bool = False, cost_data: dict | None = None,
) -> dict:
    return await _post(
        "/api/internal/job-completed",
        {
            "jobId": job_id,
            "requestId": request_id,
            "driverId": driver_id,
            "status": status,
            "source": source,
            "summary": summary,
            "error": error,
            "receiptUrl": receipt_url,
            "transactionId": transaction_id,
            "paymentLikely": payment_likely,
            "costData": cost_data,
        },
    )
