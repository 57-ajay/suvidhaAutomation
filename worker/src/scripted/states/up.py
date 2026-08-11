# worker/src/scripted/states/up.py
"""Uttar Pradesh border-tax SCRIPTED runner (web source, human handover).

Phases 1-5 are the shared parivahan wizard (scripted.wizard) with UP's flags,
then scripted.handover takes over: the operator solves the captcha, picks the
payment method, and pays on the live view while we watch for the receipt.

UP flags (mirroring the proven fully-automated up.py):
  vehicle_info   Vehicle Category left untouched (RC pre-fills it); Permit
                 Type only if empty (primary -> fallback); Service Type set
                 UNCONDITIONALLY (UP behavior), no first-option fallback;
                 no Distance field.
  tax_info       Tax Mode by visible text only (UP lists DAYS); plain
                 type="date" inputs (runtime-detected); DAYS fills Tax Upto.
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

open_portal = make_open_portal("UP", "up")

vehicle_info = make_vehicle_info(
    set_category_if_empty=False,
    has_distance=False,
    service_set_if_empty=False,  # UP sets Service Type unconditionally
    service_first_option_fallback=False,
    manual_has_category=True,
    manual_has_distance=False,
    manual_datetime_dates=False,
)

tax_info = make_tax_info(state_code="UP", tax_mode_values=None)

HANDOVER_CONFIG = HandoverConfig(
    state_name="Uttar Pradesh",
    # Receipt-page-exclusive markers ONLY — no page before the receipt
    # contains any of them, and the old "government of <state>" header
    # marker broke silently twice: MP's receipt says "Transport Department
    # MADHYA PRADESH" (no "Government of"), and UK's says "GOVERNMENT OF
    # UTTRAKHAND" (the portal's own spelling, no second 'A') — both made
    # every receipt time out despite being on screen (verified against
    # real receipt PDFs, 2026-08-11).
    receipt_markers=[
        "checkpost tax e-receipt",
        "receipt no",
        "grand total",
    ],
    negative_patterns=list(DEFAULT_NEGATIVE_PATTERNS),
)

PHASES: list[Phase] = [
    Phase("open_portal", open_portal, enter_status=Status.AI_AGENT_STARTED, max_secs=150),
    Phase("select_service", select_service, max_secs=150),
    Phase("owner_info", owner_info, max_secs=420),  # incl. pending-clear
    Phase("vehicle_info", vehicle_info, max_secs=180),
    Phase("tax_info", tax_info, max_secs=180),
    make_handover_phase(HANDOVER_CONFIG),
]
