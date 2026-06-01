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
`_sanitize_narration`) are load-bearing — see DECISIONS.md §5.

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

**Social — talking your way out.** `influence_npc` allows one persuasion attempt
per NPC against an author-set `disposition_dc` (`None` = unreachable by talk).
Success flips a hostile NPC to neutral; attacking a calmed NPC re-provokes it; and
de-escalating the *last* hostile ends combat. In combat it costs the actor's action.

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
and refuses when tapped out; cantrips are free). Damaging spells — magic missile,
guiding bolt, chromatic orb — roll and apply atomically and thread crits.
Consumables apply through `use_item` / `apply_consumable`, and `lookup_rule` serves
an SRD-lite reference.

**Quest flags.** Boolean story markers recording that something happened (a clue
read, a seal broken), surfaced each turn and used to gate ways and endings.
*(Hardening to strict-boolean values is in progress — see roadmap.)*

**Persistence & resume.** JSON save/load with a full round-trip (HP, slots, scene,
combat, flags), mid-session resume, sensible defaults for older saves, and
template-based NPC spawning from the scenario file.

**Observability.** A tool trace (`/trace`, `/full_trace`) shows exactly what the
agent decided each turn, alongside a per-call stats sidecar capturing latency and
token usage — including prompt-cache reads and writes.

**Performance.** The static system-prompt-plus-tools prefix is cached across calls,
and every API call is instrumented per phase. Profiling showed the run is
**decode-bound** (~30 tok/s, with caching fully engaged and *not* the bottleneck),
which sets the direction for the latency work below.

## Roadmap

**In progress / specced.**


- **Latency.** *(Done — see DECISIONS.md §5.)* The wasted terminating generation is
  no longer thrown away: out of combat it *is* the narration, and in combat one call
  narrates the player action plus all NPC beats. **Still open:** run the mechanical
  tool-selection phase on a faster model while keeping the quality model for narration;
  and stream narration for perceived latency. A profiling harness (`profile_api.py`)
  measures the before/after.

**Deliberately deferred (decided, not forgotten).**

- A `max_spell_slots` cap — a slot-restoring item can currently over-fill past the
  intended maximum.
- Companions / following — a de-escalated NPC becoming an ally that travels with
  the party across scenes (today "not hostile" means *stands aside*, not *joins*).
- `use_item` on a downed ally — administering a potion to revive someone. The heal
  path already supports it; the tool is self-use only for now.

**Out of scope (future work — noted as judgment, not omission).**
Advantage/disadvantage, movement & range, areas of effect, saving throws,
conditions beyond unconscious, reactions/bonus actions, utility & healing spells,
XP/leveling, resting, character creation, enemy-initiated stealth, equipment→AC;
and the big swings: the full 5e ruleset, multiplayer, persistent multi-session
campaigns, image/voice, a polished GUI, and procedural map generation.

## Layout

```
src/
  game_state.py        # Character / NPC / GameState dataclasses + JSON save/load
  rules.py             # deterministic mechanics: dice, attack, spells, damage/heal,
                       #   death saves, monsters, consumables, SRD-lite
  tools.py             # Anthropic tool schemas + dispatch() against live state
  dm_agent.py          # tool-use loop with folded narration, caching, instrumentation
  main.py              # terminal REPL: /state /trace /full_trace /save /quit
data/
  scenario.json        # the demo adventure
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