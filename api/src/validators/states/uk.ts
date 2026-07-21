// api/src/validators/states/uk.ts
//
// Uttarakhand border-tax validator. Scope: SCRIPTED path only (web source,
// human handover) — UK is not in the fully-automated set, so run.ts rejects
// path=fullyAutomated for it.
//
// Ported knowledge (old repo uk.py / defaults / dates):
//   - Tax Mode is RC-DEPENDENT: the portal renders different mode options per
//     vehicle. We accept DAYS / QUARTERLY / YEARLY here; if the RC doesn't get
//     the requested mode, the WORKER aborts with
//     abort_reason="tax_mode_not_offered_for_rc" and the offered list.
//   - UK was never in the old SAME_DAY / NO_SAME_DAY sets — it took the
//     same-day-allowed default (duration offset = duration - 1). Kept.
//   - taxUpto is mandatory ONLY for DAYS; QUARTERLY / YEARLY are computed and
//     locked by the portal (empty string tells the worker to read it back).
//   - Defaults: DEHRADUN entry. UK's checkpost names (ASHARODI / KULHAL /
//     TIMLI / TUNI) do NOT track the district, so entryCheckpoint defaults to
//     "" — the worker's checkpost helper then falls to the FIRST option (the
//     old "first_option" strategy). Service Type "Air Conditioned Service";
//     Permit TEMPORARY PERMIT with ALL INDIA TOURIST PERMIT fallback.
//   - paymentMethod is NOT pinned: the operator picks the method at the portal
//     during the handover (UK's gateway is net-banking only — no UPI/QR).
//     Optional, pass-through uppercase, default NETBANKING.
//   - No Distance field on UK, so none is emitted.

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
    // Standard UK entry (Dehradun border); change here if the fleet enters
    // elsewhere.
    entryDistrict: "DEHRADUN",
    // UK checkposts don't track the district — empty means "first option" on
    // the worker.
    entryCheckpoint: "",
    permitType: "TEMPORARY PERMIT",
    permitTypeFallback: "ALL INDIA TOURIST PERMIT",
    serviceType: "Air Conditioned Service",
    paymentMethod: "NETBANKING",
} as const;

export const ukValidator: StateValidator = {
    code: "UK",
    name: "Uttarakhand",

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
        // Optional 24h "HH:MM"; the worker stamps it only if the live field
        // turns out to be datetime-local (runtime detection). null = not sent.
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
            // UK is same-day-allowed: duration=1 -> taxUpto === taxFrom.
            taxUpto = addDays(taxFrom!, duration - 1);
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
                reason:
                    "Uttarakhand accepts DAYS, QUARTERLY or YEARLY (the " +
                    "portal offers modes per RC; an unoffered mode fails at " +
                    "run time with the offered list)",
            });
        }
        if (needsTaxUpto && taxFrom && taxUpto && taxUpto < taxFrom) {
            invalid.push({
                field: "taxUpto",
                reason: "must be on or after taxFrom",
            });
        }

        if (missing.length || invalid.length) {
            return fail("Uttarakhand", missing, invalid);
        }

        return ok({
            state: "UTTARAKHAND",
            stateCode: "UK",
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
            // No distance field on UK.
        });
    },
};
