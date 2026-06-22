// Reads and writes the request document at the configured path. We own ONLY:
//
//   aiAgentData.{status, statusUpdatedAt, source, error,
//                captcha{...}, qrCode{...}, receipt{...},
//                paymentCompleted, paymentCompletedAt,
//                receiptGenerated, receiptGeneratedAt}
//   agentCost
//   manualReview                      (on failed)
//   cancelledDetails                  (on cancelled)
//   receiptDocumentUrl                (on completed)
//   status (top-level, terminal only) + statusUpdateHistory append
//   nextRequestAllowed                (on failed/cancelled — driver block)
//
// Everything else (amount, partnerDetails, vehicleDetails, processType,
// aiProcessTriggered, …) is written by other CabsWale services and is never
// touched here. All writes are merges; the doc is created if a request arrives
// before the client wrote it.
//

import { db, FieldValue, Timestamp } from "../firebase";
import { requestDocPath } from "../config";
import {
    type Status,
    assertTransition,
    isStatus,
    isTerminal,
    STATUS,
    TOP_LEVEL_STATUS,
} from "../lifecycle/statuses";

/** Firestore Timestamp for `d` (default: now). Use this for everything we
 *  persist. No IST shift — a Timestamp is an absolute instant. */
export function ts(d: Date = new Date()): Timestamp {
    return Timestamp.fromDate(d);
}

export function isoIST(d: Date = new Date()): string {
    const ist = new Date(d.getTime() + 5.5 * 3600_000);
    const p = (n: number) => String(n).padStart(2, "0");
    return (
        `${ist.getUTCFullYear()}-${p(ist.getUTCMonth() + 1)}-${p(ist.getUTCDate())}` +
        `T${p(ist.getUTCHours())}:${p(ist.getUTCMinutes())}:${p(ist.getUTCSeconds())}+05:30`
    );
}

// ─── Driver re-request blocking ───────────────────────────────────────────
// On any non-completed terminal the doc gets a root-level nextRequestAllowed
// (isoIST, like every other timestamp here). The app/backend enforces it; we
// only write. Discriminator mirrors main's driverUsage logic — the ground
// truth is whether a QR ever reached the driver:
//   QR generated, payment not made -> now + 45 minutes
//   no QR (docs invalid / portal blocked / abandoned earlier) -> next IST midnight
const QR_NO_PAYMENT_BLOCK_MS = 45 * 60 * 1000;
const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

export function afterFiveMinutes(from: Date = new Date()): Date {
    return new Date(from.getTime() + 5 * 60 * 1000);
}

export function nextISTMidnight(from: Date = new Date()): Date {
    const ist = new Date(from.getTime() + IST_OFFSET_MS);
    const nextMidnightISTAsUTC = Date.UTC(
        ist.getUTCFullYear(), ist.getUTCMonth(), ist.getUTCDate() + 1,
        0, 0, 0, 0,
    );
    return new Date(nextMidnightISTAsUTC - IST_OFFSET_MS);
}

function docRef(requestId: string, driverId: string) {
    return db.doc(requestDocPath(requestId, driverId));
}

export async function getAgentStatus(
    requestId: string,
    driverId: string,
): Promise<Status> {
    const snap = await docRef(requestId, driverId).get();
    const cur = snap.get("aiAgentData.status");
    return isStatus(cur) ? cur : STATUS.QUEUED;
}

export interface SetStatusOpts {
    requestId: string;
    driverId: string;
    to: Status;
    source?: string;
    error?: string | null;
    /** Extra fields merged under aiAgentData in the same write. */
    extra?: Record<string, unknown>;
    /** Skip the FSM guard — only for the very first write from queued. */
    force?: boolean;
}

/**
 * FSM-guarded status write, done in a transaction so concurrent writers can't
 * race past the guard. Merges; never clobbers sibling aiAgentData fields.
 * Every write also appends {from, to, at} to aiAgentData.statusHistory — the
 * append-only timeline the client can render per requestId.
 */
export async function setAgentStatus(opts: SetStatusOpts): Promise<Status> {
    const { requestId, driverId, to } = opts;
    const ref = docRef(requestId, driverId);

    await db.runTransaction(async (tx) => {
        const snap = await tx.get(ref);
        const cur = snap.get("aiAgentData.status");
        const from: Status = isStatus(cur) ? cur : STATUS.QUEUED;
        if (!opts.force) {
            assertTransition(from, to);
        }
        const now = isoIST();
        const aiAgentData: Record<string, unknown> = {
            status: to,
            statusUpdatedAt: ts(),
            statusHistory: FieldValue.arrayUnion({ from, to, at: now }),
            ...(opts.source ? { source: opts.source } : {}),
            ...(opts.error !== undefined
                ? {
                    error: {
                        isError: opts.error != null,
                        message: opts.error ?? "",
                    },
                }
                : {}),
            ...(opts.extra ?? {}),
        };
        tx.set(ref, { aiAgentData, updatedAt: ts() }, { merge: true });
    });

    return to;
}

/** Merge fields under aiAgentData without changing the status. */
export async function patchAiAgentData(
    requestId: string,
    driverId: string,
    fields: Record<string, unknown>,
): Promise<void> {
    await docRef(requestId, driverId).set(
        { aiAgentData: fields, updatedAt: ts() },
        { merge: true },
    );
}

export async function saveAgentCost(
    requestId: string,
    driverId: string,
    agentCost: Record<string, unknown>,
): Promise<void> {
    await docRef(requestId, driverId).set(
        { agentCost, updatedAt: ts() },
        { merge: true },
    );
}

export interface ApplyTerminalOpts {
    requestId: string;
    driverId: string;
    to: Status; // must be terminal
    source?: string;
    summary?: string;
    error?: string | null;
    receiptUrl?: string | null;
    paymentCompleted?: boolean;
    transactionId?: string;
}

/**
 * Write the terminal in one transaction: aiAgentData.status + flags, the
 * top-level status, the statusUpdateHistory append, and — depending on the
 * terminal — receiptDocumentUrl / manualReview / cancelledDetails.
 */
export async function applyTerminal(opts: ApplyTerminalOpts): Promise<void> {
    const { requestId, driverId, to } = opts;
    if (!isTerminal(to)) throw new Error(`applyTerminal called with "${to}"`);
    const ref = docRef(requestId, driverId);
    const now = ts();

    await db.runTransaction(async (tx) => {
        const snap = await tx.get(ref);
        const cur = snap.get("aiAgentData.status");
        const from: Status = isStatus(cur) ? cur : STATUS.QUEUED;
        assertTransition(from, to);

        const prevTop: string = snap.get("status") ?? "";

        const aiAgentData: Record<string, unknown> = {
            status: to,
            statusUpdatedAt: now,
            statusHistory: FieldValue.arrayUnion({ from, to, at: now }),
            ...(opts.source ? { source: opts.source } : {}),
            error: { isError: opts.error != null, message: opts.error ?? "" },
            paymentCompleted: opts.paymentCompleted ?? to === STATUS.COMPLETED,
            ...(opts.paymentCompleted || to === STATUS.COMPLETED
                ? { paymentCompletedAt: now }
                : {}),
            ...(to === STATUS.COMPLETED && opts.receiptUrl
                ? { receiptGenerated: true, receiptGeneratedAt: now }
                : {}),
        };

        const topLevel: Record<string, unknown> = {
            aiAgentData,
            updatedAt: now,
            // status: TOP_LEVEL_STATUS[to],
            statusUpdateHistory: [
                ...((snap.get("statusUpdateHistory") as unknown[]) ?? []),
                {
                    beforeStatus: prevTop,
                    afterStatus: TOP_LEVEL_STATUS[to],
                    at: now,
                    transactionId: opts.transactionId ?? "",
                },
            ],
        };

        if (to === STATUS.COMPLETED && opts.receiptUrl) {
            topLevel.receiptDocumentUrl = opts.receiptUrl;
        }
        if (to === STATUS.FAILED) {
            topLevel.manualReview = {
                required: true,
                reason: opts.error ?? opts.summary ?? "agent run failed",
                resolved: false,
                resolvedAt: null,
                remarks: "",
            };
        }
        if (to === STATUS.CANCELLED) {
            topLevel.cancelledDetails = {
                cancelledAt: now,
                cancelledBy: "ai_agent",
                reason: opts.error ?? opts.summary ?? "cancelled before payment",
            };
        }
        if (to !== STATUS.COMPLETED) {
            const hasQR = !!(
                snap.get("aiAgentData.qrCode.url") ?? snap.get("qrCodeUrl")
            );
            const unblockAt = hasQR
                ? new Date(Date.now() + QR_NO_PAYMENT_BLOCK_MS)
                : afterFiveMinutes();
            topLevel.nextRequestAllowed = ts(unblockAt);
        }

        tx.set(ref, topLevel, { merge: true });
    });
}
