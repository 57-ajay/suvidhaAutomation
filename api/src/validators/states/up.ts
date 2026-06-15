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
        const taxMode = str(raw.taxMode)?.toUpperCase();
        const taxFrom = normalizeDate(raw.taxFrom);
        const taxUpto = normalizeDate(raw.taxUpto);
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
        const needsTaxUpto = taxMode === "DAYS";
        if (needsTaxUpto && !taxUpto) {
            if (str(raw.taxUpto)) {
                invalid.push({
                    field: "taxUpto",
                    reason: "expected YYYY-MM-DD or dd/Mon/yyyy",
                });
            } else missing.push("taxUpto");
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
