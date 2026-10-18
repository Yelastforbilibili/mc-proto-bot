"""Hardness and tool requirements per block, per release.

Mojang's ``reports/blocks.json`` lists every block and every state of it but says
nothing about how long it takes to break: hardness, the tool that speeds it up
and the tier needed to *harvest* it live in vanilla's code.  minecraft-data
publishes all three, so this table is generated from there (see
``tools/gen_break_times.py``) and joined by block name onto the block ids of the
release's own registry.  A release minecraft-data does not cover yet inherits the
rows by name and records that in ``source``, because inventing a hardness would be
worse than saying where the number came from.

The unit is what vanilla uses: ``hardness`` is the "hardness" number (dirt 0.5,
stone 1.5, obsidian 50, bedrock -1 for unbreakable) and ``ceil(hardness * 30 /
speed)`` is the number of ticks to hold the button with the right tool.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .items import normalise_identifier
from .tools import (  # noqa: F401  (tier_for_tools is re-exported for the generator)
    break_seconds,
    best_tool,
    tier_for_tools,
    tool_spec,
)

DATA_DIRECTORY = Path(__file__).resolve().parent.parent / "data"

#: ``material`` values from minecraft-data that name a tool kind.  Compound
#: values ("leaves;mineable/hoe") still carry the tool after the last slash.
_TOOL_MATERIALS = ("pickaxe", "axe", "shovel", "hoe")


def material_tool(material: Any) -> str | None:
    """The tool kind a minecraft-data ``material`` implies, if any."""

    if not isinstance(material, str):
        return None
    for part in material.split(";"):
        kind = part.rsplit("/", 1)[-1]
        if kind in _TOOL_MATERIALS:
            return kind
    return None


class BreakTable:
    """``hardness``/``tool``/``tier`` per block identifier for one release."""

    __slots__ = ("version", "protocol", "blocks", "source")

    def __init__(
        self,
        blocks: Mapping[str, Mapping[str, Any]],
        *,
        version: str = "",
        protocol: int = 0,
        source: str = "",
    ) -> None:
        self.version = version
        self.protocol = protocol
        self.source = source
        self.blocks: dict[str, dict[str, Any]] = {}
        for name, row in blocks.items():
            record: dict[str, Any] = {"hardness": float(row["hardness"])}
            tool = row.get("tool")
            if tool:
                record["tool"] = str(tool)
            if row.get("requires"):
                # "requires" is not the same as "has a tier": dirt is mined
                # fastest with a shovel but drops by hand, stone needs *a*
                # pickaxe (tier 0), obsidian needs diamond (tier 3).
                record["requires"] = True
                record["tier"] = int(row.get("tier", 0))
            self.blocks[normalise_identifier(name)] = record

    def __len__(self) -> int:
        return len(self.blocks)

    def __contains__(self, identifier: object) -> bool:
        if not isinstance(identifier, str):
            return False
        return normalise_identifier(identifier) in self.blocks

    def row(self, identifier: str) -> dict[str, Any] | None:
        """The raw row for a block, or ``None`` when the block is unknown."""

        return self.blocks.get(normalise_identifier(identifier))

    def hardness(self, identifier: str) -> float | None:
        """Hardness of a block, ``None`` when unknown, ``-1`` when unbreakable."""

        row = self.row(identifier)
        return None if row is None else float(row["hardness"])

    def tool(self, identifier: str) -> str | None:
        """The tool kind that speeds this block up, if any."""

        row = self.row(identifier)
        return None if row is None else row.get("tool")

    def tier(self, identifier: str) -> int:
        """Minimum tool tier needed to get drops (0 when any tool will do)."""

        row = self.row(identifier)
        return 0 if row is None else int(row.get("tier", 0))

    def requires_tool(self, identifier: str) -> bool:
        """Whether the block only drops when mined with the right tool."""

        row = self.row(identifier)
        return False if row is None else bool(row.get("requires"))

    def seconds(self, identifier: str, *, tool: str | None = None) -> float | None:
        """How long ``tool`` (an item identifier, or ``None`` for a bare hand)
        needs to break the block, in seconds.

        Vanilla uses the tool's speed multiplier whenever the tool is the right
        *kind* for the block, and only the "correct tool" bonus (the divisor of
        30 instead of 100) depends on the tier actually being high enough -- so
        a gold pickaxe is fast on obsidian and still gets nothing.
        """

        row = self.row(identifier)
        if row is None:
            return None
        hardness = float(row["hardness"])
        speed = 1.0
        can_harvest = not row.get("requires", False)
        spec = tool_spec(tool) if tool is not None else None
        if spec is not None and spec.kind == row.get("tool"):
            speed = spec.speed
            if can_harvest or spec.tier >= int(row.get("tier", 0)):
                can_harvest = True
        return break_seconds(hardness, speed=speed, can_harvest=can_harvest)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"BreakTable(version={self.version!r}, blocks={len(self)})"

    # -- construction ----------------------------------------------------

    @classmethod
    def from_minecraft_data(
        cls, payload, *, version: str = "", protocol: int = 0
    ) -> BreakTable:
        """Build from minecraft-data's ``blocks.json`` (a list of blocks)."""

        blocks: dict[str, dict[str, Any]] = {}
        for entry in payload:
            name = entry.get("name")
            hardness = entry.get("hardness")
            if not name or hardness is None:
                continue
            row: dict[str, Any] = {"hardness": float(hardness)}
            tool = material_tool(entry.get("material"))
            if tool:
                row["tool"] = tool
            blocks[str(name)] = row
        return cls(blocks, version=version, protocol=protocol, source="minecraft-data")

    @classmethod
    def load(cls, path: Path | str) -> BreakTable:
        path = Path(path)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
            payload = json.load(handle)
        return cls(
            payload.get("blocks", {}),
            version=str(payload.get("version", "")),
            protocol=int(payload.get("protocol", 0)),
            source=str(payload.get("source", path.name)),
        )

    def dump(
        self, path: Path | str, *, version: str, protocol: int, source: str
    ) -> None:
        """Write the table in the format :meth:`load` reads."""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": version,
            "protocol": protocol,
            "source": source,
            "blocks": dict(sorted(self.blocks.items())),
        }
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "wt", encoding="utf-8") as handle:  # type: ignore[operator]
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)


_CACHE: dict[int, BreakTable | None] = {}


def bundled_path(protocol: int) -> Path:
    """Path of the bundled breaking table for ``protocol``."""

    return DATA_DIRECTORY / f"breaking-{protocol}.json.gz"


def table_for(protocol: int) -> BreakTable | None:
    """Return the bundled breaking table for ``protocol``, loading it once."""

    try:
        return _CACHE[protocol]
    except KeyError:
        pass
    path = bundled_path(protocol)
    table = BreakTable.load(path) if path.exists() else None
    _CACHE[protocol] = table
    return table


def clear_cache() -> None:
    """Forget cached tables (tests that regenerate data use this)."""

    _CACHE.clear()
