// api/src/validators/states/mp.ts
//
// Madhya Pradesh border-tax validator. Scope: app source + UPI.
//
// Mirrors up.ts almost exactly — MP, like UP, is a SAME_DAY state (duration
// offset = duration - 1) and uses plain type="date" inputs. MP differences:
//   - Tax Mode: DAYS ONLY (the portal's dropdown has a single option), so
//     VALID_TAX_MODES is just {DAYS} and taxUpto is always required.
//   - Defaults: SHEOPUR entry; entryCheckpoint "" so the worker takes the
//     district's first checkpost (MP's checkpost names — KARAHAL/NAHAR/... —
//     don't match the district); TEMPORARY PERMIT (MP's portal also lists a
//     typo'd "SEPECIAL PERMIT" we never select — selecting by text avoids
//     it); Air Conditioned Service.
//   - No Distance field on MP, so none is emitted.

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

const VALID_TAX_MODES = new Set(["DAYS"]);

function addDays(date: string, duration: number): string {
    const [year, month, day] = date.split("-").map(Number);
    const d = new Date(Date.UTC(year, month - 1, day));
    d.setUTCDate(d.getUTCDate() + duration);
    return d.toISOString().slice(0, 10);
}

const DEFAULTS = {
    mobileNumber: "",
    // Standard MP entry; change here if the fleet enters elsewhere.
    entryDistrict: "SHEOPUR",
    // Empty -> worker picks the district's first available checkpost.
    entryCheckpoint: "",
    // MP's RC permit fields; the worker sets one only when the RC left it empty.
    permitType: "TEMPORARY PERMIT",
    permitTypeFallback: "TEMPORARY PERMIT",
    serviceType: "Air Conditioned Service",
} as const;

export const mpValidator: StateValidator = {
    code: "MP",
    name: "Madhya Pradesh",

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
            } else {
                missing.push("taxFrom");
            }
        }

        // MP is DAYS-only, so taxUpto is always required.
        // MP is SAME_DAY (plain type="date" portal): a 1-day permit runs
        // taxFrom -> taxFrom, N days -> taxFrom + (N-1). When the caller
        // sends a duration it is AUTHORITATIVE: clients have shipped taxUpto
        // precomputed as taxFrom + duration (the datetime states'
        // convention), which bills one extra day here — so a duration-derived
        // value overrides any client-sent taxUpto.
        const needsTaxUpto = taxMode === "DAYS";
        if (needsTaxUpto && taxFrom && durationUsable) {
            const computed = addDays(taxFrom, duration - 1);
            if (taxUpto && taxUpto !== computed) {
                console.log(
                    `[mp] taxUpto ${taxUpto} disagrees with duration=` +
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
                reason: "Madhya Pradesh supports DAYS only",
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
            return fail("Madhya Pradesh", missing, invalid);
        }

        return ok({
            state: "MADHYA PRADESH",
            stateCode: "MP",
            paymentMethod: "UPI",
            requestId: requestId!,
            driverId: driverId!,
            mobileNumber: str(raw.mobileNumber) ?? DEFAULTS.mobileNumber,
            vehicleNumber: normalizeVehicleNumber(vehicleNumber!),
            taxMode: taxMode!,
            taxFrom: taxFrom!,
            taxUpto: taxUpto!,
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
            // No distance field on MP.
        });
    },
};
