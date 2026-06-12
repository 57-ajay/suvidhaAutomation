from __future__ import annotations

import redis

from config import REDIS_URL

QUEUE_KEY = "job:queue"

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
