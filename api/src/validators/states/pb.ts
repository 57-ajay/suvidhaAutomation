// api/src/validators/states/pb.ts
//
// Punjab border-tax validator. Scope: app source + UPI.
//
// Like hr.ts, PB is a NO_SAME_DAY state (duration offset = duration). PB
// differences:
//   - Tax Mode: DAYS or QUARTERLY only (the portal lists no other option).
//   - taxUpto is mandatory ONLY for DAYS; for QUARTERLY the portal computes
//     and locks it, so any client value is stripped here.
//   - Defaults: MOHALI entry; entryCheckpoint "" so the worker takes the
//     district's first checkpost (PB's compound checkpost names — KHARAR,
//     ... — don't match the district); NOT APPLICABLE permit/service.
//   - No Distance field on PB, so none is emitted.

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

const VALID_TAX_MODES = new Set(["DAYS", "QUARTERLY"]);

function addDays(date: string, duration: number): string {
    const [year, month, day] = date.split("-").map(Number);
    const d = new Date(Date.UTC(year, month - 1, day));
    d.setUTCDate(d.getUTCDate() + duration);
    return d.toISOString().slice(0, 10);
}

const DEFAULTS = {
    mobileNumber: "",
    // Standard PB entry; change here if the fleet enters elsewhere.
    entryDistrict: "MOHALI",
    // Empty -> worker picks the district's first available checkpost.
    entryCheckpoint: "",
    // PB's RC permit/service fields; the worker sets one only if the RC left
    // it empty.
    permitType: "NOT APPLICABLE",
    permitTypeFallback: "NOT APPLICABLE",
    serviceType: "NOT APPLICABLE",
} as const;

export const pbValidator: StateValidator = {
    code: "PB",
    name: "Punjab",

    validate(raw: Record<string, unknown>): ValidationResult {
        const missing: string[] = [];
        const invalid: FieldError[] = [];

        const vehicleNumber = str(raw.vehicleNumber);
        const duration = Number(raw.duration) || 0;
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

        // taxUpto only matters in DAYS mode; QUARTERLY is portal-locked.
        const needsTaxUpto = taxMode === "DAYS";
        if (needsTaxUpto && !taxUpto && duration >= 1) {
            // PB is NO_SAME_DAY: duration=1 -> taxFrom + 1.
            taxUpto = addDays(taxFrom!, duration);
        }
        if (!taxUpto) {
            taxUpto = taxFrom;
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
                reason: "Punjab supports DAYS or QUARTERLY only",
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
            return fail("Punjab", missing, invalid);
        }

        return ok({
            state: "PUNJAB",
            stateCode: "PB",
            paymentMethod: "UPI",
            requestId: requestId!,
            driverId: driverId!,
            mobileNumber: str(raw.mobileNumber) ?? DEFAULTS.mobileNumber,
            vehicleNumber: normalizeVehicleNumber(vehicleNumber!),
            taxMode: taxMode!,
            taxFrom: taxFrom!,
            // Stripped for QUARTERLY: empty tells the worker the portal auto-fills
            // (and locks) this field.
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
            // No distance field on PB.
        });
    },
};
