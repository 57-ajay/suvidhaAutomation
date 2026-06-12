"""The user-solved disclaimer captcha.

One attempt = one image the user actually saw:

  capture (blank captures auto-refresh + recapture, costing NO attempt)
    -> save-captcha           API: status captchaSolving, captcha.lastResult
                              "awaiting_input", fresh signed URL per attempt
    -> wait for the answer    redis humanInput, CAPTCHA_WAIT_SECS budget,
                              external cancel honored
    -> fill + submit_action() state runner's closure: Pay Online + confirm
                              popup + "did we leave the disclaimer?" check
    -> verdict                accepted: captcha-result("accepted"), return
                              rejected: captcha-result("rejected") so the
                              client shows "wrong code" BEFORE the next image
                              lands, dismiss popup, refresh, next attempt

Exhausting MAX_USER_CAPTCHA_ATTEMPTS is a hard cancel — by design nothing
before the gateway can move money, so this stays retryable for the driver.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

import api_client
from config import CAPTCHA_WAIT_SECS, MAX_USER_CAPTCHA_ATTEMPTS
from engine.canvas import wait_and_capture_canvas_png
from engine.steps import click, dismiss_popup, fill
from engine.types import RunContext, ScriptedAbort, StepLog, StepStatus
from lifecycle.status import Status
from redis_client import job_key


async def _capture_readable(
    ctx: RunContext,
    *,
    container_selector: str | None,
    refresh_selector: str,
) -> str | None:
    """Capture the captcha the user will actually be validated against.

    Order matters for sync with the portal: FIRST wait for the visible canvas
    to paint (a freshly mounted section reads as blank for a beat — that is
    not a reason to refresh). Only if the paint-wait exhausts do we click
    refresh ONCE, settle, and wait again. After this function returns an
    image, NOTHING may touch refresh until the portal rejects the answer —
    otherwise the user solves a code the portal has already replaced.
    """
    del container_selector  # probe always scans the whole document
    await asyncio.sleep(1.0)  # let the section mount before the first probe

    for round_no in (1, 2):
        b64, detail = await wait_and_capture_canvas_png(
            ctx.session,
            timeout=6.0,
            tick=0.6,
        )
        if b64:
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="captcha.capture",
                    status=StepStatus.OK,
                    attempt=round_no,
                    value=detail,
                )
            )
            return b64
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="captcha.capture",
                status=StepStatus.RETRIED,
                attempt=round_no,
                error=detail,
            )
        )
        if round_no == 1:
            # Nothing has been shown to the user yet, so one refresh is safe.
            try:
                await click(
                    ctx.session,
                    refresh_selector,
                    log=ctx.log,
                    name="captcha.refresh_unpainted",
                    timeout=4,
                    retries=0,
                )
            except ScriptedAbort:
                pass
            await asyncio.sleep(1.5)
    return None


async def _wait_for_answer(ctx: RunContext) -> str | None:
    """Poll redis for humanInput. None on timeout; raises on external cancel."""
    key = job_key(ctx.job_id)
    deadline = time.monotonic() + CAPTCHA_WAIT_SECS
    while time.monotonic() < deadline:
        if ctx.r.hget(key, "status") == "cancelled":
            raise ScriptedAbort(
                "cancelled by user while solving the captcha", terminal="cancelled"
            )
        answer = ctx.r.hget(key, "humanInput")
        if answer:
            ctx.r.hdel(key, "humanInput")
            return str(answer).strip()
        await asyncio.sleep(1.0)
    return None


async def solve_user_captcha(
    ctx: RunContext,
    *,
    input_selector: str,
    refresh_selector: str,
    submit_action: Callable[[], Awaitable[bool]],
    is_rejected: Callable[[], Awaitable[bool]],
    canvas_container: str | None = None,
) -> None:
    p = ctx.params
    max_attempts = MAX_USER_CAPTCHA_ATTEMPTS

    for attempt in range(1, max_attempts + 1):
        b64 = await _capture_readable(
            ctx,
            container_selector=canvas_container,
            refresh_selector=refresh_selector,
        )
        if b64 is None:
            raise ScriptedAbort(
                "could not capture a readable captcha image from the portal",
                terminal="cancelled",
            )

        resp = await api_client.save_captcha(
            request_id=p.requestId,
            driver_id=p.driverId,
            image_base64=b64,
            attempt=attempt,
            max_attempts=max_attempts,
            wait_seconds=CAPTCHA_WAIT_SECS,
        )
        if not resp.get("ok"):
            raise ScriptedAbort(
                f"could not deliver the captcha image to the app "
                f"({resp.get('error', 'upload failed')})",
                terminal="cancelled",
            )
        # save-captcha flipped the status API-side; keep the local cursor honest.
        ctx.reporter.sync_local(Status.CAPTCHA_SOLVING)
        ctx.r.hset(
            job_key(ctx.job_id),
            mapping={
                "captchaUrl": resp.get("url", ""),
                "captchaAttempt": str(attempt),
            },
        )
        ctx.reporter.set_wait(
            f"Enter the captcha shown in the app (attempt {attempt} of "
            f"{max_attempts}) for vehicle {p.vehicleNumber}.",
        )

        answer = await _wait_for_answer(ctx)
        ctx.reporter.clear_wait()
        if answer is None:
            raise ScriptedAbort(
                f"no captcha answer received within "
                f"{CAPTCHA_WAIT_SECS // 60} minutes — request cancelled, "
                "no payment was made",
                terminal="cancelled",
            )

        await fill(
            ctx.session,
            input_selector,
            answer,
            log=ctx.log,
            name=f"captcha.fill_{attempt}",
        )
        advanced = await submit_action()
        rejected = await is_rejected()

        if advanced and not rejected:
            await api_client.captcha_result(
                request_id=p.requestId,
                driver_id=p.driverId,
                result="accepted",
                attempt=attempt,
            )
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="captcha.accepted",
                    status=StepStatus.OK,
                    attempt=attempt,
                )
            )
            return

        # Wrong code: tell the client explicitly, then refresh for a new image.
        await api_client.captcha_result(
            request_id=p.requestId,
            driver_id=p.driverId,
            result="rejected",
            attempt=attempt,
        )
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="captcha.rejected",
                status=StepStatus.RETRIED,
                attempt=attempt,
            )
        )
        await dismiss_popup(
            ctx.session, log=ctx.log, name=f"captcha.dismiss_reject_{attempt}"
        )
        if attempt < max_attempts:
            try:
                await click(
                    ctx.session,
                    refresh_selector,
                    log=ctx.log,
                    name=f"captcha.refresh_{attempt}",
                    timeout=5,
                    retries=0,
                )
            except ScriptedAbort:
                pass
            await asyncio.sleep(1.0)

    raise ScriptedAbort(
        f"captcha was entered incorrectly {max_attempts} times — request "
        "cancelled, no payment was made",
        terminal="cancelled",
    )
