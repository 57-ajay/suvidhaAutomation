"""Lifecycle status mirror of api/src/lifecycle/statuses.ts.

Keep in lockstep with the TypeScript source of truth. The API re-validates
every transition, so drift surfaces as a 409 from status-update rather than
silent corruption — but don't rely on that; keep them identical.
"""

from __future__ import annotations


class Status:
    QUEUED = "queued"
    AI_AGENT_STARTED = "aiAgentStarted"
    PENDING_TRANSACTION = "pendingTransaction"
    PENDING_TRANSACTION_CAPTCHA = "pendingTransactionCaptcha"
    CAPTCHA_SOLVING = "captchaSolving"
    SETTING_UP_PAYMENT_REQUEST = "settingUpPaymentRequest"
    QR_PAYMENT_NEEDED = "qrPaymentNeeded"
    VERIFYING_PAYMENT = "verifyingPayment"
    VERIFYING_PENDING_PAYMENT = "verifyingPendingPayment"
    GENERATING_RECEIPT = "generatingReceipt"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL = {Status.COMPLETED, Status.CANCELLED, Status.FAILED}

# Statuses from which an unexpected stop means no money has moved.
PRE_PAYMENT = {
    Status.QUEUED,
    Status.AI_AGENT_STARTED,
    Status.PENDING_TRANSACTION,
    Status.PENDING_TRANSACTION_CAPTCHA,
    Status.CAPTCHA_SOLVING,
}

_ALLOWED: dict[str, set[str]] = {
    Status.QUEUED: {Status.AI_AGENT_STARTED, Status.CANCELLED, Status.FAILED},
    Status.AI_AGENT_STARTED: {
        Status.PENDING_TRANSACTION,
        Status.CAPTCHA_SOLVING,
        Status.SETTING_UP_PAYMENT_REQUEST,
        Status.CANCELLED,
    },
    Status.PENDING_TRANSACTION: {
        Status.AI_AGENT_STARTED,
        Status.PENDING_TRANSACTION_CAPTCHA,
        Status.CANCELLED,
    },
    Status.PENDING_TRANSACTION_CAPTCHA: {
        Status.PENDING_TRANSACTION,
        Status.AI_AGENT_STARTED,
        Status.CANCELLED,
    },
    Status.CAPTCHA_SOLVING: {
        Status.SETTING_UP_PAYMENT_REQUEST,
        Status.CANCELLED,
    },
    Status.SETTING_UP_PAYMENT_REQUEST: {
        Status.QR_PAYMENT_NEEDED,
        Status.CANCELLED,
        Status.FAILED,
    },
    Status.QR_PAYMENT_NEEDED: {
        Status.VERIFYING_PAYMENT,
        Status.GENERATING_RECEIPT,
        Status.VERIFYING_PENDING_PAYMENT,
        Status.FAILED,
    },
    Status.VERIFYING_PAYMENT: {
        Status.GENERATING_RECEIPT,
        Status.VERIFYING_PENDING_PAYMENT,
        Status.COMPLETED,
        Status.FAILED,
    },
    Status.VERIFYING_PENDING_PAYMENT: {
        Status.GENERATING_RECEIPT,
        Status.FAILED,
    },
    Status.GENERATING_RECEIPT: {Status.COMPLETED, Status.FAILED},
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
