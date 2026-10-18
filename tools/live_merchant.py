"""Live check: complete a villager trade through the container layer.

The chest and furnace tools prove windows opened by *blocks*.  This one proves
the entity path -- ``Menu.open_entity`` against a villager -- and with it the
last menu type 26.3 has (``minecraft:merchant``).

The run is a real trade, not just an opened window:

1. the bridge summons a farmer villager that offers 2 emeralds for 6 bread;
2. the bot right-clicks the villager and the server opens ``minecraft:merchant``;
3. the bot moves 2 emeralds into the buy slot;
4. the server fills the result slot on its own -- the layer only reads it;
5. the bot takes the result with a shift click and the trade is recorded;
6. the bridge confirms both the player's bread and the villager's ``uses``
   counter, so the trade really happened on the server.

Usage::

    uv run python tools/live_merchant.py --version 26.3
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
from protobot.containers import layout_for
from protobot.physics import Vec3

from live_chest import _bridge, _command, _nbt, _unwrap, _wait_inventory

OFFER = (
    '{VillagerData:{profession:"minecraft:farmer",level:2},'
    "Offers:{Recipes:[{buy:{id:\"minecraft:emerald\",count:2},"
    'sell:{id:"minecraft:bread",count:6},maxUses:16,uses:0,xp:1,'
    "priceMultiplier:0.05f,specialPrice:0,demand:0,rewardExp:1b}]}}"
)


async def _find_entity(bot: Any, *, timeout: float = 6.0) -> Any | None:
    """Wait for the closest entity near the bot (the villager we summoned)."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    position = bot.physics_state.position
    while loop.time() < deadline:
        best: tuple[float, Any] | None = None
        for entity in bot.entities.values():
            offset = Vec3(entity.x, entity.y, entity.z) - position
            distance = math.sqrt(offset.length_squared)
            if best is None or distance < best[0]:
                best = (distance, entity)
        if best is not None and best[0] <= 6.0:
            return best[1]
        await asyncio.sleep(0.05)
    return None


async def _wait_result(menu: Any, item: str, *, timeout: float = 5.0) -> str | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        stack = menu.item(menu.slot("result"))
        if stack.identifier == item:
            return stack.identifier
        await asyncio.sleep(0.05)
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="MerchantProbe")
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    parser.add_argument("--buy", default="minecraft:emerald")
    parser.add_argument("--buy-count", type=int, default=2)
    parser.add_argument("--sell", default="minecraft:bread")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "version": args.version,
        "buy": f"{args.buy_count} {args.buy}",
        "sell": args.sell,
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
        report["spawn"] = [round(position.x, 3), round(position.y, 3), round(position.z, 3)]

        _command(args.bridge, "kill @e[type=minecraft:villager]")
        summon = (
            f"summon minecraft:villager {position.x + 2:.2f} {position.y:.2f}"
            f" {position.z:.2f} {OFFER}"
        )
        report["summon"] = _command(args.bridge, summon).get("output")

        villager = await _find_entity(bot)
        if villager is None:
            report["ok"] = False
            report["error"] = "the summoned villager never appeared to the bot"
            return 1
        centre = Vec3(villager.x, villager.y, villager.z)
        offset = centre - position
        report["entity"] = {
            "id": villager.entity_id,
            "type_id": villager.type_id,
            "distance": round(math.sqrt(offset.length_squared), 3),
        }

        menu = await bot.menu.open_entity(villager.entity_id, position=centre)
        state = menu.state()
        layout = layout_for(menu.identifier, menu.slot_count)
        report["menu"] = {
            "identifier": menu.identifier,
            "menu_type_id": state.menu_type_id,
            "slot_count": menu.slot_count,
            "derived": layout.derived,
            "buy_slots": layout.slots("container"),
            "result_slot": layout.slot("result"),
            "player": [layout.player[0], layout.player[-1]],
        }

        # The player's inventory survives across runs on this server, so the
        # leftover emeralds of an earlier run would make the deposit ambiguous.
        report["clear"] = _command(args.bridge, f"clear {args.username}").get("output")
        _command(args.bridge, f"give {args.username} {args.buy} {args.buy_count}")
        held = await _wait_inventory(bot, args.buy, args.buy_count)
        report["inventory_before"] = held
        if held < args.buy_count:
            report["ok"] = False
            report["error"] = f"the bot only saw {held} {args.buy}"
            return 1

        moved = await menu.deposit(args.buy, args.buy_count)
        report["deposited"] = moved
        report["result_after_deposit"] = await _wait_result(menu, args.sell)

        took = await menu.quick_move(menu.slot("result"))
        report["took_result"] = took
        await asyncio.sleep(0.2)
        report["player_view"] = menu.count(args.sell, where="player")
        report["player_nbt"] = _nbt(args.bridge, f"data get entity {args.username} Inventory")
        report["offers_nbt"] = _nbt(
            args.bridge, "data get entity @e[type=minecraft:villager,limit=1] Offers.Recipes"
        )

        with contextlib.suppress(Exception):
            await menu.close()

        report["ok"] = (
            moved == args.buy_count
            and report["result_after_deposit"] == args.sell
            and report["player_view"] > 0
            and args.sell in report["player_nbt"]
            and "uses: 0" not in report["offers_nbt"]
            and "uses:" in report["offers_nbt"]
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
        with contextlib.suppress(Exception):
            _command(args.bridge, "kill @e[type=minecraft:villager]")
        print(json.dumps(report, indent=2, default=str), flush=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
