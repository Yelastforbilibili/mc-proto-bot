"""Release metadata and the packet IDs needed by the core state machine."""

from __future__ import annotations

from dataclasses import dataclass

from protobot.errors import UnsupportedVersion
from protobot.protocol.breaking import BreakTable
from protobot.protocol.breaking import table_for as breaking_table_for
from protobot.protocol.entities import EntityTable, table_for as entity_table_for
from protobot.protocol.items import ItemTable, table_for
from protobot.protocol.menus import MenuTable, table_for as menu_table_for


@dataclass(frozen=True, slots=True)
class PlayPacketIds:
    clientbound_add_entity: int
    clientbound_custom_payload: int
    clientbound_container_close: int
    clientbound_container_set_content: int
    clientbound_container_set_slot: int
    clientbound_open_screen: int
    clientbound_remove_mob_effect: int
    clientbound_remove_entities: int
    clientbound_block_update: int
    clientbound_chunk_batch_finished: int
    clientbound_chunk_data: int
    clientbound_disconnect: int
    clientbound_game_event: int
    clientbound_keep_alive: int
    clientbound_login: int
    clientbound_move_vehicle: int
    clientbound_move_entity_pos: int
    clientbound_move_entity_pos_rot: int
    clientbound_move_entity_rot: int
    clientbound_ping: int
    clientbound_player_abilities: int
    clientbound_player_chat: int
    clientbound_profileless_chat: int
    clientbound_position: int
    clientbound_respawn: int
    clientbound_section_blocks_update: int
    clientbound_set_entity_data: int
    clientbound_set_entity_motion: int
    clientbound_set_equipment: int
    clientbound_set_passengers: int
    clientbound_set_player_inventory: int
    clientbound_start_configuration: int
    clientbound_system_chat: int
    clientbound_teleport_entity: int
    clientbound_transfer: int
    clientbound_unload_chunk: int
    clientbound_update_attributes: int
    clientbound_update_mob_effect: int
    serverbound_chat_ack: int
    serverbound_chunk_batch_received: int
    serverbound_chat: int
    serverbound_chat_command: int
    serverbound_chat_session_update: int
    serverbound_custom_payload: int
    serverbound_container_click: int
    serverbound_container_close: int
    serverbound_configuration_acknowledged: int
    serverbound_keep_alive: int
    serverbound_move_vehicle: int
    serverbound_paddle_boat: int
    serverbound_position: int
    serverbound_position_look: int
    serverbound_look: int
    serverbound_flying: int
    serverbound_player_abilities: int
    serverbound_player_command: int
    serverbound_player_input: int
    serverbound_player_loaded: int
    serverbound_pong: int
    serverbound_set_carried_item: int
    serverbound_tick_end: int
    serverbound_use_item: int
    serverbound_teleport_confirm: int = 0
    #: Positioned sound packet. 0 = this release's id is unverified, so
    #: features that need it must degrade rather than decode on a guess.
    clientbound_sound: int = 0
    #: Death and respawn. Again 0 means unverified for this release, and the
    #: core skips 0 so a real 0x00 packet is never taken for a death signal.
    clientbound_set_health: int = 0
    clientbound_player_combat_kill: int = 0
    serverbound_client_command: int = 0
    #: Tab-list add/remove, behind the player_join / player_leave events.
    #: 0 means unverified for this release, as above.
    clientbound_player_info_update: int = 0
    clientbound_player_info_remove: int = 0
    # --- interaction ---------------------------------------------------
    # Everything below exists for ``protobot.actions``: the packet ids the
    # primitive layer needs in order to dig, place, attack, interact and
    # swing.  ``0`` means this release does not key the packet here, and the
    # action layer refuses to send rather than guessing an id.
    #:
    #: ``PlayerActionPacket``: dig start/progress/finish, drop item, release.
    serverbound_player_action: int = 0
    #: ``UseItemOnPacket``: right click a specific block face.
    serverbound_use_item_on: int = 0
    #: ``InteractPacket``: attack / interact / interact-at an entity.
    serverbound_interact: int = 0
    #: Arm swing. ``punch`` from 26.3 on, ``arm_animation`` before it.
    serverbound_swing: int = 0
    #: 26.3 split the entity attack out of ``interact`` into its own packet.
    #: Absent (0) on every earlier release.
    serverbound_attack: int = 0
    #: Confirms a container slot state id; the server resyncs the whole
    #: inventory when a client never acknowledges one.
    serverbound_container_slot_state_changed: int = 0
    serverbound_set_creative_mode_slot: int = 0
    #: Ack for a ``player_action`` sequence number: the dig confirmation.
    clientbound_block_changed_ack: int = 0
    #: Break particles; broadcast to everyone but the breaking player.
    clientbound_block_destruction: int = 0
    #: Other entities' arm swings (26.3 split this out of ``animation``).
    clientbound_swing_animation: int = 0
    #: Which hotbar slot the server thinks is selected.
    clientbound_set_held_slot: int = 0
    #: Furnace/brewing/enchanting progress bars.
    clientbound_container_set_data: int = 0
    #: The item held on the cursor.  Releases that have this packet carry the
    #: cursor here instead of in a ``container_set_slot`` with slot ``-1``.
    clientbound_set_cursor_item: int = 0
    #: Hit feedback for the entity in view.
    clientbound_hurt_animation: int = 0
    clientbound_damage_event: int = 0
    #: Broken-block replacement packet new in 26.3 (0 on earlier releases).
    clientbound_add_transient_block: int = 0


@dataclass(frozen=True, slots=True)
class ConfigurationPacketIds:
    """Clientbound configuration IDs that moved between releases.

    The remaining configuration IDs (cookie request, custom payload,
    disconnect, finish, keep-alive, ping, registry data) and every
    serverbound configuration ID are identical in all supported releases.
    """

    clientbound_transfer: int = 0x0B
    clientbound_select_known_packs: int = 0x0E
    clientbound_code_of_conduct: int = 0x13


#: 1.21.8 keeps the 1.21.11-26.2 transfer/select_known_packs numbering: its
#: clientbound configuration table has transfer at 0x0B and select_known_packs
#: at 0x0E (source=1.21.8 reports/packets.json, configuration state).
#: ``code_of_conduct`` does not exist in 772 at all -- that table ends at 0x12
#: ``show_dialog`` -- so the inherited 0x13 default names no packet.  Nothing
#: sends it on this protocol, so the default is kept instead of re-pointing it
#: at a real packet that means something else.
_CONFIGURATION_772 = ConfigurationPacketIds()

#: 1.21.11 through 26.2 share this numbering. 26.3 inserts ``post_effects``
#: at 0x0A, shifting every later clientbound configuration packet by one.
_CONFIGURATION_774_776 = ConfigurationPacketIds()
_CONFIGURATION_777 = ConfigurationPacketIds(
    clientbound_transfer=0x0C,
    clientbound_select_known_packs=0x0F,
    clientbound_code_of_conduct=0x14,
)


@dataclass(frozen=True, slots=True)
class VersionSpec:
    name: str
    protocol: int
    data_version: int
    packets: PlayPacketIds
    item_identifiers: tuple[tuple[int, str], ...] = ()
    hard_collision_entities: tuple[tuple[int, float, float], ...] = ()
    boat_entities: tuple[int, ...] = ()
    chest_boat_entities: tuple[int, ...] = ()
    raft_entities: tuple[int, ...] = ()
    #: Entity-type registry IDs with bespoke collision/metadata handling.
    happy_ghast_entity: int = 58
    shulker_entity: int = 112
    configuration: ConfigurationPacketIds = _CONFIGURATION_774_776

    def item_identifier(self, registry_id: int) -> str | None:
        """Resolve an item ID using this exact release's registry.

        ``item_identifiers`` holds the handful of overrides the physics and
        equipment code needs; the full registry is the bundled
        ``protobot/data/items-<protocol>.json.gz`` table, because item IDs are
        per-release and guessing them silently selects the wrong item.
        """

        for item_id, identifier in self.item_identifiers:
            if item_id == registry_id:
                return identifier
        table = self.items
        return None if table is None else table.identifier(registry_id)

    @property
    def items(self) -> ItemTable | None:
        """The bundled item registry for this release, if one ships."""

        return table_for(self.protocol)

    def menu_identifier(self, registry_id: int) -> str | None:
        """Resolve a ``clientbound_open_screen`` menu type ID to its name.

        The wire identifies a window by registry ID (26.3 sends ``2`` for a
        chest), so the container layer needs this release's own
        ``minecraft:menu`` table to know which slots mean what.
        """

        table = self.menus
        return None if table is None else table.identifier(registry_id)

    @property
    def menus(self) -> MenuTable | None:
        """The bundled menu registry for this release, if one ships."""

        return menu_table_for(self.protocol)

    def entity_identifier(self, registry_id: int) -> str | None:
        """Resolve an entity-type ID to its name.

        Entity raycasting needs the name twice: to look up the hitbox, and to
        decide whether the thing in front of the player is what the caller
        asked for.  Unknown ids stay ``None`` so callers can fall back instead
        of guessing a size.
        """

        table = self.entities
        return None if table is None else table.identifier(registry_id)

    @property
    def entities(self) -> EntityTable | None:
        """The bundled entity-type registry for this release, if one ships."""

        return entity_table_for(self.protocol)

    def entity_size(self, registry_id: int) -> tuple[float, float] | None:
        """Hitbox ``(width, height)`` of an entity type, if measured."""

        table = self.entities
        return None if table is None else table.size(registry_id)

    def hardness(self, identifier: str) -> float | None:
        """Vanilla block hardness, or ``None`` when the block is not in the table.

        Hardness is what turns "dig this" into "hold the button this long", and
        it is the one block property neither Mojang's reports nor the protocol
        carries, so it ships in its own per-release data file.
        """

        table = self.breaking
        return None if table is None else table.hardness(identifier)

    @property
    def breaking(self) -> BreakTable | None:
        """The bundled hardness table for this release, if one ships."""

        return breaking_table_for(self.protocol)

    def hard_collision_dimensions(self, registry_id: int) -> tuple[float, float] | None:
        for entity_id, width, height in self.hard_collision_entities:
            if entity_id == registry_id:
                return width, height
        return None

    def boat_kind(self, registry_id: int) -> str | None:
        """Return the source-level AbstractBoat variant for an entity ID."""

        if registry_id not in self.boat_entities:
            return None
        chest = registry_id in self.chest_boat_entities
        raft = registry_id in self.raft_entities
        if raft:
            return "chest_raft" if chest else "raft"
        return "chest_boat" if chest else "boat"


# 1.21.8 entity-type registry ids.  Source: the release's own Mojang
# ``reports/registries.json`` (``minecraft:entity_type`` protocol_id), which
# matches minecraft-data ``data/pc/1.21.8/entities.json`` id-for-id for all 20
# boat/raft types.  Every later release inserts entities, so none of these
# numbers are shared with the 26.x tables below.
_BOAT_ENTITIES_772 = (
    0,  # acacia_boat
    1,  # acacia_chest_boat
    8,  # bamboo_chest_raft
    9,  # bamboo_raft
    12,  # birch_boat
    13,  # birch_chest_boat
    22,  # cherry_boat
    23,  # cherry_chest_boat
    31,  # dark_oak_boat
    32,  # dark_oak_chest_boat
    72,  # jungle_boat
    73,  # jungle_chest_boat
    79,  # mangrove_boat
    80,  # mangrove_chest_boat
    85,  # oak_boat
    86,  # oak_chest_boat
    90,  # pale_oak_boat
    91,  # pale_oak_chest_boat
    120,  # spruce_boat
    121,  # spruce_chest_boat
)
_CHEST_BOAT_ENTITIES_772 = (1, 8, 13, 23, 32, 73, 80, 86, 91, 121)
_RAFT_ENTITIES_772 = (8, 9)
_HAPPY_GHAST_ENTITY_772 = 56
_SHULKER_ENTITY_772 = 107

#: Boat 1.375 x 0.5625 is the unchanged vanilla AbstractBoat box; happy_ghast
#: is 4.0 x 4.0 and the shulker 1.0 x 1.0.  All three are the dimensions
#: minecraft-data records for 1.21.8 (entities.json width/height).
_HARD_COLLISION_ENTITIES_772 = (
    *((entity_id, 1.375, 0.5625) for entity_id in _BOAT_ENTITIES_772),
    (_HAPPY_GHAST_ENTITY_772, 4.0, 4.0),
    (_SHULKER_ENTITY_772, 1.0, 1.0),
)

_BOAT_ENTITIES = (
        0,
        1,
        8,
        9,
        12,
        13,
        23,
        24,
        33,
        34,
        74,
        75,
        81,
        82,
        89,
        90,
        94,
        95,
        125,
        126,
)

_CHEST_BOAT_ENTITIES = (1, 8, 13, 24, 34, 75, 82, 90, 95, 126)
_RAFT_ENTITIES = (8, 9)

_HARD_COLLISION_ENTITIES = (
    *((entity_id, 1.375, 0.5625) for entity_id in _BOAT_ENTITIES),
    (58, 4.0, 4.0),
    (112, 1.0, 1.0),
)

# 26.3 adds the cushion (33) and poplar boats (106/107) to the entity-type
# registry, which shifts every later ID (happy_ghast 58 -> 59, shulker
# 112 -> 115, spruce boats 125/126 -> 128/129, ...).
_BOAT_ENTITIES_777 = (
    0,
    1,
    8,
    9,
    12,
    13,
    23,
    24,
    34,
    35,
    75,
    76,
    82,
    83,
    90,
    91,
    95,
    96,
    106,
    107,
    128,
    129,
)
_CHEST_BOAT_ENTITIES_777 = (1, 8, 13, 24, 35, 76, 83, 91, 96, 107, 129)
_RAFT_ENTITIES_777 = (8, 9)
_HAPPY_GHAST_ENTITY_777 = 59
_SHULKER_ENTITY_777 = 115

_HARD_COLLISION_ENTITIES_777 = (
    *((entity_id, 1.375, 0.5625) for entity_id in _BOAT_ENTITIES_777),
    (_HAPPY_GHAST_ENTITY_777, 4.0, 4.0),
    (_SHULKER_ENTITY_777, 1.0, 1.0),
)


# Protocol 772 play ids.  Generated from the official 1.21.8 server bundle
# (``java -DbundlerMainClass=net.minecraft.data.Main -jar server-bundle.jar
# --reports``) with ``tools/gen_packet_ids.py --reports .../packets.json``;
# ``tests/data/packet-ids-772.json`` is the same table and audits every field
# below.  minecraft-data's 1.21.8 protocol.json agrees on every play field it
# defines (it omits this release's play-state transfer packet).
_PLAY_772 = PlayPacketIds(
    clientbound_add_entity=0x01,
    clientbound_remove_entities=0x46,
    clientbound_set_entity_data=0x5C,
    clientbound_set_entity_motion=0x5E,
    clientbound_set_equipment=0x5F,
    clientbound_set_passengers=0x64,
    clientbound_move_entity_pos=0x2E,
    clientbound_move_entity_pos_rot=0x2F,
    clientbound_move_entity_rot=0x31,
    clientbound_teleport_entity=0x76,
    clientbound_move_vehicle=0x32,
    clientbound_update_attributes=0x7C,
    clientbound_update_mob_effect=0x7D,
    clientbound_remove_mob_effect=0x47,
    clientbound_hurt_animation=0x24,
    clientbound_damage_event=0x19,
    clientbound_block_update=0x08,
    clientbound_section_blocks_update=0x4D,
    clientbound_block_changed_ack=0x04,
    clientbound_block_destruction=0x05,
    clientbound_chunk_data=0x27,  # level_chunk_with_light
    clientbound_unload_chunk=0x21,  # forget_level_chunk
    clientbound_chunk_batch_finished=0x0B,
    clientbound_login=0x2B,
    clientbound_disconnect=0x1C,
    clientbound_keep_alive=0x26,
    clientbound_ping=0x36,
    clientbound_game_event=0x22,
    clientbound_respawn=0x4B,
    clientbound_player_abilities=0x39,
    clientbound_position=0x41,  # player_position
    clientbound_start_configuration=0x6F,
    # The play-state transfer packet (identical shape to the configuration one).
    clientbound_transfer=0x7A,
    clientbound_sound=0x6E,
    clientbound_set_health=0x61,
    clientbound_player_combat_kill=0x3D,
    clientbound_player_info_update=0x3F,
    clientbound_player_info_remove=0x3E,
    clientbound_custom_payload=0x18,
    clientbound_player_chat=0x3A,
    clientbound_system_chat=0x72,
    clientbound_profileless_chat=0x1D,  # disguised_chat
    clientbound_container_close=0x11,
    clientbound_container_set_content=0x12,
    clientbound_container_set_slot=0x14,
    clientbound_container_set_data=0x13,
    clientbound_open_screen=0x34,
    clientbound_set_player_inventory=0x65,
    clientbound_set_held_slot=0x62,
    clientbound_set_cursor_item=0x59,
    # 1.21.8 still bundles every entity animation (arm swing, hurt, crit) into
    # the generic ``animate`` packet, which is 1.21.x's "animation".
    clientbound_swing_animation=0x02,
    # Absent in 772: ``add_transient_block`` is a 26.3 packet that replaces a
    # broken block without a second block update.  0 = not keyed here.
    clientbound_add_transient_block=0,
    serverbound_keep_alive=0x1B,
    serverbound_pong=0x2C,
    serverbound_client_command=0x0B,
    serverbound_player_abilities=0x27,
    serverbound_player_command=0x29,
    serverbound_player_input=0x2A,
    serverbound_player_loaded=0x2B,
    serverbound_configuration_acknowledged=0x0F,
    serverbound_teleport_confirm=0x00,  # accept_teleportation
    serverbound_custom_payload=0x15,
    serverbound_tick_end=0x0C,  # client_tick_end
    serverbound_chunk_batch_received=0x0A,
    serverbound_position=0x1D,  # move_player_pos
    serverbound_position_look=0x1E,  # move_player_pos_rot
    serverbound_look=0x1F,  # move_player_rot
    serverbound_flying=0x20,  # move_player_status_only
    serverbound_move_vehicle=0x21,
    serverbound_paddle_boat=0x22,
    serverbound_player_action=0x28,
    serverbound_use_item_on=0x3F,
    serverbound_interact=0x19,
    serverbound_swing=0x3C,
    # Absent in 772: 1.21.8 attacks through ``interact`` (mode 1), there is no
    # separate attack packet.  0 = not keyed here.
    serverbound_attack=0,
    serverbound_use_item=0x40,
    serverbound_container_slot_state_changed=0x13,
    serverbound_container_click=0x11,
    serverbound_container_close=0x12,
    serverbound_set_carried_item=0x34,
    serverbound_set_creative_mode_slot=0x37,
    serverbound_chat=0x08,
    serverbound_chat_ack=0x05,
    serverbound_chat_command=0x06,
    serverbound_chat_session_update=0x09,
)

_PLAY_774 = PlayPacketIds(
    clientbound_add_entity=0x01,
    clientbound_custom_payload=0x18,
    clientbound_container_close=0x11,
    clientbound_container_set_content=0x12,
    clientbound_container_set_slot=0x14,
    clientbound_open_screen=0x39,
    clientbound_profileless_chat=0x21,
    clientbound_remove_mob_effect=0x4C,
    clientbound_remove_entities=0x4B,
    clientbound_block_update=0x08,
    clientbound_chunk_batch_finished=0x0B,
    clientbound_chunk_data=0x2C,
    clientbound_disconnect=0x20,
    clientbound_game_event=0x26,
    clientbound_keep_alive=0x2B,
    clientbound_login=0x30,
    clientbound_move_vehicle=0x37,
    clientbound_move_entity_pos=0x33,
    clientbound_move_entity_pos_rot=0x34,
    clientbound_move_entity_rot=0x36,
    clientbound_ping=0x3B,
    clientbound_player_abilities=0x3E,
    clientbound_player_chat=0x3F,
    clientbound_position=0x46,
    clientbound_respawn=0x50,
    clientbound_section_blocks_update=0x52,
    clientbound_set_entity_data=0x61,
    clientbound_set_entity_motion=0x63,
    clientbound_set_equipment=0x64,
    clientbound_set_passengers=0x69,
    clientbound_set_player_inventory=0x6A,
    clientbound_start_configuration=0x74,
    clientbound_system_chat=0x77,
    clientbound_teleport_entity=0x7B,
    clientbound_transfer=0x7F,
    clientbound_unload_chunk=0x25,
    clientbound_update_attributes=0x81,
    clientbound_update_mob_effect=0x82,
    serverbound_chat_ack=0x05,
    serverbound_chunk_batch_received=0x0A,
    serverbound_chat=0x08,
    serverbound_chat_command=0x06,
    serverbound_chat_session_update=0x09,
    serverbound_custom_payload=0x15,
    serverbound_container_click=0x11,
    serverbound_container_close=0x12,
    serverbound_configuration_acknowledged=0x0F,
    serverbound_keep_alive=0x1B,
    serverbound_move_vehicle=0x21,
    serverbound_paddle_boat=0x22,
    serverbound_position=0x1D,
    serverbound_position_look=0x1E,
    serverbound_look=0x1F,
    serverbound_flying=0x20,
    serverbound_player_abilities=0x27,
    serverbound_player_command=0x29,
    serverbound_player_input=0x2A,
    serverbound_player_loaded=0x2B,
    serverbound_pong=0x2C,
    serverbound_set_carried_item=0x34,
    serverbound_tick_end=0x0C,
    serverbound_use_item=0x40,
    # Interaction ids, from minecraft-data's 1.21.11 table (test_packet_ids).
    serverbound_player_action=0x28,
    serverbound_use_item_on=0x3F,
    serverbound_interact=0x19,
    serverbound_swing=0x3C,
    serverbound_container_slot_state_changed=0x13,
    serverbound_set_creative_mode_slot=0x37,
    clientbound_block_changed_ack=0x04,
    clientbound_block_destruction=0x05,
    clientbound_swing_animation=0x02,
    clientbound_set_held_slot=0x67,
    clientbound_set_cursor_item=0x5E,
    clientbound_container_set_data=0x13,
    clientbound_hurt_animation=0x29,
    clientbound_damage_event=0x19,
    # Released with the interaction layer: previously left at 0 ("unverified")
    # even though the release data has always carried them.
    clientbound_sound=0x73,
    clientbound_set_health=0x66,
    clientbound_player_combat_kill=0x42,
    clientbound_player_info_update=0x44,
    clientbound_player_info_remove=0x43,
    serverbound_client_command=0x0B,
)

_PLAY_775_776 = PlayPacketIds(
    clientbound_sound=0x75,  # 116/117/119 = sound_entity/sound/stop_sound
    clientbound_set_health=0x68,  # 104: Float health, VarInt food, Float saturation
    clientbound_player_combat_kill=0x44,  # 68: VarInt entity id + death message
    serverbound_client_command=0x0C,  # 12: one VarInt, 0 = perform respawn
    # Interaction ids, from the 26.1 reports/packets.json.  26.1, 26.1.1 and
    # 26.1.2 share protocol 775 and agree on every one of these.
    serverbound_player_action=0x29,
    serverbound_use_item_on=0x42,
    serverbound_interact=0x1A,
    serverbound_swing=0x3F,  # renamed from "swing" to "punch" in 26.3 (0x2E)
    serverbound_attack=0x01,
    serverbound_container_slot_state_changed=0x14,
    serverbound_set_creative_mode_slot=0x38,
    clientbound_block_changed_ack=0x04,
    clientbound_block_destruction=0x05,
    clientbound_swing_animation=0x02,
    clientbound_set_held_slot=0x69,
    clientbound_set_cursor_item=0x60,
    clientbound_container_set_data=0x13,
    clientbound_hurt_animation=0x2A,
    clientbound_damage_event=0x19,
    # 69/70: right after combat_kill, followed by player_look_at (0x47) and
    # player_position (0x48) -- both neighbours are verified ids from this
    # very table, and there is no room left in between.
    clientbound_player_info_remove=0x45,
    clientbound_player_info_update=0x46,
    clientbound_add_entity=0x01,
    clientbound_custom_payload=0x18,
    clientbound_container_close=0x11,
    clientbound_container_set_content=0x12,
    clientbound_container_set_slot=0x14,
    clientbound_open_screen=0x3B,
    clientbound_profileless_chat=0x21,
    clientbound_remove_mob_effect=0x4E,
    clientbound_remove_entities=0x4D,
    clientbound_block_update=0x08,
    clientbound_chunk_batch_finished=0x0B,
    clientbound_chunk_data=0x2D,
    clientbound_disconnect=0x20,
    clientbound_game_event=0x26,
    clientbound_keep_alive=0x2C,
    clientbound_login=0x31,
    clientbound_move_vehicle=0x39,
    clientbound_move_entity_pos=0x35,
    clientbound_move_entity_pos_rot=0x36,
    clientbound_move_entity_rot=0x38,
    clientbound_ping=0x3D,
    clientbound_player_abilities=0x40,
    clientbound_player_chat=0x41,
    clientbound_position=0x48,
    clientbound_respawn=0x52,
    clientbound_section_blocks_update=0x54,
    clientbound_set_entity_data=0x63,
    clientbound_set_entity_motion=0x65,
    clientbound_set_equipment=0x66,
    clientbound_set_passengers=0x6B,
    clientbound_set_player_inventory=0x6C,
    clientbound_start_configuration=0x76,
    clientbound_system_chat=0x79,
    clientbound_teleport_entity=0x7D,
    clientbound_transfer=0x81,
    clientbound_unload_chunk=0x25,
    clientbound_update_attributes=0x83,
    clientbound_update_mob_effect=0x84,
    serverbound_chat_ack=0x06,
    serverbound_chunk_batch_received=0x0B,
    serverbound_chat=0x09,
    serverbound_chat_command=0x07,
    serverbound_chat_session_update=0x0A,
    serverbound_custom_payload=0x16,
    serverbound_container_click=0x12,
    serverbound_container_close=0x13,
    serverbound_configuration_acknowledged=0x10,
    serverbound_keep_alive=0x1C,
    serverbound_move_vehicle=0x22,
    serverbound_paddle_boat=0x23,
    serverbound_position=0x1E,
    serverbound_position_look=0x1F,
    serverbound_look=0x20,
    serverbound_flying=0x21,
    serverbound_player_abilities=0x28,
    serverbound_player_command=0x2A,
    serverbound_player_input=0x2B,
    serverbound_player_loaded=0x2C,
    serverbound_pong=0x2D,
    serverbound_set_carried_item=0x35,
    serverbound_tick_end=0x0D,
    serverbound_use_item=0x43,
)

# Generated from the 26.3 server's ``reports/packets.json``.  New clientbound
# packets (add_transient_block, swing_animation, ...) shift most IDs after
# 0x25.  Serverbound, ``punch`` is inserted at 0x2E and ``swing`` (formerly
# the last ID) is removed, so only IDs 0x2E..0x3E move -- of which ProtoBot
# uses set_carried_item.
_PLAY_777 = PlayPacketIds(
    clientbound_sound=0x77,
    # Interaction ids, from the 26.3 server's reports/packets.json.
    serverbound_player_action=0x29,
    serverbound_use_item_on=0x42,
    serverbound_interact=0x1A,
    serverbound_swing=0x2E,
    serverbound_attack=0x01,
    serverbound_container_slot_state_changed=0x14,
    serverbound_set_creative_mode_slot=0x39,
    clientbound_block_changed_ack=0x04,
    clientbound_block_destruction=0x05,
    clientbound_swing_animation=0x7B,
    clientbound_set_held_slot=0x6B,
    clientbound_set_cursor_item=0x62,
    clientbound_container_set_data=0x13,
    clientbound_hurt_animation=0x2B,
    clientbound_damage_event=0x19,
    clientbound_add_transient_block=0x25,
    clientbound_set_health=0x6A,
    clientbound_player_combat_kill=0x45,
    serverbound_client_command=0x0C,
    clientbound_player_info_remove=0x46,
    clientbound_player_info_update=0x47,
    clientbound_add_entity=0x01,
    clientbound_custom_payload=0x18,
    clientbound_container_close=0x11,
    clientbound_container_set_content=0x12,
    clientbound_container_set_slot=0x14,
    clientbound_open_screen=0x3C,
    clientbound_profileless_chat=0x21,
    clientbound_remove_mob_effect=0x4F,
    clientbound_remove_entities=0x4E,
    clientbound_block_update=0x08,
    clientbound_chunk_batch_finished=0x0B,
    clientbound_chunk_data=0x2E,
    clientbound_disconnect=0x20,
    clientbound_game_event=0x27,
    clientbound_keep_alive=0x2D,
    clientbound_login=0x32,
    clientbound_move_vehicle=0x3A,
    clientbound_move_entity_pos=0x36,
    clientbound_move_entity_pos_rot=0x37,
    clientbound_move_entity_rot=0x39,
    clientbound_ping=0x3E,
    clientbound_player_abilities=0x41,
    clientbound_player_chat=0x42,
    clientbound_position=0x49,
    clientbound_respawn=0x54,
    clientbound_section_blocks_update=0x56,
    clientbound_set_entity_data=0x65,
    clientbound_set_entity_motion=0x67,
    clientbound_set_equipment=0x68,
    clientbound_set_passengers=0x6D,
    clientbound_set_player_inventory=0x6E,
    clientbound_start_configuration=0x78,
    clientbound_system_chat=0x7C,
    clientbound_teleport_entity=0x80,
    clientbound_transfer=0x84,
    clientbound_unload_chunk=0x26,
    clientbound_update_attributes=0x86,
    clientbound_update_mob_effect=0x87,
    serverbound_chat_ack=0x06,
    serverbound_chunk_batch_received=0x0B,
    serverbound_chat=0x09,
    serverbound_chat_command=0x07,
    serverbound_chat_session_update=0x0A,
    serverbound_custom_payload=0x16,
    serverbound_container_click=0x12,
    serverbound_container_close=0x13,
    serverbound_configuration_acknowledged=0x10,
    serverbound_keep_alive=0x1C,
    serverbound_move_vehicle=0x22,
    serverbound_paddle_boat=0x23,
    serverbound_position=0x1E,
    serverbound_position_look=0x1F,
    serverbound_look=0x20,
    serverbound_flying=0x21,
    serverbound_player_abilities=0x28,
    serverbound_player_command=0x2A,
    serverbound_player_input=0x2B,
    serverbound_player_loaded=0x2C,
    serverbound_pong=0x2D,
    serverbound_set_carried_item=0x36,
    serverbound_tick_end=0x0D,
    serverbound_use_item=0x43,
)

SUPPORTED_VERSIONS: dict[str, VersionSpec] = {
    "1.21.8": VersionSpec(
        "1.21.8",
        772,
        # Mojang's own version.json (inside the 1.21.8 server bundle) reports
        # world_version 4440, which is the level.dat DataVersion.
        4440,
        _PLAY_772,
        # minecraft:item protocol_id in 1.21.8 reports/registries.json.
        ((826, "minecraft:elytra"), (916, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES_772,
        _BOAT_ENTITIES_772,
        _CHEST_BOAT_ENTITIES_772,
        _RAFT_ENTITIES_772,
        happy_ghast_entity=_HAPPY_GHAST_ENTITY_772,
        shulker_entity=_SHULKER_ENTITY_772,
        configuration=_CONFIGURATION_772,
    ),
    "1.21.11": VersionSpec(
        "1.21.11",
        774,
        4671,
        _PLAY_774,
        ((862, "minecraft:elytra"), (957, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
    "26.1": VersionSpec(
        "26.1",
        775,
        4786,
        _PLAY_775_776,
        ((863, "minecraft:elytra"), (958, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
    "26.1.1": VersionSpec(
        "26.1.1",
        775,
        4788,
        _PLAY_775_776,
        ((863, "minecraft:elytra"), (958, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
    "26.1.2": VersionSpec(
        "26.1.2",
        775,
        4790,
        _PLAY_775_776,
        ((863, "minecraft:elytra"), (958, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
    "26.2": VersionSpec(
        "26.2",
        776,
        4903,
        _PLAY_775_776,
        ((890, "minecraft:elytra"), (985, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
    "26.3": VersionSpec(
        "26.3",
        777,
        5023,
        _PLAY_777,
        ((974, "minecraft:elytra"), (1071, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES_777,
        _BOAT_ENTITIES_777,
        _CHEST_BOAT_ENTITIES_777,
        _RAFT_ENTITIES_777,
        happy_ghast_entity=_HAPPY_GHAST_ENTITY_777,
        shulker_entity=_SHULKER_ENTITY_777,
        configuration=_CONFIGURATION_777,
    ),
}


def get_version(version: str | VersionSpec) -> VersionSpec:
    if isinstance(version, VersionSpec):
        return version
    try:
        return SUPPORTED_VERSIONS[version]
    except KeyError as error:
        supported = ", ".join(SUPPORTED_VERSIONS)
        raise UnsupportedVersion(
            f"unsupported Minecraft version {version!r}; use {supported}"
        ) from error
