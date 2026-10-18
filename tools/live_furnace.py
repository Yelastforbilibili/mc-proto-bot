"""Live check: smelt something in a furnace and prove the server did it.

The furnace is the first menu with *progress*: its window carries four
``container_set_data`` values (lit time, lit total, cook progress, cook total)
next to the item slots.  A container layer that only understands slots can
deposit and withdraw, but cannot tell a running furnace from an idle one.

The tool drives the whole vanilla sequence:

1. place a furnace and give the bot cobblestone and coal;
2. deposit one coal into the fuel slot and cobblestone into the input slot;
3. watch the data slots -- ``lit_time`` must become positive and
   ``cook_progress`` must climb towards ``cook_total``;
4. wait for the output slot to hold ``minecraft:stone``;
5. take the stone out and ask the server what the furnace holds.

Coal burns for 1600 ticks and cobblestone takes 200 ticks to smelt, so the run
takes about ten seconds; that is real smelting, not a shortcut.

Usage::

    uv run python tools/live_furnace.py --version 26.3

Prints a JSON report with the observed timers and exits non-zero unless the
server agrees a stone exists.
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
    data = payload.get("data")
    if isinstance(data, dict):
        merged = dict(payload)
        merged.update(data)
        return merged
    return payload


def _command(base: str, command: str) -> dict[str, Any]:
    return _unwrap(_bridge(base, "/command", body={"command": command}))


def _nbt(base: str, command: str) -> str:
    payload = _command(base, command)
    output = payload.get("output") or []
    if isinstance(output, str):
        output = [output]
    return " ".join(str(line) for line in output)


def _setblock(base: str, world: str, target: tuple[int, int, int], data: str) -> int | None:
    payload = _unwrap(
        _bridge(
            base,
            "/setblock",
            query={"world": world, "x": target[0], "y": target[1], "z": target[2]},
            body={"data": data},
        )
    )
    return (payload.get("block") or {}).get("stateId")


async def _wait_state(bot: Any, target: tuple[int, int, int], expected: int | None, *, timeout: float = 2.0) -> int:
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
        await asyncio.sleep(0.02)
    return state


async def _wait_inventory(bot: Any, item: str, count: int, *, timeout: float = 8.0) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    total = 0
    while loop.time() < deadline:
        total = sum(
            bot.get_inventory_item(slot).count
            for slot in range(46)
            if bot.get_inventory_item(slot).identifier == item
        )
        if total >= count:
            return total
        await asyncio.sleep(0.05)
    return total


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
    parser.add_argument("--username", default="FurnaceProbe")
    parser.add_argument("--world", default="world")
    parser.add_argument("--input", default="minecraft:cobblestone")
    parser.add_argument("--fuel", default="minecraft:coal")
    parser.add_argument("--output", default="minecraft:stone")
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    parser.add_argument("--smelt-timeout", type=float, default=25.0)
    args = parser.parse_args()

    report: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "version": args.version,
        "input": args.input,
        "fuel": args.fuel,
        "output": args.output,
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
        spot = (math.floor(position.x) + 2, math.floor(position.y), math.floor(position.z))
        report["spot"] = list(spot)
        _setblock(args.bridge, args.world, spot, "minecraft:air")
        report["furnace_state"] = await _wait_state(
            bot, spot, _setblock(args.bridge, args.world, spot, "minecraft:furnace")
        )

        _command(args.bridge, f"clear {args.username}")
        for item, count in ((args.input, 3), (args.fuel, 3)):
            _command(args.bridge, f"give {args.username} {item} {count}")
            seen = await _wait_inventory(bot, item, count)
            report[f"gave_{item.split(':')[-1]}"] = seen
            if seen < count:
                report["ok"] = False
                report["error"] = f"the bot never saw {count} {item}"
                return 1

        if _distance(bot, spot) > 4.0:
            await bot.navigate_to(spot[0] + 2.5, spot[2] + 0.5, timeout=60.0)
        report["distance"] = round(_distance(bot, spot), 3)

        menu = await bot.menu.open_block(spot)
        report["menu"] = {
            "identifier": menu.identifier,
            "container": list(menu.slots("container")),
            "result": list(menu.slots("result")),
        }
        input_slot = menu.slots("container")[0]
        fuel_slot = menu.slots("container")[1]
        output_slot = menu.slot("result")

        fuel_source = menu.find(args.fuel, where="player")
        input_source = menu.find(args.input, where="player")
        report["source_slots"] = {"fuel": fuel_source, "input": input_source}
        if fuel_source is None or input_source is None:
            report["ok"] = False
            report["error"] = "the fuel or the input never reached the inventory"
            return 1
        report["fuel_moved"] = await menu.move(fuel_source, fuel_slot)
        report["input_moved"] = await menu.move(input_source, input_slot)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + args.smelt_timeout
        lit = 0
        progress = 0
        timers: list[dict[str, int]] = []
        while loop.time() < deadline:
            lit = menu.data_value("lit_time")
            progress = menu.data_value("cook_progress")
            timers.append({"lit_time": lit, "cook_progress": progress})
            if not menu.item(output_slot).empty:
                break
            await asyncio.sleep(0.5)
        report["timers"] = timers[:12]
        report["lit_time"] = lit
        report["cook_progress"] = progress
        report["cook_total"] = menu.data_value("cook_total")
        report["output_slot"] = menu.item(output_slot).identifier
        report["server_before"] = _nbt(
            args.bridge, f"data get block {spot[0]} {spot[1]} {spot[2]} Items"
        )

        taken = 0
        if not menu.item(output_slot).empty:
            taken = await menu.move(output_slot, menu.free(where="player"))
        report["taken"] = taken
        report["player_view"] = menu.count(args.output, where="player")
        report["server_after"] = _nbt(
            args.bridge, f"data get block {spot[0]} {spot[1]} {spot[2]} Items"
        )
        report["server_burn"] = _nbt(
            args.bridge, f"data get block {spot[0]} {spot[1]} {spot[2]} BurnTime"
        )
        with contextlib.suppress(Exception):
            await menu.close()

        report["ok"] = (
            lit > 0
            and taken >= 1
            and report["player_view"] >= 1
            and args.output in report["server_before"] + report["server_after"]
        )
    except BaseException as error:  # noqa: BLE001 - the probe reports, never raises
        report["ok"] = False
        report["exception"] = f"{type(error).__name__}: {error}"
    finally:
        report["closed"] = bot.closed.is_set()
        report["events"] = events[-8:]
        with contextlib.suppress(Exception):
            await bot.close()
        print(json.dumps(report, indent=2, default=str), flush=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
