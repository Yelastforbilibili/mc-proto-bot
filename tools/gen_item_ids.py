"""Generate the bundled item registry tables.

The client decodes items as registry IDs, so resolving "what is this item?"
needs a per-release ID table.  Rather than hand-copying 1400 entries, this
tool derives the table from authoritative data:

* Minecraft 26.x -- the release's own ``registries.json`` report::

      java "-DbundlerMainClass=net.minecraft.data.Main" -jar server.jar --reports
      uv run python tools/gen_item_ids.py --reports generated/reports/registries.json \\
          --version 26.3 --protocol 777

* Minecraft 1.21.x -- minecraft-data's ``items.json`` (the vanilla server
  bundles for those releases are obfuscated, so Mojang's ``--reports`` cannot
  be produced without Paper's Mojang-mapped jar)::

      uv run python tools/gen_item_ids.py --mcdata items.json \\
          --version 1.21.8 --protocol 772

Protocols 775 is shared by 26.1, 26.1.1 and 26.1.2; their registries are
identical, so one table serves all three.  Write to
``protobot/data/items-<protocol>.json.gz`` (the default when ``--out`` is
omitted) and the runtime picks it up automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from protobot.protocol.items import DATA_DIRECTORY, ItemTable


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--reports", type=Path, help="path to registries.json")
    source.add_argument("--mcdata", type=Path, help="path to minecraft-data items.json")
    parser.add_argument("--version", required=True, help="release name, e.g. 26.3")
    parser.add_argument("--protocol", type=int, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output file (default: protobot/data/items-<protocol>.json.gz)",
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help="print a few reference items so the table can be eyeballed",
    )
    return parser.parse_args(argv)


def _build(args: argparse.Namespace) -> ItemTable:
    if args.reports is not None:
        report = json.loads(args.reports.read_text(encoding="utf-8"))
        return ItemTable.from_report(
            report, version=args.version, protocol=args.protocol
        )
    items = json.loads(args.mcdata.read_text(encoding="utf-8"))
    return ItemTable.from_minecraft_data(
        items, version=args.version, protocol=args.protocol
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    table = _build(args)
    if not table:
        print("no items were resolved from the source", file=sys.stderr)
        return 1

    out = args.out or DATA_DIRECTORY / f"items-{args.protocol}.json.gz"
    table.dump(
        out,
        version=args.version,
        protocol=args.protocol,
        source=table.source,
    )
    print(f"wrote {out} ({len(table)} items) from {table.source}", flush=True)

    if args.emit:
        for name in ("minecraft:dirt", "minecraft:grass_block", "minecraft:stone"):
            print(f"  {name} -> {table.item_id(name)}", flush=True)
        for item_id in (1, 55, 28):
            print(f"  {item_id} -> {table.identifier(item_id)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
