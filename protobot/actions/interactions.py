"""The interaction primitives vanilla clients send when they touch the world.

Nothing in ProtoBot used to send ``PlayerAction``, ``UseItemOn``, ``Interact``
or ``Swing``, which is why a bot could walk perfectly and still not be able to
mine a single block.  Each primitive here is deliberately small:

* it sends exactly the packets vanilla sends, in vanilla order;
* it aims first, because the server validates the target block and face;
* it declares a completion condition, so "I sent the packet" and "the server
  did it" are different results;
* it refuses to send an id this release never audited
  (:class:`~protobot.actions.errors.UnsupportedAction`).

Sequences are per-client and shared with ``Bot.use_item`` so the server
sees one monotonic stream.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from ..constants import (
    BLOCK_INTERACTION_RANGE,
    BREAK_COOLDOWN_SECONDS,
    CONFIRM_TIMEOUT_SECONDS,
    DEFAULT_HOLD_SECONDS,
    EARLY_RELEASE_TOLERANCE,
    ENTITY_ATTACK_RANGE,
    HOLD_MARGIN_SECONDS,
    ROTATION_EPSILON,
    TICK_SECONDS,
)
from ..physics import Vec3
from ..protocol.codec import PacketWriter
from ..protocol.entities import DEFAULT_HEIGHT, DEFAULT_WIDTH
from ..protocol.tools import best_tool
from .errors import ActionError, UnsupportedAction
from .profiles import InteractionProfile, profile_for
from .raycast import (
    BlockFace,
    BlockHit,
    EntityHit,
    direction_from_angles,
    look_angles,
    raycast_blocks,
    raycast_entities,
)
from .results import ActionResult, ActionStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import Bot

#: ``ServerboundInteractPacket`` action types, legacy (pre-26.1) layout only.
#: 26.1 dropped the type: the server infers the intent from the packet contents.
INTERACT = 0
INTERACT_ATTACK = 1
INTERACT_AT = 2

#: One client tick at 20 Hz, used to pace the hold-and-wait loops.  The value
#: lives in :mod:protobot.constants; this name stays for readability below.
#: How much longer than the predicted break time a held button stays down
#: before ``STOP`` goes out.  The prediction is what the server will use, so a
#: round number would do -- but anti-cheat compares the stop against that
#: prediction and cancels it when the two disagree by even a couple of ticks
#: (Grim's ``FastBreak`` cancels once its balance passes a second, adding the
#: difference each time).  A small margin keeps every stop on the slow side.
#: Hold used when the block's hardness is not in the table.  Long enough for
#: everything hand-breakable, which is the only case a caller can still hit.
#: How long a vanilla client waits after finishing a block before it starts the
#: next one.  ``MultiPlayerGameMode`` sets ``destroyDelay = 5`` when a block
#: breaks and refuses to start another dig until those five ticks pass, so a
#: held button does not carry straight on into the next block.  Starting sooner
#: is what anti-cheat counts as a fast break (Grim's ``FastBreak`` adds
#: ``300ms - gap`` per block started within 275ms of a finish and flags once the
#: balance passes a second).
#: A held button swings the arm once per client tick, not once per animation
#: cycle: the vanilla client sends an animation packet every tick it is mining,
#: which is what lets an anti-cheat (Grim's ``FastBreak``) treat the animation
#: as a tick marker and demand one in the same tick as the break
#: (``NoSwingBreak``).  One swing per ``TICK_SECONDS`` is therefore the vanilla
#: cadence, not a nicety.

_HANDS = {"main_hand": 0, "main": 0, "off_hand": 1, "off": 1}

#: Sentinel for "the caller did not say which tool", so ``tool=None`` can mean
#: "a bare hand" instead of "use whatever is held".
_UNSET = object()


def _hand(value: str | int) -> int:
    if isinstance(value, str):
        try:
            return _HANDS[value.lower()]
        except KeyError:
            raise ValueError(f"unknown interaction hand {value!r}") from None
    if value not in (0, 1):
        raise ValueError("interaction hand must be main_hand (0) or off_hand (1)")
    return value


def _block_point(position: tuple[int, int, int], face: BlockFace) -> Vec3:
    """Centre of ``position``'s ``face``, i.e. the safest point to aim at."""

    x, y, z = position
    dx, dy, dz = face.step
    return Vec3(x + 0.5 + dx * 0.5, y + 0.5 + dy * 0.5, z + 0.5 + dz * 0.5)


def _continuous_yaw(previous: float, yaw: float) -> float:
    """The representation of ``yaw`` closest to ``previous``.

    Yaw is modulo 360, so -179 and 181 are the same direction.  Sending the
    wrapped form turns a small correction into a ~358 degree snap, which is
    what Grim's AimModulo360 flags ("Sent a large yaw snap"): a real mouse
    turns the short way and the packet delta is the size of that movement.
    """

    return previous + ((yaw - previous + 180.0) % 360.0 - 180.0)


class Actions:
    """Interaction primitives bound to one connected client.

    Reached through ``client.actions``; constructing one directly is fine too
    and lets callers keep a stable handle across reconnects of the same client
    object.
    """

    def __init__(self, client: "Bot") -> None:
        self.client = client
        #: When the last dig stopped, for :data:`BREAK_COOLDOWN_SECONDS`.
        self._last_break = 0.0

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    @property
    def version(self):
        return self.client.version

    @property
    def profile(self) -> InteractionProfile:
        """The audited payload shapes for this release.

        Packet ids are audited separately (:data:`~protobot.protocol.versions.SUPPORTED_VERSIONS`);
        this is the other half -- the enum ordinals and payload layouts inside
        those packets, which changed in 26.1 and again in 26.3.
        """

        try:
            return profile_for(self.version.protocol)
        except KeyError:
            raise UnsupportedAction(
                "interaction", self.version.name, "interaction profile"
            ) from None

    def _packet_id(self, field: str, action: str) -> int:
        """The audited id for ``field``, or :class:`UnsupportedAction`."""

        packet_id = getattr(self.version.packets, field, 0)
        if not packet_id:
            raise UnsupportedAction(action, self.version.name, field)
        return packet_id

    def _sequence(self) -> int:
        return self.client.take_sequence()

    async def _send(self, field: str, action: str, payload: bytes) -> int:
        packet_id = self._packet_id(field, action)
        close = getattr(self.client, "close_tick", None)
        if close is not None:
            # A 26.3 client closes its movement tick with ``client_tick_end``,
            # and the server treats that packet as a transaction.  An action
            # that slips in after the movement packet but before that end
            # marker is read as "sent before this tick", which anti-cheat
            # reports as Post for that action.  Closing first puts every action
            # in the gap between two ticks, where the next movement packet --
            # not a transaction -- is what judges it.
            await close()
        await self.client.send_raw(packet_id, payload)
        return packet_id

    async def _emit(self, result: ActionResult) -> ActionResult:
        await self.client.events.emit("action", result)
        return result

    def eye_position(self) -> Vec3:
        """The player's eye, which is where every ray and every aim starts."""

        state = self.client.physics_state
        return Vec3(
            state.position.x,
            state.position.y + state.eye_height,
            state.position.z,
        )

    def look_direction(self) -> Vec3:
        state = self.client.physics_state
        return direction_from_angles(state.yaw, state.pitch)

    async def look_at(self, point: Vec3) -> tuple[float, float]:
        """Turn the player towards ``point`` and send the rotation."""

        yaw, pitch = look_angles(self.eye_position(), point)
        yaw = _continuous_yaw(self.client.physics_state.yaw, yaw)
        await self.client.send_look(yaw, pitch)
        return yaw, pitch

    async def turn_to(
        self,
        point: Vec3 | tuple[float, float, float],
        *,
        tick: bool = True,
    ) -> tuple[float, float]:
        """Turn towards ``point`` *inside a movement packet*.

        Vanilla never ships a rotation on its own: the new angles ride in the
        tick's position/rotation packet, so the server -- and any anti-cheat
        that ray-traces the rotation of the tick a click arrives in -- sees the
        two as one unit.  A bare look packet lands a tick early, which is
        exactly the gap that makes a click look like it came "out of thin air".

        Nothing is sent when the angles already match, so aiming twice in a row
        stays one packet.
        """

        target = point if isinstance(point, Vec3) else Vec3(*point)
        yaw, pitch = look_angles(self.eye_position(), target)
        state = self.client.physics_state
        yaw = _continuous_yaw(state.yaw, yaw)
        if (
            abs((state.yaw - yaw + 180.0) % 360.0 - 180.0) < ROTATION_EPSILON
            and abs(state.pitch - pitch) < ROTATION_EPSILON
        ):
            return yaw, pitch
        if tick:
            # Run the physics of a tick first: the on-ground flag that rides
            # along in the next movement packet has to be the fresh one.
            await self.client.tick()
        await self.client.wait_tick_budget()
        stand = self.client.physics_state.position
        await self.client.send_position_and_rotation(
            stand.x, stand.y, stand.z, yaw, pitch
        )
        if tick:
            # And one more tick with the aim in place, so both the rotation of
            # this tick and the one before it point the same way.
            await self.client.wait_tick_budget()
            await self.client.tick()
        return yaw, pitch

    def raycast(
        self,
        *,
        max_distance: float = BLOCK_INTERACTION_RANGE,
        origin: Vec3 | None = None,
        direction: Vec3 | None = None,
    ) -> BlockHit | None:
        """What the player is aiming at, using the live rotation."""

        return raycast_blocks(
            self.client.world,
            origin if origin is not None else self.eye_position(),
            direction if direction is not None else self.look_direction(),
            max_distance=max_distance,
        )

    # ------------------------------------------------------------------
    # entity raycast
    # ------------------------------------------------------------------

    def entity_size(self, entity) -> tuple[float, float]:
        """Hitbox of a tracked entity: measured data first, then the table.

        ``set_entity_data`` only carries collision dimensions for a handful of
        types, so the per-release entity table is the usual source.  Sizes that
        neither source knows stay at the vanilla humanoid default rather than
        being skipped, because a missing box turns a real hit into a miss.
        """

        width = getattr(entity, "collision_width", None)
        height = getattr(entity, "collision_height", None)
        if width and height:
            return (float(width), float(height))
        type_id = getattr(entity, "type_id", None)
        if type_id is not None:
            size = self.version.entity_size(type_id)
            if size is not None:
                return size
        return (DEFAULT_WIDTH, DEFAULT_HEIGHT)

    def _entity_candidates(
        self,
    ) -> list[tuple[int, Vec3, tuple[float, float]]]:
        """Every tracked entity as ``(id, feet, size)`` for the ray cast."""

        own = getattr(self.client, "entity_id", None)
        candidates: list[tuple[int, Vec3, tuple[float, float]]] = []
        for entity_id, entity in self.client.entities.items():
            if own is not None and entity_id == own:
                continue
            candidates.append(
                (
                    entity_id,
                    Vec3(entity.x, entity.y, entity.z),
                    self.entity_size(entity),
                )
            )
        return candidates

    def raycast_entity(
        self,
        *,
        max_distance: float = ENTITY_ATTACK_RANGE,
        origin: Vec3 | None = None,
        direction: Vec3 | None = None,
    ) -> EntityHit | None:
        """The entity the player is looking at, within attack reach.

        Three blocks is the survival reach the server enforces; a hit farther
        away than that would only waste a packet.
        """

        return raycast_entities(
            self._entity_candidates(),
            origin if origin is not None else self.eye_position(),
            direction if direction is not None else self.look_direction(),
            max_distance=max_distance,
        )

    def entity_center(self, entity_id: int) -> Vec3 | None:
        """Middle of an entity's hitbox -- where an aim should point."""

        entity = self.client.entities.get(entity_id)
        if entity is None:
            return None
        size = self.entity_size(entity)
        return Vec3(entity.x, entity.y + size[1] / 2.0, entity.z)

    async def look_at_entity(self, entity_id: int) -> tuple[float, float]:
        """Turn towards an entity's centre and send the rotation."""

        center = self.entity_center(entity_id)
        if center is None:
            raise ActionError(
                f"entity {entity_id} is not tracked, so there is nothing to aim at"
            )
        return await self.look_at(center)

    async def attack_looked_at(
        self, *, max_distance: float = ENTITY_ATTACK_RANGE, swing: bool = True
    ) -> ActionResult | None:
        """Aim at the entity under the crosshair and hit it.

        Returns ``None`` when the crosshair is not on an entity, so a caller
        can tell "nothing there" from "not sent".
        """

        hit = self.raycast_entity(max_distance=max_distance)
        if hit is None:
            return None
        await self.look_at(hit.center)
        # Turning can move the crosshair off the target (or onto another one),
        # and a hit whose ray does not touch the hitbox is exactly what Grim's
        # Hitboxes check flags.  Only hit what the crosshair covers right now.
        again = self.raycast_entity(max_distance=max_distance)
        if again is None:
            return None
        result = await self.attack(again.entity_id, swing=swing)
        return replace(
            result,
            evidence=f"hit entity {again.entity_id} at {again.distance:.2f} blocks",
        )

    async def _settle(
        self,
        until,
        *,
        seconds: float,
        tick: bool,
        swing: bool = False,
    ) -> bool:
        """Tick the client until ``until()`` is true or ``seconds`` elapse.

        Ticking matters: a dig only makes progress while the player keeps
        sending movement packets, and the receive loop needs wall-clock time to
        deliver the server's answer.

        ``swing`` repeats the arm swing before every tick, which is the vanilla
        cadence while a button is held: one animation packet per client tick,
        sent before that tick's movement packet.  Anti-cheat reads the pair
        literally -- GrimAC requires a swing in the same tick as a break and
        treats the animations as the ticks in which mining progress may have
        advanced -- and it also closes its per-tick packet queue on the movement
        packet, so an animation sent after the movement is still queued when the
        next transaction arrives, which it reports as ``Post``.
        """

        deadline = time.monotonic() + seconds
        while True:
            if until():
                return True
            if time.monotonic() >= deadline:
                return False
            # The swing goes out *before* this tick's movement packet: a vanilla
            # client sends its input (attack/use/dig animation) and then closes
            # the tick with the position packet, which is the packet GrimAC uses
            # to clear its per-tick packet queue.  Sending the animation after
            # the movement leaves it queued when the next transaction lands, and
            # GrimAC reports that as "Post".
            if swing and tick:
                # Wait out the tick budget *first*, so the swing and the
                # movement packet go out back to back.  Sleeping after the tick
                # would leave the animation queued alone for 50 ms, which
                # GrimAC's Post check reports as "did not send animation then
                # tick" once a transaction arrives in that gap.
                await self.client.wait_tick_budget()
                await self._swing_packet()
                await self.client.tick()
            elif swing:
                await self._swing_packet()
                await asyncio.sleep(TICK_SECONDS)
            elif tick:
                # ``Bot.tick`` already paces movement packets to one per 20 Hz
                # tick (see ``MIN_TICK_GAP``), so sleeping here as well would
                # halve the tick rate and with it the mining progress the
                # server credits us with.
                await self.client.tick()
            else:
                await asyncio.sleep(TICK_SECONDS)

    # ------------------------------------------------------------------
    # arm / entity actions
    # ------------------------------------------------------------------
    async def _swing_packet(self, hand: str | int = "main_hand") -> int:
        """Send one arm swing without reporting it as an action of its own."""

        payload = (
            b""
            if self.profile.swing_is_empty
            else PacketWriter().write_varint(_hand(hand)).to_bytes()
        )
        return await self._send("serverbound_swing", "swing", payload)

    async def swing(self, *, hand: str | int = "main_hand") -> ActionResult:
        """Send one arm swing.

        26.3 replaced ``ServerboundSwingPacket`` (one ``InteractionHand``) with
        ``ServerboundPunchPacket``, which has no fields at all; the audited
        profile says which of the two this release speaks.
        """

        started = time.monotonic()
        packet_id = await self._swing_packet(hand)
        return await self._emit(
            ActionResult(
                action="swing",
                status=ActionStatus.SENT,
                packets=(packet_id,),
                elapsed=time.monotonic() - started,
            )
        )

    def _interact_location(
        self, entity_id: int, position: Vec3 | None
    ) -> Vec3:
        """Where on the entity the click lands.

        The flat (26.1+) layout always carries a location, so there is no
        "no position" packet any more.  Default to the tracked entity position,
        which is what a click on the entity's own feet would report.
        """

        if position is not None:
            return position
        # ``entities`` is on Bot; getattr keeps light stand-ins testable.
        entity = getattr(self.client, "entities", {}).get(entity_id)
        if entity is None:
            return Vec3(0.0, 0.0, 0.0)
        return Vec3(entity.x, entity.y, entity.z)

    def _flat_interact(
        self,
        entity_id: int,
        hand: int,
        location: Vec3,
        *,
        secondary: bool = False,
    ) -> bytes:
        """``entityId + hand + location + usingSecondaryAction`` (26.1+)."""

        return (
            PacketWriter()
            .write_varint(entity_id)
            .write_varint(hand)
            .write_lp_vec3(location.x, location.y, location.z)
            .write_bool(secondary)
            .to_bytes()
        )

    async def interact(
        self,
        entity_id: int,
        *,
        hand: str | int = "main_hand",
        position: Vec3 | None = None,
    ) -> ActionResult:
        """Right click an entity."""

        started = time.monotonic()
        hand_code = _hand(hand)
        if self.profile.interact_is_flat:
            payload = self._flat_interact(
                entity_id, hand_code, self._interact_location(entity_id, position)
            )
        else:
            payload = (
                PacketWriter()
                .write_varint(entity_id)
                .write_varint(INTERACT)
                .write_varint(hand_code)
                .to_bytes()
            )
        packet_id = await self._send("serverbound_interact", "interact", payload)
        return await self._emit(
            ActionResult(
                action="interact",
                status=ActionStatus.SENT,
                entity_id=entity_id,
                packets=(packet_id,),
                elapsed=time.monotonic() - started,
            )
        )

    async def interact_at(
        self, entity_id: int, position: Vec3, *, hand: str | int = "main_hand"
    ) -> ActionResult:
        """Interact at a point on an entity.

        Legacy layouts tag this with type ``INTERACT_AT``; from 26.1 the point is
        simply the ``location`` field of every interact packet.
        """

        started = time.monotonic()
        hand_code = _hand(hand)
        if self.profile.interact_is_flat:
            payload = self._flat_interact(entity_id, hand_code, position)
        else:
            payload = (
                PacketWriter()
                .write_varint(entity_id)
                .write_varint(INTERACT_AT)
                .write_float(position.x)
                .write_float(position.y)
                .write_float(position.z)
                .write_varint(hand_code)
                .to_bytes()
            )
        packet_id = await self._send("serverbound_interact", "interact_at", payload)
        return await self._emit(
            ActionResult(
                action="interact_at",
                status=ActionStatus.SENT,
                entity_id=entity_id,
                packets=(packet_id,),
                elapsed=time.monotonic() - started,
            )
        )

    async def attack(
        self, entity_id: int, *, swing: bool = True, tick: bool = True
    ) -> ActionResult:
        """Attack an entity.

        26.1 split the attack out of ``Interact`` into ``ServerboundAttackPacket``;
        1.21.x sends ``Interact`` with type ``ATTACK``.  The audited profile says
        which one this release has, so the same call works on both.

        The hit and the swing belong to one movement tick: a vanilla client
        sends attack, animation and then that tick's movement packet.  Leaving
        the attack waiting for a later tick is what Grim's Post check flags.
        """

        started = time.monotonic()
        packets: list[int] = []
        await self.client.wait_tick_budget()
        if self.profile.attack_packet:
            packets.append(
                await self._send(
                    "serverbound_attack",
                    "attack",
                    PacketWriter().write_varint(entity_id).to_bytes(),
                )
            )
        else:
            packets.append(
                await self._send(
                    "serverbound_interact",
                    "attack",
                    PacketWriter()
                    .write_varint(entity_id)
                    .write_varint(INTERACT_ATTACK)
                    .to_bytes(),
                )
            )
        if swing:
            packets.extend((await self.swing()).packets)
        if tick:
            await self.client.tick()
        return await self._emit(
            ActionResult(
                action="attack",
                status=ActionStatus.SENT,
                entity_id=entity_id,
                packets=tuple(packets),
                elapsed=time.monotonic() - started,
            )
        )

    # ------------------------------------------------------------------
    # the face a client can actually click
    # ------------------------------------------------------------------
    def block_box(self, position: tuple[int, int, int]) -> tuple[Vec3, Vec3]:
        """The world box of the block at ``position``, low and high corners.

        Collision shapes come from the release data, so a slab is half a block
        and a full block is the unit cube it looks like.  Releases without shape
        data fall back to that cube.
        """

        x, y, z = position
        low = Vec3(float(x), float(y), float(z))
        high = Vec3(float(x) + 1.0, float(y) + 1.0, float(z) + 1.0)
        world = getattr(self.client, "world", None)
        registry = getattr(world, "block_states", None)
        if world is None or registry is None:
            return low, high
        try:
            definition = registry.get(world.get_state_id(x, y, z))
        except Exception:
            return low, high
        shapes = getattr(definition, "shapes", ()) or ()
        if not shapes:
            return low, high
        return (
            Vec3(
                x + min(shape.min_x for shape in shapes),
                y + min(shape.min_y for shape in shapes),
                z + min(shape.min_z for shape in shapes),
            ),
            Vec3(
                x + max(shape.max_x for shape in shapes),
                y + max(shape.max_y for shape in shapes),
                z + max(shape.max_z for shape in shapes),
            ),
        )

    def visible_faces(self, position: tuple[int, int, int]) -> tuple[BlockFace, ...]:
        """The faces of ``position`` whose plane the eye is on the outside of.

        A client can only click a face it can see, and a real server may refuse
        the rest: Grim's ``PositionBreakA`` cancels a break that claims a face
        the eye cannot reach (``case UP -> eyePositions.maxY < combined.maxY``),
        without a word in the console.  A block raised above eye level therefore
        has no clickable top face, which is why aiming at one by default is a
        bug and not a detail.
        """

        low, high = self.block_box(position)
        eye = self.eye_position()
        faces: list[BlockFace] = []
        if eye.y > high.y:
            faces.append(BlockFace.UP)
        if eye.y < low.y:
            faces.append(BlockFace.DOWN)
        if eye.z < low.z:
            faces.append(BlockFace.NORTH)
        if eye.z > high.z:
            faces.append(BlockFace.SOUTH)
        if eye.x < low.x:
            faces.append(BlockFace.WEST)
        if eye.x > high.x:
            faces.append(BlockFace.EAST)
        return tuple(faces)

    def _inside_point(
        self, position: tuple[int, int, int], face: BlockFace
    ) -> Vec3:
        """A point just inside ``position`` behind ``face``.

        Aiming at the exact centre of a face puts the ray on the boundary,
        where whether it counts as a hit is a coin flip; anti-cheat ray-traces
        the rotation against the block box (Grim's ``RotationPlace`` and
        ``RotationBreak``), so the aim goes slightly into the block instead.
        """

        dx, dy, dz = face.step
        return Vec3(
            position[0] + 0.5 + dx * 0.35,
            position[1] + 0.5 + dy * 0.35,
            position[2] + 0.5 + dz * 0.35,
        )

    def _facing_face(
        self, position: tuple[int, int, int], *, origin: Vec3 | None = None
    ) -> BlockFace:
        """The face of the block on the eye's side, by the dominant axis.

        A client that turns to look at a block reports the face that ends up in
        front of it, which is the side of the box the eye is furthest outside
        of.  Ties go to the axis order the protocol enumerates.
        """

        low, high = self.block_box(position)
        eye = self.eye_position() if origin is None else origin
        deltas = (
            (low.x + high.x) / 2.0 - eye.x,
            (low.y + high.y) / 2.0 - eye.y,
            (low.z + high.z) / 2.0 - eye.z,
        )
        axis = max(range(3), key=lambda index: abs(deltas[index]))
        if axis == 0:
            return BlockFace.WEST if deltas[0] > 0 else BlockFace.EAST
        if axis == 1:
            return BlockFace.UP if deltas[1] < 0 else BlockFace.DOWN
        return BlockFace.NORTH if deltas[2] > 0 else BlockFace.SOUTH

    def pick_face(
        self, position: tuple[int, int, int], *, max_distance: float = BLOCK_INTERACTION_RANGE
    ) -> tuple[BlockFace, Vec3] | None:
        """The face a vanilla client would click on ``position``, and its aim.

        Vanilla sends the face its own pick ray hit, and nothing else.  When the
        current rotation does not reach the block, the face is the one that
        faces the eye -- what the client would report after turning to look at
        it.  ``None`` means no face is reachable at all, which is also when a
        real client could not click the block.
        """

        hit = self.raycast(max_distance=max_distance)
        if hit is not None and tuple(hit.position) == tuple(position):
            return hit.face, self._inside_point(position, hit.face)
        eye = self.eye_position()
        visible = self.visible_faces(position)
        face = self._facing_face(position, origin=eye)
        if face not in visible:
            if not visible:
                return None
            face = min(
                visible,
                key=lambda candidate: (
                    _block_point(position, candidate) - eye
                ).length_squared,
            )
        return face, self._inside_point(position, face)

    # ------------------------------------------------------------------
    # block actions
    # ------------------------------------------------------------------
    async def dig(
        self,
        position: tuple[int, int, int],
        *,
        face: BlockFace | None = None,
        aim: bool = True,
        tick: bool = True,
        hold: float | None = None,
        await_confirm: bool = True,
        confirm_timeout: float = CONFIRM_TIMEOUT_SECONDS,
        send_stop: bool = True,
    ) -> ActionResult:
        """Break the block at ``position``, the way a held mouse button does.

        The sequence is the vanilla one: aim, ``PlayerAction`` ``START``, the
        arm swing that goes with the button going down, keep the client ticking
        (one swing per tick, like a real client) while the server accumulates
        progress, then ``STOP``.  Confirmation is the world state at
        ``position`` changing -- which is exactly the assertion a test wants
        ("the block that was there is not there any more").

        ``hold`` is how long the button stays down.  ``None`` (the default)
        sizes it from :meth:`break_time` for the held item, which is what a
        client does implicitly: it sends ``STOP`` when its own prediction says
        the block broke.  Getting this wrong is not a small matter -- a real
        client that releases early sends ``ABORT``, and sending ``STOP`` early
        instead is both wrong and self-defeating: anti-cheat sees a break that
        finished in a fraction of the time the block needs and cancels the
        packet (Grim's ``FastBreak``), so the server never hears about the
        release and keeps its destroy state armed, which it then broadcasts as
        crack stages that climb forever.  An explicit ``hold`` shorter than
        the block needs is therefore closed with ``ABORT`` too, and reported as
        unconfirmed.  ``confirm_timeout`` is the extra window for the block
        update to arrive after ``STOP``.
        """

        started = time.monotonic()
        x, y, z = position
        world = self.client.world
        state_before = world.get_state_id(x, y, z)
        if state_before == 0:
            return await self._emit(
                ActionResult(
                    action="dig",
                    status=ActionStatus.REJECTED,
                    position=position,
                    state_before=state_before,
                    state_after=state_before,
                    elapsed=time.monotonic() - started,
                    evidence="the target is already air",
                )
            )

        # A client tells the server which face it clicked, and it can only tell
        # the truth about a face it can see.  Asking for one that faces away
        # from the eye gets the break cancelled by anti-cheat (Grim's
        # ``PositionBreakA``) while vanilla just ignores the mismatch, so the
        # packet always names a visible face.
        predicted = self._expected_break_time(state_before)
        if predicted is not None and math.isinf(predicted):
            return await self._emit(
                ActionResult(
                    action="dig",
                    status=ActionStatus.REJECTED,
                    position=position,
                    state_before=state_before,
                    state_after=state_before,
                    elapsed=time.monotonic() - started,
                    evidence="the block cannot be broken at all (hardness -1)",
                )
            )
        release = hold
        if release is None:
            release = DEFAULT_HOLD_SECONDS if predicted is None else predicted + HOLD_MARGIN_SECONDS
        # An explicit hold shorter than the block needs is an early release.
        # Vanilla closes that with ABORT; sending STOP instead makes anti-cheat
        # cancel the packet and leaves the server's destroy state armed.
        early = (
            predicted is not None
            and release + EARLY_RELEASE_TOLERANCE < predicted
        )

        # Vanilla will not start the next dig until five ticks after a block
        # breaks; going straight on is what anti-cheat calls a fast break.  The
        # wait is short, and skipping it is worth a whole second of balance.
        gap = BREAK_COOLDOWN_SECONDS - (time.monotonic() - self._last_break)
        if gap > 0.0:
            await asyncio.sleep(gap)

        picked = self.pick_face(position)
        if face is not None and face not in self.visible_faces(position):
            face = None
        if face is not None:
            hit_face = face
        elif picked is not None:
            hit_face = picked[0]
        else:
            hit_face = BlockFace.UP
        if aim:
            await self.look_at(
                picked[1]
                if picked is not None and picked[0] == hit_face
                else self._inside_point(position, hit_face)
            )

        codes = self.profile.codes
        sequence = self._sequence()
        # Vanilla order inside one tick: the action packet, then the arm swing
        # that goes with the button going down, then the movement packet that
        # closes the tick.  Grim's PacketOrderB reads exactly this -- an
        # interaction has to be followed by an animation -- and NoSwingBreak
        # wants an animation in the same tick as the break.  Waiting for the
        # tick budget first puts the whole triple in the same flush: aiming has
        # just sent a movement packet, and a start that sits in the queue on its
        # own is what Grim's Post reports as "player digging".
        await self.client.wait_tick_budget()
        packets = [
            await self._send(
                "serverbound_player_action",
                "dig",
                PacketWriter()
                .write_varint(codes.start)
                .write_position(x, y, z)
                .write_unsigned_byte(int(hit_face))
                .write_varint(sequence)
                .to_bytes(),
            )
        ]

        def changed() -> bool:
            return world.get_state_id(x, y, z) != state_before

        # ``_settle`` swings and then ticks, so the annotation above belongs to
        # the tick this call opens: start, animation, movement.
        broke = await self._settle(changed, seconds=release, tick=tick, swing=True)

        # An early release is an abort, not a stop.  A real client only sends
        # STOP once its prediction says the block broke -- and it has to be
        # right about that: anti-cheat weights how far ahead of the prediction
        # the stop arrived and cancels the packet (Grim's ``FastBreak``), which
        # leaves the server's destroy state armed and its crack stages climbing
        # for everyone watching.
        closing = codes.abort if early else codes.stop
        if send_stop:
            # The break goes out in a tick that started with a swing, the way a
            # client that is still holding the button sends it: animation, stop,
            # movement.  Wait out the tick budget so the movement follows at once
            # instead of leaving the pair queued for a transaction to find.
            await self.client.wait_tick_budget()
            packets.append(await self._swing_packet())
            packets.append(
                await self._send(
                    "serverbound_player_action",
                    "dig",
                    PacketWriter()
                    .write_varint(closing)
                    .write_position(x, y, z)
                    .write_unsigned_byte(int(hit_face))
                    .write_varint(self._sequence())
                    .to_bytes(),
                )
            )
            # Closing the tick keeps a placement or an attack that comes next out
            # of the same one, which is the ordering an anti-cheat expects
            # (Grim's PacketOrderI flags a place in the tick a dig finished).
            if tick:
                await self.client.tick()
        if await_confirm and not broke:
            broke = await self._settle(changed, seconds=confirm_timeout, tick=False)
        # The next dig has to keep its distance from this one, whether or not
        # this one landed: vanilla restarts its five tick destroy delay when a
        # break finishes, and the server decides "finished" by the stop.
        self._last_break = time.monotonic()

        state_after = world.get_state_id(x, y, z)
        if broke:
            status = ActionStatus.CONFIRMED
            evidence = f"world state {state_before} -> {state_after}"
        elif early:
            status = ActionStatus.UNCONFIRMED
            evidence = (
                f"released after {release:g}s but the block needs {predicted:g}s, "
                f"so the release went out as ABORT"
            )
        else:
            status = ActionStatus.UNCONFIRMED
            evidence = (
                f"no block update at {position} within "
                f"{release:g}s + {confirm_timeout:g}s"
            )
        return await self._emit(
            ActionResult(
                action="dig",
                status=status,
                sequence=sequence,
                position=position,
                state_before=state_before,
                state_after=state_after,
                packets=tuple(packets),
                elapsed=time.monotonic() - started,
                evidence=evidence,
                extra={"face": int(hit_face), "requested_face": None if face is None else int(face)},
            )
        )

    async def use_item_on(
        self,
        position: tuple[int, int, int],
        *,
        face: BlockFace = BlockFace.UP,
        cursor: tuple[float, float, float] | None = None,
        inside_block: bool = False,
        hand: str | int = "main_hand",
        tick: bool = True,
    ) -> ActionResult:
        """Right click the ``face`` of ``position`` (``ServerboundUseItemOnPacket``).

        This is the raw packet: the held item is used *against* that face, so a
        block item lands in the neighbouring voxel.  :meth:`place_block` is the
        version that takes the destination instead.
        """

        started = time.monotonic()
        sequence = self._sequence()
        if cursor is None:
            if face not in self.visible_faces(position):
                # Same rule as digging: a click names a face the eye can see.
                picked = self.pick_face(position)
                if picked is not None:
                    face = picked[0]
            dx, dy, dz = face.step
            # Centre of the face in block-local coordinates.
            cursor = (0.5 + dx * 0.5, 0.5 + dy * 0.5, 0.5 + dz * 0.5)
        payload = (
            PacketWriter()
            .write_varint(_hand(hand))
            .write_position(*position)
            .write_varint(int(face))
            .write_float(cursor[0])
            .write_float(cursor[1])
            .write_float(cursor[2])
            # BlockHitResult carries *two* booleans: whether the ray started
            # inside a block, and whether the hit was a world border.  Dropping
            # the second one shifts ``sequence`` by a byte and the server
            # silently discards the placement.
            .write_bool(inside_block)
            .write_bool(False)
            .write_varint(sequence)
            .to_bytes()
        )
        # The click belongs to a tick, and the tick has to be closed straight
        # away: a placement that is still queued when a transaction lands is
        # what Grim's Post reports ("player block placement"), the same way a
        # stray rollback of the arm swing gets reported as "punch".
        await self.client.wait_tick_budget()
        packet_id = await self._send("serverbound_use_item_on", "use_item_on", payload)
        # Vanilla swings the arm when the hand uses an item on a block, so the
        # placement has an animation behind it just like a break does.
        swing_id = await self._swing_packet(hand)
        packets = [packet_id, swing_id]
        # Closing the tick keeps whatever the caller does next (another
        # placement, a break) out of the tick this click belongs to, which is
        # the ordering PacketOrderI expects.
        if tick:
            await self.client.tick()
        return await self._emit(
            ActionResult(
                action="use_item_on",
                status=ActionStatus.SENT,
                sequence=sequence,
                position=position,
                packets=tuple(packets),
                elapsed=time.monotonic() - started,
                extra={"face": int(face), "cursor": cursor},
            )
        )

    def _placement_click(
        self, position: tuple[int, int, int], face: BlockFace
    ) -> tuple[tuple[int, int, int], BlockFace] | None:
        """A ``(clicked block, face)`` pair that would put a block at ``position``.

        ``face`` is tried first and the rest follow: when the requested side
        faces the eye it wins, and when it does not (placing on the ground next
        to something taller than the eye) another neighbour with a visible side
        pointing into ``position`` is clicked instead.  ``None`` means nothing
        neighbouring can be clicked from here.
        """

        world = self.client.world
        order = (face,) + tuple(other for other in BlockFace if other != face)
        for candidate in order:
            dx, dy, dz = candidate.step
            clicked = (position[0] - dx, position[1] - dy, position[2] - dz)
            if world.get_state_id(*clicked) == 0:
                continue
            if candidate in self.visible_faces(clicked):
                return clicked, candidate
        return None

    async def place_block(
        self,
        position: tuple[int, int, int],
        *,
        face: BlockFace = BlockFace.UP,
        hand: str | int = "main_hand",
        aim: bool = True,
        tick: bool = True,
        await_confirm: bool = True,
        confirm_timeout: float = CONFIRM_TIMEOUT_SECONDS,
    ) -> ActionResult:
        """Place the held block *into* ``position``.

        ``face`` names the side of the destination that touches the block you
        click: ``BlockFace.UP`` means "place on top of the block below", i.e.
        the clicked block is ``position - UP.step``.  If that side faces away
        from the eye, another neighbour with a visible side pointing into
        ``position`` is clicked instead -- a client may only claim a face it can
        see.  Confirmation is the world state at ``position`` leaving air.
        """

        started = time.monotonic()
        world = self.client.world
        state_before = world.get_state_id(*position)
        if state_before != 0:
            return await self._emit(
                ActionResult(
                    action="place_block",
                    status=ActionStatus.REJECTED,
                    position=position,
                    state_before=state_before,
                    elapsed=time.monotonic() - started,
                    evidence="the destination is not air",
                )
            )
        click = self._placement_click(position, face)
        if click is None:
            return await self._emit(
                ActionResult(
                    action="place_block",
                    status=ActionStatus.REJECTED,
                    position=position,
                    state_before=state_before,
                    elapsed=time.monotonic() - started,
                    evidence=(
                        f"no face of a block next to {position} is visible from here, "
                        "so a client could not click one"
                    ),
                )
            )
        clicked, face = click
        if aim:
            # Turn the way a vanilla client turns: the new rotation rides in a
            # movement packet (position_and_rotation), not in a bare look
            # packet.  Anti-cheat ray-traces the rotation of the tick the click
            # arrives in, so the aim has to already be part of a tick's
            # movement rather than sitting in a packet of its own.
            await self.turn_to(_block_point(clicked, face), tick=tick)
        placed = await self.use_item_on(clicked, face=face, hand=hand)
        packets = list(placed.packets)

        def changed() -> bool:
            return world.get_state_id(*position) != state_before

        confirmed = False
        if await_confirm:
            confirmed = await self._settle(
                changed, seconds=confirm_timeout, tick=tick
            )
        state_after = world.get_state_id(*position)
        return await self._emit(
            ActionResult(
                action="place_block",
                status=(
                    ActionStatus.CONFIRMED
                    if confirmed
                    else (ActionStatus.SENT if not await_confirm else ActionStatus.UNCONFIRMED)
                ),
                sequence=placed.sequence,
                position=position,
                state_before=state_before,
                state_after=state_after,
                packets=tuple(packets),
                elapsed=time.monotonic() - started,
                evidence=(
                    f"world state {state_before} -> {state_after}"
                    if confirmed
                    else f"no block update at {position} within {confirm_timeout:g}s"
                ),
                extra={"face": int(face), "clicked": clicked},
            )
        )

    def _action_position(self) -> tuple[int, int, int]:
        """The block position a ``PlayerAction`` has to name.

        Every ``PlayerAction`` carries a block position even when the action has
        nothing to do with blocks (dropping an item, swapping hands).  Vanilla
        sends whatever is being looked at, so this does the same and falls back
        to the block under the eyes.
        """

        hit = self.raycast()
        if hit is not None:
            return hit.position
        position = self.client.physics_state.position
        return (math.floor(position.x), math.floor(position.y), math.floor(position.z))

    async def _player_action(
        self,
        action: str,
        code: int,
        *,
        position: tuple[int, int, int] | None = None,
        face: BlockFace = BlockFace.UP,
    ) -> ActionResult:
        started = time.monotonic()
        position = self._action_position() if position is None else position
        packet_id = await self._send(
            "serverbound_player_action",
            action,
            PacketWriter()
            .write_varint(code)
            .write_position(*position)
            .write_unsigned_byte(int(face))
            .write_varint(self._sequence())
            .to_bytes(),
        )
        return await self._emit(
            ActionResult(
                action=action,
                status=ActionStatus.SENT,
                position=position,
                packets=(packet_id,),
                elapsed=time.monotonic() - started,
            )
        )

    async def release_use_item(
        self,
        position: tuple[int, int, int] | None = None,
        *,
        face: BlockFace = BlockFace.UP,
    ) -> ActionResult:
        """Stop using the held item (bow, food, shield) with ``PlayerAction``."""

        return await self._player_action(
            "release_use_item",
            self.profile.codes.release_use_item,
            position=position,
            face=face,
        )

    async def drop_item(
        self,
        position: tuple[int, int, int] | None = None,
        *,
        whole_stack: bool = False,
        face: BlockFace = BlockFace.UP,
    ) -> ActionResult:
        """Throw one item -- or the whole held stack -- on the ground.

        Vanilla has two codes for this (``DROP_ITEM`` and ``DROP_ALL_ITEMS``) and
        acts on the *held* slot, so select the slot first.  The drop itself is
        confirmed by the inventory, not by the action: there is no echo, so the
        result is ``SENT`` and the caller watches ``bot.player.inventory``.
        """

        return await self._player_action(
            "drop_stack" if whole_stack else "drop_item",
            self.profile.codes.drop_stack
            if whole_stack
            else self.profile.codes.drop_item,
            position=position,
            face=face,
        )

    async def swap_with_offhand(
        self,
        position: tuple[int, int, int] | None = None,
        *,
        face: BlockFace = BlockFace.UP,
    ) -> ActionResult:
        """Swap the held item with the off hand (the ``F`` key)."""

        return await self._player_action(
            "swap_offhand",
            self.profile.codes.swap_offhand,
            position=position,
            face=face,
        )

    # ------------------------------------------------------------------
    # convenience
    # ------------------------------------------------------------------
    async def dig_looked_at(
        self,
        *,
        max_distance: float = BLOCK_INTERACTION_RANGE,
        **kwargs,
    ) -> ActionResult:
        """Dig whatever the player is currently aiming at."""

        hit = self.raycast(max_distance=max_distance)
        if hit is None:
            return await self._emit(
                ActionResult(
                    action="dig",
                    status=ActionStatus.REJECTED,
                    elapsed=0.0,
                    evidence="nothing within reach",
                )
            )
        return await self.dig(hit.position, face=hit.face, aim=False, **kwargs)

    def break_time_estimate(
        self, state_id: int, *, hardness: float = 0.5, tool_factor: float = 1.0
    ) -> float:
        """Rough hold time in seconds for a block of the given hardness.

        Deliberately an estimate: the exact vanilla formula depends on tool
        tier, enchantments, water and haste.  It exists so a caller can size
        ``dig``'s ``hold`` instead of guessing.  :meth:`break_time` is the
        table-driven sibling that knows the real numbers for this release.
        """

        ticks = math.ceil(1.0 / (hardness * tool_factor / 30.0)) if hardness else 0
        return max(ticks, 1) * TICK_SECONDS

    # -- hardness and tools -------------------------------------------------

    def block_name(self, block: int | str) -> str:
        """Resolve a block state id (or a name) to a ``minecraft:`` identifier."""

        if isinstance(block, str):
            return block if ":" in block else f"minecraft:{block}"
        states = getattr(self.client.world, "block_states", None)
        state = None if states is None else states.get(int(block))
        name = getattr(state, "name", None)
        if not name:
            raise ActionError(
                f"block state {block} is not in this release's block table, so its "
                "hardness is unknown"
            )
        return name

    def break_time(
        self, block: int | str, *, tool: str | None | object = _UNSET
    ) -> float | None:
        """Seconds needed to break ``block`` with ``tool``, or ``None`` if unknown.

        ``tool`` defaults to whatever the player is holding (so the number
        answers "how long will *this* take"), and ``tool=None`` asks about a
        bare hand on purpose.
        """

        table = self.version.breaking
        if table is None:
            return None
        name = self.block_name(block)
        if tool is _UNSET:
            held = getattr(self.client, "held_item", None)
            tool = None if held is None or held.empty else held.identifier
        return table.seconds(name, tool=tool)  # type: ignore[arg-type]

    def _expected_break_time(self, state_id: int) -> float | None:
        """Predicted seconds to break a block state, or ``None`` if unknown.

        This is the number the vanilla client works out for itself before it
        decides when to send ``STOP``: the block's hardness at the progress the
        server will compute, with whatever is in hand.  Unknown states and
        missing tables return ``None`` so the caller can fall back to holding
        longer rather than guessing.
        """

        try:
            seconds = self.break_time(int(state_id))
        except (ActionError, ValueError):
            return None
        if seconds is None or seconds <= 0.0:
            return None
        return seconds

    def best_tool_for(self, block: int | str) -> tuple[int, str] | None:
        """Best hotbar tool for ``block`` as ``(inventory slot, identifier)``.

        Only the hotbar (slots 36..44) is considered, because switching to
        anything else would cost a container move first.
        """

        table = self.version.breaking
        if table is None:
            return None
        name = self.block_name(block)
        row = table.row(name)
        if row is None:
            return None
        kind = row.get("tool")
        if not kind:
            return None
        tier = int(row.get("tier", 0)) if row.get("requires") else 0
        slots: list[tuple[int, str]] = []
        for slot in range(36, 45):
            stack = self.client.get_inventory_item(slot)
            if stack is not None and not stack.empty and stack.identifier:
                slots.append((slot, stack.identifier))
        found = best_tool(slots, kind=kind, tier=tier)
        return None if found is None else (found[0], found[1])

    async def equip_tool(self, block: int | str) -> ActionResult:
        """Select the best hotbar tool for ``block`` (no-op when already held)."""

        name = self.block_name(block)
        best = self.best_tool_for(name)
        if best is None:
            return await self._emit(
                ActionResult(
                    action="equip_tool",
                    status=ActionStatus.REJECTED,
                    elapsed=0.0,
                    evidence=f"no tool in the hotbar is good enough for {name}",
                )
            )
        slot, identifier = best
        index = slot - 36
        if getattr(self.client, "selected_hotbar_slot", None) == index:
            return await self._emit(
                ActionResult(
                    action="equip_tool",
                    status=ActionStatus.SENT,
                    elapsed=0.0,
                    evidence=f"already holding {identifier}",
                )
            )
        await self.client.select_hotbar_slot(index)
        return await self._emit(
            ActionResult(
                action="equip_tool",
                status=ActionStatus.SENT,
                elapsed=0.0,
                evidence=f"selected {identifier} in hotbar slot {index}",
            )
        )
