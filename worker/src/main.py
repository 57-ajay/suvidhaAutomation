# worker/src/main.py
"""Orchestrator. Persistent loop:

  - BRPOP job:queue (blocking) for the next job id.
  - Acquire a display slot. If none free, requeue and back off briefly.
  - Spawn `python run_job.py <job_id> <display>` as a subprocess (process-per-job
    isolates browser crashes and leaks).
  - Track the subprocess; a monitor thread watches the Redis hash for a
    cancellation request and terminates the subprocess if seen.
  - On exit, release the slot.

IMPORTANT (pivot isolation): this worker MUST point at its own Redis. Sharing a
Redis keyspace with the prod worker means both would BRPOP the same job:queue and
steal each other's jobs. The compose file wires a dedicated redis-pivot for this
reason — do not point pivot at prod Redis.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from redis_client import get_redis, job_key
from slots import SlotPool
from config import MAX_SLOTS

QUEUE_KEY = "job:queue"
RUN_JOB = os.path.join(os.path.dirname(__file__), "run_job.py")


def _monitor_cancellation(proc: subprocess.Popen, job_id: str, r) -> None:
    """Kill the subprocess if the job is marked cancelled while running."""
    while proc.poll() is None:
        try:
            st = r.hget(job_key(job_id), "status")
            st = st.decode() if isinstance(st, bytes) else st
            # Only a hard external cancel before terminal matters here; run_job
            # writes the real terminal itself.
            if st == "cancelled":
                # Let run_job's own waiting loops notice first; force-kill if it
                # doesn't exit within a grace window.
                deadline = time.monotonic() + 20
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(1)
                if proc.poll() is None:
                    print(f"[main] force-terminating cancelled job {job_id}")
                    proc.terminate()
                return
        except Exception:
            pass
        time.sleep(2)


def _run_one(pool: SlotPool, r, job_id: str) -> None:
    slot = pool.acquire(job_id)
    if slot is None:
        # No capacity: requeue at the back and let the loop back off.
        r.rpush(QUEUE_KEY, job_id)
        time.sleep(2)
        return

    try:
        proc = subprocess.Popen(
            [sys.executable, RUN_JOB, job_id, slot.display],
            cwd=os.path.dirname(__file__),
        )
        t = threading.Thread(target=_monitor_cancellation,
                             args=(proc, job_id, r), daemon=True)
        t.start()
        proc.wait()
    finally:
        pool.release(slot, job_id)


def main() -> None:
    r = get_redis()
    pool = SlotPool(size=MAX_SLOTS)
    print(f"[main] pivot-a worker up. slots={MAX_SLOTS}. waiting on {QUEUE_KEY}…")

    while True:
        try:
            item = r.brpop(QUEUE_KEY, timeout=5)
            if item is None:
                continue
            _, raw = item
            job_id = raw.decode() if isinstance(raw, bytes) else raw
            # Run each job on its own thread so the orchestrator keeps draining
            # the queue up to MAX_SLOTS concurrently.
            threading.Thread(target=_run_one, args=(pool, r, job_id),
                             daemon=True).start()
        except KeyboardInterrupt:
            print("[main] shutting down")
            break
        except Exception as e:
            print(f"[main] loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
