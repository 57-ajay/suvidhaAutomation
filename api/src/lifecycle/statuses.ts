// The border-tax request lifecycle as a finite state machine.
//
// SINGLE SOURCE OF TRUTH for the agent lifecycle. The worker mirrors it in
// worker/src/lifecycle/status.py — change a status or transition here, change
// it there too. The API, the worker reporter, aiAgentData.status in Firestore,
// and the client UI all key off these exact strings.
//
//   queued                  -> validated + enqueued, waiting for a worker slot
//   aiAgentStarted          -> worker picked it up; browser is filling the portal
//   pendingTransaction      -> stuck in-flight tx found; auto-clear in progress
//   pendingTransactionCaptcha -> pending-tx captcha shown to the user
//                                  (only when AI is OFF or fell back)
//   captchaSolving          -> captcha image pushed, waiting for the user's text
//   settingUpPaymentRequest -> captcha accepted; driving gateway -> UPI
//   qrPaymentNeeded         -> UPI QR pushed, waiting for the payment
//   verifyingPayment        -> payment signal seen, confirming with the portal
//   generatingReceipt       -> payment confirmed; capturing + uploading receipt
//   completed               -> receipt captured            (TERMINAL, success)
//   cancelled               -> stopped before money moved  (TERMINAL, retryable)
//   failed                  -> payment attempted, unconfirmed or post-payment
//                              failure -> manualReview      (TERMINAL, reconcile)

export const STATUS = {
    QUEUED: "queued",
    AI_AGENT_STARTED: "aiAgentStarted",
    PENDING_TRANSACTION: "pendingTransaction",
    PENDING_TRANSACTION_CAPTCHA: "pendingTransactionCaptcha",
    CAPTCHA_SOLVING: "captchaSolving",
    SETTING_UP_PAYMENT_REQUEST: "settingUpPaymentRequest",
    QR_PAYMENT_NEEDED: "qrPaymentNeeded",
    VERIFYING_PAYMENT: "verifyingPayment",
    GENERATING_RECEIPT: "generatingReceipt",
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

// Allowed forward transitions. Self-transitions (X -> X) on non-terminal
// states are always allowed (idempotent re-emit: fresh captcha image,
// refreshed QR). Any transition FROM a terminal state is rejected.
//
// Money line: everything up to captchaSolving can only end in `cancelled`;
// from settingUpPaymentRequest onward a stop is `failed` (reconcile).
const ALLOWED: Record<Status, Status[]> = {
    [STATUS.QUEUED]: [STATUS.AI_AGENT_STARTED, STATUS.CANCELLED, STATUS.FAILED],
    [STATUS.AI_AGENT_STARTED]: [
        STATUS.PENDING_TRANSACTION,
        STATUS.SETTING_UP_PAYMENT_REQUEST, // AI solved the disclaimer captcha silently
        STATUS.CAPTCHA_SOLVING,
        STATUS.CANCELLED,
    ],
    [STATUS.PENDING_TRANSACTION]: [STATUS.AI_AGENT_STARTED, STATUS.CANCELLED],
    [STATUS.CAPTCHA_SOLVING]: [
        STATUS.SETTING_UP_PAYMENT_REQUEST,
        STATUS.PENDING_TRANSACTION_CAPTCHA, // human (or AI-fallback) pending captcha
        STATUS.CANCELLED,
    ],
    [STATUS.SETTING_UP_PAYMENT_REQUEST]: [
        STATUS.QR_PAYMENT_NEEDED,
        STATUS.CANCELLED,
        STATUS.FAILED,
    ],
    // generatingReceipt is reachable directly from qrPaymentNeeded: the user
    // can pay so fast that the first decisive thing we poll is the receipt page.
    [STATUS.QR_PAYMENT_NEEDED]: [
        STATUS.VERIFYING_PAYMENT,
        STATUS.GENERATING_RECEIPT,
        STATUS.FAILED,
    ],
    // verifyingPayment -> completed stays legal so a late job-completed from a
    // crashed worker can still close the request.
    [STATUS.VERIFYING_PAYMENT]: [
        STATUS.GENERATING_RECEIPT,
        STATUS.COMPLETED,
        STATUS.FAILED,
    ],
    // pendingTransactionCaptcha is PRE-PAYMENT: a stop here is always cancelled.
    // -> pendingTransaction once accepted (the bank-icon clear is progress),
    // -> aiAgentStarted on the open_portal restart after a successful clear.
    [STATUS.PENDING_TRANSACTION_CAPTCHA]: [
        STATUS.PENDING_TRANSACTION,
        STATUS.AI_AGENT_STARTED,
        STATUS.CANCELLED,
    ],
    [STATUS.GENERATING_RECEIPT]: [STATUS.COMPLETED, STATUS.FAILED],
    [STATUS.COMPLETED]: [],
    [STATUS.CANCELLED]: [],
    [STATUS.FAILED]: [],
};

export function isStatus(value: unknown): value is Status {
    return (
        typeof value === "string" &&
        (Object.values(STATUS) as string[]).includes(value)
    );
}

export function isTerminal(status: Status): boolean {
    return TERMINAL_STATUSES.has(status);
}

export function canTransition(from: Status, to: Status): boolean {
    if (TERMINAL_STATUSES.has(from)) return false;
    if (from === to) return true;
    return (ALLOWED[from] ?? []).includes(to);
}

/** Throws on an illegal transition. Guards every status write. */
export function assertTransition(from: Status, to: Status): void {
    if (!canTransition(from, to)) {
        throw new Error(
            `illegal status transition: "${from}" -> "${to}" is not allowed`,
        );
    }
}

// How each terminal lifecycle status maps onto the top-level request `status`
// field the rest of the CabsWale system reads.
export const TOP_LEVEL_STATUS: Partial<Record<Status, string>> = {
    [STATUS.COMPLETED]: "completed",
    [STATUS.CANCELLED]: "cancelled",
    [STATUS.FAILED]: "failed",
};
