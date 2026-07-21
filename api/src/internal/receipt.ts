// Worker calls this with the receipt rendered as a PDF (CDP printToPDF) plus
// the fields it extracted from the page. We store both; the `completed`
// terminal itself happens in job-completed, which carries the receipt URL into
// applyTerminal so "money moved + receipt captured" lands atomically.

import { uploadBase64 } from "../lib/gcs";
import { config, gcsPrefixForTask } from "../config";
import { patchAiAgentData, isoIST, ts, patchRoot } from "./requestDoc";

export interface SaveReceiptInput {
    requestId: string;
    driverId: string;
    pdfBase64?: string;
    imageBase64?: string; // PNG fallback if printToPDF ever fails
    fields?: Record<string, unknown>; // receiptNumber, amount, paymentDate, bankRef
    task?: string;
}

export async function handleSaveReceipt(input: SaveReceiptInput) {
    const { requestId, driverId } = input;
    if (!requestId || !driverId) {
        return { ok: false, error: "requestId and driverId required" };
    }
    const payload = input.pdfBase64 ?? input.imageBase64;
    if (!payload) {
        return { ok: false, error: "pdfBase64 or imageBase64 required" };
    }
    const isPdf = !!input.pdfBase64;
    if (isPdf && payload.length < 1400) {
        // ~1 KB decoded — a real receipt PDF is never this small.
        return { ok: false, error: "receipt PDF is suspiciously small" };
    }

    const up = await uploadBase64({
        base64: payload,
        destination: `${requestId}_${driverId}/receipt.${isPdf ? "pdf" : "png"}`,
        contentType: isPdf ? "application/pdf" : "image/png",
        signedUrlTtlSeconds: config.qrValidityDays * 24 * 60 * 60,
        prefix: gcsPrefixForTask(input.task),
    });

    await patchAiAgentData(requestId, driverId, {
        receipt: {
            url: up.url,
            fields: input.fields ?? {},
            uploadedAt: ts(),
        },
        receiptGenerated: true,
        receiptGeneratedAt: ts(),
    }, input.task);

    // Mirror the URL at the document ROOT too. Newer app builds read
    // aiAgentData.receipt.url; older ones read receiptDocumentUrl from the root.
    // Writing both keeps every client version working — and because this is not
    // FSM-guarded, the URL lands even if the lifecycle terminal is delayed or
    // blocked (e.g. a re-run whose doc is still in a terminal state).
    await patchRoot(requestId, driverId, { receiptDocumentUrl: up.url, status: "completed" }, input.task);

    return { ok: true, url: up.url };
}
