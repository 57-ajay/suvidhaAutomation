// POST /api/run
// Body: { taskId: "border-tax", source: "app", params: {...} }
//
//   1. Per-state validation. Failure -> 400 with { missing, invalid, message }
//      so the client can tell the driver exactly what to fix. Nothing enqueued.
//   2. Eligibility pre-check. Ineligible -> request marked cancelled up front,
//      200 with status "cancelled" + reason.
//   3. Dedupe on requestId: an already-active job is returned, not restarted.
//   4. Enqueue + seed aiAgentData.status = queued.

import { redis, keys, JOB_TTL } from "../redis";
import { json, readJson } from "../lib/http";
import { getStateValidator, supportedStates } from "../validators";
import { checkEligibility } from "../internal/eligibility";
import { setAgentStatus } from "../internal/requestDoc";
import { STATUS } from "../lifecycle/statuses";

const ACTIVE = new Set(["queued", "running", "waiting_for_human"]);

export async function handleRun(req: Request): Promise<Response> {
    const body = await readJson(req);
    const taskId: string = body.taskId ?? "border-tax";
    const rawParams: Record<string, unknown> = body.params ?? {};
    // Mandatory per product spec; accepted top-level or inside params.
    const source: string | undefined = body.source ?? (rawParams.source as string);

    if (taskId !== "border-tax" && taskId !== "puc-certificate") {
        return json({ ok: false, error: `unsupported taskId: ${taskId}` }, 400);
    }
    if (!source) {
        return json(
            {
                ok: false,
                error: "validation_failed",
                missing: ["source"],
                invalid: [],
                message: "source is required",
            },
            400,
        );
    }
    if (source !== "app") {
        return json(
            { ok: false, error: `unsupported source: ${source} (app only)` },
            400,
        );
    }

    // PUC certificate: no per-state validator, no eligibility, no money line.
    if (taskId === "puc-certificate") {
        return await handlePucRun(rawParams, source);
    }

    const validator = getStateValidator(
        (rawParams.stateCode as string) ?? (rawParams.state as string),
    );
    if (!validator) {
        return json(
            {
                ok: false,
                error: "unsupported_state",
                message:
                    `No runner for state "${rawParams.state ?? rawParams.stateCode ?? ""}". ` +
                    `Supported: ${supportedStates().map((s) => s.code).join(", ")}.`,
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

    const elig = await checkEligibility(params);
    if (!elig.eligible) {
        await setAgentStatus({
            requestId: params.requestId,
            driverId: params.driverId,
            to: STATUS.CANCELLED,
            source,
            error: elig.reason ?? "vehicle not eligible",
            force: true,
        }).catch((e) =>
            console.error(`[run] eligibility cancel write failed: ${e.message}`),
        );
        return json({
            ok: true,
            status: "cancelled",
            reason: elig.reason,
            requestId: params.requestId,
        });
    }

    // jobId == requestId: the client talks to /api/jobs/:requestId/* directly.
    const jobId = params.requestId;
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
            taskId,
            state: params.state,
            params: JSON.stringify(params),
            status: "queued",
            source,
            requestId: params.requestId,
            driverId: params.driverId,
            vehicleNumber: params.vehicleNumber,
            createdAt: now.toISOString(),
        })
        .expire(keys.job(jobId), JOB_TTL)
        .zadd(keys.allJobs, now.getTime(), jobId)
        .lpush(keys.queue, jobId)
        .exec();

    await setAgentStatus({
        requestId: params.requestId,
        driverId: params.driverId,
        to: STATUS.QUEUED,
        source,
        force: true,
    }).catch((e) =>
        console.error(`[run] seed queued status failed: ${e.message}`),
    );

    return json({ ok: true, jobId, status: "queued" });
}

// PUC has no state/eligibility concept, so it validates inline rather than
// going through the state-validator registry (which is keyed by state code).
const PUC_REQUIRED = ["requestId", "driverId", "registrationNumber", "chassisNumber"];

async function handlePucRun(
    rawParams: Record<string, unknown>,
    source: string,
): Promise<Response> {
    const get = (k: string) => {
        const v = rawParams[k];
        return typeof v === "string"
            ? v.trim()
            : typeof v === "number"
                ? String(v)
                : "";
    };

    const missing = PUC_REQUIRED.filter((k) => !get(k));
    if (missing.length) {
        return json(
            {
                ok: false,
                error: "validation_failed",
                missing,
                invalid: [],
                message: `Cannot start PUC certificate — missing: ${missing.join(", ")}.`,
            },
            400,
        );
    }

    const params = {
        requestId: get("requestId"),
        driverId: get("driverId"),
        registrationNumber: get("registrationNumber").toUpperCase(),
        chassisNumber: get("chassisNumber").toUpperCase(), // worker takes last 5
        mobileNumber: get("mobileNumber"),
        source,
    };

    // jobId == requestId, same convention as border-tax.
    const jobId = params.requestId;
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
            taskId: "puc-certificate", // worker dispatches the PUC flow off this
            params: JSON.stringify(params),
            status: "queued",
            source,
            requestId: params.requestId,
            driverId: params.driverId,
            vehicleNumber: params.registrationNumber, // ops-console label only
            createdAt: now.toISOString(),
        })
        .expire(keys.job(jobId), JOB_TTL)
        .zadd(keys.allJobs, now.getTime(), jobId)
        .lpush(keys.queue, jobId)
        .exec();

    await setAgentStatus({
        requestId: params.requestId,
        driverId: params.driverId,
        to: STATUS.QUEUED,
        source,
        task: "puc-certificate", // seeds the queued status in pucRequests
        force: true,
    }).catch((e) =>
        console.error(`[run] PUC seed queued status failed: ${e.message}`),
    );

    return json({ ok: true, jobId, status: "queued", task: "puc-certificate" });
}
