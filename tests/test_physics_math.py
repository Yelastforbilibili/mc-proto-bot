"""Table trigonometry, and the exact-zero snap on the cardinal headings.

Vanilla's sine table is sampled with ``Math.sin``, so ``cos(0)`` comes back as
``1.2246e-16`` instead of zero.  Movement code multiplies that by the walking
acceleration every tick, which is how a bot walking straight along an axis ends
up with a sideways component the server never predicted.
"""

from __future__ import annotations

import math
import unittest

from protobot.physics.engine import MovementInput, PhysicsEngine, PhysicsState
from protobot.physics.geometry import AABB
from protobot.physics.math import (
    f32,
    heading_sin_cos,
    yaw_sin_cos,
)
from protobot.physics.world import BlockProperties

_RADIANS_PER_DEGREE = f32(f32(math.pi) / 180.0)


class TableTrigTests(unittest.TestCase):
    def test_the_table_keeps_its_tiny_residue(self) -> None:
        # pi/2 samples to exactly 1.0 in the table (same as Java), but pi does
        # not: the residue lands on the other axis, which is the tick-by-tick
        # source of sideways drift on a straight walk.
        self.assertEqual(yaw_sin_cos(0.0), (0.0, 1.0))
        self.assertEqual(yaw_sin_cos(90.0)[0], 1.0)
        self.assertNotEqual(yaw_sin_cos(90.0)[1], 0.0)
        self.assertLess(abs(yaw_sin_cos(90.0)[1]), 1e-15)
        self.assertNotEqual(yaw_sin_cos(180.0)[0], 0.0)
        self.assertLess(abs(yaw_sin_cos(180.0)[0]), 1e-15)

    def test_cardinal_headings_snap_to_exact_axes(self) -> None:
        expected = {
            0.0: (0.0, 1.0),
            90.0: (1.0, 0.0),
            180.0: (0.0, -1.0),
            270.0: (-1.0, 0.0),
            -90.0: (-1.0, 0.0),
            -180.0: (0.0, -1.0),
            360.0: (0.0, 1.0),
        }
        for yaw, (sin_expected, cos_expected) in expected.items():
            with self.subTest(yaw=yaw):
                self.assertEqual(heading_sin_cos(yaw), (sin_expected, cos_expected))

    def test_every_other_heading_is_left_alone(self) -> None:
        for yaw in (45.0, 12.5, 123.456, -33.75, 359.9):
            with self.subTest(yaw=yaw):
                self.assertEqual(heading_sin_cos(yaw), yaw_sin_cos(yaw))

    def test_the_snap_only_touches_quarter_turns(self) -> None:
        # One float ulp off a cardinal heading is not a cardinal heading.
        self.assertEqual(heading_sin_cos(90.000015)[1], yaw_sin_cos(90.000015)[1])
        self.assertEqual(heading_sin_cos(0.000015)[0], yaw_sin_cos(0.000015)[0])


class _AirWorld:
    """An empty world: the player falls, but nothing pushes it sideways."""

    def collision_boxes(self, query: AABB) -> list[AABB]:
        return []

    def block_collision_boxes(self, x: int, y: int, z: int) -> tuple[AABB, ...]:
        return ()

    def block_properties(self, x: int, y: int, z: int) -> BlockProperties:
        return BlockProperties()

    def no_collision(self, query: AABB) -> bool:
        return True

    def entity_collision_boxes(self, query: AABB) -> list[AABB]:
        return []


class CardinalWalkTests(unittest.TestCase):
    def test_a_walk_along_an_axis_gains_no_sideways_velocity(self) -> None:
        engine = PhysicsEngine()
        state = PhysicsState()
        state.yaw = 90.0  # facing -x
        world = _AirWorld()

        state = engine.tick(state, MovementInput(forward=1.0), world)

        self.assertGreater(abs(state.velocity.x), 0.0)
        self.assertEqual(state.velocity.z, 0.0)

    def test_the_same_walk_at_an_angle_does_move_both_axes(self) -> None:
        engine = PhysicsEngine()
        state = PhysicsState()
        state.yaw = 45.0
        world = _AirWorld()

        state = engine.tick(state, MovementInput(forward=1.0), world)

        self.assertGreater(abs(state.velocity.x), 0.0)
        self.assertGreater(abs(state.velocity.z), 0.0)


if __name__ == "__main__":
    unittest.main()
