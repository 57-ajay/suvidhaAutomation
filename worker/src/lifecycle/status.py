# worker/src/lifecycle/status.py
"""Lifecycle status mirror of api/src/lifecycle/statuses.ts.

Keep this in lockstep with the TypeScript source of truth. The worker uses these
strings when reporting status; the API re-validates every transition, so a drift
here surfaces as a 409 from /api/internal/status-update rather than silent
corruption — but don't rely on that, keep them identical.
"""

from __future__ import annotations


class Status:
    QUEUED = "queued"
    AI_AGENT_STARTED = "aiAgentStarted"
    PENDING_TRANSACTION = "pendingTransaction"
    CAPTCHA_SOLVING = "captchaSolving"
    SETTING_UP_PAYMENT_REQUEST = "settingUpPaymentRequest"
    QR_PAYMENT_NEEDED = "qrPaymentNeeded"
    VERIFYING_PAYMENT = "verifyingPayment"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL = {Status.COMPLETED, Status.CANCELLED, Status.FAILED}

_ALLOWED: dict[str, set[str]] = {
    Status.QUEUED: {Status.AI_AGENT_STARTED, Status.CANCELLED, Status.FAILED},
    Status.AI_AGENT_STARTED: {
        Status.PENDING_TRANSACTION,
        Status.CAPTCHA_SOLVING,
        Status.CANCELLED,
    },
    Status.PENDING_TRANSACTION: {Status.AI_AGENT_STARTED, Status.CANCELLED},
    Status.CAPTCHA_SOLVING: {Status.SETTING_UP_PAYMENT_REQUEST, Status.CANCELLED},
    Status.SETTING_UP_PAYMENT_REQUEST: {
        Status.QR_PAYMENT_NEEDED,
        Status.CANCELLED,
        Status.FAILED,
    },
    Status.QR_PAYMENT_NEEDED: {Status.VERIFYING_PAYMENT, Status.FAILED},
    Status.VERIFYING_PAYMENT: {Status.COMPLETED, Status.FAILED},
    Status.COMPLETED: set(),
    Status.CANCELLED: set(),
    Status.FAILED: set(),
}


def can_transition(frm: str, to: str) -> bool:
    if frm in TERMINAL:
        return False
    if frm == to:
        return True
    return to in _ALLOWED.get(frm, set())
