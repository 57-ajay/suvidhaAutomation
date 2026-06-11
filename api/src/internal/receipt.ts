// api/src/internal/receipt.ts
//
// Worker calls this when it has captured the final receipt (PNG screenshot or
// PDF). We upload it and stash the URL + extracted fields. The actual terminal
// `completed` transition happens in job-completed, which passes the receipt URL
// through to applyTerminal — keeping "money moved + receipt captured" atomic.

import { uploadImage } from "../lib/gcs";
import { config } from "../config";
import { patchAiAgentData } from "./requestDoc";

export interface SaveReceiptInput {
    requestId: string;
    driverId: string;
    imageBase64?: string;
    pdfBase64?: string;
    fields?: Record<string, unknown>; // receiptNumber, amount, paymentDate, bankRef...
}

export async function handleSaveReceipt(input: SaveReceiptInput) {
    const { requestId, driverId } = input;
    if (!requestId || !driverId) {
        return { ok: false, error: "requestId and driverId required" };
    }
    const isPdf = !!input.pdfBase64;
    const dest = `${requestId}_${driverId}/receipt.${isPdf ? "pdf" : "png"}`;
    const up = await uploadImage({
        base64: (input.pdfBase64 ?? input.imageBase64)!,
        destination: dest,
        contentType: isPdf ? "application/pdf" : "image/png",
        signedUrlTtlSeconds: config.qrValidityDays * 24 * 60 * 60,
    });

    await patchAiAgentData(requestId, driverId, {
        receipt: {
            url: up.url,
            fields: input.fields ?? {},
        },
    });

    return { ok: true, url: up.url };
}
