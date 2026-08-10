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
        // UK is SAME_DAY (date-only portal): a 1-day permit runs taxFrom ->
        // taxFrom, N days -> taxFrom + (N-1). When the caller sends a
        // duration it is AUTHORITATIVE: clients have shipped taxUpto
        // precomputed as taxFrom + duration (the datetime states'
        // convention), which bills one extra day here — so a duration-derived
        // value overrides any client-sent taxUpto.
        const needsTaxUpto = taxMode === "DAYS";
        if (needsTaxUpto && taxFrom && durationUsable) {
            const computed = addDays(taxFrom, duration - 1);
            if (taxUpto && taxUpto !== computed) {
                console.log(
                    `[uk] taxUpto ${taxUpto} disagrees with duration=` +
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
