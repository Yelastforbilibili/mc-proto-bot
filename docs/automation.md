# Automation layers

The library is built in layers, and each layer is useful on its own:

| layer | entry point | unit of work | success means |
| --- | --- | --- | --- |
| protocol | `connect()` / `Bot.tick()` | one packet or one physics tick | the bytes decoded |
| interaction | `bot.actions` | one action (`dig`, `place_block`, …) | the server acknowledged it and the world reflects it |
| goal | `bot.goals` | one objective (`BreakBlockGoal`, …) | `is_reached(bot)` is true against the decoded world |
| task | `bot.tasks` | one script of steps | every step finished with its own goal met |

Nothing above the interaction layer invents server state. A goal may *write*
packets, but it only ever *reads* the world, which is why every layer can be
re-run after a reconnect without keeping bookkeeping in sync.

## The goal contract

A goal is a small object with two methods:

```python
class Goal:
    name = "goal"

    def is_reached(self, bot) -> bool: ...
    async def tick(self, bot) -> GoalStatus:   # RUNNING / DONE / FAILED
        ...
```

`GoalRunner.run(goal, *, timeout=120.0)` drives it:

1. `is_reached()` — if true, finish immediately without ticking (idempotent: a
   goal that is already satisfied costs nothing);
2. `tick()` — one unit of progress; slow work (a walk, a dig) belongs in one
   tick, because the runner checks the deadline between ticks;
3. after a tick that returned `DONE`, `is_reached()` is checked *again*. A goal
   claiming `DONE` while the world disagrees fails the run with
   `goal reported done but is_reached() is false`. That single rule is what keeps
   "sent the packet" from being mistaken for "the server did it";
4. `GoalError` fails the run with the goal's own message, `timeout` fails it with
   whatever the goal last put in `last_detail`, and `runner.interrupt()` stops it
   with `interrupted`.

`GoalReport` carries `goal`, `status`, `elapsed`, `attempts` (ticks) and
`detail`. Events: `goal_start(goal)` and `goal_done(report)`.

### Built-in goals

| goal | reached when | notes |
| --- | --- | --- |
| `BreakBlockGoal(position, *, hold=2.0, reach=4.5, confirm_timeout=1.0, face=None)` | the block decodes as air | `hold` must cover the hand-break time (server breaks only on `STOP`); walks over when out of reach; a chunk that is not loaded yet is "waiting", not a failure |
| `PlaceBlockGoal(position, *, item=None, face=BlockFace.UP, reach=4.5)` | the position is non-air | selects the hotbar item first, clicks the neighbour behind `face` |
| `ReachGoal(x, z, *, y=None, tolerance=1.0, timeout=30.0)` | within `tolerance` of the point | navigation failure keeps it running with the error in `last_detail` |
| `AttackEntityGoal(entity_id, *, reach=3.0)` | the entity left `bot.entities` | swings/attacks with the release-appropriate packet |
| `CraftGoal(recipe, *, times=1, table=None, reach=4.5)` | the server delivered `recipe.count * times` items in this run | 2x2 recipes craft in the inventory; bigger ones need `table=(x, y, z)` and the goal walks over and opens it; missing materials fail with the missing list |
| `WaitGoal(seconds)` | the deadline passed | `reset()` re-arms it |

Out-of-reach targets are approached inside the goal, so a caller never has to
pair a walk with an action by hand.

### Composition

`SequenceGoal(*goals)` ticks the first unreached member and is reached when all
are; `AnyOfGoal(*goals)` is reached when any is and fails only when all failed.
A failing member fails the composition and prefixes the detail with its name.

## The task layer

`Task` is an ordered list of steps with names, and `TaskRunner.run(task, *,
timeout=300.0)` executes them:

- `GoalStep(goal, *, timeout=120.0)` — one goal as one step;
- `CallStep(name, fn)` — a sync or async function; returning `False` fails with
  `returned False`, returning a non-empty string fails with that string as the
  detail, raising fails with `Type: message` (`TaskError` passes through);
- `WaitStep(seconds)` — sleep, useful for spawn/settle timing;
- `BreakBlocks(positions, *, hold=2.0, timeout_each=60.0)` and
  `PlaceBlocks([(position, item), …], *, timeout_each=60.0)` — ready-made steps
  that report `broke 2/2` / `placed 1/1` and name the block that failed.

A failed step stops the task; the report keeps the steps that ran, their
`attempts`, and a `failed_step` accessor. Retries re-run *that step* (`retries=1`
by default, `retry_delay=0.3` between attempts). A task-level timeout that
expires between steps appends a failed `task timeout` step instead of silently
truncating. Events: `task_start(task)`, `task_step(report, ctx)`,
`task_done(report)`; `ctx.data` is a dict shared by all steps of one run.

## Adding a goal

```python
from protobot.actions import ActionStatus
from protobot.goals import Goal, GoalError, GoalStatus


class HarvestCropGoal(Goal):
    name = "harvest"

    def __init__(self, position):
        super().__init__()
        self.position = tuple(int(v) for v in position)

    def is_reached(self, bot):
        return self._world_state(bot, self.position) == 0

    async def tick(self, bot):
        if self._world_state(bot, self.position) == -1:
            self.last_detail = "waiting for the chunk to load"
            return GoalStatus.RUNNING
        result = await bot.actions.dig(self.position, hold=0.6)
        if result.status is ActionStatus.REJECTED:
            raise GoalError(f"server refused {self.position}: {result.evidence}")
        self.last_detail = result.evidence
        return GoalStatus.RUNNING
```

Rules of thumb: read the world in `is_reached`, keep one unit of work per tick,
put the human-readable reason in `last_detail`, and never assume a packet
landed — the runner will check the world either way.

## Proving a layer against a real server

```bash
uv run python tools/live_dig.py   --target 8,-60,4                      # interaction layer
uv run python tools/live_goal.py  --target 8,-60,4                      # goal layer (walk + dig)
uv run python tools/live_place.py --target 2,-60,0 --give --reset       # placement + inventory
```

`live_goal.py` prints the `GoalReport` and the walls it passed over. One run
against Paper 26.3 (protocol 777), player spawn `(9.5, -60, 6.5)`, dirt at
`(8, -60, 4)`:

```json
{
  "sequence": "reach (10.5, None, 4.5) -> break (8, -60, 4)",
  "status": "done",
  "ticks": 2,
  "elapsed": 2.703,
  "state_before": 10,
  "state_after": 0,
  "end": { "x": 10.233, "y": -60.0, "z": 5.034 }
}
```

The server's own event log for the same session is the part a fake cannot
produce:

```
seq=32 join      GoalProbe x=9.5  y=-60 z=6.5
seq=33 interact  LEFT_CLICK_BLOCK block=[8,-60,4] face=UP
seq=34 break     minecraft:dirt   block=[8,-60,4] cancelled=false
```

The player started 2.7 blocks away, so the block was not broken by
spawn-adjacent luck: the goal walked, then dug.

Placement has one extra prerequisite that mining does not: the server needs to
know *which* block you are holding, and item ids drift between releases (dirt is
28 in 1.21.8 and 55 in 26.3).  `tools/live_place.py --give --reset` proves the
whole chain — server gives the item, the client decodes the inventory packet,
the goal picks the hotbar slot, the placement is accepted, and the server reads
the new block back:

```json
{ "held": "minecraft:dirt", "hotbar_slot": 36, "state_before": 0,
  "status": "confirmed", "state_after": 10,
  "server_block": { "type": "minecraft:dirt", "stateId": 10 } }
```

Both halves matter: `state_after` is the client's own decoded world, and
`server_block` is read back from the server through the prototest bridge.  See
[protocol-verification.md](protocol-verification.md) for why the item table is
generated data rather than a constant.
