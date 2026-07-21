// POST /api/verify-pending
// Body: { requestId, driverId, vehicleNumber, state, source? }
//
// Called by the backend's sweeper for requests sitting at
// verifyingPendingPayment. Enqueues a one-shot verify job onto the same queue
// (jobId == requestId). Does NOT touch the Firestore status — it stays
// verifyingPendingPayment until the verify job resolves it (a queued write here
// would be an illegal backward transition).

import { redis, keys, JOB_TTL } from "../redis";
import { json, readJson } from "../lib/http";

const ACTIVE = new Set(["queued", "running", "waiting_for_human"]);

export async function handleVerifyPending(req: Request): Promise<Response> {
    const b = await readJson(req);
    const requestId = b.requestId as string;
    const driverId = b.driverId as string;
    const vehicleNumber = b.vehicleNumber as string;
    const state = (b.state ?? b.stateCode) as string;
    const source: string = b.source ?? "app";

    const missing = ["requestId", "driverId", "vehicleNumber", "state"].filter(
        (k) => !({ requestId, driverId, vehicleNumber, state } as any)[k],
    );
    if (missing.length) {
        return json({ ok: false, error: "validation_failed", missing }, 400);
    }

    const jobId = requestId; // same id as the original request
    const existing = await redis.hget(keys.job(jobId), "status");
    if (existing && ACTIVE.has(existing)) {
        return json({ ok: true, jobId, deduped: true, status: existing });
    }

    const now = new Date();
    await redis
        .multi()
        .del(keys.job(jobId))
        .hset(keys.job(jobId), {
            id: jobId,
            taskId: "border-tax",
            task: "verifyPendingPayment",       // <- worker picks the verify pipeline
            state,
            params: JSON.stringify({ requestId, driverId, vehicleNumber, state }),
            status: "queued",
            agentStatus: "verifyingPendingPayment", // operational; Firestore unchanged
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

    return json({ ok: true, jobId, status: "queued", task: "verifyPendingPayment" });
}
