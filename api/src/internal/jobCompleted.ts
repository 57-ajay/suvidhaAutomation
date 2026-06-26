// Terminal handler. The worker POSTs here exactly once per run.
//
// Worker outcome -> lifecycle terminal:
//   "done"      -> completed
//   "cancelled" -> cancelled  (no money moved; safe to retry)
//   "failed"    -> failed     (payment attempted, unconfirmed -> manualReview)
//   "partial"   -> failed     (payment likely succeeded but a post-payment step
//                              failed — reconcile, never a silent success)

import { applyTerminal, saveAgentCost, ts } from "./requestDoc";
import { STATUS } from "../lifecycle/statuses";

export interface JobCompletedInput {
    jobId: string;
    requestId: string;
    driverId: string;
    status: "done" | "cancelled" | "failed" | "partial" | "parked";
    source?: string;
    summary?: string;
    error?: string | null;
    receiptUrl?: string | null;
    transactionId?: string; // bank reference, captured even on failure
    paymentLikely?: boolean; // worker's read on whether money moved
    costData?: Record<string, unknown> | null;
    task?: string;
}

export async function handleJobCompleted(input: JobCompletedInput) {
    const { jobId, requestId, driverId } = input;
    if (!requestId || !driverId || !jobId) {
        return { ok: false, error: "jobId, requestId, driverId required" };
    }

    // Persist cost first so it survives even if the terminal write races.
    if (input.costData) {
        const agentCost = {
            costSource: "scripted_aggregate",
            entryCount: 1,
            jobId,
            savedAt: ts(),
            source: input.source ?? "app",
            totalCachedCost: 0,
            totalCachedTokens: 0,
            totalCompletionCost: 0,
            totalCompletionTokens: 0,
            totalPromptCost: 0,
            totalPromptTokens: 0,
            totalTokens: 0,
            totalCost: 0,
            ...input.costData,
        };
        await saveAgentCost(requestId, driverId, agentCost, input.task).catch((e) =>
            console.error(`[jobCompleted] saveAgentCost failed: ${e.message}`),
        );
    }

    if (input.status === "parked") {
        // Parked, not terminal. The reporter already wrote
        // verifyingPendingPayment; we only persisted cost above. No applyTerminal.
        return { ok: true, status: STATUS.VERIFYING_PENDING_PAYMENT };
    }

    const to =
        input.status === "done"
            ? STATUS.COMPLETED
            : input.status === "cancelled"
                ? STATUS.CANCELLED
                : STATUS.FAILED;

    try {
        await applyTerminal({
            requestId,
            driverId,
            task: input.task,
            to,
            source: input.source,
            summary: input.summary,
            error: input.error ?? null,
            receiptUrl: input.receiptUrl ?? null,
            paymentCompleted:
                to === STATUS.COMPLETED ? true : !!input.paymentLikely,
            transactionId: input.transactionId,
        });
        return { ok: true, status: to };
    } catch (e: any) {
        // An illegal transition here usually means a duplicate/late completion;
        // surface it but don't 500 the worker's shutdown path.
        console.error(
            `[jobCompleted] terminal write rejected for ${requestId}: ${e.message}`,
        );
        return { ok: false, error: e.message };
    }
}
