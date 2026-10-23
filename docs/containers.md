# Container layer

`protobot.containers` turns the raw container packets into the thing a player
actually does: look in a window, take a stack, put it somewhere else, read how
long the furnace has left. Every operation is a real click sent to the server,
and every result is read back from the server's own reply -- nothing is
predicted locally.

```python
await bot.menu.open_block((6, -60, -8))     # right click a chest, wait for it
menu = bot.menu
print(menu.describe())                      # window, slots, contents
print(menu.count("minecraft:dirt"))         # what is in the chest

await menu.deposit("minecraft:dirt", 5)     # player inventory -> chest
await menu.withdraw("minecraft:dirt", 5)    # chest -> player inventory
await menu.close()
```

## What the server sends

The layer sits directly on four packets. Their layout was read out of the 26.3
server (`ServerGamePacketListenerImpl`, `AbstractContainerMenu`) and confirmed
against a live Paper server:

| packet | payload |
| --- | --- |
| `clientbound_open_screen` | `container_id varint`, `menu_type_id varint`, NBT title |
| `clientbound_container_set_content` | `container_id varint`, `state_id varint`, `count varint`, `count` × ItemStack, carried ItemStack |
| `clientbound_container_set_slot` | `container_id varint`, `state_id varint`, `slot short`, ItemStack (slot `-1` is the cursor) |
| `clientbound_container_set_data` | `container_id varint`, `data_id short`, `value short` |

The player inventory arrives as `container_id == 0` and is mirrored into
`bot.player.inventory`, so `/give` and pickups are visible without opening
anything.

`clientbound_block_changed_ack` is **not** used for this: in 26.3 it only
carries a sequence number.

## Layouts

`bot.version.menus` is generated from the release's `registries.json`
(`tools/gen_menu_ids.py`, data in `protobot/data/menus-<protocol>.json.gz`), so
a menu id resolves to a name like `minecraft:generic_9x3` or
`minecraft:crafting`.

`layout_for(identifier, slot_count)` then maps names to groups:

| group | meaning |
| --- | --- |
| `container` | the window's own slots (a chest's 27, a furnace's input/fuel) |
| `result` | output-only slots (`mayPlace` is false server-side) |
| `player` | the 36 main + hotbar slots, always the tail of the window |
| `main`, `hotbar`, `armor`, `offhand`, `craft`, `grid`, `input`, `fuel`, … | the individual groups |

`menu.slot("input")`, `menu.slots("hotbar")`, `menu.item(menu.slot("result"))`.
When a window is not in the table the layout falls back to "everything before
the last 36 slots is the container" and reports `derived=True`.

## Clicks

```python
await menu.click(slot, button=0, mode="pickup")   # raw: what the packet carries
await menu.pickup(slot)                           # left click
await menu.pickup(slot, button=1)                 # right click (take/place one)
await menu.quick_move(slot)                       # shift click
await menu.swap_with_hotbar(slot, 3)              # number key
await menu.throw(slot, all_=True)                 # drop
```

A click is confirmed by *observing the window change*, never by assuming it
worked -- `click()` returns `False` when the server ignored it. It watches
`ContainerState.slot_revision`, which only moves when slots or the cursor
change: a furnace streams progress packets several times a second, and
confirming on those made a transfer look finished before its slot update
arrived (the item then sat on the cursor, invisible to both sides).

`menu.move(source, target)` drives the cursor the way a player does: take the
stack, put it down (whole or one at a time with right clicks), put the remainder
back. It returns how many items the server actually moved, and raises
`ClickRejected` if a stack would be left on the cursor.

The cursor arrives in its own packet, `clientbound_set_cursor_item` -- not as a
`set_slot` with slot `-1`. The server only sends it when the click packet's
claims (changed slots, carried item) did not already match what the click
produced, so a click that *empties* the cursor stays silent, and a client that
only listens ends up believing it is still holding a stack. `click()` therefore
predicts the cursor the way a vanilla client does (from the two stacks and the
button) and falls back to that prediction only when no announcement arrived for
that click; a real announcement always wins.

## Crafting

```python
from protobot.crafting import recipe_for

menu = bot.player_menu                                # the 2x2 grid is always open
await menu.craft(recipe_for("minecraft:stick"), 2)

await menu.open_block((12, 64, -3))                   # right-click the table
await menu.craft(recipe_for("minecraft:stick"), 2)    # 3x3 grid
```

`Recipe` is an explicit description (`pattern`/`key`, or `shapeless`), not a
scraped recipe database: `recipe_for(...)` covers a handful of built-ins and a
caller with its own recipes just constructs `Recipe(...)`.

The primitives are the clicks a player makes:

| call | what it does |
| --- | --- |
| `menu.grid_slots()` / `menu.grid_slot(row, column)` | which slots are the crafting grid |
| `menu.fill_grid(recipe)` | take a stack of each ingredient, right-click every cell it occupies, put the remainder back |
| `menu.craft_once(recipe)` | take the result, put it in the inventory, report how many the server handed over |
| `menu.clear_grid()` | shift-click the grid back into the inventory |
| `menu.craft(recipe, times)` | the four above, repeated, stopping early when the materials run out |

`CraftGoal(recipe, times=1, table=None)` wraps that for the goal layer: a
recipe that fits 2x2 crafts in the inventory, anything bigger needs
`table=(x, y, z)` and the goal walks over and opens it. Progress is measured in
items the server actually delivered into the inventory, so "already holding
enough sticks" does not count as having crafted them -- and a recipe that needs
more materials than the inventory holds fails with the missing list instead of
a silent no-op.

## Progress data

`menu.data_value("cook_progress")` reads `container_set_data` by name:

| menu | names |
| --- | --- |
| `furnace`, `blast_furnace`, `smoker` | `lit_time`, `lit_total`, `cook_progress`, `cook_total` |
| `brewing_stand` | `brew_time`, `fuel` |
| `enchantment` | `cost_0`, `cost_1`, `cost_2`, `seed` |
| `beacon` | `levels`, `primary_effect`, `secondary_effect` |
| `stonecutter` | `selected_recipe` |
| `crafter_3x3` | `triggered`, `slot_disabled` |

## Live proof

`tools/live_chest.py` (Paper 26.3, bridge as the authority):

```text
deposit 5  -> server: [{Slot: 0b, id: "minecraft:dirt", count: 5}]   (data get block)
withdraw 5 -> server: []                                             (data get block)
              player NBT: dirt 5 (Slot 0b) + dirt 5 (Slot 9b)
```

`tools/live_furnace.py`:

```text
move cobblestone x3 -> fuel coal x3
lit_time 1592 -> 1492, cook_progress 0 -> 109, cook_total 200
server: [{Slot: 0b, cobblestone 2}, {Slot: 1b, coal 2}, {Slot: 2b, stone 1}]
take    -> server: [{Slot: 0b, cobblestone 2}, {Slot: 1b, coal 2}], player has the stone
```

`tools/live_menus.py` opens every block-openable menu on 26.3 (21 of them) and
checks the slot count and layout against the server: chest 63, crafting 46,
furnace 39, enchanting 38, beacon 37, crafter 46 (9 crafting grid + 1 read-only
preview + 36 player), and so on.

`tools/live_merchant.py` covers the one menu no block opens: it summons a
farmer villager who sells 6 bread for 2 emeralds, opens it with
`Menu.open_entity`, deposits the emeralds, waits for the server to fill the
result slot, shift-clicks it, and then asks the server for the truth:

```text
deposited 2 -> result slot: minecraft:bread (filled by the server, not by us)
took       -> player NBT: bread 6 (Slot 8b)
villager   -> Offers.Recipes[0].uses: 1
```

The `uses` counter is the part that matters: it only moves when the server
executes the trade, so this proves the clicks, the result slot and the
`minecraft:merchant` layout together.

`tools/live_craft.py` crafts the whole chain from logs, with the server's own
inventory NBT as the proof:

```text
give oak_log 8
craft 8x oak_planks (2x2 in the inventory)   -> done
craft 1x crafting_table (2x2)                -> done
move the table to hotbar slot 37, place it   -> server: minecraft:crafting_table
walk over, open it, craft 4x stick (3x3)     -> done
player NBT: oak_log 6 (Slot 0b), stick 4 (Slot 9b), oak_planks 2 (Slot 10b)
```

## While a window is open

The server announces inventory changes on the *open window's* container id, so
`/give` and pickups arrive as window slots rather than as container 0. The
client mirrors the window's player-side slots back into `bot.player.inventory`
through the layout, which keeps `bot.get_inventory_item()` honest for the whole
time a chest or a villager stays open.

### Why a window stopped working

A window dies without the caller asking: the player dies, walks out of reach, or
the block behind it gets broken or replaced. Two ways to ask about that:

```python
menu.invalid_reason()       # None while usable, otherwise a sentence
menu.live()                # same check, raises MenuNotOpen(reason)
```

The waits hand that same sentence to `protobot.expectation`, so a window that
closes mid-click ends the wait as `INVALIDATED` instead of polling until the
timeout:

```python
await menu.pickup(13)                  # True / False, as before
menu.last_outcome.status               # DONE, or INVALIDATED + reason
menu.last_outcome.describe()           # "invalidated after 0.004s (the server closed window 1 ...)"
```

`wait_until_quiet` records the same outcome when the window disappears while a
click is still settling, which used to look like a slow server.

## Items this protocol cannot decode

An item carrying *added* data components is not self-delimiting on the wire, so a
slot holding one keeps its raw bytes and is reported through
`menu.readable(slot) == False` / `ContainerState.unreadable`. Queries skip those
slots and `move` refuses to touch them instead of silently treating them as
empty.

That is the *honest* fallback, used when the component values in a multi-slot
packet cannot be read and the split is ambiguous. Two things are still exact: a
single-stack packet (the cursor, a clicked slot, the player inventory) is
delimited by the packet end, and a window whose components all have a codec
decodes completely. See [components.md](components.md) for the wire format, the
component codecs that ship, and the live proof.

## Adding a menu

1. `uv run python tools/gen_menu_ids.py --reports generated/reports/registries.json --protocol 777 --version 26.3 --out protobot/data/menus-777.json.gz`
2. add an entry to `_SEGMENTS` in `protobot/containers/layouts.py`
3. add `DATA_SLOTS` names if the window has progress bars
4. `uv run pytest` then `uv run python tools/live_menus.py --blocks <block>`
