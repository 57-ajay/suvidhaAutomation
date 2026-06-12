#!/usr/bin/env bash
# Brings up the noVNC front (websockify + token plugin), then the orchestrator.
# Per-job Xvfb/x11vnc are started by the slot pool; websockify maps each job's
# token to its slot's VNC port via files in VNC_TOKEN_DIR.
set -euo pipefail

export VNC_TOKEN_DIR="${VNC_TOKEN_DIR:-/tmp/vnc-tokens}"
mkdir -p "$VNC_TOKEN_DIR"

NOVNC_DIR="${NOVNC_DIR:-/usr/share/novnc}"
WEBSOCKIFY_PORT="${WEBSOCKIFY_PORT:-6080}"

echo "[entrypoint] websockify on :${WEBSOCKIFY_PORT} (tokens=${VNC_TOKEN_DIR})"
websockify --web="${NOVNC_DIR}" \
    --token-plugin=TokenFile --token-source="${VNC_TOKEN_DIR}" \
    "${WEBSOCKIFY_PORT}" &

sleep 1

echo "[entrypoint] starting orchestrator"
exec uv run python src/main.py
