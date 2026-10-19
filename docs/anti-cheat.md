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
- the turn travels in a movement packet (`position_and_rotation`), never in a
  bare look packet, so the aim is part of a tick;
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
