"""Values shared by the client and the concern modules that implement it.

Everything here is a constant, a small dataclass or a pure helper: no method on
this list talks to a socket, so the concern modules (`connection`, `movement`,
`play.router`, `navigator`) can import freely without import cycles.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import MIN_TICK_GAP as _MIN_TICK_GAP
from .navigation import FlightPathfinder, NavigationPath, Pathfinder
from .physics import Vec3

if TYPE_CHECKING:
    from .auth import PlayerCertificate

_MOVEMENT_EFFECTS = {
    7: "minecraft:jump_boost",
    24: "minecraft:levitation",
    27: "minecraft:slow_falling",
    29: "minecraft:dolphins_grace",
    36: "minecraft:weaving",
}

# ``PROTOBOT_TRACE_SEND=1`` prints every packet the client puts on the wire,
# which is the only way to tell "the click was rejected locally" apart from
# "the click never left" when a server action silently does nothing.
_TRACE_SEND = os.environ.get("PROTOBOT_TRACE_SEND", "").strip() not in (
    "",
    "0",
    "false",
)
# ``PROTOBOT_TRACE_RECV=1`` does the same for inbound packets, which is how one
# tells "the server never answered" apart from "we dropped the answer".
_TRACE_RECV = os.environ.get("PROTOBOT_TRACE_RECV", "").strip() not in (
    "",
    "0",
    "false",
)
# ``PROTOBOT_TRACE_POS=<path>`` appends one CSV line per sent movement tick
# (wall clock, x, y, z, yaw, pitch, on_ground).  Aligning that file with a
# server-side anti-cheat alert timestamp is how one finds the exact tick and
# spot a movement prediction disagreed with the server.
_TRACE_POS = os.environ.get("PROTOBOT_TRACE_POS", "").strip()


def _serverbound_name(packets: object, packet_id: int) -> str:
    for field in getattr(packets, "__dataclass_fields__", ()):
        if field.startswith("serverbound_") and getattr(packets, field) == packet_id:
            return field[len("serverbound_") :]
    return str(packet_id)


def _clientbound_name(packets: object, packet_id: int) -> str:
    for field in getattr(packets, "__dataclass_fields__", ()):
        if field.startswith("clientbound_") and getattr(packets, field) == packet_id:
            return field[len("clientbound_") :]
    return str(packet_id)


# Player Info Update action bits: 1.21.4+ has eight of them, which is
# exactly one byte of the fixed-size bit set the packet carries.
_PLAYER_INFO_ADD = 0x01
_PLAYER_INFO_INIT_CHAT = 0x02
_PLAYER_INFO_GAME_MODE = 0x04
_PLAYER_INFO_LISTED = 0x08
_PLAYER_INFO_LATENCY = 0x10
_PLAYER_INFO_DISPLAY_NAME = 0x20
_PLAYER_INFO_HAT = 0x40
_PLAYER_INFO_LIST_ORDER = 0x80

#: A player list arriving within this long of entering PLAY counts as the
#: initial roster rather than people joining. Vanilla sends everyone who is
#: online in one packet, but a proxy may split it across several.
_ROSTER_GRACE = 1.0

_MOVEMENT_ATTRIBUTES_774 = {
    14: "minecraft:gravity",
    15: "minecraft:jump_strength",
    21: "minecraft:movement_efficiency",
    22: "minecraft:movement_speed",
    26: "minecraft:sneaking_speed",
    28: "minecraft:step_height",
    32: "minecraft:water_movement_efficiency",
}

_MOVEMENT_ATTRIBUTES_775_776 = {
    0: "minecraft:air_drag_modifier",
    9: "minecraft:bounciness",
    17: "minecraft:friction_modifier",
    18: "minecraft:gravity",
    19: "minecraft:jump_strength",
    25: "minecraft:movement_efficiency",
    26: "minecraft:movement_speed",
    31: "minecraft:sneaking_speed",
    33: "minecraft:step_height",
    37: "minecraft:water_movement_efficiency",
}

_MOVEMENT_ATTRIBUTES = {
    774: _MOVEMENT_ATTRIBUTES_774,
    775: _MOVEMENT_ATTRIBUTES_775_776,
    776: _MOVEMENT_ATTRIBUTES_775_776,
    # 26.3 leaves the attribute registry untouched.
    777: _MOVEMENT_ATTRIBUTES_775_776,
}

_MOVEMENT_ATTRIBUTE_RANGES = {
    "minecraft:air_drag_modifier": (0.0, 2048.0),
    "minecraft:bounciness": (0.0, 1.0),
    "minecraft:friction_modifier": (0.0, 2048.0),
    "minecraft:gravity": (-1.0, 1.0),
    "minecraft:jump_strength": (0.0, 32.0),
    "minecraft:movement_efficiency": (0.0, 1.0),
    "minecraft:movement_speed": (0.0, 1024.0),
    "minecraft:sneaking_speed": (0.0, 1.0),
    "minecraft:step_height": (0.0, 10.0),
    "minecraft:water_movement_efficiency": (0.0, 1.0),
}

_STOP_SLEEPING_COMMAND = 0
_START_SPRINTING_COMMAND = 1
_STOP_SPRINTING_COMMAND = 2
_START_FALL_FLYING_COMMAND = 6
_CHANGE_GAME_MODE_EVENT = 3
_SPRINTING_SPEED_MODIFIER = "minecraft:sprinting"

_CONTAINER_CLICK_TYPES = {
    "pickup": 0,
    "quick_move": 1,
    "swap": 2,
    "clone": 3,
    "throw": 4,
    "quick_craft": 5,
    "pickup_all": 6,
}
_INTERACTION_HANDS = {"main_hand": 0, "mainhand": 0, "off_hand": 1, "offhand": 1}
DEFAULT_MINECRAFT_PORT = 25565
_LAST_SEEN_MESSAGE_COUNT = 20
_CHAT_ACKNOWLEDGEMENT_THRESHOLD = 64


@dataclass(frozen=True, slots=True)
class _LastSeenUpdate:
    offset: int
    acknowledged: bytes
    signatures: tuple[bytes, ...]
    checksum: int


class _LastSeenMessagesTracker:
    """Track signed player chat using vanilla's 20-entry acknowledgement window."""

    def __init__(self) -> None:
        self._tracked: list[bytes | None] = [None] * _LAST_SEEN_MESSAGE_COUNT
        self._tail = 0
        self._offset = 0
        self._last_tracked: bytes | None = None

    @property
    def offset(self) -> int:
        return self._offset

    def add_pending(self, signature: bytes, *, shown: bool = True) -> bool:
        if len(signature) != 256:
            raise ValueError("chat signatures must contain exactly 256 bytes")
        signature = bytes(signature)
        if signature == self._last_tracked:
            return False
        self._last_tracked = signature
        self._tracked[self._tail] = signature if shown else None
        self._tail = (self._tail + 1) % _LAST_SEEN_MESSAGE_COUNT
        self._offset += 1
        return True

    def take_offset(self) -> int:
        offset = self._offset
        self._offset = 0
        return offset

    @staticmethod
    def _signature_hash(signature: bytes) -> int:
        """Match Java ``Arrays.hashCode(byte[])`` including signed bytes."""

        result = 1
        for value in signature:
            signed_value = value if value < 128 else value - 256
            result = (31 * result + signed_value) & 0xFFFFFFFF
        return result - (1 << 32) if result >= 1 << 31 else result

    def generate_update(self) -> _LastSeenUpdate:
        """Generate vanilla's offset, bit set, signed entries, and checksum."""

        offset = self.take_offset()
        acknowledged = 0
        signatures: list[bytes] = []
        for bit in range(_LAST_SEEN_MESSAGE_COUNT):
            index = (self._tail + bit) % _LAST_SEEN_MESSAGE_COUNT
            signature = self._tracked[index]
            if signature is not None:
                acknowledged |= 1 << bit
                signatures.append(signature)
        checksum = 1
        for signature in signatures:
            checksum = (31 * checksum + self._signature_hash(signature)) & 0xFFFFFFFF
        checksum &= 0xFF
        if checksum == 0:
            checksum = 1
        return _LastSeenUpdate(
            offset,
            acknowledged.to_bytes(3, "little"),
            tuple(signatures),
            checksum,
        )


def _parse_chat_text(text: str) -> object:
    """Decode a chat message body, falling back to the raw string.

    The protocol carries message bodies as JSON text components, but lenient
    servers occasionally send plain text instead.
    """

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _find_ground_path(
    world,
    start: Vec3,
    target: Vec3,
    *,
    player_width: float,
    player_height: float,
    step_height: float,
    max_drop: float,
    allow_diagonal: bool,
    max_nodes: int,
    match_target_y: bool,
    physics=None,
) -> NavigationPath:
    planner = Pathfinder(
        world,
        player_width=player_width,
        player_height=player_height,
        step_height=step_height,
        max_drop=max_drop,
        allow_diagonal=allow_diagonal,
        physics=physics,
    )
    return planner.find_path(
        start,
        target,
        match_target_y=match_target_y,
        max_nodes=max_nodes,
    )


def _find_flight_path(
    world,
    start: Vec3,
    target: Vec3,
    *,
    player_width: float,
    player_height: float,
    allow_diagonal: bool,
    vclip: bool,
    vclip_up_limit: float,
    vclip_down_limit: float,
    max_nodes: int,
    allow_blocked_target: bool = True,
) -> NavigationPath:
    planner = FlightPathfinder(
        world,
        player_width=player_width,
        player_height=player_height,
        allow_diagonal=allow_diagonal,
        vclip=vclip,
        vclip_up_limit=vclip_up_limit,
        vclip_down_limit=vclip_down_limit,
    )
    return planner.find_path(
        start,
        target,
        max_nodes=max_nodes,
        allow_blocked_target=allow_blocked_target,
    )


class _UnsupportedItemComponents(Exception):
    pass


#: Container id the server uses for the player's own inventory.  Its slot
#: updates have to be mirrored into ``Bot.player.inventory`` or the bot cannot
#: see items it was given, picked up or had damaged.
PLAYER_CONTAINER_ID = 0

#: Highest player-inventory slot index (9 crafting/armour + 27 main + 9 hotbar
#: + 1 off hand).
PLAYER_INVENTORY_LAST_SLOT = 45

#: Defensive cap when decoding an announced container slot count.
_MAX_CONTAINER_SLOTS = 1024

#: Minimum wall-clock gap between two movement packets.  A vanilla client only
#: moves once per 20 Hz tick, and anti-cheat timer checks measure exactly that:
#: Grim's Timer adds 50 ms per movement packet and flags a client that runs
#: ahead of real time.  Bursts (aim right after a position packet) have to wait.
#: See ``protobot.constants`` for the value and the rest of the tuning knobs.
MIN_TICK_GAP = _MIN_TICK_GAP


class _UnsupportedEntityMetadata(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AttributeModifierUpdate:
    """One modifier decoded from a clientbound attribute update."""

    identifier: str
    amount: float
    operation: int


@dataclass(frozen=True, slots=True)
class AttributeUpdate:
    """A decoded attribute snapshot and its vanilla-computed value."""

    registry_id: int
    identifier: str | None
    base: float
    modifiers: tuple[AttributeModifierUpdate, ...]
    value: float


def offline_uuid(username: str) -> uuid.UUID:
    """Match Java's UUID.nameUUIDFromBytes for ``OfflinePlayer:<name>``."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{username}".encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(digest))
