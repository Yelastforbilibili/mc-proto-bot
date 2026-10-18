"""Unit tests for the interaction layer.

No network and no server: a fake bot records the packets and a fake world
answers block lookups, which is enough to pin down the wire format (the part
that breaks silently when a release shifts an id) and the completion logic
(the part that breaks silently when a block update is missed).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from protobot.actions import (
    ActionStatus,
    Actions,
    BlockFace,
    UnsupportedAction,
    look_angles,
    raycast_blocks,
)
from protobot.events import EventBus
from protobot.physics import Vec3
from protobot.protocol.codec import PacketReader
from protobot.protocol.versions import get_version


class _FakeWorld:
    def __init__(self, states=None) -> None:
        self.states = dict(states or {})

    def get_state_id(self, x: int, y: int, z: int) -> int:
        return self.states.get((x, y, z), 0)

    def set_state_id(self, x: int, y: int, z: int, state_id: int) -> None:
        self.states[(x, y, z)] = state_id


class _FakeBot:
    """The slice of ``Bot`` the interaction layer actually touches."""

    def __init__(self, version, world=None) -> None:
        self.version = version
        self.sent: list[tuple[int, bytes]] = []
        self.looks: list[tuple[float, float]] = []
        self.events = EventBus()
        self.world = world if world is not None else _FakeWorld()
        self.physics_state = SimpleNamespace(
            position=Vec3(0.5, 64.0, 0.5),
            eye_height=1.62,
            yaw=0.0,
            pitch=0.0,
        )
        self.ticks = 0
        self._sequence = 0
        self.on_tick = None

    async def send_raw(self, packet_id: int, payload: bytes = b"") -> None:
        self.sent.append((packet_id, payload))

    async def send_look(self, yaw: float, pitch: float) -> None:
        self.looks.append((yaw, pitch))
        self.physics_state.yaw = yaw
        self.physics_state.pitch = pitch

    def take_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1
        return sequence

    async def tick(self):
        self.ticks += 1
        if self.on_tick is not None:
            self.on_tick()
        return self.physics_state


class BlockFaceTests(unittest.TestCase):
    def test_ids_match_the_protocol(self) -> None:
        self.assertEqual(
            [face.name for face in BlockFace],
            ["DOWN", "UP", "NORTH", "SOUTH", "WEST", "EAST"],
        )
        self.assertEqual(int(BlockFace.UP), 1)

    def test_steps_and_opposites(self) -> None:
        self.assertEqual(BlockFace.UP.step, (0, 1, 0))
        self.assertEqual(BlockFace.NORTH.step, (0, 0, -1))
        self.assertEqual(BlockFace.WEST.opposite, BlockFace.EAST)
        self.assertEqual(BlockFace.from_step(0, -1, 0), BlockFace.DOWN)
        with self.assertRaises(ValueError):
            BlockFace.from_step(1, 1, 0)


class RaycastTests(unittest.TestCase):
    def test_hits_the_nearest_block_and_reports_the_face(self) -> None:
        world = _FakeWorld({(0, 64, 3): 7})
        hit = raycast_blocks(world, Vec3(0.5, 64.5, 0.5), Vec3(0.0, 0.0, 1.0))
        assert hit is not None
        self.assertEqual(hit.position, (0, 64, 3))
        self.assertEqual(hit.state_id, 7)
        self.assertEqual(hit.face, BlockFace.NORTH)
        self.assertAlmostEqual(hit.distance, 2.5, places=5)
        self.assertAlmostEqual(hit.point.z, 3.0, places=5)

    def test_northward_face_when_walking_the_other_way(self) -> None:
        world = _FakeWorld({(0, 64, -2): 6})
        hit = raycast_blocks(world, Vec3(0.5, 64.5, 0.5), Vec3(0.0, 0.0, -1.0))
        assert hit is not None
        self.assertEqual(hit.position, (0, 64, -2))
        self.assertEqual(hit.face, BlockFace.SOUTH)
        self.assertAlmostEqual(hit.distance, 1.5, places=5)

    def test_air_is_transparent(self) -> None:
        world = _FakeWorld({(0, 64, 2): 0, (0, 64, 4): 3})
        hit = raycast_blocks(world, Vec3(0.5, 64.5, 0.5), Vec3(0.0, 0.0, 1.0))
        assert hit is not None
        self.assertEqual(hit.position, (0, 64, 4))

    def test_reach_limit(self) -> None:
        world = _FakeWorld({(0, 64, 9): 2})
        self.assertIsNone(
            raycast_blocks(
                world, Vec3(0.5, 64.5, 0.5), Vec3(0.0, 0.0, 1.0), max_distance=4.5
            )
        )

    def test_downward_ray_enters_the_top_face(self) -> None:
        world = _FakeWorld({(0, 63, 0): 4})
        hit = raycast_blocks(world, Vec3(0.5, 64.5, 0.5), Vec3(0.0, -1.0, 0.0))
        assert hit is not None
        self.assertEqual(hit.position, (0, 63, 0))
        self.assertEqual(hit.face, BlockFace.UP)

    def test_starting_inside_a_block(self) -> None:
        world = _FakeWorld({(0, 64, 0): 5})
        hit = raycast_blocks(world, Vec3(0.5, 64.5, 0.5), Vec3(0.0, 0.0, 1.0))
        assert hit is not None
        self.assertEqual(hit.position, (0, 64, 0))
        self.assertEqual(hit.distance, 0.0)

    def test_zero_direction_has_no_hit(self) -> None:
        self.assertIsNone(
            raycast_blocks(_FakeWorld(), Vec3(0.0, 0.0, 0.0), Vec3(0.0, 0.0, 0.0))
        )


class LookAngleTests(unittest.TestCase):
    def test_forward_is_yaw_zero(self) -> None:
        yaw, pitch = look_angles(Vec3(0.0, 0.0, 0.0), Vec3(0.0, 0.0, 5.0))
        self.assertAlmostEqual(yaw, 0.0, places=5)
        self.assertAlmostEqual(pitch, 0.0, places=5)

    def test_down_is_positive_pitch(self) -> None:
        _, pitch = look_angles(Vec3(0.0, 10.0, 0.0), Vec3(0.0, 0.0, 0.0))
        self.assertAlmostEqual(pitch, 90.0, places=5)

    def test_round_trip_through_direction(self) -> None:
        from protobot.actions.raycast import direction_from_angles

        for yaw, pitch in ((0.0, 0.0), (90.0, 30.0), (-135.0, -45.0)):
            direction = direction_from_angles(yaw, pitch)
            again_yaw, again_pitch = look_angles(Vec3(0.0, 0.0, 0.0), direction)
            self.assertAlmostEqual((again_yaw - yaw + 180) % 360 - 180, 0.0, places=4)
            self.assertAlmostEqual(again_pitch, pitch, places=4)


class PayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_dig_start_and_stop_payloads_on_26_3(self) -> None:
        bot = _FakeBot(get_version("26.3"), _FakeWorld({(1, 64, 2): 9}))
        bot.on_tick = lambda: bot.world.set_state_id(1, 64, 2, 0)
        result = await Actions(bot).dig((1, 64, 2), face=BlockFace.NORTH)
        self.assertEqual(result.status, ActionStatus.CONFIRMED)
        self.assertEqual(len(bot.sent), 2)

        start_id, start_payload = bot.sent[0]
        self.assertEqual(start_id, 0x29)  # serverbound_player_action on 26.3
        reader = PacketReader(start_payload)
        self.assertEqual(reader.read_varint(), 0)  # START_DESTROY_BLOCK
        self.assertEqual(reader.read_position(), (1, 64, 2))
        self.assertEqual(reader.read_unsigned_byte(), int(BlockFace.NORTH))
        self.assertEqual(reader.read_varint(), 0)  # first sequence
        self.assertEqual(reader.read_remaining(), b"")

        stop_id, stop_payload = bot.sent[1]
        self.assertEqual(stop_id, 0x29)
        reader = PacketReader(stop_payload)
        # 26.3 inserted CHANGE_DESTROY_DIRECTION at ordinal 1, so STOP is 3 --
        # sending the 1.21.x value 2 would abort the dig instead of finishing it.
        self.assertEqual(reader.read_varint(), 3)
        self.assertEqual(reader.read_position(), (1, 64, 2))
        self.assertEqual(reader.read_unsigned_byte(), int(BlockFace.NORTH))
        self.assertEqual(reader.read_varint(), 1)
        self.assertEqual(reader.read_remaining(), b"")

    async def test_dig_stop_keeps_the_classic_code_before_26_3(self) -> None:
        bot = _FakeBot(get_version("26.1"), _FakeWorld({(1, 64, 2): 9}))
        bot.on_tick = lambda: bot.world.set_state_id(1, 64, 2, 0)
        await Actions(bot).dig((1, 64, 2), face=BlockFace.NORTH)
        reader = PacketReader(bot.sent[1][1])
        self.assertEqual(reader.read_varint(), 2)  # STOP_DESTROY_BLOCK on 26.1

    async def test_dig_aims_before_sending(self) -> None:
        bot = _FakeBot(get_version("26.3"), _FakeWorld({(1, 63, 2): 4}))
        bot.on_tick = lambda: bot.world.set_state_id(1, 63, 2, 0)
        await Actions(bot).dig((1, 63, 2), face=BlockFace.UP)
        self.assertEqual(len(bot.looks), 1)
        yaw, pitch = bot.looks[0]
        # The block is one east (x + 1) and two south (z + 2) of the player, so
        # the yaw is atan2(-1, 2) and the pitch points down at its top face.
        self.assertAlmostEqual(yaw, -26.5650511, places=4)
        self.assertGreater(pitch, 0.0)

    async def test_dig_reports_unconfirmed_when_nothing_changes(self) -> None:
        bot = _FakeBot(get_version("26.3"), _FakeWorld({(1, 64, 2): 9}))
        result = await Actions(bot).dig(
            (1, 64, 2), hold=0.0, confirm_timeout=0.0, tick=False
        )
        self.assertEqual(result.status, ActionStatus.UNCONFIRMED)
        self.assertEqual(result.state_before, 9)
        self.assertEqual(result.state_after, 9)
        self.assertIn("no block update", result.evidence)

    async def test_dig_rejects_air(self) -> None:
        bot = _FakeBot(get_version("26.3"))
        result = await Actions(bot).dig((0, 64, 0))
        self.assertEqual(result.status, ActionStatus.REJECTED)
        self.assertEqual(bot.sent, [])

    async def test_swing_is_version_aware(self) -> None:
        bot = _FakeBot(get_version("26.1"))
        await Actions(bot).swing()
        self.assertEqual(bot.sent[0][0], 0x3F)  # "swing" on 26.1.x
        self.assertEqual(bot.sent[0][1], b"\x00")  # carries the hand
        bot = _FakeBot(get_version("26.3"))
        await Actions(bot).swing(hand="off_hand")
        self.assertEqual(bot.sent[0][0], 0x2E)  # renamed to "punch" on 26.3
        # 26.3's ServerboundPunchPacket is a record with no fields at all.
        self.assertEqual(bot.sent[0][1], b"")

    async def test_use_item_on_payload(self) -> None:
        bot = _FakeBot(get_version("26.3"))
        result = await Actions(bot).use_item_on(
            (3, 64, -2), face=BlockFace.SOUTH, cursor=(0.25, 1.0, 0.75)
        )
        self.assertEqual(result.status, ActionStatus.SENT)
        packet_id, payload = bot.sent[0]
        self.assertEqual(packet_id, 0x42)
        reader = PacketReader(payload)
        self.assertEqual(reader.read_varint(), 0)  # main hand
        self.assertEqual(reader.read_position(), (3, 64, -2))
        self.assertEqual(reader.read_varint(), int(BlockFace.SOUTH))
        self.assertAlmostEqual(reader.read_float(), 0.25, places=5)
        self.assertAlmostEqual(reader.read_float(), 1.0, places=5)
        self.assertAlmostEqual(reader.read_float(), 0.75, places=5)
        self.assertFalse(reader.read_bool())  # isInside
        self.assertFalse(reader.read_bool())  # isWorldBorderHit
        self.assertEqual(reader.read_varint(), 0)
        self.assertEqual(reader.read_remaining(), b"")
        # Both booleans are on the wire on every release; one boolean shifts
        # ``sequence`` by a byte and the server drops the placement silently.
        self.assertEqual(
            len(payload),
            1 + 8 + 1 + 12 + 1 + 1 + 1,
            "hand + packed position + face + 3 floats + 2 booleans + sequence",
        )

    async def test_place_block_places_above_the_clicked_block(self) -> None:
        bot = _FakeBot(get_version("26.3"), _FakeWorld({(0, 64, 0): 5}))
        bot.on_tick = lambda: bot.world.set_state_id(0, 65, 0, 12)
        result = await Actions(bot).place_block((0, 65, 0), face=BlockFace.UP)
        self.assertEqual(result.status, ActionStatus.CONFIRMED)
        packet_id, payload = bot.sent[0]
        self.assertEqual(packet_id, 0x42)
        reader = PacketReader(payload)
        reader.read_varint()
        self.assertEqual(reader.read_position(), (0, 64, 0))  # clicked block
        self.assertEqual(reader.read_varint(), int(BlockFace.UP))

    async def test_place_block_rejects_occupied_destination(self) -> None:
        bot = _FakeBot(get_version("26.3"), _FakeWorld({(0, 65, 0): 3}))
        result = await Actions(bot).place_block((0, 65, 0))
        self.assertEqual(result.status, ActionStatus.REJECTED)
        self.assertEqual(bot.sent, [])

    async def test_attack_uses_the_split_attack_packet_on_26_3(self) -> None:
        bot = _FakeBot(get_version("26.3"))
        result = await Actions(bot).attack(42)
        ids = [packet_id for packet_id, _ in bot.sent]
        self.assertEqual(ids, [0x01, 0x2E])
        reader = PacketReader(bot.sent[0][1])
        self.assertEqual(reader.read_varint(), 42)
        self.assertEqual(result.entity_id, 42)

    async def test_attack_falls_back_to_interact_before_26_3(self) -> None:
        bot = _FakeBot(get_version("1.21.11"))
        await Actions(bot).attack(7, swing=False)
        packet_id, payload = bot.sent[0]
        self.assertEqual(packet_id, 0x19)  # serverbound_interact
        reader = PacketReader(payload)
        self.assertEqual(reader.read_varint(), 7)
        self.assertEqual(reader.read_varint(), 1)  # ATTACK
        self.assertEqual(reader.read_remaining(), b"")

    async def test_interact_payload(self) -> None:
        # 26.1 flattened Interact: no type tag, the point is always sent.
        bot = _FakeBot(get_version("26.3"))
        await Actions(bot).interact(11, hand="off_hand")
        packet_id, payload = bot.sent[0]
        self.assertEqual(packet_id, 0x1A)
        reader = PacketReader(payload)
        self.assertEqual(reader.read_varint(), 11)
        self.assertEqual(reader.read_varint(), 1)  # off hand
        self.assertEqual(reader.read_lp_vec3(), (0.0, 0.0, 0.0))
        self.assertFalse(reader.read_bool())  # usingSecondaryAction
        self.assertEqual(reader.read_remaining(), b"")

    async def test_interact_keeps_the_type_tag_before_26_1(self) -> None:
        bot = _FakeBot(get_version("1.21.11"))
        await Actions(bot).interact(11, hand="off_hand")
        reader = PacketReader(bot.sent[0][1])
        self.assertEqual(reader.read_varint(), 11)
        self.assertEqual(reader.read_varint(), 0)  # INTERACT
        self.assertEqual(reader.read_varint(), 1)  # off hand
        self.assertEqual(reader.read_remaining(), b"")

    async def test_interact_at_carries_a_point(self) -> None:
        bot = _FakeBot(get_version("26.3"))
        await Actions(bot).interact_at(11, Vec3(0.1, 0.2, 0.3))
        reader = PacketReader(bot.sent[0][1])
        self.assertEqual(reader.read_varint(), 11)
        self.assertEqual(reader.read_varint(), 0)  # main hand
        x, y, z = reader.read_lp_vec3()
        self.assertAlmostEqual(x, 0.1, places=2)
        self.assertAlmostEqual(y, 0.2, places=2)
        self.assertAlmostEqual(z, 0.3, places=2)

    async def test_dig_looked_at_uses_the_ray(self) -> None:
        # Eye height 1.62 puts the eye inside y=65, so the block at y=65 is the
        # nearest one along +Z even though another block sits behind it.
        world = _FakeWorld({(0, 64, 2): 8, (0, 65, 2): 1})
        bot = _FakeBot(get_version("26.3"), world)
        bot.on_tick = lambda: world.set_state_id(0, 65, 2, 0)
        result = await Actions(bot).dig_looked_at()
        self.assertEqual(result.position, (0, 65, 2))
        self.assertEqual(result.status, ActionStatus.CONFIRMED)


class UnsupportedActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unaudited_ids_are_refused_instead_of_guessed(self) -> None:
        from dataclasses import replace

        from protobot.protocol.versions import VersionSpec

        # A release whose interaction ids were never audited: the ids the
        # action layer depends on are 0, which means "not verified here".
        base = get_version("26.3")
        packets = replace(
            base.packets,
            serverbound_player_action=0,
            serverbound_use_item_on=0,
            serverbound_interact=0,
            serverbound_swing=0,
        )
        version = VersionSpec(
            name="test-version",
            protocol=999,
            data_version=999,
            packets=packets,
            item_identifiers=base.item_identifiers,
            hard_collision_entities=base.hard_collision_entities,
            boat_entities=base.boat_entities,
            chest_boat_entities=base.chest_boat_entities,
            raft_entities=base.raft_entities,
            happy_ghast_entity=base.happy_ghast_entity,
            shulker_entity=base.shulker_entity,
            configuration=base.configuration,
        )
        bot = _FakeBot(version, _FakeWorld({(0, 64, 0): 3}))
        with self.assertRaises(UnsupportedAction):
            await Actions(bot).dig((0, 64, 0))
        with self.assertRaises(UnsupportedAction):
            await Actions(bot).swing()
        self.assertEqual(bot.sent, [])


if __name__ == "__main__":
    unittest.main()
