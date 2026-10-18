"""Data component type registry IDs for every supported release.

An item stack on the wire ends with a *component patch*: a list of data
component type IDs and their values.  The values are not self delimiting (see
:mod:`protobot.protocol.stacks`), so the only way to read a stack that carries
components is to know how many bytes each component takes.  That knowledge is
keyed by the component type IDs of the exact release, which is what this table
provides.

The tables are generated, not hand written: see ``tools/gen_component_ids.py``.
Each release gets ``protobot/data/components-<protocol>.json.gz`` containing
``{"version": ..., "protocol": ..., "components": {"minecraft:damage": 3, ...}}``.

The packed kind of each known component (varint, enchantment map, ...) lives in
:mod:`protobot.protocol.component_codecs`, keyed by name so that an ID moving
between releases cannot silently change the meaning of a value.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Mapping

from .items import normalise_identifier

DATA_DIRECTORY = Path(__file__).resolve().parent.parent / "data"


class ComponentTable:
    """The data component type registry of one release, in both directions."""

    __slots__ = ("version", "protocol", "by_name", "by_id", "source")

    def __init__(
        self,
        components: Mapping[str, int],
        *,
        version: str = "",
        protocol: int = 0,
        source: str = "",
    ) -> None:
        self.version = version
        self.protocol = protocol
        self.source = source
        self.by_name: dict[str, int] = {
            normalise_identifier(name): int(component_id)
            for name, component_id in components.items()
        }
        self.by_id: dict[int, str] = {
            component_id: name for name, component_id in self.by_name.items()
        }

    def __len__(self) -> int:
        return len(self.by_name)

    def __contains__(self, identifier: object) -> bool:
        if not isinstance(identifier, str):
            return False
        return normalise_identifier(identifier) in self.by_name

    def identifier(self, component_id: int) -> str | None:
        """Return the component name for ``component_id``, or ``None``."""

        return self.by_id.get(component_id)

    def component_id(self, identifier: str) -> int | None:
        """Return the registry ID for ``identifier``, or ``None``."""

        return self.by_name.get(normalise_identifier(identifier))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ComponentTable(version={self.version!r}, components={len(self)})"

    # -- construction ----------------------------------------------------

    @classmethod
    def from_report(
        cls,
        report: Mapping[str, Any],
        *,
        registry: str = "minecraft:data_component_type",
        version: str = "",
        protocol: int = 0,
    ) -> ComponentTable:
        """Build from a Mojang ``registries.json`` report."""

        entries = report[registry]["entries"]
        components = {
            name: entry["protocol_id"]
            for name, entry in entries.items()
            if "protocol_id" in entry
        }
        return cls(components, version=version, protocol=protocol, source="reports")

    @classmethod
    def load(cls, path: Path | str) -> ComponentTable:
        path = Path(path)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
            payload = json.load(handle)
        return cls(
            payload.get("components", {}),
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
            "components": dict(sorted(self.by_name.items())),
        }
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "wt", encoding="utf-8") as handle:  # type: ignore[operator]
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)


_CACHE: dict[int, ComponentTable | None] = {}


def bundled_path(protocol: int) -> Path:
    """Path of the bundled component table for ``protocol``."""

    return DATA_DIRECTORY / f"components-{protocol}.json.gz"


def table_for(protocol: int) -> ComponentTable | None:
    """Return the bundled component table for ``protocol``, loading it once."""

    try:
        return _CACHE[protocol]
    except KeyError:
        pass
    path = bundled_path(protocol)
    table = ComponentTable.load(path) if path.exists() else None
    _CACHE[protocol] = table
    return table


def clear_cache() -> None:
    """Forget cached tables (tests that regenerate data use this)."""

    _CACHE.clear()


__all__ = [
    "ComponentTable",
    "DATA_DIRECTORY",
    "bundled_path",
    "clear_cache",
    "table_for",
]
