// Per-state request validation, run at /api/run BEFORE anything touches Redis
// or a browser slot. The worker assumes every field it needs is present and
// well-formed; this layer is where that guarantee is made. A failure returns a
// structured 400 listing exactly what is missing or invalid so the client can
// tell the driver precisely what to fix.

export interface FieldError {
    field: string;
    reason: string;
}

export interface ValidationResult {
    ok: boolean;
    missing: string[];
    invalid: FieldError[];
    // Normalized params the worker runs with (defaults applied, dates
    // normalized, payment method pinned). Only meaningful when ok === true.
    params: Record<string, string>;
    message: string;
}

export interface StateValidator {
    code: string; // "UP"
    name: string; // "Uttar Pradesh"
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

export function str(v: unknown): string | undefined {
    if (typeof v === "string") {
        const t = v.trim();
        return t.length ? t : undefined;
    }
    if (typeof v === "number") return String(v);
    return undefined;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}/;

/** Accepts ISO (2026-06-30…) or dd/Mon/yyyy (30/Jun/2026); returns yyyy-mm-dd. */
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

/** Optional 24h "HH:MM" for the datetime-local states (PB/HR/HP): the time
 * the worker stamps onto Tax From / Tax Upto instead of "IST now". Accepts
 * "H:MM" / "HH:MM" (a ":SS" tail is tolerated and dropped); returns
 * zero-padded "HH:MM". Absent/blank -> null (worker stamps IST now at fill
 * time). Malformed -> undefined so the caller emits a FieldError. ONE time
 * for BOTH ends — the worker reuses it so the From->Upto span stays an exact
 * 24h multiple (a minute over a whole day bills a full extra day). Date
 * states (UP/MP) simply never read it, so a client-sent value is stripped. */
export function normalizeTaxTime(v: unknown): string | null | undefined {
    const s = str(v);
    if (!s) return null;
    const m = /^(\d{1,2}):(\d{2})(?::\d{2})?$/.exec(s);
    if (!m) return undefined;
    if (Number(m[1]) > 23 || Number(m[2]) > 59) return undefined;
    return `${String(Number(m[1])).padStart(2, "0")}:${m[2]}`;
}

const VEHICLE_RE = /^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{1,4}$/;

export function normalizeVehicleNumber(s: string): string {
    return s.toUpperCase().replace(/[\s-]/g, "");
}

export function isVehicleNumber(s: string): boolean {
    return VEHICLE_RE.test(normalizeVehicleNumber(s));
}
