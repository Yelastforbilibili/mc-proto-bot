# Using protobot: the current shape of the library

This is the practical reference for the library as it stands today: what the
pieces are, which one you want, and how to put them together.  Every API named
here exists and is covered by the test suite; the deeper "why" for each
anti-cheat rule lives in [anti-cheat.md](anti-cheat.md).

## 1. Connect and drive the clock

```python
from protobot import connect
from protobot.physics import MovementInput

bot = await connect("127.0.0.1", port=25565, username="Demo", version="26.3")
await bot.wait_ready()            # player.loaded and the chunk under the feet
```

`connect()` is a coroutine (it performs the whole login handshake).  Version
`"26.3"` is the release this library is verified end to end; other releases
have generated packet and block tables only.

The client sends movement on a 20 Hz grid.  Two ways to run it:

```python
async with bot.driving():          # the library owns the clock
    bot.input = MovementInput(forward=1.0)
    await asyncio.sleep(2.0)       # the bot really walks for these 2 seconds
    await bot.wait_ticks(10)

await bot.advance()                                           # one tick
await bot.advance(MovementInput(forward=1.0), ticks=10)       # ten ticks
await bot.advance()                                           # no driver: same as tick()
```

- **`bot.driving()`** starts a background task that ticks at 20 Hz and yields
  while an action owns the tick or while the caller has just ticked itself.
  This is the recommended way to write anything non-trivial: whatever you
  `await` in the body (HTTP calls, pathfinding, JSON parsing) does not stop the
  bot from streaming movement packets.
- **`bot.advance(controls=None, *, ticks=1)`** is the one call style for "let
  time pass".  With a driver it waits for the pump and holds `controls` only
  for those ticks; without one it calls `tick()` in a loop.  The wait is
  bounded: a pump that stops claiming slots raises instead of hanging.
- **`bot.tick(controls)`** is still there; it belongs to the action layer and
  to code that deliberately owns a single tick.
- `bot.driver_error` holds the exception that stopped the pump, and the
  `driver_error` event fires with it.  Nothing is swallowed.

Steering: navigation needs to re-aim inside the tick that sends the packet.

```python
def steer() -> None:
    bot.physics_state.yaw = 90.0
    bot.input = MovementInput(forward=1.0, sprint=True)

bot.set_steer(steer)      # runs before every driven tick's packet
bot.clear_steer()
```

`navigate_to()` installs its own steer callback per waypoint and clears it on
every exit path, so you normally do not call `set_steer()` yourself.

## 2. Actions

```python
result = await bot.actions.dig((10, -60, 5))                 # ActionResult
result = await bot.actions.place_block((10, -60, 4))         # needs the item in hand
result = await bot.actions.use_item_on((10, -60, 5), face="up")
result = await bot.actions.attack_looked_at()                # hits what the crosshair is on
result = await bot.actions.sleep_in_bed()                    # walks? no: bed must be within reach
await bot.actions.swing()
```

Every action returns an `ActionResult` with `status` = `SENT`, `CONFIRMED`,
`UNCONFIRMED` or `REJECTED`, plus `evidence` (the reason the world agrees or
not) and `extra`.  `SENT` means only "the packet left"; `CONFIRMED` means the
world changed the way the action intended.

Aim is handled for you: `dig`/`place_block`/`use_item_on` pick a face that is
actually visible from the eye and turn with a `position_and_rotation` packet
(a separate look packet would let a click appear out of nowhere).  `dig`
refuses a face you cannot see instead of firing a packet the server drops.

`bot.actions.frame()` returns the `PlacementFrame` the next click will be judged
in — position, eye height, yaw, pitch, and the eye point computed from them.  The
server measures a click against the pose carried by the movement packet of the
same tick, so anything that casts a ray first and clicks second should read the
frame once and keep using it:

```python
frame = bot.actions.frame()
hit = bot.actions.raycast()            # uses that same frame
print(frame.describe())                # {"x": …, "eye_y": …, "yaw": …}
```

### Retry policy (knockback-aware)

```python
from protobot.actions import ActionPolicy, DEFAULT_POLICY, NEVER_RETRY

bot.actions.policy = ActionPolicy(retries=2, settle=0.10, max_seconds=2.0)
```

Aimed actions (`dig`, `place_block`, `attack_looked_at`) accept `policy=` and
re-aim **only when the motion epoch changed**, i.e. the bot really was
knocked back between aiming and finishing; a clean failure or a server
refusal is never retried.  After the call, `bot.actions.attempts` and
`bot.actions.retried_after_knockback` (also `result.extra`) tell you what
happened.

## 3. Waiting for the world (expectations)

```python
from protobot.expectation import wait_for_expectation

outcome = await wait_for_expectation(
    bot,
    lambda: bot.world.get_state_id(10, -60, 5) == 0,
    valid=lambda: None if (10, -60, 5) in self.expected_air else "blocked",
    timeout=3.0,
    name="dig (10, -60, 5)",
)
outcome.status    # DONE / FAILED / INVALIDATED / TIMEOUT
outcome.describe()  # "done in 0.45s" / "invalidated after 0.30s (…)"
```

Four outcomes, never an exception.  `check` is asked first, then `valid`: a
thing that became true and was then overwritten counts as done, not as a
failure.  `before_tick` lets an action keep its own packet rhythm
(`Actions._settle` uses this), `tick=False` + `poll=` is for cases that must
not send packets while waiting (menus).

## 4. Containers

```python
menu = await bot.menu.open_block((10, -60, 5))       # chest, furnace, anvil, …
menu = await bot.menu.open_entity(entity_id)         # villager, horse, …

menu.items(), menu.item(slot), menu.data_value("cook_progress")
await menu.click(slot, mode="pickup")                # or "quick_move", "swap", …
await menu.move(8, 36), await menu.deposit(range(9)), await menu.withdraw(range(9))

menu.invalid_reason()      # why the window cannot be used any more, or None
menu.live()                # the same thing, raising MenuNotOpen
menu.last_outcome          # how the last wait ended (INVALIDATED carries the reason)

await menu.enchant(item_slot, option=0)   # and anvil/grindstone/smithing/stonecutter
```

`invalid_reason()` is the whole point: window closed by the server, player
dead, the block that opened it replaced, or out of reach — each with its own
sentence.  `Menu.wait_for_update` and `wait_until_quiet` now report the same
four-state outcome instead of returning a bare `False`.

Crafting: `bot.menu.craft`, `bot.crafting.recipe_for`, or `CraftGoal(recipe, times=…)`.

## 5. Goals, tasks, pickup

```python
from protobot.goals import BreakBlockGoal, TravelGoal, SleepGoal, SequenceGoal
from protobot.tasks import Task, BreakBlocks, PlaceBlocks, GoalStep, CallStep

await bot.goals.run(TravelGoal(30.5, 12.5, tolerance=1.0))     # walks, waits for chunks
await bot.goals.run(SequenceGoal(BreakBlockGoal((10, -59, 5)), SleepGoal()))

task = Task("dig-walk-place")
task.add_step(BreakBlocks([...]))
task.add_step(GoalStep(TravelGoal(...)))
task.add_step(PlaceBlocks([...]))
report = await bot.tasks.run(task)                             # per-step status

report = await bot.pick_up(["minecraft:oak_log"], count=3, radius=8.0,
                           allow_loss=0, shape="sphere")
```

`TravelGoal` splits long routes at the decoded-chunk edge, so a 200 block trip
keeps working; `StuckWatchdog` is available as a goal wrapper.

## 6. What the world knows

- Block states, names and properties from the generated tables
  (`protobot/data/blocks-<release>.json.gz`).
- **Real collision shapes** (`shapes-26.3.json.gz`): bed 9/16, slabs 1/2,
  fences, glass panes.  `bot.world.block_collision_boxes(x, y, z)` and
  `bot.world.has_ground_under(position)` use them, which is why a bot can lie
  on a bed without claiming to be inside a full block.
- Clocks: `bot.world.game_time`, `day_time`, `day_time_advancing` — 26.3 sends
  world-clock updates, not the old `dayTime`, and the periodic broadcast is an
  empty map (it does not mean "midnight").
- Entities, positions, swings, damage, time, health, experience — all decoded
  and emitted as events (`bot.events.on("entity_motion", …)`, `"time"`,
  `"swing_animation"`, …).
- **A tick as a value.**  `bot.physics.step(state, controls, world)` runs one
  tick without touching the state it was given and returns a `StepResult`
  (next state + whether it collided).  `bot.physics.tick(...)` still writes in
  place — that is the cheap path for the live bot — but anything that asks
  "what would happen if" (a jump edge, a probe, a test) uses `step`.
  `protobot.physics.Recorder` keeps a transcript of those steps and
  `recorder.replay(engine, world)` re-runs it and reports the first frame that
  disagrees, comparing both position and velocity:

  ```python
  from protobot.physics import PhysicsEngine, Recorder

  recorder = Recorder()
  state = bot.physics_state
  for controls in [MovementInput(forward=1.0)] * 20:
      state = recorder.record(bot.physics, state, controls, bot.world).state

  print(recorder.replay(PhysicsEngine(), bot.world))   # [] means exact
  ```

## 7. The anti-cheat rules the library follows

These are not heuristics; each one is a property of the vanilla client, and
each was found by measuring a real client (see [anti-cheat.md](anti-cheat.md)):

1. **One movement packet per tick, on a 20 Hz grid.**  Catch-up is capped at two
   ticks because the server only remembers 120 ms of lag.
2. **Rotations ride inside the movement packet**; duplicates are not resent.
3. **The sprint bit needs a forward impulse and enough food** (> 6).
4. **A knockback rides in the claim that follows it** — the tick is replayed if
   the impulse arrives mid-tick.
5. **Actions own their tick**; the driver steps aside rather than interleaving.
6. **Sleeping says nothing**: while the server holds the body, no movement
   packets are sent, one `client_tick_end` per tick, and `leave_bed()` hands
   control back.
7. **Pongs are flushed before anything that waits on a transaction** (teleport
   confirmations).
8. **Cardinal headings aim with exact zeros.**  Vanilla's sine table samples
   `pi` as `1.2246e-16`, not 0, so a walk straight along an axis gains a sideways
   component every tick; `heading_sin_cos()` snaps quarter turns to exact zeros
   the way the reference implementation does.

## 8. Navigation thresholds

These are the numbers a walk finishes on, taken from a working reference
implementation (MovementSync 1.12 `PathMovement`) rather than tuned by feel:

| What | Value | Why |
| --- | --- | --- |
| Last waypoint radius | **0.3** blocks | the goal is judged tighter than the route |
| Intermediate waypoint | **0.35** blocks | a corner does not need a stop |
| Vertical arrival | **0.5** blocks | one step of slop |
| Settled before finishing | **0.025** blocks/tick, max 8 ticks | a walker that drops its input on the boundary still slides |
| Stuck | **< 0.05 blocks** of displacement for **40 ticks** | one fast tick is not progress |

`navigate_to()` also keeps a `sprint=True` default and reports
`NavigationTimeout` with the remaining distance, so a caller can tell "walked
most of the way" from "did not move".

## 9. What it does not do (yet)

- Only 26.3 is verified end to end; other releases have tables and tests, not
  live proof.
- Sleeping still leaves two small alerts (`Simulation 0.022556`, one
  `GroundSpoof claimed true`) at the moment the body is released.
- Resource packs, scoreboard/boss bars/titles, riding and vehicle control,
  sign editing are not implemented.
- Recipe knowledge is a hand-written table (a dozen recipes), not the full
  recipe registry.

## 10. The honest scope of "zero alerts"

The acceptance suite reports `no anti-cheat alerts` **when nothing interferes
with the bot**.  When a player punches the bot in the middle of an action or a
walk, the bot can still be flagged (`AntiKB`, `GroundSpoof`, `Simulation`,
occasionally `Post`): the impulse arrives while the action owns the tick, and
the claim that follows does not always carry it.  Do not read the suite's green
line as "unaffected by players fighting it".  Concretely measured:

| Situation | Alerts |
| --- | --- |
| Full suite, nobody interfering | none |
| Full suite while a player keeps punching the bot | ~1 per mixed case (#23, #25, #30 in the last run) |

## 11. Where to start reading code

| You want to change… | Go to |
| --- | --- |
| how a packet is sent or how the tick grid works | `protobot/movement.py` |
| the background pump | `protobot/driver.py` |
| "wait until X is true" | `protobot/expectation.py` |
| a new action (dig/place/…/sleep) | `protobot/actions/interactions.py` |
| retry behaviour after a knockback | `protobot/actions/policy.py` |
| window clicks and machine menus | `protobot/containers/menu.py` |
| how incoming packets update the model | `protobot/play/router.py` |
| pathfinding | `protobot/navigator.py` |
| collision shapes and clocks | `protobot/world.py` |

## Predicting where an entity will be

`bot.prediction` is dead reckoning for other entities, and the ballistics that
turn it into an aim point:

```python
aim = bot.prediction.position_at(cow_id, 2)          # feet in two ticks
centre = bot.prediction.center_at(cow_id, 2)         # middle of its box
shot = bot.prediction.intercept(cow_id)              # solved arrow launch
if shot is not None:
    await bot.actions.turn_to(shot.point)            # or send shot.yaw/pitch
    await bot.actions.use_item()                     # fire
```

The motion model is vanilla's integrator: move by the velocity, subtract
gravity, then the horizontal friction of the block underfoot and the vertical
drag. `MotionModel(hold_horizontal=False)` (exported as `PHYSICS_MOTION`)
reproduces a real client's knockback arc exactly -- 0.4, 0.2184, 0.1987
horizontally and 0.3608, 0.2752, 0.1913 vertically, measured on Paper 26.3 --
which is how the model is checked. The default holds the horizontal velocity
instead, because a mob that is walking keeps walking; that is the better guess
for aiming but it is still a guess.

What it cannot know, and does not pretend to:

* **The entity's own input** -- a mob's pathfinding, a player's keys, a jump.
  A target that starts turning only turns after the next update arrives.
* **Sparse updates** -- most entity movement arrives as 2 Hz deltas for
  interpolation, and a knockback or setback that has not arrived cannot be
  predicted at all.
* **Collisions** -- predicted positions ignore walls: a mob walking into one
  keeps going in the model until the server corrects it.
* **Vehicles** -- only the root entity is extrapolated, passengers move with
  their vehicle.
* **Latency and charge** -- what we hold is already `ping / 2` old, and arrow
  speed depends on how far the bow was drawn; the caller passes the speed.

So the lookahead is capped at `PREDICTION_HORIZON_TICKS` (20), every answer
carries a `confidence` that decays with the lookahead and with how long ago the
entity was last seen, and `intercept()` returns `None` rather than a fantasy
when the target is out of range. For a bow this is the difference between
"aim at where it is" (always misses a moving target) and "aim at where it will
be, and know how sure that is".

### Keep the crosshair on the target: `lock`

`attack_looked_at(lock=True)` aims at the entity, swings at *that* entity, and
then keeps tracking it for `lock_ticks` ticks:

```python
result = await bot.actions.attack_looked_at(lock=True, lock_ticks=2)
if result is not None and not result.extra["locked"]:
    ...                      # it left the 3 block reach, the swing still went out
```

The rule is the one a player follows without thinking: while the target is
within reach, look at it; stop when it is gone or out of reach, or when the
call ends. Every tick re-aims at the box centre the client can see right now --
no leading, no prediction. Because `turn_to()` rides the rotation inside the
movement packet, the aim and the step reach the server as one unit, and aiming
at the same angle twice sends nothing.

`lock_on(entity_id, ticks=...)` is the same loop on its own, and returns
`False` when the target vanished or is beyond `max_distance`.

This is what fixes the "the aim was right a tick ago" class of misses: a
walking cow is re-aimed at every tick of the swing, instead of being clicked
with a rotation measured when the crosshair was first put on it.
