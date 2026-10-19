"""Walking and flying to a coordinate, in pieces the server can follow.

A route is planned against the decoded world and executed one segment at a
time; each segment re-plans when the world disagrees with the plan.  Nothing
here writes a packet -- the movement mixin owns the wire.
"""

from __future__ import annotations

import asyncio
import copy
import math
from contextlib import suppress

from .log import debug as log_debug
from .navigation import (
    FlightPathfinder,
    NavigationPath,
    NavigationTimeout,
    PathNotFound,
    Pathfinder,
)
from .physics import MovementInput, PhysicsState, Vec3
from .support import _find_flight_path, _find_ground_path
from .world import World


class NavigatorMixin:
    """Methods of :class:`protobot.Bot`, grouped by concern."""

    async def navigate_to(
        self,
        x: float,
        z: float,
        *,
        y: float | None = None,
        tolerance: float = 0.3,
        sprint: bool = False,
        timeout: float = 60.0,
        max_nodes: int = 4096,
        max_drop: float = 3.0,
        replans: int = 3,
        tick_interval: float = 0.05,
        allow_diagonal: bool = True,
    ) -> PhysicsState:
        """Plan and execute a route using ordinary player input and physics.

        Server corrections or dynamic block changes can invalidate a route.
        Progress is monitored per waypoint and the route is replanned up to
        ``replans`` times before a :class:`NavigationTimeout` is raised.
        """

        self._require_play()
        if self._navigation_claimed:
            raise RuntimeError("another navigation task is already running")
        numeric = (x, z, tolerance, timeout, max_drop, tick_interval)
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("navigation coordinates and limits must be finite")
        if tolerance <= 0.0 or timeout <= 0.0 or max_drop <= 0.0:
            raise ValueError("navigation tolerances and timeouts must be positive")
        if tick_interval < 0.0:
            raise ValueError("tick_interval cannot be negative")
        if replans < 0:
            raise ValueError("replans cannot be negative")
        if y is not None and not math.isfinite(y):
            raise ValueError("navigation Y coordinate must be finite")
        self._navigation_claimed = True
        try:
            if not self.world_ready.is_set():
                await self.wait_world(timeout=timeout)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            for attempt in range(replans + 1):
                remaining = deadline - loop.time()
                if remaining <= 0.0:
                    break
                self._navigation_planning = True
                try:
                    path = await asyncio.wait_for(
                        self._plan_path_async(
                            x,
                            z,
                            y=y,
                            max_nodes=max_nodes,
                            max_drop=max_drop,
                            allow_diagonal=allow_diagonal,
                        ),
                        timeout=remaining,
                    )
                finally:
                    self._navigation_planning = False
                await self.events.emit("path", path, attempt)
                if await self._execute_path(path, deadline, tolerance, sprint, tick_interval):
                    state = self.physics_state
                    horizontal = math.hypot(x - state.position.x, z - state.position.z)
                    vertical = abs(y - state.position.y) if y is not None else 0.0
                    if horizontal <= tolerance and (y is None or vertical <= 0.55):
                        await self.tick(MovementInput(), _navigation_tick=True)
                        return self.physics_state

            state = self.physics_state
            distance = math.hypot(x - state.position.x, z - state.position.z)
            raise NavigationTimeout(
                f"navigate_to did not reach ({x:.3f}, {z:.3f}); "
                f"remaining horizontal distance is {distance:.3f} blocks"
            )
        finally:
            self._navigation_planning = False
            self._navigation_claimed = False

    async def walk_to(
        self,
        x: float,
        z: float,
        *,
        tolerance: float = 0.35,
        sprint: bool = False,
        timeout: float = 30.0,
    ) -> PhysicsState:
        """Walk in a straight line to an X/Z target using the physics engine.

        This intentionally is a small automation primitive, not a pathfinder;
        applications can build higher-level navigation on the same tick API.
        """

        self._require_play()
        if tolerance <= 0.0 or timeout <= 0.0:
            raise ValueError("tolerance and timeout must be positive")
        if not self.world_ready.is_set():
            await self.wait_world(timeout=timeout)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            state = self.physics_state
            dx, dz = x - state.position.x, z - state.position.z
            if dx * dx + dz * dz <= tolerance * tolerance:
                await self.tick(MovementInput(), send_input=True, send_position=True)
                return state
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("walk_to did not reach its target before timeout")
            state.yaw = math.degrees(math.atan2(-dx, dz))
            # ``tick`` carries the new heading in the same movement packet as
            # the step (see the ``turned`` branch there).  Sending a look packet
            # first would be a second movement packet in the same tick, and the
            # server would predict one step over two ticks.
            await self.tick(MovementInput(forward=1.0, sprint=sprint))

    async def fly_to(
        self,
        x: float,
        y: float,
        z: float,
        *,
        tolerance: float = 0.35,
        timeout: float = 180.0,
        max_nodes: int = 65536,
        replans: int = 2,
        tick_interval: float = 0.05,
        bypass_permission: bool = True,
        vclip: bool | None = None,
        vclip_up_limit: float | None = None,
        vclip_down_limit: float | None = None,
        keep_flying: bool = False,
        anti_kick: bool | None = None,
        anti_kick_interval: float | None = None,
        allow_diagonal: bool = True,
        force_flight: bool = True,
        realtime: bool = False,
        planning_horizon: float = 32.0,
        lookahead: bool = True,
        planning_timeout: float | None = None,
    ) -> PhysicsState:
        """Navigate to a 3D target using flight pathfinding.

        Flight navigation always enables only the local predictor and runs the
        normal vanilla movement-input path.  It never checks or changes the
        server abilities snapshot and never sends a serverbound abilities
        packet.  The ``force_flight`` argument is retained for API
        compatibility and is ignored.  ``timeout`` is the overall operation
        deadline.  By default the worker plans the complete route before
        movement starts, prioritizing route continuity over startup latency.
        ``realtime=True`` opts into rolling planning for worlds where the full
        route is not loaded.  ``planning_timeout`` bounds each worker-thread
        plan and defaults to ``min(timeout, 30.0)``.
        """

        self._require_play()
        if self._navigation_claimed:
            raise RuntimeError("another navigation task is already running")
        if not all(
            math.isfinite(value)
            for value in (x, y, z, tolerance, timeout, tick_interval)
        ):
            raise ValueError("flight navigation coordinates and limits must be finite")
        if tolerance <= 0.0 or timeout <= 0.0:
            raise ValueError("flight tolerance and timeout must be positive")
        if tick_interval < 0.0 or replans < 0:
            raise ValueError("flight tick interval and replans must be non-negative")
        if (
            not isinstance(max_nodes, int)
            or isinstance(max_nodes, bool)
            or max_nodes <= 0
        ):
            raise ValueError("max_nodes must be a positive integer")
        if not isinstance(bypass_permission, bool):
            raise TypeError("bypass_permission must be a bool")
        if not isinstance(keep_flying, bool):
            raise TypeError("keep_flying must be a bool")
        if not isinstance(force_flight, bool):
            raise TypeError("force_flight must be a bool")
        # This compatibility flag no longer selects a permission-aware path:
        # every fly_to call is local-only forced flight.
        force_flight = True
        if not isinstance(realtime, bool):
            raise TypeError("realtime must be a bool")
        if not isinstance(lookahead, bool):
            raise TypeError("lookahead must be a bool")
        if not math.isfinite(planning_horizon) or planning_horizon <= 0.0:
            raise ValueError("planning_horizon must be a finite positive number")
        if planning_timeout is None:
            planning_timeout = min(timeout, 30.0)
        if not math.isfinite(planning_timeout) or planning_timeout <= 0.0:
            raise ValueError("planning_timeout must be a finite positive number")
        if anti_kick is not None and not isinstance(anti_kick, bool):
            raise TypeError("anti_kick must be a bool or None")
        if anti_kick_interval is not None and (
            not math.isfinite(anti_kick_interval) or anti_kick_interval < 0.2
        ):
            raise ValueError("anti_kick_interval must be at least 0.2 seconds")
        self._navigation_claimed = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        force_started = False
        try:
            if anti_kick is not None or anti_kick_interval is not None:
                await self.set_anti_kick(
                    self.anti_kick if anti_kick is None else anti_kick,
                    interval=anti_kick_interval,
                )
            if not self.world_ready.is_set():
                remaining = deadline - loop.time()
                if remaining <= 0.0:
                    raise NavigationTimeout(
                        "flight navigation timed out before world data was ready"
                    )
                try:
                    await self.wait_world(timeout=remaining)
                except asyncio.TimeoutError as error:
                    raise NavigationTimeout(
                        "flight navigation timed out before world data was ready"
                    ) from error
            # Always use local-only flight.  Do not inspect allow_flying and do
            # not emit the abilities packet even when callers pass the legacy
            # force_flight=False/bypass_permission=False options.
            if not self._force_flight_active:
                self._force_flight_active = True
                self._force_flight_previous = self.physics_state.flying
                self._set_physics_flying(True)
                force_started = True
        except BaseException:
            if force_started:
                self._force_flight_active = False
                self._set_physics_flying(self._force_flight_previous)
            self._navigation_claimed = False
            raise
        attempt = 0
        pending_plan: asyncio.Task[NavigationPath] | None = None
        pending_plan_start: Vec3 | None = None

        async def clear_pending_plan() -> None:
            """Cancel and retrieve a speculative plan without masking navigation errors."""

            nonlocal pending_plan, pending_plan_start
            task = pending_plan
            pending_plan = None
            pending_plan_start = None
            if task is None:
                return
            if not task.done():
                task.cancel()
            # A completed lookahead may carry PathNotFound (or another worker
            # exception).  Retrieve it during cleanup so it cannot become an
            # unhandled task warning or replace the original navigation error.
            with suppress(asyncio.CancelledError, Exception):
                await task

        try:
            while deadline > loop.time():
                state = self.physics_state
                if pending_plan is not None and pending_plan_start is not None:
                    # Lookahead is speculative.  A server correction, a VClip
                    # response, or a dynamic collision update can move the bot
                    # away from the predicted segment endpoint.  Do not apply
                    # a stale plan when the deviation is larger than one body
                    # width; cancel it and calculate from the live position.
                    deviation = math.sqrt(
                        (state.position - pending_plan_start).length_squared
                    )
                    if deviation > max(1.0, state.width * 2.0):
                        await clear_pending_plan()
                remaining_distance = math.dist(
                    (state.position.x, state.position.y, state.position.z),
                    (x, y, z),
                )
                if remaining_distance <= tolerance:
                    return self.physics_state
                plan_x, plan_y, plan_z = x, y, z
                if realtime and remaining_distance > planning_horizon:
                    roll_target = self._flight_roll_target(
                        state.position,
                        Vec3(x, y, z),
                        planning_horizon,
                    )
                    plan_x, plan_y, plan_z = (
                        roll_target.x,
                        roll_target.y,
                        roll_target.z,
                    )
                self._navigation_planning = pending_plan is None
                try:
                    if pending_plan is None:
                        pending_plan = asyncio.create_task(
                            self._plan_flight_path_async(
                                plan_x,
                                plan_y,
                                plan_z,
                                max_nodes=max_nodes,
                                vclip=vclip,
                                vclip_up_limit=vclip_up_limit,
                                vclip_down_limit=vclip_down_limit,
                                allow_diagonal=allow_diagonal,
                                # A rolling segment must end outside a block;
                                # otherwise the next plan starts inside glass
                                # and repeats the same VClip boundary forever.
                                allow_blocked_target=not (
                                    realtime and remaining_distance > planning_horizon
                                ),
                                # Complete plans must never pretend unknown
                                # chunks are clear.  Rolling plans may advance
                                # into them so the server can stream more data.
                                allow_unknown_chunks=realtime,
                            ),
                            name="protobot-flight-plan",
                        )
                        pending_plan_start = state.position
                    remaining = deadline - loop.time()
                    if remaining <= 0.0:
                        raise NavigationTimeout(
                            "flight navigation deadline expired while planning"
                        )
                    deadline_limited_plan = remaining <= planning_timeout
                    plan_timeout = min(planning_timeout, remaining)
                    path = await asyncio.wait_for(
                        asyncio.shield(pending_plan),
                        timeout=plan_timeout,
                    )
                    pending_plan = None
                    pending_plan_start = None
                except (asyncio.TimeoutError, PathNotFound) as error:
                    await clear_pending_plan()
                    if isinstance(error, asyncio.TimeoutError):
                        if deadline_limited_plan or deadline - loop.time() <= 0.0:
                            raise NavigationTimeout(
                                "flight navigation deadline expired while planning"
                            ) from error
                        raise NavigationTimeout(
                            f"flight segment planning exceeded {planning_timeout:.3g} seconds"
                        ) from error
                    if not realtime or attempt >= replans:
                        raise
                    attempt += 1
                    await asyncio.sleep(min(0.25, max(0.0, deadline - loop.time())))
                    continue
                finally:
                    self._navigation_planning = False
                await self.events.emit("flight_path", path, attempt)
                operation_log = ",".join(path.operations[:32])
                if len(path.operations) > 32:
                    operation_log += ",..."
                log_debug(
                    f"[flight] planned {len(path.waypoints)} node(s): "
                    f"{operation_log or 'none'} "
                    f"cost={path.cost:.3f} explored={path.explored_nodes}"
                )

                # Start the next rolling plan before executing this one.  The
                # explicit start is the predicted end of the current segment,
                # so the worker never reads live mutable physics state.
                if realtime and lookahead and path.waypoints:
                    segment_end = path.waypoints[-1].position
                    remaining_after_segment = math.dist(
                        (segment_end.x, segment_end.y, segment_end.z),
                        (x, y, z),
                    )
                    if remaining_after_segment > tolerance:
                        next_target = self._flight_roll_target(
                            segment_end,
                            Vec3(x, y, z),
                            planning_horizon,
                        )
                        pending_plan = asyncio.create_task(
                            self._plan_flight_path_async(
                                next_target.x,
                                next_target.y,
                                next_target.z,
                                start=segment_end,
                                max_nodes=max_nodes,
                                vclip=vclip,
                                vclip_up_limit=vclip_up_limit,
                                vclip_down_limit=vclip_down_limit,
                                allow_diagonal=allow_diagonal,
                                allow_blocked_target=False,
                                allow_unknown_chunks=True,
                            ),
                            name="protobot-flight-lookahead",
                        )
                        pending_plan_start = segment_end
                if await self._execute_flight_path(
                    path,
                    deadline,
                    tolerance,
                    tick_interval,
                    force_flight=force_flight,
                ):
                    state = self.physics_state
                    if math.dist((state.position.x, state.position.y, state.position.z), (x, y, z)) <= tolerance:
                        return self.physics_state
                    if realtime:
                        continue
                await clear_pending_plan()
                attempt += 1
                if attempt > replans:
                    break
                await asyncio.sleep(min(0.1, max(0.0, deadline - loop.time())))
            state = self.physics_state
            raise NavigationTimeout(
                f"fly_to did not reach ({x:.3f}, {y:.3f}, {z:.3f}); "
                f"remaining distance is {math.dist((state.position.x, state.position.y, state.position.z), (x, y, z)):.3f} blocks"
            )
        finally:
            await clear_pending_plan()
            self._navigation_planning = False
            self._navigation_vclip_active = False
            self._navigation_vertical_active = False
            await self._stop_flying_after_navigation(
                keep_flying, force_flight=force_flight
            )
            self._navigation_claimed = False

    def plan_path(
        self,
        x: float,
        z: float,
        *,
        y: float | None = None,
        max_nodes: int = 4096,
        max_drop: float = 3.0,
        allow_diagonal: bool = True,
    ) -> NavigationPath:
        """Plan a collision-aware walking route through the decoded world."""

        self._require_play()
        if not self.world_ready.is_set():
            raise RuntimeError("path planning requires at least one decoded chunk")
        state = self.physics_state
        target_y = state.position.y if y is None else y
        planner = Pathfinder(
            self.world,
            player_width=state.width,
            player_height=state.height,
            step_height=self.physics._attribute(state, "step_height"),
            max_drop=max_drop,
            allow_diagonal=allow_diagonal,
        )
        return planner.find_path(
            state.position,
            Vec3(x, target_y, z),
            match_target_y=y is not None,
            max_nodes=max_nodes,
        )

    async def plan_path_async(
        self,
        x: float,
        z: float,
        *,
        y: float | None = None,
        max_nodes: int = 4096,
        max_drop: float = 3.0,
        allow_diagonal: bool = True,
    ) -> NavigationPath:
        """Plan a walking route in a worker thread without blocking I/O."""

        return await self._plan_path_async(
            x,
            z,
            y=y,
            max_nodes=max_nodes,
            max_drop=max_drop,
            allow_diagonal=allow_diagonal,
        )

    async def _plan_path_async(
        self,
        x: float,
        z: float,
        *,
        y: float | None,
        max_nodes: int,
        max_drop: float,
        allow_diagonal: bool = True,
    ) -> NavigationPath:
        self._require_play()
        if not self.world_ready.is_set():
            raise RuntimeError("path planning requires at least one decoded chunk")
        state = self.physics_state
        target_y = state.position.y if y is None else y
        return await asyncio.to_thread(
            _find_ground_path,
            self._navigation_world_snapshot(),
            state.position,
            Vec3(x, target_y, z),
            player_width=state.width,
            player_height=state.height,
            step_height=self.physics._attribute(state, "step_height"),
            max_drop=max_drop,
            allow_diagonal=allow_diagonal,
            max_nodes=max_nodes,
            match_target_y=y is not None,
        )

    def _navigation_world_snapshot(self, *, missing_chunks_solid: bool = True) -> World:
        """Copy loaded chunks so worker-thread pathfinding never reads live state."""

        snapshot = copy.copy(self.world)
        snapshot.chunks = {}
        for key, chunk in self.world.chunks.items():
            cloned = copy.copy(chunk)
            cloned.sections = list(chunk.sections)
            snapshot.chunks[key] = cloned
        snapshot.missing_chunks_solid = missing_chunks_solid
        snapshot.set_entity_collision_provider(None)
        return snapshot

    async def _execute_path(
        self,
        path: NavigationPath,
        deadline: float,
        tolerance: float,
        sprint: bool,
        tick_interval: float,
    ) -> bool:
        if self._navigation_active:
            raise RuntimeError("another navigation task is already running")
        self._navigation_active = True
        try:
            return await self._execute_path_unlocked(
                path, deadline, tolerance, sprint, tick_interval
            )
        finally:
            self._navigation_active = False

    async def _execute_path_unlocked(
        self,
        path: NavigationPath,
        deadline: float,
        tolerance: float,
        sprint: bool,
        tick_interval: float,
    ) -> bool:
        loop = asyncio.get_running_loop()
        for waypoint in path:
            best_distance = math.inf
            stagnant_ticks = 0
            while True:
                state = self.physics_state
                dx = waypoint.position.x - state.position.x
                dz = waypoint.position.z - state.position.z
                horizontal = math.hypot(dx, dz)
                vertical = abs(waypoint.position.y - state.position.y)
                if horizontal <= tolerance and vertical <= 0.55:
                    break
                if loop.time() >= deadline:
                    return False

                if horizontal > tolerance:
                    # Only set the heading here; ``tick`` carries it in the same
                    # movement packet as the step (one packet per tick, like a
                    # vanilla client).
                    state.yaw = math.degrees(math.atan2(-dx, dz))
                jump = (
                    waypoint.operation == "jump"
                    and state.position.y < waypoint.position.y - 0.05
                )
                await self.tick(
                    MovementInput(
                        forward=1.0 if horizontal > tolerance else 0.0,
                        jump=jump,
                        sprint=sprint,
                    ),
                    _navigation_tick=True,
                )

                distance = horizontal + vertical
                if distance < best_distance - 0.02:
                    best_distance = distance
                    stagnant_ticks = 0
                else:
                    stagnant_ticks += 1
                if stagnant_ticks >= 20:
                    return False
                await asyncio.sleep(tick_interval if tick_interval else 0)
        return True

    def plan_flight_path(
        self,
        x: float,
        y: float,
        z: float,
        *,
        max_nodes: int = 8192,
        allow_diagonal: bool = True,
        vclip: bool | None = None,
        vclip_up_limit: float | None = None,
        vclip_down_limit: float | None = None,
    ) -> NavigationPath:
        """Plan a collision-aware route through free air volume."""

        self._require_play()
        if not self.world_ready.is_set():
            raise RuntimeError("flight path planning requires at least one decoded chunk")
        self._validate_finite((x, y, z), "flight target")
        vclip = self.vclip if vclip is None else vclip
        vclip_up_limit = self.vclip_up_limit if vclip_up_limit is None else vclip_up_limit
        vclip_down_limit = self.vclip_down_limit if vclip_down_limit is None else vclip_down_limit
        state = self.physics_state
        planner = FlightPathfinder(
            self.world,
            player_width=state.width,
            player_height=state.height,
            allow_diagonal=allow_diagonal,
            vclip=vclip,
            vclip_up_limit=vclip_up_limit,
            vclip_down_limit=vclip_down_limit,
        )
        return planner.find_path(
            state.position,
            Vec3(x, y, z),
            max_nodes=max_nodes,
        )

    async def plan_flight_path_async(
        self,
        x: float,
        y: float,
        z: float,
        *,
        max_nodes: int = 8192,
        vclip: bool | None = None,
        vclip_up_limit: float | None = None,
        vclip_down_limit: float | None = None,
        allow_diagonal: bool = True,
    ) -> NavigationPath:
        """Plan a 3D flight route in a worker thread without blocking I/O."""

        return await self._plan_flight_path_async(
            x,
            y,
            z,
            start=None,
            max_nodes=max_nodes,
            vclip=vclip,
            vclip_up_limit=vclip_up_limit,
            vclip_down_limit=vclip_down_limit,
            allow_diagonal=allow_diagonal,
        )

    async def _plan_flight_path_async(
        self,
        x: float,
        y: float,
        z: float,
        *,
        start: Vec3 | None = None,
        max_nodes: int,
        vclip: bool | None,
        vclip_up_limit: float | None,
        vclip_down_limit: float | None,
        allow_diagonal: bool = True,
        allow_blocked_target: bool = True,
        allow_unknown_chunks: bool = False,
    ) -> NavigationPath:
        self._require_play()
        if not self.world_ready.is_set():
            raise RuntimeError("flight path planning requires at least one decoded chunk")
        self._validate_finite((x, y, z), "flight target")
        state = self.physics_state
        start_position = state.position if start is None else start
        if not all(
            math.isfinite(value)
            for value in (start_position.x, start_position.y, start_position.z)
        ):
            raise ValueError("flight start coordinates must be finite")
        return await asyncio.to_thread(
            _find_flight_path,
            self._navigation_world_snapshot(
                missing_chunks_solid=not allow_unknown_chunks
            ),
            start_position,
            Vec3(x, y, z),
            player_width=state.width,
            player_height=state.height,
            allow_diagonal=allow_diagonal,
            vclip=self.vclip if vclip is None else vclip,
            vclip_up_limit=self.vclip_up_limit if vclip_up_limit is None else vclip_up_limit,
            vclip_down_limit=self.vclip_down_limit if vclip_down_limit is None else vclip_down_limit,
            max_nodes=max_nodes,
            allow_blocked_target=allow_blocked_target,
        )

    def _flight_roll_target(self, start: Vec3, target: Vec3, horizon: float) -> Vec3:
        """Return a short rolling target, never farther than one horizon."""

        delta = target - start
        distance = math.sqrt(delta.length_squared)
        if distance <= horizon:
            return target
        return start + delta.scale(horizon / distance)

    async def _execute_flight_path(
        self,
        path: NavigationPath,
        deadline: float,
        tolerance: float,
        tick_interval: float,
        *,
        force_flight: bool = False,
    ) -> bool:
        if self._navigation_active:
            raise RuntimeError("another navigation task is already running")
        self._navigation_active = True
        previous_flying = self.physics_state.flying
        owns_force_state = force_flight and not self._force_flight_active
        if owns_force_state:
            self._force_flight_active = True
            self._set_physics_flying(True)
        try:
            return await self._execute_flight_path_unlocked(
                path, deadline, tolerance, tick_interval
            )
        finally:
            if owns_force_state:
                self._force_flight_active = False
                self._set_physics_flying(previous_flying)
            self._navigation_active = False

    async def _execute_flight_path_unlocked(
        self,
        path: NavigationPath,
        deadline: float,
        tolerance: float,
        tick_interval: float,
    ) -> bool:
        loop = asyncio.get_running_loop()
        waypoint_index = 0
        while waypoint_index < len(path.waypoints):
            waypoint = path.waypoints[waypoint_index]
            # Grid/clearance waypoints must be reached more precisely than the
            # final user target.  Accepting a 0.35-block error at a wall top can
            # leave the feet below the clearance plane, so the next horizontal
            # edge collides forever before VClip even starts.
            is_last_waypoint = waypoint_index == len(path.waypoints) - 1
            waypoint_tolerance = tolerance if is_last_waypoint else min(tolerance, 0.1)
            horizontal_tolerance = (
                tolerance
                if is_last_waypoint
                else max(waypoint_tolerance, self.physics_state.width / 2.0)
            )
            stagnant_ticks = 0
            best_distance = math.inf
            vertical_mode = 0
            initial_vertical_error = waypoint.position.y - self.physics_state.position.y
            vertical_direction = (
                1 if initial_vertical_error > 1.0e-7
                else (-1 if initial_vertical_error < -1.0e-7 else 0)
            )
            while True:
                state = self.physics_state
                delta = waypoint.position - state.position
                distance = math.sqrt(delta.length_squared)
                horizontal_distance = math.hypot(delta.x, delta.z)
                if vertical_direction > 0:
                    vertical_reached = -waypoint_tolerance <= delta.y <= 0.0
                elif vertical_direction < 0:
                    vertical_reached = 0.0 <= delta.y <= waypoint_tolerance
                else:
                    vertical_reached = abs(delta.y) <= waypoint_tolerance
                if (
                    waypoint.operation != "vclip"
                    and horizontal_distance <= horizontal_tolerance
                    and vertical_reached
                ):
                    break
                if loop.time() >= deadline:
                    return False
                if waypoint.operation == "vclip":
                    # VClip is a one-shot position action, not a normal
                    # physics waypoint.  Re-sending it every tick can pin the
                    # bot in the wall and lets anti-kick rewrite the packet Y
                    # into a ceiling.  Suspend synthetic anti-kick while the
                    # exact clip packet is sent, then let rolling planning
                    # validate the resulting server position.
                    self._navigation_vclip_active = True
                    try:
                        before = state.position
                        before_serial = self._position_update_serial
                        await self.send_position(
                            waypoint.position.x,
                            waypoint.position.y,
                            waypoint.position.z,
                            on_ground=False,
                            horizontal_collision=False,
                        )
                        await self.end_tick()
                    finally:
                        self._navigation_vclip_active = False
                    # Give the server one tick to accept/correct the clip before
                    # resuming normal physics and anti-kick packets.
                    log_debug(
                        f"[flight] execute vclip node "
                        f"X={waypoint.position.x:.2f} "
                        f"Y={waypoint.position.y:.2f} "
                        f"Z={waypoint.position.z:.2f}"
                    )
                    await asyncio.sleep(min(0.25, max(0.05, tick_interval)))
                    after = self.physics_state.position
                    if self._position_update_serial != before_serial:
                        # The server answered the clip.  A correction means
                        # the one-shot position was rejected; let realtime
                        # planning retry from the authoritative position.
                        if (
                            (after - waypoint.position).length_squared
                            > waypoint_tolerance * waypoint_tolerance
                        ):
                            return False
                    if (
                        (after - waypoint.position).length_squared
                        <= waypoint_tolerance * waypoint_tolerance
                    ):
                        break
                    if (before - after).length_squared <= 1.0e-6:
                        return False
                    continue
                # Re-aim every tick, but damp the requested vector near a
                # waypoint.  Full forward input while the predicted physics
                # state is still carrying momentum causes overshoot and the
                # old executor to alternate headings around the target.
                if horizontal_distance > 1.0e-5:
                    state.yaw = math.degrees(math.atan2(-delta.x, delta.z))
                vertical_error = delta.y
                vertical_deadband = max(0.04, waypoint_tolerance * 0.5)
                vertical_start = max(0.1, waypoint_tolerance)
                if vertical_mode > 0:
                    if vertical_error <= vertical_deadband:
                        vertical_mode = 0
                elif vertical_mode < 0:
                    if vertical_error >= -vertical_deadband:
                        vertical_mode = 0
                elif vertical_error > vertical_start:
                    vertical_mode = 1
                elif vertical_error < -vertical_start:
                    vertical_mode = -1
                self._navigation_vertical_active = (
                    waypoint.operation == "fly" and vertical_mode != 0
                )
                if vertical_mode == 0 and abs(vertical_error) <= vertical_start:
                    # Creative flight has vertical inertia.  Clearing it in
                    # the target band prevents alternating jump/sneak packets
                    # and leaves the bot level while it finishes horizontally.
                    state.velocity = Vec3(state.velocity.x, 0.0, state.velocity.z)
                if horizontal_distance <= horizontal_tolerance:
                    state.velocity = Vec3(0.0, state.velocity.y, 0.0)
                    speed_scale = 0.0
                else:
                    speed_scale = min(1.0, horizontal_distance / 2.0)
                controls = MovementInput(
                    forward=speed_scale,
                    strafe=0.0,
                    jump=vertical_mode > 0 and waypoint.operation == "fly",
                    sneak=vertical_mode < 0 and waypoint.operation == "fly",
                    sprint=True,
                )
                try:
                    await self.tick(controls, _navigation_tick=True)
                finally:
                    self._navigation_vertical_active = False
                if distance < best_distance - 0.01:
                    best_distance = distance
                    stagnant_ticks = 0
                else:
                    stagnant_ticks += 1
                if stagnant_ticks >= 30:
                    return False
                # ``send_raw`` can complete synchronously with an in-memory
                # transport (and often does when the socket write buffer is
                # empty).  Always yield once so the reader task can process
                # chunk data and position corrections, even at interval=0.
                await asyncio.sleep(tick_interval if tick_interval else 0)
            waypoint_index += 1
        return True

    async def _stop_flying_after_navigation(
        self,
        keep_flying: bool,
        *,
        force_flight: bool = False,
    ) -> None:
        """Leave creative flight off after a completed navigation task."""

        if force_flight:
            if self._force_flight_active:
                self._force_flight_active = False
                self._set_physics_flying(bool(keep_flying))
                velocity = self.physics_state.velocity
                self.physics_state.velocity = Vec3(velocity.x, 0.0, velocity.z)
                await self.send_input()
            return
        if keep_flying or not self.physics_state.flying or self.physics_state.spectator:
            return
        self._set_local_flying(False)
        await self._send_flying_state()

    async def navigate_flying_to(self, x: float, y: float, z: float, **kwargs) -> PhysicsState:  # type: ignore[no-untyped-def]
        """Alias for :meth:`fly_to`."""

        return await self.fly_to(x, y, z, **kwargs)

    async def navigate_to_flying(self, x: float, y: float, z: float, **kwargs) -> PhysicsState:  # type: ignore[no-untyped-def]
        """Alias for :meth:`fly_to` using the navigation naming convention."""

        return await self.fly_to(x, y, z, **kwargs)

    async def flight_to(self, x: float, y: float, z: float, **kwargs) -> PhysicsState:  # type: ignore[no-untyped-def]
        """Alias for :meth:`fly_to`."""

        return await self.fly_to(x, y, z, **kwargs)
