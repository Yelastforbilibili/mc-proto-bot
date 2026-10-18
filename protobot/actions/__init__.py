"""Interaction layer: the packets vanilla sends when the player digs, places,
attacks, interacts or swings.

The rest of ProtoBot can build a player and walk it around, but until now it
could not *touch* the world: nothing in the library ever sent
``PlayerAction``, ``UseItemOn``, ``Interact`` or ``Swing``.  This package is
that missing half, in three pieces:

``raycast``
    Which block is under the crosshair, and through which face.  Servers
    validate the target block and face, so every primitive below aims first.
``interactions``
    One ``Actions`` object per client with ``dig``, ``place_block``,
    ``attack``, ``interact``, ``swing`` and friends.  Each call owns its
    sequence number, its aiming and its completion condition, and returns an
    :class:`~protobot.actions.results.ActionResult` instead of a bare packet
    id, so callers can assert on what the *server* did.
``results`` / ``errors``
    The vocabulary for "it happened", "nothing happened" and "this release
    does not have a verified id for that packet".

Reachability is version aware.  ``VersionSpec.packets`` leaves an unaudited
packet at ``0``; the primitives raise :class:`UnsupportedAction` rather than
guess an id, which is why the same call is safe on 1.21.8, 26.1 and 26.3.

Ids are only half of it: vanilla also changed the payloads.  ``profiles`` holds
the per-release wire shapes (``Action`` ordinals, whether a swing carries a
hand, whether ``Interact`` still has a type tag), read out of each release's own
server jar, so a primitive branches on data rather than on a guess.
"""

from .errors import ActionError, ActionTimeout, UnsupportedAction
from .interactions import Actions
from .profiles import ActionCodes, InteractionProfile, profile_for
from .raycast import (
    BlockFace,
    BlockHit,
    EntityHit,
    entity_bounds,
    look_angles,
    raycast_blocks,
    raycast_entities,
)
from .results import ActionResult, ActionStatus

__all__ = [
    "ActionCodes",
    "ActionError",
    "ActionResult",
    "ActionStatus",
    "ActionTimeout",
    "Actions",
    "BlockFace",
    "BlockHit",
    "EntityHit",
    "InteractionProfile",
    "UnsupportedAction",
    "entity_bounds",
    "look_angles",
    "profile_for",
    "raycast_blocks",
    "raycast_entities",
]
