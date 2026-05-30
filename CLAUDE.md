# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

An agentic Dungeon Master for a single-session tabletop RPG. The LLM narrates and decides what to *attempt*, but every game number (dice, HP, spell slots) is owned and enforced by deterministic Python code. The LLM cannot override a tool result — if `cast_spell` returns `ok=false`, the spell fails in the fiction too.

## Commands

```bash
pip install -r requirements.txt
# set ANTHROPIC_API_KEY in a .env file (python-dotenv loads it automatically)

python -m pytest -q                     # 7 enforcement tests — no API needed
python -m src.main                      # start a session with the default scenario
python -m src.main mysave.json          # resume from a saved game
```

In-session commands: `/state`, `/save [path]`, `/quit`.

## Architecture

The core loop lives in `dm_agent.py::DMAgent.take_turn`:

1. Append the player's input (with current location/party summary) to the conversation.
2. Call `client.messages.create` with the full `TOOLS` schema.
3. If `stop_reason == "tool_use"`, execute every requested tool via `tools.dispatch()`, feed results back as `tool_result` blocks, and loop (capped at `MAX_TOOL_HOPS = 12`).
4. When the model stops calling tools, return its final text as narration.

**State is enforced in code, not by the model.** `rules.py` contains the enforcement core — functions like `cast_spell` and `attack` mutate `GameState` and return structured dicts; the model only sees the result and must narrate around it. Tests in `test_rules.py` prove this with no API calls.

**Key data flow:**
- `game_state.py` — `Character`, `NPC`, `GameState` dataclasses; JSON save/load
- `rules.py` — dice (`roll`), `attack`, `cast_spell`, `apply_damage`, `heal`, `lookup_rule`; seeded `_rng` for deterministic tests
- `tools.py` — `TOOLS` list (Anthropic tool schemas) + `dispatch()` that routes tool names to `rules` functions against live state
- `dm_agent.py` — the agentic loop; `MODEL` constant is the only place the model name appears

## Model configuration

The model string is set once in `dm_agent.py` at the top-level `MODEL` constant. Update it there when upgrading Claude versions — nowhere else needs changing. Current: `claude-sonnet-4-6`.

## Extending

Good next tools to add: `skill_check` (d20 + ability modifier vs DC), `use_item`, `add_npc`, initiative ordering. Add the Anthropic schema to `TOOLS` in `tools.py`, add a dispatch branch in `dispatch()`, and add the mechanic to `rules.py`. The test file covers enforcement — add a test for any new rules function.

Spell-slot enforcement (`test_rules.py::test_spell_slots_run_out`) is the canonical demo of the agent/chatbot distinction: the model requests a second cast, the engine refuses, the model narrates failure. Preserve this invariant when adding new mechanics.
