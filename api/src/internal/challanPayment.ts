// Challan-payment Firestore writes. ONE CHALLAN PER JOB, and this module owns
// exactly ONE field on exactly one doc:
//
//   challans/{VEH}/subChallans/{CHALLAN}
//       aiAgentPaymentStatus: {
//           status:     "queued" | "running" | "waitingForHuman"
//                     | "generatingReceipt" | "completed" | "failed" | "cancelled",
//           paid:       boolean,                       // true once a receipt is stored
//           receiptUrl: string | null,                 // the deliverable
//           error:      { isError: boolean, reason: string },
//           attempt:    number,                        // +1 per /api/run
//           createdAt:  Timestamp,                     // first request, written once
//           updatedAt:  Timestamp,
//       }
//
// Deliberately free of identity fields: the doc PATH already carries them
// (ref.id is the challan number, ref.parent.parent.id is the vehicle), so
// duplicating them inside the object would only create a second source of
// truth that can drift. A collectionGroup("subChallans") query still gives an
// ops view of every challan on every vehicle without a join.
//
// Nothing else on the doc is touched: `quotation` / `challanAmount` belong to
// challan-settlement, and the rest belongs to the client. There is deliberately
// NO aiAgentData — that shape is the border-tax request-doc lifecycle and does
// not fit a per-challan subdocument.
//
// Concurrency: five challans on one vehicle = five jobs writing five DIFFERENT
// subChallan docs, so they never contend. The real hazard is a late write from
// a worker that has already been reaped, which is why every write runs in a
// transaction behind the FSM below.

import { FieldValue, Timestamp, db, bucket } from "../firebase";

const CHALLANS_COLLECTION = process.env.CHALLANS_COLLECTION ?? "challans";
const SUBCHALLANS_COLLECTION = process.env.SUBCHALLANS_COLLECTION ?? "subChallans";
const RECEIPT_PREFIX =
    process.env.CHALLAN_PAYMENT_RECEIPT_PREFIX ??
    "driverUtilitiesRequests/challanPayments";

const normalizeId = (s: unknown): string =>
    String(s ?? "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();

/** challans/{VEH}/subChallans/{CHALLAN}. The challan id is used VERBATIM — it
 *  is the id the client wrote, and normalizing it here would create a second,
 *  orphaned doc. Only the vehicle (the parent key) is normalized. */
export function subChallanDoc(vehicleNumber: string, challanNo: string) {
    return db
        .collection(CHALLANS_COLLECTION)
        .doc(normalizeId(vehicleNumber))
        .collection(SUBCHALLANS_COLLECTION)
        .doc(String(challanNo).trim());
}

// ── the payment FSM ──────────────────────────────────────────────────────
// Same contract as lifecycle/statuses.ts, in the vocabulary a challan row
// actually needs: is anyone waiting on me, and did it work. The worker speaks
// the shared border-tax lifecycle; that is mapped in, never leaked out.
//
// Money line: `queued` and `running` are pre-payment, so a stop there is
// `cancelled` (retryable). From `waitingForHuman` on, the operator is holding a
// live payment page — a stop is `failed`, never a silent cancel.

export type PaymentStatus =
    | "queued"
    | "running"
    | "waitingForHuman"
    | "generatingReceipt"
    | "completed"
    | "failed"
    | "cancelled";

const TERMINAL: ReadonlySet<PaymentStatus> = new Set<PaymentStatus>([
    "completed",
    "failed",
    "cancelled",
]);

const ALLOWED: Record<PaymentStatus, PaymentStatus[]> = {
    queued: ["running", "cancelled", "failed"],
    running: [
        "waitingForHuman",
        "generatingReceipt", // fast path: receipt already up when we look
        "completed",
        "cancelled",
        "failed",
    ],
    waitingForHuman: ["generatingReceipt", "completed", "failed"],
    generatingReceipt: ["completed", "failed"],
    completed: [],
    failed: [],
    cancelled: [],
};

/** Self-transitions are idempotent re-emits and always legal. Anything out of
 *  a terminal is rejected — that is what stops a reaped worker resurrecting a
 *  finished challan. */
export function canTransition(from: PaymentStatus, to: PaymentStatus): boolean {
    if (TERMINAL.has(from)) return false;
    if (from === to) return true;
    // ANY non-terminal state may go to ANY terminal state. The FSM exists to
    // stop a reaped worker resurrecting a finished challan and to catch
    // nonsense forward moves — not to veto an ending. Refusing a terminal is
    // the one failure mode that strands a challan mid-flight (a cancel during
    // handover left a doc reading "waitingForHuman" forever), so endings are
    // always accepted and the reason field carries the detail.
    if (TERMINAL.has(to)) return true;
    return (ALLOWED[from] ?? []).includes(to);
}

const FROM_LIFECYCLE: Record<string, PaymentStatus> = {
    queued: "queued",
    aiAgentStarted: "running",
    captchaSolving: "running",
    settingUpPaymentRequest: "running",
    humanHandover: "waitingForHuman",
    generatingReceipt: "generatingReceipt",
    verifyingPayment: "generatingReceipt",
    completed: "completed",
    cancelled: "cancelled",
    failed: "failed",
};

export function toPaymentStatus(lifecycle: string): PaymentStatus {
    return FROM_LIFECYCLE[lifecycle] ?? "running";
}

// ── the single writer ────────────────────────────────────────────────────

export interface WriteStatusInput {
    vehicleNumber: string; // addresses the doc; NOT written into it
    challanNo: string; // addresses the doc; NOT written into it
    status: PaymentStatus;
    reason?: string; // non-empty => error.isError = true
    paid?: boolean;
    receiptUrl?: string;
    /** Skip the FSM guard. Two callers only: the enqueue path re-opening a
     *  challan that ended failed/cancelled (a retry), and the receipt upload,
     *  which is proof money moved and therefore always the last word. */
    force?: boolean;
}

export async function writeChallanPaymentStatus(
    input: WriteStatusInput,
): Promise<{ ok: boolean; status?: PaymentStatus; error?: string }> {
    const veh = normalizeId(input.vehicleNumber);
    const challan = String(input.challanNo ?? "").trim();
    if (!veh || !challan) {
        return { ok: false, error: "vehicleNumber and challanNo required" };
    }

    const ref = subChallanDoc(veh, challan);
    const now = Timestamp.now();
    const to = input.status;

    try {
        const written = await db.runTransaction(async (tx) => {
            const snap = await tx.get(ref);
            const cur = (snap.get("aiAgentPaymentStatus.status") ??
                "queued") as PaymentStatus;
            const first = !snap.get("aiAgentPaymentStatus.createdAt");

            if (!input.force && !canTransition(cur, to)) {
                return null;
            }

            const reason = input.reason ?? "";
            const patch: Record<string, unknown> = {
                status: to,
                error: { isError: !!reason, reason },
                updatedAt: now,
            };

            if (input.paid !== undefined) patch.paid = input.paid;
            if (input.receiptUrl) {
                patch.receiptUrl = input.receiptUrl;
                patch.paid = true;
            }
            if (to === "queued") {
                // A fresh attempt: clear the previous run's outcome so a retry
                // never shows a stale receipt next to a live "queued".
                patch.paid = false;
                patch.receiptUrl = null;
                patch.attempt = FieldValue.increment(1);
            }
            if (TERMINAL.has(to) && to !== "completed" && input.paid === undefined) {
                patch.paid = false;
            }
            if (first) {
                patch.createdAt = now;
                if (patch.attempt === undefined) patch.attempt = 1;
            }

            tx.set(ref, { aiAgentPaymentStatus: patch }, { merge: true });
            return to;
        });

        if (written === null) {
            console.log(
                `[challan-payment] ${veh}/${challan}: refused "${to}" ` +
                "(illegal transition or already terminal)",
            );
            return { ok: true, status: undefined };
        }
        console.log(
            `[challan-payment] ${veh}/${challan} -> ${written}` +
            (input.reason ? ` (${input.reason})` : ""),
        );
        return { ok: true, status: written };
    } catch (e: any) {
        console.error(
            `[challan-payment] status write failed for ${veh}/${challan}: ${e.message}`,
        );
        return { ok: false, error: e.message };
    }
}

/**
 * True when this challan already has a stored receipt. THE guard against
 * double payment: a client retry, a duplicate tap, or a stale queue entry must
 * never drive a second payment for money that already left. Read-only.
 */
export async function isChallanAlreadyPaid(
    vehicleNumber: string,
    challanNo: string,
): Promise<{ paid: boolean; receiptUrl?: string }> {
    try {
        const snap = await subChallanDoc(vehicleNumber, challanNo).get();
        const st = snap.get("aiAgentPaymentStatus");
        if (st?.paid === true || st?.receiptUrl) {
            return { paid: true, receiptUrl: st?.receiptUrl ?? undefined };
        }
        return { paid: false };
    } catch (e: any) {
        // Fail OPEN on a read error: blocking every payment because Firestore
        // hiccuped is worse than the (already transactional) duplicate risk.
        console.error(
            `[challan-payment] paid-check failed for ${vehicleNumber}/${challanNo}: ${e.message}`,
        );
        return { paid: false };
    }
}

// ── worker-facing: /api/internal/status-update with task=challan-payment ──

export interface ReportStatusInput {
    requestId?: string;
    status: string; // raw lifecycle status
    error?: string | null;
    extra?: Record<string, unknown>; // carries vehicleNumber + challanNo
}

export async function reportChallanPaymentStatus(input: ReportStatusInput) {
    const extra = input.extra ?? {};
    const vehicleNumber = String(extra.vehicleNumber ?? "");
    const challanNo = String(extra.challanNo ?? input.requestId ?? "");
    if (!vehicleNumber || !challanNo) {
        // Never 500 the worker's status path over a missing label.
        console.error(
            "[challan-payment] status-update without vehicleNumber/challanNo " +
            `(requestId=${input.requestId})`,
        );
        return { ok: false, error: "vehicleNumber and challanNo required in extra" };
    }
    return await writeChallanPaymentStatus({
        vehicleNumber,
        challanNo,
        status: toPaymentStatus(input.status),
        reason: input.error ?? "",
    });
}

// ── worker-facing: receipt upload ────────────────────────────────────────

export interface SaveReceiptInput {
    jobId?: string;
    requestId?: string;
    vehicleNumber?: string;
    challanNo?: string;
    pdfBase64?: string;
}

/**
 * Store the receipt PDF and close the challan out as paid, in that order: if
 * the upload fails there is nothing to record, and the worker downgrades the
 * run to `partial` so a human reconciles.
 *
 * The URL is a PERMANENT Firebase download-token URL, deliberately NOT the
 * signed URL lib/gcs.ts mints for border-tax QR/captcha artifacts. A signed URL
 * expires (7 days by default) and a paid-challan receipt is a record the driver
 * keeps.
 */
export async function handleSaveChallanPaymentReceipt(input: SaveReceiptInput) {
    const veh = normalizeId(input.vehicleNumber);
    const challan = String(input.challanNo ?? input.requestId ?? "").trim();
    if (!veh || !challan) {
        return { ok: false, error: "vehicleNumber and challanNo required" };
    }
    if (!input.pdfBase64) return { ok: false, error: "pdfBase64 required" };

    const buffer = Buffer.from(input.pdfBase64, "base64");
    if (buffer.length < 1000) {
        // ~1 KB — a real receipt PDF is never this small; almost always a blank
        // page captured a beat too early.
        return { ok: false, error: `receipt PDF too small (${buffer.length} bytes)` };
    }

    let url: string;
    try {
        const safe = challan.replace(/[^A-Za-z0-9._-]/g, "_");
        const destination = `${RECEIPT_PREFIX}/${veh}/${safe}_${Date.now()}_receipt.pdf`;
        const downloadToken = crypto.randomUUID();
        const file = bucket.file(destination);
        await file.save(buffer, {
            metadata: {
                contentType: "application/pdf",
                metadata: {
                    firebaseStorageDownloadTokens: downloadToken,
                    vehicleNumber: veh,
                    challanNo: challan,
                    ...(input.jobId ? { jobId: input.jobId } : {}),
                },
            },
        });
        url =
            `https://firebasestorage.googleapis.com/v0/b/${bucket.name}/o/` +
            `${encodeURIComponent(destination)}?alt=media&token=${downloadToken}`;
        console.log(
            `[challan-payment] receipt uploaded ${destination} (${buffer.length} bytes)`,
        );
    } catch (e: any) {
        console.error(`[challan-payment] receipt upload failed: ${e.message}`);
        return { ok: false, error: `receipt upload failed: ${e.message}` };
    }

    // receiptUrl + paid + completed land in ONE merge, so a client can never
    // observe "completed with no receipt" or "receipt with no completion".
    const wrote = await writeChallanPaymentStatus({
        vehicleNumber: veh,
        challanNo: challan,
        status: "completed",
        paid: true,
        receiptUrl: url,
        force: true, // proof money moved — always the last word
    });
    if (!wrote.ok) {
        // The PDF IS stored — hand the URL back so the run can report partial
        // with something a human can act on, instead of losing it.
        return { ok: false, error: `receipt saved but not attached: ${wrote.error}`, url };
    }

    return { ok: true, url };
}

// ── terminal (job-completed / cancel) ────────────────────────────────────

const TERMINAL_FROM_OUTCOME: Record<string, PaymentStatus> = {
    done: "completed",
    cancelled: "cancelled",
    failed: "failed",
    partial: "failed", // payment likely moved but a later step broke — reconcile
};

export interface FinishInput {
    vehicleNumber: string;
    challanNo: string;
    outcome: string; // done | cancelled | failed | partial
    summary?: string;
    error?: string | null;
    receiptUrl?: string | null;
}

export async function finishChallanPayment(input: FinishInput) {
    const status = TERMINAL_FROM_OUTCOME[input.outcome] ?? "failed";
    // `completed` is really written by the receipt upload, which is the only
    // thing that can prove payment. A "done" arriving here re-asserts it.
    return await writeChallanPaymentStatus({
        vehicleNumber: input.vehicleNumber,
        challanNo: input.challanNo,
        status,
        reason:
            status === "completed"
                ? ""
                : input.error || input.summary || `run ended ${input.outcome}`,
        ...(input.receiptUrl ? { receiptUrl: input.receiptUrl } : {}),
        force: status === "completed",
    });
}
