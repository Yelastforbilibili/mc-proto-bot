# Items that carry data components

Most items on the wire are five bytes: a count, a registry id, and two zero
counts. Enchanted books, damaged tools, potions, renamed gear and filled shulker
boxes are not: they end with a **data component patch**.

## Why a patch has no length

A patch is written by `DataComponentPatch.STREAM_CODEC`
(`artifacts/decomp263/net/minecraft/core/component/DataComponentPatch.java:78`):

```
positiveCount  varint
negativeCount  varint
positiveCount × (component type id varint, value <its own codec>)
negativeCount ×  component type id varint
```

Nothing prefixes a value with its length. `minecraft:damage` is a varint,
`minecraft:stored_enchantments` is a count plus id/level pairs, `minecraft:custom_name`
is a whole network NBT tag, `minecraft:container` is a list of stacks (which
contain patches, recursively). A reader that does not implement a component's
codec cannot step over it — and since the patch sits *inside* the stack, every
slot after it in the same packet is lost too.

The clientbound path uses `ItemStack.OPTIONAL_STREAM_CODEC`
(`net/minecraft/world/item/ItemStack.java:188`), i.e. the *undelimited* patch.
Only `ServerboundSetCreativeModeSlotPacket` uses the delimited variant, because
the server has to validate untrusted input. So the client cannot read a length
prefix that is not there.

## What is recoverable, and what is refused

`protobot/protocol/stacks.py` recovers what can be recovered and refuses the
rest:

| Situation | Result |
| --- | --- |
| One stack that fills the packet (`set_slot`, `set_cursor_item`, `set_player_inventory`, equipment) | **exact**, even with unknown components: the packet end gives the length |
| Known component values only (see below) | **exact** for the whole slot list |
| Unknown component values in a multi-slot list | **refused**: the list is reported unreadable instead of guessed |

The middle case matters because the alternative is worse than an error. The
first implementation searched for the patch length that made the packet add up;
on a real chest it produced *one* working split where the potion became
`minecraft:polished_sulfur_stairs` and every later slot shifted by one item.
The packet was internally consistent and completely wrong. `read_stacks` now
requires the split to be **unique** and raises `StackFrameError` otherwise.

## The component codecs

`protobot/protocol/component_codecs.py` maps a component *name* to the number of
bytes its value takes:

| Kind | Components | Wire |
| --- | --- | --- |
| `unit` | `unbreakable` | no bytes at all |
| `varint` | `damage`, `max_damage`, `max_stack_size`, `repair_cost` | one varint |
| `enchantments` | `enchantments`, `stored_enchantments` | varint count + (varint, varint) pairs |
| `nbt` | `custom_name`, `item_name` | one network NBT tag |
| `components` | `lore` | varint count + that many NBT tags |

The IDs themselves are per release and generated, not hand written:

```bash
python tools/gen_component_ids.py \
    --reports artifacts/gen-26.3/generated/reports/registries.json \
    --version 26.3 --protocol 777 --out protobot/data/components-777.json.gz
```

`version.components` resolves an ID to a name, and `version.component_codecs`
gives the decoder the readers for that release. A component that is not in the
table is *not* guessed — the search branches over its length, and a branch that
is not unique is refused.

## What is still out of reach

`potion_contents`, `container` (shulker contents), `attribute_modifiers`, `tool`,
`food`, `equippable`, `banner_patterns`, `bees`, … are simply not implemented
yet. Their values are decodable in principle — `PotionContents.STREAM_CODEC` is
`optional(potion) + optional(int) + list(effect) + optional(string)` — but each
needs its own reader, and the honest behaviour without one is already in place.

A window that contains such an item stays byte-only: `menu.readable(slot)` is
`False`, `ContainerState.unreadable` lists the slots, and clicks still work
because a click does not need the slot contents. A *single* stack packet for the
same item decodes exactly, so the cursor and a clicked slot are always readable.

To add a codec: implement a reader `(data, index, end) -> new_index` in
`component_codecs.py`, register it in `KINDS_BY_NAME`, and regenerate the tables.
`tests/test_stacks.py` shows the shape.

## Live proof

`tools/live_components.py` fills two chests with `/item replace block` and
compares the decoded window against the server's own `data get block ... Items`:

* **known** — stone, a diamond pickaxe with `damage=1500`, an enchanted book
  with `stored_enchantments`, a renamed netherite sword (`custom_name`), an
  unbreakable pickaxe, planks, apples. All nine slots decoded to the right item
  and count with `unreadable == []`, and every component-bearing slot kept
  `has_components`.
* **unknown** — a potion, a filled shulker box, dirt, stone. The 63 slot content
  packet was refused (`unreadable` = every slot), then picking the potion up
  decoded the cursor exactly (`minecraft:potion x1`, `has_components`), and
  putting it back left the server's `Items` unchanged.

```
"known_problems": [],
"unknown_problems": [],
"unknown": {
  "unreadable": [0, 1, 2, "...", 62],
  "after_pickup": {"carried": ["minecraft:potion", 1, true], ...}
},
"ok": true
```
