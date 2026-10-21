"""Chat-driven acceptance suite: type a number in game chat, watch it prove itself.

The bot logs in as ``SuiteBot`` and watches public chat.  Any player types a
number (``1``..``N``) and that scenario runs against the live server and reports
into chat.  ``0`` prints the menu, ``all`` runs everything, ``stop`` aborts.

Every scenario builds its own scene through the prototest bridge (blocks, items,
mobs), performs the action with the public API only, and then asks the *server*
-- ``data get``, ``/block``, ``/events``, ``/log`` -- what really happened.  A
client-side state change alone never counts as a pass.

Usage::

    uv run python tools/live_suite.py --version 26.3
    uv run python tools/live_suite.py --list        # print the menu and exit

The bot teleports itself to ``--x/--y/--z`` (default ``0.5 -60 0.5``) at start,
so every scenario can work with positions relative to a known anchor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from protobot import connect
from protobot.actions import ActionStatus, BlockFace
from protobot.constants import IDLE_TICK_SECONDS
from protobot.containers import Menu, MenuNotOpen
from protobot.crafting import recipe_for
from protobot.goals import CraftGoal, GoalStatus, TravelGoal
from protobot.physics import Vec3

CHAT_LIMIT = 230
# Vanilla kicks a player whose chat spam counter passes 200; the counter grows by
# 20 per message and shrinks by one per tick, so one message per second is safe.
CHAT_INTERVAL = 1.05
# A respawn request is ignored while the server still considers the player alive,
# which happens when the join-time health packet has not been processed yet, so
# keep asking until the client is standing again.
REVIVE_RESEND = 2.0
REVIVE_TIMEOUT = 15.0
TAG = "suite"


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------
class Bridge:
    """Minimal client for the prototest bridge (blocks, commands, events)."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode() or "{}")
        nested = payload.get("data")
        if isinstance(nested, dict):
            merged = dict(payload)
            merged.update(nested)
            return merged
        return payload

    def command(self, command: str) -> str:
        payload = self.call("/command", body={"command": command})
        output = payload.get("output") or []
        if isinstance(output, str):
            output = [output]
        return " ".join(str(line) for line in output)

    def setblock(self, world: str, position: tuple[int, int, int], block: str) -> int | None:
        payload = self.call(
            "/setblock",
            query={"world": world, "x": position[0], "y": position[1], "z": position[2]},
            body={"data": block},
        )
        block_info = payload.get("block") or {}
        if isinstance(block_info, dict):
            return block_info.get("stateId")
        return None

    def block(self, world: str, position: tuple[int, int, int]) -> dict[str, Any]:
        payload = self.call(
            "/block",
            query={"world": world, "x": position[0], "y": position[1], "z": position[2]},
        )
        info = payload.get("block")
        return info if isinstance(info, dict) else payload

    def block_state(self, world: str, position: tuple[int, int, int]) -> int:
        return int(self.block(world, position).get("stateId") or 0)

    def block_type(self, world: str, position: tuple[int, int, int]) -> str:
        return str(self.block(world, position).get("type") or "")

    def players(self) -> list[dict[str, Any]]:
        payload = self.call("/players")
        players = payload.get("players")
        return players if isinstance(players, list) else []

    def entities(
        self,
        world: str,
        *,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        radius: float = 16.0,
    ) -> list[dict[str, Any]]:
        """Loaded entities the server has around a point.

        The bridge reads them from the server's own entity list, so this is the
        authority for "did the boat appear": the client's entity map can lag a
        packet behind, the server's list cannot.
        """

        payload = self.call(
            "/entities",
            query={"world": world, "x": x, "y": y, "z": z, "radius": radius},
        )
        entities = payload.get("entities")
        return entities if isinstance(entities, list) else []

    def entities_of(self, world: str, kind: str, **around: Any) -> list[dict[str, Any]]:
        return [
            entity
            for entity in self.entities(world, **around)
            if kind in str(entity.get("type"))
        ]

    def world_time(self, world: str) -> int | None:
        """The world's day time, which is how a skipped night is visible."""

        payload = self.call("/worlds")
        for entry in payload.get("worlds") or []:
            if entry.get("name") == world:
                value = entry.get("time")
                return int(value) if isinstance(value, (int, float)) else None
        return None

    def events(self, *, since: int = 0, limit: int = 400) -> list[dict[str, Any]]:
        payload = self.call("/events", query={"since": since, "limit": limit})
        entries = payload.get("entries") or payload.get("events")
        return entries if isinstance(entries, list) else []

    def clear_events(self) -> None:
        self.call("/events/clear", body={})

    def log(self, *, since: int = 0, limit: int = 400) -> list[str]:
        """Console lines the server wrote, oldest first.

        The bridge answers with ``entries`` (a list of ``{seq, level, source,
        msg}`` objects) and a ``next`` cursor, not with pre-joined text, so the
        message is what gets matched against :data:`ANTI_CHEAT`.
        """

        payload = self.call("/log", query={"since": since, "limit": limit})
        entries = payload.get("entries") or payload.get("lines") or []
        lines: list[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                lines.append(str(entry.get("msg", "")))
            else:
                lines.append(str(entry))
        return lines

    def log_cursor(self) -> int:
        """Where the next :meth:`log` call should start from."""

        payload = self.call("/log", query={"since": 0, "limit": 1})
        try:
            return int(payload.get("next") or 0)
        except (TypeError, ValueError):
            return 0

    def inventory(self, name: str) -> dict[str, Any]:
        payload = self.call("/inventory", query={"name": name})
        return payload


#: Lines the server writes when it thinks the bot is not playing the game the way
#: a vanilla client does.  GrimAC prints one alert per flag to the console
#: (``alerts.print-to-console: true``); the check names are listed one by one
#: because the console prefix ("Grim") also appears on ordinary join messages.
ANTI_CHEAT = (
    "moved too quickly",
    "moved wrongly",
    "Flying is not enabled",
    "触发了",
    "failed PacketOrder",
    "failed NoSwingBreak",
    "failed FastBreak",
    "failed Simulation",
    "failed Post",
    "failed BadPackets",
    "failed Timer",
    "failed Interact",
    "failed Reach",
    "failed MultiActions",
    "failed Breaking",
    "failed Prediction",
)


def _top_level_compounds(text: str) -> list[str]:
    """Split NBT text into its depth-0 ``{...}`` compounds, respecting strings."""

    entries: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                entries.append(text[start : index + 1])
                start = -1
    return entries


def server_items(text: str) -> dict[str, int]:
    """Parse ``data get ... Items`` output into item -> count.

    Entries are split by brace depth so an item that carries components (a nested
    compound with its own ``id``/``count``, like a shulker box) is counted once,
    as itself, instead of being mistaken for the item inside it.
    """

    counts: dict[str, int] = {}
    for entry in _top_level_compounds(text):
        item = re.search(r'id:\s*"([^"]+)"', entry)
        count = re.search(r"(?<![\w])count:\s*(\d+)", entry)
        if item and count:
            name = item.group(1)
            counts[name] = counts.get(name, 0) + int(count.group(1))
    return counts


def first_float(text: str) -> float | None:
    match = re.search(r"(-?\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Case:
    number: int
    category: str
    title: str
    run: Callable[["Suite"], Awaitable[None]]
    timeout: float = 120.0


CASES: list[Case] = []


def case(number: int, category: str, title: str, *, timeout: float = 120.0):
    def register(fn: Callable[["Suite"], Awaitable[None]]):
        CASES.append(Case(number, category, title, fn, timeout))
        return fn

    return register


def by_number(number: int) -> Case | None:
    for item in CASES:
        if item.number == number:
            return item
    return None


def _plain_component(value) -> str:
    """Flatten a chat component (NBT dict, string, or list) to plain text."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        out = text if isinstance(text, str) else ""
        extra = value.get("extra")
        if isinstance(extra, (list, tuple)):
            out += "".join(_plain_component(item) for item in extra)
        return out
    if isinstance(value, (list, tuple)):
        return "".join(_plain_component(item) for item in value)
    return str(value)


def menu_lines() -> list[str]:
    """A compact menu: one chat line per group of categories.

    Chat is limited to 256 characters and throttled to roughly one message per
    second, so the menu has to be a handful of short lines, not 23 of them.
    """

    categories: dict[str, list[Case]] = {}
    for item in CASES:
        categories.setdefault(item.category, []).append(item)
    parts = []
    for category, items in categories.items():
        # Contiguous runs, not "first-last": categories are interleaved once new
        # cases are appended, and a span that swallows other categories lies.
        runs: list[list[int]] = []
        for item in items:
            if runs and item.number == runs[-1][-1] + 1:
                runs[-1].append(item.number)
            else:
                runs.append([item.number])
        for run in runs:
            span = f"{run[0]}" if len(run) == 1 else f"{run[0]}-{run[-1]}"
            parts.append(f"{span} {category}")
    first = CASES[0].number if CASES else 1
    last = CASES[-1].number if CASES else 1
    lines = [
        f"ProtoBot 验收套件 {len(CASES)} 项：直接发数字 {first}-{last} 跑单项，"
        "0=菜单，all=全跑，stop=中止，?N=说明"
    ]
    rows = []
    current = ""
    for part in parts:
        candidate = f"{current} · {part}" if current else part
        if len(candidate) > 110:
            rows.append(current)
            current = part
        else:
            current = candidate
    if current:
        rows.append(current)
    for row in rows:
        lines.append(row)
    return lines


# ---------------------------------------------------------------------------
# suite runtime
# ---------------------------------------------------------------------------
class Suite:
    def __init__(
        self,
        bot,
        bridge: Bridge,
        *,
        world: str = "world",
        anchor: tuple[int, int, int] = (0, -60, 0),
        server_log: str | None = None,
    ) -> None:
        self.bot = bot
        self.bridge = bridge
        self.world = world
        self.anchor = anchor
        # The bridge's ``/log`` endpoint only answers with the lines it captured
        # itself, so when the server log file is reachable on disk the suite
        # reads that instead -- that is where GrimAC writes its alerts.
        self.server_log = server_log
        try:
            self.log_offset = os.path.getsize(server_log) if server_log else 0
        except OSError:
            self.log_offset = 0
        # The alert window restarts for every scenario; 0 means "not marked yet,
        # fall back to the suite start".
        self.alert_offset = 0
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.busy = False
        self.stop_requested = False
        # One row per scenario that ran: case, passed, seconds, detail.
        self.results: list[tuple[Case, bool, float, str]] = []
        # Set while the bot is asleep: vanilla clients send no movement then.
        self.ticks_paused = False
        self.own_uuid = getattr(bot, "uuid", None)
        self.username = getattr(bot, "username", "SuiteBot")
        self.last_said = 0.0
        # Container traffic, so a scenario that loses a click can say what the
        # server actually sent instead of reporting a bare zero.
        packets = bot.version.packets
        self.container_packets = tuple(
            packet_id
            for packet_id in (
                packets.clientbound_open_screen,
                packets.clientbound_container_set_content,
                packets.clientbound_container_set_data,
                packets.clientbound_container_set_slot,
                packets.clientbound_set_cursor_item,
            )
            if packet_id
        )
        self.journal: list[tuple[float, int, int]] = []
        # Only the lines written after the suite started matter: an old flag from
        # a previous run would make every scenario look dirty.
        try:
            self.log_since = self.bridge.log_cursor()
        except Exception:  # a bridge that is not there only costs the check
            self.log_since = 0
        self.started = time.monotonic()
        self.revive_task: asyncio.Task | None = None
        # A dead bot lies on the death screen: no movement, no physics, and the
        # next scenario fails for a reason that has nothing to do with what it
        # tests.  The server never respawns a player by itself, so the suite
        # treats the core's death event as "get up now".
        bot.events.on("death", self.on_death)

    def journal_packet(self, packet: Any) -> None:
        if packet.packet_id not in self.container_packets:
            return
        self.journal.append(
            (
                round(time.monotonic() - self.started, 2),
                packet.packet_id,
                len(packet.payload),
            )
        )
        if len(self.journal) > 120:
            del self.journal[:60]

    # -- chat ---------------------------------------------------------
    async def say(self, message: str) -> None:
        text = message if len(message) <= CHAT_LIMIT else message[: CHAT_LIMIT - 3] + "..."
        # Grim's ChatB flags a chat packet whose text is not already trimmed
        # ("Invalid chat message"), and these lines are indented for the console.
        # Vanilla strips what a player types, so leading spaces never reach the
        # wire; non-breaking spaces do the indenting instead.
        stripped = text.lstrip(" ")
        if stripped != text:
            text = "\u00a0" * (len(text) - len(stripped)) + stripped
        # The vanilla server adds 20 to a spam counter per chat message and removes
        # one per tick, kicking the player above 200.  One message per second keeps
        # the counter flat, so the bot is never kicked for spamming.
        gap = time.monotonic() - self.last_said
        if gap < CHAT_INTERVAL:
            await asyncio.sleep(CHAT_INTERVAL - gap)
        try:
            await self.bot.send_message(text)
        except Exception as error:  # chat must never kill a scenario
            print(f"[chat failed] {text!r}: {error}", flush=True)
        self.last_said = time.monotonic()
        print(f"[bot] {text}", flush=True)

    async def say_many(self, lines: list[str]) -> None:
        for line in lines:
            await self.say(line)

    # -- small helpers ------------------------------------------------
    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self.stop_requested:
            raise asyncio.CancelledError("stop requested")

    def position(self) -> tuple[float, float, float]:
        position = self.bot.physics_state.position
        return (position.x, position.y, position.z)

    def feet(self) -> tuple[int, int, int]:
        x, y, z = self.position()
        return (math.floor(x), math.floor(y), math.floor(z))

    def state(self, position: tuple[int, int, int]) -> int:
        return self.bot.world.get_state_id(*position)

    def server_state(self, position: tuple[int, int, int]) -> int:
        return self.bridge.block_state(self.world, position)

    def command(self, command: str) -> str:
        return self.bridge.command(command)

    def command_quiet(self, command: str) -> str:
        """Run a command whose answer we do not care about.

        The bridge answers 500 for commands that *return a value* (``gamerule``,
        ``time query``).  The command itself still runs, so a scene that only
        wants the side effect should not fail on the answer.
        """

        try:
            return self.command(command)
        except urllib.error.HTTPError:
            return ""

    def setblock(self, position: tuple[int, int, int], block: str) -> None:
        self.bridge.setblock(self.world, position, block)

    def air(self, *positions: tuple[int, int, int]) -> None:
        for position in positions:
            self.bridge.setblock(self.world, position, "minecraft:air")

    def spot(self, dx: int, dz: int, dy: int = 0) -> tuple[int, int, int]:
        x, y, z = self.anchor
        return (x + dx, y + dy, z + dz)

    def clear_spot(self, position: tuple[int, int, int], above: int = 1) -> tuple[int, int, int]:
        self.setblock(position, "minecraft:air")
        for height in range(1, above + 1):
            self.setblock((position[0], position[1] + height, position[2]), "minecraft:air")
        return position

    async def make_air(
        self, position: tuple[int, int, int], *, above: int = 1, timeout: float = 2.0
    ) -> tuple[int, int, int]:
        """Clear a cell and wait until the local world agrees it is air.

        ``clear_spot`` only sends the command, so a check that follows too
        quickly still sees the old block and refuses to place into it.
        """

        self.clear_spot(position, above=above)
        cells = [(position[0], position[1] + height, position[2]) for height in range(above + 1)]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(self.state(cell) == 0 for cell in cells):
                return position
            await asyncio.sleep(0.05)
        raise AssertionError(f"{position} 本地世界还不是空气")

    def give(self, item: str, count: int = 1) -> None:
        self.command(f"give {self.username} {item} {count}")

    def clear_inventory(self) -> None:
        self.command(f"clear {self.username}")

    async def count(self, item: str) -> int:
        total = 0
        for slot in range(46):
            stack = self.bot.get_inventory_item(slot)
            if stack and not stack.empty and stack.identifier == item:
                total += stack.count
        return total

    async def wait_count(self, item: str, expected: int, *, timeout: float = 5.0) -> int:
        """Wait until the local inventory holds ``expected`` of ``item``.

        ``/give`` is a console command: it returns as soon as it is dispatched,
        so a slot click issued too early works off an inventory the server has
        already moved on from.
        """

        deadline = time.monotonic() + timeout
        current = 0
        while time.monotonic() < deadline:
            current = await self.count(item)
            if current >= expected:
                return current
            await asyncio.sleep(0.05)
        return current

    def server_count(self, item: str) -> int:
        text = self.command(f"data get entity {self.username} Inventory")
        return server_items(text).get(item, 0)

    async def wait_menu_item(
        self, menu, item: str, *, where: str = "player", timeout: float = 3.0
    ) -> int | None:
        """Wait until an open window reports ``item`` in ``where``."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            slot = menu.find(item, where=where)
            if slot is not None:
                return slot
            await asyncio.sleep(0.05)
        return None

    async def wait_server_count(
        self, item: str, expected: int, *, timeout: float = 6.0
    ) -> int:
        """Wait for dropped items to be picked up, as the server sees it."""

        deadline = time.monotonic() + timeout
        current = 0
        while time.monotonic() < deadline:
            current = self.server_count(item)
            if current >= expected:
                return current
            await asyncio.sleep(0.1)
        return current

    async def hold(self, item: str) -> int:
        """Put ``item`` in the hotbar, select it, return the hotbar index."""

        for slot in range(36, 45):
            stack = self.bot.get_inventory_item(slot)
            if stack and not stack.empty and stack.identifier == item:
                index = slot - 36
                await self.bot.select_hotbar_slot(index)
                return index
        source = self.bot.player_menu.find(item, where="player")
        if source is None:
            raise AssertionError(f"{item} 不在背包里")
        target = self.bot.player_menu.free(where="player")
        for slot in range(36, 45):
            stack = self.bot.get_inventory_item(slot)
            if not stack or stack.empty:
                target = slot
                break
        if target is None:
            raise AssertionError("热键栏没有空位")
        await self.bot.player_menu.move(source, target)
        await self.bot.select_hotbar_slot(target - 36)
        return target - 36

    async def settle(self, seconds: float = 0.4) -> None:
        await asyncio.sleep(seconds)

    async def close_menu(self) -> None:
        try:
            menu = self.bot.menu
            if menu.is_open and menu.container_id:
                await menu.close()
        except Exception:
            pass

    async def wait_chunk(self, position: tuple[int, int, int], timeout: float = 8.0) -> None:
        key = (position[0] >> 4, position[2] >> 4)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunks = getattr(self.bot.world, "chunks", None)
            if isinstance(chunks, dict) and key in chunks:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"区块 {key} 没有加载")

    async def teleport(self, position: tuple[float, float, float]) -> None:
        await self.close_menu()
        self.command(
            f"tp {self.username} {position[0]:.2f} {position[1]:.2f} {position[2]:.2f} 0 0"
        )
        await self.wait_chunk(
            (math.floor(position[0]), math.floor(position[1]), math.floor(position[2]))
        )
        # The server answers the teleport with a position packet.  Ticking before
        # that packet lands makes the client claim the old spot, which the server
        # logs as "moved too quickly" and answers with a second teleport, so wait
        # for the client to actually be where it was sent.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if math.dist(self.position(), position) <= 1.0:
                break
            await asyncio.sleep(0.05)
        await self.settle(0.5)
        # The server keeps its own copy of where we are, and a click that lands
        # before that copy agrees with ours is ray-traced from the wrong eye:
        # Grim's RotationPlace reports exactly that, and it only shows up on the
        # first click after a teleport.  Wait until both sides agree.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            server = self.server_position()
            if server is not None and math.dist(server, position) <= 0.3:
                break
            await asyncio.sleep(0.05)
        await self.settle(0.2)

    def server_position(self) -> tuple[float, float, float] | None:
        """Where the server says we are, or ``None`` if we are not listed."""

        for player in self.bridge.players():
            if player.get("name") != self.username:
                continue
            try:
                return (
                    float(player["x"]),
                    float(player["y"]),
                    float(player["z"]),
                )
            except (KeyError, TypeError, ValueError):
                return None
        return None

    async def empty_player_grid(self) -> None:
        """Put the craft grid and the cursor back, which ``/clear`` does not touch.

        A scenario that fails halfway can leave a stack in the 2x2 grid or on the
        cursor; the next scenario then reads a grid slot it cannot decode, or
        finds "no room" for a crafted result.
        """

        try:
            player = self.bot.player_menu
            if player.carried.count or player.state().carried_unknown:
                slot = player.free(where="player")
                if slot is not None:
                    await player.click(slot)
            for slot in player.grid_slots():
                stack = player.item(slot)
                if stack is not None and not stack.empty:
                    await player.quick_move(slot)
        except Exception:
            pass

    async def revive(self, *, announce: bool = True) -> bool:
        """Make sure the bot is standing before a scenario runs."""

        task = self.revive_task
        if task is not None and not task.done():
            await task  # join the respawn that the death event already started
        if not getattr(self.bot.player, "dead", False):
            return False
        await self._revive_now(announce=announce)
        return True

    async def _revive_now(self, *, announce: bool = True) -> None:
        """Respawn is Client Status (0 = perform respawn).

        The server answers with the respawn packet and a position sync carrying
        a teleport id, which the core confirms and follows with Player Loaded.
        Both flags matter: until they are set there is no physics state and no
        loaded chunk, so every later click would be built on nothing.
        """

        if announce:
            await self.say("[bot] 我死了，正在重生")
        deadline = time.monotonic() + REVIVE_TIMEOUT
        sent = 0.0
        while time.monotonic() < deadline:
            if not self.bot.player.dead and self.bot.player.loaded:
                break
            # Resend every couple of seconds: the server ignores a respawn for a
            # player it still considers alive, which happens when the join-time
            # health packet has not been processed server side yet.
            if time.monotonic() - sent > REVIVE_RESEND:
                await self.bot.respawn()
                sent = time.monotonic()
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("重生后没有站起来：服务器没有回应 Client Status")
        await self.settle(0.3)
        await self.wait_chunk(self.feet(), timeout=10.0)
        print("[death] respawned", flush=True)

    def schedule_revive(self) -> None:
        """Respawn in the background.

        The death event is dispatched from inside the packet loop, so awaiting
        the whole respawn here would stop the client from reading the very
        packets the respawn is waiting for.
        """

        if self.revive_task is not None and not self.revive_task.done():
            return
        self.revive_task = asyncio.create_task(self._revive_in_background())

    async def _revive_in_background(self) -> None:
        try:
            await self._revive_now(announce=not self.busy)
        except Exception as error:  # a failed revive must not kill the client
            print(f"[death] revive failed: {type(error).__name__}: {error}", flush=True)

    async def on_death(self, message: Any) -> None:
        text = _plain_component(message).strip()
        print(f"[death] {text or '(no message)'}", flush=True)
        self.schedule_revive()

    async def reset(self) -> None:
        """Between scenarios: back at the anchor, no window, no items, no mobs."""

        await self.revive()
        await self.close_menu()
        await self.empty_player_grid()
        self.clear_inventory()
        self.command(f"kill @e[tag={TAG}]")
        # Drops from earlier runs lie around the test area and fly into the
        # inventory the moment the bot walks over them, which makes "dig three
        # logs" read as six.
        self.command("kill @e[type=minecraft:item]")
        ax, ay, az = self.anchor
        # Anything else a scenario spawned outlives it: a cow wanders into the
        # crosshair of the next scenario, a villager re-opens a window, a boat
        # sits where the next structure goes.  Only players are spared.
        self.command(
            f"execute positioned {ax} {ay} {az} run kill"
            " @e[type=!minecraft:player,distance=..16]"
        )
        # Scenarios build small structures, and a probe script or a failed run
        # can leave blocks behind.  Put the floor back and wipe everything above
        # it, so every scenario starts from the same flat ground.
        if not os.environ.get("SUITE_NO_WIPE"):
            self.command(
                f"fill {ax - 12} {ay - 1} {az - 12} {ax + 12} {ay - 1} {az + 12}"
                " minecraft:grass_block"
            )
            self.command(
                f"fill {ax - 12} {ay} {az - 12} {ax + 12} {ay + 5} {az + 12} minecraft:air"
            )
        # Time and weather are scene state too: a scenario that waits for dusk
        # and dies early would otherwise leave the next one in the dark.
        self.command("time set day")
        self.command("weather clear")
        await self.select_slot(0)
        await self.teleport(
            (self.anchor[0] + 0.5, float(self.anchor[1]), self.anchor[2] + 0.5)
        )

    async def select_slot(self, index: int) -> None:
        """Put the hotbar selection back where the next scenario expects it."""

        try:
            await self.bot.select_hotbar_slot(index)
        except Exception:
            pass

    async def wait_entity(
        self,
        mob: str,
        spot: tuple[float, float, float],
        *,
        before: set[int],
        timeout: float = 8.0,
    ) -> int:
        type_id = self.bot.version.entities.entity_id(mob)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            best = None
            best_distance = 99.0
            for entity_id, entity in self.bot.entities.items():
                if entity_id in before or entity.type_id != type_id:
                    continue
                distance = math.hypot(entity.x - spot[0], entity.z - spot[2])
                if distance < best_distance:
                    best, best_distance = entity_id, distance
            if best is not None and best_distance < 3.0:
                return best
        raise AssertionError(f"{mob} 没有出现在 {spot}")

    async def summon(
        self,
        mob: str,
        spot: tuple[float, float, float],
        *,
        tag: str = TAG,
        no_ai: bool = False,
    ) -> int:
        self.command(f"kill @e[tag={tag},type={mob}]")
        await self.settle(0.2)
        before = set(self.bot.entities)
        # A wandering mob walks out of reach between hits, so a scenario about
        # landing hits (rather than chasing) pins its target in place.
        ai = "1b" if no_ai else "0b"
        self.command(
            f"summon {mob} {spot[0]:.2f} {spot[1]:.2f} {spot[2]:.2f} "
            f'{{Tags:["{tag}"],PersistenceRequired:1b,NoAI:{ai}}}'
        )
        return await self.wait_entity(mob, spot, before=before)

    def entity_health(self, mob: str, *, tag: str = TAG) -> float | None:
        text = self.command(
            f"data get entity @e[tag={tag},type={mob},limit=1] Health"
        )
        if "nothing_found" in text or "No entity was found" in text:
            return None
        return first_float(text.split(":")[-1])

    def log_lines(self) -> list[str]:
        """What the server wrote since the current scenario started.

        Prefers the console log on disk (``--server-log``) because that is where
        GrimAC prints one alert per flag; the bridge's captured log is the
        fallback when no path is given.  The window starts at the scenario's own
        start, so one alert cannot fail every scenario after it.
        """

        if self.server_log:
            try:
                with open(self.server_log, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(self.alert_offset or self.log_offset)
                    return handle.read().splitlines()
            except OSError:
                return []
        return self.bridge.log(since=self.log_since, limit=1000)

    def mark_log(self) -> None:
        """Start a fresh alert window for the next scenario."""

        self.alert_offset = 0
        if self.server_log:
            try:
                self.alert_offset = os.path.getsize(self.server_log)
            except OSError:
                self.alert_offset = 0
        try:
            self.log_since = self.bridge.log_cursor()
        except Exception:  # a bridge that is not there only costs the check
            self.log_since = 0

    def anti_cheat_lines(self) -> list[str]:
        # The suite talks about its own alerts in chat, so a line that merely
        # quotes an alert is not one: chat lines carry ``<name>`` and the log
        # level ``Async Chat Thread``.
        return [
            line
            for line in self.log_lines()
            if any(marker in line for marker in ANTI_CHEAT)
            and self.username in line
            and "Async Chat Thread" not in line
            and f"<{self.username}>" not in line
        ]

    # -- dispatch -----------------------------------------------------
    async def kill(
        self, entity_id: int, *, max_hits: int = 40, timeout: float = 90.0
    ) -> int:
        """Hit a live entity until it is gone, walking after it when it escapes.

        A cow with its brain on walks away and is knocked back by every hit, so
        this is a chase: close the distance, aim at the box centre, hit, wait
        out the attack cooldown (a hit landed before it is up deals no damage),
        repeat.  The server validates the distance, not the crosshair, so once
        the target is inside three blocks the hit is sent even if the ray clips
        the ground first.
        """

        hits = 0
        self.last_kill_distance = None
        deadline = time.monotonic() + timeout
        while hits < max_hits and time.monotonic() < deadline:
            entity = self.bot.entities.get(entity_id)
            if entity is None:
                return hits
            distance = math.dist((entity.x, entity.y, entity.z), self.feet())
            if distance > 2.2:
                # Close and short steps: a fleeing cow moves while the path is
                # being planned, so one long walk to where it used to be is
                # wasted time.
                try:
                    await self.bot.navigate_to(
                        entity.x, entity.z, tolerance=1.0, timeout=2.5
                    )
                except Exception:
                    await self.settle(0.1)
                entity = self.bot.entities.get(entity_id)
                if entity is None:
                    return hits
                distance = math.dist((entity.x, entity.y, entity.z), self.feet())
                self.last_kill_distance = distance
                if distance > 3.0:
                    continue
            try:
                await self.bot.actions.look_at_entity(entity_id)
            except Exception:
                return hits
            await self.settle(0.05)
            # Hit through the crosshair, not through the entity table: a hit
            # whose ray misses the box is what Grim's Hitboxes check flags, and
            # that is exactly what a player cannot do either.
            result = await self.bot.actions.attack_looked_at(max_distance=3.0)
            if result is None or result.entity_id != entity_id:
                await self.settle(0.1)
                continue
            hits += 1
            # Attack strength only refills while no hit is landing; firing
            # faster than that deals a fraction of the damage.
            await self.settle(0.65)
        return hits

    def events(self, kind: str) -> list[dict[str, Any]]:
        """Server recorded events of one kind since the last clear.

        The bridge stores the event name in ``type``, but the ``break`` and
        ``place`` payloads also carry a ``type`` key (the block), which overwrites
        it, so those two are recognised by their unique fields instead.
        """

        entries = self.bridge.events(since=0, limit=800)
        if kind == "break":
            return [e for e in entries if "dataBefore" in e]
        if kind == "place":
            return [e for e in entries if "against" in e]
        return [e for e in entries if e.get("type") == kind]

    async def on_chat(self, sender, name, message, chat_type_id, target_name) -> None:
        # Our own messages come back as profileless chat with sender=None, so the
        # only way to recognise them is the name component.
        if self.own_uuid is not None and sender == self.own_uuid:
            return
        if self.username and _plain_component(name).strip() == self.username:
            return
        text = _plain_component(message).strip().lower()
        if not text:
            return
        print(f"[chat] {text!r}", flush=True)
        if text in ("0", "menu", "help", "?"):
            await self.say_many(menu_lines())
            return
        describe = re.fullmatch(r"\?\s*(\d{1,2})", text)
        if describe:
            item = by_number(int(describe.group(1)))
            if item is None:
                await self.say(f"没有第 {describe.group(1)} 项")
            else:
                await self.say(f"[#{item.number}] {item.category}·{item.title}")
            return
        if text == "stop":
            self.stop_requested = True
            await self.say("已请求中止当前测试")
            return
        if text == "all":
            await self.queue.put(("all", "all"))
            return
        match = re.fullmatch(r"(\d{1,2})", text)
        if match:
            await self.queue.put((match.group(1), text))
            return
        await self.say("看不懂，发 0 看菜单")

    async def worker(self) -> None:
        while True:
            kind, _raw = await self.queue.get()
            try:
                if kind == "all":
                    await self.run_all()
                    continue
                item = by_number(int(kind))
                if item is None:
                    await self.say(f"没有第 {kind} 项，发 0 看菜单")
                    continue
                await self.run_case(item)
            finally:
                self.queue.task_done()

    async def keep_ticking(self) -> None:
        """Tick like a vanilla client, even while the test code is not.

        A real client sends one movement packet per tick no matter what else it
        is doing.  This suite blocks on its own HTTP calls (setblock, command,
        inventory reads), and a pause looks like dropped ticks to the server and
        to a movement-prediction check: the next claim then disagrees with the
        prediction by most of a step.  Filling the gaps keeps the record
        continuous, which is what the scenarios are supposed to be testing.
        """

        while not self.bot.closed.is_set():
            await asyncio.sleep(0.02)
            if self.ticks_paused:
                # A vanilla client stops reporting movement while it sleeps, and
                # a claim made from the old spot after the server has already
                # laid the player in the bed reads as a spoofed movement.
                continue
            try:
                behind = time.monotonic() - (self.bot._last_tick_packet_time or 0.0)
                if behind > IDLE_TICK_SECONDS:
                    await self.bot.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def run_case(self, item: Case) -> bool:
        if self.bot.closed.is_set():
            print(f"[{item.number}] skipped: the bot is no longer connected", flush=True)
            return False
        self.busy = True
        self.stop_requested = False
        started = time.monotonic()
        await self.say(f"[#{item.number}] 开始：{item.category}·{item.title}")
        outcome = "PASS"
        detail = ""
        self.mark_log()
        try:
            await self.reset()
            # The window starts once the scene is clean: teleporting a bot out
            # of the previous scene is infrastructure, and a correction packet
            # during it would otherwise be charged to the case that follows.
            self.mark_log()
            await asyncio.wait_for(item.run(self), timeout=item.timeout)
        except asyncio.CancelledError:
            outcome, detail = "STOP", "已中止"
        except asyncio.TimeoutError:
            outcome, detail = "FAIL", f"超时 {item.timeout:g}s"
        except Exception as error:
            outcome, detail = "FAIL", f"{type(error).__name__}: {error}"
            print(traceback.format_exc(), flush=True)
        elapsed = time.monotonic() - started
        if outcome == "PASS":
            await self.say(f"[#{item.number}] ✓ {item.title} ({elapsed:.1f}s)")
        elif outcome == "STOP":
            await self.say(f"[#{item.number}] ⏹ {detail}")
        else:
            await self.say(f"[#{item.number}] ✗ {item.title}：{detail} ({elapsed:.1f}s)")
            print(f"[{item.number}] failure: {detail}", flush=True)
        self.busy = False
        self.results.append((item, outcome == "PASS", elapsed, detail))
        return outcome == "PASS"

    async def run_all(self) -> None:
        await self.say(f"全跑 {len(CASES)} 项，约需 5-8 分钟")
        for item in CASES:
            if self.stop_requested:
                await self.say("已中止")
                return
            await self.run_case(item)


# ---------------------------------------------------------------------------
# 1. digging, placing, aiming
# ---------------------------------------------------------------------------
@case(1, "挖掘/放置", "挖一个方块")
async def dig_one(s: Suite) -> None:
    target = s.clear_spot(s.spot(2, 0))
    s.setblock(target, "minecraft:dirt")
    await s.settle(0.3)
    assert s.state(target) != 0, "本地世界没看到泥土"
    s.bridge.clear_events()
    result = await s.bot.actions.dig(target, hold=1.2)
    assert result.status is ActionStatus.CONFIRMED, f"挖掘未确认：{result.evidence}"
    local = s.state(target)
    server = s.server_state(target)
    assert local == 0 and server == 0, f"本地={local} 服务端={server}"
    events = s.events("break")
    assert events, "服务端没有 break 事件"
    swings = s.events("swing")
    assert swings, "服务端没有记录摆臂（挖方块必须带 swing，否则反作弊会标记）"
    await s.say(f"    服务端 break {events[-1].get('block')} @ {target}")
    await s.say(f"    服务端 swing {len(swings)} 次 {swings[-1].get('animation')}")


@case(2, "挖掘/放置", "放一个方块")
async def place_one(s: Suite) -> None:
    target = s.clear_spot(s.spot(2, 0))
    await s.settle(0.3)
    s.give("minecraft:dirt", 4)
    await s.settle(0.4)
    await s.hold("minecraft:dirt")
    s.bridge.clear_events()
    result = await s.bot.actions.place_block(target)
    assert result.status is ActionStatus.CONFIRMED, f"放置未确认：{result.evidence}"
    server_type = s.bridge.block_type(s.world, target)
    assert "dirt" in server_type or "grass" in server_type, f"服务端是 {server_type}"
    events = s.events("place")
    assert events, "服务端没有 place 事件"
    assert s.events("swing"), "服务端没有记录摆臂"
    await s.say(f"    服务端 place {server_type} @ {target}")


@case(3, "挖掘/放置", "挥手与右键空击")
async def swing_one(s: Suite) -> None:
    s.bridge.clear_events()
    result = await s.bot.actions.swing()
    assert result.status is ActionStatus.SENT, f"挥手没有发出：{result.status}"
    await s.settle(0.4)
    events = s.events("swing")
    assert events, "服务端没有记录挥手"
    await s.say(f"    服务端 swing {events[-1].get('animation')}")


@case(4, "挖掘/放置", "射线命中")
async def raycast_one(s: Suite) -> None:
    target = s.clear_spot(s.spot(2, 0))
    s.setblock(target, "minecraft:stone")
    await s.settle(0.3)
    center = Vec3(target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)
    await s.bot.actions.look_at(center)
    await s.settle(0.2)
    hit = s.bot.actions.raycast()
    assert hit is not None, "视线里没有方块"
    assert hit.position == target, f"命中 {hit.position}，期望 {target}"
    assert 1.0 < hit.distance < 4.5, f"距离 {hit.distance}"
    await s.say(f"    命中 {hit.position} 面 {hit.face.name} 距离 {hit.distance:.2f}")


# ---------------------------------------------------------------------------
# 2. containers
# ---------------------------------------------------------------------------
@case(5, "容器", "箱子存取")
async def chest_round_trip(s: Suite) -> None:
    target = s.clear_spot(s.spot(2, 0))
    s.setblock(target, "minecraft:chest")
    await s.settle(0.4)
    await s.teleport((s.anchor[0] + 0.5, s.anchor[1], s.anchor[2] + 0.5))
    s.give("minecraft:dirt", 7)
    await s.settle(0.5)
    menu = await s.bot.menu.open_block(target)
    assert menu.slot_count >= 63, f"槽位数 {menu.slot_count}"
    moved = await menu.deposit("minecraft:dirt", 5)
    assert moved == 5, f"存入 {moved}"
    await s.settle(0.3)
    stored = server_items(
        s.command(f"data get block {target[0]} {target[1]} {target[2]} Items")
    )
    assert stored.get("minecraft:dirt") == 5, f"服务端箱子里是 {stored}"
    taken = await menu.withdraw("minecraft:dirt", 5)
    assert taken == 5, f"取出 {taken}"
    await s.settle(0.3)
    after = server_items(
        s.command(f"data get block {target[0]} {target[1]} {target[2]} Items")
    )
    assert after.get("minecraft:dirt", 0) == 0, f"服务端还剩 {after}"
    await menu.close()
    own = s.server_count("minecraft:dirt")
    assert own == 7, f"服务端背包 dirt={own}"
    await s.say(f"    存入 5 → 服务端 {stored}；取出 5 → 背包 dirt={own}")


@case(6, "容器", "十种菜单槽位")
async def menus_grid(s: Suite) -> None:
    plan = [
        ("minecraft:chest", "minecraft:generic_9x3"),
        ("minecraft:furnace", "minecraft:furnace"),
        ("minecraft:crafting_table", "minecraft:crafting"),
        ("minecraft:enchanting_table", "minecraft:enchantment"),
        ("minecraft:anvil", "minecraft:anvil"),
        ("minecraft:hopper", "minecraft:hopper"),
        ("minecraft:shulker_box", "minecraft:shulker_box"),
        ("minecraft:beacon", "minecraft:beacon"),
        ("minecraft:brewing_stand", "minecraft:brewing_stand"),
        ("minecraft:smithing_table", "minecraft:smithing"),
    ]
    seen: list[str] = []
    for index, (block, expected) in enumerate(plan):
        target = s.clear_spot(s.spot(2, 0))
        s.setblock(target, block)
        await s.settle(0.35)
        try:
            menu = await s.bot.menu.open_block(target)
        except Exception as error:
            server_block = s.bridge.block_type(s.world, target)
            above = s.bridge.block_type(s.world, (target[0], target[1] + 1, target[2]))
            windows = [
                (container_id, state.is_open, state.menu_type_id, state.slot_count)
                for container_id, state in sorted(s.bot.containers.items())
            ]
            eye = s.bot.physics_state.position
            ray = s.bot.actions.raycast()
            raise AssertionError(
                f"{block} 打不开：{error}；服务端 {target} 是 {server_block}，上面是 {above}；"
                f"眼 {eye}，本地射线 {ray}；手上 {s.bot.player_menu.carried}；窗口 {windows}"
            ) from error
        identifier = menu.identifier
        assert identifier == expected, f"{block} 打开的是 {identifier}"
        assert menu.slot_count > 0, f"{identifier} 没有槽位"
        assert not menu.layout.derived, f"{identifier} 落到派生布局"
        seen.append(f"{block.split(':')[-1]}:{menu.slot_count}")
        await menu.close()
        await s.settle(0.15)
    await s.say(f"    {len(plan)} 种全部对上：{' '.join(seen)}")


@case(7, "容器", "熔炉熔炼取物", timeout=150.0)
async def furnace_smelt(s: Suite) -> None:
    target = s.clear_spot(s.spot(2, 0))
    s.setblock(target, "minecraft:furnace")
    await s.settle(0.4)
    s.give("minecraft:cobblestone", 1)
    s.give("minecraft:coal", 1)
    await s.wait_count("minecraft:cobblestone", 1)
    await s.wait_count("minecraft:coal", 1)
    menu = await s.bot.menu.open_block(target)
    assert menu.identifier == "minecraft:furnace", f"打开的是 {menu.identifier}"
    groups = menu.layout.groups
    input_slot = groups.get("input", (0,))[0]
    fuel_slot = groups.get("fuel", (1,))[0]
    output_slot = groups.get("result", groups.get("output", (2,)))[0]
    cobble = await s.wait_menu_item(menu, "minecraft:cobblestone")
    coal = await s.wait_menu_item(menu, "minecraft:coal")
    assert cobble is not None and coal is not None, "背包里没有原料"
    moved_in = await menu.move(cobble, input_slot, count=1)
    moved_fuel = await menu.move(coal, fuel_slot, count=1)
    await s.settle(0.3)
    # A furnace starts burning the same tick the fuel lands, so the pile can be
    # gone from the slot before the click's echo arrives: "moved 0" is the truth
    # about the slot, not about the click. The fire is the proof that landed.
    lit = menu.data_value("lit_time")
    assert moved_in == 1 and (moved_fuel == 1 or lit > 0), (
        f"投料 {moved_in}/{moved_fuel}，槽位 in={input_slot} fuel={fuel_slot} "
        f"in={menu.item(input_slot)} fuel={menu.item(fuel_slot)} lit={lit} "
        f"cobble={cobble} coal={coal} carried={menu.carried} "
        f"state={menu.state_id} revision={menu.revision} "
        f"server_coal={s.server_count('minecraft:coal')} "
        f"server_cobble={s.server_count('minecraft:cobblestone')} "
        f"filled={[(slot, menu.item(slot).identifier) for slot in menu.slots('player') if menu.item(slot) and not menu.item(slot).empty]} "
        f"packets={s.journal[-14:]}"
    )
    progress_seen = 0
    stone_seen = False
    for _ in range(40):
        await s.settle(0.5)
        progress_seen = max(progress_seen, menu.data_value("cook_progress"))
        stack = menu.item(output_slot)
        if stack and not stack.empty and stack.identifier == "minecraft:stone":
            stone_seen = True
            break
    assert stone_seen, f"没有炼出石头（进度峰值 {progress_seen}）"
    taken = await menu.move(output_slot, _first_player_slot(menu))
    await s.settle(0.3)
    stored = server_items(
        s.command(f"data get block {target[0]} {target[1]} {target[2]} Items")
    )
    assert stored.get("minecraft:stone", 0) == 0, f"服务端熔炉里还有石头 {stored}"
    own = s.server_count("minecraft:stone")
    assert taken >= 1 and own >= taken, f"取出 {taken}，服务端背包 stone={own}"
    await menu.close()
    await s.say(f"    进度峰值 {progress_seen}/200，取出石头 {taken}，服务端背包 stone={own}")


def _first_player_slot(menu) -> int:
    slots = menu.slots("player")
    return slots[0] if slots else 0


@case(8, "容器", "村民交易")
async def merchant_trade(s: Suite) -> None:
    offers = (
        '{VillagerData:{profession:"minecraft:farmer",level:2},'
        "Offers:{Recipes:[{buy:{id:\"minecraft:emerald\",count:2},"
        "sell:{id:\"minecraft:bread\",count:6},maxUses:16,uses:0,xp:1,"
        "priceMultiplier:0.05f,specialPrice:0,demand:0,rewardExp:1b}]}}"
    )
    spot = (s.anchor[0] + 2.5, s.anchor[1], s.anchor[2] + 0.5)
    s.command(f"kill @e[tag={TAG},type=minecraft:villager]")
    await s.settle(0.2)
    before = set(s.bot.entities)
    s.command(f'summon minecraft:villager {spot[0]:.2f} {spot[1]:.2f} {spot[2]:.2f} {offers}')
    entity_id = await s.wait_entity("minecraft:villager", spot, before=before)
    s.give("minecraft:emerald", 2)
    await s.settle(0.5)
    entity = s.bot.entities[entity_id]
    menu = await s.bot.menu.open_entity(
        entity_id, position=Vec3(entity.x, entity.y, entity.z)
    )
    assert menu.identifier == "minecraft:merchant", f"打开的是 {menu.identifier}"
    deposited = await menu.deposit("minecraft:emerald", 2)
    assert deposited == 2, f"递上 {deposited} 个绿宝石"
    result_slot = menu.slot("result")
    bread = None
    for _ in range(30):
        await s.settle(0.2)
        stack = menu.item(result_slot)
        if stack and not stack.empty:
            bread = stack.identifier
            break
    assert bread == "minecraft:bread", f"结果槽是 {bread}"
    await menu.quick_move(result_slot)
    await s.settle(0.4)
    own = s.server_count("minecraft:bread")
    assert own >= 6, f"服务端背包 bread={own}"
    await menu.close()
    await s.say(f"    结果槽 {bread}，服务端背包 bread={own}")


# ---------------------------------------------------------------------------
# 3. crafting
# ---------------------------------------------------------------------------
@case(9, "合成", "背包里合木板")
async def craft_planks(s: Suite) -> None:
    s.give("minecraft:oak_log", 2)
    await s.settle(0.5)
    recipe = recipe_for("minecraft:oak_planks")
    menu = s.bot.player_menu
    made = await menu.craft(recipe, times=2)
    assert made == 8, f"合出 {made} 块木板"
    own = s.server_count("minecraft:oak_planks")
    assert own == 8, f"服务端背包 planks={own}"
    await s.say(f"    2 原木 → 服务端 planks={own}")


@case(10, "合成", "工作台合木棍")
async def craft_sticks(s: Suite) -> None:
    target = s.clear_spot(s.spot(2, 0))
    s.setblock(target, "minecraft:crafting_table")
    await s.settle(0.4)
    s.give("minecraft:oak_planks", 4)
    await s.settle(0.5)
    report = await s.bot.goals.run(
        CraftGoal(recipe_for("minecraft:stick"), times=1, table=target, timeout=2.0),
        timeout=60.0,
    )
    assert report.status is GoalStatus.DONE, f"goal {report.status}：{report.detail}"
    own = s.server_count("minecraft:stick")
    assert own == 4, f"服务端背包 stick={own}"
    await s.say(f"    3×3 合出木棍，服务端 stick={own}")


@case(11, "合成", "端到端合成链")
async def craft_chain(s: Suite) -> None:
    s.give("minecraft:oak_log", 8)
    await s.settle(0.6)
    planks = await s.bot.goals.run(
        CraftGoal(recipe_for("minecraft:oak_planks"), times=2), timeout=60.0
    )
    assert planks.status is GoalStatus.DONE, f"木板 {planks.detail}"
    table = await s.bot.goals.run(
        CraftGoal(recipe_for("minecraft:crafting_table")), timeout=60.0
    )
    assert table.status is GoalStatus.DONE, f"工作台 {table.detail}"
    slot = None
    for candidate in range(36, 45):
        stack = s.bot.get_inventory_item(candidate)
        if stack and not stack.empty and stack.identifier == "minecraft:crafting_table":
            slot = candidate
            break
    if slot is None:
        # A craft lands in the main inventory, so move it up to the hotbar first.
        menu = s.bot.player_menu
        source = menu.find("minecraft:crafting_table", where="player")
        assert source is not None, "背包里没有工作台"
        free = None
        for candidate in range(36, 45):
            stack = s.bot.get_inventory_item(candidate)
            if stack is None or stack.empty:
                free = candidate
                break
        assert free is not None, "热键栏满了"
        moved = await menu.move(source, free)
        assert moved >= 1, f"把工作台挪到热键栏失败：{moved}"
        slot = free
    await s.bot.select_hotbar_slot(slot - 36)
    target = await s.make_air(s.spot(2, 0))
    placed = await s.bot.actions.place_block(target)
    assert placed.status is ActionStatus.CONFIRMED, f"放置工作台：{placed.evidence}"
    await s.settle(0.4)
    sticks = await s.bot.goals.run(
        CraftGoal(recipe_for("minecraft:stick"), times=1, table=target, timeout=2.0),
        timeout=60.0,
    )
    assert sticks.status is GoalStatus.DONE, f"木棍 {sticks.detail}"
    text = s.command(f"data get entity {s.username} Inventory")
    items = server_items(text)
    assert items.get("minecraft:oak_log") == 6, f"原木 {items}"
    assert items.get("minecraft:stick") == 4, f"木棍 {items}"
    await s.say(f"    原木6 木板{items.get('minecraft:oak_planks', 0)} 木棍4（服务端 NBT）")


# ---------------------------------------------------------------------------
# 4. navigation
# ---------------------------------------------------------------------------
@case(12, "导航", "短距离寻路")
async def navigate_short(s: Suite) -> None:
    start = s.feet()
    goal_x = start[0] + 10
    await s.bot.navigate_to(goal_x + 0.5, start[2] + 0.5, timeout=40.0)
    await s.settle(0.4)
    x, _, z = s.feet()
    assert abs(x - goal_x) <= 2, f"停在 x={x}"
    server = _server_position(s)
    assert abs(server[0] - goal_x) <= 2.5, f"服务端 x={server[0]}"
    await s.say(f"    走到 x={x}，服务端 {server[0]:.1f}")


@case(13, "导航", "远距离跨区块")
async def travel_far(s: Suite) -> None:
    start = s.feet()
    target_x = start[0] + 60
    report = await s.bot.goals.run(
        TravelGoal(target_x + 0.5, start[2] + 0.5, tolerance=1.0, timeout=180.0),
        timeout=240.0,
    )
    assert report.status is GoalStatus.DONE, f"travel {report.status}：{report.detail}"
    server = _server_position(s)
    assert abs(server[0] - (target_x + 0.5)) <= 2.0, f"服务端 x={server[0]}"
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"
    await s.say(f"    走了 {abs(server[0] - start[0]):.1f} 格，无常速告警")


@case(14, "导航", "往返不漂移")
async def travel_back(s: Suite) -> None:
    start = s.feet()
    out = start[0] + 40
    first = await s.bot.goals.run(
        TravelGoal(out + 0.5, start[2] + 0.5, tolerance=1.0, timeout=150.0), timeout=200.0
    )
    assert first.status is GoalStatus.DONE, f"去程 {first.detail}"
    back = await s.bot.goals.run(
        TravelGoal(start[0] + 0.5, start[2] + 0.5, tolerance=1.0, timeout=150.0),
        timeout=200.0,
    )
    assert back.status is GoalStatus.DONE, f"回程 {back.detail}"
    server = _server_position(s)
    assert abs(server[0] - (start[0] + 0.5)) <= 2.0, f"回程停在 x={server[0]}"
    await s.say(f"    往返 {2 * 40} 格，回到服务端 x={server[0]:.1f}")


def _server_position(s: Suite) -> tuple[float, float, float]:
    for player in s.bridge.players():
        if player.get("name") == s.username:
            return (
                float(player.get("x") or 0.0),
                float(player.get("y") or 0.0),
                float(player.get("z") or 0.0),
            )
    raise AssertionError(f"桥没有 {s.username} 的位置")


# ---------------------------------------------------------------------------
# 5. combat
# ---------------------------------------------------------------------------
@case(15, "战斗", "打牛到死", timeout=180.0)
async def kill_cow(s: Suite) -> None:
    spot = (s.anchor[0] + 2.5, float(s.anchor[1]), s.anchor[2] + 0.5)
    # A sword, because that is what a player brings to a fight: a bare hand
    # needs ten hits and gives a live cow ten chances to walk out of reach.
    s.give("minecraft:iron_sword", 1)
    await s.settle(0.4)
    await s.hold("minecraft:iron_sword")
    # A live cow, brain included: every hit knocks it back, so the killer has to
    # keep walking after it instead of standing in one place swinging.
    entity_id = await s.summon("minecraft:cow", spot)
    await s.bot.actions.look_at_entity(entity_id)
    health_before = s.entity_health("minecraft:cow")
    assert health_before and health_before > 0, f"服务端血量 {health_before}"
    swings = await s.kill(entity_id)
    await s.settle(0.5)
    health_after = s.entity_health("minecraft:cow")
    assert health_after is None, (
        f"牛还活着：{swings} 次命中后服务端血量 {health_after}，"
        f"最后距离 {s.last_kill_distance}"
    )
    assert not any(eid == entity_id for eid in s.bot.entities), "客户端还留着实体"
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"
    await s.say(f"    服务端血量 {health_before:.0f} → 消失，{swings} 个攻击包")


@case(16, "战斗", "选中最近目标并实战命中")
async def pick_nearest(s: Suite) -> None:
    # Two live cows, one inside the three block attack range and one outside it.
    # The test is not "the ray sees a cow": it is "the hit lands on the near cow
    # and the far cow keeps its health".
    near_tag = f"{TAG}-near"
    far_tag = f"{TAG}-far"
    for tag in (near_tag, far_tag):
        s.command(f"kill @e[tag={tag}]")
    await s.settle(0.3)
    near = (s.anchor[0] + 2.2, float(s.anchor[1]), s.anchor[2] + 0.5)
    far = (s.anchor[0] + 2.2, float(s.anchor[1]), s.anchor[2] + 5.2)
    before = set(s.bot.entities)
    s.command(
        f'summon minecraft:cow {far[0]:.2f} {far[1]:.2f} {far[2]:.2f} '
        f'{{Tags:["{far_tag}"],PersistenceRequired:1b}}'
    )
    far_id = await s.wait_entity("minecraft:cow", far, before=before)
    before.add(far_id)
    s.command(
        f'summon minecraft:cow {near[0]:.2f} {near[1]:.2f} {near[2]:.2f} '
        f'{{Tags:["{near_tag}"],PersistenceRequired:1b}}'
    )
    near_id = await s.wait_entity("minecraft:cow", near, before=before)
    await s.settle(0.3)

    center = s.bot.actions.entity_center(near_id)
    assert center is not None, "最近的牛没有被跟踪"
    await s.bot.actions.look_at(center)
    await s.settle(0.2)
    hit = s.bot.actions.raycast_entity()
    assert hit is not None, "射线里没有实体"
    assert hit.entity_id == near_id, f"选中 {hit.entity_id}，最近的是 {near_id}"

    near_before = s.entity_health("minecraft:cow", tag=near_tag)
    far_before = s.entity_health("minecraft:cow", tag=far_tag)
    assert near_before is not None and near_before > 0, f"近牛服务端血量 {near_before}"
    assert far_before is not None and far_before > 0, f"远牛服务端血量 {far_before}"
    result = await s.bot.actions.attack_looked_at(max_distance=3.0)
    assert result is not None, "射线在牛身上却打不出去"
    assert result.entity_id == near_id, f"打到了 {result.entity_id}，不是 {near_id}"
    await s.settle(0.7)
    near_after = s.entity_health("minecraft:cow", tag=near_tag)
    far_after = s.entity_health("minecraft:cow", tag=far_tag)
    assert near_after is None or near_after < near_before, (
        f"近牛没掉血：{near_before} → {near_after}"
    )
    assert far_after == far_before, f"远牛也掉血了：{far_before} → {far_after}"
    for tag in (near_tag, far_tag):
        s.command(f"kill @e[tag={tag}]")
    await s.say(
        f"    射线选中 {near_id}（近，{hit.distance:.2f} 格）；"
        f"实战命中：近牛 {near_before:.0f}→{near_after if near_after is not None else '死'}，"
        f"远牛 {far_before:.0f} 未变"
    )


# ---------------------------------------------------------------------------
# 6. hardness and tools
# ---------------------------------------------------------------------------
@case(17, "工具", "硬度表数值")
async def hardness_values(s: Suite) -> None:
    version = s.bot.version
    table = version.breaking
    assert table is not None, "这个版本没有硬度表"
    stone_hand = table.seconds("minecraft:stone")
    stone_iron = table.seconds("minecraft:stone", tool="minecraft:iron_pickaxe")
    obsidian_diamond = table.seconds("minecraft:obsidian", tool="minecraft:diamond_pickaxe")
    dirt_hand = table.seconds("minecraft:dirt")
    bedrock = table.seconds("minecraft:bedrock")
    assert abs(stone_hand - 7.5) < 0.01, f"石头徒手 {stone_hand}"
    assert abs(stone_iron - 0.4) < 0.01, f"石头铁镐 {stone_iron}"
    assert abs(obsidian_diamond - 9.4) < 0.01, f"黑曜石钻石镐 {obsidian_diamond}"
    assert abs(dirt_hand - 0.75) < 0.01, f"泥土徒手 {dirt_hand}"
    assert bedrock == float("inf"), f"基岩 {bedrock}"
    assert version.hardness("minecraft:stone") == 1.5, "version.hardness 不对"
    assert s.bot.actions.block_name(await _stone_state(s)) == "minecraft:stone", "block_name 不对"
    await s.say(
        f"    石徒手{stone_hand:g}s 铁镐{stone_iron:g}s 黑曜石钻石镐{obsidian_diamond:g}s 基岩∞"
    )


async def _stone_state(s: Suite) -> int:
    target = s.clear_spot(s.spot(2, 0))
    s.setblock(target, "minecraft:stone")
    for _ in range(20):
        await s.settle(0.05)
        state = s.state(target)
        if state != 0:
            return state
    raise AssertionError("本地世界没看到石头")


@case(18, "工具", "工具选择与挖掘速度")
async def tool_speed(s: Suite) -> None:
    iron_spot = s.clear_spot(s.spot(2, 0))
    wood_spot = s.clear_spot(s.spot(-2, 0))
    for spot in (iron_spot, wood_spot):
        s.setblock(spot, "minecraft:stone")
    await s.settle(0.4)
    s.give("minecraft:iron_pickaxe", 1)
    s.give("minecraft:wooden_pickaxe", 1)
    await s.settle(0.5)

    async def timed(spot: tuple[int, int, int], tool: str) -> float:
        await s.hold(tool)
        await s.settle(0.25)
        selected = s.command(f"data get entity {s.username} SelectedItem")
        assert tool.split(":")[-1] in selected, f"手上不是 {tool}：{selected}"
        updates: list[float] = []

        def on_update(x, y, z, state_id) -> None:
            if (x, y, z) == spot and state_id == 0:
                updates.append(time.monotonic())

        s.bot.events.on("block_update", on_update)
        started = time.monotonic()
        # hold=None asks the library for a vanilla dig: it keeps the button
        # down for the predicted time plus a margin and then releases.  An
        # explicit short hold is an early release, and vanilla closes that with
        # ABORT -- which cancels the dig on the server instead of finishing it.
        result = await s.bot.actions.dig(spot, hold=None)
        for _ in range(80):
            if updates:
                break
            await s.settle(0.05)
        assert result.status is ActionStatus.CONFIRMED or updates, "方块没破"
        assert s.server_state(spot) == 0, "服务端方块还在"
        return (updates[0] - started) if updates else (time.monotonic() - started)

    iron_delay = await timed(iron_spot, "minecraft:iron_pickaxe")
    wood_delay = await timed(wood_spot, "minecraft:wooden_pickaxe")
    assert iron_delay < 0.9, f"铁镐破石头用了 {iron_delay:.2f}s，比 0.4s 的基准慢太多"
    assert wood_delay > iron_delay + 0.4, (
        f"木镐 {wood_delay:.2f}s 与铁镐 {iron_delay:.2f}s 没有拉开差距"
    )
    await s.say(
        f"    铁镐破石 {iron_delay:.2f}s，木镐 {wood_delay:.2f}s（服务端基准 0.4 / 1.15）"
    )


# ---------------------------------------------------------------------------
# 7. data components
# ---------------------------------------------------------------------------
KNOWN_ITEMS: tuple[tuple[int, str, int], ...] = (
    (0, "minecraft:stone", 3),
    (1, "minecraft:diamond_pickaxe[minecraft:damage=1500]", 1),
    (2, "minecraft:dirt", 7),
    (3, 'minecraft:enchanted_book[minecraft:stored_enchantments={"minecraft:sharpness":3}]', 1),
    (4, "minecraft:oak_planks", 5),
    (5, 'minecraft:netherite_sword[minecraft:custom_name=\'"Probe"\']', 1),
    (6, "minecraft:apple", 2),
    (7, "minecraft:netherite_pickaxe[minecraft:unbreakable={}]", 1),
    (8, "minecraft:stone", 9),
)

UNKNOWN_ITEMS: tuple[tuple[int, str, int], ...] = (
    (0, 'minecraft:potion[minecraft:potion_contents={potion:"minecraft:healing"}]', 1),
    (1, "minecraft:dirt", 4),
    (
        2,
        'minecraft:shulker_box[minecraft:container=[{slot:0b,item:{id:"minecraft:diamond",count:5}}]]',
        1,
    ),
)


@case(19, "组件", "已知组件整窗解码")
async def components_known(s: Suite) -> None:
    target = s.clear_spot(s.spot(2, 0))
    s.setblock(target, "minecraft:chest")
    await s.settle(0.4)
    for slot, spec, amount in KNOWN_ITEMS:
        s.command(
            f"item replace block {target[0]} {target[1]} {target[2]} "
            f"container.{slot} with {spec} {amount}"
        )
    await s.settle(0.4)
    menu = await s.bot.menu.open_block(target)
    assert menu.slot_count >= 63, f"槽位 {menu.slot_count}"
    assert not menu.state().unreadable, f"不可解槽位 {sorted(menu.state().unreadable)[:5]}"
    decoded: list[str] = []
    for slot in range(9):
        stack = menu.item(slot)
        decoded.append(f"{slot}:{(stack.identifier or 'air').split(':')[-1]}x{stack.count}")
    server = server_items(
        s.command(f"data get block {target[0]} {target[1]} {target[2]} Items")
    )
    expected = {
        "minecraft:stone": 12,
        "minecraft:diamond_pickaxe": 1,
        "minecraft:dirt": 7,
        "minecraft:enchanted_book": 1,
        "minecraft:oak_planks": 5,
        "minecraft:netherite_sword": 1,
        "minecraft:apple": 2,
        "minecraft:netherite_pickaxe": 1,
    }
    assert server == expected, f"服务端 {server}"
    damaged = menu.item(1)
    assert damaged.has_components, "钻石镐的 damage 组件丢了"
    assert damaged.item_id is not None, "钻石镐没有 item id"
    await menu.close()
    await s.say(f"    9 槽全解：{' '.join(decoded)}")


@case(20, "组件", "未知组件诚实拒解")
async def components_unknown(s: Suite) -> None:
    target = s.clear_spot(s.spot(2, 0))
    s.setblock(target, "minecraft:chest")
    await s.settle(0.4)
    for slot, spec, amount in UNKNOWN_ITEMS:
        s.command(
            f"item replace block {target[0]} {target[1]} {target[2]} "
            f"container.{slot} with {spec} {amount}"
        )
    await s.settle(0.4)
    menu = await s.bot.menu.open_block(target)
    unreadable = sorted(menu.state().unreadable)
    assert unreadable, "带未知组件的窗反而被解开了"
    assert not menu.readable(0), "未知组件槽被当成可读"
    picked = await menu.pickup(0)
    assert picked, "点不动那个药水槽"
    carried = menu.carried
    assert carried.identifier == "minecraft:potion", f"光标上是 {carried.identifier}"
    assert carried.count == 1, f"光标 {carried.count}"
    assert carried.has_components, "光标的组件丢了"
    await menu.pickup(0)
    await s.settle(0.4)
    stored = server_items(
        s.command(f"data get block {target[0]} {target[1]} {target[2]} Items")
    )
    assert stored.get("minecraft:potion") == 1, f"放回后服务端 {stored}"
    await menu.close()
    await s.say(
        f"    整窗拒解 {len(unreadable)} 槽，光标单栈精确解出 potion，放回后服务端一致"
    )


# ---------------------------------------------------------------------------
# 8. mixed scenarios
# ---------------------------------------------------------------------------
@case(21, "混合", "伐木工：走→挖→合成→放置→合成", timeout=180.0)
async def mixed_lumberjack(s: Suite) -> None:
    # A three block log column a couple of blocks away, plus the walk that
    # reaches it.  The column floats one block up (the floor block underneath
    # stays), so every drop falls onto the walking surface: a hole that has to
    # be stepped into would add a one block fall to the movement record, which
    # is a movement-prediction edge case this scenario has no business testing.
    # The walk goes along z, clear of the column.
    column = [s.clear_spot(s.spot(3, 2, dy)) for dy in (1, 2, 3)]
    for position in column:
        s.setblock(position, "minecraft:oak_log")
    await s.settle(0.4)
    start = s.feet()
    walked = await s.bot.goals.run(
        TravelGoal(start[0] + 0.5, start[2] + 2.5, tolerance=1.0, timeout=90.0),
        timeout=120.0,
    )
    assert walked.status is GoalStatus.DONE, f"走过去 {walked.detail}"
    for position in column:
        # No explicit hold: the library holds for the predicted break time plus
        # a small margin and then releases, which is what a vanilla client does.
        # An explicit short hold would go out as ABORT (a "fast break" to the
        # anti-cheat) and leave the block standing.
        result = await s.bot.actions.dig(position)
        assert result.status is ActionStatus.CONFIRMED, (
            f"{position} 没挖掉：{result.evidence}"
            f"；feet={s.feet()} eye={s.bot.actions.eye_position()}"
            f" visible={[face.name for face in s.bot.actions.visible_faces(position)]}"
            f" pick={s.bot.actions.pick_face(position)}"
            f" extra={result.extra}"
        )
    # Drops fly out of the block with a small random offset, so walking to
    # guessed spots was a lottery.  The library now enumerates the dropped
    # items the server actually told us about (entity metadata carries the
    # stack) and walks to each one until the inventory agrees.
    report = await s.bot.pick_up(
        items=["minecraft:oak_log"], count=3, radius=8.0, allow_loss=-1, timeout=45.0
    )
    assert report.ok, f"没捡齐：{report.describe()}；{report.reason}"
    logs = await s.wait_server_count("minecraft:oak_log", 3, timeout=3.0)
    assert logs == 3, f"服务端背包 log={logs}"
    planks = await s.bot.goals.run(
        CraftGoal(recipe_for("minecraft:oak_planks"), times=3), timeout=90.0
    )
    assert planks.status is GoalStatus.DONE, f"木板 {planks.detail}"
    table = await s.bot.goals.run(
        CraftGoal(recipe_for("minecraft:crafting_table")), timeout=90.0
    )
    assert table.status is GoalStatus.DONE, f"工作台 {table.detail}"
    table_spot = await s.make_air(s.spot(2, 0))
    await s.hold("minecraft:crafting_table")
    placed = await s.bot.actions.place_block(table_spot)
    assert placed.status is ActionStatus.CONFIRMED, f"放工作台：{placed.evidence}"
    assert "crafting_table" in s.bridge.block_type(s.world, table_spot), "服务端没放下工作台"
    await s.settle(0.4)
    sticks = await s.bot.goals.run(
        CraftGoal(recipe_for("minecraft:stick"), times=1, table=table_spot, timeout=2.0),
        timeout=90.0,
    )
    assert sticks.status is GoalStatus.DONE, f"木棍 {sticks.detail}"
    items = server_items(s.command(f"data get entity {s.username} Inventory"))
    assert items.get("minecraft:stick") == 4, f"木棍 {items}"
    await s.say(
        f"    挖 3 原木 → 木板{items.get('minecraft:oak_planks', 0)} → 工作台落地 → 木棍4"
    )


@case(22, "混合", "仓储：存取→合成→存回", timeout=180.0)
async def mixed_warehouse(s: Suite) -> None:
    chest = s.clear_spot(s.spot(2, 0))
    s.setblock(chest, "minecraft:chest")
    await s.settle(0.4)
    s.give("minecraft:oak_log", 10)
    held = await s.wait_count("minecraft:oak_log", 10)
    assert held >= 10, f"背包里只有 {held} 个原木"
    menu = await s.bot.menu.open_block(chest)
    try:
        stored = await menu.deposit("minecraft:oak_log", 10)
    except Exception as error:
        window = [
            (slot, str(menu.item(slot)))
            for slot in menu.slots("player")
            if menu.item(slot) and not menu.item(slot).empty
        ]
        bag = [
            (slot, str(s.bot.get_inventory_item(slot)))
            for slot in range(46)
            if s.bot.get_inventory_item(slot) and not s.bot.get_inventory_item(slot).empty
        ]
        server = server_items(s.command(f"data get entity {s.username} Inventory"))
        raise AssertionError(
            f"存入失败 {type(error).__name__}: {error}；窗口 {menu.identifier} "
            f"容器 {menu.container_id} state={menu.state_id} 槽位 {window} "
            f"模型背包 {bag} 服务端 {server}"
        ) from error
    assert stored == 10, f"存入 {stored}"
    await s.settle(0.3)
    left = server_items(s.command(f"data get block {chest[0]} {chest[1]} {chest[2]} Items"))
    assert left.get("minecraft:oak_log") == 10, f"服务端箱子 {left}"
    taken = await menu.withdraw("minecraft:oak_log", 4)
    assert taken == 4, f"取出 {taken}"
    await menu.close()
    await s.settle(0.3)
    planks = await s.bot.goals.run(
        CraftGoal(recipe_for("minecraft:oak_planks"), times=4), timeout=90.0
    )
    assert planks.status is GoalStatus.DONE, f"木板 {planks.detail}"
    menu = await s.bot.menu.open_block(chest)
    back = await menu.deposit("minecraft:oak_planks", 4)
    assert back == 4, f"木板存回 {back}"
    await s.settle(0.3)
    final = server_items(s.command(f"data get block {chest[0]} {chest[1]} {chest[2]} Items"))
    assert final.get("minecraft:oak_log") == 6, f"箱子里原木 {final}"
    assert final.get("minecraft:oak_planks") == 4, f"箱子里木板 {final}"
    await menu.close()
    await s.say(f"    箱子 10→取4→合木板→存回：{final}")


@case(23, "混合", "远征：走远→杀牛→走回", timeout=240.0)
async def mixed_expedition(s: Suite) -> None:
    start = s.feet()
    out_x = start[0] + 30
    out = await s.bot.goals.run(
        TravelGoal(out_x + 0.5, start[2] + 0.5, tolerance=1.5, timeout=150.0), timeout=200.0
    )
    assert out.status is GoalStatus.DONE, f"远征 {out.detail}"
    await s.settle(0.4)
    spot = (s.position()[0] + 2.0, float(s.feet()[1]), s.position()[2])
    # A sword in the hotbar, the way a player would set out, and a real cow with
    # its brain on: it walks, it gets knocked back, and the killer has to follow
    # it, which is what a live server looks like.
    s.give("minecraft:iron_sword", 1)
    await s.settle(0.4)
    await s.hold("minecraft:iron_sword")
    entity_id = await s.summon("minecraft:cow", spot)
    await s.bot.actions.look_at_entity(entity_id)
    kills = await s.kill(entity_id)
    if s.entity_health("minecraft:cow") is not None:
        kills += await s.kill(entity_id)
    assert s.entity_health("minecraft:cow") is None, (
        f"远处的牛没死（{kills} 次命中，最后距离 {s.last_kill_distance}）"
    )
    home = await s.bot.goals.run(
        TravelGoal(start[0] + 0.5, start[2] + 0.5, tolerance=1.5, timeout=150.0),
        timeout=200.0,
    )
    assert home.status is GoalStatus.DONE, f"回程 {home.detail}"
    server = _server_position(s)
    assert abs(server[0] - (start[0] + 0.5)) <= 2.5, f"停在 x={server[0]}"
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"
    await s.say(f"    往返 60 格 + {kills} 次攻击，回到 x={server[0]:.1f}，无常速告警")


@case(24, "机器菜单", "机器菜单：切石/砂轮/铁砧/锻造", timeout=300.0)
async def machine_menus(s: Suite) -> None:
    menu = s.bot.menu

    async def open_machine(dx: int, block: str) -> Menu:
        spot = s.clear_spot(s.spot(dx, 0))
        s.setblock(spot, block)
        await s.wait_chunk(spot)
        await s.settle(0.4)
        window = await menu.open_block(spot)
        names = {
            "minecraft:enchanting_table": "minecraft:enchantment",
            "minecraft:smithing_table": "minecraft:smithing",
        }
        expected = names.get(block, block)
        await s.say(f"    {block} → {window.identifier} {window.slot_count} 格")
        assert window.identifier == expected, f"{block} 打开了 {window.identifier}"
        # Six clicks in a row, each with its own turn, otherwise end up close
        # enough that one lands inside the next tick and is reported as a
        # misordered placement.  A moment of ordinary ticking between windows
        # keeps the packet stream looking like a player reading each menu.
        await s.settle(0.25)
        return window

    async def wait_result(window: Menu, *, timeout: float = 3.0) -> None:
        """Give the server a moment to fill the result slot after a button."""

        slot = window.layout.slot("result")
        deadline = time.monotonic() + timeout
        while window.item(slot).count == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    # Every machine follows the same three steps, so each block below is
    # "stock the inputs, press the button or type the name, take the result".
    window = await open_machine(-3, "minecraft:stonecutter")
    try:
        s.give("minecraft:stone", 4)
        await s.settle(0.5)
        cut = await window.stonecutter("minecraft:stone", recipe=0)
        assert cut.count > 0, "切石机没有产出"
    finally:
        await window.close()
    await s.say(f"    切石机 → {cut.identifier} x{cut.count}")

    window = await open_machine(-2, "minecraft:grindstone")
    try:
        s.give(
            'minecraft:diamond_sword[minecraft:enchantments={"minecraft:sharpness":3}]',
            1,
        )
        await s.settle(0.5)
        polished = await window.grindstone("minecraft:diamond_sword")
        assert polished.identifier == "minecraft:diamond_sword", polished.identifier
    finally:
        await window.close()
    await s.say("    砂轮 → 附魔过的剑变回普通剑（服务端产物）")

    window = await open_machine(-1, "minecraft:anvil")
    try:
        s.give("minecraft:iron_sword", 1)
        await s.settle(0.5)
        renamed = await window.anvil("minecraft:iron_sword", name="ProtoBlade")
        assert renamed.identifier == "minecraft:iron_sword", renamed.identifier
    finally:
        await window.close()
    await s.say("    铁砧 → 改名成功并取走产物")

    window = await open_machine(0, "minecraft:smithing_table")
    try:
        s.give("minecraft:netherite_upgrade_smithing_template", 1)
        s.give("minecraft:diamond_sword", 1)
        s.give("minecraft:netherite_ingot", 1)
        await s.settle(0.6)
        upgraded = await window.smithing(
            "minecraft:netherite_upgrade_smithing_template",
            "minecraft:diamond_sword",
            "minecraft:netherite_ingot",
        )
        assert upgraded.identifier == "minecraft:netherite_sword", upgraded.identifier
    finally:
        await window.close()
    await s.say("    锻造台 → 钻石剑升级成下界合金剑")

    # The loom and the enchanting table are wired through the same two
    # primitives (button + result); their live scenes land in a follow-up case.
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"


@case(25, "容器失效", "容器失效：走远与方块被拆", timeout=120.0)
async def machine_liveness(s: Suite) -> None:
    spot = s.clear_spot(s.spot(2, 0))
    s.setblock(spot, "minecraft:enchanting_table")
    await s.wait_chunk(spot)
    await s.settle(0.4)
    window = await s.bot.menu.open_block(spot)
    window.live()  # reachable and intact: the check must pass
    # Walking away: the server closes the window, and even before that the
    # library refuses to click a menu the player can no longer reach.  The walk
    # goes through TravelGoal rather than a bare navigate_to: a long single-shot
    # path leaves tick gaps that the server's movement prediction then flags.
    goal = TravelGoal(
        spot[0] + 12.5,
        spot[2] + 0.5,
        tolerance=1.0,
        timeout=60.0,
    )
    outcome = await s.bot.goals.run(goal)
    assert outcome.ok, f"没走到远处：{outcome}"
    await s.settle(0.6)
    with_raise = False
    try:
        window.live()
    except MenuNotOpen as error:
        with_raise = True
        assert "blocks from" in str(error) or "closed the window" in str(error), error
    assert with_raise, "走远之后容器仍然被当成可用"
    await s.bot.navigate_to(spot[0] - 1.5, spot[2] + 0.5, timeout=20.0)
    await s.settle(0.4)
    # Breaking the block under a live window: the server sends the close packet.
    window = await s.bot.menu.open_block(spot)
    s.setblock(spot, "minecraft:air")
    await s.settle(0.6)
    menu_gone = False
    try:
        window.live()
    except MenuNotOpen:
        menu_gone = True
    assert menu_gone, "方块被拆掉之后容器仍然被当成可用"
    await s.say("    走远→拒绝点击；拆方块→窗口失效（两态都被拦住）")


# ---------------------------------------------------------------------------
# 10. watching a bot's inventory from the server plugin
# ---------------------------------------------------------------------------


def _menu_to_chest_slot(slot: int) -> int | None:
    """The chest slot that holds the item the player menu keeps at ``slot``.

    The bridge plugin copies the player inventory index for index (hotbar, main,
    armour, offhand), while the protocol numbers the same slots differently
    inside the player menu.  Slots the plugin does not mirror (the crafting
    grid) map to ``None``.
    """

    if 36 <= slot <= 44:  # hotbar
        return slot - 36
    if 9 <= slot <= 35:  # main inventory
        return slot
    if 5 <= slot <= 8:  # armour: boots, leggings, chestplate, helmet
        return 36 + (slot - 5)
    if slot == 45:  # offhand
        return 40
    return None


def _protocol_items(bot) -> dict[int, tuple[str, int]]:
    """Slot -> (identifier, count) from the client's own view of its inventory."""

    state = bot.containers.get(0)
    items: dict[int, tuple[str, int]] = {}
    for slot, stack in (getattr(state, "items", None) or {}).items():
        if stack is not None and stack.count > 0:
            items[slot] = (stack.identifier, stack.count)
    return items


def _open_view_window(bot) -> tuple[int, Any] | tuple[None, None]:
    for container_id, state in bot.containers.items():
        if container_id != 0 and state.is_open:
            return container_id, state
    return None, None


def _mapped(items: dict[int, tuple[str, int]]) -> dict[int, tuple[str, int]]:
    mapped: dict[int, tuple[str, int]] = {}
    for slot, item in items.items():
        chest = _menu_to_chest_slot(slot)
        if chest is not None:
            mapped[chest] = item
    return mapped


@case(26, "观看模式", "观看模式：/botregister 与 /see 的 chest/inv 两种视角", timeout=180.0)
async def spectator_view(s: Suite) -> None:
    """A second client is watched by this one through the bridge plugin.

    The watched client registers itself with /botregister; this client becomes
    op and uses /seepattern + /see.  Both patterns are checked against the
    watched client's own view of its slots, so a wrong slot mapping cannot pass.
    """

    watched_name = "WatchedBot"
    watched = await connect(
        getattr(s.bot, "host", "127.0.0.1"),
        port=int(getattr(s.bot, "port", 25565)),
        username=watched_name,
        version=s.bot.version.name,
        timeout=30.0,
    )
    try:
        for _ in range(600):
            position = watched.physics_state.position
            key = (math.floor(position.x) >> 4, math.floor(position.z) >> 4)
            if watched.player.loaded and key in watched.world.chunks:
                break
            await watched.tick()
        s.command(f"gamemode survival {watched_name}")
        s.command(f"clear {watched_name}")
        s.command(f"give {watched_name} minecraft:diamond 4")
        s.command(f"give {watched_name} minecraft:bread 2")
        await s.settle(1.2)
        await watched.register_as_bot()
        await s.settle(0.8)
        listing = s.bridge.call("/bots")
        assert watched_name in (listing.get("registered") or []), f"注册失败：{listing}"

        s.command(f"op {s.username}")
        s.clear_inventory()
        await s.settle(0.4)

        # -- chest pattern: a live virtual chest, closing it leaves the view --
        await s.bot.send_command("seepattern chest")
        await s.bot.send_command(f"see {watched_name}")
        container_id, state = None, None
        for _ in range(150):
            container_id, state = _open_view_window(s.bot)
            if container_id is not None:
                break
            await s.settle(0.1)
        assert container_id is not None, "/see 之后没有打开箱子界面"
        assert state.slot_count >= 45, f"窗口只有 {state.slot_count} 格：{state}"
        shown = {
            slot: (stack.identifier, stack.count)
            for slot, stack in (state.items or {}).items()
            if stack is not None and stack.count > 0 and slot < 45
        }
        expected = _mapped(_protocol_items(watched))
        assert shown == expected, f"箱子视角与 bot 背包不一致：{shown} != {expected}"

        # live: the window follows the bot without reopening anything
        s.command(f"give {watched_name} minecraft:emerald 1")
        await s.settle(1.2)
        fresh = _open_view_window(s.bot)[1]
        shown = {
            slot: (stack.identifier, stack.count)
            for slot, stack in (fresh.items or {}).items()
            if stack is not None and stack.count > 0 and slot < 45
        }
        expected = _mapped(_protocol_items(watched))
        assert shown == expected, f"实时更新没跟上：{shown} != {expected}"

        await s.bot.send_command("see stop")
        await s.settle(0.8)
        assert _open_view_window(s.bot)[0] is None, "/see stop 没有关掉箱子界面"
        # The window closes on our side at once; the plugin's bookkeeping runs on
        # its own tick, so give it a moment before calling the session leaked.
        views: list = [1]
        for _ in range(20):
            views = s.bridge.call("/bots").get("views") or []
            if not views:
                break
            await s.settle(0.1)
        assert not views, f"服务端还留着观看会话：{views}"

        # -- inventory pattern: 1:1 into our own slots, closing does not exit --
        await s.bot.send_command("seepattern inv")
        await s.bot.send_command(f"see {watched_name}")
        await s.settle(1.4)
        mirrored = _protocol_items(s.bot)
        source = _protocol_items(watched)
        assert mirrored == source, f"原生背包视角不是 1:1：{mirrored} != {source}"
        assert s.bridge.call("/bots").get("views"), "inv 模式的观看会话没有登记"
        # In this pattern no window of ours is open, so opening and closing one's
        # own inventory is a purely client-side gesture that sends no packet.
        # The view therefore has to survive it: only /see stop may end it.
        await s.settle(1.0)
        assert s.bridge.call("/bots").get("views"), "什么都没做，观看会话却消失了"
        await s.bot.send_command("see stop")
        await s.settle(1.0)
        assert _protocol_items(s.bot) == {}, f"/see stop 没有还原背包：{_protocol_items(s.bot)}"
        await s.say("    chest 视角实时映射；inv 视角 1:1 且只有 /see stop 能退出")
    finally:
        try:
            await s.bot.send_command("see stop")
        except Exception:  # noqa: BLE001 - the connection may already be gone
            pass
        s.command(f"deop {s.username}")
        await watched.close()
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"


@case(27, "机器菜单", "附魔台：放剑 → 按键 → 取走被附魔的剑", timeout=180.0)
async def enchanting_table(s: Suite) -> None:
    """The one machine whose result is not a plain craft: the server rewrites the item."""

    spot = s.clear_spot(s.spot(2, 0))
    s.setblock(spot, "minecraft:enchanting_table")
    await s.wait_chunk(spot)
    s.clear_inventory()
    s.give("minecraft:diamond_sword", 1)
    s.give("minecraft:lapis_lazuli", 5)
    # An offer is only live when the player can pay its levels; the cheapest
    # rung (option 0) costs one level and one lapis.
    s.command(f"xp add {s.username} 30 levels")
    await s.settle(0.6)
    before = s.bot.player.experience_level
    window = await s.bot.menu.open_block(spot)
    assert window.identifier == "minecraft:enchantment", window.identifier
    enchanted = await window.enchant("minecraft:diamond_sword", option=0)
    assert enchanted.count == 1, f"附魔槽里的剑不见了：{enchanted}"
    assert enchanted.identifier == "minecraft:diamond_sword", enchanted.identifier
    await window.quick_move(window.layout.slot("item"))
    await s.settle(0.6)
    inventory = s.command(f"data get entity {s.username} Inventory")
    assert "diamond_sword" in inventory, f"服务端背包里没有剑：{inventory}"
    assert "enchantments" in inventory, f"剑没有被附魔：{inventory}"
    after = s.bot.player.experience_level
    assert after < before, f"等级没有扣：{before} → {after}"
    assert s.bot.player_menu.count("minecraft:lapis_lazuli") < 5, "青金石没有扣"
    await s.say(f"    附魔成功：等级 {before}→{after}，服务端背包里已经带 enchantments")
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"


@case(28, "新能力", "桶：水桶与岩浆桶倒在实心方块顶上", timeout=180.0)
async def bucket_liquids(s: Suite) -> None:
    """Buckets are poured with the *item use* packet, not the block click.

    ``BucketItem`` only overrides ``use`` (the right-click item path), so the
    block click a vanilla client sends first is declined by the block and the
    server does nothing; the client then follows it with ``ServerboundUseItem``
    in the same tick, and *that* is what pours (the server runs its own ray from
    the eye, so the aim has to be right before either packet).
    """

    floor = s.clear_spot(s.spot(2, 0))
    above = (floor[0], floor[1] + 1, floor[2])
    s.setblock(floor, "minecraft:stone")
    s.air(above)
    await s.wait_chunk(floor)
    await s.settle(0.4)
    s.clear_inventory()
    await s.settle(0.4)

    for item, liquid in (
        ("minecraft:water_bucket", "minecraft:water"),
        ("minecraft:lava_bucket", "minecraft:lava"),
    ):
        s.give(item, 1)
        await s.settle(0.5)
        await s.hold(item)
        await s.settle(0.4)
        selected = s.command(f"data get entity {s.username} SelectedItem")
        assert item.split(":")[-1] in selected, f"手上不是 {item}：{selected}"
        await s.bot.actions.turn_to(
            (floor[0] + 0.5, floor[1] + 1.0, floor[2] + 0.5)
        )
        # Vanilla order: the block click first (which the bucket declines), and
        # the item use right after it in the same tick.
        await s.bot.actions.use_item_on(
            floor, face=BlockFace.UP, cursor=(0.5, 1.0, 0.5), tick=False
        )
        await s.bot.actions.use_item()
        deadline = time.monotonic() + 6.0
        placed = ""
        while time.monotonic() < deadline:
            placed = s.bridge.block_type(s.world, above)
            if placed == liquid:
                break
            await s.settle(0.1)
        assert placed == liquid, (
            f"{liquid} 没有出现在 {above}：现在那里是 {placed or '未知'}"
            f"（手上的还是 {selected}）"
        )
        # The bucket is empty afterwards, which is what a vanilla client sees.
        emptied = await s.wait_server_count("minecraft:bucket", 1, timeout=4.0)
        assert emptied >= 1, f"倒完 {item} 之后背包里没有空桶"
        # Put the cell back to air for the next round; a flowing liquid would
        # otherwise keep replacing itself and hide the second pour.
        s.setblock(above, "minecraft:air")
        s.clear_inventory()
        await s.settle(0.5)

    # Scooping is the same packet pointed at the liquid: the bucket fills and
    # the liquid disappears, which is the other half of the vanilla behaviour.
    # The block click still names the *solid* block behind the liquid -- a
    # client's block pick skips fluids, and naming the liquid itself claims a
    # placement against air.
    s.setblock(above, "minecraft:water")
    await s.settle(0.5)
    s.give("minecraft:bucket", 1)
    await s.settle(0.4)
    await s.hold("minecraft:bucket")
    await s.settle(0.3)
    await s.bot.actions.turn_to(
        (above[0] + 0.5, above[1] + 0.6, above[2] + 0.5)
    )
    await s.bot.actions.use_item_on(
        floor, face=BlockFace.UP, cursor=(0.5, 1.0, 0.5), tick=False
    )
    await s.bot.actions.use_item()
    filled = await s.wait_server_count("minecraft:water_bucket", 1, timeout=6.0)
    assert filled >= 1, "空桶没有舀起水"
    scooped = ""
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        scooped = s.bridge.block_type(s.world, above)
        if scooped != "minecraft:water":
            break
        await s.settle(0.1)
    assert scooped != "minecraft:water", f"水没有被舀走：{above} 还是水"
    await s.say("    水桶/岩浆桶倒出、空桶回收、舀水都由服务端确认")
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"


@case(29, "新能力", "船：橡木船放进水里，服务端真的多了一条船", timeout=180.0)
async def boat_in_water(s: Suite) -> None:
    """A boat is placed by the item-use packet aimed at a water surface.

    ``BoatItem`` only overrides ``use``, and the server casts its own ray from
    the eye, so the aim has to sit on the water's top face before the packet
    goes out.  The proof is server side: a ``minecraft:oak_boat`` entity the
    server lists, and the boat gone from the hand.
    """

    centre = s.clear_spot(s.spot(3, 1))
    # The water sits in the feet-level cell, its floor one lower, and a stone
    # rim keeps it from running: the surface ends up level with the bot's feet.
    level = centre[1]
    pool: list[tuple[int, int, int]] = []
    for dx in range(-1, 2):
        for dz in (-1, 0, 1):
            water = (centre[0] + dx, level, centre[2] + dz)
            s.setblock((water[0], level - 1, water[2]), "minecraft:stone")
            s.setblock(water, "minecraft:water")
            s.air((water[0], level + 1, water[2]))
            pool.append(water)
    for dx in range(-2, 3):
        for dz in (-2, 2):
            s.setblock((centre[0] + dx, level, centre[2] + dz), "minecraft:stone")
    for dz in range(-1, 2):
        for dx in (-2, 2):
            s.setblock((centre[0] + dx, level, centre[2] + dz), "minecraft:stone")
    await s.wait_chunk(pool[4])
    await s.settle(0.6)
    s.clear_inventory()
    s.give("minecraft:oak_boat", 1)
    await s.settle(0.6)
    await s.hold("minecraft:oak_boat")
    await s.settle(0.3)
    held = s.command(f"data get entity {s.username} SelectedItem")
    assert "oak_boat" in held, f"手上不是船：{held}"

    surface = (float(centre[0]) + 0.5, float(level + 1), float(centre[2]) + 0.5)
    await s.bot.actions.turn_to(surface)
    await s.settle(0.3)
    hit = s.bot.actions.raycast()
    assert hit is not None, "眼睛没有指向水面"
    await s.bot.actions.use_item()

    around = {
        "x": float(centre[0]) + 0.5,
        "y": float(level) + 0.5,
        "z": float(centre[2]) + 0.5,
        "radius": 6.0,
    }
    boats: list[dict[str, Any]] = []
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        boats = s.bridge.entities_of(s.world, "boat", **around)
        if boats:
            break
        await s.settle(0.1)
    assert boats, (
        f"服务端没有出现船（瞄准点 {surface}，脚下 {s.bot.physics_state.position}，"
        f"射线 {hit}，附近实体 "
        f"{[entity.get('type') for entity in s.bridge.entities(s.world, **around)]}）"
    )
    boat = boats[0]
    assert level <= float(boat.get("y") or 0) <= level + 2.0, (
        f"船没有落在水面上：y={boat.get('y')}，水面 y={level + 1}"
    )
    assert s.server_count("minecraft:oak_boat") == 0, "船放下之后手上还留着一只"
    await s.say(f"    船落在 {boat.get('type')} y={float(boat.get('y') or 0):.2f}（服务端列表）")
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"


@case(30, "新能力", "矿车：铁轨上放矿车，服务端多一辆车", timeout=180.0)
async def minecart_on_rail(s: Suite) -> None:
    """A minecart is the block click: ``MinecartItem.useOn`` places the entity."""

    rail = s.clear_spot(s.spot(2, 0))
    s.command(f"setblock {rail[0]} {rail[1]} {rail[2]} minecraft:rail")
    s.air((rail[0], rail[1] + 1, rail[2]))
    await s.wait_chunk(rail)
    await s.settle(0.5)
    s.clear_inventory()
    s.give("minecraft:minecart", 1)
    await s.settle(0.5)
    await s.hold("minecraft:minecart")
    await s.settle(0.3)
    await s.bot.actions.turn_to((rail[0] + 0.5, float(rail[1]) + 1.0, rail[2] + 0.5))
    await s.bot.actions.use_item_on(
        rail, face=BlockFace.UP, cursor=(0.5, 1.0, 0.5)
    )
    around = {
        "x": rail[0] + 0.5,
        "y": rail[1] + 1.0,
        "z": rail[2] + 0.5,
        "radius": 4.0,
    }
    carts: list[dict[str, Any]] = []
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        carts = s.bridge.entities_of(s.world, "minecart", **around)
        if carts:
            break
        await s.settle(0.1)
    assert carts, (
        f"铁轨上没有出现矿车（附近实体 "
        f"{[entity.get('type') for entity in s.bridge.entities(s.world, **around)]}）"
    )
    assert s.server_count("minecraft:minecart") == 0, "矿车放下之后手上还留着一辆"
    await s.say(f"    矿车出现在 {rail} 的铁轨上（服务端列表）")
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"


@case(31, "新能力", "睡觉：夜里点床，服务端把夜晚跳过", timeout=180.0)
async def sleep_in_bed(s: Suite) -> None:
    """Lying down is a block click with an empty hand, and the night skip proves it.

    The click has to be worth something on the server: with one player online the
    default ``playersSleepingPercentage`` is met, so a real sleep rolls the world
    clock over to morning.  An empty hand matters because 26.3 routes the click
    through ``useWithoutItem`` only when there is nothing to place.
    """

    base = s.clear_spot(s.spot(2, 2))
    # A bed is two blocks standing in the feet-level cells: foot at the spot,
    # head one to the south.  Both need a floor under them, and neither may sit
    # under the player -- sleeping in a bed you are standing inside is refused.
    foot = base
    head = (base[0], base[1], base[2] + 1)
    s.setblock((foot[0], foot[1] - 1, foot[2]), "minecraft:stone")
    s.setblock((head[0], head[1] - 1, head[2]), "minecraft:stone")
    s.command(
        f"setblock {foot[0]} {foot[1]} {foot[2]} "
        "minecraft:red_bed[part=foot,facing=south]"
    )
    s.command(
        f"setblock {head[0]} {head[1]} {head[2]} "
        "minecraft:red_bed[part=head,facing=south]"
    )
    s.air((foot[0], foot[1] + 1, foot[2]), (head[0], head[1] + 1, head[2]))
    await s.wait_chunk(foot)
    if s.bridge.block_type(s.world, foot) != "minecraft:red_bed":
        raise AssertionError(
            f"床没有放下：{foot} 是 {s.bridge.block_type(s.world, foot)}，"
            f"床头是 {s.bridge.block_type(s.world, head)}"
        )
    s.command("time set 18000")
    s.command("weather clear")
    await s.settle(0.6)
    s.clear_inventory()
    await s.settle(0.4)
    night = s.bridge.world_time(s.world)
    assert night is not None and night > 12000, f"没有到夜里：time={night}"

    await s.bot.actions.turn_to((foot[0] + 0.5, float(foot[1]) + 0.6, foot[2] + 0.5))
    await s.settle(0.2)
    hit = s.bot.actions.raycast()
    await s.bot.actions.use_item_on(foot, face=BlockFace.UP, cursor=(0.5, 1.0, 0.5))
    # From here the bot is in bed.  A vanilla client stops sending movement
    # while it sleeps, and the server has already moved the body into the bed:
    # ticking from the old spot would claim movement that never happened.
    s.ticks_paused = True
    morning = None
    asleep = deep = False
    deadline = time.monotonic() + 20.0
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            if not deep:
                # "The bed accepted me" and "I slept long enough for the night
                # to count" are two different vanilla facts: ``sleeping_pos``
                # appears when the bed is entered, ``SleepTimer`` only caps at
                # 100 after five seconds of staying in it.
                nbt = s.command(f"data get entity {s.username}")
                asleep = asleep or "sleeping_pos" in nbt
                deep = deep or re.search(r"SleepTimer:\s*100s", nbt) is not None
            current = s.bridge.world_time(s.world)
            if current is not None and current < 1000:
                morning = current
                break
    finally:
        s.ticks_paused = False
        await s.settle(0.6)
    woke = (
        (round(s.bot.physics_state.position.x, 2), round(s.bot.physics_state.position.y, 2), round(s.bot.physics_state.position.z, 2)),
        s.server_position(),
    )
    assert asleep, (
        f"床没有接受这次点击：脚 {s.bot.physics_state.position}，准星 {hit}，"
        f"床 {foot}={s.bridge.block_type(s.world, foot)}"
    )
    # A skip is the strongest proof of the same fact: the server only rolls the
    # clock over once a sleeper has sat in the bed past the vanilla threshold,
    # and it wakes the player on the same tick -- so SleepTimer may never be
    # caught at 100 by a poll that runs every 0.3 s.
    assert deep or morning is not None, (
        f"人躺下了但没睡够五个游戏秒：脚 {s.bot.physics_state.position}，"
        f"准星 {hit}，床 {foot}={s.bridge.block_type(s.world, foot)}，"
        f"SleepTimer 没到 100（time={s.bridge.world_time(s.world)}）"
    )
    # The clock only rolls over once every active player is asleep, so with an
    # operator connected the night legitimately stays dark.  What belongs to
    # this bot is that the bed accepted it and that it slept past the vanilla
    # threshold; the rollover is asserted whenever the bot is alone.
    others = [p for p in s.bridge.players() if p.get("name") != s.username]
    if others:
        await s.say(
            f"    线上还有 {len(others)} 名玩家，原版不会跳过夜晚；"
            "已确认入睡且 SleepTimer=100"
        )
    else:
        assert morning is not None, (
            f"只有我一个人在线，夜晚却没被跳过：床 {foot}，"
            f"time 一直停在 {s.bridge.world_time(s.world)}"
        )
        await s.say(f"    夜里点床 → 服务端把时间跳到 {morning}")
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}（醒来时我们 {woke[0]}，服务端 {woke[1]}）"
    assert not bad, f"反作弊日志：{bad[:2]}"


@case(32, "新能力", "繁殖：两头牛各喂小麦，生出小牛", timeout=180.0)
async def breed_cows(s: Suite) -> None:
    """Feeding two cows makes a third one, and the server counts all three.

    The cows keep their brains: the mating goal needs a tick loop, so ``NoAI``
    animals never breed however many wheat they eat.
    """

    base = s.clear_spot(s.spot(2, 0))
    spots = [(base[0], base[1], base[2]), (base[0] + 1, base[1], base[2])]
    s.command("kill @e[type=minecraft:cow]")
    for spot in spots:
        s.air(spot)
    await s.settle(0.3)
    for spot in spots:
        s.command(
            f"summon minecraft:cow {spot[0] + 0.5} {spot[1]} {spot[2] + 0.5} "
            '{Tags:["breed"],PersistenceRequired:1b,Silent:1b,Invulnerable:1b}'
        )
    await s.settle(1.0)
    around = {
        "x": float(base[0]) + 0.5,
        "y": float(base[1]),
        "z": float(base[2]) + 0.5,
        "radius": 12.0,
    }
    before = s.bridge.entities_of(s.world, "cow", **around)
    assert len(before) >= 2, f"两头牛没召出来：{before}"

    s.clear_inventory()
    s.give("minecraft:wheat", 8)
    await s.settle(0.6)
    await s.hold("minecraft:wheat")
    await s.settle(0.3)
    wheat_before = s.server_count("minecraft:wheat")
    cow_type = s.bot.version.entities.entity_id("minecraft:cow")
    targets = [
        entity_id
        for entity_id, entity in s.bot.entities.items()
        if entity.type_id == cow_type
    ]
    fed = 0
    for entity_id in targets:
        entity = s.bot.entities[entity_id]
        await s.bot.actions.turn_to((entity.x, entity.y + 0.7, entity.z))
        await s.settle(0.2)
        await s.bot.actions.interact(entity_id)
        fed += 1
        if fed >= 2:
            break
        await s.settle(0.5)
    assert fed >= 2, f"只点到 {fed} 头牛"
    await s.settle(0.5)
    wheat_after = s.server_count("minecraft:wheat")
    assert wheat_after == wheat_before - 2, (
        f"小麦没有各被吃一个：{wheat_before} → {wheat_after}"
    )

    grown: list[dict[str, Any]] = []
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        await s.settle(1.0)
        grown = s.bridge.entities_of(s.world, "cow", **around)
        if len(grown) > len(before):
            break
    assert len(grown) > len(before), (
        f"没有生小牛：喂食前 {len(before)} 头，现在 {len(grown)} 头"
    )
    await s.say(f"    两头牛各吃一个小麦，服务端从 {len(before)} 头变成 {len(grown)} 头")
    s.command("kill @e[type=minecraft:cow]")
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
async def main_async(args: argparse.Namespace) -> int:
    bridge = Bridge(args.bridge)
    status = bridge.call("/status")
    print(
        f"[bridge] {status.get('serverVersion')} tps={status.get('tps')} "
        f"players={len(bridge.players())}",
        flush=True,
    )
    bot = await connect(
        args.host,
        port=args.port,
        username=args.username,
        version=args.version,
        timeout=30.0,
    )
    suite = Suite(
        bot,
        bridge,
        world=args.world,
        anchor=(args.x, args.y, args.z),
        server_log=args.server_log,
    )
    bot.events.on("player_chat", suite.on_chat)
    bot.events.on("packet", suite.journal_packet)
    worker = asyncio.create_task(suite.worker())
    idle = asyncio.create_task(suite.keep_ticking())

    try:
        await asyncio.sleep(1.0)
        bridge.command(f"gamemode survival {args.username}")
        # Announce the client to the bridge plugin's spectator registry, so an
        # operator can watch this bot's inventory with /see while a scenario
        # runs. Servers without the plugin answer "unknown command" and nothing
        # else happens.
        try:
            await bot.register_as_bot()
        except Exception as error:  # noqa: BLE001 - best effort, never fail a run
            print(f"[suite] could not register as a bot: {error}", flush=True)
        await suite.teleport((args.x + 0.5, float(args.y), args.z + 0.5))
        await suite.say_many(menu_lines())
        if args.run:
            wanted: list[Case] = []
            if args.run.strip().lower() == "all":
                wanted = list(CASES)
            else:
                for chunk in args.run.split(","):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    item = by_number(int(chunk))
                    if item is None:
                        print(f"[suite] no case {chunk}", flush=True)
                        return 2
                    wanted.append(item)
            failures = 0
            for item in wanted:
                if bot.closed.is_set():
                    print("[suite] the connection closed, stopping here", flush=True)
                    failures += 1
                    break
                ok = await suite.run_case(item)
                if not ok:
                    failures += 1
            print("", flush=True)
            print("[suite] results", flush=True)
            for item, passed, seconds, detail in suite.results:
                mark = "✓" if passed else "✗"
                line = (
                    f"  {mark} #{item.number:>2} {seconds:6.1f}s"
                    f"  {item.category}·{item.title}"
                )
                if detail:
                    line += f"  ({detail})"
                print(line, flush=True)
            total = sum(row[2] for row in suite.results)
            print(f"[suite] {len(wanted) - failures}/{len(wanted)} passed"
                  f" in {total:.1f}s", flush=True)
            alerts = suite.anti_cheat_lines()
            if alerts:
                print(f"[suite] anti-cheat alerts: {len(alerts)}", flush=True)
                for line in alerts[:10]:
                    print(f"    {line.strip()}", flush=True)
            else:
                print("[suite] no anti-cheat alerts", flush=True)
            return 0 if failures == 0 else 1
        print("[suite] ready: type a number in game chat", flush=True)
        while not bot.closed.is_set():
            await asyncio.sleep(0.5)
    finally:
        worker.cancel()
        idle.cancel()
        try:
            await bot.close()
        except Exception:
            pass
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="SuiteBot")
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    parser.add_argument("--world", default="world")
    parser.add_argument(
        "--server-log",
        default=None,
        help="console log to scan for anti-cheat alerts (default: the bridge's copy)",
    )
    parser.add_argument("--x", type=int, default=0)
    parser.add_argument("--y", type=int, default=-60)
    parser.add_argument("--z", type=int, default=0)
    parser.add_argument("--list", action="store_true", help="print the menu and exit")
    parser.add_argument(
        "--run",
        default="",
        help="run these cases without chat (comma separated, or 'all') and exit",
    )
    args = parser.parse_args()
    if args.list:
        for line in menu_lines():
            print(line)
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())







