"""Challan-payment task (vcourts.gov.in Virtual Courts).

ONE JOB PER CHALLAN. A vehicle with five open challans produces five
independent requests, five jobs, and five subChallan docs — nothing about this
task ever fans out, so a failure on one challan can never affect another.

    client -> POST /api/run { taskId: "challan-payment",
                              params: { vehicleNumber, challanNo, requestId,
                                        phoneNo, chassisNo, department? } }

    requestId == challanNo, because that is the doc id of the record we write:

        subChallanRequests/{challanNo}
            aiAgentStatus: { status, paid, error, reason, attempt, ... }
            receipt:   { url, at }      <- root, only once paid
            subStatus: "completed"      <- root, only once paid

Phases
    open_portal     index.php -> Select Department -> Proceed Now. The
                    department comes from the challan prefix (departments.py)
                    unless the client passed one explicitly.
    search_challan  Challan/Vehicle No. tab -> fill the CHALLAN number -> solve
                    the securimage captcha (Vertex OCR, silent) -> Submit ->
                    results. "does not exist" ends the run as cancelled.
    open_record     View -> reveal the verification fields -> fill the mobile
                    number and the last 4 of the chassis. Stops just short of
                    "Get OTP": that OTP goes to the driver's phone and only the
                    operator can carry it across.
    human_handover  Operator clicks Get OTP, enters it, and pays on the live
                    view. We poll for the receipt page (its window.print
                    button) or a portal failure marker, then printToPDF and
                    upload via /api/internal/challan-payment/save-receipt.

Money line: everything through open_record is pre-payment, so a stop there is
`cancelled` (retryable). From human_handover onward the operator is holding a
live payment page, so a stop is `failed` — reconcile, never a silent cancel.
This mirrors the scripted border-tax handover exactly.

Ported from the legacy repo's scripted/challan/*: the page flow, the
legacy-confirmed selectors, and the receipt-detection JS. Everything else —
phases, statuses, captcha, receipt upload, reporting — is the autoScale stack.
"""
