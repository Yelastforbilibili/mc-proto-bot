"""Container layer: semantic windows on top of the raw click API.

``protobot.client.Bot`` speaks the container protocol by number (``click_container``
and friends).  This package adds the meaning: which slot is the furnace's fuel,
what does "put the dirt in the chest" mean, and how do we know the server
agreed.  It sits between the action layer and the goal layer, so a goal can say
"deposit 15 dirt" without knowing a slot number or a state ID.

    from protobot.containers import Menu

    menu = await Menu(bot).open_block((6, -60, -8))
    moved = await menu.deposit("minecraft:dirt", 15)
"""

from .errors import ClickRejected, ContainerError, MenuLayoutError, MenuNotOpen
from .layouts import (
    PLAYER_SEGMENTS,
    RESULT_SEGMENTS,
    MenuLayout,
    known_identifiers,
    layout_for,
)
from .menu import (
    CLICK_MODES,
    DATA_SLOTS,
    DEFAULT_CLICK_TIMEOUT,
    MAX_STACK,
    PLAYER_CONTAINER_ID,
    Menu,
    player_menu,
)

__all__ = [
    "CLICK_MODES",
    "ClickRejected",
    "ContainerError",
    "DATA_SLOTS",
    "DEFAULT_CLICK_TIMEOUT",
    "MAX_STACK",
    "Menu",
    "MenuLayout",
    "MenuLayoutError",
    "MenuNotOpen",
    "PLAYER_CONTAINER_ID",
    "PLAYER_SEGMENTS",
    "RESULT_SEGMENTS",
    "known_identifiers",
    "layout_for",
    "player_menu",
]
