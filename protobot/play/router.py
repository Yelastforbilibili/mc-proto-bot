"""Clientbound packet handlers for the play state.

The play state is a stream of packets, so "which packet sets which field" is
answered by this module and nowhere else.  Handlers decode, update the local
model and emit an event; anything that has to be *sent* belongs to another
concern.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
import uuid
from collections.abc import Iterable

from ..errors import ConnectionClosed, LoginRejected, OnlineModeRequired, ProtocolError
from ..log import error as log_error, trace as log_trace
from ..physics import AABB, StatusEffect, Vec3
from ..physics.math import f32, minecraft_cos, minecraft_sin
from ..protocol.codec import PacketReader, PacketWriter
from ..protocol.connection import ConnectionState, ProtocolConnection, RawPacket
from ..protocol.nbt import read_anonymous_nbt
from ..protocol.stacks import StackFrameError, read_stack, read_stacks
from ..state import (
    ContainerState,
    EntityMetadataValue,
    EntityState,
    EquipmentSlot,
    ItemStack,
    PlayerAbilities,
    PlayerListEntry,
)
from ..support import (
    AttributeModifierUpdate,
    AttributeUpdate,
    PLAYER_CONTAINER_ID,
    PLAYER_INVENTORY_LAST_SLOT,
    _CHANGE_GAME_MODE_EVENT,
    _CHAT_ACKNOWLEDGEMENT_THRESHOLD,
    _MAX_CONTAINER_SLOTS,
    _MOVEMENT_ATTRIBUTES,
    _MOVEMENT_ATTRIBUTE_RANGES,
    _MOVEMENT_EFFECTS,
    _PLAYER_INFO_ADD,
    _PLAYER_INFO_DISPLAY_NAME,
    _PLAYER_INFO_GAME_MODE,
    _PLAYER_INFO_HAT,
    _PLAYER_INFO_INIT_CHAT,
    _PLAYER_INFO_LATENCY,
    _PLAYER_INFO_LISTED,
    _PLAYER_INFO_LIST_ORDER,
    _ROSTER_GRACE,
    _SPRINTING_SPEED_MODIFIER,
    _TRACE_POS,
    _UnsupportedEntityMetadata,
    _UnsupportedItemComponents,
    _parse_chat_text,
)
from ..text import plain_text


class PlayRouterMixin:
    """Methods of :class:`protobot.Bot`, grouped by concern."""

    async def _handle_play(self, packet: RawPacket) -> None:
        ids = self.version.packets
        if packet.packet_id == ids.clientbound_disconnect:
            reason = self._best_effort_reason(packet.payload)
            self.disconnect_reason = reason
            raw = packet.payload.hex()
            if len(raw) > 512:
                raw = raw[:512] + "..."
            log_error(
                f"[disconnect] server kick reason={reason!r} "
                f"payload_hex={raw}"
            )
            raise ConnectionClosed(reason)
        if packet.packet_id == ids.clientbound_container_close:
            await self._handle_container_close(packet.payload)
        elif packet.packet_id == ids.clientbound_container_set_content:
            await self._handle_container_content(packet.payload)
        elif packet.packet_id == ids.clientbound_container_set_slot:
            await self._handle_container_slot(packet.payload)
        elif ids.clientbound_container_set_data and (
            packet.packet_id == ids.clientbound_container_set_data
        ):
            await self._handle_container_set_data(packet.payload)
        elif ids.clientbound_set_cursor_item and (
            packet.packet_id == ids.clientbound_set_cursor_item
        ):
            await self._handle_set_cursor_item(packet.payload)
        elif packet.packet_id == ids.clientbound_open_screen:
            await self._handle_open_screen(packet.payload)
        elif packet.packet_id == ids.clientbound_add_entity:
            await self._handle_add_entity(packet.payload)
        elif packet.packet_id == ids.clientbound_player_abilities:
            await self._handle_player_abilities(packet.payload)
        elif packet.packet_id == ids.clientbound_game_event:
            await self._handle_game_event(packet.payload)
        elif packet.packet_id == ids.clientbound_block_update:
            await self._handle_block_update(packet.payload)
        elif (
            ids.clientbound_block_changed_ack
            and packet.packet_id == ids.clientbound_block_changed_ack
        ):
            await self._handle_block_changed_ack(packet.payload)
        elif packet.packet_id == ids.clientbound_custom_payload:
            reader = PacketReader(packet.payload)
            channel = reader.read_string(max_chars=32767)
            data = reader.read_remaining()
            await self.events.emit("play_payload", channel, data)
            response = self.modlist.handle_response(channel, data)
            if response is not None:
                await self.send_play_payload(response.channel, response.data)
        elif packet.packet_id == ids.clientbound_chunk_batch_finished:
            await self._handle_chunk_batch_finished(packet.payload)
        elif packet.packet_id == ids.clientbound_chunk_data:
            await self._handle_chunk_data(packet.payload)
        elif packet.packet_id == ids.clientbound_keep_alive:
            reader = PacketReader(packet.payload)
            value = reader.read_long()
            reader.expect_end()
            response = PacketWriter().write_long(value).to_bytes()
            await self.send_raw(ids.serverbound_keep_alive, response)
        elif packet.packet_id == ids.clientbound_login:
            self._handle_play_login(packet.payload)
            await self.events.emit("world", self.session)
        elif packet.packet_id == ids.clientbound_move_vehicle:
            await self._handle_move_vehicle(packet.payload)
        elif packet.packet_id == ids.clientbound_move_entity_pos:
            await self._handle_move_entity(packet.payload, position=True, rotation=False)
        elif packet.packet_id == ids.clientbound_move_entity_pos_rot:
            await self._handle_move_entity(packet.payload, position=True, rotation=True)
        elif packet.packet_id == ids.clientbound_move_entity_rot:
            await self._handle_move_entity(packet.payload, position=False, rotation=True)
        elif (
            ids.clientbound_entity_position_sync
            and packet.packet_id == ids.clientbound_entity_position_sync
        ):
            await self._handle_entity_position_sync(packet.payload)
        elif packet.packet_id == ids.clientbound_ping:
            reader = PacketReader(packet.payload)
            value = reader.read_int()
            reader.expect_end()
            # A pong is the transaction an anti-cheat judges the packets queued
            # before it at, and only a movement packet clears that queue.  An
            # action sits in the gap between the last movement packet and its own
            # tick's movement packet for a moment (swing now, move next), so a
            # pong answered there makes the swing look out of order.  Hold the
            # answer until that tick has gone out -- and keep the answers in the
            # order the pings arrived, because jumping that queue is a check of
            # its own.
            holding = bool(self._pong_pending) or time.monotonic() < getattr(
                self, "_action_busy_until", 0.0
            )
            if holding:
                self._pong_pending.append(value)
            else:
                await self.send_raw(
                    ids.serverbound_pong, PacketWriter().write_int(value).to_bytes()
                )
        elif packet.packet_id == ids.clientbound_player_chat:
            await self._handle_player_chat(packet.payload)
        elif packet.packet_id == ids.clientbound_profileless_chat:
            await self._handle_profileless_chat(packet.payload)
        elif packet.packet_id == ids.clientbound_position:
            await self._handle_position(packet.payload)
        elif packet.packet_id == ids.clientbound_remove_mob_effect:
            await self._handle_remove_mob_effect(packet.payload)
        elif packet.packet_id == ids.clientbound_remove_entities:
            await self._handle_remove_entities(packet.payload)
        elif packet.packet_id == ids.clientbound_respawn:
            self._handle_respawn(packet.payload)
            await self.events.emit("respawn", self.session)
        elif (
            ids.clientbound_player_combat_kill
            and packet.packet_id == ids.clientbound_player_combat_kill
        ):
            await self._handle_player_combat_kill(packet.payload)
        elif ids.clientbound_set_health and packet.packet_id == ids.clientbound_set_health:
            await self._handle_set_health(packet.payload)
        elif (
            ids.clientbound_set_experience
            and packet.packet_id == ids.clientbound_set_experience
        ):
            self._handle_set_experience(packet.payload)
            await self.events.emit("experience", self.player)
        elif (
            ids.clientbound_player_info_update
            and packet.packet_id == ids.clientbound_player_info_update
        ):
            await self._handle_player_info_update(packet.payload)
        elif (
            ids.clientbound_player_info_remove
            and packet.packet_id == ids.clientbound_player_info_remove
        ):
            await self._handle_player_info_remove(packet.payload)
        elif packet.packet_id == ids.clientbound_section_blocks_update:
            await self._handle_section_blocks_update(packet.payload)
        elif packet.packet_id == ids.clientbound_set_entity_data:
            await self._handle_set_entity_data(packet.payload)
        elif packet.packet_id == ids.clientbound_set_entity_motion:
            await self._handle_set_entity_motion(packet.payload)
        elif packet.packet_id == ids.clientbound_set_equipment:
            await self._handle_set_equipment(packet.payload)
        elif packet.packet_id == ids.clientbound_set_passengers:
            await self._handle_set_passengers(packet.payload)
        elif (
            ids.clientbound_swing_animation
            and packet.packet_id == ids.clientbound_swing_animation
        ):
            await self._handle_swing_animation(packet.payload)
        elif packet.packet_id == ids.clientbound_set_player_inventory:
            await self._handle_set_player_inventory(packet.payload)
        elif packet.packet_id == ids.clientbound_start_configuration:
            await self._send_chat_acknowledgement()
            await self.send_raw(ids.serverbound_configuration_acknowledged)
            self._reset_server_state()
            self.state = ConnectionState.CONFIGURATION
            await self.events.emit("reconfiguration", self)
            await self._send_client_information()
        elif packet.packet_id == ids.clientbound_system_chat:
            await self._handle_system_chat(packet.payload)
        elif packet.packet_id == ids.clientbound_unload_chunk:
            reader = PacketReader(packet.payload)
            chunk_x, chunk_z = reader.read_chunk_pos()
            reader.expect_end()
            self.world.unload_chunk(chunk_x, chunk_z)
            await self.events.emit("chunk_unload", chunk_x, chunk_z)
        elif packet.packet_id == ids.clientbound_teleport_entity:
            await self._handle_teleport_entity(packet.payload)
        elif packet.packet_id == ids.clientbound_transfer:
            await self._handle_transfer(packet.payload)
        elif packet.packet_id == ids.clientbound_update_attributes:
            await self._handle_update_attributes(packet.payload)
        elif packet.packet_id == ids.clientbound_update_mob_effect:
            await self._handle_update_mob_effect(packet.payload)

    async def _handle_login(self, packet: RawPacket) -> None:
        if packet.packet_id == 0x00:
            raise LoginRejected(self._best_effort_reason(packet.payload))
        if packet.packet_id == 0x01:
            await self._handle_encryption_request(packet.payload)
            return
        if packet.packet_id == 0x02:
            self._read_login_success(packet.payload)
            await self.send_raw(0x03)
            self.state = ConnectionState.CONFIGURATION
            await self._send_client_information()
            await self.events.emit("login", self)
            return
        if packet.packet_id == 0x03:
            reader = PacketReader(packet.payload)
            threshold = reader.read_varint()
            reader.expect_end()
            if threshold < 0:
                raise ProtocolError("negative compression threshold")
            self._connection.compression_threshold = threshold
            return
        if packet.packet_id == 0x04:
            reader = PacketReader(packet.payload)
            message_id = reader.read_varint()
            channel = reader.read_string(max_chars=32767)
            data = reader.read_remaining()
            await self.events.emit("login_plugin_request", channel, data)
            if channel == "velocity:player_info" and self._velocity_secret is not None:
                response_data = self._velocity_forwarding_payload()
            else:
                response_data = self.modlist.handle(channel, data)
            response = PacketWriter().write_varint(message_id).write_bool(
                response_data is not None
            )
            if response_data is not None:
                response.write_raw(response_data)
            await self.send_raw(0x02, response.to_bytes())
            return
        if packet.packet_id == 0x05:
            reader = PacketReader(packet.payload)
            key = reader.read_string(max_chars=32767)
            reader.expect_end()
            await self.events.emit("cookie_request", key)
            response = PacketWriter().write_string(key).write_bool(False).to_bytes()
            await self.send_raw(0x04, response)

    async def _handle_encryption_request(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        server_id = reader.read_string(max_chars=20)
        public_key = reader.read_bytes(max_length=1024)
        verify_token = reader.read_bytes(max_length=1024)
        should_authenticate = reader.read_bool() if self.version.protocol >= 766 and reader.remaining else True
        reader.expect_end()

        shared_secret = secrets.token_bytes(16)

        authenticated = False
        if should_authenticate:
            if not self.access_token:
                raise OnlineModeRequired(
                    "Server requested online-mode authentication, but no access_token was provided. "
                    "Provide access_token to connect() / Bot() or use device_code_login()."
                )
            from ..auth import join_session_server, minecraft_sha1_digest

            server_hash = minecraft_sha1_digest(server_id, shared_secret, public_key)
            await asyncio.gather(
                join_session_server(
                    self.access_token,
                    self.uuid,
                    server_hash,
                    self.session_server,
                ),
                self._prepare_chat_certificate(),
            )
            authenticated = True

        from ..auth import rsa_encrypt

        encrypted_secret = rsa_encrypt(public_key, shared_secret)
        encrypted_token = rsa_encrypt(public_key, verify_token)

        response = (
            PacketWriter()
            .write_bytes(encrypted_secret)
            .write_bytes(encrypted_token)
            .to_bytes()
        )
        await self.send_raw(0x01, response)
        self._connection.enable_encryption(shared_secret)
        self._authenticated_connection = authenticated

    async def _handle_configuration(self, packet: RawPacket) -> None:
        # IDs 0x00-0x07 are stable; later clientbound IDs moved in 26.3.
        configuration = self.version.configuration
        if packet.packet_id == 0x00:
            reader = PacketReader(packet.payload)
            key = reader.read_string(max_chars=32767)
            response = PacketWriter().write_string(key).write_bool(False).to_bytes()
            await self.send_raw(0x01, response)
        elif packet.packet_id == 0x01:
            reader = PacketReader(packet.payload)
            channel = reader.read_string(max_chars=32767)
            data = reader.read_remaining()
            await self.events.emit("configuration_payload", channel, data)
            await self.events.emit("mod_payload", channel, data)
            response = self.modlist.handle_response(channel, data)
            if response is not None:
                await self.send_configuration_payload(response.channel, response.data)
        elif packet.packet_id == 0x02:
            raise LoginRejected(self._best_effort_reason(packet.payload))
        elif packet.packet_id == 0x03:
            await self.send_raw(0x03)
            self.state = ConnectionState.PLAY
            await self._activate_chat_session()
            self.ready.set()
            await self.events.emit("ready", self)
        elif packet.packet_id == 0x04:
            reader = PacketReader(packet.payload)
            value = reader.read_long()
            reader.expect_end()
            await self.send_raw(0x04, PacketWriter().write_long(value).to_bytes())
        elif packet.packet_id == 0x05:
            reader = PacketReader(packet.payload)
            value = reader.read_int()
            reader.expect_end()
            await self.send_raw(0x05, PacketWriter().write_int(value).to_bytes())
        elif packet.packet_id == 0x07:
            registry_id = self.registries.apply_packet(packet.payload)
            await self.events.emit("registry", registry_id)
        elif packet.packet_id == configuration.clientbound_select_known_packs:
            await self.send_raw(0x07, PacketWriter().write_varint(0).to_bytes())
        elif packet.packet_id == configuration.clientbound_code_of_conduct:
            await self.send_raw(0x09)
        elif packet.packet_id == configuration.clientbound_transfer:
            await self._handle_transfer(packet.payload)

    def _handle_play_login(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        # A fresh PLAY state: the player list starts empty, and whatever
        # arrives next is the initial roster.
        self.players.clear()
        self._roster_synced = False
        self._roster_deadline = time.monotonic() + _ROSTER_GRACE
        self.session.entity_id = reader.read_int()
        self.session.hardcore = reader.read_bool()
        level_count = reader.read_varint()
        if not 0 <= level_count <= 1024:
            raise ProtocolError(f"invalid level count {level_count}")
        for _ in range(level_count):
            reader.read_string(max_chars=32767)
        reader.read_varint()  # Max players is retained by the server for compatibility.
        self.session.view_distance = reader.read_varint()
        self.session.simulation_distance = reader.read_varint()
        reader.read_bool()  # Reduced debug info.
        reader.read_bool()  # Show death screen.
        reader.read_bool()  # Limited crafting.
        self._read_spawn_info(reader)
        if self.version.protocol >= 776:
            self.session.online_mode = reader.read_bool()
        self.session.enforces_secure_chat = reader.read_bool()
        reader.expect_end()

    def _read_login_success(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        self.uuid = reader.read_uuid()
        self.username = reader.read_string(max_chars=16)
        property_count = reader.read_varint()
        if property_count < 0 or property_count > 1024:
            raise ProtocolError("invalid login property count")
        for _ in range(property_count):
            reader.read_string()
            reader.read_string()
            if reader.read_bool():
                reader.read_string()
        if self.version.protocol >= 776:
            self.session_id = reader.read_uuid()
        reader.expect_end()

    async def _handle_transfer(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        host = reader.read_string(max_chars=32767)
        port = reader.read_varint()
        reader.expect_end()
        if not host:
            raise ProtocolError("transfer host must not be empty")
        if not 0 < port < 65536:
            raise ProtocolError(f"invalid transfer port {port}")
        if not self.accept_transfers:
            await self.events.emit("transfer", host, port)
            reason = f"server requested transfer to {host}:{port}"
            self.disconnect_reason = reason
            raise ConnectionClosed(reason)

        max_packet_size = self._connection.max_packet_size
        await self._connection.close()
        self._connection = ProtocolConnection(max_packet_size=max_packet_size)
        self._reset_server_state()
        self._authenticated_connection = False
        self.state = ConnectionState.DISCONNECTED
        self.host = host
        self.port = port
        self.handshake_host = host
        self.disconnect_reason = None
        await self.events.emit("transfer", host, port)
        await self._open_login_connection(host, port, host)

    async def _handle_player_abilities(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        flags = reader.read_unsigned_byte()
        fly_speed = reader.read_float()
        walk_speed = reader.read_float()
        reader.expect_end()
        if not math.isfinite(fly_speed) or not math.isfinite(walk_speed):
            raise ProtocolError("player ability speeds must be finite")
        abilities = PlayerAbilities(
            invulnerable=bool(flags & 0x01),
            flying=bool(flags & 0x02),
            allow_flying=bool(flags & 0x04),
            instant_build=bool(flags & 0x08),
            fly_speed=fly_speed,
            walk_speed=walk_speed,
        )
        self.player.abilities = abilities
        self.physics_state.allow_flying = abilities.allow_flying
        self.physics_state.fly_speed = abilities.fly_speed
        if self._force_flight_active:
            self._set_physics_flying(True)
        else:
            self._set_local_flying(abilities.flying)
        await self.events.emit("abilities", abilities)

    async def _handle_system_chat(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        component = read_anonymous_nbt(reader)
        overlay = reader.read_bool()
        reader.expect_end()
        await self.events.emit("system_chat", component, overlay)

    def _read_chat_type_holder(self, reader: PacketReader) -> int | None:
        """Read a ChatType holder: a registry id, or an inline custom type.

        Returns the registry id, or ``None`` when the server inlined a custom
        chat type (holder value 0); the inline definition is validated and
        skipped, since decoration is left to the caller.
        """

        value = reader.read_varint()
        if value != 0:
            return value - 1
        reader.read_string(max_chars=32767)  # translation key
        parameter_count = reader.read_varint()
        if parameter_count < 0 or parameter_count > 16:
            raise ProtocolError(f"invalid chat type parameter count {parameter_count}")
        for _ in range(parameter_count):
            reader.read_varint()
        read_anonymous_nbt(reader)  # style
        return None

    async def _handle_player_chat(self, payload: bytes) -> None:
        """Decode a signed player chat message and emit ``player_chat``.

        Layout per the 1.21.5+ protocol (verified against MCProtocolLib for
        protocol 776): global index, sender, index, optional signature, the
        message body, timestamp, salt, last-seen signatures, optional unsigned
        content, a filter mask, a ChatType holder, then the name and optional
        target name components.
        """

        reader = PacketReader(payload)
        reader.read_varint()  # global index
        sender = reader.read_uuid()
        reader.read_varint()  # per-sender index
        signature = reader.read_raw(256) if reader.read_bool() else None
        content = reader.read_string(max_chars=256)
        reader.read_long()  # timestamp
        reader.read_long()  # salt
        seen_count = reader.read_varint()
        if seen_count < 0 or seen_count > 20:
            raise ProtocolError(f"invalid last-seen message count {seen_count}")
        for _ in range(seen_count):
            if reader.read_varint() == 0:
                reader.read_raw(256)  # previous message signature
        unsigned_content = None
        if reader.read_bool():
            unsigned_content = read_anonymous_nbt(reader)
        filter_mask = reader.read_varint()
        if filter_mask == 2:
            self._skip_bit_set(reader)
        chat_type_id = self._read_chat_type_holder(reader)
        name = read_anonymous_nbt(reader)
        target_name = read_anonymous_nbt(reader) if reader.read_bool() else None
        reader.expect_end()

        # Mark the message before dispatch. A plugin commonly replies from its
        # event handler, and that reply must already acknowledge this message.
        if signature is not None:
            added = self._last_seen_messages.add_pending(signature)
            if (
                added
                and self._last_seen_messages.offset
                > _CHAT_ACKNOWLEDGEMENT_THRESHOLD
            ):
                await self._send_chat_acknowledgement()
        message = unsigned_content if unsigned_content is not None else _parse_chat_text(content)
        await self.events.emit("player_chat", sender, name, message, chat_type_id, target_name)

    async def _send_chat_acknowledgement(self) -> None:
        """Flush pending signed-chat offsets without sending a chat message."""

        async with self._chat_send_lock:
            offset = self._last_seen_messages.take_offset()
            if offset:
                await self.send_raw(
                    self.version.packets.serverbound_chat_ack,
                    PacketWriter().write_varint(offset).to_bytes(),
                )

    async def _handle_profileless_chat(self, payload: bytes) -> None:
        """Decode a profileless (unsigned) chat message and emit ``player_chat``.

        Servers that do not enforce chat signing broadcast this packet instead
        of the signed variant; the sender uuid is therefore ``None``.
        """

        reader = PacketReader(payload)
        message = read_anonymous_nbt(reader)
        chat_type_id = self._read_chat_type_holder(reader)
        name = read_anonymous_nbt(reader)
        target_name = read_anonymous_nbt(reader) if reader.read_bool() else None
        reader.expect_end()
        await self.events.emit("player_chat", None, name, message, chat_type_id, target_name)

    async def _handle_game_event(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        event_id = reader.read_unsigned_byte()
        value = reader.read_float()
        reader.expect_end()
        if not math.isfinite(value):
            raise ProtocolError("game event value must be finite")
        if event_id == _CHANGE_GAME_MODE_EVENT:
            game_mode = int(value)
            if value != game_mode or not 0 <= game_mode <= 3:
                raise ProtocolError(f"invalid game mode event value {value!r}")
            self.session.previous_game_mode = self.session.game_mode
            self._set_game_mode(game_mode)
            await self.events.emit("game_mode", game_mode)
        await self.events.emit("game_event", event_id, value)

    async def _handle_block_update(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        x, y, z = reader.read_position()
        state_id = reader.read_varint()
        reader.expect_end()
        self.world.set_state_id(x, y, z, state_id)
        await self.events.emit("block_update", x, y, z, state_id)

    async def _handle_block_changed_ack(self, payload: bytes) -> None:
        """The server confirming it received a ``PlayerAction`` sequence.

        The packet carries the sequence number only: the resulting block state
        arrives separately as a ``block_update``.  It is still the earliest
        proof that an action reached the server, so it is surfaced as an event
        and never used to mutate the world.
        """

        reader = PacketReader(payload)
        sequence = reader.read_varint()
        reader.expect_end()
        await self.events.emit("block_changed_ack", sequence)

    async def _handle_chunk_batch_finished(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        batch_size = reader.read_varint()
        reader.expect_end()
        if batch_size < 0:
            raise ProtocolError("negative chunk batch size")
        response = PacketWriter().write_float(7.0).to_bytes()
        await self.send_raw(self.version.packets.serverbound_chunk_batch_received, response)
        await self.events.emit("chunk_batch", batch_size)

    async def _handle_chunk_data(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        chunk_x = reader.read_int()
        chunk_z = reader.read_int()
        heightmap_count = reader.read_varint()
        if not 0 <= heightmap_count <= 64:
            raise ProtocolError(f"invalid heightmap count {heightmap_count}")
        for _ in range(heightmap_count):
            reader.read_varint()
            self._skip_long_array(reader, limit=256)
        chunk_data = reader.read_bytes(max_length=2 * 1024 * 1024)

        block_entity_count = reader.read_varint()
        if not 0 <= block_entity_count <= 65536:
            raise ProtocolError(f"invalid block entity count {block_entity_count}")
        for _ in range(block_entity_count):
            reader.read_byte()
            reader.read_short()
            reader.read_varint()
            read_anonymous_nbt(reader)

        for _ in range(4):
            self._skip_bit_set(reader)
        for _ in range(2):
            light_count = reader.read_varint()
            if not 0 <= light_count <= 1024:
                raise ProtocolError(f"invalid light array count {light_count}")
            for _ in range(light_count):
                reader.read_bytes(max_length=2048)
        reader.expect_end()

        chunk = self.world.load_chunk(chunk_x, chunk_z, chunk_data)
        if not self.world_ready.is_set():
            self.world_ready.set()
            await self.events.emit("world_ready", self.world)
        await self.events.emit("chunk", chunk)

    async def _handle_section_blocks_update(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        packed_section = reader.read_unsigned_long()
        section_x = self._signed_bits(packed_section >> 42, 22)
        section_z = self._signed_bits((packed_section >> 20) & 0x3FFFFF, 22)
        section_y = self._signed_bits(packed_section & 0xFFFFF, 20)
        count = reader.read_varint()
        if not 0 <= count <= 4096:
            raise ProtocolError(f"invalid section block update count {count}")
        updates: list[tuple[int, int, int, int]] = []
        for _ in range(count):
            record = reader.read_varlong()
            state_id = record >> 12
            local = record & 0xFFF
            x = (section_x << 4) + ((local >> 8) & 0xF)
            y = (section_y << 4) + (local & 0xF)
            z = (section_z << 4) + ((local >> 4) & 0xF)
            self.world.set_state_id(x, y, z, state_id)
            updates.append((x, y, z, state_id))
        reader.expect_end()
        await self.events.emit("section_blocks_update", updates)

    @staticmethod
    def _unpack_degrees(value: int) -> float:
        return f32(value * (360.0 / 256.0))

    @staticmethod
    def _validate_finite(values: Iterable[float], description: str) -> None:
        if not all(math.isfinite(value) for value in values):
            raise ProtocolError(f"{description} must be finite")

    def _entity_for_state(self, entity_id: int) -> EntityState:
        entity = self.entities.get(entity_id)
        if entity is None:
            entity = EntityState(entity_id)
            self.entities[entity_id] = entity
        return entity

    def _vehicle_passenger_ids(self, vehicle_id: int) -> tuple[int, ...]:
        if vehicle_id == self.session.entity_id:
            return self.player.passenger_ids
        vehicle = self.entities.get(vehicle_id)
        return () if vehicle is None else vehicle.passenger_ids

    def _set_vehicle_passenger_ids(
        self, vehicle_id: int, passenger_ids: tuple[int, ...]
    ) -> None:
        if vehicle_id == self.session.entity_id:
            self.player.passenger_ids = passenger_ids
        else:
            self._entity_for_state(vehicle_id).passenger_ids = passenger_ids

    def _passenger_vehicle_id(self, passenger_id: int) -> int | None:
        if passenger_id == self.session.entity_id:
            return self.player.vehicle_id
        passenger = self.entities.get(passenger_id)
        return None if passenger is None else passenger.vehicle_id

    def _set_passenger_vehicle_id(
        self, passenger_id: int, vehicle_id: int | None
    ) -> None:
        if passenger_id == self.session.entity_id:
            self.player.vehicle_id = vehicle_id
        else:
            self._entity_for_state(passenger_id).vehicle_id = vehicle_id

    def _root_vehicle_id(self, entity_id: int) -> int:
        current = entity_id
        visited: set[int] = set()
        while current not in visited:
            visited.add(current)
            if current == self.session.entity_id:
                vehicle_id = self.player.vehicle_id
            else:
                entity = self.entities.get(current)
                vehicle_id = None if entity is None else entity.vehicle_id
            if vehicle_id is None:
                return current
            current = vehicle_id
        return current

    def _controlled_root_vehicle(self) -> EntityState | None:
        local_id = self.session.entity_id
        if local_id is None or self.player.vehicle_id is None:
            return None
        root_id = self._root_vehicle_id(local_id)
        if root_id == local_id:
            return None
        vehicle = self.entities.get(root_id)
        if (
            vehicle is None
            or vehicle.type_id is None
            or self.version.boat_kind(vehicle.type_id) is None
            or not vehicle.passenger_ids
            or vehicle.passenger_ids[0] != local_id
        ):
            return None
        return vehicle

    def _packet_controlled_root_vehicle(self) -> EntityState | None:
        """Resolve the vehicle targeted by an authoritative correction packet."""

        local_id = self.session.entity_id
        if local_id is None or self.player.vehicle_id is None:
            return None
        root_id = self._root_vehicle_id(local_id)
        if root_id == local_id:
            return None
        vehicle = self.entities.get(root_id)
        if (
            vehicle is None
            or not vehicle.passenger_ids
            or vehicle.passenger_ids[0] != local_id
        ):
            return None
        return vehicle

    def _tick_remote_entity_metadata(self) -> None:
        for entity in self.entities.values():
            if entity.type_id != self.version.shulker_entity:
                continue
            previous = entity.shulker_current_peek
            entity.shulker_previous_peek = previous
            target = f32(float(entity.shulker_peek) * f32(0.01))
            if previous == target:
                continue
            if previous > target:
                entity.shulker_current_peek = f32(
                    max(target, min(1.0, f32(previous - f32(0.05))))
                )
            else:
                entity.shulker_current_peek = f32(
                    max(0.0, min(target, f32(previous + f32(0.05))))
                )

    @staticmethod
    def _metadata_value(entity: EntityState, index: int, default: object) -> object:
        value = entity.metadata.get(index)
        return default if value is None else value.value

    def _happy_ghast_collides_with_player(self, entity: EntityState) -> bool:
        health = self._metadata_value(entity, 9, 20.0)
        baby = self._metadata_value(entity, 16, False)
        stays_still = self._metadata_value(entity, 19, False)
        return (
            isinstance(health, (float, int))
            and health > 0.0
            and not bool(baby)
            and (bool(stays_still) or self.player.y >= entity.y + 4.0)
        )

    @staticmethod
    def _shulker_collision_box(entity: EntityState) -> AABB:
        amount = f32(entity.shulker_current_peek)
        angle = f32(f32(f32(0.5) + amount) * f32(math.pi))
        physical_peek = f32(
            f32(0.5) - f32(minecraft_sin(angle) * f32(0.5))
        )
        opposite_steps = (
            (0.0, 1.0, 0.0),
            (0.0, -1.0, 0.0),
            (0.0, 0.0, 1.0),
            (0.0, 0.0, -1.0),
            (1.0, 0.0, 0.0),
            (-1.0, 0.0, 0.0),
        )
        step_x, step_y, step_z = opposite_steps[entity.shulker_attach_face % 6]
        return AABB(-0.5, 0.0, -0.5, 0.5, 1.0, 0.5).expand_towards(
            Vec3(
                step_x * physical_peek,
                step_y * physical_peek,
                step_z * physical_peek,
            )
        ).move(entity.x, entity.y, entity.z)

    def _hard_entity_collision_boxes(self, query: AABB) -> list[AABB]:
        epsilon = 1.0e-7
        expanded = AABB(
            query.min_x - epsilon,
            query.min_y - epsilon,
            query.min_z - epsilon,
            query.max_x + epsilon,
            query.max_y + epsilon,
            query.max_z + epsilon,
        )
        local_id = self.session.entity_id
        local_root = self._root_vehicle_id(local_id) if local_id is not None else None
        result: list[AABB] = []
        for entity in self.entities.values():
            width = entity.collision_width
            height = entity.collision_height
            if width is None or height is None:
                continue
            if (
                local_id is not None
                and local_root == self._root_vehicle_id(entity.entity_id)
            ):
                continue
            if entity.type_id == self.version.happy_ghast_entity:
                if not self._happy_ghast_collides_with_player(entity):
                    continue
                half_width = width / 2.0
                box = AABB(
                    entity.x - half_width,
                    entity.y,
                    entity.z - half_width,
                    entity.x + half_width,
                    entity.y + height,
                    entity.z + half_width,
                )
            elif entity.type_id == self.version.shulker_entity:
                health = self._metadata_value(entity, 9, 30.0)
                if not isinstance(health, (float, int)) or health <= 0.0:
                    continue
                box = self._shulker_collision_box(entity)
            else:
                half_width = width / 2.0
                box = AABB(
                    entity.x - half_width,
                    entity.y,
                    entity.z - half_width,
                    entity.x + half_width,
                    entity.y + height,
                    entity.z + half_width,
                )
            if box.intersects(expanded):
                result.append(box)
        return result

    async def _handle_add_entity(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        entity_uuid = reader.read_uuid()
        type_id = reader.read_varint()
        if type_id < 0:
            raise ProtocolError(f"invalid entity-type registry ID {type_id}")
        position = tuple(reader.read_double() for _ in range(3))
        if self.version.protocol == 774:
            # 1.21.11 keeps rotation/object-data before velocity; 26.1+
            # moved velocity immediately after the position.
            pitch = self._unpack_degrees(reader.read_byte())
            yaw = self._unpack_degrees(reader.read_byte())
            head_yaw = self._unpack_degrees(reader.read_byte())
            data = reader.read_varint()
            velocity = reader.read_lp_vec3()
        else:
            velocity = reader.read_lp_vec3()
            pitch = self._unpack_degrees(reader.read_byte())
            yaw = self._unpack_degrees(reader.read_byte())
            head_yaw = self._unpack_degrees(reader.read_byte())
            data = reader.read_varint()
        reader.expect_end()
        self._validate_finite((*position, *velocity), "entity position and velocity")

        entity = self._entity_for_state(entity_id)
        entity.entity_uuid = entity_uuid
        entity.type_id = type_id
        dimensions = self.version.hard_collision_dimensions(type_id)
        if dimensions is None:
            entity.collision_width = None
            entity.collision_height = None
        else:
            entity.collision_width, entity.collision_height = dimensions
        entity.x, entity.y, entity.z = position
        entity.velocity_x, entity.velocity_y, entity.velocity_z = velocity
        entity.pitch = pitch
        entity.yaw = yaw
        entity.head_yaw = head_yaw
        entity.data = data
        await self.events.emit("entity_add", entity)

    async def _handle_entity_position_sync(self, payload: bytes) -> None:
        """Absolute position of a moving entity (26.3 ``entity_position_sync``).

        The relative ``move_entity`` packets only carry small deltas, so a
        knockback or any teleport-like correction arrives here.  A client that
        ignores this packet keeps an entity at the last delta position and
        slowly drifts away from where the server has it -- close enough to
        look right on screen and far enough to make a aimed hit miss.
        """

        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        kind = reader.read_varint()
        if kind == 1:
            count = reader.read_varint()
            if count <= 0 or count > reader.remaining // 25:
                raise ProtocolError(
                    f"entity position path with {count} steps exceeds payload"
                )
            position = (0.0, 0.0, 0.0)
            for _ in range(count):
                # Each step is absolute; only the last one is where the entity
                # is when the packet finishes, which is the point to track.
                position = (
                    reader.read_double(),
                    reader.read_double(),
                    reader.read_double(),
                )
                reader.read_varint()  # interpolation ticks for this step
        elif kind == 0:
            position = (
                reader.read_double(),
                reader.read_double(),
                reader.read_double(),
            )
        else:
            raise ProtocolError(f"unknown entity position path type {kind}")
        yaw = reader.read_float()
        pitch = reader.read_float()
        on_ground = reader.read_bool()
        reader.expect_end()
        self._validate_finite((*position, yaw, pitch), "entity position sync")

        entity = self.entities.get(entity_id)
        if entity is not None:
            entity.x, entity.y, entity.z = position
            entity.yaw, entity.pitch = yaw, pitch
            entity.on_ground = on_ground
        await self.events.emit("entity_move", entity_id, entity)

    async def _handle_move_entity(
        self,
        payload: bytes,
        *,
        position: bool,
        rotation: bool,
    ) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        if self.version.protocol >= 777:
            delta, angles, on_ground = self._read_move_entity_777(
                reader, position=position, rotation=rotation
            )
        else:
            delta = (
                tuple(reader.read_short() / 4096.0 for _ in range(3))
                if position
                else None
            )
            angles = (
                (
                    self._unpack_degrees(reader.read_byte()),
                    self._unpack_degrees(reader.read_byte()),
                )
                if rotation
                else None
            )
            on_ground = reader.read_bool()
        reader.expect_end()

        entity = self.entities.get(entity_id)
        if entity is not None:
            if delta is not None:
                entity.x += delta[0]
                entity.y += delta[1]
                entity.z += delta[2]
            if angles is not None:
                entity.yaw, entity.pitch = angles
            entity.on_ground = on_ground
        await self.events.emit("entity_move", entity_id, entity)

    def _read_move_entity_777(
        self,
        reader: PacketReader,
        *,
        position: bool,
        rotation: bool,
    ) -> tuple[
        tuple[float, float, float] | None, tuple[float, float] | None, bool
    ]:
        """Decode the 26.3 ``ClientboundMoveEntityPacket`` body after the id.

        Pos/PosRot carry a ``properties`` VarInt (bit 0 = on ground, the
        remaining bits = step count) followed by a ``VecDelta``: three shorts
        when the step count is zero, otherwise ``step count`` entries of
        ``(ticks VarInt, 3 shorts)`` that each chain from the previous step.
        Rot moved its on-ground flag in front of the two angle bytes.
        """

        delta: tuple[float, float, float] | None = None
        if position:
            properties = reader.read_varint()
            on_ground = bool(properties & 1)
            step_count = (properties & 0xFFFFFFFF) >> 1
            if step_count == 0:
                delta = tuple(reader.read_short() / 4096.0 for _ in range(3))
            else:
                # Each step is at least 7 bytes (1-byte VarInt + 3 shorts);
                # vanilla rejects counts that cannot fit the remaining data.
                if step_count > reader.remaining // 7:
                    raise ProtocolError(
                        f"entity movement with {step_count} steps exceeds payload"
                    )
                total = [0, 0, 0]
                for _ in range(step_count):
                    reader.read_varint()  # Interpolation ticks for this step.
                    for axis in range(3):
                        total[axis] += reader.read_short()
                delta = (total[0] / 4096.0, total[1] / 4096.0, total[2] / 4096.0)
        else:
            on_ground = reader.read_bool()
        angles = (
            (
                self._unpack_degrees(reader.read_byte()),
                self._unpack_degrees(reader.read_byte()),
            )
            if rotation
            else None
        )
        return delta, angles, on_ground

    async def _handle_move_vehicle(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        position = tuple(reader.read_double() for _ in range(3))
        yaw = reader.read_float()
        pitch = reader.read_float()
        reader.expect_end()
        self._validate_finite((*position, yaw, pitch), "vehicle movement values")

        vehicle = self._packet_controlled_root_vehicle()
        if vehicle is None:
            await self.events.emit("vehicle_move", None)
            return
        dx = vehicle.x - position[0]
        dy = vehicle.y - position[1]
        dz = vehicle.z - position[2]
        if dx * dx + dy * dy + dz * dz > 1.0e-5:
            vehicle.x, vehicle.y, vehicle.z = position
            vehicle.yaw, vehicle.pitch = yaw, pitch
            self._sync_local_riding_position()
        await self._send_vehicle_movement(vehicle)
        await self.events.emit("vehicle_move", vehicle)

    def _read_entity_metadata_value(
        self,
        reader: PacketReader,
        serializer_id: int,
    ) -> object:
        if serializer_id == 0:
            return reader.read_byte()
        if serializer_id == 1:
            return reader.read_varint()
        if serializer_id == 2:
            return reader.read_varlong()
        if serializer_id == 3:
            return reader.read_float()
        if serializer_id == 4:
            return reader.read_string()
        if serializer_id == 5:
            return read_anonymous_nbt(reader)
        if serializer_id == 6:
            return read_anonymous_nbt(reader) if reader.read_bool() else None
        if serializer_id == 7:
            try:
                return self._read_item_stack(reader)
            except _UnsupportedItemComponents as error:
                raise _UnsupportedEntityMetadata(str(error)) from error
        if serializer_id == 8:
            return reader.read_bool()
        if serializer_id == 9:
            return tuple(reader.read_float() for _ in range(3))
        if serializer_id == 10:
            return reader.read_position()
        if serializer_id == 11:
            return reader.read_position() if reader.read_bool() else None
        if serializer_id == 12:
            return reader.read_varint() % 6
        if serializer_id == 13:
            return reader.read_uuid() if reader.read_bool() else None
        if serializer_id in (14, 15):
            encoded = reader.read_varint()
            return None if serializer_id == 15 and encoded == 0 else encoded
        if serializer_id == 16:
            raise _UnsupportedEntityMetadata("particle options require a type-specific codec")
        if serializer_id == 17:
            count = reader.read_varint()
            if not 0 <= count <= 16384:
                raise ProtocolError(f"invalid entity particle count {count}")
            if count:
                raise _UnsupportedEntityMetadata(
                    "non-empty particle lists require type-specific codecs"
                )
            return ()
        if serializer_id == 18:
            return tuple(reader.read_varint() for _ in range(3))
        if serializer_id == 19:
            encoded = reader.read_varint()
            return None if encoded == 0 else encoded - 1
        if serializer_id == 20:
            return reader.read_varint()

        if self.version.protocol == 774:
            optional_global, vector, quaternion, profile, arm = 29, 35, 36, 37, 38
        else:
            optional_global, vector, quaternion, profile, arm = 33, 39, 40, 41, 42
        if 21 <= serializer_id < vector and serializer_id != optional_global:
            return reader.read_varint()
        if serializer_id == optional_global:
            if not reader.read_bool():
                return None
            return reader.read_string(), reader.read_position()
        if serializer_id == vector:
            return tuple(reader.read_float() for _ in range(3))
        if serializer_id == quaternion:
            return tuple(reader.read_float() for _ in range(4))
        if serializer_id == profile:
            raise _UnsupportedEntityMetadata(
                "resolvable profiles require profile-property and skin codecs"
            )
        if serializer_id == arm:
            return reader.read_varint()
        if self.version.protocol >= 777 and serializer_id == 43:
            # 26.3 appends DYE_COLOR (a VarInt id-mapped enum) after HUMANOID_ARM.
            return reader.read_varint()
        raise ProtocolError(f"unknown entity metadata serializer {serializer_id}")

    async def _handle_set_entity_data(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        updates: list[tuple[int, EntityMetadataValue]] = []
        while True:
            index = reader.read_unsigned_byte()
            if index == 0xFF:
                break
            serializer_id = reader.read_varint()
            if serializer_id < 0:
                raise ProtocolError(
                    f"invalid entity metadata serializer {serializer_id}"
                )
            try:
                value = self._read_entity_metadata_value(reader, serializer_id)
            except _UnsupportedEntityMetadata as error:
                await self.events.emit(
                    "entity_data_unparsed",
                    entity_id,
                    index,
                    serializer_id,
                    str(error),
                    payload,
                )
                return
            updates.append((index, EntityMetadataValue(serializer_id, value)))
        reader.expect_end()

        entity = None if entity_id == self.session.entity_id else self.entities.get(entity_id)
        metadata = self.player.metadata if entity_id == self.session.entity_id else None
        if entity is not None:
            metadata = entity.metadata
        if metadata is not None:
            for index, value in updates:
                metadata[index] = value
        if entity is not None and entity.type_id == self.version.shulker_entity:
            for index, value in updates:
                if index == 16 and value.serializer_id == 12:
                    entity.shulker_attach_face = int(value.value)
                elif index == 17 and value.serializer_id == 0:
                    entity.shulker_peek = int(value.value)
        await self.events.emit("entity_data", entity_id, tuple(updates), entity)

    async def _handle_remove_entities(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        count = reader.read_varint()
        if not 0 <= count <= 65536:
            raise ProtocolError(f"invalid removed-entity count {count}")
        entity_ids = tuple(reader.read_varint() for _ in range(count))
        reader.expect_end()

        removed = tuple(
            entity
            for entity_id in entity_ids
            if (entity := self.entities.pop(entity_id, None)) is not None
        )
        removed_ids = set(entity_ids)
        if self.player.vehicle_id in removed_ids:
            self.player.vehicle_id = None
        if any(passenger_id in removed_ids for passenger_id in self.player.passenger_ids):
            self.player.passenger_ids = tuple(
                passenger_id
                for passenger_id in self.player.passenger_ids
                if passenger_id not in removed_ids
            )
        for entity in self.entities.values():
            if entity.vehicle_id in removed_ids:
                entity.vehicle_id = None
            if any(passenger_id in removed_ids for passenger_id in entity.passenger_ids):
                entity.passenger_ids = tuple(
                    passenger_id
                    for passenger_id in entity.passenger_ids
                    if passenger_id not in removed_ids
                )
        await self.events.emit("entities_remove", entity_ids, removed)

    async def _handle_set_entity_motion(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        velocity = reader.read_lp_vec3()
        reader.expect_end()
        self._validate_finite(velocity, "entity velocity")
        entity = self.entities.get(entity_id)
        if entity is not None:
            entity.velocity_x, entity.velocity_y, entity.velocity_z = velocity
        if entity_id == self.session.entity_id:
            # A vanilla client applies this impulse in the next tick it runs and
            # never has to reconcile it with a tick that is already in flight.
            # Our reader and our tick loop are separate tasks, so the impulse can
            # land after the physics step but before that tick's movement packet
            # goes out; the epoch tells _tick_once to recompute rather than claim
            # a position that ignores the knockback.
            if self.knockback:
                self._motion_epoch += 1
                self.player.velocity_x, self.player.velocity_y, self.player.velocity_z = velocity
                self.physics_state.velocity = Vec3(*velocity)
        await self.events.emit("entity_motion", entity_id, velocity, entity)

    async def _handle_swing_animation(self, payload: bytes) -> None:
        """Another player (or mob) moved its arm where we can see it.

        The server broadcasts this whenever anyone swings, including us: our own
        swings come back on this packet, which is what makes it useful as proof
        that the arm animation really went out.

        The payload changed shape in 26.3. Up to 26.2 it is the old
        ``ClientboundAnimatePacket``: an entity id and one action byte, where 0
        is "swing the main arm". From 26.3 it is
        ``ClientboundSwingAnimationPacket``: entity id, hand, then the
        ``SwingAnimation`` component (type and duration). Both start with the
        entity id, and in both a main-hand swing carries 0, so the event reports
        that one number and leaves the rest alone.
        """

        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        if reader.remaining == 1:
            hand = reader.read_unsigned_byte()
        else:
            hand = reader.read_varint()
            reader.read_varint()
            reader.read_varint()
        reader.expect_end()
        await self.events.emit("swing_animation", entity_id, hand)

    async def _handle_set_passengers(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        vehicle_id = reader.read_varint()
        count = reader.read_varint()
        if not 0 <= count <= 65536:
            raise ProtocolError(f"invalid passenger count {count}")
        passenger_ids = tuple(reader.read_varint() for _ in range(count))
        reader.expect_end()

        previous_ids = set(self._vehicle_passenger_ids(vehicle_id))
        next_ids = set(passenger_ids)
        self._set_vehicle_passenger_ids(vehicle_id, passenger_ids)
        for passenger_id in previous_ids - next_ids:
            if self._passenger_vehicle_id(passenger_id) == vehicle_id:
                self._set_passenger_vehicle_id(passenger_id, None)
        for passenger_id in passenger_ids:
            previous_vehicle_id = self._passenger_vehicle_id(passenger_id)
            if previous_vehicle_id is not None and previous_vehicle_id != vehicle_id:
                previous_passengers = self._vehicle_passenger_ids(previous_vehicle_id)
                self._set_vehicle_passenger_ids(
                    previous_vehicle_id,
                    tuple(
                        existing_id
                        for existing_id in previous_passengers
                        if existing_id != passenger_id
                    ),
                )
            self._set_passenger_vehicle_id(passenger_id, vehicle_id)
        self._sync_local_riding_position()
        await self.events.emit("passengers", vehicle_id, passenger_ids)

    def _read_item_stack(self, reader: PacketReader) -> ItemStack:
        count = reader.read_varint()
        if count <= 0:
            return ItemStack()
        item_id = reader.read_varint()
        if item_id < 0:
            raise ProtocolError(f"invalid item registry ID {item_id}")
        positive_count = reader.read_varint()
        negative_count = reader.read_varint()
        if not 0 <= positive_count <= 65536 or not 0 <= negative_count <= 65536:
            raise ProtocolError(
                "item data-component counts must be between 0 and 65536"
            )
        if positive_count:
            raise _UnsupportedItemComponents(
                f"{positive_count} added data component(s) are not self-delimiting"
            )
        removed_component_ids = tuple(
            reader.read_varint() for _ in range(negative_count)
        )
        if any(component_id < 0 for component_id in removed_component_ids):
            raise ProtocolError("invalid removed data-component registry ID")
        identifier = self.version.item_identifier(item_id)
        return ItemStack(count, item_id, identifier, removed_component_ids)

    async def _handle_set_equipment(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        updates: list[tuple[EquipmentSlot, ItemStack]] = []
        try:
            while True:
                encoded_slot = reader.read_unsigned_byte()
                slot_id = encoded_slot & 0x7F
                try:
                    slot = EquipmentSlot(slot_id)
                except ValueError as error:
                    raise ProtocolError(f"invalid equipment slot {slot_id}") from error
                updates.append((slot, self._read_item_stack(reader)))
                if not encoded_slot & 0x80:
                    break
        except _UnsupportedItemComponents as error:
            await self.events.emit("equipment_unparsed", entity_id, str(error), payload)
            return
        reader.expect_end()

        entity = self._entity_for_state(entity_id)
        for slot, item in updates:
            entity.equipment[slot] = item
        if entity_id == self.session.entity_id:
            for slot, item in updates:
                self.player.equipment[slot] = item
            feet = self.player.equipment.get(EquipmentSlot.FEET)
            self.physics_state.wearing_leather_boots = (
                feet is not None and feet.identifier == "minecraft:leather_boots"
            )
        await self.events.emit("equipment", entity_id, tuple(updates))

    async def _handle_set_player_inventory(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        slot = reader.read_varint()
        if not 0 <= slot <= 45:
            raise ProtocolError(f"invalid player inventory slot {slot}")
        try:
            item = read_stack(
                payload,
                item_identifier=self.version.item_identifier,
                codecs=self.version.component_codecs,
                start=reader.offset,
                end=len(payload),
            ).item
        except (StackFrameError, ProtocolError) as error:
            await self.events.emit("inventory_unparsed", slot, str(error), payload)
            return
        self.player.inventory[slot] = item
        await self.events.emit("inventory", slot, item)

    def _container_state(self, container_id: int) -> ContainerState:
        state = self.containers.get(container_id)
        if state is None:
            state = ContainerState(container_id)
            self.containers[container_id] = state
        return state

    async def _handle_open_screen(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        container_id = reader.read_varint()
        menu_type_id = reader.read_varint()
        title_data = reader.read_remaining()
        state = ContainerState(
            container_id=container_id,
            menu_type_id=menu_type_id,
            title_data=title_data,
        )
        self.containers[container_id] = state
        self._active_container_id = container_id
        await self.events.emit("container_open", state)

    async def _handle_container_content(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        container_id = reader.read_varint()
        state_id = reader.read_varint()
        body = reader.read_remaining()
        state = self._container_state(container_id)
        state.state_id = state_id
        state.content_data = body
        state.slots.clear()
        state.items.clear()
        state.unreadable.clear()
        state.carried_item_data = b""
        state.carried_item = ItemStack()
        state.carried_unknown = False
        state.is_open = True
        state.revision += 1
        state.slot_revision += 1
        # Only a menu the server actually opened can be the active one.  The
        # player inventory (container 0) is re-sent on every join, respawn and
        # state change, so treating its content packet as "the open menu" would
        # clobber a chest the player just opened -- the server sends both in the
        # same tick, inventory last.
        if container_id != PLAYER_CONTAINER_ID:
            self._active_container_id = container_id
        self._decode_container_content(state, body)
        if container_id == PLAYER_CONTAINER_ID:
            await self._sync_player_inventory(state)
        elif self._mirror_open_menu(state):
            await self.events.emit(
                "inventory_sync", dict(self.player.inventory), state.carried_item
            )
        await self.events.emit("container_content", state)

    def _decode_container_content(self, state: ContainerState, body: bytes) -> None:
        """Decode ``count`` slots plus the carried stack into ``state``.

        ``count`` is a slot count, not a byte length, and a stack carrying added
        data components hides its own end -- so the slot list is recovered as a
        whole (see :mod:`protobot.protocol.stacks`) instead of slot by slot.
        """

        reader = PacketReader(body)
        count = reader.read_varint()
        if not 0 <= count <= _MAX_CONTAINER_SLOTS:
            raise ProtocolError(f"invalid container slot count {count}")
        state.slot_count = count
        try:
            frames = read_stacks(
                body,
                count + 1,
                item_identifier=self.version.item_identifier,
                codecs=self.version.component_codecs,
                start=reader.offset,
                end=len(body),
            )
        except StackFrameError:
            # No patch lengths add up: keep every slot byte-only and let callers
            # steer around them rather than silently reading them as empty.
            state.unreadable.update(range(count))
            state.carried_unknown = True
            return
        for slot, frame in enumerate(frames[:count]):
            state.items[slot] = frame.item
            state.slots[slot] = body[frame.start : frame.stop]
        carried = frames[count]
        state.carried_item = carried.item
        state.carried_item_data = body[carried.start : carried.stop]

    def _decode_stack_frame(self, raw: bytes) -> ItemStack:
        """Decode one stack that must fill ``raw`` exactly."""

        return read_stack(
            raw,
            item_identifier=self.version.item_identifier,
            codecs=self.version.component_codecs,
        ).item

    async def _handle_container_slot(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        container_id = reader.read_varint()
        state_id = reader.read_varint()
        slot = reader.read_short()
        raw = reader.read_remaining()
        state = self._container_state(container_id)
        state.state_id = state_id
        state.is_open = True
        state.revision += 1
        state.slot_revision += 1
        if slot < 0:
            # Releases before the dedicated cursor packet announce the cursor
            # as slot -1; it is not a window slot.
            state.carried_item_data = raw
            state.cursor_revision += 1
            try:
                state.carried_item = self._decode_stack_frame(raw)
                state.carried_unknown = False
            except (StackFrameError, ProtocolError):
                state.carried_unknown = True
            await self.events.emit("container_cursor", state)
            return
        state.slots[slot] = raw
        try:
            item = self._decode_stack_frame(raw)
        except (StackFrameError, ProtocolError):
            state.items.pop(slot, None)
            state.unreadable.add(slot)
            await self.events.emit("container_unparsed", state, slot)
            return
        state.unreadable.discard(slot)
        state.items[slot] = item
        if container_id == PLAYER_CONTAINER_ID:
            await self._sync_player_slot(slot, raw)
        elif self._mirror_open_menu(state, slot):
            await self.events.emit("inventory_slot", state, slot)
        await self.events.emit("container_slot", state, slot)

    async def _handle_container_set_data(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        container_id = reader.read_varint()
        data_id = reader.read_short()
        value = reader.read_short()
        reader.expect_end()
        state = self._container_state(container_id)
        state.data[data_id] = value
        state.revision += 1
        await self.events.emit("container_data", state, data_id, value)

    async def _handle_set_cursor_item(self, payload: bytes) -> None:
        """Track the stack the player carries on the cursor.

        Every supported release announces the cursor with its own packet
        instead of a ``container_set_slot`` for slot ``-1``.  The cursor is a
        property of the player, so it is applied to the window that currently
        has it -- without this the local cursor goes stale after the first
        click and every following click reports an old ``state_id``.
        """

        state = self.active_container
        if state is None:
            state = self.containers.get(PLAYER_CONTAINER_ID)
        if state is None:
            return
        try:
            state.carried_item = self._decode_stack_frame(payload)
            state.carried_item_data = payload
            state.carried_unknown = False
        except (StackFrameError, ProtocolError):
            state.carried_item_data = payload
            state.carried_unknown = True
        state.revision += 1
        state.slot_revision += 1
        state.cursor_revision += 1
        await self.events.emit("container_cursor", state)

    def _menu_layout(self, state: ContainerState):
        """Layout of the open window, cached by menu type and slot count."""

        key = (state.menu_type_id, state.slot_count)
        layout = self._layout_cache.get(key)
        if layout is None:
            from ..containers.layouts import layout_for

            layout = layout_for(
                self.version.menu_identifier(state.menu_type_id), state.slot_count
            )
            self._layout_cache[key] = layout
        return layout

    def _mirror_open_menu(self, state: ContainerState, slot: int | None = None) -> bool:
        """Copy a window's player-side slots into ``player.inventory``.

        While a window is open the server announces inventory changes on *that
        window's* container id: a ``/give`` lands in the window's player slots,
        not on container 0.  Without this the public inventory view would go
        stale for as long as a chest or a villager stays open.
        """

        player = self._menu_layout(state).player
        if not player:
            return False
        if slot is None:
            targets = [index for index in player if index in state.items]
        else:
            targets = [slot] if slot in player else []
        if not targets:
            return False
        for menu_slot in targets:
            position = player.index(menu_slot)
            target = 9 + position if position < 27 else 36 + (position - 27)
            if 0 <= target <= PLAYER_INVENTORY_LAST_SLOT:
                stack = state.items[menu_slot]
                self.player.inventory[target] = stack
                zero = self.containers.get(PLAYER_CONTAINER_ID)
                if zero is not None and zero is not state:
                    # Container 0 stops updating while a window is open, so keep
                    # its decoded view in step too: menus read slot contents from
                    # their own state, and acting on a stale slot does nothing.
                    zero.items[target] = stack
                    if menu_slot in state.slots:
                        zero.slots[target] = state.slots[menu_slot]
        return True

    async def _sync_player_slot(self, slot: int, raw: bytes) -> None:
        """Mirror one ``Container 0`` slot into ``player.inventory``.

        The server announces items gained outside a container (``/give``,
        pickups, consumed durability) as slot updates on container 0, so a bot
        that only listens to :meth:`_handle_set_player_inventory` keeps
        believing its inventory is empty -- and then cannot select the hotbar
        slot holding the block it was just handed.
        """

        if not 0 <= slot <= PLAYER_INVENTORY_LAST_SLOT:
            return
        reader = PacketReader(raw)
        try:
            item = self._read_item_stack(reader)
        except (_UnsupportedItemComponents, ProtocolError) as error:
            await self.events.emit("inventory_unparsed", slot, str(error), raw)
            return
        reader.expect_end()
        self.player.inventory[slot] = item
        await self.events.emit("inventory", slot, item)

    async def _sync_player_inventory(self, state: ContainerState) -> None:
        """Mirror a decoded ``Container 0`` content packet into the inventory."""

        for slot, item in state.items.items():
            if 0 <= slot <= PLAYER_INVENTORY_LAST_SLOT:
                self.player.inventory[slot] = item
        await self.events.emit("inventory_sync", dict(self.player.inventory), state.carried_item)

    async def _handle_container_close(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        container_id = reader.read_varint()
        reader.expect_end()
        state = self._container_state(container_id)
        state.is_open = False
        if self._active_container_id == container_id:
            self._active_container_id = None
        await self.events.emit("container_close", state)

    async def _handle_teleport_entity(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        position = [reader.read_double() for _ in range(3)]
        velocity = [reader.read_double() for _ in range(3)]
        packet_yaw = reader.read_float()
        packet_pitch = reader.read_float()
        relative = reader.read_unsigned_int()
        on_ground = reader.read_bool()
        reader.expect_end()
        self._validate_finite(
            (*position, *velocity, packet_yaw, packet_pitch),
            "entity teleport values",
        )

        entity = self.entities.get(entity_id)
        if entity is not None:
            current_position = (entity.x, entity.y, entity.z)
            current_velocity = (
                entity.velocity_x,
                entity.velocity_y,
                entity.velocity_z,
            )
            for index in range(3):
                if relative & (1 << index):
                    position[index] += current_position[index]

            current_yaw = f32(entity.yaw)
            current_pitch = f32(entity.pitch)
            yaw = (
                f32(packet_yaw + current_yaw)
                if relative & (1 << 3)
                else f32(packet_yaw)
            )
            pitch_input = (
                packet_pitch + current_pitch
                if relative & (1 << 4)
                else packet_pitch
            )
            pitch = f32(max(-90.0, min(90.0, pitch_input)))

            rotated_current = list(current_velocity)
            if relative & (1 << 8):
                rotate_x = f32(current_pitch - pitch)
                rotate_y = f32(current_yaw - yaw)
                radians_x = f32(math.radians(rotate_x))
                cx = minecraft_cos(radians_x)
                sx = minecraft_sin(radians_x)
                x, y, z = rotated_current
                y, z = y * cx + z * sx, z * cx - y * sx
                radians_y = f32(math.radians(rotate_y))
                cy = minecraft_cos(radians_y)
                sy = minecraft_sin(radians_y)
                x, z = x * cy + z * sy, z * cy - x * sy
                rotated_current = [x, y, z]
            for index in range(3):
                if relative & (1 << (index + 5)):
                    velocity[index] += rotated_current[index]

            entity.x, entity.y, entity.z = position
            entity.velocity_x, entity.velocity_y, entity.velocity_z = velocity
            entity.yaw = yaw
            entity.pitch = pitch
            entity.on_ground = on_ground
        await self.events.emit("entity_teleport", entity_id, entity, relative)

    async def _handle_update_mob_effect(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        effect_id = reader.read_varint()
        amplifier = reader.read_varint()
        duration = reader.read_varint()
        flags = reader.read_unsigned_byte()
        reader.expect_end()

        if effect_id < 0:
            raise ProtocolError(f"invalid status-effect registry ID {effect_id}")
        if not 0 <= amplifier <= 255:
            raise ProtocolError(f"invalid status-effect amplifier {amplifier}")
        if duration < -1:
            raise ProtocolError(f"invalid status-effect duration {duration}")
        if flags & ~0x0F:
            raise ProtocolError(f"invalid status-effect flags {flags:#04x}")

        identifier = _MOVEMENT_EFFECTS.get(effect_id)
        effect = StatusEffect(
            amplifier=amplifier,
            duration=duration,
            ambient=bool(flags & 0x01),
            show_particles=bool(flags & 0x02),
            show_icon=bool(flags & 0x04),
            keep_fading=bool(flags & 0x08),
        )
        if entity_id == self.session.entity_id and identifier is not None:
            effect = self.physics_state.set_status_effect(
                identifier,
                amplifier=effect.amplifier,
                duration=effect.duration,
                ambient=effect.ambient,
                show_particles=effect.show_particles,
                show_icon=effect.show_icon,
                keep_fading=effect.keep_fading,
            )
        await self.events.emit("effect_update", entity_id, effect_id, identifier, effect)

    async def _handle_remove_mob_effect(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        effect_id = reader.read_varint()
        reader.expect_end()
        if effect_id < 0:
            raise ProtocolError(f"invalid status-effect registry ID {effect_id}")

        identifier = _MOVEMENT_EFFECTS.get(effect_id)
        removed: StatusEffect | None = None
        if entity_id == self.session.entity_id and identifier is not None:
            removed = self.physics_state.remove_status_effect(identifier)
        await self.events.emit("effect_remove", entity_id, effect_id, identifier, removed)

    async def _handle_update_attributes(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        count = reader.read_varint()
        if not 0 <= count <= 128:
            raise ProtocolError(f"invalid attribute update count {count}")

        identifiers = _MOVEMENT_ATTRIBUTES.get(self.version.protocol, {})
        updates: list[AttributeUpdate] = []
        for _ in range(count):
            registry_id = reader.read_varint()
            if registry_id < 0:
                raise ProtocolError(f"invalid attribute registry ID {registry_id}")
            base = reader.read_double()
            if not math.isfinite(base):
                raise ProtocolError("attribute base value must be finite")
            modifier_count = reader.read_varint()
            if not 0 <= modifier_count <= 1024:
                raise ProtocolError(f"invalid attribute modifier count {modifier_count}")

            modifiers: list[AttributeModifierUpdate] = []
            for _ in range(modifier_count):
                modifier_id = reader.read_string(max_chars=32767)
                amount = reader.read_double()
                operation = reader.read_varint()
                if not math.isfinite(amount):
                    raise ProtocolError("attribute modifier amount must be finite")
                if operation not in (0, 1, 2):
                    raise ProtocolError(f"invalid attribute modifier operation {operation}")
                modifiers.append(AttributeModifierUpdate(modifier_id, amount, operation))

            identifier = identifiers.get(registry_id)
            value = self._calculate_attribute_value(base, modifiers)
            if identifier is not None:
                minimum, maximum = _MOVEMENT_ATTRIBUTE_RANGES[identifier]
                value = min(max(value, minimum), maximum)
            updates.append(
                AttributeUpdate(
                    registry_id=registry_id,
                    identifier=identifier,
                    base=base,
                    modifiers=tuple(modifiers),
                    value=value,
                )
            )
        reader.expect_end()

        if entity_id == self.session.entity_id:
            for update in updates:
                if update.identifier is not None:
                    value = update.value
                    if update.identifier == "minecraft:movement_speed":
                        value = self._calculate_attribute_value(
                            update.base,
                            (
                                modifier
                                for modifier in update.modifiers
                                if modifier.identifier != _SPRINTING_SPEED_MODIFIER
                            ),
                        )
                        minimum, maximum = _MOVEMENT_ATTRIBUTE_RANGES[update.identifier]
                        value = min(max(value, minimum), maximum)
                    self.physics_state.set_attribute(update.identifier, value)
        await self.events.emit("attributes", entity_id, tuple(updates))

    @staticmethod
    def _calculate_attribute_value(
        base: float, modifiers: Iterable[AttributeModifierUpdate]
    ) -> float:
        modifiers = tuple(modifiers)
        base_after_add = base
        for modifier in modifiers:
            if modifier.operation == 0:
                base_after_add += modifier.amount
        value = base_after_add
        for modifier in modifiers:
            if modifier.operation == 1:
                value += base_after_add * modifier.amount
        for modifier in modifiers:
            if modifier.operation == 2:
                value *= 1.0 + modifier.amount
        if not math.isfinite(value):
            raise ProtocolError("computed attribute value must be finite")
        return value

    def _handle_respawn(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        self._read_spawn_info(reader)
        reader.read_unsigned_byte()  # Data components to retain.
        reader.expect_end()
        self.world.chunks.clear()
        self.entities.clear()
        self.containers.clear()
        self._active_container_id = None
        self.player.vehicle_id = None
        self.player.passenger_ids = ()
        self.player.loaded = False
        self.player.gliding = False
        self.player.dead = False
        self.physics_state.gliding = False
        self.physics_state.gliding_ticks = 0
        self.world_ready.clear()
        self.loaded.clear()

    async def _handle_set_health(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        health = reader.read_float()
        food = reader.read_varint()
        saturation = reader.read_float()
        reader.expect_end()
        self.player.health = health
        self.player.food = food
        self.player.saturation = saturation
        await self.events.emit("health", health, food, saturation)
        # Health reaching zero is a death signal too, but a weaker one than
        # combat_kill: the server resends it on tick boundaries and plugins can
        # scale health. Zero arrives repeatedly, so dead dedupes the event.
        if health <= 0.0 and not self.player.dead:
            self.player.dead = True
            await self.events.emit("death", None)

    def _handle_set_experience(self, payload: bytes) -> None:
        """Mirror the experience bar, level and total the server reports.

        Enchanting is gated on the level (``EnchantmentMenu`` refuses a button
        whose cost is above it), so the library has to know the server's number
        instead of tracking the XP it thinks it earned.
        """

        reader = PacketReader(payload)
        progress = reader.read_float()
        level = reader.read_varint()
        total = reader.read_varint()
        reader.expect_end()
        self.player.experience_progress = progress
        self.player.experience_level = level
        self.player.experience_total = total

    async def _handle_player_combat_kill(self, payload: bytes) -> None:
        """Combat Death: the server telling the client to show the death
        screen, which makes it the authoritative death signal."""
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        message = read_anonymous_nbt(reader)
        reader.expect_end()
        if self.session.entity_id is not None and entity_id != self.session.entity_id:
            return  # Someone else died (spectating, etc.), not this bot.
        self.player.health = 0.0
        if self.player.dead:
            return
        self.player.dead = True
        await self.events.emit("death", message)

    async def _handle_player_info_update(self, payload: bytes) -> None:
        """Player list update: maintains :attr:`players` and emits
        ``player_join`` for anyone new.

        The whole packet is decoded aside before anything is stored. The action
        bit set is fixed-size (1.21.4+ has eight actions, exactly one byte), so
        a release that adds a ninth would make it two bytes and every field
        after it would be read off by one. ``expect_end()`` therefore has to
        land exactly at the end; when it does not, the packet is discarded and
        ``player_list_unparsed`` is emitted instead -- better no join/leave
        events at all than player names invented out of misaligned bytes.
        """

        try:
            decoded = self._decode_player_info(payload)
        except (ProtocolError, ValueError) as error:
            await self.events.emit("player_list_unparsed", str(error), payload)
            return
        joined: list[PlayerListEntry] = []
        for entry_uuid, name, game_mode, latency, listed, display_name in decoded:
            entry = self.players.get(entry_uuid)
            if entry is None:
                entry = PlayerListEntry(uuid=entry_uuid)
                self.players[entry_uuid] = entry
                if name is not None:
                    joined.append(entry)
            if name is not None:
                entry.name = name
            if game_mode is not None:
                entry.game_mode = game_mode
            if latency is not None:
                entry.latency = latency
            if listed is not None:
                entry.listed = listed
            if display_name is not None:
                entry.display_name = display_name
        # On joining, the server sends everyone who is online; that is a
        # roster, not people arriving.
        if not self._roster_synced or time.monotonic() < self._roster_deadline:
            self._roster_synced = True
            await self.events.emit("player_list", tuple(self.players.values()))
            return
        for entry in joined:
            if entry.uuid == self.uuid:
                continue  # This bot's own entry is not a join
            await self.events.emit("player_join", entry)

    def _decode_player_info(
        self, payload: bytes
    ) -> list[tuple[uuid.UUID, str | None, int | None, int | None, bool | None, str | None]]:
        reader = PacketReader(payload)
        actions = reader.read_unsigned_byte()
        count = reader.read_varint()
        if not 0 <= count <= 4096:
            raise ProtocolError(f"invalid player info entry count {count}")
        decoded: list[
            tuple[uuid.UUID, str | None, int | None, int | None, bool | None, str | None]
        ] = []
        for _ in range(count):
            entry_uuid = reader.read_uuid()
            name: str | None = None
            game_mode: int | None = None
            latency: int | None = None
            listed: bool | None = None
            display_name: str | None = None
            if actions & _PLAYER_INFO_ADD:
                name = reader.read_string(max_chars=16)
                if not name or any(character < " " for character in name):
                    raise ProtocolError(f"invalid player name {name!r}")
                properties = reader.read_varint()
                if not 0 <= properties <= 64:
                    raise ProtocolError(f"invalid property count {properties}")
                for _ in range(properties):
                    reader.read_string(max_chars=64)  # Property name.
                    reader.read_string(max_chars=32767)  # Value.
                    if reader.read_bool():
                        reader.read_string(max_chars=32767)  # Signature.
            if actions & _PLAYER_INFO_INIT_CHAT and reader.read_bool():
                reader.read_uuid()  # Chat session ID.
                reader.read_long()  # Public key expiry.
                reader.read_bytes(max_length=1 << 16)  # Public key.
                reader.read_bytes(max_length=1 << 16)  # Key signature.
            if actions & _PLAYER_INFO_GAME_MODE:
                game_mode = reader.read_varint()
            if actions & _PLAYER_INFO_LISTED:
                listed = reader.read_bool()
            if actions & _PLAYER_INFO_LATENCY:
                latency = reader.read_varint()
            if actions & _PLAYER_INFO_DISPLAY_NAME:
                display_name = (
                    plain_text(read_anonymous_nbt(reader)) if reader.read_bool() else ""
                )
            if actions & _PLAYER_INFO_LIST_ORDER:
                reader.read_varint()  # Tab-list sort order.
            if actions & _PLAYER_INFO_HAT:
                reader.read_bool()  # Show hat.
            decoded.append(
                (entry_uuid, name, game_mode, latency, listed, display_name)
            )
        reader.expect_end()
        return decoded

    async def _handle_player_info_remove(self, payload: bytes) -> None:
        """Player list removal: emits ``player_leave``."""

        try:
            reader = PacketReader(payload)
            count = reader.read_varint()
            if not 0 <= count <= 4096:
                raise ProtocolError(f"invalid player removal count {count}")
            removed_uuids = [reader.read_uuid() for _ in range(count)]
            reader.expect_end()
        except (ProtocolError, ValueError) as error:
            await self.events.emit("player_list_unparsed", str(error), payload)
            return
        for entry_uuid in removed_uuids:
            entry = self.players.pop(entry_uuid, None)
            if entry is None or entry.uuid == self.uuid or not entry.name:
                continue
            await self.events.emit("player_leave", entry)

    def _read_spawn_info(self, reader: PacketReader) -> None:
        dimension_type_id = reader.read_varint()
        self.session.dimension_type_id = dimension_type_id
        self.session.dimension_name = reader.read_string(max_chars=32767)
        reader.read_long()  # Hashed seed.
        if self.version.protocol >= 777:
            # 26.3 encodes GameType through its VarInt id mapper and the
            # previous mode as OPTIONAL_VAR_INT; keep the historical -1
            # sentinel for "no previous mode".
            self._set_game_mode(reader.read_varint())
            previous = self._read_optional_varint(reader)
            self.session.previous_game_mode = -1 if previous is None else previous
        else:
            self._set_game_mode(reader.read_byte())
            self.session.previous_game_mode = reader.read_byte()
        self.session.is_debug = reader.read_bool()
        self.session.is_flat = reader.read_bool()
        if reader.read_bool():
            reader.read_string(max_chars=32767)
            reader.read_position()
        reader.read_varint()  # Portal cooldown.
        self.session.sea_level = reader.read_varint()
        bounds = self.registries.dimension_bounds(dimension_type_id)
        if bounds is not None:
            self.world.set_dimension_bounds(*bounds)

    def _set_game_mode(self, game_mode: int) -> None:
        if not 0 <= game_mode <= 3:
            raise ProtocolError(f"invalid game mode {game_mode}")
        entering_spectator = game_mode == 3 and not self.physics_state.spectator
        self.session.game_mode = game_mode
        self.physics_state.spectator = game_mode == 3
        if entering_spectator:
            velocity = self.physics_state.velocity
            self.physics_state.velocity = Vec3(velocity.x, 0.0, velocity.z)

    @staticmethod
    def _skip_long_array(reader: PacketReader, *, limit: int) -> None:
        count = reader.read_varint()
        if not 0 <= count <= limit:
            raise ProtocolError(f"invalid long array count {count}")
        for _ in range(count):
            reader.read_long()

    def _skip_bit_set(self, reader: PacketReader) -> None:
        """Skip a vanilla ``BitSet`` (light masks, chat filter masks).

        Up to 26.2 a BitSet is a VarInt long count followed by the longs.
        26.3 switched ``ByteBufCodecs.BIT_SET`` to ``BitSet.toByteArray``
        framed as an ordinary VarInt-prefixed byte array.
        """

        if self.version.protocol >= 777:
            reader.read_bytes(max_length=1024 * 8)
        else:
            self._skip_long_array(reader, limit=1024)

    @staticmethod
    def _read_optional_varint(reader: PacketReader) -> int | None:
        """Decode ``ByteBufCodecs.OPTIONAL_VAR_INT`` (0 = empty, else value + 1)."""

        encoded = reader.read_varint()
        return None if encoded == 0 else encoded - 1

    @staticmethod
    def _signed_bits(value: int, bits: int) -> int:
        sign = 1 << (bits - 1)
        return value - (1 << bits) if value & sign else value

    async def _handle_position(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        teleport_id = reader.read_varint()
        values = [reader.read_double() for _ in range(6)]
        packet_yaw = reader.read_float()
        packet_pitch = reader.read_float()
        relative = reader.read_unsigned_int()
        reader.expect_end()
        if _TRACE_POS:
            local = self.physics_state.position
            log_trace(
                f"[tp] t={time.monotonic():.3f} id={teleport_id}"
                f" pos=({values[0]:.4f},{values[1]:.4f},{values[2]:.4f})"
                f" vel=({values[3]:.4f},{values[4]:.4f},{values[5]:.4f})"
                f" rot=({packet_yaw:.3f},{packet_pitch:.3f}) rel=0x{relative:x}"
                f" local=({local.x:.4f},{local.y:.4f},{local.z:.4f})"
            )

        position = values[:3]
        packet_velocity = values[3:]
        current_position = (self.player.x, self.player.y, self.player.z)
        current_velocity = (
            self.player.velocity_x,
            self.player.velocity_y,
            self.player.velocity_z,
        )
        for index in range(3):
            if relative & (1 << index):
                position[index] += current_position[index]

        # Relative rotation is resolved before applying ROTATE_DELTA.  This
        # mirrors PositionMoveRotation.calculateAbsolute in the 26.2 server:
        # X_ROT is clamped to [-90, 90], while Y_ROT is left unwrapped.
        current_yaw = f32(self.player.yaw)
        current_pitch = f32(self.player.pitch)
        yaw = f32(packet_yaw + current_yaw) if relative & (1 << 3) else f32(packet_yaw)
        pitch_input = packet_pitch + current_pitch if relative & (1 << 4) else packet_pitch
        pitch = f32(max(-90.0, min(90.0, pitch_input)))

        # DELTA_* resolves as ``packet + current`` only for the flagged axes.
        # ROTATE_DELTA rotates the *current* movement vector before that
        # per-axis resolution; it does not rotate the packet's delta itself.
        rotated_current = list(current_velocity)
        if relative & (1 << 8):  # Relative.ROTATE_DELTA
            # The current movement vector is rotated from its old orientation
            # into the resulting player orientation, first around X then around
            # Y (the order used by Vec3.xRot(...).yRot(...)).
            # Keep the subtraction in Java-float precision before converting
            # to radians and looking up Minecraft's sine/cosine table.
            rotate_x = f32(current_pitch - pitch)
            rotate_y = f32(current_yaw - yaw)
            radians_x = f32(math.radians(rotate_x))
            cx = minecraft_cos(radians_x)
            sx = minecraft_sin(radians_x)
            x, y, z = rotated_current
            y, z = y * cx + z * sx, z * cx - y * sx
            radians_y = f32(math.radians(rotate_y))
            cy = minecraft_cos(radians_y)
            sy = minecraft_sin(radians_y)
            x, z = x * cy + z * sy, z * cy - x * sy
            rotated_current = [x, y, z]
        velocity = list(packet_velocity)
        for index in range(3):
            if relative & (1 << (index + 5)):
                velocity[index] += rotated_current[index]

        self.player.x, self.player.y, self.player.z = position
        self.player.velocity_x, self.player.velocity_y, self.player.velocity_z = velocity
        self.player.yaw, self.player.pitch = yaw, pitch
        self.physics_state.position = Vec3(*position)
        self.physics_state.velocity = Vec3(*velocity)
        self.physics_state.yaw = yaw
        self.physics_state.pitch = pitch
        self.physics_state.on_ground = self.player.on_ground
        self.physics_state.horizontal_collision = self.player.horizontal_collision
        if not self.physics_state.on_ground and not self._force_flight_active:
            # A teleport can put us on top of a block while the predictor still
            # carries the "in the air" flag from before it.  Claiming that flag
            # in the next movement packet is a ground spoof and anti-cheat
            # flags it, so settle the flag from the world we have: a solid block
            # right under the feet means we are standing on it.
            below = self.world.get_state_id(
                math.floor(position[0]),
                math.floor(position[1] - 0.001),
                math.floor(position[2]),
            )
            if below > 0:
                self.player.on_ground = True
                self.physics_state.on_ground = True
        if self._force_flight_active:
            # Keep forced local flight active after any authoritative position
            # sync.  This packet is not interpreted as a permission decision.
            self.physics_state.on_ground = False
            self.physics_state.flying = True
            self.physics_state.velocity = Vec3(*velocity)
        self._position_update_serial += 1
        confirm = PacketWriter().write_varint(teleport_id)
        if self.version.protocol >= 777:
            # 26.3 echoes the resolved position/rotation in the accept packet;
            # the server snaps to its own target and then validates these
            # values like a regular movement packet.
            confirm.write_double(position[0]).write_double(position[1])
            confirm.write_double(position[2]).write_float(yaw).write_float(pitch)
        await self.send_raw(
            self.version.packets.serverbound_teleport_confirm,
            confirm.to_bytes(),
        )
        if not self.player.loaded:
            self.player.loaded = True
            self.loaded.set()
            await self.send_raw(self.version.packets.serverbound_player_loaded)
        await self.events.emit("position", self.player)
