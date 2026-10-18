"""Walk a long distance and prove the server saw the movement.

The point is not "did ``navigate_to`` return" but "did the player really end up
where the goal says".  The bridge reads the server's own player record, and the
server log is scanned for the complaints a cheating client provokes
(``moved too quickly``, ``moved wrongly``, ``Flying is not enabled``): a goal
may only claim success if the server agrees *and* stayed quiet.

The default distance is longer than the server's view distance, so the target
chunk is not loaded when the goal starts.  ``TravelGoal`` therefore cannot hand
the whole trip to the pathfinder: it walks to the edge of the terrain it knows,
waits for the next chunks, and continues -- which is exactly what a client that
only trusts what the server sent has to do.

Usage::

    uv run python tools/live_travel.py --distance 220
    uv run python tools/live_travel.py --distance 60 --axis z --tolerance 1.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

from protobot import connect
from protobot.goals import GoalStatus, TravelGoal

SUSPICIOUS = ("moved too quickly", "moved wrongly", "Flying is not enabled")


def _bridge(
    base: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = base.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode({key: str(value) for key, value in query.items()})
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _server_position(base: str, username: str) -> tuple[float, float, float] | None:
    """The position the *server* believes the player is at."""
    players = _unwrap(_bridge(base, "/players")).get("players") or []
    for entry in players:
        if entry.get("name") != username:
            continue
        if all(key in entry for key in ("x", "y", "z")):
            return (float(entry["x"]), float(entry["y"]), float(entry["z"]))
        position = entry.get("position") or entry.get("location")
        if isinstance(position, dict):
            return (float(position["x"]), float(position["y"]), float(position["z"]))
        if isinstance(position, (list, tuple)) and len(position) >= 3:
            return (float(position[0]), float(position[1]), float(position[2]))
    return None


def _log_cursor(base: str) -> int:
    payload = _unwrap(_bridge(base, "/log", query={"lines": 1}))
    return int(payload.get("next") or 0)


def _suspicious_lines(base: str, since: int, username: str) -> list[str]:
    payload = _unwrap(_bridge(base, "/log", query={"since": since, "lines": 4000}))
    hits: list[str] = []
    for entry in payload.get("entries") or []:
        message = str(entry.get("msg") or "")
        if username not in message and "Flying is not enabled" not in message:
            continue
        lowered = message.lower()
        if any(marker.lower() in lowered for marker in SUSPICIOUS):
            hits.append(f"{entry.get('source')}: {message}")
    return hits


def _horizontal(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.hypot(a[0] - b[0], a[2] - b[2])


async def _wait_ready(bot: Any, *, timeout: float = 30.0) -> tuple[float, float, float]:
    """Wait until the server has sent the terrain the player stands on.

    ``World.get_state_id`` answers 0 for a chunk it has not decoded, so "is not
    -1" would pass immediately; the decoded-chunk map is the honest signal.
    """

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


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="TravelProbe")
    parser.add_argument("--distance", type=float, default=220.0, help="blocks to walk from the spawn point")
    parser.add_argument("--axis", choices=("x", "z"), default="x")
    parser.add_argument("--tolerance", type=float, default=1.5)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--segment-seconds", type=float, default=20.0)
    args = parser.parse_args()

    report: dict[str, Any] = {
        "version": args.version,
        "username": args.username,
        "distance": args.distance,
        "axis": args.axis,
        "tolerance": args.tolerance,
        "ok": False,
    }
    bot = await connect(
        args.host,
        port=args.port,
        username=args.username,
        version=args.version,
    )
    try:
        start = await _wait_ready(bot)
        start_server = _server_position(args.bridge, args.username) or start
        target_x = start[0] + args.distance if args.axis == "x" else start[0]
        target_z = start[2] + args.distance if args.axis == "z" else start[2]
        goal = TravelGoal(
            target_x,
            target_z,
            tolerance=args.tolerance,
            timeout=args.timeout,
            segment_seconds=args.segment_seconds,
        )
        cursor = _log_cursor(args.bridge)
        segments: list[tuple[float, float]] = []
        original = bot.navigate_to

        async def counting(x: float, z: float, **kwargs: Any) -> Any:
            segments.append((x, z))
            return await original(x, z, **kwargs)

        bot.navigate_to = counting
        started = time.monotonic()
        result = await bot.goals.run(goal, timeout=args.timeout + 30.0)
        elapsed = time.monotonic() - started
        end = bot.physics_state.position
        end_server = _server_position(args.bridge, args.username) or (end.x, end.y, end.z)
        target = (target_x, end_server[1], target_z)
        suspicious = _suspicious_lines(args.bridge, cursor, args.username)
        report.update(
            {
                "status": result.status.value,
                "elapsed": round(elapsed, 3),
                "detail": result.detail,
                "segments": len(segments),
                "segment_targets": [[round(x, 1), round(z, 1)] for x, z in segments[:12]],
                "start_server": [round(value, 2) for value in start_server],
                "end_server": [round(value, 2) for value in end_server],
                "target": [round(target_x, 2), round(target_z, 2)],
                "walked": round(_horizontal(start_server, end_server), 2),
                "server_distance_to_target": round(
                    math.hypot(end_server[0] - target_x, end_server[2] - target_z), 2
                ),
                "suspicious_log": suspicious,
            }
        )
        report["ok"] = bool(
            result.status is GoalStatus.DONE
            and report["server_distance_to_target"] <= args.tolerance + 1.0
            and not suspicious
        )
    finally:
        await bot.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
