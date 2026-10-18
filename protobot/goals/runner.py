"""The loop that turns a goal into progress.

The runner is deliberately small: it checks the goal's own success condition,
gives it one tick, and sleeps for a fraction of a game tick so the network
receive loop can deliver the server's answer.  Everything that decides *what* to
do lives in the goal; everything that decides *how long to keep trying* lives
here, which is what lets callers give a goal a budget without the goal knowing
about wall-clock time.

Goals emit ``goal_start`` and ``goal_done`` on the bot's event bus with the goal
and the :class:`~protobot.goals.base.GoalReport` respectively, so a plugin can
watch automation it does not own.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .base import Goal, GoalError, GoalReport, GoalStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import Bot

__all__ = ["GoalRunner"]

#: Vanilla runs 20 ticks per second; a runner that sleeps longer misses
#: server-side timing (a dig needs a tick cadence closer than 50 ms).
DEFAULT_TICK_INTERVAL = 0.05


class GoalRunner:
    """Run :class:`~protobot.goals.base.Goal` objects one tick at a time."""

    def __init__(self, bot: Bot, *, tick_interval: float = DEFAULT_TICK_INTERVAL) -> None:
        if tick_interval < 0.0:
            raise ValueError("tick_interval cannot be negative")
        self.bot = bot
        self.tick_interval = tick_interval
        self._current: Goal | None = None
        self._interrupted = False

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._current is not None

    @property
    def current(self) -> Goal | None:
        return self._current

    def interrupt(self) -> None:
        """Ask the running goal to stop at the end of the current tick."""

        self._interrupted = True

    # ------------------------------------------------------------------
    # running
    # ------------------------------------------------------------------

    async def run(self, goal: Goal, *, timeout: float = 120.0) -> GoalReport:
        """Tick ``goal`` until it is reached, fails, or ``timeout`` elapses."""

        if self._current is not None:
            raise RuntimeError("a goal is already running")
        if timeout <= 0.0:
            raise ValueError("timeout must be positive")
        self._current = goal
        self._interrupted = False
        started = time.monotonic()
        attempts = 0
        goal.reset()
        await self.bot.events.emit("goal_start", goal)
        try:
            while True:
                if goal.is_reached(self.bot):
                    return await self._finish(
                        GoalReport(goal.describe(), GoalStatus.DONE, time.monotonic() - started, attempts)
                    )
                if self._interrupted:
                    return await self._finish(
                        GoalReport(
                            goal.describe(),
                            GoalStatus.FAILED,
                            time.monotonic() - started,
                            attempts,
                            "interrupted",
                        )
                    )
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    return await self._finish(
                        GoalReport(
                            goal.describe(),
                            GoalStatus.FAILED,
                            elapsed,
                            attempts,
                            goal.last_detail or f"timeout after {timeout:g}s",
                        )
                    )
                attempts += 1
                try:
                    status = await goal.tick(self.bot)
                except GoalError as error:
                    return await self._finish(
                        GoalReport(
                            goal.describe(),
                            GoalStatus.FAILED,
                            time.monotonic() - started,
                            attempts,
                            str(error),
                        )
                    )
                if status is GoalStatus.FAILED:
                    return await self._finish(
                        GoalReport(
                            goal.describe(),
                            GoalStatus.FAILED,
                            time.monotonic() - started,
                            attempts,
                            goal.last_detail or "goal reported failure",
                        )
                    )
                if status is GoalStatus.DONE and not goal.is_reached(self.bot):
                    # ``tick`` returning DONE is a claim, not proof.  Surfacing
                    # the disagreement here keeps the contract honest instead of
                    # spinning on a goal that will never satisfy ``is_reached``.
                    return await self._finish(
                        GoalReport(
                            goal.describe(),
                            GoalStatus.FAILED,
                            time.monotonic() - started,
                            attempts,
                            "goal reported done but is_reached() is false",
                        )
                    )
                await asyncio.sleep(self.tick_interval)
        finally:
            self._current = None
            self._interrupted = False

    async def run_all(self, *goals: Goal, timeout: float = 300.0) -> list[GoalReport]:
        """Run goals in order, stopping at the first failure.

        Each goal gets ``timeout`` seconds of its own, which is the useful
        default for a short script: one slow step should not eat another's
        budget.  Failed goals are included in the returned list and the caller
        decides whether to continue.
        """

        reports: list[GoalReport] = []
        for goal in goals:
            report = await self.run(goal, timeout=timeout)
            reports.append(report)
            if not report.ok:
                break
        return reports

    async def _finish(self, report: GoalReport) -> GoalReport:
        await self.bot.events.emit("goal_done", report)
        return report
