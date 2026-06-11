# worker/src/lifecycle/reporter.py
"""Single place the worker reports lifecycle status from.

set_status() does three things:
  1. Local FSM guard (mirrors the API) so we never even try an illegal jump.
  2. Update the Redis job hash (status + optional waitReason) so the dashboard
     and the /api/jobs/:id/status endpoint reflect reality immediately.
  3. POST /api/internal/status-update so the API writes aiAgentData.status to
     Firestore (the app subscribes to that doc).

The reporter tracks the last status it wrote per job so the guard has a `from`.
"""

from __future__ import annotations

import redis

from .status import Status, can_transition
from redis_client import job_key
import api_client


class StatusReporter:
    def __init__(self, *, job_id: str, request_id: str, driver_id: str,
                 source: str, r: redis.Redis):
        self.job_id = job_id
        self.request_id = request_id
        self.driver_id = driver_id
        self.source = source
        self.r = r
        self._current = Status.QUEUED

    @property
    def current(self) -> str:
        return self._current

    async def set_status(
        self, to: str, *, error: str | None = None,
        wait_reason: str | None = None, extra: dict | None = None,
    ) -> None:
        if not can_transition(self._current, to):
            print(
                f"[reporter] BLOCKED illegal transition {self._current} -> {to} "
                f"(job={self.job_id})"
            )
            return

        # Redis first (drives the live dashboard + status endpoint).
        mapping: dict[str, str] = {"agentStatus": to}
        if wait_reason is not None:
            mapping["status"] = "waiting_for_human"
            mapping["waitReason"] = wait_reason
        else:
            # Only stamp "running" for non-waiting, non-terminal states.
            if to not in (Status.COMPLETED, Status.CANCELLED, Status.FAILED):
                mapping["status"] = "running"
        try:
            self.r.hset(job_key(self.job_id), mapping=mapping)
        except Exception as e:
            print(f"[reporter] redis hset failed: {e}")

        # Firestore via API.
        try:
            await api_client.status_update(
                request_id=self.request_id,
                driver_id=self.driver_id,
                status=to,
                source=self.source,
                error=error,
                extra=extra,
            )
        except Exception as e:
            print(f"[reporter] status_update API call failed: {e}")

        self._current = to
        print(f"[reporter] job={self.job_id} status -> {to}")

    def sync_local(self, status: str) -> None:
        """Advance the local FSM cursor WITHOUT writing.

        Some status flips happen API-side as a side effect of an image upload
        (save_qr -> qrPaymentNeeded, save_captcha -> captchaSolving) so that the
        image and the status land together. After such a call the worker's local
        cursor would be stale and the next set_status() would be wrongly blocked.
        Call this to keep the cursor honest.
        """
        self._current = status

    def clear_wait(self) -> None:
        """Clear the waiting flag in Redis (after human input arrives)."""
        try:
            self.r.hdel(job_key(self.job_id), "waitReason")
            self.r.hset(job_key(self.job_id), "status", "running")
        except Exception as e:
            print(f"[reporter] clear_wait failed: {e}")
