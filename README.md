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
  author-placed, and the password-relay rule (relay what the player *says*, never
  supply the word). These are deliberate, logged in `DECISIONS.md`, and the
  surfaces that *can* be hardened (e.g. snapshot redaction) are.

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
against an author-set `disposition_dc` (`None` = unreachable by talk). Success flips
a hostile NPC to neutral; attacking a calmed NPC re-provokes it; and de-escalating
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

**Checks & saves.** `skill_check` resolves a *proactive* `d20 + ability modifier` vs
a DC (perception, persuasion, athletics, stealth…) and, in combat, is the acting
character's turn-guarded action. `saving_throw` is its *reactive* twin for **resisting**
an effect (DEX vs a trap, CON vs poison, WIS vs fear): it is not an action, isn't
turn-guarded, and can be rolled for any affected character on anyone's turn — and a
character proficient in the save (its `save_proficiencies`) adds proficiency, which a
plain check never does. The engine owns the roll; the model applies the consequence of a
failed save through `apply_dice`/`modify_hp`.

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

**Quest flags.** Boolean story markers recording that something happened (a clue
read, a seal broken), surfaced each turn and used to gate ways and endings.
*(Hardening to strict-boolean values is in progress — see roadmap.)*

**Persistence & resume.** JSON save/load with a full round-trip (HP, slots, scene,
combat, flags), mid-session resume, sensible defaults for older saves, and
template-based NPC spawning from the scenario file.

**Scenario validator.** `python -m src.validate <scenario.json>` lints an authored
scenario before play, catching content bugs that would otherwise only surface
mid-session: exits that point at nonexistent scenes, NPC/reinforcement `template`s
not in `MONSTERS`, hazard `damage` that isn't valid dice or an unknown save ability,
scene/party entries whose keys would crash the loader, and the subtle one — a gate
`requires` flag that isn't in normalized form, which `set_quest_flag` could never
satisfy. Every check is derived from how the engine actually consumes the data, so it
can't drift from a separate schema. Issues are split into **errors** (the scenario
will misbehave or fail to load) and **warnings** (a likely mistake with an engine
fallback); exit status is non-zero only on errors.

**Observability & CLI.** A tool trace (`/trace`, `/full_trace`) shows exactly what
the agent decided each turn, alongside a per-call stats sidecar capturing latency and
token usage — including prompt-cache reads and writes. In-session commands include
`/help`, `/state`, `/recap` (replay the story so far), `/roll <notation>` (open
flavor rolls), `/undo` (rewind the last turn), and `/save`. The game **autosaves**
to `saves/autosave.json` after every turn for crash-safe resume.

**Performance.** The static system-prompt-plus-tools prefix is cached across calls,
and every API call is instrumented per phase. Profiling showed the run is
**decode-bound** (~30 tok/s, with caching fully engaged and *not* the bottleneck),
which sets the direction for the latency work below.

## Roadmap

**In progress / specced.**


- **Latency.** *(Mostly done — see DECISIONS.md §5–6.)* The wasted terminating
  generation is no longer thrown away: out of combat it *is* the narration, and in
  combat one call narrates the player action plus all NPC beats. Narration also
  **streams** to the terminal as it generates (behind a leak gate), so the player
  reads from the first token. A profiling harness (`profile_api.py`) measures the
  before/after.
- **Two-model split (potential).** Run the mechanical tool-selection phase on a
  faster, cheaper model (e.g. Haiku) while keeping the quality model for narration.
  Tool decisions are largely mechanical (map the player's words to the right tool +
  args), so they may not need the top model; narration is where prose quality matters.
  Would need a second model constant in `dm_agent.py` and routing the tool-use
  `client` calls to it. Open question: whether the cheaper model picks tools/args
  reliably enough to keep enforcement clean.

## Need to implement

## Potential implementations

Future work, ranked roughly least → most difficult to implement.

*CLI & quality-of-life (all terminal, mostly cheap):*

- **Status HUD:** a compact header/footer each prompt — party HP bars, slots, conditions, and in combat the round + initiative order with the active actor highlighted and dying/dead/companion markers. Reformats data `/state` already has.
- **Color & Markdown output:** render scene text, NPC names, and inline dice via `rich` (with a `--plain` fallback for pipes/CI), plus a spinner during the pre-stream API latency.
- **`/cost` and `/export`:** summarize session tokens + estimated cost from the stats sidecar, and write the transcript to a shareable Markdown session log.
- **Input history:** `readline` (stdlib) for arrow-key recall and line editing at the prompt.
- **`--seed` flag:** fix the dice RNG for a whole session for reproducible demos and bug reports.
- **API retry/backoff:** wrap the model calls so a rate-limit or network blip mid-turn doesn't abort the session.

*Mechanics:*

- **Concentration:** one concentration spell at a time, broken by damage — pairs with the multi-category spell engine.
- **Inspiration / luck point:** a once-per-session reroll the DM can award.
- **Gold ledger:** a tracked party currency (a `gold` total plus add/spend tools) so loot and rewards carry a real number.
- **Equipment → AC:** an armor table so worn gear sets a character's AC instead of a flat value.
- **Advantage/disadvantage:** roll 2d20 and take the higher (or lower), threaded through attacks and checks.
- **Resting:** a short/long rest that restores HP and spell slots between encounters.
- **Conditions beyond unconscious:** prone, poisoned, frightened, restrained, etc., with mechanical effects on rolls.
- **Enemy-initiated stealth:** let foes ambush the party — the mirror of `attempt_ambush`.
- **Merchants (buy/sell):** shopkeeper NPCs with inventories that trade against the gold ledger.
- **Utility & healing spells:** expand the spell table beyond damage (shield, bless, cure wounds).
- **Multi-category spell engine:** today `cast_damaging_spell` resolves only single-target *damage* (auto-hit or spell-attack); generalize it into a category-aware resolver that also handles saving-throw spells (save for half), area-of-effect multi-target, healing, buffs/utility, and condition-inflicting spells — so the broad SRD spell list in `rules.SPELLS` is mechanically resolved, not just narrated.
- **Fail-forward system:** failed checks that advance the fiction with a cost or complication instead of dead-ending — partial-success outcomes baked into resolution.
- **Structured quests:** tracked multi-step objectives with prerequisites and completion state, beyond today's single-flag gates.
- **Reactions & bonus actions:** a richer action economy (opportunity attacks, off-hand attacks, reactions).
- **Movement & range:** positioning, distances, and reach/range bands.
- **Areas of effect:** templated multi-target spells and abilities (depends on positioning).
- **XP & leveling:** experience, level-ups, and growing stats/slots across a session.
- **Character creation:** build a party at the start of a session instead of authored pre-generated characters.
- **The big swings:** the full 5e ruleset, multiplayer, persistent multi-session campaigns, image/voice, a polished GUI, and procedural map generation.

## Layout

```
src/
  game_state.py        # Character / NPC / GameState dataclasses + JSON save/load
  rules.py             # deterministic mechanics: dice, attack, spells, damage/heal,
                       #   death saves, monsters, consumables, SRD-lite
  tools.py             # Anthropic tool schemas + dispatch() against live state
  dm_agent.py          # tool-use loop with folded narration, caching, instrumentation
  main.py              # terminal REPL: /state /undo /trace /full_trace /save /quit + autosave
  validate.py          # scenario linter: python -m src.validate <scenario.json>
data/
  scenario.json        # the demo adventure
  demo_*.json          # per-feature demo scenarios
  DEMOS.md             # index of the demos + how to trigger each feature
tests/
  test_rules.py        # ~300 enforcement tests — no API needed
  test_answer_gate.py  # answer-gated-exit behaviour + redaction, no API needed
DECISIONS.md           # architecture decision log (the soft/hard boundaries, caching, …)
```

## Run

```bash
pip install -r requirements.txt
cp .env.example .env && $EDITOR .env      # add your ANTHROPIC_API_KEY
python -m pytest -q                       # ~300 enforcement tests, no API needed
python -m src.main                        # play
python -m src.main data/scenario.json     # explicit scenario, or a savegame path to resume
```

> Confirm the current model string in `src/dm_agent.py` (`MODEL`) against
> https://docs.claude.com/en/docs/about-claude/models — it's the only place the
> model is named.

## Demos

**Per-feature demo scenarios.** [`data/DEMOS.md`](data/DEMOS.md) is an index of
focused, ready-to-run scenarios (`data/demo_*.json`) — one per feature-cluster
(combat, death saves, social & companions, stealth, gates & loot, spells & items,
reinforcements) — each with the exact player inputs to type to trigger the feature
and what to watch for in `/state` and `/trace`.

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

Roughly 300 tests across `tests/`, all running with **no API**. They drive the
rules engine, the tool dispatch, and the agent loop (with a mocked client) to prove
the hard boundaries: slot economy, clamped/atomic damage, the full death-save and
endgame logic, turn-order and surprise handling, social de-escalation, and
answer-gate matching plus redaction. What unit tests can't reach by construction —
whether the model drives the verified machinery correctly in live play — is the job
of a real playthrough.

## Mechanics note

Uses a simplified subset of the D&D 5e SRD (CC-BY-4.0). Expand `rules.SRD_RULES`
and the combat math as you go.