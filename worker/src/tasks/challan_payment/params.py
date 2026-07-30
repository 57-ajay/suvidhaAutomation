"""ChallanPaymentParams: validated job parameters for the challan-payment
runner.

Contract with api/src/routes/challanPaymentRun.ts:
    required: vehicleNumber, challanNo, requestId
    optional: chassisNo, engineNo, phoneNo, department, driverId, source

Conventions (mirrors ChallanSettlementParams / PucParams):
  - ONE JOB PER CHALLAN. requestId == the challan number EXACTLY as the client
    wrote it, because that is the subChallans doc id:

        challans/{vehicleNumber}/subChallans/{requestId}

    vehicleNumber is therefore required too — it is the parent doc key, not a
    label. Both are needed to address the doc; neither is optional.
  - challanNo is kept alongside requestId for readability in logs and the
    portal fill. The API guarantees they are the same string.
  - driverId defaults to "na": the subChallan doc is keyed by vehicle+challan,
    but /api/internal/job-completed still requires a non-empty driverId.
  - phoneNo / chassisNo are optional at the type level only — the open_record
    phase aborts with a readable message when either is missing, because the
    portal's verification step needs both.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def normalize_id(s: str) -> str:
    """Canonical form used for matching challan/vehicle identifiers."""
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


class ChallanPaymentParams(BaseModel):
    # ── required ──────────────────────────────────────────────────────────
    requestId: str = Field(min_length=1)  # == the subChallans doc id
    vehicleNumber: str = Field(min_length=4, max_length=20)  # parent doc key
    challanNo: str = Field(min_length=1)

    # ── optional ──────────────────────────────────────────────────────────
    chassisNo: str | None = None
    engineNo: str | None = None
    phoneNo: str | None = None
    # If provided, used VERBATIM as the Virtual Courts "Select Department"
    # option text. Omitted -> derived from the challan prefix by
    # tasks.challan_payment.departments.department_for_payment.
    department: str | None = None

    # ── plumbing ──────────────────────────────────────────────────────────
    driverId: str = "na"
    source: Literal["app", "web"] = "web"

    # ── validators ────────────────────────────────────────────────────────
    @field_validator("vehicleNumber")
    @classmethod
    def _normalize_vehicle(cls, v: str) -> str:
        n = normalize_id(v)
        if not (4 <= len(n) <= 12):
            raise ValueError(f"vehicle number '{v}' normalizes to '{n}' (bad length)")
        return n

    @field_validator("challanNo", "requestId")
    @classmethod
    def _strip(cls, v: str) -> str:
        # NOT normalized: requestId is a Firestore doc id and must round-trip
        # to exactly what the client wrote.
        return (v or "").strip()

    @field_validator("phoneNo")
    @classmethod
    def _normalize_phone(cls, v: str | None) -> str | None:
        if not v:
            return None
        digits = re.sub(r"\D", "", v)
        return digits[-10:] if digits else None  # strips +91 / spaces

    @field_validator("driverId", mode="before")
    @classmethod
    def _default_driver(cls, v) -> str:
        s = str(v or "").strip()
        return s if s else "na"

    # ── helpers ───────────────────────────────────────────────────────────

    def chassis_last4(self) -> str:
        return (self.chassisNo or "").strip()[-4:]

    @classmethod
    def from_job(cls, raw: dict) -> "ChallanPaymentParams":
        return cls(**raw)
