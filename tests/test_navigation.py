from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from protobot.client import Bot
from protobot.navigation import (
    FlightPathfinder,
    NavigationPath,
    Pathfinder,
    PathWaypoint,
    _FlightNode,
)
from protobot.navigator import NavigatorMixin
from protobot.physics import PhysicsEngine, StaticCollisionWorld, Vec3


class FlightPathQualityTests(unittest.TestCase):
    def test_open_air_route_is_one_continuous_flight_node(self) -> None:
        planner = FlightPathfinder(StaticCollisionWorld())
        target = Vec3(80.5, 82.0, -30.5)

        path = planner.find_path(Vec3(0.5, 70.0, 0.5), target)

        self.assertEqual(path.operations, ("fly",))
        self.assertEqual(path.waypoints[0].position, target)
        self.assertEqual(path.explored_nodes, 0)

    def test_route_cost_prefers_straight_flight(self) -> None:
        current = _FlightNode(0, 140, 0, move_x=1)

        straight = FlightPathfinder._flight_edge_cost(
            current, 1, 0, 0, vclip_edge=False
        )
        turn = FlightPathfinder._flight_edge_cost(
            current, 0, 0, 1, vclip_edge=False
        )
        reverse = FlightPathfinder._flight_edge_cost(
            current, -1, 0, 0, vclip_edge=False
        )

        self.assertLess(straight, turn)
        self.assertLess(turn, reverse)

    def test_route_cost_penalizes_vclip_entry(self) -> None:
        current = _FlightNode(0, 140, 0)

        normal = FlightPathfinder._flight_edge_cost(
            current, 0, 1, 0, vclip_edge=False
        )
        clipped = FlightPathfinder._flight_edge_cost(
            current, 0, 1, 0, vclip_edge=True
        )

        self.assertGreater(clipped, normal + 2.0)

    def test_continuous_vclip_is_merged_into_one_action(self) -> None:
        world = StaticCollisionWorld()
        for x in range(-2, 3):
            for z in range(-2, 3):
                world.add_block(x, 2, z)
        planner = FlightPathfinder(world, vclip=True, vclip_up_limit=3.0)

        path = planner.find_path(Vec3(0.5, 0.0, 0.5), Vec3(0.5, 3.0, 0.5))

        self.assertEqual(path.operations, ("vclip",))
        self.assertEqual(path.waypoints[0].position, Vec3(0.5, 3.0, 0.5))

    def test_fly_to_defaults_to_complete_quality_plan(self) -> None:
        parameters = inspect.signature(Bot.fly_to).parameters

        self.assertIs(parameters["realtime"].default, False)
        self.assertEqual(parameters["planning_horizon"].default, 32.0)
        self.assertEqual(parameters["max_nodes"].default, 65536)
        self.assertEqual(parameters["timeout"].default, 180.0)


class JumpReplayTests(unittest.TestCase):
    """A jump edge is accepted only if the integrator actually lands it."""

    def _world(self) -> StaticCollisionWorld:
        world = StaticCollisionWorld()
        world.fill((-4, -61, -4), (4, -60, 4))
        return world

    def test_a_step_up_is_replayed_and_accepted(self) -> None:
        world = self._world()
        world.add_block(0, -60, 1)  # a one-block step: its top face is -59
        planner = Pathfinder(world, physics=PhysicsEngine())

        path = planner.find_path(Vec3(0.5, -60.0, 0.5), Vec3(0.5, -59.0, 1.5))

        self.assertEqual(len(path.waypoints), 1)
        self.assertGreater(planner.jump_replays, 0)
        self.assertEqual(planner.jump_rejects, 0)

    def test_a_jump_that_cannot_reach_is_rejected(self) -> None:
        world = self._world()
        world.add_block(0, -60, 1)
        planner = Pathfinder(world, physics=PhysicsEngine())

        self.assertTrue(
            planner._jump_lands(Vec3(0.5, -60.0, 0.5), Vec3(0.5, -59.0, 1.5))
        )
        self.assertFalse(
            planner._jump_lands(Vec3(0.5, -60.0, 0.5), Vec3(0.5, -60.0, 4.5))
        )
        self.assertEqual(planner.jump_rejects, 1)

    def test_without_an_engine_no_edge_is_replayed(self) -> None:
        world = self._world()
        world.add_block(0, -60, 1)
        planner = Pathfinder(world)

        planner.find_path(Vec3(0.5, -60.0, 0.5), Vec3(0.5, -59.0, 1.5))

        self.assertEqual(planner.jump_replays, 0)


class RouteNewsTests(unittest.TestCase):
    """A block update near the remaining waypoints makes the route stale."""

    def _bot(self, *, ticks: int = 100) -> SimpleNamespace:
        return SimpleNamespace(
            _ticks_completed=ticks, _block_changes=[], _block_change_serial=0
        )

    def _path(self) -> NavigationPath:
        return NavigationPath(
            (
                PathWaypoint(Vec3(0.5, -60.0, 0.5), False),
                PathWaypoint(Vec3(0.5, -60.0, 8.5), False),
            ),
            explored_nodes=2,
            cost=8.0,
        )

    def test_a_change_under_a_remaining_node_is_news(self) -> None:
        bot = self._bot()
        NavigatorMixin.note_block_change(bot, 0, -61, 8)

        self.assertTrue(NavigatorMixin._route_blocked(bot, self._path(), 1, since_tick=0))

    def test_a_change_already_walked_past_is_not(self) -> None:
        bot = self._bot()
        NavigatorMixin.note_block_change(bot, 0, -61, 0)  # behind the second node

        self.assertFalse(
            NavigatorMixin._route_blocked(bot, self._path(), 1, since_tick=0)
        )

    def test_far_away_or_old_changes_are_ignored(self) -> None:
        bot = self._bot()
        NavigatorMixin.note_block_change(bot, 9, -61, 9)
        self.assertFalse(
            NavigatorMixin._route_blocked(bot, self._path(), 1, since_tick=0)
        )

        bot = self._bot()
        NavigatorMixin.note_block_change(bot, 0, -61, 8)
        self.assertFalse(
            NavigatorMixin._route_blocked(bot, self._path(), 1, since_tick=101)
        )

    def test_the_notebook_is_bounded(self) -> None:
        bot = self._bot()
        for index in range(200):
            NavigatorMixin.note_block_change(bot, index, -61, 0)

        self.assertLessEqual(len(bot._block_changes), 128)
        self.assertEqual(bot._block_change_serial, 200)


if __name__ == "__main__":
    unittest.main()
