"""Play a session from the terminal.

Usage:
    python -m src.main                 # load data/scenario.json
    python -m src.main mysave.json     # load a saved game

In-session commands: /state  /trace  /save [path]  /quit
"""

from __future__ import annotations

import os
import sys

from .dm_agent import DMAgent
from .game_state import GameState

DEFAULT_SCENARIO = os.path.join(os.path.dirname(__file__), "..", "data", "scenario.json")


def print_state(state: GameState) -> None:
    print(f"\n  Location: {state.location}")
    for c in state.party.values():
        spells = ", ".join(f"L{lvl}:{n}" for lvl, n in sorted(c.spell_slots.items())) or "none"
        status = ", ".join(c.conditions) or "ok"
        print(f"  {c.name}: HP {c.hp}/{c.max_hp} | spells {spells} | {status}")
        print(f"    Inventory: {', '.join(c.inventory) if c.inventory else '—'}")
    for n in state.npcs.values():
        print(f"  {n.name} (NPC): HP {n.hp}/{n.max_hp}{' [down]' if n.is_down else ''}")
    if state.combat_round > 0:
        all_actors = {**state.party, **state.npcs}
        order = " → ".join(
            all_actors[k].name if k in all_actors else k
            for k in state.combat_order
        )
        active_key = state.combat_order[state.combat_index]
        active_name = all_actors[active_key].name if active_key in all_actors else active_key
        print(f"  Combat: round {state.combat_round} | {order} | up: {active_name}")
    else:
        print("  Combat: not in combat")
    print()


def print_tool_trace(trace: list) -> None:
    if not trace:
        return
    print("  [tools]")
    for call in trace:
        result_summary = {k: v for k, v in call["result"].items() if k != "state"}
        print(f"    {call['name']}({call['input']}) -> {result_summary}")
    print()


def print_full_trace(full_trace: list) -> None:
    if not full_trace:
        print("  No tool calls recorded yet.\n")
        return
    for entry in full_trace:
        print(f"  [Turn {entry['turn']}] > {entry.get('input', '')}")
        if not entry["calls"]:
            print("    (no tool calls)")
        for call in entry["calls"]:
            result_summary = {k: v for k, v in call["result"].items() if k != "state"}
            print(f"    {call['name']}({call['input']}) -> {result_summary}")
    print()


def main() -> None:
    argv = sys.argv[1:]
    debug = "--debug" in argv
    argv = [a for a in argv if a != "--debug"]
    path = argv[0] if argv else DEFAULT_SCENARIO
    state = GameState.load(path)
    agent = DMAgent(state)

    print("=" * 60)
    print("  DM AGENT — type /state, /trace, /save, or /quit at any time")
    print("=" * 60)
    if state.scene:
        print(f"\n{state.scene}\n")

    while True:
        try:
            player = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nFarewell, adventurer.")
            break

        if not player:
            continue
        if player == "/quit":
            break
        if player == "/state":
            print_state(state)
            continue
        if player == "/trace":
            print_full_trace(agent.full_trace)
            continue
        if player.startswith("/save"):
            parts = player.split(maxsplit=1)
            out = parts[1] if len(parts) > 1 else "savegame.json"
            state.save(out)
            print(f"  saved -> {out}")
            continue

        narration = agent.take_turn(player)
        print(f"\n{narration}\n")
        if debug:
            print_tool_trace(agent.tool_trace)
            print_state(state)


if __name__ == "__main__":
    main()
