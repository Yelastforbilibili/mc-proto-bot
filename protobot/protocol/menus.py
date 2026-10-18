"""Menu (container window) registry IDs for every supported release.

``clientbound_open_screen`` names the window with a *menu type ID*, not a name:
26.3 sends ``2`` for a chest, and only the release's ``minecraft:menu``
registry knows that ``2`` is ``minecraft:generic_9x3``.  That name is what tells
the container layer which slot means "the output" and which slots belong to the
player, so it has to come from the exact release's data rather than a guess.

The tables are generated, not hand written: see ``tools/gen_menu_ids.py``.
Each release gets ``protobot/data/menus-<protocol>.json.gz`` containing
``{"version": ..., "protocol": ..., "menus": {"minecraft:generic_9x3": 2, ...}}``.

Releases whose data carries no menu registry (the 1.21.x ``minecraft-data``
dumps) ship no table; :meth:`VersionSpec.menu_identifier` then returns ``None``
and the container layer falls back to a layout derived from the slot count.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Mapping

from .items import normalise_identifier

DATA_DIRECTORY = Path(__file__).resolve().parent.parent / "data"


class MenuTable:
    """The menu registry of one release, in both directions."""

    __slots__ = ("version", "protocol", "by_name", "by_id", "source")

    def __init__(
        self,
        menus: Mapping[str, int],
        *,
        version: str = "",
        protocol: int = 0,
        source: str = "",
    ) -> None:
        self.version = version
        self.protocol = protocol
        self.source = source
        self.by_name: dict[str, int] = {
            normalise_identifier(name): int(menu_id) for name, menu_id in menus.items()
        }
        self.by_id: dict[int, str] = {
            menu_id: name for name, menu_id in self.by_name.items()
        }

    def __len__(self) -> int:
        return len(self.by_name)

    def __contains__(self, identifier: object) -> bool:
        if not isinstance(identifier, str):
            return False
        return normalise_identifier(identifier) in self.by_name

    def identifier(self, registry_id: int) -> str | None:
        """Return the menu name for ``registry_id``, or ``None``."""

        return self.by_id.get(registry_id)

    def menu_id(self, identifier: str) -> int | None:
        """Return the registry ID for ``identifier``, or ``None``."""

        return self.by_name.get(normalise_identifier(identifier))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MenuTable(version={self.version!r}, menus={len(self)})"

    # -- construction ----------------------------------------------------

    @classmethod
    def from_report(
        cls,
        report: Mapping[str, Any],
        *,
        registry: str = "minecraft:menu",
        version: str = "",
        protocol: int = 0,
    ) -> MenuTable:
        """Build from a Mojang ``registries.json`` report (26.x and later)."""

        entries = report[registry]["entries"]
        menus = {
            name: entry["protocol_id"]
            for name, entry in entries.items()
            if "protocol_id" in entry
        }
        return cls(menus, version=version, protocol=protocol, source="reports")

    @classmethod
    def load(cls, path: Path | str) -> MenuTable:
        path = Path(path)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
            payload = json.load(handle)
        return cls(
            payload.get("menus", {}),
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
            "menus": dict(sorted(self.by_name.items())),
        }
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "wt", encoding="utf-8") as handle:  # type: ignore[operator]
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)


_CACHE: dict[int, MenuTable | None] = {}


def bundled_path(protocol: int) -> Path:
    """Path of the bundled menu table for ``protocol``."""

    return DATA_DIRECTORY / f"menus-{protocol}.json.gz"


def table_for(protocol: int) -> MenuTable | None:
    """Return the bundled menu table for ``protocol``, loading it once."""

    try:
        return _CACHE[protocol]
    except KeyError:
        pass
    path = bundled_path(protocol)
    table = MenuTable.load(path) if path.exists() else None
    _CACHE[protocol] = table
    return table


def clear_cache() -> None:
    """Forget cached tables (tests that regenerate data use this)."""

    _CACHE.clear()
