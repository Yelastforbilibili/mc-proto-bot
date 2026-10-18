# Verifying a release before you trust the ids

ProtoBot's packet ids come from data, not from memory: `tools/gen_packet_ids.py`
reads Mojang's own `reports/packets.json` (or a `minecraft-data` protocol dump)
and `tests/test_packet_ids.py` fails the moment a checked-in table disagrees
with the source.  That covers *which id* a packet has.

It cannot cover the other half.  Vanilla also changes what is *inside* a packet
without renaming it, and a wrong payload is worse than a wrong id: the id gets
your bytes to the server, so the server accepts the connection and silently
does nothing.  This is exactly how the interaction layer looked "implemented"
and still could not break a block.

Four payloads moved:

| What | 1.21.x (772/774) | 26.1 / 26.2 (775/776) | 26.3 (777) |
| --- | --- | --- | --- |
| `ServerboundPlayerActionPacket.Action` ordinals | `START 0, ABORT 1, STOP 2, ...` (1.21.8 has 7 constants, 1.21.11 has 8: `STAB` appended at 7) | same seven ordinals, `STAB` appended at 7 | `CHANGE_DESTROY_DIRECTION` inserted at **1**, so `ABORT 2, STOP 3` |
| Arm swing | `ServerboundSwingPacket(hand)` | `ServerboundSwingPacket(hand)` | `ServerboundPunchPacket()` — **no fields at all** |
| `Interact` | `entityId + type [+ vec3] [+ hand]` | `entityId + hand + Vec3 + bool`, no type | same as 26.1 |
| Attack | `Interact` type `ATTACK` | `ServerboundAttackPacket(entityId)` | same as 26.1 |

Two details cost a full debugging session each and are easy to miss in a table:

* `BlockHitResult` writes **two** booleans — `isInside()` **and**
  `isWorldBorderHit()`.  Writing one shifts `sequence` by a byte, and the server
  then discards `ServerboundUseItemOnPacket` without an error.
* `BlockPos` is a packed long (8 bytes), `Direction` is a **byte** in
  `PlayerAction` but a **varint enum** inside `BlockHitResult`, and the three
  floats of an `INTERACT_AT` are absolute world coordinates while the three
  floats of a block hit are block-relative `[0, 1)`.

`protobot/actions/profiles.py` encodes the rows; `Actions` branches on the
profile of the connected release, so a new row is a data change, not a rewrite.
`tools/gen_packet_ids.py --emit` prints the id table half.

## The ritual for a new release

Everything below runs offline against the release's own jar; no guessing from a
wiki and no server needed.

1. **Ids.** Fetch the version's server bundle from the Mojang manifest, then

   ```bash
   java "-DbundlerMainClass=net.minecraft.data.Main" -jar server-bundle.jar --reports
   ```

   On PowerShell the `-D` property must be quoted or the shell eats
   `net.minecraft...` and the JVM fails with `ClassNotFoundException`.
   `generated/reports/packets.json` is the source of truth for ids:

   ```bash
   python tools/gen_packet_ids.py --reports generated/reports/packets.json \
       --protocol 777 --version 26.3 --out tests/data/packet-ids-777.json
   ```

   `--emit` additionally prints a `PlayPacketIds(...)` literal for
   `protobot/protocol/versions.py`.

2. **A jar with readable names.** Mojang's 26.x bundles ship
   `META-INF/versions/<id>/server-<id>.jar` already Mojang-mapped.  1.21.x
   `server.jar` is obfuscated — use the matching **Paper** jar instead, which
   ships Mojang-mapped classes.  Extract each version into its own directory:
   two extractions into one directory silently give you the *first* version's
   inner jar again (same filename, same size, wrong release).

   The 1.21.x recipe we used, with the three obstacles spelled out:

   ```bash
   # Paper's v2 download API is gone (HTTP 410); v3 is current:
   #   https://fill.papermc.io/v3/projects/paper/versions/<VER>/builds
   # The download is a patch-mode paperclip: it ships no server jar, only
   #   META-INF/versions/<VER>/server-<VER>.jar.patch
   java -jar paper-<VER>.jar    # refuses to start: PKIX path building failed
   ```

   Downloading the vanilla jar yourself first (`cache/mojang_<VER>.jar` inside
   the paperclip's own run directory) makes Paperclip apply its patches and
   produce the Mojang-mapped jar; `javap` then reads it normally.

3. **Enums that go over the wire.** `writeEnum` puts the ordinal on the wire,
   so the declaration order *is* the protocol:

   ```bash
   javap -p -classpath inner.jar "net.minecraft.network.protocol.game.ServerboundPlayerActionPacket\$Action"
   ```

   Read the `public static final` lines top to bottom; the position of
   `STOP_DESTROY_BLOCK` is the value to send.

4. **Record components and payload order.** For a packet that still has a
   hand-written `write` method, the order comes from the bytecode:

   ```bash
   javap -p -c -classpath inner.jar net.minecraft.network.protocol.game.ServerboundUseItemOnPacket
   ```

   Look at the `invokevirtual` sequence inside `private void write(...)`.
   Composite codecs (`StreamCodec.composite(...)`) already show the order in the
   decompiled source.

5. **Update the row, not the primitives.** Add or edit an `InteractionProfile`
   in `protobot/actions/profiles.py` and a `tests/data/packet-ids-<protocol>.json`
   truth file, then `python -m unittest discover -s tests -t .`.

## Proving it against a real server

Unit tests pin the bytes; they cannot prove the server agrees.  `tools/live_dig.py`
is the end-to-end assertion the interaction layer exists for:

```bash
uv run python tools/live_dig.py --host 127.0.0.1 --port 25565 --version 26.3
```

It connects, digs the block under the player, and reports three facts: the
client's world state went from solid to `0`, the action confirmed, and (with the
prototest bridge watching the server) the server logged a block break event with
`cancelled: false`.  Exit code is non-zero when the block did not break.

Two server behaviours worth knowing because they look like client bugs:

* the server only breaks the block when it sees `STOP` — `ServerPlayerGameMode`
  accumulates progress per tick and calls `destroyAndAck` once
  `progress >= 0.7`.  A `hold` that is too short sends a valid `STOP` that
  finishes nothing;
* the debug lines explaining a refusal are behind `SharedConstants.DEBUG_BLOCK_BREAK`,
  which is off on a production server, so "too far" / "too high" refusals are
  silent.

## Placing needs the item table, breaking does not

Mining carries no item payload, so `dig` works from ids alone.  Every attempt to
*place* has to name the block being held, and an item id is not portable:

| | 1.21.8 (772) | 26.3 (777) |
| --- | --- | --- |
| `minecraft:dirt` | 28 | 55 |
| `minecraft:grass_block` | 9 | 54 |

`protobot/protocol/items.py` loads `protobot/data/items-<protocol>.json.gz`
(generated by `tools/gen_item_ids.py` from `reports/registries.json` for 26.x and
from `minecraft-data` for 1.21.x), so `PlaceBlockGoal` resolves "dirt" to the id
that release actually uses.  Before that table existed the whole library knew
exactly two items (elytra and leather boots, the only ones the physics code
cared about) and every placement failed as "no such item" while looking like a
missing inventory.

The end-to-end assertion for placement is `tools/live_place.py`:

```bash
uv run python tools/live_place.py --host 127.0.0.1 --give --reset --target 2,-60,0
```

It gives itself the block, waits for the inventory update, places, and reports
both halves of the truth: `state_after` from the client's own world and
`server_block.stateId` read back from the server.  `--reset` clears the
destination first, which makes the run repeatable and exercises one more real
behaviour: a `setblock` arrives as a `clientbound_block_update` a few
milliseconds *after* the HTTP call returns, so an action fired immediately
afterwards still sees the old block and is rejected as "destination is not air".
Tools wait for the update; library code never guesses.
