// Challan-payment Firestore writes. ONE CHALLAN PER JOB, one top-level doc:
//
//   subChallanRequests/{CHALLAN}
//       aiAgentStatus: {
//           status:    "queued" | "running" | "waitingForHuman"
//                    | "generatingReceipt" | "completed" | "failed" | "cancelled",
//           paid:      boolean,      // true once a receipt is stored
//           error:     boolean,      // true iff reason is non-empty
//           reason:    string,       // "" unless error
//           attempt:   number,       // +1 per /api/run for this challan
//           createdAt: Timestamp,    // first request, written once
//           updatedAt: Timestamp,
//       }
//       receipt:   { url: string, at: Timestamp }   // root, only once paid
//       subStatus: "completed"                      // root, only once paid
//
// The doc id IS the challan number, so nothing here needs a vehicle number to
// address a write — that is why the reporter no longer carries one. The
// vehicle is still passed to the receipt upload, but only to shape the GCS
// path.
//
// receipt + subStatus + aiAgentStatus.paid + status:"completed" are written in
// ONE merge, so a client can never observe a completed challan with no receipt,
// or a receipt with no completion.
//
// On anything short of success, `subStatus` and `receipt` are left untouched —
// a failed run must not stamp a root field the client reads as "done", and must
// not erase proof of a payment that did go through.
//
// Concurrency: five challans on one vehicle = five jobs writing five DIFFERENT
// docs, so they never contend. The real hazard is a late write from a worker
// that has already been reaped, which is why every write runs in a transaction
// behind the FSM below.

import { FieldValue, Timestamp, db, bucket } from "../firebase";

const SUBCHALLAN_REQUESTS_COLLECTION =
    process.env.SUBCHALLAN_REQUESTS_COLLECTION ?? "subChallanRequests";
const RECEIPT_PREFIX =
    process.env.CHALLAN_PAYMENT_RECEIPT_PREFIX ??
    "driverUtilitiesRequests/challanPayments";

const normalizeId = (s: unknown): string =>
    String(s ?? "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();

/** subChallanRequests/{CHALLAN}. The challan id is used VERBATIM — it is the id
 *  the client wrote, and normalizing it here would create a second, orphaned
 *  doc. */
export function subChallanRequestDoc(challanNo: string) {
    return db
        .collection(SUBCHALLAN_REQUESTS_COLLECTION)
        .doc(String(challanNo).trim());
}

// ── the payment FSM ──────────────────────────────────────────────────────
// Same contract as lifecycle/statuses.ts, in the vocabulary a challan row
// actually needs: is anyone waiting on me, and did it work. The worker speaks
// the shared border-tax lifecycle; that is mapped in, never leaked out.

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
    queued: ["running"],
    running: ["waitingForHuman", "generatingReceipt"],
    waitingForHuman: ["generatingReceipt"],
    generatingReceipt: [],
    completed: [],
    failed: [],
    cancelled: [],
};

/** Self-transitions are idempotent re-emits and always legal. Any non-terminal
 *  may reach any terminal: the FSM exists to stop a reaped worker resurrecting
 *  a finished challan and to catch nonsense forward moves — never to veto an
 *  ending, which is the one failure mode that strands a challan mid-flight. */
export function canTransition(from: PaymentStatus, to: PaymentStatus): boolean {
    if (TERMINAL.has(from)) return false;
    if (from === to) return true;
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
    challanNo: string; // addresses the doc; NOT written into it
    status: PaymentStatus;
    reason?: string; // non-empty => aiAgentStatus.error = true
    paid?: boolean;
    /** Set only by the receipt upload. Writes root receipt + subStatus too. */
    receiptUrl?: string;
    /** Skip the FSM guard. Two callers only: the enqueue path re-opening a
     *  challan that ended failed/cancelled (a retry), and the receipt upload,
     *  which is proof money moved and therefore always the last word. */
    force?: boolean;
}

export async function writeChallanPaymentStatus(
    input: WriteStatusInput,
): Promise<{ ok: boolean; status?: PaymentStatus; error?: string }> {
    const challan = String(input.challanNo ?? "").trim();
    if (!challan) return { ok: false, error: "challanNo required" };

    const ref = subChallanRequestDoc(challan);
    const now = Timestamp.now();
    const to = input.status;

    try {
        const written = await db.runTransaction(async (tx) => {
            const snap = await tx.get(ref);
            const cur = (snap.get("aiAgentStatus.status") ?? "queued") as PaymentStatus;
            const first = !snap.get("aiAgentStatus.createdAt");

            if (!input.force && !canTransition(cur, to)) return null;

            const reason = input.reason ?? "";
            const agent: Record<string, unknown> = {
                status: to,
                error: !!reason,
                reason,
                updatedAt: now,
            };
            if (input.paid !== undefined) agent.paid = input.paid;
            if (to === "queued") {
                agent.paid = false;
                agent.attempt = FieldValue.increment(1);
            }
            if (TERMINAL.has(to) && to !== "completed" && input.paid === undefined) {
                agent.paid = false;
            }
            if (first) {
                agent.createdAt = now;
                if (agent.attempt === undefined) agent.attempt = 1;
            }

            const docPatch: Record<string, unknown> = { aiAgentStatus: agent };

            // Root fields are written ONLY on a real receipt. A failed or
            // cancelled run leaves subStatus and receipt exactly as they were:
            // never stamp "completed" without proof, never erase proof that a
            // payment went through.
            if (input.receiptUrl) {
                agent.paid = true;
                docPatch.receipt = { url: input.receiptUrl, at: now };
                docPatch.subStatus = "completed";
            }

            tx.set(ref, docPatch, { merge: true });
            return to;
        });

        if (written === null) {
            console.log(
                `[challan-payment] ${challan}: refused "${to}" ` +
                "(illegal transition or already terminal)",
            );
            return { ok: true, status: undefined };
        }
        console.log(
            `[challan-payment] ${challan} -> ${written}` +
            (input.reason ? ` (${input.reason})` : ""),
        );
        return { ok: true, status: written };
    } catch (e: any) {
        console.error(`[challan-payment] status write failed for ${challan}: ${e.message}`);
        return { ok: false, error: e.message };
    }
}

/**
 * True when this challan already has a stored receipt. THE guard against
 * double payment: a client retry, a duplicate tap, or a stale queue entry must
 * never drive a second payment for money that already left. Read-only.
 */
export async function isChallanAlreadyPaid(
    challanNo: string,
): Promise<{ paid: boolean; receiptUrl?: string }> {
    try {
        const snap = await subChallanRequestDoc(challanNo).get();
        const receiptUrl = snap.get("receipt.url");
        if (receiptUrl || snap.get("aiAgentStatus.paid") === true) {
            return { paid: true, receiptUrl: receiptUrl ?? undefined };
        }
        return { paid: false };
    } catch (e: any) {
        // Fail OPEN on a read error: blocking every payment because Firestore
        // hiccuped is worse than the (already transactional) duplicate risk.
        console.error(`[challan-payment] paid-check failed for ${challanNo}: ${e.message}`);
        return { paid: false };
    }
}

// ── worker-facing: /api/internal/status-update with task=challan-payment ──

export interface ReportStatusInput {
    requestId?: string; // == the challan number == the doc id
    status: string; // raw lifecycle status
    error?: string | null;
}

export async function reportChallanPaymentStatus(input: ReportStatusInput) {
    const challanNo = String(input.requestId ?? "").trim();
    if (!challanNo) {
        // Never 500 the worker's status path over a missing id.
        console.error("[challan-payment] status-update with no requestId");
        return { ok: false, error: "requestId (challanNo) required" };
    }
    return await writeChallanPaymentStatus({
        challanNo,
        status: toPaymentStatus(input.status),
        reason: input.error ?? "",
    });
}

// ── worker-facing: receipt upload ────────────────────────────────────────

export interface SaveReceiptInput {
    jobId?: string;
    requestId?: string;
    vehicleNumber?: string; // GCS path only — not needed to address the doc
    challanNo?: string;
    department?: string;
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
    const challan = String(input.challanNo ?? input.requestId ?? "").trim();
    if (!challan) return { ok: false, error: "challanNo required" };
    if (!input.pdfBase64) return { ok: false, error: "pdfBase64 required" };

    const buffer = Buffer.from(input.pdfBase64, "base64");
    if (buffer.length < 1000) {
        // ~1 KB — a real receipt PDF is never this small; almost always a blank
        // page captured a beat too early.
        return { ok: false, error: `receipt PDF too small (${buffer.length} bytes)` };
    }

    const veh = normalizeId(input.vehicleNumber) || "unknown";
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
                    challanNo: challan,
                    vehicleNumber: veh,
                    ...(input.department ? { department: input.department } : {}),
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

    const wrote = await writeChallanPaymentStatus({
        challanNo: challan,
        status: "completed",
        paid: true,
        receiptUrl: url, // also writes root receipt{} + subStatus
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
    challanNo: string;
    outcome: string; // done | cancelled | failed | partial
    summary?: string;
    error?: string | null;
}

export async function finishChallanPayment(input: FinishInput) {
    const status = TERMINAL_FROM_OUTCOME[input.outcome] ?? "failed";
    // `completed` is really written by the receipt upload, which is the only
    // thing that can prove payment. A "done" arriving here re-asserts it, but
    // deliberately does NOT touch root receipt/subStatus.
    return await writeChallanPaymentStatus({
        challanNo: input.challanNo,
        status,
        reason:
            status === "completed"
                ? ""
                : input.error || input.summary || `run ended ${input.outcome}`,
        force: status === "completed",
    });
}
