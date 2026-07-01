// api/src/internal/vehicleDetails.ts
//
// Resolve the RC record for a vehicle so the worker can fill the parivahan
// owner-info + vehicle-info forms manually when VAHAN's "Get Details" returns
// no data ("No data found for this vehicle number. Please enter the details..").
//
// Ported from main's api/src/internal/borderTax/vehicleDetails.ts. The ONLY
// behavioural change vs. main is WHEN it runs: main attached the record to
// every border-tax job at queue time (paying the RC-lookup latency on every
// run); here the worker calls handleVehicleDetails() lazily — only after it
// actually sees the "No data found" popup — so the common path (VAHAN has the
// data) never pays for the lookup.
//
// Resolution order (API-first, DB-as-fallback):
//   1. ALWAYS trigger a live RC lookup via the invincibleOcean cloud function
//      first. That function persists the canonical record to
//      vehicleDetails/{REGNO} server-side and returns a differently-shaped
//      HTTP body. We deliberately IGNORE the body and re-read the persisted
//      doc, so the caller always gets the freshest RC data and byte-for-byte
//      the same record shape the worker's manual fill is known to work with.
//      (Using the raw HTTP body directly was the original bug: it lacks fields
//      the persisted doc carries.)
//   2. If the RC lookup FAILS (HTTP error / timeout / unusable body), fall back
//      to whatever is already cached in vehicleDetails/{REGNO}, even if it's
//      old/stale — better a slightly stale record than nothing. If the DB is
//      also empty, return null.
//
// Never throws — a miss just means the manual-entry fallback has no data and
// the worker fails that run later with a clear, driver-readable message.

import { db } from "../firebase";

const RC_API_URL =
    process.env.RC_API_URL ??
    "https://us-central1-bwi-cabswalle.cloudfunctions.net/invincibleOcean-verifyRcDetailsV2";

// The cloud function can hit slow government RC services; give it room.
const RC_API_TIMEOUT_MS = 30_000;

// After the lookup succeeds, the persisted doc is normally readable
// immediately (single-doc gets are strongly consistent), but we poll a few
// times in case the function's write lands a beat after its HTTP response.
const POST_LOOKUP_READ_ATTEMPTS = 6;
const POST_LOOKUP_READ_DELAY_MS = 500;

/** Normalize a registration number the same way the worker does: strip every
 *  non-alphanumeric char and upper-case. "hr38ae7922" / "HR-38-AE-7922" →
 *  "HR38AE7922". */
export function normalizeRegNo(vehicleNumber: string): string {
    return (vehicleNumber || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

/** Mirror Dart's `DateTime.now().microsecondsSinceEpoch.toString()`. JS has
 *  no microsecond clock, so we scale millis to micros and add a sub-ms random
 *  tail — gives a unique ~16-digit id in the same shape the API expects. */
function makeVerificationId(): string {
    return String(Date.now() * 1000 + Math.floor(Math.random() * 1000));
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Read the cached doc. Returns its data, or null if absent / read fails. */
async function readVehicleDetails(
    regNo: string,
): Promise<Record<string, unknown> | null> {
    try {
        const snap = await db.collection("vehicleDetails").doc(regNo).get();
        return snap.exists ? (snap.data() as Record<string, unknown>) : null;
    } catch (e) {
        console.error(`[vehicleDetails] read failed for ${regNo}:`, e);
        return null;
    }
}

/** Fire the invincibleOcean RC lookup purely to make it persist the record.
 *  Returns true if the lookup succeeded (HTTP ok + success/reg_no in body).
 *  The body itself is intentionally discarded — we read the persisted doc. */
async function triggerRcLookup(regNo: string): Promise<boolean> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), RC_API_TIMEOUT_MS);
    try {
        const res = await fetch(RC_API_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                vehicleNumber: regNo,
                source: "app",
                verificationId: makeVerificationId(),
            }),
            signal: controller.signal,
        });
        if (!res.ok) {
            console.error(`[vehicleDetails] RC API HTTP ${res.status} for ${regNo}`);
            return false;
        }
        const data = (await res.json()) as Record<string, unknown>;
        if (data && (data.success === true || data.reg_no)) {
            console.log(`[vehicleDetails] RC lookup succeeded for ${regNo}`);
            return true;
        }
        console.error(
            `[vehicleDetails] RC API returned no usable data for ${regNo} ` +
            `(success=${data?.success})`,
        );
        return false;
    } catch (e) {
        console.error(`[vehicleDetails] RC API call failed for ${regNo}:`, e);
        return false;
    } finally {
        clearTimeout(timer);
    }
}

/** Resolve the vehicleDetails record: ALWAYS trigger the live RC lookup first
 *  (which refreshes the persisted doc), then read THAT doc back. Only if the
 *  lookup fails do we fall back to the existing — possibly stale — DB record. */
export async function fetchVehicleDetails(
    vehicleNumber: string,
): Promise<Record<string, unknown> | null> {
    const regNo = normalizeRegNo(vehicleNumber);
    if (!regNo) return null;

    // 1. Always hit the live RC lookup first so the cloud function refreshes
    //    vehicleDetails/{regNo} with the latest RC data.
    const apiOk = await triggerRcLookup(regNo);

    // 2. Lookup succeeded — read the freshly-persisted doc. Poll briefly in
    //    case the write lands a beat after the HTTP response.
    if (apiOk) {
        for (let attempt = 1; attempt <= POST_LOOKUP_READ_ATTEMPTS; attempt++) {
            const doc = await readVehicleDetails(regNo);
            if (doc) {
                if (attempt > 1) {
                    console.log(
                        `[vehicleDetails] refreshed doc for ${regNo} appeared on ` +
                        `read attempt ${attempt}`,
                    );
                }
                return doc;
            }
            await sleep(POST_LOOKUP_READ_DELAY_MS);
        }
        console.error(
            `[vehicleDetails] RC lookup for ${regNo} reported success but the ` +
            `persisted doc never appeared after ${POST_LOOKUP_READ_ATTEMPTS} reads ` +
            `(~${(POST_LOOKUP_READ_ATTEMPTS * POST_LOOKUP_READ_DELAY_MS) / 1000}s). ` +
            `The cloud function may persist asynchronously — widen the read window.`,
        );
    }

    // 3. API unreachable (or its write never materialized) — fall back to
    //    whatever is already cached, even if stale. Better old data than none.
    const stale = await readVehicleDetails(regNo);
    if (stale) {
        console.log(
            `[vehicleDetails] serving existing DB record for ${regNo} ` +
            `(RC API ${apiOk ? "succeeded but doc was absent" : "unavailable"}); ` +
            `data may be stale.`,
        );
        return stale;
    }

    console.error(
        `[vehicleDetails] no record for ${regNo}: RC API ` +
        `${apiOk ? "ok but no doc appeared" : "failed"} and the DB is empty.`,
    );
    return null;
}

// ─── Internal endpoint handler ───────────────────────────────────────────
//
// The worker POSTs here from the owner-info phase the moment it sees the
// "No data found" popup. We resolve the RC record and hand the raw map back;
// the worker maps it onto the parivahan form fields. requestId/driverId are
// accepted for symmetry with the other internal endpoints (logging/tracing) —
// the lookup itself only needs the vehicle number.

export interface VehicleDetailsInput {
    requestId?: string;
    driverId?: string;
    vehicleNumber?: string;
}

export async function handleVehicleDetails(input: VehicleDetailsInput) {
    const vehicleNumber = (input.vehicleNumber ?? "").trim();
    if (!vehicleNumber) {
        return { ok: false, error: "vehicleNumber required" };
    }

    const regNo = normalizeRegNo(vehicleNumber);
    const vd = await fetchVehicleDetails(regNo);

    if (!vd) {
        // Not an HTTP error — a legitimate "RC service had nothing" outcome.
        // The worker treats a null record as "manual fill has no data" and
        // ends that run with a clear abort_reason.
        console.log(
            `[vehicleDetails] /api/internal/vehicle-details no record for ${regNo} ` +
            `(req=${input.requestId ?? "?"})`,
        );
        return { ok: true, vehicleDetails: null };
    }

    console.log(
        `[vehicleDetails] /api/internal/vehicle-details resolved ${regNo} ` +
        `(req=${input.requestId ?? "?"})`,
    );
    return { ok: true, vehicleDetails: vd };
}
