"""State -> runner registry, the worker-side mirror of the API's validator
registry. Adding a state: write states/<code>.py exporting PHASES, register
it here, and add its validator on the API. Nothing else changes."""

from __future__ import annotations

from engine.pipeline import Phase
from engine.types import ScriptedAbort

from .states import up
from .verify_pending import make_verify_phases

_RUNNERS: dict[str, list[Phase]] = {
    "UP": up.PHASES,
}

_VERIFY_CONFIGS = {
    "UP": up._UP_PAYMENT_CONFIG,  # reuse the state's receipt markers
}


def resolve_verify_phases(state: str) -> list[Phase]:
    key = _ALIASES.get((state or "").strip().upper(), (state or "").strip().upper())
    cfg = _VERIFY_CONFIGS.get(key)
    if not cfg:
        raise ScriptedAbort(f"no verify runner for state '{state}'", terminal="failed")
    return make_verify_phases(cfg)


_ALIASES: dict[str, str] = {
    "UTTAR PRADESH": "UP",
    "UTTARPRADESH": "UP",
    "U.P.": "UP",
}


def resolve_phases(state: str) -> list[Phase]:
    key = (state or "").strip().upper()
    key = _ALIASES.get(key, key)
    phases = _RUNNERS.get(key)
    if not phases:
        raise ScriptedAbort(
            f"no runner for state '{state}' — supported: {', '.join(sorted(_RUNNERS))}",
            terminal="cancelled",
        )
    return phases
