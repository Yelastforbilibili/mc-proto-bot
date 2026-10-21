"""A tour of the library: connect, read the world, walk, dig, pick up, place, script.

Run it against a throwaway server (it breaks and puts back one block):

    python examples/tour.py --host 127.0.0.1 --username Tour

Every step prints what it asked for *and* what the server said, because the point
of these layers is that the answer comes from the server rather than from the
bot's own optimism.  Read it top to bottom: each section is one layer.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from protobot import connect
from protobot.actions import ActionStatus
from protobot.goals import TravelGoal
from protobot.tasks import BreakBlocks, PlaceBlocks, Task


def line(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 60 - len(title)))


def block_name(bot, position: tuple[int, int, int]) -> str:
    state_id = bot.world.get_state_id(*position)
    if state_id <= 0:
        return "minecraft:air"
    definition = bot.world.block_states.get(state_id)
    return definition.name if definition is not None else "minecraft:unknown"


def choose_floor_block(bot, position) -> tuple[int, int, int] | None:
    """The first solid block at foot level, one to two blocks away."""

    x, y, z = int(position.x // 1), int(position.y // 1), int(position.z // 1)
    for dx, dz in ((2, 0), (-2, 0), (0, 2), (0, -2)):
        candidate = (x + dx, y, z + dz)
        if bot.world.get_state_id(*candidate) > 0:
            return candidate
    return None


async def tour(bot, block_argument: str | None) -> int:
    version = bot.version

    line("1. connected")
    print(f"release        {version.name} (protocol {version.protocol})")
    print(f"player         {bot.username} at {bot.physics_state.position}")
    await bot.wait_world()
    print(f"chunks decoded {len(bot.world.chunks)}")

    line("2. what the shipped data tables know")
    position = bot.physics_state.position
    below = (int(position.x // 1), int(position.y // 1) - 1, int(position.z // 1))
    floor = block_name(bot, below)
    print(f"floor under you {floor}")
    print(f"hardness         {version.breaking.hardness(floor)}")
    seconds = version.breaking.seconds(floor, tool=bot.held_item)
    print(f"break time       {seconds if seconds is None else round(seconds, 3)} s")
    print(f"held item        {bot.held_item}")

    line("3. walking (bot.goals.TravelGoal)")
    start = bot.physics_state.position
    goal = TravelGoal(start.x + 4.0, start.z, tolerance=1.0, timeout=30.0)
    report = await bot.goals.run(goal, timeout=40.0)
    print(f"goal           {report.status.value} in {report.elapsed:.2f}s")
    print(f"moved          {bot.physics_state.position.x - start.x:+.2f} blocks east")

    line("4. one action at a time (bot.actions)")
    if block_argument:
        target = tuple(int(part) for part in block_argument.split(","))
    else:
        target = choose_floor_block(bot, bot.physics_state.position)
    if target is None:
        print("nothing solid within reach to dig; skipping the dig/place steps")
        return 0
    victim = block_name(bot, target)
    print(f"target         {target} = {victim}")
    result = await bot.actions.dig(target)
    print(f"dig            {result.status.value} in {result.elapsed:.2f}s")
    print(f"evidence       {result.evidence}")
    if result.status is not ActionStatus.CONFIRMED:
        print("the server did not confirm the break; stopping here")
        return 1

    line("5. picking the drop up (bot.pick_up)")
    pickup = await bot.pick_up(count=1, radius=6.0, timeout=20.0)
    print(f"collected      {pickup.collected} in {pickup.seconds:.2f}s")
    print(f"ok             {pickup.ok} ({pickup.reason})")

    line("6. putting it back (bot.actions.place_block)")
    slot = bot.player_menu.find(victim, where="hotbar")
    print(f"hotbar slot    {slot}")
    if slot is not None:
        await bot.select_hotbar_slot(slot - 36)
        print(f"holding        {bot.held_item}")
        placed = await bot.actions.place_block(target)
        print(f"place          {placed.status.value}")
        print(f"evidence       {placed.evidence}")

    line("7. scripting the same thing (bot.tasks)")
    task = Task("tour")
    task.add(BreakBlocks([target], hold=1.5))
    task.add(PlaceBlocks([(target, victim)]))
    task_report = await bot.tasks.run(task, timeout=120.0)
    print(f"task           {task_report}")
    for step in task_report.steps:
        print(
            f"  {step.name:<22} {step.status.value:<6} "
            f"{step.elapsed:5.2f}s {step.detail}"
        )

    line("8. where the rest lives")
    print("containers  bot.menu.open_block((x, y, z)) / deposit / craft / enchant")
    print("combat      bot.actions.attack_looked_at()")
    print("watching    bot.register_as_bot(), then /see <name> in game")
    print("docs        docs/architecture.md · containers.md · automation.md")
    return 0 if task_report.ok else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="Walk through ProtoBot's layers")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--username", default="Tour")
    parser.add_argument("--version", default="26.3")
    parser.add_argument(
        "--block",
        default=None,
        help="block to dig and put back as x,y,z (default: a solid block 2 away)",
    )
    args = parser.parse_args()

    bot = await connect(
        args.host, port=args.port, username=args.username, version=args.version
    )
    try:
        return await tour(bot, args.block)
    finally:
        await bot.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
