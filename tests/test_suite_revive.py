"""The suite has to get back on its feet after a death.

A dead bot lies on the death screen -- no physics, no clicks -- so a scenario
that runs after a death fails for a reason that has nothing to do with what it
tests.  These tests pin down the revive loop without a server.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import live_suite  # noqa: E402
from protobot.events import EventBus  # noqa: E402
from protobot.physics import Vec3  # noqa: E402


class _Packets:
    clientbound_open_screen = 0
    clientbound_container_set_content = 0
    clientbound_container_set_data = 0
    clientbound_container_set_slot = 0
    clientbound_set_cursor_item = 0


class _Player:
    def __init__(self, *, dead: bool = False, loaded: bool = True) -> None:
        self.dead = dead
        self.loaded = loaded
        self.health = 0.0 if dead else 20.0


class _Bot:
    """A bot that becomes standing after the given number of respawn requests."""

    def __init__(
        self,
        *,
        dead: bool = True,
        succeed_after: int = 1,
        loaded: bool = True,
    ) -> None:
        self.version = SimpleNamespace(packets=_Packets())
        self.events = EventBus()
        self.uuid = "00000000-0000-0000-0000-000000000001"
        self.username = "TestBot"
        self.player = _Player(dead=dead, loaded=loaded)
        self.physics_state = SimpleNamespace(position=Vec3(0.5, -60.0, 0.5))
        self.world = SimpleNamespace(chunks={(0, 0): object()})
        self.respawns = 0
        self.chats: list[str] = []
        self._succeed_after = succeed_after
        self.closed = SimpleNamespace(is_set=lambda: True)

    async def respawn(self) -> None:
        self.respawns += 1
        if self.respawns >= self._succeed_after:
            self.player.dead = False
            self.player.health = 20.0
            self.player.loaded = True

    async def send_message(self, message: str) -> None:
        self.chats.append(message)


def _suite(bot: _Bot) -> live_suite.Suite:
    bridge = live_suite.Bridge("http://127.0.0.1:1")  # never called in these tests
    return live_suite.Suite(bot, bridge, world="world", anchor=(0, -60, 0))


class ReviveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._resend = live_suite.REVIVE_RESEND
        self._timeout = live_suite.REVIVE_TIMEOUT
        live_suite.REVIVE_RESEND = 0.02
        live_suite.REVIVE_TIMEOUT = 2.0
        live_suite.CHAT_INTERVAL = 0.0

    def tearDown(self) -> None:
        live_suite.REVIVE_RESEND = self._resend
        live_suite.REVIVE_TIMEOUT = self._timeout

    async def test_an_alive_bot_is_left_alone(self) -> None:
        bot = _Bot(dead=False)
        suite = _suite(bot)
        self.assertFalse(await suite.revive())
        self.assertEqual(bot.respawns, 0)

    async def test_a_dead_bot_asks_to_respawn_once(self) -> None:
        bot = _Bot(dead=True, succeed_after=1)
        suite = _suite(bot)
        self.assertTrue(await suite.revive())
        self.assertEqual(bot.respawns, 1)
        self.assertFalse(bot.player.dead)

    async def test_the_request_is_resent_until_the_client_stands_up(self) -> None:
        # The server ignores a respawn for a player it still thinks is alive, so
        # one unanswered request must not end the attempt.
        bot = _Bot(dead=True, succeed_after=3)
        suite = _suite(bot)
        self.assertTrue(await suite.revive())
        self.assertGreaterEqual(bot.respawns, 3)

    async def test_it_waits_for_the_position_sync_not_just_the_respawn(self) -> None:
        bot = _Bot(dead=True)
        bot.player.loaded = False  # the respawn packet clears this too

        async def respawn() -> None:
            bot.respawns += 1
            bot.player.dead = False

        bot.respawn = respawn  # type: ignore[assignment]
        suite = _suite(bot)
        with self.assertRaises(AssertionError):
            await suite.revive()

    async def test_giving_up_says_so(self) -> None:
        bot = _Bot(dead=True, succeed_after=999)
        suite = _suite(bot)
        with self.assertRaises(AssertionError) as raised:
            await suite.revive()
        self.assertIn("重生", str(raised.exception))

    async def test_the_death_handler_does_not_block_the_packet_loop(self) -> None:
        # The handler runs inside the packet dispatch, so awaiting the whole
        # respawn there would stop the client from reading the packets it waits
        # for.  It must hand the work to a task and return.
        bot = _Bot(dead=True, succeed_after=999)
        suite = _suite(bot)
        started = asyncio.get_running_loop().time()
        await suite.on_death(None)
        elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 0.5)
        self.assertIsNotNone(suite.revive_task)
        suite.revive_task.cancel()

    async def test_reset_joins_an_in_flight_revive(self) -> None:
        bot = _Bot(dead=True, succeed_after=1)
        suite = _suite(bot)
        suite.schedule_revive()
        await asyncio.sleep(0)
        # revive() waits for the task the death event already started instead of
        # sending a second Client Status.
        self.assertFalse(await suite.revive() or bot.player.dead)
        self.assertEqual(bot.respawns, 1)


if __name__ == "__main__":
    unittest.main()
