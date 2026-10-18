"""Tests for recovering item-stack frames whose components hide their length."""

from __future__ import annotations

import unittest

from protobot.client import Bot
from protobot.protocol.codec import PacketWriter
from protobot.protocol.component_codecs import KINDS_BY_NAME, codecs_by_id
from protobot.protocol.components import clear_cache as clear_component_cache
from protobot.protocol.components import table_for as component_table_for
from protobot.protocol.stacks import (
    StackFrameError,
    read_stack,
    read_stacks,
    read_remaining_stacks,
)
from protobot.protocol.versions import get_version
from protobot.state import ContainerState, ItemStack

VERSION = get_version("26.3")
DIRT_ID = 55
STONE_ID = 1
TABLE = VERSION.components
assert TABLE is not None

UNBREAKABLE = TABLE.component_id("minecraft:unbreakable")
DAMAGE = TABLE.component_id("minecraft:damage")
MAX_DAMAGE = TABLE.component_id("minecraft:max_damage")
ENCHANTMENTS = TABLE.component_id("minecraft:stored_enchantments")
CUSTOM_NAME = TABLE.component_id("minecraft:custom_name")
CODECS = VERSION.component_codecs


def identifier(item_id: int) -> str | None:
    return VERSION.item_identifier(item_id)


def plain(count: int, item_id: int) -> bytes:
    return (
        PacketWriter()
        .write_varint(count)
        .write_varint(item_id)
        .write_varint(0)
        .write_varint(0)
        .to_bytes()
    )


def empty() -> bytes:
    return PacketWriter().write_varint(0).to_bytes()


def with_components(
    count: int, item_id: int, *, removed: tuple[int, ...] = (), values: tuple[int, ...] = ()
) -> bytes:
    """A stack whose component values are opaque byte strings of given lengths.

    The component IDs are 100+ so that no shipped codec claims them: these are
    exactly the values the decoder has to guess at.
    """

    writer = (
        PacketWriter()
        .write_varint(count)
        .write_varint(item_id)
        .write_varint(len(values))
        .write_varint(len(removed))
    )
    for index, length in enumerate(values):
        writer.write_varint(100 + index)
        writer.write_raw(bytes([0xAB]) * length)
    for component_id in removed:
        writer.write_varint(component_id)
    return writer.to_bytes()


def with_known_components(
    count: int,
    item_id: int,
    *,
    damage: int | None = None,
    max_damage: int | None = None,
    unbreakable: bool = False,
    enchantments: tuple[tuple[int, int], ...] = (),
) -> bytes:
    """A stack whose components all have a codec, so the patch is exact."""

    added = 0
    body = PacketWriter()
    if damage is not None:
        body.write_varint(DAMAGE).write_varint(damage)
        added += 1
    if max_damage is not None:
        body.write_varint(MAX_DAMAGE).write_varint(max_damage)
        added += 1
    if unbreakable:
        body.write_varint(UNBREAKABLE)
        added += 1
    if enchantments:
        body.write_varint(ENCHANTMENTS).write_varint(len(enchantments))
        for enchantment_id, level in enchantments:
            body.write_varint(enchantment_id).write_varint(level)
        added += 1
    header = (
        PacketWriter()
        .write_varint(count)
        .write_varint(item_id)
        .write_varint(added)
        .write_varint(0)
    )
    return header.write_raw(body.to_bytes()).to_bytes()


class ComponentTableTests(unittest.TestCase):
    def test_every_release_ships_a_component_table(self) -> None:
        for name in ("1.21.8", "1.21.11", "26.1.1", "26.2", "26.3"):
            table = get_version(name).components
            self.assertIsNotNone(table, name)
            self.assertGreater(len(table), 90, name)  # type: ignore[arg-type]

    def test_ids_and_names_reverse_each_other(self) -> None:
        self.assertEqual(TABLE.identifier(DAMAGE), "minecraft:damage")
        self.assertEqual(TABLE.component_id("minecraft:damage"), DAMAGE)
        self.assertEqual(TABLE.component_id("minecraft:damage"), DAMAGE)
        self.assertIsNone(TABLE.identifier(100_000))
        self.assertIsNone(TABLE.component_id("protobot:nope"))

    def test_the_version_spec_reaches_the_table(self) -> None:
        self.assertEqual(VERSION.component_identifier(DAMAGE), "minecraft:damage")
        self.assertEqual(get_version("1.21.8").component_identifier(100_000), None)

    def test_clear_cache_reloads_the_same_table(self) -> None:
        clear_component_cache()

        reloaded = component_table_for(VERSION.protocol)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.by_name, TABLE.by_name)  # type: ignore[union-attr]
        self.assertIsInstance(TABLE.component_id("minecraft:damage"), int)

    def test_only_known_kinds_are_registered(self) -> None:
        self.assertIn("minecraft:damage", KINDS_BY_NAME)
        self.assertNotIn("minecraft:container", KINDS_BY_NAME)
        self.assertEqual(len(codecs_by_id(TABLE)), len(KINDS_BY_NAME))


class PlainStackTests(unittest.TestCase):
    def test_an_empty_stack_is_one_byte(self) -> None:
        stack = read_stack(empty(), item_identifier=identifier)

        self.assertTrue(stack.item.empty)
        self.assertEqual(stack.length, 1)

    def test_a_plain_stack_reads_its_identity(self) -> None:
        stack = read_stack(plain(5, DIRT_ID), item_identifier=identifier)

        self.assertEqual(stack.item.count, 5)
        self.assertEqual(stack.item.item_id, DIRT_ID)
        self.assertEqual(stack.item.identifier, "minecraft:dirt")
        self.assertFalse(stack.item.has_components)

    def test_removed_components_are_kept(self) -> None:
        raw = with_components(1, DIRT_ID, removed=(3, 4))

        stack = read_stack(raw, item_identifier=identifier)

        self.assertEqual(stack.item.removed_component_ids, (3, 4))
        self.assertEqual(stack.stop, len(raw))

    def test_trailing_bytes_are_rejected(self) -> None:
        with self.assertRaises(StackFrameError):
            read_stack(plain(1, DIRT_ID) + b"\x00", item_identifier=identifier)


class ComponentStackTests(unittest.TestCase):
    def test_a_single_unknown_component_is_delimited_by_the_packet(self) -> None:
        raw = with_components(1, 900, values=(7,))

        stack = read_stack(raw, item_identifier=identifier)

        self.assertEqual(stack.item.count, 1)
        self.assertEqual(stack.stop, len(raw))
        self.assertTrue(stack.item.has_components)
        # one varint type ID plus the seven opaque bytes
        self.assertEqual(len(stack.item.components), 1 + 7)

    def test_known_components_are_stepped_over_exactly(self) -> None:
        raw = with_known_components(
            1, DIRT_ID, damage=12, max_damage=250, unbreakable=True, enchantments=((5, 2),)
        )

        stack = read_stack(
            raw, item_identifier=identifier, codecs=CODECS
        )

        self.assertEqual(stack.item.count, 1)
        self.assertEqual(stack.stop, len(raw))
        self.assertTrue(stack.item.has_components)
        self.assertGreater(len(stack.item.components), 0)

    def test_ambiguous_values_are_refused_rather_than_guessed(self) -> None:
        # Two unknown values can be split in many ways, so no split may be
        # reported as *the* answer: a wrong one would shift every later slot.
        raw = with_components(2, 900, values=(3, 0, 11))

        with self.assertRaises(StackFrameError):
            read_stack(raw, item_identifier=identifier)

    def test_the_slots_after_a_component_stack_stay_readable(self) -> None:
        raw = (
            plain(1, DIRT_ID)
            + with_known_components(1, 900, damage=3)
            + plain(3, STONE_ID)
        )

        frames = read_stacks(raw, 3, item_identifier=identifier, codecs=CODECS)

        self.assertEqual([frame.item.count for frame in frames], [1, 1, 3])
        self.assertEqual(frames[2].item.identifier, "minecraft:stone")
        self.assertEqual(frames[1].stop, frames[2].start)

    def test_an_unknown_component_in_a_list_is_refused(self) -> None:
        raw = with_known_components(1, 900, damage=4) + plain(2, STONE_ID)

        with self.assertRaises(StackFrameError):
            read_stacks(raw, 2, item_identifier=identifier, codecs={})

    def test_garbage_is_reported_as_unrecoverable(self) -> None:
        with self.assertRaises(StackFrameError):
            read_stacks(b"\xff" * 4, 1, item_identifier=identifier)

    def test_an_empty_stream_reads_as_no_stacks(self) -> None:
        self.assertEqual(
            read_remaining_stacks(b"", item_identifier=identifier, start=0, end=0), []
        )

    def test_a_truncated_stream_is_unrecoverable(self) -> None:
        with self.assertRaises(StackFrameError):
            read_remaining_stacks(
                plain(1, DIRT_ID)[:-1], item_identifier=identifier
            )

    def test_an_equipment_style_stream_reads_to_the_end(self) -> None:
        raw = plain(1, DIRT_ID) + with_known_components(1, 900, damage=2)

        frames = read_remaining_stacks(raw, item_identifier=identifier, codecs=CODECS)

        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[-1].stop, len(raw))


class ComponentCodecTests(unittest.TestCase):
    def test_a_varint_component_advances_by_its_own_bytes(self) -> None:
        raw = with_known_components(1, DIRT_ID, damage=300)

        stack = read_stack(raw, item_identifier=identifier, codecs=CODECS)

        # 300 as a varint is ``ac 02``; the patch must end right after it.
        self.assertTrue(stack.item.components.endswith(b"\xac\x02"))

    def test_an_enchantment_map_is_counted(self) -> None:
        raw = with_known_components(
            1, DIRT_ID, enchantments=((5, 2), (9, 1), (12, 3))
        )

        stack = read_stack(raw, item_identifier=identifier, codecs=CODECS)

        self.assertEqual(stack.stop, len(raw))

    def test_a_unit_component_takes_no_bytes(self) -> None:
        raw = (
            PacketWriter()
            .write_varint(1)
            .write_varint(DIRT_ID)
            .write_varint(1)
            .write_varint(0)
            .write_varint(UNBREAKABLE)
            .to_bytes()
        )

        stack = read_stack(raw, item_identifier=identifier, codecs=CODECS)

        self.assertEqual(stack.length, len(raw))
        self.assertEqual(stack.item.components, bytes([UNBREAKABLE]))

    def test_a_text_component_is_read_as_nbt(self) -> None:
        # network NBT: TAG_String, then the unsigned-short length and the bytes
        # (network NBT has no root name)
        text = b"\x08\x00\x04dirt"
        raw = (
            PacketWriter()
            .write_varint(1)
            .write_varint(DIRT_ID)
            .write_varint(1)
            .write_varint(0)
            .write_varint(CUSTOM_NAME)
            .write_raw(text)
            .to_bytes()
        )

        stack = read_stack(raw, item_identifier=identifier, codecs=CODECS)

        self.assertEqual(stack.stop, len(raw))
        self.assertTrue(stack.item.components.endswith(text))

    def test_a_truncated_value_is_not_accepted(self) -> None:
        # A damage component whose varint is cut off cannot be read, so this
        # split is rejected instead of being taken on trust.
        raw = (
            PacketWriter()
            .write_varint(1)
            .write_varint(DIRT_ID)
            .write_varint(1)
            .write_varint(0)
            .write_varint(DAMAGE)
            .write_raw(b"\xff")
            .to_bytes()
        )

        with self.assertRaises(StackFrameError):
            read_stack(raw, item_identifier=identifier, codecs=CODECS)


class ContentPacketTests(unittest.TestCase):
    def _bot(self) -> Bot:
        stub = object.__new__(Bot)
        stub.version = VERSION
        return stub

    def _content(self, *stacks: bytes, carried: bytes | None = None) -> bytes:
        writer = PacketWriter().write_varint(len(stacks))
        for stack in stacks:
            writer.write_raw(stack)
        writer.write_raw(carried if carried is not None else empty())
        return writer.to_bytes()

    def test_content_with_known_components_decodes_every_slot(self) -> None:
        component_raw = with_known_components(1, 900, damage=1500, max_damage=1561)
        carried_raw = with_known_components(2, 901, enchantments=((5, 3),))
        body = self._content(
            plain(1, DIRT_ID),
            component_raw,
            plain(4, STONE_ID),
            carried=carried_raw,
        )
        state = ContainerState(0)

        self._bot()._decode_container_content(state, body)

        self.assertEqual(state.slot_count, 3)
        self.assertEqual(sorted(state.items), [0, 1, 2])
        self.assertEqual(state.items[2].identifier, "minecraft:stone")
        self.assertEqual(state.items[1].count, 1)
        self.assertTrue(state.items[1].has_components)
        self.assertFalse(state.unreadable)
        self.assertEqual(state.carried_item.count, 2)
        self.assertFalse(state.carried_unknown)
        self.assertEqual(state.slots[1], component_raw)
        self.assertEqual(state.carried_item_data, carried_raw)

    def test_an_unreadable_component_keeps_the_window_byte_only(self) -> None:
        body = self._content(
            plain(1, DIRT_ID),
            with_components(1, 900, values=(6,)),
            plain(4, STONE_ID),
        )
        state = ContainerState(0)

        self._bot()._decode_container_content(state, body)

        self.assertEqual(state.slot_count, 3)
        self.assertEqual(state.items, {})
        self.assertEqual(state.unreadable, {0, 1, 2})
        self.assertTrue(state.carried_unknown)

    def test_content_that_cannot_be_split_stays_byte_only(self) -> None:
        body = PacketWriter().write_varint(2).write_raw(b"\xff\xff\xff").to_bytes()
        state = ContainerState(0)

        self._bot()._decode_container_content(state, body)

        self.assertEqual(state.slot_count, 2)
        self.assertEqual(state.unreadable, {0, 1})
        self.assertTrue(state.carried_unknown)
        self.assertEqual(state.items, {})

    def test_an_unknown_item_id_still_decodes(self) -> None:
        body = self._content(plain(3, 4000))

        state = ContainerState(0)
        self._bot()._decode_container_content(state, body)

        self.assertEqual(state.items[0].count, 3)
        self.assertIsNone(state.items[0].identifier)

    def test_a_component_cursor_packet_decodes_exactly(self) -> None:
        # One stack that fills the payload: the end of the packet is the length,
        # which is why the cursor copy can carry components.
        raw = with_components(1, 900, values=(5,))
        stub = object.__new__(Bot)
        stub.version = VERSION
        stub.state = None

        item = stub._decode_stack_frame(raw)

        self.assertIsInstance(item, ItemStack)
        self.assertTrue(item.has_components)
        self.assertEqual(item.count, 1)


if __name__ == "__main__":
    unittest.main()
