"""Prove that a break is visible to another player: the arm swings.

``Actions.dig`` sends a swing with the button press and then keeps swinging while
it is held.  The server answers every swing with a ``ClientboundSwingAnimation``
broadcast to the players that can see the digger, so a second bot standing next
to it is the real audience test: if the watcher receives the animation for the
digger's entity, a human client would see the arm move.

Two things that cost an hour here:

* wait for the chunk *without* ticking.  ``tick()`` runs the physics on what the
  client has decoded so far; ticking before the ground arrives means falling,
  and the server believes the falling position because the client keeps
  reporting it;
* keep both bots ticking once loaded, or the idle one stops moving and the
  server stops tracking entities for it.

Usage::

    python tools/live_swing_seen.py --version 26.3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import urllib.request
from typing import Any

from protobot import connect

SWING_ANIMATION = 0
TARGET = (2, -60, -4)


def bridge(base: str, path: str, body: dict[str, Any] | None = None) -> Any:
    url = f"{base}{path}"
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def chunk_key(x: float, z: float) -> tuple[int, int]:
    return (math.floor(x) >> 4, math.floor(z) >> 4)


async def wait_chunk(bot, x: float, z: float, *, timeout: float = 20.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    key = chunk_key(x, z)
    while loop.time() < deadline:
        if key in getattr(bot.world, "chunks", {}):
            return True
        await asyncio.sleep(0.05)
    return key in getattr(bot.world, "chunks", {})


async def run(args: argparse.Namespace) -> int:
    status = bridge(args.bridge, "/status")
    print(
        f"[bridge] {status.get('serverVersion')} players={len(status.get('players') or [])}",
        flush=True,
    )

    watcher = await connect(
        args.host, port=args.port, username=args.watcher, version=args.version, timeout=30.0
    )
    digger = await connect(
        args.host, port=args.port, username=args.digger, version=args.version, timeout=30.0
    )
    seen: list[dict[str, int]] = []

    def on_swing(entity_id: int, hand: int) -> None:
        seen.append({"entity_id": entity_id, "hand": hand})

    watcher.events.on("swing_animation", on_swing)
    tickers: list[asyncio.Task] = []
    try:
        await asyncio.sleep(1.5)
        bridge(args.bridge, "/command", {"command": f"gamemode survival {args.digger}"})
        bridge(args.bridge, "/command", {"command": f"clear {args.digger}"})
        bridge(
            args.bridge,
            "/command",
            {"command": f"tp {args.digger} {TARGET[0] + 2.5} -60 {TARGET[2] - 0.5} 0 0"},
        )
        bridge(
            args.bridge,
            "/command",
            {"command": f"tp {args.watcher} {TARGET[0] + 2.5} -60 {TARGET[2] + 3.5} 0 0"},
        )
        bridge(
            args.bridge,
            f"/setblock?world={args.world}&x={TARGET[0]}&y={TARGET[1]}&z={TARGET[2]}",
            {"data": args.block},
        )

        watcher_ready = await wait_chunk(watcher, TARGET[0] + 2.5, TARGET[2] + 3.5)
        digger_ready = await wait_chunk(digger, TARGET[0] + 2.5, TARGET[2] - 0.5)
        if not (watcher_ready and digger_ready):
            print(json.dumps({"ok": False, "reason": "chunks never loaded"}), flush=True)
            return 1

        async def keep(bot) -> None:
            while True:
                await bot.tick()
                await asyncio.sleep(0.05)

        tickers = [asyncio.create_task(keep(watcher)), asyncio.create_task(keep(digger))]
        await asyncio.sleep(1.5)

        before = digger.world.get_state_id(*TARGET)
        result = await digger.actions.dig(TARGET, hold=args.hold)
        await asyncio.sleep(0.5)
        after = digger.world.get_state_id(*TARGET)

        animations = [entry for entry in seen if entry["hand"] == SWING_ANIMATION]
        report = {
            "target": list(TARGET),
            "dig_status": str(result.status),
            "state_before": before,
            "state_after": after,
            "watcher_sees_entities": len(watcher.entities),
            "swings_seen": len(animations),
            "animations": animations[:8],
            "ok": before != 0 and after == 0 and bool(animations),
        }
        print(json.dumps(report, ensure_ascii=False, indent=1), flush=True)
        return 0 if report["ok"] else 1
    finally:
        for task in tickers:
            task.cancel()
        for task in tickers:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await digger.close()
        await watcher.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--bridge", default="http://127.0.0.1:25963")
    parser.add_argument("--world", default="world")
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--digger", default="SwingDigger")
    parser.add_argument("--watcher", default="SwingWatcher")
    parser.add_argument("--block", default="minecraft:dirt")
    parser.add_argument("--hold", type=float, default=1.2)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
