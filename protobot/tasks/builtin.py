"""Ready-made steps: the shapes most scripts need first.

These stay thin on purpose.  A step that grows policy belongs in a goal or a
task of its own, so the report keeps naming something a human can act on
("break blocks: broke 2/3, failed at (2, -59, 0)") instead of "step 4 failed".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

from ..goals import BreakBlockGoal, PlaceBlockGoal
from .base import Step, StepReport, StepStatus, TaskContext

__all__ = ["BreakBlocks", "PlaceBlocks", "WaitStep"]


class BreakBlocks(Step):
    """Break several blocks, reporting how many of them broke."""

    name = "break blocks"

    def __init__(
        self,
        positions: Iterable[tuple[int, int, int]],
        *,
        hold: float = 2.0,
        timeout_each: float = 60.0,
    ) -> None:
        self.positions = tuple(
            (int(position[0]), int(position[1]), int(position[2]))
            for position in positions
        )
        self.hold = hold
        self.timeout_each = timeout_each

    async def run(self, ctx: TaskContext) -> StepReport:
        started = time.monotonic()
        done = 0
        for position in self.positions:
            goal = BreakBlockGoal(position, hold=self.hold)
            report = await ctx.bot.goals.run(goal, timeout=self.timeout_each)
            if not report.ok:
                return self._report(
                    StepStatus.FAILED,
                    started,
                    f"broke {done}/{len(self.positions)}; "
                    f"failed at {position}: {report.detail or report.status.value}",
                )
            done += 1
        return self._report(
            StepStatus.DONE, started, f"broke {done}/{len(self.positions)}"
        )

    def describe(self) -> str:
        return f"{self.name} ({len(self.positions)})"


class PlaceBlocks(Step):
    """Place blocks from the hotbar at several positions."""

    name = "place blocks"

    def __init__(
        self,
        targets: Iterable[tuple[tuple[int, int, int], str | None]],
        *,
        timeout_each: float = 60.0,
    ) -> None:
        self.targets = tuple(
            ((int(position[0]), int(position[1]), int(position[2])), item)
            for position, item in targets
        )
        self.timeout_each = timeout_each

    async def run(self, ctx: TaskContext) -> StepReport:
        started = time.monotonic()
        done = 0
        for position, item in self.targets:
            goal = PlaceBlockGoal(position, item=item)
            report = await ctx.bot.goals.run(goal, timeout=self.timeout_each)
            if not report.ok:
                return self._report(
                    StepStatus.FAILED,
                    started,
                    f"placed {done}/{len(self.targets)}; "
                    f"failed at {position}: {report.detail or report.status.value}",
                )
            done += 1
        return self._report(
            StepStatus.DONE, started, f"placed {done}/{len(self.targets)}"
        )

    def describe(self) -> str:
        return f"{self.name} ({len(self.targets)})"


class WaitStep(Step):
    """Pause the script; useful while a server applies an effect."""

    name = "wait"

    def __init__(self, seconds: float) -> None:
        if seconds < 0.0:
            raise ValueError("seconds cannot be negative")
        self.seconds = seconds

    async def run(self, ctx: TaskContext) -> StepReport:
        started = time.monotonic()
        await asyncio.sleep(self.seconds)
        return self._report(StepStatus.DONE, started, f"slept {self.seconds:g}s")

    def describe(self) -> str:
        return f"wait {self.seconds:g}s"
