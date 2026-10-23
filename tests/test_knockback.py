"""Regression tests for the knockback (server velocity) path.

A vanilla client applies an impulse between two ticks and then moves, so the
very next movement packet already carries it.  Our reader task and tick loop
run concurrently, so an impulse can land *after* a tick has run its physics and
*before* that tick's packet goes out.  Claiming the pre-impulse position there
looks like "did not take the knockback" to a movement prediction check; these
tests pin down the recompute that avoids it.
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
