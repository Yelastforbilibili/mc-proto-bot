"""A tick as a value: compute without mutating, record, replay.

The bot writes its physics state in place 20 times a second, which is the cheap
thing to do.  Everything that wants to ask "what would happen if" -- a planner
testing a jump edge, a probe comparing itself with the server, a regression test
pinning an arc -- needs the same arithmetic without the side effect, and a way to
re-run a transcript afterwards.
"""

from __future__ import annotations

import unittest

from protobot.physics import (
    AABB,
    BlockProperties,
    Frame,
    MovementInput,
    PhysicsAttributes,
    PhysicsEngine,
    PhysicsState,
    Recorder,
)
from protobot.physics.geometry import AABB as AABBType


class _AirWorld:
    """Nothing to collide with; the player falls through empty space."""

    def collision_boxes(self, query: AABBType) -> list[AABBType]:
        return []

    def block_collision_boxes(self, x: int, y: int, z: int) -> tuple[AABBType, ...]:
        return ()

    def block_properties(self, x: int, y: int, z: int) -> BlockProperties:
        return BlockProperties()

    def no_collision(self, query: AABBType) -> bool:
        return True

    def entity_collision_boxes(self, query: AABBType) -> list[AABBType]:
        return []


class _FloorWorld(_AirWorld):
    """A single full block under the feet, so the walk is on solid ground."""

    def __init__(self, top: float = 0.0) -> None:
        self.top = top

    def block_collision_boxes(self, x: int, y: int, z: int) -> tuple[AABBType, ...]:
        if y == int(self.top) - 1:
            return (
                AABB(float(x), self.top - 1.0, float(z), x + 1.0, self.top, z + 1.0),
            )
        return ()

    def collision_boxes(self, query: AABBType) -> list[AABBType]:
        return [
            box
            for box in self.block_collision_boxes(
                int(query.min_x), int(query.min_y), int(query.min_z)
            )
            if box.intersects(query)
        ]

    def no_collision(self, query: AABBType) -> bool:
        return not self.collision_boxes(query)


class StepTests(unittest.TestCase):
    def test_step_does_not_touch_the_state_it_was_given(self) -> None:
        engine = PhysicsEngine()
        state = PhysicsState()
        state.yaw = 0.0
        before = (state.position, state.velocity, state.on_ground)

        result = engine.step(state, MovementInput(forward=1.0), _AirWorld())

        self.assertEqual((state.position, state.velocity, state.on_ground), before)
        self.assertNotEqual(result.state.position, state.position)

    def test_step_matches_tick(self) -> None:
        engine = PhysicsEngine()
        walked = PhysicsState()
        walked.yaw = 30.0
        stepped = walked.copy()
        controls = MovementInput(forward=1.0, sprint=True)
        engine.tick(walked, controls, _FloorWorld())

        result = engine.step(stepped, controls, _FloorWorld())

        self.assertAlmostEqual(result.state.position.x, walked.position.x, places=9)
        self.assertAlmostEqual(result.state.position.z, walked.position.z, places=9)
        self.assertEqual(result.collided, walked.horizontal_collision)

    def test_the_copy_carries_its_mutable_containers(self) -> None:
        state = PhysicsState()
        state.attribute_values["movement_speed"] = 0.1

        clone = state.copy()
        clone.attribute_values["movement_speed"] = 0.2

        self.assertEqual(state.attribute_values["movement_speed"], 0.1)


class ReplayTests(unittest.TestCase):
    def _transcript(self) -> Recorder:
        engine = PhysicsEngine()
        world = _FloorWorld()
        recorder = Recorder()
        state = PhysicsState()
        for tick in range(6):
            controls = MovementInput(forward=1.0, jump=tick == 2)
            result = recorder.record(engine, state, controls, world)
            state = result.state
        return recorder

    def test_a_replay_of_a_recorded_run_is_exact(self) -> None:
        recorder = self._transcript()
        self.assertEqual(len(recorder.frames), 6)
        self.assertEqual(recorder.replay(PhysicsEngine(), _FloorWorld()), [])

    def test_a_replay_says_where_it_diverges(self) -> None:
        recorder = self._transcript()
        # A different gravity changes the vertical integrator on every frame.
        heavier = PhysicsEngine(PhysicsAttributes(gravity=0.03))

        problems = recorder.replay(heavier, _FloorWorld())

        self.assertEqual(len(problems), 6)
        self.assertIn("frame 0: expected", problems[0])

    def test_frames_keep_their_own_values(self) -> None:
        recorder = self._transcript()
        first = recorder.frames[0]
        self.assertIsInstance(first, Frame)
        self.assertNotEqual(first.before.position, first.after.position)


if __name__ == "__main__":
    unittest.main()
