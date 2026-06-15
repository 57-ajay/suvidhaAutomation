"""Typed view of the job params. The API validator is the gatekeeper; by the
time a job reaches a worker these fields are guaranteed present and
normalized. A missing required field here therefore means a contract bug, not
user error — the run cancels (no money has moved) with an internal-error
message instead of limping into the portal with bad data.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.types import ScriptedAbort

_REQUIRED = (
    "requestId",
    "driverId",
    "vehicleNumber",
    "state",
    "taxMode",
    "taxFrom",
    "entryDistrict",
)


@dataclass(frozen=True)
class BorderTaxParams:
    requestId: str
    driverId: str
    vehicleNumber: str
    mobileNumber: str
    state: str
    stateCode: str
    paymentMethod: str
    taxMode: str
    taxFrom: str
    taxUpto: str
    entryDistrict: str
    entryCheckpoint: str
    permitType: str
    permitTypeFallback: str
    serviceType: str

    @property
    def fills_tax_upto(self) -> bool:
        """True only for DAYS mode — for MONTHLY/QUARTERLY/YEARLY the portal
        computes and locks Tax Upto itself, and writing it would override
        the portal's value behind the UI lock."""
        return self.taxMode.strip().upper() == "DAYS"

    @classmethod
    def from_job(cls, raw: dict) -> "BorderTaxParams":
        missing = [k for k in _REQUIRED if not str(raw.get(k, "")).strip()]
        # taxUpto is part of the contract only in DAYS mode.
        if (
            str(raw.get("taxMode", "")).strip().upper() == "DAYS"
            and not str(raw.get("taxUpto", "")).strip()
        ):
            missing.append("taxUpto")
        if missing:
            raise ScriptedAbort(
                "internal error: job reached the worker without validated "
                f"parameter(s): {', '.join(missing)}",
                terminal="cancelled",
            )
        return cls(
            requestId=str(raw["requestId"]),
            driverId=str(raw["driverId"]),
            vehicleNumber=str(raw["vehicleNumber"]),
            mobileNumber=str(raw.get("mobileNumber", "")),
            state=str(raw["state"]),
            stateCode=str(raw.get("stateCode", "")),
            paymentMethod=str(raw.get("paymentMethod", "UPI")),
            taxMode=str(raw["taxMode"]),
            taxFrom=str(raw["taxFrom"]),
            taxUpto=str(raw.get("taxUpto", "")),
            entryDistrict=str(raw["entryDistrict"]),
            entryCheckpoint=str(raw.get("entryCheckpoint", "")),
            permitType=str(raw.get("permitType", "TEMPORARY PERMIT")),
            permitTypeFallback=str(
                raw.get("permitTypeFallback", "ALL INDIA TOURIST PERMIT"),
            ),
            serviceType=str(raw.get("serviceType", "Air Conditioned Service")),
        )
