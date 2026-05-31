"""Play a session from the terminal.

Usage:
    python -m src.main                              # load data/scenario.json
    python -m src.main data/my_scenario.json        # load a custom scenario
    python -m src.main savegame.json                # resume a saved game
    python -m src.main data/my_scenario.json --debug

In-session commands: /state  /trace  /save [path]  /quit
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .dm_agent import DMAgent
from .game_state import GameState

DEFAULT_SCENARIO = os.path.join(os.path.dirname(__file__), "..", "data", "scenario.json")
SAVE_DIR = Path("saves")


def _resolve_save_path(raw: str, base_dir: Path = SAVE_DIR) -> Path:
    """Return the full Path for a save file, creating base_dir if needed.

    Raises ValueError for empty/whitespace-only names. Strips directory
    components (path-traversal guard) and appends .json if absent.
    """
    name = raw.strip()
    if not name:
        raise ValueError("Save name cannot be empty.")
    name = Path(name).name  # basename only — discards any leading ../
    if not name:
        raise ValueError("Save name resolved to empty after stripping directory components.")
    if not name.lower().endswith(".json"):
        name = name + ".json"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / name


def _do_save(game_state, raw: str, base_dir: Path = SAVE_DIR, overwrite: bool = False) -> tuple:
    """Resolve path and write state; return (status, path_or_message).

    status values: "saved", "exists" (no-clobber), "error".
    Never raises — all failures are captured and returned as ("error", msg).
    """
    try:
        path = _resolve_save_path(raw, base_dir)
    except Exception as e:
        return ("error", str(e))
    if path.exists() and not overwrite:
        return ("exists", path)
    try:
        game_state.save(str(path))
    except Exception as e:
        return ("error", str(e))
    return ("saved", path)


def print_state(state: GameState) -> None:
    print(f"\n  Location: {state.location}")
    for c in state.party.values():
        slots = ", ".join(f"L{lvl}:{n}" for lvl, n in sorted(c.spell_slots.items())) or "none"
        status = ", ".join(c.conditions) or "ok"
        print(f"  {c.name}: HP {c.hp}/{c.max_hp} | slots {slots} | {status}")
        print(f"    Inventory: {', '.join(c.inventory) if c.inventory else '—'}")
        if c.spells:
            ability = f" [{c.spellcasting_ability}]" if c.spellcasting_ability else ""
            print(f"    Spells{ability}: {', '.join(c.spells)}")
    for n in state.npcs.values():
        disposition = "hostile" if n.hostile else "friendly"
        status = " [down]" if n.is_down else ""
        print(f"  {n.name} (NPC){status}: HP {n.hp}/{n.max_hp} | AC {n.ac} | atk +{n.attack_bonus} | {disposition}")
        if n.inventory:
            print(f"    Inventory: {', '.join(n.inventory)}")
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
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="DM Agent — agentic tabletop RPG dungeon master",
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default=DEFAULT_SCENARIO,
        help="path to a scenario or saved-game JSON (default: data/scenario.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print tool trace and state after each turn",
    )
    args = parser.parse_args()
    debug = args.debug
    state = GameState.load(args.scenario)
    agent = DMAgent(state)

    print("=" * 60)
    print("  DM AGENT — type /state, /trace, /save, or /quit at any time")
    print(f"  Scenario: {args.scenario}")
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
            if len(parts) > 1:
                raw = parts[1]
            else:
                try:
                    raw = input("Save as: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
            status, val = _do_save(state, raw)
            if status == "saved":
                print(f"  Saved to {val}")
            elif status == "exists":
                try:
                    confirm = input(f"  {val} exists — overwrite? (y/N): ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                if confirm == "y":
                    status2, val2 = _do_save(state, raw, overwrite=True)
                    if status2 == "saved":
                        print(f"  Saved to {val2}")
                    else:
                        print(f"  {val2}")
            else:
                print(f"  {val}")
            continue

        narration = agent.take_turn(player)
        print(f"\n{narration}\n")
        if debug:
            print_tool_trace(agent.tool_trace)
            print_state(state)
        if state.game_over:
            print("— The End —")
            break


if __name__ == "__main__":
    main()
