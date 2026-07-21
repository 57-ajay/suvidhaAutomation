# worker/src/scripted/states/tn.py
"""Tamil Nadu border-tax SCRIPTED runner (web source, human handover).

New to this repo — state knowledge ported from the old repo's
scripted/border_tax/tn.py + _handover_runner.

TN specifics (from the old runner):
  owner_info     Entry District default KRISHNAGIRI (validator).
                 ⚠️ GUESS carried over from the old repo — TN's district
                 dropdown was never captured; KRISHNAGIRI (Hosur border, the
                 high-traffic Karnataka->TN cab entry) is a placeholder. A
                 miss falls back to the first district in the list. Confirm
                 against the live portal and update the validator default.
                 Checkposts don't track the district (old "first_option"
                 strategy) — the shared helper's first-option fallback
                 carries it.
  vehicle_info   Vehicle Category set-if-empty (LPV is typically the sole
                 real option); Permit Type only if empty (default ALL INDIA
                 TOURIST PERMIT, fallback CONTRACT CARRIAGE PERMIT — this
                 drives the Permit Fee line on the tax table); Service Type
                 NOT APPLICABLE, set-if-empty with first-option fallback;
                 NO Distance field.
  tax_info       TN offers WEEKLY (2) / MONTHLY (4) / QUARTERLY (5) — there
                 is NO DAYS option, so Tax Upto is ALWAYS portal-derived
                 (fills_tax_upto is False for every valid TN mode; the
                 validator strips taxUpto). Visible-text match carries all
                 three (note TN MONTHLY=4, not the generic 3 — the value map
                 below is TN's own); a mode the RC doesn't get aborts with
                 abort_reason="tax_mode_not_offered_for_rc" + the offered
                 list. Tax From is plain type="date" (runtime-detected).
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

open_portal = make_open_portal("TN", "tn")

vehicle_info = make_vehicle_info(
    set_category_if_empty=True,
    has_distance=False,
    service_set_if_empty=True,
    service_first_option_fallback=True,
    manual_has_category=True,
    manual_has_distance=False,
    manual_datetime_dates=False,
)

# TN's own option values (old tn.py): WEEKLY=2, MONTHLY=4, QUARTERLY=5.
_TN_TAX_MODE_VALUES = {"WEEKLY": "2", "MONTHLY": "4", "QUARTERLY": "5"}

tax_info = make_tax_info(
    state_code="TN",
    tax_mode_values=_TN_TAX_MODE_VALUES,
    list_offered_on_miss=True,
)

HANDOVER_CONFIG = HandoverConfig(
    state_name="Tamil Nadu",
    receipt_markers=[
        "government of tamil nadu",
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
