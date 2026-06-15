"""Orchestrator. BRPOPs the queue, runs each job as a subprocess on its own
display slot, and guarantees every job reaches a terminal:

  - Cancel monitor: when the API marks a job cancelled, the subprocess gets a
    grace window to exit through its own status checks (the captcha and
    payment loops poll for exactly this); only then is it terminated.
  - Orphan fallback: if a subprocess dies without writing its terminal
    (SIGTERM, OOM, crash), finalize_orphan() writes it here — cancelled if the
    run was still pre-payment, failed (manualReview) if money may have moved.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time

import api_client
from config import JOB_MAX_RUNTIME_SECS, JOB_TTL, MAX_SLOTS
from lifecycle.status import PRE_PAYMENT, Status
from redis_client import QUEUE_KEY, get_redis, job_key
from slots import SlotPool, live_url

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
TERMINALS = {"done", "cancelled", "failed", "partial"}
CANCEL_GRACE_SECS = 20


def finalize_orphan(job_id: str, r) -> None:
    key = job_key(job_id)
    job = r.hgetall(key)
    if not job or job.get("terminalApplied") == "1":
        return

    agent = job.get("agentStatus", "")
    pre = agent in PRE_PAYMENT or agent == ""
    user_cancel = job.get("status") == "cancelled"
    status = "cancelled" if pre else "failed"
    payment_likely = agent in (Status.VERIFYING_PAYMENT, Status.GENERATING_RECEIPT)

    if user_cancel and pre:
        summary = "cancelled by user before payment"
    elif user_cancel:
        summary = (
            "cancelled by user during the payment window — verify "
            "with the bank before retrying"
        )
    elif pre:
        summary = "the agent stopped unexpectedly before payment"
    else:
        summary = (
            "the agent stopped unexpectedly during the payment "
            "window — reconcile manually"
        )

    r.hset(
        key,
        mapping={
            "status": status,
            "agentStatus": Status.CANCELLED if status == "cancelled" else Status.FAILED,
            "result": summary,
            "error": summary,
        },
    )
    r.hdel(key, "waitReason", "humanInput")
    r.expire(key, JOB_TTL)

    request_id = job.get("requestId", job_id)
    driver_id = job.get("driverId", "")
    try:
        resp = asyncio.run(
            api_client.job_completed(
                job_id=job_id,
                request_id=request_id,
                driver_id=driver_id,
                status=status,
                source=job.get("source", "app"),
                summary=summary,
                error=summary,
                payment_likely=payment_likely,
            )
        )
        if resp.get("ok"):
            r.hset(key, "terminalApplied", "1")
    except Exception as e:
        print(f"[main] orphan job-completed failed for {job_id}: {e}")
    print(f"[main] orphan finalized job={job_id} -> {status}")


def handle_job(job_id: str, pool: SlotPool, r) -> None:
    key = job_key(job_id)

    slot = None
    while slot is None:
        if r.hget(key, "status") == "cancelled":
            finalize_orphan(job_id, r)
            return
        slot = pool.try_acquire(job_id)
        if slot is None:
            time.sleep(1.0)

    try:
        r.hset(
            key,
            mapping={
                "liveUrl": live_url(job_id),
                "slotIndex": str(slot.index),
            },
        )
        proc = subprocess.Popen(
            [sys.executable, os.path.join(SRC_DIR, "run_job.py"), job_id, slot.display],
            cwd=SRC_DIR,
            env={**os.environ, "DISPLAY": slot.display},
        )

        graced_at: float | None = None
        started_at = time.monotonic()
        hard_deadline = JOB_MAX_RUNTIME_SECS + 120  # run_job's own ceiling + grace
        while proc.poll() is None:
            if time.monotonic() - started_at > hard_deadline:
                print(
                    f"[main] job {job_id} exceeded the hard deadline "
                    f"({hard_deadline}s); terminating wedged subprocess"
                )
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if r.hget(key, "status") == "cancelled":
                if graced_at is None:
                    graced_at = time.monotonic()
                    print(
                        f"[main] cancel requested for {job_id}; "
                        f"{CANCEL_GRACE_SECS}s grace for a clean exit"
                    )
                elif time.monotonic() - graced_at > CANCEL_GRACE_SECS:
                    print(f"[main] grace expired; terminating {job_id}")
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
            time.sleep(1.0)
        proc.wait()
    finally:
        pool.release(slot, job_id)
        finalize_orphan(job_id, r)


def main() -> None:
    r = get_redis()
    pool = SlotPool(MAX_SLOTS)
    print(f"[main] border-tax worker up — {MAX_SLOTS} slots, queue '{QUEUE_KEY}'")

    while True:
        try:
            item = r.brpop(QUEUE_KEY, timeout=5)
        except Exception as e:
            print(f"[main] redis brpop error: {e}; retrying")
            time.sleep(2.0)
            continue
        if not item:
            continue

        job_id = item[1]
        if r.hget(job_key(job_id), "status") == "cancelled":
            finalize_orphan(job_id, r)
            continue

        print(f"[main] picked up job {job_id}")
        threading.Thread(
            target=handle_job,
            args=(job_id, pool, r),
            daemon=True,
        ).start()


if __name__ == "__main__":
    main()
