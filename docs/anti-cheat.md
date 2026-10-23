# Passing an anti-cheat

A library that speaks the protocol correctly can still look exactly like a
script, because a script is what almost every protocol bot is.  The test server
runs [GrimAC](https://github.com/GrimAnticheat/Grim) (2.3.74), so the acceptance
suite treats its console as the referee: every scenario asserts that no line
mentioning the bot is an alert.

Four packet-level behaviours had to match vanilla.  All of them are in the core,
so every action and goal inherits them.

## 1. One movement packet per tick, 50 ms apart

`Bot._begin_tick_packet` closes the previous tick (`client_tick_end` on 26.3+)
and waits out `MIN_TICK_GAP` before emitting the next movement packet.  Whether
the packet carries a position, a rotation or only the on-ground flag, it costs
one tick of wall-clock time.

Grim measures this in two places, both in
`common/src/main/java/ac/grim/grimac/checks/impl/timer/`:

- `Timer` adds 50 ms to a balance per movement packet and flags when the balance
  runs ahead of real time, with `setback = 10`.
- `TickTimer` requires one movement packet per `CLIENT_TICK_END` on 26.3 and
  flags a tick that carries two.

Before the fix, aiming right after a position packet put two ticks into one
frame.  The log said:

```
Grim » SuiteBot 触发了 Timer (x31)
Grim » SuiteBot 触发了 TickTimer (x94) type=end, packets=2
```

The setback that follows a `Timer` flag *cancels* movement and combat packets,
which is why a combat scenario reported "the near cow lost no health" while the
attack packets were being sent: the server threw them away.

## 2. Never repeat a rotation

`Bot.send_look` returns without sending anything when the new angles are within
`dYaw^2 + dPitch^2 <= 9.0E-6` of the last sent rotation -- the same test vanilla's
`LocalPlayer.sendPosition` uses to decide between a look packet and a status
packet.  Aiming twice at the same point (a stationary mob, a second dig pass) is
now silent.  Grim's `AimDuplicateLook` flags exactly the duplicate:

```
Grim » SuiteBot 触发了 AimDuplicateLook (x20)
```

## 3. Swing before the movement packet, not after

Grim's `Post` check queues the first non-tick packet of a tick and flags it when
a transaction arrives before the next tick packet.  Sending `punch` after the
tick's position packet therefore flagged:

```
Grim » SuiteBot 触发了 Post (x20) punch v26.3
```

`Actions._settle` now swings *before* `Bot.tick()`, so within a tick the order is
animation, then movement -- and movement closes the tick.

## 4. Swing once per tick while the button is held

`NoSwingBreak` flags a break with no animation in the same tick, and `FastBreak`
uses the animation as its tick marker.  So `dig()` swings before the movement
packet of every held tick and once more immediately before
`STOP_DESTROY_BLOCK`.  See [combat.md](combat.md#arm-swings) for the watcher
proof that other players receive the animation.

## 5. Turn inside a movement packet, and keep the ground flag honest

Two checks in Grim's scaffolding module flag a block placement that is otherwise
perfectly legal.

`RotationPlace` boxes the block the packet *clicks* and ray-traces the rotation
of the tick the click arrived in.  Three things had to change to satisfy it:

- the aim is the centre of the clicked face, the point a real crosshair sits on,
  not the centre of the block that is about to appear -- a ray to the destination
  passes *over* the clicked box and the trace misses;
- the turn travels in the movement packet of the tick it belongs to, never in a
  bare look packet: `turn_to` sets the view and lets the next `tick()` carry it,
  so the aim is part of a tick and no second, empty movement claim is made (a
  look packet while the player is still sliding to a stop claims "I did not move"
  where the server predicts the slide, which is `Simulation`);
- physics runs for one tick before that packet, so it carries current flags.

`GroundSpoof` flags a movement packet that claims "not on ground" while the
player stands on a block.  Two causes, both fixed:

- `place_block` used to send its aim packet with the flag of an older tick
  (`claimed false`, once per connection) -- it now ticks first;
- a server teleport lands the player on a block while the predictor still holds
  the airborne flag, so `_handle_position` now settles `on_ground` from the world
  model: a solid block right under the feet means standing.

```
python tools/live_suite.py --version 26.3 --server-log ..\Test26.3Server\logs\latest.log --run 2,2,2,2,2
[suite] 5/5 passed
[suite] no anti-cheat alerts
```

## 6. Wait five ticks between blocks, and never stop ticking

Two rules about *when* a dig starts, both from the vanilla client rather than from
the packet format.

`MultiPlayerGameMode` sets `destroyDelay = 5` when a block breaks and refuses to
start the next dig until those five ticks pass, so a held button never carries
straight on into the next block.  `FastBreak` counts the gap between a finished
break and the next `START_DESTROY_BLOCK`: anything under 275 ms adds
`300 ms - gap` to a balance that cancels the dig once it passes a second.  Mining
a three block column back to back therefore needs `BREAK_COOLDOWN_SECONDS = 0.3`
between blocks -- `dig()` tracks that itself, so callers can dig in a loop.

The second rule is that a client never stops ticking.  Every pause in the tick
stream -- even one caused by the caller blocking on something else, like an HTTP
call to the bridge -- is a hole in the record, and the next movement claim then
disagrees with the prediction by most of a step.  That is the `Simulation` offset
this suite used to see (`.034980 /gl …`, then a halving tail as the lenience
decays) at the start of a walk that came right after a burst of `/setblock`
calls.  `Suite.keep_ticking()` fills those gaps: it ticks whenever the last
movement packet is more than 75 ms old, exactly like a client that is not doing
anything else.

## 7. Answer pings where the tick allows, and in order

A pong is the transaction an anti-cheat judges the packets queued before it at.
Grim's `GrimProcessor.isTransaction` counts exactly two packets --
`ServerboundPongPacket` and `ServerboundWindowConfirmationPacket` -- and `Post`
clears its per-tick queue only on a movement packet:

```
movement  -> post.clear(); sentFlying = true
pong      -> if (sentFlying && !post.isEmpty()) flag(post.getFirst())
action    -> queued, but only while sentFlying
```

So the safe order inside a tick is animation, movement, and *then* anything else;
a pong that arrives in the moment between an action's swing and that tick's
movement packet makes the swing look like it arrived out of order:

```
Grim » SuiteBot 触发了 Post (x8) punch v26.3
```

`Bot._pong_pending` holds the answer while an action owns the tick
(`_action_busy_until`) and `end_tick()` flushes it once the movement packet has
gone out.  The answers leave in the order the pings arrived: answering a newer
ping first is `TransactionOrder` ("skipped=1"), a check of its own.

## What this document is not about

The client still carries Meteor's cheating features -- `vclip` routes the
planner through solid blocks, `anti_kick` writes a synthetic Y into periodic
movement packets (`constants.ANTI_KICK_WIRE_OFFSET`), and `set_no_fall()`
suppresses fall damage. They are opt-in: `connect()` and `Bot()` both default to
`vclip=False, anti_kick=False`, and `tests/test_defaults.py` keeps them that way.

None of that is "matching vanilla". Every rule in this document is behaviour a
vanilla client produces, which is why the acceptance suite can assert zero
alerts; the cheat switches exist for servers you own, and they are exactly the
kind of packet an anti-cheat is written to catch.

## How to verify

```bash
python tools/live_suite.py --version 26.3 \
    --server-log ..\Test26.3Server\logs\latest.log --run all
```

```
[suite] 23/23 passed
[suite] no anti-cheat alerts
```

Notes for anyone repeating this:

- The bridge's `GET /log` only captures the plugin's own startup lines, so the
  suite reads the real `logs/latest.log` from the offset it had when it started
  (`--server-log`).  Without that flag it falls back to the bridge and sees
  nothing.
- Alerts are one line each because `plugins/GrimAC/config.yml` sets
  `alerts.print-to-console: true`; the message format lives in `messages.yml`
  (`Grim » <player> 触发了 <check> (x<vl>)`).  An English install says
  `failed <check>` instead, which is why the marker list carries both.
- The suite's own chat lines quote alerts, so they are filtered out by looking
  for the chat log level and the `<name>` prefix.

## 7. Knockback has to ride in the movement packet that follows it

An impulse arrives as `ClientboundSetEntityMotionPacket`, and a movement
prediction check sandwiches it between two transactions: the packet before the
impulse and the packet after it.  The claim in between is expected to include
the impulse, with no room left for "I will apply it next tick".

A vanilla client satisfies this for free: packets are handled between ticks, so
an impulse is in `deltaMovement` before the next tick runs.  Our reader task and
tick loop are separate coroutines, so the same impulse can land in the middle of
a tick -- after the physics step, before that tick's movement packet goes out.
That packet then describes a step computed before the impulse existed, and the
difference is reported as `Simulation` (and, on the knockback check itself,
`AntiKB`), often with a setback teleport that throws the bot back where it stood.

Two guards keep the tick self-consistent:

- `_tick_once` waits for its packet slot *before* running the physics.  The wait
  used to sit between the physics step and the send, which is exactly the window
  an impulse needs to slip into.
- `_handle_set_entity_motion` bumps `self._motion_epoch`.  If the epoch moved
  while a tick was in flight, `_tick_once` restores the pre-tick snapshot, keeps
  the freshly arrived velocity and replays the step, so the packet it sends is
  the one that includes the impulse.

`connect(knockback=False)` turns the path off: impulses are then dropped instead
of applied, and a tick that overlaps one is not replayed.  Vanilla clients never
disable it; the switch exists for callers that drive movement themselves (a
pathfinder that must not be pushed off its lane, for example).
