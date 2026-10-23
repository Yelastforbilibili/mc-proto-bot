"""Every tolerance this library chooses for itself, in one place.

The numbers here are *ours*: cadences, epsilons, margins and ranges we picked so
a bot's packet stream looks like a vanilla client's.  They are deliberately not
vanilla constants.

Vanilla constants -- the ones transcribed out of ``LivingEntity``,
``BlockBehaviour``, ``ServerPlayerGameMode`` or the protocol -- stay next to the
code that mirrors them (``physics/engine.py``, ``protocol/breaking.py``,
``protocol/tools.py``).  Moving those away from the source line they were copied
from would make them harder to audit against the server jar, which is the only
thing that keeps the physics honest.

Change a value here when you want a different trade-off; a few tests pin the
defaults on purpose (``tests/test_actions.py``, ``tests/test_containers.py``),
so a change that breaks the vanilla-shaped packet sequence will show up.
"""

from __future__ import annotations

__all__ = [
    "AIM_INSIDE_OFFSET",
    "ANTI_KICK_WIRE_OFFSET",
    "BLOCK_INTERACTION_RANGE",
    "BREAK_COOLDOWN_SECONDS",
    "CLICK_TIMEOUT_SECONDS",
    "CONFIRM_TIMEOUT_SECONDS",
    "DEFAULT_HOLD_SECONDS",
    "EARLY_RELEASE_TOLERANCE",
    "ENTITY_ATTACK_RANGE",
    "GOAL_POLL_SECONDS",
    "HOLD_MARGIN_SECONDS",
    "IDLE_TICK_SECONDS",
    "MAX_CATCH_UP_TICKS",
    "MIN_TICK_GAP",
    "POLL_SECONDS",
    "QUIET_SECONDS",
    "ROTATION_EPSILON",
    "TICK_DEBT_RESET_SECONDS",
    "TICK_SECONDS",
    "VANILLA_ROTATION_EPSILON_SQUARED",
]

# ---------------------------------------------------------------------------
# tick cadence
# ---------------------------------------------------------------------------

#: One Minecraft tick.  Movement packets go out at this cadence and nothing
#: faster: Grim's ``Timer`` adds 50 ms per movement packet and flags a client
#: that runs ahead of real time.
TICK_SECONDS = 0.05

#: Minimum wall-clock gap between two movement packets (same value, named for
#: the place it is enforced: ``Client._begin_tick_packet``).  A burst -- an aim
#: immediately followed by a position packet -- waits this long.
MIN_TICK_GAP = TICK_SECONDS

#: When a tool that is supposed to keep the client ticking (the acceptance
#: suite) sees a gap wider than this, it ticks to fill it.  A vanilla client
#: never stops sending movement; a hole in the stream makes the next step
#: disagree with the server's prediction by most of a step.
IDLE_TICK_SECONDS = 0.075

#: How many ticks one late call may run back to back.
#:
#: A vanilla client drains up to ten per frame (``Minecraft``:
#: ``MAX_TICKS_PER_UPDATE = 10``) because a real client's frame can take 500 ms
#: and it keeps the unpaid remainder in ``DeltaTracker``.  We cannot afford ten:
#: an anti-cheat measures those bursts against the wall clock.  Grim's ``Timer``
#: adds 50 ms per movement packet, subtracts real time, flags the moment the
#: balance goes positive, and clamps how far behind it remembers at
#: ``Timer.drift`` = 120 ms.  Spending two slots (100 ms) still fits inside that
#: allowance, so the knockback arc keeps its shape across an ordinary await
#: instead of stretching, without the client ever claiming to run ahead of time.
MAX_CATCH_UP_TICKS = 2

#: The widest debt one call will replay.  Anything older cannot be spent inside
#: the allowance above: the packets would have to go out faster than real time,
#: which is exactly what ``Timer`` flags.  It is a caller that stopped ticking
#: (waiting for a chunk, a path plan, a chat round trip), so the grid re-anchors
#: to now and the unspendable debt is dropped -- the honest record of a client
#: that was not running.  A vanilla client needs no valve because its loop never
#: stops; keeping our own loop running is the caller's or a driver's job.
TICK_DEBT_RESET_SECONDS = MAX_CATCH_UP_TICKS * TICK_SECONDS

# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------

#: Degrees.  Two angles closer than this are the same aim, so a second packet
#: carrying them is a duplicate that anti-cheat checks flag.
ROTATION_EPSILON = 1.0e-4

#: Squared degrees.  Vanilla's own threshold for "the rotation changed"
#: (``dYaw^2 + dPitch^2 > 9.0E-6`` in ``LocalPlayer``/``MultiPlayerGameMode``),
#: used when mirroring that decision.
VANILLA_ROTATION_EPSILON_SQUARED = 9.0e-6

# ---------------------------------------------------------------------------
# ranges and aiming
# ---------------------------------------------------------------------------

#: Blocks.  Vanilla's block interaction range; anything further is a click the
#: server refuses to process.
BLOCK_INTERACTION_RANGE = 4.5

#: Blocks.  Vanilla's attack range for an empty hand.
ENTITY_ATTACK_RANGE = 3.0

#: Blocks.  How far inside a face the aim point sits.  Aiming at the exact face
#: centre puts the ray on the boundary between two boxes, where rounding decides
#: which one a ray trace hits.
AIM_INSIDE_OFFSET = 0.35

#: Seconds.  How long an action waits for the server to confirm it before it
#: reports ``UNCONFIRMED``.
CONFIRM_TIMEOUT_SECONDS = 0.75

# ---------------------------------------------------------------------------
# breaking
# ---------------------------------------------------------------------------

#: Seconds added to the predicted break time before the button is released.
#: A release before the server has finished is an early release, and vanilla
#: closes an early release with ABORT.
HOLD_MARGIN_SECONDS = 0.15

#: Seconds.  Hold time for a block whose break time is unknown.
DEFAULT_HOLD_SECONDS = 2.0

#: Seconds.  A hold this much shorter than the prediction counts as an early
#: release (and is reported as such instead of being sent as STOP).
EARLY_RELEASE_TOLERANCE = 0.05

#: Seconds.  Vanilla's ``MultiPlayerGameMode.destroyDelay`` is five ticks: after
#: a block breaks the client refuses to start the next dig for that long.
#: Grim's ``FastBreak`` adds ``300 ms - gap`` to a balance for a start sooner than
#: 275 ms and cancels the dig once it passes a second.
BREAK_COOLDOWN_SECONDS = 0.3

# ---------------------------------------------------------------------------
# container pacing
# ---------------------------------------------------------------------------

#: Seconds to wait for a click to be confirmed by a slot update.
CLICK_TIMEOUT_SECONDS = 1.0

#: Seconds of silence that mark a window update as settled.
QUIET_SECONDS = 0.05

#: Seconds between polls while waiting for a window update.
POLL_SECONDS = 0.005

#: Seconds between polls in the goal layer (travel, wait-for-chunk).
GOAL_POLL_SECONDS = 0.05

# ---------------------------------------------------------------------------
# emulation features that are off by default
# ---------------------------------------------------------------------------

#: Blocks.  The downward nudge Meteor's packet anti-kick mode writes into a
#: periodic movement packet to keep a server's position check happy while the
#: bot hovers.  Only used when ``connect(anti_kick=True)``; it is a lie about the
#: player's position, so it is opt-in and documented as such.
ANTI_KICK_WIRE_OFFSET = 0.03130
