"""Challan number -> Virtual Courts "Select Department" mapping.

Rule (same as the legacy challan-settlement Phase 1.5):
  - challan starts with a DIGIT               -> Delhi(Notice Department)
  - challan starts with 2 LETTERS             -> state code -> department below
  - anything else / code not in the dropdown  -> unmapped (reported, skipped)

The department strings below are the EXACT <option> texts from the live
vcourts.gov.in dropdown (July 2026 snapshot supplied by the user) — the runner
selects by option text (engine.steps.select_by_text, case-insensitive substring
on option text), so these must stay verbatim. Codes that exist as challan
prefixes but have NO department in the dropdown (AP, TS/TG, BR, JH, GA, ...)
are intentionally absent: they land in `unmapped` and are surfaced in the run
summary instead of silently guessing.

KEEP IN SYNC with api/src/internal/challanSettlement.ts (STATE_TO_DEPARTMENT).
"""

from __future__ import annotations

from .params import normalize_id

DELHI_NOTICE = "Delhi(Notice Department)"

# state prefix -> dropdown option text (verbatim).
STATE_TO_DEPARTMENT: dict[str, str] = {
    "AS": "Assam(Traffic Department)",
    "CH": "Chandigarh(Traffic Department)",
    "CG": "Chhattisgarh(Traffic Department)",
    "DL": "Delhi(Traffic Department)",
    "GJ": "Gujarat(Traffic Department)",
    "HR": "Haryana(Traffic Department)",
    "HP": "Himachal Pradesh(Traffic Department)",
    "JK": "Jammu and Kashmir(Jammu Traffic Department)",
    "KA": "Karnataka(Traffic Department)",
    "KL": "Kerala(Police Department)",
    "MP": "Madhya Pradesh(Traffic Department)",
    "MH": "Maharashtra(Transport Department)",
    "MN": "Manipur(Traffic Department)",
    "ML": "Meghalaya(Traffic Department)",
    "OD": "Odisha(Odisha Traffic CTC-BBSR Commissionerate)",
    "PB": "Punjab(Traffic Department)",
    "RJ": "Rajasthan(Traffic Department)",
    "TN": "Tamil Nadu(Traffic Department)",
    "TR": "Tripura(Traffic Department)",
    "UK": "Uttarakhand(Traffic Department)",
    "UP": "Uttar Pradesh(Traffic Department)",
    "WB": "West Bengal(Traffic Department)",
}


def department_for_challan(challan_no: str) -> str | None:
    """Return the department option text for one challan, or None if unmapped."""
    cn = normalize_id(challan_no)
    if not cn:
        return None
    if cn[0].isdigit():
        return DELHI_NOTICE
    prefix = cn[:2]
    return STATE_TO_DEPARTMENT.get(prefix)


def plan_departments(
    challan_numbers: list[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """Build the department query plan from the client's challan list.

    Returns (plan, unmapped):
      plan     — ordered dict: department option text -> [original challan
                 strings that pointed at it] (insertion order = query order)
      unmapped — original challan strings with no department in the dropdown
    """
    plan: dict[str, list[str]] = {}
    unmapped: list[str] = []
    for original in challan_numbers:
        dept = department_for_challan(original)
        if dept is None:
            unmapped.append(original)
            continue
        plan.setdefault(dept, []).append(original)
    return plan, unmapped
