// api/src/routes/run.ts
//
// POST /api/run
// Body: { taskId: "border-tax", params: {...}, source?: "app" }
//
// Order of operations:
//   1. Resolve the per-state validator and validate params. On failure -> 400
//      with { missing, invalid, message } so the app can tell the driver exactly
//      what to fix. Nothing is enqueued.
//   2. Pre-launch eligibility (PUCC/insurance/fitness). Ineligible -> the request
//      is marked cancelled up front and we return 200 with status "cancelled".
//   3. Enqueue: write the job hash, push to the queue, seed aiAgentData.status.

import { randomUUID } from "crypto";
import { redis, keys, JOB_TTL } from "../redis";
import { json, readJson } from "../lib/http";
import { getStateValidator } from "../validators/borderTax";
import { checkEligibility } from "../internal/eligibility";
import { setAgentStatus } from "../internal/requestDoc";
import { STATUS } from "../lifecycle/statuses";
import { config } from "../config";

export async function handleRun(req: Request): Promise<Response> {
    const body = await readJson(req);
    const taskId: string = body.taskId ?? "border-tax";
    const source: string = body.source ?? "app";
    const rawParams: Record<string, unknown> = body.params ?? {};

    if (taskId !== "border-tax") {
        return json({ ok: false, error: `unsupported taskId: ${taskId}` }, 400);
    }

    // 1. Validate.
    const validator = getStateValidator(
        (rawParams.stateCode as string) ?? (rawParams.state as string),
    );
    if (!validator) {
        return json(
            {
                ok: false,
                error: "unsupported_state",
                message: `No validator for state "${rawParams.state ?? rawParams.stateCode ?? ""}". Pivot-a ships UP only.`,
            },
            400,
        );
    }

    const result = validator.validate(rawParams);
    if (!result.ok) {
        return json(
            {
                ok: false,
                error: "validation_failed",
                missing: result.missing,
                invalid: result.invalid,
                message: result.message,
            },
            400,
        );
    }
    const params = result.params;

    // 2. Eligibility (cheap cancel before spending a browser slot).
    const elig = await checkEligibility(params);
    if (!elig.eligible) {
        await setAgentStatus({
            requestId: params.requestId,
            driverId: params.driverId,
            to: STATUS.CANCELLED,
            source,
            error: elig.reason ?? "vehicle not eligible",
            force: true, // first write
        }).catch((e) => console.error(`[run] eligibility cancel write failed: ${e.message}`));
        return json({
            ok: true,
            status: "cancelled",
            reason: elig.reason,
            requestId: params.requestId,
        });
    }

    // 3. Enqueue.
    const jobId = randomUUID().replace(/-/g, "").slice(0, 20);
    const createdAt = new Date().toISOString();
    const createdAtMs = Date.now();

    const pipeline = redis.pipeline();
    pipeline.hset(keys.job(jobId), {
        id: jobId,
        taskId,
        params: JSON.stringify(params),
        status: "queued",
        source,
        pipeline: config.pipelineTag,
        createdAt,
    });
    pipeline.expire(keys.job(jobId), JOB_TTL);
    pipeline.zadd(keys.allJobs, createdAtMs, jobId);
    pipeline.zadd(keys.taskJobs(taskId), createdAtMs, jobId);
    pipeline.lpush(keys.queue, jobId);
    await pipeline.exec();

    // Seed lifecycle status (force: first write from nothing -> queued).
    await setAgentStatus({
        requestId: params.requestId,
        driverId: params.driverId,
        to: STATUS.QUEUED,
        source,
        force: true,
    }).catch((e) => console.error(`[run] seed status failed: ${e.message}`));

    return json({ ok: true, jobId, status: "queued", requestId: params.requestId });
}
