# How the bot is put together

This document is the map of `protobot/`: what each module owns, which way the
dependencies point, and which invariants the code is arranged to protect. Read
it before adding a packet handler or a movement feature, because both have a
single correct home.

Until recently all of this lived in one 5,600-line `client.py` with a
194-method `Bot` class. The behaviour did not change in the split -- the same
tests and the same 23-case acceptance suite pass before and after -- only where
the code lives.

## The modules

| File | Lines | Owns |
| --- | --- | --- |
| `protobot/client.py` | 826 | `Bot` itself: constructor, lazily built high-level layers, state accessors, `connect()` |
| `protobot/connection.py` | 571 | opening, reading, keeping and closing the socket; login, chat signing, transfers |
| `protobot/movement.py` | 1,137 | everything the client says about position and rotation, and the 20 Hz tick grid |
| `protobot/driver.py` | 200 | the background 20 Hz driver: `bot.driving()`, `wait_ticks`, `driver_error` |
| `protobot/expectation.py` | 150 | `Outcome`/`ExpectationStatus` and `wait_for_expectation`, the one "is it true yet?" loop |
| `protobot/play/router.py` | 2,086 | clientbound play-state packets: decode, update the local model, emit an event |
| `protobot/navigator.py` | 946 | planning routes and following them |
| `protobot/support.py` | 349 | constants, small dataclasses and pure helpers shared by the above |
| `protobot/session.py` | -- | `BotSession`: one connection policy, a fresh `Bot` per attempt, reconnect |

`Bot` is assembled from four mixins:

```python
class Bot(ConnectionMixin, MovementMixin, PlayRouterMixin, NavigatorMixin):
```

The mixins are plain classes whose methods call each other through `self`, so
there is no indirection layer and no duplicated state: `self.player`,
`self.physics_state`, `self.world` and `self.events` are created once in
`Bot.__init__` and every concern uses those same objects. The only rule is that
a mixin may import from `support`, `state`, `physics`, `protocol` and `world`,
and never from another mixin's module -- if two concerns need each other, the
call goes through `self`.

## The packet path

```
socket ─► ProtocolConnection ─► ConnectionMixin._read_loop        (connection.py:360)
                                    │
                                    ├─ configuration state ─► handled in place
                                    └─ play state ─────────► PlayRouterMixin._handle_play  (play/router.py:67)
                                                                 │
                                                                 ├─ decode payload
                                                                 ├─ mutate the model (player, world, entities, containers)
                                                                 └─ await self.events.emit("<name>", ...)
```

Two rules keep this debuggable:

* **One handler, one packet.** `_handle_play` is a dispatch table keyed by the
  packet ids in `ProtocolVersion.packets`; a packet that is not in the table is
  logged at debug level and dropped, never guessed at.
* **Handlers never write to the socket.** Sending belongs to `actions`,
  `movement` or `connection`. A handler that needs to reply (keep-alive, chat
  acknowledgement) calls a connection method, so the "who can send" question has
  one answer per packet.

Every handler emits an event after it has updated state, and the event carries
the decoded values, not the payload. That is what `tools/live_suite.py` and the
`docs/*.md` proofs assert against.

## The tick path

A vanilla client sends **exactly one movement packet per tick**, and a rotation
rides inside that packet. Two packets in one tick makes the server simulate two
steps of movement against one step of physics, which shows up as a
`Simulation`/`Timer` alert from an anti-cheat and as a position correction from
the server.

`MovementMixin.tick` (movement.py:70) is the only place that advances physics,
and the tick is a **grid**, not a sleep: `self._next_tick_at` holds the monotonic
instant at which the next movement packet is due, and every send calls
`_begin_tick_packet` (movement.py:520), which

* closes the previous tick when one was already sent in it (26.3's
  `client_tick_end` must appear exactly once per movement packet),
* sleeps only for the remainder of the current slot, and then
* advances the grid by exactly `TICK_SECONDS` -- `scheduled + 50 ms`, never
  `now + 50 ms`, so a caller that is late does not stretch the grid.

`tick` then sends **as many ticks as are owed** (`_ticks_due`), up to
`MAX_CATCH_UP_TICKS` (2). The cap is not vanilla's -- a vanilla client replays up
to 10 ticks per frame -- but vanilla is not being measured by an anti-cheat:
Grim's `Timer` adds 50 ms of balance per tick packet and only remembers
`Timer.drift` (120 ms) of being behind, so at most two owed slots can be repaid
without claiming that the client ran faster than real time. Debt older than
`TICK_DEBT_RESET_SECONDS` (100 ms) is dropped and the grid re-anchors, which is
the honest description: the caller was not ticking.

Everything that sends movement goes through the same helper, and two more rules
live in `_tick_once`:

* **The sprint bit needs a forward impulse.** `MovementInput(forward=0,
  sprint=True)` is a state a vanilla client cannot produce (releasing the key
  stops sprinting), and the server keeps its own sprint flag from
  `player_command`, so the mismatch costs a few millimetres of prediction every
  tick -- which an anti-cheat eventually reports as `Simulation`. The same guard
  clears the bit when the food bar is at or below six, because vanilla refuses
  to sprint there and the server will not grant the speed either.
* **A knockback impulse that lands mid-tick is replayed, not deferred.** The
  tick snapshots `physics_state`, and if `self._motion_epoch` moved while the
  tick was running, the snapshot is restored, the impulse installed and the step
  is recomputed, so the packet that follows carries the impulse. Anti-cheats put
  a velocity packet between two transaction packets and measure the *next*
  movement claim against it.

Consequences worth remembering:

* A one-off action that also needs a movement packet (dig, place, attack,
  hotbar switch) does `wait_tick_budget` → send → swing → `tick`. Doing it in
  the other order leaves the action packet queued until the next tick, which
  anti-cheats report as `Post`.
* Rotation must not be sent as its own `look` packet in the same tick as a
  position packet. Aim with `Actions.turn_to`, which sends the angles inside a
  position+rotation packet.
* A claim that was computed before a teleport landed must never be sent: both
  `_send_position_packet` and the accept path compare `_position_update_serial`
  around the wait for their slot and drop the packet instead.
* Sleeping is the one state where a vanilla client sends **no** movement packet
  at all (the server freezes the body). `bot.sleeping` mirrors that: the tick
  still claims its slot and still sends `client_tick_end`, but no physics and no
  position.

## The tick driver

`bot.tick()` needs a caller. A caller that does `await asyncio.sleep(...)`,
awaits an HTTP request or waits on a lock stops sending movement, and the world
moves on without it -- which is invisible while idling and very visible while
falling or being knocked back.

```python
async with bot.driving():            # 20 Hz, owned by the library
    bot.input = MovementInput(forward=1.0)
    await asyncio.sleep(2.0)         # the bot keeps walking
```

`driver.py` runs `_drive` as a task: it waits for the next grid slot, then calls
`self.tick(self.input)`. It yields the tick to the caller when an action has
claimed the tick (`_action_busy_until`), when the caller ticked recently
(`_last_tick_at` within 90% of a tick), or when this tick already carried a
movement packet -- so a mix of driven and hand-driven ticks never doubles a
packet. Errors do not vanish: they are stored on `bot.driver_error` and emitted
as `driver_error`. `bot.close()` stops the driver before closing the socket.

`wait_ticks(n)` is the small primitive that makes the driver usable from test
code: "let the world run n ticks, then continue".

### One call style: `advance()`

Everything that walks, waits or falls calls `await bot.advance(...)` instead of
`await bot.tick(...)`:

```python
await bot.advance()                                  # let one tick happen
await bot.advance(MovementInput(forward=1.0), ticks=10)   # hold this for ten ticks
```

With a driver running it waits for the pump and holds `controls` for exactly the
ticks it waits for, then puts the previous input back. The restore is what keeps a
slow caller honest: while it plans a path the pump keeps ticking at 20 Hz, and an
input left behind would walk the bot that whole time. Without a driver the same
call is `tick()` repeated, so library users who run their own loop see no change.

The wait is bounded on purpose: if the pump stops claiming slots the call raises
instead of hanging until the caller's own timeout, because an infinite await reads
like a slow server and hides the real bug.

`tick()` is still the right call in one place: the action layer, where a click,
dig or place owns the tick between its own packet and the movement packet of the
same tick. Those windows set `_action_busy_until`, and the driver yields them.

### Steering: `set_steer()`

A walker cannot aim between ticks when the pump sends every slot: nudging
`bot.input` from outside races the pump. Navigation installs a callback instead:

```python
bot.set_steer(lambda: aim_from_here())   # runs immediately before each driven tick
```

The callback runs right before the tick is sent, which is exactly where a vanilla
client turns -- the rotation rides in that tick's movement packet. Navigation sets
one per waypoint and clears it on every exit; `bot.input` is cleared at the same
time so a finished path leaves nothing held down.

## The layers above

```
Bot                 the wire and the local model (this document)
 ├─ actions/        one honest primitive per vanilla action, with confirmation
 ├─ containers/     windows: layouts, clicks, the menu state machine
 ├─ crafting/       explicit recipes and the grid they fill
 ├─ goals/          "is this true yet?" loops over actions
 └─ tasks/          sequences of goals and calls
```

None of these touch a private attribute of `Bot`; they call its public methods
and its public attributes. That is deliberate -- it is what let the split above
happen without touching a single line of `actions/` or `goals/`.

## Expectations: one loop for "is it true yet?"

Every action used to hand-roll its own wait: a `while` loop, a deadline, a
`changed()` closure and a `try/except`. Six of them disagreed about what a
timeout means, and none could say *why* it stopped. `expectation.py` is now the
only such loop:

```python
outcome = await wait_for_expectation(
    bot,
    check,                      # is the thing true?
    valid=lambda: bed_still_there(bot),   # or a string saying why it never can be
    timeout=30.0,
    name="sleep at (12, -60, 15)",
)
outcome.status   # DONE / FAILED / INVALIDATED / TIMEOUT
```

The four statuses are the point:

* **DONE** -- the check finally returned true.
* **INVALIDATED** -- the world changed in a way that makes the wait meaningless
  ("the bed at (12, -60, 15) disappeared while waiting to fall asleep"). This is
  what a polled `changed()` closure could never express, and it is why the suite
  used to hang for the whole timeout when a block was removed underneath it.
* **TIMEOUT** -- the deadline passed with the world unchanged.
* **FAILED** -- the check itself raised (a decode error, a missing registry
  entry), so the wait reports the exception instead of hiding it.

`check` is asked **before** `valid`: a block that broke and was immediately
replaced by another is a success, and letting `valid` answer first would report
that as a failure. `wait_for_expectation` never raises; `Actions.last_outcome`
holds the outcome of the most recent action so a caller can explain a `False`
without guessing.

Actions that own the tick pass `tick=False` and a `before_tick` callback, so the
wait drives their own packet rhythm instead of double-ticking.

## What the world knows

`world.py` carries three kinds of truth, and each has a data file or a packet
behind it:

* **block states** -- id, name, properties and *collision shapes*. Mojang's
  `reports/blocks.json` has no collision boxes, so the generated table used to
  write a full cube for every solid block: a bed was a full block, a slab was a
  full block, and a sleeping player's body was simulated one block lower than
  the bed it lay on. `protobot/data/shapes-26.3.json.gz` fixes that: it is
  dumped from the game itself (`tools/export_shapes.java` in `artifacts/shapes/`
  + `tools/gen_block_shapes.py`), 35,723 states over 393 distinct boxes, and
  `BlockStateRegistry.load_shapes` overlays it on the packaged table. Air keeps
  no shape; a bed keeps four, the tallest 9/16.
* **the clock** -- since 26.3 `ClientboundSetTimePacket` is a *map of world
  clocks*, not a `dayTime` field. `World.apply_clock_updates` keeps the raw
  `ClockState.total_ticks` per clock id and derives `day_time` (`total_ticks %
  24000`), `day_time_advancing` (`rate != 0`) and `game_time`. This is what makes
  "did the night actually get skipped?" answerable: the periodic broadcast has an
  **empty map**, so only a real clock change moves `day_time`.
* **support** -- `World.has_ground_under(position)` asks whether a collision box
  top touches the feet *at that position*, using the same exact shapes the engine
  collides with. It is the honest answer to "am I standing?" after a teleport
  moved the body somewhere the carried ground flag says nothing about.

## Where new code goes

| You are adding | It goes in |
| --- | --- |
| a clientbound packet handler | `play/router.py`, plus the id in `protocol/versions.py` and the audit test data |
| a movement packet or a tick rule | `movement.py` |
| something that must run while the caller is busy | `driver.py` |
| a "keep trying until X" wait, with its failure reasons | `expectation.py`, called from the action |
| a collision shape or a clock | `world.py` (+ `tools/gen_block_shapes.py` for shapes) |
| a route, a jump rule, a flight rule | `navigator.py` (planning stays in `navigation.py`) |
| a tuning constant | `constants.py`, with a test in `tests/test_defaults.py` |
| a player action (right click, dig, place, attack, sleep) | `actions/interactions.py` |
| a window layout or a click mode | `containers/layouts.py`, `containers/menu.py` |
| a "keep trying until X" behaviour | `goals/`, and a task in `tasks/` if it is a sequence |
| anything that needs a live server to prove it | a case in `tools/live_suite.py` |

## Reconnecting

`Bot` is one-shot: its `closed` and `ready` events are never reset, so the
library never tries to revive one. `session.py` owns that policy instead --
`BotSession` resolves credentials, creates a fresh `Bot` per attempt, runs the
20 Hz tick loop, and after a disconnect waits `reconnect_delay` seconds (up to
`reconnect_max_attempts`) before building the next one. Plugins see a new `bot`
attribute after each reconnect and must not cache state that belongs to a
connection.

## Cheat emulation is opt-in

`vclip`, `anti_kick`, `no_fall` and the flying tricks are still available, but
every default is off (`Bot`, `connect`, `SessionConfig`, `FlightPathfinder`,
and the config file the CLI writes). They exist to work around a specific
server, not to make the bot "feel" alive, and turning one on means telling the
server about movement that never happened -- which is exactly what the
anti-cheat proofs in `docs/anti-cheat.md` measure. `tests/test_defaults.py`
pins that promise so a well-meaning default change fails loudly.

## Reading order for a newcomer

1. `client.py` -- what a `Bot` is, and the four things it is made of.
2. `movement.py` -- the tick, and the one-packet-per-tick rule.
3. `play/router.py` -- what a packet does to the model.
4. `connection.py` -- how the socket, login and chat work.
5. `navigator.py` -- how a route is planned and walked.
6. `actions/` then `goals/` -- what a caller actually writes.

`docs/protocol-verification.md` explains how the packet ids and payload layouts
in this code were checked against a real 26.3 jar, which is what makes the
router trustworthy.
