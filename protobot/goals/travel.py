"""Travel goals: segmented walking that waits for the world to arrive.

A bot only knows the terrain the server has sent it.  Walking straight at a
distant target therefore means walking into unknown chunks, where every block
looks like air to the pathfinder.  These goals move in segments instead: they
aim for the farthest loaded point toward the target, let the server stream the
next chunks in, and only then continue -- with a watchdog that fails instead of
walking into a wall forever.
"""

from __future__ import annotations

import asyncio
import math

from ..navigation import NavigationTimeout, PathNotFound
from .base import Goal, GoalError, GoalStatus

DEFAULT_TOLERANCE = 1.0
DEFAULT_TIMEOUT = 120.0
SEGMENT_SECONDS = 20.0
POLL_SECONDS = 0.05
FRONTIER_STEP = 4.0
FRONTIER_LIMIT = 64.0


def _chunk_of(value: float) -> int:
    return int(math.floor(value)) >> 4


def _chunk_loaded(bot, x: float, z: float) -> bool:
    """Whether the chunk holding ``(x, z)`` has been decoded."""

    chunks = getattr(bot.world, "chunks", None)
    if isinstance(chunks, dict):
        return (_chunk_of(x), _chunk_of(z)) in chunks
    y = int(math.floor(bot.physics_state.position.y))
    return bot.world.get_state_id(int(math.floor(x)), y, int(math.floor(z))) != -1


class StuckWatchdog:
    """Counts how often a walk stopped making progress.

    Fed one position sample per tick: it returns ``True`` when the bot has
    stayed inside ``distance`` for ``seconds``, and gives up after ``limit``
    strikes.  Time is passed in, so a test can drive it without sleeping.
    """

    def __init__(
        self,
        *,
        distance: float = 0.6,
        seconds: float = 3.0,
        limit: int = 3,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.distance = distance
        self.seconds = seconds
        self.limit = limit
        self.strikes = 0
        self._anchor: tuple[float, float] | None = None
        self._since = 0.0

    def reset(self) -> None:
        self.strikes = 0
        self._anchor = None
        self._since = 0.0

    @property
    def exhausted(self) -> bool:
        return self.strikes >= self.limit

    def observe(self, position: tuple[float, float], now: float) -> bool:
        """Record a sample; ``True`` when it counts as a strike."""

        x, z = position
        if self._anchor is None:
            self._anchor = (x, z)
            self._since = now
            return False
        if math.hypot(x - self._anchor[0], z - self._anchor[1]) >= self.distance:
            self._anchor = (x, z)
            self._since = now
            return False
        if now - self._since < self.seconds:
            return False
        self.strikes += 1
        self._anchor = (x, z)
        self._since = now
        return True


class WaitChunkGoal(Goal):
    """Wait until the chunk holding ``(x, z)`` has been decoded.

    Useful in front of anything that needs to read the ground: the server
    streams chunks as the player moves, so "the terrain is not here yet" is a
    normal state, not an error.
    """

    name = "wait_chunk"

    def __init__(self, x: float, z: float, *, timeout: float = 30.0) -> None:
        super().__init__()
        self.x = int(math.floor(x))
        self.z = int(math.floor(z))
        self.timeout = timeout
        self._deadline: float | None = None

    def reset(self) -> None:
        super().reset()
        self._deadline = None

    def describe(self) -> str:
        return f"wait for chunk ({_chunk_of(self.x)}, {_chunk_of(self.z)})"

    def is_reached(self, bot) -> bool:
        return _chunk_loaded(bot, self.x, self.z)

    async def tick(self, bot) -> GoalStatus:
        loop = asyncio.get_running_loop()
        if self._deadline is None:
            self._deadline = loop.time() + self.timeout
        if self.is_reached(bot):
            return GoalStatus.DONE
        if loop.time() >= self._deadline:
            raise GoalError(
                f"chunk ({_chunk_of(self.x)}, {_chunk_of(self.z)}) never loaded"
            )
        self.last_detail = f"waiting for chunk ({_chunk_of(self.x)}, {_chunk_of(self.z)})"
        await asyncio.sleep(POLL_SECONDS)
        return GoalStatus.RUNNING


class TravelGoal(Goal):
    """Walk to ``(x, z)``, in segments, waiting for chunks as they arrive."""

    name = "travel"

    def __init__(
        self,
        x: float,
        z: float,
        *,
        y: float | None = None,
        tolerance: float = DEFAULT_TOLERANCE,
        timeout: float = DEFAULT_TIMEOUT,
        segment_seconds: float = SEGMENT_SECONDS,
        stuck_seconds: float = 3.0,
        max_strikes: int = 3,
    ) -> None:
        super().__init__()
        self.x = float(x)
        self.z = float(z)
        self.y = None if y is None else float(y)
        self.tolerance = tolerance
        self.timeout = timeout
        self.segment_seconds = segment_seconds
        self.watchdog = StuckWatchdog(seconds=stuck_seconds, limit=max_strikes)
        self._deadline: float | None = None
        self._segments = 0

    def reset(self) -> None:
        super().reset()
        self._deadline = None
        self._segments = 0
        self.watchdog.reset()

    def describe(self) -> str:
        return f"travel ({self.x:g}, {self.y if self.y is None else format(self.y, 'g')}, {self.z:g})"

    def distance(self, bot) -> float:
        position = bot.physics_state.position
        return math.hypot(self.x - position.x, self.z - position.z)

    def is_reached(self, bot) -> bool:
        return self.distance(bot) <= self.tolerance

    async def tick(self, bot) -> GoalStatus:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._deadline is None:
            self._deadline = now + self.timeout
        if self.is_reached(bot):
            self.last_detail = f"arrived within {self.tolerance:g} blocks"
            return GoalStatus.DONE
        if now >= self._deadline:
            raise GoalError(
                f"gave up {self.distance(bot):.1f} blocks short of"
                f" ({self.x:g}, {self.z:g})"
            )
        # The pathfinder plans from the terrain it has decoded, so a player
        # whose own chunk is still in flight would be planned from thin air --
        # PathNotFound, or worse a path through blocks that are not there.
        position = bot.physics_state.position
        if not _chunk_loaded(bot, position.x, position.z):
            self.last_detail = (
                f"waiting for chunk ({_chunk_of(position.x)}, {_chunk_of(position.z)})"
                " under the player"
            )
            await asyncio.sleep(POLL_SECONDS)
            return GoalStatus.RUNNING
        if not _chunk_loaded(bot, self.x, self.z):
            waypoint = self._frontier(bot)
            if waypoint is not None:
                await self._walk(bot, waypoint[0], waypoint[1])
                self.last_detail = (
                    f"stepping to ({waypoint[0]:g}, {waypoint[1]:g}) while the"
                    f" chunk around ({_chunk_of(self.x)}, {_chunk_of(self.z)}) loads"
                )
                return GoalStatus.RUNNING
            self.last_detail = (
                f"waiting for chunk ({_chunk_of(self.x)}, {_chunk_of(self.z)})"
            )
            await asyncio.sleep(POLL_SECONDS)
            return GoalStatus.RUNNING
        await self._walk(bot, self.x, self.z)
        self.last_detail = f"{self.distance(bot):.1f} blocks to go"
        return GoalStatus.RUNNING

    # -- helpers ---------------------------------------------------------

    def _frontier(self, bot) -> tuple[float, float] | None:
        """The farthest loaded point on the way to the target, if any."""

        position = bot.physics_state.position
        distance = self.distance(bot)
        if distance <= self.tolerance:
            return None
        step_x = (self.x - position.x) / distance
        step_z = (self.z - position.z) / distance
        best: tuple[float, float] | None = None
        travelled = FRONTIER_STEP
        while travelled <= min(distance, FRONTIER_LIMIT):
            point = (position.x + step_x * travelled, position.z + step_z * travelled)
            if not _chunk_loaded(bot, point[0], point[1]):
                break
            best = point
            travelled += FRONTIER_STEP
        if best is None:
            return None
        if math.hypot(best[0] - position.x, best[1] - position.z) < FRONTIER_STEP:
            return None
        return best

    async def _walk(self, bot, x: float, z: float) -> None:
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                bot.navigate_to(
                    x,
                    z,
                    y=self.y,
                    tolerance=max(self.tolerance * 0.5, 0.3),
                    timeout=self.segment_seconds,
                ),
                timeout=self.segment_seconds + 5.0,
            )
        except (asyncio.TimeoutError, NavigationTimeout, PathNotFound, RuntimeError) as error:
            self._strike(str(error) or type(error).__name__)
            return
        self._segments += 1
        position = bot.physics_state.position
        if self.watchdog.observe((position.x, position.z), loop.time()):
            self._strike(f"no progress for {self.watchdog.seconds:g}s")

    def _strike(self, reason: str) -> None:
        self.watchdog.strikes += 1
        self.last_detail = f"stuck ({reason}), strike {self.watchdog.strikes}/{self.watchdog.limit}"
        if self.watchdog.exhausted:
            raise GoalError(
                f"could not reach ({self.x:g}, {self.z:g}): {reason}"
                f" after {self.watchdog.strikes} attempts"
            )
