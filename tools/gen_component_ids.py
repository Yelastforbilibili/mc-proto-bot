#!/usr/bin/env python3
"""Generate the data component type tables bundled with the bot.

Usage::

    python tools/gen_component_ids.py --reports generated/reports/registries.json \
        --version 26.3 --protocol 777 --out protobot/data/components-777.json.gz

The ID list is only half of what a stack decoder needs: the *value* of each
component is not self delimiting, so ``protobot.protocol.component_codecs``
knows the packed kind of the components it can read.  This tool therefore also
prints, to stderr, every component whose value it cannot read yet, so the gap is
visible instead of surprising.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protobot.protocol.component_codecs import KINDS_BY_NAME  # noqa: E402
from protobot.protocol.components import ComponentTable  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reports", help="Mojang registries.json")
    parser.add_argument("--version", default="", help="release name, e.g. 26.3")
    parser.add_argument("--protocol", type=int, default=0, help="protocol number")
    parser.add_argument("--out", help="output .json.gz")
    parser.add_argument(
        "--only-known",
        action="store_true",
        help="keep only components whose value this library can decode",
    )
    args = parser.parse_args(argv)

    if not args.reports:
        parser.error("--reports is required")
    report = json.loads(Path(args.reports).read_text(encoding="utf-8"))
    table = ComponentTable.from_report(
        report, version=args.version, protocol=args.protocol
    )

    known = {
        name: component_id
        for name, component_id in table.by_name.items()
        if name in KINDS_BY_NAME
    }
    unknown = sorted(set(table.by_name) - set(known))
    if args.only_known:
        table = ComponentTable(known, version=args.version, protocol=args.protocol)
    if args.out:
        table.dump(
            args.out,
            version=args.version,
            protocol=args.protocol,
            source="reports",
        )
        print(f"wrote {args.out} ({len(table)} components, {len(known)} decodable)")
    for name in unknown:
        print(f"unknown component: {name}", file=sys.stderr)
    if unknown and not sys.stderr.isatty():
        print(
            f"{len(unknown)} component(s) have no value codec yet; "
            "a stack carrying one cannot be decoded exactly",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
