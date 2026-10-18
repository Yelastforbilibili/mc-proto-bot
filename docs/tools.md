# Hardness and tools

`dig` sends the same two packets whatever the bot is holding, so nothing in the
protocol says how long to hold. The server decides from three things it knows
privately: the block's hardness, the tool's speed, and whether the tool is good
enough to get drops. This document covers the data that reproduces those three,
the API over it, and the live check that the numbers are vanilla's.

## Why the data is bundled

Hardness is **not** in Mojang's generated reports. `reports/blocks.json` lists
block state ids and their names, nothing else. minecraft-data does carry
`hardness`, `material` (`"mineable/pickaxe"`, sometimes `"leaves;mineable/hoe"`)
and `harvestTools`, so the tables are generated from that and joined by name
onto the release's own block list:

```console
$ python tools/gen_break_times.py --mcdata ../artifacts/mcdata/1.21.11-blocks.json \
      --items-protocol 774 --version 1.21.11 --protocol 774 \
      --out protobot/data/breaking-774.json.gz

$ python tools/gen_break_times.py --reports ../artifacts/gen-26.3/generated/reports/blocks.json \
      --fallback protobot/data/breaking-776.json.gz --version 26.3 --protocol 777 \
      --out protobot/data/breaking-777.json.gz
```

`--reports` walks the release's authoritative block list and fills each name
from the previous release's table (`--fallback`), which is why the shipped
tables report how many blocks are still missing:

| release | protocol | blocks | notes |
| --- | --- | --- | --- |
| 1.21.8 | 772 | 1105 | direct from minecraft-data |
| 1.21.11 | 774 | 1166 | direct from minecraft-data |
| 26.1 | 775 | 1166 | 2 new blocks have no hardness yet |
| 26.2 | 776 | 1166 | 30 missing |
| 26.3 | 777 | 1166 | 120 of the 1286 blocks are new in 26.3 and unknown |

A block that is not in the table returns `None` instead of a made-up number, so
an unknown block is a "hold until you see it break", never a silent 0.2 s.

## The formula

Vanilla computes progress per tick and breaks the block when it reaches 1.0:

```
damage  = speed / hardness / (30 if the tool can harvest, else 100)
ticks   = ceil(1 / damage)
seconds = ticks / 20
```

Two consequences the API leans on:

* the tool's speed applies whenever the tool is the right **kind**, even if the
  tier is too low to get drops (a golden pickaxe is fast on obsidian and still
  gets nothing);
* "requires a tool" is not the same as "tier 0". Stone needs *a* pickaxe (tier
  0, wooden suffices) and obsidian needs *diamond* (tier 3); dirt's tool is a
  shovel but a bare hand still harvests it. The table stores both flags
  (`requires` and `tier`) so those three cases stay distinct.

## API

```python
from protobot import connect
from protobot.protocol.tools import best_tool, tool_spec, break_seconds

bot = await connect("127.0.0.1", version="26.3")

table = bot.version.breaking
table.hardness("minecraft:stone")                             # 1.5
table.seconds("minecraft:stone")                              # 7.5   bare hand
table.seconds("minecraft:stone", tool="minecraft:wooden_pickaxe")   # 1.15
table.seconds("minecraft:stone", tool="minecraft:iron_pickaxe")     # 0.4
table.seconds("minecraft:obsidian", tool="minecraft:iron_pickaxe")  # 41.7, no drops
table.tier("minecraft:obsidian")                              # 3
table.requires_tool("minecraft:dirt")                         # False

bot.actions.break_time("minecraft:stone")            # uses whatever is held
bot.actions.best_tool_for("minecraft:stone")         # (38, "minecraft:iron_pickaxe")
await bot.actions.equip_tool("minecraft:stone")      # selects it, or REJECTED
await bot.actions.dig(position, hold=0.5)            # hold the button 0.5 s
```

`best_tool_for` only looks at the hotbar (slots 36..44): switching to anything
else would need a container move first, and a caller that wants that can do it
with `bot.menu`. `equip_tool` is a no-op when the best tool is already held, and
answers `REJECTED` with `"no tool in the hotbar is good enough for ..."` when the
inventory cannot harvest the block at all.

`break_time_estimate(state_id, hardness=..., tool_factor=...)` still exists for
callers that hold their own hardness numbers; `break_time` is the table-driven
one.

## Live proof

`tools/live_tool.py` holds the button for the same 0.4 s twice, once with each
pickaxe, and records when the block actually disappeared (the server's own
`block_update`, not a sleep):

```json
{
  "predicted_break_seconds": {"hand": 7.5, "wooden_pickaxe": 1.15, "iron_pickaxe": 0.4},
  "equipped": {"held": "minecraft:iron_pickaxe", "evidence": "selected minecraft:iron_pickaxe in hotbar slot 1"},
  "iron":   {"status": "confirmed", "total_to_break": 0.453, "delay_after_release": 0.053},
  "wooden": {"status": "confirmed", "total_to_break": 1.109, "delay_after_release": 0.709},
  "delay_gap": 0.656,
  "suspicious_log": [],
  "ok": true
}
```

The measured 0.453 s and 1.109 s match the predicted 0.4 s and 1.15 s to within
a couple of ticks, and `equip_tool` picked the iron pickaxe without being told
which one was faster.

## Two things that surprise people

**A short hold still breaks the block.** `STOP_DESTROY_BLOCK` with progress
below 0.7 does not cancel anything: the server sets `hasDelayedDestroy`, keeps
counting from the original start tick, and destroys the block once progress
reaches 1.0 on its own (`ServerPlayerGameMode.tick`). That is why the wooden
case above still reports `confirmed` — 0.7 s *after* the bot let go. `dig`
reports `UNCONFIRMED` when nothing happened inside its confirm window, so a
probe that wants "my hold was too short" has to look at *when* the block went
away, not just whether it did.

**Debug logs are off.** `ServerPlayerGameMode.debugLogging` is gated on
`SharedConstants.DEBUG_BLOCK_BREAK`, which is false on a production server, so a
refused dig leaves no trace in the server log. The block state is the only
witness.
