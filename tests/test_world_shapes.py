"""World-level tests: exact collision shapes, the clock table and ground probing."""

from __future__ import annotations

import gzip
import json
import unittest
from pathlib import Path

from protobot.physics.geometry import Vec3
from protobot.world import DAY_LENGTH, ClockState, World

DATA = Path(__file__).resolve().parent.parent / "protobot" / "data"


class ShapeTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(protocol=777)

    def test_bed_is_not_a_full_cube(self) -> None:
        definition = self.world.block_states.get(2261)
        self.assertEqual(definition.name, "minecraft:red_bed")
        self.assertGreater(len(definition.shapes), 1)
        self.assertAlmostEqual(max(box.max_y for box in definition.shapes), 0.5625)

    def test_slab_is_half_a_block(self) -> None:
        definition = self.world.block_states.get(3732)
        self.assertEqual(definition.name, "minecraft:yellow_wool_slab")
        self.assertAlmostEqual(definition.shapes[0].max_y, 0.5)

    def test_air_has_no_shape(self) -> None:
        self.assertEqual(self.world.block_states.get(0).shapes, ())

    def test_shape_overlay_skips_unknown_states(self) -> None:
        applied = self.world.block_states.load_shapes(
            {
                "boxes": [[0.0, 0.0, 0.0, 1.0, 0.25, 1.0]],
                "states": {"1": [0], "999999": [0]},
            }
        )
        self.assertEqual(applied, 1)
        self.assertAlmostEqual(self.world.block_states.get(1).shapes[0].max_y, 0.25)

    def test_bundled_table_covers_the_dump(self) -> None:
        table = DATA / "shapes-26.3.json.gz"
        self.assertTrue(table.exists())
        with gzip.open(table, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        self.assertEqual(payload["release"], "26.3")
        self.assertGreater(len(payload["states"]), 30_000)
        self.assertGreater(len(payload["boxes"]), 100)


class GroundProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(protocol=777)
        self.state_ids: dict[tuple[int, int, int], int] = {}

    def _stub_state(self) -> None:
        world = self.world

        def get_state_id(x: int, y: int, z: int) -> int:
            return self.state_ids.get((x, y, z), 0)

        world.get_state_id = get_state_id  # type: ignore[method-assign]

    def test_feet_on_the_bed_top_count_as_ground(self) -> None:
        self.state_ids[(0, 0, 0)] = 2261
        self._stub_state()
        self.assertTrue(self.world.has_ground_under(Vec3(0.5, 0.5625, 0.5)))
        # More than the 1 mm tolerance away from the top face is a gap.
        self.assertFalse(self.world.has_ground_under(Vec3(0.5, 0.58, 0.5)))

    def test_feet_hanging_above_air_are_not_grounded(self) -> None:
        self.state_ids[(0, 0, 0)] = 2261
        self._stub_state()
        # 0.125 above the bed top is exactly the sleeping-pose gap.
        self.assertFalse(self.world.has_ground_under(Vec3(0.5, 0.6875, 0.5)))

    def test_a_full_block_counts_and_a_neighbour_does_not(self) -> None:
        self.state_ids[(0, 0, 0)] = 1
        self._stub_state()
        self.assertTrue(self.world.has_ground_under(Vec3(0.5, 1.0, 0.5)))
        self.assertFalse(self.world.has_ground_under(Vec3(2.5, 1.0, 0.5)))


class ClockTests(unittest.TestCase):
    def test_updates_carry_the_overworld_clock_into_day_time(self) -> None:
        world = World(protocol=777)
        world.apply_clock_updates(901_000, {0: ClockState(total_ticks=13_000)})
        self.assertEqual(world.game_time, 901_000)
        self.assertEqual(world.day_time, 13_000)
        self.assertTrue(world.day_time_advancing)

    def test_day_time_wraps_at_the_day_length(self) -> None:
        world = World(protocol=777)
        world.apply_clock_updates(5, {0: ClockState(total_ticks=DAY_LENGTH * 3 + 500)})
        self.assertEqual(world.day_time, 500)

    def test_a_paused_clock_stops_advancing(self) -> None:
        world = World(protocol=777)
        world.apply_clock_updates(5, {0: ClockState(total_ticks=100, rate=0.0)})
        self.assertFalse(world.day_time_advancing)

    def test_an_empty_update_keeps_the_known_state(self) -> None:
        world = World(protocol=777)
        world.apply_clock_updates(10, {0: ClockState(total_ticks=13_000)})
        world.apply_clock_updates(11, {})
        self.assertEqual(world.day_time, 13_000)
        self.assertEqual(world.game_time, 11)


if __name__ == "__main__":
    unittest.main()
