"""Where an entity will be in a few ticks, and where to aim to hit it.

The server only ever tells us where something *was*: ``entity_position_sync``
carries an absolute position, ``move_entity`` the small deltas in between, and
``set_entity_motion`` a velocity when something pushed it.  Between two of
those packets the client has to guess, and every aimed action -- attacking,
interacting, and a bow shot later -- needs the guess to be a measured one
rather than "wherever the last packet left it".

Three pieces live here:

``MotionModel`` / ``step`` / ``arc``
    The pure integrator: one tick of an entity that receives no input
    (horizontal friction, vertical drag, gravity).  It reproduces the arc the
    vanilla client produces for a knockback, which is the only entity motion
    we can check against a real client.

``EntityPredictor``
    The bookkeeping: listen to ``entity_move`` and ``entity_motion``, derive a
    velocity per tick, and answer "where will entity N be in k ticks".

``ProjectileModel`` / ``solve_launch`` / ``EntityPredictor.intercept``
    The ballistics: an arrow drops and slows, so aiming at where a target is
    now always misses a target that moves.  ``intercept`` iterates "fly for t
    ticks, re-aim at where the target will be after t ticks" until the flight
    time stops changing.

What this model cannot know (and does not pretend to)
-----------------------------------------------------

* **The entity's own input.**  A mob's pathfinding, a player's keys and jumps
  are in no packet.  We extrapolate the velocity we measured, so a target that
  starts turning only turns once the next update arrives.
* **Updates are sparse.**  Vanilla sends most entity movement as 2 Hz deltas
  for interpolation; the ticks in between are extrapolated, and a knockback or
  a setback that has not arrived yet cannot be predicted at all.
* **Collisions.**  Predicted positions ignore walls and fences: a mob that
  walks into a wall keeps going in our model until the server corrects it.
* **Vehicles.**  Only the root entity is extrapolated.  A passenger moves with
  its vehicle and its own track is meaningless while riding.
* **Latency.**  What we hold is already about ``ping / 2`` old, and a launch
  rotation takes effect when the server reads the packet.  A shot that flies
  for ten ticks can still miss a target that changed its mind.
* **Charge.**  Arrow speed depends on how far the bow was drawn; the caller
  passes the speed.  This module does not guess it.

The horizon is therefore capped (``PREDICTION_HORIZON_TICKS``) and every answer
carries a ``confidence`` that decays with the lookahead, so a caller can decide
to walk closer instead of shooting into the dark.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .physics import Vec3

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Bot
    from .state import EntityState
    from .world import World

__all__ = [
    "AIR_DRAG",
    "ARROW",
    "DEFAULT_MOTION",
    "PHYSICS_MOTION",
    "EntityPredictor",
    "EYE_HEIGHT",
    "GRAVITY",
    "GROUND_FRICTION",
    "Launch",
    "MotionModel",
    "PREDICTION_HORIZON_TICKS",
    "PredictedState",
    "ProjectileModel",
    "Track",
    "VERTICAL_DRAG",
    "arc",
    "direction_from_angles",
    "look_angles",
    "solve_launch",
    "step",
]

#: Default block slipperiness (``BlockBehaviour.getFriction``) times vanilla's
#: air drag: what a walking mob keeps of its speed each tick on the ground.
GROUND_FRICTION = 0.6 * 0.91
#: Horizontal drag of an entity that is not touching the ground.
AIR_DRAG = 0.91
#: Vertical drag vanilla applies after subtracting gravity.
VERTICAL_DRAG = 0.98
#: Vanilla gravity for living entities (``getEffectiveGravity``).
GRAVITY = 0.08
#: How far ahead a prediction may look.  Everything past this is fiction.
PREDICTION_HORIZON_TICKS = 20
#: Eye offset above the feet, used when a caller does not pass an origin.
EYE_HEIGHT = 1.62
#: Speed a fully drawn arrow leaves the bow with, blocks per tick.
ARROW_SPEED = 3.0
#: Entities count as airborne once this much vertical speed is left.
AIRBORNE_EPSILON = 1.0e-3


def look_angles(origin: Vec3, target: Vec3) -> tuple[float, float]:
    """``(yaw, pitch)`` in degrees pointing from ``origin`` at ``target``.

    Same convention as ``protobot.actions.raycast.look_angles``: yaw 0 looks
    towards +Z, pitch is positive downwards.
    """

    dx = target.x - origin.x
    dy = target.y - origin.y
    dz = target.z - origin.z
    horizontal = math.hypot(dx, dz)
    yaw = math.degrees(math.atan2(-dx, dz))
    pitch = math.degrees(-math.atan2(dy, horizontal))
    return yaw, pitch


def direction_from_angles(yaw: float, pitch: float) -> Vec3:
    """Unit vector for a yaw/pitch pair, inverse of :func:`look_angles`."""

    yaw_radians = math.radians(yaw)
    pitch_radians = math.radians(pitch)
    cos_pitch = math.cos(pitch_radians)
    return Vec3(
        -math.sin(yaw_radians) * cos_pitch,
        -math.sin(pitch_radians),
        math.cos(yaw_radians) * cos_pitch,
    )


@dataclass(frozen=True, slots=True)
class MotionModel:
    """The constants one tick of entity motion is made of.

    ``hold_horizontal`` decides what we assume about the entity's *own* input,
    the one thing no packet carries.  Something standing still that gets pushed
    (a knockback) loses horizontal speed at the friction of the block underfoot;
    a mob that is walking keeps its speed because its AI keeps pushing.  The
    default leans towards the walking target, because that is what an aimed
    action usually faces, and ``PHYSICS_MOTION`` is the faithful no-input
    integrator used to check the arc against a real client.
    """

    friction: float = GROUND_FRICTION
    air_drag: float = AIR_DRAG
    vertical_drag: float = VERTICAL_DRAG
    gravity: float = GRAVITY
    hold_horizontal: bool = True


DEFAULT_MOTION = MotionModel()
#: The faithful no-input integrator: horizontal speed decays every tick.
PHYSICS_MOTION = MotionModel(hold_horizontal=False)


@dataclass(frozen=True, slots=True)
class PredictedState:
    """One predicted tick of an entity."""

    ticks: int
    position: Vec3
    velocity: Vec3
    on_ground: bool
    confidence: float = 1.0


def _supported(position: Vec3, world: World | None, width: float) -> bool | None:
    """Is something's top face under ``position``?  ``None`` when unknown."""

    if world is None:
        return None
    has_ground = getattr(world, "has_ground_under", None)
    if has_ground is None:
        return None
    return bool(has_ground(position, width=width))


def step(
    state: PredictedState,
    *,
    model: MotionModel = DEFAULT_MOTION,
    world: World | None = None,
    width: float = 0.6,
) -> PredictedState:
    """Advance a dead-reckoned entity by one tick.

    The order matches ``LivingEntity.travel``: move by the velocity, subtract
    gravity from it, then apply the horizontal friction of the block the entity
    stood on and the vertical drag.
    """

    position = Vec3(
        state.position.x + state.velocity.x,
        state.position.y + state.velocity.y,
        state.position.z + state.velocity.z,
    )
    horizontal = model.friction if state.on_ground else model.air_drag
    if model.hold_horizontal:
        # Assume the entity keeps walking: its AI, or the player's keys, keep
        # pushing, so only gravity and the vertical drag change its velocity.
        horizontal = 1.0
    velocity = Vec3(
        state.velocity.x * horizontal,
        (state.velocity.y - model.gravity) * model.vertical_drag,
        state.velocity.z * horizontal,
    )

    supported = _supported(position, world, width)
    if supported is None:
        # Without a world the honest rule is "airborne as soon as it moves up":
        # gravity keeps a falling entity down, and a jump only happens because
        # something told it to, which we cannot see.
        on_ground = state.on_ground and state.velocity.y <= AIRBORNE_EPSILON
    else:
        on_ground = supported
        if on_ground:
            velocity = Vec3(velocity.x, 0.0, velocity.z)

    return PredictedState(
        ticks=state.ticks + 1,
        position=position,
        velocity=velocity,
        on_ground=on_ground,
        confidence=state.confidence,
    )


def arc(
    state: PredictedState,
    ticks: int,
    *,
    model: MotionModel = DEFAULT_MOTION,
    world: World | None = None,
    width: float = 0.6,
) -> list[PredictedState]:
    """``ticks`` future states, starting with the one passed in."""

    states = [state]
    current = state
    for _ in range(max(0, ticks)):
        current = step(current, model=model, world=world, width=width)
        states.append(current)
    return states


@dataclass(frozen=True, slots=True)
class ProjectileModel:
    """An arrow-like projectile: initial speed, drag and gravity per tick."""

    speed: float = ARROW_SPEED
    drag: float = 0.99
    gravity: float = 0.05


ARROW = ProjectileModel()


@dataclass(frozen=True, slots=True)
class Launch:
    """A solved rotation: fly ``ticks`` ticks to land on the requested point."""

    yaw: float
    pitch: float
    ticks: int


def flight_path(
    origin: Vec3,
    yaw: float,
    pitch: float,
    *,
    model: ProjectileModel = ARROW,
    max_ticks: int = 200,
) -> list[Vec3]:
    """Positions of the projectile after each tick it flies."""

    direction = direction_from_angles(yaw, pitch)
    velocity = direction.scale(model.speed)
    position = origin
    path: list[Vec3] = []
    for _ in range(max(0, max_ticks)):
        position = Vec3(
            position.x + velocity.x,
            position.y + velocity.y,
            position.z + velocity.z,
        )
        velocity = Vec3(
            velocity.x * model.drag,
            (velocity.y - model.gravity) * model.drag,
            velocity.z * model.drag,
        )
        path.append(position)
    return path


def _drop_error(
    origin: Vec3,
    target: Vec3,
    horizontal: float,
    pitch: float,
    *,
    model: ProjectileModel,
    max_ticks: int,
) -> tuple[float, int] | None:
    """How far the projectile passes the target height at the target range."""

    yaw = look_angles(origin, Vec3(target.x, origin.y, target.z))[0]
    direction = direction_from_angles(yaw, pitch)
    velocity = direction.scale(model.speed)
    position = origin
    previous = origin
    previous_horizontal = 0.0
    for tick in range(1, max(0, max_ticks) + 1):
        previous = position
        position = Vec3(
            position.x + velocity.x,
            position.y + velocity.y,
            position.z + velocity.z,
        )
        velocity = Vec3(
            velocity.x * model.drag,
            (velocity.y - model.gravity) * model.drag,
            velocity.z * model.drag,
        )
        travelled = math.hypot(position.x - origin.x, position.z - origin.z)
        if travelled >= horizontal:
            span = travelled - previous_horizontal
            if span <= 0.0:
                return None
            fraction = (horizontal - previous_horizontal) / span
            height = previous.y + (position.y - previous.y) * fraction
            return height - target.y, tick
        previous_horizontal = travelled
    return None


def solve_launch(
    origin: Vec3,
    target: Vec3,
    *,
    model: ProjectileModel = ARROW,
    max_ticks: int = 200,
    tolerance: float = 0.05,
) -> Launch | None:
    """Rotation that lands a projectile on ``target``, or ``None`` if too far.

    Pitch is positive downwards.  A flat shot always falls short of a target at
    or above eye level, and aiming further up only adds height at the target's
    range, so the height crosses the target exactly once per side: the sweep
    finds the crossing nearest to flat and a bisection inside it returns the
    low arc -- the shot a player can repeat, and the one that spends the least
    time in the air, which matters when the target moves.
    """

    dx = target.x - origin.x
    dz = target.z - origin.z
    horizontal = math.hypot(dx, dz)

    if horizontal < 1.0e-6:
        # Straight up or straight down: nothing to sweep, just point at it.
        pitch = look_angles(origin, target)[1]
        return Launch(yaw=look_angles(origin, target)[0], pitch=pitch, ticks=1)

    yaw = look_angles(origin, Vec3(target.x, origin.y, target.z))[0]

    def error(pitch: float) -> tuple[float, int] | None:
        return _drop_error(
            origin, target, horizontal, pitch, model=model, max_ticks=max_ticks
        )

    flat = error(0.0)
    if flat is None:
        return None

    # Walk the side the flat shot needs until the height changes sign.
    direction = 1.0 if flat[0] >= 0.0 else -1.0
    near_pitch, near_error, near_ticks = 0.0, flat[0], flat[1]
    far: tuple[float, float, int] | None = None
    for index in range(1, 90):
        pitch = direction * float(index)
        result = error(pitch)
        if result is None:
            break
        if abs(result[0]) <= tolerance:
            return Launch(yaw=yaw, pitch=pitch, ticks=result[1])
        if (result[0] < 0.0) != (near_error < 0.0):
            far = (pitch, result[0], result[1])
            break
        near_pitch, near_error, near_ticks = pitch, result[0], result[1]
    if far is None:
        return None

    low_pitch, low_error = (
        (near_pitch, near_error) if near_pitch < far[0] else (far[0], far[1])
    )
    high_pitch = far[0] if low_pitch == near_pitch else near_pitch
    best_pitch, best_error, best_ticks = (
        far if abs(far[1]) < abs(near_error) else (near_pitch, near_error, near_ticks)
    )

    for _ in range(32):
        middle = (low_pitch + high_pitch) * 0.5
        result = error(middle)
        if result is None:
            high_pitch = middle
            continue
        height, ticks = result
        if abs(height) < abs(best_error):
            best_pitch, best_error, best_ticks = middle, height, ticks
        if abs(height) <= tolerance:
            return Launch(yaw=yaw, pitch=middle, ticks=ticks)
        if (height < 0.0) == (low_error < 0.0):
            low_pitch = middle
        else:
            high_pitch = middle
        if high_pitch - low_pitch < 1.0e-4:
            break

    return Launch(yaw=yaw, pitch=best_pitch, ticks=best_ticks)


@dataclass(frozen=True, slots=True)
class Aim:
    """A solved shot: rotation, target point and the flight time it assumed."""

    launch: Launch
    point: Vec3
    confidence: float

    @property
    def yaw(self) -> float:
        return self.launch.yaw

    @property
    def pitch(self) -> float:
        return self.launch.pitch


@dataclass(slots=True)
class Track:
    """What we know about one entity's motion right now."""

    position: Vec3
    velocity: Vec3
    tick: int
    on_ground: bool
    width: float = 0.6
    height: float = 1.8
    velocity_tick: int = -1


class EntityPredictor:
    """Per-entity motion tracks plus the arithmetic that looks ahead."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._tracks: dict[int, Track] = {}
        self._motions: dict[int, tuple[Vec3, int]] = {}
        bot.events.on("entity_move", self._on_move)
        bot.events.on("entity_motion", self._on_motion)
        bot.events.on("entity_remove", self._on_remove)

    # -- bookkeeping -----------------------------------------------------

    def _clock(self) -> int:
        return int(getattr(self.bot, "_ticks_completed", 0))

    def _on_remove(self, entity_ids: object) -> None:
        identifiers = (
            entity_ids if isinstance(entity_ids, (list, tuple, set)) else (entity_ids,)
        )
        for entity_id in identifiers:
            if isinstance(entity_id, int):
                self.forget(entity_id)

    def _on_move(self, entity_id: int, entity: EntityState | None) -> None:
        if entity is not None:
            self.observe(entity_id, entity)

    def _on_motion(
        self, entity_id: int, velocity: object, entity: EntityState | None = None
    ) -> None:
        try:
            values = tuple(float(part) for part in velocity)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if len(values) != 3:
            return
        self._motions[entity_id] = (Vec3(*values), self._clock())
        if entity_id in self._tracks:
            # A push replaces the extrapolated velocity: the next ticks belong
            # to the impulse, not to the walk we measured before it.
            track = self._tracks[entity_id]
            track.velocity = Vec3(*values)
            track.velocity_tick = self._clock()
            track.on_ground = False

    def observe(self, entity_id: int, entity: EntityState | None = None) -> None:
        """Record the entity's latest position and derive its velocity."""

        if entity is None:
            entity = getattr(self.bot, "entities", {}).get(entity_id)
        if entity is None:
            return
        position = Vec3(entity.x, entity.y, entity.z)
        now = self._clock()
        previous = self._tracks.get(entity_id)
        if previous is None:
            velocity = Vec3(entity.velocity_x, entity.velocity_y, entity.velocity_z)
            self._tracks[entity_id] = Track(
                position=position,
                velocity=velocity,
                tick=now,
                on_ground=bool(entity.on_ground),
                width=float(entity.collision_width or 0.6),
                height=float(entity.collision_height or 1.8),
                velocity_tick=now if velocity.length_squared else -1,
            )
            return

        elapsed = now - previous.tick
        if elapsed > 0:
            derived = Vec3(
                (position.x - previous.position.x) / elapsed,
                (position.y - previous.position.y) / elapsed,
                (position.z - previous.position.z) / elapsed,
            )
            packet = self._motions.get(entity_id)
            if packet is not None and packet[1] >= previous.tick:
                previous.velocity = packet[0]
                previous.velocity_tick = packet[1]
            else:
                previous.velocity = derived
                previous.velocity_tick = now
        previous.position = position
        previous.tick = now
        previous.on_ground = bool(entity.on_ground)
        previous.width = float(entity.collision_width or previous.width)
        previous.height = float(entity.collision_height or previous.height)

    def forget(self, entity_id: int) -> None:
        self._tracks.pop(entity_id, None)
        self._motions.pop(entity_id, None)

    def clear(self) -> None:
        self._tracks.clear()
        self._motions.clear()

    # -- queries ---------------------------------------------------------

    def track(self, entity_id: int) -> Track | None:
        track = self._tracks.get(entity_id)
        if track is None:
            entity = getattr(self.bot, "entities", {}).get(entity_id)
            if entity is not None:
                self.observe(entity_id, entity)
                track = self._tracks.get(entity_id)
        return track

    def velocity_of(self, entity_id: int) -> Vec3:
        track = self.track(entity_id)
        return track.velocity if track is not None else Vec3()

    def confidence(self, entity_id: int, ticks: int) -> float:
        track = self.track(entity_id)
        if track is None:
            return 0.0
        # Two things rot a guess: how far ahead it looks, and how long ago we
        # last heard from the entity.
        lookahead = min(max(ticks, 0), PREDICTION_HORIZON_TICKS)
        freshness = min(
            max(self._clock() - track.tick, 0), PREDICTION_HORIZON_TICKS * 2
        )
        decay = 1.0 - lookahead / float(PREDICTION_HORIZON_TICKS)
        stale = 1.0 - freshness / float(PREDICTION_HORIZON_TICKS * 2)
        return max(0.0, min(1.0, decay * stale))

    def _start(self, entity_id: int, *, world: World | None = None) -> PredictedState | None:
        track = self.track(entity_id)
        if track is None:
            return None
        return PredictedState(
            ticks=0,
            position=track.position,
            velocity=track.velocity,
            on_ground=track.on_ground,
            confidence=self.confidence(entity_id, 0),
        )

    def position_at(
        self,
        entity_id: int,
        ticks: int = 1,
        *,
        model: MotionModel = DEFAULT_MOTION,
        world: World | None = None,
    ) -> Vec3 | None:
        """Where the entity's feet will be in ``ticks`` ticks, or ``None``."""

        start = self._start(entity_id, world=world)
        if start is None:
            return None
        lookahead = max(0, min(int(ticks), PREDICTION_HORIZON_TICKS))
        track = self.track(entity_id)
        width = track.width if track is not None else 0.6
        return arc(start, lookahead, model=model, world=world, width=width)[-1].position

    def arc(
        self,
        entity_id: int,
        ticks: int,
        *,
        model: MotionModel = DEFAULT_MOTION,
        world: World | None = None,
    ) -> list[PredictedState]:
        """Every predicted tick up to ``ticks``, starting from the last update."""

        start = self._start(entity_id, world=world)
        if start is None:
            return []
        lookahead = max(0, min(int(ticks), PREDICTION_HORIZON_TICKS))
        track = self.track(entity_id)
        width = track.width if track is not None else 0.6
        return arc(start, lookahead, model=model, world=world, width=width)

    def center_at(
        self,
        entity_id: int,
        ticks: int = 1,
        *,
        world: World | None = None,
    ) -> Vec3 | None:
        """Middle of the entity's box at that tick: the point to aim at."""

        position = self.position_at(entity_id, ticks, world=world)
        if position is None:
            return None
        track = self.track(entity_id)
        height = track.height if track is not None else 1.8
        return Vec3(position.x, position.y + height * 0.5, position.z)

    def eye_position(self) -> Vec3:
        """Our own eye, the origin a projectile leaves from."""

        position = self.bot.physics_state.position
        return Vec3(position.x, position.y + EYE_HEIGHT, position.z)

    def intercept(
        self,
        entity_id: int,
        *,
        origin: Vec3 | None = None,
        model: ProjectileModel = ARROW,
        iterations: int = 4,
        world: World | None = None,
        max_ticks: int = 200,
    ) -> Aim | None:
        """Solve the shot that meets the moving entity where it will be.

        Iterates "assume the flight takes t ticks, aim at the target's position
        after t ticks, solve the launch, read the flight time back" until the
        flight time stops moving.  Returns ``None`` when the target is beyond
        reach or the iteration does not settle (a target turning faster than
        the projectile can compensate).
        """

        start = self._start(entity_id, world=world)
        if start is None:
            return None
        shoot_from = origin if origin is not None else self.eye_position()
        flight = max(1, int(round(
            math.hypot(
                start.position.x - shoot_from.x, start.position.z - shoot_from.z
            )
            / max(model.speed, 1.0e-6)
        )))
        aim_point: Vec3 | None = None
        launch: Launch | None = None
        for _ in range(max(1, iterations)):
            aim_point = self.center_at(entity_id, flight, world=world)
            if aim_point is None:
                return None
            solved = solve_launch(
                shoot_from, aim_point, model=model, max_ticks=max_ticks
            )
            if solved is None:
                return None
            launch = solved
            if abs(solved.ticks - flight) <= 1:
                flight = solved.ticks
                break
            flight = max(1, solved.ticks)
        if launch is None or aim_point is None:
            return None
        return Aim(
            launch=launch,
            point=aim_point,
            confidence=self.confidence(entity_id, launch.ticks),
        )
