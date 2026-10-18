"""Live check: open every container block and confirm its real menu layout.

The container layer ships a slot layout for every menu in the registry
(``protobot/containers/layouts.py``).  A layout cannot be trusted just because it
was typed out: the server picks the menu type, and a wrong slot count silently
mis-addresses every click.  So this tool asks the server for all of them:

for each block name, clear the voxel, place the block, right-click it, and record

* whether a window opened at all,
* the ``minecraft:menu`` registry id the server sent,
* the slot count from ``container_set_content``,
* and whether that pair is a *known* layout with a matching size.

A passing run means the layout table agrees with the server for every menu a
block can open.  ``minecraft:merchant`` is entity-only and covered by
``Menu.open_entity`` instead.

Usage::

    uv run python tools/live_menus.py --version 26.3
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
from protobot.containers import ContainerError, MenuNotOpen, layout_for

BLOCKS = (
    "chest",
    "trapped_chest",
    "barrel",
    "dispenser",
    "dropper",
    "hopper",
    "furnace",
    "blast_furnace",
    "smoker",
    "crafting_table",
    "anvil",
    "enchanting_table",
    "brewing_stand",
    "beacon",
    "loom",
    "stonecutter",
    "smithing_table",
    "cartography_table",
    "grindstone",
    "shulker_box",
    "crafter",
)


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
        if expected is not None and state == expected:
            return state
        if expected is None and state not in (0, -1):
            return state
        await asyncio.sleep(0.02)
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
    parser.add_argument("--username", default="MenuProbe")
    parser.add_argument("--world", default="world")
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    parser.add_argument("--blocks", default="", help="comma separated subset")
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()

    blocks = [name.strip() for name in args.blocks.split(",") if name.strip()] or list(BLOCKS)
    report: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "version": args.version,
        "blocks": len(blocks),
    }
    results: list[dict[str, Any]] = []

    bot = await connect(
        args.host,
        port=args.port,
        username=args.username,
        version=args.version,
        timeout=20.0,
    )
    try:
        await bot.wait_world(timeout=20.0)
        position = bot.physics_state.position
        spot = (math.floor(position.x) + 2, math.floor(position.y), math.floor(position.z))
        report["spot"] = list(spot)
        await bot.navigate_to(spot[0] + 2.5, spot[2] + 0.5, timeout=60.0)
        report["distance"] = round(_distance(bot, spot), 3)

        for name in blocks:
            entry: dict[str, Any] = {"block": name}
            try:
                state = _setblock(args.bridge, args.world, spot, f"minecraft:{name}")
                entry["state"] = await _wait_state(bot, spot, state)
                menu = await bot.menu.open_block(spot, timeout=args.timeout)
                identifier = menu.identifier
                layout = layout_for(identifier, menu.slot_count or None)
                entry.update(
                    {
                        "menu": identifier,
                        "slots": menu.slot_count,
                        "container": len(layout.container),
                        "player": len(layout.player),
                        "known": not layout.derived,
                        "ok": not layout.derived and len(layout.player) == 36,
                    }
                )
                with contextlib.suppress(Exception):
                    await menu.close()
                await asyncio.sleep(0.1)
            except (MenuNotOpen, ContainerError) as error:
                entry.update({"ok": False, "error": f"{type(error).__name__}: {error}"})
            except BaseException as error:  # noqa: BLE001 - keep going through the list
                entry.update({"ok": False, "exception": f"{type(error).__name__}: {error}"})
            results.append(entry)
            if name != blocks[-1]:
                # The server and the client must agree that the previous window
                # is gone before the next right-click is meaningful.
                _setblock(args.bridge, args.world, spot, "minecraft:air")
                await _wait_state(bot, spot, 0)

        report["results"] = results
        report["ok"] = all(entry.get("ok") for entry in results) and bool(results)
        report["failed"] = [entry["block"] for entry in results if not entry.get("ok")]
    except BaseException as error:  # noqa: BLE001 - the probe reports, never raises
        report["ok"] = False
        report["exception"] = f"{type(error).__name__}: {error}"
    finally:
        with contextlib.suppress(Exception):
            await bot.close()
        print(json.dumps(report, indent=2, default=str), flush=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
