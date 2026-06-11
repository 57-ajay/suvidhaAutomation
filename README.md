# border-tax pivot-a

A production-grade rebuild of the CabsWale border-tax payment agent.
**Scope:** app source + UPI, **Uttar Pradesh** first.

- **API** — Bun + TypeScript. Owns validation, the lifecycle state machine, all
  Firestore writes, and the job queue.
- **Worker** — Python + browser-use. Drives the parivahan CheckPost portal with a
  deterministic scripted flow, reports status, and waits for the two human/AI
  seams (the user captcha and the UPI payment).

The split is deliberate: the worker stays **credential-free** (no Firebase key),
talks only to Redis and the API's internal endpoints; the API holds the keys and
is the single writer of Firestore. Same shape as prod, tightened.

---

## What changed vs prod (the point of pivot-a)

1. **An explicit lifecycle FSM** (`api/src/lifecycle/statuses.ts`, mirrored in
   `worker/src/lifecycle/status.py`). Every status write is guarded; illegal
   jumps are rejected. Three terminals with distinct meaning:
   - `completed` — receipt captured (success).
   - `cancelled` — stopped **before any money moved** (safe to retry).
   - `failed` — payment **attempted but unconfirmed** (e.g. SBI "pending"/"NA")
     → flips `manualReview.required` for reconciliation. Never a silent success.
2. **Validation moved into the API** (`api/src/validators/`). Bad input is
   rejected at `/api/run` with a structured `{ missing, invalid, message }`
   **before a browser slot is ever spent**.
3. **Eligibility pre-check** (`api/src/internal/eligibility.ts`). PUCC / insurance
   / fitness is the #1 "die four pages deep" failure — catch it up front and
   cancel cheaply. (Ships as a pass-through stub; wire it to RC/VAHAN — see TODO.)
4. **Two captchas, two solvers.** The background pending-transaction captcha stays
   **AI-solved**; the main payment captcha is now **user-solved** — 3 attempts,
   then a **hard cancel** (no money has moved at that stage).

---

## Lifecycle

```
queued
  └─ aiAgentStarted
       ├─ pendingTransaction ──(cleared)──┐   (AI captcha; gated by AUTO_CLEAR_PENDING)
       │                                  ↓
       └────────────────────────► captchaSolving   (USER captcha, ×3 then cancel)
                                          ↓
                              settingUpPaymentRequest   (→ SBI ePay → UPI)
                                          ↓
                                   qrPaymentNeeded   (UPI QR shown, await payment)
                                          ↓
                                  verifyingPayment
                                          ↓
                                      completed                 ── TERMINAL (success)

  cancelled  ── TERMINAL (no money moved; safe to retry)
  failed     ── TERMINAL (payment attempted, unconfirmed; manualReview)
```

---

## The Firestore document

We write **only** the `aiAgentData` subtree, plus `agentCost`, and (on a terminal)
the top-level `status` + `statusUpdateHistory` (+ `cancelledDetails` / `manualReview`
/ `receiptDocumentUrl`). The rest of the document is written by the existing flow.

`aiAgentData` we maintain:

```
aiAgentData: {
  status, statusUpdatedAt, source,
  paymentCompleted, paymentCompletedAt,
  receiptGenerated, receiptGeneratedAt,
  qrCode:  { url, uploadedAt, expiredAt, notificationSent, notificationSentAt },
  captcha: { url, attempt, uploadedAt },     // pivot-a addition
  error:   { isError, message }
}
```

> ⚠️ **Confirm the document path.** You wrote
> `driverUtilities/bordertaxRequests/{requestId}` — that's a 3-segment collection
> path, but a Firestore *document* needs an even number of segments, so there's
> almost certainly a driver segment (your GCS key is `..._<partnerId>`). It's an
> env template (`REQUEST_DOC_PATH_TEMPLATE`) with `{driverId}` / `{requestId}`
> placeholders, defaulting to `driverUtilities/{driverId}/bordertaxRequests/{requestId}`.
> Set the right value in `.env` before running.

---

## API surface

Public:
- `POST /api/run` — `{ taskId:"border-tax", source:"app", params:{...} }`. Validates,
  eligibility-checks, enqueues. 400 with details on bad params.
- `GET  /api/jobs/:id/status`
- `POST /api/jobs/:id/intervene` — `{ input }` (the captcha text, or `"paid"`).
- `POST /api/jobs/:id/cancel`
- `GET  /api/health`, `GET /api/tasks`

Internal (worker → API; keep behind the private network):
- `POST /api/internal/status-update`
- `POST /api/internal/save-qr`
- `POST /api/internal/save-captcha`
- `POST /api/internal/save-receipt`
- `POST /api/internal/job-completed`

---

## Run it

```bash
unzip border-tax-pivot-a.zip && cd border-tax-pivot-a
cp .env.example .env      # then fill in (esp. REQUEST_DOC_PATH_TEMPLATE, GCS_BUCKET, creds)
docker compose up --build
```

Local dev without Docker:

```bash
# API
cd api && bun install && bun run dev          # :3000

# Worker (needs Xvfb/x11vnc/websockify + chromium on the host)
cd worker && uv sync && uv run python src/main.py
```

Live-view a running job:
`https://<DOMAIN>/vnc.html?path=websockify%3Ftoken%3D<jobId>&autoconnect=true`

Deploying next to prod (the "separate Caddy" question) → **`deploy/README.md`**.
Short version: two Caddies can't both bind :443, so run one Caddy with two site
blocks (`deploy/Caddyfile.combined`); Redis stays separate (`redis-pivot`).

---

## Before going live — confirm against the running portal

The contract layer (FSM, validation, DS mapping, queue, routing) is complete. The
browser-specific bits carry `TODO: confirm` markers because they must be checked
against the current CheckPost build:

- **Selectors** in `worker/src/scripted/border_tax/up.py` (centralized at the top
  of the file) and the captcha/pending selectors — verify ids/labels live.
- **`eligibility.ts`** — wire to your RC/VAHAN validity source so PUCC/insurance/
  fitness cancels at `/api/run`.
- **CDP seam** `_cdp_eval` in `worker/src/scripted/steps.py` — confirm the CDP
  client accessor matches your installed browser-use version.
- **Payment markers** in `worker/src/scripted/payment_wait.py` already encode the
  SBI "pending"/"(NA)" negative cases from the sample receipts; extend if you see
  other phrasings in the wild.

Internal-endpoint auth is left as a TODO (worker and API share a private network);
add a shared secret header before exposing the API beyond that network.
