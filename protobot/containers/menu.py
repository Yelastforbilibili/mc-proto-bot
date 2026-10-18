"""Semantic windows: address container slots by name, not by number.

``bot.click_container(54)`` works, but 54 is the third hotbar slot of the first
chest menu and the fuel slot of a furnace.  This layer puts the meaning back:

    menu = bot.menu                      # whatever the server has opened
    await menu.open_block((6, -60, -8))  # right-click a chest, wait for it
    await menu.deposit("minecraft:dirt", 15)
    await menu.withdraw("minecraft:dirt")

Every operation is confirmed against the server: a click is only reported as
successful once the server has sent a slot/state update for that window, and
item transfers report the number of items the *server* actually moved, not the
number the caller asked for.

The click itself carries an empty ``changedSlots`` list and an empty carried
``HashedStack``.  ``ServerGamePacketListenerImpl.handleContainerClick`` only
uses those for its own redundant-update suppression and validates the real
``stateId``, so a stateId-tracking client that lets the server decide is both
correct and what vanilla clients send.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Iterable, Mapping

from ..actions import BlockFace
from ..physics import Vec3
from ..protocol.items import normalise_identifier
from ..state import ContainerState, ItemStack
from .errors import ClickRejected, ContainerError, MenuLayoutError, MenuNotOpen
from .layouts import MenuLayout, layout_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..crafting.recipes import Recipe

#: The player's own inventory window; it is never "opened" by the server.
PLAYER_CONTAINER_ID = 0

#: Vanilla ``ContainerInput`` ordinals.
CLICK_MODES = {
    "pickup": 0,
    "quick_move": 1,
    "swap": 2,
    "throw": 3,
    "quick_craft": 4,
    "pickup_all": 5,
    "quick_craft_all": 6,
}

#: How long a click may take to come back from the server.
DEFAULT_CLICK_TIMEOUT = 1.0

#: How long the window has to stay unchanged before a click is considered
#: finished.  A click produces several ``container_set_slot`` packets (the
#: clicked slot, the cursor, and every slot the server re-sends), and they can
#: straddle two TCP reads, so "one update arrived" is not "this click is done".
#: Sending the next click while the previous one is still trickling in makes the
#: local model lag one click behind, and a stale ``state_id`` then makes the
#: server answer every click with a full resync.
QUIET_SECONDS = 0.05

#: Poll interval for the quiet detector; small enough not to add latency of its
#: own, large enough not to spin.
POLL_SECONDS = 0.005

#: Assumed maximum stack size when picking a target slot.  The server has the
#: final word: an over-full target simply does not accept the item, and the
#: observed delta is what the caller sees.
MAX_STACK = 64

#: Index of each ``container_set_data`` value per menu.  These are the numbers
#: vanilla menus use for their progress bars; the server sends them as short
#: (menu type, index, value) triples, which is why nothing here needs the slot
#: layout to interpret them.
DATA_SLOTS: dict[str, dict[str, int]] = {
    "minecraft:furnace": {
        "lit_time": 0,
        "lit_total": 1,
        "cook_progress": 2,
        "cook_total": 3,
    },
    "minecraft:blast_furnace": {
        "lit_time": 0,
        "lit_total": 1,
        "cook_progress": 2,
        "cook_total": 3,
    },
    "minecraft:smoker": {
        "lit_time": 0,
        "lit_total": 1,
        "cook_progress": 2,
        "cook_total": 3,
    },
    "minecraft:brewing_stand": {"brew_time": 0, "fuel": 1},
    "minecraft:enchantment": {"cost_0": 0, "cost_1": 1, "cost_2": 2, "seed": 3},
    "minecraft:beacon": {"levels": 0, "primary_effect": 1, "secondary_effect": 2},
    "minecraft:stonecutter": {"selected_recipe": 0},
    "minecraft:crafter_3x3": {"triggered": 0, "slot_disabled": 1},
}


def _identifier(value: object) -> str | None:
    if isinstance(value, ItemStack):
        return value.identifier
    if isinstance(value, str):
        return normalise_identifier(value)
    identifier = getattr(value, "identifier", None)
    if isinstance(identifier, str) and identifier:
        return normalise_identifier(identifier)
    return None


def _matches(stack: ItemStack, wanted: object) -> bool:
    """Whether ``stack`` is the item the caller asked for."""

    if stack.count <= 0:
        return False
    if isinstance(wanted, int):
        return stack.item_id == wanted
    identifier = _identifier(wanted)
    if identifier is None:
        raise TypeError(f"cannot match items against {wanted!r}")
    return stack.identifier == identifier


def _with_count(stack: ItemStack, count: int) -> ItemStack:
    """Copy of ``stack`` holding ``count`` items (``ItemStack`` is frozen)."""

    if count <= 0:
        return ItemStack()
    return ItemStack(count, stack.item_id, stack.identifier, stack.removed_component_ids)


class Menu:
    """One server-opened window, addressed by logical slot names."""

    def __init__(self, bot, container_id: int | None = None) -> None:
        self.bot = bot
        self._container_id = container_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        try:
            return f"<Menu {self.container_id} {self.identifier} ({self.slot_count} slots)>"
        except ContainerError:
            return "<Menu closed>"

    # -- state -----------------------------------------------------------

    @property
    def container_id(self) -> int:
        """The window ID, resolved from the active container when unset."""

        if self._container_id is not None:
            return self._container_id
        active = self.bot.active_container
        if active is None:
            raise MenuNotOpen("no window is open")
        return active.container_id

    def state(self) -> ContainerState:
        """The latest server snapshot of this window."""

        state = self.bot.containers.get(self.container_id)
        if state is None:
            raise MenuNotOpen(f"container {self.container_id} is not tracked")
        return state

    @property
    def is_open(self) -> bool:
        state = self.bot.containers.get(self.container_id)
        return state is not None and state.is_open

    @property
    def state_id(self) -> int:
        return self.state().state_id

    @property
    def revision(self) -> int:
        """Counter bumped on every update, including progress packets."""

        return self.state().revision

    @property
    def slot_revision(self) -> int:
        """Counter bumped only when slots or the cursor change."""

        return self.state().slot_revision

    @property
    def slot_count(self) -> int:
        state = self.state()
        if state.slot_count:
            return state.slot_count
        return max(state.items, default=-1) + 1

    @property
    def identifier(self) -> str | None:
        """The menu's registry name, resolved from this release's table.

        Container 0 is always the player's own inventory menu, which the server
        never announces with ``open_screen`` -- so it has no ``menu_type_id``
        and would otherwise fall back to a made-up layout that puts the 2x2
        grid where the result slot is.
        """

        state = self.state()
        if state.container_id == PLAYER_CONTAINER_ID:
            return "minecraft:inventory"
        if state.menu_type_id is None:
            return None
        return self.bot.version.menu_identifier(state.menu_type_id)

    @property
    def layout(self) -> MenuLayout:
        return layout_for(self.identifier, self.slot_count or None)

    def slots(self, name: str) -> tuple[int, ...]:
        return self.layout.slots(name)

    def slot(self, name: str) -> int:
        return self.layout.slot(name)

    @property
    def data(self) -> Mapping[int, int]:
        """Progress/telemetry values (furnace timers, enchant costs, ...)."""

        return dict(self.state().data)

    def data_value(self, index: int | str, default: int = 0) -> int:
        """A progress value by index, or by name (``"cook_progress"``).

        Names come from :data:`DATA_SLOTS`; unknown names raise ``KeyError`` so a
        typo is loud instead of silently reading slot 0.
        """

        if isinstance(index, str):
            table = DATA_SLOTS.get(self.identifier, {})
            try:
                index = table[index]
            except KeyError:
                known = ", ".join(sorted(table)) or "no named values"
                raise KeyError(
                    f"{self.identifier or 'this menu'} has {known}"
                ) from None
        return self.state().data.get(index, default)

    @property
    def carried(self) -> ItemStack:
        """The item currently held on the cursor, as last announced."""

        return self.state().carried_item

    @property
    def items(self) -> dict[int, ItemStack]:
        return dict(self.state().items)

    def item(self, slot: int) -> ItemStack:
        return self.state().items.get(slot, ItemStack())

    def readable(self, slot: int) -> bool:
        """Whether this slot's contents could be decoded.

        A stack carrying added data components (enchanted gear, potions) is not
        self-delimiting on this protocol version, so such slots stay byte-only.
        Treating them as empty would make the layer deposit on top of an
        unknown stack, so every operation steers around them instead.
        """

        return slot not in self.state().unreadable

    def __getitem__(self, slot: int) -> ItemStack:
        return self.item(slot)

    # -- queries ---------------------------------------------------------

    def _slots_for(self, where: str) -> tuple[int, ...]:
        if where in ("all", "any"):
            return self.slots("all")
        if where == "player":
            return self.slots("player")
        return self.slots(where)

    def count(self, item: object, *, where: str = "any") -> int:
        """Total number of ``item`` in the given region."""

        return sum(
            stack.count
            for slot in self._slots_for(where)
            if _matches(stack := self.item(slot), item)
        )

    def find(self, item: object, *, where: str = "player", min_count: int = 1) -> int | None:
        """First slot in ``where`` holding at least ``min_count`` of ``item``."""

        for slot in self._slots_for(where):
            if not self.readable(slot):
                continue
            stack = self.item(slot)
            if _matches(stack, item) and stack.count >= min_count:
                return slot
        return None

    def free(self, item: object | None = None, *, where: str = "player") -> int | None:
        """First empty slot, or the first slot already holding ``item``."""

        slots = self._slots_for(where)
        for slot in slots:
            if not self.readable(slot):
                continue
            if self.item(slot).count == 0:
                return slot
        if item is not None:
            for slot in slots:
                if self.readable(slot) and _matches(self.item(slot), item):
                    return slot
        return None

    def describe(self) -> str:
        parts = [self.layout.describe()]
        for name in ("container", "result", "player"):
            slots = self.layout.groups.get(name, ())
            if slots:
                filled = sum(1 for slot in slots if self.item(slot).count)
                parts.append(f"{name} {filled}/{len(slots)}")
        return ", ".join(parts)

    # -- clicking --------------------------------------------------------

    async def click(
        self,
        slot: int,
        *,
        button: int = 0,
        mode: str | int = "pickup",
        settle: bool = True,
        timeout: float = DEFAULT_CLICK_TIMEOUT,
    ) -> bool:
        """Send one click and wait for the server to act on it.

        Returns ``True`` once the window was updated, ``False`` when nothing
        came back within ``timeout`` -- which means the server ignored or
        rejected the click.

        Two quiet periods bracket the click.  The first makes sure the previous
        click's packets have all been applied, so the ``state_id`` we send is
        the one the server expects; the second makes sure this click's packets
        have all been applied before the caller reads the window.
        """

        if isinstance(mode, str):
            try:
                mode_id = CLICK_MODES[mode.lower()]
            except KeyError:
                raise ValueError(f"unknown click mode {mode!r}") from None
        else:
            mode_id = int(mode)
        container_id = self.container_id
        await self.wait_until_quiet(container_id, timeout=timeout)
        state = self.state()
        revision = state.slot_revision
        cursor_revision = state.cursor_revision
        predicted = self._predict_cursor(
            None if state.carried_unknown else state.carried_item,
            self.item(slot) if slot >= 0 and self.readable(slot) else None,
            button,
            mode_id,
        )
        await self.bot.click_container(
            slot,
            button=button,
            click_type=mode_id,
            container_id=state.container_id,
            state_id=state.state_id,
        )
        if not settle:
            return True
        changed = await self.wait_for_update(
            revision, container_id=container_id, timeout=timeout
        )
        if changed:
            await self.wait_until_quiet(container_id, timeout=timeout)
            if (
                predicted is not None
                and state.cursor_revision == cursor_revision
                and not state.carried_unknown
            ):
                # The server only announces the cursor when the claim we sent
                # did not already match what the click produced, so a click that
                # ends with the cursor empty stays silent.  Fill that gap the way
                # a vanilla client does: predict it from the two stacks.
                state.carried_item = predicted
                state.carried_item_data = b""
        return changed

    def _predict_cursor(
        self,
        cursor: ItemStack | None,
        slot_stack: ItemStack | None,
        button: int,
        mode_id: int,
    ) -> ItemStack | None:
        """Predict the cursor a click leaves behind, or ``None`` if unsure.

        Only the modes that move the cursor are predicted.  Shift-clicks,
        number-key swaps and double-clicks never touch it, and the server always
        echoes a real mismatch, so a prediction is a gap filler and not the
        source of truth.
        """

        if cursor is None or slot_stack is None:
            return None
        kind = next(
            (name for name, value in CLICK_MODES.items() if value == mode_id), None
        )
        if kind in ("quick_move", "swap", "pickup_all", "throw", "quick_craft", "quick_craft_all"):
            return cursor
        if kind != "pickup":
            return None
        cursor_count = cursor.count
        stack_count = slot_stack.count
        same = bool(
            cursor_count and stack_count and _identifier(cursor) == _identifier(slot_stack)
        )
        if button == 1:
            if cursor_count <= 0:
                if stack_count <= 0:
                    return cursor
                return _with_count(slot_stack, (stack_count + 1) // 2)
            if same or stack_count <= 0:
                if stack_count >= MAX_STACK:
                    return cursor
                return _with_count(cursor, cursor_count - 1)
            return cursor
        if cursor_count <= 0:
            return slot_stack if stack_count > 0 else cursor
        if stack_count <= 0:
            return ItemStack()
        if same:
            left = cursor_count - min(MAX_STACK - stack_count, cursor_count)
            return _with_count(cursor, left)
        return slot_stack

    async def wait_until_quiet(
        self,
        container_id: int | None = None,
        *,
        quiet: float | None = None,
        timeout: float = DEFAULT_CLICK_TIMEOUT,
    ) -> bool:
        """Wait until the window has stopped changing for ``quiet`` seconds.

        The click protocol has no acknowledgement: the only signal that the
        server finished with a click is that it stopped sending updates for it.
        """

        if container_id is None:
            container_id = self.container_id
        window = QUIET_SECONDS if quiet is None else quiet
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, window)
        state = self.bot.containers.get(container_id)
        if state is None:
            return False
        last = state.slot_revision
        changed_at = loop.time()
        while loop.time() < deadline:
            await asyncio.sleep(POLL_SECONDS if window > POLL_SECONDS else max(window, POLL_SECONDS))
            state = self.bot.containers.get(container_id)
            if state is None:
                return False
            if state.slot_revision != last:
                last = state.slot_revision
                changed_at = loop.time()
                continue
            if window <= 0 or loop.time() - changed_at >= window:
                return True
        return False

    async def wait_for_update(
        self,
        revision: int,
        *,
        container_id: int | None = None,
        timeout: float = DEFAULT_CLICK_TIMEOUT,
    ) -> bool:
        """Block until the window contents change or ``timeout`` expires.

        Deliberately watches :attr:`ContainerState.slot_revision` and not
        ``revision``: a furnace or a brewing stand sends ``container_set_data``
        several times a second, and treating that as "the click landed" would
        report a move before the slot update it was waiting for had arrived.
        """

        if container_id is None:
            container_id = self.container_id
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            state = self.bot.containers.get(container_id)
            if state is not None and state.slot_revision != revision:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(POLL_SECONDS)

    async def pickup(self, slot: int, *, button: int = 0, **kwargs) -> bool:
        """Left click (``button=0``) or right click (``button=1``) a slot."""

        return await self.click(slot, button=button, mode="pickup", **kwargs)

    async def quick_move(self, slot: int, **kwargs) -> bool:
        """Shift-click: move the stack to the other side of the window."""

        return await self.click(slot, button=0, mode="quick_move", **kwargs)

    async def swap_with_hotbar(self, slot: int, hotbar: int, **kwargs) -> bool:
        """Number-key swap: ``hotbar`` is 0..8."""

        if not 0 <= hotbar <= 8:
            raise ValueError("hotbar index must be between 0 and 8")
        return await self.click(slot, button=hotbar, mode="swap", **kwargs)

    async def swap_with_offhand(self, slot: int, **kwargs) -> bool:
        return await self.click(slot, button=40, mode="swap", **kwargs)

    async def throw(self, slot: int, *, all_: bool = False, **kwargs) -> bool:
        """Drop one item (``all_=False``) or the whole stack out of the window."""

        return await self.click(slot, button=1 if all_ else 0, mode="throw", **kwargs)

    async def pickup_all(self, slot: int, **kwargs) -> bool:
        """Double-click style collection of every matching stack onto the cursor."""

        return await self.click(slot, button=0, mode="pickup_all", **kwargs)

    # -- transfers -------------------------------------------------------

    async def move(
        self,
        source: int,
        target: int,
        *,
        count: int | None = None,
        timeout: float = 2.0,
    ) -> int:
        """Move up to ``count`` items (default: the whole stack) between slots.

        Returns how many items the server actually moved.  Works with the
        cursor the way a player does: take the stack, put it down, return the
        remainder -- so it never depends on client-side predictions.
        """

        stack = self.item(source)
        if not self.readable(source) or not self.readable(target):
            raise ContainerError(
                "one of the slots holds an item whose data components this "
                "protocol version cannot decode; it stays server-side "
                "byte-only and cannot be moved reliably"
            )
        if stack.count <= 0:
            return 0
        destination = self.item(target)
        if destination.count and not _matches(destination, stack):
            raise ClickRejected(
                f"slot {target} holds {destination.identifier}, not {stack.identifier}",
                slot=target,
            )
        held = self.carried
        if held.count or self.state().carried_unknown:
            raise ContainerError(
                "the cursor is not empty; put the held stack down first"
            )
        available = stack.count
        wanted = available if count is None else max(0, min(count, available))
        if wanted <= 0:
            return 0
        if not await self.pickup(source, timeout=timeout):
            raise ClickRejected(f"the server did not pick up slot {source}", slot=source)
        before = self.item(target).count
        if wanted >= available:
            # The whole stack is on the cursor; a left click merges it.
            await self.click(target, button=0, mode="pickup", timeout=timeout)
        else:
            for _ in range(wanted):
                if not await self.click(target, button=1, mode="pickup", timeout=timeout):
                    break
        moved = max(0, self.item(target).count - before)
        if self.carried.count or self.state().carried_unknown:
            # The target had no room left.  A stack left on the cursor is
            # invisible to both sides of the window, so put it back; a result
            # slot refuses placements, and that has to be an error rather than
            # a silent lost item.
            await self.pickup(source, timeout=timeout)
            if self.carried.count or self.state().carried_unknown:
                raise ClickRejected(
                    f"slot {target} had no room for the stack and slot {source} "
                    "refused it back, so the items stayed on the cursor",
                    slot=target,
                )
            moved = max(0, self.item(target).count - before)
        return moved

    async def deposit(
        self,
        item: object,
        count: int | None = None,
        *,
        where: str = "container",
        source: str = "player",
    ) -> int:
        """Move up to ``count`` items (default: everything) into the window."""

        return await self._transfer_items(
            item, count, source=source, target=where
        )

    async def withdraw(
        self,
        item: object,
        count: int | None = None,
        *,
        where: str = "container",
        target: str = "player",
    ) -> int:
        """Move up to ``count`` items (default: everything) out of the window."""

        return await self._transfer_items(item, count, source=where, target=target)

    async def _transfer_items(
        self,
        item: object,
        count: int | None,
        *,
        source: str,
        target: str,
    ) -> int:
        wanted = count
        moved_total = 0
        source_slots = self._slots_for(source)
        target_slots = self._slots_for(target)
        for _ in range(256):  # bounded: one destination slot per iteration
            if wanted is not None and wanted <= 0:
                break
            source_slot = None
            for slot in source_slots:
                if not self.readable(slot):
                    continue
                stack = self.item(slot)
                if _matches(stack, item):
                    source_slot = slot
                    break
            if source_slot is None:
                break
            available = self.item(source_slot).count
            target_slot = None
            for slot in target_slots:
                if not self.readable(slot):
                    continue
                stack = self.item(slot)
                if stack.count == 0:
                    target_slot = slot
                    break
                if _matches(stack, item) and stack.count < MAX_STACK:
                    target_slot = slot
                    break
            if target_slot is None:
                break
            room = MAX_STACK - self.item(target_slot).count
            amount = available if wanted is None else min(available, wanted, room)
            amount = min(amount, room)
            if amount <= 0:
                break
            moved = await self.move(source_slot, target_slot, count=amount)
            if moved <= 0:
                break
            moved_total += moved
            if wanted is not None:
                wanted -= moved
        return moved_total

    async def deposit_all(self, *, source: str = "player") -> int:
        """Shift-click every occupied slot of ``source`` to the other side."""

        moved = 0
        for slot in self._slots_for(source):
            if not self.readable(slot):
                continue
            stack = self.item(slot)
            if stack.count <= 0:
                continue
            before = stack.count
            if await self.quick_move(slot):
                moved += max(0, before - self.item(slot).count)
        return moved

    async def withdraw_all(self, *, where: str = "container") -> int:
        """Shift-click every occupied slot of ``where`` to the player."""

        return await self.deposit_all(source=where)

    async def close(self) -> None:
        """Ask the server to close this window."""

        await self.bot.close_container(self.container_id)

    # -- crafting --------------------------------------------------------

    def grid_slots(self) -> tuple[int, ...]:
        """The crafting grid slots, or ``()`` for a window without one."""

        return self.layout.groups.get("grid", ())

    def _grid_width(self) -> int:
        count = len(self.grid_slots())
        if count == 4:
            return 2
        if count == 9:
            return 3
        raise MenuLayoutError(
            f"{self.identifier or 'this window'} has no crafting grid"
        )

    def grid_slot(self, row: int, column: int) -> int:
        """The slot at ``(row, column)`` of this window's crafting grid."""

        width = self._grid_width()
        if not (0 <= row < width and 0 <= column < width):
            raise MenuLayoutError(
                f"({row}, {column}) is outside the {width}x{width} grid"
            )
        return self.grid_slots()[row * width + column]

    async def fill_grid(self, recipe: Recipe, *, timeout: float = 2.0) -> None:
        """Place one of each ingredient into the grid, the way a player does.

        The stack is taken to the cursor, right-clicked once per cell, and the
        remainder put back, so the server sees ordinary player clicks and the
        inventory ends up exactly as the recipe describes.
        """

        width = self._grid_width()
        if recipe.size > width:
            raise MenuLayoutError(
                f"{recipe.describe()} needs a {recipe.size}x{recipe.size} grid but "
                f"{self.identifier or 'this window'} only has {width}x{width}"
            )
        wanted: dict[str, list[int]] = {}
        for row, column, item in recipe.cells:
            wanted.setdefault(item, []).append(self.grid_slot(row, column))
        for slots in wanted.values():
            for slot in slots:
                if not self.readable(slot):
                    raise ContainerError(
                        f"grid slot {slot} holds an item this protocol cannot decode"
                    )
                if self.item(slot).count:
                    raise ContainerError(
                        f"grid slot {slot} is not empty; clear the grid first"
                    )
        for item, slots in wanted.items():
            remaining = list(slots)
            while remaining:
                source = self.find(item, where="player")
                if source is None:
                    raise ContainerError(
                        f"no {item} left in the inventory to craft "
                        f"{recipe.output} with"
                    )
                if not await self.pickup(source, timeout=timeout):
                    raise ClickRejected(
                        f"the server did not pick up {item} from slot {source}",
                        slot=source,
                    )
                for slot in list(remaining):
                    placed = await self.click(
                        slot, button=1, mode="pickup", timeout=timeout
                    )
                    if not placed:
                        break
                    remaining.remove(slot)
                if self.carried.count:
                    # Put the unused part of the stack back where it came from.
                    await self.pickup(source, timeout=timeout)
                    if self.carried.count:
                        raise ClickRejected(
                            "the leftovers would not go back into slot "
                            f"{source}, so the cursor still holds items",
                            slot=source,
                        )

    async def clear_grid(self, *, timeout: float = 2.0) -> int:
        """Shift-click every grid slot back into the inventory."""

        moved = 0
        for slot in self.grid_slots():
            if not self.readable(slot):
                continue
            stack = self.item(slot)
            if stack.count <= 0:
                continue
            if await self.quick_move(slot, timeout=timeout):
                moved += max(0, stack.count - self.item(slot).count)
        return moved

    async def craft_once(self, recipe: Recipe, *, timeout: float = 2.0) -> int:
        """Click the result slot once and put the crafted item in the inventory.

        The grid has to be filled already.  Returns how many items the server
        actually handed over.
        """

        result = self.slot("result")
        if not self.readable(result):
            raise ContainerError(f"result slot {result} holds undecodable data")
        stack = self.item(result)
        if stack.count <= 0:
            raise ClickRejected(f"slot {result} has nothing to craft", slot=result)
        if not _matches(stack, recipe.output):
            raise ClickRejected(
                f"slot {result} offers {stack.identifier}, not {recipe.output}",
                slot=result,
            )
        before = self.count(recipe.output, where="player")
        if not await self.pickup(result, timeout=timeout):
            raise ClickRejected(
                f"the server did not hand over {recipe.output} from slot {result}",
                slot=result,
            )
        target = self.free(recipe.output, where="player")
        if target is None:
            raise ContainerError(
                f"no room in the inventory for {recipe.output}; the crafted item "
                "is on the cursor"
            )
        if not await self.click(target, button=0, mode="pickup", timeout=timeout):
            raise ClickRejected(
                f"the server did not put {recipe.output} into slot {target}",
                slot=target,
            )
        if self.carried.count or self.state().carried_unknown:
            raise ClickRejected(
                f"slot {target} could not take the crafted stack, so it stayed on "
                "the cursor",
                slot=target,
            )
        return max(0, self.count(recipe.output, where="player") - before)

    def has_ingredients(self, recipe: Recipe, *, where: str = "player") -> bool:
        """Whether the inventory holds everything one craft of ``recipe`` needs."""

        return not self.missing_ingredients(recipe, where=where)

    def missing_ingredients(
        self, recipe: Recipe, *, where: str = "player"
    ) -> dict[str, int]:
        """How much of each ingredient is still missing for one craft."""

        missing: dict[str, int] = {}
        for item, need in recipe.ingredients.items():
            held = self.count(item, where=where)
            if held < need:
                missing[item] = need - held
        return missing

    async def craft(
        self,
        recipe: Recipe,
        times: int = 1,
        *,
        timeout: float = 2.0,
    ) -> int:
        """Craft ``times`` copies and return how much the server really made.

        Stops early when the ingredients run out: a caller asking for five
        crafts with materials for two gets two, not an exception.  The grid is
        cleared afterwards either way, so leftover items never strand.
        """

        if times <= 0:
            return 0
        made = 0
        try:
            for _ in range(times):
                if not self.has_ingredients(recipe):
                    break
                await self.fill_grid(recipe, timeout=timeout)
                made += await self.craft_once(recipe, timeout=timeout)
        finally:
            await self.clear_grid(timeout=timeout)
        return made

    # -- opening ---------------------------------------------------------

    async def open_block(
        self,
        position: tuple[int, int, int],
        *,
        face: BlockFace | None = None,
        reach: float = 4.5,
        timeout: float = 2.0,
    ) -> Menu:
        """Right-click a block and return the window the server opens."""

        x, y, z = position
        center = Vec3(x + 0.5, y + 0.5, z + 0.5)
        eye = self.bot.physics_state.position
        offset = center - eye
        if offset.length_squared > reach * reach:
            raise ContainerError(
                f"{position} is {math.sqrt(offset.length_squared):.1f} blocks away,"
                f" beyond the {reach} block reach"
            )
        before = self._open_windows()
        await self.bot.actions.look_at(center)
        await self.bot.actions.use_item_on(position, face=face or BlockFace.UP)
        return await self._wait_for_window(before, timeout=timeout)

    async def open_entity(
        self,
        entity_id: int,
        *,
        hand: str | int = "main_hand",
        position: Vec3 | None = None,
        timeout: float = 2.0,
    ) -> Menu:
        """Right-click an entity (villager, donkey, chest boat, ...)."""

        before = self._open_windows()
        await self.bot.actions.interact(entity_id, hand=hand, position=position)
        return await self._wait_for_window(before, timeout=timeout)

    def _open_windows(self) -> set[int]:
        return {
            container_id
            for container_id, state in self.bot.containers.items()
            if container_id != PLAYER_CONTAINER_ID and state.is_open
        }

    async def _wait_for_window(self, before: set[int], *, timeout: float) -> Menu:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for container_id, state in self.bot.containers.items():
                if container_id == PLAYER_CONTAINER_ID:
                    continue
                # ``open_screen`` only names the window; the slots arrive in the
                # ``container_set_content`` packet that follows it.  Returning on
                # the screen packet alone hands back a menu with no slots, which
                # every click would then miss.
                if (
                    container_id not in before
                    and state.is_open
                    and state.menu_type_id is not None
                    and state.slot_count
                ):
                    return Menu(self.bot, container_id)
            if loop.time() >= deadline:
                raise MenuNotOpen(f"the server opened no window within {timeout:.1f}s")
            await asyncio.sleep(0.01)


def player_menu(bot) -> Menu:
    """The always-present player inventory window (container 0)."""

    return Menu(bot, PLAYER_CONTAINER_ID)
