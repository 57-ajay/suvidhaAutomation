"""Ordered record of every step in a run. Dumped into the Redis job hash at
the end so any failure can be replayed step by step."""

from __future__ import annotations

from .types import StepLog


class StepLogger:
    def __init__(self) -> None:
        self._entries: list[StepLog] = []
        self._index = 0

    def next_index(self) -> int:
        self._index += 1
        return self._index

    def record(self, entry: StepLog) -> None:
        self._entries.append(entry)
        tag = entry.status.value.upper()
        extras = []
        if entry.attempt:
            extras.append(f"attempt={entry.attempt}")
        if entry.duration_ms is not None:
            extras.append(f"{entry.duration_ms}ms")
        if entry.error:
            extras.append(f"error={entry.error}")
        print(f"[step {entry.index:03d}] {tag:10s} {entry.name}"
              + (f" ({', '.join(extras)})" if extras else ""))

    def dump(self) -> list[StepLog]:
        return list(self._entries)

    def total_ai_cost(self) -> float:
        return sum(e.ai_cost_usd or 0.0 for e in self._entries)
