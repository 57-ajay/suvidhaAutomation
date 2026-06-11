// api/src/internal/jobCompleted.ts
//
// Terminal handler. The worker POSTs here once, when a run finishes. It maps the
// worker's RunOutcome status onto a lifecycle terminal and writes everything in
// the right order: receipt (if any) -> agentCost -> applyTerminal.
//
// Worker status -> lifecycle terminal:
//   "done"      -> completed
//   "cancelled" -> cancelled   (no money moved; safe to retry)
//   "failed"    -> failed      (payment attempted, unconfirmed; manualReview)
//   "partial"   -> failed      (payment likely went through but post-payment
//                               step failed — treat as reconcile, not success)

import { applyTerminal, saveAgentCost } from "./requestDoc";
import { STATUS } from "../lifecycle/statuses";

export interface JobCompletedInput {
    jobId: string;
    requestId: string;
    driverId: string;
    status: "done" | "cancelled" | "failed" | "partial";
    source?: string;
    summary?: string;
    error?: string | null;
    receiptUrl?: string | null;
    transactionId?: string; // SBI / bank reference, captured even on failure
    paymentLikely?: boolean; // worker's read on whether money moved
    costData?: Record<string, unknown> | null;
}

export async function handleJobCompleted(input: JobCompletedInput) {
    const { jobId, requestId, driverId } = input;
    if (!requestId || !driverId || !jobId) {
        return { ok: false, error: "jobId, requestId, driverId required" };
    }

    // 1. Persist cost first so it survives even if the terminal write races.
    if (input.costData) {
        const agentCost = {
            costSource: "scripted_aggregate",
            entryCount: 1,
            jobId,
            savedAt: new Date().toISOString(),
            source: input.source ?? "app",
            totalCachedCost: 0,
            totalCachedTokens: 0,
            totalCompletionCost: 0,
            totalCompletionTokens: 0,
            totalPromptCost: 0,
            totalPromptTokens: 0,
            totalTokens: 0,
            ...input.costData,
        };
        await saveAgentCost(requestId, driverId, agentCost).catch((e) =>
            console.error(`[jobCompleted] saveAgentCost failed: ${e.message}`),
        );
    }

    // 2. Apply terminal.
    const to =
        input.status === "done"
            ? STATUS.COMPLETED
            : input.status === "cancelled"
              ? STATUS.CANCELLED
              : STATUS.FAILED; // failed + partial both land here

    try {
        await applyTerminal({
            requestId,
            driverId,
            to,
            source: input.source,
            summary: input.summary,
            error: input.error ?? null,
            receiptUrl: input.receiptUrl ?? null,
            paymentCompleted: to === STATUS.COMPLETED ? true : !!input.paymentLikely,
            transactionId: input.transactionId,
        });
    } catch (e: any) {
        // An illegal transition here usually means a duplicate/late completion.
        console.error(`[jobCompleted] applyTerminal rejected: ${e.message}`);
        return { ok: false, error: e.message };
    }

    return { ok: true, terminal: to };
}
