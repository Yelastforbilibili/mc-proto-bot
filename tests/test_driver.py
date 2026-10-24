"""Regression tests for the 20 Hz tick driver.

The driver is the answer to "my caller awaited something slow and the client
froze": it owns no state of its own, it only pumps ``tick`` on the same grid as
everybody else.  That makes three properties worth pinning:

* it actually ticks while the caller is waiting on something else;
* it yields a slot that an explicit tick or an action already claimed, because
  two bodies sending one step is exactly the position desync the grid prevents;
* it never dies silently -- a tick that raises leaves the error on the bot and
  fires ``driver_error``, so a stopped pump is distinguishable from a slow
  server.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from protobot.client import Bot
from protobot.driver import DRIVER_GUARD_SECONDS
from protobot.physics import MovementInput
from protobot.protocol.connection import ConnectionState

TICK = 0.05
START = 1000.0


class FakeClock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = START

    def advance(self, delay: float) -> None:
        self.now += delay


def _make_bot() -> tuple[Bot, list[tuple[int, bytes]]]:
    bot = Bot("localhost", username="ProtoDrive", version="26.3")
    bot.state = ConnectionState.PLAY
    bot.session.entity_id = 21
    bot.player.loaded = True
    bot.physics_state.on_ground = True
    sent: list[tuple[int, bytes]] = []

    async def capture(packet_id: int, payload: bytes = b"") -> None:
        sent.append((packet_id, payload))

    bot.send_raw = capture  # type: ignore[method-assign]
    return bot, sent


def _movements(bot: Bot, sent: list[tuple[int, bytes]]) -> int:
    ids = {
        bot.version.packets.serverbound_position,
        bot.version.packets.serverbound_position_look,
    }
    return sum(1 for packet_id, _ in sent if packet_id in ids)


class DriverPumpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        # The driver imports the clock seams by name, so it needs its own patch.
        self.enterContext(mock.patch("protobot.movement._now", lambda: self.clock.now))
        self.enterContext(mock.patch("protobot.driver._now", lambda: self.clock.now))
        self.enterContext(mock.patch("protobot.movement._sleep", self._fake_sleep))
        self.enterContext(mock.patch("protobot.driver._sleep", self._fake_sleep))

    async def _fake_sleep(self, delay: float) -> None:
        self.clock.advance(delay)
        await asyncio.sleep(0)

    async def test_the_driver_ticks_while_the_caller_waits(self) -> None:
        bot, sent = _make_bot()
        bot.input = MovementInput(forward=1.0)

        async with bot.driving():
            self.assertTrue(bot.driving_active)
            await bot.wait_ticks(4, timeout=5.0)

        self.assertGreaterEqual(_movements(bot, sent), 4)
        self.assertFalse(bot.driving_active)
        self.assertIsNone(bot.driver_error)

    async def test_stop_driver_cancels_the_task(self) -> None:
        bot, _sent = _make_bot()
        await bot.start_driver()
        task = bot._driver_task
        self.assertIsNotNone(task)
        await bot.start_driver()  # idempotent: same task, not a second pump
        self.assertIs(bot._driver_task, task)
        await bot.stop_driver()
        self.assertFalse(bot.driving_active)
        self.assertTrue(task is not None and task.done())
        await bot.stop_driver()  # stopping twice is a no-op

    async def test_the_driver_needs_a_play_session(self) -> None:
        bot, _sent = _make_bot()
        bot.state = ConnectionState.CONFIGURATION
        with self.assertRaises(RuntimeError):
            await bot.start_driver()

    async def test_a_yield_hands_the_slot_to_somebody_else(self) -> None:
        bot, _sent = _make_bot()
        # Fresh bot: nothing has ticked and no action reserved anything.
        bot._last_tick_at = 0.0
        self.assertFalse(bot._driver_yields())

        # An explicit tick a moment ago owns the slot.
        bot._last_tick_at = self.clock.now
        self.assertTrue(bot._driver_yields())

        # ... and so does an action that reserved the tick for its packets.
        bot._last_tick_at = self.clock.now - DRIVER_GUARD_SECONDS * 2
        bot._action_busy_until = self.clock.now + 0.01
        self.assertTrue(bot._driver_yields())

        # A tick that is still open must be closed by its owner, not by us.
        bot._action_busy_until = 0.0
        bot._position_sent_this_tick = True
        self.assertTrue(bot._driver_yields())

        bot._position_sent_this_tick = False
        self.assertFalse(bot._driver_yields())

    async def test_a_broken_tick_stops_the_pump_and_is_reported(self) -> None:
        bot, _sent = _make_bot()
        bot._last_tick_at = START - DRIVER_GUARD_SECONDS * 2
        problems: list[BaseException] = []
        bot.events.on("driver_error", problems.append)

        async def explode(*_args, **_kwargs):
            raise RuntimeError("socket gone")

        bot.tick = explode  # type: ignore[method-assign]
        await bot.start_driver()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if not bot.driving_active:
                break

        self.assertFalse(bot.driving_active)
        self.assertIsInstance(bot.driver_error, RuntimeError)
        self.assertEqual([type(item) for item in problems], [RuntimeError])


class WaitTicksTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_ticks_without_a_driver_says_why(self) -> None:
        # No pump, so no tick can ever arrive: that is a stopped driver, not a
        # slow server, and hanging until the caller's own timeout hides it.
        bot, _sent = _make_bot()
        with self.assertRaises(RuntimeError):
            await bot.wait_ticks(1, timeout=5.0)

    async def test_wait_zero_ticks_returns_immediately(self) -> None:
        bot, _sent = _make_bot()
        self.assertIs(await bot.wait_ticks(0), bot.physics_state)


class AdvanceTests(unittest.IsolatedAsyncioTestCase):
    """``advance`` is the one way to say "let the game move".

    Two call shapes must stay equivalent, because every layer above uses it:
    with a driver it holds the controls and waits for the pump, without one it
    is the plain tick the caller wrote before.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.enterContext(mock.patch("protobot.movement._now", lambda: self.clock.now))
        self.enterContext(mock.patch("protobot.driver._now", lambda: self.clock.now))
        self.enterContext(mock.patch("protobot.movement._sleep", self._fake_sleep))
        self.enterContext(mock.patch("protobot.driver._sleep", self._fake_sleep))

    async def _fake_sleep(self, delay: float) -> None:
        self.clock.advance(delay)
        await asyncio.sleep(0)

    async def test_without_a_driver_advance_is_a_tick(self) -> None:
        bot, sent = _make_bot()
        await bot.advance(MovementInput(forward=1.0))
        self.assertEqual(_movements(bot, sent), 1)
        self.assertEqual(bot._ticks_completed, 1)

    async def test_without_a_driver_advance_leaves_the_held_input_alone(self) -> None:
        bot, _sent = _make_bot()
        before = bot.input
        await bot.advance()
        self.assertIs(bot.input, before)

    async def test_with_a_driver_advance_holds_the_controls(self) -> None:
        bot, sent = _make_bot()
        held = MovementInput(strafe=1.0)
        bot.input = held
        async with bot.driving():
            await bot.advance(MovementInput(forward=1.0), ticks=2)
            pump = _movements(bot, sent)
            self.assertGreaterEqual(pump, 2)
        # The caller's own call must not have added ticks of its own.
        self.assertLessEqual(_movements(bot, sent) - pump, 1)
        # ... and the controls handed over for those ticks are handed back: the
        # pump keeps ticking while the caller plans, and a control left behind
        # would walk the bot the whole time.
        self.assertIs(bot.input, held)

    async def test_navigation_release_drops_the_controls(self) -> None:
        # A walker parks its controls on ``input`` so the pump keeps walking
        # between waypoints; handing the bot back must clear them, or a driven
        # bot walks into the next scenario on its own.
        bot, _sent = _make_bot()
        bot.input = MovementInput(forward=1.0, sprint=True)
        bot._navigation_claimed = True
        bot._release_navigation_input()
        self.assertFalse(bot._navigation_claimed)
        self.assertEqual(bot.input.forward, 0.0)
        self.assertFalse(bot.input.sprint)

