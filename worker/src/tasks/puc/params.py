"""Typed view of the PUC job params.

Same contract as BorderTaxParams: the API validator is the gatekeeper, so by
the time a job reaches the worker these fields are present and normalized. A
missing required field here means a contract bug, not user error, and cancels
the run (nothing irreversible happens) with an internal-error message.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.types import ScriptedAbort

_REQUIRED = (
    "requestId",
    "driverId",
    "registrationNumber",
    "chassisNumber",  # the worker only uses the last 5; see chassisLast5
)


@dataclass(frozen=True)
class PucParams:
    requestId: str
    driverId: str
    registrationNumber: str
    chassisNumber: str  # full chassis as provided; portal wants last 5
    mobileNumber: str = ""  # not used by the lookup; kept for the record
    source: str = "app"

    @property
    def chassisLast5(self) -> str:
        """The portal's Chassis Number field is maxlength=5 ('*Enter last 5
        character'). Take the last 5 of whatever was provided, uppercased."""
        return self.chassisNumber.strip().upper()[-5:]

    @property
    def regn(self) -> str:
        return self.registrationNumber.strip().upper()

    @classmethod
    def from_job(cls, raw: dict) -> "PucParams":
        missing = [k for k in _REQUIRED if not str(raw.get(k, "")).strip()]
        if missing:
            raise ScriptedAbort(
                "internal error: PUC job reached the worker without validated "
                f"parameter(s): {', '.join(missing)}",
                terminal="cancelled",
            )
        return cls(
            requestId=str(raw["requestId"]),
            driverId=str(raw["driverId"]),
            registrationNumber=str(raw["registrationNumber"]),
            chassisNumber=str(raw["chassisNumber"]),
            mobileNumber=str(raw.get("mobileNumber", "")),
            source=str(raw.get("source", "app")),
        )
