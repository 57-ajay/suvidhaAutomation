// api/src/validators/borderTax/up.ts
//
// Uttar Pradesh border-tax validator. Pivot-a scope: app source + UPI only.
//
// Required vs optional was derived from the live UP flow (worker phases 1-10):
//   - vehicleNumber, taxMode, taxFrom, taxUpto, entryDistrict  -> hard required
//   - entryCheckpoint                                          -> optional (the
//        runner picks the option that matches the district when omitted)
//   - permitType / serviceType                                 -> optional, with
//        portal-driven defaults; permitTypeFallback covers RCs that arrive with
//        Permit Type empty (Service Type options are conditional on it)
//   - requestId, driverId, mobileNumber                        -> bookkeeping,
//        required so we can write Firestore + notify the driver

import {
    type StateValidator,
    type ValidationResult,
    type FieldError,
    ok,
    fail,
    str,
    normalizeDate,
    isVehicleNumber,
} from "../types";

const VALID_TAX_MODES = new Set(["DAYS", "MONTHLY", "QUARTERLY", "YEARLY"]);

export const upValidator: StateValidator = {
    code: "UP",
    name: "Uttar Pradesh",

    validate(raw: Record<string, unknown>): ValidationResult {
        const missing: string[] = [];
        const invalid: FieldError[] = [];

        const vehicleNumber = str(raw.vehicleNumber);
        const taxModeRaw = str(raw.taxMode);
        const taxMode = taxModeRaw ? taxModeRaw.toUpperCase() : undefined;
        const taxFrom = normalizeDate(raw.taxFrom);
        const taxUpto = normalizeDate(raw.taxUpto);
        const entryDistrict = str(raw.entryDistrict);
        const requestId = str(raw.requestId);
        const driverId = str(raw.driverId);
        const mobileNumber = str(raw.mobileNumber);

        // ── hard-required presence ──
        if (!vehicleNumber) missing.push("vehicleNumber");
        if (!taxMode) missing.push("taxMode");
        if (!taxFrom) missing.push("taxFrom");
        if (!entryDistrict) missing.push("entryDistrict");
        if (!requestId) missing.push("requestId");
        if (!driverId) missing.push("driverId");
        if (!mobileNumber) missing.push("mobileNumber");

        // taxUpto is required for DAYS mode; for MONTHLY/QUARTERLY/YEARLY the
        // portal derives the end date, but we still pass it through if given.
        if (taxMode === "DAYS" && !taxUpto) missing.push("taxUpto");

        // ── format checks (only on fields that are present) ──
        if (vehicleNumber && !isVehicleNumber(vehicleNumber)) {
            invalid.push({
                field: "vehicleNumber",
                reason: "not a valid registration number",
            });
        }
        if (taxMode && !VALID_TAX_MODES.has(taxMode)) {
            invalid.push({
                field: "taxMode",
                reason: `must be one of ${[...VALID_TAX_MODES].join(", ")}`,
            });
        }
        if (raw.taxFrom && !taxFrom) {
            invalid.push({ field: "taxFrom", reason: "unparseable date" });
        }
        if (raw.taxUpto && !taxUpto) {
            invalid.push({ field: "taxUpto", reason: "unparseable date" });
        }
        if (taxFrom && taxUpto && taxUpto < taxFrom) {
            invalid.push({ field: "taxUpto", reason: "is before taxFrom" });
        }
        if (mobileNumber && !/^(\+?91)?[6-9]\d{9}$/.test(mobileNumber.replace(/\s/g, ""))) {
            invalid.push({ field: "mobileNumber", reason: "not a valid Indian mobile number" });
        }

        // pivot-a only accepts UPI; reject an explicit non-UPI method loudly.
        const pm = str(raw.paymentMethod);
        if (pm && pm.toLowerCase() !== "upi") {
            invalid.push({
                field: "paymentMethod",
                reason: "pivot-a UP supports UPI only",
            });
        }

        if (missing.length || invalid.length) {
            return fail(this.name, missing, invalid);
        }

        // ── normalized params handed to the worker ──
        return ok({
            state: "Uttar Pradesh",
            stateCode: "UP",
            vehicleNumber: vehicleNumber!.toUpperCase().replace(/[\s-]/g, ""),
            taxMode: taxMode!,
            taxFrom: taxFrom!,
            taxUpto: taxUpto ?? "",
            entryDistrict: entryDistrict!.toUpperCase(),
            entryCheckpoint: (str(raw.entryCheckpoint) ?? "").toUpperCase(),
            permitType: str(raw.permitType) ?? "",
            permitTypeFallback: str(raw.permitTypeFallback) ?? "ALL INDIA TOURIST PERMIT",
            serviceType: str(raw.serviceType) ?? "Air Conditioned Service",
            paymentMethod: "upi",
            source: "app",
            requestId: requestId!,
            driverId: driverId!,
            mobileNumber: mobileNumber!,
        });
    },
};
