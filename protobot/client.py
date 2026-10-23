"""The bot object: connection, movement, packets and pathfinding in one place.

`Bot` itself only keeps the pieces that do not belong to any single concern:
the constructor, the lazily built high-level layers (`actions`, `menu`,
`goals`, `tasks`) and the small state accessors.  Everything else lives in
a mixin, one per concern:

* :class:protobot.connection.ConnectionMixin -- socket, login, chat, reconnect
* :class:protobot.movement.MovementMixin -- the 20 Hz movement tick and its packets
* :class:protobot.play.router.PlayRouterMixin -- play-state packets to handlers to state
* :class:protobot.navigator.NavigatorMixin -- path planning and path following

`protobot.support` holds the module-level constants and helpers that used to
sit at the top of this file; the names this module used to expose are imported
back here so `from protobot.client import ...` keeps working.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import uuid
from collections.abc import Callable, Iterable, Mapping

from .connection import ConnectionMixin
from .driver import DriverMixin
from .events import EventBus
from .modlist import ChannelSpec, Loader, ModListAdapter, make_adapter
from .movement import MovementMixin
from .navigator import NavigatorMixin
from .pickup import PickupMixin
from .physics import (
    BoatPhysicsEngine,
    MovementInput,
    PhysicsAttributes,
    PhysicsEngine,
    PhysicsState,
)
from .play.router import PlayRouterMixin
from .protocol.codec import PacketWriter
from .protocol.connection import ConnectionState, ProtocolConnection
from .protocol.versions import VersionSpec, get_version
from .registry import RegistryStore
from .state import (
    ContainerState,
    EntityState,
    ItemStack,
    PlayerListEntry,
    PlayerState,
    WorldSessionState,
)
from .support import (
    _CONTAINER_CLICK_TYPES,
    _INTERACTION_HANDS,
    _LastSeenMessagesTracker,
    PLAYER_CONTAINER_ID,
    PLAYER_INVENTORY_LAST_SLOT,
    offline_uuid,
)
from .support import (  # re-exported: these were module-level names here before the split
    AttributeModifierUpdate,
    AttributeUpdate,
    DEFAULT_MINECRAFT_PORT,
    MIN_TICK_GAP,
)
from .world import BlockStateRegistry, World

class Bot(
    ConnectionMixin,
    MovementMixin,
    PlayRouterMixin,
    NavigatorMixin,
    PickupMixin,
    DriverMixin,
):
    """An offline-mode Minecraft bot.

    ``start`` opens the socket and starts the protocol task. ``wait_ready`` waits
    until configuration is complete. The convenience :func:`connect` does both.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = 25565,
        handshake_host: str | None = None,
        username: str = "ProtoBot",
        version: str | VersionSpec = "26.2",
        connect_timeout: float = 10.0,
        resolve_srv: bool = True,
        access_token: str | None = None,
        profile_uuid: str | uuid.UUID | None = None,
        session_server: str = "https://sessionserver.mojang.com",
        minecraft_services: str = "https://api.minecraftservices.com",
        loader: Loader | str = Loader.VANILLA,
        mods: Mapping[str, str] | None = None,
        loader_protocol: int = 0,
        configuration_channels: Mapping[str, ChannelSpec | str | int] | None = None,
        play_channels: Mapping[str, ChannelSpec | str | int] | None = None,
        display_names: Mapping[str, str] | None = None,
        common_versions: Iterable[int] = (1,),
        fabric_registry_handler: Callable[[bytes], bool] | None = None,
        velocity_secret: str | bytes | None = None,
        velocity_player_ip: str = "127.0.0.1",
        accept_transfers: bool = True,
        modlist: ModListAdapter | None = None,
        block_states: BlockStateRegistry | None = None,
        block_state_report: str | None = None,
        block_state_table: str | None = None,
        physics_engine: PhysicsEngine | None = None,
        physics_attributes: PhysicsAttributes | None = None,
        # Whether an incoming knockback impulse moves the local predictor.  A
        # vanilla client never switches this off: the server computes the
        # impulse, sends it in one packet, and the client applies it on the
        # next tick, which is what every prediction check expects to see.  The
        # switch exists for callers that drive movement themselves and would
        # rather not have an impulse fight their own pathing.
        knockback: bool = True,
        # These two emulate a cheat client, not a vanilla one: ``vclip`` lets the
        # planner route through solid blocks, and ``anti_kick`` writes a
        # synthetic position into periodic movement packets.  Both are off by
        # default; a bot that turns them on is telling the server something that
        # did not happen and will be flagged wherever an anti-cheat is running.
        vclip: bool = False,
        vclip_up_limit: float = 3.0,
        vclip_down_limit: float = 2.0,
        anti_kick: bool = False,
        anti_kick_interval: float = 1.0,
    ) -> None:
        if not 1 <= len(username) <= 16:
            raise ValueError("username must contain 1 to 16 characters")
        if not 0 < port < 65536:
            raise ValueError("port must be between 1 and 65535")
        if not math.isfinite(connect_timeout) or connect_timeout <= 0.0:
            raise ValueError("connect_timeout must be a finite positive number")
        self.host = host
        self.port = port
        if handshake_host is not None and (
            not isinstance(handshake_host, str) or not handshake_host
        ):
            raise ValueError("handshake_host must be a non-empty string or None")
        self.handshake_host = handshake_host
        self.username = username
        self.version = get_version(version)
        if not isinstance(resolve_srv, bool):
            raise TypeError("resolve_srv must be a bool")
        self.resolve_srv = resolve_srv
        # The address actually dialled, which differs from host/port whenever an
        # SRV record redirects the connection.
        self.connected_host = host
        self.connected_port = port
        if profile_uuid is not None:
            self.uuid = uuid.UUID(str(profile_uuid)) if not isinstance(profile_uuid, uuid.UUID) else profile_uuid
        else:
            self.uuid = offline_uuid(username)
        self.access_token = access_token
        self.session_server = session_server
        if not isinstance(minecraft_services, str) or not minecraft_services:
            raise ValueError("minecraft_services must be a non-empty string")
        self.minecraft_services = minecraft_services
        self.session_id: uuid.UUID | None = None
        self._authenticated_connection = False
        self._chat_certificate: PlayerCertificate | None = None
        self._chat_certificate_error: str | None = None
        self._chat_session_id: uuid.UUID | None = None
        self._chat_message_index = 0
        self._chat_send_lock = asyncio.Lock()
        self._chat_certificate_refresh_task: (
            asyncio.Task[PlayerCertificate] | None
        ) = None
        self._next_chat_certificate_refresh_ms = 0
        self.connect_timeout = connect_timeout
        if isinstance(velocity_secret, str):
            velocity_secret = velocity_secret.encode("utf-8")
        elif velocity_secret is not None and not isinstance(velocity_secret, bytes):
            raise TypeError("velocity_secret must be str, bytes, or None")
        if velocity_secret is not None and not velocity_secret:
            raise ValueError("velocity_secret must not be empty")
        if not isinstance(velocity_player_ip, str) or not velocity_player_ip:
            raise ValueError("velocity_player_ip must be a non-empty string")
        try:
            ipaddress.ip_address(velocity_player_ip)
        except ValueError as error:
            raise ValueError("velocity_player_ip must be a valid IPv4 or IPv6 address") from error
        self._velocity_secret = velocity_secret
        self.velocity_player_ip = velocity_player_ip
        if not isinstance(accept_transfers, bool):
            raise TypeError("accept_transfers must be a bool")
        self.accept_transfers = accept_transfers
        self.modlist = modlist or make_adapter(
            loader,
            mods,
            protocol_version=loader_protocol,
            configuration_channels=configuration_channels,
            play_channels=play_channels,
            display_names=display_names,
            common_versions=common_versions,
            fabric_registry_handler=fabric_registry_handler,
        )
        self.state = ConnectionState.DISCONNECTED
        self.player = PlayerState()
        self.entities: dict[int, EntityState] = {}
        #: Tab list, keyed by profile UUID; drives player_join / player_leave.
        self.players: dict[uuid.UUID, PlayerListEntry] = {}
        self._roster_synced = False
        self._roster_deadline = 0.0
        self.containers: dict[int, ContainerState] = {}
        self._active_container_id: int | None = None
        self.session = WorldSessionState()
        self.registries = RegistryStore()
        self.world = World(
            block_states,
            protocol=self.version.protocol,
            release=self.version.name,
        )
        self.world.set_entity_collision_provider(self._hard_entity_collision_boxes)
        if block_state_report is not None:
            self.world.block_states.load_report(block_state_report)
        if block_state_table is not None:
            self.world.block_states.load_table(block_state_table)
        self.events = EventBus()
        self.ready = asyncio.Event()
        self.loaded = asyncio.Event()
        self.world_ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.disconnect_reason: str | None = None
        self._connection = ProtocolConnection()
        self._reader_task: asyncio.Task[None] | None = None
        self._terminal_error: BaseException | None = None
        if physics_engine is not None and physics_attributes is not None:
            raise ValueError("pass either physics_engine or physics_attributes, not both")
        self.physics = physics_engine or PhysicsEngine(physics_attributes)
        if not isinstance(knockback, bool):
            raise TypeError("knockback must be a bool")
        self.knockback = knockback
        # Bumped whenever the server sends this player a velocity.  A tick that
        # was already computed from the old velocity must not claim a position
        # that ignores the impulse; see ``MovementMixin._tick_once``.
        self._motion_epoch = 0
        if not isinstance(vclip, bool):
            raise TypeError("vclip must be a bool")
        if not all(
            math.isfinite(value) and value >= 0.0
            for value in (vclip_up_limit, vclip_down_limit)
        ):
            raise ValueError("VClip limits must be finite and non-negative")
        self.vclip = vclip
        self.vclip_up_limit = vclip_up_limit
        self.vclip_down_limit = vclip_down_limit
        if not isinstance(anti_kick, bool):
            raise TypeError("anti_kick must be a bool")
        if not math.isfinite(anti_kick_interval) or anti_kick_interval < 0.2:
            raise ValueError("anti_kick_interval must be at least 0.2 seconds")
        self.anti_kick = anti_kick
        self.anti_kick_interval = anti_kick_interval
        self._next_anti_kick = 0.0
        # Meteor's packet anti-kick mode keeps the local position untouched and
        # rewrites only a periodic movement packet to the previous packet Y -
        # 0.03130.  The server sees a tiny downward motion while the predictor
        # continues from the real coordinate.
        self._anti_kick_last_packet_y: float | None = None
        self.boat_physics = BoatPhysicsEngine()
        self.physics_state = PhysicsState()
        self._navigation_active = False
        self._navigation_planning = False
        self._navigation_claimed = False
        self._force_flight_active = False
        self._force_flight_previous = False
        self._no_fall = False
        # True while the player is lying in a bed.  A sleeping vanilla client
        # simulates nothing and sends no movement packet at all: the server keeps
        # the body on the bed, and the client's own step ends with zero
        # displacement.  The flag lets the tick loop mirror that instead of
        # claiming a fall through a bed whose collision box this library
        # approximates as a full cube.
        self.sleeping = False
        self._navigation_vclip_active = False
        self._navigation_vertical_active = False
        self._position_update_serial = 0
        # 26.3 kicks a client that sends two position-bearing movement packets
        # without a client_tick_end in between; see _send_position_packet.
        self._position_sent_this_tick = False
        # Monotonic time of the last movement packet; see _begin_tick_packet.
        self._last_tick_packet_time = 0.0
        # Monotonic instant at which the next movement packet is due.  Owed tick
        # time is carried here, never dropped, so a late caller catches up the
        # way a vanilla client does instead of stretching the tick grid.
        self._next_tick_at: float | None = None
        # Rotation carried by the last look/posRot packet; see _rotation_unchanged.
        self._last_sent_yaw = None
        self._last_sent_pitch = None
        self._last_sent_input_flags = 0
        self._last_sent_sprinting = False
        self._next_sequence = 0
        self._actions = None
        self._goals = None
        self._tasks = None
        self._menu = None
        self._layout_cache = {}
        self._last_seen_messages = _LastSeenMessagesTracker()
        # One tick is one movement packet.  Goals, actions and a caller's own
        # loop can all ask for one at the same time, and two bodies running at
        # once would advance the physics twice and report it as a single step --
        # a position the server cannot predict.  ``_action_busy_until`` tells a
        # caller-side tick loop to stay out of a tick an action has reserved.
        self._tick_lock = asyncio.Lock()
        # The position of the last position-carrying packet, so a rotation that
        # does not move the player can go out as a rotation-only packet, the way
        # vanilla sends it.
        self._last_sent_position: tuple[float, float, float] | None = None
        self._action_busy_until = 0.0
        # Pongs held back while an action is between its swing and the movement
        # packet of the same tick; they go out once that tick is closed, because
        # a pong is the moment an anti-cheat judges the packets queued before it.
        self._pong_pending: list[int] = []
        # The tick driver (protobot/driver.py).  `input` is what the driver
        # holds down every tick; a caller that awaits something slow sets it and
        # lets the pump keep the stream at 20 Hz instead of freezing the client.
        self.input = MovementInput()
        self._driver_task: asyncio.Task[None] | None = None
        self._driver_error: BaseException | None = None
        # Monotonic time of the last completed tick, and how many have run: the
        # driver yields a slot the caller already ticked, and wait_ticks counts
        # game ticks instead of wall-clock seconds.
        self._last_tick_at = 0.0
        self._ticks_completed = 0

    @property
    def active_container(self) -> ContainerState | None:
        """Return the currently open menu, if the server has opened one."""

        if self._active_container_id is None:
            return None
        return self.containers.get(self._active_container_id)

    @property
    def online_players(self) -> tuple[str, ...]:
        """Names in the tab list, sorted; empty before the first roster.

        This is who the server says is online, not who is nearby -- unlike
        :attr:`entities`, it is not limited to the loaded chunks.
        """

        return tuple(sorted(entry.name for entry in self.players.values() if entry.name))

    @property
    def loaded_chunk_bounds(self) -> tuple[int, int, int, int] | None:
        """Return ``(min_x, max_x, min_z, max_z)`` for currently loaded chunks."""

        if not self.world.chunks:
            return None
        xs = [key[0] for key in self.world.chunks]
        zs = [key[1] for key in self.world.chunks]
        return min(xs), max(xs), min(zs), max(zs)

    @property
    def loaded_chunk_radius(self) -> int | None:
        """Return the loaded chunk radius around the bot's current chunk."""

        bounds = self.loaded_chunk_bounds
        if bounds is None:
            return None
        chunk_x = math.floor(self.physics_state.position.x) >> 4
        chunk_z = math.floor(self.physics_state.position.z) >> 4
        return max(
            abs(chunk_x - bounds[0]),
            abs(bounds[1] - chunk_x),
            abs(chunk_z - bounds[2]),
            abs(bounds[3] - chunk_z),
        )

    def find_player(self, name: str) -> PlayerListEntry | None:
        """Look a tab-list entry up by name, case-insensitively."""

        wanted = name.strip().lower()
        for entry in self.players.values():
            if entry.name.lower() == wanted:
                return entry
        return None

    def find_player_entity(self, name: str) -> EntityState | None:
        """Return the nearby entity matching a tab-list player name.

        The tab list is the authoritative name-to-UUID mapping, while entity
        spawn/move packets carry the position.  Keeping the lookup here lets
        callers work for players who have not sent a chat message and avoids
        making each plugin duplicate the UUID join logic.
        """

        entry = self.find_player(name)
        if entry is None:
            return None
        wanted = entry.uuid
        for entity in self.entities.values():
            if entity.entity_uuid == wanted:
                return entity
        return None

    def find_player_position(self, name: str) -> tuple[float, float, float] | None:
        """Return a nearby tab-listed player's latest XYZ position."""

        entity = self.find_player_entity(name)
        return None if entity is None else entity.position

    @property
    def visible_players(self) -> tuple[tuple[PlayerListEntry, EntityState], ...]:
        """Return tab-listed players whose entity position is currently known."""

        entities = {
            entity.entity_uuid: entity
            for entity in self.entities.values()
            if entity.entity_uuid is not None
        }
        return tuple(
            (entry, entities[entry.uuid])
            for entry in self.players.values()
            if entry.name and entry.uuid in entities
        )

    async def click_container(
        self,
        slot: int,
        *,
        button: int = 0,
        click_type: str | int = "pickup",
        container_id: int | None = None,
        state_id: int | None = None,
        changed_slots: Mapping[int, bytes] | None = None,
        carried_item_data: bytes | None = None,
    ) -> None:
        """Send a container click using the current menu state.

        The default is the common left-click/PICKUP action with no client-side
        slot changes and an empty carried ``HashedStack``. Advanced callers may
        provide the raw encoded bodies of changed or carried hashed stacks.
        """

        self._require_play()
        active = self.active_container
        if container_id is None:
            if active is None:
                raise RuntimeError("cannot click a container before the server opens one")
            container_id = active.container_id
        if state_id is None:
            state_id = (
                active.state_id
                if active is not None and active.container_id == container_id
                else 0
            )
        if not -32768 <= slot <= 32767:
            raise ValueError("container slot must fit a signed short")
        if not -128 <= button <= 127:
            raise ValueError("container button must fit a signed byte")
        if isinstance(click_type, str):
            try:
                click_type = _CONTAINER_CLICK_TYPES[click_type.lower()]
            except KeyError as error:
                raise ValueError(f"unknown container click type {click_type!r}") from error
        if not 0 <= click_type <= 6:
            raise ValueError("container click type must be between 0 and 6")
        if not 0 <= state_id < (1 << 31):
            raise ValueError("container state ID must be a non-negative VarInt")
        changed_slots = changed_slots or {}
        if len(changed_slots) > 128:
            raise ValueError("container click cannot contain more than 128 changed slots")
        writer = (
            PacketWriter()
            .write_varint(container_id)
            .write_varint(state_id)
            .write_short(slot)
            .write_byte(button)
            .write_varint(click_type)
            .write_varint(len(changed_slots))
        )
        for changed_slot, stack_data in changed_slots.items():
            if not -32768 <= changed_slot <= 32767:
                raise ValueError("changed container slot must fit a signed short")
            if not isinstance(stack_data, (bytes, bytearray, memoryview)):
                raise TypeError("changed slot data must be bytes")
            writer.write_short(changed_slot).write_bool(True).write_raw(stack_data)
        if carried_item_data is None:
            writer.write_bool(False)
        else:
            if not isinstance(carried_item_data, (bytes, bytearray, memoryview)):
                raise TypeError("carried item data must be bytes")
            writer.write_bool(True).write_raw(carried_item_data)
        await self.send_raw(self.version.packets.serverbound_container_click, writer.to_bytes())

    async def click_container_button(
        self, button: int, *, container_id: int | None = None
    ) -> None:
        """Press one of a menu's buttons.

        Vanilla uses this for the three enchanting options, the stonecutter
        recipe index and the loom pattern; the server validates the press
        against the open menu, so the slot contents have to be in place first.
        """

        self._require_play()
        if container_id is None:
            active = self.active_container
            if active is None:
                raise RuntimeError("cannot press a menu button before the server opens one")
            container_id = active.container_id
        if not 0 <= button < (1 << 31):
            raise ValueError("menu button must be a non-negative VarInt")
        await self.send_raw(
            self.version.packets.serverbound_container_button_click,
            PacketWriter().write_varint(container_id).write_varint(button).to_bytes(),
        )

    async def rename_container_item(
        self, name: str, *, container_id: int | None = None
    ) -> None:
        """Set the name in an anvil's (or smithing table's) text box."""

        self._require_play()
        if not isinstance(name, str):
            raise TypeError("container item name must be a string")
        if not name:
            raise ValueError("an empty name clears the box; send the item name instead")
        await self.send_raw(
            self.version.packets.serverbound_rename_item,
            PacketWriter().write_string(name).to_bytes(),
        )

    async def close_container(self, container_id: int | None = None) -> None:
        """Ask the server to close the active container."""

        self._require_play()
        if container_id is None:
            active = self.active_container
            if active is None:
                raise RuntimeError("cannot close a container when none is open")
            container_id = active.container_id
        await self.send_raw(
            self.version.packets.serverbound_container_close,
            PacketWriter().write_varint(container_id).to_bytes(),
        )
        # The server does not echo our own close, so clear the local view here:
        # later clicks still name this container id, and the server would ignore
        # them because it has already closed the window.
        closed = self.containers.get(container_id)
        if closed is not None:
            closed.is_open = False
        if self._active_container_id == container_id:
            self._active_container_id = None
        zero = self.containers.get(PLAYER_CONTAINER_ID)
        if zero is not None:
            for slot in range(PLAYER_INVENTORY_LAST_SLOT + 1):
                stack = self.player.inventory.get(slot)
                if stack is not None:
                    zero.items[slot] = stack
                    zero.unreadable.discard(slot)

    @property
    def actions(self):
        """Interaction primitives (dig, place, attack, ...), created on demand.

        The object only stores a reference to this bot, so it stays valid
        across reconnects and is cheap enough to read on every use.
        """

        if self._actions is None:
            from .actions import Actions

            self._actions = Actions(self)
        return self._actions

    @property
    def goals(self):
        """Goal runner: declarative objectives retried until they are true.

        Created on demand and bound to this bot, so it survives reconnects the
        same way :attr:`actions` does.
        """

        if self._goals is None:
            from .goals import GoalRunner

            self._goals = GoalRunner(self)
        return self._goals

    @property
    def tasks(self):
        """Task runner: scripts of steps with retries and a report."""

        if self._tasks is None:
            from .tasks import TaskRunner

            self._tasks = TaskRunner(self)
        return self._tasks

    @property
    def menu(self):
        """The window the server currently has open, with named slots.

        Resolves the active container on every use, so it follows chests being
        opened and closed; :attr:`player_menu` always addresses the player's
        own inventory (container 0), which is never "opened".
        """

        if self._menu is None:
            from .containers import Menu

            self._menu = Menu(self)
        return self._menu

    @property
    def player_menu(self):
        """The player's own inventory window (container 0)."""

        from .containers import player_menu

        return player_menu(self)

    def take_sequence(self) -> int:
        """Allocate the next interaction sequence number.

        ``PlayerAction``, ``UseItem`` and ``UseItemOn`` all carry a sequence
        that the server may acknowledge; vanilla keeps one counter per client
        and this is it, so the interaction layer and :meth:`use_item` share a
        single monotonic stream.
        """

        sequence = self._next_sequence
        self._next_sequence = (sequence + 1) & 0x7FFFFFFF
        return sequence

    async def use_item(
        self,
        *,
        hand: str | int = "main_hand",
        sequence: int | None = None,
        yaw: float | None = None,
        pitch: float | None = None,
    ) -> int:
        """Use the held item and return the sequence number sent on the wire."""

        self._require_play()
        if isinstance(hand, str):
            try:
                hand = _INTERACTION_HANDS[hand.lower()]
            except KeyError as error:
                raise ValueError(f"unknown interaction hand {hand!r}") from error
        if hand not in (0, 1):
            raise ValueError("interaction hand must be main_hand (0) or off_hand (1)")
        if sequence is None:
            sequence = self.take_sequence()
        elif not 0 <= sequence < (1 << 31):
            raise ValueError("use-item sequence must be a non-negative VarInt")
        yaw = self.player.yaw if yaw is None else yaw
        pitch = self.player.pitch if pitch is None else pitch
        self._validate_finite((yaw, pitch), "use-item rotation")
        payload = (
            PacketWriter()
            .write_unsigned_byte(hand)
            .write_varint(sequence)
            .write_float(yaw)
            .write_float(pitch)
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_use_item, payload)
        return sequence

    async def select_hotbar_slot(self, slot: int) -> None:
        """Select a zero-based hotbar slot using vanilla's carried-item packet."""

        self._require_play()
        if not isinstance(slot, int) or isinstance(slot, bool):
            raise TypeError("hotbar slot must be an int")
        if not 0 <= slot <= 8:
            raise ValueError("hotbar slot must be between 0 and 8")
        if self.player.selected_hotbar_slot == slot:
            # Vanilla never tells the server about a slot it is already holding.
            # Repeating it is what anti-cheat flags as "sent duplicate slot id"
            # (Grim's BadPacketsA), and the server learns nothing from it.
            return
        # The switch belongs to a tick: vanilla sends it with the input of the
        # tick it happened in, right before that tick's movement packet.  A
        # carried-item packet that sits alone until some later tick is what
        # Grim's Post check flags ("held item change"), so flush it here.
        await self.wait_tick_budget()
        await self.send_raw(
            self.version.packets.serverbound_set_carried_item,
            PacketWriter().write_short(slot).to_bytes(),
        )
        self.player.selected_hotbar_slot = slot
        await self.tick()

    async def set_hotbar_slot(self, slot: int) -> None:
        """Alias for :meth:`select_hotbar_slot` used by inventory clients."""

        await self.select_hotbar_slot(slot)

    async def switch_slot(self, slot: int) -> None:
        """Select a hotbar slot and synchronize the carried-item packet."""

        await self.select_hotbar_slot(slot)

    async def switch_hotbar_slot(self, slot: int) -> None:
        """Alias for :meth:`select_hotbar_slot`."""

        await self.select_hotbar_slot(slot)

    async def select_slot(self, slot: int) -> None:
        """Short alias for selecting a zero-based hotbar slot."""

        await self.select_hotbar_slot(slot)

    async def set_selected_slot(self, slot: int) -> None:
        """Set the currently held hotbar slot."""

        await self.select_hotbar_slot(slot)

    @property
    def selected_hotbar_slot(self) -> int:
        """The currently selected zero-based hotbar slot."""

        return self.player.selected_hotbar_slot

    @property
    def held_item(self) -> ItemStack:
        """The latest item snapshot for the currently selected hotbar slot."""

        return self.get_inventory_item(36 + self.player.selected_hotbar_slot)

    @property
    def inventory(self) -> Mapping[int, ItemStack]:
        """Return a read-only view of the latest player inventory snapshot."""

        return self.player.inventory

    def get_inventory_item(self, slot: int) -> ItemStack:
        """Return a player inventory slot, or an empty stack when unknown."""

        if not isinstance(slot, int) or isinstance(slot, bool):
            raise TypeError("inventory slot must be an int")
        if not 0 <= slot <= 45:
            raise ValueError("inventory slot must be between 0 and 45")
        return self.player.inventory.get(slot, ItemStack())

    def inventory_item(self, slot: int) -> ItemStack:
        """Alias for :meth:`get_inventory_item`."""

        return self.get_inventory_item(slot)

    def get_slot(self, slot: int) -> ItemStack:
        """Short alias for :meth:`get_inventory_item`."""

        return self.get_inventory_item(slot)

    def get_inventory(self) -> Mapping[int, ItemStack]:
        """Return the latest player inventory snapshot."""

        return self.inventory

    async def click_inventory(
        self,
        slot: int,
        *,
        button: int = 0,
        click_type: str | int = "pickup",
        state_id: int = 0,
        changed_slots: Mapping[int, bytes] | None = None,
        carried_item_data: bytes | None = None,
    ) -> None:
        """Click a player inventory slot through the player's container (0).

        When a menu is open, callers should use :meth:`click_container` with
        that menu's container id because slot numbering is menu-specific.
        """

        if not isinstance(slot, int) or isinstance(slot, bool):
            raise TypeError("inventory slot must be an int")
        if not 0 <= slot <= 45:
            raise ValueError("inventory slot must be between 0 and 45")
        await self.click_container(
            slot,
            button=button,
            click_type=click_type,
            container_id=0,
            state_id=state_id,
            changed_slots=changed_slots,
            carried_item_data=carried_item_data,
        )

    async def move_inventory_item(
        self,
        slot: int,
        *,
        quick_move: bool = True,
        state_id: int = 0,
    ) -> None:
        """Move an item between the container and player inventory."""

        await self.click_inventory(
            slot,
            click_type="quick_move" if quick_move else "pickup",
            state_id=state_id,
        )

    async def click_slot(self, slot: int, **kwargs) -> None:  # type: ignore[no-untyped-def]
        """Short alias for :meth:`click_inventory`."""

        await self.click_inventory(slot, **kwargs)

    async def transfer_inventory_item(
        self,
        slot: int,
        *,
        state_id: int = 0,
    ) -> None:
        """Quick-move an item to the other side of the current container."""

        await self.move_inventory_item(slot, quick_move=True, state_id=state_id)

    async def drop_inventory_item(
        self,
        slot: int,
        *,
        whole_stack: bool = False,
        state_id: int = 0,
    ) -> None:
        """Drop one item (or the whole stack) from a player inventory slot."""

        await self.click_inventory(
            slot,
            button=1 if whole_stack else 0,
            click_type="throw",
            state_id=state_id,
        )

    async def drop_item(self, slot: int, **kwargs) -> None:  # type: ignore[no-untyped-def]
        """Short alias for :meth:`drop_inventory_item`."""

        await self.drop_inventory_item(slot, **kwargs)

    def load_block_state_report(self, report: str) -> int:
        """Install a Mojang ``blocks.json`` report for collision prediction."""

        return self.world.block_states.load_report(report)



async def connect(
    host: str,
    *,
    port: int = 25565,
    handshake_host: str | None = None,
    username: str = "ProtoBot",
    version: str | VersionSpec = "26.2",
    timeout: float = 30.0,
    resolve_srv: bool = True,
    access_token: str | None = None,
    profile_uuid: str | uuid.UUID | None = None,
    session_server: str = "https://sessionserver.mojang.com",
    minecraft_services: str = "https://api.minecraftservices.com",
    loader: Loader | str = Loader.VANILLA,
    mods: Mapping[str, str] | None = None,
    loader_protocol: int = 0,
    configuration_channels: Mapping[str, ChannelSpec | str | int] | None = None,
    play_channels: Mapping[str, ChannelSpec | str | int] | None = None,
    display_names: Mapping[str, str] | None = None,
    common_versions: Iterable[int] = (1,),
    fabric_registry_handler: Callable[[bytes], bool] | None = None,
    velocity_secret: str | bytes | None = None,
    velocity_player_ip: str = "127.0.0.1",
    accept_transfers: bool = True,
    modlist: ModListAdapter | None = None,
    block_states: BlockStateRegistry | None = None,
    block_state_report: str | None = None,
    block_state_table: str | None = None,
    physics_engine: PhysicsEngine | None = None,
    physics_attributes: PhysicsAttributes | None = None,
    knockback: bool = True,
    # Cheat emulation, off by default: see the note on ``Bot.__init__``.
    vclip: bool = False,
    vclip_up_limit: float = 3.0,
    vclip_down_limit: float = 2.0,
    anti_kick: bool = False,
    anti_kick_interval: float = 1.0,
) -> Bot:
    """Connect a bot (offline or online) and return it after the initial spawn position."""
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("timeout must be a finite positive number")
    bot = Bot(
        host,
        port=port,
        handshake_host=handshake_host,
        username=username,
        version=version,
        connect_timeout=timeout,
        resolve_srv=resolve_srv,
        access_token=access_token,
        profile_uuid=profile_uuid,
        session_server=session_server,
        minecraft_services=minecraft_services,
        loader=loader,
        mods=mods,
        loader_protocol=loader_protocol,
        configuration_channels=configuration_channels,
        play_channels=play_channels,
        display_names=display_names,
        common_versions=common_versions,
        fabric_registry_handler=fabric_registry_handler,
        velocity_secret=velocity_secret,
        velocity_player_ip=velocity_player_ip,
        accept_transfers=accept_transfers,
        modlist=modlist,
        block_states=block_states,
        block_state_report=block_state_report,
        block_state_table=block_state_table,
        physics_engine=physics_engine,
        physics_attributes=physics_attributes,
        knockback=knockback,
        vclip=vclip,
        vclip_up_limit=vclip_up_limit,
        vclip_down_limit=vclip_down_limit,
        anti_kick=anti_kick,
        anti_kick_interval=anti_kick_interval,
    )
    try:
        await bot.start()
        await bot.wait_ready(timeout=timeout)
        await bot.wait_loaded(timeout=timeout)
        return bot
    except BaseException:
        await bot.close()
        raise
