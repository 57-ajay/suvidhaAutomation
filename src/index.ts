// api/src/index.ts
//
// Pivot-a border-tax API. BUN + TS. Thin router; all logic lives in routes/* and
// internal/*. Firestore is imported here so admin initializes at boot.

import { config } from "./config";
import { json, corsHeaders } from "./lib/http";
import "./firebase";
import { handleRun } from "./routes/run";
import { handleStatus, handleIntervene, handleCancel } from "./routes/jobs";
import { handleInternal } from "./routes/internal";
import { supportedStates } from "./validators/borderTax";

const server = Bun.serve({
    port: config.port,
    async fetch(req) {
        if (req.method === "OPTIONS") {
            return new Response(null, { status: 204, headers: corsHeaders() });
        }

        const url = new URL(req.url);
        const p = url.pathname;

        try {
            if (req.method === "GET" && p === "/api/health") {
                return json({ ok: true, pipeline: config.pipelineTag, states: supportedStates() });
            }
            if (req.method === "GET" && p === "/api/tasks") {
                return json({ tasks: [{ id: "border-tax", states: supportedStates() }] });
            }
            if (req.method === "POST" && p === "/api/run") {
                return await handleRun(req);
            }

            // /api/jobs/:id/(status|intervene|cancel)
            const m = p.match(/^\/api\/jobs\/([^/]+)\/(status|intervene|cancel)$/);
            if (m) {
                const [, jobId, action] = m;
                if (action === "status" && req.method === "GET")
                    return await handleStatus(jobId!);
                if (action === "intervene" && req.method === "POST")
                    return await handleIntervene(jobId!, req);
                if (action === "cancel" && req.method === "POST")
                    return await handleCancel(jobId!);
            }

            // /api/internal/*
            if (p.startsWith("/api/internal/") && req.method === "POST") {
                const res = await handleInternal(p, req);
                if (res) return res;
            }

            return json({ error: "not found", path: p }, 404);
        } catch (e: any) {
            console.error(`[api] ERROR ${req.method} ${p}:`, e);
            return json({ ok: false, error: e.message }, 500);
        }
    },
});

console.log(`[api] pivot-a listening on :${server.port} (pipeline=${config.pipelineTag})`);
