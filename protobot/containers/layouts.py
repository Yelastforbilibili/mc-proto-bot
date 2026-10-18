"""Slot maps for the vanilla container menus.

A click is addressed by a raw slot number, and that number means something
different in every window: slot 2 is the output of a furnace, the result of an
anvil, and the third storage slot of a chest.  Hard-coding those offsets at
every call site is how bots put their pickaxe into a furnace's fuel slot.

This module turns the release's own menu name (``minecraft:generic_9x3``) plus
the slot count the server announced into a :class:`MenuLayout`, which maps
logical names to slot numbers:

* ``container`` -- the slots that belong to the block/entity and accept
  deposits (chest storage, hopper storage, furnace input and fuel, crafting
  grid, ...);
* ``result`` -- output slots that only ever receive items from the server;
* ``main``, ``hotbar`` -- the player's own inventory inside the window;
* the individual segment names (``input``, ``fuel``, ``output``, ``grid``,
  ``potions``, ``ingredient``, ``pattern``, ``template``, ...).

The numbers come from the vanilla ``AbstractContainerMenu`` subclasses and are
validated at runtime against the announced slot count: if a menu announces a
different size, :func:`layout_for` falls back to a count-derived layout rather
than returning confidently wrong offsets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: Segments that are the player's own inventory rather than the block's.
PLAYER_SEGMENTS = ("main", "hotbar", "armor", "offhand")

#: Segments that hold server-side output; they never accept a deposit.
RESULT_SEGMENTS = ("result", "output")

#: Every block menu ends with 27 storage slots followed by the 9 hotbar slots.
_PLAYER_TAIL: tuple[tuple[str, int], ...] = (("main", 27), ("hotbar", 9))

#: Segment lists in slot order for each 26.x ``minecraft:menu`` entry.
_SEGMENTS: dict[str, tuple[tuple[str, int], ...]] = {
    "minecraft:generic_9x1": (("container", 9),) + _PLAYER_TAIL,
    "minecraft:generic_9x2": (("container", 18),) + _PLAYER_TAIL,
    "minecraft:generic_9x3": (("container", 27),) + _PLAYER_TAIL,
    "minecraft:generic_9x4": (("container", 36),) + _PLAYER_TAIL,
    "minecraft:generic_9x5": (("container", 45),) + _PLAYER_TAIL,
    "minecraft:generic_9x6": (("container", 54),) + _PLAYER_TAIL,
    "minecraft:generic_3x3": (("container", 9),) + _PLAYER_TAIL,
    # The crafter has a 3x3 grid, then a read-only preview slot at 9 (the
    # server rejects clicks on it), then the player rows.
    "minecraft:crafter_3x3": (("grid", 9), ("result", 1)) + _PLAYER_TAIL,
    "minecraft:anvil": (("input", 2), ("result", 1)) + _PLAYER_TAIL,
    "minecraft:beacon": (("payment", 1),) + _PLAYER_TAIL,
    "minecraft:blast_furnace": (("input", 1), ("fuel", 1), ("output", 1))
    + _PLAYER_TAIL,
    "minecraft:brewing_stand": (("potions", 3), ("ingredient", 1), ("fuel", 1))
    + _PLAYER_TAIL,
    "minecraft:crafting": (("result", 1), ("grid", 9)) + _PLAYER_TAIL,
    "minecraft:enchantment": (("item", 1), ("lapis", 1)) + _PLAYER_TAIL,
    "minecraft:furnace": (("input", 1), ("fuel", 1), ("output", 1)) + _PLAYER_TAIL,
    "minecraft:grindstone": (("input", 2), ("result", 1)) + _PLAYER_TAIL,
    "minecraft:hopper": (("container", 5),) + _PLAYER_TAIL,
    "minecraft:lectern": (("book", 1),) + _PLAYER_TAIL,
    "minecraft:loom": (
        ("banner", 1),
        ("dye", 1),
        ("pattern", 1),
        ("result", 1),
    )
    + _PLAYER_TAIL,
    "minecraft:merchant": (("input", 2), ("result", 1)) + _PLAYER_TAIL,
    "minecraft:shulker_box": (("container", 27),) + _PLAYER_TAIL,
    "minecraft:smithing": (
        ("template", 1),
        ("base", 1),
        ("addition", 1),
        ("result", 1),
    )
    + _PLAYER_TAIL,
    "minecraft:smoker": (("input", 1), ("fuel", 1), ("output", 1)) + _PLAYER_TAIL,
    "minecraft:cartography_table": (("map", 1), ("paper", 1), ("result", 1))
    + _PLAYER_TAIL,
    "minecraft:stonecutter": (("input", 1), ("result", 1)) + _PLAYER_TAIL,
    # The player inventory is container 0 and is never "opened"; the server
    # still sends its content, and crafting a 2x2 recipe happens here.
    "minecraft:inventory": (
        ("result", 1),
        ("grid", 4),
        ("armor", 4),
        ("main", 27),
        ("hotbar", 9),
        ("offhand", 1),
    ),
    # The player's own menu in older releases is called "minecraft:crafting".
    "minecraft:unknown": (),
}


@dataclass(frozen=True, slots=True)
class MenuLayout:
    """Logical slot names for one open menu."""

    identifier: str
    slot_count: int
    groups: Mapping[str, tuple[int, ...]]
    #: ``True`` when nothing identified the menu and slots were derived only
    #: from the announced count -- the names are then a heuristic.
    derived: bool = False

    def __contains__(self, slot: object) -> bool:
        return isinstance(slot, int) and 0 <= slot < self.slot_count

    def __len__(self) -> int:
        return self.slot_count

    def slots(self, name: str) -> tuple[int, ...]:
        """Return the slot numbers of one segment.

        ``container``, ``result``, ``player`` and ``all`` are always present;
        segment names raise :class:`KeyError` when the menu has none.
        """

        try:
            return self.groups[name]
        except KeyError:
            raise KeyError(
                f"{self.identifier} has no {name!r} slots; it has "
                f"{', '.join(sorted(self.groups)) or 'none'}"
            ) from None

    def slot(self, name: str) -> int:
        """:meth:`slots` for a segment that holds exactly one slot."""

        slots = self.slots(name)
        if len(slots) != 1:
            raise ValueError(f"{self.identifier} {name!r} segment holds {len(slots)} slots")
        return slots[0]

    def name_of(self, slot: int) -> str | None:
        """Return the segment name containing ``slot``, or ``None``."""

        for name, slots in self.groups.items():
            if slot in slots:
                return name
        return None

    @property
    def container(self) -> tuple[int, ...]:
        return self.groups.get("container", ())

    @property
    def result(self) -> tuple[int, ...]:
        return self.groups.get("result", ())

    @property
    def player(self) -> tuple[int, ...]:
        return self.groups.get("player", ())

    @property
    def main(self) -> tuple[int, ...]:
        return self.groups.get("main", ())

    @property
    def hotbar(self) -> tuple[int, ...]:
        return self.groups.get("hotbar", ())

    def describe(self) -> str:
        parts = [
            f"{name}={slots[0]}..{slots[-1]}"
            for name, slots in sorted(
                (pair for pair in self.groups.items() if pair[1]),
                key=lambda pair: pair[1][0],
            )
            if name in ("container", "result", "grid", "input", "output", "main")
        ]
        return f"{self.identifier} ({self.slot_count} slots: {', '.join(parts)})"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MenuLayout({self.describe()})"


def _finish(
    identifier: str,
    segments: tuple[tuple[str, int], ...],
    *,
    derived: bool = False,
) -> MenuLayout:
    groups: dict[str, tuple[int, ...]] = {}
    offset = 0
    for name, count in segments:
        groups[name] = tuple(range(offset, offset + count))
        offset += count
    slot_count = offset

    container = tuple(
        sorted(
            slot
            for name, slots in groups.items()
            if name not in PLAYER_SEGMENTS and name not in RESULT_SEGMENTS
            for slot in slots
        )
    )
    if identifier == "minecraft:inventory":
        # The player's own menu owns no storage: its "grid" is the 2x2
        # crafting area, which is not a deposit target.
        container = ()
    result = tuple(
        sorted(slot for name in RESULT_SEGMENTS for slot in groups.get(name, ()))
    )
    # "player" is the 36 storage slots (27 main + 9 hotbar).  Armour and the
    # off hand keep their own names: they are not general deposit targets.
    player = tuple(
        sorted(slot for name in ("main", "hotbar") for slot in groups.get(name, ()))
    )
    groups["container"] = container
    groups["result"] = result
    groups["player"] = player
    groups["all"] = tuple(range(slot_count))
    return MenuLayout(identifier, slot_count, groups, derived)


def _derived(slot_count: int, *, identifier: str = "minecraft:unknown") -> MenuLayout:
    """Build a layout from the announced slot count alone.

    Every window the server can open ends with the player's 36 storage slots,
    so everything before them belongs to the block.  This is a guess, and the
    layout says so through :attr:`MenuLayout.derived`.
    """

    slot_count = max(0, int(slot_count))
    if slot_count >= 36:
        block = slot_count - 36
        segments: tuple[tuple[str, int], ...] = (("container", block),) + _PLAYER_TAIL
    else:
        segments = (("container", slot_count),)
    return _finish(identifier, segments, derived=True)


def known_identifiers() -> tuple[str, ...]:
    """Every menu name this module knows a slot map for."""

    return tuple(sorted(name for name in _SEGMENTS if name != "minecraft:unknown"))


def layout_for(identifier: str | None, slot_count: int | None = None) -> MenuLayout:
    """Return the layout for a menu name/``slot_count`` pair.

    When the name is unknown, or the announced slot count disagrees with the
    known map, the count-derived layout is returned (and
    :attr:`MenuLayout.derived` is set) instead of a confidently wrong one.
    """

    identifier = identifier or "minecraft:unknown"
    segments = _SEGMENTS.get(identifier)
    if segments:
        known = sum(count for _, count in segments)
        if slot_count is None or slot_count == known:
            return _finish(identifier, segments)
    if slot_count is None:
        return _derived(0, identifier=identifier)
    return _derived(slot_count, identifier=identifier)
