// api/src/validators/states/tn.ts
//
// Tamil Nadu border-tax validator. Scope: SCRIPTED path only (web source,
// human handover) — TN is not in the fully-automated set, so run.ts rejects
// path=fullyAutomated for it.
//
// Ported knowledge (old repo tn.py / defaults / dates):
//   - Tax Mode: WEEKLY / MONTHLY / QUARTERLY — TN has NO DAYS option.
//   - taxUpto is ALWAYS portal-derived on TN (e.g. WEEKLY -> Tax From + 6
//     days), so any client value is stripped for every mode (empty string
//     tells the worker the portal owns the field). `duration` is therefore
//     ignored here.
//   - TN was in the old NO_SAME_DAY set; moot in practice since we never
//     compute taxUpto for TN, noted for the record.
//   - Defaults: KRISHNAGIRI entry. ⚠️ GUESS carried over from the old repo —
//     TN's district dropdown was never captured; KRISHNAGIRI (Hosur border,
//     the high-traffic Karnataka->TN cab entry) is a placeholder. A miss falls
//     back to the first district on the worker. Confirm on the live portal and
//     update this default. Checkposts don't track the district either, so
//     entryCheckpoint defaults to "" (worker first-option strategy).
//   - Permit ALL INDIA TOURIST PERMIT with CONTRACT CARRIAGE PERMIT fallback —
//     this drives the Permit Fee line on TN's tax table; change the default if
//     the fleet runs on contract-carriage permits. Service Type NOT
//     APPLICABLE.
//   - paymentMethod is NOT pinned (operator picks at the portal). Optional,
//     pass-through uppercase, default UPI.
//   - No Distance field on TN, so none is emitted.

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

const VALID_TAX_MODES = new Set(["WEEKLY", "MONTHLY", "QUARTERLY"]);

const DEFAULTS = {
    mobileNumber: "",
    // ⚠️ Placeholder from the old repo (see header) — confirm on the live
    // portal.
    entryDistrict: "KRISHNAGIRI",
    // TN checkposts don't track the district — empty means "first option" on
    // the worker.
    entryCheckpoint: "",
    permitType: "ALL INDIA TOURIST PERMIT",
    permitTypeFallback: "CONTRACT CARRIAGE PERMIT",
    serviceType: "NOT APPLICABLE",
    paymentMethod: "UPI",
} as const;

export const tnValidator: StateValidator = {
    code: "TN",
    name: "Tamil Nadu",

    validate(raw: Record<string, unknown>): ValidationResult {
        const missing: string[] = [];
        const invalid: FieldError[] = [];

        const vehicleNumber = str(raw.vehicleNumber);
        const taxMode = str(raw.taxMode)?.toUpperCase();
        const taxFrom = normalizeDate(raw.taxFrom);
        const paymentMethod = str(raw.paymentMethod)?.toUpperCase();
        const requestId = str(raw.requestId);
        const driverId = str(raw.driverId);
        // Optional; the live Tax From is plain type="date" per the old repo,
        // but runtime detection decides — accept the field regardless.
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

        if (vehicleNumber && !isVehicleNumber(vehicleNumber)) {
            invalid.push({
                field: "vehicleNumber",
                reason: "not a valid Indian registration number",
            });
        }
        if (taxMode && !VALID_TAX_MODES.has(taxMode)) {
            invalid.push({
                field: "taxMode",
                reason:
                    "Tamil Nadu supports WEEKLY, MONTHLY or QUARTERLY only " +
                    "(there is no DAYS option on the TN portal)",
            });
        }

        if (missing.length || invalid.length) {
            return fail("Tamil Nadu", missing, invalid);
        }

        return ok({
            state: "TAMIL NADU",
            stateCode: "TN",
            // Informational on the scripted path — the operator picks the real
            // method at the portal.
            paymentMethod: paymentMethod || DEFAULTS.paymentMethod,
            requestId: requestId!,
            driverId: driverId!,
            mobileNumber: str(raw.mobileNumber) ?? DEFAULTS.mobileNumber,
            vehicleNumber: normalizeVehicleNumber(vehicleNumber!),
            taxMode: taxMode!,
            taxFrom: taxFrom!,
            // ALWAYS portal-derived on TN: empty tells the worker to only read
            // it back.
            taxUpto: "",
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
            // No distance field on TN.
        });
    },
};
