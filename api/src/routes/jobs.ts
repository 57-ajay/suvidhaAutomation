// GET  /api/jobs/:id/status     -> redis job hash (minus bulky fields)
// POST /api/jobs/:id/intervene  -> push humanInput (captcha text, or "paid")
// POST /api/jobs/:id/cancel     -> request cancellation; worker monitor reacts

import { redis, keys, JOB_TTL } from "../redis";
import { json, readJson } from "../lib/http";

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

export async function handleList(limitRaw: string | null): Promise<Response> {
    const limit = Math.min(50, Math.max(1, parseInt(limitRaw ?? "20", 10) || 20));
    const ids = await redis.zrevrange(keys.allJobs, 0, limit - 1);
    const jobs: Record<string, string>[] = [];
    for (const id of ids) {
        const job = await redis.hgetall(keys.job(id));
        if (!job || !job.id) continue; // expired
        const { params, ...rest } = job;
        jobs.push(rest);
    }
    return json({ jobs });
}

export async function handleCancel(jobId: string): Promise<Response> {
    const job = await redis.hgetall(keys.job(jobId));
    if (!job || !job.id) return json({ error: "not found" }, 404);
    if (TERMINAL.has(job.status ?? "")) {
        return json({ error: `job already ${job.status}` }, 400);
    }

    if (job.status === "queued") {
        await redis.lrem(keys.queue, 0, jobId);
    }
    await redis.hset(keys.job(jobId), "status", "cancelled");
    await redis.expire(keys.job(jobId), JOB_TTL);
    return json({ ok: true, message: "cancellation requested" });
}
