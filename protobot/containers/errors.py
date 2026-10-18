"""Errors raised by the container layer."""

from __future__ import annotations

from ..errors import ProtoBotError


class ContainerError(ProtoBotError):
    """Base class for container/window failures."""


class MenuNotOpen(ContainerError):
    """No window is open, or the requested one is not tracked."""


class MenuLayoutError(ContainerError):
    """The window's slots cannot be mapped onto a known layout."""


class ClickRejected(ContainerError):
    """A click was sent but the server did not apply it."""

    def __init__(self, message: str, *, slot: int | None = None) -> None:
        super().__init__(message)
        self.slot = slot
