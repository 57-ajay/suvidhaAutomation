#!/usr/bin/env bash
# worker/entrypoint.sh
#
# Brings up the noVNC front (websockify with the token plugin) and then the
# orchestrator. Per-job Xvfb/x11vnc are started by the slot pool; websockify maps
# each job's token to its slot's VNC port via files in VNC_TOKEN_DIR.
set -euo pipefail

export VNC_TOKEN_DIR="${VNC_TOKEN_DIR:-/tmp/vnc-tokens}"
mkdir -p "$VNC_TOKEN_DIR"

NOVNC_DIR="${NOVNC_DIR:-/usr/share/novnc}"
WEBSOCKIFY_PORT="${WEBSOCKIFY_PORT:-6080}"

echo "[entrypoint] starting websockify on :${WEBSOCKIFY_PORT} (token dir=${VNC_TOKEN_DIR})"
websockify --web="${NOVNC_DIR}" \
    --token-plugin=TokenFile --token-source="${VNC_TOKEN_DIR}" \
    "${WEBSOCKIFY_PORT}" &

# Give websockify a moment to bind.
sleep 1

echo "[entrypoint] starting orchestrator"
cd /app/src
exec uv run python main.py
