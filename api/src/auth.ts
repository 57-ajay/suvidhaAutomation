// Session auth for the ops console + the unified authorization check.
// One internal account (DASHBOARD_USER / DASHBOARD_PASS, server-side in the
// secret), stateless HMAC-signed session cookie (SESSION_SECRET). Machines
// keep using X-Internal-Key (PUBLIC_API_KEY); humans log in. Rotating
// SESSION_SECRET invalidates every session — that's the logout-everyone lever.

import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { config } from "./config";
import { json } from "./lib/http";

const COOKIE = "dash_session";
const SESSION_TTL_SECS = 7 * 24 * 3600;

if (config.dashboardUser && !config.sessionSecret) {
    console.warn("[auth] DASHBOARD_USER set but SESSION_SECRET empty — set it!");
}

// Constant-time string compare (hash both sides so lengths always match).
const eq = (a: string, b: string) =>
    timingSafeEqual(
        createHash("sha256").update(a).digest(),
        createHash("sha256").update(b).digest(),
    );

const sign = (payload: string) =>
    createHmac("sha256", config.sessionSecret).update(payload).digest("hex");

function makeToken(): string {
    const exp = Math.floor(Date.now() / 1000) + SESSION_TTL_SECS;
    return `${exp}.${sign(String(exp))}`;
}

function tokenValid(token: string): boolean {
    const [expS, sig] = token.split(".");
    if (!expS || !sig) return false;
    const exp = parseInt(expS, 10);
    if (!Number.isFinite(exp) || exp < Date.now() / 1000) return false;
    return eq(sig, sign(expS));
}

export function hasSession(req: Request): boolean {
    const cookie = req.headers.get("cookie") ?? "";
    const m = cookie.match(new RegExp(`(?:^|;\\s*)${COOKIE}=([^;]+)`));
    return !!m && !!config.sessionSecret && tokenValid(m[1]!);
}

// Session OR key. No-op only when NEITHER guard is configured (back-compat
// for private deployments).
export function authorized(req: Request): boolean {
    const guardActive = !!(config.publicApiKey || config.dashboardUser);
    if (!guardActive) return true;
    if (
        config.publicApiKey &&
        req.headers.get("X-Internal-Key") === config.publicApiKey
    )
        return true;
    return hasSession(req);
}

export async function handleLogin(req: Request): Promise<Response> {
    const b: any = await req.json().catch(() => ({}));
    const ok =
        !!config.dashboardUser &&
        !!config.dashboardPass &&
        eq(String(b.username ?? ""), config.dashboardUser) &&
        eq(String(b.password ?? ""), config.dashboardPass);
    if (!ok) {
        await new Promise((r) => setTimeout(r, 350)); // blunt brute force
        return json({ ok: false, error: "invalid credentials" }, 401);
    }
    return new Response(JSON.stringify({ ok: true }), {
        headers: {
            "Content-Type": "application/json",
            "Set-Cookie":
                `${COOKIE}=${makeToken()}; HttpOnly; Secure; SameSite=Lax; ` +
                `Path=/api; Max-Age=${SESSION_TTL_SECS}`,
        },
    });
}

export function handleLogout(): Response {
    return new Response(JSON.stringify({ ok: true }), {
        headers: {
            "Content-Type": "application/json",
            "Set-Cookie": `${COOKIE}=; HttpOnly; Secure; SameSite=Lax; Path=/api; Max-Age=0`,
        },
    });
}

export const LOGIN_HTML = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Console — sign in</title><style>
body{margin:0;height:100vh;display:grid;place-items:center;background:#0b0d12;
color:#d6d9e0;font-family:ui-monospace,Menlo,Consolas,monospace}
.card{background:#12151d;border:1px solid #232838;border-radius:10px;
padding:28px 30px;width:320px}
h1{font-size:14px;letter-spacing:.18em;margin:0 0 18px;color:#e8b34b}
label{display:block;font-size:11px;color:#8a90a3;margin:12px 0 4px;
text-transform:uppercase;letter-spacing:.08em}
input{width:100%;box-sizing:border-box;background:#0b0d12;border:1px solid #2a3042;
color:#d6d9e0;border-radius:6px;padding:9px 10px;font:inherit}
button{margin-top:18px;width:100%;background:#e8b34b;border:0;border-radius:6px;
padding:10px;font:inherit;font-weight:600;color:#151515;cursor:pointer}
.err{color:#ff7a7a;font-size:12px;min-height:16px;margin-top:10px}
</style></head><body><form class="card" id="f"><h1>AGENT CONSOLE</h1>
<label>id</label><input id="u" autocomplete="username">
<label>password</label><input id="p" type="password" autocomplete="current-password">
<button>Sign in</button><div class="err" id="e"></div></form>
<script>
document.getElementById('f').addEventListener('submit', async function (ev) {
  ev.preventDefault();
  var r = await fetch('/api/login', { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username: document.getElementById('u').value,
                           password: document.getElementById('p').value }) });
  if (r.ok) { location.reload(); }
  else { document.getElementById('e').textContent = 'invalid credentials'; }
});
</script></body></html>`;
