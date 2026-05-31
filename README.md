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

## Extend it with Claude Code

Good next prompts to hand Claude Code, in order:

- "Add a `skill_check` tool: roll d20 + a named ability modifier against a DC, and
  surface the modifiers on `Character`."
- "Add an `initiative` system so combat resolves in turn order, and have the DM
  run NPC turns automatically."
- "Add a tool-call trace: log every tool name + result per turn and print it with
  a `/trace` command, so the agent's decisions are visible."
- "Write an integration test that mocks the Anthropic client and asserts the agent
  re-prompts after a failed `cast_spell` instead of narrating success."

## Mechanics note

Uses a simplified subset of the D&D 5e SRD (CC-BY-4.0). Expand `rules.SRD_RULES`
and the combat math as you go.


Design decision: batch NPC turn narration into a single model call
Decision. Combat narration is split — the player's own action is narrated in its own dedicated call, while all auto-run NPC actions for the cycle are narrated together in one call (fed an ordered list, one beat per action) — instead of one narration call per resolved action.
Why. Per-action narration made model round-trips scale with combatant count, so a two-goblin room (scene 2) ran noticeably slower than a one-goblin room (scene 1). Batching makes NPC narration a constant single call regardless of how many NPCs act, cutting per-round latency and token cost.
The trade-off. This trades a reliability guarantee for speed. Per-action narration was introduced specifically to stop the model dropping, merging, or reordering beats — when each call holds exactly one beat, it physically can't. Batching NPC beats back into one call reintroduces some of that risk for NPC actions: the model could drop or merge a goblin's beat even though the engine applied its effect, producing a narration/engine mismatch (HP changed in state, no prose explaining it). We accept that weaker guarantee for NPCs but deliberately keep the player's own action on the strong, per-action path, because a dropped player-action beat is the worst version of that bug.
Scope — what's unaffected. This is purely a narration-presentation change. Mechanical resolution is untouched: each NPC action still runs its own tool call, its own next_turn, and all enforcement (turn ownership, action economy, rolled==applied). Engine state stays authoritative regardless of how narration is grouped, so the worst case is incomplete prose, never wrong numbers.
Guardrails. Feed the batched call the explicit ordered list with "one beat per action, in order"; keep the mocked-client test asserting the batched call receives every NPC action in resolution order; optionally add a cheap check that the beat count matches the NPC-action count, to catch a silently dropped beat.
Reversibility. Localized to the narration step in dm_agent.py — easy to revert to per-action, or make it conditional (batch above N NPCs, per-action at or below). Low lock-in, so revisit if you ever run large multi-enemy encounters, where drop/reorder risk grows with batch size.