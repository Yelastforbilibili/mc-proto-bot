"""Per-release wire shapes for the interaction packets.

The packet *ids* live in :mod:`protobot.protocol.versions` and are audited
against Mojang's own reports.  Ids are only half of the story: vanilla also
changed the *payload* of the same packet name between releases, and an id table
cannot express that.  This module records the payload differences, one row per
protocol, so every primitive in :mod:`protobot.actions.interactions` can branch
on data instead of on a hard-coded guess.

Three things changed inside the payloads:

``Action`` ordinals
    ``ServerboundPlayerActionPacket.Action`` is written with ``writeEnum``, so
    the ordinal *is* the wire value.  26.3 inserted ``CHANGE_DESTROY_DIRECTION``
    at index 1, which pushed ``STOP_DESTROY_BLOCK`` from 2 to 3.  A client that
    hard-codes 2 therefore sends ``ABORT`` on 26.3 and the block never breaks.

``Swing`` payload
    26.3 replaced ``ServerboundSwingPacket`` (one ``InteractionHand``) with
    ``ServerboundPunchPacket``, which has *no* fields at all.

``Interact`` layout
    Up to 1.21.x the packet is ``entityId + action type (+ position) (+ hand)``.
    26.1 flattened it to ``entityId + hand + location + usingSecondaryAction``
    with no type at all -- the server decides what the click means.

Every row below was read out of that release's own server jar (``javap`` for
the enum order and the record components, CFR for the payload writer), not
guessed from a wiki.  The provenance is on the row.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionCodes:
    """Ordinals of ``ServerboundPlayerActionPacket.Action``.

    Only the codes the library actually sends are modelled; ``STAB`` and
    ``CHANGE_DESTROY_DIRECTION`` are named here so the shift is not a mystery
    the next time vanilla inserts one.
    """

    start: int
    abort: int
    stop: int
    drop_stack: int
    drop_item: int
    release_use_item: int
    swap_offhand: int


@dataclass(frozen=True)
class InteractionProfile:
    """How one release encodes the interaction packets."""

    #: ``ServerboundPlayerActionPacket.Action`` ordinals.
    codes: ActionCodes
    #: ``"hand"`` writes one ``InteractionHand``, ``"empty"`` writes nothing.
    swing_payload: str
    #: ``"legacy"`` is the type-tagged layout, ``"target"`` the flattened one.
    interact_layout: str
    #: Whether a dedicated ``attack`` packet exists (else ``Interact`` ATTACK).
    attack_packet: bool
    #: Where the row came from, so a future maintainer can re-verify it.
    source: str

    @property
    def swing_is_empty(self) -> bool:
        return self.swing_payload == "empty"

    @property
    def interact_is_flat(self) -> bool:
        return self.interact_layout == "target"


#: 1.21.x through 26.2: STAB was appended *after* SWAP_ITEM_WITH_OFFHAND in
#: 26.1/26.2, so ordinals 0..6 are unchanged from 1.21.x.
_CLASSIC = ActionCodes(
    start=0,
    abort=1,
    stop=2,
    drop_stack=3,
    drop_item=4,
    release_use_item=5,
    swap_offhand=6,
)

#: 26.3 only: CHANGE_DESTROY_DIRECTION took index 1.
_SHIFTED = ActionCodes(
    start=0,
    abort=2,
    stop=3,
    drop_stack=4,
    drop_item=5,
    release_use_item=6,
    swap_offhand=7,
)

_LEGACY = InteractionProfile(
    codes=_CLASSIC,
    swing_payload="hand",
    interact_layout="legacy",
    attack_packet=False,
    source="1.21.x ServerboundInteractPacket(type, hand); no ServerboundAttackPacket",
)

_FLAT = InteractionProfile(
    codes=_CLASSIC,
    swing_payload="hand",
    interact_layout="target",
    attack_packet=True,
    source="26.1/26.2: ServerboundInteractPacket(entityId, hand, Vec3, bool), ServerboundSwingPacket(hand), ServerboundAttackPacket(entityId)",
)

_FLAT_PUNCH = InteractionProfile(
    codes=_SHIFTED,
    swing_payload="empty",
    interact_layout="target",
    attack_packet=True,
    source="26.3: ServerboundPunchPacket() with StreamCodec.unit, Action has CHANGE_DESTROY_DIRECTION at 1",
)

#: Keyed by protocol version, because that is what identifies a wire format.
PROFILES: dict[int, InteractionProfile] = {
    772: _LEGACY,  # 1.21.8
    774: _LEGACY,  # 1.21.11
    775: _FLAT,  # 26.1 / 26.1.1 / 26.1.2
    776: _FLAT,  # 26.2
    777: _FLAT_PUNCH,  # 26.3
}


def profile_for(protocol: int) -> InteractionProfile:
    """The interaction profile for ``protocol``.

    Raises :class:`KeyError` with the protocol number when the release was never
    audited -- the caller turns that into :class:`~protobot.actions.errors.UnsupportedAction`.
    """

    try:
        return PROFILES[protocol]
    except KeyError:
        raise KeyError(f"no audited interaction profile for protocol {protocol}") from None
