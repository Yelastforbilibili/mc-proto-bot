"""The tick driver: a 20 Hz pump the library runs for you.

A :class:`~protobot.Bot` only moves when something ticks it, and ``tick`` sends
exactly one movement packet.  That is the right primitive -- it keeps the wire
deterministic -- but it makes the caller responsible for the cadence: a script
that awaits something slow stops sending packets, freezes its own physics while
the server keeps simulating, and comes back to a world that moved without it.

This module owns the other half: :meth:`DriverMixin.start_driver` runs an
``asyncio`` task that ticks on the same absolute grid as everybody else, so the
packet stream stays at 20 Hz while the caller does its own waiting.  The driver
is a *pump*, not an owner: it yields whenever somebody else ticked the current
slot or an action has reserved the tick (see ``_action_busy_until``), so
explicit ``await bot.tick(...)`` loops, actions and navigation keep working
unchanged with the driver running underneath them.

What it changes for a caller::

    async with bot.driving():
        bot.input = MovementInput(forward=1.0)   # held every driven tick
        await asyncio.sleep(3.0)                 # the bot actually walks

Without a driver that ``sleep`` would be three seconds of a frozen client.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from .constants import TICK_SECONDS
from .movement import _now, _sleep
from .physics import MovementInput, PhysicsState
from .protocol.connection import ConnectionState

__all__ = ["DriverMixin"]

#: A tick inside this window of the last one is not the driver's to send.  The
#: margin is below ``TICK_SECONDS`` so a caller ticking a few milliseconds early
#: still owns its slot instead of racing the pump for it.
DRIVER_GUARD_SECONDS = TICK_SECONDS * 0.9

#: How often :meth:`DriverMixin.wait_ticks` looks at the tick counter.  Short
#: compared with a tick, long enough not to spin a core.
WAIT_POLL_SECONDS = 0.005


class DriverMixin:
    """Methods of :class:`protobot.Bot`, grouped by concern."""

    @property
    def driving_active(self) -> bool:
        """Whether the 20 Hz driver task is running right now."""

        task = self._driver_task
        return task is not None and not task.done()

    @property
    def driver_error(self) -> BaseException | None:
        """The exception that stopped the driver, if one did.

        The driver runs outside any caller's ``await``, so an exception there
        cannot propagate to whoever started it.  It is kept here (and emitted as
        ``"driver_error"``) so a caller can tell a stopped pump from a broken
        server instead of silently standing still.
        """

        return self._driver_error

    async def start_driver(self) -> None:
        """Start the 20 Hz tick pump.

        The driver sends a tick whenever no other tick claimed the current slot,
        using :attr:`input` as the controls.  It is idempotent: starting an
        already running driver does nothing.
        """

        if self.state is not ConnectionState.PLAY:
            raise RuntimeError(
                "the tick driver needs an established play session;"
                f" the bot is in {self.state.name}"
            )
        if self.driving_active:
            return
        self._driver_error = None
        self._driver_task = asyncio.get_running_loop().create_task(
            self._drive(), name=f"protobot-driver-{self.username}"
        )

    async def stop_driver(self) -> None:
        """Stop the tick pump, if it is running."""

        task, self._driver_task = self._driver_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @asynccontextmanager
    async def driving(self) -> AsyncIterator[DriverMixin]:
        """Run the tick pump for the duration of the ``async with`` block.

        Equivalent to :meth:`start_driver` / :meth:`stop_driver`, with the stop
        guaranteed even when the body raises.
        """

        await self.start_driver()
        try:
            yield self
        finally:
            await self.stop_driver()

    async def wait_ticks(
        self, count: int = 1, *, timeout: float | None = None
    ) -> PhysicsState:
        """Wait until ``count`` more ticks have run, then return the state.

        This is what a caller uses instead of ``asyncio.sleep`` while the driver
        runs: it waits for *game* ticks rather than wall-clock time, so a slow
        machine does not turn a ten-tick knockback arc into a mistimed one.
        """

        if count <= 0:
            return self.physics_state
        target = self._ticks_completed + count
        deadline = None if timeout is None else _now() + timeout
        while self._ticks_completed < target:
            if deadline is not None and _now() >= deadline:
                raise TimeoutError(
                    f"waited {timeout:.3f}s for {count} ticks,"
                    f" only {self._ticks_completed - (target - count)} ran"
                )
            if not self.driving_active or self._driver_error is not None:
                # Nothing can deliver the tick, so waiting is not a slow server
                # but a stopped pump: say which, instead of hanging the caller
                # for as long as its own timeout allows.
                raise RuntimeError(
                    "the tick driver stopped before the expected ticks arrived"
                    f" ({self.driver_error!r})"
                )
            await asyncio.sleep(WAIT_POLL_SECONDS)
        return self.physics_state

    def set_steer(self, steer: Callable[[], None] | None) -> None:
        """Aim the next tick from wherever it is about to start.

        The driver holds one input for every slot, so the walker cannot nudge the
        heading between ticks without racing it.  This callback runs immediately
        before the driver sends a tick, which is exactly where a vanilla client
        turns: the rotation rides in that tick's movement packet.
        """

        self._steer = steer

    def clear_steer(self) -> None:
        """Stop steering; the driver holds :attr:`input` unchanged."""

        self._steer = None

    async def advance(
        self, controls: MovementInput | None = None, *, ticks: int = 1
    ) -> PhysicsState:
        """Advance the world by ``ticks`` ticks, whoever is sending them.

        This is the one call that expresses "let the game move": with a driver
        running it holds ``controls`` on :attr:`input` for the ticks it waits for
        and then puts the previous controls back, so a walk, a fall or a knockback
        arc is measured in ticks instead of in wall-clock sleeps.  Without a
        driver it is exactly ``await self.tick(controls)``, repeated, so callers
        that manage their own loop keep working unchanged.

        Restoring the input is what keeps a slow caller honest: the pump keeps
        ticking at 20 Hz while the caller plans a path or waits for a chunk, and a
        control left behind would walk the bot that whole time.

        ``controls=None`` leaves the held input alone (and, without a driver,
        ticks with no input as before).  Anything that owns the wire for a tick --
        the action layer's click/dig/place windows -- keeps calling :meth:`tick`
        directly, because the driver yields those slots to it on purpose.
        """

        if self.driving_active:
            previous = self.input
            if controls is not None:
                self.input = controls
            try:
                if ticks > 0:
                    # Bounded on purpose: a pump that is alive but never claims a
                    # slot (an action window left open, a stuck driver loop) must
                    # surface as an error here instead of hanging the caller until
                    # its own timeout, which would look like a server problem.
                    await self.wait_ticks(
                        ticks, timeout=max(1.0, ticks * TICK_SECONDS * 40)
                    )
            finally:
                self.input = previous
            return self.physics_state
        state = self.physics_state
        for _ in range(max(1, ticks)):
            # A tick taken while navigation owns the body still has to advance the
            # physics: without this flag _tick_once sees the navigation claim and
            # returns early, which would spin the loop without moving at all.
            state = await self.tick(
                controls, _navigation_tick=bool(self._navigation_claimed)
            )
        return state

    async def _drive(self) -> None:
        """The pump itself: one tick per grid slot, unless someone beat us to it."""

        try:
            while True:
                await self._sleep_until_slot()
                if self._driver_yields():
                    # Somebody else owns this slot: an explicit tick just ran, or
                    # an action has reserved the tick for its own packets.  Give
                    # them a whole slot and look again.
                    await _sleep(TICK_SECONDS)
                    continue
                steer = self._steer
                if steer is not None:
                    # Aim from the position this tick starts at, so the rotation
                    # and the step describe the same instant.
                    steer()
                await self.tick(
                    self.input, _navigation_tick=bool(self._navigation_claimed)
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead pump must be visible
            self._driver_error = exc
            with contextlib.suppress(Exception):
                await self.events.emit("driver_error", exc)

    async def _sleep_until_slot(self) -> None:
        """Sleep until the next tick is due on the shared grid."""

        scheduled = self._next_tick_at
        if scheduled is None:
            return
        delay = scheduled - _now()
        if delay > 0.0:
            await _sleep(delay)

    def _driver_yields(self) -> bool:
        """Whether the driver must let this slot go to somebody else."""

        now = _now()
        if now < self._action_busy_until:
            # An action is between its own packet and the movement packet of the
            # same tick; a driver tick here would close that tick early and the
            # action's packet would land on the wrong side of the movement.
            return True
        if now - self._last_tick_at < DRIVER_GUARD_SECONDS:
            return True
        return bool(self._position_sent_this_tick)
