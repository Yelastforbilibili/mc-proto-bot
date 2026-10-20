"""Client-side state models."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import IntEnum


class EquipmentSlot(IntEnum):
    """Wire ordinals used by the clientbound equipment packet."""

    MAINHAND = 0
    OFFHAND = 1
    FEET = 2
    LEGS = 3
    CHEST = 4
    HEAD = 5
    BODY = 6
    SADDLE = 7


@dataclass(frozen=True, slots=True)
class ItemStack:
    """Safely decoded identity, removal list, and raw added-component patch.

    ``components`` holds the bytes of the added data-component patch verbatim.
    Each component value is only self-delimiting to the codec that wrote it, so
    the stack keeps the payload instead of guessing field by field; the frame
    around it (count, item id, patch length) is what ``protobot.protocol.stacks``
    recovers, and that is what makes the *next* slot readable.
    """

    count: int = 0
    item_id: int | None = None
    identifier: str | None = None
    removed_component_ids: tuple[int, ...] = ()
    components: bytes = b""

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("item count cannot be negative")
        if self.count and (self.item_id is None or self.item_id < 0):
            raise ValueError("non-empty item stacks require a registry ID")
        if not self.count and self.item_id is not None:
            raise ValueError("empty item stacks cannot have a registry ID")
        if any(component_id < 0 for component_id in self.removed_component_ids):
            raise ValueError("component registry IDs cannot be negative")

    @property
    def empty(self) -> bool:
        return self.count == 0

    @property
    def has_components(self) -> bool:
        """True when the stack carries an added-component patch."""

        return bool(self.components)


@dataclass(slots=True)
class ContainerState:
    """Latest server snapshot for one open container.

    Both views are kept.  ``slots`` holds the exact protocol bytes after the
    slot header -- which makes custom GUI components observable without
    pretending to understand application-specific component registries -- while
    ``items`` holds the decoded stacks the container layer works with.  A stack
    carrying added data components cannot be decoded (their length is only
    known to the codec that wrote them), so ``unreadable`` records the slots
    that stayed byte-only.  The container layer never writes to or reasons
    about those slots -- it steers around them instead of pretending they are
    empty.
    """

    container_id: int
    menu_type_id: int | None = None
    state_id: int = 0
    title_data: bytes = b""
    slots: dict[int, bytes] = field(default_factory=dict)
    items: dict[int, ItemStack] = field(default_factory=dict)
    data: dict[int, int] = field(default_factory=dict)
    slot_count: int = 0
    unreadable: set[int] = field(default_factory=set)
    carried_item_data: bytes = b""
    carried_item: ItemStack = field(default_factory=ItemStack)
    carried_unknown: bool = False
    content_data: bytes = b""
    is_open: bool = True
    #: Bumped on every server update for this window; a click is confirmed by
    #: observing a change, not by assuming the click worked.
    revision: int = 0
    #: Bumped only when the *contents* change (slots or the cursor), never by a
    #: progress update.  A furnace sends ``container_set_data`` several times a
    #: second, so confirming a click on ``revision`` alone would report success
    #: before the slot update it was actually waiting for had arrived.
    slot_revision: int = 0
    #: Bumped only when the server announces the cursor.  The server stays
    #: quiet when the click packet already claimed the resulting cursor, so a
    #: click that produces no announcement may be predicted locally instead of
    #: left stale.
    cursor_revision: int = 0


@dataclass(frozen=True, slots=True)
class EntityMetadataValue:
    """One decoded SynchedEntityData value keyed by its accessor index."""

    serializer_id: int
    value: object


@dataclass(slots=True)
class EntityState:
    """Latest protocol position, motion, mount, and equipment for one entity."""

    entity_id: int
    entity_uuid: uuid.UUID | None = None
    type_id: int | None = None
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    velocity_z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    head_yaw: float = 0.0
    data: int = 0
    on_ground: bool = False
    collision_width: float | None = None
    collision_height: float | None = None
    vehicle_id: int | None = None
    passenger_ids: tuple[int, ...] = ()
    metadata: dict[int, EntityMetadataValue] = field(default_factory=dict)
    shulker_attach_face: int = 0
    shulker_peek: int = 0
    shulker_current_peek: float = 0.0
    shulker_previous_peek: float = 0.0
    boat_status: str = "in_air"
    boat_old_status: str = "in_air"
    boat_water_level: float = 0.0
    boat_land_friction: float = 0.0
    boat_last_y_velocity: float = 0.0
    boat_delta_rotation: float = 0.0
    boat_left_paddle: bool = False
    boat_right_paddle: bool = False
    horizontal_collision: bool = False
    vertical_collision: bool = False
    equipment: dict[EquipmentSlot, ItemStack] = field(default_factory=dict)

    @property
    def position(self) -> tuple[float, float, float]:
        return self.x, self.y, self.z

    @property
    def velocity(self) -> tuple[float, float, float]:
        return self.velocity_x, self.velocity_y, self.velocity_z


@dataclass(slots=True)
class PlayerListEntry:
    """One tab-list row: who the server says is online.

    Fields keep their last known value: the server sends partial updates
    (latency alone, game mode alone), so only what a packet carries is
    replaced.  ``name`` is empty when the entry was created by an update that
    arrived without the add-player action.
    """

    uuid: uuid.UUID
    name: str = ""
    game_mode: int = 0
    latency: int = 0
    listed: bool = True
    #: Display name as plain text; empty when the server sent none, in which
    #: case ``label`` falls back to ``name``.
    display_name: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.name or str(self.uuid)


@dataclass(slots=True)
class PlayerAbilities:
    """The six values carried by the clientbound player-abilities packet."""

    invulnerable: bool = False
    flying: bool = False
    allow_flying: bool = False
    instant_build: bool = False
    fly_speed: float = 0.05
    walk_speed: float = 0.1


@dataclass(slots=True)
class PlayerState:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    velocity_z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    on_ground: bool = False
    horizontal_collision: bool = False
    loaded: bool = False
    #: Only clientbound_set_health updates these; on versions where that
    #: packet id is unverified they keep their initial values.
    health: float = 20.0
    food: int = 20
    saturation: float = 5.0
    #: True after a death signal, cleared by the respawn packet.
    dead: bool = False
    #: Experience, mirrored from ``clientbound_set_experience``.  Enchanting is
    #: gated on the level, so a caller wants the server's number, not a guess.
    experience_progress: float = 0.0
    experience_level: int = 0
    experience_total: int = 0
    pose: str = "standing"
    crouching: bool = False
    swimming: bool = False
    gliding: bool = False
    selected_hotbar_slot: int = 0
    vehicle_id: int | None = None
    passenger_ids: tuple[int, ...] = ()
    metadata: dict[int, EntityMetadataValue] = field(default_factory=dict)
    abilities: PlayerAbilities = field(default_factory=PlayerAbilities)
    inventory: dict[int, ItemStack] = field(default_factory=dict)
    equipment: dict[EquipmentSlot, ItemStack] = field(default_factory=dict)

    @property
    def position(self) -> tuple[float, float, float]:
        return self.x, self.y, self.z

    @property
    def velocity(self) -> tuple[float, float, float]:
        return self.velocity_x, self.velocity_y, self.velocity_z

    @property
    def wearing_elytra(self) -> bool:
        chest = self.equipment.get(EquipmentSlot.CHEST)
        return chest is not None and chest.identifier == "minecraft:elytra"


@dataclass(slots=True)
class WorldSessionState:
    entity_id: int | None = None
    dimension_type_id: int | None = None
    dimension_name: str | None = None
    game_mode: int = 0
    previous_game_mode: int = -1
    hardcore: bool = False
    is_debug: bool = False
    is_flat: bool = False
    view_distance: int = 0
    simulation_distance: int = 0
    sea_level: int = 63
    online_mode: bool = False
    enforces_secure_chat: bool = False
