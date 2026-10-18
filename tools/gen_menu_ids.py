"""Generate the bundled menu (container window) registry tables.

``clientbound_open_screen`` identifies the window by registry ID, so the
container layer needs the release's ``minecraft:menu`` registry to know that
``2`` on 26.3 is a 9x3 chest rather than, say, a furnace.  This tool derives
that table from authoritative data instead of hand-copying it::

    java "-DbundlerMainClass=net.minecraft.data.Main" -jar server.jar --reports
    uv run python tools/gen_menu_ids.py --reports generated/reports/registries.json \\
        --version 26.3 --protocol 777

Only Mojang's ``registries.json`` carries the registry; minecraft-data has no
menu table, so 1.21.x releases are generated here from their own server
bundles.  Skipping a release is safe: the container layer then falls back to a
layout derived from the slot count.

Write to ``protobot/data/menus-<protocol>.json.gz`` (the default when ``--out``
is omitted) and the runtime picks it up automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from protobot.protocol.menus import DATA_DIRECTORY, MenuTable


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True, help="path to registries.json")
    parser.add_argument("--version", required=True, help="release name, e.g. 26.3")
    parser.add_argument("--protocol", type=int, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output file (default: protobot/data/menus-<protocol>.json.gz)",
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help="print the whole table so it can be eyeballed",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = json.loads(args.reports.read_text(encoding="utf-8"))
    table = MenuTable.from_report(report, version=args.version, protocol=args.protocol)
    if not table:
        print("no menus were resolved from the report", file=sys.stderr)
        return 1

    out = args.out or DATA_DIRECTORY / f"menus-{args.protocol}.json.gz"
    table.dump(out, version=args.version, protocol=args.protocol, source=table.source)
    print(f"wrote {out} ({len(table)} menus) from {table.source}", flush=True)

    if args.emit:
        for name, menu_id in sorted(table.by_name.items(), key=lambda pair: pair[1]):
            print(f"  {menu_id:>2} -> {name}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
