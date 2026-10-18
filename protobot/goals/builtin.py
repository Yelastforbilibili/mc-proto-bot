"""Goals the interaction layer can actually satisfy.

Every goal here is expressed in terms of what the *server* will see: reaching a
position sends ordinary movement, breaking a block sends the dig packets, and
success is read back from the decoded world rather than assumed from a write
succeeding.

The two rules that keep these simple:

* slow operations (navigation, a dig) are a single tick, because the runner
  checks the deadline between ticks and lying about progress is worse than a
  long tick;
* the world is never mutated locally.  ``is_reached`` reads the server's answer,
  so a goal that breaks the same block twice is harmless.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from ..actions import ActionStatus, BlockFace
from ..containers.errors import ContainerError
from ..crafting.recipes import Recipe
from ..navigation import NavigationTimeout
from ..physics import Vec3
from .base import Goal, GoalError, GoalStatus, block_center

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import Bot

__all__ = [
    "AttackEntityGoal",
    "BreakBlockGoal",
    "PlaceBlockGoal",
    "ReachGoal",
    "WaitGoal",
]


class ReachGoal(Goal):
    """Stand within ``tolerance`` blocks of a point."""

    name = "reach"

    def __init__(
        self,
        x: float,
        z: float,
        *,
        y: float | None = None,
        tolerance: float = 1.0,
        timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self.x = x
        self.z = z
        self.y = y
        self.tolerance = tolerance
        self.timeout = timeout

    def is_reached(self, bot: Bot) -> bool:
        if self._horizontal_distance(bot, self.x, self.z) > self.tolerance:
            return False
        if self.y is None:
            return True
        return abs(self.y - bot.physics_state.position.y) <= 1.0

    async def tick(self, bot: Bot) -> GoalStatus:
        try:
            await bot.navigate_to(
                self.x,
                self.z,
                y=self.y,
                tolerance=self.tolerance,
                timeout=self.timeout,
            )
        except (NavigationTimeout, TimeoutError, RuntimeError) as error:
            self.last_detail = f"{type(error).__name__}: {error}"
            return GoalStatus.RUNNING
        return GoalStatus.DONE

    def describe(self) -> str:
        return f"reach ({self.x:g}, {self.y if self.y is None else format(self.y, 'g')}, {self.z:g})"


class BreakBlockGoal(Goal):
    """Break one block and wait until the world says it is air.

    ``hold`` is how long the dig packets are held down.  The server only breaks
    the block when the accumulated progress reaches its threshold, so ``hold``
    must exceed the block's hand-break time: dirt needs ~0.6 s, stone ~1.5 s,
    wood ~2 s.  :meth:`protobot.actions.Actions.break_time_estimate` converts a
    hardness value into that duration.
    """

    name = "break"

    def __init__(
        self,
        position: tuple[int, int, int],
        *,
        hold: float = 2.0,
        reach: float = 4.5,
        confirm_timeout: float = 1.0,
        face: BlockFace | None = None,
    ) -> None:
        super().__init__()
        self.position = (int(position[0]), int(position[1]), int(position[2]))
        self.hold = hold
        self.reach = reach
        self.confirm_timeout = confirm_timeout
        self.face = face

    def is_reached(self, bot: Bot) -> bool:
        return self._world_state(bot, self.position) == 0

    async def tick(self, bot: Bot) -> GoalStatus:
        state = self._world_state(bot, self.position)
        if state == -1:
            self.last_detail = "waiting for the chunk to load"
            return GoalStatus.RUNNING
        if state == 0:
            return GoalStatus.DONE
        centre = block_center(*self.position)
        if self._eye_distance(bot, centre) > self.reach:
            await _approach(bot, centre, self.reach, self)
            return GoalStatus.RUNNING
        result = await bot.actions.dig(
            self.position,
            face=self.face,
            hold=self.hold,
            confirm_timeout=self.confirm_timeout,
        )
        if result.status is ActionStatus.REJECTED:
            raise GoalError(f"server rejected digging {self.position}: {result.evidence}")
        self.last_detail = result.evidence
        if self._world_state(bot, self.position) == 0:
            return GoalStatus.DONE
        return GoalStatus.RUNNING

    def describe(self) -> str:
        return f"break {self.position}"


class PlaceBlockGoal(Goal):
    """Put a block into an empty position, selecting the item first.

    The click lands on the neighbour behind ``face``, exactly like a player
    placing a block: :class:`~protobot.actions.interactions.Actions.place_block`
    derives the clicked position from ``position - face.step``.
    """

    name = "place"

    def __init__(
        self,
        position: tuple[int, int, int],
        *,
        item: str | None = None,
        face: BlockFace = BlockFace.UP,
        reach: float = 4.5,
    ) -> None:
        super().__init__()
        self.position = (int(position[0]), int(position[1]), int(position[2]))
        self.item = item
        self.face = face
        self.reach = reach

    def is_reached(self, bot: Bot) -> bool:
        return self._world_state(bot, self.position) > 0

    async def tick(self, bot: Bot) -> GoalStatus:
        if self._world_state(bot, self.position) == -1:
            self.last_detail = "waiting for the chunk to load"
            return GoalStatus.RUNNING
        if self.item is not None:
            slot = _find_hotbar_item(bot, self.item)
            if slot is None:
                raise GoalError(f"no {self.item} in the hotbar to place")
            if bot.selected_hotbar_slot != slot:
                await bot.select_hotbar_slot(slot)
        dx, dy, dz = self.face.step
        clicked = (self.position[0] - dx, self.position[1] - dy, self.position[2] - dz)
        centre = block_center(*clicked)
        if self._eye_distance(bot, centre) > self.reach:
            await _approach(bot, centre, self.reach, self)
            return GoalStatus.RUNNING
        result = await bot.actions.place_block(self.position, face=self.face)
        self.last_detail = result.evidence
        if result.status is ActionStatus.REJECTED:
            raise GoalError(f"cannot place at {self.position}: {result.evidence}")
        if self._world_state(bot, self.position) > 0:
            return GoalStatus.DONE
        return GoalStatus.RUNNING

    def describe(self) -> str:
        suffix = f" with {self.item}" if self.item else ""
        return f"place {self.position}{suffix}"


class AttackEntityGoal(Goal):
    """Attack an entity until it disappears from the client's entity list."""

    name = "attack"

    def __init__(self, entity_id: int, *, reach: float = 3.0) -> None:
        super().__init__()
        self.entity_id = entity_id
        self.reach = reach

    def is_reached(self, bot: Bot) -> bool:
        return self.entity_id not in bot.entities

    async def tick(self, bot: Bot) -> GoalStatus:
        entity = bot.entities.get(self.entity_id)
        if entity is None:
            return GoalStatus.DONE
        if self._horizontal_distance(bot, entity.x, entity.z) > self.reach:
            await _approach(bot, Vec3(entity.x, entity.y, entity.z), self.reach, self)
            return GoalStatus.RUNNING
        result = await bot.actions.attack(self.entity_id)
        self.last_detail = result.evidence or "attack sent"
        return GoalStatus.RUNNING

    def describe(self) -> str:
        return f"attack entity {self.entity_id}"


class WaitGoal(Goal):
    """Do nothing for ``seconds``; useful between steps of a script."""

    name = "wait"

    def __init__(self, seconds: float) -> None:
        super().__init__()
        if seconds < 0.0:
            raise ValueError("seconds cannot be negative")
        self.seconds = seconds
        self._deadline: float | None = None

    def reset(self) -> None:
        super().reset()
        self._deadline = None

    def is_reached(self, bot: Bot) -> bool:
        if self._deadline is None:
            self._deadline = time.monotonic() + self.seconds
            return self.seconds == 0.0
        return time.monotonic() >= self._deadline

    async def tick(self, bot: Bot) -> GoalStatus:
        await asyncio.sleep(min(0.05, self.seconds))
        return GoalStatus.DONE if self.is_reached(bot) else GoalStatus.RUNNING

    def describe(self) -> str:
        return f"wait {self.seconds:g}s"


class CraftGoal(Goal):
    """Craft copies of a recipe, using the player grid or a crafting table.

    Progress is what the *server* handed over: every craft goes through the
    result slot and the goal only counts items the server actually put in the
    inventory, so a rejected or dropped click cannot look like success.
    """

    name = "craft"

    def __init__(
        self,
        recipe: Recipe,
        *,
        times: int = 1,
        table: tuple[int, int, int] | None = None,
        reach: float = 4.5,
        timeout: float = 2.0,
    ) -> None:
        super().__init__()
        if times <= 0:
            raise ValueError("times must be positive")
        self.recipe = recipe
        self.times = times
        self.table = (
            None
            if table is None
            else (int(table[0]), int(table[1]), int(table[2]))
        )
        self.reach = reach
        self.timeout = timeout
        self._crafted_up_to_now = 0

    def reset(self) -> None:
        super().reset()
        self._crafted_up_to_now = 0

    @property
    def target(self) -> int:
        return self.recipe.count * self.times

    def is_reached(self, bot: Bot) -> bool:
        return self._crafted_up_to_now >= self.target

    async def tick(self, bot: Bot) -> GoalStatus:
        made = self._crafted_up_to_now
        remaining = self.target - made
        if remaining <= 0:
            return GoalStatus.DONE
        crafts = -(-remaining // self.recipe.count)
        menu = await self._open(bot)
        if menu is None:
            return GoalStatus.RUNNING
        missing = menu.missing_ingredients(self.recipe)
        if missing:
            raise GoalError(
                f"missing {', '.join(f'{count}x {item}' for item, count in missing.items())} "
                f"for {self.recipe.describe()}"
            )
        closes = self.table is not None
        try:
            crafted = await menu.craft(self.recipe, crafts, timeout=self.timeout)
        finally:
            if closes:
                await menu.close()
                await asyncio.sleep(0.05)
        self._crafted_up_to_now += crafted
        self.last_detail = (
            f"crafted {self._crafted_up_to_now}/{self.target} {self.recipe.output}"
            f" ({crafts} grid fill{'s' if crafts != 1 else ''})"
        )
        if crafted <= 0:
            raise GoalError(
                f"the server crafted nothing from {self.recipe.describe()}"
            )
        return (
            GoalStatus.DONE
            if self._crafted_up_to_now >= self.target
            else GoalStatus.RUNNING
        )

    async def _open(self, bot: Bot) -> object | None:
        if self.recipe.size <= 2:
            return bot.player_menu
        if self.table is None:
            raise GoalError(
                f"{self.recipe.describe()} needs a 3x3 grid: pass the position of "
                "a crafting table as table=(x, y, z)"
            )
        current = bot.menu
        if current.is_open and current.identifier == "minecraft:crafting":
            return current
        centre = block_center(*self.table)
        if self._eye_distance(bot, centre) > self.reach:
            await _approach(bot, centre, self.reach, self)
            return None
        try:
            return await current.open_block(self.table, reach=self.reach)
        except ContainerError as error:
            self.last_detail = f"open crafting table at {self.table}: {error}"
            await _approach(bot, centre, self.reach, self)
            return None

    def describe(self) -> str:
        where = f" at {self.table}" if self.table is not None else ""
        return (
            f"craft {self.recipe.count * self.times}x {self.recipe.output}"
            f"{where}"
        )


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


async def _approach(bot: Bot, point: Vec3, reach: float, goal: Goal) -> None:
    """Walk to within ``reach`` of ``point``; failures are reported, not raised."""

    try:
        await bot.navigate_to(
            point.x,
            point.z,
            tolerance=max(0.6, reach * 0.5),
            timeout=20.0,
        )
    except (NavigationTimeout, TimeoutError, RuntimeError) as error:
        goal.last_detail = f"approach: {type(error).__name__}: {error}"


def _find_hotbar_item(bot: Bot, item: str) -> int | None:
    """Zero-based hotbar slot holding ``item``, or ``None``.

    Vanilla hotbar slots live at inventory indices 36..44, which is why the
    index is offset here rather than in the goal.
    """

    wanted = item if ":" in item else f"minecraft:{item}"
    for slot in range(9):
        stack = bot.get_inventory_item(36 + slot)
        if stack.count <= 0:
            continue
        identifier = stack.identifier or ""
        if identifier == wanted or identifier == item:
            return slot
    return None
