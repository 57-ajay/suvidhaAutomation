"""Challan-settlement task (vcourts.gov.in Virtual Courts).

Client-driven, no-payment flow on the shared engine.pipeline — the challan
sibling of border-tax and PUC:

    client -> POST /api/run { taskId: "challan-settlement",
                              params: { vehicleNumber, challanNumbers[] } }

    Phase "start"        assert aiCheckingSettlement=true on challans/{VEH}
                         (already set by the API at enqueue; idempotent merge),
                         seed the per-run scratch (department plan, requested-
                         challan map).
    Phase "dept_N_*"     ONE phase PER Virtual Courts department derived from
                         the client's challan numbers. Each phase is fully
                         isolated: navigate index.php -> select department ->
                         Proceed Now -> Challan/Vehicle No. tab -> fill VEHICLE
                         number -> solve the securimage captcha (AI OCR, silent;
                         no human fallback by default) -> Submit -> extract all
                         records -> skip paid/disposed/warrant/pending -> save
                         quotations (plus null-amount physicalCourt marks for
                         requested challans absent from the results or
                         transferred to a regular court) via
                         /api/internal/challan-settlement/quotations.
                         Any failure inside a department marks THAT department
                         skipped/failed and the run continues.
    Phase "finalize"     clear aiCheckingSettlement, write the run summary on
                         challans/{VEH}, and fold per-department results into
                         the RunOutcome (done / cancelled).

Data written (by the API, on the worker's behalf):
    challans/{VEH}                          aiCheckingSettlement flag + summary
    challans/{VEH}/subChallans/{challanNo}  { quotation: { amount, physicalCourt, at } }
                                            + challanAmount at the root for
                                              challans the client did NOT send
                                              (extras discovered on vcourts).
                                            amount is null + physicalCourt true
                                              for requested challans concluded
                                              NOT to be on Virtual Courts
                                              (dept says "does not exist",
                                              absent from results, transferred
                                              to regular court, or no vcourts
                                              department for the prefix).

Never pays anything, never clicks "View" — read-only extraction. Payment is a
separate task.
"""
