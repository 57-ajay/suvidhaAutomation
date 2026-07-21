# worker/src/scripted/states/mp.py
"""Madhya Pradesh border-tax SCRIPTED runner (web source, human handover).

MP rides UP's exact wizard shape (the proven fully-automated mp.py reuses
UP's vehicle_info and tax_info verbatim): no Distance field, Vehicle Category
left to the RC, Service Type set unconditionally, DAYS-only tax mode on plain
type="date" inputs. MP's checkpost names don't track the district, so the
shared checkpost helper's first-option fallback carries it. Only the state
dropdown value and the receipt markers are MP's own.
"""

from __future__ import annotations

from engine.pipeline import Phase
from lifecycle.status import Status

from ..handover import DEFAULT_NEGATIVE_PATTERNS, HandoverConfig, make_handover_phase
from ..wizard import make_open_portal, owner_info, select_service
from .up import tax_info, vehicle_info  # scripted UP's, not the auto module's

open_portal = make_open_portal("MP", "mp")

HANDOVER_CONFIG = HandoverConfig(
    state_name="Madhya Pradesh",
    receipt_markers=[
        "government of madhya pradesh",
        "checkpost tax e-receipt",
        "receipt no",
        "grand total",
    ],
    negative_patterns=list(DEFAULT_NEGATIVE_PATTERNS),
)

PHASES: list[Phase] = [
    Phase("open_portal", open_portal, enter_status=Status.AI_AGENT_STARTED, max_secs=150),
    Phase("select_service", select_service, max_secs=150),
    Phase("owner_info", owner_info, max_secs=300),
    Phase("vehicle_info", vehicle_info, max_secs=180),
    Phase("tax_info", tax_info, max_secs=180),
    make_handover_phase(HANDOVER_CONFIG),
]
