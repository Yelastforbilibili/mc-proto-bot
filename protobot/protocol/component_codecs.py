"""How many bytes each known data component takes on the wire.

A component patch has no length prefixes, so decoding a stack that carries
components means knowing the exact size of every value (see
:mod:`protobot.protocol.stacks`).  Everything here was read off the 26.3
decompilation and is keyed by *component name*, never by ID: IDs move between
releases, the packed shape does not.

``KINDS_BY_NAME`` is deliberately small and honest.  A component that is not in
it is not guessed: the stack decoder branches over its length instead, and a
stack whose patch fits more than one way is reported as unreadable rather than
silently mis-parsed.
"""

from __future__ import annotations

from typing import Callable, Mapping

from .codec import PacketReader
from .nbt import read_anonymous_nbt

#: value takes no bytes at all (``StreamCodec.unit``)
UNIT = "unit"
#: one varint
VARINT = "varint"
#: one byte, 0 or 1
BOOL = "bool"
#: ``ItemEnchantments``: varint count, then (varint enchantment, varint level) pairs
ENCHANTMENTS = "enchantments"
#: one network NBT tag (``ComponentSerialization.STREAM_CODEC``)
NBT = "nbt"
#: varint count, then that many network NBT tags (``ItemLore``)
COMPONENTS = "components"

#: Component name -> packed kind, for every component this library can read.
KINDS_BY_NAME: dict[str, str] = {
    # Unit.STREAM_CODEC = StreamCodec.unit(INSTANCE)
    "minecraft:unbreakable": UNIT,
    # ByteBufCodecs.VAR_INT
    "minecraft:max_stack_size": VARINT,
    "minecraft:max_damage": VARINT,
    "minecraft:damage": VARINT,
    "minecraft:repair_cost": VARINT,
    # ItemEnchantments.STREAM_CODEC
    "minecraft:enchantments": ENCHANTMENTS,
    "minecraft:stored_enchantments": ENCHANTMENTS,
    # ComponentSerialization.STREAM_CODEC
    "minecraft:custom_name": NBT,
    "minecraft:item_name": NBT,
    # ItemLore.STREAM_CODEC
    "minecraft:lore": COMPONENTS,
}


class ComponentCodecError(ValueError):
    """A component value did not fit the bytes that are left."""


def read_varint(data: bytes, index: int, end: int) -> int:
    """Read one varint from ``data[index:end]`` and return the next index."""

    value = 0
    shift = 0
    while True:
        if index >= end:
            raise ComponentCodecError("component value is truncated")
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return index
        shift += 7
        if shift > 35:
            raise ComponentCodecError("varint component value is too long")


def _read_unit(data: bytes, index: int, end: int) -> int:
    return index


def _read_varint(data: bytes, index: int, end: int) -> int:
    return read_varint(data, index, end)


def _read_bool(data: bytes, index: int, end: int) -> int:
    if index >= end:
        raise ComponentCodecError("boolean component value is truncated")
    return index + 1


def _read_enchantments(data: bytes, index: int, end: int) -> int:
    reader = PacketReader(data[index:end])
    count = reader.read_varint()
    for _ in range(count):
        reader.read_varint()  # enchantment registry id
        reader.read_varint()  # level
    stop = index + reader.offset
    if stop > end:
        raise ComponentCodecError("enchantment component runs past the patch")
    return stop


def _read_nbt(data: bytes, index: int, end: int) -> int:
    reader = PacketReader(data[index:end])
    try:
        read_anonymous_nbt(reader)
    except Exception as error:  # noqa: BLE001 - any parse failure means truncation
        raise ComponentCodecError(f"text component is not readable: {error}") from error
    stop = index + reader.offset
    if stop > end:
        raise ComponentCodecError("text component runs past the patch")
    return stop


def _read_components(data: bytes, index: int, end: int) -> int:
    reader = PacketReader(data[index:end])
    count = reader.read_varint()
    for _ in range(count):
        read_anonymous_nbt(reader)
    stop = index + reader.offset
    if stop > end:
        raise ComponentCodecError("text component list runs past the patch")
    return stop


_READERS: dict[str, Callable[[bytes, int, int], int]] = {
    UNIT: _read_unit,
    VARINT: _read_varint,
    BOOL: _read_bool,
    ENCHANTMENTS: _read_enchantments,
    NBT: _read_nbt,
    COMPONENTS: _read_components,
}


def reader_for(kind: str) -> Callable[[bytes, int, int], int]:
    """Return the reader for ``kind`` (raises :class:`KeyError`)."""

    return _READERS[kind]


def codecs_by_id(table: Mapping[str, int] | object) -> dict[int, Callable[[bytes, int, int], int]]:
    """Map component type IDs to value readers using a component table.

    ``table`` is anything with a ``by_name`` mapping (a
    :class:`protobot.protocol.components.ComponentTable`), or the mapping
    itself.
    """

    by_name = getattr(table, "by_name", table)
    codecs: dict[int, Callable[[bytes, int, int], int]] = {}
    for name, kind in KINDS_BY_NAME.items():
        component_id = by_name.get(name) if hasattr(by_name, "get") else None
        if component_id is None:
            continue
        codecs[int(component_id)] = _READERS[kind]
    return codecs


__all__ = [
    "BOOL",
    "COMPONENTS",
    "ComponentCodecError",
    "ENCHANTMENTS",
    "KINDS_BY_NAME",
    "NBT",
    "UNIT",
    "VARINT",
    "codecs_by_id",
    "read_varint",
    "reader_for",
]
