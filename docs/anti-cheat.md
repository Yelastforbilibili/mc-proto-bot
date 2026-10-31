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
calls.  The acceptance suite now runs every bot on `bot.driving()` (one 20 Hz
pump from `start_driver()` to `stop_driver()`), so those holes cannot open in
the first place; `Suite.keep_ticking()` is only a compatibility shim for older
scripts that drove the bot themselves.

## 7. Answer a ping at once, except between an action and its own tick

A pong is not just an echo.  It is the transaction an anti-cheat judges the
packets queued before it at, and it is also how the server learns which part of
the packet stream the client has certainly seen:

```
server: transaction A (a ping)
server: set_entity_motion          <- the knockback
server: transaction B (another ping)
```

Grim records the knockback against transaction A, and it only treats it as
"known to be in the present" once the client's answer to B has arrived
(`likelyKB`).  From then on the next movement claim has to carry the whole
impulse; the first claim that does not is `AntiKB`, and the offset is the
impulse itself:

```
Grim » KbLook 触发了 AntiKB (x1) o: .320240        # .538672 - .2184
Grim » KbLook 触发了 Simulation (x2) .320240 /gl 11
Grim » KbLook 触发了 GroundSpoof (x1) claimed false
```

So a pong goes out the moment the ping arrives.  Holding it back -- which this
client used to do for the whole `_action_busy_until` window -- makes the claim
that follows look like it ignored a knockback that had already been delivered:
`Simulation .538253` against the full impulse, plus `GroundSpoof claimed false`
when a later claim claims to be off the ground.  Both numbers were measured on
Paper 26.3 + GrimAC v2.3.74 with the two-bot punching scene in
`artifacts/kb_look_window.py`.

The one exception is `Post`:

```
movement  -> post.clear(); sentFlying = true
pong      -> if (sentFlying && !post.isEmpty()) flag(post.getFirst())
action    -> queued, but only while sentFlying
```

An action packet must be followed by its own tick's movement packet with no
transaction in between, and an action closes its tick and then waits for the
next tick slot before sending.  `Actions._send` therefore arms
`Bot._pong_hold_until` for one action tick (80 ms), and
`Bot._release_pong_hold()` -- called right after the movement packet has been
written -- answers whatever arrived in that gap.  The answers leave in the order
the pings arrived: answering a newer ping first is `TransactionOrder`
("skipped=1"), a check of its own.

Measured on that scene, 30 s per run:

| answer policy | alerts |
| --- | --- |
| held for the whole action window (old) | `AntiKB .320240` + `Simulation .538253` + `GroundSpoof claimed false` |
| immediate everywhere | 2 x `Post (x2) attack v26.3` |
| immediate, held for one action tick | none of the above (`Timer` was present in every run, including the old policy) |

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

## 8. The sprint bit needs a forward impulse

The input packet carries one bit per action, sprint included, and the server
keeps its own sprint flag from the START/STOP SPRINTING commands.  A movement
prediction combines both: if the input says "no forward" while the flag still
says "sprinting", the two sides disagree by a few millimetres on every such
tick, which Grim reports as `Simulation` (0.005 to 0.01, decaying with air drag
because the error rides in the velocity).

A vanilla client cannot produce that state -- releasing forward stops the sprint
in the same tick, and the client sends STOP_SPRINTING -- so `_tick_once` clears
`controls.sprint` whenever there is no forward impulse.  This matters for
`navigate_to`, which pauses with `forward=0` whenever it is inside a waypoint's
tolerance: sprint + knockback + a pause in flight used to produce a decay series
of `Simulation` offsets, and stopping the sprint at the pause removes them.

## 9. Catch up two ticks at most, because the clock is measured

`tick` keeps an absolute 50 ms grid in `_next_tick_at` and, when a caller comes
back late, runs the slots that have already passed instead of stretching the
next one.  That is what keeps a knockback arc ten ticks long when the loop that
drives the bot hiccups: the impulse is resolved by the physics of the tick it
belongs to, not by whenever the caller got around to it.

The depth of that catch-up is not free.  Vanilla drains up to ten ticks per frame
(`Minecraft.MAX_TICKS_PER_UPDATE`) because a real frame can take 500 ms, but the
anti-cheat measures those bursts against the wall clock: Grim's `Timer` adds
50 ms per movement packet (`timerBalanceRealTime += 50e6`), flags the moment the
balance goes positive, and only remembers how far behind a client was up to
`Timer.drift` (default 120 ms, `limitFallBehind`).  A client that was away for
200 ms therefore cannot spend more than those 120 ms of remembered debt; sending
the full four slots would put it half a tick ahead of real time and be flagged.

`MAX_CATCH_UP_TICKS` is two (100 ms, inside the allowance), and
`TICK_DEBT_RESET_SECONDS` is its width: debt older than that is not jitter but a
caller that stopped ticking, so the grid re-anchors to now and drops it.  With
ten ticks that is the difference between a burst of ten packets (four `Timer`
alerts in the acceptance suite) and one honest tick.

The consequence is worth stating plainly: a caller that awaits something for
longer than two ticks still stretches the world by the difference.  Only a
driver that keeps ticking while the caller awaits -- a real client's frame loop
-- removes that; the grid makes the debt visible and bounded instead of silent.

## 10. Sleeping: say nothing while the server holds the body

Clicking a bed is the one interaction where the server moves the player *in
answer to* the click: `ServerPlayer.startSleepInBed` calls
`connection.teleport(...)` and puts the body at the sleeping pose, 0.6875 blocks
above the floor and up to three blocks from where the player stood.  Every claim
we send between the click and that teleport is judged against a body the server
has already moved -- measured at `Simulation 3.077768`, which is exactly
`sqrt(0.6875² + 3²)`, followed by the anti-cheat setting us back with the same
teleport 234 times.

So `sleep_in_bed` does three things in this order:

1. `use_item_on(..., tick=False)`: the click goes out, and no movement packet
   rides behind it;
2. `end_tick()` right after the click: an anti-cheat queues action packets that
   arrive after a tick's movement marker and reports the queue when a ping lands
   before the next movement packet clears it (`Post`), and the tick end clears
   that queue without claiming a position;
3. `bot.sleeping = True` for the wait: `_tick_once` then simulates nothing and
   sends no position at all, only the tick-end marker every tick that a vanilla
   client also sends while asleep.

Getting up, or a bed that is broken under us, clears the flag and sends
`STOP_SLEEPING` (action code 0) so the server hands control back instead of
keeping a frozen avatar.

The honest limitation: a bed is a 9/16 block box and the sleeping pose sits on
top of it, so when the night is skipped the server's wake-up teleport lands the
standing player a fraction above the bed.  We resolve that with the real shape
table (`data/shapes-26.3.json.gz`) and `World.has_ground_under`, which fixed the
three-block offset but not the last 0.0225: a sleeping round trip still leaves one
`Simulation` and one `GroundSpoof (claimed true)` per sleep in the acceptance
probe.  Both are single alerts with no setback, and both are recorded here rather
than rounded away.


