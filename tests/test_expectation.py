"""Tests for the expectation layer: four endings, none of them an exception."""

from __future__ import annotations

import time
import unittest

from protobot.expectation import (
    ExpectationStatus,
    Outcome,
    wait_for_expectation,
)


class _FakeBot:
    """Counts ticks so a wait can prove it kept the client alive."""

    def __init__(self) -> None:
        self.ticks = 0

    async def tick(self) -> None:
        self.ticks += 1

    async def advance(self) -> None:
        self.ticks += 1


class WaitEndingTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_true_ends_done(self) -> None:
        bot = _FakeBot()
        outcome = await wait_for_expectation(bot, lambda: True, timeout=1.0, name="dig")
        self.assertIs(outcome.status, ExpectationStatus.DONE)
        self.assertTrue(outcome)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.name, "dig")
        self.assertIn("dig: done in", outcome.describe())
        # A condition that is already true must not tick the client at all.
        self.assertEqual(bot.ticks, 0)

    async def test_timeout_is_a_value_not_a_raise(self) -> None:
        bot = _FakeBot()
        outcome = await wait_for_expectation(
            bot, lambda: False, timeout=0.01, name="place"
        )
        self.assertIs(outcome.status, ExpectationStatus.TIMEOUT)
        self.assertFalse(outcome)
        self.assertIn("timed out", outcome.describe())
        self.assertIn("still not true after", outcome.detail)
        self.assertGreater(bot.ticks, 0)

    async def test_valid_reason_invalidates_immediately(self) -> None:
        bot = _FakeBot()
        outcome = await wait_for_expectation(
            bot,
            lambda: False,
            valid=lambda: "the bed at (1, 2, 3) disappeared",
            timeout=30.0,
            name="sleep",
        )
        self.assertIs(outcome.status, ExpectationStatus.INVALIDATED)
        self.assertIn("disappeared", outcome.detail)
        self.assertIn("invalidated after", outcome.describe())
        # The reason is checked before the first tick, so an impossible wait
        # costs nothing at all.
        self.assertEqual(bot.ticks, 0)

    async def test_valid_none_keeps_waiting(self) -> None:
        bot = _FakeBot()
        seen: list[int] = []

        def check() -> bool:
            seen.append(len(seen))
            return len(seen) > 2

        outcome = await wait_for_expectation(
            bot, check, valid=lambda: None, timeout=5.0
        )
        self.assertIs(outcome.status, ExpectationStatus.DONE)
        self.assertGreater(bot.ticks, 0)

    async def test_check_raising_becomes_failed(self) -> None:
        def check() -> bool:
            raise ValueError("state id 0 is not in the block table")

        outcome = await wait_for_expectation(_FakeBot(), check, timeout=5.0, name="dig")
        self.assertIs(outcome.status, ExpectationStatus.FAILED)
        self.assertIn("ValueError", outcome.detail)
        self.assertIn("state id 0", outcome.detail)
        self.assertIn("failed after", outcome.describe())

    async def test_done_wins_over_valid_on_the_same_poll(self) -> None:
        # The block broke *and* something replaced it: success is what happened.
        outcome = await wait_for_expectation(
            _FakeBot(), lambda: True, valid=lambda: "replaced", timeout=1.0
        )
        self.assertIs(outcome.status, ExpectationStatus.DONE)

    async def test_before_tick_runs_once_per_poll(self) -> None:
        bot = _FakeBot()
        swings = 0
        checks = 0

        async def swing() -> None:
            nonlocal swings
            swings += 1

        def check() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 3

        outcome = await wait_for_expectation(
            bot, check, timeout=5.0, before_tick=swing
        )
        self.assertIs(outcome.status, ExpectationStatus.DONE)
        # Three checks, the last one ends the wait: two polls actually ran.
        self.assertEqual(checks, 3)
        self.assertEqual(swings, 2)
        self.assertEqual(bot.ticks, 2)

    async def test_poll_without_ticking_does_not_touch_the_client(self) -> None:
        bot = _FakeBot()
        started = time.monotonic()
        outcome = await wait_for_expectation(
            bot, lambda: False, timeout=0.03, tick=False, poll=0.005
        )
        self.assertIs(outcome.status, ExpectationStatus.TIMEOUT)
        self.assertEqual(bot.ticks, 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.02)


class OutcomeTests(unittest.TestCase):
    def test_describe_covers_all_four_endings(self) -> None:
        done = Outcome(ExpectationStatus.DONE, "dig", "", 0.5)
        timed = Outcome(ExpectationStatus.TIMEOUT, "dig", "still not true after 1.00s", 1.0)
        invalid = Outcome(ExpectationStatus.INVALIDATED, "sleep", "bed gone", 0.5)
        failed = Outcome(ExpectationStatus.FAILED, "place", "ValueError: boom", 0.5)

        self.assertEqual(done.describe(), "dig: done in 0.50s")
        self.assertEqual(timed.describe(), "dig: timed out after 1.00s (still not true after 1.00s)")
        self.assertEqual(invalid.describe(), "sleep: invalidated after 0.50s (bed gone)")
        self.assertEqual(failed.describe(), "place: failed after 0.50s (ValueError: boom)")

        self.assertTrue(done)
        self.assertFalse(timed)
        self.assertFalse(invalid)
        self.assertFalse(failed)

    def test_outcome_is_frozen(self) -> None:
        outcome = Outcome(ExpectationStatus.DONE, "dig")
        with self.assertRaises(Exception):
            outcome.name = "other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
