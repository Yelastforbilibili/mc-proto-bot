"""Prove the entity raycast and the attack packet against a real server.

The bot cannot see the mob it is hitting -- it sees an entity id, a position
and a type id, and has to compute the hitbox itself.  So the proof here is the
server's own health value, not anything the client believes:

1. summon a stationary mob two blocks in front of the player,
2. aim at it with ``Actions.look_at_entity`` and hit it with
   ``Actions.attack_looked_at``,
3. ask the server for ``Health`` before, during and after the fight,
4. wait for the server to remove the entity, and check the server log for
   movement/anti-cheat complaints.

The mob is summoned with ``NoAI`` on purpose: this tool isolates the packet --
one hit, one health point -- and a target that runs away would mix the chase in.
For a live target that walks and is knocked back, run scenarios 15/16/23 of
``tools/live_suite.py``, which summon ordinary cows and follow them.

Run it with a Paper 26.3 server and the prototest bridge up::

    python tools/live_combat.py --version 26.3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from typing import Any

from protobot import connect
from protobot.actions import ActionStatus
from protobot.physics import Vec3

from live_chest import _bridge, _command, _nbt, _unwrap

MOB = "minecraft:cow"
SUSPICIOUS = ("moved too quickly", "moved wrongly", "Flying is not enabled")

_HEALTH = re.compile(r"(-?\d+(?:\.\d+)?)")


def _log_cursor(base: str) -> int:
    payload = _unwrap(_bridge(base, "/log", query={"limit": 1}))
    return int(payload.get("next") or 0)


def _suspicious_lines(base: str, since: int, username: str) -> list[str]:
    payload = _unwrap(_bridge(base, "/log", query={"since": since, "limit": 4000}))
    hits: list[str] = []
    for entry in payload.get("entries") or []:
        message = str(entry.get("msg") or "")
        if username not in message and "Flying is not enabled" not in message:
            continue
        lowered = message.lower()
        if any(marker.lower() in lowered for marker in SUSPICIOUS):
            hits.append(f"{entry.get('source')}: {message}")
    return hits


def _events(base: str, *, types: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Every event the server recorded since the buffer was cleared."""

    payload = _unwrap(_bridge(base, "/events", query={"since": 0, "limit": 1000}))
    entries = payload.get("entries") or []
    if not types:
        return [dict(entry) for entry in entries]
    return [dict(entry) for entry in entries if entry.get("type") in types]


def _selector(mob: str, spot: tuple[float, float, float]) -> str:
    """Target the mob we put there, not whichever one happens to be nearest."""

    return (
        f"@e[type={mob},tag=protobot-combat,x={spot[0]:.2f},"
        f"y={spot[1]:.2f},z={spot[2]:.2f},distance=..3,limit=1,sort=nearest]"
    )


def _mob_health(base: str, mob: str, spot: tuple[float, float, float]) -> float | None:
    """The server's health value for the mob standing at ``spot``."""

    text = _nbt(base, f"data get entity {_selector(mob, spot)} Health")
    match = _HEALTH.search(text.replace("has the following entity data:", ""))
    if match is None:
        return None
    return float(match.group(1))


async def _wait_ready(bot: Any, *, timeout: float = 30.0) -> tuple[float, float, float]:
    """Wait until the chunk the player stands in has been decoded."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        position = bot.physics_state.position
        chunk = (int(position.x // 1) >> 4, int(position.z // 1) >> 4)
        chunks = getattr(bot.world, "chunks", None)
        if isinstance(chunks, dict) and chunk in chunks:
            return (position.x, position.y, position.z)
        await asyncio.sleep(0.05)
    raise TimeoutError("the server never sent the chunk the player spawns in")


async def _wait_entity(
    bot: Any, mob: str, before: set[int], spot: tuple[float, float, float], *,
    tolerance: float = 2.0, timeout: float = 10.0,
) -> Any | None:
    """Wait for the mob this run added, matched by id and position.

    Matching on the nearest mob of the type is not enough: the server tracks
    entities dozens of blocks out, and a leftover mob can sit exactly where the
    new one lands.  An id we have not seen yet, near the summon point, is the
    only unambiguous answer.
    """

    target = Vec3(*spot)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for entity in bot.entities.values():
            if entity.entity_id in before:
                continue
            if bot.version.entity_identifier(entity.type_id) != mob:
                continue
            offset = Vec3(entity.x, entity.y, entity.z) - target
            if math.sqrt(offset.length_squared) <= tolerance:
                return entity
        await asyncio.sleep(0.05)
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="CombatProbe")
    parser.add_argument("--mob", default=MOB, help="entity type to summon (default: a peaceful-safe cow)")
    parser.add_argument(
        "--nbt",
        default='{NoAI:1b,Silent:1b,PersistenceRequired:1b,Tags:["protobot-combat"]}',
        help="NBT for the summoned mob (keep the tag so cleanup can find it)",
    )
    parser.add_argument("--reach", type=float, default=2.5, help="blocks in front of the player to summon at")
    parser.add_argument("--hits", type=int, default=12, help="how many attacks to try")
    parser.add_argument("--interval", type=float, default=0.65, help="seconds between attacks (vanilla cooldown)")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    report: dict[str, Any] = {"username": args.username, "version": args.version}
    cursor = _log_cursor(args.bridge)
    _bridge(args.bridge, "/events/clear", body={})
    bot = await connect(
        args.host,
        port=args.port,
        username=args.username,
        version=args.version,
        timeout=args.timeout,
    )
    try:
        x, y, z = await _wait_ready(bot)
        report["start"] = [round(x, 2), round(y, 2), round(z, 2)]

        spot = (x, y, z + args.reach)
        # Clear anything standing where the summon lands: with an older mob in
        # between, the ray hits it and the health of *our* mob never moves.
        _command(args.bridge, "kill @e[type=" + args.mob + ",tag=protobot-combat]")
        _command(
            args.bridge,
            f"kill @e[type={args.mob},x={spot[0]:.2f},y={spot[1]:.2f},"
            f"z={spot[2]:.2f},distance=..6]",
        )
        before = set(bot.entities)
        _command(
            args.bridge,
            f"summon {args.mob} "
            f"{spot[0]:.2f} {spot[1]:.2f} {spot[2]:.2f} "
            + args.nbt,
        )
        entity = await _wait_entity(bot, args.mob, before, spot)
        if entity is None:
            report["error"] = "the summoned mob never reached the client"
            print(json.dumps(report, indent=2))
            return 1
        report["entity_id"] = entity.entity_id
        report["spot"] = [round(value, 2) for value in spot]
        report["type"] = bot.version.entity_identifier(entity.type_id)
        report["size"] = list(bot.actions.entity_size(entity))
        report["distance"] = round(
            math.sqrt((Vec3(entity.x, entity.y, entity.z) - bot.physics_state.position).length_squared),
            2,
        )
        report["health_before"] = _mob_health(args.bridge, args.mob, spot)

        hits: list[dict[str, Any]] = []
        lowest = report["health_before"]
        for index in range(args.hits):
            # aim first, exactly like a player: the ray is computed from the
            # rotation the client actually sent
            await bot.actions.look_at_entity(entity.entity_id)
            hit = bot.actions.raycast_entity()
            if hit is None:
                hits.append({"index": index, "raycast": "no entity under the crosshair"})
                break
            result = await bot.actions.attack_looked_at()
            if result is None:
                hits.append({"index": index, "error": "attack_looked_at saw nothing"})
                break
            health = _mob_health(args.bridge, args.mob, spot)
            if health is not None and (lowest is None or health < lowest):
                lowest = health
            hits.append(
                {
                    "index": index,
                    "status": result.status.value,
                    "confirmed": result.status is ActionStatus.CONFIRMED,
                    "entity_id": result.entity_id,
                    "evidence": result.evidence,
                    "server_health": health,
                }
            )
            if health is None:
                break
            await asyncio.sleep(args.interval)
        report["hits"] = hits
        report["health_after"] = _mob_health(args.bridge, args.mob, spot)
        report["health_lowest"] = lowest

        # the server removes a dead mob, and the client drops it from tracking
        gone = False
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            if entity.entity_id not in bot.entities:
                gone = True
                break
            await asyncio.sleep(0.05)
        report["entity_removed"] = gone
        report["suspicious_log"] = _suspicious_lines(args.bridge, cursor, args.username)

        # The bridge records player events; a swing per hit proves each click
        # reached the server, and the health drop proves the attack landed.
        events = _events(args.bridge, types=("swing", "interact", "damage", "death"))
        report["swings"] = sum(1 for entry in events if entry.get("type") == "swing")
        report["events"] = events[:20]

        damaged = report["health_before"] is not None and (
            lowest is not None and lowest < report["health_before"]
        )
        killed = report["health_after"] is None and report["entity_removed"]
        budget = (
            bool(hits)
            and damaged
            and killed
            and report["swings"] >= len(hits)
            and not report["suspicious_log"]
        )
        report["ok"] = budget
        if not budget:
            report["error"] = "the server never confirmed the kill"
        print(json.dumps(report, indent=2))
        return 0 if budget else 1
    finally:
        await bot.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
