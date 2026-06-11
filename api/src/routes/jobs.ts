// api/src/routes/jobs.ts
//
// GET  /api/jobs/:id/status     -> redis job hash (sans bulky fields)
// POST /api/jobs/:id/intervene  -> push humanInput (captcha text, OTP, "paid")
// POST /api/jobs/:id/cancel     -> request cancellation; worker monitor reacts

import { redis, keys } from "../redis";
import { json, readJson } from "../lib/http";

export async function handleStatus(jobId: string): Promise<Response> {
    const jobHash = await redis.hgetall(keys.job(jobId));
    if (!jobHash || !jobHash.id) {
        return json({ error: "not found" }, 404);
    }
    // Strip fields the client doesn't need.
    const { prompt, tools, ...rest } = jobHash as Record<string, string>;
    return json(rest);
}

export async function handleIntervene(
    jobId: string,
    req: Request,
): Promise<Response> {
    const jobHash = await redis.hgetall(keys.job(jobId));
    if (!jobHash || !jobHash.id) return json({ error: "not found" }, 404);

    if (jobHash.status !== "waiting_for_human") {
        return json(
            { error: "job is not waiting for input", status: jobHash.status },
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

export async function handleCancel(jobId: string): Promise<Response> {
    const jobHash = await redis.hgetall(keys.job(jobId));
    if (!jobHash || !jobHash.id) return json({ error: "not found" }, 404);

    await redis.hset(keys.job(jobId), "status", "cancelled");
    return json({ ok: true, message: "cancellation requested" });
}
