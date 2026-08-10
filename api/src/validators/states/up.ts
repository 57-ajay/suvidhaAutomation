// Uttar Pradesh border-tax validator. Scope: app source + UPI.
//
// MANDATORY (product spec): source [enforced in routes/run.ts], state
// [resolved by the registry before this runs], requestId, driverId,
// vehicleNumber, taxMode, taxFrom, paymentMethod (must be UPI).
// taxUpto is mandatory ONLY for taxMode=DAYS. For MONTHLY/QUARTERLY/
// YEARLY the portal computes and locks Tax Upto from Tax From + mode, so
// any client-sent value is IGNORED (stripped here) — script-writing it
// would bypass the portal's UI lock and desync the calculated period.
//
// Everything else falls back to DEFAULTS below — one block to edit when the
// standard entry point or portal vocabulary changes. entryCheckpoint stays ""
// so the worker picks the district's first available checkpoint; permitType
// only applies when the RC arrives with Permit Type empty (Service Type
// options are conditional on it), with permitTypeFallback as the second tier.

import {
    type StateValidator,
    type ValidationResult,
    type FieldError,
    ok,
    fail,
    str,
    normalizeDate,
    isVehicleNumber,
    normalizeVehicleNumber,
} from "../types";

const VALID_TAX_MODES = new Set(["DAYS", "MONTHLY", "QUARTERLY", "YEARLY"]);


function addDays(date: string, duration: number): string {
    const [year, month, day] = date.split("-").map(Number);

    const d = new Date(Date.UTC(year, month - 1, day));
    d.setUTCDate(d.getUTCDate() + duration);

    return d.toISOString().slice(0, 10);
}

const DEFAULTS = {
    mobileNumber: "",
    // Standard Delhi-NCR -> UP entry; change here if your fleet enters elsewhere.
    entryDistrict: "GAUTAM BUDDHA NAGAR",
    entryCheckpoint: "",
    permitType: "TEMPORARY PERMIT",
    permitTypeFallback: "ALL INDIA TOURIST PERMIT",
    serviceType: "Air Conditioned Service",
} as const;

export const upValidator: StateValidator = {
    code: "UP",
    name: "Uttar Pradesh",

    validate(raw: Record<string, unknown>): ValidationResult {
        const missing: string[] = [];
        const invalid: FieldError[] = [];

        const vehicleNumber = str(raw.vehicleNumber);
        // Optional day count; sanity-bounded below so a garbage value
        // 400s instead of feeding addDays an Invalid Date (-> 500).
        const durationNum = Number(raw.duration);
        const duration = Math.trunc(durationNum) || 0;
        const durationSent =
            raw.duration != null && String(raw.duration).trim() !== "";
        const durationUsable =
            Number.isFinite(durationNum) && duration >= 1 && duration <= 3650;
        const taxMode = str(raw.taxMode)?.toUpperCase();
        const taxFrom = normalizeDate(raw.taxFrom);
        let taxUpto = normalizeDate(raw.taxUpto);
        const paymentMethod = str(raw.paymentMethod)?.toUpperCase();
        const requestId = str(raw.requestId);
        const driverId = str(raw.driverId);

        if (!vehicleNumber) missing.push("vehicleNumber");
        if (!taxMode) missing.push("taxMode");
        if (!paymentMethod) missing.push("paymentMethod");
        if (!requestId) missing.push("requestId");
        if (!driverId) missing.push("driverId");
        if (!taxFrom) {
            if (str(raw.taxFrom)) {
                invalid.push({
                    field: "taxFrom",
                    reason: "expected YYYY-MM-DD or dd/Mon/yyyy",
                });
            } else missing.push("taxFrom");
        }
        // taxUpto only matters in DAYS mode; otherwise the portal owns it.
        // UP is SAME_DAY (plain type="date" portal, no time component): a
        // 1-day permit runs taxFrom -> taxFrom, N days -> taxFrom + (N-1).
        // When the caller sends a duration it is AUTHORITATIVE: clients have
        // shipped taxUpto precomputed as taxFrom + duration (the datetime
        // states' convention), which bills one extra day here — so a
        // duration-derived value overrides any client-sent taxUpto.
        const needsTaxUpto = taxMode === "DAYS";
        if (needsTaxUpto && taxFrom && durationUsable) {
            const computed = addDays(taxFrom, duration - 1);
            if (taxUpto && taxUpto !== computed) {
                console.log(
                    `[up] taxUpto ${taxUpto} disagrees with duration=` +
                    `${duration} (-> ${computed}); using the duration`,
                );
            }
            taxUpto = computed;
        }
        if (!taxUpto) {
            // No duration and no taxUpto: default to a 1-day (same-day) permit.
            taxUpto = taxFrom;
        }

        if (durationSent && !durationUsable) {
            invalid.push({
                field: "duration",
                reason: "must be an integer between 1 and 3650 (days)",
            });
        }
        if (vehicleNumber && !isVehicleNumber(vehicleNumber)) {
            invalid.push({
                field: "vehicleNumber",
                reason: "not a valid Indian registration number",
            });
        }
        if (taxMode && !VALID_TAX_MODES.has(taxMode)) {
            invalid.push({
                field: "taxMode",
                reason: `must be one of ${[...VALID_TAX_MODES].join("/")}`,
            });
        }
        if (needsTaxUpto && taxFrom && taxUpto && taxUpto < taxFrom) {
            invalid.push({
                field: "taxUpto",
                reason: "must be on or after taxFrom",
            });
        }
        if (paymentMethod && paymentMethod !== "UPI") {
            invalid.push({
                field: "paymentMethod",
                reason: "only UPI is supported on this pipeline",
            });
        }

        if (missing.length || invalid.length) {
            return fail("Uttar Pradesh", missing, invalid);
        }

        return ok({
            state: "UTTAR PRADESH",
            stateCode: "UP",
            paymentMethod: "UPI",
            requestId: requestId!,
            driverId: driverId!,
            mobileNumber: str(raw.mobileNumber) ?? DEFAULTS.mobileNumber,
            vehicleNumber: normalizeVehicleNumber(vehicleNumber!),
            taxMode: taxMode!,
            taxFrom: taxFrom!,
            // Stripped for non-DAYS modes: empty string tells the worker
            // the portal auto-fills this field.
            taxUpto: needsTaxUpto ? taxUpto! : "",
            entryDistrict: (
                str(raw.entryDistrict) ?? DEFAULTS.entryDistrict
            ).toUpperCase(),
            entryCheckpoint: (
                str(raw.entryCheckpoint) ?? DEFAULTS.entryCheckpoint
            ).toUpperCase(),
            permitType: str(raw.permitType) ?? DEFAULTS.permitType,
            permitTypeFallback:
                str(raw.permitTypeFallback) ?? DEFAULTS.permitTypeFallback,
            serviceType: str(raw.serviceType) ?? DEFAULTS.serviceType,
        });
    },
};
