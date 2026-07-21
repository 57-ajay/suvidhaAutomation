"""PUC-certificate task.

A self-contained, no-payment runner on the parivahan PUC portal
(puc.parivahan.gov.in). Reuses the border-tax engine wholesale — the phase
pipeline, the captcha solver (AI-silent OCR + human fallback), the
printToPDF -> save_receipt upload path, the FSM and the reporter. The only
thing that differs at the infra level is the Firestore collection, which is
selected by `task` ("puc-certificate") on every doc write.
"""
