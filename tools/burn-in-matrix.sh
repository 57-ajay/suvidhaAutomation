#!/usr/bin/env bash
# tools/burn-in-matrix.sh — fire the /api/run source/path matrix at a LIVE API.
#
# Serves MEMORY.md §9-4 (real-request burn-in via curl + X-Internal-Key) for
# BOTH paths. By default only the NEGATIVE cases run — every one must be
# rejected, so nothing is enqueued and prod stays clean. Pass --enqueue to
# also fire the two REAL runs (app fullyAutomated + web scripted); those DO
# queue work, cost a browser slot each, and (if you pay) real money — watch
# them on the dashboard / live view.
#
# Usage:
#   BASE=https://gkep.cabswale.in KEY=<PUBLIC_API_KEY> ./tools/burn-in-matrix.sh
#   ... --enqueue VEHICLE=UP32AB1234 DRIVER=<realDriverId>   # real runs too
#
# Requires: curl, jq. Exits non-zero if any expectation fails.

set -u
BASE="${BASE:-http://localhost:3000}"
KEY="${KEY:-}"
ENQUEUE=0
for a in "$@"; do
  case "$a" in
    --enqueue) ENQUEUE=1 ;;
    VEHICLE=*) VEHICLE="${a#VEHICLE=}" ;;
    DRIVER=*)  DRIVER="${a#DRIVER=}" ;;
  esac
done
VEHICLE="${VEHICLE:-UP32AB1234}"
DRIVER="${DRIVER:-burnin_driver}"
TAXFROM="$(date -d '+1 day' +%F 2>/dev/null || date -v+1d +%F)"
TAXUPTO="$(date -d '+2 day' +%F 2>/dev/null || date -v+2d +%F)"

PASS=0; FAIL=0
hdr=(-H "Content-Type: application/json")
[ -n "$KEY" ] && hdr+=(-H "X-Internal-Key: $KEY")

# post <name> <expected_http> <jq_filter_that_must_be_true> <json_body>
post() {
  local name="$1" want="$2" filt="$3" body="$4"
  local resp code json
  resp=$(curl -sS -o /tmp/burnin.json -w '%{http_code}' "${hdr[@]}" \
              -X POST "$BASE/api/run" -d "$body" 2>/dev/null)
  code="$resp"; json=$(cat /tmp/burnin.json 2>/dev/null || echo '{}')
  if [ "$code" = "$want" ] && echo "$json" | jq -e "$filt" >/dev/null 2>&1; then
    PASS=$((PASS+1)); printf '  ok  %s\n' "$name"
  else
    FAIL=$((FAIL+1)); printf 'FAIL  %s  (http=%s body=%s)\n' "$name" "$code" "$json"
  fi
}

UPP='"requestId":"burnin-neg","driverId":"'$DRIVER'","vehicleNumber":"'$VEHICLE'","state":"UP","taxMode":"DAYS","taxFrom":"'$TAXFROM'","taxUpto":"'$TAXUPTO'","paymentMethod":"UPI","entryDistrict":"SAHARANPUR","mobileNumber":"9999999999"'

echo "── negative matrix against $BASE (nothing enqueues) ──"
if [ -n "$KEY" ]; then
  # deliberately NO key header here — must bounce at the auth layer (401),
  # so the valid body can never reach the enqueue path.
  code=$(curl -sS -o /tmp/burnin.json -w '%{http_code}' -H "Content-Type: application/json" \
              -X POST "$BASE/api/run" \
              -d '{"taskId":"border-tax","source":"app","params":{'"$UPP"'}}' 2>/dev/null)
  if [ "$code" = "401" ]; then PASS=$((PASS+1)); printf '  ok  %s\n' "auth: no key -> 401"
  else FAIL=$((FAIL+1)); printf 'FAIL  %s (http=%s)\n' "auth: no key -> 401" "$code"; fi
fi

post "missing source -> 400" 400 '.missing | index("source")' \
  '{"taskId":"border-tax","params":{'"$UPP"'}}'
post "source=phone -> 400" 400 '.error | test("unsupported source")' \
  '{"taskId":"border-tax","source":"phone","params":{'"$UPP"'}}'
post "app + path=scripted -> 400" 400 '.error == "unsupported_path"' \
  '{"taskId":"border-tax","source":"app","path":"scripted","params":{'"$UPP"'}}'
post "web without path -> 400" 400 '.missing | index("path")' \
  '{"taskId":"border-tax","source":"web","params":{'"$UPP"'}}'
post "web + path=banana -> 400" 400 '.error == "unsupported_path"' \
  '{"taskId":"border-tax","source":"web","path":"banana","params":{'"$UPP"'}}'
post "web + fullyAutomated + UK -> 400" 400 '.message | test("not fully automated")' \
  '{"taskId":"border-tax","source":"web","path":"fullyAutomated","params":{"requestId":"burnin-neg-uk","driverId":"'$DRIVER'","vehicleNumber":"UK07AB1234","state":"UK","taxMode":"DAYS","taxFrom":"'$TAXFROM'"}}'
# Gate-state case: pick any registered state NOT currently allow-listed; if
# every state is scripted-enabled there is nothing to gate — skip cleanly.
GATE_STATE=$(curl -s "$BASE/api/health" | jq -r '[.states[] | select(.scriptedEnabled == false)][0].code // empty')
if [ -n "$GATE_STATE" ]; then
  post "web + scripted + non-allow-listed state ($GATE_STATE) -> 400 env hint" 400 \
    '.message | test("SCRIPTED_BORDER_TAX_STATES")' \
    '{"taskId":"border-tax","source":"web","path":"scripted","params":{"requestId":"burnin-neg-gate","driverId":"'$DRIVER'","vehicleNumber":"'$GATE_STATE'01AB1234","state":"'$GATE_STATE'","taxMode":"DAYS","taxFrom":"'$TAXFROM'"}}'
else
  echo "  --  gate-state case skipped (every state is scripted-enabled)"
fi
post "puc + web -> 400 app only" 400 '.error | test("app only")' \
  '{"taskId":"puc-certificate","source":"web","params":{"requestId":"burnin-neg-puc","driverId":"'$DRIVER'","registrationNumber":"'$VEHICLE'","chassisNumber":"ABCDE12345"}}'
# TN's no-DAYS rule fires at the validator, which is only reachable when TN
# is allow-listed; otherwise the allow-list gate rejects first. Both are
# correct 400s and neither enqueues, so accept either.
post "TN + DAYS -> 400 (validator or allow-list gate)" 400 \
  '.error == "validation_failed" or .error == "unsupported_path"' \
  '{"taskId":"border-tax","source":"web","path":"scripted","params":{"requestId":"burnin-neg-tn","driverId":"'$DRIVER'","vehicleNumber":"TN01AB1234","state":"TN","taxMode":"DAYS","taxFrom":"'$TAXFROM'"}}'

if [ "$ENQUEUE" = "1" ]; then
  RID_A="burnin-auto-$(date +%s)"
  RID_S="burnin-scripted-$(date +%s)"
  echo "── REAL runs (enqueue!) — watch the dashboard ──"
  post "REAL app fullyAutomated queued" 200 \
    '.ok == true and .status == "queued" and .path == "fullyAutomated"' \
    '{"taskId":"border-tax","source":"app","params":{"requestId":"'$RID_A'","driverId":"'$DRIVER'","vehicleNumber":"'$VEHICLE'","state":"UP","taxMode":"DAYS","taxFrom":"'$TAXFROM'","taxUpto":"'$TAXUPTO'","paymentMethod":"UPI","entryDistrict":"SAHARANPUR","mobileNumber":"9999999999"}}'
  post "REAL web scripted queued (state must be allow-listed)" 200 \
    '.ok == true and .status == "queued" and .path == "scripted"' \
    '{"taskId":"border-tax","source":"web","path":"scripted","params":{"requestId":"'$RID_S'","driverId":"'$DRIVER'","vehicleNumber":"'$VEHICLE'","state":"UP","taxMode":"DAYS","taxFrom":"'$TAXFROM'","taxUpto":"'$TAXUPTO'","entryDistrict":"SAHARANPUR","mobileNumber":"9999999999"}}'
  echo "queued: $RID_A (auto) · $RID_S (scripted — go solve it on the live view)"
  echo "judge BOTH in Firestore from the app's point of view: aiAgentData.status,"
  echo "aiAgentData.path, waitReason during humanHandover, receiptDocumentUrl."
fi

echo
echo "passed=$PASS failed=$FAIL"
[ "$FAIL" = "0" ]
