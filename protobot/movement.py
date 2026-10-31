"""Everything the client says about where the player is and where it looks.

The rule this module exists to protect: a vanilla client sends exactly one
movement packet per tick, and a rotation rides inside it.  Every sender here
goes through the same tick budget, which is what keeps the server's prediction
-- and any anti-cheat re-deriving it -- in step with us.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import math
import time

from .constants import (
    ANTI_KICK_WIRE_OFFSET,
    MAX_CATCH_UP_TICKS,
    TICK_DEBT_RESET_SECONDS,
    TICK_SECONDS,
    VANILLA_ROTATION_EPSILON_SQUARED,
)
from .log import debug as log_debug, trace as log_trace
from .physics import AABB, MovementInput, PhysicsState, Vec3
from .physics.math import f32, minecraft_cos, minecraft_sin
from .protocol.codec import PacketWriter
from .state import EntityState
from .support import (
    _START_FALL_FLYING_COMMAND,
    _START_SPRINTING_COMMAND,
    _STOP_SLEEPING_COMMAND,
    _STOP_SPRINTING_COMMAND,
    _TRACE_POS,
    _TRACE_SEND,
)


#: A wait shorter than this is bookkeeping noise, not a schedule: two floats
#: that should be equal can still leave a few attoseconds on the clock.
_SLEEP_EPSILON = 1e-6


def _now() -> float:
    """Monotonic seconds.  A seam so tests can drive the tick grid."""

    return time.monotonic()


async def _sleep(delay: float) -> None:
    """Sleep without blocking the loop.  A seam so tests can advance the grid."""

    await asyncio.sleep(delay)


class MovementMixin:
    """Methods of :class:`protobot.Bot`, grouped by concern."""

    async def tick(
        self,
        controls: MovementInput | None = None,
        *,
        send_input: bool = True,
        send_position: bool = True,
        _navigation_tick: bool = False,
    ) -> PhysicsState:
        """Advance the local player's 20 Hz tick grid.

        The body runs under a lock because one tick is one movement packet: a
        goal, an action and a caller's own loop can all ask for a tick, and two
        of them running at once would advance the physics twice and then report
        that as a single step, which the server reads as a position that does
        not match its prediction.

        A call advances one tick when it arrives on time.  When the caller is
        slightly late -- it awaited something short and the grid slipped by a
        slot or two -- the call runs the ticks whose slot has passed, so a
        knockback arc keeps its shape instead of being stretched by the await.

        The catch-up is deliberately shallow: ``MAX_CATCH_UP_TICKS``.  Vanilla
        drains up to ten per frame, but those bursts are measured against the
        wall clock by the anti-cheat (see the constant), and a library caller
        waiting on a chunk, a path plan or a chat round trip is not a 500 ms
        frame -- it is a client that was not running.  Debt wider than the
        allowance therefore drops and the grid re-anchors to now, which records
        that honestly instead of replaying seconds of stale input.
        """

        claimed = False
        async with self._tick_lock:
            state = self.physics_state
            for _ in range(self._ticks_due(send_input, send_position)):
                scheduled = self._next_tick_at
                state = await self._tick_once(
                    controls,
                    send_input=send_input,
                    send_position=send_position,
                    _navigation_tick=_navigation_tick,
                )
                self._last_tick_at = _now()
                self._ticks_completed += 1
                if self._next_tick_at == scheduled:
                    # This call did not claim a slot: the tick was a no-op
                    # (navigation owns the body, or nothing was sent), so
                    # catching up would only spin.
                    break
                claimed = True

        if not claimed and (send_input or send_position):
            # A no-op tick still owes the event loop a turn.  Without this an
            # ordinary ``while True: await bot.tick()`` loop keeps the loop for
            # itself -- ``asyncio.Lock`` is not a yield point when it is free --
            # and starves whatever owns the body: the navigation it is waiting
            # for, the reader task, the driver.
            #
            # The wait is taken *after* releasing ``_tick_lock`` on purpose.  An
            # earlier version waited inside the lock and hung scenario #23 for
            # minutes: whoever held the body (the navigation) needed a tick of
            # its own to finish, and that tick could not enter.
            if self._next_tick_at is None:
                await _sleep(TICK_SECONDS)
            else:
                await self.wait_tick_budget()

        return state

    def _ticks_due(self, send_input: bool, send_position: bool) -> int:
        """How many ticks are owed right now, the way vanilla counts them."""

        if not (send_input or send_position):
            return 1
        scheduled = self._next_tick_at
        if scheduled is None:
            return 1
        behind = _now() - scheduled
        if behind <= 0.0:
            return 1
        if behind > TICK_DEBT_RESET_SECONDS:
            # Wider than the anti-cheat lets a client spend (see
            # _begin_tick_packet, which re-anchors): one tick from now.
            return 1
        # Every slot that has arrived is owed, current one included.  Binary
        # division of the float clock can land a hair under the count (0.10 -
        # 0.05 is 0.0499...), which would lose the slot the caller is standing
        # in, so round up there.
        return min(MAX_CATCH_UP_TICKS, int(behind / TICK_SECONDS + 1e-9) + 1)

    async def _tick_once(
        self,
        controls: MovementInput | None = None,
        *,
        send_input: bool = True,
        send_position: bool = True,
        _navigation_tick: bool = False,
    ) -> PhysicsState:
        """The tick itself, called with ``_tick_lock`` held.

        The deterministic physics state is updated first, then the ordinary
        serverbound movement/input packets are emitted.  Callers that want to
        drive their own trajectory can pass ``send_position=False`` and use the
        resulting state as a prediction only.
        """

        self._require_play()
        await self._refresh_chat_certificate_if_due()
        controls = controls or MovementInput()
        if controls.sprint and controls.forward <= 0.0:
            # Vanilla drops out of sprint as soon as the forward impulse is
            # released, so a real client never reports "sprinting while standing
            # still".  The server keeps its own sprint flag from the START/STOP
            # SPRINTING commands and a movement prediction uses that flag
            # together with the input bits; leaving the flag set while the input
            # says "no forward" makes the two disagree by a few millimetres every
            # such tick, which is what ``navigate_to`` used to do whenever it
            # paused inside a waypoint's tolerance.
            controls = dataclasses.replace(controls, sprint=False)
        if controls.sprint and not self._can_sprint():
            # Vanilla's ``LocalPlayer.isSprintingPossible`` asks
            # ``Player.hasEnoughFoodToDoExhaustiveManoeuvres`` for a walking
            # player, which is ``FoodData.hasEnoughFood`` (``foodLevel > 6``) or
            # ``abilities.mayfly``.  Grim checks the same thing and reports
            # ``SprintA (hunger=6)``; more importantly the server will not
            # accelerate a hungry player, so a client that keeps the bit claims a
            # step 1.3 times too long and is set back on every single tick --
            # which looks like the bot walking into an invisible wall.
            controls = dataclasses.replace(controls, sprint=False)
        if (
            self._navigation_claimed
            or self._navigation_active
            or self._navigation_planning
        ) and not _navigation_tick:
            return self.physics_state
        if self.sleeping:
            # A sleeping client simulates nothing and reports nothing: the server
            # keeps the body on the bed (it teleported us to the bed pose) and
            # vanilla's own step ends with zero displacement, so ``sendPosition``
            # has nothing to say.  Claiming a position here would describe a fall
            # through the bed -- this library's block table approximates every
            # solid block as a full cube, so the bed reads as a floor one block
            # lower than it really is -- and the server's prediction flags the
            # offset every single tick.
            self.physics_state.velocity = Vec3()
            if send_position:
                # The tick still has to cost a slot: vanilla sends
                # ``client_tick_end`` once per game tick whether or not the body
                # moved (``Minecraft.tick`` sends it unconditionally), and a
                # loop that closed ticks faster than the clock would be flagged
                # as running the game too fast -- which is exactly what an
                # unpaced freeze did before this.
                await self._begin_tick_packet()
                await self.end_tick()
            return self.physics_state
        self._tick_remote_entity_metadata()
        if self.player.vehicle_id is not None:
            vehicle = self._controlled_root_vehicle()
            if vehicle is not None:
                self.boat_physics.tick(vehicle, controls, self.world)
            self.physics_state.velocity = Vec3()
            self.player.velocity_x = 0.0
            self.player.velocity_y = 0.0
            self.player.velocity_z = 0.0
            self._sync_local_riding_position()
            if send_input:
                await self._sync_input(
                    forward=controls.forward > 0,
                    backward=controls.forward < 0,
                    left=controls.strafe < 0,
                    right=controls.strafe > 0,
                    jump=controls.jump,
                    sneak=controls.sneak or controls.crawl,
                    sprint=controls.sprint,
                )
                self.physics_state.sprinting = controls.sprint
                await self._sync_sprinting(controls.sprint)
                if vehicle is not None:
                    await self._send_paddle_boat(vehicle)
            if send_position:
                await self.send_look(
                    self.physics_state.yaw,
                    self.physics_state.pitch,
                    on_ground=self.physics_state.on_ground,
                    horizontal_collision=self.physics_state.horizontal_collision,
                )
                if vehicle is not None:
                    await self._send_vehicle_movement(vehicle)
            await self.end_tick()
            return self.physics_state
        if (
            self.physics_state.spectator
            and self.player.abilities.allow_flying
            and not self.physics_state.flying
            and not self._force_flight_active
        ):
            # Spectator flight is locked on by ClientPlayerEntity before it
            # applies movement input.
            self._set_local_flying(True)
            await self._send_flying_state()
        was_flying = self.physics_state.flying
        if send_input or send_position:
            # Wait for this tick's slot *before* running the physics, not after
            # it.  A movement packet has to leave 50 ms after the previous one,
            # and that sleep used to sit between the physics step and the send:
            # an impulse that arrived during the sleep was written into the
            # local velocity, but the packet that followed still described the
            # pre-impulse step.  An anti-cheat sandwiches the knockback packet
            # between two transactions and expects the very next movement to
            # carry it, so that gap reads as "did not take the knockback".
            await self.wait_tick_budget()
            if send_position and self.version.protocol >= 777:
                # Close the previous tick here, before the snapshot, and not
                # from inside the packet write.  ``end_tick`` sends
                # ``client_tick_end`` and then flushes the pongs that were held
                # while an action owned the tick: both are awaits, so a
                # knockback could land inside them, i.e. after the pre-tick
                # snapshot was taken but before the packet describing that step
                # reached the wire.  The packet then left without the impulse
                # while the flushed pong had already told the anti-cheat that
                # the very next movement must carry it -- which is the exact
                # pattern ``AntiKB`` reports as an offset of one whole impulse
                # (0.538672 = |(0.4, 0.3608)|).  An aim that ships a bare look
                # packet (``look_at``, ``turn_to(tick=False)``) leaves the slot
                # claimed, so the driver's next tick is the one that takes this
                # branch: the window is not on a fixed schedule, which is why
                # only some ticks lost the knockback.
                if self._position_sent_this_tick:
                    await self.end_tick()
        position_serial = self._position_update_serial
        motion_epoch = self._motion_epoch
        state_before = self._snapshot_physics_state()
        await self._advance_physics(controls, was_flying)
        if send_input:
            # Input and sprint-state packets belong to the tick that is about to
            # be closed by the movement packet below.  Sending them inside that
            # same budget keeps them from sitting alone, where Grim's Post check
            # flags them as an "entity action" queued without a movement.
            input_values = {
                "forward": controls.forward > 0,
                "backward": controls.forward < 0,
                "left": controls.strafe < 0,
                "right": controls.strafe > 0,
                "jump": controls.jump,
                "sneak": controls.sneak or controls.crawl,
                "sprint": controls.sprint,
            }
            # Input packets are state-synchronized and deduplicated.  Forced
            # flight used to resend the same input every tick, adding 20
            # packets/sec without changing server state.
            await self._sync_input(**input_values)
            await self._sync_sprinting(self.physics_state.sprinting)
        if send_position:
            while self._motion_epoch != motion_epoch:
                # The server decided this player's velocity after the step
                # above -- a knockback.  Recompute the tick from the pre-tick
                # state so the packet below carries the impulse, which is what a
                # vanilla client does: it applies the velocity between ticks and
                # then moves.  Without this the packet describes a step that
                # ignored an impulse the server had already sent, and a
                # prediction check measures the difference as a displacement it
                # cannot explain.  The loop consumes one epoch per replay, so the
                # tripwire further down can run the same helper.
                motion_epoch = self._motion_epoch
                await self._replay_with_impulse(controls, was_flying, state_before)
            state = self.physics_state
            if self._position_update_serial != position_serial:
                # An authoritative teleport landed while this tick was being
                # sent.  The step above was computed from the pre-teleport
                # position, so claiming it would describe a jump the server
                # never made -- which is exactly what a movement prediction
                # check flags.  Drop this tick's movement: the next tick starts
                # from the teleported state, and the tick itself still ends.
                if _TRACE_SEND:
                    log_trace(
                        f"[tick] {time.monotonic():.3f} dropped movement"
                        " (teleport race)"
                    )
                self._trace_position(controls)
                # The tick that was supposed to send this packet is closed at the
                # start of every tick now, so there is normally nothing left to
                # close here; only a packet sent earlier in this same tick (a
                # look from an action) still needs its end marker.
                if self._position_sent_this_tick:
                    await self.end_tick()
                return self.physics_state
            if self._force_flight_active:
                # Forced flight is local-only.  Do not let the predictor's
                # landing flag switch the next tick back to walking, and do
                # not advertise an on-ground movement packet while flying.
                state.on_ground = False
                state.vertical_collision = False
            if self._motion_epoch != motion_epoch:
                # Tripwire.  Nothing between the epoch check above and this line
                # awaits, so this cannot fire today; it exists so that an await
                # added here later cannot silently ship a packet that ignores a
                # knockback -- exactly the bug the ordering above is built to
                # prevent.  Replaying is cheap and idempotent for one tick.
                motion_epoch = self._motion_epoch
                await self._replay_with_impulse(controls, was_flying, state_before)
            # A vanilla client sends exactly one movement packet per tick, and
            # the rotation rides in it when the view changed.  A separate Look
            # packet would be a second movement packet in the same tick: the
            # server then sees two ticks for one step of movement, which is what
            # a prediction check reports as an offset.
            turned = (
                abs((self.player.yaw - state.yaw + 180.0) % 360.0 - 180.0) > 1e-4
                or abs(self.player.pitch - state.pitch) > 1e-4
            )
            if turned or (self._force_flight_active and _navigation_tick):
                await self.send_position_and_rotation(
                    state.position.x,
                    state.position.y,
                    state.position.z,
                    state.yaw,
                    state.pitch,
                    on_ground=False if self._force_flight_active else state.on_ground,
                    horizontal_collision=state.horizontal_collision,
                )
            else:
                await self.send_position(
                    state.position.x,
                    state.position.y,
                    state.position.z,
                    on_ground=False if self._force_flight_active else state.on_ground,
                    horizontal_collision=state.horizontal_collision,
                )
            self.player.yaw, self.player.pitch = state.yaw, state.pitch
        self._trace_position(controls)
        self.player.pose = self.physics_state.pose
        self.player.crouching = self.physics_state.crouching
        self.player.swimming = self.physics_state.swimming
        self.player.gliding = self.physics_state.gliding
        if self.physics_state.gliding_collision_damage > 0.0:
            await self.events.emit(
                "gliding_collision",
                self.physics_state.gliding_collision_damage,
            )
        await self.end_tick()
        return self.physics_state

    async def _advance_physics(
        self,
        controls: MovementInput,
        was_flying: bool,
        *,
        announce_flying: bool = True,
    ) -> None:
        """Run one step of the vanilla physics and mirror it onto the player.

        Split out of :meth:`_tick_once` because a knockback can arrive between
        the step and the movement packet, and the step then has to be replayed
        from the pre-tick state (see ``_snapshot_physics_state``).
        """

        self.physics.tick(self.physics_state, controls, self.world)
        velocity = self.physics_state.velocity
        self.player.velocity_x = velocity.x
        self.player.velocity_y = velocity.y
        self.player.velocity_z = velocity.z
        if self.physics_state.flying != was_flying:
            if self._force_flight_active:
                # Vanilla physics can turn flight off after touching ground;
                # forced navigation keeps the local predictor airborne without
                # changing or synchronizing the server abilities flags.
                self._set_physics_flying(True)
            else:
                self.player.abilities.flying = self.physics_state.flying
                if announce_flying:
                    await self._send_flying_state()
        if self._force_flight_active:
            # A ground collision must not make the next navigation tick enter
            # the walking branch.  The server receives an airborne movement
            # flag below; keep the predictor in that same state as well.
            self._set_physics_flying(True)
            self.physics_state.on_ground = False

    async def _replay_with_impulse(
        self,
        controls: MovementInput,
        was_flying: bool,
        state_before: PhysicsState,
    ) -> None:
        """Recompute the step with an impulse that arrived after it.

        The snapshot is the state from before this tick ran, and the impulse is
        new information that arrived after the snapshot was taken, so it belongs
        to the replayed tick, not to the one being undone.  Both callers run this
        with no other await in flight, so the packet that follows describes the
        replay rather than the step it replaced.
        """

        impulse = self.physics_state.velocity
        self._restore_physics_state(state_before)
        self.physics_state.velocity = impulse
        await self._advance_physics(controls, was_flying, announce_flying=False)

    def _snapshot_physics_state(self) -> PhysicsState:
        """Copy the physics state so one step can be undone and replayed."""

        state = self.physics_state
        snapshot = copy.copy(state)
        snapshot.status_effects = copy.deepcopy(state.status_effects)
        snapshot.attribute_values = dict(state.attribute_values)
        return snapshot

    def _restore_physics_state(self, snapshot: PhysicsState) -> None:
        """Write a snapshot back into the live state, keeping its identity."""

        for field in dataclasses.fields(PhysicsState):
            setattr(self.physics_state, field.name, getattr(snapshot, field.name))

    async def send_position(
        self,
        x: float,
        y: float,
        z: float,
        *,
        on_ground: bool | None = None,
        horizontal_collision: bool | None = None,
    ) -> None:
        self._require_play()
        self._set_movement_flags(on_ground, horizontal_collision)
        self.player.x, self.player.y, self.player.z = x, y, z
        self.physics_state.position = Vec3(x, y, z)
        self._last_sent_position = (x, y, z)
        self.physics_state.on_ground = self.player.on_ground
        self.physics_state.horizontal_collision = self.player.horizontal_collision
        packet_y = self._anti_kick_wire_y(y)
        payload = (
            PacketWriter()
            .write_double(x)
            .write_double(packet_y)
            .write_double(z)
            .write_unsigned_byte(self._movement_flags())
            .to_bytes()
        )
        await self._send_position_packet(self.version.packets.serverbound_position, payload)

    async def send_position_and_rotation(
        self,
        x: float,
        y: float,
        z: float,
        yaw: float,
        pitch: float,
        *,
        on_ground: bool | None = None,
        horizontal_collision: bool | None = None,
    ) -> None:
        self._require_play()
        self._set_movement_flags(on_ground, horizontal_collision)
        yaw = self._wire_yaw(yaw)
        self.player.x, self.player.y, self.player.z = x, y, z
        self.player.yaw, self.player.pitch = yaw, pitch
        self.physics_state.position = Vec3(x, y, z)
        self._last_sent_position = (x, y, z)
        self.physics_state.yaw, self.physics_state.pitch = yaw, pitch
        self.physics_state.on_ground = self.player.on_ground
        self.physics_state.horizontal_collision = self.player.horizontal_collision
        packet_y = self._anti_kick_wire_y(y)
        payload = (
            PacketWriter()
            .write_double(x)
            .write_double(packet_y)
            .write_double(z)
            .write_float(yaw)
            .write_float(pitch)
            .write_unsigned_byte(self._movement_flags())
            .to_bytes()
        )
        await self._send_position_packet(
            self.version.packets.serverbound_position_look, payload
        )
        self._last_sent_yaw, self._last_sent_pitch = yaw, pitch

    async def wait_tick_budget(self) -> None:
        """Sleep until the next movement packet's slot has arrived.

        Actions use this to place a packet (an arm swing, a stop-digging) right
        before the movement packet that closes the tick, so anti-cheat never
        finds it queued alone when a transaction arrives.  It only *waits* for
        the slot; the tick claims it, so asking twice cannot push two movement
        packets into one slot.  When the grid is already behind, this returns at
        once and the tick that follows catches up.
        """

        scheduled = self._next_tick_at
        if scheduled is None:
            if not self._last_tick_packet_time:
                return
            scheduled = self._last_tick_packet_time + TICK_SECONDS
            self._next_tick_at = scheduled
        delay = scheduled - _now()
        if delay <= _SLEEP_EPSILON:
            return
        await _sleep(delay)

    async def _begin_tick_packet(self) -> None:
        """Start a movement tick the way a vanilla client does.

        A vanilla client emits exactly one movement packet per 20 Hz tick and
        closes that tick with ``client_tick_end`` (26.3+).  Aiming used to send
        a look packet straight after the previous tick's position packet, which
        is two ticks inside one real-time frame.  Grim's Timer check adds 50 ms
        per movement packet and flags once the balance runs ahead of real time,
        and its TickTimer check flags a tick that carries two movement packets.
        So close the previous tick and keep the packets on the grid.

        The grid is absolute, not relative: the next slot is the one this packet
        was scheduled for plus one tick, so a call that is already late sends
        immediately and hands the debt on, and ``tick`` can drain several slots
        in a row after a stall exactly like ``Minecraft.runTick``.  Only a gap
        too wide to be jitter re-anchors the grid, because replaying seconds of
        stale input would claim movement the server never saw.
        """

        if self.version.protocol >= 777 and self._position_sent_this_tick:
            await self.end_tick()
        now = _now()
        scheduled = self._next_tick_at
        if scheduled is None or now - scheduled > TICK_DEBT_RESET_SECONDS:
            scheduled = now
        delay = scheduled - now
        if delay > _SLEEP_EPSILON:
            await _sleep(delay)
            now = _now()
        self._next_tick_at = scheduled + TICK_SECONDS
        self._last_tick_packet_time = now
        self._position_sent_this_tick = True

    async def _send_position_packet(self, packet_id: int, payload: bytes) -> None:
        """Send a position-bearing movement packet within the tick budget.

        Whether the packet carries a position, a rotation or only the on-ground
        flag, it belongs to one movement tick: ``_begin_tick_packet`` closes the
        previous tick and keeps the 20 Hz cadence, exactly like a vanilla client
        that moves once per tick.
        """

        await self._begin_tick_packet()
        if _TRACE_SEND:
            state = self.physics_state
            log_trace(
                f"[pos] {time.time():.4f} y={state.position.y:.4f}"
                f" ground={int(bool(self.player.on_ground))}"
                f" vy={state.velocity.y:.4f}"
            )
        await self.send_raw(packet_id, payload)
        await self._release_pong_hold()

    async def _release_pong_hold(self) -> None:
        """Let the answers an action held back go now that its tick has moved.

        See the docstring in :meth:`Bot.tick` and Grim's ``Post`` check: the
        transaction an action waits for is the movement packet of its own tick.
        Once that packet is on the wire the queue it opens is cleared, so the
        answers can follow it instead of landing inside it.
        """

        if not self._pong_hold_until:
            return
        self._pong_hold_until = 0.0
        if self._pong_pending:
            await self._flush_pongs()

    async def send_vehicle_position_and_rotation(
        self,
        x: float,
        y: float,
        z: float,
        yaw: float,
        pitch: float,
        *,
        on_ground: bool | None = None,
    ) -> None:
        """Move the locally controlled root vehicle.

        This is the mounted counterpart to :meth:`send_position_and_rotation`.
        Vanilla accepts this packet only from the root vehicle's controlling
        passenger, so the high-level API rejects non-controlled mounts.
        """

        self._require_play()
        self._validate_finite((x, y, z, yaw, pitch), "vehicle movement values")
        yaw = self._wire_yaw(yaw)
        vehicle = self._controlled_root_vehicle()
        if vehicle is None:
            raise RuntimeError("the local player is not controlling a supported vehicle")
        vehicle.x, vehicle.y, vehicle.z = x, y, z
        vehicle.yaw, vehicle.pitch = f32(yaw), f32(pitch)
        if on_ground is not None:
            vehicle.on_ground = bool(on_ground)
        self._sync_local_riding_position()
        await self._send_vehicle_movement(vehicle)

    async def send_input(
        self,
        *,
        forward: bool = False,
        backward: bool = False,
        left: bool = False,
        right: bool = False,
        jump: bool = False,
        sneak: bool = False,
        sprint: bool = False,
    ) -> None:
        self._require_play()
        flags = self._input_flags(forward, backward, left, right, jump, sneak, sprint)
        await self.send_raw(
            self.version.packets.serverbound_player_input,
            PacketWriter().write_unsigned_byte(flags).to_bytes(),
        )
        self._last_sent_input_flags = flags

    def _can_sprint(self) -> bool:
        """Whether a vanilla client would allow the sprint flag right now.

        Only the conditions a client can decide on its own: enough food for the
        manoeuvre (``FoodData.hasEnoughFood``: food level above six) or the
        flight ability that bypasses it.  The rest of
        ``LocalPlayer.isSprintingPossible`` (a vehicle that can sprint, shallow
        water, mobility restriction) does not apply to a walking bot, and the
        wall case is handled by the physics itself.
        """

        return self.player.food > 6 or self.physics_state.allow_flying

    async def _sync_input(
        self,
        *,
        forward: bool,
        backward: bool,
        left: bool,
        right: bool,
        jump: bool,
        sneak: bool,
        sprint: bool,
    ) -> None:
        flags = self._input_flags(forward, backward, left, right, jump, sneak, sprint)
        if flags == self._last_sent_input_flags:
            return
        await self.send_input(
            forward=forward,
            backward=backward,
            left=left,
            right=right,
            jump=jump,
            sneak=sneak,
            sprint=sprint,
        )

    async def set_flying(
        self,
        enabled: bool,
        *,
        bypass_permission: bool = False,
    ) -> None:
        """Enable or disable abilities flight and synchronize it to the server.

        ``bypass_permission`` skips ProtoBot's local ``allow_flying`` guard.
        This is useful for custom servers/proxies that grant flight through a
        plugin but do not send the vanilla abilities flag.  The server remains
        authoritative and may reject the packet or correct the player.
        """

        self._require_play()
        if not isinstance(bypass_permission, bool):
            raise TypeError("bypass_permission must be a bool")
        if self._force_flight_active:
            # Forced navigation owns only the local predictor.  An external
            # cleanup call must not rewrite the server-provided abilities
            # snapshot or send a competing abilities packet.
            self._set_physics_flying(enabled)
            return
        abilities = self.player.abilities
        if (
            enabled
            and not abilities.allow_flying
            and not self.physics_state.spectator
            and not bypass_permission
        ):
            raise RuntimeError("the server has not granted flight permission")
        if not enabled and self.physics_state.spectator:
            raise RuntimeError("spectator flight is locked")
        # Forced navigation can leave the local predictor flying while the
        # server snapshot remains ``flying=False``.  In that state a request
        # to stop must still clear the predictor even though no abilities
        # packet is needed.
        if enabled == abilities.flying and enabled == self.physics_state.flying:
            return
        if enabled == abilities.flying:
            self._set_physics_flying(enabled)
            return
        self._set_local_flying(enabled)
        await self._send_flying_state()

    async def stop_flying(self) -> None:
        """Stop local/abilities flight, clear flight input, and resume gravity."""

        self._force_flight_active = False
        self._set_physics_flying(False)
        velocity = self.physics_state.velocity
        self.physics_state.velocity = Vec3(velocity.x, 0.0, velocity.z)
        await self.send_input()
        if self.player.abilities.flying:
            self.player.abilities.flying = False
            await self._send_flying_state()

    async def start_gliding(self) -> None:
        """Request fall-flying and start the local Elytra predictor.

        The server remains authoritative about whether an equipped item has a
        usable glider component. ProtoBot tracks the item identity but does not
        yet decode arbitrary positive component patches, so an explicit call is
        treated as the caller's assertion that the equipment is usable.
        """

        self._require_play()
        state = self.physics_state
        if state.gliding:
            return
        if state.flying:
            raise RuntimeError("abilities flight must be disabled before gliding")
        if state.on_ground:
            raise RuntimeError("gliding cannot start while on the ground")
        if state.in_water:
            raise RuntimeError("gliding cannot start while touching water")
        if state.status_effect("levitation") is not None:
            raise RuntimeError("gliding cannot start while levitating")
        entity_id = self.session.entity_id
        if entity_id is None:
            raise RuntimeError("gliding cannot start before play login")
        payload = (
            PacketWriter()
            .write_varint(entity_id)
            .write_varint(_START_FALL_FLYING_COMMAND)
            .write_varint(0)
            .to_bytes()
        )
        state.gliding = True
        self.player.gliding = True
        await self.send_raw(self.version.packets.serverbound_player_command, payload)

    def stop_gliding(self) -> None:
        """Stop local fall-flying prediction; vanilla has no stop-gliding packet."""

        self._require_play()
        self.physics_state.gliding = False
        self.physics_state.gliding_ticks = 0
        self.player.gliding = False

    def _set_local_flying(self, enabled: bool) -> None:
        self.player.abilities.flying = enabled
        self._set_physics_flying(enabled)

    def _set_physics_flying(self, enabled: bool) -> None:
        """Toggle only the local predictor's flight state.

        Forced navigation uses this instead of :meth:`_set_local_flying` so
        the server-provided abilities snapshot is left untouched.
        """

        self.physics_state.flying = enabled
        if enabled:
            self.physics_state.on_ground = False
            self.physics_state.vertical_collision = False
            self.player.on_ground = False
            self.physics_state.gliding = False
            self.physics_state.gliding_ticks = 0
            self.player.gliding = False
        else:
            self._anti_kick_last_packet_y = None
            self._next_anti_kick = 0.0
            self._navigation_vclip_active = False
            self._navigation_vertical_active = False

    def _anti_kick_wire_y(self, current_y: float) -> float:
        """Return Meteor-style packet anti-kick Y without changing local state.

        Meteor's Packet mode waits for its delay window, then rewrites the
        outgoing movement packet to ``lastPacketY - 0.03130`` whenever the
        current packet would otherwise be level or rising (or only falling by
        less than that threshold).  ProtoBot sends a position packet every
        physics tick, so rewriting the packet in-place gives the same wire
        behavior without injecting a second packet or moving the predictor.
        """

        if not self.anti_kick or not self.physics_state.flying:
            return current_y
        if self._navigation_vclip_active or self._navigation_vertical_active:
            # A VClip waypoint is intentionally inside a collision volume;
            # rewriting its Y would turn the one-shot clip into a repeated
            # collision/teleport loop.  The same applies to ordinary vertical
            # steering: anti-kick must not fight a deliberate ascent/descent.
            self._anti_kick_last_packet_y = current_y
            return current_y
        # Never send a synthetic Y that puts the player's body into a roof or
        # another collision box.  This is especially important when the bot is
        # flying immediately below a ceiling: Meteor's -0.03130 packet is only
        # useful in open air.
        half = self.physics_state.width / 2.0
        body = AABB(
            self.physics_state.position.x - half + 1.0e-7,
            current_y + 1.0e-7,
            self.physics_state.position.z - half + 1.0e-7,
            self.physics_state.position.x + half - 1.0e-7,
            current_y + self.physics_state.height - 1.0e-7,
            self.physics_state.position.z + half - 1.0e-7,
        )
        if not self.world.no_collision(body):
            self._anti_kick_last_packet_y = current_y
            return current_y
        now = asyncio.get_running_loop().time()
        if now < self._next_anti_kick:
            # Meteor tracks the last ordinary movement packet continuously,
            # not just when its 20-tick rewrite window opens.
            self._anti_kick_last_packet_y = current_y
            return current_y
        last_y = self._anti_kick_last_packet_y
        packet_y = current_y
        if last_y is None:
            self._anti_kick_last_packet_y = current_y
            # Meteor waits through its initial delay window before sending
            # the first synthetic downward packet.  Without this, the
            # previous zero deadline makes the second movement packet dip
            # immediately after flight starts, which can trigger strict
            # movement checks before the server has accepted the flight state.
            self._next_anti_kick = now + self.anti_kick_interval
        elif current_y >= last_y or last_y - current_y < ANTI_KICK_WIRE_OFFSET:
            packet_y = last_y - ANTI_KICK_WIRE_OFFSET
        else:
            self._anti_kick_last_packet_y = current_y
        candidate = body.move(0.0, packet_y - current_y, 0.0)
        if not self.world.no_collision(candidate):
            self._anti_kick_last_packet_y = current_y
            return current_y
        self._next_anti_kick = now + self.anti_kick_interval
        if packet_y != current_y:
            log_debug(
                f"[anti-kick] movement Y adjusted from {current_y:.5f} "
                f"to {packet_y:.5f} (interval {self.anti_kick_interval:.2f}s)"
            )
        return packet_y

    async def _send_flying_state(self) -> None:
        if self._force_flight_active:
            # Forced navigation deliberately keeps the server's abilities
            # snapshot untouched, even if another local state transition tries
            # to synchronize flight while the route is running.
            return
        flags = 0x02 if self.player.abilities.flying else 0x00
        await self.send_raw(
            self.version.packets.serverbound_player_abilities,
            PacketWriter().write_unsigned_byte(flags).to_bytes(),
        )

    async def _sync_sprinting(self, sprinting: bool) -> None:
        if sprinting == self._last_sent_sprinting:
            return
        entity_id = self.session.entity_id
        if entity_id is None:
            raise RuntimeError("sprint state cannot be synchronized before play login")
        command = _START_SPRINTING_COMMAND if sprinting else _STOP_SPRINTING_COMMAND
        payload = (
            PacketWriter()
            .write_varint(entity_id)
            .write_varint(command)
            .write_varint(0)
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_player_command, payload)
        self._last_sent_sprinting = sprinting

    async def leave_bed(self) -> None:
        """Get up from a bed, the way a vanilla client does.

        ``PlayerCommandAction.STOP_SLEEPING`` is the first ordinal in 26.3
        (``STOP_SLEEPING``, ``START_SPRINTING``, ``STOP_SPRINTING``, ...).  A
        sleeping player is frozen server-side, so this is the packet that hands
        control back: until it arrives every movement claim disagrees with the
        server's idea of where the body is.  Sending it when the player is not
        asleep is a no-op on the server.
        """

        entity_id = self.session.entity_id
        if entity_id is None:
            raise RuntimeError("a bed cannot be left before play login")
        payload = (
            PacketWriter()
            .write_varint(entity_id)
            .write_varint(_STOP_SLEEPING_COMMAND)
            .write_varint(0)
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_player_command, payload)

    def _wire_yaw(self, yaw: float) -> float:
        """``yaw`` adjusted so this packet differs from the last by a small angle.

        Yaw is modulo 360, and the physics engine, the navigation and the action
        layer set it independently, so the same direction can arrive as 30 or as
        390.  Two consecutive packets that disagree by about a full turn are a
        snap to the server and to anti-cheat (Grim's AimModulo360, "Sent a large
        yaw snap"), while the player turned by nothing at all.  Keeping every
        packet within half a turn of the previous one makes the wire say what
        really happened.
        """

        previous = self._last_sent_yaw
        if previous is None:
            return yaw
        return previous + ((yaw - previous + 180.0) % 360.0 - 180.0)

    def _rotation_unchanged(self, yaw: float, pitch: float) -> bool:
        """True when a vanilla client would not send a rotation packet at all.

        ``LocalPlayer.sendPosition`` only emits a look/posRot packet when
        ``dYaw^2 + dPitch^2 > 9.0E-6``; otherwise it falls back to a position or
        status packet.  Aiming twice at the same point therefore sends nothing,
        and Grim's AimDuplicateLook check flags exactly that duplicate.
        """

        if self._last_sent_yaw is None or self._last_sent_pitch is None:
            return False
        yaw_delta = yaw - self._last_sent_yaw
        pitch_delta = pitch - self._last_sent_pitch
        return (
            yaw_delta * yaw_delta + pitch_delta * pitch_delta
            <= VANILLA_ROTATION_EPSILON_SQUARED
        )

    async def end_tick(self) -> None:
        self._require_play()
        await self.send_raw(self.version.packets.serverbound_tick_end)
        self._position_sent_this_tick = False
        await self._flush_pongs()

    async def _flush_pongs(self) -> None:
        """Answer the pings that arrived while an action owned the tick.

        A pong is where an anti-cheat judges the packets queued ahead of it, and
        only a movement packet clears that queue.  Sending the answer the moment
        the ping arrives would therefore let it fall between an action's swing
        and the movement packet of the same tick, so the swing looks out of
        order.  The answers go out here, when the tick has just closed.
        """

        pending = self._pong_pending
        if not pending:
            return
        values = list(pending)
        pending.clear()
        for value in values:
            try:
                await self.send_raw(
                    self.version.packets.serverbound_pong,
                    PacketWriter().write_int(value).to_bytes(),
                )
            except Exception:  # noqa: BLE001 - the tick outlived the connection
                return

    async def close_tick(self) -> None:
        """End the movement tick if one is still open.

        Interactions are not movement packets, so they must not fall inside an
        open tick: 26.3 marks a tick's end with ``client_tick_end``, and a
        server (or an anti-cheat watching packet order) treats that marker as a
        transaction.  An action sent after the movement packet but before its
        end marker therefore looks like it arrived before the tick it belongs
        to.  Closing first leaves the action in the gap between ticks, where
        the next movement packet is what judges it.
        """

        if self.version.protocol >= 777 and self._position_sent_this_tick:
            await self.end_tick()

    async def send_look(
        self,
        yaw: float,
        pitch: float,
        *,
        on_ground: bool | None = None,
        horizontal_collision: bool | None = None,
    ) -> None:
        """Send a vanilla look-only movement packet and update local state."""

        self._require_play()
        self._set_movement_flags(on_ground, horizontal_collision)
        yaw = self._wire_yaw(yaw)
        self.player.yaw, self.player.pitch = yaw, pitch
        self.physics_state.yaw, self.physics_state.pitch = yaw, pitch
        if self._rotation_unchanged(yaw, pitch):
            # Vanilla sends nothing here (the server already has this rotation),
            # and repeating it is what Grim's AimDuplicateLook flags.
            return
        if self.anti_kick and self.physics_state.flying:
            # Meteor's packet mode converts rotation-only movement to a full
            # position+rotation packet so its Y rewrite can be applied.
            packet_y = self._anti_kick_wire_y(self.physics_state.position.y)
            payload = (
                PacketWriter()
                .write_double(self.physics_state.position.x)
                .write_double(packet_y)
                .write_double(self.physics_state.position.z)
                .write_float(yaw)
                .write_float(pitch)
                .write_unsigned_byte(self._movement_flags())
                .to_bytes()
            )
            await self._send_position_packet(
                self.version.packets.serverbound_position_look, payload
            )
            return
        payload = (
            PacketWriter()
            .write_float(yaw)
            .write_float(pitch)
            .write_unsigned_byte(self._movement_flags())
            .to_bytes()
        )
        await self._begin_tick_packet()
        await self.send_raw(self.version.packets.serverbound_look, payload)
        self._last_sent_yaw, self._last_sent_pitch = yaw, pitch

    async def send_status(
        self,
        *,
        on_ground: bool | None = None,
        horizontal_collision: bool | None = None,
    ) -> None:
        """Send the status-only movement packet used when position is unchanged."""

        self._require_play()
        self._set_movement_flags(on_ground, horizontal_collision)
        if self.anti_kick and self.physics_state.flying:
            # Likewise turn a status-only packet into a position packet.  This
            # mirrors Meteor's Packet mode interception for Flying packets.
            packet_y = self._anti_kick_wire_y(self.physics_state.position.y)
            payload = (
                PacketWriter()
                .write_double(self.physics_state.position.x)
                .write_double(packet_y)
                .write_double(self.physics_state.position.z)
                .write_unsigned_byte(self._movement_flags())
                .to_bytes()
            )
            await self._send_position_packet(
                self.version.packets.serverbound_position, payload
            )
            return
        await self._begin_tick_packet()
        await self.send_raw(
            self.version.packets.serverbound_flying,
            PacketWriter().write_unsigned_byte(self._movement_flags()).to_bytes(),
        )

    def _trace_position(self, controls: MovementInput | None = None) -> None:
        """Append this tick's movement to ``$PROTOBOT_TRACE_POS`` when set."""

        if not _TRACE_POS:
            return
        state = self.physics_state
        inputs = controls or MovementInput()
        line = (
            f"{time.time():.4f},{state.position.x:.4f},{state.position.y:.4f},"
            f"{state.position.z:.4f},{state.yaw:.2f},{state.pitch:.2f},"
            f"{int(state.on_ground)},{state.velocity.x:.4f},"
            f"{state.velocity.y:.4f},{state.velocity.z:.4f},"
            f"{int(inputs.forward > 0)},{int(inputs.strafe != 0)},"
            f"{int(bool(inputs.jump))},{int(bool(inputs.sprint))}\n"
        )
        try:
            with open(_TRACE_POS, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    def _movement_flags(self) -> int:
        on_ground = False if self._no_fall else self.player.on_ground
        return int(on_ground) | (int(self.player.horizontal_collision) << 1)

    @staticmethod
    def _input_flags(*values: bool) -> int:
        return sum((1 << bit) for bit, enabled in enumerate(values) if enabled)

    def _set_movement_flags(
        self,
        on_ground: bool | None,
        horizontal_collision: bool | None,
    ) -> None:
        """Keep the public player snapshot and predictor flags in lockstep."""

        if on_ground is not None:
            value = bool(on_ground)
            self.player.on_ground = value
            self.physics_state.on_ground = value
        if horizontal_collision is not None:
            value = bool(horizontal_collision)
            self.player.horizontal_collision = value
            self.physics_state.horizontal_collision = value

    def set_no_fall(self, enabled: bool) -> None:
        """Force the on-ground bit off in all outgoing movement packets.

        This is deliberately a wire-only setting: the local physics predictor
        keeps its real ``on_ground`` and fall-distance state, while the server
        receives ``on_ground=false`` for player and vehicle movement packets.
        """

        if not isinstance(enabled, bool):
            raise TypeError("no_fall enabled must be a bool")
        self._no_fall = enabled

    async def set_anti_kick(
        self,
        enabled: bool,
        *,
        interval: float | None = None,
    ) -> None:
        """Enable periodic flying heartbeats and local flight reassertion.

        This helps proxies/plugins that kick idle flying clients, but it cannot
        grant server-side ``mayfly`` permission or defeat server movement checks.
        """

        if not isinstance(enabled, bool):
            raise TypeError("anti_kick enabled must be a bool")
        if interval is not None:
            if not math.isfinite(interval) or interval < 0.2:
                raise ValueError("anti_kick interval must be at least 0.2 seconds")
            self.anti_kick_interval = interval
        self.anti_kick = enabled
        self._next_anti_kick = 0.0
        self._anti_kick_last_packet_y = None

    async def _send_vehicle_movement(self, vehicle: EntityState) -> None:
        payload = (
            PacketWriter()
            .write_double(vehicle.x)
            .write_double(vehicle.y)
            .write_double(vehicle.z)
            .write_float(vehicle.yaw)
            .write_float(vehicle.pitch)
            .write_bool(False if self._no_fall else vehicle.on_ground)
            .to_bytes()
        )
        await self._begin_tick_packet()
        await self.send_raw(self.version.packets.serverbound_move_vehicle, payload)

    async def _send_paddle_boat(self, vehicle: EntityState) -> None:
        payload = (
            PacketWriter()
            .write_bool(vehicle.boat_left_paddle)
            .write_bool(vehicle.boat_right_paddle)
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_paddle_boat, payload)

    def _sync_local_riding_position(self) -> bool:
        """Apply AbstractBoat.positionRider to the local player when possible."""

        local_id = self.session.entity_id
        vehicle_id = self.player.vehicle_id
        if local_id is None or vehicle_id is None:
            return False
        vehicle = self.entities.get(vehicle_id)
        if vehicle is None or vehicle.type_id is None:
            return False
        kind = self.version.boat_kind(vehicle.type_id)
        if kind is None:
            return False
        try:
            passenger_index = vehicle.passenger_ids.index(local_id)
        except ValueError:
            return False

        passenger_count = len(vehicle.passenger_ids)
        if kind.startswith("chest_"):
            longitudinal_offset = float(f32(0.15))
        elif passenger_count > 1:
            longitudinal_offset = float(f32(0.2 if passenger_index == 0 else -0.6))
        else:
            longitudinal_offset = 0.0

        if kind.endswith("raft"):
            ride_height = float(f32(f32(0.5625) * f32(0.8888889)))
        else:
            ride_height = float(f32(f32(0.5625) / f32(3.0)))
        radians = f32(f32(-vehicle.yaw) * f32(f32(math.pi) / f32(180.0)))
        sin_yaw = minecraft_sin(radians)
        cos_yaw = minecraft_cos(radians)
        x = vehicle.x + longitudinal_offset * sin_yaw
        y = vehicle.y + ride_height - 0.6
        z = vehicle.z + longitudinal_offset * cos_yaw

        delta_yaw = f32((f32(self.player.yaw - vehicle.yaw) + 180.0) % 360.0 - 180.0)
        clamped_yaw = f32(max(-105.0, min(105.0, delta_yaw)))
        yaw = f32(self.player.yaw + clamped_yaw - delta_yaw)
        self.player.x, self.player.y, self.player.z = x, y, z
        self.player.yaw = yaw
        self.physics_state.position = Vec3(x, y, z)
        self.physics_state.velocity = Vec3()
        self.physics_state.yaw = yaw
        return True
