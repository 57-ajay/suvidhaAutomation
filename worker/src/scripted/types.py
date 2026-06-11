# worker/src/scripted/types.py
"""Shared types for the scripted runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StepStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    RETRIED = "retried"
    HANDED_OFF = "handed_off"


@dataclass
class StepLog:
    index: int
    name: str
    status: StepStatus
    url: str | None = None
    selector: str | None = None
    value: str | None = None
    attempt: int = 1
    duration_ms: int = 0
    error: str | None = None


@dataclass
class RunOutcome:
    """The single return type of every scripted run.

    status:
      "done"      -> completed (receipt captured)
      "cancelled" -> stopped pre-payment, no money moved
      "failed"    -> payment attempted, unconfirmed
      "partial"   -> payment likely went through but a post-payment step failed
    """

    status: str  # done | cancelled | failed | partial
    summary: str = ""
    error: str | None = None
    receipt_url: str | None = None
    transaction_id: str | None = None
    payment_likely: bool = False
    cost_data: dict | None = None
    run_log: list[dict] = field(default_factory=list)


class ScriptedAbort(Exception):
    """Raised inside a scripted run to bail with a reason.

    `terminal` is one of cancelled | failed | partial; the runner maps it to a
    RunOutcome. Default is cancelled (pre-payment, safe).
    """

    def __init__(self, message: str, *, terminal: str = "cancelled",
                 payment_likely: bool = False):
        super().__init__(message)
        self.message = message
        self.terminal = terminal
        self.payment_likely = payment_likely
