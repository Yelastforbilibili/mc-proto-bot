"""Generate the bundled entity registry tables.

Entity ids are what the client decodes in spawn packets, and entity hitboxes
are what an entity raycast needs.  Ids come from the release's own
``registries.json``; dimensions come from minecraft-data (Mojang's reports do
not publish them) and are joined by name, so a release new enough to have no
minecraft-data entry still gets correct ids with unmeasured sizes.

* ids from a report::

      uv run python tools/gen_entity_ids.py --reports generated/reports/registries.json \\
          --sizes entities-1.21.11.json --version 26.3 --protocol 777

* everything from minecraft-data::

      uv run python tools/gen_entity_ids.py --mcdata entities-1.21.11.json \\
          --version 1.21.11 --protocol 774

Write to ``protobot/data/entities-<protocol>.json.gz`` (the default when
``--out`` is omitted) and the runtime picks it up automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from protobot.protocol.entities import DATA_DIRECTORY, EntityTable, load_sizes


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--reports", type=Path, help="path to registries.json")
    source.add_argument("--mcdata", type=Path, help="path to minecraft-data entities.json")
    parser.add_argument("--sizes", type=Path, default=None, help="entities.json to join sizes from")
    parser.add_argument("--version", required=True, help="release name, e.g. 26.3")
    parser.add_argument("--protocol", type=int, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output file (default: protobot/data/entities-<protocol>.json.gz)",
    )
    return parser.parse_args(argv)


def _build(args: argparse.Namespace) -> EntityTable:
    if args.reports is not None:
        report = json.loads(args.reports.read_text(encoding="utf-8"))
        sizes = load_sizes(args.sizes) if args.sizes is not None else {}
        return EntityTable.from_report(
            report, sizes=sizes, version=args.version, protocol=args.protocol
        )
    entities = json.loads(args.mcdata.read_text(encoding="utf-8"))
    return EntityTable.from_minecraft_data(
        entities, version=args.version, protocol=args.protocol
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    table = _build(args)
    if not table:
        print("no entity types were resolved from the source", file=sys.stderr)
        return 1

    out = args.out or DATA_DIRECTORY / f"entities-{args.protocol}.json.gz"
    table.dump(out, version=args.version, protocol=args.protocol, source=table.source)
    measured = sum(1 for entry in table.by_name.values() if "width" in entry)
    print(
        f"wrote {out} ({len(table)} types, {measured} measured) from {table.source}",
        flush=True,
    )
    unmeasured = sorted(
        name for name, entry in table.by_name.items() if "width" not in entry
    )
    if unmeasured:
        print(
            f"  {len(unmeasured)} types have no size, e.g. {', '.join(unmeasured[:5])}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
