"""What a tool is worth: which blocks it speeds up, and how fast.

Vanilla decides a block's break time from three numbers: the block's hardness,
whether the held item is the *right kind* of tool for it, and the tool's speed
multiplier.  The kinds and multipliers are fixed vanilla constants (a wooden
pickaxe is 2, an iron one 6, gold 12 but only as good as wood when it comes to
what it can harvest), so they live here as data rather than being guessed at each
call site.

Tiers exist because *harvesting* and *speeding up* are different questions:
gold is the fastest low-tier tool and cannot harvest iron ore; a netherite pickaxe
is slightly faster than diamond and harvests everything diamond does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .items import normalise_identifier

#: ``prefix -> (tier, speed)``.  A tool's tier caps the *minimum* tier of the
#: blocks it can harvest; speed is the vanilla multiplier against a matching
#: block.
TOOL_TIERS: dict[str, tuple[int, float]] = {
    "wooden": (0, 2.0),
    "golden": (0, 12.0),
    "stone": (1, 4.0),
    "iron": (2, 6.0),
    "diamond": (3, 8.0),
    "netherite": (4, 9.0),
}

#: Kinds that exist, i.e. the ``material`` values from minecraft-data mapped to a
#: name that matches the tool suffix.
KINDS = ("pickaxe", "axe", "shovel", "hoe", "sword")


@dataclass(frozen=True)
class ToolSpec:
    """One tool item: what it is, what tier, and its speed multiplier."""

    kind: str
    tier: int
    speed: float

    def can_harvest(self, required: int) -> bool:
        return self.tier >= required


#: Tools whose name does not follow the ``<tier>_<kind>`` pattern.
_SPECIALS: dict[str, ToolSpec] = {
    "minecraft:shears": ToolSpec("shears", 0, 5.0),
}


def tool_spec(identifier: str) -> ToolSpec | None:
    """Return the :class:`ToolSpec` for an item, or ``None`` if it is no tool."""

    name = normalise_identifier(identifier)
    special = _SPECIALS.get(name)
    if special is not None:
        return special
    bare = name.split(":", 1)[-1]
    if "_" not in bare:
        return None
    prefix, kind = bare.split("_", 1)
    if kind not in KINDS:
        return None
    tier_and_speed = TOOL_TIERS.get(prefix)
    if tier_and_speed is None:
        return None
    tier, speed = tier_and_speed
    return ToolSpec(kind, tier, speed)


def tier_for_tools(identifiers) -> int | None:
    """The minimum tool tier among ``identifiers`` (the easiest way to harvest).

    Used when reading minecraft-data's ``harvestTools``: if a block lists only
    diamond and netherite pickaxes, it needs tier 3.
    """

    tiers = [
        spec.tier
        for spec in (tool_spec(identifier) for identifier in identifiers)
        if spec is not None
    ]
    return min(tiers) if tiers else None


def best_tool(
    slots, *, kind: str | None, tier: int = 0
) -> tuple[int, str, ToolSpec] | None:
    """Pick the fastest usable tool for a block out of ``slots``.

    ``slots`` is an iterable of ``(slot, item_identifier)``; the return value is
    ``(slot, identifier, spec)`` or ``None`` when nothing in the inventory is the
    right kind or good enough.  A tool that cannot harvest the block is still a
    candidate when nothing better exists -- the caller decides, which is why the
    tier check is separate from the speed choice.
    """

    best: tuple[int, str, ToolSpec] | None = None
    for slot, identifier in slots:
        spec = tool_spec(identifier)
        if spec is None or kind is None or spec.kind != kind:
            continue
        if not spec.can_harvest(tier):
            continue
        if best is None or spec.speed > best[2].speed:
            best = (slot, normalise_identifier(identifier), spec)
    return best


def break_seconds(
    hardness: float, *, speed: float = 1.0, can_harvest: bool = True
) -> float:
    """Seconds of holding the button, following vanilla's own formula.

    ``damage_per_tick = speed / hardness / (can_harvest ? 30 : 100)`` and the
    block breaks once ``damage`` has accumulated past 1, so the tick count is
    ``ceil(hardness * 30 / speed)`` (or ``* 100`` when the tool is wrong).
    """

    if hardness < 0:
        return math.inf
    if hardness == 0:
        return 0.0
    factor = 30.0 if can_harvest else 100.0
    damage = speed / hardness / factor
    if damage >= 1.0:
        return 0.0
    return math.ceil(1.0 / damage) / 20.0
