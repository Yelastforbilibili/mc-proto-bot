"""The entity-type registry of a release, with the size a hitbox needs.

Entity *ids* come from the release's own ``registries.json`` so they cannot
drift; entity *dimensions* do not exist in Mojang's reports, so they are joined
by name from minecraft-data and stored alongside.  A type with no measured size
keeps ``None`` -- callers must then fall back to a documented default instead of
pretending the entity is a player-sized box.

The table is what makes an entity raycast possible: aiming at a cow and aiming
at a boat need different boxes, and the box is ``width`` x ``height`` around the
feet position the server sends, which is exactly what vanilla uses.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .items import normalise_identifier

DATA_DIRECTORY = Path(__file__).resolve().parent.parent / "data"

DEFAULT_WIDTH = 0.6
DEFAULT_HEIGHT = 1.8


class EntityTable:
    """Entity types of one release: name to id, and the measured size."""

    __slots__ = ("version", "protocol", "by_name", "by_id", "source")

    def __init__(
        self,
        entities: Mapping[str, Mapping[str, Any]],
        *,
        version: str = "",
        protocol: int = 0,
        source: str = "",
    ) -> None:
        self.version = version
        self.protocol = protocol
        self.source = source
        self.by_name: dict[str, dict[str, Any]] = {}
        for name, entry in entities.items():
            record: dict[str, Any] = {"id": int(entry["id"])}
            width = entry.get("width")
            height = entry.get("height")
            if width is not None and height is not None:
                record["width"] = float(width)
                record["height"] = float(height)
            self.by_name[normalise_identifier(name)] = record
        self.by_id: dict[int, str] = {
            entry["id"]: name for name, entry in self.by_name.items()
        }

    def __len__(self) -> int:
        return len(self.by_name)

    def __contains__(self, identifier: object) -> bool:
        if not isinstance(identifier, str):
            return False
        return normalise_identifier(identifier) in self.by_name

    def identifier(self, registry_id: int) -> str | None:
        """Return the entity name for ``registry_id``, or ``None``."""

        return self.by_id.get(registry_id)

    def entity_id(self, identifier: str) -> int | None:
        """Return the registry ID for ``identifier``, or ``None``."""

        entry = self.by_name.get(normalise_identifier(identifier))
        return None if entry is None else int(entry["id"])

    def size(self, identifier: str | int) -> tuple[float, float] | None:
        """Return ``(width, height)`` for an entity name or id, or ``None``."""

        name = identifier if isinstance(identifier, str) else self.by_id.get(identifier)
        if name is None:
            return None
        entry = self.by_name.get(normalise_identifier(name))
        if entry is None or "width" not in entry:
            return None
        return entry["width"], entry["height"]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EntityTable(version={self.version!r}, types={len(self)})"

    # -- construction ----------------------------------------------------

    @classmethod
    def from_report(
        cls,
        report: Mapping[str, Any],
        *,
        sizes: Mapping[str, tuple[float, float]] | None = None,
        registry: str = "minecraft:entity_type",
        version: str = "",
        protocol: int = 0,
    ) -> EntityTable:
        """Build from a Mojang ``registries.json`` report.

        ``sizes`` is a name to ``(width, height)`` map, normally joined from
        minecraft-data; ids without a size stay unmeasured.
        """

        entries = report[registry]["entries"]
        measured = dict(sizes or {})
        entities: dict[str, dict[str, Any]] = {}
        for name, entry in entries.items():
            if "protocol_id" not in entry:
                continue
            record: dict[str, Any] = {"id": entry["protocol_id"]}
            size = measured.get(normalise_identifier(name))
            if size is not None:
                record["width"], record["height"] = float(size[0]), float(size[1])
            entities[name] = record
        return cls(entities, version=version, protocol=protocol, source="reports")

    @classmethod
    def from_minecraft_data(
        cls,
        entities: Iterable[Mapping[str, Any]],
        *,
        version: str = "",
        protocol: int = 0,
    ) -> EntityTable:
        """Build from minecraft-data's ``entities.json`` (ids and sizes)."""

        table: dict[str, dict[str, Any]] = {}
        for entry in entities:
            name = entry.get("name")
            if name is None or "id" not in entry:
                continue
            record: dict[str, Any] = {"id": int(entry["id"])}
            if "width" in entry and "height" in entry:
                record["width"] = float(entry["width"])
                record["height"] = float(entry["height"])
            table[str(name)] = record
        return cls(table, version=version, protocol=protocol, source="minecraft-data")

    @classmethod
    def load(cls, path: Path | str) -> EntityTable:
        path = Path(path)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
            payload = json.load(handle)
        return cls(
            payload.get("entities", {}),
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
            "entities": dict(sorted(self.by_name.items())),
        }
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "wt", encoding="utf-8") as handle:  # type: ignore[operator]
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)


_CACHE: dict[int, EntityTable | None] = {}


def bundled_path(protocol: int) -> Path:
    """Path of the bundled table for ``protocol``."""

    return DATA_DIRECTORY / f"entities-{protocol}.json.gz"


def table_for(protocol: int) -> EntityTable | None:
    """Return the bundled entity table for ``protocol``, loading it once."""

    try:
        return _CACHE[protocol]
    except KeyError:
        pass
    path = bundled_path(protocol)
    table = EntityTable.load(path) if path.exists() else None
    _CACHE[protocol] = table
    return table


def clear_cache() -> None:
    """Forget cached tables (tests that regenerate data use this)."""

    _CACHE.clear()


def load_sizes(path: Path | str) -> dict[str, tuple[float, float]]:
    """Read minecraft-data's ``entities.json`` as name to ``(width, height)``."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    sizes: dict[str, tuple[float, float]] = {}
    for entry in payload:
        name = entry.get("name")
        if name is None or "width" not in entry or "height" not in entry:
            continue
        sizes[normalise_identifier(str(name))] = (
            float(entry["width"]),
            float(entry["height"]),
        )
    return sizes
