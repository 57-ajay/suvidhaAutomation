# worker/src/scripted/log.py
"""Append-only step logger. Each scripted step records a StepLog; the full list
is attached to the RunOutcome and surfaced to the API for the automation log."""

from __future__ import annotations

from dataclasses import asdict

from .types import StepLog


class StepLogger:
    def __init__(self) -> None:
        self._steps: list[StepLog] = []
        self._idx = 0

    def next_index(self) -> int:
        self._idx += 1
        return self._idx

    def record(self, step: StepLog) -> None:
        self._steps.append(step)
        print(
            f"[step {step.index:03d}] {step.name} "
            f"status={step.status.value} "
            f"{('sel=' + step.selector) if step.selector else ''} "
            f"{('err=' + step.error) if step.error else ''}"
        )

    def dump(self) -> list[dict]:
        return [asdict(s) | {"status": s.status.value} for s in self._steps]
