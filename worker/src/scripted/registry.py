# worker/src/scripted/registry.py
"""State -> SCRIPTED runner registry, the worker-side mirror of the API's
scripted allow-list (config.scriptedStates / SCRIPTED_BORDER_TAX_STATES).

Adding a scripted state: write states/<code>.py exporting PHASES, register it
here, add its validator on the API, and put its code in the env allow-list.
Nothing else changes — run_job dispatches path=="scripted" through
resolve_phases exactly like the fully-automated registry.
"""

from __future__ import annotations

from engine.pipeline import Phase
from engine.types import ScriptedAbort

from .states import br, hp, hr, mp, pb, tn, uk, up

_RUNNERS: dict[str, list[Phase]] = {
    "UP": up.PHASES,
    "HR": hr.PHASES,
    "MP": mp.PHASES,
    "PB": pb.PHASES,
    "HP": hp.PHASES,
    "UK": uk.PHASES,
    "TN": tn.PHASES,
    "BR": br.PHASES,
}

_ALIASES: dict[str, str] = {
    "UTTAR PRADESH": "UP",
    "UTTARPRADESH": "UP",
    "U.P.": "UP",
    "HARYANA": "HR",
    "MADHYA PRADESH": "MP",
    "PUNJAB": "PB",
    "HIMACHAL PRADESH": "HP",
    "H.P.": "HP",
    "UTTARAKHAND": "UK",
    "U.K.": "UK",
    "TAMIL NADU": "TN",
    "TAMILNADU": "TN",
    "T.N.": "TN",
    "BIHAR": "BR",
}


def resolve_phases(state: str) -> list[Phase]:
    key = (state or "").strip().upper()
    key = _ALIASES.get(key, key)
    phases = _RUNNERS.get(key)
    if not phases:
        raise ScriptedAbort(
            f"no scripted runner for state '{state}' — supported: "
            f"{', '.join(sorted(_RUNNERS))}",
            terminal="cancelled",
        )
    return phases
