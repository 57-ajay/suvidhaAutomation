// Live-view router. /websockify?token=<jobId> -> HGET job:<id> podIp ->
// relay the websocket to ws://<podIp>:6080. Also serves the noVNC static
// files so the Ingress needs exactly one rule for everything non-/api.

import { resolve } from "path";
import Redis from "ioredis";

const PORT = Number(process.env.PORT ?? 8080);
const NOVNC_DIR = resolve(process.env.NOVNC_DIR ?? "/usr/share/novnc");
const WORKER_WS_PORT = Number(process.env.WORKER_WS_PORT ?? 6080);

const redis = new Redis(process.env.REDIS_URL ?? "redis://redis:6379", {
    maxRetriesPerRequest: 3,
});

type Ctx = {
    token: string;
    podIp: string;
    upstream: WebSocket | null;
    pending: (string | Uint8Array)[]; // client frames until upstream opens
};

const server = Bun.serve<Ctx>({
    port: PORT,
    async fetch(req, server) {
        const url = new URL(req.url);

        if (url.pathname === "/healthz") return new Response("ok");

        if (url.pathname === "/websockify") {
            const token = url.searchParams.get("token") ?? "";
            if (!token) return new Response("token required", { status: 400 });

            let podIp: string | null = null;
            try {
                podIp = await redis.hget(`job:${token}`, "podIp");
            } catch (e) {
                console.error(`[router] redis lookup failed for ${token}:`, e);
                return new Response("lookup failed", { status: 503 });
            }
            if (!podIp) return new Response("unknown job", { status: 404 });

            // Echo the client's subprotocol. noVNC/websockify may negotiate
            // "binary"; a proxy that swallows the header breaks the
            // handshake silently (browser closes with no useful error).
            const proto = req.headers.get("sec-websocket-protocol");
            const chosen = proto?.split(",").map((s) => s.trim()).find(Boolean);

            const ok = server.upgrade(req, {
                data: { token, podIp, upstream: null, pending: [] },
                ...(chosen
                    ? { headers: new Headers({ "Sec-WebSocket-Protocol": chosen }) }
                    : {}),
            });

            return ok ? undefined : new Response("upgrade failed", { status: 400 });
        }

        // Static noVNC ("/" -> vnc.html), traversal-safe.
        const path = url.pathname === "/" ? "/vnc.html" : url.pathname;
        const full = resolve(NOVNC_DIR, "." + path);
        if (!full.startsWith(NOVNC_DIR + "/")) return new Response("no", { status: 403 });
        const file = Bun.file(full);
        if (!(await file.exists())) return new Response("not found", { status: 404 });
        return new Response(file);
    },

    websocket: {
        open(ws) {
            const { podIp, token } = ws.data;
            const upstream = new WebSocket(
                `ws://${podIp}:${WORKER_WS_PORT}/websockify?token=${encodeURIComponent(token)}`,
                ["binary"],
            );
            upstream.binaryType = "arraybuffer";
            ws.data.upstream = upstream;

            upstream.onopen = () => {
                for (const f of ws.data.pending) upstream.send(f);
                ws.data.pending.length = 0;
            };
            upstream.onmessage = (ev) => {
                ws.send(ev.data instanceof ArrayBuffer ? new Uint8Array(ev.data) : ev.data);
            };
            upstream.onclose = () => ws.close();
            upstream.onerror = () => {
                console.error(`[router] upstream ${podIp} unreachable (job ${token})`);
                ws.close(1011, "upstream unavailable");
            };
            console.log(`[router] ${token} -> ${podIp}:${WORKER_WS_PORT}`);
        },
        message(ws, message) {
            const up = ws.data.upstream;
            if (up && up.readyState === WebSocket.OPEN) up.send(message);
            else ws.data.pending.push(message);
        },
        close(ws) {
            ws.data.upstream?.close();
        },
    },
});
process.on("SIGTERM", () => { console.log("[router] SIGTERM — exiting"); process.exit(0); });
process.on("SIGINT", () => process.exit(0));

console.log(`[router] listening on :${server.port} (novnc=${NOVNC_DIR})`);
