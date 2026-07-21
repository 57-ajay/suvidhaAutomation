// api/src/validators/states/br.ts
//
// Bihar border-tax validator. Scope: SCRIPTED path only (web source, human
// handover) — BR is not in the fully-automated set, so run.ts rejects
// path=fullyAutomated for it.
//
// Ported knowledge (old repo br.py / defaults / dates):
//   - Tax Mode: DAYS / QUARTERLY / YEARLY.
//   - BR is a NO_SAME_DAY state (duration offset = duration): duration=1 ->
//     taxUpto = taxFrom + 1.
//   - taxUpto is mandatory ONLY for DAYS; QUARTERLY / YEARLY are computed and
//     locked by the portal (empty string tells the worker to read it back).
//   - Defaults: PATNA entry. BR's checkpost names do NOT match districts
//     (place names or "NOT APPLICABLE"), so entryCheckpoint defaults to "" —
//     the worker falls to the FIRST option (old "first_option" strategy).
//     Permit TEMPORARY PERMIT; Service Type NOT APPLICABLE.
//   - The old repo's own sources disagree on BR's Tax From/Upto input type
//     (date vs datetime-local) — the worker's runtime detection decides on
//     the live field, so taxTime is accepted optionally here.
//   - paymentMethod is NOT pinned (operator picks at the portal). Optional,
//     pass-through uppercase, default NETBANKING.
//   - No Distance field on BR, so none is emitted.

import {
    type StateValidator,
    type ValidationResult,
    type FieldError,
    ok,
    fail,
    str,
    normalizeDate,
    normalizeTaxTime,
    isVehicleNumber,
    normalizeVehicleNumber,
} from "../types";

const VALID_TAX_MODES = new Set(["DAYS", "QUARTERLY", "YEARLY"]);

function addDays(date: string, duration: number): string {
    const [year, month, day] = date.split("-").map(Number);
    const d = new Date(Date.UTC(year, month - 1, day));
    d.setUTCDate(d.getUTCDate() + duration);
    return d.toISOString().slice(0, 10);
}

const DEFAULTS = {
    mobileNumber: "",
    // Standard BR entry; change here if the fleet enters elsewhere.
    entryDistrict: "PATNA",
    // BR checkposts don't match districts — empty means "first option" on the
    // worker.
    entryCheckpoint: "",
    permitType: "TEMPORARY PERMIT",
    permitTypeFallback: "TEMPORARY PERMIT",
    serviceType: "NOT APPLICABLE",
    paymentMethod: "NETBANKING",
} as const;

export const brValidator: StateValidator = {
    code: "BR",
    name: "Bihar",

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
        // Optional 24h "HH:MM"; stamped only if the live field is
        // datetime-local (runtime detection). null = not sent (fine).
        const taxTime = normalizeTaxTime(raw.taxTime);

        if (!vehicleNumber) missing.push("vehicleNumber");
        if (!taxMode) missing.push("taxMode");
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
        if (taxTime === undefined) {
            invalid.push({
                field: "taxTime",
                reason: 'expected 24h "HH:MM", e.g. "09:30" or "14:00"',
            });
        }

        // taxUpto only matters in DAYS mode; QUARTERLY / YEARLY are
        // portal-locked.
        const needsTaxUpto = taxMode === "DAYS";
        if (needsTaxUpto && !taxUpto && duration >= 1) {
            // BR is NO_SAME_DAY: duration=1 -> taxFrom + 1.
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
                reason: "Bihar supports DAYS, QUARTERLY or YEARLY only",
            });
        }
        if (needsTaxUpto && taxFrom && taxUpto && taxUpto < taxFrom) {
            invalid.push({
                field: "taxUpto",
                reason: "must be on or after taxFrom",
            });
        }

        if (missing.length || invalid.length) {
            return fail("Bihar", missing, invalid);
        }

        return ok({
            state: "BIHAR",
            stateCode: "BR",
            // Informational on the scripted path — the operator picks the real
            // method at the portal.
            paymentMethod: paymentMethod || DEFAULTS.paymentMethod,
            requestId: requestId!,
            driverId: driverId!,
            mobileNumber: str(raw.mobileNumber) ?? DEFAULTS.mobileNumber,
            vehicleNumber: normalizeVehicleNumber(vehicleNumber!),
            taxMode: taxMode!,
            taxFrom: taxFrom!,
            // Stripped for QUARTERLY / YEARLY: empty tells the worker the
            // portal auto-fills (and locks) this field.
            taxUpto: needsTaxUpto ? taxUpto! : "",
            ...(taxTime ? { taxTime } : {}),
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
            // No distance field on BR.
        });
    },
};
