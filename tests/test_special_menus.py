"""The machine menus: enchanting table, anvil, grindstone, loom, smithing, stonecutter.

These six menus are not storage.  A player puts something into a named input
slot, presses a button (or types a name), and the server rewrites the result
slot.  The fakes below model exactly that: the click machinery is the same one
``test_containers`` uses, and a button or a rename is a callback the test
controls.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from protobot.containers import ClickRejected, ContainerError, Menu, MenuNotOpen
from protobot.physics import Vec3

from tests.test_containers import FakeBot, FakeServer


class MachineServer(FakeServer):
    """A machine menu whose button and name box are scripted by the test."""

    def __init__(
        self,
        identifier: str,
        *,
        container_id: int = 1,
        menu_type_id: int = 0,
        slot_count: int | None = None,
    ) -> None:
        super().__init__(
            identifier,
            container_id=container_id,
            menu_type_id=menu_type_id,
            slot_count=slot_count,
        )
        self.buttons: list[int] = []
        self.names: list[str] = []
        #: ``callable(server, index)`` -- what the server does with a button.
        self.on_button = None
        #: ``callable(server, name)`` -- what the server does with a name.
        self.on_rename = None

    def button(self, index: int) -> None:
        self.buttons.append(index)
        if self.on_button is not None:
            self.on_button(self, index)

    def rename(self, name: str) -> None:
        self.names.append(name)
        if self.on_rename is not None:
            self.on_rename(self, name)


class MachineBot(FakeBot):
    def __init__(self, *, health: float = 20.0, level: int = 30) -> None:
        super().__init__()
        self.player = SimpleNamespace(
            health=float(health), dead=False, experience_level=level
        )
        self.buttons: list[tuple[int, int | None]] = []
        self.names: list[str] = []
        self.world = None

    async def click_container_button(
        self, button: int, *, container_id: int | None = None
    ) -> None:
        self.buttons.append((button, container_id))
        assert self.server is not None
        self.server.button(button)

    async def rename_container_item(
        self, name: str, *, container_id: int | None = None
    ) -> None:
        self.names.append(name)
        assert self.server is not None
        self.server.rename(name)


def _open(bot: MachineBot, identifier: str, *, menu_type_id: int = 0) -> MachineServer:
    """Start the machine window; the player part starts empty."""

    bot.open_player()
    return bot.open(MachineServer(identifier, menu_type_id=menu_type_id))


def _stock_player(server: MachineServer, *items: tuple[str, int]) -> None:
    """Put items into the window's own copy of the player inventory."""

    for slot, (identifier, count) in zip(server.layout.player, items):
        server.set(slot, identifier, count)


class EnchantTests(unittest.IsolatedAsyncioTestCase):
    async def test_enchant_stocks_item_and_lapis_then_presses_the_option(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        _stock_player(
            server,
            ("minecraft:diamond_sword", 1),
            ("minecraft:lapis_lazuli", 3),
        )

        def press(srv: MachineServer, index: int) -> None:
            # The table consumes one lapis and rewrites the item in place.
            srv.set(srv.layout.slot("lapis"), "minecraft:lapis_lazuli", 2)
            srv.set(srv.layout.slot("item"), "minecraft:diamond_sword", 1)

        server.on_button = press
        menu = Menu(bot, server.state.container_id)
        produced = await menu.enchant("minecraft:diamond_sword", timeout=0.2)
        self.assertEqual(produced.identifier, "minecraft:diamond_sword")
        self.assertEqual(bot.buttons, [(0, server.state.container_id)])
        self.assertEqual(menu.item(menu.layout.slot("lapis")).count, 2)

    async def test_enchant_reports_a_button_that_did_nothing(self) -> None:
        bot = MachineBot(level=3)
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        menu = Menu(bot, server.state.container_id)
        server.set(menu.layout.slot("item"), "minecraft:stone_sword", 1)
        with self.assertRaises(ClickRejected) as caught:
            await menu.enchant("minecraft:stone_sword", lapis=False, timeout=0.05)
        self.assertIn("level 3", str(caught.exception))

    async def test_enchant_needs_the_item_in_the_inventory(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        menu = Menu(bot, server.state.container_id)
        with self.assertRaises(ContainerError):
            await menu.enchant("minecraft:diamond_sword", timeout=0.05)

    async def test_enchant_refuses_an_unknown_option(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        menu = Menu(bot, server.state.container_id)
        with self.assertRaises(ValueError):
            await menu.enchant("minecraft:stone_sword", option=3)


class MachineTests(unittest.IsolatedAsyncioTestCase):
    async def test_anvil_renames_and_takes_the_result(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:anvil", menu_type_id=8)
        _stock_player(server, ("minecraft:diamond_sword", 1))

        def rename(srv: MachineServer, name: str) -> None:
            srv.set(srv.layout.slot("result"), "minecraft:diamond_sword", 1)

        server.on_rename = rename
        menu = Menu(bot, server.state.container_id)
        produced = await menu.anvil("minecraft:diamond_sword", name="Probe", timeout=0.2)
        self.assertEqual(produced.identifier, "minecraft:diamond_sword")
        self.assertEqual(bot.names, ["Probe"])
        self.assertEqual(server.names, ["Probe"])

    async def test_grindstone_takes_the_result_without_a_button(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:grindstone", menu_type_id=15)
        _stock_player(server, ("minecraft:diamond_sword", 1))
        menu = Menu(bot, server.state.container_id)
        server.set(menu.layout.slot("result"), "minecraft:diamond_sword", 1)
        produced = await menu.grindstone("minecraft:diamond_sword", timeout=0.2)
        self.assertEqual(produced.identifier, "minecraft:diamond_sword")
        self.assertEqual(bot.buttons, [])

    async def test_stonecutter_presses_the_recipe_button(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:stonecutter", menu_type_id=24)
        _stock_player(server, ("minecraft:stone", 4))
        menu = Menu(bot, server.state.container_id)
        server.on_button = lambda srv, index: srv.set(
            srv.layout.slot("result"), "minecraft:stone_slab", 2
        )
        produced = await menu.stonecutter("minecraft:stone", recipe=3, timeout=0.2)
        self.assertEqual(produced.identifier, "minecraft:stone_slab")
        self.assertEqual(bot.buttons, [(3, server.state.container_id)])

    async def test_loom_presses_a_pattern(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:loom", menu_type_id=18)
        _stock_player(
            server,
            ("minecraft:white_banner", 1),
            ("minecraft:red_dye", 2),
            ("minecraft:flower_banner_pattern", 1),
        )
        menu = Menu(bot, server.state.container_id)
        server.on_button = lambda srv, index: srv.set(
            srv.layout.slot("result"), "minecraft:white_banner", 1
        )
        produced = await menu.loom(
            "minecraft:white_banner",
            dye="minecraft:red_dye",
            pattern_item="minecraft:flower_banner_pattern",
            pattern=2,
            timeout=0.2,
        )
        self.assertEqual(produced.identifier, "minecraft:white_banner")
        self.assertEqual(bot.buttons, [(2, server.state.container_id)])

    async def test_smithing_stocks_three_inputs(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:smithing", menu_type_id=21)
        _stock_player(
            server,
            ("minecraft:netherite_upgrade_smithing_template", 1),
            ("minecraft:diamond_sword", 1),
            ("minecraft:netherite_ingot", 1),
        )
        menu = Menu(bot, server.state.container_id)
        server.set(menu.layout.slot("result"), "minecraft:netherite_sword", 1)
        produced = await menu.smithing(
            "minecraft:netherite_upgrade_smithing_template",
            "minecraft:diamond_sword",
            "minecraft:netherite_ingot",
            timeout=0.2,
        )
        self.assertEqual(produced.identifier, "minecraft:netherite_sword")
        self.assertEqual(menu.item(0).identifier, "minecraft:netherite_upgrade_smithing_template")

    async def test_an_empty_result_slot_is_reported(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:grindstone", menu_type_id=15)
        _stock_player(server, ("minecraft:diamond_sword", 1))
        menu = Menu(bot, server.state.container_id)
        with self.assertRaises(ClickRejected):
            await menu.grindstone("minecraft:diamond_sword", timeout=0.05)


class WorldStub:
    """Enough of a world model for ``live()``: one named block, air elsewhere."""

    def __init__(self, blocks: dict[tuple[int, int, int], str]) -> None:
        self.blocks = blocks
        self.block_states = self

    def get(self, state_id: int) -> SimpleNamespace:
        if state_id == 1 and self.blocks:
            return SimpleNamespace(name=next(iter(self.blocks.values())))
        return SimpleNamespace(name="minecraft:air")

    def get_state_id(self, x: int, y: int, z: int) -> int:
        return 1 if (x, y, z) in self.blocks else 0


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_accepts_a_usable_window(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        Menu(bot, server.state.container_id).live()

    async def test_live_rejects_a_closed_window(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        server.state.is_open = False
        with self.assertRaises(MenuNotOpen) as caught:
            Menu(bot, server.state.container_id).live()
        self.assertIn("closed the window", str(caught.exception))

    async def test_live_rejects_a_dead_player(self) -> None:
        bot = MachineBot(health=0.0)
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        with self.assertRaises(MenuNotOpen) as caught:
            Menu(bot, server.state.container_id).live()
        self.assertIn("died", str(caught.exception))

    async def test_live_rejects_a_block_that_was_broken(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        menu = Menu(bot, server.state.container_id)
        menu.block_position = (2, 64, 0)
        menu.block_identifier = "minecraft:enchanting_table"
        bot.world = WorldStub({})
        with self.assertRaises(MenuNotOpen) as caught:
            menu.live()
        self.assertIn("minecraft:air", str(caught.exception))

    async def test_live_rejects_a_block_that_was_replaced(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        menu = Menu(bot, server.state.container_id)
        menu.block_position = (2, 64, 0)
        menu.block_identifier = "minecraft:enchanting_table"
        bot.world = WorldStub({(2, 64, 0): "minecraft:oak_planks"})
        with self.assertRaises(MenuNotOpen) as caught:
            menu.live()
        self.assertIn("minecraft:oak_planks", str(caught.exception))

    async def test_live_rejects_a_player_that_walked_away(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        menu = Menu(bot, server.state.container_id)
        menu.block_position = (2, 64, 0)
        menu.block_identifier = "minecraft:enchanting_table"
        bot.world = WorldStub({(2, 64, 0): "minecraft:enchanting_table"})
        bot.physics_state.position = Vec3(40.5, 64.0, 0.5)
        with self.assertRaises(MenuNotOpen) as caught:
            menu.live()
        self.assertIn("blocks from", str(caught.exception))

    async def test_enchant_checks_liveness_first(self) -> None:
        bot = MachineBot()
        server = _open(bot, "minecraft:enchantment", menu_type_id=13)
        server.state.is_open = False
        with self.assertRaises(MenuNotOpen):
            await Menu(bot, server.state.container_id).enchant(
                "minecraft:diamond_sword", timeout=0.05
            )
        self.assertEqual(bot.buttons, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
