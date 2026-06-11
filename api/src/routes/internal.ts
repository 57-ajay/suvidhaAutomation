// api/src/routes/internal.ts
//
// Internal endpoints the worker calls. These own all Firestore writes. Not
// exposed publicly in prod — keep them behind the worker network / an auth
// header. Pivot-a leaves auth as a TODO (worker and api share a private network).

import { json, readJson } from "../lib/http";
import { handleSaveQR } from "../internal/qr";
import { handleSaveCaptcha } from "../internal/captcha";
import { handleSaveReceipt } from "../internal/receipt";
import { handleJobCompleted } from "../internal/jobCompleted";
import { setAgentStatus } from "../internal/requestDoc";
import { isStatus, type Status } from "../lifecycle/statuses";

export async function handleInternal(
    pathname: string,
    req: Request,
): Promise<Response | null> {
    // POST /api/internal/status-update
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

    // POST /api/internal/save-qr
    if (pathname === "/api/internal/save-qr") {
        return json(await handleSaveQR(await readJson(req)));
    }

    // POST /api/internal/save-captcha
    if (pathname === "/api/internal/save-captcha") {
        return json(await handleSaveCaptcha(await readJson(req)));
    }

    // POST /api/internal/save-receipt
    if (pathname === "/api/internal/save-receipt") {
        return json(await handleSaveReceipt(await readJson(req)));
    }

    // POST /api/internal/job-completed
    if (pathname === "/api/internal/job-completed") {
        return json(await handleJobCompleted(await readJson(req)));
    }

    return null; // not an internal route
}
