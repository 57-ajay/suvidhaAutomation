# worker/src/scripted/captcha_user.py
"""User-solved captcha loop (pivot-a's main captcha).

Flow per the design:
  1. Capture the captcha canvas as a PNG (toDataURL, with a screenshot fallback).
  2. POST it to the API -> uploads to GCS, writes aiAgentData.captcha, sets status
     captchaSolving. The app shows the image.
  3. Set Redis waiting_for_human and poll for humanInput (the user's text), which
     arrives via POST /api/jobs/:id/intervene.
  4. Fill + submit. The PORTAL is the only verifier — watch for the "Invalid
     Captcha" SweetAlert2 popup. Accepted -> return. Rejected -> dismiss popup,
     click refresh, re-capture, re-upload (attempt++), loop.
  5. After MAX_USER_CAPTCHA_ATTEMPTS wrong tries -> raise ScriptedAbort
     (terminal=cancelled). No money has moved at this stage.

`submit_action` is supplied by the caller: it fills any other fields, clicks the
submit/Pay Online button, and returns True if the page advanced past the captcha.
`is_rejected` returns True if the captcha-mismatch popup is showing.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import redis

from .log import StepLogger
from .steps import _cdp_eval, click, dismiss_popup, fill
from .types import StepLog, StepStatus, ScriptedAbort
from redis_client import job_key
from config import MAX_USER_CAPTCHA_ATTEMPTS, CAPTCHA_WAIT_SECS
import api_client


SubmitAction = Callable[[], Awaitable[bool]]
RejectedCheck = Callable[[], Awaitable[bool]]


async def _capture_canvas_png(session, canvas_selector: str) -> str | None:
    """Return base64 PNG (no data: prefix) of the captcha canvas, or None."""
    # Preferred: canvas.toDataURL — exact pixels, no compositing surprises.
    expr = (
        f"var c = document.querySelector({json.dumps(canvas_selector)});"
        f"if(!c) return '';"
        f"try {{ return c.toDataURL('image/png'); }} catch(e) {{ return ''; }}"
    )
    try:
        data_url = await _cdp_eval(session, expr)
        if data_url and data_url.startswith("data:image"):
            return data_url.split(",", 1)[1]
    except Exception:
        pass

    # Fallback: clip-screenshot the element bounds via CDP Page.captureScreenshot.
    try:
        rect = await _cdp_eval(
            session,
            f"var c=document.querySelector({json.dumps(canvas_selector)});"
            f"if(!c) return null; var r=c.getBoundingClientRect();"
            f"return {{x:r.x,y:r.y,w:r.width,h:r.height}};",
        )
        if not rect or not rect.get("w"):
            return None
        cdp = getattr(session, "cdp_client", None)
        if cdp is None:
            return None
        shot = await cdp.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "clip": {
                    "x": rect["x"], "y": rect["y"],
                    "width": rect["w"], "height": rect["h"],
                    "scale": 1,
                },
            },
        )
        return shot.get("data")
    except Exception:
        return None


async def solve_user_captcha(
    session,
    *,
    canvas_selector: str,
    input_selector: str,
    refresh_selector: str | None,
    submit_action: SubmitAction,
    is_rejected: RejectedCheck,
    request_id: str,
    driver_id: str,
    job_id: str,
    r: redis.Redis,
    log: StepLogger,
    name: str = "user_captcha",
) -> None:
    """Drive the user-solved captcha loop. Raises ScriptedAbort on exhaustion."""

    for attempt in range(1, MAX_USER_CAPTCHA_ATTEMPTS + 1):
        png = await _capture_canvas_png(session, canvas_selector)
        if not png:
            log.record(StepLog(log.next_index(), f"{name}.capture", StepStatus.FAILED,
                               attempt=attempt, error="could not capture captcha image"))
            raise ScriptedAbort("could not capture captcha image", terminal="cancelled")

        # Upload + set captchaSolving (idempotent on repeat attempts).
        await api_client.save_captcha(
            request_id=request_id, driver_id=driver_id,
            image_base64=png, attempt=attempt,
        )
        log.record(StepLog(log.next_index(), f"{name}.uploaded", StepStatus.OK,
                           attempt=attempt))

        # Wait for the user's text.
        reason = (
            f"Please type the captcha shown (attempt {attempt} of "
            f"{MAX_USER_CAPTCHA_ATTEMPTS})."
        )
        text = await _wait_for_human(job_id, r, reason, timeout=CAPTCHA_WAIT_SECS)
        if not text:
            log.record(StepLog(log.next_index(), f"{name}.timeout", StepStatus.FAILED,
                               attempt=attempt, error="no captcha input from user"))
            raise ScriptedAbort(
                "user did not provide the captcha in time", terminal="cancelled"
            )

        # Fill + submit.
        await fill(session, input_selector, text.strip(), log=log,
                   name=f"{name}.fill")
        advanced = await submit_action()

        if advanced and not await is_rejected(session):
            log.record(StepLog(log.next_index(), f"{name}.accepted", StepStatus.OK,
                               attempt=attempt, value=text.strip()))
            return

        # Rejected — dismiss popup, refresh, loop.
        log.record(StepLog(log.next_index(), f"{name}.rejected", StepStatus.RETRIED,
                           attempt=attempt, value=text.strip()))
        await dismiss_popup(session, log=log, name=f"{name}.dismiss")
        if refresh_selector:
            try:
                await click(session, refresh_selector, log=log,
                            name=f"{name}.refresh", timeout=5, retries=0)
            except Exception:
                pass
        await asyncio.sleep(1.0)

    raise ScriptedAbort(
        f"captcha could not be solved after {MAX_USER_CAPTCHA_ATTEMPTS} attempts",
        terminal="cancelled",
    )


async def _wait_for_human(job_id: str, r: redis.Redis, reason: str, *,
                          timeout: int) -> str:
    """Set waiting_for_human and poll Redis for humanInput. Same mechanism the
    intervene endpoint writes to. Returns '' on timeout."""
    r.hset(job_key(job_id), mapping={"status": "waiting_for_human",
                                     "waitReason": reason})
    waited = 0
    while waited < timeout:
        val = r.hget(job_key(job_id), "humanInput")
        if val:
            text = val.decode() if isinstance(val, bytes) else val
            r.hdel(job_key(job_id), "humanInput", "waitReason")
            r.hset(job_key(job_id), "status", "running")
            return text
        # Honor cancellation while waiting.
        st = r.hget(job_key(job_id), "status")
        st = st.decode() if isinstance(st, bytes) else st
        if st == "cancelled":
            return ""
        await asyncio.sleep(1)
        waited += 1
    r.hdel(job_key(job_id), "waitReason")
    return ""
