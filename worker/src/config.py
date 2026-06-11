# worker/src/config.py
"""Worker configuration from environment."""

from __future__ import annotations

import os

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
API_URL = os.environ.get("API_URL", "http://api:3000")
DOMAIN = os.environ.get("DOMAIN", "localhost")

MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "5"))
JOB_TTL = 60 * 60 * 24

# Base X display number; slot i uses display BASE_DISPLAY + i.
BASE_DISPLAY = int(os.environ.get("BASE_DISPLAY", "99"))
# x11vnc port for slot i = BASE_VNC_PORT + i. websockify maps tokens -> these.
BASE_VNC_PORT = int(os.environ.get("BASE_VNC_PORT", "5900"))
SCREEN_GEOMETRY = os.environ.get("SCREEN_GEOMETRY", "1280x800x24")

# Feature flags
AUTO_CLEAR_PENDING = os.environ.get("AUTO_CLEAR_PENDING", "false").lower() in (
    "1",
    "true",
    "yes",
)
MAX_USER_CAPTCHA_ATTEMPTS = int(os.environ.get("MAX_USER_CAPTCHA_ATTEMPTS", "3"))

# Per-status max dwell time in seconds before auto-terminal.
CAPTCHA_WAIT_SECS = int(os.environ.get("CAPTCHA_WAIT_SECS", "300"))
QR_WAIT_SECS = int(os.environ.get("QR_WAIT_SECS", "360"))
PAYMENT_VERIFY_SECS = int(os.environ.get("PAYMENT_VERIFY_SECS", "120"))

# Optional egress proxy for .nic.in per-IP filtering.
EGRESS_PROXY = os.environ.get("EGRESS_PROXY", "").strip()
