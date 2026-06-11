// api/src/lifecycle/statuses.ts
//
// The border-tax request lifecycle, as a finite state machine.
//
// This file is the SINGLE SOURCE OF TRUTH for the agent lifecycle. The worker
// mirrors it in worker/src/lifecycle/status.py — if you change a status name or
// an allowed transition here, change it there too. Everything (API routes, the
// worker reporter, the Firestore `aiAgentData.status` field, the app UI) keys
// off these exact strings.
//
//   queued                   -> validated + enqueued, waiting for a worker slot
//   aiAgentStarted           -> worker picked it up, browser is driving the portal
//   pendingTransaction       -> a stuck in-flight tx was found; AI is clearing it
//   captchaSolving           -> main captcha shown to the user, waiting for input
//   settingUpPaymentRequest  -> captcha accepted, selecting SBI -> UPI gateway
//   qrPaymentNeeded          -> UPI QR shown to the user, waiting for payment
//   verifyingPayment         -> user/portal signalled payment, confirming receipt
//   completed                -> receipt captured (TERMINAL, success)
//   cancelled                -> stopped before any money moved (TERMINAL, safe)
//   failed                   -> payment attempted but unconfirmed (TERMINAL, reconcile)

export const STATUS = {
    QUEUED: "queued",
    AI_AGENT_STARTED: "aiAgentStarted",
    PENDING_TRANSACTION: "pendingTransaction",
    CAPTCHA_SOLVING: "captchaSolving",
    SETTING_UP_PAYMENT_REQUEST: "settingUpPaymentRequest",
    QR_PAYMENT_NEEDED: "qrPaymentNeeded",
    VERIFYING_PAYMENT: "verifyingPayment",
    COMPLETED: "completed",
    CANCELLED: "cancelled",
    FAILED: "failed",
} as const;

export type Status = (typeof STATUS)[keyof typeof STATUS];

export const TERMINAL_STATUSES: ReadonlySet<Status> = new Set([
    STATUS.COMPLETED,
    STATUS.CANCELLED,
    STATUS.FAILED,
]);

// Allowed forward transitions. Self-transitions (X -> X) on non-terminal states
// are always allowed (idempotent re-emit, e.g. pushing a fresh captcha image or
// a refreshed QR). Any transition FROM a terminal state is rejected.
const ALLOWED: Record<Status, Status[]> = {
    [STATUS.QUEUED]: [STATUS.AI_AGENT_STARTED, STATUS.CANCELLED, STATUS.FAILED],
    [STATUS.AI_AGENT_STARTED]: [
        STATUS.PENDING_TRANSACTION,
        STATUS.CAPTCHA_SOLVING,
        STATUS.CANCELLED,
    ],
    [STATUS.PENDING_TRANSACTION]: [STATUS.AI_AGENT_STARTED, STATUS.CANCELLED],
    [STATUS.CAPTCHA_SOLVING]: [
        STATUS.SETTING_UP_PAYMENT_REQUEST,
        STATUS.CANCELLED,
    ],
    [STATUS.SETTING_UP_PAYMENT_REQUEST]: [
        STATUS.QR_PAYMENT_NEEDED,
        STATUS.CANCELLED,
        STATUS.FAILED,
    ],
    [STATUS.QR_PAYMENT_NEEDED]: [STATUS.VERIFYING_PAYMENT, STATUS.FAILED],
    [STATUS.VERIFYING_PAYMENT]: [STATUS.COMPLETED, STATUS.FAILED],
    [STATUS.COMPLETED]: [],
    [STATUS.CANCELLED]: [],
    [STATUS.FAILED]: [],
};

export function isStatus(value: string): value is Status {
    return (Object.values(STATUS) as string[]).includes(value);
}

export function isTerminal(status: Status): boolean {
    return TERMINAL_STATUSES.has(status);
}

/** Whether `to` is a legal next status given the current `from`. */
export function canTransition(from: Status, to: Status): boolean {
    if (TERMINAL_STATUSES.has(from)) return false;
    if (from === to) return true; // idempotent re-emit on a non-terminal state
    return (ALLOWED[from] ?? []).includes(to);
}

/** Throws if the transition is illegal. Use this to guard every status write. */
export function assertTransition(from: Status, to: Status): void {
    if (!canTransition(from, to)) {
        throw new Error(
            `illegal status transition: "${from}" -> "${to}" is not allowed`,
        );
    }
}

// How each terminal lifecycle status maps onto the top-level request `status`
// field that the rest of the CabsWale system reads.
export const TOP_LEVEL_STATUS: Partial<Record<Status, string>> = {
    [STATUS.COMPLETED]: "completed",
    [STATUS.CANCELLED]: "cancelled",
    [STATUS.FAILED]: "failed",
};
