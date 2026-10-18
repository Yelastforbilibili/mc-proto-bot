"""Errors raised by the interaction layer."""

from __future__ import annotations


class ActionError(Exception):
    """Base class for interaction failures."""


class UnsupportedAction(ActionError):
    """The negotiated protocol has no verified id for this action.

    ``VersionSpec.packets`` leaves a packet id at ``0`` when the release was
    never audited for it (see ``tests/test_packet_ids.py``).  Sending id ``0``
    would put some unrelated packet on the wire, so the interaction layer
    refuses to guess and raises this instead.
    """

    def __init__(self, action: str, version: str, field: str) -> None:
        super().__init__(
            f"{action} is unavailable on {version}: the id for {field} is "
            "unverified for this release"
        )
        self.action = action
        self.version = version
        self.field = field


class ActionTimeout(ActionError):
    """The server never confirmed an action before its deadline."""
