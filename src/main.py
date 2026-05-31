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
import json
import os
from pathlib import Path

from .dm_agent import DMAgent
from .game_state import GameState
from .rules import CONSUMABLES

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


def _do_save(
    game_state,
    raw: str,
    base_dir: Path = SAVE_DIR,
    overwrite: bool = False,
    *,
    trace: list,
    stats_trace: list | None = None,
) -> tuple:
    """Resolve path and write state; return (status, path_or_message).

    status values: "saved", "exists" (no-clobber), "error".
    Never raises — all failures are captured and returned as ("error", msg).

    When trace is provided, also writes a sidecar at <name>.trace.jsonl —
    one JSON record per list element, one per line.
    When stats_trace is provided, also writes <name>_stats_trace.json.
    Sidecar failures are silently swallowed so they never block the save.
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
    if trace:
        sidecar = path.with_suffix(".trace.jsonl")
        try:
            with open(sidecar, "w") as _f:
                for record in trace:
                    _f.write(json.dumps(record) + "\n")
        except Exception:
            pass
    if stats_trace is not None:
        stats_path = path.with_name(path.stem + "_stats_trace.json")
        try:
            with open(stats_path, "w") as _f:
                json.dump(stats_trace, _f, indent=2)
        except Exception:
            pass
    return ("saved", path)


def print_state(state: GameState) -> None:
    print(f"\n  Location: {state.location}")
    for c in state.party.values():
        slots = ", ".join(f"L{lvl}:{n}" for lvl, n in sorted(c.spell_slots.items())) or "none"
        status = ", ".join(c.conditions) or "ok"
        print(f"  {c.name}: HP {c.hp}/{c.max_hp} | slots {slots} | {status}")
        def _fmt_item(item: str) -> str:
            return f"{item} (consumable)" if item.lower() in CONSUMABLES else item
        inv = ", ".join(_fmt_item(i) for i in c.inventory) if c.inventory else "—"
        print(f"    Inventory: {inv}")
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


def _launch_mode(gs) -> str:
    """Return 'resume' if any play has happened, 'new' otherwise."""
    if gs.narrative or gs.turn > 0 or gs.combat_round > 0:
        return "resume"
    return "new"


def _resume_opening(gs, n: int = 1) -> str:
    """Return the last n DM narration beats joined by blank lines."""
    tail = gs.narrative[-n:] if gs.narrative else []
    return "\n\n".join(e["text"] for e in tail)


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


def _build_stats_trace(full_trace: list) -> list:
    """Build a per-turn stats structure: tool calls + api call timing/tokens."""
    result = []
    for entry in full_trace:
        result.append({
            "turn": entry["turn"],
            "player_input": entry.get("input", ""),
            "tool_calls": [
                {"name": c["name"], "input": c["input"], "result": c["result"]}
                for c in entry["calls"]
            ],
            "api_calls": entry.get("api_calls", []),
        })
    return result


def print_full_trace_verbose(full_trace: list) -> None:
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
        for ac in entry.get("api_calls", []):
            u = ac["usage"]
            tokens = f"in={u['input']} out={u['output']}"
            if u.get("cache_read"):
                tokens += f" cache_read={u['cache_read']}"
            if u.get("cache_write"):
                tokens += f" cache_write={u['cache_write']}"
            print(f"    api:{ac['phase']} {ac['elapsed']:.1f}s | {tokens}")
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
    args = parser.parse_args()
    state = GameState.load(args.scenario)
    agent = DMAgent(state)

    print("=" * 60)
    print("  DM AGENT — type /state, /trace, /full_trace, /save, or /quit at any time")
    print(f"  Scenario: {args.scenario}")
    print("=" * 60)
    mode = _launch_mode(state)
    if mode == "resume":
        opening = _resume_opening(state)
        if opening:
            print(f"\n{opening}\n")
    elif state.scene:
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
        if player == "/full_trace":
            print_full_trace_verbose(agent.full_trace)
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
            # Flatten per-turn groups into one record per tool call.
            trace = [
                {"turn": entry["turn"], **call}
                for entry in agent.full_trace
                for call in entry["calls"]
            ]
            stats = _build_stats_trace(agent.full_trace)
            status, val = _do_save(state, raw, trace=trace, stats_trace=stats)
            if status == "saved":
                print(f"  Saved to {val}")
            elif status == "exists":
                try:
                    confirm = input(f"  {val} exists — overwrite? (y/N): ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                if confirm == "y":
                    status2, val2 = _do_save(state, raw, overwrite=True, trace=trace, stats_trace=stats)
                    if status2 == "saved":
                        print(f"  Saved to {val2}")
                    else:
                        print(f"  {val2}")
            else:
                print(f"  {val}")
            continue

        narration = agent.take_turn(player)
        print(f"\n{narration}\n")
        if state.game_over:
            print("— The End —")
            try:
                answer = input("  Save this run? (Y/N): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                answer = "n"
            if answer == "y":
                try:
                    raw = input("  Save as: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    raw = ""
                if raw:
                    end_trace = [
                        {"turn": entry["turn"], **call}
                        for entry in agent.full_trace
                        for call in entry["calls"]
                    ]
                    end_stats = _build_stats_trace(agent.full_trace)
                    status, val = _do_save(state, raw, trace=end_trace, stats_trace=end_stats)
                    if status == "saved":
                        print(f"  Saved to {val}")
                    elif status == "exists":
                        try:
                            confirm = input(f"  {val} exists — overwrite? (y/N): ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            print()
                            confirm = "n"
                        if confirm == "y":
                            status2, val2 = _do_save(state, raw, overwrite=True, trace=end_trace, stats_trace=end_stats)
                            print(f"  {'Saved to ' + val2 if status2 == 'saved' else val2}")
                    else:
                        print(f"  {val}")
            break


if __name__ == "__main__":
    main()
