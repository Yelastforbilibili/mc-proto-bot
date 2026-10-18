"""Unit tests for the goal layer.

The interesting behaviour is not "does it call dig" but the loop contract: a goal
that claims success without reaching its own condition is a bug, a deadline is
reported with the goal's own explanation, and a composed goal resumes at the
right step instead of starting over.
"""

from __future__ import annotations

import time
import unittest

from protobot.actions import ActionStatus
from protobot.navigation import PathNotFound
from protobot.goals import (
    AnyOfGoal,
    AttackEntityGoal,
    BreakBlockGoal,
    Goal,
    GoalError,
    GoalRunner,
    GoalStatus,
    PlaceBlockGoal,
    ReachGoal,
    SequenceGoal,
    StuckWatchdog,
    TravelGoal,
    WaitChunkGoal,
    WaitGoal,
)
from tests._automation_fakes import FakeBot


class _ScriptedGoal(Goal):
    """Goal driven by an explicit script of tick results."""

    name = "scripted"

    def __init__(self, results, *, reached_after: int | None = None) -> None:
        super().__init__()
        self.results = list(results)
        self.reached_after = reached_after
        self.ticks = 0

    def is_reached(self, bot) -> bool:
        if self.reached_after is None:
            return False
        return self.ticks >= self.reached_after

    async def tick(self, bot) -> GoalStatus:
        self.ticks += 1
        if not self.results:
            return GoalStatus.RUNNING
        result = self.results.pop(0)
        if result is GoalStatus.FAILED:
            self.last_detail = "scripted failure"
        return result


class _Reached(Goal):
    name = "reached"

    def is_reached(self, bot) -> bool:
        return True

    async def tick(self, bot) -> GoalStatus:
        raise AssertionError("a reached goal must not be ticked")


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = FakeBot()

    async def test_reached_goal_finishes_without_ticks(self) -> None:
        report = await self.bot.goals.run(_Reached(), timeout=1.0)
        self.assertTrue(report.ok)
        self.assertEqual(report.attempts, 0)
        self.assertEqual(report.status, GoalStatus.DONE)

    async def test_timeout_reports_the_goals_own_detail(self) -> None:
        runner = GoalRunner(self.bot, tick_interval=0.0)
        goal = _ScriptedGoal([GoalStatus.RUNNING] * 100)

        async def mark(bot):
            goal.last_detail = "still walking"
            return GoalStatus.RUNNING

        goal.tick = mark
        report = await runner.run(goal, timeout=0.05)
        self.assertFalse(report.ok)
        self.assertEqual(report.status, GoalStatus.FAILED)
        self.assertIn("still walking", report.detail)

    async def test_claiming_done_without_reaching_is_a_failure(self) -> None:
        runner = GoalRunner(self.bot, tick_interval=0.0)
        goal = _ScriptedGoal([GoalStatus.DONE], reached_after=None)
        report = await runner.run(goal, timeout=1.0)
        self.assertFalse(report.ok)
        self.assertIn("is_reached", report.detail)

    async def test_goal_error_becomes_a_failed_report(self) -> None:
        runner = GoalRunner(self.bot, tick_interval=0.0)

        class _Boom(Goal):
            name = "boom"

            def is_reached(self, bot) -> bool:
                return False

            async def tick(self, bot) -> GoalStatus:
                raise GoalError("nowhere to stand")

        report = await runner.run(_Boom(), timeout=1.0)
        self.assertFalse(report.ok)
        self.assertEqual(report.detail, "nowhere to stand")

    async def test_interrupt_stops_the_loop(self) -> None:
        runner = GoalRunner(self.bot, tick_interval=0.0)

        class _Interrupting(Goal):
            name = "interrupting"

            def is_reached(self, bot) -> bool:
                return False

            async def tick(self, bot) -> GoalStatus:
                runner.interrupt()
                return GoalStatus.RUNNING

        report = await runner.run(_Interrupting(), timeout=5.0)
        self.assertFalse(report.ok)
        self.assertEqual(report.detail, "interrupted")

    async def test_runner_rejects_a_second_goal(self) -> None:
        import asyncio
        import contextlib

        runner = GoalRunner(self.bot, tick_interval=0.0)
        gate = asyncio.Event()

        class _Blocking(Goal):
            name = "blocking"

            def is_reached(self, bot) -> bool:
                return False

            async def tick(self, bot) -> GoalStatus:
                await gate.wait()
                return GoalStatus.RUNNING

        first = asyncio.create_task(runner.run(_Blocking(), timeout=2.0))
        await asyncio.sleep(0)
        try:
            with self.assertRaises(RuntimeError):
                await runner.run(_Reached(), timeout=0.1)
        finally:
            gate.set()
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first
        self.assertFalse(runner.running)

    async def test_events_are_emitted(self) -> None:
        seen: list[str] = []
        self.bot.events.on("goal_start", lambda goal: seen.append(f"start:{goal.name}"))
        self.bot.events.on("goal_done", lambda report: seen.append(f"done:{report.goal}"))
        await self.bot.goals.run(_Reached(), timeout=1.0)
        self.assertEqual(seen, ["start:reached", "done:reached"])

    async def test_the_runner_is_a_bot_property(self) -> None:
        self.assertIs(self.bot.goals, self.bot.goals)


class BreakBlockGoalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = FakeBot(states={(1, 64, 0): 10})

    async def test_an_air_block_is_already_reached(self) -> None:
        bot = FakeBot(states={(1, 64, 0): 0})
        report = await bot.goals.run(BreakBlockGoal((1, 64, 0)), timeout=1.0)
        self.assertTrue(report.ok)
        self.assertEqual(bot.actions.calls, [])

    async def test_breaks_an_in_reach_block(self) -> None:
        goal = BreakBlockGoal((1, 64, 0))
        self.assertFalse(goal.is_reached(self.bot))
        self.assertEqual(await goal.tick(self.bot), GoalStatus.DONE)
        self.assertTrue(goal.is_reached(self.bot))
        self.assertEqual([call[0] for call in self.bot.actions.calls], ["dig"])

    async def test_an_unloaded_chunk_is_not_a_failure(self) -> None:
        bot = FakeBot(unknown=True)
        goal = BreakBlockGoal((1, 64, 0))
        self.assertEqual(await goal.tick(bot), GoalStatus.RUNNING)
        self.assertIn("chunk", goal.last_detail)
        self.assertEqual(bot.actions.calls, [])
        self.assertEqual(bot.navigation, [])

    async def test_an_out_of_reach_block_is_approached_first(self) -> None:
        bot = FakeBot(states={(30, 64, 0): 12})
        goal = BreakBlockGoal((30, 64, 0))
        self.assertEqual(await goal.tick(bot), GoalStatus.RUNNING)
        self.assertEqual(len(bot.navigation), 1)
        self.assertAlmostEqual(bot.navigation[0][0], 30.5, places=3)
        self.assertEqual(bot.actions.calls, [])

    async def test_a_rejected_dig_raises(self) -> None:
        self.bot.dig_status = ActionStatus.REJECTED
        goal = BreakBlockGoal((1, 64, 0))
        with self.assertRaises(GoalError):
            await goal.tick(self.bot)

    async def test_running_the_goal_end_to_end(self) -> None:
        report = await self.bot.goals.run(BreakBlockGoal((1, 64, 0)), timeout=2.0)
        self.assertTrue(report.ok)
        self.assertEqual(self.bot.world.get_state_id(1, 64, 0), 0)


class PlaceBlockGoalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = FakeBot()

    async def test_a_solid_position_is_already_reached(self) -> None:
        bot = FakeBot(states={(1, 64, 0): 5})
        goal = PlaceBlockGoal((1, 64, 0))
        self.assertTrue(goal.is_reached(bot))

    async def test_selects_the_item_then_places(self) -> None:
        self.bot.place_item_in_hotbar("minecraft:dirt", 2)
        goal = PlaceBlockGoal((1, 64, 0), item="minecraft:dirt")
        self.assertEqual(await goal.tick(self.bot), GoalStatus.DONE)
        self.assertEqual(self.bot.selected_hotbar_slot, 2)
        self.assertEqual([call[0] for call in self.bot.actions.calls], ["place_block"])

    async def test_a_missing_item_is_an_error(self) -> None:
        goal = PlaceBlockGoal((1, 64, 0), item="minecraft:stone")
        with self.assertRaises(GoalError):
            await goal.tick(self.bot)

    async def test_placement_targets_the_neighbour_behind_the_face(self) -> None:
        self.bot = FakeBot(position=(0.5, 64.0, 0.5))
        self.bot.place_state = 1
        goal = PlaceBlockGoal((1, 64, 0))
        await goal.tick(self.bot)
        name, args, kwargs = self.bot.actions.calls[0]
        self.assertEqual(args[0], (1, 64, 0))


class AttackEntityGoalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = FakeBot()
        self.bot.entities[7] = self._entity(0.5, 64.0, 0.5)

    @staticmethod
    def _entity(x: float, y: float, z: float):
        from types import SimpleNamespace

        return SimpleNamespace(x=x, y=y, z=z)

    async def test_a_gone_entity_is_already_reached(self) -> None:
        goal = AttackEntityGoal(99)
        self.assertTrue(goal.is_reached(self.bot))

    async def test_attacks_when_in_range_and_tracks_removal(self) -> None:
        goal = AttackEntityGoal(7)
        self.assertFalse(goal.is_reached(self.bot))
        self.assertEqual(await goal.tick(self.bot), GoalStatus.RUNNING)
        self.assertTrue(goal.is_reached(self.bot))
        self.assertEqual([call[0] for call in self.bot.actions.calls], ["attack"])

    async def test_walks_to_a_distant_entity(self) -> None:
        self.bot.entities[8] = self._entity(20.0, 64.0, 0.0)
        goal = AttackEntityGoal(8)
        self.assertEqual(await goal.tick(self.bot), GoalStatus.RUNNING)
        self.assertEqual(len(self.bot.navigation), 1)
        self.assertEqual(self.bot.actions.calls, [])


class ReachGoalTests(unittest.IsolatedAsyncioTestCase):
    async def test_already_close_is_reached(self) -> None:
        bot = FakeBot()
        self.assertTrue(ReachGoal(0.5, 0.5, tolerance=1.0).is_reached(bot))

    async def test_navigation_is_requested_and_reported(self) -> None:
        bot = FakeBot()
        goal = ReachGoal(10.0, 5.0, tolerance=0.5)
        self.assertEqual(await goal.tick(bot), GoalStatus.DONE)
        self.assertEqual(bot.navigation, [(10.0, 5.0)])
        self.assertTrue(goal.is_reached(bot))


class CompositeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = FakeBot(states={(1, 64, 0): 10, (2, 64, 0): 11})

    async def test_sequence_runs_one_goal_at_a_time(self) -> None:
        goal = SequenceGoal(BreakBlockGoal((1, 64, 0)), BreakBlockGoal((2, 64, 0)))
        self.assertFalse(goal.is_reached(self.bot))
        self.assertEqual(await goal.tick(self.bot), GoalStatus.RUNNING)
        self.assertEqual(self.bot.world.get_state_id(1, 64, 0), 0)
        self.assertEqual(self.bot.world.get_state_id(2, 64, 0), 11)
        self.assertEqual(await goal.tick(self.bot), GoalStatus.RUNNING)
        self.assertTrue(goal.is_reached(self.bot))
        report = await self.bot.goals.run(goal, timeout=2.0)
        self.assertTrue(report.ok)

    async def test_any_of_succeeds_on_the_first_reached_member(self) -> None:
        goal = AnyOfGoal(_Reached(), BreakBlockGoal((1, 64, 0)))
        report = await self.bot.goals.run(goal, timeout=1.0)
        self.assertTrue(report.ok)
        self.assertEqual(self.bot.actions.calls, [])

    def test_composites_need_at_least_one_goal(self) -> None:
        with self.assertRaises(ValueError):
            SequenceGoal()
        with self.assertRaises(ValueError):
            AnyOfGoal()


class WaitGoalTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_seconds_is_immediately_reached(self) -> None:
        self.assertTrue(WaitGoal(0.0).is_reached(FakeBot()))

    async def test_waits_then_succeeds(self) -> None:
        bot = FakeBot()
        started = time.monotonic()
        report = await bot.goals.run(WaitGoal(0.1), timeout=2.0)
        self.assertTrue(report.ok)
        self.assertGreaterEqual(time.monotonic() - started, 0.09)

    def test_negative_seconds_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WaitGoal(-1.0)


class WatchdogTests(unittest.TestCase):
    def test_moving_keeps_the_watchdog_quiet(self) -> None:
        watchdog = StuckWatchdog(distance=1.0, seconds=1.0, limit=2)
        self.assertFalse(watchdog.observe((0.0, 0.0), 0.0))
        self.assertFalse(watchdog.observe((2.0, 0.0), 5.0))
        self.assertEqual(watchdog.strikes, 0)
        self.assertFalse(watchdog.exhausted)

    def test_standing_still_strikes_after_the_grace_period(self) -> None:
        watchdog = StuckWatchdog(distance=1.0, seconds=2.0, limit=1)
        self.assertFalse(watchdog.observe((0.0, 0.0), 0.0))
        self.assertFalse(watchdog.observe((0.2, 0.0), 1.0))
        self.assertTrue(watchdog.observe((0.2, 0.0), 2.5))
        self.assertEqual(watchdog.strikes, 1)
        self.assertTrue(watchdog.exhausted)

    def test_reset_clears_the_strikes(self) -> None:
        watchdog = StuckWatchdog(seconds=0.0, limit=1)
        watchdog.observe((0.0, 0.0), 0.0)
        self.assertTrue(watchdog.observe((0.0, 0.0), 1.0))
        watchdog.reset()
        self.assertEqual(watchdog.strikes, 0)
        self.assertFalse(watchdog.exhausted)

    def test_a_limit_is_required(self) -> None:
        with self.assertRaises(ValueError):
            StuckWatchdog(limit=0)


class WaitChunkTests(unittest.IsolatedAsyncioTestCase):
    def test_reached_when_the_chunk_is_decoded(self) -> None:
        bot = FakeBot()
        bot.world.chunks = {(1, 0): "chunk"}
        self.assertTrue(WaitChunkGoal(30.5, 3.0).is_reached(bot))
        self.assertFalse(WaitChunkGoal(35.0, 3.0).is_reached(bot))

    def test_falls_back_to_block_lookups_without_a_chunk_map(self) -> None:
        self.assertTrue(WaitChunkGoal(30.0, 3.0).is_reached(FakeBot()))
        self.assertFalse(WaitChunkGoal(30.0, 3.0).is_reached(FakeBot(unknown=True)))

    async def test_waits_until_the_server_sends_the_chunk(self) -> None:
        bot = FakeBot()
        bot.world.chunks = {}
        goal = WaitChunkGoal(16, 0)
        self.assertFalse(goal.is_reached(bot))
        bot.world.chunks[(1, 0)] = "chunk"
        report = await bot.goals.run(goal, timeout=5.0)
        self.assertTrue(report.ok, report.detail)

    async def test_gives_up_on_a_chunk_that_never_arrives(self) -> None:
        bot = FakeBot()
        bot.world.chunks = {}
        report = await bot.goals.run(WaitChunkGoal(16, 0, timeout=0.05), timeout=5.0)
        self.assertFalse(report.ok)
        self.assertIn("never loaded", report.detail)

    def test_describe_names_the_chunk(self) -> None:
        self.assertEqual(WaitChunkGoal(30.5, 12.5).describe(), "wait for chunk (1, 0)")


class TravelGoalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = FakeBot()

    async def test_already_there_does_not_walk(self) -> None:
        report = await self.bot.goals.run(TravelGoal(0.5, 0.5), timeout=5.0)
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(self.bot.navigation, [])

    async def test_walks_straight_when_the_chunk_is_known(self) -> None:
        report = await self.bot.goals.run(TravelGoal(8.5, 4.5), timeout=5.0)
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(self.bot.navigation, [(8.5, 4.5)])

    async def test_stops_at_the_loaded_frontier(self) -> None:
        # Chunk (1, 0) covers x = 16..31, so nothing past x = 16 is known yet.
        self.bot.world.chunks = {(0, 0): "here"}
        goal = TravelGoal(40.5, 0.5, tolerance=0.5)
        status = await goal.tick(self.bot)
        self.assertIs(status, GoalStatus.RUNNING)
        self.assertIn("stepping", goal.last_detail)
        walked = self.bot.navigation[-1]
        self.assertLess(walked[0], 16.0)
        self.assertGreater(walked[0], 0.5)

    async def test_finishes_once_the_chunks_arrive(self) -> None:
        self.bot.world.chunks = {(0, 0): "here"}
        goal = TravelGoal(40.5, 0.5, tolerance=0.5)
        await goal.tick(self.bot)
        # The server streams the rest of the terrain in as the player moves.
        for chunk_x in range(0, 4):
            self.bot.world.chunks[(chunk_x, 0)] = "chunk"
        report = await self.bot.goals.run(goal, timeout=5.0)
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(self.bot.navigation[-1], (40.5, 0.5))

    async def test_a_failing_walk_becomes_a_goal_failure(self) -> None:
        self.bot.navigation_failures = 99
        report = await self.bot.goals.run(
            TravelGoal(8.5, 0.5, max_strikes=2), timeout=5.0
        )
        self.assertFalse(report.ok)
        self.assertIn("no path", report.detail)

    async def test_a_blocked_walk_hits_the_watchdog(self) -> None:
        self.bot.navigation_blocked = True
        report = await self.bot.goals.run(
            TravelGoal(8.5, 0.5, stuck_seconds=0.0, max_strikes=2), timeout=5.0
        )
        self.assertFalse(report.ok)
        self.assertIn("no progress", report.detail)

    async def test_timeout_reports_the_remaining_distance(self) -> None:
        self.bot.navigation_blocked = True
        report = await self.bot.goals.run(
            TravelGoal(8.5, 0.5, timeout=0.01), timeout=5.0
        )
        self.assertFalse(report.ok)
        self.assertIn("blocks short of", report.detail)

    async def test_waits_for_the_chunk_under_the_player(self) -> None:
        # A pathfinder that plans from undecoded terrain is worse than slow.
        self.bot.world.chunks = {}
        goal = TravelGoal(8.5, 0.5)
        status = await goal.tick(self.bot)
        self.assertIs(status, GoalStatus.RUNNING)
        self.assertIn("under the player", goal.last_detail)
        self.assertEqual(self.bot.navigation, [])

    async def test_a_pathfinder_refusal_is_a_strike_not_a_crash(self) -> None:
        self.bot.navigation_raises = PathNotFound("the start cell has no surface")
        report = await self.bot.goals.run(
            TravelGoal(8.5, 0.5, max_strikes=2), timeout=5.0
        )
        self.assertFalse(report.ok)
        self.assertIn("no surface", report.detail)


if __name__ == "__main__":
    unittest.main()
