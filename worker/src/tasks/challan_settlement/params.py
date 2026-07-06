"""ChallanSettlementParams: validated job parameters for the challan-settlement
runner.

Contract with api/src/routes/run.ts (handleChallanSettlementRun):
    required: vehicleNumber, challanNumbers (>=1), requestId
    optional: driverId, source

Conventions (mirrors BorderTaxParams / PucParams):
  - requestId == normalized vehicle number. The API's per-task doc template for
    "challan-settlement" is `challans/{requestId}`, so every status-update /
    job-completed write lands on the vehicle's challans doc.
  - driverId defaults to "na" — the challans doc is keyed by vehicle only, but
    the internal endpoints (status-update, job-completed) require a non-empty
    driverId, so we always carry one.
  - challanNumbers are normalized (strip non-alphanumerics, uppercase) for
    MATCHING; the ORIGINAL client strings are preserved so subChallans doc ids
    match what the client will read back. See `requested_map()`.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def normalize_id(s: str) -> str:
    """Canonical form used for matching challan/vehicle identifiers."""
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


class ChallanSettlementParams(BaseModel):
    # ── required ──────────────────────────────────────────────────────────
    vehicleNumber: str = Field(min_length=4, max_length=20)
    challanNumbers: list[str] = Field(min_length=1)
    requestId: str = Field(min_length=1)

    # ── plumbing ──────────────────────────────────────────────────────────
    driverId: str = "na"
    source: Literal["app", "web"] = "web"

    @field_validator("vehicleNumber")
    @classmethod
    def _normalize_vehicle(cls, v: str) -> str:
        n = normalize_id(v)
        if not (4 <= len(n) <= 12):
            raise ValueError(f"vehicle number '{v}' normalizes to '{n}' (bad length)")
        return n

    @field_validator("challanNumbers", mode="before")
    @classmethod
    def _coerce_list(cls, v):
        # Redis job params arrive JSON-decoded, but tolerate a JSON string or a
        # comma-separated string from a manual dashboard run.
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    v = json.loads(s)
                except Exception:
                    v = [s]
            else:
                v = [p for p in re.split(r"[,\s]+", s) if p]
        if isinstance(v, (int, float)):
            v = [str(v)]
        return v

    @field_validator("challanNumbers")
    @classmethod
    def _clean_challans(cls, v: list) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in v:
            original = str(item).strip()
            norm = normalize_id(original)
            if not (5 <= len(norm) <= 35):
                raise ValueError(
                    f"challan '{original}' has invalid length after normalization"
                )
            if norm in seen:
                continue  # silently dedupe duplicates from the client
            seen.add(norm)
            out.append(original)
        if not out:
            raise ValueError("challanNumbers is empty after cleaning")
        return out

    @field_validator("driverId", mode="before")
    @classmethod
    def _default_driver(cls, v) -> str:
        s = str(v or "").strip()
        return s if s else "na"

    # ── helpers ───────────────────────────────────────────────────────────

    def requested_map(self) -> dict[str, str]:
        """normalized challan id -> ORIGINAL client string (subChallans doc id
        for matched records, so the client can read back exactly what it sent)."""
        return {normalize_id(c): c for c in self.challanNumbers}

    @classmethod
    def from_job(cls, raw: dict) -> "ChallanSettlementParams":
        return cls(**raw)
