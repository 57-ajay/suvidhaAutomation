# Border Tax Agent

Automates state border-tax payment on the parivahan **CheckPost V4** portal for
CabsWale drivers. The driver's app talks to Firestore; this stack drives the
portal in a real browser, pushes the captcha and the UPI QR back to the app,
and captures the e-receipt.

Scope: **app source + UPI**, **Uttar Pradesh** live, architected so adding a
state is two registry entries (see *Adding a state*).

```
app ──▶ Firestore ◀── writes ──┐
 │                             │
 └─▶ POST /api/run        ┌────┴─────┐    job:queue     ┌──────────────┐
        (validate,  ──▶   │ API (Bun)│ ──── Redis ────▶ │ worker (uv)  │
         eligibility,     └────┬─────┘ ◀── /internal ── │ Xvfb + CDP   │
         dedupe, enqueue)      │                        │ browser-use  │
                          GCS (QR / captcha /           └──────┬───────┘
                               receipt, signed URLs)       CheckPost V4
```

- **API** (Bun + TS) owns validation, the queue, **all Firestore/GCS writes**,
  and the FSM. The service-account key is mounted here **only**.
- **Worker** (Python + uv, browser-use over CDP) drives the portal headed on
  per-job Xvfb displays, mirrored live over noVNC. It talks to Firestore
  exclusively through `/api/internal/*`. Vertex AI auth is the VM's default
  service account (ADC) — no key in the container.
- **Redis** is the live operational view (queue, job hash, console); Firestore
  is the client-facing record.

## Lifecycle

`api/src/lifecycle/statuses.ts` is the source of truth;
`worker/src/lifecycle/status.py` mirrors it. Every transition is FSM-guarded
on both sides.

| `aiAgentData.status` | meaning | client shows |
|---|---|---|
| `queued` | validated, waiting for a slot | "starting…" |
| `aiAgentStarted` | browser is filling the portal | progress |
| `pendingTransaction` | clearing a stuck in-flight tx (gated) | progress |
| `pendingTransactionCaptcha` | **user input needed** to clear a pending tx (AI off / fell back) | captcha UI |
| `captchaSolving` | **user input needed** — see `aiAgentData.captcha` | captcha UI |
| `settingUpPaymentRequest` | captcha accepted, reaching the gateway | progress |
| `qrPaymentNeeded` | **user payment needed** — see `aiAgentData.qrCode` | QR + pay |
| `verifyingPayment` | payment signal seen, confirming | progress |
| `generatingReceipt` | payment confirmed, capturing receipt | progress |
| `completed` | receipt uploaded (terminal) | success + receipt |
| `cancelled` | stopped **before any money moved** (terminal, retryable) | reason + retry |
| `failed` | payment attempted/unconfirmed (terminal) → `manualReview` | "we're on it" |

Money rule: through `captchaSolving` every stop is `cancelled`; from
`settingUpPaymentRequest` onward a stop is `failed`/reconcile — never a
silent success.

## Client contract (Firestore)

Doc: `driverUtilitiesRequests/data/borderTaxRequests/{requestId}`
(template: `REQUEST_DOC_PATH_TEMPLATE`). The agent writes **only**:

- `aiAgentData.status`, `statusUpdatedAt`, `source`, `error{isError,message}`
- `aiAgentData.captcha` — `{url, attempt, maxAttempts, lastResult,
  uploadedAt, inputDeadline, resultAt}`
- `aiAgentData.qrCode` — `{url, uploadedAt, expiredAt, notificationSent:false,
  notificationSentAt:null}` (the existing Firestore trigger sends the push)
- `aiAgentData.receipt` — `{url, fields{receiptNumber, amount, paymentDate,
  bankRef}, uploadedAt}` + `paymentCompleted`, `receiptGenerated` flags
- `agentCost`
- terminal only: top-level `status`, a `statusUpdateHistory` append,
  `receiptDocumentUrl` (completed), `manualReview` (failed),
  `cancelledDetails` (cancelled)

Everything else (`amount`, `partnerDetails`, `vehicleDetails`, `processType`,
`aiProcessTriggered`, …) belongs to other services and is never touched.

**Captcha handling in the app** — one listener on the doc is enough:

```
captcha.lastResult == "awaiting_input"  -> show captcha.url + input box
                                           ("attempt {attempt} of {maxAttempts}",
                                            countdown to inputDeadline)
                                           submit via POST /api/jobs/{requestId}/intervene {input}
captcha.lastResult == "rejected"        -> show "wrong code" — a fresh image
                                           (new url, attempt+1) follows in seconds
captcha.lastResult == "accepted"        -> hide the captcha UI
```

Each attempt uploads a **fresh filename** (`captcha_{attempt}.png`) so image
caching can never show a stale captcha.

## API

Public:

- `POST /api/run` — `{taskId:"border-tax", source:"app", params:{…}}`.
  Validates per state (400 lists `missing` / `invalid`), eligibility-checks,
  dedupes on active `requestId`, enqueues. `jobId == requestId`.
- `GET  /api/jobs/:id/status` · `POST /api/jobs/:id/intervene {input}` ·
  `POST /api/jobs/:id/cancel`
- `GET  /api/jobs?limit=N` · `GET /api/health` · `GET /api/tasks`
- `GET  /api/dashboard` — ops console (captcha/QR, intervene, live view).

Internal (worker → API, private network; optional `X-Internal-Key`):
`status-update`, `save-captcha`, `captcha-result`, `save-qr`, `save-receipt`,
`job-completed`.

Required `params` for UP: `requestId, driverId, mobileNumber, vehicleNumber,
taxMode (DAYS|MONTHLY|QUARTERLY|YEARLY), taxFrom, entryDistrict` —
plus `taxUpto` when `taxMode=DAYS`. Optional: `entryCheckpoint, permitType,
permitTypeFallback, serviceType`. Payment method is pinned to UPI.

## Run it

```bash
cp .env.example .env          # fill GCS_BUCKET, DOMAIN, doc path if different
cp /path/to/service.json .    # Firebase key — API container only
docker network create proxy   # once, if the external Caddy network is absent
docker compose up -d --build
```

Add `deploy/Caddyfile.pivot.example` as a site block to the standalone Caddy
(it resolves `pivot-api` / `pivot-worker` over the `proxy` network), then:

- console: `https://<DOMAIN>/api/dashboard` (via the `/api/*` handle)
- live view per job: `https://<DOMAIN>/vnc.html?autoconnect=true&resize=scale&path=websockify%3Ftoken%3D<jobId>`
  (also stored on the job hash as `liveUrl`)

Local dev without Docker: `cd api && bun install && bun run dev`, and
`cd worker && uv sync && uv run python src/main.py` (needs Xvfb, x11vnc,
websockify/noVNC on the host, plus Chromium where browser-use looks for it:
`uvx playwright install chromium`).

Worker VM needs `roles/aiplatform.user` on `VERTEX_PROJECT` for the
pending-clear OCR (only used when `AUTO_CLEAR_PENDING=true`).

## Adding a state

1. `api/src/validators/states/<code>.ts` — required/optional fields, defaults,
   normalization; register in `api/src/validators/index.ts`.
2. `worker/src/tasks/border_tax/states/<code>.py` — a selectors block + phase
   functions + a `PHASES` list; register in `tasks/border_tax/registry.py`.
3. New lifecycle step, if the state needs one: add the status + transitions in
   `statuses.ts`, mirror in `status.py`, give the phase an `enter_status`.

Nothing else changes — queue, console, captcha/QR/receipt plumbing, and the
client contract are state-agnostic.

## Before first live run

Selector ids are centralized at the top of
`worker/src/tasks/border_tax/states/up.py` (and `pending_clear.py`); the ones
marked *(confirm live)* must be checked against the current CheckPost build.
Same for the CDP accessor in `engine/steps.py:cdp_eval` against the installed
browser-use, and the payment markers in `payment_wait.py` if SBI changes its
wording. `internal/eligibility.ts` is a pass-through — wire it to RC/VAHAN
validity data to cancel doomed requests before a slot is spent.
