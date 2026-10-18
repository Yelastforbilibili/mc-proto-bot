"""Shared test doubles for the automation layers.

No network and no server: the fake world answers block lookups, the fake action
layer mutates it the way a server eventually would, and the fake navigation
moves the player instantly.  That is enough to pin the decisions the layers make
— which goal is ticked next, when a result counts as confirmed, how a retry is
reported — without pretending to be a Minecraft server.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

from protobot.actions import ActionStatus, ActionResult
from protobot.events import EventBus
from protobot.goals import GoalRunner
from protobot.physics import Vec3
from protobot.protocol.versions import get_version
from protobot.state import ItemStack
from protobot.tasks import TaskRunner


class FakeWorld:
    def __init__(self, states=None, *, unknown=False) -> None:
        self.states = dict(states or {})
        self.unknown = unknown

    def get_state_id(self, x: int, y: int, z: int) -> int:
        if self.unknown:
            return -1
        return self.states.get((x, y, z), 0)

    def set_state_id(self, x: int, y: int, z: int, state_id: int) -> None:
        self.states[(x, y, z)] = state_id


class FakeActions:
    """Records calls and applies their effect to the fake world."""

    def __init__(self, bot: "FakeBot") -> None:
        self.bot = bot
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    async def dig(self, position, *, face=None, hold=2.0, confirm_timeout=1.0, **kwargs):
        self._record("dig", position, face=face, hold=hold)
        if self.bot.dig_status is ActionStatus.REJECTED:
            return ActionResult(
                "dig", ActionStatus.REJECTED, packets=(41,), evidence="rejected"
            )
        self.bot.world.set_state_id(*position, 0)
        return ActionResult(
            "dig", ActionStatus.CONFIRMED, packets=(41, 41), evidence="world state 1 -> 0"
        )

    async def place_block(self, position, *, face=None, **kwargs):
        self._record("place_block", position, face=face)
        if self.bot.place_state is None:
            return ActionResult(
                "place_block", ActionStatus.REJECTED, packets=(44,), evidence="no neighbour"
            )
        self.bot.world.set_state_id(*position, self.bot.place_state)
        return ActionResult("place_block", ActionStatus.CONFIRMED, packets=(44,))

    async def attack(self, entity_id, *, swing=True):
        self._record("attack", entity_id)
        if self.bot.remove_on_attack:
            self.bot.entities.pop(entity_id, None)
        return ActionResult("attack", ActionStatus.CONFIRMED, packets=(1,))


class FakeBot:
    """The slice of ``Bot`` the goal and task layers touch."""

    def __init__(self, *, states=None, unknown=False, position=(0.5, 64.0, 0.5)) -> None:
        self.version = get_version("26.3")
        self.world = FakeWorld(states, unknown=unknown)
        self.events = EventBus()
        self.entities: dict[int, SimpleNamespace] = {}
        self.inventory: dict[int, ItemStack] = {}
        self.physics_state = SimpleNamespace(
            position=Vec3(*position),
            eye_height=1.62,
            yaw=0.0,
            pitch=0.0,
        )
        self.dig_status = ActionStatus.CONFIRMED
        self.place_state: int | None = 1
        self.remove_on_attack = True
        self.navigation: list[tuple[float, float]] = []
        self._actions = FakeActions(self)
        self._goals = GoalRunner(self)
        self._tasks = TaskRunner(self)
        self._selected = 0

    # -- action / automation accessors ----------------------------------

    @property
    def actions(self) -> FakeActions:
        return self._actions

    @property
    def goals(self) -> GoalRunner:
        return self._goals

    @property
    def tasks(self) -> TaskRunner:
        return self._tasks

    # -- navigation -----------------------------------------------------

    async def navigate_to(self, x, z, *, y=None, tolerance=0.3, timeout=60.0, **kwargs):
        self.navigation.append((x, z))
        state = self.physics_state
        position = state.position
        state.position = Vec3(x, y if y is not None else position.y, z)
        return state

    # -- inventory ------------------------------------------------------

    def get_inventory_item(self, slot: int) -> ItemStack:
        return self.inventory.get(slot, ItemStack())

    @property
    def selected_hotbar_slot(self) -> int:
        return self._selected

    async def select_hotbar_slot(self, slot: int) -> None:
        self._selected = slot

    # -- helpers --------------------------------------------------------

    def place_item_in_hotbar(self, identifier: str, slot: int = 0) -> None:
        self.inventory[36 + slot] = ItemStack(count=64, item_id=1, identifier=identifier)

    def distance_to(self, x: float, z: float) -> float:
        position = self.physics_state.position
        return math.hypot(x - position.x, z - position.z)
