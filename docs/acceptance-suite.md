# The live acceptance suite

`tools/live_suite.py` is an in-game checklist. It connects a bot to a real
server, listens to chat, and runs one scenario when a player types a number.
Every result goes back into chat, so the whole library can be checked from
inside the game without touching a terminal.

It is a verification tool, not part of the library: it builds its own scenes
with server commands, then asserts on what the **server** reports (`data get`,
the bridge's block/event endpoints), not on what the bot thinks.

## Running it

```bash
# A Paper server plus the bridge from docs/protocol-verification.md
python tools/live_suite.py --version 26.3

# Or run scenarios directly, without chat:
python tools/live_suite.py --run 1,5,15
python tools/live_suite.py --run all
```

Options: `--host --port --username --bridge --world --x --y --z` and `--list`
(print the numbered scenarios without connecting).

With `--run`, the last thing printed is a table with one row per scenario and
the wall time it took, then the totals:

```
[suite] results
  ✓ # 1    1.4s  dig/place·one block dug
  ✓ # 2    1.1s  dig/place·one block placed
  ...
  ✗ #13   19.1s  navigation·far travel  (AssertionError: …)
[suite] 22/23 passed in 231.2s
[suite] no anti-cheat alerts
```

`tools/ci_local.ps1 -Live` runs the same thing as the last step of local CI, so
the scenario table ends up next to the unit-test result instead of in a separate
terminal.

Then, in game chat as **any** player:

| Say | What happens |
| --- | --- |
| `0` | Print the menu: one line per category with its number range |
| `1` .. `23` | Run that scenario and report pass/fail in chat |
| `all` | Run every scenario in order |
| `stop` | Stop the scenario that is running |
| `?7` | Describe scenario 7 without running it |

Chat updates are throttled to about one per second: the vanilla server adds 20
to a spam counter per message and removes one per tick, kicking above 200.

## The scenarios

| # | Category | Scenario | What it proves |
| --- | --- | --- | --- |
| 1 | dig/place | one block dug | start/stop dig, a swing behind it, block update, drop picked up |
| 2 | dig/place | one block placed | `use_item_on`, placement confirmed by the server |
| 3 | dig/place | swing and empty use | arm animation plus a right click into the air |
| 4 | dig/place | ray hits the block | block raycast against the loaded world |
| 5 | containers | chest in and out | clicks, `deposit`/`withdraw`, server NBT |
| 6 | containers | ten menu layouts | every openable container maps to real slots |
| 7 | containers | furnace smelting | fuel/input moves, `container_set_data` progress |
| 8 | containers | villager trade | entity menu, result slot, server `Offers` |
| 9 | crafting | planks in the 2x2 grid | `fill_grid` + `craft_once` |
| 10 | crafting | sticks at a table | 3x3 grid, `CraftGoal` with a table |
| 11 | crafting | end to end chain | logs to planks to a placed table to sticks |
| 12 | navigation | short walk | `navigate_to` lands on the target |
| 13 | navigation | long walk | `TravelGoal` across chunk borders |
| 14 | navigation | there and back | two trips, no drift |
| 15 | combat | kill a cow | a live cow (knockback and all) chased down, server health to zero |
| 16 | combat | nearest target | the ray picks the nearer mob, the hit lands on it, the far one keeps its health |
| 17 | tools | hardness numbers | the bundled table matches vanilla times |
| 18 | tools | tool choice and speed | `equip_tool` and a measured break delay |
| 19 | components | known components | a window with data components decodes fully |
| 20 | components | unknown components | the window is refused, one stack still decodes |
| 21 | mixed | lumberjack | walk, dig, craft, place, craft again |
| 22 | mixed | warehouse | store, withdraw, craft, store the result |
| 23 | mixed | expedition | walk 30 blocks, kill, walk home |
| 24 | machines | stonecutter, grindstone, anvil, smithing | button-driven menus and their server-side results |
| 25 | machines | a window that stops being valid | distance and a broken block both raise `MenuNotOpen` |
| 26 | spectator | watching a registered bot | `/see` mirrors slots in both patterns, `/see stop` restores |
| 27 | machines | the enchanting table | sword plus lapis, button click, an enchanted sword in the server's inventory |
| 28 | items | buckets | water and lava poured on a solid top face, the server's block changes |
| 29 | items | boats | an oak boat placed on water, a boat entity on the server |
| 30 | items | minecarts | a minecart on a rail, a minecart entity on the server |
| 31 | items | sleeping | a bed entered at night, the server rolls the clock over |
| 32 | items | breeding | wheat to two cows, a third cow on the server |
| 33 | tasks | a task of steps | one Task runs dig → walk → place → a server check, and the report names every step |

Cases 24 to 27 are the ones that need a second party (the server's own menu logic, or another
player watching), so they are the slowest proofs in the suite and the ones most worth reading when
something about menus or spectators changes.

Cases 28 to 32 are the *use* half of the item system: each one asserts an effect the server owns
(a block that changed, an entity that exists, a clock that moved, a mob that appeared) rather than
something the client can claim. They also pin down where vanilla puts the work: a bucket has only
`use` (the client sends the block click and then the use *in the same tick*), a bed only needs an
empty hand at night, and breeding needs mobs that kept their AI.

## How a scenario is isolated

`Suite.reset()` runs before every scenario: close the window, clear the
inventory, put the craft grid and the cursor back, kill tagged mobs **and
dropped items**, wipe the plot, and teleport back to the anchor.

Four of those are worth spelling out, because each one cost a debugging session:

* **Every non-player entity within 16 blocks is killed**, not just the tagged
  ones. A cow from the combat scenario wandered into the next scenario's
  crosshair; a villager left a window open; a boat sat where the next structure
  went. `execute positioned <anchor> run kill @e[type=!minecraft:player,distance=..16]`.
* **The floor is put back** (`fill … grass_block` one layer below the anchor)
  and everything above it is wiped, so a probe script or a failed run cannot
  leave a stray block that changes what the next scenario is standing on.
* **Time and weather are reset** (`time set day`, `weather clear`): a scenario
  that waits for dusk and dies early would otherwise leave the next one in the
  dark.
* **The hotbar selection goes back to slot 0**, so "hold the pickaxe" starts from
  a known slot rather than whatever the last scenario left behind.

Dropped items matter most: leftovers from an earlier scenario fly into the
inventory as soon as the bot walks over them, which turns "dug three logs" into
six.

Each scenario builds its own scene with commands (`setblock`, `summon`, `give`,
`item replace`), then checks the server:

```python
s.give("minecraft:oak_log", 10)
await s.wait_count("minecraft:oak_log", 10)   # /give is async: wait for the slots
menu = await s.bot.menu.open_block(chest)
stored = await menu.deposit("minecraft:oak_log", 10)
left = server_items(s.command("data get block ... Items"))
assert left.get("minecraft:oak_log") == 10
```

## Death and respawn

A dead bot lies on the death screen: no physics, no clicks, and every scenario
after it fails for a reason that has nothing to do with what it tests. The
server never respawns a player by itself, so the suite treats the core's `death`
event as "get up now": it sends Client Status (0 = perform respawn) and waits
for **both** the respawn packet and the position sync that follows, because the
respawn clears `player.loaded` and the next click needs a loaded position.

The handler only *starts* a task. It is dispatched from inside the client's
packet loop, so awaiting the whole respawn there would stop the client from
reading the very packets it is waiting for — that deadlock is what made the
first version sit for ten seconds and then recover at the deadline. The request
is also resent every couple of seconds: the server ignores a respawn for a player
it still considers alive, which happens when the join-time health packet has not
been processed yet. `Suite.revive()` is called again by `reset()`, so a scenario
always begins on its feet.

```python
bot.events.on("death", suite.on_death)   # wires the same handler as live_suite.py
```

## Things that bit us

- **A command is not a fact.** `/give` returns as soon as it is dispatched;
  clicking before its slot update arrives works off an inventory the server has
  already moved on from. Wait for the model instead.
- **A closed window is not gone.** The server does not answer our own close
  packet, so the client has to clear the active window itself; otherwise the
  next click names a window the server has already closed and is ignored.
- **Drops need to be collected.** Items only fly into the inventory within about
  a block, so a scenario that digs from one block away has to step onto the drops
  before counting them.
- **Two bots, one server.** The suite identifies its own chat echoes by player
  name; offline servers echo chat without a sender uuid.
- **A runner without an active window is dead on arrival.** A scenario that opens
  a menu leaves it open when it fails, and the next scenario's clicks then go to
  a window the server has already replaced; `reset()` closes whatever is still
  open before it starts.
- **A pause is a lie about the movement.** The bot only ticks when the test code
  asks it to, so every blocking call (bridge HTTP, `time.sleep`, a long
  `wait_for`) is a gap in the movement record; a movement check then flags the
  first step after the gap. `Suite.keep_ticking()` runs alongside the scenarios
  and ticks whenever the last movement packet is more than 75 ms old.
- **Back-to-back digging is a fast break.** Vanilla waits five ticks after a
  block breaks before starting the next one; `dig()` now enforces the same
  cooldown, so a three block column can be mined in a loop.

## The result this document is written against

```
powershell -File .\tools\ci_local.ps1 -Live
== summary
  ok    byte-compile (protobot, tools, tests)           0.2s
  ok    import hygiene (tools\check_imports.py)         0.5s
  ok    unit tests (no server, no network)             49.7s
  ok    acceptance suite (33 cases, GrimAC on)        267.1s
  total 317.5s
  all steps passed
```

342 unit tests plus the 33 live scenarios (`[suite] 33/33 passed`,
`[suite] no anti-cheat alerts`); GrimAC 2.x with `alerts.print-to-console: true`,
`experimental-checks: false`.

Two scenarios live in a state the server controls rather than the bot: case 31
kills the night only when the sleeping player is the last one awake, so with an
operator connected it asserts what the bot owns instead (the bed accepted it and
`SleepTimer` reached 100) and says so in chat. Case 26 needs a second client to
watch the first one.
