"""Phase pipeline. A state runner is just an ordered list of Phases:

    Phase(name, run, enter_status=None)

run_phases() walks the list, setting enter_status before each phase that has
one. A phase may:
  - return None            -> continue to the next phase
  - return a RunOutcome    -> the run is over (the payment phase does this)
  - raise ScriptedAbort    -> stop with that terminal
  - raise RestartFrom(n)   -> rewind to phase `n` (pending-clear recovery);
                              capped so a flapping portal can't loop forever

Adding/removing a phase, or a status, is an edit to one list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from .types import RestartFrom, RunContext, RunOutcome, ScriptedAbort


@dataclass
class Phase:
    name: str
    run: Callable[[RunContext], Awaitable[RunOutcome | None]]
    enter_status: str | None = None


async def run_phases(
    phases: list[Phase], ctx: RunContext, *, max_restarts: int = 1,
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
            outcome = await phase.run(ctx)
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
        "internal error: pipeline ended without an outcome", terminal="cancelled",
    )
