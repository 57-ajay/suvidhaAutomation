"""Single place the worker reports lifecycle status from.

set_status() does three things:
  1. Local FSM guard (mirrors the API) so an illegal jump is never attempted.
  2. Update the Redis job hash (status + optional waitReason + extras) so the
     console and /api/jobs/:id/status reflect reality immediately.
  3. POST status-update so the API writes aiAgentData.status to Firestore —
     the client's source of truth.

Some flips happen API-side as a side effect of an upload (save-qr ->
qrPaymentNeeded, save-captcha -> captchaSolving) so image and status land
together; call sync_local() afterwards to keep the cursor honest.
"""

from __future__ import annotations

import redis

import api_client
from redis_client import job_key
from .status import Status, can_transition, TERMINAL


class StatusReporter:
    def __init__(
        self,
        *,
        job_id: str,
        request_id: str,
        driver_id: str,
        source: str,
        r: redis.Redis,
        task: str = "border-tax",
        extra: dict | None = None,
    ):
        self.job_id = job_id
        self.request_id = request_id
        self.driver_id = driver_id
        self.source = source
        self.r = r
        self.task = task
        # Merged into the `extra` of EVERY status_update. Tasks whose Firestore
        # target needs more than requestId to address set it at dispatch —
        # challan-payment writes into challans/{vehicleNumber}/subChallans/
        # {challanNo}, which no requestDocPath template can express.
        self.extra = extra or {}
        self._current = Status.QUEUED

    @property
    def current(self) -> str:
        return self._current

    async def set_status(
        self,
        to: str,
        *,
        error: str | None = None,
        wait_reason: str | None = None,
        extra: dict | None = None,
        redis_extra: dict | None = None,
    ) -> None:
        if not can_transition(self._current, to):
            print(
                f"[reporter] BLOCKED illegal {self._current} -> {to} "
                f"(job={self.job_id})"
            )
            return

        mapping: dict[str, str] = {"agentStatus": to}
        if wait_reason is not None:
            mapping["status"] = "waiting_for_human"
            mapping["waitReason"] = wait_reason
        elif to not in TERMINAL:
            mapping["status"] = "running"
        if redis_extra:
            mapping.update({k: str(v) for k, v in redis_extra.items()})
        try:
            self.r.hset(job_key(self.job_id), mapping=mapping)
        except Exception as e:
            print(f"[reporter] redis hset failed: {e}")

        try:
            await api_client.status_update(
                request_id=self.request_id,
                driver_id=self.driver_id,
                status=to,
                source=self.source,
                error=error,
                extra={**self.extra, **(extra or {})} or None,
                task=self.task,
            )
        except Exception as e:
            print(f"[reporter] status_update failed: {e}")

        self._current = to
        print(f"[reporter] job={self.job_id} status -> {to}")

    def sync_local(self, status: str) -> None:
        """Advance the local cursor WITHOUT writing (after API-side flips)."""
        self._current = status
        try:
            self.r.hset(job_key(self.job_id), "agentStatus", status)
        except Exception:
            pass

    def set_wait(self, reason: str) -> None:
        try:
            self.r.hset(
                job_key(self.job_id),
                mapping={
                    "status": "waiting_for_human",
                    "waitReason": reason,
                },
            )
        except Exception as e:
            print(f"[reporter] set_wait failed: {e}")

    def clear_wait(self) -> None:
        try:
            self.r.hdel(job_key(self.job_id), "waitReason")
            self.r.hset(job_key(self.job_id), "status", "running")
        except Exception as e:
            print(f"[reporter] clear_wait failed: {e}")
