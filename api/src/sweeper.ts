// Zombie sweeper. finalize_orphan needs a living orchestrator; when a worker
// pod is SIGKILLed (grace expiry, node loss, OOM of main.py) its in-flight
// jobs freeze at running/waiting_for_human forever. Workers >= v5 heartbeat
// `leaseUntil` on every job they hold; this sweep finalizes anything whose
// lease expired, with the same money-line semantics as finalize_orphan.
// Jobs without a lease (pre-v5, or genuinely still in the queue) are never
// touched. `terminalApplied=1` marks "already closed properly; reaper skips"
// — the contract run_job.py already writes.

import { redis, keys, JOB_TTL } from "./redis";
import { handleJobCompleted } from "./internal/jobCompleted";

const SWEEP_INTERVAL_MS = 60_000;
const SCAN_MAX = 1000;
const TERMINAL = new Set(["done", "cancelled", "failed", "partial"]);
// status=queued WITH a lease means "picked up by a worker, then lost".
const SWEEPABLE = new Set(["running", "waiting_for_human", "queued"]);

// Keep in lockstep with worker/src/lifecycle/status.py
const PRE_PAYMENT = new Set([
    "queued", "aiAgentStarted", "pendingTransaction",
    "pendingTransactionCaptcha", "captchaSolving",
]);
const PAYMENT_LIKELY = new Set(["verifyingPayment", "generatingReceipt"]);

let sweeping = false;

export function startZombieSweeper(): void {
    setInterval(() => void sweep(), SWEEP_INTERVAL_MS);
    console.log(`[sweeper] zombie sweeper armed (every ${SWEEP_INTERVAL_MS / 1000}s)`);
}

async function sweep(): Promise<void> {
    if (sweeping) return;
    sweeping = true;
    try {
        const now = Math.floor(Date.now() / 1000);
        const ids = await redis.zrevrange(keys.allJobs, 0, SCAN_MAX - 1);
        for (const id of ids) {
            const [status, agentStatus, lease, applied] = await redis.hmget(
                keys.job(id), "status", "agentStatus", "leaseUntil", "terminalApplied",
            );
            console.log({ "[agentStatus]": agentStatus });
            if (!status || applied === "1" || !SWEEPABLE.has(status)) continue;
            const leaseAt = lease ? parseInt(lease, 10) : NaN;
            if (!Number.isFinite(leaseAt) || leaseAt >= now) continue;
            await finalizeZombie(id, now).catch((e) =>
                console.error(`[sweeper] finalize failed for ${id}:`, e));
        }
    } catch (e) {
        console.error("[sweeper] pass failed:", e);
    } finally {
        sweeping = false;
    }
}

async function finalizeZombie(jobId: string, now: number): Promise<void> {
    // Fresh full read right before acting — the job may have finished, or a
    // live worker may have refreshed the lease, between scan and here.
    const job = await redis.hgetall(keys.job(jobId));
    if (!job || !job.id || job.terminalApplied === "1") return;
    if (TERMINAL.has(job.status ?? "")) return;
    const leaseAt = parseInt(job.leaseUntil ?? "", 10);
    if (!Number.isFinite(leaseAt) || leaseAt >= now) return;

    let params: Record<string, any> = {};
    try { params = JSON.parse(job.params ?? "{}"); } catch { /* ignore */ }
    const requestId = job.requestId || params.requestId || jobId;
    const driverId = job.driverId || params.driverId || "";

    const agent = job.agentStatus ?? "";
    const pre = agent === "" || PRE_PAYMENT.has(agent);
    const status = pre ? "cancelled" : "failed";
    const summary = pre
        ? "the agent stopped unexpectedly before payment"
        : "the agent stopped unexpectedly during the payment window — reconcile manually";

    // Redis mirror first (dashboard truth), same shape as finalize_orphan.
    await redis.hset(keys.job(jobId), {
        status, agentStatus: status, result: summary, error: summary,
        sweptAt: new Date().toISOString(),
    });
    await redis.hdel(keys.job(jobId), "waitReason", "humanInput", "leaseUntil");
    await redis.expire(keys.job(jobId), JOB_TTL);

    // Firestore terminal through the exact path the worker uses.
    const resp = await handleJobCompleted({
        jobId, requestId, driverId,
        status: status as "cancelled" | "failed",
        source: job.source ?? "app",
        summary, error: summary,
        paymentLikely: PAYMENT_LIKELY.has(agent),
        task: job.taskId ?? "border-tax",
    });
    if (resp.ok) await redis.hset(keys.job(jobId), "terminalApplied", "1");
    console.log(
        `[sweeper] zombie ${jobId} (agentStatus=${agent || "?"}) -> ${status}` +
        (resp.ok ? "" : ` — firestore write rejected: ${(resp as any).error}`),
    );
}
