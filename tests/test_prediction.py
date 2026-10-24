"""Where an entity will be, and where to aim to hit it.

The motion expectations are the numbers a vanilla client produces for a
knockback arc, measured against a real client on Paper 26.3
(``artifacts/kb_vanilla.py``): 0.4, 0.2184, 0.1987, 0.1809 horizontally and
0.3608, 0.2752, 0.1913 vertically.  If those stop matching, the predictor is
wrong in a way that makes every aimed action miss.
"""

from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

from protobot.events import EventBus
from protobot.physics import Vec3
from protobot.prediction import (
    ARROW,
    PHYSICS_MOTION,
    PREDICTION_HORIZON_TICKS,
    EntityPredictor,
    PredictedState,
    arc,
    flight_path,
    solve_launch,
)
from protobot.state import EntityState


def _bot(position: Vec3 | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        events=EventBus(),
        entities={},
        _ticks_completed=0,
        physics_state=SimpleNamespace(position=position or Vec3(0.5, -60.0, 0.5)),
    )


def _entity(
    entity_id: int,
    position: tuple[float, float, float],
    *,
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0),
    on_ground: bool = True,
    height: float = 1.8,
) -> EntityState:
    return EntityState(
        entity_id=entity_id,
        x=position[0],
        y=position[1],
        z=position[2],
        velocity_x=velocity[0],
        velocity_y=velocity[1],
        velocity_z=velocity[2],
        on_ground=on_ground,
        collision_width=0.6,
        collision_height=height,
    )


def _knockback_start(
    position: Vec3 = Vec3(0.0, 0.0, 0.0),
) -> PredictedState:
    return PredictedState(
        ticks=0,
        position=position,
        velocity=Vec3(0.0, 0.3608, 0.4),
        on_ground=True,
    )


class MotionModelTests(unittest.TestCase):
    def test_step_reproduces_the_vanilla_knockback_arc(self) -> None:
        states = arc(_knockback_start(), 5, model=PHYSICS_MOTION)
        forward = [states[index + 1].position.z - states[index].position.z for index in range(5)]
        upward = [states[index + 1].position.y - states[index].position.y for index in range(5)]

        for measured, expected in zip(
            forward, [0.4, 0.2184, 0.1987, 0.1809, 0.1646], strict=True
        ):
            self.assertAlmostEqual(measured, expected, places=3)
        for measured, expected in zip(
            upward, [0.3608, 0.2752, 0.1913, 0.1091, 0.0285], strict=True
        ):
            self.assertAlmostEqual(measured, expected, places=3)

    def test_the_first_tick_uses_ground_friction_then_air_drag(self) -> None:
        states = arc(_knockback_start(), 2, model=PHYSICS_MOTION)

        self.assertTrue(states[0].on_ground)
        self.assertFalse(states[1].on_ground)
        # Ground friction keeps 0.546 of the horizontal speed, air drag 0.91.
        self.assertAlmostEqual(states[1].velocity.z, 0.4 * 0.6 * 0.91, places=4)
        self.assertAlmostEqual(
            states[2].velocity.z, states[1].velocity.z * 0.91, places=4
        )

    def test_a_world_decides_whether_it_stands(self) -> None:
        class World:
            def __init__(self, standing: bool) -> None:
                self.standing = standing

            def has_ground_under(self, position: Vec3, *, width: float) -> bool:
                return self.standing

        falling = arc(
            PredictedState(
                ticks=0, position=Vec3(0.0, 10.0, 0.0), velocity=Vec3(0, 0, 0), on_ground=True
            ),
            2,
            world=World(False),
        )
        self.assertFalse(falling[1].on_ground)
        self.assertLess(falling[2].position.y, falling[1].position.y)

        resting = arc(
            PredictedState(
                ticks=0, position=Vec3(0.0, 10.0, 0.0), velocity=Vec3(0, 0, 0), on_ground=False
            ),
            1,
            world=World(True),
        )
        self.assertTrue(resting[1].on_ground)
        self.assertAlmostEqual(resting[1].velocity.y, 0.0)

    def test_the_walking_model_holds_its_speed_and_the_physics_model_does_not(
        self,
    ) -> None:
        walking = arc(_knockback_start(), 2)
        pushed = arc(_knockback_start(), 2, model=PHYSICS_MOTION)

        self.assertAlmostEqual(walking[2].position.z, 0.8, places=6)
        self.assertLess(pushed[2].position.z, walking[2].position.z - 0.1)

    def test_arc_starts_with_the_state_it_was_given(self) -> None:
        start = _knockback_start()
        self.assertEqual(arc(start, 0), [start])
        self.assertEqual(len(arc(start, 3)), 4)


class VelocityTests(unittest.TestCase):
    def test_velocity_comes_from_position_deltas(self) -> None:
        bot = _bot()
        predictor = EntityPredictor(bot)
        bot._ticks_completed = 10
        predictor.observe(7, _entity(7, (0.0, -60.0, 0.0)))
        bot._ticks_completed = 11
        predictor.observe(7, _entity(7, (0.0, -60.0, 0.2)))

        velocity = predictor.velocity_of(7)
        self.assertAlmostEqual(velocity.z, 0.2, places=6)
        self.assertAlmostEqual(velocity.x, 0.0, places=6)

    def test_a_motion_packet_replaces_the_measured_velocity(self) -> None:
        bot = _bot()
        predictor = EntityPredictor(bot)
        bot._ticks_completed = 4
        predictor.observe(3, _entity(3, (0.0, -60.0, 0.0)))
        bot._ticks_completed = 5
        predictor.observe(3, _entity(3, (0.0, -60.0, 0.1)))

        predictor._on_motion(3, (0.0, 0.3608, 0.4))
        velocity = predictor.velocity_of(3)
        self.assertAlmostEqual(velocity.z, 0.4, places=6)
        self.assertAlmostEqual(velocity.y, 0.3608, places=6)

    def test_an_entity_that_never_moved_has_no_velocity(self) -> None:
        bot = _bot()
        predictor = EntityPredictor(bot)
        predictor.observe(5, _entity(5, (2.0, -60.0, 2.0)))
        self.assertAlmostEqual(predictor.velocity_of(5).length_squared, 0.0)


class LookaheadTests(unittest.TestCase):
    def test_position_at_walks_the_predicted_ticks(self) -> None:
        bot = _bot()
        predictor = EntityPredictor(bot)
        predictor.observe(9, _entity(9, (0.0, -60.0, 0.0), velocity=(0.0, 0.0, 0.5)))

        one = predictor.position_at(9, 1)
        two = predictor.position_at(9, 2)
        assert one is not None and two is not None
        self.assertAlmostEqual(one.z, 0.5, places=6)
        # Second tick: 0.5 more of velocity, then the ground friction bites.
        self.assertAlmostEqual(two.z, 0.5 + 0.5, places=6)
        self.assertGreater(predictor.position_at(9, 3).z, two.z)

    def test_the_horizon_is_capped(self) -> None:
        bot = _bot()
        predictor = EntityPredictor(bot)
        predictor.observe(9, _entity(9, (0.0, -60.0, 0.0), velocity=(0.0, 0.0, 0.5)))

        far = predictor.position_at(9, PREDICTION_HORIZON_TICKS + 50)
        edge = predictor.position_at(9, PREDICTION_HORIZON_TICKS)
        assert far is not None and edge is not None
        self.assertAlmostEqual(far.z, edge.z, places=9)

    def test_confidence_decays_and_expires(self) -> None:
        bot = _bot()
        predictor = EntityPredictor(bot)
        predictor.observe(9, _entity(9, (0.0, -60.0, 0.0)))

        fresh = predictor.confidence(9, 0)
        ahead = predictor.confidence(9, PREDICTION_HORIZON_TICKS // 2)
        expired = predictor.confidence(9, PREDICTION_HORIZON_TICKS)
        self.assertAlmostEqual(fresh, 1.0, places=6)
        self.assertLess(ahead, fresh)
        self.assertLessEqual(expired, 1.0e-2)

    def test_a_stale_track_loses_confidence(self) -> None:
        bot = _bot()
        predictor = EntityPredictor(bot)
        predictor.observe(9, _entity(9, (0.0, -60.0, 0.0)))
        bot._ticks_completed += 2 * PREDICTION_HORIZON_TICKS
        self.assertLessEqual(predictor.confidence(9, 0), 0.01)

    def test_center_is_the_middle_of_the_box(self) -> None:
        bot = _bot()
        predictor = EntityPredictor(bot)
        predictor.observe(11, _entity(11, (1.0, -60.0, 1.0), height=1.8))
        center = predictor.center_at(11, 0)
        assert center is not None
        self.assertAlmostEqual(center.y, -60.0 + 0.9, places=6)

    def test_unknown_and_forgotten_entities(self) -> None:
        bot = _bot()
        predictor = EntityPredictor(bot)
        self.assertIsNone(predictor.position_at(404, 1))
        self.assertEqual(predictor.arc(404, 3), [])
        self.assertAlmostEqual(predictor.confidence(404, 0), 0.0)

        predictor.observe(1, _entity(1, (0.0, -60.0, 0.0)))
        predictor.forget(1)
        self.assertIsNone(predictor.position_at(1, 1))

        predictor.observe(2, _entity(2, (0.0, -60.0, 0.0)))
        predictor._on_remove([2])
        self.assertIsNone(predictor.position_at(2, 1))

    def test_observe_without_a_position_reads_the_entity_table(self) -> None:
        bot = _bot()
        bot.entities[4] = _entity(4, (3.0, -60.0, 3.0))
        predictor = EntityPredictor(bot)
        predictor.observe(4)
        position = predictor.position_at(4, 0)
        assert position is not None
        self.assertAlmostEqual(position.x, 3.0)

    def test_eye_position_is_the_feet_plus_eye_height(self) -> None:
        bot = _bot(Vec3(1.0, -60.0, 2.0))
        predictor = EntityPredictor(bot)
        eye = predictor.eye_position()
        self.assertAlmostEqual(eye.y, -60.0 + 1.62, places=6)


class BallisticsTests(unittest.TestCase):
    def test_a_stationary_target_is_hit_at_its_range(self) -> None:
        origin = Vec3(0.5, -60.0 + 1.62, 0.5)
        target = Vec3(20.5, -60.0 + 0.9, 0.5)
        launch = solve_launch(origin, target)
        assert launch is not None

        path = flight_path(origin, launch.yaw, launch.pitch, max_ticks=launch.ticks)
        landing = path[-1]
        self.assertLess(math.dist((landing.x, landing.y, landing.z), (target.x, target.y, target.z)), 0.6)

    def test_a_distant_target_needs_a_higher_arc(self) -> None:
        origin = Vec3(0.5, -59.0, 0.5)
        near = solve_launch(origin, Vec3(0.5, -59.0, 6.5))
        far = solve_launch(origin, Vec3(0.5, -59.0, 30.5))
        assert near is not None and far is not None
        # Pitch is positive downwards, so a longer shot needs a higher arc.
        self.assertLess(far.pitch, near.pitch)

    def test_a_target_out_of_range_has_no_solution(self) -> None:
        origin = Vec3(0.5, -59.0, 0.5)
        self.assertIsNone(solve_launch(origin, Vec3(0.5, -59.0, 400.5)))

    def test_directly_below_is_a_straight_shot(self) -> None:
        launch = solve_launch(Vec3(0.5, -58.0, 0.5), Vec3(0.5, -60.5, 0.5))
        assert launch is not None
        self.assertGreater(launch.pitch, 45.0)

    def test_intercept_leads_a_moving_target(self) -> None:
        bot = _bot(Vec3(0.5, -60.0, 0.5))
        predictor = EntityPredictor(bot)
        predictor.observe(
            12, _entity(12, (0.5, -60.0, 10.5), velocity=(0.0, 0.0, 0.3))
        )

        aim = predictor.intercept(12, model=ARROW)
        assert aim is not None
        # The shot is aimed where the cow will be, not where it is.
        self.assertGreater(aim.point.z, 10.5)
        self.assertGreater(aim.launch.ticks, 1)
        self.assertGreater(aim.confidence, 0.0)

    def test_intercept_without_a_track_is_none(self) -> None:
        predictor = EntityPredictor(_bot())
        self.assertIsNone(predictor.intercept(999))

    def test_the_arc_carries_the_model_it_was_solved_with(self) -> None:
        origin = Vec3(0.5, -59.0, 0.5)
        slow = solve_launch(origin, Vec3(0.5, -59.0, 12.5), model=ARROW.__class__(speed=1.0))
        fast = solve_launch(origin, Vec3(0.5, -59.0, 12.5))
        assert slow is not None and fast is not None
        self.assertGreater(slow.ticks, fast.ticks)


if __name__ == "__main__":
    unittest.main()
