#!/usr/bin/env bash
# tools/api-matrix-harness/run.sh — executes the /api/run source/path matrix
# against the CURRENT repo code (not a snapshot): copies the live api/src
# route + validators + FSM + config into a temp sandbox, adds recording stubs
# for redis/http/requestDoc/challan, bundles with esbuild, runs in node.
# 21 assertions across both allow-list modes; exits non-zero on any failure.
#
# Usage (from repo root or anywhere):  tools/api-matrix-harness/run.sh
# Override the source tree:            API_SRC=/path/to/api/src run.sh
# Needs: node >= 18, npx (esbuild fetched on first run).
#
# NOTE: fixtures use fixed future-ish dates; if a validator ever grows a
# taxFrom freshness guard, bump the dates in harness.ts.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
API_SRC="${API_SRC:-$HERE/../../api/src}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/src/routes" "$TMP/src/validators/states" \
         "$TMP/src/lifecycle" "$TMP/src/lib" "$TMP/src/internal"
cp "$API_SRC/routes/run.ts"            "$TMP/src/routes/"
cp "$API_SRC/validators/index.ts"      "$API_SRC/validators/types.ts" "$TMP/src/validators/"
cp "$API_SRC"/validators/states/*.ts   "$TMP/src/validators/states/"
cp "$API_SRC/lifecycle/statuses.ts"    "$TMP/src/lifecycle/"
cp "$API_SRC/config.ts"                "$TMP/src/"
cp "$HERE/stubs/redis.ts"              "$TMP/src/redis.ts"
cp "$HERE/stubs/http.ts"               "$TMP/src/lib/http.ts"
cp "$HERE/stubs/requestDoc.ts"         "$TMP/src/internal/requestDoc.ts"
cp "$HERE/stubs/challanSettlementRun.ts" "$TMP/src/routes/challanSettlementRun.ts"
cp "$HERE/harness.ts"                  "$TMP/src/harness.ts"

npx --yes esbuild "$TMP/src/harness.ts" --bundle --platform=node \
    --format=esm --outfile="$TMP/out.mjs" >/dev/null

echo "── enabled mode (SCRIPTED_BORDER_TAX_STATES=UP,UK,TN) ──"
SCRIPTED_BORDER_TAX_STATES="UP,UK,TN" node "$TMP/out.mjs"
echo "── disabled mode (allow-list empty) ──"
HARNESS_MODE=disabled SCRIPTED_BORDER_TAX_STATES= node "$TMP/out.mjs"
