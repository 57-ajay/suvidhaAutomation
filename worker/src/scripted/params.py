# worker/src/scripted/params.py
"""Border-tax params as seen by the worker.

The API already validated and normalized these before enqueueing, so this is a
thin typed view plus a defensive parse. If a required field is somehow missing
here it's a bug upstream, not bad user input — we fail the run loudly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BorderTaxParams:
    vehicleNumber: str
    state: str
    stateCode: str
    taxMode: str
    taxFrom: str
    taxUpto: str
    entryDistrict: str
    requestId: str
    driverId: str
    mobileNumber: str
    paymentMethod: str = "upi"
    source: str = "app"
    entryCheckpoint: str = ""
    permitType: str = ""
    permitTypeFallback: str = "ALL INDIA TOURIST PERMIT"
    serviceType: str = "Air Conditioned Service"

    @staticmethod
    def from_dict(d: dict) -> "BorderTaxParams":
        required = [
            "vehicleNumber", "stateCode", "taxMode", "taxFrom",
            "entryDistrict", "requestId", "driverId", "mobileNumber",
        ]
        missing = [k for k in required if not d.get(k)]
        if missing:
            raise ValueError(f"worker received params missing: {missing}")
        return BorderTaxParams(
            vehicleNumber=d["vehicleNumber"],
            state=d.get("state", ""),
            stateCode=d["stateCode"],
            taxMode=d["taxMode"],
            taxFrom=d["taxFrom"],
            taxUpto=d.get("taxUpto", ""),
            entryDistrict=d["entryDistrict"],
            requestId=d["requestId"],
            driverId=d["driverId"],
            mobileNumber=d["mobileNumber"],
            paymentMethod=d.get("paymentMethod", "upi"),
            source=d.get("source", "app"),
            entryCheckpoint=d.get("entryCheckpoint", ""),
            permitType=d.get("permitType", ""),
            permitTypeFallback=d.get("permitTypeFallback", "ALL INDIA TOURIST PERMIT"),
            serviceType=d.get("serviceType", "Air Conditioned Service"),
        )
