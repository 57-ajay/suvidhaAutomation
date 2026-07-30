// POST /api/run with taskId "challan-payment" delegates here.
//
// Body: { taskId: "challan-payment", source: "app" | "web",
//         params: { vehicleNumber, challanNo, phoneNo, chassisNo,
//                   driverId?, department?, requestId? } }
//
// ONE CHALLAN PER REQUEST. A vehicle with five open challans produces five
// calls, five jobs and five independent outcomes — this route never fans out.
//
//   1. Validate + normalize. The challan must map to a Virtual Courts
//      department (or carry an explicit `department`), and it must come with
//      the mobile number and chassis the portal's verification step demands —
//      all three are checked HERE so a request that provably cannot finish is
//      rejected in milliseconds instead of burning a browser slot to fail.
//   2. requestId == challanNo (the subChallans doc id);
//      jobId == "cp-" + normalized challan, so one challan has at most one
//      active payment job and it never collides with border-tax / PUC /
//      challan-settlement job ids.
//   3. Seed aiAgentStatus.status = "queued" on subChallanRequests/{challanNo}
//      so the client sees the challan move the moment the request is accepted.
//   4. Enqueue. If THAT fails, the seed is rolled back to "failed" — a challan
//      must never sit at "queued" with no job behind it.

import { redis, keys, JOB_TTL } from "../redis";
import { json } from "../lib/http";
import { planDepartments } from "../internal/challanSettlement";
import {
    isChallanAlreadyPaid,
    writeChallanPaymentStatus,
} from "../internal/challanPayment";

const ACTIVE = new Set(["queued", "running", "waiting_for_human"]);

const normalizeId = (s: unknown): string =>
    String(s ?? "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();

const str = (v: unknown): string =>
    typeof v === "string" ? v.trim() : typeof v === "number" ? String(v) : "";

export async function handleChallanPaymentRun(
    rawParams: Record<string, unknown>,
    source: string,
): Promise<Response> {
    // ── 1. validate ──────────────────────────────────────────────────────
    const vehicleNumber = normalizeId(rawParams.vehicleNumber ?? rawParams.regNo);
    const challanNo = str(rawParams.challanNo ?? rawParams.challanNumber);
    const phoneNo = str(rawParams.phoneNo ?? rawParams.mobileNumber).replace(/\D/g, "");
    const chassisNo = str(rawParams.chassisNo ?? rawParams.chassisNumber);

    const missing: string[] = [];
    const invalid: string[] = [];

    if (!vehicleNumber) missing.push("vehicleNumber");
    else if (vehicleNumber.length < 4 || vehicleNumber.length > 12)
        invalid.push("vehicleNumber");

    if (!challanNo) missing.push("challanNo");
    else if (normalizeId(challanNo).length < 5 || normalizeId(challanNo).length > 35)
        invalid.push("challanNo");

    // Both are hard requirements of the portal's verification step: it will not
    // send an OTP without the mobile number, and will not accept the OTP
    // without the last 4 of the chassis. Optional in the type, mandatory here.
    if (!phoneNo) missing.push("phoneNo");
    else if (phoneNo.length < 10) invalid.push("phoneNo");
    if (!chassisNo) missing.push("chassisNo");
    else if (chassisNo.length < 4) invalid.push("chassisNo");

    // requestId is the subChallans doc id and MUST be the challan number. If a
    // client sends one anyway, make sure it agrees rather than silently writing
    // the receipt onto a different doc.
    const sentRequestId = str(rawParams.requestId);
    if (sentRequestId && normalizeId(sentRequestId) !== normalizeId(challanNo)) {
        invalid.push("requestId");
    }

    if (missing.length || invalid.length) {
        return json(
            {
                ok: false,
                error: "validation_failed",
                missing,
                invalid,
                message:
                    "Cannot start the challan payment — " +
                    (missing.length ? `missing: ${missing.join(", ")}. ` : "") +
                    (invalid.length ? `invalid: ${invalid.join(", ")}.` : ""),
            },
            400,
        );
    }

    // Department: explicit override wins; otherwise derive from the prefix with
    // the same table challan-settlement uses.
    const department = str(rawParams.department);
    if (!department) {
        const { departments, unmapped } = planDepartments([challanNo]);
        if (!departments.length) {
            return json(
                {
                    ok: false,
                    error: "no_department",
                    unmapped,
                    message:
                        `Challan ${challanNo} does not map to a Virtual Courts ` +
                        "department. Pass an explicit `department` to override.",
                },
                400,
            );
        }
    }

    const driverId = str(rawParams.driverId) || "na";
    const requestId = challanNo; // == the subChallans doc id
    const jobId = `cp-${normalizeId(challanNo)}`;

    // ── 2. dedupe on an active job for THIS challan ──────────────────────
    const existing = await redis.hget(keys.job(jobId), "status");
    if (existing && ACTIVE.has(existing)) {
        return json({ ok: true, jobId, deduped: true, status: existing });
    }

    // Already paid -> refuse, loudly. Redis dedupe only covers a job that is
    // still active (and only for 24h); this covers the case that actually
    // costs money — a retry, a double tap, or a re-queue of a challan whose
    // receipt is already stored.
    const paid = await isChallanAlreadyPaid(challanNo);
    if (paid.paid) {
        return json(
            {
                ok: false,
                error: "already_paid",
                challanNo,
                vehicleNumber,
                receiptUrl: paid.receiptUrl,
                message:
                    `Challan ${challanNo} for ${vehicleNumber} is already marked ` +
                    "paid with a stored receipt — refusing to pay it twice.",
            },
            409,
        );
    }

    const params = {
        requestId,
        vehicleNumber,
        challanNo,
        phoneNo,
        chassisNo,
        ...(department ? { department } : {}),
        ...(str(rawParams.engineNo) ? { engineNo: str(rawParams.engineNo) } : {}),
        driverId,
        source,
    };

    // ── 3. seed the challan's status so the client sees it immediately ───
    const seed = await writeChallanPaymentStatus({
        challanNo,
        status: "queued",
        force: true, // a retry re-opens a challan that ended failed/cancelled
    });
    if (!seed.ok) {
        return json(
            { ok: false, error: `could not mark the challan queued: ${seed.error}` },
            500,
        );
    }

    // ── 4. enqueue (roll the seed back if THIS fails) ────────────────────
    try {
        const now = new Date();
        await redis
            .multi()
            .del(keys.job(jobId))
            .hset(keys.job(jobId), {
                id: jobId,
                taskId: "challan-payment", // worker dispatches the payment flow off this
                params: JSON.stringify(params),
                status: "queued",
                source,
                requestId,
                driverId,
                vehicleNumber,
                challanNo, // ops console label + job-completed lookup
                createdAt: now.toISOString(),
            })
            .expire(keys.job(jobId), JOB_TTL)
            .zadd(keys.allJobs, now.getTime(), jobId)
            .lpush(keys.queue, jobId)
            .exec();
    } catch (e: any) {
        console.error(`[run] challan-payment enqueue failed: ${e.message}`);
        await writeChallanPaymentStatus({
            challanNo,
            status: "failed",
            reason: `enqueue failed: ${e.message}`,
            force: true,
        }).catch(() => { });
        return json(
            { ok: false, error: `could not enqueue the job: ${e.message}` },
            500,
        );
    }

    return json({
        ok: true,
        jobId,
        requestId,
        status: "queued",
        task: "challan-payment",
        vehicleNumber,
        challanNo,
    });
}
