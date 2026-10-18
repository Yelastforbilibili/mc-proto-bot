"""Recover item-stack frames whose component patch is not self-delimiting.

An ``ItemStack`` on the wire is ``count`` + ``item id`` + a component patch.  The
patch is ``added count`` + ``removed count``, then one ``component type id`` +
``value`` pair per added component, then the removed type ids.  Every value is
written by its own codec -- a varint, an enchantment map, an NBT blob, a list of
stacks -- and nothing prefixes it with its length.  A reader that does not
implement a component's codec therefore cannot step over it, and because the
patch sits inside the stack, every slot after it in the same packet is lost too.

Two things make a stack recoverable anyway:

* A single stack that fills the payload exactly (``container_set_slot``,
  ``set_cursor_item``, ``set_player_inventory``, equipment) is delimited by the
  packet end, so its patch length is *known* even with unknown components.
* Components whose codec is known (:mod:`protobot.protocol.component_codecs`)
  are stepped over exactly instead of guessed, so a slot list carrying them
  stays readable.

What is left is the multiset of unknown component values.  This module searches
for the lengths that make the list add up and, because the same bytes can often
be split more than one way, only returns a split that is **unique**.  Reporting a
stack list as unreadable is the honest answer; inventing one item and shifting
every slot after it is worse than saying nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from ..errors import ProtocolError
from ..state import ItemStack

#: Component values seen in practice are small; 1024 leaves generous headroom
#: for a shulker box inside a shulker box while keeping the search bounded.
MAX_COMPONENT_BYTES = 1024

#: A stack never holds more than 99 items in vanilla; a candidate parse that
#: claims otherwise is a wrong split of the previous stack's component patch.
MAX_COUNT = 99

#: Total candidate component values a single packet may examine before the frame
#: is declared unrecoverable.  A window of 1024 slots with three unknown stacks
#: stays far below this.
MAX_ATTEMPTS = 50_000


class StackFrameError(ProtocolError):
    """Raised when no unique patch-length assignment makes the list add up."""


@dataclass(frozen=True, slots=True)
class DecodedStack:
    """One decoded stack plus the byte range it occupied."""

    item: ItemStack
    start: int
    stop: int

    @property
    def length(self) -> int:
        return self.stop - self.start


class _Budget:
    """Counts candidate component values so the search cannot run away."""

    __slots__ = ("left",)

    def __init__(self, attempts: int) -> None:
        self.left = attempts

    def spend(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise StackFrameError("gave up looking for stack boundaries")


def _read_varint(data: bytes, index: int, end: int) -> tuple[int, int]:
    value = 0
    shift = 0
    position = index
    while True:
        if position >= end:
            raise StackFrameError("stack header runs past the end of the packet")
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
        if shift > 35:
            raise StackFrameError("stack header varint is too long")


def _read_header(
    data: bytes, index: int, end: int
) -> tuple[int, int, int, int, int]:
    """Read a stack up to the start of its component patch.

    Returns ``(count, item_id, added, removed, patch_start)``.  ``count == 0``
    means an empty slot, which is one byte and has no patch at all.
    """

    count, index = _read_varint(data, index, end)
    if count < 0 or count > MAX_COUNT:
        # Also the main pruning rule for the search below: a wrong patch length
        # that swallows the next stack's header almost always yields a count
        # like 114712827307, and rejecting it keeps the true split unique.
        raise StackFrameError(f"impossible item count {count}")
    if count == 0:
        return 0, 0, 0, 0, index
    item_id, index = _read_varint(data, index, end)
    if item_id < 0:
        raise StackFrameError(f"invalid item registry ID {item_id}")
    added, index = _read_varint(data, index, end)
    removed, index = _read_varint(data, index, end)
    if not 0 <= added <= 65536 or not 0 <= removed <= 65536:
        raise StackFrameError("item data-component counts must be 0 to 65536")
    return count, item_id, added, removed, index


def _scan_patch(
    data: bytes,
    cursor: int,
    end: int,
    *,
    added: int,
    removed: int,
    codecs: Mapping[int, Callable[[bytes, int, int], int]],
    budget: _Budget,
) -> Iterator[tuple[int, tuple[int, ...]]]:
    """Yield every ``(stop, removed_ids)`` the patch could end at.

    Known component codecs advance exactly; unknown ones are branched over.  The
    removed ids come last, so they are read once the added values are consumed.
    """

    if added == 0:
        try:
            stop, removed_ids = _read_removed(data, cursor, end, removed)
        except StackFrameError:
            return
        yield stop, removed_ids
        return

    try:
        component_id, after_id = _read_varint(data, cursor, end)
    except StackFrameError:
        return
    reader = codecs.get(component_id)
    if reader is not None:
        budget.spend()
        try:
            after_value = reader(data, after_id, end)
        except Exception:  # noqa: BLE001 - any codec error means this split is wrong
            return
        if after_value < after_id or after_value > end:
            return
        yield from _scan_patch(
            data,
            after_value,
            end,
            added=added - 1,
            removed=removed,
            codecs=codecs,
            budget=budget,
        )
        return

    longest = min(MAX_COMPONENT_BYTES, end - after_id)
    for length in range(longest + 1):
        budget.spend()
        yield from _scan_patch(
            data,
            after_id + length,
            end,
            added=added - 1,
            removed=removed,
            codecs=codecs,
            budget=budget,
        )


def _read_removed(
    data: bytes, cursor: int, end: int, removed: int
) -> tuple[int, tuple[int, ...]]:
    removed_ids: list[int] = []
    for _ in range(removed):
        component_id, cursor = _read_varint(data, cursor, end)
        if component_id < 0:
            raise StackFrameError("invalid removed data-component registry ID")
        removed_ids.append(component_id)
    return cursor, tuple(removed_ids)


def _scan_stack(
    data: bytes,
    start: int,
    end: int,
    *,
    item_identifier: Callable[[int], str | None],
    codecs: Mapping[int, Callable[[bytes, int, int], int]],
    budget: _Budget,
) -> Iterator[DecodedStack]:
    """Yield every plausible frame for the stack starting at ``start``."""

    try:
        count, item_id, added, removed, patch_start = _read_header(data, start, end)
    except StackFrameError:
        return
    if count == 0:
        yield DecodedStack(ItemStack(), start, patch_start)
        return
    for stop, removed_ids in _scan_patch(
        data,
        patch_start,
        end,
        added=added,
        removed=removed,
        codecs=codecs,
        budget=budget,
    ):
        item = ItemStack(
            count,
            item_id,
            item_identifier(item_id),
            removed_ids,
            data[patch_start:stop] if added else b"",
        )
        yield DecodedStack(item, start, stop)


def read_stacks(
    data: bytes,
    count: int,
    *,
    item_identifier: Callable[[int], str | None],
    codecs: Mapping[int, Callable[[bytes, int, int], int]] | None = None,
    start: int = 0,
    end: int | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[DecodedStack]:
    """Decode ``count`` consecutive stacks that fill ``data[start:end]``.

    Raises :class:`StackFrameError` when no assignment fits, or when more than
    one does: an ambiguous packet has no honest decoding.
    """

    if end is None:
        end = len(data)
    if count <= 0:
        if end != start:
            raise StackFrameError(
                f"{end - start} byte(s) left over after {count} stack(s)"
            )
        return []
    codecs = codecs or {}
    budget = _Budget(max_attempts)
    solutions: list[list[DecodedStack]] = []
    frames: list[DecodedStack] = []

    def walk(cursor: int, index: int) -> None:
        if len(solutions) > 1:
            return
        if index == count:
            if cursor == end:
                solutions.append(list(frames))
            return
        for candidate in _scan_stack(
            data,
            cursor,
            end,
            item_identifier=item_identifier,
            codecs=codecs,
            budget=budget,
        ):
            frames.append(candidate)
            walk(candidate.stop, index + 1)
            frames.pop()
            if len(solutions) > 1:
                return

    walk(start, 0)
    if not solutions:
        raise StackFrameError(
            f"no stack boundaries fill {end - start} byte(s) exactly"
        )
    if len(solutions) > 1:
        raise StackFrameError(
            f"{count} stack(s) fit more than one component-patch assignment"
        )
    return solutions[0]


def read_stack(
    data: bytes,
    *,
    item_identifier: Callable[[int], str | None],
    codecs: Mapping[int, Callable[[bytes, int, int], int]] | None = None,
    start: int = 0,
    end: int | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> DecodedStack:
    """Decode the single stack that fills ``data[start:end]``."""

    return read_stacks(
        data,
        1,
        item_identifier=item_identifier,
        codecs=codecs,
        start=start,
        end=end,
        max_attempts=max_attempts,
    )[0]


def read_remaining_stacks(
    data: bytes,
    *,
    item_identifier: Callable[[int], str | None],
    codecs: Mapping[int, Callable[[bytes, int, int], int]] | None = None,
    start: int = 0,
    end: int | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[DecodedStack]:
    """Decode as many stacks as fill ``data[start:end]`` exactly.

    Equipment updates have no slot count: the packet end is the only terminator.
    The split must still be unique, exactly as for a counted list.
    """

    if end is None:
        end = len(data)
    codecs = codecs or {}
    budget = _Budget(max_attempts)
    solutions: list[list[DecodedStack]] = []
    frames: list[DecodedStack] = []

    def walk(cursor: int) -> None:
        if len(solutions) > 1:
            return
        if cursor == end:
            solutions.append(list(frames))
            return
        for candidate in _scan_stack(
            data,
            cursor,
            end,
            item_identifier=item_identifier,
            codecs=codecs,
            budget=budget,
        ):
            frames.append(candidate)
            walk(candidate.stop)
            frames.pop()
            if len(solutions) > 1:
                return

    walk(start)
    if not solutions:
        raise StackFrameError(f"no stack boundaries fill {end - start} byte(s) exactly")
    if len(solutions) > 1:
        raise StackFrameError(
            f"more than one stack split fills {end - start} byte(s) exactly"
        )
    return solutions[0]


__all__ = [
    "DecodedStack",
    "MAX_ATTEMPTS",
    "MAX_COMPONENT_BYTES",
    "MAX_COUNT",
    "StackFrameError",
    "read_remaining_stacks",
    "read_stack",
    "read_stacks",
]
