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
//
// Everything else (amount, partnerDetails, vehicleDetails, processType,
// aiProcessTriggered, …) is written by other CabsWale services and is never
// touched here. All writes are merges; the doc is created if a request arrives
// before the client wrote it.
//
// Timestamps are ISO strings with the +05:30 offset to match the existing DS.

import { db } from "../firebase";
import { requestDocPath } from "../config";
import {
    type Status,
    assertTransition,
    isStatus,
    isTerminal,
    STATUS,
    TOP_LEVEL_STATUS,
} from "../lifecycle/statuses";

export function isoIST(d: Date = new Date()): string {
    const ist = new Date(d.getTime() + 5.5 * 3600_000);
    const p = (n: number) => String(n).padStart(2, "0");
    return (
        `${ist.getUTCFullYear()}-${p(ist.getUTCMonth() + 1)}-${p(ist.getUTCDate())}` +
        `T${p(ist.getUTCHours())}:${p(ist.getUTCMinutes())}:${p(ist.getUTCSeconds())}+05:30`
    );
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
 */
export async function setAgentStatus(opts: SetStatusOpts): Promise<Status> {
    const { requestId, driverId, to } = opts;
    const ref = docRef(requestId, driverId);

    await db.runTransaction(async (tx) => {
        if (!opts.force) {
            const snap = await tx.get(ref);
            const cur = snap.get("aiAgentData.status");
            const from: Status = isStatus(cur) ? cur : STATUS.QUEUED;
            assertTransition(from, to);
        }
        const aiAgentData: Record<string, unknown> = {
            status: to,
            statusUpdatedAt: isoIST(),
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
        tx.set(ref, { aiAgentData, updatedAt: isoIST() }, { merge: true });
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
        { aiAgentData: fields, updatedAt: isoIST() },
        { merge: true },
    );
}

export async function saveAgentCost(
    requestId: string,
    driverId: string,
    agentCost: Record<string, unknown>,
): Promise<void> {
    await docRef(requestId, driverId).set(
        { agentCost, updatedAt: isoIST() },
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
    const now = isoIST();

    await db.runTransaction(async (tx) => {
        const snap = await tx.get(ref);
        const cur = snap.get("aiAgentData.status");
        const from: Status = isStatus(cur) ? cur : STATUS.QUEUED;
        assertTransition(from, to);

        const prevTop: string = snap.get("status") ?? "";

        const aiAgentData: Record<string, unknown> = {
            status: to,
            statusUpdatedAt: now,
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
            status: TOP_LEVEL_STATUS[to],
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

        tx.set(ref, topLevel, { merge: true });
    });
}
