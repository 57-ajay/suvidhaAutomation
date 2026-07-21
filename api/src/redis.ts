// One Redis connection for the whole API. The job hash is the live operational
// view (dashboard, /status endpoint); Firestore is the client-facing record.

import Redis from "ioredis";
import { config } from "./config";

export const redis = new Redis(config.redisUrl, { maxRetriesPerRequest: 3 });

export const JOB_TTL = 60 * 60 * 24; // jobs stay inspectable for 24h

export const keys = {
    job: (jobId: string) => `job:${jobId}`,
    queue: "job:queue",
    allJobs: "jobs:all", // zset scored by createdAt ms
} as const;
