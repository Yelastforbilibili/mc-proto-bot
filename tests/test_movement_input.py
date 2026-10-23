"""Regression tests for the sprint bit in the input packet.

A vanilla client cannot sprint without a forward impulse: releasing forward
stops the sprint in the same tick, and the server learns about it with a
STOP_SPRINTING command.  The predictor on the other side keeps the sprint flag
from those commands and combines it with the input bits, so a tick that said
"sprinting, no forward" made the two disagree by a few millimetres, which Grim
reports as ``Simulation``.  ``navigate_to`` used to do exactly that whenever it
paused inside a waypoint's tolerance.
"""

from __future__ import annotations

import unittest

from protobot.client import Bot
from protobot.movement import _START_SPRINTING_COMMAND, _STOP_SPRINTING_COMMAND
from protobot.physics import MovementInput
from protobot.protocol.connection import ConnectionState


def _make_bot(version: str = "26.3") -> tuple[Bot, list[tuple[int, bytes]]]:
    bot = Bot("localhost", username="ProtoInput", version=version)
    bot.state = ConnectionState.PLAY
    bot.session.entity_id = 21
    bot.player.loaded = True
    bot.physics_state.on_ground = True
    sent: list[tuple[int, bytes]] = []

    async def capture(packet_id: int, payload: bytes = b"") -> None:
        sent.append((packet_id, payload))

    bot.send_raw = capture  # type: ignore[method-assign]

    original_budget = bot.wait_tick_budget

    async def budget() -> None:
        await original_budget()

    bot.wait_tick_budget = budget  # type: ignore[method-assign]
    return bot, sent


def _command_payloads(bot: Bot, sent: list[tuple[int, bytes]]) -> list[int]:
    packet_id = bot.version.packets.serverbound_player_command
    # entity id (varint, small in tests) then the command (varint).
    return [payload[1] for header, payload in sent if header == packet_id]


def _input_flags(bot: Bot, sent: list[tuple[int, bytes]]) -> list[int]:
    packet_id = bot.version.packets.serverbound_player_input
    return [payload[0] for header, payload in sent if header == packet_id]


class SprintNeedsForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_release_drops_the_sprint_flag(self) -> None:
        bot, _ = _make_bot()

        await bot._tick_once(MovementInput(forward=1.0, sprint=True))
        self.assertTrue(bot.physics_state.sprinting)

        await bot._tick_once(MovementInput(forward=0.0, sprint=True))
        self.assertFalse(bot.physics_state.sprinting)

    async def test_forward_release_sends_stop_sprinting(self) -> None:
        bot, sent = _make_bot()

        await bot._tick_once(MovementInput(forward=1.0, sprint=True))
        await bot._tick_once(MovementInput(forward=0.0, sprint=True))

        commands = _command_payloads(bot, sent)
        self.assertEqual(
            commands, [_START_SPRINTING_COMMAND, _STOP_SPRINTING_COMMAND]
        )

    async def test_input_packet_does_not_claim_sprint_without_forward(self) -> None:
        bot, sent = _make_bot()

        await bot._tick_once(MovementInput(forward=1.0, sprint=True))
        await bot._tick_once(MovementInput(forward=0.0, sprint=True))

        flags = _input_flags(bot, sent)
        self.assertEqual(flags[0], bot._input_flags(True, False, False, False, False, False, True))
        self.assertEqual(flags[-1], 0)

    async def test_sprinting_with_forward_stays_on(self) -> None:
        bot, _ = _make_bot()

        await bot._tick_once(MovementInput(forward=1.0, sprint=True))
        await bot._tick_once(MovementInput(forward=1.0, sprint=True))

        self.assertTrue(bot.physics_state.sprinting)

    async def test_strafe_only_cannot_sprint(self) -> None:
        bot, _ = _make_bot()

        await bot._tick_once(MovementInput(strafe=1.0, sprint=True))

        self.assertFalse(bot.physics_state.sprinting)


if __name__ == "__main__":
    unittest.main()
