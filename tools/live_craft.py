"""Craft planks, a crafting table, and sticks on a real server.

This proves the crafting layer end to end against a vanilla server: the 2x2
recipe runs in the player's own grid, the table is then placed with the
interaction layer, and the 3x3 sticks recipe runs in the window the server
opens. Every claim is checked against the server's own view through the
prototest bridge, not against the client's model.

Usage:
    python tools/live_craft.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import sys
from typing import Any

from protobot import connect
from protobot.crafting import recipe_for
from protobot.goals import CraftGoal, GoalStatus, PlaceBlockGoal

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from live_chest import (  # noqa: E402  (path shim above)
    _bridge,
    _command,
    _nbt,
    _unwrap,
    _wait_inventory,
)

PLANKS = "minecraft:oak_planks"
LOG = "minecraft:oak_log"
TABLE = "minecraft:crafting_table"
STICKS = "minecraft:stick"


async def _count(bot: Any, item: str) -> int:
    total = 0
    for slot in range(46):
        stack = bot.get_inventory_item(slot)
        if stack.identifier == item:
            total += stack.count
    return total


def _server_block(base: str, world: str, target: tuple[int, int, int]) -> dict[str, Any]:
    payload = _unwrap(
        _bridge(
            base,
            "/block",
            query={"world": world, "x": target[0], "y": target[1], "z": target[2]},
        )
    )
    return payload.get("block") or {}


def _free_target(bot: Any, world: str, base: str) -> tuple[int, int, int]:
    """Pick an air voxel next to the bot with solid ground below it."""

    position = bot.physics_state.position
    feet_y = math.floor(position.y)
    for dx, dz in ((2, 0), (-2, 0), (0, 2), (0, -2), (1, 1), (-1, -1)):
        target = (math.floor(position.x) + dx, feet_y, math.floor(position.z) + dz)
        block = _server_block(base, world, target)
        below = _server_block(base, world, (target[0], target[1] - 1, target[2]))
        if block.get("stateId", 0) == 0 and below.get("stateId", 0) not in (0, None):
            return target
    raise SystemExit("no free voxel next to the bot to place the crafting table")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="CraftProbe")
    parser.add_argument("--world", default="world")
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    parser.add_argument("--logs", type=int, default=8)
    args = parser.parse_args()

    report: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "version": args.version,
        "username": args.username,
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
    ok = False
    try:
        await bot.wait_world(timeout=20.0)
        _command(args.bridge, f"clear {args.username}")
        _command(args.bridge, f"give {args.username} {LOG} {args.logs}")
        report["logs_before"] = await _wait_inventory(bot, LOG, args.logs, timeout=8.0)

        planks_goal = CraftGoal(recipe_for(PLANKS), times=2)
        planks_report = await bot.goals.run(planks_goal, timeout=60.0)
        report["planks"] = {
            "status": planks_report.status.value,
            "detail": planks_report.detail,
            "count": await _count(bot, PLANKS),
        }

        table_goal = CraftGoal(recipe_for(TABLE), times=1)
        table_report = await bot.goals.run(table_goal, timeout=60.0)
        report["table"] = {
            "status": table_report.status.value,
            "detail": table_report.detail,
            "count": await _count(bot, TABLE),
        }

        # The placement goal only reaches into the hotbar, so put the table
        # there explicitly -- crafting the planks may have filled slot 36.
        menu = bot.player_menu
        table_slot = menu.find(TABLE, where="player")
        if table_slot is None:
            raise SystemExit("the crafted table is not in the inventory")
        if not 36 <= table_slot <= 44:
            target_slot = next(
                (slot for slot in range(36, 45) if not menu.item(slot).count), 36
            )
            await menu.move(table_slot, target_slot)
            table_slot = target_slot
        report["table_slot"] = table_slot

        target = _free_target(bot, args.world, args.bridge)
        report["target"] = list(target)
        place_goal = PlaceBlockGoal(target, item=TABLE)
        place_report = await bot.goals.run(place_goal, timeout=60.0)
        report["place"] = {"status": place_report.status.value, "detail": place_report.detail}
        report["server_block"] = _server_block(args.bridge, args.world, target)

        sticks_goal = CraftGoal(recipe_for(STICKS), times=1, table=target)
        sticks_report = await bot.goals.run(sticks_goal, timeout=60.0)
        report["sticks"] = {
            "status": sticks_report.status.value,
            "detail": sticks_report.detail,
            "count": await _count(bot, STICKS),
        }
        report["player_inventory_nbt"] = _nbt(
            args.bridge, f"data get entity {args.username} Inventory"
        )
        ok = (
            planks_report.status is GoalStatus.DONE
            and table_report.status is GoalStatus.DONE
            and place_report.status is GoalStatus.DONE
            and sticks_report.status is GoalStatus.DONE
            and report["sticks"]["count"] >= 4
        )
    finally:
        report["events"] = events[-6:]
        print(json.dumps(report, indent=2))
        with contextlib.suppress(Exception):
            await bot.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
