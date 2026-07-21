// api/src/config.ts
//
// All environment configuration in one place. See .env.example for docs.

function env(key: string, fallback?: string): string {
    const v = process.env[key];
    if (v === undefined || v === "") {
        if (fallback !== undefined) return fallback;
        throw new Error(`missing required env var: ${key}`);
    }
    return v;
}

export const config = {
    port: parseInt(env("PORT", "3000"), 10),
    redisUrl: env("REDIS_URL", "redis://localhost:6379"),
    jobTtlSeconds: 60 * 60 * 24, // 24h

    // Firestore document path template for a request. CONFIRM THIS against your
    // real schema. The DS shared shows `driverUtilities/bordertaxRequests/{requestId}`
    // which is a 3-segment (collection) path — a document needs an even number of
    // segments, so there is most likely a driver segment. Supported placeholders:
    // {driverId}, {requestId}.
    requestDocPathTemplate: env(
        "REQUEST_DOC_PATH_TEMPLATE",
        "driverUtilities/{driverId}/bordertaxRequests/{requestId}",
    ),

    // GCS bucket for QR / captcha / receipt images.
    gcsBucket: env("GCS_BUCKET", ""),
    // Folder prefix inside the bucket.
    gcsPrefix: env("GCS_PREFIX", "driverUtilitiesRequests/borderTaxRequests"),
    // Signed-URL lifetime for short-lived assets (captcha) in seconds.
    captchaUrlTtlSeconds: parseInt(env("CAPTCHA_URL_TTL_SECONDS", "180"), 10),
    // QR validity window in days (matches the app's qrCode.expiredAt).
    qrValidityDays: parseInt(env("QR_VALIDITY_DAYS", "7"), 10),

    pipelineTag: env("PIPELINE_TAG", "pivot-a"),
};

/** Resolve the Firestore doc path for a request. */
export function requestDocPath(requestId: string, driverId: string): string {
    return config.requestDocPathTemplate
        .replaceAll("{requestId}", requestId)
        .replaceAll("{driverId}", driverId);
}
