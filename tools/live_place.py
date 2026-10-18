"""Live check: place a block on a real server and prove it landed.

``live_dig.py`` proves the break path; this proves the *other* half of the
interaction layer -- ``ServerboundUseItemOnPacket``.  That packet is the one
with the most ways to be subtly wrong, and every one of them fails silently:

* ``BlockHitResult`` carries **two** booleans (inside-block and world-border);
  write one and the server reads ``sequence`` from the wrong offset, drops the
  placement and never complains;
* the three floats are block-relative ``[0, 1)``, not world coordinates;
* the held item must actually be the block being placed, which needs the item
  registry (dirt is registry id 28 on 1.21.8 but 55 on 26.3).

Usage::

    uv run python tools/live_place.py --version 26.3 --target 2,-60,0

With ``--give`` the tool asks the prototest bridge to hand the bot the block,
which is what a survival bot needs (it cannot place from an empty inventory).
Without a bridge, put the block in the bot's hand yourself.

The target is the *destination* voxel; the tool clicks the neighbouring block's
upward face, exactly like a player would.  Prints a JSON report and exits
non-zero unless the client and the server both agree the block exists.
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


def _server_block(base: str, world: str, target: tuple[int, int, int]) -> dict[str, Any]:
    payload = _bridge(
        base,
        "/block",
        query={"world": world, "x": target[0], "y": target[1], "z": target[2]},
    )
    # The bridge answers {"ok": true, "block": {...}}; tools that wrap the call
    # (the DSH plugin) nest the same body under "data".
    block = payload.get("block")
    if block is None:
        block = payload.get("data", {}).get("block", {})
    return block


def _distance(bot: Any, target: tuple[int, int, int]) -> float:
    eye = bot.physics_state.position
    return math.dist(
        (eye.x, eye.y + 1.62, eye.z),
        (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5),
    )


async def _wait_hotbar_item(bot: Any, item: str, *, timeout: float = 5.0) -> int | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for slot in range(36, 45):
            if bot.get_inventory_item(slot).identifier == item:
                return slot
        await asyncio.sleep(0.1)
    return None


async def _wait_state(
    bot: Any, target: tuple[int, int, int], expected: int, *, timeout: float = 3.0
) -> int:
    """Wait until the client's own world agrees with an out-of-band edit.

    A bridge ``setblock`` reaches the client as a ``block_update`` packet a few
    milliseconds after the HTTP call returns; placing before that lands on a
    stale copy of the world and the action is rejected.
    """

    deadline = asyncio.get_running_loop().time() + timeout
    state = bot.world.get_state_id(*target)
    while state != expected and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        state = bot.world.get_state_id(*target)
    return state


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="PlaceProbe")
    parser.add_argument("--target", default=None, help="destination x,y,z")
    parser.add_argument("--item", default="minecraft:dirt")
    parser.add_argument("--world", default="world")
    parser.add_argument(
        "--bridge",
        default="http://127.0.0.1:25963",
        help="prototest bridge base URL (used for --give and the server check)",
    )
    parser.add_argument(
        "--give",
        action="store_true",
        help="give the bot the block through the bridge before placing",
    )
    parser.add_argument("--confirm-timeout", type=float, default=2.0)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="clear the destination to air through the bridge first (repeatable proof)",
    )
    args = parser.parse_args()

    if args.target:
        parts = tuple(int(part) for part in args.target.split(","))
        if len(parts) != 3:
            raise SystemExit("--target needs exactly three integers: x,y,z")
        target = parts
    else:
        position = None
        target = None

    report: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "version": args.version,
        "item": args.item,
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
        if target is None:
            position = bot.physics_state.position
            target = (
                math.floor(position.x) + 2,
                math.floor(position.y),
                math.floor(position.z),
            )
        report["target"] = list(target)
        position = bot.physics_state.position
        report["position"] = [round(position.x, 3), round(position.y, 3), round(position.z, 3)]

        if args.reset:
            _bridge(
                args.bridge,
                "/setblock",
                query={
                    "world": args.world,
                    "x": target[0],
                    "y": target[1],
                    "z": target[2],
                },
                body={"data": "minecraft:air"},
            )
            report["reset_state"] = await _wait_state(bot, target, 0)

        if args.give:
            given = _bridge(
                args.bridge,
                "/command",
                body={"command": f"give {args.username} {args.item} 1"},
            )
            report["give"] = given.get("data", given)

        slot = await _wait_hotbar_item(bot, args.item)
        report["hotbar_slot"] = slot
        if slot is None:
            report["ok"] = False
            report["error"] = f"{args.item} never reached the hotbar; use --give"
            return 1
        await bot.select_hotbar_slot(slot - 36)
        report["held"] = bot.held_item.identifier

        report["distance"] = round(_distance(bot, target), 3)
        if report["distance"] > 4.0:
            await bot.navigate_to(target[0] + 2.5, target[2] + 0.5, timeout=60.0)
            moved = bot.physics_state.position
            report["position_after_walk"] = [
                round(moved.x, 3),
                round(moved.y, 3),
                round(moved.z, 3),
            ]
            report["distance"] = round(_distance(bot, target), 3)

        state_before = bot.world.get_state_id(*target)
        report["state_before"] = state_before
        result = await bot.actions.place_block(target, confirm_timeout=args.confirm_timeout)
        report.update(
            {
                "status": result.status.value,
                "confirmed": result.confirmed,
                "evidence": result.evidence,
                "packets": list(result.packets),
                "elapsed": round(result.elapsed, 3),
                "state_after": bot.world.get_state_id(*target),
            }
        )

        block = _server_block(args.bridge, args.world, target)
        report["server_block"] = {
            key: block.get(key)
            for key in ("type", "stateId", "solid", "hardness")
            if key in block
        }
        report["ok"] = (
            block.get("type") == args.item
            and block.get("stateId") not in (None, 0)
            and report["state_after"] == block.get("stateId")
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
