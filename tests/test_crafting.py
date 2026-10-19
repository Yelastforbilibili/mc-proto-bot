"""Tests for the crafting layer: recipes, grid filling, and the result slot.

The fake server extends the one in ``test_containers`` with the recipe
behaviour of a crafting window: the result slot is computed from the grid and
appears only while the grid matches, and taking the result consumes one item
from every occupied grid slot.
"""

from __future__ import annotations

import unittest

from protobot.containers import (
    ClickRejected,
    ContainerError,
    MenuLayoutError,
    PLAYER_CONTAINER_ID,
)
from protobot.containers import menu as menu_module
from protobot.crafting import Recipe, recipe_for
from protobot.crafting.recipes import RECIPES, WOOD_RECIPES
from protobot.events import EventBus
from protobot.goals import CraftGoal, GoalRunner
from protobot.goals.base import GoalStatus
from protobot.state import ItemStack

# The fake windows below answer a click synchronously; the settle window that
# keeps a real server's multi-packet reply intact would only add latency here.
_QUIET_SECONDS = menu_module.QUIET_SECONDS


def setUpModule() -> None:
    menu_module.QUIET_SECONDS = 0.0


def tearDownModule() -> None:
    menu_module.QUIET_SECONDS = _QUIET_SECONDS

from tests.test_containers import FakeBot, FakeServer, _stack

PLANKS = "minecraft:oak_planks"
STICKS = "minecraft:stick"
TABLE = "minecraft:crafting_table"


class CraftingServer(FakeServer):
    """A window with a crafting grid and a computed result slot."""

    def __init__(
        self,
        recipe: Recipe | None,
        identifier: str = "minecraft:crafting",
        *,
        container_id: int = 1,
        menu_type_id: int = 12,
    ) -> None:
        super().__init__(
            identifier, container_id=container_id, menu_type_id=menu_type_id
        )
        self.recipe = recipe

    @property
    def result_slot(self) -> int:
        return self.layout.slot("result")

    def click(self, slot: int, button: int, mode: int) -> bool:
        if slot == self.result_slot and mode == 0:
            return self._take_result(button)
        worked = super().click(slot, button, mode)
        self._refresh()
        return worked

    def _grid(self) -> tuple[int, ...]:
        return self.layout.slots("grid")

    def _matches(self) -> bool:
        if self.recipe is None:
            return False
        grid = self._grid()
        width = 3 if len(grid) == 9 else 2
        wanted: dict[int, str] = {}
        for row, column, item in self.recipe.cells:
            if row >= width or column >= width:
                return False
            wanted[grid[row * width + column]] = item
        for slot in grid:
            stack = self.state.items.get(slot, ItemStack())
            expected = wanted.get(slot)
            if expected is None:
                if not stack.empty:
                    return False
            elif stack.identifier != expected or stack.count != 1:
                return False
        return True

    def _refresh(self) -> None:
        result = self.result_slot
        current = self.state.items.get(result, ItemStack())
        if self._matches():
            if current.empty:
                self._put(result, _stack(self.recipe.output, self.recipe.count))
                self._bump()
        elif not current.empty:
            self._put(result, ItemStack())
            self._bump()

    def _take_result(self, button: int) -> bool:
        result = self.result_slot
        current = self.state.items.get(result, ItemStack())
        if button != 0 or current.empty or not self.recipe:
            return False
        if not self.state.carried_item.empty:
            return False
        self.state.carried_item = self._take(result, current.count)
        for slot in self._grid():
            if not self.state.items.get(slot, ItemStack()).empty:
                self._take(slot, 1)
        self._bump()
        self._refresh()
        return True


class CraftBot(FakeBot):
    """A bot whose clicks are routed to the window they belong to.

    It also mirrors an open window's player-side slots into the player
    inventory, which is what ``Bot._mirror_open_menu`` does on the real client:
    the server announces inventory changes on the open window's container id.
    """

    def __init__(self) -> None:
        super().__init__()
        self.servers: dict[int, FakeServer] = {}
        self.events = EventBus()
        self._goals = GoalRunner(self)

    def host(self, server: FakeServer) -> FakeServer:
        self.servers[server.state.container_id] = server
        return self.open(server)

    def open_player(
        self, items: dict[int, tuple[str, int]] | None = None
    ) -> FakeServer:
        server = super().open_player(items)
        self.servers[PLAYER_CONTAINER_ID] = server
        return server

    async def click_container(
        self,
        slot: int,
        *,
        button: int = 0,
        click_type: object = "pickup",
        container_id: int | None = None,
        **kwargs: object,
    ) -> bool:
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
        server = self.servers.get(0 if container_id is None else container_id)
        if server is None:
            return False
        worked = server.click(slot, button, mode)
        self._mirror()
        return worked

    async def close_container(self, container_id: int | None = None) -> None:
        self._active_container_id = None

    def _mirror(self) -> None:
        active = self.active_container
        if active is None or active.container_id == PLAYER_CONTAINER_ID:
            return
        server = self.servers.get(active.container_id)
        player = self.servers.get(PLAYER_CONTAINER_ID)
        if server is None or player is None:
            return
        for index, menu_slot in enumerate(server.layout.player):
            inventory_slot = 9 + index if index < 27 else 36 + index - 27
            stack = server.state.items.get(menu_slot, ItemStack())
            if stack.empty:
                player.state.items.pop(inventory_slot, None)
            else:
                player.state.items[inventory_slot] = stack

    def get_inventory_item(self, slot: int) -> ItemStack:
        player = self.servers.get(PLAYER_CONTAINER_ID)
        if player is None:
            return ItemStack()
        return player.state.items.get(slot, ItemStack())

    @property
    def goals(self) -> GoalRunner:
        return self._goals


class RecipeTests(unittest.TestCase):
    def test_shaped_recipe_geometry(self) -> None:
        recipe = recipe_for(STICKS)
        self.assertTrue(recipe.shaped)
        self.assertEqual((recipe.width, recipe.height, recipe.size), (1, 2, 2))
        self.assertEqual(recipe.cells, ((0, 0, PLANKS), (1, 0, PLANKS)))
        self.assertEqual(recipe.ingredients, {PLANKS: 2})
        self.assertEqual(recipe.count, 4)

    def test_grid_size_follows_the_pattern(self) -> None:
        self.assertEqual(recipe_for("chest").size, 3)
        self.assertEqual(recipe_for(TABLE).size, 2)
        self.assertEqual(recipe_for("furnace").size, 3)

    def test_shapeless_recipe(self) -> None:
        recipe = recipe_for("oak_planks")
        self.assertFalse(recipe.shaped)
        self.assertEqual(recipe.ingredients, {"minecraft:oak_log": 1})
        self.assertEqual(recipe.count, 4)

    def test_wood_variant_lookup(self) -> None:
        self.assertIn(PLANKS, recipe_for(STICKS).ingredients)
        spruce = recipe_for(STICKS, planks="minecraft:spruce_planks")
        self.assertIn("minecraft:spruce_planks", spruce.ingredients)
        self.assertNotIn(PLANKS, spruce.ingredients)
        self.assertIn("spruce", WOOD_RECIPES)

    def test_unknown_output(self) -> None:
        with self.assertRaises(KeyError):
            recipe_for("minecraft:netherite_sword")

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            Recipe(output="", pattern=("x",), key={"x": PLANKS})
        with self.assertRaises(ValueError):
            Recipe(output=STICKS, count=0, pattern=("x",), key={"x": PLANKS})
        with self.assertRaises(ValueError):
            Recipe(output=STICKS)
        with self.assertRaises(ValueError):
            Recipe(output=STICKS, shapeless=(PLANKS,), pattern=("x",), key={"x": PLANKS})
        with self.assertRaises(ValueError):
            Recipe(output=STICKS, pattern=("xx", "x"), key={"x": PLANKS})
        with self.assertRaises(ValueError):
            Recipe(output=STICKS, pattern=("x",), key={})
        with self.assertRaises(ValueError):
            Recipe(output=STICKS, pattern=("xxxx",), key={"x": PLANKS})

    def test_describe(self) -> None:
        self.assertIn("minecraft:stick", recipe_for(STICKS).describe())
        self.assertIn("minecraft:oak_log", recipe_for("oak_planks").describe())
        self.assertIn(STICKS, RECIPES)


class GridTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.recipe = recipe_for(STICKS)
        self.bot = CraftBot()
        self.server = self.bot.host(CraftingServer(self.recipe))
        self.server.set(10, PLANKS, 6)
        self.menu = self.bot.menu

    async def test_fill_grid_places_one_per_cell(self) -> None:
        await self.menu.fill_grid(self.recipe)
        grid = self.server.layout.slots("grid")
        self.assertEqual(self.server.get(grid[0]), (PLANKS, 1))
        self.assertEqual(self.server.get(grid[3]), (PLANKS, 1))
        for slot in grid:
            if slot not in (grid[0], grid[3]):
                self.assertEqual(self.server.get(slot), (None, 0))
        self.assertEqual(self.server.get(10), (PLANKS, 4))
        self.assertTrue(self.server.cursor().empty)

    async def test_fill_grid_leaves_the_result_slot_alone(self) -> None:
        await self.menu.fill_grid(self.recipe)
        self.assertEqual(self.server.get(self.server.result_slot), (STICKS, 4))

    async def test_fill_grid_needs_a_wide_enough_grid(self) -> None:
        player = self.bot.open_player({9: (PLANKS, 4)})
        chest_recipe = recipe_for("chest")
        with self.assertRaises(MenuLayoutError):
            await self.bot.player_menu.fill_grid(chest_recipe)
        self.assertEqual(player.get(9), (PLANKS, 4))

    async def test_fill_grid_without_ingredients(self) -> None:
        with self.assertRaises(ContainerError):
            await self.menu.fill_grid(recipe_for("furnace"))

    async def test_fill_grid_refuses_a_dirty_grid(self) -> None:
        self.server.set(self.server.layout.slots("grid")[0], "minecraft:dirt", 1)
        with self.assertRaises(ContainerError):
            await self.menu.fill_grid(self.recipe)

    async def test_clear_grid_returns_everything(self) -> None:
        await self.menu.fill_grid(self.recipe)
        moved = await self.menu.clear_grid()
        self.assertEqual(moved, 2)
        for slot in self.server.layout.slots("grid"):
            self.assertEqual(self.server.get(slot), (None, 0))
        self.assertEqual(self.server.get(10), (PLANKS, 6))


class CraftingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.recipe = recipe_for(STICKS)
        self.bot = CraftBot()
        self.server = self.bot.host(CraftingServer(self.recipe))
        self.server.set(10, PLANKS, 6)
        self.menu = self.bot.menu

    async def test_craft_once_puts_the_result_in_the_inventory(self) -> None:
        await self.menu.fill_grid(self.recipe)
        made = await self.menu.craft_once(self.recipe)
        self.assertEqual(made, 4)
        self.assertTrue(self.server.cursor().empty)
        identifiers = [
            self.server.get(slot)[0]
            for slot in self.server.layout.player
            if self.server.get(slot)[0]
        ]
        self.assertIn(STICKS, identifiers)
        self.assertEqual(self.server.get(10), (PLANKS, 4))

    async def test_craft_twice_consumes_two_sets(self) -> None:
        made = await self.menu.craft(self.recipe, 2)
        self.assertEqual(made, 8)
        sticks = sum(
            self.server.get(slot)[1]
            for slot in self.server.layout.player
            if self.server.get(slot)[0] == STICKS
        )
        self.assertEqual(sticks, 8)
        self.assertEqual(self.server.get(10), (PLANKS, 2))
        for slot in self.server.layout.slots("grid"):
            self.assertEqual(self.server.get(slot), (None, 0))

    async def test_craft_stops_when_the_materials_run_out(self) -> None:
        self.server.set(10, PLANKS, 2)
        made = await self.menu.craft(self.recipe, 3)
        self.assertEqual(made, 4)
        self.assertEqual(self.menu.count(PLANKS), 0)
        self.assertEqual(self.menu.count(STICKS), 4)

    async def test_craft_once_rejects_the_wrong_result(self) -> None:
        self.server.recipe = recipe_for("crafting_table")
        await self.menu.fill_grid(recipe_for("crafting_table"))
        with self.assertRaises(ClickRejected):
            await self.menu.craft_once(self.recipe)

    async def test_craft_once_needs_a_result(self) -> None:
        with self.assertRaises(ClickRejected):
            await self.menu.craft_once(self.recipe)

    async def test_player_grid_crafts_two_by_two(self) -> None:
        # Crafting in the inventory means nothing else is open; a real client
        # cannot click container 0 while a chest window owns the screen.
        self.bot.host(
            CraftingServer(
                recipe_for(TABLE),
                "minecraft:inventory",
                container_id=PLAYER_CONTAINER_ID,
                menu_type_id=0,
            )
        )
        self.bot._active_container_id = None
        player = self.bot.servers[PLAYER_CONTAINER_ID]
        player.recipe = recipe_for(TABLE)
        player.set(9, PLANKS, 4)
        menu = self.bot.player_menu
        self.assertEqual(menu.identifier, "minecraft:inventory")
        made = await menu.craft(recipe_for(TABLE))
        self.assertEqual(made, 1)
        for slot in menu.layout.slots("grid"):
            self.assertEqual(player.get(slot), (None, 0))
        tables = [
            player.get(slot)[1]
            for slot in menu.layout.player
            if player.get(slot)[0] == TABLE
        ]
        self.assertEqual(tables, [1])


class CraftGoalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.recipe = recipe_for(STICKS)
        self.bot = CraftBot()
        self.table = CraftingServer(self.recipe)
        self.bot.servers[self.table.state.container_id] = self.table
        self.bot.containers[self.table.state.container_id] = self.table.state
        player = CraftingServer(
            self.recipe,
            "minecraft:inventory",
            container_id=PLAYER_CONTAINER_ID,
            menu_type_id=0,
        )
        player.set(10, PLANKS, 6)
        self.player = self.bot.host(player)

    async def test_goal_finishes_when_the_inventory_grew(self) -> None:
        goal = CraftGoal(self.recipe, times=2)
        report = await self.bot.goals.run(goal, timeout=10.0)
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(goal._crafted_up_to_now, 8)
        self.assertEqual(
            sum(
                self.player.get(slot)[1]
                for slot in self.player.layout.player
                if self.player.get(slot)[0] == STICKS
            ),
            8,
        )

    async def test_goal_is_not_reached_before_it_crafts(self) -> None:
        self.player.set(20, STICKS, 8)
        goal = CraftGoal(self.recipe, times=2)
        self.assertFalse(goal.is_reached(self.bot))
        self.assertEqual(goal.target, 8)

    async def test_missing_materials_are_named(self) -> None:
        # "the server crafted nothing" was useless when the real answer was
        # "you have four planks and the table's recipe eats four of them".
        self.player.set(10, None)
        self.player.set(11, PLANKS, 1)
        goal = CraftGoal(self.recipe)
        report = await self.bot.goals.run(goal, timeout=5.0)
        self.assertFalse(report.ok)
        self.assertIn("missing", report.detail)
        self.assertIn(PLANKS, report.detail)
        self.assertIn("1x", report.detail)

    async def test_missing_ingredients_can_be_asked_for(self) -> None:
        self.player.set(10, PLANKS, 1)
        self.assertEqual(
            self.bot.player_menu.missing_ingredients(self.recipe), {PLANKS: 1}
        )
        self.player.set(10, PLANKS, 6)
        self.assertEqual(self.bot.player_menu.missing_ingredients(self.recipe), {})

    async def test_three_by_three_needs_a_table(self) -> None:
        goal = CraftGoal(recipe_for("chest"))
        report = await self.bot.goals.run(goal, timeout=5.0)
        self.assertFalse(report.ok)
        self.assertIn("crafting table", report.detail)

    async def test_three_by_three_crafts_at_the_table(self) -> None:
        recipe = recipe_for("chest")
        self.table.recipe = recipe
        self.table.set(10, PLANKS, 8)
        self.bot._active_container_id = self.table.state.container_id
        goal = CraftGoal(recipe, table=(5, -60, 5))
        report = await self.bot.goals.run(goal, timeout=10.0)
        self.assertTrue(report.ok, report.detail)
        chests = sum(
            self.table.get(slot)[1]
            for slot in self.table.layout.player
            if self.table.get(slot)[0] == "minecraft:chest"
        )
        self.assertEqual(chests, 1)
        planks = sum(
            self.table.get(slot)[1]
            for slot in self.table.layout.player
            if self.table.get(slot)[0] == PLANKS
        )
        self.assertEqual(planks, 0)
        self.assertIsNone(self.bot._active_container_id)

    async def test_goal_status_after_one_craft(self) -> None:
        goal = CraftGoal(self.recipe, times=1)
        status = await goal.tick(self.bot)
        self.assertIs(status, GoalStatus.DONE)
        # The goal tracks items, not crafts: one craft of sticks yields four.
        self.assertEqual(goal._crafted_up_to_now, 4)
