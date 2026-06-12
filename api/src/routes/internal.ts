// Endpoints the worker calls. These own ALL Firestore writes. They live behind
// the private docker network; if INTERNAL_API_KEY is set, the X-Internal-Key
// header is also enforced (set the same value in the worker env).

import { json, readJson } from "../lib/http";
import { config } from "../config";
import { handleSaveQR } from "../internal/qr";
import { handleSaveCaptcha, handleCaptchaResult } from "../internal/captcha";
import { handleSaveReceipt } from "../internal/receipt";
import { handleJobCompleted } from "../internal/jobCompleted";
import { setAgentStatus } from "../internal/requestDoc";
import { isStatus, type Status } from "../lifecycle/statuses";

export async function handleInternal(
    pathname: string,
    req: Request,
): Promise<Response | null> {
    if (
        config.internalApiKey &&
        req.headers.get("X-Internal-Key") !== config.internalApiKey
    ) {
        return json({ ok: false, error: "unauthorized" }, 401);
    }

    if (pathname === "/api/internal/status-update") {
        const b = await readJson(req);
        if (!isStatus(b.status)) {
            return json({ ok: false, error: `unknown status: ${b.status}` }, 400);
        }
        try {
            const written = await setAgentStatus({
                requestId: b.requestId,
                driverId: b.driverId,
                to: b.status as Status,
                source: b.source,
                error: b.error ?? undefined,
                extra: b.extra,
            });
            return json({ ok: true, status: written });
        } catch (e: any) {
            return json({ ok: false, error: e.message }, 409);
        }
    }

    if (pathname === "/api/internal/save-captcha") {
        return json(await handleSaveCaptcha(await readJson(req) as any));
    }
    if (pathname === "/api/internal/captcha-result") {
        return json(await handleCaptchaResult(await readJson(req) as any));
    }
    if (pathname === "/api/internal/save-qr") {
        return json(await handleSaveQR(await readJson(req) as any));
    }
    if (pathname === "/api/internal/save-receipt") {
        return json(await handleSaveReceipt(await readJson(req) as any));
    }
    if (pathname === "/api/internal/job-completed") {
        return json(await handleJobCompleted(await readJson(req) as any));
    }

    return null;
}
