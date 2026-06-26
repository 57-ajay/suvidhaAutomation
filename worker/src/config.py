"""Worker configuration from environment. Every tunable lives here."""

from __future__ import annotations

import os


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
API_URL = os.environ.get("API_URL", "http://localhost:3000")
DOMAIN = os.environ.get("DOMAIN", "localhost")
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")

MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "3"))
JOB_TTL = 60 * 60 * 24

# Slot i runs on display :{BASE_DISPLAY+i}; its x11vnc on BASE_VNC_PORT+i.
BASE_DISPLAY = int(os.environ.get("BASE_DISPLAY", "99"))
BASE_VNC_PORT = int(os.environ.get("BASE_VNC_PORT", "5900"))
SCREEN_GEOMETRY = os.environ.get("SCREEN_GEOMETRY", "1280x800x24")
VNC_TOKEN_DIR = os.environ.get("VNC_TOKEN_DIR", "/tmp/vnc-tokens")

# Human-seam budgets (seconds).
CAPTCHA_WAIT_SECS = int(os.environ.get("CAPTCHA_WAIT_SECS", "300"))
QR_WAIT_SECS = int(os.environ.get("QR_WAIT_SECS", "360"))
PAYMENT_VERIFY_SECS = int(os.environ.get("PAYMENT_VERIFY_SECS", "120"))

# Pause after every successful click/fill/select. Gov portals are slow
# Angular apps; giving them a beat between actions avoids racing re-renders.
STEP_DELAY_SECS = float(os.environ.get("STEP_DELAY_SECS", "0.8"))

# Hard ceiling for one job, end to end. Covers the worst legitimate path
# (pending-clear + restart + 3 captcha attempts + full payment window) with
# headroom; anything past it is a wedged run, terminated by the money-line
# rule. The orchestrator hard-kills the subprocess at this + 120s grace.
JOB_MAX_RUNTIME_SECS = int(os.environ.get("JOB_MAX_RUNTIME_SECS", "2400"))

# User-solved captcha attempts before a hard cancel (no money has moved).
MAX_USER_CAPTCHA_ATTEMPTS = int(os.environ.get("MAX_USER_CAPTCHA_ATTEMPTS", "3"))


# Pending-transaction auto-clear (Vertex-OCR background captcha). OFF by
# default: a pending transaction then cancels with a clear retry message.
AUTO_CLEAR_PENDING = _bool("AUTO_CLEAR_PENDING", "false")


# Captcha solving policy (global). "ai" tries Vertex OCR first and SILENTLY
# (no captcha image uploaded, no captcha status written) and falls back to the
# human seam only if OCR exhausts MAX_AI_CAPTCHA_ATTEMPTS. "human" goes straight
# to the user. Redis-overridable later; env-only for now.
def _captcha_mode() -> str:
    m = os.environ.get("CAPTCHA_MODE", "ai").strip().lower()
    return m if m in ("ai", "human") else "ai"


CAPTCHA_MODE = _captcha_mode()

# Silent OCR attempts before falling back to a human (ai / fallback path).
MAX_AI_CAPTCHA_ATTEMPTS = int(os.environ.get("MAX_AI_CAPTCHA_ATTEMPTS", "5"))

# Vertex AI (the only LLM path; ADC via the GCE metadata server — no key file).
VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "cabswale-ai")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3-flash-preview")

# Optional egress proxy that owns the IP allow-listed with .nic.in.
EGRESS_PROXY = os.environ.get("EGRESS_PROXY", "").strip()

# When set, every captcha image the worker captures is written here as PNG
# (mount it as a volume, or `docker cp`, to retrieve). Debug only.
CAPTCHA_DEBUG_DIR = os.environ.get("CAPTCHA_DEBUG_DIR", "").strip()

# PUC-certificate task. Whole-flow restarts from the portal open on any
# transient failure; the captcha budget + per-phase deadlines bound the rest.
PUC_MAX_RETRIES = int(os.environ.get("PUC_MAX_RETRIES", "2"))
