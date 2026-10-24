"""Regression tests for the 20 Hz tick grid.

A vanilla client does not sleep between ticks; it keeps a clock and runs every
tick whose slot has passed, at most ten per frame
(``Minecraft.MAX_TICKS_PER_UPDATE``), and it keeps the unpaid remainder in
``DeltaTracker`` instead of dropping it.  A loop that only ever advances one
tick per call cannot do that: every await that took longer than 50 ms silently
stretched the world, so a knockback arc that the server has already simulated
for ten ticks arrived late, or not at all, and the bot looked like it hung in
the air.

``tick`` now carries the owed time in ``_next_tick_at`` and drains it, so these
tests pin the arithmetic: on time is one tick, late is several, a stall wider
than ``TICK_DEBT_RESET_SECONDS`` re-anchors instead of replaying stale input,
and ``wait_tick_budget`` only waits for a slot without claiming it.
"""

from __future__ import annotations

import unittest
from unittest import mock

from protobot.client import Bot
from protobot.protocol.connection import ConnectionState
from protobot.physics import MovementInput

TICK = 0.05


class FakeClock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []


def _make_bot() -> tuple[Bot, list[tuple[int, bytes]]]:
    bot = Bot("localhost", username="ProtoGrid", version="26.3")
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


def _tick_ends(bot: Bot, sent: list[tuple[int, bytes]]) -> int:
    packet_id = bot.version.packets.serverbound_tick_end
    return sum(1 for header, _ in sent if header == packet_id)


class TickGridTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.enterContext(
            mock.patch("protobot.movement._now", lambda: self.clock.now)
        )
        self.enterContext(mock.patch("protobot.movement._sleep", self._fake_sleep))

    async def _fake_sleep(self, delay: float) -> None:
        self.clock.sleeps.append(delay)
        self.clock.now += delay

    async def test_a_tick_on_time_claims_exactly_one_slot(self) -> None:
        bot, sent = _make_bot()

        await bot.tick(MovementInput(forward=1.0))

        self.assertEqual(_movements(bot, sent), 1)
        self.assertAlmostEqual(bot._next_tick_at, TICK)
        self.assertEqual(self.clock.sleeps, [])

    async def test_a_late_caller_runs_every_tick_it_owes(self) -> None:
        bot, sent = _make_bot()
        await bot.tick(MovementInput(forward=1.0))
        sent.clear()

        # One and a bit slots behind: the slot we are standing in plus the one
        # that passed.  Two is also the widest burst the anti-cheat allows.
        self.clock.now += 0.10
        await bot.tick(MovementInput(forward=1.0))

        self.assertEqual(_movements(bot, sent), 2)
        self.assertAlmostEqual(bot._next_tick_at, 0.15)
        # Catching up sends immediately; nothing needed to wait for a slot.
        self.assertEqual(self.clock.sleeps, [])

    async def test_a_stall_wider_than_the_allowance_re_anchors(self) -> None:
        bot, sent = _make_bot()
        await bot.tick(MovementInput(forward=1.0))
        sent.clear()

        # 600 ms behind is twelve slots, and sending twelve packets in one burst
        # would claim to be half a second ahead of real time -- which is what
        # Grim's Timer flags.  The grid starts again from now instead.
        self.clock.now += 0.60
        await bot.tick(MovementInput(forward=1.0))

        self.assertEqual(_movements(bot, sent), 1)
        self.assertAlmostEqual(bot._next_tick_at, self.clock.now + TICK)

        # The next call is on time again: it waits for its slot, then goes.
        sent.clear()
        await bot.tick(MovementInput(forward=1.0))
        self.assertEqual(_movements(bot, sent), 1)
        self.assertAlmostEqual(bot._next_tick_at, self.clock.now + TICK)
        self.assertEqual(len(self.clock.sleeps), 1)

    async def test_a_long_stall_re_anchors_instead_of_replaying_stale_input(self) -> None:
        bot, sent = _make_bot()
        await bot.tick(MovementInput(forward=1.0))
        sent.clear()

        # The caller stopped ticking for seconds (a path plan, a chat round
        # trip).  Replaying that input would claim movement the server never saw.
        self.clock.now += 8.0
        await bot.tick(MovementInput(forward=1.0))

        self.assertEqual(_movements(bot, sent), 1)
        self.assertAlmostEqual(bot._next_tick_at, self.clock.now + TICK)

    async def test_each_caught_up_tick_is_closed_before_the_next_packet(self) -> None:
        bot, sent = _make_bot()
        await bot.tick(MovementInput(forward=1.0))
        sent.clear()

        self.clock.now += 0.10
        await bot.tick(MovementInput(forward=1.0))

        # 26.3 refuses two position-bearing packets without a tick end between
        # them, so every packet is followed by its own tick end.
        self.assertEqual(_movements(bot, sent), 2)
        self.assertEqual(_tick_ends(bot, sent), 2)

    async def test_waiting_for_the_budget_does_not_claim_the_slot(self) -> None:
        bot, sent = _make_bot()
        await bot.tick(MovementInput(forward=1.0))
        scheduled = bot._next_tick_at
        sent.clear()

        await bot.wait_tick_budget()
        self.assertAlmostEqual(self.clock.now, scheduled)
        await bot.wait_tick_budget()

        self.assertEqual(bot._next_tick_at, scheduled)
        self.assertEqual(_movements(bot, sent), 0)
        self.assertEqual(len(self.clock.sleeps), 1)

    async def test_a_prediction_only_tick_does_not_drain_the_grid(self) -> None:
        bot, sent = _make_bot()
        await bot.tick(MovementInput(forward=1.0))
        sent.clear()

        self.clock.now += 0.60
        await bot.tick(MovementInput(forward=1.0), send_position=False)

        self.assertEqual(_movements(bot, sent), 0)
        self.assertAlmostEqual(bot._next_tick_at, TICK)

    async def test_a_no_op_tick_gives_the_event_loop_a_turn(self) -> None:
        bot, sent = _make_bot()
        await bot.tick(MovementInput(forward=1.0))
        sent.clear()
        self.clock.sleeps.clear()

        # Navigation owns the body, so this call cannot advance the physics or
        # claim a slot.  It still has to come back to the loop, or a caller that
        # simply loops on ``tick()`` would starve the navigation it waits for.
        bot._navigation_claimed = True
        bot._next_tick_at = self.clock.now + TICK
        await bot.tick()

        self.assertEqual(_movements(bot, sent), 0)
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], TICK)


if __name__ == "__main__":
    unittest.main()
