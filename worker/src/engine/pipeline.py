"""Phase pipeline. A state runner is just an ordered list of Phases:

    Phase(name, run, enter_status=None)

run_phases() walks the list, setting enter_status before each phase that has
one. A phase may:
  - return None            -> continue to the next phase
  - return a RunOutcome    -> the run is over (the payment phase does this)
  - raise ScriptedAbort    -> stop with that terminal
  - raise RestartFrom(n)   -> rewind to phase `n` (pending-clear recovery);
                              capped so a flapping portal can't loop forever

Every phase also runs under its own max_secs deadline (default 5 minutes —
phases with legitimate human waits set their own). A phase that exceeds it
is terminated by the money-line rule: cancelled while still pre-payment,
failed (manualReview) once the payment flow has started. This is the
guarantee that an abandoned or wedged run can never sit on a slot forever.

Adding/removing a phase, or a status, is an edit to one list.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from lifecycle.status import PRE_PAYMENT

from .types import RestartFrom, RunContext, RunOutcome, ScriptedAbort


@dataclass
class Phase:
    name: str
    run: Callable[[RunContext], Awaitable[RunOutcome | None]]
    enter_status: str | None = None
    max_secs: float = 300.0


async def run_phases(
    phases: list[Phase],
    ctx: RunContext,
    *,
    max_restarts: int = 1,
) -> RunOutcome:
    names = [p.name for p in phases]
    i = 0
    restarts = 0
    while i < len(phases):
        phase = phases[i]
        if phase.enter_status:
            await ctx.reporter.set_status(phase.enter_status)
        print(f"[pipeline] job={ctx.job_id} phase {i + 1}/{len(phases)}: {phase.name}")
        try:
            outcome = await asyncio.wait_for(phase.run(ctx), timeout=phase.max_secs)
        except asyncio.TimeoutError:
            pre = ctx.reporter.current in PRE_PAYMENT
            raise ScriptedAbort(
                f"step '{phase.name}' did not finish within "
                f"{int(phase.max_secs // 60)} minutes — "
                + (
                    "request cancelled, no payment was made"
                    if pre
                    else "stopped during the payment flow, reconcile manually"
                ),
                terminal="cancelled" if pre else "failed",
            )
        except RestartFrom as rf:
            if rf.phase_name not in names:
                raise ScriptedAbort(
                    f"internal error: restart target '{rf.phase_name}' unknown",
                    terminal="cancelled",
                )
            if restarts >= max_restarts:
                raise ScriptedAbort(
                    f"portal kept blocking after recovery ({rf.why})",
                    terminal="cancelled",
                )
            restarts += 1
            i = names.index(rf.phase_name)
            print(f"[pipeline] job={ctx.job_id} RESTART -> {rf.phase_name} ({rf.why})")
            continue
        if outcome is not None:
            return outcome
        i += 1
    raise ScriptedAbort(
        "internal error: pipeline ended without an outcome",
        terminal="cancelled",
    )
