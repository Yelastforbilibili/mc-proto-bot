"""Tests for the container layer: layouts, menu queries and click semantics.

The fake server below reimplements the parts of ``AbstractContainerMenu.clicked``
that this layer drives (pickup, quick move, swap, throw, pickup-all).  Its rules
were checked against a real Paper 26.3 server: a click with an empty changed-slot
set and an empty carried stack is still applied, which is what lets the layer
send plain clicks and confirm them by watching the window's revision.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from protobot.containers import (
    ClickRejected,
    CLICK_MODES,
    ContainerError,
    Menu,
    MenuNotOpen,
    PLAYER_CONTAINER_ID,
    layout_for,
    player_menu,
)
from protobot.containers import menu as menu_module
from protobot.physics import Vec3
from protobot.protocol.versions import get_version
from protobot.state import ContainerState, ItemStack

# The fakes below apply a click synchronously, so the settle window that keeps
# a real server's multi-packet reply in one piece would only add latency here.
_QUIET_SECONDS = menu_module.QUIET_SECONDS


def setUpModule() -> None:
    menu_module.QUIET_SECONDS = 0.0


def tearDownModule() -> None:
    menu_module.QUIET_SECONDS = _QUIET_SECONDS

DIRT = "minecraft:dirt"
STONE = "minecraft:stone"
GRASS = "minecraft:grass_block"


def _stack(identifier: str | None, count: int = 1) -> ItemStack:
    if identifier is None or count == 0:
        return ItemStack()
    return ItemStack(count=count, item_id=0, identifier=identifier)


class FakeServer:
    """A window that answers clicks the way the vanilla menu does."""

    def __init__(
        self,
        identifier: str,
        *,
        container_id: int = 1,
        menu_type_id: int = 2,
        slot_count: int | None = None,
    ) -> None:
        layout = layout_for(identifier, slot_count)
        self.layout = layout
        self.state = ContainerState(
            container_id=container_id, menu_type_id=menu_type_id
        )
        self.state.slot_count = layout.slot_count
        self.state.state_id = 1
        self.clicks: list[tuple[int, int, int]] = []

    # -- helpers ---------------------------------------------------------
    def set(self, slot: int, identifier: str | None, count: int = 1) -> None:
        if identifier is None or count == 0:
            self.state.items.pop(slot, None)
        else:
            self.state.items[slot] = _stack(identifier, count)
        self.state.revision += 1
        self.state.slot_revision += 1

    def progress(self) -> None:
        """A ``container_set_data`` update: it must not confirm a click."""

        self.state.revision += 1

    def get(self, slot: int) -> tuple[str | None, int]:
        stack = self.state.items.get(slot, ItemStack())
        return (stack.identifier, stack.count)

    def cursor(self) -> ItemStack:
        return self.state.carried_item

    def _bump(self) -> None:
        self.state.revision += 1
        self.state.slot_revision += 1
        self.state.state_id += 1

    def _take(self, slot: int, amount: int) -> ItemStack:
        stack = self.state.items.get(slot, ItemStack())
        if stack.empty:
            return ItemStack()
        amount = min(amount, stack.count)
        taken = _stack(stack.identifier, amount)
        left = stack.count - amount
        if left:
            self.state.items[slot] = _stack(stack.identifier, left)
        else:
            self.state.items.pop(slot, None)
        return taken

    def _put(self, slot: int, stack: ItemStack) -> None:
        if stack.empty:
            self.state.items.pop(slot, None)
        else:
            self.state.items[slot] = stack

    # -- click handling --------------------------------------------------
    def click(self, slot: int, button: int, mode: int) -> bool:
        self.clicks.append((slot, button, mode))
        if mode == 0:
            return self._pickup(slot, button)
        if mode == 1:
            return self._quick_move(slot)
        if mode == 2:
            return self._swap(slot, button)
        if mode == 3:
            return self._throw(slot, button)
        if mode == 5:
            return self._pickup_all(slot)
        return False

    def _pickup(self, slot: int, button: int) -> bool:
        if not 0 <= slot < self.state.slot_count:
            return False
        carried = self.state.carried_item
        stack = self.state.items.get(slot, ItemStack())
        if button == 0:
            if carried.empty:
                if stack.empty:
                    return False
                self.state.carried_item = self._take(slot, stack.count)
            elif stack.empty:
                self._put(slot, carried)
                self.state.carried_item = ItemStack()
            elif stack.identifier == carried.identifier:
                room = 64 - stack.count
                moved = min(room, carried.count)
                if not moved:
                    return False
                self._put(slot, _stack(stack.identifier, stack.count + moved))
                self.state.carried_item = (
                    _stack(carried.identifier, carried.count - moved)
                    if carried.count > moved
                    else ItemStack()
                )
            else:
                self._put(slot, carried)
                self.state.carried_item = stack
        elif button == 1:
            if carried.empty:
                if stack.empty:
                    return False
                half = (stack.count + 1) // 2
                self.state.carried_item = self._take(slot, half)
            elif stack.empty:
                self._put(slot, _stack(carried.identifier, 1))
                self.state.carried_item = (
                    _stack(carried.identifier, carried.count - 1)
                    if carried.count > 1
                    else ItemStack()
                )
            elif stack.identifier == carried.identifier and stack.count < 64:
                self._put(slot, _stack(stack.identifier, stack.count + 1))
                self.state.carried_item = (
                    _stack(carried.identifier, carried.count - 1)
                    if carried.count > 1
                    else ItemStack()
                )
            else:
                return False
        else:
            return False
        self._bump()
        return True

    def _slots_in(self, name: str) -> tuple[int, ...]:
        return self.layout.slots(name)

    def _quick_move(self, slot: int) -> bool:
        if not 0 <= slot < self.state.slot_count:
            return False
        stack = self.state.items.get(slot, ItemStack())
        if stack.empty:
            return False
        if slot in self.layout.player:
            targets = self.layout.container
        else:
            targets = self.layout.player
        remaining = stack.count
        for target in targets:
            if not remaining:
                break
            current = self.state.items.get(target, ItemStack())
            if current.empty:
                self._put(target, _stack(stack.identifier, remaining))
                remaining = 0
                break
            if current.identifier != stack.identifier or current.count >= 64:
                continue
            moved = min(64 - current.count, remaining)
            self._put(target, _stack(current.identifier, current.count + moved))
            remaining -= moved
        if remaining == stack.count:
            return False
        self._put(
            slot,
            _stack(stack.identifier, remaining) if remaining else ItemStack(),
        )
        self._bump()
        return True

    def _swap(self, slot: int, button: int) -> bool:
        if not 0 <= slot < self.state.slot_count:
            return False
        hotbar = self.layout.hotbar
        if button == 40:
            if "offhand" not in self.layout.groups:
                return False
            other = self.layout.slot("offhand")
        elif 0 <= button < len(hotbar):
            other = hotbar[button]
        else:
            return False
        mine = self.state.items.get(slot, ItemStack())
        theirs = self.state.items.get(other, ItemStack())
        self._put(slot, theirs)
        self._put(other, mine)
        self._bump()
        return True

    def _throw(self, slot: int, button: int) -> bool:
        if not 0 <= slot < self.state.slot_count:
            return False
        stack = self.state.items.get(slot, ItemStack())
        if stack.empty:
            return False
        self._take(slot, stack.count if button else 1)
        self._bump()
        return True

    def _pickup_all(self, slot: int) -> bool:
        if not 0 <= slot < self.state.slot_count:
            return False
        carried = self.state.carried_item
        if carried.empty:
            return False
        moved = False
        for index in self.layout.all:
            stack = self.state.items.get(index, ItemStack())
            if stack.empty or stack.identifier != carried.identifier:
                continue
            self._put(index, ItemStack())
            carried = _stack(carried.identifier, carried.count + stack.count)
            moved = True
        if not moved:
            return False
        self.state.carried_item = carried
        self._bump()
        return True


class FakeActions:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot
        self.looks: list[Vec3] = []
        self.used: list[tuple[int, tuple[int, int, int]]] = []

    async def look_at(self, point) -> tuple[float, float]:
        self.looks.append(point)
        return (0.0, 0.0)

    async def use_item_on(self, position, **kwargs):
        self.used.append((0, position))
        return SimpleNamespace(ok=True)

    async def interact(self, entity_id, **kwargs):
        self.used.append((entity_id, None))
        return SimpleNamespace(ok=True)


class FakeBot:
    def __init__(
        self,
        position: tuple[float, float, float] = (0.5, 64.0, 0.5),
    ) -> None:
        self.version = get_version("26.3")
        self.containers: dict[int, ContainerState] = {}
        self._active_container_id: int | None = None
        self.closed: list[int] = []
        self.physics_state = SimpleNamespace(
            position=Vec3(*position), eye_height=1.62, yaw=0.0, pitch=0.0
        )
        self.actions = FakeActions(self)
        self._menu = None
        self.server: FakeServer | None = None

    @property
    def active_container(self) -> ContainerState | None:
        if self._active_container_id is None:
            return None
        return self.containers.get(self._active_container_id)

    async def click_container(self, slot: int, *, button: int = 0, click_type: str = "pickup", **kwargs) -> bool:
        assert self.server is not None
        if isinstance(click_type, str):
            mode = {
                "pickup": 0,
                "quick_move": 1,
                "swap": 2,
                "throw": 3,
                "quick_craft": 4,
                "pickup_all": 5,
                "quick_craft_all": 6,
            }[click_type]
        else:
            mode = int(click_type)
        return self.server.click(slot, button, mode)

    async def close_container(self) -> None:
        if self._active_container_id is not None:
            self.closed.append(self._active_container_id)
        self._active_container_id = None

    @property
    def menu(self) -> Menu:
        return Menu(self)

    @property
    def player_menu(self) -> Menu:
        return player_menu(self)

    # -- wiring helpers --------------------------------------------------
    def open(self, server: FakeServer) -> FakeServer:
        self.server = server
        self.containers[server.state.container_id] = server.state
        if server.state.container_id != PLAYER_CONTAINER_ID:
            self._active_container_id = server.state.container_id
        return server

    def open_player(self, items: dict[int, tuple[str, int]] | None = None) -> FakeServer:
        server = FakeServer(
            "minecraft:inventory",
            container_id=PLAYER_CONTAINER_ID,
            menu_type_id=0,
        )
        self.containers[PLAYER_CONTAINER_ID] = server.state
        for slot, (identifier, count) in (items or {}).items():
            server.set(slot, identifier, count)
        return server


class LayoutTests(unittest.TestCase):
    def test_chest_layout(self) -> None:
        layout = layout_for("minecraft:generic_9x3")
        self.assertEqual(layout.slot_count, 63)
        self.assertEqual(layout.container, tuple(range(27)))
        self.assertEqual(layout.player, tuple(range(27, 63)))
        self.assertEqual(layout.main, tuple(range(27, 54)))
        self.assertEqual(layout.hotbar, tuple(range(54, 63)))
        self.assertFalse(layout.derived)

    def test_furnace_layout(self) -> None:
        layout = layout_for("minecraft:furnace")
        self.assertEqual(layout.slot_count, 39)
        self.assertEqual(layout.container, (0, 1))
        self.assertEqual(layout.result, (2,))
        self.assertEqual(layout.player, tuple(range(3, 39)))

    def test_crafter_keeps_its_preview_slot_out_of_the_grid(self) -> None:
        layout = layout_for("minecraft:crafter_3x3")
        self.assertEqual(layout.slot_count, 46)
        self.assertEqual(layout.slots("grid"), tuple(range(9)))
        self.assertEqual(layout.container, tuple(range(9)))
        self.assertEqual(layout.result, (9,))
        self.assertEqual(layout.player, tuple(range(10, 46)))

    def test_player_layout_keeps_armour_out_of_the_player_group(self) -> None:
        layout = layout_for("minecraft:inventory")
        self.assertEqual(layout.result, (0,))
        self.assertEqual(layout.slots("grid"), (1, 2, 3, 4))
        self.assertEqual(layout.slots("armor"), (5, 6, 7, 8))
        self.assertEqual(layout.player, tuple(range(9, 45)))
        self.assertEqual(layout.slots("offhand"), (45,))
        self.assertEqual(layout.container, ())

    def test_unknown_menu_falls_back_to_a_derived_layout(self) -> None:
        layout = layout_for("minecraft:something_new", 45)
        self.assertTrue(layout.derived)
        self.assertEqual(layout.container, tuple(range(9)))
        self.assertEqual(layout.player, tuple(range(9, 45)))

    def test_mismatched_slot_count_derives_instead(self) -> None:
        layout = layout_for("minecraft:generic_9x3", 36)
        self.assertTrue(layout.derived)
        self.assertEqual(layout.container, ())

    def test_slot_helpers(self) -> None:
        layout = layout_for("minecraft:crafting")
        self.assertEqual(layout.slot("result"), 0)
        self.assertEqual(layout.slots("grid"), tuple(range(1, 10)))
        self.assertEqual(layout.player, tuple(range(10, 46)))
        self.assertEqual(layout.name_of(2), "grid")
        with self.assertRaises(KeyError):
            layout.slot("fuel")
        with self.assertRaises(ValueError):
            layout.slot("grid")
        self.assertIn("container", layout.describe())


class MenuQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = FakeBot()
        self.server = self.bot.open(FakeServer("minecraft:generic_9x3"))
        self.server.set(0, DIRT, 10)
        self.server.set(1, STONE, 3)
        self.server.set(27, DIRT, 5)
        self.menu = self.bot.menu

    def test_identity(self) -> None:
        self.assertTrue(self.menu.is_open)
        self.assertEqual(self.menu.container_id, 1)
        self.assertEqual(self.menu.identifier, "minecraft:generic_9x3")
        self.assertEqual(self.menu.slot_count, 63)
        self.assertEqual(self.menu.state_id, 1)

    def test_counts_and_find(self) -> None:
        self.assertEqual(self.menu.count(DIRT, where="container"), 10)
        self.assertEqual(self.menu.count(DIRT, where="player"), 5)
        self.assertEqual(self.menu.count(DIRT), 15)
        self.assertEqual(self.menu.find(DIRT, where="container"), 0)
        self.assertEqual(self.menu.find(STONE), None)
        self.assertEqual(self.menu.free(where="container"), 2)
        self.assertEqual(self.menu.free(DIRT, where="container"), 2)

    def test_slot_views(self) -> None:
        self.assertEqual(self.menu.item(0).identifier, DIRT)
        self.assertEqual(self.menu[0].count, 10)
        self.assertTrue(self.menu.item(5).empty)
        self.assertEqual(self.menu.slots("container")[:2], (0, 1))
        self.assertIn("generic_9x3", self.menu.describe())

    def test_missing_menu_reports_closed(self) -> None:
        self.bot._active_container_id = None
        with self.assertRaises(MenuNotOpen):
            Menu(self.bot).state()

    def test_player_menu_is_the_inventory(self) -> None:
        player = self.bot.open_player({9: (DIRT, 7)})
        menu = self.bot.player_menu
        self.assertEqual(menu.container_id, 0)
        self.assertTrue(menu.is_open)
        self.assertEqual(menu.count(DIRT), 7)
        self.assertEqual(player.get(9), (DIRT, 7))


class ClickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = FakeBot()
        self.server = self.bot.open(FakeServer("minecraft:generic_9x3"))
        self.menu = self.bot.menu

    def test_pickup_changes_the_window_and_bumps_the_revision(self) -> None:
        self.server.set(0, DIRT, 4)
        revision = self.menu.revision
        self.assertTrue(
            asyncio.run(self.menu.pickup(0))
        )
        self.assertEqual(self.menu.carried.identifier, DIRT)
        self.assertEqual(self.menu.carried.count, 4)
        self.assertTrue(self.menu.item(0).empty)
        self.assertGreater(self.menu.revision, revision)
        self.assertEqual(self.server.clicks, [(0, 0, 0)])

    def test_rejected_click_returns_false_instead_of_hanging(self) -> None:
        self.assertFalse(
            asyncio.run(self.menu.click(30, settle=True, timeout=0.05))
        )

    def test_progress_updates_do_not_confirm_a_click(self) -> None:
        # A furnace streams container_set_data; those bump ``revision`` only,
        # and waiting on that counter made a move look finished before the slot
        # update arrived (the stone then ended up stuck on the cursor).
        revision = self.menu.slot_revision
        self.server.progress()
        self.assertGreater(self.menu.revision, revision)
        self.assertEqual(self.menu.slot_revision, revision)
        self.assertFalse(asyncio.run(self.menu.wait_for_update(revision, timeout=0.05)))

    def test_quick_move_and_swap_and_throw(self) -> None:
        self.server.set(0, DIRT, 16)
        self.assertTrue(asyncio.run(self.menu.quick_move(0)))
        self.assertTrue(self.menu.item(0).empty)
        self.assertEqual(self.menu.count(DIRT, where="player"), 16)

        self.server.set(54, STONE, 1)
        self.assertTrue(asyncio.run(self.menu.swap_with_hotbar(0, 0)))
        self.assertEqual(self.menu.item(0).identifier, STONE)
        self.assertTrue(self.menu.item(54).empty)

        self.assertTrue(asyncio.run(self.menu.throw(0, all_=True)))
        self.assertTrue(self.menu.item(0).empty)
        self.assertEqual(self.server.clicks[-1], (0, 1, 3))

    def test_move_whole_stack(self) -> None:
        self.server.set(0, DIRT, 12)
        moved = asyncio.run(self.menu.move(0, 2))
        self.assertEqual(moved, 12)
        self.assertEqual(self.server.get(2), (DIRT, 12))
        self.assertEqual(self.server.get(0), (None, 0))
        self.assertTrue(self.menu.carried.empty)

    def test_move_partial_count_uses_right_clicks(self) -> None:
        self.server.set(0, DIRT, 10)
        moved = asyncio.run(self.menu.move(0, 2, count=3))
        self.assertEqual(moved, 3)
        self.assertEqual(self.server.get(2), (DIRT, 3))
        self.assertEqual(self.server.get(0), (DIRT, 7))
        self.assertTrue(any(button == 1 for _, button, _ in self.server.clicks))

    def test_move_merges_into_an_existing_stack(self) -> None:
        self.server.set(0, DIRT, 10)
        self.server.set(2, DIRT, 60)
        moved = asyncio.run(self.menu.move(0, 2))
        self.assertEqual(moved, 4)
        self.assertEqual(self.server.get(2), (DIRT, 64))
        self.assertEqual(self.server.get(0), (DIRT, 6))

    def test_move_refuses_an_unrelated_target_stack(self) -> None:
        self.server.set(0, DIRT, 5)
        self.server.set(2, STONE, 1)
        with self.assertRaises(ClickRejected):
            asyncio.run(self.menu.move(0, 2))
        self.assertEqual(self.server.get(0), (DIRT, 5))
        self.assertEqual(self.server.get(2), (STONE, 1))

    def test_deposit_and_withdraw(self) -> None:
        player = FakeServer(
            "minecraft:generic_9x3", container_id=1, menu_type_id=2
        )
        self.bot.open(player)
        player.set(27, DIRT, 20)
        moved = asyncio.run(self.menu.deposit(DIRT, 8))
        self.assertEqual(moved, 8)
        self.assertEqual(player.get(0), (DIRT, 8))
        self.assertEqual(player.get(27), (DIRT, 12))

        taken = asyncio.run(self.menu.withdraw(DIRT, 3, where="container"))
        self.assertEqual(taken, 3)
        self.assertEqual(player.get(0), (DIRT, 5))

    def test_deposit_all_moves_every_matching_stack(self) -> None:
        player = FakeServer("minecraft:generic_9x3", container_id=1, menu_type_id=2)
        self.bot.open(player)
        player.set(27, DIRT, 5)
        player.set(28, STONE, 2)
        total = asyncio.run(self.menu.deposit_all(source="player"))
        self.assertEqual(total, 7)
        self.assertEqual(player.get(0), (DIRT, 5))
        self.assertEqual(player.get(1), (STONE, 2))


class CursorPredictionTests(unittest.TestCase):
    """The cursor is guessed locally when the server stays silent about it.

    ``clientbound_set_cursor_item`` only arrives when the click packet's claims
    did not match, so a click that empties the cursor produces no packet at all.
    """

    def setUp(self) -> None:
        self.bot = FakeBot()
        self.server = self.bot.open(FakeServer("minecraft:generic_9x3"))
        self.menu = self.bot.menu

    def predict(self, cursor, slot, button, mode="pickup") -> tuple[str | None, int]:
        stack = self.menu._predict_cursor(
            cursor,
            slot,
            button,
            CLICK_MODES[mode],
        )
        self.assertIsNotNone(stack)
        return (stack.identifier, stack.count)

    def test_left_click_takes_the_whole_stack(self) -> None:
        self.assertEqual(
            self.predict(ItemStack(), _stack(DIRT, 8), 0), (DIRT, 8)
        )

    def test_left_click_on_an_empty_slot_puts_the_cursor_down(self) -> None:
        self.assertEqual(
            self.predict(_stack(DIRT, 8), ItemStack(), 0), (None, 0)
        )

    def test_left_click_merges_into_a_matching_stack(self) -> None:
        self.assertEqual(
            self.predict(_stack(DIRT, 8), _stack(DIRT, 60), 0), (DIRT, 4)
        )

    def test_left_click_swaps_with_another_item(self) -> None:
        self.assertEqual(
            self.predict(_stack(DIRT, 4), _stack(STONE, 2), 0), (STONE, 2)
        )

    def test_right_click_takes_half(self) -> None:
        self.assertEqual(
            self.predict(ItemStack(), _stack(DIRT, 7), 1), (DIRT, 4)
        )

    def test_right_click_places_one(self) -> None:
        self.assertEqual(
            self.predict(_stack(DIRT, 3), ItemStack(), 1), (DIRT, 2)
        )
        self.assertEqual(
            self.predict(_stack(DIRT, 1), ItemStack(), 1), (None, 0)
        )

    def test_clicks_that_never_move_the_cursor_keep_it(self) -> None:
        for mode in ("quick_move", "swap", "pickup_all", "throw"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    self.predict(_stack(DIRT, 2), _stack(STONE, 2), 0, mode),
                    (DIRT, 2),
                )

    def test_unknown_modes_are_not_guessed(self) -> None:
        self.assertIsNone(
            self.menu._predict_cursor(ItemStack(), _stack(DIRT, 2), 0, 99)
        )

    def test_a_silent_click_still_updates_the_cursor(self) -> None:
        # The fake window has no cursor packet at all, so ``click`` has to fall
        # back to the prediction or every later click is sent with a stale view.
        self.server.set(0, DIRT, 6)
        self.assertTrue(asyncio.run(self.menu.pickup(0)))
        self.assertEqual((self.menu.carried.identifier, self.menu.carried.count), (DIRT, 6))


class UnreadableTests(unittest.TestCase):
    def test_unreadable_slots_are_steered_around(self) -> None:
        bot = FakeBot()
        server = bot.open(FakeServer("minecraft:generic_9x3"))
        server.set(0, DIRT, 4)
        server.state.unreadable.add(0)
        server.state.unreadable.add(1)
        menu = bot.menu
        self.assertFalse(menu.readable(0))
        self.assertIsNone(menu.find(DIRT))
        self.assertEqual(menu.free(where="container"), 2)
        with self.assertRaises(ContainerError):
            asyncio.run(menu.move(0, 5))
        self.assertIn("generic_9x3", menu.describe())


class OpenTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_block_waits_for_the_window(self) -> None:
        bot = FakeBot()
        bot.open_player()
        open_block = Vec3(3.5, 63.5, 0.5)

        async def use_item_on(position, **kwargs):
            bot.actions.used.append((0, position))
            server = FakeServer("minecraft:generic_9x3", container_id=1, menu_type_id=2)
            bot.open(server)
            return SimpleNamespace(ok=True)

        bot.actions.use_item_on = use_item_on
        menu = await Menu(bot).open_block((3, 63, 0))
        self.assertTrue(menu.is_open)
        self.assertEqual(menu.identifier, "minecraft:generic_9x3")
        self.assertTrue(bot.actions.looks)

    async def test_out_of_reach_block_is_refused(self) -> None:
        bot = FakeBot()
        with self.assertRaises(ContainerError):
            await Menu(bot).open_block((40, 64, 40))

    async def test_open_entity_uses_interact(self) -> None:
        bot = FakeBot()
        bot.open_player()

        async def interact(entity_id, **kwargs):
            bot.actions.used.append((entity_id, None))
            server = FakeServer("minecraft:merchant", container_id=1, menu_type_id=19)
            bot.open(server)
            return SimpleNamespace(ok=True)

        bot.actions.interact = interact
        menu = await Menu(bot).open_entity(42)
        self.assertEqual(menu.identifier, "minecraft:merchant")
        self.assertEqual(bot.actions.used, [(42, None)])


class DataSlotTests(unittest.TestCase):
    def test_furnace_data_is_exposed(self) -> None:
        bot = FakeBot()
        server = bot.open(FakeServer("minecraft:furnace", menu_type_id=14))
        menu = bot.menu
        self.assertEqual(menu.slots("container"), (0, 1))
        self.assertEqual(menu.slots("result"), (2,))
        server.state.data.update({0: 10, 1: 200, 2: 3, 3: 200})
        self.assertEqual(menu.data[2], 3)
        self.assertEqual(menu.data_value(2, -1), 3)
        self.assertEqual(menu.data_value(9, -1), -1)
        self.assertEqual(menu.data_value("cook_progress"), 3)
        self.assertEqual(menu.data_value("cook_total"), 200)
        self.assertEqual(menu.data_value("cook_progress", -1), 3)
        with self.assertRaises(KeyError):
            menu.data_value("brew_time")
        self.assertEqual(menu.identifier, "minecraft:furnace")


if __name__ == "__main__":
    unittest.main()
