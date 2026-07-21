// api/src/routes/jobs.ts
// GET  /api/jobs/:id/status     -> redis job hash (minus bulky fields)
// POST /api/jobs/:id/intervene  -> push humanInput (captcha text, or "paid")
// POST /api/jobs/:id/cancel     -> request cancellation; worker monitor reacts

import { redis, keys, JOB_TTL } from "../redis";
import { json, readJson } from "../lib/http";
import { applyTerminal } from "../internal/requestDoc";
import { STATUS } from "../lifecycle/statuses";

const TERMINAL = new Set(["done", "cancelled", "failed", "partial"]);

export async function handleStatus(jobId: string): Promise<Response> {
    const job = await redis.hgetall(keys.job(jobId));
    if (!job || !job.id) return json({ error: "not found" }, 404);
    const { params, ...rest } = job;
    return json(rest);
}

export async function handleIntervene(
    jobId: string,
    req: Request,
): Promise<Response> {
    const job = await redis.hgetall(keys.job(jobId));
    if (!job || !job.id) return json({ error: "not found" }, 404);

    if (job.status !== "waiting_for_human") {
        return json(
            { error: "job is not waiting for input", status: job.status },
            400,
        );
    }

    const body = await readJson(req);
    const input = body.input;
    if (input === undefined || input === null || `${input}`.length === 0) {
        return json({ error: "input required" }, 400);
    }

    await redis.hset(keys.job(jobId), "humanInput", `${input}`);
    return json({ ok: true, message: "input submitted, agent will resume" });
}

export async function handleList(params: URLSearchParams): Promise<Response> {
    const limit = Math.min(100, Math.max(1, parseInt(params.get("limit") ?? "50", 10) || 50));
    const offset = Math.max(0, parseInt(params.get("offset") ?? "0", 10) || 0);
    const taskId = (params.get("taskId") ?? "").trim(); // "" = all tasks
    const status = (params.get("status") ?? "").trim(); // "" = all statuses
    const source = (params.get("source") ?? "").trim(); // "" = all sources
    const path = (params.get("path") ?? "").trim(); // "" = all paths

    // Newest-first ids, bounded so a busy console can't fan out unbounded
    // HGETALLs. 1000 jobs is far more than the 24h TTL window ever holds.
    const SCAN_MAX = 1000;
    const ids = await redis.zrevrange(keys.allJobs, 0, SCAN_MAX - 1);

    // Status breakdown for the header cards. Respects the task filter (so the
    // cards reflect the selected task) but NOT the status filter — the cards ARE
    // the status breakdown. The returned list additionally honours `status`.
    const counts: Record<string, number> = {
        done: 0, cancelled: 0, failed: 0, partial: 0,
        running: 0, waiting_for_human: 0, queued: 0, parked: 0, total: 0,
    };
    const matched: Record<string, string>[] = [];
    for (const id of ids) {
        const job = await redis.hgetall(keys.job(id));
        if (!job || !job.id) continue; // expired/cleared
        if (taskId && (job.taskId ?? "border-tax") !== taskId) continue;
        // Pre-scripted jobs have no `path` hash field; they were all app +
        // fullyAutomated, so the defaults keep them filterable.
        if (source && (job.source ?? "app") !== source) continue;
        if (path && (job.path ?? "fullyAutomated") !== path) continue;

        const st = job.status ?? "";
        counts.total++;
        if (Object.prototype.hasOwnProperty.call(counts, st)) counts[st]++;

        if (status && st !== status) continue;
        const { params: _drop, ...rest } = job;
        matched.push(rest);
    }

    const total = matched.length;          // count AFTER filters (within the scan window)
    const page = matched.slice(offset, offset + limit);
    return json({ jobs: page, total, counts, limit, offset });
}

export async function handleCancel(jobId: string): Promise<Response> {
    const job = await redis.hgetall(keys.job(jobId));
    if (!job || !job.id) return json({ error: "not found" }, 404);
    if (TERMINAL.has(job.status ?? "")) {
        return json({ error: `job already ${job.status}` }, 400);
    }
    //
    // if (job.status === "queued") {
    //     await redis.lrem(keys.queue, 0, jobId);
    // }
    const wasQueued = job.status === "queued";
    if (wasQueued) {
        await redis.lrem(keys.queue, 0, jobId);
    }
    await redis.hset(keys.job(jobId), "status", "cancelled");
    await redis.expire(keys.job(jobId), JOB_TTL);

    // A RUNNING job's Firestore terminal is written when it stops — by the
    // worker, or the orchestrator's finalize_orphan fallback. But a job
    // cancelled while still QUEUED is pulled from the queue above and never
    // reaches a worker, so nothing else writes its terminal and the client
    // doc would sit at "queued" forever. Write it here. Queued is always
    // pre-payment -> a clean cancelled; terminalApplied guards a double-write
    // if a worker raced in before the lrem.
    if (wasQueued && job.requestId && job.driverId) {
        await applyTerminal({
            requestId: job.requestId,
            driverId: job.driverId,
            task: job.taskId ?? "border-tax",
            to: STATUS.CANCELLED,
            source: job.source ?? "app",
            summary: "cancelled by user before the run started",
            error: "cancelled by user",
        }).catch((e) =>
            console.error(`[cancel] Firestore terminal write failed for ${jobId}: ${e.message}`),
        );
        await redis.hset(keys.job(jobId), "terminalApplied", "1");
    }

    return json({ ok: true, message: "cancellation requested" });
}
