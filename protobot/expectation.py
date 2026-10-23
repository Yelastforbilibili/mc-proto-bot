"""Expectations: wait for the world to agree, or find out why it cannot.

Every primitive in this library waits for the server to confirm something: a
block breaks, a slot fills, a window opens, a bed is accepted.  Each of them used
to hand-roll that wait, and a hand-rolled wait can only tell two things apart --
"it happened" and "it never happened" -- so when the *premise* of the wait
disappeared the caller sat there until the deadline and got a generic timeout.
That is exactly how a bot hangs on a bed somebody broke two seconds after it was
clicked: the click was accepted, the world moved on, and nothing was watching.

An :class:`Expectation` waits for two different things on every poll:

``check()``
    "Has the thing I am waiting for happened?"  True finishes it
    :attr:`~ExpectationStatus.DONE`.

``valid()``
    "Is it still possible?"  A reason string means no, and the wait ends
    :attr:`~ExpectationStatus.INVALIDATED` immediately, carrying that reason.
    A caller that ignores it still gets a fast, honest failure instead of a
    timeout, and the reason names what changed.

The other two endings are :attr:`~ExpectationStatus.TIMEOUT` (the clock ran out)
and :attr:`~ExpectationStatus.FAILED` (``check`` raised; the exception text is
kept in the outcome).  ``wait_for_expectation`` never raises for any of them:
outcomes are values, so a caller can retry, replan or report without a
try/except around its control flow.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .constants import TICK_SECONDS

__all__ = [
    "ExpectationStatus",
    "Outcome",
    "wait_for_expectation",
]


class ExpectationStatus(enum.Enum):
    """How a wait ended."""

    DONE = "done"
    FAILED = "failed"
    INVALIDATED = "invalidated"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of one wait: never an exception, always a value."""

    status: ExpectationStatus
    name: str = ""
    detail: str = ""
    elapsed: float = 0.0

    def __bool__(self) -> bool:
        return self.status is ExpectationStatus.DONE

    @property
    def ok(self) -> bool:
        """Alias for ``bool(outcome)``, for callers that dislike truthiness."""

        return self.status is ExpectationStatus.DONE

    def describe(self) -> str:
        """One line a caller can put in a report or log."""

        spent = f"{self.elapsed:.2f}s"
        if self.status is ExpectationStatus.DONE:
            return f"{self.name}: done in {spent}"
        if self.status is ExpectationStatus.INVALIDATED:
            return f"{self.name}: invalidated after {spent} ({self.detail})"
        if self.status is ExpectationStatus.TIMEOUT:
            suffix = f" ({self.detail})" if self.detail else ""
            return f"{self.name}: timed out after {spent}{suffix}"
        return f"{self.name}: failed after {spent} ({self.detail})"


async def wait_for_expectation(
    bot,
    check: Callable[[], bool],
    *,
    valid: Callable[[], str | None] | None = None,
    timeout: float,
    name: str = "expectation",
    tick: bool = True,
    before_tick: Callable[[], Awaitable[None]] | None = None,
    poll: float = TICK_SECONDS,
) -> Outcome:
    """Poll ``check`` until it is true, ``valid`` says it cannot be, or time is up.

    ``tick`` keeps the client alive while waiting (a dig only advances while the
    player keeps sending movement packets, and the receive loop needs wall-clock
    time to deliver the server's answer).  ``before_tick`` runs once per poll
    before that tick, which is where the action layer puts its arm swing so the
    animation lands in the same tick as the movement packet.
    """

    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    while True:
        # The success condition is asked first: a block that broke is a success
        # even if something has already taken its place, and reporting
        # "invalidated" over a real result would be a lie.
        try:
            happened = check()
        except Exception as error:  # noqa: BLE001 - an outcome, not a crash
            return Outcome(
                ExpectationStatus.FAILED,
                name,
                f"{type(error).__name__}: {error}",
                time.monotonic() - started,
            )
        if happened:
            return Outcome(
                ExpectationStatus.DONE, name, "", time.monotonic() - started
            )
        if valid is not None:
            reason = valid()
            if reason:
                return Outcome(
                    ExpectationStatus.INVALIDATED,
                    name,
                    str(reason),
                    time.monotonic() - started,
                )
        if time.monotonic() >= deadline:
            return Outcome(
                ExpectationStatus.TIMEOUT,
                name,
                f"still not true after {timeout:.2f}s",
                time.monotonic() - started,
            )
        if before_tick is not None:
            await before_tick()
        if tick:
            await bot.tick()
        else:
            await asyncio.sleep(max(0.0, poll))
