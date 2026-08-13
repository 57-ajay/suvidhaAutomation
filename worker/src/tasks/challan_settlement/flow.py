"""Challan-settlement runner: one pipeline phase per Virtual Courts department.

Unlike border-tax (fixed phase list per state) the phase list here is built at
dispatch time from the client's challan numbers — make_phases(params) returns

    [ start, dept_1_<slug>, ..., dept_N_<slug>, finalize ]

Department phases are FULLY ISOLATED: every exception (bad selector, captcha
budget exhausted, portal error, internal timeout) is caught inside the phase,
recorded as that department's result, and the pipeline moves on. Only `start`
and `finalize` can end the run — and finalize always tries to clear the
aiCheckingSettlement flag first, whatever happened before it. (The API also
drops the flag on EVERY terminal write — see applyTerminal — so a hard worker
crash can't strand the client at "checking".)

vcourts error surfaces are Bootstrap MODALS, not native alerts (confirmed live
on Jul 2026): both "Invalid Captcha..." and "This number does not exist" render
as a `.modal` with an `.alert-danger` body. document.body.innerText only
includes VISIBLE elements, so the marker checks see the modal exactly while it
is open — but a stale open modal would poison every later check, so we
force-dismiss visible modals before each submit and after each rejection.

Captcha: securimage <img>, solved by the shared task-agnostic solver
(tasks.border_tax.captcha_user.solve_captcha) in AI mode. Wrong captcha ->
the solver refreshes and retries up to CHALLAN_SETTLEMENT_CAPTCHA_ATTEMPTS
(default 5; falls back to the global MAX_AI_CAPTCHA_ATTEMPTS if the
captcha_user patch isn't applied). Exhaustion -> that department is skipped
(captcha_failed) and the run continues. Human fallback is OFF by default —
there is no client captcha UI on the challans doc — and can be enabled with
CHALLAN_SETTLEMENT_HUMAN_CAPTCHA=true once one exists.

"This number does not exist" -> NOT a failure: the department has nothing for
this vehicle, so every client challan routed to it is concluded to be with a
PHYSICAL court. Those challans are saved as quotation { amount: null,
physicalCourt: true } and the department closes as skipped (not_found). The
same physical-court mark is written for a client challan that is absent from a
department's results page, one the portal shows as "Transferred to Regular
Court", and (in finalize) challans with no vcourts department at all. Challans
the portal DOES show but as Paid / Disposed / Warrant / proceedings-pending
get NO write — they exist on Virtual Courts, they just aren't settleable. And
when a department page fails to load (no_response / timeout / captcha
exhausted) nothing is written for its challans either: we could not conclude
anything about them.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time

import api_client
from engine.pipeline import Phase
from engine.steps import (
    cdp_eval,
    click,
    fill,
    navigate,
    select_by_text,
    sleep_seconds,
    wait_for_selector,
)
from engine.types import RunContext, RunOutcome, ScriptedAbort, StepLog, StepStatus
from lifecycle.status import Status
from tasks.border_tax.captcha_user import solve_captcha

from . import selectors as S
from .departments import plan_departments
from .extract import (
    INVALID_CAPTCHA_MARKERS,
    NOT_FOUND_MARKERS,
    RESULTS_MARKER,
    build_quotation_records,
    extract_raw_records,
)
from .params import ChallanSettlementParams, normalize_id

# ── tunables ─────────────────────────────────────────────────────────────
DEPT_PHASE_MAX_SECS = 600.0  # outer pipeline deadline per department
DEPT_INNER_BUDGET = 540.0  # inner budget — trips FIRST so the failure is
# caught in-phase (skipped dept) instead of
# aborting the whole run via the pipeline timer
RESULTS_POLL_SECS = 45.0  # after Submit: wait for results / not-found
PAGE_SETTLE_SECS = 1.5

# Wrong-captcha retries for THIS task (the solver refreshes + retries
# internally). Passed through to solve_captcha when the captcha_user patch
# (max_ai_attempts param) is applied; otherwise the global
# MAX_AI_CAPTCHA_ATTEMPTS (also default 5) governs.
CAPTCHA_ATTEMPTS = int(os.environ.get("CHALLAN_SETTLEMENT_CAPTCHA_ATTEMPTS", "5"))
_SOLVE_EXTRA_KW: dict = (
    {"max_ai_attempts": CAPTCHA_ATTEMPTS}
    if "max_ai_attempts" in inspect.signature(solve_captcha).parameters
    else {}
)

HUMAN_CAPTCHA_FALLBACK = os.environ.get(
    "CHALLAN_SETTLEMENT_HUMAN_CAPTCHA", "false"
).lower() in ("1", "true", "yes")

# Belt-and-braces: also trap any native alert()/confirm() (harmless if unused —
# the live site uses Bootstrap modals, handled below).
_ALERT_TRAP_JS = (
    "(function(){"
    "  if(window.__csTrap) return true;"
    "  window.__csTrap = true; window.__csAlerts = [];"
    "  window.alert = function(m){ window.__csAlerts.push(String(m)); };"
    "  window.confirm = function(m){ window.__csAlerts.push(String(m)); return true; };"
    "  return true;"
    "})()"
)

# Text of any VISIBLE modal alert (e.g. "Invalid Captcha...", "This number
# does not exist"). Hidden modals have zero rects and are excluded.
_MODAL_TEXT_JS = (
    "(function(){"
    "  var out = [];"
    "  document.querySelectorAll('.modal .alert-danger, .modal .alert-success')"
    "    .forEach(function(el){"
    "      var r = el.getBoundingClientRect();"
    "      if(r.width > 0 && r.height > 0){ out.push(el.innerText || ''); }"
    "    });"
    "  return out.join(' | ');"
    "})()"
)

# Force-close every visible Bootstrap modal: click its dismiss control (the
# site-native path), then hard-hide (.show removed, display:none) and clear
# the backdrop so a half-finished fade can never block the next step.
_DISMISS_MODAL_JS = (
    "(function(){"
    "  var n = 0;"
    "  document.querySelectorAll('.modal').forEach(function(m){"
    "    var r = m.getBoundingClientRect();"
    "    if(!(r.width > 0 && r.height > 0)) return;"
    "    var b = m.querySelector('[data-bs-dismiss=\"modal\"], .close');"
    "    if(b){ try{ b.click(); }catch(e){} }"
    "    m.classList.remove('show');"
    "    m.style.display = 'none';"
    "    n++;"
    "  });"
    "  document.querySelectorAll('.modal-backdrop').forEach(function(bd){ bd.remove(); });"
    "  document.body.classList.remove('modal-open');"
    "  return n;"
    "})()"
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]


async def _page_text(ctx: RunContext) -> str:
    """Visible body text + visible modal alerts + any trapped native alerts,
    lowercased, for marker checks."""
    try:
        body = (
            await cdp_eval(ctx.session, "document.body ? document.body.innerText : ''")
            or ""
        )
        modal = await cdp_eval(ctx.session, _MODAL_TEXT_JS) or ""
        alerts = (
            await cdp_eval(ctx.session, "(window.__csAlerts||[]).join(' | ')") or ""
        )
        return f"{body}\n{modal}\n{alerts}".lower()
    except Exception:
        return ""


async def _dismiss_modals(ctx: RunContext, where: str) -> None:
    try:
        n = await cdp_eval(ctx.session, _DISMISS_MODAL_JS)
        if n:
            _log(ctx, f"{where}.dismiss_modal", StepStatus.OK, value=f"closed {n}")
    except Exception:
        pass


def _log(
    ctx: RunContext,
    name: str,
    status: StepStatus,
    value: str = "",
    error: str | None = None,
):
    ctx.log.record(
        StepLog(
            index=ctx.log.next_index(),
            name=name,
            status=status,
            value=value[:500] if value else None,
            error=error[:500] if error else None,
        )
    )


# ── phase: start ─────────────────────────────────────────────────────────


async def _start(ctx: RunContext) -> None:
    p: ChallanSettlementParams = ctx.params
    plan, unmapped = plan_departments(p.challanNumbers)

    ctx.scratch["plan"] = plan  # dept -> [client challans]
    ctx.scratch["unmapped"] = unmapped  # client challans w/o dept
    ctx.scratch["requested_map"] = p.requested_map()  # norm id -> client string
    ctx.scratch["matched"] = set()  # norm ids we quoted
    ctx.scratch["physical"] = set()  # norm ids marked physical-court (null quotation)
    ctx.scratch["dept_results"] = []  # per-dept result dicts
    ctx.scratch["saved_matched"] = 0
    ctx.scratch["saved_extra"] = 0
    ctx.scratch["saved_physical"] = 0

    # Idempotent re-assert (the API already set it at enqueue) — also covers a
    # job fired straight from the ops dashboard.
    resp = await api_client.challan_settlement_flag(
        request_id=p.requestId,
        driver_id=p.driverId,
        vehicle_number=p.vehicleNumber,
        checking=True,
        requested_challans=p.challanNumbers,
    )
    _log(
        ctx,
        "start.flag_checking",
        StepStatus.OK if resp.get("ok") else StepStatus.FAILED,
        value=f"depts={list(plan)} unmapped={unmapped}",
        error=None if resp.get("ok") else str(resp.get("error")),
    )
    if not plan:
        # Nothing to query; finalize will clear the flag + report unmapped.
        _log(
            ctx,
            "start.no_departments",
            StepStatus.FAILED,
            value=f"no vcourts department for any of {p.challanNumbers}",
        )


# ── phase body: one department ───────────────────────────────────────────


def _dept_result(
    dept: str,
    status: str,
    *,
    reason: str = "",
    saved: int = 0,
    matched: int = 0,
    extra: int = 0,
    physical: int = 0,
    dropped: int = 0,
) -> dict:
    return {
        "department": dept,
        "status": status,
        "reason": reason,
        "saved": saved,
        "matched": matched,
        "extra": extra,
        "physical": physical,
        "dropped": dropped,
    }


async def _save_quotations(ctx: RunContext, dept: str, records: list[dict]) -> dict:
    """save_challan_quotations with one retry (transient API hiccups)."""
    p: ChallanSettlementParams = ctx.params
    resp = await api_client.save_challan_quotations(
        job_id=ctx.job_id,
        request_id=p.requestId,
        driver_id=p.driverId,
        vehicle_number=p.vehicleNumber,
        department=dept,
        records=records,
    )
    if not resp.get("ok"):
        resp = await api_client.save_challan_quotations(
            job_id=ctx.job_id,
            request_id=p.requestId,
            driver_id=p.driverId,
            vehicle_number=p.vehicleNumber,
            department=dept,
            records=records,
        )
    return resp


def _physical_records(ctx: RunContext, originals: list[str]) -> list[dict]:
    """Physical-court save records (quotation amount null + physicalCourt flag)
    for client challans concluded NOT to be on Virtual Courts. Skips anything
    already quoted or already marked in an earlier phase."""
    matched: set[str] = ctx.scratch.get("matched", set())
    physical: set[str] = ctx.scratch.get("physical", set())
    out: list[dict] = []
    seen: set[str] = set()
    for original in originals:
        norm = normalize_id(original)
        if norm in matched or norm in physical or norm in seen:
            continue
        seen.add(norm)
        out.append(
            {
                "challanId": original,  # client's ORIGINAL string == doc id
                "amount": None,
                "isExtra": False,
                "physicalCourt": True,
            }
        )
    return out


def _record_physical_saved(ctx: RunContext, records: list[dict]) -> None:
    marked: set[str] = ctx.scratch.setdefault("physical", set())
    for rec in records:
        marked.add(normalize_id(rec["challanId"]))
    ctx.scratch["saved_physical"] = ctx.scratch.get("saved_physical", 0) + len(records)


async def _department_inner(ctx: RunContext, dept: str, prefix: str) -> dict:
    """Full cycle for one department. Raises are handled by the wrapper."""
    p: ChallanSettlementParams = ctx.params
    veh = p.vehicleNumber

    # A — index.php: select department, Proceed Now.
    await navigate(ctx.session, S.VC_HOME, log=ctx.log, name=f"{prefix}.open_home")
    await wait_for_selector(
        ctx.session,
        S.SEL_DEPT_DROPDOWN,
        log=ctx.log,
        name=f"{prefix}.wait_dropdown",
        timeout=30,
    )
    await select_by_text(
        ctx.session,
        S.SEL_DEPT_DROPDOWN,
        dept,
        log=ctx.log,
        name=f"{prefix}.select_department",
        timeout=15,
    )
    await click(ctx.session, S.SEL_PROCEED, log=ctx.log, name=f"{prefix}.proceed")

    # B — main.php: police tab, vehicle number, captcha.
    await wait_for_selector(
        ctx.session,
        S.SEL_TAB_POLICE,
        log=ctx.log,
        name=f"{prefix}.wait_main",
        timeout=30,
    )
    await cdp_eval(ctx.session, _ALERT_TRAP_JS)
    await click(ctx.session, S.SEL_TAB_POLICE, log=ctx.log, name=f"{prefix}.tab_police")
    await wait_for_selector(
        ctx.session,
        S.SEL_VEHICLE_INPUT,
        log=ctx.log,
        name=f"{prefix}.wait_vehicle_input",
        timeout=20,
    )
    await fill(
        ctx.session,
        S.SEL_VEHICLE_INPUT,
        veh,
        log=ctx.log,
        name=f"{prefix}.fill_vehicle",
    )
    await sleep_seconds(PAGE_SETTLE_SECS, log=ctx.log, name=f"{prefix}.settle")

    async def _submit_and_check() -> bool:
        """Close any leftover modal, re-fill the vehicle field (a rejected
        captcha resets the form — legacy-flow lesson), click Submit, poll for
        a decisive outcome. Returns True on results OR the not-found modal
        (both decisive); False on the invalid-captcha modal or silence."""
        await _dismiss_modals(ctx, prefix)
        try:
            await fill(
                ctx.session,
                S.SEL_VEHICLE_INPUT,
                veh,
                log=ctx.log,
                name=f"{prefix}.refill_vehicle",
            )
        except Exception:
            pass
        try:
            await click(ctx.session, S.SEL_SUBMIT, log=ctx.log, name=f"{prefix}.submit")
        except Exception:
            return False
        deadline = time.monotonic() + RESULTS_POLL_SECS
        while time.monotonic() < deadline:
            text = await _page_text(ctx)
            if RESULTS_MARKER.lower() in text:
                return True
            if any(m in text for m in NOT_FOUND_MARKERS):
                return True  # decisive outcome — not a captcha failure
            if any(m in text for m in INVALID_CAPTCHA_MARKERS):
                # Close it NOW so the solver's next attempt starts clean.
                await _dismiss_modals(ctx, prefix)
                return False
            await asyncio.sleep(1.0)
        return False

    async def _is_rejected() -> bool:
        """Decisive-first: results or not-found present -> NOT rejected, even
        if stale error text lingers somewhere. Invalid-captcha modal (or no
        response at all) -> rejected; dismiss so the retry starts clean."""
        text = await _page_text(ctx)
        if RESULTS_MARKER.lower() in text or any(m in text for m in NOT_FOUND_MARKERS):
            return False
        await _dismiss_modals(ctx, prefix)
        return True

    try:
        await solve_captcha(
            ctx,
            status=Status.CAPTCHA_SOLVING,  # human-fallback path only
            stage="challan-settlement",
            input_selector=S.SEL_CAPTCHA_INPUT,
            refresh_selector=S.SEL_CAPTCHA_REFRESH,
            image_selector=S.SEL_CAPTCHA_IMAGE,
            submit_action=_submit_and_check,
            is_rejected=_is_rejected,
            mode="ai",
            human_fallback=HUMAN_CAPTCHA_FALLBACK,
            terminal="cancelled",
            abort_message=f"captcha could not be solved for {dept}",
            **_SOLVE_EXTRA_KW,
        )
    except ScriptedAbort as e:
        return _dept_result(dept, "skipped", reason=f"captcha_failed: {e.reason}")

    # C — read the outcome. Do NOT dismiss before reading: the not-found
    # signal IS the visible modal text.
    text = await _page_text(ctx)
    if any(m in text for m in NOT_FOUND_MARKERS) and RESULTS_MARKER.lower() not in text:
        await _dismiss_modals(ctx, prefix)
        # "This number does not exist" == this department has NOTHING for the
        # vehicle, so every client challan routed here is with a physical
        # court: persist quotation { amount: null, physicalCourt: true }.
        physical = _physical_records(ctx, ctx.scratch.get("plan", {}).get(dept, []))
        if physical:
            resp = await _save_quotations(ctx, dept, physical)
            if not resp.get("ok"):
                return _dept_result(
                    dept,
                    "failed",
                    reason=f"physical_save_failed: {resp.get('error', 'unknown')}",
                )
            _record_physical_saved(ctx, physical)
            for rec in physical:
                _log(
                    ctx,
                    f"{prefix}.record",
                    StepStatus.OK,
                    value=f"{rec['challanId']} PHYSICAL-COURT (not on vcourts)",
                )
        return _dept_result(
            dept, "skipped", reason="not_found", physical=len(physical)
        )
    if RESULTS_MARKER.lower() not in text:
        # Page never produced a decisive outcome (site failed to load / hung):
        # we could not conclude anything, so NO physical-court marks here.
        return _dept_result(dept, "skipped", reason="no_response")

    await _dismiss_modals(ctx, prefix)  # results path: clear any leftover overlay
    raws = await extract_raw_records(ctx.session)
    valid, dropped = build_quotation_records(raws)

    from collections import Counter

    breakdown = Counter(d.get("reason", "?") for d in dropped)
    _log(
        ctx,
        f"{prefix}.extract",
        StepStatus.OK,
        value=f"raw={len(raws)} valid={len(valid)} dropped={len(dropped)} "
        f"[{', '.join(f'{k}:{v}' for k, v in breakdown.items())}]",
    )

    # D — match against the client's list; build the save payload.
    #     matched  -> doc id = the client's ORIGINAL challan string,
    #                 payload { quotation only }
    #     extra    -> doc id = the portal's challan string,
    #                 payload { quotation + challanAmount (== settlement) }
    #     physical -> requested challan ABSENT from this department's results,
    #                 or shown as "Transferred to Regular Court": quotation
    #                 { amount: null, physicalCourt: true }. Challans the
    #                 portal DID show as Paid / Disposed / Warrant / pending
    #                 get NO write — they are on vcourts, just not settleable.
    requested_map: dict[str, str] = ctx.scratch["requested_map"]
    records: list[dict] = []
    n_matched = n_extra = 0
    for rec in valid:
        norm = normalize_id(rec["challanId"])
        client_original = requested_map.get(norm)
        is_extra = client_original is None
        records.append(
            {
                "challanId": client_original if client_original else rec["challanId"],
                "amount": rec["amount"],
                "isExtra": is_extra,
            }
        )
        if is_extra:
            n_extra += 1
        else:
            n_matched += 1
            ctx.scratch["matched"].add(norm)
        _log(
            ctx,
            f"{prefix}.record",
            StepStatus.OK,
            value=f"{rec['challanId']} amount={rec['amount']} "
            f"{'EXTRA' if is_extra else 'matched'} "
            f"fine={rec.get('fine')} offence={rec.get('offence', '')[:60]}",
        )

    # Physical-court determination for THIS department's requested challans:
    # absent from the results entirely, or present but transferred to a
    # regular court -> null quotation + physicalCourt. Any other drop reason
    # (paid / disposed / warrant / pending / unreadable) -> leave untouched.
    valid_norms = {normalize_id(r["challanId"]) for r in valid}
    dropped_reasons: dict[str, str] = {}
    for d in dropped:
        norm = normalize_id(d.get("challanId") or "")
        if norm and norm not in dropped_reasons:
            dropped_reasons[norm] = d.get("reason", "")

    physical: list[dict] = []
    untouched: list[str] = []
    for original in ctx.scratch.get("plan", {}).get(dept, []):
        norm = normalize_id(original)
        if norm in valid_norms or norm in ctx.scratch["matched"]:
            continue
        if norm in ctx.scratch["physical"]:
            continue
        drop_reason = dropped_reasons.get(norm)
        if drop_reason is None or drop_reason == "skip_transferred_regular_court":
            physical.append(
                {
                    "challanId": original,
                    "amount": None,
                    "isExtra": False,
                    "physicalCourt": True,
                }
            )
            _log(
                ctx,
                f"{prefix}.record",
                StepStatus.OK,
                value=f"{original} PHYSICAL-COURT "
                f"({'transferred to regular court' if drop_reason else 'not on vcourts'})",
            )
        else:
            untouched.append(f"{original}({drop_reason})")
    if untouched:
        _log(
            ctx,
            f"{prefix}.not_settleable",
            StepStatus.OK,
            value=f"on vcourts but not settleable, no write: {', '.join(untouched)}",
        )
    records.extend(physical)

    if not records:
        # Nothing to quote AND nothing to mark physical (e.g. every requested
        # challan here was shown but Paid/Disposed).
        reason = "0_records" if not raws else "no_settleable_records"
        return _dept_result(dept, "skipped", reason=reason, dropped=len(dropped))

    # E — save (one retry).
    resp = await _save_quotations(ctx, dept, records)
    if not resp.get("ok"):
        return _dept_result(
            dept,
            "failed",
            reason=f"save_failed: {resp.get('error', 'unknown')}",
            matched=n_matched,
            extra=n_extra,
            dropped=len(dropped),
        )

    ctx.scratch["saved_matched"] += n_matched
    ctx.scratch["saved_extra"] += n_extra
    _record_physical_saved(ctx, physical)
    if not valid:
        # Results page answered fine but held nothing settleable; the
        # physical-court marks landed. Same legit-skip reasons as before so
        # finalize's outcome mapping is unchanged.
        reason = "0_records" if not raws else "no_settleable_records"
        return _dept_result(
            dept,
            "skipped",
            reason=reason,
            saved=len(records),
            physical=len(physical),
            dropped=len(dropped),
        )
    return _dept_result(
        dept,
        "confirmed",
        saved=len(records),
        matched=n_matched,
        extra=n_extra,
        physical=len(physical),
        dropped=len(dropped),
    )


def _make_department_phase(dept: str, index: int) -> Phase:
    prefix = f"dept{index}_{_slug(dept)}"

    async def _run(ctx: RunContext) -> None:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                _department_inner(ctx, dept, prefix), timeout=DEPT_INNER_BUDGET
            )
        except asyncio.TimeoutError:
            result = _dept_result(dept, "failed", reason="dept_timeout")
        except ScriptedAbort as e:
            result = _dept_result(dept, "failed", reason=f"aborted: {e.reason}")
        except Exception as e:
            result = _dept_result(dept, "failed", reason=f"{type(e).__name__}: {e}")
        result["secs"] = round(time.monotonic() - started, 1)
        ctx.scratch.setdefault("dept_results", []).append(result)
        _log(
            ctx,
            f"{prefix}.result",
            StepStatus.OK
            if result["status"] == "confirmed"
            else StepStatus.FAILED
            if result["status"] == "failed"
            else StepStatus.RETRIED,
            value=json.dumps(result),
        )
        return None  # always continue to the next department

    return Phase(name=prefix, run=_run, max_secs=DEPT_PHASE_MAX_SECS)


# ── phase: finalize ──────────────────────────────────────────────────────

_LEGIT_SKIPS = {"not_found", "0_records", "no_settleable_records"}


def _is_legit_skip(reason: str) -> bool:
    return reason.split(":")[0].strip() in _LEGIT_SKIPS


async def _finalize(ctx: RunContext) -> RunOutcome:
    p: ChallanSettlementParams = ctx.params
    results: list[dict] = ctx.scratch.get("dept_results", [])
    unmapped: list[str] = ctx.scratch.get("unmapped", [])
    requested_map: dict[str, str] = ctx.scratch.get("requested_map", {})
    matched: set[str] = ctx.scratch.get("matched", set())

    confirmed = [r for r in results if r["status"] == "confirmed"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]
    hard_skips = [r for r in skipped if not _is_legit_skip(r["reason"])]

    # Unmapped challans have NO Virtual Courts department at all, so they are
    # definitionally not settleable on vcourts: mark them physical-court too.
    # Best-effort — a save failure here is noted in the summary but never
    # blocks clearing the flag or closing the run.
    physical: set[str] = ctx.scratch.setdefault("physical", set())
    unmapped_failed = False
    unmapped_records = _physical_records(ctx, unmapped)
    if unmapped_records:
        resp = await _save_quotations(ctx, "(no vcourts department)", unmapped_records)
        if resp.get("ok"):
            _record_physical_saved(ctx, unmapped_records)
        else:
            unmapped_failed = True
        _log(
            ctx,
            "finalize.mark_unmapped_physical",
            StepStatus.OK if resp.get("ok") else StepStatus.FAILED,
            value=f"{len(unmapped_records)} challans",
            error=None if resp.get("ok") else str(resp.get("error")),
        )

    marked_physical = [
        orig for norm, orig in requested_map.items() if norm in physical
    ]
    not_quoted = [
        orig
        for norm, orig in requested_map.items()
        if norm not in matched and norm not in physical
    ]

    parts = [
        f"Challan settlement scan for {p.vehicleNumber}:",
        f"{len(confirmed)}/{len(results)} departments saved quotations "
        f"({ctx.scratch.get('saved_matched', 0)} requested + "
        f"{ctx.scratch.get('saved_extra', 0)} extra).",
    ]
    if marked_physical:
        parts.append(
            f"Not in Virtual Courts, marked physical court (quotation null): "
            f"{', '.join(marked_physical)}."
        )
    if not_quoted:
        parts.append(f"No settlement found for: {', '.join(not_quoted)}.")
    if unmapped:
        parts.append(f"No Virtual Courts department for: {', '.join(unmapped)}.")
    if unmapped_failed:
        parts.append(
            "WARNING: failed to save physical-court marks for the unmapped challans."
        )
    for r in skipped:
        parts.append(f"[{r['department']}] skipped ({r['reason']}).")
    for r in failed:
        parts.append(f"[{r['department']}] FAILED ({r['reason']}).")
    summary = " ".join(parts)

    # Clear the flag — always, before deciding the outcome. One retry. The API
    # additionally clears it on every terminal write (applyTerminal), so even
    # if BOTH attempts here fail, job-completed will drop it.
    flag = await api_client.challan_settlement_flag(
        request_id=p.requestId,
        driver_id=p.driverId,
        vehicle_number=p.vehicleNumber,
        checking=False,
        summary=summary,
    )
    if not flag.get("ok"):
        flag = await api_client.challan_settlement_flag(
            request_id=p.requestId,
            driver_id=p.driverId,
            vehicle_number=p.vehicleNumber,
            checking=False,
            summary=summary,
        )
    _log(
        ctx,
        "finalize.flag_cleared",
        StepStatus.OK if flag.get("ok") else StepStatus.FAILED,
        value=summary[:400],
        error=None if flag.get("ok") else str(flag.get("error")),
    )

    total_cost = 0.0
    for entry in getattr(ctx.log, "entries", []) or []:
        total_cost += float(getattr(entry, "ai_cost_usd", None) or 0.0)

    # Outcome mapping (no money ever moves in this task):
    #   any dept confirmed             -> done      (quotations landed; failures
    #                                                are itemized in the summary)
    #   nothing confirmed, any failure -> cancelled (retryable; NOT `failed`,
    #                                                which flips manualReview)
    #   nothing confirmed, all legit   -> done      (scan ran clean; there was
    #                                                simply nothing settleable —
    #                                                incl. "only department was
    #                                                not_found")
    if confirmed:
        status = "done"
    elif failed or hard_skips:
        status = "cancelled"
    else:
        status = "done"

    return RunOutcome(
        status=status, summary=summary, total_cost_usd=round(total_cost, 6)
    )


# ── entry point ──────────────────────────────────────────────────────────


def make_phases(params: ChallanSettlementParams) -> list[Phase]:
    plan, _unmapped = plan_departments(params.challanNumbers)
    phases: list[Phase] = [Phase(name="start", run=_start, max_secs=60)]
    for i, dept in enumerate(plan, start=1):
        phases.append(_make_department_phase(dept, i))
    phases.append(Phase(name="finalize", run=_finalize, max_secs=120))
    return phases
