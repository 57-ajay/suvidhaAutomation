// Challan-settlement Firestore writes. Owns the ONLY two shapes this task
// persists for the client:
//
//   challans/{VEH}                                (parent doc, merge)
//       aiCheckingSettlement: boolean
//       aiCheckingSettlementUpdatedAt / StartedAt / FinishedAt
//       aiSettlementRequestedChallans / aiSettlementSummary
//
//   challans/{VEH}/subChallans/{challanNo}        (merge)
//       quotation: { amount: number, at: Timestamp }
//       challanAmount: number        <- ONLY for "extra" challans the client
//                                       did not send (found during the vcourts
//                                       vehicle scan); equals the settlement.
//
// Doc-id convention (set by the worker): for challans the CLIENT sent, the
// subChallans doc id is the client's ORIGINAL string, so the app reads back
// exactly the id it wrote. Extras use the portal's challan number.
//
// The collection name defaults to top-level "challans"; override with
// CHALLANS_COLLECTION if the app keeps it elsewhere (e.g. nested under
// driverUtilitiesRequests/data/...).

import { FieldValue } from "firebase-admin/firestore";
import { db } from "../firebase";

const CHALLANS_COLLECTION = process.env.CHALLANS_COLLECTION ?? "challans";

const normalizeId = (s: unknown): string =>
    String(s ?? "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();

const challanDoc = (vehicleNumber: string) =>
    db.collection(CHALLANS_COLLECTION).doc(normalizeId(vehicleNumber));

// ── department mapping (mirror of worker departments.py — keep in sync) ──
// Exact <option> texts from the live vcourts dropdown (July 2026). Digit-
// leading challan numbers -> Delhi(Notice Department). Prefixes with no
// department in the dropdown are unmapped and reported to the client.

export const DELHI_NOTICE = "Delhi(Notice Department)";

export const STATE_TO_DEPARTMENT: Record<string, string> = {
    AS: "Assam(Traffic Department)",
    CH: "Chandigarh(Traffic Department)",
    CG: "Chhattisgarh(Traffic Department)",
    DL: "Delhi(Traffic Department)",
    GJ: "Gujarat(Traffic Department)",
    HR: "Haryana(Traffic Department)",
    HP: "Himachal Pradesh(Traffic Department)",
    JK: "Jammu and Kashmir(Jammu Traffic Department)",
    KA: "Karnataka(Traffic Department)",
    KL: "Kerala(Police Department)",
    MP: "Madhya Pradesh(Traffic Department)",
    MH: "Maharashtra(Transport Department)",
    MN: "Manipur(Traffic Department)",
    ML: "Meghalaya(Traffic Department)",
    OD: "Odisha(Odisha Traffic CTC-BBSR Commissionerate)",
    PB: "Punjab(Traffic Department)",
    RJ: "Rajasthan(Traffic Department)",
    TN: "Tamil Nadu(Traffic Department)",
    TR: "Tripura(Traffic Department)",
    UK: "Uttarakhand(Traffic Department)",
    UP: "Uttar Pradesh(Traffic Department)",
    WB: "West Bengal(Traffic Department)",
};

export function departmentForChallan(challanNo: string): string | null {
    const cn = normalizeId(challanNo);
    if (!cn) return null;
    if (/^\d/.test(cn)) return DELHI_NOTICE;
    return STATE_TO_DEPARTMENT[cn.slice(0, 2)] ?? null;
}

export function planDepartments(challanNumbers: string[]): {
    departments: string[];
    unmapped: string[];
} {
    const seen = new Set<string>();
    const departments: string[] = [];
    const unmapped: string[] = [];
    for (const c of challanNumbers) {
        const dept = departmentForChallan(c);
        if (!dept) {
            unmapped.push(c);
            continue;
        }
        if (!seen.has(dept)) {
            seen.add(dept);
            departments.push(dept);
        }
    }
    return { departments, unmapped };
}

// ── flag ─────────────────────────────────────────────────────────────────

export interface ChallanFlagInput {
    vehicleNumber?: string;
    requestId?: string; // == vehicle number; accepted for symmetry
    checking?: boolean;
    summary?: string;
    requestedChallans?: string[];
}

export async function setChallanCheckingFlag(input: ChallanFlagInput) {
    const veh = normalizeId(input.vehicleNumber ?? input.requestId);
    if (!veh) return { ok: false, error: "vehicleNumber required" };
    const checking = !!input.checking;

    const patch: Record<string, unknown> = {
        vehicleNumber: veh,
        aiCheckingSettlement: checking,
        aiCheckingSettlementUpdatedAt: FieldValue.serverTimestamp(),
    };
    if (checking) {
        patch.aiCheckingSettlementStartedAt = FieldValue.serverTimestamp();
        if (Array.isArray(input.requestedChallans) && input.requestedChallans.length) {
            patch.aiSettlementRequestedChallans = input.requestedChallans.map(String);
        }
    } else {
        patch.aiCheckingSettlementFinishedAt = FieldValue.serverTimestamp();
        if (input.summary) patch.aiSettlementSummary = String(input.summary).slice(0, 2000);
    }

    try {
        await challanDoc(veh).set(patch, { merge: true });
        console.log(`[challan-settlement] flag ${veh} aiCheckingSettlement=${checking}`);
        return { ok: true, vehicleNumber: veh, checking };
    } catch (e: any) {
        console.error(`[challan-settlement] flag write failed for ${veh}: ${e.message}`);
        return { ok: false, error: e.message };
    }
}

// ── quotations ───────────────────────────────────────────────────────────

export interface ChallanQuotationRecord {
    challanId: string; // subChallans doc id (client-original for matched, portal for extra)
    amount: number;    // settlement (Proposed Fine)
    isExtra?: boolean; // true -> also stamp challanAmount at the root
}

export interface SaveChallanQuotationsInput {
    jobId?: string;
    requestId?: string;
    driverId?: string;
    vehicleNumber?: string;
    department?: string;
    records?: ChallanQuotationRecord[];
}

const toAmount = (v: unknown): number | null => {
    const n = typeof v === "number" ? v : parseInt(String(v ?? "").replace(/[^\d-]/g, ""), 10);
    return Number.isFinite(n) ? n : null;
};

export async function handleSaveChallanQuotations(input: SaveChallanQuotationsInput) {
    const veh = normalizeId(input.vehicleNumber ?? input.requestId);
    if (!veh) return { ok: false, error: "vehicleNumber required" };
    const records = Array.isArray(input.records) ? input.records : [];
    if (!records.length) return { ok: false, error: "records array required" };

    const dropped: { challanId: string; reason: string }[] = [];
    const accepted: { challanId: string; amount: number; isExtra: boolean }[] = [];
    const seen = new Set<string>();

    for (const r of records) {
        const challanId = String(r?.challanId ?? "").trim();
        if (!challanId || /\s|[\/\\]/.test(challanId)) {
            dropped.push({ challanId, reason: "invalid_challanId" });
            continue;
        }
        const amount = toAmount(r?.amount);
        if (amount === null || amount < 0) {
            dropped.push({ challanId, reason: "invalid_amount" });
            continue;
        }
        const key = normalizeId(challanId);
        if (seen.has(key)) {
            dropped.push({ challanId, reason: "duplicate" });
            continue;
        }
        seen.add(key);
        if (amount === 0) {
            // Verified-zero settlements are legal (court set fine to 0); keep,
            // but leave a trail for reconciliation.
            console.warn(
                `[challan-settlement] ZERO quotation ${challanId} vehicle=${veh} ` +
                `dept=${input.department ?? "?"} job=${input.jobId ?? "?"}`,
            );
        }
        accepted.push({ challanId, amount, isExtra: !!r?.isExtra });
    }

    if (!accepted.length) {
        return { ok: false, error: "no valid records", dropped };
    }

    try {
        const parent = challanDoc(veh);
        const batch = db.batch();
        for (const rec of accepted) {
            const payload: Record<string, unknown> = {
                quotation: {
                    amount: rec.amount,
                    at: FieldValue.serverTimestamp(),
                },
            };
            if (rec.isExtra) payload.challanAmount = rec.amount;
            batch.set(parent.collection("subChallans").doc(rec.challanId), payload, {
                merge: true,
            });
        }
        batch.set(
            parent,
            { lastQuotationAt: FieldValue.serverTimestamp(), vehicleNumber: veh },
            { merge: true },
        );
        await batch.commit();

        console.log(
            `[challan-settlement] saved ${accepted.length} quotations ` +
            `(${accepted.filter((r) => r.isExtra).length} extra) vehicle=${veh} ` +
            `dept=${input.department ?? "?"} job=${input.jobId ?? "?"} ` +
            `dropped=${dropped.length}`,
        );
        return { ok: true, saved: accepted.length, dropped };
    } catch (e: any) {
        console.error(`[challan-settlement] quotation write failed for ${veh}: ${e.message}`);
        return { ok: false, error: e.message, dropped };
    }
}
