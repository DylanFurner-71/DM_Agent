# DM Agent

An agentic Dungeon Master for a single-session tabletop RPG. The model narrates,
voices NPCs, and adapts to player choices — but **every game number is owned and
enforced by code**, not by the model's memory. Dice, hit points, and spell slots
live in a deterministic rules engine; the LLM can only *request* an action, which
the engine grants or denies.

That separation is the whole point. It's what makes this an **agent** (an
observe → decide → act → observe loop with real tools and enforced state) rather
than a chatbot that happens to mention dice.

## The design in one picture

```
player input
     │
     ▼
┌──────────────┐   requests tool     ┌─────────────────┐
│   DM agent   │ ──────────────────▶ │  tool dispatch  │
│ (LLM, loops  │                     │  (deterministic)│
│  on tool_use)│ ◀────────────────── │  rules + state  │
└──────────────┘   enforced result   └─────────────────┘
     │                                        │
     ▼                                        ▼
  narration                             game_state.py
                                    (HP, slots, NPCs, flags)
```

The model is told, in the system prompt, that it does not know any numbers and
must call a tool for every roll, attack, spell, and state read — and that it must
respect a tool result even when the result thwarts the story (a failed spell
*fizzles*). The enforcement is also real in code: `cast_spell` decrements actual
slots and returns `ok=false` when they're gone, so the model literally cannot
cheat.

## Layout

```
src/
  game_state.py   # Character / NPC / GameState dataclasses + JSON save/load
  rules.py        # deterministic mechanics: dice, attack, cast_spell, damage/heal, SRD-lite
  tools.py        # Anthropic tool schemas + dispatch() against live state
  dm_agent.py     # the tool-use loop around the Messages API
  main.py         # terminal REPL: /state /save /quit
data/scenario.json # starting encounter, tuned for the demo
tests/test_rules.py # proves enforcement with no API needed
```

## Run

```bash
pip install -r requirements.txt
cp .env.example .env && $EDITOR .env      # add your ANTHROPIC_API_KEY
python -m pytest -q                       # 7 enforcement tests, no API needed
python -m src.main                        # play
```

> Confirm the current model string in `src/dm_agent.py` (`MODEL`) against
> https://docs.claude.com/en/docs/about-claude/models — it's the only place the
> model is named.

## The demo (your "money shot")

The starting scenario gives the mage **Wisp** exactly one level-1 spell slot, on
purpose. Run this sequence live:

1. *"Wisp hurls a magic missile at the goblin."* → agent calls `cast_spell` (ok),
   then `attack`/`roll_dice`, narrates the hit.
2. *"Wisp casts magic missile again."* → `cast_spell` returns **ok=false**; the
   agent is forced to narrate the spell fizzling because Wisp is tapped out.
3. *"/state"* → show the HP and slot numbers changing across turns.

Step 2 is the proof. A reviewer sees the agent *wanted* to continue the story but
the enforced state stopped it — that's the difference between an agent and
narration. `tests/test_rules.py::test_spell_slots_run_out` is the same guarantee
in a unit test.

## One-week plan

- **Day 1 — skeleton (this scaffold).** State model, rules engine, tool dispatch,
  agent loop, passing tests. Already done here.
- **Day 2 — make it play well.** Tune the system prompt, tighten narration length,
  handle multi-actor combat turns, add 2–3 more tools you find you want
  (`use_item`, `skill_check` with DC, `add_npc`).
- **Day 3 — content + persistence.** Flesh out the scenario into a short
  3-scene adventure with branching via quest flags; verify save/load mid-session.
- **Day 4 — robustness.** Cap tool hops (done), handle bad model requests
  gracefully (unknown actors already handled), add logging of the tool trace so
  you can show *what the agent decided*.
- **Day 5 — the showcase.** A scripted demo transcript, a short README GIF/asciinema,
  and a "how it works" section pointing at the enforcement. Optionally a thin web
  UI, but the terminal demo is enough.
- **Buffer (Days 6–7).** Polish, more tests, write-up.

## In scope / out of scope

**In:** single session, single party, turn-based, a simplified SRD subset,
terminal play, enforced dice/HP/slots, save/load, a tool trace.

**Out (resist these — they blow the week):** full 5e ruleset, multiplayer, a
persistent campaign across sessions, image generation, voice, a polished GUI,
procedural map generation. Note them in the README as "future work" — that reads
as judgment, not omission.


## Mechanics note

Uses a simplified subset of the D&D 5e SRD (CC-BY-4.0). Expand `rules.SRD_RULES`
and the combat math as you go.
