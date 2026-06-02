# DM Agent

An agentic Dungeon Master for a single-session tabletop RPG. The model narrates,
voices NPCs, and adapts to player choices — but **every game number is owned and
enforced by code**, not by the model's memory. Dice, hit points, spell slots,
death saves, and turn order live in a deterministic rules engine; the LLM can only
*request* an action, which the engine grants or denies.

That separation is the whole point. It's what makes this an **agent** (an
observe → decide → act → observe loop with real tools and enforced state) rather
than a chatbot that happens to mention dice. The model is told, in the system
prompt, that it knows no numbers and must call a tool for every roll, attack,
spell, and state read — and that it must respect a tool result even when the
result thwarts the story (a failed spell *fizzles*). The enforcement is also real
in code: `cast_spell` decrements actual slots and returns `ok=false` when they're
gone, so the model literally cannot cheat.

## Quickstart

```bash
pip install -r requirements.txt          # in a virtualenv — see Run for the full setup
cp .env.example .env && $EDITOR .env     # add your ANTHROPIC_API_KEY
python3 -m src.main                       # play the default scenario
```

See [Run](#run) for the isolated-venv setup, [Demos](#demos) for guided feature
walkthroughs, and [`data/ADVENTURES.md`](data/ADVENTURES.md) for full play-throughs.

## The design in one picture

```
   player input
        │
        ▼
┌──────────────────┐   requests a tool   ┌─────────────────┐
│     DM agent     │ ──────────────────▶ │  tool dispatch  │
│   resolve tools  │                     │ (deterministic) │
│  then narrate ── │ ◀────────────────── │  rules + state  │
└──────────────────┘   enforced result   └─────────────────┘
        │      ▲                                   │
        ▼      └──── redacted snapshot ◀───────────┤
   narration       (injected each turn)            ▼
   to player                                  game_state.py
                                          (HP, slots, NPCs, flags)
```

Each turn runs a **tool-use loop** where the model resolves the action by calling
tools, then **narrates** the enforced outcome. Narration is folded into the loop
rather than spent on a separate call: out of combat the loop's terminating turn
*is* the narration, and in combat a single call narrates the player's action plus
every engine-resolved NPC beat in order. A compact, *redacted* state snapshot is
injected every turn so the model always sees current numbers — but never the things
it isn't supposed to know (hidden stealth DCs, gate passwords). Because prose is now
produced with tool results in context, the leak screens (`_extract_narration` /
`_sanitize_narration`) are load-bearing — see DECISIONS.md §5. In the terminal, that
prose **streams** as it generates, behind a leak gate that holds the opening until a
state-dump can be ruled out (DECISIONS.md §6).

## How enforcement works

The boundary comes in two strengths:

- **Hard boundaries — enforced in code.** Slots, HP, dice, turn order, death
  saves, AC checks. The engine is the single source of truth; a tool result
  cannot be overridden by narration. These are covered by the no-API test suite.
- **Soft boundaries — enforced by prompt, documented as such.** A handful of
  rules can't be expressed purely in code and are held by the system prompt:
  target selection (don't pick a target the player didn't name), loot being
  author-placed, the password-relay rule (relay what the player *says*, never
  supply the word), asking instead of guessing when a social attempt's actor or
  approach is unspecified, concluding a hostile-free terminal scene only when the
  player signals they're leaving, and *whether* to award inspiration (the budget
  and the reroll stay engine-owned; only the judgment to grant it is the model's).
  These are deliberate, logged in `DECISIONS.md`, and the surfaces that *can* be
  hardened (e.g. snapshot redaction) are.

## Features (implemented)

**Combat.** Turn-based, multi-actor combat with rolled initiative and a strict
turn guard — you can only act on your turn, and the engine owns the pointer.
Weapons resolve from inventory + ability modifiers (finesse picks the better of
STR/DEX), roll to-hit against AC, and crit on a natural 20 (which doubles into the
damage and death-save paths). HP is clamped at zero; damage and healing are atomic
through the tool that *rolls and applies together*, so fiction-only dice can't be
laundered into real HP changes. `start_combat` / `end_combat` / `next_turn` are
engine-driven, combat auto-ends when one side is down, and unnamed targets trigger
auto-selection or an `ambiguous_target` re-prompt.

**Death, downed state & endgame.** A full death-save cycle: downed at 0 HP,
`roll_death_save` each turn while dying (natural 20 revives to 1 HP, natural 1
counts as two failures, three successes stabilize, three failures kill). Damage
while down adds failures (a crit adds two); massive overkill is instant death;
healing resets the whole state. A party wipe produces a defeat epilogue; clearing
a terminal scene produces a victory epilogue. The engine decides when the run ends.

**Social & companions.** `influence_npc` allows one persuasion attempt per NPC
against a `disposition_dc` (`None` = unreachable by talk). Each monster template
carries a **randomly-assigned default** `disposition_dc` (it is not a 5e stat, unlike
`alertness_dc`, which derives from passive Perception); authors can override it per-NPC.
Success flips a hostile NPC to neutral; attacking a calmed NPC re-provokes it; and de-escalating
the *last* hostile ends combat. In combat it costs the actor's action. A won-over NPC
can then be recruited with `recruit_npc`: a **companion** follows the party across
scenes and, once added to `start_combat`, fights hostiles on the party's side (the
engine resolves its attacks automatically). A companion doesn't replace a party
member — a full party wipe is still a defeat even if it survives.

**Stealth & ambush.** `attempt_ambush` rolls group stealth against the highest
`alertness_dc` present (`None` = always alert, can't be ambushed). Winning grants a
surprise round in which surprised hostiles are skipped on round 1 — including the
edge case where a surprised NPC sits first in the initiative order.

**Exploration: scenes, gates & loot.** Multi-scene adventures with fixed,
author-declared geography — the model narrates only the exits that exist and can't
invent a passage. Transitions can be gated two ways: **flag gates** (a key or met
condition) and **answer gates** (a spoken password the engine owns and matches,
with the word redacted from everything the model sees, so it must relay the
player's words rather than supply the secret). Loot is author-placed and obvious on
look, picked up with `take_item`.

**Spells & items.** A real spell-slot economy (`cast_spell` decrements actual slots
and refuses when tapped out; cantrips are free). Slots are capped at the starting
allotment, so a restoring item (Pearl of Power) can't over-fill — at the cap it's
refused and not consumed. A broad SRD set of single-target damaging spells (cantrips
through 9th level — fire bolt, magic missile, fireball, disintegrate, meteor swarm, …)
rolls and applies atomically and threads crits; save-for-half and area spells currently
resolve as single-target full damage (see the spell-engine item in the roadmap).
Consumables apply through
`use_item` / `apply_consumable`; an item can be used on oneself or **administered to
a party ally** — pouring a healing potion into a downed ally revives them and resets
their death saves, spending the giver's action. `lookup_rule` serves an SRD-lite reference.

**Gold.** A per-character coin purse (`Character.gold`), tracked as another engine-owned
resource. `add_gold` credits loot and rewards; `spend_gold` debits purchases and bribes —
and like the spell-slot economy it **refuses an overspend** (`insufficient_gold`, with
nothing deducted), so the model narrates "you can't afford it" rather than conjuring coin
the character doesn't have. Each PC's balance round-trips through save/load and surfaces in
`/state` and the HUD. The foundation the roadmap's **merchants** build on.

**Checks & saves.** `skill_check` resolves a *proactive* `d20 + ability modifier` vs
a DC (perception, persuasion, athletics, stealth…) and, in combat, is the acting
character's turn-guarded action. Naming a 5e **skill** (e.g. `stealth`, `persuasion`)
makes the engine pick that skill's governing ability and add the character's
`proficiency_bonus` when they're in `skill_proficiencies` — twice for `expertise` — so a
rogue's Stealth genuinely beats a raw DEX check; a bare ability check (no skill named)
adds none. `saving_throw` is its *reactive* twin for **resisting** an effect (DEX vs a
trap, CON vs poison, WIS vs fear): it is not an action, isn't turn-guarded, can be rolled
for any affected character on anyone's turn, and adds proficiency for a proficient save
(its `save_proficiencies`). The skill→ability map, the proficiency math, and the roll are
all engine-owned; the model only names the skill and applies the consequence of a failed
save through `apply_dice`/`modify_hp`.

**Inspiration.** A single DM-awarded reroll, built as another capped engine resource — the
same shape as the spell-slot economy. The DM may grant a character inspiration
(`award_inspiration`) to reward clever play or strong roleplay; that *award* is a soft
judgment, but the budget is hard: a character holds at most one and gets only one for the
entire session — once spent it is lifetime-locked and can never be re-awarded (refused
`at_cap`/`already_used`). The player spends it by setting `use_inspiration` on that
character's `skill_check` or `saving_throw`, and the engine rolls **2d20 and keeps the
higher** inside the same atomic roll, then spends the point (trying to spend with none held
rolls normally and reports `inspiration_used: false`). The model's discretion only decides
*whether* a reroll is offered — never the number, which stays in the engine.

**Advantage & disadvantage.** Attacks and spell attacks can be rolled with advantage or
disadvantage: the model sets `advantage`/`disadvantage` on `attack` / `cast_spell` when the
fiction clearly favors or hinders the attacker (flanking or a prone target vs. a blinded
attacker), and the engine rolls **2d20 and keeps the higher (or lower)** — they cancel to a
straight roll if both are set, and an auto-hit spell like `magic_missile` ignores them. As
with inspiration, the *judgment* to apply it is the model's, but the dice and the kept
result are engine-owned (the shared `rules._d20` primitive also backs inspiration's
keep-higher reroll on checks and saves).

**Hazards & traps.** Author-placed scene dangers — a dart trap, a spore cloud, a
rune-ward — declared in a scene's `hazards` manifest and sprung with `trigger_hazard`.
The manifest is the sole authority (mirroring loot, exits, and reinforcements): the model
may only trigger a declared hazard, never invent one, and **never supplies the save
ability, DC, or damage** — those are author-owned and the engine rolls the save and
applies the damage atomically for every affected character (full on a fail, half if the
hazard is save-for-half, none on a success). Hazards can be gated behind a quest flag
(armed only after a trigger), fire once or repeatedly, and be marked `hidden` so a
concealed trap isn't telegraphed before it springs. This is the engine-authoritative
upgrade of describing a trap's DC in prose: the numbers leave the model's hands entirely.

**Quest flags.** Story markers recording that something happened (a clue read, a seal
broken), surfaced each turn and used to gate ways and endings. Values are JSON primitives
(usually booleans); the boundary is two-layer — *hard:* a reserved-key denylist
(`hp, max_hp, ac, spell_slots, damage, initiative`), JSON-primitive validation, and string
redaction in everything the model sees; *soft:* an action-vs-discovery prompt rule for what
qualifies as a flag (see DECISIONS.md, "quest_flags hold narrative facts only").

**Persistence & resume.** JSON save/load with a full round-trip (HP, slots, scene,
combat, flags), mid-session resume, sensible defaults for older saves, and
template-based NPC spawning from the scenario file.

**Scenario validator.** `python3 -m src.validate <scenario.json>` lints an authored
scenario before play, catching content bugs that would otherwise only surface
mid-session: exits that point at nonexistent scenes, NPC/reinforcement `template`s
not in `MONSTERS`, hazard `damage` that isn't valid dice or an unknown save ability,
scene/party entries whose keys would crash the loader, **wrong-typed field values**
that load fine but crash the engine math later (a non-integer `hp`/`ac`/`dc`, an
`ability_modifiers` value or a list field that isn't the right type), unknown
top-level keys, sanity issues that load but make no sense at play (an `hp` above
`max_hp`, a non-positive `max_hp`, or two actors that share a name — which makes
`find_actor` unable to target both), and the subtle one — a gate
`requires` flag that isn't in normalized form, which `set_quest_flag` could never
satisfy. Every check is derived from how the engine actually consumes the data, so it
can't drift from a separate schema. Issues are split into **errors** (the scenario
will misbehave or fail to load) and **warnings** (a likely mistake with an engine
fallback); exit status is non-zero only on errors.

**Observability & CLI.** A tool trace (`/trace`, `/full_trace`) shows exactly what
the agent decided each turn, alongside a per-call stats sidecar capturing latency and
token usage — including prompt-cache reads and writes. A compact **status HUD**
prints before each prompt — per-PC HP bars, spell slots, and conditions, plus
indented sub-lines for each caster's known spells (grouped by level) and inventory
(an Items line for gear and a Consumables line with quantities), and — in combat —
the round and initiative order with the active actor marked and dying/dead/companion
tags (toggle with `/hud` or start with `--no-hud`). In-session commands include
`/help`, `/state` (a full readout: per-PC HP/AC, known spells grouped by level with
their slot budget, death-save progress, inventory, companion allies, hostiles, and
the current scene's exits and loot), `/recap` (replay the story so far),
`/roll <notation>` (open flavor rolls), `/undo` (rewind the last turn), `/cost`
(session token usage and estimated spend, derived from the per-call stats),
`/export` (write the story so far to a shareable Markdown session log), and `/save`.
The game **autosaves** to `saves/autosave.json` after every turn for crash-safe resume.
Transient model-call failures (a rate limit, a 5xx, a network blip) are **retried with
exponential backoff** — honoring a `Retry-After` header when present — so a passing hiccup
doesn't abort a turn; deterministic 4xx errors surface immediately. A `--seed N` flag fixes
the dice RNG for the whole session (reproducible rolls for demos and bug reports).
Output is **color- and Markdown-rendered** with [`rich`](https://github.com/Textualize/rich)
when stdout is a terminal — scene/recap prose as Markdown, NPC names colored by
disposition, the HUD colored by health (HP bars green/yellow/red) and side, inline dice
highlighted, and a *thinking* spinner over the pre-stream API latency — with `--plain`
(auto-on when piped or non-tty) for clean text in pipes/CI.
`rich` is a soft dependency: absent it, the app degrades to plain text. The prompt
also has **input history** — `readline` (stdlib) gives arrow-key recall and line
editing, persisted across sessions to `saves/.input_history` (best-effort; a quiet
no-op where `readline` is unavailable, e.g. stock Windows).

**Performance.** Profiling showed the run is **decode-bound** (~30 tok/s, with
caching fully engaged and *not* the bottleneck) and that wall time is dominated by a
~3.3s fixed cost *per API call* — so **calls-per-turn**, not output size, is the lever
the optimizations below pull. Every API call is instrumented per phase (`/cost` and
the stats sidecar break out prompt-cache reads vs. writes).

*Caching & lean context.* The static system-prompt-plus-tools prefix is cached across
every call (`cache_control` on both the system block and the tools array). Context is
rebuilt fresh each turn — a redacted state snapshot plus the last `NARRATION_WINDOW`
narration pairs — so per-turn cost stays flat instead of growing with session length,
and stale `tool_use`/`tool_result` blocks are never carried forward (their effects
already live in the engine state). The snapshot is kept lean: it injects live numbers
(HP, slots, flags, scene) but **not** the unbounded session history
(`transcript`/`narrative`/`log`), which the model never acts on — re-injecting it once
cost ~3.4k tokens in profiling and rode through every later hop of that turn.

*Fewer calls per turn.* Narration is folded into the tool loop rather than spent on a
separate call (DECISIONS.md §5): out of combat the loop's terminating turn *is* the
narration. In combat the player's action is resolved tool-only and a single
`_narrate_turn` covers it plus every NPC beat in order (DECISIONS.md §2). Three further
call-count cuts live in the loop:

- **Combat fast-path.** Once the active PC's action is spent, the tool loop breaks
  immediately rather than paying for a terminal model turn whose prose would be
  scrubbed anyway — saving one call on every combat player turn.
- **Engine-resolved NPC turns.** A plain hostile or companion attack is resolved
  entirely in Python (`resolve_npc_action`) with **no API call**; a synthetic
  `[Engine: …]` context line is injected so the unified narration still has the exact
  hit/miss/damage facts.
- **Batched NPC fallbacks.** NPC turns the engine *can't* resolve deterministically are
  queued and flushed in a single `_execute` call, not one call per NPC.

*Bounded output.* Each narration phase requests a right-sized `max_tokens` budget
(400 for the post-combat close, 300 for the epilogue, `min(256 × beats, 1024)` for the
unified turn), so the decode-bound generation never runs longer than the prose needs.

## Roadmap

### In progress / specced

**Latency.** *(Mostly done — see DECISIONS.md §5–6 and the Performance section
  above.)* The wasted terminating generation is no longer thrown away: out of combat
  it *is* the narration, and in combat one call narrates the player action plus all NPC
  beats. On top of that the loop now skips the spent-action terminal turn in combat,
  resolves plain NPC attacks in-engine with no API call, batches un-resolvable NPC
  turns into one call, and right-sizes each narration `max_tokens`. Narration also
  **streams** to the terminal as it generates (behind a leak gate), so the player reads
  from the first token. `get_state` was trimmed of unbounded session history. (Profiling
  was driven off the per-call stats sidecar surfaced by `/cost`.) The remaining levers
  are captured in the table below.

**Remaining latency levers** — recorded here so they aren't lost. Profiling a full run
showed a ~3.3s fixed cost *per API call* (so calls-per-turn is the lever, not output
size), with wall time splitting roughly **40% tool-selection / 52% narration**. Ranked
by payoff vs. risk:

| Lever | Payoff | Risk |
|---|---|---|
| **Two-model split** — run the mechanical tool-selection ("thinking") calls on a faster, cheaper model (e.g. Haiku), keep the quality model for narration. Needs a second model constant in `dm_agent.py` and routing the tool-use `client` calls to it. | High — ~40% of wall time is mechanical tool-selection | Medium — the cheaper model must still pick the right tool/args, or enforcement narration breaks |
| **Lean harder on parallel `tool_use`** to cut hops on multi-tool turns (e.g. two `take_item`s + a `move_scene` resolved in one response instead of three) | Low–medium | Low code, but prompt-level and unreliable |
| **Prompt narration for more brevity** (the `max_tokens` budgets are already right-sized per phase; this is the prose-quality dial) | Medium — narration is ~half of wall time | Trades prose quality, which the project prioritizes |

## Need to implement

*(Nothing queued — the CLI & quality-of-life items here, input history, `--seed`, and
API retry/backoff, are all implemented. New near-term work lands here.)*

## Potential implementations

Future work, ranked roughly least → most difficult to implement.

*Mechanics:*

- **Merchants (buy/sell):** shopkeeper NPCs with a stock and price list, plus `buy_item`/`sell_item` tools that move entries between the merchant and a PC's `inventory` and debit/credit the **gold ledger** (its prerequisite). Reuses the loot / `take_item` plumbing.
- **Temporary HP:** a separate hit-point buffer (from `false life`, `heroism`, a paladin's lay-on-hands variants, etc.) that absorbs damage **before** real HP, is **not** restored by healing, doesn't stack (take the higher of old/new), and is cleared on a long rest. Add a `temp_hp` field on `Character`/`NPC`, drain it first in `apply_damage`, and have `heal` ignore it. Pairs with the **utility & healing spells** work.
- **Enemy-initiated stealth:** the mirror of `attempt_ambush` — let a scene or NPC roll stealth against the party's passive Perception to open with a surprise round *against* the PCs. Reuses the surprise plumbing already in `start_combat` (the `surprised` flag and round-1 skip).
- **Resting:** a `rest` tool (short/long) backed by a `rules.rest()` helper that restores HP toward `max_hp`, refills `spell_slots` to `max_spell_slots`, and on a long rest clears **temporary HP** and any once-per-rest feature budget. Pairs with class resources that recharge on a rest.
- **Equipment → AC:** an armor table (alongside `rules.WEAPONS`/`MONSTERS`) mapping worn gear to an AC formula (base + capped DEX), with an `equip`/`unequip` tool that recomputes `Character.ac` instead of using the authored flat value. Pairs with the **Equipment Table** below.
- **Conditions beyond unconscious:** today `Character.conditions` is a free-text list only `unconscious`/`dead` act on. Give named conditions mechanical teeth — prone/restrained and poisoned grant dis/advantage (via the existing `_d20` flags), frightened blocks approaching the source — applied in `attack`/`skill_check`/`saving_throw`. Pairs with the spell & hazard work that *inflicts* them.
- **Damage types — resistance / immunity / vulnerability:** weapons and spells already carry a `damage_type`, but `apply_damage` ignores it. Add per-actor `resistances` / `immunities` / `vulnerabilities` (lists of damage types) and have the damage path **halve / zero / double** the amount by type before applying it. Requires threading `damage_type` from `attack` / `cast_damaging_spell` / `trigger_hazard` into `apply_damage`, which today takes a bare amount. Core to monster identity — a skeleton resisting piercing, a fire elemental immune to fire — and currently `damage_type` is purely cosmetic.
- **Fail-forward system:** bake partial success into resolution — have `skill_check` return a margin / outcome band ("success at a cost", "fail forward") plus a prompt rule, so a miss advances the fiction with a complication instead of dead-ending. Partly modeled already by the skill-check demo's `apply_dice`-on-failure.
- **Structured quests:** a quest object (id, ordered steps, prerequisites, completion state) layered over today's flat `quest_flags`, surfaced in the state snapshot and `/state`, with tools to advance/complete a step. Generalizes the single-flag gates that exits, hazards, and reinforcements already key off.
- **Equipment Table and Functionality:** a table (alongside `rules.WEAPONS`/`CONSUMABLES`) describing non-weapon gear — `spellbook`, `holy symbol`, `thieves tools` — and what each enables, so an item in `inventory` becomes a prerequisite the engine checks (e.g. a cleric needs a holy symbol to cast). More involved: each item needs a hook into the relevant system.
- **Multi-category spell engine:** today `cast_damaging_spell` resolves only single-target *damage* (auto-hit or spell-attack); generalize it into a category-aware resolver that also handles saving-throw spells (save for half), area-of-effect multi-target, healing, buffs/utility, and condition-inflicting spells — so the broad SRD spell list in `rules.SPELLS` is mechanically resolved, not just narrated.
- **Utility & healing spells:** expand `rules.SPELLS` beyond `effect: "damage"` with healing (`cure wounds`), buffs (`bless`, `shield`), and utility entries — resolved mechanically by the **multi-category spell engine** (its prerequisite) rather than just narrated.
- **Concentration:** track one concentration spell per caster (a `concentrating_on` field) that a buff/control spell sets and incoming damage forces a CON `saving_throw` to keep, dropping the effect on failure. Depends on the **multi-category spell engine** (which spells concentrate) and **conditions** (the effects it sustains).
- **Monster multiattack & special abilities:** `resolve_npc_action` resolves exactly one basic attack per NPC turn. Real 5e monsters get **multiattack** (N attacks per turn) plus signature abilities — breath weapons (a save-for-half cone), regeneration (the troll), pack tactics (advantage when an ally is adjacent), monster spellcasting, and at the high end legendary actions/resistances. Model an `actions` / `multiattack` manifest on the monster stat block in `MONSTERS` and extend the engine-driven NPC turn to run it. This is the combat-fidelity counterpart to the multi-category spell engine, and the thinnest part of the sim today (NPCs are single basic attackers).
- **Movement & range:** positioning, distances, and reach/range bands on the combat state, giving melee-vs-ranged, opportunity attacks, and cover a spatial basis. A prerequisite for **areas of effect** and most of **reactions & bonus actions**.
- **Reactions & bonus actions:** a richer action economy — today a turn is one action guarded by `GameState.action_used`. Add separate `bonus_action_used`/`reaction_used` budgets reset by `next_turn`, off-hand attacks, and opportunity attacks when a foe leaves reach (needs **movement & range**). Underpins many **class features**.
- **Areas of effect:** templated multi-target spells and abilities (cone, line, radius) that select every actor in the template. Depends on **movement & range** for positioning, and feeds the **multi-category spell engine**'s AoE category.
- **Races / species & traits:** a species layer applied at character build that grants passive traits — darkvision, type-specific damage resistances (e.g. dwarven resistance to poison), movement quirks, an extra language, advantage on certain saves, and small ability adjustments. Mostly data plus a few hooks into the systems above (resistances, advantage). Pairs with character creation.
- **Classes & class features:** the biggest structural gap — and the thing that most makes a character feel like *D&D*. Today a `Character` is a generic stat-block (ability mods, attack bonus, slots); 5e characters are defined by a **class** and its level-scaling **features** — Sneak Attack, Rage, Action Surge, Channel Divinity, Fighting Styles, and class-driven spellcasting progression. Needs a class/subclass model plus a feature system that hooks the engine (extra damage dice on a qualifying hit, a granted bonus action, a to-hit/AC/save modifier, resource pools like ki or superiority dice). Depends on most of the systems above (action economy, conditions, resources) and feeds **XP & leveling** (features unlock by level) and **character creation**.
- **XP & leveling:** track party experience, award it on encounter completion, and level up — growing `max_hp`, `proficiency_bonus`, and `spell_slots`/`max_spell_slots`, and unlocking **class features** by level via a `rules`-owned progression table. Depends on **classes & class features**.
- **Character creation:** an interactive party builder at session start — pick **race**, **class**, abilities, and starting gear — instead of authored pre-gens. The front-end that ties together races, classes, the equipment table, and skill/ability setup.

## The BIG swings - (largest scope, most ambitious):*

- **Full 5e ruleset:** the complete SRD — every class, subclass, spell, monster, and rule interaction — rather than the simplified subset modeled today.
- **Multiplayer:** multiple human players sharing one session (turn handoff, per-player input), instead of a single player driving the whole party.
- **Persistent multi-session campaigns:** carry characters, inventory, and world state across sessions — saved progression beyond the single-session scope this is built for.
- **Image / voice:** generated scene and character art plus spoken narration and voice input, beyond the text terminal.
- **Polished GUI:** a graphical client (web or desktop) in place of the terminal REPL.
- **Procedural map generation:** engine-generated scene graphs and encounters instead of hand-authored scenario JSON.

## Layout

```
src/
  game_state.py        # Character / NPC / GameState dataclasses + JSON save/load
  rules.py             # deterministic mechanics: dice, attack, spells, damage/heal,
                       #   death saves, monsters, consumables, SRD-lite
  tools.py             # Anthropic tool schemas + dispatch() against live state
  dm_agent.py          # tool-use loop with folded narration, caching, instrumentation
  main.py              # terminal REPL controller: command dispatch, save/launch + autosave
  views.py             # presentation layer: HUD, /state, /recap, /roll, tool/stats traces
  validate.py          # scenario linter: python3 -m src.validate <scenario.json>
data/
  scenario.json        # the demo adventure
  DEMOS.md             # index of the demos + how to trigger each feature
  demos/               # per-feature demo scenarios (demo_*.json)
  demos/scripts/       # smoke-test input scripts (one .txt per demo)
  ADVENTURES.md        # index of the full, play-through adventures
  adventures/          # full multi-scene adventures (*.json)
smoke_test.py          # replay every demo end-to-end against the live model
.env.example           # copy to .env and add ANTHROPIC_API_KEY
tests/                 # ~671 tests total, all no-API
  test_rules.py        # 147 — enforcement core: dice, attack, spells, combat/turn guards
  test_tools.py        #  74 — dispatch, guards, target/redaction, get_state
  test_death_saves.py  #  44 — downed/dying/dead cycle + damage-while-down
  test_views.py        #  44 — rich/plain rendering: /state, /cost, /export
  test_agent.py        #  41 — agent loop: context, narration routing, closing prompts
  test_hud.py          #  39 — status HUD: bars, spells, inventory, color
  test_validate.py     #  54 — scenario linter checks
  test_scenes.py       #  27 — scene loading, move_scene, gated exits, terminal conclusion
  test_ambush.py       #  26 — attempt_ambush, surprise, companions
  test_save.py         #  23 — /save + /export helpers
  test_reinforcements.py #22 — add_npc reinforcements + available_reinforcements
  test_persistence.py  #  22 — JSON save/load round-trip
  test_items.py        #  20 — take_item / use_item / consumables
  test_social.py       #  15 — influence_npc + parley-to-combat
  test_answer_gate.py  #  15 — answer-gated exits + password redaction
  test_narrative.py    #  13 — narration recording / transcript
  test_hazards.py      #  10 — trigger_hazard traps + available_hazards
  test_retry.py        #  10 — API retry/backoff
  test_undo.py         #   9 — /undo per-turn rewind
  test_cli.py          #   9 — CLI args, --seed, launch/resume
  test_input_history.py#   6 — readline input history
  _helpers.py          #   –  shared test fixtures (no tests)
DECISIONS.md           # architecture decision log (the soft/hard boundaries, caching, …)
```

## Run

```bash
python3 -m venv .venv                     # create an isolated environment (.venv is git-ignored)
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.txt           # installs into .venv, not your system Python
cp .env.example .env && $EDITOR .env      # add your ANTHROPIC_API_KEY (auto-loaded from .env)
python3 -m pytest -q                       # ~671 enforcement tests, no API needed
python3 -m src.main                        # play
python3 -m src.main data/scenario.json     # explicit scenario, or a savegame path to resume
python3 -m src.main --seed 42              # fix the dice RNG for reproducible rolls (demos/bug reports)
```

> The virtual environment keeps this project's dependencies (`rich`, `anthropic`, …)
> out of your global/system Python. Activate it (`source .venv/bin/activate`) in each
> new shell before running; `deactivate` when you're done.

> Confirm the current model string in `src/dm_agent.py` (`MODEL`) against
> https://docs.claude.com/en/docs/about-claude/models — it's the only place the
> model is named.

## Demos

**Per-feature demo scenarios.** [`data/DEMOS.md`](data/DEMOS.md) is an index of
focused, ready-to-run scenarios (`data/demos/`) — one per feature-cluster
(combat, death saves, social & companions, stealth, gates & loot, spells & items,
skill checks, saves & hazards, reinforcements, flat effects) — each with the exact player inputs to type to trigger the feature
and what to watch for in `/state` and `/trace`.

**Full adventures.** For longer, play-through scenarios (vs. the one-feature demos),
[`data/ADVENTURES.md`](data/ADVENTURES.md) indexes the multi-scene adventures in
`data/adventures/` — each a self-contained session with its own party, a branching or
trap-laden map, and a boss — with launch and validate commands for each.

**The money shot — enforcement you can see.** The starting scenario gives the mage
**Wisp** exactly one level-1 slot, on purpose:

1. *"Wisp hurls a magic missile at the goblin."* → `cast_spell` (ok), then the
   attack resolves and the agent narrates the hit.
2. *"Wisp casts magic missile again."* → `cast_spell` returns **ok=false**; the
   agent is *forced* to narrate the spell fizzling — it wanted to continue the
   story, but enforced state stopped it.
3. *"/state"* → watch HP and slot numbers change across turns.

Step 2 is the proof, and `tests/test_rules.py::test_spell_slots_run_out` is the same
guarantee as a unit test.

**The full run — systems composing.** A boss encounter exercises the whole stack:
read a journal for the warden's name, sneak up for a surprise round (or parley to
avoid the fight), trade blows and ride the death-save cycle, then speak the password
at the answer-gated door to claim victory — with the engine owning every number and
the password from first to last.

## Testing

Roughly 671 tests across `tests/`, all running with **no API**. They drive the
rules engine, the tool dispatch, and the agent loop (with a mocked client) to prove
the hard boundaries: slot economy, clamped/atomic damage, the full death-save and
endgame logic, turn-order and surprise handling, social de-escalation, and
answer-gate matching plus redaction. What unit tests can't reach by construction —
whether the model drives the verified machinery correctly in live play — is the job
of a real playthrough.

**Smoke test — the demos, end to end.** `smoke_test.py` replays each demo's
scripted inputs (`data/demos/scripts/<demo>.txt`, lifted from the DEMOS.md
walkthroughs) against the live model, stops when the game ends, and saves the
final state:

```bash
python3 smoke_test.py            # → saves/demo_combat.json, demo_death_saves.json, …
python3 smoke_test.py _1         # suffix each save → saves/demo_combat_1.json, … (run again as _2, _3)
```

It prints a per-demo pass/`no-end` line and exits non-zero if any demo throws —
an *integration* check that a full run survives end to end, complementing the
no-API suite above. Two caveats: it calls the Anthropic API (needs
`ANTHROPIC_API_KEY`, spends tokens), and the fixed seed only steadies the
**dice** — the model is non-deterministic, so a scripted line can occasionally
desync from the game state. The suffix lets repeated runs sit side by side for
comparison.

## Mechanics note

Uses a simplified subset of the D&D 5e SRD (CC-BY-4.0). Expand `rules.SRD_RULES`
and the combat math as you go.