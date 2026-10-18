"""Goal composition.

Composites are goals too, so they nest arbitrarily.  They only decide *which*
goal gets the tick; the success condition stays a plain read of the world, which
is why a ``SequenceGoal`` that is interrupted and rebuilt mid-way still resumes
at the right step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Goal, GoalStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import Bot

__all__ = ["AnyOfGoal", "SequenceGoal"]


class SequenceGoal(Goal):
    """Run goals one after another; reached when all of them are."""

    name = "sequence"

    def __init__(self, *goals: Goal, name: str | None = None) -> None:
        super().__init__()
        if not goals:
            raise ValueError("a sequence needs at least one goal")
        self.goals = tuple(goals)
        if name is not None:
            self.name = name

    def is_reached(self, bot: Bot) -> bool:
        return all(goal.is_reached(bot) for goal in self.goals)

    async def tick(self, bot: Bot) -> GoalStatus:
        for goal in self.goals:
            if goal.is_reached(bot):
                continue
            status = await goal.tick(bot)
            if status is GoalStatus.FAILED:
                self.last_detail = f"{goal.describe()}: {goal.last_detail or 'failed'}"
                return GoalStatus.FAILED
            # Any progress means the next decision waits for the next tick; the
            # goal's own ``is_reached`` is what promotes the sequence onward.
            return GoalStatus.RUNNING
        return GoalStatus.DONE

    def reset(self) -> None:
        super().reset()
        for goal in self.goals:
            goal.reset()

    def describe(self) -> str:
        return " -> ".join(goal.describe() for goal in self.goals)


class AnyOfGoal(Goal):
    """Reached as soon as one goal is reached; fails only when all fail."""

    name = "any-of"

    def __init__(self, *goals: Goal, name: str | None = None) -> None:
        super().__init__()
        if not goals:
            raise ValueError("any-of needs at least one goal")
        self.goals = tuple(goals)
        if name is not None:
            self.name = name

    def is_reached(self, bot: Bot) -> bool:
        return any(goal.is_reached(bot) for goal in self.goals)

    async def tick(self, bot: Bot) -> GoalStatus:
        failures: list[str] = []
        for goal in self.goals:
            if goal.is_reached(bot):
                return GoalStatus.DONE
            status = await goal.tick(bot)
            if status is GoalStatus.DONE:
                return GoalStatus.DONE
            if status is GoalStatus.FAILED:
                failures.append(f"{goal.describe()}: {goal.last_detail or 'failed'}")
        if failures and len(failures) == len(self.goals):
            self.last_detail = "; ".join(failures)
            return GoalStatus.FAILED
        if failures:
            self.last_detail = "; ".join(failures)
        return GoalStatus.RUNNING

    def reset(self) -> None:
        super().reset()
        for goal in self.goals:
            goal.reset()

    def describe(self) -> str:
        return " | ".join(goal.describe() for goal in self.goals)
