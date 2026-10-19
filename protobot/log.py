"""Print-style leveled logging, routable to the TUI log area.

``info`` / ``warn`` / ``error`` / ``debug`` take the same arguments as
``print`` (positional values joined with ``sep``, terminated by ``end``).
By default lines go to stdout; while the TUI runs, cli_app installs a sink
that feeds its log area directly -- unlike ``print``, whose output Textual
captures and swallows while its message loop is running.

Levels
------

``info``, ``warn`` and ``error`` are user-facing and printed by default.
``debug`` is off unless a level asks for it, so a library user does not have to
filter our diagnostics themselves.

The level comes from :func:`set_level` or from the ``PROTOBOT_LOG`` environment
variable (``debug`` / ``info`` / ``warn`` / ``error`` / ``none``).  Setting any
``PROTOBOT_TRACE_*`` variable (see :mod:`protobot.client`) also switches debug on,
because those traces used to print unconditionally.

Usage:

    from protobot import log
    log.info("[chat]", text)          # like print("[chat]", text)
    log.warn("missing setting:", key)
    log.debug("packet %s", name)      # only with PROTOBOT_LOG=debug
"""

from __future__ import annotations

import os
from typing import Any, Callable

__all__ = [
    "DEBUG",
    "ERROR",
    "INFO",
    "OFF",
    "WARN",
    "debug",
    "enabled",
    "error",
    "get_level",
    "info",
    "level_of",
    "set_level",
    "set_sink",
    "trace",
    "warn",
]

#: Receives complete, newline-free lines.  Default routes back to print.
_sink: Callable[[str], None] = lambda line: print(line)

# Numeric levels, low is chatty.  ``logs_at`` compares against these.
DEBUG = 10
INFO = 20
WARN = 30
ERROR = 40
OFF = 100

_NAMES = {
    "debug": DEBUG,
    "info": INFO,
    "warn": WARN,
    "warning": WARN,
    "error": ERROR,
    "off": OFF,
    "none": OFF,
    "quiet": OFF,
}

_LEVEL_LABEL = {
    DEBUG: "debug",
    INFO: "info",
    WARN: "warn",
    ERROR: "error",
    OFF: "off",
}


def level_of(name: str | int | None, default: int = INFO) -> int:
    """Numeric level for ``name``; ``None`` or junk falls back to ``default``."""
    if isinstance(name, int):
        return name
    if not name:
        return default
    return _NAMES.get(str(name).strip().lower(), default)


def _initial_level() -> int:
    configured = os.environ.get("PROTOBOT_LOG", "").strip()
    if configured:
        return level_of(configured, INFO)
    # The packet/movement traces predate the level and used to print straight
    # from the client, so switching one on still means "everything, please".
    for name in ("PROTOBOT_TRACE_SEND", "PROTOBOT_TRACE_RECV", "PROTOBOT_TRACE_POS"):
        if os.environ.get(name, "").strip():
            return DEBUG
    return INFO


_level = _initial_level()


def set_level(level: str | int | None) -> int:
    """Set the threshold and return it.  ``None`` restores the default."""
    global _level
    _level = INFO if level is None else level_of(level)
    return _level


def get_level() -> int:
    return _level


def enabled(level: int) -> bool:
    """Whether a line at ``level`` would be printed."""
    return level >= _level


def set_sink(sink: Callable[[str], None] | None) -> None:
    """Route log lines to a custom sink, or back to stdout when ``None``."""
    global _sink
    _sink = sink if sink is not None else lambda line: print(line)


def _emit(*args: Any, sep: str = " ", end: str = "\n") -> None:
    text = sep.join(str(arg) for arg in args) + end
    pieces = text.split("\n")
    for line in pieces[:-1]:
        _sink(line)
    if pieces[-1]:  # a trailing partial line (end without newline)
        _sink(pieces[-1])


def _emit_at(level: int, prefix: str, args: tuple, sep: str, end: str) -> None:
    if level < _level:
        return
    _emit(prefix, *args, sep=sep, end=end)


def info(*args: Any, sep: str = " ", end: str = "\n") -> None:
    if _level <= INFO:
        _emit(*args, sep=sep, end=end)


def warn(*args: Any, sep: str = " ", end: str = "\n") -> None:
    _emit_at(WARN, "[warn]", args, sep, end)


def error(*args: Any, sep: str = " ", end: str = "\n") -> None:
    _emit_at(ERROR, "[error]", args, sep, end)


def debug(*args: Any, sep: str = " ", end: str = "\n") -> None:
    _emit_at(DEBUG, "[debug]", args, sep, end)


def trace(*args: Any, sep: str = " ", end: str = "\n") -> None:
    """A line below ``debug``: packet dumps and per-tick movement records.

    Callers gate these behind ``PROTOBOT_TRACE_*`` already, so the only thing
    this adds is one place to switch them off.
    """
    _emit_at(DEBUG, "[debug]", args, sep, end)
