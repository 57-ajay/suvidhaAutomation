// api/src/validators/types.ts
//
// Per-state request validation. Each border-tax state registers a validator
// that runs at /api/run BEFORE anything is written to Redis. The worker assumes
// every field it needs is present and well-formed; this is where we guarantee
// that. A failure returns a structured 400 listing exactly what is missing or
// invalid, so the app can tell the driver precisely what to fix.

export interface FieldError {
    field: string;
    reason: string;
}

export interface ValidationResult {
    ok: boolean;
    missing: string[];
    invalid: FieldError[];
    // The normalized params the worker should run with (defaults applied,
    // payment method pinned, dates normalized). Only meaningful when ok === true.
    params: Record<string, string>;
    message: string;
}

export interface StateValidator {
    /** 2-letter state code, e.g. "UP". */
    code: string;
    /** Human name used in error messages, e.g. "Uttar Pradesh". */
    name: string;
    validate(raw: Record<string, unknown>): ValidationResult;
}

export function ok(params: Record<string, string>): ValidationResult {
    return { ok: true, missing: [], invalid: [], params, message: "" };
}

export function fail(
    stateName: string,
    missing: string[],
    invalid: FieldError[],
): ValidationResult {
    const parts: string[] = [];
    if (missing.length) parts.push(`missing: ${missing.join(", ")}`);
    if (invalid.length)
        parts.push(
            `invalid: ${invalid.map((i) => `${i.field} (${i.reason})`).join(", ")}`,
        );
    return {
        ok: false,
        missing,
        invalid,
        params: {},
        message: `Cannot start ${stateName} border tax — ${parts.join("; ")}.`,
    };
}

// ── Small shared helpers ────────────────────────────────────────────────

export function str(v: unknown): string | undefined {
    if (typeof v === "string") {
        const t = v.trim();
        return t.length ? t : undefined;
    }
    if (typeof v === "number") return String(v);
    return undefined;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}/;

/** Accepts ISO (2026-06-30...) or dd/Mon/yyyy (30/Jun/2026) and returns ISO yyyy-mm-dd. */
export function normalizeDate(v: unknown): string | undefined {
    const s = str(v);
    if (!s) return undefined;
    if (ISO_DATE.test(s)) return s.slice(0, 10);
    const m = s.match(/^(\d{1,2})[/\-]([A-Za-z]{3})[/\-](\d{4})$/);
    if (m) {
        const months: Record<string, string> = {
            jan: "01", feb: "02", mar: "03", apr: "04", may: "05", jun: "06",
            jul: "07", aug: "08", sep: "09", oct: "10", nov: "11", dec: "12",
        };
        const mm = months[m[2]!.toLowerCase()];
        if (mm) return `${m[3]}-${mm}-${m[1]!.padStart(2, "0")}`;
    }
    return undefined;
}

const VEHICLE_RE = /^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{1,4}$/;

export function isVehicleNumber(s: string): boolean {
    return VEHICLE_RE.test(s.toUpperCase().replace(/[\s-]/g, ""));
}
