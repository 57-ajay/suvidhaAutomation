# worker/src/scripted/states/br.py
"""Bihar border-tax SCRIPTED runner (web source, human handover).

New to this repo — state knowledge ported from the old repo's
scripted/border_tax/br.py + _handover_runner.

BR specifics (from the old runner):
  owner_info     Entry District default PATNA (validator). BR's checkpost
                 names do NOT match district names (place names or "NOT
                 APPLICABLE") — old "first_option" strategy; the shared
                 helper's first-option fallback carries it.
  vehicle_info   Vehicle Category set-if-empty; Permit Type only if empty
                 (default TEMPORARY PERMIT); Service Type NOT APPLICABLE,
                 set-if-empty with first-option fallback; NO Distance field.
  tax_info       Tax Mode DAYS (default) / QUARTERLY / YEARLY — visible text
                 first, generic value fallback (DAYS=1, QUARTERLY=5,
                 YEARLY=7); a mode the RC doesn't get aborts with
                 abort_reason="tax_mode_not_offered_for_rc" + the offered
                 list. The old repo's own sources DISAGREE on the Tax
                 From/Upto input type (the handover runner's field notes say
                 type="date", br.py's docstring says datetime-local) —
                 runtime detection in scripted.tax_dates decides on the live
                 field either way; DAYS fills Tax Upto.
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

open_portal = make_open_portal("BR", "br")

vehicle_info = make_vehicle_info(
    set_category_if_empty=True,
    has_distance=False,
    service_set_if_empty=True,
    service_first_option_fallback=True,
    manual_has_category=True,
    manual_has_distance=False,
    manual_datetime_dates=False,
)

_BR_TAX_MODE_VALUES = {"DAYS": "1", "QUARTERLY": "5", "YEARLY": "7"}

tax_info = make_tax_info(
    state_code="BR",
    tax_mode_values=_BR_TAX_MODE_VALUES,
    list_offered_on_miss=True,
)

HANDOVER_CONFIG = HandoverConfig(
    state_name="Bihar",
    receipt_markers=[
        "government of bihar",
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
