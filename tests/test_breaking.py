"""Hardness tables, tool tiers, and the tool-picking action.

The numbers here are vanilla's own: they are what a player sees while holding
the mouse button down, so a wrong table means every dig either gives up early
(nothing breaks) or wastes seconds per block.
"""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from protobot.actions import ActionStatus, Actions
from protobot.events import EventBus
from protobot.physics import Vec3
from protobot.protocol.breaking import BreakTable, bundled_path, material_tool, table_for
from protobot.protocol.items import table_for as item_table_for
from protobot.protocol.tools import (
    TOOL_TIERS,
    ToolSpec,
    best_tool,
    break_seconds,
    tier_for_tools,
    tool_spec,
)
from protobot.protocol.versions import SUPPORTED_VERSIONS, get_version
from protobot.state import ItemStack

PROTOCOLS = tuple(sorted({spec.protocol for spec in SUPPORTED_VERSIONS.values()}))


class ToolSpecTests(unittest.TestCase):
    def test_known_identifiers(self):
        self.assertEqual(tool_spec("minecraft:iron_pickaxe"), ToolSpec("pickaxe", 2, 6.0))
        self.assertEqual(tool_spec("minecraft:diamond_axe"), ToolSpec("axe", 3, 8.0))
        self.assertEqual(tool_spec("minecraft:shears"), ToolSpec("shears", 0, 5.0))

    def test_unknown_identifiers(self):
        for identifier in ("minecraft:dirt", "minecraft:carrot_on_a_stick", "minecraft:stick"):
            self.assertIsNone(tool_spec(identifier), identifier)

    def test_namespace_is_optional(self):
        self.assertEqual(tool_spec("stone_pickaxe"), ToolSpec("pickaxe", 1, 4.0))

    def test_can_harvest(self):
        self.assertTrue(ToolSpec("pickaxe", 2, 6.0).can_harvest(1))
        self.assertFalse(ToolSpec("pickaxe", 0, 2.0).can_harvest(3))

    def test_tier_for_tools(self):
        tools = ("minecraft:wooden_pickaxe", "minecraft:stone_pickaxe")
        self.assertEqual(tier_for_tools(tools), 0)
        self.assertEqual(tier_for_tools(("minecraft:iron_pickaxe",)), 2)
        self.assertIsNone(tier_for_tools(()))
        self.assertIsNone(tier_for_tools(("minecraft:dirt",)))

    def test_best_tool_prefers_speed_within_tier(self):
        slots = [
            (36, "minecraft:wooden_pickaxe"),
            (38, "minecraft:iron_pickaxe"),
            (40, "minecraft:diamond_sword"),
        ]
        found = best_tool(slots, kind="pickaxe", tier=0)
        self.assertEqual(found[0], 38)
        self.assertIsNone(best_tool(slots, kind="pickaxe", tier=4))

    def test_tiers_table_is_complete(self):
        for tier, (level, speed) in TOOL_TIERS.items():
            self.assertIsInstance(level, int)
            self.assertGreater(speed, 0.0, tier)


class FormulaTests(unittest.TestCase):
    def test_unbreakable_and_instant(self):
        self.assertEqual(break_seconds(-1.0), float("inf"))
        self.assertEqual(break_seconds(0.0), 0.0)

    def test_known_times(self):
        # dirt by hand is the classic "0.75 s"
        self.assertEqual(break_seconds(0.5), 0.75)
        # no tool that can harvest is a 100 divisor, not 30
        self.assertEqual(break_seconds(1.5, can_harvest=False), 7.5)
        self.assertEqual(break_seconds(1.5, speed=2.0), 1.15)

    def test_never_returns_zero_for_real_blocks(self):
        self.assertGreater(break_seconds(0.1), 0.0)


class MaterialTests(unittest.TestCase):
    def test_plain_and_compound_materials(self):
        self.assertEqual(material_tool("mineable/pickaxe"), "pickaxe")
        self.assertEqual(material_tool("leaves;mineable/hoe"), "hoe")
        self.assertIsNone(material_tool("default"))
        self.assertIsNone(material_tool(None))


class TableTests(unittest.TestCase):
    def test_every_supported_protocol_has_a_table(self):
        for protocol in PROTOCOLS:
            table = table_for(protocol)
            self.assertIsNotNone(table, protocol)
            self.assertGreater(len(table), 1000, protocol)

    def test_unknown_protocol(self):
        self.assertIsNone(table_for(9999))
        empty = BreakTable({}, version="x", protocol=1)
        self.assertIsNone(empty.row("minecraft:stone"))

    def test_rows_carry_hardness_and_tool(self):
        table = table_for(777)
        self.assertEqual(table.hardness("minecraft:stone"), 1.5)
        self.assertEqual(table.tool("minecraft:stone"), "pickaxe")
        self.assertEqual(table.hardness("minecraft:grass_block"), 0.6)
        self.assertIsNone(table.hardness("minecraft:no_such_block"))

    def test_requires_is_not_the_same_as_tier_zero(self):
        table = table_for(777)
        # dirt is quickest with a shovel and drops by hand
        self.assertFalse(table.requires_tool("minecraft:dirt"))
        self.assertEqual(table.tier("minecraft:dirt"), 0)
        # stone needs *a* pickaxe: tier 0, yet a bare hand gets nothing
        self.assertTrue(table.requires_tool("minecraft:stone"))
        self.assertEqual(table.tier("minecraft:stone"), 0)
        self.assertEqual(table.tier("minecraft:obsidian"), 3)

    def test_vanilla_break_times(self):
        table = table_for(777)
        checks = [
            ("minecraft:dirt", None, 0.75),
            ("minecraft:stone", None, 7.5),
            ("minecraft:stone", "minecraft:wooden_pickaxe", 1.15),
            ("minecraft:stone", "minecraft:iron_pickaxe", 0.4),
            ("minecraft:stone", "minecraft:diamond_pickaxe", 0.3),
            ("minecraft:obsidian", "minecraft:diamond_pickaxe", 9.4),
            ("minecraft:obsidian", "minecraft:iron_pickaxe", 41.7),
            ("minecraft:oak_log", "minecraft:iron_axe", 0.5),
            ("minecraft:ancient_debris", "minecraft:diamond_pickaxe", 5.65),
        ]
        for identifier, tool, expected in checks:
            with self.subTest(identifier=identifier, tool=tool):
                self.assertAlmostEqual(
                    table.seconds(identifier, tool=tool), expected, places=2
                )

    def test_wrong_kind_is_no_faster_but_still_breaks(self):
        table = table_for(777)
        # an axe does not speed stone up, and stone still needs a pickaxe
        self.assertEqual(table.seconds("minecraft:stone", tool="minecraft:iron_axe"), 7.5)
        # bedrock never breaks, whatever is held
        self.assertEqual(
            table.seconds("minecraft:bedrock", tool="minecraft:netherite_pickaxe"),
            float("inf"),
        )

    def test_round_trip(self):
        table = table_for(777)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "breaking.json.gz"
            table.dump(path, version="26.3", protocol=777, source="test")
            loaded = BreakTable.load(path)
        self.assertEqual(len(loaded), len(table))
        self.assertEqual(loaded.hardness("minecraft:stone"), 1.5)
        self.assertTrue(loaded.requires_tool("minecraft:stone"))

    def test_bundled_files_are_gzipped_json(self):
        for protocol in PROTOCOLS:
            path = bundled_path(protocol)
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["protocol"], protocol)
            self.assertGreater(len(payload["blocks"]), 1000, protocol)

    def test_from_minecraft_data_shape(self):
        payload = [
            {"name": "stone", "hardness": 1.5, "material": "mineable/pickaxe"},
            {"name": "dirt", "hardness": 0.5, "material": "mineable/shovel"},
            {"name": "obsidian", "hardness": 50.0, "material": "default"},
        ]
        table = BreakTable.from_minecraft_data(payload, version="1.21.8", protocol=772)
        self.assertEqual(table.hardness("minecraft:stone"), 1.5)
        self.assertEqual(table.tool("minecraft:stone"), "pickaxe")
        self.assertFalse(table.requires_tool("minecraft:dirt"))
        # no harvest tools in this payload, so nothing "requires" a tool
        self.assertFalse(table.requires_tool("minecraft:obsidian"))

    def test_version_helpers(self):
        version = get_version("26.3")
        self.assertIsNotNone(version.breaking)
        self.assertEqual(version.hardness("minecraft:stone"), 1.5)
        self.assertEqual(version.breaking.seconds("minecraft:dirt"), 0.75)

    def test_items_table_can_resolve_harvest_tools(self):
        # the generator turns minecraft-data's numeric harvestTools into names
        self.assertEqual(item_table_for(777).identifier(55), "minecraft:dirt")


class _States:
    def __init__(self, states):
        self.states = states

    def get(self, state_id):
        return self.states.get(state_id)


class _World:
    def __init__(self, states=None):
        self.block_states = _States(states or {})
        self.chunks = {(0, 0): object()}

    def get_state_id(self, x, y, z):
        return 0


class _Bot:
    """Just enough bot for the tool helpers."""

    def __init__(self, *, held=None, hotbar=None, states=None):
        self.version = get_version("26.3")
        self.world = _World(states)
        self.hotbar = dict(hotbar or {})
        self._held = held
        self.selected_hotbar_slot = 0
        self.selects = []
        self.physics_state = type(
            "Physics",
            (),
            {
                "position": Vec3(0.5, 64.0, 0.5),
                "eye_height": 1.62,
                "yaw": 0.0,
                "pitch": 0.0,
            },
        )()
        self.sent = []
        self.sent_looks = []
        self.events = EventBus()

    @property
    def held_item(self):
        return self._held

    def get_inventory_item(self, slot):
        return self.hotbar.get(slot)

    async def select_hotbar_slot(self, slot):
        self.selected_hotbar_slot = slot
        self.selects.append(slot)

    async def send_raw(self, packet_id, payload=b""):
        self.sent.append((packet_id, payload))

    async def send_look(self, yaw, pitch):
        self.sent_looks.append((yaw, pitch))


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def test_block_name_from_state_id(self):
        names = {
            9: type("State", (), {"name": "minecraft:grass_block"})(),
            10: type("State", (), {"name": "minecraft:dirt"})(),
        }
        actions = Actions(_Bot(states=names))
        self.assertEqual(actions.block_name(9), "minecraft:grass_block")
        self.assertEqual(actions.block_name("stone"), "minecraft:stone")
        self.assertEqual(actions.block_name("minecraft:stone"), "minecraft:stone")
        with self.assertRaises(Exception):
            actions.block_name(999)

    def test_break_time_uses_the_held_tool_by_default(self):
        pickaxe = ItemStack(count=1, item_id=900, identifier="minecraft:iron_pickaxe")
        actions = Actions(_Bot(held=pickaxe))
        self.assertAlmostEqual(actions.break_time("minecraft:stone"), 0.4, places=2)
        # an explicit None asks about a bare hand
        self.assertAlmostEqual(actions.break_time("minecraft:stone", tool=None), 7.5, places=2)
        self.assertAlmostEqual(
            actions.break_time("minecraft:stone", tool="minecraft:wooden_pickaxe"),
            1.15,
            places=2,
        )

    def test_break_time_with_empty_hand(self):
        actions = Actions(_Bot())
        self.assertAlmostEqual(actions.break_time("minecraft:dirt"), 0.75, places=2)

    def test_break_time_unknown_block(self):
        actions = Actions(_Bot())
        self.assertIsNone(actions.break_time("minecraft:no_such_block"))

    def test_best_tool_scans_only_the_hotbar(self):
        hotbar = {
            36: ItemStack(count=1, item_id=1, identifier="minecraft:wooden_pickaxe"),
            38: ItemStack(count=1, item_id=2, identifier="minecraft:iron_pickaxe"),
            9: ItemStack(count=1, item_id=3, identifier="minecraft:netherite_pickaxe"),
        }
        actions = Actions(_Bot(hotbar=hotbar))
        self.assertEqual(actions.best_tool_for("minecraft:stone"), (38, "minecraft:iron_pickaxe"))
        # obsidian needs diamond: the iron pickaxe is not enough
        self.assertIsNone(actions.best_tool_for("minecraft:obsidian"))

    def test_best_tool_for_blocks_without_a_tool(self):
        actions = Actions(_Bot(hotbar={36: ItemStack(1, 1, "minecraft:iron_pickaxe")}))
        # bedrock has no tool in its row, so nothing on the hotbar is "best"
        self.assertIsNone(actions.best_tool_for("minecraft:bedrock"))

    def test_best_tool_for_dirt_is_its_shovel(self):
        actions = Actions(_Bot(hotbar={36: ItemStack(1, 1, "minecraft:iron_shovel")}))
        self.assertEqual(actions.best_tool_for("minecraft:dirt"), (36, "minecraft:iron_shovel"))

    async def test_equip_tool_selects_the_hotbar_index(self):
        bot = _Bot(hotbar={38: ItemStack(1, 2, "minecraft:iron_pickaxe")})
        result = await Actions(bot).equip_tool("minecraft:stone")
        self.assertEqual(bot.selects, [2])
        self.assertIs(result.status, ActionStatus.SENT)
        self.assertIn("iron_pickaxe", result.evidence)

    async def test_equip_tool_is_a_no_op_when_already_held(self):
        bot = _Bot(hotbar={36: ItemStack(1, 2, "minecraft:iron_pickaxe")})
        result = await Actions(bot).equip_tool("minecraft:stone")
        self.assertEqual(bot.selects, [])
        self.assertIn("already holding", result.evidence)

    async def test_equip_tool_rejects_when_nothing_is_good_enough(self):
        bot = _Bot(hotbar={36: ItemStack(1, 2, "minecraft:wooden_pickaxe")})
        result = await Actions(bot).equip_tool("minecraft:obsidian")
        self.assertIs(result.status, ActionStatus.REJECTED)
        self.assertIn("obsidian", result.evidence)
        self.assertEqual(bot.selects, [])


if __name__ == "__main__":
    unittest.main()
