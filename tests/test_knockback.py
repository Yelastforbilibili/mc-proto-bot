"""Regression tests for the knockback (server velocity) path.

A vanilla client applies an impulse between two ticks and then moves, so the
very next movement packet already carries it.  Our reader task and tick loop
run concurrently, so an impulse can land *after* a tick has run its physics and
*before* that tick's packet goes out.  Claiming the pre-impulse position there
looks like "did not take the knockback" to a movement prediction check; these
tests pin down the recompute that avoids it.
"""

from __future__ import annotations

import time
import unittest

from protobot.client import Bot
from protobot.physics.engine import PhysicsState
from protobot.physics.geometry import Vec3
from protobot.protocol import PacketWriter
from protobot.protocol.connection import ConnectionState


def _make_bot(
    version: str = "26.3", *, knockback: bool = True
) -> tuple[Bot, list[tuple[int, bytes]]]:
    bot = Bot("localhost", username="ProtoKB", version=version, knockback=knockback)
    bot.state = ConnectionState.PLAY
    sent: list[tuple[int, bytes]] = []

    async def capture(packet_id: int, payload: bytes = b"") -> None:
        sent.append((packet_id, payload))

    bot.send_raw = capture  # type: ignore[method-assign]
    return bot, sent


def _motion(entity_id: int, x: float, y: float, z: float) -> bytes:
    return (
        PacketWriter()
        .write_varint(entity_id)
        .write_lp_vec3(x, y, z)
        .to_bytes()
    )


class MotionPacketTests(unittest.IsolatedAsyncioTestCase):
    async def test_impulse_sets_velocity_and_bumps_epoch(self) -> None:
        bot, _ = _make_bot()
        bot.session.entity_id = 7
        bot.player.loaded = True
        seen: list[tuple[int, tuple[float, float, float]]] = []
        bot.events.on(
            "entity_motion",
            lambda entity_id, velocity, entity: seen.append((entity_id, velocity)),
        )

        await bot._handle_set_entity_motion(_motion(7, 0.0, 0.3608, 0.4))

        self.assertAlmostEqual(bot.physics_state.velocity.z, 0.4, places=4)
        self.assertAlmostEqual(bot.physics_state.velocity.y, 0.3608, places=4)
        self.assertEqual(bot._motion_epoch, 1)
        self.assertEqual([entry[0] for entry in seen], [7])

    async def test_knockback_off_keeps_velocity_and_skips_replay(self) -> None:
        bot, _ = _make_bot(knockback=False)
        bot.session.entity_id = 7
        bot.player.loaded = True
        before = bot.physics_state.velocity

        await bot._handle_set_entity_motion(_motion(7, 1.0, 1.0, 1.0))

        self.assertEqual(bot.physics_state.velocity, before)
        self.assertEqual(bot.player.velocity_x, 0.0)
        # Nothing to replay: the impulse was dropped on purpose, so the tick
        # that overlaps it must not rewind and recompute.
        self.assertEqual(bot._motion_epoch, 0)

    async def test_other_entity_impulse_does_not_touch_player(self) -> None:
        bot, _ = _make_bot()
        bot.session.entity_id = 7
        bot.player.loaded = True

        await bot._handle_set_entity_motion(_motion(9, 1.0, 1.0, 1.0))

        self.assertEqual(bot.physics_state.velocity.z, 0.0)
        self.assertEqual(bot._motion_epoch, 0)

    def test_knockback_must_be_a_bool(self) -> None:
        with self.assertRaises(TypeError):
            Bot("localhost", username="ProtoKB", knockback="yes")  # type: ignore[arg-type]


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_rewinds_every_field_and_keeps_identity(self) -> None:
        bot, _ = _make_bot()
        state = bot.physics_state
        state.position = Vec3(state.position.x, 64.0, state.position.z)
        state.velocity = Vec3(0.25, state.velocity.y, state.velocity.z)
        state.status_effects = {"speed": object()}  # type: ignore[dict-item]
        before = bot._snapshot_physics_state()

        state.position = Vec3(state.position.x, 10.0, state.position.z)
        state.velocity = Vec3(-1.0, state.velocity.y, state.velocity.z)
        state.status_effects = {}
        bot._restore_physics_state(before)

        self.assertIs(bot.physics_state, state)
        self.assertAlmostEqual(state.position.y, 64.0, places=6)
        self.assertAlmostEqual(state.velocity.x, 0.25, places=6)
        self.assertIn("speed", state.status_effects)

    async def test_snapshot_copies_mutable_containers(self) -> None:
        bot, _ = _make_bot()
        bot.physics_state.attribute_values = {"movement_speed": 0.1}
        snapshot = bot._snapshot_physics_state()

        bot.physics_state.attribute_values["movement_speed"] = 0.5
        bot.physics_state.status_effects["haste"] = object()  # type: ignore[assignment]

        self.assertEqual(snapshot.attribute_values, {"movement_speed": 0.1})
        self.assertEqual(snapshot.status_effects, {})


class MidTickImpulseTests(unittest.IsolatedAsyncioTestCase):
    """The actual bug: an impulse that lands while a tick is in flight."""

    async def _run_tick(
        self, *, impulse: tuple[float, float, float] | None
    ) -> tuple[Bot, PhysicsState, list[tuple[float, float, float]], int]:
        bot, _ = _make_bot()
        bot.session.entity_id = 21
        bot.player.loaded = True
        claimed: list[tuple[float, float, float]] = []
        original_send = bot.send_position

        async def capture_position(
            x: float,
            y: float,
            z: float,
            *,
            on_ground: bool | None = None,
            horizontal_collision: bool | None = None,
        ) -> None:
            claimed.append((x, y, z))
            await original_send(
                x,
                y,
                z,
                on_ground=on_ground,
                horizontal_collision=horizontal_collision,
            )

        bot.send_position = capture_position  # type: ignore[method-assign]

        steps: list[int] = []
        engine_tick = bot.physics.tick

        def counting_tick(state, controls, world) -> None:
            steps.append(1)
            engine_tick(state, controls, world)

        bot.physics.tick = counting_tick  # type: ignore[method-assign]

        original_budget = bot.wait_tick_budget
        original_sync_input = bot._sync_input

        async def budget() -> None:
            # Keep the test fast; the real budget only paces the packets.
            await original_budget()

        async def sync_input(**values: bool) -> None:
            await original_sync_input(**values)
            # This is where the reader task can run: the step above is done and
            # the movement packet has not gone out yet.
            if impulse is not None:
                x, y, z = impulse
                await bot._handle_set_entity_motion(_motion(21, x, y, z))

        bot.wait_tick_budget = budget  # type: ignore[method-assign]
        bot._sync_input = sync_input  # type: ignore[method-assign]

        await bot._tick_once()
        return bot, bot.physics_state, claimed, len(steps)

    async def test_impulse_during_tick_is_claimed_in_the_same_packet(self) -> None:
        control_bot, control_state, control_claims, control_steps = await self._run_tick(
            impulse=None
        )
        hit_bot, hit_state, hit_claims, hit_steps = await self._run_tick(
            impulse=(0.0, 0.0, 0.4)
        )

        self.assertEqual(control_steps, 1)
        self.assertEqual(hit_steps, 2)  # computed, rewound, recomputed
        self.assertEqual(len(control_claims), 1)
        self.assertEqual(len(hit_claims), 1)
        # The claimed step is the recomputed one, so it carries the impulse.
        self.assertAlmostEqual(hit_claims[0][2], hit_state.position.z, places=6)
        self.assertGreater(hit_claims[0][2], control_claims[0][2] + 0.1)
        self.assertNotAlmostEqual(hit_state.velocity.z, control_state.velocity.z)

    async def test_tick_without_impulse_is_not_replayed(self) -> None:
        _, _, claims, steps = await self._run_tick(impulse=None)

        self.assertEqual(steps, 1)
        self.assertEqual(len(claims), 1)

    async def test_knockback_off_ignores_the_impulse(self) -> None:
        bot, _ = _make_bot(knockback=False)
        bot.session.entity_id = 21
        bot.player.loaded = True
        claimed: list[tuple[float, float, float]] = []
        original_send = bot.send_position

        async def capture_position(
            x: float,
            y: float,
            z: float,
            *,
            on_ground: bool | None = None,
            horizontal_collision: bool | None = None,
        ) -> None:
            claimed.append((x, y, z))
            await original_send(
                x,
                y,
                z,
                on_ground=on_ground,
                horizontal_collision=horizontal_collision,
            )

        bot.send_position = capture_position  # type: ignore[method-assign]

        original_budget = bot.wait_tick_budget
        original_sync_input = bot._sync_input

        async def budget() -> None:
            await original_budget()

        async def sync_input(**values: bool) -> None:
            await original_sync_input(**values)
            await bot._handle_set_entity_motion(_motion(21, 0.0, 0.0, 0.4))

        bot.wait_tick_budget = budget  # type: ignore[method-assign]
        bot._sync_input = sync_input  # type: ignore[method-assign]

        await bot._tick_once()

        self.assertEqual(len(claimed), 1)
        self.assertEqual(bot.physics_state.velocity.z, 0.0)


class LookTickWindowTests(unittest.IsolatedAsyncioTestCase):
    """The window that only opens on a tick that already sent a look packet.

    Closing a tick is not free: ``end_tick`` sends ``client_tick_end`` and then
    flushes the pongs an action window held back.  That close used to happen
    inside the packet write, i.e. after the physics step and after the knockback
    epoch check, so an impulse that landed inside it reached the local velocity
    and the epoch but not the packet that was about to leave -- while the flushed
    pong had already told the anti-cheat that the very next movement must carry
    it.  A look packet claiming the tick first is the only way to reach that
    branch, which is why only aiming ticks used to lose the knockback.
    """

    async def _run_look_tick(
        self, *, impulse: tuple[float, float, float] | None
    ) -> tuple[list[tuple[float, float, float]], int]:
        bot, _ = _make_bot()
        bot.session.entity_id = 21
        bot.player.loaded = True
        # A look packet already claimed this tick, so the movement packet goes
        # through the "close the previous tick" branch.
        bot._position_sent_this_tick = True
        claimed: list[tuple[float, float, float]] = []
        original_send = bot.send_position

        async def capture_position(
            x: float,
            y: float,
            z: float,
            *,
            on_ground: bool | None = None,
            horizontal_collision: bool | None = None,
        ) -> None:
            claimed.append((x, y, z))
            await original_send(
                x,
                y,
                z,
                on_ground=on_ground,
                horizontal_collision=horizontal_collision,
            )

        bot.send_position = capture_position  # type: ignore[method-assign]

        steps: list[int] = []
        engine_tick = bot.physics.tick

        def counting_tick(state, controls, world) -> None:
            steps.append(1)
            engine_tick(state, controls, world)

        bot.physics.tick = counting_tick  # type: ignore[method-assign]

        original_end_tick = bot.end_tick
        injected: list[int] = []

        async def end_tick() -> None:
            await original_end_tick()
            # This is where the reader task can run: the previous tick is closed
            # and its pongs are out, but this tick's packet has not been built.
            if impulse is not None and not injected:
                injected.append(1)
                x, y, z = impulse
                await bot._handle_set_entity_motion(_motion(21, x, y, z))

        bot.end_tick = end_tick  # type: ignore[method-assign]

        await bot._tick_once()
        return claimed, len(steps)

    async def test_impulse_while_closing_the_previous_tick_is_claimed(self) -> None:
        control_claims, control_steps = await self._run_look_tick(impulse=None)
        hit_claims, hit_steps = await self._run_look_tick(impulse=(0.0, 0.0, 0.4))

        # The impulse landed before the snapshot, so it is part of this step and
        # there is nothing to replay.
        self.assertEqual(control_steps, 1)
        self.assertEqual(hit_steps, 1)
        self.assertEqual(len(control_claims), 1)
        self.assertEqual(len(hit_claims), 1)
        self.assertGreater(hit_claims[0][2], control_claims[0][2] + 0.1)

    async def test_the_previous_tick_is_closed_before_the_physics_step(self) -> None:
        bot, sent = _make_bot()
        bot.session.entity_id = 21
        bot.player.loaded = True
        bot._position_sent_this_tick = True
        tick_end_id = bot.version.packets.serverbound_tick_end
        closed_at_physics: list[int] = []
        engine_tick = bot.physics.tick

        def counting_tick(state, controls, world) -> None:
            closed_at_physics.append(
                sum(1 for packet_id, _ in sent if packet_id == tick_end_id)
            )
            engine_tick(state, controls, world)

        bot.physics.tick = counting_tick  # type: ignore[method-assign]

        await bot._tick_once()

        # One ``client_tick_end`` belongs to the tick a look packet already
        # claimed; it has to be on the wire before this tick computes anything,
        # otherwise an impulse can be published into a tick that was closed.
        self.assertEqual(closed_at_physics, [1])


class PongOrderTests(unittest.IsolatedAsyncioTestCase):
    """The pong is the transaction the impulse is measured against.

    The server wraps a knockback in two transactions and Grim only demands the
    impulse from the movement packets that follow the second one.  Holding the
    answer back makes the movement packet that follows look like it ignored a
    knockback that had already arrived -- ``AntiKB`` with one whole impulse as
    the offset, then a setback that undoes it.
    """

    async def test_a_ping_is_answered_at_once(self) -> None:
        bot, sent = _make_bot()

        await bot._handle_ping(7)

        self.assertEqual(bot._pong_pending, [])
        self.assertEqual(
            [packet_id for packet_id, _ in sent],
            [bot.version.packets.serverbound_pong],
        )

    async def test_a_ping_between_an_action_and_its_tick_waits(self) -> None:
        bot, sent = _make_bot()
        bot._pong_hold_until = time.monotonic() + 0.08

        await bot._handle_ping(7)

        # A transaction landing there makes Grim's ``Post`` check report the
        # action out of order, so this one answer is the exception.
        self.assertEqual(bot._pong_pending, [7])
        self.assertEqual(sent, [])

    async def test_the_movement_packet_releases_the_held_answer(self) -> None:
        bot, sent = _make_bot()
        bot._pong_hold_until = time.monotonic() + 0.08
        await bot._handle_ping(7)

        async def begin_tick() -> None:
            return None

        bot._begin_tick_packet = begin_tick  # type: ignore[method-assign]
        await bot._send_position_packet(bot.version.packets.serverbound_position, b"")

        # The movement packet goes first: only it clears the action queue the
        # answer would otherwise report the action from.
        self.assertEqual(
            [packet_id for packet_id, _ in sent],
            [
                bot.version.packets.serverbound_position,
                bot.version.packets.serverbound_pong,
            ],
        )
        self.assertEqual(bot._pong_pending, [])
        self.assertEqual(bot._pong_hold_until, 0.0)


def _explode(
    centre: tuple[float, float, float],
    *,
    radius: float = 4.0,
    blocks: int = 12,
    knockback: tuple[float, float, float] | None = None,
    trailing: bytes = b"",
) -> bytes:
    writer = (
        PacketWriter()
        .write_double(centre[0])
        .write_double(centre[1])
        .write_double(centre[2])
        .write_float(radius)
        .write_varint(blocks)
    )
    if knockback is None:
        writer = writer.write_bool(False)
    else:
        writer = (
            writer.write_bool(True)
            .write_double(knockback[0])
            .write_double(knockback[1])
            .write_double(knockback[2])
        )
    if trailing:
        writer = writer.write_bytes(trailing)
    return writer.to_bytes()


class ExplodePacketTests(unittest.IsolatedAsyncioTestCase):
    """An explosion *adds* to the velocity, where the motion packet replaces it."""

    def _prepare(self, *, knockback: bool = True):
        bot, _ = _make_bot(knockback=knockback)
        bot.session.entity_id = 7
        bot.player.loaded = True
        bot.physics_state.velocity = Vec3(0.1, 0.0, 0.05)
        bot.player.velocity_x = 0.1
        bot.player.velocity_z = 0.05
        seen: list[tuple] = []
        bot.events.on("explosion", lambda *args: seen.append(args))
        return bot, seen

    async def test_knockback_is_added_and_marks_the_tick_stale(self) -> None:
        bot, seen = self._prepare()

        await bot._handle_explode(
            _explode((3.0, -60.0, 4.0), knockback=(0.2, 0.3, -0.4))
        )

        velocity = bot.physics_state.velocity
        self.assertAlmostEqual(velocity.x, 0.3, places=6)
        self.assertAlmostEqual(velocity.y, 0.3, places=6)
        self.assertAlmostEqual(velocity.z, -0.35, places=6)
        self.assertEqual(bot.player.velocity_x, velocity.x)
        self.assertEqual(bot.player.velocity_z, velocity.z)
        self.assertEqual(bot._motion_epoch, 1)
        self.assertEqual(len(seen), 1)
        centre, radius, blocks, knockback = seen[0]
        self.assertEqual(centre, Vec3(3.0, -60.0, 4.0))
        self.assertEqual(radius, 4.0)
        self.assertEqual(blocks, 12)
        self.assertEqual(knockback, (0.2, 0.3, -0.4))

    async def test_a_blast_without_a_push_leaves_the_velocity_alone(self) -> None:
        bot, seen = self._prepare()

        await bot._handle_explode(_explode((3.0, -60.0, 4.0)))

        self.assertEqual(bot.physics_state.velocity, Vec3(0.1, 0.0, 0.05))
        self.assertEqual(bot._motion_epoch, 0)
        self.assertEqual(seen[0][3], None)

    async def test_the_sound_and_particle_tail_is_not_decoded(self) -> None:
        bot, _ = self._prepare()

        # Particle id, sound id, a weighted particle list and the sound flag:
        # none of it is needed, so a reader that stops at the knockback must not
        # trip over it.
        await bot._handle_explode(
            _explode(
                (0.0, 0.0, 0.0),
                knockback=(0.0, 0.4, 0.0),
                trailing=b"\x07\x01\x02\x03\xff",
            )
        )

        self.assertAlmostEqual(bot.physics_state.velocity.y, 0.4, places=6)

    async def test_the_switch_ignores_the_push(self) -> None:
        bot, seen = self._prepare(knockback=False)

        await bot._handle_explode(
            _explode((3.0, -60.0, 4.0), knockback=(0.2, 0.3, -0.4))
        )

        self.assertEqual(bot.physics_state.velocity, Vec3(0.1, 0.0, 0.05))
        self.assertEqual(bot._motion_epoch, 0)
        self.assertEqual(seen[0][3], (0.2, 0.3, -0.4))


if __name__ == "__main__":
    unittest.main()
