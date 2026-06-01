"""Play a session from the terminal.

Usage:
    python -m src.main                              # load data/scenario.json
    python -m src.main data/my_scenario.json        # load a custom scenario
    python -m src.main savegame.json                # resume a saved game
    python -m src.main data/my_scenario.json --plain   # no color/Markdown/spinner

In-session commands: /help  /state  /hud  /recap  /roll  /undo  /trace  /full_trace  /cost  /export [path]  /save [path]  /quit

Output is colorized and Markdown-rendered with `rich` when stdout is a terminal;
pass --plain (or pipe/redirect output) for plain text. The game autosaves to
saves/autosave.json after every turn; resume with
`python -m src.main saves/autosave.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .dm_agent import DMAgent
from .game_state import GameState
from .views import (
    Spinner,
    _build_stats_trace,
    banner,
    format_cost,
    format_hud,
    format_transcript_markdown,
    print_full_trace,
    print_full_trace_verbose,
    print_help,
    print_recap,
    print_roll,
    print_state,
    render_markdown,
    set_plain,
)

DEFAULT_SCENARIO = os.path.join(os.path.dirname(__file__), "..", "data", "scenario.json")
SAVE_DIR = Path("saves")
AUTOSAVE_NAME = "autosave"   # rolling per-turn save in SAVE_DIR (saves/autosave.json)


def _resolve_save_path(raw: str, base_dir: Path = SAVE_DIR, ext: str = ".json") -> Path:
    """Return the full Path for a save/export file, creating base_dir if needed.

    Raises ValueError for empty/whitespace-only names. Strips directory
    components (path-traversal guard) and appends `ext` if absent (e.g. .json for
    savegames, .md for transcript exports).
    """
    name = raw.strip()
    if not name:
        raise ValueError("Save name cannot be empty.")
    name = Path(name).name  # basename only — discards any leading ../
    if not name:
        raise ValueError("Save name resolved to empty after stripping directory components.")
    if not name.lower().endswith(ext.lower()):
        name = name + ext
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


def _do_export(state, raw: str, base_dir: Path = SAVE_DIR, overwrite: bool = False) -> tuple:
    """Write the transcript as a Markdown session log; return (status, path_or_message).

    status values: "saved", "exists" (no-clobber), "empty" (nothing played yet),
    "error". Never raises — mirrors _do_save so the REPL handler can share its shape.
    Writes a .md alongside saves (the dir is git-ignored); unlike /save it is pure
    story prose — no game state, no sidecars.
    """
    markdown = format_transcript_markdown(state)
    if not markdown:
        return ("empty", "Nothing to export yet — play a turn first.")
    try:
        path = _resolve_save_path(raw, base_dir, ext=".md")
    except Exception as e:
        return ("error", str(e))
    if path.exists() and not overwrite:
        return ("exists", path)
    try:
        with open(path, "w") as f:
            f.write(markdown)
    except Exception as e:
        return ("error", str(e))
    return ("saved", path)


def _autosave(state) -> None:
    """Refresh the rolling per-turn autosave. Best-effort: never interrupts play.

    Writes only the game state (no trace sidecars) to saves/autosave.json,
    overwriting the previous turn's snapshot. Resume with
    `python -m src.main saves/autosave.json`. Disk errors are reported quietly
    but never raise, so a failed autosave can't end the session.
    """
    status, val = _do_save(state, AUTOSAVE_NAME, overwrite=True, trace=[])
    if status == "error":
        print(f"  (autosave failed: {val})")


def _launch_mode(gs) -> str:
    """Return 'resume' if any play has happened, 'new' otherwise."""
    if gs.narrative or gs.turn > 0 or gs.combat_round > 0:
        return "resume"
    return "new"


def _resume_opening(gs, n: int = 1) -> str:
    """Return the last n DM narration beats joined by blank lines."""
    tail = gs.narrative[-n:] if gs.narrative else []
    return "\n\n".join(e["text"] for e in tail)


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
        "--no-hud",
        action="store_true",
        help="don't show the compact status HUD before each prompt (toggle in-session with /hud)",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="disable color/Markdown/spinner (also auto-on when output isn't a terminal)",
    )
    args = parser.parse_args()
    set_plain(args.plain or not sys.stdout.isatty())
    state = GameState.load(args.scenario)
    agent = DMAgent(state)

    # A 'thinking' spinner fills the pre-stream API latency; the first narration
    # token stops it, then prose streams straight to stdout for low perceived latency.
    spinner = Spinner("  The DM considers…")

    def _emit_delta(text: str) -> None:
        spinner.stop()
        sys.stdout.write(text)
        sys.stdout.flush()
    agent.on_narration_delta = _emit_delta

    banner(args.scenario)
    mode = _launch_mode(state)
    if mode == "resume":
        opening = _resume_opening(state)
        if opening:
            print()
            render_markdown(opening)
            print()
    elif state.scene:
        print()
        render_markdown(state.scene)
        print()

    hud_enabled = not args.no_hud

    while True:
        if hud_enabled:
            hud = format_hud(state)
            if hud:
                print(hud)
        try:
            player = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nFarewell, adventurer.")
            break

        if not player:
            continue
        if player == "/quit":
            break
        if player == "/help":
            print_help()
            continue
        if player == "/hud":
            hud_enabled = not hud_enabled
            print(f"  HUD {'on' if hud_enabled else 'off'}.\n")
            continue
        if player == "/state":
            print_state(state)
            continue
        if player == "/recap":
            print_recap(state)
            continue
        if player.startswith("/roll"):
            print_roll(player.split(maxsplit=1)[1].strip() if " " in player else "")
            continue
        if player == "/undo":
            if agent.undo():
                _autosave(state)
                print("\n  ↩  Reverted the last turn.\n")
                opening = _resume_opening(state)
                if opening:
                    render_markdown(opening)
                    print()
            else:
                print("\n  Nothing to undo.\n")
            continue
        if player == "/trace":
            print_full_trace(agent.full_trace)
            continue
        if player == "/full_trace":
            print_full_trace_verbose(agent.full_trace)
            continue
        if player == "/cost":
            print(format_cost(agent.full_trace, agent.model))
            print()
            continue
        if player.startswith("/export"):
            parts = player.split(maxsplit=1)
            if len(parts) > 1:
                raw = parts[1]
            else:
                try:
                    raw = input("Export as: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
            status, val = _do_export(state, raw)
            if status == "saved":
                print(f"  Exported to {val}\n")
            elif status == "exists":
                try:
                    confirm = input(f"  {val} exists — overwrite? (y/N): ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                if confirm == "y":
                    status2, val2 = _do_export(state, raw, overwrite=True)
                    print(f"  {'Exported to ' + str(val2) if status2 == 'saved' else val2}\n")
            else:  # "empty" or "error"
                print(f"  {val}\n")
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

        # Narration streams to stdout via on_narration_delta as it generates; we
        # just frame it with blank lines. (take_turn still returns the full text for
        # history/logging — it is not re-printed here, to avoid doubling.)
        # The spinner covers the wait until the first streamed token, then stops.
        sys.stdout.write("\n")
        sys.stdout.flush()
        spinner.start()
        try:
            agent.take_turn(player)
        finally:
            spinner.stop()
        sys.stdout.write("\n\n")
        sys.stdout.flush()
        _autosave(state)
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
