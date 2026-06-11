// api/src/internal/captcha.ts
//
// Worker calls this each time it needs the user to solve the main captcha:
// once on first show, and again on every refresh after a wrong attempt. The
// `attempt` counter is what tells the app to swap the displayed image. The URL
// is short-lived (CAPTCHA_URL_TTL_SECONDS) because a captcha is only valid for
// the few minutes the portal keeps the page alive.

import { uploadImage } from "../lib/gcs";
import { config } from "../config";
import { FieldValue } from "../firebase";
import { patchAiAgentData, setAgentStatus } from "./requestDoc";
import { STATUS } from "../lifecycle/statuses";

export interface SaveCaptchaInput {
    requestId: string;
    driverId: string;
    imageBase64: string;
    attempt: number; // 1-based
}

export async function handleSaveCaptcha(input: SaveCaptchaInput) {
    const { requestId, driverId, attempt } = input;
    if (!requestId || !driverId) {
        return { ok: false, error: "requestId and driverId required" };
    }

    // A fresh filename per attempt avoids any CDN/browser caching of the old image.
    const dest = `${requestId}_${driverId}/captcha_${attempt}.png`;
    const up = await uploadImage({
        base64: input.imageBase64,
        destination: dest,
        signedUrlTtlSeconds: config.captchaUrlTtlSeconds,
    });

    await patchAiAgentData(requestId, driverId, {
        captcha: {
            url: up.url,
            attempt,
            uploadedAt: FieldValue.serverTimestamp(),
        },
    });

    // setAgentStatus on captchaSolving is idempotent (self-transition allowed),
    // so calling it on every attempt is fine.
    await setAgentStatus({
        requestId,
        driverId,
        to: STATUS.CAPTCHA_SOLVING,
    });

    return { ok: true, url: up.url, attempt };
}
