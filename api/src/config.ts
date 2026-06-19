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

    qrValidUpto: 2 * 60 + 50, // 170 seconds,
    qrValidityDays: int("QR_VALIDITY_DAYS", 7),
    captchaUrlTtlSeconds: int("CAPTCHA_URL_TTL_SECONDS", 600),

    // Shared secret for /api/internal/*. Empty = no check (private network).
    internalApiKey: process.env.INTERNAL_API_KEY ?? "",
} as const;


// The SBIePay UPI QR's real lifetime differs by state; the client uses the
// saved `expiredAt` for its countdown, so this must track the live QR. Keyed
// by stateCode (e.g. "MP"); unlisted states fall back to config.qrValidUpto.
const QR_VALID_SECS_BY_STATE: Record<string, number> = {
    MP: 4 * 60 + 50, // 290s — MP QR valid ~5 min; 4:50 leaves a small buffer
    // UP / HR / PB: add when their real SBIePay-Lite QR lifetime is confirmed
};

export function qrValidSecsForState(stateCode?: string): number {
    const code = (stateCode ?? "").trim().toUpperCase();
    return QR_VALID_SECS_BY_STATE[code] ?? config.qrValidUpto;
}

export function requestDocPath(requestId: string, driverId: string): string {
    return config.requestDocPathTemplate
        .replace("{requestId}", requestId)
        .replace("{driverId}", driverId);
}
