// api/src/internal/requestDoc.ts
//
// Reads and writes the request document at the configured path. WE own only the
// `aiAgentData` subtree (plus terminal top-level status + agentCost). The rest
// of the document (amount, partnerDetails, vehicleDetails, ...) is written by
// the existing CabsWale flow and we never touch it.
//
// Shape of the part we maintain (see the DS in the project brief):
//
//   aiAgentData: {
//     status, statusUpdatedAt, source,
//     paymentCompleted, paymentCompletedAt,
//     receiptGenerated, receiptGeneratedAt,
//     qrCode:  { url, uploadedAt, expiredAt, notificationSent, notificationSentAt },
//     captcha: { url, attempt, uploadedAt },          // pivot-a addition
//     error:   { isError, message }
//   }
//   agentCost: { ... }
//   processType, aiProcessTriggered
//   manualReview: { required, reason, resolved, resolvedAt, remarks }
//   status (top-level), statusUpdateHistory[], receiptDocumentUrl, cancelledDetails

import { db, FieldValue } from "../firebase";
import { requestDocPath } from "../config";
import {
    type Status,
    assertTransition,
    isStatus,
    TOP_LEVEL_STATUS,
} from "../lifecycle/statuses";

function docRef(requestId: string, driverId: string) {
    return db.doc(requestDocPath(requestId, driverId));
}

/** Current lifecycle status from aiAgentData, or "queued" if absent. */
export async function getAgentStatus(
    requestId: string,
    driverId: string,
): Promise<Status> {
    const snap = await docRef(requestId, driverId).get();
    const cur = snap.get("aiAgentData.status");
    return isStatus(cur) ? cur : "queued";
}

interface SetStatusOpts {
    requestId: string;
    driverId: string;
    to: Status;
    source?: string;
    error?: string | null;
    // Extra fields to merge under aiAgentData in the same write.
    extra?: Record<string, unknown>;
    // Skip the FSM guard (only for the very first write from queued).
    force?: boolean;
}

/**
 * Update aiAgentData.status with the FSM guard applied. Merges, never clobbers
 * sibling fields. Returns the status actually written.
 */
export async function setAgentStatus(opts: SetStatusOpts): Promise<Status> {
    const { requestId, driverId, to } = opts;
    const ref = docRef(requestId, driverId);

    if (!opts.force) {
        const from = await getAgentStatus(requestId, driverId);
        assertTransition(from, to); // throws on illegal transition
    }

    const aiAgentData: Record<string, unknown> = {
        status: to,
        statusUpdatedAt: FieldValue.serverTimestamp(),
        ...(opts.source ? { source: opts.source } : {}),
        ...(opts.error !== undefined
            ? { error: { isError: opts.error != null, message: opts.error ?? "" } }
            : {}),
        ...(opts.extra ?? {}),
    };

    await ref.set(
        {
            aiAgentData,
            aiProcessTriggered: true,
            processType: "auto",
        },
        { merge: true },
    );

    console.log(`[requestDoc] ${requestId} aiAgentData.status -> ${to}`);
    return to;
}

/** Merge an arbitrary patch under aiAgentData (used by qr/captcha/receipt writers). */
export async function patchAiAgentData(
    requestId: string,
    driverId: string,
    patch: Record<string, unknown>,
): Promise<void> {
    await docRef(requestId, driverId).set({ aiAgentData: patch }, { merge: true });
}

/** Write the agentCost block (shape mirrors the DS in the brief). */
export async function saveAgentCost(
    requestId: string,
    driverId: string,
    agentCost: Record<string, unknown>,
): Promise<void> {
    await docRef(requestId, driverId).set({ agentCost }, { merge: true });
}

interface TerminalOpts {
    requestId: string;
    driverId: string;
    to: Extract<Status, "completed" | "cancelled" | "failed">;
    source?: string;
    summary?: string;
    error?: string | null;
    receiptUrl?: string | null;
    paymentCompleted?: boolean;
    transactionId?: string;
}

/**
 * Apply a terminal status: updates aiAgentData, the top-level `status`, appends
 * to statusUpdateHistory, and (on failure) flags manualReview. This is the only
 * place the top-level request status is moved by the agent.
 */
export async function applyTerminal(opts: TerminalOpts): Promise<void> {
    const { requestId, driverId, to } = opts;
    const ref = docRef(requestId, driverId);

    await db.runTransaction(async (tx) => {
        const snap = await tx.get(ref);
        const from: Status = isStatus(snap.get("aiAgentData.status"))
            ? snap.get("aiAgentData.status")
            : "queued";
        assertTransition(from, to);

        const beforeTop = (snap.get("status") as string) ?? "";
        const topLevel = TOP_LEVEL_STATUS[to] ?? beforeTop;

        const aiAgentData: Record<string, unknown> = {
            status: to,
            statusUpdatedAt: FieldValue.serverTimestamp(),
            ...(opts.source ? { source: opts.source } : {}),
            error: {
                isError: opts.error != null,
                message: opts.error ?? "",
            },
        };
        if (to === "completed") {
            aiAgentData.paymentCompleted = true;
            aiAgentData.paymentCompletedAt = FieldValue.serverTimestamp();
            aiAgentData.receiptGenerated = !!opts.receiptUrl;
            if (opts.receiptUrl)
                aiAgentData.receiptGeneratedAt = FieldValue.serverTimestamp();
        } else {
            aiAgentData.paymentCompleted = !!opts.paymentCompleted;
        }

        const patch: Record<string, unknown> = {
            aiAgentData,
            status: topLevel,
            updatedAt: FieldValue.serverTimestamp(),
            statusUpdateHistory: FieldValue.arrayUnion({
                beforeStatus: beforeTop,
                afterStatus: topLevel,
                at: new Date().toISOString(),
                transactionId: opts.transactionId ?? "",
            }),
        };

        if (opts.receiptUrl) patch.receiptDocumentUrl = opts.receiptUrl;

        if (to === "cancelled") {
            patch.cancelledDetails = {
                reason: opts.summary ?? opts.error ?? "cancelled",
                at: new Date().toISOString(),
            };
        }
        if (to === "failed") {
            // Money may be in limbo: queue a human review rather than silently failing.
            patch.manualReview = {
                required: true,
                reason: opts.error ?? opts.summary ?? "payment unconfirmed",
                resolved: false,
                resolvedAt: null,
                remarks: "",
            };
            patch.pendingRemark = opts.error ?? opts.summary ?? null;
        }

        tx.set(ref, patch, { merge: true });
    });

    console.log(`[requestDoc] ${requestId} TERMINAL -> ${to}`);
}
