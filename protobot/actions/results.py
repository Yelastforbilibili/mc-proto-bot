"""What an interaction returned, and how much of it the server confirmed."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActionStatus(str, Enum):
    """How far an interaction got.

    The distinction that matters is ``SENT`` versus ``CONFIRMED``: a packet
    leaving the client proves nothing about the server, and most bugs in this
    area are "we sent it and assumed".  Primitives that have an observable
    consequence report ``CONFIRMED`` only when they saw it.
    """

    #: The packet(s) left the client; nothing was awaited.
    SENT = "sent"
    #: The server acknowledged the effect (block state changed, entity event
    #: observed, ...).
    CONFIRMED = "confirmed"
    #: The packet(s) were sent but no evidence arrived before the deadline.
    UNCONFIRMED = "unconfirmed"
    #: The call refused to touch the wire (target already air, unknown id, ...).
    REJECTED = "rejected"


@dataclass(frozen=True)
class ActionResult:
    """The outcome of one interaction, including the evidence for it."""

    #: ``"dig"``, ``"place_block"``, ``"attack"``, ``"interact"``, ``"swing"``.
    action: str
    status: ActionStatus
    #: Sequence number of the primary packet, when the packet carries one.
    sequence: int | None = None
    position: tuple[int, int, int] | None = None
    entity_id: int | None = None
    #: Block state id before and after (``None`` for entity actions).
    state_before: int | None = None
    state_after: int | None = None
    #: Packet ids actually written, in order.  Handy in a failure report.
    packets: tuple[int, ...] = ()
    #: Wall-clock seconds the call occupied.
    elapsed: float = 0.0
    #: Human readable reason for ``UNCONFIRMED``/``REJECTED``, or what was
    #: observed for ``CONFIRMED`` (``"world state 5 -> 0"``).
    evidence: str = ""
    #: Free-form extras (the :class:`~protobot.actions.raycast.BlockHit` that
    #: was targeted, the face that was used, ...).
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the server did the thing, or when nothing was expected."""

        return self.status in (ActionStatus.SENT, ActionStatus.CONFIRMED)

    @property
    def confirmed(self) -> bool:
        return self.status is ActionStatus.CONFIRMED

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        where = ""
        if self.position is not None:
            where = " at {},{},{}".format(*self.position)
        elif self.entity_id is not None:
            where = f" on entity {self.entity_id}"
        tail = f" ({self.evidence})" if self.evidence else ""
        return f"{self.action}{where}: {self.status.value}{tail}"
