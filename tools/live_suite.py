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
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from protobot import connect
from protobot.actions import ActionStatus, BlockFace
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

    def events(self, *, since: int = 0, limit: int = 400) -> list[dict[str, Any]]:
        payload = self.call("/events", query={"since": since, "limit": limit})
        entries = payload.get("entries") or payload.get("events")
        return entries if isinstance(entries, list) else []

    def clear_events(self) -> None:
        self.call("/events/clear", body={})

    def log(self, *, since: int = 0, limit: int = 400) -> list[str]:
        payload = self.call("/log", query={"since": since, "limit": limit})
        lines = payload.get("lines") or payload.get("log") or []
        if isinstance(lines, str):
            return lines.splitlines()
        return [str(line) for line in lines]

    def inventory(self, name: str) -> dict[str, Any]:
        payload = self.call("/inventory", query={"name": name})
        return payload


ANTI_CHEAT = ("moved too quickly", "moved wrongly", "Flying is not enabled")


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
        span = f"{items[0].number}-{items[-1].number}"
        parts.append(f"{span} {category}")
    lines = ["ProtoBot 验收套件 23 项：直接发数字 1-23 跑单项，0=菜单，all=全跑，stop=中止，?N=说明"]
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
    ) -> None:
        self.bot = bot
        self.bridge = bridge
        self.world = world
        self.anchor = anchor
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.busy = False
        self.stop_requested = False
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
        await self.settle(0.5)

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
        await self.teleport(
            (self.anchor[0] + 0.5, float(self.anchor[1]), self.anchor[2] + 0.5)
        )

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

    def anti_cheat_lines(self) -> list[str]:
        return [
            line
            for line in self.bridge.log(limit=800)
            if any(marker in line for marker in ANTI_CHEAT)
            and self.username in line
        ]

    # -- dispatch -----------------------------------------------------
    async def kill(
        self, entity_id: int, *, max_hits: int = 30, timeout: float = 45.0
    ) -> int:
        """Hit a tracked entity until it is gone, walking after it when it escapes.

        The server only accepts a hit while the entity is inside the three block
        attack range, and a cow is knocked back by every hit, so a plain swing
        loop misses once it drifts away.
        """

        hits = 0
        deadline = time.monotonic() + timeout
        for _ in range(max_hits):
            if time.monotonic() > deadline:
                break
            entity = self.bot.entities.get(entity_id)
            if entity is None:
                break
            near = math.dist((entity.x, entity.y, entity.z), self.feet()) <= 2.8
            hit = self.bot.actions.raycast_entity(max_distance=3.0)
            if (hit is None or hit.entity_id != entity_id) and not near:
                try:
                    await self.bot.navigate_to(
                        entity.x, entity.z, tolerance=1.6, timeout=3.0
                    )
                except Exception:
                    pass
                entity = self.bot.entities.get(entity_id)
                if entity is None:
                    break
                near = math.dist((entity.x, entity.y, entity.z), self.feet()) <= 2.8
                hit = self.bot.actions.raycast_entity(max_distance=3.0)
            try:
                await self.bot.actions.look_at_entity(entity_id)
            except Exception:
                break
            await self.settle(0.1)
            if hit is not None and hit.entity_id == entity_id:
                result = await self.bot.actions.attack_looked_at(max_distance=3.0)
            elif near:
                # The server checks the distance, not the crosshair, so a nearby
                # mob is hittable even when the ray skims past its box.
                result = await self.bot.actions.attack(entity_id, swing=True)
            else:
                await self.settle(0.2)
                continue
            if result is not None:
                hits += 1
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
        try:
            await self.reset()
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
    await s.say(f"    服务端 break {events[-1].get('block')} @ {target}")


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
        menu = await s.bot.menu.open_block(target)
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
    assert moved_in == 1 and moved_fuel == 1, (
        f"投料 {moved_in}/{moved_fuel}，槽位 in={input_slot} fuel={fuel_slot} "
        f"in={menu.item(input_slot)} fuel={menu.item(fuel_slot)} "
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
@case(15, "战斗", "打牛到死")
async def kill_cow(s: Suite) -> None:
    spot = (s.anchor[0] + 2.5, float(s.anchor[1]), s.anchor[2] + 0.5)
    entity_id = await s.summon("minecraft:cow", spot, no_ai=True)
    await s.bot.actions.look_at_entity(entity_id)
    health_before = s.entity_health("minecraft:cow")
    assert health_before and health_before > 0, f"服务端血量 {health_before}"
    swings = await s.kill(entity_id)
    await s.settle(0.5)
    gone = s.entity_health("minecraft:cow") is None
    assert gone, "牛还活着"
    assert not any(eid == entity_id for eid in s.bot.entities), "客户端还留着实体"
    bad = s.anti_cheat_lines()
    assert not bad, f"反作弊日志：{bad[:2]}"
    await s.say(f"    服务端血量 {health_before:.0f} → 消失，{swings} 个攻击包")


@case(16, "战斗", "击中最近的目标")
async def pick_nearest(s: Suite) -> None:
    near = (s.anchor[0] + 2.2, float(s.anchor[1]), s.anchor[2] + 0.5)
    far = (s.anchor[0] + 2.2, float(s.anchor[1]), s.anchor[2] + 2.6)
    kill_before = s.command(f"kill @e[tag={TAG}]")
    await s.settle(0.3)
    before = set(s.bot.entities)
    s.command(f'summon minecraft:cow {far[0]:.2f} {far[1]:.2f} {far[2]:.2f} {{Tags:["{TAG}"],PersistenceRequired:1b}}')
    far_id = await s.wait_entity("minecraft:cow", far, before=before)
    before.add(far_id)
    s.command(f'summon minecraft:cow {near[0]:.2f} {near[1]:.2f} {near[2]:.2f} {{Tags:["{TAG}"],PersistenceRequired:1b}}')
    near_id = await s.wait_entity("minecraft:cow", near, before=before)
    center = s.bot.actions.entity_center(near_id)
    assert center is not None, "最近的牛没有被跟踪"
    await s.bot.actions.look_at(center)
    await s.settle(0.2)
    hit = s.bot.actions.raycast_entity()
    assert hit is not None, "射线里没有实体"
    assert hit.entity_id == near_id, f"选中 {hit.entity_id}，最近的是 {near_id}"
    await s.say(f"    射线选中 {hit.entity_id}（近），忽略 {far_id}，距离 {hit.distance:.2f}")


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
        result = await s.bot.actions.dig(spot, hold=0.5)
        release = started + 0.5
        for _ in range(60):
            if updates:
                break
            await s.settle(0.05)
        assert result.status is ActionStatus.CONFIRMED or updates, "方块没破"
        assert s.server_state(spot) == 0, "服务端方块还在"
        return (updates[0] - release) if updates else 0.0

    iron_delay = await timed(iron_spot, "minecraft:iron_pickaxe")
    wood_delay = await timed(wood_spot, "minecraft:wooden_pickaxe")
    assert iron_delay < 0.3, f"铁镐松手后还等了 {iron_delay:.2f}s"
    assert wood_delay > 0.3, f"木镐松手后只等了 {wood_delay:.2f}s"
    await s.say(
        f"    铁镐破石延迟 {iron_delay:.2f}s，木镐 {wood_delay:.2f}s（服务端补完）"
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
    # A three block log column a couple of blocks away, plus the walk that reaches it.
    column = [s.clear_spot(s.spot(3, 2, dy)) for dy in (0, 1, 2)]
    for position in column:
        s.setblock(position, "minecraft:oak_log")
    await s.settle(0.4)
    start = s.feet()
    walked = await s.bot.goals.run(
        TravelGoal(start[0] + 2.5, start[2] + 0.5, tolerance=1.0, timeout=90.0), timeout=120.0
    )
    assert walked.status is GoalStatus.DONE, f"走过去 {walked.detail}"
    for position in column:
        result = await s.bot.actions.dig(position, hold=3.4)
        assert result.status is ActionStatus.CONFIRMED, f"{position} 没挖掉：{result.evidence}"
    # Items only fly into the inventory within about a block, so step next to
    # the column (not into it: those cells are still solid above) and wait for
    # every drop to fall into reach.
    collect = (column[0][0] + 1.6, column[0][2] + 0.6)
    logs = 0
    for _ in range(3):
        try:
            await s.bot.navigate_to(
                collect[0], collect[1], tolerance=0.6, timeout=15.0
            )
        except Exception:
            pass
        logs = await s.wait_server_count("minecraft:oak_log", 3, timeout=4.0)
        if logs >= 3:
            break
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
    # Pinned in place: the scenario is about walking out, killing and walking
    # back, and a wandering target turns "did the kill land" into a chase.
    entity_id = await s.summon("minecraft:cow", spot, no_ai=True)
    await s.bot.actions.look_at_entity(entity_id)
    kills = await s.kill(entity_id)
    if s.entity_health("minecraft:cow") is not None:
        kills += await s.kill(entity_id)
    assert s.entity_health("minecraft:cow") is None, f"远处的牛没死（{kills} 次命中）"
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
    suite = Suite(bot, bridge, world=args.world, anchor=(args.x, args.y, args.z))
    bot.events.on("player_chat", suite.on_chat)
    bot.events.on("packet", suite.journal_packet)
    worker = asyncio.create_task(suite.worker())

    try:
        await asyncio.sleep(1.0)
        bridge.command(f"gamemode survival {args.username}")
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
            print(f"[suite] {len(wanted) - failures}/{len(wanted)} passed", flush=True)
            return 0 if failures == 0 else 1
        print("[suite] ready: type a number in game chat", flush=True)
        while not bot.closed.is_set():
            await asyncio.sleep(0.5)
    finally:
        worker.cancel()
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
