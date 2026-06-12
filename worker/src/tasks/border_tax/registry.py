"""State -> runner registry, the worker-side mirror of the API's validator
registry. Adding a state: write states/<code>.py exporting PHASES, register
it here, and add its validator on the API. Nothing else changes."""

from __future__ import annotations

from engine.pipeline import Phase
from engine.types import ScriptedAbort

from .states import up

_RUNNERS: dict[str, list[Phase]] = {
    "UP": up.PHASES,
}

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
            f"no runner for state '{state}' — supported: "
            f"{', '.join(sorted(_RUNNERS))}",
            terminal="cancelled",
        )
    return phases
