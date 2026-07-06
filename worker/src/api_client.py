"""HTTP client for the API's /api/internal/* endpoints.

The worker never writes Firestore directly — it POSTs here and the API
persists. Awaited where correctness depends on it (captcha/QR/receipt uploads,
the terminal); errors are returned, not raised, so a flaky network can't crash
a run mid-payment.
"""

from __future__ import annotations

import httpx

from config import API_URL, INTERNAL_API_KEY


def _headers() -> dict:
    return {"X-Internal-Key": INTERNAL_API_KEY} if INTERNAL_API_KEY else {}


async def _post(path: str, payload: dict, *, timeout: float = 60.0) -> dict:
    url = f"{API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=_headers())
        try:
            data = resp.json()
        except Exception:
            data = {"ok": resp.is_success, "status_code": resp.status_code}
        if not resp.is_success:
            print(f"[api_client] {path} -> {resp.status_code}: {data}")
        return data
    except Exception as e:
        print(f"[api_client] {path} request failed: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def status_update(
    *,
    request_id: str,
    driver_id: str,
    status: str,
    source: str = "app",
    error: str | None = None,
    extra: dict | None = None,
    task: str = "border-tax",
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
            "task": task,
        },
    )


async def save_captcha(
    *,
    request_id: str,
    driver_id: str,
    image_base64: str,
    attempt: int,
    max_attempts: int,
    wait_seconds: int,
    stage: str = "disclaimer",
    task: str = "border-tax",
) -> dict:
    return await _post(
        "/api/internal/save-captcha",
        {
            "requestId": request_id,
            "driverId": driver_id,
            "imageBase64": image_base64,
            "attempt": attempt,
            "maxAttempts": max_attempts,
            "waitSeconds": wait_seconds,
            "stage": stage,
            "task": task,
        },
    )


async def captcha_result(
    *,
    request_id: str,
    driver_id: str,
    result: str,
    attempt: int,
    task: str = "border-tax",
) -> dict:
    return await _post(
        "/api/internal/captcha-result",
        {
            "requestId": request_id,
            "driverId": driver_id,
            "result": result,
            "attempt": attempt,
            "task": task,
        },
    )


async def save_qr(
    *,
    request_id: str,
    driver_id: str,
    vehicle_number: str,
    image_base64: str,
    portal_amount: float | None = None,
    state_code: str = "",
    task: str = "border-tax",
) -> dict:

    payload: dict = {
        "requestId": request_id,
        "driverId": driver_id,
        "vehicleNumber": vehicle_number,
        "imageBase64": image_base64,
        "task": task,
    }

    if len(state_code) != 0:
        payload["state_code"] = state_code

    if portal_amount is not None:
        payload["portalAmount"] = portal_amount

    return await _post("/api/internal/save-qr", payload)


async def save_receipt(
    *,
    request_id: str,
    driver_id: str,
    pdf_base64: str | None = None,
    image_base64: str | None = None,
    fields: dict | None = None,
    task: str = "border-tax",
) -> dict:
    return await _post(
        "/api/internal/save-receipt",
        {
            "requestId": request_id,
            "driverId": driver_id,
            "pdfBase64": pdf_base64,
            "imageBase64": image_base64,
            "fields": fields or {},
            "task": task,
        },
        timeout=120.0,
    )


async def fetch_vehicle_details(
    *,
    request_id: str,
    driver_id: str,
    vehicle_number: str,
) -> dict | None:
    """Resolve the RC record for a vehicle on demand.

    Called by the owner-info phase the moment the parivahan portal shows the
    "No data found for this vehicle number" popup (VAHAN has nothing). The API
    triggers a live RC lookup (refreshing Firestore vehicleDetails/{REGNO}) and
    returns the persisted record so the worker can fill the owner-info +
    vehicle-info forms manually.

    Returns the RC record dict, or None when the RC service has no usable data
    (the caller then ends the run with a clear, driver-readable message). The
    45s timeout covers the cloud function's worst-case government-RC latency
    (it self-bounds at 30s) plus the post-write read poll.
    """
    data = await _post(
        "/api/internal/vehicle-details",
        {
            "requestId": request_id,
            "driverId": driver_id,
            "vehicleNumber": vehicle_number,
        },
        timeout=45.0,
    )
    vd = data.get("vehicleDetails") if isinstance(data, dict) else None
    return vd if isinstance(vd, dict) else None


async def job_completed(
    *,
    job_id: str,
    request_id: str,
    driver_id: str,
    status: str,
    source: str = "app",
    summary: str | None = None,
    error: str | None = None,
    receipt_url: str | None = None,
    transaction_id: str | None = None,
    payment_likely: bool = False,
    cost_data: dict | None = None,
    task: str = "border-tax",
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
            "task": task,
        },
    )


# ─── challan-settlement ─────────────────────────────────────────────────


async def challan_settlement_flag(
    *,
    request_id: str,
    driver_id: str,
    vehicle_number: str,
    checking: bool,
    summary: str | None = None,
    requested_challans: list[str] | None = None,
    task: str = "challan-settlement",
) -> dict:
    """Set / clear aiCheckingSettlement on challans/{VEH} (merge)."""
    return await _post(
        "/api/internal/challan-settlement/flag",
        {
            "requestId": request_id,
            "driverId": driver_id,
            "vehicleNumber": vehicle_number,
            "checking": checking,
            "summary": summary,
            "requestedChallans": requested_challans,
            "task": task,
        },
    )


async def save_challan_quotations(
    *,
    job_id: str,
    request_id: str,
    driver_id: str,
    vehicle_number: str,
    department: str,
    records: list[dict],
    task: str = "challan-settlement",
) -> dict:
    """Persist quotations under challans/{VEH}/subChallans/{challanId}.
    records: [{"challanId": str, "amount": int, "isExtra": bool}]"""
    return await _post(
        "/api/internal/challan-settlement/quotations",
        {
            "jobId": job_id,
            "requestId": request_id,
            "driverId": driver_id,
            "vehicleNumber": vehicle_number,
            "department": department,
            "records": records,
            "task": task,
        },
        timeout=90.0,
    )
