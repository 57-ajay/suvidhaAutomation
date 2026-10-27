from __future__ import annotations

import redis

from config import REDIS_URL

QUEUE_KEY = "job:queue"
# Jobs a worker has picked up and not yet finished. KEDA scales on
# LLEN(job:queue) + LLEN(job:active), so the fleet can drop to zero only once
# nothing is queued AND nothing is in flight. The API sweeper prunes entries
# a dead pod left behind (see api/src/sweeper.ts).
ACTIVE_KEY = "job:active"

SOCKET_TIMEOUT_SECS = 15


def get_redis() -> redis.Redis:
    return redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_timeout=SOCKET_TIMEOUT_SECS,
        socket_connect_timeout=10,
        health_check_interval=30,
    )


def job_key(job_id: str) -> str:
    return f"job:{job_id}"
