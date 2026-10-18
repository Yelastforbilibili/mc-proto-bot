"""Which block is under the crosshair, and through which face.

The physics package already ships the collision primitives an interaction
needs (``AABB``, ``clip_axis``); what it does not have is the *other* half of
every vanilla interaction: the block ray cast.  A server validates the target
block, the face and the distance, so a client that guesses cannot dig or place
reliably -- it just gets silently ignored.

The traversal is the standard Amanatides & Woo voxel walk, which visits every
block the ray passes through in order, so the first solid voxel found is the
nearest one.  Blocks are treated as their unit cube.  That is exact for every
full block and an approximation for stairs, slabs and fences, which is the
same trade-off the collision engine makes.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

from ..physics import Vec3

#: Vanilla ``Direction`` ids, in the order the protocol sends them.
_DIRECTION_STEPS = (
    (0, -1, 0),  # DOWN
    (0, 1, 0),  # UP
    (0, 0, -1),  # NORTH
    (0, 0, 1),  # SOUTH
    (-1, 0, 0),  # WEST
    (1, 0, 0),  # EAST
)

_OPPOSITE = (1, 0, 3, 2, 5, 4)


class BlockFace(IntEnum):
    """A block face, using vanilla's ``Direction`` ordinals.

    The id is what goes on the wire: ``PlayerAction`` and ``UseItemOn`` both
    carry the face of the block that was hit, not the direction the player
    looks.
    """

    DOWN = 0
    UP = 1
    NORTH = 2
    SOUTH = 3
    WEST = 4
    EAST = 5

    @property
    def step(self) -> tuple[int, int, int]:
        """Unit vector pointing out of the face."""

        return _DIRECTION_STEPS[self.value]

    @property
    def opposite(self) -> "BlockFace":
        return BlockFace(_OPPOSITE[self.value])

    @property
    def axis(self) -> str:
        return "y" if self.value < 2 else ("z" if self.value < 4 else "x")

    @classmethod
    def from_step(cls, dx: int, dy: int, dz: int) -> "BlockFace":
        """The face whose outward normal is ``(dx, dy, dz)``."""

        try:
            return cls(_DIRECTION_STEPS.index((dx, dy, dz)))
        except ValueError:
            raise ValueError(f"not an axis direction: {(dx, dy, dz)!r}") from None


@dataclass(frozen=True)
class BlockHit:
    """A ray cast result: the block, the face it was entered through, the point."""

    position: tuple[int, int, int]
    face: BlockFace
    point: Vec3
    distance: float
    state_id: int

    @property
    def face_point(self) -> Vec3:
        """The hit point relative to the block, clamped into ``[0, 1]``."""

        x, y, z = self.position
        return Vec3(
            min(max(self.point.x - x, 0.0), 1.0),
            min(max(self.point.y - y, 0.0), 1.0),
            min(max(self.point.z - z, 0.0), 1.0),
        )

    def center(self) -> Vec3:
        x, y, z = self.position
        return Vec3(x + 0.5, y + 0.5, z + 0.5)

    def face_center(self) -> Vec3:
        """Centre of the hit face -- the safest point to aim at."""

        x, y, z = self.position
        dx, dy, dz = self.face.step
        return Vec3(
            x + 0.5 + dx * 0.5,
            y + 0.5 + dy * 0.5,
            z + 0.5 + dz * 0.5,
        )


def look_angles(origin: Vec3, target: Vec3) -> tuple[float, float]:
    """Return ``(yaw, pitch)`` in degrees that make the player look at ``target``.

    Matches the convention the physics engine and the navigation code use:
    yaw 0 looks towards +Z, pitch is positive downwards.
    """

    dx = target.x - origin.x
    dy = target.y - origin.y
    dz = target.z - origin.z
    horizontal = math.hypot(dx, dz)
    yaw = math.degrees(math.atan2(-dx, dz))
    pitch = math.degrees(-math.atan2(dy, horizontal))
    return yaw, pitch


def direction_from_angles(yaw: float, pitch: float) -> Vec3:
    """Unit look vector for a yaw/pitch pair, inverse of :func:`look_angles`."""

    yaw_radians = math.radians(yaw)
    pitch_radians = math.radians(pitch)
    cos_pitch = math.cos(pitch_radians)
    return Vec3(
        -math.sin(yaw_radians) * cos_pitch,
        -math.sin(pitch_radians),
        math.cos(yaw_radians) * cos_pitch,
    )


def _ray_box(
    origin: Vec3, direction: Vec3, x: int, y: int, z: int
) -> tuple[float, BlockFace] | None:
    """Distance to the ray/unit-cube intersection, and the face it entered by.

    The limiting axis of the slab test *is* the entered face, so no separate
    face bookkeeping is needed -- but which side was entered depends on the
    sign of the direction: a downward ray crosses the *high* boundary of the
    voxel first, and therefore hits its top face.
    """

    t_min = 0.0
    t_max = math.inf
    face = BlockFace.UP
    for index, coordinate in enumerate((origin.x, origin.y, origin.z)):
        low = (x, y, z)[index]
        delta = (direction.x, direction.y, direction.z)[index]
        if delta == 0.0:
            if coordinate < low or coordinate >= low + 1.0:
                return None
            continue
        t_low = (low - coordinate) / delta
        t_high = (low + 1.0 - coordinate) / delta
        if t_low < t_high:
            near, near_face = t_low, (BlockFace.WEST, BlockFace.DOWN, BlockFace.NORTH)[
                index
            ]
            far = t_high
        else:
            near, near_face = t_high, (BlockFace.EAST, BlockFace.UP, BlockFace.SOUTH)[
                index
            ]
            far = t_low
        if near > t_min:
            t_min = near
            face = near_face
        if far < t_max:
            t_max = far
        if t_min > t_max:
            return None
    return t_min, face


def raycast_blocks(
    world,
    origin: Vec3,
    direction: Vec3,
    *,
    max_distance: float = 4.5,
    is_solid: Callable[[int], bool] | None = None,
) -> BlockHit | None:
    """Walk the ray from ``origin`` and return the first solid block.

    ``is_solid`` receives a block state id and decides whether the ray stops
    there; by default every state except air (``0``) stops it, which is what
    "which block am I looking at" means.  ``max_distance`` defaults to the
    vanilla survival reach (4.5 blocks).
    """

    if is_solid is None:

        def is_solid(state_id: int) -> bool:
            return state_id != 0

    length = math.sqrt(direction.length_squared)
    if length == 0.0:
        return None
    direction = Vec3(direction.x / length, direction.y / length, direction.z / length)
    coordinates = (origin.x, origin.y, origin.z)
    deltas = (direction.x, direction.y, direction.z)

    voxel = [math.floor(axis) for axis in coordinates]
    steps = [0, 0, 0]
    t_max = [math.inf, math.inf, math.inf]
    t_delta = [math.inf, math.inf, math.inf]
    for index, delta in enumerate(deltas):
        if delta > 0:
            steps[index] = 1
            t_max[index] = (voxel[index] + 1 - coordinates[index]) / delta
            t_delta[index] = 1.0 / delta
        elif delta < 0:
            steps[index] = -1
            t_max[index] = (voxel[index] - coordinates[index]) / delta
            t_delta[index] = -1.0 / delta

    first_state = world.get_state_id(*voxel)
    if is_solid(first_state):
        # Standing inside a block: report the origin voxel rather than walking
        # out of it, which is what vanilla's pick does.
        return BlockHit(tuple(voxel), BlockFace.UP, origin, 0.0, first_state)

    while True:
        axis = min(range(3), key=lambda index: t_max[index])
        distance = t_max[axis]
        if distance > max_distance:
            return None
        voxel[axis] += steps[axis]
        t_max[axis] += t_delta[axis]
        position = (voxel[0], voxel[1], voxel[2])
        state_id = world.get_state_id(*position)
        if not is_solid(state_id):
            continue
        hit = _ray_box(origin, direction, *position)
        if hit is None:
            return None
        travelled, face = hit
        point = Vec3(
            origin.x + direction.x * travelled,
            origin.y + direction.y * travelled,
            origin.z + direction.z * travelled,
        )
        return BlockHit(position, face, point, travelled, state_id)
