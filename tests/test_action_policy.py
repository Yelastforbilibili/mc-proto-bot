"""The knockback tolerance knob: when an aim is retried and when it is not."""

from __future__ import annotations

import unittest
from dataclasses import replace

from protobot.actions import (
    DEFAULT_POLICY,
    NEVER_RETRY,
    ActionPolicy,
    ActionResult,
    Actions,
    ActionStatus,
)


class _Client:
    """Just enough client for the retry loop: a knockback counter."""

    def __init__(self) -> None:
        self._motion_epoch = 0


class _Attempt:
    """Records attempts and pushes the player around on demand."""

    def __init__(self, client: _Client, *, knockbacks=(), statuses=()) -> None:
        self.client = client
        self.knockbacks = list(knockbacks)
        self.statuses = list(statuses)
        self.calls = 0

    async def __call__(self) -> ActionResult:
        index = self.calls
        self.calls += 1
        status = (
            self.statuses[index]
            if index < len(self.statuses)
            else self.statuses[-1]
            if self.statuses
            else ActionStatus.CONFIRMED
        )
        result = ActionResult(action="attack", status=status)
        if index < len(self.knockbacks) and self.knockbacks[index]:
            # The shove arrives while the packet is in flight.
            self.client._motion_epoch += 1
        return result


class PolicyTests(unittest.TestCase):
    def test_negative_retries_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            ActionPolicy(retries=-1)

    def test_attempts_counts_the_first_try(self) -> None:
        self.assertEqual(ActionPolicy(retries=0).attempts, 1)
        self.assertEqual(ActionPolicy(retries=2).attempts, 3)

    def test_describe_says_when_nothing_is_retried(self) -> None:
        self.assertIn("no re-aim", NEVER_RETRY.describe())
        self.assertIn("1 re-aim", DEFAULT_POLICY.describe())

    def test_default_retries_once(self) -> None:
        self.assertEqual(DEFAULT_POLICY.retries, 1)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = _Client()
        self.actions = Actions(self.client)  # type: ignore[arg-type]

    async def test_a_clean_attempt_is_not_repeated(self) -> None:
        attempt = _Attempt(self.client, statuses=[ActionStatus.SENT])
        result = await self.actions._attempt_with_retry(
            attempt, policy=DEFAULT_POLICY, name="attack"
        )
        self.assertEqual(attempt.calls, 1)
        self.assertEqual(self.actions.attempts, 1)
        self.assertFalse(self.actions.retried_after_knockback)
        self.assertNotIn("knocked_back", (result.extra if result else {}))

    async def test_a_knockback_buys_one_more_aim(self) -> None:
        attempt = _Attempt(
            self.client,
            knockbacks=[True, False],
            statuses=[ActionStatus.UNCONFIRMED, ActionStatus.SENT],
        )
        result = await self.actions._attempt_with_retry(
            attempt, policy=DEFAULT_POLICY, name="attack"
        )
        self.assertEqual(attempt.calls, 2)
        self.assertEqual(self.actions.attempts, 2)
        self.assertTrue(self.actions.retried_after_knockback)
        self.assertIsNotNone(result)
        self.assertTrue(result.ok)
        self.assertEqual(result.extra["attempts"], 2)
        self.assertTrue(result.extra["knocked_back"])

    async def test_without_a_knockback_a_failure_is_final(self) -> None:
        attempt = _Attempt(self.client, statuses=[ActionStatus.UNCONFIRMED])
        result = await self.actions._attempt_with_retry(
            attempt, policy=DEFAULT_POLICY, name="attack"
        )
        self.assertEqual(attempt.calls, 1)
        self.assertFalse(self.actions.retried_after_knockback)
        self.assertEqual(result.status, ActionStatus.UNCONFIRMED)

    async def test_retries_zero_reports_the_failure_as_it_is(self) -> None:
        attempt = _Attempt(
            self.client,
            knockbacks=[True],
            statuses=[ActionStatus.UNCONFIRMED],
        )
        result = await self.actions._attempt_with_retry(
            attempt, policy=NEVER_RETRY, name="attack"
        )
        self.assertEqual(attempt.calls, 1)
        self.assertFalse(self.actions.retried_after_knockback)
        self.assertNotIn("knocked_back", result.extra)

    async def test_a_successful_attempt_is_kept_even_if_hit(self) -> None:
        attempt = _Attempt(
            self.client, knockbacks=[True], statuses=[ActionStatus.CONFIRMED]
        )
        await self.actions._attempt_with_retry(
            attempt, policy=DEFAULT_POLICY, name="attack"
        )
        self.assertEqual(attempt.calls, 1)

    async def test_the_time_ceiling_stops_the_loop(self) -> None:
        attempt = _Attempt(
            self.client,
            knockbacks=[True, True, True],
            statuses=[ActionStatus.UNCONFIRMED],
        )
        # The budget is shorter than one settle, so the loop must stop after the
        # retry it already paid for instead of running all five.
        policy = replace(DEFAULT_POLICY, retries=5, settle=0.05, max_seconds=0.02)
        await self.actions._attempt_with_retry(attempt, policy=policy, name="attack")
        self.assertEqual(attempt.calls, 2)

    async def test_the_policy_can_be_installed_on_the_actions_object(self) -> None:
        self.assertIs(self.actions.policy, DEFAULT_POLICY)
        self.actions.policy = NEVER_RETRY
        self.assertEqual(self.actions.policy.retries, 0)


class WrapperTests(unittest.IsolatedAsyncioTestCase):
    """``dig``/``place_block`` keep their signatures and forward the policy."""

    def setUp(self) -> None:
        self.client = _Client()
        self.actions = Actions(self.client)  # type: ignore[arg-type]

    async def test_dig_forwards_every_keyword(self) -> None:
        seen: dict[str, object] = {}

        async def fake_dig_once(position, **keywords):
            seen.update(keywords)
            seen["position"] = position
            return ActionResult(action="dig", status=ActionStatus.CONFIRMED)

        self.actions._dig_once = fake_dig_once  # type: ignore[method-assign]
        result = await self.actions.dig(
            (1, 2, 3), face=None, aim=False, hold=0.5, send_stop=False
        )
        self.assertTrue(result.ok)
        self.assertEqual(seen["position"], (1, 2, 3))
        self.assertIs(seen["aim"], False)
        self.assertEqual(seen["hold"], 0.5)
        self.assertIs(seen["send_stop"], False)

    async def test_place_block_forwards_every_keyword(self) -> None:
        seen: dict[str, object] = {}

        async def fake_place(position, **keywords):
            seen.update(keywords)
            return ActionResult(action="place_block", status=ActionStatus.CONFIRMED)

        self.actions._place_block_once = fake_place  # type: ignore[method-assign]
        result = await self.actions.place_block(
            (1, 2, 3), hand="off_hand", aim=False, await_confirm=False
        )
        self.assertTrue(result.ok)
        self.assertEqual(seen["hand"], "off_hand")
        self.assertIs(seen["aim"], False)
        self.assertIs(seen["await_confirm"], False)


if __name__ == "__main__":
    unittest.main()
