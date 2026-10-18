"""Item registry IDs for every supported release.

Minecraft sends items as *registry IDs*, so anything that reasons about the
player's inventory -- "is the held item the dirt I was asked to place?" --
needs the ID-to-name table of that exact release.  Guessing it is worse than
useless: IDs shift between releases (dirt is 28 on 1.21.8, 55 on 26.3), and a
mismatch silently picks the wrong item.

The tables are generated, not hand written: see ``tools/gen_item_ids.py``.
Each release gets ``protobot/data/items-<protocol>.json.gz`` containing
``{"version": ..., "protocol": ..., "items": {"minecraft:dirt": 55, ...}}``.

Protocol 775 is shared by 26.1, 26.1.1 and 26.1.2 whose registries are
identical, so one table covers all three.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

DATA_DIRECTORY = Path(__file__).resolve().parent.parent / "data"

#: ``"minecraft:"`` is implied for bare names, matching the rest of the library.
NAMESPACE = "minecraft:"


def normalise_identifier(identifier: str) -> str:
    """Return ``identifier`` with an explicit ``minecraft:`` namespace."""

    identifier = identifier.strip()
    if not identifier:
        raise ValueError("item identifier must not be empty")
    return identifier if ":" in identifier else NAMESPACE + identifier


class ItemTable:
    """The item registry of one release, in both directions."""

    __slots__ = ("version", "protocol", "by_name", "by_id", "source")

    def __init__(
        self,
        items: Mapping[str, int],
        *,
        version: str = "",
        protocol: int = 0,
        source: str = "",
    ) -> None:
        self.version = version
        self.protocol = protocol
        self.source = source
        self.by_name: dict[str, int] = {
            normalise_identifier(name): int(item_id) for name, item_id in items.items()
        }
        self.by_id: dict[int, str] = {
            item_id: name for name, item_id in self.by_name.items()
        }

    def __len__(self) -> int:
        return len(self.by_name)

    def __contains__(self, identifier: object) -> bool:
        if not isinstance(identifier, str):
            return False
        return normalise_identifier(identifier) in self.by_name

    def identifier(self, registry_id: int) -> str | None:
        """Return the item name for ``registry_id``, or ``None``."""

        return self.by_id.get(registry_id)

    def item_id(self, identifier: str) -> int | None:
        """Return the registry ID for ``identifier``, or ``None``."""

        return self.by_name.get(normalise_identifier(identifier))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ItemTable(version={self.version!r}, items={len(self)})"

    # -- construction ----------------------------------------------------

    @classmethod
    def from_report(
        cls,
        report: Mapping[str, Any],
        *,
        registry: str = "minecraft:item",
        version: str = "",
        protocol: int = 0,
    ) -> ItemTable:
        """Build from a Mojang ``registries.json`` report (26.x and later)."""

        entries = report[registry]["entries"]
        items = {
            name: entry["protocol_id"]
            for name, entry in entries.items()
            if "protocol_id" in entry
        }
        return cls(items, version=version, protocol=protocol, source="reports")

    @classmethod
    def from_minecraft_data(
        cls,
        items: Iterable[Mapping[str, Any]],
        *,
        version: str = "",
        protocol: int = 0,
    ) -> ItemTable:
        """Build from minecraft-data's ``items.json`` (1.21.x and earlier)."""

        table = {
            str(entry["name"]): int(entry["id"])
            for entry in items
            if "id" in entry and "name" in entry
        }
        return cls(table, version=version, protocol=protocol, source="minecraft-data")

    @classmethod
    def load(cls, path: Path | str) -> ItemTable:
        path = Path(path)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
            payload = json.load(handle)
        return cls(
            payload.get("items", {}),
            version=str(payload.get("version", "")),
            protocol=int(payload.get("protocol", 0)),
            source=str(payload.get("source", path.name)),
        )

    def dump(self, path: Path | str, *, version: str, protocol: int, source: str) -> None:
        """Write the table in the format :meth:`load` reads."""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": version,
            "protocol": protocol,
            "source": source,
            "items": dict(sorted(self.by_name.items())),
        }
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "wt", encoding="utf-8") as handle:  # type: ignore[operator]
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)


_CACHE: dict[int, ItemTable | None] = {}


def bundled_path(protocol: int) -> Path:
    """Path of the bundled table for ``protocol``."""

    return DATA_DIRECTORY / f"items-{protocol}.json.gz"


def table_for(protocol: int) -> ItemTable | None:
    """Return the bundled item table for ``protocol``, loading it once.

    ``None`` means the release ships no table; callers must then treat item
    identity as unknown instead of inventing one.
    """

    try:
        return _CACHE[protocol]
    except KeyError:
        pass
    path = bundled_path(protocol)
    table = ItemTable.load(path) if path.exists() else None
    _CACHE[protocol] = table
    return table


def clear_cache() -> None:
    """Forget cached tables (tests that regenerate data use this)."""

    _CACHE.clear()
