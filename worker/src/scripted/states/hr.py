# worker/src/scripted/states/hr.py
"""Haryana border-tax SCRIPTED runner (web source, human handover).

HR flags (mirroring the proven fully-automated hr.py):
  vehicle_info   Vehicle Category set-if-empty (first real option); Permit
                 Type only if empty; Service Type set-if-empty with a
                 first-option fallback; Distance filled when empty
                 (params.distance, default "1000") — HR is the only state
                 with this field.
  tax_info       Tax Mode by visible text, HR <option value> fallback map
                 (labels carry a leading space; HR codes differ from the
                 generic parivahan map — MONTHLY=4, not 3); datetime-local
                 Tax From/Upto (runtime-detected; tax_dates stamps the IST
                 time); DAYS fills Tax Upto, other modes are portal-locked.
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

open_portal = make_open_portal("HR", "hr")

vehicle_info = make_vehicle_info(
    set_category_if_empty=True,
    has_distance=True,
    service_set_if_empty=True,
    service_first_option_fallback=True,
    manual_has_category=True,
    manual_has_distance=True,
    manual_datetime_dates=False,
)

_HR_TAX_MODE_VALUES = {
    "DAYS": "1",
    "MONTHLY": "4",
    "QUARTERLY": "5",
    "HALF YEARLY": "6",
    "YEARLY": "7",
}

tax_info = make_tax_info(state_code="HR", tax_mode_values=_HR_TAX_MODE_VALUES)

HANDOVER_CONFIG = HandoverConfig(
    state_name="Haryana",
    receipt_markers=[
        "government of haryana",
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
