# Hitting an entity needs the hitbox, not the id

Blocks are easy: every full block is the unit cube, so a ray cast walks voxels
and the first solid one is the answer. Entities are not: the client is told an
entity id, a *feet* position and a type id, and it has to reconstruct the box
itself before it can decide whether the crosshair is on it.

Three things follow, and each one is a place where a naive implementation
silently misses:

1. **The box comes from per-release data.** Hitbox sizes are not on the wire and
   they change between releases (26.3 alone renames and adds a few types). The
   sizes live in `protobot/data/entities-<protocol>.json.gz`, generated from
   Mojang's `registries.json` plus `minecraft-data`'s measured hitboxes:

   ```text
   python tools/gen_entity_ids.py --reports artifacts/gen-26.3/generated/reports/registries.json \
       --sizes artifacts/mcdata/entities-1.21.11.json --version 26.3 --protocol 777 \
       --out protobot/data/entities-777.json.gz
   ```

   Only a handful of types are unmeasured (cushions, sulfur cubes, poplar
   boats); those fall back to the humanoid box instead of being skipped, because
   a missing box turns a real hit into a miss.

2. **The position is the feet, not the centre.** `add_entity` and the movement
   packets all carry the feet position, so the box is built symmetrically around
   x/z and grows upwards:

   ```python
   low, high = entity_bounds(Vec3(x, y, z), (width, height))  # y is the feet
   ```

3. **The reach is the server's reach.** Survival attack range is 3 blocks and
   the server checks it. `Actions.raycast_entity()` defaults to 3.0 and drops
   anything farther, so a hit it reports is one the server will accept.

## Aiming

An entity hit is aimed at the **middle of its hitbox**, not at the feet and not
at the raw packet position:

```python
hit = bot.actions.raycast_entity()          # what the crosshair is on
await bot.actions.look_at_entity(entity_id) # turn towards hitbox centre
result = await bot.actions.attack_looked_at()
```

`attack_looked_at()` returns `None` when the crosshair is not on an entity --
"nothing there" is different from "not sent" -- and otherwise aims, sends
`ServerboundAttackPacket` (26.1+) or `Interact` with the `ATTACK` tag (1.21.x),
and swings.

## Proving it against a real server

Packet ids are audited (`tests/test_packet_ids.py`) and the payloads are read out
of each release's jar (`docs/protocol-verification.md`), but neither proves the
server *accepted* the attack. `tools/live_combat.py` does:

```text
python tools/live_combat.py --version 26.3
```

It summons a stationary mob two and a half blocks in front of the player, aims by
ray cast, hits it on the vanilla cooldown, and asks the server for `Health`
through the bridge after every hit. The run below is the one recorded against
Paper 26.3 (the cow starts at 10 and is gone after the last hit):

```json
{
  "type": "minecraft:cow",
  "size": [0.9, 1.4],
  "distance": 2.5,
  "health_before": 10.0,
  "hits": [
    {"index": 3, "entity_id": 1446, "evidence": "hit entity 1446 at 2.18 blocks", "server_health": 9.0},
    {"index": 4, "server_health": 8.0},
    {"index": 5, "server_health": 7.0},
    "...": "one point per hit until the mob dies"
  ],
  "health_after": null,
  "health_lowest": 1.0,
  "entity_removed": true,
  "swings": 14,
  "suspicious_log": [],
  "ok": true
}
```

`health_after: null` is the server saying the mob is no longer there,
`entity_removed` is the client dropping it from tracking, and `suspicious_log`
is empty because 0.65 s between hits is an ordinary player's cadence -- the
server never logs "moved too quickly" or a reach complaint.

## Arm swings

`attack()` swings as part of the hit, but a swing is not only an attack
flourish: the server turns every swing packet into an arm animation for nearby
players, and a bot that breaks blocks with nothing but `PlayerAction` packets is
the textbook anti-cheat signature of a script.

So `dig()` sends `START_DESTROY_BLOCK`, then swings before the movement packet of
every tick while the button stays down, swings once more and only then sends
`STOP_DESTROY_BLOCK`, and closes that tick with `client_tick_end`.
`use_item_on()`/`place_block()` swing right after using the hand on a block.
The packets are the same ones `swing()` sends, so 26.3 gets the empty
`ServerboundPunchPacket` and 26.1/1.21.x get `ServerboundSwingPacket` with the
hand. Scenario 1 of `tools/live_suite.py` asserts the server recorded them:

```
[#1]     服务端 break [2, -60, 0] @ (2, -60, 0)
[#1]     服务端 swing 4 次 ARM_SWING
[#1] ✓ 挖一个方块 (4.7s)
```

The server's own event log says it *received* the swings. To prove the arm is
visible to other players, `tools/live_swing_seen.py` connects a second bot a few
blocks away and listens for the broadcast the server makes from those swings,
`clientbound_swing_animation` (surfaced as the `swing_animation` event):

```
[bridge] 26.3-205-a90108f (MC: 26.3) players=0
{
 "target": [2, -60, -4],
 "dig_status": "ActionStatus.CONFIRMED",
 "state_before": 10,
 "state_after": 0,
 "watcher_sees_entities": 62,
 "swings_seen": 4,
 "animations": [{"entity_id": 4619, "hand": 0}, ...],
 "ok": true
}
```

Two details that made that probe fail twice before it worked, both about the
second bot rather than the digger:

- **Wait for the chunk without ticking.** `tick()` runs the physics over what the
  client has decoded so far; ticking before the ground arrives means falling,
  and the server believes the falling position because the client keeps
  reporting it. A bot at y=-2500 is tracking nothing.
- **Keep the watcher ticking.** An idle bot stops answering, and the server
  drops its entity tracking, so the animation never arrives.

The 26.3 payload also changed shape: up to 26.2 it is the old
`ClientboundAnimatePacket` (entity id + one action byte, 0 = main arm), from
26.3 it is `ClientboundSwingAnimationPacket` (entity id, hand, then the
`SwingAnimation` component). Both put 0 in the second field for a main-hand
swing, which is what the event reports.

## Things that bit us

- **A leftover mob spoils the proof.** The server tracks entities dozens of
  blocks out, so "nearest cow" can be an older one standing right where the new
  one lands: the ray hits it and the health of the summoned mob never moves. The
  tool kills the leftovers around the summon point first, tags its own mob, and
  matches the *new* entity id rather than a distance sort.
- **The client sees dead entities for a moment.** Entity ids the server removed
  can still be in `bot.entities` for a tick, so the first hit or two of a run can
  go to a ghost. Later hits land, which is why the tool reads the health after
  every swing instead of only at the end.
- **`Bot` has no `entity_id`.** Nothing in the client tracks "which entity is
  me", so the ray cast does not try to skip the player; in survival the player's
  own box is simply not where the crosshair is.
- **A pinned mob proves nothing.** Summoning with `NoAI:1b` keeps the target
  still, which hides everything interesting: a live cow walks, and every hit
  knocks it back out of reach, so the killer has to follow it. Scenarios 15 and
  23 therefore summon ordinary cows and chase them, which is also why they land
  11 hits for 10 health -- one of them fell inside the attack cooldown and dealt
  no damage, exactly as it would for a player.

## Knockback reference: the same arc on a real client and on the bot

A standing, unsprinted punch is the cleanest measurement in the game: the server
sends one impulse (`SetEntityMotion [-0.4, 0.3608, 0]` for the axis it hits) and
everything after that is pure physics. `artifacts/kb_vanilla.py` puts the same
attacker next to the human client, punches once, reads the server's `/trace`
samples (which are the victim's own move packets), then repeats the blow on a bot
from the identical geometry.

Both victims produced, tick for tick, the same twelve moves:

```
#  1: dz 0.4000  dy  0.3608      #  7: dz 0.1363  dy -0.1279
#  2: dz 0.2184  dy  0.2752      #  8: dz 0.1240  dy -0.2037
#  3: dz 0.1987  dy  0.1913      #  9: dz 0.1129  dy -0.2781
#  4: dz 0.1809  dy  0.1091      # 10: dz 0.1027  dy -0.3046
#  5: dz 0.1646  dy  0.0285      # 11: dz 0.0935  dy  0.0000
#  6: dz 0.1498  dy -0.0505      # 12: dz 0.0789  dy  0.0000
```

- **1.96 blocks** of horizontal travel, **9 airborne moves**, roughly half a
  second of air time. The first step is ground friction (`0.4 * 0.546`), the rest
  is air drag (`* 0.91`), and the vertical column is `0.98` drag minus gravity.
- The numbers agreeing to four decimals is the point: the bot's arc is not "close
  to" the real client's, it is the same sequence, because both are the same
  physics on the same impulse.
- How to read a wrong arc: a first step of `0.4` followed by `0.4` again means we
  claimed the impulse twice; a jump straight to `0.2184` means the impulse was
  applied a tick late (which is what the anti-cheat's `AntiKB` offset measures);
  a step of `0.0` in the middle means a teleport correction interrupted the arc.
