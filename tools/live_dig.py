"""Live check: dig a real block on a real server and prove it broke.

This is the end-to-end assertion the interaction layer exists for, and it is
deliberately *not* a unit test: it needs a running server, a spawned player and
a solid block within reach, and it proves three separate things that a fake
cannot:

1. the client's packet actually reaches the server (the server logs the
   left click and then a block break event);
2. the server agrees the block is gone (the authoritative state is air);
3. the client saw the change (the world state went from solid to ``0``).

Usage::

    uv run python tools/live_dig.py --host 127.0.0.1 --port 25565 --version 26.3

Without ``--target`` the block under the player's feet is used, which is the
one block every flat test world is guaranteed to have.  Prints a JSON report and
exits non-zero when the block did not break.

Set ``hold`` (seconds) from the block's hardness: dirt by hand breaks in ~0.6s,
stone in ~1.6s.  The server only breaks the block when it sees ``STOP``, so a
hold that is too short silently does nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import sys
import time
from typing import Any

from protobot import connect


async def _wait_loaded(bot: Any, position: tuple[int, int, int], *, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = bot.world.get_state_id(*position)
        except Exception:  # noqa: BLE001 - chunk may not have arrived yet
            state = None
        if state:
            return state
        await asyncio.sleep(0.1)
    return None


def _feet_block(bot: Any) -> tuple[int, int, int]:
    position = bot.physics_state.position
    return (
        math.floor(position.x),
        math.floor(position.y) - 1,
        math.floor(position.z),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="DigProbe")
    parser.add_argument(
        "--target",
        default=None,
        help="x,y,z of the block to break (default: the block under the player)",
    )
    parser.add_argument("--hold", type=float, default=2.0)
    parser.add_argument("--confirm-timeout", type=float, default=2.0)
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
    try:
        await bot.wait_world(timeout=20.0)
        if args.target:
            target = tuple(int(part) for part in args.target.split(","))
            if len(target) != 3:
                raise SystemExit("--target needs exactly three integers: x,y,z")
        else:
            target = _feet_block(bot)
        report["target"] = list(target)
        report["position"] = {
            axis: round(getattr(bot.physics_state.position, axis), 3)
            for axis in ("x", "y", "z")
        }

        state_before = await _wait_loaded(bot, target, timeout=15.0)
        report["state_before"] = state_before
        if not state_before:
            report["ok"] = False
            report["error"] = "no solid block at the target; is the chunk loaded?"
            return 1

        result = await bot.actions.dig(
            target, hold=args.hold, confirm_timeout=args.confirm_timeout
        )
        state_after = bot.world.get_state_id(*target)
        report.update(
            {
                "status": result.status.value,
                "confirmed": result.confirmed,
                "evidence": result.evidence,
                "packets": list(result.packets),
                "elapsed": round(result.elapsed, 3),
                "state_after": state_after,
                "ok": state_before != state_after and state_after == 0,
            }
        )
    except BaseException as error:  # noqa: BLE001 - the probe reports, never raises
        report["ok"] = False
        report["exception"] = f"{type(error).__name__}: {error}"
    finally:
        report["closed"] = bot.closed.is_set()
        report["disconnect_reason"] = bot.disconnect_reason
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
