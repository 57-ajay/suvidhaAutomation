# worker/src/scripted/states/pb.py
"""Punjab border-tax SCRIPTED runner (web source, human handover).

PB flags (mirroring the proven fully-automated pb.py):
  vehicle_info   Vehicle Category set-if-empty; Permit Type only if empty;
                 Service Type set-if-empty with first-option fallback;
                 NO Distance field.
  tax_info       Tax Mode by visible text (no PB value map in the proven
                 runner); datetime-local Tax From/Upto (runtime-detected);
                 DAYS fills Tax Upto.
"""

from __future__ import annotations

from engine.pipeline import Phase
from lifecycle.status import Status

from ..handover import DEFAULT_NEGATIVE_PATTERNS, HandoverConfig, make_handover_phase
from ..wizard import (
    make_open_portal,
    make_tax_info,
    make_vehicle_info,
    owner_info,
    select_service,
)

open_portal = make_open_portal("PB", "pb")

vehicle_info = make_vehicle_info(
    set_category_if_empty=True,
    has_distance=False,
    service_set_if_empty=True,
    service_first_option_fallback=True,
    manual_has_category=True,
    manual_has_distance=False,
    manual_datetime_dates=False,
)

tax_info = make_tax_info(state_code="PB", tax_mode_values=None)

HANDOVER_CONFIG = HandoverConfig(
    state_name="Punjab",
    receipt_markers=[
        "government of punjab",
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
