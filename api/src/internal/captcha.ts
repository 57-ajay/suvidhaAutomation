// The user-solved captcha, as the client sees it through Firestore.
//
// aiAgentData.captcha is the full client contract:
//   url           signed image URL (fresh filename per attempt — no caching)
//   attempt       1-based attempt counter
//   maxAttempts   so the UI can render "attempt 2 of 3"
//   lastResult    "awaiting_input" -> show the image and an input box
//                 "rejected"       -> show "wrong code"; a new image follows
//                 "accepted"       -> hide the captcha UI
//   uploadedAt / inputDeadline / resultAt
//
// Flow: worker captures the canvas -> save-captcha (status captchaSolving,
// lastResult awaiting_input) -> user answers via /api/jobs/:id/intervene ->
// worker submits to the portal -> captcha-result writes the verdict. On
// "rejected" the worker refreshes, recaptures, and calls save-captcha again
// with attempt+1. Exhaustion ends the run as `cancelled` (no money has moved).
// stage:"pending" flips
// pendingTransactionCaptcha instead of captchaSolving

import { uploadBase64 } from "../lib/gcs";
import { config, gcsPrefixForTask } from "../config";
import { patchAiAgentData, setAgentStatus, ts } from "./requestDoc";
import { STATUS } from "../lifecycle/statuses";

export interface SaveCaptchaInput {
    requestId: string;
    driverId: string;
    imageBase64: string;
    attempt: number; // 1-based
    maxAttempts?: number;
    waitSeconds?: number; // how long the worker will wait for the answer
    stage?: "disclaimer" | "pending"; // which page -> which captcha status
    task?: string;
}

export async function handleSaveCaptcha(input: SaveCaptchaInput) {
    const { requestId, driverId, attempt } = input;
    if (!requestId || !driverId) {
        return { ok: false, error: "requestId and driverId required" };
    }
    if (!input.imageBase64) {
        return { ok: false, error: "imageBase64 required" };
    }

    const up = await uploadBase64({
        base64: input.imageBase64,
        destination: `${requestId}_${driverId}/captcha_${attempt}.png`,
        signedUrlTtlSeconds: config.captchaUrlTtlSeconds,
        prefix: gcsPrefixForTask(input.task),
    });

    const deadline = input.waitSeconds
        ? ts(new Date(Date.now() + input.waitSeconds * 1000))
        : null;

    await patchAiAgentData(requestId, driverId, {
        captcha: {
            url: up.url,
            attempt,
            maxAttempts: input.maxAttempts ?? null,
            lastResult: "awaiting_input",
            uploadedAt: ts(),
            inputDeadline: deadline,
            resultAt: null,
        },
    }, input.task);

    const to_ =
        input.stage === "pending"
            ? STATUS.PENDING_TRANSACTION_CAPTCHA
            : STATUS.CAPTCHA_SOLVING;
    // Self-transition on captchaSolving is legal, so repeat attempts are fine.
    await setAgentStatus({ requestId, driverId, to: to_, task: input.task });

    return { ok: true, url: up.url, attempt };
}

export interface CaptchaResultInput {
    requestId: string;
    driverId: string;
    result: "accepted" | "rejected";
    attempt: number;
    task?: string;
}

export async function handleCaptchaResult(input: CaptchaResultInput) {
    const { requestId, driverId, result, attempt } = input;
    if (!requestId || !driverId) {
        return { ok: false, error: "requestId and driverId required" };
    }
    if (result !== "accepted" && result !== "rejected") {
        return { ok: false, error: "result must be accepted|rejected" };
    }

    await patchAiAgentData(requestId, driverId, {
        captcha: { lastResult: result, attempt, resultAt: ts() },
    }, input.task);

    return { ok: true };
}
