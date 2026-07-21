// api/src/redis.ts
import Redis from "ioredis";
import { config } from "./config";

export const redis = new Redis(config.redisUrl, {
    maxRetriesPerRequest: null,
});

redis.on("error", (e) => console.error("[redis] error:", e.message));
redis.on("connect", () => console.log("[redis] connected"));

export const JOB_TTL = config.jobTtlSeconds;

export const keys = {
    job: (id: string) => `job:${id}`,
    queue: "job:queue",
    allJobs: "jobs:all",
    taskJobs: (taskId: string) => `jobs:task:${taskId}`,
};
