"""Live check: let the goal layer walk over and break a real block.

``tools/live_dig.py`` proves the *interaction* layer; this proves the layer above
it, where the caller states an objective instead of a packet sequence:

    reach (10.5, 4.5) -> break (8, -60, 4)

The runner drives the loop, so the interesting output is the :class:`GoalReport`:
whether the goal reached a server-confirmed state, how long it took, how many
ticks ran, and the goal's own explanation when it did not.

Usage::

    uv run python tools/live_goal.py --target 8,-60,4

The block has to be solid before the run; a flat test world gives you one at
``x,z`` a few blocks away after ``/setblock``.  Prints a JSON report and exits
non-zero when the block is still there.
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
from protobot.goals import BreakBlockGoal, GoalStatus, ReachGoal, SequenceGoal


def _feet_block(bot: Any) -> tuple[int, int, int]:
    position = bot.physics_state.position
    return (
        math.floor(position.x),
        math.floor(position.y) - 1,
        math.floor(position.z),
    )


def _stand_point(target: tuple[int, int, int]) -> tuple[float, float]:
    """A spot two and a half blocks away from ``target`` on the x axis."""

    return (target[0] + 2.5, target[2] + 0.5)


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


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="GoalProbe")
    parser.add_argument(
        "--target",
        default=None,
        help="x,y,z of the block to break (default: the block under the player)",
    )
    parser.add_argument("--hold", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    report: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "version": args.version,
        "username": args.username,
    }
    goals_seen: list[str] = []

    bot = await connect(
        args.host,
        port=args.port,
        username=args.username,
        version=args.version,
        timeout=20.0,
    )
    bot.events.on("goal_start", lambda goal: goals_seen.append(f"start:{goal.name}"))
    bot.events.on("goal_done", lambda goal_report: goals_seen.append(f"done:{goal_report.goal}:{goal_report.status.value}"))
    try:
        await bot.wait_world(timeout=20.0)
        if args.target:
            target = tuple(int(part) for part in args.target.split(","))
            if len(target) != 3:
                raise SystemExit("--target needs exactly three integers: x,y,z")
        else:
            target = _feet_block(bot)
        report["target"] = list(target)
        report["start"] = {
            axis: round(getattr(bot.physics_state.position, axis), 3)
            for axis in ("x", "y", "z")
        }

        state_before = await _wait_loaded(bot, target, timeout=15.0)
        report["state_before"] = state_before
        if not state_before:
            report["ok"] = False
            report["error"] = "no solid block at the target; is the chunk loaded?"
            return 1

        stand_x, stand_z = _stand_point(target)
        goal = SequenceGoal(
            ReachGoal(stand_x, stand_z, tolerance=1.0, timeout=60.0),
            BreakBlockGoal(target, hold=args.hold, confirm_timeout=2.0),
            name="walk-then-break",
        )
        report["sequence"] = goal.describe()
        started = time.monotonic()
        result = await bot.goals.run(goal, timeout=args.timeout)
        state_after = bot.world.get_state_id(*target)
        report.update(
            {
                "goal": result.goal,
                "status": result.status.value,
                "ticks": result.attempts,
                "elapsed": round(result.elapsed, 3),
                "detail": result.detail,
                "state_after": state_after,
                "end": {
                    axis: round(getattr(bot.physics_state.position, axis), 3)
                    for axis in ("x", "y", "z")
                },
                "wall_clock": round(time.monotonic() - started, 3),
                "ok": result.status is GoalStatus.DONE and state_after == 0,
            }
        )
    except BaseException as error:  # noqa: BLE001 - the probe reports, never raises
        report["ok"] = False
        report["exception"] = f"{type(error).__name__}: {error}"
    finally:
        report["goals"] = goals_seen[-6:]
        report["disconnect_reason"] = bot.disconnect_reason
        terminal = getattr(bot, "_terminal_error", None)
        if terminal is not None:
            report["terminal_error"] = f"{type(terminal).__name__}: {terminal}"
        with contextlib.suppress(Exception):
            await bot.close()
        print(json.dumps(report, indent=2, default=str), flush=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
