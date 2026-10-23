"""Tests for SleepGoal: sleeping is proved by the clock, not by the click."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from protobot.actions import ActionStatus, ActionResult
from protobot.goals import GoalStatus, SleepGoal
from protobot.navigation import NavigationTimeout
from protobot.physics import Vec3


NIGHT = 18000
DAY = 1000


def _result(status: ActionStatus, evidence: str = "") -> ActionResult:
    return ActionResult(action="sleep_in_bed", status=status, evidence=evidence)


class _FakeActions:
    """Stands in for ``bot.actions``; records what the goal asked for."""

    NIGHT_START = 12500
    NIGHT_END = 23500

    def __init__(self, bed=(2, -60, 2), status=ActionStatus.CONFIRMED) -> None:
        self.bed = bed
        self.status = status
        self.evidence = "the night was skipped: day_time 18000 -> 0"
        self.beds_asked: list[float] = []
        self.sleeps: list[tuple] = []

    def find_bed(self, *, radius: float = 6.0):
        self.beds_asked.append(radius)
        return self.bed

    async def sleep_in_bed(self, position=None, **kwargs) -> ActionResult:
        self.sleeps.append((position, kwargs))
        return _result(self.status, self.evidence)


class _FakeBot:
    def __init__(self, *, day_time: int = NIGHT, position=(0.5, -60, 0.5)) -> None:
        self.world = SimpleNamespace(day_time=day_time)
        self.physics_state = SimpleNamespace(position=Vec3(*position))
        self.actions = _FakeActions()
        self.navigations: list[tuple] = []
        self.navigation_error: Exception | None = None

    async def navigate_to(self, x, z, **kwargs):
        self.navigations.append((x, z, kwargs))
        if self.navigation_error is not None:
            raise self.navigation_error
        return self.physics_state


class NightCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_daytime_fails_with_the_reason(self) -> None:
        bot = _FakeBot(day_time=DAY)
        goal = SleepGoal()
        status = await goal.tick(bot)
        self.assertIs(status, GoalStatus.FAILED)
        self.assertIn("no night to sleep through", goal.last_detail)
        self.assertIn(str(DAY), goal.last_detail)
        # Nothing was clicked: a bed in daylight is a wasted round trip.
        self.assertEqual(bot.actions.sleeps, [])
        self.assertFalse(goal.is_reached(bot))

    async def test_goal_does_not_claim_done_before_it_starts(self) -> None:
        # is_reached is asked before the first tick by the runner, and a
        # SleepGoal that starts at dawn has not slept through anything.
        bot = _FakeBot(day_time=DAY)
        goal = SleepGoal()
        self.assertFalse(goal.is_reached(bot))

    async def test_confirm_means_done_and_the_clock_moved(self) -> None:
        bot = _FakeBot(day_time=NIGHT)
        goal = SleepGoal()
        self.assertFalse(goal.is_reached(bot))
        status = await goal.tick(bot)
        self.assertIs(status, GoalStatus.DONE)
        self.assertIn("night was skipped", goal.last_detail)
        # The world clock is what makes the goal reached.
        bot.world.day_time = 0
        self.assertTrue(goal.is_reached(bot))
        # A nearby bed was looked up, and the click named the bed explicitly.
        self.assertEqual(bot.actions.beds_asked, [6.0])
        self.assertEqual(bot.actions.sleeps[0][0], (2, -60, 2))

    async def test_walks_to_the_bed_before_clicking(self) -> None:
        bot = _FakeBot(day_time=NIGHT, position=(0.5, -60, 12.5))
        goal = SleepGoal()
        status = await goal.tick(bot)
        self.assertIs(status, GoalStatus.DONE)
        self.assertEqual(len(bot.navigations), 1)
        x, z, kwargs = bot.navigations[0]
        self.assertEqual((x, z), (2.5, 2.5))
        self.assertEqual(kwargs["y"], -59.5)
        self.assertEqual(kwargs["tolerance"], 1.0)
        self.assertEqual(len(bot.actions.sleeps), 1)

    async def test_click_range_does_not_walk(self) -> None:
        bot = _FakeBot(day_time=NIGHT, position=(2.5, -60, 3.5))
        goal = SleepGoal()
        await goal.tick(bot)
        self.assertEqual(bot.navigations, [])

    async def test_unreachable_bed_returns_running_with_the_reason(self) -> None:
        bot = _FakeBot(day_time=NIGHT, position=(0.5, -60, 40.5))
        bot.navigation_error = NavigationTimeout("no path to (2.5, 2.5)")
        goal = SleepGoal()
        status = await goal.tick(bot)
        self.assertIs(status, GoalStatus.RUNNING)
        self.assertIn("could not walk to the bed", goal.last_detail)
        self.assertEqual(bot.actions.sleeps, [])

    async def test_no_bed_is_a_loud_failure(self) -> None:
        bot = _FakeBot(day_time=NIGHT)
        bot.actions.bed = None
        goal = SleepGoal(radius=9.0)
        status = await goal.tick(bot)
        self.assertIs(status, GoalStatus.FAILED)
        self.assertIn("no bed within 9 blocks", goal.last_detail)
        self.assertEqual(bot.actions.beds_asked, [9.0])


class ResultMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_fails_with_the_action_reason(self) -> None:
        bot = _FakeBot(day_time=NIGHT)
        bot.actions.status = ActionStatus.REJECTED
        bot.actions.evidence = "the bed at (2, -60, 2) disappeared while waiting"
        goal = SleepGoal()
        status = await goal.tick(bot)
        self.assertIs(status, GoalStatus.FAILED)
        self.assertIn("disappeared", goal.last_detail)
        self.assertIn("rejected", goal.last_detail)

    async def test_unconfirmed_keeps_running(self) -> None:
        bot = _FakeBot(day_time=NIGHT)
        bot.actions.status = ActionStatus.UNCONFIRMED
        bot.actions.evidence = "timed out after 30.00s"
        goal = SleepGoal()
        status = await goal.tick(bot)
        self.assertIs(status, GoalStatus.RUNNING)
        self.assertIn("unconfirmed", goal.last_detail)
        self.assertFalse(goal.is_reached(bot))

    async def test_reset_allows_a_new_night(self) -> None:
        bot = _FakeBot(day_time=NIGHT)
        goal = SleepGoal()
        await goal.tick(bot)
        bot.world.day_time = 0
        self.assertTrue(goal.is_reached(bot))
        goal.reset()
        self.assertFalse(goal.is_reached(bot))
        # After a reset the daytime rule applies again instead of "done".
        status = await goal.tick(bot)
        self.assertIs(status, GoalStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
