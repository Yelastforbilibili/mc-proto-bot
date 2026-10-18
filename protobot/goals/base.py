"""Goals: declarative objectives that are retried until they are true.

A goal answers two questions and nothing else:

* ``is_reached(bot)`` — is the objective already true, right now?
* ``await tick(bot)`` — what is the smallest useful step towards it?

The :class:`~protobot.goals.runner.GoalRunner` owns the loop, the deadline and
the reporting.  Goals never loop on their own, never block waiting for a state
change, and never assume the previous tick's work survived.  That split is what
makes them composable: :class:`~protobot.goals.composite.SequenceGoal` only has
to decide *which* goal to tick, and any goal can be dropped and rebuilt from
whatever the world looks like next.

``is_reached`` is the single source of truth.  When ``tick`` returns
:attr:`GoalStatus.DONE` the runner still verifies ``is_reached`` and reports a
failure if the goal lied, so a broken goal is loud instead of silently skipped.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from ..errors import ProtoBotError
from ..physics import Vec3

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import Bot

__all__ = [
    "Goal",
    "GoalError",
    "GoalReport",
    "GoalStatus",
    "block_center",
]


class GoalError(ProtoBotError):
    """A goal cannot be completed; the message says why."""


class GoalStatus(str, Enum):
    """What one goal tick achieved."""

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GoalReport:
    """Outcome of running one goal to completion or failure."""

    goal: str
    status: GoalStatus
    elapsed: float
    attempts: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is GoalStatus.DONE

    def __str__(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.goal}: {self.status.value} in {self.elapsed:.2f}s{suffix}"


def block_center(x: int, y: int, z: int) -> Vec3:
    """Centre of a block, the point a goal aims at."""

    return Vec3(x + 0.5, y + 0.5, z + 0.5)


class Goal(ABC):
    """One objective, ticked by a runner until ``is_reached`` is true."""

    #: Short human name used in reports and events.
    name = "goal"

    def __init__(self) -> None:
        #: Why the last tick did not finish; copied into :class:`GoalReport`.
        self.last_detail = ""

    @abstractmethod
    def is_reached(self, bot: Bot) -> bool:
        """Whether the objective is satisfied right now."""

    @abstractmethod
    async def tick(self, bot: Bot) -> GoalStatus:
        """Make one unit of progress and report what it achieved."""

    def reset(self) -> None:
        """Clear per-run bookkeeping before a run starts."""

        self.last_detail = ""

    def describe(self) -> str:
        return self.name

    # ------------------------------------------------------------------
    # helpers shared by built-in goals
    # ------------------------------------------------------------------

    def _world_state(self, bot: Bot, position: tuple[int, int, int]) -> int:
        """Block state at ``position``; ``-1`` means "chunk not loaded yet"."""

        return bot.world.get_state_id(*position)

    @staticmethod
    def _horizontal_distance(bot: Bot, x: float, z: float) -> float:
        position = bot.physics_state.position
        return math.hypot(x - position.x, z - position.z)

    def _eye_distance(self, bot: Bot, point: Vec3) -> float:
        position = bot.physics_state.position
        eye = Vec3(
            position.x,
            position.y + getattr(bot.physics_state, "eye_height", 1.62),
            position.z,
        )
        return math.sqrt((point - eye).length_squared)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.describe()}>"
