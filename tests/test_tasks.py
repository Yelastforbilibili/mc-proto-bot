"""Unit tests for the task layer.

A task is only worth having if a failure names the step that failed and a retry
is visible in the report, so that is what these pin.
"""

from __future__ import annotations

import unittest

from protobot.actions import ActionResult, ActionStatus
from protobot.goals import BreakBlockGoal, Goal, GoalStatus
from protobot.tasks import (
    BreakBlocks,
    CallStep,
    GoalStep,
    PlaceBlocks,
    Step,
    StepStatus,
    Task,
    TaskRunner,
    WaitStep,
)
from tests._automation_fakes import FakeBot


class TaskRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = FakeBot()
        self.bot._tasks = TaskRunner(self.bot, retries=0, retry_delay=0.0)

    async def test_runs_steps_in_order(self) -> None:
        seen: list[str] = []
        task = Task("ordered")
        task.add_call("first", lambda ctx: seen.append("first"))
        task.add_call("second", lambda ctx: seen.append("second"))
        report = await self.bot.tasks.run(task)
        self.assertTrue(report.ok)
        self.assertEqual(seen, ["first", "second"])
        self.assertEqual(len(report.steps), 2)

    async def test_a_failed_step_stops_the_task(self) -> None:
        seen: list[str] = []

        async def boom(ctx):
            raise ValueError("nope")

        task = (
            Task("stops")
            .add_call("ok", lambda ctx: seen.append("ok"))
            .add(CallStep("boom", boom))
            .add_call("never", lambda ctx: seen.append("never"))
        )
        report = await self.bot.tasks.run(task)
        self.assertFalse(report.ok)
        self.assertEqual(seen, ["ok"])
        self.assertIsNotNone(report.failed_step)
        self.assertEqual(report.failed_step.name, "boom")
        self.assertIn("nope", report.failed_step.detail)
        self.assertEqual(len(report.steps), 2)

    async def test_returning_false_is_a_failure(self) -> None:
        task = Task("false").add_call("no", lambda ctx: False)
        report = await self.bot.tasks.run(task)
        self.assertFalse(report.ok)
        self.assertIn("returned False", report.failed_step.detail)

    async def test_returning_a_string_is_a_failure_with_that_detail(self) -> None:
        task = Task("string").add_call("why", lambda ctx: "chest is full")
        report = await self.bot.tasks.run(task)
        self.assertFalse(report.ok)
        self.assertEqual(report.failed_step.detail, "chest is full")

    async def test_retry_recovers_and_is_visible(self) -> None:
        self.bot._tasks = TaskRunner(self.bot, retries=1, retry_delay=0.0)
        attempts = {"n": 0}

        def flaky(ctx):
            attempts["n"] += 1
            return attempts["n"] > 1

        report = await self.bot.tasks.run(Task("retry").add_call("flaky", flaky))
        self.assertTrue(report.ok)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(report.steps[0].attempts, 2)

    async def test_retries_are_bounded(self) -> None:
        self.bot._tasks = TaskRunner(self.bot, retries=2, retry_delay=0.0)
        attempts = {"n": 0}

        def always(ctx):
            attempts["n"] += 1
            return False

        report = await self.bot.tasks.run(Task("bounded").add_call("always", always))
        self.assertFalse(report.ok)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(report.steps[0].attempts, 3)

    async def test_task_timeout_is_reported_before_the_next_step(self) -> None:
        self.bot._tasks = TaskRunner(self.bot, retries=0, retry_delay=0.0)
        task = (
            Task("slow", WaitStep(0.15))
            .add_call("after", lambda ctx: None)
        )
        report = await self.bot.tasks.run(task, timeout=0.05)
        self.assertFalse(report.ok)
        self.assertEqual(report.steps[-1].status, StepStatus.FAILED)
        self.assertIn("timeout", report.steps[-1].detail)

    async def test_context_is_shared_between_steps(self) -> None:
        task = Task("shared")
        task.add_call("write", lambda ctx: ctx.data.__setitem__("k", 1))
        task.add_call("read", lambda ctx: None if ctx.data.get("k") == 1 else "missing")
        report = await self.bot.tasks.run(task)
        self.assertTrue(report.ok)

    async def test_events_are_emitted(self) -> None:
        seen: list[str] = []
        self.bot.events.on("task_start", lambda task: seen.append(f"start:{task.name}"))
        self.bot.events.on("task_step", lambda report, ctx: seen.append(f"step:{report.name}"))
        self.bot.events.on("task_done", lambda report: seen.append(f"done:{report.name}"))
        await self.bot.tasks.run(Task("events").add_call("one", lambda ctx: None))
        self.assertEqual(seen, ["start:events", "step:one", "done:events"])

    async def test_a_step_that_raises_fails_the_task_instead_of_crashing(self) -> None:
        self.bot._tasks = TaskRunner(self.bot, retries=0, retry_delay=0.0)

        class _Exploding(Step):
            name = "exploding"

            async def run(self, ctx):
                raise ConnectionError("connection is not open")

        report = await self.bot.tasks.run(Task("crash", _Exploding()))
        self.assertFalse(report.ok)
        self.assertEqual(report.failed_step.name, "exploding")
        self.assertIn("ConnectionError: connection is not open", report.failed_step.detail)

    def test_numeric_arguments_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            TaskRunner(self.bot, retries=-1)
        with self.assertRaises(ValueError):
            TaskRunner(self.bot, retry_delay=-1.0)


class GoalStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_goal_step_reports_the_goal_outcome(self) -> None:
        bot = FakeBot(states={(1, 64, 0): 10})
        bot._tasks = TaskRunner(bot, retries=0)
        task = Task("dig").add(GoalStep(BreakBlockGoal((1, 64, 0)), timeout=2.0))
        report = await bot.tasks.run(task)
        self.assertTrue(report.ok)
        self.assertEqual(bot.world.get_state_id(1, 64, 0), 0)
        self.assertIn("goal: break", report.steps[0].name)

    async def test_a_failed_goal_fails_the_step(self) -> None:
        class _Failing(Goal):
            name = "break nothing"

            def is_reached(self, bot) -> bool:
                return False

            async def tick(self, bot) -> GoalStatus:
                self.last_detail = "no suitable tool"
                return GoalStatus.FAILED

        bot = FakeBot()
        bot._tasks = TaskRunner(bot, retries=0)
        task = Task("fail").add(GoalStep(_Failing(), timeout=1.0))
        report = await bot.tasks.run(task)
        self.assertFalse(report.ok)
        self.assertIn("break", report.failed_step.name)
        self.assertIn("no suitable tool", report.failed_step.detail)


class BuiltinStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_break_blocks_reports_the_count(self) -> None:
        bot = FakeBot(states={(1, 64, 0): 10, (2, 64, 0): 11})
        bot._tasks = TaskRunner(bot, retries=0)
        report = await bot.tasks.run(Task("mine", BreakBlocks([(1, 64, 0), (2, 64, 0)])))
        self.assertTrue(report.ok)
        self.assertEqual(report.steps[0].detail, "broke 2/2")
        self.assertEqual(bot.world.get_state_id(1, 64, 0), 0)
        self.assertEqual(bot.world.get_state_id(2, 64, 0), 0)

    async def test_break_blocks_names_the_block_that_failed(self) -> None:
        bot = FakeBot(states={(1, 64, 0): 10, (60, 64, 0): 12})
        bot._tasks = TaskRunner(bot, retries=0)
        original = bot.actions.dig

        async def dig(position, **kwargs):
            if position == (60, 64, 0):
                return ActionResult(
                    action="dig",
                    status=ActionStatus.REJECTED,
                    position=position,
                    evidence="out of reach",
                )
            return await original(position, **kwargs)

        bot.actions.dig = dig
        step = BreakBlocks([(1, 64, 0), (60, 64, 0)], timeout_each=0.05)
        report = await bot.tasks.run(Task("mine", step))
        self.assertFalse(report.ok)
        self.assertIn("broke 1/2", report.failed_step.detail)
        self.assertIn("60", report.failed_step.detail)

    async def test_place_blocks_selects_items_and_places(self) -> None:
        bot = FakeBot()
        bot._tasks = TaskRunner(bot, retries=0)
        bot.place_item_in_hotbar("minecraft:dirt", 1)
        step = PlaceBlocks([((1, 64, 0), "minecraft:dirt")], timeout_each=1.0)
        report = await bot.tasks.run(Task("build", step))
        self.assertTrue(report.ok)
        self.assertEqual(bot.selected_hotbar_slot, 1)
        self.assertEqual(report.steps[0].detail, "placed 1/1")

    async def test_wait_step_sleeps(self) -> None:
        bot = FakeBot()
        bot._tasks = TaskRunner(bot, retries=0)
        report = await bot.tasks.run(Task("wait", WaitStep(0.02)))
        self.assertTrue(report.ok)
        self.assertIn("slept", report.steps[0].detail)

    def test_empty_task_is_described(self) -> None:
        self.assertEqual(Task("nothing").describe(), "(empty)")
        self.assertEqual(Task("t", WaitStep(1)).describe(), "wait 1s")


if __name__ == "__main__":
    unittest.main()
