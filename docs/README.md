# ProtoBot documentation

Start with the [main README](../README.md) for what the library is and what it
supports today; these documents go one layer down. Chinese index:
[README_zh.md](README_zh.md).

| Document | What question it answers |
| --- | --- |
| [architecture.md](architecture.md) | How the bot is split into concerns (connection / movement / packet router / navigator), where a new packet handler or a new feature belongs, and the invariants a tick has to keep. |
| [protocol-verification.md](protocol-verification.md) | How each per-release payload was checked against the real server jar, and how to re-verify a payload after a version bump. |
| [automation.md](automation.md) | The contracts of the goal and task layers: what `is_reached()` must mean, how retries and timeouts are reported, how to add a goal or a task step. |
| [containers.md](containers.md) | How menus work: the packets, the layout tables, click semantics, why the cursor is predicted locally, the machine verbs, and what invalidates a window mid-use. |
| [components.md](components.md) | Why a stack with data components cannot simply be length-prefixed on the clientbound path, which components are decoded, and why an ambiguous slot list is refused. |
| [tools.md](tools.md) | Where hardness and tool tiers come from (they are in no packet), the vanilla break-time formula, and how the per-release tables are generated. |
| [combat.md](combat.md) | Entity raycasting, the arm swing a real client sends while attacking or digging, and how a kill is proven with the server's own health. |
| [anti-cheat.md](anti-cheat.md) | The packet shapes that make a bot look like a player under GrimAC: one movement packet per tick, actions between ticks, cooldowns, aims that hit what they claim. Read this before writing an action. |
| [acceptance-suite.md](acceptance-suite.md) | The 33 in-game scenarios, how to trigger one from chat, how each scenario builds and cleans its scene, and what a green run looks like. |

## The rules the documents keep repeating

1. **The server is the only source of truth.** Every layer reports what the
   server did, not what the bot intended: a block is broken when the block
   update arrived, a click is confirmed when the slot update arrived.
2. **One tick, one movement packet.** Rotation rides in the movement packet of
   the tick it belongs to; actions go between ticks, never inside one.
3. **Prefer the reason over a retry.** A failure names what was missing (an item,
   a window, a face of a block you cannot see) instead of trying again blindly.

## How to check the tree

```powershell
.\tools\ci_local.ps1            # byte-compile, import hygiene, unit tests
.\tools\ci_local.ps1 -Live      # the same plus the 33 in-game scenarios
```

The in-game suite needs a Paper 26.3 test server (see the
[bridge repository](https://github.com/Yelastforbilibili/mc-prototest-bridge))
and `-ServerLog` pointing at its `logs/latest.log`.
