"""Canonical packet ids for the play/configuration states, and the map from
``VersionSpec.packets`` field names onto the names the two public data sources
use for them.

Every id in :mod:`protobot.protocol.versions` is hand written, which is fine
for correctness (a wrong id is a broken client) but expensive to extend and
impossible to audit.  This module is the audit half: it turns either

* a vanilla ``reports/packets.json`` (any release, generated with
  ``java -DbundlerMainClass=net.minecraft.data.Main -jar <server-bundle> --reports``), or
* a ``minecraft-data`` ``protocol.json`` (a separate, slower-moving corpus of
  the older releases)

into ``{field_name: packet_id}`` tables that can be compared against the ids
this library ships.  ``tools/gen_packet_ids.py`` writes those tables into
``tests/data/`` and ``tests/test_packet_ids.py`` compares them on every test
run, so a release bump that shifts an id fails a test instead of misbehaving
in game.

The two sources disagree about names, so each field carries both spellings:
vanilla renamed most of the protocol in 26.x (``block_dig`` became
``player_action``, ``acknowledge_player_digging`` became
``block_changed_ack``) while minecraft-data keeps the historical ones.  The
first spelling in each tuple is the vanilla one; the loader tries them in
order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: One side of a field's spelling: a single name, several vanilla spellings
#: (tried in order), or ``None`` when that source has no such packet yet.
Aliases = "str | tuple[str, ...] | None"

#: ``field name -> (vanilla name, minecraft-data name)``.
#:
#: The vanilla name is ``None`` when the source predates the packet; a field
#: that no source can resolve is reported instead of guessed.
CLIENTBOUND_FIELDS: dict[str, tuple[str | tuple[str, ...] | None, str | None]] = {
    # --- entities -------------------------------------------------------
    "clientbound_add_entity": ("add_entity", "spawn_entity"),
    "clientbound_remove_entities": ("remove_entities", "entity_destroy"),
    "clientbound_set_entity_data": ("set_entity_data", "entity_metadata"),
    "clientbound_set_entity_motion": ("set_entity_motion", "entity_velocity"),
    "clientbound_set_equipment": ("set_equipment", "entity_equipment"),
    "clientbound_set_passengers": ("set_passengers", "set_passengers"),
    "clientbound_move_entity_pos": ("move_entity_pos", "rel_entity_move"),
    "clientbound_move_entity_pos_rot": ("move_entity_pos_rot", "entity_move_look"),
    "clientbound_move_entity_rot": ("move_entity_rot", "entity_look"),
    "clientbound_teleport_entity": ("teleport_entity", "entity_teleport"),
    "clientbound_move_vehicle": ("move_vehicle", "vehicle_move"),
    "clientbound_update_attributes": ("update_attributes", "entity_update_attributes"),
    "clientbound_update_mob_effect": ("update_mob_effect", "entity_effect"),
    "clientbound_remove_mob_effect": ("remove_mob_effect", "remove_entity_effect"),
    # ``hurt_animation`` (the red flash) and ``damage_event`` (the actual hit)
    "clientbound_hurt_animation": ("hurt_animation", "hurt_animation"),
    "clientbound_damage_event": ("damage_event", "damage_event"),
    # --- world ----------------------------------------------------------
    "clientbound_block_update": ("block_update", "block_change"),
    #: Multi-block update. Called ``multi_block_change`` until 1.21.9 renamed
    #: it to ``section_blocks_update``; both carry the same payload.
    "clientbound_section_blocks_update": (
        "section_blocks_update",
        "multi_block_change",
    ),
    #: Sent by the server to acknowledge a ``player_action`` sequence number.
    #: The dig confirmation primitive rides on this.
    "clientbound_block_changed_ack": (
        "block_changed_ack",
        "acknowledge_player_digging",
    ),
    #: The break particle burst, sent to everyone but the breaking player.
    "clientbound_block_destruction": ("block_destruction", "block_break_animation"),
    #: Broken-block replacement for the 26.3 client (``add_transient_block``).
    "clientbound_add_transient_block": ("add_transient_block", None),
    "clientbound_chunk_data": ("level_chunk_with_light", "map_chunk"),
    "clientbound_unload_chunk": ("forget_level_chunk", "unload_chunk"),
    "clientbound_chunk_batch_finished": (
        "chunk_batch_finished",
        "chunk_batch_finished",
    ),
    # --- session --------------------------------------------------------
    "clientbound_login": ("login", "login"),
    "clientbound_disconnect": ("disconnect", "kick_disconnect"),
    "clientbound_keep_alive": ("keep_alive", "keep_alive"),
    "clientbound_ping": ("ping", "ping"),
    "clientbound_game_event": ("game_event", "game_state_change"),
    "clientbound_respawn": ("respawn", "respawn"),
    "clientbound_player_abilities": ("player_abilities", "abilities"),
    "clientbound_position": ("player_position", "position"),
    "clientbound_start_configuration": (
        "start_configuration",
        "start_configuration",
    ),
    "clientbound_transfer": ("transfer", "transfer"),
    "clientbound_sound": ("sound", "sound_effect"),
    "clientbound_set_health": ("set_health", "update_health"),
    # Combat/kill signals, and the tab-list packets behind player_join.
    "clientbound_player_combat_kill": ("player_combat_kill", "death_combat_event"),
    "clientbound_player_info_update": ("player_info_update", "player_info"),
    "clientbound_player_info_remove": ("player_info_remove", "player_remove"),
    "clientbound_custom_payload": ("custom_payload", "custom_payload"),
    # --- chat -----------------------------------------------------------
    "clientbound_player_chat": ("player_chat", "player_chat"),
    "clientbound_system_chat": ("system_chat", "system_chat"),
    "clientbound_profileless_chat": ("disguised_chat", "profileless_chat"),
    # --- inventory ------------------------------------------------------
    "clientbound_container_close": ("container_close", "close_window"),
    "clientbound_container_set_content": ("container_set_content", "window_items"),
    "clientbound_container_set_slot": ("container_set_slot", "set_slot"),
    "clientbound_container_set_data": ("container_set_data", "craft_progress_bar"),
    #: The stack on the cursor; 26.x and 1.21.x both send it as its own packet
    #: rather than as a ``container_set_slot`` for slot ``-1``.
    "clientbound_set_cursor_item": ("set_cursor_item", "set_cursor_item"),
    "clientbound_open_screen": ("open_screen", "open_window"),
    "clientbound_set_player_inventory": (
        "set_player_inventory",
        "set_player_inventory",
    ),
    #: Which hotbar slot the server believes is selected.
    "clientbound_set_held_slot": ("set_held_slot", "held_item_slot"),
    # --- animation ------------------------------------------------------
    #: Broadcast when *another* entity swings. 26.3 calls the packet
    #: ``swing_animation``; through 1.21.11 it is the generic animate packet,
    #: spelled ``animate`` in the vanilla reports and ``animation`` in
    #: minecraft-data.
    "clientbound_swing_animation": (("swing_animation", "animate"), "animation"),
}

SERVERBOUND_FIELDS: dict[str, tuple[str | tuple[str, ...] | None, str | None]] = {
    # --- session --------------------------------------------------------
    "serverbound_keep_alive": ("keep_alive", "keep_alive"),
    "serverbound_pong": ("pong", "pong"),
    "serverbound_client_command": ("client_command", "client_command"),
    "serverbound_player_abilities": ("player_abilities", "abilities"),
    #: Sprint/sneak/leave-bed style commands. minecraft-data still calls this
    #: ``entity_action`` from its pre-1.19 naming.
    "serverbound_player_command": ("player_command", "entity_action"),
    "serverbound_player_input": ("player_input", "player_input"),
    "serverbound_player_loaded": ("player_loaded", "player_loaded"),
    "serverbound_configuration_acknowledged": (
        "configuration_acknowledged",
        "configuration_acknowledged",
    ),
    "serverbound_teleport_confirm": ("accept_teleportation", "teleport_confirm"),
    "serverbound_custom_payload": ("custom_payload", "custom_payload"),
    #: 1.21.9+ end-of-tick marker; must be sent for the client to be
    #: considered ticking normally.
    "serverbound_tick_end": ("client_tick_end", "tick_end"),
    "serverbound_chunk_batch_received": (
        "chunk_batch_received",
        "chunk_batch_received",
    ),
    # --- movement -------------------------------------------------------
    "serverbound_position": ("move_player_pos", "position"),
    "serverbound_position_look": ("move_player_pos_rot", "position_look"),
    "serverbound_look": ("move_player_rot", "look"),
    "serverbound_flying": ("move_player_status_only", "flying"),
    "serverbound_move_vehicle": ("move_vehicle", "vehicle_move"),
    "serverbound_paddle_boat": ("paddle_boat", "steer_boat"),
    # --- interaction (the layer this fork exists for) -------------------
    #: ``PlayerActionPacket``: dig start/stop, drop item, release use item.
    "serverbound_player_action": ("player_action", "block_dig"),
    #: ``UseItemOnPacket``: right click a block face.
    "serverbound_use_item_on": ("use_item_on", "block_place"),
    #: ``InteractPacket``: attack / interact / interact-at an entity.
    "serverbound_interact": ("interact", "use_entity"),
    #: Arm swing. 1.21.11 and 26.1.x call it ``swing``; 26.3 renamed the same
    #: packet to ``punch`` and moved it from 0x3F to 0x2E.
    "serverbound_swing": (("punch", "swing"), "arm_animation"),
    #: 26.3 split the attack out of ``interact`` into its own packet.
    "serverbound_attack": ("attack", None),
    #: ``UseItemPacket``: right click with whatever the selected slot holds.
    "serverbound_use_item": ("use_item", "use_item"),
    #: Confirms a ``container_set_slot`` state id; the server resyncs the
    #: whole inventory when it is missing.
    "serverbound_container_slot_state_changed": (
        "container_slot_state_changed",
        "set_slot_state",
    ),
    # --- inventory ------------------------------------------------------
    "serverbound_container_click": ("container_click", "window_click"),
    "serverbound_container_close": ("container_close", "close_window"),
    "serverbound_set_carried_item": ("set_carried_item", "held_item_slot"),
    "serverbound_set_creative_mode_slot": (
        "set_creative_mode_slot",
        "set_creative_slot",
    ),
    # --- chat -----------------------------------------------------------
    "serverbound_chat": ("chat", "chat_message"),
    "serverbound_chat_ack": ("chat_ack", "message_acknowledgement"),
    "serverbound_chat_command": ("chat_command", "chat_command"),
    "serverbound_chat_session_update": ("chat_session_update", "chat_session_update"),
}

#: Configuration-state clientbound ids that actually move between releases.
CONFIGURATION_FIELDS: dict[str, tuple[str | tuple[str, ...] | None, str | None]] = {
    "clientbound_transfer": ("transfer", "transfer"),
    "clientbound_select_known_packs": ("select_known_packs", "select_known_packs"),
    "clientbound_code_of_conduct": ("code_of_conduct", "code_of_conduct"),
}


def _strip_namespace(name: str) -> str:
    return name.split(":", 1)[1] if ":" in name else name


def _mcdata_mapping(state: Mapping[str, Any], direction: str) -> dict[str, int]:
    """Pull ``{name: id}`` out of a minecraft-data ``types.packet`` mapper."""

    packet_type = state[direction]["types"]["packet"]
    for field in packet_type[1]:
        if field["name"] == "name":
            mapper = field["type"][1]["mappings"]
            return {name: int(pid, 16) for pid, name in mapper.items()}
    raise ValueError("minecraft-data packet type has no name mapper")


def from_reports(report: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Convert a vanilla ``reports/packets.json`` into ``{state: {dir: {name: id}}}``."""

    tables: dict[str, dict[str, int]] = {}
    for state, packet_id_key in (
        ("play", "play"),
        ("configuration", "configuration"),
    ):
        section = report[packet_id_key]
        for direction in ("clientbound", "serverbound"):
            tables[f"{state}.{direction}"] = {
                _strip_namespace(name): info["protocol_id"]
                for name, info in section[direction].items()
            }
    return tables


def from_minecraft_data(protocol: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Convert a minecraft-data ``protocol.json`` into the same table shape."""

    tables: dict[str, dict[str, int]] = {}
    version = protocol.get("version", {}).get("protocolVersion")
    # minecraft-data keeps an old-style top level ("play") and, since the
    # 1.21.9 reshuffle, the versioned name; accept both.
    play = protocol.get("play")
    if play is None:
        for key, value in protocol.items():
            if isinstance(value, Mapping) and "toClient" in value:
                play = value
                break
    if play is None:
        raise ValueError("minecraft-data protocol.json has no play state")
    for state, direction in (("play", "toClient"), ("play", "toServer")):
        name = "clientbound" if direction == "toClient" else "serverbound"
        tables[f"{state}.{name}"] = _mcdata_mapping(play, direction)
    configuration = protocol.get("configuration")
    if configuration:
        tables["configuration.clientbound"] = _mcdata_mapping(
            configuration, "toClient"
        )
        tables["configuration.serverbound"] = _mcdata_mapping(
            configuration, "toServer"
        )
    if version is not None:
        tables.setdefault("version", {})["protocol"] = int(version)
    return tables


def load(source: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Detect the source shape and convert it."""

    if "play" in source and isinstance(source["play"], Mapping):
        play = source["play"]
        if "clientbound" in play:  # vanilla reports
            return from_reports(source)
        if "toClient" in play:  # minecraft-data
            return from_minecraft_data(source)
    raise ValueError("unrecognised packet table source")


def _side(aliases: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalise one side of a field's spelling to a tuple of names."""

    if aliases is None:
        return ()
    if isinstance(aliases, str):
        return (aliases,)
    return tuple(aliases)


def _names(entry: tuple[Any, Any]) -> tuple[str, ...]:
    """Every spelling of a field, vanilla side first, then minecraft-data's."""

    return _side(entry[0]) + _side(entry[1])


def _resolve(
    table: Mapping[str, int], entry: tuple[Any, Any]
) -> tuple[int | None, str | None]:
    for alias in _names(entry):
        if alias in table:
            return table[alias], alias
    return None, None


def field_ids(
    tables: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Resolve every known field against ``tables``.

    Returns ``(resolved, missing)`` where ``resolved`` is
    ``{field_name: {"play": id} | {"configuration": id}}`` in a form that can be
    compared attribute-by-attribute against ``VersionSpec.packets``, and
    ``missing`` lists fields neither spelling matched (a release genuinely
    dropped the packet, or an alias needs adding).
    """

    resolved: dict[str, dict[str, int]] = {}
    missing: list[str] = []
    groups = (
        ("play", "clientbound", CLIENTBOUND_FIELDS),
        ("play", "serverbound", SERVERBOUND_FIELDS),
        ("configuration", "clientbound", CONFIGURATION_FIELDS),
    )
    for state, direction, fields in groups:
        table = tables.get(f"{state}.{direction}", {})
        for field_name, aliases in fields.items():
            packet_id, _ = _resolve(table, aliases)
            if packet_id is None:
                missing.append(field_name)
            else:
                # ``clientbound_transfer`` is a play *and* a configuration
                # packet, so a hard assignment here would drop its play id
                # (and make ``--emit`` raise KeyError).  Keep both states.
                resolved.setdefault(field_name, {})[state] = packet_id
    return resolved, missing


def unmapped_packets(
    tables: Mapping[str, Mapping[str, int]],
) -> dict[str, list[tuple[str, int]]]:
    """Names a source knows about that no ``PlayPacketIds`` field claims.

    Printed by the generator so that a release which adds a packet the library
    does not model yet shows up as a one-line addition instead of a mystery.
    """

    claimed: dict[str, set[str]] = {}
    groups = (
        ("play.clientbound", CLIENTBOUND_FIELDS),
        ("play.serverbound", SERVERBOUND_FIELDS),
        ("configuration.clientbound", CONFIGURATION_FIELDS),
    )
    for key, fields in groups:
        names: set[str] = set()
        for aliases in fields.values():
            names.update(_names(aliases))
        claimed[key] = names
    unmapped: dict[str, list[tuple[str, int]]] = {}
    for key, table in tables.items():
        if key not in claimed:
            continue
        leftovers = sorted(
            ((name, pid) for name, pid in table.items() if name not in claimed[key]),
            key=lambda row: row[1],
        )
        unmapped[key] = leftovers
    return unmapped
