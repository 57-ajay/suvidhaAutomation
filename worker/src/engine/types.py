"""Core types shared by every phase, step, and runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel


class StepStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    RETRIED = "retried"
    HANDED_OFF = "handed_off"


class StepLog(BaseModel):
    index: int
    name: str
    status: StepStatus
    attempt: int | None = None
    duration_ms: int | None = None
    selector: str | None = None
    value: str | None = None
    url: str | None = None
    error: str | None = None
    ai_cost_usd: float | None = None


class RunOutcome(BaseModel):
    # done | cancelled | failed | partial
    status: str
    summary: str
    error: str | None = None
    abort_reason: str | None = None
    receipt_url: str | None = None
    receipt_number: str | None = None
    amount: float | None = None
    transaction_id: str | None = None
    payment_likely: bool = False
    total_cost_usd: float = 0.0
    run_log: list[StepLog] = []


class ScriptedAbort(Exception):
    """Stop the run with a user-readable message.

    terminal:
      "cancelled" — nothing irreversible happened; the request is retryable.
      "failed"    — money may have moved; the API flips manualReview.
    """

    def __init__(self, message: str, *, terminal: str = "cancelled",
                 reason: str | None = None):
        super().__init__(message)
        self.message = message
        self.terminal = terminal
        self.reason = reason or message


class RestartFrom(Exception):
    """Raised by a phase to restart the pipeline from a named earlier phase
    (e.g. after a pending-transaction clear). The pipeline caps restarts."""

    def __init__(self, phase_name: str, why: str):
        super().__init__(f"restart from {phase_name}: {why}")
        self.phase_name = phase_name
        self.why = why


@dataclass
class RunContext:
    """Everything a phase needs. Phases mutate `scratch` to pass values
    forward; the final phase sets `outcome` (or returns it)."""

    session: Any  # browser_use BrowserSession
    params: Any   # tasks.border_tax.params.BorderTaxParams
    reporter: Any  # lifecycle.reporter.StatusReporter
    log: Any      # engine.log.StepLogger
    r: Any        # redis client
    job_id: str
    scratch: dict = field(default_factory=dict)
