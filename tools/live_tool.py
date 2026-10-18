"""Prove on a real server that the hardness table predicts the break, per tool.

The hardness table is only worth having if the server agrees with it, so this
probe asks the server directly.  Stone needs 0.4 s with an iron pickaxe and
1.15 s with a wooden one; the bot holds for the same 0.4 s in both cases and
the probe records **when** the block actually disappeared:

* iron  -- progress is past 0.7 when the bot releases, so the server destroys
  the block at the stop: the delay between release and break is ~0;
* wood  -- progress is only ~0.36 at release, so the server keeps going
  (``hasDelayedDestroy``) and finishes the job about 0.7 s later.

That gap is the table being visibly right, and it is measured from the server's
own block state, not from the bot's opinion.

    python tools/live_tool.py --version 26.3
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

from live_chest import _bridge, _command, _nbt, _unwrap, _wait_inventory, _wait_state

#: Hold long enough that an iron pickaxe breaks stone (0.4 s) and short enough
#: that a wooden one (1.15 s) is still a long way off.
HOLD = 0.4


def _hotbar_slot(bot: Any, identifier: str) -> int | None:
    for slot in range(36, 45):
        stack = bot.get_inventory_item(slot)
        if not stack.empty and stack.identifier == identifier:
            return slot
    return None


def _server_block(base: str, world: str, target: tuple[int, int, int]) -> dict[str, Any]:
    payload = _unwrap(
        _bridge(
            base,
            "/block",
            query={"world": world, "x": target[0], "y": target[1], "z": target[2]},
        )
    )
    block = payload.get("block")
    if isinstance(block, dict):
        return block
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("block"), dict):
        return data["block"]
    return {}


def _setblock(base: str, world: str, target: tuple[int, int, int], data: str) -> dict[str, Any]:
    payload = _unwrap(
        _bridge(
            base,
            "/setblock",
            query={"world": world, "x": target[0], "y": target[1], "z": target[2]},
            body={"data": data},
        )
    )
    block = payload.get("block")
    return block if isinstance(block, dict) else {}


async def _lay_stone(bot: Any, base: str, world: str, target: tuple[int, int, int]) -> None:
    state = _setblock(base, world, target, "minecraft:stone")
    await _wait_state(bot, target, state.get("stateId"))


async def _measure(
    bot: Any,
    base: str,
    world: str,
    target: tuple[int, int, int],
    *,
    tool: str,
    hold: float = HOLD,
) -> dict[str, Any]:
    """Hold for ``hold`` seconds and report when the block actually went away."""

    await _lay_stone(bot, base, world, target)
    loop = asyncio.get_running_loop()
    seen: dict[str, float] = {}

    def on_update(x: int, y: int, z: int, state_id: int) -> None:
        if (x, y, z) == target and state_id == 0 and "air" not in seen:
            seen["air"] = loop.time()

    bot.events.on("block_update", on_update)
    try:
        started = loop.time()
        result = await bot.actions.dig(target, hold=hold, confirm_timeout=3.0)
        released = started + hold
    finally:
        bot.events.remove("block_update", on_update)

    air_at = seen.get("air")
    return {
        "tool": tool,
        "hold": hold,
        "status": result.status.value,
        "evidence": result.evidence,
        "broke": "air" in seen,
        "delay_after_release": None if air_at is None else round(air_at - released, 3),
        "total_to_break": None if air_at is None else round(air_at - started, 3),
        "server": _server_block(base, world, target).get("type"),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="ToolProbe")
    parser.add_argument("--world", default="world")
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "version": args.version,
        "world": args.world,
        "hold": HOLD,
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
        floor_x, floor_y, floor_z = (
            math.floor(position.x),
            math.floor(position.y),
            math.floor(position.z),
        )
        # Two different targets: a finished dig leaves the server's delayed
        # destroy armed, and a fresh block in the *same* spot would inherit it.
        target_iron = (floor_x + 2, floor_y, floor_z)
        target_wooden = (floor_x - 2, floor_y, floor_z)
        report["spawn"] = [round(position.x, 3), round(position.y, 3), round(position.z, 3)]
        report["targets"] = {"iron": list(target_iron), "wooden": list(target_wooden)}

        _command(args.bridge, f"clear {args.username}")
        for item in ("minecraft:wooden_pickaxe", "minecraft:iron_pickaxe"):
            _command(args.bridge, f"give {args.username} {item} 1")
        wooden = await _wait_inventory(bot, "minecraft:wooden_pickaxe", 1)
        iron = await _wait_inventory(bot, "minecraft:iron_pickaxe", 1)
        report["inventory"] = {"wooden_pickaxe": wooden, "iron_pickaxe": iron}
        if not wooden or not iron:
            report["ok"] = False
            report["error"] = "the bot never saw both pickaxes"
            return 1

        table = bot.version.breaking
        predictions = {
            "hand": table.seconds("minecraft:stone"),
            "wooden_pickaxe": table.seconds("minecraft:stone", tool="minecraft:wooden_pickaxe"),
            "iron_pickaxe": table.seconds("minecraft:stone", tool="minecraft:iron_pickaxe"),
        }
        report["predicted_break_seconds"] = predictions

        # equip_tool must pick the *faster* pickaxe, not just any pickaxe.
        equipped = await bot.actions.equip_tool("minecraft:stone")
        held = bot.held_item
        report["equipped"] = {
            "status": equipped.status.value,
            "evidence": equipped.evidence,
            "held": None if held.empty else held.identifier,
            "hotbar_index": bot.selected_hotbar_slot,
            "server_selected": _nbt(args.bridge, f"data get entity {args.username} SelectedItem"),
        }

        report["iron"] = await _measure(
            bot,
            args.bridge,
            args.world,
            target_iron,
            tool="minecraft:iron_pickaxe",
        )

        wooden_slot = _hotbar_slot(bot, "minecraft:wooden_pickaxe")
        await bot.select_hotbar_slot(wooden_slot - 36)
        await asyncio.sleep(0.3)
        report["server_held_wooden"] = _nbt(
            args.bridge, f"data get entity {args.username} SelectedItem"
        )
        report["wooden"] = await _measure(
            bot,
            args.bridge,
            args.world,
            target_wooden,
            tool="minecraft:wooden_pickaxe",
        )

        log = _unwrap(_bridge(args.bridge, "/log", query={"since": 0, "limit": 200}))
        lines = log.get("lines") or log.get("log") or []
        suspicious = [
            line
            for line in lines
            if isinstance(line, str)
            and any(
                marker in line
                for marker in ("moved too quickly", "moved wrongly", "Flying is not enabled")
            )
        ]
        report["suspicious_log"] = suspicious[-5:]

        iron_delay = report["iron"]["delay_after_release"]
        wooden_delay = report["wooden"]["delay_after_release"]
        report["delay_gap"] = (
            None if iron_delay is None or wooden_delay is None else round(wooden_delay - iron_delay, 3)
        )
        ok = (
            report["equipped"]["held"] == "minecraft:iron_pickaxe"
            and report["iron"]["broke"]
            and report["wooden"]["broke"]
            and iron_delay is not None
            and wooden_delay is not None
            and iron_delay <= 0.2
            and wooden_delay >= 0.4
            and not suspicious
        )
        report["ok"] = ok
        for target in (target_iron, target_wooden):
            _setblock(args.bridge, args.world, target, "minecraft:air")
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
