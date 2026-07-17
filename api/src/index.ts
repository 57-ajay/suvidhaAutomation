// Border-tax API. Bun + TS. Thin router; logic lives in routes/* and
// internal/*. Firebase is imported here so admin initializes at boot and a bad
// credential fails fast instead of on the first request.

import { config } from "./config";
import { json, corsHeaders } from "./lib/http";
import "./firebase";
import { handleRun } from "./routes/run";
import {
    handleStatus,
    handleIntervene,
    handleCancel,
    handleList,
} from "./routes/jobs";
import { handleInternal } from "./routes/internal";
import { supportedStates } from "./validators";
import { DASHBOARD_HTML } from "./dashboard";
import { handleVerifyPending } from "./routes/verifyPending";
import { startZombieSweeper } from "./sweeper";

const server = Bun.serve({
    port: config.port,
    async fetch(req) {
        if (req.method === "OPTIONS") {
            return new Response(null, { status: 204, headers: corsHeaders() });
        }

        const url = new URL(req.url);
        const p = url.pathname;

        // Opt-in guard for public mutating routes. No-op unless PUBLIC_API_KEY
        // is set (so existing private deployments are unaffected).
        const publicAuthorized = () =>
            !config.publicApiKey ||
            req.headers.get("X-Internal-Key") === config.publicApiKey;
        const unauthorized = () => json({ ok: false, error: "unauthorized" }, 401);

        try {
            if (req.method === "GET" && p === "/api/health") {
                return json({
                    ok: true,
                    pipeline: config.pipelineTag,
                    states: supportedStates(),
                });
            }
            if (req.method === "GET" && p === "/api/tasks") {
                return json({
                    tasks: [
                        { id: "border-tax", states: supportedStates() },
                        { id: "puc-certificate", states: [] },
                    ],
                });
            }
            if (req.method === "GET" && p === "/api/jobs") {
                return await handleList(url.searchParams);
            }
            if (req.method === "POST" && p === "/api/run") {
                if (!publicAuthorized()) return unauthorized();
                return await handleRun(req);
            }
            if (req.method === "POST" && p === "/api/verify-pending") {
                if (!publicAuthorized()) return unauthorized();
                return handleVerifyPending(req);
            }
            if (req.method === "GET" && p === "/api/dashboard") {
                return new Response(DASHBOARD_HTML, {
                    headers: { "Content-Type": "text/html", ...corsHeaders() },
                });
            }

            const m = p.match(/^\/api\/jobs\/([^/]+)\/(status|intervene|cancel)$/);
            if (m) {
                const [, jobId, action] = m;
                if (action === "status" && req.method === "GET")
                    return await handleStatus(jobId!);
                if (action === "intervene" && req.method === "POST") {
                    if (!publicAuthorized()) return unauthorized();
                    return await handleIntervene(jobId!, req);
                }
                if (action === "cancel" && req.method === "POST") {
                    if (!publicAuthorized()) return unauthorized();
                    return await handleCancel(jobId!);
                }
            }

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

startZombieSweeper();
process.on("SIGTERM", () => { console.log("[api] SIGTERM — exiting"); process.exit(0); });
console.log(
    `[api] border-tax listening on :${server.port} (pipeline=${config.pipelineTag})`,
);
