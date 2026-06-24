"""Shared Tax From / Tax Upto handling for the border-tax states.

WHY THIS EXISTS
The CheckPost portals split into two families for the validity fields:

  * datetime-local states (PB, HR): #floatingTaxfrom / #uptpDate accept
    "YYYY-MM-DDTHH:MM". We stamp the CURRENT IST time of day so a permit
    starts at the moment we submit, not at midnight. A midnight start expires
    the previous night, so a driver crossing later that day is left uncovered;
    starting at "now" gives a full window forward from submission. Billing is
    exact-24h based (a minute past a whole day costs an extra day), so the
    SAME time is stamped on BOTH ends and the From->Upto span stays an exact
    24h multiple. PB/HR are NO_SAME_DAY: in DAYS mode the API already sets
    taxUpto = taxFrom + duration (>= tomorrow), which keeps Tax Upto above the
    portal's "min pinned to now" even with a now-time stamp.

  * date states (UP, MP): #floatingTaxfrom / #uptpDate are plain
    <input type="date"> and reject a time component, so we send the bare ISO
    date (these are SAME_DAY; the API sets taxUpto = taxFrom + duration - 1).

The date itself is the driver's requested taxFrom (already normalized and
freshness-guarded by the API validator); here we only decide the TIME.

CONFIG + RUNTIME DETECTION
STATE_USES_TIME records intent per state. The live field's `type` (read via
get_input_type) is the actual source of truth for the format, so a state we
forget to add still fills correctly, and a portal that flips a field type
trips a loud log instead of silently pushing a malformed value. Keep the map
as documentation + drift alarm; add new states there.

NOTE ON "now": the time is read here, in the worker, at fill time -- which is
when the permit goes live, giving the driver the full window forward. If you
ever want it anchored to API-receive time instead, have the validator emit a
full "YYYY-MM-DDTHH:MM" taxFrom/taxUpto; _stamp() is idempotent and will pass
it straight through (date states would then need the time trimmed).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.log import StepLogger
from engine.steps import fill, get_input_type
from engine.types import StepLog, StepStatus

# IST is a fixed +05:30 with no DST, so a fixed offset is correct year-round
# and needs no tzdata package on the host.
_IST = timezone(timedelta(hours=5, minutes=30))

# Declared per-state time policy for the Tax From/Upto fields.
#   True  -> datetime-local; stamp the current IST time of day.
#   False -> plain date; send the bare ISO date (no time).
# Runtime detection is authoritative for the format; this map documents intent
# and flags drift. A state absent here falls back to the live field type.
STATE_USES_TIME: dict[str, bool] = {
    "PB": True,
    "HR": True,
    "UP": False,
    "MP": False,
}


def ist_hhmm(now: datetime | None = None) -> str:
    """Current IST wall-clock as 'HH:MM' (datetime-local is minute precision)."""
    return (now or datetime.now(_IST)).strftime("%H:%M")


def _note(log: StepLogger, name: str, value: str) -> None:
    log.record(
        StepLog(index=log.next_index(), name=name, status=StepStatus.OK, value=value)
    )


def _stamp(iso_date: str, hhmm: str | None) -> str:
    """Format one date for the field: append 'T<hhmm>' for datetime-local, or
    return the bare date when hhmm is None. Idempotent -- a value that already
    carries a time (contains 'T') is returned unchanged, so a caller that one
    day sends a full datetime is respected rather than double-stamped."""
    s = (iso_date or "").strip()
    if not s or hhmm is None:
        return s
    return s if "T" in s else f"{s}T{hhmm}"


async def _uses_time(session, state: str, sel_from: str, log: StepLogger) -> bool:
    """Whether to stamp a time, reconciling the live field type with the
    STATE_USES_TIME map. The DOM wins; the map only validates + documents, and
    only the exceptional paths log (the happy path stays quiet)."""
    key = (state or "").strip().upper()
    declared = STATE_USES_TIME.get(key)
    field_type = await get_input_type(session, sel_from)

    if field_type is None:
        # Couldn't read the field. Trust declared intent if we have it, else
        # assume date-only -- the safe default never pushes a time into a plain
        # date input (which would fail the fill outright).
        use_time = bool(declared)
        _note(
            log,
            "p5.tax_date_type_unreadable",
            f"{key}: Tax From type unreadable; using STATE_USES_TIME="
            f"{declared} -> use_time={use_time}",
        )
        return use_time

    detected = field_type == "datetime-local"
    if declared is None:
        _note(
            log,
            "p5.tax_date_unconfigured",
            f"{key}: not in STATE_USES_TIME; live type={field_type!r} "
            f"-> use_time={detected}",
        )
    elif declared != detected:
        _note(
            log,
            "p5.tax_date_config_drift",
            f"{key}: STATE_USES_TIME={declared} but live type={field_type!r}; "
            f"trusting DOM -> use_time={detected}",
        )
    return detected


async def fill_tax_dates(
    session,
    state: str,
    sel_from: str,
    sel_upto: str,
    tax_from: str,
    tax_upto: str,
    *,
    fills_upto: bool,
    log: StepLogger,
) -> None:
    """Fill Tax From (always) and Tax Upto (only when `fills_upto` -- DAYS
    mode) for one state.

    For datetime-local states the current IST time is computed ONCE here and
    stamped on both ends, so From and Upto share the same minute and the span
    stays an exact 24h multiple (the portal bills a minute over a whole day as
    a full extra day). For date states the bare ISO date is sent unchanged.
    The stamped value lands in the fill step's own log line.
    """
    use_time = await _uses_time(session, state, sel_from, log)
    hhmm = ist_hhmm() if use_time else None  # one read, reused for both ends

    await fill(
        session, sel_from, _stamp(tax_from, hhmm), log=log, name="p5.fill_tax_from"
    )
    if fills_upto:
        await fill(
            session, sel_upto, _stamp(tax_upto, hhmm), log=log, name="p5.fill_tax_upto"
        )
