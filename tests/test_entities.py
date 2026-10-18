"""Tests for the bundled entity registry tables."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from protobot.protocol.entities import (
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    EntityTable,
    bundled_path,
    clear_cache,
    load_sizes,
    table_for,
)
from protobot.protocol.versions import SUPPORTED_VERSIONS, get_version

PROTOCOLS = tuple(
    sorted({spec.protocol for spec in SUPPORTED_VERSIONS.values()})
)


class EntityTableTests(unittest.TestCase):
    def test_from_report_joins_sizes_by_name(self) -> None:
        report = {
            "minecraft:entity_type": {
                "default": "minecraft:pig",
                "entries": {
                    "minecraft:cow": {"protocol_id": 30},
                    "minecraft:boat": {"protocol_id": 40},
                    "minecraft:no_id": {},
                },
            }
        }
        sizes = {"minecraft:cow": (0.9, 1.4)}
        table = EntityTable.from_report(
            report, sizes=sizes, version="26.3", protocol=777
        )
        self.assertEqual(table.entity_id("cow"), 30)
        self.assertEqual(table.identifier(30), "minecraft:cow")
        self.assertEqual(table.size(30), (0.9, 1.4))
        self.assertEqual(table.size("minecraft:cow"), (0.9, 1.4))
        # a type with no measured box stays unmeasured instead of guessing
        self.assertIsNone(table.size(40))
        self.assertIsNone(table.size("minecraft:boat"))
        self.assertEqual(len(table), 2)

    def test_from_minecraft_data_reads_ids_and_sizes(self) -> None:
        entities = [
            {"id": 28, "name": "cow", "width": 0.9, "height": 1.4},
            {"id": 29, "name": "no_size"},
            {"name": "no_id"},
        ]
        table = EntityTable.from_minecraft_data(
            entities, version="1.21.8", protocol=772
        )
        self.assertEqual(table.entity_id("minecraft:cow"), 28)
        self.assertEqual(table.size(28), (0.9, 1.4))
        self.assertIsNone(table.size(29))
        self.assertEqual(len(table), 2)

    def test_unknown_lookups_are_none(self) -> None:
        table = EntityTable({"minecraft:cow": {"id": 30}})
        self.assertIsNone(table.identifier(9999))
        self.assertIsNone(table.entity_id("minecraft:not_a_real_entity"))
        self.assertIsNone(table.size(9999))
        self.assertNotIn(30, table)  # only strings are identifiers

    def test_dump_and_load_round_trip(self) -> None:
        table = EntityTable(
            {"minecraft:cow": {"id": 30, "width": 0.9, "height": 1.4}},
            version="26.3",
            protocol=777,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entities-777.json.gz"
            table.dump(path, version="26.3", protocol=777, source="reports")
            loaded = EntityTable.load(path)
        self.assertEqual(loaded.version, "26.3")
        self.assertEqual(loaded.protocol, 777)
        self.assertEqual(loaded.source, "reports")
        self.assertEqual(loaded.entity_id("cow"), 30)
        self.assertEqual(loaded.size(30), (0.9, 1.4))


class LoadSizesTests(unittest.TestCase):
    def test_minecraft_data_shape_becomes_a_size_map(self) -> None:
        payload = [
            {"id": 30, "name": "cow", "width": 0.9, "height": 1.4},
            {"id": 31, "name": "no_size"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entities.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            sizes = load_sizes(path)
        self.assertEqual(sizes, {"minecraft:cow": (0.9, 1.4)})


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
                self.assertGreater(len(table), 100)
                self.assertEqual(table.protocol, protocol)

    def test_the_26_3_table_matches_the_live_server(self) -> None:
        # Verified against a real 26.3 server: the cow a /summon produced had
        # type id 30 and a hitbox the ray cast had to rebuild as 0.9 x 1.4.
        table = table_for(777)
        assert table is not None
        self.assertEqual(table.entity_id("minecraft:cow"), 30)
        self.assertEqual(table.identifier(30), "minecraft:cow")
        self.assertEqual(table.size(30), (0.9, 1.4))
        self.assertEqual(table.size("minecraft:zombie"), (0.6, 1.95))

    def test_ids_move_between_releases(self) -> None:
        # The cow is 28 on 1.21.8 and 30 on 26.3; a shared table would be wrong.
        old = table_for(772)
        new = table_for(777)
        assert old is not None and new is not None
        self.assertEqual(old.entity_id("minecraft:cow"), 28)
        self.assertEqual(new.entity_id("minecraft:cow"), 30)

    def test_unmeasured_types_are_reported_as_such(self) -> None:
        # These exist in 26.3 but minecraft-data has no measured hitbox for them,
        # so the table must say "unknown" rather than inventing a player-sized box.
        table = table_for(777)
        assert table is not None
        self.assertIsNone(table.size("minecraft:sulfur_cube"))
        self.assertIsNone(table.size("minecraft:cushion"))
        self.assertLess(DEFAULT_WIDTH, DEFAULT_HEIGHT)

    def test_unknown_protocol_has_no_table(self) -> None:
        self.assertIsNone(table_for(9999))


class VersionSpecEntitiesTests(unittest.TestCase):
    def test_entity_identifier_uses_the_bundled_table(self) -> None:
        self.assertEqual(get_version("26.3").entity_identifier(30), "minecraft:cow")
        self.assertEqual(
            get_version("1.21.8").entity_identifier(28), "minecraft:cow"
        )

    def test_entity_size_uses_the_bundled_table(self) -> None:
        self.assertEqual(get_version("26.3").entity_size(30), (0.9, 1.4))
        self.assertIsNone(get_version("26.3").entity_size(9999))


if __name__ == "__main__":
    unittest.main()
