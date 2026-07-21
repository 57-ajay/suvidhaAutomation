# worker/src/scripted/states/uk.py
"""Uttarakhand border-tax SCRIPTED runner (web source, human handover).

New to this repo — the state knowledge is ported from the old repo's
scripted/border_tax/uk.py + _handover_runner (the same parivahan wizard;
the flow itself is scripted.wizard).

UK specifics (from the old runner):
  owner_info     Entry District default DEHRADUN (validator); UK's checkpost
                 names (ASHARODI / KULHAL / TIMLI / TUNI) do NOT track the
                 district, so the desired value misses and the shared helper
                 falls to the FIRST option — the old "first_option" strategy.
  vehicle_info   Vehicle Category set-if-empty; Permit Type only if empty
                 (default TEMPORARY PERMIT, fallback ALL INDIA TOURIST
                 PERMIT); Service Type set-if-empty (default "Air Conditioned
                 Service") with first-option fallback; NO Distance field.
  tax_info       Tax Mode is RC-DEPENDENT — the portal renders different
                 modes per vehicle. Visible text first, generic parivahan
                 value fallback (DAYS=1, QUARTERLY=5, YEARLY=7); if the
                 requested mode isn't offered for this RC the run aborts
                 with abort_reason="tax_mode_not_offered_for_rc" listing
                 exactly what the portal offered. Tax From/Upto input types
                 are runtime-detected; DAYS fills Tax Upto.

NET-BANKING NOTE: UK's gateway offers "SBI (Multi Bank Payment)" net banking
only — no UPI/QR. That is entirely the operator's problem on this path: they
complete the net-banking payment during the handover; we only watch for the
receipt.
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

open_portal = make_open_portal("UK", "uk")

vehicle_info = make_vehicle_info(
    set_category_if_empty=True,
    has_distance=False,
    service_set_if_empty=True,
    service_first_option_fallback=True,
    manual_has_category=True,
    manual_has_distance=False,
    manual_datetime_dates=False,
)

# Generic parivahan option values (old repo's shared map). Text match runs
# first; the offered-list abort handles a mode the RC simply doesn't get.
_UK_TAX_MODE_VALUES = {"DAYS": "1", "QUARTERLY": "5", "YEARLY": "7"}

tax_info = make_tax_info(
    state_code="UK",
    tax_mode_values=_UK_TAX_MODE_VALUES,
    list_offered_on_miss=True,
)

HANDOVER_CONFIG = HandoverConfig(
    state_name="Uttarakhand",
    receipt_markers=[
        "government of uttarakhand",
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
