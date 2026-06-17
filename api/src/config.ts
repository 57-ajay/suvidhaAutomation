// All API configuration, read once from the environment.

function int(name: string, fallback: number): number {
    const v = parseInt(process.env[name] ?? "", 10);
    return Number.isFinite(v) ? v : fallback;
}

export const config = {
    port: int("PORT", 3000),
    pipelineTag: process.env.PIPELINE_TAG ?? "border-tax",

    redisUrl: process.env.REDIS_URL ?? "redis://localhost:6379",

    // Firestore document that holds one border-tax request.
    // {requestId} and {driverId} are substituted at write time.
    requestDocPathTemplate:
        process.env.REQUEST_DOC_PATH_TEMPLATE ??
        "driverUtilitiesRequests/data/borderTaxRequests/{requestId}",

    gcsBucket: process.env.GCS_BUCKET ?? "",
    gcsPrefix:
        process.env.GCS_PREFIX ?? "driverUtilitiesRequests/borderTaxRequests",

    qrValidUpto: 2 * 60 + 50, // 170 seconds
    qrValidityDays: int("QR_VALIDITY_DAYS", 7),
    captchaUrlTtlSeconds: int("CAPTCHA_URL_TTL_SECONDS", 600),

    // Shared secret for /api/internal/*. Empty = no check (private network).
    internalApiKey: process.env.INTERNAL_API_KEY ?? "",
} as const;

export function requestDocPath(requestId: string, driverId: string): string {
    return config.requestDocPathTemplate
        .replace("{requestId}", requestId)
        .replace("{driverId}", driverId);
}
