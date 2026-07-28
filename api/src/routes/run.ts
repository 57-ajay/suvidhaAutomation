// api/src/routes/run.ts
// POST /api/run
// Body: { taskId: "border-tax", source: "app" | "web", path?, params: {...} }
//
//   1. Source/path routing (border-tax):
//        source is REQUIRED ("app" | "web").
//        app  -> path is always "fullyAutomated" (an explicit different path
//                is rejected; there is no operator on the app flow).
//        web  -> path is REQUIRED: "scripted" | "fullyAutomated".
//        fullyAutomated -> state must be in AUTO_STATE_CODES (UP/HR/MP/PB/HP).
//        scripted       -> state must be in the env allow-list
//                          SCRIPTED_BORDER_TAX_STATES (config.scriptedStates);
//                          empty allow-list = scripted disabled everywhere.
//      PUC stays app-only; challan-settlement keeps its own handling.
//   2. Per-state validation. Failure -> 400 with { missing, invalid, message }
//      so the client can tell the caller exactly what to fix. Nothing enqueued.
//      (The old eligibility pre-check is REMOVED — the client checks
//      eligibility itself; any task that reaches us is a task to complete.)
//   3. Dedupe on requestId: an already-active job is returned, not restarted.
//   4. Enqueue (job hash carries `path`) + seed aiAgentData.status = queued
//      with aiAgentData.path, so the client can filter on it.

import { redis, keys, JOB_TTL } from "../redis";
import { json, readJson } from "../lib/http";
import {
    getStateValidator,
    supportedStates,
    isAutoCapable,
    isScriptedEnabled,
} from "../validators";
import { setAgentStatus } from "../internal/requestDoc";
import { handleChallanSettlementRun } from "./challanSettlementRun";
import { handleChallanPaymentRun } from "./challanPaymentRun";
import { STATUS } from "../lifecycle/statuses";

const ACTIVE = new Set(["queued", "running", "waiting_for_human"]);

export async function handleRun(req: Request): Promise<Response> {
    const body = await readJson(req);
    const taskId: string = body.taskId ?? "border-tax";
    const rawParams: Record<string, unknown> = body.params ?? {};
    // Mandatory per product spec; accepted top-level or inside params.
    const source: string | undefined = body.source ?? (rawParams.source as string);

    if (
        taskId !== "border-tax" &&
        taskId !== "puc-certificate" &&
        taskId !== "challan-settlement" &&
        taskId !== "challan-payment"
    ) {
        return json({ ok: false, error: `unsupported taskId: ${taskId}` }, 400);
    }

    if (taskId === "challan-settlement") {
        if (source !== "app" && source !== "web") {
            return json(
                { ok: false, error: `unsupported source: ${source}` },
                400,
            );
        }
        return await handleChallanSettlementRun(rawParams, source);
    }

    // Challan payment: scripted-only (there is no fully-automated path — the
    // OTP goes to the driver's phone), so it takes no `path`, exactly like
    // challan-settlement and PUC. One challan per request.
    if (taskId === "challan-payment") {
        if (source !== "app" && source !== "web") {
            return json(
                { ok: false, error: `unsupported source: ${source}` },
                400,
            );
        }
        return await handleChallanPaymentRun(rawParams, source);
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
    if (source !== "app" && source !== "web") {
        return json(
            { ok: false, error: `unsupported source: ${source} (app | web)` },
            400,
        );
    }

    // PUC certificate: app-only (no operator flow), no per-state validator,
    // no money line.
    if (taskId === "puc-certificate") {
        if (source !== "app") {
            return json(
                {
                    ok: false,
                    error: `unsupported source for puc-certificate: ${source} (app only)`,
                },
                400,
            );
        }
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

    // ── path resolution (see the header matrix) ─────────────────────────
    const rawPath: string | undefined =
        (body.path as string) ?? (rawParams.path as string | undefined);
    let path: "fullyAutomated" | "scripted";
    if (source === "app") {
        if (rawPath && rawPath !== "fullyAutomated") {
            return json(
                {
                    ok: false,
                    error: "unsupported_path",
                    message:
                        `path "${rawPath}" is not available for source "app" — ` +
                        "app runs are always fullyAutomated",
                },
                400,
            );
        }
        path = "fullyAutomated";
    } else {
        if (!rawPath) {
            return json(
                {
                    ok: false,
                    error: "validation_failed",
                    missing: ["path"],
                    invalid: [],
                    message:
                        'path is required for source "web": "scripted" or ' +
                        '"fullyAutomated"',
                },
                400,
            );
        }
        if (rawPath !== "scripted" && rawPath !== "fullyAutomated") {
            return json(
                {
                    ok: false,
                    error: "unsupported_path",
                    message:
                        `unsupported path: ${rawPath} ` +
                        '("scripted" | "fullyAutomated")',
                },
                400,
            );
        }
        path = rawPath;
    }

    if (path === "fullyAutomated" && !isAutoCapable(validator.code)) {
        return json(
            {
                ok: false,
                error: "unsupported_path",
                message:
                    `state ${validator.code} is not fully automated — ` +
                    'run it with path "scripted"',
            },
            400,
        );
    }
    if (path === "scripted" && !isScriptedEnabled(validator.code)) {
        return json(
            {
                ok: false,
                error: "unsupported_path",
                message:
                    `the scripted path is not enabled for state ` +
                    `${validator.code} — add it to SCRIPTED_BORDER_TAX_STATES ` +
                    "on the API to roll it out",
            },
            400,
        );
    }

    // The five fully-automated validators pin paymentMethod to UPI (their
    // pipeline IS UPI). On the scripted path the operator picks the method at
    // the portal, so the pin must not 400 a web request: satisfy it for
    // validation, then put the caller's explicit value back afterwards. The
    // scripted-only validators (UK/TN/BR) don't pin and keep their own
    // defaults — no shim for them.
    const callerPaymentMethod = rawParams.paymentMethod;
    if (path === "scripted" && isAutoCapable(validator.code)) {
        rawParams.paymentMethod = "UPI";
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
    if (
        path === "scripted" &&
        isAutoCapable(validator.code) &&
        typeof callerPaymentMethod === "string" &&
        callerPaymentMethod.trim()
    ) {
        params.paymentMethod = callerPaymentMethod.trim().toUpperCase();
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
            path,
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
        // aiAgentData.path is written once here and persists across merges,
        // so the client can filter fullyAutomated vs scripted requests.
        extra: { path },
        force: true,
    }).catch((e) =>
        console.error(`[run] seed queued status failed: ${e.message}`),
    );

    return json({ ok: true, jobId, status: "queued", path });
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
