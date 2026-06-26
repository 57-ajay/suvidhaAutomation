# Vendor panel — dedicated agent instance

The B2B vendor panel reuses this exact agent, run as a **separate instance**
pointed at the vendor product's Firebase project. The existing CabsWale instance
is untouched. No code changes are required to repoint it — everything below is
config — except the opt-in `PUBLIC_API_KEY` guard added for the internet-exposed
case.

## Topology

```
vendor panel ─ writes ─▶ Firestore (new project) ◀─ writes lifecycle ── this agent
   │                          ▲
   └─ callable ─▶ Cloud Functions ── POST /api/run (+X-Internal-Key) ──┘
                  (hold the agent URL + key; vendors never call it)
```

- The panel's Cloud Functions call `POST /api/run`, `/api/jobs/:id/intervene`,
  and `/api/jobs/:id/cancel` — never the browser.
- This agent writes the lifecycle into `borderTaxRequests/{requestId}` in the
  **same** project the panel reads from (via `onSnapshot`).
- Captcha / QR / receipt images go to the new project's Cloud Storage bucket.

## Configure

Copy `.env.vendor.example` → `.env` and set:

| var | value |
|---|---|
| `REQUEST_DOC_PATH_TEMPLATE` | `borderTaxRequests/{requestId}` (matches the panel schema) |
| `GCS_BUCKET` | the new project's bucket, e.g. `your-project.firebasestorage.app` |
| `GCS_PREFIX` | `borderTaxRequests` |
| `SERVICE_ACCOUNT_FILE` | path to the **new project's** service-account JSON (mounted at `/secrets/service.json`) |
| `INTERNAL_API_KEY` | worker ↔ api secret (private network) |
| `PUBLIC_API_KEY` | **must equal** the panel functions' `INTERNAL_API_KEY` secret |

The service account needs Firestore + Storage (object admin) on the new project.
`docker-compose.yaml` already mounts `${SERVICE_ACCOUNT_FILE}` and sets
`GOOGLE_APPLICATION_CREDENTIALS=/secrets/service.json`.

## Must match the panel

Set these on the panel's Cloud Functions (`functions/`):

- `SUVIDHA_API_URL` = this instance's public HTTPS base URL.
- `INTERNAL_API_KEY` (secret) = this instance's `PUBLIC_API_KEY`.

## Exposure & security

`PUBLIC_API_KEY` makes the mutating public routes require a matching
`X-Internal-Key`, so only the panel's Functions can enqueue work or intervene.
GET reads and `/api/dashboard` are **not** key-guarded (the ops console must stay
browser-usable) — restrict them at the proxy (auth / IP allowlist). Expose the
api behind the existing Caddy `proxy` network at the public URL you set as
`SUVIDHA_API_URL`.

## Run

```bash
docker network create proxy   # once, if not present
cp .env.vendor.example .env    # then edit
docker compose up -d --build
```

Smoke-test the guard (replace URL/key):

```bash
curl -s $URL/api/health                                   # ok, no key needed
curl -s -X POST $URL/api/run -d '{}'                      # 401 unauthorized
curl -s -X POST $URL/api/run -H "X-Internal-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"taskId":"border-tax","source":"app","params":{}}' # 400 validation (key OK)
```
