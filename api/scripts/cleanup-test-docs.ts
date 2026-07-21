// api/scripts/cleanup-test-docs.ts — MEMORY §9-5 prod test-data cleanup.
//
// SELF-CONTAINED on purpose: initializes firebase-admin itself with the same
// credential order as src/firebase.ts (FIREBASE_SERVICE_ACCOUNT inline ->
// GOOGLE_APPLICATION_CREDENTIALS path -> ADC), so it runs unchanged inside
// the api pod (bun + firebase-admin + creds already there) or on any machine
// with the key file. Reads the same *_DOC_PATH_TEMPLATE envs the api uses,
// so a non-default template is respected automatically.
//
// WHAT IT TOUCHES (border-tax + puc request collections only):
//   scans ids with prefixes  test-  bt_  puc_   (test- = declared test docs;
//   bt_/puc_ = the ops dashboard's Generate button — real client traffic can
//   never produce those shapes)
//
// SAFETY MODEL — precedence per doc:
//   1. id in --keep list                -> KEEP
//   2. id starts with "test-"           -> DELETE  (declared test; MEMORY rule)
//   3. doc has receiptDocumentUrl       -> KEEP    (receipt guard: a bt_ doc
//                                          holding a real paid receipt — like
//                                          the ₹4440 scripted run — is never
//                                          bulk-deleted)
//   4. id starts with bt_ / puc_        -> DELETE
//
// NOTHING is deleted without --delete; the default run is a full DRY-RUN
// report. challans/{VEH} are NEVER deleted here — --challans prints them for
// the hand review MEMORY mandates.
//
// USAGE
//   bun cleanup-test-docs.ts                      # dry-run report (always start here)
//   bun cleanup-test-docs.ts --keep id1,id2       # dry-run with extra protected ids
//   bun cleanup-test-docs.ts --delete             # perform the deletions from the report
//   bun cleanup-test-docs.ts --delete --keep id1  # both
//   bun cleanup-test-docs.ts --repair 3JVgobriW5VbCmohc7ed --note "kill-drill QR cancel, no payment issue — reconciled by hand"
//   bun cleanup-test-docs.ts --challans           # read-only listing for hand review

import { initializeApp, cert, applicationDefault, type App } from "firebase-admin/app";
import { getFirestore, FieldPath, Timestamp } from "firebase-admin/firestore";
import { readFileSync } from "node:fs";

// ── bootstrap (mirrors src/firebase.ts) ─────────────────────────────────
function init(): App {
    const inline = process.env.FIREBASE_SERVICE_ACCOUNT;
    const keyPath = process.env.GOOGLE_APPLICATION_CREDENTIALS;
    const credential = inline
        ? cert(JSON.parse(inline))
        : keyPath
            ? cert(keyPath)
            : applicationDefault();
    return initializeApp({ credential });
}
const db = getFirestore(init());

// Sanity line so a wrong-project run is obvious before anything happens.
try {
    const keyPath = process.env.GOOGLE_APPLICATION_CREDENTIALS;
    const projectId = process.env.FIREBASE_SERVICE_ACCOUNT
        ? JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT).project_id
        : keyPath
            ? JSON.parse(readFileSync(keyPath, "utf8")).project_id
            : "(application default)";
    console.log(`connected via service account for project: ${projectId}\n`);
} catch { /* best-effort only */ }

// ── collections from the same env templates the api uses ────────────────
function collOf(tmpl: string | undefined, fallback: string): string {
    return (tmpl ?? fallback).replace(/\/\{requestId\}$/, "");
}
const BORDER_COLL = collOf(
    process.env.REQUEST_DOC_PATH_TEMPLATE,
    "driverUtilitiesRequests/data/borderTaxRequests/{requestId}",
);
const PUC_COLL = collOf(
    process.env.PUC_DOC_PATH_TEMPLATE,
    "driverUtilitiesRequests/data/pucRequests/{requestId}",
);
const CHALLAN_COLL = collOf(
    process.env.CHALLAN_DOC_PATH_TEMPLATE,
    "challans/{requestId}",
);
const GCS_BUCKET = process.env.GCS_BUCKET ?? "";
const GCS_PREFIX = process.env.GCS_PREFIX ?? "driverUtilitiesRequests/borderTaxRequests";
const PUC_GCS_PREFIX = process.env.PUC_GCS_PREFIX ?? "driverUtilitiesRequests/pucRequests";

const PREFIXES = ["test-", "bt_", "puc_"];

// ── args ────────────────────────────────────────────────────────────────
const argv = process.argv.slice(2);
const DO_DELETE = argv.includes("--delete");
const DO_CHALLANS = argv.includes("--challans");
const keepArg = argv.find((_, i) => argv[i - 1] === "--keep") ?? "";
const KEEP = new Set(keepArg.split(",").map((s) => s.trim()).filter(Boolean));
const repairId = argv.find((_, i) => argv[i - 1] === "--repair") ?? "";
const note = argv.find((_, i) => argv[i - 1] === "--note") ?? "";

type Row = {
    coll: string;
    id: string;
    verdict: "DELETE" | "KEEP(list)" | "KEEP(receipt)";
    status: string;
    agent: string;
    receipt: boolean;
    review: boolean;
};

function classify(id: string, data: Record<string, unknown>): Row["verdict"] {
    if (KEEP.has(id)) return "KEEP(list)";
    if (id.startsWith("test-")) return "DELETE";
    if (data.receiptDocumentUrl) return "KEEP(receipt)";
    return "DELETE"; // bt_/puc_ without a receipt
}

async function scan(coll: string): Promise<Row[]> {
    const rows: Row[] = [];
    for (const prefix of PREFIXES) {
        let cursor: FirebaseFirestore.QueryDocumentSnapshot | null = null;
        for (; ;) {
            let q = db.collection(coll)
                .orderBy(FieldPath.documentId())
                .startAt(prefix)
                .endAt(prefix + "\uf8ff")
                .limit(200);
            if (cursor) q = q.startAfter(cursor);
            const snap = await q.get();
            if (snap.empty) break;
            for (const d of snap.docs) {
                const data = d.data() as Record<string, unknown>;
                const ai = (data.aiAgentData ?? {}) as Record<string, unknown>;
                rows.push({
                    coll,
                    id: d.id,
                    verdict: classify(d.id, data),
                    status: String(data.status ?? ""),
                    agent: String(ai.status ?? ""),
                    receipt: !!data.receiptDocumentUrl,
                    review: !!(data.manualReview as Record<string, unknown> | undefined)?.required,
                });
            }
            cursor = snap.docs[snap.docs.length - 1]!;
            if (snap.size < 200) break;
        }
    }
    return rows;
}

async function main() {
    // ── repair mode (real docs: clear manualReview, annotate, keep reason) ──
    if (repairId) {
        const ref = db.collection(BORDER_COLL).doc(repairId);
        const snap = await ref.get();
        if (!snap.exists) { console.error(`repair: ${repairId} not found in ${BORDER_COLL}`); process.exit(1); }
        const before = (snap.get("manualReview") ?? {}) as Record<string, unknown>;
        console.log("before:", JSON.stringify(before));
        const after = {
            ...before,
            required: false,
            resolved: true,
            resolvedAt: Timestamp.now(),
            remarks: note || "resolved by hand during §9-5 cleanup",
        };
        await ref.set({ manualReview: after, updatedAt: Timestamp.now() }, { merge: true });
        console.log("after: ", JSON.stringify({ ...after, resolvedAt: "(now)" }));
        console.log(`repaired ${BORDER_COLL}/${repairId}`);
        return;
    }

    // ── challans hand-review listing (read-only, never deleted here) ──────
    if (DO_CHALLANS) {
        const snap = await db.collection(CHALLAN_COLL).limit(100).get();
        console.log(`${CHALLAN_COLL}: ${snap.size} doc(s) (first 100) — REVIEW BY HAND, real vehicle keys:\n`);
        for (const d of snap.docs) {
            const data = d.data() as Record<string, unknown>;
            console.log(`  ${d.id}  aiCheckingSettlement=${String(data.aiCheckingSettlement ?? "-")}  keys=[${Object.keys(data).join(", ")}]`);
        }
        return;
    }

    // ── scan + report ─────────────────────────────────────────────────────
    const rows = [...(await scan(BORDER_COLL)), ...(await scan(PUC_COLL))];
    if (!rows.length) { console.log("no docs matched the test prefixes — already clean."); return; }

    let del = 0;
    for (const r of rows) {
        if (r.verdict === "DELETE") del++;
        console.log(
            `${r.verdict.padEnd(13)} ${r.coll.split("/").pop()}/${r.id}` +
            `  status=${r.status || "-"} agent=${r.agent || "-"}` +
            `${r.receipt ? " [RECEIPT]" : ""}${r.review ? " [manualReview]" : ""}`,
        );
    }
    console.log(`\n${rows.length} matched · ${del} deletable · ${rows.length - del} kept`);

    if (!DO_DELETE) {
        console.log("\nDRY-RUN — nothing deleted. Re-run with --delete to execute,");
        console.log("or --keep id1,id2 to protect specific ids first.");
    } else {
        const targets = rows.filter((r) => r.verdict === "DELETE");
        for (let i = 0; i < targets.length; i += 200) {
            const batch = db.batch();
            for (const r of targets.slice(i, i + 200)) {
                batch.delete(db.collection(r.coll).doc(r.id));
            }
            await batch.commit();
            console.log(`deleted ${Math.min(i + 200, targets.length)}/${targets.length}`);
        }
        console.log("done.");
    }

    // ── the parts this script deliberately does NOT automate ──────────────
    console.log(`
MANUAL CHECKLIST (from MEMORY §9-5):
 1. --repair 3JVgobriW5VbCmohc7ed --note "..."   (real driver — repair, never delete)
 2. challans/{VEH}: run --challans and hand-review UP31CT0QW1 / HP03LT3037 etc.
 3. test_driver_001 / test-driver-* side effects (setAssignedPartner, usage
    counters) live in client-owned collections — check those by hand.
 4. GCS leftovers (pennies, optional):
      gsutil ls gs://${GCS_BUCKET}/${GCS_PREFIX}/test-*
      gsutil ls gs://${GCS_BUCKET}/${PUC_GCS_PREFIX}/test-*
    then gsutil -m rm -r the test-* prefixes you want gone. Do NOT touch the
    prefix of any KEEP(receipt) doc.`);
}

main().then(() => process.exit(0)).catch((e) => { console.error(e); process.exit(1); });
