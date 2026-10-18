"""The packet ids ``versions.py`` ships by hand must match the release data.

The tables in ``tests/data/packet-ids-<protocol>.json`` are produced by
``tools/gen_packet_ids.py`` straight from either the vanilla data generator's
``reports/packets.json`` or ``minecraft-data``'s ``protocol.json``.  Nothing in
here touches the network or an optional extra: the generated tables are
checked in.

A failing case means one of two things, and the message says which:

* a release bumped an id -- regenerate the table, then fix ``versions.py``;
* a field that used to be unverified now has a known id -- set it.

Regenerate with::

    python tools/gen_packet_ids.py --reports <packets.json> \\
        --protocol 777 --version 26.3 --out tests/data/packet-ids-777.json
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from protobot.protocol.versions import SUPPORTED_VERSIONS

DATA_DIR = Path(__file__).parent / "data"


def _audit_tables() -> dict[int, dict]:
    tables: dict[int, dict] = {}
    for path in sorted(DATA_DIR.glob("packet-ids-*.json")):
        table = json.loads(path.read_text(encoding="utf-8"))
        tables[table["protocol"]] = table
    return tables


class PacketIdAuditTests(unittest.TestCase):
    """Cross-check ``SUPPORTED_VERSIONS`` against the generated audit tables."""

    def setUp(self) -> None:
        self.tables = _audit_tables()
        self.by_protocol: dict[int, list] = {}
        for spec in SUPPORTED_VERSIONS.values():
            self.by_protocol.setdefault(spec.protocol, []).append(spec)

    def test_every_supported_protocol_has_an_audit_table(self) -> None:
        unaudited = sorted(set(self.by_protocol) - set(self.tables))
        self.assertEqual(
            unaudited,
            [],
            "supported protocol(s) without a tests/data/packet-ids-<n>.json "
            "table; generate one with tools/gen_packet_ids.py",
        )

    def test_audit_tables_record_the_release(self) -> None:
        for protocol, table in self.tables.items():
            with self.subTest(protocol=protocol):
                self.assertIn("source", table)
                self.assertTrue(table.get("play"), "table has no play-state ids")

    def test_shipped_play_ids_match_the_release(self) -> None:
        for protocol, table in self.tables.items():
            specs = self.by_protocol.get(protocol)
            if not specs:
                continue  # audited in advance of support; nothing shipped yet
            for spec in specs:
                self._check(spec, table, "play", spec.packets)

    def test_shipped_configuration_ids_match_the_release(self) -> None:
        for protocol, table in self.tables.items():
            specs = self.by_protocol.get(protocol)
            if not specs:
                continue
            for spec in specs:
                if not table.get("configuration"):
                    continue
                self._check(spec, table, "configuration", spec.configuration)

    def _check(self, spec, table: dict, state: str, ids) -> None:
        for field, expected in sorted(table[state].items()):
            with self.subTest(version=spec.name, protocol=spec.protocol, field=field):
                actual = getattr(ids, field)
                self.assertEqual(
                    actual,
                    expected,
                    f"{spec.name} {field} is 0x{actual:02X} but the "
                    f"release data says 0x{expected:02X}",
                )


class PacketTableHelperTests(unittest.TestCase):
    """The loader that produced the audit tables has to keep working."""

    def test_reports_and_minecraft_data_agree_on_shared_release(self) -> None:
        from protobot.protocol.packet_table import from_reports

        report = {
            "play": {
                "clientbound": {"minecraft:block_update": {"protocol_id": 8}},
                "serverbound": {"minecraft:player_action": {"protocol_id": 41}},
            },
            "configuration": {
                "clientbound": {
                    "minecraft:transfer": {"protocol_id": 12},
                },
                "serverbound": {"minecraft:keep_alive": {"protocol_id": 4}},
            },
        }
        tables = from_reports(report)
        self.assertEqual(tables["play.clientbound"], {"block_update": 8})
        self.assertEqual(tables["play.serverbound"], {"player_action": 41})
        self.assertEqual(tables["configuration.clientbound"], {"transfer": 12})

    def test_minecraft_data_mapper_loader(self) -> None:
        from protobot.protocol.packet_table import from_minecraft_data

        protocol = {
            "version": {"protocolVersion": 774},
            "play": {
                "toClient": {
                    "types": {
                        "packet": [
                            "container",
                            [
                                {
                                    "name": "name",
                                    "type": ["mapper", {"mappings": {"0x08": "block_change"}}],
                                }
                            ],
                        ]
                    }
                },
                "toServer": {
                    "types": {
                        "packet": [
                            "container",
                            [
                                {
                                    "name": "name",
                                    "type": ["mapper", {"mappings": {"0x28": "block_dig"}}],
                                }
                            ],
                        ]
                    }
                },
            },
        }
        tables = from_minecraft_data(protocol)
        self.assertEqual(tables["play.clientbound"], {"block_change": 8})
        self.assertEqual(tables["play.serverbound"], {"block_dig": 40})
        self.assertEqual(tables["version"]["protocol"], 774)


if __name__ == "__main__":
    unittest.main()
