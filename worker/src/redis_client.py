# worker/src/redis_client.py
"""Thin redis helpers shared across the worker."""

from __future__ import annotations

import redis

from config import REDIS_URL


def get_redis() -> redis.Redis:
    return redis.from_url(REDIS_URL)


def job_key(job_id: str) -> str:
    return f"job:{job_id}"
