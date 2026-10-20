"""Walking to dropped items and picking them up.

A vanilla client picks a dropped item up when it walks within about a block of
it, and the server then deletes the entity and adds the stack to the inventory.
This module turns that into one call: it enumerates the item entities inside a
sphere, walks to the ones that match, and counts what the *server* put in the
inventory -- never what the local model believes.

The item a dropped entity carries lives in entity metadata index 8, which the
packet router already decodes as an :class:`ItemStack`, so a filter by item name
is a real filter and not a guess.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .state import ItemStack

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Bot

ITEM_ENTITY = "minecraft:item"
ITEM_METADATA_INDEX = 8
PICKUP_RANGE = 1.0
POLL_SECONDS = 0.05


class PickupError(RuntimeError):
    """A pickup that could not be finished, with the report so far."""

    def __init__(self, message: str, report: "PickupReport") -> None:
        super().__init__(message)
        self.report = report


class InventoryFull(PickupError):
    """The inventory has no room left for what was asked for."""


class TooMuchLoss(PickupError):
    """More items are gone than the caller was willing to lose."""


@dataclass(slots=True)
class PickupReport:
    """What a :meth:`PickupMixin.pick_up` call actually achieved."""

    collected: dict[str, int] = field(default_factory=dict)
    lost: int = 0
    wanted: dict[str, int] = field(default_factory=dict)
    visited: int = 0
    seconds: float = 0.0
    ok: bool = False
    reason: str = ""

    def total(self) -> int:
        return sum(self.collected.values())

    def shortfall(self) -> dict[str, int]:
        return {
            name: want - self.collected.get(name, 0)
            for name, want in self.wanted.items()
            if want > self.collected.get(name, 0)
        }

    def describe(self) -> str:
        parts = ", ".join(f"{name} x{count}" for name, count in self.collected.items())
        text = f"picked up {parts or 'nothing'}"
        if self.lost:
            text += f" (lost {self.lost})"
        return f"{text} in {self.seconds:.1f}s"


def _parse_want(
    items: list[str] | None,
    count: int | dict[str, int] | None,
) -> dict[str, int]:
    """Normalise ``(items, count)`` into ``{item: wanted}``, -1 meaning "all"."""

    names = [name for name in (items or []) if name]
    if isinstance(count, dict):
        wanted = {name: int(value) for name, value in count.items()}
        for name in names:
            wanted.setdefault(name, -1)
        return wanted
    amount = -1 if count is None else int(count)
    if not names:
        return {"*": amount}
    return {name: amount for name in names}


class PickupMixin:
    """``bot.pick_up(...)``: collect dropped items by walking to them."""

    # ------------------------------------------------------------------
    # reading the world
    # ------------------------------------------------------------------
    def _item_entity_name(self, entity) -> str | None:
        """The item a dropped-item entity carries, if the metadata arrived."""

        value = entity.metadata.get(ITEM_METADATA_INDEX)
        stack = getattr(value, "value", None)
        if isinstance(stack, ItemStack) and stack.identifier:
            return stack.identifier
        return None

    def dropped_items(
        self: Bot, *, radius: float = 8.0, items: list[str] | None = None
    ) -> list[tuple[float, int, str, int, float, float, float]]:
        """Matching dropped items as ``(distance, entity_id, name, count, x, y, z)``."""

        origin = self.physics_state.position
        wanted = set(items or [])
        found = []
        for entity in self.entities.values():
            name = self._item_entity_name(entity)
            if name is None:
                if entity.type_id is None or self._entity_name(entity) != ITEM_ENTITY:
                    continue
                if wanted:
                    # A filter was asked for and this drop does not say what it
                    # is yet; skipping it is the only honest answer.
                    continue
            elif wanted and name not in wanted:
                continue
            distance = math.dist((origin.x, origin.y, origin.z), (entity.x, entity.y, entity.z))
            if radius is not None and radius >= 0 and distance > radius:
                continue
            stack = getattr(entity.metadata.get(ITEM_METADATA_INDEX), "value", None)
            amount = getattr(stack, "count", 1) if stack is not None else 1
            found.append(
                (distance, entity.entity_id, name or "minecraft:unknown", int(amount), entity.x, entity.y, entity.z)
            )
        found.sort()
        return found

    def _entity_name(self: Bot, entity) -> str | None:
        identifier = getattr(self.version, "entity_identifier", None)
        if identifier is None or entity.type_id is None:
            return None
        return identifier(entity.type_id)

    def _inventory_count(self: Bot, name: str) -> int:
        menu = self.player_menu
        if name == "*":
            return sum(stack.count for stack in menu.slots().values())
        return menu.count(name)

    def _has_room(self: Bot, stacks: int = 1) -> bool:
        return self.player_menu.free() is not None or stacks <= 0

    # ------------------------------------------------------------------
    # the action
    # ------------------------------------------------------------------
    async def pick_up(
        self: Bot,
        items: list[str] | None = None,
        count: int | dict[str, int] | None = None,
        *,
        radius: float = 8.0,
        shape: str = "sphere",
        allow_loss: int = -1,
        timeout: float = 60.0,
        near: float = 0.8,
    ) -> PickupReport:
        """Walk to dropped items until ``count`` of each is in the inventory.

        ``count`` is an int (that many items overall) or a dict (that many of
        each named item); ``-1`` in either place means "everything you can
        reach".  ``radius`` is the sphere searched at call time, ``-1`` for the
        whole loaded view.  ``allow_loss`` is how many items may already be
        missing from that sphere (or vanish before they are reached), ``-1``
        meaning no limit; exceeding it raises :class:`TooMuchLoss`.

        The count comes from the server: an item counts once its entity is gone
        *and* the player's own window shows it.  Losing track of an item is
        booked as loss, not as success.
        """

        if shape not in ("sphere", "box"):
            raise ValueError(f"unknown search shape {shape!r}")
        started = time.monotonic()
        wanted = _parse_want(items, count)
        report = PickupReport(wanted=dict(wanted))

        baseline = {name: self._inventory_count(name) for name in wanted}
        found = self.dropped_items(radius=radius, items=None if "*" in wanted else list(wanted))
        available: dict[str, int] = {}
        if "*" in wanted:
            available["*"] = sum(entry[3] for entry in found)
        else:
            for entry in found:
                available[entry[2]] = available.get(entry[2], 0) + entry[3]

        for name, want in wanted.items():
            if want < 0:
                continue
            missing = want - available.get(name, 0)
            if missing > 0 and (allow_loss >= 0 and missing > allow_loss):
                report.reason = (
                    f"only {available.get(name, 0)} of {name} inside {radius} blocks,"
                    f" short by {missing} (allow_loss={allow_loss})"
                )
                raise TooMuchLoss(report.reason, report)
        if not found:
            report.seconds = time.monotonic() - started
            report.reason = f"no dropped item within {radius} blocks"
            report.ok = not any(want > 0 for want in wanted.values())
            return report

        deadline = started + timeout
        visited: set[int] = set()
        while time.monotonic() < deadline:
            candidates = self.dropped_items(
                radius=radius, items=None if "*" in wanted else list(wanted)
            )
            candidates = [entry for entry in candidates if entry[1] not in visited]
            if not candidates:
                break
            _, entity_id, name, amount, x, y, z = candidates[0]
            if self.player_menu.free() is None and self._inventory_count(name) < wanted.get(name, 10**9):
                report.reason = "the inventory is full"
                report.seconds = time.monotonic() - started
                self._finish(report, wanted, baseline)
                raise InventoryFull(report.reason, report)
            try:
                await self.navigate_to(x, z, tolerance=near, timeout=min(8.0, max(1.0, deadline - time.monotonic())))
            except Exception as error:  # noqa: BLE001 - any walk failure is one lost item
                visited.add(entity_id)
                report.lost += amount
                if allow_loss >= 0 and report.lost > allow_loss:
                    report.reason = f"cannot reach {name} at ({x:.1f}, {y:.1f}, {z:.1f}): {error}"
                    report.seconds = time.monotonic() - started
                    raise TooMuchLoss(report.reason, report) from error
                continue
            visited.add(entity_id)
            report.visited += 1
            # Wait for the server to delete the entity and credit the stack.
            poll_until = min(deadline, time.monotonic() + 2.0)
            while time.monotonic() < poll_until:
                if entity_id not in self.entities:
                    break
                await self.tick()
                await asyncio.sleep(POLL_SECONDS)
            if entity_id in self.entities:
                entity = self.entities[entity_id]
                if math.dist((x, y, z), (entity.x, entity.y, entity.z)) > 1.5:
                    # It rolled away; drop the memo so the next pass retries it.
                    visited.discard(entity_id)
                else:
                    report.lost += amount
                    if allow_loss >= 0 and report.lost > allow_loss:
                        report.reason = f"{name} at ({x:.1f}, {y:.1f}, {z:.1f}) was not picked up"
                        report.seconds = time.monotonic() - started
                        raise TooMuchLoss(report.reason, report)
            self._finish(report, wanted, baseline)
            if self._satisfied(report, wanted):
                break

        self._finish(report, wanted, baseline)
        report.seconds = time.monotonic() - started
        report.ok = self._satisfied(report, wanted)
        if not report.ok:
            report.reason = report.reason or "not everything asked for was collected"
        return report

    def _finish(self: Bot, report: PickupReport, wanted: dict[str, int], baseline: dict[str, int]) -> None:
        for name in wanted:
            collected = self._inventory_count(name) - baseline[name]
            if collected > 0:
                report.collected[name] = report.collected.get(name, 0) + collected
                baseline[name] += collected

    def _satisfied(self: Bot, report: PickupReport, wanted: dict[str, int]) -> bool:
        for name, want in wanted.items():
            if want > 0 and report.collected.get(name, 0) < min(want, report.collected.get(name, 0) + 1):
                return False
        if "*" in wanted and wanted["*"] < 0:
            return True
        return all(
            want < 0 or report.collected.get(name, 0) >= want
            for name, want in wanted.items()
        )
