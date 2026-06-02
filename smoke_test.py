#!/usr/bin/env python3
"""Smoke-test runner: replay every demo's scripted inputs against the live model.

Usage:
    python smoke_test.py [suffix]

Examples:
    python smoke_test.py            # saves saves/demo_combat.json, ...
    python smoke_test.py _1         # saves saves/demo_combat_1.json, ...
    python smoke_test.py _2         # a second run, saved alongside the first

For each demo under data/demos/ that has a matching input script in
data/demos/scripts/<demo>.txt, this:

  1. seeds the dice RNG (so dice are steady across runs),
  2. loads the scenario and builds a DMAgent,
  3. feeds the scripted player inputs to take_turn one at a time, stopping early
     if the game ends, and
  4. saves the final GameState as saves/<demo><suffix>.json — reusing the same
     writer (`_do_save`) as the in-session `/save` command, so the path and
     format match exactly, plus a saves/<demo><suffix>_stats_trace.json sidecar
     (per-turn tool calls + API stats, built the same way `/save` does).

The optional suffix is appended to each demo's filename, so repeated runs
(_1, _2, ...) don't clobber each other. This matters because each run calls the
Anthropic API and is NOT deterministic — `--seed`/`rules.seed` only fixes the
dice, not the model's tool choices or narration. Requires ANTHROPIC_API_KEY.

This is an *integration smoke test* (does a full demo run end-to-end without
throwing? does it reach an ending?), not a correctness test — the deterministic
guarantees (slot economy, gates, turn order, redaction) are covered by the
no-API pytest suite. The scripts encode each demo's documented happy path
(see DEMOS.md) and may need a tweak after a real keyed run.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

from src import rules
from src.dm_agent import DMAgent
from src.game_state import GameState
from src.main import _do_save  # reuse the exact /save writer (same path + format)
from src.views import _build_stats_trace, set_plain

DEMO_DIR = Path("data/demos")
SCRIPT_DIR = DEMO_DIR / "scripts"
SEED = 42  # fixed so dice are reproducible run-to-run (the model still varies)


def load_inputs(script_path: Path) -> list[str]:
    """Read a script file into a list of player inputs, skipping blanks and comments."""
    inputs = []
    for raw in script_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        inputs.append(line)
    return inputs


def run_demo(scenario_path: Path, script_path: Path, suffix: str) -> dict:
    """Run one demo end-to-end and save its final state. Returns a result record."""
    inputs = load_inputs(script_path)
    rules.seed(SEED)
    state = GameState.load(str(scenario_path))
    agent = DMAgent(state)

    turns = 0
    for line in inputs:
        agent.take_turn(line)
        turns += 1
        if state.game_over:
            break

    save_name = scenario_path.stem + suffix
    stats = _build_stats_trace(agent.full_trace)
    status, where = _do_save(state, save_name, overwrite=True, stats_trace=stats)
    return {
        "demo": scenario_path.stem,
        "turns": turns,
        "of": len(inputs),
        "game_over": state.game_over,
        "outcome": state.game_outcome or "(unfinished)",
        "save_status": status,
        "save_path": str(where),
    }


def main() -> int:
    load_dotenv()  # pull ANTHROPIC_API_KEY from a .env at the project root
    suffix = sys.argv[1] if len(sys.argv) > 1 else ""
    set_plain(True)  # no color codes in the smoke log

    scripts = sorted(SCRIPT_DIR.glob("*.txt"))
    if not scripts:
        print(f"No input scripts found in {SCRIPT_DIR}/.")
        return 1

    print(f"Smoke test — {len(scripts)} demo(s), seed {SEED}, suffix {suffix!r}\n")
    results: list[dict] = []
    failures = 0

    for script_path in scripts:
        scenario_path = DEMO_DIR / f"{script_path.stem}.json"
        if not scenario_path.exists():
            print(f"  SKIP  {script_path.stem}: no scenario {scenario_path}")
            continue
        print(f"  RUN   {script_path.stem} …", end="", flush=True)
        try:
            rec = run_demo(scenario_path, script_path, suffix)
        except Exception as exc:  # a crashed turn is a smoke-test failure, not fatal
            failures += 1
            print(f" FAIL ({type(exc).__name__}: {exc})")
            traceback.print_exc()
            continue
        if rec["save_status"] != "saved":
            failures += 1
        flag = "ok" if rec["game_over"] else "no-end"
        print(
            f" {flag}  {rec['turns']}/{rec['of']} turns, "
            f"outcome={rec['outcome']}, saved→{rec['save_path']} ({rec['save_status']})"
        )
        results.append(rec)

    print(f"\n{len(results)} ran, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
