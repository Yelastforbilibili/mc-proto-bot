"""Tests for the bundled item registry tables."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from protobot.protocol.items import (
    DATA_DIRECTORY,
    ItemTable,
    bundled_path,
    clear_cache,
    normalise_identifier,
    table_for,
)
from protobot.protocol.versions import SUPPORTED_VERSIONS, get_version

PROTOCOLS = tuple(
    sorted({spec.protocol for spec in SUPPORTED_VERSIONS.values()})
)


class NormaliseTests(unittest.TestCase):
    def test_bare_name_gets_the_minecraft_namespace(self) -> None:
        self.assertEqual(normalise_identifier("dirt"), "minecraft:dirt")

    def test_explicit_namespace_is_kept(self) -> None:
        self.assertEqual(normalise_identifier("mod:thing"), "mod:thing")

    def test_empty_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalise_identifier("  ")


class ItemTableTests(unittest.TestCase):
    def test_from_report_reads_protocol_ids(self) -> None:
        report = {
            "minecraft:item": {
                "default": "minecraft:air",
                "entries": {
                    "minecraft:stone": {"protocol_id": 1},
                    "minecraft:dirt": {"protocol_id": 55},
                },
            }
        }
        table = ItemTable.from_report(report, version="26.3", protocol=777)
        self.assertEqual(table.item_id("dirt"), 55)
        self.assertEqual(table.identifier(55), "minecraft:dirt")
        self.assertEqual(len(table), 2)
        self.assertIn("stone", table)

    def test_from_minecraft_data_reads_item_ids(self) -> None:
        items = [
            {"id": 27, "name": "grass_block"},
            {"id": 28, "name": "dirt"},
            {"name": "no_id"},
        ]
        table = ItemTable.from_minecraft_data(items, version="1.21.8", protocol=772)
        self.assertEqual(table.item_id("minecraft:dirt"), 28)
        self.assertEqual(table.identifier(27), "minecraft:grass_block")
        self.assertEqual(len(table), 2)

    def test_unknown_identifiers_are_none(self) -> None:
        table = ItemTable({"minecraft:dirt": 1})
        self.assertIsNone(table.identifier(9999))
        self.assertIsNone(table.item_id("minecraft:not_a_real_item"))
        self.assertNotIn(1, table)  # only strings are identifiers

    def test_dump_and_load_round_trip(self) -> None:
        table = ItemTable({"minecraft:dirt": 55}, version="26.3", protocol=777)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items-777.json.gz"
            table.dump(path, version="26.3", protocol=777, source="reports")
            loaded = ItemTable.load(path)
        self.assertEqual(loaded.version, "26.3")
        self.assertEqual(loaded.protocol, 777)
        self.assertEqual(loaded.source, "reports")
        self.assertEqual(loaded.item_id("dirt"), 55)


class BundledTableTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_cache()

    def test_every_supported_protocol_ships_a_table(self) -> None:
        for protocol in PROTOCOLS:
            with self.subTest(protocol=protocol):
                path = bundled_path(protocol)
                self.assertTrue(path.exists(), f"missing {path.name}")
                table = table_for(protocol)
                self.assertIsNotNone(table)
                assert table is not None
                self.assertGreater(len(table), 1000)
                self.assertEqual(table.protocol, protocol)

    def test_the_26_3_table_matches_the_live_server(self) -> None:
        # Verified against a real 26.3 server: the /give'd dirt arrived as
        # registry id 55 and item 54 is grass_block.
        table = table_for(777)
        assert table is not None
        self.assertEqual(table.item_id("minecraft:dirt"), 55)
        self.assertEqual(table.identifier(55), "minecraft:dirt")
        self.assertEqual(table.item_id("minecraft:grass_block"), 54)

    def test_ids_move_between_releases(self) -> None:
        # Dirt is 28 on 1.21.8 and 55 on 26.3; a shared table would be wrong.
        old = table_for(772)
        new = table_for(777)
        assert old is not None and new is not None
        self.assertEqual(old.item_id("minecraft:dirt"), 28)
        self.assertEqual(new.item_id("minecraft:dirt"), 55)

    def test_unknown_protocol_has_no_table(self) -> None:
        self.assertIsNone(table_for(9999))


class VersionSpecItemsTests(unittest.TestCase):
    def test_item_identifier_uses_the_bundled_table(self) -> None:
        self.assertEqual(get_version("26.3").item_identifier(55), "minecraft:dirt")
        self.assertEqual(
            get_version("1.21.8").item_identifier(28), "minecraft:dirt"
        )

    def test_explicit_overrides_still_win(self) -> None:
        spec = get_version("26.3")
        overrides = spec.item_identifiers
        self.assertTrue(overrides, "the equipment overrides disappeared")
        for item_id, identifier in overrides:
            self.assertEqual(spec.item_identifier(item_id), identifier)


if __name__ == "__main__":
    unittest.main()
