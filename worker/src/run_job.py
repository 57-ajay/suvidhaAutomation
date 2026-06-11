# worker/src/run_job.py
"""Run exactly one job, in its own process, on a given X display.

Invoked by the orchestrator as:  python run_job.py <job_id> <display>

Steps:
  1. Load the job hash from Redis, parse params.
  2. Build the reporter; set aiAgentStarted.
  3. Make a browser on the display, run the scripted UP flow.
  4. Map the RunOutcome -> POST /api/internal/job-completed (the API applies the
     terminal + agentCost atomically). Also stamp the Redis hash terminal.

The process exit code is 0 on a clean terminal (any of done/cancelled/failed),
non-zero only on an unexpected crash so the orchestrator can tell them apart.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback

from redis_client import get_redis, job_key
from lifecycle.reporter import StatusReporter
from lifecycle.status import Status
from scripted.params import BorderTaxParams
from scripted.runner import run_border_tax
from scripted.types import RunOutcome, ScriptedAbort
from browser_config import make_browser
import api_client


def _build_cost(outcome: RunOutcome, source: str) -> dict:
    data = outcome.cost_data or {}
    total = float(data.get("totalCost", 0.0) or 0.0)
    return {
        "totalCost": total,
        "totalTokens": int(data.get("totalTokens", 0) or 0),
        "totalPromptTokens": int(data.get("totalPromptTokens", 0) or 0),
        "totalCompletionTokens": int(data.get("totalCompletionTokens", 0) or 0),
    }


async def _run(job_id: str, display: str) -> int:
    r = get_redis()
    raw = r.hgetall(job_key(job_id))
    if not raw:
        print(f"[run_job] job {job_id} not found")
        return 2
    job = {
        (k.decode() if isinstance(k, bytes) else k):
        (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }
    source = job.get("source", "app")
    params = BorderTaxParams.from_dict(json.loads(job.get("params", "{}")))

    reporter = StatusReporter(
        job_id=job_id, request_id=params.requestId, driver_id=params.driverId,
        source=source, r=r,
    )

    # queued -> aiAgentStarted
    reporter.sync_local(Status.QUEUED)
    await reporter.set_status(Status.AI_AGENT_STARTED)

    session = make_browser(display=display)
    outcome: RunOutcome
    try:
        await session.start()
        outcome = await run_border_tax(session, params, reporter=reporter, r=r,
                                       job_id=job_id)
    except ScriptedAbort as e:
        outcome = RunOutcome(status=e.terminal, summary=e.message, error=e.message,
                             payment_likely=e.payment_likely)
    except Exception as e:  # unexpected crash
        traceback.print_exc()
        # Pre-payment crash is safe to cancel; we can't prove money moved.
        outcome = RunOutcome(status="cancelled",
                             summary="worker crashed before completion",
                             error=f"{type(e).__name__}: {e}")
    finally:
        try:
            await session.kill()
        except Exception:
            pass

    # Map outcome -> terminal via API (owns Firestore + agentCost).
    await api_client.job_completed(
        job_id=job_id, request_id=params.requestId, driver_id=params.driverId,
        status=outcome.status, source=source, summary=outcome.summary,
        error=outcome.error, receipt_url=outcome.receipt_url,
        transaction_id=outcome.transaction_id, payment_likely=outcome.payment_likely,
        cost_data=_build_cost(outcome, source),
    )

    # Stamp Redis terminal for the dashboard.
    terminal = {
        "done": "completed", "cancelled": "cancelled",
        "failed": "failed", "partial": "failed",
    }.get(outcome.status, "failed")
    r.hset(job_key(job_id), mapping={
        "status": terminal,
        "agentStatus": terminal,
        "summary": outcome.summary or "",
        "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    print(f"[run_job] job {job_id} finished: {outcome.status} -> {terminal}")
    return 0


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: python run_job.py <job_id> <display>")
        sys.exit(2)
    job_id, display = sys.argv[1], sys.argv[2]
    code = asyncio.run(_run(job_id, display))
    sys.exit(code)


if __name__ == "__main__":
    main()
