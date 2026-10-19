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

## How a scenario is isolated

`Suite.reset()` runs before every scenario: close the window, clear the
inventory, kill tagged mobs **and dropped items**, teleport back to the anchor.
Dropped items matter: leftovers from an earlier scenario fly into the inventory
as soon as the bot walks over them, which turns "dug three logs" into six.

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
python tools/live_suite.py --version 26.3 \
    --server-log ..\Test26.3Server\logs\latest.log --run all
[suite] 23/23 passed
[suite] no anti-cheat alerts
```

301 unit tests plus the 23 live scenarios; GrimAC 2.x with
`alerts.print-to-console: true`, `experimental-checks: false`.
