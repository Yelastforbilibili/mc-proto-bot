"""The tuning knobs live in one place, and the cheat emulation stays off.

These are defaults with a reason, so they get a test rather than a comment:
somebody flipping one of them should see a red test that names the promise it
breaks.
"""

from __future__ import annotations

import inspect
import unittest

from protobot import constants, log
from protobot.actions import interactions
from protobot.client import Bot, connect
from protobot.containers import menu
from protobot.goals import travel


class ConstantsTests(unittest.TestCase):
    def test_the_packet_cadence_is_one_value(self) -> None:
        self.assertEqual(interactions.TICK_SECONDS, constants.TICK_SECONDS)
        self.assertEqual(menu.POLL_SECONDS, constants.POLL_SECONDS)
        self.assertEqual(travel.POLL_SECONDS, constants.GOAL_POLL_SECONDS)
        self.assertEqual(menu.QUIET_SECONDS, constants.QUIET_SECONDS)

    def test_the_client_reads_the_same_tick_gap(self) -> None:
        from protobot import client

        self.assertEqual(client.MIN_TICK_GAP, constants.MIN_TICK_GAP)

    def test_break_hold_constants_are_exposed(self) -> None:
        self.assertEqual(interactions.HOLD_MARGIN_SECONDS, constants.HOLD_MARGIN_SECONDS)
        self.assertEqual(
            interactions.DEFAULT_HOLD_SECONDS, constants.DEFAULT_HOLD_SECONDS
        )
        self.assertEqual(
            interactions.BREAK_COOLDOWN_SECONDS, constants.BREAK_COOLDOWN_SECONDS
        )

    def test_the_interaction_defaults_come_from_the_shared_module(self) -> None:
        dig = inspect.signature(interactions.Actions.dig)
        self.assertEqual(
            dig.parameters["confirm_timeout"].default,
            constants.CONFIRM_TIMEOUT_SECONDS,
        )
        raycast = inspect.signature(interactions.Actions.raycast)
        self.assertEqual(
            raycast.parameters["max_distance"].default,
            constants.BLOCK_INTERACTION_RANGE,
        )


class CheatDefaultsTests(unittest.TestCase):
    """A vanilla-shaped bot is the default; cheating is something you ask for."""

    def test_connect_leaves_cheat_emulation_off(self) -> None:
        parameters = inspect.signature(connect).parameters
        self.assertFalse(parameters["vclip"].default)
        self.assertFalse(parameters["anti_kick"].default)

    def test_bot_init_leaves_cheat_emulation_off(self) -> None:
        parameters = inspect.signature(Bot.__init__).parameters
        self.assertFalse(parameters["vclip"].default)
        self.assertFalse(parameters["anti_kick"].default)

    def test_anti_kick_offset_is_documented_in_one_place(self) -> None:
        self.assertAlmostEqual(constants.ANTI_KICK_WIRE_OFFSET, 0.0313, places=6)


class LogLevelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lines: list[str] = []
        self._level = log.get_level()
        log.set_sink(self.lines.append)

    def tearDown(self) -> None:
        log.set_sink(None)
        log.set_level(self._level)

    def test_the_library_is_quiet_by_default(self) -> None:
        self.assertEqual(log.get_level(), log.INFO)
        log.debug("noise")
        log.trace("packet dump")
        log.info("news")
        log.warn("careful")
        log.error("broken")
        self.assertEqual(self.lines, ["news", "[warn] careful", "[error] broken"])

    def test_debug_needs_to_be_asked_for(self) -> None:
        log.set_level("debug")
        log.debug("noise")
        log.trace("packet dump")
        self.assertEqual(self.lines, ["[debug] noise", "[debug] packet dump"])

    def test_off_silences_every_level(self) -> None:
        log.set_level("none")
        log.info("news")
        log.warn("careful")
        log.error("broken")
        log.debug("noise")
        self.assertEqual(self.lines, [])

    def test_level_names_are_forgiving(self) -> None:
        self.assertEqual(log.level_of("DEBUG"), log.DEBUG)
        self.assertEqual(log.level_of("warning"), log.WARN)
        self.assertEqual(log.level_of(None), log.INFO)
        self.assertEqual(log.level_of("nonsense"), log.INFO)
        self.assertEqual(log.level_of(35), 35)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
