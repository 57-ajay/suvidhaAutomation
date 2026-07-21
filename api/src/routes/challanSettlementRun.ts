// POST /api/run with taskId "challan-settlement" delegates here (see the
// three-line patch to routes/run.ts in PATCHES.md).
//
// Body: { taskId: "challan-settlement", source: "app" | "web",
//         params: { vehicleNumber, challanNumbers: string[], driverId? } }
//
//   1. Validate + normalize. challanNumbers must map to >=1 Virtual Courts
//      department or the request is rejected with the unmapped list.
//   2. requestId == normalized vehicle number (the challans doc key);
//      jobId == "chs-" + vehicle number, so a vehicle has at most one active
//      settlement scan and never collides with border-tax/PUC job ids.
//   3. Set aiCheckingSettlement=true on challans/{VEH} up front so the client
//      sees "checking" the moment the request is accepted.
//   4. Enqueue + seed aiAgentData.status = queued on the same doc (task
//      routing: "challan-settlement" -> challans/{requestId}).
//
// Flag invariant: aiCheckingSettlement must NEVER be left true without a live
// job behind it — client-side triggers key off it. So if the enqueue itself
// fails after step 3, the flag is rolled back to false before the 500 goes
// out. (Every terminal path also clears it — see applyTerminal.)

import { redis, keys, JOB_TTL } from "../redis";
import { json } from "../lib/http";
import { setAgentStatus } from "../internal/requestDoc";
import { STATUS } from "../lifecycle/statuses";
import {
    planDepartments,
    setChallanCheckingFlag,
} from "../internal/challanSettlement";

const ACTIVE = new Set(["queued", "running", "waiting_for_human"]);

const normalizeId = (s: unknown): string =>
    String(s ?? "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();

export async function handleChallanSettlementRun(
    rawParams: Record<string, unknown>,
    source: string,
): Promise<Response> {
    // ── 1. validate ──────────────────────────────────────────────────────
    const vehicleNumber = normalizeId(rawParams.vehicleNumber ?? rawParams.regNo);
    const rawChallans = rawParams.challanNumbers ?? rawParams.challans;

    const missing: string[] = [];
    const invalid: string[] = [];
    if (!vehicleNumber) missing.push("vehicleNumber");
    else if (vehicleNumber.length < 4 || vehicleNumber.length > 12)
        invalid.push("vehicleNumber");

    let challanNumbers: string[] = [];
    if (!Array.isArray(rawChallans) || rawChallans.length === 0) {
        missing.push("challanNumbers");
    } else {
        const seen = new Set<string>();
        for (const c of rawChallans) {
            const original = String(c).trim();
            const norm = normalizeId(original);
            if (norm.length < 5 || norm.length > 35) {
                invalid.push(`challanNumbers[${original}]`);
                continue;
            }
            if (seen.has(norm)) continue; // dedupe silently
            seen.add(norm);
            challanNumbers.push(original);
        }
        if (!challanNumbers.length && !invalid.length) missing.push("challanNumbers");
    }

    if (missing.length || invalid.length) {
        return json(
            {
                ok: false,
                error: "validation_failed",
                missing,
                invalid,
                message:
                    `Cannot start challan settlement check — ` +
                    (missing.length ? `missing: ${missing.join(", ")}. ` : "") +
                    (invalid.length ? `invalid: ${invalid.join(", ")}.` : ""),
            },
            400,
        );
    }

    const { departments, unmapped } = planDepartments(challanNumbers);
    if (!departments.length) {
        return json(
            {
                ok: false,
                error: "no_departments",
                unmapped,
                message:
                    "None of the challan numbers map to a Virtual Courts " +
                    "department, so there is nothing to query.",
            },
            400,
        );
    }

    const driverId = String(rawParams.driverId ?? "").trim() || "na";
    const requestId = vehicleNumber; // challans/{requestId} == challans/{VEH}
    const jobId = `chs-${vehicleNumber}`;

    // ── 2. dedupe on an active job for this vehicle ──────────────────────
    const existing = await redis.hget(keys.job(jobId), "status");
    if (existing && ACTIVE.has(existing)) {
        return json({ ok: true, jobId, deduped: true, status: existing });
    }

    const params = {
        vehicleNumber,
        challanNumbers,
        requestId,
        driverId,
        source,
    };

    // ── 3. flag first, so the client sees "checking" immediately ─────────
    const flag = await setChallanCheckingFlag({
        vehicleNumber,
        checking: true,
        requestedChallans: challanNumbers,
    });
    if (!flag.ok) {
        return json(
            { ok: false, error: `could not mark aiCheckingSettlement: ${flag.error}` },
            500,
        );
    }

    // ── 4. enqueue (roll the flag back if THIS fails — never leave the
    //       client stuck at "checking" with no job behind it) ─────────────
    try {
        const now = new Date();
        await redis
            .multi()
            .del(keys.job(jobId))
            .hset(keys.job(jobId), {
                id: jobId,
                taskId: "challan-settlement", // worker dispatches the settlement flow off this
                params: JSON.stringify(params),
                status: "queued",
                source,
                requestId,
                driverId,
                vehicleNumber,
                createdAt: now.toISOString(),
            })
            .expire(keys.job(jobId), JOB_TTL)
            .zadd(keys.allJobs, now.getTime(), jobId)
            .lpush(keys.queue, jobId)
            .exec();
    } catch (e: any) {
        console.error(`[run] challan-settlement enqueue failed: ${e.message}`);
        await setChallanCheckingFlag({
            vehicleNumber,
            checking: false,
            summary: `enqueue failed: ${e.message}`,
        }).catch(() => { });
        return json(
            { ok: false, error: `could not enqueue the job: ${e.message}` },
            500,
        );
    }

    await setAgentStatus({
        requestId,
        driverId,
        to: STATUS.QUEUED,
        source,
        task: "challan-settlement", // seeds queued status on challans/{VEH}
        force: true,
    }).catch((e: any) =>
        console.error(`[run] challan-settlement seed queued failed: ${e.message}`),
    );

    return json({
        ok: true,
        jobId,
        status: "queued",
        task: "challan-settlement",
        departments,
        unmapped, // client-visible: these challans cannot be quoted at all
    });
}
