"""Play a session from the terminal.

Usage:
    python -m src.main                 # load data/scenario.json
    python -m src.main mysave.json     # load a saved game

In-session commands: /state  /save [path]  /quit
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
        slots = ", ".join(f"L{lvl}:{n}" for lvl, n in sorted(c.spell_slots.items())) or "none"
        print(f"  {c.name}: HP {c.hp}/{c.max_hp} | slots {slots} | {', '.join(c.conditions) or 'ok'}")
    for n in state.npcs.values():
        print(f"  {n.name} (NPC): HP {n.hp}/{n.max_hp}{' [down]' if n.is_down else ''}")
    print()


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    state = GameState.load(path)
    agent = DMAgent(state)

    print("=" * 60)
    print("  DM AGENT — type /state, /save, or /quit at any time")
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
        if player.startswith("/save"):
            parts = player.split(maxsplit=1)
            out = parts[1] if len(parts) > 1 else "savegame.json"
            state.save(out)
            print(f"  saved -> {out}")
            continue

        narration = agent.take_turn(player)
        print(f"\n{narration}\n")


if __name__ == "__main__":
    main()
