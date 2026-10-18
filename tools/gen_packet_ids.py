"""Regenerate and audit the packet ids that ``protobot.protocol.versions``
ships by hand.

Usage
-----
Vanilla reports (any release; the modern 26.x line only ships tables this way)::

    java -DbundlerMainClass=net.minecraft.data.Main -jar <server-bundle>.jar --reports
    python tools/gen_packet_ids.py --reports generated/reports/packets.json \\
        --protocol 777 --out tests/data/packet-ids-777.json --emit

minecraft-data (the older releases)::

    python tools/gen_packet_ids.py --mcdata 1.21.8-protocol.json \\
        --protocol 772 --out tests/data/packet-ids-772.json

``--out`` writes the table ``tests/test_packet_ids.py`` diff against; ``--emit``
prints a ``PlayPacketIds(...)`` literal to paste into ``versions.py``.  Every
field the source cannot resolve is listed instead of silently skipped, and
packets the library does not model yet are listed too, so a release bump is a
guided diff rather than a guessing game.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from protobot.protocol.packet_table import (  # noqa: E402
    CLIENTBOUND_FIELDS,
    CONFIGURATION_FIELDS,
    SERVERBOUND_FIELDS,
    field_ids,
    load,
    unmapped_packets,
)


def _literal(pairs: list[tuple[str, int]], indent: str = "    ") -> str:
    return "\n".join(f"{indent}{name}=0x{value:02X}," for name, value in pairs)


def emit(version_name: str, resolved: dict[str, dict[str, int]]) -> str:
    clientbound = [
        (field, resolved[field]["play"])
        for field in CLIENTBOUND_FIELDS
        if field in resolved
    ]
    serverbound = [
        (field, resolved[field]["play"])
        for field in SERVERBOUND_FIELDS
        if field in resolved
    ]
    configuration = [
        (field, resolved[field]["configuration"])
        for field in CONFIGURATION_FIELDS
        if field in resolved
    ]
    blocks = [f"PlayPacketIds(  # {version_name}"]
    blocks.append(_literal(clientbound))
    blocks.append(_literal(serverbound))
    blocks.append(")")
    if configuration:
        blocks.append(f"ConfigurationPacketIds(  # {version_name}")
        blocks.append(_literal(configuration))
        blocks.append(")")
    return "\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--reports", type=Path, help="vanilla reports/packets.json")
    source.add_argument("--mcdata", type=Path, help="minecraft-data protocol.json")
    parser.add_argument("--protocol", type=int, help="protocol number to record")
    parser.add_argument("--version", default="", help="release name for comments")
    parser.add_argument("--out", type=Path, help="write the audit table here")
    parser.add_argument("--emit", action="store_true", help="print versions.py literals")
    args = parser.parse_args(argv)

    path = args.reports or args.mcdata
    document = json.loads(path.read_text(encoding="utf-8"))
    tables = load(document)

    protocol = args.protocol
    if protocol is None:
        protocol = tables.get("version", {}).get("protocol")
    if protocol is None:
        parser.error("--protocol is required when the source does not record one")

    resolved, missing = field_ids(tables)
    unmapped = unmapped_packets(tables)

    if args.out:
        payload = {
            "protocol": protocol,
            "version": args.version or path.parent.parent.name,
            "source": f"{'reports' if args.reports else 'mcdata'}:{path.name}",
            "play": {
                field: values["play"]
                for field, values in resolved.items()
                if "play" in values
            },
            "configuration": {
                field: values["configuration"]
                for field, values in resolved.items()
                if "configuration" in values
            },
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.out} ({len(resolved)} fields, protocol {protocol})")

    if args.emit:
        print(emit(args.version or str(protocol), resolved))

    if missing:
        print(f"\n{len(missing)} field(s) unknown to this source:", file=sys.stderr)
        for field in missing:
            print(f"  {field}", file=sys.stderr)
    for key, leftovers in unmapped.items():
        if not leftovers:
            continue
        print(f"\n{key}: {len(leftovers)} packet(s) not modelled yet:", file=sys.stderr)
        for name, packet_id in leftovers:
            print(f"  0x{packet_id:02X}  {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
