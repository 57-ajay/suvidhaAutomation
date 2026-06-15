"""Captcha solving for the CheckPost portal — one policy for every surface.

Two captcha pages exist (the pending-transaction "Check Transaction Status"
page and the Step-4 disclaimer). Both route through solve_captcha(), which
honors the global CAPTCHA_MODE:

  ai     -> Vertex OCR, SILENTLY: no captcha image is uploaded and no captcha
            status (captchaSolving / pendingTransactionCaptcha) is written, so
            the client never shows a captcha the AI is handling. Only if OCR
            exhausts MAX_AI_CAPTCHA_ATTEMPTS do we fall back to the human seam.
  human  -> capture -> save-captcha (flips the captcha status API-side, fresh
            signed URL per attempt) -> wait for the user's text -> submit ->
            verdict. The old user-solved flow.

Capture discipline (shared by both paths): wait for the visible canvas to
PAINT, refresh at most once on a blank, and never refresh an image already
shown to a user until the portal rejects it. canvas_scope narrows the probe to
the container holding the real canvas (the pending page nests it in
div#captcha); without it the whole-document probe can latch a blank sibling
canvas and spin on refresh.

Through both paths nothing irreversible happens, so exhaustion is a hard cancel
(no money has moved) and the request stays retryable.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import api_client
from config import (
    CAPTCHA_MODE,
    CAPTCHA_WAIT_SECS,
    MAX_AI_CAPTCHA_ATTEMPTS,
    MAX_USER_CAPTCHA_ATTEMPTS,
)
from engine.canvas import wait_and_capture_canvas_png
from engine.steps import cdp_eval, dismiss_popup, fill
from engine.types import RunContext, ScriptedAbort, StepLog, StepStatus
from lifecycle.status import Status
from llm.vertex import ocr_image
from redis_client import job_key


async def _refresh(ctx: RunContext, refresh_selector: str) -> None:
    """Click the captcha refresh control. cdp_eval querySelector click handles
    any selector (incl. the pending page's sibling-button group) robustly."""
    try:
        await cdp_eval(
            ctx.session,
            "(function(s){var e=document.querySelector(s);"
            "if(e){e.click();return true;}return false;})("
            + json.dumps(refresh_selector)
            + ")",
        )
    except Exception:
        pass
    await asyncio.sleep(1.0)


async def _capture_readable(
    ctx: RunContext,
    *,
    refresh_selector: str,
    canvas_scope: str | None = None,
) -> str | None:
    """Capture the captcha the user/OCR will actually be validated against.

    FIRST wait for the visible canvas to paint (a freshly mounted section reads
    blank for a beat — not a reason to refresh). Only if the paint-wait exhausts
    do we refresh ONCE, settle, and wait again. After this returns an image,
    NOTHING may touch refresh until the portal rejects the answer."""
    await asyncio.sleep(1.0)  # let the section mount before the first probe
    scope = canvas_scope or "canvas"

    for round_no in (1, 2):
        b64, detail = await wait_and_capture_canvas_png(
            ctx.session,
            timeout=6.0,
            tick=0.6,
            scope_selector=scope,
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
            # Nothing has been shown yet, so one refresh is safe.
            await _refresh(ctx, refresh_selector)
    return None


async def _wait_for_answer(ctx: RunContext) -> str | None:
    """Poll redis for humanInput. None on timeout; raises on external cancel."""
    key = job_key(ctx.job_id)
    deadline = time.monotonic() + CAPTCHA_WAIT_SECS
    while time.monotonic() < deadline:
        if ctx.r.hget(key, "status") == "cancelled":
            raise ScriptedAbort(
                "cancelled by user while solving the captcha",
                terminal="cancelled",
            )
        answer = ctx.r.hget(key, "humanInput")
        if answer:
            ctx.r.hdel(key, "humanInput")
            return str(answer).strip()
        await asyncio.sleep(1.0)
    return None


async def _try_ai_captcha(
    ctx: RunContext,
    *,
    input_selector: str,
    refresh_selector: str,
    submit_action: Callable[[], Awaitable[bool]],
    is_rejected: Callable[[], Awaitable[bool]],
    canvas_scope: str | None,
) -> bool:
    """Silent Vertex-OCR attempts. No save-captcha, no captcha status, no
    captcha-result — the client must never see a captcha the AI is solving.
    Returns True once the submit is accepted; False when OCR is exhausted (or
    capture itself fails), so the caller can hand off to a human."""
    for attempt in range(1, MAX_AI_CAPTCHA_ATTEMPTS + 1):
        b64 = await _capture_readable(
            ctx,
            refresh_selector=refresh_selector,
            canvas_scope=canvas_scope,
        )
        if b64 is None:
            return False  # capture failed; let a human try

        text, cost = await ocr_image(b64)
        ctx.log.record(
            StepLog(
                index=ctx.log.next_index(),
                name="captcha.ai_ocr",
                status=StepStatus.OK if text != "UNREADABLE" else StepStatus.RETRIED,
                attempt=attempt,
                value=text,
                ai_cost_usd=cost,
            )
        )
        if text == "UNREADABLE":
            await _refresh(ctx, refresh_selector)
            continue

        await fill(
            ctx.session,
            input_selector,
            text,
            log=ctx.log,
            name=f"captcha.ai_fill_{attempt}",
        )
        if await submit_action() and not await is_rejected():
            ctx.log.record(
                StepLog(
                    index=ctx.log.next_index(),
                    name="captcha.ai_accepted",
                    status=StepStatus.OK,
                    attempt=attempt,
                )
            )
            return True

        await dismiss_popup(
            ctx.session, log=ctx.log, name=f"captcha.ai_dismiss_{attempt}"
        )
        await _refresh(ctx, refresh_selector)

    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name="captcha.ai_exhausted",
            status=StepStatus.HANDED_OFF,
            value=f"{MAX_AI_CAPTCHA_ATTEMPTS} OCR attempts failed; falling back",
        )
    )
    return False


async def _solve_human_captcha(
    ctx: RunContext,
    *,
    status: str,
    stage: str,
    input_selector: str,
    refresh_selector: str,
    submit_action: Callable[[], Awaitable[bool]],
    is_rejected: Callable[[], Awaitable[bool]],
    canvas_scope: str | None,
) -> None:
    """Capture -> save-captcha (flips `status` API-side via `stage`) -> wait for
    the user -> submit -> verdict, one fresh image per attempt."""
    p = ctx.params
    max_attempts = MAX_USER_CAPTCHA_ATTEMPTS

    for attempt in range(1, max_attempts + 1):
        b64 = await _capture_readable(
            ctx,
            refresh_selector=refresh_selector,
            canvas_scope=canvas_scope,
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
            stage=stage,
        )
        if not resp.get("ok"):
            raise ScriptedAbort(
                f"could not deliver the captcha image to the app "
                f"({resp.get('error', 'upload failed')})",
                terminal="cancelled",
            )
        # save-captcha flipped the status API-side; keep the cursor honest.
        ctx.reporter.sync_local(status)
        ctx.r.hset(
            job_key(ctx.job_id),
            mapping={
                "captchaUrl": resp.get("url", ""),
                "captchaAttempt": str(attempt),
            },
        )
        ctx.reporter.set_wait(
            f"Enter the captcha shown in the app (attempt {attempt} of "
            f"{max_attempts}) for vehicle {p.vehicleNumber}."
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
            await _refresh(ctx, refresh_selector)

    raise ScriptedAbort(
        f"captcha was entered incorrectly {max_attempts} times — request "
        "cancelled, no payment was made",
        terminal="cancelled",
    )


async def solve_captcha(
    ctx: RunContext,
    *,
    status: str,
    stage: str,
    input_selector: str,
    refresh_selector: str,
    submit_action: Callable[[], Awaitable[bool]],
    is_rejected: Callable[[], Awaitable[bool]],
    canvas_scope: str | None = None,
    mode: str | None = None,
) -> None:
    """Solve one captcha page under the global CAPTCHA_MODE.

    status  the captcha lifecycle status this page reports when a HUMAN solves
            it (Status.CAPTCHA_SOLVING or Status.PENDING_TRANSACTION_CAPTCHA).
    stage   "disclaimer" | "pending" — tells save-captcha which status to flip.
    submit_action  everything to do AFTER the captcha text is typed; returns
            True if the page advanced past the captcha.
    is_rejected    True if the portal showed a captcha-mismatch.
    Raises ScriptedAbort(cancelled) only after the human seam is exhausted."""
    mode = (mode or CAPTCHA_MODE).lower()

    if mode == "ai":
        if await _try_ai_captcha(
            ctx,
            input_selector=input_selector,
            refresh_selector=refresh_selector,
            submit_action=submit_action,
            is_rejected=is_rejected,
            canvas_scope=canvas_scope,
        ):
            return
        # OCR exhausted — clear any leftover popup and hand to the user.
        await dismiss_popup(ctx.session, log=ctx.log, name="captcha.ai_handoff")

    await _solve_human_captcha(
        ctx,
        status=status,
        stage=stage,
        input_selector=input_selector,
        refresh_selector=refresh_selector,
        submit_action=submit_action,
        is_rejected=is_rejected,
        canvas_scope=canvas_scope,
    )


# Back-compat shim for any state still calling the old name. New code should
# call solve_captcha directly.
async def solve_user_captcha(
    ctx: RunContext,
    *,
    input_selector: str,
    refresh_selector: str,
    submit_action: Callable[[], Awaitable[bool]],
    is_rejected: Callable[[], Awaitable[bool]],
    canvas_container: str | None = None,
) -> None:
    await solve_captcha(
        ctx,
        status=Status.CAPTCHA_SOLVING,
        stage="disclaimer",
        input_selector=input_selector,
        refresh_selector=refresh_selector,
        submit_action=submit_action,
        is_rejected=is_rejected,
        canvas_scope=canvas_container,
    )
