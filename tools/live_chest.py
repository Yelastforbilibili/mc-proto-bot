"""Live check: move items into and out of a chest, proven by the server.

``live_dig.py`` and ``live_place.py`` prove the two block actions.  This one
proves the container layer end to end against a real Paper server:

1. the bridge puts a chest next to the bot and gives it some dirt;
2. the bot right-clicks the chest and the server opens a 9x3 window;
3. the layer moves the dirt into the chest with plain clicks;
4. the tool asks the *server* -- ``data get block ... Items`` -- what the chest
   now holds, because the client window alone proves nothing;
5. the layer takes the dirt back out and the server is asked again.

Every step is the sequence a player performs, with no server-side shortcuts, so
a passing run means the click protocol, the state-id bookkeeping and the slot
layouts all agree with vanilla.

Usage::

    uv run python tools/live_chest.py --version 26.3 --count 5

The chest is placed two blocks east of wherever the bot logs in, lifted to the
bot's own y level so it is always within reach.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import sys
import urllib.parse
import urllib.request
from typing import Any

from protobot import connect


def _bridge(
    base: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call the prototest bridge: a body means POST, no body means GET."""

    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """The bridge answers bare; the DSH plugin nests the same body under ``data``."""

    data = payload.get("data")
    if isinstance(data, dict):
        merged = dict(payload)
        merged.update(data)
        return merged
    return payload


def _command(base: str, command: str) -> dict[str, Any]:
    return _unwrap(_bridge(base, "/command", body={"command": command}))


def _nbt(base: str, command: str) -> str:
    """Run a ``data get`` command and return its joined output."""

    payload = _command(base, command)
    output = payload.get("output") or []
    if isinstance(output, str):
        output = [output]
    return " ".join(str(line) for line in output)


def _place_chest(base: str, world: str, target: tuple[int, int, int]) -> int | None:
    # Clear the voxel first: replacing a chest with a chest keeps its block
    # entity, so a rerun would inherit whatever the previous run stored.
    for data in ("minecraft:air", "minecraft:chest"):
        payload = _unwrap(
            _bridge(
                base,
                "/setblock",
                query={"world": world, "x": target[0], "y": target[1], "z": target[2]},
                body={"data": data},
            )
        )
    block = payload.get("block") or {}
    return block.get("stateId")


async def _wait_inventory(bot: Any, item: str, count: int, *, timeout: float = 8.0) -> int:
    """Wait until the bot's own inventory holds ``count`` of ``item``."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    total = 0
    while loop.time() < deadline:
        total = 0
        for slot in range(0, 46):
            stack = bot.get_inventory_item(slot)
            if stack.identifier == item:
                total += stack.count
        if total >= count:
            return total
        await asyncio.sleep(0.05)
    return total


async def _wait_state(bot: Any, target: tuple[int, int, int], expected: int | None, *, timeout: float = 3.0) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    state = bot.world.get_state_id(*target)
    while loop.time() < deadline:
        state = bot.world.get_state_id(*target)
        if expected is None:
            if state not in (0, -1):
                return state
        elif state == expected:
            return state
        await asyncio.sleep(0.05)
    return state


def _distance(bot: Any, target: tuple[int, int, int]) -> float:
    eye = bot.physics_state.position
    return math.dist(
        (eye.x, eye.y + 1.62, eye.z),
        (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="ChestProbe")
    parser.add_argument("--world", default="world")
    parser.add_argument("--item", default="minecraft:dirt")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "version": args.version,
        "item": args.item,
        "count": args.count,
    }
    events: list[str] = []

    bot = await connect(
        args.host,
        port=args.port,
        username=args.username,
        version=args.version,
        timeout=20.0,
    )
    bot.events.on("error", lambda error: events.append(f"error:{type(error).__name__}:{error}"))
    bot.events.on("close", lambda reason: events.append(f"close:{reason}"))
    try:
        await bot.wait_world(timeout=20.0)
        position = bot.physics_state.position
        chest = (math.floor(position.x) + 2, math.floor(position.y), math.floor(position.z))
        report["spawn"] = [round(position.x, 3), round(position.y, 3), round(position.z, 3)]
        report["chest"] = list(chest)

        report["chest_state"] = await _wait_state(bot, chest, _place_chest(args.bridge, args.world, chest))

        given = _command(args.bridge, f"give {args.username} {args.item} {args.count}")
        report["give"] = given.get("output")
        holding = await _wait_inventory(bot, args.item, args.count)
        report["inventory_before"] = holding
        if holding < args.count:
            report["ok"] = False
            report["error"] = f"the bot only ever saw {holding} {args.item} in its inventory"
            return 1

        if _distance(bot, chest) > 4.0:
            stand = (chest[0] + 2.5, chest[2] + 0.5)
            await bot.navigate_to(stand[0], stand[1], timeout=60.0)
        report["distance"] = round(_distance(bot, chest), 3)

        menu = await bot.menu.open_block(chest)
        report["menu"] = {
            "identifier": menu.identifier,
            "slot_count": menu.slot_count,
            "container_slots": len(menu.layout.container),
            "player_slots": len(menu.layout.player),
            "derived": menu.layout.derived,
        }

        moved = await menu.deposit(args.item, args.count)
        report["deposited"] = moved
        report["chest_view"] = menu.count(args.item, where="container")
        report["chest_nbt"] = _nbt(args.bridge, f"data get block {chest[0]} {chest[1]} {chest[2]} Items")

        taken = await menu.withdraw(args.item, moved)
        report["withdrawn"] = taken
        report["player_view"] = menu.count(args.item, where="player")
        report["chest_nbt_after"] = _nbt(args.bridge, f"data get block {chest[0]} {chest[1]} {chest[2]} Items")
        report["player_nbt"] = _nbt(args.bridge, f"data get entity {args.username} Inventory")

        with contextlib.suppress(Exception):
            await menu.close()

        stored = args.count if f"count: {args.count}" in report["chest_nbt"] else 0
        report["ok"] = (
            moved == args.count
            and stored == args.count
            and "minecraft:dirt" not in report["chest_nbt_after"]
            and taken == moved
            and report["player_view"] >= moved
        )
    except BaseException as error:  # noqa: BLE001 - the probe reports, never raises
        report["ok"] = False
        report["exception"] = f"{type(error).__name__}: {error}"
    finally:
        report["closed"] = bot.closed.is_set()
        terminal = getattr(bot, "_terminal_error", None)
        if terminal is not None:
            report["terminal_error"] = f"{type(terminal).__name__}: {terminal}"
        report["events"] = events[-8:]
        with contextlib.suppress(Exception):
            await bot.close()
        print(json.dumps(report, indent=2, default=str), flush=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
