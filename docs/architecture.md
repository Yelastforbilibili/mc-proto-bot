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
| `protobot/movement.py` | 883 | everything the client says about position and rotation, and the 20 Hz tick |
| `protobot/play/router.py` | 2197 | clientbound play-state packets: decode, update the local model, emit an event |
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

`MovementMixin.tick` (movement.py:34) is the only place that advances physics,
and everything that sends movement goes through the same two helpers:

* `wait_tick_budget` (movement.py:267) sleeps until the current tick is at least
  `MIN_TICK_GAP` old, so a burst of one-off actions (a click, a hotbar switch)
  cannot bunch into one tick.
* `_begin_tick_packet` (movement.py:281) closes the previous tick if a movement
  packet was already sent in it, then marks the current one as sent.

Consequences worth remembering:

* A one-off action that also needs a movement packet (dig, place, attack,
  hotbar switch) does `wait_tick_budget` → send → swing → `tick`. Doing it in
  the other order leaves the action packet queued until the next tick, which
  anti-cheats report as `Post`.
* Rotation must not be sent as its own `look` packet in the same tick as a
  position packet. Aim with `Actions.turn_to`, which sends the angles inside a
  position+rotation packet.
* `end_tick` (movement.py:651) is what closes a 26.3 tick (`client_tick_end`);
  any code that sends a movement packet by hand must go through
  `_begin_tick_packet` so the tick still closes exactly once.

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

## Where new code goes

| You are adding | It goes in |
| --- | --- |
| a clientbound packet handler | `play/router.py`, plus the id in `protocol/versions.py` and the audit test data |
| a movement packet or a tick rule | `movement.py` |
| a route, a jump rule, a flight rule | `navigator.py` (planning stays in `navigation.py`) |
| a tuning constant | `constants.py`, with a test in `tests/test_defaults.py` |
| a player action (right click, dig, place, attack) | `actions/interactions.py` |
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
