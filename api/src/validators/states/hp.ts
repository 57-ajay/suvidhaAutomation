// api/src/validators/states/hp.ts
//
// Himachal Pradesh border-tax validator. Scope: app source + UPI.
//
// Like hr.ts / pb.ts, HP is a NO_SAME_DAY state (duration offset = duration).
// HP differences:
//   - Tax Mode: DAYS / QUARTERLY / YEARLY only (the portal lists no MONTHLY or
//     HALF YEARLY option).
//   - taxUpto is mandatory ONLY for DAYS; for QUARTERLY / YEARLY the portal
//     computes and locks it from Tax From + mode, so any client value is
//     stripped here (empty string tells the worker the portal owns the field).
//   - Defaults: TIPRA entry; HP's checkpost shares the district name, so
//     entryCheckpoint defaults to the district (like HR). RC usually arrives
//     with TEMPORARY PERMIT / NOT APPLICABLE; the worker sets a field only when
//     the RC left it empty.
//   - No Distance field on HP, so none is emitted.

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
    // Standard HP entry; change here if the fleet enters elsewhere.
    entryDistrict: "TIPRA",
    // HP's checkpost shares the district name, so default it to the district.
    entryCheckpoint: "TIPRA",
    // HP's RC permit/service fields; the worker sets one only if the RC left
    // it empty.
    permitType: "TEMPORARY PERMIT",
    permitTypeFallback: "TEMPORARY PERMIT",
    serviceType: "NOT APPLICABLE",
} as const;

export const hpValidator: StateValidator = {
    code: "HP",
    name: "Himachal Pradesh",

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
        // Optional 24h "HH:MM" the worker stamps onto the datetime-local Tax
        // From / Tax Upto instead of "IST now". null = not sent (fine).
        const taxTime = normalizeTaxTime(raw.taxTime);

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
        if (taxTime === undefined) {
            invalid.push({
                field: "taxTime",
                reason: 'expected 24h "HH:MM", e.g. "09:30" or "14:00"',
            });
        }

        // taxUpto only matters in DAYS mode; QUARTERLY / YEARLY are portal-locked.
        // HP is NO_SAME_DAY (datetime-local portal, exact-24h billing): a
        // 1-day permit runs taxFrom HH:MM -> (taxFrom + 1) HH:MM. When the
        // caller sends a duration it is AUTHORITATIVE and overrides any
        // client-precomputed taxUpto, so both fields can never disagree.
        const needsTaxUpto = taxMode === "DAYS";
        if (needsTaxUpto && taxFrom && durationUsable) {
            const computed = addDays(taxFrom, duration);
            if (taxUpto && taxUpto !== computed) {
                console.log(
                    `[hp] taxUpto ${taxUpto} disagrees with duration=` +
                    `${duration} (-> ${computed}); using the duration`,
                );
            }
            taxUpto = computed;
        }
        if (needsTaxUpto && !taxUpto) {
            // A same-day span is invalid on this portal (Tax Upto's min is
            // pinned above Tax From), so with neither field usable the old
            // taxUpto = taxFrom fallback shipped a job the portal rejects.
            missing.push("taxUpto (or duration)");
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
                reason: "Himachal Pradesh supports DAYS, QUARTERLY or YEARLY only",
            });
        }
        if (needsTaxUpto && taxFrom && taxUpto && taxUpto <= taxFrom) {
            // Same-day is invalid too: this datetime-local portal pins Tax
            // Upto's min above Tax From, so an explicit taxUpto == taxFrom
            // would ship a job the portal rejects.
            invalid.push({
                field: "taxUpto",
                reason: "must be after taxFrom (same-day span is invalid here)",
            });
        }
        if (paymentMethod && paymentMethod !== "UPI") {
            invalid.push({
                field: "paymentMethod",
                reason: "only UPI is supported on this pipeline",
            });
        }

        if (missing.length || invalid.length) {
            return fail("Himachal Pradesh", missing, invalid);
        }

        return ok({
            state: "HIMACHAL PRADESH",
            stateCode: "HP",
            paymentMethod: "UPI",
            requestId: requestId!,
            driverId: driverId!,
            mobileNumber: str(raw.mobileNumber) ?? DEFAULTS.mobileNumber,
            vehicleNumber: normalizeVehicleNumber(vehicleNumber!),
            taxMode: taxMode!,
            taxFrom: taxFrom!,
            // Stripped for QUARTERLY / YEARLY: empty tells the worker the portal
            // auto-fills (and locks) this field.
            taxUpto: needsTaxUpto ? taxUpto! : "",
            // Optional; shipped only when the caller sent a valid time. The
            // worker stamps it on BOTH ends (a same-day past time is clamped
            // to now at fill — see worker tax_dates.resolve_hhmm).
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
            // No distance field on HP.
        });
    },
};
