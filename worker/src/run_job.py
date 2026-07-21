# worker/src/run_job.py
"""Per-job entrypoint: `python run_job.py <job_id> <display>`.

Owns the terminal rule:
  - ScriptedAbort carries its own terminal (cancelled / failed).
  - For anything unexpected, the lifecycle position decides: a crash while
    still in PRE_PAYMENT is `cancelled` (retryable, no money moved); past the
    money line it is `failed` so the API flips manualReview for reconcile.

Writes the Redis terminal, POSTs job-completed (the API applies the Firestore
terminal atomically), marks terminalApplied so the orchestrator's orphan
fallback knows this run closed itself, and always kills the browser.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import traceback

import api_client
from browser_config import build_browser_session
from config import JOB_MAX_RUNTIME_SECS, JOB_TTL
from engine.log import StepLogger
from engine.pipeline import run_phases
from engine.types import RunContext, RunOutcome, ScriptedAbort
from lifecycle.reporter import StatusReporter
from lifecycle.status import PRE_PAYMENT, Status
from redis_client import get_redis, job_key
from tasks.border_tax.params import BorderTaxParams
from tasks.border_tax.registry import resolve_phases

TERMINAL_AGENT_STATUS = {
    "done": Status.COMPLETED,
    "cancelled": Status.CANCELLED,
    "failed": Status.FAILED,
    "partial": Status.FAILED,
}


async def _close_browser(session) -> None:
    for meth in ("kill", "stop", "close"):
        fn = getattr(session, meth, None)
        if fn is None:
            continue
        try:
            await fn()
            return
        except Exception:
            continue


async def amain(job_id: str, display: str) -> None:
    r = get_redis()
    key = job_key(job_id)
    job = r.hgetall(key)
    if not job:
        print(f"[run_job] job {job_id} not found")
        return

    params_raw = json.loads(job.get("params") or "{}")
    source = job.get("source", "app")
    request_id = job.get("requestId") or params_raw.get("requestId", job_id)
    driver_id = job.get("driverId") or params_raw.get("driverId", "")
    task_id = job.get("taskId", "border-tax")

    reporter = StatusReporter(
        job_id=job_id,
        request_id=request_id,
        driver_id=driver_id,
        source=source,
        r=r,
        task=task_id,
    )
    log = StepLogger()
    session = None

    try:
        task = job.get("task", "borderTax")  # border-tax sub-mode (verify vs normal)
        max_restarts = 1
        ctx_task = "border-tax"

        if task_id == "puc-certificate":
            # No-payment PUC flow on the parivahan portal. Whole-flow retry is
            # bounded by PUC_MAX_RETRIES; the captcha budget + per-phase
            # deadlines bound the rest.
            from tasks.puc.params import PucParams
            from tasks.puc.flow import PHASES as puc_phases
            from config import PUC_MAX_RETRIES

            params = PucParams.from_job(params_raw)
            phases = puc_phases
            ctx_task = "puc-certificate"
            max_restarts = PUC_MAX_RETRIES
            await reporter.set_status(Status.AI_AGENT_STARTED)

        elif task_id == "challan-settlement":
            # No-payment quotation scan on vcourts.gov.in. Phase list is built
            # per-job from the client's challan numbers (one phase per
            # department); each department phase is internally fault-isolated.
            from tasks.challan_settlement.params import ChallanSettlementParams
            from tasks.challan_settlement.flow import make_phases

            params = ChallanSettlementParams.from_job(params_raw)
            phases = make_phases(params)
            ctx_task = "challan-settlement"
            await reporter.set_status(Status.AI_AGENT_STARTED)

        elif task == "verifyPendingPayment":
            from tasks.border_tax.verify_pending import VerifyPendingParams
            from tasks.border_tax.registry import resolve_verify_phases

            params = VerifyPendingParams.from_job(params_raw)
            phases = resolve_verify_phases(params.state)
            # The request is already verifyingPendingPayment (set at park).
            # Seed the local cursor so the FSM guard lets us go to
            # generatingReceipt / failed without re-asserting it.
            reporter.sync_local(Status.VERIFYING_PENDING_PAYMENT)
        else:
            # Border-tax. `path` picks the runner family:
            #   fullyAutomated (default) -> tasks.border_tax (AI captcha + UPI)
            #   scripted (web source)    -> scripted.* (form-fill -> human
            #                               handover -> receipt watch)
            # The API enforced the source/path/state matrix at enqueue; an
            # unknown state here is an internal error, not a user one.
            path = job.get("path", "fullyAutomated")
            if path == "scripted":
                from scripted.params import BorderTaxParams as ScriptedParams
                from scripted.registry import resolve_phases as scripted_phases

                params = ScriptedParams.from_job(params_raw)
                phases = scripted_phases(params.state)
            else:
                params = BorderTaxParams.from_job(params_raw)
                phases = resolve_phases(params.state)
            await reporter.set_status(Status.AI_AGENT_STARTED)

        session = build_browser_session(display)
        await asyncio.wait_for(session.start(), timeout=90)

        ctx = RunContext(
            session=session,
            params=params,
            reporter=reporter,
            log=log,
            r=r,
            job_id=job_id,
            task=ctx_task,
        )
        # Global ceiling on top of the per-phase deadlines: whatever happens,
        # this job ends. Terminal follows the money-line rule below.
        outcome = await asyncio.wait_for(
            run_phases(phases, ctx, max_restarts=max_restarts),
            timeout=JOB_MAX_RUNTIME_SECS,
        )
    # try:
    #     params = BorderTaxParams.from_job(params_raw)
    #     phases = resolve_phases(params.state)
    #     task = job.get("task", "borderTax")
    #
    #     if task == "verifyPendingPayment":
    #         from tasks.border_tax.verify_pending import VerifyPendingParams
    #         from tasks.border_tax.registry import resolve_verify_phases
    #
    #         params = VerifyPendingParams.from_job(params_raw)
    #         phases = resolve_verify_phases(params.state)
    #         # The request is already verifyingPendingPayment (set at park).
    #         # Seed the local cursor so the FSM guard lets us go to
    #         # generatingReceipt / failed without re-asserting it.
    #         reporter.sync_local(Status.VERIFYING_PENDING_PAYMENT)
    #     else:
    #         params = BorderTaxParams.from_job(params_raw)
    #         phases = resolve_phases(params.state)
    #         await reporter.set_status(Status.AI_AGENT_STARTED)
    #
    #     session = build_browser_session(display)
    #     await asyncio.wait_for(session.start(), timeout=90)
    #
    #     ctx = RunContext(
    #         session=session,
    #         params=params,
    #         reporter=reporter,
    #         log=log,
    #         r=r,
    #         job_id=job_id,
    #     )
    #     # Global ceiling on top of the per-phase deadlines: whatever happens,
    #     # this job ends. Terminal follows the money-line rule below.
    #     outcome = await asyncio.wait_for(
    #         run_phases(phases, ctx), timeout=JOB_MAX_RUNTIME_SECS
    #     )
    except asyncio.TimeoutError:
        pre = reporter.current in PRE_PAYMENT
        msg = (
            f"the run exceeded the {JOB_MAX_RUNTIME_SECS // 60}-minute "
            f"limit and was stopped"
            + ("" if pre else " during the payment flow — reconcile")
        )
        outcome = RunOutcome(
            status="cancelled" if pre else "failed",
            summary=msg,
            error=msg,
            payment_likely=reporter.current
            in (Status.VERIFYING_PAYMENT, Status.GENERATING_RECEIPT),
            run_log=log.dump(),
        )
    except ScriptedAbort as e:
        status = "cancelled" if e.terminal == "cancelled" else "failed"
        outcome = RunOutcome(
            status=status,
            summary=e.message,
            error=e.message,
            abort_reason=e.reason,
            payment_likely=e.terminal == "failed"
            and reporter.current
            in (Status.VERIFYING_PAYMENT, Status.GENERATING_RECEIPT),
            run_log=log.dump(),
        )
    except Exception as e:
        traceback.print_exc()
        pre = reporter.current in PRE_PAYMENT
        msg = f"unexpected error: {type(e).__name__}: {e}"
        outcome = RunOutcome(
            status="cancelled" if pre else "failed",
            summary=msg
            + ("" if pre else " — stopped during the payment window, reconcile"),
            error=msg,
            payment_likely=reporter.current
            in (Status.VERIFYING_PAYMENT, Status.GENERATING_RECEIPT),
            run_log=log.dump(),
        )
    finally:
        if session is not None:
            await _close_browser(session)

    if not outcome.total_cost_usd:
        outcome.total_cost_usd = log.total_ai_cost()

    try:
        run_log_json = json.dumps(
            [e.model_dump(exclude_none=True) for e in outcome.run_log],
        )[:50_000]
    except Exception:
        run_log_json = "[]"

    if outcome.status == "parked":
        r.hset(
            key,
            mapping={
                "status": "parked",
                "agentStatus": Status.VERIFYING_PENDING_PAYMENT,
                "result": (outcome.summary or "")[:2000],
                "error": (outcome.error or "")[:2000],
                "totalCostUsd": f"{outcome.total_cost_usd:.6f}",
                "runLog": run_log_json,
            },
        )
        r.hdel(key, "waitReason", "humanInput")
        r.expire(key, JOB_TTL)
        # Firestore status was already set to verifyingPendingPayment by the
        # reporter. job-completed(parked) saves cost only — no applyTerminal.
        resp = await api_client.job_completed(
            job_id=job_id,
            request_id=request_id,
            driver_id=driver_id,
            status="parked",
            source=source,
            summary=outcome.summary,
            error=outcome.error,
            payment_likely=outcome.payment_likely,
            cost_data={"totalCost": round(outcome.total_cost_usd, 6)},
        )
        if resp.get("ok"):
            r.hset(
                key, "terminalApplied", "1"
            )  # this worker closed itself; reaper skips
        print(f"[run_job] job={job_id} -> parked (verifyingPendingPayment)")
        return

    r.hset(
        key,
        mapping={
            "status": outcome.status,
            "agentStatus": TERMINAL_AGENT_STATUS[outcome.status],
            "result": (outcome.summary or "")[:2000],
            "error": (outcome.error or "")[:2000],
            "totalCostUsd": f"{outcome.total_cost_usd:.6f}",
            "runLog": run_log_json,
        },
    )
    r.hdel(key, "waitReason", "humanInput")
    r.expire(key, JOB_TTL)

    resp = await api_client.job_completed(
        job_id=job_id,
        request_id=request_id,
        driver_id=driver_id,
        status=outcome.status,
        source=source,
        summary=outcome.summary,
        error=outcome.error,
        receipt_url=outcome.receipt_url,
        transaction_id=outcome.transaction_id,
        payment_likely=outcome.payment_likely,
        cost_data={"totalCost": round(outcome.total_cost_usd, 6)},
        task=task_id,
    )
    if resp.get("ok"):
        r.hset(key, "terminalApplied", "1")
    else:
        print(f"[run_job] job-completed not acknowledged: {resp}")

    print(f"[run_job] job={job_id} -> {outcome.status}: {outcome.summary}")


async def _runner(job_id: str, display: str) -> None:
    """amain() wrapped so SIGTERM (the orchestrator's grace-expiry or
    hard-deadline terminate) CANCELS the task instead of killing the process
    outright. Python's default SIGTERM handler exits immediately — finally
    blocks never run, the browser is never closed, and the Chromium orphans
    until main.py's killpg backstop. With this handler the CancelledError
    unwinds through amain's finally, _close_browser kills the session, and
    the orchestrator's orphan fallback writes the terminal."""
    task = asyncio.ensure_future(amain(job_id, display))
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: run_job.py <job_id> <display>")
        sys.exit(2)
    job_id, display = sys.argv[1], sys.argv[2]
    os.environ["DISPLAY"] = display
    asyncio.run(_runner(job_id, display))


if __name__ == "__main__":
    main()
