#!/usr/bin/env python3
"""Prove it on a real server: component stacks decode where they can, and say so where they cannot.

Two chests, both filled by the server itself with ``/item replace block``:

* **known** -- every item carries a component whose value codec this library
  implements (damage, enchantments, a text component, an empty unit component).
  The whole 63 slot content packet has to decode, slot for slot.
* **unknown** -- a potion and a filled shulker box carry component values whose
  codecs are not implemented.  The multi-slot packet must refuse to guess (the
  window goes byte-only), while a *single* stack packet -- the cursor and the
  clicked slot -- still decodes exactly, because the packet end gives its length.

The server's own ``data get block ... Items`` is the truth for every assert.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

from live_chest import _command, _nbt, _place_chest
from protobot import connect

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 25565
DEFAULT_WORLD = "world"
DEFAULT_CHEST = (8, -60, -8)

#: Items whose component values all have a codec: the content packet must decode
#: completely.  A plain stack sits before, between and after them so a wrong
#: length would show up as a shifted slot.
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

#: Items whose component values have no codec here: the window may only be
#: reported as unreadable, never invented.
UNKNOWN_ITEMS: tuple[tuple[int, str, int], ...] = (
    (0, 'minecraft:potion[minecraft:potion_contents={potion:"minecraft:healing"}]', 1),
    (1, "minecraft:dirt", 4),
    (
        2,
        'minecraft:shulker_box[minecraft:container=[{slot:0b,item:{id:"minecraft:diamond",count:5}}]]',
        1,
    ),
    (3, "minecraft:stone", 2),
)

KNOWN_EXPECTED = {
    0: ("stone", 3),
    1: ("diamond_pickaxe", 1),
    2: ("dirt", 7),
    3: ("enchanted_book", 1),
    4: ("oak_planks", 5),
    5: ("netherite_sword", 1),
    6: ("apple", 2),
    7: ("netherite_pickaxe", 1),
    8: ("stone", 9),
}
KNOWN_COMPONENT_SLOTS = (1, 3, 5, 7)

_ITEM_ENTRY = re.compile(
    r'\{Slot:\s*(?P<slot>\d+)b,\s*id:\s*"(?P<id>[^"]+)",\s*count:\s*(?P<count>\d+)b?'
)


def _server_items(base: str, world: str, target: tuple[int, int, int]) -> dict[int, tuple[str, int]]:
    """Ask the server which items are in the chest (authoritative)."""

    text = _nbt(base, f"data get block {target[0]} {target[1]} {target[2]} Items")
    found: dict[int, tuple[str, int]] = {}
    for match in _ITEM_ENTRY.finditer(text):
        name = match.group("id").split(":")[-1]
        found[int(match.group("slot"))] = (name, int(match.group("count")))
    return found


def _fill(base: str, target: tuple[int, int, int], items: tuple[tuple[int, str, int], ...]) -> list[str]:
    """Put ``items`` into the chest and return the slots the server refused."""

    refused: list[str] = []
    for slot, item, count in items:
        answer = _command(
            base,
            f"item replace block {target[0]} {target[1]} {target[2]} container.{slot} with {item} {count}",
        )
        if not answer.get("ok"):
            refused.append(f"slot {slot}: {answer.get('output')}")
    return refused


async def _wait_slots(bot: Any, container_id: int, *, timeout: float = 6.0) -> Any:
    """

    Wait until the window has a slot count and a settled state id.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        state = bot.containers.get(container_id)
        if state is not None and state.slot_count >= 37 and state.revision > 0:
            return state
        await asyncio.sleep(0.05)
    raise TimeoutError(f"container {container_id} never reported its slots")


def _decoded_slots(state: Any) -> dict[str, Any]:
    return {
        str(slot): {
            "identifier": item.identifier,
            "count": item.count,
            "has_components": item.has_components,
        }
        for slot, item in sorted(state.items.items())
        if not item.empty
    }


async def _open(bot: Any, base: str, target: tuple[int, int, int]) -> Any:
    await bot.navigate_to(target[0] + 0.5, target[2] + 2.5, tolerance=1.0, timeout=60.0)
    menu = bot.menu
    await menu.open_block(target)
    await _wait_slots(bot, menu.container_id)
    return menu


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="ComponentProbe")
    parser.add_argument("--world", default=DEFAULT_WORLD)
    parser.add_argument(
        "--target",
        default=f"{DEFAULT_CHEST[0]},{DEFAULT_CHEST[1]},{DEFAULT_CHEST[2]}",
        help="position of the first (known-components) chest as x,y,z",
    )
    args = parser.parse_args()

    target = tuple(int(part) for part in args.target.split(","))
    other = (target[0] + 2, target[1], target[2])
    base = f"http://{args.host}:25963"
    report: dict[str, Any] = {"known_chest": list(target), "unknown_chest": list(other), "ok": False}

    bot = await connect(args.host, port=args.port, username=args.username, version=args.version)
    try:
        await bot.navigate_to(target[0] + 0.5, target[2] + 2.5, tolerance=1.0, timeout=60.0)
        _place_chest(base, args.world, target)
        _place_chest(base, args.world, other)

        known_refused = _fill(base, target, KNOWN_ITEMS)
        unknown_refused = _fill(base, other, UNKNOWN_ITEMS)
        report["refused"] = {"known": known_refused, "unknown": unknown_refused}
        report["known_server"] = {
            str(slot): list(value) for slot, value in _server_items(base, args.world, target).items()
        }
        report["unknown_server"] = {
            str(slot): list(value) for slot, value in _server_items(base, args.world, other).items()
        }

        # -- known components: the whole window must decode ------------------
        menu = await _open(bot, base, target)
        state = menu.state()
        report["known"] = {
            "slot_count": state.slot_count,
            "decoded": _decoded_slots(state),
            "unreadable": sorted(state.unreadable),
            "carried_unknown": state.carried_unknown,
        }
        known_problems: list[str] = []
        if state.unreadable:
            known_problems.append(f"{len(state.unreadable)} slot(s) stayed byte-only")
        for slot, (name, count) in KNOWN_EXPECTED.items():
            item = state.items.get(slot)
            if item is None:
                known_problems.append(f"slot {slot} ({name}) did not decode")
                continue
            if item.identifier != f"minecraft:{name}" or item.count != count:
                known_problems.append(
                    f"slot {slot} decoded as {item.identifier} x{item.count}, "
                    f"server says {name} x{count}"
                )
        for slot in KNOWN_COMPONENT_SLOTS:
            item = state.items.get(slot)
            if item is not None and not item.has_components:
                known_problems.append(f"slot {slot} lost its component patch")
        await menu.close()

        # -- unknown components: refuse the list, read the single stack -----
        menu = await _open(bot, base, other)
        state = menu.state()
        report["unknown"] = {
            "slot_count": state.slot_count,
            "decoded": _decoded_slots(state),
            "unreadable": sorted(state.unreadable),
            "carried_unknown": state.carried_unknown,
        }
        unknown_problems: list[str] = []
        if not state.unreadable:
            unknown_problems.append("the ambiguous window was decoded anyway")
        if state.items:
            unknown_problems.append("slots were filled from an ambiguous packet")

        # Picking the potion up produces single-stack packets (the slot update
        # and the cursor), whose length the packet end fixes: those must decode.
        await menu.pickup(0)
        state = menu.state()
        carried = state.carried_item
        potion_slot = state.items.get(0)
        report["unknown"]["after_pickup"] = {
            "carried": [carried.identifier, carried.count, carried.has_components],
            "slot_0": None
            if potion_slot is None
            else [potion_slot.identifier, potion_slot.count, potion_slot.has_components],
            "unreadable": sorted(state.unreadable),
        }
        if carried.identifier != "minecraft:potion":
            unknown_problems.append(f"the cursor decoded as {carried.identifier}, not a potion")
        if not carried.has_components:
            unknown_problems.append("the cursor lost the potion's components")

        # Put it back and let the server confirm the chest is unchanged.
        await menu.pickup(0)
        await asyncio.sleep(0.4)
        after = _server_items(base, args.world, other)
        report["unknown"]["server_after_putback"] = {
            str(slot): list(value) for slot, value in after.items()
        }
        if after.get(0, ("", 0))[0] != "potion":
            unknown_problems.append(f"the server did not take the potion back: {after}")
        await menu.close()

        report["known_problems"] = known_problems
        report["unknown_problems"] = unknown_problems
        report["ok"] = not known_problems and not unknown_problems and not known_refused
    except Exception as error:  # noqa: BLE001 - the report carries the failure
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        await bot.close()

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
