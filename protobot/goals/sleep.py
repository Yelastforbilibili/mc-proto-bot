"""Sleeping: get to a bed, lie down, and wait for the night to be skipped.

Sleeping reads like one right click, but three different things can go wrong and
each one deserves a different answer:

* there is no bed nearby -- walking somewhere else is the only fix;
* the bed is out of reach -- walk there first, the click alone cannot fix it;
* the player is lying down and the server is still waiting for other players --
  clicking again changes nothing, so the goal should not pretend it progressed.

So this goal stays thin: find the bed, walk into reach, call
:meth:`~protobot.actions.interactions.Actions.sleep_in_bed`, and report what the
action proved.  Nothing blocks: the night being skipped is observed through the
world clock (``clientbound_update_time``), which is the same signal a vanilla
client renders its sunrise from.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..actions import ActionStatus
from ..navigation import NavigationTimeout
from .base import Goal, GoalStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import Bot

__all__ = ["SleepGoal"]

#: How close the player has to stand before the click is worth sending.  The
#: server accepts a block click within three blocks; two leaves room for the
#: knock of a doorway without standing on the bed itself.
APPROACH_DISTANCE = 2.0


class SleepGoal(Goal):
    """Sleep through the night, reporting *why* it could not when it cannot."""

    name = "sleep"

    def __init__(
        self,
        bed: tuple[int, int, int] | None = None,
        *,
        radius: float = 6.0,
        timeout: float = 30.0,
    ) -> None:
        super().__init__()
        #: Block position of the bed, or ``None`` to look for one nearby.
        self.bed = bed
        self.radius = radius
        self.timeout = timeout
        self._began = False
        self._started_in_night = False

    # ------------------------------------------------------------------
    # goal protocol
    # ------------------------------------------------------------------
    def reset(self) -> None:
        super().reset()
        self._began = False
        self._started_in_night = False

    def is_reached(self, bot: Bot) -> bool:
        # A goal that starts during the day has no night to skip, so it must not
        # report "done" the moment it is asked: it fails loudly in ``tick``.
        if not self._began or not self._started_in_night:
            return False
        return not self._in_night(bot)

    async def tick(self, bot: Bot) -> GoalStatus:
        if not self._began:
            self._began = True
            self._started_in_night = self._in_night(bot)
        if not self._started_in_night:
            day_time = bot.world.day_time % 24000
            self.last_detail = (
                f"day_time is {day_time}: there is no night to sleep through, "
                "and a bed would refuse the click"
            )
            return GoalStatus.FAILED

        bed = self.bed if self.bed is not None else bot.actions.find_bed(radius=self.radius)
        if bed is None:
            self.last_detail = (
                f"no bed within {self.radius:g} blocks of the player; pass the bed "
                "position to SleepGoal if it is further away"
            )
            return GoalStatus.FAILED

        if not await self._approach(bot, bed):
            return GoalStatus.RUNNING

        result = await bot.actions.sleep_in_bed(
            bed, radius=self.radius, timeout=self.timeout
        )
        if result.status is ActionStatus.CONFIRMED:
            self.last_detail = result.evidence
            return GoalStatus.DONE
        self.last_detail = f"{result.status.value}: {result.evidence}"
        if result.status is ActionStatus.REJECTED:
            # Out of reach, occupied, daytime, or the bed was taken away: none
            # of those get better by clicking again in the same second.
            return GoalStatus.FAILED
        return GoalStatus.RUNNING

    def describe(self) -> str:
        where = "nearby bed" if self.bed is None else f"bed at {self.bed}"
        return f"{self.name} ({where})"

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _in_night(self, bot: Bot) -> bool:
        day_time = bot.world.day_time % 24000
        return bot.actions.NIGHT_START <= day_time < bot.actions.NIGHT_END

    async def _approach(self, bot: Bot, bed: tuple[int, int, int]) -> bool:
        """Walk into clicking range of ``bed``; ``False`` means "try again later"."""

        position = bot.physics_state.position
        target = (bed[0] + 0.5, bed[1] + 0.5, bed[2] + 0.5)
        if math.dist((position.x, position.y, position.z), target) <= APPROACH_DISTANCE:
            return True
        try:
            await bot.navigate_to(
                target[0],
                target[2],
                y=target[1],
                tolerance=1.0,
                timeout=min(self.timeout, 20.0),
            )
        except NavigationTimeout as error:
            self.last_detail = f"could not walk to the bed at {bed}: {error}"
            return False
        return True
