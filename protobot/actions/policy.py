"""How hard an action tries when the world moves under it.

Every aiming action has a window between "the crosshair is right" and "the
packet the server judges arrives".  Being knocked back inside that window turns
a perfectly good aim into a stale one, and the vanilla client simply does not
care: it keeps aiming at where the target is now.  This module is the knob that
lets a caller decide how much of that the library does on its own.

Nothing here is a retry of a *rejected* action -- a server that says no stays a
no.  It only re-aims when the aim went stale because the player was pushed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    """How to react when a knockback lands between aiming and acting.

    ``retries`` is the interesting knob: ``0`` means "never re-aim, report what
    happened", ``1`` (the default) means "if the server pushed me while I was
    doing it, one more attempt".  Copy-free: build the one you want and assign
    it to ``bot.actions.policy``, or pass it to a single call.
    """

    #: Extra attempts allowed after a knockback invalidated the aim.
    retries: int = 1
    #: Seconds to let the knockback arc start before aiming again, so the new
    #: aim is against a position that is not sliding away any more.
    settle: float = 0.10
    #: Wall-clock ceiling for all attempts together, knockbacks or not.
    max_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.retries < 0:
            raise ValueError("retries cannot be negative")
        if self.settle < 0.0:
            raise ValueError("settle cannot be negative")
        if self.max_seconds <= 0.0:
            raise ValueError("max_seconds must be positive")

    @property
    def attempts(self) -> int:
        """Total attempts a caller may see, the first one included."""

        return self.retries + 1

    def describe(self) -> str:
        if self.retries == 0:
            return "no re-aim after a knockback"
        return (
            f"up to {self.retries} re-aim(s) after a knockback, "
            f"{self.settle:.2f}s settle, {self.max_seconds:.1f}s total"
        )


#: The policy every action uses unless the caller installs another one.  One
#: retry is deliberate: a knocked-back click is the common case in a fight, and
#: a caller that wants none can say so in one line.
DEFAULT_POLICY = ActionPolicy()

#: Never re-aim -- for callers that would rather see the failure themselves.
NEVER_RETRY = ActionPolicy(retries=0)
