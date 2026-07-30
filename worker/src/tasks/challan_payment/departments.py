"""Challan number -> Virtual Courts "Select Department" for ONE challan.

Deliberately a thin wrapper over tasks.challan_settlement.departments rather
than a second copy of the table: both tasks drive the SAME dropdown on the same
page, so a divergence between them would be a silent mis-route (paying against
the wrong court) rather than a visible bug. The settlement table is already
annotated "KEEP IN SYNC with api/src/internal/challanSettlement.ts"; this makes
it one table instead of three.

The only thing added here is the explicit `department` param override, which is
the seam for challans whose prefix is not in the dropdown.
"""

from __future__ import annotations

from tasks.challan_settlement.departments import (  # noqa: F401  (re-export)
    DELHI_NOTICE,
    STATE_TO_DEPARTMENT,
    department_for_challan,
)


def department_for_payment(challan_no: str, override: str | None = None) -> str | None:
    """Department option text for this challan, or None when unmapped.

    `override` (the API `department` param) always wins, so an operator can
    force a court the prefix rule doesn't cover without a code change.
    """
    if override and override.strip():
        return override.strip()
    return department_for_challan(challan_no)
