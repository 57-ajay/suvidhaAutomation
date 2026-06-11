# worker/src/scripted/runner.py
"""Dispatch a border-tax job to the right per-state scripted runner.

Pivot-a ships UP only. Adding a state = add a module under border_tax/ and a
branch here.
"""

from __future__ import annotations

from .log import StepLogger
from .params import BorderTaxParams
from .types import RunOutcome
from .border_tax import up as up_runner


async def run_border_tax(session, params: BorderTaxParams, *, reporter, r,
                         job_id: str) -> RunOutcome:
    log = StepLogger()
    code = (params.stateCode or "").upper()

    if code == "UP":
        return await up_runner.run(session, params, reporter=reporter, r=r,
                                   log=log, job_id=job_id)

    return RunOutcome(
        status="cancelled",
        summary=f"state {code} not supported in pivot-a",
        error=f"no scripted runner for state {code}",
        run_log=log.dump(),
    )
