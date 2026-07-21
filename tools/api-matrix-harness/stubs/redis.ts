// stub: records writes on globalThis.__cap
export const JOB_TTL = 86400;
export const keys = {
  job: (id: string) => `job:${id}`,
  allJobs: "jobs:all",
  queue: "jobs:queue",
};
function cap(): any { return (globalThis as any).__cap; }
export const redis = {
  async hget(_k: string, _f: string) { return cap().existing ?? null; },
  multi() {
    const rec: any = { hsets: [] };
    const chain: any = {
      del: () => chain,
      hset: (_k: string, obj: any) => { rec.hsets.push(obj); return chain; },
      expire: () => chain,
      zadd: () => chain,
      lpush: () => chain,
      exec: async () => { cap().multi = rec; return []; },
    };
    return chain;
  },
};
