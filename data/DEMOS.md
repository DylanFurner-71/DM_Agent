# Demo scenarios

Focused scenarios, one per feature-cluster from the README's **Features
(implemented)** list. Each is small and built so the matching feature is easy to
trigger and watch. Drive them from the terminal:

```bash
python -m src.main data/demos/demo_combat.json
```

In every session you can use the CLI commands to observe what the engine did:
`/help`, `/state` (HP, slots, inventory, NPCs, combat order), `/hud` (toggle the
compact status header shown before each prompt), `/recap` (story so far),
`/roll <notation>` (open flavor roll), `/undo` (rewind the last turn),
`/trace` (tools called per turn), `/full_trace` (adds timing + token usage),
`/save [name]`, `/quit`.

> **A note on dice.** Outcomes are real rolls, so a given run may vary (a
> persuasion can fail, a PC may not drop exactly when expected). The scenarios are
> *tuned* to make the target feature likely; if a roll goes the other way, the
> feature still demonstrates — just keep playing or relaunch. The hard guarantees
> (slot economy, gates, turn order, redaction) are deterministic regardless of dice.

## Feature → scenario map

| README feature | Scenario |
|---|---|
| Combat (initiative, turn guard, AC/crit, auto-target & ambiguous_target, auto-end) | `demo_combat.json` |
| Death, downed state & endgame (death saves, revive, defeat/victory epilogue) | `demo_death_saves.json` |
| Social & companions (`influence_npc`, immovable foe, `recruit_npc`, ally in combat) | `demo_social_companions.json` |
| Stealth & ambush (group stealth, surprise round, always-alert foe) | `demo_stealth.json` |
| Exploration: scenes, gates & loot + Quest flags (flag gate, answer gate, `take_item`) | `demo_gates_loot.json` |
| Spells & items (slot economy "money shot", Pearl-of-Power cap, `use_item`, `lookup_rule`) | `demo_spells_items.json` |
| Checks (proactive `skill_check`: perception/athletics/arcana, fail-forward, in-combat) | `demo_skill_checks.json` |
| Saving throws & hazards/traps (`trigger_hazard` author-placed traps + bare `saving_throw`) | `demo_saving_throws.json` |
| Reinforcements (`add_npc`, author-declared, trigger-gated, mid-combat insertion) | `demo_reinforcements.json` |
| Flat effects & flavor rolls (`modify_hp` exact damage/heal, `roll_dice` fiction-only) | `demo_flat_effects.json` |
| Branching geography (a fork with two routes that reconverge, multi-scene) | `adventures/stormhold_keep.json` |
| Persistence & resume | any scenario — see the bottom section |
| Autosave & `/undo` (rewind a turn) | any scenario — see the bottom section |

---

## demo_combat.json — Combat

```bash
python -m src.main data/demos/demo_combat.json
```

**Party:** Aldric (cleric, mace), Kael (rogue, finesse shortsword + shortbow),
Wisp (mage). **Foes:** Grik (goblin) and Fang (wolf). Single terminal room —
clearing it wins.

**Shows:** `start_combat` with rolled initiative, the engine-owned turn pointer and
turn guard, weapon attacks vs AC (Kael's finesse weapon uses DEX), crits on a
natural 20, `ambiguous_target` vs auto-target, and the automatic victory epilogue
when the last foe falls in a terminal scene.

**Play it:**
1. `we attack the goblin and the wolf!` → DM calls `start_combat`; the engine prints
   the initiative order and prompts whoever is first. (`/trace` shows `start_combat`.)
2. On a character's turn, type a **bare** action to see disambiguation:
   `Kael attacks` → with two foes alive the engine returns `ambiguous_target` and the
   DM asks "Grik or Fang?".
3. Name the target: `Kael shoots Fang with the shortbow` (or `Aldric swings his mace
   at Grik`). Try acting out of turn (`Wisp casts magic missile` when it isn't her
   turn) to see the turn guard refuse it.
4. Keep going. Once one foe is down, a bare `attack` **auto-targets** the survivor.
5. When both are down, combat auto-ends and the **victory epilogue** fires. Watch a
   natural-20 hit double the damage dice in `/full_trace`.

---

## demo_death_saves.json — Death, downed state & endgame

```bash
python -m src.main data/demos/demo_death_saves.json
```

**Party:** Aldric (sturdy, carries a healing potion), Wisp (fragile — **5 max HP**).
**Foes:** Skar and Vrik (goblins). The engine targets the lowest-HP conscious PC, so
the goblins pile onto Wisp.

**Shows:** a PC dropping to 0 HP (unconscious, dying), the engine **auto-rolling
death saves** on the downed PC's turn (you never roll them yourself), and reviving a
downed ally by **administering an item** — which resets their death-save counters.

**Play it:**
1. `roll for initiative — we fight!` → `start_combat`.
2. Play it out. The goblins focus **Wisp**; a solid hit drops her to 0. On her turn
   the engine prints a death-save result automatically (watch `/state` for her
   `unconscious` condition and the banner each round).
3. On **Aldric's** turn, revive her: `Aldric pours his healing potion down Wisp's
   throat`. This is `use_item` with a `target` — it spends *Aldric's* action, heals
   Wisp above 0, and clears her dying state. `/trace` shows the revive.
4. Finish the goblins for the victory epilogue.
   - *Want the defeat epilogue instead?* Let both PCs fall (don't heal Wisp, let
     Aldric take hits) — a full party wipe ends the run in defeat.

---

## demo_social_companions.json — Social & companions

```bash
python -m src.main data/demos/demo_social_companions.json
```

**Party:** Aldric, Wisp (high CHA, the face). **Scene 1 (parley_ledge):** Snik, a
goblin open to reason (`disposition_dc 13`). **Scene 2 (warren):** Rattle, a
**mindless skeleton** (`disposition_dc null` = immovable) and Grub (goblin).

**Shows:** `influence_npc` swaying a hostile to neutral, the "immovable" foe that
cannot be reasoned with, `recruit_npc` turning a won-over NPC into a **companion**,
the companion **following across scenes**, and fighting hostiles on the party's side.

**Play it:**
1. `Wisp tries to talk Snik down — persuade him to stand aside` → `influence_npc`
   (one attempt per NPC; success flips Snik to non-hostile). `/state` shows Snik as
   friendly.
   - *If it fails:* out of combat a failed parley auto-starts a fight — the engine
     rolls initiative and you fight Snik instead. (Relaunch to retry the social path.)
2. `Wisp asks Snik to join us` → `recruit_npc`. Snik is now a companion.
3. `we head down the rope-bridge into the warren` → `move_scene`; Snik **follows**.
   (`/state` still lists Snik, now in the warren.)
4. `Wisp tries to reason with the skeleton` → `influence_npc` returns **immovable** —
   Rattle can't be talked to.
5. `we attack — Snik, fight with us!` → `start_combat` including `snik`. On Snik's
   turn the **engine resolves his attack** against a hostile automatically; narrate it
   like any NPC beat. Clear the room to win.
   - *Bonus:* attack a calmed NPC to see it **re-provoke** to hostile.

---

## demo_stealth.json — Stealth & ambush

```bash
python -m src.main data/demos/demo_stealth.json
```

**Party:** Kael (DEX +3) and Wisp (DEX +2) — both stealthy. **Scene 1
(sleeping_sentries):** Doz and Grit, drowsy goblins (`alertness_dc 4`, easy to
ambush). **Scene 2 (watchpost):** The Watcher, a skeleton with `alertness_dc null`
(**always alert**).

**Shows:** `attempt_ambush` as a weakest-link group stealth check vs the highest
alertness DC, a won **surprise round** (surprised foes are skipped on round 1), and
an always-alert foe that **cannot** be ambushed (the engine refuses and drops you
straight into a fair fight).

**Play it:**
1. `we sneak up on the two lookouts` → `attempt_ambush` (bar = 4; both PCs roll DEX).
   On success the engine sets a pending ambush. (`/trace` shows the per-PC rolls.)
2. `now we strike!` → `start_combat`; the surprised goblins are marked and **lose
   their first turn**. Cut them down.
3. `we crawl deeper toward the blue glow` → `move_scene` to the watchpost.
4. `we try to sneak up on the Watcher` → `attempt_ambush` returns **cannot_ambush**
   and the engine **auto-starts combat** (no surprise) — the foe was already watching.
   Do *not* call start_combat yourself; just fight. Win for the victory epilogue.

---

## demo_gates_loot.json — Exploration: scenes, gates & loot (+ quest flags)

```bash
python -m src.main data/demos/demo_gates_loot.json
```

**Party:** Aldric, Wisp. No combat — pure exploration. **Scene 1 (antechamber):** a
journal naming the password *ashfall*, a bronze lever, loot (`healing_potion`,
`bronze_key`), and a **flag-gated** arch. **Scene 2 (gallery):** loot
(`pearl_of_power`) and an **answer-gated** iron door. **Scene 3 (vault):** the
Sundering Crown — terminal victory.

**Shows:** author-placed loot revealed by searching (`take_item`), a **flag gate**
(`requires`) opened by a quest flag, an **answer gate** (`requires_answer`) where the
password is redacted from the model and you must *speak* it, fixed geography (the DM
can't invent exits), and the victory epilogue.

**Play it:**
1. `search the antechamber` → DM reveals and grants loot (`take_item`
   `healing_potion`, `bronze_key`) and reads the journal, learning *ashfall*. The DM
   records the discovery as a quest flag — see it in `/state`.
2. `try the warded arch` → `move_scene` is refused (**locked**) — the ward is up.
3. `pull the bronze lever` → DM sets a quest flag (e.g. `ward_lowered`).
4. `now go through the arch` → `move_scene` succeeds into the gallery.
5. `take the pearl`, then `go to the iron door and say "ashfall"` → the DM relays your
   exact word to the answer gate; the door opens to the vault. (Try a *wrong* word
   first — it stays locked. The DM never volunteers the password.)
6. `take the crown`, then `is there anywhere further to go?` → leaving the terminal
   vault triggers the **victory epilogue**.
   - *Fixed geography:* try `go north` / `find a secret passage` anywhere — the DM
     only offers declared exits and refuses to invent one.

---

## demo_spells_items.json — Spells & items

```bash
python -m src.main data/demos/demo_spells_items.json
```

**Party:** Aldric (cleric — `guiding_bolt`, `sacred_flame` cantrip, a healing
potion), Wisp (mage with **exactly one** L1 slot, `max_spell_slots` capped at 1).
**Foes:** three kobolds. Loot: a `pearl_of_power`.

**Shows:** the slot-economy **money shot** (a second cast fails when slots run out),
free cantrips, the **Pearl-of-Power cap** (refused when slots are already full, and
*not* consumed), restoring a spent slot, `use_item` self-heal, and `lookup_rule`.

**Play it:**
1. `/state` → confirm Wisp has `L1:1`. `we attack the kobolds!` → `start_combat`.
2. On Wisp's turn: `Wisp casts magic missile at Zik` → slot goes 1 → 0, damage applied
   atomically.
3. Later Wisp turn: `Wisp casts magic missile at Zak again` → **`cast_spell` returns
   ok=false** (no slots); the DM is forced to narrate the fizzle. Then
   `Wisp casts fire bolt at Zak` → a **cantrip is free** and works.
4. Pick up and use the Pearl: `Wisp grabs the Pearl of Power` (`take_item`). With her
   slot already spent, `Wisp crushes the Pearl of Power` → restores L1 back to 1.
   (If you try the Pearl while she is **at full** slots it is **refused** —
   `slots_full` — and stays in inventory. Try it before step 2 to see this.)
   *Note:* in combat, using an item is that character's action and is turn-guarded.
5. `Aldric drinks his healing potion` → `use_item` self-heal (roll-and-apply).
6. `how do spell slots work?` → the DM answers via `lookup_rule`. Clear the kobolds
   to win.

---

## demo_skill_checks.json — Checks (proactive `skill_check`)

```bash
python -m src.main data/demos/demo_skill_checks.json
```

**Party:** Bram (STR +4, the muscle), Senna (WIS +3 / DEX +3, the eyes), Orin
(INT +4, the scholar) — each strong in a different ability. **Scene 1
(proving_hall):** three challenges, one per talent — a loose flagstone (a
**perception** find), a jammed portcullis (an **athletics** heave), and a
rune-sealed door whose cipher needs reading (an **arcana/investigation** check).
**Scene 2 (sentinel_span):** a Bone Sentinel guarding a narrow span over a chasm —
terminal.

**Shows:** `skill_check` as a proactive `d20 + ability modifier` vs a DC, the
ability modifier (not proficiency) driving the roll so a different PC shines on each
test, a **fail-forward** consequence applied with `apply_dice`, a check **gating
progress** via a quest flag, and — in the fight — a `skill_check` that **is the
acting character's turn-guarded action** (the reactive `saving_throw` twin lives in
`demo_saving_throws.json`).

> A check never adds proficiency — only the raw ability modifier — which is the
> deliberate contrast with a proficient *saving throw*. Watch `/trace`: each test is
> a single `skill_check`; a failed heave is followed by an `apply_dice`.

**Play it:**
1. `Senna scans the floor for anything out of place` → a **perception** (WIS) check.
   On a success the DM reveals the cache under the loose flagstone — `take the
   healing potion` (`take_item`). Senna's high WIS makes her the one to spot it.
2. `Bram heaves the jammed portcullis open` → an **athletics** (STR) check; Bram's
   +4 makes it likely. *Fail-forward:* if the check fails, the slab slips and the DM
   applies a few points with `apply_dice` (e.g. `1d4` to Bram) — a failed check costs
   something rather than just stalling. (`/state` shows the HP dip; `/trace` shows
   `skill_check` then `apply_dice`.)
3. `Orin studies the glyph-lock and reads the cipher` → an **arcana/investigation**
   (INT) check. On a success the DM records the discovery as the `cipher_read` quest
   flag (see it in `/state`). Try the door **before** this — `go through the rune
   door` is refused (**locked**) until the flag is set.
4. `now open the rune door and go through` → `move_scene` succeeds into the span.
   - *Wrong PC, on purpose:* have Bram attempt the arcana check (`Bram tries to read
     the glyphs`) — he can roll, but his INT −1 makes failure likely; the point is the
     ability modifier, not who declares it.
5. On the span, `we attack the Bone Sentinel` → `start_combat`. On **Bram's** turn,
   instead of swinging: `Bram tries to shove the Sentinel off the span` → an
   **in-combat `skill_check`** (athletics). This **is Bram's action** and is
   turn-guarded — try it on someone else's turn and the engine refuses; try a second
   action after it and the action guard refuses. The DM narrates the shove from the
   result.
6. Drop the Sentinel to clear the terminal span and fire the **victory epilogue**;
   `take the warden token` first if you like.

---

## demo_saving_throws.json — Saving throws & hazards/traps

```bash
python -m src.main data/demos/demo_saving_throws.json
```

**Party:** Aldric (proficient in WIS/CHA saves, carries a healing potion), Kael
(DEX/INT saves), Wisp (INT/WIS saves). **Scene 1 (trapped_gallery):** an author-placed
DEX dart-trap (`floor_darts`, hidden) and a CON poison-spore cloud (`spore_cloud`,
save-for-half). **Scene 2 (fear_sanctum):** a WIS fear-ward guarding the relic —
terminal victory.

**Shows:** `trigger_hazard` springing **author-placed traps** whose save ability, DC,
and damage live in the scene's `hazards` manifest — the engine rolls each save and
applies the damage atomically, and the model never sees or supplies the numbers; the
**hidden** flag (the dart trap isn't telegraphed until it springs); **save-for-half**
(the spore cloud); proficiency-aware saves (Kael's DEX-proficient save beats the dart
trap more easily); and the contrast with a **bare `saving_throw`** for the fear-ward
(a one-off, non-damage effect with a DM-set DC — not every save is a hazard).

> The state snapshot lists hazards by **id and name only** — the DC and damage stay
> engine-owned. Watch `/trace`: a hazard resolves in a single `trigger_hazard` call
> (save + damage), whereas the fear-ward is a plain `saving_throw`.

**Play it:**
1. `we cross the gallery` → the DM springs `floor_darts` with `trigger_hazard` (it's a
   hidden trap, so it shouldn't have been telegraphed). The engine rolls each PC's DEX
   save against the authored DC and applies `2d6` to those who fail — Kael (DEX
   proficient) fares best. (`/state` shows the HP drop; `/trace` shows one `trigger_hazard`.)
2. `we push through the spores` → the DM springs `spore_cloud` (visible, save-for-half):
   a CON save; failers take full `1d6` poison, successes take half.
3. `Aldric drinks his healing potion` if someone's hurt (`use_item`), then `take the
   healing draught` and `go through the far arch`.
4. In the sanctum, `we approach the reliquary` → this is **not** a hazard but a one-off
   fear effect: the DM calls a bare **WIS `saving_throw`** (DC from the prose; Aldric and
   Wisp are proficient, Kael isn't). Then `take the Pale Sigil` and `is there anywhere
   further to go?` → leaving the terminal sanctum fires the **victory epilogue**.
   - *Try to re-spring a trap:* step back onto the dart plate — `trigger_hazard` returns
     `already_sprung` (one-shot hazards fire once).
   - *Check vs save:* ask for a *check* ("Kael studies the plate — perception check") to
     see `skill_check` add **no** proficiency where the DEX *save* on the same character does.

## demo_reinforcements.json — Reinforcements (`add_npc`)

```bash
python -m src.main data/demos/demo_reinforcements.json
```

**Party:** Aldric, Kael, Wisp. **Scene (alarm_post):** Grik (goblin) beside a brass
alarm-horn. The scene declares a `reinforcements` manifest: `goblin_reserve` (Skab,
available immediately) and `ogre_enforcer` (Grukk, **gated** behind the
`alarm_raised` flag).

**Shows:** that `add_npc` can spawn **only author-declared** reinforcements (never an
arbitrary monster), a **trigger-gated** wave hidden until its flag is set, mid-combat
initiative insertion (the new foe slots into the order without disturbing the active
turn), and one-spawn-per-id.

**Play it:**
1. `we attack Grik!` → `start_combat` with the party and Grik.
2. As the fight develops, the DM may bring in the ready reserve — narrated as a goblin
   charging in from a side tunnel (`add_npc goblin_reserve`). Watch `/trace` for the
   `add_npc` call and the updated initiative order. It can only ever be **Skab** — the
   manifest is the sole authority.
3. Trigger the gated wave: `stop Grik before he reaches the horn!` — if Grik sounds the
   alarm, the DM records `alarm_raised`, which **unlocks** `ogre_enforcer`. The DM then
   brings Grukk crashing in (`add_npc ogre_enforcer`), inserted at his rolled
   initiative slot. (Before the flag is set, an attempt to spawn the ogre is refused —
   it's hidden and `locked`.)
4. Survive the wave to win. Each reinforcement can arrive only once.

---

## demo_flat_effects.json — Flat effects & flavor rolls (`modify_hp`, `roll_dice`)

```bash
python -m src.main data/demos/demo_flat_effects.json
```

**Party:** Aldric (a cleric, starting **wounded at 18/24** so a heal is visible) and
Wisp (16/16). **Scene (warded_vault):** a single, peaceful terminal room with three
curiosities — a warding glyph that deals a *flat, exact* toll, a restorative font that
heals a *flat, exact* amount, and a burst strongbox of loose coin to be *counted* —
plus the silver reliquary to claim before leaving.

**Shows:** the two tools no other demo exercises. `modify_hp` for a **flat, known**
HP change — the engine applies an exact amount the fiction states, *not* a dice roll
(that would be `apply_dice`) and *not* an author-placed trap (that would be
`trigger_hazard`, which needs a `hazards` manifest — there is none here). And
`roll_dice` for **fiction-only** randomness that touches no tracked state (the coin
count) — the roll the engine forbids from feeding HP.

> Watch `/trace`: the ward and font fire a single `modify_hp` each (exact ±amount);
> the coin count fires `roll_dice` and **no** `modify_hp`. `/state` shows HP move by
> precisely the stated number, and the font heal **clamping at max HP**.

**Play it:**
1. `Wisp counts the spilled coins` → a **`roll_dice`** flavor roll (e.g. `3d6`×10 gp):
   the DM narrates the haul, and **nothing changes** in `/state` — fiction-only.
2. `Aldric lays his hand on the warding runes` → **`modify_hp` −6** on Aldric
   (`/state`: 18 → 12, exactly 6, no roll). The flat toll the scene names.
3. `Aldric drinks from the restorative font` → **`modify_hp` +8** healing (12 → 20).
   Then `Aldric drinks again` — the font has gone dark, so the DM narrates no effect;
   or have a full-HP PC drink to watch the heal **clamp at max** rather than overfill.
4. `take the silver reliquary` (`take_item`), then `we have what we came for — let's
   leave` → the engine concludes the terminal scene and fires the **victory epilogue**.

> The bound is real too: `modify_hp` refuses a magnitude larger than the target's max
> HP (reason `amount_out_of_range`) — a flat effect can't exceed a full bar in one hit.

---

## stormhold_keep.json — Branching geography

```bash
python -m src.main data/adventures/stormhold_keep.json
```

**Party:** Aldric, Kael, Wisp. A five-scene crawl through Stormhold Keep whose map
forks and then reconverges:

```
1 Storm Gate ─→ 2 Great Hall ┬─→ 3 High Ramparts ──┐
                             └─→ 4 Flooded Crypt ───┴─→ 5 Throne Sanctum (terminal)
```

Scene 2 offers **two** exits; scenes 3 and 4 are different routes that both lead to
the same scene 5. This is the demo for non-linear scene geography — the engine holds
the model to the declared exits, so it can only offer the paths that exist, and a
played-through branch genuinely skips the other route's content.

**Shows:** a fork with a meaningful choice (the two routes differ — see below),
two paths converging on one destination, fixed author-declared geography across five
scenes, and a terminal boss whose defeat fires the victory epilogue. Along the way it
also composes the other features: a fight (or optional parley) in scene 2, and a
choice between a **direct brute fight** (ramparts, reward: a greater healing potion)
and a **stealthy undead route** (crypt — ambushable foes, reward: a Pearl of Power).

**Play it:**
1. `search the gate, then go into the great hall` → loot the messenger's
   `healing_potion`, then `move_scene` to scene 2.
2. In the Great Hall, fight or talk down the two raiders (`Wisp persuades Dax to
   stand aside` uses `influence_npc`). Then **pick a route**:
   - **High road:** `we take the spiral stair up to the ramparts` → fight Hookjaw the
     ogre, `take the greater healing potion`, then `go through the captain's door`.
   - **Low road:** `we head down the cellar steps into the crypt` → `we sneak up on the
     skeletons` (`attempt_ambush`) for a surprise round, `take the Pearl of Power`,
     then `take the submerged passage up`.
3. Either route arrives at the **Throne Sanctum** (scene 5). Defeat Captain Vexis to
   trigger the victory epilogue; `take the stormhold signet` first if you like.
   - *Geography check:* at the fork, try `is there a back way out?` — the DM only
     offers the two declared exits and won't invent a third. Replay and take the other
     route to see the content you skipped.

## Persistence & resume (any scenario)

The save/resume round-trip works from any of the above:

1. Part-way through a session: `/save my_run` → writes `saves/my_run.json` plus trace
   sidecars (`my_run.trace.jsonl`, `my_run_stats_trace.json`).
2. `/quit`.
3. Resume exactly where you left off — same HP, slots, scene, combat state, flags:
   ```bash
   python -m src.main saves/my_run.json
   ```

The game also offers to save at the end of a run (after a victory or defeat
epilogue), so you can keep the completed transcript and trace.

**Autosave & `/undo` (any scenario).** Every turn is autosaved to
`saves/autosave.json`, so a crash or accidental `/quit` loses nothing — resume with
`python -m src.main saves/autosave.json`. And at any prompt, `/undo` rewinds the
last turn (state, narration, and trace all roll back); type it twice to step back
two turns. Try it in any demo: take an action, `/state` to see the change, `/undo`,
then `/state` again to watch it revert.
