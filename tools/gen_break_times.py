"""Generate the per-release block breaking table (hardness, tool, tier).

Mojang's ``reports/blocks.json`` says which states a block has but never how long
it takes to break -- hardness and the tool that speeds it up live in code, not in
the data generator.  minecraft-data carries both (``hardness``, ``material``,
``harvestTools``) for the releases it knows, so this joins that onto the block
names of a release:

    python tools/gen_break_times.py --mcdata artifacts/mcdata/1.21.11-blocks.json \
        --version 26.1 --protocol 775 --out protobot/data/breaking-775.json.gz

For a release minecraft-data does not cover (26.x), pass an existing table as
``--fallback``: hardness and tool requirements are stable vanilla values that
change only when a block changes, so the table is joined by block name and the
result says where it came from:

    python tools/gen_break_times.py --reports artifacts/gen-26.3/generated/reports/blocks.json \
        --fallback protobot/data/breaking-776.json.gz \
        --version 26.3 --protocol 777 --out protobot/data/breaking-777.json.gz
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protobot.protocol.breaking import (  # noqa: E402
    BreakTable,
    material_tool,
    tier_for_tools,
)
from protobot.protocol.items import normalise_identifier, table_for  # noqa: E402
from protobot.protocol.tools import tool_spec  # noqa: E402

TOOL_PREFIXES = ("wooden", "stone", "iron", "golden", "diamond", "netherite")


def _tool_tier(identifier: str) -> int | None:
    """Minimum tier encoded by a harvest tool's name, or ``None``."""

    name = normalise_identifier(identifier).split(":", 1)[1]
    if "_" not in name:
        return None
    prefix = name.split("_", 1)[0]
    if prefix not in TOOL_PREFIXES:
        return None
    return tier_for_tools([identifier])


def _rows_from_mcdata(payload: list[dict], protocol: int | None) -> dict[str, dict]:
    items = table_for(protocol) if protocol else None
    rows: dict[str, dict] = {}
    for entry in payload:
        name = entry.get("name")
        if not name:
            continue
        identifier = normalise_identifier(str(name))
        hardness = entry.get("hardness")
        if hardness is None:
            continue
        row: dict = {"hardness": float(hardness)}
        tool = material_tool(entry.get("material"))
        if tool:
            row["tool"] = tool
        harvest = entry.get("harvestTools") or {}
        tools: list[str] = []
        for raw in harvest:
            identifier_of_tool = None
            if items is not None:
                identifier_of_tool = items.identifier(int(raw))
            if identifier_of_tool is None:
                continue
            tools.append(identifier_of_tool)
        if tools:
            tier = tier_for_tools(tools)
            if tier is not None:
                # ``requires`` is what separates "any tool works" (dirt) from
                # "the right tool of at least this tier is needed to get drops"
                # (stone needs *a* pickaxe, obsidian needs diamond).
                row["requires"] = True
                row["tier"] = tier
            if "tool" not in row:
                # minecraft-data's ``material`` is missing for a few blocks whose
                # only tool is a pickaxe (obsidian is the loud one: hardness 50,
                # "default" material, but 9.4 s with a diamond pickaxe).  When
                # every harvesting tool is the same kind, that kind is also the
                # one that speeds the block up.
                kinds = {
                    spec.kind
                    for spec in (tool_spec(identifier) for identifier in tools)
                    if spec is not None
                }
                if len(kinds) == 1:
                    row["tool"] = kinds.pop()
        rows[identifier] = row
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--mcdata", help="minecraft-data blocks.json with hardness")
    source.add_argument("--reports", help="Mojang blocks.json (names only)")
    parser.add_argument("--fallback", help="existing breaking table to join by name")
    parser.add_argument("--items-protocol", type=int, default=None,
                        help="protocol used to resolve harvestTools ids")
    parser.add_argument("--version", required=True)
    parser.add_argument("--protocol", required=True, type=int)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    if args.mcdata:
        payload = json.loads(Path(args.mcdata).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            parser.error("--mcdata must be minecraft-data's blocks.json (a list)")
        rows = _rows_from_mcdata(payload, args.items_protocol or args.protocol)
        origin = f"minecraft-data {Path(args.mcdata).name}"
    else:
        report = json.loads(Path(args.reports).read_text(encoding="utf-8"))
        if args.fallback is None:
            parser.error("--reports only gives block names, so pass --fallback")
        fallback = BreakTable.load(args.fallback)
        rows = {}
        missing = 0
        for raw_name in report:
            identifier = normalise_identifier(str(raw_name))
            row = fallback.row(identifier)
            if row is None:
                missing += 1
                continue
            rows[identifier] = dict(row)
        origin = f"joined from {Path(args.fallback).name}"
        if missing:
            print(f"{missing} blocks have no hardness in the fallback", file=sys.stderr)

    table = BreakTable(rows, version=args.version, protocol=args.protocol, source=origin)
    table.dump(Path(args.out), version=args.version, protocol=args.protocol, source=origin)
    print(f"wrote {args.out}: {len(table)} blocks from {origin}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
