# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

An agentic Dungeon Master for a single-session tabletop RPG. The LLM narrates and decides what to *attempt*, but every game number (dice, HP, spell slots) is owned and enforced by deterministic Python code. The LLM cannot override a tool result — if `cast_spell` returns `ok=false`, the spell fails in the fiction too.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate   # isolated env (.venv is git-ignored); Windows: .venv\Scripts\activate
pip install -r requirements.txt                      # installs into .venv, not system Python
# set ANTHROPIC_API_KEY in a .env file (python-dotenv loads it automatically)

python -m pytest -q                                  # enforcement tests — no API needed
python -m src.main                                   # default scenario (data/scenario.json)
python -m src.main data/my_scenario.json             # custom scenario
python -m src.main savegame.json                     # resume a saved game
python -m src.main --seed 42                          # fix the dice RNG for reproducible rolls
python -m src.main --help                             # show all options (--seed, --no-hud, --plain)
python -m src.validate data/scenario.json            # lint a scenario before play (no API)
```

In-session commands: `/help`, `/state`, `/hud`, `/recap`, `/roll <notation>`, `/undo`, `/trace`, `/full_trace`, `/cost`, `/export [path]`, `/save [path]`, `/quit`. The game autosaves to `saves/autosave.json` after every turn (`python -m src.main saves/autosave.json` to resume).

## Architecture

The core loop lives in `dm_agent.py::DMAgent.take_turn`:

1. Rebuild a fresh, bounded context (`NARRATION_WINDOW` recent turns) and inject a *redacted* state snapshot plus the player's input.
2. `_execute` runs the tool-use loop: call `client.messages.create` with the full `TOOLS` schema; while `stop_reason == "tool_use"`, run each tool via `tools.dispatch()`, feed results back as `tool_result` blocks, loop (capped at `MAX_TOOL_HOPS = 12`).
3. In combat, advance the engine-driven NPC turns (mostly resolved without an API call) and accumulate ordered beats.
4. Narrate. Narration is folded into as few calls as possible — out of combat the tool loop's *terminating turn* is the narration; in combat one unified call (`_narrate_turn`) covers the player action plus all NPC beats; combat-over and end-of-run use dedicated calls. In the terminal the prose **streams** behind a leak gate. See DECISIONS.md §5–6 for the full rationale and trade-offs.

**State is enforced in code, not by the model.** `rules.py` contains the enforcement core — functions like `cast_spell` and `attack` mutate `GameState` and return structured dicts; the model only sees the result and must narrate around it. Because narration is now produced with tool results in context, the leak screens (`_extract_narration` / `_sanitize_narration`) are load-bearing. Tests in `test_rules.py` prove the enforcement with no API calls.

**Key data flow:**
- `game_state.py` — `Character`, `NPC`, `GameState` dataclasses; JSON save/load
- `rules.py` — dice (`roll`), `attack`, `cast_spell`, `apply_damage`, `heal`, `lookup_rule`; seedable `_rng` (deterministic tests and the `--seed` flag)
- `tools.py` — `TOOLS` list (Anthropic tool schemas) + `dispatch()` that routes tool names to `rules` functions against live state
- `dm_agent.py` — the agentic loop; `MODEL` constant is the only place the model name appears
- `main.py` — terminal REPL: command dispatch, save/launch, per-turn autosave
- `views.py` — presentation layer: HUD, `/state`, `/recap`, `/roll`, `/cost`, `/export`, tool/stats traces (rich + plain)

## Model configuration

The model string is set once in `dm_agent.py` at the top-level `MODEL` constant. Update it there when upgrading Claude versions — nowhere else needs changing. Current: `claude-sonnet-4-6`.

## Extending

To add a new mechanic: add the Anthropic schema to `TOOLS` in `tools.py`, add a dispatch branch in `dispatch()`, and add the mechanic to `rules.py`. The test file covers enforcement — add a test for any new rules function.

Already implemented: combat, the death-save cycle, social (`influence_npc`), companions (`recruit_npc` — cross-scene allies that fight for the party), stealth (`attempt_ambush`), scene/gate/loot, spells with a capped slot economy (`max_spell_slots`), `use_item` (self or administered to a downed ally), `skill_check`, `saving_throw` (the reactive twin of `skill_check` — resisting an effect; not turn-guarded, adds proficiency for `save_proficiencies`), `trigger_hazard` (author-placed scene traps: a scene's `hazards` manifest owns the save ability/DC/damage, the engine rolls the save and applies damage atomically; gated/once/hidden like reinforcements; sprung set tracked in `GameState.sprung_hazards`), `add_npc`, and `award_inspiration` (a single DM-awarded, engine-budgeted reroll — `Character.inspiration`/`inspiration_used`; spent via `use_inspiration` on `skill_check`/`saving_throw` for 2d20-keep-higher; one per character per session, lifetime-locked once used; the award is soft, the cap and dice are engine-owned). Also implemented at the REPL layer: `/undo` (DMAgent snapshots pre-turn `GameState` via `to_dict` into a bounded `_undo_stack` and restores it in place via `GameState.restore`; the agent-side narration window and tool trace roll back too) and per-turn autosave to `saves/autosave.json`. A standalone `src/validate.py` (`python -m src.validate <scenario.json>`) lints authored scenarios against the engine tables before play — its checks derive from how the engine consumes the data (MONSTERS/WEAPONS/SPELLS/CONSUMABLES, the flag normalizer, the dataclass fields), so add a check there whenever you add an authored-content field. For what's next, see the README's "Potential implementations" (ranked) section, plus the two-model split in DECISIONS.md. Note: hazards build on `saving_throw` — `trigger_hazard` is the author-placed twin that combines the save with engine-owned `apply_dice` damage, so traps' numbers never leave the engine.

Spell-slot enforcement (`test_rules.py::test_spell_slots_run_out`) is the canonical demo of the agent/chatbot distinction: the model requests a second cast, the engine refuses, the model narrates failure. Preserve this invariant when adding new mechanics.
