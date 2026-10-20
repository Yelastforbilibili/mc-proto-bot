"""The connection: opening it, reading it, staying on it, closing it.

A `Bot` spends its whole life in one loop (`_read_loop`) that hands
packets to the play router and keeps the session alive: keep-alive replies,
chat signing, transfers, and the wait helpers a caller uses after connecting.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
import uuid
from contextlib import suppress
from types import TracebackType
from typing import TYPE_CHECKING

from .errors import (
    ConnectionClosed,
    LoginRejected,
    OnlineModeRequired,
    ProtocolError,
    UnsupportedVersion,
)
from .log import error as log_error, trace as log_trace, warn as log_warn
from .physics import PhysicsState
from .protocol.codec import PacketReader, PacketWriter
from .protocol.connection import ConnectionState
from .protocol.framing import make_handshake
from .registry import RegistryStore
from .srv import resolve_minecraft_srv
from .state import PlayerState, WorldSessionState
from .support import (
    DEFAULT_MINECRAFT_PORT,
    _LastSeenMessagesTracker,
    _TRACE_RECV,
    _TRACE_SEND,
    _clientbound_name,
    _serverbound_name,
)
from .text import plain_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Bot

class ConnectionMixin:
    """Methods of :class:`protobot.Bot`, grouped by concern."""

    async def __aenter__(self) -> Bot:
        if self.state is ConnectionState.DISCONNECTED and not self.closed.is_set():
            try:
                await self.start()
                await self.wait_ready()
                await self.wait_loaded()
            except BaseException:
                await self.close()
                raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def on(self, event: str, handler=None):  # type: ignore[no-untyped-def]
        """Register an event handler; usable directly or as a decorator."""
        return self.events.on(event, handler)

    async def start(self) -> None:
        if self.state is not ConnectionState.DISCONNECTED or self._reader_task is not None:
            raise RuntimeError("bot has already been started")
        await self._open_login_connection(
            self.host,
            self.port,
            self.handshake_host or self.host,
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name=f"protobot:{self.username}")

    async def _prepare_chat_certificate(self) -> None:
        if not self.access_token:
            return
        from .auth import fetch_player_certificate

        try:
            self._chat_certificate = await fetch_player_certificate(
                self.access_token, self.minecraft_services
            )
            self._chat_certificate_error = None
            self._next_chat_certificate_refresh_ms = (
                self._chat_certificate.refreshed_after_ms
            )
        except Exception as error:
            self._chat_certificate = None
            self._chat_certificate_error = str(error)
            self._next_chat_certificate_refresh_ms = int(time.time() * 1000) + 60_000
            log_warn(f"[chat] secure-chat certificate unavailable: {error}")

    async def _activate_chat_session(self) -> None:
        certificate = self._chat_certificate
        if certificate is None or not self._authenticated_connection:
            return
        if certificate.expired:
            self._chat_certificate = None
            self._chat_certificate_error = "the Mojang player certificate has expired"
            return
        async with self._chat_send_lock:
            session_id = uuid.uuid4()
            payload = (
                PacketWriter()
                .write_uuid(session_id)
                .write_long(certificate.expires_at_ms)
                .write_bytes(certificate.public_key_der)
                .write_bytes(certificate.key_signature)
                .to_bytes()
            )
            await self.send_raw(
                self.version.packets.serverbound_chat_session_update, payload
            )
            self._chat_session_id = session_id
            self._chat_message_index = 0

    async def _refresh_chat_certificate_if_due(self) -> None:
        task = self._chat_certificate_refresh_task
        if (
            task is None
            and self._authenticated_connection
            and self.access_token is not None
            and int(time.time() * 1000) >= self._next_chat_certificate_refresh_ms
        ):
            from .auth import fetch_player_certificate

            task = asyncio.create_task(
                fetch_player_certificate(
                    self.access_token or "", self.minecraft_services
                ),
                name=f"protobot-chat-certificate:{self.username}",
            )
            self._chat_certificate_refresh_task = task
            self._next_chat_certificate_refresh_ms = int(time.time() * 1000) + 3_600_000
        if task is None or not task.done():
            return

        self._chat_certificate_refresh_task = None
        try:
            self._chat_certificate = task.result()
            self._chat_certificate_error = None
            self._next_chat_certificate_refresh_ms = (
                self._chat_certificate.refreshed_after_ms
            )
            await self._activate_chat_session()
        except Exception as error:
            self._chat_certificate_error = str(error)
            self._next_chat_certificate_refresh_ms = int(time.time() * 1000) + 60_000
            log_warn(f"[chat] secure-chat certificate refresh failed: {error}")

    async def _resolve_endpoint(self, host: str, port: int) -> tuple[str, int]:
        """Apply Minecraft SRV redirection the way a vanilla client does.

        Vanilla only looks up ``_minecraft._tcp.<host>`` when the player typed no
        port, so the lookup is confined to the default port. Any lookup failure
        falls through to the address as given.
        """

        if not self.resolve_srv or port != DEFAULT_MINECRAFT_PORT:
            return host, port
        target = await asyncio.to_thread(resolve_minecraft_srv, host)
        if target is None:
            return host, port
        await self.events.emit("srv_resolved", host, target[0], target[1])
        return target

    async def _open_login_connection(
        self,
        host: str,
        port: int,
        handshake_host: str,
    ) -> None:
        connect_host, connect_port = await self._resolve_endpoint(host, port)
        self.connected_host = connect_host
        self.connected_port = connect_port
        await asyncio.wait_for(
            self._connection.open(connect_host, connect_port),
            timeout=self.connect_timeout,
        )
        self.state = ConnectionState.HANDSHAKING
        protocol_host = self.modlist.handshake_host(handshake_host)
        await self._connection.send_packet(
            0,
            make_handshake(self.version.protocol, protocol_host, connect_port, 2),
        )
        self.state = ConnectionState.LOGIN
        login_start = PacketWriter().write_string(self.username, max_chars=16).write_uuid(self.uuid)
        await self._connection.send_packet(0, login_start.to_bytes())

    async def wait_ready(self, *, timeout: float = 30.0) -> None:
        ready_task = asyncio.create_task(self.ready.wait())
        closed_task = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (ready_task, closed_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"login did not complete within {timeout} seconds")
            if closed_task in done and not self.ready.is_set():
                if self._terminal_error is not None:
                    raise self._terminal_error
                raise ConnectionClosed(self.disconnect_reason or "connection closed during login")
        finally:
            ready_task.cancel()
            closed_task.cancel()

    async def wait_loaded(self, *, timeout: float = 30.0) -> None:
        """Wait until the server has sent the initial player position."""

        loaded_task = asyncio.create_task(self.loaded.wait())
        closed_task = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (loaded_task, closed_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"initial position was not received within {timeout} seconds")
            if closed_task in done and not self.loaded.is_set():
                if self._terminal_error is not None:
                    raise self._terminal_error
                raise ConnectionClosed(self.disconnect_reason or "connection closed before spawn")
        finally:
            loaded_task.cancel()
            closed_task.cancel()

    async def wait_world(self, *, timeout: float = 30.0) -> None:
        """Wait until at least one decoded chunk is available."""

        world_task = asyncio.create_task(self.world_ready.wait())
        closed_task = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (world_task, closed_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"no chunk was received within {timeout} seconds")
            if closed_task in done and not self.world_ready.is_set():
                if self._terminal_error is not None:
                    raise self._terminal_error
                raise ConnectionClosed(
                    self.disconnect_reason or "connection closed before world data"
                )
        finally:
            world_task.cancel()
            closed_task.cancel()

    async def close(self) -> None:
        certificate_task, self._chat_certificate_refresh_task = (
            self._chat_certificate_refresh_task,
            None,
        )
        if certificate_task is not None:
            certificate_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await certificate_task
        task, self._reader_task = self._reader_task, None
        current = asyncio.current_task()
        if task is not None and task is not current:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._connection.close()
        self.state = ConnectionState.DISCONNECTED
        self.closed.set()

    async def send_raw(self, packet_id: int, payload: bytes = b"") -> None:
        """Send a packet in the current state for advanced protocol use."""
        if _TRACE_SEND:
            # ``log_trace`` is the one place traces can be switched off; the
            # environment variable only decides whether we build the string.
            log_trace(
                f"[send] {time.time():.4f}"
                f" {_serverbound_name(self.version.packets, packet_id)}"
                f" {len(payload)}B"
            )
        await self._connection.send_packet(packet_id, payload)

    async def send_configuration_payload(self, channel: str, data: bytes = b"") -> None:
        """Send a configuration custom payload.

        Loader handshakes use this escape hatch for messages that are not part
        of the common Forge/Fabric adapter.  It is valid only while the
        connection is in the configuration state.
        """

        if self.state is not ConnectionState.CONFIGURATION:
            raise RuntimeError(
                f"configuration payload requires configuration state, currently {self.state.value}"
            )
        payload = PacketWriter().write_string(channel, max_chars=32767).write_raw(data).to_bytes()
        await self.send_raw(0x02, payload)

    async def send_play_payload(self, channel: str, data: bytes = b"") -> None:
        """Send a play-state custom payload for loader/application protocols."""

        self._require_play()
        payload = PacketWriter().write_string(channel, max_chars=32767).write_raw(data).to_bytes()
        await self.send_raw(self.version.packets.serverbound_custom_payload, payload)

    def _require_play(self) -> None:
        if self.state is not ConnectionState.PLAY:
            if self._terminal_error is not None:
                raise self._terminal_error
            raise RuntimeError(f"operation requires play state, currently {self.state.value}")

    def _reset_server_state(self) -> None:
        """Discard state that cannot survive a backend or TCP server switch."""

        self.ready.clear()
        self.loaded.clear()
        self.world_ready.clear()
        self.world.chunks.clear()
        self.player = PlayerState()
        self.session = WorldSessionState()
        self.session_id = None
        self._chat_session_id = None
        self._chat_message_index = 0
        self.entities.clear()
        self.players.clear()
        self._roster_synced = False
        self._roster_deadline = 0.0
        self.containers.clear()
        self._active_container_id = None
        self.registries = RegistryStore()
        self.physics_state = PhysicsState()
        self._navigation_active = False
        self._navigation_planning = False
        self._navigation_claimed = False
        self._force_flight_active = False
        self._navigation_vclip_active = False
        self._navigation_vertical_active = False
        self._next_anti_kick = 0.0
        self._anti_kick_last_packet_y = None
        self._position_sent_this_tick = False
        self._last_tick_packet_time = 0.0
        self._last_sent_yaw = None
        self._last_sent_pitch = None
        self._last_sent_input_flags = 0
        self._last_sent_sprinting = False
        self._next_sequence = 0
        self._last_seen_messages = _LastSeenMessagesTracker()

    async def _read_loop(self) -> None:
        try:
            while True:
                packet = await self._connection.receive_packet(self.state)
                await self.events.emit("packet", packet)
                if _TRACE_RECV:
                    log_trace(
                        f"[recv] {_clientbound_name(self.version.packets, packet.packet_id)}"
                        f" {len(packet.payload)}B"
                    )
                await self.events.emit(f"packet:{self.state.value}:{packet.packet_id}", packet)
                if self.state is ConnectionState.LOGIN:
                    await self._handle_login(packet)
                elif self.state is ConnectionState.CONFIGURATION:
                    await self._handle_configuration(packet)
                elif self.state is ConnectionState.PLAY:
                    await self._handle_play(packet)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._terminal_error = error
            if self.disconnect_reason is None and isinstance(
                error, (ConnectionClosed, LoginRejected)
            ):
                self.disconnect_reason = str(error)
            if not (
                isinstance(error, ConnectionClosed)
                and self.disconnect_reason is not None
            ):
                log_error(
                    f"[disconnect] state={self.state.value} "
                    f"error={type(error).__name__}: {error}; "
                    f"reason={self.disconnect_reason or str(error)!r}"
                )
            await self.events.emit("error", error)
        finally:
            await self._connection.close()
            self.state = ConnectionState.DISCONNECTED
            self.closed.set()
            await self.events.emit("close", self.disconnect_reason)

    async def _send_client_information(self) -> None:
        payload = (
            PacketWriter()
            .write_string("en_us", max_chars=16)
            .write_byte(10)
            .write_varint(0)
            .write_bool(True)
            .write_unsigned_byte(0x7F)
            .write_varint(1)
            .write_bool(False)
            .write_bool(True)
            .write_varint(0)
            .to_bytes()
        )
        await self.send_raw(0x00, payload)

    def _velocity_forwarding_payload(self) -> bytes:
        """Build Velocity modern-forwarding v1 data and its HMAC signature."""

        if self._velocity_secret is None:
            raise RuntimeError("Velocity forwarding secret is not configured")
        body = (
            PacketWriter()
            .write_varint(1)
            .write_string(self.velocity_player_ip, max_chars=32767)
            .write_uuid(self.uuid)
            .write_string(self.username, max_chars=16)
            .write_varint(0)
            .to_bytes()
        )
        signature = hmac.new(self._velocity_secret, body, hashlib.sha256).digest()
        return signature + body

    @staticmethod
    def _best_effort_reason(payload: bytes) -> str:
        reader = PacketReader(payload)
        try:
            text = reader.read_string(max_chars=262144)
            if not reader.remaining:
                try:
                    component = json.loads(text)
                    if isinstance(component, (dict, list)):
                        rendered = plain_text(component).strip()
                        if rendered:
                            return rendered
                except (TypeError, json.JSONDecodeError):
                    pass
                return text
        except ProtocolError:
            pass
        return f"server disconnected (encoded reason: {payload.hex()})"
    async def send_command(self, command: str) -> None:
        """Send an unsigned command; a leading slash is accepted and removed."""

        self._require_play()
        if not isinstance(command, str):
            raise TypeError("command must be a string")
        if command.startswith("/"):
            command = command[1:]
        if not command:
            raise ValueError("command must not be empty")
        payload = PacketWriter().write_string(command, max_chars=32767).to_bytes()
        await self.send_raw(self.version.packets.serverbound_chat_command, payload)

    async def register_as_bot(self, *, command: str = "botregister") -> None:
        """Announce this client to the test server's spectator registry.

        ``prototest-bridge`` answers ``/botregister`` by adding this player to
        the list an operator can watch with ``/see``, which is how a human can
        look at a bot's inventory while a scenario runs. Servers without the
        plugin simply answer "unknown command": one chat line, no state change.
        """

        await self.send_command(command)

    async def send_message(self, message: str) -> None:
        """Send chat, signing it when an online account certificate is active.

        Online bots automatically fetch and register a Mojang player
        certificate. Offline bots retain the protocol's unsigned fallback.
        """

        self._require_play()
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        if not message:
            raise ValueError("message must not be empty")
        if len(message) > 256:
            raise ValueError("message exceeds the 256 character chat limit")
        async with self._chat_send_lock:
            if self.session.enforces_secure_chat and (
                self._chat_certificate is None or self._chat_session_id is None
            ):
                detail = self._chat_certificate_error or (
                    "no online account token was supplied"
                )
                raise OnlineModeRequired(
                    "the server enforces secure chat, but no Mojang player "
                    f"certificate is active: {detail}"
                )
            update = self._last_seen_messages.generate_update()
            timestamp_ms = int(time.time() * 1000)
            salt = secrets.randbits(64)
            signature = self._sign_chat_message(
                message,
                timestamp_ms=timestamp_ms,
                salt=salt,
                last_seen=update.signatures,
            )
            writer = (
                PacketWriter()
                .write_string(message, max_chars=256)
                .write_long(timestamp_ms)
                .write_unsigned_long(salt)
                .write_bool(signature is not None)
            )
            if signature is not None:
                writer.write_raw(signature)
            payload = (
                writer.write_varint(update.offset)
                .write_raw(update.acknowledged)
                .write_unsigned_byte(update.checksum)
                .to_bytes()
            )
            await self.send_raw(self.version.packets.serverbound_chat, payload)
        # Let plugins see what this bot said, whoever sent it: the server
        # echoes chat back as ``player_chat``, and without this a plugin
        # cannot tell its own words (or another plugin's) from a stranger's.
        await self.events.emit("chat_sent", message)

    def _sign_chat_message(
        self,
        message: str,
        *,
        timestamp_ms: int,
        salt: int,
        last_seen: tuple[bytes, ...],
    ) -> bytes | None:
        certificate = self._chat_certificate
        session_id = self._chat_session_id
        if certificate is None or session_id is None:
            return None
        if certificate.expired:
            raise OnlineModeRequired("the Mojang player certificate has expired")

        content = message.encode("utf-8")
        signed = (
            PacketWriter()
            .write_int(1)
            .write_uuid(self.uuid)
            .write_uuid(session_id)
            .write_int(self._chat_message_index)
            .write_unsigned_long(salt)
            .write_long(timestamp_ms // 1000)
            .write_int(len(content))
            .write_raw(content)
            .write_int(len(last_seen))
        )
        for signature in last_seen:
            signed.write_raw(signature)
        signature = certificate.sign(signed.to_bytes())
        self._chat_message_index += 1
        return signature

    async def respawn(self) -> None:
        """Leave the death screen: Client Status with action 0 (perform respawn).

        The server never respawns a dead player on its own — not even with
        ``doImmediateRespawn`` on, where the vanilla client just sends this
        without showing the screen first. Confirming the teleport that follows
        and re-sending Player Loaded is handled by the position handler, since
        :meth:`_handle_respawn` clears ``player.loaded``.
        """

        self._require_play()
        packet_id = self.version.packets.serverbound_client_command
        if not packet_id:
            raise UnsupportedVersion(
                "the serverbound_client_command packet id is unverified on "
                f"{self.version.name}, so a respawn cannot be requested"
            )
        await self.send_raw(packet_id, PacketWriter().write_varint(0).to_bytes())
