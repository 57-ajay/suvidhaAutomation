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

    // PUC-certificate task: its own Firestore collection + GCS prefix. Same
    // {requestId}/{driverId} substitution. Border-tax paths are untouched.
    pucDocPathTemplate:
        process.env.PUC_DOC_PATH_TEMPLATE ??
        "driverUtilitiesRequests/data/pucRequests/{requestId}",
    pucGcsPrefix:
        process.env.PUC_GCS_PREFIX ?? "driverUtilitiesRequests/pucRequests",

    qrValidUpto: 2 * 60 + 50, // 170 seconds,
    qrValidityDays: int("QR_VALIDITY_DAYS", 7),
    captchaUrlTtlSeconds: int("CAPTCHA_URL_TTL_SECONDS", 600),

    // Shared secret for /api/internal/*. Empty = no check (private network).
    internalApiKey: process.env.INTERNAL_API_KEY ?? "",

    // Opt-in guard for the PUBLIC mutating routes (/api/run, /api/verify-pending,
    // /api/jobs/:id/intervene|cancel). Empty = no check (back-compat). Set this on
    // an internet-exposed instance (e.g. the vendor panel's agent) so only callers
    // that send a matching X-Internal-Key — the panel's Cloud Functions — can
    // enqueue work or intervene. GET reads / the ops dashboard are unaffected and
    // should be restricted at the proxy.
    publicApiKey: process.env.PUBLIC_API_KEY ?? "",
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

// Per-task Firestore document templates. Unknown / absent task -> border-tax.
const DOC_PATH_TEMPLATE_BY_TASK: Record<string, string> = {
    "border-tax": config.requestDocPathTemplate,
    "puc-certificate": config.pucDocPathTemplate,
};

export function requestDocPath(
    requestId: string,
    driverId: string,
    task: string = "border-tax",
): string {
    const tmpl = DOC_PATH_TEMPLATE_BY_TASK[task] ?? config.requestDocPathTemplate;
    return tmpl
        .replace("{requestId}", requestId)
        .replace("{driverId}", driverId);
}

// Per-task GCS prefix for uploaded artifacts (captcha / QR / receipt).
const GCS_PREFIX_BY_TASK: Record<string, string> = {
    "border-tax": config.gcsPrefix,
    "puc-certificate": config.pucGcsPrefix,
};

export function gcsPrefixForTask(task: string = "border-tax"): string {
    return GCS_PREFIX_BY_TASK[task] ?? config.gcsPrefix;
}
