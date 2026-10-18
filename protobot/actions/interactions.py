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

from ..physics import Vec3
from ..protocol.codec import PacketWriter
from ..protocol.entities import DEFAULT_HEIGHT, DEFAULT_WIDTH
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

#: One client tick at 20 Hz, used to pace the hold-and-wait loops.
TICK_SECONDS = 0.05

_HANDS = {"main_hand": 0, "main": 0, "off_hand": 1, "off": 1}


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


class Actions:
    """Interaction primitives bound to one connected client.

    Reached through ``client.actions``; constructing one directly is fine too
    and lets callers keep a stable handle across reconnects of the same client
    object.
    """

    def __init__(self, client: "Bot") -> None:
        self.client = client

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
        await self.client.send_look(yaw, pitch)
        return yaw, pitch

    def raycast(
        self,
        *,
        max_distance: float = 4.5,
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
        max_distance: float = 3.0,
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
        self, *, max_distance: float = 3.0, swing: bool = True
    ) -> ActionResult | None:
        """Aim at the entity under the crosshair and hit it.

        Returns ``None`` when the crosshair is not on an entity, so a caller
        can tell "nothing there" from "not sent".
        """

        hit = self.raycast_entity(max_distance=max_distance)
        if hit is None:
            return None
        await self.look_at(hit.center)
        result = await self.attack(hit.entity_id, swing=swing)
        return replace(
            result,
            evidence=f"hit entity {hit.entity_id} at {hit.distance:.2f} blocks",
        )

    async def _settle(
        self,
        until,
        *,
        seconds: float,
        tick: bool,
    ) -> bool:
        """Tick the client until ``until()`` is true or ``seconds`` elapse.

        Ticking matters: a dig only makes progress while the player keeps
        sending movement packets, and the receive loop needs wall-clock time to
        deliver the server's answer.
        """

        deadline = time.monotonic() + seconds
        while True:
            if until():
                return True
            if time.monotonic() >= deadline:
                return False
            if tick:
                await self.client.tick()
            await asyncio.sleep(TICK_SECONDS)

    # ------------------------------------------------------------------
    # arm / entity actions
    # ------------------------------------------------------------------
    async def swing(self, *, hand: str | int = "main_hand") -> ActionResult:
        """Send one arm swing.

        26.3 replaced ``ServerboundSwingPacket`` (one ``InteractionHand``) with
        ``ServerboundPunchPacket``, which has no fields at all; the audited
        profile says which of the two this release speaks.
        """

        started = time.monotonic()
        payload = (
            b""
            if self.profile.swing_is_empty
            else PacketWriter().write_varint(_hand(hand)).to_bytes()
        )
        packet_id = await self._send("serverbound_swing", "swing", payload)
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

    async def attack(self, entity_id: int, *, swing: bool = True) -> ActionResult:
        """Attack an entity.

        26.1 split the attack out of ``Interact`` into ``ServerboundAttackPacket``;
        1.21.x sends ``Interact`` with type ``ATTACK``.  The audited profile says
        which one this release has, so the same call works on both.
        """

        started = time.monotonic()
        packets: list[int] = []
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
    # block actions
    # ------------------------------------------------------------------
    async def dig(
        self,
        position: tuple[int, int, int],
        *,
        face: BlockFace | None = None,
        aim: bool = True,
        tick: bool = True,
        hold: float = 2.0,
        await_confirm: bool = True,
        confirm_timeout: float = 0.75,
        send_stop: bool = True,
    ) -> ActionResult:
        """Break the block at ``position``, the way a held mouse button does.

        The sequence is the vanilla one: aim, ``PlayerAction`` ``START``, keep
        the client ticking while the server accumulates progress, then
        ``STOP``.  Confirmation is the world state at ``position`` changing --
        which is exactly the assertion a test wants ("the block that was there
        is not there any more").

        ``hold`` is how long the button stays down, and it is not an optional
        nicety: the server accumulates destroy progress per tick but only
        breaks the block when it sees ``STOP`` (``ServerPlayerGameMode``:
        ``progress >= 0.7`` -> ``destroyAndAck``).  A block that needs ~11
        ticks (dirt by hand) breaks in ~0.6s, stone by hand needs ~1.6s;
        :meth:`break_time_estimate` sizes it.  Holding longer is harmless,
        holding too short silently does nothing, which is why the default errs
        long.  ``confirm_timeout`` is the extra window for the block update to
        arrive after ``STOP``.
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

        hit_face = face if face is not None else BlockFace.UP
        if aim:
            await self.look_at(_block_point(position, hit_face))

        codes = self.profile.codes
        sequence = self._sequence()
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

        broke = await self._settle(changed, seconds=hold, tick=tick)

        if send_stop:
            packets.append(
                await self._send(
                    "serverbound_player_action",
                    "dig",
                    PacketWriter()
                    .write_varint(codes.stop)
                    .write_position(x, y, z)
                    .write_unsigned_byte(int(hit_face))
                    .write_varint(self._sequence())
                    .to_bytes(),
                )
            )
        if await_confirm and not broke:
            broke = await self._settle(changed, seconds=confirm_timeout, tick=False)

        state_after = world.get_state_id(x, y, z)
        if broke:
            status = ActionStatus.CONFIRMED
            evidence = f"world state {state_before} -> {state_after}"
        else:
            status = ActionStatus.UNCONFIRMED
            evidence = (
                f"no block update at {position} within "
                f"{hold:g}s + {confirm_timeout:g}s"
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
                extra={"face": int(hit_face)},
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
    ) -> ActionResult:
        """Right click the ``face`` of ``position`` (``ServerboundUseItemOnPacket``).

        This is the raw packet: the held item is used *against* that face, so a
        block item lands in the neighbouring voxel.  :meth:`place_block` is the
        version that takes the destination instead.
        """

        started = time.monotonic()
        sequence = self._sequence()
        if cursor is None:
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
        packet_id = await self._send("serverbound_use_item_on", "use_item_on", payload)
        return await self._emit(
            ActionResult(
                action="use_item_on",
                status=ActionStatus.SENT,
                sequence=sequence,
                position=position,
                packets=(packet_id,),
                elapsed=time.monotonic() - started,
                extra={"face": int(face), "cursor": cursor},
            )
        )

    async def place_block(
        self,
        position: tuple[int, int, int],
        *,
        face: BlockFace = BlockFace.UP,
        hand: str | int = "main_hand",
        aim: bool = True,
        tick: bool = True,
        await_confirm: bool = True,
        confirm_timeout: float = 0.75,
    ) -> ActionResult:
        """Place the held block *into* ``position``.

        ``face`` names the side of the destination that touches the block you
        click: ``BlockFace.UP`` means "place on top of the block below", i.e.
        the clicked block is ``position - UP.step``.  Confirmation is the world
        state at ``position`` leaving air.
        """

        started = time.monotonic()
        world = self.client.world
        dx, dy, dz = face.step
        clicked = (position[0] - dx, position[1] - dy, position[2] - dz)
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
        if aim:
            await self.look_at(
                Vec3(
                    clicked[0] + 0.5 + dx * 0.5,
                    clicked[1] + 0.5 + dy * 0.5,
                    clicked[2] + 0.5 + dz * 0.5,
                )
            )
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

    async def release_use_item(
        self, position: tuple[int, int, int], *, face: BlockFace = BlockFace.UP
    ) -> ActionResult:
        """Stop using the held item (bow, food, shield) with ``PlayerAction``."""

        started = time.monotonic()
        packet_id = await self._send(
            "serverbound_player_action",
            "release_use_item",
            PacketWriter()
            .write_varint(self.profile.codes.release_use_item)
            .write_position(*position)
            .write_unsigned_byte(int(face))
            .write_varint(self._sequence())
            .to_bytes(),
        )
        return await self._emit(
            ActionResult(
                action="release_use_item",
                status=ActionStatus.SENT,
                position=position,
                packets=(packet_id,),
                elapsed=time.monotonic() - started,
            )
        )

    # ------------------------------------------------------------------
    # convenience
    # ------------------------------------------------------------------
    async def dig_looked_at(
        self,
        *,
        max_distance: float = 4.5,
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
        ``dig``'s ``hold`` instead of guessing.
        """

        ticks = math.ceil(1.0 / (hardness * tool_factor / 30.0)) if hardness else 0
        return max(ticks, 1) * TICK_SECONDS
